"""Tests for ontary.connect.mapping: declarative canonical -> ontology
bindings (spec connector-framework §5 mapping.py, §3 AC7, §7 edge cases)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from ontary.connect import (
    CanonicalRecord,
    LinkBinding,
    MappingSpec,
    MappingValidationError,
    ObjectBinding,
    SourceLineage,
)
from ontary.meta import (
    Cardinality,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
)


class Widget(CanonicalRecord):
    widget_key: str
    label: str


class Gadget(CanonicalRecord):
    gadget_key: str
    widget_key: str


def make_lineage() -> SourceLineage:
    return SourceLineage(source_system="acme", extracted_at=datetime(2026, 1, 1))


def toy_registry() -> OntologyRegistry:
    registry = OntologyRegistry()
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Widget",
            display_name="Widget",
            description="A widget",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="name", type="str"),
            ],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Gadget",
            display_name="Gadget",
            description="A gadget",
            layer="L0",
            properties=[PropertyDef(name="id", type="str")],
            primary_key="id",
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="gadgetOfWidget",
            from_type="Gadget",
            to_type="Widget",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Gadget belongs to Widget",
        )
    )
    return registry


def widget_binding(**overrides: Any) -> ObjectBinding:
    defaults: dict[str, Any] = dict(
        entity="widgets",
        object_type="Widget",
        key_field="widget_key",
        property_map={"label": "name"},
        record_model=Widget,
    )
    defaults.update(overrides)
    return ObjectBinding(**defaults)


def gadget_binding(**overrides: Any) -> ObjectBinding:
    defaults: dict[str, Any] = dict(
        entity="gadgets",
        object_type="Gadget",
        key_field="gadget_key",
        property_map={},
        record_model=Gadget,
    )
    defaults.update(overrides)
    return ObjectBinding(**defaults)


def test_valid_mapping_spec_validates_cleanly() -> None:
    spec = MappingSpec(
        object_bindings=[widget_binding(), gadget_binding()],
        link_bindings=[
            LinkBinding(
                link_type="gadgetOfWidget",
                from_entity="gadgets",
                from_key_field="gadget_key",
                to_entity="widgets",
                to_key_field="widget_key",
            )
        ],
    )
    spec.validate(toy_registry())  # must not raise


def test_undeclared_object_type_named_in_error() -> None:
    spec = MappingSpec(object_bindings=[widget_binding(object_type="Nope")])
    with pytest.raises(MappingValidationError) as exc:
        spec.validate(toy_registry())
    message = str(exc.value)
    assert "widgets" in message
    assert "Nope" in message


def test_undeclared_link_type_named_in_error() -> None:
    spec = MappingSpec(
        object_bindings=[widget_binding(), gadget_binding()],
        link_bindings=[
            LinkBinding(
                link_type="noSuchLink",
                from_entity="gadgets",
                from_key_field="gadget_key",
                to_entity="widgets",
                to_key_field="widget_key",
            )
        ],
    )
    with pytest.raises(MappingValidationError) as exc:
        spec.validate(toy_registry())
    message = str(exc.value)
    assert "noSuchLink" in message


def test_unknown_ontology_property_named_in_error() -> None:
    spec = MappingSpec(
        object_bindings=[widget_binding(property_map={"label": "not_a_property"})]
    )
    with pytest.raises(MappingValidationError) as exc:
        spec.validate(toy_registry())
    message = str(exc.value)
    assert "widgets" in message
    assert "not_a_property" in message


def test_canonical_field_absent_from_record_model_named_in_error() -> None:
    spec = MappingSpec(
        object_bindings=[widget_binding(property_map={"missing_field": "name"})]
    )
    with pytest.raises(MappingValidationError) as exc:
        spec.validate(toy_registry())
    message = str(exc.value)
    assert "widgets" in message
    assert "missing_field" in message


def test_key_field_absent_from_record_model_named_in_error() -> None:
    spec = MappingSpec(object_bindings=[widget_binding(key_field="no_such_key")])
    with pytest.raises(MappingValidationError) as exc:
        spec.validate(toy_registry())
    message = str(exc.value)
    assert "no_such_key" in message


def test_duplicate_entity_binding_named_in_error() -> None:
    spec = MappingSpec(object_bindings=[widget_binding(), widget_binding()])
    with pytest.raises(MappingValidationError) as exc:
        spec.validate(toy_registry())
    message = str(exc.value)
    assert "widgets" in message
    assert "bound more than once" in message


def test_link_binding_from_key_field_absent_from_record_model_named_in_error() -> None:
    spec = MappingSpec(
        object_bindings=[widget_binding(), gadget_binding()],
        link_bindings=[
            LinkBinding(
                link_type="gadgetOfWidget",
                from_entity="gadgets",
                from_key_field="no_such_field",
                to_entity="widgets",
                to_key_field="widget_key",
            )
        ],
    )
    with pytest.raises(MappingValidationError) as exc:
        spec.validate(toy_registry())
    message = str(exc.value)
    assert "no_such_field" in message
    assert "gadgetOfWidget" in message


def test_link_binding_to_key_field_absent_from_record_model_named_in_error() -> None:
    """`to_key_field="no_such_field"` is absent from BOTH sides' models here
    (it only has to be absent from the FROM-side one -- see
    `test_link_binding_to_key_field_checked_against_from_side_record_model`
    below for the asymmetric case that actually distinguishes which side
    validate() checks)."""
    spec = MappingSpec(
        object_bindings=[widget_binding(), gadget_binding()],
        link_bindings=[
            LinkBinding(
                link_type="gadgetOfWidget",
                from_entity="gadgets",
                from_key_field="gadget_key",
                to_entity="widgets",
                to_key_field="no_such_field",
            )
        ],
    )
    with pytest.raises(MappingValidationError) as exc:
        spec.validate(toy_registry())
    message = str(exc.value)
    assert "no_such_field" in message
    assert "gadgetOfWidget" in message


def test_link_binding_to_key_field_checked_against_from_side_record_model() -> None:
    """Regression test for a reviewer finding (T6 attempt 2): the mapper
    (`mapper.py`'s `_map_links`) resolves BOTH `from_key_field` and
    `to_key_field` off the FROM-side canonical record -- `to_key_field`'s
    value is only ever looked up against the TO-side `ObjectBinding`'s ids,
    never read off a to-side record. `validate()` used to check
    `to_key_field` against the TO-side binding's `record_model`, which could
    wrongly accept a field that only exists there (never checked against the
    from-side model where it is actually read) and wrongly reject a field
    that only exists on the from-side model.

    `label` exists on `Widget` (the to-side model) but NOT on `Gadget` (the
    from-side model) -- a `to_key_field="label"` binding must be rejected,
    because at run time `Gadget.model_dump().get("label")` is always `None`.
    """
    spec = MappingSpec(
        object_bindings=[widget_binding(), gadget_binding()],
        link_bindings=[
            LinkBinding(
                link_type="gadgetOfWidget",
                from_entity="gadgets",
                from_key_field="gadget_key",
                to_entity="widgets",
                to_key_field="label",
            )
        ],
    )
    with pytest.raises(MappingValidationError) as exc:
        spec.validate(toy_registry())
    message = str(exc.value)
    assert "label" in message
    assert "gadgetOfWidget" in message
    assert "Gadget" in message


def test_link_binding_unbound_entity_named_in_error() -> None:
    spec = MappingSpec(
        object_bindings=[widget_binding()],
        link_bindings=[
            LinkBinding(
                link_type="gadgetOfWidget",
                from_entity="gadgets",  # never bound by an ObjectBinding
                from_key_field="gadget_key",
                to_entity="widgets",
                to_key_field="widget_key",
            )
        ],
    )
    with pytest.raises(MappingValidationError) as exc:
        spec.validate(toy_registry())
    message = str(exc.value)
    assert "gadgets" in message


def test_link_binding_endpoint_types_inconsistent_with_registry() -> None:
    """The link_type declares Gadget -> Widget; binding gadgets' entity to
    the Widget object type (an author mistake) must be rejected."""
    spec = MappingSpec(
        object_bindings=[
            widget_binding(),
            gadget_binding(object_type="Widget", entity="gadgets"),
        ],
        link_bindings=[
            LinkBinding(
                link_type="gadgetOfWidget",
                from_entity="gadgets",
                from_key_field="gadget_key",
                to_entity="widgets",
                to_key_field="widget_key",
            )
        ],
    )
    with pytest.raises(MappingValidationError) as exc:
        spec.validate(toy_registry())
    message = str(exc.value)
    assert "gadgets" in message
    assert "gadgetOfWidget" in message


def test_transform_escape_hatch_is_typed_and_callable() -> None:
    def _transform(record: CanonicalRecord) -> dict[str, Any]:
        assert isinstance(record, Widget)
        return {"name": record.label.upper()}

    binding = widget_binding(transform=_transform)
    widget = Widget(widget_key="w1", label="lamp", lineage=make_lineage())
    result = binding.transform
    assert result is not None
    assert result(widget) == {"name": "LAMP"}


def test_mapping_spec_validate_collects_multiple_errors() -> None:
    spec = MappingSpec(
        object_bindings=[
            widget_binding(object_type="Nope"),
            gadget_binding(property_map={"missing": "id"}),
        ]
    )
    with pytest.raises(MappingValidationError) as exc:
        spec.validate(toy_registry())
    message = str(exc.value)
    assert "Nope" in message
    assert "missing" in message
