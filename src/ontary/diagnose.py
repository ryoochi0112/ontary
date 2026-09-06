"""Complete, non-raising diagnostics for an authored ontology.

The core rules mirror the conditions checked by ``OntologyDef.validate`` and
its registry/policy validators.  They read descriptor data only; no rule
mutates the registry, policy, or store.  Later DX tasks can extend ``RULES``
with advisory rules without changing the collector contract.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from ontary.meta import (
    ActionTypeDef,
    LinkTypeDef,
    ObjectTypeDef,
    PropertyDef,
    Sensitivity,
)
from ontary.scope import (
    CustomResolver,
    DirectProperty,
    ScopePolicy,
    ScopeRule,
    SelfScope,
    ViaLink,
    incoherent_scope_declarations,
)
from ontary.typesys import validate_scalar

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


DiagnosticRule = Callable[["OntologyDef", "Store | None"], tuple[Finding, ...]]


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
# The design guide calls a first-class point-in-time value a snapshot.  This
# exact marker lets an author explicitly sanction the otherwise forbidden
# ``*Snapshot`` API-name suffix in the serializable ObjectTypeDef description.
DECLARED_SNAPSHOT_MARKER = "declared snapshot"

_DEFAULT_SENSITIVITY = Sensitivity()


def _error(code: str, location: str, message: str, fix_hint: str) -> Finding:
    return Finding(
        code=code,
        severity="error",
        location=location,
        message=message,
        fix_hint=fix_hint,
    )


def _warn(code: str, location: str, message: str, fix_hint: str) -> Finding:
    return Finding(
        code=code,
        severity="warn",
        location=location,
        message=message,
        fix_hint=fix_hint,
    )


def _info(code: str, location: str, message: str, fix_hint: str) -> Finding:
    return Finding(
        code=code,
        severity="info",
        location=location,
        message=message,
        fix_hint=fix_hint,
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


def _linked_source_type(definition: "OntologyDef", object_type: str) -> str:
    """Return the sole link sibling, or ``source`` when it is ambiguous."""
    links = [
        link
        for link in definition.registry.link_types.values()
        if link.from_type == object_type or link.to_type == object_type
    ]
    if len(links) != 1:
        return "source"
    link = links[0]
    if link.from_type == object_type and link.to_type != object_type:
        return link.to_type
    if link.to_type == object_type and link.from_type != object_type:
        return link.from_type
    return "source"


def _registry_upcaster_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    registry = definition.registry
    object_types = registry.object_types
    findings: list[Finding] = []

    for (api_name, from_version) in sorted(registry.upcasters):
        obj = object_types.get(api_name)
        location = f"OntologyRegistry.upcasters[{api_name!r}, {from_version}]"
        if obj is None:
            findings.append(
                _error(
                    "ONTOLOGY_INVALID",
                    location,
                    f"upcaster for unregistered object type {api_name!r} "
                    f"(from version {from_version})",
                    "Register the object type before declaring its upcaster, "
                    "or remove the orphaned upcaster.",
                )
            )
            continue
        if from_version >= obj.version:
            findings.append(
                _error(
                    "ONTOLOGY_INVALID",
                    location,
                    f"upcaster for {api_name!r} reads from version "
                    f"{from_version}, which is not older than the declared "
                    f"version {obj.version} -- it could never apply to a "
                    "stored row",
                    "Declare the step from an older stored version, or remove "
                    "this upcaster.",
                )
            )

    for api_name in sorted(object_types):
        obj = object_types[api_name]
        if obj.version <= 1:
            continue
        missing = [
            version
            for version in range(1, obj.version)
            if (api_name, version) not in registry.upcasters
        ]
        if missing:
            findings.append(
                _error(
                    "ONTOLOGY_INVALID",
                    f"ObjectTypeDef[{api_name!r}].version",
                    f"ObjectTypeDef {api_name!r} is declared version "
                    f"{obj.version} but has no upcaster from version(s) "
                    f"{missing} -- a row stored at any of those versions "
                    "could not be read. Declare one upcaster per step, or "
                    "migrate the rows and drop the version bump",
                    "Declare one upcaster for every missing version step, or "
                    "migrate those rows and remove the version bump.",
                )
            )
    return tuple(findings)


def _registry_owned_default_findings(
    definition: "OntologyDef", _store: "Store | None"
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
            mismatch = validate_scalar(default, prop.type, prop.choices)
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
    definition: "OntologyDef", _store: "Store | None"
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
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    registry = definition.registry
    object_types = registry.object_types
    capabilities = registry.capabilities
    effect_types = registry.effect_types
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

        for effect in sorted(action.effects):
            if effect not in effect_types:
                findings.append(
                    _error(
                        "ONTOLOGY_INVALID",
                        f"{base}.effects[{effect!r}]",
                        f"ActionTypeDef {api_name!r}: dangling effect "
                        f"{effect!r}",
                        "Declare the effect type before using it on the action, "
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
    return tuple(findings)


def _registry_function_findings(
    definition: "OntologyDef", _store: "Store | None"
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
    definition: "OntologyDef", _store: "Store | None"
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


def _stored_derivable_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    """Warn on aggregate-shaped properties that look like stored rollups."""
    findings: list[Finding] = []
    for api_name in sorted(definition.registry.object_types):
        obj = definition.registry.object_types[api_name]
        sibling = _linked_source_type(definition, api_name)
        for prop in sorted(obj.properties, key=lambda item: item.name):
            if not _is_aggregate_name(prop.name):
                continue
            findings.append(
                _warn(
                    "STORED_DERIVABLE",
                    f"object {api_name}, property {prop.name}",
                    "looks like a stored aggregate; facts are stored once and "
                    "derived by Functions",
                    "declare a Function that computes it from "
                    f"{sibling} rows, or mark the type as a declared snapshot",
                )
            )
    return tuple(findings)


def _crud_action_name_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    """Warn when an action API name exposes a storage-level CRUD operation."""
    findings: list[Finding] = []
    for api_name in sorted(definition.registry.action_types):
        if not api_name.startswith(CRUD_ACTION_PREFIXES):
            continue
        findings.append(
            _warn(
                "CRUD_ACTION_NAME",
                f"action {api_name}",
                "action name looks like a CRUD operation rather than a "
                "business verb",
                "Rename the action with the business outcome it performs.",
            )
        )
    return tuple(findings)


def _forbidden_type_name_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    """Warn on version/history clones, except explicitly declared snapshots."""
    findings: list[Finding] = []
    for api_name in sorted(definition.registry.object_types):
        obj = definition.registry.object_types[api_name]
        suffix = next(
            (candidate for candidate in FORBIDDEN_TYPE_SUFFIXES if api_name.endswith(candidate)),
            None,
        )
        if suffix is None:
            continue
        if suffix == "Snapshot" and DECLARED_SNAPSHOT_MARKER in obj.description.casefold():
            continue
        findings.append(
            _warn(
                "FORBIDDEN_TYPE_NAME",
                f"object {api_name}",
                "object type name looks like a version, history, or snapshot "
                "clone",
                "Keep one object type and use versioning/upcasters; declare a "
                "snapshot explicitly when a point-in-time value is first-class.",
            )
        )
    return tuple(findings)


def _micro_action_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
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
                "action shape looks like a single-property write",
                "Model the business transition as one invariant-preserving "
                "action instead of exposing a property setter.",
            )
        )
    return tuple(findings)


def _unscoped_sensitive_findings(
    definition: "OntologyDef", _store: "Store | None"
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
                )
            )
    return tuple(findings)


def _min_n_unset_findings(
    definition: "OntologyDef", _store: "Store | None"
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
        ),
    )


RULES: tuple[DiagnosticRule, ...] = (
    _registry_upcaster_findings,
    _registry_owned_default_findings,
    _registry_link_findings,
    _registry_action_findings,
    _registry_function_findings,
    _scope_policy_findings,
    _stored_derivable_findings,
    _crud_action_name_findings,
    _forbidden_type_name_findings,
    _micro_action_findings,
    _unscoped_sensitive_findings,
    _min_n_unset_findings,
)


def _collect_findings(
    definition: "OntologyDef", store: "Store | None"
) -> list[Finding]:
    """Run every rule, turning an unexpected rule failure into a finding."""
    findings: list[Finding] = []
    for rule in RULES:
        try:
            findings.extend(rule(definition, store))
        except Exception as exc:
            findings.append(
                _error(
                    "DIAGNOSE_RULE_FAILED",
                    rule.__name__,
                    f"diagnostic rule {rule.__name__!r} failed: {exc}",
                    "Fix the reported rule error and run diagnose() again.",
                )
            )
    return findings
