"""Typed `OntologyClient.get`/`.list` (spec typed-authoring T3): class-form
overloads resolve to `api_name`, hydrate a `StoredObject` into the author's
`OntologyObject` subclass, populate `redacted_fields`/`lineage`, validate
`where=` key existence (`UNKNOWN_FIELD`), and wrap a bad stored value in a
coded `INVALID_RECORD` validation-kind error instead of a bare pydantic
`ValidationError`.

Built on a small in-test "Ticket" ontology (class-based authoring, T1/T2)
plus `InMemoryStore` (M3.5 T5) -- exercises the client end to end rather
than `GuardedQuery`/`hydrate` in isolation, since T3's whole point is the
seam between them.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import pytest
from conftest import raises_code
from pydantic import Field, ValidationError

from ontary.authoring import LinkHandle, Ontology, OntologyObject, hydrate, prop
from ontary.client import OntologyClient
from ontary.errors import InternalError, ValidationFailed, VisibilityError
from ontary.meta import Cardinality, Sensitivity
from ontary.scope import DirectProperty
from ontary.security import Consumer
from ontary.store import Lineage, Source, StoredObject
from ontary.store.inmemory import InMemoryStore

SRC = Source(source_system="test")


def _build_ontology() -> tuple[Ontology, type[OntologyObject]]:
    ontology = Ontology(name="t3", scope_levels=["team"])

    @ontology.object(layer="L0", scope="unscoped")
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        subject: str
        created_at: datetime
        # Restricted (hidden from a human consumer); Optional per AC6.
        internal_note: str | None = prop(
            default=None, sensitivity=Sensitivity(human_visible=False)
        )
        # Fully visible, optional -- may simply be absent (sparse ingest),
        # which must NOT be confused with a redacted field (spec §8).
        resolution: str | None = None

    return ontology, Ticket


def _human_consumer() -> Consumer:
    return Consumer(actor_id="u1", role="Agent", scope_level="team", scope_id="t1", kind="human")


def _ai_consumer() -> Consumer:
    return Consumer(actor_id="ai1", role="Agent", scope_level="team", scope_id="t1", kind="ai")


def _client(consumer: Consumer) -> tuple[OntologyClient, type[OntologyObject], InMemoryStore]:
    ontology, Ticket = _build_ontology()
    definition = ontology.definition
    definition.validate()
    store = InMemoryStore(definition.registry)
    client = OntologyClient(definition, store, consumer)
    return client, Ticket, store


class TestTypedGetList:
    def test_date_hydration_rejects_datetime_shaped_storage_value(self) -> None:
        ontology = Ontology(name="date-hydration", scope_levels=["team"])

        @ontology.object(layer="L0", scope="unscoped")
        class Event(OntologyObject):
            id: str = prop(primary_key=True)
            occurred_on: date

        ontology.validate()
        store = InMemoryStore(ontology.registry)
        client = OntologyClient(ontology, store, _human_consumer())
        store.insert("Event", {"id": "e1", "occurred_on": "2026-08-25"}, SRC)

        # Pydantic can coerce midnight datetime strings into dates. Planting
        # one behind the write guard proves hydration enforces the narrower
        # durable YYYY-MM-DD shape itself instead of relying on that coercion.
        store._objects[-1]["payload"] = json.dumps(
            {"id": "e1", "occurred_on": "2026-08-25T00:00:00"}
        )
        with raises_code(ValidationFailed, "INVALID_RECORD"):
            client.get(Event, "e1")

    def test_date_hydration_rejects_python_date_as_non_storage_shape(self) -> None:
        ontology = Ontology(name="date-storage-shape", scope_levels=["team"])

        @ontology.object(layer="L0", scope="unscoped")
        class Event(OntologyObject):
            id: str = prop(primary_key=True)
            occurred_on: date

        ontology.validate()
        stored = StoredObject(
            payload={"id": "e1", "occurred_on": date(2026, 8, 25)},
            lineage=Lineage(
                object_type="Event",
                object_id="e1",
                valid_from="2026-08-25T00:00:00+00:00",
                valid_to=None,
                source_system="corrupt-fixture",
                source_id=None,
                extracted_at=None,
            ),
        )

        # A date object is valid input but not valid durable storage. Pydantic
        # would accept it unchanged, so this assertion specifically pins the
        # pre-hydration storage-shape guard.
        with raises_code(ValidationFailed, "INVALID_RECORD"):
            hydrate(Event, stored, "human")

    def test_get_returns_model_instance_with_hidden_field_redacted(self) -> None:
        client, Ticket, _ = _client(_human_consumer())
        client.ingest(
            "Ticket",
            [
                {
                    "id": "t1",
                    "subject": "printer on fire",
                    "created_at": "2026-07-24T10:00:00+00:00",
                    "internal_note": "escalate to facilities",
                    # `resolution` deliberately absent -- sparse, visible.
                }
            ],
            SRC,
        )

        ticket = client.get(Ticket, "t1")
        assert ticket is not None
        assert isinstance(ticket, Ticket)
        assert ticket.subject == "printer on fire"
        assert ticket.internal_note is None
        assert "internal_note" in ticket.redacted_fields
        # A visible-but-absent field is None too, but NOT redacted (AC6).
        assert ticket.resolution is None
        assert "resolution" not in ticket.redacted_fields

    def test_lineage_populated_and_not_a_model_field(self) -> None:
        client, Ticket, _ = _client(_human_consumer())
        client.ingest(
            "Ticket",
            [{"id": "t1", "subject": "s", "created_at": "2026-07-24T10:00:00+00:00"}],
            SRC,
        )
        ticket = client.get(Ticket, "t1")
        assert ticket is not None
        assert ticket.lineage is not None
        assert ticket.lineage.object_id == "t1"
        assert ticket.lineage.source_system == "test"
        assert "lineage" not in type(ticket).model_fields
        assert "redacted_fields" not in type(ticket).model_fields

    def test_list_returns_model_instances(self) -> None:
        client, Ticket, _ = _client(_human_consumer())
        client.ingest(
            "Ticket",
            [
                {"id": "t1", "subject": "a", "created_at": "2026-07-24T10:00:00+00:00"},
                {"id": "t2", "subject": "b", "created_at": "2026-07-24T11:00:00+00:00"},
            ],
            SRC,
        )
        tickets = client.list(Ticket)
        assert {t.subject for t in tickets} == {"a", "b"}
        assert all(isinstance(t, Ticket) for t in tickets)

    def test_datetime_hydrates_to_datetime(self) -> None:
        client, Ticket, _ = _client(_human_consumer())
        client.ingest(
            "Ticket",
            [{"id": "t1", "subject": "a", "created_at": "2026-07-24T10:00:00+00:00"}],
            SRC,
        )
        ticket = client.get(Ticket, "t1")
        assert ticket is not None
        assert isinstance(ticket.created_at, datetime)

    def test_corrupt_stored_datetime_raises_coded_error_not_bare_validation_error(
        self,
    ) -> None:
        client, Ticket, store = _client(_human_consumer())
        # Plant a corrupt stored value by writing the row DIRECTLY into the
        # backend's storage, not through `store.insert`.
        #
        # `store.insert` used to be the bypass (ingest validates ISO strings; the
        # raw store did not). M9a closed that -- `declared_shape_violation` now
        # refuses a value that does not match its declared type on every write
        # path -- which is why this poke exists rather than a public call.
        #
        # The guarantee under test survives that change and still matters: a row
        # can hold a value today's declaration rejects because it was written
        # under an OLDER declaration. That is precisely ontology drift, and the
        # end-to-end version of this scenario (write under one shape, re-declare,
        # accept the drift, read) is covered by
        # tests/test_ontology_evolution.py. Here we only pin that hydration fails
        # with a CODED error rather than a bare pydantic `ValidationError`.
        store.insert(
            "Ticket",
            {"id": "t1", "subject": "a", "created_at": "2026-07-24T10:00:00+00:00"},
            SRC,
        )
        row = store._objects[-1]
        row["payload"] = json.dumps(
            {"id": "t1", "subject": "a", "created_at": "not-a-datetime"}
        )
        with raises_code(ValidationFailed, "INVALID_RECORD") as exc_info:
            client.get(Ticket, "t1")
        assert exc_info.value.kind == "validation"
        assert isinstance(exc_info.value.__cause__, ValidationError)

    def test_typed_get_with_undecorated_class_raises_unknown_name(self) -> None:
        client, _, _ = _client(_human_consumer())

        class NotRegistered(OntologyObject):
            id: str = prop(primary_key=True)

        with raises_code(ValidationFailed, "UNKNOWN_NAME"):
            client.get(NotRegistered, "t1")

    def test_undecorated_subclass_of_registered_class_raises_unknown_name(
        self,
    ) -> None:
        """A subclass that merely INHERITS `Ticket`'s `_ontary_api_name`/
        `_ontary_registry` ClassVars (never itself passed through
        `@ontology.object(...)`) must NOT resolve -- else it would
        silently hydrate as if it were the registered `Ticket` class."""
        client, Ticket, _ = _client(_human_consumer())
        client.ingest(
            "Ticket",
            [{"id": "t1", "subject": "a", "created_at": "2026-07-24T10:00:00+00:00"}],
            SRC,
        )

        class SubTicket(Ticket):
            pass

        with raises_code(ValidationFailed, "UNKNOWN_NAME"):
            client.get(SubTicket, "t1")

    def test_cross_ontology_same_api_name_raises_unknown_name_both_directions(
        self,
    ) -> None:
        """Two separate `Ontology` instances that each register a class
        under the SAME api_name ("Ticket") must never resolve each
        other's class -- a name-only stamp check would silently hydrate
        the wrong ontology's rows into the wrong class, with
        `redacted_fields` computed from the wrong sensitivity map."""
        client_a, TicketA, _ = _client(_human_consumer())
        client_b, TicketB, _ = _client(_human_consumer())

        client_a.ingest(
            "Ticket",
            [{"id": "t1", "subject": "a", "created_at": "2026-07-24T10:00:00+00:00"}],
            SRC,
        )
        client_b.ingest(
            "Ticket",
            [{"id": "t1", "subject": "b", "created_at": "2026-07-24T10:00:00+00:00"}],
            SRC,
        )

        with raises_code(ValidationFailed, "UNKNOWN_NAME"):
            client_b.get(TicketA, "t1")
        with raises_code(ValidationFailed, "UNKNOWN_NAME"):
            client_a.get(TicketB, "t1")

    def test_where_unknown_key_typed_raises_unknown_field(self) -> None:
        client, Ticket, _ = _client(_human_consumer())
        client.ingest(
            "Ticket",
            [{"id": "t1", "subject": "a", "created_at": "2026-07-24T10:00:00+00:00"}],
            SRC,
        )
        with raises_code(ValidationFailed, "UNKNOWN_FIELD"):
            client.list(Ticket, where={"nope": 1})

    def test_where_unknown_key_string_form_raises_unknown_field(self) -> None:
        client, _, _ = _client(_human_consumer())
        client.ingest(
            "Ticket",
            [{"id": "t1", "subject": "a", "created_at": "2026-07-24T10:00:00+00:00"}],
            SRC,
        )
        with raises_code(ValidationFailed, "UNKNOWN_FIELD") as exc_info:
            client.list("Ticket", where={"subjcet": "a"})
        assert str(exc_info.value) == (
            "Ticket: where= names unknown field(s) ['subjcet']"
        )

    def test_where_lineage_key_rejection_message_is_stable(self) -> None:
        client, Ticket, _ = _client(_human_consumer())
        client.ingest(
            "Ticket",
            [{"id": "t1", "subject": "a", "created_at": "2026-07-24T10:00:00+00:00"}],
            SRC,
        )

        for obj_type in (Ticket, "Ticket"):
            with raises_code(ValidationFailed, "UNKNOWN_FIELD") as exc_info:
                client.list(obj_type, where={"source_system": "test"})
            assert str(exc_info.value) == (
                "Ticket: where= names unknown field(s) ['source_system']"
            )

    def test_where_hidden_known_key_typed_raises_visibility_denied(self) -> None:
        client, Ticket, _ = _client(_human_consumer())
        client.ingest(
            "Ticket",
            [{"id": "t1", "subject": "a", "created_at": "2026-07-24T10:00:00+00:00"}],
            SRC,
        )
        with raises_code(VisibilityError, "VISIBILITY_DENIED"):
            client.list(Ticket, where={"internal_note": "x"})

    def test_ai_consumer_sees_internal_note_not_redacted(self) -> None:
        client, Ticket, _ = _client(_ai_consumer())
        client.ingest(
            "Ticket",
            [
                {
                    "id": "t1",
                    "subject": "a",
                    "created_at": "2026-07-24T10:00:00+00:00",
                    "internal_note": "visible to ai",
                }
            ],
            SRC,
        )
        ticket = client.get(Ticket, "t1")
        assert ticket is not None
        assert ticket.internal_note == "visible to ai"
        assert "internal_note" not in ticket.redacted_fields


def _build_default_injection_ontology() -> tuple[Ontology, type[OntologyObject]]:
    """A class exercising `_derive_properties`'s Optional-without-default
    injection path (spec §6/§8): a restricted-sensitivity `str | None`
    field with NO explicit default (must be injected as `None`), plus two
    Optional fields that must be left untouched -- one with an explicit
    non-None default, one with a `default_factory`."""
    ontology = Ontology(name="t3-default-injection", scope_levels=["team"])

    @ontology.object(layer="L0", scope="unscoped")
    class Widget(OntologyObject):
        id: str = prop(primary_key=True)
        # Restricted + Optional, NO default -- `_derive_properties` must
        # inject `default=None` and rebuild the model for this to even be
        # constructible without the field.
        secret: str | None = prop(sensitivity=Sensitivity(human_visible=False))
        # Optional with an explicit non-None default -- must be left
        # completely untouched (not required, default stays "keepme").
        tag: str | None = "keepme"
        # Optional with a `default_factory` -- also must be left
        # untouched (not required, default_factory still produces its
        # value, not silently overwritten with a plain `None` default).
        notes: list[str] | None = Field(default_factory=lambda: ["seed"])

    return ontology, Widget


class TestOptionalDefaultInjection:
    def test_restricted_optional_with_no_default_constructs_as_none(self) -> None:
        _, Widget = _build_default_injection_ontology()
        widget = Widget(id="w1", tag="x")
        assert widget.secret is None

    def test_restricted_optional_with_no_default_hydrates_as_none(self) -> None:
        ontology, Widget = _build_default_injection_ontology()
        definition = ontology.definition
        definition.validate()
        store = InMemoryStore(definition.registry)
        client = OntologyClient(definition, store, _human_consumer())
        client.ingest("Widget", [{"id": "w1"}], SRC)

        widget = client.get(Widget, "w1")
        assert widget is not None
        assert widget.secret is None

    def test_explicit_non_none_default_is_untouched(self) -> None:
        _, Widget = _build_default_injection_ontology()
        widget = Widget(id="w1")
        assert widget.tag == "keepme"
        assert not Widget.model_fields["tag"].is_required()

    def test_default_factory_optional_field_is_untouched(self) -> None:
        _, Widget = _build_default_injection_ontology()
        widget = Widget(id="w1")
        assert widget.notes == ["seed"]
        assert not Widget.model_fields["notes"].is_required()
        assert Widget.model_fields["notes"].default_factory is not None


# -- T4: typed traverse via LinkHandle + aggregate name validation --------


def _build_linked_ontology() -> (
    tuple[Ontology, type[OntologyObject], type[OntologyObject], Any, Any]
):
    """Ticket -[worksWith]-> Person (plain) and Ticket -[assignedTo]->
    Person (identity_revealing), plus a numeric `priority` field on
    Ticket for `aggregate`. Person has a human-hidden `ssn`."""
    ontology = Ontology(name="t4", scope_levels=["team"])

    @ontology.object(layer="L0", scope="unscoped")
    class Person(OntologyObject):
        id: str = prop(primary_key=True)
        name: str
        ssn: str | None = prop(default=None, sensitivity=Sensitivity(human_visible=False))

    @ontology.object(layer="L0", scope="unscoped")
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        subject: str
        priority: int = 1

    works_with = ontology.link("worksWith", Ticket, Person, Cardinality.MANY_TO_ONE)
    assigned_to = ontology.link(
        "assignedTo",
        Ticket,
        Person,
        Cardinality.MANY_TO_ONE,
        identity_revealing=True,
    )

    return ontology, Ticket, Person, works_with, assigned_to


def _linked_client(
    consumer: Consumer,
) -> tuple[
    OntologyClient,
    type[OntologyObject],
    type[OntologyObject],
    Any,
    Any,
    InMemoryStore,
]:
    ontology, Ticket, Person, works_with, assigned_to = _build_linked_ontology()
    definition = ontology.definition
    definition.validate()
    store = InMemoryStore(definition.registry)
    client = OntologyClient(definition, store, consumer)
    return client, Ticket, Person, works_with, assigned_to, store


class TestTypedTraverse:
    def test_traverse_via_handle_returns_typed_instances_with_redaction(self) -> None:
        client, Ticket, Person, works_with, _, _ = _linked_client(_human_consumer())
        client.ingest("Ticket", [{"id": "t1", "subject": "s", "priority": 1}], SRC)
        client.ingest(
            "Person", [{"id": "p1", "name": "Ada", "ssn": "123-45-6789"}], SRC
        )
        client.ingest_links("worksWith", [("t1", "p1")], SRC)

        results = client.traverse(works_with, "t1")
        assert len(results) == 1
        person = results[0]
        assert isinstance(person, Person)
        assert person.name == "Ada"
        assert person.ssn is None
        assert "ssn" in person.redacted_fields

    def test_traverse_via_handle_no_redaction_for_ai_consumer(self) -> None:
        client, Ticket, Person, works_with, _, _ = _linked_client(_ai_consumer())
        client.ingest("Ticket", [{"id": "t1", "subject": "s", "priority": 1}], SRC)
        client.ingest(
            "Person", [{"id": "p1", "name": "Ada", "ssn": "123-45-6789"}], SRC
        )
        client.ingest_links("worksWith", [("t1", "p1")], SRC)

        results = client.traverse(works_with, "t1")
        assert results[0].ssn == "123-45-6789"
        assert "ssn" not in results[0].redacted_fields

    def test_identity_revealing_link_typed_denied_for_human(self) -> None:
        client, Ticket, Person, _, assigned_to, _ = _linked_client(_human_consumer())
        client.ingest("Ticket", [{"id": "t1", "subject": "s", "priority": 1}], SRC)
        client.ingest("Person", [{"id": "p1", "name": "Ada"}], SRC)
        client.ingest_links("assignedTo", [("t1", "p1")], SRC)

        with raises_code(VisibilityError, "VISIBILITY_DENIED"):
            client.traverse(assigned_to, "t1")

    def test_identity_revealing_link_typed_allowed_for_ai(self) -> None:
        client, Ticket, Person, _, assigned_to, _ = _linked_client(_ai_consumer())
        client.ingest("Ticket", [{"id": "t1", "subject": "s", "priority": 1}], SRC)
        client.ingest("Person", [{"id": "p1", "name": "Ada"}], SRC)
        client.ingest_links("assignedTo", [("t1", "p1")], SRC)

        results = client.traverse(assigned_to, "t1")
        assert results[0].name == "Ada"

    def test_handle_from_another_ontology_raises_unknown_name(self) -> None:
        client_a, *_ = _linked_client(_human_consumer())
        _, _, _, works_with_b, _ = _build_linked_ontology()

        with raises_code(ValidationFailed, "UNKNOWN_NAME"):
            client_a.traverse(works_with_b, "t1")

    def test_handle_with_unregistered_link_name_raises_unknown_name(self) -> None:
        client, Ticket, Person, _, _, _ = _linked_client(_human_consumer())
        bogus = LinkHandle(api_name="nope", from_cls=Ticket, to_cls=Person)

        with raises_code(ValidationFailed, "UNKNOWN_NAME"):
            client.traverse(bogus, "t1")

    def test_typed_traverse_refuses_missing_internal_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _, _, works_with, _, _ = _linked_client(_human_consumer())
        monkeypatch.setattr(client, "_registry", None)
        monkeypatch.setattr(client, "_api_name_for", lambda cls: cls.__name__)

        with raises_code(InternalError, "INTERNAL_ERROR"):
            client.traverse(works_with, "t1")

    def test_traverse_string_form_unchanged(self) -> None:
        client, Ticket, Person, _, _, _ = _linked_client(_human_consumer())
        client.ingest("Ticket", [{"id": "t1", "subject": "s", "priority": 1}], SRC)
        client.ingest("Person", [{"id": "p1", "name": "Ada"}], SRC)
        client.ingest_links("worksWith", [("t1", "p1")], SRC)

        results = client.traverse("Ticket", "worksWith", "t1")
        assert [r.payload["id"] for r in results] == ["p1"]

    def test_traverse_string_form_missing_link_name_raises_type_error(self) -> None:
        client, _, _, works_with, _, _ = _linked_client(_human_consumer())
        client.ingest("Ticket", [{"id": "t1", "subject": "s", "priority": 1}], SRC)

        untyped_traverse: Any = client.traverse
        with pytest.raises(TypeError):
            untyped_traverse("t1", via=works_with)


class TestTypedAggregate:
    def test_aggregate_typed_unknown_value_field_raises_unknown_field(self) -> None:
        client, Ticket, _, _, _, _ = _linked_client(_human_consumer())
        client.ingest("Ticket", [{"id": "t1", "subject": "s", "priority": 3}], SRC)

        with raises_code(ValidationFailed, "UNKNOWN_FIELD"):
            client.aggregate(Ticket, "nope")

    def test_aggregate_typed_unknown_where_key_raises_unknown_field(self) -> None:
        client, Ticket, _, _, _, _ = _linked_client(_human_consumer())
        client.ingest("Ticket", [{"id": "t1", "subject": "s", "priority": 3}], SRC)

        with raises_code(ValidationFailed, "UNKNOWN_FIELD"):
            client.aggregate(Ticket, "priority", where={"nope": 1})

    def test_aggregate_string_unknown_where_key_raises_unknown_field(self) -> None:
        client, _, _, _, _, _ = _linked_client(_human_consumer())
        client.ingest("Ticket", [{"id": "t1", "subject": "s", "priority": 3}], SRC)

        with raises_code(ValidationFailed, "UNKNOWN_FIELD") as exc_info:
            client.aggregate("Ticket", "priority", where={"subjcet": "s"})
        assert str(exc_info.value) == (
            "Ticket: where= names unknown field(s) ['subjcet']"
        )

    def test_aggregate_by_string_unknown_where_key_raises_unknown_field(self) -> None:
        client, _, _, _, _, _ = _linked_client(_human_consumer())
        client.ingest("Ticket", [{"id": "t1", "subject": "s", "priority": 3}], SRC)

        with raises_code(ValidationFailed, "UNKNOWN_FIELD"):
            client.aggregate_by(
                "Ticket", "priority", "subject", where={"subjcet": "s"}
            )

    def test_aggregate_typed_unknown_group_by_raises_unknown_field(self) -> None:
        client, Ticket, _, _, _, _ = _linked_client(_human_consumer())
        client.ingest("Ticket", [{"id": "t1", "subject": "s", "priority": 3}], SRC)

        with raises_code(ValidationFailed, "UNKNOWN_FIELD"):
            client.aggregate_by(Ticket, "priority", "nope")

    def test_aggregate_typed_matches_string_form(self) -> None:
        client, Ticket, _, _, _, _ = _linked_client(_human_consumer())
        client.ingest(
            "Ticket",
            [
                {"id": "t1", "subject": "a", "priority": 2},
                {"id": "t2", "subject": "b", "priority": 4},
                {"id": "t3", "subject": "c", "priority": 6},
            ],
            SRC,
        )

        typed_result = client.aggregate(Ticket, "priority")
        string_result = client.aggregate("Ticket", "priority")
        assert typed_result == string_result == pytest.approx(4.0)


def _build_counting_ontology() -> tuple[Ontology, type[OntologyObject]]:
    """A team-scoped `Note` at the default `min_n=3`, declaring no
    `contributor_rules` -- so this also drives the row-count fallback
    through the client facade."""
    ontology = Ontology(name="t5", scope_levels=["team"])

    @ontology.object(
        layer="L0",
        scope=[DirectProperty(level="team", property_name="team_id")],
    )
    class Note(OntologyObject):
        id: str = prop(primary_key=True)
        team_id: str | None = prop(default=None, scope_level="team")

    return ontology, Note


def _counting_client(
    *, on_t1: int, on_t2: int = 0
) -> tuple[OntologyClient, type[OntologyObject]]:
    ontology, Note = _build_counting_ontology()
    definition = ontology.definition
    definition.validate()
    store = InMemoryStore(definition.registry)
    client = OntologyClient(definition, store, _human_consumer())
    rows = [{"id": f"t1-{i}", "team_id": "t1"} for i in range(on_t1)]
    rows += [{"id": f"t2-{i}", "team_id": "t2"} for i in range(on_t2)]
    if rows:
        client.ingest("Note", rows, SRC)
    return client, Note


class TestTypedCountContributors:
    """Reviewer P0: `OntologyClient.count_contributors` had no behavioural
    test at all -- replacing its body with an unguarded
    `len(store.read_all(...))` left the full suite green through BOTH
    overloads. The surface-set assertion in `tests/test_client.py` only
    pins the method's NAME, which cannot tell a delegating implementation
    from an ungated one. These assert behaviour through the client facade.
    """

    def test_string_overload_releases_at_threshold(self) -> None:
        client, _ = _counting_client(on_t1=3)
        assert client.count_contributors("Note") == 3

    def test_typed_overload_releases_at_threshold(self) -> None:
        client, Note = _counting_client(on_t1=3)
        assert client.count_contributors(Note) == 3

    def test_string_overload_refuses_below_min_n(self) -> None:
        client, _ = _counting_client(on_t1=2)
        with raises_code(VisibilityError, "MIN_N_VIOLATION"):
            client.count_contributors("Note")

    def test_typed_overload_refuses_below_min_n(self) -> None:
        """The typed path is a SEPARATE branch (`_api_name_for` +
        validation) -- gating one overload and not the other is exactly the
        divergence this pins."""
        client, Note = _counting_client(on_t1=2)
        with raises_code(VisibilityError, "MIN_N_VIOLATION"):
            client.count_contributors(Note)

    def test_out_of_scope_rows_are_not_counted(self) -> None:
        """2 visible on t1, 5 invisible on t2. An unscoped delegation would
        see 7, clear min_n=3, and answer for a population this consumer
        cannot read."""
        client, Note = _counting_client(on_t1=2, on_t2=5)
        with raises_code(VisibilityError, "MIN_N_VIOLATION"):
            client.count_contributors("Note")
        with raises_code(VisibilityError, "MIN_N_VIOLATION"):
            client.count_contributors(Note)

    def test_counts_only_the_visible_team(self) -> None:
        client, Note = _counting_client(on_t1=3, on_t2=5)
        assert client.count_contributors("Note") == 3
        assert client.count_contributors(Note) == 3

    def test_typed_unknown_where_key_raises_unknown_field(self) -> None:
        client, Note = _counting_client(on_t1=3)
        with raises_code(ValidationFailed, "UNKNOWN_FIELD"):
            client.count_contributors(Note, where={"nope": 1})

    def test_string_unknown_where_key_raises_unknown_field(self) -> None:
        client, _ = _counting_client(on_t1=3)
        with raises_code(ValidationFailed, "UNKNOWN_FIELD"):
            client.count_contributors("Note", where={"teem_id": "t1"})

    def test_typed_matches_string_form(self) -> None:
        client, Note = _counting_client(on_t1=4)
        assert client.count_contributors(Note) == client.count_contributors("Note")
