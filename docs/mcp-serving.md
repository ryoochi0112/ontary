# MCP serving

[Back to the README](../README.md) · [API reference](api-reference.md)

MCP is a serving surface over the same ontology runtime, not a second authorization
model. The server exposes introspection, guarded reads, actions, and Functions using
the consumer and providers supplied at construction. The underlying ontology
handlers are bound once; a request does not register a fresh copy of the domain.

## Quickstart step: serve one consumer

For a single-agent or local deployment, build a server with the operator-selected
`Consumer`:

```python
from ontary import build_mcp_server

server = build_mcp_server(ontology, store, consumer)
server.run()  # stdio is the default transport
```

This is the short form used by the README quickstart. The agent sees the same
scope, sensitivity, row-visibility, and min-N decisions as a direct
`OntologyClient`. Use this shape when one process intentionally represents one
asserted identity.

## Tools, bounded queries, and safety annotations

The twelve tools are registered with these MCP safety hints:

| Tools | Annotation |
| --- | --- |
| `list_object_types`, `list_link_types`, `list_action_types`, `list_functions` | `readOnlyHint=True` |
| `get_declarations`, single-object reads, `query_objects`, `count_objects`, `traverse_links`, `call_function` | `readOnlyHint=True` |
| `aggregate_objects` | `readOnlyHint=True` |
| `execute_action` | `destructiveHint=True` |

`query_objects` accepts `where`, `order_by`, `limit`, and `after`. Its `where` grammar uses
bare scalars for equality and one-key `gt`/`gte`/`lt`/`lte`/`in`/`ne`/`contains`
operator mappings, validated against declared property types. Unknown operators
return `UNKNOWN_OPERATOR`; incompatible operators or operands return
`OPERATOR_TYPE_MISMATCH`; unknown keys return the existing `UNKNOWN_FIELD` error.
The MCP server applies a default limit of 100 and refuses
an explicit limit above the hard maximum of 1000 with `INVALID_LIMIT`.
`count_objects` accepts the same `where` grammar and returns the number of rows
visible to the invoking consumer.

`count_objects` and `aggregate_objects` are unbounded operations on this
untrusted surface: each can scan the full underlying type to apply its matching,
scope, row-visibility, and (for aggregation) release gates. They have no cap,
so callers should treat them as potentially expensive on large types;
`query_objects` caps the rows it returns, but an `order_by` page sorts the whole
type and costs the same as `count_objects`.

The successful response preserves the existing rows under `result` and adds an
opaque `next_cursor` alongside them:

```json
{
  "result": [{"payload": {"id": "book-1"}, "lineage": {}}],
  "next_cursor": "opaque-page-token"
}
```

Pass that cursor back with the same explicit `limit` to fetch the next page. A
`null` cursor means the visible result set is exhausted. `after` without an
explicit `limit` returns `AFTER_WITHOUT_LIMIT`, and `limit < 1` continues to use
the underlying `INVALID_LIMIT` validation.

`traverse_links` is intentionally not paged here: its underlying
`OntologyClient.traverse`/`GuardedQuery.traverse` API returns an unpaged list and
does not expose `limit`, `after`, or a cursor to delegate. Adding a cap to that
tool would require a new core traversal surface rather than a trivial MCP
adaptation.

## Serve many proven identities

`build_multi_consumer_mcp_server` is the HTTP shape for a process that serves more
than one identity. It does not take a fixed `consumer`. Instead, each invocation
receives the MCP transport's already-verified `AccessToken`, calls your
`resolve_consumer` callback, and creates a cheap consumer view over the shared
runtime.

```python
from mcp.server.auth.settings import AuthSettings
from ontary import Consumer
from ontary.mcp_server import (
    ConsumerResolver,
    build_multi_consumer_mcp_server,
)


def resolve_consumer(token) -> Consumer | None:
    # Map token.subject (or token.client_id) through your directory.
    return directory.lookup(token.subject)


server = build_multi_consumer_mcp_server(
    ontology,
    store,
    resolve_consumer=resolve_consumer,
    token_verifier=my_token_verifier,
    auth=AuthSettings(issuer_url=..., resource_server_url=...),
)
server.run(transport="streamable-http")
```

The resolver is application code. It may look up a database row or an organization
directory, but it is not a token verifier and it is not a security proxy supplied by
ontary. Once the resolver returns, the server stamps `Consumer.principal` from the
verified token, so a resolver cannot forge the principal that the transport proved.

Resolution happens for every invocation and is never cached. A revoked or
re-scoped token therefore cannot reuse an earlier consumer binding, and concurrent
requests for different identities share the immutable runtime machinery without
sharing per-request consumer state.

## Authentication and failure direction

The SDK verifies no token and issues none. Pass the deployer's `token_verifier` and
`AuthSettings` into the builder so FastMCP can install its authentication pipeline.
The callback receives an `AccessToken` that MCP has already verified.

If a request has no verified token, the server raises `PermissionDenied` with code
`UNAUTHENTICATED`. If a verified principal has no mapped `Consumer`, it raises
`PermissionDenied` with code `CONSUMER_UNRESOLVED`. These are distinct recovery
paths: configure authentication for the first and map the principal for the
second. Both happen before an `OntologyClient` exists, so neither is audited as an
ontology action or Function.

Once a request resolves, audit entries retain both identities: `principal` records
who the transport proved and `actor`/`role` records the consumer the resolver
selected. That makes a resolver which maps every principal to one privileged actor
visible to an auditor.

## Transport requirements

The multi-consumer builder constructs FastMCP with `stateless_http=True`. Stateful
streamable HTTP would reuse the task that handled `initialize`, which could retain
one request's authentication context for later calls. Stateless requests give each
invocation a fresh task and preserve the no-cache identity contract, at the cost of
session resumability and replay of missed events.

`stdio` carries no bearer-token context. A multi-consumer server run over stdio
therefore refuses calls with `UNAUTHENTICATED`; use the single-consumer
`build_mcp_server(ontology, store, consumer)` for a zero-infrastructure stdio
process. The SDK does not provide its own HTTP entrypoint: the deployer runs the
FastMCP server or wraps its ASGI application in the host's process.

See [the API reference MCP section](api-reference.md#mcp-server) for the builder
signatures and tool behavior, and its
[`Declarations`](api-reference.md#descriptor-authoring) paragraph for the runtime's
declared identity contract.

[Return to the README](../README.md) · [Return to the API reference](api-reference.md)
