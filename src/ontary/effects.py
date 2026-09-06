"""Author-facing data and failures for governed outside-world interactions.

Effects are deliberately modeled without store, query, consumer, or action
dependencies. A handler emits a frozen payload as data, and a dispatcher later
receives only that payload plus minimal provenance. Keeping this module at the
edge of the package import graph prevents dispatch code from gaining an
accidental path back into ontology state.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, ConfigDict

__all__ = [
    "EffectDispatcher",
    "EffectMeta",
    "EffectPayload",
]


class EffectPayload(BaseModel):
    """Base for emitted outside-write data.

    Reassignment is forbidden so the declared value cannot be casually changed
    after construction. Nested mutable values still require the runtime's deep
    snapshot at emission time; Pydantic freezing is intentionally shallow.
    """

    model_config = ConfigDict(frozen=True)


class EffectMeta(BaseModel):
    """Minimal dispatch provenance: the engine provides no ontology access.

    A dispatcher may identify which action and actor produced an effect, and the
    engine hands it nothing else -- no store, no `Consumer`, no `ActionContext`.
    The field set is asserted exactly by `tests/test_docs.py`, so widening it to
    provide such a path fails a test rather than passing review.

    This is NOT a sandbox, and an earlier version of this docstring overclaimed
    that it was (corrected 2026-07-26): a dispatcher is author code and can close
    over a store or a client, exactly as the README's own example closes over
    `store` to assert the writes committed. What the narrow field set guarantees
    is that reaching ontology state from a dispatcher is a visible, deliberate act
    by the author, never something the runtime supplies silently -- the same
    distinction `Declarations.writeback` draws about capability providers.
    """

    model_config = ConfigDict(frozen=True)

    action: str
    actor_id: str
    role: str
    ts: datetime
    effect_id: str
    """The outbox row's id: stable across every attempt at this ONE emission,
    and different for a second emission of an identical payload (spec
    `durable-effect-outbox` AC7).

    This is the idempotency key. Delivery is at-least-once -- the process can
    die after the outside call returns and before the row is marked delivered,
    and the next drain will then send it again -- so a dispatcher whose outside
    effect is not naturally idempotent MUST record `effect_id` on the far side
    and ignore a repeat. The runtime cannot do this for it: it has no idea what
    the dispatcher talks to."""

    attempt: int
    """1-based attempt counter. `1` is the inline post-commit attempt; `2+`
    come from `drain_effects()`. Useful for logging and for a dispatcher that
    wants to escalate differently on a retry -- never for deduplication, which
    is `effect_id`'s job."""


EffectDispatcher = Callable[[EffectPayload, EffectMeta], None]
