"""Lineage-stamped bulk ingest API (spec AC9).

`bulk_upsert`/`bulk_link` validate each record/pair against the declared
ontology *before* writing: unknown object/link types, missing primary keys,
missing required properties, and wrong property types are rejected per-record
with a typed error collected into an `IngestReport` -- no partial silent
writes of an invalid record (spec §7). Cardinality violations on link
creation are likewise reported per-pair rather than aborting the whole batch.

This module bypasses the action pipeline (no role/scope checks, no audit --
that's the actions layer's job) but never bypasses schema validation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from pydantic_core import core_schema

from ontary.errors import ConflictError, ValidationFailed
from ontary.meta import ObjectTypeDef, OntologyRegistry, PropertyDef
from ontary.store import Source, Store
from ontary.typesys import validate_scalar


class IngestError(Exception):
    """A per-record rejection or a client-level batch failure.

    The low-level engine constructs this with `index`, `reason`, and `code` as
    a report row. The client constructs it with `report=` after the complete
    batch so the full report remains available to a caller that chooses the
    default raising policy. In either form, `code` stays one of the existing
    per-record catalogued codes: `OWNED_TYPE_REFUSED`/
    `OWNED_PROPERTY_REFUSED` (kind `authority`), `INVALID_RECORD` (kind
    `validation`), `UNKNOWN_OBJECT_TYPE`/`UNKNOWN_LINK_TYPE`, or
    `CARDINALITY_VIOLATION`."""

    index: int | None
    reason: str | None
    code: str
    report: IngestReport | None

    def __init__(
        self,
        index: int | None = None,
        reason: str | None = None,
        code: str | None = None,
        *,
        report: IngestReport | None = None,
    ) -> None:
        if report is not None:
            if report.ok:
                raise ValueError("an ingest error requires a report with failures")
            self.index = None
            self.reason = None
            self.code = report.errors[0].code
            self.report = report
            super().__init__(
                f"ingest completed with {len(report.inserted_ids)} committed "
                f"record(s) and {len(report.errors)} failed record(s)"
            )
            return

        if index is None or reason is None or code is None:
            raise TypeError(
                "an ingest report error requires index, reason, and code"
            )
        self.index = index
        self.reason = reason
        self.code = code
        self.report = None
        super().__init__(reason)

    def model_dump(self) -> dict[str, Any]:
        """Keep report serialization compatible with the former Pydantic row."""
        return {"index": self.index, "reason": self.reason, "code": self.code}

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, IngestError):
            return NotImplemented
        return self.model_dump() == other.model_dump()

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: Any
    ) -> core_schema.CoreSchema:
        def validate(value: Any) -> IngestError:
            if isinstance(value, cls):
                return value
            if isinstance(value, dict):
                return cls(
                    index=value["index"],
                    reason=value["reason"],
                    code=value["code"],
                )
            raise TypeError("an ingest error must be an IngestError or mapping")

        def serialize(value: IngestError) -> dict[str, Any]:
            return value.model_dump()

        return core_schema.no_info_plain_validator_function(
            validate,
            serialization=core_schema.plain_serializer_function_ser_schema(serialize),
        )


class IngestReport(BaseModel):
    """Outcome of a bulk_upsert / bulk_link call."""

    inserted_ids: list[str] = Field(default_factory=list)
    errors: list[IngestError] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _validate_record(
    obj_def: ObjectTypeDef, record: dict[str, Any]
) -> tuple[str, str] | None:
    """Return a `(code, reason)` rejection, or None if the record is valid.

    Authority (declared-contracts §3 AC3, §8 edge cases): a declared owned
    property is exempt from the required-property check below -- it is
    forbidden in a source record instead (a connector may never supply an
    ontology-owned property; the declared default is injected on first
    insert, and the merge in `store.update` preserves the current value on
    re-ingest) -- that refusal carries `OWNED_PROPERTY_REFUSED`; every other
    rejection here is a plain shape failure, carrying `INVALID_RECORD`.
    """
    pk_value = record.get(obj_def.primary_key)
    if pk_value is None:
        return "INVALID_RECORD", f"missing primary key {obj_def.primary_key!r}"

    prop_by_name: dict[str, PropertyDef] = {p.name: p for p in obj_def.properties}
    owned_defaults = obj_def.owned_property_defaults()

    for prop in obj_def.properties:
        if prop.name == obj_def.primary_key or prop.name in owned_defaults:
            continue
        if prop.required and prop.name not in record:
            return "INVALID_RECORD", f"missing required property {prop.name!r}"

    for key, value in record.items():
        if key in owned_defaults:
            return (
                "OWNED_PROPERTY_REFUSED",
                f"property {key!r} is ontology-owned on object type "
                f"{obj_def.api_name!r} -- a source record may not supply it",
            )
        found_prop = prop_by_name.get(key)
        if found_prop is None:
            return (
                "INVALID_RECORD",
                f"unknown property {key!r} for object type {obj_def.api_name!r}",
            )
        prop = found_prop
        if value is None:
            if prop.required:
                return "INVALID_RECORD", f"property {key!r} is required but null"
            continue
        mismatch = validate_scalar(value, prop.type, prop.choices)
        if mismatch is not None:
            return "INVALID_RECORD", f"property {key!r} {mismatch}"

    return None


def bulk_upsert(
    store: Store,
    registry: OntologyRegistry,
    obj_type: str,
    records: list[dict[str, Any]],
    source: Source,
) -> IngestReport:
    """Validate then write each record; per-record rejection, no partial writes.

    Each record is validated against the ObjectTypeDef *before* any write for
    that record happens. Invalid records are collected as typed errors
    (record index + reason) in the returned report; valid records are
    written with the given lineage `Source`. One invalid record never blocks
    or partially-writes the rest of the batch.

    A record whose primary key already has a current row is routed to
    `store.update` (closing the prior row, inserting a new current one) so
    that re-ingesting a known id updates history instead of creating a
    second `valid_to IS NULL` row for the same id. Because a source record
    can never carry an owned property (rejected by `_validate_record`
    above), that merge preserves whatever an action most recently wrote to
    an owned property while refreshing every source-backed property from
    this record (declared-contracts §3 AC4/AC5).

    Authority (declared-contracts §3 AC9): refuses outright, before writing
    anything, if `store` already has a caller-opened `transaction()` block
    open -- same reasoning as `ActionExecutor.execute()`'s refusal. In
    particular, calling `bulk_upsert` from inside a registered action
    handler body raises a `ConflictError` with code
    `CALLER_TRANSACTION_REFUSED`: `ActionExecutor.execute()` already owns
    an open `store.transaction()` for the whole duration of the handler
    call, so `bulk_upsert` sees `store.in_transaction` True and refuses --
    a handler must write via the store's own `insert`/`update`/`create_link`
    (subject to the authority checks `capture_action_writes` enforces on
    them), never via the guard-independent bulk-ingest path.
    """
    if store.in_transaction:
        raise ConflictError(
            "bulk_upsert refused: called while the caller has an open "
            "store.transaction() block -- the engine must own the "
            "transaction boundary",
            code="CALLER_TRANSACTION_REFUSED",
        )

    report = IngestReport()

    try:
        obj_def = registry.get_object_type(obj_type)
    except ValidationFailed:
        return IngestReport(
            errors=[
                IngestError(
                    index=i,
                    reason=f"unknown object type {obj_type!r}",
                    code="UNKNOWN_OBJECT_TYPE",
                )
                for i in range(len(records))
            ]
        )

    for i, record in enumerate(records):
        if obj_def.is_owned_type:
            report.errors.append(
                IngestError(
                    index=i,
                    reason=(
                        f"object type {obj_type!r} is ontology-owned "
                        "(owned=True) -- no source supplies its rows"
                    ),
                    code="OWNED_TYPE_REFUSED",
                )
            )
            continue

        rejection = _validate_record(obj_def, record)
        if rejection is not None:
            code, reason = rejection
            report.errors.append(IngestError(index=i, reason=reason, code=code))
            continue

        pk_value = record.get(obj_def.primary_key)
        current = (
            store.read_current(obj_type, str(pk_value)) if pk_value is not None else None
        )
        if current is not None:
            store.update(obj_type, str(pk_value), record, source)
            report.inserted_ids.append(str(pk_value))
        else:
            payload = dict(obj_def.owned_property_defaults())
            payload.update(record)
            obj_id = store.insert(obj_type, payload, source)
            report.inserted_ids.append(obj_id)

    return report


def bulk_link(
    store: Store,
    registry: OntologyRegistry,
    link_type: str,
    pairs: list[tuple[str, str]],
    source: Source,
) -> IngestReport:
    """Create links for each (from_id, to_id) pair; per-pair rejection.

    Cardinality violations (and unknown link types) are surfaced per-pair in
    the returned report rather than aborting the whole batch. `source` is
    accepted for symmetry with `bulk_upsert` and future lineage-on-links
    support; the current store schema does not persist link lineage.

    Authority (declared-contracts §3 AC9): refuses outright, before writing
    anything, if `store` already has a caller-opened `transaction()` block
    open. As with `bulk_upsert`, calling `bulk_link` from inside a
    registered action handler body raises a `ConflictError` with code
    `CALLER_TRANSACTION_REFUSED` for the same reason:
    `ActionExecutor.execute()` already owns an open transaction for the
    duration of the handler call.
    """
    if store.in_transaction:
        raise ConflictError(
            "bulk_link refused: called while the caller has an open "
            "store.transaction() block -- the engine must own the "
            "transaction boundary",
            code="CALLER_TRANSACTION_REFUSED",
        )

    report = IngestReport()

    try:
        link_def = registry.get_link_type(link_type)
    except ValidationFailed:
        return IngestReport(
            errors=[
                IngestError(
                    index=i,
                    reason=f"unknown link type {link_type!r}",
                    code="UNKNOWN_LINK_TYPE",
                )
                for i in range(len(pairs))
            ]
        )

    if link_def.owned is True:
        return IngestReport(
            errors=[
                IngestError(
                    index=i,
                    reason=(
                        f"link type {link_type!r} is ontology-owned "
                        "(owned=True) -- no source supplies its links"
                    ),
                    code="OWNED_TYPE_REFUSED",
                )
                for i in range(len(pairs))
            ]
        )

    for i, (from_id, to_id) in enumerate(pairs):
        try:
            store.create_link(link_type, from_id, to_id)
        except ConflictError as exc:
            if exc.code != "CARDINALITY_VIOLATION":
                raise
            report.errors.append(
                IngestError(index=i, reason=str(exc), code="CARDINALITY_VIOLATION")
            )
        else:
            report.inserted_ids.append(f"{from_id}->{to_id}")

    return report
