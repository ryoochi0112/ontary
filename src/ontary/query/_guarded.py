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

from datetime import datetime
from typing import Any, Literal, overload

from ontary.audit import AuditEntry, EmittedEvent
from ontary.errors import InternalError, ValidationFailed, VisibilityError
from ontary.meta import OntologyRegistry
from ontary.query._aggregate import (
    AggregateFunc,
    AggregateValue,
    _aggregate,
    _AuthorDispatch,
    _contributor_count,
    _min_n_violation_message,
    _visible_rows,
    _where_gate,
)
from ontary.query._disclosure import (
    _hidden_fields,
    _positioned_visible_events,
    _ReadDisclosure,
    _redact,
    _require_coherent_scope,
    _visible,
    redacted_fields,
    scope_limited,
)
from ontary.query._paging import (
    _UNSET_LIMIT,
    DEFAULT_READ_LIMIT,
    OrderBy,
    Page,
    _effective_limit,
    _ordered_page,
    _page_of,
    _row_id_page,
    _unbounded_results,
)
from ontary.query._params import (
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
)
from ontary.security import Consumer
from ontary.store import Store, StoredObject


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
        effective_limit = _effective_limit(limit)
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
            return _unbounded_results(
                self,
                consumer,
                obj_type,
                where_matcher,
                order_spec,
                redact_rows=_redact_rows,
                scope_cache=scope_cache,
                stop_after=_stop_after,
            )

        if order_spec is not None:
            return _ordered_page(
                self,
                consumer,
                obj_type,
                where_matcher,
                order_spec,
                effective_limit,
                after,
                scope_cache,
            )
        return _row_id_page(
            self,
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
        return _page_of(self, consumer, target_type, kept, limit)

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
        _where_gate(self, consumer, obj_type, disclosure)
        visible = _visible_rows(
            self,
            consumer,
            obj_type,
            disclosure.where,
            scope_cache=_ScopeReadCache(),
        )
        count = _contributor_count(self, obj_type, visible)
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

    def _aggregate(
        self, consumer: Consumer, obj_type: str, value_field: str | None,
        where: dict[str, Any] | None, group_by: str | None, func: AggregateFunc, *,
        scope_cache: _ScopeReadCache, _author_dispatch: _AuthorDispatch | None = None,
        _disclosures: list[tuple[str, str]] | None = None,
    ) -> dict[str, AggregateValue] | AggregateValue:
        return _aggregate(
            self, consumer, obj_type, value_field, where, group_by, func, scope_cache=scope_cache,
            _author_dispatch=_author_dispatch, _disclosures=_disclosures,
        )
