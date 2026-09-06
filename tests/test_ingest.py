"""Tests for ontary.ingest: lineage-stamped bulk write API (spec AC9), and
ingest authority (declared-contracts §3 AC3/AC4/AC5/AC9)."""

from __future__ import annotations

import pickle

import pytest
from conftest import raises_code

from ontary.client import OntologyClient
from ontary.errors import ConflictError, OntaryError, ValidationFailed
from ontary.ingest import IngestError, IngestReport, bulk_link, bulk_upsert
from ontary.meta import (
    Cardinality,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
)
from ontary.ontology import OntologyDef
from ontary.scope import ScopePolicy
from ontary.security import Consumer
from ontary.store import ObjectStore, Source


def build_registry() -> OntologyRegistry:
    registry = OntologyRegistry()
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Team",
            display_name="Team",
            description="A canonical team",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str", required=False),
                PropertyDef(name="name", type="str"),
                PropertyDef(name="size", type="int", required=False),
                PropertyDef(name="joined_at", type="datetime", required=False),
            ],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Department",
            display_name="Department",
            description="A canonical department",
            layer="L0",
            properties=[PropertyDef(name="id", type="str", required=False)],
            primary_key="id",
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="belongsToDepartment",
            from_type="Team",
            to_type="Department",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Team belongs to a Department",
        )
    )
    # Authority fixtures (declared-contracts §3 AC1/AC3/AC4/AC5/AC9).
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Ticket",
            display_name="Ticket",
            description="A source-backed ticket with one ontology-owned property",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str", required=False),
                PropertyDef(
                    name="status", type="str", choices=("open", "closed")
                ),
                PropertyDef(name="note", type="str", required=False),
            ],
            primary_key="id",
            owned={"note": "no note yet"},
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="OwnedThing",
            display_name="Owned Thing",
            description="A whole-type ontology-owned object -- no connector supplies it",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str", required=False),
                PropertyDef(name="name", type="str"),
            ],
            primary_key="id",
            owned=True,
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="ownedLink",
            from_type="Team",
            to_type="Department",
            cardinality=Cardinality.MANY_TO_MANY,
            description="An ontology-owned link -- no connector supplies it",
            owned=True,
        )
    )
    registry.validate()
    return registry


@pytest.fixture
def registry() -> OntologyRegistry:
    return build_registry()


@pytest.fixture
def store(registry: OntologyRegistry) -> ObjectStore:
    return ObjectStore(registry)


def _client_for(registry: OntologyRegistry, store: ObjectStore) -> OntologyClient:
    ontology = OntologyDef(
        name="ingest-tests",
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
            actor_id="loader",
            role="Loader",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
    )


@pytest.fixture
def client(registry: OntologyRegistry, store: ObjectStore) -> OntologyClient:
    return _client_for(registry, store)


def test_client_ingest_default_raises_after_committing_valid_prefix(
    client: OntologyClient, store: ObjectStore
) -> None:
    records = [
        {"id": "team-client-good", "name": "Rockets"},
        {"id": "team-client-bad", "name": "Comets", "size": "many"},
    ]

    with pytest.raises(IngestError) as exc_info:
        client.ingest("Team", records, Source(source_system="synthetic"))

    error = exc_info.value
    assert error.report.inserted_ids == ["team-client-good"]
    assert len(error.report.errors) == 1
    assert error.report.errors[0].index == 1
    assert error.report.errors[0].code == "INVALID_RECORD"
    assert "1 committed" in str(error)
    assert "1 failed" in str(error)
    assert store.read_current("Team", "team-client-good") is not None
    assert store.read_current("Team", "team-client-bad") is None


def test_client_ingest_failure_is_caught_as_ontary_error(
    client: OntologyClient,
) -> None:
    try:
        client.ingest(
            "Team",
            [{"id": "team-client-bad", "name": "Comets", "size": "many"}],
            Source(source_system="synthetic"),
        )
    except OntaryError as error:
        assert isinstance(error, IngestError)
        assert error.code == "INVALID_RECORD"
        assert error.report is not None
        assert error.report.inserted_ids == []
        assert len(error.report.errors) == 1
        assert error.report.errors[0].code == "INVALID_RECORD"
    else:
        pytest.fail("client ingest failure was not caught as OntaryError")


def test_per_record_ingest_error_survives_pickle() -> None:
    original = IngestError(
        index=3,
        reason="missing required property 'name'",
        code="INVALID_RECORD",
    )

    restored = pickle.loads(pickle.dumps(original))

    assert isinstance(restored, IngestError)
    assert restored.index == 3
    assert restored.reason == "missing required property 'name'"
    assert restored.code == "INVALID_RECORD"
    assert restored.report is None
    assert str(restored) == str(original)


def test_report_form_ingest_error_survives_pickle() -> None:
    report = IngestReport(
        inserted_ids=["team-client-good"],
        errors=[
            IngestError(
                index=1,
                reason="property 'size' must be an int",
                code="INVALID_RECORD",
            )
        ],
    )
    original = IngestError(report=report)

    restored = pickle.loads(pickle.dumps(original))

    assert isinstance(restored, IngestError)
    assert restored.index is None
    assert restored.reason is None
    assert restored.code == "INVALID_RECORD"
    assert restored.report == report
    assert str(restored) == str(original)


def test_client_ingest_report_mode_matches_raised_report(
    registry: OntologyRegistry,
) -> None:
    records = [
        {"id": "team-report-good", "name": "Rockets"},
        {"id": "team-report-bad", "name": "Comets", "size": "many"},
    ]
    report_client = _client_for(registry, ObjectStore(registry))
    raise_client = _client_for(registry, ObjectStore(registry))

    report = report_client.ingest(
        "Team", records, Source(source_system="synthetic"), on_error="report"
    )
    assert report.errors[0].code == "INVALID_RECORD"

    with pytest.raises(IngestError) as exc_info:
        raise_client.ingest("Team", records, Source(source_system="synthetic"))

    assert exc_info.value.report == report


def test_client_ingest_default_does_not_raise_for_fully_valid_batch(
    client: OntologyClient,
) -> None:
    report = client.ingest(
        "Team",
        [
            {"id": "team-valid-1", "name": "Rockets"},
            {"id": "team-valid-2", "name": "Comets", "size": 2},
        ],
        Source(source_system="synthetic"),
    )

    assert report.ok
    assert report.inserted_ids == ["team-valid-1", "team-valid-2"]


def test_client_ingest_rejects_invalid_on_error_without_ingesting(
    client: OntologyClient, store: ObjectStore
) -> None:
    with raises_code(ValidationFailed, "INVALID_PARAMS"):
        client.ingest(  # type: ignore[arg-type]
            "Team",
            [{"id": "team-invalid-mode", "name": "Rockets"}],
            Source(source_system="synthetic"),
            on_error="riase",
        )

    assert store.read_current("Team", "team-invalid-mode") is None


def test_client_ingest_links_default_raises_after_committing_valid_prefix(
    client: OntologyClient, store: ObjectStore
) -> None:
    source = Source(source_system="synthetic")
    store.insert("Team", {"id": "team-link", "name": "Rockets"}, source)
    store.insert("Department", {"id": "department-a"}, source)
    store.insert("Department", {"id": "department-b"}, source)

    pairs = [
        ("team-link", "department-a"),
        ("team-link", "department-b"),
    ]
    with pytest.raises(IngestError) as exc_info:
        client.ingest_links("belongsToDepartment", pairs, source)

    error = exc_info.value
    assert error.report.inserted_ids == ["team-link->department-a"]
    assert len(error.report.errors) == 1
    assert error.report.errors[0].index == 1
    assert error.report.errors[0].code == "CARDINALITY_VIOLATION"
    assert "1 committed" in str(error)
    assert "1 failed" in str(error)
    assert store.links_from("belongsToDepartment", "team-link") == ["department-a"]


def test_client_ingest_links_default_does_not_raise_for_fully_valid_batch(
    client: OntologyClient, store: ObjectStore
) -> None:
    source = Source(source_system="synthetic")
    store.insert("Team", {"id": "team-link-valid-1", "name": "Rockets"}, source)
    store.insert("Team", {"id": "team-link-valid-2", "name": "Comets"}, source)
    store.insert("Department", {"id": "department-valid-a"}, source)
    store.insert("Department", {"id": "department-valid-b"}, source)

    report = client.ingest_links(
        "belongsToDepartment",
        [
            ("team-link-valid-1", "department-valid-a"),
            ("team-link-valid-2", "department-valid-b"),
        ],
        source,
    )

    assert report.ok
    assert report.inserted_ids == [
        "team-link-valid-1->department-valid-a",
        "team-link-valid-2->department-valid-b",
    ]


def test_client_ingest_links_rejects_invalid_on_error_without_ingesting(
    client: OntologyClient, store: ObjectStore
) -> None:
    source = Source(source_system="synthetic")
    store.insert("Team", {"id": "team-link-invalid-mode", "name": "Rockets"}, source)
    store.insert("Department", {"id": "department-invalid-mode"}, source)

    with raises_code(ValidationFailed, "INVALID_PARAMS"):
        client.ingest_links(  # type: ignore[arg-type]
            "belongsToDepartment",
            [("team-link-invalid-mode", "department-invalid-mode")],
            source,
            on_error="riase",
        )

    assert store.links_from("belongsToDepartment", "team-link-invalid-mode") == []


def test_client_ingest_links_report_mode_matches_raised_report(
    registry: OntologyRegistry,
) -> None:
    source = Source(source_system="synthetic")
    pairs = [
        ("team-link-report", "department-a"),
        ("team-link-report", "department-b"),
    ]
    report_store = ObjectStore(registry)
    raise_store = ObjectStore(registry)
    for store in (report_store, raise_store):
        store.insert("Team", {"id": "team-link-report", "name": "Rockets"}, source)
        store.insert("Department", {"id": "department-a"}, source)
        store.insert("Department", {"id": "department-b"}, source)

    report_client = _client_for(registry, report_store)
    raise_client = _client_for(registry, raise_store)
    report = report_client.ingest_links(
        "belongsToDepartment", pairs, source, on_error="report"
    )

    with pytest.raises(IngestError) as exc_info:
        raise_client.ingest_links("belongsToDepartment", pairs, source)

    assert exc_info.value.report == report


def test_bulk_upsert_happy_path_stamps_lineage(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    report = bulk_upsert(
        store,
        registry,
        "Team",
        [{"id": "team-1", "name": "Rockets", "size": 5}],
        Source(
            source_system="synthetic",
            source_id="seed-1",
            extracted_at="2026-01-01T00:00:00+00:00",
        ),
    )

    assert report.ok
    assert report.errors == []
    assert report.inserted_ids == ["team-1"]

    got = store.read_current("Team", "team-1")
    assert got is not None
    assert got.payload["name"] == "Rockets"
    assert got.lineage.source_system == "synthetic"
    assert got.lineage.source_id == "seed-1"
    assert got.lineage.extracted_at == "2026-01-01T00:00:00+00:00"


def test_bulk_upsert_without_extracted_at_leaves_it_none(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    """AC9 finding 2: `extracted_at` is optional lineage -- absent stays
    None rather than defaulting to some fabricated timestamp."""
    report = bulk_upsert(
        store,
        registry,
        "Team",
        [{"id": "team-no-extracted", "name": "Rockets"}],
        Source(source_system="synthetic"),
    )

    assert report.ok
    got = store.read_current("Team", "team-no-extracted")
    assert got is not None
    assert got.lineage.extracted_at is None


def test_bulk_upsert_missing_pk_rejected(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    report = bulk_upsert(
        store,
        registry,
        "Department",
        [{}],
        Source(source_system="synthetic"),
    )

    assert not report.ok
    assert report.inserted_ids == []
    assert len(report.errors) == 1
    assert report.errors[0].index == 0
    assert "primary key" in report.errors[0].reason
    assert report.errors[0].code == "INVALID_RECORD"


def test_bulk_upsert_missing_required_property_rejected(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    report = bulk_upsert(
        store,
        registry,
        "Team",
        [{"id": "team-2"}],
        Source(source_system="synthetic"),
    )

    assert not report.ok
    assert report.inserted_ids == []
    assert "name" in report.errors[0].reason
    # not written even partially
    assert store.read_current("Team", "team-2") is None


def test_bulk_upsert_wrong_type_rejected(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    report = bulk_upsert(
        store,
        registry,
        "Team",
        [{"id": "team-3", "name": "Rockets", "size": "a lot"}],
        Source(source_system="synthetic"),
    )

    assert not report.ok
    assert "size" in report.errors[0].reason
    assert store.read_current("Team", "team-3") is None


def test_bulk_upsert_out_of_choices_value_is_reported_without_a_write(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    report = bulk_upsert(
        store,
        registry,
        "Ticket",
        [{"id": "ticket-pending", "status": "pending"}],
        Source(source_system="synthetic"),
    )

    assert not report.ok
    assert report.errors[0].code == "INVALID_RECORD"
    assert "status" in report.errors[0].reason
    assert store.read_current("Ticket", "ticket-pending") is None


def test_bulk_upsert_unknown_object_type_rejected(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    report = bulk_upsert(
        store,
        registry,
        "NotARealType",
        [{"id": "x"}],
        Source(source_system="synthetic"),
    )

    assert not report.ok
    assert len(report.errors) == 1
    assert "unknown object type" in report.errors[0].reason
    assert report.errors[0].code == "UNKNOWN_OBJECT_TYPE"


def test_bulk_upsert_partial_batch_no_silent_writes(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    report = bulk_upsert(
        store,
        registry,
        "Team",
        [
            {"id": "team-good", "name": "Rockets"},
            {"id": "team-bad"},  # missing name
        ],
        Source(source_system="synthetic"),
    )

    assert report.inserted_ids == ["team-good"]
    assert len(report.errors) == 1
    assert report.errors[0].index == 1
    assert store.read_current("Team", "team-good") is not None
    assert store.read_current("Team", "team-bad") is None


def test_bulk_link_cardinality_violation_reported_per_pair(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    report = bulk_link(
        store,
        registry,
        "belongsToDepartment",
        [
            ("team-1", "dept-a"),
            ("team-1", "dept-b"),  # violates MANY_TO_ONE: team-1 already linked
            ("team-2", "dept-a"),
        ],
        Source(source_system="synthetic"),
    )

    assert len(report.errors) == 1
    assert report.errors[0].index == 1
    assert report.errors[0].code == "CARDINALITY_VIOLATION"
    assert len(report.inserted_ids) == 2
    assert store.links_from("belongsToDepartment", "team-1") == ["dept-a"]
    assert store.links_from("belongsToDepartment", "team-2") == ["dept-a"]


def test_bulk_link_unknown_link_type_rejected(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    report = bulk_link(
        store,
        registry,
        "notARealLink",
        [("a", "b")],
        Source(source_system="synthetic"),
    )

    assert not report.ok
    assert "unknown link type" in report.errors[0].reason
    assert report.errors[0].code == "UNKNOWN_LINK_TYPE"


def test_bulk_upsert_reingest_updates_existing_row(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    """P0 regression: re-ingesting a known pk must update, not double-insert.

    A second bulk_upsert of the same pk with a changed payload must leave
    exactly one current (valid_to IS NULL) row for that id, and that row
    must reflect the new payload -- not silently corrupt history by leaving
    two current rows behind while `_get` happens to still return stale data.
    """
    source = Source(source_system="synthetic", source_id="seed-1")
    report1 = bulk_upsert(
        store, registry, "Team", [{"id": "team-1", "name": "Rockets"}], source
    )
    assert report1.ok

    report2 = bulk_upsert(
        store, registry, "Team", [{"id": "team-1", "name": "Renamed"}], source
    )
    assert report2.ok
    assert report2.inserted_ids == ["team-1"]

    got = store.read_current("Team", "team-1")
    assert got is not None
    assert got.payload["name"] == "Renamed"

    # exactly one current row for team-1 in the underlying table.
    cur = store._conn.execute(
        "SELECT COUNT(*) FROM objects WHERE object_type = ? AND id = ? "
        "AND valid_to IS NULL",
        ("Team", "team-1"),
    )
    assert cur.fetchone()[0] == 1

    # history preserved: at least one prior (closed) row also exists.
    cur = store._conn.execute(
        "SELECT COUNT(*) FROM objects WHERE object_type = ? AND id = ?",
        ("Team", "team-1"),
    )
    assert cur.fetchone()[0] == 2


def test_bulk_upsert_unknown_key_rejected(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    report = bulk_upsert(
        store,
        registry,
        "Team",
        [{"id": "team-4", "name": "Rockets", "not_a_declared_prop": "x"}],
        Source(source_system="synthetic"),
    )

    assert not report.ok
    assert "not_a_declared_prop" in report.errors[0].reason
    assert store.read_current("Team", "team-4") is None


def test_bulk_upsert_malformed_datetime_rejected(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    report = bulk_upsert(
        store,
        registry,
        "Team",
        [{"id": "team-5", "name": "Rockets", "joined_at": "not-a-date"}],
        Source(source_system="synthetic"),
    )

    assert not report.ok
    assert "joined_at" in report.errors[0].reason
    assert store.read_current("Team", "team-5") is None


def test_bulk_upsert_valid_datetime_accepted(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    report = bulk_upsert(
        store,
        registry,
        "Team",
        [{"id": "team-6", "name": "Rockets", "joined_at": "2024-01-01T00:00:00"}],
        Source(source_system="synthetic"),
    )

    assert report.ok
    assert store.read_current("Team", "team-6") is not None


def test_bulk_upsert_refuses_owned_type_per_record_rest_proceeds(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    """AC3: a record targeting a whole-type owned object type is refused
    per-record with a typed error; the rest of the batch still proceeds."""
    report = bulk_upsert(
        store,
        registry,
        "OwnedThing",
        [
            {"id": "owned-1", "name": "x"},
            {"id": "owned-2", "name": "y"},
        ],
        Source(source_system="synthetic"),
    )

    assert not report.ok
    assert len(report.errors) == 2
    for err in report.errors:
        assert "ontology-owned" in err.reason
        assert err.code == "OWNED_TYPE_REFUSED"
    assert report.inserted_ids == []
    assert store.read_current("OwnedThing", "owned-1") is None


def test_bulk_upsert_refuses_owned_property_supplied_rest_proceeds(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    """AC3: a record supplying an owned property on an otherwise
    source-backed type is refused per-record naming the property; a
    sibling record without that property still proceeds."""
    report = bulk_upsert(
        store,
        registry,
        "Ticket",
        [
            {"id": "t1", "status": "open", "note": "sneaky"},
            {"id": "t2", "status": "open"},
        ],
        Source(source_system="synthetic"),
    )

    assert len(report.errors) == 1
    assert report.errors[0].index == 0
    assert "note" in report.errors[0].reason
    assert report.errors[0].code == "OWNED_PROPERTY_REFUSED"
    assert report.inserted_ids == ["t2"]
    assert store.read_current("Ticket", "t1") is None
    assert store.read_current("Ticket", "t2") is not None


def test_bulk_upsert_injects_owned_defaults_on_first_insert(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    report = bulk_upsert(
        store,
        registry,
        "Ticket",
        [{"id": "t3", "status": "open"}],
        Source(source_system="synthetic"),
    )

    assert report.ok
    got = store.read_current("Ticket", "t3")
    assert got is not None
    assert got.payload["note"] == "no note yet"


def test_ingest_owned_property_survives_action_edit_and_reingest(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    """The AC4/AC5 review anchor: ingest -> action-edit -> re-ingest.

    A source-backed `Ticket` is ingested (owned `note` defaulted); an
    action-style write (`store.update`, simulating a handler running
    inside the executor's capture context) then edits the owned `note`;
    a subsequent re-ingest of the SAME record with a changed source-backed
    `status` must refresh `status` from source AND preserve the
    action-written `note`.
    """
    source = Source(source_system="synthetic", source_id="seed-1")

    report1 = bulk_upsert(
        store, registry, "Ticket", [{"id": "t4", "status": "open"}], source
    )
    assert report1.ok
    got = store.read_current("Ticket", "t4")
    assert got is not None
    assert got.payload["note"] == "no note yet"

    # Action-style edit of the owned property.
    store.update("Ticket", "t4", {"note": "escalated by ops"}, source)
    edited = store.read_current("Ticket", "t4")
    assert edited is not None
    assert edited.payload["note"] == "escalated by ops"

    # Re-ingest the SAME record with changed source-backed values -- the
    # record still can never carry `note` (forbidden by _validate_record).
    report2 = bulk_upsert(
        store, registry, "Ticket", [{"id": "t4", "status": "closed"}], source
    )
    assert report2.ok

    final = store.read_current("Ticket", "t4")
    assert final is not None
    assert final.payload["status"] == "closed"  # source-backed property refreshed
    assert final.payload["note"] == "escalated by ops"  # owned property survives


def test_bulk_upsert_refuses_inside_caller_transaction(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    with store.transaction():
        with raises_code(ConflictError, "CALLER_TRANSACTION_REFUSED"):
            bulk_upsert(
                store,
                registry,
                "Team",
                [{"id": "team-txn", "name": "Rockets"}],
                Source(source_system="synthetic"),
            )


def test_bulk_link_refuses_inside_caller_transaction(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    with store.transaction():
        with raises_code(ConflictError, "CALLER_TRANSACTION_REFUSED"):
            bulk_link(
                store,
                registry,
                "belongsToDepartment",
                [("team-1", "dept-a")],
                Source(source_system="synthetic"),
            )


def test_bulk_link_refuses_owned_link_type_per_pair(
    store: ObjectStore, registry: OntologyRegistry
) -> None:
    report = bulk_link(
        store,
        registry,
        "ownedLink",
        [("team-1", "dept-a"), ("team-2", "dept-a")],
        Source(source_system="synthetic"),
    )

    assert not report.ok
    assert len(report.errors) == 2
    for err in report.errors:
        assert "ontology-owned" in err.reason
        assert err.code == "OWNED_TYPE_REFUSED"
    assert report.inserted_ids == []


def test_no_public_read_path_on_object_store() -> None:
    """AC7 groundwork: no CONSUMER-facing public read path bypasses guards.
    `ingest.py` itself calls the public, engine-internal `store.read_current`
    (internal engine code, not a consumer). `read_current`/`read_all` are a
    deliberate, documented exception (finding 5) for trusted handler/loader
    code, not a read a CONSUMER (human/AI client, MCP tool, Function) can
    reach.
    """
    public_names = {
        name
        for name in dir(ObjectStore)
        if not name.startswith("_") and callable(getattr(ObjectStore, name))
    }
    forbidden_public_reads = {"get", "get_all", "get_as_of", "query", "find"}
    assert public_names.isdisjoint(forbidden_public_reads)
    assert "read_current" in public_names
    assert "read_all" in public_names
