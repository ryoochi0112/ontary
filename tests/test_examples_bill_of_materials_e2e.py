"""Structural guarantees and the initial BOM golden sample through OntologyClient."""

from examples.bill_of_materials.fixtures import SOURCE_V1, load_world
from examples.bill_of_materials.ontology import (
    BomLine,
    ExplodeBom,
    Part,
    Supplier,
    bom_child,
    bom_parent,
    ontology,
    part_supplier,
)
from ontary import Cardinality


def test_given_the_ontology_when_validated_then_it_has_no_diagnostics() -> None:
    ontology.validate()

    assert ontology.diagnose() == []


def test_given_master_data_when_inspecting_types_then_every_type_is_unscoped_and_not_owned() -> None:
    assert ontology.definition.policy.unscoped_types == {"Supplier", "Part", "BomLine"}
    for cls in (Supplier, Part, BomLine):
        assert ontology.registry.get_object_type(cls.__name__).owned is False


def test_given_the_ontology_when_inspecting_writers_then_no_actions_are_registered() -> None:
    assert ontology.registry.action_types == {}


def test_given_source_v1_when_loading_world_then_every_batch_reports_no_errors() -> None:
    world = load_world()

    assert set(world.reports) == set(SOURCE_V1)
    assert all(report.ok and report.errors == [] for report in world.reports.values())


def test_given_source_v1_when_exploding_bike_then_rows_match_the_golden_sample() -> None:
    world = load_world()

    result = world.client.call_function(ExplodeBom(part_id="bike"))

    assert result == {
        "rows": [
            {
                "level": 1,
                "part_id": "frame",
                "part_name": "Frame",
                "quantity_per_parent": 1,
                "total_quantity": 1,
            },
            {
                "level": 1,
                "part_id": "wheel",
                "part_name": "Wheel",
                "quantity_per_parent": 2,
                "total_quantity": 2,
            },
            {
                "level": 2,
                "part_id": "hub",
                "part_name": "Hub",
                "quantity_per_parent": 1,
                "total_quantity": 2,
            },
            {
                "level": 3,
                "part_id": "bearing",
                "part_name": "Bearing",
                "quantity_per_parent": 2,
                "total_quantity": 4,
            },
            {
                "level": 2,
                "part_id": "spoke",
                "part_name": "Spoke",
                "quantity_per_parent": 32,
                "total_quantity": 64,
            },
        ],
        "cycles": [],
    }


def test_given_parts_when_inspecting_properties_then_quantities_belong_only_to_lines() -> None:
    assert set(Part.model_fields) == {"id", "name"}
    assert set(BomLine.model_fields) == {"id", "quantity"}
    assert BomLine.model_fields["quantity"].annotation is int
    line_type = ontology.registry.get_object_type("BomLine")
    assert [rule.name for rule in line_type.rules] == ["positive_quantity"]


def test_given_source_lines_when_traversing_then_each_has_one_parent_and_child() -> None:
    world = load_world()

    for line in world.client.list(BomLine, limit=None):
        assert len(world.client.traverse(bom_parent, line.id)) == 1
        assert len(world.client.traverse(bom_child, line.id)) == 1
    for name in ("bom_parent", "bom_child", "part_supplier"):
        link = ontology.registry.get_link_type(name)
        assert link.cardinality == Cardinality.MANY_TO_ONE
        assert link.owned is False
    for part_id, supplier_id in SOURCE_V1["part_supplier"]:
        assert [supplier.id for supplier in world.client.traverse(part_supplier, part_id)] == [
            supplier_id
        ]
