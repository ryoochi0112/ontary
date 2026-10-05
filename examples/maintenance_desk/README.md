# Maintenance desk reference app

`examples/maintenance_desk/` is a second in-repo reference app for `ontary`.
It models a maintenance desk: people report faults on equipment, and a
technician repairs them with spare parts. Read it after the
[tickets app](../tickets/README.md) when you want to see more engine
guarantees in one ontology.

## Domain map

- **Region** and **Site** are the two scope levels. A Site belongs to one
  Region (`site_region`).
- **Asset** is a machine at a Site (`asset_site`). Its `location` is a nested
  `Location` model (`building`, `floor`).
- **Technician** works at a Site (`technician_site`). `phone` and `email` are
  sensitive.
- **Part** is catalogue data. It is `unscoped`: every consumer reads it.
- **WorkOrder** is a fault on an Asset (`order_asset`). It is an owned type, so
  only actions write it.
- **PartsUsage** records parts used on a WorkOrder (`usage_order`,
  `usage_part`). It is an owned snapshot type.
- **Reservation** holds part stock for a WorkOrder (`reservation_order`,
  `reservation_part`). It is owned too.
- **WorkOrderCompleted** is an event that `complete` emits.

Six actions are the only writers: `report_fault`, `triage`, `schedule`,
`complete`, `cancel`, and `record_parts_used`. Two Functions derive answers:
`overdue` lists open orders past their deadline at a Site, and
`maintenance_cost` sums parts cost per Asset. Neither answer is stored.

Three consumers appear in [`fixtures.py`](fixtures.py): a regional Manager, a
Technician confined to one Site, and an AI assistant confined to one Site.
Money is an integer in minor units, in one currency.

See [`ontology.py`](ontology.py) for the full declaration.

## Which feature enforces which guarantee

| Guarantee | Engine feature | Where |
| --- | --- | --- |
| An order only takes legal moves (`reported` to `triaged` to `scheduled` to `done`; `cancelled` from any open status). | `transitions=` on `WorkOrder.status` | `MOVES` in `ontology.py` |
| A zero or negative quantity is refused. | `@ontology.rule` | `valid_usage`, `positive_quantity` |
| An AI assistant never reads `phone` or `email`. | `Sensitivity(ai_usable=False)` | `Technician` |
| A Site-A user never reads Site-B rows. | `ScopePolicy` through `ViaLink` | every scoped type |
| Past usage keeps its cost when the catalogue price changes. | `snapshot=True` | `PartsUsage.unit_cost` |
| A one-person aggregate is withheld. | `min_n=2` | `Ontology(...)` |
| Only the six actions write orders, usage and reservations. | `owned=True` | `WorkOrder`, `PartsUsage`, `Reservation` |

The first block builds the app and shows the transition and the rule. The
fixture clock reads 2026-10-05 09:00 UTC, when nine orders are seeded.
Two days later the urgent and high open orders are overdue.

```python
from datetime import timedelta

from examples.maintenance_desk.fixtures import (
    FIXTURE_NOW,
    FixtureClock,
    assistant_consumer,
    load_fixtures,
    manager_consumer,
    seed_work_orders,
    technician_consumer,
)
from examples.maintenance_desk.ontology import (
    Complete,
    RecordPartsUsed,
    SiteQuestion,
    Technician,
    WorkOrder,
    WorkOrderCompleted,
    build_ontology,
)
from ontary.testing import SequentialIds, raises_code

clock = FixtureClock(FIXTURE_NOW)
ontology, store = build_ontology(clock=clock)
ids = load_fixtures(store)
manager = ontology.bind(store, clock=clock, id_factory=SequentialIds("wo")).for_consumer(
    manager_consumer(ids)
)
orders = seed_work_orders(manager, ids)

# Transition: a reported order cannot jump to done.
with raises_code("TRANSITION_NOT_ALLOWED"):
    manager.execute(Complete(order_id=orders["a_reported"]))
assert manager.get(WorkOrder, orders["a_reported"]).status == "reported"

# Rule: a usage record needs a positive quantity.
with raises_code("RULE_VIOLATED"):
    manager.execute(
        RecordPartsUsed(order_id=orders["a_scheduled"], part_id="part-0", quantity=0)
    )

# Derived answer: nothing is overdue now, two orders are two days later.
site_a = SiteQuestion(site_id=ids["site_a_id"])
assert manager.call_function(site_a) == []
clock.set(FIXTURE_NOW + timedelta(days=2))
late = manager.call_function(site_a)
assert {row["id"] for row in late} == {orders["a_reported"], orders["a_triaged"]}
```

## Sensitivity, scope and `min_n`

The assistant sees the same Technician rows as the Technician, but `phone` and
`email` come back as `None`. A Site-A consumer who asks about Site B gets
`VISIBILITY_DENIED`.

`min_n=2` is a deliberate choice. The default is 3, and a declared
`Sensitivity` with that default makes `diagnose()` report `MIN_N_UNSET`. A
value of 1 turns the guard off. A value of 2 still refuses an aggregate that
describes one person, and each Site in the fixtures has enough rows to pass.
Site A has two Technicians, so counting them works. Counting only one of them
raises `MIN_N_VIOLATION`.

```python
assistant = ontology.bind(store, clock=clock).for_consumer(assistant_consumer(ids))
for tech in assistant.list(Technician, limit=None):
    assert tech.name and tech.phone is None and tech.email is None

with raises_code("VISIBILITY_DENIED"):
    assistant.call_function(SiteQuestion(site_id=ids["site_b_id"]))

technician = ontology.bind(store, clock=clock).for_consumer(technician_consumer(ids))
assert technician.aggregate(Technician, func="count") == 2
with raises_code("MIN_N_VIOLATION"):
    technician.aggregate(Technician, where={"id": "A-tech-0"}, func="count")
```

## Why the technician is a link

A scheduled order needs a technician. The first idea is a rule: "a scheduled
order must have a technician". But a rule reads only the object's own
properties. It cannot see links. That pushes you to store `technician_id` as a
plain string on `WorkOrder`, and then the engine neither checks that the
technician exists nor lets you traverse the relation.

This app keeps the assignment as the owned link `order_technician` instead.
Only the `schedule` action creates it, and the action itself checks that the
technician exists and works at the asset's Site. So no rule checks the
technician. Rules that see links are tracked in
[ontary#156](https://github.com/ryoochi0112/ontary/issues/156).

## Reading events

`complete` emits `WorkOrderCompleted`, and you read committed events with
`client.events`. Nothing delivers them to a listener yet. Subscriber delivery
is tracked in [ontary#155](https://github.com/ryoochi0112/ontary/issues/155).

Until then, poll with a cursor. Keep the timestamp of the last event you
handled. Ask for events `since` that instant, and skip any you already saw.
The `since` bound includes its instant, so the skip is needed.

```python
seen: set[tuple[str, str]] = set()
cursor = None


def poll() -> list[str]:
    """Return the asset ids of completions that are new since the last poll."""
    global cursor
    fresh = []
    for record in manager.events(WorkOrderCompleted, since=cursor):
        key = (record.invocation_id, record.about_id)
        if key not in seen:
            seen.add(key)
            fresh.append(record.payload.asset_id)
        cursor = record.ts
    return fresh


assert sorted(poll()) == ["A-asset-0", "B-asset-0"]
assert poll() == []
```

## Running it

From the repository root, run the tests:

```bash
uv run pytest tests/test_examples_maintenance_desk_e2e.py tests/test_examples_maintenance_desk_mcp.py
```

[`run_mcp.py`](run_mcp.py) serves the ontology over MCP stdio for one Site-A
technician. The fixture clock is moved two days on, so `overdue` returns
orders:

```bash
uv run python -m examples.maintenance_desk.run_mcp
```

## Test coverage

- [`tests/test_examples_maintenance_desk_e2e.py`](../../tests/test_examples_maintenance_desk_e2e.py)
  covers each guarantee above through the three consumers, and executes every
  Python block in this README.
- [`tests/test_examples_maintenance_desk_mcp.py`](../../tests/test_examples_maintenance_desk_mcp.py)
  calls the MCP server in process: a derived Function and a refused action.
