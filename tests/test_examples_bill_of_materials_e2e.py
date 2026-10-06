"""Structural guarantees and the initial BOM golden sample through OntologyClient."""

import pytest

from examples.bill_of_materials.fixtures import (
    CYCLIC_SOURCE,
    SOURCE,
    SOURCE_V1,
    SOURCE_V2,
    load_master_data,
    load_world,
)
from examples.bill_of_materials.ontology import (
    BomLine,
    ExplodeBom,
    Part,
    PartsFromSupplier,
    Supplier,
    WhereUsed,
    bom_child,
    bom_parent,
    ontology,
    part_supplier,
)
from ontary import Cardinality, FunctionParams, PreconditionFailed
from ontary.testing import raises_code


def test_given_the_ontology_when_validated_then_it_has_no_diagnostics() -> None:
    ontology.validate()

    assert ontology.diagnose() == []


def test_given_master_data_when_inspecting_types_then_every_type_is_unscoped_and_not_owned() -> (
    None
):
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


@pytest.mark.parametrize("quantity", [0, -1], ids=["zero", "negative"])
def test_given_valid_lines_when_a_nonpositive_quantity_is_ingested_then_only_that_row_is_refused(
    quantity: int,
) -> None:
    world = load_world()
    records = [
        {"id": "valid-before", "quantity": 3},
        {"id": "invalid-quantity", "quantity": quantity},
        {"id": "valid-after", "quantity": 4},
    ]

    report = world.client.ingest("BomLine", records, SOURCE, on_error="report")

    assert not report.ok
    assert len(report.errors) == 1
    error = report.errors[0]
    assert error.index == 1
    assert error.code == "RULE_VIOLATED"
    assert "positive_quantity" in error.reason
    assert world.client.get(BomLine, "invalid-quantity") is None
    for record in (records[0], records[2]):
        line = world.client.get(BomLine, record["id"])
        assert line is not None
        assert line.model_dump() == record


def test_given_source_v1_when_reloading_v2_then_valid_rows_commit_and_resent_links_do_not_duplicate() -> (
    None
):
    world = load_world()
    links = (("bom_parent", bom_parent), ("bom_child", bom_child), ("part_supplier", part_supplier))
    before = {
        (key, from_id): len(world.client.traverse(link, from_id))
        for key, link in links
        for from_id, _ in SOURCE_V1[key]
    }

    reports = load_master_data(world.client, SOURCE_V2)

    assert set(reports) == set(SOURCE_V2)
    assert all(
        report.ok and report.errors == [] for key, report in reports.items() if key != "bom_child"
    )
    assert not reports["bom_child"].ok
    assert len(reports["bom_child"].errors) == 1
    error = reports["bom_child"].errors[0]
    assert error.index == SOURCE_V2["bom_child"].index(("scooter-ghost", "ghost"))
    assert error.code == "LINK_ENDPOINT_NOT_FOUND"
    assert "ghost" in error.reason
    for key, cls in (("suppliers", Supplier), ("parts", Part), ("lines", BomLine)):
        for record in SOURCE_V2[key]:
            obj = world.client.get(cls, record["id"])
            assert obj is not None
            assert obj.model_dump() == record
    for key, link in links:
        for from_id, to_id in SOURCE_V2[key]:
            targets = world.client.traverse(link, from_id)
            assert [target.id for target in targets] == ([] if to_id == "ghost" else [to_id])
        for from_id, _ in SOURCE_V1[key]:
            assert len(world.client.traverse(link, from_id)) == before[key, from_id]

    bike = world.client.call_function(ExplodeBom(part_id="bike"))
    assert bike == {
        "rows": [
            {
                "level": 1,
                "part_id": "bell",
                "part_name": "Bell",
                "quantity_per_parent": 1,
                "total_quantity": 1,
            },
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
                "quantity_per_parent": 36,
                "total_quantity": 72,
            },
        ],
        "cycles": [],
    }
    deck_line = world.client.get(BomLine, "scooter-deck")
    assert deck_line is not None
    assert deck_line.model_dump() == {"id": "scooter-deck", "quantity": 1}
    assert world.client.traverse(bom_child, "scooter-ghost") == []
    scooter = world.client.call_function(ExplodeBom(part_id="scooter"))
    assert scooter == {
        "rows": [
            {
                "level": 1,
                "part_id": "deck",
                "part_name": "Deck",
                "quantity_per_parent": 1,
                "total_quantity": 1,
            },
            *bike["rows"][2:],
        ],
        "cycles": [],
    }


def test_given_a_shared_wheel_when_exploding_scooter_then_its_subtree_appears_under_both_products() -> (
    None
):
    world = load_world()

    scooter = world.client.call_function(ExplodeBom(part_id="scooter"))
    bike = world.client.call_function(ExplodeBom(part_id="bike"))

    subtree = [
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
    ]
    assert scooter == {
        "rows": [
            {
                "level": 1,
                "part_id": "deck",
                "part_name": "Deck",
                "quantity_per_parent": 1,
                "total_quantity": 1,
            },
            *subtree,
        ],
        "cycles": [],
    }
    assert bike["rows"][1:] == subtree
    assert bike["cycles"] == []


def test_given_a_cyclic_source_when_exploding_a_then_the_cycle_is_data_and_other_branches_are_walked() -> (
    None
):
    world = load_world(CYCLIC_SOURCE)
    assert all(report.ok for report in world.reports.values())

    result = world.client.call_function(ExplodeBom(part_id="a"))

    assert result == {
        "rows": [
            {
                "level": 1,
                "part_id": "b",
                "part_name": "B",
                "quantity_per_parent": 1,
                "total_quantity": 1,
            },
            {
                "level": 2,
                "part_id": "c",
                "part_name": "C",
                "quantity_per_parent": 1,
                "total_quantity": 1,
            },
            {
                "level": 1,
                "part_id": "d",
                "part_name": "D",
                "quantity_per_parent": 1,
                "total_quantity": 1,
            },
        ],
        "cycles": [["a", "b", "c", "a"]],
    }


@pytest.mark.parametrize(
    ("part_id", "expected"),
    [
        (
            "bearing",
            [
                (1, "hub", "Hub", 2),
                (2, "wheel", "Wheel", 1),
                (3, "bike", "Bike", 2),
                (3, "scooter", "Scooter", 2),
            ],
        ),
        (
            "spoke",
            [(1, "wheel", "Wheel", 32), (2, "bike", "Bike", 2), (2, "scooter", "Scooter", 2)],
        ),
        ("bike", []),
    ],
)
def test_given_source_v1_when_where_used_is_called_then_ancestors_are_depth_first_by_part_id(
    part_id: str,
    expected: list[tuple[int, str, str, int]],
) -> None:
    world = load_world()

    result = world.client.call_function(WhereUsed(part_id=part_id))

    assert result == {
        "rows": [
            {"level": level, "part_id": ancestor_id, "part_name": name, "quantity": quantity}
            for level, ancestor_id, name, quantity in expected
        ],
        "cycles": [],
    }


@pytest.mark.parametrize(
    ("supplier_id", "expected"),
    [
        ("acme", [("deck", "Deck"), ("frame", "Frame"), ("spoke", "Spoke")]),
        ("boltco", [("bearing", "Bearing"), ("hub", "Hub")]),
    ],
)
def test_given_source_v1_when_parts_from_supplier_is_called_then_parts_are_sorted_by_id(
    supplier_id: str,
    expected: list[tuple[str, str]],
) -> None:
    world = load_world()

    result = world.client.call_function(PartsFromSupplier(supplier_id=supplier_id))

    assert result == [{"part_id": part_id, "part_name": name} for part_id, name in expected]


@pytest.mark.parametrize(
    "params",
    [
        ExplodeBom(part_id="unknown-part"),
        WhereUsed(part_id="unknown-part"),
        PartsFromSupplier(supplier_id="unknown-supplier"),
    ],
    ids=["explode-bom", "where-used", "parts-from-supplier"],
)
def test_given_an_unknown_id_when_a_function_is_called_then_the_refusal_names_the_id(
    params: FunctionParams,
) -> None:
    world = load_world()
    unknown_id = next(iter(params.model_dump().values()))

    with raises_code("PRECONDITION_FAILED"):
        try:
            world.client.call_function(params)
        except PreconditionFailed as error:
            assert unknown_id in str(error)
            raise


def test_given_a_leaf_when_exploding_spoke_then_rows_and_cycles_are_empty() -> None:
    world = load_world()

    result = world.client.call_function(ExplodeBom(part_id="spoke"))

    assert result == {"rows": [], "cycles": []}
