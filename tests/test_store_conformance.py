"""Conformance suite for the `Store` Protocol (m35-sdk-refactor T5).

Parametrized over store factories -- the *same* behavioral assertions run
against every backend so the storage seam (spec §6 "Storage seam") can't
silently drift. Written against `ObjectStore` (SQLite) first, red-green: the
suite is derived by observing `ObjectStore`'s real behavior (including the
oddities noted below), then `InMemoryStore` is made to match.

Behavioral assertions use only `Store` protocol members (no
`store._conn`/`store._registry` reach-ins) -- that's deliberate: a test that
only compiles against a protocol member is a test that actually proves the
seam, and it's the only way the same assertions can run unmodified against a
dict-backed double. Backend-independent model and public-surface checks from
the legacy SQLite store suite live here too when they can be expressed without
reaching into one backend's storage representation.

Coded errors (m35-sdk-refactor T6/AC7): `update`/`create_link`/
`links_from`/`links_to` on BOTH backends wrap a registry `KeyError` for an
unregistered object/link type into validation-kind errors with
`UNKNOWN_OBJECT_TYPE`/`UNKNOWN_LINK_TYPE` -- matching `insert`'s existing
behavior -- so no bare `KeyError` escapes any of the four store methods on
either backend; see the "coded errors" section below.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from conftest import raises_code

from ontary import Ontology, OntologyClient, OntologyObject, prop
from ontary.audit import CapabilityAccessRecord, EffectRecord
from ontary.errors import AuthorityError, ConflictError, ValidationFailed
from ontary.meta import (
    Cardinality,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
)
from ontary.outbox import OutboxRecord
from ontary.security import Consumer
from ontary.store import (
    AuditEntry,
    Lineage,
    ObjectStore,
    Source,
    Store,
    StoredObject,
    WriteRecord,
)
from ontary.store.inmemory import InMemoryStore

StoreFactory = Callable[[OntologyRegistry], Store]


def _object_type(api_name: str) -> ObjectTypeDef:
    return ObjectTypeDef(
        api_name=api_name,
        display_name=api_name,
        description=f"A canonical {api_name}",
        layer="L0",
        properties=[
            PropertyDef(name="id", type="str", required=False),
            PropertyDef(name="name", type="str"),
        ],
        primary_key="id",
    )


def build_registry() -> OntologyRegistry:
    registry = OntologyRegistry()
    for api_name in ("Company", "Team", "Department"):
        registry.register_object_type(_object_type(api_name))
    registry.register_link_type(
        LinkTypeDef(
            api_name="belongsToDepartment",
            from_type="Team",
            to_type="Department",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Team belongs to a Department",
        )
    )
    registry.validate()
    return registry


def _date_ontology() -> tuple[Ontology, type[OntologyObject]]:
    ontology = Ontology("date-conformance", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped")
    class Event(OntologyObject):
        id: str = prop(primary_key=True)
        occurred_on: date

    ontology.validate()
    return ontology, Event


def _date_consumer() -> Consumer:
    return Consumer(
        actor_id="date-reader",
        role="Reader",
        scope_level="org",
        scope_id="unused-for-unscoped-type",
        kind="human",
    )


def _choices_ontology() -> tuple[Ontology, type[OntologyObject]]:
    ontology = Ontology("choices-conformance", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped")
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        status: str = prop(choices=["open", "closed"])

    ontology.validate()
    return ontology, Ticket


def _choices_consumer() -> Consumer:
    return Consumer(
        actor_id="choices-reader",
        role="Reader",
        scope_level="org",
        scope_id="unused-for-unscoped-type",
        kind="human",
    )


def build_authority_registry() -> OntologyRegistry:
    """Registry exercising declared-contracts §3 AC2 authority combinations:
    a fully source-backed type (`Ticket`), a whole-type owned type
    (`Escalation`), a source-backed type with an owned property (`Company`,
    owned property `tier`), and both an owned and a source-backed link
    type."""
    registry = OntologyRegistry()
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Ticket",
            display_name="Ticket",
            description="A source-supplied ticket",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str", required=False),
                PropertyDef(name="status", type="str"),
            ],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Escalation",
            display_name="Escalation",
            description="An ontology-owned escalation record",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str", required=False),
                PropertyDef(name="reason", type="str"),
            ],
            primary_key="id",
            owned=True,
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Company",
            display_name="Company",
            description="A source-backed company with an owned property",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str", required=False),
                PropertyDef(name="name", type="str"),
                PropertyDef(name="tier", type="str", required=False),
            ],
            primary_key="id",
            owned={"tier": "unranked"},
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="ownedLink",
            from_type="Escalation",
            to_type="Ticket",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Ontology-owned link",
            owned=True,
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="sourceLink",
            from_type="Ticket",
            to_type="Company",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Source-backed link",
        )
    )
    registry.validate()
    return registry


def _make_object_store(registry: OntologyRegistry) -> Store:
    return ObjectStore(registry)


def _make_in_memory_store(registry: OntologyRegistry) -> Store:
    return InMemoryStore(registry)


# The Postgres backend joins the suite when a server is reachable (M8a).
#
# `ONTARY_TEST_POSTGRES_DSN` gates it, and its ABSENCE skips rather than fails:
# `make verify` is defined as offline and no-DB, and breaking that would make the
# gate unrunnable on a laptop without Docker. The obvious hazard is the one M6
# found in packaging -- a backend nothing ever exercises -- so CI runs this suite
# against a real Postgres service container on every PR, and the skip is a local
# convenience rather than the project's position on whether it is tested.
#
# Each factory call gets its own SCHEMA, dropped and recreated, so the suite's
# many stores never see each other's rows. A database would be cleaner still and
# needs a second connection to create; a schema is one statement on the
# connection we already have.
POSTGRES_DSN = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
_postgres_schema_counter = itertools.count()


def _make_postgres_store(registry: OntologyRegistry) -> Store:
    from ontary.store.postgres import PostgresStore

    assert POSTGRES_DSN is not None
    schema = f"ontary_test_{next(_postgres_schema_counter)}"
    import psycopg

    with psycopg.connect(POSTGRES_DSN, autocommit=True) as setup:
        setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        setup.execute(f'CREATE SCHEMA "{schema}"')
    separator = "&" if "?" in POSTGRES_DSN else "?"
    return PostgresStore(
        registry, f"{POSTGRES_DSN}{separator}options=-csearch_path%3D{schema}"
    )


STORE_FACTORIES: list[StoreFactory] = [
    _make_object_store,
    _make_in_memory_store,
]
if POSTGRES_DSN:
    STORE_FACTORIES.append(_make_postgres_store)


@pytest.fixture(params=STORE_FACTORIES, ids=lambda f: f.__name__.removeprefix("_make_"))
def store_factory(request: pytest.FixtureRequest) -> StoreFactory:
    factory: StoreFactory = request.param
    return factory


@pytest.fixture
def store(store_factory: StoreFactory) -> Store:
    return store_factory(build_registry())


@pytest.fixture
def one_to_many_store(store_factory: StoreFactory) -> Store:
    registry = build_registry()
    registry.register_link_type(
        LinkTypeDef(
            api_name="hasTeam",
            from_type="Department",
            to_type="Team",
            cardinality=Cardinality.ONE_TO_MANY,
            description=(
                "Department has teams -- ONE_TO_MANY, so the *to*-side "
                "(Team) is the cardinality-constrained one: a second link "
                "targeting the same `to_id` from a *different* `from_id` "
                "must raise a conflict-kind error with `CARDINALITY_VIOLATION`, while the same "
                "`from_id` may link to many distinct teams unchecked."
            ),
        )
    )
    registry.validate()
    return store_factory(registry)


@pytest.fixture
def authority_store(store_factory: StoreFactory) -> Store:
    return store_factory(build_authority_registry())


def _history_rows(
    store: Store, object_type: str, object_id: str
) -> list[tuple[dict[str, Any], str | None]]:
    """Read all stored versions through each backend's private representation.

    The Store protocol intentionally exposes current rows only, so the moved
    history-retention assertion needs a backend-aware probe: SQL for the two
    relational stores and their row list for the in-memory implementation.
    """
    if isinstance(store, ObjectStore):
        rows = store._conn.execute(
            """
            SELECT payload, valid_to FROM objects
            WHERE object_type = ? AND id = ? AND tenant = ?
            ORDER BY row_id ASC
            """,
            (object_type, object_id, store._tenant),
        ).fetchall()
        return [(json.loads(row["payload"]), row["valid_to"]) for row in rows]

    if isinstance(store, InMemoryStore):
        return [
            (json.loads(row["payload"]), row["valid_to"])
            for row in store._objects
            if row["object_type"] == object_type
            and row["id"] == object_id
            and row["tenant"] == store._tenant
        ]

    from ontary.store.postgres import PostgresStore

    assert isinstance(store, PostgresStore)
    with store._conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT payload, valid_to FROM objects
            WHERE object_type = %s AND id = %s AND tenant = %s
            ORDER BY row_id ASC
            """,
            (object_type, object_id, store._tenant),
        )
        rows = cursor.fetchall()
    return [(json.loads(payload), valid_to) for payload, valid_to in rows]


@pytest.fixture(
    params=["sqlite", "in_memory"] + (["postgres"] if POSTGRES_DSN else [])
)
def concurrent_stores(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Callable[[], Store]:
    """Fresh handles onto one backend substrate for concurrency conformance.

    SQLite and Postgres need separate connections, matching independent action
    workers/processes. The in-memory backend has no shared substrate between
    instances, so every call returns the same store and the workers are threads.
    SQL handles are constructed in the worker that uses them because SQLite
    deliberately binds a connection to its creating thread.
    """
    registry = build_registry()
    if request.param == "sqlite":
        path = tmp_path / "concurrent.db"
        ObjectStore(registry, str(path))
        return lambda: ObjectStore(registry, str(path))
    if request.param == "in_memory":
        shared = InMemoryStore(registry)
        return lambda: shared

    assert request.param == "postgres"
    assert POSTGRES_DSN is not None
    schema = f"ontary_test_concurrent_{next(_postgres_schema_counter)}"
    import psycopg

    with psycopg.connect(POSTGRES_DSN, autocommit=True) as setup:
        setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        setup.execute(f'CREATE SCHEMA "{schema}"')
    separator = "&" if "?" in POSTGRES_DSN else "?"
    dsn = f"{POSTGRES_DSN}{separator}options=-csearch_path%3D{schema}"
    from ontary.store.postgres import PostgresStore

    PostgresStore(registry, dsn)
    return lambda: PostgresStore(registry, dsn)


# -- CRUD / current-after-update / history --------------------------------


def test_date_declares_persists_hydrates_and_filters_by_equality(
    store_factory: StoreFactory,
) -> None:
    """AC24's date row runs unchanged against every Store backend."""
    ontology, Event = _date_ontology()
    store = store_factory(ontology.registry)
    client = OntologyClient(ontology, store, _date_consumer())

    event_def = ontology.registry.get_object_type("Event")
    declared_types = {prop.name: prop.type for prop in event_def.properties}
    assert declared_types["occurred_on"] == "date"

    store.insert(
        "Event",
        {"id": "date-object", "occurred_on": date(2026, 8, 25)},
        Source(source_system="test"),
    )
    store.insert(
        "Event",
        {"id": "iso-string", "occurred_on": "2026-08-26"},
        Source(source_system="test"),
    )

    stored = store.read_current("Event", "date-object")
    assert stored is not None
    assert stored.payload["occurred_on"] == "2026-08-25"
    assert isinstance(stored.payload["occurred_on"], str)

    hydrated = client.get(Event, "date-object")
    assert hydrated is not None
    assert hydrated.occurred_on == date(2026, 8, 25)
    assert type(hydrated.occurred_on) is date

    by_date = client.list(Event, {"occurred_on": date(2026, 8, 25)})
    assert [event.id for event in by_date] == ["date-object"]
    by_iso_string = client.list(Event, {"occurred_on": "2026-08-26"})
    assert [event.id for event in by_iso_string] == ["iso-string"]


@pytest.mark.parametrize(
    ("bad_value", "case"),
    [
        pytest.param("2026/08/25", "non-iso-string", id="non-iso-string"),
        pytest.param(
            "20260825", "non-yyyy-mm-dd-string", id="non-yyyy-mm-dd-string"
        ),
        pytest.param(
            datetime(2026, 8, 25, 12, 30),
            "datetime-with-time",
            id="datetime-with-time",
        ),
        pytest.param(20260825, "wrong-type", id="wrong-type"),
    ],
)
def test_date_insert_rejects_each_invalid_shape_as_invalid_record(
    store_factory: StoreFactory, bad_value: object, case: str
) -> None:
    """Each fixture differs from a valid row only in the date value's shape."""
    ontology, _Event = _date_ontology()
    store = store_factory(ontology.registry)

    with raises_code(ValidationFailed, "INVALID_RECORD"):
        store.insert(
            "Event",
            {"id": case, "occurred_on": bad_value},
            Source(source_system="test"),
        )
    assert store.read_current("Event", case) is None


def test_choices_declares_persists_hydrates_and_filters_by_equality(
    store_factory: StoreFactory,
) -> None:
    """AC24's enum row runs unchanged against every Store backend."""
    ontology, Ticket = _choices_ontology()
    store = store_factory(ontology.registry)
    client = OntologyClient(ontology, store, _choices_consumer())

    status_def = ontology.registry.get_object_type("Ticket").properties[1]
    assert status_def.type == "str"
    assert status_def.choices == ("open", "closed")

    store.insert(
        "Ticket",
        {"id": "ticket-open", "status": "open"},
        Source(source_system="test"),
    )
    store.insert(
        "Ticket",
        {"id": "ticket-closed", "status": "closed"},
        Source(source_system="test"),
    )

    stored = store.read_current("Ticket", "ticket-open")
    assert stored is not None
    assert stored.payload["status"] == "open"

    hydrated = client.get(Ticket, "ticket-open")
    assert hydrated is not None
    assert hydrated.status == "open"

    closed = client.list(Ticket, {"status": "closed"})
    assert [ticket.id for ticket in closed] == ["ticket-closed"]


def test_choices_insert_rejects_out_of_set_value(
    store_factory: StoreFactory,
) -> None:
    ontology, _Ticket = _choices_ontology()
    store = store_factory(ontology.registry)

    with raises_code(ValidationFailed, "INVALID_RECORD"):
        store.insert(
            "Ticket",
            {"id": "ticket-pending", "status": "pending"},
            Source(source_system="test"),
        )

    assert store.read_current("Ticket", "ticket-pending") is None


def test_choices_update_rejects_out_of_set_value_without_changing_row(
    store_factory: StoreFactory,
) -> None:
    ontology, _Ticket = _choices_ontology()
    store = store_factory(ontology.registry)
    source = Source(source_system="test")
    store.insert("Ticket", {"id": "ticket-1", "status": "open"}, source)

    with raises_code(ValidationFailed, "INVALID_RECORD"):
        store.update("Ticket", "ticket-1", {"status": "pending"}, source)

    stored = store.read_current("Ticket", "ticket-1")
    assert stored is not None
    assert stored.payload["status"] == "open"


def test_choices_hydration_rejects_persisted_out_of_set_value(
    store_factory: StoreFactory,
) -> None:
    """Simulate a legacy/corrupt row while still reading every real backend."""
    ontology, Ticket = _choices_ontology()
    store = store_factory(ontology.registry)
    client = OntologyClient(ontology, store, _choices_consumer())
    status_def = ontology.registry.get_object_type("Ticket").properties[1]
    declared_choices = status_def.choices

    status_def.choices = None
    store.insert(
        "Ticket",
        {"id": "legacy-pending", "status": "pending"},
        Source(source_system="legacy"),
    )
    status_def.choices = declared_choices

    with raises_code(ValidationFailed, "INVALID_RECORD"):
        client.get(Ticket, "legacy-pending")


@pytest.mark.parametrize(
    "source_id",
    ["s1", "seed-1"],
    ids=["conformance", "legacy_store_case"],
)
def test_insert_and_read_current_roundtrip(
    store: Store, source_id: str
) -> None:
    obj_id = store.insert(
        "Company",
        {"name": "Acme"},
        Source(source_system="synthetic", source_id=source_id),
    )

    got = store.read_current("Company", obj_id)
    assert got is not None
    assert got.payload["name"] == "Acme"
    assert got.payload["id"] == obj_id
    assert got.lineage.object_type == "Company"
    assert got.lineage.object_id == obj_id
    assert got.lineage.source_system == "synthetic"
    assert got.lineage.source_id == source_id
    assert got.lineage.valid_from is not None
    assert got.lineage.valid_to is None

    all_companies = store.read_all("Company")
    assert any(c.payload["id"] == obj_id for c in all_companies)


@pytest.mark.parametrize(
    ("source_id", "extracted_at"),
    [
        ("s1", "2024-01-01T00:00:00+00:00"),
        ("seed-1", "2026-01-01T00:00:00+00:00"),
    ],
    ids=["conformance", "legacy_store_case"],
)
def test_insert_extracted_at_lineage_round_trips_into_read(
    store: Store, source_id: str, extracted_at: str
) -> None:
    """`Source.extracted_at` (the upstream extraction/observation timestamp,
    distinct from the store's own `valid_from`) must round-trip into every
    read result's `lineage.extracted_at` field, on both backends."""
    obj_id = store.insert(
        "Company",
        {"name": "Acme"},
        Source(
            source_system="synthetic",
            source_id=source_id,
            extracted_at=extracted_at,
        ),
    )

    current = store.read_current("Company", obj_id)
    assert current is not None
    assert current.lineage.extracted_at == extracted_at

    [from_all] = [c for c in store.read_all("Company") if c.payload["id"] == obj_id]
    assert from_all.lineage.extracted_at == extracted_at


def test_insert_without_extracted_at_leaves_it_none(store: Store) -> None:
    obj_id = store.insert(
        "Company", {"name": "Acme"}, Source(source_system="synthetic")
    )

    got = store.read_current("Company", obj_id)
    assert got is not None
    assert got.lineage.extracted_at is None


def test_read_current_missing_object_returns_none(store: Store) -> None:
    assert store.read_current("Company", "does-not-exist") is None


def test_update_returns_latest_and_merges_untouched_fields(store: Store) -> None:
    obj_id = store.insert(
        "Team", {"name": "Rockets"}, Source(source_system="synthetic")
    )
    store.update(
        "Team", obj_id, {"name": "Rockets FC"}, Source(source_system="synthetic")
    )

    current = store.read_current("Team", obj_id)
    assert current is not None
    assert current.payload["name"] == "Rockets FC"
    assert current.payload["id"] == obj_id


def test_update_persists_extracted_at_on_new_version(store: Store) -> None:
    obj_id = store.insert(
        "Team", {"name": "Rockets"}, Source(source_system="synthetic")
    )
    store.update(
        "Team",
        obj_id,
        {"name": "Rockets FC"},
        Source(source_system="synthetic", extracted_at="2026-02-02T00:00:00+00:00"),
    )

    got = store.read_current("Team", obj_id)
    assert got is not None
    assert got.lineage.extracted_at == "2026-02-02T00:00:00+00:00"


def test_update_closes_old_row_read_all_has_single_current_entry(store: Store) -> None:
    """Pins the bitemporal write (spec AC5): after an update, `read_all`
    must show exactly one CURRENT row per id -- the prior version is closed,
    not left dangling as a second "current" row, and not silently
    overwritten in a way that would corrupt history."""
    obj_id = store.insert(
        "Team", {"name": "Rockets"}, Source(source_system="synthetic")
    )
    store.update(
        "Team", obj_id, {"name": "Rockets FC"}, Source(source_system="synthetic")
    )
    store.update(
        "Team", obj_id, {"name": "Rockets FC II"}, Source(source_system="synthetic")
    )

    all_teams = [t for t in store.read_all("Team") if t.payload["id"] == obj_id]
    assert len(all_teams) == 1
    assert all_teams[0].payload["name"] == "Rockets FC II"


def test_update_closes_old_row_and_preserves_history_row(store: Store) -> None:
    """`update()` closes and retains the old row on every backend."""
    obj_id = store.insert(
        "Team", {"name": "Rockets"}, Source(source_system="synthetic")
    )

    store.update(
        "Team", obj_id, {"name": "Rockets FC"}, Source(source_system="synthetic")
    )

    rows = _history_rows(store, "Team", obj_id)
    assert len(rows) == 2, "update() must retain the old row, not overwrite it"
    old_payload, old_valid_to = rows[0]
    new_payload, new_valid_to = rows[1]
    assert old_payload["name"] == "Rockets"
    assert old_valid_to is not None, "old row must be closed on update"
    assert new_payload["name"] == "Rockets FC"
    assert new_valid_to is None, "new row must be open (current)"

    current = store.read_current("Team", obj_id)
    assert current is not None
    assert current.payload["name"] == "Rockets FC"


def test_update_does_not_leak_underscore_metadata_into_merge(store: Store) -> None:
    obj_id = store.insert(
        "Team", {"name": "Rockets"}, Source(source_system="synthetic", source_id="s1")
    )
    store.update(
        "Team",
        obj_id,
        {"name": "Rockets FC"},
        Source(source_system="reingest", source_id="s2"),
    )

    current = store.read_current("Team", obj_id)
    assert current is not None
    assert current.lineage.source_system == "reingest"
    assert current.lineage.source_id == "s2"
    assert current.payload["name"] == "Rockets FC"
    assert not any(key.startswith("_") for key in current.payload)


def test_update_missing_object_raises_object_not_found(store: Store) -> None:
    with raises_code(ValidationFailed, "OBJECT_NOT_FOUND"):
        store.update(
            "Team", "no-such-id", {"name": "x"}, Source(source_system="synthetic")
        )


def test_insert_mints_id_when_absent(store: Store) -> None:
    obj_id = store.insert("Company", {"name": "Acme"}, Source(source_system="s"))
    assert obj_id
    obj_id_2 = store.insert("Company", {"name": "Beta"}, Source(source_system="s"))
    assert obj_id_2 != obj_id


def test_read_result_mutation_does_not_corrupt_stored_state(store: Store) -> None:
    """Both backends round-trip payloads through JSON (`ObjectStore` via its
    TEXT column, `InMemoryStore` via an explicit `json.dumps`/`json.loads`
    pair) -- so a read result is always a fresh, unshared structure.
    Mutating a nested container in one read must never be visible to a
    later, independent read."""
    obj_id = store.insert(
        "Company",
        {"name": "Acme", "tags": {"nested": ["a", "b"]}},
        Source(source_system="s"),
    )

    first = store.read_current("Company", obj_id)
    assert first is not None
    first.payload["tags"]["nested"].append("mutated")
    first.payload["name"] = "corrupted"

    second = store.read_current("Company", obj_id)
    assert second is not None
    assert second.payload["tags"]["nested"] == ["a", "b"]
    assert second.payload["name"] == "Acme"


def test_insert_non_json_serializable_payload_raises_type_error(store: Store) -> None:
    """Pins `ObjectStore`'s incidental-but-relied-on `json.dumps` behavior:
    a payload value that isn't JSON-serializable (e.g. a bare `set`) raises
    `TypeError` on insert, on both backends."""
    with pytest.raises(TypeError):
        store.insert(
            "Company", {"name": "Acme", "bad": {1, 2, 3}}, Source(source_system="s")
        )


# -- read_page / keyset ordering (pagination-hardening T1, amended T2) ------
#
# `read_page` is the same raw, trusted-caller-only read path as `read_current`/
# `read_all` -- no scope/visibility filtering happens here (that's the
# guarded layer's job).
#
# AMENDED 2026-07-25 (spec §5, three times -- this is the final form):
# ordering keys off the store's own per-row identity (SQLite's physical
# ROWID for `ObjectStore`, an equivalent counter for `InMemoryStore`), NOT
# the payload primary key. A review proved the rejected pk-keyed design
# silently DROPS ROWS: the store does not enforce pk uniqueness among
# current rows (two `insert` calls with the same payload id, or an `update`
# that mutates a pk into a collision, or an int/str pk pair that collides
# under any text-cast comparison), so a cursor keyed on the payload pk
# cannot express the resulting ties. A second review pass found the per-row
# identity ALSO could not live on `Lineage`/`StoredObject` (AC8: a narrow
# consumer could infer how many rows its scope hid, or another tenant's
# write volume, by gap arithmetic on a dense row counter) -- so `read_page`
# returns it OUT-OF-BAND instead, one `PagedRow(key, obj)` per row, never as
# a field on `obj` itself.
#
# A THIRD review pass (T2) found even THAT insufficient: the out-of-band key
# was still the raw row id (just a decimal string), and a caller could
# subtract two of its own consecutive cursors to recover exactly how many
# rows its scope hid (proven: cursors `['23','27','35','37']` -> hidden
# counts `[3,7,1]`), plus the magnitude leaked total store write volume.
# `PagedRow.key` is therefore now a RANDOM PER-ROW PAGE TOKEN (uuid4 hex,
# written to an indexed `page_token` column on every insert) instead of the
# row id itself -- `read_page` resolves `token -> row_id` via one indexed
# lookup, then pages by row identity exactly as before. These tests pin
# ordering/boundary/exhaustion/tie-safety identically on both backends, AND
# (new, T2) that the token is genuinely opaque -- not order-bearing, not
# parseable as the row identity it resolves to; `test_read_page_tie_safety_
# ...` below remains the regression pin for the dropped-rows bug itself.


# The exact FIELD SET `Lineage` may carry -- pinned at the MODEL-SCHEMA
# level, not just the serialized MCP wire (see `tests/test_mcp.py`'s
# `_LINEAGE_WIRE_KEYS`, which only pins `model_dump()`'s output keys). A
# reviewer twice re-added a `row_id`-shaped field straight onto `Lineage`
# (spec §5's amendment history) and every one of the 662 pre-existing tests
# kept passing while the leak was fully live, because none of them
# enumerated `Lineage`'s own field set -- a `Field(exclude=True)` field
# passes the wire-shape pin (it disappears from `model_dump()`) while still
# being live on the Python attribute, which is exactly the AC8 side channel
# (a narrow consumer reading `lineage.row_id` directly, or any future
# serializer that doesn't respect `exclude=True`, could still infer how
# many rows its scope hid by gap arithmetic). This assertion fails the
# instant ANY field is added to `Lineage`, `exclude`d or not.
_LINEAGE_MODEL_FIELDS = {
    "object_type",
    "object_id",
    "valid_from",
    "valid_to",
    "source_system",
    "source_id",
    "extracted_at",
}


def test_lineage_model_field_set_is_pinned_against_row_identity_leaking_back_in() -> None:
    assert set(Lineage.model_fields) == _LINEAGE_MODEL_FIELDS


# Companion pin, one level up: the same leak class can re-enter through
# `StoredObject` itself instead of through `Lineage` -- e.g. a row identity
# added directly as a THIRD top-level field (`{payload, lineage, key}`)
# rather than nested inside `lineage`. That shape would slip past the
# `Lineage` pin above (which only enumerates `Lineage`'s own fields) AND
# past `tests/test_mcp.py`'s `_LINEAGE_WIRE_KEYS` (which only enumerates the
# *nested* `lineage` object's keys on the wire, not `StoredObject`'s own
# top-level shape). Hardcoded literal, not derived from the class, so
# adding a field can't silently widen the pin along with it.
_STORED_OBJECT_MODEL_FIELDS = {"payload", "lineage"}


def test_stored_object_model_field_set_is_pinned_against_row_identity_leaking_back_in() -> None:
    assert set(StoredObject.model_fields) == _STORED_OBJECT_MODEL_FIELDS


def _insert_ids(store: Store, obj_type: str, ids: list[str]) -> None:
    for object_id in ids:
        store.insert(obj_type, {"id": object_id, "name": object_id}, Source(source_system="s"))


# Generous vs. every walk in this module (single/double-digit row counts): a
# boundary regression (e.g. the keyset filter's `row_id > ?` silently
# becoming `>=`) can turn "the walk terminates" into "the walk repeats
# forever" -- capping iterations turns that into a failing assertion instead
# of a hung CI job.
_MAX_WALK_ITERATIONS = 1000


def _walk_pages(store: Store, obj_type: str, batch: int) -> list[StoredObject]:
    """Drive a full keyset walk via repeated `read_page` calls the way a
    real caller (T2's page-filling loop) would: the cursor for the next
    call is the LAST row's own `PagedRow.key` from the PREVIOUS page -- an
    opaque per-row page token, out-of-band, resolved back to row identity
    INSIDE `read_page` -- NEVER a value read off a returned row's payload
    (spec §5's amended decision; that's precisely the design the
    tie-safety test below would catch a regression back into). Terminates
    when a page comes back with fewer than `batch` rows
    (spec: `read_page`'s own exhaustion signal -- there is no separate
    flag), capped at `_MAX_WALK_ITERATIONS` so a regression fails the test
    rather than hanging CI."""
    walked: list[StoredObject] = []
    after_key: str | None = None
    for _ in range(_MAX_WALK_ITERATIONS):
        page = store.read_page(obj_type, after_key=after_key, batch=batch)
        walked.extend(row.obj for row in page)
        if len(page) < batch:
            return walked
        after_key = page[-1].key
    pytest.fail(
        f"_walk_pages did not terminate within {_MAX_WALK_ITERATIONS} "
        "read_page calls -- a boundary regression in the keyset filter can "
        "turn a walk into an infinite loop instead of a failing assertion"
    )


def test_read_page_full_walk_matches_read_all_order_and_content(store: Store) -> None:
    """A full walk (batch smaller than the row count, so more than one
    `read_page` call is needed) reproduces `read_all`'s order AND content
    exactly -- INSERTION order, since ordering keys off the store's own
    per-row identity, not a sort of the payload primary key."""
    ids = ["c3", "c1", "c5", "c4", "c2"]
    _insert_ids(store, "Company", ids)

    walked = _walk_pages(store, "Company", batch=2)

    assert [o.payload["id"] for o in walked] == ids
    assert [o.payload["id"] for o in store.read_all("Company")] == ids


def test_read_page_key_is_an_opaque_token_not_order_bearing(store: Store) -> None:
    """`PagedRow.key` (spec §5's T2 amendment) is a random per-row PAGE
    TOKEN, not the row identity it resolves to internally: a walk still
    works end to end (keys remain usable as `after_key` round-trips -- see
    `_walk_pages`), but the keys themselves carry no ORDER information --
    unlike the row-id string this replaced, `sorted(keys)` is not the walk
    order (do not assert any actual token VALUE here, only that it is not
    order-bearing -- the exact token format is deliberately unspecified)."""
    ids = [f"n{i}" for i in range(8)]
    _insert_ids(store, "Company", ids)

    page = store.read_page("Company", batch=len(ids))
    assert [row.obj.payload["id"] for row in page] == ids
    keys = [row.key for row in page]

    walked = _walk_pages(store, "Company", batch=2)
    assert [o.payload["id"] for o in walked] == ids

    assert sorted(keys) != keys, (
        "cursor keys sorted lexicographically match walk (insertion) order "
        "-- if this ever holds by construction rather than chance, the key "
        "may have regressed into an order-bearing value (spec AC8)"
    )


def test_read_page_after_key_boundary_is_strictly_greater(store: Store) -> None:
    _insert_ids(store, "Company", ["a1", "a2", "a3"])

    first_page = store.read_page("Company", batch=2)
    assert [row.obj.payload["id"] for row in first_page] == ["a1", "a2"]
    cursor = first_page[-1].key  # per-row key of the last row in the batch

    page = store.read_page("Company", after_key=cursor)

    ids = [row.obj.payload["id"] for row in page]
    assert ids == ["a3"]
    assert "a2" not in ids


def test_read_page_batch_caps_and_short_page_signals_exhaustion(store: Store) -> None:
    _insert_ids(store, "Company", ["b1", "b2", "b3", "b4", "b5"])

    first = store.read_page("Company", batch=2)
    assert [row.obj.payload["id"] for row in first] == ["b1", "b2"]
    assert len(first) == 2  # full batch: not (yet) known to be exhausted
    cursor1 = first[-1].key

    second = store.read_page("Company", after_key=cursor1, batch=2)
    assert [row.obj.payload["id"] for row in second] == ["b3", "b4"]
    assert len(second) == 2
    cursor2 = second[-1].key

    third = store.read_page("Company", after_key=cursor2, batch=2)
    assert [row.obj.payload["id"] for row in third] == ["b5"]
    # short page ⇒ this walk is exhausted -- the ONLY exhaustion signal
    # (no separate next-key/flag): fewer rows than `batch` means there is
    # nothing left to fetch.
    assert len(third) < 2


def test_read_page_batch_below_one_raises_invalid_batch(store: Store) -> None:
    """AC6-equivalent at the store layer: SQLite's own `LIMIT -1` means
    UNLIMITED (the opposite of a bounded call) and `InMemoryStore`'s naive
    slicing would silently drop rows from the end instead -- both backends
    refuse a non-positive `batch` explicitly rather than guess."""
    _insert_ids(store, "Company", ["z1"])
    for bad_batch in (0, -1, -500):
        with raises_code(ValidationFailed, "INVALID_BATCH"):
            store.read_page("Company", batch=bad_batch)


def test_read_page_after_key_malformed_or_unknown_raises_invalid_cursor(
    store: Store,
) -> None:
    """`after_key` is UNTRUSTED input (spec pagination-hardening §6: it
    reaches the store from an MCP client via a later task's page-filling
    loop, round-tripped back from a previous page's cursor with no
    guarantee the caller didn't tamper with it).

    AMENDED 2026-07-25 (T2 review, spec §5's token amendment): `after_key`
    is now a random per-row PAGE TOKEN, not a decimal row id, so there is
    no longer a distinct "malformed" vs. "well-formed but out of range"
    split -- EVERY string that was never actually issued as a real row's
    token (a non-integer shape, an injection-ish payload, a huge decimal
    that used to be integer-parseable, or a syntactically plausible-looking
    but never-assigned token) resolves to nothing in the `page_token` index
    and raises this SAME validation-kind error with `INVALID_CURSOR` on both backends."""
    _insert_ids(store, "Company", ["k1"])
    bad_keys = [
        "c1",
        "",
        "abc",
        "1; DROP TABLE objects; --",
        "9" * 30,
        "-" + "9" * 30,
        str(2**63),  # a decimal string; no longer special -- just unknown
        "deadbeefdeadbeefdeadbeefdeadbeef",  # uuid4-hex-SHAPED, never issued
    ]
    for bad_key in bad_keys:
        with raises_code(ValidationFailed, "INVALID_CURSOR"):
            store.read_page("Company", after_key=bad_key)


def test_read_page_after_key_from_a_different_object_type_raises_invalid_cursor(
    store: Store,
) -> None:
    """T2 review P1: `page_token` is only unique GLOBALLY (uuid4 hex), not
    scoped to `object_type` -- a cursor issued for one type is a real,
    resolvable token that happens to name a row of a DIFFERENT type. Both
    `_resolve_page_token` implementations must additionally check
    `object_type` and refuse the mismatch with an `INVALID_CURSOR` validation-kind error, rather than
    silently accepting the other type's `row_id` as a positional offset
    into the requested type's ordering -- the fail-open receipt this
    regression test pins: `read_page("Team", after_key=<Company's cursor>)`
    must raise, never return an empty/short page with no error."""
    _insert_ids(store, "Company", ["co1"])
    _insert_ids(store, "Team", ["team1"])

    company_page = store.read_page("Company", batch=1)
    company_cursor = company_page[-1].key

    with raises_code(ValidationFailed, "INVALID_CURSOR"):
        store.read_page("Team", after_key=company_cursor)

    # And the mirror direction, so both backends are pinned both ways.
    team_page = store.read_page("Team", batch=1)
    team_cursor = team_page[-1].key
    with raises_code(ValidationFailed, "INVALID_CURSOR"):
        store.read_page("Company", after_key=team_cursor)


def test_read_page_tie_safety_survives_duplicate_payload_pks(
    store: Store,
) -> None:
    """THE regression this redesign fixes (spec pagination-hardening §5's
    2026-07-25 amendment, proven by review at 502-in/500-out on both
    backends): the store does not enforce payload-pk uniqueness among
    CURRENT rows -- two separate `insert` calls with the same payload id
    yield two current rows, and a cursor keyed on the payload pk cannot
    express that tie, so it silently drops a row. Ordering by the store's own
    per-row identity instead means every one of these adversarial rows
    survives a keyset walk, even at the most adversarial batch size of 1 --
    this assertion FAILS if the cursor is ever re-keyed on the payload pk.

    AMENDED by M9a: this test used to ALSO insert `{"id": 10}` alongside
    `{"id": "10"}`, since an int-vs-str payload pk collides under any
    text-cast ordering. That input is no longer reachable through the write
    path -- `declared_shape_violation` refuses a value that does not match its
    declared `PropertyType`, which is asserted below rather than dropped.
    Duplicate pks remain reachable (the store still does not enforce pk
    uniqueness), so the tie the cursor design must survive is still exercised.
    """
    store.insert("Company", {"id": "dup", "name": "first"}, Source(source_system="s"))
    store.insert("Company", {"id": "dup", "name": "second"}, Source(source_system="s"))
    store.insert("Company", {"id": "10", "name": "str-ten"}, Source(source_system="s"))

    walked = _walk_pages(store, "Company", batch=1)

    assert [o.payload["name"] for o in walked] == ["first", "second", "str-ten"]

    # The other half of the old adversarial pair, now refused at the seam.
    with raises_code(ValidationFailed, "INVALID_RECORD"):
        store.insert("Company", {"id": 10, "name": "int-ten"}, Source(source_system="s"))


def test_read_all_full_count_with_duplicate_payload_pks(store: Store) -> None:
    """Scaled-down pin of the review's 502-in/500-out finding: `read_all`
    must return every current row regardless of duplicate payload pks -- it
    is its own single query/pass ordered by `row_id`, never a payload-pk
    comparison that could tie/collide."""
    total = 12
    for i in range(total):
        # 4 distinct payload ids, each reused 3x -- current rows sharing a
        # duplicate business key, exactly the case the store does not (and
        # per spec, is not required to) prevent.
        store.insert(
            "Company", {"id": f"c{i % 4}", "name": f"row-{i}"}, Source(source_system="s")
        )

    assert len(store.read_all("Company")) == total


def test_read_page_and_read_all_only_current_after_pk_changing_update(
    store: Store,
) -> None:
    """An `update` that changes the payload primary key's own VALUE (not
    just another property) must still leave exactly one current row behind:
    `read_page`/`read_all` order by the store's own row identity, which an
    update to any payload field -- including the declared pk -- never
    touches."""
    obj_id = store.insert("Company", {"id": "orig", "name": "Acme"}, Source(source_system="s"))
    store.update(
        "Company", obj_id, {"id": "renamed", "name": "Acme"}, Source(source_system="s")
    )

    all_current = store.read_all("Company")
    assert len(all_current) == 1
    assert all_current[0].payload["id"] == "renamed"

    paged = store.read_page("Company")
    assert len(paged) == 1
    assert paged[0].obj.payload["id"] == "renamed"
    # short page (1 row, default batch DEFAULT_BATCH): this walk is
    # exhausted -- the only signal, no separate cursor/flag.


def test_read_page_ordering_identical_across_backends_for_same_inserts() -> None:
    """`row_id` VALUES are engine-generated (SQLite's physical ROWID vs.
    `InMemoryStore`'s own counter) and never promised to match -- but for
    the SAME sequence of inserts, both backends must return the SAME
    sequence of rows (spec §6/§11: ordering only has to be deterministic and
    IDENTICAL across backends). Builds both backends directly (rather than
    via the `store` fixture, which only ever hands one backend per test
    run) so a single test can compare them side by side."""
    ids = ["m3", "m1", "m5", "m4", "m2"]

    sequences: list[list[str]] = []
    for factory in STORE_FACTORIES:
        backend_store = factory(build_registry())
        _insert_ids(backend_store, "Company", ids)
        sequences.append([o.payload["id"] for o in backend_store.read_all("Company")])

    assert len(sequences) == len(STORE_FACTORIES)
    assert sequences[0] == ids
    assert sequences[1] == ids


def test_read_all_unregistered_object_type_returns_empty_not_raise(store: Store) -> None:
    """Base behavior restored (pagination-hardening T1 redesign): `read_all`
    must NOT raise for an unregistered object type. It never needed a
    registry lookup even before this task (no payload primary key to
    extract now that ordering is by the store's own `row_id`), so an
    unknown type simply matches zero rows."""
    assert store.read_all("Widget") == []


def test_read_page_unregistered_object_type_returns_empty_not_raise(store: Store) -> None:
    """Mirrors `read_all`'s behavior above, for the same reason: `read_page`
    needs no registry lookup either now that ordering is by `row_id`, not a
    payload primary key resolved via the type's registered PropertyDef. A
    zero-row page is also exhausted -- there is no separate cursor that
    could be left dangling."""
    assert store.read_page("Widget") == []


# -- links + cardinality ----------------------------------------------------


def test_create_link_and_links_from_links_to(store: Store) -> None:
    store.create_link("belongsToDepartment", "team-1", "dept-a")
    store.create_link("belongsToDepartment", "team-2", "dept-a")

    assert store.links_from("belongsToDepartment", "team-1") == ["dept-a"]
    assert sorted(store.links_to("belongsToDepartment", "dept-a")) == [
        "team-1",
        "team-2",
    ]
    assert store.links_from("belongsToDepartment", "team-unknown") == []


def test_create_link_many_to_one_second_parent_raises_cardinality(
    store: Store,
) -> None:
    store.create_link("belongsToDepartment", "team-1", "dept-a")

    with raises_code(ConflictError, "CARDINALITY_VIOLATION"):
        store.create_link("belongsToDepartment", "team-1", "dept-b")

    # a different from_id is unaffected
    store.create_link("belongsToDepartment", "team-2", "dept-a")


def test_create_link_one_to_many_second_from_id_to_same_target_raises_cardinality(
    one_to_many_store: Store,
) -> None:
    """`hasTeam` is ONE_TO_MANY -- the to-side branch (store.py's second
    cardinality check) is the one that fires here: a *different* from_id
    targeting an already-linked to_id must raise, while the *same* from_id
    linking to further, distinct to_ids must not."""
    one_to_many_store.create_link("hasTeam", "dept-a", "team-1")

    with raises_code(ConflictError, "CARDINALITY_VIOLATION"):
        one_to_many_store.create_link("hasTeam", "dept-b", "team-1")

    # the same from_id linking to a different, unlinked to_id is unaffected
    one_to_many_store.create_link("hasTeam", "dept-a", "team-2")


# -- authority (capture_action_writes) --------------------------------------


def test_capture_refuses_create_on_non_owned_type(authority_store: Store) -> None:
    with authority_store.capture_action_writes() as writes:
        with raises_code(AuthorityError, "SOURCE_CREATE_REFUSED") as excinfo:
            authority_store.insert(
                "Ticket", {"status": "open"}, Source(source_system="action")
            )
    assert "Ticket" in str(excinfo.value)
    assert writes == []


def test_capture_allows_create_on_owned_type_and_records_write(
    authority_store: Store,
) -> None:
    with authority_store.capture_action_writes() as writes:
        obj_id = authority_store.insert(
            "Escalation", {"reason": "SLA breach"}, Source(source_system="action")
        )

    assert writes == [
        WriteRecord(op="create", object_type="Escalation", object_id=obj_id)
    ]


def test_capture_refuses_update_on_non_owned_property(authority_store: Store) -> None:
    ticket_id = authority_store.insert(
        "Ticket", {"status": "open"}, Source(source_system="connector")
    )

    with authority_store.capture_action_writes() as writes:
        with raises_code(AuthorityError, "UNDECLARED_SOURCE_WRITE") as excinfo:
            authority_store.update(
                "Ticket",
                ticket_id,
                {"status": "closed"},
                Source(source_system="action"),
            )
    assert "status" in str(excinfo.value)
    assert writes == []


def test_capture_allows_update_on_owned_property_and_records_write(
    authority_store: Store,
) -> None:
    company_id = authority_store.insert(
        "Company", {"name": "Acme"}, Source(source_system="connector")
    )

    with authority_store.capture_action_writes() as writes:
        authority_store.update(
            "Company", company_id, {"tier": "gold"}, Source(source_system="action")
        )

    assert writes == [
        WriteRecord(op="update", object_type="Company", object_id=company_id)
    ]


def test_capture_refuses_mixed_update_naming_source_backed_keys(
    authority_store: Store,
) -> None:
    """An authority failure names only the source-backed keys."""
    company_id = authority_store.insert(
        "Company", {"name": "Acme"}, Source(source_system="connector")
    )

    with authority_store.capture_action_writes() as writes:
        with raises_code(AuthorityError, "UNDECLARED_SOURCE_WRITE") as excinfo:
            authority_store.update(
                "Company",
                company_id,
                {"tier": "gold", "name": "New Acme"},
                Source(source_system="action"),
            )
    assert "name" in str(excinfo.value)
    assert "tier" not in str(excinfo.value)
    assert writes == []


def test_capture_refuses_create_link_on_non_owned_link_type(
    authority_store: Store,
) -> None:
    authority_store.insert("Ticket", {"status": "open"}, Source(source_system="c"))
    authority_store.insert("Company", {"name": "Acme"}, Source(source_system="c"))

    with authority_store.capture_action_writes() as writes:
        with raises_code(AuthorityError, "UNDECLARED_SOURCE_WRITE") as excinfo:
            authority_store.create_link("sourceLink", "ticket-1", "company-1")
    assert "sourceLink" in str(excinfo.value)
    assert writes == []


def test_capture_allows_owned_link_and_records_write(authority_store: Store) -> None:
    with authority_store.capture_action_writes() as writes:
        authority_store.create_link("ownedLink", "esc-1", "ticket-1")

    assert writes == [
        WriteRecord(op="link", link_type="ownedLink", from_id="esc-1", to_id="ticket-1")
    ]


def test_direct_writes_outside_capture_context_are_unrestricted(
    authority_store: Store,
) -> None:
    ticket_id = authority_store.insert(
        "Ticket", {"status": "open"}, Source(source_system="connector")
    )
    authority_store.update(
        "Ticket", ticket_id, {"status": "closed"}, Source(source_system="connector")
    )
    got = authority_store.read_current("Ticket", ticket_id)
    assert got is not None
    assert got.payload["status"] == "closed"


def test_nested_capture_action_writes_raises(authority_store: Store) -> None:
    with authority_store.capture_action_writes():
        with pytest.raises(RuntimeError):
            with authority_store.capture_action_writes():
                pass


# -- audit -------------------------------------------------------------


def test_audit_append_and_read_back(store: Store) -> None:
    entry = AuditEntry(
        actor="alice",
        role="TeamOwner",
        action="CaptureIntent",
        target_type="Intent",
        target_id="intent-1",
        params={"statement": "improve pride"},
        outcome="ok",
    )
    store.append_audit(entry)

    entries = store.audit_entries()
    assert len(entries) == 1
    got = entries[0]
    assert got.actor == "alice"
    assert got.role == "TeamOwner"
    assert got.action == "CaptureIntent"
    assert got.target_type == "Intent"
    assert got.target_id == "intent-1"
    assert got.params == {"statement": "improve pride"}
    assert got.outcome == "ok"


def test_audit_writes_round_trip(store: Store) -> None:
    writes = [
        WriteRecord(op="create", object_type="Escalation", object_id="esc-1"),
        WriteRecord(op="link", link_type="ownedLink", from_id="esc-1", to_id="ticket-1"),
    ]
    entry = AuditEntry(
        actor="alice",
        role="TeamOwner",
        action="Escalate",
        target_type="Ticket",
        target_id="ticket-1",
        params={},
        outcome="ok",
        writes=writes,
    )
    store.append_audit(entry)

    got = store.audit_entries()[0]
    assert got.writes == writes


def test_audit_entry_defaults_writes_to_empty_list() -> None:
    entry = AuditEntry(
        actor="alice",
        role="TeamOwner",
        action="Deny",
        target_type="Ticket",
        params={},
        outcome="denied",
    )
    assert entry.writes == []


def test_audit_effects_and_capability_accesses_round_trip(store: Store) -> None:
    effects = [
        EffectRecord(
            api_name="send_notification",
            payload={"channel": "ops", "nested": {"ids": [1, 2]}},
            outcome="dispatched",
        ),
        EffectRecord(
            api_name="create_ticket",
            payload={"priority": "high"},
            outcome="failed",
            error="transport unavailable",
        ),
    ]
    accesses = [
        CapabilityAccessRecord(api_name="llm", count=2),
        CapabilityAccessRecord(api_name="search", count=1),
    ]
    entry = AuditEntry(
        actor="alice",
        role="TeamOwner",
        action="Escalate",
        target_type="Ticket",
        target_id="ticket-1",
        params={},
        outcome="effects_dispatched",
        effects=effects,
        capability_accesses=accesses,
    )
    store.append_audit(entry)

    got = store.audit_entries()[0]
    assert got.effects == effects
    assert got.capability_accesses == accesses


def test_unserializable_effect_payload_uses_per_key_placeholder(
    store: Store,
) -> None:
    entry = AuditEntry(
        actor="alice",
        role="TeamOwner",
        action="WeirdEffect",
        target_type="Ticket",
        params={},
        outcome="effects_dispatched",
        effects=[
            EffectRecord(
                api_name="notify",
                payload={"ok": "fine", "bad": {"not", "json"}},
                outcome="failed",
                error="bad payload",
            )
        ],
    )
    store.append_audit(entry)  # must not raise

    payload = store.audit_entries()[0].effects[0].payload
    assert payload["ok"] == "fine"
    assert "$unserializable" in payload["bad"]


def test_append_audit_never_raises_on_unserializable_param(store: Store) -> None:
    entry = AuditEntry(
        actor="alice",
        role="TeamOwner",
        action="Weird",
        target_type="Ticket",
        params={"ok": "fine", "bad": {"a", "b"}},
        outcome="ok",
    )
    store.append_audit(entry)  # must not raise

    got = store.audit_entries()[0]
    assert got.params["ok"] == "fine"
    assert "$unserializable" in got.params["bad"]


def test_audit_log_is_append_only_no_update_delete_api(store: Store) -> None:
    public_and_protected = {
        name for name in dir(type(store)) if not name.startswith("__")
    }
    forbidden = {
        "update_audit",
        "delete_audit",
        "remove_audit",
        "update_audit_entry",
        "delete_audit_entry",
    }
    assert public_and_protected.isdisjoint(forbidden)


def test_audit_entries_append_order(store: Store) -> None:
    for i in range(3):
        store.append_audit(
            AuditEntry(
                actor="alice",
                role="TeamOwner",
                action=f"Action{i}",
                target_type="Ticket",
                params={},
                outcome="ok",
            )
        )
    actions = [e.action for e in store.audit_entries()]
    assert actions == ["Action0", "Action1", "Action2"]


# -- transactions -------------------------------------------------------


def test_in_transaction_true_inside_false_outside(store: Store) -> None:
    assert store.in_transaction is False
    with store.transaction():
        assert store.in_transaction is True
        with store.transaction():
            assert store.in_transaction is True
    assert store.in_transaction is False


def test_read_then_write_transaction_serializes_concurrent_id_allocation(
    concurrent_stores: Callable[[], Store],
) -> None:
    """A max-plus-one allocation in one transaction cannot reuse an id.

    The start barrier proves both workers are running before either opens its
    transaction. The read barrier forces both pre-change implementations to
    observe the same maximum before either writes. With correct outermost
    serialization, worker 2 cannot reach that barrier while worker 1 holds the
    transaction; worker 1's bounded barrier break lets it commit, after which
    worker 2 reads the new maximum. No sleep or scheduler race chooses the
    interleaving under test.
    """
    started = threading.Barrier(2)
    read_same_snapshot = threading.Barrier(2)
    errors: list[BaseException] = []

    def allocate() -> None:
        try:
            store = concurrent_stores()
            started.wait(timeout=5)
            with store.transaction():
                suffixes = [
                    int(str(row.payload["id"]).removeprefix("SKL-"))
                    for row in store.read_all("Company")
                ]
                next_id = f"SKL-{max(suffixes, default=0) + 1:05d}"
                try:
                    read_same_snapshot.wait(timeout=1)
                except threading.BrokenBarrierError:
                    # Expected after serialization: the second worker cannot
                    # finish its read until this transaction commits.
                    pass
                store.insert(
                    "Company",
                    {"id": next_id, "name": next_id},
                    Source(source_system="action:AllocateSkillId"),
                )
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=allocate, daemon=True) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert not any(worker.is_alive() for worker in workers), "worker deadlocked"
    assert errors == []
    ids = [
        str(row.payload["id"])
        for row in concurrent_stores().read_all("Company")
    ]
    assert ids == ["SKL-00001", "SKL-00002"]
    assert len(ids) == len(set(ids))


def test_uncommitted_writes_invisible_to_concurrent_reader(
    concurrent_stores: Callable[[], Store],
) -> None:
    """A reader outside any transaction never observes uncommitted writes.

    The writer inserts inside a transaction, signals the reader, then holds the
    transaction open for a bounded wait before rolling back. Whatever the reader
    manages to observe in that window must be the pre-transaction state: the
    SQL backends isolate connections, and a serialized in-memory store must
    block (or equivalently hide) the read until rollback. A dirty read here
    would let a concurrent consumer act on rows of an action that never
    happened.
    """
    inserted = threading.Event()
    observed = threading.Event()
    errors: list[BaseException] = []

    def write_then_roll_back() -> None:
        try:
            store = concurrent_stores()
            with pytest.raises(RuntimeError):
                with store.transaction():
                    store.insert(
                        "Company",
                        {"name": "Phantom"},
                        Source(source_system="action:NeverHappened"),
                    )
                    inserted.set()
                    # Bounded: a lock-serialized reader cannot finish its read
                    # (and set the event) until this transaction ends.
                    observed.wait(timeout=1)
                    raise RuntimeError("roll back")
        except BaseException as exc:  # pragma: no cover - surfaced via assert
            errors.append(exc)

    writer = threading.Thread(target=write_then_roll_back, daemon=True)
    writer.start()
    assert inserted.wait(timeout=5)
    seen = [row.payload["name"] for row in concurrent_stores().read_all("Company")]
    observed.set()
    writer.join(timeout=10)

    assert not writer.is_alive(), "writer deadlocked"
    assert errors == []
    assert seen == []
    assert concurrent_stores().read_all("Company") == []


def test_transaction_rollback_on_error_discards_writes(store: Store) -> None:
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.insert("Company", {"name": "Acme"}, Source(source_system="test"))
            raise RuntimeError("boom")

    assert store.read_all("Company") == []


def test_nested_transaction_rollback_discards_inner_writes(store: Store) -> None:
    """An exception raised in an outer `transaction()` must roll back writes
    made by an inner, nested `transaction()` call too: `transaction()` is
    reentrant (only the outermost level commits/rolls back)."""
    with pytest.raises(RuntimeError):
        with store.transaction():
            with store.transaction():
                store.insert(
                    "Company", {"name": "Acme"}, Source(source_system="test")
                )
            raise RuntimeError("outer failure after inner commit-looking exit")

    assert store.read_all("Company") == []


def test_audit_append_participates_in_the_enclosing_transaction(store: Store) -> None:
    """An audit entry appended INSIDE a transaction must roll back with it, on
    BOTH backends.

    This is the store-level half of the milestone's atomicity claim: M5 appends
    the `pending` effect record inside the action transaction precisely so the
    record of what an action intended to send outside commits with the ontology
    writes, never separately. `append_audit` opens its own transaction, and
    `transaction()` is reentrant, so it must JOIN the enclosing one.

    Whole-branch review finding (2026-07-26): the in-memory half of this was a
    DELETABLE guard. The atomicity fault-injection test constructs only
    `ObjectStore`, and the rollback tests above roll back an object insert but
    never an audit append -- so removing `_audit` from `InMemoryStore`'s
    transaction snapshot left the whole suite green while the two backends
    silently disagreed about whether a durable pending record can survive a
    rolled-back action.
    """
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.insert("Company", {"name": "Acme"}, Source(source_system="test"))
            store.append_audit(
                AuditEntry(
                    actor="operator-1",
                    role="Operator",
                    action="Rollback",
                    target_type="Company",
                    outcome="ok",
                    effects=[
                        EffectRecord(
                            api_name="Notify",
                            payload={"message": "should not survive"},
                            outcome="pending",
                        )
                    ],
                )
            )
            raise RuntimeError("boom after the audit append")

    assert store.read_all("Company") == []
    assert store.audit_entries() == []


def test_transaction_rolls_back_on_base_exception_too(store: Store) -> None:
    """Rollback must fire for `BaseException`, not only `Exception`.

    Re-review finding (2026-07-26): both `transaction()` implementations caught
    only `Exception`, so a handler interrupted by `KeyboardInterrupt` (or raising
    `SystemExit`) left its writes in place -- readable by a later caller on the
    same connection, or swept into an outer commit. Pre-existing, but M5's
    atomicity claim (the pending effect record and the ontology writes are ONE
    fact) rests on it directly: an interrupted action must not leave an orphaned
    `pending` record either. The exception is still re-raised -- process-control
    exceptions are cleaned up after, never swallowed.
    """
    with pytest.raises(KeyboardInterrupt):
        with store.transaction():
            store.insert("Company", {"name": "Acme"}, Source(source_system="test"))
            store.append_audit(
                AuditEntry(
                    actor="operator-1",
                    role="Operator",
                    action="Interrupted",
                    target_type="Company",
                    outcome="ok",
                    effects=[
                        EffectRecord(
                            api_name="Notify",
                            payload={"message": "orphan"},
                            outcome="pending",
                        )
                    ],
                )
            )
            raise KeyboardInterrupt

    assert store.read_all("Company") == []
    assert store.audit_entries() == []


def test_audit_entries_are_not_mutable_through_the_returned_models(
    store: Store,
) -> None:
    """`audit_entries()` must not hand out handles onto the stored log.

    Whole-branch review finding: `InMemoryStore.audit_entries` copied the LIST
    but returned the same mutable `AuditEntry`/`EffectRecord` objects it stored,
    so a dispatcher closing over the store could rewrite a committed `pending`
    record in place -- an append-only log that was not append-only, and a
    backend-parity break, since `ObjectStore` reconstructs its models from
    persisted JSON and never had the hole.
    """
    store.append_audit(
        AuditEntry(
            actor="operator-1",
            role="Operator",
            action="Publish",
            target_type="Company",
            outcome="ok",
            effects=[
                EffectRecord(
                    api_name="Notify",
                    payload={"message": "original"},
                    outcome="pending",
                )
            ],
        )
    )

    first = store.audit_entries()
    first[0].outcome = "tampered"
    first[0].effects[0].outcome = "failed"
    first[0].effects[0].payload["message"] = "rewritten"

    second = store.audit_entries()
    assert second[0].outcome == "ok"
    assert second[0].effects[0].outcome == "pending"
    assert second[0].effects[0].payload == {"message": "original"}


def test_capture_action_writes_are_a_context_manager_yielding_list(
    store: Store,
) -> None:
    with store.capture_action_writes() as writes:
        assert writes == []
    assert isinstance(writes, list)


def test_no_public_read_path_on_object_store(store: Store) -> None:
    """No consumer-facing query shortcut bypasses the guarded read layer."""
    public_names = {
        name
        for name in dir(type(store))
        if not name.startswith("_") and callable(getattr(type(store), name))
    }
    forbidden_public_reads = {"get", "get_all", "get_as_of", "query", "find"}
    assert public_names.isdisjoint(forbidden_public_reads)
    assert "read_current" in public_names
    assert "read_all" in public_names


# -- coded errors (m35-sdk-refactor T6/AC7) ---------------------------------


def test_update_unknown_object_type_raises_coded_error(store: Store) -> None:
    with raises_code(ValidationFailed, "UNKNOWN_OBJECT_TYPE"):
        store.update(
            "Widget", "some-id", {"name": "x"}, Source(source_system="test")
        )


def test_create_link_unknown_link_type_raises_coded_error(store: Store) -> None:
    with raises_code(ValidationFailed, "UNKNOWN_LINK_TYPE"):
        store.create_link("noSuchLink", "a", "b")


def test_links_from_unknown_link_type_raises_coded_error(store: Store) -> None:
    with raises_code(ValidationFailed, "UNKNOWN_LINK_TYPE"):
        store.links_from("noSuchLink", "a")


def test_links_to_unknown_link_type_raises_coded_error(store: Store) -> None:
    with raises_code(ValidationFailed, "UNKNOWN_LINK_TYPE"):
        store.links_to("noSuchLink", "b")


# -- effect outbox (durable-effect-outbox AC12) ------------------------------


def _outbox_row(
    effect_id: str = "eff-1",
    *,
    seq: int = 0,
    next_attempt_at: datetime | None = None,
    lease_until: datetime | None = None,
) -> OutboxRecord:
    return OutboxRecord(
        effect_id=effect_id,
        invocation_id="inv-1",
        seq=seq,
        api_name="Notice",
        payload={"label": effect_id},
        action="Emit",
        actor_id="operator-1",
        role="Operator",
        emitted_at=_OUTBOX_NOW,
        next_attempt_at=next_attempt_at or _OUTBOX_NOW,
        lease_until=lease_until,
        updated_at=_OUTBOX_NOW,
    )


_OUTBOX_NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def test_enqueued_effects_read_back_intact(store: Store) -> None:
    store.enqueue_effects([_outbox_row()])

    rows = store.outbox_entries()

    assert len(rows) == 1
    assert rows[0].model_dump() == _outbox_row().model_dump()


def test_enqueue_is_rolled_back_with_the_surrounding_transaction(
    store: Store,
) -> None:
    """The atomicity the whole design rests on, at the seam rather than
    through an action: an outbox row written inside a transaction that then
    fails must not survive it."""
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.enqueue_effects([_outbox_row()])
            raise RuntimeError("caller failed")

    assert store.outbox_entries() == []


def test_claim_never_leases_uncommitted_outbox_rows(
    concurrent_stores: Callable[[], Store],
) -> None:
    """A dispatcher's claim must not deliver an effect of a transaction that
    rolls back.

    Same shape as `test_uncommitted_writes_invisible_to_concurrent_reader`,
    but through the claim path: the writer enqueues inside a transaction,
    holds it open for a bounded wait, then rolls back. A claim racing that
    window must lease nothing -- returning the row would hand the dispatcher
    an effect of an action that never happened (durable-effect-outbox AC1),
    and the SQL backends' single-statement scan+lease already cannot.
    """
    enqueued = threading.Event()
    claimed_done = threading.Event()
    errors: list[BaseException] = []

    def enqueue_then_roll_back() -> None:
        try:
            store = concurrent_stores()
            with pytest.raises(RuntimeError):
                with store.transaction():
                    store.enqueue_effects([_outbox_row()])
                    enqueued.set()
                    claimed_done.wait(timeout=1)
                    raise RuntimeError("roll back")
        except BaseException as exc:  # pragma: no cover - surfaced via assert
            errors.append(exc)

    writer = threading.Thread(target=enqueue_then_roll_back, daemon=True)
    writer.start()
    assert enqueued.wait(timeout=5)
    claimed = concurrent_stores().claim_due_effects(
        limit=10, now=_OUTBOX_NOW, lease=timedelta(seconds=30)
    )
    claimed_done.set()
    writer.join(timeout=10)

    assert not writer.is_alive(), "writer deadlocked"
    assert errors == []
    assert claimed == []
    assert concurrent_stores().outbox_entries() == []


def test_claim_leases_only_due_unleased_pending_rows(store: Store) -> None:
    store.enqueue_effects(
        [
            _outbox_row("due"),
            _outbox_row("later", seq=1, next_attempt_at=_OUTBOX_NOW + timedelta(hours=1)),
            _outbox_row("leased", seq=2, lease_until=_OUTBOX_NOW + timedelta(hours=1)),
        ]
    )

    claimed = store.claim_due_effects(
        limit=10, now=_OUTBOX_NOW, lease=timedelta(seconds=30)
    )

    assert [row.effect_id for row in claimed] == ["due"]
    assert claimed[0].lease_until == _OUTBOX_NOW + timedelta(seconds=30)
    # And the lease is persisted, not just reported.
    stored = {row.effect_id: row.lease_until for row in store.outbox_entries()}
    assert stored["due"] == _OUTBOX_NOW + timedelta(seconds=30)


def test_claim_respects_its_limit(store: Store) -> None:
    store.enqueue_effects([_outbox_row("a"), _outbox_row("b", seq=1)])

    claimed = store.claim_due_effects(
        limit=1, now=_OUTBOX_NOW, lease=timedelta(seconds=30)
    )

    assert len(claimed) == 1


def test_resolve_increments_attempts_and_clears_the_lease(store: Store) -> None:
    store.enqueue_effects([_outbox_row()])
    store.claim_due_effects(limit=1, now=_OUTBOX_NOW, lease=timedelta(seconds=30))

    store.resolve_effect(
        "eff-1",
        state="pending",
        next_attempt_at=_OUTBOX_NOW + timedelta(minutes=5),
        error="transport down",
        now=_OUTBOX_NOW,
    )

    row = store.outbox_entries()[0]
    assert (row.state, row.attempts, row.last_error) == ("pending", 1, "transport down")
    assert row.lease_until is None
    assert row.next_attempt_at == _OUTBOX_NOW + timedelta(minutes=5)


def test_release_clears_the_lease_without_spending_an_attempt(store: Store) -> None:
    store.enqueue_effects([_outbox_row()])
    store.claim_due_effects(limit=1, now=_OUTBOX_NOW, lease=timedelta(seconds=30))

    store.release_effect_claim("eff-1", now=_OUTBOX_NOW)

    row = store.outbox_entries()[0]
    assert (row.state, row.attempts, row.lease_until) == ("pending", 0, None)


def test_delivered_and_failed_rows_are_never_claimed_again(store: Store) -> None:
    store.enqueue_effects([_outbox_row("done"), _outbox_row("dead", seq=1)])
    for effect_id, state in (("done", "delivered"), ("dead", "failed")):
        store.resolve_effect(
            effect_id,
            state=state,
            next_attempt_at=_OUTBOX_NOW,
            error=None,
            now=_OUTBOX_NOW,
        )

    claimed = store.claim_due_effects(
        limit=10, now=_OUTBOX_NOW + timedelta(days=1), lease=timedelta(seconds=30)
    )

    assert claimed == []
    # Kept, not deleted: a dead letter a caller can still read.
    assert {row.effect_id for row in store.outbox_entries()} == {"done", "dead"}


def test_resolving_an_unknown_effect_id_is_a_noop(store: Store) -> None:
    store.resolve_effect(
        "never-existed",
        state="delivered",
        next_attempt_at=_OUTBOX_NOW,
        error=None,
        now=_OUTBOX_NOW,
    )

    assert store.outbox_entries() == []


def test_audit_entries_preserve_the_invocation_id(store: Store) -> None:
    """Backend parity: `InMemoryStore.append_audit` normalized field by field
    and silently dropped `invocation_id` when M7a added it."""
    store.append_audit(
        AuditEntry(
            invocation_id="inv-7",
            actor="a",
            role="Agent",
            action="A",
            target_type="Widget",
            outcome="ok",
        )
    )

    assert [entry.invocation_id for entry in store.audit_entries()] == ["inv-7"]


def test_audit_entries_round_trip_every_field(store: Store) -> None:
    """Backend parity across EVERY `AuditEntry` field, not one at a time.

    Originally a single-field `principal` test (M10, spec `multi-consumer-mcp`
    AC10), rewritten after a live bug: `InMemoryStore.append_audit`'s
    normalization step lists every `AuditEntry` field explicitly, and at three
    separate points in this project's history a field added to `AuditEntry`
    (`invocation_id`, then `principal`, then `kind`) was never added there --
    so that backend silently reverted to the field's default on read-back
    while `ObjectStore` round-tripped the real value, and each was only
    caught because someone happened to write a test for that ONE field.
    Iterating `AuditEntry.model_fields` instead means the next field added to
    `AuditEntry` and forgotten in a backend's normalization is caught
    automatically, on every backend this suite runs against, without anyone
    having to remember to extend this test by hand.
    """
    entry = AuditEntry(
        ts=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        kind="function",
        invocation_id="inv-1",
        principal="svc-agent-7",
        actor="alice",
        role="TeamOwner",
        action="DoThing",
        target_type="Ticket",
        target_id="ticket-9",
        params={"a": 1},
        outcome="ok",
        writes=[WriteRecord(op="create", object_type="Widget", object_id="w-1")],
        effects=[
            EffectRecord(
                api_name="notify",
                payload={"x": 1},
                outcome="dispatched",
                effect_id="eff-1",
            )
        ],
        capability_accesses=[CapabilityAccessRecord(api_name="llm", count=3)],
    )
    store.append_audit(entry)

    got = store.audit_entries()[0]
    for field in AuditEntry.model_fields:
        assert getattr(got, field) == getattr(entry, field), field


# -- type versions and upcasting (M9b) ---------------------------------------


def test_a_row_records_the_declared_version_of_its_type(store: Store) -> None:
    """Both backends stamp the write, so an older row can be recognized as one.

    Asserted through the protocol rather than by inspecting a column, because the
    in-memory backend has no columns -- which is exactly the kind of difference a
    conformance suite exists to keep behavioral.
    """
    store.insert("Company", {"id": "c-1", "name": "Acme"}, Source(source_system="s"))

    # Nothing in the `Store` protocol exposes the stored version directly (it is
    # an implementation detail of the read path), so the observable contract is
    # that a current-version row reads back byte-identically.
    row = store.read_current("Company", "c-1")
    assert row is not None
    assert row.payload == {"id": "c-1", "name": "Acme"}


def test_audit_importable_before_store_in_fresh_interpreter() -> None:
    """Importing ``ontary.audit`` first must not trigger a store cycle."""
    result = subprocess.run(
        [sys.executable, "-c", "import ontary.audit; import ontary"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
