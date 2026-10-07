"""The `GuardedQuery` surface that concern modules call back into."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from ontary.meta import OntologyRegistry
from ontary.query._disclosure import _ReadDisclosure
from ontary.scope import ScopePolicy, _ScopeReadCache
from ontary.security import Consumer
from ontary.store import Store, StoredObject


class _QueryHost(Protocol):
    _store: Store
    _registry: OntologyRegistry
    _policy: ScopePolicy

    def _visible(
        self, consumer: Consumer, obj: StoredObject, *,
        scope_cache: _ScopeReadCache | None = None,
        resolved_scope: dict[str, str | None] | None = None,
    ) -> bool: ...

    def _redact(
        self, consumer: Consumer, obj_type: str, obj: StoredObject,
    ) -> StoredObject: ...

    def _require_coherent_scope(
        self, obj_type: str, *, where: dict[str, Any] | None = None, group_by: str | None = None,
    ) -> _ReadDisclosure: ...

    def _aggregate(
        self,
        consumer: Consumer,
        obj_type: str,
        value_field: str | None,
        where: dict[str, Any] | None,
        group_by: str | None,
        func: Literal["mean", "count", "sum", "min", "max"],
        *,
        scope_cache: _ScopeReadCache,
        _author_dispatch: Any = None,
        _disclosures: list[tuple[str, str]] | None = None,
    ) -> dict[str, int | float] | int | float: ...
