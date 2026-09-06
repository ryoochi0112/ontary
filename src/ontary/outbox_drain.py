"""Durable effect-outbox delivery: inline attempts and the drain loop.

Moved verbatim from `ontary.actions` (C4 of the staged refactor): this is
the outbox/delivery half that lived in the action module -- a separate
concern from the executor pipeline. NOT in `ontary.outbox`, because
`ontary.store` imports `outbox` (pure value types) and this module needs
the `Store` protocol -- placing it there would create a cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from ontary.audit import EffectRecord
from ontary.effects import EffectDispatcher, EffectMeta, EffectPayload
from ontary.outbox import DrainReport, OutboxRecord, RetryPolicy
from ontary.store import AuditEntry, Store


def _describe_dispatch_failure(exc: Exception) -> str:
    """`str(exc)` for an effect-dispatch failure, but never raising.

    Whole-branch review finding (2026-07-26): the per-effect isolation AC12
    promises was defeated by the recording of the failure itself. `str(exc)` runs
    author code -- an exception whose `__str__` raises (or whose `args` hold an
    object with a raising `__repr__`) propagated straight out of the `except`
    block, so `execute()` raised AFTER the writes and the pending row had already
    committed, the remaining effects were never attempted, and no finalization row
    was written. Every one of those outcomes contradicts a stated guarantee.

    Mirrors `audit._safe_json_dumps`'s never-raise discipline: a value this
    engine cannot render must degrade to a placeholder, never take down the
    surrounding operation.
    """
    try:
        return str(exc)
    except Exception:  # pragma: no cover - exercised via a raising __str__
        try:
            return f"<unrenderable {type(exc).__name__}>"
        except Exception:
            return "<unrenderable exception>"



def _resolve_quietly(
    store: Store,
    effect_id: str,
    *,
    state: str,
    next_attempt_at: datetime,
    error: str | None,
    now: datetime,
) -> None:
    """Write one attempt's outcome to the outbox, swallowing a store failure.

    Best-effort on purpose (spec AC8), and the swallowed case is exactly the
    at-least-once window: if the outside call succeeded and THIS write fails,
    the row stays pending and a later drain sends it again. Raising instead
    would surface a bookkeeping failure as an action failure, after the action
    (and its outside call) already succeeded -- a strictly worse lie than a
    duplicate delivery the contract already permits.
    """
    try:
        store.resolve_effect(
            effect_id,
            state=cast(Any, state),
            next_attempt_at=next_attempt_at,
            error=error,
            now=now,
        )
    except Exception:  # pragma: no cover - exercised via a failing-store double
        pass


def _record_failed_attempt(
    store: Store,
    record: OutboxRecord,
    policy: RetryPolicy,
    *,
    attempt: int,
    now: datetime,
    error: str,
) -> tuple[str, EffectRecord]:
    """Book one failed attempt: schedule the next one, or exhaust the budget.

    `retrying` and `failed` differ only in whether `RetryPolicy.schedule`
    returned a time. Kept in one place so the inline path, the drain path, and
    the payload-rehydration failure cannot each invent their own accounting.
    """
    retry_at = policy.schedule(attempts=attempt, now=now)
    status = "retrying" if retry_at is not None else "failed"
    _resolve_quietly(
        store,
        record.effect_id,
        state="pending" if retry_at is not None else "failed",
        # A terminal row keeps its old due time: there is no next attempt for
        # it to be due for, and inventing a future one would read as scheduled.
        next_attempt_at=retry_at if retry_at is not None else record.next_attempt_at,
        error=error,
        now=now,
    )
    return status, EffectRecord(
        api_name=record.api_name,
        payload=record.payload,
        outcome=cast(Any, status),
        error=error,
        effect_id=record.effect_id,
    )


def _deliver_effect(
    store: Store,
    record: OutboxRecord,
    dispatcher: EffectDispatcher,
    payload: EffectPayload,
    policy: RetryPolicy,
    *,
    attempt: int,
    now: datetime,
) -> tuple[str, EffectRecord]:
    """Make ONE delivery attempt for one outbox row and record its outcome.

    Shared by both delivery paths -- `ActionExecutor.execute`'s inline
    post-commit attempt and `OntologyClient.drain_effects`'s retries -- so the
    retry accounting, the error rendering, and the audit shape cannot drift
    between them. Returns `(status, audit record)` where status is
    `delivered` / `retrying` / `failed`.

    Only `Exception` is caught. A `KeyboardInterrupt` or `SystemExit` from a
    dispatcher still propagates (M5 AC12), and now leaves a leased pending row
    behind rather than a lost effect: once the lease expires, a drain delivers
    it (spec AC13).
    """
    meta = EffectMeta(
        action=record.action,
        actor_id=record.actor_id,
        role=record.role,
        # The EMITTING action's timestamp, not this attempt's: a redelivery
        # reports when the effect happened, and `attempt` says which try it is.
        ts=record.emitted_at,
        effect_id=record.effect_id,
        attempt=attempt,
    )
    try:
        dispatcher(payload, meta)
    except Exception as exc:
        return _record_failed_attempt(
            store,
            record,
            policy,
            attempt=attempt,
            now=now,
            error=_describe_dispatch_failure(exc),
        )
    _resolve_quietly(
        store,
        record.effect_id,
        state="delivered",
        next_attempt_at=record.next_attempt_at,
        error=None,
        now=now,
    )
    return "delivered", EffectRecord(
        api_name=record.api_name,
        payload=record.payload,
        outcome="dispatched",
        effect_id=record.effect_id,
    )


def drain_effect_outbox(
    store: Store,
    dispatchers: Mapping[str, tuple[type[EffectPayload], EffectDispatcher]],
    policy: RetryPolicy,
    *,
    limit: int,
    now: datetime,
) -> DrainReport:
    """Claim due outbox rows, attempt each one, and report what happened.

    The recovery half of the outbox (spec AC4): effects whose inline attempt
    failed, and effects whose process died before it could attempt them at all.
    Called through `OntologyClient.drain_effects` / `OntologyRuntime.
    drain_effects`, which supply the caller's bound dispatchers.

    Three things worth knowing about the shape of this loop:

    - **A row this caller cannot dispatch is released, not failed** (AC10).
      Burning an attempt because a client was wired for a different effect set
      would spend a real delivery budget on a wiring gap.
    - **A payload that will not rehydrate IS a failed attempt.** It means the
      declared payload class no longer accepts data this ontology itself
      emitted -- a real defect, bounded by `max_attempts` and visible as
      `last_error` on the row, rather than a row that is retried forever or one
      that silently disappears.
    - **The audit append is per row and best-effort**, carrying the ORIGINAL
      `invocation_id` so a retry is correlated with the `execute()` that emitted
      it (AC9), not with the drain that delivered it.
    """
    claimed = store.claim_due_effects(limit=limit, now=now, lease=policy.lease)
    delivered = retrying = failed = skipped = 0
    for record in claimed:
        bound = dispatchers.get(record.api_name)
        if bound is None:
            store.release_effect_claim(record.effect_id, now=now)
            skipped += 1
            continue
        payload_cls, dispatcher = bound
        attempt = record.attempts + 1
        try:
            payload = payload_cls.model_validate(record.payload)
        except Exception as exc:
            status, audited = _record_failed_attempt(
                store,
                record,
                policy,
                attempt=attempt,
                now=now,
                error=(
                    f"stored payload does not validate against "
                    f"{payload_cls.__name__}: {_describe_dispatch_failure(exc)}"
                ),
            )
        else:
            status, audited = _deliver_effect(
                store, record, dispatcher, payload, policy, attempt=attempt, now=now
            )
        if status == "delivered":
            delivered += 1
        elif status == "retrying":
            retrying += 1
        else:
            failed += 1
        try:
            store.append_audit(
                AuditEntry(
                    invocation_id=record.invocation_id,
                    actor=record.actor_id,
                    role=record.role,
                    # No `principal=` here, deliberately: the outbox row never
                    # carried one (`OutboxRecord` has no `principal` field --
                    # see spec `multi-consumer-mcp` §4.2), so this redelivery
                    # entry reads `principal = None`, same as `target_type`/
                    # `target_id` below -- the outbox is not a second, drifting
                    # record of the action's identity. The original entry this
                    # one joins to by `invocation_id` is the one that carries
                    # the principal.
                    action=record.action,
                    # The row does not carry the action's target type/id: an
                    # effect payload is the author's own declared data, and
                    # copying ontology identity onto it would make the outbox a
                    # second, drifting record of what the action touched. The
                    # `invocation_id` is the join back to the entry that has it.
                    target_type=record.action,
                    target_id=None,
                    params={},
                    outcome="effects_dispatched",
                    ts=now,
                    effects=[audited],
                )
            )
        except Exception:  # pragma: no cover - append_audit never raises today
            pass
    return DrainReport(
        claimed=len(claimed),
        delivered=delivered,
        retrying=retrying,
        failed=failed,
        skipped=skipped,
    )
