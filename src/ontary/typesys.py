"""Single source of truth for `PropertyType` -> accepted-Python-type scalar
validation (spec `m35-sdk-refactor` §6 AC6).

This is a leaf module: it imports nothing but stdlib/pydantic, so `meta`,
`ingest`, and `actions` can all depend on it with no cycle risk. Previously
each of those three modules carried its own copy of this table (and its own
copy of the bool-is-not-int / ISO-8601-datetime checks); this module is now
the only place either lives.
"""

from __future__ import annotations

from datetime import date, datetime
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
    "datetime": (str,),
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


def _to_storage_scalar(value: Any, prop_type: PropertyType) -> Any:
    """Return the JSON-safe scalar representation used by every store."""
    if (
        prop_type == "date"
        and isinstance(value, date)
        and not isinstance(value, datetime)
    ):
        return value.isoformat()
    return value


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
    - a declared `"datetime"` value (always a `str` at this layer) parses as
      ISO-8601.
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
    if prop_type == "datetime" and isinstance(value, str):
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return f"is not a valid ISO-8601 datetime: {value!r}"
    if choices is not None and value not in choices:
        return f"expected one of {list(choices)!r}, got {value!r}"
    return None
