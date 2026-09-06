# Tickets reference app

`examples/tickets/` is the in-repo reference consumer for `ontary`. It models
a small support-ticket workflow:

- **Tickets** (`Ticket`), the owned entity being tracked, with a `channel`
  choices property (`email`, `chat`, or `phone`).
- **Agents** and **Teams**, the identities and scopes tickets are governed
  under.
- **Escalation**, an owned lifecycle object opened, resolved, and archived
  through the `OpenEscalation`, `ResolveTicket`, and `ArchiveTicket` business
  verbs.

See [`ontology.py`](ontology.py) for the full declaration.

## Running it

[`run_mcp.py`](run_mcp.py) starts a multi-consumer MCP server over the
tickets ontology:

```bash
uv run python examples/tickets/run_mcp.py
```

Note: `run_mcp.py`'s `build_multi_consumer_server()` omits `token_verifier`
and `auth` for brevity. Unlike [`docs/mcp-serving.md`](../../docs/mcp-serving.md),
copying it verbatim over a real transport yields `UNAUTHENTICATED` calls.

[`run_connector.py`](run_connector.py) demonstrates connector ingestion
against the same ontology.

## Test coverage

- [`tests/test_examples_tickets_e2e.py`](../../tests/test_examples_tickets_e2e.py)
  exercises the app end to end: tenant isolation, `ontary.testing` adoption,
  the multi-consumer MCP example with two queue-scoped identities, all five
  CLI commands, and a clean `diagnose()` result.
- [`tests/test_examples_smoke.py`](../../tests/test_examples_smoke.py) smoke-
  tests the example scripts.
