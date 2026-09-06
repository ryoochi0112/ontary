"""Tests for `ObjectStore`'s SQLite schema-version stamp + legacy
`page_token` migration (spec pagination-hardening AC11, T4).

Deliberately file-backed (`tmp_path`, real `sqlite3.connect(str(path))`
files) rather than `:memory:` -- that IS the point (see the spec's AC11 and
`_init_schema`'s docstring): the rest of the suite uses `:memory:`
exclusively and cannot catch a store file written by a pre-`page_token`
ontary failing to open at all.
"""

from __future__ import annotations

import multiprocessing
import multiprocessing.synchronize
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pytest
from conftest import raises_code

from ontary.errors import ConflictError
from ontary.meta import ObjectTypeDef, OntologyRegistry, PropertyDef
from ontary.query import GuardedQuery
from ontary.scope import ScopePolicy
from ontary.security import Consumer
from ontary.store import (
    SCHEMA_VERSION,
    AuditEntry,
    ObjectStore,
    Source,
)

SRC = Source(source_system="test")


RegistryFactory = Callable[..., OntologyRegistry]
PolicyFactory = Callable[..., ScopePolicy]
ConsumerFactory = Callable[..., Consumer]

_WIDGET_TYPE = ObjectTypeDef(
    api_name="Widget",
    display_name="Widget",
    description="A canonical widget",
    layer="L0",
    properties=[
        PropertyDef(name="id", type="str", required=False),
        PropertyDef(name="name", type="str"),
    ],
    primary_key="id",
)


# Verbatim T1 (pre-`page_token`) DDL -- copied from `git show
# 1804b2a:src/ontary/store.py`'s `_init_schema`, so the fixture is a
# genuinely old, unstamped store shape rather than a hand-wave.
_LEGACY_T1_DDL = """
CREATE TABLE IF NOT EXISTS objects (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_type TEXT NOT NULL,
    id TEXT NOT NULL,
    payload TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NULL,
    source_system TEXT NOT NULL,
    source_id TEXT NULL,
    extracted_at TEXT NULL
);

CREATE TABLE IF NOT EXISTS links (
    link_type TEXT NOT NULL,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NULL,
    params TEXT NOT NULL,
    outcome TEXT NOT NULL,
    writes TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_objects_type_id
    ON objects (object_type, id);
CREATE INDEX IF NOT EXISTS idx_links_from
    ON links (link_type, from_id);
CREATE INDEX IF NOT EXISTS idx_links_to
    ON links (link_type, to_id);

CREATE INDEX IF NOT EXISTS idx_objects_type_rowid
    ON objects (object_type, row_id);
"""


def _write_legacy_file(path: Path, rows: list[tuple[str, str]]) -> None:
    """Build a genuine pre-`page_token` store file (raw sqlite3, no
    `page_token` column at all, `user_version` left at its SQLite default
    of 0) and insert `rows` (id, name) pairs of current "Widget" rows."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_LEGACY_T1_DDL)
        for obj_id, name in rows:
            conn.execute(
                """
                INSERT INTO objects
                    (object_type, id, payload, valid_from, valid_to,
                     source_system, source_id, extracted_at)
                VALUES ('Widget', ?, ?, '2026-01-01T00:00:00+00:00', NULL,
                        'legacy', NULL, NULL)
                """,
                (obj_id, f'{{"id": "{obj_id}", "name": "{name}"}}'),
            )
        conn.commit()
    finally:
        conn.close()


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _initialize_v1_connection(conn: sqlite3.Connection) -> None:
    """Create the exact pre-M5 persisted shape and stamp it version 1."""
    conn.executescript(_LEGACY_T1_DDL)
    conn.execute("ALTER TABLE objects ADD COLUMN page_token TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_objects_page_token "
        "ON objects (page_token)"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()


def _write_v1_file(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        _initialize_v1_connection(conn)
    finally:
        conn.close()


# Verbatim pre-M3 DDL -- copied from `git show 9cccf9b^:src/ontary/store.py`
# (the commit immediately before `objects.extracted_at` was added, itself
# well before Milestone 3 added `audit_log.writes`): `objects` has neither
# `extracted_at` nor `page_token`, and `audit_log` has no `writes` column at
# all. This is the exact "real pre-Milestone-3 DDL, one row" fixture the P0
# review (spec pagination-hardening AC11) reproduces its receipt against --
# a legacy file whose narrower shape goes undetected by a `page_token`-only
# migration check.
_LEGACY_PRE_M3_DDL = """
CREATE TABLE IF NOT EXISTS objects (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_type TEXT NOT NULL,
    id TEXT NOT NULL,
    payload TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NULL,
    source_system TEXT NOT NULL,
    source_id TEXT NULL
);

CREATE TABLE IF NOT EXISTS links (
    link_type TEXT NOT NULL,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NULL,
    params TEXT NOT NULL,
    outcome TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_objects_type_id
    ON objects (object_type, id);
CREATE INDEX IF NOT EXISTS idx_links_from
    ON links (link_type, from_id);
CREATE INDEX IF NOT EXISTS idx_links_to
    ON links (link_type, to_id);
"""


def _write_legacy_pre_m3_file(path: Path, rows: list[tuple[str, str]]) -> None:
    """Build a genuine pre-Milestone-3 store file (raw sqlite3, no
    `extracted_at`/`page_token` columns on `objects` at all, no `writes`
    column on `audit_log`, `user_version` left at its SQLite default of 0)
    and insert `rows` (id, name) pairs of current "Widget" rows."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_LEGACY_PRE_M3_DDL)
        for obj_id, name in rows:
            conn.execute(
                """
                INSERT INTO objects
                    (object_type, id, payload, valid_from, valid_to,
                     source_system, source_id)
                VALUES ('Widget', ?, ?, '2026-01-01T00:00:00+00:00', NULL,
                        'legacy', NULL)
                """,
                (obj_id, f'{{"id": "{obj_id}", "name": "{name}"}}'),
            )
        conn.commit()
    finally:
        conn.close()


# A legacy DDL whose `objects` table is COMPLETE (carries `extracted_at`,
# same as `_LEGACY_T1_DDL`'s -- the only column it lacks, `page_token`, is
# self-migrated and excluded from the check) but whose `audit_log` table
# lacks `writes` -- the pre-Milestone-3 shape isolated to ONLY the
# audit_log arm, so a test against it can pin that arm specifically rather
# than relying on `_LEGACY_PRE_M3_DDL` (whose `objects` table trips the
# check FIRST, per `_SCHEMA_TABLES`'s ("objects", "links", "audit_log")
# iteration order, so the audit_log arm never actually runs there).
_LEGACY_AUDIT_LOG_MISSING_WRITES_DDL = """
CREATE TABLE IF NOT EXISTS objects (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_type TEXT NOT NULL,
    id TEXT NOT NULL,
    payload TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NULL,
    source_system TEXT NOT NULL,
    source_id TEXT NULL,
    extracted_at TEXT NULL
);

CREATE TABLE IF NOT EXISTS links (
    link_type TEXT NOT NULL,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NULL,
    params TEXT NOT NULL,
    outcome TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_objects_type_id
    ON objects (object_type, id);
CREATE INDEX IF NOT EXISTS idx_links_from
    ON links (link_type, from_id);
CREATE INDEX IF NOT EXISTS idx_links_to
    ON links (link_type, to_id);
"""


def _write_legacy_audit_log_missing_writes_file(
    path: Path, rows: list[tuple[str, str]]
) -> None:
    """Build a legacy store file whose `objects` table is COMPLETE (has
    `extracted_at`) and whose ONLY incompatibility is `audit_log` lacking
    `writes` -- isolates the audit_log arm of
    `_check_legacy_schema_compatible` from the objects arm (see
    `_LEGACY_AUDIT_LOG_MISSING_WRITES_DDL`'s docstring)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_LEGACY_AUDIT_LOG_MISSING_WRITES_DDL)
        for obj_id, name in rows:
            conn.execute(
                """
                INSERT INTO objects
                    (object_type, id, payload, valid_from, valid_to,
                     source_system, source_id, extracted_at)
                VALUES ('Widget', ?, ?, '2026-01-01T00:00:00+00:00', NULL,
                        'legacy', NULL, NULL)
                """,
                (obj_id, f'{{"id": "{obj_id}", "name": "{name}"}}'),
            )
        conn.commit()
    finally:
        conn.close()


def _dump_schema(path: Path) -> str:
    """A stable textual snapshot of every table's DDL, used to assert a
    refused legacy file is left byte-for-byte untouched (not merely
    unstamped) by `_check_legacy_schema_compatible`."""
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY sql"
        ).fetchall()
        return "\n".join(row[0] for row in rows)
    finally:
        conn.close()


# -- fresh file -----------------------------------------------------------


def test_fresh_file_stamps_schema_version_and_works_normally(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    db_path = tmp_path / "fresh.db"
    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    assert _user_version(db_path) == SCHEMA_VERSION == 9

    obj_id = store.insert("Widget", {"name": "sprocket"}, SRC)
    got = store.read_current("Widget", obj_id)
    assert got is not None
    assert got.payload["name"] == "sprocket"

    pages = store.read_page("Widget")
    assert len(pages) == 1
    assert pages[0].obj.payload["name"] == "sprocket"
    assert pages[0].key  # a real page token was minted


# -- legacy unstamped file predating page_token: migrated in place --------


def test_legacy_unstamped_file_is_migrated_in_place(
    tmp_path: Path,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_consumer: ConsumerFactory,
) -> None:
    db_path = tmp_path / "legacy.db"
    rows = [("w1", "Alpha"), ("w2", "Bravo"), ("w3", "Charlie"), ("w4", "Delta")]
    _write_legacy_file(db_path, rows)
    assert _user_version(db_path) == 0  # genuinely unstamped before opening

    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    # 1. schema actually migrated: column + unique index exist, version stamped
    assert _user_version(db_path) == SCHEMA_VERSION == 9
    raw = sqlite3.connect(str(db_path))
    try:
        columns = {row[1] for row in raw.execute("PRAGMA table_info(objects)")}
        assert "page_token" in columns
        index_names = {
            row[1] for row in raw.execute("PRAGMA index_list(objects)")
        }
        assert "idx_objects_page_token" in index_names
    finally:
        raw.close()

    # 2. every pre-existing row got its OWN distinct, non-empty token
    all_rows = store.read_all("Widget")
    assert len(all_rows) == len(rows)
    pages = store.read_page("Widget", batch=100)
    tokens = [p.key for p in pages]
    assert len(tokens) == len(rows)
    assert all(tokens)  # non-empty
    assert len(set(tokens)) == len(tokens)  # every row has its own token

    # 3. the migrated file is genuinely usable end-to-end: a real paginated
    #    walk (read_page + GuardedQuery.get_objects with limit) over the
    #    migrated rows works, not just "the stamp says so".
    query = GuardedQuery(
        store,
        make_registry(object_types=(_WIDGET_TYPE,)),
        make_policy(levels=["org"], unscoped_types={"Widget"}),
    )
    consumer = make_consumer()

    seen_ids: list[str] = []
    cursor: str | None = None
    while True:
        page = query.get_objects(consumer, "Widget", limit=2, after=cursor)
        seen_ids.extend(obj.payload["id"] for obj in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert sorted(seen_ids) == sorted(obj_id for obj_id, _ in rows)

    # A brand new insert into the migrated file keeps working too.
    new_id = store.insert("Widget", {"name": "Echo"}, SRC)
    assert store.read_current("Widget", new_id) is not None


def test_legacy_file_with_all_columns_but_page_token_still_migrates(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    """Regression check for the P0 fix (spec pagination-hardening AC11
    review): `_check_legacy_schema_compatible` must NOT flag a legacy file
    that already carries every OTHER required column (`extracted_at`,
    `writes`) and is missing only `page_token` -- exactly the
    `_LEGACY_T1_DDL` fixture `test_legacy_unstamped_file_is_migrated_in_place`
    above already exercises end-to-end. This test pins the narrower claim
    directly against the new check itself, so a future change to the check
    that over-broadly refuses this common case is caught right here."""
    db_path = tmp_path / "legacy_full_minus_token.db"
    _write_legacy_file(db_path, [("w1", "Alpha")])
    assert _user_version(db_path) == 0

    store = ObjectStore(
        make_registry(object_types=(_WIDGET_TYPE,)), str(db_path)
    )  # must not raise

    assert _user_version(db_path) == SCHEMA_VERSION
    assert len(store.read_all("Widget")) == 1


def test_legacy_file_missing_columns_beyond_page_token_is_refused(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    """The P0 fix itself (spec pagination-hardening AC11 review): a real
    pre-Milestone-3 file (`objects` missing `extracted_at`, `audit_log`
    missing `writes` -- neither of which this engine has a migration for,
    unlike `page_token`) must be refused with a coded
    a coded schema refusal naming the incompatible table, `user_version`
    must stay 0 (never stamped), and the file must be left completely
    untouched -- not merely unstamped -- so a future engine version that
    DOES carry a migration for these columns can still repair it.

    Before the fix, this exact scenario was the reviewer's receipt: the
    engine opened the file, stamped `user_version=1` (because its
    page_token-only check saw nothing to migrate for `page_token` beyond
    what it already knew to add), and every subsequent
    read_all/read_page/read_current/insert/append_audit raised an uncoded
    `sqlite3.OperationalError` naming a column that was never there --
    permanently, because a `user_version == SCHEMA_VERSION` file is never
    re-inspected."""
    db_path = tmp_path / "pre_m3_legacy.db"
    _write_legacy_pre_m3_file(db_path, [("w1", "Alpha")])
    assert _user_version(db_path) == 0
    schema_before = _dump_schema(db_path)

    with raises_code(ConflictError, "STORE_SCHEMA_INCOMPATIBLE") as exc_info:
        ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    # `_SCHEMA_TABLES` checks `objects` before `audit_log`, and this
    # fixture's `objects` is ALSO missing `extracted_at`, so `objects` is
    # what actually trips the check first -- name it specifically, rather
    # than the looser `"objects" in message or "audit_log" in message`
    # that is equally satisfied whichever table happens to be checked
    # first (and so would not catch the audit_log arm silently never
    # running -- see `test_legacy_file_with_audit_log_missing_writes_is_refused`
    # below, which pins that arm on its own, isolated fixture).
    message = str(exc_info.value)
    assert "objects" in message

    # Never stamped, and the file's schema is byte-for-byte what it was
    # found as -- no ALTER/CREATE ran against it at all.
    assert _user_version(db_path) == 0
    assert _dump_schema(db_path) == schema_before

    # Re-inspectable: opening it again raises the exact same coded refusal,
    # not some different failure from a half-applied prior attempt.
    with raises_code(ConflictError, "STORE_SCHEMA_INCOMPATIBLE"):
        ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    assert _user_version(db_path) == 0


def test_legacy_file_with_audit_log_missing_writes_is_refused(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    """Pins the audit_log arm of `_check_legacy_schema_compatible`
    SPECIFICALLY, using a fixture whose `objects` table is complete (only
    `page_token` missing, which is self-migrated) and whose ONLY
    incompatibility is `audit_log` lacking `writes`. Without this test, a
    regression narrowing `_SCHEMA_TABLES` to `("objects",)` -- dropping the
    audit_log/links arms entirely -- passes the whole suite unnoticed,
    because `test_legacy_file_missing_columns_beyond_page_token_is_refused`'s
    `_LEGACY_PRE_M3_DDL` fixture trips on `objects` first and never
    exercises the `audit_log` arm at all."""
    db_path = tmp_path / "legacy_audit_log_missing_writes.db"
    _write_legacy_audit_log_missing_writes_file(db_path, [("w1", "Alpha")])
    assert _user_version(db_path) == 0
    schema_before = _dump_schema(db_path)

    with raises_code(ConflictError, "STORE_SCHEMA_INCOMPATIBLE") as exc_info:
        ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    message = str(exc_info.value)
    assert "audit_log" in message
    assert "writes" in message

    assert _user_version(db_path) == 0
    assert _dump_schema(db_path) == schema_before


# -- stamped v1 audit schema: migrated to v2 -------------------------------


def test_stamped_v1_file_migrates_audit_columns_in_place(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    db_path = tmp_path / "v1.db"
    _write_v1_file(db_path)
    assert _user_version(db_path) == 1

    ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    assert _user_version(db_path) == SCHEMA_VERSION == 9
    conn = sqlite3.connect(str(db_path))
    try:
        columns = {
            row[1]: (row[3], row[4])
            for row in conn.execute("PRAGMA table_info(audit_log)")
        }
    finally:
        conn.close()
    assert columns["effects"] == (1, "'[]'")
    assert columns["capability_accesses"] == (1, "'[]'")


def test_v1_to_v2_migration_is_idempotent_across_reopens(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    db_path = tmp_path / "v1_reopen.db"
    _write_v1_file(db_path)

    ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    assert _user_version(db_path) == SCHEMA_VERSION
    conn = sqlite3.connect(str(db_path))
    try:
        columns = [
            row[1] for row in conn.execute("PRAGMA table_info(audit_log)")
        ]
    finally:
        conn.close()
    assert columns.count("effects") == 1
    assert columns.count("capability_accesses") == 1


def test_v2_reader_accepts_old_shape_insert_from_already_open_v1_connection(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    db_path = tmp_path / "open_v1_writer.db"
    old_connection = sqlite3.connect(str(db_path))
    try:
        _initialize_v1_connection(old_connection)
        store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
        assert _user_version(db_path) == SCHEMA_VERSION

        old_connection.execute(
            """
            INSERT INTO audit_log
                (ts, actor, role, action, target_type, target_id, params,
                 outcome, writes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-07-25T00:00:00+00:00",
                "old-process",
                "Member",
                "LegacyAction",
                "Widget",
                None,
                "{}",
                "ok",
                "[]",
            ),
        )
        old_connection.commit()

        entries = store.audit_entries()
    finally:
        old_connection.close()

    assert len(entries) == 1
    assert entries[0].action == "LegacyAction"
    assert entries[0].effects == []
    assert entries[0].capability_accesses == []


def test_current_stamp_missing_other_required_column_is_coded_refusal(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    db_path = tmp_path / "bad_v2.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_LEGACY_AUDIT_LOG_MISSING_WRITES_DDL)
        conn.execute("ALTER TABLE objects ADD COLUMN page_token TEXT")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()

    with raises_code(ConflictError, "STORE_SCHEMA_INCOMPATIBLE") as exc_info:
        ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    assert "audit_log" in str(exc_info.value)
    assert "writes" in str(exc_info.value)
    assert _user_version(db_path) == SCHEMA_VERSION


def test_partially_migrated_current_file_self_heals_owned_column(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    db_path = tmp_path / "partial_v2.db"
    _write_v1_file(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "ALTER TABLE audit_log ADD COLUMN effects "
            "TEXT NOT NULL DEFAULT '[]'"
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()

    ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    conn = sqlite3.connect(str(db_path))
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(audit_log)")
        }
    finally:
        conn.close()
    assert {"effects", "capability_accesses"} <= columns


# -- already-current file: no migration needed -----------------------------


def test_already_stamped_current_file_opens_without_migration(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    db_path = tmp_path / "stamped.db"
    # First create a real current-version file, then close and reopen it.
    store1 = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    obj_id = store1.insert("Widget", {"name": "Foxtrot"}, SRC)
    del store1  # release the sqlite3 connection

    assert _user_version(db_path) == SCHEMA_VERSION

    store2 = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    assert _user_version(db_path) == SCHEMA_VERSION  # unchanged, not re-stamped

    got = store2.read_current("Widget", obj_id)
    assert got is not None
    assert got.payload["name"] == "Foxtrot"


# -- every sub-current stamp is dispatched to migration, not just the -------
# -- versions this engine happens to enumerate (spec `multi-consumer-mcp`, --
# -- `_init_schema` case (c)) -------------------------------------------------


@pytest.mark.parametrize("stamp", range(1, SCHEMA_VERSION))
def test_every_sub_current_stamp_migrates_to_current_and_stays_usable(
    tmp_path: Path, stamp: int, make_registry: RegistryFactory
) -> None:
    """Pins the `0 < user_version < SCHEMA_VERSION` RANGE in `_init_schema`.

    A v1-shaped file (the narrowest real shape any of these stamps could
    hold) is force-stamped with every value in `range(1, SCHEMA_VERSION)` in
    turn -- not just the ones this engine happens to enumerate a fixture
    for above. Reverting that dispatch to an enumerated tuple (the shape it
    rotted into before this fix -- `(1, 2, 3, 4, 5, 8)` -- silently skips
    every stamp the tuple forgets to list, routing it to case (e)'s refusal
    instead of migrating it; this test fails immediately against that
    regression because 6 and 7 (and any future gap) are not in the tuple.
    """
    db_path = tmp_path / f"stamp_{stamp}.db"
    _write_v1_file(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"PRAGMA user_version = {stamp}")
        conn.commit()
    finally:
        conn.close()
    assert _user_version(db_path) == stamp

    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    assert _user_version(db_path) == SCHEMA_VERSION
    # The store is genuinely usable afterward, not just stamped.
    obj_id = store.insert("Widget", {"name": "Golf"}, SRC)
    got = store.read_current("Widget", obj_id)
    assert got is not None
    assert got.payload["name"] == "Golf"
    store.append_audit(
        AuditEntry(
            ts="2026-01-01T00:00:00+00:00",
            actor="tester",
            role="Agent",
            action="Test",
            target_type="Widget",
            target_id=obj_id,
            params={},
            outcome="ok",
        )
    )
    assert len(store.audit_entries()) == 1


# -- future/unsupported version: refused at construction -------------------


def test_higher_version_file_raises_store_version_unsupported(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    db_path = tmp_path / "future.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_LEGACY_T1_DDL)
        conn.execute("PRAGMA user_version = 999")
        conn.commit()
    finally:
        conn.close()

    with raises_code(ConflictError, "STORE_VERSION_UNSUPPORTED") as exc_info:
        ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    message = str(exc_info.value)
    assert "999" in message
    assert str(SCHEMA_VERSION) in message


# -- legacy file that already has page_token: stamped, not re-migrated -----


def test_legacy_file_with_page_token_already_present_is_stamped_not_remigrated(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    db_path = tmp_path / "already_has_token.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_LEGACY_T1_DDL)
        conn.execute("ALTER TABLE objects ADD COLUMN page_token TEXT")
        for obj_id, name in [("w1", "Golf"), ("w2", "Hotel")]:
            conn.execute(
                """
                INSERT INTO objects
                    (object_type, id, payload, valid_from, valid_to,
                     source_system, source_id, extracted_at, page_token)
                VALUES ('Widget', ?, ?, '2026-01-01T00:00:00+00:00', NULL,
                        'legacy', NULL, NULL, ?)
                """,
                (obj_id, f'{{"id": "{obj_id}", "name": "{name}"}}', uuid.uuid4().hex),
            )
        conn.commit()
    finally:
        conn.close()
    assert _user_version(db_path) == 0

    # Opening must NOT raise (no duplicate unique-index error) and must stamp.
    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    assert _user_version(db_path) == SCHEMA_VERSION

    rows = store.read_all("Widget")
    assert len(rows) == 2
    assert {row.payload["name"] for row in rows} == {"Golf", "Hotel"}


# -- legacy file whose page_token column exists but holds NULLs: the ---------
# -- migration must be keyed on DATA, not schema presence (T4 review fix). --


def _write_legacy_file_with_page_token_column(
    path: Path, rows: list[tuple[str, str]], column_type: str
) -> None:
    """A version-0 file that already HAS a `page_token` column (of
    `column_type`) but every row's value is `NULL` -- e.g. a botched prior
    migration, or some other schema variant that added the column without
    ever populating it. A presence-only migrate/skip check stamps this file
    `SCHEMA_VERSION` with its `NULL`s intact; every read then raises an
    uncoded pydantic `ValidationError` (`page_token` expects `str`, gets
    `None`). SQLite's own UNIQUE index does not protect against this --
    a unique index permits any number of `NULL`s."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_LEGACY_T1_DDL)
        conn.execute(f"ALTER TABLE objects ADD COLUMN page_token {column_type}")
        for obj_id, name in rows:
            conn.execute(
                """
                INSERT INTO objects
                    (object_type, id, payload, valid_from, valid_to,
                     source_system, source_id, extracted_at, page_token)
                VALUES ('Widget', ?, ?, '2026-01-01T00:00:00+00:00', NULL,
                        'legacy', NULL, NULL, NULL)
                """,
                (obj_id, f'{{"id": "{obj_id}", "name": "{name}"}}'),
            )
        conn.commit()
    finally:
        conn.close()


def test_legacy_file_with_null_valued_page_token_column_is_self_healed(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    db_path = tmp_path / "null_tokens.db"
    rows = [("w1", "Golf"), ("w2", "Hotel")]
    _write_legacy_file_with_page_token_column(db_path, rows, "TEXT")
    assert _user_version(db_path) == 0

    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    assert _user_version(db_path) == SCHEMA_VERSION

    # Not just stamped -- actually migrated and readable: every row got a
    # real, distinct token backfilled, and read_page/read_all both work.
    assert len(store.read_all("Widget")) == len(rows)
    pages = store.read_page("Widget", batch=10)
    tokens = [p.key for p in pages]
    assert len(tokens) == len(rows)
    assert all(isinstance(t, str) and t for t in tokens)
    assert len(set(tokens)) == len(tokens)


def test_legacy_file_with_integer_typed_null_page_token_column_is_self_healed(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    """Same defect, different column type (a non-TEXT `page_token` column):
    SQLite is dynamically typed, so a differently-declared column's `NULL`s
    are exactly as unreadable, and must be backfilled exactly the same way
    -- the fix keys off `page_token IS NULL`, never the declared type."""
    db_path = tmp_path / "int_tokens.db"
    rows = [("w1", "India"), ("w2", "Juliet")]
    _write_legacy_file_with_page_token_column(db_path, rows, "INTEGER")
    assert _user_version(db_path) == 0

    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    assert _user_version(db_path) == SCHEMA_VERSION

    assert len(store.read_all("Widget")) == len(rows)
    pages = store.read_page("Widget", batch=10)
    tokens = [p.key for p in pages]
    assert len(tokens) == len(rows)
    assert all(isinstance(t, str) and t for t in tokens)
    assert len(set(tokens)) == len(tokens)


# -- migration is atomic: a mid-backfill crash leaves the file EXACTLY as ---
# -- found, never half-migrated, and a subsequent open retries cleanly. ----


def test_migration_mid_backfill_crash_leaves_file_untouched_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_registry: RegistryFactory,
) -> None:
    """Pins the load-bearing atomicity of `_migrate_add_page_token`'s
    transaction: replacing its `BEGIN IMMEDIATE`/commit/rollback with
    autocommit leaves every OTHER test in the suite green, but a real crash
    mid-backfill then leaves a file with `page_token` present, some rows
    NULL, and `user_version` about to be lied about as `SCHEMA_VERSION` on
    the next open -- exactly the permanent lying stamp AC11 forbids. This
    test fails that mutation directly: it forces a real exception partway
    through the per-row backfill loop and asserts the file is left
    byte-for-byte as it was found."""
    db_path = tmp_path / "crash.db"
    rows = [(f"w{i}", f"Row{i}") for i in range(10)]
    _write_legacy_file(db_path, rows)
    assert _user_version(db_path) == 0

    original_uuid4 = uuid.uuid4
    calls = {"n": 0}

    def _flaky_uuid4() -> uuid.UUID:
        calls["n"] += 1
        if calls["n"] > 3:
            raise RuntimeError("simulated crash mid-backfill")
        return original_uuid4()

    monkeypatch.setattr("ontary.store.migration.uuid.uuid4", _flaky_uuid4)
    try:
        with pytest.raises(RuntimeError, match="simulated crash mid-backfill"):
            ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    finally:
        monkeypatch.setattr("ontary.store.migration.uuid.uuid4", original_uuid4)

    # The file must be left EXACTLY as it was found: still unstamped, still
    # missing `page_token` entirely (the `ALTER TABLE` itself rolled back
    # along with every completed per-row `UPDATE`), and every original row
    # payload byte-for-byte unchanged -- not half-migrated.
    assert _user_version(db_path) == 0
    raw = sqlite3.connect(str(db_path))
    try:
        columns = {row[1] for row in raw.execute("PRAGMA table_info(objects)")}
        assert "page_token" not in columns
        stored = raw.execute(
            "SELECT id, payload FROM objects ORDER BY row_id ASC"
        ).fetchall()
    finally:
        raw.close()
    assert [(r[0], r[1]) for r in stored] == [
        (obj_id, f'{{"id": "{obj_id}", "name": "{name}"}}') for obj_id, name in rows
    ]

    # A clean retry afterward (no more injected failure) must succeed and
    # migrate the whole file normally.
    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    assert _user_version(db_path) == SCHEMA_VERSION
    all_rows = store.read_all("Widget")
    assert len(all_rows) == len(rows)
    tokens = [p.key for p in store.read_page("Widget", batch=len(rows) + 1)]
    assert len(set(tokens)) == len(rows)


# -- migration is concurrency-idempotent: re-issuing it against an ---------
# -- already-migrated file is a clean no-op, never a crash. -----------------


def _new_store_with_conn(conn: sqlite3.Connection) -> ObjectStore:
    """Build a bare `ObjectStore` around an already-open connection without
    going through `__init__`/`_init_schema` (so the private migration
    helper can be exercised directly against a specific connection). Sets
    every attribute `ObjectStore.__init__` sets besides `_conn` itself
    (`_txn_depth`, `_capture`) so a migration path that happens to call
    `self.transaction()` exercises the real transaction bookkeeping instead
    of raising a spurious `AttributeError` unrelated to what the test is
    actually probing."""
    store = object.__new__(ObjectStore)
    store._conn = conn
    store._txn_depth = 0
    store._capture = None
    return store


def test_migration_helper_called_twice_sequentially_is_idempotent_noop(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    """Helper-level idempotency: calling `_migrate_add_page_token` a SECOND
    time, on a fresh connection to a file that a FIRST call already fully
    migrated (and whose changes are already committed), is a clean no-op.

    NOTE what this test does NOT prove: because the two calls run
    sequentially (not concurrently), the second connection's `PRAGMA
    table_info` read -- whether taken before or after `BEGIN IMMEDIATE`
    -- always sees the first connection's already-committed `page_token`
    column, so its `ALTER TABLE` is never even attempted; the
    `except sqlite3.OperationalError` duplicate-column branch is NEVER
    exercised here. That branch (and the stale-pre-lock-read race it
    guards against) is instead pinned by
    `test_migration_helper_forced_to_alter_an_already_migrated_table_is_a_
    clean_noop` below, which forces a real `ALTER TABLE` against a
    genuinely already-migrated table by directly faking a stale
    `PRAGMA table_info` result, and by
    `test_concurrent_processes_opening_same_legacy_file_all_succeed`, which
    races real OS processes against a real multi-thousand-row file."""
    db_path = tmp_path / "race.db"
    rows = [("w1", "Kilo"), ("w2", "Lima")]
    _write_legacy_file(db_path, rows)
    assert _user_version(db_path) == 0

    conn_a = sqlite3.connect(str(db_path))
    conn_a.row_factory = sqlite3.Row
    store_a = _new_store_with_conn(conn_a)
    store_a._migrate_add_page_token()  # "winner": migrates for real

    conn_b = sqlite3.connect(str(db_path))
    conn_b.row_factory = sqlite3.Row
    store_b = _new_store_with_conn(conn_b)
    # "loser": re-issuing the same migration helper against a file that is
    # now already migrated must be a clean no-op, never a raised
    # OperationalError.
    store_b._migrate_add_page_token()

    conn_a.close()
    conn_b.close()

    # The file is left in a fully consistent, usable state: opening it
    # normally afterward stamps it and every row is readable with a real,
    # distinct token -- not corrupted by the double migration attempt.
    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    assert _user_version(db_path) == SCHEMA_VERSION
    assert len(store.read_all("Widget")) == len(rows)
    tokens = [p.key for p in store.read_page("Widget", batch=10)]
    assert len(tokens) == len(rows)
    assert len(set(tokens)) == len(tokens)


_PRE_MIGRATION_OBJECTS_COLUMNS = (
    "row_id",
    "object_type",
    "id",
    "payload",
    "valid_from",
    "valid_to",
    "source_system",
    "source_id",
    "extracted_at",
)
"""Exactly `_LEGACY_T1_DDL`'s `objects` columns -- i.e. `page_token` is
genuinely absent from this set, the way a losing concurrent opener's
pre-lock `PRAGMA table_info` read would have seen it before the T4 fix
moved that read to happen AFTER `BEGIN IMMEDIATE` acquires the write
lock."""


class _StaleTableInfoConnection(sqlite3.Connection):
    """A real `sqlite3.Connection` subclass whose `PRAGMA table_info
    (objects)` calls are stubbed to return `_PRE_MIGRATION_OBJECTS_COLUMNS`
    (i.e. as if `page_token` were not there) regardless of what the table
    ACTUALLY looks like right now -- every other statement (`BEGIN
    IMMEDIATE`, `ALTER TABLE`, the backfill `SELECT`/`UPDATE`, `CREATE
    UNIQUE INDEX`, `COMMIT`/`ROLLBACK`) passes straight through to the real
    connection. This is what a stale pre-lock read looks like made
    deterministic instead of timing-dependent: it forces
    `_migrate_add_page_token` to attempt a real `ALTER TABLE ... ADD COLUMN
    page_token` against a table that (per this same test) has ALREADY been
    migrated for real, so the real sqlite3 engine raises a genuine
    `duplicate column name: page_token` `OperationalError` -- unlike the
    sequential same-file test above, which never provokes that error at
    all because its second connection's un-stubbed read always sees the
    first connection's real, already-committed column."""

    def execute(
        self, sql: str, *args: object, **kwargs: object
    ) -> object:
        if sql.strip() == "PRAGMA table_info(objects)":
            return [{"name": name} for name in _PRE_MIGRATION_OBJECTS_COLUMNS]
        return super().execute(sql, *args, **kwargs)


def test_migration_helper_forced_to_alter_an_already_migrated_table_is_a_clean_noop(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    """Deterministically drives the exact losing-path interleaving the T4
    fix guards against (spec pagination-hardening AC11 / T4 review), with
    NO threads or processes: a connection whose `PRAGMA table_info` read is
    stubbed to the PRE-migration column set is handed to
    `_migrate_add_page_token` AFTER a real, independent connection has
    already migrated the same file for real. The stubbed connection's
    `ALTER TABLE objects ADD COLUMN page_token TEXT` is therefore issued
    for real against an already-migrated table and genuinely raises
    `sqlite3.OperationalError: duplicate column name: page_token` -- this
    is the one thing the purely-sequential
    `test_migration_helper_called_twice_sequentially_is_idempotent_noop`
    test above can never provoke. Asserts:

    1. the call returns cleanly (the `except sqlite3.OperationalError`
       duplicate-column branch in `_migrate_add_page_token` is what makes
       that possible -- reverting it makes this test fail, see the T4
       mutation check);
    2. the file is left intact (still readable, same row count, unique
       index still present);
    3. NO row is re-tokened: every pre-existing row's `page_token` is
       BYTE-FOR-BYTE the same value the real migration assigned it, never
       clobbered by the stubbed connection's no-op backfill pass (an
       outstanding cursor built from a pre-existing token must stay
       valid).
    """
    db_path = tmp_path / "stale_reader.db"
    rows = [("w1", "Mike"), ("w2", "November"), ("w3", "Oscar")]
    _write_legacy_file(db_path, rows)
    assert _user_version(db_path) == 0

    # Real migration, for real, on an independent connection -- the file
    # genuinely has the `page_token` column and every row genuinely has its
    # own distinct token afterward.
    conn_winner = sqlite3.connect(str(db_path))
    conn_winner.row_factory = sqlite3.Row
    store_winner = _new_store_with_conn(conn_winner)
    store_winner._migrate_add_page_token()
    conn_winner.commit()

    tokens_before = {
        row["id"]: row["page_token"]
        for row in conn_winner.execute("SELECT id, page_token FROM objects")
    }
    assert len(tokens_before) == len(rows)
    assert all(tokens_before.values())  # every pre-existing row already tokened
    conn_winner.close()

    # The "loser": its `PRAGMA table_info` read is stubbed to look exactly
    # like the pre-migration schema even though the file it is actually
    # talking to has already been migrated for real above.
    conn_loser = sqlite3.connect(str(db_path), factory=_StaleTableInfoConnection)
    conn_loser.row_factory = sqlite3.Row
    store_loser = _new_store_with_conn(conn_loser)

    store_loser._migrate_add_page_token()  # must not raise

    conn_loser.close()

    # File intact: readable, right row count, unique index present, and --
    # the load-bearing assertion -- no row's token changed.
    raw = sqlite3.connect(str(db_path))
    raw.row_factory = sqlite3.Row
    try:
        index_names = {row[1] for row in raw.execute("PRAGMA index_list(objects)")}
        assert "idx_objects_page_token" in index_names
        tokens_after = {
            row["id"]: row["page_token"]
            for row in raw.execute("SELECT id, page_token FROM objects")
        }
    finally:
        raw.close()
    assert len(tokens_after) == len(rows)
    assert tokens_after == tokens_before  # not one row was re-tokened

    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    assert len(store.read_all("Widget")) == len(rows)


def _open_legacy_store_barriered(
    path: str,
    barrier: "multiprocessing.synchronize.Barrier",
    results: "multiprocessing.Queue[tuple[bool, str]]",
) -> None:
    """Top-level (picklable, `spawn`-safe) worker for
    `test_concurrent_processes_opening_same_legacy_file_all_succeed`: waits
    at `barrier` so every worker process starts at (as close to) the same
    instant, then opens `path` as a real `ObjectStore`, which runs
    `_init_schema` -> `_migrate_add_page_token` against a genuinely shared,
    concurrently contended file. Any exception here (including the
    pre-T4-fix `sqlite3.OperationalError: duplicate column name:
    page_token`) is reported back to the parent through `results` rather
    than crashing the worker silently."""
    try:
        barrier.wait(timeout=30)
        registry = OntologyRegistry()
        registry.register_object_type(
            ObjectTypeDef(
                api_name="Widget",
                display_name="Widget",
                description="A canonical widget",
                layer="L0",
                properties=[
                    PropertyDef(name="id", type="str", required=False),
                    PropertyDef(name="name", type="str"),
                ],
                primary_key="id",
            )
        )
        registry.validate()
        ObjectStore(registry, path)
        results.put((True, ""))
    except BaseException as exc:  # noqa: BLE001 -- report, never swallow silently
        results.put((False, f"{type(exc).__name__}: {exc}"))


def test_concurrent_processes_opening_same_legacy_file_all_succeed(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    """A real multi-process race against a real, sizeable (~8k-row) legacy
    file (spec pagination-hardening AC11 / T4 review, AND spec
    `multi-consumer-mcp` T9 review): several OS processes call
    `ObjectStore(...)` on the SAME file at (as close to) the same instant
    as `multiprocessing` allows, via a `Barrier` so every worker is
    blocked at the same starting line rather than trickling in
    sequentially (which would just hit the already-stamped fast path and
    never actually race).

    TWO distinct races are pinned here, because `_write_legacy_file`'s
    fixture predates BOTH `page_token` (pre-T4) and `tenant` (pre-T9), and
    `_create_or_migrate_unstamped` migrates both columns while opening the
    same file. Before the T4 fix (deferred `BEGIN` + unprotected `ALTER
    TABLE`, `PRAGMA table_info` read before any lock is held), this
    reliably killed most losers with a bare `sqlite3.OperationalError:
    duplicate column name: page_token`. Before the T9 fix (this same
    method calling `_add_tenant_columns` with no `BEGIN IMMEDIATE` of its
    own around that call), the reviewer's mutation baseline shows this
    test instead fails with `sqlite3.OperationalError: duplicate column
    name: tenant` -- the failure this test actually catches TODAY, with
    both fixes reverted. With both fixes in place (`BEGIN IMMEDIATE`
    acquires the write lock before `table_info` is (re-)read for either
    column, plus each column's own duplicate-column catch as a second
    line of defense), every opener must succeed.

    NOTE: because this is a genuine OS-level race, it is PROBABILISTIC --
    reverting the T9 fix in `store.py` still passed this test 55 of 60
    runs in the reviewer's mutation testing, so it barely pins the tenant
    race by itself. It is kept as a real multi-process smoke test for
    both columns, but the load-bearing, DETERMINISTIC guard for the T9
    fix is
    `test_add_tenant_columns_holds_the_write_lock_on_both_entry_paths`
    below, which fails on every single run if either caller's `BEGIN
    IMMEDIATE` disappears or is downgraded to a deferred `BEGIN`."""
    db_path = tmp_path / "concurrent_legacy.db"
    rows = [(f"w{i}", f"Row{i}") for i in range(8_000)]
    _write_legacy_file(db_path, rows)
    assert _user_version(db_path) == 0

    n_workers = 6
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(n_workers)
    results_queue: "multiprocessing.Queue[tuple[bool, str]]" = ctx.Queue()
    procs = [
        ctx.Process(
            target=_open_legacy_store_barriered,
            args=(str(db_path), barrier, results_queue),
        )
        for _ in range(n_workers)
    ]
    for proc in procs:
        proc.start()

    outcomes = [results_queue.get(timeout=60) for _ in procs]
    for proc in procs:
        proc.join(timeout=60)

    failures = [msg for ok, msg in outcomes if not ok]
    assert not failures, f"{len(failures)}/{n_workers} openers failed: {failures}"

    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    assert _user_version(db_path) == SCHEMA_VERSION
    assert len(store.read_all("Widget")) == len(rows)
    tokens = [p.key for p in store.read_page("Widget", batch=len(rows) + 1)]
    assert len(tokens) == len(rows)
    assert len(set(tokens)) == len(tokens)  # every row's token still unique


def test_concurrent_processes_opening_same_v1_file_all_succeed(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    db_path = tmp_path / "concurrent_v1.db"
    _write_v1_file(db_path)
    assert _user_version(db_path) == 1

    n_workers = 6
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(n_workers)
    results_queue: "multiprocessing.Queue[tuple[bool, str]]" = ctx.Queue()
    procs = [
        ctx.Process(
            target=_open_legacy_store_barriered,
            args=(str(db_path), barrier, results_queue),
        )
        for _ in range(n_workers)
    ]
    for proc in procs:
        proc.start()

    outcomes = [results_queue.get(timeout=60) for _ in procs]
    for proc in procs:
        proc.join(timeout=60)

    failures = [msg for ok, msg in outcomes if not ok]
    assert not failures, f"{len(failures)}/{n_workers} openers failed: {failures}"
    assert _user_version(db_path) == SCHEMA_VERSION

    conn = sqlite3.connect(str(db_path))
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(audit_log)")
        }
    finally:
        conn.close()
    assert {"effects", "capability_accesses"} <= columns


def test_add_tenant_columns_holds_the_write_lock_on_both_entry_paths(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    """Deterministic regression guard for the T9 fix (spec
    `multi-consumer-mcp` review), pinning `_add_tenant_columns`'s
    documented precondition -- "assumes the caller holds the write lock (a
    `BEGIN IMMEDIATE` already open)" -- directly, rather than leaning on
    the genuine OS-level race
    `test_concurrent_processes_opening_same_legacy_file_all_succeed` and
    `test_concurrent_processes_opening_same_v1_file_all_succeed` exercise.
    Those race tests are worth keeping, but they are PROBABILISTIC: the
    reviewer's mutation testing found that reverting the T9 fix in
    `store.py` still left the legacy-file race test passing 55 of 60 runs
    -- a guard that misses its own removal ~92% of the time barely guards
    anything.

    This test spies on `ObjectStore._add_tenant_columns` and, at the exact
    instant the method is entered, probes -- from an INDEPENDENT
    connection to the same file -- whether the RESERVED (write) lock is
    genuinely held: `BEGIN IMMEDIATE` on that second connection succeeds
    iff nobody holds it. This is deliberately NOT `self._conn.
    in_transaction`: that real `sqlite3.Connection` property is `True` for
    ANY open transaction, including a plain deferred `BEGIN`, which takes
    only a SHARED lock and does not block another connection's `BEGIN
    IMMEDIATE`. A guard built on `in_transaction` alone passes even when
    `_add_tenant_columns`'s `PRAGMA table_info` reads run unlocked -- the
    reviewer confirmed that changing store.py's `BEGIN IMMEDIATE` to a
    bare `BEGIN` reinstates exactly the race T9 exists to prevent (two
    openers both see `tenant` absent and both `ALTER`) while leaving
    `in_transaction` `True` throughout. The lock probe below discriminates
    that mutation; `in_transaction` does not.

    Checked for BOTH callers (L-c: guard the class, not one instance):

    (a) `_create_or_migrate_unstamped`, opening a legacy, never-stamped
        file -- which, per its own docstring, calls in TWICE: once
        directly (guarded by its own `BEGIN IMMEDIATE`) and once more
        indirectly via the `_migrate_to_current_version()` call it makes
        afterward (guarded by THAT method's own `BEGIN IMMEDIATE`) -- so
        the write lock must be held both times;
    (b) `_migrate_to_current_version`, opening a v1-stamped file directly
        -- exactly one call, also held.

    Confirmed against the reviewer's mutations: deleting
    `_create_or_migrate_unstamped`'s own `BEGIN IMMEDIATE` (store.py,
    `_add_tenant_columns` first call site) turns (a) into `[False, True]`
    (no lock on the first call, the second call's own `BEGIN IMMEDIATE`
    from `_migrate_to_current_version` still holds it); deleting
    `_migrate_to_current_version`'s `BEGIN IMMEDIATE` turns (a) into
    `[True, False]` and (b) into `[False]`; and downgrading that first
    `BEGIN IMMEDIATE` to a bare `BEGIN` (the deferred-lock race shape)
    turns (a) into `[False, True]` too -- undetected by `in_transaction`,
    caught here.
    """
    held: list[bool] = []
    probe_path: list[Path] = []
    original = ObjectStore._add_tenant_columns

    def spy(self: ObjectStore) -> None:
        probe = sqlite3.connect(str(probe_path[0]), timeout=0)
        try:
            probe.execute("BEGIN IMMEDIATE")  # succeeds iff nobody holds RESERVED
            held.append(False)
        except sqlite3.OperationalError:
            held.append(True)
        finally:
            probe.close()
        original(self)

    with mock.patch.object(ObjectStore, "_add_tenant_columns", spy):
        # (a) legacy, never-stamped file -> `_create_or_migrate_unstamped`.
        legacy_path = tmp_path / "legacy_tenant_txn.db"
        _write_legacy_file(legacy_path, [("w1", "Alpha")])
        assert _user_version(legacy_path) == 0
        probe_path[:] = [legacy_path]

        ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(legacy_path))

        assert held == [True, True], (
            "_add_tenant_columns must run with the write lock genuinely "
            f"held on BOTH of its calls from the legacy-file path; got {held}"
        )
        held.clear()

        # (b) v1-stamped file -> `_migrate_to_current_version` directly.
        v1_path = tmp_path / "v1_tenant_txn.db"
        _write_v1_file(v1_path)
        assert _user_version(v1_path) == 1
        probe_path[:] = [v1_path]

        ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(v1_path))

        assert held == [True], (
            "_add_tenant_columns must run with the write lock genuinely "
            f"held from the v1-file path; got {held}"
        )


def test_opening_current_version_file_takes_no_write_lock(tmp_path: Path) -> None:
    """A file already stamped `SCHEMA_VERSION` must open while ANOTHER
    connection holds the write lock.

    Regression guard. Before the v1->v2 audit migration existed,
    `_init_schema`'s current-version branch was a bare no-op, so opening an
    already-migrated file took no lock and never contended. Routing that
    branch unconditionally through `_migrate_add_audit_effect_columns` (which
    opens `BEGIN IMMEDIATE`) silently regressed it: once the connection's busy
    timeout elapsed, any concurrent writer turned an ordinary open into an
    uncoded `sqlite3.OperationalError: database is locked`. The full suite
    stayed green because the multi-process test above races openers against a
    LEGACY file, where taking the lock is correct and expected.

    So this pins the lock-free path directly rather than through a race: hold
    a write transaction open, then open the store. It must succeed.
    """
    db_path = tmp_path / "current.db"
    ObjectStore(OntologyRegistry(), str(db_path))
    assert _user_version(db_path) == SCHEMA_VERSION

    holder = sqlite3.connect(str(db_path))
    try:
        holder.execute("BEGIN IMMEDIATE")
        # No exception, and no downgrade of the stamp.
        ObjectStore(OntologyRegistry(), str(db_path))
        assert _user_version(db_path) == SCHEMA_VERSION
    finally:
        holder.rollback()
        holder.close()


def test_current_version_file_missing_an_owned_column_still_self_heals(
    tmp_path: Path,
) -> None:
    """The lock-free fast path must not cost self-healing.

    A file stamped `SCHEMA_VERSION` whose migration was interrupted (stamp
    landed, column did not) still gets repaired on the next open -- the write
    lock is taken exactly when there is something to add.
    """
    db_path = tmp_path / "half_migrated.db"
    ObjectStore(OntologyRegistry(), str(db_path))

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("ALTER TABLE audit_log DROP COLUMN capability_accesses")
        conn.commit()
    finally:
        conn.close()

    ObjectStore(OntologyRegistry(), str(db_path))

    conn = sqlite3.connect(str(db_path))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_log)")}
    finally:
        conn.close()
    assert {"effects", "capability_accesses"} <= columns
    assert _user_version(db_path) == SCHEMA_VERSION


def test_failing_commit_rolls_back_and_cannot_be_committed_later(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    """A commit that RAISES must roll back, not leave the transaction open.

    Third-pass review finding (2026-07-26). `transaction()` rolled back for an
    exception from the body, but its `commit()` sat in the `else` branch
    unguarded. When `commit()` raised -- SQLITE_BUSY being the realistic case --
    the transaction stayed OPEN while `_txn_depth` returned to 0, so the NEXT
    transaction to commit adopted the abandoned work. In `ActionExecutor.execute`
    that next transaction is the error-audit append, which meant an action whose
    commit failed had its writes and its `pending` effect record become DURABLE,
    with audit rows ["ok", "error"], while the caller got the commit error and no
    dispatcher ever ran.

    Real file-backed, and it drives the failure through `append_audit` exactly as
    the executor's error path does -- an in-memory store or a bare
    `with transaction()` would not exhibit the adoption.
    """
    db_path = tmp_path / "flaky_commit.db"
    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    class FlakyCommitConn:
        """Real connection whose FIRST commit fails, as a lock contention would."""

        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn
            self._commits = 0

        def commit(self) -> None:
            self._commits += 1
            if self._commits == 1:
                raise sqlite3.OperationalError("database is locked")
            self._conn.commit()

        def __getattr__(self, name: str) -> object:
            return getattr(self._conn, name)

    store._conn = FlakyCommitConn(store._conn)  # type: ignore[assignment]

    with raises_code(ConflictError, "STORE_BUSY"):
        with store.transaction():
            store.insert("Widget", {"id": "w-1", "name": "Acme"}, SRC)

    # The error path appends an audit row in a NEW transaction. That commit must
    # not adopt the abandoned write.
    store.append_audit(
        AuditEntry(
            actor="operator-1",
            role="Operator",
            action="Failed",
            target_type="Widget",
            outcome="error",
        )
    )

    assert store.read_all("Widget") == []
    assert [e.outcome for e in store.audit_entries()] == ["error"]


# -- stamped v2 audit schema: migrated to v3 (invocation_id) ----------------


_V2_DDL = """
CREATE TABLE IF NOT EXISTS objects (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_type TEXT NOT NULL,
    id TEXT NOT NULL,
    payload TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NULL,
    source_system TEXT NOT NULL,
    source_id TEXT NULL,
    extracted_at TEXT NULL,
    page_token TEXT NULL
);

CREATE TABLE IF NOT EXISTS links (
    link_type TEXT NOT NULL,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NULL,
    params TEXT NOT NULL,
    outcome TEXT NOT NULL,
    writes TEXT NOT NULL DEFAULT '[]',
    effects TEXT NOT NULL DEFAULT '[]',
    capability_accesses TEXT NOT NULL DEFAULT '[]'
);
"""


def _write_v2_file(path: Path) -> None:
    """A file exactly as the v2 engine (M5, pre-invocation-id) left it, with
    one real audit row so the migration has something to preserve."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_V2_DDL)
        conn.execute(
            "INSERT INTO audit_log (ts, actor, role, action, target_type, "
            "target_id, params, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-01-01T00:00:00+00:00", "old", "Agent", "LegacyAction", "T", None, "{}", "ok"),
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()


def test_stamped_v2_file_gains_invocation_id_column_in_place(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    db_path = tmp_path / "v2.db"
    _write_v2_file(db_path)
    assert _user_version(db_path) == 2

    ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    assert _user_version(db_path) == SCHEMA_VERSION == 9
    conn = sqlite3.connect(str(db_path))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_log)")}
    finally:
        conn.close()
    assert "invocation_id" in columns


def test_v2_rows_survive_the_migration_as_unknown_not_invented(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    """The pre-existing row keeps its data and gets `invocation_id = None`.

    Backfilling an id would assert a correlation that was never recorded --
    the v2 engine had no invocation boundary at all. `None` is the honest
    value, and it is why the column is nullable rather than
    `NOT NULL DEFAULT ''` like the two columns the v1->v2 migration added.
    """
    db_path = tmp_path / "v2.db"
    _write_v2_file(db_path)

    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    entries = store.audit_entries()

    assert len(entries) == 1
    assert entries[0].action == "LegacyAction"
    assert entries[0].outcome == "ok"
    assert entries[0].invocation_id is None


def test_migrated_v2_file_records_invocation_ids_on_new_rows(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    """Old rows stay unattributed; rows written after the migration carry ids."""
    db_path = tmp_path / "v2.db"
    _write_v2_file(db_path)
    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    store.append_audit(
        AuditEntry(
            actor="new",
            role="Agent",
            action="NewAction",
            target_type="T",
            outcome="ok",
            invocation_id="inv-42",
        )
    )

    ids = [entry.invocation_id for entry in store.audit_entries()]
    assert ids == [None, "inv-42"]


# -- v3 -> v4: the durable effect outbox table -------------------------------


_V3_DDL = _V2_DDL.replace(
    "    capability_accesses TEXT NOT NULL DEFAULT '[]'\n",
    "    capability_accesses TEXT NOT NULL DEFAULT '[]',\n"
    "    invocation_id TEXT NULL\n",
)
"""A v3 file: everything v2 had, plus `audit_log.invocation_id`, and NO
`effect_outbox` table -- the one thing v4 adds."""


def _write_v3_file(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_V3_DDL)
        conn.execute(
            "INSERT INTO audit_log (ts, actor, role, action, target_type, "
            "target_id, params, outcome, invocation_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-01-01T00:00:00+00:00",
                "old",
                "Agent",
                "LegacyAction",
                "T",
                None,
                "{}",
                "ok",
                "inv-legacy",
            ),
        )
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()


def _table_exists(path: Path, table: str) -> bool:
    conn = sqlite3.connect(str(path))
    try:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def test_stamped_v3_file_gains_the_effect_outbox_table_in_place(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    db_path = tmp_path / "v3.db"
    _write_v3_file(db_path)
    assert _user_version(db_path) == 3
    assert not _table_exists(db_path, "effect_outbox")

    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    assert _user_version(db_path) == SCHEMA_VERSION == 9
    assert _table_exists(db_path, "effect_outbox")
    # Purely additive: the v3 audit row is untouched, ids and all.
    entries = store.audit_entries()
    assert [(e.action, e.invocation_id) for e in entries] == [
        ("LegacyAction", "inv-legacy")
    ]
    assert store.outbox_entries() == []


def test_v3_to_v4_migration_is_idempotent_across_reopens(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    db_path = tmp_path / "v3.db"
    _write_v3_file(db_path)

    ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    reopened = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    assert _user_version(db_path) == SCHEMA_VERSION
    assert len(reopened.audit_entries()) == 1


def test_current_file_missing_the_outbox_table_self_heals(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    """A v4 stamp whose table creation was interrupted.

    The stamp and the `CREATE TABLE` share one transaction, so this should be
    unreachable in practice -- but `_init_schema` case (d) exists precisely to
    repair a file that reached an impossible state, and the alternative to
    repairing it is an uncoded `no such table` on the first emitted effect.
    """
    db_path = tmp_path / "wounded.db"
    _write_v3_file(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()
    assert not _table_exists(db_path, "effect_outbox")

    ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    assert _table_exists(db_path, "effect_outbox")
    assert _user_version(db_path) == SCHEMA_VERSION


# -- v4 -> v5: audit_log.kind ------------------------------------------------


_V4_DDL = _V3_DDL + """
CREATE TABLE IF NOT EXISTS effect_outbox (
    effect_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    api_name TEXT NOT NULL,
    payload TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    role TEXT NOT NULL,
    emitted_at TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    lease_until TEXT NULL,
    last_error TEXT NULL,
    updated_at TEXT NOT NULL
);
"""
"""A v4 file: the outbox table exists, `audit_log.kind` does not."""


def _write_v4_file(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_V4_DDL)
        conn.execute(
            "INSERT INTO audit_log (ts, actor, role, action, target_type, "
            "target_id, params, outcome, invocation_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-01-01T00:00:00+00:00",
                "old",
                "Agent",
                "LegacyAction",
                "T",
                None,
                "{}",
                "ok",
                "inv-legacy",
            ),
        )
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
    finally:
        conn.close()


def test_stamped_v4_file_gains_the_kind_column_backfilled_as_action(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    """Unlike `invocation_id`, `kind` is backfilled rather than left NULL.

    Every row written before function auditing existed really WAS an action --
    `FunctionRegistry.call` persisted nothing at all -- so `'action'` records a
    fact instead of inventing one, and the column can be `NOT NULL`.
    """
    db_path = tmp_path / "v4.db"
    _write_v4_file(db_path)
    assert _user_version(db_path) == 4

    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    assert _user_version(db_path) == SCHEMA_VERSION == 9
    entries = store.audit_entries()
    assert [(e.action, e.kind, e.invocation_id) for e in entries] == [
        ("LegacyAction", "action", "inv-legacy")
    ]


def test_v4_to_v5_migration_is_idempotent_across_reopens(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    db_path = tmp_path / "v4.db"
    _write_v4_file(db_path)

    ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    reopened = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    assert _user_version(db_path) == SCHEMA_VERSION
    assert len(reopened.audit_entries()) == 1


def test_v3_file_gains_both_the_outbox_table_and_kind_in_one_pass(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    """An upgrade that skips a version: one migration method owns every gap
    this engine can repair, so a v3 file lands on v5 in a single transaction
    rather than needing to be walked through v4 first."""
    db_path = tmp_path / "v3.db"
    _write_v3_file(db_path)

    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    assert _user_version(db_path) == SCHEMA_VERSION == 9
    assert _table_exists(db_path, "effect_outbox")
    assert [e.kind for e in store.audit_entries()] == ["action"]


# -- v5 -> v6: the ontology fingerprint table --------------------------------


_V5_DDL = _V4_DDL.replace(
    "    invocation_id TEXT NULL\n",
    "    invocation_id TEXT NULL,\n    kind TEXT NOT NULL DEFAULT 'action'\n",
)
"""A v5 file: everything v4 had plus `audit_log.kind`, and NO
`ontology_fingerprint` table -- the one thing v6 adds."""


def _write_v5_file(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_V5_DDL)
        conn.execute(
            "INSERT INTO audit_log (ts, actor, role, action, target_type, "
            "target_id, params, outcome, invocation_id, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-01-01T00:00:00+00:00",
                "old",
                "Agent",
                "LegacyAction",
                "T",
                None,
                "{}",
                "ok",
                "inv-legacy",
                "action",
            ),
        )
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
    finally:
        conn.close()


def test_stamped_v5_file_gains_the_fingerprint_table_in_place(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    db_path = tmp_path / "v5.db"
    _write_v5_file(db_path)
    assert _user_version(db_path) == 5
    assert not _table_exists(db_path, "ontology_fingerprint")

    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    assert _user_version(db_path) == SCHEMA_VERSION == 9
    assert _table_exists(db_path, "ontology_fingerprint")
    # The pre-existing audit row is untouched by an additive migration.
    assert [(e.action, e.kind) for e in store.audit_entries()] == [
        ("LegacyAction", "action")
    ]


def test_a_migrated_v5_file_adopts_its_fingerprint_rather_than_claiming_it(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    """`adopted` records what this engine actually knows.

    A v5 file's rows were written before fingerprints existed, so the engine
    cannot claim they were written under today's declarations -- it can only
    record that today's declarations were stamped onto them. The flag preserves
    that difference instead of flattening it into a clean-looking record.
    """
    db_path = tmp_path / "v5.db"
    _write_v5_file(db_path)
    registry = make_registry(object_types=(_WIDGET_TYPE,))

    store = ObjectStore(registry, str(db_path))

    recorded = store.read_ontology_fingerprint()
    assert recorded is not None
    conn = sqlite3.connect(str(db_path))
    try:
        adopted = conn.execute(
            "SELECT adopted FROM ontology_fingerprint WHERE id = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    # This particular v5 fixture holds no OBJECT rows, only an audit row, so the
    # engine can honestly treat the stamp as observed rather than adopted.
    assert adopted == 0


# -- v8 -> v9: audit_log.principal (spec `multi-consumer-mcp` AC10) ----------


_V8_DDL = """
CREATE TABLE IF NOT EXISTS objects (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_type TEXT NOT NULL,
    id TEXT NOT NULL,
    payload TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NULL,
    source_system TEXT NOT NULL,
    source_id TEXT NULL,
    extracted_at TEXT NULL,
    page_token TEXT NOT NULL,
    type_version INTEGER NOT NULL DEFAULT 1,
    tenant TEXT NOT NULL DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS links (
    link_type TEXT NOT NULL,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NULL,
    tenant TEXT NOT NULL DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NULL,
    params TEXT NOT NULL,
    outcome TEXT NOT NULL,
    writes TEXT NOT NULL DEFAULT '[]',
    effects TEXT NOT NULL DEFAULT '[]',
    capability_accesses TEXT NOT NULL DEFAULT '[]',
    invocation_id TEXT NULL,
    kind TEXT NOT NULL DEFAULT 'action',
    tenant TEXT NOT NULL DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS effect_outbox (
    effect_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    api_name TEXT NOT NULL,
    payload TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    role TEXT NOT NULL,
    emitted_at TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    lease_until TEXT NULL,
    last_error TEXT NULL,
    updated_at TEXT NOT NULL,
    tenant TEXT NOT NULL DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS ontology_fingerprint (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    digest TEXT NOT NULL,
    types TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    adopted INTEGER NOT NULL DEFAULT 0,
    versions TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_objects_page_token ON objects (page_token);
"""
"""A v8 file: everything v7 had plus `tenant` on every row-bearing table, and
NO `audit_log.principal` -- the one thing v9 adds."""


def _write_v8_file(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_V8_DDL)
        conn.execute(
            "INSERT INTO audit_log (ts, actor, role, action, target_type, "
            "target_id, params, outcome, invocation_id, kind, tenant) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-01-01T00:00:00+00:00",
                "old",
                "Agent",
                "LegacyAction",
                "T",
                None,
                "{}",
                "ok",
                "inv-legacy",
                "action",
                "default",
            ),
        )
        conn.execute("PRAGMA user_version = 8")
        conn.commit()
    finally:
        conn.close()


def test_stamped_v8_file_gains_the_principal_column_in_place(
    tmp_path: Path,
    make_registry: RegistryFactory,
) -> None:
    """Unlike `kind`, `principal` is left `NULL` rather than backfilled --
    same reasoning as `invocation_id`: a row written before M10 was written by
    an engine with no authenticated-principal concept at all, so there is no
    fact to backfill, only a guess to avoid making."""
    db_path = tmp_path / "v8.db"
    _write_v8_file(db_path)
    assert _user_version(db_path) == 8

    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    assert _user_version(db_path) == SCHEMA_VERSION == 9
    conn = sqlite3.connect(str(db_path))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_log)")}
    finally:
        conn.close()
    assert "principal" in columns
    entries = store.audit_entries()
    assert len(entries) == 1
    assert entries[0].action == "LegacyAction"
    assert entries[0].principal is None


def test_v8_to_v9_migration_is_idempotent_across_reopens(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    db_path = tmp_path / "v8.db"
    _write_v8_file(db_path)

    ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    reopened = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    assert _user_version(db_path) == SCHEMA_VERSION
    assert len(reopened.audit_entries()) == 1
    assert reopened.audit_entries()[0].principal is None


def test_migrated_v8_file_records_principals_on_new_rows(
    tmp_path: Path, make_registry: RegistryFactory
) -> None:
    """Old rows stay unattributed; rows written after the migration carry the
    authenticated principal a caller stamps onto them."""
    db_path = tmp_path / "v8.db"
    _write_v8_file(db_path)
    store = ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    store.append_audit(
        AuditEntry(
            actor="new",
            role="Agent",
            action="NewAction",
            target_type="T",
            outcome="ok",
            principal="svc-agent-7",
        )
    )

    principals = [entry.principal for entry in store.audit_entries()]
    assert principals == [None, "svc-agent-7"]


def test_v9_file_refused_by_a_v8_expecting_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_registry: RegistryFactory,
) -> None:
    """AC10's other half: migration only runs forward. A file this (v9)
    engine already stamped is refused, not silently opened, by an engine that
    still expects v8 -- simulated here by patching `SCHEMA_VERSION` down
    after writing a real v9 file, rather than by hand-rolling a v9 DDL
    fixture that would just duplicate `_SCHEMA_SQL`."""
    db_path = tmp_path / "v9.db"
    ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))
    assert _user_version(db_path) == 9

    monkeypatch.setattr("ontary.store.migration.SCHEMA_VERSION", 8)

    with raises_code(ConflictError, "STORE_VERSION_UNSUPPORTED") as exc_info:
        ObjectStore(make_registry(object_types=(_WIDGET_TYPE,)), str(db_path))

    message = str(exc_info.value)
    assert "9" in message
    assert "8" in message
