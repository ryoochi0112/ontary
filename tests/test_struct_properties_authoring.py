"""Declaration of flat structured properties and action parameters."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

import pytest
from conftest import raises_code
from pydantic import BaseModel, Field

from ontary import ActionContext, ActionParams, Ontology, OntologyObject, prop
from ontary.errors import ValidationFailed
from ontary.meta import StructFieldDef


class Currency(Enum):
    JPY = "JPY"
    USD = "USD"


class Money(BaseModel):
    value: float
    currency: Currency
    kind: Literal["gross", "net"]
    count: int | None = None
    active: bool = True
    booked: date | None = None
    recorded: datetime | None = None


EXPECTED_FIELDS = (
    StructFieldDef(name="value", type="float"),
    StructFieldDef(name="currency", type="str", choices=("JPY", "USD")),
    StructFieldDef(name="kind", type="str", choices=("gross", "net")),
    StructFieldDef(name="count", type="int", required=False),
    StructFieldDef(name="active", type="bool"),
    StructFieldDef(name="booked", type="date", required=False),
    StructFieldDef(name="recorded", type="datetime", required=False),
)


def _local() -> Ontology:
    return Ontology("struct-authoring", scope_levels=["org"], min_n=1)


def _register_property(annotation: Any, *, marker: Any = None) -> None:
    local = _local()
    namespace: dict[str, Any] = {
        "__annotations__": {"id": str, "amount": annotation},
        "id": prop(primary_key=True),
    }
    if marker is not None:
        namespace["amount"] = marker
    thing = type("Thing", (OntologyObject,), namespace)
    local.object(layer="L0", scope="unscoped")(thing)


def test_struct_and_optional_struct_derive_all_inner_fields() -> None:
    local = _local()

    @local.object(layer="L0", scope="unscoped")
    class Order(OntologyObject):
        id: str = prop(primary_key=True)
        amount: Money
        discount: Money | None

    props = {p.name: p for p in local.registry.get_object_type("Order").properties}
    assert props["amount"].type == "struct"
    assert props["amount"].fields == EXPECTED_FIELDS
    assert props["amount"].required is True
    assert props["discount"].type == "struct"
    assert props["discount"].fields == EXPECTED_FIELDS
    assert props["discount"].required is False
    assert props["id"].type == "str"
    assert props["id"].fields is None


def test_action_params_struct_derives_the_same_declaration() -> None:
    local = _local()

    @local.object(layer="L0", scope="unscoped")
    class Order(OntologyObject):
        id: str = prop(primary_key=True)

    class ChargeParams(ActionParams):
        amount: Money
        optional_amount: Money | None

    @local.action(ChargeParams, target=Order, roles=["Clerk"], api_name="Charge")
    def _charge(ctx: ActionContext, params: ChargeParams) -> dict[str, Any]:
        return {}

    params = {p.name: p for p in local.registry.get_action_type("Charge").parameters}
    assert params["amount"].type == "struct"
    assert params["amount"].fields == EXPECTED_FIELDS
    assert params["amount"].required is True
    assert params["optional_amount"].type == "struct"
    assert params["optional_amount"].fields == EXPECTED_FIELDS
    assert params["optional_amount"].required is False


@pytest.mark.parametrize(
    ("annotation", "fix"),
    [
        (list[Money], "use a linked object type"),
        (dict[str, Money], "use a linked object type"),
        (list[dict[str, Money]], "use a linked object type"),
        (Money, "use a string primary key"),
    ],
)
def test_invalid_top_level_struct_shapes_are_refused(annotation: Any, fix: str) -> None:
    if annotation is Money:
        local = _local()
        thing = type(
            "Thing",
            (OntologyObject,),
            {"__annotations__": {"id": Money}, "id": prop(primary_key=True)},
        )
        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
            local.object(layer="L0", scope="unscoped")(thing)
        assert "Thing.id" in str(excinfo.value)
    else:
        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
            _register_property(annotation)
        assert "Thing.amount" in str(excinfo.value)
    assert fix in str(excinfo.value)


def test_nested_struct_is_refused_with_flatten_fix() -> None:
    class Wrapper(BaseModel):
        amount: Money

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        _register_property(Wrapper)
    assert "Thing.amount.amount" in str(excinfo.value)
    assert "flatten it, or use a linked object type" in str(excinfo.value)


@pytest.mark.parametrize("annotation", [list[Money], dict[str, Money]])
def test_collection_of_struct_inside_struct_is_refused(annotation: Any) -> None:
    wrapper = type("Wrapper", (BaseModel,), {"__annotations__": {"items": annotation}})
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        _register_property(wrapper)
    assert "Thing.amount.items" in str(excinfo.value)
    assert "use a linked object type" in str(excinfo.value)


def test_ontology_object_as_struct_is_refused_with_link_fix() -> None:
    class Other(OntologyObject):
        id: str

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        _register_property(Other)
    assert "Thing.amount" in str(excinfo.value)
    assert "use a link" in str(excinfo.value)


def test_ontology_object_as_inner_field_is_refused_with_link_fix() -> None:
    class Other(OntologyObject):
        id: str

    class Wrapper(BaseModel):
        other: Other

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        _register_property(Wrapper)
    assert "Thing.amount.other" in str(excinfo.value)
    assert "use a link" in str(excinfo.value)


@pytest.mark.parametrize("annotation", [Any, dict, dict[str, int], list, list[str]])
def test_json_like_inner_fields_are_refused(annotation: Any) -> None:
    wrapper = type("Wrapper", (BaseModel,), {"__annotations__": {"item": annotation}})
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        _register_property(wrapper)
    assert "Thing.amount.item" in str(excinfo.value)
    assert "use a scalar field" in str(excinfo.value)


def test_aliased_inner_field_is_refused() -> None:
    class Aliased(BaseModel):
        value: float = Field(alias="v")

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        _register_property(Aliased)
    assert "Thing.amount.value" in str(excinfo.value)
    assert "use the field name" in str(excinfo.value)


def test_validation_aliased_inner_field_is_refused() -> None:
    class Aliased(BaseModel):
        value: float = Field(validation_alias="v")

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        _register_property(Aliased)
    assert "Thing.amount.value" in str(excinfo.value)
    assert "use the field name" in str(excinfo.value)


def test_serialization_aliased_inner_field_is_refused() -> None:
    class Aliased(BaseModel):
        value: float = Field(serialization_alias="v")

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        _register_property(Aliased)
    assert "Thing.amount.value" in str(excinfo.value)
    assert "use the field name" in str(excinfo.value)


def test_inner_prop_metadata_is_refused() -> None:
    class Marked(BaseModel):
        value: float = prop(sensitivity=None)

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        _register_property(Marked)
    assert "Thing.amount.value" in str(excinfo.value)
    assert "mark the whole property" in str(excinfo.value)


def test_choices_on_struct_property_are_refused() -> None:
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        _register_property(Money, marker=prop(choices=["JPY"]))
    assert "Thing.amount" in str(excinfo.value)
    assert "drop prop(choices=...)" in str(excinfo.value)


def test_collection_of_struct_cannot_be_hidden_by_json_override() -> None:
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        _register_property(list[Money], marker=prop(property_type="json"))
    assert "use a linked object type" in str(excinfo.value)


def test_choices_on_struct_action_param_are_refused() -> None:
    local = _local()

    @local.object(layer="L0", scope="unscoped")
    class Order(OntologyObject):
        id: str = prop(primary_key=True)

    class BadParams(ActionParams):
        amount: Money = prop(choices=["JPY"])

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:

        @local.action(BadParams, target=Order, roles=["Clerk"], api_name="Bad")
        def _bad(ctx: ActionContext, params: BadParams) -> dict[str, Any]:
            return {}

    assert "BadParams.amount" in str(excinfo.value)
    assert "drop prop(choices=...)" in str(excinfo.value)


def test_non_struct_annotations_keep_their_existing_declarations() -> None:
    local = _local()

    @local.object(layer="L0", scope="unscoped")
    class Existing(OntologyObject):
        id: str = prop(primary_key=True)
        count: int
        optional: str | None
        currency: Currency
        kind: Literal["gross", "net"]
        items: list[str]
        mapping: dict[str, int]

    props = {p.name: p for p in local.registry.get_object_type("Existing").properties}
    assert {name: (p.type, p.choices, p.fields, p.required) for name, p in props.items()} == {
        "id": ("str", None, None, True),
        "count": ("int", None, None, True),
        "optional": ("str", None, None, False),
        "currency": ("str", ("JPY", "USD"), None, True),
        "kind": ("str", ("gross", "net"), None, True),
        "items": ("json", None, None, True),
        "mapping": ("json", None, None, True),
    }
