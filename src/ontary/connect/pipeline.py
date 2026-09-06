"""One obvious entry point for a connector run (spec connector-framework.md
§5 Approach `pipeline.py`, §6 data flow).

`run_pipeline` wires the stages `extract() (or pre-extracted raw) -> pure
transform() -> validate(registry) -> map_batch()` together, but each stage
stays independently callable/testable (`connector.transform()` on fixtures,
`mapping.validate()` before any run, `mapper.map_batch()` against a hand-
built `CanonicalBatch`).

`run_at` is a library-code parameter, never a `datetime.now()` call baked
into this module -- spec: "do NOT call datetime.now() in library code paths
that tests can't control". Callers (a CLI, a scheduler) default it however
they like; here the default lives at the outermost, side-effecting caller,
not inside this pure-ish orchestration function.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ontary.connect.canonical import RawTables, SourceConnector
from ontary.connect.mapper import RunReport, map_batch
from ontary.connect.mapping import MappingSpec
from ontary.ontology import OntologyDef, resolve_definition
from ontary.store import Store

if TYPE_CHECKING:
    from ontary.authoring import Ontology


def run_pipeline(
    connector: SourceConnector,
    mapping: MappingSpec,
    ontology: OntologyDef | Ontology,
    store: Store,
    *,
    raw: RawTables | None = None,
    run_at: str | None = None,
) -> RunReport:
    """Validate `mapping` against `ontology.registry`, then run
    `extract() (or `raw`) -> transform() -> map_batch()`.

    Passing `raw` is how tests/fixtures avoid the side-effecting
    `connector.extract()` (spec: "never run by `make verify`") -- when
    given, `connector.extract()` is never called at all.

    `run_at` defaults to the current UTC time captured once here, at the
    side-effecting entry point, rather than inside `map_batch`/`RunReport`
    construction -- so a caller that wants a controlled/reproducible
    timestamp (e.g. a test) can simply pass one.
    """
    definition = resolve_definition(ontology)
    mapping.validate(definition.registry)

    raw_tables = raw if raw is not None else connector.extract()
    batch = connector.transform(raw_tables)

    resolved_run_at = run_at if run_at is not None else datetime.now(timezone.utc).isoformat()

    return map_batch(
        batch,
        mapping,
        definition.registry,
        store,
        source_system=connector.name,
        run_at=resolved_run_at,
    )
