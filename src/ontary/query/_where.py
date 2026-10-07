"""`where` normalization, evaluation, and compilation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any, NoReturn

from ontary.errors import ValidationFailed
from ontary.meta import OntologyRegistry
from ontary.typesys import (
    PropertyType,
    _to_storage_scalar,
    choice_value,
    validate_scalar,
)

_WHERE_OPERATORS = frozenset({"gt", "gte", "lt", "lte", "in", "ne", "contains"})
_WHERE_COMPARISON_OPERATORS = frozenset({"gt", "gte", "lt", "lte"})
_COMPARABLE_PROPERTY_TYPES = frozenset({"int", "float", "date", "datetime"})
_CONJUNCTION = "and"
"""Internal clause operator for a multi-operator condition mapping.

Never a caller-facing spelling: `_compile_where_clause` emits it only for an
``"operators"`` condition, whose operand is the tuple of compiled sub-clauses.
It is not in `_WHERE_OPERATORS`, so ``{"and": ...}`` from a caller is still
`UNKNOWN_OPERATOR`.
"""

_WhereClause = tuple[str, PropertyType, str, Any]
_WhereMatcher = Callable[[dict[str, Any]], bool]


_NormalizedCondition = tuple[str, str, Any]
"""One `where` condition, snapshotted as ``(kind, operator, operand)``.

``kind`` is ``"bare"`` (the condition was not a mapping, so it is equality
sugar), ``"operator"`` (a one-entry mapping, whose key is NOT yet known to
be a real operator), ``"operators"`` (a mapping with several entries, whose
``operand`` is the tuple of ``(key, operand)`` pairs in caller order -- the
conjunction of those clauses, so ``{"gte": a, "lt": b}`` is a range), or
``"malformed"`` (an empty mapping, or one with a non-``str`` key, whose
``operand`` carries its key tuple for the refusal message).
"""

_NormalizedWhere = tuple[tuple[str, _NormalizedCondition], ...]
"""A whole `where` mapping, snapshotted once and immutable thereafter."""


def _is_supplied_value(value: Any) -> bool:
    """Whether a `where` operand is a value the caller had to already hold.

    The supply-shaped exemption rests entirely on this: naming a value you
    must already possess teaches you nothing you did not bring. `None` fails
    it -- null-ness is universally available, so ``where={"person_id": None}``
    is a free per-row null probe, not a supply -- and so does any container or
    arbitrary object, which fails closed ahead of operand validation.
    """
    return isinstance(value, str | int | float | date)


def _normalize_where_condition(condition: Any) -> _NormalizedCondition:
    """Snapshot one caller-supplied condition, reading it EXACTLY once.

    The classifier and the compiler used to each re-read the caller's
    mapping. A mapping whose ``items()`` answered differently on the second
    read therefore got classified on one predicate and executed as another --
    a supply-shaped `eq` at the gate, a `contains` alphabet-walk at the
    matcher. Everything downstream consumes this immutable triple instead of
    the caller's object, so there is no second read to disagree with.
    """
    if not isinstance(condition, dict):
        return ("bare", "eq", condition)
    items = tuple(condition.items())
    if not items or not all(isinstance(operator, str) for operator, _ in items):
        return ("malformed", "", tuple(operator for operator, _ in items))
    if len(items) == 1:
        operator, operand = items[0]
        return ("operator", operator, operand)
    return ("operators", "", items)


def _require_where_mapping(where: object) -> None:
    """Refuse a `where=` that is not a mapping, before anything reads it.

    The type check has to come BEFORE the falsy short-circuit below, and that
    ordering is the whole fix. `if not where: return None` treated a falsy
    NON-mapping as "no filter", so ``where=""``/``0``/``[]``/``False``
    silently matched every visible row -- a caller who believed they were
    narrowing received the entire population, with no exception to notice. A
    truthy one reached ``.items()`` and raised a bare `AttributeError` on all
    six read surfaces. The same caller mistake was therefore loud for one
    operand and silent for the other.

    An EMPTY MAPPING keeps meaning "no filter": ``{}`` is a mapping, so it
    passes here and short-circuits below exactly as it always has.

    `INVALID_PARAMS` rather than a new code: the MCP boundary already answers
    it for this exact complaint (`_tool_optional_object` -- "tool parameter
    'where' must be an object, got str"), and `get_objects`' own `order_by`
    shape refusal already uses it for a read parameter in this file. The
    in-process refusal and the wire one now agree on the code as well as on
    the verdict.
    """
    if where is None or isinstance(where, dict):
        return
    raise ValidationFailed(
        "where= must be a mapping of field name to condition, got "
        f"{type(where).__name__}",
        code="INVALID_PARAMS",
    )


def _normalize_where(where: dict[str, Any] | None) -> _NormalizedWhere | None:
    """Snapshot a whole `where` mapping at the public boundary, once."""
    _require_where_mapping(where)
    if not where:
        return None
    return tuple(
        (field, _normalize_where_condition(condition))
        for field, condition in where.items()
    )


def _evaluate_comparison(
    actual: Any, prop_type: PropertyType, operator: str, operand: Any
) -> bool:
    if prop_type == "date":
        if not isinstance(actual, str):
            return False
        try:
            actual_value = date.fromisoformat(actual)
        except ValueError:
            return False
        if actual_value.isoformat() != actual:
            return False
        operand_value = date.fromisoformat(operand)
    elif prop_type == "datetime":
        # Compared as instants, not as strings: `2026-02-15T09:00:00+09:00`
        # and `2026-02-15T00:00:00+00:00` are the same moment. A naive
        # stored value against an aware operand (or the reverse) has no
        # defined order; Python raises `TypeError`, and the row is treated
        # as unmatched below, the same verdict a corrupt value gets.
        if not isinstance(actual, str):
            return False
        try:
            actual_value = datetime.fromisoformat(actual)
        except ValueError:
            return False
        operand_value = datetime.fromisoformat(operand)
    else:
        actual_value = actual
        operand_value = operand

    try:
        if operator == "gt":
            return actual_value > operand_value
        if operator == "gte":
            return actual_value >= operand_value
        if operator == "lt":
            return actual_value < operand_value
        return actual_value <= operand_value
    except TypeError:
        # A corrupt historical row does not satisfy a typed predicate.
        # Writes already enforce declared shape; this keeps a read from
        # surfacing an uncoded Python comparison failure if old data does not.
        return False


def _evaluate_where_clause(payload: dict[str, Any], clause: _WhereClause) -> bool:
    field, prop_type, operator, operand = clause
    if operator == _CONJUNCTION:
        return all(_evaluate_where_clause(payload, sub) for sub in operand)
    actual = payload.get(field)
    if operator == "eq":
        return bool(actual == operand)
    if operator == "ne":
        return bool(actual != operand)
    if operator == "in":
        return bool(actual in operand)
    if operator == "contains":
        return bool(isinstance(actual, str) and operand in actual)
    return _evaluate_comparison(actual, prop_type, operator, operand)


def _evaluate_where(
    payload: dict[str, Any], clauses: tuple[_WhereClause, ...]
) -> bool:
    """Evaluate the normalized where clauses against one stored payload.

    This is deliberately the only operator evaluator. Typed and string
    client reads, aggregate selection, BoundQuery reads, and MCP reads all
    converge on ``GuardedQuery`` before reaching this function.
    """
    return all(_evaluate_where_clause(payload, clause) for clause in clauses)


def _operator_type_mismatch(
    obj_type: str,
    field: str,
    operator: object,
    prop_type: str,
    detail: str,
) -> NoReturn:
    raise ValidationFailed(
        f"{obj_type}.{field}: operator {operator!r} is incompatible with "
        f"declared type {prop_type!r} ({detail})",
        code="OPERATOR_TYPE_MISMATCH",
    )


def _unknown_operator(obj_type: str, field: str, operator: object) -> NoReturn:
    raise ValidationFailed(
        f"{obj_type}.{field}: unknown where operator {operator!r}",
        code="UNKNOWN_OPERATOR",
    )


def _validate_where_keys(
    registry: OntologyRegistry, obj_type: str, where: _NormalizedWhere | None
) -> None:
    """Refuse unknown `where=` keys for a string-form object name.

    The registry descriptor is the string surface's equivalent of the
    typed model's `model_fields`. Lineage metadata is not in that payload
    schema, so keys such as `source_system` keep the existing
    `UNKNOWN_FIELD` rejection shape instead of becoming filterable.
    Unknown object names use the registry's existing
    `UNKNOWN_OBJECT_TYPE` refusal before payload validation.
    """
    if not where:
        return
    obj_def = registry.get_object_type(obj_type)
    unknown = {field for field, _ in where} - {
        prop.name for prop in obj_def.properties
    }
    if unknown:
        raise ValidationFailed(
            f"{obj_type}: where= names unknown field(s) "
            f"{sorted(unknown)!r}",
            code="UNKNOWN_FIELD",
        )


def _normalize_where_operand(
    operator: object, operand: Any, prop_type: PropertyType
) -> Any:
    if operator == "in" and isinstance(operand, list):
        return [
            _to_storage_scalar(value, prop_type)
            if value is not None
            else value
            for value in operand
        ]
    return (
        _to_storage_scalar(operand, prop_type)
        if operand is not None
        else operand
    )


def _validate_where_operator(
    obj_type: str,
    field: str,
    prop_type: PropertyType,
    operator: str,
    operand: Any,
) -> None:
    if operator in _WHERE_COMPARISON_OPERATORS:
        if prop_type not in _COMPARABLE_PROPERTY_TYPES:
            _operator_type_mismatch(
                obj_type,
                field,
                operator,
                prop_type,
                "comparisons require an int, float, date, or datetime property",
            )
        _validate_where_scalar(
            obj_type, field, operator, prop_type, operand
        )
    elif operator == "in":
        if not isinstance(operand, list):
            _operator_type_mismatch(
                obj_type,
                field,
                operator,
                prop_type,
                "the operand must be a list",
            )
        for value in operand:
            _validate_where_scalar(
                obj_type, field, operator, prop_type, value
            )
    elif operator == "eq":
        # The bare form is the only equality spelling, so `eq` never
        # reaches here from `_WHERE_OPERATORS` -- `_compile_where_clause`
        # routes its `bare` branch here by hand. `None` stays the null
        # test (pinned by
        # `test_bare_null_still_filters_a_field_that_is_not_hidden`);
        # every other operand is a declared-type scalar, same as `ne`.
        if operand is not None:
            _validate_where_scalar(
                obj_type, field, operator, prop_type, operand
            )
    elif operator == "ne":
        _validate_where_scalar(
            obj_type, field, operator, prop_type, operand
        )
    else:
        if prop_type != "str":
            _operator_type_mismatch(
                obj_type,
                field,
                operator,
                prop_type,
                "contains requires a str property",
            )
        _validate_where_scalar(
            obj_type, field, operator, prop_type, operand, scalar_type="str"
        )


def _validate_where_scalar(
    obj_type: str,
    field: str,
    operator: str,
    prop_type: PropertyType,
    operand: Any,
    *,
    scalar_type: PropertyType | None = None,
) -> None:
    checked_type = prop_type if scalar_type is None else scalar_type
    mismatch = validate_scalar(operand, checked_type)
    if mismatch is not None:
        _operator_type_mismatch(obj_type, field, operator, prop_type, mismatch)


def _unwrap_choice_operand(operand: Any, choices: tuple[str, ...] | None) -> Any:
    """Apply `typesys.choice_value` to a where operand (#42): an
    ``Enum`` member is unwrapped ONLY when the field declares `choices`,
    and only unwrapped -- the operand gains no membership check here, so
    a field without `choices` still refuses the member as before."""
    if isinstance(operand, list):
        return [choice_value(value, choices) for value in operand]
    return choice_value(operand, choices)


def _compile_where_clause(
    obj_type: str,
    field: str,
    prop_type: PropertyType,
    condition: _NormalizedCondition,
    choices: tuple[str, ...] | None = None,
) -> _WhereClause:
    """Compile one already-snapshotted condition.

    Takes the same `_NormalizedCondition` the field gate classified, not
    the caller's mapping -- see `_normalize_where_condition` for why a
    second read of that mapping was a disclosure.
    """
    if prop_type == "struct":
        raise ValidationFailed(
            f"where is not supported on struct property {field!r}",
            code="OPERATOR_TYPE_MISMATCH",
        )
    kind, operator, operand = condition
    if kind in ("bare", "operator"):
        operand = _unwrap_choice_operand(operand, choices)
    if kind == "bare":
        _validate_where_operator(obj_type, field, prop_type, "eq", operand)
        return (
            field,
            prop_type,
            "eq",
            _normalize_where_operand("eq", operand, prop_type),
        )

    if kind == "malformed":
        _unknown_operator(obj_type, field, operand)
    if kind == "operators":
        # Each pair is validated as its own one-key clause, in caller
        # order, so a bad key refuses with the same code and message it
        # would get alone; the clauses are then AND-ed per row.
        return (
            field,
            prop_type,
            _CONJUNCTION,
            tuple(
                _compile_where_clause(
                    obj_type, field, prop_type, ("operator", op, sub_operand), choices
                )
                for op, sub_operand in operand
            ),
        )
    if operator not in _WHERE_OPERATORS:
        _unknown_operator(obj_type, field, operator)
    _validate_where_operator(
        obj_type, field, prop_type, operator, operand
    )
    return (
        field,
        prop_type,
        operator,
        _normalize_where_operand(operator, operand, prop_type),
    )


def _compile_where(
    registry: OntologyRegistry, obj_type: str, where: _NormalizedWhere | None
) -> _WhereMatcher | None:
    """Validate operator clauses and compile them into one row matcher.

    Callers invoke this only after the existing unknown-field and hidden-
    field gates. That ordering is intentional: a caller cannot use an
    operator error to probe a field it was not allowed to name.
    """
    if not where:
        return None
    obj_def = registry.get_object_type(obj_type)
    prop_types = {prop.name: prop.type for prop in obj_def.properties}
    prop_choices = {prop.name: prop.choices for prop in obj_def.properties}
    clauses = [
        _compile_where_clause(
            obj_type, field, prop_types[field], condition, prop_choices[field]
        )
        for field, condition in where
    ]

    frozen_clauses = tuple(clauses)
    return lambda payload: _evaluate_where(payload, frozen_clauses)
