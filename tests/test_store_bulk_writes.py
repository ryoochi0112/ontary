"""Bulk object writes share single-write rules, retain history, and bound reads."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import wraps

import pytest
from _history import history_rows
from conftest import raises_code
from test_store_conformance import (
    STORE_FACTORIES,
    StoreFactory,
    _datetime_ontology,
    _note_registry,
    _order_registry,
    build_authority_registry,
    build_registry,
)

import ontary.store as store_pkg
from ontary import Ontology, OntologyObject, prop
from ontary.audit import WriteRecord
from ontary.errors import PreconditionFailed, ValidationFailed
from ontary.meta import RuleDef
from ontary.store import Source
from ontary.store._core import StoreCore

_SOURCE = Source(
    source_system="bulk-tests", source_id="original", extracted_at="2026-01-01T00:00:00Z"
)
_NEW_SOURCE = Source(
    source_system="next-extraction", source_id="new", extracted_at="2026-01-02T00:00:00Z"
)

# Spec §6 counts calls to storage steps, including defensive/single-row reads.
_READ_STEPS = (
    "_current_row",
    "_last_row",
    "_live_object_rows",
    "_current_rows_many",
    "_link_ids_from",
    "_link_ids_to",
    "_link_ids_from_many",
    "_link_ids_to_many",
)
_WRITE_STEPS = ("_insert_object_row", "_close_object_rows", "_insert_link_row")


@contextmanager
def _count_steps(store: StoreCore) -> Iterator[Counter[str]]:
    counts: Counter[str] = Counter()

    def counted(original, name):
        @wraps(original)
        def call(*args, **kwargs):
            counts[name] += 1
            return original(*args, **kwargs)

        return call

    with pytest.MonkeyPatch.context() as patch:
        for name in (*_READ_STEPS, *_WRITE_STEPS):
            patch.setattr(store, name, counted(getattr(store, name), name))
        yield counts


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return self.value


class _ChoiceId(Enum):
    A = "a"
    B = "b"


def _choice_pk_registry():
    ontology = Ontology("choice-pk-bulk", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped")
    class Item(OntologyObject):
        id: str = prop(primary_key=True, choices=["a", "b"])
        name: str

    ontology.validate()
    return ontology.registry


@pytest.fixture(params=STORE_FACTORIES, ids=lambda f: f.__name__.removeprefix("_make_"))
def store_factory(request: pytest.FixtureRequest) -> StoreFactory:
    return request.param


@pytest.fixture
def store(store_factory: StoreFactory) -> StoreCore:
    result = store_factory(build_registry())
    assert isinstance(result, StoreCore)
    return result


def test_row_outcome_is_a_frozen_internal_dataclass() -> None:
    from ontary.store.values import RowOutcome

    assert is_dataclass(RowOutcome)
    assert [field.name for field in fields(RowOutcome)] == ["status", "id", "error"]
    outcome = RowOutcome(status="inserted", id="a", error=None)
    with pytest.raises(FrozenInstanceError):
        outcome.status = "updated"
    assert not hasattr(store_pkg, "RowOutcome")


def test_bulk_outcomes_preserve_input_order_and_refusals_leave_no_history(store) -> None:
    from ontary.store.values import RowOutcome

    store.insert("Team", {"id": "same", "name": "same"}, _SOURCE)
    store.insert("Team", {"id": "change", "name": "old"}, _SOURCE)
    outcomes = store.upsert_objects(
        "Team",
        [
            {"id": "new", "name": "new"},
            {"id": "same", "name": "same"},
            {"id": "bad"},
            {"id": "change", "name": "changed"},
            {"id": "last", "name": "last"},
        ],
        _NEW_SOURCE,
        refusals=frozenset({"INVALID_RECORD"}),
    )
    assert [outcome.status for outcome in outcomes] == [
        "inserted", "unchanged", "refused", "updated", "inserted"
    ]
    assert [outcome.id for outcome in outcomes] == ["new", "same", "bad", "change", "last"]
    assert all(isinstance(outcome, RowOutcome) for outcome in outcomes)
    assert all(outcome.error is None for outcome in outcomes if outcome.status != "refused")
    assert isinstance(outcomes[2].error, ValidationFailed)
    assert outcomes[2].error.code == "INVALID_RECORD"
    assert store.read_current("Team", "bad") is None
    assert history_rows(store, "Team", "bad") == 0
    assert store.read_current("Team", "change").payload["name"] == "changed"
    assert [row.payload["id"] for row in store.read_all("Team")] == [
        "same", "new", "change", "last"
    ]


@pytest.mark.parametrize("across_chunks", [False, True])
def test_repeated_primary_key_sees_earlier_writes_and_merges_them(store, across_chunks) -> None:
    from ontary.store._core import INGEST_BATCH

    records = [{"id": "repeat", "name": "first", "extra": {"retained": True}}]
    if across_chunks:
        records.extend({"id": f"filler-{i}", "name": "filler"} for i in range(INGEST_BATCH - 1))
    records.extend([
        {"id": "repeat", "name": "second"},
        {"id": "repeat", "name": "second"},
    ])
    outcomes = store.upsert_objects("Team", records, _SOURCE)
    assert outcomes[0].status == "inserted"
    assert [outcome.status for outcome in outcomes[-2:]] == ["updated", "unchanged"]
    assert all(outcome.id == "repeat" for outcome in (outcomes[0], *outcomes[-2:]))
    assert store.read_current("Team", "repeat").payload == {
        "id": "repeat", "name": "second", "extra": {"retained": True}
    }
    assert history_rows(store, "Team", "repeat") == 2


def test_owned_default_is_insert_only_and_action_written_value_survives(store_factory) -> None:
    store = store_factory(build_authority_registry())
    record = {"id": "company", "name": "Acme"}
    assert store.upsert_objects("Company", [record], _SOURCE)[0].status == "inserted"
    assert store.read_current("Company", "company").payload["tier"] == "unranked"
    with store.capture_action_writes():
        store.update("Company", "company", {"tier": "gold"}, Source(source_system="action"))
    action_row = store.read_current("Company", "company")
    assert store.upsert_objects("Company", [record], _NEW_SOURCE)[0].status == "unchanged"
    assert store.read_current("Company", "company") == action_row
    assert history_rows(store, "Company", "company") == 2
    assert store.upsert_objects(
        "Company", [{"id": "company", "name": "Acme Ltd"}], _NEW_SOURCE
    )[0].status == "updated"
    assert store.read_current("Company", "company").payload["tier"] == "gold"
    assert record == {"id": "company", "name": "Acme"}


def test_owned_default_added_later_does_not_change_existing_row(store_factory) -> None:
    registry = build_authority_registry()
    obj_def = registry.get_object_type("Company")
    obj_def.owned = False
    store = store_factory(registry)
    record = {"id": "company", "name": "Acme"}
    store.upsert_objects("Company", [record], _SOURCE)
    original = store.read_current("Company", "company")
    obj_def.owned = {"tier": "unranked"}
    assert store.upsert_objects("Company", [record], _NEW_SOURCE)[0].status == "unchanged"
    assert store.read_current("Company", "company") == original
    assert "tier" not in original.payload
    assert history_rows(store, "Company", "company") == 1


def test_datetime_spelling_change_writes_a_version_even_for_the_same_instant(store_factory) -> None:
    ontology, _ = _datetime_ontology()
    store = store_factory(ontology.registry)
    utc = "2026-01-01T00:00:00Z"
    offset = "2026-01-01T09:00:00+09:00"
    assert datetime.fromisoformat(utc) == datetime.fromisoformat(offset)
    store.upsert_objects("Order", [{"id": "o", "placed_at": utc}], _SOURCE)
    assert store.upsert_objects(
        "Order", [{"id": "o", "placed_at": offset}], _NEW_SOURCE
    )[0].status == "updated"
    assert store.read_current("Order", "o").payload["placed_at"] == offset
    assert history_rows(store, "Order", "o") == 2


def test_key_order_only_is_unchanged_but_integer_vs_float_is_a_change(store_factory) -> None:
    store = store_factory(_note_registry())
    first = {"id": "n", "body": {"a": 1, "z": {"α": "日本語", "b": 2}}}
    reordered = {"body": {"z": {"b": 2, "α": "日本語"}, "a": 1}, "id": "n"}
    store.upsert_objects("Note", [first], _SOURCE)
    assert store.upsert_objects("Note", [reordered], _NEW_SOURCE)[0].status == "unchanged"
    assert history_rows(store, "Note", "n") == 1
    changed = {"id": "n", "body": {"a": 1.0, "z": {"α": "日本語", "b": 2}}}
    assert store.upsert_objects("Note", [changed], _NEW_SOURCE)[0].status == "updated"
    assert type(store.read_current("Note", "n").payload["body"]["a"]) is float
    assert history_rows(store, "Note", "n") == 2


def test_identical_row_keeps_lineage_history_and_spends_no_clock_or_write(store) -> None:
    clock = _Clock()
    store.bind_clock(clock)
    record = {"id": "a", "name": "original"}
    store.upsert_objects("Team", [record], _SOURCE)
    original = store.read_current("Team", "a")
    assert clock.reads == 1
    clock.value += timedelta(days=1)
    with _count_steps(store) as counts:
        outcomes = store.upsert_objects("Team", [record], _NEW_SOURCE)
    assert [(outcome.status, outcome.id) for outcome in outcomes] == [("unchanged", "a")]
    assert clock.reads == 1
    assert sum(counts[name] for name in _WRITE_STEPS) == 0
    assert store.read_current("Team", "a") == original
    assert original.lineage.valid_from == "2026-01-01T00:00:00.000000+00:00"
    assert original.lineage.source_system == _SOURCE.source_system
    assert original.lineage.source_id == _SOURCE.source_id
    assert original.lineage.extracted_at == _SOURCE.extracted_at
    assert history_rows(store, "Team", "a") == 1
    assert store.upsert_objects(
        "Team", [{"id": "a", "name": "changed"}], _NEW_SOURCE
    )[0].status == "updated"
    changed = store.read_current("Team", "a")
    assert clock.reads == 2
    assert changed.lineage.valid_from == "2026-01-02T00:00:00.000000+00:00"
    assert changed.lineage.source_system == _NEW_SOURCE.source_system
    assert history_rows(store, "Team", "a") == 2


def test_identical_row_still_checks_new_rules_before_skipping(store_factory) -> None:
    registry = build_registry()
    store = store_factory(registry)
    record = {"id": "a", "name": "now invalid"}
    store.upsert_objects("Team", [record], _SOURCE)
    original = store.read_current("Team", "a")
    registry.get_object_type("Team").rules = (
        RuleDef(
            name="valid-name",
            message="name must be valid",
            check=lambda row: row["name"] == "valid",
        ),
    )
    clock = _Clock()
    store.bind_clock(clock)
    outcomes = store.upsert_objects(
        "Team", [record], _NEW_SOURCE, refusals=frozenset({"RULE_VIOLATED"})
    )
    assert outcomes[0].status == "refused"
    assert outcomes[0].error.code == "RULE_VIOLATED"
    assert clock.reads == 0
    assert store.read_current("Team", "a") == original
    assert history_rows(store, "Team", "a") == 1


def test_rule_and_transition_refusals_do_not_write_and_later_rows_commit(store_factory) -> None:
    registry = _order_registry(rules=(
        RuleDef(name="not-bad", message="bad id refused", check=lambda row: row["id"] != "bad"),
    ))
    store = store_factory(registry)
    store.insert("Order", {"id": "existing", "status": "pending"}, _SOURCE)
    original = store.read_current("Order", "existing")
    clock = _Clock()
    clock.value = datetime(2099, 1, 1, tzinfo=timezone.utc)
    store.bind_clock(clock)
    outcomes = store.upsert_objects(
        "Order",
        [
            {"id": "bad", "status": "pending"},
            {"id": "existing", "status": "shipped"},
            {"id": "valid", "status": "pending"},
        ],
        _NEW_SOURCE,
        refusals=frozenset({"RULE_VIOLATED", "TRANSITION_NOT_ALLOWED"}),
    )
    assert [outcome.status for outcome in outcomes] == ["refused", "refused", "inserted"]
    assert [outcome.error.code for outcome in outcomes[:2]] == [
        "RULE_VIOLATED", "TRANSITION_NOT_ALLOWED"
    ]
    assert clock.reads == 1
    assert history_rows(store, "Order", "bad") == 0
    assert store.read_current("Order", "existing") == original
    assert history_rows(store, "Order", "existing") == 1
    assert store.read_current("Order", "valid").payload["status"] == "pending"


@pytest.mark.parametrize(
    "error", [RuntimeError("sixth write failed"), KeyboardInterrupt("interrupted")]
)
def test_sixth_storage_write_failure_rolls_back_and_propagates_original(
    store, monkeypatch, error
) -> None:
    original = store._insert_object_row
    attempts = 0

    def fail_sixth(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 6:
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_insert_object_row", fail_sixth)
    with pytest.raises(type(error)) as caught:
        store.upsert_objects("Team", [{"id": str(i), "name": str(i)} for i in range(10)], _SOURCE)
    assert caught.value is error
    assert attempts == 6
    assert not store.in_transaction
    for i in range(10):
        assert store.read_current("Team", str(i)) is None
        assert history_rows(store, "Team", str(i)) == 0


def test_unlisted_coded_error_rolls_back_prior_insert_and_update(store) -> None:
    store.insert("Team", {"id": "existing", "name": "original"}, _SOURCE)
    original = store.read_current("Team", "existing")
    with raises_code(ValidationFailed, "INVALID_RECORD"):
        store.upsert_objects(
            "Team",
            [{"id": "new", "name": "new"}, {"id": "existing", "name": "changed"}, {"id": "bad"}],
            _NEW_SOURCE,
            refusals=frozenset({"RULE_VIOLATED"}),
        )
    assert store.read_current("Team", "existing") == original
    assert history_rows(store, "Team", "existing") == 1
    assert store.read_current("Team", "new") is None
    assert history_rows(store, "Team", "new") == 0
    assert history_rows(store, "Team", "bad") == 0


@pytest.mark.parametrize("existing", [False, True])
def test_unencodable_bulk_payload_rolls_back_before_spending_its_tick(
    store_factory, existing
) -> None:
    store = store_factory(_note_registry())
    if existing:
        store.insert("Note", {"id": "bad", "body": {"original": True}}, _SOURCE)
    original = store.read_current("Note", "bad")
    clock = _Clock()
    clock.value = datetime(2099, 1, 1, tzinfo=timezone.utc)
    store.bind_clock(clock)
    with pytest.raises(TypeError):
        store.upsert_objects(
            "Note", [{"id": "ok", "body": {}}, {"id": "bad", "body": {"k": object()}}], _SOURCE
        )
    assert clock.reads == 1
    assert store.read_current("Note", "ok") is None
    assert store.read_current("Note", "bad") == original
    assert history_rows(store, "Note", "bad") == int(existing)


def test_clock_regression_rolls_back_a_prior_bulk_insert(store) -> None:
    clock = _Clock()
    store.bind_clock(clock)
    store.insert("Team", {"id": "existing", "name": "original"}, _SOURCE)
    original = store.read_current("Team", "existing")
    clock.value -= timedelta(days=1)
    with raises_code(PreconditionFailed, "CLOCK_REGRESSION"):
        store.upsert_objects(
            "Team",
            [{"id": "new", "name": "new"}, {"id": "existing", "name": "changed"}],
            _NEW_SOURCE,
        )
    assert store.read_current("Team", "new") is None
    assert store.read_current("Team", "existing") == original
    assert history_rows(store, "Team", "existing") == 1


@pytest.mark.parametrize("bulk", [False, True])
def test_update_checks_not_before_on_every_live_row(store, bulk) -> None:
    if isinstance(store, (store_pkg.ObjectStore, store_pkg.PostgresStore)):
        # Defensive handling of legacy duplicate rows, in this disposable store.
        store._conn.execute("DROP INDEX idx_objects_live_id")
        store._conn.commit()
    clock = _Clock()
    store.bind_clock(clock)
    record = {"id": "a", "name": "first"}
    store.insert("Team", record, _SOURCE)
    with store.transaction():
        store._insert_object_row(
            "Team", "a", json.dumps({"id": "a", "name": "second"}),
            "2026-01-02T00:00:00.000000+00:00", _SOURCE,
        )
    original = store._live_object_rows("Team", "a")
    assert len(original) == 2
    with raises_code(PreconditionFailed, "CLOCK_REGRESSION"):
        if bulk:
            store.upsert_objects("Team", [{"id": "a", "name": "changed"}], _NEW_SOURCE)
        else:
            store.update("Team", "a", {"name": "changed"}, _NEW_SOURCE)
    assert store._live_object_rows("Team", "a") == original
    assert history_rows(store, "Team", "a") == 2


def test_thousand_new_then_identical_records_use_two_reads_and_identical_writes_nothing(
    store,
) -> None:
    from ontary.store._core import INGEST_BATCH
    from ontary.store.values import DEFAULT_BATCH

    assert INGEST_BATCH == DEFAULT_BATCH == 500
    records = [{"id": str(i), "name": str(i)} for i in range(1000)]
    clock = _Clock()
    store.bind_clock(clock)
    with _count_steps(store) as first:
        inserted = store.upsert_objects("Team", records, _SOURCE)
    assert len(inserted) == 1000
    assert all(outcome.status == "inserted" for outcome in inserted)
    assert sum(first[name] for name in _READ_STEPS) <= 2, first
    assert first["_current_rows_many"] == 2
    assert first["_insert_object_row"] == 1000
    assert clock.reads == 1000
    with _count_steps(store) as second:
        unchanged = store.upsert_objects("Team", records, _NEW_SOURCE)
    assert [(outcome.status, outcome.id) for outcome in unchanged] == [
        ("unchanged", record["id"]) for record in records
    ]
    assert sum(second[name] for name in _READ_STEPS) <= 2, second
    assert second["_current_rows_many"] == 2
    assert sum(second[name] for name in _WRITE_STEPS) == 0, second
    assert clock.reads == 1000


def test_missing_ids_are_remembered_across_chunks_even_when_refused(store, monkeypatch) -> None:
    from ontary.store._core import INGEST_BATCH

    original = store._current_rows_many
    batches = []

    def note_batch(obj_type, ids):
        batches.append(ids)
        return original(obj_type, ids)

    monkeypatch.setattr(store, "_current_rows_many", note_batch)
    outcomes = store.upsert_objects(
        "Team", [{"id": "bad"}] * (INGEST_BATCH + 1), _SOURCE,
        refusals=frozenset({"INVALID_RECORD"}),
    )
    assert len(outcomes) == INGEST_BATCH + 1
    assert all(outcome.status == "refused" for outcome in outcomes)
    assert batches == [["bad"]]
    assert history_rows(store, "Team", "bad") == 0


def test_direct_bulk_caller_without_primary_key_mints_ids_or_refuses_shape(store) -> None:
    records = [{"name": "minted"}, {}]
    outcomes = store.upsert_objects(
        "Team", records, _SOURCE, refusals=frozenset({"INVALID_RECORD"})
    )
    assert outcomes[0].status == "inserted"
    assert str(uuid.UUID(outcomes[0].id)) == outcomes[0].id
    assert store.read_current("Team", outcomes[0].id).payload["name"] == "minted"
    assert outcomes[1].status == "refused"
    assert outcomes[1].id is None
    assert outcomes[1].error.code == "INVALID_RECORD"
    assert records == [{"name": "minted"}, {}]


def test_bulk_ids_are_canonical_and_invalid_non_string_ids_are_refused(store) -> None:
    class Id(str, Enum):
        A = "a"

    outcomes = store.upsert_objects(
        "Team", [{"id": Id.A, "name": "one"}, {"id": "a", "name": "one"}, {"id": 7, "name": "bad"}],
        _SOURCE, refusals=frozenset({"INVALID_RECORD"}),
    )
    assert [(outcome.status, outcome.id) for outcome in outcomes] == [
        ("inserted", "a"), ("unchanged", "a"), ("refused", "7")
    ]
    assert all(type(outcome.id) is str for outcome in outcomes)
    assert store.read_current("Team", "a").payload == {"id": "a", "name": "one"}
    assert history_rows(store, "Team", "a") == 1
    assert history_rows(store, "Team", "7") == 0


@pytest.mark.parametrize("changed", [False, True])
def test_bulk_plain_enum_primary_key_finds_existing_row(store_factory, changed) -> None:
    store = store_factory(_choice_pk_registry())
    store.insert("Item", {"id": "a", "name": "original"}, _SOURCE)
    record = {"id": _ChoiceId.A, "name": "changed" if changed else "original"}
    with _count_steps(store) as counts:
        outcomes = store.upsert_objects("Item", [record], _NEW_SOURCE)
    assert [(outcome.status, outcome.id) for outcome in outcomes] == [
        ("updated" if changed else "unchanged", "a")
    ]
    assert outcomes[0].error is None
    assert store.read_current("Item", "a").payload == {"id": "a", "name": record["name"]}
    assert store.read_current("Item", str(_ChoiceId.A)) is None
    assert history_rows(store, "Item", "a") == 1 + int(changed)
    assert counts["_current_rows_many"] == 1
    assert counts["_current_row"] == 0
    assert record["id"] is _ChoiceId.A


@pytest.mark.parametrize("across_chunks", [False, True])
def test_bulk_repeated_plain_enum_primary_key_sees_earlier_writes(
    store_factory, across_chunks
) -> None:
    from ontary.store._core import INGEST_BATCH

    store = store_factory(_choice_pk_registry())
    records = [{"id": _ChoiceId.A, "name": "first", "extra": "retained"}]
    if across_chunks:
        records.extend(
            {"id": _ChoiceId.B, "name": "filler"} for _ in range(INGEST_BATCH - 1)
        )
    records.extend([
        {"id": _ChoiceId.A, "name": "second"},
        {"id": _ChoiceId.A, "name": "second"},
    ])
    with _count_steps(store) as counts:
        outcomes = store.upsert_objects("Item", records, _SOURCE)
    assert (outcomes[0].status, outcomes[0].id) == ("inserted", "a")
    assert [(outcome.status, outcome.id) for outcome in outcomes[-2:]] == [
        ("updated", "a"), ("unchanged", "a")
    ]
    assert store.read_current("Item", "a").payload == {
        "id": "a", "name": "second", "extra": "retained"
    }
    assert store.read_current("Item", str(_ChoiceId.A)) is None
    assert history_rows(store, "Item", "a") == 2
    assert counts["_current_rows_many"] == 1
    assert counts["_current_row"] == 0
    assert all(record["id"] in (_ChoiceId.A, _ChoiceId.B) for record in records)


def test_bulk_opens_one_outer_transaction_and_all_steps_including_clock_run_inside(
    store, monkeypatch
) -> None:
    original_transaction = store.transaction
    outer_entries = 0

    @contextmanager
    def transaction():
        nonlocal outer_entries
        if not store.in_transaction:
            outer_entries += 1
        with original_transaction() as value:
            yield value

    def inside(original):
        @wraps(original)
        def call(*args, **kwargs):
            assert store.in_transaction
            return original(*args, **kwargs)

        return call

    monkeypatch.setattr(store, "transaction", transaction)
    for name in (*_READ_STEPS, *_WRITE_STEPS, "_now"):
        monkeypatch.setattr(store, name, inside(getattr(store, name)))
    outcomes = store.upsert_objects(
        "Team", [{"id": "a", "name": "one"}, {"id": "a", "name": "two"}], _SOURCE
    )
    assert [outcome.status for outcome in outcomes] == ["inserted", "updated"]
    assert outer_entries == 1


def test_empty_bulk_input_returns_no_outcomes_without_storage_or_clock_calls(
    store, monkeypatch
) -> None:
    def fail(*args, **kwargs):
        pytest.fail("empty bulk call touched storage or clock")

    for name in (*_READ_STEPS, *_WRITE_STEPS, "_now"):
        monkeypatch.setattr(store, name, fail)
    assert store.upsert_objects("Team", [], _SOURCE) == []


def test_bulk_capture_records_only_writes_after_outer_transaction_exits(
    store_factory, monkeypatch
) -> None:
    store = store_factory(_order_registry())
    original = store._record_write

    def after_transaction(record):
        assert not store.in_transaction
        original(record)

    monkeypatch.setattr(store, "_record_write", after_transaction)
    with store.capture_action_writes() as writes:
        outcomes = store.upsert_objects(
            "Order",
            [
                {"id": "a", "status": "pending"},
                {"id": "a", "status": "paid"},
                {"id": "a", "status": "paid"},
            ],
            _SOURCE,
        )
    assert [outcome.status for outcome in outcomes] == ["inserted", "updated", "unchanged"]
    assert writes == [
        WriteRecord(op="create", object_type="Order", object_id="a"),
        WriteRecord(op="update", object_type="Order", object_id="a"),
    ]


def test_single_insert_and_update_record_writes_after_their_transactions(
    store_factory, monkeypatch
) -> None:
    store = store_factory(_order_registry())
    original = store._record_write

    def after_transaction(record):
        assert not store.in_transaction
        original(record)

    monkeypatch.setattr(store, "_record_write", after_transaction)
    with store.capture_action_writes() as writes:
        store.insert("Order", {"id": "a", "status": "pending"}, _SOURCE)
        store.update("Order", "a", {"status": "pending"}, _SOURCE)
    assert writes == [
        WriteRecord(op="create", object_type="Order", object_id="a"),
        WriteRecord(op="update", object_type="Order", object_id="a"),
    ]
    assert history_rows(store, "Order", "a") == 2


def test_history_counter_includes_closed_rows_and_filters_tenant_type_and_id(
    store, monkeypatch
) -> None:
    store.insert("Team", {"id": "a", "name": "one"}, _SOURCE)
    store.update("Team", "a", {"name": "two"}, _NEW_SOURCE)
    store.insert("Team", {"id": "b", "name": "other"}, _SOURCE)
    store.insert("Company", {"id": "a", "name": "other type"}, _SOURCE)
    store.retire_object("Team", "a")
    assert history_rows(store, "Team", "a") == 2
    assert history_rows(store, "Team", "b") == 1
    assert history_rows(store, "Company", "a") == 1
    monkeypatch.setattr(store, "_tenant", "foreign")
    assert history_rows(store, "Team", "a") == 0
