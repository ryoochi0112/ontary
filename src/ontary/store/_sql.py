"""Internal SQL vocabulary shared by the SQLite and Postgres stores.

The explicit column lists and write-side statement templates here are the
single source for both SQL backends.  ``render`` supplies each backend's
placeholder syntax without touching either backend's on-disk schema, and
``execute`` makes their cursor differences invisible to callers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

Dialect = Literal["sqlite", "postgres"]
DialectText = str | Mapping[Dialect, str]


# These are the explicit statement columns.  Auto-generated identity columns
# such as audit_log.seq are intentionally absent: no statement below writes or
# selects them.
OBJECT_COLUMNS: tuple[str, ...] = (
    "row_id",
    "object_type",
    "id",
    "payload",
    "valid_from",
    "valid_to",
    "source_system",
    "source_id",
    "extracted_at",
    "page_token",
    "type_version",
)

AUDIT_LOG_COLUMNS: tuple[str, ...] = (
    "ts",
    "actor",
    "role",
    "action",
    "target_type",
    "target_id",
    "params",
    "outcome",
    "writes",
    "effects",
    "capability_accesses",
    "invocation_id",
    "kind",
    "tenant",
    "principal",
)

EFFECT_OUTBOX_COLUMNS: tuple[str, ...] = (
    "effect_id",
    "invocation_id",
    "seq",
    "api_name",
    "payload",
    "action",
    "actor_id",
    "role",
    "emitted_at",
    "state",
    "attempts",
    "next_attempt_at",
    "lease_until",
    "last_error",
    "updated_at",
    "tenant",
)
EFFECT_OUTBOX_ROW_COLUMNS: tuple[str, ...] = EFFECT_OUTBOX_COLUMNS[:-1]

ONTOLOGY_FINGERPRINT_COLUMNS: tuple[str, ...] = (
    "id",
    "digest",
    "types",
    "first_seen",
    "adopted",
    "versions",
)
ONTOLOGY_FINGERPRINT_READ_COLUMNS: tuple[str, ...] = (
    ONTOLOGY_FINGERPRINT_COLUMNS[1],
    ONTOLOGY_FINGERPRINT_COLUMNS[2],
    ONTOLOGY_FINGERPRINT_COLUMNS[5],
)

_AUDIT_LOG_COLUMN_LIST = ", ".join(AUDIT_LOG_COLUMNS)
_OBJECT_COLUMN_LIST = ", ".join(OBJECT_COLUMNS)
_EFFECT_OUTBOX_COLUMN_LIST = ", ".join(EFFECT_OUTBOX_COLUMNS)
_EFFECT_OUTBOX_ROW_COLUMN_LIST = ", ".join(EFFECT_OUTBOX_ROW_COLUMNS)
_ONTOLOGY_FINGERPRINT_COLUMN_LIST = ", ".join(ONTOLOGY_FINGERPRINT_COLUMNS)
_ONTOLOGY_FINGERPRINT_READ_COLUMN_LIST = ", ".join(ONTOLOGY_FINGERPRINT_READ_COLUMNS)
LINKS_FROM_COLUMNS: tuple[str, ...] = ("to_id",)
LINKS_TO_COLUMNS: tuple[str, ...] = ("from_id",)
_LINKS_FROM_COLUMN_LIST = ", ".join(LINKS_FROM_COLUMNS)
_LINKS_TO_COLUMN_LIST = ", ".join(LINKS_TO_COLUMNS)


@dataclass(frozen=True)
class ColumnSpec:
    """One table column, including dialect-specific spelling and comments."""

    name: str
    type_spelling: DialectText
    constraints: DialectText = ""
    prefix: DialectText = ""


@dataclass(frozen=True)
class IndexSpec:
    """One table index and its dialect-specific layout details."""

    name: str
    table: str
    columns: tuple[str, ...]
    unique: bool = False
    prefix: DialectText = ""
    on_newline: bool | Mapping[Dialect, bool] = False
    sqlite_order: int = 0
    postgres_order: int = 0


@dataclass(frozen=True)
class TableSpec:
    """A table and all indexes belonging to it in the shared schema."""

    name: str
    columns: tuple[ColumnSpec, ...]
    indexes: tuple[IndexSpec, ...] = ()
    constraints: tuple[DialectText, ...] = ()
    prefix: DialectText = ""
    dialects: frozenset[Dialect] = frozenset(("sqlite", "postgres"))
    section: str = "core"
    sqlite_order: int = 0
    postgres_order: int = 0
    postgres_column_order: tuple[str, ...] = ()


_OBJECTS_PREFIX: DialectText = {
    "sqlite": (
        "\n"
        "-- `page_token` (pagination-hardening T2 amendment, spec §5): a\n"
        "-- fresh uuid4 hex written on EVERY row insert (including the\n"
        "-- new version `update`'s close-old-insert-new writes) -- the\n"
        "-- opaque cursor `read_page` hands out, resolved back to\n"
        "-- `row_id` via `idx_objects_page_token` below. This IS a schema\n"
        "-- change from the T1 shape; T4's `SCHEMA_VERSION` stamp must\n"
        "-- cover it (see that task).\n"
    ),
    "postgres": "\n",
}
_TYPE_VERSION_PREFIX: DialectText = {
    "sqlite": (
        "    -- Which version of the declared type this row's payload was written\n"
        "    -- under (M9b). Read by `ontary.upcast.upcast_payload` on every read;\n"
        "    -- written from the declaration on every insert/update.\n"
    ),
    "postgres": "",
}
_TENANT_COLUMN_PREFIX: DialectText = {
    "sqlite": (
        "    -- Which tenant owns this row (M8b). NOT NULL with a default so the\n"
        "    -- single-tenant case needs no ceremony and a migrated row is never\n"
        '    -- ownerless: a NULL tenant would read as "belongs to everybody",\n'
        "    -- which is precisely wrong for an isolation column.\n"
    ),
    "postgres": "",
}
_AUDIT_PRINCIPAL_PREFIX: DialectText = {
    "sqlite": (
        "    -- The principal that authenticated the call this entry audits (M10,\n"
        "    -- spec `multi-consumer-mcp` AC8/AC10). NULL for every pre-M10 row and\n"
        '    -- for a redelivered outbox effect (§4.2) -- both mean "we do not know\n'
        '    -- who authenticated", never "nobody did", so no default is correct here.\n'
    ),
    "postgres": "",
}
_OBJECTS_INDEX_PREFIX: DialectText = {
    "sqlite": (
        "\n"
        "-- Indexes for the point-lookup access patterns every read goes\n"
        "-- through: `read_current`/`read_all` (object_type[, id]) and `links_from`/\n"
        "-- `links_to` (link_type + from_id/to_id). Without these, SQLite\n"
        "-- full-scans `objects`/`links` on EVERY lookup -- invisible at\n"
        "-- fixture scale (tens of rows) but O(n^2) once a real ingest puts\n"
        "-- tens of thousands of objects/links in the store.\n"
        "-- Non-behavioral: pure read speedup.\n"
    ),
    "postgres": "\n",
}
_OBJECTS_PAGE_INDEX_PREFIX: DialectText = {
    "sqlite": (
        "\n"
        "-- `read_page`/`read_all` filter by `object_type` AND order by\n"
        "-- `row_id` on every page. `row_id` IS the table's physical\n"
        "-- SQLite ROWID (the `INTEGER PRIMARY KEY` alias), but that alone\n"
        "-- does NOT make `object_type = ? ... ORDER BY row_id` free:\n"
        "-- without a compound index covering both columns, SQLite must\n"
        "-- still pick a plan (typically `idx_objects_type_id` above, or a\n"
        "-- full scan) to satisfy the `object_type` filter and then sort\n"
        "-- the matches with a temp b-tree to satisfy `ORDER BY row_id` --\n"
        "-- a per-page sort that makes a full keyset walk O(n^2/batch),\n"
        "-- not O(n) (measured 2612ms vs 116ms at 80k rows with this index\n"
        "-- present). This index lets SQLite satisfy the filter AND the\n"
        "-- order in one pass with zero sort step.\n"
    ),
    "postgres": "",
}
_PAGE_TOKEN_INDEX_PREFIX: DialectText = {
    "sqlite": (
        "\n"
        "-- `read_page`'s `after_key` -> `row_id` resolution (see\n"
        "-- the `INVALID_CURSOR` validation rule) is a single indexed point lookup\n"
        "-- on `page_token`, never a scan -- UNIQUE also gives SQLite's\n"
        "-- own constraint enforcement for free (a `page_token` collision\n"
        "-- -- practically impossible for uuid4 -- raises on INSERT\n"
        "-- rather than silently letting two rows share a cursor).\n"
    ),
    "postgres": "",
}
_OUTBOX_PREFIX: DialectText = {
    "sqlite": (
        "\n"
        "-- The durable effect outbox (spec `durable-effect-outbox` AC1/AC2).\n"
        "-- Deliberately NOT more `audit_log` columns: delivery state\n"
        "-- CHANGES (attempts, next_attempt_at, lease_until), and the audit\n"
        "-- log is append-only. Rows are written inside the emitting\n"
        "-- action's own transaction, so they commit with the ontology\n"
        "-- writes or not at all, and are mutated only by this store's\n"
        "-- claim/release/resolve methods.\n"
    ),
    "postgres": "\n",
}
_OUTBOX_SEQUENCE_INDEX_PREFIX: DialectText = {
    "sqlite": (
        "\n"
        "-- Emission order is unique per invocation: two rows sharing an\n"
        "-- (invocation_id, seq) would mean one emission was recorded\n"
        "-- twice, so SQLite refuses the second rather than leaving a\n"
        "-- duplicate delivery for the recipient to discover.\n"
    ),
    "postgres": "",
}
_OUTBOX_DUE_INDEX_PREFIX: DialectText = {
    "sqlite": (
        "\n"
        "-- `claim_due_effects` filters `state = 'pending' AND\n"
        "-- next_attempt_at <= ?` on every drain. Without this index that is\n"
        "-- a full scan of a table whose delivered/failed rows are kept\n"
        "-- forever (a dead letter a caller can still read is worth more\n"
        "-- than a small table).\n"
    ),
    "postgres": "",
}
_FINGERPRINT_PREFIX: DialectText = {
    "sqlite": (
        "\n"
        "-- Which ontology definition this store's rows were written under\n"
        "-- (spec `ontology-evolution` AC2). One row, ever: `id = 1`.\n"
        "-- Before this table, nothing recorded the SHAPE a row was written\n"
        '-- under, so no reader could tell "this row predates the change"\n'
        '-- from "this row is wrong" -- which is the only distinction that\n'
        "-- makes a migration possible.\n"
    ),
    "postgres": "\n",
}


TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec(
        name="schema_meta",
        columns=(
            ColumnSpec("key", "TEXT", "PRIMARY KEY"),
            ColumnSpec("value", "TEXT", "NOT NULL"),
        ),
        dialects=frozenset(("postgres",)),
        prefix="\n",
        section="meta",
        postgres_order=0,
    ),
    TableSpec(
        name="objects",
        columns=(
            ColumnSpec(
                "row_id",
                {
                    "sqlite": "INTEGER PRIMARY KEY AUTOINCREMENT",
                    "postgres": "BIGSERIAL PRIMARY KEY",
                },
            ),
            ColumnSpec("object_type", "TEXT", "NOT NULL"),
            ColumnSpec("id", "TEXT", "NOT NULL"),
            ColumnSpec("payload", "TEXT", "NOT NULL"),
            ColumnSpec("valid_from", "TEXT", "NOT NULL"),
            ColumnSpec("valid_to", "TEXT", "NULL"),
            ColumnSpec("source_system", "TEXT", "NOT NULL"),
            ColumnSpec("source_id", "TEXT", "NULL"),
            ColumnSpec("extracted_at", "TEXT", "NULL"),
            ColumnSpec("page_token", "TEXT", "NOT NULL"),
            ColumnSpec("type_version", "INTEGER", "NOT NULL DEFAULT 1", _TYPE_VERSION_PREFIX),
            ColumnSpec("tenant", "TEXT", "NOT NULL DEFAULT 'default'", _TENANT_COLUMN_PREFIX),
        ),
        indexes=(
            IndexSpec(
                "idx_objects_type_id",
                "objects",
                ("tenant", "object_type", "id"),
                prefix=_OBJECTS_INDEX_PREFIX,
                on_newline=True,
                sqlite_order=3,
                postgres_order=6,
            ),
            IndexSpec(
                "idx_objects_type_rowid",
                "objects",
                ("tenant", "object_type", "row_id"),
                prefix=_OBJECTS_PAGE_INDEX_PREFIX,
                on_newline=True,
                sqlite_order=6,
                postgres_order=7,
            ),
            IndexSpec(
                "idx_objects_page_token",
                "objects",
                ("page_token",),
                unique=True,
                prefix=_PAGE_TOKEN_INDEX_PREFIX,
                on_newline={"sqlite": True, "postgres": False},
                sqlite_order=7,
                postgres_order=8,
            ),
        ),
        prefix=_OBJECTS_PREFIX,
        sqlite_order=0,
        postgres_order=1,
    ),
    TableSpec(
        name="links",
        columns=(
            ColumnSpec("link_type", "TEXT", "NOT NULL"),
            ColumnSpec("from_id", "TEXT", "NOT NULL"),
            ColumnSpec("to_id", "TEXT", "NOT NULL"),
            ColumnSpec("valid_from", "TEXT", "NOT NULL"),
            ColumnSpec("valid_to", "TEXT", "NULL"),
            ColumnSpec("tenant", "TEXT", "NOT NULL DEFAULT 'default'"),
        ),
        indexes=(
            IndexSpec(
                "idx_links_from",
                "links",
                ("tenant", "link_type", "from_id"),
                on_newline={"sqlite": True, "postgres": False},
                sqlite_order=4,
                postgres_order=9,
            ),
            IndexSpec(
                "idx_links_to",
                "links",
                ("tenant", "link_type", "to_id"),
                on_newline={"sqlite": True, "postgres": False},
                sqlite_order=5,
                postgres_order=10,
            ),
        ),
        prefix="\n",
        sqlite_order=1,
        postgres_order=2,
    ),
    TableSpec(
        name="audit_log",
        columns=(
            ColumnSpec(
                "seq",
                {
                    "sqlite": "INTEGER PRIMARY KEY AUTOINCREMENT",
                    "postgres": "BIGSERIAL PRIMARY KEY",
                },
            ),
            ColumnSpec("ts", "TEXT", "NOT NULL"),
            ColumnSpec("actor", "TEXT", "NOT NULL"),
            ColumnSpec("role", "TEXT", "NOT NULL"),
            ColumnSpec("action", "TEXT", "NOT NULL"),
            ColumnSpec("target_type", "TEXT", "NOT NULL"),
            ColumnSpec("target_id", "TEXT", "NULL"),
            ColumnSpec("params", "TEXT", "NOT NULL"),
            ColumnSpec("outcome", "TEXT", "NOT NULL"),
            ColumnSpec("writes", "TEXT", "NOT NULL DEFAULT '[]'"),
            ColumnSpec("effects", "TEXT", "NOT NULL DEFAULT '[]'"),
            ColumnSpec("capability_accesses", "TEXT", "NOT NULL DEFAULT '[]'"),
            ColumnSpec("invocation_id", "TEXT", "NULL"),
            ColumnSpec("kind", "TEXT", "NOT NULL DEFAULT 'action'"),
            ColumnSpec("tenant", "TEXT", "NOT NULL DEFAULT 'default'"),
            ColumnSpec("principal", "TEXT", "NULL", _AUDIT_PRINCIPAL_PREFIX),
        ),
        prefix="\n",
        section="core",
        sqlite_order=2,
        postgres_order=3,
        postgres_column_order=(
            "seq",
            "ts",
            "actor",
            "role",
            "action",
            "target_type",
            "target_id",
            "params",
            "outcome",
            "writes",
            "effects",
            "capability_accesses",
            "invocation_id",
            "kind",
            "tenant",
            "principal",
        ),
    ),
    TableSpec(
        name="effect_outbox",
        columns=(
            ColumnSpec("effect_id", "TEXT", "PRIMARY KEY"),
            ColumnSpec("invocation_id", "TEXT", "NOT NULL"),
            ColumnSpec("seq", "INTEGER", "NOT NULL"),
            ColumnSpec("api_name", "TEXT", "NOT NULL"),
            ColumnSpec("payload", "TEXT", "NOT NULL"),
            ColumnSpec("action", "TEXT", "NOT NULL"),
            ColumnSpec("actor_id", "TEXT", "NOT NULL"),
            ColumnSpec("role", "TEXT", "NOT NULL"),
            ColumnSpec("emitted_at", "TEXT", "NOT NULL"),
            ColumnSpec("state", "TEXT", "NOT NULL"),
            ColumnSpec("attempts", "INTEGER", "NOT NULL DEFAULT 0"),
            ColumnSpec("next_attempt_at", "TEXT", "NOT NULL"),
            ColumnSpec("lease_until", "TEXT", "NULL"),
            ColumnSpec("last_error", "TEXT", "NULL"),
            ColumnSpec("updated_at", "TEXT", "NOT NULL"),
            ColumnSpec("tenant", "TEXT", "NOT NULL DEFAULT 'default'"),
        ),
        indexes=(
            IndexSpec(
                "idx_effect_outbox_invocation_seq",
                "effect_outbox",
                ("invocation_id", "seq"),
                unique=True,
                prefix=_OUTBOX_SEQUENCE_INDEX_PREFIX,
                on_newline=True,
                sqlite_order=9,
                postgres_order=11,
            ),
            IndexSpec(
                "idx_effect_outbox_due",
                "effect_outbox",
                ("tenant", "state", "next_attempt_at"),
                prefix=_OUTBOX_DUE_INDEX_PREFIX,
                on_newline=True,
                sqlite_order=10,
                postgres_order=12,
            ),
        ),
        prefix=_OUTBOX_PREFIX,
        section="effect_outbox",
        sqlite_order=8,
        postgres_order=4,
    ),
    TableSpec(
        name="ontology_fingerprint",
        columns=(
            ColumnSpec(
                "id",
                "INTEGER",
                {"sqlite": "PRIMARY KEY CHECK (id = 1)", "postgres": "PRIMARY KEY"},
            ),
            ColumnSpec("digest", "TEXT", "NOT NULL"),
            ColumnSpec("types", "TEXT", "NOT NULL"),
            ColumnSpec("first_seen", "TEXT", "NOT NULL"),
            ColumnSpec("adopted", "INTEGER", "NOT NULL DEFAULT 0"),
            ColumnSpec("versions", "TEXT", "NOT NULL DEFAULT '{}'"),
        ),
        prefix=_FINGERPRINT_PREFIX,
        section="ontology_fingerprint",
        sqlite_order=11,
        postgres_order=5,
    ),
)

CORE_TABLE_SPECS: tuple[TableSpec, ...] = tuple(
    table for table in TABLE_SPECS if table.section == "core"
)
EFFECT_OUTBOX_TABLE_SPECS: tuple[TableSpec, ...] = tuple(
    table for table in TABLE_SPECS if table.section == "effect_outbox"
)
ONTOLOGY_FINGERPRINT_TABLE_SPECS: tuple[TableSpec, ...] = tuple(
    table for table in TABLE_SPECS if table.section == "ontology_fingerprint"
)


def _dialect_text(value: DialectText, dialect: Dialect) -> str:
    return value if isinstance(value, str) else value[dialect]


def _dialect_bool(value: bool | Mapping[Dialect, bool], dialect: Dialect) -> bool:
    return value if isinstance(value, bool) else value[dialect]


def _table_columns(table: TableSpec, dialect: Dialect) -> tuple[ColumnSpec, ...]:
    if dialect != "postgres" or not table.postgres_column_order:
        return table.columns
    by_name = {column.name: column for column in table.columns}
    ordered_names = table.postgres_column_order
    ordered = tuple(by_name[name] for name in ordered_names)
    extras = tuple(column for column in table.columns if column.name not in ordered_names)
    return ordered + extras


def _render_table(table: TableSpec, dialect: Dialect) -> str:
    lines = []
    for column in _table_columns(table, dialect):
        line = (
            f"{_dialect_text(column.prefix, dialect)}    {column.name} "
            f"{_dialect_text(column.type_spelling, dialect)}"
        )
        constraint = _dialect_text(column.constraints, dialect)
        if constraint:
            line += f" {constraint}"
        lines.append(line)
    lines.extend(f"    {_dialect_text(constraint, dialect)}" for constraint in table.constraints)
    return (
        f"{_dialect_text(table.prefix, dialect)}CREATE TABLE IF NOT EXISTS {table.name} (\n"
        f"{',\n'.join(lines)}\n);\n"
    )


def _render_index(index: IndexSpec, dialect: Dialect) -> str:
    create = "CREATE UNIQUE INDEX IF NOT EXISTS" if index.unique else "CREATE INDEX IF NOT EXISTS"
    head = f"{create} {index.name}"
    target = f"ON {index.table} ({', '.join(index.columns)});"
    if _dialect_bool(index.on_newline, dialect):
        target = f"\n    {target}"
    else:
        target = f" {target}"
    return f"{_dialect_text(index.prefix, dialect)}{head}{target}\n"


def render_schema(dialect: Dialect, *, table_specs: Sequence[TableSpec] | None = None) -> str:
    """Render the complete schema from one table-spec source."""
    tables = tuple(TABLE_SPECS if table_specs is None else table_specs)
    ddl: list[tuple[int, str]] = []
    for table in tables:
        if dialect not in table.dialects:
            continue
        table_order = table.sqlite_order if dialect == "sqlite" else table.postgres_order
        ddl.append((table_order, _render_table(table, dialect)))
        for index in table.indexes:
            index_order = index.sqlite_order if dialect == "sqlite" else index.postgres_order
            ddl.append((index_order, _render_index(index, dialect)))
    return "".join(sql for _, sql in sorted(ddl, key=lambda item: item[0]))


# ``{p}`` is one bind marker and ``{placeholders}`` is a generated list of
# bind markers.  Keeping the template text here means only the parameter
# style changes at the backend boundary.
OBJECT_CURRENT_SELECT_TEMPLATE = f"""
SELECT {_OBJECT_COLUMN_LIST} FROM objects
WHERE object_type = {{p}} AND id = {{p}} AND valid_to IS NULL
  AND tenant = {{p}}
ORDER BY row_id ASC
LIMIT 1
"""

OBJECT_ALL_SELECT_TEMPLATE = f"""
SELECT {_OBJECT_COLUMN_LIST} FROM objects
WHERE object_type = {{p}} AND valid_to IS NULL AND tenant = {{p}}
ORDER BY row_id ASC
"""

OBJECT_PAGE_TOKEN_SELECT_TEMPLATE = """
SELECT row_id FROM objects
WHERE page_token = {p} AND object_type = {p} AND tenant = {p}
"""

LINKS_FROM_SELECT_TEMPLATE = f"""
SELECT {_LINKS_FROM_COLUMN_LIST} FROM links
WHERE link_type = {{p}} AND from_id = {{p}} AND valid_to IS NULL
  AND tenant = {{p}}
"""

LINKS_TO_SELECT_TEMPLATE = f"""
SELECT {_LINKS_TO_COLUMN_LIST} FROM links
WHERE link_type = {{p}} AND to_id = {{p}} AND valid_to IS NULL
  AND tenant = {{p}}
"""

AUDIT_LOG_INSERT_TEMPLATE = f"""
INSERT INTO audit_log
    ({_AUDIT_LOG_COLUMN_LIST})
VALUES ({{placeholders}})
"""

AUDIT_LOG_SELECT_TEMPLATE = f"""
SELECT {_AUDIT_LOG_COLUMN_LIST}
FROM audit_log
WHERE tenant = {{p}} ORDER BY seq ASC
"""

EFFECT_OUTBOX_INSERT_TEMPLATE = f"""
INSERT INTO effect_outbox
    ({_EFFECT_OUTBOX_COLUMN_LIST})
VALUES ({{placeholders}})
"""

EFFECT_OUTBOX_SELECT_TEMPLATE = f"""
SELECT {_EFFECT_OUTBOX_ROW_COLUMN_LIST} FROM effect_outbox
WHERE tenant = {{p}} ORDER BY emitted_at ASC, seq ASC
"""

EFFECT_OUTBOX_CLAIM_SELECT_TEMPLATE = f"""
SELECT {_EFFECT_OUTBOX_ROW_COLUMN_LIST} FROM effect_outbox
WHERE state = 'pending'
  AND next_attempt_at <= {{p}}
  AND (lease_until IS NULL OR lease_until <= {{p}})
  AND tenant = {{p}}
ORDER BY emitted_at ASC, seq ASC
LIMIT {{p}}
"""

EFFECT_OUTBOX_CLAIM_UPDATE_TEMPLATE = """
UPDATE effect_outbox
SET lease_until = {p}, updated_at = {p}
WHERE effect_id = {p}
  AND state = 'pending'
  AND next_attempt_at <= {p}
  AND (lease_until IS NULL OR lease_until <= {p})
  AND tenant = {p}
"""

EFFECT_OUTBOX_POSTGRES_CLAIM_TEMPLATE = f"""
UPDATE effect_outbox SET lease_until = {{p}}, updated_at = {{p}}
WHERE effect_id IN (
    SELECT effect_id FROM effect_outbox
    WHERE state = 'pending'
      AND next_attempt_at <= {{p}}
      AND (lease_until IS NULL OR lease_until <= {{p}})
      AND tenant = {{p}}
    ORDER BY emitted_at ASC, seq ASC
    LIMIT {{p}}
    FOR UPDATE SKIP LOCKED
)
RETURNING {_EFFECT_OUTBOX_ROW_COLUMN_LIST}
"""

EFFECT_OUTBOX_RELEASE_TEMPLATE = """
UPDATE effect_outbox SET lease_until = NULL, updated_at = {p}
WHERE effect_id = {p} AND tenant = {p}
"""

EFFECT_OUTBOX_RESOLVE_TEMPLATE = """
UPDATE effect_outbox
SET state = {p},
    attempts = attempts + 1,
    next_attempt_at = {p},
    lease_until = NULL,
    last_error = {p},
    updated_at = {p}
WHERE effect_id = {p} AND tenant = {p}
"""

ONTOLOGY_FINGERPRINT_SELECT_TEMPLATE = f"""
SELECT {_ONTOLOGY_FINGERPRINT_READ_COLUMN_LIST} FROM ontology_fingerprint
WHERE id = 1
"""

ONTOLOGY_FINGERPRINT_UPSERT_TEMPLATE = f"""
INSERT INTO ontology_fingerprint
    ({_ONTOLOGY_FINGERPRINT_COLUMN_LIST})
VALUES (1, {{p}}, {{p}}, {{p}}, {{p}}, {{p}})
ON CONFLICT (id) DO UPDATE SET
    digest = excluded.digest,
    types = excluded.types,
    adopted = excluded.adopted,
    versions = excluded.versions
"""


def _placeholder(dialect: Dialect) -> str:
    return "?" if dialect == "sqlite" else "%s"


def placeholders(dialect: Dialect, count: int) -> str:
    """Build a comma-separated bind-marker list for ``count`` values."""
    if count < 0:
        raise ValueError(f"placeholder count must be non-negative, got {count}")
    return ", ".join([_placeholder(dialect)] * count)


def render(template: str, dialect: Dialect, *, placeholder_count: int | None = None) -> str:
    """Render a statement template for one backend's parameter style."""
    if "{placeholders}" in template:
        if placeholder_count is None:
            raise ValueError("placeholder_count is required for a placeholder list")
        template = template.replace("{placeholders}", placeholders(dialect, placeholder_count))
    elif placeholder_count is not None:
        raise ValueError("placeholder_count is only valid for a placeholder list")
    return template.replace("{p}", _placeholder(dialect))


class ExecutionResult(NamedTuple):
    """Materialized result shared by sqlite3 cursors and psycopg cursors."""

    rows: list[Any]
    rowcount: int


def execute(
    conn: Any,
    sql: str,
    params: Sequence[Any] = (),
    *,
    dialect: Dialect,
) -> ExecutionResult:
    """Execute SQL and materialize any returned rows with one result shape."""
    if dialect == "sqlite":
        cursor = conn.execute(sql, params)
        rows = list(cursor.fetchall()) if cursor.description is not None else []
        return ExecutionResult(rows=rows, rowcount=int(cursor.rowcount))

    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        rows = list(cursor.fetchall()) if cursor.description is not None else []
        return ExecutionResult(rows=rows, rowcount=int(cursor.rowcount))
