"""The SQLite schema gate, as a mixin for `ObjectStore`.

The only place this package issues SQLite DDL. There is no migration ladder:
a file is either at `SCHEMA_VERSION` (open it), brand new (create it), or
refused. `ObjectStore` subclasses this so schema creation stays a real method
dispatched through `self`.
"""

from __future__ import annotations

import sqlite3

from ontary.errors import ConflictError
from ontary.meta import OntologyRegistry
from ontary.store.schema import _SCHEMA_SQL, SCHEMA_VERSION


class SqliteSchemaGate:
    """Schema-creation half of `ObjectStore`.

    Annotations only, no state: `ObjectStore.__init__` owns `_conn` and
    `_registry`; this mixin borrows them.
    """

    _conn: sqlite3.Connection
    _registry: OntologyRegistry

    def _init_schema(self) -> None:
        """Create the schema at `SCHEMA_VERSION`, or refuse a file this engine
        did not write, before any other query touches `self._conn`.

        Exactly three outcomes, from the file's own `PRAGMA user_version`:

        (a) `user_version == SCHEMA_VERSION` -- this engine's own file, open it.
        (b) `user_version == 0` and no `objects` table -- a brand new file
            (including `:memory:`, always this case): create every table/index
            fresh, then stamp `SCHEMA_VERSION`.
        (c) anything else -- refused with a `ConflictError` coded
            `STORE_VERSION_UNSUPPORTED`, naming both versions. That covers a
            stamp above or below this engine's, and an unstamped file that
            already has tables: adopting either would be the "lying stamp"
            this gate exists to rule out, since a file whose shape this engine
            cannot read must not carry a stamp claiming it can.

        Mirrors the Postgres backend's `_init_schema` deliberately: both
        backends refuse rather than migrate, so an operator moving between
        ontary versions gets one answer, not two.
        """
        user_version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version == SCHEMA_VERSION:
            return
        objects_exists = (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'objects'"
            ).fetchone()
            is not None
        )
        if user_version == 0:
            if not objects_exists:
                self._create_schema()
                return
            raise ConflictError(
                "this store file already has an `objects` table but no ontary "
                f"schema stamp (expected {SCHEMA_VERSION} in `PRAGMA "
                "user_version`); refusing to adopt a schema this engine did not "
                "create",
                code="STORE_VERSION_UNSUPPORTED",
            )
        raise ConflictError(
            f"store file schema version {user_version} is not supported by this "
            f"engine (engine SCHEMA_VERSION={SCHEMA_VERSION}); this backend ships "
            "without a migration ladder -- point it at a file created by a "
            "matching ontary version, or create a fresh one and migrate the data "
            "yourself",
            code="STORE_VERSION_UNSUPPORTED",
        )

    def _create_schema(self) -> None:
        """Create every table/index and stamp the file, as ONE transaction.

        The stamp is inside the same `BEGIN IMMEDIATE` as the DDL (SQLite's DDL
        and `PRAGMA user_version` are both transactional), so a process killed
        mid-create leaves the file exactly as it was found -- never a set of
        tables carrying no stamp, which the gate above would then refuse
        forever."""
        try:
            self._conn.executescript(
                f"BEGIN IMMEDIATE;\n{_SCHEMA_SQL}\n"
                f"PRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
            )
        except Exception:
            self._conn.rollback()
            raise
