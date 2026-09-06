"""The SQLite schema gate: create at `SCHEMA_VERSION`, or refuse.

There is no migration ladder. A file this engine did not write at exactly
`SCHEMA_VERSION` is refused with `STORE_VERSION_UNSUPPORTED`, the same coded
refusal the Postgres backend gives, rather than migrated or adopted.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from ontary.errors import ConflictError
from ontary.meta import OntologyRegistry
from ontary.store import ObjectStore
from ontary.store.schema import SCHEMA_VERSION

RegistryFactory = Callable[..., OntologyRegistry]


def test_fresh_file_is_created_and_stamped(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    path = tmp_path / "s.sqlite"
    ObjectStore(make_registry("T"), str(path))
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("SELECT 1 FROM objects LIMIT 0").fetchall() == []


def test_older_stamp_is_refused(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE objects (id TEXT)")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    conn.commit()
    conn.close()
    with pytest.raises(ConflictError) as exc:
        ObjectStore(make_registry("T"), str(path))
    assert exc.value.code == "STORE_VERSION_UNSUPPORTED"


def test_unstamped_file_with_tables_is_refused(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    path = tmp_path / "foreign.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE objects (id TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(ConflictError) as exc:
        ObjectStore(make_registry("T"), str(path))
    assert exc.value.code == "STORE_VERSION_UNSUPPORTED"
