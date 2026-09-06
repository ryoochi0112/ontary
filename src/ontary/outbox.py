"""The durable effect outbox: the delivery state machine behind declared
effects (spec `durable-effect-outbox`).

Three value types and no behavior that touches a store, a consumer, or an
ontology. `OutboxRecord` is what a `Store` persists and hands back,
`RetryPolicy` is the pure arithmetic deciding when (and whether) an attempt
happens again, and `DrainReport` is what a drain tells its caller. Keeping the
module dependency-free -- like `ontary.effects`, which it sits next to -- is
what lets `store.py`, `actions.py`, and `client.py` all import it without a
cycle.

Why a separate table rather than more audit columns: delivery state CHANGES
(`attempts`, `next_attempt_at`, `lease_until`), and the audit log is
append-only. M5 wrote the `pending` record into the audit log because it was
evidence; this is a work item, which is a different thing that happens to be
about the same event. The two correlate by `invocation_id` (M7a) and
`effect_id`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_RETRY_POLICY",
    "DrainReport",
    "OutboxRecord",
    "OutboxState",
    "RetryPolicy",
]


OutboxState = Literal["pending", "delivered", "failed"]
"""`pending` -- not yet delivered; will be attempted when `next_attempt_at`
arrives. `delivered` -- a dispatcher returned normally and the row was marked
(terminal). `failed` -- `RetryPolicy.max_attempts` attempts were made and every
one raised (terminal, a dead letter kept for reading, never retried again).

There is deliberately no `inflight` state. An attempt in progress is a `pending`
row holding a LEASE (`lease_until` in the future); if the process dies mid-call,
no state has to be repaired -- the lease simply expires and the row becomes
claimable again. A separate `inflight` state would need a reaper to move rows
back out of it, which is the same wall-clock decision with one more way to get
stuck.
"""


class OutboxRecord(BaseModel):
    """One emitted effect as a durable work item.

    Frozen: a record handed to a caller is a value read out of the store, never
    a handle back into the row. State transitions go through the `Store`
    methods, which is the only place `attempts` may increment.
    """

    model_config = ConfigDict(frozen=True)

    effect_id: str
    """Stable id for this delivery, minted at emission and unchanged across
    every attempt -- so it is the key a dispatcher deduplicates on to survive
    the at-least-once redelivery window (spec AC7/AC8). Handed to the
    dispatcher as `EffectMeta.effect_id` and recorded on the audit
    `EffectRecord`."""

    invocation_id: str
    """The `execute()` call that emitted this effect (M7a). Correlates the row
    with its `pending` audit entry and with every later `effects_dispatched`
    entry a drain appends."""

    seq: int
    """0-based emission order within the invocation. Unique with
    `invocation_id`, and the tiebreaker that keeps a claim batch in emission
    order."""

    api_name: str
    payload: dict[str, Any]
    action: str
    actor_id: str
    role: str

    emitted_at: datetime
    """The emitting action's audit timestamp -- the `ts` every attempt's
    `EffectMeta` carries, so a redelivery reports when the effect HAPPENED, not
    when it was retried."""

    state: OutboxState = "pending"
    attempts: int = 0
    """Attempts already made. The inline post-commit attempt is attempt 1 (spec
    AC3), so a freshly enqueued row is 0 and a row that has been tried once and
    is awaiting a retry is 1."""

    next_attempt_at: datetime
    """Earliest time a claim may hand this row out. Equal to `emitted_at` on a
    fresh row (the inline attempt is due immediately); pushed out by
    `RetryPolicy.schedule` after a failure."""

    lease_until: datetime | None = None
    """Exclusive claim, held while an attempt is in flight. `None` (or a past
    time) means claimable. A row is born LEASED by its own invocation so the
    inline attempt cannot race a concurrent drainer."""

    last_error: str | None = None
    updated_at: datetime


class RetryPolicy(BaseModel):
    """When the next attempt happens, and whether there is one at all.

    Deterministic and jitter-free on purpose (spec §4): the tests assert exact
    `next_attempt_at` values and never sleep, and the deployment scale where
    jitter earns its keep is the scale that needs Postgres first.
    """

    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(default=3, ge=1)
    """Total attempts, inline one included. `1` reproduces the pre-outbox
    at-most-once behavior: one shot, then `failed`."""

    initial_backoff: timedelta = Field(default=timedelta(seconds=1), ge=timedelta(0))
    multiplier: float = Field(default=2.0, ge=1.0)
    max_backoff: timedelta = Field(default=timedelta(minutes=5), ge=timedelta(0))
    lease: timedelta = Field(default=timedelta(seconds=60), gt=timedelta(0))
    """How long a claim is exclusive. Too short double-sends a slow dispatcher;
    too long strands a crashed one. A wall-clock lease is not a fence -- see
    spec R3."""

    def backoff_for(self, attempts: int) -> timedelta:
        """Delay before attempt number `attempts + 1`, given `attempts` already
        made. `initial_backoff * multiplier ** (attempts - 1)`, capped at
        `max_backoff`; the cap also protects against an overflow from a large
        `attempts` (`timedelta` raises `OverflowError` well before a realistic
        `max_attempts` could, but `max_attempts` is caller-supplied)."""
        if attempts <= 0:
            return timedelta(0)
        try:
            delay = self.initial_backoff * (self.multiplier ** (attempts - 1))
        except OverflowError:
            return self.max_backoff
        return min(delay, self.max_backoff)

    def schedule(self, *, attempts: int, now: datetime) -> datetime | None:
        """When to try again after `attempts` failed attempts, or `None` when
        the budget is spent and the row is terminally `failed`."""
        if attempts >= self.max_attempts:
            return None
        return now + self.backoff_for(attempts)


DEFAULT_RETRY_POLICY = RetryPolicy()
"""Bound by every runtime/client that does not pass its own. Retries are ON by
default -- an outbox whose retries are opt-in is an outbox nobody turns on --
which is exactly why the declared contract is at-least-once (spec §3)."""


class DrainReport(BaseModel):
    """What one `drain_effects()` call did. `claimed == delivered + retrying +
    failed + skipped` always holds, so a caller can assert on the whole
    accounting rather than on one number."""

    model_config = ConfigDict(frozen=True)

    claimed: int = 0
    delivered: int = 0
    retrying: int = 0
    """Attempt failed, budget remains: the row is pending again with a later
    `next_attempt_at`."""
    failed: int = 0
    """Attempt failed and exhausted `max_attempts`: terminal dead letter."""
    skipped: int = 0
    """Claimed, but this client has no dispatcher bound for the row's effect --
    the claim was released and `attempts` left untouched (spec AC10). A nonzero
    count here is a wiring signal, not a delivery failure."""
