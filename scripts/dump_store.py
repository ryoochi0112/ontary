"""Canonical, ontary-free dump of an ontary-shaped SQLite store, as JSON.

    python scripts/dump_store.py <path-to-sqlite-file>

Prints one JSON document to stdout: `{"schema_version": <PRAGMA user_version>,
"tables": {<table>: [<row>, ...], ...}}`, tables and rows both in a stable,
content-derived order, and every dict's keys alphabetized (`json.dumps(...,
sort_keys=True)`) -- so the SAME logical content always serializes to the
SAME bytes, on this file or on any other file holding the same content.

This is the ONLY comparison medium the AC5 fixture-honesty CI job (ontary
`v1-acceptance-gate` spec §6) has: it regenerates a fixture under the tag's
own venv and diffs this script's output for the regenerated file against the
output for the committed one. Comparison is therefore **semantic**, never
byte-equality on the `.sqlite` file itself -- SQLite's own on-disk bytes
depend on page layout, vacuum history, and the platform's libsqlite3 build,
none of which are part of what a writer script promises to reproduce.

Deliberately plain `sqlite3` (stdlib only, no `ontary` import): this has to run
unmodified under an ancient tag's venv (whatever that tag's `ontary`/pydantic
pins are, or are not, installed) AND under this repo's current HEAD, so it
cannot depend on any shape `ontary` itself might change across the very
versions it is comparing.

## What is normalized, and why

Every table/row is dumped byte-for-byte AS STORED, with exactly one
exception: the six (table, column) pairs below, each holding a value
`src/ontary/store/sqlite.py` writes straight from the wall clock or a random
UUID with **no caller-level override** -- not even a writer using
`ontary.testing.FixedClock`/`SequentialIds` can make these reproducible,
because the methods that set them (`ObjectStore.insert`/`update`/
`create_link`/`close_link`/`write_ontology_fingerprint`, read directly to
confirm this) call `datetime.now(...)`/`uuid.uuid4()` inline rather than
through any injectable seam:

- `objects.valid_from`, `objects.valid_to` -- row lifecycle timestamps
  (`_utcnow_iso()` in `ObjectStore.insert`/`update`/`retire_object`).
- `objects.page_token` -- a fresh `uuid.uuid4().hex` minted on every row
  insert: the opaque `read_page` cursor.
- `links.valid_from`, `links.valid_to` -- the same row-lifecycle timestamp,
  for links (`ObjectStore.create_link`/`close_link`).
- `ontology_fingerprint.first_seen` -- stamped once, at `ObjectStore.
  __init__` time, before any runtime/clock exists to inject into it.

A non-null normalized value is replaced with the literal string
`"<normalized>"`; `NULL` stays `NULL` (a live row's `valid_to` and a closed
row's `valid_to` must keep comparing UNEQUAL to each other -- only the exact
closing instant is thrown away, never the fact that closing happened).

Every OTHER timestamp-shaped column in this schema -- `audit_log.ts`,
`effect_outbox.emitted_at`/`next_attempt_at`/`lease_until`/`updated_at`,
and `objects.extracted_at` -- IS reachable through a caller-supplied `clock=`
(`OntologyRuntime`/`ActionExecutor`) or a caller-supplied `Source.
extracted_at`, so a correctly written frozen writer script makes them
reproducible BY CONSTRUCTION, and this script compares them literally. That
is deliberate, not an oversight: normalizing them away here would hide the
exact regression the honesty job exists to catch -- a frozen writer whose
`FixedClock` constant, or hardcoded `extracted_at`, quietly changed.

## Determinism

For one file, two runs of this script produce byte-identical stdout: rows are
fetched via `ORDER BY rowid ASC` (SQLite's own row order, itself reproducible
run-to-run for a store built by a deterministic writer, since `rowid` is
assigned by insertion sequence) and then re-sorted by each row's OWN
normalized content -- not by `rowid` -- so a table's row order reflects row
DATA, never storage-layer position; `rowid` is fetched only to break ties
among rows whose normalized content is identical, and it is never itself
part of the output (mirroring this codebase's own rule against ever
surfacing raw row identity -- see `ontary.store.values.Lineage`'s docstring).
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

#: (table, columns) pairs whose values this script replaces with
#: `_NORMALIZED_PLACEHOLDER` -- see the module docstring's "What is
#: normalized" section for the exact source line backing each one. Tables
#: are discovered from `sqlite_master`, never hardcoded here or anywhere else
#: in this script, so an old-shape file missing a table (e.g. no
#: `ontology_fingerprint` before schema v6) just dumps fewer keys, not an
#: error -- this map is consulted per table that DOES exist.
_VOLATILE_COLUMNS: dict[str, frozenset[str]] = {
    "objects": frozenset({"valid_from", "valid_to", "page_token"}),
    "links": frozenset({"valid_from", "valid_to"}),
    "ontology_fingerprint": frozenset({"first_seen"}),
}

_NORMALIZED_PLACEHOLDER = "<normalized>"

#: Table names come from `sqlite_master` (trusted), never from argv, so this
#: can only fire on a genuinely malformed database -- checked anyway because
#: `_dump_table` interpolates the name into a SQL string.
_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _table_names(conn: sqlite3.Connection) -> list[str]:
    """Every user table, alphabetically. `sqlite_%` tables (SQLite's own
    bookkeeping -- e.g. `sqlite_sequence`, present because `objects.row_id`
    and `audit_log.seq` are `AUTOINCREMENT`) are never part of the
    ontology's own schema and are excluded."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
        "ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _json_safe(value: Any) -> Any:
    """SQLite hands back `bytes` for a BLOB column. Nothing in this schema
    declares one today, but hex-encoding rather than raising keeps this
    script from breaking the moment one is added."""
    if isinstance(value, bytes):
        return value.hex()
    return value


def _normalize_row(table: str, row: dict[str, Any]) -> dict[str, Any]:
    volatile = _VOLATILE_COLUMNS.get(table, frozenset())
    normalized: dict[str, Any] = {}
    for column, value in row.items():
        if column in volatile and value is not None:
            normalized[column] = _NORMALIZED_PLACEHOLDER
        else:
            normalized[column] = value
    return normalized


def _row_sort_key(row: dict[str, Any]) -> str:
    """A canonical string for one (already-normalized) row, used only to
    ORDER rows within a table: content decides the order, never storage
    position (see the module docstring's Determinism section)."""
    return json.dumps(row, sort_keys=True, default=str)


def _dump_table(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if not _VALID_IDENTIFIER.match(table):
        raise ValueError(f"refusing to dump a non-identifier table name: {table!r}")
    cursor = conn.execute(f"SELECT * FROM {table} ORDER BY rowid ASC")
    assert cursor.description is not None, f"no column description for {table!r}"
    columns = [description[0] for description in cursor.description]
    rows = [
        _normalize_row(
            table,
            {column: _json_safe(value) for column, value in zip(columns, values, strict=True)},
        )
        for values in cursor.fetchall()
    ]
    rows.sort(key=_row_sort_key)
    return rows


def dump(path: str | Path) -> dict[str, Any]:
    """Return the canonical dump of the SQLite file at `path`.

    Opened strictly read-only via a `file:` URI: `sqlite3.connect` on a
    plain path silently CREATES an empty database file when it does not
    exist, which is exactly wrong for a tool whose one job is to inspect an
    existing fixture without ever being able to mutate or conjure one.
    """
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        version_row = conn.execute("PRAGMA user_version").fetchone()
        assert version_row is not None, "PRAGMA user_version returned no row"
        tables = _table_names(conn)
        return {
            "schema_version": int(version_row[0]),
            "tables": {table: _dump_table(conn, table) for table in tables},
        }
    finally:
        conn.close()


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python scripts/dump_store.py <path-to-sqlite-file>", file=sys.stderr)
        raise SystemExit(2)
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        raise SystemExit(2)
    try:
        result = dump(path)
    except sqlite3.DatabaseError as exc:
        print(f"{path} is not a readable SQLite database: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
