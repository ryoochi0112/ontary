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
from typing import Any, TypeVar, cast, overload

from ontary._typed_api import _TypedReadMixin, list_objects, resolve_capability
from ontary.audit import CapabilityAccessRecord
from ontary.errors import PreconditionFailed, ValidationFailed
from ontary.meta import OntologyRegistry
from ontary.model import (
    CapabilityHandle,
    LinkHandle,
    OntologyObject,
)
from ontary.query import GuardedQuery
from ontary.security import Consumer
from ontary.store import StoredObject

T = TypeVar("T", bound="OntologyObject")
F = TypeVar("F", bound="OntologyObject")
P = TypeVar("P")


class BoundQuery(_TypedReadMixin):
    """A `GuardedQuery` with its `consumer` fixed -- the only object a
    registered Function handler ever receives (see module docstring).

    `registry` is optional (defaults to `None`) so a directly-constructed
    `BoundQuery(query, consumer)` -- as used by dedicated unit tests that
    only exercise the string-form surface -- keeps working unchanged;
    `OntologyClient.call_function` passes its ontology's registry so the
    typed overloads can resolve classes/`LinkHandle`s. Without a bound
    registry, a typed call's class-identity check can never match (there is
    nothing to match against) and fails closed with `ValidationFailed` code
    `UNKNOWN_NAME`, same as
    a class registered on a different ontology.
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
    ) -> None:
        self._query = query
        self._consumer = consumer
        self._registry = registry
        self._declared_capabilities: frozenset[str] = frozenset()
        # Capability providers ONLY -- deliberately no effect dispatchers here.
        # A Function cannot declare effects (spec `governed-effects` AC4:
        # `@ontology.function(effects=...)` is refused outright), so carrying
        # dispatchers it can never use would be dead surface inviting exactly the
        # wiring AC4 forbids. This withholds an outward-write PATH; it is not a
        # sandbox -- see the module docstring and AC4's scope note.
        self._capability_providers = dict(capability_providers or {})
        self._capability_accesses = capability_accesses

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
    def list(self, obj_type: str, where: dict[str, Any] | None = None) -> list[StoredObject]: ...
    @overload
    def list(self, obj_type: type[T], where: dict[str, Any] | None = None) -> list[T]: ...
    def list(
        self, obj_type: type[T] | str, where: dict[str, Any] | None = None
    ) -> list[T] | list[StoredObject]:
        return list_objects(
            self._query,
            self._consumer,
            obj_type,
            where,
            api_name_for=self._api_name_for,
            validate_where_keys=self._validate_where_keys,
            limit=None,
            after=None,
        )

    @overload
    def traverse(self, link_type: str, from_id: str) -> builtins.list[StoredObject]: ...
    @overload
    def traverse(
        self, link_cls: LinkHandle[F, T], from_obj_or_id: str, /
    ) -> builtins.list[T]: ...
    @overload
    def traverse(self, from_id: str, /, *, via: "LinkHandle[F, T]") -> builtins.list[T]: ...
    def traverse(
        self,
        link_type: str | LinkHandle[F, T],
        from_id: str | None = None,
        *,
        via: "LinkHandle[Any, Any] | None" = None,
    ) -> "builtins.list[StoredObject] | builtins.list[T]":
        """String form: `traverse(link_type, from_id)`. Typed form accepts
        the handle-first `traverse(link_cls, from_obj_or_id)` shape used by
        `OntologyClient`, while retaining the older keyword spelling for
        function bodies already using `traverse(from_id, via=handle)`."""
        if isinstance(link_type, LinkHandle):
            if from_id is None:
                raise ValidationFailed(
                    "typed traversal requires a source object or id",
                    code="INVALID_PARAMS",
                )
            return self._traverse_via(from_id, link_type)
        if via is not None:
            return self._traverse_via(link_type, via)

        if from_id is None:
            raise ValidationFailed(
                "string-form traversal requires from_id",
                code="INVALID_PARAMS",
            )
        return self._query.traverse(self._consumer, link_type, from_id)

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
        # Scope the declared-capability set to THIS call and restore it after.
        # The set has to arrive here rather than at construction, because a
        # `BoundQuery` is built before the callee is known; but leaving it
        # assigned would mean the query object keeps this function's authority
        # after the call returns, so a later `capability()` on the same object
        # -- outside any call -- would resolve against whichever function ran
        # last. `OntologyClient.call_function` builds a fresh `BoundQuery` per
        # call today, so nothing leaks in practice; the try/finally is what
        # keeps that true if a caller ever reuses one.
        previous = query._declared_capabilities
        query._declared_capabilities = frozenset(function_def.capabilities)
        try:
            return handler(query, params)
        finally:
            query._declared_capabilities = previous
