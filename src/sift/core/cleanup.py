"""Cleanup: the only code in Sift that deletes anything.

The flow is always the same, whichever interface is driving it:

1. ``build_plan`` - the user's explicit selection becomes a :class:`CleanupPlan`. Every
   item is validated; anything unsafe is kept in the plan as *refused*, with the reason.
   Building a plan deletes nothing.
2. The interface shows the plan (the preview) and asks the user to confirm.
3. ``execute`` - requires the confirmation token that was issued for exactly that plan.
   Each item is validated again, measured, deleted, and recorded.
4. A :class:`CleanupResult` reports what was selected, deleted, failed and refused.

A user who declines at step 2 calls ``cancel`` instead, which records the cancellation and
touches nothing. Dry runs go through the same validation and stop before deleting.

Deletion is permanent. Nothing here moves files to a recycle bin or keeps a copy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import stat
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sift.core import sizing
from sift.core.detectors import RuleSet
from sift.core.models import (
    Artifact,
    CleanupItem,
    CleanupItemResult,
    CleanupPlan,
    CleanupPlanTotals,
    CleanupResult,
    CleanupStatus,
    CleanupTotals,
    Confidence,
    ScanResult,
)
from sift.core.safety import Kind, UnsafeOperation, ValidatedTarget, validate_target
from sift.core.sizing import extended_path

PLAN_LIFETIME = timedelta(minutes=10)
CONFIRMATION_WORD = "delete"
PERMANENT_NOTICE = "Deletion is permanent. Sift cannot restore deleted files."

# Tokens are only valid inside the process that issued them.
_SECRET = secrets.token_bytes(32)
_executed: set[str] = set()
_executed_lock = threading.Lock()

ProgressCallback = Callable[[int, int, CleanupItem], None]


class UnknownArtifact(Exception):
    """A selected artifact ID is not part of the scan."""

    def __init__(self, artifact_ids: Sequence[str]) -> None:
        super().__init__(f"unknown artifact id(s): {', '.join(artifact_ids)}")
        self.artifact_ids = list(artifact_ids)


def _check_classification(
    path: str, rule: str, ruleset: RuleSet, min_confidence: Confidence
) -> None:
    """The path must still be the same kind of artifact it was when it was scanned."""
    detection = ruleset.classify_path(path)
    if detection is None:
        raise UnsafeOperation("changed", "it is no longer recognised as an artifact")
    if detection.rule.id != rule:
        raise UnsafeOperation("changed", f"it is now classified as {detection.rule.id}, not {rule}")
    if detection.ambiguous:
        reason = detection.review_reason or "the evidence is ambiguous"
        raise UnsafeOperation("ambiguous", f"it is ambiguous: {reason}")
    if not detection.confidence.meets(min_confidence):
        raise UnsafeOperation(
            "low-confidence",
            f"confidence is {detection.confidence.value}, below the "
            f"{min_confidence.value} threshold",
        )


def _validate(
    path: str,
    kind: Kind,
    rule: str,
    root: str,
    ruleset: RuleSet,
    min_confidence: Confidence,
) -> ValidatedTarget:
    target = validate_target(path, root, kind)
    _check_classification(target.path, rule, ruleset, min_confidence)
    return target


def plan_item(
    artifact: Artifact, root: str, ruleset: RuleSet, min_confidence: Confidence
) -> CleanupItem:
    size = artifact.size
    if size is None:
        size = sizing.measure(artifact.path, artifact.kind)
    item = CleanupItem(
        artifact_id=artifact.id,
        name=artifact.name,
        path=artifact.path,
        relative_path=artifact.relative_path,
        kind=artifact.kind,
        ecosystem=artifact.ecosystem,
        rule=artifact.rule,
        confidence=artifact.confidence,
        file_count=size.file_count,
        logical_bytes=size.logical_bytes,
        allocated_bytes=size.allocated_bytes,
        allowed=False,
    )
    try:
        if artifact.ambiguous:
            reason = artifact.review_reason or "the evidence is ambiguous"
            raise UnsafeOperation("ambiguous", f"it is ambiguous: {reason}")
        if not artifact.confidence.meets(min_confidence):
            raise UnsafeOperation(
                "low-confidence",
                f"confidence is {artifact.confidence.value}, below the "
                f"{min_confidence.value} threshold",
            )
        target = _validate(
            artifact.path, artifact.kind, artifact.rule, root, ruleset, min_confidence
        )
    except UnsafeOperation as refusal:
        item.refusal_code = refusal.code
        item.refusal_reason = refusal.reason
        return item
    item.allowed = True
    item.identity = target.identity
    return item


def _signature(plan: CleanupPlan) -> str:
    """Bind the token to this plan's identity and to exactly the items it may delete."""
    payload = json.dumps(
        [
            plan.id,
            plan.scan_id,
            plan.root,
            plan.dry_run,
            plan.min_confidence.value,
            plan.expires_at.isoformat(),
            [
                [item.artifact_id, item.path, item.kind, item.rule, list(item.identity or ())]
                for item in plan.items
                if item.allowed
            ],
        ],
        separators=(",", ":"),
    )
    return hmac.new(_SECRET, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def build_plan(
    scan: ScanResult,
    artifact_ids: Sequence[str],
    ruleset: RuleSet,
    *,
    dry_run: bool = False,
    min_confidence: Confidence | None = None,
) -> CleanupPlan:
    """Turn an explicit selection into a previewable plan. Deletes nothing."""
    selected = list(dict.fromkeys(artifact_ids))
    by_id = {artifact.id: artifact for artifact in scan.artifacts}
    unknown = [identifier for identifier in selected if identifier not in by_id]
    if unknown:
        raise UnknownArtifact(unknown)

    threshold = min_confidence or scan.options.min_confidence
    items = [plan_item(by_id[i], scan.root, ruleset, threshold) for i in selected]
    allowed = [item for item in items if item.allowed]
    refused = [item for item in items if not item.allowed]
    now = datetime.now(UTC)
    plan = CleanupPlan(
        id=secrets.token_hex(8),
        scan_id=scan.id,
        root=scan.root,
        created_at=now,
        expires_at=now + PLAN_LIFETIME,
        dry_run=dry_run,
        min_confidence=threshold,
        items=items,
        totals=CleanupPlanTotals(
            detected_count=scan.totals.artifact_count,
            detected_bytes=scan.totals.logical_bytes,
            selected_count=len(items),
            selected_bytes=sum(item.logical_bytes for item in items),
            deletable_count=len(allowed),
            deletable_bytes=sum(item.logical_bytes for item in allowed),
            deletable_file_count=sum(item.file_count for item in allowed),
            refused_count=len(refused),
            refused_bytes=sum(item.logical_bytes for item in refused),
        ),
    )
    plan.confirmation_token = _signature(plan)
    return plan


def _remove(path: str, kind: Kind) -> list[str]:
    """Delete one validated artifact. Returns a message for each entry that survived."""
    errors: list[str] = []
    target = extended_path(path)
    long_prefix = "\\\\?\\"

    def on_error(function: Callable[..., Any], failed: str, error: BaseException) -> None:
        # Read-only files cannot be deleted on Windows until the flag is cleared.
        # lstat first: chmod would follow a symlink and change a file outside the artifact.
        retryable = function in (os.unlink, os.rmdir)
        if os.name == "nt" and retryable and isinstance(error, PermissionError):
            try:
                if not stat.S_ISLNK(os.lstat(failed).st_mode):
                    os.chmod(failed, stat.S_IWRITE)
                    function(failed)
                    return
            except OSError as again:
                error = again
        errors.append(f"{failed.removeprefix(long_prefix)}: {error}")

    if kind == "file":
        try:
            os.unlink(target)
        except OSError as exc:
            on_error(os.unlink, target, exc)
    else:
        # shutil.rmtree never follows links, and on platforms with directory file
        # descriptors it is also resistant to a directory being swapped mid-walk.
        shutil.rmtree(target, onexc=on_error)
    return errors


def _refused(item: CleanupItem, code: str | None, reason: str | None) -> CleanupItemResult:
    return CleanupItemResult(
        artifact_id=item.artifact_id,
        name=item.name,
        path=item.path,
        relative_path=item.relative_path,
        status=CleanupStatus.REFUSED,
        selected_bytes=item.logical_bytes,
        message=f"refused: {reason}" if reason else "refused",
        errors=[code] if code else [],
    )


def _revalidate(plan: CleanupPlan, item: CleanupItem, ruleset: RuleSet) -> None:
    target = _validate(item.path, item.kind, item.rule, plan.root, ruleset, plan.min_confidence)
    if item.identity is not None and target.identity != item.identity:
        raise UnsafeOperation("identity-changed", "it is not the same directory that was previewed")


def _delete_item(item: CleanupItem) -> CleanupItemResult:
    before = sizing.measure(item.path, item.kind)
    errors = _remove(item.path, item.kind)
    result = CleanupItemResult(
        artifact_id=item.artifact_id,
        name=item.name,
        path=item.path,
        relative_path=item.relative_path,
        status=CleanupStatus.DELETED,
        selected_bytes=item.logical_bytes,
        deleted_bytes=before.logical_bytes,
        deleted_files=before.file_count,
    )
    if os.path.lexists(extended_path(item.path)):
        remaining = sizing.measure(item.path, item.kind)
        result.deleted_bytes = max(before.logical_bytes - remaining.logical_bytes, 0)
        result.deleted_files = max(before.file_count - remaining.file_count, 0)
        result.failed_bytes = remaining.logical_bytes
        removed_something = result.deleted_files > 0 or result.deleted_bytes > 0
        result.status = CleanupStatus.PARTIAL if removed_something else CleanupStatus.FAILED
        result.message = "some entries could not be deleted"
        result.errors = errors[:20]
    elif errors:
        result.errors = errors[:20]
    return result


def _finish(
    plan: CleanupPlan,
    started: datetime,
    items: list[CleanupItemResult],
    *,
    cancelled: bool = False,
) -> CleanupResult:
    def total(field: str, *statuses: CleanupStatus) -> int:
        chosen = [i for i in items if not statuses or i.status in statuses]
        return sum(int(getattr(i, field)) for i in chosen)

    return CleanupResult(
        id=secrets.token_hex(8),
        plan_id=plan.id,
        scan_id=plan.scan_id,
        root=plan.root,
        started_at=started,
        finished_at=datetime.now(UTC),
        dry_run=plan.dry_run,
        cancelled=cancelled,
        items=items,
        totals=CleanupTotals(
            detected_bytes=plan.totals.detected_bytes,
            selected_bytes=plan.totals.selected_bytes,
            deleted_bytes=total("deleted_bytes"),
            failed_bytes=total("failed_bytes"),
            refused_bytes=total("selected_bytes", CleanupStatus.REFUSED),
            would_delete_bytes=total("selected_bytes", CleanupStatus.DRY_RUN),
            deleted_count=sum(1 for i in items if i.status is CleanupStatus.DELETED),
            failed_count=sum(
                1 for i in items if i.status in (CleanupStatus.FAILED, CleanupStatus.PARTIAL)
            ),
            refused_count=sum(1 for i in items if i.status is CleanupStatus.REFUSED),
        ),
    )


def verify_confirmation(plan: CleanupPlan, token: str | None) -> None:
    """Raise unless ``token`` authorises a real run of exactly this plan, right now."""
    if not token or not hmac.compare_digest(token, _signature(plan)):
        raise UnsafeOperation(
            "not-confirmed",
            "cleanup was not confirmed: the confirmation token does not match this preview",
        )
    if datetime.now(UTC) > plan.expires_at:
        raise UnsafeOperation("expired", "the preview has expired; build a new one")


def execute(
    plan: CleanupPlan,
    token: str | None,
    ruleset: RuleSet,
    *,
    progress: ProgressCallback | None = None,
) -> CleanupResult:
    """Carry out a plan.

    A dry run needs no token and deletes nothing. A real run requires the plan's
    confirmation token, can only happen once per plan, and re-validates every item
    immediately before deleting it.
    """
    started = datetime.now(UTC)
    if not plan.dry_run:
        verify_confirmation(plan, token)
        with _executed_lock:
            if plan.id in _executed:
                raise UnsafeOperation("already-executed", "this preview was already used")
            _executed.add(plan.id)

    results: list[CleanupItemResult] = []
    for index, item in enumerate(plan.items):
        if progress is not None:
            progress(index, len(plan.items), item)
        if not item.allowed:
            results.append(_refused(item, item.refusal_code, item.refusal_reason))
            continue
        try:
            _revalidate(plan, item, ruleset)
        except UnsafeOperation as refusal:
            results.append(_refused(item, refusal.code, refusal.reason))
            continue
        if plan.dry_run:
            results.append(
                CleanupItemResult(
                    artifact_id=item.artifact_id,
                    name=item.name,
                    path=item.path,
                    relative_path=item.relative_path,
                    status=CleanupStatus.DRY_RUN,
                    selected_bytes=item.logical_bytes,
                    message="dry run: nothing was deleted",
                )
            )
        else:
            results.append(_delete_item(item))
    return _finish(plan, started, results)


def cancel(plan: CleanupPlan) -> CleanupResult:
    """Record that the user backed out. Nothing is deleted."""
    with _executed_lock:
        _executed.add(plan.id)  # a cancelled preview cannot be executed afterwards
    items = [
        CleanupItemResult(
            artifact_id=item.artifact_id,
            name=item.name,
            path=item.path,
            relative_path=item.relative_path,
            status=CleanupStatus.CANCELLED,
            selected_bytes=item.logical_bytes,
            message="cancelled: nothing was deleted",
        )
        for item in plan.items
    ]
    return _finish(plan, datetime.now(UTC), items, cancelled=True)
