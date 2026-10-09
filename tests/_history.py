"""Count every retained object version through each backend's representation."""

from __future__ import annotations

from ontary.store._core import StoreCore
from ontary.store.inmemory import InMemoryStore
from ontary.store.postgres import PostgresStore
from ontary.store.sqlite import ObjectStore


def history_rows(store: StoreCore, obj_type: str, obj_id: str) -> int:
    """Count live and closed rows of this object in the store's tenant."""
    if isinstance(store, InMemoryStore):
        return sum(
            row["object_type"] == obj_type
            and row["id"] == obj_id
            and row["tenant"] == store._tenant
            for row in store._objects
        )
    if isinstance(store, ObjectStore):
        return int(
            store._conn.execute(
                "SELECT COUNT(*) FROM objects WHERE object_type = ? AND id = ? AND tenant = ?",
                (obj_type, obj_id, store._tenant),
            ).fetchone()[0]
        )
    assert isinstance(store, PostgresStore)
    with store._conn.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM objects WHERE object_type = %s AND id = %s AND tenant = %s",
            (obj_type, obj_id, store._tenant),
        )
        return int(cursor.fetchone()[0])
