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

Fourteen tools, all subject to the same guards as the Python surface. Read-only
tools carry `ToolAnnotations(readOnlyHint=True)`; `execute_action` carries
`ToolAnnotations(destructiveHint=True)`:

| Tool | Purpose | Annotation |
| --- | --- | --- |
| `list_object_types` | Introspection | `readOnlyHint=True` |
| `list_link_types` | Introspection | `readOnlyHint=True` |
| `list_action_types` | Introspection, incl. parameter defs | `readOnlyHint=True` |
| `list_functions` | Introspection | `readOnlyHint=True` |
| `list_event_types` | Introspection | `readOnlyHint=True` |
| `get_declarations` | The declared contract bundle | `readOnlyHint=True` |
| `get_object` | Single read | `readOnlyHint=True` |
| `query_objects` | Filtered/paged read | `readOnlyHint=True` |
| `count_objects` | Visible-row count | `readOnlyHint=True` |
| `aggregate_objects` | Aggregate read | `readOnlyHint=True` |
| `traverse_links` | Follow a link | `readOnlyHint=True` |
| `list_events` | Paged business-event read | `readOnlyHint=True` |
| `execute_action` | Run an action | `destructiveHint=True` |
| `call_function` | Call a function | `readOnlyHint=True` |

Tool arguments. The published input schema marks every argument optional,
because each tool validates its own arguments: a missing required one is refused
with `INVALID_PARAMS` (`missing required tool parameter`). `aggregate_objects`
also needs `value_field` for every `func` except `"count"`; without it the engine
refuses with `INVALID_PARAMS` (`value_field is required`). `execute_action` and `call_function`
take the action's or Function's parameters as one `params` object, keyed by
parameter name, even when it is empty (`{}`). `list_action_types` and
`list_functions` publish each parameter's name, type, `description` (`null` when undescribed),
and required status.

| Tool | Required arguments | Optional arguments |
| --- | --- | --- |
| `list_object_types`, `list_link_types`, `list_action_types`, `list_functions`, `list_event_types`, `get_declarations` | — | — |
| `get_object` | `obj_type`, `obj_id` | — |
| `query_objects` | `obj_type` | `where`, `order_by`, `limit`, `after`, `include_total` (default `false`) |
| `count_objects` | `obj_type` | `where` |
| `aggregate_objects` | `obj_type` | `value_field`, `group_by`, `where`, `func` (default `"mean"`) |
| `traverse_links` | `obj_type`, `obj_id`, `link_api_name` | `reverse` (default `false`), `limit`, `after`, `include_total` (default `false`) |
| `list_events` | — | `event_type`, `about_type`, `about_id`, `since`, `until`, `limit`, `after`, `include_total` (default `false`) |
| `execute_action` | `api_name`, `params` | — |
| `call_function` | `api_name`, `params` | — |

The results of `execute_action` and `call_function` carry `date` and `datetime` values as ISO 8601 strings.
A result that is not JSON refuses with `RESULT_NOT_JSON`, not `INTERNAL_ERROR`; see
[Result values](api-actions-functions.md#result-values).

`list_object_types` includes a `transitions` key on every property. Its value is
`null` when the property has no graph, or an object with the complete `initial`
state list and `moves` mapping when it does. Each object type also has a `rules`
list containing each rule's `name` and `message`; rule code is never included.

`query_objects(obj_type, where=None, order_by=None, limit=None, after=None, include_total=False)` is always bounded
on the MCP surface: an omitted `limit` uses the server default cap of 100 rows,
and an explicit limit may be at most 1000. The underlying paged read supplies an
opaque `next_cursor`; a successful response keeps the existing rows under
`result` and adds `next_cursor` and `has_more` alongside it. `next_cursor` is
`null` exactly when `has_more` is `false`, so a page that ends exactly at the
last visible row has no cursor. Pass that cursor back with the same explicit `limit` to continue. `after` without an
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
`traverse_links` pages the same way: an omitted `limit` returns one default page of
100 rows (at most 1000 when explicit), `next_cursor` and `has_more` follow the same
rules, and `after` without `limit` returns `AFTER_WITHOUT_LIMIT`.

Both paged tools accept `include_total` (default `false`). When `true`, the
response adds a top-level `total`: the number of rows visible to the caller, the
same figure `count_objects` returns. It is not min-N gated, and it is not
transactional with the page: rows written between the two reads can make `total`
and the page disagree. Without `include_total` the `total` key is absent.
`limit=0` with `include_total=true` is count-only mode: `result` is `[]`,
`next_cursor` is `null`, `has_more` is `total > 0`, `order_by` is ignored, and
`after` is refused with `INVALID_LIMIT`. `limit=0` without `include_total` is
`INVALID_LIMIT`.

Pass `reverse=true` to traverse from the link's
target side and return source-side objects; identity-revealing denial is symmetric.

See [Read results](#read-results) for response keys and object row serialization.
Errors return a code from the table below; anything unclassified becomes
`INTERNAL_ERROR` rather than leaking internals to the caller.

Needs the `mcp` extra.

### `list_event_types` and `list_events`

`list_event_types` takes no arguments and returns `{"event_types": [...]}`, one entry
per declared event type. Each entry has `api_name`, `description`, `properties` (the
same per-property keys as `list_object_types`, including `ai_usable` and
`human_visible`), `about_types` (the sorted target types of the actions that emit it),
and `emitted_by` (the sorted `api_name`s of those actions). Like the other
introspection tools, it is read-only and, on a multi-consumer server, refuses an
unauthenticated call.

`list_events(event_type=None, about_type=None, about_id=None, since=None, until=None, limit=None, after=None, include_total=False)`
lists the business events this consumer may see, in log order: audit sequence, then
emission order. Every argument is optional.

- `event_type` limits the list to one declared event type.
- `about_type` limits it to events about one declared object type, after visibility
  is applied. `about_id` narrows that to one subject and needs `about_type`.
- `since` is an inclusive lower bound and `until` an exclusive upper bound. Each is an
  ISO 8601 date-time with a UTC offset, such as `2026-10-07T09:00:00+00:00`.

An event is listed only when its subject is visible to this consumer, judged by the
subject's latest row. A retired subject still counts, so its events stay listed unless
its latest row is out of scope.

The response is a paged read with the same envelope as `query_objects`: `result`,
`next_cursor`, `has_more`, `scope_limited`, and `total` when `include_total=true`.
`limit` defaults to 100 and may be at most 1000. `after` takes the previous
`next_cursor` and needs an explicit `limit`. `next_cursor` is `null` exactly when
`has_more` is `false`. `total` is the number of visible events across all pages and is
not min-N gated. `limit=0` with `include_total=true` is count-only mode: `result` is
`[]`, `next_cursor` is `null`, and `has_more` is `total > 0`.
`scope_limited` depends on declarations only: it is `true` when any subject type the
selected events can be about is scoped or has a `row_visibility` rule.

Each row has the keys `event_type`, `about_type`, `about_id`, `ts` (an ISO 8601
string with offset), `invocation_id`, `payload`, and `redacted_fields`.
`redacted_fields` is a sorted list of hidden field names, or `[]`. A redacted key is
absent from `payload`; it is never `null`. For example, consumer `ai`, scoped to
team `a`, calls
`list_events(event_type="TicketEscalated", about_type="Ticket", about_id="T-1", limit=10)`
where `customer_email` is declared `ai_usable=False`:

```json
{
  "result": [
    {
      "event_type": "TicketEscalated",
      "about_type": "Ticket",
      "about_id": "T-1",
      "ts": "2026-10-07T09:12:03.120000+00:00",
      "invocation_id": "inv_8f2c",
      "payload": {"reason": "SLA breach"},
      "redacted_fields": ["customer_email"]
    }
  ],
  "next_cursor": null,
  "has_more": false,
  "scope_limited": true
}
```

`list_events` refuses with these codes, in this order of checks:

| Code | When |
| --- | --- |
| `UNKNOWN_EVENT_TYPE` | `event_type` is not a declared event type. |
| `UNKNOWN_OBJECT_TYPE` | `about_type` is not a declared object type. |
| `INVALID_PARAMS` | `about_id` is given without `about_type`, or `since` or `until` is naive (no UTC offset) or not parseable. |
| `AFTER_WITHOUT_LIMIT` | `after` is given without `limit`. |
| `INVALID_LIMIT` | `limit` is above 1000 or below 1, or `after` is combined with count-only mode. |
| `INVALID_CURSOR` | `after` is not a `next_cursor` returned by `list_events`. |

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

Same fourteen tools as `build_mcp_server`, all fail-closed the same three ways, including
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

## Read results

The table lists every top-level key in a successful read response and every
key in an object row. Errors use the error envelope instead.

| Tool | Top-level keys | Row keys | `result` value | Scope policy |
| --- | --- | --- | --- | --- |
| `get_object` | `result` | `payload`, `lineage`, `redacted_fields` | Object row, or `null` for a missing or retired object | No `scope_limited`; an out-of-scope ID refuses with `VISIBILITY_DENIED`. |
| `query_objects` | `result`, `next_cursor`, `has_more`, `total`, `scope_limited` | `payload`, `lineage`, `redacted_fields` | List of object rows | `scope_limited` follows the queried type's declarations. |
| `count_objects` | `result`, `scope_limited` | — | Integer count of visible rows | `scope_limited` follows the counted type's declarations. |
| `traverse_links` | `result`, `next_cursor`, `has_more`, `total`, `scope_limited` | `payload`, `lineage`, `redacted_fields` | List of object rows | `scope_limited` follows the result type's declarations. |

`scope_limited` is a boolean on every successful query, count, and traversal,
including empty lists and zero counts.
It is `true` exactly when the result type is absent from `unscoped_types` or has
a `row_visibility` rule.
For `traverse_links`, the result type is the link's `to_type` by default and
its `from_type` when `reverse=true`.
The marker depends only on declarations for this consumer and type.
It never depends on stored data.
It reveals neither how many rows were hidden nor whether any rows were hidden.
`get_object` has no `scope_limited`, including when its result is `null` or it refuses.

An object row has the shape
`{"payload": {...}, "lineage": {...}, "redacted_fields": [...]}`.
`redacted_fields` is a sorted list of property names hidden from this consumer,
or `[]` when none are hidden.
A redacted key is absent from `payload`; it is never set to `null`.
For human consumers, a field with `human_visible=False` is listed.
For AI consumers, a field with `ai_usable=False` is listed.
A hidden scope-routing key declared through `DirectProperty` is listed too.

`total` appears only when the call sets `include_total=true`.

An agent asks, "How many tickets are linked to team `a`, and show me the first
two." It calls `traverse_links` with `obj_type="Team"`, `obj_id="a"`,
`link_api_name="inTeam"`, `reverse=true`, `limit=2`, and `include_total=true`. It gets:

```json
{"result": [{"payload": {...}, "lineage": {...}, "redacted_fields": []},
            {"payload": {...}, "lineage": {...}, "redacted_fields": []}],
 "next_cursor": "<opaque>", "has_more": true, "total": 218, "scope_limited": true}
```

The agent says: "218 tickets are linked to team a (that is what you can see; scope
may hide others). Here are the first two; I can page through the rest."

A short page or `next_cursor: null` means "no more rows" in the caller's visible
selection, never "some were hidden".
Neither signal says whether rows exist outside that selection.

An undeclared `obj_type` on any of the five read tools (`query_objects`,
`count_objects`, `get_object`, `traverse_links`, `aggregate_objects`) returns the
error envelope with `UNKNOWN_OBJECT_TYPE`. The check runs before every other
check, such as the `limit` cap or the link lookup. The response has no `result`
and no `scope_limited`, so an empty list with `scope_limited: true` always means a
declared type. For example, `obj_type="Tickte"` returns:

```json
{"error": {"type": "ValidationFailed", "message": "unregistered object type: 'Tickte'", "code": "UNKNOWN_OBJECT_TYPE", "kind": "validation"}}
```

---
