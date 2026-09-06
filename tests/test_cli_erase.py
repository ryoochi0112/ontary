"""CLI contract tests for operator erasure (lifecycle-queries AC9)."""

from __future__ import annotations

from pathlib import Path

import pytest

import ontary.cli as cli
from ontary.authoring import Ontology, OntologyObject, prop
from ontary.client import OntologyClient
from ontary.security import Consumer
from ontary.store import ObjectStore, Source


def _target(name: str) -> str:
    return f"{__name__}:{name}"


def _erase_ontology() -> Ontology:
    ontology = Ontology("cli-erase", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped")
    class Document(OntologyObject):
        id: str = prop(primary_key=True)
        content: str

    ontology.validate()
    return ontology


def _seed_store(path: Path) -> None:
    ontology = _erase_ontology()
    store = ObjectStore(ontology.registry, str(path))
    client = OntologyClient(
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
    report = client.ingest(
        "Document",
        [
            {"id": "keep-me", "content": "content that survives"},
            {"id": "erase-me", "content": "content to purge"},
        ],
        Source(source_system="cli-test"),
    )
    assert report.ok


def _erase_args(path: Path, object_id: str) -> list[str]:
    return [
        "erase",
        _target("_erase_ontology"),
        "--store",
        str(path),
        "--type",
        "Document",
        "--id",
        object_id,
        "--operator",
        "privacy-ops@example.com",
    ]


def test_erase_cli_erases_only_the_requested_id_and_reports_no_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store_path = tmp_path / "cli-erase.db"
    _seed_store(store_path)

    code = cli.main(_erase_args(store_path, "erase-me"))

    captured = capsys.readouterr()
    assert code == 0
    assert "erase: Document:erase-me" in captured.out
    assert "operator: privacy-ops@example.com" in captured.out
    assert "outcome: erased" in captured.out
    assert "object_rows_purged: 1" in captured.out
    assert captured.err == ""

    reopened = ObjectStore(_erase_ontology().registry, str(store_path))
    assert reopened.read_current("Document", "erase-me") is None
    kept = reopened.read_current("Document", "keep-me")
    assert kept is not None
    assert kept.payload == {"id": "keep-me", "content": "content that survives"}

    code = cli.main(_erase_args(store_path, "erase-me"))

    captured = capsys.readouterr()
    assert code == 0
    assert "outcome: no_op" in captured.out
    assert "code: OBJECT_ALREADY_ERASED" in captured.out
    assert "object_rows_purged: 0" in captured.out
    assert captured.err == ""


def test_erase_cli_not_found_is_coded_nonzero_without_touching_other_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store_path = tmp_path / "cli-erase-not-found.db"
    _seed_store(store_path)

    code = cli.main(_erase_args(store_path, "does-not-exist"))

    captured = capsys.readouterr()
    assert code == 1
    assert "refused: OBJECT_ERASURE_NOT_FOUND:" in captured.out
    assert captured.err == ""

    reopened = ObjectStore(_erase_ontology().registry, str(store_path))
    kept = reopened.read_current("Document", "keep-me")
    target = reopened.read_current("Document", "erase-me")
    assert kept is not None
    assert target is not None
    assert kept.payload["content"] == "content that survives"
    assert target.payload["content"] == "content to purge"


def test_erase_cli_defaults_operator_to_os_user(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store_path = tmp_path / "cli-erase-default-operator.db"
    _seed_store(store_path)

    code = cli.main(
        [
            "erase",
            _target("_erase_ontology"),
            "--store",
            str(store_path),
            "--type",
            "Document",
            "--id",
            "erase-me",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert f"operator: {cli.getpass.getuser()}" in captured.out


def test_non_erase_commands_do_not_resolve_default_operator(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_getuser() -> str:
        raise AssertionError("getpass.getuser() must be lazy")

    monkeypatch.setattr(cli.getpass, "getuser", fail_getuser)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])
    assert exc_info.value.code == 0
    capsys.readouterr()

    assert cli.main(["validate", _target("_erase_ontology")]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""


def test_erase_help_documents_tombstone_semantics(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["erase", "--help"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert "content from current and historical rows" in captured.out
    assert "structure and lineage survive" in captured.out
    assert "--operator" in captured.out
