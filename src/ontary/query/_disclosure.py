"""Scope and row visibility, redaction, and read disclosure."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ontary.audit import AuditEntry, EmittedEvent
from ontary.errors import ValidationFailed
from ontary.meta import OntologyRegistry, PropertyDef
from ontary.query._params import _HiddenFields, _validate_group_by
from ontary.query._where import (
    _is_supplied_value,
    _normalize_where,
    _NormalizedCondition,
    _NormalizedWhere,
)
from ontary.scope import (
    DirectProperty,
    RowVisibilityStore,
    ScopePolicy,
    _ScopeReadCache,
    incoherent_scope_declarations,
    resolve_owning_scope,
)
from ontary.security import Consumer, covers_scope
from ontary.store import Store, StoredObject
from ontary.store._shared import iso_instant


def _where_disclosure(
    condition: _NormalizedCondition,
) -> Literal["supplied", "learned"]:
    """Classify only predicate shape for the pre-validation field gate.

    Bare equality and a LONE ``in`` over an explicit list of values are
    supply-shaped: the caller must already possess the values being named.
    Every other shape is learning-shaped, including malformed clauses, a
    multi-operator mapping (even one that contains an ``in`` -- pairing it
    with a range or a ``contains`` would turn the supply into a probe on the
    value), and any operand that supplies no value (see `_is_supplied_value`),
    so hidden fields fail closed before operator validation can reveal
    anything about them.
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
    so field gates never re-derive it. ``narrows_population`` records whether
    the selection narrows the population; the hidden aggregate-value exemption
    consumes this flag.
    """

    where: _NormalizedWhere | None
    where_fields: tuple[tuple[str, Literal["supplied", "learned"]], ...]
    group_by: str | None

    @property
    def narrows_population(self) -> bool:
        return bool(self.where) or self.group_by is not None



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
    declared a `DirectProperty` rule with the same field name. Pooling this
    set globally meant declaring `DirectProperty("person_id")` on ONE type
    (e.g. Team, where a
    team-scoped consumer legitimately supplies its own `person_id`-shaped
    routing key) silently exempted `person_id` on EVERY OTHER type too --
    including a type where `person_id` is a `human_visible=False` identity
    field, re-opening exactly the de-anonymization oracle the prototype's
    own reviewer history (see the comment on `dso.query._SCOPE_KEY_FIELDS`)
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


def _require_coherent_scope(
    registry: OntologyRegistry,
    policy: ScopePolicy,
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

    Type resolution comes first, so an undeclared name never reaches a
    policy, where, or group_by verdict: it refuses `UNKNOWN_OBJECT_TYPE`.
    """
    registry.get_object_type(obj_type)
    errors = incoherent_scope_declarations(policy, obj_type)
    if errors:
        raise ValidationFailed("; ".join(errors), code="SCOPE_POLICY_ERROR")
    if group_by is not None:
        _validate_group_by(registry, obj_type, group_by)
    normalized_where = _normalize_where(where)
    return _ReadDisclosure(
        where=normalized_where,
        where_fields=tuple(
            (field, _where_disclosure(condition))
            for field, condition in normalized_where or ()
        ),
        group_by=group_by,
    )

def scope_limited(
    require_coherent_scope: Callable[..., _ReadDisclosure],
    policy: ScopePolicy,
    obj_type: str,
) -> bool:
    """Mirror the two deny branches of `_visible` using declarations only.

    Scope coverage and row visibility may deny even when every stored row
    happens to be visible; this marker never reads the store.
    Refuse an incoherent policy like every public read.
    """
    require_coherent_scope(obj_type)
    return (
        obj_type not in policy.unscoped_types
        or obj_type in policy.row_visibility
    )

def _visible(
    store: Store,
    policy: ScopePolicy,
    consumer: Consumer,
    obj: StoredObject,
    *,
    scope_cache: _ScopeReadCache | None = None,
    resolved_scope: dict[str, str | None] | None = None,
) -> bool:
    """Apply scope coverage and row policy using the read's scope frame.

    Object reads resolve the current frame here. Event reads supply the
    subject's scope, resolved at its retirement instant when it is retired.
    """
    # The V/S/R/W family in
    # `test_exists_bound_matches_unbounded_walk_over_generated_row_shapes`
    # models both rejection branches here; a new branch needs a new kind.
    # A new deny branch must update scope_limited and its tie test.
    obj_type = obj.lineage.object_type
    if obj_type not in policy.unscoped_types:
        resolved = resolved_scope
        if resolved is None:
            resolved = resolve_owning_scope(
                policy,
                store,
                obj_type,
                obj.lineage.object_id,
                cache=scope_cache,
            )
        if not covers_scope(policy, consumer, resolved):
            return False

    # `row_visibility` (see `ontary.scope.RowVisibilityFn`) is applied
    # ON TOP OF -- never instead of -- the scope check above, for EVERY
    # object type (including `unscoped_types`: scope-unscoped does not
    # imply row-visibility-unscoped). A type with no predicate declared
    # here is unaffected. The predicate is handed the object's PAYLOAD
    # only (never lineage) -- exactly the fields a declared property
    # rule could reference.
    row_visibility = policy.row_visibility.get(obj_type)
    if row_visibility is None:
        return True
    return row_visibility(
        RowVisibilityStore(store), consumer, obj_type, obj.payload
    )

# -- redaction -----------------------------------------------------

def _hidden_properties(consumer: Consumer, properties: Sequence[PropertyDef]) -> set[str]:
    return {
        prop.name for prop in properties
        if (consumer.kind == "human" and not prop.sensitivity.human_visible)
        or (consumer.kind == "ai" and not prop.sensitivity.ai_usable)
    }

def _hidden_fields(
    registry: OntologyRegistry,
    policy: ScopePolicy,
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
    obj_def = registry.get_object_type(obj_type)
    hidden = _hidden_properties(consumer, obj_def.properties)

    if disclosure == "supplied":
        # A consumer always SUPPLIES, never LEARNS, a scope-routing value
        # used by a bare eq or in over an explicit list. This exemption
        # applies only to those supply-shaped filter predicates; learning-
        # shaped predicates and order_by/group_by consult "learned"
        # because they disclose information about the value rather than
        # merely selecting rows by values the consumer already holds.
        # `_redact` still strips the same hidden fields from every row.
        return hidden - _scope_key_fields(policy, obj_type)

    # A learned value is never exempted, including when it is also a
    # scope-routing property. Aggregates, ordering, and grouping can
    # disclose information about the value rather than merely selecting
    # rows with a value the consumer already supplied.
    return hidden

def redacted_fields(
    require_coherent_scope: Callable[..., _ReadDisclosure],
    hidden_fields: _HiddenFields,
    consumer: Consumer,
    obj_type: str,
) -> tuple[str, ...]:
    """Sorted property names that `_redact` strips from returned rows.

    Refuse an incoherent policy like every public read.
    """
    require_coherent_scope(obj_type)
    return tuple(sorted(hidden_fields(consumer, obj_type, disclosure="learned")))


def _redact(
    hidden_fields: _HiddenFields,
    consumer: Consumer,
    obj_type: str,
    obj: StoredObject,
) -> StoredObject:
    """Redact hidden fields from a COPY of `obj`'s payload -- `lineage`
    is never touched (it carries no sensitivity-classified property) and
    the original `obj`/store row is never mutated (`StoredObject` is
    frozen; this always returns a new instance with a new `payload`
    dict)."""
    hidden = hidden_fields(consumer, obj_type, disclosure="learned")
    payload = {k: v for k, v in obj.payload.items() if k not in hidden}
    return StoredObject(payload=payload, lineage=obj.lineage)


def _positioned_visible_events(
    store: Store,
    registry: OntologyRegistry,
    policy: ScopePolicy,
    require_coherent_scope: Callable[..., _ReadDisclosure],
    is_visible: Callable[..., bool],
    scope_cache: _ScopeReadCache,
    consumer: Consumer,
    *,
    event_type: str | None,
    about: tuple[str, str] | None,
    since: datetime | None,
    until: datetime | None,
) -> list[tuple[tuple[int, int], AuditEntry, EmittedEvent, frozenset[str]]]:
    """`visible_events` rows, each led by its log position (#109).

    A position is ``(index in store.audit_entries(), emission index in
    that entry)``. The audit log is append-only, so a position never
    changes as the log grows; positions strictly increase in result order.
    """
    for boundary in (since, until):
        if boundary is not None:
            iso_instant(boundary)

    event_types = registry.event_types
    selected_event_types = set(event_types) if event_type is None else {event_type} & event_types.keys()
    subject_types = {
        action.target_type for action in registry.action_types.values()
        if selected_event_types.intersection(action.emits)
    }
    if about is not None:
        subject_types.add(about[0])
    for subject_type in sorted(subject_types):
        require_coherent_scope(subject_type)

    candidates = [
        ((entry_index, event_index), entry, event)
        for entry_index, entry in enumerate(store.audit_entries())
        if entry.kind == "action" and entry.outcome == "ok"
        and (since is None or entry.ts >= since)
        and (until is None or entry.ts < until)
        for event_index, event in enumerate(entry.events)
        if event.event_type in event_types
        and event.about_type in subject_types
        and (event_type is None or event.event_type == event_type)
        and (about is None or (event.about_type, event.about_id) == about)
    ]

    visible: list[tuple[tuple[int, int], AuditEntry, EmittedEvent, frozenset[str]]] = []
    for position, entry, event in candidates:
        subject = scope_cache.read_last(store, event.about_type, event.about_id)
        if subject is None:
            continue
        resolved_scope = None
        if event.about_type not in policy.unscoped_types:
            resolved_scope = resolve_owning_scope(
                policy, store, event.about_type, event.about_id,
                cache=scope_cache, include_retired=True,
            )
        if not is_visible(
            consumer, subject, scope_cache=scope_cache, resolved_scope=resolved_scope,
        ):
            continue
        hidden = frozenset(_hidden_properties(
            consumer, event_types[event.event_type].properties,
        ))
        payload = {key: value for key, value in event.payload.items() if key not in hidden}
        visible.append((position, entry, event.model_copy(update={"payload": payload}), hidden))
    return visible



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
