"""Authoring tests for governed capability and effect declarations (M5 T2).

These tests stop at descriptor authoring: providers, emission, and dispatch are
later tasks. The important boundary here is that handles retain registry
identity while the derived IR retains only JSON-serializable api-name strings.
"""

from __future__ import annotations

from typing import Protocol, cast

from conftest import raises_code

from ontary.actions import ActionContext
from ontary.authoring import ActionParams, Ontology, OntologyObject, prop, target
from ontary.effects import EffectPayload
from ontary.errors import ValidationFailed
from ontary.functions import BoundQuery


class WeatherReader(Protocol):
    def temperature(self, city: str) -> float: ...


def _notification_payload() -> type[EffectPayload]:
    """Return a fresh class because one payload class is single-declaration."""

    class SendNotification(EffectPayload):
        recipient: str
        tags: list[str] | None = None

    return SendNotification


def _ontology(name: str = "effects") -> tuple[Ontology, type[OntologyObject]]:
    ontology = Ontology(name=name, scope_levels=["global"])

    @ontology.object(layer="L0", scope="unscoped")
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)

    return ontology, Ticket


def test_capability_and_effect_derive_registered_descriptors_and_handles() -> None:
    ontology, _Ticket = _ontology()
    send_notification = _notification_payload()

    weather = ontology.capability(
        WeatherReader, name="Weather", description="Reads current weather."
    )
    notification = ontology.effect(
        send_notification,
        api_name="Notify",
        description="Sends a notification.",
    )

    assert weather.api_name == "Weather"
    assert weather.proto is WeatherReader
    assert weather.registry is ontology.registry
    assert ontology.registry.get_capability("Weather").model_dump() == {
        "api_name": "Weather",
        "description": "Reads current weather.",
    }
    assert notification.api_name == "Notify"
    assert notification.payload_cls is send_notification
    assert notification.registry is ontology.registry
    assert ontology.registry.get_effect_type("Notify").model_dump() == {
        "api_name": "Notify",
        "description": "Sends a notification.",
        "payload": [
            {"name": "recipient", "type_name": "str", "required": True},
            {"name": "tags", "type_name": "list[str]", "required": False},
        ],
    }


def test_effect_requires_effect_payload_subclass() -> None:
    ontology, _Ticket = _ontology()

    class NotAnEffect:
        pass

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        ontology.effect(cast(type[EffectPayload], NotAnEffect))
    assert "EffectPayload" in str(exc_info.value)


def test_capability_after_definition_is_frozen() -> None:
    ontology, _Ticket = _ontology()
    ontology.definition

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        ontology.capability(WeatherReader)
    assert "frozen" in str(exc_info.value)


def test_effect_after_definition_is_frozen() -> None:
    ontology, _Ticket = _ontology()
    ontology.definition

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        ontology.effect(_notification_payload())
    assert "frozen" in str(exc_info.value)


def test_action_rejects_foreign_capability_handle_naming_capability() -> None:
    ontology, Ticket = _ontology("first")
    other, _OtherTicket = _ontology("second")
    foreign = other.capability(WeatherReader)

    class Params(ActionParams):
        ticket_id: str = target(Ticket)

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

        @ontology.action(
            Params, target=Ticket, roles=["Agent"], capabilities=[foreign]
        )
        def act(ctx: ActionContext, params: Params) -> dict[str, str]:
            return {}
    assert "capability" in str(exc_info.value)
    assert "different Ontology" in str(exc_info.value)


def test_action_rejects_foreign_effect_handle_naming_effect() -> None:
    ontology, Ticket = _ontology("first")
    other, _OtherTicket = _ontology("second")
    foreign = other.effect(_notification_payload())

    class Params(ActionParams):
        ticket_id: str = target(Ticket)

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

        @ontology.action(Params, target=Ticket, roles=["Agent"], effects=[foreign])
        def act(ctx: ActionContext, params: Params) -> dict[str, str]:
            return {}
    assert "effect" in str(exc_info.value)
    assert "different Ontology" in str(exc_info.value)


def test_effect_payload_class_declared_on_two_ontologies_raises() -> None:
    first, _FirstTicket = _ontology("first")
    second, _SecondTicket = _ontology("second")
    payload_cls = _notification_payload()
    first.effect(payload_cls)

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        second.effect(payload_cls)
    assert "another Ontology" in str(exc_info.value)


def test_effect_payload_subclass_does_not_inherit_parent_declaration_stamp() -> None:
    first, _FirstTicket = _ontology("first")
    second, _SecondTicket = _ontology("second")
    parent = _notification_payload()
    first.effect(parent)

    class SpecializedNotification(parent):
        urgency: int

    specialized = second.effect(SpecializedNotification)

    assert specialized.registry is second.registry
    assert second.registry.get_effect_type("SpecializedNotification").api_name == (
        "SpecializedNotification"
    )


def test_function_refuses_effects() -> None:
    ontology, _Ticket = _ontology()
    notification = ontology.effect(_notification_payload())

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

        @ontology.function(effects=[notification])
        def invalid(query: BoundQuery, params: dict[str, object]) -> object:
            return None
    assert "function" in str(exc_info.value)
    assert "effects" in str(exc_info.value)


def test_declared_but_unused_capability_and_effect_validate() -> None:
    ontology, _Ticket = _ontology()
    ontology.capability(WeatherReader)
    ontology.effect(_notification_payload())

    ontology.validate()


def test_action_and_function_record_handle_api_names() -> None:
    ontology, Ticket = _ontology()
    weather = ontology.capability(WeatherReader)
    notification = ontology.effect(_notification_payload())

    class Params(ActionParams):
        ticket_id: str = target(Ticket)

    @ontology.action(
        Params,
        target=Ticket,
        roles=["Agent"],
        capabilities=[weather],
        effects=[notification],
        api_name="CheckWeather",
    )
    def act(ctx: ActionContext, params: Params) -> dict[str, str]:
        return {}

    @ontology.function(capabilities=[weather], api_name="Forecast")
    def forecast(query: BoundQuery, params: dict[str, object]) -> object:
        return None

    assert ontology.registry.get_action_type("CheckWeather").capabilities == [
        "WeatherReader"
    ]
    assert ontology.registry.get_action_type("CheckWeather").effects == [
        "SendNotification"
    ]
    assert ontology.registry.get_function("Forecast").capabilities == ["WeatherReader"]
    ontology.validate()


def test_same_api_names_on_two_ontologies_do_not_cross_talk() -> None:
    first, _FirstTicket = _ontology("first")
    second, _SecondTicket = _ontology("second")

    class FirstPayload(EffectPayload):
        message: str

    class SecondPayload(EffectPayload):
        message: str

    first_capability = first.capability(
        WeatherReader, name="SharedCapability", description="first capability"
    )
    second_capability = second.capability(
        WeatherReader, name="SharedCapability", description="second capability"
    )
    first_effect = first.effect(
        FirstPayload, api_name="SharedEffect", description="first effect"
    )
    second_effect = second.effect(
        SecondPayload, api_name="SharedEffect", description="second effect"
    )

    assert first_capability.registry is first.registry
    assert second_capability.registry is second.registry
    assert first_effect.registry is first.registry
    assert second_effect.registry is second.registry
    assert first.registry.get_capability("SharedCapability").description == (
        "first capability"
    )
    assert second.registry.get_capability("SharedCapability").description == (
        "second capability"
    )
    assert first.registry.get_effect_type("SharedEffect").description == "first effect"
    assert second.registry.get_effect_type("SharedEffect").description == "second effect"
