"""Class-based ontology authoring: author Pydantic classes decorated with
`@ontology.object(...)`; the SDK derives the existing descriptor IR
(`ObjectTypeDef`/`PropertyDef`) from them. Zero engine changes --
`GuardedQuery`/`ActionExecutor`/`ObjectStore`/MCP still only ever see the
derived descriptors.

Object-type authoring uses `OntologyObject`, `prop()` and
`Ontology.object()`. Link authoring adds links (`LinkHandle`,
`Ontology.link()`), assembles the `ScopePolicy` from the per-class
`scope`/`contributor`/`row_visibility` kwargs that `Ontology.object()`
stashes, and exposes the built `OntologyDef` as `Ontology.definition`,
with `Ontology.validate()` available for explicit validation.
"""

from __future__ import annotations

import builtins
import types
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from typing import (
    Any,
    Literal,
    Protocol,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    overload,
)

from pydantic import BaseModel, Field, RootModel, ValidationError

from ontary.actions import ActionContext, TypedHandler
from ontary.client import OntologyRuntime
from ontary.diagnose import (
    Finding,
    _collect_findings,
    _store_row_findings,
    _stored_rule_findings,
)
from ontary.errors import ValidationFailed
from ontary.functions import (
    BoundQuery,
    FunctionRegistry,
    _dict_handler_refusal,
    _takes_query_only,
)
from ontary.meta import (
    ActionLint,
    ActionParameterDef,
    ActionTypeDef,
    CapabilityDef,
    Cardinality,
    EventLint,
    EventTypeDef,
    FunctionDef,
    FunctionLint,
    LinkTypeDef,
    ObjectLint,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
    PropertyLint,
    RuleDef,
    Sensitivity,
    StructFieldDef,
    TransitionDef,
    default_action_description,
    default_function_description,
    primary_key_sensitivity_violation,
    target_param_mismatch,
)
from ontary.model import ActionParams as ActionParams
from ontary.model import CapabilityHandle as CapabilityHandle
from ontary.model import Event
from ontary.model import FunctionParams as FunctionParams
from ontary.model import LinkHandle as LinkHandle
from ontary.model import OntologyObject as OntologyObject
from ontary.model import _class_stamp as _class_stamp
from ontary.model import hydrate as hydrate
from ontary.ontology import OntologyDef
from ontary.scope import RowVisibilityFn, ScopePolicy, ScopeRule
from ontary.store import Store
from ontary.store.values import Lineage, StoredObject
from ontary.typesys import PropertyType, choice_value

__all__ = [
    "ActionParams",
    "CapabilityHandle",
    "LinkHandle",
    "Ontology",
    "OntologyObject",
    "hydrate",
    "prop",
    "ref",
    "scope_ref",
    "target",
]

# Author field names reserved for OntologyObject's own hydration metadata.
_RESERVED_FIELD_NAMES = frozenset({"redacted_fields", "lineage"})

# Annotation -> PropertyType for the scalar types the mapping recognizes
# directly; `dict[...]`/`list[...]`/`Any` are handled separately
# below since they're generic/singleton, not a fixed set of types.
_ANNOTATION_MAP: dict[Any, PropertyType] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
    date: "date",
    datetime: "datetime",
}


def _normalize_accept(accept: str | Sequence[str] | None) -> tuple[str, ...]:
    if accept is None:
        return ()
    if isinstance(accept, str):
        return (accept,)
    return tuple(dict.fromkeys(accept))


def prop(
    *,
    primary_key: bool = False,
    sensitivity: Sensitivity | None = None,
    scope_level: str | None = None,
    required: bool | None = None,
    property_type: PropertyType | None = None,
    choices: Sequence[str] | None = None,
    transitions: TransitionDef | None = None,
    accept: PropertyLint | Sequence[PropertyLint] | None = None,
    **field_kwargs: Any,
) -> Any:
    """`pydantic.Field(...)` plus ontology metadata, stashed in
    `json_schema_extra["ontary"]` for `Ontology.object()` to read back.
    Everything else (default, description, ...) passes straight through
    to `Field`, so plain annotated fields and bare `Field(...)` keep
    working on the same class.
    """
    ontary_meta: dict[str, Any] = {"primary_key": primary_key, "accept": _normalize_accept(accept)}
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
    if transitions is not None:
        ontary_meta["transitions"] = transitions

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
    Only unwraps a plain two-armed Optional; a wider union isn't
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
        if _contains_model(annotation):
            raise ValidationFailed(
                f"{class_name}.{field_name}: collections of models are not supported; "
                "use a linked object type",
                code="ONTOLOGY_INVALID",
            )
        return "json"
    raise ValidationFailed(
        f"{class_name}.{field_name}: unmappable annotation {annotation!r} "
        "(accepted: str, int, float, bool, datetime.date, datetime.datetime, dict[...], "
        "list[...], Any, a string Enum or Literal[...] -- or override with "
        "prop(property_type=...))",
        code="ONTOLOGY_INVALID",
    )


def _contains_model(annotation: Any) -> bool:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return True
    return any(_contains_model(arg) for arg in get_args(annotation))


def _choice_members(
    annotation: Any, field_name: str, class_name: str
) -> tuple[str, ...] | None:
    """The string values a choice annotation allows, or ``None`` when
    ``annotation`` is not an ``Enum`` subclass or a ``Literal[...]`` (#42).

    An ``Enum`` contributes its member values in declaration order; a
    ``Literal`` contributes its arguments. Either maps to ``type="str"`` plus
    ``choices``, so every member must be a string.
    """
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        members: tuple[Any, ...] = tuple(member.value for member in annotation)
    elif get_origin(annotation) is Literal:
        members = get_args(annotation)
    else:
        return None
    non_strings = [member for member in members if not isinstance(member, str)]
    if non_strings:
        raise ValidationFailed(
            f"{class_name}.{field_name}: choice annotation {annotation!r} has "
            f"non-string members {non_strings!r} -- a choice property stores "
            "string values, so give every Enum member or Literal argument a "
            "string value",
            code="ONTOLOGY_INVALID",
        )
    return members


def _struct_fields(
    annotation: Any, field_name: str, class_name: str, *, opaque_json: bool = False
) -> tuple[StructFieldDef, ...] | None:
    """Derive a flat BaseModel's declared scalar fields, if applicable."""
    if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
        return None
    path = f"{class_name}.{field_name}"
    if issubclass(annotation, OntologyObject):
        raise ValidationFailed(
            f"{path}: an OntologyObject cannot be a struct; use a link",
            code="ONTOLOGY_INVALID",
        )
    if issubclass(annotation, RootModel):
        raise ValidationFailed(
            f"{path}: a RootModel cannot be a struct; "
            "annotate the inner type directly, or use a flat BaseModel",
            code="ONTOLOGY_INVALID",
        )
    fields: list[StructFieldDef] = []
    for inner_name, inner_info in annotation.model_fields.items():
        inner_path = f"{path}.{inner_name}"
        if not opaque_json and (
            inner_info.default_factory is not None
            or (not inner_info.is_required() and inner_info.default is not None)
        ):
            raise ValidationFailed(
                f"{inner_path}: struct inner field '{inner_name}' has a default; "
                "a struct inner field may only default to None "
                "(make it Optional with `= None`, or set the value at the call site)",
                code="ONTOLOGY_INVALID",
            )
        if any(
            alias is not None and alias != inner_name
            for alias in (
                inner_info.alias,
                inner_info.validation_alias,
                inner_info.serialization_alias,
            )
        ):
            raise ValidationFailed(
                f"{inner_path}: inner aliases are unsupported; use the field name",
                code="ONTOLOGY_INVALID",
            )
        if _field_ontary_meta(inner_info.json_schema_extra):
            raise ValidationFailed(
                f"{inner_path}: inner prop() metadata is unsupported; "
                "mark the whole property",
                code="ONTOLOGY_INVALID",
            )
        inner_annotation, inner_optional = _unwrap_optional(inner_info.annotation)
        if isinstance(inner_annotation, type) and issubclass(inner_annotation, BaseModel):
            if issubclass(inner_annotation, OntologyObject):
                raise ValidationFailed(
                    f"{inner_path}: an OntologyObject cannot be a struct; use a link",
                    code="ONTOLOGY_INVALID",
                )
            raise ValidationFailed(
                f"{inner_path}: nested structs are unsupported; "
                "flatten it, or use a linked object type",
                code="ONTOLOGY_INVALID",
            )
        choices = _choice_members(inner_annotation, inner_name, path)
        if choices is not None:
            inner_type: PropertyType = "str"
        else:
            inner_type = _property_type_for(inner_annotation, inner_name, path)
        if inner_type == "json":
            raise ValidationFailed(
                f"{inner_path}: json/dict/list/Any inner fields are unsupported; "
                "use a scalar field",
                code="ONTOLOGY_INVALID",
            )
        fields.append(
            StructFieldDef(
                name=inner_name,
                type=inner_type,
                choices=choices,
                required=not inner_optional,
            )
        )
    return tuple(fields)


def _apply_struct_type_override(
    fields: tuple[StructFieldDef, ...] | None,
    meta: dict[str, Any],
    field_name: str,
    class_name: str,
) -> tuple[StructFieldDef, ...] | None:
    if fields is None:
        return None
    override = meta.get("property_type")
    if override == "json":
        return None
    if override is not None:
        raise ValidationFailed(
            f"{class_name}.{field_name}: remove property_type "
            "(a model annotation derives a struct), or use "
            "property_type='json' to store it opaque",
            code="ONTOLOGY_INVALID",
        )
    return fields


def _refuse_double_choices(meta: dict[str, Any], field_name: str, class_name: str) -> None:
    if meta.get("choices") is not None:
        raise ValidationFailed(
            f"{class_name}.{field_name}: the Enum/Literal annotation already "
            "declares the choices -- drop prop(choices=...)",
            code="ONTOLOGY_INVALID",
        )


def _repair_optional_default(field_info: Any, is_optional: bool) -> bool:
    """Fix up an Optional-annotated field an author left with no explicit
    default: without one, Pydantic still treats it as required at
    construction (`X | None` doesn't imply `= None`). Returns whether the
    field was mutated -- the caller collects that into its `needs_rebuild`
    flag and calls `model_rebuild(force=True)` once. The one fixup both
    derive loops (`_derive_properties`/`_derive_action_params`)
    share."""
    if is_optional and field_info.is_required():
        field_info.default = None
        return True
    return False


def _normalized_transitions(
    graph: TransitionDef | None, choices: tuple[str, ...] | None
) -> TransitionDef | None:
    if graph is None:
        return None
    return TransitionDef(
        initial=tuple(choice_value(state, choices) for state in graph.initial),
        moves={
            choice_value(source, choices): tuple(
                choice_value(target, choices) for target in targets
            )
            for source, targets in graph.moves.items()
        },
    )


def _check_event_metadata(
    class_name: str, field_name: str, meta: dict[str, Any], *, event: bool
) -> None:
    if not event:
        return
    for marker in ("primary_key", "transitions", "scope_level"):
        if (marker == "primary_key" and meta.get(marker)) or (
            marker != "primary_key" and meta.get(marker) is not None
        ):
            raise ValidationFailed(
                f"{class_name}.{field_name}: prop({marker}=...) "
                "is not supported on events",
                code="ONTOLOGY_INVALID",
            )


def _check_primary_key_sensitivity(
    class_name: str, field_name: str, sensitivity: Sensitivity, *, primary_key: bool
) -> None:
    if primary_key:
        body = primary_key_sensitivity_violation(sensitivity)
        if body is not None:
            raise ValidationFailed(
                f"{class_name}.{field_name}: {body}", code="ONTOLOGY_INVALID"
            )


def _derive_properties(
    cls: type[OntologyObject] | type[Event], *, event: bool = False
) -> tuple[list[PropertyDef], str]:
    """Introspects `cls.model_fields` -> `(PropertyDef list, primary_key)`.
    Raises `ValidationFailed` with code `ONTOLOGY_INVALID` for a reserved field name, an
    unmappable annotation, a sensitivity/Optional mismatch, or a
    primary-key count other than exactly one.

    Event mode refuses object identity/state/scope metadata and returns an
    empty primary-key string; events carry payload fields without an identity.

    Also fixes up any Optional-annotated field an author left with no
    explicit default: without one, Pydantic still treats it as required
    at construction (`X | None` doesn't imply `= None` the way it would
    in some other typed-model libraries) -- which would make a sparse/
    absent-and-restricted stored value fail typed hydration.
    `cls.model_rebuild(force=True)` regenerates the validator after the mutation; a no-op call when nothing changed would
    still be cheap, but is skipped entirely unless needed.
    """
    props: list[PropertyDef] = []
    primary_keys: list[str] = []
    needs_rebuild = False
    for field_name, field_info in cls.model_fields.items():
        if not event and field_name in _RESERVED_FIELD_NAMES:
            raise ValidationFailed(
                f"{cls.__name__}.{field_name}: reserved for OntologyObject "
                "hydration metadata (redacted_fields/lineage), not usable "
                "as an author field name",
                code="ONTOLOGY_INVALID",
            )

        meta = _field_ontary_meta(field_info.json_schema_extra)
        _check_event_metadata(cls.__name__, field_name, meta, event=event)
        annotation, is_optional = _unwrap_optional(field_info.annotation)
        fields = _apply_struct_type_override(
            _struct_fields(
                annotation, field_name, cls.__name__,
                opaque_json=meta.get("property_type") == "json",
            ), meta, field_name, cls.__name__
        )
        if fields is not None:
            if meta.get("primary_key"):
                raise ValidationFailed(
                    f"{cls.__name__}.{field_name}: a struct cannot be the primary key; "
                    "use a string primary key",
                    code="ONTOLOGY_INVALID",
                )
            if meta.get("choices") is not None:
                raise ValidationFailed(
                    f"{cls.__name__}.{field_name}: a struct declares its inner choices; "
                    "drop prop(choices=...)",
                    code="ONTOLOGY_INVALID",
                )
            property_type: PropertyType = "struct"
            choices = None
        else:
            choices = _choice_members(annotation, field_name, cls.__name__)
        if fields is None and choices is not None:
            _refuse_double_choices(meta, field_name, cls.__name__)
            if meta.get("primary_key"):
                raise ValidationFailed(
                    f"{cls.__name__}.{field_name}: a choice annotation cannot be "
                    "the primary key -- a choice is a value, not an identity; "
                    "annotate the primary key as str",
                    code="ONTOLOGY_INVALID",
                )
            property_type = meta.get("property_type") or "str"
        elif fields is None:
            if get_origin(annotation) in (dict, list) and _contains_model(annotation):
                _property_type_for(annotation, field_name, cls.__name__)
            property_type = meta.get("property_type") or _property_type_for(
                annotation, field_name, cls.__name__
            )
            choices = meta.get("choices")
        sensitivity = meta.get("sensitivity") or Sensitivity()
        _check_primary_key_sensitivity(
            cls.__name__, field_name, sensitivity, primary_key=bool(meta.get("primary_key"))
        )
        restricted = not sensitivity.human_visible or not sensitivity.ai_usable
        if restricted and not is_optional:
            raise ValidationFailed(
                f"{cls.__name__}.{field_name}: restricted sensitivity "
                "(human_visible=False or ai_usable=False) requires an "
                "Optional annotation",
                code="ONTOLOGY_INVALID",
            )

        needs_rebuild = _repair_optional_default(field_info, is_optional) or needs_rebuild

        required = meta.get("required")
        if required is None:
            required = not is_optional

        if meta.get("primary_key"):
            primary_keys.append(field_name)
            if meta.get("transitions") is not None:
                raise ValidationFailed(
                    f"{cls.__name__}.{field_name}: transitions cannot govern a primary key; "
                    "move transitions to a choice property",
                    code="ONTOLOGY_INVALID",
                )

        graph = _normalized_transitions(meta.get("transitions"), choices)

        props.append(
            PropertyDef(
                name=field_name,
                type=property_type,
                choices=tuple(choices) if choices is not None else None,
                transitions=graph,
                fields=fields,
                required=required,
                sensitivity=sensitivity,
                scope_level=meta.get("scope_level"),
                accept=meta.get("accept", ()),
            )
        )

    if not event and len(primary_keys) != 1:
        raise ValidationFailed(
            f"{cls.__name__}: expected exactly one prop(primary_key=True) "
            f"field, found {len(primary_keys)}: {primary_keys}",
            code="ONTOLOGY_INVALID",
        )
    if needs_rebuild:
        cls.model_rebuild(force=True)
    return props, primary_keys[0] if primary_keys else ""


_T = TypeVar("_T", bound=OntologyObject)
_P = TypeVar("_P", bound=ActionParams)
_E = TypeVar("_E", bound=Event)
_FP = TypeVar("_FP", bound=FunctionParams)
_R = TypeVar("_R")
_H = TypeVar("_H", bound=Callable[..., Any])


class _NoClassFunctionDecorator(Protocol):
    def __call__(self, fn: Callable[[BoundQuery], _R]) -> Callable[[BoundQuery], _R]: ...


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
    `scope_semantics="target"`. The field's annotation must be
    `str` (or `str | None`) -- checked at `@ontology.action` derivation
    time, not here (a bare `Field(...)` call can't see the annotation it's
    assigned to).
    """
    return _marker(cls, "target", **field_kwargs)


def scope_ref(cls: type["OntologyObject"], **field_kwargs: Any) -> Any:
    """Field marker on an `ActionParams` field: derives
    `ActionParameterDef.refers_to=<cls's registered api_name>` and
    `scope_semantics="scope"`. Same `str`-annotation requirement
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


def _derive_params(
    cls: type[ActionParams] | type[FunctionParams],
    registry: OntologyRegistry,
    *,
    owner: Literal["action", "function"],
) -> list[ActionParameterDef]:
    """Derive action or function parameters from model fields. Both share
    the scalar, choice, struct, Optional, and ref rules; functions reject
    the action-only target and scope markers.
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
        if owner == "function" and scope_semantics is not None:
            raise ValidationFailed(
                f"{cls.__name__}.{field_name}: functions take ref(); "
                "target()/scope_ref() are action-only",
                code="ONTOLOGY_INVALID",
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
            choices = None
            fields = None
        else:
            refers_to = None
            struct_fields = _struct_fields(
                annotation, field_name, cls.__name__,
                opaque_json=meta.get("property_type") == "json",
            )
            fields = _apply_struct_type_override(
                struct_fields, meta, field_name, cls.__name__,
            )
            if fields is not None:
                if meta.get("choices") is not None:
                    raise ValidationFailed(
                        f"{cls.__name__}.{field_name}: a struct declares its inner choices; "
                        "drop prop(choices=...)",
                        code="ONTOLOGY_INVALID",
                    )
                property_type = "struct"
                choices = None
            else:
                choices = _choice_members(annotation, field_name, cls.__name__)
            if fields is None and choices is not None:
                _refuse_double_choices(meta, field_name, cls.__name__)
                property_type = "str"
            elif fields is None:
                property_type = (
                    "json" if struct_fields is not None
                    else _property_type_for(annotation, field_name, cls.__name__)
                )
                declared = meta.get("choices")
                choices = tuple(declared) if declared is not None else None

        required = meta.get("required")
        if required is None:
            # A Python default makes a parameter optional, as in pydantic.
            # Scope markers stay required: the scope gate reads the caller's
            # dict, so an omitted marker would skip it.
            required = not is_optional and (
                scope_semantics is not None or field_info.is_required()
            )

        params.append(
            ActionParameterDef(
                name=field_name,
                type=property_type,
                description=field_info.description,
                choices=choices,
                fields=fields,
                required=required,
                refers_to=refers_to,
                scope_semantics=scope_semantics,
            )
        )

    if needs_rebuild:
        cls.model_rebuild(force=True)
    return params


def _derive_action_params(
    cls: type[ActionParams], registry: OntologyRegistry
) -> list[ActionParameterDef]:
    return _derive_params(cls, registry, owner="action")


_C = TypeVar("_C", bound=OntologyObject)
_F = TypeVar("_F", bound=OntologyObject)
_ProviderT_co = TypeVar("_ProviderT_co", covariant=True)


# The four `Cardinality` member names, spelled out so a misspelled string is a
# mypy error at the call site (ontary#39) -- runtime still accepts any `str`
# that names a member, only the annotation narrows.
CardinalityName = Literal["ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_ONE", "MANY_TO_MANY"]

_SCOPE_RULE_TYPES: tuple[type, ...] = get_args(ScopeRule)
_SCOPE_RULE_NAMES = ", ".join(t.__name__ for t in _SCOPE_RULE_TYPES)


def _refuse_kwarg(cls_name: str, kwarg: str, given: object, accepted: str) -> ValidationFailed:
    return ValidationFailed(
        f"{cls_name!r}: {kwarg}={given!r} is not accepted by "
        f"@ontology.object(...); {kwarg}= takes {accepted}",
        code="ONTOLOGY_INVALID",
    )


def _check_rule_sequence(cls_name: str, kwarg: str, given: object, accepted: str) -> None:
    """Refuse anything but a non-str sequence whose every element is a
    `ScopeRule`; name the offending element when one element is wrong."""
    if isinstance(given, (str, bytes)) or not isinstance(given, Sequence):
        hint = f"; wrap a single rule in a list: {kwarg}=[{given!r}]" if isinstance(
            given, _SCOPE_RULE_TYPES
        ) else ""
        raise _refuse_kwarg(cls_name, kwarg, given, accepted + hint)
    for element in given:
        if not isinstance(element, _SCOPE_RULE_TYPES):
            raise ValidationFailed(
                f"{cls_name!r}: {kwarg}= element {element!r} is not a ScopeRule "
                f"({_SCOPE_RULE_NAMES}); {kwarg}= takes {accepted}",
                code="ONTOLOGY_INVALID",
            )


def _check_scope_kwargs(
    cls_name: str, scope: object, contributor: object, row_visibility: object
) -> None:
    """Shape-check the `scope`/`contributor`/`row_visibility` kwargs at
    decoration time (ontary#39) so a typo is refused where it was written,
    with `ONTOLOGY_INVALID`, instead of surfacing later from `.definition`
    as a raw pydantic error deep inside `ScopePolicy`."""
    rules_accepted = f"a list of ScopeRule ({_SCOPE_RULE_NAMES})"
    if scope is not None and scope != "unscoped":
        _check_rule_sequence(
            cls_name, "scope", scope, f'the literal "unscoped" or {rules_accepted}'
        )
    if contributor is not None:
        hint = ' (did you mean scope="unscoped"?)' if contributor == "unscoped" else ""
        _check_rule_sequence(cls_name, "contributor", contributor, rules_accepted + hint)
    if row_visibility is not None and not callable(row_visibility):
        raise _refuse_kwarg(
            cls_name,
            "row_visibility",
            row_visibility,
            "a callable (store, consumer, obj_type, row) -> bool",
        )


class Ontology:
    """Authoring facade: accumulates an `OntologyRegistry` plus, per
    registered class, its derived `ObjectTypeDef` and the scope/
    contributor/row_visibility kwargs it was decorated with -- no module-
    global state, so two `Ontology` instances never cross-talk.
    `.link(...)` derives `LinkTypeDef`s; `.definition` lazily assembles the
    `ScopePolicy` from the stored per-class kwargs and builds the
    `OntologyDef` the client binds to -- built once and cached: further
    `.object()`/`.link()` calls after that first access raise, since the
    registry/policy they'd mutate has already been baked into the returned
    `OntologyDef`.
    """

    def __init__(self, name: str, scope_levels: list[str], min_n: int = 3) -> None:
        self.name = name
        self.scope_levels = scope_levels
        self.min_n = min_n
        self.registry = OntologyRegistry()
        self._classes: dict[str, type[OntologyObject]] = {}
        self._object_scope_kwargs: dict[str, dict[str, Any]] = {}
        self._action_handlers: dict[str, tuple[TypedHandler, type[ActionParams]]] = {}
        self._function_handlers: dict[
            str,
            tuple[
                Callable[..., Any],
                type[FunctionParams] | None,
            ],
        ] = {}
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
        a plain `CapabilityDef`, keeping the ontology IR serializable.
        Protocols are not stamped: the same provider interface may
        intentionally be declared by multiple independent ontologies, while
        each returned handle still carries registry identity.

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
        front door of this feature for the canonical use case.
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

    def object(
        self,
        *,
        layer: str,
        owned: bool | dict[str, Any] = False,
        api_name: str | None = None,
        description: str | None = None,
        display_name: str | None = None,
        scope: Literal["unscoped"] | Sequence[ScopeRule] | None = None,
        contributor: Sequence[ScopeRule] | None = None,
        row_visibility: RowVisibilityFn | None = None,
        accept: ObjectLint | Sequence[ObjectLint] | None = None,
        snapshot: bool = False,
    ) -> Callable[[type[_C]], type[_C]]:
        """Derives an `ObjectTypeDef` from `model_fields` and registers it.
        `scope`/`contributor`/`row_visibility` are shape-checked at decoration
        time (a typo raises `ValidationFailed` with code `ONTOLOGY_INVALID`
        naming the class and the kwarg), then stored per-class and assembled
        into the `ScopePolicy` lazily, by `.definition`.
        """

        def decorator(cls: type[_C]) -> type[_C]:
            self._check_not_frozen(f"object type {cls.__name__!r}")
            _check_scope_kwargs(cls.__name__, scope, contributor, row_visibility)
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
                accept=_normalize_accept(accept),
                snapshot=snapshot,
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

    def event(
        self,
        *,
        description: str | None = None,
        api_name: str | None = None,
        accept: EventLint | Sequence[EventLint] = (),
    ) -> Callable[[type[_E]], type[_E]]:
        """Register a typed business fact with property-derived payload fields."""

        def decorator(cls: type[_E]) -> type[_E]:
            self._check_not_frozen(f"event type {cls.__name__!r}")
            if not issubclass(cls, Event):
                raise ValidationFailed(
                    f"{cls.__name__!r} must subclass Event to use @ontology.event(...)",
                    code="ONTOLOGY_INVALID",
                )
            if _class_stamp(cls)[1] is not None:
                raise ValidationFailed(
                    f"{cls.__name__!r} is already registered as an event "
                    "(a class may be decorated once)",
                    code="ONTOLOGY_INVALID",
                )
            name = api_name if api_name is not None else cls.__name__
            properties, _ = _derive_properties(cls, event=True)
            self.registry.register_event_type(EventTypeDef(
                api_name=name,
                description=description,
                properties=properties,
                accept=_normalize_accept(accept),
            ))
            cls._ontary_api_name = name
            cls._ontary_registry = self.registry
            return cls

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

    def rule(
        self, cls: type[_T], name: str, *, message: str
    ) -> Callable[[Callable[[_T], bool]], Callable[[_T], bool]]:
        """Register a typed predicate over the complete storage-form row."""

        def decorator(fn: Callable[[_T], bool]) -> Callable[[_T], bool]:
            self._check_not_frozen(f"rule {name!r}")
            api_name = self._registered_api_name(cls)
            obj_def = self.registry.get_object_type(api_name)

            def check(row: dict[str, Any]) -> bool:
                object_id = str(row[obj_def.primary_key])
                stored = StoredObject(
                    payload=row,
                    lineage=Lineage(
                        object_type=api_name,
                        object_id=object_id,
                        valid_from="",
                        valid_to=None,
                        source_system="rule",
                        source_id=None,
                        extracted_at=None,
                    ),
                )
                return bool(fn(hydrate(cls, stored, "human")))

            rule = RuleDef(name=name, message=message, check=check)
            candidate = ObjectTypeDef.model_validate(
                {**obj_def.__dict__, "rules": (*obj_def.rules, rule)}
            )
            obj_def.rules = candidate.rules
            return fn

        return decorator

    def link(
        self,
        api_name: str,
        from_cls: type[_F],
        to_cls: type[_T],
        cardinality: Cardinality | CardinalityName,
        *,
        description: str | None = None,
        identity_revealing: bool = False,
        owned: bool = False,
    ) -> LinkHandle[_F, _T]:
        """Derives + registers a `LinkTypeDef` between two classes already
        registered on THIS ontology (raises `ValidationFailed` with code
        `ONTOLOGY_INVALID` for a class registered elsewhere or not at all, or
        for a `cardinality` that names no `Cardinality` member) and returns a
        `LinkHandle` for statically-typed traversal.
        """
        self._check_not_frozen(f"link {api_name!r}")
        from_type = self._registered_api_name(from_cls)
        to_type = self._registered_api_name(to_cls)
        if isinstance(cardinality, Cardinality):
            card = cardinality
        else:
            try:
                card = Cardinality(cardinality)
            except ValueError:
                valid = ", ".join(member.value for member in Cardinality)
                raise ValidationFailed(
                    f"link {api_name!r}: cardinality={cardinality!r} is not a "
                    f"Cardinality; pass a Cardinality member or one of {valid}",
                    code="ONTOLOGY_INVALID",
                ) from None
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
        emits: Sequence[type[Event]] = (),
        accept: ActionLint | Sequence[ActionLint] | None = None,
    ) -> Callable[
        [Callable[[ActionContext, _P], dict[str, Any]]],
        Callable[[ActionContext, _P], dict[str, Any]],
    ]:
        """Derives an `ActionTypeDef` + `ActionParameterDef`s from
        `params_cls` and registers both it and the decorated handler
        (`(ctx: ActionContext, params: params_cls) -> dict[str, Any]`) on
        this `Ontology`. `api_name` defaults
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
            event_names: list[str] = []
            for event_cls in emits:
                event_name, event_registry = _class_stamp(event_cls)
                if (
                    not issubclass(event_cls, Event)
                    or event_name is None
                    or event_registry is not self.registry
                    or event_name not in self.registry.event_types
                ):
                    raise ValidationFailed(
                        f"{event_cls.__name__!r} is not a registered event on this Ontology "
                        f"-- cannot use it in action {name!r} emits",
                        code="ONTOLOGY_INVALID",
                    )
                if event_name not in event_names:
                    event_names.append(event_name)
            action_def = ActionTypeDef(
                api_name=name,
                display_name=display_name if display_name is not None else name,
                target_type=target_type,
                executable_by_roles=list(roles),
                description=description
                if description is not None
                else default_action_description(name),
                parameters=parameters,
                capabilities=capability_names,
                emits=event_names,
                accept=_normalize_accept(accept),
            )
            mismatch = target_param_mismatch(action_def)
            if mismatch is not None:
                raise ValidationFailed(mismatch[1], code="ONTOLOGY_INVALID")
            self.registry.register_action_type(action_def)  # dup api_name raises
            params_cls._ontary_api_name = name
            params_cls._ontary_registry = self.registry
            self._action_handlers[name] = (cast(TypedHandler, fn), params_cls)
            return fn

        return decorator

    @overload
    def function(
        self,
        params_cls: type[_FP],
        /,
        *,
        description: str | None = ...,
        input_description: str = ...,
        output_description: str = ...,
        api_name: str | None = ...,
        capabilities: Sequence[CapabilityHandle[builtins.object]] = ...,
        audit: bool | None = ...,
        accept: FunctionLint | Sequence[FunctionLint] | None = ...,
    ) -> Callable[[Callable[[BoundQuery, _FP], _R]], Callable[[BoundQuery, _FP], _R]]: ...

    @overload
    def function(
        self,
        params_cls: None = None,
        /,
        *,
        description: str | None = ...,
        input_description: str = ...,
        output_description: str = ...,
        api_name: str | None = ...,
        capabilities: Sequence[CapabilityHandle[builtins.object]] = ...,
        audit: bool | None = ...,
        accept: FunctionLint | Sequence[FunctionLint] | None = ...,
    ) -> _NoClassFunctionDecorator: ...

    def function(
        self,
        params_cls: type[FunctionParams] | None = None,
        /,
        *,
        description: str | None = None,
        input_description: str = "",
        output_description: str = "",
        api_name: str | None = None,
        capabilities: Sequence[CapabilityHandle[builtins.object]] = (),
        audit: bool | None = None,
        accept: FunctionLint | Sequence[FunctionLint] | None = None,
    ) -> Any:
        """Derives a `FunctionDef` and declares a typed or no-input
        handler on this `Ontology`. `api_name` defaults to `fn.__name__`.
        The handler is bound onto the `OntologyDef`'s
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

        def decorator(fn: _H) -> _H:
            resolved_name = name if name is not None else fn.__name__
            self._check_not_frozen(f"function {resolved_name!r}")
            if params_cls is not None:
                if not isinstance(params_cls, type) or not issubclass(
                    params_cls, FunctionParams
                ):
                    raise ValidationFailed(
                        f"function {resolved_name!r}: params class must inherit FunctionParams",
                        code="ONTOLOGY_INVALID",
                    )
                existing_registry = params_cls.__dict__.get("_ontary_registry")
                if existing_registry is not None and existing_registry is not self.registry:
                    raise ValidationFailed(
                        f"{params_cls.__name__!r} is already registered as a "
                        "function params class on another Ontology",
                        code="ONTOLOGY_INVALID",
                    )
                for existing_name, (_, existing_cls) in self._function_handlers.items():
                    if existing_cls is params_cls:
                        raise ValidationFailed(
                            f"{params_cls.__name__!r} is already declared as "
                            f"function {existing_name!r} (a params class may be declared once)",
                            code="ONTOLOGY_INVALID",
                        )
                parameters: list[ActionParameterDef] = _derive_params(
                    params_cls, self.registry, owner="function"
                )
            else:
                if not _takes_query_only(fn):
                    raise ValidationFailed(
                        _dict_handler_refusal(resolved_name), code="ONTOLOGY_INVALID"
                    )
                parameters = []
            capability_names = self._capability_api_names(
                capabilities, declared_on=f"function {resolved_name!r}"
            )
            fn_def = FunctionDef(
                api_name=resolved_name,
                description=description
                if description is not None
                else default_function_description(resolved_name),
                input_description=input_description,
                output_description=output_description,
                parameters=parameters,
                capabilities=capability_names,
                audit=audit,
                accept=_normalize_accept(accept),
            )
            self.registry.register_function(fn_def)  # dup api_name raises
            if params_cls is not None:
                params_cls._ontary_api_name = resolved_name
                params_cls._ontary_registry = self.registry
            self._function_handlers[resolved_name] = (fn, params_cls)
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

        try:
            return ScopePolicy(
                levels=self.scope_levels,
                unscoped_types=unscoped_types,
                rules=rules,
                contributor_rules=contributor_rules,
                row_visibility=row_visibility,
                min_n=self.min_n,
            )
        except ValidationError as exc:
            # Only `Ontology(...)`'s own kwargs can fail here: `scope_levels`
            # (empty / duplicated) and `min_n` (< 1). Per-class kwargs were
            # already shape-checked at decoration (`_check_scope_kwargs`).
            raise ValidationFailed(
                f"Ontology {self.name!r}: "
                + "; ".join(self._describe_policy_error(err) for err in exc.errors()),
                code="ONTOLOGY_INVALID",
            ) from None

    def _describe_policy_error(self, err: Mapping[str, Any]) -> str:
        field = err["loc"][0] if err["loc"] else "?"
        if field == "levels":
            detail = str(err["msg"]).removeprefix("Value error, ")
            return f"scope_levels={self.scope_levels!r} rejected: {detail}"
        if field == "min_n":
            return f"min_n={self.min_n!r} rejected: min_n must be >= 1"
        return f"{field}: {err['msg']}"

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
            for fn_name, (fn, params_cls) in self._function_handlers.items():
                functions.register(fn_name, fn, params_cls=params_cls)
            self._definition = OntologyDef(
                name=self.name,
                registry=self.registry,
                policy=self._build_policy(),
                functions=functions,
                # Typed action handlers ride the definition, mirroring
                # the function binding above. Safe to snapshot here: this
                # first access freezes registration, so the map cannot go
                # stale.
                action_handlers=dict(self._action_handlers),
            )
        return self._definition

    def validate(self, store: Store | None = None) -> None:
        """Convenience: builds `.definition` (if not already built) and
        validates it -- equivalent to `ontology.definition.validate()`.

        With a `store`, also sweeps its current rows (see `diagnose`).
        Hydration failures raise `INVALID_RECORD`; otherwise broken declared
        rules raise `RULE_VIOLATED`. A store read error propagates as-is.
        """
        definition = self.definition
        definition.validate()
        if store is None:
            return
        findings: list[Finding] = []
        for api_name in sorted(definition.registry.object_types):
            findings.extend(
                _store_row_findings(definition, store, api_name, self._classes)
            )
            findings.extend(_stored_rule_findings(definition, store, api_name))
        if findings:
            message = "; ".join(finding.message for finding in findings)
            if any(finding.code == "INVALID_RECORD" for finding in findings):
                raise ValidationFailed(message, code="INVALID_RECORD")
            raise ValidationFailed(
                message, code="RULE_VIOLATED"
            )

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

    def diagnose(self, store: Store | None = None) -> list[Finding]:
        """Return all validation findings without raising mid-sweep.

        If this ontology is still being authored, diagnostics build a
        transient definition from the same pre-freeze structures used by
        ``validate``; the cached definition is left untouched so
        registration can continue afterwards.

        With a ``store``, also sweep its current rows (``Store.read_all``)
        and report, per (type, property), how many rows ``hydrate`` would
        refuse under this ontology as ``INVALID_RECORD`` findings, plus one
        ``RULE_VIOLATED`` finding per declared rule broken by current rows.
        The sweep reads every current row, so it is explicit here rather
        than run at ``bind()``.
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
        return _collect_findings(definition, store=store, classes=self._classes)

    def bind(
        self,
        store: Store,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        capabilities: Mapping[CapabilityHandle[Any], builtins.object] | None = None,
    ) -> OntologyRuntime:
        """Builds an `OntologyRuntime`: ONE
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
        )
