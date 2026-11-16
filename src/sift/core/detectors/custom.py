"""User-defined rules from ``[[detectors.custom]]`` in the config file.

Custom rules follow the same evidence requirement as built-in ones: a rule with neither
``manifests`` nor ``markers`` can only ever produce ambiguous, non-cleanable matches.
"""

from __future__ import annotations

from collections.abc import Sequence

from sift.core.config import CustomRule
from sift.core.detectors.base import Detector, Rule
from sift.core.models import Ecosystem


def build(custom: Sequence[CustomRule]) -> Detector:
    rules: list[Rule] = []
    for entry in custom:
        if entry.dir_names:
            rules.append(
                Rule(
                    id=f"custom.{entry.name}",
                    names=tuple(entry.dir_names),
                    ecosystem=Ecosystem.CUSTOM,
                    category="custom",
                    description=entry.description,
                    manifests=tuple(entry.manifests),
                    markers=tuple(entry.markers),
                )
            )
        if entry.file_names:
            rules.append(
                Rule(
                    id=f"custom.{entry.name}",
                    names=tuple(entry.file_names),
                    kind="file",
                    ecosystem=Ecosystem.CUSTOM,
                    category="custom",
                    description=entry.description,
                    manifests=tuple(entry.manifests),
                )
            )
    return Detector(name="custom", ecosystem=Ecosystem.CUSTOM, rules=tuple(rules))
