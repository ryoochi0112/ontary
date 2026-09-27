"""Declaration contract for choice transitions and single-object rules."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import raises_code

from ontary.errors import ValidationFailed
from ontary.meta import ObjectTypeDef, PropertyDef, RuleDef, TransitionDef

CHOICES = ("pending", "paid", "shipped")
MOVES = {"pending": ("paid",), "paid": ("shipped",), "shipped": ()}


def _object(*rules: RuleDef) -> ObjectTypeDef:
    return ObjectTypeDef(
        api_name="Order",
        display_name="Order",
        description="An order",
        layer="test",
        properties=[
            PropertyDef(name="id", type="str"),
            PropertyDef(
                name="status", type="str", choices=CHOICES,
                transitions=TransitionDef(initial=("pending",), moves=MOVES),
            ),
        ],
        primary_key="id",
        rules=rules,
    )


def test_valid_transition_graph_and_rule_are_serializable() -> None:
    graph = TransitionDef(initial=("pending",), moves=MOVES)
    status = PropertyDef(name="status", type="str", choices=CHOICES, transitions=graph)
    rule = RuleDef(name="paid_first", message="pay before shipping", check=lambda row: bool(row))
    obj = _object(rule)
    assert status.transitions == graph
    assert obj.rules == (rule,)
    assert "check" not in rule.model_dump()
    dumped = obj.model_dump()
    assert dumped["rules"] == ({"name": "paid_first", "message": "pay before shipping"},)
    assert dumped["properties"][1]["transitions"] == {
        "initial": ("pending",), "moves": MOVES,
    }
    assert json.loads(obj.model_dump_json())["rules"] == [
        {"name": "paid_first", "message": "pay before shipping"}
    ]
    assert json.loads(obj.model_dump_json())["properties"][1]["transitions"] == {
        "initial": ["pending"],
        "moves": {"pending": ["paid"], "paid": ["shipped"], "shipped": []},
    }


def test_transition_requires_initial_state() -> None:
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        TransitionDef(initial=(), moves=MOVES)
    assert "initial" in str(excinfo.value) and "non-empty" in str(excinfo.value)


@pytest.mark.parametrize(
    "initial,moves",
    [
        ((1,), MOVES),
        (("pending",), {1: ("paid",)}),
        (("pending",), {"pending": (1,)}),
    ],
)
def test_transition_states_must_be_strings(
    initial: tuple[Any, ...], moves: dict[Any, tuple[Any, ...]]
) -> None:
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        TransitionDef(initial=initial, moves=moves)
    assert "state" in str(excinfo.value) and "str" in str(excinfo.value)


def test_transition_requires_choices() -> None:
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        PropertyDef(name="status", type="str", transitions=TransitionDef(initial=("pending",), moves=MOVES))
    assert "declare choices (an Enum or Literal) on the property" in str(excinfo.value)


@pytest.mark.parametrize(
    "initial,moves,unknown",
    [
        (("missing",), MOVES, "missing"),
        (("pending",), {**MOVES, "missing": ()}, "missing"),
        (("pending",), {**MOVES, "paid": ("missing",)}, "missing"),
    ],
)
def test_transition_refuses_undeclared_state(
    initial: tuple[str, ...], moves: dict[str, tuple[str, ...]], unknown: str
) -> None:
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        PropertyDef(
            name="status", type="str", choices=CHOICES,
            transitions=TransitionDef(initial=initial, moves=moves),
        )
    assert unknown in str(excinfo.value)
    assert "use only declared choices" in str(excinfo.value)


def test_transition_requires_every_choice_as_move_key() -> None:
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        PropertyDef(
            name="status", type="str", choices=CHOICES,
            transitions=TransitionDef(initial=("pending",), moves={"pending": ("paid",), "paid": ()}),
        )
    assert "shipped" in str(excinfo.value)
    assert "list every state; use [] for a terminal state" in str(excinfo.value)


@pytest.mark.parametrize(
    "name,message,check,fix",
    [
        ("", "pay before shipping", lambda row: True, "name must be non-empty"),
        ("paid_first", "", lambda row: True, "message must be non-empty"),
        ("paid_first", "pay before shipping", 1, "check must be callable"),
    ],
)
def test_rule_refuses_invalid_declaration(
    name: str, message: str, check: Any, fix: str
) -> None:
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        RuleDef(name=name, message=message, check=check)
    assert fix in str(excinfo.value)


def test_object_type_refuses_duplicate_rule_names() -> None:
    first = RuleDef(name="paid_first", message="one", check=lambda row: True)
    second = RuleDef(name="paid_first", message="two", check=lambda row: False)
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        _object(first, second)
    assert "paid_first" in str(excinfo.value)
    assert "give each rule a unique name" in str(excinfo.value)
