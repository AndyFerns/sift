"""Detector building blocks.

The scanner *nominates* a candidate whenever it meets an entry whose name some rule cares
about. That alone proves nothing, so each rule then looks for evidence:

* a **manifest** - a file sitting next to the candidate (``Cargo.toml`` beside ``target/``)
* a **marker** - something inside the candidate that the tool always writes
  (``pyvenv.cfg`` inside ``.venv/``)

Confidence follows from what was found:

==============================================  ===========================
evidence                                        result
==============================================  ===========================
manifest and marker                             high
tool-reserved name plus a manifest or a marker  high
marker only                                     the rule's ``marker_only``
manifest only                                   the rule's ``manifest_only``
nothing                                         low and ambiguous
==============================================  ===========================

When two ecosystems claim the same directory with equal confidence the result is marked
ambiguous. Ambiguous artifacts are reported for review and are never cleanable.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal

from sift.core.models import Confidence, Ecosystem, Evidence

# Upper bound on how many entries we list inside a candidate while looking for markers.
_CHILD_LIMIT = 20_000


@dataclass(frozen=True, slots=True)
class DirListing:
    """Names found directly inside one directory. Links are kept apart and never followed."""

    path: str
    files: frozenset[str]
    dirs: frozenset[str]
    links: frozenset[str] = frozenset()


def is_link(entry: os.DirEntry[str]) -> bool:
    """True for symlinks and Windows junctions - anything that points somewhere else."""
    try:
        return entry.is_symlink() or entry.is_junction()
    except OSError:
        return True


def list_dir(path: str, *, limit: int | None = None) -> DirListing:
    """List ``path`` without following links. Raises OSError if it cannot be read."""
    files: set[str] = set()
    dirs: set[str] = set()
    links: set[str] = set()
    with os.scandir(path) as entries:
        for count, entry in enumerate(entries):
            if limit is not None and count >= limit:
                break
            if is_link(entry):
                links.add(entry.name)
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            (dirs if is_dir else files).add(entry.name)
    return DirListing(path, frozenset(files), frozenset(dirs), frozenset(links))


class Candidate:
    """A directory entry whose name matches at least one rule."""

    __slots__ = ("_children", "is_dir", "name", "parent", "path")

    def __init__(self, path: str, name: str, is_dir: bool, parent: DirListing) -> None:
        self.path = path
        self.name = name
        self.is_dir = is_dir
        self.parent = parent
        self._children: DirListing | None = None

    def children(self) -> DirListing:
        """Shallow listing of the candidate itself, read once and cached."""
        if self._children is None:
            if not self.is_dir:
                self._children = DirListing(self.path, frozenset(), frozenset())
            else:
                try:
                    self._children = list_dir(self.path, limit=_CHILD_LIMIT)
                except OSError:
                    self._children = DirListing(self.path, frozenset(), frozenset())
        return self._children


Check = Callable[[Candidate], Evidence | None]


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    names: tuple[str, ...]
    ecosystem: Ecosystem
    category: str
    description: str
    manifests: tuple[str, ...] = ()
    manifest_prefixes: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()
    marker_suffixes: tuple[str, ...] = ()
    check: Check | None = None
    """Custom marker test, for evidence that is not a simple child name."""
    distinctive: bool = False
    """The name is reserved by a tool (``node_modules``), unlike a generic one (``build``)."""
    require_marker: bool = False
    """Without a marker the match stays ambiguous, whatever else is present."""
    manifest_only: Confidence = Confidence.MEDIUM
    marker_only: Confidence | None = Confidence.MEDIUM
    """None means the markers are too generic to stand on their own."""
    kind: Literal["directory", "file"] = "directory"
    subpath: str | None = None
    """The artifact is this child of the matched directory (``.angular`` -> ``cache``)."""

    def evaluate(self, candidate: Candidate) -> Detection | None:
        if candidate.is_dir != (self.kind == "directory"):
            return None

        path = candidate.path
        if self.subpath is not None:
            if self.subpath not in candidate.children().dirs:
                return None
            path = os.path.join(candidate.path, self.subpath)

        evidence: list[Evidence] = []
        manifest = self._find_manifest(candidate, evidence)
        marker = self._find_marker(candidate, evidence)

        ambiguous = False
        reason: str | None = None
        corroborated = marker and (manifest or self.distinctive)
        reserved_name = manifest and self.distinctive and not self.require_marker
        if corroborated or reserved_name:
            confidence = Confidence.HIGH
        elif marker and self.marker_only is not None:
            confidence = self.marker_only
        elif manifest and not self.require_marker:
            confidence = self.manifest_only
        else:
            confidence = Confidence.LOW
            ambiguous = True
            reason = self._why_ambiguous(manifest, marker)
            evidence.append(
                Evidence(kind="name", detail=f"named {candidate.name!r}, which is not proof")
            )
        return Detection(self, path, confidence, ambiguous, evidence, reason)

    def _find_manifest(self, candidate: Candidate, evidence: list[Evidence]) -> bool:
        siblings = candidate.parent.files
        found = [name for name in self.manifests if name in siblings]
        if not found and self.manifest_prefixes:
            found = sorted(n for n in siblings if n.startswith(self.manifest_prefixes))[:1]
        for name in found[:3]:
            evidence.append(
                Evidence(
                    kind="manifest",
                    detail=f"{name} is next to it",
                    path=os.path.join(candidate.parent.path, name),
                )
            )
        return bool(found)

    def _find_marker(self, candidate: Candidate, evidence: list[Evidence]) -> bool:
        found = False
        if self.markers or self.marker_suffixes:
            inside = candidate.children()
            names = inside.files | inside.dirs
            hits = [name for name in self.markers if name in names]
            if not hits and self.marker_suffixes:
                hits = sorted(n for n in inside.files if n.endswith(self.marker_suffixes))[:1]
            for name in hits[:3]:
                evidence.append(
                    Evidence(
                        kind="marker",
                        detail=f"contains {name}",
                        path=os.path.join(candidate.path, name),
                    )
                )
            found = bool(hits)
        if self.check is not None:
            extra = self.check(candidate)
            if extra is not None:
                evidence.append(extra)
                found = True
        return found

    def _why_ambiguous(self, manifest: bool, marker: bool) -> str:
        if self.require_marker and not marker:
            wanted = " or ".join(self.markers) or "its marker"
            return f"expected {wanted} inside, but it is missing"
        if marker and not manifest:
            wanted = " or ".join(self.manifests[:3])
            return f"its contents look right, but there is no {wanted} next to it"
        return "only the name matches; no manifest or marker confirms it"


@dataclass(slots=True)
class Detection:
    rule: Rule
    path: str
    confidence: Confidence
    ambiguous: bool
    evidence: list[Evidence] = field(default_factory=list)
    review_reason: str | None = None

    @property
    def strength(self) -> tuple[int, int]:
        """Sort key: confidence first, evidence-backed before name-only."""
        return (self.confidence.rank, 0 if self.ambiguous else 1)


@dataclass(frozen=True, slots=True)
class Detector:
    """A named group of rules for one ecosystem."""

    name: str
    ecosystem: Ecosystem
    rules: tuple[Rule, ...]


class RuleSet:
    """Every active rule, indexed by the entry names that should nominate a candidate."""

    def __init__(self, detectors: Iterable[Detector]) -> None:
        self.detectors = tuple(detectors)
        self.dir_rules: dict[str, list[Rule]] = {}
        self.file_rules: dict[str, list[Rule]] = {}
        for detector in self.detectors:
            for rule in detector.rules:
                index = self.dir_rules if rule.kind == "directory" else self.file_rules
                for name in rule.names:
                    index.setdefault(name, []).append(rule)

    def classify_path(self, path: str) -> Detection | None:
        """Classify one path from scratch, the way the scanner would have met it.

        Used to re-check an artifact right before it is deleted. Returns None when the
        path is gone, is a link, or is no longer recognised as an artifact at all.
        """
        # The artifact may be the matched entry itself, or a child of it (.angular/cache).
        for matched in (path, os.path.dirname(path)):
            name = os.path.basename(matched)
            try:
                listing = list_dir(os.path.dirname(matched))
            except OSError:
                return None
            if name in listing.dirs:
                is_dir = True
            elif name in listing.files:
                is_dir = False
            else:
                continue
            detection = self.classify(Candidate(matched, name, is_dir, listing))
            if detection is not None and os.path.normcase(detection.path) == os.path.normcase(path):
                return detection
        return None

    def classify(self, candidate: Candidate) -> Detection | None:
        """Run every rule that cares about this name and settle on one answer."""
        index = self.dir_rules if candidate.is_dir else self.file_rules
        rules = index.get(candidate.name)
        if not rules:
            return None
        detections = [d for rule in rules if (d := rule.evaluate(candidate)) is not None]
        if not detections:
            return None

        best = max(d.strength for d in detections)
        top = [d for d in detections if d.strength == best]
        winner = top[0]
        rivals = [d for d in top[1:] if d.rule.ecosystem != winner.rule.ecosystem]
        if not rivals:
            return winner

        others = ", ".join(d.rule.id for d in rivals)
        if winner.ambiguous:
            # Nobody had evidence. Do not pretend to know which ecosystem this belongs to.
            names = ", ".join(d.rule.id for d in top)
            unknown = Rule(
                id=f"unknown.{candidate.name}",
                names=(candidate.name,),
                ecosystem=Ecosystem.GENERAL,
                category="unknown",
                description=f"Unconfirmed directory named {candidate.name!r}",
            )
            return Detection(
                unknown,
                winner.path,
                Confidence.LOW,
                True,
                [
                    Evidence(
                        kind="name",
                        detail=f"the name could mean {names}, but nothing confirms any of them",
                    )
                ],
                "only the name matches; no manifest or marker confirms it",
            )

        evidence = list(winner.evidence)
        for rival in rivals:
            evidence.extend(rival.evidence)
        evidence.append(Evidence(kind="conflict", detail=f"equally strong evidence for {others}"))
        return Detection(
            winner.rule,
            winner.path,
            winner.confidence,
            True,
            evidence,
            f"evidence points to both {winner.rule.id} and {others}",
        )
