"""Pin the public surface of `ontary.store` and the `Store` protocol.

The package has no `__all__`, so the front door's surface is its public,
non-module names. Literals are captured from the tree, not computed, so any
rename, removal, or signature change fails here first.
"""

from __future__ import annotations

import inspect
import types

from test_store_conformance import build_registry

import ontary.store as store_pkg
from ontary.store import InMemoryStore, ObjectStore, Store
from ontary.store.protocol import Store as ProtocolStore

EXPECTED_NAMES = [
    "AuditEntry",
    "CapabilityAccessRecord",
    "DEFAULT_BATCH",
    "DEFAULT_TENANT",
    "InMemoryStore",
    "Lineage",
    "ObjectStore",
    "PagedRow",
    "PostgresStore",
    "SCHEMA_VERSION",
    "Source",
    "Store",
    "StoredObject",
    "WriteRecord",
]

EXPECTED_SCHEMA_VERSION = 14

EXPECTED_SIGNATURES = {
    "append_audit": "(self, entry: 'AuditEntry') -> 'None'",
    "audit_entries": "(self) -> 'list[AuditEntry]'",
    "bind_clock": "(self, clock: 'Callable[[], datetime] | None') -> 'Callable[[], datetime]'",
    "capture_action_writes": "(self, *, at: 'str | None' = None) -> 'AbstractContextManager[list[WriteRecord]]'",
    "close_link": "(self, link_type: 'str', from_id: 'str', to_id: 'str') -> 'bool'",
    "create_link": "(self, link_type: 'str', from_id: 'str', to_id: 'str') -> 'None'",
    "insert": "(self, obj_type: 'str', payload: 'dict[str, Any]', source: \"'Source'\") -> 'str'",
    "links_from": "(self, link_type: 'str', from_id: 'str') -> 'list[str]'",
    "links_from_asof": "(self, link_type: 'str', from_id: 'str', asof: 'str') -> 'list[str]'",
    "links_to": "(self, link_type: 'str', to_id: 'str') -> 'list[str]'",
    "links_to_asof": "(self, link_type: 'str', to_id: 'str', asof: 'str') -> 'list[str]'",
    "read_all": "(self, obj_type: 'str') -> 'list[StoredObject]'",
    "read_current": "(self, obj_type: 'str', obj_id: 'str') -> 'StoredObject | None'",
    "read_last": "(self, obj_type: 'str', obj_id: 'str') -> 'StoredObject | None'",
    "read_page": "(self, obj_type: 'str', after_key: 'str | None' = None, batch: 'int' = 500) -> 'list[PagedRow]'",
    "retire_object": "(self, object_type: 'str', obj_id: 'str') -> 'StoredObject'",
    "transaction": "(self) -> 'AbstractContextManager[Any]'",
    "update": "(self, obj_type: 'str', obj_id: 'str', payload_changes: 'dict[str, Any]', source: \"'Source'\") -> 'None'",
}


def test_store_front_door_names_are_pinned() -> None:
    names = sorted(
        n
        for n, v in vars(store_pkg).items()
        if not n.startswith("_") and n != "annotations" and not isinstance(v, types.ModuleType)
    )
    assert names == EXPECTED_NAMES


def test_schema_version_is_pinned() -> None:
    assert store_pkg.SCHEMA_VERSION == EXPECTED_SCHEMA_VERSION


def test_store_protocol_signatures_are_pinned() -> None:
    actual = {
        name: str(inspect.signature(member))
        for name, member in inspect.getmembers(ProtocolStore, callable)
        if not name.startswith("_")
    }
    assert actual == EXPECTED_SIGNATURES
    assert isinstance(inspect.getattr_static(ProtocolStore, "in_transaction"), property)


def test_both_local_backends_satisfy_the_protocol() -> None:
    registry = build_registry()
    assert isinstance(InMemoryStore(registry), Store)
    assert isinstance(ObjectStore(registry, ":memory:"), Store)
