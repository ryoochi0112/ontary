"""Class-based ontology authoring (spec `typed-authoring` §6): author
Pydantic classes decorated with `@ontology.object(...)`; the SDK derives
the existing descriptor IR (`ObjectTypeDef`/`PropertyDef`) from them. Zero
engine changes -- `GuardedQuery`/`ActionExecutor`/`ObjectStore`/MCP still
only ever see the derived descriptors.

T1 covers object-type authoring (`OntologyObject`, `prop()`,
`Ontology.object()`). This task (T2) adds links (`LinkHandle`,
`Ontology.link()`), assembles the `ScopePolicy` from the per-class
`scope`/`contributor`/`row_visibility` kwargs T1 stashed, and exposes the
built `OntologyDef` as `Ontology.definition` (+ `Ontology.validate()`).
"""

from __future__ import annotations

import builtins
import types
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from typing import (
    Any,
    Literal,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    overload,
)

from pydantic import Field

from ontary.actions import ActionContext, TypedHandler
from ontary.client import OntologyRuntime
from ontary.diagnose import Finding, _collect_findings
from ontary.effects import EffectDispatcher, EffectPayload
from ontary.errors import ValidationFailed
from ontary.functions import FunctionHandler, FunctionRegistry
from ontary.meta import (
    ActionParameterDef,
    ActionTypeDef,
    CapabilityDef,
    Cardinality,
    EffectFieldDef,
    EffectTypeDef,
    FunctionDef,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
    Sensitivity,
    Upcaster,
)
from ontary.model import ActionParams as ActionParams
from ontary.model import CapabilityHandle as CapabilityHandle
from ontary.model import EffectHandle as EffectHandle
from ontary.model import LinkHandle as LinkHandle
from ontary.model import OntologyObject as OntologyObject
from ontary.model import _class_stamp as _class_stamp
from ontary.model import hydrate as hydrate
from ontary.ontology import OntologyDef
from ontary.scope import RowVisibilityFn, ScopePolicy, ScopeRule
from ontary.store import Store
from ontary.typesys import PropertyType

__all__ = [
    "ActionParams",
    "CapabilityHandle",
    "EffectHandle",
    "LinkHandle",
    "Ontology",
    "OntologyObject",
    "hydrate",
    "prop",
    "ref",
    "scope_ref",
    "target",
]

# Author field names reserved for OntologyObject's own hydration metadata
# (spec §8 edge case).
_RESERVED_FIELD_NAMES = frozenset({"redacted_fields", "lineage"})

# Annotation -> PropertyType for the scalar types the mapping recognizes
# directly (spec §6); `dict[...]`/`list[...]`/`Any` are handled separately
# below since they're generic/singleton, not a fixed set of types.
_ANNOTATION_MAP: dict[Any, PropertyType] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
    date: "date",
    datetime: "datetime",
}




def prop(
    *,
    primary_key: bool = False,
    sensitivity: Sensitivity | None = None,
    scope_level: str | None = None,
    required: bool | None = None,
    property_type: PropertyType | None = None,
    choices: Sequence[str] | None = None,
    **field_kwargs: Any,
) -> Any:
    """`pydantic.Field(...)` plus ontology metadata, stashed in
    `json_schema_extra["ontary"]` for `Ontology.object()` to read back.
    Everything else (default, description, ...) passes straight through
    to `Field`, so plain annotated fields and bare `Field(...)` keep
    working on the same class (AC3).
    """
    ontary_meta: dict[str, Any] = {"primary_key": primary_key}
    if sensitivity is not None:
        ontary_meta["sensitivity"] = sensitivity
    if scope_level is not None:
        ontary_meta["scope_level"] = scope_level
    if required is not None:
        ontary_meta["required"] = required
    if property_type is not None:
        ontary_meta["property_type"] = property_type
    if choices is not None:
        ontary_meta["choices"] = choices

    extra = dict(field_kwargs.pop("json_schema_extra", None) or {})
    extra["ontary"] = ontary_meta
    return Field(json_schema_extra=extra, **field_kwargs)


def _field_ontary_meta(annotation_extra: Any) -> dict[str, Any]:
    if isinstance(annotation_extra, dict):
        meta = annotation_extra.get("ontary")
        if isinstance(meta, dict):
            return meta
    return {}


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """`X | None` -> `(X, True)`; anything else -> `(annotation, False)`.
    Only unwraps a plain two-armed Optional (spec §6); a wider union isn't
    a supported annotation shape.
    """
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        args = get_args(annotation)
        if len(args) == 2 and type(None) in args:
            other = args[0] if args[1] is type(None) else args[1]
            return other, True
    return annotation, False


def _property_type_for(annotation: Any, field_name: str, class_name: str) -> PropertyType:
    if annotation in _ANNOTATION_MAP:
        return _ANNOTATION_MAP[annotation]
    if annotation is Any:
        return "json"
    origin = get_origin(annotation)
    if origin in (dict, list) or annotation in (dict, list):
        return "json"
    raise ValidationFailed(
        f"{class_name}.{field_name}: unmappable annotation {annotation!r} "
        "(accepted: str, int, float, bool, datetime.date, datetime.datetime, dict[...], "
        "list[...], Any -- or override with prop(property_type=...))",
        code="ONTOLOGY_INVALID",
    )


def _repair_optional_default(field_info: Any, is_optional: bool) -> bool:
    """Fix up an Optional-annotated field an author left with no explicit
    default: without one, Pydantic still treats it as required at
    construction (`X | None` doesn't imply `= None`). Returns whether the
    field was mutated -- the caller collects that into its `needs_rebuild`
    flag and calls `model_rebuild(force=True)` once. The one fixup all
    three derive loops (`_derive_properties`/`_derive_action_params`/
    `_derive_effect_payload`) share."""
    if is_optional and field_info.is_required():
        field_info.default = None
        return True
    return False


def _derive_properties(cls: type["OntologyObject"]) -> tuple[list[PropertyDef], str]:
    """Introspects `cls.model_fields` -> `(PropertyDef list, primary_key)`.
    Raises `ValidationFailed` with code `ONTOLOGY_INVALID` for a reserved field name, an
    unmappable annotation, an AC6 sensitivity/Optional mismatch, or a
    primary-key count other than exactly one.

    Also fixes up any Optional-annotated field an author left with no
    explicit default: without one, Pydantic still treats it as required
    at construction (`X | None` doesn't imply `= None` the way it would
    in some other typed-model libraries) -- which would make a sparse/
    absent-and-restricted stored value fail typed hydration (spec §6 "T3
    hydration", AC6). `cls.model_rebuild(force=True)` regenerates the
    validator after the mutation; a no-op call when nothing changed would
    still be cheap, but is skipped entirely unless needed.
    """
    props: list[PropertyDef] = []
    primary_keys: list[str] = []
    needs_rebuild = False
    for field_name, field_info in cls.model_fields.items():
        if field_name in _RESERVED_FIELD_NAMES:
            raise ValidationFailed(
                f"{cls.__name__}.{field_name}: reserved for OntologyObject "
                "hydration metadata (redacted_fields/lineage), not usable "
                "as an author field name",
                code="ONTOLOGY_INVALID",
            )

        meta = _field_ontary_meta(field_info.json_schema_extra)
        annotation, is_optional = _unwrap_optional(field_info.annotation)
        property_type = meta.get("property_type") or _property_type_for(
            annotation, field_name, cls.__name__
        )
        choices = meta.get("choices")
        sensitivity = meta.get("sensitivity") or Sensitivity()
        restricted = not sensitivity.human_visible or not sensitivity.ai_usable
        if restricted and not is_optional:
            raise ValidationFailed(
                f"{cls.__name__}.{field_name}: restricted sensitivity "
                "(human_visible=False or ai_usable=False) requires an "
                "Optional annotation (AC6)",
                code="ONTOLOGY_INVALID",
            )

        needs_rebuild = _repair_optional_default(field_info, is_optional) or needs_rebuild

        required = meta.get("required")
        if required is None:
            required = not is_optional

        if meta.get("primary_key"):
            primary_keys.append(field_name)

        props.append(
            PropertyDef(
                name=field_name,
                type=property_type,
                choices=tuple(choices) if choices is not None else None,
                required=required,
                sensitivity=sensitivity,
                scope_level=meta.get("scope_level"),
            )
        )

    if len(primary_keys) != 1:
        raise ValidationFailed(
            f"{cls.__name__}: expected exactly one prop(primary_key=True) "
            f"field, found {len(primary_keys)}: {primary_keys}",
            code="ONTOLOGY_INVALID",
        )
    if needs_rebuild:
        cls.model_rebuild(force=True)
    return props, primary_keys[0]



_T = TypeVar("_T", bound=OntologyObject)
_P = TypeVar("_P", bound=ActionParams)


def _marker(
    cls: type["OntologyObject"],
    semantics: str | None,
    *,
    required: bool | None = None,
    **field_kwargs: Any,
) -> Any:
    ontary_meta: dict[str, Any] = {"refers_to_cls": cls, "semantics": semantics}
    if required is not None:
        ontary_meta["required"] = required
    extra = dict(field_kwargs.pop("json_schema_extra", None) or {})
    extra["ontary"] = ontary_meta
    return Field(json_schema_extra=extra, **field_kwargs)


def target(cls: type["OntologyObject"], **field_kwargs: Any) -> Any:
    """Field marker on an `ActionParams` field: derives
    `ActionParameterDef.refers_to=<cls's registered api_name>` and
    `scope_semantics="target"` (spec §6). The field's annotation must be
    `str` (or `str | None`) -- checked at `@ontology.action` derivation
    time, not here (a bare `Field(...)` call can't see the annotation it's
    assigned to).
    """
    return _marker(cls, "target", **field_kwargs)


def scope_ref(cls: type["OntologyObject"], **field_kwargs: Any) -> Any:
    """Field marker on an `ActionParams` field: derives
    `ActionParameterDef.refers_to=<cls's registered api_name>` and
    `scope_semantics="scope"` (spec §6). Same `str`-annotation requirement
    as `target()`.
    """
    return _marker(cls, "scope", **field_kwargs)


def ref(cls: type["OntologyObject"], **field_kwargs: Any) -> Any:
    """Field marker on an `ActionParams` field: derives
    `ActionParameterDef.refers_to=<cls's registered api_name>` only --
    `scope_semantics` stays `None` (not a scope-enforcement target/scope
    param, just a plain reference to another object type). Same
    `str`-annotation requirement as `target()`/`scope_ref()`.
    """
    return _marker(cls, None, **field_kwargs)



def _api_name_for_registered(
    cls: type["OntologyObject"], registry: OntologyRegistry, what: str
) -> str:
    """Resolves a decorated `OntologyObject` subclass to its registered
    `api_name`, using `_class_stamp` for the identity-stamp read, and
    requiring the stamped registry to be `registry`, by identity.
    Fail-closed `ValidationFailed` with code `ONTOLOGY_INVALID` for an
    undecorated class or one registered on a different `Ontology`.
    """
    name, cls_registry = _class_stamp(cls)
    if name is None or cls_registry is None:
        raise ValidationFailed(
            f"{cls.__name__!r} is not decorated with @ontology.object(...) "
            f"-- cannot use it as {what}",
            code="ONTOLOGY_INVALID",
        )
    if cls_registry is not registry:
        raise ValidationFailed(
            f"{cls.__name__!r} (api_name {name!r}) is registered on a "
            f"different Ontology -- cannot use it as {what}",
            code="ONTOLOGY_INVALID",
        )
    return name


def _derive_action_params(
    cls: type[ActionParams], registry: OntologyRegistry
) -> list[ActionParameterDef]:
    """Introspects `cls.model_fields` -> `list[ActionParameterDef]` (spec
    §6): plain annotations use the same scalar mapping as `OntologyObject`
    properties; `target()`/`scope_ref()`-marked fields derive `refers_to`/
    `scope_semantics` instead (and must be `str`-annotated -- checked here,
    the earliest point the annotation is visible). Required rule mirrors
    `_derive_properties`: non-Optional -> required, with a `prop()`-style
    `required=` override on the marker itself.
    """
    params: list[ActionParameterDef] = []
    needs_rebuild = False
    for field_name, field_info in cls.model_fields.items():
        meta = _field_ontary_meta(field_info.json_schema_extra)
        annotation, is_optional = _unwrap_optional(field_info.annotation)

        needs_rebuild = _repair_optional_default(field_info, is_optional) or needs_rebuild

        refers_to_cls = meta.get("refers_to_cls")
        scope_semantics = cast(
            "Literal['target', 'scope'] | None", meta.get("semantics")
        )
        if refers_to_cls is not None:
            if annotation is not str:
                raise ValidationFailed(
                    f"{cls.__name__}.{field_name}: target()/scope_ref() "
                    "markers require a str (or str | None) annotation, got "
                    f"{field_info.annotation!r}",
                    code="ONTOLOGY_INVALID",
                )
            refers_to = _api_name_for_registered(
                refers_to_cls, registry, f"{cls.__name__}.{field_name}'s marker class"
            )
            property_type: PropertyType = "str"
        else:
            refers_to = None
            property_type = _property_type_for(annotation, field_name, cls.__name__)

        required = meta.get("required")
        if required is None:
            required = not is_optional

        params.append(
            ActionParameterDef(
                name=field_name,
                type=property_type,
                required=required,
                refers_to=refers_to,
                scope_semantics=scope_semantics,
            )
        )

    if needs_rebuild:
        cls.model_rebuild(force=True)
    return params


_C = TypeVar("_C", bound=OntologyObject)
_F = TypeVar("_F", bound=OntologyObject)
_ProviderT_co = TypeVar("_ProviderT_co", covariant=True)
_EffectT_co = TypeVar("_EffectT_co", bound=EffectPayload, covariant=True)



def _effect_field_type_name(annotation: Any) -> str:
    """Return a compact, JSON-safe display name for effect field IR.

    Effect descriptors describe payload structure without retaining Python
    annotation objects. Builtins use their ordinary names (`str`); parameterized
    forms use Python's stable spelling (`list[str]`) with an incidental
    `typing.` prefix removed. Validation remains Pydantic's responsibility --
    unlike ontology properties, effect data is not restricted to the SDK's
    scalar property type system.
    """
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def _derive_effect_payload(cls: type[EffectPayload]) -> list[EffectFieldDef]:
    """Derive serializable field IR with the action-params requiredness rule.

    In particular, `X | None` is optional even when the author omitted
    `= None`; as in `_derive_action_params`, the Pydantic field is repaired and
    the model rebuilt so construction semantics agree with the declaration.
    The Optional wrapper is omitted from `type_name` because requiredness
    carries that information independently.
    """
    payload: list[EffectFieldDef] = []
    needs_rebuild = False
    for field_name, field_info in cls.model_fields.items():
        annotation, is_optional = _unwrap_optional(field_info.annotation)
        needs_rebuild = _repair_optional_default(field_info, is_optional) or needs_rebuild
        payload.append(
            EffectFieldDef(
                name=field_name,
                type_name=_effect_field_type_name(annotation),
                required=not is_optional,
            )
        )
    if needs_rebuild:
        cls.model_rebuild(force=True)
    return payload


class Ontology:
    """Authoring facade: accumulates an `OntologyRegistry` plus, per
    registered class, its derived `ObjectTypeDef` and the scope/
    contributor/row_visibility kwargs it was decorated with -- no module-
    global state, so two `Ontology` instances never cross-talk (spec §8).
    `.link(...)` derives `LinkTypeDef`s; `.definition` lazily assembles the
    `ScopePolicy` from the stored per-class kwargs and builds the
    `OntologyDef` the client binds to -- built once and cached: further
    `.object()`/`.link()` calls after that first access raise, since the
    registry/policy they'd mutate has already been baked into the returned
    `OntologyDef` (spec §6).
    """

    def __init__(self, name: str, scope_levels: list[str], min_n: int = 3) -> None:
        self.name = name
        self.scope_levels = scope_levels
        self.min_n = min_n
        self.registry = OntologyRegistry()
        self._classes: dict[str, type[OntologyObject]] = {}
        self._object_scope_kwargs: dict[str, dict[str, Any]] = {}
        self._action_handlers: dict[str, tuple[TypedHandler, type[ActionParams]]] = {}
        self._function_handlers: dict[str, FunctionHandler] = {}
        self._definition: OntologyDef | None = None

    def _check_not_frozen(self, what: str) -> None:
        if self._definition is not None:
            raise ValidationFailed(
                f"Ontology {self.name!r}: cannot register {what} after "
                ".definition has already been built (registrations are "
                "frozen once the definition is accessed)",
                code="ONTOLOGY_INVALID",
            )

    @overload
    def capability(
        self,
        proto: type[_ProviderT_co],
        *,
        name: str | None = ...,
        description: str | None = ...,
    ) -> CapabilityHandle[_ProviderT_co]: ...
    @overload
    def capability(
        self,
        proto: Any,
        *,
        name: str | None = ...,
        description: str | None = ...,
    ) -> CapabilityHandle[Any]: ...
    def capability(
        self,
        proto: Any,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> CapabilityHandle[Any]:
        """Declare one outside-world read and return its typed handle.

        The protocol belongs only to authoring/type checking; the registry gets
        a plain `CapabilityDef`, keeping the ontology IR serializable. Unlike
        effect payload classes, protocols are not stamped: the same provider
        interface may intentionally be declared by multiple independent
        ontologies, while each returned handle still carries registry identity.

        **Why two overloads.** A `Protocol` is the single most natural thing to
        declare a capability with -- it is a natural fit for an `LLMClient`
        provider -- but passing one to a plain `proto: type[P]` parameter is a
        `type-abstract` error under `mypy --strict` ("Only concrete class can be
        given where type[X] is expected"), because that annotation promises
        instantiability. This method never instantiates `proto`; it reads
        `__name__` and stores the class on the handle for narrowing. So:
        - a CONCRETE provider class hits the first overload and `P` is inferred,
          needing no annotation: `h = ontology.capability(MyHttpClient)`;
        - a `Protocol` (or ABC) hits the second, with `P` supplied by the
          annotation on the assignment target:
          `LLM: CapabilityHandle[LLMClient] = ontology.capability(LLMClient)`.
        Either way the author writes NO `cast`. The alternative -- keeping the
        single `type[P]` signature and making every Protocol author write
        `cast(Any, LLMClient)` at the declaration site -- puts a wart on the
        front door of this milestone's headline feature for the exact use case
        the spec presents as canonical.
        """
        api_name = name if name is not None else proto.__name__
        self._check_not_frozen(f"capability {api_name!r}")
        self.registry.register_capability(
            CapabilityDef(
                api_name=api_name,
                description=description
                if description is not None
                else f"Provides {api_name}.",
            )
        )
        return CapabilityHandle(
            api_name=api_name, proto=proto, registry=self.registry
        )

    def effect(
        self,
        payload_cls: type[_EffectT_co],
        *,
        api_name: str | None = None,
        description: str | None = None,
    ) -> EffectHandle[_EffectT_co]:
        """Declare one outside-world write payload and return its typed handle.

        Payload classes are identity-stamped exactly like action params classes:
        one class may describe one declaration on one ontology. Crucially the
        stamp read goes through `_class_stamp`, hence `cls.__dict__`, so an
        undecorated subclass does not inherit its parent's declaration and
        silently resolve to the parent's api-name (the M4a T3 regression).
        """
        name = api_name if api_name is not None else payload_cls.__name__
        self._check_not_frozen(f"effect {name!r}")
        if not issubclass(payload_cls, EffectPayload):
            raise ValidationFailed(
                f"{payload_cls.__name__!r} must subclass EffectPayload to be "
                "declared with ontology.effect(...)",
                code="ONTOLOGY_INVALID",
            )

        existing_name, existing_registry = _class_stamp(payload_cls)
        if existing_registry is not None:
            if existing_registry is not self.registry:
                raise ValidationFailed(
                    f"{payload_cls.__name__!r} is already declared as an "
                    "effect payload on another Ontology (a payload class may "
                    "only be declared once, on a single Ontology)",
                    code="ONTOLOGY_INVALID",
                )
            raise ValidationFailed(
                f"{payload_cls.__name__!r} is already declared as effect "
                f"{existing_name!r} (a payload class may be declared once)",
                code="ONTOLOGY_INVALID",
            )

        effect_def = EffectTypeDef(
            api_name=name,
            description=description
            if description is not None
            else f"Emits {name}.",
            payload=_derive_effect_payload(payload_cls),
        )
        self.registry.register_effect_type(effect_def)
        # setattr keeps mypy off the TypeVar-bound class (B010 exempted).
        setattr(payload_cls, "_ontary_api_name", name)  # noqa: B010
        setattr(payload_cls, "_ontary_registry", self.registry)  # noqa: B010
        return EffectHandle(
            api_name=name, payload_cls=payload_cls, registry=self.registry
        )

    def _capability_api_names(
        self,
        handles: Sequence[CapabilityHandle[builtins.object]],
        *,
        declared_on: str,
    ) -> list[str]:
        """Resolve handles only when their registry is THIS ontology's.

        Api-name equality is insufficient: independent ontologies may legally
        declare the same name, and accepting a foreign handle would bind later
        provider lookups to the wrong authoring identity.
        """
        names: list[str] = []
        for handle in handles:
            if handle.registry is not self.registry:
                raise ValidationFailed(
                    f"capability {handle.api_name!r} for {declared_on} is "
                    "registered on a different Ontology",
                    code="ONTOLOGY_INVALID",
                )
            names.append(handle.api_name)
        return names

    def _effect_api_names(
        self,
        handles: Sequence[EffectHandle[EffectPayload]],
        *,
        declared_on: str,
    ) -> list[str]:
        """Effect counterpart to `_capability_api_names`."""
        names: list[str] = []
        for handle in handles:
            if handle.registry is not self.registry:
                raise ValidationFailed(
                    f"effect {handle.api_name!r} for {declared_on} is "
                    "registered on a different Ontology",
                    code="ONTOLOGY_INVALID",
                )
            names.append(handle.api_name)
        return names

    def object(
        self,
        *,
        layer: str,
        owned: bool | dict[str, Any] = False,
        api_name: str | None = None,
        description: str | None = None,
        display_name: str | None = None,
        scope: Any = None,
        contributor: Any = None,
        row_visibility: Any = None,
        version: int = 1,
    ) -> Callable[[type[_C]], type[_C]]:
        """Derives an `ObjectTypeDef` from `model_fields` and registers it.
        `scope`/`contributor`/`row_visibility` are stored per-class and
        assembled into the `ScopePolicy` lazily, by `.definition`.

        `version` (M9b) declares which iteration of this type's shape the code
        describes. Leave it at 1 until you change a declared property under data
        that already exists; then bump it and declare an
        `@ontology.upcaster(cls, from_version=...)` for each step. A changed shape
        WITHOUT a bump is still refused as drift -- the bump is how an author says
        the change was deliberate.
        """

        def decorator(cls: type[_C]) -> type[_C]:
            self._check_not_frozen(f"object type {cls.__name__!r}")
            existing_registry = cls.__dict__.get("_ontary_registry")
            if existing_registry is not None and existing_registry is not self.registry:
                raise ValidationFailed(
                    f"{cls.__name__!r} is already registered on another "
                    "Ontology (a class may only be decorated with "
                    "@ontology.object(...) once, on a single Ontology)",
                    code="ONTOLOGY_INVALID",
                )
            name = api_name if api_name is not None else cls.__name__
            props, primary_key = _derive_properties(cls)
            obj_def = ObjectTypeDef(
                api_name=name,
                display_name=display_name if display_name is not None else name,
                description=description if description is not None else f"A {name}.",
                layer=layer,
                properties=props,
                primary_key=primary_key,
                owned=owned,
                version=version,
            )
            self.registry.register_object_type(obj_def)
            cls._ontary_api_name = name
            cls._ontary_sensitivity = {p.name: p.sensitivity for p in props}
            cls._ontary_registry = self.registry
            self._classes[name] = cls
            self._object_scope_kwargs[name] = {
                "scope": scope,
                "contributor": contributor,
                "row_visibility": row_visibility,
            }
            return cls

        return decorator

    def upcaster(
        self, cls: type[OntologyObject], *, from_version: int
    ) -> Callable[[Upcaster], Upcaster]:
        """Declare how to read a `from_version` row of `cls` as the next version.

        ```python
        @ontology.object(layer="L0", version=2, scope=[...])
        class Widget(OntologyObject):
            id: str = prop(primary_key=True)
            label_v2: str | None = None

        @ontology.upcaster(Widget, from_version=1)
        def widget_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
            payload["label_v2"] = payload.pop("label", None)
            return payload
        ```

        The function is returned unchanged, so it stays directly callable and
        directly testable -- an upcaster is the piece of an evolution most worth
        unit-testing on its own, before any store is involved.

        Registration is per STEP (`n` -> `n+1`); see
        `OntologyRegistry.register_upcaster` for why a v1->v3 jump is refused.
        `ontology.validate()` then checks the chain is complete, so a missing step
        fails at declaration time rather than on the first read of the one row
        that still needs it.
        """

        def decorator(fn: Upcaster) -> Upcaster:
            self._check_not_frozen(f"upcaster for {cls.__name__!r}")
            self.registry.register_upcaster(
                self._registered_api_name(cls), from_version, fn
            )
            return fn

        return decorator

    def _registered_api_name(self, cls: type[OntologyObject]) -> str:
        for name, registered_cls in self._classes.items():
            if registered_cls is cls:
                return name
        raise ValidationFailed(
            f"{cls.__name__!r} is not registered on Ontology {self.name!r} "
            "(decorate it with @ontology.object(...) on THIS ontology "
            "before linking it)",
            code="ONTOLOGY_INVALID",
        )

    def link(
        self,
        api_name: str,
        from_cls: type[_F],
        to_cls: type[_T],
        cardinality: Cardinality | str,
        *,
        description: str | None = None,
        identity_revealing: bool = False,
        owned: bool = False,
    ) -> LinkHandle[_F, _T]:
        """Derives + registers a `LinkTypeDef` between two classes already
        registered on THIS ontology (raises `ValidationFailed` with code
        `ONTOLOGY_INVALID` for a class registered elsewhere or not at all) and returns a
        `LinkHandle` for statically-typed traversal.
        """
        self._check_not_frozen(f"link {api_name!r}")
        from_type = self._registered_api_name(from_cls)
        to_type = self._registered_api_name(to_cls)
        card = cardinality if isinstance(cardinality, Cardinality) else Cardinality(cardinality)
        link_def = LinkTypeDef(
            api_name=api_name,
            from_type=from_type,
            to_type=to_type,
            cardinality=card,
            description=description
            if description is not None
            else f"{from_type} -> {to_type}.",
            identity_revealing=identity_revealing,
            owned=owned,
        )
        self.registry.register_link_type(link_def)
        return LinkHandle(api_name=api_name, from_cls=from_cls, to_cls=to_cls)

    def action(
        self,
        params_cls: type[_P],
        *,
        target: type[OntologyObject],
        roles: list[str],
        display_name: str | None = None,
        description: str | None = None,
        api_name: str | None = None,
        capabilities: Sequence[CapabilityHandle[builtins.object]] = (),
        effects: Sequence[EffectHandle[EffectPayload]] = (),
    ) -> Callable[
        [Callable[[ActionContext, _P], dict[str, Any]]],
        Callable[[ActionContext, _P], dict[str, Any]],
    ]:
        """Derives an `ActionTypeDef` + `ActionParameterDef`s from
        `params_cls` and registers both it and the decorated handler
        (`(ctx: ActionContext, params: params_cls) -> dict[str, Any]`) on
        this `Ontology` (spec `typed-actions.md` §6). `api_name` defaults
        to `params_cls.__name__`; `display_name` defaults to `api_name`.
        `target` must be an `OntologyObject` class already registered on
        THIS ontology (same identity discipline as `.link()`'s endpoints).

        Generic over `_P` (bound to `ActionParams`) so a decorated handler
        can be typed against ITS OWN params class (`(ctx, params:
        EscalateTicketParams) -> ...`), not the erased `TypedHandler`
        shape -- mypy checks the handler body against the concrete class.
        Internally the handler is stored erased (`TypedHandler`, a single
        `cast` at the one place it's stashed in `_action_handlers`) since
        the table holds handlers for many different params classes.
        """
        name = api_name if api_name is not None else params_cls.__name__

        def decorator(
            fn: Callable[[ActionContext, _P], dict[str, Any]]
        ) -> Callable[[ActionContext, _P], dict[str, Any]]:
            self._check_not_frozen(f"action {name!r}")
            existing_registry = params_cls.__dict__.get("_ontary_registry")
            if existing_registry is not None and existing_registry is not self.registry:
                raise ValidationFailed(
                    f"{params_cls.__name__!r} is already registered as an "
                    "action params class on another Ontology (a params "
                    "class may only be declared with @ontology.action(...) "
                    "once, on a single Ontology)",
                    code="ONTOLOGY_INVALID",
                )
            for existing_name, (_, existing_params_cls) in self._action_handlers.items():
                if existing_params_cls is params_cls:
                    raise ValidationFailed(
                        f"{params_cls.__name__!r} is already declared as "
                        f"action {existing_name!r} (a params class may be "
                        "declared once)",
                        code="ONTOLOGY_INVALID",
                    )

            target_type = _api_name_for_registered(
                target, self.registry, f"action {name!r}'s target"
            )
            parameters = _derive_action_params(params_cls, self.registry)
            capability_names = self._capability_api_names(
                capabilities, declared_on=f"action {name!r}"
            )
            effect_names = self._effect_api_names(
                effects, declared_on=f"action {name!r}"
            )
            action_def = ActionTypeDef(
                api_name=name,
                display_name=display_name if display_name is not None else name,
                target_type=target_type,
                executable_by_roles=list(roles),
                description=description
                if description is not None
                else f"Executes {name}.",
                parameters=parameters,
                capabilities=capability_names,
                effects=effect_names,
            )
            self.registry.register_action_type(action_def)  # dup api_name raises
            params_cls._ontary_api_name = name
            params_cls._ontary_registry = self.registry
            self._action_handlers[name] = (cast(TypedHandler, fn), params_cls)
            return fn

        return decorator

    def function(
        self,
        *,
        description: str | None = None,
        input_description: str = "",
        output_description: str = "",
        api_name: str | None = None,
        capabilities: Sequence[CapabilityHandle[builtins.object]] = (),
        effects: Sequence[EffectHandle[EffectPayload]] | None = None,
        audit: bool | None = None,
    ) -> Callable[[FunctionHandler], FunctionHandler]:
        """Derives a `FunctionDef` and declares the decorated handler
        (`(query: BoundQuery, params: dict) -> Any`) on this `Ontology`
        (spec `typed-actions.md` §6/AC8): `api_name` defaults to
        `fn.__name__`. The handler is bound onto the `OntologyDef`'s
        `FunctionRegistry` exactly once, lazily at `.definition` build time
        -- NOT here -- since `FunctionRegistry` lives on `OntologyDef`
        (mirrors how `.action()`'s handlers are only bound once a runtime
        is constructed against a store). Duplicate `api_name` raises the
        same `ValidationFailed` with code `ONTOLOGY_INVALID` `.register_function` already raises
        for any other duplicate descriptor; declaring after `.definition`
        has been accessed raises the same freeze error as `.object()`/
        `.link()`/`.action()`.
        """
        name = api_name if api_name is not None else None

        def decorator(fn: FunctionHandler) -> FunctionHandler:
            resolved_name = name if name is not None else fn.__name__
            self._check_not_frozen(f"function {resolved_name!r}")
            if effects is not None:
                raise ValidationFailed(
                    f"function {resolved_name!r} cannot declare effects "
                    "(functions may read outside-world capabilities but may "
                    "not write outside the ontology)",
                    code="ONTOLOGY_INVALID",
                )
            capability_names = self._capability_api_names(
                capabilities, declared_on=f"function {resolved_name!r}"
            )
            fn_def = FunctionDef(
                api_name=resolved_name,
                description=description
                if description is not None
                else f"Computes {resolved_name}.",
                input_description=input_description,
                output_description=output_description,
                capabilities=capability_names,
                audit=audit,
            )
            self.registry.register_function(fn_def)  # dup api_name raises
            self._function_handlers[resolved_name] = fn
            return fn

        return decorator

    def _build_policy(self) -> ScopePolicy:
        """Assembles a `ScopePolicy` from the per-class `scope`/
        `contributor`/`row_visibility` kwargs stashed by `.object(...)`:
        `scope=[ScopeRule, ...]` -> `policy.rules[api_name]`,
        `scope="unscoped"` -> `policy.unscoped_types`,
        `contributor=[...]` -> `policy.contributor_rules[api_name]`,
        `row_visibility=fn` -> `policy.row_visibility[api_name]`.
        """
        rules: dict[str, list[ScopeRule]] = {}
        unscoped_types: set[str] = set()
        contributor_rules: dict[str, list[ScopeRule]] = {}
        row_visibility: dict[str, RowVisibilityFn] = {}

        for api_name, kwargs in self._object_scope_kwargs.items():
            scope = kwargs.get("scope")
            if scope == "unscoped":
                unscoped_types.add(api_name)
            elif scope is not None:
                rules[api_name] = scope

            contributor = kwargs.get("contributor")
            if contributor is not None:
                contributor_rules[api_name] = contributor

            fn = kwargs.get("row_visibility")
            if fn is not None:
                row_visibility[api_name] = fn

        return ScopePolicy(
            levels=self.scope_levels,
            unscoped_types=unscoped_types,
            rules=rules,
            contributor_rules=contributor_rules,
            row_visibility=row_visibility,
            min_n=self.min_n,
        )

    @property
    def definition(self) -> OntologyDef:
        """Lazily builds and caches the `OntologyDef` this ontology binds
        a client to (registry + assembled `ScopePolicy` + a `FunctionRegistry`
        with every `@ontology.function(...)`-declared handler on THIS
        ontology bound exactly once -- the one place that binding happens,
        since `FunctionRegistry` lives on `OntologyDef`, not on `Ontology`
        itself). Does NOT call `.validate()` -- an author calls
        `.definition.validate()` (or `Ontology.validate()`) once, same flow
        as descriptor authoring today. First access freezes registration:
        further `.object()`/`.link()`/`.action()`/`.function()` calls raise
        (see `_check_not_frozen`).
        """
        if self._definition is None:
            functions = FunctionRegistry(self.registry)
            for fn_name, fn in self._function_handlers.items():
                functions.register(fn_name, fn)
            self._definition = OntologyDef(
                name=self.name,
                registry=self.registry,
                policy=self._build_policy(),
                functions=functions,
                # Typed action handlers ride the definition (C2), mirroring
                # the function binding above. Safe to snapshot here: this
                # first access freezes registration, so the map cannot go
                # stale.
                action_handlers=dict(self._action_handlers),
            )
        return self._definition

    def validate(self) -> None:
        """Convenience: builds `.definition` (if not already built) and
        validates it -- equivalent to `ontology.definition.validate()`."""
        self.definition.validate()

    def _build_diagnostic_policy(self) -> ScopePolicy:
        """Build an unchecked policy snapshot for diagnose's constructor checks.

        ``ScopePolicy`` normally validates ``levels`` and ``min_n`` while it is
        constructed.  ``diagnose`` must still report those defects, so this
        fallback uses Pydantic's unchecked constructor only when the normal
        policy build rejects them.  It never assigns the snapshot to
        ``self._definition``.
        """
        rules: dict[str, Any] = {}
        unscoped_types: set[str] = set()
        contributor_rules: dict[str, Any] = {}
        row_visibility: dict[str, Any] = {}

        for api_name, kwargs in self._object_scope_kwargs.items():
            scope = kwargs.get("scope")
            if scope == "unscoped":
                unscoped_types.add(api_name)
            elif scope is not None:
                rules[api_name] = scope

            contributor = kwargs.get("contributor")
            if contributor is not None:
                contributor_rules[api_name] = contributor

            row_visibility_fn = kwargs.get("row_visibility")
            if row_visibility_fn is not None:
                row_visibility[api_name] = row_visibility_fn

        return ScopePolicy.model_construct(
            levels=list(self.scope_levels),
            unscoped_types=unscoped_types,
            rules=rules,
            contributor_rules=contributor_rules,
            row_visibility=row_visibility,
            min_n=self.min_n,
        )

    def diagnose(self) -> list[Finding]:
        """Return all validation findings without raising mid-sweep.

        If this ontology is still being authored, diagnostics build a
        transient definition from the same pre-freeze structures used by
        ``validate``; the cached definition is left untouched so
        registration can continue afterwards.
        """
        if self._definition is not None:
            definition = self._definition
        else:
            try:
                policy = self._build_policy()
            except Exception:
                policy = self._build_diagnostic_policy()
            definition = OntologyDef(
                name=self.name,
                registry=self.registry,
                policy=policy,
            )
        return _collect_findings(definition)

    def bind(
        self,
        store: Store,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        capabilities: Mapping[CapabilityHandle[Any], builtins.object] | None = None,
        effects: Mapping[EffectHandle[Any], EffectDispatcher] | None = None,
    ) -> OntologyRuntime:
        """Builds an `OntologyRuntime` (spec `typed-actions.md` AC6): ONE
        `GuardedQuery` + ONE `ActionExecutor` bound to `store`, with every
        `@ontology.action(...)`-declared handler on THIS ontology
        auto-bound exactly once. `runtime.for_consumer(consumer)` then
        hands out cheap, consumer-scoped `OntologyClient` views sharing
        that same query/executor.
        """
        return OntologyRuntime(
            self,
            store,
            clock=clock,
            id_factory=id_factory,
            capabilities=capabilities,
            effects=effects,
        )
