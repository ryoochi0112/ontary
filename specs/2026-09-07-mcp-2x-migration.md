# Spec: `ontary[mcp]` moves from mcp 1.x to mcp 2.x

**Status:** planned 2026-09-07 from a measured probe (mcp 2.1.1 and 1.29.1 in isolated
venvs, `tests/test_mcp*.py` run against each). Interview: one question (Q1), answered.
Engineering layer + ledger at `$CTRL/var/specs/ontary/mcp-2x-migration.{md,tasks.md}`.
**Repo:** `ryoochi0112/ontary`, one PR. Unblocks the Dependabot ignore added in PR #11
(PR #10 was the red 2.x bump).

## 1. Problem & user value

`src/ontary/mcp_server.py` imports `mcp.server.fastmcp.FastMCP`, which mcp 2.x replaces
with a stub that raises. Anyone who installs `ontary[mcp]` next to a 2.x-only dependency
gets no MCP server, and Dependabot cannot move us because PR #11 ignores mcp majors until
this spec lands. mcp 2.x is the line new features go to; 1.x is still released in lockstep
(1.29.1 and 2.1.0 both on 2026-08-24) but carries no forward-compat shim, so the two lines
only drift further apart.

**Decision: drop 1.x rather than support both.** Dual support would put a union return
type on `build_mcp_server`/`build_multi_consumer_mcp_server`, double the test helpers, and
add a CI leg, for a pre-1.0 library whose compatibility policy already says a minor bump
may break you. A 1.x user pins `ontary<0.12`.

## 2. Users & context

- **Who:** anyone installing `ontary[mcp]`; the `ontary-mcp` console script; the sandbox
  (its internal consumer) later, when it bumps its exact `ontary==0.11.x` pin.
  Because of that exact pin the sandbox migration spec is not affected by this PR.
- **Trigger:** one PR. Afterwards Dependabot follows mcp 2.x minors normally.
- **Measured facts (2.1.1 vs 1.28.1/1.29.1), so `/plan` does not redo the probe:**

| Surface | 2.1.1 |
|---|---|
| `mcp.server.fastmcp` | stub module; raises `ModuleNotFoundError` naming `mcp.server.mcpserver.MCPServer` |
| `AccessToken.subject`, `TokenVerifier`, `AuthSettings`, `get_access_token`, `auth_context_var`, `AuthenticatedUser`, `ToolAnnotations`, `TransportSecuritySettings` | unchanged |
| `MCPServer(token_verifier=, auth=)` | unchanged; still raises `ValueError` when only one is given |
| `stateless_http=`, `json_response=`, `transport_security=`, `host`, `port` | gone from the constructor and from `settings`; now kwargs of `streamable_http_app()` and `run()` |
| sync `def` tool handlers | run on a worker thread (`anyio.to_thread.run_sync`); `async def` handlers run on the event loop as before |
| `call_tool()` return | `CallToolResult` object (`content`, `structured_content`, `is_error`), was `list \| tuple` |
| `Tool.inputSchema`, `ToolAnnotations.readOnlyHint` | snake_case attributes (`input_schema`, `read_only_hint`); camelCase still accepted as kwargs |
| stateful streamable HTTP token freeze (the reason for hardcoded `stateless_http=True`) | gone: `test_stateless_http_serves_each_request_its_own_token` passes in stateful mode on 2.1.1 and fails on 1.28.1 |
| requirements | Python >=3.10, pydantic >=2.12; adds `opentelemetry-api`, `httpx2`, `mcp-types` |

Test outcome of the probe: with only the import renamed, 50/54 multi-consumer tests fail
because every SQLite call runs on a worker thread ("objects created in a thread can only
be used in that same thread"), surfaced as `INTERNAL_ERROR`. With the twelve handlers made
`async def`, every remaining failure across the six mcp-touching test files is a test-side
shape change (13 cases) or the test that simulated the 2.x layout.

## 3. Acceptance criteria

1. **Pin.** `pyproject.toml` declares `mcp>=2.1.1,<3` under the `mcp` extra. The comment
   above it records the measured basis (the table above, in short), replacing the 1.27.2
   rationale. `uv.lock` resolves 2.x.
2. **No 1.x names anywhere.** `grep -rn "fastmcp\|FastMCP" src tests examples docs README.md`
   returns nothing except the CHANGELOG entry for this migration. The lazy loaders import
   `mcp.server.mcpserver.MCPServer`; type annotations and docs say `MCPServer`.
3. **Handlers stay on the event loop.** All twelve registered tools are `async def`. One
   test asserts, for every tool the live server lists, that its handler is a coroutine
   function (guard the class, not a sample), so a future sync tool cannot reintroduce the
   worker-thread path. The existing SQLite-backed tool tests pass unchanged in meaning.
4. **`stateless_http` hardcode removed (Q1 = A).** `build_multi_consumer_mcp_server`
   constructs `MCPServer(name, token_verifier=..., auth=...)` and nothing else. The deployer
   chooses transport options on `run()`/`streamable_http_app()`. The ASGI test is
   parametrised over `stateless_http` `True` and `False` and proves each request resolves
   its own token in both modes; the `False` case is the regression guard for the freeze.
5. **Wrong-version hint flips direction.** With mcp 1.x installed (root package present,
   `mcp.server.mcpserver` missing) importing a builder raises the `MCP_INCOMPATIBLE_HINT`
   naming `mcp>=2.1.1,<3`. The missing-extra hint is unchanged. A test simulates the 1.x
   layout the way the current test simulates 2.x; that current test is retired.
6. **Test helpers read 2.x shapes.** `_call` helpers return `structured_content` when
   present, else decode `content[0].text`; schema assertions use `input_schema` and
   `read_only_hint`. No helper accepts the 1.x list/tuple shape.
7. **Examples.** `examples/tickets/run_mcp.py` builds and runs an `MCPServer`;
   `tests/test_examples_smoke.py` and `tests/test_examples_tickets_e2e.py` pass.
8. **Docs.** `docs/mcp-serving.md`, `docs/api-reference.md`, `docs/api-reference.ja.md`, and
   `README.md` say `MCPServer`; the "constructed `stateless_http=True`, hardcoded" paragraphs
   become a short note that 2.x resolves each request's token in stateful mode too and that
   transport options are the deployer's `run()`/`streamable_http_app()` kwargs, with one
   streamable-HTTP example showing them. The `ConsumerResolver` docstring's reason for being
   synchronous is rewritten: the handlers are `async def`, so the resolver runs on the event
   loop and must not block.
9. **Changelog and Dependabot.** `CHANGELOG.md` `[Unreleased]` gets a **Breaking** entry
   (mcp 2.x required; `MCPServer` returned; `stateless_http` no longer forced). The
   `ignore:` block for mcp in `.github/dependabot.yml` is deleted and the changelog line
   that describes it is updated.
10. **`make verify` is green** on 3.12 and 3.13, including the stranger test, which now
    installs mcp 2.x from the wheel's `[mcp]` extra.

## 4. Scope & out-of-scope

In scope: the pin, the rename, async handlers, the stateless change, the hint flip, test
and example updates, docs, changelog, Dependabot.

Out of scope: supporting mcp 1.x in the same release; an async `ConsumerResolver`;
thread-safe stores; adopting 2.x-only features (`Context` injection, middleware,
extensions, subscriptions, `MCPError` pass-through); OpenTelemetry configuration; the
sandbox's own bump to ontary 0.12 (its `mcp` must move to 2.x at the same time, to be
recorded in its friction log when it happens); the release tag itself.

## 5. Open questions & decisions

| # | Question | Decision |
|---|---|---|
| pre | Drop 1.x or support both? | **Drop 1.x.** Reasons in § 1. |
| pre | Worker-thread handlers break the SQLite store; fix where? | **`async def` handlers** (event loop, as 1.x behaved). Thread-safe stores are a separate project. |
| Q1 | Keep forcing `stateless_http=True` now that 2.x has no token freeze? | **A.** Stop forcing; deployer chooses; ASGI test covers both modes. |
| pre | Version bump? | Pre-1.0 minor per `docs/compatibility.md`; next release is 0.12.0. The tag is Ryo's call, outside this PR. |
