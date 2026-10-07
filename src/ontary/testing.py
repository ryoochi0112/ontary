"""Small, deterministic helpers for testing an ontology application.

The helpers compose the public ``ontary`` API, except the private scenario
module, which reuses engine conversions for typed and stored values. SDK-user
test suites need no engine implementation imports or test-framework-specific
fixture machinery.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from ontary import Consumer
from ontary._scenario import FixedClock, Scenario, SequentialIds, make_store, scenario

__all__ = [
    "FixedClock",
    "Scenario",
    "SequentialIds",
    "consumer",
    "make_store",
    "raises_code",
    "scenario",
]


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
    `OntaryError` subclass, including `ontary.ingest.IngestError`, as well as
    structurally compatible author-defined coded exceptions. An exception
    with no ``.code`` propagates unchanged. The exception message is
    intentionally ignored; callers should assert the stable machine-readable
    code rather than prose.
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
