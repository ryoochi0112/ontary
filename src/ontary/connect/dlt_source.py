"""dlt extraction path (spec connector-framework.md §5 Approach
`dlt_source.py`, §3 AC3, §7 edge cases, §10 top risk).

`run_dlt_extract` runs a `dlt.pipeline` into a **local DuckDB staging
destination** confined entirely under `staging_dir` -- the pipeline's
working dir, its DuckDB database file, and its dataset are all disposable
and hermetic, so a failed/partial run never touches the ontology store and
a rerun is always safe (spec §7 "dlt pipeline fails mid-load"). Every
landed table is read back and handed to the caller as `RawTables` -- the
exact same shape a plain `extract()` returns -- so `transform()` never
needs to know whether its raw rows came from dlt or a hand-written
connector (AC3).

This is the *only* module in `ontary.connect` that imports `dlt`/`duckdb`,
and both are an optional extra (`uv sync --extra dlt`, or `pip install
'.[dlt]'` from a checkout) -- so the core install and `make verify`'s
default path need neither. The import is
guarded inside `run_dlt_extract` (not at module import time) precisely so
that `import ontary.connect.dlt_source` -- and therefore
`import ontary.connect` re-exporting `run_dlt_extract` -- succeeds even
without dlt installed; only *calling* `run_dlt_extract` without the extra
raises, with a message naming the extra (spec §7 "dlt extra not installed
but dlt path invoked").

dlt's own typing is intentionally loose in a few spots (`Any`-typed
`data`/`destination` parameters, an untyped duckdb cursor); the narrow
`# type: ignore`s below contain that looseness to this one module rather
than relaxing mypy config anywhere (spec §10 risk).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ontary.connect.canonical import RawTables

if TYPE_CHECKING:
    from dlt.common.pipeline import LoadInfo

_MISSING_DLT_MSG = (
    "the dlt extraction path requires the optional 'dlt' extra -- install "
    "it with `uv sync --extra dlt` (or, from a checkout, "
    "`pip install '.[dlt]'`)."
)


def run_dlt_extract(source: Any, pipeline_name: str, staging_dir: str | Path) -> RawTables:
    """Run `source` (a dlt source/resource, or a list of them) through a
    dlt pipeline into a disposable DuckDB destination under `staging_dir`,
    then read every landed table back as `RawTables`.

    `staging_dir` confines the *entire* dlt footprint for this run -- the
    pipeline's own working/state directory (`pipelines_dir`) and its
    DuckDB database file both live under it -- so nothing this call does
    persists or is shared outside the directory the caller controls (spec
    §7 "dlt pipeline fails mid-load": staging is local/disposable, rerun
    safe).

    Raises a clear `ImportError` naming `ontary[dlt]` if dlt/duckdb
    are not installed (spec §7 edge case), before doing anything else.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_MISSING_DLT_MSG) from exc

    # dlt's telemetry defaults on and phones home in a background thread;
    # `make verify` must be fully offline (spec §3 AC3 "no network
    # anywhere"), so disable it via dlt's own runtime config env var before
    # touching the pipeline. Only set if the caller hasn't already decided.
    os.environ.setdefault("RUNTIME__DLTHUB_TELEMETRY", "false")

    staging_path = Path(staging_dir)
    staging_path.mkdir(parents=True, exist_ok=True)
    pipelines_dir = staging_path / "pipelines"
    db_path = staging_path / f"{pipeline_name}.duckdb"

    pipeline = dlt.pipeline(
        pipeline_name=pipeline_name,
        pipelines_dir=str(pipelines_dir),
        destination=dlt.destinations.duckdb(str(db_path)),
        dataset_name="raw",
    )
    load_info: LoadInfo = pipeline.run(source)
    if load_info.has_failed_jobs:
        raise RuntimeError(f"dlt pipeline {pipeline_name!r} had failed load jobs: {load_info}")

    table_names = pipeline.default_schema.data_table_names()

    raw: RawTables = {}
    with pipeline.sql_client() as client:
        for table_name in table_names:
            with client.execute_query(f"select * from {table_name}") as cur:
                columns = [col[0] for col in cur.description]
                rows = cur.fetchall()
            raw[table_name] = [
                {
                    col: value
                    for col, value in zip(columns, row, strict=True)
                    if not col.startswith("_dlt_")
                }
                for row in rows
            ]

    return raw
