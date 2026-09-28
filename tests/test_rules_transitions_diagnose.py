"""Stored-row diagnostics for declared rules."""

from __future__ import annotations

import json

from conftest import raises_code

from ontary import Ontology, OntologyObject, prop
from ontary.errors import ValidationFailed
from ontary.meta import TransitionDef
from ontary.store import Source
from ontary.store.inmemory import InMemoryStore
from ontary.store.values import StoredObject


def _orders() -> tuple[Ontology, InMemoryStore]:
    ontology = Ontology("orders", ["org"])

    @ontology.object(layer="L0", scope="unscoped")
    class Order(OntologyObject):
        id: str = prop(primary_key=True)
        status: str = prop(
            choices=("pending", "shipped"),
            transitions=TransitionDef(
                initial=("pending",),
                moves={"pending": ("shipped",), "shipped": ()},
            ),
        )
        paid: bool

    @ontology.rule(Order, "payment_first", message="payment required")
    def payment_first(order: Order) -> bool:
        return order.status != "shipped" or order.paid

    @ontology.rule(Order, "check_runs", message="check must not raise")
    def check_runs(order: Order) -> bool:
        if order.id == "o-2" and order.status == "shipped":
            raise RuntimeError("deliberate predicate failure")
        return True

    return ontology, InMemoryStore(ontology.registry)


def _insert(store: InMemoryStore, obj_id: str, *, status: str = "pending") -> None:
    store.insert(
        "Order", {"id": obj_id, "status": status, "paid": True},
        Source(source_system="test"),
    )


def _alter_payload(store: InMemoryStore, obj_id: str, **changes: object) -> None:
    for row in store._objects:
        if row["id"] == obj_id:
            payload = json.loads(row["payload"])
            payload.update(changes)
            row["payload"] = json.dumps(payload)
            return
    raise AssertionError(f"missing test row {obj_id}")


def test_diagnose_aggregates_broken_rows_by_rule_and_counts_raising_checks() -> None:
    ontology, store = _orders()
    _insert(store, "o-1")
    _insert(store, "o-2")
    _insert(store, "o-3", status="shipped")
    _alter_payload(store, "o-1", status="shipped", paid=False)
    _alter_payload(store, "o-2", status="shipped", paid=False)

    findings = ontology.diagnose(store=store)

    assert [(f.code, f.severity, f.location, f.message, f.fix_hint) for f in findings] == [
        (
            "RULE_VIOLATED", "error", "ObjectTypeDef['Order'].rules['payment_first']",
            "2 of 3 current rows break rule 'payment_first' (payment required) (e.g. id 'o-1')",
            "fix those rows with an action, or re-ingest them",
        ),
        (
            "RULE_VIOLATED", "error", "ObjectTypeDef['Order'].rules['check_runs']",
            "1 of 3 current rows break rule 'check_runs' (check must not raise) (e.g. id 'o-2')",
            "fix those rows with an action, or re-ingest them",
        ),
    ]


def test_validate_store_raises_rule_violated_for_stored_rule_failure() -> None:
    ontology, store = _orders()
    _insert(store, "o-1")
    _alter_payload(store, "o-1", status="shipped", paid=False)

    with raises_code(ValidationFailed, "RULE_VIOLATED") as exc:
        ontology.validate(store=store)
    assert str(exc.value) == (
        "1 of 1 current rows break rule 'payment_first' (payment required) "
        "(e.g. id 'o-1')"
    )


def test_validate_prefers_invalid_record_when_hydration_and_rule_fail() -> None:
    ontology, store = _orders()
    _insert(store, "o-1")
    _alter_payload(store, "o-1", status="shipped", paid=False)
    _alter_payload(store, "o-1", paid="not a bool")

    with raises_code(ValidationFailed, "INVALID_RECORD") as exc:
        ontology.validate(store=store)
    assert "payment_first" in str(exc.value)


def test_clean_store_has_no_rule_or_transition_findings() -> None:
    ontology, store = _orders()
    _insert(store, "o-1", status="shipped")

    assert ontology.diagnose(store=store) == []
    ontology.validate(store=store)


def test_rule_sweep_read_failure_becomes_diagnostic_finding() -> None:
    ontology, _ = _orders()

    class BrokenRuleRead(InMemoryStore):
        reads = 0

        def read_all(self, obj_type: str) -> list[StoredObject]:
            self.reads += 1
            if self.reads == 2:
                raise RuntimeError("rule read failed")
            return super().read_all(obj_type)

    findings = ontology.diagnose(store=BrokenRuleRead(ontology.registry))

    assert [(f.code, f.location) for f in findings] == [
        ("DIAGNOSE_RULE_FAILED", "_stored_rule_findings['Order']")
    ]
    assert "rule read failed" in findings[0].message
