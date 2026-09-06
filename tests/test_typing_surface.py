"""Typing-surface pin for typed authoring (spec `typed-authoring` AC5/AC11):
`typing.assert_type` checks over the MIGRATED `examples/tickets` models,
proving mypy --strict infers the exact typed-client return types with
ZERO casts and ZERO type-ignores anywhere in this file. Each `assert_type`
call is paired with a runtime assertion so the same call is proven to
actually work against a fixture-seeded store, not just type-check.

This file is itself checked by `mypy --strict` via `make verify`.
"""

from __future__ import annotations

from typing import Any, Protocol, assert_type

from conftest import raises_code

import ontary
from examples.tickets.fixtures import load_fixtures
from examples.tickets.ontology import (
    NOTIFY_TICKET_ESCALATION,
    Agent,
    EscalateTicketParams,
    Org,
    Queue,
    Ticket,
    build_ontology,
    commentByAgent,
    commentOnTicket,
    queueOfOrg,
    ticketInQueue,
)
from ontary import (
    ActionContext,
    ActionParams,
    BoundQuery,
    CapabilityHandle,
    Consumer,
    ObjectStore,
    Page,
    Source,
    TypedPage,
)
from ontary.authoring import Ontology, OntologyObject, prop
from ontary.client import OntologyClient, OntologyRuntime
from ontary.effects import EffectMeta, EffectPayload
from ontary.errors import VisibilityError


class _InlineCapabilityProvider(Protocol):
    def label_for(self, key: str) -> str:
        ...


_CAPABILITY_ONTOLOGY = Ontology(
    name="capability-identity", scope_levels=["org"], min_n=1
)
INLINE_CAPABILITY: CapabilityHandle[_InlineCapabilityProvider] = _CAPABILITY_ONTOLOGY.capability(
    _InlineCapabilityProvider,
    name="InlineCapabilityProvider",
    description="A minimal capability used to pin handle identity.",
)


_RECORDED_TICKET_EFFECTS: list[EffectPayload] = []


def _record_ticket_effect(payload: EffectPayload, _meta: EffectMeta) -> None:
    _RECORDED_TICKET_EFFECTS.append(payload)


def _client() -> tuple[OntologyClient, dict[str, str]]:
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    consumer = Consumer(
        actor_id="agent-1",
        role="Agent",
        scope_level="queue",
        scope_id=ids["queue_a_id"],
        kind="human",
    )
    client = OntologyClient(
        ontology,
        store,
        consumer,
        effects={NOTIFY_TICKET_ESCALATION: _record_ticket_effect},
    )
    return client, ids


def _client_with_extra_queue_a_tickets() -> tuple[OntologyClient, ObjectStore, dict[str, str]]:
    """Same queue_a-scoped client as `_client()`, plus two more queue_a
    `Ticket`s sharing a distinct, NON-scope `status` ("urgent") -- carried
    debt from T3's review: `where={"queue_id": queue_a_id}` against a
    consumer who is ALREADY queue_a-scoped is a no-op filter (every visible
    Ticket already has that `queue_id`), so it can never catch a `where=`
    dropped from the typed `aggregate`/`aggregate_by` delegation in
    `client.py` -- the same blind spot pre-existed on typed `aggregate`.
    Filtering on `status` (not a scope-routing property) instead means the
    typed and string forms can only agree if `where=` is actually threaded
    through."""
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    src = Source(source_system="test")
    store.insert(
        "Ticket",
        {
            "subject": "Urgent A",
            "age_hours": 20.0,
            "status": "urgent",
            "queue_id": ids["queue_a_id"],
        },
        src,
    )
    store.insert(
        "Ticket",
        {
            "subject": "Urgent B",
            "age_hours": 30.0,
            "status": "urgent",
            "queue_id": ids["queue_a_id"],
        },
        src,
    )
    consumer = Consumer(
        actor_id="agent-1",
        role="Agent",
        scope_level="queue",
        scope_id=ids["queue_a_id"],
        kind="human",
    )
    client = OntologyClient(ontology, store, consumer)
    return client, store, ids


def test_typed_get_returns_optional_ticket() -> None:
    client, ids = _client()
    ticket = client.get(Ticket, ids["ticket_1_id"])
    assert_type(ticket, Ticket | None)
    assert ticket is not None
    assert isinstance(ticket, Ticket)
    assert ticket.subject == "Invoice mismatch"


def test_typed_list_returns_list_of_ticket() -> None:
    client, ids = _client()
    tickets = client.list(Ticket, where={"queue_id": ids["queue_a_id"]})
    assert_type(tickets, list[Ticket])
    assert {t.subject for t in tickets} == {"Invoice mismatch", "Refund request"}
    assert all(isinstance(t, Ticket) for t in tickets)


def test_typed_list_with_limit_returns_typed_page_of_ticket() -> None:
    # spec pagination-hardening AC7: `client.list(Ticket, limit=...) ->
    # TypedPage[Ticket]` -- pinned separately from the unpaginated
    # `list[Ticket]` overload above, since passing `limit=` switches the
    # return type entirely rather than just narrowing a list.
    client, ids = _client()
    page = client.list(Ticket, where={"queue_id": ids["queue_a_id"]}, limit=1)
    assert_type(page, TypedPage[Ticket])
    assert len(page.items) == 1
    assert isinstance(page.items[0], Ticket)
    assert page.next_cursor is not None

    rest = client.list(
        Ticket, where={"queue_id": ids["queue_a_id"]}, limit=1, after=page.next_cursor
    )
    assert_type(rest, TypedPage[Ticket])
    assert {t.id for t in rest.items}.isdisjoint({t.id for t in page.items})


def test_string_list_with_limit_returns_page() -> None:
    # spec pagination-hardening AC7: the string form paginates identically,
    # returning a `Page` (of `StoredObject`) instead of `TypedPage[T]`.
    client, ids = _client()
    page = client.list("Ticket", where={"queue_id": ids["queue_a_id"]}, limit=1)
    assert_type(page, Page)
    assert len(page.items) == 1
    assert page.next_cursor is not None


def test_typed_traverse_comment_on_ticket_returns_list_of_ticket() -> None:
    client, ids = _client()
    tickets = client.traverse(commentOnTicket, ids["comment_id"])
    assert_type(tickets, list[Ticket])
    assert [t.id for t in tickets] == [ids["ticket_1_id"]]


def test_typed_traverse_comment_by_agent_denied_for_human_consumer() -> None:
    # identity_revealing link -- a human-kind `Consumer` gets VISIBILITY_DENIED
    # on the SAME typed handle-first `traverse(...)` call the
    # AI-allowed case below uses (README's "same handle" claim).
    client, ids = _client()
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        client.traverse(commentByAgent, ids["comment_id"])


def test_typed_traverse_comment_by_agent_returns_list_of_agent() -> None:
    # identity_revealing link -- traversal requires the AI consumer kind
    # (spec §8); a fresh AI-kind client demonstrates a differently-typed
    # `to_cls` (Agent, not Ticket) resolving correctly through the SAME
    # handle-first `traverse(...)` overload.
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    consumer = Consumer(
        actor_id="ai-1", role="Agent", scope_level="queue", scope_id=ids["queue_a_id"], kind="ai"
    )
    client = OntologyClient(ontology, store, consumer)

    agents = client.traverse(commentByAgent, ids["comment_id"])
    assert_type(agents, list[Agent])
    assert [a.id for a in agents] == [ids["agent_1_id"]]


def test_typed_traverse_ticket_in_queue_returns_list_of_queue() -> None:
    client, ids = _client()
    queues = client.traverse(ticketInQueue, ids["ticket_1_id"])
    assert_type(queues, list[Queue])
    assert [q.id for q in queues] == [ids["queue_a_id"]]


def test_typed_traverse_queue_of_org_returns_list_of_org() -> None:
    # Org is scoped at the "org" level (SelfScope) -- a queue-scoped
    # consumer can't see it (no scope-hierarchy inference, per README's
    # "Known limitation"), so this traversal needs an org-scoped consumer.
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    consumer = Consumer(
        actor_id="agent-1", role="Agent", scope_level="org", scope_id=ids["org_id"], kind="human"
    )
    client = OntologyClient(ontology, store, consumer)

    orgs = client.traverse(queueOfOrg, ids["queue_a_id"])
    assert_type(orgs, list[Org])
    assert [o.id for o in orgs] == [ids["org_id"]]


def test_typed_aggregate_matches_string_form() -> None:
    # `where=` here filters on `status` (a NON-scope field) rather than
    # `queue_id` (the consumer's own scope key -- a no-op filter once
    # already queue_a-scoped, see `_client_with_extra_queue_a_tickets`'s
    # docstring), so it is LOAD-BEARING: dropping it from the typed
    # delegation changes the result instead of leaving the suite green.
    client, _store, ids = _client_with_extra_queue_a_tickets()
    typed_result = client.aggregate(Ticket, "age_hours", where={"status": "urgent"})
    assert_type(typed_result, float)
    string_result = client.aggregate("Ticket", "age_hours", where={"status": "urgent"})
    assert typed_result == string_result
    assert typed_result == 25.0  # mean(20.0, 30.0) -- proves the filter narrowed the set

    ticket_stats = client.call_function("ticketStats", {"queue_id": ids["queue_a_id"]})
    assert isinstance(ticket_stats, float)


def test_typed_aggregate_by_matches_string_form() -> None:
    # Same load-bearing `where=` as above (see that test's comment).
    client, _store, _ids = _client_with_extra_queue_a_tickets()
    typed_result = client.aggregate_by(
        Ticket, "age_hours", "status", where={"status": "urgent"}
    )
    assert_type(typed_result, dict[str, float])
    string_result = client.aggregate_by(
        "Ticket", "age_hours", "status", where={"status": "urgent"}
    )
    assert typed_result == string_result
    assert typed_result == {"urgent": 25.0}  # pins the where= narrowing the group set


def test_typed_execute_accepts_positional_and_keyword_forms() -> None:
    # README's "typed client.execute(EscalateTicket(ticket_id=...))" claim
    # (spec typed-actions.md AC5) -- the params INSTANCE is accepted
    # directly (no string action name), and both the typed and string forms
    # also accept their documented keyword spellings.
    client, ids = _client()
    result = client.execute(EscalateTicketParams(ticket_id=ids["ticket_1_id"]))
    assert_type(result, dict[str, Any])
    assert result == {"ticket_id": ids["ticket_1_id"]}

    string_result = client.execute("EscalateTicket", {"ticket_id": ids["ticket_2_id"]})
    assert_type(string_result, dict[str, Any])
    assert string_result == {"ticket_id": ids["ticket_2_id"]}

    typed_keyword_result = client.execute(
        params=EscalateTicketParams(ticket_id=ids["ticket_1_id"])
    )
    assert_type(typed_keyword_result, dict[str, Any])
    assert typed_keyword_result == {"ticket_id": ids["ticket_1_id"]}

    string_keyword_result = client.execute(
        action="EscalateTicket", params={"ticket_id": ids["ticket_2_id"]}
    )
    assert_type(string_keyword_result, dict[str, Any])
    assert string_keyword_result == {"ticket_id": ids["ticket_2_id"]}


def test_ontology_bind_returns_ontology_runtime() -> None:
    # spec typed-actions.md AC6: `ontology.bind(store) -> OntologyRuntime`.
    ontology, store = build_ontology()
    load_fixtures(store)
    runtime = ontology.bind(store)
    assert_type(runtime, OntologyRuntime)
    assert isinstance(runtime, OntologyRuntime)


def test_runtime_for_consumer_returns_ontology_client() -> None:
    # spec typed-actions.md AC6: `runtime.for_consumer(consumer) ->
    # OntologyClient`, a cheap per-consumer view sharing the runtime's
    # query/actions machinery.
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    runtime = ontology.bind(store)
    consumer = Consumer(
        actor_id="agent-1",
        role="Agent",
        scope_level="queue",
        scope_id=ids["queue_a_id"],
        kind="human",
    )
    client = runtime.for_consumer(consumer)
    assert_type(client, OntologyClient)
    assert isinstance(client, OntologyClient)

    ticket = client.get(Ticket, ids["ticket_1_id"])
    assert_type(ticket, Ticket | None)
    assert ticket is not None
    assert ticket.subject == "Invoice mismatch"


def test_bound_query_typed_get_and_list() -> None:
    # spec typed-actions.md AC8: `BoundQuery` gains the same M4a-style
    # typed overloads as `OntologyClient` -- `get(type[T], id) -> T | None`,
    # typed `list`.
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    consumer = Consumer(
        actor_id="agent-1",
        role="Agent",
        scope_level="queue",
        scope_id=ids["queue_a_id"],
        kind="human",
    )
    runtime = ontology.bind(store)
    bound = BoundQuery(runtime.query, consumer, ontology.registry)

    ticket = bound.get(Ticket, ids["ticket_1_id"])
    assert_type(ticket, Ticket | None)
    assert ticket is not None
    assert ticket.subject == "Invoice mismatch"

    tickets = bound.list(Ticket, where={"queue_id": ids["queue_a_id"]})
    assert_type(tickets, list[Ticket])
    assert {t.subject for t in tickets} == {"Invoice mismatch", "Refund request"}


def test_bound_query_typed_traverse_returns_list_of_queue() -> None:
    # spec typed-actions.md AC8: handle-first `BoundQuery.traverse(...)`
    # resolves the SAME `LinkHandle` overload mechanics as
    # `OntologyClient.traverse`.
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    consumer = Consumer(
        actor_id="agent-1",
        role="Agent",
        scope_level="queue",
        scope_id=ids["queue_a_id"],
        kind="human",
    )
    runtime = ontology.bind(store)
    bound = BoundQuery(runtime.query, consumer, ontology.registry)

    queues = bound.traverse(ticketInQueue, ids["ticket_1_id"])
    assert_type(queues, list[Queue])
    assert [q.id for q in queues] == [ids["queue_a_id"]]


def test_capability_access_narrows_provider_protocol() -> None:
    class LLMClient:
        def complete(self, prompt: str) -> str:
            raise NotImplementedError

    class LLMProvider(LLMClient):
        def complete(self, prompt: str) -> str:
            return f"completed: {prompt}"

    ontology = Ontology("typed-capability", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped")
    class Record(OntologyObject):
        id: str = prop(primary_key=True)

    llm = ontology.capability(LLMClient, name="LLM")

    class RunParams(ActionParams):
        pass

    @ontology.action(
        RunParams,
        target=Record,
        roles=["Operator"],
        capabilities=[llm],
        api_name="Run",
    )
    def run(ctx: ActionContext, _params: RunParams) -> dict[str, str]:
        provider = ctx.capability(llm)
        assert_type(provider, LLMClient)
        return {"result": provider.complete("action")}

    @ontology.function(api_name="Complete", capabilities=[llm])
    def complete(query: BoundQuery, _params: dict[str, Any]) -> str:
        provider = query.capability(llm)
        assert_type(provider, LLMClient)
        return provider.complete("function")

    consumer = Consumer(
        actor_id="operator-1",
        role="Operator",
        scope_level="org",
        scope_id="org-1",
        kind="human",
    )
    client = ontology.bind(
        ObjectStore(ontology.registry), capabilities={llm: LLMProvider()}
    ).for_consumer(consumer)
    assert client.execute("Run", {}) == {"result": "completed: action"}
    assert client.call_function("Complete", {}) == "completed: function"


def test_capability_handle_preserves_its_provider_protocol() -> None:
    """A `CapabilityHandle` keeps the author's Protocol as its `proto`, so
    `ctx.capability(handle)`/`query.capability(handle)` narrow to that exact
    type at every access site with no cast.

    Uses a tiny inline capability declaration; this assertion is about the
    engine's typing guarantee, not about any one domain.
    """
    assert_type(INLINE_CAPABILITY.proto, type[_InlineCapabilityProvider])
    assert INLINE_CAPABILITY.proto is _InlineCapabilityProvider
    assert INLINE_CAPABILITY.registry is _CAPABILITY_ONTOLOGY.registry


def test_paged_row_is_not_on_the_front_door() -> None:
    # spec pagination-hardening §5 (T2 amendment): `PagedRow` (store.py) is
    # ENGINE-INTERNAL -- it carries a random per-row page token (opaque
    # cursor) out-of-band, never the store's own physical row identity and
    # never on a consumer-visible model (AC8) -- and must never be exported
    # from the `ontary` front door.
    assert not hasattr(ontary, "PagedRow")
