"""Per-row ingest reporting for declared transitions and rules."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from ontary.client import OntologyClient
from ontary.errors import ValidationFailed
from ontary.ingest import IngestError
from ontary.meta import (
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
    RuleDef,
    TransitionDef,
)
from ontary.ontology import OntologyDef
from ontary.scope import ScopePolicy
from ontary.security import Consumer
from ontary.store import ObjectStore, Source

SRC = Source(source_system="loader")


@pytest.fixture
def setup() -> Iterator[tuple[OntologyClient, ObjectStore]]:
    registry = OntologyRegistry()
    registry.register_object_type(ObjectTypeDef(
        api_name="Order", display_name="Order", description="An order", layer="L0",
        properties=[
            PropertyDef(name="id", type="str"),
            PropertyDef(
                name="status", type="str", choices=("pending", "paid", "shipped"),
                transitions=TransitionDef(
                    initial=("pending",),
                    moves={"pending": ("paid",), "paid": ("shipped",), "shipped": ()},
                ),
            ),
            PropertyDef(name="paid_at", type="str", required=False),
        ],
        rules=(RuleDef(
            name="shipped_needs_payment", message="payment required",
            check=lambda row: row.get("status") != "shipped" or row.get("paid_at") is not None,
        ),),
        primary_key="id",
    ))
    ontology = OntologyDef(
        name="orders", registry=registry,
        policy=ScopePolicy(levels=["org"], unscoped_types={"Order"}, min_n=1),
    )
    ontology.validate()
    store = ObjectStore(registry)
    client = OntologyClient(
        ontology, store,
        Consumer(actor_id="loader", role="Loader", scope_level="org",
                 scope_id="org-1", kind="human"),
    )
    try:
        yield client, store
    finally:
        store._conn.close()


def _payload(store: ObjectStore, obj_id: str) -> dict[str, Any] | None:
    row = store.read_current("Order", obj_id)
    return None if row is None else row.payload


def test_transition_refusal_is_per_row_and_later_rows_commit(
    setup: tuple[OntologyClient, ObjectStore],
) -> None:
    client, store = setup
    store.insert("Order", {"id": "existing", "status": "pending"}, SRC)

    report = client.ingest("Order", [
        {"id": "existing", "status": "shipped", "paid_at": "today"},
        {"id": "later", "status": "shipped", "paid_at": "today"},
    ], SRC, on_error="report")

    assert report.inserted_ids == ["later"]
    assert [(error.index, error.code, error.reason) for error in report.errors] == [(
        0, "TRANSITION_NOT_ALLOWED",
        "Order 'existing': status cannot move from 'pending' to 'shipped'; "
        "allowed from 'pending': ['paid']",
    )]
    assert _payload(store, "existing") == {"id": "existing", "status": "pending"}
    assert _payload(store, "later") == {
        "id": "later", "status": "shipped", "paid_at": "today",
    }


def test_raise_mode_finishes_batch_before_raising_transition_report(
    setup: tuple[OntologyClient, ObjectStore],
) -> None:
    client, store = setup
    store.insert("Order", {"id": "existing", "status": "pending"}, SRC)

    with pytest.raises(IngestError) as caught:
        client.ingest("Order", [
            {"id": "existing", "status": "shipped", "paid_at": "today"},
            {"id": "later", "status": "paid"},
        ], SRC, on_error="raise")

    report = caught.value.report
    assert report is not None
    assert report.inserted_ids == ["later"]
    assert [(error.index, error.code, error.reason) for error in report.errors] == [(
        0, "TRANSITION_NOT_ALLOWED",
        "Order 'existing': status cannot move from 'pending' to 'shipped'; "
        "allowed from 'pending': ['paid']",
    )]
    assert _payload(store, "later") == {"id": "later", "status": "paid"}


def test_rule_refusals_on_insert_and_update_are_per_row(
    setup: tuple[OntologyClient, ObjectStore],
) -> None:
    client, store = setup
    store.insert("Order", {"id": "existing", "status": "shipped", "paid_at": "today"}, SRC)

    report = client.ingest("Order", [
        {"id": "bad-insert", "status": "shipped"},
        {"id": "existing", "status": "shipped", "paid_at": None},
        {"id": "later", "status": "pending"},
    ], SRC, on_error="report")

    assert report.inserted_ids == ["later"]
    assert [(error.index, error.code, error.reason) for error in report.errors] == [
        (0, "RULE_VIOLATED", "Order 'bad-insert': rule 'shipped_needs_payment': payment required"),
        (1, "RULE_VIOLATED", "Order 'existing': rule 'shipped_needs_payment': payment required"),
    ]
    assert _payload(store, "bad-insert") is None
    assert _payload(store, "existing") == {
        "id": "existing", "status": "shipped", "paid_at": "today",
    }
    assert _payload(store, "later") == {"id": "later", "status": "pending"}


def test_other_store_errors_still_propagate(
    setup: tuple[OntologyClient, ObjectStore], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = setup
    store.insert("Order", {"id": "existing", "status": "pending"}, SRC)

    def fail_update(*args: object, **kwargs: object) -> None:
        raise ValidationFailed("unexpected store rejection", code="INVALID_RECORD")

    monkeypatch.setattr(store, "update", fail_update)
    with pytest.raises(ValidationFailed, match="unexpected store rejection") as caught:
        client.ingest("Order", [
            {"id": "existing", "status": "paid"},
            {"id": "later", "status": "pending"},
        ], SRC, on_error="report")
    assert caught.value.code == "INVALID_RECORD"
    assert _payload(store, "later") is None
