"""Generic MCP server: exposes any `OntologyDef` to an MCP client (e.g. a
Claude agent). Two builders share one implementation:

- `build_mcp_server(ontology, store, consumer)` -- one server process bound
  to exactly one `Consumer`, for a single-consumer
  stdio deployment. Unchanged, byte-identically, by everything below.
- `build_multi_consumer_mcp_server(ontology, store, resolve_consumer=...)`
  -- one server process serving MANY proven identities: each tool
  invocation resolves its own `Consumer` from that request's verified MCP
  `AccessToken`.

Every tool here delegates to a single `OntologyClient` built from the
`(ontology, store, consumer)` triple this module is handed -- there is no
raw store/registry *data* read anywhere in this file (introspecting type
*definitions* off `ontology.registry` is fine and expected: those are
schema, not consumer data, and are identical for every consumer). This
mirrors `OntologyClient`'s own guarantee (see `ontary.client`'s docstring)
one layer further out.

All fourteen tool bodies are registered exactly once, by `_register_tools`,
against a `resolve_client: Callable[[], OntologyClient]` seam instead of
each tool closing directly over an
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

Deliberately NOT exposed: `client.ingest`/`client.ingest_links`. The MCP
surface is scoped to introspection + query/traverse/execute/call --
bulk data-loading is an unguarded, lineage-stamped bulk-upsert path,
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
result, never a raw traceback (the `code` is the same
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
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, NamedTuple, cast

from pydantic import Field, SkipValidation

from ontary.audit import AuditEntry, EmittedEvent
from ontary.authoring import CapabilityHandle
from ontary.client import OntologyClient, OntologyRuntime
from ontary.errors import OntaryError, PermissionDenied, ValidationFailed
from ontary.meta import (
    ActionParameterDef,
    ActionTypeDef,
    EventTypeDef,
    FunctionDef,
    LinkTypeDef,
    ObjectTypeDef,
    PropertyDef,
)
from ontary.ontology import OntologyDef, SupportsDefinition, resolve_definition
from ontary.query import AggregateFunc, AggregateValue, Page
from ontary.security import Consumer
from ontary.store import Store, StoredObject

if TYPE_CHECKING:
    from mcp.server.auth.provider import AccessToken, TokenVerifier
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations


# `Callable[["AccessToken"], Consumer | None]`, quoted: `AccessToken` only
# exists under `TYPE_CHECKING` above (see `_load_get_access_token`'s
# docstring for why it must stay out of the runtime import graph), so a
# real (unquoted) reference here would `NameError` the moment this module
# is imported without the `mcp` extra -- exactly the failure `_load_mcp_server`
# already exists to avoid for `MCPServer` itself.
ConsumerResolver = Callable[["AccessToken"], "Consumer | None"]
"""Maps one request's verified `AccessToken` to the `Consumer` it should act
as, or `None` if nothing maps it. Must be
SYNCHRONOUS -- the fourteen tool handlers are `async def`, so the resolver is
called on the event loop inside the request's task. It must therefore be a
plain synchronous callable that does not block (no network round-trips that
stall the loop -- a slow resolver stalls every in-flight request), and an
`async` resolver is not accepted because the tool bodies call it
synchronously; see `build_multi_consumer_mcp_server`'s docstring.

If this callback raises a deliberate `OntaryError` (rather than returning
`None`), that error's `.message` IS echoed verbatim in the tool's error
envelope -- unlike any OTHER exception type, which is re-typed to a
message-free `RuntimeError` before it can reach a caller. A resolver
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
empty venv, which nothing in CI had ever done.

Deliberately an `ImportError`, not an `OntaryError`. `ERROR_CODES` catalogues
refusals a CONSUMER can receive from a running runtime -- over MCP, that means
the structured error envelope. A missing optional dependency is an environment
problem that happens BEFORE any server exists to answer, and `ImportError` is
what a caller guarding an optional integration already catches.
"""

MCP_INCOMPATIBLE_HINT = (
    "the installed `mcp` version is unsupported by ontary -- install the "
    "`mcp>=2.1.1,<3` range with `pip install 'ontary[mcp]'` "
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


def _load_mcp_server() -> type[MCPServer]:
    """Import `MCPServer` on demand, or explain how to install it.

    Deferred to call time rather than module scope so that importing
    `ontary.mcp_server` (and therefore running the `ontary-mcp` entrypoint, or
    reading `MCP_EXTRA_HINT`) works without the extra and can produce a useful
    message instead of a traceback from the import system.
    """
    try:
        from mcp.server.mcpserver import MCPServer
    except ModuleNotFoundError as exc:
        _raise_mcp_import_error(exc)
    return MCPServer


def _load_tool_annotations() -> type[ToolAnnotations]:
    """Import MCP's annotation model only when the optional extra is used."""
    try:
        from mcp.types import ToolAnnotations
    except ModuleNotFoundError as exc:
        _raise_mcp_import_error(exc)
    return ToolAnnotations


def _load_get_access_token() -> Callable[[], "AccessToken | None"]:
    """Import `get_access_token` on demand, exactly like `_load_mcp_server`
    above defers `MCPServer` -- so importing `ontary.mcp_server` (and
    therefore `ontary`, which imports `build_multi_consumer_mcp_server`
    unconditionally) never needs the `mcp` extra just to define this
    module. Called once, from `build_multi_consumer_mcp_server` itself --
    and called BEFORE that function's own `_load_mcp_server()` call, so if the
    `mcp` extra is missing THIS loader is the one that raises first and
    surfaces `MCP_EXTRA_HINT`, not `_load_mcp_server`. It stays a separate
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
    """Classify `exc` into a stable `(code, kind)` pair: every
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
# MCPServer passes supplied values into the tool body. Parameters whose missing
# value must be handled under `_try` use nullable annotations only at this
# framework boundary; their `None` defaults still reach the body, which then
# decides whether the value is required or an accepted optional. `reverse`
# keeps a non-nullable boolean annotation and a `False` default in the public
# schema, while its body validator still sees explicit raw JSON values.
# All argument failures therefore use the same structured envelope as every
# domain refusal instead of escaping as framework/Pydantic text.
_ToolString = Annotated[str | None, SkipValidation()]
_ToolObject = Annotated[dict[str, Any] | None, SkipValidation()]
_ToolBool = Annotated[bool, SkipValidation()]


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


def _tool_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise _invalid_tool_param(name, "a boolean", value)
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


def _serialize(obj: StoredObject | None, redacted: tuple[str, ...]) -> dict[str, Any] | None:
    """Serialize an object as `{"payload", "lineage", "redacted_fields"}`.

    `model_dump(mode="json")` keeps payload and lineage JSON-safe; the
    declaration-only redaction names sit alongside them. `None` stays `None`.
    """
    return None if obj is None else {
        **obj.model_dump(mode="json"), "redacted_fields": list(redacted),
    }


def _try(fn: Callable[[], Any]) -> tuple[Any, dict[str, Any] | None]:
    """Run `fn`, returning `(result, None)` on success or `(None, envelope)`
    on a caught failure -- the classification shared by `_run` (nests a
    success under `"result"`) and `_run_unwrapped` (returns it at the top
    level, for the introspection tools). Extracted so both callers, and
    any future caller that resolves
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
        # envelope boundary. The engine's RESULT_NOT_JSON check (#167) runs
        # first, inside the handler call, so Function/Action results reach
        # here already encoded. This json.dumps round trip is the generic
        # fallback for values that bypass it: unsupported objects and
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
    of nesting it under `"result"` -- used only by the five introspection
    tools. `_run`'s "always nest under result" rule exists to prevent an
    *ontology-controlled* key from colliding with `"error"`; introspection's
    top-level keys (`object_types`, `link_types`, ...) are fixed by this
    module, not by any ontology, so there is nothing for them to collide
    with. Failure classification is
    identical to `_run` -- both share `_try`.
    """
    result, error = _try(fn)
    return error if error is not None else result


def _run_marked(fn: Callable[[], tuple[Any, bool]]) -> dict[str, Any]:
    """Wrap a successful read with its declaration-only `scope_limited` mark."""
    result, error = _try(fn)
    if error is not None:
        return error
    value, scope_limited = result
    return {"result": value, "scope_limited": scope_limited}


class _PagedRead(NamedTuple):
    """One successful list read before it is shaped into the wire envelope."""

    rows: list[dict[str, Any] | None]
    next_cursor: str | None
    has_more: bool
    total: int | None
    scope_limited: bool


def _run_paged(fn: Callable[[], _PagedRead]) -> dict[str, Any]:
    """Wrap a paged read as `{"result", "next_cursor", "has_more", ["total"],
    "scope_limited"}`.

    `total` is present only when the caller asked for it (`include_total`).
    Rows retain their payload/lineage split and add `redacted_fields`.
    Errors keep the shared envelope and carry no read marks.
    """
    result, error = _try(fn)
    if error is not None:
        return error
    read = _PagedRead(*result)
    envelope: dict[str, Any] = {
        "result": read.rows,
        "next_cursor": read.next_cursor,
        "has_more": read.has_more,
    }
    if read.total is not None:
        envelope["total"] = read.total
    envelope["scope_limited"] = read.scope_limited
    return envelope


def _over_cap_limit(tool: str, limit: int) -> ValidationFailed:
    return ValidationFailed(
        f"{tool} limit must be <= {MAX_LIMIT}, got {limit!r}",
        code="INVALID_LIMIT",
    )


def _count_only_after() -> ValidationFailed:
    return ValidationFailed(
        "limit=0 with include_total=true is count-only mode, which takes no "
        "after; drop after, or pass a limit of 1 or more to page",
        code="INVALID_LIMIT",
    )


def _query_objects_page(
    resolve_client: Callable[[], OntologyClient],
    obj_type: Any,
    where: Any,
    order_by: Any,
    limit: Any,
    after: Any,
    include_total: Any,
) -> _PagedRead:
    """Apply the MCP cap and delegate one query page to the client surface."""
    client = resolve_client()
    obj_type = _tool_string("obj_type", obj_type)
    where = _tool_optional_object("where", where)
    limit = _tool_optional_int("limit", limit)
    after = _tool_optional_string("after", after)
    include_total = _tool_bool("include_total", include_total)
    # Once the arguments have their shape, an undeclared type is refused
    # before any other check (#194), so an over-cap limit or a stray cursor
    # never masks UNKNOWN_OBJECT_TYPE.
    client._require_object_type(obj_type)
    if limit is None and after is not None:
        # Delegate this refusal to the underlying client so its existing
        # AFTER_WITHOUT_LIMIT code/message remains the only validation for
        # this branch. Current client/query code raises before reading any
        # rows; this fallback is defensive if that contract ever regresses.
        client.list(obj_type, where, limit=None, after=after)
        raise RuntimeError("the underlying paged read accepted after without limit")
    if limit is not None and limit > MAX_LIMIT:
        raise _over_cap_limit("get_objects", limit)
    if limit == 0 and include_total:
        # Count-only mode: no page is read, so `order_by` cannot matter.
        if after is not None:
            raise _count_only_after()
        count = client.count(obj_type, where)
        scope_limited, _ = client._read_marks(obj_type)
        return _PagedRead([], None, count > 0, count, scope_limited)
    page_limit = DEFAULT_LIMIT if limit is None else limit
    if order_by is None:
        page = client.list(obj_type, where, limit=page_limit, after=after)
    else:
        page = client.list(
            obj_type,
            where,
            limit=page_limit,
            after=after,
            order_by=order_by,
        )
    total = client.count(obj_type, where) if include_total else None
    scope_limited, redacted = client._read_marks(obj_type)
    return _PagedRead(
        [_serialize(obj, redacted) for obj in page.items],
        page.next_cursor,
        page.has_more,
        total,
        scope_limited,
    )


_EVENT_CURSOR = re.compile(r"ev1\.([0-9]+)\.([0-9]+)")
"""A `list_events` cursor: the log position `(audit_index, event_index)` of
the last row on the previous page. Resuming keeps rows strictly after it."""


def _event_instant(name: str, value: str | None) -> datetime | None:
    """Parse a `since`/`until` boundary as a UTC instant; refuse a naive,
    unparseable, or out-of-range one (#217)."""
    if value is None:
        return None
    try:
        instant: datetime | None = datetime.fromisoformat(value)
    except ValueError:
        instant = None
    if instant is None or instant.utcoffset() is None:
        raise ValidationFailed(
            f"list_events {name} must be an ISO 8601 date-time with a UTC "
            f"offset, such as 2026-10-07T09:00:00+00:00; got {value!r}",
            code="INVALID_PARAMS",
        )
    try:
        return instant.astimezone(timezone.utc)
    except OverflowError:
        # The value parses, but shifting it to UTC leaves years 0001-9999
        # (for example 0001-01-01T00:00:00+01:00). Refuse it here, before
        # the store's conversion turns it into a generic INTERNAL_ERROR.
        raise ValidationFailed(
            f"list_events {name} must stay within years 0001-9999 once "
            f"converted to UTC; got {value!r}",
            code="INVALID_PARAMS",
        ) from None


def _event_paging(
    limit: int | None, after: str | None, include_total: bool
) -> tuple[int, tuple[int, int] | None]:
    """Apply the `query_objects` paging rules; return (page limit, cursor).

    A page limit of 0 means count-only mode.
    """
    if limit is None and after is not None:
        raise ValidationFailed(
            "list_events: after= was given without limit= -- pass the same "
            "limit as the page that returned this cursor",
            code="AFTER_WITHOUT_LIMIT",
        )
    if limit is not None and limit > MAX_LIMIT:
        raise _over_cap_limit("list_events", limit)
    if limit == 0 and include_total:
        if after is not None:
            raise _count_only_after()
        return 0, None
    if limit is not None and limit < 1:
        raise ValidationFailed(
            f"list_events limit must be >= 1, got {limit!r}",
            code="INVALID_LIMIT",
        )
    page_limit = DEFAULT_LIMIT if limit is None else limit
    if after is None:
        return page_limit, None
    match = _EVENT_CURSOR.fullmatch(after)
    if match is None:
        raise _invalid_event_cursor()
    try:
        return page_limit, (int(match[1]), int(match[2]))
    except ValueError:
        # The shape matches but a digit group exceeds the interpreter's
        # int-conversion limit (#218). Still a malformed cursor, and the
        # interpreter's message is not for the consumer.
        raise _invalid_event_cursor() from None


def _invalid_event_cursor() -> ValidationFailed:
    return ValidationFailed(
        "list_events after must be a next_cursor returned by list_events",
        code="INVALID_CURSOR",
    )


def _event_row(
    entry: AuditEntry, event: EmittedEvent, hidden: frozenset[str]
) -> dict[str, Any]:
    """One wire row; hidden keys are already absent from `event.payload`."""
    return {
        "event_type": event.event_type,
        "about_type": event.about_type,
        "about_id": event.about_id,
        "ts": entry.ts.isoformat(),
        "invocation_id": entry.invocation_id,
        "payload": dict(event.payload),
        "redacted_fields": sorted(hidden),
    }


def _list_events_page(
    resolve_client: Callable[[], OntologyClient],
    event_type: Any,
    about_type: Any,
    about_id: Any,
    since: Any,
    until: Any,
    limit: Any,
    after: Any,
    include_total: Any,
) -> _PagedRead:
    """Validate, read the visible event log, and cut one page from it.

    Validation order: argument shapes, `event_type` declared, `about_type`
    declared, `about_id` needs `about_type`, the time window, the paging
    rules, then the cursor. The engine filters scope, row policy, retired
    subjects, and redaction; this function only pages its positioned rows.
    """
    client = resolve_client()
    checked_event_type = _tool_optional_string("event_type", event_type)
    checked_about_type = _tool_optional_string("about_type", about_type)
    checked_about_id = _tool_optional_string("about_id", about_id)
    checked_since = _tool_optional_string("since", since)
    checked_until = _tool_optional_string("until", until)
    checked_limit = _tool_optional_int("limit", limit)
    checked_after = _tool_optional_string("after", after)
    checked_include_total = _tool_bool("include_total", include_total)
    if checked_event_type is not None:
        client._require_event_type(checked_event_type)
    if checked_about_type is not None:
        client._require_object_type(checked_about_type)
    if checked_about_id is not None and checked_about_type is None:
        raise ValidationFailed(
            "list_events about_id needs about_type: name the subject's "
            "object type as well as its id",
            code="INVALID_PARAMS",
        )
    since_at = _event_instant("since", checked_since)
    until_at = _event_instant("until", checked_until)
    page_limit, cursor = _event_paging(checked_limit, checked_after, checked_include_total)
    about = (
        None if checked_about_type is None or checked_about_id is None
        else (checked_about_type, checked_about_id)
    )
    rows = client._event_rows(
        checked_event_type, about=about, since=since_at, until=until_at
    )
    if checked_about_type is not None and about is None:
        rows = [row for row in rows if row[2].about_type == checked_about_type]
    scope_limited = client._event_scope_limited(checked_event_type, checked_about_type)
    total = len(rows) if checked_include_total else None
    if page_limit == 0:
        return _PagedRead([], None, len(rows) > 0, total, scope_limited)
    rest = rows if cursor is None else [row for row in rows if row[0] > cursor]
    page = rest[:page_limit]
    has_more = len(rest) > page_limit
    next_cursor = f"ev1.{page[-1][0][0]}.{page[-1][0][1]}" if has_more else None
    return _PagedRead(
        [_event_row(entry, event, hidden) for _, entry, event, hidden in page],
        next_cursor,
        has_more,
        total,
        scope_limited,
    )


def _get_object(
    resolve_client: Callable[[], OntologyClient], obj_type: Any, obj_id: Any
) -> dict[str, Any] | None:
    """Read one object, adding declaration-only redaction names after success."""
    client = resolve_client()
    checked_obj_type = _tool_string("obj_type", obj_type)
    checked_obj_id = _tool_string("obj_id", obj_id)
    client._require_object_type(checked_obj_type)
    obj = client.get(checked_obj_type, checked_obj_id)
    if obj is None:
        return None
    _, redacted = client._read_marks(checked_obj_type)
    return _serialize(obj, redacted)


def _count_objects(
    resolve_client: Callable[[], OntologyClient], obj_type: Any, where: Any
) -> tuple[int, bool]:
    """Count visible rows, then compute the declaration-only scope mark."""
    client = resolve_client()
    checked_obj_type = _tool_string("obj_type", obj_type)
    checked_where = _tool_optional_object("where", where)
    client._require_object_type(checked_obj_type)
    count = client.count(checked_obj_type, checked_where)
    scope_limited, _ = client._read_marks(checked_obj_type)
    return count, scope_limited


def _traverse_links(
    client: OntologyClient,
    obj_type: Any,
    link_api_name: Any,
    obj_id: Any,
    reverse: Any,
    limit: Any,
    after: Any,
    include_total: Any,
) -> _PagedRead:
    """Validate, traverse one page, then mark the result type.

    Every read goes through `client.traverse`, so the anchor/link checks
    and the identity-revealing denial run before any paging rule, and a
    count-only total is never computed without them.
    """
    checked_obj_type = _tool_string("obj_type", obj_type)
    checked_link = _tool_string("link_api_name", link_api_name)
    checked_obj_id = _tool_string("obj_id", obj_id)
    checked_reverse = _tool_bool("reverse", reverse)
    checked_limit = _tool_optional_int("limit", limit)
    checked_after = _tool_optional_string("after", after)
    checked_include_total = _tool_bool("include_total", include_total)
    # The anchor type resolves before the link lookup (#194): an undeclared
    # anchor is UNKNOWN_OBJECT_TYPE even when the link is unknown too.
    client._require_object_type(checked_obj_type)

    def traverse(**paging: Any) -> Any:
        if checked_reverse:
            return client.traverse(
                checked_obj_type, checked_link, checked_obj_id, reverse=True, **paging
            )
        return client.traverse(checked_obj_type, checked_link, checked_obj_id, **paging)

    if checked_limit is None and checked_after is not None:
        # The client refuses with its own AFTER_WITHOUT_LIMIT, after its
        # anchor/link checks; this fallback is defensive only.
        traverse(after=checked_after)
        raise RuntimeError("the underlying traversal accepted after without limit")
    count_only = checked_limit == 0 and checked_include_total
    if checked_limit is not None and (
        checked_limit > MAX_LIMIT or (count_only and checked_after is not None)
    ):
        # An MCP-only refusal still waits for the engine's gates (anchor,
        # link, identity-revealing denial): a zero-limit probe runs them and
        # then stops at the engine's own INVALID_LIMIT before reading rows.
        try:
            traverse(limit=0)
        except ValidationFailed as exc:
            if exc.code != "INVALID_LIMIT":
                raise
        else:
            raise RuntimeError("the underlying traversal accepted limit=0")
        if checked_limit > MAX_LIMIT:
            raise _over_cap_limit("traverse_links", checked_limit)
        raise _count_only_after()
    if count_only:
        total: int | None = len(traverse())
        page = Page(items=[], next_cursor=None, has_more=bool(total))
    else:
        page_limit = DEFAULT_LIMIT if checked_limit is None else checked_limit
        page = traverse(limit=page_limit, after=checked_after)
        total = len(traverse()) if checked_include_total else None
    target_type = client._link_target_type(checked_link, reverse=checked_reverse)
    scope_limited, redacted = client._read_marks(target_type)
    return _PagedRead(
        [_serialize(obj, redacted) for obj in page.items],
        page.next_cursor,
        page.has_more,
        total,
        scope_limited,
    )


def _aggregate_objects(
    resolve_client: Callable[[], OntologyClient],
    obj_type: Any,
    value_field: Any,
    group_by: Any,
    where: Any,
    func: Any,
) -> AggregateValue | dict[str, float] | dict[str, int]:
    """Validate one MCP aggregate request and delegate to the client seam."""
    client = resolve_client()
    checked_obj_type = _tool_string("obj_type", obj_type)
    checked_value_field = _tool_optional_string("value_field", value_field)
    checked_group_by = _tool_optional_string("group_by", group_by)
    checked_where = _tool_optional_object("where", where)
    checked_func = cast(AggregateFunc, _tool_string("func", func))
    client._require_object_type(checked_obj_type)
    # `value_field` is optional only for func="count"; for every other func a
    # `None` value_field is invalid and must reach the engine's
    # `INVALID_PARAMS` refusal rather than be rejected here. The typed
    # `OntologyClient.aggregate(_by)` overloads narrow `value_field` to
    # `str` outside `func="count"`, a func-dependent rule that cannot be
    # expressed on a runtime `AggregateFunc`; typing the field as `Any`
    # hands the decision to the engine, which is the only place that
    # refuses a genuinely missing `value_field`.
    engine_value_field: Any = checked_value_field
    if checked_group_by is None:
        return client.aggregate(
            checked_obj_type,
            engine_value_field,
            checked_where,
            func=checked_func,
        )
    return client.aggregate_by(
        checked_obj_type,
        engine_value_field,
        checked_group_by,
        checked_where,
        func=checked_func,
    )


def _register_aggregate_tool(
    server: MCPServer,
    resolve_client: Callable[[], OntologyClient],
    read_only: ToolAnnotations,
) -> None:
    """Register the guarded aggregate tool on one MCP server."""

    @server.tool(annotations=read_only)
    async def aggregate_objects(
        obj_type: _ToolString = None,
        value_field: Annotated[
            str | None,
            SkipValidation(),
            Field(
                description=(
                    "Declared payload field to reduce. Optional for "
                    "func=\"count\": omit it to count every visible row "
                    "instead of rows carrying a specific field."
                )
            ),
        ] = None,
        group_by: Annotated[
            str | None,
            SkipValidation(),
            Field(
                description=(
                    "Optional declared payload field for grouped aggregation."
                )
            ),
        ] = None,
        where: Annotated[
            dict[str, Any] | None,
            SkipValidation(),
            Field(
                description=(
                    "Typed operator matching on declared payload fields; bare "
                    "scalars use equality and mapping values support gt, gte, "
                    "lt, lte, in, ne, and contains; several operators in one "
                    "mapping are AND-ed, so {\"gte\": a, \"lt\": b} is a range."
                )
            ),
        ] = None,
        func: Annotated[
            str | None,
            SkipValidation(),
            Field(description="One of mean, count, sum, min, or max."),
        ] = "mean",
    ) -> dict[str, Any]:
        """Aggregate a visible selection with min-N release discipline.

        ``func`` defaults to ``mean`` and accepts ``count``, ``sum``, ``min``,
        and ``max``. ``value_field`` is required for every func except
        ``count``, where it is optional: omit it to get a min-N-released
        count of every visible row, or supply a declared field to count
        only rows carrying it. Supplying ``group_by`` returns one released
        value per group; an empty selection refuses with
        ``MIN_N_VIOLATION`` in both grouped and ungrouped forms. ``where``
        uses the same typed JSON operator grammar as ``query_objects``.
        """
        return _run(
            lambda: _aggregate_objects(
                resolve_client,
                obj_type,
                value_field,
                group_by,
                where,
                func,
            )
        )


def _property_payload(p: PropertyDef) -> dict[str, Any]:
    return {
        "name": p.name,
        "type": p.type,
        "choices": None if p.choices is None else list(p.choices),
        "transitions": None if p.transitions is None else {
            "initial": list(p.transitions.initial),
            "moves": {
                state: list(targets)
                for state, targets in p.transitions.moves.items()
            },
        },
        "fields": None if p.fields is None else [
            {
                "name": field.name,
                "type": field.type,
                "required": field.required,
                "choices": None if field.choices is None else list(field.choices),
            }
            for field in p.fields
        ],
        "required": p.required,
        "ai_usable": p.sensitivity.ai_usable,
        "human_visible": p.sensitivity.human_visible,
        "scope_level": p.scope_level,
    }


def _object_type_payload(defn: ObjectTypeDef) -> dict[str, Any]:
    return {
        "api_name": defn.api_name,
        "display_name": defn.display_name,
        "description": defn.description,
        "layer": defn.layer,
        "primary_key": defn.primary_key,
        "properties": [_property_payload(p) for p in defn.properties],
        "rules": [
            {"name": rule.name, "message": rule.message}
            for rule in defn.rules
        ],
    }


def _event_type_payload(
    defn: EventTypeDef, action_types: Mapping[str, ActionTypeDef]
) -> dict[str, Any]:
    emitters = [a for a in action_types.values() if defn.api_name in a.emits]
    return {
        "api_name": defn.api_name,
        "description": defn.description,
        "properties": [_property_payload(p) for p in defn.properties],
        "about_types": sorted({a.target_type for a in emitters}),
        "emitted_by": sorted(a.api_name for a in emitters),
    }


def _list_object_types(
    resolve_client: Callable[[], OntologyClient], ontology: OntologyDef
) -> dict[str, Any]:
    resolve_client()
    return {
        "object_types": [
            _object_type_payload(d) for d in ontology.registry.object_types.values()
        ]
    }


def _list_link_types(
    resolve_client: Callable[[], OntologyClient], ontology: OntologyDef
) -> dict[str, Any]:
    resolve_client()
    return {
        "link_types": [
            _link_type_payload(d) for d in ontology.registry.link_types.values()
        ]
    }


def _list_action_types(
    resolve_client: Callable[[], OntologyClient], ontology: OntologyDef
) -> dict[str, Any]:
    resolve_client()
    return {
        "action_types": [
            _action_type_payload(d) for d in ontology.registry.action_types.values()
        ]
    }


def _list_functions(
    resolve_client: Callable[[], OntologyClient], ontology: OntologyDef
) -> dict[str, Any]:
    resolve_client()
    return {
        "functions": [
            _function_payload(d) for d in ontology.registry.functions.values()
        ]
    }


def _list_event_types(
    resolve_client: Callable[[], OntologyClient], ontology: OntologyDef
) -> dict[str, Any]:
    resolve_client()
    action_types = ontology.registry.action_types
    return {
        "event_types": [
            _event_type_payload(d, action_types)
            for d in ontology.registry.event_types.values()
        ]
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


def _parameter_payload(parameter: ActionParameterDef) -> dict[str, Any]:
    return {
        "name": parameter.name,
        "type": parameter.type,
        "description": parameter.description,
        "choices": None if parameter.choices is None else list(parameter.choices),
        "fields": None
        if parameter.fields is None
        else [
            {
                "name": field.name,
                "type": field.type,
                "required": field.required,
                "choices": None if field.choices is None else list(field.choices),
            }
            for field in parameter.fields
        ],
        "required": parameter.required,
        "refers_to": parameter.refers_to,
    }


def _action_type_payload(defn: ActionTypeDef) -> dict[str, Any]:
    return {
        "api_name": defn.api_name,
        "display_name": defn.display_name,
        "target_type": defn.target_type,
        "emits": list(defn.emits),
        "executable_by_roles": list(defn.executable_by_roles),
        "description": defn.description,
        "capabilities": list(defn.capabilities),
        "parameters": [
            {**_parameter_payload(parameter), "scope_semantics": parameter.scope_semantics}
            for parameter in defn.parameters
        ],
    }


def _function_payload(defn: FunctionDef) -> dict[str, Any]:
    return {
        "api_name": defn.api_name,
        "description": defn.description,
        "input_description": defn.input_description,
        "output_description": defn.output_description,
        "capabilities": list(defn.capabilities),
        "parameters": [_parameter_payload(parameter) for parameter in defn.parameters],
    }


def _register_tools(
    server: MCPServer,
    ontology: OntologyDef,
    resolve_client: Callable[[], OntologyClient],
) -> None:
    """Register all fourteen tools onto `server`, ONE tool body each, shared by
    both `build_mcp_server` and `build_multi_consumer_mcp_server`.

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

    The five introspection tools (`list_object_types`/`list_link_types`/
    `list_action_types`/`list_functions`/`list_event_types`) still read only `ontology.
    registry` for their payload -- that part is schema, not consumer data,
    and untouched by who's asking. They call `resolve_client()` too, so
    that identity is proven -- and a raising resolver refused -- before
    ANY tool answers, introspection included. A raising
    resolver is classified by `_try` into the identical structured
    envelope an `OntaryError` already produces (see `_run_unwrapped`/`_run`),
    so introspection fails exactly like every other tool rather than
    growing its own error path.
    """

    tool_annotations = _load_tool_annotations()
    read_only = tool_annotations(read_only_hint=True)
    destructive = tool_annotations(destructive_hint=True)

    @server.tool(annotations=read_only)
    async def list_object_types() -> dict[str, Any]:
        """List every declared object type (introspection, not consumer data)."""

        return _run_unwrapped(partial(_list_object_types, resolve_client, ontology))

    @server.tool(annotations=read_only)
    async def list_link_types() -> dict[str, Any]:
        """List every declared link type (introspection, not consumer data)."""

        return _run_unwrapped(partial(_list_link_types, resolve_client, ontology))

    @server.tool(annotations=read_only)
    async def list_action_types() -> dict[str, Any]:
        """List every declared action type (introspection, not consumer data)."""

        return _run_unwrapped(partial(_list_action_types, resolve_client, ontology))

    @server.tool(annotations=read_only)
    async def list_functions() -> dict[str, Any]:
        """List every declared function (introspection, not consumer data)."""

        return _run_unwrapped(partial(_list_functions, resolve_client, ontology))

    @server.tool(annotations=read_only)
    async def list_event_types() -> dict[str, Any]:
        """List every declared event type (introspection, not consumer data)."""

        return _run_unwrapped(partial(_list_event_types, resolve_client, ontology))

    @server.tool(annotations=read_only)
    async def get_declarations() -> dict[str, Any]:
        """This runtime's declared contracts: authority model,
        write-back/re-ingest/visibility/transaction-ownership/idempotency
        stance, audit scope, and this ontology's `min_n` -- identical to
        `client.declarations`."""
        return _run(lambda: resolve_client().declarations.model_dump())

    @server.tool(annotations=read_only)
    async def get_object(
        obj_type: _ToolString = None, obj_id: _ToolString = None
    ) -> dict[str, Any]:
        """Fetch one object by type + id, through the guarded read layer.

        `client.get` returns a `StoredObject` (payload/lineage split); the
        wire row is `{"payload": {...}, "lineage": {...}, "redacted_fields": [...]}`
        so a caller sees the object's own properties and its storage/
        provenance metadata (object_type, object_id, valid_from/to,
        source_system, source_id, extracted_at) as two clearly separated,
        typed halves rather than lineage fields mixed into the payload
        under magic `_`-prefixed keys. `redacted_fields` lists sorted property
        names hidden by declarations; each key is absent from `payload`, never null.
        Missing or retired objects return `{"result": null}`; an out-of-scope
        object still refuses with `VISIBILITY_DENIED`. This response has no
        `scope_limited`; that marker on list/count/traversal depends on declarations only.
        """

        return _run(lambda: _get_object(resolve_client, obj_type, obj_id))

    @server.tool(annotations=read_only)
    async def query_objects(
        obj_type: _ToolString = None,
        where: Annotated[
            dict[str, Any] | None,
            SkipValidation(),
            Field(
                description=(
                    "Typed operator matching on declared payload fields; bare "
                    "scalars use equality and mapping values support gt, gte, "
                    "lt, lte, in, ne, and contains (several operators in one "
                    "mapping are AND-ed, so {\"gte\": a, \"lt\": b} is a "
                    "range); unknown keys raise "
                    "UNKNOWN_FIELD."
                )
            ),
        ] = None,
        order_by: Annotated[
            str | tuple[str, str] | list[str] | None,
            SkipValidation(),
            Field(
                description=(
                    "Order by a declared payload field, ascending by default; "
                    "use [field, 'desc'] for descending order."
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
        include_total: Annotated[
            bool,
            SkipValidation(),
            Field(
                description=(
                    "When true, add an integer total: the visible-row count "
                    "across all pages (not min-N gated). With limit=0 it is "
                    "count-only mode: no rows, just the total."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Query/filter objects of a type, through the guarded read layer.

        The `where` grammar is typed operator matching on declared payload fields:
        bare scalars use equality and mapping values support `gt`,
        `gte`, `lt`, `lte`, `in`, `ne`, and `contains`, AND-ed when one mapping
        names several (`{"gte": a, "lt": b}` is a range); unknown keys raise `UNKNOWN_FIELD`;
        lineage fields are not filterable.
        `limit` defaults to 100 and has a hard maximum of 1000. `after` is the
        opaque cursor from the previous page and requires an explicit
        `limit`; it is passed to the underlying paged read. The successful
        response keeps rows under `result` and includes `next_cursor` and the
        boolean `has_more`. `has_more` is true exactly when at least one more
        visible row follows this page; `next_cursor` is the cursor for that
        next page when `has_more` is true and `None` exactly when it is false.
        A limit below one and `after` without `limit` use the underlying
        `INVALID_LIMIT`/`AFTER_WITHOUT_LIMIT` validations.

        `include_total=true` adds an integer `total`: the visible-row count
        across all pages for this `where`, equal to `count_objects` and not
        min-N gated. Without it the `total` key is absent. `limit=0` with
        `include_total=true` is count-only mode: `result` is `[]`,
        `next_cursor` is `None`, `has_more` is true when `total` is above
        zero, and `order_by` is ignored; count-only mode takes no `after`.
        `limit=0` without `include_total` refuses with `INVALID_LIMIT`.

        `scope_limited` is a boolean based on declarations only for this consumer
        and type, never on stored rows. Each row has `payload`, `lineage`, and
        `redacted_fields`, a sorted list of hidden property names. A redacted key
        is absent from `payload`, never null; see `get_object` for the row shape."""
        return _run_paged(
            lambda: _query_objects_page(
                resolve_client,
                obj_type,
                where,
                order_by,
                limit,
                after,
                include_total,
            )
        )

    @server.tool(annotations=read_only)
    async def count_objects(
        obj_type: _ToolString = None,
        where: Annotated[
            dict[str, Any] | None,
            SkipValidation(),
            Field(
                description=(
                    "Typed operator matching on declared payload fields; bare "
                    "scalars use equality and mapping values support gt, gte, "
                    "lt, lte, in, ne, and contains; several operators in one "
                    "mapping are AND-ed, so {\"gte\": a, \"lt\": b} is a range."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Count objects visible to this consumer after applying ``where``.

        This delegates to ``OntologyClient.count`` so the Python and MCP
        surfaces share field gates, the operator evaluator, scope, and row
        visibility. A fully scoped-away selection returns zero.
        The response is `{"result": int, "scope_limited": bool}`; `scope_limited`
        depends on declarations only for this consumer and type, never on stored rows.

        This is a visible-row count and is not min-N-gated: it reveals only
        what ``query_objects`` already lists. For a min-N-released count
        call ``aggregate_objects`` with ``func="count"`` (no ``value_field``
        needed).
        """

        return _run_marked(lambda: _count_objects(resolve_client, obj_type, where))

    _register_aggregate_tool(server, resolve_client, read_only)

    @server.tool(annotations=read_only)
    async def traverse_links(
        obj_type: _ToolString = None,
        obj_id: _ToolString = None,
        link_api_name: _ToolString = None,
        reverse: _ToolBool = False,
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
        include_total: Annotated[
            bool,
            SkipValidation(),
            Field(
                description=(
                    "When true, add an integer total: the visible-row count "
                    "across all pages (not min-N gated). With limit=0 it is "
                    "count-only mode: no rows, just the total."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Traverse a declared link from an object, through the guarded read
        layer, one page at a time.
        Omit ``reverse`` or pass ``False`` for forward traversal; pass
        ``reverse=True`` to traverse from the link's target side. The value
        must be a JSON boolean when supplied.

        `limit` defaults to 100 and has a hard maximum of 1000, as in
        `query_objects`. `after` is the opaque `next_cursor` from the previous
        page and requires an explicit `limit`. Rows come in the store's link
        order. The response includes `next_cursor` and the boolean `has_more`:
        `has_more` is true exactly when at least one more visible linked row
        follows this page, and `next_cursor` is `None` exactly when
        `has_more` is false. A limit above 1000, below one, or `after`
        without `limit` refuse with `INVALID_LIMIT`/`AFTER_WITHOUT_LIMIT`.

        `include_total=true` adds an integer `total`: the visible linked-row
        count across all pages, not min-N gated. Without it the `total` key is
        absent. `limit=0` with `include_total=true` is count-only mode:
        `result` is `[]`, `next_cursor` is `None`, and `has_more` is true when
        `total` is above zero; count-only mode takes no `after`. `limit=0`
        without `include_total` refuses with `INVALID_LIMIT`.

        The response also has `result` and boolean `scope_limited`, which depends on
        declarations only for this consumer and the result type (the link's source
        type when reversed), never on stored rows. Each row has `payload`, `lineage`,
        and `redacted_fields`, a sorted list of hidden property names. A redacted
        key is absent from `payload`, never null; see `get_object` for the row shape."""
        return _run_paged(
            lambda: _traverse_links(
                resolve_client(),
                obj_type,
                link_api_name,
                obj_id,
                reverse,
                limit,
                after,
                include_total,
            )
        )

    @server.tool(annotations=read_only)
    async def list_events(
        event_type: Annotated[
            str | None,
            SkipValidation(),
            Field(description="Declared event type api_name; omit for every type."),
        ] = None,
        about_type: Annotated[
            str | None,
            SkipValidation(),
            Field(description="Declared object type the events are about."),
        ] = None,
        about_id: Annotated[
            str | None,
            SkipValidation(),
            Field(description="Subject object id; requires about_type."),
        ] = None,
        since: Annotated[
            str | None,
            SkipValidation(),
            Field(
                description=(
                    "Inclusive lower bound: an ISO 8601 date-time with a UTC "
                    "offset."
                )
            ),
        ] = None,
        until: Annotated[
            str | None,
            SkipValidation(),
            Field(
                description=(
                    "Exclusive upper bound: an ISO 8601 date-time with a UTC "
                    "offset."
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
        include_total: Annotated[
            bool,
            SkipValidation(),
            Field(
                description=(
                    "When true, add an integer total: the visible-event count "
                    "across all pages (not min-N gated). With limit=0 it is "
                    "count-only mode: no rows, just the total."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        """List the business events this consumer may see, in log order
        (audit sequence, then emission order), one page at a time.

        Filter by `event_type`, by subject (`about_type`, optionally with
        `about_id`), and by time (`since` inclusive, `until` exclusive; ISO
        8601 strings with an offset). An event is listed only when its
        subject is visible to this consumer, using the subject's latest row
        (retired subjects included). Each row has `event_type`, `about_type`,
        `about_id`, `ts`, `invocation_id`, `payload`, and `redacted_fields`,
        a sorted list of hidden field names; a redacted key is absent from
        `payload`, never null.

        Paging follows `query_objects`: `limit` defaults to 100 and has a
        hard maximum of 1000, `after` is the previous `next_cursor` and
        requires `limit`, `next_cursor` is `None` exactly when `has_more` is
        false, `include_total=true` adds the visible-event `total`, and
        `limit=0` with `include_total=true` is count-only mode.
        `scope_limited` depends on declarations only: it is true when any
        subject type these events can be about is scoped or has row
        visibility. Undeclared names refuse with `UNKNOWN_EVENT_TYPE` or
        `UNKNOWN_OBJECT_TYPE`."""
        return _run_paged(
            lambda: _list_events_page(
                resolve_client,
                event_type,
                about_type,
                about_id,
                since,
                until,
                limit,
                after,
                include_total,
            )
        )

    @server.tool(annotations=destructive)
    async def execute_action(
        api_name: _ToolString = None, params: _ToolObject = None
    ) -> dict[str, Any]:
        """Execute a governed action (role/scope/precondition/audit pipeline)."""
        return _run(
            lambda: resolve_client().execute(
                _tool_string("api_name", api_name), _tool_object("params", params)
            )
        )

    @server.tool(annotations=read_only)
    async def call_function(
        api_name: _ToolString = None, params: _ToolObject = None
    ) -> dict[str, Any]:
        """Call a derived Function through the guarded read layer."""
        return _run(
            lambda: resolve_client().call_function(
                _tool_string("api_name", api_name), _tool_object("params", params)
            )
        )


def build_mcp_server(
    ontology: OntologyDef | SupportsDefinition,
    store: Store,
    consumer: Consumer,
    *,
    name: str | None = None,
    capabilities: Mapping[CapabilityHandle[Any], object] | None = None,
) -> MCPServer:
    """Build an `MCPServer` bound to exactly one `(ontology, store,
    consumer)` triple -- one server process, one Consumer identity.

    `ontology` accepts a plain `OntologyDef` (descriptor authoring) or any
    `SupportsDefinition` -- an `Ontology` (class authoring), or anything
    else with a `definition` property -- the same annotation as
    `OntologyClient`'s own constructor, which this function delegates
    to internally (#222). Handlers arrive pre-bound: an `Ontology`'s
    `@ontology.action(...)`/`@ontology.function(...)`-declared handlers
    auto-bind to the internal `OntologyClient` this server builds (the old
    `register_handlers` callback parameter was removed -- there is no
    per-server registration step left to perform here). A descriptor-
    authored `OntologyDef` passed here has no declared handlers to
    auto-bind, so its actions are unregistered (`UNKNOWN_ACTION`) unless
    it is migrated to class authoring -- see `ontary.client`'s docstring.

    Delegates tool registration to `_register_tools` with a constant
    `resolve_client` that always returns the one client built here and
    never raises -- so this function's own behavior is unchanged by the
    shared-registration refactor.
    """
    client = OntologyClient(
        ontology,
        store,
        consumer,
        capabilities=capabilities,
    )

    definition = resolve_definition(ontology)
    server = _load_mcp_server()(name or definition.name)
    _register_tools(server, definition, lambda: client)
    return server


def build_multi_consumer_mcp_server(
    ontology: OntologyDef | SupportsDefinition,
    store: Store,
    *,
    resolve_consumer: ConsumerResolver,
    name: str | None = None,
    capabilities: Mapping[CapabilityHandle[Any], object] | None = None,
    token_verifier: TokenVerifier | None = None,
    auth: AuthSettings | None = None,
) -> MCPServer:
    """Build an `MCPServer` serving MANY proven identities over one
    `(ontology, store)` pair -- no `consumer` parameter. Each tool
    invocation resolves its own
    `Consumer` from that request's verified MCP `AccessToken`, via the
    caller-supplied `resolve_consumer`.

    `token_verifier` and `auth` are forwarded VERBATIM to the underlying
    `MCPServer(...)` call below -- this is the only place a deployer can wire
    them, because `MCPServer` has no public setter for either afterwards (its
    token verifier lives on a private, underscore-prefixed field, and its
    auth pipeline -- `RequireAuthMiddleware`, the bearer-auth backend, the
    `/.well-known/oauth-protected-resource` metadata route -- is built once,
    inside `streamable_http_app()`/`sse_app()`, gated on both being present
    at that call). Passing neither is a legitimate stdio-only or
    intentionally-open deployment; passing exactly one of `token_verifier`/
    `auth` is not a fail-closed runtime state at all -- `MCPServer.__init__`
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

    Transport options -- `stateless_http`, `json_response`,
    `transport_security`, `host`, and `port` -- are not constructor arguments
    on mcp 2.x; the deployer passes them to
    `server.run(transport="streamable-http", ...)` or (all but `port`)
    `server.streamable_http_app(...)`. On mcp 2.x, each request resolves its
    own token in stateful sessions too: the auth context is no longer copied
    once at `initialize`, and `tests/test_mcp_multi_consumer.py` pins both
    session modes at the ASGI boundary.

    **One runtime, many views.** Exactly one `OntologyRuntime` -- one
    `GuardedQuery`, one `ActionExecutor`, every ontology-declared handler
    bound once -- is built here, at construction, same as `build_mcp_server`
    builds one `OntologyClient` once. Every call goes through `runtime.
    for_consumer(...)`, a cheap view sharing that SAME `query`/`actions` by
    identity; two different identities' calls therefore see `client.actions`
    as the same object, not two independent copies of a role/scope guard.

    **Resolution is never cached.** The `resolve_client` seam
    `_register_tools` calls PER INVOCATION does the full read-token/
    resolve/stamp/`for_consumer` sequence below every single time -- there
    is no memoization keyed on the token, so a revoked or re-scoped token
    cannot be served from a stale binding, and two interleaved calls from
    different identities on this one server object never share any mutable
    per-call state.

    **Fail-closed sequence, per invocation, ALL fourteen tools:**

    1. No verified `AccessToken` on the request (`get_access_token()`
       returns `None` -- true for stdio, or HTTP with no `token_verifier`
       configured) -> `PermissionDenied` with code `UNAUTHENTICATED`. This is
       the FIRST thing a
       misconfigured deployment hits, so its message names the fix.
    2. `resolve_consumer(token)` returns `None` -> `PermissionDenied` with code
       `CONSUMER_UNRESOLVED` -- a verified principal exists, but nothing maps
       it to a `Consumer`.
       Deliberately a different code from (1): the fix differs (configure
       authentication vs. map this principal).
    3. `resolve_consumer(token)` raises an `OntaryError` -> re-raised as-is;
       `_try`/`_run` classify it exactly like an `OntaryError` raised from
       inside a tool body.
    4. `resolve_consumer(token)` raises anything else -> re-raised as a
       plain `RuntimeError` carrying NO part of the original message:
       `_try`'s classification keys off exception TYPE, and `KeyError`/
       `ValueError` are two of the types it already echoes verbatim for
       tool-body failures (`UNKNOWN_NAME`/`INVALID_PARAMS`) -- a resolver
       that happens to raise either of those (e.g. a `dict` lookup against
       an unmapped token) must NOT be echoed, since its message may embed
       token material, so it is deliberately re-typed to a type `_try`
       has no special case for and therefore routes to the generic
       `INTERNAL_ERROR` envelope.

    **The principal cannot be forged by author code.** After
    `resolve_consumer` returns a `Consumer`, THIS function -- not the
    resolver -- overwrites `principal` from the verified token:
    `token.subject or token.client_id` (`client_id` is non-optional on
    `AccessToken`, so an authenticated call always ends up with a
    `principal`). A resolver that sets `Consumer.principal` itself has that
    value discarded, never read.

    The import of `get_access_token` is deferred to call time via
    `_load_get_access_token`, exactly like `_load_mcp_server` above defers
    `MCPServer` -- see that function's docstring.
    """
    definition = resolve_definition(ontology)
    runtime = OntologyRuntime(
        ontology,
        store,
        capabilities=capabilities,
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
            # A resolver's own deliberate refusal -- surfaced unchanged so
            # `_try` classifies it exactly like any other `OntaryError`.
            raise
        except Exception as exc:
            # ANY other exception -- including a bare `KeyError`/
            # `ValueError` `_try` would otherwise echo verbatim for a
            # tool-body failure -- is re-typed to a plain `RuntimeError`
            # with a message that names nothing about the original
            # failure, so `_try`'s generic (message-free) `INTERNAL_ERROR`
            # branch is the only one that can possibly match (the
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

    server = _load_mcp_server()(
        name or definition.name,
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
        _load_mcp_server()
    except ImportError as exc:
        raise SystemExit(str(exc)) from exc
    raise SystemExit(
        "ontary.mcp_server has no default ontology to serve; call "
        "build_mcp_server(ontology, store, consumer).run() from your own "
        "entrypoint script."
    )


if __name__ == "__main__":  # pragma: no cover
    main()
