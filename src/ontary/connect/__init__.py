"""Domain-free connector framework: canonical staging + connector contract
(spec connector-framework.md §5 Approach).

NAMING NOTE: `SourceLineage` (from `canonical.py`) is the pre-ingest source
stamp on a canonical connector record: its source system, optional source
id, and extraction time. `ontary.Lineage` is the post-ingest read-model
lineage that a consumer reads back alongside a stored object. These are
distinct names for distinct stages of the pipeline -- import `SourceLineage`
from `ontary.connect` for the source stamp and `Lineage` from `ontary` for the
read-model lineage.
"""

from __future__ import annotations

from ontary.connect.base import (
    BaseConnector,
    to_date,
    to_datetime,
    to_optional_date,
    to_optional_datetime,
)
from ontary.connect.canonical import (
    CanonicalBatch,
    CanonicalRecord,
    RawTables,
    SourceConnector,
    SourceLineage,
)
from ontary.connect.dlt_source import run_dlt_extract
from ontary.connect.mapper import (
    LinkSkip,
    RunReport,
    map_batch,
    oid,
)
from ontary.connect.mapping import (
    LinkBinding,
    MappingSpec,
    MappingValidationError,
    ObjectBinding,
)
from ontary.connect.pipeline import run_pipeline

__all__ = [
    "BaseConnector",
    "to_date",
    "to_datetime",
    "to_optional_date",
    "to_optional_datetime",
    "CanonicalBatch",
    "CanonicalRecord",
    "RawTables",
    "SourceLineage",
    "SourceConnector",
    "LinkBinding",
    "LinkSkip",
    "MappingSpec",
    "MappingValidationError",
    "ObjectBinding",
    "RunReport",
    "map_batch",
    "oid",
    "run_dlt_extract",
    "run_pipeline",
]
