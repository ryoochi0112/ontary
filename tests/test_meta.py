import json
from datetime import date, datetime

import pytest
from conftest import raises_code

from ontary.errors import ValidationFailed
from ontary.meta import (
    ActionTypeDef,
    CapabilityDef,
    Cardinality,
    EffectFieldDef,
    EffectTypeDef,
    FunctionDef,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
    Sensitivity,
)
from ontary.typesys import validate_scalar


def _person_type() -> ObjectTypeDef:
    return ObjectTypeDef(
        api_name="Person",
        display_name="Person",
        description="A canonical person",
        layer="L0",
        properties=[
            PropertyDef(name="id", type="str"),
            PropertyDef(
                name="email",
                type="str",
                sensitivity=Sensitivity(ai_usable=False, human_visible=True),
                scope_level="person",
            ),
        ],
        primary_key="id",
    )


def _team_type() -> ObjectTypeDef:
    return ObjectTypeDef(
        api_name="Team",
        display_name="Team",
        description="A canonical team",
        layer="L0",
        properties=[PropertyDef(name="id", type="str")],
        primary_key="id",
    )


def test_valid_registration_and_validate_passes() -> None:
    registry = OntologyRegistry()
    registry.register_object_type(_person_type())
    registry.register_object_type(_team_type())
    registry.register_link_type(
        LinkTypeDef(
            api_name="memberOf",
            from_type="Person",
            to_type="Team",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Person is a member of a Team",
        )
    )
    registry.register_action_type(
        ActionTypeDef(
            api_name="DefineGoal",
            display_name="Define Goal",
            target_type="Team",
            executable_by_roles=["TeamOwner"],
            description="Defines a goal for a team",
        )
    )

    registry.validate()

    assert registry.get_object_type("Person").api_name == "Person"
    assert registry.get_link_type("memberOf").to_type == "Team"
    assert registry.get_action_type("DefineGoal").target_type == "Team"


@pytest.mark.parametrize(
    ("getter_name", "expected_code"),
    [
        ("get_object_type", "UNKNOWN_OBJECT_TYPE"),
        ("get_link_type", "UNKNOWN_LINK_TYPE"),
        ("get_action_type", "UNKNOWN_ACTION"),
        ("get_function", "UNKNOWN_NAME"),
        ("get_capability", "UNKNOWN_NAME"),
        ("get_effect_type", "UNKNOWN_NAME"),
    ],
)
def test_unknown_registry_lookup_raises_coded_validation_not_key_error(
    getter_name: str, expected_code: str
) -> None:
    registry = OntologyRegistry()
    getter = getattr(registry, getter_name)

    with raises_code(ValidationFailed, expected_code) as exc_info:
        getter("Missing")
    assert not isinstance(exc_info.value, KeyError)


def test_dangling_link_endpoint_raises() -> None:
    registry = OntologyRegistry()
    registry.register_object_type(_person_type())
    registry.register_link_type(
        LinkTypeDef(
            api_name="memberOf",
            from_type="Person",
            to_type="Team",  # not registered
            cardinality=Cardinality.MANY_TO_ONE,
            description="dangling",
        )
    )

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID"):
        registry.validate()


def test_dangling_action_target_raises() -> None:
    registry = OntologyRegistry()
    registry.register_object_type(_person_type())
    registry.register_action_type(
        ActionTypeDef(
            api_name="DefineGoal",
            display_name="Define Goal",
            target_type="Team",  # not registered
            executable_by_roles=["TeamOwner"],
            description="dangling",
        )
    )

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID"):
        registry.validate()


def test_duplicate_api_name_raises() -> None:
    registry = OntologyRegistry()
    registry.register_object_type(_person_type())

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID"):
        registry.register_object_type(_person_type())


def test_primary_key_not_in_properties_raises() -> None:
    with pytest.raises(ValueError):
        ObjectTypeDef(
            api_name="Broken",
            display_name="Broken",
            description="bad primary key",
            layer="L0",
            properties=[PropertyDef(name="id", type="str")],
            primary_key="missing",
        )


def test_owned_defaults_to_false_and_existing_definitions_remain_valid() -> None:
    person = _person_type()
    team = _team_type()
    assert person.owned is False
    assert person.owned_property_defaults() == {}
    assert person.is_owned_type is False

    registry = OntologyRegistry()
    registry.register_object_type(person)
    registry.register_object_type(team)
    registry.register_link_type(
        LinkTypeDef(
            api_name="memberOf",
            from_type="Person",
            to_type="Team",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Person is a member of a Team",
        )
    )
    assert registry.get_link_type("memberOf").owned is False

    registry.validate()


def test_owned_whole_type_is_valid() -> None:
    registry = OntologyRegistry()
    goal = _team_type().model_copy(update={"api_name": "Goal", "owned": True})
    registry.register_object_type(goal)

    registry.validate()
    assert goal.is_owned_type is True
    assert goal.owned_property_defaults() == {}


def test_owned_property_not_in_properties_raises() -> None:
    registry = OntologyRegistry()
    person = _person_type().model_copy(
        update={"owned": {"nickname": "n/a"}}
    )
    registry.register_object_type(person)

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        registry.validate()
    assert "nickname" in str(exc_info.value)


def test_owned_primary_key_raises() -> None:
    registry = OntologyRegistry()
    person = _person_type().model_copy(update={"owned": {"id": "x"}})
    registry.register_object_type(person)

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        registry.validate()
    assert "id" in str(exc_info.value)


def test_owned_default_wrong_type_raises() -> None:
    registry = OntologyRegistry()
    person = _person_type().model_copy(
        update={"owned": {"email": 123}}
    )
    registry.register_object_type(person)

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        registry.validate()
    assert "email" in str(exc_info.value)


def test_owned_default_none_raises_for_required_property() -> None:
    registry = OntologyRegistry()
    person = _person_type().model_copy(
        update={"owned": {"email": None}}
    )
    registry.register_object_type(person)

    # email is required=True by default in _person_type(), so a None
    # default should raise naming the property.
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        registry.validate()
    assert "email" in str(exc_info.value)


def test_owned_default_none_allowed_when_property_not_required() -> None:
    registry = OntologyRegistry()
    nickname_type = ObjectTypeDef(
        api_name="Widget",
        display_name="Widget",
        description="has an optional owned property",
        layer="L0",
        properties=[
            PropertyDef(name="id", type="str"),
            PropertyDef(name="nickname", type="str", required=False),
        ],
        primary_key="id",
        owned={"nickname": None},
    )
    registry.register_object_type(nickname_type)

    registry.validate()
    assert nickname_type.owned_property_defaults() == {"nickname": None}


def test_owned_valid_default_passes() -> None:
    registry = OntologyRegistry()
    widget = ObjectTypeDef(
        api_name="Widget",
        display_name="Widget",
        description="a source-backed type with one owned property",
        layer="L0",
        properties=[
            PropertyDef(name="id", type="str"),
            PropertyDef(name="status", type="str"),
        ],
        primary_key="id",
        owned={"status": "unset"},
    )
    registry.register_object_type(widget)

    registry.validate()


def test_owned_default_outside_property_choices_raises() -> None:
    registry = OntologyRegistry()
    widget = ObjectTypeDef(
        api_name="Widget",
        display_name="Widget",
        description="a source-backed type with one owned property",
        layer="L0",
        properties=[
            PropertyDef(name="id", type="str"),
            PropertyDef(
                name="status", type="str", choices=("open", "closed")
            ),
        ],
        primary_key="id",
        owned={"status": "pending"},
    )
    registry.register_object_type(widget)

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        registry.validate()
    assert "status" in str(exc_info.value)


def test_owned_default_invalid_iso_datetime_raises() -> None:
    registry = OntologyRegistry()
    widget = ObjectTypeDef(
        api_name="Widget",
        display_name="Widget",
        description="a source-backed type with a datetime owned property",
        layer="L0",
        properties=[
            PropertyDef(name="id", type="str"),
            PropertyDef(name="created_at", type="datetime"),
        ],
        primary_key="id",
        owned={"created_at": "not-a-date"},
    )
    registry.register_object_type(widget)

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        registry.validate()
    assert "created_at" in str(exc_info.value)


def test_owned_default_valid_iso_datetime_passes() -> None:
    registry = OntologyRegistry()
    widget = ObjectTypeDef(
        api_name="Widget",
        display_name="Widget",
        description="a source-backed type with a datetime owned property",
        layer="L0",
        properties=[
            PropertyDef(name="id", type="str"),
            PropertyDef(name="created_at", type="datetime"),
        ],
        primary_key="id",
        owned={"created_at": "2024-01-01T00:00:00"},
    )
    registry.register_object_type(widget)

    registry.validate()


def test_owned_link_type_defaults_to_false() -> None:
    registry = OntologyRegistry()
    registry.register_object_type(_person_type())
    registry.register_object_type(_team_type())
    owned_link = LinkTypeDef(
        api_name="assignedTo",
        from_type="Person",
        to_type="Team",
        cardinality=Cardinality.MANY_TO_ONE,
        description="ontology-owned link",
        owned=True,
    )
    registry.register_link_type(owned_link)

    registry.validate()
    assert owned_link.owned is True


def test_capability_and_effect_descriptors_are_reachable_and_json_serializable() -> None:
    registry = OntologyRegistry()
    capability = CapabilityDef(
        api_name="WeatherReader",
        description="Reads the current weather.",
    )
    effect = EffectTypeDef(
        api_name="SendNotification",
        description="Sends a notification after commit.",
        payload=[
            EffectFieldDef(name="recipient", type_name="str", required=True),
            EffectFieldDef(name="tags", type_name="list[str]", required=False),
        ],
    )
    registry.register_capability(capability)
    registry.register_effect_type(effect)

    assert registry.get_capability("WeatherReader") is capability
    assert registry.get_effect_type("SendNotification") is effect
    assert registry.capabilities == {"WeatherReader": capability}
    assert registry.effect_types == {"SendNotification": effect}
    json.dumps(capability.model_dump(mode="json"))
    json.dumps(effect.model_dump(mode="json"))


def test_duplicate_capability_api_name_raises_with_descriptor_name() -> None:
    registry = OntologyRegistry()
    capability = CapabilityDef(api_name="WeatherReader", description="Weather.")
    registry.register_capability(capability)

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        registry.register_capability(capability)
    assert "duplicate CapabilityDef api_name: 'WeatherReader'" in str(exc_info.value)


def test_duplicate_effect_api_name_raises_with_descriptor_name() -> None:
    registry = OntologyRegistry()
    effect = EffectTypeDef(
        api_name="SendNotification",
        description="Notify.",
        payload=[],
    )
    registry.register_effect_type(effect)

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        registry.register_effect_type(effect)
    assert "duplicate EffectTypeDef api_name: 'SendNotification'" in str(exc_info.value)


def test_validate_rejects_action_with_unregistered_capability() -> None:
    registry = OntologyRegistry()
    registry.register_object_type(_team_type())
    registry.register_action_type(
        ActionTypeDef(
            api_name="DefineGoal",
            display_name="Define Goal",
            target_type="Team",
            executable_by_roles=["TeamOwner"],
            description="Defines a goal.",
            capabilities=["WeatherReader"],
        )
    )

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        registry.validate()
    assert "ActionTypeDef 'DefineGoal': dangling capability 'WeatherReader'" in str(
        exc_info.value
    )


def test_validate_rejects_action_with_unregistered_effect() -> None:
    registry = OntologyRegistry()
    registry.register_object_type(_team_type())
    registry.register_action_type(
        ActionTypeDef(
            api_name="DefineGoal",
            display_name="Define Goal",
            target_type="Team",
            executable_by_roles=["TeamOwner"],
            description="Defines a goal.",
            effects=["SendNotification"],
        )
    )

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        registry.validate()
    assert "ActionTypeDef 'DefineGoal': dangling effect 'SendNotification'" in str(
        exc_info.value
    )


def test_validate_passes_when_action_capability_and_effect_resolve() -> None:
    registry = OntologyRegistry()
    registry.register_object_type(_team_type())
    registry.register_capability(
        CapabilityDef(api_name="WeatherReader", description="Weather.")
    )
    registry.register_effect_type(
        EffectTypeDef(
            api_name="SendNotification",
            description="Notify.",
            payload=[],
        )
    )
    registry.register_action_type(
        ActionTypeDef(
            api_name="DefineGoal",
            display_name="Define Goal",
            target_type="Team",
            executable_by_roles=["TeamOwner"],
            description="Defines a goal.",
            capabilities=["WeatherReader"],
            effects=["SendNotification"],
        )
    )

    registry.validate()


def test_validate_rejects_function_with_unregistered_capability() -> None:
    registry = OntologyRegistry()
    registry.register_function(
        FunctionDef(
            api_name="forecast",
            description="Forecast.",
            input_description="None.",
            output_description="A forecast.",
            capabilities=["WeatherReader"],
        )
    )

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        registry.validate()
    assert "FunctionDef 'forecast': dangling capability 'WeatherReader'" in str(
        exc_info.value
    )


# -- typesys.validate_scalar (spec m35-sdk-refactor §6 AC6) -----------------


@pytest.mark.parametrize(
    "prop_type,value",
    [
        ("str", "hello"),
        ("int", 3),
        ("float", 3.5),
        ("float", 3),  # int accepted where float is declared
        ("bool", True),
        ("bool", False),
        ("date", date(2024, 1, 1)),
        ("date", "2024-01-01"),
        ("datetime", "2024-01-01T00:00:00"),
        ("json", {"a": 1}),
        ("json", [1, 2]),
        ("json", "a string is valid json-typed scalar too"),
        ("json", 1),
        ("json", 1.5),
    ],
)
def test_validate_scalar_happy_path(prop_type: str, value: object) -> None:
    assert validate_scalar(value, prop_type) is None


@pytest.mark.parametrize(
    "prop_type,value",
    [
        ("str", 1),
        ("int", "1"),
        ("int", True),  # bool must not satisfy int despite being a subclass
        ("float", "3.5"),
        ("float", True),
        ("bool", 1),
        ("bool", "true"),
        ("date", "2024/01/01"),
        ("date", datetime(2024, 1, 1, 12, 0)),
        ("date", 20240101),
        ("datetime", 1234),
        ("datetime", "not-a-date"),
        # bool is never accepted for a non-"bool" declared type, even "json"
        # (which otherwise accepts bool's supertype int) -- Python's bool
        # is an int subclass, so this guard must run before the isinstance
        # check.
        ("json", True),
    ],
)
def test_validate_scalar_mismatch(prop_type: str, value: object) -> None:
    error = validate_scalar(value, prop_type)
    assert error is not None
    assert "expected type" in error or "ISO-8601" in error
