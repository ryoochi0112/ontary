"""Guarded query layer: the only public read path over the object store.

Generalized replacement for `dso.query.GuardedQuery`. Enforces scope
visibility, sensitivity redaction (`ai_usable` / `human_visible`), and the
min-N aggregation guard for ANY ontology declared via `ontary.meta` +
`ontary.scope.ScopePolicy`. `Store.read_current`/`read_all`/`links_from`/
`links_to` remain engine-internal raw reads; `GuardedQuery` is the public
seam every human/AI consumer must go through.

What changed relative to the prototype, and why each hardcoded DSO piece
went away:

- `query._UNSCOPED_TYPES` (a fixed set of unscoped DSO type names) ->
  declarative `ScopePolicy.unscoped_types`.
- The prototype's *two* scope resolvers -- `resolve_owning_scope` (for the
  `_CHAIN_SCOPED_TYPES` allowlist) and this module's own
  `_resolve_team_id`/`_resolve_company_id`/`_resolve_person_id` fallback
  (for everything else, with its own "no scoping info -> treat as visible"
  escape hatch) -- collapse into ONE call: `ontary.scope.resolve_owning_scope`
  plus `ontary.security.covers_scope`. A `ScopePolicy` is complete for every
  object type an author declares a rule for (or lists in
  `unscoped_types`); there is no third "not modelled at all, so let it
  through" bucket here on purpose -- that permissive fallback was exactly
  the class of leak the prototype's own review history (see the long
  comment on `_CHAIN_SCOPED_TYPES` in dso/query.py) had to keep closing one
  type at a time. An ontology author who wants a type to be visible to
  everyone says so explicitly via `unscoped_types`; anything else with an
  unresolvable chain denies, matching prototype parity for a *properly
  declared* type and improving on it for an *undeclared* one.
- `security.MIN_N` (a module constant) -> `ScopePolicy.min_n`, read off the
  policy this `GuardedQuery` is constructed with.
- Sensitivity redaction (`ai_usable` / `human_visible`) was already generic
  in the prototype (driven by `PropertyDef.sensitivity`) and ports
  unchanged.
- `_SCOPE_KEY_FIELDS` (hardcoded `{"team_id", "company_id"}`, exempted from
  the supplied-value where= hidden-field gate so a consumer can filter by a
  scope-routing key it already knows without "learning" anything new --
  see the prototype's reviewer note) -> derived from the policy itself:
  every `DirectProperty.property_name` declared across `policy.rules` is a
  scope-routing property by construction, so the same supplied-value where=
  exemption falls out without hardcoding field names. This remains a filter-
  gate exemption only for supply-shaped predicates (bare `eq` and `in` over
  an explicit list): learning-shaped predicates and order_by/group_by consult
  `disclosure="learned"` because they disclose information about the value
  rather than merely selecting rows by values the consumer already holds, and
  `_redact` still strips the field from every returned row.
- `EngagementScoreSnapshot`-specific object-level withholding
  (`_snapshot_withheld`, min-N/person-privacy on a single object type) is
  DSO domain policy, not hardcoded engine policy -- but leaving it entirely
  OUTSIDE `GuardedQuery` (as an opt-in helper the DSO example had to
  remember to call on every read path) left the MCP/client surfaces free to
  bypass it (whole-branch review finding). The engine-level generalization
  is `ScopePolicy.row_visibility` (see `ontary.scope.RowVisibilityFn`): an
  author-supplied, per-type predicate `GuardedQuery` itself enforces on
  EVERY read path (`get_object`/`get_objects`/`traverse`/`aggregate`), on
  top of -- never instead of -- the ordinary scope check. A type with no
  predicate declared is unaffected. The DSO example wires `_snapshot_
  withheld`'s exact outcomes into one such predicate on `EngagementScore
  Snapshot`; a domain-specific ontology can use the same pattern for its own
  row-visibility predicate.
- The prototype's `_contributor_count`:
  de-duplicated contributors via a hardcoded `person_id` property /
  `byPerson` link (a Response/Person-specific anti-gaming rule). The
  generalized replacement is `ScopePolicy.contributor_rules` -- a per-type,
  declarative resolution list reusing the SAME `ScopeRule` machinery as
  scope resolution (`DirectProperty`/`ViaLink`/etc.) -- plus
  `ontary.scope.resolve_contributor`. `aggregate` below counts DISTINCT
  resolved contributors per group for any object type an ontology author
  declares contributor rules for (a row whose contributor fails to resolve
  counts as its own row, mirroring the prototype's fallback); a type with
  NO contributor rules declared falls back to a plain row count -- the
  still-real, domain-agnostic floor every ontology gets for free with zero
  declarations.
- `identity_revealing` link handling on `traverse` for human consumers is
  already generic (declared on `LinkTypeDef`, see `ontary.meta`) and ports
  unchanged: a human consumer is denied before any target is
  resolved/returned; an AI consumer still goes through normal visibility +
  redaction on the returned rows.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import datetime
from itertools import islice
from typing import Any, Final, Generic, Literal, NoReturn, TypeVar, final, overload

from pydantic import BaseModel, ConfigDict

from ontary.audit import AuditEntry, EmittedEvent
from ontary.errors import InternalError, ValidationFailed, VisibilityError
from ontary.meta import OntologyRegistry
from ontary.query._disclosure import (
    _hidden_fields,
    _positioned_visible_events,
    _ReadDisclosure,
    _record_disclosure,
    _redact,
    _require_coherent_scope,
    _visible,
    redacted_fields,
    scope_limited,
)
from ontary.query._params import (
    _NUMERIC_PROPERTY_TYPES,
    _OrderSpec,
    _property_type,
    _validate_read_fields,
)
from ontary.query._where import (
    _compile_where,
    _NormalizedWhere,
    _validate_where_keys,
    _WhereMatcher,
)
from ontary.scope import (
    ScopePolicy,
    _ScopeReadCache,
    resolve_contributor,
)
from ontary.security import Consumer
from ontary.store import DEFAULT_BATCH, Store, StoredObject
from ontary.typesys import (
    PropertyType,
)

_MIN_N_COUNT_WITHHELD = "count withheld"
_AGGREGATE_FUNCS = ("mean", "count", "sum", "min", "max")

DEFAULT_READ_LIMIT = 1000
"""The default bound for consumer object reads.

Omitting ``limit`` returns a ``Page`` of at most this many rows. Passing
``limit=None`` is the explicit opt-in to the unbounded list form.

Ordered pages are a scale caveat: every page materializes and sorts the entire
object type, regardless of ``limit`` or ``where`` selectivity. In the measured
20,000-row case, ``limit=10`` read all 20,000 rows with ``order_by`` versus 500
without it; that is O(N log N) work per ordered page and O(N² log N) for a full
ordered walk.
"""

_UNSET_LIMIT = object()

OrderBy = str | tuple[str, Literal["asc", "desc"]]
"""Payload-field ordering accepted by ``GuardedQuery.get_objects``."""

AggregateFunc = Literal["mean", "count", "sum", "min", "max"]
"""Reduction accepted by ``aggregate`` and ``aggregate_by``."""

@final
class _AuthorDispatch:
    """Engine-minted proof that the code making a read selection is a
    callable the ontology author registered against a declared
    `FunctionDef` -- the one provenance the disclosure exemption below
    trusts (see `_aggregate`'s `contributor_dedup_declared` comment).

    This is a TYPE, not a string, on purpose. The tier used to be an
    `origin="author_function"` keyword on the public `aggregate` surface,
    which meant any caller could *spell* author provenance as data and get
    a hidden field's mean back (finding 4). A grant cannot be spelled: the
    only instance is `_AUTHOR_DISPATCH` below, it is module-private, and
    `FunctionRegistry.call` is the only place in the engine that attaches
    it -- to a per-dispatch `BoundQuery` the caller never holds a reference
    to. Every public constructor, including the exported `BoundQuery`,
    yields `None` here and therefore reads at consumer tier.

    In-process Python can of course import `_AUTHOR_DISPATCH` and forge the
    grant. That residual is deliberate and is the same bar as taking a raw
    `Store` handle and writing around the action gate: reaching into
    private engine state, not using the SDK. What this closes is the
    *public, typed, documented* door.
    """

    __slots__ = ()


_AUTHOR_DISPATCH: Final = _AuthorDispatch()
"""The engine's only author-provenance grant. Compared by identity."""

AggregateValue = int | float
"""One released aggregate value; ``count`` returns int, others float."""














def _min_n_violation_message(prefix: str, min_n: int) -> str:
    return (
        f"{prefix}: fewer than min_n={min_n} contributors -- "
        f"{_MIN_N_COUNT_WITHHELD}"
    )












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




class GuardedQuery:
    """Public, security-aware read path over a `Store` for one ontology
    (`registry` + `policy` pair)."""

    def __init__(
        self, store: Store, registry: OntologyRegistry, policy: ScopePolicy
    ) -> None:
        self._store = store
        self._registry = registry
        self._policy = policy

    # -- scope resolution --------------------------------------------------

    def _require_coherent_scope(
        self, obj_type: str, *, where: dict[str, Any] | None = None, group_by: str | None = None,
    ) -> _ReadDisclosure:
        """Refuse incoherent policy and mint the read's disclosure scope."""
        return _require_coherent_scope(
            self._registry, self._policy, obj_type, where=where, group_by=group_by,
        )

    def scope_limited(self, obj_type: str) -> bool:
        """Mirror the two deny branches of `_visible` using declarations only.

        Scope coverage and row visibility may deny even when every stored row
        happens to be visible; this marker never reads the store.
        Refuse an incoherent policy like every public read.
        """
        return scope_limited(self._require_coherent_scope, self._policy, obj_type)

    def _visible(
        self, consumer: Consumer, obj: StoredObject, *,
        scope_cache: _ScopeReadCache | None = None,
        resolved_scope: dict[str, str | None] | None = None,
    ) -> bool:
        """Apply scope coverage and row policy using the read's scope frame."""
        return _visible(
            self._store, self._policy, consumer, obj,
            scope_cache=scope_cache, resolved_scope=resolved_scope,
        )

    # -- redaction -----------------------------------------------------

    def _hidden_fields(
        self, consumer: Consumer, obj_type: str, *, disclosure: Literal["supplied", "learned"],
    ) -> set[str]:
        """Property names this consumer may not see on `obj_type`."""
        return _hidden_fields(
            self._registry, self._policy, consumer, obj_type, disclosure=disclosure,
        )

    def redacted_fields(self, consumer: Consumer, obj_type: str) -> tuple[str, ...]:
        """Sorted property names that `_redact` strips from returned rows.

        Refuse an incoherent policy like every public read.
        """
        return redacted_fields(
            self._require_coherent_scope, self._hidden_fields, consumer, obj_type,
        )

    def _validate_where_keys(
        self, obj_type: str, where: _NormalizedWhere | None
    ) -> None:
        """Refuse unknown `where=` keys for a string-form object name."""
        _validate_where_keys(self._registry, obj_type, where)

    def _compile_where(
        self, obj_type: str, where: _NormalizedWhere | None
    ) -> _WhereMatcher | None:
        """Compile `where` into a row matcher."""
        return _compile_where(self._registry, obj_type, where)

    def _redact(
        self, consumer: Consumer, obj_type: str, obj: StoredObject,
    ) -> StoredObject:
        """Redact hidden fields from a COPY of `obj`'s payload."""
        return _redact(self._hidden_fields, consumer, obj_type, obj)

    def visible_events(
        self,
        consumer: Consumer,
        *,
        event_type: str | None = None,
        about: tuple[str, str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[tuple[AuditEntry, EmittedEvent, frozenset[str]]]:
        """Scan the tenant's audit log in seq and emission order (#47).

        Visibility follows the subject's latest row, including its final row
        after retirement. Subject policy checks use declarations alone and run
        before reading audit. Events outside those checked types are hidden.
        Reads cost O(audit log); there is no paging or SQL pushdown yet.
        """
        return [
            (entry, event, hidden)
            for _, entry, event, hidden in self._positioned_visible_events(
                consumer, event_type=event_type, about=about, since=since, until=until,
            )
        ]

    def _positioned_visible_events(
        self, consumer: Consumer, *, event_type: str | None, about: tuple[str, str] | None,
        since: datetime | None, until: datetime | None,
    ) -> list[tuple[tuple[int, int], AuditEntry, EmittedEvent, frozenset[str]]]:
        """`visible_events` rows, each led by its log position (#109)."""
        return _positioned_visible_events(
            self._store, self._registry, self._policy, self._require_coherent_scope,
            self._visible, _ScopeReadCache(), consumer,
            event_type=event_type, about=about, since=since, until=until,
        )









    def _sort_entries(
        self,
        obj_type: str,
        entries: list[tuple[str | None, StoredObject]],
        order_spec: _OrderSpec,
    ) -> list[tuple[str | None, StoredObject]]:
        field, descending = order_spec
        obj_def = self._registry.get_object_type(obj_type)
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
        self, obj_type: str, batch_size: int
    ) -> list[tuple[str | None, StoredObject]]:
        cursor: str | None = None
        entries: list[tuple[str | None, StoredObject]] = []
        while True:
            batch = self._store.read_page(
                obj_type, after_key=cursor, batch=batch_size
            )
            entries.extend((paged_row.key, paged_row.obj) for paged_row in batch)
            if len(batch) < batch_size:
                return entries
            cursor = batch[-1].key

    @staticmethod
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
        self,
        consumer: Consumer,
        obj_type: str,
        where_matcher: _WhereMatcher | None,
        order_spec: _OrderSpec | None,
        *,
        redact_rows: bool,
        scope_cache: _ScopeReadCache,
        stop_after: int | None,
    ) -> list[StoredObject]:
        rows = self._store.read_all(obj_type)
        if order_spec is not None:
            ordered = self._sort_entries(
                obj_type,
                [(None, row) for row in rows],
                order_spec,
            )
            rows = [row for _key, row in ordered]
        # E1: the generator makes the bound count yielded rows by construction,
        # so there is nowhere to write a drifting "rows reached" counter.
        def _selected() -> Iterator[StoredObject]:
            for row in rows:
                if where_matcher is not None and not where_matcher(row.payload):
                    continue
                if not self._visible(consumer, row, scope_cache=scope_cache):
                    continue
                yield (
                    self._redact(consumer, obj_type, row) if redact_rows else row
                )

        selected = _selected()
        if stop_after is None:
            return list(selected)
        return list(islice(selected, stop_after))

    def _row_id_page(
        self,
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
        batch_size = max(limit + 1, DEFAULT_BATCH)
        cursor = after
        kept: list[tuple[str, StoredObject]] = []
        while True:
            batch = self._store.read_page(obj_type, after_key=cursor, batch=batch_size)
            for paged_row in batch:
                cursor = paged_row.key
                if (
                    where_matcher is not None
                    and not where_matcher(paged_row.obj.payload)
                ):
                    continue
                if not self._visible(consumer, paged_row.obj, scope_cache=scope_cache):
                    continue
                kept.append((paged_row.key, paged_row.obj))
                if len(kept) > limit:
                    break
            if len(kept) > limit or len(batch) < batch_size:
                break
        return self._page_of(consumer, obj_type, kept, limit)

    def _page_of(
        self,
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
            items=[self._redact(consumer, obj_type, row) for _key, row in page_rows],
            next_cursor=page_rows[-1][0] if has_more else None,
            has_more=has_more,
        )

    def _ordered_page(
        self,
        consumer: Consumer,
        obj_type: str,
        where_matcher: _WhereMatcher | None,
        order_spec: _OrderSpec,
        limit: int,
        after: str | None,
        scope_cache: _ScopeReadCache,
    ) -> Page:
        # The store cursor still validates against the store's row-id stream;
        # the complete stream is then sorted in Python for this query. This
        # keeps store-side ordering unchanged while making the returned
        # cursor a real opaque key for the last ordered row.
        if after is not None:
            self._store.read_page(obj_type, after_key=after, batch=1)

        entries = self._sort_entries(
            obj_type,
            self._read_all_paged_rows(obj_type, max(limit, DEFAULT_BATCH)),
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
            if where_matcher is not None and not where_matcher(row.payload):
                continue
            if not self._visible(consumer, row, scope_cache=scope_cache):
                continue
            kept.append((key, row))
            if len(kept) > limit:
                break
        return self._page_of(consumer, obj_type, kept, limit)

    # -- public reads -----------------------------------------------------

    @overload
    def get_objects(
        self,
        consumer: Consumer,
        obj_type: str,
        where: dict[str, Any] | None = None,
        *,
        limit: None,
        after: str | None = None,
        order_by: OrderBy | None = None,
    ) -> list[StoredObject]: ...
    @overload
    def get_objects(
        self,
        consumer: Consumer,
        obj_type: str,
        where: dict[str, Any] | None = None,
        *,
        limit: None,
        after: str | None = None,
        order_by: OrderBy | None = None,
        _redact_rows: Literal[False],
        _stop_after: int | None = None,
    ) -> list[StoredObject]: ...
    @overload
    def get_objects(
        self,
        consumer: Consumer,
        obj_type: str,
        where: dict[str, Any] | None = None,
        *,
        limit: int = DEFAULT_READ_LIMIT,
        after: str | None = None,
        order_by: OrderBy | None = None,
    ) -> Page: ...
    def get_objects(
        self,
        consumer: Consumer,
        obj_type: str,
        where: dict[str, Any] | None = None,
        *,
        limit: int | None | object = _UNSET_LIMIT,
        after: str | None = None,
        order_by: OrderBy | None = None,
        _redact_rows: bool = True,
        _stop_after: int | None = None,
    ) -> list[StoredObject] | Page:
        """Read visible payload rows, optionally ordered and paged.

        Omitting limit uses DEFAULT_READ_LIMIT and returns a Page. Passing
        limit=None explicitly selects the unbounded list form. order_by
        accepts a declared payload field (ascending by default) or a
        (field, direction) pair. Its field gates run with where before
        operator validation. ``_redact_rows`` is an internal count-only
        projection switch: the selection and visibility gates always run, and
        only the payload-copy redaction is skipped when it is false.
        ``_stop_after`` is an internal bound on that same unbounded walk.
        """
        disclosure = self._require_coherent_scope(obj_type, where=where)
        effective_limit = self._effective_limit(limit)
        scope_cache = _ScopeReadCache()

        if after is not None and effective_limit is None:
            raise ValidationFailed(
                "get_objects: after= was given without limit= -- the "
                "unpaginated path has no page to resume",
                code="AFTER_WITHOUT_LIMIT",
            )

        order_spec = _validate_read_fields(
            self._registry,
            self._hidden_fields,
            consumer,
            obj_type,
            disclosure.where,
            disclosure.where_fields,
            order_by,
        )
        if effective_limit is not None and effective_limit < 1:
            raise ValidationFailed(
                f"get_objects limit must be >= 1, got {effective_limit!r}",
                code="INVALID_LIMIT",
            )
        where_matcher = _compile_where(self._registry, obj_type, disclosure.where)

        if effective_limit is None:
            return self._unbounded_results(
                consumer,
                obj_type,
                where_matcher,
                order_spec,
                redact_rows=_redact_rows,
                scope_cache=scope_cache,
                stop_after=_stop_after,
            )

        if order_spec is not None:
            return self._ordered_page(
                consumer,
                obj_type,
                where_matcher,
                order_spec,
                effective_limit,
                after,
                scope_cache,
            )
        return self._row_id_page(
            consumer,
            obj_type,
            where_matcher,
            effective_limit,
            after,
            scope_cache,
        )

    def get_object(
        self,
        consumer: Consumer,
        obj_type: str,
        obj_id: str,
    ) -> StoredObject | None:
        self._require_coherent_scope(obj_type)
        row = self._store.read_current(obj_type, obj_id)
        if row is None:
            return None
        scope_cache = _ScopeReadCache()
        if not self._visible(consumer, row, scope_cache=scope_cache):
            raise VisibilityError(
                f"{consumer.actor_id!r} cannot view {obj_type}/{obj_id}",
                code="VISIBILITY_DENIED",
            )
        return self._redact(consumer, obj_type, row)

    def count(
        self,
        consumer: Consumer,
        obj_type: str,
        where: dict[str, Any] | None = None,
    ) -> int:
        """Count rows in the consumer's visible selection.

        The explicit unbounded read is the canonical selection path: field
        gates, operator compilation, scope, and row visibility therefore
        cannot drift from ``get_objects``. It uses that same path with only
        payload redaction disabled, after the visibility decision, because the
        count never returns a payload. This is ordinary visible-row counting
        and deliberately has no min-N release gate; see
        ``count_contributors`` for the privacy-counting primitive.
        """
        return len(
            self.get_objects(
                consumer,
                obj_type,
                where,
                limit=None,
                _redact_rows=False,
            )
        )

    def exists(
        self,
        consumer: Consumer,
        obj_type: str,
        where: dict[str, Any] | None = None,
    ) -> bool:
        """Whether the consumer's visible selection contains any row."""
        return bool(
            self.get_objects(
                consumer,
                obj_type,
                where,
                limit=None,
                _redact_rows=False,
                _stop_after=1,
            )
        )

    def _traverse_targets(
        self,
        consumer: Consumer,
        link_type: str,
        from_id: str,
        *,
        reverse: bool,
    ) -> tuple[str, list[str]]:
        """The one gate every link traversal passes before it reads a row:
        the identity-revealing denial, the target type, the scope-coherence
        check, and the link's id list in the store's link order. Returns
        `(target_type, target_ids)`; ids are raw (unchecked for visibility).
        """
        link_def = self._registry.get_link_type(link_type)
        if link_def.identity_revealing and consumer.kind == "human":
            # e.g. an anonymized-survey-response -> authoring-person link:
            # traversing would re-identify who is behind scoped/redacted
            # data, the same guarantee sensitivity redaction enforces on
            # the query path -- deny before resolving/returning any target,
            # regardless of direction or scope. AI consumers are still
            # subject to normal visibility + redaction on the returned row
            # below.
            raise VisibilityError(
                f"{consumer.actor_id!r} cannot traverse identity-revealing "
                f"link {link_type!r}",
                code="VISIBILITY_DENIED",
            )
        target_type = link_def.from_type if reverse else link_def.to_type
        self._require_coherent_scope(target_type)
        target_ids = (
            self._store.links_to(link_type, from_id)
            if reverse
            else self._store.links_from(link_type, from_id)
        )
        return target_type, target_ids

    def traverse(
        self,
        consumer: Consumer,
        link_type: str,
        from_id: str,
        *,
        reverse: bool = False,
    ) -> list[StoredObject]:
        target_type, target_ids = self._traverse_targets(
            consumer, link_type, from_id, reverse=reverse
        )
        scope_cache = _ScopeReadCache()
        results = []
        for tid in target_ids:
            row = self._store.read_current(target_type, tid)
            if row is None or not self._visible(
                consumer, row, scope_cache=scope_cache
            ):
                continue
            results.append(self._redact(consumer, target_type, row))
        return results

    def traverse_page(
        self,
        consumer: Consumer,
        link_type: str,
        from_id: str,
        *,
        reverse: bool = False,
        limit: int,
        after: str | None = None,
    ) -> Page:
        """One page of `traverse`, in the same (store link) order.

        Keeps `limit` visible linked rows and peeks one more visible row to
        set `has_more`; rows that are missing or hidden from this consumer
        never count. `next_cursor` is the object id of the last kept row
        when `has_more` is true, else `None`.

        `after` resumes after that object id. An id this consumer cannot
        resume from -- not in the current link list, no current row, or a
        row hidden from this consumer -- refuses with `STALE_CURSOR` and one
        identical message for all three, so a guessed `after` never reveals
        that a hidden row is linked here.
        """
        target_type, target_ids = self._traverse_targets(
            consumer, link_type, from_id, reverse=reverse
        )
        if limit < 1:
            raise ValidationFailed(
                f"traverse_page limit must be >= 1, got {limit!r}",
                code="INVALID_LIMIT",
            )
        scope_cache = _ScopeReadCache()
        start = 0
        if after is not None:
            resume_row = None
            if after in target_ids:
                resume_row = self._store.read_current(target_type, after)
            if resume_row is None or not self._visible(
                consumer, resume_row, scope_cache=scope_cache
            ):
                raise ValidationFailed(
                    "traverse: the cursor no longer names a linked row you can "
                    "resume from; restart the traversal from the first page",
                    code="STALE_CURSOR",
                )
            start = target_ids.index(after) + 1

        # Peek one visible row past `limit`, as `_row_id_page` does: only
        # rows that pass `_visible` count, and the peeked row is never
        # redacted or returned (`_page_of` keeps the first `limit`).
        kept: list[tuple[str | None, StoredObject]] = []
        for tid in target_ids[start:]:
            row = self._store.read_current(target_type, tid)
            if row is None or not self._visible(
                consumer, row, scope_cache=scope_cache
            ):
                continue
            kept.append((tid, row))
            if len(kept) > limit:
                break
        return self._page_of(consumer, target_type, kept, limit)

    # -- aggregation -----------------------------------------------------

    def aggregate(
        self,
        consumer: Consumer,
        obj_type: str,
        value_field: str | None = None,
        where: dict[str, Any] | None = None,
        *,
        func: AggregateFunc = "mean",
        _author_dispatch: _AuthorDispatch | None = None,
        _disclosures: list[tuple[str, str]] | None = None,
    ) -> AggregateValue:
        """Reduce `value_field` over every visible row after min-N release.

        Under `func="count"` `value_field` may be `None`: then every
        visible row is counted and min-N is measured over those rows'
        contributors. Any other func with no `value_field` refuses with
        `INVALID_PARAMS` before a row is read.
        """
        result = self._aggregate(
            consumer,
            obj_type,
            value_field,
            where,
            None,
            func,
            scope_cache=_ScopeReadCache(),
            _author_dispatch=_author_dispatch,
            _disclosures=_disclosures,
        )
        if isinstance(result, bool) or not isinstance(result, (int, float)):
            raise InternalError(
                "aggregate returned a non-numeric result for an ungrouped query",
                code="INTERNAL_ERROR",
            )
        return result

    def aggregate_by(
        self,
        consumer: Consumer,
        obj_type: str,
        value_field: str | None,
        group_by: str,
        where: dict[str, Any] | None = None,
        *,
        func: AggregateFunc = "mean",
        _author_dispatch: _AuthorDispatch | None = None,
        _disclosures: list[tuple[str, str]] | None = None,
    ) -> dict[str, AggregateValue]:
        """Reduce `value_field` once per distinct `group_by` value (or, under
        `func="count"` with `value_field=None`, count each group's rows).

        `group_by` must be non-empty: `_aggregate` branches on `group_by`'s
        TRUTHINESS (it is the one shared body backing both `aggregate` and
        `aggregate_by`), so a falsy-but-non-`None` `group_by` (e.g. `""`)
        would silently take the ungrouped path and hand back a `float`
        instead of a `dict` -- this guard, not an `assert` on the result,
        is what refuses that (`assert` is stripped under `python -O`, and
        by the time `_aggregate` returns it is too late anyway: the wrong
        branch already ran). This is the single choke point every caller
        of grouped aggregation converges on -- `BoundQuery.aggregate_by`
        and `OntologyClient.aggregate_by`'s string overload both delegate
        straight here, so neither needs its own copy of this check.

        `_validate_group_by` runs at the same choke point and for the same
        reason. `group_by` used to be the ONE identifier parameter on this
        surface that was never checked against the type's declared properties
        -- `where` keys, `order_by`, and the typed overload's own `group_by`
        all were -- so an unknown name silently became `None` for every row
        and returned the ungrouped mean under the key `"None"`, and a
        `json`-declared name reached `dict.setdefault` with an unhashable
        value and raised a bare `TypeError`.

        The type resolves before the `group_by` check, like every read: an
        undeclared type refuses `UNKNOWN_OBJECT_TYPE` whatever `group_by` is."""
        self._registry.get_object_type(obj_type)
        if not group_by:
            raise ValidationFailed(
                f"aggregate_by: group_by must be a non-empty string, got "
                f"{group_by!r}",
                code="INVALID_GROUP_BY",
            )
        result = self._aggregate(
            consumer,
            obj_type,
            value_field,
            where,
            group_by,
            func,
            scope_cache=_ScopeReadCache(),
            _author_dispatch=_author_dispatch,
            _disclosures=_disclosures,
        )
        if not isinstance(result, dict):
            raise InternalError(
                "aggregate_by returned a non-dict result for a grouped query",
                code="INTERNAL_ERROR",
            )
        return result

    def count_contributors(
        self,
        consumer: Consumer,
        obj_type: str,
        where: dict[str, Any] | None = None,
    ) -> int:
        """How many distinct contributors back this selection -- the size of
        a *releasable* population, never an identity.

        The engine already computes this number to enforce min-N and then
        throws it away, so an author who wants to report "based on N people"
        alongside an aggregate has had only one option: count an identity
        field off visible rows, which forces that field to stay readable.
        This method removes that trade -- the count is available to a
        consumer who cannot read the contributor field at all, because
        contributor resolution reads the raw store row, not the redacted
        projection.

        **Subject to the same min-N gate as the aggregate it accompanies**,
        and that is load-bearing rather than defensive symmetry. Returning
        `2` for a selection whose mean is withheld for having fewer than
        `min_n` contributors would re-open exactly the disclosure the
        withholding exists to prevent, through a new door and past
        `aggregate`'s own refusal. So a selection that raises
        `MIN_N_VIOLATION` from `aggregate` raises the same `VisibilityError`
        here too --
        `test_count_contributors_and_aggregate_agree_on_every_release_decision`
        sweeps the boundary and pins the two verdicts together, because
        pinning only one population size would leave a later refactor free
        to gate one path and not the other.

        The refusal message names the THRESHOLD and not the observed count,
        for the same reason: "fewer than 3" is the disclosable fact, "exactly
        2" is the oracle. `_aggregate` uses the same withholding wording on
        grouped, ungrouped, and empty selections.

        **KNOWN RESIDUAL -- what this gate does NOT close.** It closes
        *direct* release of a sub-threshold count. It does **not** close
        **complementary differencing** across two released selections:

            count(where=None)            -> 10   released
            count(where={"score": 4.0})  ->  8   released
            count(where={"score": 5.0})  -> REFUSED
            10 - 8                       ->  2   the refused cell's size

        So the honest guarantee is "no sub-threshold count is returned
        directly", NOT "no sub-threshold count is derivable". Stating the
        stronger version would be a claim exceeding the code, and this
        method is the wrong place in the codebase to be loose about that.

        This residual is genuinely *new* relative to `aggregate` alone: two
        means yield only a ratio between cell sizes, never an absolute
        count. It is bounded today by who can reach it -- there is no
        `count_contributors` MCP tool, so an outside caller reaches it only
        through a Function an author wrote to accept an arbitrary `where`.
        An author who does that is choosing the exposure, and should know it
        from this docstring rather than discover it from a reviewer.

        Closing it properly needs release-set evaluation (complementary
        suppression over the set of cells a caller has been shown) -- not
        something this method can do while answering one query at a time.
        `test_count_contributors_complement_differencing_is_a_known_residual`
        pins the residual so the claim and the behaviour have to move together
        the day release-set evaluation lands.
        """
        # Same first gates as `_aggregate`, and for the parity reason this
        # method exists to hold: an unregistered type must refuse as one on
        # both paths, or the two disagree the moment a caller misspells a
        # type name. The policy-coherence check leads for the same reason it
        # leads there -- an incoherent declaration cannot be answered at all,
        # so no property of the caller's selection may change the verdict.
        disclosure = self._require_coherent_scope(obj_type, where=where)
        self._registry.get_object_type(obj_type)
        self._where_gate(consumer, obj_type, disclosure)
        visible = self._visible_rows(
            consumer,
            obj_type,
            disclosure.where,
            scope_cache=_ScopeReadCache(),
        )
        count = self._contributor_count(obj_type, visible)
        min_n = self._policy.min_n
        # The empty selection refuses UNCONDITIONALLY, not just when it
        # trips `min_n` -- mirroring `_aggregate`'s own `no_rows` branch,
        # which raises for zero visible rows regardless of the threshold.
        # Without this, a post-construction mutation to `min_n=0` would make
        # `aggregate` raise while this returned `0`, and the parity the
        # release/refuse sweep asserts would hold only for `min_n >= 1`.
        # (An unregistered `obj_type` no longer reaches here at all -- both
        # methods now refuse it above with `UNKNOWN_OBJECT_TYPE`.)
        # Parity is the invariant a reviewer checks; a version of it that
        # is true for most thresholds is not one.
        passed = bool(visible) and count >= min_n
        if not passed:
            raise VisibilityError(
                _min_n_violation_message(obj_type, min_n),
                code="MIN_N_VIOLATION",
            )
        return count

    def _where_gate(
        self,
        consumer: Consumer,
        obj_type: str,
        disclosure: _ReadDisclosure,
    ) -> None:
        """Refuse a `where` that filters on a field hidden from `consumer`.

        Shared by `_aggregate` and `count_contributors` so the two cannot
        drift apart: a filter oracle closed on one path and left open on the
        other is the same disclosure either way.
        """
        _validate_where_keys(self._registry, obj_type, disclosure.where)
        if disclosure.where_fields:
            denied_keys = {
                field
                for field, field_disclosure in disclosure.where_fields
                if field
                in self._hidden_fields(
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

    def _visible_rows(
        self,
        consumer: Consumer,
        obj_type: str,
        where: _NormalizedWhere | None,
        *,
        scope_cache: _ScopeReadCache,
    ) -> list[StoredObject]:
        """Rows of `obj_type` matching `where` that `consumer` may see.

        Extracted from `_aggregate` unchanged, and shared with
        `count_contributors` so both describe the same population -- see that
        method's docstring for why divergence here would be a disclosure and
        not merely an inconsistency.
        """
        where_matcher = _compile_where(self._registry, obj_type, where)
        visible = []
        for row in self._store.read_all(obj_type):
            if where_matcher is not None and not where_matcher(row.payload):
                continue
            if not self._visible(consumer, row, scope_cache=scope_cache):
                continue
            visible.append(row)
        return visible

    def _value_field_gate(
        self,
        consumer: Consumer,
        obj_type: str,
        value_field: str,
        func: AggregateFunc,
        _author_dispatch: _AuthorDispatch | None,
        disclosure: _ReadDisclosure,
    ) -> bool:
        """Refuse a hidden `value_field`, or report that the contributor
        exemption is what allowed it.

        The exemption is a release for the complete visible population only.
        Any non-empty ``where`` or any ``group_by`` narrows that population,
        so this gate refuses it here regardless of predicate operator or
        field visibility. The immutable disclosure scope is minted by the same
        choke point every public read reaches.

        Returns `True` only when the field IS hidden from this consumer and
        the exemption granted the read anyway. `False` covers both a plainly
        visible field and -- unreachable, since the refusal is raised here --
        anything else; the caller uses it to record a disclosure, so a
        `True` that did not come from the exemption would be a false entry in
        the audit log.
        """
        # `.get(...)` and a truthiness test, not `in`: see `_aggregate`'s note
        # on an EMPTY rule list, which `ScopePolicy.validate` refuses outright
        # and this is the runtime half of.
        contributor_dedup_declared = bool(
            self._policy.contributor_rules.get(obj_type)
        )
        value_field_hidden = value_field in self._hidden_fields(
            consumer, obj_type, disclosure="learned"
        )
        exemption_grants_this_read = (
            contributor_dedup_declared
            and _author_dispatch is _AUTHOR_DISPATCH
            and func in ("mean", "count")
            and not disclosure.narrows_population
        )
        if value_field_hidden and not exemption_grants_this_read:
            raise VisibilityError(
                f"{consumer.actor_id!r} cannot aggregate hidden field "
                f"{value_field!r} on {obj_type!r}",
                code="VISIBILITY_DENIED",
            )
        return value_field_hidden and exemption_grants_this_read

    def _aggregate(
        self,
        consumer: Consumer,
        obj_type: str,
        value_field: str | None,
        where: dict[str, Any] | None,
        group_by: str | None,
        func: AggregateFunc,
        *,
        scope_cache: _ScopeReadCache,
        _author_dispatch: _AuthorDispatch | None = None,
        _disclosures: list[tuple[str, str]] | None = None,
    ) -> dict[str, AggregateValue] | AggregateValue:
        disclosure = self._require_coherent_scope(
            obj_type, where=where, group_by=group_by
        )
        normalized_where = disclosure.where

        if func not in _AGGREGATE_FUNCS:
            raise ValidationFailed(
                f"aggregate func must be one of {list(_AGGREGATE_FUNCS)!r}, got {func!r}",
                code="INVALID_PARAMS",
            )
        if value_field is None and func != "count":
            raise ValidationFailed(
                f"value_field is required for func={func!r}",
                code="INVALID_PARAMS",
            )

        # An unregistered type refuses as one, with the code the catalogue
        # declares for it. Without this the read simply found no rows and the
        # empty selection answered `MIN_N_VIOLATION` -- naming a contributor
        # threshold for a population that cannot exist. The disclosure gate
        # above (`_require_coherent_scope`) now resolves the type as its first
        # statement, so this line only restates that precondition locally:
        # every gate below reads the declaration, and `_property_type` still
        # answers "no declared type" for an unregistered name, which would be
        # a fail-OPEN answer here. `_hidden_fields` no longer swallows the
        # case; it raises `UNKNOWN_OBJECT_TYPE` too.
        self._registry.get_object_type(obj_type)

        # The guarantee at this gate is that a consumer cannot learn an
        # individual's hidden value through a released aggregate. A hidden
        # `value_field` therefore stays gated even when it is also a scope
        # routing field: the aggregate returns values, not merely a selection
        # supplied by the caller, so it checks `_hidden_fields` directly.
        #
        # ONE narrow exception, not a general escape hatch: a type that
        # declares `contributor_rules` (see `ScopePolicy.contributor_rules`)
        # has an ontology author who has *already* asserted "min_n on this
        # type counts distinct identities, not rows". The exemption is
        # author-provenance-gated: only an author-declared Function may release
        # `mean` or `count` for that otherwise-hidden field over the complete
        # visible population (e.g. DSO's `deriveCurrentState` over >= min_n
        # distinct contributors). A direct consumer selection is
        # refused even when the same type has contributor rules. `min` and
        # `max` directly release boundary individuals, while `sum` is exactly
        # `mean * count`; all three remain outside the positive exemption.
        # This gate removes caller-selected second populations by refusing
        # narrowed or grouped hidden-field reads. The complete visible
        # population can still drift over time or vary across consumer scopes,
        # so repeated unnarrowed mean/count releases retain a differencing
        # residual.
        #
        # Provenance is an engine-minted GRANT compared by identity, never a
        # value the caller supplies (see `_AuthorDispatch`). It used to be an
        # `origin="author_function"` keyword on this method's public
        # signature, which made the whole exemption decorative: the string
        # was public API, so any caller could assert the tier and read a
        # hidden field's mean, and `BoundQuery` carried the tier as a CLASS
        # attribute so merely constructing one -- it is exported -- granted
        # it too. `FunctionRegistry.call` is now the only mint site, and it
        # attaches the grant to a per-dispatch `BoundQuery` no caller holds.
        #
        # Author provenance is still necessary but no longer sufficient for a
        # narrowed read: raw Function params piped into `where` cannot reopen
        # the contributor exemption because the selection shape is part of
        # this same decision.
        # `.get(...)` and a truthiness test, not `in`: a type mapped to an
        # EMPTY rule list is in `contributor_rules` while declaring no way to
        # resolve a contributor at all. Membership alone would open the
        # exemption on a distinct-identity guarantee nothing can provide --
        # every row would fail to resolve. `ScopePolicy.validate` refuses that
        # declaration outright; this is the runtime half, because a policy may
        # reach a query without `validate()` ever having been called.
        # Whether THIS read is one the exemption -- and only the exemption --
        # allowed. Recorded at the two return points rather than here, because
        # a release that min-N then refuses is not a release: nothing reached
        # the handler, and an audit row saying otherwise would be a false
        # positive in the one log an auditor trusts. `_disclosures` is the
        # sink `OntologyClient.call_function` reads back, threaded by
        # reference exactly as `capability_accesses` is -- see that method.
        releasing = value_field is not None and self._value_field_gate(
            consumer,
            obj_type,
            value_field,
            func,
            _author_dispatch,
            disclosure,
        )

        # The where gate classifies each predicate shape independently from
        # group_by, whose returned dictionary keys are always learned values.
        self._where_gate(consumer, obj_type, disclosure)
        group_by_hidden = self._hidden_fields(
            consumer, obj_type, disclosure="learned"
        )
        if group_by and group_by in group_by_hidden:
            raise VisibilityError(
                f"{consumer.actor_id!r} cannot group {obj_type} by hidden "
                f"field {group_by!r}",
                code="VISIBILITY_DENIED",
            )

        # Declared-type check: only reached once every visibility/
        # redaction gate above has already passed, so a denial a consumer
        # isn't entitled to see never gets pre-empted/leaked by a type
        # error about a field they couldn't aggregate anyway. Checked
        # before any row is iterated/coerced -- a field whose values merely
        # happen to look numeric on some rows can never sneak past the
        # type contract.
        prop_type = (
            _property_type(self._registry, obj_type, value_field)
            if value_field is not None else None
        )
        # Existence, checked here and not earlier for the reason the block
        # above states: below every visibility gate, so a denial the consumer
        # is not entitled to see is never pre-empted by a complaint about a
        # name. A declared-but-hidden `value_field` passes this check and
        # still reaches `VISIBILITY_DENIED` unchanged -- answering
        # `UNKNOWN_FIELD` for it would leak "no such field" about a field that
        # exists AND make that gate unreachable (for `group_by`).
        #
        # For a supplied field, `None` means "no such declared property": the
        # unregistered-type case `_property_type` also swallows is already
        # impossible, because `get_object_type` ran un-caught at the top of
        # this method. The old guard `prop_type is not None` let an undeclared
        # name SKIP the type contract entirely rather than fail closed, and
        # the assumption behind it -- that an unrecognized name simply finds
        # no matching keys -- is false: `declared_shape_violation` allows
        # undeclared payload keys deliberately, so those rows carry values
        # this method then reduced and released, over a field with no declared
        # type, no `Sensitivity` and no scope routing. `where=`, `order_by`
        # and `group_by` all refuse such a name; `value_field` was the only
        # identifier parameter on the read surface that did not, and the
        # TYPED overload already refused it (`_validate_field_name`).
        if value_field is not None and prop_type is None:
            raise ValidationFailed(
                f"{obj_type}: value_field names unknown field(s) "
                f"[{value_field!r}]",
                code="UNKNOWN_FIELD",
            )
        if func != "count" and prop_type not in _NUMERIC_PROPERTY_TYPES:
            raise ValidationFailed(
                f"{obj_type}.{value_field} is declared {prop_type!r}, not "
                f"numeric (int/float) -- aggregate cannot compute a {func} "
                "over it",
                code="NON_NUMERIC_AGGREGATE",
            )

        visible = self._visible_rows(
            consumer,
            obj_type,
            normalized_where,
            scope_cache=scope_cache,
        )

        min_n = self._policy.min_n
        subject = obj_type if value_field is None else f"{obj_type}.{value_field}"
        if not visible:
            raise VisibilityError(
                _min_n_violation_message(subject, min_n),
                code="MIN_N_VIOLATION",
            )

        groups: dict[str | None, list[StoredObject]] = {}
        for row in visible:
            key = row.payload.get(group_by) if group_by else None
            groups.setdefault(key, []).append(row)

        # Defence in depth: the learned-value group_by gate above now makes
        # a hidden group key unreachable here by construction. Keep this
        # message redaction anyway, because the up-front refusal is the only
        # thing making the branch dead; a future change to that refusal must
        # not silently re-open disclosure of the raw key value in a
        # `MIN_N_VIOLATION` message.
        group_key_hidden = bool(group_by) and group_by in self._hidden_fields(
            consumer, obj_type, disclosure="learned"
        )

        out: dict[str, AggregateValue] = {}
        # The raw group key behind each key already released, so a collision
        # can name both populations rather than only the one that arrived
        # second. Kept beside `out` and not derived from it because `out`
        # holds the reduced value, from which the key it came from cannot be
        # recovered.
        released_from: dict[str, object] = {}
        for key, group_rows in groups.items():
            # min-N is measured over the rows that actually CARRY the value,
            # not over every row in the group. The two diverge whenever
            # `value_field` is optional: a group of rows from `min_n` distinct
            # people where only one answered used to clear the floor and then
            # release that one person's value as the "mean" (and, under
            # `func="count"`, release how many people answered). The floor and
            # the released number must describe the same population.
            value_rows = (
                group_rows if value_field is None
                else [r for r in group_rows if value_field in r.payload]
            )
            contributor_count = self._contributor_count(obj_type, value_rows)
            passed = contributor_count >= min_n
            if not passed:
                shown_key = "<redacted>" if group_key_hidden else repr(key)
                # Ungrouped: every row shares the one `None` key, so naming
                # it would read `X.field group None`. Only a real `group_by`
                # has a group to name.
                group_subject = (
                    f"{subject} group {shown_key}" if group_by else subject
                )
                raise VisibilityError(
                    _min_n_violation_message(group_subject, min_n),
                    code="MIN_N_VIOLATION",
                )
            if func == "count":
                aggregate_value: AggregateValue = len(value_rows)
            else:
                assert value_field is not None  # validated before any row read
                values = [float(r.payload[value_field]) for r in value_rows]
                if func == "sum":
                    aggregate_value = sum(values)
                elif func == "min":
                    aggregate_value = min(values, default=0.0)
                elif func == "max":
                    aggregate_value = max(values, default=0.0)
                else:
                    aggregate_value = sum(values) / len(values) if values else 0.0
            if group_by:
                self._release_group(
                    out, released_from, obj_type, group_by, key, aggregate_value
                )
        if value_field is not None:
            _record_disclosure(_disclosures, releasing, obj_type, value_field)
        return out if group_by else aggregate_value

    @staticmethod
    def _release_group(
        out: dict[str, AggregateValue],
        released_from: dict[str, object],
        obj_type: str,
        group_by: str,
        key: object,
        value: AggregateValue,
    ) -> None:
        """Put one group's reduced value in the released dictionary, refusing
        rather than overwriting a cell another group already holds.

        `dict[str, ...]` is the released shape -- it is what `AggregateValue`
        is keyed by, what the typed overloads return, and what MCP puts on the
        wire -- so the group key has to survive a `str()` that is NOT
        injective over the values a group key can take. An optional property
        keys `None` on the rows that lack it, which collides with a row
        carrying the literal string `"None"`.

        The two populations did not merge: the later assignment overwrote the
        earlier one, so the released cell described whichever population was
        inserted last -- and under `func="count"` reported that population's
        size for a call backed by both. Nothing in the result said so, and
        nothing the caller passed decided which one won.

        Refusal, not repair, because the alternatives do not exist. The keys
        cannot stop being strings without changing a public return type and
        MCP's wire shape, and no reserved spelling for the absent key is safe
        -- any sentinel is itself a value some row may legitimately hold.

        Called from inside the release loop, AFTER each group's min-N check,
        so the floor keeps its precedence: a shape complaint must never
        pre-empt the gate that decides whether a population may be released at
        all (the same ordering rule stated for the `value_field` type check).
        """
        released_key = str(key)
        if released_key in out:
            raise ValidationFailed(
                f"{obj_type}.{group_by}: group keys "
                f"{released_from[released_key]!r} and {key!r} both release as "
                f"{released_key!r} -- two populations cannot share one cell",
                code="GROUP_KEY_COLLISION",
            )
        released_from[released_key] = key
        out[released_key] = value


    def _contributor_count(
        self,
        obj_type: str,
        group_rows: list[StoredObject],
    ) -> int:
        """Counts distinct contributors per row group; see the module docstring
        for how the prototype's hardcoded `_contributor_count` was replaced.

        Falls back to a plain row count when `obj_type` has no
        `contributor_rules` declared at all -- that type never opted into
        identity de-dup, so rows are its floor. Otherwise resolves each row's
        contributor via `ontary.scope.resolve_contributor` and counts distinct
        resolved ids, plus ONE for the unresolved rows collectively.

        Unresolved rows used to count one apiece (mirroring the prototype).
        That failed open: the engine has no evidence two unresolved rows come
        from different people, and `close_link`/`ActionContext.retire` turn
        resolvable rows into unresolved ones -- so retiring one Reader made
        three of that Reader's rows look like three people and released their
        mean. `covers_scope` already denies on an unresolved level rather than
        guessing; contributor counting now follows the same rule. The unknown
        population is still never silently dropped: it contributes the single
        identity the engine can actually prove.
        """
        if obj_type not in self._policy.contributor_rules:
            return len(group_rows)

        contributor_ids: set[str] = set()
        unresolved_rows = 0
        for row in group_rows:
            resolved = resolve_contributor(
                self._policy,
                self._store,
                obj_type,
                row.lineage.object_id,
            )
            if resolved is not None:
                contributor_ids.add(resolved)
            else:
                unresolved_rows += 1
        return len(contributor_ids) + (1 if unresolved_rows else 0)
