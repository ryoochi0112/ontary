"""Store-level checks for declared transitions and rules."""

from __future__ import annotations

from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from conftest import raises_code

from ontary import ObjectStore, Source
from ontary.errors import PreconditionFailed, ValidationFailed
from ontary.meta import (
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
    RuleDef,
    Sensitivity,
    TransitionDef,
)
from ontary.store._shared import merge_update

SRC = Source(source_system="seed")
GRAPH = TransitionDef(
    initial=("pending",),
    moves={"pending": ("paid", "cancelled"), "paid": ("shipped",),
           "shipped": (), "cancelled": ()},
)


def _store(
    *, rules: tuple[RuleDef, ...] = (), sensitivity: Sensitivity | None = None,
    path: str = ":memory:", governed: bool = True,
) -> ObjectStore:
    registry = OntologyRegistry()
    registry.register_object_type(ObjectTypeDef(
        api_name="Order", display_name="Order", description="An order", layer="L0",
        properties=[
            PropertyDef(name="id", type="str"),
            PropertyDef(name="status", type="str",
                        choices=tuple(GRAPH.moves) if governed else None,
                        transitions=GRAPH if governed else None, required=False,
                        sensitivity=sensitivity or Sensitivity()),
            PropertyDef(name="paid_at", type="str", required=False),
            PropertyDef(name="placed_on", type="date", required=False),
        ],
        rules=rules, primary_key="id", owned=True,
    ))
    return ObjectStore(registry, path=path)


def _row(store: ObjectStore, obj_id: str) -> dict[str, Any]:
    row = store.read_current("Order", obj_id)
    assert row is not None
    return row.payload


def test_update_only_allows_declared_move_and_reports_allowed_targets() -> None:
    store = _store()
    store.insert("Order", {"id": "o-1", "status": "pending"}, SRC)
    with raises_code(PreconditionFailed, "TRANSITION_NOT_ALLOWED") as exc:
        store.update("Order", "o-1", {"status": "shipped"}, SRC)
    assert str(exc.value) == (
        "Order 'o-1': status cannot move from 'pending' to 'shipped'; "
        "allowed from 'pending': ['paid', 'cancelled']"
    )
    assert _row(store, "o-1")["status"] == "pending"
    store.update("Order", "o-1", {"status": "paid"}, SRC)
    assert _row(store, "o-1")["status"] == "paid"


def test_update_from_preexisting_undeclared_state_is_coded_refusal(tmp_path: Path) -> None:
    path = str(tmp_path / "orders.sqlite")
    old_store = _store(path=path, governed=False)
    old_store.insert("Order", {"id": "o-1", "status": "open"}, SRC)

    store = _store(path=path)
    with raises_code(PreconditionFailed, "TRANSITION_NOT_ALLOWED") as exc:
        store.update("Order", "o-1", {"status": "paid"}, SRC)
    assert str(exc.value) == (
        "Order 'o-1': status cannot move from 'open' to 'paid'; "
        "allowed from 'open': []"
    )
    assert _row(store, "o-1")["status"] == "open"


def test_unchanged_state_and_unrelated_update_are_allowed() -> None:
    store = _store()
    store.insert("Order", {"id": "o-1", "status": "shipped"}, SRC)
    store.update("Order", "o-1", {"status": "shipped"}, SRC)
    store.update("Order", "o-1", {"paid_at": "today"}, SRC)
    assert _row(store, "o-1") == {"id": "o-1", "status": "shipped", "paid_at": "today"}


def test_store_insert_may_start_anywhere_but_captured_insert_checks_initial() -> None:
    store = _store()
    store.insert("Order", {"id": "free", "status": "shipped"}, SRC)
    assert _row(store, "free")["status"] == "shipped"
    with store.capture_action_writes() as writes:
        with raises_code(PreconditionFailed, "TRANSITION_NOT_ALLOWED") as exc:
            store.insert("Order", {"id": "captured", "status": "shipped"}, SRC)
    assert str(exc.value) == (
        "Order 'captured': status cannot move from None to 'shipped'; "
        "allowed from None: ['pending']"
    )
    assert writes == []
    assert store.read_current("Order", "captured") is None


def test_update_to_none_is_refused_and_none_start_uses_capture_flag() -> None:
    store = _store()
    store.insert("Order", {"id": "o-1", "status": "pending"}, SRC)
    with raises_code(PreconditionFailed, "TRANSITION_NOT_ALLOWED") as exc:
        store.update("Order", "o-1", {"status": None}, SRC)
    assert str(exc.value) == (
        "Order 'o-1': status cannot move from 'pending' to None; "
        "allowed from 'pending': ['paid', 'cancelled']"
    )
    store.insert("Order", {"id": "empty"}, SRC)
    store.update("Order", "empty", {"status": "shipped"}, SRC)
    assert _row(store, "empty")["status"] == "shipped"
    current = store.read_current("Order", "empty")
    assert current is not None
    # The backends pass the action capture flag in T3; test this helper's flag here.
    with raises_code(PreconditionFailed, "TRANSITION_NOT_ALLOWED") as captured:
        merge_update(store._registry.get_object_type("Order"), "Order", "empty", current,
                     {"status": "pending"}, capturing=True)
    assert str(captured.value) == (
        "Order 'empty': status cannot move from 'shipped' to 'pending'; "
        "allowed from 'shipped': []"
    )
    store.insert("Order", {"id": "unset"}, SRC)
    unset = store.read_current("Order", "unset")
    assert unset is not None
    with raises_code(PreconditionFailed, "TRANSITION_NOT_ALLOWED") as start:
        merge_update(store._registry.get_object_type("Order"), "Order", "unset", unset,
                     {"status": "shipped"}, capturing=True)
    assert str(start.value) == (
        "Order 'unset': status cannot move from None to 'shipped'; "
        "allowed from None: ['pending']"
    )
    merged = merge_update(store._registry.get_object_type("Order"), "Order", "unset", unset,
                          {"status": "pending"}, capturing=True)
    assert merged["status"] == "pending"


@pytest.mark.parametrize("sensitivity", [Sensitivity(ai_usable=False), Sensitivity(human_visible=False)])
def test_transition_refusal_hides_restricted_current_value(sensitivity: Sensitivity) -> None:
    store = _store(sensitivity=sensitivity)
    store.insert("Order", {"id": "o-1", "status": "pending"}, SRC)
    with raises_code(PreconditionFailed, "TRANSITION_NOT_ALLOWED") as exc:
        store.update("Order", "o-1", {"status": "shipped"}, SRC)
    assert str(exc.value) == (
        "Order 'o-1': status cannot move from its current value to 'shipped'"
    )


def test_rules_run_on_insert_and_update_with_full_normalized_row() -> None:
    class Status(Enum):
        PAID = "paid"
        SHIPPED = "shipped"

    seen: list[dict[str, Any]] = []

    def paid_when_shipped(row: dict[str, Any]) -> bool:
        seen.append(row.copy())
        return row.get("status") != "shipped" or row.get("paid_at") is not None

    store = _store(rules=(RuleDef(name="shipped_needs_payment", message="payment required",
                                  check=paid_when_shipped),))
    with raises_code(ValidationFailed, "RULE_VIOLATED") as inserted:
        store.insert("Order", {"id": "bad", "status": "shipped"}, SRC)
    assert str(inserted.value) == "Order 'bad': rule 'shipped_needs_payment': payment required"
    assert store.read_current("Order", "bad") is None
    store.insert("Order", {"id": "o-1", "status": Status.PAID, "paid_at": "today"}, SRC)
    store.update("Order", "o-1", {"status": Status.SHIPPED}, SRC)
    with raises_code(ValidationFailed, "RULE_VIOLATED") as updated:
        store.update("Order", "o-1", {"paid_at": None,
                                       "placed_on": date(2026, 9, 28)}, SRC)
    assert str(updated.value) == "Order 'o-1': rule 'shipped_needs_payment': payment required"
    assert _row(store, "o-1")["paid_at"] == "today"
    assert seen[-1] == {"id": "o-1", "status": "shipped", "paid_at": None,
                        "placed_on": "2026-09-28"}


def test_raising_rule_fails_closed_and_reports_all_rules_in_order() -> None:
    def raises(row: dict[str, Any]) -> bool:
        raise RuntimeError("secret row value")

    store = _store(rules=(
        RuleDef(name="first", message="first failed", check=lambda row: False),
        RuleDef(name="broken", message="unavailable", check=raises),
        RuleDef(name="third", message="third failed", check=lambda row: False),
    ))
    with raises_code(ValidationFailed, "RULE_VIOLATED") as exc:
        store.insert("Order", {"id": "o-1", "status": "pending"}, SRC)
    assert str(exc.value) == (
        "Order 'o-1': rule 'first': first failed; "
        "rule 'broken' raised RuntimeError; rule 'third': third failed"
    )


def test_retire_does_not_run_rule_or_transition_check() -> None:
    calls = 0

    def rule(row: dict[str, Any]) -> bool:
        nonlocal calls
        calls += 1
        return True

    store = _store(rules=(RuleDef(name="watch", message="watched", check=rule),))
    store.insert("Order", {"id": "o-1", "status": "pending"}, SRC)
    assert calls == 1
    store.retire_object("Order", "o-1")
    assert calls == 1
