"""T9 design-guide lint coverage and mutation pins."""

from __future__ import annotations

import json
import warnings
from enum import StrEnum
from typing import Any, Literal

import pytest

import ontary.cli as cli
import ontary.diagnose as diagnose_module
from ontary import (
    ActionParams,
    Cardinality,
    Event,
    ObjectStore,
    Ontology,
    OntologyObject,
    SelfScope,
    Sensitivity,
    prop,
    target,
)
from ontary.meta import ActionLint, EventLint, FunctionLint, ObjectLint, PropertyLint


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
        ticket.properties[1] = ticket.properties[1].model_copy(update={"name": property_name})
    return ontology


def test_stored_derivable_golden_finding_is_byte_exact() -> None:
    findings = _golden_ticket_ontology().diagnose()

    assert len(findings) == 1
    dumped = findings[0].model_dump(mode="json")
    assert dumped == {
        "code": "STORED_DERIVABLE",
        "severity": "warn",
        "location": "object Ticket, property avg_response_hours",
        "message": "the name reads as a score or aggregate",
        "fix_hint": (
            "if Ticket.avg_response_hours is computed from other rows, derive it "
            "with a Function; if it is recorded from outside, "
            'add accept="STORED_DERIVABLE" to the property'
        ),
        "guide": (
            "https://ryoochi0112.github.io/ontary/ontology-design/#normalization-and-derived-values"
        ),
    }


def test_stored_derivable_negative_for_plain_property() -> None:
    assert _golden_ticket_ontology("response_hours").diagnose() == []


def _ontology_with_action_name(api_name: str, *, accept: ActionLint | None = None) -> Ontology:
    ontology = _scoped_ontology("action-name")

    @ontology.object(layer="L0", api_name="Ticket", scope=[SelfScope(level="org")])
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        status: Literal["open", "closed"] = prop()

    class Params(ActionParams):
        ticket_id: str = target(Ticket)

    @ontology.action(
        Params,
        target=Ticket,
        roles=["Operator"],
        api_name=api_name,
        display_name="Update status",
        description="Update a ticket's status.",
        accept=accept,
    )
    def handler(_ctx: Any, _params: Params) -> dict[str, Any]:
        return {}

    return ontology


@pytest.mark.parametrize(
    ("api_name", "anchor"),
    [
        ("SetStatus", "action-sprawl"),
        ("UpdateStatus", "action-sprawl"),
        ("CreateTicket", "action-sprawl"),
        ("DeleteTicket", "retirement-and-removal"),
        ("RemoveTicket", "retirement-and-removal"),
        ("EraseTicket", "retirement-and-removal"),
    ],
)
def test_crud_action_name_positive(api_name: str, anchor: str) -> None:
    findings = _ontology_with_action_name(api_name).diagnose()

    assert [finding.code for finding in findings] == ["CRUD_ACTION_NAME"]
    assert findings[0].severity == "warn"
    assert findings[0].guide == diagnose_module.GUIDE_URL + "#" + anchor


def test_crud_action_name_negative_when_only_api_name_uses_business_verb() -> None:
    assert _ontology_with_action_name("ApproveTicket").diagnose() == []


def _snapshot_named_ontology(
    description: str = "A copied ticket value.",
    *,
    api_name: str = "TicketSnapshot",
    snapshot: bool = False,
    accept: ObjectLint | None = None,
) -> Ontology:
    ontology = _scoped_ontology("snapshot-name")

    @ontology.object(
        layer="L0",
        api_name=api_name,
        snapshot=snapshot,
        accept=accept,
        description=description,
        scope=[SelfScope(level="org")],
    )
    class TicketSnapshot(OntologyObject):
        id: str = prop(primary_key=True)

    return ontology


def test_forbidden_type_name_positive_without_declared_snapshot_marker() -> None:
    findings = _snapshot_named_ontology("A copied ticket value.").diagnose()

    assert [finding.code for finding in findings] == ["FORBIDDEN_TYPE_NAME"]
    assert findings[0].guide == diagnose_module.GUIDE_URL + "#the-time-machine"
    assert findings[0].severity == "warn"


def test_description_snapshot_marker_has_no_effect() -> None:
    assert (
        _snapshot_named_ontology("A declared snapshot of Ticket at an observation time.")
        .diagnose()[0]
        .code
        == "FORBIDDEN_TYPE_NAME"
    )


def _ontology_with_micro_action(
    parameter_name: str,
    *,
    api_name: str = "ApproveTicket",
    accept: ActionLint | None = None,
) -> Ontology:
    ontology = _scoped_ontology("micro-action")

    @ontology.object(layer="L0", api_name="Ticket", scope=[SelfScope(level="org")])
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        status: Literal["open", "closed"] = prop()

    class Params(ActionParams):
        ticket_id: str = target(Ticket)
        status: str

    @ontology.action(
        Params,
        target=Ticket,
        roles=["Operator"],
        api_name=api_name,
        description="Set a ticket's status.",
        accept=accept,
    )
    def handler(_ctx: Any, _params: Params) -> dict[str, Any]:
        return {}

    if parameter_name != "status":
        action = ontology.registry.action_types[api_name]
        action.parameters[1] = action.parameters[1].model_copy(update={"name": parameter_name})
    return ontology


def test_micro_action_positive_for_single_property_shape() -> None:
    findings = _ontology_with_micro_action("status").diagnose()

    assert [finding.code for finding in findings] == ["MICRO_ACTION"]
    assert findings[0].guide == diagnose_module.GUIDE_URL + "#action-sprawl"
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
    assert findings[0].guide == diagnose_module.GUIDE_URL + "#security-design"
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
    assert findings[0].guide == diagnose_module.GUIDE_URL + "#security-design"
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
        description="Update a ticket's status.",
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
        ("FREE_TEXT_STATUS", "_free_text_status_findings"),
        ("AUDIT_TYPE", "_audit_type_findings"),
        ("EVENT_NEVER_EMITTED", "_event_never_emitted_findings"),
    ],
)
def test_each_lint_is_pinned_in_rules_tuple(code: str, rule_name: str) -> None:
    """Deleting any one rule from RULES must make its lint pin fail."""
    assert code
    assert any(rule.__name__ == rule_name for rule in diagnose_module.RULES)


def _ontology_with_function_name(api_name: str, *, accept: FunctionLint | None = None) -> Ontology:
    ontology = _scoped_ontology("function-name")

    @ontology.function(api_name=api_name, description="Return a number.", accept=accept)
    def handler(_query: Any) -> int:
        return 0

    return ontology


@pytest.mark.parametrize(
    "api_name",
    [
        "DeleteTicket",
        "set_status",
        "deleteTicket",
        "_erase_user",
        "UPDATE_TICKET",
        "__CreateTicket",
        "RemoveTicket",
    ],
)
@pytest.mark.parametrize("kind", ["action", "function"])
def test_crud_first_word_positive(api_name: str, kind: str) -> None:
    ontology = (
        _ontology_with_action_name(api_name)
        if kind == "action"
        else _ontology_with_function_name(api_name)
    )
    findings = ontology.diagnose()
    assert [f.code for f in findings] == ["CRUD_ACTION_NAME"]
    assert findings[0].location == f"{kind} {api_name}"
    assert findings[0].guide is not None


@pytest.mark.parametrize(
    "api_name",
    [
        "SettleInvoice",
        "SetupAccount",
        "createdAtLookup",
        "removalReason",
        "ApproveTicket",
    ],
)
@pytest.mark.parametrize("kind", ["action", "function"])
def test_crud_first_word_negative(api_name: str, kind: str) -> None:
    ontology = (
        _ontology_with_action_name(api_name)
        if kind == "action"
        else _ontology_with_function_name(api_name)
    )
    assert ontology.diagnose() == []


@pytest.mark.parametrize("kind", ["action", "function"])
def test_crud_accept(kind: str) -> None:
    ontology = (
        _ontology_with_action_name("DeleteTicket", accept="CRUD_ACTION_NAME")
        if kind == "action"
        else _ontology_with_function_name("DeleteTicket", accept="CRUD_ACTION_NAME")
    )
    assert ontology.diagnose() == []


def test_crud_golden_finding_is_byte_exact() -> None:
    assert _ontology_with_action_name("DeleteTicket").diagnose()[0].model_dump() == {
        "code": "CRUD_ACTION_NAME",
        "severity": "warn",
        "location": "action DeleteTicket",
        "message": '"Delete" names a storage operation, not a business outcome',
        "fix_hint": (
            "name the outcome, for example WithdrawTicket, and call ctx.retire() "
            "inside the handler; if the storage operation is intentional, "
            'add accept="CRUD_ACTION_NAME"'
        ),
        "guide": diagnose_module.GUIDE_URL + "#retirement-and-removal",
    }


@pytest.mark.parametrize(
    "api_name,example",
    [
        ("set_status", "ChangeStatus"),
        ("UPDATE_TICKET", "ChangeTicket"),
        ("CreateTicket", "RegisterTicket"),
        ("_erase_user", "WithdrawUser"),
    ],
)
def test_crud_hint_uses_rest_of_name(api_name: str, example: str) -> None:
    finding = _ontology_with_action_name(api_name).diagnose()[0]
    assert example in finding.fix_hint
    assert 'accept="CRUD_ACTION_NAME"' in finding.fix_hint


def test_crud_guide_selection_does_not_depend_on_anchor_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        diagnose_module.GUIDE_ANCHORS,
        "CRUD_ACTION_NAME",
        ("retirement-and-removal", "action-sprawl"),
    )
    for api_name, anchor in [
        ("SetStatus", "action-sprawl"),
        ("DeleteTicket", "retirement-and-removal"),
    ]:
        assert _ontology_with_action_name(api_name).diagnose()[0].guide == (
            diagnose_module.GUIDE_URL + "#" + anchor
        )


@pytest.mark.parametrize(
    "api_name,suffix,kind,base",
    [
        ("SurveyResponseV2", "V2", "version", "SurveyResponse"),
        ("SurveyV10", "V10", "version", "Survey"),
        ("Survey2024", "2024", "year", "Survey"),
        ("Survey1999", "1999", "year", "Survey"),
        ("TicketHistory", "History", "history", "Ticket"),
        ("TicketSnapshot", "Snapshot", "snapshot", "Ticket"),
    ],
)
def test_forbidden_suffix_kinds(api_name: str, suffix: str, kind: str, base: str) -> None:
    findings = _snapshot_named_ontology(api_name=api_name).diagnose()
    assert [f.code for f in findings] == ["FORBIDDEN_TYPE_NAME"]
    assert findings[0].message == (f'the name ends in "{suffix}", which reads as a {kind} clone')
    assert f"keep one type, {base};" in findings[0].fix_hint
    assert 'accept="FORBIDDEN_TYPE_NAME"' in findings[0].fix_hint


@pytest.mark.parametrize(
    "api_name",
    [
        "History",
        "Snapshot",
        "V2",
        "2024",
        "Form1099",
        "Route66",
        "Catch22",
        "Ticket",
    ],
)
def test_forbidden_suffix_negative(api_name: str) -> None:
    assert _snapshot_named_ontology(api_name=api_name).diagnose() == []


def test_forbidden_golden_finding_is_byte_exact() -> None:
    assert _snapshot_named_ontology(api_name="SurveyResponseV2").diagnose()[0].model_dump() == {
        "code": "FORBIDDEN_TYPE_NAME",
        "severity": "warn",
        "location": "object SurveyResponseV2",
        "message": 'the name ends in "V2", which reads as a version clone',
        "fix_hint": (
            "keep one type, SurveyResponse; the store already keeps row history. "
            "If a point-in-time value must be first-class, declare the type with "
            'snapshot=True; if the name is intentional, add accept="FORBIDDEN_TYPE_NAME"'
        ),
        "guide": diagnose_module.GUIDE_URL + "#the-time-machine",
    }


def test_snapshot_flag_and_forbidden_accept() -> None:
    assert _snapshot_named_ontology(snapshot=True).diagnose() == []
    assert _snapshot_named_ontology(accept="FORBIDDEN_TYPE_NAME").diagnose() == []
    assert "snapshot=True" in _snapshot_named_ontology().diagnose()[0].fix_hint


@pytest.mark.parametrize("api_name", ["TicketV2", "Ticket2024", "TicketHistory"])
def test_snapshot_flag_does_not_excuse_other_suffixes(api_name: str) -> None:
    assert [
        f.code for f in _snapshot_named_ontology(api_name=api_name, snapshot=True).diagnose()
    ] == ["FORBIDDEN_TYPE_NAME"]


def _stored_values_ontology(
    *,
    snapshot: bool = False,
    description: str = "",
    accept: PropertyLint | None = None,
) -> Ontology:
    ontology = _scoped_ontology("stored-values")

    @ontology.object(layer="L0", scope="unscoped", snapshot=snapshot, description=description)
    class Observation(OntologyObject):
        id: str = prop(primary_key=True)
        credit_score: int = prop(accept=accept)
        exchange_rate: float = prop()
        retry_count: int = prop()

    return ontology


def test_stored_accept_is_property_local() -> None:
    assert [f.location for f in _stored_values_ontology(accept="STORED_DERIVABLE").diagnose()] == [
        "object Observation, property exchange_rate",
        "object Observation, property retry_count",
    ]


def test_snapshot_silences_all_stored_derivable_properties() -> None:
    assert _stored_values_ontology(snapshot=True).diagnose() == []


def test_description_marker_does_not_silence_stored_derivable() -> None:
    assert len(_stored_values_ontology(description="declared snapshot").diagnose()) == 3


def test_stored_hint_without_links_names_both_exits() -> None:
    for finding in _stored_values_ontology().diagnose():
        assert "derive it with a Function" in finding.fix_hint
        assert "recorded from outside" in finding.fix_hint
        assert 'accept="STORED_DERIVABLE"' in finding.fix_hint
        assert "snapshot" not in finding.fix_hint
        assert finding.location.split("property ")[1] in finding.fix_hint


def test_micro_golden_finding_is_byte_exact() -> None:
    assert _ontology_with_micro_action("status").diagnose()[0].model_dump() == {
        "code": "MICRO_ACTION",
        "severity": "warn",
        "location": "action ApproveTicket, parameter status",
        "message": "action shape looks like a one-property write to Ticket.status",
        "fix_hint": (
            "make ApproveTicket express a business outcome for Ticket and change "
            "status alongside the other facts that outcome requires; if this "
            'one-property action is intentional, add accept="MICRO_ACTION"'
        ),
        "guide": diagnose_module.GUIDE_URL + "#action-sprawl",
    }


@pytest.mark.parametrize(
    "accept,remaining",
    [
        ("MICRO_ACTION", "CRUD_ACTION_NAME"),
        ("CRUD_ACTION_NAME", "MICRO_ACTION"),
    ],
)
def test_action_accepts_are_independent(accept: ActionLint, remaining: str) -> None:
    assert [
        f.code
        for f in _ontology_with_micro_action(
            "status", api_name="SetStatus", accept=accept
        ).diagnose()
    ] == [remaining]


def test_micro_accept_removes_finding() -> None:
    assert _ontology_with_micro_action("status", accept="MICRO_ACTION").diagnose() == []


class _Status(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


def _status_ontology(
    name: str = "status", *, optional: bool = False,
    accept: PropertyLint | None = None,
) -> Ontology:
    ontology = _scoped_ontology("status")

    @ontology.object(layer="L0", scope="unscoped")
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        status: str = prop(accept=accept)

    obj = ontology.registry.object_types["Ticket"]
    obj.properties[1] = obj.properties[1].model_copy(
        update={"name": name, "required": not optional}
    )
    return ontology


@pytest.mark.parametrize("name", ["status", "ticket_status"])
@pytest.mark.parametrize("optional", [False, True])
def test_free_text_status_positive(name: str, optional: bool) -> None:
    findings = _status_ontology(name, optional=optional).diagnose()
    assert [f.code for f in findings] == ["FREE_TEXT_STATUS"]
    assert findings[0].location == f"object Ticket, property {name}"


@pytest.mark.parametrize("name", ["us_state", "statuses", "status_note"])
def test_free_text_status_name_negative(name: str) -> None:
    assert _status_ontology(name).diagnose() == []


def test_free_text_status_non_string_negative() -> None:
    ontology = _status_ontology()
    ontology.registry.object_types["Ticket"].properties[1].type = "int"
    assert ontology.diagnose() == []


def test_free_text_status_choices_negative() -> None:
    ontology = _scoped_ontology("choices")

    @ontology.object(layer="L0", scope="unscoped")
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        status: _Status
        optional_status: _Status | None = None
        literal_status: Literal["open", "closed"]
        optional_literal_status: Literal["open", "closed"] | None = None
        explicit_status: str = prop(choices=["open", "closed"])

    assert ontology.diagnose() == []


def test_free_text_status_accept_is_property_local() -> None:
    ontology = _status_ontology(accept="FREE_TEXT_STATUS")
    assert ontology.diagnose() == []
    obj = ontology.registry.object_types["Ticket"]
    obj.properties.append(obj.properties[1].model_copy(
        update={"name": "other_status", "accept": ()}
    ))
    assert [f.location for f in ontology.diagnose()] == [
        "object Ticket, property other_status"
    ]


def test_free_text_status_golden_finding_is_byte_exact() -> None:
    assert _status_ontology().diagnose()[0].model_dump() == {
        "code": "FREE_TEXT_STATUS",
        "severity": "warn",
        "location": "object Ticket, property status",
        "message": "a status stored as free text accepts any value",
        "fix_hint": (
            "annotate Ticket.status with a StrEnum or Literal so every write path "
            "enforces the allowed values; if free text is intentional, "
            'add accept="FREE_TEXT_STATUS" to the property'
        ),
        "guide": diagnose_module.GUIDE_URL + "#choice-properties",
    }


def _audit_ontology(api_name: str, *, accept: ObjectLint | None = None) -> Ontology:
    ontology = _scoped_ontology("audit")

    @ontology.object(layer="L0", scope="unscoped", api_name=api_name, accept=accept)
    class Record(OntologyObject):
        id: str = prop(primary_key=True)

    return ontology


@pytest.mark.parametrize("suffix", ["AuditLog", "AuditEntry", "AuditTrail", "AuditRecord", "AuditEvent"])
@pytest.mark.parametrize("prefix", ["", "Ticket"])
def test_audit_type_positive(prefix: str, suffix: str) -> None:
    findings = _audit_ontology(prefix + suffix).diagnose()
    assert [f.code for f in findings] == ["AUDIT_TYPE"]
    assert findings[0].location == f"object {prefix}{suffix}"


@pytest.mark.parametrize("api_name", ["Audit", "AuditPlan", "Auditor", "AuditLogPlan", "TicketAuditLogPlan"])
def test_audit_type_negative(api_name: str) -> None:
    assert _audit_ontology(api_name).diagnose() == []


def test_audit_type_accept_is_object_local() -> None:
    ontology = _audit_ontology("TicketAuditLog", accept="AUDIT_TYPE")
    assert ontology.diagnose() == []
    @ontology.object(layer="L0", scope="unscoped")
    class AuditEntry(OntologyObject):
        id: str = prop(primary_key=True)

    assert [f.location for f in ontology.diagnose()] == ["object AuditEntry"]


def test_audit_type_golden_finding_is_byte_exact() -> None:
    assert _audit_ontology("TicketAuditLog").diagnose()[0].model_dump() == {
        "code": "AUDIT_TYPE",
        "severity": "warn",
        "location": "object TicketAuditLog",
        "message": "the type looks like a duplicate of the engine's action audit",
        "fix_hint": (
            "remove TicketAuditLog because the engine already records every action "
            'as AuditEntry; if it represents a domain fact, add accept="AUDIT_TYPE"'
        ),
        "guide": diagnose_module.GUIDE_URL + "#the-golden-hammer",
    }


def test_warn_only_validate_and_bind_emit_no_python_warnings() -> None:
    ontology = _status_ontology()
    assert [f.severity for f in ontology.diagnose()] == ["warn"]
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        ontology.validate()
        ontology.bind(ObjectStore(ontology.registry))
    assert captured == []


def _event_ontology(
    *, api_name: str = "TicketApproved", emits: bool = False,
    accept: EventLint | tuple[()] = (),
) -> Ontology:
    ontology = _scoped_ontology("events")

    @ontology.event(api_name=api_name, accept=accept)
    class Approved(Event):
        reason: str

    @ontology.object(layer="L0", scope="unscoped")
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)

    class ApproveTicket(ActionParams):
        ticket_id: str = target(Ticket)

    @ontology.action(
        ApproveTicket, target=Ticket, roles=["Operator"],
        emits=[Approved] if emits else [],
        description="Approve a ticket.",
    )
    def handler(_ctx: Any, _params: ApproveTicket) -> dict[str, Any]:
        return {}

    return ontology


@pytest.mark.parametrize("api_name", ["TicketApproved", "OrderShipped"])
def test_event_never_emitted_golden_finding_is_byte_exact(api_name: str) -> None:
    findings = _event_ontology(api_name=api_name).diagnose()
    assert len(findings) == 1
    assert findings[0].model_dump() == {
        "code": "EVENT_NEVER_EMITTED",
        "severity": "warn",
        "location": f"event {api_name}",
        "message": f"event {api_name} is declared but appears in no action's emits",
        "fix_hint": (
            f"add {api_name} to an action's emits=[{api_name}] or remove it; "
            'if the unused event is intentional, add accept="EVENT_NEVER_EMITTED"'
        ),
        "guide": diagnose_module.GUIDE_URL + "#events",
    }


def test_event_emits_declaration_silences_finding_without_execution() -> None:
    ontology = _event_ontology(emits=True)
    assert ontology.diagnose() == []

    @ontology.event()
    class TicketRejected(Event):
        reason: str

    assert [f.location for f in ontology.diagnose()] == ["event TicketRejected"]


def test_event_accept_silences_only_that_event_without_emits() -> None:
    ontology = _event_ontology(accept="EVENT_NEVER_EMITTED")
    assert ontology.diagnose() == []

    @ontology.event()
    class TicketRejected(Event):
        reason: str

    assert [f.location for f in ontology.diagnose()] == ["event TicketRejected"]


def test_event_validate_strict_warning_exits_one_and_json_is_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    target_name = f"{__name__}:_event_ontology"
    normal_code = cli.main(["validate", target_name, "--json"])
    normal_output = capsys.readouterr().out
    strict_code = cli.main(["validate", target_name, "--strict", "--json"])
    strict_output = capsys.readouterr().out

    assert normal_code == 0
    assert strict_code == 1
    assert json.loads(strict_output) == json.loads(normal_output)
    assert [item["code"] for item in json.loads(strict_output)] == ["EVENT_NEVER_EMITTED"]
    assert [item["severity"] for item in json.loads(strict_output)] == ["warn"]
