"""`OntologyClient`: the single facade an app developer or MCP server binds to.

Given one `OntologyDef` (the authored registry + policy + functions) plus a
`store` and a `consumer`, `OntologyClient` builds its own `GuardedQuery` and
`ActionExecutor` (exposed publicly as `client.actions`) and provides:

- **reads** -- `get`/`list`/`count`/`exists`/`traverse`/`aggregate`/`aggregate_by`: thin delegation to a
  `GuardedQuery` bound with this client's `consumer`. Every read goes
  through `GuardedQuery`; nothing here ever touches `Store.read_current`/
  `read_all` directly (spec AC6/AC7).
- **writes** -- `execute` (-> `client.actions`, the governed action
  pipeline: role/scope/precondition/audit) and `ingest`/`ingest_links` (->
  `ontary.ingest`). Ingest is a schema-validated but guard-INDEPENDENT
  bulk-load surface (spec AC9): it deliberately does not go through this
  client's `consumer` role/scope, mirroring the prototype's connector path
  (data *loading*, not a consumer-facing query/action) -- it still never
  bypasses schema validation (unknown type/missing pk/wrong property type
  are rejected per-record).
- **call_function** -- `FunctionRegistry.call`, handed a `BoundQuery` (this
  client's `GuardedQuery` with `consumer` already fixed) so a Function
  handler can only read, and only ever as this client's own consumer (see
  `ontary.functions`).

Legacy (descriptor-authoring) action handlers are registered on
`client.actions` via the internal `_register` seam, NOT on `OntologyDef`
-- see `ontary.ontology`'s docstring for why (handlers close over a
concrete `Store`, which only exists once a `Store` is bound). The old
public `register_handler`/`.handler` surface was removed (AC7 clean
break). Class-authored (`Ontology.action(...)`)
handlers, by contrast, ARE declared on the `Ontology` (spec
`typed-actions.md` §6) and are auto-bound to every runtime built from it --
see `OntologyRuntime` below.

`OntologyRuntime` (spec `typed-actions.md` AC6) holds the shared, consumer-
free machinery for one `(ontology, store)` pair -- ONE `GuardedQuery` + ONE
`ActionExecutor`, with all of the ontology's declared action handlers bound
exactly once at construction. `runtime.for_consumer(consumer)` is a cheap
view: a new `OntologyClient` sharing the runtime's `query`/`actions` by
identity, with only `consumer` swapped -- so many consumers share one
executor/handler table without re-wiring. `Ontology.bind(store)` is the
one typed entry point that builds a runtime with its declared handlers
auto-bound; direct `OntologyClient(ontology, store, consumer)` construction
(kept working, spec AC6) builds a single-use `OntologyRuntime` internally
-- auto-binding therefore lives in EXACTLY ONE code path: `OntologyRuntime.
__init__`.

Every `OntologyClient`/`OntologyRuntime` is fully instance-scoped: no
module-global registry, policy, handler table, or store anywhere in this
file, so two `OntologyDef`s/`Ontology`s (+ stores + clients + runtimes)
coexist in one process without cross-talk (spec §7).
"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from ontary._runtime import default_clock, default_id_factory
from ontary._typed_api import _TypedReadMixin, list_objects
from ontary.actions import ActionError, ActionExecutor, TypedHandler
from ontary.audit import AuditEntry, CapabilityAccessRecord
from ontary.declarations import Declarations, declarations
from ontary.effects import EffectDispatcher
from ontary.errors import ValidationFailed, VisibilityError
from ontary.explain import DecisionTrace, MinNTrace, _TraceCollector
from ontary.functions import BoundQuery
from ontary.ingest import IngestError, IngestReport, bulk_link, bulk_upsert
from ontary.meta import OntologyRegistry
from ontary.model import (
    ActionParams,
    CapabilityHandle,
    EffectHandle,
    LinkHandle,
    OntologyObject,
    _class_stamp,
)
from ontary.ontology import OntologyDef, resolve_definition
from ontary.outbox import (
    DEFAULT_RETRY_POLICY,
    DrainReport,
    OutboxRecord,
    RetryPolicy,
)
from ontary.outbox_drain import drain_effect_outbox
from ontary.query import (
    _UNSET_LIMIT,
    DEFAULT_READ_LIMIT,
    GuardedQuery,
    OrderBy,
    Page,
    TypedPage,
)
from ontary.security import Consumer
from ontary.store import Source, Store, StoredObject

if TYPE_CHECKING:
    from ontary.authoring import Ontology

__all__ = [
    "OntologyClient",
    "OntologyRuntime",
]

T = TypeVar("T", bound=OntologyObject)
F = TypeVar("F", bound=OntologyObject)


def _checked_provider_map(
    provided: Mapping[Any, Any] | None,
    registry: OntologyRegistry,
    what: str,
) -> dict[Any, Any]:
    """Copy a provider/dispatcher map, refusing any key from another `Ontology`.

    Whole-branch review finding (2026-07-26): binding a FOREIGN handle used to be
    accepted silently. The map was copied unchecked, the pre-flight then skipped
    keys whose `registry` was not this one, and the author was told
    `CAPABILITY_NOT_PROVIDED` / `EFFECT_NOT_DISPATCHABLE` -- "you bound no
    provider" -- for a capability they had visibly just bound. Two ontologies
    declaring the same api_name (which spec AC1 explicitly permits) made this easy
    to hit and near-impossible to diagnose from the message.

    AC8 already says a handle from another registry is `UNKNOWN_NAME`; that was
    enforced at ACCESS time (`ctx.capability(foreign)`) but not at BIND time, so
    the check now runs where the mistake is actually made. Fail-closed and early:
    a mis-bound provider is a wiring bug, and the copy is also what keeps a later
    mutation of the caller's dict from mattering (AC5).

    Also refuses a handle of the WRONG KIND (re-review finding, 2026-07-26). A
    `CapabilityDef` and an `EffectTypeDef` live in separate registry namespaces, so
    `capability(X, name="Foo")` and `effect(Y, api_name="Foo")` can BOTH be
    declared -- verified. Both pre-flights then match on `(api_name, registry)`
    only, so a `CapabilityHandle` bound in `effects=` would satisfy an effect
    declaration and be invoked as its dispatcher, and vice versa. That is
    fail-OPEN: the wrong object gets called, rather than anything being refused.
    """
    if provided is None:
        return {}
    # `provided is None`, not `not provided`: a nonempty Mapping may be falsey (a
    # dict subclass overriding __bool__), and `not provided` would skip validation
    # entirely and hand back {} -- reinstating the very misleading
    # "no provider bound" diagnosis this function exists to prevent.
    expected = CapabilityHandle if what == "capability" else EffectHandle
    checked: dict[Any, Any] = {}
    for handle, value in provided.items():
        if not isinstance(handle, expected):
            raise ValidationFailed(
                f"{type(handle).__name__} {getattr(handle, 'api_name', handle)!r} "
                f"cannot be bound in the {what} map -- expected a "
                f"{expected.__name__}",
                code="UNKNOWN_NAME",
            )
        handle_registry = getattr(handle, "registry", None)
        if handle_registry is not registry:
            raise ValidationFailed(
                f"{what} {getattr(handle, 'api_name', handle)!r} was declared on "
                f"a different Ontology -- it cannot be bound as a {what} "
                "provider/dispatcher on this one",
                code="UNKNOWN_NAME",
            )
        checked[handle] = value
    return checked


class OntologyRuntime:
    """Shared, consumer-free machinery for one `(ontology, store)` pair
    (spec `typed-actions.md` §6/AC6): ONE `GuardedQuery` + ONE
    `ActionExecutor`, with every ontology-declared action handler bound
    exactly once, at construction.

    `ontology` may be an `Ontology` (class authoring -- its
    `_action_handlers` are auto-bound) or a plain `OntologyDef`
    (descriptor authoring -- no declared handlers to bind; wire them onto
    `runtime.actions` via the internal `_register` seam, same as today).
    `handlers`, if given explicitly, overrides
    the ontology's own handler table -- used only by `OntologyClient`'s
    direct-construction path below, which always passes `None`/omits it
    and lets this constructor derive the handlers itself.

    Two `OntologyRuntime`s built from the SAME `Ontology` (e.g. two
    different stores, or two calls to `.bind(store)`) never collide:
    each builds its OWN fresh `ActionExecutor` and registers handlers
    into it, so there is no shared, mutable registration table to
    double-register into.
    """

    def __init__(
        self,
        ontology: OntologyDef | Ontology,
        store: Store,
        handlers: Mapping[str, tuple[TypedHandler, type[ActionParams]]] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        capabilities: Mapping[CapabilityHandle[Any], object] | None = None,
        effects: Mapping[EffectHandle[Any], EffectDispatcher] | None = None,
        effect_retry: RetryPolicy = DEFAULT_RETRY_POLICY,
    ) -> None:
        definition = resolve_definition(ontology)
        if handlers is None:
            handlers = definition.action_handlers
        self._definition = definition
        self._store = store
        self._clock = clock if clock is not None else default_clock
        self._id_factory = id_factory if id_factory is not None else default_id_factory
        self._capability_providers = _checked_provider_map(
            capabilities, definition.registry, "capability"
        )
        self._effect_dispatchers = _checked_provider_map(
            effects, definition.registry, "effect"
        )
        self._effect_retry = effect_retry
        self.query = GuardedQuery(store, definition.registry, definition.policy)
        self.actions = ActionExecutor(
            store,
            definition.registry,
            definition.policy,
            retry_policy=effect_retry,
            clock=self._clock,
            id_factory=self._id_factory,
        )
        for api_name, (fn, params_cls) in (handlers or {}).items():
            self.actions._register(api_name, fn, params_cls)

    def explain_read(
        self, consumer: Consumer, obj_type: str, id: str
    ) -> DecisionTrace:
        """Explain one guarded read without changing its decision.

        This operator-only method may distinguish a denied row from a
        nonexistent row. It intentionally lives on ``OntologyRuntime`` and
        is absent from consumer-bound clients and MCP.
        """
        return self._explain_row(consumer, obj_type, id)

    def explain_list(
        self,
        consumer: Consumer,
        obj_type: str,
        where: dict[str, Any] | None = None,
    ) -> list[DecisionTrace]:
        """Return one decision trace per raw row matching ``where``.

        Denied rows are included because this is an operator oracle. Each
        trace carries the same aggregate-relevant min-N outcome for the
        selected visible population; that diagnostic does not add a min-N
        gate to ordinary list reads.
        """
        # Reproduce the ordinary list path's pre-row coherence, field, and
        # operator gates directly so their refusals are byte-identical to a
        # real list call. Do not fetch its redacted rows: explain must also
        # include raw matching rows that the ordinary path hides. The
        # `_require_coherent_scope` gate snapshots `where` once into
        # `disclosure`; pass that disclosure to `_validate_read_fields`, then
        # reuse `disclosure.where` for both the matcher and the min-N
        # selection.
        disclosure = self.query._require_coherent_scope(obj_type, where=where)
        self.query._validate_read_fields(consumer, obj_type, disclosure, None)
        normalized_where = disclosure.where
        where_matcher = self.query._compile_where(obj_type, normalized_where)
        selection_trace = _TraceCollector(
            obj_type, "<selection>", self._definition.policy.min_n
        )
        min_n = self.query._selection_min_n(
            consumer, obj_type, normalized_where, selection_trace
        )

        traces: list[DecisionTrace] = []
        for row in self._store.read_all(obj_type):
            # Explain must use the exact matcher used by ordinary reads.
            # This keeps operator semantics from diverging on this raw-row path.
            if where_matcher is not None and not where_matcher(row.payload):
                continue
            traces.append(
                self._explain_row(
                    consumer,
                    obj_type,
                    row.lineage.object_id,
                    min_n=min_n,
                )
            )
        return traces

    def _explain_row(
        self,
        consumer: Consumer,
        obj_type: str,
        obj_id: str,
        *,
        min_n: MinNTrace | None = None,
    ) -> DecisionTrace:
        collector = _TraceCollector(
            obj_type, obj_id, self._definition.policy.min_n
        )
        try:
            row = self.query.get_object(
                consumer, obj_type, obj_id, trace=collector
            )
        except VisibilityError as exc:
            return collector.build(
                "denied", error_code=exc.code, min_n=min_n
            )
        if row is None:
            return collector.build("not_found", min_n=min_n)
        if collector.redactions:
            return collector.build("redacted", min_n=min_n)
        return collector.build("visible", min_n=min_n)

    def drain_effects(
        self, *, limit: int = 100, now: datetime | None = None
    ) -> DrainReport:
        """Retry effects this runtime's dispatchers can deliver (spec AC4).

        Consumer-free on purpose: an outbox row carries the actor and role of
        the `execute()` that emitted it, so redelivering it is not an act by
        whoever happens to run the drain. `OntologyClient.drain_effects`
        delegates here for exactly that reason.

        Call it from a worker loop, a cron job, or the tail of a request --
        the SDK starts no thread of its own and has no schedule (spec §4).
        `now` is injectable so a test can advance past a backoff without
        sleeping.
        """
        return drain_effect_outbox(
            self._store,
            {
                handle.api_name: (handle.payload_cls, dispatcher)
                for handle, dispatcher in self._effect_dispatchers.items()
            },
            self._effect_retry,
            limit=limit,
            now=now if now is not None else self._clock(),
        )

    def for_consumer(
        self,
        consumer: Consumer,
        *,
        capabilities: Mapping[CapabilityHandle[Any], object] | None = None,
        effects: Mapping[EffectHandle[Any], EffectDispatcher] | None = None,
    ) -> "OntologyClient":
        """Cheap per-consumer view: a new `OntologyClient` sharing this
        runtime's `query`/`actions` BY IDENTITY (spec AC6) -- only
        `consumer` differs between views built from the same runtime."""
        # Overrides are registry-checked here too, not only at bind: a foreign
        # handle passed to `for_consumer` would otherwise slip past the runtime
        # constructor's check and resurface later as a misleading "no provider
        # bound" refusal (see `_checked_provider_map`).
        registry = self._definition.registry
        capability_providers = dict(self._capability_providers)
        capability_providers.update(
            _checked_provider_map(capabilities, registry, "capability")
        )
        effect_dispatchers = dict(self._effect_dispatchers)
        effect_dispatchers.update(
            _checked_provider_map(effects, registry, "effect")
        )
        return OntologyClient._from_runtime(
            self,
            consumer,
            capabilities=capability_providers,
            effects=effect_dispatchers,
        )


class OntologyClient(_TypedReadMixin):
    """Bound to exactly one `(OntologyDef-or-Ontology, Store, Consumer)`
    triple.

    Accepts either a plain `OntologyDef` (descriptor authoring -- no
    ontology-declared handlers exist; wire them onto `client.actions` via
    the internal `_register` seam) or an `Ontology` (class authoring,
    spec AC6): passing the
    latter directly auto-binds every `@ontology.action(...)`-declared
    handler with zero manual wiring, identical to going through
    `ontology.bind(store).for_consumer(consumer)`. Internally this always
    builds a (possibly single-use) `OntologyRuntime` -- see that class's
    docstring; auto-binding lives in EXACTLY ONE place, `OntologyRuntime.
    __init__`.
    """

    _typed_owner = "client"

    def __init__(
        self,
        ontology: OntologyDef | Ontology,
        store: Store,
        consumer: Consumer,
        *,
        capabilities: Mapping[CapabilityHandle[Any], object] | None = None,
        effects: Mapping[EffectHandle[Any], EffectDispatcher] | None = None,
        effect_retry: RetryPolicy = DEFAULT_RETRY_POLICY,
    ) -> None:
        self._init_from_runtime(
            OntologyRuntime(
                ontology,
                store,
                capabilities=capabilities,
                effects=effects,
                effect_retry=effect_retry,
            ),
            consumer,
        )

    @classmethod
    def _from_runtime(
        cls,
        runtime: OntologyRuntime,
        consumer: Consumer,
        *,
        capabilities: Mapping[CapabilityHandle[Any], object] | None = None,
        effects: Mapping[EffectHandle[Any], EffectDispatcher] | None = None,
    ) -> "OntologyClient":
        """Private alternate constructor: a client VIEW sharing an
        existing runtime's `query`/`actions` by identity -- used by
        `OntologyRuntime.for_consumer` only."""
        self = cls.__new__(cls)
        self._init_from_runtime(
            runtime,
            consumer,
            capabilities=capabilities,
            effects=effects,
        )
        return self

    def _init_from_runtime(
        self,
        runtime: OntologyRuntime,
        consumer: Consumer,
        *,
        capabilities: Mapping[CapabilityHandle[Any], object] | None = None,
        effects: Mapping[EffectHandle[Any], EffectDispatcher] | None = None,
    ) -> None:
        self._ontology = runtime._definition
        self._registry = runtime._definition.registry
        self._store = runtime._store
        self._consumer = consumer
        self._clock = runtime._clock
        self._id_factory = runtime._id_factory
        # Validated HERE as well, not only by the callers: this is the single
        # choke point every client-construction path funnels through, so a future
        # caller of `_from_runtime` cannot reintroduce an unchecked map (re-review
        # finding, 2026-07-26). Maps inherited from the runtime were already
        # checked at its construction, so re-checking them is a cheap no-op.
        registry = runtime._definition.registry
        self._capability_providers = _checked_provider_map(
            runtime._capability_providers if capabilities is None else capabilities,
            registry,
            "capability",
        )
        self._effect_dispatchers = _checked_provider_map(
            runtime._effect_dispatchers if effects is None else effects,
            registry,
            "effect",
        )
        self._query = runtime.query
        self.actions = runtime.actions
        # Kept so `drain_effects` can rebuild the same runtime-level drain with
        # THIS view's dispatcher overrides (`for_consumer(effects=...)`) rather
        # than the runtime's originals.
        self._effect_retry = runtime._effect_retry

    # -- reads --------------------------------------------------------

    @overload
    def list(
        self,
        obj_type: type[T],
        where: dict[str, Any] | None = None,
        *,
        limit: None,
        after: str | None = None, order_by: OrderBy | None = None,
    ) -> builtins.list[T]: ...
    @overload
    def list(
        self,
        obj_type: type[T],
        where: dict[str, Any] | None = None,
        *,
        limit: int = DEFAULT_READ_LIMIT,
        after: str | None = None, order_by: OrderBy | None = None,
    ) -> TypedPage[T]: ...
    @overload
    def list(
        self,
        obj_type: str,
        where: dict[str, Any] | None = None,
        *,
        limit: None,
        after: str | None = None, order_by: OrderBy | None = None,
    ) -> builtins.list[StoredObject]: ...
    @overload
    def list(
        self,
        obj_type: str,
        where: dict[str, Any] | None = None,
        *,
        limit: int = DEFAULT_READ_LIMIT,
        after: str | None = None, order_by: OrderBy | None = None,
    ) -> Page: ...
    def list(
        self,
        obj_type: type[T] | str,
        where: dict[str, Any] | None = None,
        *,
        limit: int | None | object = _UNSET_LIMIT,
        after: str | None = None, order_by: OrderBy | None = None,
    ) -> builtins.list[T] | builtins.list[StoredObject] | TypedPage[T] | Page:
        """Read through the shared guarded list path.
        Omitting limit applies DEFAULT_READ_LIMIT and returns a Page (or a
        TypedPage for a typed object). Passing limit=None explicitly selects
        the unbounded list form. order_by and after are forwarded unchanged,
        so field gates, ordering, cursor validation, and page hydration stay
        in one implementation.
        """
        return list_objects(
            self._query,
            self._consumer,
            obj_type,
            where,
            api_name_for=self._api_name_for,
            validate_where_keys=self._validate_where_keys,
            limit=limit,
            after=after, order_by=order_by,
        )

    @overload
    def traverse(
        self, obj_type: str, link: str, from_id: str, *, reverse: bool = False
    ) -> builtins.list[StoredObject]: ...
    @overload
    def traverse(
        self, link_cls: LinkHandle[F, T], from_obj_or_id: F | str, /, *, reverse: Literal[False] = False
    ) -> builtins.list[T]: ...
    @overload
    def traverse(
        self, link_cls: LinkHandle[F, T], to_obj_or_id: T | str, /, *, reverse: Literal[True]
    ) -> builtins.list[F]: ...
    @overload
    def traverse(self, link_cls: LinkHandle[F, T], anchor_obj_or_id: F | T | str, /, *, reverse: bool) -> builtins.list[T] | builtins.list[F]: ...
    def traverse(
        self,
        obj_type: str | LinkHandle[F, T],
        link: str | F | T,
        from_id: str | None = None,
        *,
        reverse: bool = False,
    ) -> builtins.list[StoredObject] | builtins.list[T] | builtins.list[F]:
        """Traverse a declared link.

        The string form is ``traverse(obj_type, link, from_id)``. The typed
        form is ``traverse(link_cls, from_obj_or_id)``; a typed anchor may be
        either the source/target object's model instance or its string id.

        String form validates the named anchor type before delegation to
        `GuardedQuery`, which performs the guarded traversal. Typed form
        validates the handle's ontology and hydrates the linked class;
        `reverse=True` uses the target as anchor and the source as result.
        """
        if isinstance(obj_type, LinkHandle):
            anchor_id = self._traverse_anchor_id(obj_type, link, reverse=reverse)
            return self._traverse_via(anchor_id, obj_type, reverse=reverse)

        if not isinstance(link, str) or from_id is None:
            raise ValidationFailed(
                "string-form traverse requires obj_type, link, and from_id",
                code="INVALID_PARAMS",
            )
        try:
            link_def = self._ontology.registry.get_link_type(link)
        except ValidationFailed as exc:
            raise ValidationFailed(str(exc), code="UNKNOWN_NAME") from exc
        expected_anchor_type = link_def.to_type if reverse else link_def.from_type
        if expected_anchor_type != obj_type:
            direction = "to" if reverse else "from"
            raise ValidationFailed(
                f"link {link!r} runs {direction} {expected_anchor_type!r}, "
                f"not {obj_type!r}",
                code="UNKNOWN_NAME",
            )
        return self._query.traverse(self._consumer, link, from_id, reverse=reverse)

    def _traverse_anchor_id(self, link_cls: LinkHandle[F, T], anchor_obj_or_id: F | T | str, *, reverse: bool = False) -> str:
        if isinstance(anchor_obj_or_id, str):
            return anchor_obj_or_id
        anchor_cls = link_cls.to_cls if reverse else link_cls.from_cls
        self._api_name_for(anchor_cls)
        if not isinstance(anchor_obj_or_id, anchor_cls):
            raise ValidationFailed(
                f"typed traversal anchor must be {anchor_cls.__name__} or a string id",
                code="INVALID_PARAMS",
            )
        object_def = self._ontology.registry.get_object_type(self._api_name_for(anchor_cls))
        anchor_id = getattr(anchor_obj_or_id, object_def.primary_key, None)
        if not isinstance(anchor_id, str):
            raise ValidationFailed(
                f"typed traversal anchor {anchor_cls.__name__} has no string id "
                f"for {object_def.primary_key!r}",
                code="INVALID_PARAMS",
            )
        return anchor_id

    # Both directions share GuardedQuery's identity and visibility gates;
    # this façade only resolves the typed anchor and hydrates the resulting
    # direction-specific class.
    # -- writes ---------------------------------------------------------

    def _api_name_for_action_params(self, params: ActionParams) -> str:
        """Resolves a typed `ActionParams` instance to its registered
        `api_name` (stamped by `Ontology.action()`), using the same
        `__dict__`-stamp + registry-identity discipline as
        `_api_name_for` -- fail-closed `ActionError(code="UNKNOWN_ACTION")`
        for an undecorated params class, or one declared on a different
        `Ontology`."""
        cls = type(params)
        name, registry = _class_stamp(cls)
        if name is None or registry is None:
            raise ActionError(
                f"{cls.__name__!r} is not decorated with @ontology.action("
                "...) -- it has no registered action api_name",
                code="UNKNOWN_ACTION",
            )
        if registry is not self._ontology.registry:
            raise ActionError(
                f"{cls.__name__!r} (api_name {name!r}) was declared on a "
                "different Ontology -- it is not part of this client's "
                "ontology",
                code="UNKNOWN_ACTION",
            )
        return name

    @overload
    def execute(self, action: ActionParams) -> dict[str, Any]: ...
    @overload
    def execute(self, *, params: ActionParams) -> dict[str, Any]: ...
    @overload
    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]: ...
    def execute(
        self,
        action: str | ActionParams | None = None,
        params: dict[str, Any] | ActionParams | None = None,
    ) -> dict[str, Any]:
        if isinstance(action, ActionParams):
            if params is not None:
                raise ValidationFailed(
                    "typed-form execute accepts exactly one ActionParams "
                    "argument",
                    code="INVALID_PARAMS",
                )
            name = self._api_name_for_action_params(action)
            return self.actions.execute(
                self._consumer,
                name,
                action,
                capability_providers=self._capability_providers,
                effect_dispatchers=self._effect_dispatchers,
            )
        if action is None and isinstance(params, ActionParams):
            name = self._api_name_for_action_params(params)
            return self.actions.execute(
                self._consumer,
                name,
                params,
                capability_providers=self._capability_providers,
                effect_dispatchers=self._effect_dispatchers,
            )
        if not isinstance(action, str) or not isinstance(params, dict):
            raise ValidationFailed(
                "string-form execute requires an action name and a params dict",
                code="INVALID_PARAMS",
            )
        return self.actions.execute(
            self._consumer,
            action,
            params,
            capability_providers=self._capability_providers,
            effect_dispatchers=self._effect_dispatchers,
        )

    def drain_effects(
        self, *, limit: int = 100, now: datetime | None = None
    ) -> DrainReport:
        """Retry due effects using THIS client's bound dispatchers (spec AC4).

        Identical to `OntologyRuntime.drain_effects` except that a client built
        by `for_consumer(effects=...)` drains with its own dispatcher overrides.
        The drain itself is not scoped to `self._consumer`: a row is delivered
        on behalf of the actor who emitted it, whose identity the row carries
        and hands to the dispatcher as `EffectMeta.actor_id`. Draining is
        machinery, not an act by the consumer who happens to trigger it -- so
        it is deliberately NOT a governed, scope-checked surface, and a caller
        who should not be able to trigger outward sends should not be given a
        client with dispatchers bound.
        """
        return drain_effect_outbox(
            self._store,
            {
                handle.api_name: (handle.payload_cls, dispatcher)
                for handle, dispatcher in self._effect_dispatchers.items()
            },
            self._effect_retry,
            limit=limit,
            now=now if now is not None else self._clock(),
        )

    def outbox(self) -> builtins.list[OutboxRecord]:
        """Every outbox row, oldest emission first -- pending, delivered, and
        failed alike (spec AC2).

        An administrative read, like `audit_entries`: unscoped, unredacted, and
        not a consumer-facing surface. It answers "was it delivered, and if
        not, why" without making the caller reach for the store."""
        return self._store.outbox_entries()

    def ingest(
        self,
        obj_type: str,
        records: builtins.list[dict[str, Any]],
        source: Source,
        *,
        on_error: Literal["raise", "report"] = "raise",
    ) -> IngestReport:
        if on_error not in ("raise", "report"):
            raise ValidationFailed(
                f"on_error must be 'raise' or 'report', got {on_error!r}",
                code="INVALID_PARAMS",
            )
        report = bulk_upsert(
            self._store, self._ontology.registry, obj_type, records, source
        )
        if on_error == "raise" and not report.ok:
            raise IngestError(report=report)
        return report

    def ingest_links(
        self,
        link_api_name: str,
        pairs: builtins.list[tuple[str, str]],
        source: Source,
        *,
        on_error: Literal["raise", "report"] = "raise",
    ) -> IngestReport:
        if on_error not in ("raise", "report"):
            raise ValidationFailed(
                f"on_error must be 'raise' or 'report', got {on_error!r}",
                code="INVALID_PARAMS",
            )
        report = bulk_link(
            self._store, self._ontology.registry, link_api_name, pairs, source
        )
        if on_error == "raise" and not report.ok:
            raise IngestError(report=report)
        return report

    # -- functions --------------------------------------------------------

    def call_function(self, api_name: str, params: dict[str, Any]) -> Any:
        """Call a declared Function and return its derived value.

        This is the function **audit boundary**. A function that
        `FunctionDef.audited` says to record appends one entry per call --
        `kind="function"`, the same `invocation_id` discipline actions use, and
        the capability accesses the handler made. Functions never write and
        cannot emit effects, so `writes`/`effects` are always empty on those
        entries; the interesting column is `capability_accesses`, which is what
        answers "what did the AI reach for?".

        Auditing is conditional by default -- see `FunctionDef.audited` -- so an
        ontology of cheap read-only functions does not pay a write per call.
        The error path is audited too: a handler that reached outside and then
        raised has already had its effect on the world, and an audit log that
        only recorded successes would say otherwise.

        **One condition is not the declaration's to make.** A call that
        actually released a hidden field through the AC10 contributor
        exemption (`GuardedQuery._aggregate`) appends an entry whatever
        `FunctionDef.audited` says, `audit=False` included. `audited` answers
        "can this function reach outside the process?"; that question is now
        settled -- author provenance is minted at dispatch (see
        `functions.BoundQuery`) -- but being unable to reach outside was never
        the same as being unable to hand a consumer an individual-bearing
        number for a field they cannot read. The trace follows the RELEASE,
        not the declaration: a capability-less function that releases nothing
        still costs no write, and declaring `contributor_rules` does not bill
        every function in the ontology for one.

        A release that min-N then refuses is not a release and appends
        nothing; two reasons to audit one call still append one entry.
        """
        try:
            function_def = self._ontology.registry.get_function(api_name)
        except ValidationFailed:
            # An unregistered name must fail exactly as it did before this
            # audit boundary existed: `FunctionRegistry.call` owns that error
            # (a `PreconditionFailed` with code `FUNCTION_ERROR`), and looking the def up here first
            # must not replace it with a bare `KeyError` on the way past.
            return self._ontology.functions.call(
                api_name,
                BoundQuery(
                    self._query,
                    self._consumer,
                    self._ontology.registry,
                    capability_providers=self._capability_providers,
                ),
                params,
            )
        audited = function_def.audited
        accesses: builtins.list[CapabilityAccessRecord] | None = [] if audited else None
        # Always a real list, even for a function this ontology declared as
        # unaudited: it is what the guarded read layer appends to when the
        # AC10 exemption releases a hidden field, and that release is audited
        # on its own terms below.
        disclosures: builtins.list[tuple[str, str]] = []

        bound = BoundQuery(
            self._query,
            self._consumer,
            self._ontology.registry,
            capability_providers=self._capability_providers,
            capability_accesses=accesses,
            disclosures=disclosures,
        )
        invocation_id = self._id_factory()
        try:
            result = self._ontology.functions.call(api_name, bound, params)
        except Exception:
            if audited or disclosures:
                self._append_function_audit(
                    api_name, params, "error", accesses or [], invocation_id
                )
            raise
        if audited or disclosures:
            self._append_function_audit(
                api_name, params, "ok", accesses or [], invocation_id
            )
        return result

    def _append_function_audit(
        self,
        api_name: str,
        params: dict[str, Any],
        outcome: str,
        accesses: builtins.list[CapabilityAccessRecord],
        invocation_id: str,
    ) -> None:
        """Append one `kind="function"` entry.

        Relies on `Store.append_audit`'s own never-raise contract: losing the
        audit write must not turn a successful read into a failed one."""
        self._store.append_audit(
            AuditEntry(
                kind="function",
                invocation_id=invocation_id,
                actor=self._consumer.actor_id,
                role=self._consumer.role,
                principal=self._consumer.principal,
                action=api_name,
                # A function has no target object type. Empty rather than a
                # sentinel like "Function": `target_type` names a declared
                # object type everywhere else, and a magic string there could
                # collide with a real type of that name. `kind` is the
                # discriminator; this field is simply not applicable.
                target_type="",
                target_id=None,
                params=params,
                outcome=outcome,
                ts=self._clock(),
                # The reason this boundary exists: what the handler reached
                # for. Functions never write and cannot emit, so `writes` and
                # `effects` stay empty by construction.
                capability_accesses=accesses,
            )
        )

    # -- declared contracts -----------------------------------------------

    @property
    def declarations(self) -> Declarations:
        """This runtime's declared answers (spec AC10) -- authority model,
        write-back/re-ingest/visibility/transaction-ownership/idempotency
        stance, audit scope, and this ontology's own `min_n`."""
        return declarations(self._ontology)
