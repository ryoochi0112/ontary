# `ontary` — API reference: MCP server

**English** · [日本語](api-mcp.ja.md) · [← API reference](api-reference.md)

*Reference* — MCP server contracts; see the [API reference](api-reference.md) and [Getting started](getting-started.md) for related contracts and examples.

## MCP server

```python
from ontary.mcp_server import build_mcp_server

server = build_mcp_server(ontology, store, consumer, *, name=None,
                          capabilities=None)  # -> MCPServer
```

One server process, one `Consumer` identity. Declared handlers arrive **pre-bound** —
there is no registration callback.

Twelve tools, all subject to the same guards as the Python surface. Read-only
tools carry `ToolAnnotations(readOnlyHint=True)`; `execute_action` carries
`ToolAnnotations(destructiveHint=True)`:

| Tool | Purpose | Annotation |
| --- | --- | --- |
| `list_object_types` | Introspection | `readOnlyHint=True` |
| `list_link_types` | Introspection | `readOnlyHint=True` |
| `list_action_types` | Introspection, incl. parameter defs | `readOnlyHint=True` |
| `list_functions` | Introspection | `readOnlyHint=True` |
| `get_declarations` | The declared contract bundle | `readOnlyHint=True` |
| `get_object` | Single read | `readOnlyHint=True` |
| `query_objects` | Filtered/paged read | `readOnlyHint=True` |
| `count_objects` | Visible-row count | `readOnlyHint=True` |
| `aggregate_objects` | Aggregate read | `readOnlyHint=True` |
| `traverse_links` | Follow a link | `readOnlyHint=True` |
| `execute_action` | Run an action | `destructiveHint=True` |
| `call_function` | Call a function | `readOnlyHint=True` |

Tool arguments. The published input schema marks every argument optional,
because each tool validates its own arguments: a missing required one is refused
with `INVALID_PARAMS` (`missing required tool parameter`). `aggregate_objects`
also needs `value_field` for every `func` except `"count"`; without it the engine
refuses with `INVALID_PARAMS` (`value_field is required`). `execute_action` and `call_function`
take the action's or Function's parameters as one `params` object, keyed by
parameter name, even when it is empty (`{}`). `list_action_types` and
`list_functions` publish each parameter's name, type, and required status.

| Tool | Required arguments | Optional arguments |
| --- | --- | --- |
| `list_object_types`, `list_link_types`, `list_action_types`, `list_functions`, `get_declarations` | — | — |
| `get_object` | `obj_type`, `obj_id` | — |
| `query_objects` | `obj_type` | `where`, `order_by`, `limit`, `after` |
| `count_objects` | `obj_type` | `where` |
| `aggregate_objects` | `obj_type` | `value_field`, `group_by`, `where`, `func` (default `"mean"`) |
| `traverse_links` | `obj_type`, `obj_id`, `link_api_name` | `reverse` (default `false`) |
| `execute_action` | `api_name`, `params` | — |
| `call_function` | `api_name`, `params` | — |

`list_object_types` includes a `transitions` key on every property. Its value is
`null` when the property has no graph, or an object with the complete `initial`
state list and `moves` mapping when it does. Each object type also has a `rules`
list containing each rule's `name` and `message`; rule code is never included.

`query_objects(obj_type, where=None, order_by=None, limit=None, after=None)` is always bounded
on the MCP surface: an omitted `limit` uses the server default cap of 100 rows,
and an explicit limit may be at most 1000. The underlying paged read supplies an
opaque `next_cursor`; a successful response keeps the existing rows under
`result` and adds `next_cursor` alongside it (`null` when exhausted). Pass that
cursor back with the same explicit `limit` to continue. `after` without an
explicit `limit` returns `AFTER_WITHOUT_LIMIT`; values below 1 or above 1000
return `INVALID_LIMIT`.

The `where` grammar accepts a bare scalar for equality or an operator mapping
using `gt`, `gte`, `lt`, `lte`, `in`, `ne`, or `contains`; several operators in
one mapping are AND-ed, so `{"gte": a, "lt": b}` is a range. Operators are
validated against the declared property type; unknown operators raise
`UNKNOWN_OPERATOR`, and an incompatible operator or operand raises
`OPERATOR_TYPE_MISMATCH`. Date comparisons use the stored ISO date order;
datetime comparisons are by instant across UTC offsets.
Lineage fields are not filterable, and an unknown key raises `UNKNOWN_FIELD`.
`order_by` accepts a declared payload field, ascending by default, or a
`(field, "asc"|"desc")` pair, and composes with the page cursor.
`count_objects` returns the number of rows visible to the consumer and is not
min-N-gated. It reveals only what `query_objects` already lists; for a min-N-released
count use `aggregate_objects(func="count")`, which needs no `value_field`.
`aggregate_objects` accepts `func="mean"|"count"|"sum"|"min"|"max"`
with `"mean"` as the default; every function keeps the same min-N release
discipline, including refusal of grouped-empty `{}` selections. `value_field` is
optional for `func="count"` — omitting it counts every visible row; every other
func requires it and raises `INVALID_PARAMS` if it is missing.
`traverse_links` remains an unpaged list because the underlying
`OntologyClient.traverse`/`GuardedQuery.traverse` API has no `limit`/`after`
cursor surface to delegate to. Pass `reverse=true` to traverse from the link's
target side and return source-side objects; identity-revealing denial is symmetric.

Objects serialize as `{"payload": {...}, "lineage": {...}}` (`None` stays `None`).
Errors return a code from the table below; anything unclassified becomes
`INTERNAL_ERROR` rather than leaking internals to the caller.

Needs the `mcp` extra.

### Multi-consumer serving

```python
from ontary.mcp_server import build_multi_consumer_mcp_server, ConsumerResolver

server = build_multi_consumer_mcp_server(
    ontology, store, *, resolve_consumer, name=None,
    capabilities=None,
    token_verifier=None, auth=None,
)  # -> MCPServer
```

One server process, **many proven identities** — no `consumer` argument. Builds exactly
one `OntologyRuntime` (one `GuardedQuery`, one `ActionExecutor`, declared handlers bound
once); every call takes a cheap `runtime.for_consumer(...)` view.

Per invocation: reads that request's MCP-verified `AccessToken` off the transport's own
contextvar, calls the caller-supplied `ConsumerResolver` (`resolve_consumer(token) ->
Consumer | None`), then stamps `Consumer.principal` from the token (`subject`, falling
back to `client_id`) **after** the resolver returns — so a resolver cannot forge who
authenticated. Resolution is never cached: a revoked or re-scoped token can never be
served from a stale binding.

Same twelve tools as `build_mcp_server`, all fail-closed the same three ways, including
introspection:

| Condition | Code |
| --- | --- |
| No verified `AccessToken` on the request (`get_access_token()` returns `None` — no token presented, or HTTP with no `token_verifier` configured; always true over stdio) | `UNAUTHENTICATED` |
| `resolve_consumer` returns `None` for a verified principal | `CONSUMER_UNRESOLVED` |
| `resolve_consumer` raises anything other than a deliberate `OntaryError` | generic `INTERNAL_ERROR` — the raised message never reaches the caller, since it may embed token material |

The SDK verifies no token and issues none — `token_verifier` and `auth` are MCP's own
types (`mcp.server.auth.provider.TokenVerifier` / `mcp.server.auth.settings.
AuthSettings`), configured by the deployer and forwarded VERBATIM to the underlying
`MCPServer(...)` call — the only place either can be wired, since `MCPServer` exposes no
public setter for either afterwards. Passing neither is a legitimate stdio-only or
intentionally-open deployment; passing exactly one of the two is not a runtime state at
all — `MCPServer.__init__` raises `ValueError` (fail-fast at construction), so no server
is ever built and no call is ever made. Stdio carries no auth context at all, so a
multi-consumer server run that way always refuses every call with `UNAUTHENTICATED`
regardless of these two arguments; use `build_mcp_server(ontology, store, consumer)`
for a single-consumer stdio process instead.

**Transport options belong to `run()`/`streamable_http_app()`.** The builder does not
force a session mode: on mcp 2.x, `stateless_http`, `json_response`,
`transport_security`, and `host` are keyword arguments of `run()` and
`streamable_http_app()`, and `port` of `run()` (an ASGI app binds no socket). Each request resolves its own token in stateful
sessions too; `tests/test_mcp_multi_consumer.py` pins both modes at the ASGI
boundary. `build_mcp_server` has one `Consumer` bound at construction and no
per-request identity to resolve.

Needs the `mcp` extra.

---

