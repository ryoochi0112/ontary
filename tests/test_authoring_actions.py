"""Unit tests for typed action authoring (`ontary.authoring`'s
`ActionParams`/`target()`/`scope_ref()`/`Ontology.action()`) -- spec
`typed-actions.md` T2.

Builds a tiny "tickets"-shaped toy ontology via the class-based authoring
API and checks the derived `ActionTypeDef`/`ActionParameterDef`s against
the actually-registered `EscalateTicket` descriptor in
`examples/tickets/ontology.py` (spec AC1), marker/declaration-time
validation errors (AC2), and that typed `client.execute(params_instance)`
and string `client.execute(name, dict)` reach the identical handler with
identical audited params (AC5).
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import raises_code

from examples.tickets.ontology import build_ontology
from ontary.actions import ActionContext, ActionError, ActionExecutor
from ontary.authoring import ActionParams, Ontology, OntologyObject, prop, ref, scope_ref, target
from ontary.client import OntologyClient
from ontary.errors import ValidationFailed
from ontary.ontology import OntologyDef
from ontary.scope import ScopePolicy
from ontary.security import Consumer
from ontary.store import ObjectStore, Source

LEVELS = ["queue", "org"]
SRC = Source(source_system="test")


def _build_tickets_ontology() -> tuple[Ontology, type[OntologyObject], type[OntologyObject]]:
    """A tiny ticket/team ontology (unscoped, for pipeline-focus tests --
    scope enforcement itself is covered by `test_actions.py`)."""
    ontology = Ontology(name="tickets", scope_levels=LEVELS)

    @ontology.object(layer="L0", scope="unscoped")
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        subject: str

    @ontology.object(layer="L0", scope="unscoped")
    class Team(OntologyObject):
        id: str = prop(primary_key=True)
        name: str

    return ontology, Ticket, Team


def _agent(role: str = "Agent") -> Consumer:
    return Consumer(actor_id="a1", role=role, scope_level="org", scope_id="org-1", kind="human")


class TestDerivedMatchesHandWritten:
    def test_escalate_ticket_shape(self) -> None:
        """Spec AC1: the class-authored `EscalateTicket` below must derive
        the SAME `ActionTypeDef` as the real, actually-registered
        `examples/tickets/ontology.py::EscalateTicket` descriptor -- binding
        to the live example (not a literal copy in this test file) means
        drift in the example would fail this test."""
        ontology, Ticket, _Team = _build_tickets_ontology()

        class EscalateTicketParams(ActionParams):
            ticket_id: str = target(Ticket)
            reason: str | None = None

        @ontology.action(
            EscalateTicketParams,
            target=Ticket,
            roles=["Agent", "Manager"],
            display_name="Escalate Ticket",
            description="Escalates a Ticket as urgent.",
            api_name="EscalateTicket",
        )
        def escalate(ctx: ActionContext, params: EscalateTicketParams) -> dict[str, str]:
            return {"ticket_id": params.ticket_id}

        derived = ontology.registry.get_action_type("EscalateTicket")
        example_ontology_def, _example_store = build_ontology()
        example_action = example_ontology_def.registry.get_action_type("EscalateTicket")
        assert derived == example_action

    def test_api_name_defaults_to_params_cls_name(self) -> None:
        ontology, Ticket, _Team = _build_tickets_ontology()

        class SomeParams(ActionParams):
            ticket_id: str = target(Ticket)

        @ontology.action(SomeParams, target=Ticket, roles=["Agent"])
        def handler(ctx: ActionContext, params: SomeParams) -> dict[str, str]:
            return {}

        derived = ontology.registry.get_action_type("SomeParams")
        assert derived.display_name == "SomeParams"


class TestRequiredRule:
    def test_non_optional_required_true_optional_required_false(self) -> None:
        ontology, Ticket, _Team = _build_tickets_ontology()

        class Params(ActionParams):
            ticket_id: str = target(Ticket)
            reason: str | None = None

        @ontology.action(Params, target=Ticket, roles=["Agent"], api_name="Act")
        def handler(ctx: ActionContext, params: Params) -> dict[str, str]:
            return {}

        derived = ontology.registry.get_action_type("Act")
        by_name = {p.name: p for p in derived.parameters}
        assert by_name["ticket_id"].required is True
        assert by_name["reason"].required is False


class TestMarkerValidation:
    def test_marker_on_non_str_field_rejected_at_decoration(self) -> None:
        ontology, Ticket, _Team = _build_tickets_ontology()

        class BadParams(ActionParams):
            ticket_id: int = target(Ticket)

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.action(BadParams, target=Ticket, roles=["Agent"], api_name="Bad")
            def handler(ctx: ActionContext, params: BadParams) -> dict[str, str]:
                return {}
        assert "str" in str(exc_info.value)

    def test_undecorated_class_as_marker_rejected(self) -> None:
        ontology, Ticket, _Team = _build_tickets_ontology()

        class NotRegistered(OntologyObject):
            id: str = prop(primary_key=True)

        class BadParams(ActionParams):
            ticket_id: str = target(NotRegistered)

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.action(BadParams, target=Ticket, roles=["Agent"], api_name="Bad")
            def handler(ctx: ActionContext, params: BadParams) -> dict[str, str]:
                return {}
        assert "not decorated" in str(exc_info.value)

    def test_undecorated_class_as_target_rejected(self) -> None:
        ontology, Ticket, _Team = _build_tickets_ontology()

        class NotRegistered(OntologyObject):
            id: str = prop(primary_key=True)

        class Params(ActionParams):
            ticket_id: str = target(Ticket)

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.action(Params, target=NotRegistered, roles=["Agent"], api_name="Bad")
            def handler(ctx: ActionContext, params: Params) -> dict[str, str]:
                return {}
        assert "not decorated" in str(exc_info.value)

    def test_marker_class_from_another_ontology_rejected(self) -> None:
        ontology, Ticket, _Team = _build_tickets_ontology()
        other = Ontology(name="other", scope_levels=LEVELS)

        @other.object(layer="L0", scope="unscoped")
        class OtherTicket(OntologyObject):
            id: str = prop(primary_key=True)

        class Params(ActionParams):
            ticket_id: str = target(OtherTicket)

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.action(Params, target=Ticket, roles=["Agent"], api_name="Bad")
            def handler(ctx: ActionContext, params: Params) -> dict[str, str]:
                return {}
        assert "different Ontology" in str(exc_info.value)

    def test_target_class_from_another_ontology_rejected(self) -> None:
        ontology, Ticket, _Team = _build_tickets_ontology()
        other = Ontology(name="other", scope_levels=LEVELS)

        @other.object(layer="L0", scope="unscoped")
        class OtherTicket(OntologyObject):
            id: str = prop(primary_key=True)

        class Params(ActionParams):
            ticket_id: str = target(Ticket)

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.action(Params, target=OtherTicket, roles=["Agent"], api_name="Bad")
            def handler(ctx: ActionContext, params: Params) -> dict[str, str]:
                return {}
        assert "different Ontology" in str(exc_info.value)

    def test_scope_ref_marker_derives_scope_semantics(self) -> None:
        ontology, Ticket, Team = _build_tickets_ontology()

        class Params(ActionParams):
            ticket_id: str = target(Ticket)
            team_id: str = scope_ref(Team)

        @ontology.action(Params, target=Ticket, roles=["Agent"], api_name="Act")
        def handler(ctx: ActionContext, params: Params) -> dict[str, str]:
            return {}

        derived = ontology.registry.get_action_type("Act")
        by_name = {p.name: p for p in derived.parameters}
        assert by_name["team_id"].refers_to == "Team"
        assert by_name["team_id"].scope_semantics == "scope"

    def test_ref_marker_derives_refers_to_only_no_scope_semantics(self) -> None:
        """`ref()` (T7 P1 fix): a plain reference marker -- `refers_to` set,
        `scope_semantics` stays `None` (unlike `target()`/`scope_ref()`,
        which both always set it)."""
        ontology, Ticket, Team = _build_tickets_ontology()

        class Params(ActionParams):
            ticket_id: str = target(Ticket)
            owning_team_id: str = ref(Team)

        @ontology.action(Params, target=Ticket, roles=["Agent"], api_name="Act")
        def handler(ctx: ActionContext, params: Params) -> dict[str, str]:
            return {}

        derived = ontology.registry.get_action_type("Act")
        by_name = {p.name: p for p in derived.parameters}
        assert by_name["owning_team_id"].refers_to == "Team"
        assert by_name["owning_team_id"].scope_semantics is None


class TestDuplicateAndFreeze:
    def test_duplicate_action_api_name_rejected(self) -> None:
        ontology, Ticket, _Team = _build_tickets_ontology()

        class ParamsA(ActionParams):
            ticket_id: str = target(Ticket)

        class ParamsB(ActionParams):
            ticket_id: str = target(Ticket)

        @ontology.action(ParamsA, target=Ticket, roles=["Agent"], api_name="Dup")
        def handler_a(ctx: ActionContext, params: ParamsA) -> dict[str, str]:
            return {}

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.action(ParamsB, target=Ticket, roles=["Agent"], api_name="Dup")
            def handler_b(ctx: ActionContext, params: ParamsB) -> dict[str, str]:
                return {}
        assert "Dup" in str(exc_info.value)

    def test_same_params_cls_declared_twice_rejected(self) -> None:
        ontology, Ticket, _Team = _build_tickets_ontology()

        class Params(ActionParams):
            ticket_id: str = target(Ticket)

        @ontology.action(Params, target=Ticket, roles=["Agent"], api_name="First")
        def handler_a(ctx: ActionContext, params: Params) -> dict[str, str]:
            return {}

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.action(Params, target=Ticket, roles=["Agent"], api_name="Second")
            def handler_b(ctx: ActionContext, params: Params) -> dict[str, str]:
                return {}
        assert "Params" in str(exc_info.value)

    def test_action_after_freeze_rejected(self) -> None:
        ontology, Ticket, _Team = _build_tickets_ontology()
        ontology.definition  # freeze

        class Params(ActionParams):
            ticket_id: str = target(Ticket)

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.action(Params, target=Ticket, roles=["Agent"], api_name="Late")
            def handler(ctx: ActionContext, params: Params) -> dict[str, str]:
                return {}
        assert "frozen" in str(exc_info.value)


# -- typed + string execute parity -------------------------------------------


def _build_client_and_executor(
    ontology: Ontology,
) -> tuple[OntologyClient, ActionExecutor]:
    store = ObjectStore(ontology.registry)
    ontology_def = OntologyDef(
        name=ontology.name,
        registry=ontology.registry,
        policy=ScopePolicy(levels=LEVELS, unscoped_types={"Ticket", "Team"}),
        functions=ontology.definition.functions,
    )
    client = OntologyClient(ontology_def, store, _agent())
    return client, client.actions


class TestTypedExecuteParity:
    def _setup(self) -> tuple[OntologyClient, type[ActionParams], list[Any]]:
        ontology, Ticket, _Team = _build_tickets_ontology()
        store_seen: list[Any] = []

        class EscalateParams(ActionParams):
            ticket_id: str = target(Ticket)
            reason: str | None = None

        @ontology.action(
            EscalateParams, target=Ticket, roles=["Agent"], api_name="Escalate"
        )
        def escalate(ctx: ActionContext, params: EscalateParams) -> dict[str, str]:
            store_seen.append(params)
            return {"ticket_id": params.ticket_id}

        client, executor = _build_client_and_executor(ontology)
        executor._register("Escalate", escalate, EscalateParams)
        return client, EscalateParams, store_seen

    def test_typed_and_string_execute_hit_same_handler(self) -> None:
        client, EscalateParams, seen = self._setup()

        result_dict = client.execute("Escalate", {"ticket_id": "t1", "reason": "urgent"})
        result_typed = client.execute(EscalateParams(ticket_id="t1", reason="urgent"))

        assert result_dict == {"ticket_id": "t1"}
        assert result_typed == {"ticket_id": "t1"}
        assert len(seen) == 2
        assert all(isinstance(p, EscalateParams) for p in seen)

        entries = client._store.audit_entries()
        assert len(entries) == 2
        assert entries[0].params == entries[1].params

    def test_typed_form_with_extra_params_argument_is_invalid_params(self) -> None:
        client, EscalateParams, _seen = self._setup()
        untyped_execute: Any = client.execute

        with pytest.raises(ValidationFailed) as exc_info:
            untyped_execute(EscalateParams(ticket_id="t1"), {"reason": "urgent"})
        assert exc_info.value.code == "INVALID_PARAMS"

    def test_foreign_params_instance_is_unknown_action(self) -> None:
        client, EscalateParams, _seen = self._setup()

        other_ontology, OtherTicket, _OtherTeam = _build_tickets_ontology()

        class ForeignParams(ActionParams):
            ticket_id: str = target(OtherTicket)

        @other_ontology.action(
            ForeignParams, target=OtherTicket, roles=["Agent"], api_name="Foreign"
        )
        def handler(ctx: ActionContext, params: ForeignParams) -> dict[str, str]:
            return {}

        with pytest.raises(ActionError) as exc_info:
            client.execute(ForeignParams(ticket_id="t1"))
        assert exc_info.value.code == "UNKNOWN_ACTION"

    def test_undecorated_params_instance_is_unknown_action(self) -> None:
        client, _EscalateParams, _seen = self._setup()

        class PlainParams(ActionParams):
            ticket_id: str

        with pytest.raises(ActionError) as exc_info:
            client.execute(PlainParams(ticket_id="t1"))
        assert exc_info.value.code == "UNKNOWN_ACTION"

    def test_undecorated_subclass_of_declared_params_is_unknown_action(self) -> None:
        """Fail-closed pin: an undecorated `Sub(EscalateParams)` subclass
        must NOT ride on its registered base class's action lookup --
        `client.execute` dispatches by the params instance's OWN class, not
        by any base class, so `Sub` -- never itself passed to
        `ontology.action(...)` -- is an unknown action."""
        client, EscalateParams, _seen = self._setup()

        class Sub(EscalateParams):
            pass

        with pytest.raises(ActionError) as exc_info:
            client.execute(Sub(ticket_id="t1", reason="urgent"))
        assert exc_info.value.code == "UNKNOWN_ACTION"


def test_front_door_exports() -> None:
    from ontary import ActionParams as FrontDoorActionParams
    from ontary import target as front_door_target
    from ontary.authoring import scope_ref as front_door_scope_ref

    assert FrontDoorActionParams is ActionParams
    assert front_door_target is target
    assert front_door_scope_ref is scope_ref
