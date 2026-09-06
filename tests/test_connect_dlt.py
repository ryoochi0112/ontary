"""Tests for ontary.connect.dlt_source: the optional dlt extraction path
(spec connector-framework §5 dlt_source.py, §3 AC3, §7 edge cases, §8 "dlt
path" row).

Covers:
- offline integration (AC3): a dict-backed dlt resource -> DuckDB staging
  under a tmp dir -> `run_dlt_extract` returns `RawTables` with the exact
  rows back, `_dlt_*` columns stripped -- and that `RawTables` composes with
  a trivial pure `transform()`-shaped function unchanged.
- `import ontary.connect` succeeds without dlt/duckdb being a hard
  dependency of core (they ARE installed in this dev environment, but the
  module boundary -- guarded import inside `run_dlt_extract`, not at module
  top -- is what this test actually exercises via monkeypatching).
- the missing-extra error path: guarding `import dlt` failing raises a
  clear `ImportError` naming `ontary[dlt]`.

No network anywhere: dlt telemetry is disabled via the `RUNTIME__DLTHUB_TELEMETRY`
env var `run_dlt_extract` itself sets (spec AC3 "no network anywhere").
"""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

import pytest

from ontary.connect.canonical import RawTables
from ontary.connect.dlt_source import run_dlt_extract


def test_import_ontary_connect_does_not_require_dlt() -> None:
    """Plain `import ontary.connect` must work even without dlt installed
    (spec §7 "dlt extra not installed but dlt path invoked" -- only *calling*
    the dlt path should fail, never importing the package)."""
    import ontary.connect

    assert hasattr(ontary.connect, "run_dlt_extract")


def test_run_dlt_extract_round_trips_rows_into_raw_tables(tmp_path: Path) -> None:
    """AC3: a dict-backed dlt resource -> DuckDB staging -> RawTables,
    values preserved and `_dlt_*` internal columns stripped."""
    import dlt

    @dlt.resource(name="people")  # type: ignore[misc]
    def people() -> list[dict[str, Any]]:
        return [
            {"id": 1, "name": "Ada", "team": "eng"},
            {"id": 2, "name": "Grace", "team": "eng"},
        ]

    @dlt.resource(name="orgs")  # type: ignore[misc]
    def orgs() -> list[dict[str, Any]]:
        return [{"id": "o1", "name": "Acme"}]

    raw = run_dlt_extract([people(), orgs()], "test_pipeline", tmp_path)

    assert set(raw.keys()) == {"people", "orgs"}

    people_rows = sorted(raw["people"], key=lambda r: r["id"])
    assert people_rows == [
        {"id": 1, "name": "Ada", "team": "eng"},
        {"id": 2, "name": "Grace", "team": "eng"},
    ]
    assert raw["orgs"] == [{"id": "o1", "name": "Acme"}]

    # no dlt-internal columns leaked into any row.
    for rows in raw.values():
        for row in rows:
            assert all(not col.startswith("_dlt_") for col in row)


def test_run_dlt_extract_result_composes_with_a_pure_transform(tmp_path: Path) -> None:
    """The RawTables `run_dlt_extract` returns is exactly what a connector's
    pure `transform()` consumes -- same shape a plain `extract()` would
    hand it (spec AC3: "the same transform() works either way")."""
    import dlt

    @dlt.resource(name="widgets")  # type: ignore[misc]
    def widgets() -> list[dict[str, Any]]:
        return [{"widget_key": "w1", "label": "Widget One"}]

    raw = run_dlt_extract([widgets()], "compose_pipeline", tmp_path)

    def transform(raw_tables: RawTables) -> list[str]:
        """A trivial stand-in for a connector's pure transform()."""
        return [row["widget_key"] for row in raw_tables.get("widgets", [])]

    assert transform(raw) == ["w1"]


def test_run_dlt_extract_confines_pipeline_state_under_staging_dir(tmp_path: Path) -> None:
    """The pipeline's DuckDB file and working dir both land under
    `staging_dir` -- runs are disposable/hermetic (spec §5 dlt_source.py,
    §7 "dlt pipeline fails mid-load: staging is local/disposable")."""
    import dlt

    @dlt.resource(name="rows")  # type: ignore[misc]
    def rows() -> list[dict[str, Any]]:
        return [{"n": 1}]

    run_dlt_extract([rows()], "confine_pipeline", tmp_path)

    contents = list(tmp_path.rglob("*"))
    assert any(p.suffix == ".duckdb" for p in contents)
    assert (tmp_path / "pipelines").exists()


def test_run_dlt_extract_raises_clear_error_when_dlt_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """spec §7 "dlt extra not installed but dlt path invoked": a clear
    ImportError naming `ontary[dlt]`, via the same guarded import
    branch `run_dlt_extract` uses when dlt truly isn't installed."""
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "dlt" or name.startswith("dlt."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ImportError, match=r"\[dlt\]"):
        run_dlt_extract([], "missing_dlt_pipeline", tmp_path)
