# Tickets reference app

`examples/tickets/` is the v1 **ACCEPTANCE GATE**: the in-repo reference
consumer whose full-surface coverage ([`tests/test_v1_gate_coverage.py`](../../tests/test_v1_gate_coverage.py)),
end-to-end coverage ([`tests/test_examples_tickets_e2e.py`](../../tests/test_examples_tickets_e2e.py)),
and committed upgrade fixtures ([`tests/fixtures/upgrade/`](../../tests/fixtures/upgrade/))
define when a release may ship. The written bar is [`docs/v1-gate.md`](../../docs/v1-gate.md).

## Migration diary

Each entry records what a minor crossing forced this app to change. Entries are
newest first.

### 0.9.0 → 0.10.0

This crossing forced **no change** to `examples/tickets/ontology.py` or any other
app file: `Ticket` stays at version 2, the object-type set is unchanged, and every
business verb, effect, and function is identical to 0.9.0. The only visible effect
is on the fixture writer, not the app: `OpenEscalation`'s handler already called
`ctx.insert("Escalation", ...)` with no client-supplied id, and that id now comes
from the runtime's configured `id_factory` before the store is touched, instead of
the store's own `uuid.uuid4()` fallback the writer previously had to monkeypatch
around (see [`docs/compatibility.md` § Auto-minted ids come from `id_factory` (0.9
→ 0.10)](../../docs/compatibility.md#auto-minted-ids-come-from-id_factory-09--010)).
`tests/fixtures/upgrade/writers/write_v0_10_0.py` passes `id_factory=SequentialIds(...)`
to `Ontology.bind` and needs no monkeypatch at all.

### 0.8.0 → 0.9.0

This crossing exercised the **UPCASTER**, not the schema ladder. `SCHEMA_VERSION`
has been `9` unchanged since v0.6.0; the current value is recorded in
[`src/ontary/store/schema.py`](../../src/ontary/store/schema.py). The gate therefore
made no schema-ladder migration claim for this crossing.

- **Ticket shape:** `Ticket` moved from version 1 to version 2 with a `channel`
  choices property (`email`, `chat`, or `phone`). Its upcaster injects `"email"`
  when it reads a v1 row.
- **Ontology-owned lifecycle:** the app added the owned `Escalation` object and
  the `OpenEscalation`, `ResolveTicket`, and `ArchiveTicket` business verbs.
  `ArchiveTicket` explicitly uses `ctx.unlink` and `ctx.retire`; the lifecycle
  is exercised through the client and its audit writes.
- **Query coverage:** the seed grew to sixteen tickets across three channels,
  three statuses, and both queues. The e2e now uses query operators,
  `order_by`, aggregates, reverse traversal, and follows `Page` cursors with
  `after` until `next_cursor` is `None`.
- **Runtime coverage:** the e2e proves tenant isolation, adopts `ontary.testing`,
  drives a failed effect followed by an outbox drain, and exercises the
  multi-consumer MCP example with two queue-scoped identities.
- **CLI and diagnostics:** the e2e runs all five CLI commands—`validate`,
  `version`, `explain`, `erase`, and `serve`—and asserts a clean `diagnose()`
  result.
- **Storage envelope (decision C):** `examples/tickets/` itself did not change for
  this part of the crossing. The measured envelope, the `diagnose()` lint that warns
  when a type crosses it, the new `explain_scan` operator surface, the
  scope-resolution memo, and `exists()`'s early exit are, respectively, documentation,
  an advisory signal, an operator-only surface, and two internal speedups — none
  required an ontology declaration or a handler to adopt. The lint's own value pins,
  golden message, and silence cases live in
  [`tests/test_diagnose_lints.py`](../../tests/test_diagnose_lints.py); the reference
  app's own coverage of the lint lives in
  [`tests/test_examples_tickets_e2e.py`](../../tests/test_examples_tickets_e2e.py):
  one test forces `STORAGE_ENVELOPE_EXCEEDED` by monkeypatching a zero-row threshold
  over the app's ordinary store fixture; a second asserts the same fixture stays
  silent at its real, un-monkeypatched size. Nothing under `examples/tickets/` changed
  to make either pass — this part of the crossing forced no app-code change.

### 0.7.0 → 0.8.0

`examples/tickets/` itself was unchanged at this crossing: the v0.7.0 and
v0.8.0 tag trees are both `a02c4f5`. It therefore forced no app-code change.
The two committed fixtures differ by their tag's writing engine, not by a
different app shape.

## Upgrade fixtures and writer freeze

Fixtures for v0.7.0 and v0.8.0 are committed and opened by
[`tests/test_upgrade_fixtures.py`](../../tests/test_upgrade_fixtures.py) on every
`make verify`. The `upgrade-fixture-honesty` job in
[`.github/workflows/verify.yml`](../../.github/workflows/verify.yml) regenerates
each fixture from its own tag and compares it semantically against the committed
one. That job is live, and it is the enforcement mechanism for the writer-freeze
policy. It runs only in GitHub Actions, so PR CI is its only pre-merge proof.

Note: `run_mcp.py`'s `build_multi_consumer_server()` omits `token_verifier` and
`auth` for brevity. Unlike [`docs/mcp-serving.md`](../../docs/mcp-serving.md),
copying it verbatim over a real transport yields `UNAUTHENTICATED` calls.
