"""Postgres `Store` implementation.

The third backend, and the one that makes the storage seam worth having: the
roadmap called a Postgres backend "a fill-in rather than a rewrite" because
`Store` is a real protocol with a shared conformance suite. This is that claim
being cashed. Every behavioral assertion in `tests/test_store_conformance.py`
runs against this class unchanged.

The write path (order of steps, refusals, cardinality, the clock, and write
capture) is `ontary.store._core.StoreCore`'s; this module supplies the
advisory-lock transaction and the storage steps it calls, plus its own reads.
Pure contract logic shared with the other backends lives in
`ontary.store._shared`.

What genuinely differs from SQLite, and why each choice was made:

- **`BIGSERIAL`, not `INTEGER PRIMARY KEY AUTOINCREMENT`.** Same contract: a
  monotonic per-row identity that `read_page`'s cursor orders by.
- **A `schema_meta` table, not `PRAGMA user_version`.** Postgres has no
  per-database integer to stamp, and inventing one on `pg_class` comments would
  be worse than a table anyone can read.
- **No migration ladder** -- which is now what SQLite does too, so this is a
  shared rule rather than a Postgres-only one. Both backends ship AT the current
  SCHEMA_VERSION, so a store they did not create at that version is refused
  rather than migrated. A migration path is untested code pretending to be a
  safety net unless something is actually exercising it.
- **`payload` is `TEXT`, not `JSONB`.** JSONB is what a Postgres deployment
  eventually wants (indexing, containment queries), and it also normalizes: key
  order changes, duplicate keys collapse, numeric literals are rewritten. The
  engine never queries inside a payload in SQL -- filtering happens in Python
  above the seam -- so JSONB would buy nothing today and cost byte-for-byte
  parity with the other two backends. Revisit when a query actually needs it,
  with its own parity review.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ontary.audit import AuditEntry
from ontary.errors import ConflictError
from ontary.meta import OntologyRegistry
from ontary.store import _sql
from ontary.store._core import StoreCore
from ontary.store._shared import (
    AuditRowFields,
    canonical_id,
    check_read_page_batch,
    decode_audit_entry,
    resolve_link_type,
    unknown_page_token,
)
from ontary.store.schema import SCHEMA_VERSION
from ontary.store.values import (
    DEFAULT_BATCH,
    DEFAULT_TENANT,
    Lineage,
    PagedRow,
    Source,
    StoredObject,
)

__all__ = ["POSTGRES_EXTRA_HINT", "PostgresStore"]


POSTGRES_EXTRA_HINT = (
    "the Postgres backend needs the optional `psycopg` dependency, which the "
    "core package does not install -- run `pip install 'ontary[postgres]'` "
    "(or `uv add 'ontary[postgres]'`) and try again"
)
"""What to tell someone whose `postgres` extra is missing.

Same reasoning as `MCP_EXTRA_HINT`: `from ontary.store.postgres import
PostgresStore` is one line away from any deployment guide, and a bare
`ModuleNotFoundError: No module named 'psycopg'` does not mention that an extra
exists. Found the same way too -- by installing the core package and importing
this module (the `package` CI job now runs that path).
"""


def _psycopg() -> Any:
    """Import psycopg on demand, or explain how to install it."""
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        if exc.name is not None and exc.name.split(".")[0] != "psycopg":
            raise
        raise ImportError(POSTGRES_EXTRA_HINT) from exc
    return psycopg


_SCHEMA_SQL = _sql.render_schema("postgres")
"""Fresh-create Postgres DDL rendered from the shared table specs."""

_RLS_SQL = """
-- Row-level security: the DATABASE enforces tenant isolation, not just the
-- engine. This is defense in depth in the literal sense -- every query
-- this backend issues already carries `AND tenant = %s`, and these policies are
-- what catches the day one of them does not.
--
-- `FORCE` matters: without it, policies do not apply to the table's OWNER, which
-- is usually the role the application connects as, so the protection would be
-- decorative in exactly the deployment that needs it. Superusers bypass RLS
-- regardless -- that is Postgres, not a choice here -- which is why an
-- application role must not be a superuser and why the test for this connects as
-- an ordinary role.
--
-- `current_setting('ontary.tenant', true)` returns NULL when unset, and
-- `tenant = NULL` is never true, so an un-set session sees NOTHING. Fail-closed
-- by construction rather than by remembering to set it.
--
-- `WITH CHECK` repeats `USING` deliberately, and is redundant TODAY: Postgres
-- falls back to the USING expression when a policy omits WITH CHECK, so writes
-- are already checked. Verified by mutation -- deleting these clauses changes no
-- test outcome. Kept because the two expressions are separate concepts (what you
-- may READ vs what you may WRITE) and the day they diverge, the WITH CHECK has
-- to exist to be edited.
ALTER TABLE objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE objects FORCE ROW LEVEL SECURITY;
ALTER TABLE links ENABLE ROW LEVEL SECURITY;
ALTER TABLE links FORCE ROW LEVEL SECURITY;
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON objects USING
    (tenant = current_setting('ontary.tenant', true))
    WITH CHECK (tenant = current_setting('ontary.tenant', true));
CREATE POLICY tenant_isolation ON links USING
    (tenant = current_setting('ontary.tenant', true))
    WITH CHECK (tenant = current_setting('ontary.tenant', true));
CREATE POLICY tenant_isolation ON audit_log USING
    (tenant = current_setting('ontary.tenant', true))
    WITH CHECK (tenant = current_setting('ontary.tenant', true));
"""
"""Applied once at schema creation, when `rls=True` (the default).

Deliberately NOT applied to `schema_meta`: it is per-DEPLOYMENT rather than
per-tenant (one process serves one declaration).
"""


class PostgresStore(StoreCore):
    """Postgres-backed store satisfying the `Store` protocol.

    ```python
    store = PostgresStore(ontology.registry, "postgresql://localhost/ontary")
    ```

    One connection per instance, `autocommit=False`, with the same reentrant
    `transaction()` contract the other backends have: nested calls share the
    outermost transaction, and only the outermost commits or rolls back.

    One instance is bound to one tenant. Every statement carries the tenant
    predicate, and row-level-security policies provide database-enforced defense
    in depth unless ``rls=False``. A Postgres superuser bypasses RLS by design;
    engine predicates still apply in that case.
    """

    def __init__(
        self,
        registry: OntologyRegistry,
        dsn: str,
        *,
        tenant: str = DEFAULT_TENANT,
        rls: bool = True,
    ) -> None:
        """`tenant` scopes every read and write; `rls` additionally has the
        DATABASE enforce that.

        Both layers, on purpose. The engine's `AND tenant = %s` on every statement
        is the primary mechanism; the RLS policies are what catch the day someone
        adds a query and forgets one. `rls=False` exists for a deployment whose
        role cannot own the tables (RLS policies are DDL) or one that manages
        policies itself -- it disables the second layer, never the first.

        Note the ceiling: **a superuser bypasses RLS**, always, by Postgres
        design. An application connecting as a superuser gets engine-level
        filtering only, whatever `rls` says.
        """
        super().__init__(registry, tenant=tenant)
        psycopg = _psycopg()
        self._rls = rls
        self._conn = psycopg.connect(dsn, autocommit=False)
        self._txn_depth = 0
        self._init_schema()
        # Session-scoped, not per-transaction: not every read this backend makes
        # opens one, and an unset variable means the policies match nothing --
        # which is the right failure but a useless one to hit on every SELECT.
        with self._conn.cursor() as cur:
            cur.execute("SELECT set_config('ontary.tenant', %s, false)", (tenant,))
        self._conn.commit()

    # -- schema ----------------------------------------------------------

    def _init_schema(self) -> None:
        """Create the schema at `SCHEMA_VERSION`, or refuse a store this engine
        did not write.

        No migration ladder, and that is a decision rather than an omission --
        the same decision the SQLite backend makes. Every database is either
        empty (create it) or already at this version (use it). Anything else is
        refused with the same coded error a wrong-version SQLite file gets,
        because the honest answer is identical: this engine cannot read it, and
        guessing is worse than stopping.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = 'schema_meta'"
            )
            has_meta = cur.fetchone() is not None
            if has_meta:
                cur.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'")
                row = cur.fetchone()
                stamped = int(row[0]) if row is not None else 0
            else:
                stamped = 0

            if stamped == SCHEMA_VERSION:
                self._conn.commit()
                return
            if stamped != 0:
                self._conn.rollback()
                raise ConflictError(
                    f"Postgres store schema version {stamped} is not supported by "
                    f"this engine (engine SCHEMA_VERSION={SCHEMA_VERSION}); this "
                    "backend ships without a migration ladder -- point it at a "
                    "database created by a matching ontary version, or create a "
                    "fresh one and migrate the data yourself",
                    code="STORE_VERSION_UNSUPPORTED",
                )

            # `stamped == 0`: brand new, or a database whose tables exist without
            # the stamp. The latter is refused rather than adopted -- an unstamped
            # `objects` table was created by something that is not this engine,
            # and stamping it would be the "lying stamp" the SQLite backend's
            # schema check exists to rule out.
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = 'objects'"
            )
            if cur.fetchone() is not None:
                self._conn.rollback()
                raise ConflictError(
                    "this database already has an `objects` table but no ontary "
                    f"schema stamp (expected {SCHEMA_VERSION} in `schema_meta`); "
                    "refusing to adopt a schema this engine did not create",
                    code="STORE_VERSION_UNSUPPORTED",
                )
            cur.execute(_SCHEMA_SQL)
            if self._rls:
                cur.execute(_RLS_SQL)
            cur.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (str(SCHEMA_VERSION),),
            )
        self._conn.commit()

    # -- transactions ----------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Serialized, reentrant transaction matching the other backends.

        The outermost call takes one transaction-scoped advisory lock before
        yielding. Its two keys identify the current database/schema and tenant,
        so read-then-write actions for the same logical store serialize without
        blocking independent tenant stores. This avoids SERIALIZABLE's required
        retry-on-40001 contract and touches no RLS-protected table. As on the
        other backends, cleanup catches ``BaseException``.
        """
        self._txn_depth += 1
        is_outermost = self._txn_depth == 1
        try:
            if is_outermost:
                self._conn.execute(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext(current_database() || ':' || current_schema()), "
                    "hashtext(%s))",
                    (self._tenant,),
                )
            yield self._conn
        except BaseException:
            if is_outermost:
                self._conn.rollback()
            raise
        else:
            if is_outermost:
                try:
                    self._conn.commit()
                except BaseException:
                    self._conn.rollback()
                    raise
        finally:
            self._txn_depth -= 1

    @property
    def in_transaction(self) -> bool:
        return self._txn_depth > 0

    # -- objects: storage steps -----------------------------------------

    def _insert_object_row(
        self,
        obj_type: str,
        obj_id: str,
        encoded_payload: str,
        valid_from: str,
        source: Source,
    ) -> None:
        _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_INSERT_TEMPLATE, "postgres"),
            (
                obj_type,
                obj_id,
                encoded_payload,
                valid_from,
                source.source_system,
                source.source_id,
                source.extracted_at,
                uuid.uuid4().hex,
                self._tenant,
            ),
            dialect="postgres",
        )

    def _live_object_rows(self, obj_type: str, obj_id: str) -> list[StoredObject]:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_LIVE_ROWS_SELECT_TEMPLATE, "postgres"),
            (obj_type, obj_id, self._tenant),
            dialect="postgres",
        ).rows
        return [self._row_to_stored(row) for row in rows]

    def _has_object_history(self, obj_type: str, obj_id: str) -> bool:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_HISTORY_PROBE_TEMPLATE, "postgres"),
            (obj_type, obj_id, self._tenant),
            dialect="postgres",
        ).rows
        return bool(rows)

    def _close_object_rows(self, obj_type: str, obj_id: str, valid_to: str) -> None:
        _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_CLOSE_LIVE_TEMPLATE, "postgres"),
            (valid_to, obj_type, obj_id, self._tenant),
            dialect="postgres",
        )

    _OBJECT_COLUMNS = ", ".join(_sql.OBJECT_COLUMNS)

    def _row_to_stored(self, row: tuple[Any, ...]) -> StoredObject:
        (
            _row_id,
            object_type,
            obj_id,
            payload,
            valid_from,
            valid_to,
            source_system,
            source_id,
            extracted_at,
            _page_token,
        ) = row
        return StoredObject(
            payload=json.loads(payload),
            lineage=Lineage(
                object_type=object_type,
                object_id=str(obj_id),
                valid_from=valid_from,
                valid_to=valid_to,
                source_system=source_system,
                source_id=source_id,
                extracted_at=extracted_at,
            ),
        )

    def read_current(self, obj_type: str, obj_id: str) -> StoredObject | None:
        obj_id = canonical_id(obj_id)
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_CURRENT_SELECT_TEMPLATE, "postgres"),
            (obj_type, obj_id, self._tenant),
            dialect="postgres",
        ).rows
        row = rows[0] if rows else None
        return None if row is None else self._row_to_stored(row)

    def read_last(self, obj_type: str, obj_id: str) -> StoredObject | None:
        obj_id = canonical_id(obj_id)
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_LAST_SELECT_TEMPLATE, "postgres"),
            (obj_type, obj_id, self._tenant),
            dialect="postgres",
        ).rows
        row = rows[0] if rows else None
        return None if row is None else self._row_to_stored(row)

    def read_all(self, obj_type: str) -> list[StoredObject]:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_ALL_SELECT_TEMPLATE, "postgres"),
            (obj_type, self._tenant),
            dialect="postgres",
        ).rows
        return [self._row_to_stored(row) for row in rows]

    def _resolve_page_token(self, obj_type: str, token: str) -> int:
        """Resolve a cursor token to a row identity, scoped to `obj_type`.

        The `object_type` guard is load-bearing and the conformance suite caught
        its absence on this backend's first run: a `page_token` is globally unique
        (uuid4), so a token issued for one type resolves to a real `row_id`
        belonging to ANOTHER type's row. Without the guard that row_id is accepted
        as a resume point into the caller's type -- an arbitrary positional offset
        into an unrelated ordering, silently, which is a fail-open.
        """
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_PAGE_TOKEN_SELECT_TEMPLATE, "postgres"),
            (token, obj_type, self._tenant),
            dialect="postgres",
        ).rows
        row = rows[0] if rows else None
        if row is None:
            raise unknown_page_token(obj_type, token)
        return int(row[0])

    def read_page(
        self, obj_type: str, after_key: str | None = None, batch: int = DEFAULT_BATCH
    ) -> list[PagedRow]:
        check_read_page_batch(batch)
        after_row_id = None if after_key is None else self._resolve_page_token(obj_type, after_key)
        with self._conn.cursor() as cur:
            if after_row_id is None:
                cur.execute(
                    f"""
                    SELECT {self._OBJECT_COLUMNS} FROM objects
                    WHERE object_type = %s AND valid_to IS NULL AND tenant = %s
                    ORDER BY row_id ASC LIMIT %s
                    """,
                    (obj_type, self._tenant, batch),
                )
            else:
                cur.execute(
                    f"""
                    SELECT {self._OBJECT_COLUMNS} FROM objects
                    WHERE object_type = %s AND valid_to IS NULL AND row_id > %s
                      AND tenant = %s
                    ORDER BY row_id ASC LIMIT %s
                    """,
                    (obj_type, after_row_id, self._tenant, batch),
                )
            rows = cur.fetchall()
        return [PagedRow(key=str(row[9]), obj=self._row_to_stored(row)) for row in rows]

    # -- links -----------------------------------------------------------

    def _insert_link_row(
        self, link_type: str, from_id: str, to_id: str, valid_from: str
    ) -> None:
        _sql.execute(
            self._conn,
            _sql.render(_sql.LINK_INSERT_TEMPLATE, "postgres"),
            (link_type, from_id, to_id, valid_from, self._tenant),
            dialect="postgres",
        )

    def _live_link_valid_froms(
        self, link_type: str, from_id: str, to_id: str
    ) -> list[str]:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINK_LIVE_VALID_FROM_SELECT_TEMPLATE, "postgres"),
            (link_type, from_id, to_id, self._tenant),
            dialect="postgres",
        ).rows
        return [str(row[0]) for row in rows]

    def _close_link_rows(
        self, link_type: str, from_id: str, to_id: str, valid_to: str
    ) -> None:
        _sql.execute(
            self._conn,
            _sql.render(_sql.LINK_CLOSE_LIVE_TEMPLATE, "postgres"),
            (valid_to, link_type, from_id, to_id, self._tenant),
            dialect="postgres",
        )

    def links_from(self, link_type: str, from_id: str) -> list[str]:
        from_id = canonical_id(from_id)
        resolve_link_type(self._registry, link_type)
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_FROM_SELECT_TEMPLATE, "postgres"),
            (link_type, from_id, self._tenant),
            dialect="postgres",
        ).rows
        return [str(row[0]) for row in rows]

    def links_to(self, link_type: str, to_id: str) -> list[str]:
        to_id = canonical_id(to_id)
        resolve_link_type(self._registry, link_type)
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_TO_SELECT_TEMPLATE, "postgres"),
            (link_type, to_id, self._tenant),
            dialect="postgres",
        ).rows
        return [str(row[0]) for row in rows]

    def links_from_asof(
        self, link_type: str, from_id: str, asof: str
    ) -> list[str]:
        from_id = canonical_id(from_id)
        resolve_link_type(self._registry, link_type)
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_FROM_ASOF_SELECT_TEMPLATE, "postgres"),
            (link_type, from_id, asof, asof, self._tenant),
            dialect="postgres",
        ).rows
        return [str(row[0]) for row in rows]

    def links_to_asof(self, link_type: str, to_id: str, asof: str) -> list[str]:
        to_id = canonical_id(to_id)
        resolve_link_type(self._registry, link_type)
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_TO_ASOF_SELECT_TEMPLATE, "postgres"),
            (link_type, to_id, asof, asof, self._tenant),
            dialect="postgres",
        ).rows
        return [str(row[0]) for row in rows]

    # -- audit -----------------------------------------------------------

    def _append_audit_row(self, fields: AuditRowFields) -> None:
        _sql.execute(
            self._conn,
            _sql.render(
                _sql.AUDIT_LOG_INSERT_TEMPLATE,
                "postgres",
                placeholder_count=len(_sql.AUDIT_LOG_COLUMNS),
            ),
            (
                fields.ts,
                fields.actor,
                fields.role,
                fields.action,
                fields.target_type,
                fields.target_id,
                fields.params,
                fields.outcome,
                fields.writes,
                fields.capability_accesses,
                fields.invocation_id,
                fields.kind,
                self._tenant,
                fields.principal,
                fields.unscoped_params,
                fields.error_code,
                fields.events,
            ),
            dialect="postgres",
        )

    def audit_entries(self) -> list[AuditEntry]:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.AUDIT_LOG_SELECT_TEMPLATE, "postgres"),
            (self._tenant,),
            dialect="postgres",
        ).rows
        return [
            decode_audit_entry(dict(zip(_sql.AUDIT_LOG_COLUMNS, row, strict=True))) for row in rows
        ]

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        """Close the connection. Not part of the `Store` protocol -- the other two
        backends have nothing to close -- but a Postgres connection is a real
        server-side resource and a long-lived process needs a way to release it."""
        self._conn.close()
