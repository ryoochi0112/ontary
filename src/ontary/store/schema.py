"""The SQLite schema: `SCHEMA_VERSION` and the DDL that creates it.

The DDL is rendered from the shared table specs (`ontary.store._sql`), so
SQLite and Postgres cannot drift apart into two hand-maintained schemas.
"""

from __future__ import annotations

from ontary.store import _sql

SCHEMA_VERSION = 10
"""The DDL shape this engine writes, stamped into a SQLite file's own `PRAGMA
user_version`; a file carrying a different stamp is refused rather than
migrated (`ObjectStore._init_schema`)."""


_SCHEMA_SQL = _sql.render_schema("sqlite")
