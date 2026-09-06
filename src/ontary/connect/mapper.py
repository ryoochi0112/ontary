"""Generic canonical -> ontology mapper (spec connector-framework.md §5
Approach `mapper.py`, §3 AC4/AC5, §6 data flow, §7 edge cases).

This is the one module in `ontary.connect` that imports both the canonical
staging layer (`canonical.py`, `mapping.py`) and the ontology layer
(`ontary.ingest`, `ontary.store`) -- every write it makes is routed through
`ontary.ingest.bulk_upsert`/`bulk_link` so schema validation and
upsert-on-existing-pk always apply (AC4). It never calls `store.insert`
directly.

Idempotency (AC4, ported from the prototype's `mapper.oid`): every ontology
object id is a deterministic `uuid.uuid5` hash of
`(source_system, object_type, key)` via `oid()` below. Re-mapping the exact
same batch -- in the same store or a fresh one -- always re-derives the same
ids, so `bulk_upsert` upserts (never duplicates) and link creation is
skipped once a pair already exists.

Partial batches (AC2, AC4 "endpoint absent"): a `LinkBinding` only produces a
link when both endpoint objects already exist in the store; otherwise the
pair is skipped and counted in the returned `RunReport`, never a batch
failure.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

from ontary.connect.canonical import CanonicalBatch, CanonicalRecord, SourceLineage
from ontary.connect.mapping import LinkBinding, MappingSpec, ObjectBinding
from ontary.errors import ValidationFailed
from ontary.ingest import IngestError, bulk_link, bulk_upsert
from ontary.meta import OntologyRegistry
from ontary.store import Source as StoreSource
from ontary.store import Store

_MISSING = object()

# Fixed, hardcoded namespace for deterministic ontology ids (spec AC4
# "Idempotent ids"). MUST NEVER change -- changing it would silently break
# re-ingest idempotency for every already-mapped id.
NAMESPACE = uuid.UUID("2f6b0a9b-5c9e-4b23-9f5b-4a9f6b6e6d31")


def oid(source_system: str, object_type: str, key: str) -> str:
    """Deterministic ontology id for one canonical record.

    Same `(source_system, object_type, key)` -> the same id, forever,
    regardless of which run produced it -- a plain `uuid.uuid5(NAMESPACE, ...)`
    hash, never Python's non-deterministic `hash()`. This is the whole
    idempotency mechanism: the mapper re-derives it from the canonical
    record every time rather than minting a fresh random id, so
    `bulk_upsert`/`bulk_link` see a stable primary key across re-runs.
    """
    return str(uuid.uuid5(NAMESPACE, f"{source_system}:{object_type}:{key}"))


class LinkSkip(BaseModel):
    """One skipped link, and why (spec AC4 "skip links whose endpoints are
    absent")."""

    link_type: str
    from_key: str
    to_key: str
    reason: str


class RunReport(BaseModel):
    """Outcome of one `map_batch`/`run_pipeline` call (spec AC5).

    Counts/errors are per object type / link type so an operator can see at
    a glance what a run wrote and what it rejected or skipped; `errors` and
    `link_errors` carry every per-record/per-pair `IngestError` (schema
    rejections, cardinality violations) with its object/link type attached
    so a rejection is traceable back to the binding that produced it.
    """

    source_system: str
    run_at: str

    written: dict[str, int] = Field(default_factory=dict)
    errors: dict[str, list[IngestError]] = Field(default_factory=dict)

    # entity names of every ObjectBinding whose entity this batch never
    # emitted at all (spec AC10's allowed direction -- distinct from a
    # batch entity key with no binding, which raises a ValidationFailed with
    # code ENTITY_KEY_MISMATCH instead of being recorded here).
    entities_absent_from_batch: list[str] = Field(default_factory=list)

    links_created: dict[str, int] = Field(default_factory=dict)
    # subset of a link_type's resolved pairs whose endpoints were BOTH found
    # in this run's own ids_by_entity (spec AC9).
    links_resolved_same_batch: dict[str, int] = Field(default_factory=dict)
    # subset of a link_type's resolved pairs where at least one endpoint
    # required the store-backed cross-batch fallback (spec AC9, the
    # milestone's headline bug fix).
    links_resolved_via_store: dict[str, int] = Field(default_factory=dict)
    links_skipped: dict[str, int] = Field(default_factory=dict)
    link_skip_details: list[LinkSkip] = Field(default_factory=list)
    link_errors: dict[str, list[IngestError]] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.link_errors


def _lineage_to_source(lineage: SourceLineage) -> StoreSource:
    """Map a canonical record's own `SourceLineage` onto the store's `Source`
    stamp, faithfully (spec: "map SourceLineage -> Source faithfully")."""
    return StoreSource(
        source_system=lineage.source_system,
        source_id=lineage.source_id,
        extracted_at=lineage.extracted_at.isoformat(),
    )


def _build_payload(
    binding: ObjectBinding, record: CanonicalRecord
) -> tuple[dict[str, Any], list[str]]:
    """Build one record's ontology payload from `binding.property_map`, plus
    the list (possibly empty) of canonical fields the map references that
    are entirely ABSENT from this record (spec m35-sdk-refactor AC12).

    "Absent" (the caller's field name is missing/misspelled -- a mapping
    bug) is distinguished from "present with value `None`" (an explicitly
    nullable field -- allowed) via a sentinel default on `dict.get`, never a
    bare `.get(field)` whose `None` return silently conflates the two.
    """
    data = record.model_dump()
    payload: dict[str, Any] = {}
    missing_fields: list[str] = []
    for canonical_field, ontology_property in binding.property_map.items():
        value = data.get(canonical_field, _MISSING)
        if value is _MISSING:
            missing_fields.append(canonical_field)
            continue
        payload[ontology_property] = value
    if binding.transform is not None:
        payload.update(binding.transform(record))
    return payload, missing_fields


def _key_value(record: CanonicalRecord, field: str) -> Any:
    return record.model_dump().get(field)


def _map_objects(
    binding: ObjectBinding,
    records: list[CanonicalRecord],
    registry: OntologyRegistry,
    store: Store,
    report: RunReport,
) -> dict[Any, str]:
    """Map every record for one `ObjectBinding`, writing through
    `ingest.bulk_upsert` one record at a time so each object is stamped with
    its *own* record's lineage (spec: "per record batch") -- rather than one
    batch call sharing a single `Source`. Returns `{key_field value ->
    ontology object id}` for `LinkBinding` resolution.

    A per-record `bulk_upsert` call keeps the per-record rejection reported
    by `ingest.py` (index 0 always, for that single-record call) mapped back
    to this record's position within the entity's record list, so the
    `RunReport` still identifies which record failed.

    A `None` `key_field` value, or a `key_field` value that repeats within
    this same batch, is rejected as a per-record error rather than silently
    deriving/reusing an `oid()` -- both would otherwise collapse two
    distinct canonical records onto the same ontology id via `bulk_upsert`'s
    upsert-on-existing-pk path (silent data loss, not idempotency).
    """
    obj_def = registry.get_object_type(binding.object_type)
    ids: dict[Any, str] = {}
    written = 0
    errors: list[IngestError] = []
    seen_keys: set[Any] = set()

    for i, record in enumerate(records):
        key_value = _key_value(record, binding.key_field)
        if key_value is None:
            errors.append(
                IngestError(
                    index=i,
                    reason=f"key_field {binding.key_field!r} is None",
                    code="INVALID_RECORD",
                )
            )
            continue
        if key_value in seen_keys:
            errors.append(
                IngestError(
                    index=i,
                    reason=(
                        f"duplicate key_field {binding.key_field!r} value "
                        f"{key_value!r} within this batch"
                    ),
                    code="INVALID_RECORD",
                )
            )
            continue
        seen_keys.add(key_value)

        payload, missing_fields = _build_payload(binding, record)
        if missing_fields:
            errors.append(
                IngestError(
                    index=i,
                    reason=(
                        f"property_map canonical field(s) {missing_fields!r} "
                        f"are absent from this record (not merely None)"
                    ),
                    code="MISSING_MAPPED_FIELD",
                )
            )
            continue

        obj_id = oid(report.source_system, binding.object_type, str(key_value))
        payload[obj_def.primary_key] = obj_id

        ingest_report = bulk_upsert(
            store,
            registry,
            binding.object_type,
            [payload],
            _lineage_to_source(record.lineage),
        )
        if ingest_report.errors:
            errors.extend(
                IngestError(index=i, reason=e.reason, code=e.code)
                for e in ingest_report.errors
            )
            continue
        written += 1
        ids[key_value] = obj_id

    report.written[binding.object_type] = report.written.get(binding.object_type, 0) + written
    if errors:
        report.errors.setdefault(binding.object_type, []).extend(errors)
    return ids


def _resolve_endpoint(
    ids: dict[Any, str],
    key_value: Any,
    object_type: str | None,
    source_system: str,
    store: Store,
) -> tuple[str | None, bool]:
    """Resolve one link endpoint's ontology object id, returning `(object_id
    or None, used_store_fallback)` (spec m35-sdk-refactor AC9).

    Same-batch ids (`ids`, this call's `ids_by_entity[entity]`) are checked
    first. When the key isn't there, the store-backed cross-batch fallback
    computes the SAME deterministic id the write side would have assigned
    for that record -- `oid(source_system, object_type, str(key_value))`,
    the identical `oid()` call `_map_objects` uses -- and checks it actually
    exists via `store.read_current` before trusting it. Only when BOTH the
    same-batch lookup and the store-backed check miss is the endpoint
    unresolved (`None`, `False`).
    """
    obj_id = ids.get(key_value)
    if obj_id is not None:
        return obj_id, False
    if object_type is None or key_value is None:
        return None, False
    candidate_id = oid(source_system, object_type, str(key_value))
    if store.read_current(object_type, candidate_id) is not None:
        return candidate_id, True
    return None, False


def _map_links(
    link: LinkBinding,
    batch: CanonicalBatch,
    ids_by_entity: dict[str, dict[Any, str]],
    bound_object_types: dict[str, str],
    registry: OntologyRegistry,
    store: Store,
    report: RunReport,
) -> None:
    """Create `link.link_type` for every from-side record whose
    `from_key_field`/`to_key_field` values resolve to an ontology object id
    -- first via `ids_by_entity` (ids written by *this* `map_batch` call),
    then via a store-backed fallback that re-derives the deterministic
    `oid()` a PREVIOUS run would have assigned and checks it actually
    exists (spec m35-sdk-refactor AC9, `_resolve_endpoint` above). Only an
    endpoint that misses BOTH lookups is skipped and counted (spec §7 "Link
    endpoint absent (partial batch, cross-batch refs)"), never a batch
    failure. Re-running the same batch creates no duplicate links: an
    already-linked pair is detected via `store.links_from` (the public
    link-read API, since this is trusted mapper code, not a consumer read --
    see `store.py`'s "package-private read API" docstring) and skipped
    rather than re-sent to `bulk_link`.
    """
    from_ids = ids_by_entity.get(link.from_entity, {})
    to_ids = ids_by_entity.get(link.to_entity, {})
    from_object_type = bound_object_types.get(link.from_entity)
    to_object_type = bound_object_types.get(link.to_entity)

    created = 0
    skipped = 0
    resolved_same_batch = 0
    resolved_via_store = 0
    for record in batch.get(link.from_entity):
        from_key = _key_value(record, link.from_key_field)
        to_key = _key_value(record, link.to_key_field)
        from_id, from_via_store = _resolve_endpoint(
            from_ids, from_key, from_object_type, report.source_system, store
        )
        to_id, to_via_store = _resolve_endpoint(
            to_ids, to_key, to_object_type, report.source_system, store
        )

        if from_id is None or to_id is None:
            skipped += 1
            report.link_skip_details.append(
                LinkSkip(
                    link_type=link.link_type,
                    from_key=str(from_key),
                    to_key=str(to_key),
                    reason="endpoint object absent from this batch/store",
                )
            )
            continue

        if from_via_store or to_via_store:
            resolved_via_store += 1
        else:
            resolved_same_batch += 1

        if to_id in store.links_from(link.link_type, from_id):
            continue  # already linked (prior run, or earlier in this one): idempotent skip

        link_report = bulk_link(
            store,
            registry,
            link.link_type,
            [(from_id, to_id)],
            StoreSource(source_system=report.source_system),
        )
        if link_report.errors:
            report.link_errors.setdefault(link.link_type, []).extend(link_report.errors)
        else:
            created += 1

    report.links_created[link.link_type] = report.links_created.get(link.link_type, 0) + created
    report.links_skipped[link.link_type] = report.links_skipped.get(link.link_type, 0) + skipped
    report.links_resolved_same_batch[link.link_type] = (
        report.links_resolved_same_batch.get(link.link_type, 0) + resolved_same_batch
    )
    report.links_resolved_via_store[link.link_type] = (
        report.links_resolved_via_store.get(link.link_type, 0) + resolved_via_store
    )


def map_batch(
    batch: CanonicalBatch,
    mapping: MappingSpec,
    registry: OntologyRegistry,
    store: Store,
    *,
    source_system: str,
    run_at: str,
) -> RunReport:
    """Map one `CanonicalBatch` onto the ontology via `mapping` (spec AC4/AC5).

    Entity-key contract (spec m35-sdk-refactor AC10): a batch entity key that
    no `ObjectBinding` references raises a ValidationFailed with code
    `ENTITY_KEY_MISMATCH`. A bound
    entity the batch never emits is instead allowed -- the normal
    partial/incremental-batch shape (AC2) -- and counted in
    `RunReport.entities_absent_from_batch`.

    Every object write goes through `ingest.bulk_upsert` and every link
    through `ingest.bulk_link` -- schema validation always on, per-record/
    pair rejection carried into the returned `RunReport` rather than
    aborting the rest of the batch.
    """
    report = RunReport(source_system=source_system, run_at=run_at)

    bound_entities = {binding.entity for binding in mapping.object_bindings}
    unbound_batch_keys = sorted(set(batch.entities) - bound_entities)
    if unbound_batch_keys:
        raise ValidationFailed(
            f"CanonicalBatch contains entity key(s) {unbound_batch_keys!r} "
            f"that no ObjectBinding references -- bind them or remove them "
            f"from the batch",
            code="ENTITY_KEY_MISMATCH",
        )

    # entity -> {key_field value -> ontology object id}, needed by
    # LinkBinding resolution below.
    ids_by_entity: dict[str, dict[Any, str]] = {}
    bound_object_types: dict[str, str] = {}

    for binding in mapping.object_bindings:
        bound_object_types[binding.entity] = binding.object_type
        if binding.entity not in batch.entities:
            report.entities_absent_from_batch.append(binding.entity)
            continue
        records = batch.get(binding.entity)
        if not records:
            continue
        ids_by_entity[binding.entity] = _map_objects(binding, records, registry, store, report)

    for link in mapping.link_bindings:
        _map_links(link, batch, ids_by_entity, bound_object_types, registry, store, report)

    return report
