"""In-memory `Store` implementation: dict-backed, no SQL.

Proves the storage seam (spec m35-sdk-refactor §6 "Storage seam") is real,
not decorative: `InMemoryStore` implements the same `Store` Protocol as
`ontary.store.ObjectStore` and passes the shared conformance suite
(`tests/test_store_conformance.py`) unchanged -- CRUD, links + cardinality,
authority refusals, audit, and transaction rollback all behave identically.
It also doubles as a fast, dependency-free test double for user code.

Deliberately thin: no shared base class with `ObjectStore` (see spec §6
"Alternative rejected") -- a from-scratch implementation is the only way the
conformance suite proves the seam rather than exercising inherited SQLite
helpers by another name. Refinement of that rule: pure CONTRACT logic the
spec requires to be byte-identical across backends (refusal checks,
validation, codecs) is shared via `ontary.store._shared` -- see that
module's doctrine docstring -- while all storage mechanics remain
from-scratch here, so the conformance suite still proves the seam.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, TypedDict

from ontary.audit import (
    AuditEntry,
    WriteRecord,
)
from ontary.errors import ConflictError
from ontary.fingerprint import OntologyFingerprint
from ontary.meta import Cardinality, OntologyRegistry
from ontary.outbox import OutboxRecord, OutboxState
from ontary.store._shared import (
    WriteCapture,
    check_claim_limit,
    check_link_write_authority,
    check_read_page_batch,
    check_update_authority,
    decode_audit_entry,
    encode_audit_entry,
    merge_update,
    prepare_insert,
    resolve_link_type,
    unknown_page_token,
)
from ontary.store.protocol import Store, check_ontology_fingerprint
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
    type_version: int


class _LinkRow(TypedDict):
    tenant: str
    link_type: str
    from_id: str
    to_id: str
    valid_from: str
    valid_to: str | None


class InMemoryStore:
    """Dict-backed store matching `ObjectStore`'s observed semantics.

    Rows are never mutated in place -- closing an object's current row (on
    `update`) replaces its list entry with a new dict rather than assigning
    into the existing one. That makes `transaction()` rollback a cheap
    shallow-copy snapshot/restore of the three top-level lists (objects,
    links, audit) rather than a deep copy.

    **Tenancy here is structural** (M8b). Two `InMemoryStore` instances share no
    state, so one instance is one tenant's data by construction and cannot leak
    the way a shared file or database could. The `tenant` argument is still
    honored -- object and link rows carry it and reads filter on it -- so the
    stored shape and the read semantics match the other backends; but the
    cross-tenant isolation TESTS necessarily target the two backends where two
    stores can share a substrate. Audit and outbox rows are not filtered here,
    because within one instance every row has the same tenant and a filter would
    be a no-op dressed up as a guard.
    """

    def __init__(
        self, registry: OntologyRegistry, *, tenant: str = DEFAULT_TENANT
    ) -> None:
        """`tenant` scopes every read and write, matching `ObjectStore` (M8b).
        Two in-memory stores cannot share rows anyway, so this exists for parity:
        a test double whose tenant argument was ignored would let a caller
        "verify" isolation against a store that cannot leak."""
        if not tenant:
            raise ValueError("tenant must be a non-empty string")
        self._registry = registry
        self._tenant = tenant
        self._objects: list[_ObjectRow] = []
        self._links: list[_LinkRow] = []
        self._audit: list[AuditEntry] = []
        # Outbox rows are FROZEN models replaced wholesale on every state
        # change (never mutated in place), for the same reason object rows are:
        # it keeps `transaction()`'s rollback a shallow list copy.
        self._outbox: list[OutboxRecord] = []
        # One in-memory instance is its whole storage substrate. A re-entrant
        # lock serializes outer transactions between threads while still letting
        # store methods join the transaction opened by ActionExecutor. Depth is
        # thread-local: another action waiting for the lock must not look like a
        # caller-opened nested transaction to `in_transaction`.
        self._transaction_lock = threading.RLock()
        self._transaction_state = threading.local()
        self._write_capture = WriteCapture()
        # Mirrors `ObjectStore`'s `objects.row_id INTEGER PRIMARY KEY
        # AUTOINCREMENT`: a monotonic, per-row identity distinct from the
        # payload primary key (`Lineage.object_id`), whose uniqueness among
        # current rows the store does not enforce -- see `read_page`'s
        # docstring (spec pagination-hardening §5's amended decision). Never
        # exposed on `Lineage`/`StoredObject` (see `Lineage`'s docstring);
        # `read_page`'s only consumer-facing surface for it is the
        # out-of-band cursor string.
        self._next_row_id = 1
        self._fingerprint: OntologyFingerprint | None = None
        self._fingerprint_adopted = False
        # Recorded at construction, exactly as `ObjectStore` does. An in-memory
        # store is always born empty, so this always WRITES rather than
        # comparing -- keeping the call anyway means the two backends share one
        # code path instead of one of them quietly opting out of it.
        check_ontology_fingerprint(self, registry)

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
                    list[OutboxRecord],
                    int,
                ]
                | None
            ) = None
            if is_outermost:
                snapshot = (
                    list(self._objects),
                    list(self._links),
                    list(self._audit),
                    # Included for the same reason the audit list is: the emitting
                    # action's outbox rows must vanish with its writes when it rolls
                    # back (spec `durable-effect-outbox` AC1), or a rolled-back
                    # action notifies the outside world after all.
                    list(self._outbox),
                    self._next_row_id,
                )
            try:
                yield None
            # `BaseException`, not `Exception` (re-review finding, 2026-07-26): a
            # handler interrupted by KeyboardInterrupt previously left its mutations
            # in place, since this snapshot was only restored for `Exception`. Kept
            # identical to `ObjectStore.transaction` -- the two backends must agree
            # about what a rolled-back action leaves behind, which is exactly what the
            # `pending`-record atomicity claim depends on. Always re-raised.
            except BaseException:
                if is_outermost and snapshot is not None:
                    (
                        self._objects,
                        self._links,
                        self._audit,
                        self._outbox,
                        self._next_row_id,
                    ) = snapshot
                raise
            finally:
                self._transaction_state.depth -= 1

    @property
    def in_transaction(self) -> bool:
        return getattr(self._transaction_state, "depth", 0) > 0

    @contextmanager
    def capture_action_writes(self) -> Iterator[list[WriteRecord]]:
        with self._write_capture.capture() as records:
            yield records

    def _record_write(self, record: WriteRecord) -> None:
        self._write_capture.record(record)

    def _next_id(self) -> int:
        row_id = self._next_row_id
        self._next_row_id += 1
        return row_id

    # -- objects ---------------------------------------------------------

    def insert(self, obj_type: str, payload: dict[str, Any], source: Source) -> str:
        prepared = prepare_insert(
            self._registry, obj_type, payload, capturing=self._write_capture.active
        )
        obj_def, payload, obj_id = prepared.obj_def, prepared.payload, prepared.obj_id

        now = _utcnow_iso()
        with self.transaction():
            self._objects.append(
                {
                    "tenant": self._tenant,
                    "object_type": obj_type,
                    "id": obj_id,
                    # JSON round-trip (matching `ObjectStore`'s TEXT column):
                    # fresh, unshared structures on every read; a
                    # non-JSON-serializable value raises `TypeError` here,
                    # exactly as `ObjectStore.insert`'s `json.dumps` does.
                    "payload": json.dumps(payload),
                    "valid_from": now,
                    "valid_to": None,
                    "source_system": source.source_system,
                    "source_id": source.source_id,
                    "extracted_at": source.extracted_at,
                    "row_id": self._next_id(),
                    "page_token": uuid.uuid4().hex,
                    "type_version": obj_def.version,
                }
            )
        self._record_write(WriteRecord(op="create", object_type=obj_type, object_id=obj_id))
        return obj_id

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

        with self.transaction():
            for i, row in enumerate(self._objects):
                if (
                    row["object_type"] == obj_type
                    and row["id"] == obj_id
                    and row["valid_to"] is None
                ):
                    self._objects[i] = {**row, "valid_to": now}
                    break
            self._objects.append(
                {
                    "tenant": self._tenant,
                    "object_type": obj_type,
                    "id": obj_id,
                    "payload": json.dumps(merged),
                    "valid_from": now,
                    "valid_to": None,
                    "source_system": source.source_system,
                    "source_id": source.source_id,
                    "extracted_at": source.extracted_at,
                    "row_id": self._next_id(),
                    "page_token": uuid.uuid4().hex,
                    # A write always lands at the CURRENT declared version: the
                    # merge above is a current-shape change over an already-upcast
                    # payload, so the result is current by construction.
                    "type_version": obj_def.version,
                }
            )
        self._record_write(WriteRecord(op="update", object_type=obj_type, object_id=obj_id))

    def _row_to_stored(self, row: _ObjectRow) -> StoredObject:
        # Upcast at the read boundary, identical to `ObjectStore._row_to_stored`
        # (M9b) -- the two backends must agree about what an older row reads as,
        # or a test double lets through a shape production would reject.
        # `json.loads` here (mirroring `ObjectStore._row_to_stored`) is what
        # guarantees a fresh, unshared structure on every read: mutating the
        # returned payload (including nested containers) can never reach
        # back into `self._objects` or leak forward into a later read.
        payload: dict[str, Any] = upcast_payload(
            self._registry,
            row["object_type"],
            json.loads(row["payload"]),
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

    def read_all(self, obj_type: str) -> list[StoredObject]:
        """Raw (unredacted, unscoped) read of every current row of
        `obj_type`, ordered by the same per-row identity as `read_page`
        (spec AC2). Its OWN single pass -- NOT a `read_page` loop: looping
        would re-scan/re-sort per batch, and a batch-boundary loop is
        exactly what silently dropped tied rows under the old payload-pk-
        keyed cursor design this task replaces."""
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
        was issued for (spec §5's T2 amendment) -- a linear scan here rather
        than a maintained `token -> row_id` dict, unlike `ObjectStore`'s
        real unique SQL index: this backend is a dependency-free test
        double, never the scale-sensitive path the index in `ObjectStore`
        exists to keep O(1) (see `idx_objects_page_token`'s comment), and a
        maintained dict would need its own transaction-rollback snapshot
        logic for no behavioral gain. The scan additionally requires
        `row["object_type"] == obj_type`: `page_token` is only unique
        *globally*, not scoped to a type, so a token issued for one object
        type is a real token belonging to a DIFFERENT type's row --
        without this guard it would still resolve to that row's `row_id`
        and be accepted as a valid resume point into the CALLER's
        requested type, silently reinterpreted as an arbitrary positional
        offset into an unrelated type's ordering instead of refused.
        Raises a validation-kind `INVALID_CURSOR` failure if `token` was never
        issued for `obj_type` specifically."""
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
        among current rows -- spec pagination-hardening §5's amended
        decision). `row_id` never leaves this method except resolved-into
        internally: the CURSOR a caller actually receives, `PagedRow.key`,
        is a random per-row page token instead (spec §5's T2 amendment --
        see `PagedRow`'s docstring for why), resolved back to `row_id` via
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
        with self.transaction():
            self._links.append(
                {
                    "tenant": self._tenant,
                    "link_type": link_type,
                    "from_id": from_id,
                    "to_id": to_id,
                    "valid_from": now,
                    "valid_to": None,
                }
            )
        self._record_write(
            WriteRecord(op="link", link_type=link_type, from_id=from_id, to_id=to_id)
        )

    def links_from(self, link_type: str, from_id: str) -> list[str]:
        resolve_link_type(self._registry, link_type)
        with self._transaction_lock:
            return [
                row["to_id"]
                for row in self._links
                if row["tenant"] == self._tenant
                and row["link_type"] == link_type
                and row["from_id"] == from_id
                and row["valid_to"] is None
            ]

    def links_to(self, link_type: str, to_id: str) -> list[str]:
        resolve_link_type(self._registry, link_type)
        with self._transaction_lock:
            return [
                row["from_id"]
                for row in self._links
                if row["tenant"] == self._tenant
                and row["link_type"] == link_type
                and row["to_id"] == to_id
                and row["valid_to"] is None
            ]

    # -- audit ---------------------------------------------------------

    def append_audit(self, entry: AuditEntry) -> None:
        """Persist one audit entry. Never raises (declared-contracts §3
        AC12): params/writes that fail JSON encoding are recorded as
        placeholders via `_safe_json_dumps`, matching `ObjectStore`'s
        JSON-round-trip behavior exactly even though this backend has no
        physical serialization requirement of its own."""
        # encode-then-decode through the SHARED codec -- literally the same
        # serialize/rebuild round trip this method used to hand-write per
        # field. The hand-written version silently dropped every field it
        # forgot to list, and shipped that bug twice (`invocation_id`, M7a;
        # `kind`, M7b -- see git history). With the field enumeration living
        # only in `ontary.store._shared`, this backend no longer HAS a list
        # to forget a field from; `test_audit_entries_round_trip_every_field`
        # still guards the codec itself.
        normalized = decode_audit_entry(encode_audit_entry(entry)._asdict())
        with self.transaction():
            self._audit.append(normalized)

    def audit_entries(self) -> list[AuditEntry]:
        """Deep copies, so a reader cannot rewrite the append-only log.

        Whole-branch review finding (2026-07-26): `list(self._audit)` copied the
        LIST but handed out the same mutable `AuditEntry`/`EffectRecord`/
        `WriteRecord` objects it stores. A dispatcher closing over an in-memory
        store could therefore run
        `store.audit_entries()[0].effects[0].outcome = "failed"` and rewrite a
        durable, already-committed `pending` record in place -- an append-only log
        that was not append-only.

        `ObjectStore` never had this hole: it reconstructs models from persisted
        JSON on every read, so its callers always get fresh objects. That made
        this a BACKEND-PARITY defect as well -- two `Store` implementations with
        different audit-mutability semantics while claiming to satisfy one
        Protocol. Copying at the seam restores parity without freezing the record
        models, which would ripple through every construction site in the engine.
        """
        with self._transaction_lock:
            return [entry.model_copy(deep=True) for entry in self._audit]

    # -- ontology fingerprint --------------------------------------------

    def read_ontology_fingerprint(self) -> OntologyFingerprint | None:
        return self._fingerprint

    def write_ontology_fingerprint(
        self, fingerprint: OntologyFingerprint, *, adopted: bool
    ) -> None:
        """Kept in memory like everything else here.

        Drift detection is nearly vacuous for this backend -- an `InMemoryStore`
        is born empty, so its constructor always records rather than compares --
        and it is implemented anyway, for two reasons: the conformance suite can
        then assert the storage contract itself on both backends, and a test
        double that silently lacked the machinery would let a caller "verify"
        drift handling against a store that cannot drift.
        """
        self._fingerprint = fingerprint
        self._fingerprint_adopted = adopted

    # -- effect outbox ---------------------------------------------------

    def enqueue_effects(self, records: Sequence[OutboxRecord]) -> None:
        """Append outbox rows inside the caller's transaction (spec AC1).

        Round-trips each payload through JSON to match `ObjectStore`, which
        has no choice about it: a payload this backend would happily keep as
        live Python objects but SQLite could not store must fail the SAME way
        on both, inside the action transaction, rather than being deliverable
        in tests and unsendable in production.
        """
        with self.transaction():
            for record in records:
                if any(row.effect_id == record.effect_id for row in self._outbox):
                    # `effect_id TEXT PRIMARY KEY` on the SQLite side.
                    raise ValueError(
                        f"duplicate outbox effect_id: {record.effect_id!r}"
                    )
                if any(
                    row.invocation_id == record.invocation_id and row.seq == record.seq
                    for row in self._outbox
                ):
                    # `idx_effect_outbox_invocation_seq` on the SQLite side.
                    raise ValueError(
                        "duplicate outbox (invocation_id, seq): "
                        f"({record.invocation_id!r}, {record.seq})"
                    )
                self._outbox.append(
                    record.model_copy(
                        update={"payload": json.loads(json.dumps(record.payload))}
                    )
                )

    def _replace_outbox_row(self, effect_id: str, **changes: Any) -> bool:
        for index, row in enumerate(self._outbox):
            if row.effect_id == effect_id:
                self._outbox[index] = row.model_copy(update=changes)
                return True
        return False

    def claim_due_effects(
        self, *, limit: int, now: datetime, lease: timedelta
    ) -> list[OutboxRecord]:
        check_claim_limit(limit)
        lease_until = now + lease
        claimed: list[OutboxRecord] = []
        # The due scan happens INSIDE the transaction, matching the SQL
        # backends' single-statement scan+lease: scanning before taking the
        # lock let a dispatcher observe -- and then "claim" -- outbox rows of
        # an in-flight transaction that subsequently rolled back, delivering
        # an effect of an action that never happened (and let two concurrent
        # claimers lease the same pending row).
        with self.transaction():
            due = sorted(
                (
                    row
                    for row in self._outbox
                    if row.state == "pending"
                    and row.next_attempt_at <= now
                    and (row.lease_until is None or row.lease_until <= now)
                ),
                key=lambda row: (row.emitted_at, row.seq),
            )[:limit]
            for row in due:
                if self._replace_outbox_row(
                    row.effect_id, lease_until=lease_until, updated_at=now
                ):
                    claimed.append(
                        row.model_copy(
                            update={"lease_until": lease_until, "updated_at": now}
                        )
                    )
        return claimed

    def release_effect_claim(self, effect_id: str, *, now: datetime) -> None:
        with self.transaction():
            self._replace_outbox_row(effect_id, lease_until=None, updated_at=now)

    def resolve_effect(
        self,
        effect_id: str,
        *,
        state: OutboxState,
        next_attempt_at: datetime,
        error: str | None,
        now: datetime,
    ) -> None:
        # Lookup inside the transaction for the same reason as the claim
        # scan: an unlocked read could pair a stale `attempts` with another
        # thread's concurrent resolve and lose an increment.
        with self.transaction():
            current = next(
                (row for row in self._outbox if row.effect_id == effect_id), None
            )
            if current is None:
                return
            self._replace_outbox_row(
                effect_id,
                state=state,
                attempts=current.attempts + 1,
                next_attempt_at=next_attempt_at,
                lease_until=None,
                last_error=error,
                updated_at=now,
            )

    def outbox_entries(self) -> list[OutboxRecord]:
        """Copies, in emission order. `OutboxRecord` is frozen, but its
        `payload` dict is not, so the deep copy is what stops a caller from
        editing a stored payload through the value it was handed -- the same
        parity rule `audit_entries` documents."""
        with self._transaction_lock:
            return [
                row.model_copy(deep=True)
                for row in sorted(
                    self._outbox, key=lambda row: (row.emitted_at, row.seq)
                )
            ]


def _static_conformance_check(registry: OntologyRegistry) -> Store:
    """Never called at runtime -- exists purely so mypy (which runs
    `--strict` over `src/`, see `make verify`) fails the build if
    `InMemoryStore` ever drifts from the `Store` Protocol it's declared to
    satisfy (spec §6 "Storage seam"). Returning `InMemoryStore(registry)`
    typed as `Store` forces a structural check at type-check time with zero
    runtime cost and zero `# type: ignore`."""
    return InMemoryStore(registry)
