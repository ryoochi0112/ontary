"""Run the tickets escalation action and drain its effect outbox.

Run it with:

    uv run python -m examples.tickets.run_effects
"""

from __future__ import annotations

from examples.tickets.fixtures import load_fixtures
from examples.tickets.ontology import NOTIFY_TICKET_ESCALATION, build_ontology
from ontary import Consumer, DrainReport, EffectDispatcher, EffectMeta, EffectPayload


def _print_effect(payload: EffectPayload, meta: EffectMeta) -> None:
    print(
        f"effect={NOTIFY_TICKET_ESCALATION.api_name} action={meta.action} "
        f"payload={payload.model_dump(mode='json')} attempt={meta.attempt}"
    )


# Bound through an annotated name so the reference app exercises the public
# `EffectDispatcher`: the v1 coverage gate (tests/test_v1_gate_coverage.py)
# requires every public name to appear here or carry an allowlist reason, and
# this one's reason was deleted. Inlining `_print_effect` into `effects=` below
# reds `make verify` -- the coverage gate if the import goes with it, ruff's
# F401 if the import stays behind.
_dispatch_escalation: EffectDispatcher = _print_effect


def run() -> DrainReport:
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    runtime = ontology.bind(
        store,
        effects={NOTIFY_TICKET_ESCALATION: _dispatch_escalation},
    )
    client = runtime.for_consumer(
        Consumer(
            actor_id="ticket-agent",
            role="Agent",
            scope_level="queue",
            scope_id=ids["queue_a_id"],
            kind="human",
        )
    )

    client.execute(
        "EscalateTicket",
        {"ticket_id": ids["ticket_1_id"], "reason": "priority"},
    )
    report = client.drain_effects()
    print(report)
    return report


def main() -> None:
    run()


if __name__ == "__main__":
    main()
