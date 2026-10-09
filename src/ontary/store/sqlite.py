"""SQLite-backed object store: history, lineage, links, audit.

Domain-agnostic storage layer for any ontology declared via
`ontary.meta.OntologyRegistry`. Stores objects with bitemporal-ish history
(valid_from/valid_to, close-old-insert-new), links with cardinality
enforcement, and an append-only audit log.

`ObjectStore` subclasses `ontary.store._core.StoreCore`, which owns the write
and read paths, and supplies the SQLite storage steps (connection,
transaction, SQL).

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
from ontary.store._filter import (
    OBJECT_FILTERED_ALL_SELECT_TEMPLATE,
    OBJECT_FILTERED_PAGE_SELECT_TEMPLATE,
    SQLITE_DOMAIN,
    RowFilter,
    _ontary_instant,
    compile_filter,
    compile_row_filter,
)
from ontary.store._shared import (
    AuditRowFields,
    decode_audit_entry,
)
from ontary.store.migration import SqliteSchemaGate
from ontary.store.values import (
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
        self.bind_domain = SQLITE_DOMAIN
        self._conn = sqlite3.connect(path, timeout=busy_timeout)
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function("ontary_instant", 1, _ontary_instant, deterministic=True)
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

    def _current_row(self, obj_type: str, obj_id: str) -> StoredObject | None:
        """The live row of one object: a raw, unredacted read for trusted
        callers only (see `StoreCore`); consumers read through
        `ontary.query.GuardedQuery`."""
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_CURRENT_SELECT_TEMPLATE, "sqlite"),
            (obj_type, obj_id, self._tenant),
            dialect="sqlite",
        ).rows
        row = rows[0] if rows else None
        return None if row is None else self._row_to_stored(row)

    def _current_rows_many(self, obj_type: str, ids: list[str]) -> dict[str, StoredObject]:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_CURRENT_MANY_SELECT_TEMPLATE, "sqlite"),
            (obj_type, json.dumps(ids, ensure_ascii=False), self._tenant),
            dialect="sqlite",
        ).rows
        result: dict[str, StoredObject] = {}
        # Rows arrive in row_id order, matching the single-row live pick.
        for row in rows:
            obj_id = str(row["id"])
            if obj_id not in result:
                result[obj_id] = self._row_to_stored(row)
        return {obj_id: result[obj_id] for obj_id in ids if obj_id in result}

    def _last_row(self, obj_type: str, obj_id: str) -> StoredObject | None:
        """One object's newest row, live or retired -- what `_current_row`
        cannot serve because it filters on `valid_to IS NULL`."""
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_LAST_SELECT_TEMPLATE, "sqlite"),
            (obj_type, obj_id, self._tenant),
            dialect="sqlite",
        ).rows
        row = rows[0] if rows else None
        return None if row is None else self._row_to_stored(row)

    def _all_rows(self, obj_type: str) -> list[StoredObject]:
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

    def prefilter_exact(self, row_filter: RowFilter) -> bool:
        return compile_row_filter(row_filter, "sqlite", self.bind_domain).exact

    def _filtered_all_rows(
        self, obj_type: str, row_filter: RowFilter
    ) -> list[StoredObject]:
        fragment, params = compile_filter(row_filter, "sqlite", self.bind_domain)

        def read() -> list[StoredObject]:
            template = OBJECT_FILTERED_ALL_SELECT_TEMPLATE.replace("{filter}", fragment)
            rows = _sql.execute(
                self._conn,
                _sql.render(template, "sqlite"),
                [obj_type, self._tenant, *params],
                dialect="sqlite",
            ).rows
            return [self._row_to_stored(row) for row in rows]

        return _sql.prefilter(
            self._conn,
            read,
            lambda: self._all_rows(obj_type),
            dialect="sqlite",
            errors=(sqlite3.Error, ValueError, OverflowError),
            on_fallback=lambda: self._prefilter_fallback(obj_type),
        )

    def _page_token_row_id(self, obj_type: str, token: str) -> int | None:
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
        than refused. Returns `None` if `token` was never issued for
        `obj_type` specifically; the core turns that into the cursor refusal
        (a caller never sees a bare `None` it could mistake for "start of
        table" -- see the page-token docstring)."""
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.OBJECT_PAGE_TOKEN_SELECT_TEMPLATE, "sqlite"),
            (token, obj_type, self._tenant),
            dialect="sqlite",
        ).rows
        row = rows[0] if rows else None
        return None if row is None else int(row["row_id"])

    def _page_rows(
        self, obj_type: str, after_row_id: int | None, batch: int
    ) -> list[PagedRow]:
        """Orders by `row_id` -- `objects`' own `INTEGER PRIMARY KEY`, i.e.
        SQLite's physical ROWID: a real, already-indexed column (see
        `idx_objects_type_rowid`), so no `json_extract`, no `CAST`, and no
        registry lookup is needed (unlike a payload-pk-keyed cursor, which
        also can't express the ties the store allows among current rows).
        `row_id` itself never leaves the store except resolved-into
        internally: the CURSOR a caller actually receives, `PagedRow.key`,
        is a random per-row page token instead (see `PagedRow`'s docstring
        for why), resolved back to `row_id` by `_page_token_row_id` when it
        comes back as `after_key`."""
        if after_row_id is None:
            rows = _sql.execute(
                self._conn,
                _sql.render(_sql.OBJECT_PAGE_FIRST_SELECT_TEMPLATE, "sqlite"),
                (obj_type, self._tenant, batch),
                dialect="sqlite",
            ).rows
        else:
            rows = _sql.execute(
                self._conn,
                _sql.render(_sql.OBJECT_PAGE_AFTER_SELECT_TEMPLATE, "sqlite"),
                (obj_type, after_row_id, self._tenant, batch),
                dialect="sqlite",
            ).rows
        return [PagedRow(key=row["page_token"], obj=self._row_to_stored(row)) for row in rows]

    def _filtered_page_rows(
        self, obj_type: str, row_filter: RowFilter, after_row_id: int | None, batch: int
    ) -> list[PagedRow]:
        fragment, params = compile_filter(row_filter, "sqlite", self.bind_domain)

        def read() -> list[PagedRow]:
            template = OBJECT_FILTERED_PAGE_SELECT_TEMPLATE.replace("{filter}", fragment)
            rows = _sql.execute(
                self._conn,
                _sql.render(template, "sqlite"),
                [
                    obj_type, self._tenant,
                    after_row_id if after_row_id is not None else 0,
                    *params, batch,
                ],
                dialect="sqlite",
            ).rows
            return [PagedRow(key=row["page_token"], obj=self._row_to_stored(row)) for row in rows]

        return _sql.prefilter(
            self._conn,
            read,
            lambda: self._page_rows(obj_type, after_row_id, batch),
            dialect="sqlite",
            errors=(sqlite3.Error, ValueError, OverflowError),
            on_fallback=lambda: self._prefilter_fallback(obj_type),
        )

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

    def _link_ids_from(self, link_type: str, from_id: str) -> list[str]:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_FROM_SELECT_TEMPLATE, "sqlite"),
            (link_type, from_id, self._tenant),
            dialect="sqlite",
        ).rows
        return [str(row["to_id"]) for row in rows]

    def _link_ids_to(self, link_type: str, to_id: str) -> list[str]:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_TO_SELECT_TEMPLATE, "sqlite"),
            (link_type, to_id, self._tenant),
            dialect="sqlite",
        ).rows
        return [str(row["from_id"]) for row in rows]

    def _link_ids_from_many(self, link_type: str, ids: list[str]) -> dict[str, list[str]]:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_FROM_MANY_SELECT_TEMPLATE, "sqlite"),
            (link_type, json.dumps(ids, ensure_ascii=False), self._tenant),
            dialect="sqlite",
        ).rows
        result: dict[str, list[str]] = {obj_id: [] for obj_id in ids}
        for row in rows:
            result[str(row["from_id"])].append(str(row["to_id"]))
        return result

    def _link_ids_to_many(self, link_type: str, ids: list[str]) -> dict[str, list[str]]:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_TO_MANY_SELECT_TEMPLATE, "sqlite"),
            (link_type, json.dumps(ids, ensure_ascii=False), self._tenant),
            dialect="sqlite",
        ).rows
        result: dict[str, list[str]] = {obj_id: [] for obj_id in ids}
        for row in rows:
            result[str(row["to_id"])].append(str(row["from_id"]))
        return result

    def _link_ids_from_asof(
        self, link_type: str, from_id: str, asof: str
    ) -> list[str]:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.LINKS_FROM_ASOF_SELECT_TEMPLATE, "sqlite"),
            (link_type, from_id, asof, asof, self._tenant),
            dialect="sqlite",
        ).rows
        return [str(row["to_id"]) for row in rows]

    def _link_ids_to_asof(self, link_type: str, to_id: str, asof: str) -> list[str]:
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

    def _audit_rows(self) -> list[AuditEntry]:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.AUDIT_LOG_SELECT_TEMPLATE, "sqlite"),
            (self._tenant,),
            dialect="sqlite",
        ).rows
        return [decode_audit_entry(row) for row in rows]
