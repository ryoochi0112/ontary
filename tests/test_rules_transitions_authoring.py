"""Typed authoring of declared transitions and rules."""

from __future__ import annotations

from enum import StrEnum
from typing import assert_type

from conftest import raises_code

from ontary import Ontology, OntologyObject, prop
from ontary.errors import ValidationFailed
from ontary.meta import TransitionDef


class Status(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"


GRAPH = TransitionDef(
    initial=("pending",),
    moves={"pending": ("paid",), "paid": ("shipped",), "shipped": ()},
)


def test_enum_transition_states_and_typed_rule_are_registered() -> None:
    ontology = Ontology("orders", ["org"])
    enum_graph = TransitionDef(
        initial=(Status.PENDING,),
        moves={Status.PENDING: (Status.PAID,), Status.PAID: (Status.SHIPPED,), Status.SHIPPED: ()},
    )

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Order(OntologyObject):
        id: str = prop(primary_key=True)
        status: Status = prop(transitions=enum_graph)

    seen: list[Order] = []

    @ontology.rule(Order, "paid_first", message="payment required")
    def paid_first(order: Order) -> bool:
        assert_type(order, Order)
        seen.append(order)
        return order.status is Status.PENDING

    obj = ontology.registry.get_object_type("Order")
    assert obj.properties[1].transitions == GRAPH
    assert [(rule.name, rule.message) for rule in obj.rules] == [("paid_first", "payment required")]
    assert obj.rules[0].check({"id": "o-1", "status": "pending"})
    assert type(seen[0]) is Order and seen[0].status is Status.PENDING
    assert paid_first(seen[0])


def test_non_choice_transition_is_refused_through_authoring() -> None:
    ontology = Ontology("orders", ["org"])
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc:
        @ontology.object(layer="L0", scope="unscoped")
        class Order(OntologyObject):
            id: str = prop(primary_key=True)
            status: str = prop(transitions=GRAPH)
    assert str(exc.value) == (
        "PropertyDef 'status': transitions require choices; "
        "declare choices (an Enum or Literal) on the property"
    )


def test_primary_key_transition_is_refused() -> None:
    ontology = Ontology("orders", ["org"])
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc:
        @ontology.object(layer="L0", scope="unscoped")
        class Order(OntologyObject):
            id: str = prop(primary_key=True, choices=("pending", "paid", "shipped"), transitions=GRAPH)
    assert str(exc.value) == (
        "Order.id: transitions cannot govern a primary key; "
        "move transitions to a choice property"
    )


def test_rule_refuses_unknown_class() -> None:
    ontology = Ontology("orders", ["org"])

    class Unknown(OntologyObject):
        id: str = prop(primary_key=True)

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc:
        @ontology.rule(Unknown, "check", message="check it")
        def unknown(obj: Unknown) -> bool:
            return True
    assert str(exc.value) == (
        "'Unknown' is not registered on Ontology 'orders' "
        "(decorate it with @ontology.object(...) on THIS ontology before linking it)"
    )


def test_rule_refuses_frozen_ontology() -> None:
    ontology = Ontology("orders", ["org"])

    @ontology.object(layer="L0", scope="unscoped")
    class Order(OntologyObject):
        id: str = prop(primary_key=True)

    ontology.definition
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc:
        @ontology.rule(Order, "check", message="check it")
        def frozen(obj: Order) -> bool:
            return True
    assert str(exc.value) == (
        "Ontology 'orders': cannot register rule 'check' after .definition "
        "has already been built (registrations are frozen once the definition is accessed)"
    )


def test_duplicate_rule_name_is_refused() -> None:
    ontology = Ontology("orders", ["org"])

    @ontology.object(layer="L0", scope="unscoped")
    class Order(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.rule(Order, "check", message="first")
    def first(obj: Order) -> bool:
        return True

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc:
        @ontology.rule(Order, "check", message="second")
        def second(obj: Order) -> bool:
            return True
    assert str(exc.value) == (
        "ObjectTypeDef 'Order': duplicate rule 'check'; "
        "give each rule a unique name"
    )
