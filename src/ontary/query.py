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

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from itertools import islice
from typing import Any, Final, Generic, Literal, NoReturn, TypeVar, final, overload

from pydantic import BaseModel, ConfigDict

from ontary.errors import InternalError, ValidationFailed, VisibilityError
from ontary.explain import MinNTrace, _ScanCollector, _TraceCollector
from ontary.meta import OntologyRegistry
from ontary.scope import (
    DirectProperty,
    RowVisibilityStore,
    ScopePolicy,
    _ScopeReadCache,
    incoherent_scope_declarations,
    resolve_contributor,
    resolve_owning_scope,
)
from ontary.security import Consumer, covers_scope
from ontary.store import DEFAULT_BATCH, Store, StoredObject
from ontary.typesys import PropertyType, _to_storage_scalar, validate_scalar

_NUMERIC_PROPERTY_TYPES = {"int", "float"}
_GROUPABLE_PROPERTY_TYPES = {"str", "int", "float", "bool", "date", "datetime"}
"""Declared types whose values may be a `group_by` key.

An ALLOW-list, not a `{"json"}` deny-list, so a `PropertyType` added later
fails closed here instead of reaching `dict.setdefault` and raising a bare
`TypeError` from inside the grouping loop. `json` is the only current member
that can hold a `dict`/`list`, and so the only one excluded.
"""
_WHERE_OPERATORS = frozenset({"gt", "gte", "lt", "lte", "in", "ne", "contains"})
_WHERE_COMPARISON_OPERATORS = frozenset({"gt", "gte", "lt", "lte"})
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

_OrderSpec = tuple[str, bool]

_WhereClause = tuple[str, PropertyType, str, Any]
_WhereMatcher = Callable[[dict[str, Any]], bool]


_NormalizedCondition = tuple[str, str, Any]
"""One `where` condition, snapshotted as ``(kind, operator, operand)``.

``kind`` is ``"bare"`` (the condition was not a mapping, so it is equality
sugar), ``"operator"`` (a single-entry mapping, whose key is NOT yet known to
be a real operator), or ``"malformed"`` (a mapping that is not a single entry,
whose ``operand`` carries its key tuple for the refusal message).
"""

_NormalizedWhere = tuple[tuple[str, _NormalizedCondition], ...]
"""A whole `where` mapping, snapshotted once and immutable thereafter."""


def _is_supplied_value(value: Any) -> bool:
    """Whether a `where` operand is a value the caller had to already hold.

    The supply-shaped exemption rests entirely on this: naming a value you
    must already possess teaches you nothing you did not bring. `None` fails
    it -- null-ness is universally available, so ``where={"person_id": None}``
    is a free per-row null probe, not a supply -- and so does any container or
    arbitrary object, which fails closed ahead of operand validation.
    """
    return isinstance(value, str | int | float | date)


def _normalize_where_condition(condition: Any) -> _NormalizedCondition:
    """Snapshot one caller-supplied condition, reading it EXACTLY once.

    The classifier and the compiler used to each re-read the caller's
    mapping. A mapping whose ``items()`` answered differently on the second
    read therefore got classified on one predicate and executed as another --
    a supply-shaped `eq` at the gate, a `contains` alphabet-walk at the
    matcher. Everything downstream consumes this immutable triple instead of
    the caller's object, so there is no second read to disagree with.
    """
    if not isinstance(condition, dict):
        return ("bare", "eq", condition)
    items = tuple(condition.items())
    if len(items) != 1:
        return ("malformed", "", tuple(operator for operator, _ in items))
    operator, operand = items[0]
    if not isinstance(operator, str):
        return ("malformed", "", (operator,))
    return ("operator", operator, operand)


def _require_where_mapping(where: object) -> None:
    """Refuse a `where=` that is not a mapping, before anything reads it.

    The type check has to come BEFORE the falsy short-circuit below, and that
    ordering is the whole fix. `if not where: return None` treated a falsy
    NON-mapping as "no filter", so ``where=""``/``0``/``[]``/``False``
    silently matched every visible row -- a caller who believed they were
    narrowing received the entire population, with no exception to notice. A
    truthy one reached ``.items()`` and raised a bare `AttributeError` on all
    six read surfaces. The same caller mistake was therefore loud for one
    operand and silent for the other.

    An EMPTY MAPPING keeps meaning "no filter": ``{}`` is a mapping, so it
    passes here and short-circuits below exactly as it always has.

    `INVALID_PARAMS` rather than a new code: the MCP boundary already answers
    it for this exact complaint (`_tool_optional_object` -- "tool parameter
    'where' must be an object, got str"), and `get_objects`' own `order_by`
    shape refusal already uses it for a read parameter in this file. The
    in-process refusal and the wire one now agree on the code as well as on
    the verdict.
    """
    if where is None or isinstance(where, dict):
        return
    raise ValidationFailed(
        "where= must be a mapping of field name to condition, got "
        f"{type(where).__name__}",
        code="INVALID_PARAMS",
    )


def _normalize_where(where: dict[str, Any] | None) -> _NormalizedWhere | None:
    """Snapshot a whole `where` mapping at the public boundary, once."""
    _require_where_mapping(where)
    if not where:
        return None
    return tuple(
        (field, _normalize_where_condition(condition))
        for field, condition in where.items()
    )


def _where_disclosure(
    condition: _NormalizedCondition,
) -> Literal["supplied", "learned"]:
    """Classify only predicate shape for the pre-validation field gate.

    Bare equality and ``in`` over an explicit list of values are supply-shaped:
    the caller must already possess the values being named. Every other shape
    is learning-shaped, including malformed clauses and any operand that
    supplies no value (see `_is_supplied_value`), so hidden fields fail closed
    before operator validation can reveal anything about them.
    """
    kind, operator, operand = condition
    if kind == "bare":
        return "supplied" if _is_supplied_value(operand) else "learned"
    if (
        kind == "operator"
        and operator == "in"
        and isinstance(operand, list)
        and operand
        and all(_is_supplied_value(value) for value in operand)
    ):
        return "supplied"
    return "learned"


@dataclass(frozen=True)
class _ReadDisclosure:
    """One immutable disclosure decision for a read selection.

    ``where`` is the caller mapping snapshotted once. ``where_fields`` keeps
    each predicate's supplied/learned classification beside that snapshot,
    so field gates never re-derive it. ``narrows_population`` is the D4 rule
    consumed by the hidden aggregate-value exemption.
    """

    where: _NormalizedWhere | None
    where_fields: tuple[tuple[str, Literal["supplied", "learned"]], ...]
    group_by: str | None

    @property
    def narrows_population(self) -> bool:
        return bool(self.where) or self.group_by is not None


def _min_n_violation_message(prefix: str, min_n: int) -> str:
    return (
        f"{prefix}: fewer than min_n={min_n} contributors -- "
        f"{_MIN_N_COUNT_WITHHELD}"
    )


def _evaluate_comparison(
    actual: Any, prop_type: PropertyType, operator: str, operand: Any
) -> bool:
    if prop_type == "date":
        if not isinstance(actual, str):
            return False
        try:
            actual_value = date.fromisoformat(actual)
        except ValueError:
            return False
        if actual_value.isoformat() != actual:
            return False
        operand_value = date.fromisoformat(operand)
    else:
        actual_value = actual
        operand_value = operand

    try:
        if operator == "gt":
            return actual_value > operand_value
        if operator == "gte":
            return actual_value >= operand_value
        if operator == "lt":
            return actual_value < operand_value
        return actual_value <= operand_value
    except TypeError:
        # A corrupt historical row does not satisfy a typed predicate.
        # Writes already enforce declared shape; this keeps a read from
        # surfacing an uncoded Python comparison failure if old data does not.
        return False


def _evaluate_where_clause(payload: dict[str, Any], clause: _WhereClause) -> bool:
    field, prop_type, operator, operand = clause
    actual = payload.get(field)
    if operator == "eq":
        return bool(actual == operand)
    if operator == "ne":
        return bool(actual != operand)
    if operator == "in":
        return bool(actual in operand)
    if operator == "contains":
        return bool(isinstance(actual, str) and operand in actual)
    return _evaluate_comparison(actual, prop_type, operator, operand)


def _evaluate_where(
    payload: dict[str, Any], clauses: tuple[_WhereClause, ...]
) -> bool:
    """Evaluate the normalized where clauses against one stored payload.

    This is deliberately the only operator evaluator. Typed and string
    client reads, aggregate selection, BoundQuery reads, and MCP reads all
    converge on ``GuardedQuery`` before reaching this function.
    """
    return all(_evaluate_where_clause(payload, clause) for clause in clauses)


def _operator_type_mismatch(
    obj_type: str,
    field: str,
    operator: object,
    prop_type: str,
    detail: str,
) -> NoReturn:
    raise ValidationFailed(
        f"{obj_type}.{field}: operator {operator!r} is incompatible with "
        f"declared type {prop_type!r} ({detail})",
        code="OPERATOR_TYPE_MISMATCH",
    )


def _unknown_operator(obj_type: str, field: str, operator: object) -> NoReturn:
    raise ValidationFailed(
        f"{obj_type}.{field}: unknown where operator {operator!r}",
        code="UNKNOWN_OPERATOR",
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
    instances (spec pagination-hardening AC7) instead of raw
    `StoredObject`s; `next_cursor` has the exact same opaque-cursor
    contract as `Page.next_cursor`."""

    model_config = ConfigDict(frozen=True)

    items: list[_T]
    next_cursor: str | None

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


def _scope_key_fields(policy: ScopePolicy, obj_type: str) -> set[str]:
    """Property names that route a consumer's own scope filters for
    `obj_type` specifically, generalizing the prototype's hardcoded
    `_SCOPE_KEY_FIELDS`.

    A consumer supplies a scope-routing value to select its own scope, so
    these fields are exempt from the supplied-value where= hidden-field gate
    below (`_redact` still strips them from every returned row -- this is a
    filter-gate exemption only, never a weakening of output redaction).

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

    def _require_coherent_scope(
        self,
        obj_type: str,
        *,
        where: dict[str, Any] | None = None,
        group_by: str | None = None,
    ) -> _ReadDisclosure:
        """Refuse incoherent policy and mint the read's disclosure scope.

        `ScopePolicy.validate()` reports this at startup, but a policy reaches
        a query without it: `OntologyClient` constructs and serves an
        unvalidated `OntologyDef`, and `unscoped_types` is a plain mutable set
        that can be added to after any startup check has already passed. So
        the declaration check is where an author is told, and this is what
        actually closes the door.

        CALLED AT THE PUBLIC ENTRY, BEFORE ANY ROW IS READ -- never from
        `_visible`. A refusal raised per row is itself an oracle: measured at
        `78cdc5a` with the check at `_visible`, `where={hidden_key: <right
        guess>}` raised while `<wrong guess>` returned `0`, because no row
        survived the `where` filter for `_visible` to object to. The caller
        still learns which guess was right, off the refusal instead of the
        count. The verdict has to be a property of the declaration alone, so
        it cannot vary with the selection.

        Scoped to the type being read, not the whole policy: one incoherent
        entry must not turn every other type's reads into policy complaints.
        """
        errors = incoherent_scope_declarations(self._policy, obj_type)
        if errors:
            raise ValidationFailed("; ".join(errors), code="SCOPE_POLICY_ERROR")
        if group_by is not None:
            self._validate_group_by(obj_type, group_by)
        normalized_where = _normalize_where(where)
        return _ReadDisclosure(
            where=normalized_where,
            where_fields=tuple(
                (field, _where_disclosure(condition))
                for field, condition in normalized_where or ()
            ),
            group_by=group_by,
        )

    def _visible(
        self,
        consumer: Consumer,
        obj: StoredObject,
        trace: _TraceCollector | None = None,
        *,
        scope_cache: _ScopeReadCache | None = None,
        scan: _ScanCollector | None = None,
    ) -> bool:
        # The V/S/R/W family in
        # `test_exists_bound_matches_unbounded_walk_over_generated_row_shapes`
        # models both rejection branches here; a new branch needs a new kind.
        obj_type = obj.lineage.object_type
        if obj_type not in self._policy.unscoped_types:
            resolved = resolve_owning_scope(
                self._policy,
                self._store,
                obj_type,
                obj.lineage.object_id,
                trace,
                cache=scope_cache,
            )
            if not covers_scope(self._policy, consumer, resolved):
                if scan is not None:
                    scan.rows_hidden_by_scope += 1
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

    def _hidden_fields(
        self,
        consumer: Consumer,
        obj_type: str,
        *,
        disclosure: Literal["supplied", "learned"],
    ) -> set[str]:
        """The set of property names this consumer is not allowed to see on
        `obj_type`, per the sensitivity rules declared on its `PropertyDef`s.

        A `"learned"` disclosure uses the complete hidden set. A `"supplied"`
        disclosure is for a filter key the consumer supplied and
        removes the ontology's scope-routing properties (see
        `_scope_key_fields`) from that set. Those names are exempt from this
        *filter* gate only; `_redact` above still strips them from every
        returned row.
        """
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

        if disclosure == "supplied":
            # A consumer always SUPPLIES, never LEARNS, a scope-routing value
            # used by a bare eq or in over an explicit list. This exemption
            # applies only to those supply-shaped filter predicates; learning-
            # shaped predicates and order_by/group_by consult "learned"
            # because they disclose information about the value rather than
            # merely selecting rows by values the consumer already holds.
            # `_redact` still strips the same hidden fields from every row.
            return hidden - _scope_key_fields(self._policy, obj_type)

        # A learned value is never exempted, including when it is also a
        # scope-routing property. Aggregates, ordering, and grouping can
        # disclose information about the value rather than merely selecting
        # rows with a value the consumer already supplied.
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
        hidden = self._hidden_fields(consumer, obj_type, disclosure="learned")
        if trace is not None:
            trace.record_redaction(
                consumer.kind, tuple(sorted(hidden & obj.payload.keys()))
            )
        payload = {k: v for k, v in obj.payload.items() if k not in hidden}
        return StoredObject(payload=payload, lineage=obj.lineage)

    def _validate_where_keys(
        self, obj_type: str, where: _NormalizedWhere | None
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
        unknown = {field for field, _ in where} - {
            prop.name for prop in obj_def.properties
        }
        if unknown:
            raise ValidationFailed(
                f"{obj_type}: where= names unknown field(s) "
                f"{sorted(unknown)!r}",
                code="UNKNOWN_FIELD",
            )

    def _validate_group_by(self, obj_type: str, group_by: str) -> None:
        """Refuse a `group_by` that is not a declared, groupable property.

        Existence first, and with the same `UNKNOWN_FIELD` code and message
        shape `order_by` already uses on this surface: an unknown name must
        not be answered with a number, and the three identifier parameters of
        one read must not disagree about what an unknown name is.

        Then the declared TYPE, checked before a single row is read. Doing it
        on the declaration rather than on the stored values is the point: a
        `json` property whose rows all happen to hold scalars would otherwise
        group fine until the first `dict` a connector delivered, which is the
        same data-dependent silence the bare `TypeError` had. A hidden
        `group_by` is deliberately NOT decided here -- it is a declared field,
        so it passes existence and reaches `_aggregate`'s visibility gate
        unchanged; answering `UNKNOWN_FIELD` for it would both leak "no such
        field" about a field that exists and make that gate unreachable.
        """
        obj_def = self._registry.get_object_type(obj_type)
        declared = {prop.name: prop.type for prop in obj_def.properties}
        if group_by not in declared:
            raise ValidationFailed(
                f"{obj_type}: group_by names unknown field(s) [{group_by!r}]",
                code="UNKNOWN_FIELD",
            )
        group_by_type = declared[group_by]
        if group_by_type not in _GROUPABLE_PROPERTY_TYPES:
            raise ValidationFailed(
                f"{obj_type}.{group_by} is declared {group_by_type!r}, which "
                "cannot be a group key -- its values need not be hashable, so "
                "grouping by it fails on the data rather than on the "
                "declaration",
                code="INVALID_GROUP_BY",
            )

    def _validate_read_fields(
        self,
        consumer: Consumer,
        obj_type: str,
        disclosure: _ReadDisclosure,
        order_by: Any,
    ) -> _OrderSpec | None:
        """Run the one field-gate path shared by filtered/ordered reads.

        Unknown and hidden fields are rejected before where-operator
        validation. The order_by value deliberately enters this same gate
        before its direction is checked, so an unknown or hidden field cannot
        be used to probe which ordering forms the ontology accepts.
        """
        self._validate_where_keys(obj_type, disclosure.where)

        order_shape: tuple[str, object] | None
        if order_by is None:
            order_shape = None
        elif isinstance(order_by, str):
            order_shape = (order_by, "asc")
        elif (
            isinstance(order_by, (tuple, list))
            and len(order_by) == 2
            and isinstance(order_by[0], str)
        ):
            order_shape = (order_by[0], order_by[1])
        else:
            raise ValidationFailed(
                "get_objects order_by must be a field name or a "
                "(field, 'asc'|'desc') pair",
                code="INVALID_PARAMS",
            )

        if order_shape is not None:
            order_field, _direction = order_shape
            obj_def = self._registry.get_object_type(obj_type)
            declared = {prop.name for prop in obj_def.properties}
            if order_field not in declared:
                raise ValidationFailed(
                    f"{obj_type}: order_by names unknown field(s) "
                    f"[{order_field!r}]",
                    code="UNKNOWN_FIELD",
                )

        if disclosure.where_fields:
            # The scope-key exemption belongs only to predicates whose shape
            # supplies explicit values. Learning-shaped and malformed clauses
            # consult the un-exempted hidden set before operator validation.
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

        if order_shape is None:
            return None

        order_field, direction = order_shape
        # Ordering discloses rank, so even a scope-routing property is a
        # learned value here and must consult the un-exempted hidden set.
        order_hidden = self._hidden_fields(
            consumer, obj_type, disclosure="learned"
        )
        if order_field in order_hidden:
            raise VisibilityError(
                f"{consumer.actor_id!r} cannot order {obj_type} on "
                f"hidden field(s) [{order_field!r}]",
                code="VISIBILITY_DENIED",
            )
        if direction not in {"asc", "desc"}:
            raise ValidationFailed(
                "get_objects order_by direction must be 'asc' or 'desc', "
                f"got {direction!r}",
                code="INVALID_PARAMS",
            )
        return order_field, direction == "desc"

    @staticmethod
    def _normalize_where_operand(
        operator: object, operand: Any, prop_type: PropertyType
    ) -> Any:
        if operator == "in" and isinstance(operand, list):
            return [
                _to_storage_scalar(value, prop_type)
                if value is not None
                else value
                for value in operand
            ]
        return (
            _to_storage_scalar(operand, prop_type)
            if operand is not None
            else operand
        )

    def _validate_where_operator(
        self,
        obj_type: str,
        field: str,
        prop_type: PropertyType,
        operator: str,
        operand: Any,
    ) -> None:
        if operator in _WHERE_COMPARISON_OPERATORS:
            if prop_type not in {"int", "float", "date"}:
                _operator_type_mismatch(
                    obj_type,
                    field,
                    operator,
                    prop_type,
                    "comparisons require an int, float, or date property",
                )
            self._validate_where_scalar(
                obj_type, field, operator, prop_type, operand
            )
        elif operator == "in":
            if not isinstance(operand, list):
                _operator_type_mismatch(
                    obj_type,
                    field,
                    operator,
                    prop_type,
                    "the operand must be a list",
                )
            for value in operand:
                self._validate_where_scalar(
                    obj_type, field, operator, prop_type, value
                )
        elif operator == "eq":
            # The bare form is the only equality spelling, so `eq` never
            # reaches here from `_WHERE_OPERATORS` -- `_compile_where_clause`
            # routes its `bare` branch here by hand. `None` stays the null
            # test (pinned by
            # `test_bare_null_still_filters_a_field_that_is_not_hidden`);
            # every other operand is a declared-type scalar, same as `ne`.
            if operand is not None:
                self._validate_where_scalar(
                    obj_type, field, operator, prop_type, operand
                )
        elif operator == "ne":
            self._validate_where_scalar(
                obj_type, field, operator, prop_type, operand
            )
        else:
            if prop_type != "str":
                _operator_type_mismatch(
                    obj_type,
                    field,
                    operator,
                    prop_type,
                    "contains requires a str property",
                )
            self._validate_where_scalar(
                obj_type, field, operator, prop_type, operand, scalar_type="str"
            )

    @staticmethod
    def _validate_where_scalar(
        obj_type: str,
        field: str,
        operator: str,
        prop_type: PropertyType,
        operand: Any,
        *,
        scalar_type: PropertyType | None = None,
    ) -> None:
        checked_type = prop_type if scalar_type is None else scalar_type
        mismatch = validate_scalar(operand, checked_type)
        if mismatch is not None:
            _operator_type_mismatch(obj_type, field, operator, prop_type, mismatch)

    def _compile_where_clause(
        self,
        obj_type: str,
        field: str,
        prop_type: PropertyType,
        condition: _NormalizedCondition,
    ) -> _WhereClause:
        """Compile one already-snapshotted condition.

        Takes the same `_NormalizedCondition` the field gate classified, not
        the caller's mapping -- see `_normalize_where_condition` for why a
        second read of that mapping was a disclosure.
        """
        kind, operator, operand = condition
        if kind == "bare":
            self._validate_where_operator(obj_type, field, prop_type, "eq", operand)
            return (
                field,
                prop_type,
                "eq",
                self._normalize_where_operand("eq", operand, prop_type),
            )

        if kind == "malformed":
            _unknown_operator(obj_type, field, operand)
        if operator not in _WHERE_OPERATORS:
            _unknown_operator(obj_type, field, operator)
        self._validate_where_operator(
            obj_type, field, prop_type, operator, operand
        )
        return (
            field,
            prop_type,
            operator,
            self._normalize_where_operand(operator, operand, prop_type),
        )

    def _compile_where(
        self, obj_type: str, where: _NormalizedWhere | None
    ) -> _WhereMatcher | None:
        """Validate operator clauses and compile them into one row matcher.

        Callers invoke this only after the existing unknown-field and hidden-
        field gates. That ordering is intentional: a caller cannot use an
        operator error to probe a field it was not allowed to name.
        """
        if not where:
            return None
        obj_def = self._registry.get_object_type(obj_type)
        prop_types = {prop.name: prop.type for prop in obj_def.properties}
        clauses = [
            self._compile_where_clause(obj_type, field, prop_types[field], condition)
            for field, condition in where
        ]

        frozen_clauses = tuple(clauses)
        return lambda payload: _evaluate_where(payload, frozen_clauses)

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
        trace: _TraceCollector | None,
        *,
        redact_rows: bool,
        scope_cache: _ScopeReadCache,
        stop_after: int | None,
        scan: _ScanCollector | None,
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
                if scan is not None:
                    scan.rows_scanned += 1
                if where_matcher is not None and not where_matcher(row.payload):
                    continue
                if not self._visible(
                    consumer,
                    row,
                    trace,
                    scope_cache=scope_cache,
                    scan=scan,
                ):
                    continue
                if scan is not None:
                    scan.rows_returned += 1
                yield (
                    self._redact(consumer, obj_type, row, trace)
                    if redact_rows
                    else row
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
        trace: _TraceCollector | None,
        scope_cache: _ScopeReadCache,
    ) -> Page:
        batch_size = max(limit, DEFAULT_BATCH)
        cursor = after
        kept: list[StoredObject] = []
        last_kept_key: str | None = None
        next_cursor: str | None = None
        while True:
            batch = self._store.read_page(obj_type, after_key=cursor, batch=batch_size)
            for paged_row in batch:
                cursor = paged_row.key
                if (
                    where_matcher is not None
                    and not where_matcher(paged_row.obj.payload)
                ):
                    continue
                if not self._visible(
                    consumer, paged_row.obj, trace, scope_cache=scope_cache
                ):
                    continue
                kept.append(self._redact(consumer, obj_type, paged_row.obj, trace))
                last_kept_key = paged_row.key
                if len(kept) >= limit:
                    break
            if len(kept) >= limit:
                next_cursor = last_kept_key
                break
            if len(batch) < batch_size:
                next_cursor = None
                break
        return Page(items=kept, next_cursor=next_cursor)

    def _ordered_page(
        self,
        consumer: Consumer,
        obj_type: str,
        where_matcher: _WhereMatcher | None,
        order_spec: _OrderSpec,
        limit: int,
        after: str | None,
        trace: _TraceCollector | None,
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

        items: list[StoredObject] = []
        last_key: str | None = None
        for key, row in entries:
            if where_matcher is not None and not where_matcher(row.payload):
                continue
            if not self._visible(consumer, row, trace, scope_cache=scope_cache):
                continue
            items.append(self._redact(consumer, obj_type, row, trace))
            last_key = key
            if len(items) >= limit:
                break
        return Page(
            items=items,
            next_cursor=last_key if len(items) >= limit else None,
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
        trace: _TraceCollector | None = None,
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
        trace: _TraceCollector | None = None,
        _redact_rows: Literal[False],
        _stop_after: int | None = None,
        _scan: _ScanCollector | None = None,
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
        trace: _TraceCollector | None = None,
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
        trace: _TraceCollector | None = None,
        _redact_rows: bool = True,
        _stop_after: int | None = None,
        _scan: _ScanCollector | None = None,
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
        ``_scan`` optionally observes that walk for the operator-only scan
        report and is never constructed by ordinary reads.
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

        order_spec = self._validate_read_fields(
            consumer, obj_type, disclosure, order_by
        )
        if effective_limit is not None and effective_limit < 1:
            raise ValidationFailed(
                f"get_objects limit must be >= 1, got {effective_limit!r}",
                code="INVALID_LIMIT",
            )
        where_matcher = self._compile_where(obj_type, disclosure.where)

        if effective_limit is None:
            return self._unbounded_results(
                consumer,
                obj_type,
                where_matcher,
                order_spec,
                trace,
                redact_rows=_redact_rows,
                scope_cache=scope_cache,
                stop_after=_stop_after,
                scan=_scan,
            )

        if order_spec is not None:
            return self._ordered_page(
                consumer,
                obj_type,
                where_matcher,
                order_spec,
                effective_limit,
                after,
                trace,
                scope_cache,
            )
        return self._row_id_page(
            consumer,
            obj_type,
            where_matcher,
            effective_limit,
            after,
            trace,
            scope_cache,
        )

    def get_object(
        self,
        consumer: Consumer,
        obj_type: str,
        obj_id: str,
        *,
        trace: _TraceCollector | None = None,
    ) -> StoredObject | None:
        self._require_coherent_scope(obj_type)
        row = self._store.read_current(obj_type, obj_id)
        if row is None:
            return None
        scope_cache = _ScopeReadCache()
        if not self._visible(consumer, row, trace, scope_cache=scope_cache):
            raise VisibilityError(
                f"{consumer.actor_id!r} cannot view {obj_type}/{obj_id}",
                code="VISIBILITY_DENIED",
            )
        return self._redact(consumer, obj_type, row, trace)

    def count(
        self,
        consumer: Consumer,
        obj_type: str,
        where: dict[str, Any] | None = None,
        *,
        trace: _TraceCollector | None = None,
    ) -> int:
        """Count rows in the consumer's visible selection.

        The explicit unbounded read is the canonical selection path: field
        gates, T7 operator compilation, scope, and row visibility therefore
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
                trace=trace,
                _redact_rows=False,
            )
        )

    def exists(
        self,
        consumer: Consumer,
        obj_type: str,
        where: dict[str, Any] | None = None,
        *,
        trace: _TraceCollector | None = None,
    ) -> bool:
        """Whether the consumer's visible selection contains any row."""
        return bool(
            self.get_objects(
                consumer,
                obj_type,
                where,
                limit=None,
                trace=trace,
                _redact_rows=False,
                _stop_after=1,
            )
        )

    def traverse(
        self,
        consumer: Consumer,
        link_type: str,
        from_id: str,
        *,
        reverse: bool = False,
        trace: _TraceCollector | None = None,
    ) -> list[StoredObject]:
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
        scope_cache = _ScopeReadCache()
        results = []
        for tid in target_ids:
            row = self._store.read_current(target_type, tid)
            if row is None or not self._visible(
                consumer, row, trace, scope_cache=scope_cache
            ):
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
        func: AggregateFunc = "mean",
        trace: _TraceCollector | None = None,
        _author_dispatch: _AuthorDispatch | None = None,
        _disclosures: list[tuple[str, str]] | None = None,
    ) -> AggregateValue:
        """Reduce `value_field` over every visible row after min-N release."""
        result = self._aggregate(
            consumer,
            obj_type,
            value_field,
            where,
            None,
            func,
            trace,
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
        value_field: str,
        group_by: str,
        where: dict[str, Any] | None = None,
        *,
        func: AggregateFunc = "mean",
        trace: _TraceCollector | None = None,
        _author_dispatch: _AuthorDispatch | None = None,
        _disclosures: list[tuple[str, str]] | None = None,
    ) -> dict[str, AggregateValue]:
        """Reduce `value_field` once per distinct `group_by` value.

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
        value and raised a bare `TypeError`."""
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
            trace,
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
            trace,
            scope_cache=_ScopeReadCache(),
        )
        count = self._contributor_count(obj_type, visible, trace)
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
        disclosure: _ReadDisclosure,
    ) -> None:
        """Refuse a `where` that filters on a field hidden from `consumer`.

        Shared by `_aggregate` and `count_contributors` so the two cannot
        drift apart: a filter oracle closed on one path and left open on the
        other is the same disclosure either way.
        """
        self._validate_where_keys(obj_type, disclosure.where)
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
        trace: _TraceCollector | None = None,
        *,
        scope_cache: _ScopeReadCache,
    ) -> list[StoredObject]:
        """Rows of `obj_type` matching `where` that `consumer` may see.

        Extracted from `_aggregate` unchanged, and shared with
        `count_contributors` so both describe the same population -- see that
        method's docstring for why divergence here would be a disclosure and
        not merely an inconsistency.
        """
        where_matcher = self._compile_where(obj_type, where)
        visible = []
        for row in self._store.read_all(obj_type):
            if where_matcher is not None and not where_matcher(row.payload):
                continue
            if not self._visible(consumer, row, trace, scope_cache=scope_cache):
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
        """Refuse a hidden `value_field`, or report that the AC10 exemption is
        what allowed it.

        The exemption is a release for the complete visible population only.
        Any non-empty ``where`` or any ``group_by`` narrows that population,
        so D4 refuses it here regardless of predicate operator or field
        visibility. The immutable disclosure scope is minted by the same
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
        value_field: str,
        where: dict[str, Any] | None,
        group_by: str | None,
        func: AggregateFunc,
        trace: _TraceCollector | None = None,
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

        # An unregistered type refuses as one, with the code the catalogue
        # declares for it. Without this the read simply found no rows and the
        # empty selection answered `MIN_N_VIOLATION` -- naming a contributor
        # threshold for a population that cannot exist, and disagreeing with
        # this same call's own `where=`-bearing spelling, which has always
        # reached `UNKNOWN_OBJECT_TYPE` through `_validate_where_keys`. The
        # resolution is first because every gate below reads the declaration:
        # `_hidden_fields` and `_property_type` both swallow the unregistered
        # case and return "nothing hidden" / "no declared type", which is a
        # fail-OPEN answer that only stayed harmless while there were no rows.
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
        # distinct contributors -- AC10). A direct consumer selection is
        # refused even when the same type has contributor rules. `min` and
        # `max` directly release boundary individuals, while `sum` is exactly
        # `mean * count`; all three remain outside the positive exemption.
        # D4 removes caller-selected second populations by refusing narrowed
        # or grouped hidden-field reads. The complete visible population can
        # still drift over time or vary across consumer scopes, so repeated
        # unnarrowed mean/count releases retain a differencing residual.
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
        releasing = self._value_field_gate(
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

        # Declared-type check (AC7): only reached once every visibility/
        # redaction gate above has already passed, so a denial a consumer
        # isn't entitled to see never gets pre-empted/leaked by a type
        # error about a field they couldn't aggregate anyway. Checked
        # before any row is iterated/coerced -- a field whose values merely
        # happen to look numeric on some rows can never sneak past the
        # type contract.
        prop_type = self._property_type(obj_type, value_field)
        # Existence, checked here and not earlier for the reason the block
        # above states: below every visibility gate, so a denial the consumer
        # is not entitled to see is never pre-empted by a complaint about a
        # name. A declared-but-hidden `value_field` passes this check and
        # still reaches `VISIBILITY_DENIED` unchanged -- answering
        # `UNKNOWN_FIELD` for it would leak "no such field" about a field that
        # exists AND make that gate unreachable (Track B's M9, for `group_by`).
        #
        # `None` here means "no such declared property" and nothing else: the
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
        if prop_type is None:
            raise ValidationFailed(
                f"{obj_type}: value_field names unknown field(s) "
                f"[{value_field!r}]",
                code="UNKNOWN_FIELD",
            )
        if prop_type not in _NUMERIC_PROPERTY_TYPES:
            raise ValidationFailed(
                f"{obj_type}.{value_field} is declared {prop_type!r}, not "
                "numeric (int/float) -- aggregate cannot compute a mean "
                "over it",
                code="NON_NUMERIC_AGGREGATE",
            )

        visible = self._visible_rows(
            consumer,
            obj_type,
            normalized_where,
            trace,
            scope_cache=scope_cache,
        )

        min_n = self._policy.min_n
        if not visible:
            if trace is not None:
                trace.record_min_n(count=0, threshold=min_n, passed=False)
            raise VisibilityError(
                _min_n_violation_message(f"{obj_type}.{value_field}", min_n),
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
            value_rows = [r for r in group_rows if value_field in r.payload]
            contributor_count = self._contributor_count(
                obj_type, value_rows, trace
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
            values = [float(r.payload[value_field]) for r in value_rows]
            if func == "count":
                aggregate_value: AggregateValue = len(values)
            elif func == "sum":
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
            else:
                self._record_disclosure(
                    _disclosures, releasing, obj_type, value_field
                )
                return aggregate_value

        self._record_disclosure(_disclosures, releasing, obj_type, value_field)
        return out

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

    @staticmethod
    def _record_disclosure(
        sink: list[tuple[str, str]] | None,
        releasing: bool,
        obj_type: str,
        value_field: str,
    ) -> None:
        """Note one hidden-field release into a dispatch's disclosure sink.

        Called from `_aggregate`'s two return points -- unconditionally, with
        the conditions inside -- so the grouped and ungrouped paths cannot
        drift into recording different things. A no-op for an ordinary read
        (`releasing` false) and for any caller that passed no sink, which is
        every caller except a declared-Function dispatch.
        """
        if releasing and sink is not None:
            sink.append((obj_type, value_field))

    def _property_type(self, obj_type: str, field_name: str) -> str | None:
        """The declared `PropertyType` of `field_name` on `obj_type`, or
        `None` if `obj_type` is unregistered or declares no property by
        that name.

        The `None` answer is a refusal for the caller to act on, not a
        permission to proceed. This docstring used to say an unrecognized
        `value_field` was "left to the existing row-based aggregation, which
        simply finds no matching keys", and `_aggregate` skipped its type gate
        on that basis. Both were wrong: `meta.declared_shape_violation` allows
        undeclared payload keys deliberately, so a row can and does carry a
        key no property declares, and reducing over one released an ungoverned
        number (or let `float()` raise on the data). See `_aggregate`.
        """
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
                trace,
            )
            if resolved is not None:
                contributor_ids.add(resolved)
            else:
                unresolved_rows += 1
        return len(contributor_ids) + (1 if unresolved_rows else 0)

    def _selection_min_n(
        self,
        consumer: Consumer,
        obj_type: str,
        where: _NormalizedWhere | None,
        trace: _TraceCollector,
    ) -> MinNTrace:
        """Record aggregate-relevant min-N for an explained list selection."""
        visible = self._visible_rows(
            consumer,
            obj_type,
            where,
            trace,
            scope_cache=_ScopeReadCache(),
        )
        count = self._contributor_count(obj_type, visible, trace)
        threshold = self._policy.min_n
        trace.record_min_n(
            count=count,
            threshold=threshold,
            passed=bool(visible) and count >= threshold,
        )
        return trace.min_n
