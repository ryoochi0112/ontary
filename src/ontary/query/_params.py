"""Read-parameter validation: fields, order_by, group_by."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from ontary.errors import ValidationFailed, VisibilityError
from ontary.meta import OntologyRegistry
from ontary.query._where import _NormalizedWhere, _validate_where_keys
from ontary.security import Consumer

_NUMERIC_PROPERTY_TYPES = {"int", "float"}
_GROUPABLE_PROPERTY_TYPES = {"str", "int", "float", "bool", "date", "datetime"}
"""Declared types whose values may be a `group_by` key.

An ALLOW-list, not a `{"json"}` deny-list, so a `PropertyType` added later
fails closed here instead of reaching `dict.setdefault` and raising a bare
`TypeError` from inside the grouping loop. `json` is the only current member
that can hold a `dict`/`list`, and so the only one excluded.
"""

_OrderSpec = tuple[str, bool]


class _HiddenFields(Protocol):
    """The `GuardedQuery._hidden_fields` call shape, passed in as a bound method."""

    def __call__(
        self,
        consumer: Consumer,
        obj_type: str,
        *,
        disclosure: Literal["supplied", "learned"],
    ) -> set[str]: ...


def _validate_group_by(
    registry: OntologyRegistry, obj_type: str, group_by: str
) -> None:
    """Refuse a `group_by` that is not a declared, groupable property.

    Existence first, and with the same `UNKNOWN_FIELD` code and message
    shape `order_by` already uses on this surface: an unknown name must
    not be answered with a number, and the three identifier parameters of
    one read must not disagree about what an unknown name is.

    Then the declared TYPE, checked before a single row is read. Doing it
    on the declaration rather than on the stored values is the point: a
    `json` property whose rows all happen to hold scalars would otherwise
    group fine until the first `dict` a connector delivered, which is the
    same data-dependent silence the bare `TypeError` had. A hidden
    `group_by` is deliberately NOT decided here -- it is a declared field,
    so it passes existence and reaches `_aggregate`'s visibility gate
    unchanged; answering `UNKNOWN_FIELD` for it would both leak "no such
    field" about a field that exists and make that gate unreachable.
    """
    obj_def = registry.get_object_type(obj_type)
    declared = {prop.name: prop.type for prop in obj_def.properties}
    if group_by not in declared:
        raise ValidationFailed(
            f"{obj_type}: group_by names unknown field(s) [{group_by!r}]",
            code="UNKNOWN_FIELD",
        )
    group_by_type = declared[group_by]
    if group_by_type not in _GROUPABLE_PROPERTY_TYPES:
        raise ValidationFailed(
            f"{obj_type}.{group_by} is declared {group_by_type!r}, which "
            "cannot be a group key -- its values need not be hashable, so "
            "grouping by it fails on the data rather than on the "
            "declaration",
            code="INVALID_GROUP_BY",
        )


def _validate_read_fields(
    registry: OntologyRegistry,
    hidden_fields: _HiddenFields,
    consumer: Consumer,
    obj_type: str,
    where: _NormalizedWhere | None,
    where_fields: tuple[tuple[str, Literal["supplied", "learned"]], ...],
    order_by: Any,
) -> _OrderSpec | None:
    """Run the one field-gate path shared by filtered/ordered reads.

    Unknown and hidden fields are rejected before where-operator
    validation. The order_by value deliberately enters this same gate
    before its direction is checked, so an unknown or hidden field cannot
    be used to probe which ordering forms the ontology accepts.
    """
    _validate_where_keys(registry, obj_type, where)

    order_shape: tuple[str, object] | None
    if order_by is None:
        order_shape = None
    elif isinstance(order_by, str):
        order_shape = (order_by, "asc")
    elif (
        isinstance(order_by, (tuple, list))
        and len(order_by) == 2
        and isinstance(order_by[0], str)
    ):
        order_shape = (order_by[0], order_by[1])
    else:
        raise ValidationFailed(
            "get_objects order_by must be a field name or a "
            "(field, 'asc'|'desc') pair",
            code="INVALID_PARAMS",
        )

    if order_shape is not None:
        order_field, _direction = order_shape
        obj_def = registry.get_object_type(obj_type)
        declared = {prop.name for prop in obj_def.properties}
        if order_field not in declared:
            raise ValidationFailed(
                f"{obj_type}: order_by names unknown field(s) "
                f"[{order_field!r}]",
                code="UNKNOWN_FIELD",
            )

    if where_fields:
        # The scope-key exemption belongs only to predicates whose shape
        # supplies explicit values. Learning-shaped and malformed clauses
        # consult the un-exempted hidden set before operator validation.
        denied_keys = {
            field
            for field, field_disclosure in where_fields
            if field
            in hidden_fields(
                consumer,
                obj_type,
                disclosure=field_disclosure,
            )
        }
        if denied_keys:
            raise VisibilityError(
                f"{consumer.actor_id!r} cannot filter {obj_type} on "
                f"hidden field(s) {sorted(denied_keys)!r}",
                code="VISIBILITY_DENIED",
            )

    if order_shape is None:
        return None

    order_field, direction = order_shape
    # Ordering discloses rank, so even a scope-routing property is a
    # learned value here and must consult the un-exempted hidden set.
    order_hidden = hidden_fields(
        consumer, obj_type, disclosure="learned"
    )
    if order_field in order_hidden:
        raise VisibilityError(
            f"{consumer.actor_id!r} cannot order {obj_type} on "
            f"hidden field(s) [{order_field!r}]",
            code="VISIBILITY_DENIED",
        )
    if _property_type(registry, obj_type, order_field) == "struct":
        raise ValidationFailed(
            f"order_by is not supported on struct property {order_field!r}",
            code="INVALID_PARAMS",
        )
    if direction not in {"asc", "desc"}:
        raise ValidationFailed(
            "get_objects order_by direction must be 'asc' or 'desc', "
            f"got {direction!r}",
            code="INVALID_PARAMS",
        )
    return order_field, direction == "desc"


def _property_type(
    registry: OntologyRegistry, obj_type: str, field_name: str
) -> str | None:
    """The declared `PropertyType` of `field_name` on `obj_type`, or
    `None` if `obj_type` is unregistered or declares no property by
    that name.

    The `None` answer is a refusal for the caller to act on, not a
    permission to proceed. This docstring used to say an unrecognized
    `value_field` was "left to the existing row-based aggregation, which
    simply finds no matching keys", and `_aggregate` skipped its type gate
    on that basis. Both were wrong: `meta.declared_shape_violation` allows
    undeclared payload keys deliberately, so a row can and does carry a
    key no property declares, and reducing over one released an ungoverned
    number (or let `float()` raise on the data). See `_aggregate`.
    """
    try:
        obj_def = registry.get_object_type(obj_type)
    except ValidationFailed:
        return None
    for prop in obj_def.properties:
        if prop.name == field_name:
            return prop.type
    return None
