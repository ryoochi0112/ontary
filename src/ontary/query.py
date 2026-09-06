"""Guarded query layer: the only public read path over the object store.

Generalized replacement for `dso.query.GuardedQuery` (see
specs/ontary-platform.md §3 AC6/AC7, §5, §7). Enforces scope
visibility, sensitivity redaction (`ai_usable` / `human_visible`), and the
min-N aggregation guard for ANY ontology declared via `ontary.meta` +
`ontary.scope.ScopePolicy`. `Store.read_current`/`read_all`/`links_from`/
`links_to` remain engine-internal raw reads; `GuardedQuery` is the public
seam every human/AI consumer must go through (AC7).

What changed relative to the prototype, and why each hardcoded DSO piece
went away:

- `query._UNSCOPED_TYPES` (a fixed set of DSO L0 type names) -> declarative
  `ScopePolicy.unscoped_types`.
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
  the where=/group_by= hidden-field gate so a consumer can filter/group by
  a scope-routing key it already knows without "learning" anything new --
  see the prototype's reviewer note) -> derived from the policy itself:
  every `DirectProperty.property_name` declared across `policy.rules` is a
  scope-routing property by construction, so the same exemption falls out
  without hardcoding field names.
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
- The prototype's `_contributor_count` (T4 review debt, closed here in T8):
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

from typing import Any, Generic, TypeVar, overload

from pydantic import BaseModel, ConfigDict

from ontary.errors import InternalError, ValidationFailed, VisibilityError
from ontary.explain import MinNTrace, _TraceCollector
from ontary.meta import OntologyRegistry
from ontary.scope import (
    DirectProperty,
    RowVisibilityStore,
    ScopePolicy,
    resolve_contributor,
    resolve_owning_scope,
)
from ontary.security import Consumer, covers_scope
from ontary.store import DEFAULT_BATCH, Store, StoredObject
from ontary.typesys import _to_storage_scalar

_NUMERIC_PROPERTY_TYPES = {"int", "float"}
_MIN_N_COUNT_WITHHELD = "count withheld"


def _min_n_violation_message(prefix: str, min_n: int) -> str:
    return (
        f"{prefix}: fewer than min_n={min_n} contributors -- "
        f"{_MIN_N_COUNT_WITHHELD}"
    )

_T = TypeVar("_T")


class Page(BaseModel):
    """A page of `get_objects(..., limit=...)` results (spec pagination-
    hardening AC3/AC4/AC7): `items` holds exactly `limit` `StoredObject`s
    whenever that many visible rows remain, and `next_cursor` is the
    opaque key of the last row actually KEPT -- never a payload primary
    key, never read off a row (`StoredObject`/`Lineage` carry no row
    identity, AC8) -- or `None` iff the store was exhausted before the
    page filled. Frozen, like every other read-model value this layer
    returns."""

    model_config = ConfigDict(frozen=True)

    items: list[StoredObject]
    next_cursor: str | None


class TypedPage(BaseModel, Generic[_T]):
    """The typed-client counterpart to `Page`: `items` holds hydrated `T`
    instances (spec pagination-hardening AC7) instead of raw
    `StoredObject`s; `next_cursor` has the exact same opaque-cursor
    contract as `Page.next_cursor`."""

    model_config = ConfigDict(frozen=True)

    items: list[_T]
    next_cursor: str | None


def _scope_key_fields(policy: ScopePolicy, obj_type: str) -> set[str]:
    """Property names that route a consumer's own scope filters/group-bys
    for `obj_type` specifically, generalizing the prototype's hardcoded
    `_SCOPE_KEY_FIELDS`.

    A consumer always SUPPLIES, never LEARNS, the value it filters/groups
    by on one of these fields, so they are exempt from the where=/group_by=
    hidden-field gate below (`_redact` still strips them from every
    returned row -- this is a filter-gate exemption only, never a weakening
    of output redaction).

    Scoped to `policy.rules.get(obj_type)` ONLY -- not pooled across every
    type in `policy.rules`. Property names are not globally unique across
    an ontology's object types: a broader-scoped consumer querying type B
    must not inherit an exemption that only makes sense because type A
    declared a `DirectProperty` rule with the same field name. Reviewer P1
    (T4): pooling this set globally meant declaring
    `DirectProperty("person_id")` on ONE type (e.g. Team, where a
    team-scoped consumer legitimately supplies its own `person_id`-shaped
    routing key) silently exempted `person_id` on EVERY OTHER type too --
    including a type where `person_id` is a `human_visible=False` identity
    field, re-opening exactly the de-anonymization oracle the prototype's
    own reviewer history (see `dso.query._SCOPE_KEY_FIELDS`'s P0 comment)
    had to close: a broader-scoped human loops
    `where={"person_id": pid}` per candidate id and joins the (visible)
    rows that come back to learn who is behind otherwise-hidden data. Any
    ontology author declaring `DirectProperty` on an identity-like,
    sensitivity-hidden property should assume the SAME per-type exemption
    still applies to consumers broader than the declaring type's own scope
    level, and weigh that against the property's sensitivity before
    declaring the rule.
    """
    fields: set[str] = set()
    for rule in policy.rules.get(obj_type, []):
        if isinstance(rule, DirectProperty):
            fields.add(rule.property_name)
    return fields


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

    def _visible(
        self,
        consumer: Consumer,
        obj: StoredObject,
        trace: _TraceCollector | None = None,
    ) -> bool:
        obj_type = obj.lineage.object_type
        if obj_type not in self._policy.unscoped_types:
            resolved = resolve_owning_scope(
                self._policy,
                self._store,
                obj_type,
                obj.lineage.object_id,
                trace,
            )
            if not covers_scope(self._policy, consumer, resolved):
                return False

        # `row_visibility` (see `ontary.scope.RowVisibilityFn`) is applied
        # ON TOP OF -- never instead of -- the scope check above, for EVERY
        # object type (including `unscoped_types`: scope-unscoped does not
        # imply row-visibility-unscoped). A type with no predicate declared
        # here is unaffected. The predicate is handed the object's PAYLOAD
        # only (never lineage) -- exactly the fields a declared property
        # rule could reference.
        row_visibility = self._policy.row_visibility.get(obj_type)
        if row_visibility is None:
            return True
        return row_visibility(
            RowVisibilityStore(self._store), consumer, obj_type, obj.payload
        )

    # -- redaction -----------------------------------------------------

    def _hidden_fields(self, consumer: Consumer, obj_type: str) -> set[str]:
        """The set of property names this consumer is not allowed to see on
        `obj_type`, per the sensitivity rules declared on its `PropertyDef`s."""
        try:
            obj_def = self._registry.get_object_type(obj_type)
        except ValidationFailed:
            return set()

        hidden: set[str] = set()
        for prop in obj_def.properties:
            if consumer.kind == "human" and not prop.sensitivity.human_visible:
                hidden.add(prop.name)
            if consumer.kind == "ai" and not prop.sensitivity.ai_usable:
                hidden.add(prop.name)
        return hidden

    def _redact(
        self,
        consumer: Consumer,
        obj_type: str,
        obj: StoredObject,
        trace: _TraceCollector | None = None,
    ) -> StoredObject:
        """Redact hidden fields from a COPY of `obj`'s payload -- `lineage`
        is never touched (it carries no sensitivity-classified property) and
        the original `obj`/store row is never mutated (`StoredObject` is
        frozen; this always returns a new instance with a new `payload`
        dict)."""
        hidden = self._hidden_fields(consumer, obj_type)
        if trace is not None:
            trace.record_redaction(
                consumer.kind, tuple(sorted(hidden & obj.payload.keys()))
            )
        payload = {k: v for k, v in obj.payload.items() if k not in hidden}
        return StoredObject(payload=payload, lineage=obj.lineage)

    def _filter_hidden_fields(self, consumer: Consumer, obj_type: str) -> set[str]:
        """Fields this consumer may not use as a `where=`/`group_by=` key.

        Same as `_hidden_fields` minus the ontology's scope-routing
        properties (see `_scope_key_fields`) -- those names are exempt from
        this *filter* gate only; `_redact` above still strips them from
        every returned row.
        """
        return self._hidden_fields(consumer, obj_type) - _scope_key_fields(
            self._policy, obj_type
        )

    def _validate_where_keys(
        self, obj_type: str, where: dict[str, Any] | None
    ) -> None:
        """Refuse unknown `where=` keys for a string-form object name.

        The registry descriptor is the string surface's equivalent of the
        typed model's `model_fields`. Lineage metadata is not in that payload
        schema, so keys such as `source_system` keep the existing
        `UNKNOWN_FIELD` rejection shape instead of becoming filterable.
        Unknown object names use the registry's existing
        `UNKNOWN_OBJECT_TYPE` refusal before payload validation.
        """
        if not where:
            return
        obj_def = self._registry.get_object_type(obj_type)
        unknown = set(where) - {prop.name for prop in obj_def.properties}
        if unknown:
            raise ValidationFailed(
                f"{obj_type}: where= names unknown field(s) "
                f"{sorted(unknown)!r}",
                code="UNKNOWN_FIELD",
            )

    def _normalize_where_values(
        self, obj_type: str, where: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Convert typed filter values to their persisted scalar forms."""
        if not where:
            return where
        obj_def = self._registry.get_object_type(obj_type)
        prop_types = {prop.name: prop.type for prop in obj_def.properties}
        return {
            key: _to_storage_scalar(value, prop_types[key])
            if key in prop_types and value is not None
            else value
            for key, value in where.items()
        }

    # -- public reads -----------------------------------------------------

    @overload
    def get_objects(
        self,
        consumer: Consumer,
        obj_type: str,
        where: dict[str, Any] | None = None,
        *,
        limit: None = None,
        after: str | None = None,
        trace: _TraceCollector | None = None,
    ) -> list[StoredObject]: ...
    @overload
    def get_objects(
        self,
        consumer: Consumer,
        obj_type: str,
        where: dict[str, Any] | None = None,
        *,
        limit: int,
        after: str | None = None,
        trace: _TraceCollector | None = None,
    ) -> Page: ...
    def get_objects(
        self,
        consumer: Consumer,
        obj_type: str,
        where: dict[str, Any] | None = None,
        *,
        limit: int | None = None,
        after: str | None = None,
        trace: _TraceCollector | None = None,
    ) -> list[StoredObject] | Page:
        """`where=` matches against PAYLOAD fields only -- lineage (e.g.
        `source_system`) is never a filter key here; a consumer who needs to
        filter by provenance is out of scope for this read path.

        `limit is None` (the default) is EXACTLY today's behavior and
        return type -- every visible row, via `Store.read_all` (spec
        pagination-hardening AC5); passing `after` without `limit` on this
        path is refused below, not silently ignored (see the
        `AFTER_WITHOUT_LIMIT` validation rule). Passing `limit` returns a `Page`
        filled to exactly `limit` items whenever that many visible rows
        remain (AC3):
        repeated `Store.read_page` batches (size `max(limit, DEFAULT_BATCH)`
        so the very first fetch is never smaller than the caller's own
        page) are each filtered by `where=`/`_visible`/`_redact` in the
        SAME order as the unpaginated path above, appending kept rows until
        `limit` is reached. The loop's only exit conditions are "kept
        `limit` rows" or "a batch came back shorter than requested" (store
        exhausted) -- NEVER "this batch didn't fill the page", which is the
        difference between bounded termination and the all-invisible-tail
        infinite loop spec §11 names as the risk here. `next_cursor` is the
        `PagedRow.key` of the last row actually KEPT (which usually sits
        mid-batch once filtering removes invisible/filtered rows) -- never
        a payload primary key, never read off `StoredObject` itself (AC8)
        -- or `None` iff the store was exhausted before the page filled.

        `after` is only meaningful alongside a `limit` -- the unpaginated
        path below has no page to resume -- so `after is not None and
        limit is None` is a coded `AFTER_WITHOUT_LIMIT` refusal, not a
        silent full re-read (T2 review P1: a resume loop that lost track
        of its own `limit` must fail loudly, not silently duplicate work)."""
        if after is not None and limit is None:
            raise ValidationFailed(
                "get_objects: after= was given without limit= -- the "
                "unpaginated path has no page to resume",
                code="AFTER_WITHOUT_LIMIT",
            )

        self._validate_where_keys(obj_type, where)
        where = self._normalize_where_values(obj_type, where)
        if where:
            hidden = self._filter_hidden_fields(consumer, obj_type)
            denied_keys = hidden & where.keys()
            if denied_keys:
                raise VisibilityError(
                    f"{consumer.actor_id!r} cannot filter {obj_type} on "
                    f"hidden field(s) {sorted(denied_keys)!r}",
                    code="VISIBILITY_DENIED",
                )

        if limit is None:
            rows = self._store.read_all(obj_type)
            results = []
            for row in rows:
                if where and any(row.payload.get(k) != v for k, v in where.items()):
                    continue
                if not self._visible(consumer, row, trace):
                    continue
                results.append(self._redact(consumer, obj_type, row, trace))
            return results

        if limit < 1:
            raise ValidationFailed(
                f"get_objects limit must be >= 1, got {limit!r}",
                code="INVALID_LIMIT",
            )

        batch_size = max(limit, DEFAULT_BATCH)
        cursor = after
        kept: list[StoredObject] = []
        last_kept_key: str | None = None
        next_cursor: str | None = None
        while True:
            batch = self._store.read_page(obj_type, after_key=cursor, batch=batch_size)
            for paged_row in batch:
                cursor = paged_row.key
                if where and any(
                    paged_row.obj.payload.get(k) != v for k, v in where.items()
                ):
                    continue
                if not self._visible(consumer, paged_row.obj, trace):
                    continue
                kept.append(self._redact(consumer, obj_type, paged_row.obj, trace))
                last_kept_key = paged_row.key
                if len(kept) >= limit:
                    break
            if len(kept) >= limit:
                next_cursor = last_kept_key
                break
            if len(batch) < batch_size:
                # Store exhausted before the page filled -- terminate here,
                # NEVER on "page not yet full" (spec §11's fill-loop
                # termination risk): an all-invisible tail keeps this branch
                # unreachable until the store itself runs dry.
                next_cursor = None
                break
        return Page(items=kept, next_cursor=next_cursor)

    def get_object(
        self,
        consumer: Consumer,
        obj_type: str,
        obj_id: str,
        *,
        trace: _TraceCollector | None = None,
    ) -> StoredObject | None:
        row = self._store.read_current(obj_type, obj_id)
        if row is None:
            return None
        if not self._visible(consumer, row, trace):
            raise VisibilityError(
                f"{consumer.actor_id!r} cannot view {obj_type}/{obj_id}",
                code="VISIBILITY_DENIED",
            )
        return self._redact(consumer, obj_type, row, trace)

    def traverse(
        self,
        consumer: Consumer,
        link_type: str,
        from_id: str,
        *,
        trace: _TraceCollector | None = None,
    ) -> list[StoredObject]:
        link_def = self._registry.get_link_type(link_type)
        if link_def.identity_revealing and consumer.kind == "human":
            # e.g. an anonymized-survey-response -> authoring-person link:
            # traversing would re-identify who is behind scoped/redacted
            # data, the same guarantee sensitivity redaction enforces on
            # the query path -- deny before resolving/returning any target,
            # regardless of scope. AI consumers are still subject to normal
            # visibility + redaction on the returned row below.
            raise VisibilityError(
                f"{consumer.actor_id!r} cannot traverse identity-revealing "
                f"link {link_type!r}",
                code="VISIBILITY_DENIED",
            )
        target_type = link_def.to_type
        target_ids = self._store.links_from(link_type, from_id)
        results = []
        for tid in target_ids:
            row = self._store.read_current(target_type, tid)
            if row is None or not self._visible(consumer, row, trace):
                continue
            results.append(self._redact(consumer, target_type, row, trace))
        return results

    # -- aggregation -----------------------------------------------------

    def aggregate(
        self,
        consumer: Consumer,
        obj_type: str,
        value_field: str,
        where: dict[str, Any] | None = None,
        *,
        trace: _TraceCollector | None = None,
    ) -> float:
        """Ungrouped aggregate: the mean of `value_field` over every
        visible row (spec pagination-hardening AC9). Thin typed wrapper
        over `_aggregate` (`group_by=None`) -- every gate lives there; this
        method only narrows `_aggregate`'s union return to the `float` the
        ungrouped call always produces (see `_aggregate`'s own control
        flow: with `group_by=None` there is exactly one group, and it
        either raises `VisibilityError` with code `MIN_N_VIOLATION` or returns
        its mean directly)."""
        result = self._aggregate(
            consumer, obj_type, value_field, where, None, trace
        )
        if not isinstance(result, float):
            raise InternalError(
                "aggregate returned a non-float result for an ungrouped query",
                code="INTERNAL_ERROR",
            )
        return result

    def aggregate_by(
        self,
        consumer: Consumer,
        obj_type: str,
        value_field: str,
        group_by: str,
        where: dict[str, Any] | None = None,
        *,
        trace: _TraceCollector | None = None,
    ) -> dict[str, float]:
        """Grouped aggregate: one mean per distinct `group_by` value (spec
        pagination-hardening AC9). Thin typed wrapper over `_aggregate` --
        every gate lives there; this method only narrows `_aggregate`'s
        union return to the `dict[str, float]` the grouped call always
        produces.

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
        straight here, so neither needs its own copy of this check."""
        if not group_by:
            raise ValidationFailed(
                f"aggregate_by: group_by must be a non-empty string, got "
                f"{group_by!r}",
                code="INVALID_GROUP_BY",
            )
        result = self._aggregate(
            consumer, obj_type, value_field, where, group_by, trace
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
        *,
        trace: _TraceCollector | None = None,
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
        suppression over the set of cells a caller has been shown), which is
        SDK M12 -- not something this method can do while answering one
        query at a time. `test_count_contributors_complement_differencing_is_a_known_residual`
        pins the residual so the claim and the behaviour have to move
        together the day M12 lands.
        """
        hidden = self._filter_hidden_fields(consumer, obj_type)
        self._where_gate(consumer, obj_type, where, hidden)
        visible = self._visible_rows(consumer, obj_type, where, trace)
        count = self._contributor_count(obj_type, visible, trace)
        min_n = self._policy.min_n
        # The empty selection refuses UNCONDITIONALLY, not just when it
        # trips `min_n` -- mirroring `_aggregate`'s own `no_rows` branch,
        # which raises for zero visible rows regardless of the threshold.
        # Without this, a post-construction mutation to `min_n=0` or an
        # unregistered `obj_type` would make
        # `aggregate` raise while this returned `0`, and the parity the
        # release/refuse sweep asserts would hold only for `min_n >= 1`.
        # Parity is the invariant a reviewer checks; a version of it that
        # is true for most thresholds is not one.
        passed = bool(visible) and count >= min_n
        if trace is not None:
            trace.record_min_n(count=count, threshold=min_n, passed=passed)
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
        where: dict[str, Any] | None,
        hidden: set[str],
    ) -> None:
        """Refuse a `where` that filters on a field hidden from `consumer`.

        Shared by `_aggregate` and `count_contributors` so the two cannot
        drift apart: a filter oracle closed on one path and left open on the
        other is the same disclosure either way.
        """
        self._validate_where_keys(obj_type, where)
        if where:
            denied_keys = hidden & where.keys()
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
        where: dict[str, Any] | None,
        trace: _TraceCollector | None = None,
    ) -> list[StoredObject]:
        """Rows of `obj_type` matching `where` that `consumer` may see.

        Extracted from `_aggregate` unchanged, and shared with
        `count_contributors` so both describe the same population -- see that
        method's docstring for why divergence here would be a disclosure and
        not merely an inconsistency.
        """
        where = self._normalize_where_values(obj_type, where)
        visible = []
        for row in self._store.read_all(obj_type):
            if where and any(row.payload.get(k) != v for k, v in where.items()):
                continue
            if not self._visible(consumer, row, trace):
                continue
            visible.append(row)
        return visible

    def _aggregate(
        self,
        consumer: Consumer,
        obj_type: str,
        value_field: str,
        where: dict[str, Any] | None,
        group_by: str | None,
        trace: _TraceCollector | None = None,
    ) -> dict[str, float] | float:
        # `value_field` is never exempt via `_scope_key_fields` the way
        # where=/group_by= filter keys are -- a consumer can legitimately
        # SUPPLY a scope-routing value to filter/group by without learning
        # anything new, but aggregating a hidden field's VALUES (even as a
        # derived mean) discloses exactly the data sensitivity redaction
        # exists to hide (whole-branch review finding: `aggregate` never
        # gated `value_field` at all). So this checks the un-exempted
        # `_hidden_fields` directly, not `_filter_hidden_fields`.
        #
        # ONE narrow exception, not a general escape hatch: a type that
        # declares `contributor_rules` (see `ScopePolicy.contributor_rules`)
        # has an ontology author who has *already* asserted "min_n on this
        # type counts distinct identities, not rows" -- exactly the anti-
        # gaming guarantee (T4/T8 review debt) that makes deriving a mean
        # from an individually-hidden-but-identity-de-duplicated field safe
        # to expose (e.g. DSO's `Response.value`: `human_visible=False`
        # because an individual response must never be read directly, but
        # `deriveCurrentState`'s aggregate over >= min_n distinct
        # *contributors* is the intended, prototype-parity mechanism for
        # deriving a team's metric state -- AC10). A type with NO
        # `contributor_rules` gets no such guarantee (min_n there is a bare
        # row count, satisfiable by e.g. many rows from one hidden
        # identity), so its hidden fields stay fully gated here -- this is
        # the case the toy ontology's `Book.acquisition_cost` regression
        # test below exercises.
        contributor_dedup_declared = obj_type in self._policy.contributor_rules
        if (
            value_field in self._hidden_fields(consumer, obj_type)
            and not contributor_dedup_declared
        ):
            raise VisibilityError(
                f"{consumer.actor_id!r} cannot aggregate hidden field "
                f"{value_field!r} on {obj_type!r}",
                code="VISIBILITY_DENIED",
            )

        hidden = self._filter_hidden_fields(consumer, obj_type)
        self._where_gate(consumer, obj_type, where, hidden)
        if group_by and group_by in hidden:
            raise VisibilityError(
                f"{consumer.actor_id!r} cannot group {obj_type} by hidden "
                f"field {group_by!r}",
                code="VISIBILITY_DENIED",
            )

        # Declared-type check (AC7): only reached once every visibility/
        # redaction gate above has already passed, so a denial a consumer
        # isn't entitled to see never gets pre-empted/leaked by a type
        # error about a field they couldn't aggregate anyway. Checked
        # before any row is iterated/coerced -- a field whose values merely
        # happen to look numeric on some rows can never sneak past the
        # type contract.
        prop_type = self._property_type(obj_type, value_field)
        if prop_type is not None and prop_type not in _NUMERIC_PROPERTY_TYPES:
            raise ValidationFailed(
                f"{obj_type}.{value_field} is declared {prop_type!r}, not "
                "numeric (int/float) -- aggregate cannot compute a mean "
                "over it",
                code="NON_NUMERIC_AGGREGATE",
            )

        visible = self._visible_rows(consumer, obj_type, where, trace)

        groups: dict[str | None, list[StoredObject]] = {}
        for row in visible:
            key = row.payload.get(group_by) if group_by else None
            groups.setdefault(key, []).append(row)

        # `group_by` may be sensitivity-hidden from this consumer yet still
        # allowed here as a scope-routing key exemption (see
        # `_scope_key_fields`) -- that only exempts the *filter/group-by
        # gate*, never output redaction. The `MIN_N_VIOLATION` message below
        # must honor that same redaction: it must not echo a group key
        # VALUE the consumer isn't allowed to see (`_redact` would strip
        # this field from every returned row).
        group_key_hidden = bool(group_by) and group_by in self._hidden_fields(
            consumer, obj_type
        )

        out: dict[str, float] = {}
        no_rows = not visible and group_by is None
        min_n = self._policy.min_n
        for key, group_rows in groups.items():
            contributor_count = self._contributor_count(
                obj_type, group_rows, trace
            )
            passed = contributor_count >= min_n
            if trace is not None:
                trace.record_min_n(
                    count=contributor_count, threshold=min_n, passed=passed
                )
            if not passed:
                shown_key = "<redacted>" if group_key_hidden else repr(key)
                raise VisibilityError(
                    _min_n_violation_message(
                        f"{obj_type}.{value_field} group {shown_key}", min_n
                    ),
                    code="MIN_N_VIOLATION",
                )
            values = [
                float(r.payload[value_field])
                for r in group_rows
                if value_field in r.payload
            ]
            mean = sum(values) / len(values) if values else 0.0
            if group_by:
                out[str(key)] = mean
            else:
                return mean

        if no_rows:
            if trace is not None:
                trace.record_min_n(count=0, threshold=min_n, passed=False)
            raise VisibilityError(
                _min_n_violation_message(f"{obj_type}.{value_field}", min_n),
                code="MIN_N_VIOLATION",
            )

        return out

    def _property_type(self, obj_type: str, field_name: str) -> str | None:
        """The declared `PropertyType` of `field_name` on `obj_type`, or
        `None` if `obj_type` is unregistered or declares no property by
        that name (an unrecognized `value_field` is left to the existing
        row-based aggregation, which simply finds no matching keys)."""
        try:
            obj_def = self._registry.get_object_type(obj_type)
        except ValidationFailed:
            return None
        for prop in obj_def.properties:
            if prop.name == field_name:
                return prop.type
        return None

    def _contributor_count(
        self,
        obj_type: str,
        group_rows: list[StoredObject],
        trace: _TraceCollector | None = None,
    ) -> int:
        """Count distinct contributors backing a group of rows (T4 review
        debt; see module docstring for what replaced the prototype's
        hardcoded `_contributor_count`).

        Falls back to a plain row count when `obj_type` has no
        `contributor_rules` declared at all. Otherwise resolves each row's
        contributor via `ontary.scope.resolve_contributor` and counts
        distinct resolved ids; a row whose contributor fails to resolve
        counts as its own row (mirrors the prototype's fallback -- only
        genuinely irresolvable rows fall back to a row-count contribution,
        never silently dropped).
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
                trace,
            )
            if resolved is not None:
                contributor_ids.add(resolved)
            else:
                unresolved_rows += 1
        return len(contributor_ids) + unresolved_rows

    def _selection_min_n(
        self,
        consumer: Consumer,
        obj_type: str,
        where: dict[str, Any] | None,
        trace: _TraceCollector,
    ) -> MinNTrace:
        """Record aggregate-relevant min-N for an explained list selection."""
        visible = self._visible_rows(consumer, obj_type, where, trace)
        count = self._contributor_count(obj_type, visible, trace)
        threshold = self._policy.min_n
        trace.record_min_n(
            count=count,
            threshold=threshold,
            passed=bool(visible) and count >= threshold,
        )
        return trace.min_n
