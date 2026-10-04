"""The eager scenario core on InMemory, including rollback and determinism."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel

import ontary.testing
from ontary import (
    ActionContext,
    ActionParams,
    Ontology,
    OntologyObject,
    Sensitivity,
    Source,
    prop,
)
from ontary._scenario import SCENARIO_EPOCH
from ontary.errors import ValidationFailed
from ontary.testing import FixedClock, Scenario, SequentialIds, consumer, make_store, scenario

ontology = Ontology("scenario-core", scope_levels=["org"], min_n=1)


class Currency(Enum):
    JPY = "JPY"
    USD = "USD"


class Amount(BaseModel):
    value: int
    currency: Currency


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Record(OntologyObject):
    key: str = prop(primary_key=True)
    label: str
    amount: Amount


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Group(OntologyObject):
    id: str = prop(primary_key=True)


belongs_to = ontology.link("belongsTo", Record, Group, "MANY_TO_ONE", owned=True)
AMOUNT = Amount(value=10, currency=Currency.JPY)
OPERATOR = consumer(role="Operator")
VIEWER = consumer(role="Viewer")


class Create(ActionParams):
    pass


@ontology.action(Create, target=Record, roles=["Operator"])
def create(ctx: ActionContext, _params: Create) -> dict[str, str]:
    obj = ctx.create(Record, label="created", amount=AMOUNT)
    return {"key": obj.key}


class Mutant(ActionParams):
    pass


class CodedError(Exception):
    code = "MUTANT_REFUSED"


@ontology.action(Mutant, target=Record, roles=["Operator"])
def mutant(ctx: ActionContext, _params: Mutant) -> dict[str, Any]:
    obj = ctx.get(Record, "seed")
    assert obj is not None
    obj.label = "mutated"
    ctx.save(obj)
    created = ctx.create(Record, label="temporary", amount=AMOUNT)
    ctx.link(belongs_to, created, "g1")
    raise CodedError("refused after writing")


class Crash(ActionParams):
    pass


CRASH = RuntimeError("uncoded failure")


@ontology.action(Crash, target=Record, roles=["Operator"])
def crash(_ctx: ActionContext, _params: Crash) -> dict[str, Any]:
    raise CRASH


def record(key: str = "seed") -> Record:
    return Record(key=key, label="original", amount=AMOUNT)


def test_given_seeds_declared_typed_payload_and_links_eagerly() -> None:
    store = make_store(ontology)
    test = scenario(ontology, store=store)
    obj, group = record(), Group(id="g1")
    assert isinstance(test, Scenario)
    assert test.given(obj, group) is test
    stored = store.read_current("Record", "seed")
    assert stored is not None
    assert stored.payload == {
        "key": "seed", "label": "original", "amount": {"value": 10, "currency": "JPY"},
    }
    assert stored.lineage.source_system == "ontary.testing"
    assert test.given_link(belongs_to, obj, group) is test
    assert test.given_link(belongs_to, "seed", "g1") is test
    assert store.links_from("belongsTo", "seed") == ["g1"]
    assert test._snapshot()[1] == {("belongsTo", "seed", "g1")}


def test_given_refusals_name_type_custom_primary_key_and_code() -> None:
    test = scenario(ontology).given(record())
    with pytest.raises(AssertionError, match="given: Record 'seed': OBJECT_ALREADY_EXISTS") as exc:
        test.given(record())
    assert getattr(exc.value.__cause__, "code", None) == "OBJECT_ALREADY_EXISTS"

    class Unknown(OntologyObject):
        id: str

    with pytest.raises(AssertionError, match="given: Unknown 'u1': UNKNOWN_NAME") as exc:
        test.given(Unknown(id="u1"))
    assert getattr(exc.value.__cause__, "code", None) == "UNKNOWN_NAME"

    invalid = Record.model_construct(key="bad", amount=AMOUNT)
    with pytest.raises(AssertionError, match="given: Record 'bad': INVALID_RECORD"):
        test.given(invalid)


def test_given_link_refusal_names_link_endpoints_and_code() -> None:
    test = scenario(ontology).given(record())
    with pytest.raises(AssertionError, match="given: belongsTo 'seed' -> 'missing':") as exc:
        test.given_link(belongs_to, "seed", "missing")
    assert getattr(exc.value.__cause__, "code", None) in str(exc.value)


def test_given_and_given_link_are_refused_after_when_even_after_checked_error() -> None:
    test = scenario(ontology).when(Create(), by=VIEWER).then_error("PERMISSION_DENIED")
    with pytest.raises(AssertionError, match="before the first when"):
        test.given(record())
    with pytest.raises(AssertionError, match="before the first when"):
        test.given_link(belongs_to, "seed", "g1")


def test_when_is_eager_and_then_result_compares_equality() -> None:
    store = make_store(ontology)
    test = scenario(ontology, store=store)
    assert test.when(Create(), by=OPERATOR) is test
    assert store.read_current("Record", "id-2") is not None
    assert test.then_result({"key": "id-2"}) is test
    with pytest.raises(AssertionError, match="expected.*wrong.*got.*id-2"):
        test.then_result({"key": "wrong"})
    with pytest.raises(AssertionError, match="expected 'PERMISSION_DENIED'.*no error"):
        test.then_error("PERMISSION_DENIED")


def test_role_denial_uses_governed_path_and_can_be_checked_then_followed_by_success() -> None:
    store = make_store(ontology)
    test = scenario(ontology, store=store).given(record())
    before = test._snapshot()
    test.when(Create(), by=VIEWER)
    assert test._snapshot() == before
    assert len(store.audit_entries()) == 1
    with pytest.raises(AssertionError, match="expected 'OTHER'.*PERMISSION_DENIED"):
        test.then_error("OTHER")
    with pytest.raises(AssertionError, match="step 1 Create by Viewer.*PERMISSION_DENIED"):
        test.then_result({})
    assert test.then_error("PERMISSION_DENIED") is test
    test.when(Create(), by=OPERATOR).then_result({"key": "id-3"})


def test_unchecked_error_blocks_next_when_before_running() -> None:
    store = make_store(ontology)
    test = scenario(ontology, store=store).when(Create(), by=OPERATOR)
    test.when(Create(), by=VIEWER)
    with pytest.raises(AssertionError, match="step 2 Create by Viewer.*PERMISSION_DENIED"):
        test.when(Create(), by=OPERATOR)
    assert len(store.audit_entries()) == 2
    assert len(store.read_all("Record")) == 1


def test_mutant_writes_then_raises_and_store_rolls_objects_and_links_back() -> None:
    store = make_store(ontology)
    test = scenario(ontology, store=store).given(record(), Group(id="g1"))
    before = test._snapshot()
    test.when(Mutant(), by=OPERATOR).then_error("MUTANT_REFUSED")
    assert test._snapshot() == before
    assert len(store.read_all("Record")) == 1
    assert store.links_from("belongsTo", "id-2") == []
    assert len(store.audit_entries()) == 1


def test_snapshot_detects_committed_payload_inserts_updates_and_link_changes() -> None:
    store = make_store(ontology)
    test = scenario(ontology, store=store).given(record(), Group(id="g1"))
    before = test._snapshot()
    store.update("Record", "seed", {"label": "committed"}, Source(source_system="test"))
    after_update = test._snapshot()
    assert after_update != before
    assert before[0][("Record", "seed")]["label"] == "original"
    test.when(Create(), by=OPERATOR)
    after_insert = test._snapshot()
    assert after_insert != after_update
    store.create_link("belongsTo", "seed", "g1")
    assert test._snapshot() != after_insert
    store.close_link("belongsTo", "seed", "g1")
    assert test._snapshot() == after_insert


@pytest.mark.parametrize("change", ["object", "link"])
def test_then_error_names_committed_state_changes(change: str) -> None:
    store = make_store(ontology)
    test = scenario(ontology, store=store).given(record(), Group(id="g1"))
    test.when(Create(), by=VIEWER)
    if change == "object":
        store.update("Record", "seed", {"label": "leaked"}, Source(source_system="test"))
        message = "state changed.*object Record 'seed'.*leaked"
    else:
        store.create_link("belongsTo", "seed", "g1")
        message = "state changed.*link added.*belongsTo.*seed.*g1"
    with pytest.raises(AssertionError, match=message):
        test.then_error("PERMISSION_DENIED")
    # A failed state check must not mark the error as checked.
    with pytest.raises(AssertionError, match="PERMISSION_DENIED"):
        test.when(Create(), by=OPERATOR)


def test_default_environment_binds_once_before_seed_and_is_repeatable() -> None:
    def run_once() -> tuple[Any, Any]:
        store = make_store(ontology)
        with patch.object(ontology, "bind", wraps=ontology.bind) as bind:
            test = scenario(ontology, store=store)
            bind.assert_called_once()
            test.given(record(), Group(id="g1")).given_link(belongs_to, "seed", "g1")
            test.when(Create(), by=OPERATOR).then_result({"key": "id-2"})
            test.when(Create(), by=OPERATOR).then_result({"key": "id-4"})
            bind.assert_called_once()
        rows = [row for api in ontology.registry.object_types for row in store.read_all(api)]
        assert {row.lineage.valid_from for row in rows} == {
            SCENARIO_EPOCH.isoformat(timespec="microseconds")
        }
        assert {entry.ts for entry in store.audit_entries()} == {SCENARIO_EPOCH}
        return rows, store.audit_entries()

    assert run_once() == run_once()
    first, second = scenario(ontology), scenario(ontology)
    first.when(Create(), by=OPERATOR).then_result({"key": "id-2"})
    second.when(Create(), by=OPERATOR).then_result({"key": "id-2"})
    assert first._snapshot() == second._snapshot()


def test_environment_overrides_are_used() -> None:
    store = make_store(ontology)
    instant = datetime(2030, 1, 1, tzinfo=timezone.utc)
    test = scenario(ontology, store=store, clock=FixedClock(instant),
                    id_factory=SequentialIds("custom"), capabilities={})
    test.given(record()).when(Create(), by=OPERATOR).then_result({"key": "custom-2"})
    assert {row.lineage.valid_from for row in store.read_all("Record")} == {
        instant.isoformat(timespec="microseconds")
    }


def test_uncoded_exception_propagates_unchanged() -> None:
    with pytest.raises(RuntimeError) as exc:
        scenario(ontology).when(Crash(), by=OPERATOR)
    assert exc.value is CRASH


def test_result_and_error_before_when_explain_missing_outcome() -> None:
    test = scenario(ontology).given(record())
    with pytest.raises(AssertionError, match="no successful when"):
        test.then_result({})
    with pytest.raises(AssertionError, match="expected 'EXPECTED'.*no error"):
        test.then_error("EXPECTED")


def test_testing_docstring_documents_private_module_exception() -> None:
    assert "private scenario" in (ontary.testing.__doc__ or "")


@ontology.object(layer="L0", scope="unscoped", owned=True)
class TypedState(OntologyObject):
    id: str = prop(primary_key=True)
    status: Currency
    amount: Amount


@ontology.object(layer="L0", scope="unscoped", owned=True)
class PrivateState(OntologyObject):
    id: str = prop(primary_key=True)
    secret: str | None = prop(default=None, sensitivity=Sensitivity(human_visible=False))


def test_then_compares_only_named_fields_after_typed_hydration() -> None:
    state = TypedState(id="typed-1", status=Currency.JPY, amount=AMOUNT)
    test = scenario(ontology).given(state)

    # The direct Enum and the struct (including its nested Enum) are restored
    # to their declared types before equality is checked.
    assert test.then(TypedState, "typed-1", status=Currency.JPY, amount=AMOUNT) is test

    # A field the test did not name is deliberately ignored.
    test._store.update(
        "TypedState", "typed-1", {"status": "USD"}, Source(source_system="test")
    )
    assert test.then(TypedState, "typed-1", amount=AMOUNT) is test


def test_then_unknown_field_raises_validation_failed_invalid_record() -> None:
    test = scenario(ontology).given(TypedState(id="typed-1", status=Currency.JPY, amount=AMOUNT))

    with pytest.raises(ValidationFailed) as exc:
        test.then(TypedState, "typed-1", typo="value")

    assert exc.value.code == "INVALID_RECORD"


def test_then_compares_sensitive_property_from_unredacted_stored_truth() -> None:
    test = scenario(ontology).given(PrivateState(id="private-1", secret="classified"))

    human_read = test._runtime.for_consumer(VIEWER).get(PrivateState, "private-1")
    assert human_read is not None
    assert human_read.secret is None
    assert "secret" in human_read.redacted_fields
    assert test.then(PrivateState, "private-1", secret="classified") is test


def test_then_golden_mismatch_message_is_exact() -> None:
    message_ontology = Ontology("scenario-message", scope_levels=["org"], min_n=1)

    @message_ontology.object(layer="L0", scope="unscoped", owned=True)
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        escalated: bool = False

    class EscalateTicket(ActionParams):
        pass

    @message_ontology.action(EscalateTicket, target=Ticket, roles=["Viewer"])
    def do_not_escalate(_ctx: ActionContext, _params: EscalateTicket) -> dict[str, Any]:
        return {}

    test = scenario(message_ontology).given(Ticket(id="t-1", escalated=False))
    test.when(EscalateTicket(), by=VIEWER)

    with pytest.raises(AssertionError) as exc:
        test.then(Ticket, "t-1", escalated=True)

    assert str(exc.value) == (
        "then: Ticket 't-1' does not match after step 1 (EscalateTicket by Viewer):\n"
        "  escalated: expected True, got False"
    )


def test_then_missing_row_and_then_after_error_messages_are_exact() -> None:
    test = scenario(ontology).given(record())
    with pytest.raises(AssertionError) as missing:
        test.then(Record, "missing", label="original")
    assert str(missing.value) == "then: no current Record 'missing'"

    test.when(Create(), by=VIEWER)
    with pytest.raises(AssertionError) as after_error:
        test.then(Record, "seed", label="original")
    assert str(after_error.value) == (
        "then: step 1 Create by Viewer failed with PERMISSION_DENIED"
    )


def test_then_error_after_ok_message_is_exact() -> None:
    test = scenario(ontology).when(Create(), by=OPERATOR)

    with pytest.raises(AssertionError) as exc:
        test.then_error("PERMISSION_DENIED")

    assert str(exc.value) == "then_error: expected 'PERMISSION_DENIED', got no error"


def test_seeded_state_can_check_object_and_link_presence_and_absence() -> None:
    test = scenario(ontology).given(record(), Group(id="g1"))
    test.given_link(belongs_to, "seed", "g1")

    assert test.then(Record, "seed", label="original") is test
    assert test.then_absent(Record, "missing") is test
    assert test.then_link(belongs_to, "seed", "g1") is test
    assert test.then_no_link(belongs_to, "seed", "missing") is test


def test_state_presence_checks_fail_when_the_expected_state_is_missing_or_present() -> None:
    test = scenario(ontology).given(record(), Group(id="g1"))
    test.given_link(belongs_to, "seed", "g1")

    with pytest.raises(AssertionError, match="then_absent: current Record 'seed' exists"):
        test.then_absent(Record, "seed")
    with pytest.raises(AssertionError, match="then_link: expected belongsTo link 'seed' -> 'missing' to exist"):
        test.then_link(belongs_to, "seed", "missing")
    with pytest.raises(AssertionError, match="then_no_link: unexpected belongsTo link 'seed' -> 'g1'"):
        test.then_no_link(belongs_to, "seed", "g1")


@pytest.mark.parametrize("by", [OPERATOR, VIEWER])
def test_absence_and_link_checks_work_after_ok_and_error(by: Any) -> None:
    test = scenario(ontology).given(record(), Group(id="g1"))
    test.given_link(belongs_to, "seed", "g1").when(Create(), by=by)

    assert test.then_absent(Record, "missing") is test
    assert test.then_link(belongs_to, "seed", "g1") is test
    assert test.then_no_link(belongs_to, "seed", "missing") is test


@pytest.mark.parametrize("check", ["then", "then_absent", "then_link", "then_no_link"])
def test_state_checks_do_not_mark_an_error_as_checked(check: str) -> None:
    test = scenario(ontology).given(record(), Group(id="g1"))
    test.given_link(belongs_to, "seed", "g1").when(Create(), by=VIEWER)

    if check == "then":
        with pytest.raises(AssertionError, match="failed with PERMISSION_DENIED"):
            test.then(Record, "seed", label="original")
    elif check == "then_absent":
        test.then_absent(Record, "missing")
    elif check == "then_link":
        test.then_link(belongs_to, "seed", "g1")
    else:
        test.then_no_link(belongs_to, "seed", "missing")

    assert getattr(test._outcome, "checked", None) is False
    with pytest.raises(AssertionError, match="step 1 Create by Viewer failed with PERMISSION_DENIED"):
        test.when(Create(), by=OPERATOR)
