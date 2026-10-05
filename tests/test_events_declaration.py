"""Event declarations share property types, with no object identity metadata."""

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ValidationError

from ontary.authoring import Ontology, _derive_properties, prop
from ontary.errors import ValidationFailed
from ontary.meta import ActionTypeDef, EventTypeDef, OntologyRegistry, Sensitivity
from ontary.model import ActionParams, Event, OntologyObject, _class_stamp


class Status(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class Detail(BaseModel):
    label: str
    count: int
    status: Status
    note: str | None = None


class Payload(BaseModel):
    text: str
    count: int
    ratio: float
    enabled: bool
    day: date
    instant: datetime
    status: Status
    choice: Literal["a", "b"]
    detail: Detail
    secret: str | None = prop(sensitivity=Sensitivity(ai_usable=False))


def _ontology() -> Ontology:
    return Ontology("events", scope_levels=["org"])


def _target(ontology: Ontology) -> type[OntologyObject]:
    @ontology.object(layer="L0", scope="unscoped")
    class Record(OntologyObject):
        id: str = prop(primary_key=True)

    return Record


def test_payload_properties_match_objects_and_stamp() -> None:
    ontology = _ontology()

    @ontology.event(api_name="Happened", description="A fact.")
    class Fact(Payload, Event):
        pass

    class Record(Payload, OntologyObject):
        id: str = prop(primary_key=True)

    properties, _ = _derive_properties(Record)
    event_def = ontology.registry.get_event_type("Happened")
    assert event_def == EventTypeDef(
        api_name="Happened", description="A fact.", properties=properties[:-1]
    )
    assert _class_stamp(Fact) == ("Happened", ontology.registry)
    assert Fact.model_fields["secret"].default is None
    assert Fact.model_config["extra"] == "forbid"
    fact = Fact(
        text="fact", count=1, ratio=1.5, enabled=True,
        day=date(2026, 10, 4), instant=datetime(2026, 10, 4),
        status=Status.OPEN, choice="a",
        detail=Detail(label="detail", count=2, status=Status.CLOSED),
    )
    assert fact.secret is None


def test_default_name_description_and_extra_forbidden() -> None:
    ontology = _ontology()

    @ontology.event()
    class Fact(Event):
        value: str

    assert ontology.registry.get_event_type("Fact").description is None
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Fact.model_validate({"value": "yes", "unknown": "no"})


@pytest.mark.parametrize(
    "metadata, marker",
    [
        ({"primary_key": True}, "primary_key"),
        ({"transitions": {"initial": ["open"], "moves": {}}}, "transitions"),
        ({"scope_level": "org"}, "scope_level"),
    ],
)
def test_object_only_metadata_refused(metadata: dict[str, Any], marker: str) -> None:
    ontology = _ontology()

    class Fact(Event):
        value: str = prop(**metadata)

    with pytest.raises(ValidationFailed) as exc:
        ontology.event()(Fact)
    assert exc.value.code == "ONTOLOGY_INVALID"
    assert str(exc.value) == f"Fact.value: prop({marker}=...) is not supported on events"


def test_unsupported_annotation_matches_object_refusal() -> None:
    ontology = _ontology()

    class Fact(Event):
        value: bytes

    with pytest.raises(ValidationFailed) as exc:
        ontology.event()(Fact)
    assert exc.value.code == "ONTOLOGY_INVALID"
    assert str(exc.value) == (
        "Fact.value: unmappable annotation <class 'bytes'> "
        "(accepted: str, int, float, bool, datetime.date, datetime.datetime, dict[...], "
        "list[...], Any, a string Enum or Literal[...] -- or override with "
        "prop(property_type=...))"
    )


@pytest.mark.parametrize("sensitivity", [Sensitivity(ai_usable=False), Sensitivity(human_visible=False)])
def test_restricted_fields_must_be_optional(sensitivity: Sensitivity) -> None:
    class Fact(Event):
        value: str = prop(sensitivity=sensitivity)

    with pytest.raises(ValidationFailed) as exc:
        _ontology().event()(Fact)
    assert exc.value.code == "ONTOLOGY_INVALID"
    assert str(exc.value) == (
        "Fact.value: restricted sensitivity (human_visible=False or ai_usable=False) "
        "requires an Optional annotation"
    )


def test_non_event_class_refused() -> None:
    class Fact(BaseModel):
        pass

    with pytest.raises(ValidationFailed) as exc:
        _ontology().event()(Fact)
    assert exc.value.code == "ONTOLOGY_INVALID"
    assert str(exc.value) == "'Fact' must subclass Event to use @ontology.event(...)"


@pytest.mark.parametrize("foreign", [False, True])
def test_class_cannot_be_decorated_twice(foreign: bool) -> None:
    ontology = _ontology()

    @ontology.event()
    class Fact(Event):
        pass

    with pytest.raises(ValidationFailed) as exc:
        (_ontology() if foreign else ontology).event(api_name="Renamed")(Fact)
    assert exc.value.code == "ONTOLOGY_INVALID"
    assert str(exc.value) == "'Fact' is already registered as an event (a class may be decorated once)"


def test_subclass_can_register_with_its_own_stamp() -> None:
    ontology = _ontology()

    @ontology.event()
    class Fact(Event):
        value: str

    @ontology.event()
    class Sub(Fact):
        count: int

    assert _class_stamp(Sub) == ("Sub", ontology.registry)
    assert [p.name for p in ontology.registry.get_event_type("Sub").properties] == ["value", "count"]


def test_event_registration_is_frozen_after_definition() -> None:
    ontology = _ontology()
    _target(ontology)
    _ = ontology.definition

    class Fact(Event):
        pass

    with pytest.raises(ValidationFailed) as exc:
        ontology.event()(Fact)
    assert exc.value.code == "ONTOLOGY_INVALID"
    assert str(exc.value) == (
        "Ontology 'events': cannot register event type 'Fact' after .definition has "
        "already been built (registrations are frozen once the definition is accessed)"
    )
    assert ontology.registry.event_types == {}
    assert _class_stamp(Fact) == (None, None)


def test_emits_preserves_names_order_and_collapses_duplicates() -> None:
    ontology = _ontology()
    record = _target(ontology)

    @ontology.event(api_name="Second")
    class A(Event):
        pass

    @ontology.event()
    class B(Event):
        pass

    class Params(ActionParams):
        pass

    @ontology.action(Params, target=record, roles=[], emits=[B, A, B])
    def run(ctx: Any, params: Params) -> dict[str, Any]:
        return {}

    assert ontology.registry.get_action_type("Params").emits == ["B", "Second"]
    assert ontology.registry.get_action_type("Params").model_dump()["emits"] == ["B", "Second"]
    ontology.registry.validate()


@pytest.mark.parametrize("kind", ["undecorated", "subclass", "foreign", "object", "missing_descriptor"])
def test_emits_requires_event_of_this_ontology(kind: str) -> None:
    ontology = _ontology()
    record = _target(ontology)

    class Fact(Event):
        pass

    if kind == "foreign":
        _ontology().event()(Fact)
    elif kind == "missing_descriptor":
        Fact._ontary_api_name = "Fact"
        Fact._ontary_registry = ontology.registry
    elif kind == "subclass":
        ontology.event()(Fact)

        class Sub(Fact):
            pass

        Fact = Sub
    event_cls = record if kind == "object" else Fact

    class Params(ActionParams):
        pass

    with pytest.raises(ValidationFailed) as exc:
        @ontology.action(Params, target=record, roles=[], emits=[event_cls])
        def run(ctx: Any, params: Params) -> dict[str, Any]:
            return {}

    assert exc.value.code == "ONTOLOGY_INVALID"
    assert str(exc.value) == (
        f"{event_cls.__name__!r} is not a registered event on this Ontology "
        "-- cannot use it in action 'Params' emits"
    )
    assert ontology.registry.action_types == {}
    assert _class_stamp(Params) == (None, None)


def test_registry_duplicate_unknown_and_defensive_copy() -> None:
    registry = OntologyRegistry()
    event = EventTypeDef(api_name="Fact", description=None, properties=[])
    registry.register_event_type(event)
    assert registry.event_types == {"Fact": event}
    registry.event_types.clear()
    assert registry.get_event_type("Fact") is event
    with pytest.raises(ValidationFailed) as exc:
        registry.register_event_type(event)
    assert exc.value.code == "ONTOLOGY_INVALID"
    assert str(exc.value) == "duplicate EventTypeDef api_name: 'Fact'"
    with pytest.raises(ValidationFailed) as exc:
        registry.get_event_type("Nope")
    assert exc.value.code == "UNKNOWN_NAME"
    assert str(exc.value) == "unregistered event type: 'Nope'"


def test_registry_cross_check_refuses_unknown_emits() -> None:
    ontology = _ontology()
    _target(ontology)
    ontology.registry.register_action_type(ActionTypeDef(
        api_name="Run", display_name="Run", target_type="Record",
        executable_by_roles=[], description="", emits=["Nope"],
    ))
    with pytest.raises(ValidationFailed) as exc:
        ontology.registry.validate()
    assert exc.value.code == "ONTOLOGY_INVALID"
    assert str(exc.value) == "ActionTypeDef 'Run': dangling event 'Nope'"


@pytest.mark.parametrize("accept", ["EVENT_NEVER_EMITTED", ["EVENT_NEVER_EMITTED"] * 2])
def test_accept_string_sequence_and_deduplication(accept: Any) -> None:
    ontology = _ontology()

    @ontology.event(accept=accept)
    class Fact(Event):
        pass

    assert ontology.registry.get_event_type("Fact").accept == ("EVENT_NEVER_EMITTED",)


@pytest.mark.parametrize("descriptor", [False, True])
def test_accept_refuses_other_lint_codes(descriptor: bool) -> None:
    with pytest.raises(ValidationFailed) as exc:
        if descriptor:
            EventTypeDef(api_name="Fact", description=None, properties=[], accept=("AUDIT_TYPE",))
        else:
            @_ontology().event(accept="AUDIT_TYPE")
            class Fact(Event):
                pass
    assert exc.value.code == "ONTOLOGY_INVALID"
    assert str(exc.value) == (
        "EventTypeDef 'Fact': cannot accept 'AUDIT_TYPE'; accepted codes: EVENT_NEVER_EMITTED"
    )
