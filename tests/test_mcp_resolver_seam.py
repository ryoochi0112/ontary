"""Coverage for the `resolve_client` seam `_register_tools` wires (spec
`multi-consumer-mcp.md` §4.3) that `tests/test_mcp.py` cannot see, since that
suite only ever exercises `build_mcp_server`'s constant, never-raising
resolver -- these tests call `_register_tools` directly with two doubles
instead:

- a COUNTING-AND-IDENTITY resolver, asserting each of the twelve tools calls
  `resolve_client()` exactly once per invocation, AND (for the seven tools
  that act on consumer data rather than schema, see `DATA_TOOLS` below)
  that the tool's result reflects the FRESH, distinguishable client
  returned by THIS invocation's `resolve_client()` call -- not some
  earlier one. This pins two separate claims:
    * the `mcp_server` module docstring's and `_register_tools`'s own
      docstring's claim that the four introspection tools "call
      `resolve_client()` too, purely to go through the same seam every
      other tool does" -- deleting that call (the "unused call, let me
      clean this up" mutation a future reader might make) leaves the
      count at zero for those four tools, and this is the only test in
      the repo that would notice;
    * that the tool body actually USES the object `resolve_client()`
      returns, rather than merely calling it and discarding the result --
      a resolver that is called once per invocation (satisfying the count
      assertion) but whose return value is silently swapped for a
      memoized/stale client is exactly the cross-consumer data leak this
      seam exists to prevent, and no other test in the repo would notice.
  Also asserts `ALL_TOOLS` below is exactly the set `_register_tools`
  registers, not a hardcoded sample a twelfth tool could silently
  escape.
- a RAISING resolver, asserting every one of the twelve tools -- both
  un-wrapped introspection tools (`_run_unwrapped`) and wrapped
  data/action tools (`_run`) -- fail closed into the same generic
  `INTERNAL_ERROR`/`internal` envelope with no trace of the raised
  message anywhere in the payload. This pins the docstrings' other claim
  -- "a raising `resolve_client` is classified by the exact same
      `_try`/`_run` machinery a coded error from inside a tool body already
  goes through" -- against a mutation that hoists the `resolve_client()`
  call out of ANY of the four introspection tools' `_list` closures
  (not just `list_object_types`, which used to be the only one checked --
  a hoist in `list_link_types`/`list_action_types`/`list_functions` alone
  used to escape both tests entirely), which would let the raise escape
  `_try` entirely (a bare exception out of `_register_tools` itself)
  instead of being classified into a structured envelope.

`tests/test_mcp.py` is deliberately untouched (spec AC12).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from ontary.mcp_server import _register_tools
from ontary.meta import OntologyRegistry
from ontary.ontology import OntologyDef
from ontary.scope import ScopePolicy


def _empty_ontology() -> OntologyDef:
    """An `OntologyDef` with nothing declared -- the four introspection
    tools read only `ontology.registry`, and an empty registry is enough
    for their bodies to run to completion (empty lists back)."""
    return OntologyDef("empty", OntologyRegistry(), ScopePolicy(levels=["global"]))


def _call(server: FastMCP, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call `name` in-process and decode the JSON payload the tool
    returned -- same shape as `tests/test_mcp.py`'s own `_call` helper,
    duplicated here rather than imported so this file stays independent of
    that one (spec AC12: `tests/test_mcp.py` stays untouched)."""
    result = asyncio.run(server.call_tool(name, arguments))
    if isinstance(result, tuple):
        _content, structured = result
        assert isinstance(structured, dict)
        return structured
    assert isinstance(result, list)
    assert len(result) == 1
    payload: dict[str, Any] = json.loads(result[0].text)  # type: ignore[union-attr]
    return payload


# All twelve tools `_register_tools` registers (spec `multi-consumer-mcp.md`
# §4.3), each paired with arguments that let its body run to completion
# against the stub clients below -- covering all twelve, not a sample (lesson
# L-c). `test_all_tools_matches_what_register_tools_actually_registered`
# below cross-checks this tuple against the live server rather than trusting
# it by inspection alone (P1-2: a twelfth tool must not be able to escape
# both this tuple and the assertions driven off it).
ALL_TOOLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("list_object_types", {}),
    ("list_link_types", {}),
    ("list_action_types", {}),
    ("list_functions", {}),
    ("get_declarations", {}),
    ("get_object", {"obj_type": "Book", "obj_id": "b1"}),
    ("query_objects", {"obj_type": "Book", "where": None}),
    ("count_objects", {"obj_type": "Book", "where": None}),
    (
        "aggregate_objects",
        {"obj_type": "Book", "value_field": "rating", "func": "mean"},
    ),
    (
        "traverse_links",
        {"obj_type": "Book", "obj_id": "b1", "link_api_name": "onShelf"},
    ),
    ("execute_action", {"api_name": "RelocateBook", "params": {}}),
    ("call_function", {"api_name": "countBooksOnShelf", "params": {}}),
)

# The seven tools whose body acts on the resolved client's DATA (as opposed to
# the four introspection tools, which deliberately read only
# `ontology.registry` and ignore whatever `resolve_client()` returns -- see
# `_register_tools`'s own docstring). Only these seven can meaningfully be
# checked for "did the result come from THIS invocation's client", which is
# a real asymmetry worth calling out rather than silently working around.
DATA_TOOLS: frozenset[str] = frozenset(
    {
        "get_declarations",
        "get_object",
        "query_objects",
        "count_objects",
        "aggregate_objects",
        "traverse_links",
        "execute_action",
        "call_function",
    }
)


class _Declarations:
    def model_dump(self) -> dict[str, Any]:
        return {}


class _Page:
    """Small page double for the client's paged string-list surface."""

    def __init__(self, items: list[Any]) -> None:
        self.items = items
        self.next_cursor: str | None = None


class _StubClient:
    """A minimal `OntologyClient` double: every method a tool body calls
    returns a JSON-serializable no-op value. Used only by the RAISING test,
    where `resolve_client` never actually returns one -- kept separate from
    `_FreshClient` below so that test's typing stays independent of the
    identity-tagging machinery the counting/identity test needs."""

    declarations = _Declarations()

    def get(self, obj_type: str, obj_id: str) -> None:
        return None

    def list(
        self,
        obj_type: str,
        where: dict[str, Any] | None = None,
        *,
        limit: int | None = None,
        after: str | None = None,
    ) -> _Page:
        return _Page([])

    def count(
        self, obj_type: str, where: dict[str, Any] | None = None
    ) -> int:
        return 0

    def aggregate(
        self,
        obj_type: str,
        value_field: str,
        where: dict[str, Any] | None = None,
        *,
        func: str = "mean",
    ) -> float:
        return 0.0

    def traverse(self, obj_type: str, obj_id: str, link_api_name: str) -> list[Any]:
        return []

    def execute(self, api_name: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def call_function(self, api_name: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}


class _Tagged:
    """Stands in for whatever `OntologyClient.get`/`.list`/`.traverse`
    return (a `StoredObject`, duck-typed here) -- its `model_dump` embeds
    `client_id` so `_serialize`'s `obj.model_dump(mode="json")` call
    produces a payload a test can trace back to the exact `_FreshClient`
    that produced it."""

    def __init__(self, client_id: int) -> None:
        self.client_id = client_id

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return {"client_id": self.client_id}


class _FreshClient:
    """A distinguishable `OntologyClient` double: constructed with a
    `client_id`, and every data-bearing method embeds that id in whatever
    it returns. A `resolve_client` that hands out a NEW `_FreshClient` per
    call (see `test_resolve_client_result_is_the_client_the_tool_acts_on`
    below) lets a test prove the tool body used THIS invocation's client --
    not merely that `resolve_client()` was called and its result discarded
    in favor of some earlier one (the memoized-stale-client mutation P1-3
    exists to catch)."""

    def __init__(self, client_id: int) -> None:
        self.client_id = client_id
        self.declarations = _TaggedDeclarations(client_id)

    def get(self, obj_type: str, obj_id: str) -> _Tagged:
        return _Tagged(self.client_id)

    def list(
        self,
        obj_type: str,
        where: dict[str, Any] | None = None,
        *,
        limit: int | None = None,
        after: str | None = None,
    ) -> _Page:
        return _Page([_Tagged(self.client_id)])

    def count(
        self, obj_type: str, where: dict[str, Any] | None = None
    ) -> int:
        return self.client_id

    def aggregate(
        self,
        obj_type: str,
        value_field: str,
        where: dict[str, Any] | None = None,
        *,
        func: str = "mean",
    ) -> int:
        return self.client_id

    def traverse(self, obj_type: str, obj_id: str, link_api_name: str) -> list[_Tagged]:
        return [_Tagged(self.client_id)]

    def execute(self, api_name: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"client_id": self.client_id}

    def call_function(self, api_name: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"client_id": self.client_id}


class _TaggedDeclarations:
    def __init__(self, client_id: int) -> None:
        self.client_id = client_id

    def model_dump(self) -> dict[str, Any]:
        return {"client_id": self.client_id}


def _client_ids_in(value: Any) -> list[int]:
    """Recursively pull every `client_id` a `_FreshClient`-derived payload
    embedded, out of whatever shape `_run`/`_run_unwrapped` wrapped it in
    (`{"result": {"client_id": ...}}` or `{"result": [{"client_id": ...}, ...]}`)."""
    if isinstance(value, dict):
        if "client_id" in value and isinstance(value["client_id"], int):
            return [value["client_id"]]
        return [i for v in value.values() for i in _client_ids_in(v)]
    if isinstance(value, list):
        return [i for v in value for i in _client_ids_in(v)]
    return []


def test_all_tools_matches_what_register_tools_actually_registered() -> None:
    """`ALL_TOOLS` must be the ACTUAL set `_register_tools` wires up, not a
    hardcoded tuple that merely happens to agree with it today (P1-2) -- an
    twelfth tool `_register_tools` grows must show up here as a failure,
    not silently escape both this file's assertions."""
    server = FastMCP("tool-inventory")
    _register_tools(server, _empty_ontology(), lambda: _StubClient())  # type: ignore[arg-type]

    assert {t.name for t in asyncio.run(server.list_tools())} == {n for n, _ in ALL_TOOLS}


def test_resolve_client_result_is_the_client_the_tool_acts_on() -> None:
    server = FastMCP("counting-and-identity")
    calls = {"n": 0}

    def resolve_client() -> _FreshClient:
        calls["n"] += 1
        return _FreshClient(calls["n"])

    _register_tools(server, _empty_ontology(), resolve_client)  # type: ignore[arg-type]

    for name, arguments in ALL_TOOLS:
        before = calls["n"]
        payload = _call(server, name, arguments)
        assert calls["n"] == before + 1, (
            f"{name} did not call resolve_client() exactly once"
            f" (count went from {before} to {calls['n']})"
        )

        if name not in DATA_TOOLS:
            continue

        this_invocation_id = calls["n"]
        if name in {"count_objects", "aggregate_objects"}:
            assert payload.get("result") == this_invocation_id
            continue
        found_ids = _client_ids_in(payload.get("result"))
        assert found_ids, (
            f"{name}'s result carried no client_id at all -- expected it"
            f" to reflect the client resolved for this invocation"
            f" ({this_invocation_id}); payload was {payload!r}"
        )
        assert all(i == this_invocation_id for i in found_ids), (
            f"{name}'s result did not reflect the client resolved for THIS"
            f" invocation (expected client_id {this_invocation_id}, found"
            f" {found_ids} -- a stale/memoized client is exactly the"
            f" cross-consumer data leak this seam exists to prevent);"
            f" payload was {payload!r}"
        )


def test_raising_resolver_fails_closed_without_leaking_message() -> None:
    server = FastMCP("raising")
    secret = "super-secret-internal-detail-nobody-should-see"

    def resolve_client() -> _StubClient:
        raise RuntimeError(secret)

    _register_tools(server, _empty_ontology(), resolve_client)  # type: ignore[arg-type]

    expected = {
        "error": {
            "type": "InternalError",
            "message": "internal server error",
            "code": "INTERNAL_ERROR",
            "kind": "internal",
        }
    }

    # Every one of the twelve tools -- unwrapped introspection (`_run_unwrapped`)
    # and wrapped data/action tools (`_run`) alike -- must share the
    # identical fail-closed envelope, and none may echo the raised message.
    # Driven off `ALL_TOOLS` (P1-1): a hoist of `resolve_client()` out of
    # just three of the four introspection tools' `_list` closures used to
    # escape this test entirely, since it only ever checked
    # `list_object_types` (never hoisted) and `get_declarations` (not an
    # introspection tool at all).
    for name, arguments in ALL_TOOLS:
        payload = _call(server, name, arguments)
        assert payload == expected, f"{name} did not fail closed: {payload!r}"
        assert secret not in json.dumps(payload)
