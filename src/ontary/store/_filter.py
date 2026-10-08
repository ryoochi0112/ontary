"""Neutral row-filter IR and permissive SQL predicates.

The query layer remains the final judge. Unknown JSON shapes pass through;
well-formed scalars and null/missing values are filtered in storage.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ontary.store import _sql
from ontary.typesys import PropertyType


@dataclass(frozen=True)
class WhereTerm:
    field: str
    prop_type: PropertyType
    op: str
    operand: Any


@dataclass(frozen=True)
class ScopeTerm:
    rules: tuple[tuple[str, str | None], ...]
    scope_id: str
    climb_possible: bool


@dataclass(frozen=True)
class RowFilter:
    where: tuple[WhereTerm, ...] = ()
    scope: ScopeTerm | None = None


OBJECT_FILTERED_ALL_SELECT_TEMPLATE = f"""
SELECT {', '.join(_sql.OBJECT_COLUMNS)} FROM objects
WHERE object_type = {{p}} AND tenant = {{p}} AND valid_to IS NULL
  AND ({{filter}})
ORDER BY row_id ASC
"""


OBJECT_FILTERED_PAGE_SELECT_TEMPLATE = f"""
SELECT {', '.join(_sql.OBJECT_COLUMNS)} FROM objects
WHERE object_type = {{p}} AND tenant = {{p}} AND valid_to IS NULL
  AND row_id > {{p}} AND ({{filter}})
ORDER BY row_id ASC
LIMIT {{p}}
"""

_OPERATORS = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_ISO_INSTANT = (
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})?$"
)


def _unbindable(value: object) -> bool:
    """Strings outside both drivers' text domain must be judged in Python."""
    if not isinstance(value, str):
        return False
    if "\x00" in value:
        return True
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _compiles_to_true(term: WhereTerm | ScopeTerm) -> bool:
    """Identify unconditional pass-through components in either SQL dialect."""
    if isinstance(term, ScopeTerm):
        if not term.rules:
            return term.climb_possible
        return _unbindable(term.scope_id) or any(
            _unbindable(field) for _, field in term.rules
        )
    if term.op == "and":
        return all(_compiles_to_true(child) for child in term.operand)
    if _unbindable(term.field) or _unbindable(term.operand):
        return True
    if term.op == "in":
        return any(_compiles_to_true(
            WhereTerm(term.field, term.prop_type, "eq", item),
        ) for item in term.operand)
    return term.op in {*_OPERATORS, "contains"} and (
        isinstance(term.operand, dict | list) or term.prop_type == "struct"
    )


def filter_is_selective(row_filter: RowFilter) -> bool:
    """Whether every component filters rather than unconditionally passing rows.

    This only controls batch sizing; Python still judges every fetched row.
    """
    return all(not _compiles_to_true(term) for term in row_filter.where) and (
        row_filter.scope is None or not _compiles_to_true(row_filter.scope)
    )


def compile_filter(f: RowFilter, dialect: _sql.Dialect) -> tuple[str, list[Any]]:
    """Return fixed SQL with bind markers, and parameters in marker order."""
    parts: list[str] = []
    params: list[Any] = []
    for term in f.where:
        fragment, values = _compile_term(term, dialect)
        parts.append(fragment)
        params.extend(values)
    if f.scope is not None:
        fragment, values = _compile_scope(f.scope, dialect)
        parts.append(fragment)
        params.extend(values)
    return " AND ".join(parts) or "TRUE", params


def _compile_scope(scope: ScopeTerm, dialect: _sql.Dialect) -> tuple[str, list[Any]]:
    """Preserve the first resolving rule; leave hierarchy climbing to Python."""
    fallback = "TRUE" if scope.climb_possible else "FALSE"
    if _compiles_to_true(scope):
        return "TRUE", []
    if not scope.rules:
        return "FALSE", []
    parts: list[str] = []
    params: list[Any] = []
    collate = "COLLATE BINARY" if dialect == "sqlite" else 'COLLATE "C"'
    for kind, field in scope.rules:
        if kind == "self":
            parts.append(f"WHEN TRUE THEN id {collate} = {{p}}")
            params.append(scope.scope_id)
        elif kind == "prop" and field is not None:
            fragment, values = _compile_scope_property(field, scope.scope_id, dialect)
            parts.append(fragment)
            params.extend(values)
        else:
            raise ValueError(f"unknown scope rule: {kind}")
    chain = "CASE " + " ".join(parts) + f" ELSE {fallback} END"
    # Protect every per-row JSON operation, even when self is the first rule.
    # PostgreSQL payload is TEXT and may contain JSON that jsonb rejects.
    valid = "json_valid(payload)" if dialect == "sqlite" else "pg_input_is_valid(payload, 'jsonb')"
    return f"CASE WHEN {valid} THEN {chain} ELSE TRUE END", params


def _compile_scope_property(
    field: str, scope_id: str, dialect: _sql.Dialect,
) -> tuple[str, list[Any]]:
    if dialect == "sqlite":
        field = '$.' + json.dumps(field, ensure_ascii=False)
        value_type = "json_type(payload, {p})"
        value = "json_extract(payload, {p})"
        # A non-null value resolves even when it is not a string. Python's
        # str() decides those values; NUL strings also pass through.
        match = (
            f"CASE WHEN {value_type} <> 'text' THEN TRUE "
            f"WHEN instr({value}, char(0)) > 0 THEN TRUE "
            f"ELSE {value} COLLATE BINARY = {{p}} END"
        )
        fields = [field] * 5
    else:
        value_type = "jsonb_typeof(payload::jsonb -> {p})"
        match = (
            f"CASE WHEN {value_type} <> 'string' THEN TRUE "
            'ELSE (payload::jsonb ->> {p}) COLLATE "C" = {p} END'
        )
        fields = [field] * 4
    resolves = f"{value_type} IS NOT NULL AND {value_type} <> 'null'"
    return f"WHEN {resolves} THEN {match}", [*fields, scope_id]


def _combine(terms: Iterable[WhereTerm], dialect: _sql.Dialect, join: str) -> tuple[str, list[Any]]:
    parts: list[str] = []
    params: list[Any] = []
    for term in terms:
        fragment, values = _compile_term(term, dialect)
        parts.append(fragment)
        params.extend(values)
    if not parts:
        return ("TRUE" if join == " AND " else "FALSE"), params
    return "(" + join.join(parts) + ")", params


def _scalar_type(term: WhereTerm) -> str:
    if term.prop_type != "json":
        return term.prop_type
    if isinstance(term.operand, bool):
        return "bool"
    if isinstance(term.operand, int | float):
        return "float"
    return "str"


def _type_guard(kind: str, dialect: _sql.Dialect) -> str:
    if kind in {"int", "float"}:
        return "t IN ('integer', 'real')" if dialect == "sqlite" else "t = 'number'"
    if kind == "bool":
        return "t IN ('true', 'false')" if dialect == "sqlite" else "t = 'boolean'"
    return "t = 'text'" if dialect == "sqlite" else "t = 'string'"


def _datetime_comparison(operator: str, dialect: _sql.Dialect) -> str:
    if dialect == "sqlite":
        return (
            "CASE WHEN ontary_instant(v) IS NULL OR ontary_instant(o) IS NULL THEN TRUE "
            "WHEN substr(ontary_instant(v), 1, 1) <> substr(ontary_instant(o), 1, 1) "
            f"THEN FALSE ELSE ontary_instant(v) {operator} ontary_instant(o) END"
        )
    # CASE protects casts, including when the optimizer reorders WHERE terms.
    # Both sides explicitly select timestamp vs timestamptz; naive values never
    # acquire the connection's TimeZone.
    aware_v = "v ~ '(Z|[+-][0-9]{2}:[0-9]{2})$'"
    aware_o = "o ~ '(Z|[+-][0-9]{2}:[0-9]{2})$'"
    valid_v = (
        f"pg_input_is_valid(v, CASE WHEN {aware_v} THEN 'timestamp with time zone' "
        "ELSE 'timestamp without time zone' END)"
    )
    valid_o = (
        f"pg_input_is_valid(o, CASE WHEN {aware_o} THEN 'timestamp with time zone' "
        "ELSE 'timestamp without time zone' END)"
    )
    return (
        "CASE WHEN NOT (v ~ {p} AND o ~ {p}) THEN TRUE "
        f"WHEN NOT ({valid_v} AND {valid_o}) THEN TRUE "
        f"WHEN ({aware_v}) <> ({aware_o}) THEN FALSE "
        f"WHEN {aware_v} THEN v::timestamptz {operator} o::timestamptz "
        f"ELSE v::timestamp {operator} o::timestamp END"
    )


def _comparison(term: WhereTerm, kind: str, dialect: _sql.Dialect) -> tuple[str, list[Any]]:
    operator = _OPERATORS[term.op]
    if isinstance(term.operand, float) and math.isnan(term.operand):
        return ("TRUE" if term.op == "ne" else "FALSE"), []
    if kind == "datetime" and term.op not in {"eq", "ne"}:
        return _datetime_comparison(operator, dialect), ([_ISO_INSTANT] * 2 if dialect == "postgres" else [])
    if kind == "date" and term.op not in {"eq", "ne"}:
        if dialect == "sqlite":
            return (
                "CASE WHEN v NOT GLOB "
                "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' THEN TRUE "
                f"ELSE v COLLATE BINARY {operator} o END", []
            )
        return (
            "CASE WHEN v !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN TRUE "
            f'ELSE v COLLATE "C" {operator} o COLLATE "C" END', []
        )
    if kind in {"int", "float", "bool"}:
        if dialect == "sqlite":
            integer_guard = "t = 'integer'"
            if isinstance(term.operand, int) and not -(2**63) <= term.operand < 2**63:
                # Binding this operand requires REAL conversion too. Large
                # REAL values can then lose an ordering distinction as well.
                integer_guard = "TRUE"
            return (
                f"CASE WHEN {integer_guard} AND (v > 9007199254740992 "
                "OR v < -9007199254740992) THEN TRUE "
                f"ELSE v {operator} o END", []
            )
        if kind == "bool":
            return f"(CASE WHEN v = 'true' THEN 1 ELSE 0 END) {operator} o", []
        # JSON's round-trip decimal spelling can differ from the float's
        # exact value in Python's float/int comparisons. Outside the exact
        # integer domain, leave both directions and equality to Python.
        return (
            "CASE WHEN abs(v::numeric) > 9007199254740992 "
            "OR abs(o) > 9007199254740992 THEN TRUE "
            f"ELSE v::numeric {operator} o END", []
        )
    collate = "COLLATE BINARY" if dialect == "sqlite" else 'COLLATE "C"'
    return f"v {collate} {operator} o {collate}", []


def _compile_term(term: WhereTerm, dialect: _sql.Dialect) -> tuple[str, list[Any]]:
    if term.op == "and":
        return _combine(term.operand, dialect, " AND ")
    # Let Python judge strings outside the drivers' bind domain, without
    # binding any field or operand from the affected clause.
    if _compiles_to_true(term):
        return "TRUE", []
    if term.op == "in":
        return _combine(
            (WhereTerm(term.field, term.prop_type, "eq", item) for item in term.operand),
            dialect, " OR ",
        )
    if term.op not in {*_OPERATORS, "contains"}:
        raise ValueError(f"unknown filter operator: {term.op}")
    kind = _scalar_type(term)
    guard = _type_guard(kind, dialect)
    null = "t IS NULL OR t = 'null'"
    extra_params: list[Any] = []
    if term.operand is None:
        # json accepts every non-null JSON shape, so its null probe is exact.
        wrong = "FALSE" if term.prop_type == "json" else f"NOT ({guard})"
        predicate = f"({null}) OR ({wrong})" if term.op == "eq" else f"NOT ({null})"
    else:
        if term.op == "contains":
            comparison = "instr(v, o) > 0" if dialect == "sqlite" else "strpos(v, o) > 0"
        else:
            comparison, extra_params = _comparison(term, kind, dialect)
        null_result = "TRUE" if term.op == "ne" else "FALSE"
        predicate = (
            f"CASE WHEN {null} THEN {null_result} "
            f"WHEN NOT ({guard}) THEN TRUE ELSE {comparison} END"
        )
    if dialect == "sqlite":
        field = '$.' + json.dumps(term.field, ensure_ascii=False)
        source = "SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o"
        operand = int(term.operand) if isinstance(term.operand, bool) else term.operand
        # sqlite3 cannot bind arbitrary-size Python ints. Passing their decimal
        # spelling with numeric affinity preserves comparison for smaller rows;
        # stored integers outside the exact domain pass through above.
        if isinstance(operand, int) and not -(2**63) <= operand < 2**63:
            operand = str(operand)
            source = source.replace("{p} AS o", "CAST({p} AS NUMERIC) AS o")
    else:
        field = term.field
        operand = term.operand
        cast = "numeric" if kind in {"int", "float", "bool"} and operand is not None else "text"
        if isinstance(operand, bool):
            operand = int(operand)
        elif isinstance(operand, float):
            # Bind the round-trip spelling rather than float8 -> numeric,
            # which rounds to fewer significant digits on Postgres.
            operand = str(operand)
        source = (
            "SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, "
            f"payload::jsonb ->> {{p}} AS v, {{p}}::{cast} AS o"
        )
    fragment = f"(SELECT {predicate} FROM ({source}) AS filter_value)"
    if dialect == "sqlite":
        # json.dumps permits NaN and infinities, which SQLite can interpret
        # differently from Python. Guard each term with strict JSON validity.
        fragment = f"CASE WHEN json_valid(payload) THEN {fragment} ELSE TRUE END"
    else:
        # json.dumps can produce TEXT payloads (e.g. escaped U+0000) that
        # jsonb rejects. CASE guards the whole correlated subquery per row,
        # regardless of object-type/tenant predicate evaluation order.
        fragment = (
            "CASE WHEN pg_input_is_valid(payload, 'jsonb') "
            f"THEN {fragment} ELSE TRUE END"
        )
    return fragment, [*extra_params, field, field, operand]
