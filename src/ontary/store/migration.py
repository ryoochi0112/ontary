"""The SQLite schema-migration engine, as a mixin for `ObjectStore`.

Moved verbatim from the original `ontary/store.py` (B2 of the staged
refactor); only the class wrapper is new. A mixin rather than free
functions so every migration method stays a real method dispatched through
`self` -- `ObjectStore` subclasses it, and the version-pinning tests that
construct a bare instance or spy on `_add_tenant_columns` keep working
against the same method objects.
"""

from __future__ import annotations

import sqlite3
import uuid

from ontary.errors import ConflictError
from ontary.meta import OntologyRegistry
from ontary.store.schema import (
    _EFFECT_OUTBOX_SQL,
    _ONTOLOGY_FINGERPRINT_SQL,
    _SCHEMA_SQL,
    _SELF_MIGRATED_TABLES,
    SCHEMA_VERSION,
    _schema_columns,
    _split_sql_statements,
)
from ontary.store.values import DEFAULT_TENANT


class SqliteSchemaMigrator:
    """Schema creation/migration half of `ObjectStore` (B2 split).

    Annotations only, no state: `ObjectStore.__init__` owns `_conn` and
    `_registry`; this mixin borrows them.
    """

    _conn: sqlite3.Connection
    _registry: OntologyRegistry

    def _init_schema(self) -> None:
        """Version-gated schema entry point (spec pagination-hardening
        AC11), run before any other query touches `self._conn`. Reads the
        file's own `PRAGMA user_version` and handles exactly these cases, IN
        THIS ORDER:

        (a) `user_version == 0` and `objects` does not exist yet -- a brand
            new file (including `:memory:`, always this case): create every
            table/index fresh, then stamp `SCHEMA_VERSION`.
        (b) `user_version == 0` and `objects` already exists -- a LEGACY,
            never-stamped file from before this stamp existed. INSPECTED,
            never blindly stamped (a lying stamp is worse than no stamp):
            every EXISTING table (`objects`/`links`/`audit_log`) is first
            checked, via `_check_legacy_schema_compatible`, against the full
            column set this engine's own DDL requires -- not just
            `page_token` -- because `CREATE TABLE IF NOT EXISTS` silently
            no-ops against an existing table with a NARROWER shape, so a
            page_token-only check would migrate that one column, stamp, and
            leave e.g. a missing `objects.extracted_at` or
            `audit_log.writes` to fail every subsequent query with an
            uncoded SQL error under an irreversible stamp (spec AC11 P0
            review fix). Only once that check passes is
            `_migrate_add_page_token` ALWAYS invoked (never gated on
            column presence alone -- see that method's docstring for why a
            schema-only check can stamp an unreadable file); either way,
            finish creating any other still-missing table/index, then
            stamp.
        (c) `0 < user_version < SCHEMA_VERSION` -- migrate every gap this
            engine owns (the `audit_log`/`objects`/`effect_outbox` columns
            and tables) and stamp `SCHEMA_VERSION` atomically under one
            `BEGIN IMMEDIATE`. One migration method handles all of them
            because each is additive and idempotent, so an interrupted
            upgrade from any of these versions re-runs correctly from
            wherever it stopped. Deliberately a RANGE, not an enumerated
            tuple of the versions this engine happens to know about today:
            an enumerated list silently stops matching every version bump
            that forgets to extend it, routing that old file to case (e)'s
            refusal instead of migrating it -- exactly the bug found (and
            fixed here) when v9 exposed that versions 6-8 had fallen out of
            what had been `(1, 2, 3, 4, 5)` (regression-pinned by
            `test_every_sub_current_stamp_migrates_to_current_and_stays_usable`
            in `tests/test_store_version.py`, which fails against that
            rotted tuple).

            This RANGE is only safe because `_migrate_to_current_version`
            is, and stays, purely additive: it decides what to do per
            column/table from what is ALREADY THERE, never from which
            version number the file was stamped with, so routing every
            sub-current stamp through the same method is correct regardless
            of which stamp it actually is. `_migrate_to_current_version`'s
            own docstring warns that a migration needing to REWRITE
            existing data cannot be folded into that method -- and a
            version gap like that cannot be folded into this RANGE either:
            dispatching it here would silently skip the rewrite and still
            stamp the file `SCHEMA_VERSION`, a lying stamp. Whoever adds
            the first non-additive migration must give that version its
            own case here (or otherwise revisit this dispatch) rather than
            letting it fall through this range.
        (d) `user_version == SCHEMA_VERSION` -- verify the complete schema
            LOCK-FREE, and take the migration's write lock only if something
            this engine owns is genuinely absent (a file stamped current whose
            migration was interrupted). Verifying without a lock is the point:
            this branch used to be a bare no-op, so concurrent opens of a
            current file never contended, and unconditionally taking
            `BEGIN IMMEDIATE` here turned an ordinary open into an uncoded
            `sqlite3.OperationalError: database is locked` whenever another
            process held the write lock. Self-healing is preserved; the cost
            is paid only by files that actually need healing.
        (e) anything else -- refuse via
            a `ConflictError` with code `STORE_VERSION_UNSUPPORTED` naming both
            versions, before any other query can run.
        """
        user_version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version == 0:
            self._create_or_migrate_unstamped()
        elif 0 < user_version < SCHEMA_VERSION:
            self._migrate_to_current_version()
        elif user_version == SCHEMA_VERSION:
            if any(self._schema_gaps_needing_migration()):
                self._migrate_to_current_version()
        else:
            raise ConflictError(
                f"store file schema version {user_version} is not supported "
                f"by this engine (engine SCHEMA_VERSION={SCHEMA_VERSION}); "
                "refusing to open it -- upgrade/downgrade ontary to match "
                "the file, or migrate the file to this engine's version "
                "before opening it here",
                code="STORE_VERSION_UNSUPPORTED",
            )

    def _create_or_migrate_unstamped(self) -> None:
        """Handles `_init_schema`'s cases (a)/(b): `user_version == 0`.

        Distinguishes "brand new file" from "legacy unstamped file" by
        inspecting `sqlite_master` for the `objects` table -- NOT by
        assuming version 0 always means fresh, which is exactly the lying-
        stamp bug AC11 exists to rule out.

        `_check_legacy_schema_compatible` runs FIRST, before any DDL touches
        the connection: it inspects every table that already exists (there
        is nothing to check for one that doesn't -- `_create_schema` below
        will create it fresh, in full shape) against this engine's own
        required column set (spec AC11 P0 review fix -- see that method and
        the `STORE_SCHEMA_INCOMPATIBLE` catalog entry's rationale for why a
        `page_token`-only check is not enough). Only once that passes is an existing `objects`
        table handed to `_migrate_add_page_token` (that method decides
        internally, from data rather than schema shape, whether there is
        anything left to do -- see its docstring); only a file this engine
        can actually read afterward gets `PRAGMA user_version` set."""
        self._check_legacy_schema_compatible()
        objects_exists = (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'objects'"
            ).fetchone()
            is not None
        )
        if objects_exists:
            self._migrate_add_page_token()
            # Before `_create_schema()` below, for the same reason the versioned
            # migration does it first: that DDL creates `(tenant, ...)` indexes,
            # which a pre-v8 table cannot satisfy.
            #
            # `_migrate_add_page_token` above already committed (and released)
            # its own `BEGIN IMMEDIATE`, so `_add_tenant_columns` -- which
            # documents that it "assumes the caller holds the write lock" --
            # would otherwise run its `PRAGMA table_info` reads and `ALTER
            # TABLE`s completely unguarded here. Two processes opening the
            # same legacy file could then both see `tenant` absent and both
            # issue the `ALTER TABLE`, the way `_migrate_add_page_token`'s own
            # docstring describes for `page_token` pre-T4: a bare
            # `sqlite3.OperationalError: duplicate column name: tenant`. Same
            # fix, same shape: acquire the write lock first, so a losing
            # opener's `PRAGMA table_info` (re-)read -- which happens INSIDE
            # `_add_tenant_columns`, once it holds the lock below -- sees the
            # winner's already-added column and skips its own `ALTER`; the
            # `except sqlite3.OperationalError` inside `_add_tenant_columns`
            # is the second line of defense for the same theoretically
            # possible interleaving `_migrate_add_page_token` guards against.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._add_tenant_columns()
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()
        # Idempotent regardless of (a)/(b): creates whatever tables/indexes
        # are still missing (a fresh file needs everything; a legacy file
        # already migrated above needs only `links`/`audit_log`/`effect_outbox`/
        # the non-page_token indexes if those happen to be absent too -- spec
        # §8's "partial table set from a crashed create" edge case).
        self._create_schema()
        self._migrate_to_current_version()

    _SELF_MIGRATED_COLUMNS: dict[str, frozenset[str]] = {
        "objects": frozenset({"page_token", "type_version", "tenant"}),
        "audit_log": frozenset(
            {
                "effects",
                "capability_accesses",
                "invocation_id",
                "kind",
                "tenant",
                "principal",
            }
        ),
        "links": frozenset({"tenant"}),
        "effect_outbox": frozenset({"tenant"}),
        "ontology_fingerprint": frozenset({"versions"}),
    }
    """Per-table column names `_check_legacy_schema_compatible` EXCLUDES
    from its required-columns check, because this engine already carries an
    active migration for them. Every column NOT listed here that a legacy
    file's existing table is missing has no migration path --
    `STORE_SCHEMA_INCOMPATIBLE` conflict is the only correct response."""

    def _check_legacy_schema_compatible(self) -> None:
        """The P0 fix (spec pagination-hardening AC11 review): before
        `_create_or_migrate_unstamped` does ANY schema-modifying work,
        verify that every table which already exists in this legacy,
        never-stamped file carries every column this engine's own DDL
        (`_SCHEMA_SQL`, via `_schema_columns`) requires -- except columns
        this engine already knows how to add via its explicit migrations.

        Why this is necessary, and why `_migrate_add_page_token` alone was
        not enough: `CREATE TABLE IF NOT EXISTS` (in `_create_schema`) is a
        silent no-op against a table that already exists, NARROWER shape and
        all -- it does not add missing columns to an existing table, only
        create a table that is entirely absent. So a version-0 file
        predating e.g. `objects.extracted_at` or `audit_log.writes` would,
        without this check, sail through the known migrations, then
        `_create_schema` (which skips
        every table that already exists), then get STAMPED
        `SCHEMA_VERSION` -- after which every read/write raises an uncoded
        `sqlite3.OperationalError` naming the missing column, permanently,
        because a file could otherwise carry a current version stamp with
        an unreadable shape. That is exactly the
        "lying stamp" AC11 exists to rule out, one level deeper than the
        page_token-only check this engine originally shipped with.

        Runs BEFORE any DDL in this call so an incompatible file is left
        completely untouched (not merely unstamped) -- `user_version` stays
        0 and the file is byte-for-byte what it was found as, re-inspectable
        by a future engine version that DOES carry a migration for whatever
        column it is missing.

        Raises a `ConflictError` with code `STORE_SCHEMA_INCOMPATIBLE`, naming the table and the exact
        missing column(s) if any existing table falls short; does nothing
        (a handful of cheap `PRAGMA table_info` reads) if every existing
        table already carries every required column, which is the common
        case (a version-0 file needing only the `page_token` migration)."""
        expected_by_table = _schema_columns()
        for table, expected_columns in expected_by_table.items():
            exists = (
                self._conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                is not None
            )
            if not exists:
                continue
            actual_columns = {
                row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            self_migrated = self._SELF_MIGRATED_COLUMNS.get(table, frozenset())
            missing = (expected_columns - self_migrated) - actual_columns
            if missing:
                raise ConflictError(
                    f"legacy store file's `{table}` table is missing "
                    f"column(s) {sorted(missing)} required by this engine's "
                    f"schema (SCHEMA_VERSION={SCHEMA_VERSION}); refusing to "
                    "stamp or open it -- this file was written by an older "
                    "ontary schema shape this engine has no migration for "
                    "(only explicitly owned migration columns are "
                    "auto-migrated); "
                    "open it with a matching older ontary version, or add "
                    "the missing column(s) yourself, before opening it here",
                    code="STORE_SCHEMA_INCOMPATIBLE",
                )

    def _migrate_add_page_token(self) -> None:
        """The version-0 page-token migration (spec pagination-hardening
        AC11): every current `objects` row must carry a
        real, non-`NULL` `page_token`. Adds the column if it is entirely
        absent, then ALWAYS backfills a DISTINCT `uuid4().hex` into every row
        whose `page_token IS NULL` (each row gets its OWN token via a
        per-row `UPDATE ... WHERE row_id = ?` -- never one shared value,
        which would make every backfilled row resolve to the same cursor
        and collide against the unique index built right after), then
        creates the unique index -- all inside one explicit transaction.

        Keyed on DATA, not schema (T4 review fix): the caller no longer
        decides "skip the whole migration" from column PRESENCE alone. A
        version-0 file whose `objects` already HAS a `page_token` column --
        but holding `NULL`s (e.g. a botched prior migration, or a schema
        variant that added the column without ever populating it) -- would,
        under a presence-only check, be stamped `SCHEMA_VERSION` with every
        row still `NULL`, after which every read raises an uncoded pydantic
        `ValidationError` (`page_token` expects `str`, gets `None`) -- a
        permanent lying stamp, exactly what AC11 forbids. SQLite's unique
        index does NOT protect against this: a UNIQUE index permits any
        number of `NULL`s. Filtering the backfill query on `page_token IS
        NULL` (rather than gating the whole method on column presence)
        makes this migration self-healing on every open, not a one-shot
        check: a file with the column present and every row already
        correctly tokened costs one no-op `SELECT` naming zero rows.

        Concurrency-idempotent (T4 review fix): `BEGIN IMMEDIATE` acquires
        the write lock BEFORE `PRAGMA table_info` is read (rather than
        after, which is what let a losing concurrent opener's stale
        pre-lock read of `table_info` drive a blind `ALTER TABLE` against a
        schema the winner had already migrated while the loser waited on
        the lock -- a bare, uncoded `sqlite3.OperationalError: duplicate
        column name: page_token`). Re-reading `table_info` fresh INSIDE the
        transaction means a loser sees the winner's already-added column
        and skips the `ALTER` outright; the `except sqlite3.OperationalError`
        below is kept as a second line of defense for the theoretically
        possible interleaving where two connections both raced past the
        `IMMEDIATE` lock acquisition in different SQLite versions/configs --
        either way, the loser falls through to the same data-driven
        backfill (which finds nothing left to do) rather than raising.

        Crash semantics: `BEGIN IMMEDIATE`/`COMMIT` wrap the `ALTER TABLE`,
        every backfill `UPDATE`, and the `CREATE UNIQUE INDEX` as one atomic
        unit (SQLite's DDL is transactional). If the process dies
        mid-backfill, the transaction never commits, so SQLite's own
        rollback journal reverts the `ALTER TABLE` along with every
        completed `UPDATE` -- the file is left EXACTLY as it was found
        (still `user_version == 0`, still missing `page_token` entirely, or
        still carrying whatever `NULL`s it had), not half-migrated. The next
        `ObjectStore.__init__` on that file lands back in case (b) and
        retries the whole migration from scratch; `_create_or_migrate_
        unstamped` only stamps `SCHEMA_VERSION` *after* this method returns,
        so a rolled-back file can never end up carrying a lying stamp.

        Nullable column, by SQLite's own limitation: the `ALTER TABLE ...
        ADD COLUMN page_token TEXT` above cannot also say `NOT NULL` the way
        `_create_schema`'s fresh-file DDL does (SQLite refuses `ADD COLUMN
        ... NOT NULL` without a non-`NULL` default, and a single shared
        default would make every backfilled row's *un-updated* future default
        collide, defeating the point of a per-row token) -- so a migrated
        file's `objects.page_token` column stays NULLABLE at the schema
        level even after this method returns, unlike a brand-new file's.
        This is harmless in practice: every row that could ever hold `NULL`
        is backfilled above before the transaction commits, and every
        subsequent `insert`/`update` (T2's close-old-insert-new writes
        included) supplies a fresh `uuid4().hex` token itself -- nothing in
        this engine ever inserts a row without one -- so no row is ever
        actually `NULL` after migration; only the column's *declared*
        nullability differs from a fresh file's, not any row's actual data."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(objects)")
            }
            if "page_token" not in columns:
                try:
                    self._conn.execute(
                        "ALTER TABLE objects ADD COLUMN page_token TEXT"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc):
                        raise
                    # A concurrent opener won the race and already added the
                    # column while we waited on the `BEGIN IMMEDIATE` write
                    # lock (see docstring). Fall through to the same
                    # data-driven backfill below -- it will find nothing
                    # left to do and this call becomes a clean no-op.
            row_ids = [
                row["row_id"]
                for row in self._conn.execute(
                    "SELECT row_id FROM objects WHERE page_token IS NULL"
                )
            ]
            for row_id in row_ids:
                self._conn.execute(
                    "UPDATE objects SET page_token = ? WHERE row_id = ?",
                    (uuid.uuid4().hex, row_id),
                )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_objects_page_token "
                "ON objects (page_token)"
            )
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def _schema_gaps_needing_migration(self) -> tuple[frozenset[str], bool]:
        """LOCK-FREE schema check: validate every pre-existing table against
        `_SCHEMA_SQL` (via `_schema_columns`, so the expectation IS the DDL
        re-executed and cannot drift) and return the gaps THIS migration owns
        -- `(missing audit_log columns, effect_outbox table is absent)`.

        Deliberately reads only -- no `BEGIN IMMEDIATE`, no write lock. That
        matters for the already-migrated case: before the v1->v2 migration
        existed, `_init_schema`'s `user_version == SCHEMA_VERSION` branch was
        a bare no-op, so opening a current file took no lock at all and any
        number of processes could open it concurrently while another wrote.
        Taking the migration's write lock on EVERY open regressed that into an
        uncoded `sqlite3.OperationalError: database is locked` after the
        connection's busy timeout, which is both a concurrency regression and
        an uncoded failure this engine's conventions forbid. So the lock is now
        taken only when there is actually something to add -- see `_init_schema`
        case (d).
        """
        expected_by_table = _schema_columns()
        outbox_missing = False
        for table, expected_columns in expected_by_table.items():
            exists = (
                self._conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                is not None
            )
            if not exists:
                if table in _SELF_MIGRATED_TABLES:
                    # A v3 file, or a current-stamped one whose v4 migration was
                    # interrupted: this engine creates the table itself rather
                    # than refusing the file. Nothing can be lost, because a
                    # table that never existed holds no rows.
                    outbox_missing = True
                    continue
                raise ConflictError(
                    f"store file is missing required table `{table}` "
                    f"(SCHEMA_VERSION={SCHEMA_VERSION}); refusing to stamp",
                    code="STORE_SCHEMA_INCOMPATIBLE",
                )
            actual_columns = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            # Per-table, from `_SELF_MIGRATED_COLUMNS` -- NOT hardcoded to
            # `audit_log`, which is what this line said until v7 gave `objects` a
            # self-migrated column of its own (`type_version`) and every legacy
            # file started being refused as incompatible.
            allowed_missing = self._SELF_MIGRATED_COLUMNS.get(table, frozenset())
            missing = (expected_columns - allowed_missing) - actual_columns
            if missing:
                raise ConflictError(
                    f"store file's `{table}` table is missing column(s) "
                    f"{sorted(missing)} required by this engine's schema "
                    f"(SCHEMA_VERSION={SCHEMA_VERSION}); refusing to stamp",
                    code="STORE_SCHEMA_INCOMPATIBLE",
                )

        # Which owned columns are actually absent, across every table that has
        # any -- the signal `_init_schema` case (d) uses to decide whether a
        # current-stamped file needs the migration's write lock at all.
        missing_owned: set[str] = set()
        for table, columns in self._SELF_MIGRATED_COLUMNS.items():
            actual = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            missing_owned |= {f"{table}.{column}" for column in columns - actual}
        return frozenset(missing_owned), outbox_missing

    def _add_tenant_columns(self) -> None:
        """Add v8's `tenant` column to every row-bearing table that lacks it.

        NOT NULL with a constant default, like `audit_log.kind` and for the same
        reason: rows written before tenancy existed genuinely belong to the one
        implicit tenant that wrote them, so backfilling `DEFAULT_TENANT` records a
        fact rather than inventing one.

        Idempotent and callable from both migration entry points, because the v8
        indexes are `(tenant, ...)` and therefore every path that creates an index
        must have run this first. Assumes the caller holds the write lock (a
        `BEGIN IMMEDIATE` already open) -- both callers guarantee this:
        `_migrate_to_current_version` opens one itself before calling in, and
        `_create_or_migrate_unstamped` opens one around its own call for exactly
        this reason (T9 fix: it used to call this completely unguarded, letting
        two processes opening the same legacy file both read `PRAGMA table_info`
        before either held the lock and both issue the `ALTER TABLE` -- a bare
        `sqlite3.OperationalError: duplicate column name: tenant`, the same bug
        `_migrate_add_page_token` had for `page_token` pre-T4).

        Because the write lock is acquired by the caller (not here), the
        `PRAGMA table_info` read below happens fresh *inside* that lock -- a
        losing opener sees the winner's already-added column and skips the
        `ALTER` outright, same as `_migrate_add_page_token`. The `except
        sqlite3.OperationalError` per table is kept as a second line of
        defense for the same theoretically possible interleaving that
        method's docstring describes.
        """
        for table in ("objects", "links", "audit_log", "effect_outbox"):
            columns = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if columns and "tenant" not in columns:
                try:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN tenant TEXT NOT NULL "
                        f"DEFAULT '{DEFAULT_TENANT}'"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc):
                        raise
                    # A concurrent opener won the race and already added the
                    # column while we waited on the caller's `BEGIN IMMEDIATE`
                    # write lock -- fall through to the next table.

    def _migrate_to_current_version(self) -> None:
        """Migrate a v1-v8 file to `SCHEMA_VERSION` atomically and
        idempotently.

        The required column set is derived from `_SCHEMA_SQL` by executing
        it, never duplicated here. Under one `BEGIN IMMEDIATE`, every
        pre-existing table is checked for required columns other than the
        ones this migration owns, missing owned columns are added with
        load-bearing non-null constant defaults, the `effect_outbox` table
        (v4) is created if absent, and `SCHEMA_VERSION` is stamped.
        Re-running this method repairs any single missing piece and is a
        no-op for a complete schema. The pre-check is re-run INSIDE the
        transaction rather than trusting the caller's lock-free result --
        another process may have migrated the file in between.

        One method for every version gap rather than a chain of per-version
        steps, because each gap this engine owns is ADDITIVE and independent:
        a v1 file needs three audit columns and a table, a v3 file needs only
        the table, and an upgrade interrupted between the two needs whatever
        is left. Ordering them would buy nothing and add a state machine to
        get wrong. A v4-only migration that had to REWRITE data could not be
        folded in this way and would need its own method.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._schema_gaps_needing_migration()

            audit_columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(audit_log)")
            }
            if "effects" not in audit_columns:
                self._conn.execute(
                    "ALTER TABLE audit_log ADD COLUMN effects "
                    "TEXT NOT NULL DEFAULT '[]'"
                )
            if "capability_accesses" not in audit_columns:
                self._conn.execute(
                    "ALTER TABLE audit_log ADD COLUMN capability_accesses "
                    "TEXT NOT NULL DEFAULT '[]'"
                )
            if "invocation_id" not in audit_columns:
                # Nullable, unlike the two above: there is no correct value to
                # backfill. A pre-v3 row was written by an engine that had no
                # invocation boundary, and inventing an id for it would assert
                # a correlation that was never recorded. NULL says "unknown",
                # which is the truth. Nullability also means an older engine's
                # INSERT (which never names this column) keeps working against
                # a migrated file.
                self._conn.execute(
                    "ALTER TABLE audit_log ADD COLUMN invocation_id TEXT NULL"
                )
            if "kind" not in audit_columns:
                # NOT NULL with a constant default, unlike `invocation_id`
                # above: every row that predates function auditing really was
                # an action, so backfilling 'action' records a fact rather
                # than inventing one.
                self._conn.execute(
                    "ALTER TABLE audit_log ADD COLUMN kind "
                    "TEXT NOT NULL DEFAULT 'action'"
                )
            # v8's tenant columns come FIRST, before any DDL below that names
            # them: the v8 indexes are `(tenant, ...)`, so creating them against a
            # pre-v8 table fails with `no such column: tenant`. Found by running
            # the v4-file migration test, which is exactly what those fixtures are
            # for.
            self._add_tenant_columns()
            # v4 (spec `durable-effect-outbox` AC11). `CREATE TABLE IF NOT
            # EXISTS` is the whole migration -- there is nothing to backfill,
            # since an effect emitted before v4 was dispatched (or lost)
            # in-process and was never a work item. Executed as part of this
            # transaction, so an interrupted upgrade stamps nothing and the
            # next open retries; `executescript` is deliberately NOT used
            # (it COMMITs any open transaction first, which would break that).
            for statement in _split_sql_statements(
                _EFFECT_OUTBOX_SQL + _ONTOLOGY_FINGERPRINT_SQL
            ):
                self._conn.execute(statement)
            fingerprint_columns = {
                row["name"]
                for row in self._conn.execute(
                    "PRAGMA table_info(ontology_fingerprint)"
                )
            }
            if fingerprint_columns and "versions" not in fingerprint_columns:
                # A v6 file: the table exists without per-type versions. Default
                # '{}' reads as "every type was at version 1", which is true --
                # versions did not exist when that fingerprint was written.
                self._conn.execute(
                    "ALTER TABLE ontology_fingerprint ADD COLUMN versions "
                    "TEXT NOT NULL DEFAULT '{}'"
                )
            object_columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(objects)")
            }
            if "type_version" not in object_columns:
                # v7. NOT NULL with a constant default of 1, then per-type
                # backfill below: a file that reaches this point has already
                # passed the fingerprint check, so its rows genuinely match the
                # declaration -- stamping them with each type's currently
                # declared version records a fact rather than a guess. (The
                # fingerprint check runs after `_init_schema`, so on the very
                # first v6->v7 open the backfill and the check happen in that
                # order; a drifted file is refused immediately after, before any
                # read can trust the stamp.)
                self._conn.execute(
                    "ALTER TABLE objects ADD COLUMN type_version "
                    "INTEGER NOT NULL DEFAULT 1"
                )
                for api_name, obj_def in self._registry.object_types.items():
                    if obj_def.version != 1:
                        self._conn.execute(
                            "UPDATE objects SET type_version = ? "
                            "WHERE object_type = ?",
                            (obj_def.version, api_name),
                        )
            if "principal" not in audit_columns:
                # v9 (spec `multi-consumer-mcp` AC10). Nullable, like
                # `invocation_id` above and for the same reason: there is no
                # correct value to backfill. A pre-v9 row was written by an
                # engine that had no authenticated-principal concept at all,
                # and inventing one would assert a fact that was never
                # recorded. NULL says "we do not know who authenticated",
                # which is the truth.
                self._conn.execute(
                    "ALTER TABLE audit_log ADD COLUMN principal TEXT NULL"
                )
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def _create_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
