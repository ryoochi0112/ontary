"""Canonical staging schema base + connector contract (spec
connector-framework.md §5 Approach `canonical.py`, §3 AC1-AC2, §7 edge cases).

Every connector writes to a source-agnostic canonical staging schema --
never to the ontology directly -- and a mapper (a separate module) binds
canonical models, never a vendor's field names, to the ontology. That is the
vendor-independence mechanism: swap the connector, keep the same canonical
shape, and the same mapper produces the same ontology shape.

Every canonical record carries a `SourceLineage` stamp (source system, optional
source id, extraction time) so a mapper can trace an ontology object/link
back to its origin and derive an idempotent ontology id from
`(source_system, canonical key)`.

Unlike the DSO prototype this module generalizes from, no domain vocabulary
lives here: authors subclass `CanonicalRecord` to declare their own canonical
models, and `CanonicalBatch` maps an author-chosen entity name to a list of
records rather than hardcoding fixed entity slots.

IMPORTANT: this module -- and this subpackage in general -- MUST NOT import
anything from the ontology layer (`ontary.meta`, `ontary.store`,
`ontary.security`, `ontary.actions`, `ontary.functions`, `ontary.query`).
The canonical schema is source-agnostic *and* ontology-agnostic.
"""

from __future__ import annotations

from datetime import datetime
from inspect import isabstract
from typing import Any, Protocol

from pydantic import BaseModel, Field


class SourceLineage(BaseModel):
    """Where a canonical record came from and when it was extracted.

    Required (never optional) on every canonical record -- see
    `test_connect_canonical.py` for the "SourceLineage required" regression
    tests (spec AC1).
    """

    source_system: str
    source_id: str | None = None
    extracted_at: datetime


class CanonicalRecord(BaseModel):
    """Base class every author-declared canonical model subclasses.

    The required `lineage` field is the only thing the SDK imposes -- it
    enforces AC1 (a canonical record without lineage is rejected at model
    construction) while leaving every other field to the author's subclass.
    """

    lineage: SourceLineage


# Raw, source-shaped rows keyed by table/sheet name -- e.g.
# {"employees": [...], "groups": [...], ...}. Whatever a connector's
# `extract()` returns and whatever its `transform()` consumes; the author's
# `CanonicalRecord` subclasses are the only thing that crosses the connector
# boundary into a `CanonicalBatch`.
RawTables = dict[str, list[dict[str, Any]]]


class CanonicalBatch(BaseModel):
    """Everything one connector `transform()` call produced, keyed by an
    author-chosen entity name.

    Partial batches are the normal case (spec AC2, §7 "source lacks
    entities"): any subset of entities may be present, and `get()` returns
    an empty list rather than raising for an entity a batch omits.
    """

    entities: dict[str, list[CanonicalRecord]] = Field(default_factory=dict)

    def get(self, entity: str) -> list[CanonicalRecord]:
        return self.entities.get(entity, [])


class SourceConnector(Protocol):
    """The contract every connector implements (spec §5 Approach).

    `extract()` is source-specific and side-effecting (network/DB reads);
    `make verify` never calls it. The default here raises so an incomplete
    connector fails loudly rather than silently returning nothing.

    `transform()` is pure -- source-shaped raw rows in, a `CanonicalBatch`
    out -- and is exactly what gets unit-tested against fixtures.
    """

    name: str

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not isabstract(cls) and not hasattr(cls, "name"):
            raise TypeError(
                f"concrete connector {cls.__name__!r} must define class attribute 'name'"
            )

    def extract(self) -> RawTables:
        raise NotImplementedError

    def transform(self, raw: RawTables) -> CanonicalBatch:
        raise NotImplementedError
