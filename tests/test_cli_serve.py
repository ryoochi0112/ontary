"""CLI contract tests for the localhost-only MCP development server."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import pytest
from conftest import _mcp_uninstalled

import ontary.cli as cli
import ontary.mcp_server as mcp_server
from ontary.authoring import Ontology, OntologyObject, prop
from ontary.security import Consumer
from ontary.store import InMemoryStore, ObjectStore, Source, Store


def _target(name: str = "_serve_ontology") -> str:
    return f"{__name__}:{name}"


def _serve_ontology() -> Ontology:
    ontology = Ontology("cli-serve", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped")
    class Document(OntologyObject):
        id: str = prop(primary_key=True)

    ontology.validate()
    return ontology


class _FakeSettings:
    host = "not-configured"
    port = -1


class _FakeServer:
    def __init__(self) -> None:
        self.settings: cli._MCPSettings = _FakeSettings()
        self.runs: list[tuple[str, str, int]] = []

    def run(
        self,
        transport: Literal["streamable-http"] = "streamable-http",
    ) -> None:
        self.runs.append((transport, self.settings.host, self.settings.port))


def test_start_dev_server_refuses_non_local_bind() -> None:
    server = _FakeServer()

    with pytest.raises(ValueError, match=r"localhost-only.*127\.0\.0\.1"):
        cli._start_dev_server(server, host="0.0.0.0", port=8123)

    assert server.settings.host == "not-configured"
    assert server.runs == []


def test_cli_exposes_no_host_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["serve", _target(), "--dev", "--host", "0.0.0.0"])

    captured = capsys.readouterr()
    assert exc_info.value.code != 0
    assert "unrecognized arguments: --host 0.0.0.0" in captured.err


def test_serve_requires_dev_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["serve", _target()])

    captured = capsys.readouterr()
    assert exc_info.value.code != 0
    assert "--dev" in captured.err


@pytest.mark.parametrize("port", ["0", "65536"])
def test_serve_refuses_out_of_range_port(
    port: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["serve", _target(), "--dev", "--port", port])

    captured = capsys.readouterr()
    assert exc_info.value.code != 0
    assert "port must be between 1 and 65535" in captured.err


def test_serve_refuses_non_integer_port(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["serve", _target(), "--dev", "--port", "eight"])

    captured = capsys.readouterr()
    assert exc_info.value.code != 0
    assert "port must be an integer" in captured.err


def test_serve_runs_streamable_http_on_localhost_and_chosen_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _FakeServer()

    def fake_build(
        _ontology: Ontology,
        _store: Store,
        _consumer: Consumer,
        *,
        name: str | None = None,
    ) -> _FakeServer:
        assert name == "cli-serve (ontary dev)"
        return server

    monkeypatch.setattr(mcp_server, "build_mcp_server", fake_build)

    assert cli.main(["serve", _target(), "--dev", "--port", "9123"]) == 0
    assert server.runs == [("streamable-http", "127.0.0.1", 9123)]


def test_serve_uses_default_port(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakeServer()

    def fake_build(*_args: Any, **_kwargs: Any) -> _FakeServer:
        return server

    monkeypatch.setattr(mcp_server, "build_mcp_server", fake_build)

    assert cli.main(["serve", _target(), "--dev"]) == 0
    assert server.runs == [("streamable-http", "127.0.0.1", 8000)]


def test_serve_binds_an_in_memory_store_and_dev_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_build(
        _ontology: Ontology,
        store: Store,
        consumer: Consumer,
        *,
        name: str | None = None,
    ) -> _FakeServer:
        captured["store"] = store
        captured["consumer"] = consumer
        return _FakeServer()

    monkeypatch.setattr(mcp_server, "build_mcp_server", fake_build)

    assert cli.main(["serve", _target(), "--dev"]) == 0
    assert isinstance(captured["store"], InMemoryStore)
    consumer = captured["consumer"]
    assert isinstance(consumer, Consumer)
    assert consumer.actor_id == "ontary-dev"
    assert consumer.role == "ontary-dev"
    assert consumer.scope_level == "dev"
    assert consumer.scope_id == "localhost"


def test_serve_tuple_target_uses_its_returned_seeded_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology = _serve_ontology()
    returned_store = InMemoryStore(ontology.registry)
    returned_store.insert(
        "Document",
        {"id": "seeded"},
        Source(source_system="cli-serve-test"),
    )
    target_module = ModuleType("cli_serve_tuple_target")
    target_module.ontology_and_store = (ontology, returned_store)
    monkeypatch.setitem(sys.modules, target_module.__name__, target_module)
    captured: dict[str, object] = {}

    def fake_build(
        _ontology: Ontology,
        store: Store,
        _consumer: Consumer,
        *,
        name: str | None = None,
    ) -> _FakeServer:
        captured["store"] = store
        return _FakeServer()

    monkeypatch.setattr(mcp_server, "build_mcp_server", fake_build)

    assert (
        cli.main(
            [
                "serve",
                f"{target_module.__name__}:ontology_and_store",
                "--dev",
            ]
        )
        == 0
    )
    assert captured["store"] is returned_store


def test_serve_store_path_binds_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_build(
        _ontology: Ontology,
        store: Store,
        _consumer: Consumer,
        *,
        name: str | None = None,
    ) -> _FakeServer:
        captured["store"] = store
        return _FakeServer()

    monkeypatch.setattr(mcp_server, "build_mcp_server", fake_build)
    database = tmp_path / "serve.db"

    assert cli.main(["serve", _target(), "--dev", "--store", str(database)]) == 0
    assert isinstance(captured["store"], ObjectStore)
    assert database.exists()


def test_serve_missing_extra_shows_existing_hint_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _mcp_uninstalled():
        code = cli.main(["serve", _target(), "--dev"])

    captured = capsys.readouterr()
    assert code == 2
    assert "pip install 'ontary[mcp]'" in captured.err
    assert "Traceback" not in captured.err


def test_serve_target_load_failure_exits_two_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["serve", "not-a-target", "--dev"])

    captured = capsys.readouterr()
    assert code == 2
    assert "target must use the form pkg.module:attr" in captured.err
    assert "Traceback" not in captured.err


def test_serve_unreadable_store_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(
        ["serve", _target(), "--dev", "--store", str(tmp_path)]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "could not open SQLite store" in captured.err


def test_serve_help_documents_fail_closed_dev_scope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["serve", "--help"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert "dev consumer sees unscoped rows only" in captured.out
    assert "unless the ontology declares matching dev scopes" in captured.out
    assert "ontary explain" in captured.out
