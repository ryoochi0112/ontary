# Bill of materials reference app

`examples/bill_of_materials/` is the fourth in-repo reference app for `ontary`.
It models a bill of materials (BOM): which parts make up a product, and how many of each.
Read it after the [room booking app](../room_booking/README.md) when you want to see master data loaded in bulk and a hierarchy walked through links.

## Domain map

- **Supplier** has a `name`. It supplies parts.
- **Part** has a `name`. A part is a component or an assembly. An assembly has no supplier.
- **BomLine** holds one `quantity`. It says that one parent part uses this many of one child part.

Three links connect them:

- `bom_parent` links a BomLine to the Part that is the parent (many to one).
- `bom_child` links a BomLine to the Part that is the child (many to one).
- `part_supplier` links a Part to its Supplier (many to one).

Part has no `children`, `parent`, or `quantity` property.
Each fact is stored once, on a BomLine or on a link.
Three Functions derive every answer: `explode_bom`, `where_used`, and `parts_from_supplier`.

The ontology declares one scope level, `plant`, but no type uses it.
Every type is `scope="unscoped"`, so every consumer reads all rows.
`ontary` requires at least one scope level, so the example declares one and leaves it unused.
See [`ontology.py`](ontology.py) for the full declaration.

## Which feature enforces which guarantee

| Guarantee | Engine feature | Where |
| --- | --- | --- |
| A line quantity is positive. | `@ontology.rule` | `positive_quantity` in `ontology.py` |
| Each load records where its data came from. | `Source` (lineage) | `SOURCE` in `fixtures.py` |
| One bad row does not stop a load. | `on_error="report"` | `load_master_data` in `fixtures.py` |
| A cycle in the data does not loop forever. | Path guard in the Function | `explode_bom` in `ontology.py` |
| An unknown id is refused, not answered with nothing. | `PreconditionFailed` | `_require_part` in `ontology.py` |

The first block loads the world and explodes the bike.
`load_world()` returns the ontology, the store, a planner client, and one report per batch.

```python
from examples.bill_of_materials.fixtures import load_world
from examples.bill_of_materials.ontology import ExplodeBom, PartsFromSupplier, WhereUsed

world = load_world()
client = world.client

bike = client.call_function(ExplodeBom(part_id="bike"))
assert bike["cycles"] == []
assert bike["rows"] == [
    {"level": 1, "part_id": "frame", "part_name": "Frame", "quantity_per_parent": 1, "total_quantity": 1},
    {"level": 1, "part_id": "wheel", "part_name": "Wheel", "quantity_per_parent": 2, "total_quantity": 2},
    {"level": 2, "part_id": "hub", "part_name": "Hub", "quantity_per_parent": 1, "total_quantity": 2},
    {"level": 3, "part_id": "bearing", "part_name": "Bearing", "quantity_per_parent": 2, "total_quantity": 4},
    {"level": 2, "part_id": "spoke", "part_name": "Spoke", "quantity_per_parent": 32, "total_quantity": 64},
]
```

The walk is depth first, and siblings are sorted by `part_id`.
`total_quantity` is the parent total times the line quantity.
A bike has 2 wheels, each wheel has 2 bearings through its hub, so the bike has 4 bearings.

## Why the quantity lives on a BomLine object

A BOM quantity belongs to the pair of parts, not to one part.
A wheel is used 2 times in a bike and 2 times in a scooter, and a spoke is used 32 times in a wheel.
A `quantity` property on Part would have to hold one number for every parent.

A link has no properties.
So the relationship becomes an object type, `BomLine`, that holds the quantity.
It links to its parent and to its child with `bom_parent` and `bom_child`.
The design guide describes this pattern in [Links and object-backed link types](../../docs/ontology-design.md#links-and-object-backed-link-types).

## Loading through ingest, objects first

`load_master_data` in [`fixtures.py`](fixtures.py) is the one loading path.
It calls `client.ingest` for suppliers, parts, and lines.
Then it calls `client.ingest_links` for `bom_parent`, `bom_child`, and `part_supplier`.
Objects go first because a link needs both of its ends to exist.

Every call passes the same `Source(source_system="erp")`, so each row keeps its lineage.
`Source` records where the data came from.
It does not limit which rows a traversal can reach.
The design guide explains why a source system should not shape the domain model in [System Silos](../../docs/ontology-design.md#system-silos).

Every call also passes `on_error="report"`.
The call then returns an `IngestReport` instead of raising.
The next block checks that every batch of the first load is clean, and that a refused row is named in its report.

```python
from examples.bill_of_materials.fixtures import SOURCE
from examples.bill_of_materials.ontology import BomLine

assert sorted(world.reports) == [
    "bom_child",
    "bom_parent",
    "lines",
    "part_supplier",
    "parts",
    "suppliers",
]
assert all(report.ok and report.errors == [] for report in world.reports.values())

refused = client.ingest(
    "BomLine",
    [
        {"id": "ok-before", "quantity": 3},
        {"id": "zero-quantity", "quantity": 0},
        {"id": "ok-after", "quantity": 4},
    ],
    SOURCE,
    on_error="report",
)
assert len(refused.errors) == 1
assert refused.errors[0].index == 1
assert refused.errors[0].code == "RULE_VIOLATED"
assert "positive_quantity" in refused.errors[0].reason
assert client.get(BomLine, "zero-quantity") is None
assert client.get(BomLine, "ok-before").quantity == 3
assert client.get(BomLine, "ok-after").quantity == 4
```

## A batch is not atomic

A batch commits every valid row, and its report names each bad row.
The batch does not roll back.
The block above shows it for a rule: the zero quantity is refused, and the two lines around it are stored.

`SOURCE_V2` shows it for a link.
It is a later version of the source, loaded on top of `SOURCE_V1`.
It adds a line, `scooter-ghost`, whose child part `ghost` does not exist.
The `bom_child` batch reports one `LINK_ENDPOINT_NOT_FOUND` error for that pair.
All other pairs in the batch are linked, and every other batch is clean.
The line object `scooter-ghost` exists but has no child, so `explode_bom` skips it.

```python
from examples.bill_of_materials.fixtures import SOURCE_V1, SOURCE_V2, load_master_data

stage = load_world(SOURCE_V1)
reports = load_master_data(stage.client, SOURCE_V2)

assert len(reports["bom_child"].errors) == 1
error = reports["bom_child"].errors[0]
assert error.index == SOURCE_V2["bom_child"].index(("scooter-ghost", "ghost"))
assert error.code == "LINK_ENDPOINT_NOT_FOUND"
assert "ghost" in error.reason
assert all(report.ok for key, report in reports.items() if key != "bom_child")

assert stage.client.get(BomLine, "scooter-ghost").quantity == 1
scooter = stage.client.call_function(ExplodeBom(part_id="scooter"))
assert [row["part_id"] for row in scooter["rows"]] == ["deck", "wheel", "hub", "bearing", "spoke"]
```

## A reload updates rows and retires nothing

`SOURCE_V2` is a later version of the same source.
Loading it into the world that already holds `SOURCE_V1` shows what a reload does.
Re-sending a row with an existing id updates it.
Re-sending a link that already exists adds no link.

In `SOURCE_V2`, the spoke quantity changes from 32 to 36, and a new part `bell` joins the bike.
It also leaves out the line `scooter-deck`.
A line that is missing from a new load is not retired.
It stays live, so the scooter still has its deck.

The engine keeps the old spoke quantity of 32 as a closed row.
There is no public read for a closed row yet.
As-of reads of history are on the [roadmap](../../docs/roadmap.md#later--pulled-by-real-use) as a Later item.
So this example reads only the current quantity, and it has no sync or diff script.

```python
world = load_world(SOURCE_V1)
before = world.client.call_function(ExplodeBom(part_id="bike"))
assert [row["total_quantity"] for row in before["rows"] if row["part_id"] == "spoke"] == [64]

reload_reports = load_master_data(world.client, SOURCE_V2)
assert [key for key, report in reload_reports.items() if not report.ok] == ["bom_child"]

after = world.client.call_function(ExplodeBom(part_id="bike"))
assert after["cycles"] == []
assert [(row["part_id"], row["level"], row["total_quantity"]) for row in after["rows"]] == [
    ("bell", 1, 1),
    ("frame", 1, 1),
    ("wheel", 1, 2),
    ("hub", 2, 2),
    ("bearing", 3, 4),
    ("spoke", 2, 72),
]

scooter = world.client.call_function(ExplodeBom(part_id="scooter"))
assert [row["part_id"] for row in scooter["rows"]] == [
    "deck", "wheel", "hub", "bearing", "spoke",
]
assert world.client.get(BomLine, "scooter-deck").quantity == 1
assert world.client.get(BomLine, "wheel-spoke").quantity == 36
```

## Cycles are data, with a per-path guard

A cycle is a part that, through its lines, contains itself.
Source data can contain one by mistake.
`explode_bom` does not loop and does not raise on a cycle.
It stops at the repeated part and adds the path to the `cycles` list.
The rows found before the repeat are still returned.

The guard checks only the current path, not every part seen so far.
A global visited set would hide a shared subassembly.
The wheel is under both the bike and the scooter, and it must appear under each of them.
A per-path guard lets it appear under every parent and still stops a real loop.

`CYCLIC_SOURCE` has a to b, b to c, c to a, and a to d.

```python
from examples.bill_of_materials.fixtures import CYCLIC_SOURCE

loop = load_world(CYCLIC_SOURCE).client.call_function(ExplodeBom(part_id="a"))
assert [row["part_id"] for row in loop["rows"]] == ["b", "c", "d"]
assert [row["level"] for row in loop["rows"]] == [1, 2, 1]
assert loop["cycles"] == [["a", "b", "c", "a"]]

clean = load_world().client
assert [row["part_id"] for row in clean.call_function(ExplodeBom(part_id="scooter"))["rows"]] == [
    "deck", "wheel", "hub", "bearing", "spoke",
]
assert [row["part_id"] for row in clean.call_function(ExplodeBom(part_id="bike"))["rows"]].count(
    "hub"
) == 1
```

## where_used and parts_from_supplier

`where_used` walks upward from a part to every part that contains it.
It uses the same per-path guard.
`parts_from_supplier` reads the `part_supplier` link in reverse.
It sorts its result by `part_id`.

```python
used = client.call_function(WhereUsed(part_id="bearing"))
assert used["cycles"] == []
assert used["rows"] == [
    {"level": 1, "part_id": "hub", "part_name": "Hub", "quantity": 2},
    {"level": 2, "part_id": "wheel", "part_name": "Wheel", "quantity": 1},
    {"level": 3, "part_id": "bike", "part_name": "Bike", "quantity": 2},
    {"level": 3, "part_id": "scooter", "part_name": "Scooter", "quantity": 2},
]

assert client.call_function(PartsFromSupplier(supplier_id="acme")) == [
    {"part_id": "deck", "part_name": "Deck"},
    {"part_id": "frame", "part_name": "Frame"},
    {"part_id": "spoke", "part_name": "Spoke"},
]
```

In `where_used`, the `quantity` of a row is the quantity of the line that joins it to the level below.

## Unknown ids are refused

A Function that gets an unknown part or supplier id raises `PreconditionFailed`.
Its code is `PRECONDITION_FAILED`, and its message names the id.
It does not return an empty success.

```python
from ontary import PreconditionFailed

for params in (ExplodeBom(part_id="nope"), PartsFromSupplier(supplier_id="nobody")):
    try:
        client.call_function(params)
    except PreconditionFailed as refusal:
        assert refusal.code == "PRECONDITION_FAILED"
        assert next(iter(params.model_dump().values())) in str(refusal)
    else:
        raise AssertionError("the unknown id was not refused")
```

## Traversal cost

`traverse` follows one hop.
It makes one read for each row it returns.
`explode_bom` takes two hops per level, from a part to its lines and from a line to its child.
So one explosion makes a number of reads in proportion to the number of lines under the part: O(lines).
That is fine for a small hierarchy.
Batched traversal is tracked in [#69](https://github.com/ryoochi0112/ontary/issues/69).

## Run the tests

From the repository root:

```bash
uv run pytest tests/test_examples_bill_of_materials_e2e.py tests/test_examples_bill_of_materials_mcp.py tests/test_examples_bill_of_materials_docs.py
```

## Serve it over MCP

[`run_mcp.py`](run_mcp.py) serves the loaded ontology over MCP stdio for a planner:

```bash
uv run python -m examples.bill_of_materials.run_mcp
```
