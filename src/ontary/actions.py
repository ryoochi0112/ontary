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
from typing import Any, Literal, NoReturn, TypeVar, cast, overload

from pydantic import BaseModel, ValidationError

from ontary._runtime import default_clock, default_id_factory
from ontary._typed_api import api_name_for, resolve_capability
from ontary.audit import CapabilityAccessRecord, _audit_error_code
from ontary.errors import (
    ConflictError,
    InternalError,
    Kind,
    PermissionDenied,
    PreconditionFailed,
    ValidationFailed,
)
from ontary.meta import ActionParameterDef, ActionTypeDef, OntologyRegistry
from ontary.model import CapabilityHandle, LinkHandle, OntologyObject, hydrate
from ontary.scope import ScopePolicy, resolve_owning_scope
from ontary.security import Consumer, covers_scope
from ontary.store import AuditEntry, Source, Store, StoredObject
from ontary.store._shared import iso_instant
from ontary.typesys import _to_storage_scalar, choice_value, struct_value, validate_scalar

TypedHandler = Callable[["ActionContext", BaseModel], dict[str, Any]]
P = TypeVar("P")
_O = TypeVar("_O", bound=OntologyObject)
_F = TypeVar("_F", bound=OntologyObject)
_T = TypeVar("_T", bound=OntologyObject)

_REMOVAL_REFUSAL_CODES = frozenset(
    "OBJECT_RETIRE_NOT_FOUND OBJECT_ALREADY_RETIRED LINK_NOT_FOUND UNDECLARED_SOURCE_REMOVAL".split()
)


class ActionError(PreconditionFailed):
    """An action precondition failed."""

    kind: Kind = "precondition"


def _source(action_name: str) -> Source:
    return Source(source_system=f"action:{action_name}")


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
        "_id_factory",
        "_declared_capabilities",
        "_capability_providers",
        "_capability_accesses",
        "_loaded",
        "_now",
    )

    def __init__(
        self,
        store: Store,
        source: Source,
        consumer: Consumer,
        *,
        registry: OntologyRegistry | None = None,
        id_factory: Callable[[], str] | None = None,
        declared_capabilities: frozenset[str] = frozenset(),
        capability_providers: Mapping[CapabilityHandle[Any], object] | None = None,
        capability_accesses: list[CapabilityAccessRecord] | None = None,
        now: datetime | None = None,
    ) -> None:
        self._store = store
        self._now = now if now is not None else default_clock()
        self._source = source
        self._consumer = consumer
        self._registry = registry
        self._id_factory = id_factory if id_factory is not None else default_id_factory
        self._declared_capabilities = declared_capabilities
        self._capability_providers = dict(capability_providers or {})
        self._capability_accesses = (
            capability_accesses if capability_accesses is not None else []
        )
        # Objects this context handed out via `get`/`all`/`create`/`traverse`,
        # keyed by `id(obj)`: (the object itself, its api_name, its declared
        # property values at hand-out). `save` diffs against the snapshot, and
        # holding the object keeps its `id()` from being reused (#41).
        self._loaded: dict[int, tuple[OntologyObject, str, dict[str, Any]]] = {}

    @property
    def consumer(self) -> Consumer:
        return self._consumer

    def now(self) -> datetime:
        """The invocation's single instant; equals the audit ts and every
        valid_from this action writes."""
        return self._now

    def capability(self, handle: CapabilityHandle[P]) -> P:
        """The bound provider for `handle`, typed as the handle's protocol
        -- `ctx.capability(LLM)` narrows to
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

    # -- typed surface (#41) ---------------------------------------------------

    def _registry_or_raise(self, operation: str) -> OntologyRegistry:
        if self._registry is None:
            raise InternalError(
                f"ActionContext.{operation} requires the executor's ontology registry",
                code="INTERNAL_ERROR",
            )
        return self._registry

    def _api_name(self, cls: type[OntologyObject], operation: str) -> str:
        return api_name_for(cls, self._registry_or_raise(operation), owner="action")

    def _property_names(self, api_name: str) -> list[str]:
        obj_def = self._registry_or_raise("save").get_object_type(api_name)
        return [p.name for p in obj_def.properties]

    def _declared_snapshot(self, obj: OntologyObject, api_name: str) -> dict[str, Any]:
        properties = self._registry_or_raise("save").get_object_type(api_name).properties
        struct_names = {p.name for p in properties if p.type == "struct"}
        snapshot = obj.model_dump(include={p.name for p in properties} - struct_names)
        for prop in properties:
            if prop.type == "struct" and prop.fields is not None:
                snapshot[prop.name] = struct_value(getattr(obj, prop.name), prop.fields)
        return snapshot

    def _hand_out(self, cls: type[_O], stored: StoredObject) -> _O:
        obj = hydrate(cls, stored, self._consumer.kind)
        api_name = stored.lineage.object_type
        snapshot = self._declared_snapshot(obj, api_name)
        self._loaded[id(obj)] = (obj, api_name, snapshot)
        return obj

    def _object_id(self, obj: OntologyObject, operation: str) -> tuple[str, str]:
        api_name = self._api_name(type(obj), operation)
        primary_key = self._registry_or_raise(operation).get_object_type(api_name).primary_key
        return api_name, str(getattr(obj, primary_key))

    def _link_type(self, link: LinkHandle[Any, Any], operation: str) -> str:
        registry = self._registry_or_raise(operation)
        self._api_name(link.from_cls, operation)
        self._api_name(link.to_cls, operation)
        try:
            registry.get_link_type(link.api_name)
        except ValidationFailed as exc:
            raise ValidationFailed(
                f"link {link.api_name!r} is not registered on this action's ontology",
                code="UNKNOWN_NAME",
            ) from exc
        return link.api_name

    def _endpoint_id(self, endpoint: OntologyObject | str, operation: str) -> str:
        if isinstance(endpoint, OntologyObject):
            return self._object_id(endpoint, operation)[1]
        return endpoint

    def get(self, cls: type[_O], obj_id: str) -> _O | None:
        """Return the current `cls` object `obj_id`, or `None` when absent.

        This is a trusted handler read. It returns raw, unredacted, unscoped
        data. The object is remembered, so `save(obj)` writes back only what
        the handler changed."""
        stored = self._store.read_current(self._api_name(cls, "get"), obj_id)
        return None if stored is None else self._hand_out(cls, stored)

    def all(self, cls: type[_O]) -> list[_O]:
        """Return every current `cls` object as a trusted handler read.

        Results are raw, unredacted, and unscoped."""
        return [
            self._hand_out(cls, stored)
            for stored in self._store.read_all(self._api_name(cls, "all"))
        ]

    def create(self, cls: type[_O], /, **values: Any) -> _O:
        """Create a `cls` object from `values` and return it.

        A missing primary key is filled from the runtime's `id_factory`.
        Keyword names are checked at runtime; the class and return type are
        checked by mypy. An undeclared property is refused with
        `INVALID_RECORD`. The raw store
        deliberately accepts undeclared keys (see `declared_shape_violation`),
        so without this check a misspelled field would be stored silently."""
        api_name = self._api_name(cls, "create")
        declared = set(self._property_names(api_name))
        unknown = sorted(set(values) - declared)
        if unknown:
            raise ValidationFailed(
                f"ActionContext.create({cls.__name__}): unknown "
                f"propert{'y' if len(unknown) == 1 else 'ies'} "
                f"{', '.join(repr(name) for name in unknown)}; "
                f"declared: {', '.join(sorted(declared))}",
                code="INVALID_RECORD",
            )
        obj_id = self._insert(api_name, values)
        stored = self._store.read_current(api_name, obj_id)
        if stored is None:
            raise InternalError(
                f"ActionContext.create: {api_name} {obj_id!r} was not readable "
                "right after its insert",
                code="INTERNAL_ERROR",
            )
        return self._hand_out(cls, stored)

    def save(self, obj: OntologyObject) -> None:
        """Write the declared properties of `obj` that changed since this
        context handed it out, and nothing when none did.

        Only an object from this context's `get`, `all`, `create` or
        `traverse` can be saved (`OBJECT_NOT_LOADED` otherwise): the diff
        needs its snapshot, and a hand-built object would overwrite fields
        the handler never read. A changed primary key is refused by the store
        (`PRIMARY_KEY_IMMUTABLE`)."""
        entry = self._loaded.get(id(obj))
        if entry is None or entry[0] is not obj:
            raise ValidationFailed(
                f"ActionContext.save: this {type(obj).__name__} was not loaded "
                "by this action context -- get it with ctx.get(...) or "
                "ctx.create(...) first",
                code="OBJECT_NOT_LOADED",
            )
        _, api_name, snapshot = entry
        names = self._property_names(api_name)
        current = self._declared_snapshot(obj, api_name)
        changes = {
            name: getattr(obj, name) for name in names if current.get(name) != snapshot.get(name)
        }
        if not changes:
            return
        primary_key = self._registry_or_raise("save").get_object_type(api_name).primary_key
        self._store.update(api_name, str(snapshot[primary_key]), changes, self._source)
        self._loaded[id(obj)] = (obj, api_name, current)

    def link(self, link: LinkHandle[_F, _T], from_: _F | str, to: _T | str, /) -> None:
        """Create a `link` from `from_` to `to` (an object or its id).

        Both ends must be live. Creating an identical live link is a no-op."""
        operation = "link"
        self._store.create_link(
            self._link_type(link, operation),
            self._endpoint_id(from_, operation),
            self._endpoint_id(to, operation),
        )

    @overload
    def traverse(self, link: LinkHandle[_F, _T], anchor: _F | str, /) -> list[_T]: ...
    @overload
    def traverse(
        self, link: LinkHandle[_F, _T], anchor: _T | str, /, *, reverse: Literal[True]
    ) -> list[_F]: ...
    def traverse(
        self,
        link: LinkHandle[_F, _T],
        anchor: _F | _T | str,
        /,
        *,
        reverse: bool = False,
    ) -> list[_T] | list[_F]:
        """Return live objects reached from `anchor` through `link`.

        By default, return `To` objects. With `reverse=True`, return `From`
        objects. This trusted read returns raw, unredacted, unscoped data."""
        operation = "traverse"
        link_api_name = self._link_type(link, operation)
        anchor_id = self._endpoint_id(anchor, operation)
        if reverse:
            from_ids = self._store.links_to(link_api_name, anchor_id)
            return self._hand_out_ids(link.from_cls, from_ids)
        to_ids = self._store.links_from(link_api_name, anchor_id)
        return self._hand_out_ids(link.to_cls, to_ids)

    def _hand_out_ids(self, cls: type[_O], ids: list[str]) -> list[_O]:
        api_name = self._api_name(cls, "traverse")
        found = []
        for obj_id in ids:
            stored = self._store.read_current(api_name, obj_id)
            if stored is not None:
                found.append(self._hand_out(cls, stored))
        return found

    def _insert(self, obj_type: str, payload: dict[str, Any]) -> str:
        if self._registry is not None:
            primary_key = self._registry.get_object_type(obj_type).primary_key
            if payload.get(primary_key) is None:
                payload = dict(payload)
                payload[primary_key] = self._id_factory()
        return self._store.insert(obj_type, payload, self._source)

    def _require_action_transaction(self, operation: str) -> None:
        if not self._store.in_transaction:
            raise ConflictError(
                f"ActionContext.{operation}() may only run inside the "
                "engine-owned action transaction",
                code="CALLER_TRANSACTION_REFUSED",
            )

    @overload
    def retire(self, obj: OntologyObject, /) -> None: ...
    @overload
    def retire(self, cls: type[OntologyObject], obj_id: str, /) -> None: ...
    def retire(
        self,
        obj_type: OntologyObject | type[OntologyObject],
        obj_id: str | None = None,
    ) -> None:
        """Retire an object and close every distinct live link touching it.

        The executor constructs this context inside its engine-owned
        transaction and write-capture boundary, so the object close, both
        directions of link cascade, and audit write records commit or roll
        back together.
        """
        self._require_action_transaction("retire")
        if isinstance(obj_type, str):
            raise ValidationFailed(
                "ActionContext.retire() no longer accepts a type name string "
                "(removed in 0.18.0); use ctx.retire(obj) or ctx.retire(Cls, obj_id)",
                code="INVALID_PARAMS",
            )
        if self._registry is None:
            raise InternalError(
                "ActionContext.retire requires the executor's ontology registry",
                code="INTERNAL_ERROR",
            )
        if isinstance(obj_type, OntologyObject):
            self._loaded.pop(id(obj_type), None)
            api_name, obj_id = self._object_id(obj_type, "retire")
        else:
            api_name = self._api_name(obj_type, "retire")
        if obj_id is None:
            raise ValidationFailed(
                f"ActionContext.retire({api_name!r}) needs an object id",
                code="INVALID_PARAMS",
            )
        self._store.retire_object(api_name, obj_id)

        closed_links: set[tuple[str, str, str]] = set()

        def unlink_once(link_api_name: str, from_id: str, to_id: str) -> None:
            link = (link_api_name, from_id, to_id)
            if link in closed_links:
                return
            self._close_link(link_api_name, from_id, to_id)
            closed_links.add(link)

        for link_def in self._registry.link_types.values():
            if link_def.from_type == api_name:
                for to_id in self._store.links_from(link_def.api_name, obj_id):
                    unlink_once(link_def.api_name, obj_id, to_id)
            if link_def.to_type == api_name:
                for from_id in self._store.links_to(link_def.api_name, obj_id):
                    unlink_once(link_def.api_name, from_id, obj_id)

    def unlink(
        self,
        link_api_name: LinkHandle[_F, _T],
        from_id: _F | str,
        to_id: _T | str,
        /,
    ) -> None:
        """Close one live link inside the current action transaction. The
        handle names the link, and each end is an object or its id."""
        self._require_action_transaction("unlink")
        if isinstance(link_api_name, str):
            raise ValidationFailed(
                "ActionContext.unlink() no longer accepts a link name string "
                "(removed in 0.18.0); use ctx.unlink(handle, from_, to)",
                code="INVALID_PARAMS",
            )
        self._close_link(
            self._link_type(link_api_name, "unlink"),
            self._endpoint_id(from_id, "unlink"),
            self._endpoint_id(to_id, "unlink"),
        )

    def _close_link(self, link_api_name: str, from_id: str, to_id: str) -> None:
        # `retire`'s cascade closes links through here.
        self._require_action_transaction("unlink")
        self._store.close_link(link_api_name, from_id, to_id)


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
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._policy = policy
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
        action_def: ActionTypeDef,
        params: dict[str, Any],
        *,
        invocation_id: str | None,
        outcome: str,
        ts: datetime,
        **extra: Any,
    ) -> AuditEntry:
        """THE one literal `AuditEntry(...)` construction in the executor
        (C4 of the staged refactor) -- every denied/error/pending/follow-up
        entry goes through here, so the consumer -> actor/role/principal
        stamping cannot drift between sites (and the audit-principal AST
        guard has one site to verify instead of seven). `target_id` is
        resolved here from the declared target parameter, so a denied or
        error entry names the requested target exactly as the ok entry does
        (#49). `ts` is the invocation's single instant. `extra` carries the per-site fields (`writes`,
        `capability_accesses`, `unscoped_params`, `error_code`)."""
        return AuditEntry(
            ts=ts,
            invocation_id=invocation_id,
            actor=consumer.actor_id,
            role=consumer.role,
            principal=consumer.principal,
            action=action_name,
            target_type=action_def.target_type,
            target_id=self._resolve_target_id(action_def, params),
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
        instant: datetime,
        unscoped_params: list[str],
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
                instant,
                unscoped_params=unscoped_params,
            )

    def _preflight_capabilities(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params_dict: dict[str, Any],
        invocation_id: str,
        instant: datetime,
        capability_providers: Mapping[CapabilityHandle[Any], object] | None,
        unscoped_params: list[str],
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
                        action_def,
                        params_dict,
                        invocation_id=invocation_id,
                        ts=instant,
                        outcome="error",
                        unscoped_params=unscoped_params,
                        error_code="CAPABILITY_NOT_PROVIDED",
                    )
                )
                raise PreconditionFailed(
                    f"no provider bound for declared capability "
                    f"{capability_name!r} on action {action_name!r}",
                    code="CAPABILITY_NOT_PROVIDED",
                )
        return providers

    def execute(
        self,
        consumer: Consumer,
        action_name: str,
        params: dict[str, Any] | BaseModel,
        *,
        capability_providers: Mapping[CapabilityHandle[Any], object] | None = None,
    ) -> dict[str, Any]:
        """Run the platform pipeline for `action_name`.

        `params` may be a plain dict (string/MCP execute; validated against
        the declared `ActionParameterDef`s and, if the handler declares a
        params class, further validated via `params_cls.model_validate`) or
        an already-constructed instance of the handler's registered params
        class (typed execute; used directly, `model_dump()` feeds the same
        declared-shape validation and audit trail).
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

        # One id for this call, stamped on every audit entry it writes, so a
        # reader can correlate them by value instead of by field-matching and
        # append order. Minted after the caller-transaction refusal above,
        # which is deliberately not audited and so has nothing to correlate.
        invocation_id = self._id_factory()

        # The invocation's single clock read: stamps every audit entry below,
        # the write capture (every valid_from/valid_to) and `ctx.now()`. A
        # naive value is refused here, before anything is audited or written.
        instant = self._clock()
        iso_instant(instant)

        action_def, fn, params_cls = self._resolve_handler(action_name)

        params_dict = self._params_to_dict(action_def, params)

        if consumer.role not in action_def.executable_by_roles:
            self._deny(
                consumer,
                action_name,
                action_def,
                params_dict,
                f"role {consumer.role!r} may not execute {action_name!r}",
                invocation_id,
                instant,
                code="PERMISSION_DENIED",
            )

        self._validate_typed_datetime_params(
            consumer, action_name, action_def, params, params_dict, invocation_id, instant
        )
        self._validate_params(
            consumer, action_name, action_def, params_dict, invocation_id, instant
        )
        self._validate_params_json_safe(
            consumer, action_name, action_def, params_dict, invocation_id, instant
        )
        unscoped_params = self._enforce_scope(
            consumer, action_name, action_def, params_dict, invocation_id, instant
        )

        params_obj = self._coerce_typed_params(
            consumer,
            action_name,
            action_def,
            params,
            params_dict,
            params_cls,
            invocation_id,
            instant,
            unscoped_params,
        )

        providers = self._preflight_capabilities(
            consumer,
            action_name,
            action_def,
            params_dict,
            invocation_id,
            instant,
            capability_providers,
            unscoped_params,
        )

        capability_accesses: list[CapabilityAccessRecord] = []
        try:
            result = self._run_transaction(
                consumer,
                action_name,
                action_def,
                params_dict,
                fn,
                params_cls,
                params_obj,
                providers,
                capability_accesses,
                invocation_id,
                instant,
                unscoped_params,
            )
        except Exception as exc:
            # Audit write happens after any transaction rollback so it
            # persists regardless of the exception type (ActionError,
            # a store-layer conflict, KeyError, etc.). Nothing
            # committed, so `writes` stays empty (AC11/§8). Kept HERE, not
            # inside `_run_transaction`, so `capability_accesses`' scoping --
            # accesses recorded before the rollback survive onto this entry --
            # stays exactly as it was.
            outcome = (
                "denied"
                if getattr(exc, "code", None) in _REMOVAL_REFUSAL_CODES
                else "error"
            )
            self._store.append_audit(
                self._audit_entry(
                    consumer,
                    action_name,
                    action_def,
                    params_dict,
                    invocation_id=invocation_id,
                    ts=instant,
                    outcome=outcome,
                    capability_accesses=capability_accesses,
                    unscoped_params=unscoped_params,
                    error_code=_audit_error_code(exc),
                )
            )
            raise

        return result

    def _run_transaction(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params_dict: dict[str, Any],
        fn: Callable[..., dict[str, Any]],
        params_cls: type[BaseModel] | None,
        params_obj: BaseModel | None,
        providers: Mapping[CapabilityHandle[Any], object],
        capability_accesses: list[CapabilityAccessRecord],
        invocation_id: str,
        instant: datetime,
        unscoped_params: list[str],
    ) -> dict[str, Any]:
        """The engine-owned transaction: handler call under write capture
        and the "ok" audit entry -- all inside ONE store transaction (spec
        AC2/§7). Every handler call runs inside a store transaction here,
        so a handler that raises partway through its own writes rolls back
        ALL of them even if the handler body never called
        `store.transaction()` itself (`transaction()` is reentrant). The
        write-capture context runs inside the transaction (not around it)
        so an authority refusal mid-handler still rolls back any partial
        writes the handler already made. The caller (`execute`) owns the
        rollback error-audit."""
        with self._store.transaction():
            with self._store.capture_action_writes(
                at=iso_instant(instant)
            ) as writes:
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
                        id_factory=self._id_factory,
                        declared_capabilities=frozenset(action_def.capabilities),
                        capability_providers=providers,
                        capability_accesses=capability_accesses,
                        now=instant,
                    )
                    result = fn(ctx, params_obj)
                else:
                    result = fn(consumer, params_dict)
                result = self._validate_result_json_safe(action_name, result)
            pending_entry = self._audit_entry(
                consumer,
                action_name,
                action_def,
                params_dict,
                invocation_id=invocation_id,
                ts=instant,
                outcome="ok",
                # The invocation's single instant (read at the start of
                # `execute`), the same one `ctx.now()` and every written
                # valid_from carry -- not a second clock read.
                writes=list(writes),
                capability_accesses=capability_accesses,
                unscoped_params=unscoped_params,
            )
            self._store.append_audit(pending_entry)
        return result

    def _deny(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params: dict[str, Any],
        message: str,
        invocation_id: str | None,
        instant: datetime,
        *,
        code: str,
        unscoped_params: list[str] | None = None,
    ) -> NoReturn:
        """Audit a denied attempt (outcome "denied") and raise. Shared by
        the role check ("PERMISSION_DENIED") and the scope check
        ("SCOPE_DENIED") so both denial paths are audited identically; the
        caller supplies which code applies."""
        self._store.append_audit(
            self._audit_entry(
                consumer,
                action_name,
                action_def,
                params,
                invocation_id=invocation_id,
                ts=instant,
                outcome="denied",
                unscoped_params=list(unscoped_params or []),
                error_code=code,
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
        instant: datetime,
        *,
        unscoped_params: list[str] | None = None,
    ) -> NoReturn:
        """Audit a rejected attempt before any handler ran (outcome
        "error") -- used for basic parameter validation failures, which are
        not scope/permission denials but also never reach the handler."""
        self._store.append_audit(
            self._audit_entry(
                consumer,
                action_name,
                action_def,
                params,
                invocation_id=invocation_id,
                ts=instant,
                outcome="error",
                unscoped_params=list(unscoped_params or []),
                error_code="INVALID_PARAMS",
            )
        )
        raise ActionError(message, code="INVALID_PARAMS")

    @staticmethod
    def _params_to_dict(
        action_def: ActionTypeDef, params: dict[str, Any] | BaseModel
    ) -> dict[str, Any]:
        """The JSON-shaped view of `params` that declared-shape validation,
        the JSON round-trip check, and the audit trail all see. The handler
        still receives the original typed `params` instance, untouched.

        Typed form: `model_dump(mode="json")`, so a `datetime` param becomes
        a JSON-safe string (see `_validate_typed_datetime_params` for the
        naive-object hole that dump would open).

        Dict form: a `date`/`datetime` object under a declared parameter is
        converted to the same ISO spelling the store would persist, so it
        survives the JSON round-trip check (#38); a naive `datetime` is left
        as-is for `_validate_params` to refuse with a message that says so.
        An ``Enum`` member under a `choices`-declared parameter is unwrapped
        to its value first (#42; `typesys.choice_value`), so validation, the
        audit trail, and a dict handler all see the plain string.
        """
        if isinstance(params, BaseModel):
            struct_names = {p.name for p in action_def.parameters if p.type == "struct"}
            dumped = params.model_dump(mode="json", exclude=struct_names)
            for typed_param in action_def.parameters:
                if typed_param.type == "struct" and typed_param.fields is not None:
                    value = struct_value(getattr(params, typed_param.name), typed_param.fields)
                    dumped[typed_param.name] = (
                        _to_storage_scalar(value, typed_param.type, fields=typed_param.fields)
                        if value is not None
                        and validate_scalar(value, typed_param.type, fields=typed_param.fields) is None
                        else value
                    )
            return dumped
        declared = {p.name: p for p in action_def.parameters}
        unwrapped = {}
        for name, value in params.items():
            param = declared.get(name)
            if param is not None:
                if param.type == "struct" and param.fields is not None:
                    value = struct_value(value, param.fields)
                value = choice_value(value, param.choices)
            unwrapped[name] = value
        return {
            name: _to_storage_scalar(
                value, declared[name].type, fields=declared[name].fields
            )
            if name in declared
            and value is not None
            and validate_scalar(
                value, declared[name].type, fields=declared[name].fields
            ) is None
            else value
            for name, value in unwrapped.items()
        }

    def _validate_typed_datetime_params(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params: dict[str, Any] | BaseModel,
        params_dict: dict[str, Any],
        invocation_id: str,
        instant: datetime,
    ) -> None:
        """Typed form only: `model_dump(mode="json")` renders a NAIVE
        `datetime` as a naive string, which the string rule accepts. The
        declared `datetime` attributes are therefore checked as objects,
        and a naive one is refused under the same `INVALID_PARAMS` code as
        every other undeclared shape (#38). Runs after the role gate, like
        the rest of parameter validation."""
        if not isinstance(params, BaseModel):
            return
        for param in action_def.parameters:
            if param.type == "struct" and param.fields is not None:
                value = getattr(params, param.name, None)
                if not isinstance(value, BaseModel):
                    continue
                for field in param.fields:
                    if field.type != "datetime":
                        continue
                    inner = getattr(value, field.name, None)
                    if not isinstance(inner, datetime):
                        continue
                    mismatch = validate_scalar(inner, field.type)
                    if mismatch is not None:
                        self._error(
                            consumer,
                            action_name,
                            action_def,
                            params_dict,
                            f"{action_name!r}: {param.name}.{field.name}: {mismatch}",
                            invocation_id,
                            instant,
                        )
            if param.type != "datetime":
                continue
            value = getattr(params, param.name, None)
            if not isinstance(value, datetime):
                continue
            mismatch = validate_scalar(value, param.type)
            if mismatch is not None:
                self._error(
                    consumer,
                    action_name,
                    action_def,
                    params_dict,
                    f"{action_name!r}: parameter {param.name!r} {mismatch}",
                    invocation_id,
                    instant,
                )

    def _validate_params(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params: dict[str, Any],
        invocation_id: str | None,
        instant: datetime,
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
                    instant,
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
                    instant,
                )
            if param.name not in params:
                continue
            value = params[param.name]
            if value is None:
                continue
            type_error = self._type_mismatch(param, value)
            if type_error is not None:
                location = (
                    f"{param.name}.{type_error}"
                    if param.type == "struct" and ": " in type_error
                    else f"parameter {param.name!r} {type_error}"
                )
                self._error(
                    consumer,
                    action_name,
                    action_def,
                    params,
                    f"{action_name!r}: {location}",
                    invocation_id,
                    instant,
                )

    def _validate_params_json_safe(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params: dict[str, Any],
        invocation_id: str | None,
        instant: datetime,
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
                instant,
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
                instant,
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
        """Every action audit entry's `target_id` (AC11, #49): the value of the
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
        parsing) or outside its declared `choices` (#42)."""
        return validate_scalar(value, param.type, param.choices, fields=param.fields)

    def _enforce_scope(
        self,
        consumer: Consumer,
        action_name: str,
        action_def: ActionTypeDef,
        params: dict[str, Any],
        invocation_id: str | None,
        instant: datetime,
    ) -> list[str]:
        """Deny-by-default scope enforcement, driven by each declared
        `ActionParameterDef.scope_semantics` / `.refers_to` rather than any
        hardcoded parameter or object-type name (see module docstring).
        Returns the names of the `target` parameters that skipped the gate
        because they refer to an unscoped type, for the audit trail.

        - `scope_semantics="target"`: only enforced if the named object
          actually EXISTS -- has ever been stored, retired rows included (a
          target that never existed is left to the handler's own
          precondition check, audited "error", never a scope denial).
        - `scope_semantics="scope"`: always enforced when the param is
          present -- it names a scope object directly, existence aside.
        - A `target` that refers to a `scope="unscoped"` type (#35) has no
          owning scope to cover, so the action's `roles=` is its only gate.
          It is skipped here and named in the returned list. The gate's own
          denials and every audit entry written after the gate carry it. A `scope`
          parameter may not refer to an unscoped type; `ScopePolicy.validate`
          refuses that declaration, and here it would resolve no scope and
          deny.
        """
        # Decided up front from the declaration, not in loop order, so an
        # entry denied on an EARLIER scoped parameter still names every
        # parameter that is exempt from the gate.
        unscoped_params = [
            param.name
            for param in self._param_rules(action_def)
            if param.name in params
            and param.scope_semantics == "target"
            and param.refers_to in self._policy.unscoped_types
        ]
        for param in self._param_rules(action_def):
            if param.name not in params or param.name in unscoped_params:
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
                    instant,
                    code="SCOPE_DENIED",
                    unscoped_params=unscoped_params,
                )
            if param.refers_to is None:
                raise InternalError(
                    f"scope-bearing parameter {param.name!r} has no refers_to type",
                    code="INTERNAL_ERROR",
                )

            if param.scope_semantics == "target":
                # Asked of the NEWEST row, not the live one. `read_current`
                # answers `None` for a RETIRED object too, so skipping the
                # gate on that answer handed a consumer outside the object's
                # scope an object that still owns one -- and `create_link`
                # validates no endpoint, so the writes went through. "Never
                # stored" is the only case precondition handling owns.
                if self._store.read_last(param.refers_to, obj_id) is None:
                    continue  # nonexistent target: precondition handling applies

            # ...and having gated on the retired target, resolve the scope it
            # owned, not the one it has: a retired object holds no live links,
            # so a `ViaLink` rule would resolve `None` and deny the very
            # consumer the object belongs to. THIS GATE ONLY. `scope`
            # semantics names a live scope object and stays on the live read,
            # as does every consumer read -- see `resolve_owning_scope`.
            resolved = resolve_owning_scope(
                self._policy,
                self._store,
                param.refers_to,
                obj_id,
                include_retired=param.scope_semantics == "target",
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
                    instant,
                    code="SCOPE_DENIED",
                    unscoped_params=unscoped_params,
                )
        return unscoped_params

    @staticmethod
    def _param_rules(action_def: ActionTypeDef) -> list[ActionParameterDef]:
        return [p for p in action_def.parameters if p.scope_semantics is not None]
