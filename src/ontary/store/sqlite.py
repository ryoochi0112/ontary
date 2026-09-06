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
from ontary.store.migration import SqliteSchemaMigrator
from ontary.store.protocol import EraseResult, check_ontology_fingerprint
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

    File upgrades require a quiesced rollout. A v1 process whose connection
    was already open can finish old-shape inserts after a v2 migration because
    the new audit columns have constant defaults, but an old v1 binary opening
    the file after its v2 stamp will reject it. No v2 implementation can alter
    an already-deployed v1 reader's version gate.
    """

    def __init__(
        self,
        registry: OntologyRegistry,
        path: str = ":memory:",
        *,
        accept_ontology_drift: bool = False,
        tenant: str = DEFAULT_TENANT,
        busy_timeout: float = 5.0,
    ) -> None:
        """`accept_ontology_drift=True` opens a store whose recorded ontology
        fingerprint does not match `registry`, re-stamps it, and appends an
        audit entry recording the change (spec AC5).

        A call-site argument on purpose -- not an environment variable, not a
        config key. Someone taking this risk names it where the store is opened,
        and the audit log records that they did. It is also the honest escape
        hatch for a legitimate case: an additive change to a type whose existing
        rows genuinely satisfy the new shape needs no migration, only an
        acknowledgement.

        `tenant` scopes EVERY read and write this store performs (M8b). Two
        stores on the same file with different tenants cannot see each other's
        objects, links, audit entries, or outbox rows. It is a constructor
        argument rather than a per-call one on purpose: a tenant passed per call
        is a tenant somebody eventually forgets to pass, and the failure mode of
        that mistake is a cross-tenant read.

        Note what this is NOT: the ontology fingerprint stays per STORE, not per
        tenant, because one process serves one declaration. Per-tenant migration
        timing would need its own design.

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
        check_ontology_fingerprint(self, registry, accept_drift=accept_ontology_drift)

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
        Pre-existing, but M5's atomicity claim -- the pending effect record and
        the ontology writes are ONE fact -- rests directly on this, so it is fixed
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
                # whose commit failed had its object write and its `pending`
                # effect record become DURABLE, with audit rows ["ok", "error"],
                # while the caller received the commit error and no dispatcher
                # ever ran. Same silent transactional-integrity class as the
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
        obj_def, payload, obj_id = prepared.obj_def, prepared.payload, prepared.obj_id_raw

        now = _utcnow_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO objects
                    (object_type, id, payload, valid_from, valid_to,
                     source_system, source_id, extracted_at, page_token,
                     type_version, tenant)
                VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
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
                    obj_def.version,
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
                     type_version, tenant)
                VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
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
                    # A write always lands at the CURRENT declared version --
                    # `update` merges a current-shape change over an
                    # already-upcast payload (see `_row_to_stored`), so the
                    # result is current by construction.
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

    def erase_object_content(self, object_type: str, obj_id: str) -> EraseResult:
        """Tombstone one object's content across all durable content stores."""
        self._registry.get_object_type(object_type)
        object_rows_purged = 0
        audit_entries_purged = 0
        outbox_rows_purged = 0

        def object_row_exists(row_type: str, row_id: str) -> bool:
            return (
                self._conn.execute(
                    """
                    SELECT 1 FROM objects
                    WHERE object_type = ? AND id = ? AND tenant = ?
                    LIMIT 1
                    """,
                    (row_type, row_id, self._tenant),
                ).fetchone()
                is not None
            )

        def references_erased_object(write: dict[str, Any]) -> bool:
            return write_references_object(
                write, object_type, obj_id, self._registry, object_row_exists
            )

        with self.transaction() as conn:
            live = conn.execute(
                """
                SELECT 1 FROM objects
                WHERE object_type = ? AND id = ? AND valid_to IS NULL
                  AND tenant = ?
                LIMIT 1
                """,
                (object_type, obj_id, self._tenant),
            ).fetchone()
            if live is not None:
                # Reuse the public close operation so its timestamp/refusal
                # semantics cannot drift from erasure's close-if-live step.
                self.retire_object(object_type, obj_id)

            object_rows = conn.execute(
                """
                SELECT row_id, payload FROM objects
                WHERE object_type = ? AND id = ? AND tenant = ?
                """,
                (object_type, obj_id, self._tenant),
            ).fetchall()
            for row in object_rows:
                if json.loads(row["payload"]):
                    conn.execute(
                        "UPDATE objects SET payload = ? WHERE row_id = ? AND tenant = ?",
                        ("{}", row["row_id"], self._tenant),
                    )
                    object_rows_purged += 1

            matching_invocations: set[str] = set()
            audit_rows = conn.execute(
                """
                SELECT seq, kind, target_type, target_id, params, effects,
                       writes, invocation_id
                FROM audit_log WHERE tenant = ? ORDER BY seq ASC
                """,
                (self._tenant,),
            ).fetchall()
            for row in audit_rows:
                if row["kind"] == "erasure":
                    continue
                params = json.loads(row["params"])
                effects = json.loads(row["effects"])
                writes = json.loads(row["writes"])
                references_object = (
                    row["target_type"] == object_type and row["target_id"] == obj_id
                ) or json_references_object(
                    params, object_type, obj_id
                ) or json_references_object(
                    effects, object_type, obj_id
                ) or any(
                    references_erased_object(write) for write in writes
                )
                if not references_object:
                    continue
                if row["invocation_id"] is not None:
                    matching_invocations.add(str(row["invocation_id"]))
                effects_have_content = any(
                    effect["payload"] or effect.get("error") is not None
                    for effect in effects
                )
                if params or effects_have_content:
                    scrubbed_effects = [
                        {**effect, "payload": {}, "error": None}
                        for effect in effects
                    ]
                    conn.execute(
                        """
                        UPDATE audit_log SET params = ?, effects = ?
                        WHERE seq = ? AND tenant = ?
                        """,
                        (
                            "{}",
                            json.dumps(scrubbed_effects),
                            row["seq"],
                            self._tenant,
                        ),
                    )
                    audit_entries_purged += 1

            outbox_rows = conn.execute(
                """
                SELECT effect_id, invocation_id, payload, last_error
                FROM effect_outbox WHERE tenant = ?
                ORDER BY emitted_at ASC, seq ASC
                """,
                (self._tenant,),
            ).fetchall()
            for row in outbox_rows:
                payload = json.loads(row["payload"])
                references_object = (
                    row["invocation_id"] in matching_invocations
                    or json_references_object(payload, object_type, obj_id)
                )
                if references_object and (
                    payload or row["last_error"] is not None
                ):
                    conn.execute(
                        """
                        UPDATE effect_outbox SET payload = ?, last_error = NULL
                        WHERE effect_id = ? AND tenant = ?
                        """,
                        ("{}", row["effect_id"], self._tenant),
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
        rows = self._conn.execute(
            """
            SELECT valid_to, payload FROM objects
            WHERE object_type = ? AND id = ? AND tenant = ?
            """,
            (object_type, obj_id, self._tenant),
        ).fetchall()
        has_content = any(
            row["valid_to"] is None or bool(json.loads(row["payload"]))
            for row in rows
        )
        return bool(rows), has_content

    def _row_to_stored(self, row: sqlite3.Row) -> StoredObject:
        payload: dict[str, Any] = json.loads(row["payload"])
        # THE read boundary (M9b). Every path above this -- typed client, string
        # and MCP surfaces, guarded query, aggregates, scope resolution -- goes
        # through `_row_to_stored`, so upcasting here is what makes "one row, one
        # reality" true for all of them at once.
        payload = upcast_payload(
            self._registry,
            row["object_type"],
            payload,
            row["type_version"],
            object_id=str(row["id"]),
        )
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
                    fields.effects,
                    fields.capability_accesses,
                    fields.invocation_id,
                    fields.kind,
                    self._tenant,
                    fields.principal,
                ),
                dialect="sqlite",
            )

    # -- effect outbox ---------------------------------------------------

    @staticmethod
    def _row_to_outbox(row: sqlite3.Row) -> OutboxRecord:
        lease_until = row["lease_until"]
        return OutboxRecord(
            effect_id=row["effect_id"],
            invocation_id=row["invocation_id"],
            seq=row["seq"],
            api_name=row["api_name"],
            payload=json.loads(row["payload"]),
            action=row["action"],
            actor_id=row["actor_id"],
            role=row["role"],
            emitted_at=datetime.fromisoformat(row["emitted_at"]),
            state=row["state"],
            attempts=row["attempts"],
            next_attempt_at=datetime.fromisoformat(row["next_attempt_at"]),
            lease_until=(
                datetime.fromisoformat(lease_until) if lease_until is not None else None
            ),
            last_error=row["last_error"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def enqueue_effects(self, records: Sequence[OutboxRecord]) -> None:
        """Insert outbox rows inside the caller's transaction (spec AC1).

        Deliberately NOT `_safe_json_dumps` like `append_audit`: an audit entry
        must persist even when a param cannot be encoded, because a
        half-described record of something that happened beats no record. An
        outbox row is the opposite -- it is the instruction for something that
        has NOT happened yet, and a payload placeholder would be dispatched
        outward as though it were the author's data. A payload that cannot be
        JSON-encoded raises here, inside the action transaction, so the whole
        action rolls back and nothing is sent."""
        with self.transaction() as conn:
            for record in records:
                _sql.execute(
                    conn,
                    _sql.render(
                        _sql.EFFECT_OUTBOX_INSERT_TEMPLATE,
                        "sqlite",
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
                    dialect="sqlite",
                )

    def claim_due_effects(
        self, *, limit: int, now: datetime, lease: timedelta
    ) -> list[OutboxRecord]:
        """Select-then-lease in ONE transaction (spec AC5).

        The `UPDATE` re-states the whole eligibility predicate rather than
        trusting the ids the `SELECT` just produced: on this engine's
        connections nothing else can interleave inside the transaction, but
        repeating the predicate means the claim is still correct if a future
        backend runs it at a weaker isolation level, and it costs one indexed
        comparison. ISO-8601 UTC strings compare lexicographically in the same
        order as the instants they denote, which is what makes
        `next_attempt_at <= ?` a valid SQL comparison against TEXT.
        """
        check_claim_limit(limit)
        now_iso = now.isoformat()
        lease_until = (now + lease).isoformat()
        with self.transaction() as conn:
            rows = _sql.execute(
                conn,
                _sql.render(_sql.EFFECT_OUTBOX_CLAIM_SELECT_TEMPLATE, "sqlite"),
                (now_iso, now_iso, self._tenant, limit),
                dialect="sqlite",
            ).rows
            claimed: list[OutboxRecord] = []
            for row in rows:
                updated = _sql.execute(
                    conn,
                    _sql.render(_sql.EFFECT_OUTBOX_CLAIM_UPDATE_TEMPLATE, "sqlite"),
                    (
                        lease_until,
                        now_iso,
                        row["effect_id"],
                        now_iso,
                        now_iso,
                        self._tenant,
                    ),
                    dialect="sqlite",
                )
                if updated.rowcount == 1:
                    claimed.append(
                        self._row_to_outbox(row).model_copy(
                            update={
                                "lease_until": datetime.fromisoformat(lease_until),
                                "updated_at": now,
                            }
                        )
                    )
        return claimed

    def release_effect_claim(self, effect_id: str, *, now: datetime) -> None:
        with self.transaction() as conn:
            _sql.execute(
                conn,
                _sql.render(_sql.EFFECT_OUTBOX_RELEASE_TEMPLATE, "sqlite"),
                (now.isoformat(), effect_id, self._tenant),
                dialect="sqlite",
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
                _sql.render(_sql.EFFECT_OUTBOX_RESOLVE_TEMPLATE, "sqlite"),
                (
                    state,
                    next_attempt_at.isoformat(),
                    error,
                    now.isoformat(),
                    effect_id,
                    self._tenant,
                ),
                dialect="sqlite",
            )

    def outbox_entries(self) -> list[OutboxRecord]:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.EFFECT_OUTBOX_SELECT_TEMPLATE, "sqlite"),
            (self._tenant,),
            dialect="sqlite",
        ).rows
        return [self._row_to_outbox(row) for row in rows]

    # -- ontology fingerprint --------------------------------------------

    def read_ontology_fingerprint(self) -> OntologyFingerprint | None:
        rows = _sql.execute(
            self._conn,
            _sql.render(_sql.ONTOLOGY_FINGERPRINT_SELECT_TEMPLATE, "sqlite"),
            dialect="sqlite",
        ).rows
        row = rows[0] if rows else None
        if row is None:
            return None
        return OntologyFingerprint(
            digest=row["digest"],
            types=json.loads(row["types"]),
            versions=json.loads(row["versions"]),
        )

    def write_ontology_fingerprint(
        self, fingerprint: OntologyFingerprint, *, adopted: bool
    ) -> None:
        with self.transaction() as conn:
            _sql.execute(
                conn,
                _sql.render(_sql.ONTOLOGY_FINGERPRINT_UPSERT_TEMPLATE, "sqlite"),
                (
                    fingerprint.digest,
                    json.dumps(fingerprint.types, sort_keys=True),
                    _utcnow_iso(),
                    int(adopted),
                    json.dumps(fingerprint.versions, sort_keys=True),
                ),
                dialect="sqlite",
            )
        # `first_seen` is NOT updated by the upsert above: it records when this
        # store first had a fingerprint at all, which stays true across a later
        # re-stamp. An accepted drift changes the shape of record, not the date
        # the store started keeping one.

    def audit_entries(self) -> list[AuditEntry]:
        cur = self._conn.execute(
            "SELECT * FROM audit_log WHERE tenant = ? ORDER BY seq ASC",
            (self._tenant,),
        )
        return [decode_audit_entry(row) for row in cur.fetchall()]
