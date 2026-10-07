"""In-memory `Store` implementation: dict-backed, no SQL.

`InMemoryStore` implements the same `Store` Protocol as
`ontary.store.ObjectStore` and passes the shared conformance suite
(`tests/test_store_conformance.py`) unchanged -- CRUD, links + cardinality,
authority refusals, audit, and transaction rollback all behave identically.
It also doubles as a fast, dependency-free test double for user code.

The write path (order of steps, refusals, cardinality, the clock, and write
capture) is `ontary.store._core.StoreCore`'s; this module supplies the
dict-backed transaction and storage steps it calls, plus its own reads. Pure
contract logic shared with the other backends lives in
`ontary.store._shared`.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Literal, TypedDict

from ontary.audit import AuditEntry
from ontary.meta import OntologyRegistry
from ontary.store._core import StoreCore
from ontary.store._shared import (
    AuditRowFields,
    canonical_id,
    check_read_page_batch,
    decode_audit_entry,
    resolve_link_type,
    unknown_page_token,
)
from ontary.store.protocol import Store
from ontary.store.values import (
    DEFAULT_BATCH,
    DEFAULT_TENANT,
    Lineage,
    PagedRow,
    Source,
    StoredObject,
)


class _ObjectRow(TypedDict):
    tenant: str
    object_type: str
    id: str
    payload: str
    valid_from: str
    valid_to: str | None
    source_system: str
    source_id: str | None
    extracted_at: str | None
    row_id: int
    page_token: str


class _LinkRow(TypedDict):
    tenant: str
    link_type: str
    from_id: str
    to_id: str
    valid_from: str
    valid_to: str | None


def _live_at(row: _LinkRow, asof: str) -> bool:
    """Was this link row live at the instant `asof`?

    The SQL twin of `_sql.LINKS_*_ASOF_SELECT_TEMPLATE`'s time predicate:
    open at `asof`, or closed at or after it. Timestamps are the fixed-width
    ISO-8601 UTC spelling `values.now()` produces, so `<=`/`>=` on the
    strings order them exactly as the SQL comparison does.
    """
    return row["valid_from"] <= asof and (
        row["valid_to"] is None or row["valid_to"] >= asof
    )


def _ordered_link_ids(
    rows: Iterable[_LinkRow], column: Literal["from_id", "to_id"]
) -> list[str]:
    """Mirror the SQL backends' `ORDER BY valid_from, <id>` for link reads.

    `links` carries no monotonic key, so the total order is "earliest
    `valid_from`, then lowest id" on every backend. The SQL templates spell
    it with `COLLATE "C"`, i.e. byte order -- which is what comparing the
    Python `str`s does, since UTF-8 preserves code-point order.
    """
    return [
        row[column]
        for row in sorted(rows, key=lambda row: (row["valid_from"], row[column]))
    ]


class InMemoryStore(StoreCore):
    """Dict-backed store matching `ObjectStore`'s observed semantics.

    Rows are never mutated in place -- closing an object's current row (on
    `update`) replaces its list entry with a new dict rather than assigning
    into the existing one. That makes `transaction()` rollback a cheap
    shallow-copy snapshot/restore of the three top-level lists (objects,
    links, audit) rather than a deep copy.

    **Tenancy here is structural**. Two `InMemoryStore` instances share no
    state, so one instance is one tenant's data by construction and cannot leak
    the way a shared file or database could. The `tenant` argument is still
    honored -- object and link rows carry it and reads filter on it -- so the
    stored shape and the read semantics match the other backends; but the
    cross-tenant isolation TESTS necessarily target the two backends where two
    stores can share a substrate. Audit rows are not filtered here,
    because within one instance every row has the same tenant and a filter would
    be a no-op dressed up as a guard.
    """

    def __init__(
        self, registry: OntologyRegistry, *, tenant: str = DEFAULT_TENANT
    ) -> None:
        """`tenant` scopes every read and write, matching `ObjectStore`.
        Two in-memory stores cannot share rows anyway, so this exists for parity:
        a test double whose tenant argument was ignored would let a caller
        "verify" isolation against a store that cannot leak."""
        super().__init__(registry, tenant=tenant)
        self._objects: list[_ObjectRow] = []
        self._links: list[_LinkRow] = []
        self._audit: list[AuditEntry] = []
        # One in-memory instance is its whole storage substrate. A re-entrant
        # lock serializes outer transactions between threads while still letting
        # store methods join the transaction opened by ActionExecutor. Depth is
        # thread-local: another action waiting for the lock must not look like a
        # caller-opened nested transaction to `in_transaction`.
        self._transaction_lock = threading.RLock()
        self._transaction_state = threading.local()
        # Mirrors `ObjectStore`'s `objects.row_id INTEGER PRIMARY KEY
        # AUTOINCREMENT`: a monotonic, per-row identity distinct from the
        # payload primary key (`Lineage.object_id`), whose uniqueness among
        # current rows is not enforced here -- see `read_page`'s docstring.
        # Consumers see it only in `read_page`'s out-of-band cursor string;
        # `Lineage`/`StoredObject` never expose it; see `Lineage`'s docstring.
        self._next_row_id = 1

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialized, atomic read/write transaction; thread-safe and reentrant.

        A re-entrant lock is held for the entire outermost transaction, so a
        read followed by a write is serialized against concurrent writers on
        this store. Nested calls in the same thread share the outer snapshot;
        only that outer level commits or rolls back.
        """
        with self._transaction_lock:
            depth = getattr(self._transaction_state, "depth", 0)
            self._transaction_state.depth = depth + 1
            is_outermost = depth == 0
            snapshot: (
                tuple[
                    list[_ObjectRow],
                    list[_LinkRow],
                    list[AuditEntry],
                    int,
                ]
                | None
            ) = None
            if is_outermost:
                snapshot = (
                    list(self._objects),
                    list(self._links),
                    list(self._audit),
                    self._next_row_id,
                )
            try:
                yield None
            # `BaseException`, not `Exception` (re-review finding, 2026-07-26): a
            # handler interrupted by KeyboardInterrupt previously left its mutations
            # in place, since this snapshot was only restored for `Exception`. Kept
            # identical to `ObjectStore.transaction` -- the two backends must agree
            # about what a rolled-back action leaves behind. Always re-raised.
            except BaseException:
                if is_outermost and snapshot is not None:
                    (
                        self._objects,
                        self._links,
                        self._audit,
                        self._next_row_id,
                    ) = snapshot
                raise
            finally:
                self._transaction_state.depth -= 1

    @property
    def in_transaction(self) -> bool:
        return getattr(self._transaction_state, "depth", 0) > 0

    def _next_id(self) -> int:
        row_id = self._next_row_id
        self._next_row_id += 1
        return row_id

    # -- object storage steps ------------------------------------------

    def _matches_object(self, row: _ObjectRow, obj_type: str, obj_id: str) -> bool:
        return (
            row["tenant"] == self._tenant
            and row["object_type"] == obj_type
            and row["id"] == obj_id
        )

    def _insert_object_row(
        self,
        obj_type: str,
        obj_id: str,
        encoded_payload: str,
        valid_from: str,
        source: Source,
    ) -> None:
        self._objects.append(
            {
                "tenant": self._tenant,
                "object_type": obj_type,
                "id": obj_id,
                # The payload is held as JSON text (matching `ObjectStore`'s
                # TEXT column), so every read builds fresh, unshared
                # structures.
                "payload": encoded_payload,
                "valid_from": valid_from,
                "valid_to": None,
                "source_system": source.source_system,
                "source_id": source.source_id,
                "extracted_at": source.extracted_at,
                "row_id": self._next_id(),
                "page_token": uuid.uuid4().hex,
            }
        )

    def _live_object_rows(self, obj_type: str, obj_id: str) -> list[StoredObject]:
        # `self._objects` is append-ordered, so list order is `row_id` order.
        return [
            self._row_to_stored(row)
            for row in self._objects
            if self._matches_object(row, obj_type, obj_id) and row["valid_to"] is None
        ]

    def _has_object_history(self, obj_type: str, obj_id: str) -> bool:
        return any(self._matches_object(row, obj_type, obj_id) for row in self._objects)

    def _close_object_rows(self, obj_type: str, obj_id: str, valid_to: str) -> None:
        # Rows are replaced, never mutated in place, so the rollback snapshot
        # (a shallow copy of the list) still holds the open row.
        for index, row in enumerate(self._objects):
            if self._matches_object(row, obj_type, obj_id) and row["valid_to"] is None:
                self._objects[index] = {**row, "valid_to": valid_to}

    # -- object reads ----------------------------------------------------

    def _row_to_stored(self, row: _ObjectRow) -> StoredObject:
        # `json.loads` here (mirroring `ObjectStore._row_to_stored`) is what
        # guarantees a fresh, unshared structure on every read: mutating the
        # returned payload (including nested containers) can never reach
        # back into `self._objects` or leak forward into a later read.
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
        obj_id = canonical_id(obj_id)
        # Every public read below briefly takes `_transaction_lock` (reentrant,
        # so the writing thread's own reads pass through): rows are mutated in
        # place inside a transaction and only snapshot-restored on rollback, so
        # an unlocked cross-thread read could return rows of an action that is
        # about to roll back -- a dirty read neither SQL backend can exhibit.
        with self._transaction_lock:
            for row in self._objects:
                if (
                    row["tenant"] == self._tenant
                    and row["object_type"] == obj_type
                    and row["id"] == obj_id
                    and row["valid_to"] is None
                ):
                    return self._row_to_stored(row)
            return None

    def read_last(self, obj_type: str, obj_id: str) -> StoredObject | None:
        """Raw (unredacted, unscoped) read of one object's newest row, live
        or retired. `self._objects` is append-ordered and `update` appends
        the replacement row, so the LAST match is the newest state.

        A LIVE row wins over a newer closed one, and among live rows this
        returns `read_current`'s own pick (the first in append order). Since
        #34 `insert` refuses a live duplicate, so an id has at most one live
        row; but before that two inserts of one primary key left two live
        rows -- and a plain "last match" then answered with the opposite row
        to `read_current`. The live-first rule stays as defense in depth. That matters because the action target gate
        resolves scope from `read_last` while every consumer read resolves it
        from `read_current`: the two disagreeing let the gate authorize
        against a scope no consumer read can see.
        """
        obj_id = canonical_id(obj_id)
        with self._transaction_lock:
            newest: _ObjectRow | None = None
            for row in self._objects:
                if not (
                    row["tenant"] == self._tenant
                    and row["object_type"] == obj_type
                    and row["id"] == obj_id
                ):
                    continue
                if row["valid_to"] is None:
                    return self._row_to_stored(row)
                newest = row
            return None if newest is None else self._row_to_stored(newest)

    def read_all(self, obj_type: str) -> list[StoredObject]:
        """Raw (unredacted, unscoped) read of every current row of
        `obj_type`, ordered by the same per-row identity as `read_page`. Its
        OWN single pass -- NOT a `read_page` loop: looping would
        re-scan/re-sort per batch, and a batch-boundary loop is exactly what
        silently dropped tied rows under the old payload-pk-keyed cursor
        design this replaces."""
        with self._transaction_lock:
            current = [
                row
                for row in self._objects
                if row["tenant"] == self._tenant
                and row["object_type"] == obj_type
                and row["valid_to"] is None
            ]
            current.sort(key=lambda row: row["row_id"])
            return [self._row_to_stored(row) for row in current]

    def _resolve_page_token(self, obj_type: str, token: str) -> int:
        """Resolve an untrusted `after_key` PAGE TOKEN to the `row_id` it
        was issued for -- a linear scan here rather than a maintained `token
        -> row_id` dict, unlike `ObjectStore`'s real unique SQL index: this
        backend is a dependency-free test double, never the scale-sensitive
        path the index in `ObjectStore` exists to keep O(1) (see
        `idx_objects_page_token`'s comment), and a maintained dict would
        need its own transaction-rollback snapshot logic for no behavioral
        gain. The scan additionally requires `row["object_type"] ==
        obj_type`: `page_token` is only unique *globally*, not scoped to a
        type, so a token issued for one object type is a real token
        belonging to a DIFFERENT type's row -- without this guard it would
        still resolve to that row's `row_id` and be accepted as a valid
        resume point into the CALLER's requested type, silently
        reinterpreted as an arbitrary positional offset into an unrelated
        type's ordering instead of refused. Raises a validation-kind
        `INVALID_CURSOR` failure if `token` was never issued for `obj_type`
        specifically."""
        for row in self._objects:
            if (
                row["page_token"] == token
                and row["object_type"] == obj_type
                and row["tenant"] == self._tenant
            ):
                return row["row_id"]
        raise unknown_page_token(obj_type, token)

    def read_page(
        self, obj_type: str, after_key: str | None = None, batch: int = DEFAULT_BATCH
    ) -> list[PagedRow]:
        """See `Store.read_page`'s docstring for the full contract.

        Orders by `row_id` -- this store's own monotonic per-row counter
        (mirroring `ObjectStore`'s physical SQLite ROWID), not the payload
        primary key (which also can't express the ties the store allows
        among current rows). `row_id` never leaves this method except
        resolved-into internally: the CURSOR a caller actually receives,
        `PagedRow.key`, is a random per-row page token instead (see
        `PagedRow`'s docstring for why), resolved back to `row_id` via
        `_resolve_page_token` when it comes back as `after_key`."""
        check_read_page_batch(batch)
        with self._transaction_lock:
            after = (
                None
                if after_key is None
                else self._resolve_page_token(obj_type, after_key)
            )

            current = [
                row
                for row in self._objects
                if row["tenant"] == self._tenant
                and row["object_type"] == obj_type
                and row["valid_to"] is None
            ]
            current.sort(key=lambda row: row["row_id"])
            if after is not None:
                current = [row for row in current if row["row_id"] > after]
            page = current[:batch]
            return [
                PagedRow(key=row["page_token"], obj=self._row_to_stored(row))
                for row in page
            ]

    # -- links -------------------------------------------------------------

    def _matches_link(
        self, row: _LinkRow, link_type: str, from_id: str, to_id: str
    ) -> bool:
        return (
            row["tenant"] == self._tenant
            and row["link_type"] == link_type
            and row["from_id"] == from_id
            and row["to_id"] == to_id
            and row["valid_to"] is None
        )

    def _insert_link_row(
        self, link_type: str, from_id: str, to_id: str, valid_from: str
    ) -> None:
        self._links.append(
            {
                "tenant": self._tenant,
                "link_type": link_type,
                "from_id": from_id,
                "to_id": to_id,
                "valid_from": valid_from,
                "valid_to": None,
            }
        )

    def _live_link_valid_froms(
        self, link_type: str, from_id: str, to_id: str
    ) -> list[str]:
        return [
            row["valid_from"]
            for row in self._links
            if self._matches_link(row, link_type, from_id, to_id)
        ]

    def _close_link_rows(
        self, link_type: str, from_id: str, to_id: str, valid_to: str
    ) -> None:
        for index, row in enumerate(self._links):
            if self._matches_link(row, link_type, from_id, to_id):
                self._links[index] = {**row, "valid_to": valid_to}

    def links_from(self, link_type: str, from_id: str) -> list[str]:
        from_id = canonical_id(from_id)
        resolve_link_type(self._registry, link_type)
        with self._transaction_lock:
            return _ordered_link_ids(
                (
                    row
                    for row in self._links
                    if row["tenant"] == self._tenant
                    and row["link_type"] == link_type
                    and row["from_id"] == from_id
                    and row["valid_to"] is None
                ),
                "to_id",
            )

    def links_to(self, link_type: str, to_id: str) -> list[str]:
        to_id = canonical_id(to_id)
        resolve_link_type(self._registry, link_type)
        with self._transaction_lock:
            return _ordered_link_ids(
                (
                    row
                    for row in self._links
                    if row["tenant"] == self._tenant
                    and row["link_type"] == link_type
                    and row["to_id"] == to_id
                    and row["valid_to"] is None
                ),
                "from_id",
            )

    def links_from_asof(
        self, link_type: str, from_id: str, asof: str
    ) -> list[str]:
        from_id = canonical_id(from_id)
        resolve_link_type(self._registry, link_type)
        with self._transaction_lock:
            return _ordered_link_ids(
                (
                    row
                    for row in self._links
                    if row["tenant"] == self._tenant
                    and row["link_type"] == link_type
                    and row["from_id"] == from_id
                    and _live_at(row, asof)
                ),
                "to_id",
            )

    def links_to_asof(self, link_type: str, to_id: str, asof: str) -> list[str]:
        to_id = canonical_id(to_id)
        resolve_link_type(self._registry, link_type)
        with self._transaction_lock:
            return _ordered_link_ids(
                (
                    row
                    for row in self._links
                    if row["tenant"] == self._tenant
                    and row["link_type"] == link_type
                    and row["to_id"] == to_id
                    and _live_at(row, asof)
                ),
                "from_id",
            )

    # -- audit ---------------------------------------------------------

    def _append_audit_row(self, fields: AuditRowFields) -> None:
        # Decoding the shared codec's encoded fields gives exactly the entry
        # a SQL backend would read back -- unencodable values already
        # replaced by placeholders -- with no field list kept here to drift.
        self._audit.append(decode_audit_entry(fields._asdict()))

    def audit_entries(self) -> list[AuditEntry]:
        """Deep copies, so a reader cannot rewrite the append-only log.

        Whole-branch review finding (2026-07-26): `list(self._audit)` copied the
        LIST but handed out the same mutable `AuditEntry`/`WriteRecord` objects
        it stores. A caller closing over an in-memory store could therefore
        rewrite a durable, already-committed entry in place -- an append-only
        log that was not append-only.

        `ObjectStore` never had this hole: it reconstructs models from persisted
        JSON on every read, so its callers always get fresh objects. That made
        this a BACKEND-PARITY defect as well -- two `Store` implementations with
        different audit-mutability semantics while claiming to satisfy one
        Protocol. Copying at the seam restores parity without freezing the record
        models, which would ripple through every construction site in the engine.
        """
        with self._transaction_lock:
            return [entry.model_copy(deep=True) for entry in self._audit]


def _static_conformance_check(registry: OntologyRegistry) -> Store:
    """Never called at runtime -- exists purely so mypy (which runs
    `--strict` over `src/`, see `make verify`) fails the build if
    `InMemoryStore` ever drifts from the `Store` Protocol it's declared to
    satisfy. Returning `InMemoryStore(registry)` typed as `Store` forces a
    structural check at type-check time with zero runtime cost and zero
    `# type: ignore`."""
    return InMemoryStore(registry)
