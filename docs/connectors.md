# Connectors and canonical staging

[Back to the README](../README.md) · [API reference](api-reference.md)

`ontary.connect` is the ingestion boundary between a source system and an ontology.
It keeps vendor fields out of the domain model by making the pipeline explicit:

```text
source records → canonical records → declarative mapping → ontology writes
```

The ontology therefore survives a source replacement. A connector owns extraction
and source-specific transformation; the mapping owns how canonical entities and
relationships become declared object and link types; the ingest layer owns schema,
authority, and idempotent writes.

## Declare a canonical shape

Subclass `CanonicalRecord`. Every record carries a required `SourceLineage` stamp:
the source system, an optional source identifier, and the extraction timestamp.
Canonical fields are yours to define.

```python
from ontary import CanonicalRecord
from ontary.connect import SourceLineage


class CanonicalTicket(CanonicalRecord):
    ticket_key: str
    queue_key: str
    subject: str
    age_hours: float
```

`SourceLineage` is the pre-ingest stamp used by the connector. The read model later
exposes `ontary.store.Lineage` alongside a stored object; the two names represent
different sides of the boundary. Neither is a domain property that a connector
should smuggle into an ontology payload.

## Implement extraction and transformation

Implement the `SourceConnector` protocol or subclass `BaseConnector`. The shared
base supplies the connector name, extraction timestamp, lineage construction, and
date/datetime coercion helpers. Keep `extract()` side-effecting and
`transform(raw)` pure:

```python
from ontary.connect import BaseConnector, CanonicalBatch, RawTables


class TicketConnector(BaseConnector):
    name = "tickets"

    def extract(self) -> RawTables:
        return fetch_ticket_rows()

    def transform(self, raw: RawTables) -> CanonicalBatch:
        return CanonicalBatch(
            entities={"tickets": [self.to_canonical(row) for row in raw["tickets"]]}
        )

    def to_canonical(self, row: dict[str, object]) -> CanonicalTicket:
        return CanonicalTicket(
            lineage=self.lineage(source_id=str(row["id"])),
            ticket_key=str(row["id"]),
            queue_key=str(row["queue_id"]),
            subject=str(row["subject"]),
            age_hours=float(row["age_hours"]),
        )
```

The extraction method is not called by `make verify`; unit-test the pure transform
against fixture-shaped `RawTables`. For dlt-backed sources, `run_dlt_extract` puts
the extracted data in local DuckDB staging and returns the same raw-table shape, so
the transform and mapping do not change when extraction moves from plain Python to
dlt. The dlt integration is optional.

## Bind canonical entities to ontology types

`MappingSpec` contains `ObjectBinding` and `LinkBinding` declarations. An object
binding names the canonical entity, ontology object type, idempotent key field, and
property renames. A link binding names the declared link and the canonical keys
that resolve its endpoints.

Validate the mapping against `ontology.registry` before a run. This catches unknown
object or link types, ontology properties, canonical fields, and inconsistent link
endpoints before source data can write anything. At run time, a batch entity key
without an `ObjectBinding` raises `ValidationFailed` with code
`ENTITY_KEY_MISMATCH`. A binding whose entity is absent from a partial batch is
allowed and is reported instead of turning an incremental load into a failure.

```python
from ontary import LinkBinding, MappingSpec, ObjectBinding

mapping = MappingSpec(
    object_bindings=[
        ObjectBinding(
            entity="tickets",
            object_type="Ticket",
            key_field="ticket_key",
            property_map={"subject": "subject", "age_hours": "age_hours"},
        )
    ],
    link_bindings=[
        LinkBinding(
            link_type="ticketInQueue",
            from_entity="tickets",
            from_key_field="ticket_key",
            to_entity="queues",
            to_key_field="queue_key",
        )
    ],
)

mapping.validate(ontology.registry)
```

If a property map names a field that is absent from a particular record, the
pipeline records a per-record `MISSING_MAPPED_FIELD` error. An explicitly present
`None` remains a valid nullable value. This distinction prevents a misspelled
source field from quietly becoming an empty ontology property.

## Run the pipeline

`run_pipeline(connector, mapping, ontology, store)` wires extraction, pure
transformation, mapping validation, and ingest together. Pass `raw=` in tests to
skip the side-effecting extractor. The returned `RunReport` records object writes,
per-record errors, link creation, cross-batch resolutions, and skipped endpoints.

Object identifiers derive deterministically from source system, object type, and
canonical key through `oid`. Re-running a batch therefore upserts the same object
ids rather than creating duplicates. Link endpoints resolve first from the current
batch and then from the store using that same deterministic id; a missing endpoint
is skipped and reported, not invented.

```python
from ontary import run_pipeline

report = run_pipeline(connector, mapping, ontology, store, raw=fixtures)
if not report.ok:
    for object_type, errors in report.errors.items():
        print(object_type, errors)
```

## Runnable examples

The importable connector implementation is [`examples/tickets/connector.py`](../examples/tickets/connector.py).
Run the tickets pipeline from [`examples/tickets/run_connector.py`](../examples/tickets/run_connector.py)
with:

```console
uv run python -m examples.tickets.run_connector
```

After ingestion, consumers read through the same guarded `OntologyClient` or MCP
surface as any other data. The connector never needs a parallel authorization
policy. See [queries and pagination](queries.md) for those reads and
[storage and tenancy](storage.md) for the store boundary.

[Return to the README](../README.md) · [See connector symbols in the API reference](api-reference.md#ontaryconnect)
