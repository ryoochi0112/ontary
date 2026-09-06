"""Operator-only orchestration for one-object content erasure.

This module deliberately stays off the authoring front door, clients, action
contexts, and MCP.  Like :mod:`ontary.explain`, it is available only through
its canonical module to a trusted operator who already holds a raw store.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ontary.audit import AuditEntry
from ontary.errors import ValidationFailed
from ontary.store import Store

__all__ = ["EraseReport", "erase_object"]

_ERASURE_ACTION = "erase_object"


class EraseReport(BaseModel):
    """Non-sensitive accounting for one operator erasure request."""

    model_config = ConfigDict(frozen=True)

    object_type: str
    object_id: str
    operator: str
    erased_at: datetime
    outcome: Literal["erased", "no_op"]
    code: str | None = None
    object_rows_purged: int = 0
    audit_entries_purged: int = 0
    outbox_rows_purged: int = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _completed_erasure(
    store: Store, object_type: str, object_id: str
) -> AuditEntry | None:
    """Return the durable marker used to timestamp a zero-count no-op report."""
    for entry in reversed(store.audit_entries()):
        if (
            entry.kind == "erasure"
            and entry.action == _ERASURE_ACTION
            and entry.target_type == object_type
            and entry.target_id == object_id
            and entry.outcome == "erased"
        ):
            return entry
    return None


def erase_object(
    store: Store, object_type: str, id: str, *, operator: str
) -> EraseReport:
    """Erase one object's durable content and append operator audit evidence.

    The store verb owns close-if-live and the backend-local purge transaction.
    A request with a prior durable erasure marker is a no-op only when the
    store reports that all three content counts are zero; late-arriving
    durable content is therefore purged on a repeat. A stored object with no
    marker falls through to the audited path, even when the raw store verb
    already purged its content. A re-ingested row with the same id is a new
    real erasure.
    """
    completed = _completed_erasure(store, object_type, id)
    has_rows, _ = store.object_erasure_state(object_type, id)
    if not has_rows:
        raise ValidationFailed(
            f"{object_type} object {id!r} has no stored row to erase",
            code="OBJECT_ERASURE_NOT_FOUND",
        )

    result = store.erase_object_content(object_type, id)
    counts = {
        "object_rows_purged": result.object_rows_purged,
        "audit_entries_purged": result.audit_entries_purged,
        "outbox_rows_purged": result.outbox_rows_purged,
    }
    if completed is not None and all(count == 0 for count in counts.values()):
        return EraseReport(
            object_type=object_type,
            object_id=id,
            operator=operator,
            erased_at=completed.ts,
            outcome="no_op",
            code="OBJECT_ALREADY_ERASED",
        )

    erased_at = _utcnow()
    store.append_audit(
        AuditEntry(
            ts=erased_at,
            kind="erasure",
            actor=operator,
            role="operator",
            principal=None,
            action=_ERASURE_ACTION,
            target_type=object_type,
            target_id=id,
            params=counts,
            outcome="erased",
        )
    )
    return EraseReport(
        object_type=object_type,
        object_id=id,
        operator=operator,
        erased_at=erased_at,
        outcome="erased",
        object_rows_purged=result.object_rows_purged,
        audit_entries_purged=result.audit_entries_purged,
        outbox_rows_purged=result.outbox_rows_purged,
    )
