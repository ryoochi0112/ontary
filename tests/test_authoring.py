from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import pytest
from conftest import raises_code
from pydantic import field_validator

from ontary.authoring import LinkHandle, Ontology, OntologyObject, prop
from ontary.errors import ValidationFailed
from ontary.meta import (
    Cardinality,
    LinkTypeDef,
    ObjectTypeDef,
    PropertyDef,
    Sensitivity,
)
from ontary.scope import DirectProperty, ScopePolicy, SelfScope, ViaLink

_LEVELS = ["queue", "org"]
_M2O = Cardinality.MANY_TO_ONE

OntologyFactory = Callable[..., Ontology]
PolicyFactory = Callable[..., ScopePolicy]


def _hand_written_link(api_name: str, from_type: str, to_type: str, **kw: Any) -> LinkTypeDef:
    return LinkTypeDef(
        api_name=api_name,
        from_type=from_type,
        to_type=to_type,
        cardinality=_M2O,
        description=f"{from_type} -> {to_type}.",
        **kw,
    )


def _link_types() -> list[LinkTypeDef]:
    """Hand-written-literal parity fixture (spec `typed-authoring.md` AC1)
    -- mirrors `examples/tickets/ontology.py`'s four links field for field,
    NOT derived from the authoring API, so the parity tests below actually
    compare class-derived output against an independent hand-written
    expectation."""
    return [
        _hand_written_link("queueOfOrg", "Queue", "Org"),
        _hand_written_link("ticketInQueue", "Ticket", "Queue"),
        _hand_written_link("commentOnTicket", "Comment", "Ticket"),
        _hand_written_link("commentByAgent", "Comment", "Agent", identity_revealing=True),
    ]


def _hand_written_ticket() -> ObjectTypeDef:
    return ObjectTypeDef(
        api_name="Ticket",
        display_name="Ticket",
        description="A Ticket.",
        layer="L0",
        primary_key="id",
        properties=[
            PropertyDef(name="id", type="str"),
            PropertyDef(name="subject", type="str"),
            PropertyDef(name="age_hours", type="float"),
            PropertyDef(name="status", type="str", required=False),
            PropertyDef(
                name="queue_id", type="str", required=False, scope_level="queue"
            ),
            PropertyDef(name="escalated", type="bool", required=False),
        ],
        owned={"escalated": False},
    )


def _hand_written_agent() -> ObjectTypeDef:
    return ObjectTypeDef(
        api_name="Agent",
        display_name="Agent",
        description="A Agent.",
        layer="L0",
        primary_key="id",
        properties=[
            PropertyDef(name="id", type="str"),
            PropertyDef(name="display_name", type="str"),
            PropertyDef(
                name="email",
                type="str",
                required=False,
                sensitivity=Sensitivity(human_visible=False),
            ),
        ],
        owned=False,
    )


class TestDerivedMatchesHandWritten:
    def test_ticket_shape(self) -> None:
        ontology = Ontology(name="tickets", scope_levels=["queue", "org"])

        @ontology.object(layer="L0", owned={"escalated": False})
        class Ticket(OntologyObject):
            id: str = prop(primary_key=True)
            subject: str
            age_hours: float
            status: str | None = None
            queue_id: str | None = prop(default=None, scope_level="queue")
            escalated: bool | None = prop(default=None)

        derived = ontology.registry.get_object_type("Ticket")
        assert derived == _hand_written_ticket()

    def test_agent_shape(self) -> None:
        ontology = Ontology(name="tickets", scope_levels=["queue", "org"])

        @ontology.object(layer="L0")
        class Agent(OntologyObject):
            id: str = prop(primary_key=True)
            display_name: str
            email: str | None = prop(
                default=None, sensitivity=Sensitivity(human_visible=False)
            )

        derived = ontology.registry.get_object_type("Agent")
        assert derived == _hand_written_agent()

    def test_api_name_defaults_to_class_name(self) -> None:
        ontology = Ontology(name="tickets", scope_levels=[])

        @ontology.object(layer="L0")
        class Org(OntologyObject):
            id: str = prop(primary_key=True)

        assert "Org" in ontology.registry.object_types
        derived = ontology.registry.get_object_type("Org")
        assert derived.display_name == "Org"
        assert derived.description == "A Org."

    def test_api_name_and_description_override(self) -> None:
        ontology = Ontology(name="tickets", scope_levels=[])

        @ontology.object(layer="L0", api_name="Org2", description="An org.")
        class Org(OntologyObject):
            id: str = prop(primary_key=True)

        assert "Org2" in ontology.registry.object_types
        derived = ontology.registry.get_object_type("Org2")
        assert derived.description == "An org."

    def test_display_name_defaults_to_api_name(self) -> None:
        ontology = Ontology(name="tickets", scope_levels=[])

        @ontology.object(layer="L0")
        class Org(OntologyObject):
            id: str = prop(primary_key=True)

        derived = ontology.registry.get_object_type("Org")
        assert derived.display_name == "Org"

    def test_display_name_override(self) -> None:
        ontology = Ontology(name="tickets", scope_levels=[])

        @ontology.object(
            layer="L0", api_name="Org2", display_name="Organization"
        )
        class Org(OntologyObject):
            id: str = prop(primary_key=True)

        derived = ontology.registry.get_object_type("Org2")
        assert derived.display_name == "Organization"


class TestAnnotationMapping:
    def test_string_choices_are_declared_as_a_tuple(
        self, make_ontology: OntologyFactory
    ) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        @ontology.object(layer="L0")
        class Ticket(OntologyObject):
            id: str = prop(primary_key=True)
            status: str = prop(choices=["open", "closed"])

        prop_def = ontology.registry.get_object_type("Ticket").properties[1]
        assert prop_def.type == "str"
        assert prop_def.choices == ("open", "closed")

    def test_choices_on_non_string_property_are_rejected(
        self, make_ontology: OntologyFactory
    ) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.object(layer="L0")
            class Bad(OntologyObject):
                id: str = prop(primary_key=True)
                priority: int = prop(choices=["low", "high"])

        assert "priority" in str(exc_info.value)
        assert "str" in str(exc_info.value)

    def test_empty_choices_are_rejected(
        self, make_ontology: OntologyFactory
    ) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.object(layer="L0")
            class Bad(OntologyObject):
                id: str = prop(primary_key=True)
                status: str = prop(choices=[])

        assert "status" in str(exc_info.value)
        assert "empty" in str(exc_info.value)

    def test_non_string_choice_member_is_rejected(
        self, make_ontology: OntologyFactory
    ) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.object(layer="L0")
            class Bad(OntologyObject):
                id: str = prop(primary_key=True)
                status: str = prop(choices=["open", 1])  # type: ignore[list-item]

        assert "status" in str(exc_info.value)
        assert "non-string" in str(exc_info.value)

    def test_duplicate_choice_member_is_rejected(
        self, make_ontology: OntologyFactory
    ) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.object(layer="L0")
            class Bad(OntologyObject):
                id: str = prop(primary_key=True)
                status: str = prop(choices=["open", "open"])

        assert "status" in str(exc_info.value)
        assert "duplicate" in str(exc_info.value)

    def test_date_maps_to_date(self, make_ontology: OntologyFactory) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        @ontology.object(layer="L0")
        class Event(OntologyObject):
            id: str = prop(primary_key=True)
            occurred_on: date

        prop_def = ontology.registry.get_object_type("Event").properties[1]
        assert prop_def.type == "date"
        assert prop_def.required is True

    def test_datetime_maps_to_datetime(self, make_ontology: OntologyFactory) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        @ontology.object(layer="L0")
        class Event(OntologyObject):
            id: str = prop(primary_key=True)
            occurred_at: datetime

        prop_def = ontology.registry.get_object_type("Event").properties[1]
        assert prop_def.type == "datetime"
        assert prop_def.required is True

    def test_dict_list_any_map_to_json(self, make_ontology: OntologyFactory) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        @ontology.object(layer="L0")
        class Blob(OntologyObject):
            id: str = prop(primary_key=True)
            payload: dict[str, Any]
            tags: list[str]
            anything: Any

        types_ = {p.name: p.type for p in ontology.registry.get_object_type("Blob").properties}
        assert types_["payload"] == "json"
        assert types_["tags"] == "json"
        assert types_["anything"] == "json"

    def test_unmappable_annotation_raises(
        self, make_ontology: OntologyFactory
    ) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.object(layer="L0")
            class Bad(OntologyObject):
                id: str = prop(primary_key=True)
                tags: set[str]
        assert "tags" in str(exc_info.value)

    def test_property_type_override(self, make_ontology: OntologyFactory) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        @ontology.object(layer="L0")
        class Overridden(OntologyObject):
            id: str = prop(primary_key=True)
            weird: str = prop(property_type="json")

        prop_def = ontology.registry.get_object_type("Overridden").properties[1]
        assert prop_def.type == "json"


class TestRequiredRule:
    def test_non_optional_is_required(self, make_ontology: OntologyFactory) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        @ontology.object(layer="L0")
        class M(OntologyObject):
            id: str = prop(primary_key=True)
            a: str

        prop_def = ontology.registry.get_object_type("M").properties[1]
        assert prop_def.required is True

    def test_optional_is_not_required(self, make_ontology: OntologyFactory) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        @ontology.object(layer="L0")
        class M(OntologyObject):
            id: str = prop(primary_key=True)
            a: str | None = None

        prop_def = ontology.registry.get_object_type("M").properties[1]
        assert prop_def.required is False

    def test_required_override_on_optional(
        self, make_ontology: OntologyFactory
    ) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        @ontology.object(layer="L0")
        class M(OntologyObject):
            id: str = prop(primary_key=True)
            a: str | None = prop(default=None, required=True)

        prop_def = ontology.registry.get_object_type("M").properties[1]
        assert prop_def.required is True

    def test_required_override_on_non_optional(
        self, make_ontology: OntologyFactory
    ) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        @ontology.object(layer="L0")
        class M(OntologyObject):
            id: str = prop(primary_key=True)
            a: str = prop(required=False)

        prop_def = ontology.registry.get_object_type("M").properties[1]
        assert prop_def.required is False


class TestAC6Gate:
    def test_restricted_non_optional_rejected(
        self, make_ontology: OntologyFactory
    ) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.object(layer="L0")
            class M(OntologyObject):
                id: str = prop(primary_key=True)
                secret: str = prop(sensitivity=Sensitivity(human_visible=False))
        assert "secret" in str(exc_info.value)

    def test_restricted_ai_unusable_non_optional_rejected(
        self, make_ontology: OntologyFactory
    ) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.object(layer="L0")
            class M(OntologyObject):
                id: str = prop(primary_key=True)
                secret: str = prop(sensitivity=Sensitivity(ai_usable=False))
        assert "secret" in str(exc_info.value)

    def test_restricted_optional_accepted(
        self, make_ontology: OntologyFactory
    ) -> None:
        ontology = make_ontology(name="t", scope_levels=[])

        @ontology.object(layer="L0")
        class M(OntologyObject):
            id: str = prop(primary_key=True)
            secret: str | None = prop(
                default=None, sensitivity=Sensitivity(human_visible=False)
            )

        prop_def = ontology.registry.get_object_type("M").properties[1]
        assert prop_def.sensitivity.human_visible is False
        assert prop_def.required is False


class TestPrimaryKey:
    def test_missing_primary_key_rejected(self) -> None:
        ontology = Ontology(name="t", scope_levels=[])

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.object(layer="L0")
            class M(OntologyObject):
                id: str
        assert "primary_key" in str(exc_info.value)

    def test_duplicate_primary_key_rejected(self) -> None:
        ontology = Ontology(name="t", scope_levels=[])

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.object(layer="L0")
            class M(OntologyObject):
                id: str = prop(primary_key=True)
                other: str = prop(primary_key=True)
        assert "primary_key" in str(exc_info.value)


class TestReservedFieldNames:
    @pytest.mark.parametrize("bad_name", ["redacted_fields", "lineage"])
    def test_reserved_field_name_rejected(self, bad_name: str) -> None:
        ontology = Ontology(name="t", scope_levels=[])

        namespace = {
            "__annotations__": {"id": str, bad_name: str},
            "id": prop(primary_key=True),
        }
        cls = type("M", (OntologyObject,), namespace)

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
            ontology.object(layer="L0")(cls)
        assert bad_name in str(exc_info.value)


class TestPropFieldCoexistence:
    def test_plain_field_default_and_validator_coexist(self) -> None:
        ontology = Ontology(name="t", scope_levels=[])

        @ontology.object(layer="L0")
        class M(OntologyObject):
            id: str = prop(primary_key=True)
            plain: str
            with_default: str = "hello"
            validated: str = "x"

            @field_validator("validated")
            @classmethod
            def _upper(cls, v: str) -> str:
                return v.upper()

        instance = M(id="1", plain="a", validated="lower")
        assert instance.validated == "LOWER"
        assert instance.with_default == "hello"

        derived = ontology.registry.get_object_type("M")
        names = [p.name for p in derived.properties]
        assert names == ["id", "plain", "with_default", "validated"]
        required_by_name = {p.name: p.required for p in derived.properties}
        # plain default via `= "hello"` still counts as non-Optional-annotated
        # -> required True (default value doesn't change the annotation).
        assert required_by_name["with_default"] is True
        assert required_by_name["plain"] is True


class TestOntologyObjectAccessors:
    def test_defaults_to_empty_and_none(self) -> None:
        class M(OntologyObject):
            id: str

        instance = M(id="1")
        assert instance.redacted_fields == frozenset()
        assert instance.lineage is None

    def test_extra_keys_ignored(self) -> None:
        class M(OntologyObject):
            id: str

        instance = M.model_validate({"id": "1", "some_future_key": "x"})
        assert instance.id == "1"


class TestNoCrossTalk:
    def test_two_ontologies_same_class_name_no_conflict(self) -> None:
        ontology_a = Ontology(name="a", scope_levels=[])
        ontology_b = Ontology(name="b", scope_levels=[])

        @ontology_a.object(layer="L0")
        class M(OntologyObject):
            id: str = prop(primary_key=True)
            a_only: str

        @ontology_b.object(layer="L0")
        class M2(OntologyObject):  # noqa: N801 -- deliberately same api_name
            id: str = prop(primary_key=True)
            b_only: str

        # Force the same api_name on the second registration.
        ontology_b_registry_names = set(ontology_b.registry.object_types)
        assert ontology_b_registry_names == {"M2"}

        derived_a = ontology_a.registry.get_object_type("M")
        assert [p.name for p in derived_a.properties] == ["id", "a_only"]
        assert "M" not in ontology_b.registry.object_types
        assert "M2" not in ontology_a.registry.object_types

    def test_same_api_name_on_separate_ontologies_does_not_raise(self) -> None:
        ontology_a = Ontology(name="a", scope_levels=[])
        ontology_b = Ontology(name="b", scope_levels=[])

        @ontology_a.object(layer="L0", api_name="Shared")
        class M1(OntologyObject):
            id: str = prop(primary_key=True)

        @ontology_b.object(layer="L0", api_name="Shared")
        class M2(OntologyObject):
            id: str = prop(primary_key=True)

        assert ontology_a.registry.get_object_type("Shared").api_name == "Shared"
        assert ontology_b.registry.get_object_type("Shared").api_name == "Shared"


def _build_tickets_authored() -> tuple[Ontology, dict[str, type[OntologyObject]]]:
    """Authors the tickets object types + links + scope policy via the
    class-based API, mirroring `examples/tickets/ontology.py` field for
    field (minus actions/functions, out of scope for this test)."""
    ontology = Ontology(name="tickets", scope_levels=["queue", "org"], min_n=2)

    @ontology.object(layer="L0", scope=[SelfScope(level="org")])
    class Org(OntologyObject):
        id: str = prop(primary_key=True)
        name: str

    @ontology.object(
        layer="L0",
        scope=[
            SelfScope(level="queue"),
            ViaLink(link_api_name="queueOfOrg", direction="from", parent_type="Org"),
        ],
    )
    class Queue(OntologyObject):
        id: str = prop(primary_key=True)
        name: str

    @ontology.object(layer="L0", scope="unscoped")
    class Agent(OntologyObject):
        id: str = prop(primary_key=True)
        display_name: str
        email: str | None = prop(
            default=None, sensitivity=Sensitivity(human_visible=False)
        )

    @ontology.object(
        layer="L0",
        owned={"escalated": False},
        scope=[
            DirectProperty(level="queue", property_name="queue_id"),
            ViaLink(
                link_api_name="ticketInQueue", direction="from", parent_type="Queue"
            ),
        ],
    )
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        subject: str
        age_hours: float
        status: str | None = None
        queue_id: str | None = prop(default=None, scope_level="queue")
        escalated: bool | None = prop(default=None)

    @ontology.object(
        layer="L0",
        scope=[
            ViaLink(
                link_api_name="commentOnTicket", direction="from", parent_type="Ticket"
            )
        ],
    )
    class Comment(OntologyObject):
        id: str = prop(primary_key=True)
        text: str

    ontology.link("queueOfOrg", Queue, Org, Cardinality.MANY_TO_ONE)
    ontology.link("ticketInQueue", Ticket, Queue, "MANY_TO_ONE")
    ontology.link("commentOnTicket", Comment, Ticket, Cardinality.MANY_TO_ONE)
    ontology.link(
        "commentByAgent",
        Comment,
        Agent,
        Cardinality.MANY_TO_ONE,
        identity_revealing=True,
    )

    classes: dict[str, type[OntologyObject]] = {
        "Org": Org,
        "Queue": Queue,
        "Agent": Agent,
        "Ticket": Ticket,
        "Comment": Comment,
    }
    return ontology, classes


class TestLinks:
    def test_derived_links_match_hand_written(self) -> None:
        ontology, _classes = _build_tickets_authored()

        derived = ontology.registry.link_types
        hand_written = {link.api_name: link for link in _link_types()}

        assert derived.keys() == hand_written.keys()
        for api_name, link_def in hand_written.items():
            assert derived[api_name] == link_def

    def test_handle_carries_classes(self) -> None:
        ontology, classes = _build_tickets_authored()

        handle: LinkHandle[Any, Any] = ontology.link(
            "another", classes["Comment"], classes["Ticket"], Cardinality.MANY_TO_ONE
        )
        assert handle.api_name == "another"
        assert handle.from_cls is classes["Comment"]
        assert handle.to_cls is classes["Ticket"]

    def test_string_cardinality_accepted(self) -> None:
        ontology = Ontology(name="t", scope_levels=[])

        @ontology.object(layer="L0")
        class A(OntologyObject):
            id: str = prop(primary_key=True)

        @ontology.object(layer="L0")
        class B(OntologyObject):
            id: str = prop(primary_key=True)

        handle = ontology.link("aToB", A, B, "ONE_TO_MANY")
        assert (
            ontology.registry.get_link_type("aToB").cardinality
            == Cardinality.ONE_TO_MANY
        )
        assert handle.from_cls is A

    def test_link_to_class_from_another_ontology_rejected(self) -> None:
        ontology_a = Ontology(name="a", scope_levels=[])
        ontology_b = Ontology(name="b", scope_levels=[])

        @ontology_a.object(layer="L0")
        class A(OntologyObject):
            id: str = prop(primary_key=True)

        @ontology_b.object(layer="L0")
        class B(OntologyObject):
            id: str = prop(primary_key=True)

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
            ontology_a.link("aToB", A, B, Cardinality.MANY_TO_ONE)
        assert "B" in str(exc_info.value)


class TestScopePolicyAssembly:
    def test_policy_matches_hand_written(self, make_policy: PolicyFactory) -> None:
        ontology, _classes = _build_tickets_authored()

        derived = ontology.definition.policy
        expected = make_policy(
            levels=_LEVELS,
            unscoped_types={"Agent"},
            rules={
                "Org": [SelfScope(level="org")],
                "Queue": [
                    SelfScope(level="queue"),
                    ViaLink(
                        link_api_name="queueOfOrg", direction="from", parent_type="Org"
                    ),
                ],
                "Ticket": [
                    DirectProperty(level="queue", property_name="queue_id"),
                    ViaLink(
                        link_api_name="ticketInQueue",
                        direction="from",
                        parent_type="Queue",
                    ),
                ],
                "Comment": [
                    ViaLink(
                        link_api_name="commentOnTicket",
                        direction="from",
                        parent_type="Ticket",
                    )
                ],
            },
            contributor_rules={},
            row_visibility={},
            min_n=2,
        )

        assert derived.levels == expected.levels
        assert derived.unscoped_types == expected.unscoped_types
        assert derived.min_n == expected.min_n
        assert derived.rules == expected.rules
        assert derived.contributor_rules == expected.contributor_rules
        assert derived.row_visibility == expected.row_visibility

    def test_contributor_and_row_visibility_land_verbatim(self) -> None:
        ontology = Ontology(name="t", scope_levels=["team"], min_n=5)

        def _rv(store: Any, consumer: Any, obj_type: str, row: dict[str, Any]) -> bool:
            return True

        contributor_rule = [SelfScope(level="team")]

        @ontology.object(
            layer="L0",
            scope=[SelfScope(level="team")],
            contributor=contributor_rule,
            row_visibility=_rv,
        )
        class Response(OntologyObject):
            id: str = prop(primary_key=True)

        policy = ontology.definition.policy
        assert policy.contributor_rules == {"Response": contributor_rule}
        assert policy.row_visibility == {"Response": _rv}
        assert policy.min_n == 5


class TestDefinitionAndValidate:
    def test_definition_is_cached(self) -> None:
        ontology, _classes = _build_tickets_authored()
        first = ontology.definition
        second = ontology.definition
        assert first is second

    def test_validate_passes_for_tickets(self) -> None:
        ontology, _classes = _build_tickets_authored()
        ontology.validate()  # must not raise

    def test_validate_rejects_dangling_in_policy_ref(self) -> None:
        ontology = Ontology(name="t", scope_levels=["team"])

        @ontology.object(
            layer="L0",
            scope=[DirectProperty(level="team", property_name="team_id")],
        )
        class Thing(OntologyObject):
            id: str = prop(primary_key=True)

        with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR") as exc_info:
            ontology.validate()
        assert "team_id" in str(exc_info.value)

    def test_validate_rejects_duplicate_api_name(self) -> None:
        ontology = Ontology(name="t", scope_levels=[])

        @ontology.object(layer="L0", api_name="Dup")
        class A(OntologyObject):
            id: str = prop(primary_key=True)

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.object(layer="L0", api_name="Dup")
            class B(OntologyObject):
                id: str = prop(primary_key=True)
        assert "Dup" in str(exc_info.value)

    def test_validate_rejects_owned_pk_violation(self) -> None:
        ontology = Ontology(name="t", scope_levels=["global"])

        @ontology.object(layer="L0", scope="unscoped", owned={"id": "x"})
        class Thing(OntologyObject):
            id: str = prop(primary_key=True)

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
            ontology.validate()
        assert "primary key" in str(exc_info.value)

    def test_late_object_registration_after_definition_rejected(self) -> None:
        ontology, _classes = _build_tickets_authored()
        ontology.definition  # freeze

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:

            @ontology.object(layer="L0")
            class Late(OntologyObject):
                id: str = prop(primary_key=True)
        assert "Late" in str(exc_info.value)

    def test_late_link_registration_after_definition_rejected(self) -> None:
        ontology, classes = _build_tickets_authored()
        ontology.definition  # freeze

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
            ontology.link(
                "lateLink", classes["Comment"], classes["Org"], Cardinality.MANY_TO_ONE
            )
        assert "frozen" in str(exc_info.value)

    def test_redecorating_class_on_second_ontology_rejected(self) -> None:
        first = Ontology(name="first", scope_levels=[])

        @first.object(layer="L0")
        class Thing(OntologyObject):
            id: str = prop(primary_key=True)

        second = Ontology(name="second", scope_levels=[])

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
            second.object(layer="L0")(Thing)
        assert "already registered on another" in str(exc_info.value)

    def test_redecorating_class_on_same_ontology_hits_duplicate_api_name(self) -> None:
        ontology = Ontology(name="t", scope_levels=[])

        @ontology.object(layer="L0")
        class Thing(OntologyObject):
            id: str = prop(primary_key=True)

        with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
            ontology.object(layer="L0")(Thing)
        assert "Thing" in str(exc_info.value)


def test_front_door_exports() -> None:
    from ontary import LinkHandle as FrontDoorLinkHandle
    from ontary import Ontology as FrontDoorOntology
    from ontary import OntologyObject as FrontDoorOntologyObject
    from ontary import prop as front_door_prop

    assert FrontDoorLinkHandle is LinkHandle
    assert FrontDoorOntology is Ontology
    assert FrontDoorOntologyObject is OntologyObject
    assert front_door_prop is prop
