"""Declarative scope-resolution policy: `ScopePolicy` + `resolve_owning_scope`.

An ontology author declares an ordered scope-level hierarchy (narrowest
first, e.g. `["team", "company"]` -- author-defined strings, nothing
hardcoded here) and, per object type, an ORDERED list of resolution rules
that say how to find the scope object that owns an instance of that type at
a given level. This module knows nothing about any particular domain: it is
the generalized replacement for the prototype's `dso.security._TEAM_CHAIN` /
`_TEAM_CHAIN_TO_SIDE` / per-type `if` branches (see
specs/ontary-platform.md §5, §10).

Rule kinds (tried in declaration order, first successful one wins):

- `SelfScope(level)` -- the object itself IS a scope of `level` (e.g. a
  `Team` object is the team-level scope). Only "activates" when the caller
  is resolving exactly `level`; otherwise it is skipped (but see hierarchy
  climbing below).
- `DirectProperty(level, property_name)` -- read the scope id straight off
  the object's payload (a denormalized `team_id`/`company_id`-style
  shortcut). Only activates for its declared `level`.
- `ViaLink(link_api_name, direction, parent_type)` -- walk one governing
  link to a parent object, then resolve *the same requested level*
  recursively starting from that parent. Unlike the rules above, a
  `ViaLink` rule is level-agnostic: it is always attempted regardless of
  which level is being resolved, and simply forwards the request up the
  chain. `direction="from"` walks `store.links_from(link, obj_id)` (the
  governing link runs obj_type -> parent_type, mirroring
  `_TEAM_CHAIN`); `direction="to"` walks `store.links_to(link, obj_id)`
  (the governing link runs parent_type -> obj_type, mirroring
  `_TEAM_CHAIN_TO_SIDE`). When more than one parent is linked, every
  parent is tried in turn and the first one whose chain resolves wins
  (mirrors the prototype's Person fan-out over multiple memberships).
  That order is the store's, and it is a declared total order rather than
  the order the links were created in: **earliest link `valid_from`
  first, then lowest parent id**. `links` carries no monotonic key, so
  two links made in the same clock tick are separated by id, not by which
  was written first. The order is load-bearing -- it decides which scope
  owns the object and therefore which operator the action gate admits --
  so it is specified on every backend rather than left to each one's row
  order.
- `CustomResolver(level, fn)` -- escape hatch for one (object_type, level)
  pair the declarative rules above can't express: an author-supplied
  callable `(store, obj_type, obj_id) -> str | None`. Only activates for
  its declared `level`.

Resolution algorithm (`resolve_owning_scope` / `_resolve_level`):

To resolve `level` for `(obj_type, obj_id)`, fetch the object (missing ->
`None`) and walk `policy.rules[obj_type]` in order. A `SelfScope` /
`DirectProperty` / `CustomResolver` rule only contributes when its own
`level` matches the requested one; a `ViaLink` rule always contributes by
recursing into its parent with the *same* requested level. The first rule
that produces a non-`None` result wins; if a rule produces `None` (dead
end -- e.g. the link has no target, or the requested level doesn't match),
resolution falls through to the next rule.

This single mechanism also gives "hierarchy climbing" for free, with no
special-cased code: resolving a broader level (e.g. "company") for a type
whose rules only reach a narrower level (e.g. "team") works because the
`ViaLink` chain is level-agnostic -- it keeps walking (Goal -> Intent ->
Team) with the *broader* level still attached, until it reaches the object
type (Team) whose OWN rule list happens to include a rule for that broader
level (e.g. a `ViaLink` from Team to Company). This mirrors the prototype's
`_resolve_company_id`, which resolves a Team and then asks *that team's own
rules* for its company -- there is no separate "climb" step, just the same
recursion continuing until a rule matches the requested level.

Unresolvable chains (missing object, no rule reaches the requested level,
or a link dead-ends) return `None` for that level -- deny-by-default, never
inferred. A `visited` set of `(obj_type, obj_id, level)` triples guards
against rule cycles; hitting a cycle also resolves to `None` rather than
recursing forever.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ontary.errors import ValidationFailed
from ontary.explain import ScopePathStep, ScopeRuleTrace, _TraceCollector
from ontary.meta import OntologyRegistry
from ontary.security import Consumer
from ontary.store import Store, StoredObject

Direction = Literal["from", "to"]


class SelfScope(BaseModel):
    """The object itself IS a scope of `level` (its primary key is the id)."""

    level: str


class DirectProperty(BaseModel):
    """Read the scope id straight off the object's `property_name` field."""

    level: str
    property_name: str


class ViaLink(BaseModel):
    """Walk one governing link to a parent object, then resolve the same
    requested level recursively from there. Level-agnostic: always
    attempted regardless of which level is being resolved."""

    link_api_name: str
    direction: Direction
    parent_type: str


class CustomResolver(BaseModel):
    """Escape hatch: an author-supplied callable for one (object_type,
    level) pair the declarative rules above can't express."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    level: str
    fn: Callable[[Store, str, str], str | None]


ScopeRule = SelfScope | DirectProperty | ViaLink | CustomResolver


class RowVisibilityStore:
    """Narrow, read-only surface handed to a `row_visibility` predicate
    (see `ScopePolicy.row_visibility`) -- link traversal ONLY
    (`links_from`/`links_to`), deliberately NOT the full `Store`
    (`read_current`/`read_all` are withheld here, same as from every other
    consumer of this narrower surface).

    A row-visibility predicate is author code, same trust tier as
    `CustomResolver`'s callable (both are declared directly on a
    `ScopePolicy` by the ontology author, not reachable by an ordinary
    human/AI consumer) -- but it is handed a narrower capability than
    `CustomResolver` gets (which receives the raw `Store`) because a
    row-visibility decision only ever needs "can I reach a governing link
    target from this row", never "let me look up some unrelated object by
    id". This keeps the predicate from becoming a general unguarded read
    side-channel while still giving it everything `dso.query
    ._snapshot_withheld` needed (link presence checks) to decide whether one
    row should be shown to one consumer.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def links_from(self, link_api_name: str, from_id: str) -> list[str]:
        return self._store.links_from(link_api_name, from_id)

    def links_to(self, link_api_name: str, to_id: str) -> list[str]:
        return self._store.links_to(link_api_name, to_id)


# `(row_store, consumer, obj_type, row) -> bool`: `True` means the row is
# visible to `consumer` (subject to the ordinary scope check ALSO passing --
# see `GuardedQuery._visible`, which applies both, never either alone);
# `False` withholds it as if it did not exist (matching prototype parity:
# `dso.query.GuardedQuery._snapshot_withheld` hid rows the same way, on top
# of -- never instead of -- its own scope check). `obj_type` is included even
# though a predicate is registered per-type (so it always already knows its
# own type) purely so one predicate function can be shared across multiple
# `ScopePolicy.row_visibility` entries without a closure per type.
RowVisibilityFn = Callable[[RowVisibilityStore, Consumer, str, dict[str, Any]], bool]


def incoherent_scope_declarations(
    policy: "ScopePolicy", obj_type: str | None = None
) -> list[str]:
    """Error strings for types declared BOTH unscoped and scope-routed.

    Pass `obj_type` to ask about one type only -- what `GuardedQuery` needs,
    since it refuses the read of the incoherent type and must not name
    unrelated ones in that refusal. Omit it to sweep the whole policy.

    THE ONE definition of this rule. `ScopePolicy.validate` reports it at
    startup, `ontary.diagnose` reports it without raising, and
    `GuardedQuery` refuses at read time -- three callers, one predicate, so
    they cannot drift into disagreeing about which declarations are legal.

    A type cannot be both. `GuardedQuery._visible` short-circuits the scope
    check for anything in `unscoped_types`, so the `rules` entry is dead --
    but the harm is not the dead rule. `_scope_key_fields` still reads that
    entry, so a `DirectProperty` on a sensitivity-hidden property keeps its
    exemption from the supplied-value `where=` gate, while `unscoped_types`
    removes the scope bound that made the exemption acceptable. Neither half
    opens anything alone; together a consumer probes the hidden scope key of
    rows outside its own scope and reads membership off the result.

    `contributor_rules` and `row_visibility` are deliberately NOT checked
    here. A contributor rule resolves an identity rather than an owning
    scope (`rule.level` is not meaningful there) and never feeds
    `_scope_key_fields`, so `scope="unscoped", contributor=[...]` -- which
    the authoring sugar can produce -- is a legal declaration. Row
    visibility is applied ON TOP OF the scope check for every type,
    `unscoped_types` included, which `GuardedQuery._visible` states is
    intended.
    """
    offending = sorted(set(policy.unscoped_types) & set(policy.rules))
    if obj_type is not None:
        offending = [name for name in offending if name == obj_type]
    return [
        f"ScopePolicy.unscoped_types[{name!r}]: also declares "
        f"ScopePolicy.rules[{name!r}] -- a type is either unscoped or "
        "scope-routed, never both. The unscoped listing wins at read time, "
        "so the rule is silently dead while still exempting its "
        "DirectProperty names from the supplied-value where= gate. Remove "
        "the type from unscoped_types, or drop its rules entry"
        for name in offending
    ]


class ScopePolicy(BaseModel):
    """A complete, per-ontology declaration of how owning scope resolves.

    - `levels`: the scope-level hierarchy, narrowest first.
    - `unscoped_types`: object types with no owning scope at all (visible to
      any consumer regardless of scope -- the generalized replacement for
      `query._UNSCOPED_TYPES`).
    - `rules`: object type api_name -> ordered list of resolution rules.

    A type belongs to `unscoped_types` OR to `rules`, never both --
    `validate` refuses the overlap and `GuardedQuery` refuses to read such a
    type, because the unscoped listing removes the scope check while the
    rules entry still exempts its `DirectProperty` names from the
    supplied-value `where=` gate (see `incoherent_scope_declarations`). To
    widen what a consumer sees, declare a second, broader `ScopeRule` for
    the type rather than listing it as unscoped.
    - `min_n`: aggregation threshold (generalized `security.MIN_N`).

    KNOWN LIMITATION (M3.5 §5 decision, 2026-07-24): coverage checks
    (`ontary.security.covers_scope`) are exact-scope-id-match only -- a
    consumer scoped at a parent level (e.g. company) does not automatically
    cover objects owned by a child scope (e.g. one of that company's teams).
    Parent-covers-child coverage was deferred rather than designed here; see
    `covers_scope`'s docstring for the enforcement-level detail.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    levels: list[str]
    unscoped_types: set[str] = set()
    rules: dict[str, list[ScopeRule]] = {}
    # Per-type CONTRIBUTOR resolution rules -- generalizes the prototype's
    # hardcoded `_contributor_count` (`dso.query`, `person_id` property /
    # `byPerson` link, Response-specific): reuses the SAME `ScopeRule`
    # machinery as `rules` (`SelfScope`/`DirectProperty`/`ViaLink`/
    # `CustomResolver`) to resolve, for one object type, the id of "the
    # identity behind a row" (e.g. Response -> Person) instead of an owning
    # scope. `rule.level` is not meaningful here (contributor resolution has
    # no scope-level hierarchy) and is not checked against `self.levels`.
    # `GuardedQuery.aggregate` (T8) counts DISTINCT resolved contributors per
    # group when the queried type has contributor rules declared here,
    # falling back to a plain row count for any type that has none (T4
    # debt). Opting in means supplying at least one rule: a type mapped to an
    # EMPTY list is refused by `validate`, because it would claim the
    # distinct-identity guarantee while giving the engine no way to honour it.
    contributor_rules: dict[str, list[ScopeRule]] = {}
    # Per-type author-supplied row-visibility predicate (see
    # `RowVisibilityFn`): the generalized replacement for one-off,
    # object-level withholding rules like the prototype's
    # `dso.query.GuardedQuery._snapshot_withheld` (min-N / individual-
    # privacy withholding for `EngagementScoreSnapshot`). Applied by
    # `GuardedQuery` ON TOP OF -- never instead of -- the ordinary scope
    # check every read path already runs; a type with no entry here is
    # unaffected (scope-only, exactly today's behavior).
    row_visibility: dict[str, RowVisibilityFn] = {}
    min_n: int = Field(default=3, ge=1)

    @field_validator("levels")
    @classmethod
    def _levels_are_non_empty_and_unique(cls, levels: list[str]) -> list[str]:
        if not levels:
            raise ValueError("levels must not be empty")
        if len(levels) != len(set(levels)):
            raise ValueError("levels must not contain duplicate entries")
        return levels

    def validate(self, registry: OntologyRegistry) -> None:  # type: ignore[override]
        """Raise a validation-kind error (message naming the offending rule) when
        a rule references an object type, link type, or level that isn't
        declared in `registry` / `self.levels`, or when a `ViaLink`'s
        direction contradicts its `LinkTypeDef`'s from/to types."""
        errors: list[str] = []
        level_set = set(self.levels)

        for obj_type in self.unscoped_types:
            if obj_type not in registry.object_types:
                errors.append(
                    f"ScopePolicy.unscoped_types: undeclared object type {obj_type!r}"
                )

        errors.extend(incoherent_scope_declarations(self))

        for obj_type in self.row_visibility:
            if obj_type not in registry.object_types:
                errors.append(
                    f"ScopePolicy.row_visibility: undeclared object type {obj_type!r}"
                )

        for obj_type, rule_list in self.rules.items():
            errors.extend(
                self._validate_rule_list(
                    registry,
                    f"ScopePolicy.rules[{obj_type!r}]",
                    obj_type,
                    rule_list,
                    check_level=True,
                    level_set=level_set,
                )
            )

        for obj_type, rule_list in self.contributor_rules.items():
            # An empty list here is a declaration error, while an empty list in
            # `rules` above is not. The asymmetry is which way each fails: a
            # type with no scope rule resolves no level and `covers_scope`
            # DENIES, so it fails closed. A type mapped to no contributor rule
            # has opted into identity de-dup while supplying no identity -- it
            # would open `GuardedQuery.aggregate`'s hidden-field exemption on a
            # guarantee nothing can honour. Refuse it where the author can see
            # it, at startup.
            if not rule_list:
                errors.append(
                    f"ScopePolicy.contributor_rules[{obj_type!r}]: empty rule "
                    "list -- a type declaring contributor rules must supply at "
                    "least one rule; remove the entry to opt out of "
                    "contributor de-duplication"
                )
            errors.extend(
                self._validate_rule_list(
                    registry,
                    f"ScopePolicy.contributor_rules[{obj_type!r}]",
                    obj_type,
                    rule_list,
                    check_level=False,
                    level_set=level_set,
                )
            )

        if errors:
            raise ValidationFailed("; ".join(errors), code="SCOPE_POLICY_ERROR")

    @staticmethod
    def _validate_rule_list(
        registry: OntologyRegistry,
        prefix: str,
        obj_type: str,
        rule_list: list["ScopeRule"],
        *,
        check_level: bool,
        level_set: set[str],
    ) -> list[str]:
        errors: list[str] = []

        if obj_type not in registry.object_types:
            errors.append(f"{prefix}: undeclared object type {obj_type!r}")
            return errors

        for index, rule in enumerate(rule_list):
            label = f"{prefix}[{index}] ({rule!r})"

            if check_level and isinstance(
                rule, SelfScope | DirectProperty | CustomResolver
            ):
                if rule.level not in level_set:
                    errors.append(f"{label}: undeclared scope level {rule.level!r}")

            if isinstance(rule, DirectProperty):
                obj_def = registry.object_types.get(obj_type)
                prop_names = (
                    {p.name for p in obj_def.properties} if obj_def else set()
                )
                if rule.property_name not in prop_names:
                    errors.append(
                        f"{label}: property {rule.property_name!r} not "
                        f"declared on {obj_type!r}"
                    )

            if isinstance(rule, ViaLink):
                if rule.parent_type not in registry.object_types:
                    errors.append(
                        f"{label}: undeclared parent_type {rule.parent_type!r}"
                    )
                if rule.link_api_name not in registry.link_types:
                    errors.append(f"{label}: undeclared link {rule.link_api_name!r}")
                else:
                    link_def = registry.link_types[rule.link_api_name]
                    if rule.direction == "from":
                        expected_from, expected_to = obj_type, rule.parent_type
                    else:
                        expected_from, expected_to = rule.parent_type, obj_type
                    if (link_def.from_type, link_def.to_type) != (
                        expected_from,
                        expected_to,
                    ):
                        errors.append(
                            f"{label}: direction {rule.direction!r} expects "
                            f"link {rule.link_api_name!r} to run "
                            f"{expected_from!r} -> {expected_to!r}, but it is "
                            f"declared {link_def.from_type!r} -> "
                            f"{link_def.to_type!r}"
                        )

        return errors


_MAX_DEPTH = 64


def _live_at(obj: StoredObject, asof: str) -> bool:
    """Was `obj` still live at the instant `asof`?

    Inclusive upper bound, the same convention `links_from_asof` /
    `links_to_asof` state in `Store`: a row closed AT `asof` was still live
    then. That is what admits the TARGET's own row -- its `valid_to` IS
    `asof` -- and every ancestor closed in the same cascade tick, which
    `ActionContext.retire` produces constantly because it closes the object
    and its links on separate clock reads that collide at microsecond
    resolution.

    `valid_from` is deliberately NOT tested. `read_last` answers with the
    NEWEST row, and an object merely UPDATED after `asof` has a newest row
    that opens after it -- so a `valid_from <= asof` test would drop an
    ancestor that was alive the whole time. Whether the edge existed at
    `asof` is the link read's question and it has already been asked; the
    only thing left for the object itself is whether it had already been
    retired by then.
    """
    valid_to = obj.lineage.valid_to
    return valid_to is None or valid_to >= asof


def _resolve_level(
    policy: ScopePolicy,
    store: Store,
    obj_type: str,
    obj_id: str,
    level: str,
    visited: set[tuple[str, str, str]],
    trace: _TraceCollector | None = None,
    scope_path: tuple[ScopePathStep, ...] | None = None,
    asof: str | None = None,
) -> str | None:
    key = (obj_type, obj_id, level)
    if key in visited or len(visited) >= _MAX_DEPTH:
        return None
    visited = visited | {key}

    # Live row by default. `asof` is the ACTION TARGET GATE's question and
    # only its own -- "what scope did this object belong to at the instant
    # its row closed", asked so a retired target reaches the handler's
    # `OBJECT_ALREADY_RETIRED` instead of a scope denial. A consumer read
    # must not ask it: resolving a retired -- or ERASED -- row's scope puts
    # its children back in the visible set, which carries a population past
    # min_n and releases an aggregate computed over the erased subject's own
    # rows. `resolve_owning_scope` is the only place it is ever set.
    if asof is None:
        obj = store.read_current(obj_type, obj_id)
    else:
        obj = store.read_last(obj_type, obj_id)
        if obj is not None and not _live_at(obj, asof):
            # Retired BEFORE the instant being resolved: it was not on this
            # chain then either, so it cannot answer for it now.
            return None
    if obj is None:
        return None

    if trace is not None and scope_path is None:
        scope_path = (ScopePathStep(object_type=obj_type, object_id=obj_id),)

    for rule in policy.rules.get(obj_type, []):
        result = _apply_rule(
            policy,
            store,
            obj,
            obj_type,
            obj_id,
            level,
            rule,
            visited,
            trace,
            scope_path,
            asof,
        )
        if result is not None:
            return result

    return _climb_hierarchy(
        policy,
        store,
        obj_type,
        obj_id,
        level,
        visited,
        trace,
        scope_path,
        asof,
    )


def _apply_rule(
    policy: ScopePolicy,
    store: Store,
    obj: StoredObject,
    obj_type: str,
    obj_id: str,
    level: str,
    rule: ScopeRule,
    visited: set[tuple[str, str, str]],
    trace: _TraceCollector | None,
    scope_path: tuple[ScopePathStep, ...] | None,
    asof: str | None = None,
) -> str | None:
    """One rule's answer for `level`, or `None` to fall through to the next
    rule (the dispatch ladder `_resolve_level`'s loop used to inline)."""
    if isinstance(rule, SelfScope):
        result = str(obj_id) if rule.level == level else None
        _record_rule(
            trace, rule, obj_type, obj_id, level, result, scope_path
        )
        return result

    if isinstance(rule, DirectProperty):
        value = obj.payload.get(rule.property_name) if rule.level == level else None
        result = str(value) if value is not None else None
        _record_rule(
            trace, rule, obj_type, obj_id, level, result, scope_path
        )
        return result

    if isinstance(rule, ViaLink):
        # A retired object has no live links: `ActionContext.retire`
        # cascade-closes every one of them (AC3), so `links_from`/`links_to`
        # -- which filter `valid_to IS NULL` -- answer `[]` and the object
        # resolves to no scope at all. Reading its own row back is not
        # enough for a `ViaLink` rule: there is a tombstone to read and
        # nothing to climb. So read the links AS OF the row's own closing
        # instant, which is the last moment they were still its links.
        if asof is not None:
            if rule.direction == "from":
                parents = store.links_from_asof(rule.link_api_name, obj_id, asof)
            else:
                parents = store.links_to_asof(rule.link_api_name, obj_id, asof)
        elif rule.direction == "from":
            parents = store.links_from(rule.link_api_name, obj_id)
        else:
            parents = store.links_to(rule.link_api_name, obj_id)
        # First parent whose chain resolves wins, over the store's DECLARED
        # order (earliest link `valid_from`, then lowest id -- see
        # `Store.links_to`). Not creation order, and not each backend's own
        # row order: which parent comes first is which scope owns the object,
        # so it is part of the gate's answer rather than a detail of the read.
        for parent_id in parents:
            parent_path = None
            if trace is not None:
                parent_path = (scope_path or ()) + (
                    ScopePathStep(
                        object_type=rule.parent_type,
                        object_id=parent_id,
                        via_link=rule.link_api_name,
                        direction=rule.direction,
                    ),
                )
            result = _resolve_level(
                policy,
                store,
                rule.parent_type,
                parent_id,
                level,
                visited,
                trace,
                parent_path,
                # The SAME instant, not the parent's own and not "now". The
                # parent is admitted only if it was still live at `asof`
                # (`_live_at`), which is what keeps a retired parent from
                # winning this loop's first-parent-wins race against a live
                # one -- the round-5 inversion -- while still letting a
                # parent that outlived the target answer for it. Hard-coding
                # this live-only closed the inversion and opened its mirror:
                # an ancestor that was alive when the target died, and is the
                # only route to a scope that is alive now, was refused, so
                # the operator who owns the row was denied `SCOPE_DENIED`
                # permanently. Both directions pinned in `tests/test_actions.py`.
                asof,
            )
            if result is not None:
                _record_rule(
                    trace,
                    rule,
                    obj_type,
                    obj_id,
                    level,
                    result,
                    (
                        trace.last_resolved_path
                        if trace is not None
                        else parent_path
                    ),
                    tuple(parents) if trace is not None else (),
                )
                return result
        _record_rule(
            trace,
            rule,
            obj_type,
            obj_id,
            level,
            None,
            scope_path,
            tuple(parents) if trace is not None else (),
        )
        return None

    if isinstance(rule, CustomResolver):
        # `asof` is deliberately NOT threaded in here. The callable
        # is author code handed the raw `Store`, so its answer for a retired
        # object is its own to define; swapping what `read_current` returns
        # underneath it would change author semantics silently. The visible
        # consequence: a `CustomResolver`-scoped target retired through
        # `ActionContext.retire` resolves `None` and the action target gate
        # denies `SCOPE_DENIED` rather than reaching the handler's
        # `OBJECT_ALREADY_RETIRED`. It fails CLOSED, and a type that needs the
        # precondition refusal after retirement declares a second rule -- a
        # `DirectProperty` on a scope-key column survives it. Documented in
        # `docs/api-reference.md` and pinned in `tests/test_actions.py`.
        result = rule.fn(store, obj_type, obj_id) if rule.level == level else None
        _record_rule(
            trace, rule, obj_type, obj_id, level, result, scope_path
        )
        return result

    return None


def _record_rule(
    trace: _TraceCollector | None,
    rule: ScopeRule,
    obj_type: str,
    obj_id: str,
    requested_level: str | None,
    result: str | None,
    scope_path: tuple[ScopePathStep, ...] | None,
    candidate_parent_ids: tuple[str, ...] = (),
    *,
    resolution_kind: Literal["scope", "contributor"] = "scope",
) -> None:
    if trace is None:
        return
    if isinstance(rule, SelfScope):
        rule_kind: Literal[
            "SelfScope", "DirectProperty", "ViaLink", "CustomResolver"
        ] = "SelfScope"
    elif isinstance(rule, DirectProperty):
        rule_kind = "DirectProperty"
    elif isinstance(rule, ViaLink):
        rule_kind = "ViaLink"
    else:
        rule_kind = "CustomResolver"
    recorded_path = scope_path or (
        ScopePathStep(object_type=obj_type, object_id=obj_id),
    )
    trace.record_rule(
        ScopeRuleTrace(
            resolution_kind=resolution_kind,
            rule_kind=rule_kind,
            object_type=obj_type,
            object_id=obj_id,
            requested_level=requested_level,
            matched=result is not None,
            resolved_scope_id=result,
            scope_path=recorded_path,
            rule_level=getattr(rule, "level", None),
            property_name=(
                rule.property_name if isinstance(rule, DirectProperty) else None
            ),
            link_api_name=(
                rule.link_api_name if isinstance(rule, ViaLink) else None
            ),
            direction=rule.direction if isinstance(rule, ViaLink) else None,
            parent_type=rule.parent_type if isinstance(rule, ViaLink) else None,
            candidate_parent_ids=candidate_parent_ids,
        )
    )


def _climb_hierarchy(
    policy: ScopePolicy,
    store: Store,
    obj_type: str,
    obj_id: str,
    level: str,
    visited: set[tuple[str, str, str]],
    trace: _TraceCollector | None,
    scope_path: tuple[ScopePathStep, ...] | None,
    asof: str | None = None,
) -> str | None:
    """Hierarchy climbing: this type's own rules didn't reach `level`
    directly (no ViaLink chain happened to land on a type whose rules
    cover it). Resolve the nearest narrower level for THIS object instead
    -- possibly via a `DirectProperty` shortcut with no backing link --
    then treat the resulting id as an instance of whichever object type
    declares `SelfScope(level=narrower)` (the "canonical" type for that
    level, e.g. Team for "team") and ask THAT type's own rules for the
    originally requested (broader) level. Mirrors the prototype's
    `_resolve_company_id`, which always resolves team_id first and then
    asks `_resolve_company_id(store, "Team", team_id)`."""
    if level not in policy.levels:
        return None
    level_index = policy.levels.index(level)
    for narrower_level in reversed(policy.levels[:level_index]):
        narrow_id = _resolve_level(
            policy,
            store,
            obj_type,
            obj_id,
            narrower_level,
            visited,
            trace,
            scope_path,
            asof,
        )
        if narrow_id is None:
            continue
        canonical_type = _canonical_type_for_level(policy, narrower_level)
        if canonical_type is None:
            continue
        result = _resolve_level(
            policy,
            store,
            canonical_type,
            narrow_id,
            level,
            visited,
            trace,
            (
                scope_path
                if trace is None
                else (scope_path or ())
                + (ScopePathStep(object_type=canonical_type, object_id=narrow_id),)
            ),
            # A DIFFERENT object -- the canonical instance for the narrower
            # level -- and it is admitted on the same point-in-time terms as
            # the `ViaLink` parent hop, for the same reason: a canonical
            # instance that was live when the target closed still answers for
            # it, one already retired by then does not. The call above it
            # passes the same instant because it re-asks about THIS object at
            # a narrower level.
            asof,
        )
        if result is not None:
            return result

    return None


def _canonical_type_for_level(policy: ScopePolicy, level: str) -> str | None:
    """The object type whose own rules declare `SelfScope(level=level)` --
    i.e. the type that IS a scope of `level` (e.g. "team" -> "Team")."""
    for obj_type, rule_list in policy.rules.items():
        for rule in rule_list:
            if isinstance(rule, SelfScope) and rule.level == level:
                return obj_type
    return None


def resolve_owning_scope(
    policy: ScopePolicy,
    store: Store,
    obj_type: str,
    obj_id: str,
    trace: _TraceCollector | None = None,
    *,
    include_retired: bool = False,
) -> dict[str, str | None]:
    """Resolve every declared scope level for `(obj_type, obj_id)`.

    Returns a `level -> scope id` dict covering every level in
    `policy.levels`. A nonexistent object or an unresolvable chain for a
    given level maps that level to `None` -- callers (`covers_scope`) must
    treat that as "cannot prove coverage", never as "visible to everyone".

    The `include_retired=True` frame, which only the target gate passes:

    A retired target resolves the scope it owned **as of the instant its own
    row closed**. The links on the chain are read at that instant on both
    bounds, so an edge the object had already left cannot answer for it.

    Where a `ViaLink` hop finds more than one parent at that instant, the first
    parent whose chain resolves wins, over an order the store declares rather
    than each backend's own row order: earliest link `valid_from` first, then
    lowest parent id, compared byte-wise. It is not creation order — `links`
    declares no monotonic key, so two links made in one clock tick are
    separated by id, not by which was written first. Every backend answers in
    that order because it decides which scope owns the object, and therefore
    which operator this gate admits.

    An ancestor object is admitted when it had not already been retired by then
    — but it is read at its newest row, so its payload, and any
    `DirectProperty` key taken off that payload, is the one it carries now: an
    ancestor updated after the instant answers with the scope it is in today,
    not the one it was in then. Making the object side point-in-time too needs
    an as-of read of an object's history, which `Store` does not have. A live
    target has no closing instant and resolves in the present tense, exactly as
    a consumer read does, so this gate loosens nothing for an object that still
    exists.

    `read_asof` -- the as-of read of an object's history that would make the
    object side genuinely point-in-time -- is filed, not shipped. In code
    that live-target case is `asof` staying `None`, so every read below is
    the present-tense one.

    It exists for exactly one caller -- `ActionExecutor`'s target gate, which
    must tell "outside your scope" apart from "already retired" -- and must
    stay off for consumer
    reads: a retired or erased row that still resolves puts its children back
    in a reader's visible set, turning a min-N refusal into a released
    aggregate.

    Point-in-time rather than merely "retired rows allowed", because the two
    fail in opposite directions and both are authorization defects. Resolving
    ancestors from `read_last` unconditionally let a retired ancestor beat a
    live one and authorize an operator who no longer owns the row. Resolving
    them live-only refused an ancestor that was alive when the target closed
    and is the only route to a scope that is alive now, denying the operator
    who does own it, permanently -- a disbanded team cannot be un-retired.
    One instant answers both: at the target's `valid_to` the retired
    competitor was already closed, and the ancestor that outlived it was not.

    Where the chain resolves and the consumer covers it, the gate reaches the
    handler's own refusal (`OBJECT_ALREADY_RETIRED`). `SCOPE_DENIED` does not
    mean one thing: it is raised at two places. One is a defense-in-depth
    refusal of a scope-bearing parameter that is not a `str` — parameter
    validation rejects that first, so it is a floor under type confusion rather
    than a path in normal use. The other fires wherever coverage cannot be
    shown, and that is two situations rather than one: the chain resolved and
    this consumer is outside it, which is the ordinary denial this gate does
    not change; or the chain did not resolve at that instant, and
    deny-by-default denies. Only the second belongs to this frame. Four rule
    kinds are declared, and each meets retirement and erasure at its OWN hop:

    - `SelfScope` answers with the object's own id, which neither retirement
      nor erasure takes away.
    - `DirectProperty` reads the scope key off the payload, and
      `erase_object_content` blanks by design what that rule reads, which no
      history-aware read can recover. Retirement leaves the payload alone,
      because this gate reads the newest row rather than the live one, so
      erasure is the only lifecycle event this hop's OWN READ loses to. That is
      the target itself when the target is `DirectProperty`-scoped; it is
      equally an ANCESTOR whose own hop is `DirectProperty`, which denies a
      `ViaLink`-scoped target whose link erasure preserved. Erasure destroying
      the scope key is the point of erasure, not a gap in the gate.
    - `ViaLink` climbs the `links` table, which erasure preserves, so erasure
      costs this hop nothing. It resolves to nothing when none of the parents
      it reaches resolves in turn — among them a parent already retired BEFORE
      the target closed: its edge may still be readable at that instant, but
      its own row is not admitted, so it cannot answer at the one instant this
      gate asks about. One retired after the target — including in the same
      cascade tick — still answers for it.
    - `CustomResolver` is author code handed the raw `Store`, and the engine
      does not reach inside it, so what a retired or erased object resolves to
      is the resolver's own business rather than this frame's. The natural body
      reads `read_current`, which is `None` for a retired object, so a resolver
      written that way denies. A type that needs the precondition refusal after
      retirement declares a second rule — a `DirectProperty` on a scope-key
      column, which survives retirement.

    Each bullet is about one hop, never about one target, and the four are not
    the whole chain. `ScopePolicy.rules` maps each type to an ORDERED list, so
    a target declares as many of these hops as that list holds and is answered
    by the first that resolves; and a level no rule of its own can answer
    climbs to the canonical instance of a narrower scope, where a type declares
    one — which is none of the four. What the engine's own hops share is the
    frame: any object the engine has to read that had already been retired
    before the target closed is refused there, whichever of those hops reached
    it — so the hop that answers a target's level can fail on an object the
    target's other hops never touch. A `CustomResolver` is outside that frame
    only for the reads its own callable makes: the engine does not thread the
    instant into author code, so an ancestor the callable reaches for itself is
    read however it reads it, retired or not. Its ANSWER re-enters the engine,
    and every object the engine reads from there is refused on the frame's own
    terms — the canonical instance a narrower answer names, and any object
    whose rules the engine goes on to ask, whose row is checked before its own
    resolver runs.

    Every denial in this list fails closed — the object's own owner is denied,
    nothing is disclosed.
    """
    asof: str | None = None
    if include_retired:
        target = store.read_last(obj_type, obj_id)
        if target is not None:
            asof = target.lineage.valid_to
    return {
        level: _resolve_level(
            policy,
            store,
            obj_type,
            obj_id,
            level,
            set(),
            trace,
            None,
            asof,
        )
        for level in policy.levels
    }


def _resolve_contributor(
    policy: ScopePolicy,
    store: Store,
    obj_type: str,
    obj_id: str,
    visited: set[tuple[str, str]],
    trace: _TraceCollector | None = None,
    scope_path: tuple[ScopePathStep, ...] | None = None,
) -> str | None:
    key = (obj_type, obj_id)
    if key in visited or len(visited) >= _MAX_DEPTH:
        return None
    visited = visited | {key}

    # `read_current`, deliberately, where `_resolve_level` reads the newest
    # row: contributor identity is only ever asked of a row a query just
    # read, which is current by construction. Widening it here would change
    # what min-N counts, not what a scope gate can see.
    obj = store.read_current(obj_type, obj_id)
    if obj is None:
        return None

    if trace is not None and scope_path is None:
        scope_path = (ScopePathStep(object_type=obj_type, object_id=obj_id),)

    result: str | None
    for rule in policy.contributor_rules.get(obj_type, []):
        if isinstance(rule, SelfScope):
            result = str(obj_id)
            _record_rule(
                trace,
                rule,
                obj_type,
                obj_id,
                None,
                result,
                scope_path,
                resolution_kind="contributor",
            )
            return result

        if isinstance(rule, DirectProperty):
            value = obj.payload.get(rule.property_name)
            if value is not None:
                result = str(value)
                _record_rule(
                    trace,
                    rule,
                    obj_type,
                    obj_id,
                    None,
                    result,
                    scope_path,
                    resolution_kind="contributor",
                )
                return result
            _record_rule(
                trace,
                rule,
                obj_type,
                obj_id,
                None,
                None,
                scope_path,
                resolution_kind="contributor",
            )
            continue

        if isinstance(rule, ViaLink):
            if rule.direction == "from":
                parents = store.links_from(rule.link_api_name, obj_id)
            else:
                parents = store.links_to(rule.link_api_name, obj_id)
            for parent_id in parents:
                parent_path = None
                if trace is not None:
                    parent_path = (scope_path or ()) + (
                        ScopePathStep(
                            object_type=rule.parent_type,
                            object_id=parent_id,
                            via_link=rule.link_api_name,
                            direction=rule.direction,
                        ),
                    )
                result = _resolve_contributor(
                    policy,
                    store,
                    rule.parent_type,
                    parent_id,
                    visited,
                    trace,
                    parent_path,
                )
                if result is not None:
                    _record_rule(
                        trace,
                        rule,
                        obj_type,
                        obj_id,
                        None,
                        result,
                        (
                            trace.last_resolved_path
                            if trace is not None
                            else parent_path
                        ),
                        tuple(parents) if trace is not None else (),
                        resolution_kind="contributor",
                    )
                    return result
            _record_rule(
                trace,
                rule,
                obj_type,
                obj_id,
                None,
                None,
                scope_path,
                tuple(parents) if trace is not None else (),
                resolution_kind="contributor",
            )
            continue

        if isinstance(rule, CustomResolver):
            result = rule.fn(store, obj_type, obj_id)
            _record_rule(
                trace,
                rule,
                obj_type,
                obj_id,
                None,
                result,
                scope_path,
                resolution_kind="contributor",
            )
            if result is not None:
                return result
            continue

    return None


def resolve_contributor(
    policy: ScopePolicy,
    store: Store,
    obj_type: str,
    obj_id: str,
    trace: _TraceCollector | None = None,
) -> str | None:
    """Resolve "the identity behind a row" (e.g. a Response's authoring
    Person) for one object, via `policy.contributor_rules.get(obj_type)`
    (see `ScopePolicy.contributor_rules`). Tries rules in declaration order,
    first non-`None` result wins -- same fallback-chain semantics as
    `resolve_owning_scope`/`_resolve_level`, minus the scope-level hierarchy
    (contributor resolution has no "level", no hierarchy climbing).

    Returns `None` when the object doesn't exist, the type has no
    contributor rules at all, or every declared rule dead-ends. Note that a
    retired object reads back as `None` here, so retirement (and closing a
    `ViaLink` link) turns a previously resolvable row unresolved --
    `GuardedQuery._contributor_count` therefore counts all unresolved rows as
    ONE unknown identity between them, never one apiece, so losing the ability
    to see who wrote a row can never enlarge a min-N population.
    """
    return _resolve_contributor(policy, store, obj_type, obj_id, set(), trace)
