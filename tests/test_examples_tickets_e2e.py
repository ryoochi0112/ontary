"""The living AC4 demo (declared-contracts §3 AC4, task T8 review anchor):
`examples/tickets`' `Ticket.escalated` is declared ontology-owned
(`owned={"escalated": False}`) while `status` stays source-backed.

This test drives the FULL governed path -- `OntologyClient.ingest`, not
`store.update` directly, and `EscalateTicket` through the real
`ActionExecutor`/`OntologyClient.execute`, not a raw store write -- to prove
the seam the spec's problem statement calls out is closed end to end:

    ingest (status="open") -> EscalateTicket via executor -> re-ingest the
    SAME ticket with status="closed" from source -> `escalated` still True,
    `status` refreshed to "closed", and the escalation's audit entry carries
    the real `WriteRecord`s + a real `target_id` (not a guess).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import MCPServer

import ontary
import ontary.cli as cli
import ontary.mcp_server as mcp_server
from examples.tickets.fixtures import load_fixtures
from examples.tickets.ontology import (
    Ticket,
    build_ontology,
)
from examples.tickets.run_mcp import build_multi_consumer_server
from ontary import (
    ActionError,
    Finding,
    ObjectStore,
    OntaryError,
    Ontology,
    OntologyObject,
    Page,
    PermissionDenied,
    PreconditionFailed,
    Sensitivity,
    Source,
    TypedPage,
    ValidationFailed,
    prop,
)
from ontary.client import OntologyClient
from ontary.meta import OntologyRegistry
from ontary.security import Consumer
from ontary.store import Store, WriteRecord
from ontary.testing import (
    FixedClock,
    SequentialIds,
    consumer,
    make_store,
    raises_code,
)

StoreFactory = Callable[[OntologyRegistry], Store]


def _make_object_store(registry: OntologyRegistry) -> Store:
    return ObjectStore(registry)


POSTGRES_DSN = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
_postgres_schema_counter = itertools.count()


def _make_postgres_store(registry: OntologyRegistry) -> Store:
    from ontary.store.postgres import PostgresStore

    assert POSTGRES_DSN is not None
    schema = f"ontary_tickets_e2e_{next(_postgres_schema_counter)}"
    import psycopg

    with psycopg.connect(POSTGRES_DSN, autocommit=True) as setup:
        setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        setup.execute(f'CREATE SCHEMA "{schema}"')
    separator = "&" if "?" in POSTGRES_DSN else "?"
    return PostgresStore(
        registry, f"{POSTGRES_DSN}{separator}options=-csearch_path%3D{schema}"
    )


STORE_FACTORIES: list[StoreFactory] = [_make_object_store]
if POSTGRES_DSN:
    STORE_FACTORIES.append(_make_postgres_store)


@pytest.fixture(params=STORE_FACTORIES, ids=lambda f: f.__name__.removeprefix("_make_"))
def tickets_store_factory(request: pytest.FixtureRequest) -> StoreFactory:
    factory: StoreFactory = request.param
    return factory

_INGEST_SOURCE = Source(source_system="tickets_csv")

_TICKETS_OPEN = [
    {
        "id": "t1",
        "queue_id": "q1",
        "subject": "Invoice mismatch",
        "age_hours": 4.0,
        "status": "open",
    },
]

_TICKETS_CLOSED = [
    {
        "id": "t1",
        "queue_id": "q1",
        "subject": "Invoice mismatch",
        "age_hours": 4.0,
        "status": "closed",
    },
]


def test_ingest_escalate_reingest_survives_via_governed_path() -> None:
    ontology, store = build_ontology()
    loader = OntologyClient(
        ontology,
        store,
        Consumer(
            actor_id="loader", role="Manager", scope_level="org", scope_id="o1", kind="human"
        ),
    )

    # 1. Bulk ingest lands the ticket with status="open"; `escalated` is not
    #    supplied by source (it's ontology-owned) -- the declared default
    #    (False) is injected on insert.
    loader.ingest("Org", [{"id": "o1", "name": "Acme Support"}], _INGEST_SOURCE)
    loader.ingest("Queue", [{"id": "q1", "name": "Billing"}], _INGEST_SOURCE)
    loader.ingest_links("queueOfOrg", [("q1", "o1")], _INGEST_SOURCE)
    report = loader.ingest("Ticket", _TICKETS_OPEN, _INGEST_SOURCE)
    assert report.ok, report.errors
    loader.ingest_links("ticketInQueue", [("t1", "q1")], _INGEST_SOURCE)

    ticket_id = "t1"
    queue_id = "q1"
    ticket = store.read_current("Ticket", ticket_id)
    assert ticket is not None
    assert ticket.payload["status"] == "open"
    assert ticket.payload["escalated"] is False

    # 2. EscalateTicket through the real governed path: OntologyClient ->
    #    ActionExecutor.execute -- not store.update directly.
    consumer = Consumer(
        actor_id="agent-1", role="Agent", scope_level="queue", scope_id=queue_id, kind="human"
    )
    client = OntologyClient(ontology, store, consumer)

    result = client.execute("EscalateTicket", {"ticket_id": ticket_id})
    assert result == {"ticket_id": ticket_id}

    escalated_ticket = store.read_current("Ticket", ticket_id)
    assert escalated_ticket is not None
    assert escalated_ticket.payload["escalated"] is True
    assert escalated_ticket.payload["status"] == "open"

    # The escalation's audit entry carries the real WriteRecord(s) + a real
    # target_id (declared-contracts AC11) -- not a guessed value.
    entries = store.audit_entries()
    escalate_entry = next(
        entry
        for entry in entries
        if entry.action == "EscalateTicket" and entry.outcome == "ok"
    )
    assert escalate_entry.action == "EscalateTicket"
    assert escalate_entry.outcome == "ok"
    assert escalate_entry.target_id == ticket_id
    assert escalate_entry.writes == [
        WriteRecord(op="update", object_type="Ticket", object_id=ticket_id),
    ]

    # 3. Re-ingest the SAME ticket from source with status changed to
    #    "closed" -- the source-backed property refreshes, and the
    #    ontology-owned `escalated` (written only by the action, never by
    #    source) survives the merge (spec AC4/AC5).
    report2 = loader.ingest("Ticket", _TICKETS_CLOSED, _INGEST_SOURCE)
    assert report2.ok, report2.errors

    final_ticket = store.read_current("Ticket", ticket_id)
    assert final_ticket is not None
    assert final_ticket.payload["status"] == "closed"
    assert final_ticket.payload["escalated"] is True


def test_tickets_store_isolates_both_tenants_on_one_file(tmp_path: Path) -> None:
    ontology, _ = build_ontology()
    store_path = tmp_path / "tenants.sqlite"
    tenant_a = ObjectStore(ontology.registry, store_path, tenant="tenant-a")
    tenant_b = ObjectStore(ontology.registry, store_path, tenant="tenant-b")

    ticket_a = tenant_a.insert(
        "Ticket",
        {"subject": "Only A", "age_hours": 1.0, "channel": "email"},
        Source(source_system="tenant-test"),
    )
    ticket_b = tenant_b.insert(
        "Ticket",
        {"subject": "Only B", "age_hours": 2.0, "channel": "chat"},
        Source(source_system="tenant-test"),
    )

    assert [row.payload["id"] for row in tenant_a.read_all("Ticket")] == [ticket_a]
    assert tenant_a.read_current("Ticket", ticket_b) is None
    assert [row.payload["id"] for row in tenant_b.read_all("Ticket")] == [ticket_b]
    assert tenant_b.read_current("Ticket", ticket_a) is None


def test_testing_harness_drives_the_escalation_through_the_public_helpers() -> None:
    """`ontary.testing` adoption: `make_store`/`consumer`/`FixedClock`/
    `SequentialIds`/`raises_code` drive the example's own action, and the
    refusal keeps its full exception identity (`ActionError` IS a
    `PreconditionFailed` IS an `OntaryError`, carrying the stable code)."""
    ontology, _ = build_ontology()
    store = make_store(ontology)
    ids = load_fixtures(store)
    instant = datetime(2026, 8, 31, 9, 30, tzinfo=timezone.utc)

    operator = consumer(
        actor_id="manager-effects",
        role="Manager",
        scope_level="queue",
        scope_id=ids["queue_a_id"],
    )
    client = ontology.bind(
        store,
        clock=FixedClock(instant),
        id_factory=SequentialIds("tickets-e2e"),
    ).for_consumer(operator)

    with raises_code("PRECONDITION_FAILED"):
        client.execute(
            "EscalateTicket", {"ticket_id": "missing-ticket", "reason": "missing"}
        )

    client.execute(
        "EscalateTicket",
        {"ticket_id": ids["ticket_1_id"], "reason": "SLA exceeded"},
    )

    entry = store.audit_entries()[-1]
    assert (entry.action, entry.outcome) == ("EscalateTicket", "ok")
    assert entry.ts == instant
    assert entry.invocation_id == "tickets-e2e-2"

    with pytest.raises(ActionError) as caught_precondition:
        client.execute(
            "EscalateTicket", {"ticket_id": "missing-ticket", "reason": "missing"}
        )
    assert isinstance(caught_precondition.value, PreconditionFailed)
    assert isinstance(caught_precondition.value, OntaryError)
    assert caught_precondition.value.code == "PRECONDITION_FAILED"


def _call_mcp(server: MCPServer, client_id: str) -> dict[str, Any]:
    token = AccessToken(token="offline", client_id=client_id, scopes=[])
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        result = asyncio.run(
            server.call_tool("query_objects", {"obj_type": "Ticket", "where": None})
        )
    finally:
        auth_context_var.reset(reset)
    if result.structured_content is not None:
        return result.structured_content
    payload: dict[str, Any] = json.loads(result.content[0].text)
    return payload


def test_multi_consumer_mcp_returns_each_queue_its_actual_tickets() -> None:
    server = build_multi_consumer_server()

    billing = _call_mcp(server, "billing-agent")
    onboarding = _call_mcp(server, "onboarding-agent")

    assert {row["payload"]["subject"] for row in billing["result"]} == {
        "Invoice mismatch",
        "Refund request",
        "Account locked",
        "Payment failed",
        "Duplicate invoice",
        "Upgrade question",
        "Cancel subscription",
        "Tax receipt needed",
        "Card declined",
    }
    assert {row["payload"]["subject"] for row in onboarding["result"]} == {
        "Welcome call",
        "Trial extension",
        "Setup assistance",
        "Mobile login issue",
        "Feature request",
        "Data export help",
        "Phone callback",
    }


def test_query_surface_against_tickets_app(
    tickets_store_factory: StoreFactory,
) -> None:
    ontology, _ = build_ontology()
    store = tickets_store_factory(ontology.registry)
    ids = load_fixtures(store)
    client = OntologyClient(
        ontology,
        store,
        Consumer(
            actor_id="manager-query",
            role="Manager",
            scope_level="org",
            scope_id=ids["org_id"],
            kind="human",
        ),
    )

    with pytest.raises(ValidationFailed) as caught_validation:
        client.list("Ticket", where={"missing_field": "value"}, limit=1)
    assert caught_validation.value.code == "UNKNOWN_FIELD"

    def listed_ids(
        where: dict[str, Any],
        *,
        order_by: str | tuple[str, Literal["asc", "desc"]] = "age_hours",
    ) -> list[str]:
        rows = client.list("Ticket", where=where, limit=None, order_by=order_by)
        return [row.payload["id"] for row in rows]

    assert listed_ids({"age_hours": {"gt": 30.0}}) == [
        ids["ticket_8_id"],
        ids["ticket_16_id"],
    ]
    assert listed_ids({"age_hours": {"gte": 30.0}}) == [
        ids["ticket_15_id"],
        ids["ticket_8_id"],
        ids["ticket_16_id"],
    ]
    assert listed_ids({"age_hours": {"lt": 3.0}}) == [
        ids["ticket_3_id"],
        ids["ticket_4_id"],
    ]
    assert listed_ids({"age_hours": {"lte": 3.0}}) == [
        ids["ticket_3_id"],
        ids["ticket_4_id"],
        ids["ticket_11_id"],
    ]
    assert listed_ids({"channel": {"in": ["email"]}}) == [
        ids["ticket_11_id"],
        ids["ticket_1_id"],
        ids["ticket_6_id"],
        ids["ticket_14_id"],
        ids["ticket_8_id"],
    ]
    assert listed_ids({"status": {"ne": "open"}}) == [
        ids["ticket_4_id"],
        ids["ticket_12_id"],
        ids["ticket_6_id"],
        ids["ticket_13_id"],
        ids["ticket_10_id"],
        ids["ticket_5_id"],
        ids["ticket_15_id"],
        ids["ticket_8_id"],
        ids["ticket_16_id"],
    ]
    assert listed_ids({"subject": {"contains": "call"}}) == [
        ids["ticket_3_id"],
        ids["ticket_16_id"],
    ]

    assert client.count("Ticket", where={"status": "closed"}) == 5
    assert client.exists("Ticket", where={"subject": {"contains": "callback"}}) is True
    assert client.exists("Ticket", where={"subject": {"contains": "missing"}}) is False
    assert client.aggregate(
        "Ticket",
        "age_hours",
        where={"queue_id": ids["queue_a_id"]},
        func="sum",
    ) == 127.0
    assert client.aggregate_by(
        "Ticket", "age_hours", "channel", func="count"
    ) == {"chat": 6, "email": 5, "phone": 5}

    reverse = client.traverse(
        "Queue", "ticketInQueue", ids["queue_b_id"], reverse=True
    )
    assert [row.payload["id"] for row in reverse] == [
        ids["ticket_3_id"],
        ids["ticket_11_id"],
        ids["ticket_12_id"],
        ids["ticket_13_id"],
        ids["ticket_14_id"],
        ids["ticket_15_id"],
        ids["ticket_16_id"],
    ]

    expected_order = [
        ids[f"ticket_{number}_id"]
        for number in (16, 8, 15, 7, 14, 5, 10, 2, 9, 13, 6, 12, 1, 11, 4, 3)
    ]
    seen: list[str] = []
    after: str | None = None
    while True:
        page = client.list(
            "Ticket", limit=3, after=after, order_by=("age_hours", "desc")
        )
        assert isinstance(page, Page)
        assert len(page.items) <= 3
        seen.extend(row.payload["id"] for row in page.items)
        if page.next_cursor is None:
            break
        after = page.next_cursor
    assert seen == expected_order
    assert len(seen) == 16

    typed_page = client.list(Ticket, limit=3, order_by=("age_hours", "desc"))
    assert isinstance(typed_page, TypedPage)
    assert [ticket.id for ticket in typed_page.items] == expected_order[:3]
    assert typed_page.next_cursor is not None


def test_escalation_lifecycle_runs_through_client_and_audits() -> None:
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    client = OntologyClient(
        ontology,
        store,
        Consumer(
            actor_id="manager-1",
            role="Manager",
            scope_level="queue",
            scope_id=ids["queue_a_id"],
            kind="human",
        ),
    )

    agent_client = OntologyClient(
        ontology,
        store,
        Consumer(
            actor_id="observer-1",
            role="Observer",
            scope_level="queue",
            scope_id=ids["queue_a_id"],
            kind="human",
        ),
    )
    with pytest.raises(PermissionDenied) as caught_permission:
        agent_client.execute(
            "OpenEscalation",
            {
                "ticket_id": ids["ticket_1_id"],
                "agent_id": ids["agent_1_id"],
                "reason": "must not run",
            },
        )
    assert caught_permission.value.code == "PERMISSION_DENIED"

    opened = client.execute(
        "OpenEscalation",
        {
            "ticket_id": ids["ticket_1_id"],
            "agent_id": ids["agent_1_id"],
            "reason": "SLA breach",
        },
    )
    escalation_id = opened["escalation_id"]
    assert store.links_from("escalationAssignedTo", escalation_id) == [ids["agent_1_id"]]

    resolved = client.execute("ResolveTicket", {"escalation_id": escalation_id})
    assert resolved == {"escalation_id": escalation_id}
    escalation = store.read_current("Escalation", escalation_id)
    ticket = store.read_current("Ticket", ids["ticket_1_id"])
    assert escalation is not None
    assert escalation.payload["state"] == "resolved"
    assert ticket is not None
    assert ticket.payload["escalated"] is False

    archived = client.execute("ArchiveTicket", {"escalation_id": escalation_id})
    assert archived == {"escalation_id": escalation_id}
    assert store.read_current("Escalation", escalation_id) is None
    assert store.links_from("escalationAssignedTo", escalation_id) == []

    successful_actions = {
        entry.action: entry
        for entry in store.audit_entries()
        if entry.kind == "action" and entry.outcome == "ok"
    }
    assert {"OpenEscalation", "ResolveTicket", "ArchiveTicket"} <= successful_actions.keys()
    assert successful_actions["ArchiveTicket"].writes[0] == WriteRecord(
        op="unlink",
        link_type="escalationAssignedTo",
        from_id=escalation_id,
        to_id=ids["agent_1_id"],
    )
    assert WriteRecord(
        op="retire",
        object_type="Escalation",
        object_id=escalation_id,
    ) in successful_actions["ArchiveTicket"].writes


def test_tickets_cli_validate_and_version_in_subprocesses() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    validated = subprocess.run(
        [
            sys.executable,
            "-m",
            "ontary.cli",
            "validate",
            "examples.tickets.ontology:build_ontology",
            "--json",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    versioned = subprocess.run(
        [sys.executable, "-m", "ontary.cli", "version"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert validated.returncode == 0
    assert json.loads(validated.stdout) == []
    assert validated.stderr == ""
    assert versioned.returncode == 0
    assert versioned.stdout.strip() == ontary.__version__
    assert versioned.stderr == ""


def test_tickets_cli_serve_hands_runner_exact_dev_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class Server:
        def run(
            self,
            transport: str = "streamable-http",
            *,
            host: str,
            port: int,
        ) -> None:
            captured["run"] = (transport, host, port)

    def fake_build(
        ontology: Ontology,
        store: Store,
        consumer_value: Consumer,
        *,
        name: str | None = None,
    ) -> Server:
        captured.update(
            ontology=ontology,
            store=store,
            consumer=consumer_value,
            name=name,
        )
        return Server()

    monkeypatch.setattr(mcp_server, "build_mcp_server", fake_build)
    store_path = tmp_path / "serve.sqlite"

    assert (
        cli.main(
            [
                "serve",
                "examples.tickets.ontology:build_ontology",
                "--dev",
                "--store",
                str(store_path),
                "--port",
                "9137",
            ]
        )
        == 0
    )
    assert isinstance(captured["ontology"], Ontology)
    assert captured["ontology"].name == "tickets"  # type: ignore[union-attr]
    assert isinstance(captured["store"], ObjectStore)
    assert store_path.exists()
    consumer_value = captured["consumer"]
    assert isinstance(consumer_value, Consumer)
    assert (
        consumer_value.actor_id,
        consumer_value.role,
        consumer_value.scope_level,
        consumer_value.scope_id,
    ) == ("ontary-dev", "ontary-dev", "dev", "localhost")
    assert captured["name"] == "tickets (ontary dev)"
    assert captured["run"] == ("streamable-http", "127.0.0.1", 9137)


def test_tickets_ontology_diagnose_is_clean() -> None:
    ontology, _ = build_ontology()

    assert ontology.diagnose() == []


def test_diagnose_returns_typed_findings_for_lint_dirty_scratch_ontology() -> None:
    scratch = Ontology(name="lint-dirty", scope_levels=["org"])

    @scratch.object(layer="L0")
    class ScratchContact(OntologyObject):
        id: str = prop(primary_key=True)
        email: str | None = prop(
            default=None,
            sensitivity=Sensitivity(human_visible=False),
        )

    findings = scratch.diagnose()

    min_n = next(finding for finding in findings if finding.code == "MIN_N_UNSET")
    assert isinstance(min_n, Finding)
    assert min_n.severity == "info"
