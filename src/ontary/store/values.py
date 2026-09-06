"""Store-layer value models and constants shared by every backend.

Moved verbatim from the original `ontary/store.py` (B2 of the staged
refactor). `_utcnow_iso` is the single canonical copy -- the per-backend
duplicates are gone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict

DEFAULT_TENANT = "default"
"""The tenant a store uses when none is named (M8b).

Every row carries a tenant, including in a deployment that has only one -- a
nullable tenant would mean "belongs to everybody", which is the wrong default for
a column whose entire job is isolation. `"default"` is also what the v7 -> v8
migration backfills, because rows written before tenancy existed genuinely belong
to the single implicit tenant that wrote them.
"""


DEFAULT_BATCH = 500
"""Default page size for `Store.read_page` -- a sane batch to fetch per
round-trip, NOT a cap on how much a caller can ultimately read (repeated
`read_page` calls, or `read_all`, still walk every row). Callers needing a
different tradeoff between round-trips and per-call memory pass `batch=`
explicitly; 500 is a reasonable default for typical row sizes."""


class Source(BaseModel):
    """Lineage stamp: where a written object/version came from.

    `extracted_at` is the upstream extraction/observation timestamp (ISO-8601
    string, matching the store's own convention of persisting datetimes as
    ISO strings rather than native `datetime` columns) -- distinct from the
    store's own `valid_from`, which is when *this row* was written here.
    Optional because not every writer (e.g. an action handler minting a
    brand-new object) has an upstream extraction time to report.
    """

    source_system: str
    source_id: str | None = None
    extracted_at: str | None = None


def _utcnow_iso() -> str:
    # `timespec` pinned: a bare `isoformat()` OMITS the fractional part when
    # the clock lands on an exact second, so the same instant reaches a TEXT
    # lineage column in two widths and any comparison between them depends on
    # the collation. This keeps every NEW row 32 characters; rows already
    # written in the short spelling are handled by `_sql._COLLATE_BINARY`.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Lineage(BaseModel):
    """Where a stored object's CURRENT row came from and when it became
    current -- the non-payload half of a `StoredObject` (spec m35-sdk-
    refactor §6 "Typed read model"). Frozen: a `StoredObject` a consumer
    receives is a value, never a handle back into the store's own rows.

    Field set is exactly what `ObjectStore._row_to_dict`/`InMemoryStore.
    _row_to_dict` used to smuggle as `_`-prefixed payload keys, plus
    `object_id` (the object's own id, not previously duplicated as a
    lineage key since it was already present under its own property name in
    the payload -- surfaced here too so a caller never has to guess the
    primary-key property name to find it).

    Deliberately carries no store-internal row identity (spec pagination-
    hardening §5's amended decision): `read_page` ORDERS by the store's own
    physical row identity, but never returns that identity itself -- not
    even here, out-of-band -- a `Field(exclude=True)` would hide it from
    `model_dump()` but not from the Python attribute itself, and a narrow
    consumer reading a dense per-row counter could still infer how many
    rows its scope hid (or another tenant's write volume) by gap
    arithmetic. What DOES travel out-of-band, on `PagedRow.key`, is an
    opaque random per-row PAGE TOKEN (spec §5's T2 amendment) -- a value
    with no arithmetic relationship to row identity at all, so it is safe
    even off the model, and safe for a consumer to hold. `Lineage` therefore
    carries nothing sensitivity-classified; it is never redacted (see
    `GuardedQuery._redact`).
    """

    model_config = ConfigDict(frozen=True)

    object_type: str
    object_id: str
    valid_from: str
    valid_to: str | None
    source_system: str
    source_id: str | None
    extracted_at: str | None


class StoredObject(BaseModel):
    """A read result split into `payload` (the object's own declared
    properties) and `lineage` (storage/provenance metadata) -- the typed
    replacement for the old bare dict with `_`-prefixed lineage keys mixed
    into it (spec m35-sdk-refactor §6 AC8). Built exactly once, in `Store.
    read_current`/`read_all` (the one place a raw row becomes a Python
    value), so no other module ever re-invents the payload/lineage split.

    Frozen: consumers (via `GuardedQuery`) only ever see a redacted COPY --
    never the store's own row -- so there is no in-place-mutation path back
    into stored data.
    """

    model_config = ConfigDict(frozen=True)

    payload: dict[str, Any]
    lineage: Lineage


class PagedRow(BaseModel):
    """One `read_page` result row paired with ITS OWN cursor key --
    engine-internal only (never exported from `ontary`'s front door, never
    returned to a consumer): `StoredObject`/`Lineage` still carry no row
    identity (AC8), so the key has to travel alongside the row instead.

    `key` is a random per-row PAGE TOKEN (spec §5's T2 amendment), NOT the
    row identity itself: the row identity is still what ordering/pagination
    key off internally (`read_page` resolves a token back to it via one
    indexed lookup), but a caller who receives consecutive tokens has no
    arithmetic path from them back to how many rows separate the two
    underlying row identities -- differences and magnitudes carry no
    information, unlike the row id string this replaced. (An earlier T2
    revision emitted the raw row id here; a review proved a caller could
    subtract two of its own consecutive cursors and recover exactly how
    many rows its scope hid -- see the `INVALID_CURSOR` validation rule and
    spec §5.)

    A single batch-level `next_key` cannot express a guarded caller's real
    page boundary: `GuardedQuery.get_objects` (T2) applies `where=` +
    `_visible` + `_redact` to each row AFTER `read_page` returns a batch, so
    the last row it actually KEEPS usually sits mid-batch, not at the
    batch's end. Returning a key per row lets the guarded layer emit the
    cursor for the last row it kept, not the last row the store happened to
    fetch -- the alternative is either an over-long page (reading past
    `limit` to reach a batch boundary) or a re-fetch-and-skip hack.

    Frozen for the same reason `StoredObject`/`Lineage` are: a value, never
    a handle back into the store's own rows.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    obj: StoredObject
