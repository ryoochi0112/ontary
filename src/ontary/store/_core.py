"""The one write path every `Store` backend shares.

Backends supply storage steps only. `StoreCore` owns the order of steps of
every public write -- canonical ids, the authority and contract checks, the
payload encoding, the refusal and no-op reads, cardinality, the clock read,
and the write record -- and calls abstract `_`-prefixed storage steps that
each backend implements against its own substrate (dicts, SQLite, Postgres).
A fix to a write rule is therefore one edit here, and the three backends
agree by construction. The conformance suite (`tests/test_store_conformance.py`)
proves the storage steps each backend supplies.

The order of steps of each write:

1. Canonical ids and the authority / contract checks, outside the
   transaction.
2. The payload is JSON-encoded before the clock is read and before any row
   changes, so an unencodable payload fails with nothing written and no
   clock tick spent.
3. `insert`, `retire_object` and `close_link` read the clock here, outside
   the transaction.
4. Inside `with self.transaction()`: every read that decides a refusal or a
   no-op -- existence, link endpoints, the identical live link, and
   cardinality. `update` merges and then reads the clock; `create_link`
   reads the clock only after cardinality passes, so a refused link spends
   no tick. The not-before-`valid_from` check runs on every live row that
   the write closes, then the storage steps write and close rows.
5. The write record is captured after the transaction exits.

Transaction mechanics (`transaction`, `in_transaction`) stay backend-owned.
"""

from __future__ import annotations

import abc
import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from typing import Any

from ontary.audit import AuditEntry, WriteRecord
from ontary.meta import Cardinality, OntologyRegistry
from ontary.store._shared import (
    AuditRowFields,
    StoreClock,
    WriteCapture,
    canonical_id,
    cardinality_violation,
    check_link_endpoints,
    check_link_removal_authority,
    check_link_write_authority,
    check_object_removal_authority,
    check_update_authority,
    encode_audit_entry,
    ensure_not_before,
    live_link_not_found,
    merge_update,
    object_already_exists,
    prepare_insert,
    retire_object_refusal,
)
from ontary.store.values import Source, StoredObject

_FROM_SIDE_CARDINALITIES = (Cardinality.ONE_TO_ONE, Cardinality.MANY_TO_ONE)
_TO_SIDE_CARDINALITIES = (Cardinality.ONE_TO_ONE, Cardinality.ONE_TO_MANY)


class StoreCore(abc.ABC):
    """Shared base of every `Store` backend: the public write methods, the
    clock, and write capture live here once; backends implement the abstract
    transaction and storage steps."""

    def __init__(
        self,
        registry: OntologyRegistry,
        *,
        tenant: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`tenant` scopes every read and write and must be non-empty.
        `clock`, when given, is installed exactly as `bind_clock(clock)`
        would install it; by default no clock is installed."""
        if not tenant:
            raise ValueError("tenant must be a non-empty string")
        self._registry = registry
        self._tenant = tenant
        self._clock_state = StoreClock()
        self._write_capture = WriteCapture()
        if clock is not None:
            self._clock_state.bind(clock)

    # -- backend-owned transaction mechanics ---------------------------

    @abc.abstractmethod
    def transaction(self) -> AbstractContextManager[Any]:
        """Serialized, reentrant read/write transaction."""

    @property
    @abc.abstractmethod
    def in_transaction(self) -> bool:
        """`True` while this thread is inside `transaction()`."""

    # -- reads the write path calls ------------------------------------

    @abc.abstractmethod
    def read_current(self, obj_type: str, obj_id: str) -> StoredObject | None: ...

    @abc.abstractmethod
    def read_last(self, obj_type: str, obj_id: str) -> StoredObject | None: ...

    @abc.abstractmethod
    def links_from(self, link_type: str, from_id: str) -> list[str]: ...

    @abc.abstractmethod
    def links_to(self, link_type: str, to_id: str) -> list[str]: ...

    # -- storage steps -------------------------------------------------

    @abc.abstractmethod
    def _insert_object_row(
        self,
        obj_type: str,
        obj_id: str,
        encoded_payload: str,
        valid_from: str,
        source: Source,
    ) -> None:
        """Append one live object row with a fresh page token."""

    @abc.abstractmethod
    def _live_object_rows(self, obj_type: str, obj_id: str) -> list[StoredObject]:
        """Every live row of one object in this tenant, oldest first."""

    @abc.abstractmethod
    def _has_object_history(self, obj_type: str, obj_id: str) -> bool:
        """Whether this tenant holds any row, live or closed, for the object."""

    @abc.abstractmethod
    def _close_object_rows(self, obj_type: str, obj_id: str, valid_to: str) -> None:
        """Close every live row of one object in this tenant at `valid_to`."""

    @abc.abstractmethod
    def _insert_link_row(
        self, link_type: str, from_id: str, to_id: str, valid_from: str
    ) -> None:
        """Append one live link row."""

    @abc.abstractmethod
    def _live_link_valid_froms(
        self, link_type: str, from_id: str, to_id: str
    ) -> list[str]:
        """The `valid_from` of every live row of one link in this tenant."""

    @abc.abstractmethod
    def _close_link_rows(
        self, link_type: str, from_id: str, to_id: str, valid_to: str
    ) -> None:
        """Close every live row of one link in this tenant at `valid_to`."""

    @abc.abstractmethod
    def _append_audit_row(self, fields: AuditRowFields) -> None:
        """Persist one encoded audit entry."""

    # -- clock and write capture ---------------------------------------

    def bind_clock(
        self, clock: Callable[[], datetime] | None
    ) -> Callable[[], datetime]:
        return self._clock_state.bind(clock)

    @contextmanager
    def capture_action_writes(
        self, *, at: str | None = None
    ) -> Iterator[list[WriteRecord]]:
        with self._write_capture.capture(at=at) as records:
            yield records

    def _record_write(self, record: WriteRecord) -> None:
        self._write_capture.record(record)

    def _now(self) -> str:
        return self._clock_state.now_iso(self._write_capture)

    # -- objects -------------------------------------------------------

    def insert(self, obj_type: str, payload: dict[str, Any], source: Source) -> str:
        prepared = prepare_insert(
            self._registry, obj_type, payload, capturing=self._write_capture.active
        )
        obj_id = prepared.obj_id
        # `json.dumps`, not a lenient encoder: an unencodable payload must
        # fail the write on every backend, before the clock is read.
        encoded = json.dumps(prepared.payload)
        now = self._now()
        with self.transaction():
            if self.read_current(obj_type, obj_id) is not None:
                raise object_already_exists(obj_type, obj_id)
            self._insert_object_row(obj_type, obj_id, encoded, now, source)
        self._record_write(WriteRecord(op="create", object_type=obj_type, object_id=obj_id))
        return obj_id

    def update(
        self,
        obj_type: str,
        obj_id: str,
        payload_changes: dict[str, Any],
        source: Source,
    ) -> None:
        obj_id = canonical_id(obj_id)
        capturing = self._write_capture.active
        obj_def = check_update_authority(
            self._registry, obj_type, payload_changes, capturing=capturing
        )
        with self.transaction():
            current = self.read_current(obj_type, obj_id)
            merged = merge_update(
                obj_def, obj_type, obj_id, current, payload_changes, capturing=capturing
            )
            # Encoded before the clock read and before any row is closed, so
            # an unencodable merge leaves the old row live even when the
            # caller catches the error inside an outer transaction.
            encoded = json.dumps(merged)
            now = self._now()
            for row in self._live_object_rows(obj_type, obj_id):
                ensure_not_before(
                    row.lineage.valid_from, now, what=f"{obj_type} {obj_id}"
                )
            self._close_object_rows(obj_type, obj_id, now)
            self._insert_object_row(obj_type, obj_id, encoded, now, source)
        self._record_write(WriteRecord(op="update", object_type=obj_type, object_id=obj_id))

    def retire_object(self, object_type: str, obj_id: str) -> StoredObject:
        obj_id = canonical_id(obj_id)
        check_object_removal_authority(
            self._registry,
            object_type,
            capturing=self._write_capture.active,
        )
        now = self._now()
        with self.transaction():
            live = self._live_object_rows(object_type, obj_id)
            if not live:
                raise retire_object_refusal(
                    object_type,
                    obj_id,
                    has_history=self._has_object_history(object_type, obj_id),
                )
            for row in live:
                ensure_not_before(
                    row.lineage.valid_from, now, what=f"{object_type} {obj_id}"
                )
            self._close_object_rows(object_type, obj_id, now)
            newest = live[-1]
            retired = StoredObject(
                payload=newest.payload,
                lineage=newest.lineage.model_copy(update={"valid_to": now}),
            )
        self._record_write(
            WriteRecord(op="retire", object_type=object_type, object_id=obj_id)
        )
        return retired

    # -- links ---------------------------------------------------------

    def create_link(self, link_type: str, from_id: str, to_id: str) -> None:
        from_id, to_id = canonical_id(from_id), canonical_id(to_id)
        link_def = check_link_write_authority(
            self._registry, link_type, capturing=self._write_capture.active
        )
        with self.transaction():
            check_link_endpoints(
                link_def,
                link_type,
                from_id,
                to_id,
                read_current=self.read_current,
                read_last=self.read_last,
            )
            existing_from = self.links_from(link_type, from_id)
            if to_id in existing_from:
                # An identical live link already exists: a no-op, checked
                # before cardinality so a re-run never trips over itself.
                # Nothing is written, so no write record is captured either.
                return
            cardinality = link_def.cardinality
            if cardinality in _FROM_SIDE_CARDINALITIES and existing_from:
                raise cardinality_violation(link_type, "from_id", from_id, cardinality)
            if cardinality in _TO_SIDE_CARDINALITIES and self.links_to(link_type, to_id):
                raise cardinality_violation(link_type, "to_id", to_id, cardinality)
            now = self._now()
            self._insert_link_row(link_type, from_id, to_id, now)
        self._record_write(
            WriteRecord(op="link", link_type=link_type, from_id=from_id, to_id=to_id)
        )

    def close_link(self, link_type: str, from_id: str, to_id: str) -> bool:
        from_id, to_id = canonical_id(from_id), canonical_id(to_id)
        check_link_removal_authority(
            self._registry,
            link_type,
            capturing=self._write_capture.active,
        )
        now = self._now()
        with self.transaction():
            live_froms = self._live_link_valid_froms(link_type, from_id, to_id)
            if not live_froms:
                raise live_link_not_found(link_type, from_id, to_id)
            for valid_from in live_froms:
                ensure_not_before(
                    valid_from, now, what=f"{link_type} {from_id}->{to_id}"
                )
            self._close_link_rows(link_type, from_id, to_id, now)
        self._record_write(
            WriteRecord(
                op="unlink", link_type=link_type, from_id=from_id, to_id=to_id
            )
        )
        return True

    # -- audit ---------------------------------------------------------

    def append_audit(self, entry: AuditEntry) -> None:
        """Persist one audit entry. Never raises on payload content: values
        that fail JSON encoding are recorded as placeholders by the shared
        audit codec."""
        fields = encode_audit_entry(entry)
        with self.transaction():
            self._append_audit_row(fields)
