"""Postgres `Store` implementation (M8a).

The third backend, and the one that makes the storage seam worth having: the
roadmap called a Postgres backend "a fill-in rather than a rewrite" because
`Store` is a real protocol with a shared conformance suite. This is that claim
being cashed. Every behavioral assertion in `tests/test_store_conformance.py`
runs against this class unchanged.

Written from scratch against the protocol, NOT by subclassing `ObjectStore` --
the same decision `InMemoryStore` documents. A shared base class would let the
conformance suite pass by exercising inherited SQLite helpers through a different
name, which proves nothing about the seam. Refinement of that rule: pure
CONTRACT logic the spec requires to be byte-identical across backends
(refusal checks, validation, codecs) is shared via `ontary.store._shared`
-- see that module's doctrine docstring -- while all storage mechanics
remain from-scratch here, so the conformance suite still proves the seam.

What genuinely differs from SQLite, and why each choice was made:

- **`BIGSERIAL`, not `INTEGER PRIMARY KEY AUTOINCREMENT`.** Same contract: a
  monotonic per-row identity that `read_page`'s cursor orders by.
- **A `schema_meta` table, not `PRAGMA user_version`.** Postgres has no
  per-database integer to stamp, and inventing one on `pg_class` comments would
  be worse than a table anyone can read.
- **No migration ladder.** SQLite carries v1 -> v8 migrations because files
  written by older engines exist in the world. This backend ships AT the current
  SCHEMA_VERSION, so a
  store it did not create at the current version is refused rather than
  migrated. A migration path that has never had anything to migrate is untested
  code pretending to be a safety net.
- **`payload` is `TEXT`, not `JSONB`.** JSONB is what a Postgres deployment
  eventually wants (indexing, containment queries), and it also normalizes: key
  order changes, duplicate keys collapse, numeric literals are rewritten. The
  engine never queries inside a payload in SQL -- filtering happens in Python
  above the seam -- so JSONB would buy nothing today and cost byte-for-byte
  parity with the other two backends. Revisit when a query actually needs it,
  with its own parity review.
- **`FOR UPDATE SKIP LOCKED` for outbox claims.** The lease semantics are
  identical to the other backends (that is the contract), but Postgres can also
  make two concurrent drainers cheap and correct at the row level instead of
  relying on the lease alone. This is the one place where the same declared
  behavior gets a genuinely better implementation.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from ontary.audit import (
    AuditEntry,
    WriteRecord,
)
from ontary.errors import ConflictError
from ontary.fingerprint import OntologyFingerprint
from ontary.meta import Cardinality, OntologyRegistry
from ontary.outbox import OutboxRecord, OutboxState
from ontary.store import _sql
from ontary.store._shared import (
    WriteCapture,
    check_claim_limit,
    check_link_removal_authority,
    check_link_write_authority,
    check_object_removal_authority,
    check_read_page_batch,
    check_update_authority,
    decode_audit_entry,
    encode_audit_entry,
    json_references_object,
    live_link_not_found,
    merge_update,
    prepare_insert,
    resolve_link_type,
    retire_object_refusal,
    unknown_page_token,
    write_references_object,
)
from ontary.store.protocol import EraseResult, check_ontology_fingerprint
from ontary.store.schema import SCHEMA_VERSION
from ontary.store.values import (
    DEFAULT_BATCH,
    DEFAULT_TENANT,
    Lineage,
    PagedRow,
    Source,
    StoredObject,
    _utcnow_iso,
)
from ontary.upcast import upcast_payload

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
this module (M6's `package` CI job now runs that path).
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
-- engine (M8b). This is defense in depth in the literal sense -- every query
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
ALTER TABLE effect_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE effect_outbox FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON objects USING
    (tenant = current_setting('ontary.tenant', true))
    WITH CHECK (tenant = current_setting('ontary.tenant', true));
CREATE POLICY tenant_isolation ON links USING
    (tenant = current_setting('ontary.tenant', true))
    WITH CHECK (tenant = current_setting('ontary.tenant', true));
CREATE POLICY tenant_isolation ON audit_log USING
    (tenant = current_setting('ontary.tenant', true))
    WITH CHECK (tenant = current_setting('ontary.tenant', true));
CREATE POLICY tenant_isolation ON effect_outbox USING
    (tenant = current_setting('ontary.tenant', true))
    WITH CHECK (tenant = current_setting('ontary.tenant', true));
"""
"""Applied once at schema creation, when `rls=True` (the default).

Deliberately NOT applied to `schema_meta` or `ontology_fingerprint`: both are
per-DEPLOYMENT rather than per-tenant (one process serves one declaration), and a
policy on them would make the drift check invisible to every tenant but one.
"""


class PostgresStore:
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
        accept_ontology_drift: bool = False,
        tenant: str = DEFAULT_TENANT,
        rls: bool = True,
    ) -> None:
        """`tenant` scopes every read and write; `rls` additionally has the
        DATABASE enforce that (M8b).

        Both layers, on purpose. The engine's `AND tenant = %s` on every statement
        is the primary mechanism; the RLS policies are what catch the day someone
        adds a query and forgets one. `rls=False` exists for a deployment whose
        role cannot own the tables (RLS policies are DDL) or one that manages
        policies itself -- it disables the second layer, never the first.

        Note the ceiling: **a superuser bypasses RLS**, always, by Postgres
        design. An application connecting as a superuser gets engine-level
        filtering only, whatever `rls` says.
        """
        if not tenant:
            raise ValueError("tenant must be a non-empty string")
        psycopg = _psycopg()
        self._registry = registry
        self._tenant = tenant
        self._rls = rls
        self._conn = psycopg.connect(dsn, autocommit=False)
        self._txn_depth = 0
        self._write_capture = WriteCapture()
        self._init_schema()
        # Session-scoped, not per-transaction: not every read this backend makes
        # opens one, and an unset variable means the policies match nothing --
        # which is the right failure but a useless one to hit on every SELECT.
        with self._conn.cursor() as cur:
            cur.execute("SELECT set_config('ontary.tenant', %s, false)", (tenant,))
        self._conn.commit()
        check_ontology_fingerprint(self, registry, accept_drift=accept_ontology_drift)

    # -- schema ----------------------------------------------------------

    def _init_schema(self) -> None:
        """Create the schema at `SCHEMA_VERSION`, or refuse a store this engine
        did not write.

        No migration ladder, and that is a decision rather than an omission: the
        SQLite backend carries v1 -> v8 migrations because files written by older
        engines exist. Nothing has ever written a Postgres store with this
        package, so every database is either empty (create it) or already at this
        version (use it). Anything else is refused with the same coded error a
        wrong-version SQLite file gets, because the honest answer is identical:
        this engine cannot read it, and guessing is worse than stopping.
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
            # AC11 exists to rule out.
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

    @contextmanager
    def capture_action_writes(self) -> Iterator[list[WriteRecord]]:
        with self._write_capture.capture() as records:
            yield records

    def _record_write(self, record: WriteRecord) -> None:
        self._write_capture.record(record)

    # -- objects ---------------------------------------------------------

    def insert(self, obj_type: str, payload: dict[str, Any], source: Source) -> str:
        prepared = prepare_insert(
            self._registry, obj_type, payload, capturing=self._write_capture.active
        )
        obj_def, payload, obj_id = prepared.obj_def, prepared.payload, prepared.obj_id

        # `json.dumps` here, not `_safe_json_dumps`: an unencodable payload must
        # fail the write on every backend (SQLite raises from its own
        # `json.dumps`), or a value that is storable in tests becomes unstorable
        # in production.
        encoded = json.dumps(payload)
        now = _utcnow_iso()
        with self.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO objects
                        (object_type, id, payload, valid_from, valid_to,
                         source_system, source_id, extracted_at, page_token,
                         type_version, tenant)
                    VALUES (%s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        obj_type,
                        str(obj_id),
                        encoded,
                        now,
                        source.source_system,
                        source.source_id,
                        source.extracted_at,
                        uuid.uuid4().hex,
                        obj_def.version,
                        self._tenant,
                    ),
                )
        self._record_write(WriteRecord(op="create", object_type=obj_type, object_id=str(obj_id)))
        return str(obj_id)

    def update(
        self,
        obj_type: str,
        obj_id: str,
        payload_changes: dict[str, Any],
        source: Source,
    ) -> None:
        obj_def = check_update_authority(
            self._registry, obj_type, payload_changes, capturing=self._write_capture.active
        )
        current = self.read_current(obj_type, obj_id)
        merged = merge_update(obj_def, obj_type, obj_id, current, payload_changes)
        encoded = json.dumps(merged)
        now = _utcnow_iso()

        with self.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE objects SET valid_to = %s
                    WHERE object_type = %s AND id = %s AND valid_to IS NULL
                      AND tenant = %s
                    """,
                    (now, obj_type, obj_id, self._tenant),
                )
                cur.execute(
                    """
                    INSERT INTO objects
                        (object_type, id, payload, valid_from, valid_to,
                         source_system, source_id, extracted_at, page_token,
                         type_version, tenant)
                    VALUES (%s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        obj_type,
                        obj_id,
                        encoded,
                        now,
                        source.source_system,
                        source.source_id,
                        source.extracted_at,
                        uuid.uuid4().hex,
                        obj_def.version,
                        self._tenant,
                    ),
                )
        self._record_write(WriteRecord(op="update", object_type=obj_type, object_id=obj_id))

    def retire_object(self, object_type: str, obj_id: str) -> StoredObject:
        check_object_removal_authority(
            self._registry,
            object_type,
            capturing=self._write_capture.active,
        )
        now = _utcnow_iso()
        with self.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {self._OBJECT_COLUMNS} FROM objects
                    WHERE object_type = %s AND id = %s AND valid_to IS NULL
                      AND tenant = %s
                    ORDER BY row_id DESC
                    LIMIT 1
                    """,
                    (object_type, obj_id, self._tenant),
                )
                current = cur.fetchone()
                if current is None:
                    cur.execute(
                        """
                        SELECT 1 FROM objects
                        WHERE object_type = %s AND id = %s AND tenant = %s
                        LIMIT 1
                        """,
                        (object_type, obj_id, self._tenant),
                    )
                    raise retire_object_refusal(
                        object_type,
                        obj_id,
                        has_history=cur.fetchone() is not None,
                    )
                cur.execute(
                    """
                    UPDATE objects SET valid_to = %s
                    WHERE object_type = %s AND id = %s AND valid_to IS NULL
                      AND tenant = %s
                    """,
                    (now, object_type, obj_id, self._tenant),
                )
                closed = (*current[:5], now, *current[6:])
                retired = self._row_to_stored(closed)
        self._record_write(
            WriteRecord(op="retire", object_type=object_type, object_id=obj_id)
        )
        return retired

    def erase_object_content(self, object_type: str, obj_id: str) -> EraseResult:
        """Tombstone one object's content in one Postgres transaction."""
        self._registry.get_object_type(object_type)
        object_rows_purged = 0
        audit_entries_purged = 0
        outbox_rows_purged = 0

        def object_row_exists(row_type: str, row_id: str) -> bool:
            with self._conn.cursor() as probe:
                probe.execute(
                    """
                    SELECT 1 FROM objects
                    WHERE object_type = %s AND id = %s AND tenant = %s
                    LIMIT 1
                    """,
                    (row_type, row_id, self._tenant),
                )
                return probe.fetchone() is not None

        def references_erased_object(write: dict[str, Any]) -> bool:
            return write_references_object(
                write, object_type, obj_id, self._registry, object_row_exists
            )

        with self.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM objects
                    WHERE object_type = %s AND id = %s AND valid_to IS NULL
                      AND tenant = %s
                    LIMIT 1
                    """,
                    (object_type, obj_id, self._tenant),
                )
                if cur.fetchone() is not None:
                    # Nested calls share this transaction and keep the public
                    # close operation as the single owner of retirement rules.
                    self.retire_object(object_type, obj_id)

                cur.execute(
                    """
                    SELECT row_id, payload FROM objects
                    WHERE object_type = %s AND id = %s AND tenant = %s
                    """,
                    (object_type, obj_id, self._tenant),
                )
                for row_id, encoded_payload in cur.fetchall():
                    if json.loads(encoded_payload):
                        cur.execute(
                            """
                            UPDATE objects SET payload = %s
                            WHERE row_id = %s AND tenant = %s
                            """,
                            ("{}", row_id, self._tenant),
                        )
                        object_rows_purged += 1

                matching_invocations: set[str] = set()
                cur.execute(
                    """
                    SELECT seq, kind, target_type, target_id, params, effects,
                           writes, invocation_id
                    FROM audit_log WHERE tenant = %s ORDER BY seq ASC
                    """,
                    (self._tenant,),
                )
                for (
                    seq,
                    kind,
                    target_type,
                    target_id,
                    encoded_params,
                    encoded_effects,
                    encoded_writes,
                    invocation_id,
                ) in cur.fetchall():
                    if kind == "erasure":
                        continue
                    params = json.loads(encoded_params)
                    effects = json.loads(encoded_effects)
                    writes = json.loads(encoded_writes)
                    references_object = (
                        target_type == object_type and target_id == obj_id
                    ) or json_references_object(
                        params, object_type, obj_id
                    ) or json_references_object(
                        effects, object_type, obj_id
                    ) or any(
                        references_erased_object(write) for write in writes
                    )
                    if not references_object:
                        continue
                    if invocation_id is not None:
                        matching_invocations.add(str(invocation_id))
                    effects_have_content = any(
                        effect["payload"] or effect.get("error") is not None
                        for effect in effects
                    )
                    if params or effects_have_content:
                        scrubbed_effects = [
                            {**effect, "payload": {}, "error": None}
                            for effect in effects
                        ]
                        cur.execute(
                            """
                            UPDATE audit_log SET params = %s, effects = %s
                            WHERE seq = %s AND tenant = %s
                            """,
                            (
                                "{}",
                                json.dumps(scrubbed_effects),
                                seq,
                                self._tenant,
                            ),
                        )
                        audit_entries_purged += 1

                cur.execute(
                    """
                    SELECT effect_id, invocation_id, payload, last_error
                    FROM effect_outbox WHERE tenant = %s
                    ORDER BY emitted_at ASC, seq ASC
                    """,
                    (self._tenant,),
                )
                for (
                    effect_id,
                    invocation_id,
                    encoded_payload,
                    last_error,
                ) in cur.fetchall():
                    payload = json.loads(encoded_payload)
                    references_object = (
                        invocation_id in matching_invocations
                        or json_references_object(payload, object_type, obj_id)
                    )
                    if references_object and (
                        payload or last_error is not None
                    ):
                        cur.execute(
                            """
                            UPDATE effect_outbox
                            SET payload = %s, last_error = NULL
                            WHERE effect_id = %s AND tenant = %s
                            """,
                            ("{}", effect_id, self._tenant),
                        )
                        outbox_rows_purged += 1

        return EraseResult(
            object_rows_purged=object_rows_purged,
            audit_entries_purged=audit_entries_purged,
            outbox_rows_purged=outbox_rows_purged,
        )

    def object_erasure_state(
        self, object_type: str, obj_id: str
    ) -> tuple[bool, bool]:
        """Return whether object rows and erasable object content exist."""
        self._registry.get_object_type(object_type)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT valid_to, payload FROM objects
                WHERE object_type = %s AND id = %s AND tenant = %s
                """,
                (object_type, obj_id, self._tenant),
            )
            rows = cur.fetchall()
            has_content = any(
                valid_to is None or bool(json.loads(payload))
                for valid_to, payload in rows
            )
            return bool(rows), has_content

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
            type_version,
        ) = row
        # The read boundary, same as the other two backends: upcast an older row
        # here so every surface above sees one shape (M9b).
        upcast = upcast_payload(
            self._registry,
            object_type,
            json.loads(payload),
            type_version,
            object_id=str(obj_id),
        )
        return StoredObject(
            payload=upcast,
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
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_CURRENT_SELECT_TEMPLATE, "postgres"),
            (obj_type, obj_id, self._tenant),
            dialect="postgres",
        ).rows
        row = rows[0] if rows else None
        return None if row is None else self._row_to_stored(row)

    def read_last(self, obj_type: str, obj_id: str) -> StoredObject | None:
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

    def create_link(self, link_type: str, from_id: str, to_id: str) -> None:
        link_def = check_link_write_authority(
            self._registry, link_type, capturing=self._write_capture.active
        )

        with self.transaction() as conn:
            with conn.cursor() as cur:
                if link_def.cardinality in (
                    Cardinality.ONE_TO_ONE,
                    Cardinality.MANY_TO_ONE,
                ):
                    cur.execute(
                        "SELECT COUNT(*) FROM links WHERE link_type = %s "
                        "AND from_id = %s AND valid_to IS NULL AND tenant = %s",
                        (link_type, from_id, self._tenant),
                    )
                    existing = cur.fetchone()
                    if existing is not None and int(existing[0]) > 0:
                        raise ConflictError(
                            f"{link_type!r} is {link_def.cardinality.value}: "
                            f"{from_id!r} already has a link",
                            code="CARDINALITY_VIOLATION",
                        )
                if link_def.cardinality in (
                    Cardinality.ONE_TO_ONE,
                    Cardinality.ONE_TO_MANY,
                ):
                    cur.execute(
                        "SELECT COUNT(*) FROM links WHERE link_type = %s "
                        "AND to_id = %s AND valid_to IS NULL AND tenant = %s",
                        (link_type, to_id, self._tenant),
                    )
                    existing = cur.fetchone()
                    if existing is not None and int(existing[0]) > 0:
                        raise ConflictError(
                            f"{link_type!r} is {link_def.cardinality.value}: "
                            f"{to_id!r} already has a link",
                            code="CARDINALITY_VIOLATION",
                        )
                cur.execute(
                    """
                    INSERT INTO links
                        (link_type, from_id, to_id, valid_from, valid_to, tenant)
                    VALUES (%s, %s, %s, %s, NULL, %s)
                    """,
                    (link_type, from_id, to_id, _utcnow_iso(), self._tenant),
                )
        self._record_write(
            WriteRecord(op="link", link_type=link_type, from_id=from_id, to_id=to_id)
        )

    def close_link(self, link_type: str, from_id: str, to_id: str) -> bool:
        check_link_removal_authority(
            self._registry,
            link_type,
            capturing=self._write_capture.active,
        )
        with self.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE links SET valid_to = %s
                    WHERE link_type = %s AND from_id = %s AND to_id = %s
                      AND valid_to IS NULL AND tenant = %s
                    """,
                    (_utcnow_iso(), link_type, from_id, to_id, self._tenant),
                )
                if cur.rowcount == 0:
                    raise live_link_not_found(link_type, from_id, to_id)
        self._record_write(
            WriteRecord(
                op="unlink", link_type=link_type, from_id=from_id, to_id=to_id
            )
        )
        return True

    def links_from(self, link_type: str, from_id: str) -> list[str]:
        resolve_link_type(self._registry, link_type)
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_FROM_SELECT_TEMPLATE, "postgres"),
            (link_type, from_id, self._tenant),
            dialect="postgres",
        ).rows
        return [str(row[0]) for row in rows]

    def links_to(self, link_type: str, to_id: str) -> list[str]:
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
        resolve_link_type(self._registry, link_type)
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_FROM_ASOF_SELECT_TEMPLATE, "postgres"),
            (link_type, from_id, asof, asof, self._tenant),
            dialect="postgres",
        ).rows
        return [str(row[0]) for row in rows]

    def links_to_asof(self, link_type: str, to_id: str, asof: str) -> list[str]:
        resolve_link_type(self._registry, link_type)
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_TO_ASOF_SELECT_TEMPLATE, "postgres"),
            (link_type, to_id, asof, asof, self._tenant),
            dialect="postgres",
        ).rows
        return [str(row[0]) for row in rows]

    # -- audit -----------------------------------------------------------

    def append_audit(self, entry: AuditEntry) -> None:
        """Persist one audit entry. Never raises for an unencodable value
        (declared-contracts AC12) -- `_safe_json_dumps` degrades it to a
        placeholder, exactly as the other backends do."""
        fields = encode_audit_entry(entry)
        with self.transaction() as conn:
            _sql.execute(
                conn,
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
                    fields.effects,
                    fields.capability_accesses,
                    fields.invocation_id,
                    fields.kind,
                    self._tenant,
                    fields.principal,
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

    # -- effect outbox ---------------------------------------------------

    @staticmethod
    def _row_to_outbox(row: tuple[Any, ...]) -> OutboxRecord:
        return OutboxRecord(
            effect_id=row[0],
            invocation_id=row[1],
            seq=row[2],
            api_name=row[3],
            payload=json.loads(row[4]),
            action=row[5],
            actor_id=row[6],
            role=row[7],
            emitted_at=datetime.fromisoformat(row[8]),
            state=row[9],
            attempts=row[10],
            next_attempt_at=datetime.fromisoformat(row[11]),
            lease_until=(datetime.fromisoformat(row[12]) if row[12] is not None else None),
            last_error=row[13],
            updated_at=datetime.fromisoformat(row[14]),
        )

    def enqueue_effects(self, records: Sequence[OutboxRecord]) -> None:
        with self.transaction() as conn:
            for record in records:
                _sql.execute(
                    conn,
                    _sql.render(
                        _sql.EFFECT_OUTBOX_INSERT_TEMPLATE,
                        "postgres",
                        placeholder_count=len(_sql.EFFECT_OUTBOX_COLUMNS),
                    ),
                    (
                        record.effect_id,
                        record.invocation_id,
                        record.seq,
                        record.api_name,
                        json.dumps(record.payload),
                        record.action,
                        record.actor_id,
                        record.role,
                        record.emitted_at.isoformat(),
                        record.state,
                        record.attempts,
                        record.next_attempt_at.isoformat(),
                        (
                            record.lease_until.isoformat()
                            if record.lease_until is not None
                            else None
                        ),
                        record.last_error,
                        record.updated_at.isoformat(),
                        self._tenant,
                    ),
                    dialect="postgres",
                )

    def claim_due_effects(
        self, *, limit: int, now: datetime, lease: timedelta
    ) -> list[OutboxRecord]:
        """Lease due rows, using `FOR UPDATE SKIP LOCKED`.

        The declared semantics are the other backends' (state, due time, and
        lease decide eligibility). Postgres adds real row-level exclusion on top,
        so two drainers in two processes never even contend for the same row --
        the difference between a lease that is the ONLY protection and a lease
        that is the recovery mechanism for a crashed holder.
        """
        check_claim_limit(limit)
        now_iso = now.isoformat()
        lease_until = (now + lease).isoformat()
        with self.transaction() as conn:
            rows = _sql.execute(
                conn,
                _sql.render(_sql.EFFECT_OUTBOX_POSTGRES_CLAIM_TEMPLATE, "postgres"),
                (
                    lease_until,
                    now_iso,
                    now_iso,
                    now_iso,
                    self._tenant,
                    limit,
                ),
                dialect="postgres",
            ).rows
        claimed = [self._row_to_outbox(row) for row in rows]
        # `RETURNING` hands back the post-update row, so ordering is not
        # guaranteed by the UPDATE itself -- restore emission order here, which is
        # the contract the other backends satisfy via their ORDER BY.
        claimed.sort(key=lambda record: (record.emitted_at, record.seq))
        return claimed

    def release_effect_claim(self, effect_id: str, *, now: datetime) -> None:
        with self.transaction() as conn:
            _sql.execute(
                conn,
                _sql.render(_sql.EFFECT_OUTBOX_RELEASE_TEMPLATE, "postgres"),
                (now.isoformat(), effect_id, self._tenant),
                dialect="postgres",
            )

    def resolve_effect(
        self,
        effect_id: str,
        *,
        state: OutboxState,
        next_attempt_at: datetime,
        error: str | None,
        now: datetime,
    ) -> None:
        with self.transaction() as conn:
            _sql.execute(
                conn,
                _sql.render(_sql.EFFECT_OUTBOX_RESOLVE_TEMPLATE, "postgres"),
                (
                    state,
                    next_attempt_at.isoformat(),
                    error,
                    now.isoformat(),
                    effect_id,
                    self._tenant,
                ),
                dialect="postgres",
            )

    def outbox_entries(self) -> list[OutboxRecord]:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.EFFECT_OUTBOX_SELECT_TEMPLATE, "postgres"),
            (self._tenant,),
            dialect="postgres",
        ).rows
        return [self._row_to_outbox(row) for row in rows]

    # -- ontology fingerprint --------------------------------------------

    def read_ontology_fingerprint(self) -> OntologyFingerprint | None:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.ONTOLOGY_FINGERPRINT_SELECT_TEMPLATE, "postgres"),
            dialect="postgres",
        ).rows
        row = rows[0] if rows else None
        if row is None:
            return None
        return OntologyFingerprint(
            digest=row[0], types=json.loads(row[1]), versions=json.loads(row[2])
        )

    def write_ontology_fingerprint(
        self, fingerprint: OntologyFingerprint, *, adopted: bool
    ) -> None:
        with self.transaction() as conn:
            _sql.execute(
                conn,
                _sql.render(_sql.ONTOLOGY_FINGERPRINT_UPSERT_TEMPLATE, "postgres"),
                (
                    fingerprint.digest,
                    json.dumps(fingerprint.types, sort_keys=True),
                    _utcnow_iso(),
                    int(adopted),
                    json.dumps(fingerprint.versions, sort_keys=True),
                ),
                dialect="postgres",
            )

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        """Close the connection. Not part of the `Store` protocol -- the other two
        backends have nothing to close -- but a Postgres connection is a real
        server-side resource and a long-lived process needs a way to release it."""
        self._conn.close()
