"""Build permissive store prefilters without replacing Python read gates."""

from __future__ import annotations

from typing import Any, Literal

from ontary.meta import OntologyRegistry
from ontary.query._host import _QueryHost
from ontary.query._where import CompiledWhere, _WhereClause
from ontary.scope import CustomResolver, DirectProperty, ScopePolicy, SelfScope, ViaLink
from ontary.security import Consumer
from ontary.store._core import StoreCore
from ontary.store._filter import (
    RowFilter,
    ScopeTerm,
    WhereTerm,
)
from ontary.store.protocol import Store
from ontary.store.values import PagedRow, StoredObject


def scope_pushdown(policy: ScopePolicy, obj_type: str) -> Literal["sql", "python", "unscoped"]:
    """Classify the type's scope declaration, including every rule level."""
    if obj_type in policy.row_visibility or any(
        isinstance(rule, ViaLink | CustomResolver)
        for rule in policy.rules.get(obj_type, [])
    ):
        return "python"
    if obj_type in policy.unscoped_types:
        return "unscoped"
    return "sql"


def filter_is_exact(policy: ScopePolicy, obj_type: str) -> bool:
    """Whether well-formed rows need no additional Python scope filtering.

    Permissive storage fallbacks still require judging every fetched row.
    This classification only chooses the batch size.
    """
    return scope_pushdown(policy, obj_type) != "python"


def store_filter_is_exact(
    policy: ScopePolicy, obj_type: str, store: Store, row_filter: RowFilter | None,
) -> bool:
    """Choose small batches only when storage actually applies the prefilter."""
    return (
        row_filter is not None
        and isinstance(store, StoreCore)
        and scope_pushdown(policy, obj_type) != "python"
        and store.prefilter_exact(row_filter)
    )


def _normalize_scalar_operand(value: Any) -> Any:
    """Bind the underlying scalar, including str-mixin Enum members."""
    if isinstance(value, str) and type(value) is not str:
        return str.__str__(value)
    if isinstance(value, int) and type(value) not in {int, bool}:
        return int.__index__(value)
    if isinstance(value, float) and type(value) is not float:
        return float(value)
    return value


def _where_term(clause: _WhereClause) -> WhereTerm:
    field, prop_type, op, operand = clause
    if op == "and":
        operand = tuple(_where_term(sub) for sub in operand)
    elif op == "in":
        operand = [_normalize_scalar_operand(item) for item in operand]
    else:
        operand = _normalize_scalar_operand(operand)
    return WhereTerm(field, prop_type, op, operand)


def build_row_filter(
    registry: OntologyRegistry,
    policy: ScopePolicy,
    consumer: Consumer,
    obj_type: str,
    compiled_where: CompiledWhere | None,
) -> RowFilter | None:
    """Translate validated where clauses and the consumer's scope rules."""
    where = () if compiled_where is None else tuple(
        _where_term(clause) for clause in compiled_where.clauses
    )
    scope = None
    if scope_pushdown(policy, obj_type) == "sql":
        fields = {prop.name for prop in registry.get_object_type(obj_type).properties}
        rules: list[tuple[str, str | None]] = []
        for rule in policy.rules.get(obj_type, []):
            if isinstance(rule, SelfScope) and rule.level == consumer.scope_level:
                rules.append(("self", None))
            elif isinstance(rule, DirectProperty) and rule.level == consumer.scope_level:
                # An unvalidated policy may reference an undeclared payload
                # key. Dropping the whole scope prefilter preserves rule order
                # and lets Python decide without binding an undeclared field.
                if rule.property_name not in fields:
                    return RowFilter(where=where) if where else None
                rules.append(("prop", rule.property_name))
        climb_possible = (
            consumer.scope_level in policy.levels
            and policy.levels.index(consumer.scope_level) > 0
        )
        scope = ScopeTerm(tuple(rules), consumer.scope_id, climb_possible)
    return RowFilter(where=where, scope=scope) if where or scope is not None else None


def _fetch_all(
    query: _QueryHost,
    obj_type: str,
    row_filter: RowFilter | None,
) -> list[StoredObject]:
    """Fetch a full snapshot, using the internal prefilter API when available."""
    if row_filter is not None and isinstance(query._store, StoreCore):
        return query._store.read_all_filtered(obj_type, row_filter)
    return query._store.read_all(obj_type)


def _fetch_page(
    query: _QueryHost,
    obj_type: str,
    row_filter: RowFilter | None,
    after: str | None,
    batch: int,
) -> list[PagedRow]:
    """Use the internal filtered API only for stores that own that API."""
    if row_filter is not None and isinstance(query._store, StoreCore):
        return query._store.read_page_filtered(
            obj_type, row_filter, after_key=after, batch=batch,
        )
    return query._store.read_page(obj_type, after_key=after, batch=batch)
