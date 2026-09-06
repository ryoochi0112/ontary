"""Function registry: derived-value calls over the guarded read layer (AC3).

A Function is a declared `FunctionDef` (`ontary.meta`) plus a registered
Python callable. `FunctionRegistry.register`/`function` refuse to bind a
callable to any `api_name` that isn't a declared `FunctionDef` on the
`OntologyRegistry` the registry was built with (mirrors
`ActionExecutor._register`'s "declared? then bindable" rule) and
reject double-registration the same way.

Functions receive a `BoundQuery`, never a raw `GuardedQuery` with a
separately-passed `Consumer`, and never the `ObjectStore`:

- No store handle reaches the function body, so a function cannot write the
  ontology, only derive values from already-guarded reads (原則1 / spec AC3).
  It is given no outward-write path either: a Function cannot declare or emit an
  effect (spec AC4) and its `BoundQuery` carries no dispatcher map (AC5b). That is
  a guarantee about what the SDK PROVIDES, not a sandbox -- a capability provider
  is unsandboxed author code, so `query.capability(...)` can itself write outward
  with no effect record and no audit row (spec §11, `Declarations.writeback`).
- The `Consumer` is fixed at construction (by whichever `OntologyClient`
  built the `BoundQuery`), not accepted as a per-call argument the function
  body could vary -- a function cannot read AS a different consumer than
  the one that invoked it, closing the one bypass a bare
  `(consumer, obj_type, ...)` -style call would otherwise open.
- What a function may read that a consumer may not -- the hidden-field
  aggregate exemption in `GuardedQuery._aggregate` -- rests on AUTHOR
  PROVENANCE, and provenance is minted here, at dispatch, never accepted
  from a caller. `FunctionRegistry.call` is the only place in the SDK that
  attaches the grant, because `register` already refused to bind a handler
  to anything but a declared `FunctionDef`: reaching the dispatch means the
  engine is about to run code the ontology author declared. The grant goes
  onto a per-call `BoundQuery` the caller never holds, so no window exists
  in which the caller's own object carries it.

`BoundQuery` exposes the same read surface as `GuardedQuery`
(`get`/`list`/`traverse`/`aggregate`/`aggregate_by`) with
`consumer` already applied -- no new guard logic lives here, it is a thin closure over an
already-guarded `GuardedQuery`. Spec `typed-actions.md` §6/AC8 additionally
gives `get`/`list`/`traverse` M4a-style typed overloads
(`type[T]`/`LinkHandle` in place of a string `api_name`), resolved via the
SAME `_class_stamp` identity-stamp helper `client.OntologyClient` uses
(`ontary.model`, which sits below this module -- the old function-body
imports from `authoring` are gone since C1 of the staged refactor).
Typed lookup failures use the validation kind, which keeps both `client.py`
and this module below the same import cycle.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Mapping
from typing import Any, Literal, TypeVar, cast, overload

from ontary._typed_api import _TypedReadMixin, list_objects, resolve_capability
from ontary.audit import CapabilityAccessRecord
from ontary.errors import PreconditionFailed, ValidationFailed
from ontary.meta import OntologyRegistry
from ontary.model import (
    CapabilityHandle,
    LinkHandle,
    OntologyObject,
)
from ontary.query import (
    _AUTHOR_DISPATCH,
    GuardedQuery,
    OrderBy,
    Page,
    TypedPage,
)
from ontary.security import Consumer
from ontary.store import StoredObject

T = TypeVar("T", bound="OntologyObject")
F = TypeVar("F", bound="OntologyObject")
P = TypeVar("P")


class BoundQuery(_TypedReadMixin):
    """A `GuardedQuery` with its `consumer` fixed -- the only object a
    registered Function handler ever receives (see module docstring).

    Visible-row ``count``/``exists`` and the other typed read operations are
    inherited from ``_TypedReadMixin``; every one delegates to this instance's
    fixed consumer rather than accepting a caller-selected identity.

    `registry` is optional (defaults to `None`) so a directly-constructed
    `BoundQuery(query, consumer)` -- as used by dedicated unit tests that
    only exercise the string-form surface -- keeps working unchanged;
    `OntologyClient.call_function` passes its ontology's registry so the
    typed overloads can resolve classes/`LinkHandle`s. Without a bound
    registry, a typed call's class-identity check can never match (there is
    nothing to match against) and fails closed with `ValidationFailed` code
    `UNKNOWN_NAME`, same as
    a class registered on a different ontology.

    **Constructing one grants nothing.** This class is exported, so a
    `BoundQuery` a caller builds is an ordinary consumer-tier read surface:
    no declared capabilities, and no author-provenance grant, so the hidden-
    field aggregate exemption refuses it exactly as a direct consumer
    selection is refused. Both authorities belong to a declared-Function
    DISPATCH, and `FunctionRegistry.call` hands them out on a throwaway
    instance it builds itself (`_for_author_dispatch`).

    Being "the object a Function receives" is therefore a fact about what
    the engine passes in, never a privilege the type carries. It used to be
    the latter -- author provenance was a class attribute here -- which made
    the exemption reachable by anyone who could write `BoundQuery(q, c)`.
    """

    _typed_owner = "function"

    def __init__(
        self,
        query: GuardedQuery,
        consumer: Consumer,
        registry: OntologyRegistry | None = None,
        *,
        capability_providers: Mapping[CapabilityHandle[Any], object] | None = None,
        capability_accesses: list[CapabilityAccessRecord] | None = None,
        disclosures: list[tuple[str, str]] | None = None,
    ) -> None:
        self._query = query
        self._consumer = consumer
        self._registry = registry
        self._declared_capabilities: frozenset[str] = frozenset()
        # No author-provenance grant, and no way to ask for one: a
        # `BoundQuery` a caller builds reads at consumer tier, exactly as it
        # carries no declared capabilities. Both authorities belong to a
        # declared-Function DISPATCH, and `FunctionRegistry.call` is the only
        # place that hands either of them out -- see `_for_author_dispatch`.
        self._author_dispatch = None
        # Capability providers ONLY -- deliberately no effect dispatchers here.
        # A Function cannot declare effects (spec `governed-effects` AC4:
        # `@ontology.function(effects=...)` is refused outright), so carrying
        # dispatchers it can never use would be dead surface inviting exactly the
        # wiring AC4 forbids. This withholds an outward-write PATH; it is not a
        # sandbox -- see the module docstring and AC4's scope note.
        self._capability_providers = dict(capability_providers or {})
        self._capability_accesses = capability_accesses
        # Where the guarded read layer records that the AC10 exemption -- and
        # only the exemption -- let this dispatch read a hidden field. See
        # `_TypedReadMixin._disclosures` and `OntologyClient.call_function`.
        self._disclosures = disclosures

    def _for_author_dispatch(self, declared: frozenset[str]) -> BoundQuery:
        """A throwaway view of this query for ONE declared-Function call:
        the author-provenance grant plus that function's declared
        capabilities, on an object no caller holds a reference to.

        A fresh instance, not an install-and-restore on `self`, and the
        difference is the whole guarantee. Mutating the caller's own
        `BoundQuery` for the duration of the dispatch leaves an
        author-tier object in the caller's hands while the handler runs --
        readable from a second thread, or from any caller code the handler
        re-enters. This instance is created here, reaches only the
        handler, and is unreferenced when the call returns; there is no
        window to restore.

        `_capability_accesses` is passed by reference on purpose: it is the
        list `OntologyClient.call_function` reads back to build the audit
        entry, so the clone must append to the caller's list, not a copy.
        `_disclosures` travels the same way and for the same reason -- it is
        the other thing that call reads back, and a copy would lose every
        hidden-value release the handler made.
        """
        dispatched = BoundQuery(
            self._query,
            self._consumer,
            self._registry,
            capability_providers=self._capability_providers,
            capability_accesses=self._capability_accesses,
            disclosures=self._disclosures,
        )
        dispatched._declared_capabilities = declared
        dispatched._author_dispatch = _AUTHOR_DISPATCH
        return dispatched

    def capability(self, handle: CapabilityHandle[P]) -> P:
        """The bound provider for `handle`, typed as the handle's protocol
        (spec `governed-effects` AC6) -- so `query.capability(LLM)` narrows to
        `LLMClient` with no cast at the call site.

        Fail-closed in three ways (AC8), all before any provider is returned:
        a handle from another `Ontology` -> `ValidationFailed` code
        `UNKNOWN_NAME`; a capability this FUNCTION did not declare ->
        `ValidationFailed` code `UNDECLARED_CAPABILITY`, **even when a
        provider for it happens to be bound** (declaration is the gate, not
        availability); a declared capability with no provider ->
        `PreconditionFailed` code `CAPABILITY_NOT_PROVIDED`.

        The returned object is the author's provider, unwrapped -- the engine
        adds nothing and inspects nothing (spec §8 R1: providers are the same
        trust tier as handlers).

        Accesses ARE recorded now, on the same terms as
        `ActionContext.capability`: `count` counts provider *retrievals*, not
        calls made on the returned object, because the provider is handed back
        unwrapped and the engine never sees what happens to it afterwards. The
        record reaches the audit log through `OntologyClient.call_function`,
        which owns the function audit boundary -- a bare
        `FunctionRegistry.call` still has none, the same way a raw store handle
        bypasses the write gate (see the README's "Declared, not defended").
        """
        provider = resolve_capability(
            handle,
            registry=self._registry,
            declared=self._declared_capabilities,
            providers=self._capability_providers,
            accesses=self._capability_accesses,
            subject="function",
        )
        return cast("P", provider)

    @overload
    def list(
        self,
        obj_type: str,
        where: dict[str, Any] | None = None,
        *,
        limit: None = None,
        after: str | None = None,
        order_by: OrderBy | None = None,
    ) -> list[StoredObject]: ...
    @overload
    def list(
        self,
        obj_type: type[T],
        where: dict[str, Any] | None = None,
        *,
        limit: None = None,
        after: str | None = None,
        order_by: OrderBy | None = None,
    ) -> list[T]: ...
    @overload
    def list(
        self,
        obj_type: str,
        where: dict[str, Any] | None = None,
        *,
        limit: int,
        after: str | None = None,
        order_by: OrderBy | None = None,
    ) -> Page: ...
    @overload
    def list(
        self,
        obj_type: type[T],
        where: dict[str, Any] | None = None,
        *,
        limit: int,
        after: str | None = None,
        order_by: OrderBy | None = None,
    ) -> TypedPage[T]: ...
    def list(
        self,
        obj_type: type[T] | str,
        where: dict[str, Any] | None = None,
        *,
        limit: int | None = None,
        after: str | None = None,
        order_by: OrderBy | None = None,
    ) -> list[T] | list[StoredObject] | TypedPage[T] | Page:
        """Every visible matching row, unbounded, unless `limit` is given.

        This default differs from `OntologyClient.list`'s on purpose, and the
        difference is the point. `DEFAULT_READ_LIMIT` is *the default bound for
        consumer object reads* -- a serving-surface protection against handing
        an MCP client a million rows by accident. A Function is not a consumer
        read: it is author code deriving a value, and a value derived from a
        silently partial selection is simply wrong. Inheriting the consumer
        bound here made `count` and `list` disagree inside one Function --
        `query.count("R")` returned 1200 while `query.list("R").items` held
        1000, so a Function reporting "mean X over N rows" reported a
        self-inconsistent pair with no error (0.6.0 -> 0.8.0 regression; on
        0.6.0 `list` had no `limit` and returned everything).

        The engine's own reductions already read unbounded: `count`,
        `aggregate` and `count_contributors` all go through `Store.read_all`.
        This does not introduce unbounded author reads -- it makes the one
        surface that truncated agree with the three that never did.

        Paging is still available by asking for it: pass `limit=` for a `Page`
        with a `next_cursor`, exactly as on the consumer surface.

        Residual, stated not defended: nothing bounds what a Function RETURNS,
        here or at the MCP boundary, so an author can hand a million rows to a
        caller. That is the author's choice on the same terms as every other
        Function return value -- "Declared, not defended".
        """
        return list_objects(
            self._query,
            self._consumer,
            obj_type,
            where,
            api_name_for=self._api_name_for,
            validate_where_keys=self._validate_where_keys,
            limit=limit,
            after=after,
            order_by=order_by,
        )

    @overload
    def traverse(
        self, link_type: str, from_id: str, *, reverse: bool = False
    ) -> builtins.list[StoredObject]: ...
    @overload
    def traverse(
        self,
        link_cls: LinkHandle[F, T],
        from_obj_or_id: str,
        /,
        *,
        reverse: Literal[False] = False,
    ) -> builtins.list[T]: ...
    @overload
    def traverse(
        self,
        link_cls: LinkHandle[F, T],
        to_obj_or_id: str,
        /,
        *,
        reverse: Literal[True],
    ) -> builtins.list[F]: ...
    @overload
    def traverse(
        self,
        link_cls: LinkHandle[F, T],
        anchor_id: str,
        /,
        *,
        reverse: bool,
    ) -> builtins.list[T] | builtins.list[F]: ...
    def traverse(
        self,
        link_type: str | LinkHandle[F, T],
        from_id: str | None = None,
        *,
        reverse: bool = False,
    ) -> "builtins.list[StoredObject] | builtins.list[T] | builtins.list[F]":
        """String form: `traverse(link_type, from_id)`. Typed form accepts
        the handle-first `traverse(link_cls, from_obj_or_id)` shape used by
        `OntologyClient`. Pass `reverse=True` to traverse from the handle's
        target side and hydrate the linked source-side objects."""
        if isinstance(link_type, LinkHandle):
            if from_id is None:
                raise ValidationFailed(
                    "typed traversal requires a source object or id",
                    code="INVALID_PARAMS",
                )
            return self._traverse_via(
                from_id, link_type, reverse=reverse
            )

        if from_id is None:
            raise ValidationFailed(
                "string-form traversal requires from_id",
                code="INVALID_PARAMS",
            )
        return self._query.traverse(
            self._consumer, link_type, from_id, reverse=reverse
        )

FunctionHandler = Callable[[BoundQuery, dict[str, Any]], Any]


class FunctionRegistry:
    """Binds Python callables to declared `FunctionDef`s and invokes them.

    Constructed against one `OntologyRegistry` so `register`/`function` can
    refuse to bind an undeclared `api_name`; holds no store, no consumer, no
    module-global state (spec §7) -- instance-scoped like every other engine
    layer, so it is safe to share across many `OntologyClient`s bound to the
    same `OntologyDef`.
    """

    def __init__(self, registry: OntologyRegistry) -> None:
        self._registry = registry
        self._handlers: dict[str, FunctionHandler] = {}

    def register(self, api_name: str, fn: FunctionHandler) -> None:
        try:
            self._registry.get_function(api_name)
        except ValidationFailed as exc:
            raise PreconditionFailed(
                f"cannot register handler for undeclared function: {api_name!r}",
                code="FUNCTION_ERROR",
            ) from exc
        if api_name in self._handlers:
            raise PreconditionFailed(
                f"handler already registered for function: {api_name!r}",
                code="FUNCTION_ERROR",
            )
        self._handlers[api_name] = fn

    def function(self, api_name: str) -> Callable[[FunctionHandler], FunctionHandler]:
        """Decorator form of `register`."""

        def _decorate(fn: FunctionHandler) -> FunctionHandler:
            self.register(api_name, fn)
            return fn

        return _decorate

    def call(self, api_name: str, query: BoundQuery, params: dict[str, Any]) -> Any:
        handler = self._handlers.get(api_name)
        if handler is None:
            raise PreconditionFailed(
                f"no handler registered for function: {api_name!r}",
                code="FUNCTION_ERROR",
            )
        function_def = self._registry.get_function(api_name)
        # THE author-provenance mint. `register` already refused to bind
        # `handler` to anything but a declared `FunctionDef` on this
        # registry, so reaching this line means the engine is about to run
        # code the ontology author declared and wrote -- which is the whole
        # of what "author provenance" asserts. Nowhere else in the SDK
        # grants it.
        #
        # Both per-call authorities (the declared-capability set and the
        # provenance grant) are scoped by handing the handler its OWN
        # `BoundQuery` rather than by assigning onto the caller's and
        # restoring in a `finally`. `query` itself is never elevated, so
        # there is no interval during which the object the caller holds
        # carries this function's authority -- which install-and-restore
        # could not promise across a thread or a re-entrant callback.
        return handler(
            query._for_author_dispatch(frozenset(function_def.capabilities)),
            params,
        )
