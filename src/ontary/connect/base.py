"""`BaseConnector` + shared date/datetime coercion utilities (spec
connector-framework.md AC11, ontary task T10).

Every example connector hand-rolled the same `_lineage` helper (stamp
`source_system`/`source_id`/`extracted_at` on a `SourceLineage`) and the same
`_parse_datetime`/`_parse_optional_datetime`/`_parse_date`/`_parse_optional_date`
quartet (accept an already-parsed value or an ISO-8601 string, never read the
wall clock). This module hoists both into the shared `ontary.connect`
surface so a connector author only writes `extract()` + `transform()`.
"""

from __future__ import annotations

from datetime import date, datetime
from inspect import isabstract
from typing import Any

from ontary.connect.canonical import SourceLineage


def to_datetime(value: Any) -> datetime:
    """Accept either an already-parsed `datetime` or an ISO-8601 string;
    never reads the wall clock."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise TypeError(f"expected a datetime or ISO-8601 string, got {value!r}")


def to_optional_datetime(value: Any) -> datetime | None:
    return None if value is None else to_datetime(value)


def to_date(value: Any) -> date:
    """Accept an already-parsed `date`/`datetime` (downcast to `date`) or an
    ISO-8601 string; never reads the wall clock."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"expected a date or ISO-8601 string, got {value!r}")


def to_optional_date(value: Any) -> date | None:
    return None if value is None else to_date(value)


class BaseConnector:
    """Shared `SourceConnector` scaffolding: a connector author subclasses
    this, sets `name`, and writes `extract()` + `transform()` -- `lineage()`
    is provided.

    `extract()` deliberately raises `NotImplementedError` here too (the
    `SourceConnector` protocol's contract: an incomplete connector fails
    loudly rather than silently returning nothing); a subclass overrides it
    with the real extraction seam.
    """

    name: str

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not isabstract(cls) and not hasattr(cls, "name"):
            raise TypeError(
                f"concrete connector {cls.__name__!r} must define class attribute 'name'"
            )

    def __init__(self, extracted_at: datetime) -> None:
        self.extracted_at = extracted_at

    def lineage(self, source_id: str) -> SourceLineage:
        return SourceLineage(
            source_system=self.name,
            source_id=source_id,
            extracted_at=self.extracted_at,
        )

    def extract(self) -> Any:
        raise NotImplementedError


__all__ = [
    "BaseConnector",
    "to_datetime",
    "to_optional_datetime",
    "to_date",
    "to_optional_date",
]
