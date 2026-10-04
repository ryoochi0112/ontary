"""Complete, non-raising diagnostics for an authored ontology.

The core rules mirror the conditions checked by ``OntologyDef.validate`` and
its registry/policy validators.  They read descriptor data only; no rule
mutates the registry, policy, or store.  Later DX tasks can extend ``RULES``
with advisory rules without changing the collector contract.  The one
store-reading check, ``_store_row_findings``, runs only when a caller passes
a store (ontary#40).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from ontary.meta import (
    ActionTypeDef,
    LinkTypeDef,
    ObjectTypeDef,
    PropertyDef,
    Sensitivity,
    target_param_mismatch,
)
from ontary.scope import (
    CustomResolver,
    DirectProperty,
    ScopePolicy,
    ScopeRule,
    SelfScope,
    ViaLink,
    incoherent_scope_declarations,
    unscoped_scope_parameter_declarations,
)
from ontary.typesys import _storage_scalar_violation, validate_scalar

if TYPE_CHECKING:
    from ontary.ontology import OntologyDef
    from ontary.store import Store

__all__ = ["Finding"]


class Finding(BaseModel):
    """One actionable diagnostic reported by :meth:`Ontology.diagnose`."""

    model_config = ConfigDict(frozen=True)

    code: str
    severity: Literal["error", "warn", "info"]
    location: str
    message: str
    fix_hint: str
    guide: str | None = None


DiagnosticRule = Callable[["OntologyDef"], tuple[Finding, ...]]


GUIDE_URL = "https://ryoochi0112.github.io/ontary/ontology-design/"
GUIDE_ANCHORS: dict[str, tuple[str, ...]] = {
    "STORED_DERIVABLE": ("normalization-and-derived-values",),
    "MICRO_ACTION": ("action-sprawl",),
    "CRUD_ACTION_NAME": ("action-sprawl", "retirement-and-removal"),
    "FORBIDDEN_TYPE_NAME": ("the-time-machine",),
    "FREE_TEXT_STATUS": ("choice-properties",),
    "AUDIT_TYPE": ("the-golden-hammer",),
    "UNSCOPED_SENSITIVE": ("security-design",),
    "MIN_N_UNSET": ("security-design",),
}
ADVISORY_CODES = set(GUIDE_ANCHORS)


# Advisory name heuristics from docs/ontology-design.md.  They are deliberately
# explicit module constants so a future design-guide revision can change the
# vocabulary without hiding policy in rule control flow.
AGGREGATE_PROPERTY_PREFIXES: tuple[str, ...] = (
    "avg_",
    "mean_",
    "total_",
    "count_",
    "sum_",
)
AGGREGATE_PROPERTY_SUFFIXES: tuple[str, ...] = (
    "_score",
    "_count",
    "_avg",
    "_total",
    "_rate",
)
CRUD_ACTION_PREFIXES: tuple[str, ...] = (
    "Set",
    "Update",
    "Delete",
    "Create",
    "Remove",
    "Erase",
)
FORBIDDEN_TYPE_SUFFIXES: tuple[str, ...] = ("V2", "V3", "History", "Snapshot")
DEFAULT_MIN_N = 3

_DEFAULT_SENSITIVITY = Sensitivity()


def _error(code: str, location: str, message: str, fix_hint: str) -> Finding:
    return Finding(
        code=code,
        severity="error",
        location=location,
        message=message,
        fix_hint=fix_hint,
    )


def _warn(
    code: str, location: str, message: str, fix_hint: str, guide: str
) -> Finding:
    return Finding(
        code=code,
        severity="warn",
        location=location,
        message=message,
        fix_hint=fix_hint,
        guide=guide,
    )


def _info(
    code: str, location: str, message: str, fix_hint: str, guide: str
) -> Finding:
    return Finding(
        code=code,
        severity="info",
        location=location,
        message=message,
        fix_hint=fix_hint,
        guide=guide,
    )


def _is_aggregate_name(name: str) -> bool:
    return name.startswith(AGGREGATE_PROPERTY_PREFIXES) or name.endswith(
        AGGREGATE_PROPERTY_SUFFIXES
    )


def _is_sensitivity_marked(prop: PropertyDef) -> bool:
    """Treat a non-default Sensitivity policy as a sensitivity marker.

    ``PropertyDef`` is the frozen diagnostic IR and does not preserve whether
    an author explicitly passed ``Sensitivity()`` when that value equals the
    default.  Restricted, non-default policies are therefore the honest
    statically observable marker for these lints; this avoids treating every
    ordinary property as sensitive.
    """
    return prop.sensitivity != _DEFAULT_SENSITIVITY


def _registry_owned_default_findings(
    definition: "OntologyDef"
) -> tuple[Finding, ...]:
    registry = definition.registry
    findings: list[Finding] = []

    for api_name in sorted(registry.object_types):
        obj = registry.object_types[api_name]
        owned_defaults = obj.owned_property_defaults()
        if not owned_defaults:
            continue
        prop_by_name = {prop.name: prop for prop in obj.properties}
        for prop_name in sorted(owned_defaults):
            default = owned_defaults[prop_name]
            location = f"ObjectTypeDef[{api_name!r}].owned[{prop_name!r}]"
            if prop_name == obj.primary_key:
                findings.append(
                    _error(
                        "ONTOLOGY_INVALID",
                        location,
                        f"ObjectTypeDef {api_name!r}: owned property "
                        f"{prop_name!r} is the primary key, which can never "
                        "be individually owned (use owned=True for the "
                        "whole type)",
                        "Use owned=True for a whole-type declaration, or remove "
                        "the primary-key entry from owned properties.",
                    )
                )
                continue
            prop = prop_by_name.get(prop_name)
            if prop is None:
                findings.append(
                    _error(
                        "ONTOLOGY_INVALID",
                        location,
                        f"ObjectTypeDef {api_name!r}: owned property "
                        f"{prop_name!r} is not one of this type's properties",
                        "Declare the property on the object type or remove it "
                        "from owned properties.",
                    )
                )
                continue
            if default is None:
                if prop.required:
                    findings.append(
                        _error(
                            "ONTOLOGY_INVALID",
                            location,
                            f"ObjectTypeDef {api_name!r}: owned property "
                            f"{prop_name!r} default is None but the property "
                            "is required",
                            "Give the required property a value compatible with "
                            "its declaration, or mark it optional.",
                        )
                    )
                continue
            mismatch = validate_scalar(
                default, prop.type, prop.choices, fields=prop.fields
            )
            if mismatch is not None:
                findings.append(
                    _error(
                        "ONTOLOGY_INVALID",
                        location,
                        f"ObjectTypeDef {api_name!r}: owned property "
                        f"{prop_name!r} default {mismatch}",
                        "Replace the default with a value matching the declared "
                        "property type.",
                    )
                )
    return tuple(findings)


def _registry_link_findings(
    definition: "OntologyDef"
) -> tuple[Finding, ...]:
    registry = definition.registry
    object_types = registry.object_types
    findings: list[Finding] = []

    for api_name in sorted(registry.link_types):
        link = registry.link_types[api_name]
        if link.from_type not in object_types:
            findings.append(
                _error(
                    "ONTOLOGY_INVALID",
                    f"LinkTypeDef[{api_name!r}].from_type",
                    f"LinkTypeDef {api_name!r}: dangling from_type "
                    f"{link.from_type!r}",
                    "Register the link's from_type object or correct the link "
                    "declaration.",
                )
            )
        if link.to_type not in object_types:
            findings.append(
                _error(
                    "ONTOLOGY_INVALID",
                    f"LinkTypeDef[{api_name!r}].to_type",
                    f"LinkTypeDef {api_name!r}: dangling to_type "
                    f"{link.to_type!r}",
                    "Register the link's to_type object or correct the link "
                    "declaration.",
                )
            )
    return tuple(findings)


def _registry_action_findings(
    definition: "OntologyDef"
) -> tuple[Finding, ...]:
    registry = definition.registry
    object_types = registry.object_types
    capabilities = registry.capabilities
    findings: list[Finding] = []

    for api_name in sorted(registry.action_types):
        action: ActionTypeDef = registry.action_types[api_name]
        base = f"ActionTypeDef[{api_name!r}]"
        if action.target_type not in object_types:
            findings.append(
                _error(
                    "ONTOLOGY_INVALID",
                    f"{base}.target_type",
                    f"ActionTypeDef {api_name!r}: dangling target_type "
                    f"{action.target_type!r}",
                    "Register the target object type or point the action at a "
                    "declared object type.",
                )
            )

        for capability in sorted(action.capabilities):
            if capability not in capabilities:
                findings.append(
                    _error(
                        "ONTOLOGY_INVALID",
                        f"{base}.capabilities[{capability!r}]",
                        f"ActionTypeDef {api_name!r}: dangling capability "
                        f"{capability!r}",
                        "Declare the capability before using it on the action, "
                        "or remove the reference.",
                    )
                )

        for index, param in enumerate(action.parameters):
            param_location = f"{base}.parameters[{index}]"
            if param.scope_semantics is not None and param.refers_to is None:
                findings.append(
                    _error(
                        "ONTOLOGY_INVALID",
                        param_location,
                        f"ActionTypeDef {api_name!r} parameter "
                        f"{param.name!r}: scope_semantics "
                        f"{param.scope_semantics!r} requires refers_to",
                        "Set refers_to on the parameter or remove its scope "
                        "semantics marker.",
                    )
                )
            if param.refers_to is not None and param.refers_to not in object_types:
                findings.append(
                    _error(
                        "ONTOLOGY_INVALID",
                        param_location,
                        f"ActionTypeDef {api_name!r} parameter "
                        f"{param.name!r}: dangling refers_to "
                        f"{param.refers_to!r}",
                        "Register the referenced object type or correct the "
                        "parameter reference.",
                    )
                )

        mismatch = target_param_mismatch(action)
        if mismatch is not None:
            index, message = mismatch
            findings.append(
                _error(
                    "ONTOLOGY_INVALID",
                    f"{base}.parameters[{index}]",
                    message,
                    "Point the action's target= at the parameter's type, "
                    "or mark the parameter scope_ref()/ref() instead of "
                    "target().",
                )
            )
    return tuple(findings)


def _registry_function_findings(
    definition: "OntologyDef"
) -> tuple[Finding, ...]:
    registry = definition.registry
    capabilities = registry.capabilities
    findings: list[Finding] = []

    for api_name in sorted(registry.functions):
        function = registry.functions[api_name]
        for capability in sorted(function.capabilities):
            if capability not in capabilities:
                findings.append(
                    _error(
                        "ONTOLOGY_INVALID",
                        f"FunctionDef[{api_name!r}].capabilities[{capability!r}]",
                        f"FunctionDef {api_name!r}: dangling capability "
                        f"{capability!r}",
                        "Declare the capability before using it on the function, "
                        "or remove the reference.",
                    )
                )
    return tuple(findings)


def _scope_policy_findings(
    definition: "OntologyDef"
) -> tuple[Finding, ...]:
    policy: ScopePolicy = definition.policy
    registry = definition.registry
    object_types = registry.object_types
    link_types = registry.link_types
    findings: list[Finding] = []

    levels = policy.levels
    if not levels:
        findings.append(
            _error(
                "SCOPE_POLICY_ERROR",
                "ScopePolicy.levels",
                "levels must not be empty",
                "Declare at least one unique scope level.",
            )
        )
    else:
        try:
            duplicate_levels = len(levels) != len(set(levels))
        except TypeError:
            duplicate_levels = True
        if duplicate_levels:
            findings.append(
                _error(
                    "SCOPE_POLICY_ERROR",
                    "ScopePolicy.levels",
                    "levels must not contain duplicate entries",
                    "Declare each scope level exactly once.",
                )
            )

    if not isinstance(policy.min_n, int) or isinstance(policy.min_n, bool) or policy.min_n < 1:
        findings.append(
            _error(
                "SCOPE_POLICY_ERROR",
                "ScopePolicy.min_n",
                f"min_n must be greater than or equal to 1, got {policy.min_n!r}",
                "Set min_n to an integer greater than or equal to 1.",
            )
        )

    level_set = set(levels) if all(isinstance(level, str) for level in levels) else set()
    for obj_type in sorted(policy.unscoped_types):
        if obj_type not in object_types:
            findings.append(
                _error(
                    "SCOPE_POLICY_ERROR",
                    f"ScopePolicy.unscoped_types[{obj_type!r}]",
                    f"ScopePolicy.unscoped_types: undeclared object type {obj_type!r}",
                    "Register the object type or remove it from unscoped_types.",
                )
            )

    for message in incoherent_scope_declarations(policy):
        findings.append(
            _error(
                "SCOPE_POLICY_ERROR",
                message.split(":", 1)[0],
                message,
                "Remove the type from unscoped_types, or drop its rules entry.",
            )
        )

    for api_name, index, message in unscoped_scope_parameter_declarations(
        policy, registry
    ):
        findings.append(
            _error(
                "SCOPE_POLICY_ERROR",
                f"ActionTypeDef[{api_name!r}].parameters[{index}]",
                message,
                "Use scope_semantics 'target' for the parameter, or give the "
                "type a scope rule instead of listing it as unscoped.",
            )
        )

    for obj_type in sorted(policy.row_visibility):
        if obj_type not in object_types:
            findings.append(
                _error(
                    "SCOPE_POLICY_ERROR",
                    f"ScopePolicy.row_visibility[{obj_type!r}]",
                    f"ScopePolicy.row_visibility: undeclared object type {obj_type!r}",
                    "Register the object type or remove its row-visibility rule.",
                )
            )

    findings.extend(
        _scope_rule_list_findings(
            object_types,
            link_types,
            policy.rules,
            group="rules",
            check_level=True,
            level_set=level_set,
        )
    )
    findings.extend(
        _scope_rule_list_findings(
            object_types,
            link_types,
            policy.contributor_rules,
            group="contributor_rules",
            check_level=False,
            level_set=level_set,
            require_non_empty=True,
        )
    )
    return tuple(findings)


def _scope_rule_list_findings(
    object_types: dict[str, ObjectTypeDef],
    link_types: Mapping[str, LinkTypeDef],
    rule_lists: Mapping[str, list[ScopeRule]],
    *,
    group: str,
    check_level: bool,
    level_set: set[str],
    require_non_empty: bool = False,
) -> list[Finding]:
    findings: list[Finding] = []
    for obj_type in sorted(rule_lists):
        rule_list = rule_lists[obj_type]
        prefix = f"ScopePolicy.{group}[{obj_type!r}]"
        if require_non_empty and not rule_list:
            # `ScopePolicy.validate` refuses this at startup; a sweep that
            # walked only the rules INSIDE each list could never see a defect
            # that is a property of the list itself, and reported clean.
            findings.append(
                _error(
                    "SCOPE_POLICY_ERROR",
                    prefix,
                    f"{prefix}: empty rule list -- a type declaring "
                    "contributor rules must supply at least one rule",
                    "Remove the entry to opt out of contributor "
                    "de-duplication, or declare a rule.",
                )
            )
        if obj_type not in object_types:
            findings.append(
                _error(
                    "SCOPE_POLICY_ERROR",
                    prefix,
                    f"{prefix}: undeclared object type {obj_type!r}",
                    "Register the object type or remove this policy rule list.",
                )
            )
            continue

        for index, rule in enumerate(rule_list):
            location = f"{prefix}[{index}]"
            if check_level and isinstance(rule, (SelfScope, DirectProperty, CustomResolver)):
                if rule.level not in level_set:
                    findings.append(
                        _error(
                            "SCOPE_POLICY_ERROR",
                            location,
                            f"{location}: undeclared scope level {rule.level!r}",
                            "Add the level to ScopePolicy.levels or correct the "
                            "rule's level.",
                        )
                    )

            if isinstance(rule, DirectProperty):
                prop_names = {prop.name for prop in object_types[obj_type].properties}
                if rule.property_name not in prop_names:
                    findings.append(
                        _error(
                            "SCOPE_POLICY_ERROR",
                            location,
                            f"{location}: property {rule.property_name!r} not "
                            f"declared on {obj_type!r}",
                            "Declare the direct-property field or correct the "
                            "policy rule.",
                        )
                    )

            if isinstance(rule, ViaLink):
                if rule.parent_type not in object_types:
                    findings.append(
                        _error(
                            "SCOPE_POLICY_ERROR",
                            location,
                            f"{location}: undeclared parent_type "
                            f"{rule.parent_type!r}",
                            "Register the parent object type or correct the "
                            "ViaLink rule.",
                        )
                    )
                if rule.link_api_name not in link_types:
                    findings.append(
                        _error(
                            "SCOPE_POLICY_ERROR",
                            location,
                            f"{location}: undeclared link {rule.link_api_name!r}",
                            "Register the link or correct the ViaLink name.",
                        )
                    )
                else:
                    link_def = link_types[rule.link_api_name]
                    if rule.direction == "from":
                        expected_from, expected_to = obj_type, rule.parent_type
                    else:
                        expected_from, expected_to = rule.parent_type, obj_type
                    if (link_def.from_type, link_def.to_type) != (
                        expected_from,
                        expected_to,
                    ):
                        findings.append(
                            _error(
                                "SCOPE_POLICY_ERROR",
                                location,
                                f"{location}: direction {rule.direction!r} expects "
                                f"link {rule.link_api_name!r} to run "
                                f"{expected_from!r} -> {expected_to!r}, but it is "
                                f"declared {link_def.from_type!r} -> "
                                f"{link_def.to_type!r}",
                                "Align the ViaLink direction and parent type with "
                                "the link declaration.",
                            )
                        )
    return findings


def _stored_derivable_findings(definition: "OntologyDef") -> tuple[Finding, ...]:
    """Warn on aggregate-shaped properties that look like stored rollups."""
    findings: list[Finding] = []
    for api_name in sorted(definition.registry.object_types):
        obj = definition.registry.object_types[api_name]
        if obj.snapshot:
            continue
        for prop in sorted(obj.properties, key=lambda item: item.name):
            if "STORED_DERIVABLE" in prop.accept or not _is_aggregate_name(prop.name):
                continue
            findings.append(
                _warn(
                    "STORED_DERIVABLE",
                    f"object {api_name}, property {prop.name}",
                    "the name reads as a score or aggregate",
                    f"if {api_name}.{prop.name} is computed from other rows, "
                    "derive it with a Function; "
                    'if it is recorded from outside, add accept="STORED_DERIVABLE" '
                    "to the property",
                    GUIDE_URL + "#" + GUIDE_ANCHORS["STORED_DERIVABLE"][0],
                )
            )
    return tuple(findings)


def _api_name_words(api_name: str) -> list[str]:
    """Split snake case and camel case, including uppercase acronym words."""
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", api_name.lstrip("_"))
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", separated)
    return [word for word in separated.split("_") if word]


def _crud_action_name_findings(definition: "OntologyDef") -> tuple[Finding, ...]:
    """Warn when an action or Function starts with a storage operation word."""
    findings: list[Finding] = []
    for kind, declarations in (
        ("action", definition.registry.action_types),
        ("function", definition.registry.functions),
    ):
        for api_name in sorted(declarations):
            declaration = declarations[api_name]
            if "CRUD_ACTION_NAME" in declaration.accept:
                continue
            words = _api_name_words(api_name)
            if not words or words[0].casefold() not in {
                verb.casefold() for verb in CRUD_ACTION_PREFIXES
            }:
                continue
            verb = words[0].capitalize()
            remainder = "".join(
                word.capitalize() if word.isupper() else word[0].upper() + word[1:]
                for word in words[1:]
            )
            retirement = verb in {"Delete", "Remove", "Erase"}
            anchor = "retirement-and-removal" if retirement else "action-sprawl"
            outcome = "Withdraw" if retirement else ("Register" if verb == "Create" else "Change")
            hint = f"name the outcome, for example {outcome}{remainder}"
            if retirement:
                hint += ", and call ctx.retire() inside the handler"
            hint += '; if the storage operation is intentional, add accept="CRUD_ACTION_NAME"'
            findings.append(
                _warn(
                    "CRUD_ACTION_NAME",
                    f"{kind} {api_name}",
                    f'"{verb}" names a storage operation, not a business outcome',
                    hint,
                    GUIDE_URL + "#" + anchor,
                )
            )
    return tuple(findings)


def _forbidden_type_name_findings(definition: "OntologyDef") -> tuple[Finding, ...]:
    """Warn on version/history clones, except explicitly declared snapshots."""
    findings: list[Finding] = []
    for api_name in sorted(definition.registry.object_types):
        obj = definition.registry.object_types[api_name]
        if "FORBIDDEN_TYPE_NAME" in obj.accept:
            continue
        match = re.search(r"(V[0-9]+|(?:19|20)[0-9]{2}|History|Snapshot)$", api_name)
        if match is None or match.start() == 0:
            continue
        suffix = match.group()
        base_name = api_name[: match.start()]
        if suffix == "Snapshot" and obj.snapshot:
            continue
        kind = (
            "version" if suffix.startswith("V") else "year" if suffix.isdigit() else suffix.lower()
        )
        findings.append(
            _warn(
                "FORBIDDEN_TYPE_NAME",
                f"object {api_name}",
                f'the name ends in "{suffix}", which reads as a {kind} clone',
                f"keep one type, {base_name}; the store already keeps row history. "
                "If a point-in-time value must be first-class, declare the type with "
                'snapshot=True; if the name is intentional, add accept="FORBIDDEN_TYPE_NAME"',
                GUIDE_URL + "#" + GUIDE_ANCHORS["FORBIDDEN_TYPE_NAME"][0],
            )
        )
    return tuple(findings)



def _free_text_status_findings(definition: "OntologyDef") -> tuple[Finding, ...]:
    """Warn when a string status has no declared allowed values."""
    findings: list[Finding] = []
    for api_name in sorted(definition.registry.object_types):
        obj = definition.registry.object_types[api_name]
        for prop in sorted(obj.properties, key=lambda item: item.name):
            if (
                "FREE_TEXT_STATUS" in prop.accept
                or prop.type != "str"
                or prop.choices is not None
                or not (prop.name == "status" or prop.name.endswith("_status"))
            ):
                continue
            findings.append(
                _warn(
                    "FREE_TEXT_STATUS",
                    f"object {api_name}, property {prop.name}",
                    "a status stored as free text accepts any value",
                    f"annotate {api_name}.{prop.name} with a StrEnum or Literal "
                    "so every write path enforces the allowed values; "
                    'if free text is intentional, add accept="FREE_TEXT_STATUS" to the property',
                    GUIDE_URL + "#choice-properties",
                )
            )
    return tuple(findings)


def _audit_type_findings(definition: "OntologyDef") -> tuple[Finding, ...]:
    """Warn on types named like the engine's existing action audit."""
    findings: list[Finding] = []
    for api_name in sorted(definition.registry.object_types):
        obj = definition.registry.object_types[api_name]
        if "AUDIT_TYPE" in obj.accept or not api_name.endswith(
            ("AuditLog", "AuditEntry", "AuditTrail", "AuditRecord", "AuditEvent")
        ):
            continue
        findings.append(
            _warn(
                "AUDIT_TYPE",
                f"object {api_name}",
                "the type looks like a duplicate of the engine's action audit",
                f"remove {api_name} because the engine already records every action "
                'as AuditEntry; if it represents a domain fact, add accept="AUDIT_TYPE"',
                GUIDE_URL + "#the-golden-hammer",
            )
        )
    return tuple(findings)


def _micro_action_findings(definition: "OntologyDef") -> tuple[Finding, ...]:
    """Warn on the declared shape of a likely one-property write action.

    The descriptor IR does not contain a write set or handler body, so this is
    intentionally only a shape heuristic: exactly one parameter without a
    ``refers_to`` target marker whose name is a declared property on the
    action's target object.  It does not claim that the handler actually writes
    that property.
    """
    findings: list[Finding] = []
    object_types = definition.registry.object_types
    for api_name in sorted(definition.registry.action_types):
        action = definition.registry.action_types[api_name]
        if "MICRO_ACTION" in action.accept:
            continue
        target = object_types.get(action.target_type)
        if target is None:
            continue
        non_target = [param for param in action.parameters if param.refers_to is None]
        if len(non_target) != 1:
            continue
        parameter = non_target[0]
        if parameter.name not in {prop.name for prop in target.properties}:
            continue
        findings.append(
            _warn(
                "MICRO_ACTION",
                f"action {api_name}, parameter {parameter.name}",
                f"action shape looks like a one-property write to "
                f"{action.target_type}.{parameter.name}",
                f"make {api_name} express a business outcome for {action.target_type} "
                f"and change {parameter.name} alongside the other facts that outcome "
                'requires; if this one-property action is intentional, add accept="MICRO_ACTION"',
                GUIDE_URL + "#" + GUIDE_ANCHORS["MICRO_ACTION"][0],
            )
        )
    return tuple(findings)


def _unscoped_sensitive_findings(
    definition: "OntologyDef"
) -> tuple[Finding, ...]:
    """Warn when a non-default sensitive property lacks scope or an exemption."""
    findings: list[Finding] = []
    for api_name in sorted(definition.registry.object_types):
        obj = definition.registry.object_types[api_name]
        if (
            api_name in definition.policy.unscoped_types
            or definition.policy.rules.get(api_name)
        ):
            continue
        for prop in sorted(obj.properties, key=lambda item: item.name):
            if not _is_sensitivity_marked(prop):
                continue
            findings.append(
                _warn(
                    "UNSCOPED_SENSITIVE",
                    f"object {api_name}, property {prop.name}",
                    "sensitive property has no scope rule for its object type",
                    "Add a scope rule for the object type or explicitly mark "
                    "the type as unscoped.",
                    GUIDE_URL + "#" + GUIDE_ANCHORS["UNSCOPED_SENSITIVE"][0],
                )
            )
    return tuple(findings)


def _min_n_unset_findings(
    definition: "OntologyDef"
) -> tuple[Finding, ...]:
    """Nudge authors to choose ``min_n`` when sensitivity is declared.

    Function bodies and contributor-resolution behavior are not statically
    inspectable here.  The honest closest heuristic is therefore the policy
    value ``3`` (the constructor default) plus at least one non-default
    sensitivity policy; it intentionally does not infer whether an aggregate
    will be called.
    """
    if definition.policy.min_n != DEFAULT_MIN_N:
        return ()
    if not any(
        _is_sensitivity_marked(prop)
        for obj in definition.registry.object_types.values()
        for prop in obj.properties
    ):
        return ()
    return (
        _info(
            "MIN_N_UNSET",
            "ScopePolicy.min_n",
            "min_n is left at the default while sensitivity is declared",
            "Set min_n explicitly for the ontology's privacy requirements.",
            GUIDE_URL + "#" + GUIDE_ANCHORS["MIN_N_UNSET"][0],
        ),
    )


RULES: tuple[DiagnosticRule, ...] = (
    _registry_owned_default_findings,
    _registry_link_findings,
    _registry_action_findings,
    _registry_function_findings,
    _scope_policy_findings,
    _stored_derivable_findings,
    _crud_action_name_findings,
    _forbidden_type_name_findings,
    _micro_action_findings,
    _free_text_status_findings,
    _audit_type_findings,
    _unscoped_sensitive_findings,
    _min_n_unset_findings,
)


def _collect_findings(
    definition: "OntologyDef",
    *,
    store: Store | None = None,
    classes: Mapping[str, type[BaseModel]] | None = None,
) -> list[Finding]:
    """Run every rule, turning an unexpected rule failure into a finding.

    With a ``store``, sweep hydration and declared rules over current rows,
    one object type at a time. A failed read becomes a finding, while other
    types are still swept.
    """
    findings: list[Finding] = []
    for rule in RULES:
        try:
            findings.extend(rule(definition))
        except Exception as exc:
            findings.append(
                _error(
                    "DIAGNOSE_RULE_FAILED",
                    rule.__name__,
                    f"diagnostic rule {rule.__name__!r} failed: {exc}",
                    "Fix the reported rule error and run diagnose() again.",
                )
            )
    if store is None:
        return findings
    for api_name in sorted(definition.registry.object_types):
        try:
            findings.extend(
                _store_row_findings(definition, store, api_name, classes or {})
            )
        except Exception as exc:
            findings.append(
                _error(
                    "DIAGNOSE_RULE_FAILED",
                    f"_store_row_findings[{api_name!r}]",
                    f"store sweep of {api_name!r} failed: {exc}",
                    "Fix the reported store error and run diagnose() again.",
                )
            )
        try:
            findings.extend(_stored_rule_findings(definition, store, api_name))
        except Exception as exc:
            findings.append(
                _error(
                    "DIAGNOSE_RULE_FAILED",
                    f"_stored_rule_findings[{api_name!r}]",
                    f"rule sweep of {api_name!r} failed: {exc}",
                    "Fix the reported store error and run diagnose() again.",
                )
            )
    return findings


def _stored_rule_findings(
    definition: "OntologyDef", store: Store, api_name: str
) -> tuple[Finding, ...]:
    """Report one finding per declared rule broken by current stored rows."""
    obj_def = definition.registry.object_types[api_name]
    if not obj_def.rules:
        return ()
    rows = store.read_all(api_name)
    findings: list[Finding] = []
    for rule in obj_def.rules:
        failed_ids: list[str] = []
        for row in rows:
            try:
                passed = rule.check(row.payload)
            except Exception:
                passed = False
            if not passed:
                failed_ids.append(row.lineage.object_id)
        if failed_ids:
            findings.append(
                _error(
                    "RULE_VIOLATED",
                    f"ObjectTypeDef[{api_name!r}].rules[{rule.name!r}]",
                    f"{len(failed_ids)} of {len(rows)} current rows break rule "
                    f"{rule.name!r} ({rule.message}) (e.g. id {failed_ids[0]!r})",
                    "fix those rows with an action, or re-ingest them",
                )
            )
    return tuple(findings)


def _store_row_findings(
    definition: "OntologyDef",
    store: Store,
    api_name: str,
    classes: Mapping[str, type[BaseModel]],
) -> tuple[Finding, ...]:
    """Report current rows of ``api_name`` that ``hydrate`` would refuse.

    The store persists no registry fingerprint, so an ontology edited
    after rows were written (a new required property, a changed type or
    ``choices``) passes ``validate()`` and fails only at the first read
    with ``INVALID_RECORD`` (ontary#40). This sweep runs the same checks
    ``hydrate`` runs -- the storage scalar check per property, then the
    authored class's ``model_validate`` when one is registered -- and
    reports one finding per (type, property) with a row count, never one
    per row. A hand-built registry with no class falls back to the
    descriptors: ``PropertyDef.required`` and ``validate_scalar``.
    """
    obj_def = definition.registry.object_types[api_name]
    cls = classes.get(api_name)
    rows = store.read_all(api_name)
    # property name ("" = not attributable to one property) ->
    # (rows missing a required value, rows with a bad value, first detail)
    missing: dict[str, int] = {}
    invalid: dict[str, int] = {}
    example: dict[str, str] = {}

    for row in rows:
        row_missing, row_invalid = _row_hydration_failures(obj_def, cls, row.payload)
        for name in row_missing:
            missing[name] = missing.get(name, 0) + 1
        for name, detail in row_invalid.items():
            if name in row_missing:
                continue
            invalid[name] = invalid.get(name, 0) + 1
            example.setdefault(
                name, f"{detail} (e.g. id {row.lineage.object_id!r})"
            )

    findings: list[Finding] = []
    total = len(rows)
    base = f"ObjectTypeDef[{api_name!r}]"
    for name in sorted(set(missing) | set(invalid)):
        problems: list[str] = []
        if name in missing:
            problems.append(f"{missing[name]} missing a required value")
        if name in invalid:
            problems.append(f"{invalid[name]} with {example[name]}")
        what = f"property {name!r}" if name else "payload"
        findings.append(
            _error(
                "INVALID_RECORD",
                f"{base}.properties[{name!r}]" if name else base,
                f"ObjectTypeDef {api_name!r} {what}: "
                f"{missing.get(name, 0) + invalid.get(name, 0)} of {total} "
                f"current rows would fail hydration ({'; '.join(problems)})",
                "Backfill or re-ingest the stored rows in the new shape, or "
                "make the property optional / give it a default.",
            )
        )
    return tuple(findings)


def _row_hydration_failures(
    obj_def: ObjectTypeDef,
    cls: type[BaseModel] | None,
    payload: Mapping[str, object],
) -> tuple[set[str], dict[str, str]]:
    """Return (properties missing a required value, property -> first
    violation) for one stored payload, mirroring ``hydrate``'s checks.
    Property name ``""`` stands for a failure not attributable to one
    property (e.g. a model-level validator)."""
    missing: set[str] = set()
    invalid: dict[str, str] = {}
    for prop in obj_def.properties:
        value = payload.get(prop.name)
        if value is None:
            # Absent with a class: its model_validate decides (a Pydantic
            # default may fill it). An explicit null, or no class at all,
            # fails exactly when the property is not optional.
            if prop.required and (prop.name in payload or cls is None):
                missing.add(prop.name)
            continue
        violation = _storage_scalar_violation(
            value, prop.type, prop.choices, fields=prop.fields
        )
        if violation is None and cls is None:
            violation = validate_scalar(
                value, prop.type, prop.choices, fields=prop.fields
            )
        if violation is not None:
            invalid.setdefault(prop.name, violation)
    if cls is not None:
        try:
            cls.model_validate(payload)
        except ValidationError as exc:
            for err in exc.errors():
                loc = err["loc"]
                name = str(loc[0]) if loc else ""
                if err["type"] == "missing":
                    missing.add(name)
                else:
                    invalid.setdefault(name, str(err["msg"]))
    return missing, invalid
