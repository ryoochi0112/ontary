"""The SQLite schema: `SCHEMA_VERSION`, the DDL, and its introspection.

Moved verbatim from the original `ontary/store.py` (B2 of the staged
refactor). `_schema_columns` still derives the expected column set by
executing `_SCHEMA_SQL` itself -- the expectation IS the DDL, never a
hand-maintained second list.
"""

from __future__ import annotations

import sqlite3

from ontary.store import _sql

SCHEMA_VERSION = 9
"""The `objects` table has a `page_token` column; `audit_log` has `effects`,
`capability_accesses`, `invocation_id`, `kind` and `principal` columns; and
the `effect_outbox` and `ontology_fingerprint` tables exist, `objects` has a
`type_version` column, and `objects`/`links`/`audit_log`/`effect_outbox` each
have a `tenant` column. v4 added a TABLE (spec `durable-effect-outbox`
AC11 -- purely additive, which is why its migration is a create rather than a
backfill); v5 added `audit_log.kind`, backfilled `'action'` because every row
predating function auditing really was one; v6 added `ontology_fingerprint`
(another additive table -- see `ontary.fingerprint`); v7 added
`objects.type_version`, backfilled with each type's CURRENTLY declared version --
which is sound rather than invented, because a store only opens when its
fingerprint matches the declaration, so those rows really are at that version
(see `ObjectStore._migrate_to_current_version`); v8 added `tenant` to every
row-bearing table, backfilled `DEFAULT_TENANT`; v9 added `audit_log.principal`
(spec `multi-consumer-mcp` AC10), nullable with NO default because a
historical row genuinely means "we do not know who authenticated" -- unlike
`kind`/`tenant`, no backfill value would be a recorded fact rather than an
invented one. Stamped into a SQLite file's
own `PRAGMA user_version`
by `ObjectStore.__init__` (see `ObjectStore._init_schema`) so a file written
by a *different* ontary schema shape is recognized rather than silently
opened and left to fail as a confusing SQL error the first time a query
touches a column/index that isn't there. Bump this, and extend
`ObjectStore._init_schema`'s migration, the next time the `objects`/`links`/
`audit_log` shape changes (spec pagination-hardening §4's migration
carve-out: this constant plus the ONE in-place upgrade its own introduction
requires -- not a general migration framework)."""


_CORE_SCHEMA_SQL = _sql.render_schema("sqlite", table_specs=_sql.CORE_TABLE_SPECS)


_EFFECT_OUTBOX_SQL = _sql.render_schema("sqlite", table_specs=_sql.EFFECT_OUTBOX_TABLE_SPECS)


_ONTOLOGY_FINGERPRINT_SQL = _sql.render_schema(
    "sqlite", table_specs=_sql.ONTOLOGY_FINGERPRINT_TABLE_SPECS
)


_SCHEMA_SQL = _sql.render_schema("sqlite")

_SCHEMA_TABLES = (
    "objects",
    "links",
    "audit_log",
    "effect_outbox",
    "ontology_fingerprint",
)
"""Every table `_SCHEMA_SQL` declares, in the order
`ObjectStore._check_legacy_schema_compatible` inspects them."""


_SELF_MIGRATED_TABLES = frozenset({"effect_outbox", "ontology_fingerprint"})
"""Tables whose ENTIRE ABSENCE this engine knows how to repair, by creating
them (`ObjectStore._migrate_to_current_version`). Every other table in
`_SCHEMA_TABLES` missing from a stamped file is a `ConflictError` with code
`STORE_SCHEMA_INCOMPATIBLE`:
creating `objects` under a current stamp would silently present an empty
ontology as a complete one, whereas an absent `effect_outbox` genuinely means
"written before v4", with no data to lose."""


def _split_sql_statements(script: str) -> list[str]:
    """Split a DDL script into individual statements so they can run INSIDE an
    already-open transaction.

    `sqlite3.Connection.executescript` cannot be used there: it issues an
    implicit `COMMIT` before running the script, which would commit a
    half-finished migration and defeat the all-or-nothing stamp discipline
    `_migrate_to_current_version` depends on. Splitting is safe for THIS
    input specifically -- `_EFFECT_OUTBOX_SQL` is a fixed literal of
    `CREATE TABLE`/`CREATE INDEX` statements with no string literals, so a
    semicolon can only ever be a statement terminator. It is a helper for the
    engine's own DDL, never a general SQL parser, and must not be pointed at
    caller-supplied SQL."""
    statements = []
    for chunk in script.split(";"):
        body = "\n".join(
            line for line in chunk.splitlines() if not line.strip().startswith("--")
        ).strip()
        if body:
            statements.append(body)
    return statements


def _schema_columns() -> dict[str, frozenset[str]]:
    """The column set this engine's own DDL (`_SCHEMA_SQL`) declares for
    each of `_SCHEMA_TABLES`, computed by literally executing that same
    script against a throwaway `:memory:` connection and reading `PRAGMA
    table_info` back -- never a parallel, hand-maintained column list. That
    would be exactly the kind of duplicate that silently rots the next time
    `_SCHEMA_SQL` changes and nobody remembers to update a second copy; this
    function cannot drift because it IS the DDL, re-executed.

    Cheap: one in-process `:memory:` connection, created and torn down per
    call. Used by both legacy and versioned compatibility checks. The
    legacy check is reached from `ObjectStore.__init__`'s
    `user_version == 0` branch (`_init_schema` case (a)/(b)) -- which is
    NOT the rare on-disk
    legacy-file path alone: a `:memory:` database always starts at
    `user_version == 0` (nothing on disk to have stamped it previously), so
    this runs on EVERY `ObjectStore(":memory:")` construction, not only
    spec AC11's rare legacy-open case. Measured cost is negligible either
    way: ~0.33ms per call (~1ms added per `ObjectStore(":memory:")`
    construction), moving the full test suite's wall time from ~3.07s to
    ~3.12s. Never per-query."""
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.executescript(_SCHEMA_SQL)
        return {
            table: frozenset(row[1] for row in scratch.execute(f"PRAGMA table_info({table})"))
            for table in _SCHEMA_TABLES
        }
    finally:
        scratch.close()
