"""SQLite-backed object store: history, lineage, links, audit.

Domain-agnostic storage layer for any ontology declared via
`ontary.meta.OntologyRegistry`. Stores objects with bitemporal-ish history
(valid_from/valid_to, close-old-insert-new), links with cardinality
enforcement, and an append-only audit log.

The raw read API (`read_current`, `read_all`) is public but
*engine/trusted-caller-only*: the guarded, security-aware query layer
(`ontary.query.GuardedQuery`) is the only read path a CONSUMER (human/AI
client, MCP tool, Function) may use -- no unguarded public read path for a
consumer is exposed here. See the `Store` protocol (`ontary.store.protocol`)
for the full contract and the layering rule stated on its docstring.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ontary.audit import (
    AuditEntry,
    WriteRecord,
)
from ontary.errors import ConflictError
from ontary.meta import Cardinality, OntologyRegistry
from ontary.store import _sql
from ontary.store._shared import (
    WriteCapture,
    check_link_removal_authority,
    check_link_write_authority,
    check_object_removal_authority,
    check_read_page_batch,
    check_update_authority,
    decode_audit_entry,
    encode_audit_entry,
    live_link_not_found,
    merge_update,
    prepare_insert,
    resolve_link_type,
    retire_object_refusal,
    unknown_page_token,
)
from ontary.store.migration import SqliteSchemaMigrator
from ontary.store.values import (
    DEFAULT_BATCH,
    DEFAULT_TENANT,
    Lineage,
    PagedRow,
    Source,
    StoredObject,
    _utcnow_iso,
)


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    error_code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(error_code, int):
        primary_code = error_code & 0xFF
        if primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return True
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


class ObjectStore(SqliteSchemaMigrator):
    """SQLite-backed store for canonical objects, links, and audit log.

    Postgres-shaped SQL (standard types, no SQLite-only quirks) so the schema
    can be lifted to Postgres later with minimal changes.

    Every file is stamped with `SCHEMA_VERSION` and there is no migration
    ladder: a file written by a different ontary schema shape is refused with
    `STORE_VERSION_UNSUPPORTED` rather than upgraded in place. Moving a store
    between schema versions is an explicit operator step -- open it with the
    matching ontary version, or migrate the data into a fresh file.
    """

    def __init__(
        self,
        registry: OntologyRegistry,
        path: str = ":memory:",
        *,
        tenant: str = DEFAULT_TENANT,
        busy_timeout: float = 5.0,
    ) -> None:
        """`tenant` scopes EVERY read and write this store performs (M8b). Two
        stores on the same file with different tenants cannot see each other's
        objects, links, or audit entries. It is a constructor
        argument rather than a per-call one on purpose: a tenant passed per call
        is a tenant somebody eventually forgets to pass, and the failure mode of
        that mistake is a cross-tenant read.

        `busy_timeout` is the number of seconds SQLite waits for a locked table
        before refusing the operation. The default matches `sqlite3.connect`.
        """
        if not tenant:
            raise ValueError("tenant must be a non-empty string")
        self._registry = registry
        self._tenant = tenant
        self._conn = sqlite3.connect(path, timeout=busy_timeout)
        self._conn.row_factory = sqlite3.Row
        self._txn_depth = 0
        self._write_capture = WriteCapture()
        self._init_schema()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialized, atomic read/write transaction; reentrant.

        The outermost call issues ``BEGIN IMMEDIATE`` before yielding, acquiring
        SQLite's reserved write lock even when the first operation is a read.
        A read followed by a write in this block is therefore serialized against
        concurrent writers on the same database. Nested calls share that
        transaction; only the outermost level commits or rolls back.

        Rolls back on **`BaseException`**, not merely `Exception` (re-review
        finding, 2026-07-26). A handler interrupted by `KeyboardInterrupt`, or
        raising `SystemExit`/`GeneratorExit` mid-transaction, previously left the
        transaction un-rolled-back, so writes made before the interrupt could be
        read by a later caller on this connection or swept into an outer commit.
        Pre-existing, but the atomicity claim -- an action's audit entry and its
        ontology writes are ONE fact -- rests directly on this, so it is fixed
        here rather than left as a footnote. The exception is always re-raised:
        process-control exceptions are cleaned up after, never swallowed.
        """
        is_outermost = self._txn_depth == 0
        if is_outermost:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if not _is_busy_error(exc):
                    raise
                raise ConflictError(
                    "SQLite store remained locked beyond its configured busy timeout",
                    code="STORE_BUSY",
                ) from exc
        self._txn_depth += 1
        try:
            yield self._conn
        except BaseException:
            if is_outermost:
                self._conn.rollback()
            raise
        else:
            if is_outermost:
                # A FAILING commit must also roll back (third-pass review finding,
                # 2026-07-26). Without this, `commit()` raising -- SQLITE_BUSY is
                # the realistic case -- left the transaction OPEN while
                # `_txn_depth` returned to 0. The caller's error handler then
                # called `append_audit`, which opens a fresh transaction, and its
                # commit committed the abandoned work too. Reproduced: an action
                # whose commit failed had its object write become DURABLE, with
                # audit rows ["ok", "error"], while the caller received the
                # commit error. Same silent transactional-integrity class as the
                # BaseException rollback gap, one branch over.
                try:
                    self._conn.commit()
                except BaseException as exc:
                    self._conn.rollback()
                    if isinstance(exc, sqlite3.OperationalError) and _is_busy_error(exc):
                        raise ConflictError(
                            "SQLite store remained locked beyond its configured busy timeout",
                            code="STORE_BUSY",
                        ) from exc
                    raise
        finally:
            self._txn_depth -= 1

    # -- authority / write capture (declared-contracts §3 AC2) -----------

    @property
    def in_transaction(self) -> bool:
        """True while a `transaction()` block (at any nesting depth) is
        open on this store."""
        return self._txn_depth > 0

    @contextmanager
    def capture_action_writes(self) -> Iterator[list[WriteRecord]]:
        """Context manager that, while active, enforces authority
        declarations on action writes and records each allowed create,
        update, retire, link, or unlink as a `WriteRecord` in the yielded
        list.

        Outside a capture context the store enforces nothing -- writes from
        trusted loader/fixture code are unrestricted (declared, not
        defended; see module docstring). Nesting is a programming error
        (the executor owns one capture context per action call).
        """
        with self._write_capture.capture() as records:
            yield records

    def _record_write(self, record: WriteRecord) -> None:
        self._write_capture.record(record)

    # -- objects ---------------------------------------------------------

    def insert(self, obj_type: str, payload: dict[str, Any], source: Source) -> str:
        prepared = prepare_insert(
            self._registry, obj_type, payload, capturing=self._write_capture.active
        )
        payload, obj_id = prepared.payload, prepared.obj_id_raw

        now = _utcnow_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO objects
                    (object_type, id, payload, valid_from, valid_to,
                     source_system, source_id, extracted_at, page_token,
                     tenant)
                VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    obj_type,
                    obj_id,
                    json.dumps(payload),
                    now,
                    source.source_system,
                    source.source_id,
                    source.extracted_at,
                    uuid.uuid4().hex,
                    self._tenant,
                ),
            )
        self._record_write(
            WriteRecord(op="create", object_type=obj_type, object_id=str(obj_id))
        )
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
        now = _utcnow_iso()

        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE objects
                SET valid_to = ?
                WHERE object_type = ? AND id = ? AND valid_to IS NULL
                  AND tenant = ?
                """,
                (now, obj_type, obj_id, self._tenant),
            )
            conn.execute(
                """
                INSERT INTO objects
                    (object_type, id, payload, valid_from, valid_to,
                     source_system, source_id, extracted_at, page_token,
                     tenant)
                VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    obj_type,
                    obj_id,
                    json.dumps(merged),
                    now,
                    source.source_system,
                    source.source_id,
                    source.extracted_at,
                    uuid.uuid4().hex,
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
            current = conn.execute(
                """
                SELECT * FROM objects
                WHERE object_type = ? AND id = ? AND valid_to IS NULL
                  AND tenant = ?
                ORDER BY row_id DESC
                LIMIT 1
                """,
                (object_type, obj_id, self._tenant),
            ).fetchone()
            if current is None:
                has_history = (
                    conn.execute(
                        """
                        SELECT 1 FROM objects
                        WHERE object_type = ? AND id = ? AND tenant = ?
                        LIMIT 1
                        """,
                        (object_type, obj_id, self._tenant),
                    ).fetchone()
                    is not None
                )
                raise retire_object_refusal(
                    object_type, obj_id, has_history=has_history
                )
            conn.execute(
                """
                UPDATE objects SET valid_to = ?
                WHERE object_type = ? AND id = ? AND valid_to IS NULL
                  AND tenant = ?
                """,
                (now, object_type, obj_id, self._tenant),
            )
            closed = conn.execute(
                "SELECT * FROM objects WHERE row_id = ? AND tenant = ?",
                (current["row_id"], self._tenant),
            ).fetchone()
            assert closed is not None
            retired = self._row_to_stored(closed)
        self._record_write(
            WriteRecord(op="retire", object_type=object_type, object_id=obj_id)
        )
        return retired

    def _row_to_stored(self, row: sqlite3.Row) -> StoredObject:
        payload: dict[str, Any] = json.loads(row["payload"])
        return StoredObject(
            payload=payload,
            lineage=Lineage(
                object_type=row["object_type"],
                object_id=str(row["id"]),
                valid_from=row["valid_from"],
                valid_to=row["valid_to"],
                source_system=row["source_system"],
                source_id=row["source_id"],
                extracted_at=row["extracted_at"],
            ),
        )

    def read_current(self, obj_type: str, obj_id: str) -> StoredObject | None:
        """Public but *trusted-caller-only* raw read of a single object's
        current row (AC10 finding 5's sanctioned seam).

        This is NOT the guarded consumer read path -- it returns the full,
        unredacted payload with no scope/sensitivity/min-N checks applied.
        It exists so registered action-handler bodies (which already run
        inside `ActionExecutor.execute`'s audited, permission/scope-checked,
        transactional pipeline -- see `actions.py`), the engine's own
        `GuardedQuery`/`scope`/`ingest` internals, and other trusted
        authoring/loader code have a public method to call. It must never
        be handed to, or called on behalf of, a CONSUMER (human/AI client,
        MCP tool, Function body): `GuardedQuery` remains the only read path
        a consumer may use (AC7) -- that invariant is about there being no
        *ungated* read path a consumer can reach, not about `ObjectStore`
        having zero public methods.
        """
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_CURRENT_SELECT_TEMPLATE, "sqlite"),
            (obj_type, obj_id, self._tenant),
            dialect="sqlite",
        ).rows
        row = rows[0] if rows else None
        return None if row is None else self._row_to_stored(row)

    def read_last(self, obj_type: str, obj_id: str) -> StoredObject | None:
        """Raw (unredacted, unscoped) read of one object's newest row, live
        or retired -- the trusted-caller seam `read_current` cannot serve
        because it filters on `valid_to IS NULL` (see the protocol)."""
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_LAST_SELECT_TEMPLATE, "sqlite"),
            (obj_type, obj_id, self._tenant),
            dialect="sqlite",
        ).rows
        row = rows[0] if rows else None
        return None if row is None else self._row_to_stored(row)

    def read_all(self, obj_type: str) -> list[StoredObject]:
        """Raw (unredacted, unscoped) read of every current row of
        `obj_type`, ordered by the same per-row identity as `read_page`
        (spec AC2). Its OWN single query/pass -- NOT a `read_page` loop:
        looping would re-scan and re-sort per batch (measured 6-18x slower,
        superlinear, at 20k-40k rows), and a batch-boundary loop is exactly
        what silently dropped tied rows under the old payload-pk-keyed
        cursor design this task replaces."""
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_ALL_SELECT_TEMPLATE, "sqlite"),
            (obj_type, self._tenant),
            dialect="sqlite",
        ).rows
        return [self._row_to_stored(row) for row in rows]

    def _resolve_page_token(self, obj_type: str, token: str) -> int:
        """Resolve an untrusted `after_key` PAGE TOKEN to the `row_id` it
        was issued for, via `idx_objects_page_token`'s unique index -- one
        indexed lookup, exactly like every other point-lookup path in this
        module. The lookup is additionally constrained to `object_type =
        obj_type`: `page_token` is only ever unique *globally* (uuid4 hex),
        not scoped to a type, so a token issued for one object type
        resolves to a real `row_id` that happens to belong to a DIFFERENT
        type's row. Without the `object_type` guard that `row_id` is still
        accepted as a valid resume point into the CALLER's requested type
        -- silently reinterpreted as an arbitrary positional offset into an
        unrelated type's ordering (a fail-open, not an error) -- rather
        than refused. Raises a validation-kind `INVALID_CURSOR` failure if
        `token` was never issued
        for `obj_type` specifically (never a bare `None`/empty result a
        caller could mistake for "start of table" -- see the page-token
        docstring)."""
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_PAGE_TOKEN_SELECT_TEMPLATE, "sqlite"),
            (token, obj_type, self._tenant),
            dialect="sqlite",
        ).rows
        row = rows[0] if rows else None
        if row is None:
            raise unknown_page_token(obj_type, token)
        return int(row["row_id"])

    def read_page(
        self, obj_type: str, after_key: str | None = None, batch: int = DEFAULT_BATCH
    ) -> list[PagedRow]:
        """See `Store.read_page`'s docstring for the full contract.

        Orders by `row_id` -- `objects`' own `INTEGER PRIMARY KEY`, i.e.
        SQLite's physical ROWID: a real, already-indexed column (see
        `idx_objects_type_rowid`), so no `json_extract`, no `CAST`, and no
        registry lookup is needed (unlike a payload-pk-keyed cursor, which
        also can't express the ties the store allows among current rows --
        spec pagination-hardening §5's amended decision). `row_id` itself
        never leaves this method except resolved-into internally: the
        CURSOR a caller actually receives, `PagedRow.key`, is a random
        per-row page token instead (spec §5's T2 amendment -- see
        `PagedRow`'s docstring for why), resolved back to `row_id` via
        `_resolve_page_token` when it comes back as `after_key`."""
        check_read_page_batch(batch)
        after = (
            None
            if after_key is None
            else self._resolve_page_token(obj_type, after_key)
        )

        if after is None:
            cur = self._conn.execute(
                """
                SELECT * FROM objects
                WHERE object_type = ? AND valid_to IS NULL AND tenant = ?
                ORDER BY row_id ASC
                LIMIT ?
                """,
                (obj_type, self._tenant, batch),
            )
        else:
            cur = self._conn.execute(
                """
                SELECT * FROM objects
                WHERE object_type = ? AND valid_to IS NULL AND row_id > ?
                  AND tenant = ?
                ORDER BY row_id ASC
                LIMIT ?
                """,
                (obj_type, after, self._tenant, batch),
            )
        rows = cur.fetchall()
        return [
            PagedRow(key=row["page_token"], obj=self._row_to_stored(row)) for row in rows
        ]

    # -- links -------------------------------------------------------------

    def create_link(self, link_type: str, from_id: str, to_id: str) -> None:
        link_def = check_link_write_authority(
            self._registry, link_type, capturing=self._write_capture.active
        )

        if link_def.cardinality in (Cardinality.ONE_TO_ONE, Cardinality.MANY_TO_ONE):
            existing_from = self.links_from(link_type, from_id)
            if existing_from:
                raise ConflictError(
                    f"{link_type}: from_id {from_id!r} already has an active "
                    f"link ({link_def.cardinality.value} forbids a second)",
                    code="CARDINALITY_VIOLATION",
                )

        if link_def.cardinality in (Cardinality.ONE_TO_ONE, Cardinality.ONE_TO_MANY):
            existing_to = self.links_to(link_type, to_id)
            if existing_to:
                raise ConflictError(
                    f"{link_type}: to_id {to_id!r} already has an active "
                    f"link ({link_def.cardinality.value} forbids a second)",
                    code="CARDINALITY_VIOLATION",
                )

        now = _utcnow_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO links
                    (link_type, from_id, to_id, valid_from, valid_to, tenant)
                VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (link_type, from_id, to_id, now, self._tenant),
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
            cursor = conn.execute(
                """
                UPDATE links SET valid_to = ?
                WHERE link_type = ? AND from_id = ? AND to_id = ?
                  AND valid_to IS NULL AND tenant = ?
                """,
                (_utcnow_iso(), link_type, from_id, to_id, self._tenant),
            )
            if cursor.rowcount == 0:
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
            _sql.render(_sql.LINKS_FROM_SELECT_TEMPLATE, "sqlite"),
            (link_type, from_id, self._tenant),
            dialect="sqlite",
        ).rows
        return [str(row["to_id"]) for row in rows]

    def links_to(self, link_type: str, to_id: str) -> list[str]:
        resolve_link_type(self._registry, link_type)
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_TO_SELECT_TEMPLATE, "sqlite"),
            (link_type, to_id, self._tenant),
            dialect="sqlite",
        ).rows
        return [str(row["from_id"]) for row in rows]

    def links_from_asof(
        self, link_type: str, from_id: str, asof: str
    ) -> list[str]:
        resolve_link_type(self._registry, link_type)
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_FROM_ASOF_SELECT_TEMPLATE, "sqlite"),
            (link_type, from_id, asof, asof, self._tenant),
            dialect="sqlite",
        ).rows
        return [str(row["to_id"]) for row in rows]

    def links_to_asof(self, link_type: str, to_id: str, asof: str) -> list[str]:
        resolve_link_type(self._registry, link_type)
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_TO_ASOF_SELECT_TEMPLATE, "sqlite"),
            (link_type, to_id, asof, asof, self._tenant),
            dialect="sqlite",
        ).rows
        return [str(row["from_id"]) for row in rows]

    # -- audit ---------------------------------------------------------

    def append_audit(self, entry: AuditEntry) -> None:
        """Persist one audit entry. Never raises (declared-contracts §3
        AC12): params/writes that fail JSON encoding are recorded as
        placeholders via `_safe_json_dumps` rather than lost or crashing
        the audit write itself."""
        fields = encode_audit_entry(entry)
        with self.transaction() as conn:
            _sql.execute(
                conn,
                _sql.render(
                    _sql.AUDIT_LOG_INSERT_TEMPLATE,
                    "sqlite",
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
                ),
                dialect="sqlite",
            )

    def audit_entries(self) -> list[AuditEntry]:
        cur = self._conn.execute(
            "SELECT * FROM audit_log WHERE tenant = ? ORDER BY seq ASC",
            (self._tenant,),
        )
        return [decode_audit_entry(row) for row in cur.fetchall()]
