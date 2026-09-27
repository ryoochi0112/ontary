"""Declaration and scalar-helper contract for flat structured values."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

import pytest
from conftest import raises_code
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_serializer,
    model_serializer,
)

from ontary.errors import ValidationFailed
from ontary.meta import ActionParameterDef, PropertyDef, StructFieldDef
from ontary.typesys import (
    _storage_scalar_violation,
    _to_storage_scalar,
    struct_value,
    validate_scalar,
)


class Currency(Enum):
    JPY = "JPY"


class Money(BaseModel):
    value: float
    currency: Currency


FIELDS = (
    StructFieldDef(name="value", type="float"),
    StructFieldDef(name="currency", type="str", choices=("JPY", "USD")),
    StructFieldDef(name="note", type="str", required=False),
)


@pytest.mark.parametrize("descriptor", [PropertyDef, ActionParameterDef])
@pytest.mark.parametrize(
    "declared_type,fields,choices,fragment",
    [
        ("struct", None, None, "fields"),
        ("struct", (), None, "fields"),
        ("str", FIELDS, None, "fields"),
        ("struct", (FIELDS[0], FIELDS[0]), None, "duplicate"),
        ("struct", FIELDS, ("JPY",), "choices"),
    ],
)
def test_descriptor_refuses_invalid_field_declaration(
    descriptor: type[PropertyDef] | type[ActionParameterDef],
    declared_type: str,
    fields: tuple[StructFieldDef, ...] | None,
    choices: tuple[str, ...] | None,
    fragment: str,
) -> None:
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        descriptor.model_validate(
            {"name": "amount", "type": declared_type, "fields": fields, "choices": choices}
        )
    assert fragment in str(excinfo.value)


@pytest.mark.parametrize("invalid_type", ["json", "struct"])
def test_struct_field_refuses_non_scalar_type(invalid_type: str) -> None:
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        StructFieldDef.model_validate({"name": "nested", "type": invalid_type})
    assert "nested" in str(excinfo.value)


@pytest.mark.parametrize(
    "choices,fragment",
    [
        ((), "empty"),
        (("JPY", "JPY"), "duplicate"),
        (("JPY", 1), "non-string"),
    ],
)
def test_struct_field_reuses_choice_declaration_rules(
    choices: tuple[object, ...], fragment: str
) -> None:
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        StructFieldDef.model_validate({"name": "currency", "type": "str", "choices": choices})
    assert fragment in str(excinfo.value)


def test_valid_descriptors_preserve_inner_field_metadata() -> None:
    for descriptor in (PropertyDef, ActionParameterDef):
        declared = descriptor.model_validate(
            {"name": "amount", "type": "struct", "fields": FIELDS}
        )
        assert declared.fields == FIELDS
    assert FIELDS[2].required is False
    assert PropertyDef(name="id", type="str").fields is None


@pytest.mark.parametrize(
    "value,inner_name,fragment",
    [
        ({"value": 1}, "currency", "required"),
        ({"value": "wrong", "currency": "JPY"}, "value", "expected type"),
        ({"value": 1, "currency": "EUR"}, "currency", "expected one of"),
        ({"value": 1, "currency": "JPY", "extra": 1}, "extra", "unknown"),
        ("100 JPY", "amount", "expected"),
        ({"value": 1, "currency": None}, "currency", "required"),
    ],
)
def test_struct_value_violations_name_inner_field(
    value: Any, inner_name: str, fragment: str
) -> None:
    violation = validate_scalar(value, "struct", fields=FIELDS)
    assert violation is not None
    assert inner_name in f"amount.{violation}"
    assert fragment in violation


def test_struct_fails_closed_without_fields() -> None:
    assert validate_scalar({"value": 1}, "struct") is not None
    assert _storage_scalar_violation({"value": 1}, "struct") is not None
    with pytest.raises(ValueError, match="fields declaration"):
        _to_storage_scalar({"value": 1}, "struct")


def test_struct_normalizer_matches_model_and_dict_and_unwraps_inner_enum() -> None:
    model = Money(value=100, currency=Currency.JPY)
    raw = {"value": 100, "currency": Currency.JPY}
    assert struct_value(model, FIELDS) == struct_value(raw, FIELDS)
    assert struct_value(raw, FIELDS) == {"value": 100, "currency": "JPY", "note": None}
    assert struct_value("100 JPY", FIELDS) == "100 JPY"
    assert validate_scalar(struct_value(raw, FIELDS), "struct", fields=FIELDS) is None


def test_struct_normalizer_fills_only_absent_optional_fields() -> None:
    assert struct_value({"value": 1, "currency": "JPY", "extra": 7}, FIELDS) == {
        "value": 1, "currency": "JPY", "note": None, "extra": 7,
    }
    assert struct_value({"value": 1}, FIELDS) == {"value": 1, "note": None}
    assert validate_scalar(struct_value({"value": 1}, FIELDS), "struct", fields=FIELDS) == (
        "currency: required field missing"
    )


def test_struct_storage_converts_inner_date_and_datetime() -> None:
    fields = (
        StructFieldDef(name="day", type="date"),
        StructFieldDef(name="instant", type="datetime"),
    )
    value = {"day": date(2026, 9, 27), "instant": datetime(2026, 9, 27, tzinfo=timezone.utc)}
    stored = _to_storage_scalar(value, "struct", fields=fields)
    assert stored == {"day": "2026-09-27", "instant": "2026-09-27T00:00:00+00:00"}
    assert _storage_scalar_violation(stored, "struct", fields=fields) is None
    violation = _storage_scalar_violation({"day": date(2026, 9, 27), "instant": stored["instant"]}, "struct", fields=fields)
    assert violation is not None and "day" in violation


def test_json_and_existing_scalar_calls_keep_their_contract() -> None:
    assert validate_scalar({"a": 1}, "json") is None
    assert validate_scalar(Money(value=1, currency=Currency.JPY), "json") is not None
    assert validate_scalar(True, "int") == "expected type 'int', got bool"
    assert validate_scalar("JPY", "str", ("JPY", "USD")) is None
    assert _to_storage_scalar(date(2026, 9, 27), "date") == "2026-09-27"


def test_non_struct_type_ignores_passed_fields() -> None:
    fields = (StructFieldDef(name="day", type="date"),)
    value = {"day": date(2026, 9, 27), "extra": 1}
    assert validate_scalar(value, "json", fields=fields) == validate_scalar(value, "json")
    assert _storage_scalar_violation(value, "json", fields=fields) == _storage_scalar_violation(
        value, "json"
    )
    assert _to_storage_scalar(value, "json", fields=fields) == _to_storage_scalar(value, "json")


def test_struct_normalizer_reads_declared_attributes_and_preserves_extras() -> None:
    class Special(BaseModel):
        model_config = ConfigDict(extra="allow")
        a: int = Field(exclude=True)
        b: str

        @field_serializer("b")
        def serialize_b(self, value: str) -> str:
            return value.upper()

        @computed_field  # type: ignore[prop-decorator]
        @property
        def derived(self) -> str:
            return "computed"

        @model_serializer(mode="wrap")
        def serialize_model(self, handler: Any) -> Any:
            return {"replacement": handler(self)}

    fields = (StructFieldDef(name="a", type="int"), StructFieldDef(name="b", type="str"))
    instance = Special.model_validate({"a": 5, "b": "raw", "extra": 7})
    assert struct_value(instance, fields) == {"a": 5, "b": "raw", "extra": 7}
    assert validate_scalar(struct_value(instance, fields), "struct", fields=fields) == (
        "extra: unknown field"
    )
