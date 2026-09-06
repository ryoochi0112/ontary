"""Functions have an audit boundary (M7).

Before this, `FunctionRegistry.call` persisted nothing: a function's capability
accesses were *enforced* but invisible afterwards, so an auditor asking "what
did the AI reach for?" had no answer. Actions owned a transaction and an audit
record; functions owned neither.

Auditing is deliberately conditional -- see `FunctionDef.audited`. Functions run
far more often than actions, so a flat "audit everything" would be real write
amplification for little gained on functions that cannot reach outside the
process at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import raises_code

from ontary import (
    BoundQuery,
    Consumer,
    ObjectStore,
    Ontology,
    OntologyObject,
    SelfScope,
    prop,
)
from ontary.client import OntologyClient
from ontary.errors import PreconditionFailed


class Clock:
    """A capability provider: reaches outside the process."""

    def now(self) -> str:
        return "2026-07-26T00:00:00+00:00"


def _build() -> tuple[Ontology, Any]:
    ontology = Ontology(name="fnaudit", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope=[SelfScope(level="org")], owned=True)
    class Org(OntologyObject):
        id: str = prop(primary_key=True)
        size: float | None = None

    CLOCK = ontology.capability(Clock)

    @ontology.function(api_name="withCapability", capabilities=[CLOCK])
    def _with_capability(query: BoundQuery, _params: dict[str, Any]) -> str:
        return query.capability(CLOCK).now()

    @ontology.function(api_name="twice", capabilities=[CLOCK])
    def _twice(query: BoundQuery, _params: dict[str, Any]) -> str:
        query.capability(CLOCK)
        return query.capability(CLOCK).now()

    @ontology.function(api_name="pure")
    def _pure(_query: BoundQuery, _params: dict[str, Any]) -> int:
        return 42

    @ontology.function(api_name="pureButAudited", audit=True)
    def _pure_but_audited(_query: BoundQuery, _params: dict[str, Any]) -> int:
        return 7

    @ontology.function(api_name="quietDespiteCapability", capabilities=[CLOCK], audit=False)
    def _quiet(query: BoundQuery, _params: dict[str, Any]) -> str:
        return query.capability(CLOCK).now()

    @ontology.function(api_name="explodes", capabilities=[CLOCK])
    def _explodes(query: BoundQuery, _params: dict[str, Any]) -> str:
        query.capability(CLOCK)
        raise RuntimeError("boom")

    ontology.validate()
    return ontology, CLOCK


@pytest.fixture
def client() -> OntologyClient:
    ontology, clock = _build()
    store = ObjectStore(ontology.registry)
    return OntologyClient(
        ontology,
        store,
        Consumer(actor_id="a1", role="Admin", scope_level="org", scope_id="org-1", kind="ai"),
        capabilities={clock: Clock()},
    )


def _entries(client: OntologyClient) -> list[Any]:
    return client._store.audit_entries()


def test_capability_using_function_is_audited_by_default(client: OntologyClient) -> None:
    client.call_function("withCapability", {"tz": "UTC"})

    entries = _entries(client)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "function"
    assert entry.action == "withCapability"
    assert entry.outcome == "ok"
    assert entry.actor == "a1"
    assert entry.params == {"tz": "UTC"}
    assert [(a.api_name, a.count) for a in entry.capability_accesses] == [("Clock", 1)]
    # Functions cannot write or emit; those columns are empty by construction.
    assert entry.writes == []
    assert entry.effects == []
    assert entry.invocation_id is not None


def test_pure_function_is_not_audited_by_default(client: OntologyClient) -> None:
    """The cost decision. A function that declares no capability cannot reach
    outside the process, and everything it reads is already bounded by the
    guarded query layer -- so it does not buy an audit row per call."""
    assert client.call_function("pure", {}) == 42

    assert _entries(client) == []


def test_audit_true_overrides_the_default_for_a_pure_function(client: OntologyClient) -> None:
    assert client.call_function("pureButAudited", {}) == 7

    entries = _entries(client)
    assert [(e.kind, e.action) for e in entries] == [("function", "pureButAudited")]
    assert entries[0].capability_accesses == []


def test_audit_false_overrides_the_default_for_a_capability_function(
    client: OntologyClient,
) -> None:
    """Escape hatch in the other direction: a hot path that reaches outside on
    every call may legitimately not want a row each time."""
    client.call_function("quietDespiteCapability", {})

    assert _entries(client) == []


def test_repeated_access_increments_the_count_rather_than_duplicating(
    client: OntologyClient,
) -> None:
    client.call_function("twice", {})

    accesses = _entries(client)[0].capability_accesses
    assert [(a.api_name, a.count) for a in accesses] == [("Clock", 2)]


def test_failing_function_is_still_audited_with_its_accesses(
    client: OntologyClient,
) -> None:
    """A handler that reached outside and then raised has already had its
    effect on the world. An audit log that recorded only successes would say
    otherwise."""
    with pytest.raises(RuntimeError):
        client.call_function("explodes", {})

    entries = _entries(client)
    assert len(entries) == 1
    assert entries[0].outcome == "error"
    assert [(a.api_name, a.count) for a in entries[0].capability_accesses] == [("Clock", 1)]


def test_each_call_gets_its_own_invocation_id(client: OntologyClient) -> None:
    client.call_function("withCapability", {})
    client.call_function("withCapability", {})

    ids = [e.invocation_id for e in _entries(client)]
    assert len(set(ids)) == 2


def test_kind_separates_a_function_from_a_same_named_action(client: OntologyClient) -> None:
    """`kind` exists because nothing stops an ontology from declaring an action
    and a function with the same api_name; a consumer filtering by name must
    not conflate them."""
    client.call_function("withCapability", {})

    entry = _entries(client)[0]
    assert entry.kind == "function"
    assert entry.target_type == "", "a function has no target object type"


def test_unknown_function_still_raises_function_error(client: OntologyClient) -> None:
    """Looking the FunctionDef up to decide auditing must not change the error
    an unregistered name produces."""
    with raises_code(PreconditionFailed, "FUNCTION_ERROR"):
        client.call_function("noSuchFunction", {})
