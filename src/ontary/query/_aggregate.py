"""min-N aggregation: grouped reductions, release, and contributor counts.

The prototype's `_contributor_count` de-duplicated contributors via a
hardcoded `person_id` property / `byPerson` link (a Response/Person-specific
anti-gaming rule). The generalized replacement is
`ScopePolicy.contributor_rules` -- a per-type, declarative resolution list
reusing the SAME `ScopeRule` machinery as scope resolution
(`DirectProperty`/`ViaLink`/etc.) -- plus `ontary.scope.resolve_contributor`.
`aggregate` counts DISTINCT resolved contributors per group for any object
type an ontology author declares contributor rules for (a row whose
contributor fails to resolve counts as its own row, mirroring the prototype's
fallback); a type with NO contributor rules declared falls back to a plain
row count -- the still-real, domain-agnostic floor every ontology gets for
free with zero declarations.
"""

from __future__ import annotations

from typing import Any, Final, Literal, final

from ontary.errors import ValidationFailed, VisibilityError
from ontary.query._disclosure import _ReadDisclosure, _record_disclosure
from ontary.query._host import _QueryHost
from ontary.query._params import _NUMERIC_PROPERTY_TYPES, _property_type
from ontary.query._where import _compile_where, _NormalizedWhere, _validate_where_keys
from ontary.scope import _ScopeReadCache, resolve_contributor
from ontary.security import Consumer
from ontary.store import StoredObject

_MIN_N_COUNT_WITHHELD = "count withheld"
_AGGREGATE_FUNCS = ("mean", "count", "sum", "min", "max")

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


def _where_gate(
    query: _QueryHost,
    consumer: Consumer,
    obj_type: str,
    disclosure: _ReadDisclosure,
) -> None:
    """Refuse a `where` that filters on a field hidden from `consumer`.

    Shared by `_aggregate` and `count_contributors` so the two cannot
    drift apart: a filter oracle closed on one path and left open on the
    other is the same disclosure either way.
    """
    _validate_where_keys(query._registry, obj_type, disclosure.where)
    if disclosure.where_fields:
        denied_keys = {
            field
            for field, field_disclosure in disclosure.where_fields
            if field
            in query._hidden_fields(
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
    query: _QueryHost,
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
    where_matcher = _compile_where(query._registry, obj_type, where)
    visible = []
    for row in query._store.read_all(obj_type):
        if where_matcher is not None and not where_matcher(row.payload):
            continue
        if not query._visible(consumer, row, scope_cache=scope_cache):
            continue
        visible.append(row)
    return visible


def _value_field_gate(
    query: _QueryHost,
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
        query._policy.contributor_rules.get(obj_type)
    )
    value_field_hidden = value_field in query._hidden_fields(
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
    query: _QueryHost,
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
    disclosure = query._require_coherent_scope(
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
    query._registry.get_object_type(obj_type)

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
    releasing = value_field is not None and _value_field_gate(
        query,
        consumer,
        obj_type,
        value_field,
        func,
        _author_dispatch,
        disclosure,
    )

    # The where gate classifies each predicate shape independently from
    # group_by, whose returned dictionary keys are always learned values.
    _where_gate(query, consumer, obj_type, disclosure)
    group_by_hidden = query._hidden_fields(
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
        _property_type(query._registry, obj_type, value_field)
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

    visible = _visible_rows(
        query,
        consumer,
        obj_type,
        normalized_where,
        scope_cache=scope_cache,
    )

    min_n = query._policy.min_n
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
    group_key_hidden = bool(group_by) and group_by in query._hidden_fields(
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
        contributor_count = _contributor_count(query, obj_type, value_rows)
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
            _release_group(
                out, released_from, obj_type, group_by, key, aggregate_value
            )
    if value_field is not None:
        _record_disclosure(_disclosures, releasing, obj_type, value_field)
    return out if group_by else aggregate_value


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
    query: _QueryHost,
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
    if obj_type not in query._policy.contributor_rules:
        return len(group_rows)

    contributor_ids: set[str] = set()
    unresolved_rows = 0
    for row in group_rows:
        resolved = resolve_contributor(
            query._policy,
            query._store,
            obj_type,
            row.lineage.object_id,
        )
        if resolved is not None:
            contributor_ids.add(resolved)
        else:
            unresolved_rows += 1
    return len(contributor_ids) + (1 if unresolved_rows else 0)
