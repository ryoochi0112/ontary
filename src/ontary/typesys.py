"""Single source of truth for `PropertyType` -> accepted-Python-type scalar
validation (spec `m35-sdk-refactor` §6 AC6).

This is a leaf module: it imports nothing but stdlib/pydantic, so `meta`,
`ingest`, and `actions` can all depend on it with no cycle risk. Previously
each of those three modules carried its own copy of this table (and its own
copy of the bool-is-not-int / ISO-8601-datetime checks); this module is now
the only place either lives.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

PropertyType = Literal[
    "str", "int", "float", "bool", "date", "datetime", "json"
]

PYTHON_TYPES: dict[PropertyType, tuple[type, ...]] = {
    "str": (str,),
    "int": (int,),
    "float": (float, int),
    "bool": (bool,),
    "date": (date, str),
    "datetime": (datetime, str),
    "json": (dict, list, str, int, float, bool),
}


def _date_string_violation(value: str) -> str | None:
    """Why ``value`` is not the date storage form, or ``None``.

    ``date.fromisoformat`` also accepts compact ISO forms such as
    ``YYYYMMDD`` on supported Python versions. The store contract is narrower:
    exactly ``YYYY-MM-DD``, so the parsed value must serialize back byte-for-
    byte to the input.
    """
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return f"is not a valid ISO-8601 date (YYYY-MM-DD): {value!r}"
    if parsed.isoformat() != value:
        return f"is not a valid ISO-8601 date (YYYY-MM-DD): {value!r}"
    return None


def _datetime_string_violation(value: str) -> str | None:
    """Why ``value`` is not a datetime storage form, or ``None``.

    ``datetime.fromisoformat`` accepts a date-only string as a naive midnight,
    so ``"2026-02-15"`` would pass as a datetime. The contract is narrower: a
    declared datetime carries a time component. A date-only value stored under
    a datetime property is naive by accident, and as a `where` comparison
    operand it silently never matches an offset-aware column; both are
    refused here, at the one check every write and read path shares. Naive
    and offset-aware datetimes are both accepted; an ontology that mixes
    them in one property gets no defined order between the two kinds.
    """
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return f"is not a valid ISO-8601 datetime: {value!r}"
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    # A string `date.fromisoformat` accepts has no time component.
    return (
        f"is not a valid ISO-8601 datetime (a time component is required): {value!r}"
    )


def _naive_datetime_violation(value: datetime) -> str | None:
    """Why ``value`` is not an acceptable ``datetime`` object, or ``None``.

    A naive ``datetime`` names no instant, so it can never be compared with
    the offset-aware values the property otherwise holds. The refusal is
    deliberately narrower than the string rule (a naive ISO *string* is
    still accepted, unchanged): a caller holding a real ``datetime`` is one
    ``tzinfo=`` away from saying which instant they mean.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        return (
            "expected an offset-aware datetime (tzinfo set), got a naive one: "
            f"{value.isoformat()!r}"
        )
    return None


def _to_storage_scalar(value: Any, prop_type: PropertyType) -> Any:
    """Return the JSON-safe scalar representation used by every store.

    A ``date``/``datetime`` object becomes its own ``isoformat()`` spelling,
    offset preserved -- the store persists exactly what the caller said, so
    ``eq`` on the string surface keeps matching the spelling that was
    written (#38).
    """
    if prop_type == "datetime" and isinstance(value, datetime):
        return value.isoformat()
    if (
        prop_type == "date"
        and isinstance(value, date)
        and not isinstance(value, datetime)
    ):
        return value.isoformat()
    return value


def choice_value(value: Any, choices: tuple[str, ...] | None) -> Any:
    """The plain value a choice declaration stores for ``value`` (#42).

    An ``Enum`` member is unwrapped to its ``.value`` **only** when the
    target declares ``choices``; a declaration without ``choices`` sees the
    member unchanged, so ``validate_scalar`` refuses it exactly as before.
    Every other value passes through untouched. This is the single place an
    ``Enum`` member becomes a stored value: every entry path (store insert
    and update, ingest, a where operand, dict-form action params, owned
    defaults) applies it once, BEFORE validation and before the object id is
    read, so the value that is validated, the value that is persisted, and
    the id derived from it are the same value.
    """
    if choices is not None and isinstance(value, Enum):
        return value.value
    return value


def normalize_choice_values(
    payload: Mapping[str, Any],
    choices_by_name: Mapping[str, tuple[str, ...] | None],
) -> dict[str, Any]:
    """Copy ``payload`` with `choice_value` applied per declared name."""
    return {
        name: choice_value(value, choices_by_name.get(name))
        for name, value in payload.items()
    }


def _storage_scalar_violation(
    value: Any,
    prop_type: PropertyType,
    choices: tuple[str, ...] | None = None,
) -> str | None:
    """Validate a scalar already read from durable JSON storage.

    Input accepts a real ``date`` for convenience, but hydration must only see
    the promised storage representation. Keeping this separate prevents
    Pydantic from accepting a coercible datetime-shaped string as a date.
    """
    if prop_type == "date":
        if not isinstance(value, str):
            return (
                "expected stored ISO date string (YYYY-MM-DD), got "
                f"{type(value).__name__}"
            )
        date_violation = _date_string_violation(value)
        if date_violation is not None:
            return date_violation
    if choices is not None and value not in choices:
        return f"expected one of {list(choices)!r}, got {value!r}"
    return None


def validate_scalar(
    value: Any,
    prop_type: PropertyType,
    choices: tuple[str, ...] | None = None,
) -> str | None:
    """Return an error message if `value` doesn't match `prop_type`, else None.

    Callers that need the mismatch attributed to a particular property/param
    name should prepend that context to the returned message themselves;
    this helper stays name-agnostic so it's reusable across meta/ingest/
    actions call sites.

    Checks, in order:
    - `bool` is never accepted for a non-`"bool"` declared type (Python's
      `bool` is an `int` subclass, so this must be checked before the
      generic `isinstance` check below).
    - the value's type is one of `PYTHON_TYPES[prop_type]`.
    - a declared ``"date"`` value is either a real ``datetime.date`` (but
      never its ``datetime.datetime`` subclass) or the exact ISO storage form
      ``YYYY-MM-DD``.
    - a declared ``"datetime"`` value is either an offset-aware
      ``datetime.datetime`` (a naive one is refused with a message that says
      so) or a `str` that parses as ISO-8601 and carries a time component (a
      date-only string is a `date`).
    """
    expected_types = PYTHON_TYPES[prop_type]
    if isinstance(value, bool) and prop_type != "bool":
        return f"expected type {prop_type!r}, got bool"
    if prop_type == "date" and isinstance(value, datetime):
        return "expected type 'date', got datetime"
    if not isinstance(value, expected_types):
        return f"expected type {prop_type!r}, got {type(value).__name__}"
    if prop_type == "date" and isinstance(value, str):
        return _date_string_violation(value)
    if prop_type == "datetime" and isinstance(value, datetime):
        return _naive_datetime_violation(value)
    if prop_type == "datetime" and isinstance(value, str):
        violation = _datetime_string_violation(value)
        if violation is not None:
            return violation
    if choices is not None and value not in choices:
        return f"expected one of {list(choices)!r}, got {value!r}"
    return None
