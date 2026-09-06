"""Audit subsystem: models and JSON-safety helpers for the append-only audit
log persisted by `ontary.store.ObjectStore`.

Kept separable from object/link persistence (spec m35-sdk-refactor §6): the
audit log's shape (`AuditEntry`, `WriteRecord`-derived writes) and its
never-raise JSON encoding (`_safe_json_dumps`) are storage-adjacent concerns,
not storage-*implementation* concerns -- a future second `Store` backend
still needs these, but need not reimplement them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


class WriteRecord(BaseModel):
    """One write observed inside `ObjectStore.capture_action_writes` --
    (op, type/id) for a `create`/`update`/`retire`, or
    (link_type, from_id, to_id) for a `link`/`unlink`. Recorded only for
    writes that were allowed (authority checks happen before the write;
    refused writes never append)."""

    op: Literal["create", "update", "retire", "link", "unlink"]
    object_type: str | None = None
    link_type: str | None = None
    object_id: str | None = None
    from_id: str | None = None
    to_id: str | None = None


class CapabilityAccessRecord(BaseModel):
    """How many times an action OBTAINED a declared capability provider.

    Recorded on both the ok and the error path, so an action that reached
    outside and then failed a precondition is not audited as though nothing
    happened -- the transaction rolls back, but the LLM request already went
    out over the wire.

    `count` counts ACCESSES, not outside calls, and the field is named that
    way on purpose. `ActionContext.capability(handle)` returns the provider
    *object*; the call happens afterwards on that object, so one access
    followed by three `provider.structured(...)` calls records `count=1`, and
    an access with no call at all still records `1`. Counting real
    invocations would mean interposing a proxy on every provider method,
    which would defeat the typed passthrough `capability()` exists to give.
    Never carries arguments or results -- only the name and the count.
    """

    api_name: str
    count: int


class AuditEntry(BaseModel):
    """A single append-only audit log entry."""

    # Runtime-bound action/function callers pass their shared clock explicitly;
    # this default preserves the standalone `AuditEntry` behavior for store
    # callers that have no runtime seam.
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # What produced this entry. Actions and functions share one log -- that is
    # the point of an audit log -- but a reader has to be able to tell them
    # apart: nothing stops an ontology declaring an action and a function with
    # the same `api_name`, and a consumer filtering "everything DeleteThing
    # did" must not silently sweep in a read-only function of that name.
    #
    # Defaults to "action" because every entry written before functions were
    # audited was, in fact, an action -- unlike `invocation_id`, backfilling
    # this one asserts something true, so the column is NOT NULL with a
    # constant default.
    kind: Literal["action", "function"] = "action"
    # One id per `execute()` call, stamped on EVERY entry that call writes.
    # Without it, entries from one call are matched by field values and append
    # order, so a caller that re-enters the SAME action with the SAME params
    # produces two indistinguishable rows and a reader cannot tell which
    # attempt was the one that failed.
    #
    # `None` means "not attributable to a single call": entries written by a
    # pre-invocation-id engine (the column is nullable so a v2 file keeps
    # reading), and any future non-action audit source that has no invocation
    # boundary of its own.
    invocation_id: str | None = None
    actor: str
    role: str
    # The identity the TRANSPORT proved (an `AccessToken`'s `subject`,
    # falling back to `client_id`) -- separate from `actor`/`role`, which
    # record who the request RESOLVED to. The two can differ: a resolver bug
    # that maps every token to one privileged actor is invisible in `actor`
    # alone but visible here (spec `multi-consumer-mcp` AC8/AC9).
    #
    # `None` means no transport proved an identity for this call: direct
    # Python use, the single-consumer stdio server (`build_mcp_server`), and
    # -- like `invocation_id` before it -- every entry written before this
    # field existed, since the column is nullable so a pre-M10 file keeps
    # reading.
    principal: str | None = None
    action: str
    target_type: str
    target_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    outcome: str
    # The real (op, type, id) writes the action made (declared-contracts §3
    # AC11), captured via `capture_action_writes` -- empty for
    # denied/error/rolled-back attempts, since nothing was committed.
    writes: list[WriteRecord] = Field(default_factory=list)
    capability_accesses: list[CapabilityAccessRecord] = Field(default_factory=list)


def _placeholder(value: Any) -> dict[str, str]:
    return {"$unserializable": repr(value)}


def _safe_json_dumps(value: Any) -> str:
    """Serialize `value` to JSON, never raising (declared-contracts §3
    AC12): `append_audit` must persist an entry even when a param or write
    value cannot be faithfully JSON-encoded. Per-key placeholders are used
    for a dict's offending values where practical; anything else falls back
    to a single whole-value placeholder."""
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        pass
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            try:
                json.dumps(item)
            except (TypeError, ValueError):
                safe[key] = _placeholder(item)
            else:
                safe[key] = item
        try:
            return json.dumps(safe)
        except (TypeError, ValueError):
            pass
    return json.dumps(_placeholder(value))
