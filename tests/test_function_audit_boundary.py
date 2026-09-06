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

import asyncio
from typing import Any

import pytest
from conftest import raises_code

from ontary import (
    BoundQuery,
    Cardinality,
    Consumer,
    DirectProperty,
    ObjectStore,
    Ontology,
    OntologyObject,
    SelfScope,
    Sensitivity,
    Source,
    ViaLink,
    prop,
)
from ontary.client import OntologyClient
from ontary.errors import PreconditionFailed, VisibilityError
from ontary.mcp_server import build_mcp_server


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
    # Functions cannot write; that column is empty by construction.
    assert entry.writes == []
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


# -- the AC10 disclosure release is audited on its own terms ----------------
#
# The input this file structurally could not construct before: a Function that
# is NOT audited by declaration and yet releases an individual-bearing hidden
# aggregate. `fnaudit` above declares no `contributor_rules` at all, so its
# exemption can never fire and every test above asks only "did it declare a
# capability?". `tests/test_typed_functions.py` has the contributor fixture but
# never reads `audit_entries()`. The breaking case needed both halves crossed.


def _build_contributor() -> tuple[Ontology, Any]:
    ontology = Ontology(name="fnaudit-contrib", scope_levels=["shelf"], min_n=3)

    CLOCK = ontology.capability(Clock)

    @ontology.object(
        layer="L0", scope="unscoped", contributor=[SelfScope(level="_contributor")]
    )
    class Reader(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[DirectProperty(level="shelf", property_name="shelf_id")],
        contributor=[
            ViaLink(link_api_name="byReader", direction="from", parent_type="Reader")
        ],
    )
    class Reading(OntologyObject):
        id: str = prop(primary_key=True)
        shelf_id: str | None = prop(default=None, scope_level="shelf")
        page_count: float | None = None
        raw_score: float | None = prop(
            default=None, sensitivity=Sensitivity(ai_usable=True, human_visible=False)
        )
        scarce_score: float | None = prop(
            default=None, sensitivity=Sensitivity(ai_usable=True, human_visible=False)
        )

    ontology.link("byReader", Reading, Reader, Cardinality.MANY_TO_ONE)

    @ontology.function(api_name="releasingMean")
    def _releasing_mean(query: BoundQuery, _params: dict[str, Any]) -> float:
        return query.aggregate(Reading, "raw_score")

    @ontology.function(api_name="releasingMeanByShelf")
    def _releasing_mean_by_shelf(
        query: BoundQuery, params: dict[str, Any]
    ) -> dict[str, float]:
        return query.aggregate_by(
            Reading, "raw_score", "shelf_id", where={"shelf_id": params["shelf_id"]}
        )

    @ontology.function(api_name="releasingMeanByApiName")
    def _releasing_mean_by_api_name(
        query: BoundQuery, _params: dict[str, Any]
    ) -> float:
        return query.aggregate("Reading", "raw_score")

    @ontology.function(api_name="releasingMeanByShelfByApiName")
    def _releasing_mean_by_shelf_by_api_name(
        query: BoundQuery, params: dict[str, Any]
    ) -> dict[str, float]:
        return query.aggregate_by(
            "Reading", "raw_score", "shelf_id", where={"shelf_id": params["shelf_id"]}
        )

    @ontology.function(api_name="countsOnly")
    def _counts_only(query: BoundQuery, _params: dict[str, Any]) -> int:
        return query.count(Reading)

    @ontology.function(api_name="releasingButQuiet", audit=False)
    def _releasing_but_quiet(query: BoundQuery, _params: dict[str, Any]) -> float:
        return query.aggregate(Reading, "raw_score")

    @ontology.function(api_name="releasesThenExplodes")
    def _releases_then_explodes(
        query: BoundQuery, _params: dict[str, Any]
    ) -> float:
        query.aggregate(Reading, "raw_score")
        raise RuntimeError("boom")

    @ontology.function(api_name="visibleMean")
    def _visible_mean(query: BoundQuery, params: dict[str, Any]) -> float:
        return query.aggregate(
            Reading, "page_count", where={"shelf_id": params["shelf_id"]}
        )

    @ontology.function(api_name="narrowedHiddenMean")
    def _narrowed_hidden_mean(
        query: BoundQuery, params: dict[str, Any]
    ) -> float:
        return query.aggregate(Reading, "raw_score", where={"id": params["id"]})

    @ontology.function(api_name="releasingScarceMean")
    def _releasing_scarce_mean(query: BoundQuery, _params: dict[str, Any]) -> float:
        return query.aggregate(Reading, "scarce_score")

    @ontology.function(api_name="releasingWithCapability", capabilities=[CLOCK])
    def _releasing_with_capability(
        query: BoundQuery, _params: dict[str, Any]
    ) -> float:
        query.capability(CLOCK).now()
        return query.aggregate(Reading, "raw_score")

    ontology.validate()
    return ontology, CLOCK


@pytest.fixture
def contributor_client() -> OntologyClient:
    ontology, clock = _build_contributor()
    store = ObjectStore(ontology.registry)
    client = OntologyClient(
        ontology,
        store,
        Consumer(
            actor_id="a1",
            role="Agent",
            scope_level="shelf",
            scope_id="shelf-1",
            kind="human",
        ),
        capabilities={clock: Clock()},
    )
    values = (1.0, 4.0, 7.0, 13.0)
    src = Source(source_system="test")
    client.ingest("Reader", [{"id": f"reader-{i}"} for i in range(len(values))], src)
    client.ingest(
        "Reading",
        [
            {
                "id": f"r{i}",
                "shelf_id": "shelf-1",
                "page_count": 10.0,
                "raw_score": v,
                **({"scarce_score": v} if i < 2 else {}),
            }
            for i, v in enumerate(values)
        ],
        src,
    )
    client.ingest_links(
        "byReader", [(f"r{i}", f"reader-{i}") for i in range(len(values))], src
    )
    return client


def test_releasing_a_hidden_aggregate_is_audited_without_any_declared_capability(
    contributor_client: OntologyClient,
) -> None:
    """A capability-less Function that trips the AC10 exemption hands a
    consumer a number derived from a field they cannot read. `audited` is
    `False` for it, and before this the release left no trace at all."""
    assert contributor_client.call_function(
        "releasingMean", {"shelf_id": "shelf-1"}
    ) == pytest.approx(6.25)

    entries = _entries(contributor_client)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "function"
    assert entry.action == "releasingMean"
    assert entry.outcome == "ok"
    assert entry.actor == "a1"
    assert entry.params == {"shelf_id": "shelf-1"}
    assert entry.invocation_id is not None
    assert entry.writes == []


def test_grouped_hidden_aggregate_is_refused_without_a_disclosure_audit(
    contributor_client: OntologyClient,
) -> None:
    """D4 refuses every grouped hidden-field exemption before release."""
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        contributor_client.call_function(
            "releasingMeanByShelf", {"shelf_id": "shelf-1"}
        )

    assert _entries(contributor_client) == []


def test_a_function_that_releases_nothing_stays_unaudited_in_the_same_ontology(
    contributor_client: OntologyClient,
) -> None:
    """The cost decision survives. Auditing follows the RELEASE, not the
    ontology: declaring `contributor_rules` anywhere must not bill every
    capability-less Function in that ontology for an audit row per call."""
    assert contributor_client.call_function("countsOnly", {}) == 4

    assert _entries(contributor_client) == []


def test_audit_false_cannot_suppress_a_hidden_aggregate_release(
    contributor_client: OntologyClient,
) -> None:
    """`audit=False` overrides the DEFAULT, not the disclosure trace. An
    ontology cannot opt out of recording that it handed out a hidden value."""
    assert contributor_client.call_function(
        "releasingButQuiet", {"shelf_id": "shelf-1"}
    ) == pytest.approx(6.25)

    assert [(e.kind, e.action) for e in _entries(contributor_client)] == [
        ("function", "releasingButQuiet")
    ]


def test_a_release_followed_by_a_raise_is_still_audited(
    contributor_client: OntologyClient,
) -> None:
    """Same reasoning as the capability error path: the handler already had
    the hidden value in hand, so a log that recorded only successes would say
    the release never happened."""
    with pytest.raises(RuntimeError):
        contributor_client.call_function(
            "releasesThenExplodes", {"shelf_id": "shelf-1"}
        )

    entries = _entries(contributor_client)
    assert len(entries) == 1
    assert entries[0].action == "releasesThenExplodes"
    assert entries[0].outcome == "error"


def test_a_release_by_an_already_audited_function_writes_exactly_one_entry(
    contributor_client: OntologyClient,
) -> None:
    """The two reasons to audit must not each append a row."""
    assert contributor_client.call_function(
        "releasingWithCapability", {"shelf_id": "shelf-1"}
    ) == pytest.approx(6.25)

    entries = _entries(contributor_client)
    assert len(entries) == 1
    assert entries[0].action == "releasingWithCapability"
    assert [(a.api_name, a.count) for a in entries[0].capability_accesses] == [
        ("Clock", 1)
    ]


def test_hidden_aggregate_release_over_mcp_is_audited(
    contributor_client: OntologyClient,
) -> None:
    """Finding 11 is finding 9 over the wire: `mcp_server.call_function`
    delegates to `OntologyClient.call_function` and has no audit path of its
    own, so the pin covers it as a second call site rather than a second fix."""
    server = build_mcp_server(
        contributor_client._ontology,
        contributor_client._store,
        contributor_client._consumer,
    )

    result = asyncio.run(
        server.call_tool(
            "call_function",
            {"api_name": "releasingMean", "params": {"shelf_id": "shelf-1"}},
        )
    )

    assert result[1] == {"result": pytest.approx(6.25)}
    assert [(e.kind, e.action) for e in _entries(contributor_client)] == [
        ("function", "releasingMean")
    ]


def test_a_release_min_n_refuses_is_not_audited(
    contributor_client: OntologyClient,
) -> None:
    """The exemption opens, but min-N refuses before a value is released."""
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        contributor_client.call_function("releasingScarceMean", {})

    assert _entries(contributor_client) == []


def test_narrowed_hidden_aggregate_is_refused_without_a_disclosure_audit(
    contributor_client: OntologyClient,
) -> None:
    """D4 refuses a narrowed hidden-field exemption before release."""
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        contributor_client.call_function("narrowedHiddenMean", {"id": "r0"})

    assert _entries(contributor_client) == []


def test_aggregating_a_visible_field_is_not_a_disclosure(
    contributor_client: OntologyClient,
) -> None:
    """The exemption is what makes an aggregate auditable, not aggregating.
    A Function reducing a field the consumer could have read themselves has
    released nothing, and must not be billed an audit row for it -- otherwise
    "audit the release" quietly becomes "audit every Function that reduces"."""
    assert contributor_client.call_function(
        "visibleMean", {"shelf_id": "shelf-1"}
    ) == pytest.approx(10.0)

    assert _entries(contributor_client) == []


def test_releasing_through_the_string_form_aggregate_is_audited(
    contributor_client: OntologyClient,
) -> None:
    """`aggregate("Reading", ...)` and `aggregate(Reading, ...)` are separate
    branches in `_TypedReadMixin`, and a fix threaded through only the typed
    one leaves this green while the string spelling releases silently. Found
    by mutation -- the same shape that left `TypedPage` unpinned in G2."""
    assert contributor_client.call_function(
        "releasingMeanByApiName", {"shelf_id": "shelf-1"}
    ) == pytest.approx(6.25)

    assert [(e.kind, e.action) for e in _entries(contributor_client)] == [
        ("function", "releasingMeanByApiName")
    ]


def test_grouped_string_form_is_refused_without_a_disclosure_audit(
    contributor_client: OntologyClient,
) -> None:
    """D4 applies equally to the grouped string spelling."""
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        contributor_client.call_function(
            "releasingMeanByShelfByApiName", {"shelf_id": "shelf-1"}
        )

    assert _entries(contributor_client) == []
