"""A type cannot be both unscoped and scope-routed (remediation Track D).

`ScopePolicy.validate()` accepted a type listed in `unscoped_types` while that
same type also declared `rules`, and the engine then silently picked one of the
two meanings: `GuardedQuery._visible` short-circuits the scope check for an
unscoped type, so the declared rule became dead.

The declaration is incoherent, but the reason it matters is not that a rule goes
unused -- it is that the two halves combine into a disclosure oracle that
NEITHER HALF OPENS ALONE:

  - `_scope_key_fields` is fed by `policy.rules`, so a `DirectProperty` rule on a
    sensitivity-hidden property keeps that property exempt from the
    supplied-value `where=` gate. On its own this is the documented residual
    (`_scope_key_fields`' own docstring): a consumer may supply a scope-routing
    value it already holds, and the scope check still bounds which rows can come
    back.
  - `unscoped_types` removes that bound, so every row of the type is visible.

Together, a consumer probes the hidden scope key of rows OUTSIDE its own scope
and reads membership off the result. Measured against `78cdc5a`: `rules` alone
answered `0` to both a right and a wrong guess (no signal); `unscoped_types`
alone answered `VISIBILITY_DENIED` to both (no channel); the intersection
answered `3` and `0`, and a bisection recovered a foreign `group_id` in three
queries over eight candidates -- on `GuardedQuery`, `OntologyClient` and over
the MCP wire, on a policy that had passed `validate()`.

Both halves are pinned here. The declaration half says it at startup; the
runtime half is what actually closes the door, because a policy reaches a query
without `validate()` ever being called (`OntologyClient` constructs and serves
one) and because `unscoped_types` is a plain mutable set that can be added to
after any check has run.

WHY THE RUNTIME REFUSAL SITS AT THE PUBLIC ENTRY, NOT AT `_visible`: refusing
per row is itself an oracle. Measured -- with the check at `_visible` only, a
right guess raised `SCOPE_POLICY_ERROR` while a wrong guess returned `0`,
because no row survived the `where` filter for `_visible` to object to. The
refusal has to happen before any row is read, so it cannot depend on the
selection. `test_the_refusal_does_not_depend_on_whether_rows_match` is that
receipt.

The input the suite structurally could not construct: nothing. Six fixtures in
`tests/test_query.py` build this exact state with `policy.unscoped_types.add(...)`
-- as a convenience to widen a view, never as a subject. What did not exist was
any assertion ABOUT it, and no fixture reached the same broad view legitimately,
so there was nothing to compare the illegitimate one against. Those six now use
a type routed at two levels (`test_query.py::_shared_field_policy`), which
reproduces every precondition -- broad visibility plus a live scope-key
exemption on a hidden property -- without the incoherent declaration.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import raises_code

from ontary import Consumer, ObjectStore, Ontology, OntologyObject, Sensitivity, Source, prop
from ontary.client import OntologyClient
from ontary.diagnose import _collect_findings
from ontary.errors import ValidationFailed
from ontary.mcp_server import build_mcp_server
from ontary.meta import (
    Cardinality,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
)
from ontary.ontology import OntologyDef
from ontary.query import GuardedQuery
from ontary.scope import DirectProperty, ScopePolicy, ScopeRule

SRC = Source(source_system="test")

# Scoped to the FOREIGN group, so every probe below asks about rows this
# consumer's own scope rule would never have shown it.
CONSUMER = Consumer(
    actor_id="attacker",
    role="Member",
    scope_level="group",
    scope_id="g-attacker",
    kind="human",
)
SECRET = "amy-4402"


def _registry() -> OntologyRegistry:
    """`RecordB.group_id` is simultaneously a `DirectProperty` scope key and
    hidden from humans -- the repo's own `_shared_field_registry` shape, which
    is what makes the scope-key exemption apply to a sensitive property."""
    registry = OntologyRegistry()
    registry.register_object_type(
        ObjectTypeDef(
            api_name="RecordB",
            display_name="RecordB",
            description="Scope-routed on a property that is hidden from humans",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="group_id", type="str", required=False),
                PropertyDef(name="score", type="float", required=False),
            ],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Holder",
            display_name="Holder",
            description="Traversal source, so `traverse` has a link to walk",
            layer="L0",
            properties=[PropertyDef(name="id", type="str")],
            primary_key="id",
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="holds",
            display_name="holds",
            description="Holder -> RecordB",
            from_type="Holder",
            to_type="RecordB",
            cardinality=Cardinality.ONE_TO_MANY,
        )
    )
    group_id = next(
        prop_def
        for prop_def in registry.get_object_type("RecordB").properties
        if prop_def.name == "group_id"
    )
    group_id.sensitivity = Sensitivity(ai_usable=True, human_visible=False)
    return registry


def _policy(
    *,
    unscoped: bool,
    ruled: bool = True,
    contributor: bool = False,
    row_visibility: bool = False,
    also_holder: bool = False,
) -> ScopePolicy:
    rules: dict[str, list[ScopeRule]] = {}
    if ruled:
        rules["RecordB"] = [DirectProperty(level="group", property_name="group_id")]
    if also_holder:
        rules["Holder"] = [DirectProperty(level="group", property_name="id")]
    unscoped_types = set()
    if unscoped:
        unscoped_types.add("RecordB")
        if also_holder:
            unscoped_types.add("Holder")
    return ScopePolicy(
        levels=["person", "group"],
        unscoped_types=unscoped_types,
        rules=rules,
        contributor_rules=(
            {"RecordB": [DirectProperty(level="person", property_name="id")]}
            if contributor
            else {}
        ),
        row_visibility=(
            {"RecordB": lambda _store, _consumer, _type, _payload: True}
            if row_visibility
            else {}
        ),
        min_n=3,
    )


def _store(registry: OntologyRegistry) -> ObjectStore:
    store = ObjectStore(registry)
    store.insert("Holder", {"id": "g-attacker"}, SRC)
    store.insert(
        "RecordB", {"id": "mine-1", "group_id": "g-attacker", "score": 1.0}, SRC
    )
    for index, score in enumerate([10.0, 20.0, 30.0]):
        store.insert(
            "RecordB", {"id": f"far-{index}", "group_id": SECRET, "score": score}, SRC
        )
        store.create_link("holds", "g-attacker", f"far-{index}")
    return store


def _incoherent() -> tuple[OntologyDef, ObjectStore]:
    registry = _registry()
    definition = OntologyDef(
        name="scopeint", registry=registry, policy=_policy(unscoped=True)
    )
    return definition, _store(registry)


# -- the declaration half ---------------------------------------------------


def test_a_type_cannot_be_both_unscoped_and_scope_routed() -> None:
    """Finding 10's own sentence: the engine should say so at startup rather
    than silently pick one of the two meanings."""
    registry = _registry()

    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR") as excinfo:
        _policy(unscoped=True).validate(registry)

    assert "RecordB" in str(excinfo.value)


def test_the_refusal_names_every_offending_type() -> None:
    """Two types, both incoherent. A refusal that stops at the first one sends
    the author round the loop once per type, and a single-type fixture cannot
    tell that apart from a complete answer."""
    registry = _registry()

    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR") as excinfo:
        _policy(unscoped=True, also_holder=True).validate(registry)

    message = str(excinfo.value)
    assert "RecordB" in message
    assert "Holder" in message


def test_ontology_validate_refuses_the_intersection() -> None:
    """The entry point an author actually calls (`ontology.validate()` ->
    `OntologyDef.validate`), not just the policy method underneath it."""
    definition, _store_ = _incoherent()

    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR"):
        definition.validate()


def test_diagnose_reports_the_intersection_without_raising() -> None:
    """`diagnose` exists to report every defect in one sweep. A rule that only
    `validate()` knows about is invisible to the author running
    `ontary validate` -- and `_collect_findings` reported nothing here before
    this change."""
    definition, _store_ = _incoherent()

    findings = _collect_findings(definition)

    offending = [f for f in findings if f.code == "SCOPE_POLICY_ERROR"]
    assert offending, [f.message for f in findings]
    assert any("RecordB" in f.message for f in offending)


# -- companions: what the guard must NOT refuse (rstaff L31) ----------------


def test_unscoped_without_scope_rules_is_accepted() -> None:
    """`unscoped_types` is a declared, supported answer on its own. A guard
    that refuses the bucket instead of the overlap breaks every ontology with
    a reference type in it."""
    _policy(unscoped=True, ruled=False).validate(_registry())


def test_scope_rules_without_unscoped_are_accepted() -> None:
    _policy(unscoped=False).validate(_registry())


def test_unscoped_with_contributor_rules_is_accepted() -> None:
    """NOT the same defect, and this one IS reachable from the authoring sugar
    (`scope="unscoped", contributor=[...]`), so refusing it would break a legal
    declaration. A contributor rule resolves an IDENTITY, not an owning scope
    -- `ScopePolicy` says `rule.level` is not meaningful there -- and measured
    against `78cdc5a` it does not feed `_scope_key_fields`, so it opens no
    exemption."""
    _policy(unscoped=True, ruled=False, contributor=True).validate(_registry())


def test_unscoped_with_row_visibility_is_accepted() -> None:
    """`query.py` states this combination is intended: "scope-unscoped does not
    imply row-visibility-unscoped". The predicate is applied ON TOP OF the
    scope check, never instead of it."""
    _policy(unscoped=True, ruled=False, row_visibility=True).validate(_registry())


# -- the runtime half -------------------------------------------------------


def _guarded(definition: OntologyDef, store: ObjectStore) -> GuardedQuery:
    return GuardedQuery(store, definition.registry, definition.policy)


def _reads(definition: OntologyDef, store: ObjectStore) -> dict[str, Any]:
    """Every public read entry that takes an object type.

    `count`/`exists` delegate to `get_objects`, and `aggregate`/`aggregate_by`
    to `_aggregate`, but they are listed separately anyway: the ledger records
    two fixes on this branch that threaded one spelling of a shared path and
    left the other releasing silently (G2/M6b, B1's typed-vs-string split).
    """
    guarded = _guarded(definition, store)
    return {
        "get_objects": lambda: guarded.get_objects(CONSUMER, "RecordB", limit=None),
        "get_object": lambda: guarded.get_object(CONSUMER, "RecordB", "far-0"),
        "count": lambda: guarded.count(CONSUMER, "RecordB"),
        "exists": lambda: guarded.exists(CONSUMER, "RecordB"),
        "aggregate": lambda: guarded.aggregate(CONSUMER, "RecordB", "score"),
        "aggregate_by": lambda: guarded.aggregate_by(
            CONSUMER, "RecordB", "score", "id"
        ),
        "count_contributors": lambda: guarded.count_contributors(CONSUMER, "RecordB"),
        "traverse": lambda: guarded.traverse(CONSUMER, "holds", "g-attacker"),
    }


@pytest.mark.parametrize(
    "entry",
    [
        "get_objects",
        "get_object",
        "count",
        "exists",
        "aggregate",
        "aggregate_by",
        "count_contributors",
        "traverse",
    ],
)
def test_every_read_entry_refuses_an_incoherent_policy(entry: str) -> None:
    """`validate()` was never called on this definition -- which is the whole
    reason the runtime half exists. `OntologyClient` constructs and serves an
    unvalidated policy, and `unscoped_types` is a mutable set that can be added
    to after any startup check has already passed."""
    definition, store = _incoherent()

    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR"):
        _reads(definition, store)[entry]()


def test_the_refusal_does_not_depend_on_whether_rows_match() -> None:
    """THE seam pin. A refusal raised per row is still an oracle.

    Measured with the check at `_visible` only: a right guess raised
    `SCOPE_POLICY_ERROR` and a wrong guess returned `0`, because no row
    survived the `where` filter for `_visible` to object to -- the caller
    still learns which guess was right, off the refusal instead of the count.
    The check must run before any row is read, so both spellings must be
    indistinguishable.
    """
    definition, store = _incoherent()
    guarded = _guarded(definition, store)

    outcomes = []
    for guess in (SECRET, "amy-4403"):
        with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR") as excinfo:
            guarded.count(CONSUMER, "RecordB", where={"group_id": guess})
        outcomes.append(str(excinfo.value))

    assert outcomes[0] == outcomes[1]


def test_a_coherent_policy_still_reads() -> None:
    """Don't break what worked. Same fixture, same consumer, `unscoped_types`
    empty -- the scope rule applies and returns this consumer's own row."""
    registry = _registry()
    definition = OntologyDef(
        name="scopeint", registry=registry, policy=_policy(unscoped=False)
    )
    store = _store(registry)

    rows = _guarded(definition, store).get_objects(CONSUMER, "RecordB", limit=None)

    assert [row.payload["id"] for row in rows] == ["mine-1"]


def test_an_unrelated_type_still_reads_under_a_partly_incoherent_policy() -> None:
    """The refusal is scoped to the type that is actually incoherent. A policy
    with one bad entry must not take every other type down with it -- that
    would turn a declaration defect into an outage."""
    registry = _registry()
    definition = OntologyDef(
        name="scopeint",
        registry=registry,
        policy=_policy(unscoped=True, ruled=True),
    )
    store = _store(registry)

    # Holder is in neither bucket -- unresolvable scope, so it fails closed and
    # returns nothing. The point is that it ANSWERS: an incoherent entry for
    # RecordB must not turn every other type's reads into policy complaints.
    rows = _guarded(definition, store).get_objects(CONSUMER, "Holder", limit=None)

    assert rows == []


# -- the defect, end to end -------------------------------------------------


def test_the_intersection_cannot_probe_a_foreign_hidden_scope_key() -> None:
    """D1 itself. Before the fix this bisection recovered `amy-4402` in three
    queries over eight candidates -- the finding-8 shape, but over the whole
    population instead of the consumer's own scope, because `unscoped_types`
    removed the bound that made the residual acceptable."""
    definition, store = _incoherent()
    guarded = _guarded(definition, store)
    candidates = [
        "amy-4400",
        "amy-4401",
        SECRET,
        "amy-4403",
        "bob-1",
        "bob-2",
        "cid-9",
        "dee-0",
    ]

    remaining = candidates
    while len(remaining) > 1:
        half = remaining[: len(remaining) // 2]
        with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR"):
            guarded.count(CONSUMER, "RecordB", where={"group_id": {"in": half}})
        # No signal came back, so the search cannot narrow. Take the branch a
        # working oracle would NOT have chosen, to prove the loop is blind.
        remaining = remaining[len(remaining) // 2 :]

    assert remaining == ["dee-0"]
    assert remaining != [SECRET]


def test_refused_through_the_client_surface() -> None:
    definition, store = _incoherent()
    client = OntologyClient(definition, store, CONSUMER)

    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR"):
        client.count("RecordB", where={"group_id": SECRET})


def test_refused_over_the_mcp_wire() -> None:
    """Over the wire the number came back as `{"result": 3}` against a wrong
    guess's `{"result": 0}`. A refusal that exists in-process and not on the
    serving surface is the same disclosure."""
    definition, store = _incoherent()
    server = build_mcp_server(definition, store, CONSUMER)

    payload = asyncio.run(
        server.call_tool(
            "count_objects",
            {"obj_type": "RecordB", "where": {"group_id": SECRET}},
        )
    )[1]

    assert payload["error"]["code"] == "SCOPE_POLICY_ERROR"


def test_the_authoring_sugar_still_cannot_build_the_intersection() -> None:
    """`_build_policy` is `if/elif` on `scope=`, so a class-authored ontology
    puts a type in exactly one bucket. This pins that property rather than
    assuming it: if the sugar ever grew a second way to reach `unscoped_types`,
    every ontology written with it would start failing at the new guard, and
    this test is what says so first."""
    ontology = Ontology(name="sugar", scope_levels=["person", "group"])

    @ontology.object(layer="L0", scope="unscoped")
    class Record(OntologyObject):
        id: str = prop(primary_key=True)

    policy = ontology.definition.policy

    assert policy.unscoped_types == {"Record"}
    assert set(policy.unscoped_types) & set(policy.rules) == set()
