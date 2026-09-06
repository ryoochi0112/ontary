"""The stranger test: what a reader of the README can actually run.

Run from the repository root against a venv that holds ONLY the published
wheel with the `[mcp]` extra:

    /tmp/clean/bin/python scripts/stranger_smoke.py

1. Executes the README quickstart block verbatim (the same block
   `tests/test_docs.py` runs from the source tree, here from the wheel).
2. Runs the tickets reference app end to end: seed fixtures, escalate a
   ticket through the governed path, check the audit entry, and list the
   multi-consumer MCP server's tools.

Not a pytest module on purpose: pytest is not installed in the stranger's
environment, and installing it would defeat the point.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUICKSTART = re.compile(
    r"<!-- quickstart-runnable:start -->\n```python\n(.*?)```\n<!-- quickstart-runnable:end -->",
    re.DOTALL,
)


def check(condition: bool, description: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {description}")
    print(f"  ok: {description}")


def run_quickstart() -> None:
    print("1. README quickstart, verbatim")
    import ontary

    installed = Path(ontary.__file__).resolve().parent
    check("site-packages" in installed.parts, f"ontary imported from an install ({installed})")
    match = QUICKSTART.search((ROOT / "README.md").read_text())
    check(match is not None, "README carries the quickstart markers")
    assert match is not None
    code = match.group(1)
    namespace: dict[str, object] = {"__name__": "readme_quickstart"}
    exec(compile(code, "README.md#quickstart", "exec"), namespace)
    check("server" in namespace, "quickstart builds the MCP server object")


def run_tickets_example() -> None:
    print("2. tickets reference app, end to end")
    sys.path.insert(0, str(ROOT))
    from examples.tickets.fixtures import load_fixtures
    from examples.tickets.ontology import build_ontology
    from examples.tickets.run_mcp import build_multi_consumer_server
    from ontary import Consumer

    ontology, store = build_ontology()
    ids = load_fixtures(store)
    check("queue_a_id" in ids, "fixtures report the seeded queue id")
    agent = Consumer(
        actor_id="billing-agent",
        role="Agent",
        scope_level="queue",
        scope_id=ids["queue_a_id"],
        kind="human",
    )
    client = ontology.bind(store).for_consumer(agent)
    tickets = client.list("Ticket")
    check(len(tickets.items) > 0, f"the agent sees its queue's tickets ({len(tickets.items)})")
    ticket_id = tickets.items[0].payload["id"]
    result = client.execute("EscalateTicket", {"ticket_id": ticket_id})
    check(result == {"ticket_id": ticket_id}, "EscalateTicket runs through the governed path")
    row = store.read_current("Ticket", ticket_id)
    check(row is not None and row.payload["escalated"] is True, "the ticket is escalated")
    entry = next(
        e for e in store.audit_entries() if e.action == "EscalateTicket" and e.outcome == "ok"
    )
    check(entry.target_id == ticket_id, "the audit entry names the real target")

    server = build_multi_consumer_server()
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    check(len(names) > 0, f"the MCP server exposes tools ({sorted(names)})")


def main() -> None:
    run_quickstart()
    run_tickets_example()
    print("\nPASS: a stranger can follow the README and run the example.")


if __name__ == "__main__":
    main()
