"""Tests for the shared, dialect-aware store DDL source."""

from __future__ import annotations

import re
from dataclasses import replace

from ontary.store import _sql, schema, values
from ontary.store import postgres as postgres_store

_EXPECTED_POSTGRES_DDL = (
    "\n"
    "CREATE TABLE IF NOT EXISTS schema_meta (\n"
    "    key TEXT PRIMARY KEY,\n"
    "    value TEXT NOT NULL\n"
    ");\n"
    "\n"
    "CREATE TABLE IF NOT EXISTS objects (\n"
    "    row_id BIGSERIAL PRIMARY KEY,\n"
    "    object_type TEXT NOT NULL,\n"
    "    id TEXT NOT NULL,\n"
    "    payload TEXT NOT NULL,\n"
    "    valid_from TEXT NOT NULL,\n"
    "    valid_to TEXT NULL,\n"
    "    source_system TEXT NOT NULL,\n"
    "    source_id TEXT NULL,\n"
    "    extracted_at TEXT NULL,\n"
    "    page_token TEXT NOT NULL,\n"
    "    type_version INTEGER NOT NULL DEFAULT 1,\n"
    "    tenant TEXT NOT NULL DEFAULT 'default'\n"
    ");\n"
    "\n"
    "CREATE TABLE IF NOT EXISTS links (\n"
    "    link_type TEXT NOT NULL,\n"
    "    from_id TEXT NOT NULL,\n"
    "    to_id TEXT NOT NULL,\n"
    "    valid_from TEXT NOT NULL,\n"
    "    valid_to TEXT NULL,\n"
    "    tenant TEXT NOT NULL DEFAULT 'default'\n"
    ");\n"
    "\n"
    "CREATE TABLE IF NOT EXISTS audit_log (\n"
    "    seq BIGSERIAL PRIMARY KEY,\n"
    "    ts TEXT NOT NULL,\n"
    "    actor TEXT NOT NULL,\n"
    "    role TEXT NOT NULL,\n"
    "    action TEXT NOT NULL,\n"
    "    target_type TEXT NOT NULL,\n"
    "    target_id TEXT NULL,\n"
    "    params TEXT NOT NULL,\n"
    "    outcome TEXT NOT NULL,\n"
    "    writes TEXT NOT NULL DEFAULT '[]',\n"
    "    capability_accesses TEXT NOT NULL DEFAULT '[]',\n"
    "    invocation_id TEXT NULL,\n"
    "    kind TEXT NOT NULL DEFAULT 'action',\n"
    "    tenant TEXT NOT NULL DEFAULT 'default',\n"
    "    principal TEXT NULL\n"
    ");\n"
    "\n"
    "CREATE TABLE IF NOT EXISTS ontology_fingerprint (\n"
    "    id INTEGER PRIMARY KEY,\n"
    "    digest TEXT NOT NULL,\n"
    "    types TEXT NOT NULL,\n"
    "    first_seen TEXT NOT NULL,\n"
    "    adopted INTEGER NOT NULL DEFAULT 0,\n"
    "    versions TEXT NOT NULL DEFAULT '{}'\n"
    ");\n"
    "\n"
    "CREATE INDEX IF NOT EXISTS idx_objects_type_id\n"
    "    ON objects (tenant, object_type, id);\n"
    "CREATE INDEX IF NOT EXISTS idx_objects_type_rowid\n"
    "    ON objects (tenant, object_type, row_id);\n"
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_objects_page_token ON objects (page_token);\n"
    "CREATE INDEX IF NOT EXISTS idx_links_from ON links (tenant, link_type, from_id);\n"
    "CREATE INDEX IF NOT EXISTS idx_links_to ON links (tenant, link_type, to_id);\n"
)


def test_both_dialects_render_from_the_shared_table_specs() -> None:
    table_names = {table.name for table in _sql.TABLE_SPECS}
    assert table_names == {
        "schema_meta",
        "objects",
        "links",
        "audit_log",
        "ontology_fingerprint",
    }


def test_column_added_to_a_copied_spec_renders_in_both_dialects() -> None:
    objects = next(table for table in _sql.TABLE_SPECS if table.name == "objects")
    extended_objects = replace(
        objects,
        columns=objects.columns + (_sql.ColumnSpec("added_column", "TEXT", "NULL"),),
    )
    specs = tuple(
        extended_objects if table.name == "objects" else table for table in _sql.TABLE_SPECS
    )

    for dialect in ("sqlite", "postgres"):
        ddl = _sql.render_schema(dialect, table_specs=specs)
        assert "    added_column TEXT NULL\n" in ddl


def test_generated_postgres_ddl_is_pinned() -> None:
    assert postgres_store._SCHEMA_SQL == _EXPECTED_POSTGRES_DDL


_EXPECTED_SQLITE_DDL = (
    "\n"
    "-- `page_token` (pagination-hardening T2 amendment, spec §5): a\n"
    "-- fresh uuid4 hex written on EVERY row insert (including the\n"
    "-- new version `update`'s close-old-insert-new writes) -- the\n"
    "-- opaque cursor `read_page` hands out, resolved back to\n"
    "-- `row_id` via `idx_objects_page_token` below. This IS a schema\n"
    "-- change from the T1 shape; T4's `SCHEMA_VERSION` stamp must\n"
    "-- cover it (see that task).\n"
    "CREATE TABLE IF NOT EXISTS objects (\n"
    "    row_id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "    object_type TEXT NOT NULL,\n"
    "    id TEXT NOT NULL,\n"
    "    payload TEXT NOT NULL,\n"
    "    valid_from TEXT NOT NULL,\n"
    "    valid_to TEXT NULL,\n"
    "    source_system TEXT NOT NULL,\n"
    "    source_id TEXT NULL,\n"
    "    extracted_at TEXT NULL,\n"
    "    page_token TEXT NOT NULL,\n"
    "    -- Which version of the declared type this row's payload was written\n"
    "    -- under (M9b). Read by `ontary.upcast.upcast_payload` on every read;\n"
    "    -- written from the declaration on every insert/update.\n"
    "    type_version INTEGER NOT NULL DEFAULT 1,\n"
    "    -- Which tenant owns this row (M8b). NOT NULL with a default so the\n"
    "    -- single-tenant case needs no ceremony and a migrated row is never\n"
    "    -- ownerless: a NULL tenant would read as \"belongs to everybody\",\n"
    "    -- which is precisely wrong for an isolation column.\n"
    "    tenant TEXT NOT NULL DEFAULT 'default'\n"
    ");\n"
    "\n"
    "CREATE TABLE IF NOT EXISTS links (\n"
    "    link_type TEXT NOT NULL,\n"
    "    from_id TEXT NOT NULL,\n"
    "    to_id TEXT NOT NULL,\n"
    "    valid_from TEXT NOT NULL,\n"
    "    valid_to TEXT NULL,\n"
    "    tenant TEXT NOT NULL DEFAULT 'default'\n"
    ");\n"
    "\n"
    "CREATE TABLE IF NOT EXISTS audit_log (\n"
    "    seq INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "    ts TEXT NOT NULL,\n"
    "    actor TEXT NOT NULL,\n"
    "    role TEXT NOT NULL,\n"
    "    action TEXT NOT NULL,\n"
    "    target_type TEXT NOT NULL,\n"
    "    target_id TEXT NULL,\n"
    "    params TEXT NOT NULL,\n"
    "    outcome TEXT NOT NULL,\n"
    "    writes TEXT NOT NULL DEFAULT '[]',\n"
    "    capability_accesses TEXT NOT NULL DEFAULT '[]',\n"
    "    invocation_id TEXT NULL,\n"
    "    kind TEXT NOT NULL DEFAULT 'action',\n"
    "    tenant TEXT NOT NULL DEFAULT 'default',\n"
    "    -- The principal that authenticated the call this entry audits (M10,\n"
    "    -- spec `multi-consumer-mcp` AC8/AC10). NULL for every pre-M10 row --\n"
    "    -- which means \"we do not know who authenticated\", never \"nobody did\",\n"
    "    -- so no default is correct here.\n"
    "    principal TEXT NULL\n"
    ");\n"
    "\n"
    "-- Indexes for the point-lookup access patterns every read goes\n"
    "-- through: `read_current`/`read_all` (object_type[, id]) and `links_from`/\n"
    "-- `links_to` (link_type + from_id/to_id). Without these, SQLite\n"
    "-- full-scans `objects`/`links` on EVERY lookup -- invisible at\n"
    "-- fixture scale (tens of rows) but O(n^2) once a real ingest puts\n"
    "-- tens of thousands of objects/links in the store.\n"
    "-- Non-behavioral: pure read speedup.\n"
    "CREATE INDEX IF NOT EXISTS idx_objects_type_id\n"
    "    ON objects (tenant, object_type, id);\n"
    "CREATE INDEX IF NOT EXISTS idx_links_from\n"
    "    ON links (tenant, link_type, from_id);\n"
    "CREATE INDEX IF NOT EXISTS idx_links_to\n"
    "    ON links (tenant, link_type, to_id);\n"
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
    "CREATE INDEX IF NOT EXISTS idx_objects_type_rowid\n"
    "    ON objects (tenant, object_type, row_id);\n"
    "\n"
    "-- `read_page`'s `after_key` -> `row_id` resolution (see\n"
    "-- the `INVALID_CURSOR` validation rule) is a single indexed point lookup\n"
    "-- on `page_token`, never a scan -- UNIQUE also gives SQLite's\n"
    "-- own constraint enforcement for free (a `page_token` collision\n"
    "-- -- practically impossible for uuid4 -- raises on INSERT\n"
    "-- rather than silently letting two rows share a cursor).\n"
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_objects_page_token\n"
    "    ON objects (page_token);\n"
    "\n"
    "-- Which ontology definition this store's rows were written under\n"
    "-- (spec `ontology-evolution` AC2). One row, ever: `id = 1`.\n"
    "-- Before this table, nothing recorded the SHAPE a row was written\n"
    "-- under, so no reader could tell \"this row predates the change\"\n"
    "-- from \"this row is wrong\" -- which is the only distinction that\n"
    "-- makes a migration possible.\n"
    "CREATE TABLE IF NOT EXISTS ontology_fingerprint (\n"
    "    id INTEGER PRIMARY KEY CHECK (id = 1),\n"
    "    digest TEXT NOT NULL,\n"
    "    types TEXT NOT NULL,\n"
    "    first_seen TEXT NOT NULL,\n"
    "    adopted INTEGER NOT NULL DEFAULT 0,\n"
    "    versions TEXT NOT NULL DEFAULT '{}'\n"
    ");\n"
)


def test_generated_sqlite_ddl_is_pinned() -> None:
    assert schema._SCHEMA_SQL == _EXPECTED_SQLITE_DDL


def test_lineage_timestamps_are_one_fixed_width_spelling() -> None:
    """`_utcnow_iso` renders ONE width, including on an exact second.

    `protocol.Store` and `inmemory._live_at` both tell callers the as-of
    timestamp is "fixed-width", and the as-of predicates compare it as text.
    A bare `datetime.isoformat()` breaks that claim roughly one call in a
    million: it omits the fractional part when `microsecond == 0`, yielding a
    25-character spelling of an instant that is 32 characters the rest of the
    time. Two widths in one TEXT column make every comparison between them a
    question about collation, which is how a Postgres as-of read came to drop
    a link that was live and admit one already closed.

    The exact-second case is the whole point, so it is constructed directly
    rather than waited for.
    """
    from datetime import datetime, timezone
    from unittest.mock import patch

    on_the_second = datetime(2030, 6, 1, 0, 0, 0, 0, tzinfo=timezone.utc)
    with patch("ontary.store.values.datetime") as clock:
        clock.now.return_value = on_the_second
        rendered = values._utcnow_iso()

    assert rendered == "2030-06-01T00:00:00.000000+00:00"
    assert len(rendered) == len(values._utcnow_iso())


def _link_select_templates() -> dict[str, str]:
    """Every statement template in `_sql` that READS the links table.

    Derived from the module's own contents and from the SQL text, not from a
    list written here: a template added later joins this set on its own,
    whatever it is named. A hand-written tuple is what let the two as-of
    templates ship unordered -- the enumeration, not the templates, was the
    thing that went stale.
    """
    return {
        name: value
        for name, value in vars(_sql).items()
        if name.endswith("_TEMPLATE")
        and isinstance(value, str)
        and value.lstrip().upper().startswith("SELECT")
        and re.search(r"\bFROM\s+links\b", value)
    }


def test_every_link_read_template_specifies_an_order() -> None:
    """A links read without `ORDER BY` returns rows in whatever order the
    backend happens to hold them.

    `scope.ViaLink` takes the FIRST parent whose chain resolves, so an
    unspecified order is not cosmetic: it decides which scope owns the
    object and therefore which operator the action gate admits. SQLite and
    the in-memory store answered in creation order while Postgres answered
    in physical-row order, which `close_link`'s `UPDATE` rewrites -- the
    same store state authorizing opposite operators by backend.
    """
    templates = _link_select_templates()
    # Guard the guard: an empty or shrunken set would pass vacuously. A
    # SUPERSET check, so a template added later still has to satisfy the
    # assertion below rather than being quietly excused from it.
    assert {
        "LINKS_FROM_SELECT_TEMPLATE",
        "LINKS_TO_SELECT_TEMPLATE",
        "LINKS_FROM_ASOF_SELECT_TEMPLATE",
        "LINKS_TO_ASOF_SELECT_TEMPLATE",
    } <= set(templates), sorted(templates)

    unordered = sorted(name for name, sql in templates.items() if "ORDER BY" not in sql)
    assert unordered == []


def _order_by_terms(clause: str) -> list[str]:
    """Split an `ORDER BY` clause on its TOP-LEVEL commas only.

    A naive `clause.split(",")` also splits inside an argument list, so a
    term like `COALESCE(a, b) COLLATE "C" ASC` would arrive as the two
    fragments `COALESCE(a` and `b) COLLATE "C" ASC` -- and the first,
    carrying no token, would false-RED a correctly collated template.

    Paren depth is tracked OUTSIDE quoted literals and clamped at zero,
    because both mistakes fail in the dangerous direction. A `(` inside a
    string literal (`x || '(' COLLATE "C" ASC, y ASC`) would otherwise
    raise the depth for good and swallow the rest of the clause into one
    term, letting a sibling's token mask an uncollated `y ASC`; a leading
    `)` would drive the depth negative for the same effect. A false-RED is
    a nuisance, but a false-GREEN silently disarms the caller.
    """
    terms: list[str] = []
    depth = 0
    quoted = False
    current: list[str] = []
    for char in clause:
        if char == "'":
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(depth - 1, 0)
        if char == "," and depth == 0 and not quoted:
            terms.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        terms.append(tail)
    return terms


def test_every_link_read_template_collates_its_order_columns() -> None:
    """Every ordering column is compared BYTE-wise on Postgres too.

    The `ORDER BY` alone only makes each backend self-consistent. What makes
    the three backends agree with EACH OTHER is the `{collate}` token on
    every ordering column: SQLite compares TEXT as bytes and the in-memory
    store compares Python `str`s, so Postgres has to be pinned to
    `COLLATE "C"` or its default locale re-creates the divergence the
    `ORDER BY` was added to remove -- one backend's first parent is
    another's second, and `ViaLink` takes the first.

    This assertion exists because a behavioural row can only speak on a
    backend whose locale actually inverts the pair it builds.
    `test_link_reads_order_mixed_width_valid_from_in_byte_order` reds on
    macOS libc `en_US.UTF-8` and PASSES on the `postgres:16` image CI runs,
    where locale order and `C` order agree on the 25- and 32-character
    timestamp spellings -- and no reachable timestamp pair inverts under
    glibc at all, so that row cannot be repaired by choosing better data.
    On the gate that blocks merges it cannot tell correct from broken, and
    for the `valid_from` token this assertion is the ONLY guard.

    Its sibling `test_link_reads_break_a_valid_from_tie_in_byte_order` used
    to have the same hole and no longer does: its id pair was changed to
    one measured to invert under BOTH collations, so it reds on macOS libc
    and on `postgres:16` alike.

    Offline, dialect-rendered, and derived from the same module sweep as
    the order check above, so it runs on every platform and a template
    added later cannot escape it.
    """
    templates = _link_select_templates()
    assert {
        "LINKS_FROM_SELECT_TEMPLATE",
        "LINKS_TO_SELECT_TEMPLATE",
        "LINKS_FROM_ASOF_SELECT_TEMPLATE",
        "LINKS_TO_ASOF_SELECT_TEMPLATE",
    } <= set(templates), sorted(templates)

    # Every ORDER BY TERM, not a list of column names written here: a new
    # ordering column joins the check by being ordered on.
    uncollated: dict[str, list[str]] = {}
    for name, template in templates.items():
        clause = _sql.render(template, "postgres").split("ORDER BY", 1)[1]
        clause = re.split(r"\b(?:LIMIT|OFFSET)\b", clause, maxsplit=1)[0]
        bare = [
            term for term in _order_by_terms(clause) if 'COLLATE "C"' not in term
        ]
        if bare:
            uncollated[name] = bare
    assert uncollated == {}

    # And the token is Postgres-only -- SQLite compares TEXT as bytes
    # already, so rendering it there would be a syntax error.
    for name, template in templates.items():
        assert "COLLATE" not in _sql.render(template, "sqlite"), name


def test_every_link_read_template_collates_its_range_bounds() -> None:
    """The as-of BOUNDS are compared byte-wise too, not only the order.

    The `ORDER BY` token decides which parent `ViaLink` takes; the bounds
    token decides which parents are RETURNED at all. `valid_from <= asof`
    and `valid_to >= asof` are inequality comparisons over TEXT, so under
    Postgres's default locale they answer a different question than the
    byte comparison SQLite and the in-memory store perform -- an as-of read
    dropping a link that was live, or admitting one already closed.

    This is the same class of defect as the ordering one and it was, until
    this assertion, unguarded on the platform that gates: dropping the
    token from all four bounds gives `414 passed, 0 failed` on the
    `postgres:16` image, redding only on macOS libc. The two behavioural
    rows that cover it (`test_asof_link_read_compares_mixed_width_
    timestamps_consistently` and `..._excludes_a_link_closed_at_a_narrower_
    spelling`) turn on the 25- versus 32-character timestamp spellings,
    which glibc orders exactly as `C` does -- so, as with the ordering
    token, no choice of fixture data can repair them and the guard has to
    be structural.

    Derived like its siblings: it names no column. EVERY inequality
    comparison in a links template is required to carry the token, so a
    bound added later is covered by being compared.
    """
    templates = _link_select_templates()
    assert {
        "LINKS_FROM_ASOF_SELECT_TEMPLATE",
        "LINKS_TO_ASOF_SELECT_TEMPLATE",
    } <= set(templates), sorted(templates)

    uncollated: dict[str, list[str]] = {}
    for name, template in templates.items():
        rendered = _sql.render(template, "postgres")
        bare = [
            rendered[max(0, match.start() - 40) : match.end()].strip()
            for match in re.finditer(r"<=|>=|<|>", rendered)
            if not rendered[: match.start()].rstrip().endswith('COLLATE "C"')
        ]
        if bare:
            uncollated[name] = bare
    assert uncollated == {}

    # Guard the guard: the sweep is vacuous if no template compares a
    # range at all, so the as-of pair must actually contain inequalities.
    asof_bounds = sum(
        len(re.findall(r"<=|>=", _sql.render(templates[name], "postgres")))
        for name in ("LINKS_FROM_ASOF_SELECT_TEMPLATE", "LINKS_TO_ASOF_SELECT_TEMPLATE")
    )
    assert asof_bounds == 4, asof_bounds
