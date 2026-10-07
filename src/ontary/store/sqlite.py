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
from ontary.store.migration import SqliteSchemaGate
from ontary.store.values import (
    DEFAULT_BATCH,
    DEFAULT_TENANT,
    Lineage,
    PagedRow,
    Source,
    StoredObject,
)


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    error_code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(error_code, int):
        primary_code = error_code & 0xFF
        if primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return True
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


class ObjectStore(SqliteSchemaGate, StoreCore):
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
        """`tenant` scopes EVERY read and write this store performs. Two
        stores on the same file with different tenants cannot see each other's
        objects, links, or audit entries. It is a constructor
        argument rather than a per-call one on purpose: a tenant passed per call
        is a tenant somebody eventually forgets to pass, and the failure mode of
        that mistake is a cross-tenant read.

        `busy_timeout` is the number of seconds SQLite waits for a locked table
        before refusing the operation. The default matches `sqlite3.connect`.
        """
        super().__init__(registry, tenant=tenant)
        self._conn = sqlite3.connect(path, timeout=busy_timeout)
        self._conn.row_factory = sqlite3.Row
        self._txn_depth = 0
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

    @property
    def in_transaction(self) -> bool:
        """True while a `transaction()` block (at any nesting depth) is
        open on this store."""
        return self._txn_depth > 0

    # -- storage steps: objects --------------------------------------------

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
            _sql.render(_sql.OBJECT_INSERT_TEMPLATE, "sqlite"),
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
            dialect="sqlite",
        )

    def _live_object_rows(self, obj_type: str, obj_id: str) -> list[StoredObject]:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_LIVE_ROWS_SELECT_TEMPLATE, "sqlite"),
            (obj_type, obj_id, self._tenant),
            dialect="sqlite",
        ).rows
        return [self._row_to_stored(row) for row in rows]

    def _has_object_history(self, obj_type: str, obj_id: str) -> bool:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_HISTORY_PROBE_TEMPLATE, "sqlite"),
            (obj_type, obj_id, self._tenant),
            dialect="sqlite",
        ).rows
        return bool(rows)

    def _close_object_rows(self, obj_type: str, obj_id: str, valid_to: str) -> None:
        _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_CLOSE_LIVE_TEMPLATE, "sqlite"),
            (valid_to, obj_type, obj_id, self._tenant),
            dialect="sqlite",
        )

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
        current row.

        This is NOT the guarded consumer read path -- it returns the full,
        unredacted payload with no scope/sensitivity/min-N checks applied.
        It exists so registered action-handler bodies (which already run
        inside `ActionExecutor.execute`'s audited, permission/scope-checked,
        transactional pipeline -- see `actions.py`), the engine's own
        `GuardedQuery`/`scope`/`ingest` internals, and other trusted
        authoring/loader code have a public method to call. It must never
        be handed to, or called on behalf of, a CONSUMER (human/AI client,
        MCP tool, Function body): `GuardedQuery` remains the only read path
        a consumer may use -- that invariant is about there being no
        *ungated* read path a consumer can reach, not about `ObjectStore`
        having zero public methods.
        """
        obj_id = canonical_id(obj_id)
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
        obj_id = canonical_id(obj_id)
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
        `obj_type`, ordered by the same per-row identity as `read_page`. Its
        OWN single query/pass -- NOT a `read_page` loop: looping would
        re-scan and re-sort per batch (measured 6-18x slower, superlinear,
        at 20k-40k rows), and a batch-boundary loop is exactly what silently
        dropped tied rows under the old payload-pk-keyed cursor design this
        replaces."""
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
        also can't express the ties the store allows among current rows).
        `row_id` itself never leaves this method except resolved-into
        internally: the CURSOR a caller actually receives, `PagedRow.key`,
        is a random per-row page token instead (see `PagedRow`'s docstring
        for why), resolved back to `row_id` via `_resolve_page_token` when
        it comes back as `after_key`."""
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

    # -- storage steps: links ----------------------------------------------

    def _insert_link_row(
        self, link_type: str, from_id: str, to_id: str, valid_from: str
    ) -> None:
        _sql.execute(
            self._conn,
            _sql.render(_sql.LINK_INSERT_TEMPLATE, "sqlite"),
            (link_type, from_id, to_id, valid_from, self._tenant),
            dialect="sqlite",
        )

    def _live_link_valid_froms(
        self, link_type: str, from_id: str, to_id: str
    ) -> list[str]:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINK_LIVE_VALID_FROM_SELECT_TEMPLATE, "sqlite"),
            (link_type, from_id, to_id, self._tenant),
            dialect="sqlite",
        ).rows
        return [str(row["valid_from"]) for row in rows]

    def _close_link_rows(
        self, link_type: str, from_id: str, to_id: str, valid_to: str
    ) -> None:
        _sql.execute(
            self._conn,
            _sql.render(_sql.LINK_CLOSE_LIVE_TEMPLATE, "sqlite"),
            (valid_to, link_type, from_id, to_id, self._tenant),
            dialect="sqlite",
        )

    # -- reads: links ------------------------------------------------------

    def links_from(self, link_type: str, from_id: str) -> list[str]:
        from_id = canonical_id(from_id)
        resolve_link_type(self._registry, link_type)
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_FROM_SELECT_TEMPLATE, "sqlite"),
            (link_type, from_id, self._tenant),
            dialect="sqlite",
        ).rows
        return [str(row["to_id"]) for row in rows]

    def links_to(self, link_type: str, to_id: str) -> list[str]:
        to_id = canonical_id(to_id)
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
        from_id = canonical_id(from_id)
        resolve_link_type(self._registry, link_type)
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_FROM_ASOF_SELECT_TEMPLATE, "sqlite"),
            (link_type, from_id, asof, asof, self._tenant),
            dialect="sqlite",
        ).rows
        return [str(row["to_id"]) for row in rows]

    def links_to_asof(self, link_type: str, to_id: str, asof: str) -> list[str]:
        to_id = canonical_id(to_id)
        resolve_link_type(self._registry, link_type)
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_TO_ASOF_SELECT_TEMPLATE, "sqlite"),
            (link_type, to_id, asof, asof, self._tenant),
            dialect="sqlite",
        ).rows
        return [str(row["from_id"]) for row in rows]

    # -- audit ---------------------------------------------------------

    def _append_audit_row(self, fields: AuditRowFields) -> None:
        _sql.execute(
            self._conn,
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
                fields.unscoped_params,
                fields.error_code,
                fields.events,
            ),
            dialect="sqlite",
        )

    def audit_entries(self) -> list[AuditEntry]:
        cur = self._conn.execute(
            "SELECT * FROM audit_log WHERE tenant = ? ORDER BY seq ASC",
            (self._tenant,),
        )
        return [decode_audit_entry(row) for row in cur.fetchall()]
