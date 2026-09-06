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
  parent is tried in link order and the first one whose chain resolves
  wins (mirrors the prototype's Person fan-out over multiple memberships).
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


class ScopePolicy(BaseModel):
    """A complete, per-ontology declaration of how owning scope resolves.

    - `levels`: the scope-level hierarchy, narrowest first.
    - `unscoped_types`: object types with no owning scope at all (visible to
      any consumer regardless of scope -- the generalized replacement for
      `query._UNSCOPED_TYPES`).
    - `rules`: object type api_name -> ordered list of resolution rules.
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
    # debt).
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


def _resolve_level(
    policy: ScopePolicy,
    store: Store,
    obj_type: str,
    obj_id: str,
    level: str,
    visited: set[tuple[str, str, str]],
    trace: _TraceCollector | None = None,
    scope_path: tuple[ScopePathStep, ...] | None = None,
) -> str | None:
    key = (obj_type, obj_id, level)
    if key in visited or len(visited) >= _MAX_DEPTH:
        return None
    visited = visited | {key}

    obj = store.read_current(obj_type, obj_id)
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
            result = _resolve_level(
                policy,
                store,
                rule.parent_type,
                parent_id,
                level,
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
) -> dict[str, str | None]:
    """Resolve every declared scope level for `(obj_type, obj_id)`.

    Returns a `level -> scope id` dict covering every level in
    `policy.levels`. A nonexistent object or an unresolvable chain for a
    given level maps that level to `None` -- callers (`covers_scope`) must
    treat that as "cannot prove coverage", never as "visible to everyone".
    """
    return {
        level: _resolve_level(
            policy, store, obj_type, obj_id, level, set(), trace
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
    contributor rules at all, or every declared rule dead-ends -- callers
    (`GuardedQuery._contributor_count`) treat an unresolved contributor as
    "this row is its own contributor" (row-count fallback), mirroring the
    prototype's `_contributor_count`.
    """
    return _resolve_contributor(policy, store, obj_type, obj_id, set(), trace)
