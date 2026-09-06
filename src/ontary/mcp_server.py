"""Generic MCP server: exposes any `OntologyDef` to an MCP client (e.g. a
Claude agent). Two builders share one implementation (spec
`multi-consumer-mcp.md` §4.3 "one tool body, two servers"):

- `build_mcp_server(ontology, store, consumer)` -- one server process bound
  to exactly one `Consumer` (spec §5, AC8/AC12), for a single-consumer
  stdio deployment. Unchanged, byte-identically, by everything below.
- `build_multi_consumer_mcp_server(ontology, store, resolve_consumer=...)`
  -- one server process serving MANY proven identities: each tool
  invocation resolves its own `Consumer` from that request's verified MCP
  `AccessToken` (spec `multi-consumer-mcp.md` §3 AC1-AC9).

Every tool here delegates to a single `OntologyClient` built from the
`(ontology, store, consumer)` triple this module is handed -- there is no
raw store/registry *data* read anywhere in this file (introspecting type
*definitions* off `ontology.registry` is fine and expected: those are
schema, not consumer data, and are identical for every consumer). This
mirrors `OntologyClient`'s own guarantee (see `ontary.client`'s docstring)
one layer further out.

All ten tool bodies are registered exactly once, by `_register_tools`,
against a `resolve_client: Callable[[], OntologyClient]` seam (spec
`multi-consumer-mcp.md` §4.3) instead of each tool closing directly over an
already-built client. `build_mcp_server` below passes a constant
`lambda: client` that never raises, so its own behavior is unchanged --
this module still builds exactly one `OntologyClient` per server, up
front, and every call reaches the very same object. `build_multi_consumer_
mcp_server` passes a resolver that reads the per-request `AccessToken`,
maps it to a `Consumer` via the caller's `resolve_consumer`, and *can*
raise (no token, no mapping, or the callback's own exception) -- that raise
is classified by the exact same `_try`/`_run` machinery an `OntaryError` from
inside a tool body already goes through, so the fail-closed envelope is not
a bespoke try/except of its own.

Deliberately NOT exposed: `client.ingest`/`client.ingest_links`. Spec §5
scopes the MCP surface to introspection + query/traverse/execute/call --
bulk data-loading is an unguarded, lineage-stamped bulk-upsert path (AC9),
not a consumer-facing read/act surface, and exposing it here would let any
MCP caller write arbitrary rows bypassing every guard this module otherwise
enforces.

Every tool catches the ENTIRE `OntaryError` family -- every domain exception
raised by the guarded layers or the store, organized by reaction kind
(`VisibilityError`, `PermissionDenied`, `PreconditionFailed`,
`ValidationFailed`, `AuthorityError`, `ConflictError`, and `InternalError`),
with the stable `code` identifying the specific refusal -- plus the two
bare-Python unknown-name errors
(`KeyError`/`ValueError`) and returns a structured `{"error": {"type": ...,
"message": ..., "code": ..., "kind": ...}}` payload -- never a partial
result, never a raw traceback (spec §7 "MCP server with a mis-scoped
consumer" edge case; declared-contracts §3 AC8: the `code` is the same
stable code the Python client surfaces for the same refusal, for EVERY
`kind`, not just visibility/permission/precondition). An `OntaryError` whose
`kind` is `internal` (e.g. the `STORE_ERROR` fallback) is deliberately routed
to the SAME generic, internals-free
envelope as any other unclassified exception (`code` `INTERNAL_ERROR`,
`kind` `internal`) rather than echoing its message -- an MCP tool must
never leak a traceback / internal state to its caller, and "internal" is
precisely the taxonomy bucket that promises nothing about what's safe to
say. Any *other* exception (not an `OntaryError`, not `KeyError`/`ValueError`)
is caught too and turned into that same generic envelope.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field, SkipValidation

from ontary.authoring import CapabilityHandle, EffectHandle, Ontology
from ontary.client import OntologyClient, OntologyRuntime
from ontary.effects import EffectDispatcher
from ontary.errors import OntaryError, PermissionDenied, ValidationFailed
from ontary.meta import ActionTypeDef, FunctionDef, LinkTypeDef, ObjectTypeDef
from ontary.ontology import OntologyDef, resolve_definition
from ontary.security import Consumer
from ontary.store import Store, StoredObject

if TYPE_CHECKING:
    from mcp.server.auth.provider import AccessToken, TokenVerifier
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations


# `Callable[["AccessToken"], Consumer | None]`, quoted: `AccessToken` only
# exists under `TYPE_CHECKING` above (see `_load_get_access_token`'s
# docstring for why it must stay out of the runtime import graph), so a
# real (unquoted) reference here would `NameError` the moment this module
# is imported without the `mcp` extra -- exactly the failure `_load_fastmcp`
# already exists to avoid for `FastMCP` itself.
ConsumerResolver = Callable[["AccessToken"], "Consumer | None"]
"""Maps one request's verified `AccessToken` to the `Consumer` it should act
as, or `None` if nothing maps it (spec `multi-consumer-mcp.md` §4.1). Must be
SYNCHRONOUS -- `mcp` 1.28.1 calls a sync tool directly on the event loop
(no worker-thread offload), so an async resolver would neither stop the
loop from blocking (the tool body's store I/O is synchronous anyway) nor
avoid forking the ten tool bodies into a sync copy and an async copy; see
`build_multi_consumer_mcp_server`'s docstring (§4.4).

If this callback raises a deliberate `OntaryError` (rather than returning
`None`), that error's `.message` IS echoed verbatim in the tool's error
envelope -- unlike any OTHER exception type, which is re-typed to a
message-free `RuntimeError` before it can reach a caller (AC6). A resolver
that raises `OntaryError` for its own refusals must therefore keep the
verified token, or anything derived from it, out of that message."""


# MCP is the consumer-facing bounded-read surface. Keep these values at
# module scope so the safety boundary is reviewable and mutation-tested rather
# than hidden in a tool body.
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


MCP_EXTRA_HINT = (
    "the MCP server needs the optional `mcp` dependency, which the core "
    "package does not install -- run `pip install 'ontary[mcp]'` "
    "(or `uv add 'ontary[mcp]'`) and try again"
)
"""What to tell someone whose `mcp` extra is missing.

`ontary-mcp` is a console script installed by the CORE package, and
`build_mcp_server` is one import away from any tutorial, so this is a
first-contact failure: before this existed, `pip install ontary` followed
by `ontary-mcp` produced a bare `ModuleNotFoundError: No module named 'mcp'`
with no indication that an extra exists. Found by installing the wheel into an
empty venv, which nothing in CI had ever done (M6 step 3).

Deliberately an `ImportError`, not an `OntaryError`. `ERROR_CODES` catalogues
refusals a CONSUMER can receive from a running runtime -- over MCP, that means
the structured error envelope. A missing optional dependency is an environment
problem that happens BEFORE any server exists to answer, and `ImportError` is
what a caller guarding an optional integration already catches.
"""

MCP_INCOMPATIBLE_HINT = (
    "the installed `mcp` version is unsupported by ontary -- install the "
    "`mcp>=1.27.2,<2` range with `pip install 'ontary[mcp]'` "
    "(or `uv add 'ontary[mcp]'`) and try again"
)
"""What to tell someone whose installed MCP has an incompatible layout."""


def _raise_mcp_import_error(exc: ModuleNotFoundError) -> None:
    """Classify an absent extra separately from an incompatible MCP layout."""
    if exc.name is not None and exc.name.split(".")[0] != "mcp":
        # Something inside `mcp` failed to import ITS OWN dependency. The
        # extra is installed and broken, which is a different problem with
        # a different fix, so the original error stands unedited.
        raise exc
    hint = MCP_EXTRA_HINT if exc.name == "mcp" else MCP_INCOMPATIBLE_HINT
    raise ImportError(hint) from exc


def _load_fastmcp() -> type[FastMCP]:
    """Import `FastMCP` on demand, or explain how to install it.

    Deferred to call time rather than module scope so that importing
    `ontary.mcp_server` (and therefore running the `ontary-mcp` entrypoint, or
    reading `MCP_EXTRA_HINT`) works without the extra and can produce a useful
    message instead of a traceback from the import system.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:
        _raise_mcp_import_error(exc)
    return FastMCP


def _load_tool_annotations() -> type[ToolAnnotations]:
    """Import MCP's annotation model only when the optional extra is used."""
    try:
        from mcp.types import ToolAnnotations
    except ModuleNotFoundError as exc:
        _raise_mcp_import_error(exc)
    return ToolAnnotations


def _load_get_access_token() -> Callable[[], "AccessToken | None"]:
    """Import `get_access_token` on demand, exactly like `_load_fastmcp`
    above defers `FastMCP` -- so importing `ontary.mcp_server` (and
    therefore `ontary`, which imports `build_multi_consumer_mcp_server`
    unconditionally) never needs the `mcp` extra just to define this
    module. Called once, from `build_multi_consumer_mcp_server` itself --
    and called BEFORE that function's own `_load_fastmcp()` call, so if the
    `mcp` extra is missing THIS loader is the one that raises first and
    surfaces `MCP_EXTRA_HINT`, not `_load_fastmcp`. It stays a separate
    deferred loader (rather than a plain module-scope import) purely so the
    invariant above -- module import never requires the extra -- holds
    structurally, regardless of which loader happens to run first.
    """
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except ModuleNotFoundError as exc:
        _raise_mcp_import_error(exc)
    return get_access_token


# Every domain exception this module knows how to turn into a structured
# error payload naming the *actual* failure -- `OntaryError` covers the
# WHOLE engine exception family (visibility/permission/precondition/
# validation/authority/conflict AND internal -- `_run` below still routes
# `kind == "internal"` to the generic envelope even though it's caught
# here), plus two bare-Python unknown-name/argument types this module
# catches defensively for any registry lookup or argument check that
# raises one directly rather than through an `OntaryError` wrapper. Typed
# lookups now raise `ValidationFailed` with code `UNKNOWN_NAME`, so they no
# longer rely on multiple inheritance from `KeyError`; a future unwrapped
# lookup elsewhere in the client would still land here instead of the generic
# internal-error fallback. Anything else is
# caught by a final generic handler that never echoes the underlying
# exception's message.
_KNOWN_ERRORS: tuple[type[Exception], ...] = (
    OntaryError,
    KeyError,
    ValueError,
)

_KIND_NAMES: dict[str, str] = {
    "visibility": "VisibilityError",
    "permission": "PermissionDenied",
    "precondition": "PreconditionFailed",
    "validation": "ValidationFailed",
    "authority": "AuthorityError",
    "conflict": "ConflictError",
    "internal": "InternalError",
}


def _code_and_kind(exc: Exception) -> tuple[str, str]:
    """Classify `exc` into a stable `(code, kind)` pair (spec AC8): every
    engine exception is already an `OntaryError` carrying its own `.code`/
    `.kind`; the two bare-Python exception types `_KNOWN_ERRORS` also
    catches defensively (`KeyError`/`ValueError`, for any unwrapped
    registry lookup or client-side argument check that doesn't already
    raise an `OntaryError`) get a stable code of their own so an MCP caller
    never has to distinguish them by message text either."""
    if isinstance(exc, OntaryError):
        return exc.code, exc.kind
    if isinstance(exc, KeyError):
        return "UNKNOWN_NAME", "validation"
    return "INVALID_PARAMS", "validation"  # ValueError


def _error(exc: Exception) -> dict[str, Any]:
    code, kind = _code_and_kind(exc)
    return {
        "error": {
            "type": _KIND_NAMES[kind] if isinstance(exc, OntaryError) else type(exc).__name__,
            "message": str(exc),
            "code": code,
            "kind": kind,
        }
    }


def _generic_error() -> dict[str, Any]:
    return {
        "error": {
            "type": "InternalError",
            "message": "internal server error",
            "code": "INTERNAL_ERROR",
            "kind": "internal",
        }
    }


# `SkipValidation` preserves the public primitive JSON schemas while ensuring
# FastMCP passes supplied values into the tool body. Required parameters are
# nullable only at this framework boundary so their `None` defaults also move
# missing-argument validation under `_try`; the body still requires the value.
# All argument failures therefore use the same structured envelope as every
# domain refusal instead of escaping as framework/Pydantic text.
_ToolString = Annotated[str | None, SkipValidation()]
_ToolObject = Annotated[dict[str, Any] | None, SkipValidation()]


def _missing_tool_param(name: str) -> ValueError:
    return ValueError(f"missing required tool parameter {name!r}")


def _invalid_tool_param(name: str, expected: str, value: Any) -> ValueError:
    return ValueError(
        f"tool parameter {name!r} must be {expected}, got {type(value).__name__}"
    )


def _tool_string(name: str, value: Any) -> str:
    if value is None:
        raise _missing_tool_param(name)
    if not isinstance(value, str):
        raise _invalid_tool_param(name, "a string", value)
    return value


def _tool_object(name: str, value: Any) -> dict[str, Any]:
    if value is None:
        raise _missing_tool_param(name)
    if not isinstance(value, dict):
        raise _invalid_tool_param(name, "an object", value)
    return value


def _tool_optional_object(name: str, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return _tool_object(name, value)


def _tool_optional_int(name: str, value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise _invalid_tool_param(name, "an integer or null", value)
    return value


def _tool_optional_string(name: str, value: Any) -> str | None:
    if value is None:
        return None
    return _tool_string(name, value)


def _serialize(obj: StoredObject | None) -> dict[str, Any] | None:
    """Serialize a `StoredObject` to its MCP wire shape: `{"payload": ...,
    "lineage": ...}` (spec m35-sdk-refactor §6 AC8) -- `model_dump(mode=
    "json")` so both halves round-trip as plain JSON-safe values, with no
    magic `_`-prefixed keys smuggled into `payload`."""
    return None if obj is None else obj.model_dump(mode="json")


def _try(fn: Callable[[], Any]) -> tuple[Any, dict[str, Any] | None]:
    """Run `fn`, returning `(result, None)` on success or `(None, envelope)`
    on a caught failure -- the classification shared by `_run` (nests a
    success under `"result"`) and `_run_unwrapped` (returns it at the top
    level, for the introspection tools -- spec `multi-consumer-mcp.md`
    §4.3). Extracted so both callers, and any future caller that resolves
    a client/identity before running the tool body, get the SAME failure
    handling rather than a second hand-written copy of it.
    """
    try:
        result = fn()
    except _KNOWN_ERRORS as exc:
        # An `OntaryError` whose `kind` is "internal"
        # own fallback, or any future engine exception left unclassified)
        # is a `_KNOWN_ERRORS` match but must NOT be described any further
        # than the generic envelope -- "internal" is the one kind that
        # promises nothing about what's safe to say to a consumer.
        try:
            if isinstance(exc, OntaryError) and exc.kind == "internal":
                return None, _generic_error()
            return None, _error(exc)
        except Exception:
            # Building the envelope must never be what escapes. `_error` is
            # called from inside this `except`, so anything it raises would
            # bypass the sibling handler below and leave the caller with no
            # envelope at all -- e.g. a consumer subclass whose `__init__`
            # forwards only the message and so carries no `.code`. The
            # module promises "never a partial"; fail closed to the generic
            # envelope instead.
            return None, _generic_error()
    except Exception:
        return None, _generic_error()
    try:
        # The MCP framework serializes after the tool returns, outside this
        # envelope boundary. Normalize here so unsupported objects and
        # non-finite floats become INTERNAL_ERROR, even though json.dumps
        # uses ValueError for the latter (a tool-body ValueError remains the
        # INVALID_PARAMS branch above).
        result = json.loads(json.dumps(result, allow_nan=False))
    except Exception:
        return None, _generic_error()
    return result, None


def _run(fn: Callable[[], Any]) -> dict[str, Any]:
    """Run `fn`, wrapping its result/failure into a structured payload.

    Every successful result -- `dict` or not -- is wrapped as
    `{"result": ...}` and every failure as `{"error": {...}}` so the two
    cases are mutually-exclusive top-level keys. Returning `dict` results
    as-is (unwrapped) would let a success payload collide with the error
    envelope whenever an ontology happened to have a property/field named
    "error", making the envelope's discriminator ambiguous -- so success is
    *always* nested under "result", never merged into the top level.
    """
    result, error = _try(fn)
    return error if error is not None else {"result": result}


def _run_unwrapped(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Like `_run`, but returns `fn`'s dict result AT THE TOP LEVEL instead
    of nesting it under `"result"` -- used only by the four introspection
    tools. `_run`'s "always nest under result" rule exists to prevent an
    *ontology-controlled* key from colliding with `"error"`; introspection's
    top-level keys (`object_types`, `link_types`, ...) are fixed by this
    module, not by any ontology, so there is nothing for them to collide
    with (spec `multi-consumer-mcp.md` §4.3). Failure classification is
    identical to `_run` -- both share `_try`.
    """
    result, error = _try(fn)
    return error if error is not None else result


def _run_paged(
    fn: Callable[[], tuple[list[dict[str, Any] | None], str | None]]
) -> dict[str, Any]:
    """Run a paged read while preserving the existing row result shape.

    `query_objects` historically returned its rows under the top-level
    `result` key. Pagination adds `next_cursor` alongside that list so an
    existing caller can keep consuming the rows without unwrapping a new
    page object.
    """
    result, error = _try(fn)
    if error is not None:
        return error
    rows, next_cursor = result
    return {"result": rows, "next_cursor": next_cursor}


def _query_objects_page(
    resolve_client: Callable[[], OntologyClient],
    obj_type: Any,
    where: Any,
    limit: Any,
    after: Any,
) -> tuple[list[dict[str, Any] | None], str | None]:
    """Apply the MCP cap and delegate one query page to the client surface."""
    client = resolve_client()
    obj_type = _tool_string("obj_type", obj_type)
    where = _tool_optional_object("where", where)
    limit = _tool_optional_int("limit", limit)
    after = _tool_optional_string("after", after)
    if limit is None and after is not None:
        # Delegate this refusal to the underlying client so its existing
        # AFTER_WITHOUT_LIMIT code/message remains the only validation for
        # this branch. Current client/query code raises before reading any
        # rows; this fallback is defensive if that contract ever regresses.
        client.list(obj_type, where, after=after)
        raise RuntimeError("the underlying paged read accepted after without limit")
    if limit is not None and limit > MAX_LIMIT:
        raise ValidationFailed(
            f"get_objects limit must be <= {MAX_LIMIT}, got {limit!r}",
            code="INVALID_LIMIT",
        )
    page = client.list(
        obj_type,
        where,
        limit=DEFAULT_LIMIT if limit is None else limit,
        after=after,
    )
    return ([_serialize(obj) for obj in page.items], page.next_cursor)


def _object_type_payload(defn: ObjectTypeDef) -> dict[str, Any]:
    return {
        "api_name": defn.api_name,
        "display_name": defn.display_name,
        "description": defn.description,
        "layer": defn.layer,
        "primary_key": defn.primary_key,
        "properties": [
            {
                "name": p.name,
                "type": p.type,
                "required": p.required,
                "ai_usable": p.sensitivity.ai_usable,
                "human_visible": p.sensitivity.human_visible,
                "scope_level": p.scope_level,
            }
            for p in defn.properties
        ],
    }


def _link_type_payload(defn: LinkTypeDef) -> dict[str, Any]:
    return {
        "api_name": defn.api_name,
        "from_type": defn.from_type,
        "to_type": defn.to_type,
        "cardinality": defn.cardinality.value,
        "description": defn.description,
        "identity_revealing": defn.identity_revealing,
    }


def _action_type_payload(defn: ActionTypeDef) -> dict[str, Any]:
    return {
        "api_name": defn.api_name,
        "display_name": defn.display_name,
        "target_type": defn.target_type,
        "executable_by_roles": list(defn.executable_by_roles),
        "description": defn.description,
        "capabilities": list(defn.capabilities),
        "effects": list(defn.effects),
        "parameters": [
            {
                "name": p.name,
                "type": p.type,
                "required": p.required,
                "refers_to": p.refers_to,
                "scope_semantics": p.scope_semantics,
            }
            for p in defn.parameters
        ],
    }


def _function_payload(defn: FunctionDef) -> dict[str, Any]:
    return {
        "api_name": defn.api_name,
        "description": defn.description,
        "input_description": defn.input_description,
        "output_description": defn.output_description,
        "capabilities": list(defn.capabilities),
    }


def _register_tools(
    server: FastMCP,
    ontology: OntologyDef,
    resolve_client: Callable[[], OntologyClient],
) -> None:
    """Register all ten tools onto `server`, ONE tool body each, shared by
    both `build_mcp_server` and `build_multi_consumer_mcp_server` (spec
    `multi-consumer-mcp.md` §4.3: "one tool body, two servers").

    Every tool obtains its `OntologyClient` by CALLING `resolve_client()`
    at invocation time rather than closing over an already-built client --
    that is the whole seam. `build_mcp_server` below passes a resolver
    that always returns the same constant client and never raises, which
    is why its behavior is unchanged by this refactor.
    `build_multi_consumer_mcp_server` passes a resolver that reads the
    per-request identity and CAN raise (`PermissionDenied` with code
    `UNAUTHENTICATED` or `CONSUMER_UNRESOLVED`, or the caller's own resolver's
    exception) -- that
    raise is classified below, the same way, without this function knowing
    or caring which builder it came from.

    The four introspection tools (`list_object_types`/`list_link_types`/
    `list_action_types`/`list_functions`) still read only `ontology.
    registry` for their payload -- that part is schema, not consumer data,
    and untouched by who's asking. They call `resolve_client()` too, so
    that identity is proven -- and a raising resolver refused -- before
    ANY tool answers, introspection included (spec §3 AC4). A raising
    resolver is classified by `_try` into the identical structured
    envelope an `OntaryError` already produces (see `_run_unwrapped`/`_run`),
    so introspection fails exactly like every other tool rather than
    growing its own error path.
    """

    tool_annotations = _load_tool_annotations()
    read_only = tool_annotations(readOnlyHint=True)
    destructive = tool_annotations(destructiveHint=True)

    @server.tool(annotations=read_only)
    def list_object_types() -> dict[str, Any]:
        """List every declared object type (introspection, not consumer data)."""

        def _list() -> dict[str, Any]:
            resolve_client()
            return {
                "object_types": [
                    _object_type_payload(d)
                    for d in ontology.registry.object_types.values()
                ]
            }

        return _run_unwrapped(_list)

    @server.tool(annotations=read_only)
    def list_link_types() -> dict[str, Any]:
        """List every declared link type (introspection, not consumer data)."""

        def _list() -> dict[str, Any]:
            resolve_client()
            return {
                "link_types": [
                    _link_type_payload(d) for d in ontology.registry.link_types.values()
                ]
            }

        return _run_unwrapped(_list)

    @server.tool(annotations=read_only)
    def list_action_types() -> dict[str, Any]:
        """List every declared action type (introspection, not consumer data)."""

        def _list() -> dict[str, Any]:
            resolve_client()
            return {
                "action_types": [
                    _action_type_payload(d)
                    for d in ontology.registry.action_types.values()
                ]
            }

        return _run_unwrapped(_list)

    @server.tool(annotations=read_only)
    def list_functions() -> dict[str, Any]:
        """List every declared function (introspection, not consumer data)."""

        def _list() -> dict[str, Any]:
            resolve_client()
            return {
                "functions": [
                    _function_payload(d) for d in ontology.registry.functions.values()
                ]
            }

        return _run_unwrapped(_list)

    @server.tool(annotations=read_only)
    def get_declarations() -> dict[str, Any]:
        """This runtime's declared contracts (spec AC10): authority model,
        write-back/re-ingest/visibility/transaction-ownership/idempotency
        stance, audit scope, and this ontology's `min_n` -- identical to
        `client.declarations`."""
        return _run(lambda: resolve_client().declarations.model_dump())

    @server.tool(annotations=read_only)
    def get_object(
        obj_type: _ToolString = None, obj_id: _ToolString = None
    ) -> dict[str, Any]:
        """Fetch one object by type + id, through the guarded read layer.

        `client.get` returns a `StoredObject` (payload/lineage split, spec
        m35-sdk-refactor §6 AC8); the wire result is that model's
        `model_dump(mode="json")` -- `{"payload": {...}, "lineage": {...}}`
        -- so a caller sees the object's own properties and its storage/
        provenance metadata (object_type, object_id, valid_from/to,
        source_system, source_id, extracted_at) as two clearly separated,
        typed halves rather than lineage fields mixed into the payload
        under magic `_`-prefixed keys. `None` if the object doesn't exist.
        """
        return _run(
            lambda: _serialize(
                resolve_client().get(
                    _tool_string("obj_type", obj_type),
                    _tool_string("obj_id", obj_id),
                )
            )
        )

    @server.tool(annotations=read_only)
    def query_objects(
        obj_type: _ToolString = None,
        where: Annotated[
            dict[str, Any] | None,
            SkipValidation(),
            Field(
                description=(
                    "Equality-only matching on payload fields; unknown keys "
                    "raise UNKNOWN_FIELD."
                )
            ),
        ] = None,
        limit: Annotated[
            int | None,
            SkipValidation(),
            Field(
                description=(
                    "Page size; defaults to 100 and has a hard maximum of 1000."
                )
            ),
        ] = None,
        after: Annotated[
            str | None,
            SkipValidation(),
            Field(
                description=(
                    "Opaque next_cursor from the previous page; requires an "
                    "explicit limit."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Query/filter objects of a type, through the guarded read layer.

        The `where` grammar is equality-only matching on payload fields;
        unknown keys raise `UNKNOWN_FIELD`. Lineage fields are not filterable.
        `limit` defaults to 100 and has a hard maximum of 1000. `after` is the
        opaque cursor from the previous page and requires an explicit
        `limit`; it is passed to the underlying paged read. The successful
        response keeps rows under `result` and adds `next_cursor`, which is
        the cursor for the next page or `None` when exhausted. A limit below
        one and `after` without `limit` use the underlying
        `INVALID_LIMIT`/`AFTER_WITHOUT_LIMIT` validations.

        Each result is a `{"payload": {...}, "lineage": {...}}` object --
        see `get_object`'s docstring for the shape."""
        return _run_paged(
            lambda: _query_objects_page(
                resolve_client,
                obj_type,
                where,
                limit,
                after,
            )
        )

    @server.tool(annotations=read_only)
    def traverse_links(
        obj_type: _ToolString = None,
        obj_id: _ToolString = None,
        link_api_name: _ToolString = None,
    ) -> dict[str, Any]:
        """Traverse a declared link from an object, through the guarded read
        layer. The underlying `OntologyClient.traverse`/`GuardedQuery.traverse`
        surface is an unpaged list and exposes no `limit`/`after` cursor, so
        this tool intentionally has no pagination parameters in this release.

        Each result is a `{"payload": {...}, "lineage": {...}}` object --
        see `get_object`'s docstring for the shape."""
        return _run(
            lambda: [
                _serialize(obj)
                for obj in resolve_client().traverse(
                    _tool_string("obj_type", obj_type),
                    _tool_string("link_api_name", link_api_name),
                    _tool_string("obj_id", obj_id),
                )
            ]
        )

    @server.tool(annotations=destructive)
    def execute_action(
        api_name: _ToolString = None, params: _ToolObject = None
    ) -> dict[str, Any]:
        """Execute a governed action (role/scope/precondition/audit pipeline)."""
        return _run(
            lambda: resolve_client().execute(
                _tool_string("api_name", api_name), _tool_object("params", params)
            )
        )

    @server.tool(annotations=read_only)
    def call_function(
        api_name: _ToolString = None, params: _ToolObject = None
    ) -> dict[str, Any]:
        """Call a derived Function through the guarded read layer."""
        return _run(
            lambda: resolve_client().call_function(
                _tool_string("api_name", api_name), _tool_object("params", params)
            )
        )


def build_mcp_server(
    ontology: OntologyDef | Ontology,
    store: Store,
    consumer: Consumer,
    *,
    name: str | None = None,
    capabilities: Mapping[CapabilityHandle[Any], object] | None = None,
    effects: Mapping[EffectHandle[Any], EffectDispatcher] | None = None,
) -> FastMCP:
    """Build a `FastMCP` server bound to exactly one `(ontology, store,
    consumer)` triple -- one server process, one Consumer identity (spec
    §5).

    `ontology` accepts either a plain `OntologyDef` (descriptor authoring)
    or an `Ontology` (class authoring, spec `typed-actions.md` §6) -- same
    as `OntologyClient`'s own constructor, which this function delegates
    to internally. Handlers arrive pre-bound: an `Ontology`'s
    `@ontology.action(...)`/`@ontology.function(...)`-declared handlers
    auto-bind to the internal `OntologyClient` this server builds (spec
    AC7 removed the old `register_handlers` callback parameter -- there is
    no per-server registration step left to perform here). A descriptor-
    authored `OntologyDef` passed here has no declared handlers to
    auto-bind, so its actions are unregistered (`UNKNOWN_ACTION`) unless
    it is migrated to class authoring -- see `ontary.client`'s docstring.

    Delegates tool registration to `_register_tools` with a constant
    `resolve_client` that always returns the one client built here and
    never raises -- so this function's own behavior is unchanged by the
    shared-registration refactor (spec `multi-consumer-mcp.md` AC12).
    """
    client = OntologyClient(
        ontology,
        store,
        consumer,
        capabilities=capabilities,
        effects=effects,
    )

    definition = resolve_definition(ontology)
    server = _load_fastmcp()(name or definition.name)
    _register_tools(server, definition, lambda: client)
    return server


def build_multi_consumer_mcp_server(
    ontology: OntologyDef | Ontology,
    store: Store,
    *,
    resolve_consumer: ConsumerResolver,
    name: str | None = None,
    capabilities: Mapping[CapabilityHandle[Any], object] | None = None,
    effects: Mapping[EffectHandle[Any], EffectDispatcher] | None = None,
    token_verifier: TokenVerifier | None = None,
    auth: AuthSettings | None = None,
) -> FastMCP:
    """Build a `FastMCP` server serving MANY proven identities over one
    `(ontology, store)` pair -- no `consumer` parameter (spec
    `multi-consumer-mcp.md` §3 AC1). Each tool invocation resolves its own
    `Consumer` from that request's verified MCP `AccessToken`, via the
    caller-supplied `resolve_consumer`.

    `token_verifier` and `auth` are forwarded VERBATIM to the underlying
    `FastMCP(...)` call below -- this is the only place a deployer can wire
    them, because `FastMCP` has no public setter for either afterwards (its
    token verifier lives on a private, underscore-prefixed field, and its
    auth pipeline -- `RequireAuthMiddleware`, the bearer-auth backend, the
    `/.well-known/oauth-protected-resource` metadata route -- is built once,
    inside `streamable_http_app()`/`sse_app()`, gated on both being present
    at that call). Passing neither is a legitimate stdio-only or
    intentionally-open deployment; passing exactly one of `token_verifier`/
    `auth` is not a fail-closed runtime state at all -- `FastMCP.__init__`
    itself raises `ValueError` (`"Cannot specify auth_server_provider or
    token_verifier without auth settings"` / `"Must specify either
    auth_server_provider or token_verifier when auth is enabled"`), so
    construction never completes and no call is ever made; this is a
    fail-fast at construction, not the fail-closed-per-call sequence
    below, which only ever runs once both are present (or both absent). A
    deployer who instead builds their OWN Starlette app
    around `server.streamable_http_app()` and sets `token_verifier`/`auth`
    that way gets the raw ASGI transport's auth check but loses
    `RequireAuthMiddleware` and the protected-resource metadata route that
    `streamable_http_app()` wires in only when both fields are already set
    at that call -- passing them here, through the constructor, is what
    keeps that whole pipeline intact.

    **One runtime, many views (AC7).** Exactly one `OntologyRuntime` -- one
    `GuardedQuery`, one `ActionExecutor`, every ontology-declared handler
    bound once -- is built here, at construction, same as `build_mcp_server`
    builds one `OntologyClient` once. Every call goes through `runtime.
    for_consumer(...)`, a cheap view sharing that SAME `query`/`actions` by
    identity; two different identities' calls therefore see `client.actions`
    as the same object, not two independent copies of a role/scope guard.

    **Resolution is never cached (AC2).** The `resolve_client` seam
    `_register_tools` calls PER INVOCATION does the full read-token/
    resolve/stamp/`for_consumer` sequence below every single time -- there
    is no memoization keyed on the token, so a revoked or re-scoped token
    cannot be served from a stale binding, and two interleaved calls from
    different identities on this one server object never share any mutable
    per-call state (AC3).

    **Fail-closed sequence, per invocation, ALL ten tools (AC4/AC13):**

    1. No verified `AccessToken` on the request (`get_access_token()`
       returns `None` -- true for stdio, or HTTP with no `token_verifier`
       configured) -> `PermissionDenied` with code `UNAUTHENTICATED`. This is
       the FIRST thing a
       misconfigured deployment hits, so its message names the fix (spec
       §3's golden sample).
    2. `resolve_consumer(token)` returns `None` -> `PermissionDenied` with code
       `CONSUMER_UNRESOLVED` -- a verified principal exists, but nothing maps
       it to a `Consumer`.
       Deliberately a different code from (1): the fix differs (configure
       authentication vs. map this principal).
    3. `resolve_consumer(token)` raises an `OntaryError` -> re-raised as-is;
       `_try`/`_run` classify it exactly like an `OntaryError` raised from
       inside a tool body.
    4. `resolve_consumer(token)` raises anything else -> re-raised as a
       plain `RuntimeError` carrying NO part of the original message (AC6):
       `_try`'s classification keys off exception TYPE, and `KeyError`/
       `ValueError` are two of the types it already echoes verbatim for
       tool-body failures (`UNKNOWN_NAME`/`INVALID_PARAMS`) -- a resolver
       that happens to raise either of those (e.g. a `dict` lookup against
       an unmapped token) must NOT be echoed, since its message may embed
       token material, so it is deliberately re-typed to a type `_try`
       has no special case for and therefore routes to the generic
       `INTERNAL_ERROR` envelope.

    **The principal cannot be forged by author code (AC9).** After
    `resolve_consumer` returns a `Consumer`, THIS function -- not the
    resolver -- overwrites `principal` from the verified token:
    `token.subject or token.client_id` (`client_id` is non-optional on
    `AccessToken`, so an authenticated call always ends up with a
    `principal`). A resolver that sets `Consumer.principal` itself has that
    value discarded, never read.

    The import of `get_access_token` is deferred to call time via
    `_load_get_access_token`, exactly like `_load_fastmcp` above defers
    `FastMCP` -- see that function's docstring.
    """
    definition = resolve_definition(ontology)
    runtime = OntologyRuntime(
        ontology,
        store,
        capabilities=capabilities,
        effects=effects,
    )
    get_access_token = _load_get_access_token()

    def resolve_client() -> OntologyClient:
        token = get_access_token()
        if token is None:
            raise PermissionDenied(
                "no authenticated principal on this request -- a "
                "multi-consumer MCP server requires a verified access "
                "token. Two likely causes: (1) this process is running "
                "bare server.run(), which defaults to stdio and carries "
                "no auth context at all -- run "
                "server.run(transport='streamable-http') instead, or use "
                "build_mcp_server(ontology, store, consumer) for a "
                "single-consumer stdio process; (2) it's already "
                "streamable HTTP but token_verifier= and auth= were not "
                "passed to build_multi_consumer_mcp_server(...)",
                code="UNAUTHENTICATED",
            )
        try:
            consumer = resolve_consumer(token)
        except OntaryError:
            # A resolver's own deliberate refusal (spec §6 "resolver
            # raises a deliberate OntaryError") -- surfaced unchanged so
            # `_try` classifies it exactly like any other `OntaryError`.
            raise
        except Exception as exc:
            # ANY other exception -- including a bare `KeyError`/
            # `ValueError` `_try` would otherwise echo verbatim for a
            # tool-body failure -- is re-typed to a plain `RuntimeError`
            # with a message that names nothing about the original
            # failure, so `_try`'s generic (message-free) `INTERNAL_ERROR`
            # branch is the only one that can possibly match (AC6: the
            # resolver's message may embed token material and must never
            # be echoed).
            raise RuntimeError(
                "resolve_consumer raised while resolving this request's "
                "identity"
            ) from exc
        if consumer is None:
            raise PermissionDenied(
                "resolve_consumer returned no Consumer for this request's "
                "verified access token -- the fix is: map this principal "
                "in your deployment's resolve_consumer callback",
                code="CONSUMER_UNRESOLVED",
            )
        principal = token.subject or token.client_id
        stamped = consumer.model_copy(update={"principal": principal})
        return runtime.for_consumer(stamped)

    # `stateless_http=True` -- NOT a knob, hardcoded. FastMCP's DEFAULT
    # stateful streamable HTTP starts one long-lived session TASK on the
    # `initialize` request and reuses it for every later request bearing
    # that session's `Mcp-Session-Id`; a tool body invoked on request #2
    # therefore runs *inside request #1's task*, which copied the auth
    # contextvar once, at task-start time. `get_access_token()` inside
    # `resolve_client` above would then return request #1's `AccessToken`
    # for the entire session's life, no matter which request's bearer
    # token actually reached the transport -- silently falsifying "never
    # cached" (spec AC2) for a revoked or re-scoped token. `stateless_http
    # =True` makes `StreamableHTTPSessionManager` build a brand-new
    # transport AND a brand-new session task for every single request, so
    # there is no persistent task left to freeze a token into; the cost is
    # real (no session resumability / no replay of missed events -- `mcp`'s
    # own stateless handler still honors `json_response`, so SSE responses
    # are NOT disabled by this flag; see the README's multi-consumer
    # section) and accepted deliberately, over weakening AC2's claim
    # instead (whole-branch `/review` P0, `multi-consumer-mcp.md` §4,
    # pinned at the ASGI level by `tests/test_mcp_multi_consumer
    # .py::test_stateless_http_serves_each_request_its_own_token`).
    # `build_mcp_server` above is NOT touched: it is single-consumer/
    # stdio, has no session concept to freeze a token into in the first
    # place (one `Consumer` for the process's whole life, always), and is
    # explicitly out of scope for this fix.
    server = _load_fastmcp()(
        name or definition.name,
        stateless_http=True,
        token_verifier=token_verifier,
        auth=auth,
    )
    _register_tools(server, definition, resolve_client)
    return server


def main() -> None:
    """Optional stdio entrypoint. An ontology author wires this up in their
    own `__main__`-style script by importing `build_mcp_server` and calling
    `.run()` themselves; this bare `main()` exists only so
    `python -m ontary.mcp_server` (and the `ontary-mcp` console script)
    doesn't dead-end for a quick manual check -- it has no ontology to bind
    to, so it just documents the wiring rather than doing anything.

    It reports a MISSING EXTRA first, though. `ontary-mcp` is installed by
    the core package, so it is a plausible first thing to type after
    `pip install ontary`, and being told to write an entrypoint script
    would be misleading advice for someone who cannot import `mcp` yet.
    """
    try:
        _load_fastmcp()
    except ImportError as exc:
        raise SystemExit(str(exc)) from exc
    raise SystemExit(
        "ontary.mcp_server has no default ontology to serve; call "
        "build_mcp_server(ontology, store, consumer).run() from your own "
        "entrypoint script."
    )


if __name__ == "__main__":  # pragma: no cover
    main()
