"""AC1–AC6: ingest batches commit once, retain refusals, and bound storage work."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import partial, wraps
from math import ceil

import pytest
from _history import history_rows
from conftest import raises_code
from test_ingest import build_registry
from test_store_conformance import STORE_FACTORIES, StoreFactory, _link_history_rows

from ontary.client import OntologyClient
from ontary.errors import ConflictError
from ontary.ingest import IngestError, IngestReport, bulk_link, bulk_upsert
from ontary.meta import Cardinality, PropertyDef, RuleDef, TransitionDef
from ontary.ontology import OntologyDef
from ontary.scope import ScopePolicy
from ontary.security import Consumer
from ontary.store import DEFAULT_BATCH, Source

_SOURCE = Source(
    source_system="batch-tests", source_id="first", extracted_at="2026-01-01T00:00:00Z"
)
_NEW_SOURCE = Source(
    source_system="next-load", source_id="second", extracted_at="2026-01-02T00:00:00Z"
)
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
def _count_steps(store):
    """Count exactly the storage steps defined in design §6."""
    counts = Counter()

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


@pytest.fixture(params=STORE_FACTORIES, ids=lambda f: f.__name__.removeprefix("_make_"))
def store_factory(request: pytest.FixtureRequest) -> StoreFactory:
    return request.param


@pytest.fixture
def registry():
    registry = build_registry()
    team = registry.get_object_type("Team")
    team.properties.append(
        PropertyDef(
            name="status",
            type="str",
            required=False,
            choices=("pending", "paid", "shipped"),
            transitions=TransitionDef(
                initial=("pending",),
                moves={"pending": ("paid",), "paid": ("shipped",), "shipped": ()},
            ),
        )
    )
    team.rules = (
        RuleDef(
            name="nonnegative-size",
            message="size must be nonnegative",
            check=lambda row: row.get("size", 0) >= 0,
        ),
    )
    registry.validate()
    return registry


@pytest.fixture
def store(store_factory, registry):
    return store_factory(registry)


def _client(store, registry):
    ontology = OntologyDef(
        name="ingest-batch-tests",
        registry=registry,
        policy=ScopePolicy(
            levels=["org"],
            unscoped_types={"Team", "Department", "Ticket", "OwnedThing"},
            min_n=1,
        ),
    )
    ontology.validate()
    return OntologyClient(
        ontology,
        store,
        Consumer(
            actor_id="loader", role="Loader", scope_level="org", scope_id="org-1", kind="human"
        ),
    )


@pytest.fixture(params=["client", "functions"])
def ingester(request, store, registry):
    if request.param == "client":
        client = _client(store, registry)
        return partial(client.ingest, on_error="report"), partial(
            client.ingest_links, on_error="report"
        )
    return partial(bulk_upsert, store, registry), partial(bulk_link, store, registry)


def _seed_endpoints(store, registry, ids):
    bulk_upsert(store, registry, "Team", [{"id": i, "name": i} for i in ids], _SOURCE)
    bulk_upsert(store, registry, "Department", [{"id": i} for i in ids], _SOURCE)


def test_thousand_objects_and_links_commit_once_and_meet_first_load_and_rerun_bounds(
    store, registry, ingester, monkeypatch
) -> None:
    """AC1, AC5, AC6: both public surfaces share the design's B=500 bounds."""
    upsert, link = ingester
    ids = [str(i) for i in range(1000)]
    records = [{"id": i, "name": i} for i in ids]
    pairs = [(i, i) for i in ids]
    # Exercise all four permitted link reads with distinct types and limited to-side.
    registry.get_link_type("belongsToDepartment").cardinality = Cardinality.ONE_TO_ONE
    upsert("Department", [{"id": i} for i in ids], _SOURCE)
    original_transaction = store.transaction
    outer_entries = 0

    @contextmanager
    def transaction():
        nonlocal outer_entries
        if not store.in_transaction:
            outer_entries += 1
        with original_transaction() as value:
            yield value

    monkeypatch.setattr(store, "transaction", transaction)
    assert DEFAULT_BATCH == 500
    for unchanged, source in ((False, _SOURCE), (True, _NEW_SOURCE)):
        outer_entries = 0
        with _count_steps(store) as counts:
            report = upsert("Team", records, source)
        assert outer_entries == 1
        assert report.ok
        assert report.inserted_ids == ids
        assert report.unchanged_ids == (ids if unchanged else [])
        assert sum(counts[name] for name in _READ_STEPS) <= ceil(len(records) / DEFAULT_BATCH)
        if unchanged:
            assert sum(counts[name] for name in _WRITE_STEPS) == 0

        outer_entries = 0
        with _count_steps(store) as counts:
            report = link("belongsToDepartment", pairs, source)
        assert outer_entries == 1
        assert report.ok
        assert report.inserted_ids == [f"{i}->{i}" for i in ids]
        assert report.unchanged_ids == (report.inserted_ids if unchanged else [])
        assert sum(counts[name] for name in _READ_STEPS) <= 4 * ceil(len(pairs) / DEFAULT_BATCH)
        if unchanged:
            assert sum(counts[name] for name in _WRITE_STEPS) == 0

    for i in ids:
        assert store.read_current("Team", i).payload == {"id": i, "name": i}
        assert history_rows(store, "Team", i) == 1
        assert store.links_from("belongsToDepartment", i) == [i]
        assert _link_history_rows(store, "belongsToDepartment", i, i) == [None]


def _mixed_batch(store, registry, code):
    """Put two refused rows around an unchanged row, with good rows at both ends."""
    if code in {"CARDINALITY_VIOLATION", "LINK_ENDPOINT_NOT_FOUND"}:
        _seed_endpoints(store, registry, ["a", "b", "c", "same"])
        store.create_link("belongsToDepartment", "same", "same")
        bad = [("missing-from", "a"), ("b", "missing-to")]
        if code == "CARDINALITY_VIOLATION":
            store.create_link("belongsToDepartment", "b", "a")
            bad = [("a", "b"), ("b", "b")]
        return "belongsToDepartment", [("a", "a"), bad[0], ("same", "same"), bad[1], ("c", "c")]

    store.insert("Team", {"id": "same", "name": "same"}, _SOURCE)
    bad = [{"id": f"bad-{i}", "name": "bad"} for i in (1, 2)]
    for record in bad:
        if code == "INVALID_RECORD":
            record["size"] = "many"
        elif code == "RULE_VIOLATED":
            record["size"] = -1
        else:
            store.insert("Team", {**record, "status": "pending"}, _SOURCE)
            record["status"] = "shipped"
    return "Team", [
        {"id": "a", "name": "a"},
        bad[0],
        {"id": "same", "name": "same"},
        bad[1],
        {"id": "c", "name": "c"},
    ]


def _assert_mixed_batch(store, type_name, batch, code, report):
    assert [(error.index, error.code) for error in report.errors] == [(1, code), (3, code)]
    if type_name == "Team":
        assert report.inserted_ids == ["a", "same", "c"]
        assert report.unchanged_ids == ["same"]
        for i in report.inserted_ids:
            assert store.read_current("Team", i) is not None
            assert history_rows(store, "Team", i) == 1
        for i in ("bad-1", "bad-2"):
            # A transition rejection targets an existing row: retain it without
            # creating any new version. New invalid/rule-rejected ids have no row.
            if code == "TRANSITION_NOT_ALLOWED":
                assert store.read_current("Team", i).payload == {
                    "id": i,
                    "name": "bad",
                    "status": "pending",
                }
                assert history_rows(store, "Team", i) == 1
            else:
                assert store.read_current("Team", i) is None
                assert history_rows(store, "Team", i) == 0
    else:
        assert report.inserted_ids == ["a->a", "same->same", "c->c"]
        assert report.unchanged_ids == ["same->same"]
        for i in ("a", "same", "c"):
            assert store.links_from(type_name, i) == [i]
            assert _link_history_rows(store, type_name, i, i) == [None]
        for index in (1, 3):
            from_id, to_id = batch[index]
            assert to_id not in store.links_from(type_name, from_id)
            assert _link_history_rows(store, type_name, from_id, to_id) == []
        if code == "LINK_ENDPOINT_NOT_FOUND":
            assert store.read_current("Team", "missing-from") is None
            assert store.read_current("Department", "missing-to") is None
            assert history_rows(store, "Team", "missing-from") == 0
            assert history_rows(store, "Department", "missing-to") == 0


@pytest.mark.parametrize(
    "code",
    [
        "INVALID_RECORD",
        "RULE_VIOLATED",
        "TRANSITION_NOT_ALLOWED",
        "CARDINALITY_VIOLATION",
        "LINK_ENDPOINT_NOT_FOUND",
    ],
)
@pytest.mark.parametrize("on_error", ["report", "raise"])
def test_expected_refusals_commit_valid_rows_and_client_report_matches_functions(
    store_factory, registry, code, on_error
) -> None:
    """AC2: raise/report keep all valid rows and produce the identical full report."""
    reports = []
    for surface in ("functions", "client"):
        store = store_factory(registry)
        type_name, batch = _mixed_batch(store, registry, code)
        if surface == "functions":
            ingest = bulk_upsert if type_name == "Team" else bulk_link
            report = ingest(store, registry, type_name, batch, _NEW_SOURCE)
        else:
            client = _client(store, registry)
            ingest = client.ingest if type_name == "Team" else client.ingest_links
            if on_error == "raise":
                with pytest.raises(IngestError) as caught:
                    ingest(type_name, batch, _NEW_SOURCE, on_error=on_error)
                report = caught.value.report
                assert report is not None
                assert caught.value.code == code
            else:
                report = ingest(type_name, batch, _NEW_SOURCE, on_error=on_error)
        _assert_mixed_batch(store, type_name, batch, code, report)
        reports.append(report)
    assert reports[0] == reports[1]
    assert reports[0].model_dump() == reports[1].model_dump()


@pytest.mark.parametrize("kind", ["objects", "links"])
@pytest.mark.parametrize(
    "error", [RuntimeError("sixth write failed"), KeyboardInterrupt("interrupted")]
)
def test_unexpected_error_at_row_five_rolls_back_all_ten_rows(
    store, registry, ingester, monkeypatch, kind, error
) -> None:
    """AC3: the original unexpected exception propagates and no history remains."""
    upsert, link = ingester
    ids = [str(i) for i in range(10)]
    if kind == "links":
        _seed_endpoints(store, registry, ids)
    step = "_insert_object_row" if kind == "objects" else "_insert_link_row"
    original = getattr(store, step)
    attempts = 0

    def fail_sixth(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 6:
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(store, step, fail_sixth)
    with pytest.raises(type(error)) as caught:
        if kind == "objects":
            upsert("Team", [{"id": i, "name": i} for i in ids], _SOURCE)
        else:
            link("belongsToDepartment", [(i, i) for i in ids], _SOURCE)
    assert caught.value is error
    assert attempts == 6
    assert not store.in_transaction
    for i in ids:
        if kind == "objects":
            assert store.read_current("Team", i) is None
            assert history_rows(store, "Team", i) == 0
        else:
            assert store.links_from("belongsToDepartment", i) == []
            assert _link_history_rows(store, "belongsToDepartment", i, i) == []


def test_identical_hundred_objects_keep_history_and_lineage_then_one_change_writes_once(
    store, ingester
) -> None:
    """AC4: newer extraction lineage does not rewrite an identical payload."""
    upsert, _ = ingester
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.bind_clock(lambda: now)
    records = [{"id": str(i), "name": str(i)} for i in range(100)]
    ids = [record["id"] for record in records]
    first = upsert("Team", records, _SOURCE)
    originals = {i: store.read_current("Team", i) for i in ids}
    assert first.unchanged_ids == []
    now += timedelta(days=1)
    second = upsert("Team", records, _NEW_SOURCE)
    assert second.inserted_ids == second.unchanged_ids == ids
    for i in ids:
        assert history_rows(store, "Team", i) == 1
        assert store.read_current("Team", i) == originals[i]

    changed_index = 37
    changed_id = ids[changed_index]
    records[changed_index] = {**records[changed_index], "name": "changed"}
    third = upsert("Team", records, _NEW_SOURCE)
    assert third.inserted_ids == ids
    assert third.unchanged_ids == [i for i in ids if i != changed_id]
    for i in ids:
        assert history_rows(store, "Team", i) == (2 if i == changed_id else 1)
        current = store.read_current("Team", i)
        if i == changed_id:
            assert current.payload["name"] == "changed"
            assert current.lineage.valid_from != originals[i].lineage.valid_from
            assert current.lineage.source_system == _NEW_SOURCE.source_system
            assert current.lineage.source_id == _NEW_SOURCE.source_id
            assert current.lineage.extracted_at == _NEW_SOURCE.extracted_at
        else:
            assert current == originals[i]


def test_validation_and_core_refusals_merge_in_original_order_with_one_core_call(
    store, registry, ingester, monkeypatch
) -> None:
    upsert, _ = ingester
    store.insert("Team", {"id": "same", "name": "same"}, _SOURCE)
    original = store.upsert_objects
    calls = []

    def bulk(obj_type, records, source, *, refusals):
        calls.append((obj_type, records, source, refusals))
        return original(obj_type, records, source, refusals=refusals)

    def per_row(*args, **kwargs):
        pytest.fail("ingest made a per-row public store call")

    monkeypatch.setattr(store, "upsert_objects", bulk)
    for name in ("insert", "update", "read_current"):
        monkeypatch.setattr(store, name, per_row)
    records = [
        {"id": "rule", "name": "bad", "size": -1},
        {"id": "invalid"},
        {"id": "same", "name": "same"},
        {"id": "good", "name": "good", "joined_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        {"id": "last-invalid", "name": "bad", "size": "many"},
    ]
    report = upsert("Team", records, _SOURCE)
    assert len(calls) == 1
    assert calls[0][0] == "Team"
    assert calls[0][1] == [records[i] for i in (0, 2, 3)]
    assert calls[0][2] == _SOURCE
    assert calls[0][3] == frozenset({"TRANSITION_NOT_ALLOWED", "RULE_VIOLATED"})
    assert [(error.index, error.code) for error in report.errors] == [
        (0, "RULE_VIOLATED"),
        (1, "INVALID_RECORD"),
        (4, "INVALID_RECORD"),
    ]
    assert report.inserted_ids == ["same", "good"]
    assert report.unchanged_ids == ["same"]


def test_link_uses_one_core_call_with_refusals_and_duplicate_pairs_are_unchanged(
    store, registry, ingester, monkeypatch
) -> None:
    _, link = ingester
    _seed_endpoints(store, registry, ["a", "b"])
    original = store.create_links
    calls = []

    def bulk(link_type, pairs, *, refusals):
        calls.append((link_type, pairs, refusals))
        return original(link_type, pairs, refusals=refusals)

    def per_row(*args, **kwargs):
        pytest.fail("ingest_links made a per-row public store call")

    monkeypatch.setattr(store, "create_links", bulk)
    for name in ("create_link", "links_from", "links_to", "read_current"):
        monkeypatch.setattr(store, name, per_row)
    pairs = [("a", "a"), ("a", "a"), ("b", "b")]
    report = link("belongsToDepartment", pairs, _SOURCE)
    assert calls == [
        (
            "belongsToDepartment",
            pairs,
            frozenset({"CARDINALITY_VIOLATION", "LINK_ENDPOINT_NOT_FOUND"}),
        )
    ]
    assert report.inserted_ids == ["a->a", "a->a", "b->b"]
    assert report.unchanged_ids == ["a->a"]


class _ChoiceId(Enum):
    A = "a"


def test_plain_enum_primary_key_normalizes_before_lookup_and_duplicate_merge(
    store, registry, ingester
) -> None:
    upsert, _ = ingester
    registry.get_object_type("Team").properties[0].choices = ("a", "b")
    records = [
        {"id": _ChoiceId.A, "name": "first"},
        {"id": "a", "name": "second"},
        {"id": _ChoiceId.A, "name": "second"},
    ]
    first = upsert("Team", records, _SOURCE)
    assert first.inserted_ids == ["a", "a", "a"]
    assert first.unchanged_ids == ["a"]
    second = upsert("Team", [records[2]], _NEW_SOURCE)
    assert second.inserted_ids == second.unchanged_ids == ["a"]
    assert store.read_current("Team", "a").payload == {"id": "a", "name": "second"}
    assert store.read_current("Team", str(_ChoiceId.A)) is None
    assert history_rows(store, "Team", "a") == 2
    assert records[0]["id"] is _ChoiceId.A


def test_caller_transaction_is_refused_on_both_ingest_surfaces(store, ingester) -> None:
    upsert, link = ingester
    with store.transaction():
        with raises_code(ConflictError, "CALLER_TRANSACTION_REFUSED"):
            upsert("Team", [{"id": "a", "name": "a"}], _SOURCE)
        with raises_code(ConflictError, "CALLER_TRANSACTION_REFUSED"):
            link("belongsToDepartment", [("a", "a")], _SOURCE)
    assert store.read_current("Team", "a") is None


@pytest.mark.parametrize(
    "kind,type_name,code",
    [
        ("objects", "unknown", "UNKNOWN_OBJECT_TYPE"),
        ("objects", "OwnedThing", "OWNED_TYPE_REFUSED"),
        ("links", "unknown", "UNKNOWN_LINK_TYPE"),
        ("links", "ownedLink", "OWNED_TYPE_REFUSED"),
    ],
)
def test_unknown_and_owned_types_return_before_any_core_call(
    store, ingester, monkeypatch, kind, type_name, code
) -> None:
    upsert, link = ingester

    def fail(*args, **kwargs):
        pytest.fail("unknown/owned type reached the core or storage")

    for name in ("upsert_objects", "create_links", *_READ_STEPS, *_WRITE_STEPS):
        monkeypatch.setattr(store, name, fail)
    if kind == "objects":
        report = upsert(type_name, [{"id": "a", "name": "a"}, {"id": "b", "name": "b"}], _SOURCE)
    else:
        report = link(type_name, [("a", "a"), ("b", "b")], _SOURCE)
    assert report.inserted_ids == report.unchanged_ids == []
    assert [(error.index, error.code) for error in report.errors] == [(0, code), (1, code)]


def test_owned_default_and_action_edited_value_survive_identical_reingest(store, ingester) -> None:
    upsert, _ = ingester
    record = {"id": "ticket", "status": "open"}
    upsert("Ticket", [record], _SOURCE)
    assert store.read_current("Ticket", "ticket").payload["note"] == "no note yet"
    store.update("Ticket", "ticket", {"note": "action edit"}, _SOURCE)
    original = store.read_current("Ticket", "ticket")
    report = upsert("Ticket", [record], _NEW_SOURCE)
    assert report.inserted_ids == report.unchanged_ids == ["ticket"]
    assert store.read_current("Ticket", "ticket") == original
    assert history_rows(store, "Ticket", "ticket") == 2


def test_unchanged_ids_default_is_independent_and_report_serializes_it() -> None:
    first, second = IngestReport(), IngestReport()
    first.unchanged_ids.append("a")
    assert second.unchanged_ids == []
    assert IngestReport.model_validate(first.model_dump()).unchanged_ids == ["a"]
