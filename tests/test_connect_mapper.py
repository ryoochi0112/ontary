"""Tests for ontary.connect.mapper / pipeline: the generic canonical ->
ontology mapper, `RunReport`, and `run_pipeline` (spec connector-framework
§5 mapper.py/pipeline.py, §3 AC4/AC5, §7 edge cases)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from conftest import raises_code

from ontary.connect import (
    CanonicalBatch,
    CanonicalRecord,
    LinkBinding,
    MappingSpec,
    ObjectBinding,
    SourceLineage,
    map_batch,
    oid,
    run_pipeline,
)
from ontary.connect.canonical import RawTables
from ontary.errors import ValidationFailed
from ontary.meta import (
    Cardinality,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
)
from ontary.ontology import OntologyDef
from ontary.scope import ScopePolicy
from ontary.store import ObjectStore

RUN_AT = "2026-07-23T00:00:00+00:00"


# -- toy canonical models + ontology (non-DSO, ≤ this file) -----------------


class Widget(CanonicalRecord):
    widget_key: str
    label: str


class Gadget(CanonicalRecord):
    gadget_key: str
    widget_key: str
    note: str


def make_lineage(source_id: str | None = None) -> SourceLineage:
    return SourceLineage(
        source_system="acme", source_id=source_id, extracted_at=datetime(2026, 1, 1)
    )


def toy_registry() -> OntologyRegistry:
    registry = OntologyRegistry()
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Widget",
            display_name="Widget",
            description="A widget",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="name", type="str"),
            ],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Gadget",
            display_name="Gadget",
            description="A gadget",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="memo", type="str"),
            ],
            primary_key="id",
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="gadgetOfWidget",
            from_type="Gadget",
            to_type="Widget",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Gadget belongs to Widget",
        )
    )
    return registry


def toy_mapping() -> MappingSpec:
    return MappingSpec(
        object_bindings=[
            ObjectBinding(
                entity="widgets",
                object_type="Widget",
                key_field="widget_key",
                property_map={"label": "name"},
                record_model=Widget,
            ),
            ObjectBinding(
                entity="gadgets",
                object_type="Gadget",
                key_field="gadget_key",
                property_map={"note": "memo"},
                record_model=Gadget,
                transform=lambda r: {"memo": f"{r.note} (transformed)"},  # type: ignore[union-attr]
            ),
        ],
        link_bindings=[
            LinkBinding(
                link_type="gadgetOfWidget",
                from_entity="gadgets",
                from_key_field="gadget_key",
                to_entity="widgets",
                to_key_field="widget_key",
            )
        ],
    )


def toy_batch(*, with_orphan_gadget: bool = False) -> CanonicalBatch:
    widgets = [
        Widget(widget_key="w1", label="Widget One", lineage=make_lineage("w1")),
        Widget(widget_key="w2", label="Widget Two", lineage=make_lineage("w2")),
    ]
    gadgets = [
        Gadget(
            gadget_key="g1", widget_key="w1", note="first", lineage=make_lineage("g1")
        ),
    ]
    if with_orphan_gadget:
        gadgets.append(
            Gadget(
                gadget_key="g2",
                widget_key="missing-widget",
                note="orphan",
                lineage=make_lineage("g2"),
            )
        )
    return CanonicalBatch(entities={"widgets": widgets, "gadgets": gadgets})


def make_store(registry: OntologyRegistry) -> ObjectStore:
    return ObjectStore(registry)


# -- oid ----------------------------------------------------------------


def test_oid_is_deterministic() -> None:
    a = oid("acme", "Widget", "w1")
    b = oid("acme", "Widget", "w1")
    assert a == b


def test_oid_differs_by_source_type_or_key() -> None:
    base = oid("acme", "Widget", "w1")
    assert oid("other", "Widget", "w1") != base
    assert oid("acme", "Gadget", "w1") != base
    assert oid("acme", "Widget", "w2") != base


# -- map_batch: objects + links + transform overlay ----------------------


def test_map_batch_writes_objects_and_links() -> None:
    registry = toy_registry()
    store = make_store(registry)
    mapping = toy_mapping()
    batch = toy_batch()

    report = map_batch(
        batch, mapping, registry, store, source_system="acme", run_at=RUN_AT
    )

    assert report.written == {"Widget": 2, "Gadget": 1}
    assert report.errors == {}
    assert report.links_created == {"gadgetOfWidget": 1}
    assert report.links_skipped.get("gadgetOfWidget", 0) == 0

    widget_id = oid("acme", "Widget", "w1")
    gadget_id = oid("acme", "Gadget", "g1")
    widget_row = store.read_current("Widget", widget_id)
    assert widget_row is not None
    assert widget_row.payload["name"] == "Widget One"

    gadget_row = store.read_current("Gadget", gadget_id)
    assert gadget_row is not None
    # transform overlay ran over the property_map result
    assert gadget_row.payload["memo"] == "first (transformed)"

    assert store.links_from("gadgetOfWidget", gadget_id) == [widget_id]


def test_map_batch_links_skipped_for_missing_endpoint() -> None:
    registry = toy_registry()
    store = make_store(registry)
    mapping = toy_mapping()
    batch = toy_batch(with_orphan_gadget=True)

    report = map_batch(
        batch, mapping, registry, store, source_system="acme", run_at=RUN_AT
    )

    # the orphan gadget's own object write still succeeds
    assert report.written["Gadget"] == 2
    assert report.links_created["gadgetOfWidget"] == 1
    assert report.links_skipped["gadgetOfWidget"] == 1
    assert len(report.link_skip_details) == 1
    skip = report.link_skip_details[0]
    assert skip.link_type == "gadgetOfWidget"
    assert skip.to_key == "missing-widget"

    orphan_gadget_id = oid("acme", "Gadget", "g2")
    assert store.links_from("gadgetOfWidget", orphan_gadget_id) == []


def test_rerun_creates_no_duplicate_objects_or_links() -> None:
    registry = toy_registry()
    store = make_store(registry)
    mapping = toy_mapping()
    batch = toy_batch()

    map_batch(batch, mapping, registry, store, source_system="acme", run_at=RUN_AT)
    second = map_batch(
        batch, mapping, registry, store, source_system="acme", run_at=RUN_AT
    )

    assert second.written == {"Widget": 2, "Gadget": 1}
    assert second.links_created == {"gadgetOfWidget": 0}

    widget_id = oid("acme", "Widget", "w1")
    gadget_id = oid("acme", "Gadget", "g1")
    assert store.links_from("gadgetOfWidget", gadget_id) == [widget_id]
    assert len(store.read_all("Widget")) == 2
    assert len(store.read_all("Gadget")) == 1


def test_rerun_updates_changed_properties() -> None:
    registry = toy_registry()
    store = make_store(registry)
    mapping = toy_mapping()
    batch = toy_batch()

    map_batch(batch, mapping, registry, store, source_system="acme", run_at=RUN_AT)

    updated_batch = CanonicalBatch(
        entities={
            "widgets": [
                Widget(widget_key="w1", label="Widget One Renamed", lineage=make_lineage("w1")),
                Widget(widget_key="w2", label="Widget Two", lineage=make_lineage("w2")),
            ],
            "gadgets": batch.get("gadgets"),
        }
    )
    map_batch(
        updated_batch, mapping, registry, store, source_system="acme", run_at=RUN_AT
    )

    widget_id = oid("acme", "Widget", "w1")
    row = store.read_current("Widget", widget_id)
    assert row is not None
    assert row.payload["name"] == "Widget One Renamed"
    assert len(store.read_all("Widget")) == 2


def test_schema_invalid_record_rejected_rest_proceeds() -> None:
    """A record whose ontology property has the wrong type is rejected as
    a per-record IngestError; the rest of the batch still writes (AC5)."""
    registry = toy_registry()
    store = make_store(registry)
    mapping = toy_mapping()

    class BadWidget(CanonicalRecord):
        widget_key: str
        label: Any

    batch = CanonicalBatch(
        entities={
            "widgets": [
                BadWidget(widget_key="w1", label=123, lineage=make_lineage("w1")),
                Widget(widget_key="w2", label="Widget Two", lineage=make_lineage("w2")),
            ],
            "gadgets": [],
        }
    )

    report = map_batch(
        batch, mapping, registry, store, source_system="acme", run_at=RUN_AT
    )

    assert report.written["Widget"] == 1
    assert "Widget" in report.errors
    assert len(report.errors["Widget"]) == 1
    assert not report.ok

    good_id = oid("acme", "Widget", "w2")
    assert store.read_current("Widget", good_id) is not None
    bad_id = oid("acme", "Widget", "w1")
    assert store.read_current("Widget", bad_id) is None


def test_none_key_value_rejected_not_silently_collapsed() -> None:
    """Two records with a None key_field value must NOT collapse onto the
    same oid() (str(None) -> "None") -- each is rejected per-record, the
    rest of the batch proceeds."""
    registry = toy_registry()
    store = make_store(registry)
    mapping = toy_mapping()

    class NullableWidget(CanonicalRecord):
        widget_key: str | None
        label: str

    batch = CanonicalBatch(
        entities={
            "widgets": [
                NullableWidget(widget_key=None, label="Ghost One", lineage=make_lineage("g1")),
                NullableWidget(widget_key=None, label="Ghost Two", lineage=make_lineage("g2")),
                Widget(widget_key="w1", label="Widget One", lineage=make_lineage("w1")),
            ],
            "gadgets": [],
        }
    )

    report = map_batch(
        batch, mapping, registry, store, source_system="acme", run_at=RUN_AT
    )

    assert report.written["Widget"] == 1
    assert len(report.errors["Widget"]) == 2
    for err in report.errors["Widget"]:
        assert "widget_key" in err.reason
        assert "None" in err.reason

    # no object was ever written for either None-keyed record: they did not
    # collapse onto a shared oid("acme", "Widget", "None")
    assert len(store.read_all("Widget")) == 1
    good_id = oid("acme", "Widget", "w1")
    assert store.read_current("Widget", good_id) is not None


def test_duplicate_key_within_batch_rejected_not_silently_overwritten() -> None:
    """A second record reusing the same key_field value within one batch is
    rejected per-record rather than silently self-overwriting the first via
    bulk_upsert's upsert-on-existing-pk path."""
    registry = toy_registry()
    store = make_store(registry)
    mapping = toy_mapping()

    batch = CanonicalBatch(
        entities={
            "widgets": [
                Widget(widget_key="w1", label="First Copy", lineage=make_lineage("w1a")),
                Widget(widget_key="w1", label="Second Copy", lineage=make_lineage("w1b")),
                Widget(widget_key="w2", label="Widget Two", lineage=make_lineage("w2")),
            ],
            "gadgets": [],
        }
    )

    report = map_batch(
        batch, mapping, registry, store, source_system="acme", run_at=RUN_AT
    )

    assert report.written["Widget"] == 2
    assert len(report.errors["Widget"]) == 1
    assert report.errors["Widget"][0].index == 1
    assert "duplicate" in report.errors["Widget"][0].reason
    assert "w1" in report.errors["Widget"][0].reason

    widget_id = oid("acme", "Widget", "w1")
    row = store.read_current("Widget", widget_id)
    assert row is not None
    # the FIRST record's value won -- the duplicate never touched the store
    assert row.payload["name"] == "First Copy"
    assert len(store.read_all("Widget")) == 2


def test_partial_batch_only_present_entities_mapped() -> None:
    registry = toy_registry()
    store = make_store(registry)
    mapping = toy_mapping()
    batch = CanonicalBatch(entities={"widgets": toy_batch().get("widgets")})

    report = map_batch(
        batch, mapping, registry, store, source_system="acme", run_at=RUN_AT
    )

    assert report.written == {"Widget": 2}
    assert "Gadget" not in report.written
    assert report.links_created.get("gadgetOfWidget", 0) == 0
    assert report.links_skipped.get("gadgetOfWidget", 0) == 0


def test_lineage_mapped_to_store_source() -> None:
    registry = toy_registry()
    store = make_store(registry)
    mapping = toy_mapping()
    batch = toy_batch()

    map_batch(batch, mapping, registry, store, source_system="acme", run_at=RUN_AT)

    widget_id = oid("acme", "Widget", "w1")
    row = store.read_current("Widget", widget_id)
    assert row is not None
    assert row.lineage.source_system == "acme"
    assert row.lineage.source_id == "w1"
    assert row.lineage.extracted_at == datetime(2026, 1, 1).isoformat()


def test_run_report_lineage_fields() -> None:
    registry = toy_registry()
    store = make_store(registry)
    mapping = toy_mapping()
    batch = toy_batch()

    report = map_batch(
        batch, mapping, registry, store, source_system="acme", run_at=RUN_AT
    )
    assert report.source_system == "acme"
    assert report.run_at == RUN_AT


# -- run_pipeline: connector -> transform -> map_batch --------------------


class ToyConnector:
    name = "acme"

    def extract(self) -> RawTables:
        raise AssertionError("extract() must never be called when raw is given")

    def transform(self, raw: RawTables) -> CanonicalBatch:
        widgets = [
            Widget(
                widget_key=row["key"],
                label=row["label"],
                lineage=make_lineage(row["key"]),
            )
            for row in raw.get("widgets", [])
        ]
        gadgets = [
            Gadget(
                gadget_key=row["key"],
                widget_key=row["widget_key"],
                note=row["note"],
                lineage=make_lineage(row["key"]),
            )
            for row in raw.get("gadgets", [])
        ]
        return CanonicalBatch(entities={"widgets": widgets, "gadgets": gadgets})


def toy_ontology(registry: OntologyRegistry) -> OntologyDef:
    return OntologyDef(
        name="toy",
        registry=registry,
        policy=ScopePolicy(
            levels=["global"], unscoped_types={"Widget", "Gadget"}
        ),
    )


def test_run_pipeline_end_to_end_with_raw_fixture() -> None:
    registry = toy_registry()
    store = make_store(registry)
    mapping = toy_mapping()
    ontology = toy_ontology(registry)

    raw: RawTables = {
        "widgets": [
            {"key": "w1", "label": "Widget One"},
            {"key": "w2", "label": "Widget Two"},
        ],
        "gadgets": [{"key": "g1", "widget_key": "w1", "note": "first"}],
    }

    report = run_pipeline(
        ToyConnector(), mapping, ontology, store, raw=raw, run_at=RUN_AT
    )

    assert report.written == {"Widget": 2, "Gadget": 1}
    assert report.links_created == {"gadgetOfWidget": 1}

    widget_id = oid("acme", "Widget", "w1")
    gadget_id = oid("acme", "Gadget", "g1")
    gadget_row = store.read_current("Gadget", gadget_id)
    assert gadget_row is not None
    assert gadget_row.payload["memo"] == "first (transformed)"
    assert store.links_from("gadgetOfWidget", gadget_id) == [widget_id]


def test_run_pipeline_rejects_dangling_binding_before_run() -> None:
    registry = toy_registry()
    store = make_store(registry)
    ontology = toy_ontology(registry)

    bad_mapping = MappingSpec(
        object_bindings=[
            ObjectBinding(
                entity="widgets",
                object_type="NotARealType",
                key_field="widget_key",
                property_map={},
            )
        ]
    )

    with pytest.raises(Exception):
        run_pipeline(
            ToyConnector(), bad_mapping, ontology, store, raw={}, run_at=RUN_AT
        )


# -- cross-batch links (spec AC9, m35-sdk-refactor) -----------------------


def test_cross_batch_link_resolves_via_store_fallback() -> None:
    """The milestone's headline bug: orgs (widgets) ingested in run 1,
    memberships (gadgets) ingested in run 2 -- the link must still be
    created via a store-backed id-derivation fallback, not silently
    skipped just because the widget wasn't written by *this* `map_batch`
    call's `ids_by_entity`."""
    registry = toy_registry()
    store = make_store(registry)
    mapping = toy_mapping()
    full_batch = toy_batch()

    run1 = map_batch(
        CanonicalBatch(entities={"widgets": full_batch.get("widgets"), "gadgets": []}),
        mapping,
        registry,
        store,
        source_system="acme",
        run_at=RUN_AT,
    )
    assert run1.written == {"Widget": 2}
    assert run1.links_created.get("gadgetOfWidget", 0) == 0

    run2 = map_batch(
        CanonicalBatch(entities={"widgets": [], "gadgets": full_batch.get("gadgets")}),
        mapping,
        registry,
        store,
        source_system="acme",
        run_at=RUN_AT,
    )

    assert run2.written == {"Gadget": 1}
    assert run2.links_created == {"gadgetOfWidget": 1}
    assert run2.links_resolved_via_store.get("gadgetOfWidget") == 1
    assert run2.links_resolved_same_batch.get("gadgetOfWidget", 0) == 0
    assert run2.links_skipped.get("gadgetOfWidget", 0) == 0

    widget_id = oid("acme", "Widget", "w1")
    gadget_id = oid("acme", "Gadget", "g1")
    assert store.links_from("gadgetOfWidget", gadget_id) == [widget_id]


def test_cross_batch_link_truly_missing_endpoint_still_skipped_and_counted() -> None:
    """A store-fallback miss is still a skip, never a raise -- and it is
    counted distinctly from a same-batch skip (spec AC9/§8)."""
    registry = toy_registry()
    store = make_store(registry)
    mapping = toy_mapping()

    map_batch(
        CanonicalBatch(entities={"widgets": [], "gadgets": []}),
        mapping,
        registry,
        store,
        source_system="acme",
        run_at=RUN_AT,
    )

    orphan_gadgets = toy_batch(with_orphan_gadget=True).get("gadgets")
    report = map_batch(
        CanonicalBatch(entities={"widgets": [], "gadgets": orphan_gadgets}),
        mapping,
        registry,
        store,
        source_system="acme",
        run_at=RUN_AT,
    )

    # neither "w1" (real widget, but never ingested by any run) nor
    # "missing-widget" (never a real widget) resolves, same-batch or
    # store-backed.
    assert report.links_created.get("gadgetOfWidget", 0) == 0
    assert report.links_resolved_via_store.get("gadgetOfWidget", 0) == 0
    assert report.links_skipped.get("gadgetOfWidget", 0) == 2
    assert len(report.link_skip_details) == 2


# -- entity-key contract (spec AC10, m35-sdk-refactor) ---------------------


def test_map_batch_raises_on_batch_key_no_binding_references() -> None:
    """A batch entity key no `ObjectBinding` references is the "typo entity
    name -> silently zero objects" bug AC10 targets -- fails loudly rather
    than being silently ignored by `CanonicalBatch.get`."""
    registry = toy_registry()
    store = make_store(registry)
    mapping = toy_mapping()
    batch = CanonicalBatch(
        entities={
            "widgets": toy_batch().get("widgets"),
            "gadgets": toy_batch().get("gadgets"),
            "wigdets_typo": [],
        }
    )

    with raises_code(ValidationFailed, "ENTITY_KEY_MISMATCH"):
        map_batch(batch, mapping, registry, store, source_system="acme", run_at=RUN_AT)


def test_map_batch_allows_binding_entity_absent_from_batch_and_counts_it() -> None:
    """The OTHER direction -- a binding whose entity this batch never
    emits -- is explicitly allowed (partial/incremental batches are the
    normal case, spec AC2) but counted distinctly in `RunReport` rather
    than silently disappearing."""
    registry = toy_registry()
    store = make_store(registry)
    mapping = toy_mapping()
    batch = CanonicalBatch(entities={"widgets": toy_batch().get("widgets")})

    report = map_batch(
        batch, mapping, registry, store, source_system="acme", run_at=RUN_AT
    )

    assert report.written == {"Widget": 2}
    assert report.entities_absent_from_batch == ["gadgets"]


# -- missing property_map field (spec AC12, m35-sdk-refactor) --------------


def test_missing_mapped_field_is_per_record_error_not_silent_none() -> None:
    """A `property_map` canonical field ENTIRELY ABSENT from the record is a
    per-record ingest error, not a silently-written `None` property."""
    registry = toy_registry()
    store = make_store(registry)
    mapping = toy_mapping()

    class SparseWidget(CanonicalRecord):
        widget_key: str
        # no `label` field at all -- unlike `Widget`, whose `label` maps to
        # ontology property `name` via toy_mapping()'s property_map.

    batch = CanonicalBatch(
        entities={
            "widgets": [
                SparseWidget(widget_key="w1", lineage=make_lineage("w1")),
                Widget(widget_key="w2", label="Widget Two", lineage=make_lineage("w2")),
            ],
            "gadgets": [],
        }
    )

    report = map_batch(
        batch, mapping, registry, store, source_system="acme", run_at=RUN_AT
    )

    assert report.written["Widget"] == 1
    assert len(report.errors["Widget"]) == 1
    assert report.errors["Widget"][0].code == "MISSING_MAPPED_FIELD"
    assert "label" in report.errors["Widget"][0].reason

    missing_id = oid("acme", "Widget", "w1")
    assert store.read_current("Widget", missing_id) is None
    good_id = oid("acme", "Widget", "w2")
    assert store.read_current("Widget", good_id) is not None


def test_explicit_none_mapped_field_value_stays_none_not_an_error() -> None:
    """A mapped field explicitly present with value `None` is NOT the AC12
    bug -- nullable fields exist and must keep working."""
    registry = OntologyRegistry()
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Thing",
            display_name="Thing",
            description="A thing",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="nickname", type="str", required=False),
            ],
            primary_key="id",
        )
    )
    store = make_store(registry)

    class Thing(CanonicalRecord):
        thing_key: str
        nickname: str | None

    mapping = MappingSpec(
        object_bindings=[
            ObjectBinding(
                entity="things",
                object_type="Thing",
                key_field="thing_key",
                property_map={"nickname": "nickname"},
                record_model=Thing,
            )
        ]
    )
    batch = CanonicalBatch(
        entities={
            "things": [Thing(thing_key="t1", nickname=None, lineage=make_lineage("t1"))]
        }
    )

    report = map_batch(
        batch, mapping, registry, store, source_system="acme", run_at=RUN_AT
    )

    assert report.written == {"Thing": 1}
    assert report.errors == {}

    thing_id = oid("acme", "Thing", "t1")
    row = store.read_current("Thing", thing_id)
    assert row is not None
    assert row.payload["nickname"] is None
