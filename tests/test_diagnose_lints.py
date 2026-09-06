"""T9 design-guide lint coverage and mutation pins."""

from __future__ import annotations

import json
from typing import Any

import pytest

import ontary.diagnose as diagnose_module
from ontary import (
    ActionParams,
    Cardinality,
    Ontology,
    OntologyObject,
    SelfScope,
    Sensitivity,
    prop,
    target,
)


def _scoped_ontology(name: str, *, min_n: int = 3) -> Ontology:
    return Ontology(name, scope_levels=["org"], min_n=min_n)


def _golden_ticket_ontology(property_name: str = "avg_response_hours") -> Ontology:
    ontology = _scoped_ontology("golden")

    @ontology.object(layer="L0", api_name="Comment", scope=[SelfScope(level="org")])
    class Comment(OntologyObject):
        id: str = prop(primary_key=True)
        body: str = prop()

    @ontology.object(layer="L0", api_name="Ticket", scope=[SelfScope(level="org")])
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        avg_response_hours: float = prop()

    ontology.link(
        "ticket_comments",
        Ticket,
        Comment,
        Cardinality.ONE_TO_MANY,
        description="Ticket comments.",
    )
    if property_name != "avg_response_hours":
        ticket = ontology.registry.object_types["Ticket"]
        ticket.properties[1] = ticket.properties[1].model_copy(
            update={"name": property_name}
        )
    return ontology


def test_stored_derivable_golden_finding_is_byte_exact() -> None:
    findings = _golden_ticket_ontology().diagnose()

    assert len(findings) == 1
    dumped = findings[0].model_dump(mode="json")
    assert dumped == {
        "code": "STORED_DERIVABLE",
        "severity": "warn",
        "location": "object Ticket, property avg_response_hours",
        "message": (
            "looks like a stored aggregate; facts are stored once and derived "
            "by Functions"
        ),
        "fix_hint": (
            "declare a Function that computes it from Comment rows, or mark the "
            "type as a declared snapshot"
        ),
    }
    assert json.dumps(dumped) == (
        '{"code": "STORED_DERIVABLE", "severity": "warn", '
        '"location": "object Ticket, property avg_response_hours", '
        '"message": "looks like a stored aggregate; facts are stored once '
        'and derived by Functions", "fix_hint": "declare a Function that '
        'computes it from Comment rows, or mark the type as a declared '
        'snapshot"}'
    )


def test_stored_derivable_negative_for_plain_property() -> None:
    assert _golden_ticket_ontology("response_hours").diagnose() == []


def _ontology_with_action_name(api_name: str) -> Ontology:
    ontology = _scoped_ontology("action-name")

    @ontology.object(layer="L0", api_name="Ticket", scope=[SelfScope(level="org")])
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        status: str = prop()

    class Params(ActionParams):
        ticket_id: str = target(Ticket)

    @ontology.action(
        Params,
        target=Ticket,
        roles=["Operator"],
        api_name=api_name,
        display_name="Update status",
    )
    def handler(_ctx: Any, _params: Params) -> dict[str, Any]:
        return {}

    return ontology


def test_crud_action_name_positive() -> None:
    findings = _ontology_with_action_name("UpdateStatus").diagnose()

    assert [finding.code for finding in findings] == ["CRUD_ACTION_NAME"]
    assert findings[0].severity == "warn"


def test_crud_action_name_negative_when_only_api_name_uses_business_verb() -> None:
    assert _ontology_with_action_name("ApproveTicket").diagnose() == []


def _snapshot_named_ontology(description: str) -> Ontology:
    ontology = _scoped_ontology("snapshot-name")

    @ontology.object(
        layer="L0",
        api_name="TicketSnapshot",
        description=description,
        scope=[SelfScope(level="org")],
    )
    class TicketSnapshot(OntologyObject):
        id: str = prop(primary_key=True)

    return ontology


def test_forbidden_type_name_positive_without_declared_snapshot_marker() -> None:
    findings = _snapshot_named_ontology("A copied ticket value.").diagnose()

    assert [finding.code for finding in findings] == ["FORBIDDEN_TYPE_NAME"]
    assert findings[0].severity == "warn"


def test_forbidden_snapshot_name_negative_with_declared_snapshot_marker() -> None:
    assert (
        _snapshot_named_ontology(
            "A declared snapshot of Ticket at an observation time."
        ).diagnose()
        == []
    )


def _ontology_with_micro_action(parameter_name: str) -> Ontology:
    ontology = _scoped_ontology("micro-action")

    @ontology.object(layer="L0", api_name="Ticket", scope=[SelfScope(level="org")])
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        status: str = prop()

    class Params(ActionParams):
        ticket_id: str = target(Ticket)
        status: str

    @ontology.action(
        Params,
        target=Ticket,
        roles=["Operator"],
        api_name="ApproveTicket",
    )
    def handler(_ctx: Any, _params: Params) -> dict[str, Any]:
        return {}

    if parameter_name != "status":
        action = ontology.registry.action_types["ApproveTicket"]
        action.parameters[1] = action.parameters[1].model_copy(update={"name": parameter_name})
    return ontology


def test_micro_action_positive_for_single_property_shape() -> None:
    findings = _ontology_with_micro_action("status").diagnose()

    assert [finding.code for finding in findings] == ["MICRO_ACTION"]
    assert findings[0].severity == "warn"


def test_micro_action_negative_when_parameter_does_not_name_a_property() -> None:
    assert _ontology_with_micro_action("new_status").diagnose() == []


def _sensitive_ontology(*, scoped: bool) -> Ontology:
    ontology = _scoped_ontology("sensitive", min_n=4)
    scope = [SelfScope(level="org")] if scoped else None

    @ontology.object(layer="L0", api_name="Person", scope=scope)
    class Person(OntologyObject):
        id: str = prop(primary_key=True)
        email: str | None = prop(
            default=None,
            sensitivity=Sensitivity(human_visible=False),
        )

    return ontology


def test_unscoped_sensitive_positive_without_policy_scope_rule() -> None:
    findings = _sensitive_ontology(scoped=False).diagnose()

    assert [finding.code for finding in findings] == ["UNSCOPED_SENSITIVE"]
    assert findings[0].severity == "warn"


def test_unscoped_sensitive_negative_with_scope_rule_only() -> None:
    assert _sensitive_ontology(scoped=True).diagnose() == []


def _explicitly_unscoped_sensitive_ontology() -> Ontology:
    ontology = _scoped_ontology("explicit-unscoped", min_n=4)

    @ontology.object(layer="L0", api_name="Person", scope="unscoped")
    class Person(OntologyObject):
        id: str = prop(primary_key=True)
        email: str | None = prop(
            default=None,
            sensitivity=Sensitivity(human_visible=False),
        )

    return ontology


def test_unscoped_sensitive_negative_when_type_is_explicitly_unscoped() -> None:
    assert _explicitly_unscoped_sensitive_ontology().diagnose() == []


def _min_n_ontology(min_n: int) -> Ontology:
    ontology = _scoped_ontology("min-n", min_n=min_n)

    @ontology.object(layer="L0", api_name="Person", scope=[SelfScope(level="org")])
    class Person(OntologyObject):
        id: str = prop(primary_key=True)
        email: str | None = prop(
            default=None,
            sensitivity=Sensitivity(human_visible=False),
        )

    return ontology


def test_min_n_unset_positive_for_default_with_sensitive_property() -> None:
    findings = _min_n_ontology(3).diagnose()

    assert [finding.code for finding in findings] == ["MIN_N_UNSET"]
    assert findings[0].severity == "info"


def test_min_n_unset_negative_when_min_n_is_explicitly_non_default() -> None:
    assert _min_n_ontology(4).diagnose() == []


def test_combined_ontology_reports_three_lints_in_one_sweep() -> None:
    ontology = _scoped_ontology("combined")

    @ontology.object(layer="L0", api_name="Ticket", scope=[SelfScope(level="org")])
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        avg_response_hours: float = prop()

    @ontology.object(layer="L0", api_name="TicketV2", scope=[SelfScope(level="org")])
    class TicketV2(OntologyObject):
        id: str = prop(primary_key=True)

    class UpdateParams(ActionParams):
        ticket_id: str = target(Ticket)

    @ontology.action(
        UpdateParams,
        target=Ticket,
        roles=["Operator"],
        api_name="UpdateStatus",
    )
    def handler(_ctx: Any, _params: UpdateParams) -> dict[str, Any]:
        return {}

    findings = ontology.diagnose()

    assert len(findings) == 3
    assert {finding.code for finding in findings} == {
        "STORED_DERIVABLE",
        "CRUD_ACTION_NAME",
        "FORBIDDEN_TYPE_NAME",
    }


@pytest.mark.parametrize(
    ("code", "rule_name"),
    [
        ("STORED_DERIVABLE", "_stored_derivable_findings"),
        ("CRUD_ACTION_NAME", "_crud_action_name_findings"),
        ("FORBIDDEN_TYPE_NAME", "_forbidden_type_name_findings"),
        ("MICRO_ACTION", "_micro_action_findings"),
        ("UNSCOPED_SENSITIVE", "_unscoped_sensitive_findings"),
        ("MIN_N_UNSET", "_min_n_unset_findings"),
    ],
)
def test_each_lint_is_pinned_in_rules_tuple(code: str, rule_name: str) -> None:
    """Deleting any one rule from RULES must make its lint pin fail."""
    assert code
    assert any(rule.__name__ == rule_name for rule in diagnose_module.RULES)
