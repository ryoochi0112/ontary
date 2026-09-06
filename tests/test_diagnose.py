"""T8 diagnostics: complete validation findings without freezing authoring."""

from __future__ import annotations

import json

import pytest
from conftest import raises_code

import ontary.diagnose as diagnose_module
from ontary import Cardinality, Finding, Ontology, OntologyObject, SelfScope, prop
from ontary.errors import ValidationFailed
from ontary.meta import ActionTypeDef, FunctionDef, LinkTypeDef
from ontary.scope import ViaLink


def _ontology_with_two_independent_defects() -> Ontology:
    ontology = Ontology("diagnose", scope_levels=["org"])

    @ontology.object(
        layer="L0",
        api_name="Widget",
        version=2,
        scope=[SelfScope(level="org")],
    )
    class Widget(OntologyObject):
        id: str = prop(primary_key=True)

    ontology.registry.register_action_type(
        ActionTypeDef(
            api_name="Retarget",
            display_name="Retarget",
            target_type="MissingType",
            executable_by_roles=["Operator"],
            description="Targets an undeclared object type.",
        )
    )
    return ontology


def test_diagnose_reports_independent_defects_in_one_sweep() -> None:
    ontology = _ontology_with_two_independent_defects()

    findings = ontology.diagnose()

    assert len(findings) == 2
    assert all(finding.severity == "error" for finding in findings)
    assert all(finding.code == "ONTOLOGY_INVALID" for finding in findings)
    messages = {finding.message for finding in findings}
    assert any("no upcaster from version(s) [1]" in message for message in messages)
    assert any("dangling target_type 'MissingType'" in message for message in messages)


def test_clean_ontology_has_no_findings() -> None:
    ontology = Ontology("clean", scope_levels=["org"])

    @ontology.object(layer="L0", scope=[SelfScope(level="org")])
    class Widget(OntologyObject):
        id: str = prop(primary_key=True)

    assert ontology.diagnose() == []


def test_diagnose_reports_owned_default_outside_property_choices() -> None:
    ontology = Ontology("choices-default", scope_levels=["org"])

    @ontology.object(
        layer="L0", scope="unscoped", owned={"status": "pending"}
    )
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        status: str = prop(choices=["open", "closed"])

    findings = ontology.diagnose()

    assert len(findings) == 1
    assert findings[0].code == "ONTOLOGY_INVALID"
    assert "status" in findings[0].message


def test_findings_round_trip_through_json_model_dump() -> None:
    findings = _ontology_with_two_independent_defects().diagnose()

    dumped = [finding.model_dump(mode="json") for finding in findings]
    json.dumps(dumped)
    assert [Finding.model_validate(item) for item in dumped] == findings


def test_validate_keeps_the_existing_exception_and_message() -> None:
    ontology = Ontology("validate", scope_levels=["org"])

    @ontology.object(layer="L0", api_name="Widget", version=2)
    class Widget(OntologyObject):
        id: str = prop(primary_key=True)

    with pytest.raises(ValidationFailed) as exc_info:
        ontology.validate()

    assert exc_info.value.code == "ONTOLOGY_INVALID"
    assert str(exc_info.value) == (
        "ObjectTypeDef 'Widget' is declared version 2 but has no upcaster "
        "from version(s) [1] -- a row stored at any of those versions could "
        "not be read. Declare one upcaster per step, or migrate the rows and "
        "drop the version bump"
    )


def test_diagnose_does_not_freeze_an_ontology_still_being_authored() -> None:
    ontology = Ontology("unfrozen", scope_levels=["org"])

    @ontology.object(layer="L0")
    class First(OntologyObject):
        id: str = prop(primary_key=True)

    assert ontology.diagnose() == []

    @ontology.object(layer="L0")
    class Second(OntologyObject):
        id: str = prop(primary_key=True)

    assert ontology._definition is None
    assert set(ontology.registry.object_types) == {"First", "Second"}


def test_diagnose_reports_constructor_rejected_scope_policy_values() -> None:
    findings = Ontology("invalid-policy", scope_levels=[], min_n=0).diagnose()

    assert {(finding.code, finding.location) for finding in findings} == {
        ("SCOPE_POLICY_ERROR", "ScopePolicy.levels"),
        ("SCOPE_POLICY_ERROR", "ScopePolicy.min_n"),
    }


def test_diagnose_reports_duplicate_scope_levels() -> None:
    findings = Ontology("duplicate-levels", scope_levels=["org", "org"]).diagnose()

    assert len(findings) == 1
    assert findings[0].code == "SCOPE_POLICY_ERROR"
    assert findings[0].location == "ScopePolicy.levels"
    assert "duplicate entries" in findings[0].message


def test_diagnose_reports_an_empty_contributor_rule_list() -> None:
    """`diagnose` must report every defect `validate` refuses.

    `ScopePolicy.validate` treats an empty `contributor_rules` list as a
    declaration error -- the type opts into contributor de-dup, which
    releases `GuardedQuery.aggregate`'s hidden-field exemption, while
    supplying no identity to de-dup by. `diagnose` walked the same rule
    lists but only ever looked at rules INSIDE them, so the one defect that
    is a property of the list itself was invisible: the author saw a clean
    sweep and a startup refusal.
    """
    ontology = Ontology("empty-contributor", scope_levels=["org"])

    @ontology.object(layer="L0", scope=[SelfScope(level="org")], contributor=[])
    class Reading(OntologyObject):
        id: str = prop(primary_key=True)

    findings = ontology.diagnose()

    assert len(findings) == 1
    assert findings[0].code == "SCOPE_POLICY_ERROR"
    assert findings[0].location == "ScopePolicy.contributor_rules['Reading']"
    assert "empty rule list" in findings[0].message

    # Parity is the point: the sweep now reports exactly what startup refuses.
    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR"):
        ontology.validate()


def test_diagnose_reports_a_via_link_rule_with_an_undeclared_link() -> None:
    ontology = Ontology("missing-link", scope_levels=["org"])

    @ontology.object(layer="L0", scope=[SelfScope(level="org")])
    class Parent(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[
            ViaLink(
                link_api_name="MissingLink",
                direction="from",
                parent_type="Parent",
            )
        ],
    )
    class Child(OntologyObject):
        id: str = prop(primary_key=True)

    findings = ontology.diagnose()

    assert len(findings) == 1
    assert findings[0].code == "SCOPE_POLICY_ERROR"
    assert "undeclared link 'MissingLink'" in findings[0].message


def test_diagnose_reports_an_invalid_owned_property_default() -> None:
    ontology = Ontology("owned-default", scope_levels=["org"])

    @ontology.object(
        layer="L0",
        scope=[SelfScope(level="org")],
        owned={"label": 42},
    )
    class Widget(OntologyObject):
        id: str = prop(primary_key=True)
        label: str = prop()

    findings = ontology.diagnose()

    assert len(findings) == 1
    assert findings[0].code == "ONTOLOGY_INVALID"
    assert findings[0].location == "ObjectTypeDef['Widget'].owned['label']"
    assert "default" in findings[0].message


def test_diagnose_reports_a_link_with_a_dangling_endpoint() -> None:
    ontology = Ontology("dangling-link", scope_levels=["org"])

    @ontology.object(layer="L0", scope=[SelfScope(level="org")])
    class Widget(OntologyObject):
        id: str = prop(primary_key=True)

    ontology.registry.register_link_type(
        LinkTypeDef(
            api_name="BrokenLink",
            from_type="MissingType",
            to_type="Widget",
            cardinality=Cardinality.MANY_TO_ONE,
            description="A deliberately dangling link.",
        )
    )

    findings = ontology.diagnose()

    assert len(findings) == 1
    assert findings[0].code == "ONTOLOGY_INVALID"
    assert findings[0].location == "LinkTypeDef['BrokenLink'].from_type"
    assert "dangling from_type 'MissingType'" in findings[0].message


def test_diagnose_reports_a_function_with_a_dangling_capability() -> None:
    ontology = Ontology("dangling-function", scope_levels=["org"])
    ontology.registry.register_function(
        FunctionDef(
            api_name="Compute",
            description="A deliberately dangling function.",
            input_description="",
            output_description="",
            capabilities=["MissingCapability"],
        )
    )

    findings = ontology.diagnose()

    assert len(findings) == 1
    assert findings[0].code == "ONTOLOGY_INVALID"
    assert findings[0].location == (
        "FunctionDef['Compute'].capabilities['MissingCapability']"
    )
    assert "dangling capability 'MissingCapability'" in findings[0].message


def test_diagnose_converts_a_rule_failure_into_a_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology = Ontology("rule-failure", scope_levels=["org"])

    @ontology.object(layer="L0", scope=[SelfScope(level="org")])
    class Widget(OntologyObject):
        id: str = prop(primary_key=True)

    ontology.registry.register_link_type(
        LinkTypeDef(
            api_name="BrokenLink",
            from_type="MissingType",
            to_type="Widget",
            cardinality=Cardinality.MANY_TO_ONE,
            description="A deliberately dangling link.",
        )
    )

    def broken_rule(_definition: object) -> tuple[Finding, ...]:
        raise RuntimeError("deliberate diagnostic failure")

    monkeypatch.setattr(
        diagnose_module,
        "RULES",
        (broken_rule, *diagnose_module.RULES[1:]),
    )

    findings = ontology.diagnose()

    assert {
        (finding.code, finding.location)
        for finding in findings
    } == {
        ("DIAGNOSE_RULE_FAILED", "broken_rule"),
        ("ONTOLOGY_INVALID", "LinkTypeDef['BrokenLink'].from_type"),
    }
    assert findings[0].code == "DIAGNOSE_RULE_FAILED"
    assert findings[0].severity == "error"
    assert findings[0].location == "broken_rule"
    assert "deliberate diagnostic failure" in findings[0].message
