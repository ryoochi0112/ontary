# MCP serving

[Back to the README](../README.md) · [API reference](api-reference.md)

*How-to guide* — This page gives steps to serve your runtime over MCP, with the [API reference](api-reference.md) for details and [Ontology design](ontology-design.md) for what to expose.

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

Alternatively, serve the ontology from the command line:

```bash
ontary serve your_app.ontology:ontology --dev --store ./dev.sqlite --port 8000
```

The dev server binds to `127.0.0.1` on port 8000. It requires the `--dev` flag.
The dev consumer sees unscoped rows only, unless the ontology declares matching dev scopes.

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

Each tool's required and optional arguments are listed in the
[MCP server reference](api-mcp.md#mcp-server). `execute_action` and
`call_function` take `api_name` plus one `params` object keyed by parameter name.

`list_functions` publishes each Function's declared `parameters` alongside its
descriptions. Typed Functions list each parameter's name, type, choices,
structured fields, required status, and referenced ontology type. A Function
with no inputs publishes `parameters: []`. It never publishes `parameters: null`.

`query_objects` accepts `where`, `order_by`, `limit`, and `after`. Its `where` grammar uses
bare scalars for equality and `gt`/`gte`/`lt`/`lte`/`in`/`ne`/`contains` operator
mappings, validated against declared property types; several operators in one
mapping are AND-ed, so `{"gte": a, "lt": b}` is a range. Unknown operators
return `UNKNOWN_OPERATOR`; incompatible operators or operands return
`OPERATOR_TYPE_MISMATCH`; unknown keys return the existing `UNKNOWN_FIELD` error.
The MCP server applies a default limit of 100 and refuses
an explicit limit above the hard maximum of 1000 with `INVALID_LIMIT`.
`count_objects` accepts the same `where` grammar and returns the number of rows
visible to the invoking consumer. It is not min-N-gated: it reveals only what
`query_objects` already lists. For a min-N-released count, use
`aggregate_objects(func="count")`, which needs no `value_field`.

`count_objects` and `aggregate_objects` are unbounded operations on this
untrusted surface: each can scan the full underlying type to apply its matching,
scope, row-visibility, and (for aggregation) release gates. They have no cap,
so callers should treat them as potentially expensive on large types;
`query_objects` caps the rows it returns, but an `order_by` page sorts the whole
type and costs the same as `count_objects`.

The successful response preserves the existing rows under `result` and adds an
opaque `next_cursor` alongside them.
The response also includes `scope_limited` and a `redacted_fields` list on each row.

```json
{
  "result": [{"payload": {"id": "book-1"}, "lineage": {}, "redacted_fields": []}],
  "next_cursor": "opaque-page-token",
  "has_more": true,
  "scope_limited": true
}
```

Pass that cursor back with the same explicit `limit` to fetch the next page. A
`null` cursor means the visible result set is exhausted. `after` without an
explicit `limit` returns `AFTER_WITHOUT_LIMIT`, and `limit < 1` continues to use
the underlying `INVALID_LIMIT` validation.

`traverse_links` is paged the same way: it accepts an optional `limit` (default
100, maximum 1000) and an `after` cursor, and its envelope carries `has_more` and
`next_cursor`. A cursor that no longer names a current, visible linked row returns
`STALE_CURSOR`; restart from the first page.

## Serve many proven identities

`build_multi_consumer_mcp_server` is the HTTP shape for a process that serves more
than one identity. It does not take a fixed `consumer`. Instead, each invocation
receives the MCP transport's already-verified `AccessToken`, calls your
`resolve_consumer` callback, and creates a cheap consumer view over the shared
runtime.

Here is a complete server you can run as it stands. It declares a one-type
ontology, seeds one ticket, and serves it to one authenticated agent:

```python
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from ontary import (
    Consumer, DirectProperty, ObjectStore, Ontology, OntologyObject, Source, prop,
)
from ontary.mcp_server import build_multi_consumer_mcp_server

ontology = Ontology(name="support", scope_levels=["queue"])


@ontology.object(
    layer="L0", scope=[DirectProperty(level="queue", property_name="queue_id")]
)
class Ticket(OntologyObject):
    id: str = prop(primary_key=True)
    subject: str
    queue_id: str = prop(scope_level="queue")


ontology.validate()
store = ObjectStore(ontology.registry)  # SQLite, in memory
store.insert(
    "Ticket",
    {"id": "ticket-1", "subject": "Invoice mismatch", "queue_id": "billing"},
    Source(source_system="demo"),
)


class StaticTokenVerifier:
    """A stand-in for your identity provider's verifier: a fixed token map."""

    def __init__(self, tokens: dict[str, AccessToken]) -> None:
        self._tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        return self._tokens.get(token)  # None = not authenticated (HTTP 401)


verifier = StaticTokenVerifier({
    "token-alice": AccessToken(
        token="token-alice", client_id="support-agent", scopes=[], subject="alice"
    ),
})

consumers = {
    "alice": Consumer(
        actor_id="alice", role="Agent", scope_level="queue", scope_id="billing",
        kind="human",
    ),
}


def resolve_consumer(token: AccessToken) -> Consumer | None:
    return consumers.get(token.subject or "")


server = build_multi_consumer_mcp_server(
    ontology,
    store,
    resolve_consumer=resolve_consumer,
    token_verifier=verifier,
    auth=AuthSettings(
        issuer_url=AnyHttpUrl("https://auth.example.com"),
        resource_server_url=AnyHttpUrl("http://localhost:8000"),
    ),
)

if __name__ == "__main__":
    server.run(transport="streamable-http", stateless_http=True, json_response=True)
```

`StaticTokenVerifier` is a stand-in that only makes the example run offline. A
deployment replaces it with its identity provider's verifier, for example JWT
validation against the provider's JWKS or OAuth token introspection. The
verifier must be an `async def verify_token` that returns an `AccessToken`, or
`None` to refuse the token. `AuthSettings` names the issuer that minted the
token and this server's own URL. Running the file serves
`http://localhost:8000/mcp`.

An agent calls a tool with its bearer token:

```http
POST /mcp HTTP/1.1
Host: localhost:8000
Authorization: Bearer token-alice
Accept: application/json, text/event-stream
Content-Type: application/json

{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "query_objects", "arguments": {"obj_type": "Ticket"}}}
```

The server answers `200 OK` with the rows that alice's `billing` queue scope
allows. `content` carries the same result as text for clients that do not read
`structuredContent`; `valid_from` is the time the row was inserted.
`structuredContent` also includes `scope_limited` and a `redacted_fields` list on each row.

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [{"type": "text", "text": "<the structuredContent below, as JSON text>"}],
    "isError": false,
    "structuredContent": {
      "result": [
        {
          "payload": {"id": "ticket-1", "subject": "Invoice mismatch", "queue_id": "billing"},
          "lineage": {
            "object_type": "Ticket",
            "object_id": "ticket-1",
            "valid_from": "<insert time>",
            "valid_to": null,
            "source_system": "demo",
            "source_id": null,
            "extracted_at": null
          },
          "redacted_fields": []
        }
      ],
      "next_cursor": null,
      "has_more": false,
      "scope_limited": true
    }
  }
}
```

The same request without the `Authorization` header, or with a token the
verifier does not know, is refused with `401 Unauthorized`. The MCP transport
refuses it before ontary resolves a consumer, so no `resolve_consumer` call and
no audit entry happens:

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json
WWW-Authenticate: Bearer error="invalid_token", error_description="Authentication required", resource_metadata="http://localhost:8000/.well-known/oauth-protected-resource"

{"error": "invalid_token", "error_description": "Authentication required"}
```

The `200` body above is what `json_response=True` returns. Without it, the server
streams the same JSON-RPC message as a server-sent event. The `401` is the same in
both modes.

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
`AuthSettings` into the builder so `MCPServer` can install its authentication pipeline.
The callback receives an `AccessToken` that MCP has already verified.

If a request has no verified token, the server raises `PermissionDenied` with code
`UNAUTHENTICATED`. With `token_verifier` and `AuthSettings` configured, a missing or
unknown bearer token never gets this far: the transport answers with the `401`
shown above. `UNAUTHENTICATED` is what a server built without them, or run over
stdio, returns. If a verified principal has no mapped `Consumer`, the server raises
`PermissionDenied` with code `CONSUMER_UNRESOLVED`. These are distinct recovery
paths: configure authentication for the first and map the principal for the
second. Both happen before an `OntologyClient` exists, so neither is audited as an
ontology action or Function.

Once a request resolves, audit entries retain both identities: `principal` records
who the transport proved and `actor`/`role` records the consumer the resolver
selected. That makes a resolver which maps every principal to one privileged actor
visible to an auditor.

## Transport requirements

The multi-consumer builder returns an `MCPServer` but does not choose a transport
mode. On mcp 2.x, transport options (`stateless_http`, `json_response`,
`transport_security`, `host`) are keyword arguments of `run()` and
`streamable_http_app()`, and `port` of `run()`. Each request resolves its own token in stateful sessions
as well, and the SDK's test suite pins both modes at the ASGI boundary.

```python
server.run(transport="streamable-http", stateless_http=True, json_response=True)
```

`stdio` carries no bearer-token context. A multi-consumer server run over stdio
therefore refuses calls with `UNAUTHENTICATED`; use the single-consumer
`build_mcp_server(ontology, store, consumer)` for a zero-infrastructure stdio
process. The SDK does not provide its own HTTP entrypoint: the deployer runs the
`MCPServer` instance or wraps its ASGI application in the host's process.

See [the API reference MCP section](api-reference.md#mcp-server) for the builder
signatures and tool behavior, and its
[`Declarations`](api-reference.md#descriptor-authoring) paragraph for the runtime's
declared identity contract.

[Return to the README](../README.md) · [Return to the API reference](api-reference.md)
