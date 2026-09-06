"""SQLite-specific tests for :class:`ontary.store.ObjectStore`."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import raises_code

from ontary.errors import ConflictError
from ontary.meta import Cardinality, LinkTypeDef, OntologyRegistry
from ontary.store import ObjectStore, Store

RegistryFactory = Callable[..., OntologyRegistry]
StoreFactory = Callable[..., Store]


@pytest.fixture
def sqlite_registry(make_registry: RegistryFactory) -> OntologyRegistry:
    return make_registry(
        "Company",
        "Team",
        "Department",
        link_types=[
            LinkTypeDef(
                api_name="belongsToDepartment",
                from_type="Team",
                to_type="Department",
                cardinality=Cardinality.MANY_TO_ONE,
                description="Team belongs to a Department",
            )
        ],
    )


@pytest.mark.parametrize(
    ("kwargs", "expected_ms"),
    [({}, 5_000), ({"busy_timeout": 0.125}, 125)],
    ids=["default", "override"],
)
def test_object_store_busy_timeout_configures_sqlite_connection(
    tmp_path: Path,
    sqlite_registry: OntologyRegistry,
    make_store: StoreFactory,
    kwargs: dict[str, float],
    expected_ms: int,
) -> None:
    db_path = str(tmp_path / "busy-timeout.db")
    store = (
        ObjectStore(sqlite_registry, db_path)
        if not kwargs
        else make_store(sqlite_registry, db_path, **kwargs)
    )
    assert isinstance(store, ObjectStore)

    configured_ms = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert configured_ms == expected_ms


def test_blocked_writer_raises_coded_store_busy_error(
    tmp_path: Path,
    sqlite_registry: OntologyRegistry,
    make_store: StoreFactory,
) -> None:
    db_path = str(tmp_path / "blocked-writer.db")
    holder = make_store(sqlite_registry, db_path)
    blocked = make_store(sqlite_registry, db_path, busy_timeout=0.0)
    assert isinstance(holder, ObjectStore)
    assert isinstance(blocked, ObjectStore)

    with holder.transaction():
        with raises_code(ConflictError, "STORE_BUSY") as exc_info:
            with blocked.transaction():
                pass

    assert exc_info.value.kind == "conflict"


def test_hot_path_indexes_exist(
    sqlite_registry: OntologyRegistry, make_store: StoreFactory
) -> None:
    """The SQLite schema indexes every point-lookup column."""
    store = make_store(sqlite_registry)
    assert isinstance(store, ObjectStore)

    names = {
        row["name"]
        for table in ("objects", "links")
        for row in store._conn.execute(f"PRAGMA index_list({table})").fetchall()
    }
    assert {"idx_objects_type_id", "idx_links_from", "idx_links_to"} <= names
