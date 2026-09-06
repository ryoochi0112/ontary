"""M7c: the durable effect outbox -- atomicity, retry, drain, and lease.

Every test here runs against BOTH backends via the `env` fixture. The outbox is
the first place delivery STATE (rather than an append-only record) crosses the
`Store` seam, and M5 already shipped one in-memory/SQLite divergence in the
audit log, so parity is asserted by construction rather than spot-checked.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from conftest import raises_code

from ontary.actions import ActionContext
from ontary.authoring import ActionParams, Ontology, OntologyObject, prop
from ontary.client import OntologyClient
from ontary.effects import EffectMeta, EffectPayload
from ontary.errors import ValidationFailed
from ontary.outbox import DrainReport, OutboxRecord, RetryPolicy
from ontary.security import Consumer
from ontary.store import ObjectStore, Store
from ontary.store.inmemory import InMemoryStore

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


ConsumerFactory = Callable[..., Consumer]


def _label(payload: EffectPayload) -> str:
    return str(payload.model_dump()["label"])


def _effects_ontology() -> tuple[Ontology, dict[str, Any]]:
    """A fresh ontology per call.

    The payload classes are declared INSIDE, not at module level: a payload
    class may describe one declaration on one ontology, so reusing a
    module-level class across two `Ontology` instances is refused at
    declaration time.
    """
    ontology = Ontology("effect-outbox", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Record(OntologyObject):
        id: str = prop(primary_key=True)

    class Notice(EffectPayload):
        label: str

    class Other(EffectPayload):
        label: str

    notice = ontology.effect(Notice)
    other = ontology.effect(Other)

    class EmitParams(ActionParams):
        label: str

    @ontology.action(
        EmitParams,
        target=Record,
        roles=["Operator"],
        effects=[notice],
        api_name="Emit",
    )
    def emit(ctx: ActionContext, params: EmitParams) -> dict[str, str]:
        ctx.insert("Record", {"id": params.label})
        ctx.emit(Notice(label=params.label))
        return {"id": params.label}

    class EmitThenRaiseParams(ActionParams):
        label: str

    @ontology.action(
        EmitThenRaiseParams,
        target=Record,
        roles=["Operator"],
        effects=[notice],
        api_name="EmitThenRaise",
    )
    def emit_then_raise(
        ctx: ActionContext, params: EmitThenRaiseParams
    ) -> dict[str, str]:
        ctx.insert("Record", {"id": params.label})
        ctx.emit(Notice(label=params.label))
        raise RuntimeError("handler failed after emitting")

    class EmitBothParams(ActionParams):
        label: str

    @ontology.action(
        EmitBothParams,
        target=Record,
        roles=["Operator"],
        effects=[notice, other],
        api_name="EmitBoth",
    )
    def emit_both(ctx: ActionContext, params: EmitBothParams) -> dict[str, str]:
        ctx.emit(Notice(label=params.label))
        ctx.emit(Other(label=params.label))
        return {"id": params.label}

    ontology.validate()
    return ontology, {"notice": notice, "other": other}


@dataclass
class Env:
    ontology: Ontology
    handles: dict[str, Any]
    store: Store


@pytest.fixture(params=["sqlite", "in-memory"])
def env(request: pytest.FixtureRequest) -> Env:
    """One ontology bound to one store, for both backends."""
    ontology, handles = _effects_ontology()
    store: Store = (
        ObjectStore(ontology.registry)
        if request.param == "sqlite"
        else InMemoryStore(ontology.registry)
    )
    return Env(ontology, handles, store)


def _client(
    env: Env,
    make_consumer: ConsumerFactory,
    dispatchers: dict[str, Any] | None = None,
    *,
    retry: RetryPolicy | None = None,
) -> tuple[OntologyClient, list[tuple[str, EffectMeta]]]:
    """A client on `env`, plus the log its default dispatchers append to."""
    seen: list[tuple[str, EffectMeta]] = []

    def record(payload: EffectPayload, meta: EffectMeta) -> None:
        seen.append((_label(payload), meta))

    bound: dict[Any, Any] = {
        env.handles["notice"]: record,
        env.handles["other"]: record,
    }
    for name, fn in (dispatchers or {}).items():
        bound[env.handles[name]] = fn
    client = OntologyClient(
        env.ontology,
        env.store,
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
        effects=bound,
        **({"effect_retry": retry} if retry is not None else {}),
    )
    return client, seen


def _rows(env: Env) -> list[OutboxRecord]:
    return env.store.outbox_entries()


def _raise_once() -> Any:
    state = [True]

    def dispatch(_payload: EffectPayload, _meta: EffectMeta) -> None:
        if state:
            state.pop()
            raise RuntimeError("first attempt fails")

    return dispatch


def _raise_always() -> Any:
    def dispatch(_payload: EffectPayload, _meta: EffectMeta) -> None:
        raise RuntimeError("always fails")

    return dispatch


# -- AC1: the row commits with the writes, or not at all ----------------------


def test_emitted_effect_becomes_a_durable_outbox_row(
    env: Env, make_consumer: ConsumerFactory
) -> None:
    client, seen = _client(env, make_consumer)

    client.execute("Emit", {"label": "one"})

    rows = _rows(env)
    assert len(rows) == 1
    row = rows[0]
    assert (row.api_name, row.payload, row.state) == (
        "Notice",
        {"label": "one"},
        "delivered",
    )
    assert (row.action, row.actor_id, row.role) == ("Emit", "operator-1", "Operator")
    assert row.attempts == 1
    assert row.lease_until is None  # released when the attempt resolved
    assert row.last_error is None
    # The dispatcher saw the row's own id, and knows this was the first try.
    assert [(label, meta.effect_id, meta.attempt) for label, meta in seen] == [
        ("one", row.effect_id, 1)
    ]


def test_handler_that_raises_after_emitting_queues_nothing(
    env: Env, make_consumer: ConsumerFactory
) -> None:
    """AC1, the cheap half: the handler raises before the engine ever reaches
    the enqueue, so no row is written in the first place. A surviving row would
    be worse than the old lost dispatch -- a drain would later deliver an effect
    for an action that never happened."""
    client, seen = _client(env, make_consumer)

    with pytest.raises(RuntimeError, match="handler failed"):
        client.execute("EmitThenRaise", {"label": "rolled-back"})

    assert _rows(env) == []
    assert seen == []
    assert client.get("Record", "rolled-back") is None


def test_rows_enqueued_before_a_failed_commit_are_rolled_back(
    env: Env, make_consumer: ConsumerFactory
) -> None:
    """AC1, the half that actually needs the transaction.

    Written after a mutation check embarrassed the test above: reverting the
    in-memory backend's outbox rollback left it green, because a handler that
    raises never reaches the enqueue at all. Here the rows ARE written and the
    transaction fails afterwards -- the only shape in which "the outbox row
    commits with the writes, or not at all" is a claim about rollback rather
    than about ordering.
    """
    client, seen = _client(env, make_consumer)
    original = env.store.append_audit

    def fail_the_ok_entry(entry: Any) -> None:
        if entry.outcome == "ok":
            raise RuntimeError("audit store unavailable")
        original(entry)

    # append_audit runs INSIDE the action transaction, right after the enqueue.
    env.store.append_audit = fail_the_ok_entry
    try:
        with pytest.raises(RuntimeError, match="audit store unavailable"):
            client.execute("Emit", {"label": "one"})
    finally:
        env.store.append_audit = original

    assert _rows(env) == []
    assert seen == []
    assert client.get("Record", "one") is None


def test_unserializable_payload_refuses_inside_the_transaction(
    make_consumer: ConsumerFactory,
) -> None:
    """A payload the outbox cannot persist rolls the action back rather than
    being dispatched with a placeholder standing in for the author's data."""
    ontology = Ontology("unserializable", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Doc(OntologyObject):
        id: str = prop(primary_key=True)

    class Opaque:
        pass

    class Weird(EffectPayload):
        model_config = {"arbitrary_types_allowed": True, "frozen": True}
        blob: Any

    weird = ontology.effect(Weird)

    class P(ActionParams):
        pass

    @ontology.action(P, target=Doc, roles=["Operator"], effects=[weird], api_name="Bad")
    def bad(ctx: ActionContext, _params: P) -> dict[str, str]:
        ctx.insert("Doc", {"id": "doc-1"})
        ctx.emit(Weird(blob=Opaque()))
        return {"id": "doc-1"}

    ontology.validate()
    sent: list[EffectPayload] = []
    store = ObjectStore(ontology.registry)
    client = OntologyClient(
        ontology,
        store,
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
        effects={weird: lambda payload, _meta: sent.append(payload)},
    )

    with raises_code(ValidationFailed, "EFFECT_NOT_SERIALIZABLE") as exc_info:
        client.execute("Bad", {})

    assert "Weird" in str(exc_info.value)
    assert store.outbox_entries() == []
    assert sent == []
    assert store.read_current("Doc", "doc-1") is None


# -- AC3/AC6: the inline attempt is attempt 1 of a bounded policy -------------


def test_failed_inline_attempt_leaves_a_retryable_row(
    env: Env, make_consumer: ConsumerFactory
) -> None:
    client, _ = _client(env, make_consumer, {"notice": _raise_always()})

    before = datetime.now(timezone.utc)
    client.execute("Emit", {"label": "one"})

    row = _rows(env)[0]
    assert (row.state, row.attempts) == ("pending", 1)
    assert row.last_error == "always fails"
    # Default policy: a 1s initial backoff, so the next attempt is scheduled a
    # second out rather than being immediately due.
    assert row.next_attempt_at >= before + timedelta(seconds=1)
    assert row.lease_until is None


def test_retry_budget_is_bounded_and_ends_in_a_dead_letter(
    env: Env, make_consumer: ConsumerFactory
) -> None:
    attempts: list[int] = []

    def boom(_payload: EffectPayload, meta: EffectMeta) -> None:
        attempts.append(meta.attempt)
        raise RuntimeError("still down")

    client, _ = _client(
        env, make_consumer, {"notice": boom}, retry=RetryPolicy(max_attempts=3)
    )

    client.execute("Emit", {"label": "one"})
    # Time advances between drains: each failure schedules the next attempt a
    # backoff into the future, so a drain at the SAME instant would correctly
    # find nothing due -- which is itself the backoff working.
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    first = client.drain_effects(now=later)
    second = client.drain_effects(now=later + timedelta(minutes=1))
    third = client.drain_effects(now=later + timedelta(minutes=2))

    assert attempts == [1, 2, 3]  # the inline attempt plus two drains
    assert (first.retrying, second.failed) == (1, 1)
    # The third drain finds nothing: a `failed` row is terminal, never reclaimed.
    assert third == DrainReport(claimed=0)
    row = _rows(env)[0]
    assert (row.state, row.attempts) == ("failed", 3)
    assert row.last_error == "still down"


def test_max_attempts_one_reproduces_at_most_once(
    env: Env, make_consumer: ConsumerFactory
) -> None:
    """The escape hatch: a caller who cannot make their dispatcher idempotent
    configures one attempt and gets exactly the pre-outbox behavior."""
    client, _ = _client(
        env,
        make_consumer,
        {"notice": _raise_always()},
        retry=RetryPolicy(max_attempts=1),
    )

    client.execute("Emit", {"label": "one"})

    row = _rows(env)[0]
    assert (row.state, row.attempts) == ("failed", 1)
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert client.drain_effects(now=later) == DrainReport(claimed=0)


def test_backoff_is_exponential_deterministic_and_capped() -> None:
    policy = RetryPolicy(
        max_attempts=6,
        initial_backoff=timedelta(seconds=2),
        multiplier=3.0,
        max_backoff=timedelta(seconds=30),
    )

    assert policy.backoff_for(0) == timedelta(0)
    assert policy.backoff_for(1) == timedelta(seconds=2)
    assert policy.backoff_for(2) == timedelta(seconds=6)
    assert policy.backoff_for(3) == timedelta(seconds=18)
    assert policy.backoff_for(4) == timedelta(seconds=30)  # capped, not 54
    assert policy.backoff_for(99) == timedelta(seconds=30)  # and never overflows

    assert policy.schedule(attempts=1, now=NOW) == NOW + timedelta(seconds=2)
    # Budget spent -- `None` means terminal, not "retry immediately".
    assert policy.schedule(attempts=6, now=NOW) is None


# -- AC4/AC13: crash recovery -------------------------------------------------


def test_drain_delivers_an_effect_whose_process_died_before_dispatching(
    env: Env, make_consumer: ConsumerFactory
) -> None:
    """The hole the outbox exists to close. `KeyboardInterrupt` stands in for a
    process dying between COMMIT and the outside call: pre-outbox that effect
    was simply gone. Now the lease expires and a later drain sends it."""

    def interrupted(_payload: EffectPayload, _meta: EffectMeta) -> None:
        raise KeyboardInterrupt

    client, seen = _client(env, make_consumer, {"notice": interrupted})

    with pytest.raises(KeyboardInterrupt):
        client.execute("Emit", {"label": "one"})

    row = _rows(env)[0]
    assert (row.state, row.attempts) == ("pending", 0)  # never even attempted
    assert row.lease_until is not None  # still held by the dead invocation
    assert client.get("Record", "one") is not None  # the writes committed

    # A drain BEFORE the lease expires must not touch it.
    assert client.drain_effects(now=row.emitted_at) == DrainReport(claimed=0)

    working, delivered = _client(env, make_consumer)
    report = working.drain_effects(now=row.emitted_at + timedelta(minutes=5))

    assert report == DrainReport(claimed=1, delivered=1)
    assert [label for label, _meta in delivered] == ["one"]
    assert seen == []
    assert _rows(env)[0].state == "delivered"


def test_drain_reuses_the_same_effect_id_and_bumps_the_attempt(
    env: Env, make_consumer: ConsumerFactory
) -> None:
    calls: list[EffectMeta] = []
    failures = [True]

    def flaky(_payload: EffectPayload, meta: EffectMeta) -> None:
        calls.append(meta)
        if failures:
            failures.pop()
            raise RuntimeError("first attempt fails")

    client, _ = _client(env, make_consumer, {"notice": flaky})

    client.execute("Emit", {"label": "one"})
    client.drain_effects(now=datetime.now(timezone.utc) + timedelta(minutes=1))

    assert [meta.attempt for meta in calls] == [1, 2]
    # ONE emission, so ONE idempotency key -- what a dispatcher deduplicates on
    # to survive the at-least-once redelivery window.
    assert len({meta.effect_id for meta in calls}) == 1
    assert calls[0].ts == calls[1].ts  # the emission time, not the retry time


def test_drain_audits_the_retry_against_the_original_invocation(
    env: Env, make_consumer: ConsumerFactory
) -> None:
    """AC9: the correlation M7a's invocation id was built for."""
    client, _ = _client(env, make_consumer, {"notice": _raise_once()})
    client.execute("Emit", {"label": "one"})
    client.drain_effects(now=datetime.now(timezone.utc) + timedelta(minutes=1))

    entries = env.store.audit_entries()
    assert [entry.outcome for entry in entries] == [
        "ok",
        "effects_dispatched",
        "effects_dispatched",
    ]
    invocation_ids = {entry.invocation_id for entry in entries}
    assert len(invocation_ids) == 1 and None not in invocation_ids
    assert [entry.effects[0].outcome for entry in entries] == [
        "pending",
        "retrying",
        "dispatched",
    ]
    assert {entry.effects[0].effect_id for entry in entries} == {
        _rows(env)[0].effect_id
    }


# -- AC5: leases --------------------------------------------------------------


def test_a_claim_is_exclusive_until_its_lease_expires(
    env: Env, make_consumer: ConsumerFactory
) -> None:
    client, _ = _client(env, make_consumer, {"notice": _raise_always()})
    client.execute("Emit", {"label": "one"})
    due = datetime.now(timezone.utc) + timedelta(minutes=1)

    first = env.store.claim_due_effects(limit=10, now=due, lease=timedelta(seconds=60))
    assert len(first) == 1
    assert first[0].lease_until == due + timedelta(seconds=60)

    # A second drainer at the same instant sees nothing claimable.
    assert (
        env.store.claim_due_effects(limit=10, now=due, lease=timedelta(seconds=60)) == []
    )

    # Once the lease lapses the row is reclaimable -- which is precisely why the
    # guarantee is at-least-once and not exactly-once.
    later = due + timedelta(seconds=61)
    reclaimed = env.store.claim_due_effects(
        limit=10, now=later, lease=timedelta(seconds=60)
    )
    assert len(reclaimed) == 1


def test_claims_come_back_in_emission_order(
    env: Env, make_consumer: ConsumerFactory
) -> None:
    client, _ = _client(
        env,
        make_consumer,
        {"notice": _raise_always(), "other": _raise_always()},
    )
    client.execute("EmitBoth", {"label": "one"})

    due = datetime.now(timezone.utc) + timedelta(minutes=1)
    claimed = env.store.claim_due_effects(
        limit=10, now=due, lease=timedelta(seconds=60)
    )

    assert [(row.api_name, row.seq) for row in claimed] == [("Notice", 0), ("Other", 1)]


def test_claim_refuses_a_nonpositive_limit(env: Env) -> None:
    with pytest.raises(Exception) as exc_info:
        env.store.claim_due_effects(limit=0, now=NOW, lease=timedelta(seconds=60))
    assert getattr(exc_info.value, "code", None) == "INVALID_BATCH"


# -- AC10: a client that cannot dispatch a row releases it --------------------


def test_drain_skips_a_row_this_client_has_no_dispatcher_for(
    env: Env, make_consumer: ConsumerFactory
) -> None:
    client, _ = _client(env, make_consumer, {"notice": _raise_always()})
    client.execute("Emit", {"label": "one"})

    # A second client bound only for the OTHER effect: it must not consume the
    # Notice row's retry budget just because it happened to run a drain.
    narrow = OntologyClient(
        env.ontology,
        env.store,
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
        effects={env.handles["other"]: lambda _p, _m: None},
    )
    report = narrow.drain_effects(now=datetime.now(timezone.utc) + timedelta(minutes=1))

    assert report == DrainReport(claimed=1, skipped=1)
    row = _rows(env)[0]
    assert (row.state, row.attempts) == ("pending", 1)  # 1 from the inline try
    assert row.lease_until is None  # released, immediately claimable again


def test_drain_report_accounts_for_every_claimed_row(
    env: Env, make_consumer: ConsumerFactory
) -> None:
    def by_label(payload: EffectPayload, _meta: EffectMeta) -> None:
        if _label(payload) == "bad":
            raise RuntimeError("nope")

    client, _ = _client(
        env, make_consumer, {"notice": by_label}, retry=RetryPolicy(max_attempts=2)
    )
    client.execute("Emit", {"label": "bad"})
    client.execute("Emit", {"label": "good"})

    report = client.drain_effects(now=datetime.now(timezone.utc) + timedelta(minutes=1))

    # "good" delivered inline and is not reclaimed; "bad" is on its last attempt.
    assert report == DrainReport(claimed=1, failed=1)
    assert report.claimed == (
        report.delivered + report.retrying + report.failed + report.skipped
    )


# -- store-level parity -------------------------------------------------------


def test_outbox_entries_hands_out_copies(
    env: Env, make_consumer: ConsumerFactory
) -> None:
    client, _ = _client(env, make_consumer)
    client.execute("Emit", {"label": "one"})

    first = _rows(env)[0]
    first.payload["label"] = "tampered"

    assert _rows(env)[0].payload == {"label": "one"}


def test_duplicate_effect_id_is_refused(env: Env) -> None:
    row = OutboxRecord(
        effect_id="fixed",
        invocation_id="inv-1",
        seq=0,
        api_name="Notice",
        payload={"label": "one"},
        action="Emit",
        actor_id="operator-1",
        role="Operator",
        emitted_at=NOW,
        next_attempt_at=NOW,
        updated_at=NOW,
    )
    env.store.enqueue_effects([row])

    with pytest.raises(Exception):
        env.store.enqueue_effects([row])
    assert len(_rows(env)) == 1


def test_both_backends_record_the_invocation_id_on_audit_entries(
    make_consumer: ConsumerFactory,
) -> None:
    """Spec §5: `InMemoryStore.append_audit` dropped `invocation_id` entirely
    (M7a added the field to `AuditEntry` and to `ObjectStore`, never to this
    backend's field-by-field normalization). The outbox correlates a retry's
    audit row to the original invocation BY that value, so the parity break is
    load-bearing here, not cosmetic."""
    for build in (ObjectStore, InMemoryStore):
        ontology, handles = _effects_ontology()
        store: Store = build(ontology.registry)
        client = OntologyClient(
            ontology,
            store,
            make_consumer(
                actor_id="operator-1",
                role="Operator",
                scope_level="org",
                scope_id="org-1",
                kind="human",
            ),
            effects={handles["notice"]: lambda _p, _m: None},
        )
        client.execute("Emit", {"label": "one"})

        ids = [entry.invocation_id for entry in store.audit_entries()]
        assert len(ids) == 2  # ok + effects_dispatched
        assert len(set(ids)) == 1
        assert ids[0] is not None
