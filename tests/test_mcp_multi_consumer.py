"""In-process, offline coverage for `build_multi_consumer_mcp_server` (spec
`multi-consumer-mcp.md` §3 AC1-AC9/AC13, §7 test matrix rows #11-#20).

No socket, no OAuth issuer: the MCP auth contextvar is set/reset directly
(`mcp.server.auth.middleware.auth_context.auth_context_var`), exactly the
technique this task's brief verified works, and `MCPServer.call_tool` invokes
the tool function in-process -- same no-network approach `tests/test_mcp.py`
and `tests/test_mcp_resolver_seam.py` already use.

The "library" ontology/store/`_call` helper are duplicated here rather than
imported from `tests/test_mcp.py`, for the same reason
`test_mcp_resolver_seam.py` duplicates its own: this file must stay
independent of that one so `tests/test_mcp.py` provably stays untouched
(spec AC12).

Covers, by spec §7 row:
- #11 no auth context -> every one of the TEN registered tools (driven off
  the server's own `list_tools()`, not a hardcoded sample -- lesson L-c) ->
  `UNAUTHENTICATED`.
- #12 `resolve_consumer` returns `None` -> `CONSUMER_UNRESOLVED`.
- #13 `resolve_consumer` raises -> generic `INTERNAL_ERROR`, raised message
  absent from the payload, over all twelve tools.
- #14 the bearer token string itself never appears in an error payload.
- #15 A/B interleaved on ONE server: scoped reads differ, `execute_action`
  refused outside scope.
- #16 resolver call count == invocation count (no caching).
- #17 `client.actions` identical by identity across two identities' calls.
- #18 a resolver that presets `principal` has it overwritten from the token.
- #19 `subject=None` falls back to `client_id`.
- #20 golden end-to-end: an authenticated `execute_action` writes an audit
  row where `actor != principal`.

Plus, added after a whole-branch `/review` BLOCKED this branch with a P0 no
per-task review caught (not one of the numbered rows above -- ledger task
T10): `test_stateless_http_serves_each_request_its_own_token` drives the
REAL ASGI app (`httpx2.ASGITransport` over `MCPServer.streamable_http_app()`,
a stub `TokenVerifier`, no socket) rather than setting the contextvar
directly, because the contextvar shortcut every OTHER test in this file
uses cannot tell "resolved from this call's token" apart from "resolved
from a token frozen at session-establishment and read N times" -- see that
test's own docstring.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx2
import pytest
from conftest import _mcp_uninstalled
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import AnyHttpUrl

from ontary.actions import ActionContext, ActionError
from ontary.authoring import ActionParams, Ontology, OntologyObject, prop, scope_ref, target
from ontary.client import OntologyClient, OntologyRuntime
from ontary.functions import BoundQuery
from ontary.mcp_server import build_multi_consumer_mcp_server
from ontary.meta import Cardinality
from ontary.scope import DirectProperty, SelfScope, ViaLink
from ontary.security import Consumer
from ontary.store import ObjectStore, Source

LEVELS = ["shelf", "library"]
SRC = Source(source_system="test")


def _library_ontology() -> tuple[Ontology, type[OntologyObject]]:
    """Same shapes as `test_mcp.py`'s `_base_library_ontology` (Library/
    Shelf/Book, `inLibrary`/`onShelf` links, `RelocateBook` action,
    `countBooksOnShelf` function) -- duplicated, not imported, per this
    file's own docstring."""
    ontology = Ontology(name="library-mc", scope_levels=LEVELS, min_n=1)

    @ontology.object(layer="L0", scope=[SelfScope(level="library")])
    class Library(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[
            SelfScope(level="shelf"),
            ViaLink(link_api_name="inLibrary", direction="from", parent_type="Library"),
        ],
    )
    class Shelf(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[
            DirectProperty(level="shelf", property_name="shelf_id"),
            ViaLink(link_api_name="onShelf", direction="from", parent_type="Shelf"),
        ],
    )
    class Book(OntologyObject):
        id: str = prop(primary_key=True)
        shelf_id: str | None = prop(default=None, scope_level="shelf")
        title: str | None = None

    ontology.link(
        "inLibrary", Shelf, Library, Cardinality.MANY_TO_ONE, description="Shelf -> its library"
    )
    ontology.link(
        "onShelf",
        Book,
        Shelf,
        Cardinality.MANY_TO_ONE,
        description="Book -> its shelf",
        owned=True,
    )

    class _RelocateBookParams(ActionParams):
        book_id: str = target(Book)
        shelf_id: str = scope_ref(Shelf)

    @ontology.action(
        _RelocateBookParams,
        target=Book,
        roles=["Librarian"],
        display_name="Relocate Book",
        description="Move a book to a different shelf",
        api_name="RelocateBook",
    )
    def _relocate(ctx: ActionContext, params: _RelocateBookParams) -> dict[str, str]:
        if ctx.read_current("Book", params.book_id) is None:
            raise ActionError(f"book {params.book_id!r} does not exist", code="PRECONDITION_FAILED")
        ctx.create_link("onShelf", params.book_id, params.shelf_id)
        return {"book_id": params.book_id}

    @ontology.function(
        description="Count the (visible) books on a shelf",
        input_description="shelf_id",
        output_description="int count",
        api_name="countBooksOnShelf",
    )
    def _count_books_on_shelf(query: BoundQuery, params: dict[str, Any]) -> int:
        return len(
            query.list(
                "Book", where={"shelf_id": params["shelf_id"]}, limit=None
            )
        )

    ontology.validate()
    return ontology, Book


def _build_store(ontology: Ontology) -> ObjectStore:
    """Two shelves under one library, one book on each -- book-1 on
    shelf-1, book-2 on shelf-2 -- so a shelf-scoped consumer's visible rows
    genuinely differ (spec AC3). Deliberately does NOT pre-create either
    book's `onShelf` link (only the `shelf_id` direct property, which is
    enough for scope on its own): `RelocateBook` creates that link, and
    `onShelf` is `MANY_TO_ONE`, so a book already linked would make its
    first relocate a `CARDINALITY_VIOLATION` rather than the success this
    suite's action tests need."""
    store = ObjectStore(ontology.registry)
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-1", "library_id": "lib-1"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
    store.insert("Shelf", {"id": "shelf-2", "library_id": "lib-1"}, SRC)
    store.create_link("inLibrary", "shelf-2", "lib-1")
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1", "title": "A"}, SRC)
    store.insert("Book", {"id": "book-2", "shelf_id": "shelf-2", "title": "B"}, SRC)
    return store


def _shelf_librarian(actor_id: str, scope_id: str) -> Consumer:
    return Consumer(
        actor_id=actor_id,
        role="Librarian",
        scope_level="shelf",
        scope_id=scope_id,
        kind="human",
    )


def _library_librarian(actor_id: str, scope_id: str = "lib-1") -> Consumer:
    return Consumer(
        actor_id=actor_id,
        role="Librarian",
        scope_level="library",
        scope_id=scope_id,
        kind="human",
    )


# Client id -> Consumer this suite's `resolve_consumer` doubles map by
# default: "agent-a"/"agent-b" are shelf-scoped to DIFFERENT shelves so
# their visible reads genuinely differ (AC3); "agent-lib" is scoped at the
# library level so it can relocate a book across shelves for the golden
# end-to-end sample (AC8, spec §3's "golden sample outputs").
_CONSUMERS: dict[str, Consumer] = {
    "agent-a": _shelf_librarian("agent-a", "shelf-1"),
    "agent-b": _shelf_librarian("agent-b", "shelf-2"),
    "agent-lib": _library_librarian("ryo"),
}


def _resolve_known_consumers(token: AccessToken) -> Consumer | None:
    return _CONSUMERS.get(token.client_id)


@contextmanager
def _authenticated_as(
    client_id: str, *, subject: str | None = None, token: str = "test-bearer-token"
) -> Iterator[None]:
    """Set the MCP auth contextvar for the duration of the block -- the
    offline equivalent of a bearer-authenticated request (verified in this
    task's brief to work with no OAuth issuer / socket involved)."""
    access_token = AccessToken(token=token, client_id=client_id, scopes=[], subject=subject)
    reset = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def _call(server: MCPServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call `name` in-process and decode the JSON payload the tool
    returned -- duplicated from `test_mcp.py`'s own helper of the same
    name, per this file's docstring."""
    result = asyncio.run(server.call_tool(name, arguments))
    if result.structured_content is not None:
        return result.structured_content
    payload: dict[str, Any] = json.loads(result.content[0].text)
    return payload


# All twelve tools `_register_tools` registers, each paired with arguments that
# let its body run to completion against `_build_store`'s data.
# `test_all_tools_matches_what_server_actually_registered` below
# cross-checks this tuple against the live server's `list_tools()` rather
# than trusting it by inspection (lesson L-c: guard the CLASS, not one
# hardcoded sample -- a twelfth tool must not be able to escape this
# file's assertions).
ALL_TOOLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("list_object_types", {}),
    ("list_link_types", {}),
    ("list_action_types", {}),
    ("list_functions", {}),
    ("get_declarations", {}),
    ("get_object", {"obj_type": "Book", "obj_id": "book-1"}),
    ("query_objects", {"obj_type": "Book", "where": None}),
    ("count_objects", {"obj_type": "Book", "where": None}),
    (
        "aggregate_objects",
        {"obj_type": "Book", "value_field": "rating", "func": "mean"},
    ),
    ("traverse_links", {"obj_type": "Book", "obj_id": "book-1", "link_api_name": "onShelf"}),
    (
        "execute_action",
        {"api_name": "RelocateBook", "params": {"book_id": "book-1", "shelf_id": "shelf-1"}},
    ),
    ("call_function", {"api_name": "countBooksOnShelf", "params": {"shelf_id": "shelf-1"}}),
)


def _build_multi_consumer_server(
    resolve_consumer: Any = _resolve_known_consumers,
) -> tuple[MCPServer, ObjectStore]:
    ontology, _Book = _library_ontology()
    store = _build_store(ontology)
    server = build_multi_consumer_mcp_server(ontology, store, resolve_consumer=resolve_consumer)
    return server, store


def test_all_tools_matches_what_server_actually_registered() -> None:
    """`ALL_TOOLS` must be the ACTUAL tool set the server exposes, not a
    hardcoded tuple that merely happens to agree with it today (P1-2 class
    of bug) -- a twelfth tool must show up here as a failure."""
    server, _store = _build_multi_consumer_server()
    assert {t.name for t in asyncio.run(server.list_tools())} == {n for n, _ in ALL_TOOLS}


# -- #11: no auth context -> UNAUTHENTICATED, every tool ------------------


@pytest.mark.parametrize("name,arguments", ALL_TOOLS, ids=[n for n, _ in ALL_TOOLS])
def test_no_auth_context_every_tool_is_unauthenticated(
    name: str, arguments: dict[str, Any]
) -> None:
    server, _store = _build_multi_consumer_server()
    # No `_authenticated_as(...)` block: the contextvar is at its default
    # (unset), exactly like stdio (AC13) or HTTP with no `token_verifier`
    # configured.
    payload = _call(server, name, arguments)
    error = payload["error"]
    assert set(error) == {"type", "message", "code", "kind"}
    assert error["message"] == (
        "no authenticated principal on this request -- a "
        "multi-consumer MCP server requires a verified access "
        "token. Two likely causes: (1) this process is running "
        "bare server.run(), which defaults to stdio and carries "
        "no auth context at all -- run "
        "server.run(transport='streamable-http') instead, or use "
        "build_mcp_server(ontology, store, consumer) for a "
        "single-consumer stdio process; (2) it's already "
        "streamable HTTP but token_verifier= and auth= were not "
        "passed to build_multi_consumer_mcp_server(...)"
    )
    assert error["type"] == "PermissionDenied"
    assert error["code"] == "UNAUTHENTICATED"
    assert error["kind"] == "permission"
    assert "result" not in payload, (
        f"{name} did not fail closed with UNAUTHENTICATED: {payload!r}"
    )


# -- #12: resolver returns None -> CONSUMER_UNRESOLVED ---------------------


def test_resolver_returning_none_is_consumer_unresolved() -> None:
    server, _store = _build_multi_consumer_server(resolve_consumer=lambda _token: None)
    with _authenticated_as("agent-a"):
        payload = _call(server, "get_object", {"obj_type": "Book", "obj_id": "book-1"})
    assert payload["error"]["type"] == "PermissionDenied"
    assert payload["error"]["code"] == "CONSUMER_UNRESOLVED"
    assert payload["error"]["kind"] == "permission"
    assert "result" not in payload


# -- #13: resolver raises -> generic INTERNAL_ERROR, message not echoed ---


@pytest.mark.parametrize("name,arguments", ALL_TOOLS, ids=[n for n, _ in ALL_TOOLS])
def test_raising_resolver_fails_closed_without_leaking_message(
    name: str, arguments: dict[str, Any]
) -> None:
    secret = "super-secret-token-material-nobody-should-see"

    def _raising_resolver(_token: AccessToken) -> Consumer | None:
        raise ValueError(secret)

    server, _store = _build_multi_consumer_server(resolve_consumer=_raising_resolver)
    with _authenticated_as("agent-a"):
        payload = _call(server, name, arguments)

    assert payload == {
        "error": {
            "type": "InternalError",
            "message": "internal server error",
            "code": "INTERNAL_ERROR",
            "kind": "internal",
        }
    }, f"{name} did not fail closed generically: {payload!r}"
    assert secret not in json.dumps(payload)


# -- #14: the bearer token string never appears in an error payload -------


def test_token_string_absent_from_every_error_payload() -> None:
    token_value = "sekrit-bearer-abc123-do-not-leak"

    # CONSUMER_UNRESOLVED path.
    server, _store = _build_multi_consumer_server(resolve_consumer=lambda _t: None)
    with _authenticated_as("agent-a", token=token_value):
        unresolved_payload = _call(server, "get_object", {"obj_type": "Book", "obj_id": "book-1"})
    assert token_value not in json.dumps(unresolved_payload)

    # INTERNAL_ERROR path (resolver raises, possibly embedding the token).
    def _raising_resolver(token: AccessToken) -> Consumer | None:
        raise RuntimeError(f"could not map token {token.token!r}")

    server, _store = _build_multi_consumer_server(resolve_consumer=_raising_resolver)
    with _authenticated_as("agent-a", token=token_value):
        internal_payload = _call(server, "get_object", {"obj_type": "Book", "obj_id": "book-1"})
    assert token_value not in json.dumps(internal_payload)


# -- #15: A/B interleaved on one server -- isolation + scope refusal -------


def test_interleaved_identities_see_only_their_own_scope_and_cannot_cross_act() -> None:
    server, _store = _build_multi_consumer_server()

    with _authenticated_as("agent-a"):
        a_first = _call(server, "query_objects", {"obj_type": "Book", "where": None})
    with _authenticated_as("agent-b"):
        b = _call(server, "query_objects", {"obj_type": "Book", "where": None})
    with _authenticated_as("agent-a"):
        a_second = _call(server, "query_objects", {"obj_type": "Book", "where": None})

    a_first_ids = {row["payload"]["id"] for row in a_first["result"]}
    a_second_ids = {row["payload"]["id"] for row in a_second["result"]}
    b_ids = {row["payload"]["id"] for row in b["result"]}

    assert a_first_ids == a_second_ids == {"book-1"}
    assert b_ids == {"book-2"}

    # A cannot execute an action targeting an object outside A's scope.
    with _authenticated_as("agent-a"):
        denied = _call(
            server,
            "execute_action",
            {"api_name": "RelocateBook", "params": {"book_id": "book-2", "shelf_id": "shelf-1"}},
        )
    assert "error" in denied
    assert "result" not in denied

    # B, symmetrically, can act within its own scope.
    with _authenticated_as("agent-b"):
        allowed = _call(
            server,
            "execute_action",
            {"api_name": "RelocateBook", "params": {"book_id": "book-2", "shelf_id": "shelf-2"}},
        )
    assert allowed == {"result": {"book_id": "book-2"}}


@pytest.mark.parametrize(
    ("name", "arguments", "parameter", "expected_type"),
    [
        pytest.param(
            "get_object",
            {"obj_type": [], "obj_id": "book-1"},
            "obj_type",
            "a string",
            id="required-string",
        ),
        pytest.param(
            "execute_action",
            {"api_name": "RelocateBook", "params": []},
            "params",
            "an object",
            id="required-object",
        ),
        pytest.param(
            "query_objects",
            {"obj_type": "Book", "where": []},
            "where",
            "an object",
            id="optional-object",
        ),
        pytest.param(
            "query_objects",
            {"obj_type": "Book", "limit": "many"},
            "limit",
            "an integer or null",
            id="optional-integer",
        ),
        pytest.param(
            "query_objects",
            {"obj_type": "Book", "limit": True},
            "limit",
            "an integer or null",
            id="boolean-is-not-an-integer",
        ),
        pytest.param(
            "query_objects",
            {"obj_type": "Book", "after": 9, "limit": 1},
            "after",
            "a string",
            id="optional-string",
        ),
        pytest.param(
            "traverse_links",
            {
                "obj_type": "Book",
                "obj_id": "book-1",
                "link_api_name": "onShelf",
                "reverse": None,
            },
            "reverse",
            "a boolean",
            id="reverse-explicit-null",
        ),
        pytest.param(
            "traverse_links",
            {
                "obj_type": "Book",
                "obj_id": "book-1",
                "link_api_name": "onShelf",
                "reverse": [1],
            },
            "reverse",
            "a boolean",
            id="reverse-list",
        ),
        pytest.param(
            "traverse_links",
            {
                "obj_type": "Book",
                "obj_id": "book-1",
                "link_api_name": "onShelf",
                "reverse": "yes",
            },
            "reverse",
            "a boolean",
            id="reverse-string-yes",
        ),
        pytest.param(
            "traverse_links",
            {
                "obj_type": "Book",
                "obj_id": "book-1",
                "link_api_name": "onShelf",
                "reverse": "true",
            },
            "reverse",
            "a boolean",
            id="reverse-string-true",
        ),
        pytest.param(
            "traverse_links",
            {
                "obj_type": "Book",
                "obj_id": "book-1",
                "link_api_name": "onShelf",
                "reverse": 1,
            },
            "reverse",
            "a boolean",
            id="reverse-integer-one",
        ),
        pytest.param(
            "traverse_links",
            {
                "obj_type": "Book",
                "obj_id": "book-1",
                "link_api_name": "onShelf",
                "reverse": 0,
            },
            "reverse",
            "a boolean",
            id="reverse-integer-zero",
        ),
    ],
)
def test_wrong_typed_tool_arguments_return_structured_envelopes(
    name: str,
    arguments: dict[str, Any],
    parameter: str,
    expected_type: str,
) -> None:
    """The old suite had no reverse values that escaped/coerced at the MCP
    boundary. These JSON inputs force every reverse failure through the same
    structured envelope as the sibling argument validators."""
    server, _store = _build_multi_consumer_server()

    with _authenticated_as("agent-a"):
        payload = _call(server, name, arguments)

    assert payload == {
        "error": {
            "type": "ValueError",
            "message": (
                f"tool parameter {parameter!r} must be {expected_type}, got "
                f"{type(arguments[parameter]).__name__}"
            ),
            "code": "INVALID_PARAMS",
            "kind": "validation",
        }
    }


@pytest.mark.parametrize(
    ("name", "arguments", "parameter"),
    [
        pytest.param("get_object", {}, "obj_type", id="get-object-empty-arguments"),
        pytest.param(
            "execute_action",
            {"api_name": "RelocateBook"},
            "params",
            id="execute-action-missing-params",
        ),
    ],
)
def test_missing_required_tool_arguments_return_structured_envelopes(
    name: str,
    arguments: dict[str, Any],
    parameter: str,
) -> None:
    server, _store = _build_multi_consumer_server()

    with _authenticated_as("agent-a"):
        payload = _call(server, name, arguments)

    assert payload == {
        "error": {
            "type": "ValueError",
            "message": f"missing required tool parameter {parameter!r}",
            "code": "INVALID_PARAMS",
            "kind": "validation",
        }
    }


@pytest.mark.parametrize(
    "unserializable",
    [
        pytest.param({"value"}, id="set"),
        pytest.param(float("inf"), id="non-finite-float"),
    ],
)
def test_unserializable_tool_result_returns_internal_error_envelope(
    monkeypatch: pytest.MonkeyPatch,
    unserializable: Any,
) -> None:
    def _return_unserializable(
        _self: OntologyClient, _api_name: str, _params: dict[str, Any]
    ) -> Any:
        return {"not-json": unserializable}

    monkeypatch.setattr(OntologyClient, "call_function", _return_unserializable)
    server, _store = _build_multi_consumer_server()

    with _authenticated_as("agent-a"):
        payload = _call(
            server,
            "call_function",
            {"api_name": "countBooksOnShelf", "params": {"shelf_id": "shelf-1"}},
        )

    assert payload == {
        "error": {
            "type": "InternalError",
            "message": "internal server error",
            "code": "INTERNAL_ERROR",
            "kind": "internal",
        }
    }


# -- #16: resolver call count == invocation count (no caching) ------------


def test_resolver_call_count_matches_invocation_count() -> None:
    calls = {"n": 0}

    def _counting_resolver(token: AccessToken) -> Consumer | None:
        calls["n"] += 1
        return _resolve_known_consumers(token)

    server, _store = _build_multi_consumer_server(resolve_consumer=_counting_resolver)

    with _authenticated_as("agent-a"):
        for name, arguments in ALL_TOOLS:
            before = calls["n"]
            _call(server, name, arguments)
            assert calls["n"] == before + 1, (
                f"{name} did not call resolve_consumer exactly once per "
                f"invocation (count went from {before} to {calls['n']})"
            )

    with _authenticated_as("agent-b"):
        before = calls["n"]
        _call(server, "get_declarations", {})
        assert calls["n"] == before + 1


# -- #17: `client.actions` identical by identity across identities --------


def test_actions_identical_by_identity_across_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7's observable: two different identities' calls share the SAME
    `ActionExecutor` object by identity, because exactly one `OntologyRuntime`
    is built at server-construction time and every call only ever goes
    through `runtime.for_consumer(...)`. Spied on `OntologyRuntime.
    for_consumer` itself (rather than trusting a lower-level unit test of
    that method alone) so a future regression that builds a fresh
    `OntologyRuntime` per call inside `build_multi_consumer_mcp_server`
    would be caught here too."""
    seen_action_ids: list[int] = []
    original_for_consumer = OntologyRuntime.for_consumer

    def _spying_for_consumer(self: OntologyRuntime, consumer: Consumer, **kwargs: Any) -> Any:
        client = original_for_consumer(self, consumer, **kwargs)
        seen_action_ids.append(id(client.actions))
        return client

    monkeypatch.setattr(OntologyRuntime, "for_consumer", _spying_for_consumer)

    server, _store = _build_multi_consumer_server()
    with _authenticated_as("agent-a"):
        _call(server, "query_objects", {"obj_type": "Book", "where": None})
    with _authenticated_as("agent-b"):
        _call(server, "query_objects", {"obj_type": "Book", "where": None})

    assert len(seen_action_ids) == 2
    assert seen_action_ids[0] == seen_action_ids[1], (
        "two different identities' calls resolved to DIFFERENT `actions` "
        "objects -- expected the same ActionExecutor by identity (AC7)"
    )


# -- #18: a resolver that presets `principal` has it overwritten ----------


def test_resolver_preset_principal_is_overwritten_from_token() -> None:
    def _forging_resolver(token: AccessToken) -> Consumer | None:
        base = _resolve_known_consumers(token)
        assert base is not None
        # A resolver deliberately (or buggily) setting `principal` itself
        # must NOT be trusted -- AC9 says the server stamps it AFTER this
        # call returns.
        return base.model_copy(update={"principal": "attacker-controlled"})

    server, store = _build_multi_consumer_server(resolve_consumer=_forging_resolver)
    with _authenticated_as("agent-lib", subject="real-subject-99"):
        payload = _call(
            server,
            "execute_action",
            {"api_name": "RelocateBook", "params": {"book_id": "book-1", "shelf_id": "shelf-2"}},
        )
    assert payload == {"result": {"book_id": "book-1"}}
    entry = store.audit_entries()[-1]
    assert entry.principal == "real-subject-99"
    assert entry.principal != "attacker-controlled"


# -- #19: subject=None falls back to client_id -----------------------------


def test_subject_none_falls_back_to_client_id() -> None:
    server, store = _build_multi_consumer_server()
    with _authenticated_as("agent-lib", subject=None):
        payload = _call(
            server,
            "execute_action",
            {"api_name": "RelocateBook", "params": {"book_id": "book-1", "shelf_id": "shelf-2"}},
        )
    assert payload == {"result": {"book_id": "book-1"}}
    entry = store.audit_entries()[-1]
    assert entry.principal == "agent-lib"  # falls back to client_id


# -- #20: golden end-to-end -- actor != principal, and the log says so ----


def test_golden_end_to_end_actor_differs_from_principal_in_audit_log() -> None:
    """Spec §3's golden sample: an authenticated `execute_action` writes an
    audit row where `actor` (who this request resolved to) and `principal`
    (who the transport proved) differ."""
    server, store = _build_multi_consumer_server()
    with _authenticated_as("agent-lib", subject="svc-agent-7"):
        payload = _call(
            server,
            "execute_action",
            {"api_name": "RelocateBook", "params": {"book_id": "book-1", "shelf_id": "shelf-2"}},
        )
    assert payload == {"result": {"book_id": "book-1"}}

    entry = store.audit_entries()[-1]
    assert entry.actor == "ryo"
    assert entry.role == "Librarian"
    assert entry.principal == "svc-agent-7"
    assert entry.action == "RelocateBook"
    assert entry.kind == "action"
    assert entry.invocation_id is not None
    assert entry.actor != entry.principal


# -- optional-extra ergonomics (spec §6 "mcp extra missing"): the twin of --
# -- `tests/test_mcp.py::test_missing_mcp_extra_names_the_install_command` --


def test_missing_mcp_extra_names_the_install_command_multi_consumer() -> None:
    """`build_multi_consumer_mcp_server`'s twin of `test_mcp.py`'s check for
    `build_mcp_server`: with `mcp` uninstalled, this builder must also raise
    an `ImportError` naming the install command rather than a bare
    `ModuleNotFoundError`.

    This is the ONLY path that pins `_load_get_access_token`'s
    `MCP_EXTRA_HINT` branch as live: `build_multi_consumer_mcp_server` calls
    `_load_get_access_token()` before `_load_mcp_server()` (see that function's
    docstring), so with both loaders blocked, `_load_get_access_token` is
    the one that raises first -- not `_load_mcp_server`.
    """
    ontology, _Book = _library_ontology()
    store = _build_store(ontology)

    with _mcp_uninstalled():
        with pytest.raises(ImportError) as exc_info:
            build_multi_consumer_mcp_server(
                ontology, store, resolve_consumer=_resolve_known_consumers
            )

    message = str(exc_info.value)
    assert "optional `mcp` dependency" in message
    assert "unsupported" not in message
    # The cause is preserved, so the real import failure is still diagnosable.
    assert isinstance(exc_info.value.__cause__, ModuleNotFoundError)




# -- ledger T10 P1: passing exactly one of token_verifier/auth is a -------
# -- construction-time ValueError, NOT a runtime fail-closed state --------


def test_token_verifier_without_auth_raises_valueerror_at_construction() -> None:
    """`MCPServer.__init__` itself refuses this combination -- construction
    never completes and no call is ever made, so this is fail-FAST, not
    the per-call fail-CLOSED sequence (#11 above). Measured through the
    public builder against the actual installed `mcp`, not asserted from
    the docstring's claim about it (that claim was itself once wrong for
    the SSE case -- see this file's other tests -- so it is pinned here,
    not just described in prose)."""
    ontology, _Book = _library_ontology()
    store = _build_store(ontology)

    with pytest.raises(ValueError, match="auth"):
        build_multi_consumer_mcp_server(
            ontology,
            store,
            resolve_consumer=_resolve_known_consumers,
            token_verifier=_StubTokenVerifier({}),
        )


def test_auth_without_token_verifier_raises_valueerror_at_construction() -> None:
    """The other half of the pair above: `auth=` alone, no `token_verifier=`
    and no `auth_server_provider=`, is equally a construction-time
    `ValueError`."""
    ontology, _Book = _library_ontology()
    store = _build_store(ontology)

    with pytest.raises(ValueError, match="auth"):
        build_multi_consumer_mcp_server(
            ontology,
            store,
            resolve_consumer=_resolve_known_consumers,
            auth=AuthSettings(
                issuer_url=AnyHttpUrl("https://issuer.example"),
                resource_server_url=AnyHttpUrl("https://resource.example"),
            ),
        )


# -- whole-branch /review P0 (ledger T10): per-call identity, at the ASGI --
# -- level, driving REAL bearer auth instead of the contextvar shortcut ----


class _StubTokenVerifier:
    """The offline stand-in for a deployer's real `TokenVerifier` (JWKS,
    introspection, whatever) -- verifies exactly the two synthetic bearer
    tokens this test issues, spec §4.1: `ontary` verifies no token, `mcp`'s
    own `TokenVerifier` does."""

    def __init__(self, tokens: dict[str, AccessToken]) -> None:
        self._tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        return self._tokens.get(token)


def test_stateless_http_serves_each_request_its_own_token() -> None:
    """Drive the real ASGI auth pipeline and prove the call sees its token.

    No socket or network is involved: `httpx2.ASGITransport` invokes the
    `MCPServer.streamable_http_app()` ASGI callable in-process.
    """

    async def _run() -> None:
        ontology, _Book = _library_ontology()
        store = _build_store(ontology)

        seen: list[tuple[str, tuple[str, ...]]] = []

        def _recording_resolver(token: AccessToken) -> Consumer | None:
            # Records the (subject, scopes) this INVOCATION was resolved
            # with -- not a count (test #16 already covers "called once
            # per invocation"; this test is about WHICH token each call
            # actually saw).
            seen.append((token.subject or "", tuple(token.scopes)))
            return _library_librarian("ryo")

        # Same subject/client_id on both tokens, only `scopes` differs --
        # exactly the shape of a token that got RE-SCOPED (e.g. downgraded
        # by an IdP) mid-session, per spec §6's corrected "token expires
        # mid-session" row: the transport re-verifies the bearer token on
        # every request, so a re-scoped or revoked token must be refused
        # -- or, here, correctly re-resolved -- on the very next call.
        token_a = AccessToken(
            token="tok-A", client_id="agent", scopes=["read"], subject="same-subject"
        )
        token_b = AccessToken(
            token="tok-B", client_id="agent", scopes=["admin"], subject="same-subject"
        )

        server = build_multi_consumer_mcp_server(
            ontology,
            store,
            resolve_consumer=_recording_resolver,
            # The public seam (this task's brief, P0): forwarded VERBATIM
            # to `MCPServer(...)` by `build_multi_consumer_mcp_server` --
            # the only place either can be wired, per that function's own
            # docstring. `auth` must be set alongside `token_verifier` --
            # MCPServer only wires `BearerAuthBackend`/`AuthContextMiddleware`
            # into the ASGI app when BOTH are present (see `MCPServer.
            # streamable_http_app`); neither URL is ever dereferenced by
            # anything this test exercises.
            token_verifier=_StubTokenVerifier({"tok-A": token_a, "tok-B": token_b}),
            auth=AuthSettings(
                issuer_url=AnyHttpUrl("https://issuer.example.test"),
                resource_server_url=AnyHttpUrl("https://resource.example.test"),
            ),
        )

        app = server.streamable_http_app(
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )

        async with server.session_manager.run():
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as client:
                init_response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": LATEST_PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": {"name": "p0-probe", "version": "0.0.0"},
                        },
                    },
                    headers={
                        "Authorization": "Bearer tok-A",
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                    },
                )
                assert init_response.status_code == 200, init_response.text
                session_id = init_response.headers.get("mcp-session-id")

                call_headers = {
                    "Authorization": "Bearer tok-B",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                }
                # Reuse request #1's session id, if the transport minted
                # one -- stateless mode never does (there is no session to
                # reuse: `_handle_stateless_request` ignores this header
                # entirely), stateful mode always does, and resending it
                # is exactly what a real client does to keep talking to
                # "the same" MCP session. This is the header that routes
                # request #2 into request #1's frozen session task under
                # the OLD (stateful) behavior -- see the mutation check
                # above.
                if session_id is not None:
                    call_headers["mcp-session-id"] = session_id

                call_response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "get_declarations", "arguments": {}},
                    },
                    headers=call_headers,
                )
                assert call_response.status_code == 200, call_response.text

        assert seen == [("same-subject", ("admin",))], (
            f"resolve_consumer saw {seen!r} for the tools/call request -- "
            "expected exactly one call, resolved from THIS request's own "
            "token (subject 'same-subject', scopes ('admin',)), not a "
            "token frozen at `initialize` time"
        )

    asyncio.run(_run())
