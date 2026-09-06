"""Meta-schema descriptors and OntologyRegistry for the ontology-as-code layer.

The ontology *definition* is data: descriptors here are inspectable/exportable,
and `OntologyRegistry` validates cross-references (dangling link endpoints,
dangling action targets) at registration time. This module is domain-agnostic:
any ontology author declares their own object/link/action/function types and
their own scope-level names (see `ScopeLevel`) — nothing here is specific to
any one domain.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ontary.errors import ValidationFailed
from ontary.typesys import PropertyType, validate_scalar

__all__ = [
    "PropertyType",
    "Cardinality",
    "Sensitivity",
    "PropertyDef",
    "ObjectTypeDef",
    "LinkTypeDef",
    "ActionParameterDef",
    "CapabilityDef",
    "ActionTypeDef",
    "FunctionDef",
    "OntologyRegistry",
    "ScopeLevel",
]

# Author-defined scope-level names (e.g. "person", "team", "company", or a
# domain's own hierarchy) — `None` means unscoped. The engine treats this as
# an opaque string; the scope-level hierarchy itself is declared per-ontology.
ScopeLevel = str | None


class Cardinality(str, Enum):
    ONE_TO_ONE = "ONE_TO_ONE"
    ONE_TO_MANY = "ONE_TO_MANY"
    MANY_TO_ONE = "MANY_TO_ONE"
    MANY_TO_MANY = "MANY_TO_MANY"


class Sensitivity(BaseModel):
    """AI-use / human-visibility policy declared at schema level."""

    ai_usable: bool = True
    human_visible: bool = True


def _choices_declaration_violation(
    prop_type: object, choices: Sequence[object] | None
) -> str | None:
    """Why a property's ``choices`` declaration is invalid, or ``None``."""
    if choices is None:
        return None
    if prop_type != "str":
        return "choices are only valid on 'str' properties"
    if not choices:
        return "choices must not be empty"
    for member in choices:
        if not isinstance(member, str):
            return f"choices contains non-string member {member!r}"
    if len(set(choices)) != len(choices):
        return "choices contains a duplicate member"
    return None


class PropertyDef(BaseModel):
    name: str
    type: PropertyType
    choices: tuple[str, ...] | None = None
    required: bool = True
    sensitivity: Sensitivity = Field(default_factory=Sensitivity)
    scope_level: ScopeLevel = None

    @model_validator(mode="before")
    @classmethod
    def _valid_choices(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        violation = _choices_declaration_violation(
            data.get("type"), data.get("choices")
        )
        if violation is not None:
            raise ValidationFailed(
                f"PropertyDef {data.get('name')!r}: {violation}",
                code="ONTOLOGY_INVALID",
            )
        return data


class ObjectTypeDef(BaseModel):
    api_name: str
    display_name: str
    description: str
    layer: str
    properties: list[PropertyDef]
    primary_key: str
    # Authority declaration (spec `declared-contracts` §3 AC1): `True` means
    # the whole type is ontology-owned (no connector supplies it); a dict
    # means the type is otherwise source-backed except for the named
    # properties, each with a default value used when a connector-supplied
    # record doesn't carry it; `False` (default) means fully source-backed.
    # Undeclared == source-backed, matching the reference pattern.
    owned: bool | dict[str, Any] = False

    version: int = Field(default=1, ge=1)
    """The declared shape's version (M9b). Bumping it is how an author says "this
    type changed on purpose", which is what lets the drift check distinguish a
    deliberate evolution from an accidental one: a changed declaration WITHOUT a
    bump is still refused, and a bump with an upcaster chain covering the stored
    versions opens without an acknowledgement.

    Part of the fingerprint, necessarily -- a version bump that did not move the
    digest would be a version nobody could detect."""

    @model_validator(mode="after")
    def _primary_key_exists(self) -> "ObjectTypeDef":
        names = {p.name for p in self.properties}
        if self.primary_key not in names:
            raise ValueError(
                f"ObjectTypeDef {self.api_name!r}: primary_key "
                f"{self.primary_key!r} not found in properties {sorted(names)}"
            )
        return self

    @property
    def is_owned_type(self) -> bool:
        """True only when the *whole* type is declared ontology-owned."""
        return self.owned is True

    def owned_property_defaults(self) -> dict[str, Any]:
        """The declared owned-property -> default map, or `{}` when `owned`
        is `False` (fully source-backed) or `True` (whole-type owned, which
        carries no per-property defaults of its own).
        """
        if isinstance(self.owned, dict):
            return dict(self.owned)
        return {}


def declared_shape_violation(
    obj_def: "ObjectTypeDef", payload: dict[str, Any]
) -> str | None:
    """Why `payload` does not satisfy `obj_def`'s declared shape, or `None`.

    Added by M9a (spec `ontology-evolution` AC9) to close a hole the evolution
    investigation found: **only `bulk_upsert` enforced the declared shape.**
    `Store.insert`/`update` -- and therefore `ActionContext.insert`/`update`, the
    write path every action uses -- did not, so an action could commit a row that
    the same ontology's typed reader then refused to hydrate
    (a validation-kind `INVALID_RECORD` failure), and report success. The engine was manufacturing rows it
    could not read back.

    Checks exactly what makes a row UNREADABLE by its own declaration: every
    declared required property is present and non-null, and every declared
    property's value matches its declared `PropertyType`. Owned-property
    defaults are exempt from the required check -- the store injects them, so a
    caller omitting one is not writing a broken row.

    **An undeclared key is deliberately allowed**, and that is a narrowing of
    the spec's AC9 with a reason. Hydration ignores extra payload keys, so an
    extra key cannot produce the failure this function exists to prevent; and
    real stores carry them legitimately -- a row whose scope is resolved
    `ViaLink` may still hold the source's own foreign-key field. Enforcing their
    absence here would reject data that works today and buy nothing. `bulk_upsert`
    still refuses unknown properties, correctly: a SOURCE record claiming a
    property the ontology never declared is a mapping bug, which is a different
    situation from an action writing alongside declared state.

    Deliberately NOT `ingest._validate_record`, which looks similar and is not:
    that one also refuses any owned property (a connector may never supply one),
    which is exactly what an ACTION is allowed to do. Merging the two would make
    one of the two call sites wrong.
    """
    owned_defaults = obj_def.owned_property_defaults()
    prop_by_name = {prop.name: prop for prop in obj_def.properties}

    for prop in obj_def.properties:
        if prop.name in owned_defaults or not prop.required:
            continue
        if payload.get(prop.name) is None:
            return (
                f"missing required property {prop.name!r} on object type "
                f"{obj_def.api_name!r}"
            )

    for key, value in payload.items():
        declared = prop_by_name.get(key)
        if declared is None:
            continue
        if value is None:
            continue
        mismatch = validate_scalar(value, declared.type, declared.choices)
        if mismatch is not None:
            return f"property {key!r} {mismatch}"
    return None


Upcaster = Callable[[dict[str, Any]], dict[str, Any]]
"""Reads one stored payload at version N and returns it at version N+1.

Author code, unsandboxed, like every other extension point here. It receives a
COPY of the stored payload, so mutating the argument is harmless; the return value
is what the engine uses.
"""


class LinkTypeDef(BaseModel):
    api_name: str
    from_type: str
    to_type: str
    cardinality: Cardinality
    description: str
    # A link that, if traversed, re-identifies an individual behind
    # anonymized/scoped data (e.g. Response -> authoring Person). Default
    # False keeps every pre-existing LinkTypeDef backward-compatible; the
    # guarded query layer denies this link to human consumers only.
    identity_revealing: bool = False
    # Authority declaration (spec `declared-contracts` §3 AC1): `True` means
    # this link type is ontology-owned; `False` (default) means
    # source-backed. Undeclared == source-backed.
    owned: bool = False


class ActionParameterDef(BaseModel):
    """Declares one parameter of an `ActionTypeDef`, generalizing the
    prototype's hardcoded `_TARGET_PARAM_TYPES` / `_EXPLICIT_SCOPE_PARAMS`
    (`dso.actions`) into author-supplied data the engine reads generically.

    - `refers_to`: the api_name of the object type this parameter's value
      identifies (e.g. `"Goal"`), if any.
    - `scope_semantics="target"` (requires `refers_to`): if the param is
      present and an object of that type with that id EXISTS, the
      consumer's scope must cover it; a nonexistent target is left to the
      handler's own precondition check (audited "error"), never treated as
      a scope denial.
    - `scope_semantics="scope"` (requires `refers_to`): the param names an
      object (e.g. a team) that must itself be covered by the consumer's
      scope, regardless of whether it "exists" as a target of the action.
    """

    name: str
    type: PropertyType
    required: bool = True
    refers_to: str | None = None
    scope_semantics: Literal["target", "scope"] | None = None


class CapabilityDef(BaseModel):
    """Declares an outside-world read by stable name, without coupling the
    serializable ontology IR to the provider protocol or its runtime binding.
    The provider's Python type belongs to authoring; this descriptor is the
    durable declaration an MCP consumer can inspect.
    """

    api_name: str
    description: str


class ActionTypeDef(BaseModel):
    api_name: str
    display_name: str
    target_type: str
    executable_by_roles: list[str]
    description: str
    parameters: list[ActionParameterDef] = []
    capabilities: list[str] = []


class FunctionDef(BaseModel):
    api_name: str
    description: str
    input_description: str
    output_description: str
    capabilities: list[str] = []
    #: Whether a call to this function appends an audit entry. `None` means
    #: "use the default", which is `True` exactly when the function declares
    #: capabilities -- see `audited`.
    audit: bool | None = None

    @property
    def audited(self) -> bool:
        """Whether `OntologyClient.call_function` audits this function.

        The default is deliberately conditional rather than a flat on/off.
        Functions are called far more often than actions -- an aggregate per
        page render, say -- so auditing every one of them is real write
        amplification for little gained: a function that declares no
        capabilities cannot reach outside the process, and everything it can
        read is already bounded by the guarded query layer.

        A function that DOES declare a capability is the case an auditor
        actually asks about ("what did the AI read?"), because the provider is
        trusted author code that leaves the process. So: audited by default iff
        it declares capabilities, and `audit=True`/`audit=False` at declaration
        overrides in either direction.

        **This property answers one question, and it is not the only one.**
        "Everything it can read is already bounded by the guarded query layer"
        stays true, but that bound INCLUDES the AC10 contributor exemption: a
        capability-less function can still hand a consumer a mean over a field
        they cannot read. Being unable to reach outside the process was never
        the same as being unable to release an individual-bearing number.
        `OntologyClient.call_function` therefore audits an actual release on
        its own terms, whatever this property says and `audit=False` included
        -- see that method. Nothing here changes: a release is a runtime
        event, and a declaration cannot know whether one will happen.
        """
        if self.audit is not None:
            return self.audit
        return bool(self.capabilities)


class OntologyRegistry:
    """Holds all registered descriptors and validates cross-references."""

    def __init__(self) -> None:
        self._object_types: dict[str, ObjectTypeDef] = {}
        self._link_types: dict[str, LinkTypeDef] = {}
        self._action_types: dict[str, ActionTypeDef] = {}
        self._functions: dict[str, FunctionDef] = {}
        self._capabilities: dict[str, CapabilityDef] = {}
        # (object api_name, from_version) -> upcaster. Keyed by the version the
        # function reads FROM, because that is what a stored row carries: given
        # `type_version = 1`, the engine needs "the function that turns a v1
        # payload into a v2 one" without searching.
        self._upcasters: dict[tuple[str, int], Upcaster] = {}

    def register_object_type(self, obj: ObjectTypeDef) -> None:
        if obj.api_name in self._object_types:
            raise ValidationFailed(
                f"duplicate ObjectTypeDef api_name: {obj.api_name!r}",
                code="ONTOLOGY_INVALID",
            )
        self._object_types[obj.api_name] = obj

    def register_link_type(self, link: LinkTypeDef) -> None:
        if link.api_name in self._link_types:
            raise ValidationFailed(
                f"duplicate LinkTypeDef api_name: {link.api_name!r}",
                code="ONTOLOGY_INVALID",
            )
        self._link_types[link.api_name] = link

    def register_action_type(self, action: ActionTypeDef) -> None:
        if action.api_name in self._action_types:
            raise ValidationFailed(
                f"duplicate ActionTypeDef api_name: {action.api_name!r}",
                code="ONTOLOGY_INVALID",
            )
        self._action_types[action.api_name] = action

    def register_function(self, fn: FunctionDef) -> None:
        if fn.api_name in self._functions:
            raise ValidationFailed(
                f"duplicate FunctionDef api_name: {fn.api_name!r}",
                code="ONTOLOGY_INVALID",
            )
        self._functions[fn.api_name] = fn

    def register_capability(self, capability: CapabilityDef) -> None:
        if capability.api_name in self._capabilities:
            raise ValidationFailed(
                f"duplicate CapabilityDef api_name: {capability.api_name!r}",
                code="ONTOLOGY_INVALID",
            )
        self._capabilities[capability.api_name] = capability

    @property
    def object_types(self) -> dict[str, ObjectTypeDef]:
        return dict(self._object_types)

    @property
    def link_types(self) -> dict[str, LinkTypeDef]:
        return dict(self._link_types)

    @property
    def action_types(self) -> dict[str, ActionTypeDef]:
        return dict(self._action_types)

    @property
    def functions(self) -> dict[str, FunctionDef]:
        return dict(self._functions)

    @property
    def capabilities(self) -> dict[str, CapabilityDef]:
        return dict(self._capabilities)

    def register_upcaster(
        self, api_name: str, from_version: int, fn: "Upcaster"
    ) -> None:
        """Register the function that reads a `from_version` payload of
        `api_name` and returns a `from_version + 1` one (M9b).

        One step per registration, never a jump: an author who moved a type from
        v1 to v3 declares two functions. That is deliberate -- a chain of small
        steps can be applied to a row at ANY stored version, whereas a single
        v1->v3 function is useless to the row that happens to be at v2, and
        nothing would have told the author until that row was read.
        """
        if from_version < 1:
            raise ValidationFailed(
                f"upcaster for {api_name!r}: from_version must be >= 1, "
                f"got {from_version}",
                code="ONTOLOGY_INVALID",
            )
        key = (api_name, from_version)
        if key in self._upcasters:
            raise ValidationFailed(
                f"duplicate upcaster for object type {api_name!r} "
                f"from version {from_version}",
                code="ONTOLOGY_INVALID",
            )
        self._upcasters[key] = fn

    def upcaster(self, api_name: str, from_version: int) -> "Upcaster | None":
        return self._upcasters.get((api_name, from_version))

    @property
    def upcasters(self) -> dict[tuple[str, int], "Upcaster"]:
        return dict(self._upcasters)

    def get_object_type(self, api_name: str) -> ObjectTypeDef:
        try:
            return self._object_types[api_name]
        except KeyError as exc:
            raise ValidationFailed(
                f"unregistered object type: {api_name!r}",
                code="UNKNOWN_OBJECT_TYPE",
            ) from exc

    def get_link_type(self, api_name: str) -> LinkTypeDef:
        try:
            return self._link_types[api_name]
        except KeyError as exc:
            raise ValidationFailed(
                f"unregistered link type: {api_name!r}",
                code="UNKNOWN_LINK_TYPE",
            ) from exc

    def get_action_type(self, api_name: str) -> ActionTypeDef:
        try:
            return self._action_types[api_name]
        except KeyError as exc:
            raise ValidationFailed(
                f"unregistered action type: {api_name!r}",
                code="UNKNOWN_ACTION",
            ) from exc

    def get_function(self, api_name: str) -> FunctionDef:
        try:
            return self._functions[api_name]
        except KeyError as exc:
            raise ValidationFailed(
                f"unregistered function: {api_name!r}",
                code="UNKNOWN_NAME",
            ) from exc

    def get_capability(self, api_name: str) -> CapabilityDef:
        try:
            return self._capabilities[api_name]
        except KeyError as exc:
            raise ValidationFailed(
                f"unregistered capability: {api_name!r}",
                code="UNKNOWN_NAME",
            ) from exc

    def validate(self) -> None:
        """Raise a validation-kind error on dangling references.

        Five sequential error-collecting passes (split into one method each;
        pass order -- and therefore message order in the joined error -- is
        part of the observable behavior and unchanged):
        - upcaster chains are complete and never point past the declared version
        - owned property defaults exist, are not the pk, and match the type
        - LinkTypeDef.from_type / to_type reference registered object types
        - ActionTypeDef target/capability/parameter references resolve
        - FunctionDef capabilities reference registered capability declarations
        """
        errors: list[str] = []
        self._validate_upcasters(errors)
        self._validate_owned_property_defaults(errors)
        self._validate_link_references(errors)
        self._validate_action_references(errors)
        self._validate_function_references(errors)
        if errors:
            raise ValidationFailed("; ".join(errors), code="ONTOLOGY_INVALID")

    def _validate_upcasters(self, errors: list[str]) -> None:
        """Upcaster chains must be COMPLETE and must not point past the
        declared version (M9b). A gap is the defect that only shows up when
        the one row still at the missing version is read -- possibly months
        later, in production, on the read path -- so it is refused at
        declaration time instead."""
        for (api_name, from_version) in sorted(self._upcasters):
            obj = self._object_types.get(api_name)
            if obj is None:
                errors.append(
                    f"upcaster for unregistered object type {api_name!r} "
                    f"(from version {from_version})"
                )
                continue
            if from_version >= obj.version:
                errors.append(
                    f"upcaster for {api_name!r} reads from version "
                    f"{from_version}, which is not older than the declared "
                    f"version {obj.version} -- it could never apply to a "
                    "stored row"
                )
        for obj in self._object_types.values():
            if obj.version > 1:
                missing = [
                    v
                    for v in range(1, obj.version)
                    if (obj.api_name, v) not in self._upcasters
                ]
                if missing:
                    errors.append(
                        f"ObjectTypeDef {obj.api_name!r} is declared version "
                        f"{obj.version} but has no upcaster from version(s) "
                        f"{missing} -- a row stored at any of those versions "
                        "could not be read. Declare one upcaster per step, or "
                        "migrate the rows and drop the version bump"
                    )

    def _validate_owned_property_defaults(self, errors: list[str]) -> None:
        for obj in self._object_types.values():
            owned_defaults = obj.owned_property_defaults()
            if not owned_defaults:
                continue
            prop_by_name: dict[str, PropertyDef] = {
                p.name: p for p in obj.properties
            }
            for prop_name, default in owned_defaults.items():
                if prop_name == obj.primary_key:
                    errors.append(
                        f"ObjectTypeDef {obj.api_name!r}: owned property "
                        f"{prop_name!r} is the primary key, which can never "
                        "be individually owned (use owned=True for the "
                        "whole type)"
                    )
                    continue
                prop = prop_by_name.get(prop_name)
                if prop is None:
                    errors.append(
                        f"ObjectTypeDef {obj.api_name!r}: owned property "
                        f"{prop_name!r} is not one of this type's properties"
                    )
                    continue
                if default is None:
                    if prop.required:
                        errors.append(
                            f"ObjectTypeDef {obj.api_name!r}: owned property "
                            f"{prop_name!r} default is None but the "
                            "property is required"
                        )
                    continue
                mismatch = validate_scalar(default, prop.type, prop.choices)
                if mismatch is not None:
                    errors.append(
                        f"ObjectTypeDef {obj.api_name!r}: owned property "
                        f"{prop_name!r} default {mismatch}"
                    )

    def _validate_link_references(self, errors: list[str]) -> None:
        for link in self._link_types.values():
            if link.from_type not in self._object_types:
                errors.append(
                    f"LinkTypeDef {link.api_name!r}: dangling from_type "
                    f"{link.from_type!r}"
                )
            if link.to_type not in self._object_types:
                errors.append(
                    f"LinkTypeDef {link.api_name!r}: dangling to_type "
                    f"{link.to_type!r}"
                )

    def _validate_action_references(self, errors: list[str]) -> None:
        for action in self._action_types.values():
            if action.target_type not in self._object_types:
                errors.append(
                    f"ActionTypeDef {action.api_name!r}: dangling target_type "
                    f"{action.target_type!r}"
                )

            for capability in action.capabilities:
                if capability not in self._capabilities:
                    errors.append(
                        f"ActionTypeDef {action.api_name!r}: dangling capability "
                        f"{capability!r}"
                    )

            for param in action.parameters:
                if param.scope_semantics is not None and param.refers_to is None:
                    errors.append(
                        f"ActionTypeDef {action.api_name!r} parameter "
                        f"{param.name!r}: scope_semantics "
                        f"{param.scope_semantics!r} requires refers_to"
                    )
                if (
                    param.refers_to is not None
                    and param.refers_to not in self._object_types
                ):
                    errors.append(
                        f"ActionTypeDef {action.api_name!r} parameter "
                        f"{param.name!r}: dangling refers_to "
                        f"{param.refers_to!r}"
                    )

    def _validate_function_references(self, errors: list[str]) -> None:
        for fn in self._functions.values():
            for capability in fn.capabilities:
                if capability not in self._capabilities:
                    errors.append(
                        f"FunctionDef {fn.api_name!r}: dangling capability "
                        f"{capability!r}"
                    )
