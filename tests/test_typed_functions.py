"""Unit tests for `@ontology.function` + typed `BoundQuery` overloads (spec
`typed-actions.md` §6/AC8, T4).

Builds a small "tickets"-shaped class-authored ontology with a handful of
functions declared directly on the `Ontology` (zero manual
`register_function`/`FunctionRegistry` wiring) and exercises them both via
`Ontology.bind(store).for_consumer(consumer)` and via direct
`OntologyClient(ontology, store, consumer)` construction -- both are
documented as reaching the identical auto-bound handler (spec AC6/AC8).
"""

from __future__ import annotations

from typing import Any

from conftest import raises_code

from ontary.authoring import LinkHandle, Ontology, OntologyObject, prop
from ontary.client import OntologyClient
from ontary.errors import ValidationFailed
from ontary.functions import BoundQuery
from ontary.meta import Cardinality, Sensitivity
from ontary.security import Consumer
from ontary.store import Source
from ontary.store.inmemory import InMemoryStore

SRC = Source(source_system="test")
LEVELS = ["team"]


def _build_ontology() -> tuple[
    Ontology, type[OntologyObject], type[OntologyObject], LinkHandle[Any, Any]
]:
    ontology = Ontology(name="t4", scope_levels=LEVELS)

    @ontology.object(layer="L0", scope="unscoped")
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        subject: str
        internal_note: str | None = prop(
            default=None, sensitivity=Sensitivity(human_visible=False)
        )

    @ontology.object(layer="L0", scope="unscoped")
    class Team(OntologyObject):
        id: str = prop(primary_key=True)
        name: str

    owns = ontology.link("owns", Team, Ticket, Cardinality.ONE_TO_MANY)

    @ontology.function(
        description="Counts tickets.",
        input_description="none",
        output_description="int",
    )
    def ticketCount(query: BoundQuery, params: dict[str, Any]) -> int:
        return len(query.list(Ticket))

    @ontology.function(api_name="firstTicketSubject")
    def _first_subject(query: BoundQuery, params: dict[str, Any]) -> str | None:
        tickets = query.list(Ticket)
        return tickets[0].subject if tickets else None

    @ontology.function(api_name="internalNoteRedacted")
    def _internal_note_redacted(query: BoundQuery, params: dict[str, Any]) -> bool:
        ticket = query.get(Ticket, params["ticket_id"])
        assert ticket is not None
        return "internal_note" in ticket.redacted_fields

    @ontology.function(api_name="teamTickets")
    def _team_tickets(query: BoundQuery, params: dict[str, Any]) -> list[str]:
        tickets = query.traverse(params["team_id"], via=owns)
        return [t.id for t in tickets]

    @ontology.function(api_name="ticketsWhere")
    def _tickets_where(query: BoundQuery, params: dict[str, Any]) -> list[str]:
        tickets = query.list(Ticket, where=params.get("where"))
        return [t.id for t in tickets]

    return ontology, Ticket, Team, owns


def _human() -> Consumer:
    return Consumer(actor_id="u1", role="Agent", scope_level="team", scope_id="t1", kind="human")


def _ai() -> Consumer:
    return Consumer(actor_id="ai1", role="Agent", scope_level="team", scope_id="t1", kind="ai")


# -- zero-manual-wiring: bind()/for_consumer() and direct OntologyClient ----


class TestZeroManualWiring:
    def test_callable_via_bind_and_for_consumer(self) -> None:
        ontology, _Ticket, _Team, _owns = _build_ontology()
        store = InMemoryStore(ontology.definition.registry)
        runtime = ontology.bind(store)
        client = runtime.for_consumer(_human())
        client.ingest(
            "Ticket", [{"id": "t1", "subject": "printer"}, {"id": "t2", "subject": "wifi"}], SRC
        )

        assert client.call_function("ticketCount", {}) == 2

    def test_callable_via_direct_ontology_client(self) -> None:
        ontology, _Ticket, _Team, _owns = _build_ontology()
        store = InMemoryStore(ontology.definition.registry)
        client = OntologyClient(ontology, store, _human())
        client.ingest("Ticket", [{"id": "t1", "subject": "printer"}], SRC)

        assert client.call_function("ticketCount", {}) == 1


# -- typed reads inside a function handler: hydrated + consumer-scoped ------


class TestTypedReadsInsideHandler:
    def test_returns_hydrated_model_instances(self) -> None:
        ontology, _Ticket, _Team, _owns = _build_ontology()
        store = InMemoryStore(ontology.definition.registry)
        client = OntologyClient(ontology, store, _human())
        client.ingest("Ticket", [{"id": "t1", "subject": "printer"}], SRC)

        assert client.call_function("firstTicketSubject", {}) == "printer"

    def test_same_function_sees_different_redaction_by_calling_consumer(self) -> None:
        """Same function, same stored row -- called via a human consumer's
        client sees `internal_note` redacted, called via an ai consumer's
        client does not. The function cannot read as another consumer than
        the one that invoked it (spec module docstring, `ontary.functions`)."""
        ontology, _Ticket, _Team, _owns = _build_ontology()
        store = InMemoryStore(ontology.definition.registry)
        runtime = ontology.bind(store)
        human_client = runtime.for_consumer(_human())
        human_client.ingest(
            "Ticket",
            [{"id": "t1", "subject": "printer", "internal_note": "escalate"}],
            SRC,
        )
        ai_client = runtime.for_consumer(_ai())

        assert human_client.call_function("internalNoteRedacted", {"ticket_id": "t1"}) is True
        assert ai_client.call_function("internalNoteRedacted", {"ticket_id": "t1"}) is False

    def test_traverse_via_link_handle_returns_hydrated_targets(self) -> None:
        ontology, _Ticket, _Team, owns = _build_ontology()
        store = InMemoryStore(ontology.definition.registry)
        client = OntologyClient(ontology, store, _human())
        client.ingest("Team", [{"id": "team-1", "name": "Support"}], SRC)
        client.ingest("Ticket", [{"id": "t1", "subject": "printer"}], SRC)
        client.ingest_links(owns.api_name, [("team-1", "t1")], SRC)

        result = client.call_function("teamTickets", {"team_id": "team-1"})
        assert result == ["t1"]


# -- fail-closed class resolution + where= validation -----------------------


class TestFailClosedResolution:
    def test_undecorated_subclass_raises_unknown_name(self) -> None:
        ontology, Ticket, _Team, _owns = _build_ontology()

        class SubTicket(Ticket):
            pass

        @ontology.function(api_name="badGet")
        def _bad(query: BoundQuery, params: dict[str, Any]) -> Any:
            return query.get(SubTicket, "t1")

        store = InMemoryStore(ontology.definition.registry)
        client = OntologyClient(ontology, store, _human())
        client.ingest("Ticket", [{"id": "t1", "subject": "printer"}], SRC)
        with raises_code(ValidationFailed, "UNKNOWN_NAME"):
            client.call_function("badGet", {})

    def test_cross_ontology_class_raises_unknown_name(self) -> None:
        ontology, _Ticket, _Team, _owns = _build_ontology()
        _other_ontology, OtherTicket, _OtherTeam, _other_owns = _build_ontology()

        @ontology.function(api_name="crossGet")
        def _cross(query: BoundQuery, params: dict[str, Any]) -> Any:
            return query.get(OtherTicket, "t1")

        store = InMemoryStore(ontology.definition.registry)
        client = OntologyClient(ontology, store, _human())
        with raises_code(ValidationFailed, "UNKNOWN_NAME"):
            client.call_function("crossGet", {})

    def test_cross_ontology_link_handle_raises_unknown_name(self) -> None:
        ontology, _Ticket, _Team, _owns = _build_ontology()
        _other_ontology, _OtherTicket, _OtherTeam, other_owns = _build_ontology()

        @ontology.function(api_name="crossTraverse")
        def _cross(query: BoundQuery, params: dict[str, Any]) -> Any:
            return query.traverse("team-1", via=other_owns)

        store = InMemoryStore(ontology.definition.registry)
        client = OntologyClient(ontology, store, _human())
        with raises_code(ValidationFailed, "UNKNOWN_NAME"):
            client.call_function("crossTraverse", {})

    def test_unbound_registry_typed_call_raises_unknown_name(self) -> None:
        """A `BoundQuery` constructed with only the two required positional
        args (`query`, `consumer` -- as dedicated unit tests exercising the
        string-form surface do) has no bound registry. A typed call on it
        must fail closed with `UNKNOWN_NAME`, not silently succeed or raise
        an unrelated error."""
        ontology, Ticket, _Team, _owns = _build_ontology()
        store = InMemoryStore(ontology.definition.registry)
        client = OntologyClient(ontology, store, _human())
        client.ingest("Ticket", [{"id": "t1", "subject": "printer"}], SRC)

        from ontary.query import GuardedQuery

        guarded = GuardedQuery(store, ontology.definition.registry, ontology.definition.policy)
        query = BoundQuery(guarded, _human())
        with raises_code(ValidationFailed, "UNKNOWN_NAME"):
            query.get(Ticket, "t1")

    def test_typed_where_unknown_key_raises_unknown_field(self) -> None:
        ontology, _Ticket, _Team, _owns = _build_ontology()
        store = InMemoryStore(ontology.definition.registry)
        client = OntologyClient(ontology, store, _human())
        client.ingest("Ticket", [{"id": "t1", "subject": "printer"}], SRC)

        with raises_code(ValidationFailed, "UNKNOWN_FIELD"):
            client.call_function("ticketsWhere", {"where": {"no_such_field": "x"}})


# -- @ontology.function declaration errors -----------------------------------


class TestFunctionDeclaration:
    def test_duplicate_declaration_raises(self) -> None:
        ontology = Ontology(name="dup", scope_levels=LEVELS)

        @ontology.function(api_name="dupFn")
        def _first(query: BoundQuery, params: dict[str, Any]) -> int:
            return 1

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.function(api_name="dupFn")
            def _second(query: BoundQuery, params: dict[str, Any]) -> int:
                return 2
        assert "duplicate" in str(exc_info.value)

    def test_declaration_after_definition_frozen_raises(self) -> None:
        ontology = Ontology(name="frozen", scope_levels=LEVELS)
        ontology.definition  # freeze

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.function(api_name="tooLate")
            def _late(query: BoundQuery, params: dict[str, Any]) -> int:
                return 1
        assert "frozen" in str(exc_info.value)

    def test_default_api_name_is_function_name(self) -> None:
        ontology = Ontology(name="default-name", scope_levels=LEVELS)

        @ontology.function()
        def myFunction(query: BoundQuery, params: dict[str, Any]) -> int:
            return 7

        definition = ontology.definition
        assert definition.registry.get_function("myFunction").api_name == "myFunction"
        store = InMemoryStore(definition.registry)
        client = OntologyClient(ontology, store, _human())
        assert client.call_function("myFunction", {}) == 7
