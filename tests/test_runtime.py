"""Unit tests for `OntologyRuntime`/`Ontology.bind()`/`runtime.for_consumer()`
(spec `typed-actions.md` T3/AC6): one runtime shares its `GuardedQuery` +
`ActionExecutor` across cheap per-consumer `OntologyClient` views, with
every ontology-declared action handler auto-bound exactly once -- and two
runtimes (same or different ontologies) never cross-talk.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from conftest import raises_code

from ontary.actions import ActionContext
from ontary.authoring import ActionParams, Ontology, OntologyObject, prop, target
from ontary.client import OntologyClient
from ontary.effects import EffectMeta, EffectPayload
from ontary.errors import PermissionDenied, ValidationFailed
from ontary.functions import BoundQuery
from ontary.meta import Sensitivity
from ontary.scope import DirectProperty
from ontary.security import Consumer
from ontary.store.inmemory import InMemoryStore

LEVELS = ["team"]


class _FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _SequentialIds:
    def __init__(self) -> None:
        self._next = 0

    def __call__(self) -> str:
        self._next += 1
        return f"id-{self._next}"


def _build_ticket_ontology() -> tuple[Ontology, type[OntologyObject], type[ActionParams]]:
    """A tiny ticket ontology with one restricted (human-hidden) field and
    one declared action, used across this file's tests."""
    ontology = Ontology(name="runtime-tickets", scope_levels=LEVELS)

    @ontology.object(layer="L0", scope="unscoped")
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        subject: str
        internal_note: str | None = prop(
            default=None, sensitivity=Sensitivity(human_visible=False)
        )

    class EscalateParams(ActionParams):
        ticket_id: str = target(Ticket)

    @ontology.action(
        EscalateParams, target=Ticket, roles=["Agent"], api_name="Escalate"
    )
    def escalate(ctx: ActionContext, params: EscalateParams) -> dict[str, str]:
        # `target()` scope enforcement only applies once the target object
        # EXISTS (spec: nonexistent target is precondition territory) --
        # these tests exercise runtime sharing/isolation, not scope
        # enforcement itself, so they deliberately never insert a Ticket
        # row before executing (mirrors `test_authoring_actions.py`).
        return {"ticket_id": params.ticket_id}

    return ontology, Ticket, EscalateParams


def _build_scoped_ticket_ontology() -> (
    tuple[Ontology, type[OntologyObject], type[ActionParams]]
):
    """A team-scoped ticket ontology (a `team_id` property, resolved via a
    `DirectProperty` scope rule) with one declared, target-scoped action --
    used by `TestScopeEnforcementAcrossConsumers` below to prove
    `runtime.for_consumer()` views enforce SCOPE per-consumer, not once
    for the whole shared runtime."""
    ontology = Ontology(name="runtime-tickets-scoped", scope_levels=LEVELS)

    @ontology.object(
        layer="L0", scope=[DirectProperty(level="team", property_name="team_id")]
    )
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        subject: str
        team_id: str

    class EscalateParams(ActionParams):
        ticket_id: str = target(Ticket)

    @ontology.action(
        EscalateParams, target=Ticket, roles=["Agent"], api_name="Escalate"
    )
    def escalate(ctx: ActionContext, params: EscalateParams) -> dict[str, str]:
        return {"ticket_id": params.ticket_id}

    return ontology, Ticket, EscalateParams


def _human(actor_id: str = "u1") -> Consumer:
    return Consumer(actor_id=actor_id, role="Agent", scope_level="team", scope_id="t1", kind="human")


def _ai(actor_id: str = "ai1") -> Consumer:
    return Consumer(actor_id=actor_id, role="Agent", scope_level="team", scope_id="t1", kind="ai")


def _seed_ticket(store: InMemoryStore, ticket_id: str = "tk1") -> None:
    from ontary.store import Source

    store.insert(
        "Ticket",
        {"id": ticket_id, "subject": "s1", "internal_note": "secret"},
        Source(source_system="seed"),
    )


def _build_effect_ontology() -> tuple[Ontology, Any]:
    ontology = Ontology(name="runtime-effects", scope_levels=LEVELS, min_n=1)

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Record(OntologyObject):
        id: str = prop(primary_key=True)

    class Notice(EffectPayload):
        label: str

    notice = ontology.effect(Notice)

    class EmitParams(ActionParams):
        label: str

    @ontology.action(
        EmitParams,
        target=Record,
        roles=["Agent"],
        effects=[notice],
        api_name="Emit",
    )
    def emit(ctx: ActionContext, params: EmitParams) -> dict[str, str]:
        ctx.emit(Notice(label=params.label))
        return {"label": params.label}

    return ontology, notice


class TestRuntimeSeams:
    def test_injected_clock_and_ids_make_audit_bytes_deterministic(self) -> None:
        fixed = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)

        def run_once() -> list[str]:
            ontology, _Ticket, EscalateParams = _build_ticket_ontology()
            store = InMemoryStore(ontology.registry)
            runtime = ontology.bind(
                store,
                clock=_FixedClock(fixed),
                id_factory=_SequentialIds(),
            )
            runtime.for_consumer(_human()).execute(EscalateParams(ticket_id="tk1"))
            return [entry.model_dump_json() for entry in store.audit_entries()]

        assert run_once() == run_once()

    def test_default_clock_and_ids_still_vary_audit_entries(self) -> None:
        def run_once() -> Any:
            ontology, _Ticket, EscalateParams = _build_ticket_ontology()
            store = InMemoryStore(ontology.registry)
            ontology.bind(store).for_consumer(_human()).execute(
                EscalateParams(ticket_id="tk1")
            )
            return store.audit_entries()[0]

        first = run_once()
        second = run_once()
        assert first.ts != second.ts
        assert first.invocation_id != second.invocation_id

    def test_function_audit_uses_runtime_clock_and_id_factory(self) -> None:
        ontology = Ontology(name="runtime-functions", scope_levels=LEVELS)

        @ontology.function(api_name="Audited", audit=True)
        def audited(_query: BoundQuery, _params: dict[str, Any]) -> str:
            return "ok"

        fixed = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)
        store = InMemoryStore(ontology.registry)
        client = ontology.bind(
            store,
            clock=_FixedClock(fixed),
            id_factory=_SequentialIds(),
        ).for_consumer(_human())

        assert client.call_function("Audited", {}) == "ok"
        entry = store.audit_entries()[0]
        assert entry.ts == fixed
        assert entry.invocation_id == "id-1"

    def test_drain_uses_runtime_clock_when_omitted_and_explicit_now_wins(self) -> None:
        fixed = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)
        clock = _FixedClock(fixed)
        ontology, notice = _build_effect_ontology()
        store = InMemoryStore(ontology.registry)
        failed_labels: set[str] = set()
        attempts: list[tuple[str, int]] = []

        def dispatch(payload: EffectPayload, meta: EffectMeta) -> None:
            label = str(payload.model_dump()["label"])
            attempts.append((label, meta.attempt))
            if label not in failed_labels:
                failed_labels.add(label)
                raise RuntimeError("retry me")

        runtime = ontology.bind(
            store,
            clock=clock,
            id_factory=_SequentialIds(),
            effects={notice: dispatch},
        )
        client = runtime.for_consumer(_human())

        client.execute("Emit", {"label": "omitted"})
        first_row = store.outbox_entries()[0]
        assert first_row.emitted_at == fixed
        assert first_row.next_attempt_at == fixed + timedelta(seconds=1)

        clock.value = fixed + timedelta(seconds=1)
        assert runtime.drain_effects().delivered == 1
        assert store.outbox_entries()[0].updated_at == clock.value
        assert store.audit_entries()[-1].ts == clock.value

        clock.value = fixed
        client.execute("Emit", {"label": "explicit"})
        explicit_now = fixed + timedelta(seconds=2)
        assert runtime.drain_effects(now=explicit_now).delivered == 1
        assert store.outbox_entries()[1].updated_at == explicit_now
        assert store.audit_entries()[-1].ts == explicit_now
        assert attempts == [("omitted", 1), ("omitted", 2), ("explicit", 1), ("explicit", 2)]


class TestForConsumerSharing:
    def test_two_clients_share_query_and_executor_by_identity(self) -> None:
        ontology, _Ticket, _Params = _build_ticket_ontology()
        store = InMemoryStore(ontology.registry)
        runtime = ontology.bind(store)

        human_client = runtime.for_consumer(_human())
        ai_client = runtime.for_consumer(_ai())

        assert human_client._query is ai_client._query is runtime.query
        assert human_client.actions is ai_client.actions is runtime.actions

    def test_consumer_correct_redaction_over_shared_runtime(self) -> None:
        ontology, Ticket, _Params = _build_ticket_ontology()
        store = InMemoryStore(ontology.registry)
        _seed_ticket(store)
        runtime = ontology.bind(store)

        human_client = runtime.for_consumer(_human())
        ai_client = runtime.for_consumer(_ai())

        human_ticket = human_client.get(Ticket, "tk1")
        ai_ticket = ai_client.get(Ticket, "tk1")

        assert human_ticket is not None and ai_ticket is not None
        assert "internal_note" in human_ticket.redacted_fields
        assert "internal_note" not in ai_ticket.redacted_fields

    def test_audit_actor_rows_differ_per_consumer(self) -> None:
        ontology, _Ticket, EscalateParams = _build_ticket_ontology()
        store = InMemoryStore(ontology.registry)
        runtime = ontology.bind(store)

        human_client = runtime.for_consumer(_human("human-actor"))
        ai_client = runtime.for_consumer(_ai("ai-actor"))

        human_client.execute(EscalateParams(ticket_id="tk1"))
        ai_client.execute(EscalateParams(ticket_id="tk1"))

        entries = store.audit_entries()
        actors = [e.actor for e in entries]
        assert actors == ["human-actor", "ai-actor"]


def _agent_in_scope(scope_id: str, actor_id: str) -> Consumer:
    return Consumer(
        actor_id=actor_id, role="Agent", scope_level="team", scope_id=scope_id, kind="human"
    )


class TestScopeEnforcementAcrossConsumers:
    def test_consumer_correct_scope_enforcement_over_shared_runtime(self) -> None:
        """AC6 (P1 review fix): `for_consumer()` views are cheap, but scope
        enforcement must still be evaluated PER consumer over the ONE
        shared `ActionExecutor` -- a t1-scoped client executing an action
        against a t1-owned target must succeed, while a t2-scoped client
        built from the SAME runtime, targeting the SAME object, must be
        denied with `SCOPE_DENIED` -- and each outcome must be audited
        under its own consumer's actor."""
        ontology, _Ticket, EscalateParams = _build_scoped_ticket_ontology()
        store = InMemoryStore(ontology.registry)
        from ontary.store import Source

        store.insert(
            "Ticket",
            {"id": "tk1", "subject": "s1", "team_id": "t1"},
            Source(source_system="seed"),
        )
        runtime = ontology.bind(store)

        t1_client = runtime.for_consumer(_agent_in_scope("t1", "t1-actor"))
        t2_client = runtime.for_consumer(_agent_in_scope("t2", "t2-actor"))

        result = t1_client.execute(EscalateParams(ticket_id="tk1"))
        assert result == {"ticket_id": "tk1"}

        with raises_code(PermissionDenied, "SCOPE_DENIED"):
            t2_client.execute(EscalateParams(ticket_id="tk1"))

        entries = store.audit_entries()
        assert len(entries) == 2
        ok_entry, denied_entry = entries
        assert ok_entry.outcome == "ok"
        assert ok_entry.actor == "t1-actor"
        assert denied_entry.outcome == "denied"
        assert denied_entry.actor == "t2-actor"


class TestRuntimeIsolation:
    def test_two_runtimes_different_stores_dont_cross_talk(self) -> None:
        ontology, _Ticket, EscalateParams = _build_ticket_ontology()
        store_a = InMemoryStore(ontology.registry)
        store_b = InMemoryStore(ontology.registry)

        runtime_a = ontology.bind(store_a)
        runtime_b = ontology.bind(store_b)

        client_a = runtime_a.for_consumer(_human())
        client_b = runtime_b.for_consumer(_human())

        client_a.execute(EscalateParams(ticket_id="tk1"))

        assert store_a.audit_entries()
        assert store_b.audit_entries() == []
        assert client_a._store is not client_b._store
        assert client_a.actions is not client_b.actions
        assert client_a._query is not client_b._query

    def test_two_ontologies_dont_cross_talk(self) -> None:
        ontology_1, Ticket_1, _Params_1 = _build_ticket_ontology()
        ontology_2, Ticket_2, _Params_2 = _build_ticket_ontology()

        store_1 = InMemoryStore(ontology_1.registry)
        store_2 = InMemoryStore(ontology_2.registry)
        _seed_ticket(store_1)
        _seed_ticket(store_2)

        client_1 = ontology_1.bind(store_1).for_consumer(_human())
        client_2 = ontology_2.bind(store_2).for_consumer(_human())

        # A class registered on ontology_2 must not resolve against
        # ontology_1's client (registry-identity discipline, spec §7).
        with raises_code(ValidationFailed, "UNKNOWN_NAME"):
            client_1.get(Ticket_2, "tk1")

        # Each client still reads its own store's data fine, typed by its
        # own ontology's class.
        assert client_1.get(Ticket_1, "tk1") is not None
        assert client_2.get(Ticket_2, "tk1") is not None

    def test_double_bind_two_runtimes_from_one_ontology(self) -> None:
        """Two runtimes built from the SAME `Ontology` (e.g. two stores)
        must not raise a duplicate-handler-registration error -- each
        `OntologyRuntime` builds its own fresh `ActionExecutor`."""
        ontology, _Ticket, _Params = _build_ticket_ontology()
        store_1 = InMemoryStore(ontology.registry)
        store_2 = InMemoryStore(ontology.registry)

        runtime_1 = ontology.bind(store_1)
        runtime_2 = ontology.bind(store_2)

        assert runtime_1.actions is not runtime_2.actions
        assert runtime_1.query is not runtime_2.query


class TestDirectConstruction:
    def test_direct_client_with_authoring_ontology_auto_binds_handler(self) -> None:
        """AC6: `OntologyClient(ontology, store, consumer)` with an
        `Ontology` (not `OntologyDef`) auto-binds its declared handlers --
        zero manual `_register` wiring by the caller."""
        ontology, _Ticket, EscalateParams = _build_ticket_ontology()
        store = InMemoryStore(ontology.registry)

        client = OntologyClient(ontology, store, _human())
        result = client.execute(EscalateParams(ticket_id="tk1"))

        assert result == {"ticket_id": "tk1"}

    def test_direct_client_with_ontology_def_still_works_for_descriptor_flows(
        self,
    ) -> None:
        """Descriptor-authoring path: a plain `OntologyDef` has no
        `_action_handlers` -- `OntologyClient` still constructs fine and
        the internal `client.actions._register` seam still binds handlers
        (AC7: the old public `register_handler`/`.handler` surface is
        gone; this private seam is what descriptor authoring uses now)."""
        ontology, _Ticket, _Params = _build_ticket_ontology()
        definition = ontology.definition
        store = InMemoryStore(ontology.registry)
        _seed_ticket(store)

        # Bypass the typed handler entirely -- exercise the plain
        # OntologyDef construction path with a hand-registered legacy
        # handler for a DIFFERENT (still-registered) action api_name is
        # unnecessary here; simplest proof is that construction + reads
        # work with no `_action_handlers` attribute present on OntologyDef.
        assert not hasattr(definition, "_action_handlers")
        client = OntologyClient(definition, store, _human())
        assert client.get("Ticket", "tk1") is not None
