"""Result pages, limits, ordering, and paged row reads."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from itertools import islice
from typing import Any, Generic, Literal, NoReturn, TypeVar

from pydantic import BaseModel, ConfigDict

from ontary.errors import ValidationFailed
from ontary.query._host import _QueryHost
from ontary.query._params import _OrderSpec
from ontary.query._pushdown import _fetch_all, _fetch_page, build_row_filter, filter_is_exact
from ontary.query._where import _WhereMatcher
from ontary.scope import _ScopeReadCache
from ontary.security import Consumer
from ontary.store import DEFAULT_BATCH, StoredObject
from ontary.store._filter import RowFilter
from ontary.typesys import PropertyType

DEFAULT_READ_LIMIT = 1000
"""The default bound for consumer object reads.

Omitting ``limit`` returns a ``Page`` of at most this many rows. Passing
``limit=None`` is the explicit opt-in to the unbounded list form.

Ordered pages materialize and sort the entire prefiltered stream, regardless
of ``limit``. Permissive filters and wrapper stores may still fetch the whole
object type; sorting costs O(N log N) per page for the fetched population.
"""

_UNSET_LIMIT = object()

OrderBy = str | tuple[str, Literal["asc", "desc"]]
"""Payload-field ordering accepted by ``GuardedQuery.get_objects``."""


def _order_sort_value(value: Any, prop_type: PropertyType) -> tuple[int, Any]:
    """Return a deterministic comparable value for one declared property.

    Store rows are already validated against their declared property types.
    ``json`` is the one declared type whose Python values are not generally
    mutually comparable, so its canonical JSON representation supplies the
    same deterministic ordering without adding a store-side ordering path.
    Missing/null values sort before non-null values in ascending order and
    after them in descending order.
    """
    if value is None:
        return (0, "")
    if prop_type == "json":
        return (
            1,
            json.dumps(value, sort_keys=True, separators=(",", ":"), default=repr),
        )
    return (1, value)


_T = TypeVar("_T")


def _refuse_page_walk(what: str) -> NoReturn:
    raise ValidationFailed(
        f"a Page cannot be {what} directly -- read `.items` for the rows on "
        "this page, and pass `limit=None` if you meant the whole selection "
        "rather than one page",
        code="PAGE_NOT_ITERABLE",
    )


class Page(BaseModel):
    """A page of `get_objects(..., limit=...)` results: `items` holds
    exactly `limit` `StoredObject`s whenever that many visible rows remain.

    `has_more` is true exactly when at least one more VISIBLE row follows
    this page (the read peeks one visible row past `limit`; rows hidden by
    scope, `row_visibility`, or `where` never count). `next_cursor` is the
    opaque key of the last row actually KEPT when `has_more` is true --
    never a payload primary key, never read off a row
    (`StoredObject`/`Lineage` carry no row identity) -- and `None` exactly
    when `has_more` is false, so an exactly-full last page carries no
    cursor. Frozen, like every other read-model value this layer returns."""

    model_config = ConfigDict(frozen=True)

    items: list[StoredObject]
    next_cursor: str | None
    has_more: bool

    def __iter__(self) -> NoReturn:
        """Refuse to be walked as a sequence -- read `.items`.

        `BaseModel.__iter__` yields `(field_name, value)` pairs, so an
        inherited `for row in page` hands back `('items', [...])` and
        `('next_cursor', ...)`. Nothing fails at the loop; it fails later, as
        `AttributeError: 'tuple' object has no attribute 'payload'` at
        whatever touched the row, which names neither the page nor the fix.
        Refusing here puts the error where the mistake is.

        This is DX, not a gate: the shape it catches already failed, just
        worse and further away. `model_dump`/`model_copy`/`==` do not route
        through `__iter__` and are unaffected.
        """
        _refuse_page_walk("iterated")

    def __len__(self) -> NoReturn:
        """Refuse to be measured -- read `len(page.items)`.

        `BaseModel` defines no `__len__`, so `len(page)` used to raise a bare
        `TypeError` naming neither the page nor the fix, which is the same
        failure `__iter__` exists to replace and the same failure
        `PAGE_NOT_ITERABLE` already promised to cover.
        """
        _refuse_page_walk("measured")

    def __getitem__(self, index: object) -> NoReturn:
        """Refuse to be indexed or sliced -- read `page.items[...]`."""
        _refuse_page_walk("indexed")

    def __bool__(self) -> bool:
        """Truthiness stays pydantic's, deliberately.

        Without this, Python falls back to `__len__` and every `if page:`
        starts raising -- a behaviour change no error code promises. A page
        is an object that exists; whether it HAS rows is `page.items`.
        """
        return True


class TypedPage(BaseModel, Generic[_T]):
    """The typed-client counterpart to `Page`: `items` holds hydrated `T`
    instances instead of raw `StoredObject`s. `has_more` and `next_cursor`
    have the exact same contract as on `Page`: `next_cursor` is the opaque
    key of the last kept row when `has_more` is true, and `None` exactly
    when `has_more` is false."""

    model_config = ConfigDict(frozen=True)

    items: list[_T]
    next_cursor: str | None
    has_more: bool

    def __iter__(self) -> NoReturn:
        """Refuse to be walked as a sequence -- read `.items`. Same contract
        and same reason as `Page.__iter__`."""
        _refuse_page_walk("iterated")

    def __len__(self) -> NoReturn:
        """Refuse to be measured -- same contract as `Page.__len__`."""
        _refuse_page_walk("measured")

    def __getitem__(self, index: object) -> NoReturn:
        """Refuse to be indexed or sliced -- same contract as
        `Page.__getitem__`."""
        _refuse_page_walk("indexed")

    def __bool__(self) -> bool:
        """Truthiness stays pydantic's -- same reason as `Page.__bool__`."""
        return True


def _sort_entries(
    query: _QueryHost,
    obj_type: str,
    entries: list[tuple[str | None, StoredObject]],
    order_spec: _OrderSpec,
) -> list[tuple[str | None, StoredObject]]:
    field, descending = order_spec
    obj_def = query._registry.get_object_type(obj_type)
    prop_types = {prop.name: prop.type for prop in obj_def.properties}
    prop_type = prop_types[field]
    # Entries arrive in store row_id order. Python's sort is stable,
    # including for reverse sorts, so equal payload values retain that
    # order and row_id is the deterministic tiebreak.
    return sorted(
        entries,
        key=lambda entry: _order_sort_value(
            entry[1].payload.get(field), prop_type
        ),
        reverse=descending,
    )


def _read_all_paged_rows(
    query: _QueryHost, obj_type: str, batch_size: int,
    row_filter: RowFilter | None = None,
    *, scope_cache: _ScopeReadCache | None = None,
) -> list[tuple[str | None, StoredObject]]:
    cursor: str | None = None
    entries: list[tuple[str | None, StoredObject]] = []
    while True:
        batch = _fetch_page(query, obj_type, row_filter, cursor, batch_size)
        if scope_cache is not None:
            for paged_row in batch:
                scope_cache._prime(paged_row.obj)
        entries.extend((paged_row.key, paged_row.obj) for paged_row in batch)
        if len(batch) < batch_size:
            return entries
        cursor = batch[-1].key


def _effective_limit(limit: int | None | object) -> int | None:
    if limit is _UNSET_LIMIT:
        return DEFAULT_READ_LIMIT
    if limit is None:
        return None
    if isinstance(limit, int):
        return limit
    raise ValidationFailed(
        f"get_objects limit must be an integer or None, got {limit!r}",
        code="INVALID_LIMIT",
    )


def _unbounded_results(
    query: _QueryHost,
    consumer: Consumer,
    obj_type: str,
    where_matcher: _WhereMatcher | None,
    order_spec: _OrderSpec | None,
    *,
    redact_rows: bool,
    scope_cache: _ScopeReadCache,
    stop_after: int | None,
) -> list[StoredObject]:
    row_filter = build_row_filter(
        query._registry, query._policy, consumer, obj_type, where_matcher,
    )
    batch_size = DEFAULT_BATCH
    if stop_after is not None:
        batch_size = stop_after if filter_is_exact(query._policy, obj_type) else max(
            stop_after, DEFAULT_BATCH
        )

    def _rows() -> Iterator[StoredObject]:
        if stop_after is None:
            # A full read must judge one statement's snapshot, including
            # when sorting or counting the selection in Python.
            yield from _fetch_all(query, obj_type, row_filter)
            return
        cursor: str | None = None
        while True:
            batch = _fetch_page(query, obj_type, row_filter, cursor, batch_size)
            if scope_cache is not None:
                for paged_row in batch:
                    scope_cache._prime(paged_row.obj)
            for paged_row in batch:
                yield paged_row.obj
            if len(batch) < batch_size:
                return
            cursor = batch[-1].key

    rows = _rows()
    if order_spec is not None:
        ordered = _sort_entries(query, 
            obj_type,
            [(None, row) for row in rows],
            order_spec,
        )
        rows = (row for _key, row in ordered)
    # E1: the generator makes the bound count yielded rows by construction,
    # so there is nowhere to write a drifting "rows reached" counter.
    def _selected() -> Iterator[StoredObject]:
        for row in rows:
            if scope_cache is not None:
                scope_cache._prime(row)
            if where_matcher is not None and not where_matcher(row.payload):
                continue
            if not query._visible(consumer, row, scope_cache=scope_cache):
                continue
            yield (
                query._redact(consumer, obj_type, row) if redact_rows else row
            )

    selected = _selected()
    if stop_after is None:
        return list(selected)
    return list(islice(selected, stop_after))


def _row_id_page(
    query: _QueryHost,
    consumer: Consumer,
    obj_type: str,
    where_matcher: _WhereMatcher | None,
    limit: int,
    after: str | None,
    scope_cache: _ScopeReadCache,
) -> Page:
    # Peek one visible row past `limit`: `has_more` is true exactly when
    # that row exists. The peek runs after `where` and `_visible`, so
    # hidden rows never count, and the peeked row is never redacted or
    # returned.
    row_filter = build_row_filter(
        query._registry, query._policy, consumer, obj_type, where_matcher,
    )
    batch_size = limit + 1 if filter_is_exact(query._policy, obj_type) else max(
        limit + 1, DEFAULT_BATCH
    )
    cursor = after
    kept: list[tuple[str, StoredObject]] = []
    while True:
        batch = _fetch_page(query, obj_type, row_filter, cursor, batch_size)
        if scope_cache is not None:
            for paged_row in batch:
                scope_cache._prime(paged_row.obj)
        for paged_row in batch:
            cursor = paged_row.key
            if (
                where_matcher is not None
                and not where_matcher(paged_row.obj.payload)
            ):
                continue
            if not query._visible(consumer, paged_row.obj, scope_cache=scope_cache):
                continue
            kept.append((paged_row.key, paged_row.obj))
            if len(kept) > limit:
                break
        if len(kept) > limit or len(batch) < batch_size:
            break
    return _page_of(query, consumer, obj_type, kept, limit)


def _page_of(
    query: _QueryHost,
    consumer: Consumer,
    obj_type: str,
    kept: Sequence[tuple[str | None, StoredObject]],
    limit: int,
) -> Page:
    """Build a `Page` from up to `limit + 1` visible `(key, row)` pairs:
    keep and redact the first `limit`, and let the peeked extra row only
    set `has_more`. `next_cursor` is the `limit`-th row's key exactly
    when `has_more` is true."""
    has_more = len(kept) > limit
    page_rows = kept[:limit]
    return Page(
        items=[query._redact(consumer, obj_type, row) for _key, row in page_rows],
        next_cursor=page_rows[-1][0] if has_more else None,
        has_more=has_more,
    )


def _ordered_page(
    query: _QueryHost,
    consumer: Consumer,
    obj_type: str,
    where_matcher: _WhereMatcher | None,
    order_spec: _OrderSpec,
    limit: int,
    after: str | None,
    scope_cache: _ScopeReadCache,
) -> Page:
    # A cursor in the filtered stream is already known to be current;
    # the filtered stream is then sorted in Python for this query. This
    # keeps store-side ordering unchanged while making the returned
    # cursor a real opaque key for the last ordered row.
    row_filter = build_row_filter(
        query._registry, query._policy, consumer, obj_type, where_matcher,
    )
    batch_size = limit + 1 if filter_is_exact(query._policy, obj_type) else max(
        limit + 1, DEFAULT_BATCH
    )
    entries = _read_all_paged_rows(
        query, obj_type, batch_size, row_filter, scope_cache=scope_cache,
    )
    if after is not None and not any(key == after for key, _row in entries):
        # A cursor from another where/scope may still be current. Recompute
        # over today's unfiltered stream to preserve its resume position and
        # the distinction between a stale cursor and a valid hidden row.
        _fetch_page(query, obj_type, None, after, 1)
        entries = _read_all_paged_rows(
            query, obj_type, batch_size, scope_cache=scope_cache,
        )
    entries = _sort_entries(
            query,
            obj_type,
        entries,
        order_spec,
    )
    if after is not None:
        for index, (key, _row) in enumerate(entries):
            if key == after:
                entries = entries[index + 1 :]
                break
        else:
            raise ValidationFailed(
                "get_objects: the row this cursor points at is no longer "
                "current; restart the ordered walk from the first page",
                code="STALE_CURSOR",
            )

    kept: list[tuple[str | None, StoredObject]] = []
    for key, row in entries:
        if scope_cache is not None:
            scope_cache._prime(row)
        if where_matcher is not None and not where_matcher(row.payload):
            continue
        if not query._visible(consumer, row, scope_cache=scope_cache):
            continue
        kept.append((key, row))
        if len(kept) > limit:
            break
    return _page_of(query, consumer, obj_type, kept, limit)
