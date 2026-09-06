"""Generalized action executor (Layer 4 runtime), domain-agnostic.

Pipeline: registered? -> role permission -> declared-parameter scope checks
-> handler preconditions + transactional side effects -> audit. Every
attempt (denied/error/ok) is audited, append-only, via `ObjectStore`.

Generalizes `dso.actions.ActionExecutor`: scope enforcement here is driven
entirely by `ActionParameterDef.refers_to` / `.scope_semantics` declared on
each `ActionTypeDef` (see `ontary.meta`) plus the ontology's own
`ScopePolicy` -- the engine carries zero hardcoded parameter or object-type
names (compare the prototype's `_TARGET_PARAM_TYPES` /
`_EXPLICIT_SCOPE_PARAMS` module constants).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, NoReturn, TypeVar, cast

from pydantic import BaseModel, ValidationError

from ontary._runtime import default_clock, default_id_factory
from ontary._typed_api import resolve_capability
from ontary.audit import CapabilityAccessRecord, EffectRecord
from ontary.effects import EffectDispatcher, EffectPayload
from ontary.errors import (
    ConflictError,
    InternalError,
    Kind,
    PermissionDenied,
    PreconditionFailed,
    ValidationFailed,
)
from ontary.meta import ActionParameterDef, ActionTypeDef, OntologyRegistry
from ontary.model import CapabilityHandle, EffectHandle
from ontary.outbox import (
    DEFAULT_RETRY_POLICY,
    OutboxRecord,
    RetryPolicy,
)
from ontary.outbox_drain import _deliver_effect
from ontary.scope import ScopePolicy, resolve_owning_scope
from ontary.security import Consumer, covers_scope
from ontary.store import AuditEntry, Source, Store, StoredObject
from ontary.typesys import validate_scalar

TypedHandler = Callable[["ActionContext", BaseModel], dict[str, Any]]
P = TypeVar("P")


class ActionError(PreconditionFailed):
    """An action precondition failed."""

    kind: Kind = "precondition"


def _source(action_name: str) -> Source:
    return Source(source_system=f"action:{action_name}")


def _json_payload(api_name: str, payload: EffectPayload) -> dict[str, Any]:
    """A JSON-safe dict for one emitted payload, or a `ValidationFailed` with
    code `EFFECT_NOT_SERIALIZABLE`.

    `mode="json"` rather than a plain `model_dump()`: an outbox row is
    persisted and re-read, so `datetime`, `UUID`, `Decimal`, enum and nested
    model values must already be in their JSON spelling here -- the same
    normalization `execute()` applies to typed params. A drain rehydrates the
    declared payload class from this dict, which turns those values back into
    the types the dispatcher's signature promises.

    Pydantic emits a warning and falls back to `str()` for a value it cannot
    encode; `json.dumps` is then run to catch anything that survived that,
    since a row the store cannot write would otherwise fail deep inside the
    store with an uncoded error naming neither the effect nor the action.
    """
    try:
        dumped = payload.model_dump(mode="json")
        json.dumps(dumped)
    except Exception as exc:
        raise ValidationFailed(
            f"effect {api_name!r} emitted a payload that cannot be JSON-encoded "
            f"for the durable outbox ({type(exc).__name__}: {exc}); the action "
            "was rolled back and nothing was dispatched",
            code="EFFECT_NOT_SERIALIZABLE",
        ) from exc
    return dumped


class ActionContext:
    """Per-call handle a typed action handler receives instead of closing
    over the store (spec §6/AC3).

    Constructed by `ActionExecutor.execute` inside the transaction +
    write-capture block, wrapping `(store, source, consumer)`. Writes are
    auto-stamped with the action's own `Source` (`action:<api_name>`) --
    the handler cannot supply a different one. The store handle itself is
    private: a handler has no way to bypass this surface to reach the raw
    store or the guarded query layer.
    """

    __slots__ = (
        "_store",
        "_source",
        "_consumer",
        "_registry",
        "_declared_capabilities",
        "_capability_providers",
        "_capability_accesses",
        "_declared_effects",
        "_emitted_effects",
    )

    def __init__(
        self,
        store: Store,
        source: Source,
        consumer: Consumer,
        *,
        registry: OntologyRegistry | None = None,
        declared_capabilities: frozenset[str] = frozenset(),
        capability_providers: Mapping[CapabilityHandle[Any], object] | None = None,
        capability_accesses: list[CapabilityAccessRecord] | None = None,
        declared_effects: frozenset[str] = frozenset(),
        emitted_effects: list[tuple[str, EffectPayload]] | None = None,
    ) -> None:
        self._store = store
        self._source = source
        self._consumer = consumer
        self._registry = registry
        self._declared_capabilities = declared_capabilities
        self._capability_providers = dict(capability_providers or {})
        self._capability_accesses = (
            capability_accesses if capability_accesses is not None else []
        )
        self._declared_effects = declared_effects
        self._emitted_effects = emitted_effects if emitted_effects is not None else []

    @property
    def consumer(self) -> Consumer:
        return self._consumer

    def capability(self, handle: CapabilityHandle[P]) -> P:
        """The bound provider for `handle`, typed as the handle's protocol
        (spec `governed-effects` AC6) -- `ctx.capability(LLM)` narrows to
        `LLMClient` with no cast at the call site.

        Fail-closed in three ways (AC8): a handle from another `Ontology` ->
        `ValidationFailed` code `UNKNOWN_NAME`; a capability this ACTION did
        not declare -> `ValidationFailed` code `UNDECLARED_CAPABILITY`, **even when a provider for it happens to be
        bound** (declaration is the gate, not availability); a declared
        capability with no provider -> `PreconditionFailed` code
        `CAPABILITY_NOT_PROVIDED` (normally caught
        pre-flight by `ActionExecutor.execute` before the transaction opens --
        the check here also covers a directly-constructed context).

        Every successful access is recorded as a `CapabilityAccessRecord` on the
        list the executor created BEFORE its `try` block, so the record survives
        onto the ERROR audit entry after a rollback: an action that called an
        LLM and then failed a precondition must not be audited as though nothing
        happened. An access that RAISES does not increment -- nothing went
        outside. Counts are ACCESSES, not outside calls (see
        `audit.CapabilityAccessRecord`): this returns the provider *object*, and
        the call happens afterwards on that object.

        The provider is returned unwrapped -- the engine adds nothing and
        inspects nothing (spec §8 R1: providers are the same trust tier as
        handlers).

        **Cost to be aware of (spec §5 Q5 / §8 R5):** a handler calls its
        capability INSIDE the engine-owned store transaction, so a slow outside
        call holds the SQLite write lock for its whole duration. That is allowed
        rather than forbidden -- a handler that must decide *based on* an outside
        read has nowhere else to do it -- but a long call here blocks every other
        writer. DSO's own LLM use is entirely in Functions, which have no
        transaction.
        """
        provider = resolve_capability(
            handle,
            registry=self._registry,
            declared=self._declared_capabilities,
            providers=self._capability_providers,
            accesses=self._capability_accesses,
            subject="action",
        )
        return cast("P", provider)

    def emit(self, payload: EffectPayload) -> None:
        """Record one declared outside-write intent for post-commit dispatch.

        Emission is deliberately data-only: it performs no I/O and invokes no
        dispatcher. The payload is deep-copied at this boundary because
        `EffectPayload` freezing is shallow; later mutation of a nested list or
        dict on the handler's instance must not rewrite committed intent.

        The payload class must be declared on this executor's ontology and the
        current action must name its effect. Refusal happens here, inside the
        engine-owned transaction, so any writes the handler made first roll
        back with a `ValidationFailed` carrying `UNDECLARED_EFFECT`.
        """
        payload_cls = type(payload)
        api_name = payload_cls.__dict__.get("_ontary_api_name")
        registry = payload_cls.__dict__.get("_ontary_registry")
        if (
            registry is not self._registry
            or not isinstance(api_name, str)
            or api_name not in self._declared_effects
        ):
            raise ValidationFailed(
                f"action did not declare effect payload {payload_cls.__name__!r}",
                code="UNDECLARED_EFFECT",
            )
        self._emitted_effects.append((api_name, payload.model_copy(deep=True)))

    def insert(self, obj_type: str, payload: dict[str, Any]) -> str:
        """Insert a new object, source-stamped with this action's `Source`."""
        return self._store.insert(obj_type, payload, self._source)

    def update(self, obj_type: str, obj_id: str, changes: dict[str, Any]) -> None:
        """Update `(obj_type, obj_id)`, source-stamped with this action's
        `Source`."""
        self._store.update(obj_type, obj_id, changes, self._source)

    def create_link(self, link_api_name: str, from_id: str, to_id: str) -> None:
        """Create a link between two existing objects."""
        self._store.create_link(link_api_name, from_id, to_id)

    def read_current(self, obj_type: str, obj_id: str) -> StoredObject | None:
        """Trusted raw (unredacted, unscoped) read of one current row.

        This is not a guarded consumer read: an action handler is trusted
        ontology-author code running inside the engine-owned transaction.
        """
        return self._store.read_current(obj_type, obj_id)

    def read_all(self, obj_type: str) -> list[StoredObject]:
        """Trusted raw (unredacted, unscoped) enumeration of current rows.

        This deliberately matches ``read_current``'s handler trust tier rather
        than the guarded consumer query layer. A scope- or sensitivity-filtered
        enumeration could hide an existing row from an id allocator and cause
        it to reuse that row's id -- the opposite of why handlers need this
        operation.
        """
        return self._store.read_all(obj_type)

    def links_from(self, link_api_name: str, from_id: str) -> list[str]:
        """IDs linked FROM `from_id` via `link_api_name` (same trusted
        tier as `read_current`)."""
        return self._store.links_from(link_api_name, from_id)

    def links_to(self, link_api_name: str, to_id: str) -> list[str]:
        """IDs linked TO `to_id` via `link_api_name` (same trusted tier as
        `read_current`)."""
        return self._store.links_to(link_api_name, to_id)


class ActionExecutor:
    """Executes governed Actions against a declared `OntologyRegistry`.

    `_register` (private -- see its docstring) binds a Python callable to a
    declared `ActionTypeDef.api_name`; `execute` runs the platform pipeline.
    Nothing here knows the name of any specific action, parameter, or
    object type -- all of that is read off the registry's
    `ActionParameterDef`s and the supplied `ScopePolicy` at call time.
    """

    def __init__(
        self,
        store: Store,
        registry: OntologyRegistry,
        policy: ScopePolicy,
        *,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._policy = policy
        # Keyword-only with a default so every existing construction site
        # (including the ones in tests) keeps working: the outbox is on by
        # default, not something a caller has to opt into.
        self._retry_policy = retry_policy
        self._clock = clock if clock is not None else default_clock
        self._id_factory = id_factory if id_factory is not None else default_id_factory
        self._handlers: dict[
            str, tuple[Callable[..., dict[str, Any]], type[BaseModel] | None]
        ] = {}

    def _register(
        self,
        api_name: str,
        fn: Callable[..., dict[str, Any]],
        params_cls: type[BaseModel] | None = None,
    ) -> None:
        """Register `fn` for `api_name`, optionally paired with a typed
        params class (`fn` then receives `(ActionContext, params_cls
        instance)` instead of the legacy `(consumer, params dict)`).
        Private: the only registration entry point (AC7 removed the old
        public `register_handler`/`.handler` surface) -- `Ontology.action()`
        and `OntologyRuntime` call this directly; a legacy
        `(consumer, dict) -> dict[str, Any]` handler still registers fine
        by passing `params_cls=None`."""
        try:
            self._registry.get_action_type(api_name)
        except ValidationFailed as exc:
            raise ActionError(
                f"cannot register handler for unregistered action: {api_name!r}",
                code="UNKNOWN_ACTION",
            ) from exc
        if api_name in self._handlers:
            raise ActionError(
                f"handler already registered for action: {api_name!r}",
                code="PRECONDITION_FAILED",
            )
        self._handlers[api_name] = (fn, params_cls)

    def _audit_entry(
        self,
        consumer: Consumer,
        action_name: str,
        target_type: str,
        params: dict[str, Any],
        *,
        invocation_id: str | None,
        outcome: str,
        target_id: str | None = None,
        **extra: Any,
    ) -> AuditEntry:
        """THE one literal `AuditEntry(...)` construction in the executor
        (C4 of the staged refactor) -- every denied/error/pending/follow-up
        entry goes through here, so the consumer -> actor/role/principal
        stamping cannot drift between sites (and the audit-principal AST
        guard has one site to verify instead of seven). `extra` carries the
        per-site fields (`ts`, `writes`, `effects`, `capability_accesses`)."""
        if "ts" not in extra:
            extra["ts"] = self._clock()
        return AuditEntry(
            invocation_id=invocation_id,
            actor=consumer.actor_id,
            role=consumer.role,
            principal=consumer.principal,
            action=action_name,
            target_type=target_type,
            target_id=target_id,
            params=params,
            outcome=outcome,
            **extra,
        )

    def _resolve_handler(
        self, action_name: str
    ) -> tuple[ActionTypeDef, Callable[..., dict[str, Any]], type[BaseModel] | None]:
        """Declared-and-handled lookup: `UNKNOWN_ACTION` for either gap."""
        try:
            action_def = self._registry.get_action_type(action_name)
        except ValidationFailed as exc:
            raise ActionError(
                f"unregistered action: {action_name!r}", code="UNKNOWN_ACTION"
            ) from exc

        entry = self._handlers.get(action_name)
        if entry is None:
            raise ActionError(
                f"no handler registered for action: {action_name!r}",
                code="UNKNOWN_ACTION",
            )
        fn, params_cls = entry
        return action_def, fn, params_cls

    def _coerce_typed_params(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params: dict[str, Any] | BaseModel,
        params_dict: dict[str, Any],
        params_cls: type[BaseModel] | None,
        invocation_id: str,
    ) -> BaseModel | None:
        """Typed-invocation path (spec §6/AC5): a params class turns the
        already-validated dict into a model instance BEFORE the
        transaction, so a `model_validate` failure is audited/raised
        exactly like the declared-shape validation failures before it --
        never inside the txn/capture block, never reaching the handler."""
        if params_cls is None:
            return None
        if isinstance(params, params_cls):
            return params
        try:
            return params_cls.model_validate(params_dict)
        except ValidationError as exc:
            self._error(
                consumer,
                action_name,
                action_def,
                params_dict,
                f"{action_name!r}: params failed validation: {exc}",
                invocation_id,
            )

    def _preflight_capabilities(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params_dict: dict[str, Any],
        invocation_id: str,
        capability_providers: Mapping[CapabilityHandle[Any], object] | None,
    ) -> dict[CapabilityHandle[Any], object]:
        """Every declared capability must have a provider bound BEFORE the
        transaction opens; a gap is audited (outcome "error") and raised."""
        providers = dict(capability_providers or {})
        for capability_name in action_def.capabilities:
            if not any(
                handle.registry is self._registry
                and handle.api_name == capability_name
                for handle in providers
            ):
                self._store.append_audit(
                    self._audit_entry(
                        consumer,
                        action_name,
                        action_def.target_type,
                        params_dict,
                        invocation_id=invocation_id,
                        outcome="error",
                    )
                )
                raise PreconditionFailed(
                    f"no provider bound for declared capability "
                    f"{capability_name!r} on action {action_name!r}",
                    code="CAPABILITY_NOT_PROVIDED",
                )
        return providers

    def _preflight_effects(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params_dict: dict[str, Any],
        invocation_id: str,
        effect_dispatchers: Mapping[EffectHandle[Any], EffectDispatcher] | None,
    ) -> dict[str, EffectDispatcher]:
        """Every declared effect must have a dispatcher bound BEFORE the
        transaction opens (same shape as the capability pre-flight)."""
        dispatchers = dict(effect_dispatchers or {})
        for effect_name in action_def.effects:
            if not any(
                handle.registry is self._registry and handle.api_name == effect_name
                for handle in dispatchers
            ):
                self._store.append_audit(
                    self._audit_entry(
                        consumer,
                        action_name,
                        action_def.target_type,
                        params_dict,
                        invocation_id=invocation_id,
                        outcome="error",
                    )
                )
                raise PreconditionFailed(
                    f"no dispatcher bound for declared effect "
                    f"{effect_name!r} on action {action_name!r}",
                    code="EFFECT_NOT_DISPATCHABLE",
                )
        return {
            handle.api_name: dispatcher
            for handle, dispatcher in dispatchers.items()
            if handle.registry is self._registry
        }

    def execute(
        self,
        consumer: Consumer,
        action_name: str,
        params: dict[str, Any] | BaseModel,
        *,
        capability_providers: Mapping[CapabilityHandle[Any], object] | None = None,
        effect_dispatchers: Mapping[EffectHandle[Any], EffectDispatcher] | None = None,
    ) -> dict[str, Any]:
        """Run the platform pipeline for `action_name`.

        `params` may be a plain dict (string/MCP execute; validated against
        the declared `ActionParameterDef`s and, if the handler declares a
        params class, further validated via `params_cls.model_validate`) or
        an already-constructed instance of the handler's registered params
        class (typed execute; used directly, `model_dump()` feeds the same
        declared-shape validation and audit trail).

        Emitted effects are written to the durable `effect_outbox` INSIDE the
        action transaction (and recorded as `pending` in the audit log, as
        before), then dispatched synchronously in emission order after commit.

        Delivery is **at-least-once** (spec `durable-effect-outbox`): this
        inline pass is attempt 1 of the `RetryPolicy`, and an attempt that
        raises leaves a durable row for `OntologyClient.drain_effects()` to
        retry. One dispatcher's `Exception` is still isolated from the rest and
        never changes the handler result. A process-control exception
        (`KeyboardInterrupt`/`SystemExit`) is still NOT isolated -- it
        propagates and stops later dispatches, because refusing a Ctrl-C to
        keep a delivery promise is the wrong trade (M5 AC12) -- but the effects
        it skipped are now leased pending rows that a later drain delivers,
        rather than losses.

        The `effects_dispatched` follow-up audit append is best-effort, as is
        marking a row delivered: either can fail, or the process can die
        between the outside call and the mark. That window is what makes the
        guarantee at-least-once rather than exactly-once, and it is why a
        dispatcher must be idempotent on `EffectMeta.effect_id`.
        """
        if self._store.in_transaction:
            # AC9/§5: refuse FIRST, before any audit write -- an audit row
            # written inside the caller's own transaction could itself be
            # rolled back with it, so this refusal is deliberately NOT
            # audited.
            raise ConflictError(
                f"execute({action_name!r}): called inside a caller-opened "
                "store transaction -- the engine, not the caller, must own "
                "the transaction/audit boundary",
                code="CALLER_TRANSACTION_REFUSED",
            )

        # One id for this call, stamped on every audit entry it writes -- the
        # denied/error/ok entry AND the later `effects_dispatched` entry -- so
        # a reader can correlate them by value instead of by field-matching and
        # append order. Minted after the caller-transaction refusal above,
        # which is deliberately not audited and so has nothing to correlate.
        invocation_id = self._id_factory()

        action_def, fn, params_cls = self._resolve_handler(action_name)

        params_dict: dict[str, Any] = (
            # mode="json" so e.g. a `datetime` param serializes to a JSON-
            # safe string here (both for declared-shape/`validate_scalar`
            # checks below and the audit trail) -- the handler still
            # receives the original typed `params` instance, untouched.
            params.model_dump(mode="json")
            if isinstance(params, BaseModel)
            else params
        )

        if consumer.role not in action_def.executable_by_roles:
            self._deny(
                consumer,
                action_name,
                action_def,
                params_dict,
                f"role {consumer.role!r} may not execute {action_name!r}",
                invocation_id,
                code="PERMISSION_DENIED",
            )

        self._validate_params(consumer, action_name, action_def, params_dict, invocation_id)
        self._validate_params_json_safe(
            consumer, action_name, action_def, params_dict, invocation_id
        )
        self._enforce_scope(consumer, action_name, action_def, params_dict, invocation_id)

        params_obj = self._coerce_typed_params(
            consumer, action_name, action_def, params, params_dict, params_cls, invocation_id
        )

        providers = self._preflight_capabilities(
            consumer,
            action_name,
            action_def,
            params_dict,
            invocation_id,
            capability_providers,
        )
        dispatchers_by_name = self._preflight_effects(
            consumer,
            action_name,
            action_def,
            params_dict,
            invocation_id,
            effect_dispatchers,
        )

        capability_accesses: list[CapabilityAccessRecord] = []
        emitted_effects: list[tuple[str, EffectPayload]] = []
        target_id = self._resolve_target_id(action_def, params_dict)
        try:
            result, outbox_records = self._run_transaction(
                consumer,
                action_name,
                action_def,
                params_dict,
                target_id,
                fn,
                params_cls,
                params_obj,
                providers,
                capability_accesses,
                emitted_effects,
                invocation_id,
            )
        except Exception:
            # Audit write happens after any transaction rollback so it
            # persists regardless of the exception type (ActionError,
            # a store-layer conflict, KeyError, etc.). Nothing
            # committed, so `writes` stays empty (AC11/§8). Kept HERE, not
            # inside `_run_transaction`, so `capability_accesses`' scoping --
            # accesses recorded before the rollback survive onto this entry --
            # stays exactly as it was.
            self._store.append_audit(
                self._audit_entry(
                    consumer,
                    action_name,
                    action_def.target_type,
                    params_dict,
                    invocation_id=invocation_id,
                    outcome="error",
                    capability_accesses=capability_accesses,
                )
            )
            raise

        self._dispatch_inline(
            consumer,
            action_name,
            action_def,
            params_dict,
            target_id,
            emitted_effects,
            outbox_records,
            dispatchers_by_name,
            invocation_id,
        )
        return result

    def _run_transaction(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params_dict: dict[str, Any],
        target_id: str | None,
        fn: Callable[..., dict[str, Any]],
        params_cls: type[BaseModel] | None,
        params_obj: BaseModel | None,
        providers: Mapping[CapabilityHandle[Any], object],
        capability_accesses: list[CapabilityAccessRecord],
        emitted_effects: list[tuple[str, EffectPayload]],
        invocation_id: str,
    ) -> tuple[dict[str, Any], list[OutboxRecord]]:
        """The engine-owned transaction: handler call under write capture,
        outbox-record construction, `enqueue_effects`, and the pending
        ("ok") audit entry -- all inside ONE store transaction (spec
        AC2/§7). Every handler call runs inside a store transaction here,
        so a handler that raises partway through its own writes rolls back
        ALL of them even if the handler body never called
        `store.transaction()` itself (`transaction()` is reentrant). The
        write-capture context runs inside the transaction (not around it)
        so an authority refusal mid-handler still rolls back any partial
        writes the handler already made. The caller (`execute`) owns the
        rollback error-audit."""
        with self._store.transaction():
            with self._store.capture_action_writes() as writes:
                if params_cls is not None:
                    if params_obj is None:
                        raise InternalError(
                            "typed action parameters were not coerced before execution",
                            code="INTERNAL_ERROR",
                        )
                    ctx = ActionContext(
                        self._store,
                        _source(action_name),
                        consumer,
                        registry=self._registry,
                        declared_capabilities=frozenset(action_def.capabilities),
                        capability_providers=providers,
                        capability_accesses=capability_accesses,
                        declared_effects=frozenset(action_def.effects),
                        emitted_effects=emitted_effects,
                    )
                    result = fn(ctx, params_obj)
                else:
                    result = fn(consumer, params_dict)
                result = self._validate_result_json_safe(action_name, result)
            emitted_at = self._clock()
            outbox_records = [
                OutboxRecord(
                    effect_id=self._id_factory(),
                    invocation_id=invocation_id,
                    seq=seq,
                    api_name=api_name,
                    payload=_json_payload(api_name, snapshot),
                    action=action_name,
                    actor_id=consumer.actor_id,
                    role=consumer.role,
                    emitted_at=emitted_at,
                    state="pending",
                    attempts=0,
                    # Due immediately: the inline attempt happens as soon as
                    # this transaction commits.
                    next_attempt_at=emitted_at,
                    # Born LEASED by this invocation (spec AC5), so a
                    # drainer running concurrently in another process
                    # cannot claim the row out from under the inline
                    # attempt and double-send it. The lease expires on its
                    # own if this process dies before attempting.
                    lease_until=emitted_at + self._retry_policy.lease,
                    updated_at=emitted_at,
                )
                for seq, (api_name, snapshot) in enumerate(emitted_effects)
            ]
            # Inside the transaction, so the work items commit with the
            # writes or vanish with them (spec AC1). This CAN raise --
            # unlike `append_audit`, which never does -- and that is the
            # point: an effect that cannot be persisted must roll the
            # action back rather than be quietly dropped.
            self._store.enqueue_effects(outbox_records)
            pending_entry = self._audit_entry(
                consumer,
                action_name,
                action_def.target_type,
                params_dict,
                invocation_id=invocation_id,
                outcome="ok",
                target_id=target_id,
                # The SAME instant the outbox rows carry, rather than a
                # second `now()` a few microseconds later: `EffectMeta.ts`
                # comes off the row, and an audit entry that disagreed with
                # it about when the effect was emitted would be one more
                # thing for a reader to reconcile.
                ts=emitted_at,
                writes=list(writes),
                effects=[
                    EffectRecord(
                        api_name=record.api_name,
                        payload=record.payload,
                        outcome="pending",
                        effect_id=record.effect_id,
                    )
                    for record in outbox_records
                ],
                capability_accesses=capability_accesses,
            )
            self._store.append_audit(pending_entry)
        return result, outbox_records

    def _dispatch_inline(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params_dict: dict[str, Any],
        target_id: str | None,
        emitted_effects: list[tuple[str, EffectPayload]],
        outbox_records: list[OutboxRecord],
        dispatchers_by_name: Mapping[str, EffectDispatcher],
        invocation_id: str,
    ) -> None:
        """Post-commit inline delivery (attempt 1 of the `RetryPolicy`) +
        the best-effort `effects_dispatched` follow-up entry. The dispatcher
        gets the handler's own typed snapshot, not the JSON round-trip
        stored in the row: an inline delivery must not silently differ from
        what the handler emitted (a DRAIN has no choice and rehydrates from
        the row)."""
        if not emitted_effects:
            return
        finalized_effects: list[EffectRecord] = []
        for (api_name, snapshot), record in zip(
            emitted_effects, outbox_records, strict=True
        ):
            _, finalized = _deliver_effect(
                self._store,
                record,
                dispatchers_by_name[api_name],
                snapshot,
                self._retry_policy,
                attempt=1,
                now=self._clock(),
            )
            finalized_effects.append(finalized)

        try:
            self._store.append_audit(
                self._audit_entry(
                    consumer,
                    action_name,
                    action_def.target_type,
                    params_dict,
                    invocation_id=invocation_id,
                    outcome="effects_dispatched",
                    target_id=target_id,
                    effects=finalized_effects,
                )
            )
        except Exception:
            # Dispatch has already happened and must not be retried or
            # reported as an action failure. The durable pending row is
            # intentionally the only surviving evidence in this case.
            pass

    def _deny(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params: dict[str, Any],
        message: str,
        invocation_id: str | None,
        *,
        code: str,
    ) -> NoReturn:
        """Audit a denied attempt (outcome "denied") and raise. Shared by
        the role check ("PERMISSION_DENIED") and the scope check
        ("SCOPE_DENIED") so both denial paths are audited identically; the
        caller supplies which code applies."""
        self._store.append_audit(
            self._audit_entry(
                consumer,
                action_name,
                action_def.target_type,
                params,
                invocation_id=invocation_id,
                outcome="denied",
            )
        )
        raise PermissionDenied(message, code=code)

    def _error(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params: dict[str, Any],
        message: str,
        invocation_id: str | None,
    ) -> NoReturn:
        """Audit a rejected attempt before any handler ran (outcome
        "error") -- used for basic parameter validation failures, which are
        not scope/permission denials but also never reach the handler."""
        self._store.append_audit(
            self._audit_entry(
                consumer,
                action_name,
                action_def.target_type,
                params,
                invocation_id=invocation_id,
                outcome="error",
            )
        )
        raise ActionError(message, code="INVALID_PARAMS")

    def _validate_params(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params: dict[str, Any],
        invocation_id: str | None,
    ) -> None:
        declared = {p.name: p for p in action_def.parameters}
        for param_name in params:
            if param_name not in declared:
                self._error(
                    consumer,
                    action_name,
                    action_def,
                    params,
                    f"{action_name!r}: unknown parameter {param_name!r}",
                    invocation_id,
                )
        for param in action_def.parameters:
            if param.required and param.name not in params:
                self._error(
                    consumer,
                    action_name,
                    action_def,
                    params,
                    f"{action_name!r}: missing required parameter {param.name!r}",
                    invocation_id,
                )
            if param.name not in params:
                continue
            value = params[param.name]
            if value is None:
                continue
            type_error = self._type_mismatch(param, value)
            if type_error is not None:
                self._error(
                    consumer,
                    action_name,
                    action_def,
                    params,
                    f"{action_name!r}: parameter {param.name!r} {type_error}",
                    invocation_id,
                )

    def _validate_params_json_safe(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params: dict[str, Any],
        invocation_id: str | None,
    ) -> None:
        """AC12: refuse (as `INVALID_PARAMS`, audited) params that cannot be
        faithfully round-tripped through JSON -- the audit log persists
        params as JSON, so a value that survives declared-type validation
        but not a JSON round-trip (e.g. NaN, a custom object smuggled past
        `_type_mismatch`) must still be caught before the handler runs."""
        try:
            round_tripped = json.loads(json.dumps(params))
        except (TypeError, ValueError):
            self._error(
                consumer,
                action_name,
                action_def,
                params,
                f"{action_name!r}: parameters are not JSON-serializable",
                invocation_id,
            )
            return
        if round_tripped != params:
            self._error(
                consumer,
                action_name,
                action_def,
                params,
                f"{action_name!r}: parameters do not round-trip through "
                "JSON unchanged",
                invocation_id,
            )

    @staticmethod
    def _validate_result_json_safe(
        action_name: str, result: Any
    ) -> dict[str, Any]:
        """Validate the handler result at the caller/MCP JSON boundary.

        Action results cross the same JSON boundary as parameters on their way
        to dynamic callers and MCP. The serialize/deserialize equality check
        rejects values whose JSON encoding changes their shape (for example
        tuples or non-string mapping keys), in addition to values that cannot
        be encoded at all.
        """
        if not isinstance(result, dict):
            raise InternalError(
                f"{action_name!r}: handler returned {type(result).__name__}; "
                "action results must be JSON-safe dictionaries",
                code="INTERNAL_ERROR",
            )
        try:
            round_tripped = json.loads(json.dumps(result, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise InternalError(
                f"{action_name!r}: handler result is not JSON-serializable",
                code="INTERNAL_ERROR",
            ) from exc
        if round_tripped != result:
            raise InternalError(
                f"{action_name!r}: handler result does not round-trip through "
                "JSON unchanged",
                code="INTERNAL_ERROR",
            )
        return result

    @staticmethod
    def _resolve_target_id(
        action_def: ActionTypeDef, params: dict[str, Any]
    ) -> str | None:
        """The applied-audit entry's `target_id` (AC11): the value of the
        action's declared target parameter -- the `ActionParameterDef` with
        `scope_semantics="target"`, else the first param whose `refers_to`
        matches `action_def.target_type` -- never a guess at the handler's
        return dict."""
        target_param = next(
            (p for p in action_def.parameters if p.scope_semantics == "target"),
            None,
        )
        if target_param is None:
            target_param = next(
                (
                    p
                    for p in action_def.parameters
                    if p.refers_to == action_def.target_type
                ),
                None,
            )
        if target_param is None:
            return None
        value = params.get(target_param.name)
        return value if isinstance(value, str) else None

    @staticmethod
    def _type_mismatch(param: ActionParameterDef, value: Any) -> str | None:
        """Return a mismatch description, or None if `value` matches the
        declared `param.type` (delegates to `typesys.validate_scalar`, the
        single source of truth for `PropertyType` -> python-type checks,
        including the bool-is-not-int guard and ISO-8601 datetime
        parsing)."""
        return validate_scalar(value, param.type)

    def _enforce_scope(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params: dict[str, Any],
        invocation_id: str | None,
    ) -> None:
        """Deny-by-default scope enforcement, driven by each declared
        `ActionParameterDef.scope_semantics` / `.refers_to` rather than any
        hardcoded parameter or object-type name (see module docstring).

        - `scope_semantics="target"`: only enforced if the named object
          actually EXISTS (a nonexistent target is left to the handler's
          own precondition check, audited "error", never a scope denial).
        - `scope_semantics="scope"`: always enforced when the param is
          present -- it names a scope object directly, existence aside.
        """
        for param in self._param_rules(action_def):
            if param.name not in params:
                continue
            obj_id = params[param.name]
            if not isinstance(obj_id, str):
                # `_validate_params` runs before `_enforce_scope` and rejects
                # (audited "error") any declared param whose value doesn't
                # match its declared `type`, so a scope-bearing param can
                # only reach here as a non-str if `_validate_params` didn't
                # already reject it (defense in depth) -- never silently
                # skip the scope gate on type confusion (spec §7).
                self._deny(
                    consumer,
                    action_name,
                    action_def,
                    params,
                    f"{action_name!r}: parameter {param.name!r} must be a str "
                    f"to enforce scope, got {type(obj_id).__name__}",
                    invocation_id,
                    code="SCOPE_DENIED",
                )
            if param.refers_to is None:
                raise InternalError(
                    f"scope-bearing parameter {param.name!r} has no refers_to type",
                    code="INTERNAL_ERROR",
                )

            if param.scope_semantics == "target":
                if self._store.read_current(param.refers_to, obj_id) is None:
                    continue  # nonexistent target: precondition handling applies

            resolved = resolve_owning_scope(
                self._policy, self._store, param.refers_to, obj_id
            )
            if not covers_scope(self._policy, consumer, resolved):
                self._deny(
                    consumer,
                    action_name,
                    action_def,
                    params,
                    f"{consumer.role!r} scope {consumer.scope_level}="
                    f"{consumer.scope_id!r} does not cover {param.refers_to} "
                    f"{obj_id!r}",
                    invocation_id,
                    code="SCOPE_DENIED",
                )

    @staticmethod
    def _param_rules(action_def: ActionTypeDef) -> list[ActionParameterDef]:
        return [p for p in action_def.parameters if p.scope_semantics is not None]
