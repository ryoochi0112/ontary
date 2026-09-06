"""Shared low-level typed-model layer (C1 of the staged refactor).

The pieces of the authoring DSL that the RUNTIME also needs -- the
`OntologyObject`/`ActionParams` base classes and their class stamps,
`hydrate`, and the three typed handles -- moved here, below `functions`/
`actions`/`client`, so those modules import them at module level instead of
through the deferred-import cycle they previously used. `ontary.authoring`
re-exports every public name, so author-facing imports are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, PrivateAttr, ValidationError

from ontary.errors import ValidationFailed
from ontary.meta import OntologyRegistry, Sensitivity
from ontary.security import ConsumerKind
from ontary.store import Lineage, StoredObject
from ontary.typesys import _storage_scalar_violation

__all__ = [
    "ActionParams",
    "CapabilityHandle",
    "LinkHandle",
    "OntologyObject",
    "hydrate",
]


class OntologyObject(BaseModel):
    """Base class for author-declared object-type models.

    Carries two hydration-time private attrs the client sets after a
    guarded read (never author model fields, so they can't collide with a
    declared property): `_redacted_fields` (properties hidden from this
    consumer) and `_lineage` (the engine's frozen `Lineage`, M3.5 T7).
    `extra="ignore"` keeps forward-compat with payload keys added by later
    migrations.
    """

    model_config = ConfigDict(extra="ignore")

    _redacted_fields: frozenset[str] = PrivateAttr(default_factory=frozenset)
    _lineage: Lineage | None = PrivateAttr(default=None)

    # Stamped by `Ontology.object()`'s decorator (T3): the registered
    # `api_name`, per-property `Sensitivity`, and the `OntologyRegistry`
    # instance the class was registered on, so `OntologyClient`/
    # `hydrate()` can resolve/redact a class without holding a reference to
    # the `Ontology` facade that registered it. `None`/empty on an
    # undecorated class -- that's how a typed client call recognizes "never
    # registered" and raises a validation-kind `UNKNOWN_NAME` failure (spec §8).
    #
    # These MUST be read via `cls.__dict__.get(...)`, never plain attribute
    # access -- a normal lookup falls through to a base class's stamp on an
    # UNDECORATED SUBCLASS (`class Sub(Ticket): ...` inherits the ClassVar),
    # which would silently resolve/hydrate `Sub` as if it were registered.
    # `_ontary_registry` additionally lets a resolver reject a class that
    # carries a same-named stamp from a DIFFERENT `Ontology`/registry (two
    # separate `Ontology(...)` instances can register the same api_name
    # with unrelated classes) -- compare registry identity, never just the
    # api_name string.
    _ontary_api_name: ClassVar[str | None] = None
    _ontary_sensitivity: ClassVar[dict[str, Sensitivity]] = {}
    _ontary_registry: ClassVar[OntologyRegistry | None] = None

    @property
    def redacted_fields(self) -> frozenset[str]:
        return self._redacted_fields

    @property
    def lineage(self) -> Lineage | None:
        return self._lineage


_T = TypeVar("_T", bound=OntologyObject)


def hydrate(cls: type[_T], stored: StoredObject, consumer_kind: ConsumerKind) -> _T:
    """`cls.model_validate(stored.payload)` + set `_redacted_fields`/
    `_lineage` (spec §6). `_redacted_fields` names properties whose
    declared `Sensitivity` is restricted for `consumer_kind` AND absent
    from the payload -- a *visible* optional property that happens to be
    absent (sparse ingest) is NOT redacted, only `None`/default (spec §8).
    A `ValidationError` from a malformed stored value is wrapped in the
    validation kind, never left as a bare traceback.
    """
    api_name = cls.__dict__.get("_ontary_api_name")
    registry = cls.__dict__.get("_ontary_registry")
    if api_name is not None and registry is not None:
        obj_def = registry.get_object_type(api_name)
        for prop in obj_def.properties:
            if prop.name not in stored.payload:
                continue
            value = stored.payload[prop.name]
            if value is None:
                continue
            mismatch = _storage_scalar_violation(value, prop.type, prop.choices)
            if mismatch is not None:
                raise ValidationFailed(
                    f"{cls.__name__} {stored.lineage.object_id!r}: stored "
                    f"property {prop.name!r} {mismatch}",
                    code="INVALID_RECORD",
                )

    try:
        obj = cls.model_validate(stored.payload)
    except ValidationError as exc:
        raise ValidationFailed(
            f"{cls.__name__} {stored.lineage.object_id!r}: stored payload "
            f"failed validation on hydration: {exc}",
            code="INVALID_RECORD",
        ) from exc

    redacted: set[str] = set()
    for name, sensitivity in cls._ontary_sensitivity.items():
        restricted = (consumer_kind == "human" and not sensitivity.human_visible) or (
            consumer_kind == "ai" and not sensitivity.ai_usable
        )
        if restricted and name not in stored.payload:
            redacted.add(name)

    obj._redacted_fields = frozenset(redacted)
    obj._lineage = stored.lineage
    return obj


class ActionParams(BaseModel):
    """Base class for typed action-params models (spec `typed-actions.md`
    §6): plain scalar annotations reuse the same annotation->`PropertyType`
    table as `OntologyObject`; `target(cls)`/`scope_ref(cls)` field markers
    derive `refers_to`/`scope_semantics`. `extra="forbid"` matches the
    pipeline's own declared-shape validation, which already rejects an
    unknown parameter (`ActionExecutor._validate_params`) -- the class
    should refuse the same shape a caller would hit downstream, not accept
    more.

    Stamped by `Ontology.action()` with the registered `api_name` and the
    `OntologyRegistry` instance (mirrors `OntologyObject`'s stamps) so a
    typed `client.execute(params_instance)` can resolve an instance to its
    action fail-closed -- read via `cls.__dict__.get(...)`, never plain
    attribute access, for the same undecorated-subclass reason documented
    on `OntologyObject`.
    """

    model_config = ConfigDict(extra="forbid")

    _ontary_api_name: ClassVar[str | None] = None
    _ontary_registry: ClassVar[OntologyRegistry | None] = None


def _class_stamp(cls: type[Any]) -> tuple[str | None, OntologyRegistry | None]:
    """THE shared class/handle-resolution helper (spec `typed-actions.md`
    §6): reads an author-decorated class's `_ontary_api_name`/
    `_ontary_registry` stamps via `cls.__dict__` ONLY -- never plain
    attribute access, which would fall through to a base class's ClassVar
    on an undecorated SUBCLASS (`class Sub(Ticket): ...` inherits the
    stamps without being decorated itself) and silently resolve `Sub` as if
    it were registered (see `OntologyObject`'s docstring). Works for both
    `OntologyObject` subclasses (`@ontology.object(...)`) and `ActionParams`
    subclasses (`@ontology.action(...)`) -- both stamp the same two names.
    Returns `(None, None)` for a class that was never decorated.

    Deliberately a PURE read, not a check: `client.OntologyClient.
    _api_name_for`/`._api_name_for_action_params`, `functions.BoundQuery`'s
    typed overloads, and `_api_name_for_registered` below each compare the
    returned registry to their own and raise their own fail-closed error
    (different types/messages per caller) -- the `__dict__`-stamp
    discipline itself lives here exactly once, never duplicated.
    """
    return cls.__dict__.get("_ontary_api_name"), cls.__dict__.get("_ontary_registry")


_F = TypeVar("_F", bound=OntologyObject)
_ProviderT_co = TypeVar("_ProviderT_co", covariant=True)


@dataclass(frozen=True)
class LinkHandle(Generic[_F, _T]):
    """A statically-typed handle to one derived `LinkTypeDef`, returned by
    `Ontology.link(...)` -- carries the two author classes it connects so a
    typed `client.traverse(handle, id)` can infer its return type
    without a string lookup.
    """

    api_name: str
    from_cls: type[_F]
    to_cls: type[_T]


@dataclass(frozen=True)
class CapabilityHandle(Generic[_ProviderT_co]):
    """Typed identity for one outside-read declaration.

    `api_name` is what enters the serializable IR; `proto` preserves the
    provider type for later call-site narrowing; `registry` preserves the
    declaring ontology's identity so same-named declarations on independent
    ontologies cannot be confused. The frozen value shape also makes handles
    safe provider-map keys once runtime binding is added.
    """

    api_name: str
    proto: type[_ProviderT_co]
    registry: OntologyRegistry
