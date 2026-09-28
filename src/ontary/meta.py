"""Meta-schema descriptors and OntologyRegistry for the ontology-as-code layer.

The ontology *definition* is data: descriptors here are inspectable/exportable,
and `OntologyRegistry` validates cross-references (dangling link endpoints,
dangling action targets) at registration time. This module is domain-agnostic:
any ontology author declares their own object/link/action/function types and
their own scope-level names (see `ScopeLevel`) — nothing here is specific to
any one domain.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ontary.errors import ValidationFailed
from ontary.typesys import PropertyType, choice_value, struct_value, validate_scalar

__all__ = [
    "PropertyType",
    "Cardinality",
    "Sensitivity",
    "PropertyDef",
    "StructFieldDef",
    "TransitionDef",
    "RuleDef",
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


class StructFieldDef(BaseModel):
    name: str
    type: PropertyType
    choices: tuple[str, ...] | None = None
    required: bool = True

    @model_validator(mode="before")
    @classmethod
    def _valid_declaration(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        name = data.get("name")
        if data.get("type") not in {"str", "int", "float", "bool", "date", "datetime"}:
            raise ValidationFailed(
                f"StructFieldDef {name!r}: type must be a scalar str/int/float/bool/date/datetime",
                code="ONTOLOGY_INVALID",
            )
        violation = _choices_declaration_violation(data.get("type"), data.get("choices"))
        if violation is not None:
            raise ValidationFailed(f"StructFieldDef {name!r}: {violation}", code="ONTOLOGY_INVALID")
        return data


def _str_enum_value(state: Any) -> Any:
    """An ``Enum`` member as its value (the str check then refuses a non-str
    value); anything else unchanged."""
    if isinstance(state, Enum):
        return state.value
    return state


def _unwrap_str_enum_states(data: dict[str, Any]) -> dict[str, Any]:
    """Accept string-valued ``Enum`` members (plain ``Enum`` or ``StrEnum``) as
    states, as a choice property accepts them as values (#42)."""
    data = dict(data)
    initial = data.get("initial")
    if isinstance(initial, (list, tuple)):
        data["initial"] = tuple(_str_enum_value(state) for state in initial)
    moves = data.get("moves")
    if isinstance(moves, dict):
        data["moves"] = {
            _str_enum_value(source): (
                tuple(_str_enum_value(target) for target in targets)
                if isinstance(targets, (list, tuple))
                else targets
            )
            for source, targets in moves.items()
        }
    return data


class TransitionDef(BaseModel):
    """Allowed start states and moves for a choice property."""

    initial: tuple[str, ...]
    moves: dict[str, tuple[str, ...]]

    def __init__(
        self,
        *,
        initial: Sequence[str | Enum],
        moves: Mapping[Any, Sequence[str | Enum]],
    ) -> None:
        # Typed for authors: states may be string-valued Enum members, which
        # `_valid_states` stores as their plain values.
        super().__init__(initial=initial, moves=moves)

    @model_validator(mode="before")
    @classmethod
    def _valid_states(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = _unwrap_str_enum_states(data)
        initial = data.get("initial")
        if isinstance(initial, (list, tuple)):
            if not initial:
                raise ValidationFailed(
                    "TransitionDef: initial must be non-empty; declare a start state",
                    code="ONTOLOGY_INVALID",
                )
            if any(not isinstance(state, str) for state in initial):
                raise ValidationFailed(
                    "TransitionDef: every initial state must be a str",
                    code="ONTOLOGY_INVALID",
                )
        moves = data.get("moves")
        if isinstance(moves, dict):
            for source, targets in moves.items():
                if not isinstance(source, str):
                    raise ValidationFailed(
                        "TransitionDef: every moves source state must be a str",
                        code="ONTOLOGY_INVALID",
                    )
                if isinstance(targets, (list, tuple)) and any(
                    not isinstance(target, str) for target in targets
                ):
                    raise ValidationFailed(
                        "TransitionDef: every moves target state must be a str",
                        code="ONTOLOGY_INVALID",
                    )
        return data


class RuleDef(BaseModel):
    """Named predicate over the full new row; only metadata is exported."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    message: str
    check: Callable[[dict[str, Any]], bool] = Field(exclude=True, repr=False)

    @model_validator(mode="before")
    @classmethod
    def _valid_declaration(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for field in ("name", "message"):
            if data.get(field) == "":
                raise ValidationFailed(
                    f"RuleDef: {field} must be non-empty; provide a {field}",
                    code="ONTOLOGY_INVALID",
                )
        if "check" in data and not callable(data["check"]):
            raise ValidationFailed(
                "RuleDef: check must be callable; provide a predicate",
                code="ONTOLOGY_INVALID",
            )
        return data


def _fields_declaration_violation(
    prop_type: PropertyType, fields: tuple[StructFieldDef, ...] | None
) -> str | None:
    if prop_type == "struct":
        if not fields:
            return "struct fields must be non-empty"
        names = [field.name for field in fields]
        if len(names) != len(set(names)):
            return "struct fields contain a duplicate name"
    elif fields is not None:
        return "fields are only valid on 'struct' declarations"
    return None


class PropertyDef(BaseModel):
    name: str
    type: PropertyType
    choices: tuple[str, ...] | None = None
    fields: tuple[StructFieldDef, ...] | None = None
    transitions: TransitionDef | None = None
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

    @model_validator(mode="after")
    def _valid_fields(self) -> PropertyDef:
        violation = _fields_declaration_violation(self.type, self.fields)
        if violation is not None:
            raise ValidationFailed(f"PropertyDef {self.name!r}: {violation}", code="ONTOLOGY_INVALID")
        return self

    @model_validator(mode="after")
    def _valid_transitions(self) -> PropertyDef:
        graph = self.transitions
        if graph is None:
            return self
        if self.choices is None:
            raise ValidationFailed(
                f"PropertyDef {self.name!r}: transitions require choices; "
                "declare choices (an Enum or Literal) on the property",
                code="ONTOLOGY_INVALID",
            )
        choices = set(self.choices)
        states = set(graph.initial) | set(graph.moves)
        states.update(target for targets in graph.moves.values() for target in targets)
        unknown = sorted(states - choices)
        if unknown:
            raise ValidationFailed(
                f"PropertyDef {self.name!r}: transition states {unknown} are not in choices; "
                "use only declared choices",
                code="ONTOLOGY_INVALID",
            )
        missing = sorted(choices - graph.moves.keys())
        if missing:
            raise ValidationFailed(
                f"PropertyDef {self.name!r}: moves omit {missing}; "
                "list every state; use [] for a terminal state",
                code="ONTOLOGY_INVALID",
            )
        return self


class ObjectTypeDef(BaseModel):
    api_name: str
    display_name: str
    description: str
    layer: str
    properties: list[PropertyDef]
    rules: tuple[RuleDef, ...] = ()
    primary_key: str
    # Authority declaration (spec `declared-contracts` §3 AC1): `True` means
    # the whole type is ontology-owned (no connector supplies it); a dict
    # means the type is otherwise source-backed except for the named
    # properties, each with a default value used when a connector-supplied
    # record doesn't carry it; `False` (default) means fully source-backed.
    # Undeclared == source-backed, matching the reference pattern.
    owned: bool | dict[str, Any] = False

    @model_validator(mode="after")
    def _primary_key_exists(self) -> "ObjectTypeDef":
        names = {p.name for p in self.properties}
        if self.primary_key not in names:
            raise ValueError(
                f"ObjectTypeDef {self.api_name!r}: primary_key "
                f"{self.primary_key!r} not found in properties {sorted(names)}"
            )
        return self

    @model_validator(mode="after")
    def _unique_rule_names(self) -> ObjectTypeDef:
        names = [rule.name for rule in self.rules]
        if len(names) != len(set(names)):
            duplicate = next(name for name in names if names.count(name) > 1)
            raise ValidationFailed(
                f"ObjectTypeDef {self.api_name!r}: duplicate rule {duplicate!r}; "
                "give each rule a unique name",
                code="ONTOLOGY_INVALID",
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
            return normalize_declared_payload(self, self.owned)
        return {}


def normalize_declared_payload(
    obj_def: "ObjectTypeDef", payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Normalize declared choices and structs before write validation."""
    declarations = {prop.name: prop for prop in obj_def.properties}
    normalized: dict[str, Any] = {}
    for name, value in payload.items():
        declared = declarations.get(name)
        if declared is not None and declared.type == "struct" and declared.fields is not None:
            normalized[name] = struct_value(value, declared.fields)
        else:
            normalized[name] = choice_value(value, declared.choices if declared is not None else None)
    return normalized


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
        mismatch = validate_scalar(value, declared.type, declared.choices, fields=declared.fields)
        if mismatch is not None:
            if declared.type == "struct" and ": " in mismatch:
                inner, mismatch = mismatch.split(": ", 1)
                key = f"{key}.{inner}"
            return f"property {key!r} {mismatch}"
    return None


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
    - `choices`: the allowed string values, as on `PropertyDef` (#42).
    """

    name: str
    type: PropertyType
    choices: tuple[str, ...] | None = None
    fields: tuple[StructFieldDef, ...] | None = None
    required: bool = True
    refers_to: str | None = None
    scope_semantics: Literal["target", "scope"] | None = None

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
                f"ActionParameterDef {data.get('name')!r}: {violation}",
                code="ONTOLOGY_INVALID",
            )
        return data

    @model_validator(mode="after")
    def _valid_fields(self) -> ActionParameterDef:
        violation = _fields_declaration_violation(self.type, self.fields)
        if violation is not None:
            raise ValidationFailed(
                f"ActionParameterDef {self.name!r}: {violation}", code="ONTOLOGY_INVALID"
            )
        return self


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


def target_param_mismatch(action: ActionTypeDef) -> tuple[int, str] | None:
    """Return (index of the first `target()` param, why) when an action has
    `scope_semantics="target"` params but none refers to its `target_type`,
    else None (ontary#40).

    The scope gate resolves targets from those params' `refers_to`, while
    audit and MCP report `target_type`; with no param of that type the gate
    checks one object and the audit names another. Extra `target()` params
    of other types stay allowed (e.g. an unscoped one, recorded in
    `AuditEntry.unscoped_params`) as long as one names the target type.
    """
    targets = [
        (index, param)
        for index, param in enumerate(action.parameters)
        if param.scope_semantics == "target" and param.refers_to is not None
    ]
    if not targets or any(
        param.refers_to == action.target_type for _, param in targets
    ):
        return None
    declared = ", ".join(
        f"{param.name!r} is target({param.refers_to!r})" for _, param in targets
    )
    return targets[0][0], (
        f"ActionTypeDef {action.api_name!r}: no target() parameter refers "
        f"to target={action.target_type!r} ({declared}); one target() "
        "param must refer to the action's target type (use scope_ref() or "
        "ref() for another type)"
    )


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

        Four sequential error-collecting passes (split into one method each;
        pass order -- and therefore message order in the joined error -- is
        part of the observable behavior and unchanged):
        - owned property defaults exist, are not the pk, and match the type
        - LinkTypeDef.from_type / to_type reference registered object types
        - ActionTypeDef target/capability/parameter references resolve
        - FunctionDef capabilities reference registered capability declarations
        """
        errors: list[str] = []
        self._validate_owned_property_defaults(errors)
        self._validate_link_references(errors)
        self._validate_action_references(errors)
        self._validate_function_references(errors)
        if errors:
            raise ValidationFailed("; ".join(errors), code="ONTOLOGY_INVALID")

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
                mismatch = validate_scalar(default, prop.type, prop.choices, fields=prop.fields)
                if mismatch is not None:
                    if prop.type == "struct" and ": " in mismatch:
                        inner, mismatch = mismatch.split(": ", 1)
                        prop_name = f"{prop_name}.{inner}"
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

            mismatch = target_param_mismatch(action)
            if mismatch is not None:
                errors.append(mismatch[1])

    def _validate_function_references(self, errors: list[str]) -> None:
        for fn in self._functions.values():
            for capability in fn.capabilities:
                if capability not in self._capabilities:
                    errors.append(
                        f"FunctionDef {fn.api_name!r}: dangling capability "
                        f"{capability!r}"
                    )
