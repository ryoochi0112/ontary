"""Governed event reads (#47): latest subject visibility and typed payloads."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ValidationError

from ontary import Ontology, OntologyObject, Source, prop
from ontary.audit import AuditEntry, EmittedEvent
from ontary.client import OntologyClient
from ontary.errors import ValidationFailed
from ontary.meta import ActionTypeDef, Sensitivity
from ontary.model import Event
from ontary.scope import DirectProperty
from ontary.store.inmemory import InMemoryStore
from ontary.testing import FixedClock, consumer

INSTANT = datetime(2026, 10, 4, 9, tzinfo=timezone.utc)
SOURCE = Source(source_system="seed")


class ShippingState(Enum):
    SHIPPED = "shipped"


class ShipmentDetail(BaseModel):
    state: ShippingState
    at: datetime
    day: date


@dataclass
class ReadDomain:
    ontology: Ontology
    store: InMemoryStore
    order: type[OntologyObject]
    other: type[OntologyObject]
    shipped: type[Event]
    packed: type[Event]

    def client(self, scope_id: str = "org-1", kind: Literal["human", "ai"] = "human") -> OntologyClient:
        return self.ontology.bind(self.store).for_consumer(
            consumer(scope_level="org", scope_id=scope_id, kind=kind)
        )

    def append(
        self, *carriers: str, event_type: str = "OrderShipped", about_type: str = "Order",
        about_id: str = "order-1", ts: datetime = INSTANT, kind: str = "action",
        outcome: str = "ok", payload: dict[str, Any] | None = None,
    ) -> None:
        self.store.append_audit(AuditEntry(
            ts=ts, kind=kind, invocation_id=f"inv-{len(self.store.audit_entries()) + 1}",
            actor="operator", role="Operator", action="ShipOrder", target_type=about_type,
            outcome=outcome,
            events=[EmittedEvent(
                event_type=event_type, about_type=about_type, about_id=about_id,
                payload={"carrier": carrier, **(payload or {})},
            ) for carrier in carriers],
        ))


@pytest.fixture
def domain() -> ReadDomain:
    ontology = Ontology("event-reads", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope=[DirectProperty(level="org", property_name="org_id")])
    class Order(OntologyObject):
        id: str = prop(primary_key=True)
        org_id: str
        disclosed: bool | None = prop(sensitivity=Sensitivity(human_visible=False))

    @ontology.object(layer="L0", scope="unscoped")
    class OtherOrder(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.event()
    class OrderShipped(Event):
        carrier: str
        state: ShippingState | None = None
        at: datetime | None = None
        day: date | None = None
        detail: ShipmentDetail | None = None
        # A hidden field must become None even when its author supplied a default.
        ai_secret: str | None = prop(
            default="human-default", sensitivity=Sensitivity(ai_usable=False),
        )
        human_secret: str | None = prop(
            default="ai-default", sensitivity=Sensitivity(human_visible=False),
        )

    @ontology.event(api_name="Packed")
    class OrderPacked(Event):
        carrier: str

    for name, subject_type, emitted_types in (
        ("ShipOtherOrder", "OtherOrder", ["OrderShipped"]),
        ("ShipOrder", "Order", ["OrderShipped", "Packed"]),
    ):
        ontology.registry.register_action_type(ActionTypeDef(
            api_name=name, display_name=name, description="Emit order facts",
            target_type=subject_type, executable_by_roles=["Operator"], emits=emitted_types,
        ))

    store = InMemoryStore(ontology.registry)
    store.bind_clock(FixedClock(INSTANT))
    for object_id, org_id in (("order-1", "org-1"), ("order-2", "org-2")):
        store.insert("Order", {"id": object_id, "org_id": org_id, "disclosed": True}, SOURCE)
    store.insert("OtherOrder", {"id": "order-1"}, SOURCE)
    store.insert("OtherOrder", {"id": "order-2"}, SOURCE)
    return ReadDomain(ontology, store, Order, OtherOrder, OrderShipped, OrderPacked)


def _carriers(records: list[Any]) -> list[str]:
    return [record.payload.carrier for record in records]


def test_typed_records_have_invocation_metadata_and_are_frozen(domain: ReadDomain) -> None:
    from ontary.model import EventRecord

    domain.append("yamato")
    record = domain.client().events(domain.shipped)[0]
    assert isinstance(record, EventRecord)
    assert isinstance(record.payload, domain.shipped)
    assert record.payload.carrier == "yamato"
    assert (record.event_type, record.about_type, record.about_id) == (
        "OrderShipped", "Order", "order-1",
    )
    assert record.ts == INSTANT
    assert record.invocation_id == "inv-1"
    assert record.redacted_fields == frozenset({"human_secret"})
    with pytest.raises(ValidationError) as caught:
        record.about_id = "rewritten"
    assert caught.value.errors()[0]["type"] == "frozen_instance"


def test_unfiltered_records_keep_event_payloads_and_emission_order(domain: ReadDomain) -> None:
    domain.append("first", "second")
    domain.append("packed", event_type="Packed", ts=INSTANT - timedelta(days=1))
    domain.append("last", ts=INSTANT + timedelta(days=1))
    records = domain.client().events()
    assert _carriers(records) == ["first", "second", "packed", "last"]
    assert [record.invocation_id for record in records] == ["inv-1", "inv-1", "inv-2", "inv-3"]
    assert all(isinstance(record.payload, Event) for record in records)
    assert records[0].payload.model_dump()["carrier"] == "first"


def test_event_type_filter_uses_the_registered_api_name(domain: ReadDomain) -> None:
    domain.append("shipped")
    domain.append("packed", event_type="Packed")
    records = domain.client().events(domain.packed)
    assert _carriers(records) == ["packed"]
    assert isinstance(records[0].payload, domain.packed)
    assert records[0].event_type == "Packed"


@pytest.mark.parametrize("as_object", [False, True], ids=["tuple", "object"])
def test_about_filter_matches_both_type_and_id(domain: ReadDomain, as_object: bool) -> None:
    domain.append("match", about_type="OtherOrder")
    domain.append("wrong-type")
    domain.append("wrong-id", about_type="OtherOrder", about_id="order-2")
    about = domain.other(id="order-1") if as_object else (domain.other, "order-1")
    assert _carriers(domain.client().events(about=about)) == ["match"]


def test_since_alone_is_inclusive_and_compares_instants(domain: ReadDomain) -> None:
    domain.append("before", ts=INSTANT - timedelta(microseconds=1))
    domain.append("at")
    domain.append("after", ts=INSTANT + timedelta(microseconds=1))
    offset = timezone(timedelta(hours=9))
    assert _carriers(domain.client().events(since=INSTANT.astimezone(offset))) == ["at", "after"]


def test_until_alone_is_exclusive(domain: ReadDomain) -> None:
    domain.append("before", ts=INSTANT - timedelta(microseconds=1))
    domain.append("at")
    domain.append("after", ts=INSTANT + timedelta(microseconds=1))
    assert _carriers(domain.client().events(until=INSTANT)) == ["before"]


def test_filters_combine_and_an_empty_range_returns_none(domain: ReadDomain) -> None:
    domain.append("match")
    domain.append("wrong-type", event_type="Packed")
    domain.append("wrong-subject", about_type="OtherOrder")
    domain.append("too-early", ts=INSTANT - timedelta(microseconds=1))
    domain.append("too-late", ts=INSTANT + timedelta(microseconds=1))
    client = domain.client()
    assert _carriers(client.events(
        domain.shipped, about=(domain.order, "order-1"), since=INSTANT,
        until=INSTANT + timedelta(microseconds=1),
    )) == ["match"]
    assert client.events(since=INSTANT, until=INSTANT) == []


@pytest.mark.parametrize("boundary", ["since", "until"])
def test_naive_boundaries_are_refused_before_reading_audit(
    domain: ReadDomain, boundary: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    naive = datetime(2026, 10, 4, 9)

    def no_read() -> None:
        pytest.fail("a naive boundary must be refused before reading audit")

    monkeypatch.setattr(domain.store, "audit_entries", no_read)
    with pytest.raises(ValidationFailed) as caught:
        domain.client().events(**{boundary: naive})
    assert caught.value.code == "CLOCK_NOT_TIMEZONE_AWARE"
    assert str(caught.value) == (
        "clock returned a naive datetime (datetime.datetime(2026, 10, 4, 9, 0)); an instant "
        "must be timezone-aware -- return e.g. datetime.now(timezone.utc)"
    )


@pytest.mark.parametrize("subclass", [False, True], ids=["undecorated", "inherited-stamp"])
def test_unregistered_event_class_is_refused(domain: ReadDomain, subclass: bool) -> None:
    base = domain.shipped if subclass else Event

    class Undeclared(base):
        pass

    with pytest.raises(ValidationFailed) as caught:
        domain.client().events(Undeclared)
    assert caught.value.code == "UNKNOWN_NAME"
    assert str(caught.value) == (
        "'Undeclared' is not decorated with @ontology.event(...) -- it has no registered api_name"
    )


@pytest.mark.parametrize("missing", ["name", "registry"])
def test_event_filter_requires_both_class_stamps(domain: ReadDomain, missing: str) -> None:
    class IncompleteStamp(Event):
        carrier: str

    if missing == "name":
        IncompleteStamp._ontary_registry = domain.ontology.registry
    else:
        IncompleteStamp._ontary_api_name = "OrderShipped"
    with pytest.raises(ValidationFailed) as caught:
        domain.client().events(IncompleteStamp)
    assert caught.value.code == "UNKNOWN_NAME"
    assert str(caught.value) == (
        "'IncompleteStamp' is not decorated with @ontology.event(...) -- it has no registered api_name"
    )


def test_event_class_from_another_ontology_is_refused(domain: ReadDomain) -> None:
    foreign = Ontology("foreign", scope_levels=["org"])

    @foreign.event(api_name="OrderShipped")
    class ForeignShipped(Event):
        carrier: str

    with pytest.raises(ValidationFailed) as caught:
        domain.client().events(ForeignShipped)
    assert caught.value.code == "UNKNOWN_NAME"
    assert str(caught.value) == (
        "'ForeignShipped' (api_name 'OrderShipped') was registered on a different Ontology "
        "-- it is not part of this client's ontology"
    )


def test_a_stamped_event_removed_from_the_registry_is_refused(domain: ReadDomain) -> None:
    domain.ontology.registry._event_types.pop("OrderShipped")
    with pytest.raises(ValidationFailed) as caught:
        domain.client().events(domain.shipped)
    assert caught.value.code == "UNKNOWN_NAME"
    assert str(caught.value) == "'OrderShipped' (api_name 'OrderShipped') is not registered on this client's ontology"


def test_event_filter_refuses_a_non_event_even_with_matching_stamps(domain: ReadDomain) -> None:
    class NotAnEvent(BaseModel):
        pass

    NotAnEvent._ontary_api_name = "OrderShipped"
    NotAnEvent._ontary_registry = domain.ontology.registry
    with pytest.raises(ValidationFailed) as caught:
        domain.client().events(NotAnEvent)
    assert caught.value.code == "UNKNOWN_NAME"
    assert str(caught.value) == "events event_type must be an Event subclass"


def test_event_filter_refuses_a_value_that_is_not_a_class(domain: ReadDomain) -> None:
    with pytest.raises(ValidationFailed) as caught:
        domain.client().events("OrderShipped")
    assert caught.value.code == "UNKNOWN_NAME"
    assert str(caught.value) == "events event_type must be an Event subclass"


def test_about_refuses_an_unregistered_subject_class(domain: ReadDomain) -> None:
    class Undeclared(OntologyObject):
        id: str

    with pytest.raises(ValidationFailed) as caught:
        domain.client().events(about=(Undeclared, "order-1"))
    assert caught.value.code == "UNKNOWN_NAME"
    assert str(caught.value) == (
        "'Undeclared' is not decorated with @ontology.object(...) -- it has no registered api_name"
    )


@pytest.mark.parametrize("as_object", [False, True], ids=["tuple", "object"])
def test_about_refuses_a_non_string_subject_id(domain: ReadDomain, as_object: bool) -> None:
    about = domain.order.model_construct(id=42) if as_object else (domain.order, 42)
    with pytest.raises(ValidationFailed) as caught:
        domain.client().events(about=about)
    assert caught.value.code == "INVALID_PARAMS"
    assert str(caught.value) == "events about must name a string id for Order"


@pytest.mark.parametrize("outcome", ["error", "denied"])
def test_non_ok_action_entries_never_contribute(domain: ReadDomain, outcome: str) -> None:
    domain.append("ok")
    domain.append("hidden", outcome=outcome)
    assert _carriers(domain.client().events()) == ["ok"]


def test_ok_function_entries_never_contribute(domain: ReadDomain) -> None:
    domain.append("action")
    domain.append("function", kind="function")
    assert _carriers(domain.client().events()) == ["action"]


def test_unregistered_stored_event_types_are_hidden_before_subject_reads(
    domain: ReadDomain, monkeypatch: pytest.MonkeyPatch,
) -> None:
    domain.append("hidden", event_type="Removed")

    def no_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail("an undeclared event must be hidden before its subject is read")

    monkeypatch.setattr(domain.store, "read_last", no_read)
    assert domain.client().events() == []


def test_event_without_a_subject_row_is_hidden(domain: ReadDomain) -> None:
    domain.append("hidden", about_id="missing")
    assert domain.client().events() == []


def test_subject_scope_is_enforced_independently_of_row_visibility(domain: ReadDomain) -> None:
    domain.append("inside")
    domain.append("outside", about_id="order-2")
    domain.ontology.definition.policy.row_visibility["Order"] = lambda *_: True
    assert _carriers(domain.client().events()) == ["inside"]


def test_unresolvable_subject_scope_denies_by_default(domain: ReadDomain) -> None:
    domain.append("hidden")
    domain.ontology.definition.policy.rules.pop("Order")
    assert domain.client().events() == []


@pytest.mark.parametrize("retired", [False, True], ids=["live", "retired"])
def test_row_visibility_uses_the_latest_raw_subject_payload(domain: ReadDomain, retired: bool) -> None:
    domain.append("history")
    policy = domain.ontology.definition.policy
    policy.row_visibility["Order"] = lambda store, actor, name, row: row["disclosed"] is True
    client = domain.client()
    assert _carriers(client.events()) == ["history"]
    domain.store.update("Order", "order-1", {"disclosed": False}, SOURCE)
    if retired:
        domain.store.retire_object("Order", "order-1")
    assert client.events() == []


def test_unscoped_subject_still_runs_row_visibility(domain: ReadDomain) -> None:
    domain.append("public", about_type="OtherOrder")
    client = domain.client(scope_id="unrelated")
    assert _carriers(client.events()) == ["public"]
    domain.ontology.definition.policy.row_visibility["OtherOrder"] = lambda *_: False
    assert client.events() == []


def test_retired_subject_keeps_its_last_scope(domain: ReadDomain) -> None:
    domain.append("history")
    domain.store.retire_object("Order", "order-1")
    assert domain.store.read_current("Order", "order-1") is None
    assert _carriers(domain.client().events()) == ["history"]
    assert domain.client(scope_id="org-2").events() == []


def test_rescoped_subject_moves_its_entire_history(domain: ReadDomain) -> None:
    domain.append("before-move")
    domain.store.update("Order", "order-1", {"org_id": "org-2"}, SOURCE)
    domain.append("after-move")
    assert domain.client().events() == []
    assert _carriers(domain.client(scope_id="org-2").events()) == ["before-move", "after-move"]


@pytest.mark.parametrize("filtered", [False, True], ids=["whole-log", "about-filter"])
def test_incoherent_subject_policy_is_refused_before_any_subject_row_is_read(
    domain: ReadDomain, filtered: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The earlier, coherent subject must not be read before the later refusal.
    domain.append("coherent", about_type="OtherOrder")
    domain.append("incoherent")
    policy = domain.ontology.definition.policy
    policy.unscoped_types.add("Order")

    def no_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail("scope coherence must be checked before any subject row read")

    monkeypatch.setattr(domain.store, "read_last", no_read)
    monkeypatch.setattr(domain.store, "read_current", no_read)
    about = (domain.order, "order-1") if filtered else None
    with pytest.raises(ValidationFailed) as caught:
        domain.client().events(about=about)
    assert caught.value.code == "SCOPE_POLICY_ERROR"
    assert str(caught.value) == (
        "ScopePolicy.unscoped_types['Order']: also declares ScopePolicy.rules['Order'] -- a type "
        "is either unscoped or scope-routed, never both. The unscoped listing wins at read time, "
        "so the rule is silently dead while still exempting its DirectProperty names from the "
        "supplied-value where= gate. Remove the type from unscoped_types, or drop its rules entry"
    )


def test_unrelated_incoherent_policy_does_not_block_event_type_selection(domain: ReadDomain) -> None:
    domain.append("public", about_type="OtherOrder")
    domain.ontology.registry.get_action_type("ShipOrder").emits = ["Packed"]
    domain.ontology.definition.policy.unscoped_types.add("Order")
    assert _carriers(domain.client().events(domain.shipped, about=(domain.other, "order-1"))) == ["public"]


def test_removed_event_declarations_do_not_trigger_subject_policy_checks(domain: ReadDomain) -> None:
    domain.append("removed")
    domain.ontology.registry.get_action_type("ShipOrder").emits = ["OrderShipped"]
    domain.ontology.registry._event_types.pop("OrderShipped")
    domain.ontology.definition.policy.unscoped_types.add("Order")
    assert domain.client().events() == []


@pytest.mark.parametrize("populated", [False, True], ids=["empty-log", "populated-log"])
@pytest.mark.parametrize("typed", [False, True], ids=["all-events", "selected-event"])
@pytest.mark.parametrize("window", [
    {},
    {"since": INSTANT},
    {"since": INSTANT + timedelta(seconds=1)},
    {"until": INSTANT + timedelta(seconds=1)},
    {"until": INSTANT},
    {"since": INSTANT, "until": INSTANT + timedelta(seconds=1)},
    {"since": INSTANT, "until": INSTANT},
], ids=["unbounded", "since-includes", "since-excludes", "until-includes", "until-excludes",
        "range-includes", "empty-range"])
def test_scope_coherence_verdict_is_independent_of_audit_contents_and_window(
    domain: ReadDomain, populated: bool, typed: bool, window: dict[str, datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if populated:
        domain.append("hidden-from-consumer")
    client = domain.client(scope_id="org-2")
    domain.ontology.definition.policy.unscoped_types.add("Order")

    def no_read() -> None:
        pytest.fail("declaration scope coherence must be checked before reading audit")

    monkeypatch.setattr(domain.store, "audit_entries", no_read)
    with pytest.raises(ValidationFailed) as caught:
        client.events(domain.shipped if typed else None, **window)
    assert caught.value.code == "SCOPE_POLICY_ERROR"
    assert str(caught.value) == (
        "ScopePolicy.unscoped_types['Order']: also declares ScopePolicy.rules['Order'] -- a type "
        "is either unscoped or scope-routed, never both. The unscoped listing wins at read time, "
        "so the rule is silently dead while still exempting its DirectProperty names from the "
        "supplied-value where= gate. Remove the type from unscoped_types, or drop its rules entry"
    )


def test_about_filter_does_not_skip_other_declared_subject_policy_checks(domain: ReadDomain) -> None:
    domain.ontology.definition.policy.unscoped_types.add("Order")
    with pytest.raises(ValidationFailed) as caught:
        domain.client().events(domain.shipped, about=(domain.other, "order-1"))
    assert caught.value.code == "SCOPE_POLICY_ERROR"
    assert str(caught.value) == (
        "ScopePolicy.unscoped_types['Order']: also declares ScopePolicy.rules['Order'] -- a type "
        "is either unscoped or scope-routed, never both. The unscoped listing wins at read time, "
        "so the rule is silently dead while still exempting its DirectProperty names from the "
        "supplied-value where= gate. Remove the type from unscoped_types, or drop its rules entry"
    )


@pytest.mark.parametrize("typed", [False, True], ids=["all-events", "selected-event"])
def test_all_declared_subject_types_are_checked_in_sorted_order_before_audit(
    domain: ReadDomain, typed: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ontary.query import GuardedQuery

    query = GuardedQuery(domain.store, domain.ontology.registry, domain.ontology.definition.policy)
    checked: list[str] = []
    original = query._require_coherent_scope

    def check(subject_type: str) -> Any:
        checked.append(subject_type)
        return original(subject_type)

    def audit() -> list[AuditEntry]:
        assert checked == ["Order", "OtherOrder"]
        return []

    monkeypatch.setattr(query, "_require_coherent_scope", check)
    monkeypatch.setattr(domain.store, "audit_entries", audit)
    assert query.visible_events(consumer(), event_type="OrderShipped" if typed else None) == []


@pytest.mark.parametrize("change", ["target", "emits"])
@pytest.mark.parametrize("typed", [False, True], ids=["all-events", "selected-event"])
def test_events_about_a_former_emitting_action_target_are_hidden_before_subject_reads(
    domain: ReadDomain, change: str, typed: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    domain.append("history")
    client = domain.client()
    event_type = domain.shipped if typed else None
    assert _carriers(client.events(event_type)) == ["history"]
    action = domain.ontology.registry.get_action_type("ShipOrder")
    if change == "target":
        action.target_type = "OtherOrder"
    else:
        action.emits = []

    def no_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail("a former emitting target must be hidden before its subject is read")

    monkeypatch.setattr(domain.store, "read_last", no_read)
    assert client.events(event_type) == []


def test_about_policy_is_checked_even_on_an_empty_log_before_reading_audit(
    domain: ReadDomain, monkeypatch: pytest.MonkeyPatch,
) -> None:
    domain.ontology.registry.get_action_type("ShipOrder").emits = []
    domain.ontology.definition.policy.unscoped_types.add("Order")

    def no_read() -> None:
        pytest.fail("the explicit subject type must be checked before reading audit")

    monkeypatch.setattr(domain.store, "audit_entries", no_read)
    with pytest.raises(ValidationFailed) as caught:
        domain.client().events(about=(domain.order, "missing"), since=INSTANT, until=INSTANT)
    assert caught.value.code == "SCOPE_POLICY_ERROR"
    assert str(caught.value) == (
        "ScopePolicy.unscoped_types['Order']: also declares ScopePolicy.rules['Order'] -- a type "
        "is either unscoped or scope-routed, never both. The unscoped listing wins at read time, "
        "so the rule is silently dead while still exempting its DirectProperty names from the "
        "supplied-value where= gate. Remove the type from unscoped_types, or drop its rules entry"
    )


@pytest.mark.parametrize("kind", ["human", "ai"])
@pytest.mark.parametrize("typed", [False, True], ids=["unfiltered", "typed"])
def test_payload_sensitivity_is_independent_of_subject_properties_and_keeps_audit_raw(
    domain: ReadDomain, kind: Literal["human", "ai"], typed: bool,
) -> None:
    domain.append("yamato", payload={"ai_secret": "human-value", "human_secret": "ai-value"})
    record = domain.client(kind=kind).events(domain.shipped if typed else None)[0]
    hidden = "human_secret" if kind == "human" else "ai_secret"
    visible = "ai_secret" if kind == "human" else "human_secret"
    assert getattr(record.payload, hidden) is None
    assert getattr(record.payload, visible) == ("human-value" if kind == "human" else "ai-value")
    assert record.redacted_fields == frozenset({hidden})
    assert domain.store.audit_entries()[0].events[0].payload == {
        "carrier": "yamato", "ai_secret": "human-value", "human_secret": "ai-value",
    }


@pytest.mark.parametrize("kind", ["human", "ai"])
def test_guarded_query_drops_hidden_payload_fields_before_hydration(
    domain: ReadDomain, kind: Literal["human", "ai"],
) -> None:
    from ontary.query import GuardedQuery

    domain.append("yamato", payload={"ai_secret": "human-value", "human_secret": "ai-value"})
    query = GuardedQuery(domain.store, domain.ontology.registry, domain.ontology.definition.policy)
    entry, event, hidden = query.visible_events(consumer(kind=kind))[0]
    hidden_name = "human_secret" if kind == "human" else "ai_secret"
    visible_name = "ai_secret" if kind == "human" else "human_secret"
    assert hidden == frozenset({hidden_name})
    assert hidden_name not in event.payload
    assert event.payload[visible_name] == ("human-value" if kind == "human" else "ai-value")
    assert entry.events[0].payload == domain.store.audit_entries()[0].events[0].payload == {
        "carrier": "yamato", "ai_secret": "human-value", "human_secret": "ai-value",
    }


def test_typed_payload_hydrates_storage_scalars_choices_and_structs(domain: ReadDomain) -> None:
    payload = domain.shipped(
        carrier="yamato", state=ShippingState.SHIPPED, at=INSTANT, day=INSTANT.date(),
        detail=ShipmentDetail(state=ShippingState.SHIPPED, at=INSTANT, day=INSTANT.date()),
    )
    domain.store.append_audit(AuditEntry(
        actor="operator", role="Operator", action="ShipOrder", target_type="Order", outcome="ok",
        ts=INSTANT, invocation_id="typed-inv", events=[EmittedEvent(
            event_type="OrderShipped", about_type="Order", about_id="order-1",
            payload=payload.model_dump(mode="json"),
        )],
    ))
    record = domain.client().events(domain.shipped)[0]
    assert record.payload.state is ShippingState.SHIPPED
    assert record.payload.at == INSTANT
    assert type(record.payload.at) is datetime
    assert record.payload.day == INSTANT.date()
    assert type(record.payload.day) is date
    assert isinstance(record.payload.detail, ShipmentDetail)
    assert record.payload.detail == payload.detail


def test_events_typing_narrows_filtered_payload_and_defaults_to_event(tmp_path: Path) -> None:
    sample = tmp_path / "events_read_typing.py"
    sample.write_text(textwrap.dedent("""\
        from typing import assert_type
        from ontary.client import OntologyClient
        from ontary.model import Event, EventRecord

        class OrderShipped(Event):
            carrier: str

        def check(client: OntologyClient, optional_type: type[OrderShipped] | None) -> None:
            assert_type(client.events(OrderShipped), list[EventRecord[OrderShipped]])
            assert_type(client.events(OrderShipped)[0].payload, OrderShipped)
            assert_type(client.events(), list[EventRecord[Event]])
            assert_type(client.events(None), list[EventRecord[Event]])
            assert_type(client.events(optional_type), list[EventRecord[OrderShipped]] | list[EventRecord[Event]])
        """))
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", str(sample)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_require_event_type_refuses_an_undeclared_name_with_its_own_code(
    domain: ReadDomain,
) -> None:
    with pytest.raises(ValidationFailed) as caught:
        domain.client()._require_event_type("Shiped")
    assert caught.value.code == "UNKNOWN_EVENT_TYPE"
    assert "Shiped" in str(caught.value)


@pytest.mark.parametrize("name", ["OrderShipped", "Packed"])
def test_require_event_type_accepts_a_declared_api_name(
    domain: ReadDomain, name: str,
) -> None:
    domain.client()._require_event_type(name)


def _positions(rows: list[Any]) -> list[tuple[int, int]]:
    return [row[0] for row in rows]


def test_positioned_events_index_the_audit_log_and_emission_order(domain: ReadDomain) -> None:
    domain.append("function", kind="function")
    domain.append("first", "second")
    domain.append("denied", outcome="denied")
    domain.append("third")
    rows = domain.client()._event_rows(None, about=None, since=None, until=None)
    assert _positions(rows) == [(1, 0), (1, 1), (3, 0)]
    assert [event.payload["carrier"] for _, _, event, _ in rows] == ["first", "second", "third"]
    assert _positions(rows) == sorted(set(_positions(rows)))  # strictly increasing


def test_positions_of_earlier_events_are_stable_as_the_log_grows(domain: ReadDomain) -> None:
    domain.append("first", "second")
    client = domain.client()
    before = client._event_rows(None, about=None, since=None, until=None)
    domain.append("hidden", about_id="order-2")
    domain.append("later")
    after = client._event_rows(None, about=None, since=None, until=None)
    assert after[: len(before)] == before
    assert _positions(after) == [(0, 0), (0, 1), (2, 0)]


@pytest.mark.parametrize("kind", ["human", "ai"])
def test_visible_events_is_the_positioned_read_without_positions(
    domain: ReadDomain, kind: Literal["human", "ai"],
) -> None:
    domain.append("first", "second", payload={"ai_secret": "a", "human_secret": "h"})
    domain.append("packed", event_type="Packed")
    domain.append("outside", about_id="order-2")
    domain.append("other", about_type="OtherOrder")
    query = domain.ontology.bind(domain.store).query
    actor = consumer(scope_level="org", scope_id="org-1", kind=kind)
    filters: list[dict[str, Any]] = [
        {},
        {"event_type": "OrderShipped"},
        {"about": ("OtherOrder", "order-1")},
        {"since": INSTANT, "until": INSTANT + timedelta(microseconds=1)},
    ]
    for kwargs in filters:
        positioned = query._positioned_visible_events(
            actor, event_type=kwargs.get("event_type"), about=kwargs.get("about"),
            since=kwargs.get("since"), until=kwargs.get("until"),
        )
        assert [row[1:] for row in positioned] == query.visible_events(actor, **kwargs)


def test_event_rows_filter_by_event_type_name(domain: ReadDomain) -> None:
    domain.append("shipped")
    domain.append("packed", event_type="Packed")
    rows = domain.client()._event_rows("Packed", about=None, since=None, until=None)
    assert [(pos, event.event_type, event.payload["carrier"]) for pos, _, event, _ in rows] == [
        ((1, 0), "Packed", "packed"),
    ]


def test_event_rows_filter_by_about_tuple(domain: ReadDomain) -> None:
    domain.append("match", about_type="OtherOrder")
    domain.append("wrong-type")
    domain.append("wrong-id", about_type="OtherOrder", about_id="order-2")
    rows = domain.client()._event_rows(None, about=("OtherOrder", "order-1"), since=None, until=None)
    assert [(pos, event.payload["carrier"]) for pos, _, event, _ in rows] == [((0, 0), "match")]


def test_event_rows_window_is_inclusive_then_exclusive(domain: ReadDomain) -> None:
    domain.append("before", ts=INSTANT - timedelta(microseconds=1))
    domain.append("at")
    domain.append("after", ts=INSTANT + timedelta(microseconds=1))
    rows = domain.client()._event_rows(
        None, about=None, since=INSTANT, until=INSTANT + timedelta(microseconds=1),
    )
    assert [(pos, entry.ts, event.payload["carrier"]) for pos, entry, event, _ in rows] == [
        ((1, 0), INSTANT, "at"),
    ]


def test_event_rows_remove_hidden_payload_keys_and_name_them(domain: ReadDomain) -> None:
    domain.append("yamato", payload={"ai_secret": "a", "human_secret": "h"})
    _, _, event, hidden = domain.client(kind="ai")._event_rows(
        "OrderShipped", about=None, since=None, until=None,
    )[0]
    assert hidden == frozenset({"ai_secret"})
    assert "ai_secret" not in event.payload
    assert event.payload["human_secret"] == "h"


def test_event_rows_refuse_an_undeclared_event_type(domain: ReadDomain) -> None:
    with pytest.raises(ValidationFailed) as caught:
        domain.client()._event_rows("Shiped", about=None, since=None, until=None)
    assert caught.value.code == "UNKNOWN_EVENT_TYPE"


def test_event_rows_refuse_an_undeclared_about_type(domain: ReadDomain) -> None:
    with pytest.raises(ValidationFailed) as caught:
        domain.client()._event_rows(None, about=("Ordr", "order-1"), since=None, until=None)
    assert caught.value.code == "UNKNOWN_OBJECT_TYPE"


@pytest.mark.parametrize(("event_type", "about_type", "expected"), [
    (None, None, ["Order", "OtherOrder"]),
    ("OrderShipped", None, ["Order", "OtherOrder"]),
    ("Packed", None, ["Order"]),
    ("Packed", "Order", ["Order"]),
    ("Packed", "OtherOrder", ["Order", "OtherOrder"]),
])
def test_event_subject_types_are_sorted_emitting_targets_plus_about_type(
    domain: ReadDomain, event_type: str | None, about_type: str | None, expected: list[str],
) -> None:
    assert domain.client()._event_subject_types(event_type, about_type) == expected


def test_event_subject_types_of_an_event_no_action_emits(domain: ReadDomain) -> None:
    domain.ontology.registry.get_action_type("ShipOrder").emits = ["OrderShipped"]
    client = domain.client()
    assert client._event_subject_types("Packed", None) == []
    assert client._event_subject_types("Packed", "OtherOrder") == ["OtherOrder"]


def test_event_scope_limited_is_true_for_a_scoped_subject_with_no_events(domain: ReadDomain) -> None:
    assert domain.store.audit_entries() == []
    assert domain.client()._event_scope_limited("Packed", None) is True
    assert domain.client()._event_scope_limited(None, None) is True


def test_event_scope_limited_is_false_for_an_unscoped_subject_without_row_policy(
    domain: ReadDomain,
) -> None:
    domain.ontology.registry.get_action_type("ShipOrder").emits = ["Packed"]
    client = domain.client()
    assert client._event_scope_limited("OrderShipped", None) is False
    assert client._event_scope_limited("OrderShipped", "Order") is True
    domain.ontology.definition.policy.row_visibility["OtherOrder"] = lambda *_: True
    assert client._event_scope_limited("OrderShipped", None) is True


def test_event_scope_limited_is_false_for_an_event_no_action_emits(domain: ReadDomain) -> None:
    domain.ontology.registry.get_action_type("ShipOrder").emits = ["OrderShipped"]
    assert domain.client()._event_scope_limited("Packed", None) is False
