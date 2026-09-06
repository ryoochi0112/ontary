"""Small, deterministic helpers for testing an ontology application.

The helpers deliberately compose the public ``ontary`` API.  They are useful in
SDK-user test suites without requiring imports from the engine's implementation
modules or any test-framework-specific fixture machinery.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Literal

from ontary import (
    Consumer,
    EffectMeta,
    EffectPayload,
    InMemoryStore,
    Ontology,
)

__all__ = [
    "FixedClock",
    "SequentialIds",
    "capture_effects",
    "consumer",
    "make_store",
    "raises_code",
]


class _CapturedEffects:
    """Callable dispatcher returned by :func:`capture_effects`."""

    def __init__(self) -> None:
        self.effects: list[tuple[EffectPayload, EffectMeta]] = []

    @property
    def records(self) -> list[tuple[EffectPayload, EffectMeta]]:
        """Alias for :attr:`effects` for callers that prefer that name."""
        return self.effects

    def __call__(self, payload: EffectPayload, meta: EffectMeta) -> None:
        """Record the dispatch request without performing outside I/O."""
        self.effects.append((payload, meta))


def make_store(ontology: Ontology) -> InMemoryStore:
    """Return a fresh empty :class:`ontary.InMemoryStore` for ``ontology``."""
    return InMemoryStore(ontology.registry)


def consumer(
    *,
    actor_id: str = "test-actor",
    role: str = "Member",
    scope_level: str = "org",
    scope_id: str = "org-1",
    kind: Literal["human", "ai"] = "human",
    principal: str | None = None,
) -> Consumer:
    """Build a valid :class:`ontary.Consumer` with test-friendly defaults."""
    return Consumer(
        actor_id=actor_id,
        role=role,
        scope_level=scope_level,
        scope_id=scope_id,
        kind=kind,
        principal=principal,
    )


@contextmanager
def raises_code(code: str) -> Iterator[None]:
    """Assert that the block raises an ontary error carrying ``code``.

    Matches any exception exposing a stable string ``.code`` -- every
    `OntaryError` subclass, and `ontary.ingest.IngestError`, which carries a
    catalogued code without subclassing `OntaryError`. An exception with no
    ``.code`` propagates unchanged. The exception message is intentionally
    ignored; callers should assert the stable machine-readable code rather
    than prose.
    """
    try:
        yield
    except Exception as exc:
        exc_code = getattr(exc, "code", None)
        if not isinstance(exc_code, str):
            raise
        if exc_code != code:
            raise AssertionError(
                f"expected ontary error code {code!r}, got {exc_code!r}"
            ) from exc
    else:
        raise AssertionError(f"expected ontary error code {code!r}, but no error raised")


def capture_effects() -> _CapturedEffects:
    """Return an effect dispatcher whose calls are kept in ``.effects``.

    Each item is ``(payload, meta)``.  The list belongs to this returned
    dispatcher, so separate test runs do not share mutable state.
    """
    return _CapturedEffects()


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
