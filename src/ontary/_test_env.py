"""Deterministic test-environment primitives shared by `ontary.testing` and
`ontary._scenario`; re-exported publicly from :mod:`ontary.testing`."""

from __future__ import annotations

from datetime import datetime

from ontary.authoring import Ontology
from ontary.store import InMemoryStore


def make_store(ontology: Ontology) -> InMemoryStore:
    """Return a fresh empty :class:`ontary.InMemoryStore` for ``ontology``."""
    return InMemoryStore(ontology.registry)


class FixedClock:
    """A callable clock that returns one fixed, timezone-aware instant.

    Tick semantics are constant: every call returns ``start`` unchanged.  A
    naive ``start`` is rejected because runtime timestamps must remain
    unambiguous; pass a datetime with a timezone, such as
    ``datetime(..., tzinfo=timezone.utc)``.
    """

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("FixedClock start must be timezone-aware")
        self._start = start

    def __call__(self) -> datetime:
        return self._start


class SequentialIds:
    """A callable ID factory yielding ``prefix-1``, ``prefix-2``, and so on."""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._next = 0

    def __call__(self) -> str:
        self._next += 1
        return f"{self._prefix}-{self._next}"
