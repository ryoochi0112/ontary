"""CLI contract tests (T12)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import ontary.cli as cli
from ontary.authoring import Ontology, OntologyObject, prop
from ontary.client import OntologyRuntime
from ontary.errors import ValidationFailed, VisibilityError
from ontary.explain import DecisionTrace
from ontary.scope import DirectProperty, SelfScope
from ontary.security import Consumer
from ontary.store import InMemoryStore, ObjectStore, Source


def _target(name: str) -> str:
    return f"{__name__}:{name}"


def _warning_ontology() -> Ontology:
    ontology = Ontology("cli-warning", scope_levels=["org"])

    @ontology.object(layer="L0", scope=[SelfScope(level="org")])
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        avg_response_hours: float

    return ontology


def _error_ontology() -> Ontology:
    ontology = Ontology("cli-error", scope_levels=["org"])

    @ontology.object(
        layer="L0",
        api_name="Ticket",
        version=2,
        scope=[SelfScope(level="org")],
    )
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)

    return ontology


def _explain_target() -> tuple[Ontology, InMemoryStore]:
    ontology = Ontology("cli-explain", scope_levels=["org"], min_n=1)

    @ontology.object(
        layer="L0",
        scope=[DirectProperty(level="org", property_name="org_id")],
    )
    class Document(OntologyObject):
        id: str = prop(primary_key=True)
        org_id: str

    ontology.validate()
    store = InMemoryStore(ontology.registry)
    source = Source(source_system="cli-test")
    store.insert("Document", {"id": "visible", "org_id": "org-1"}, source)
    store.insert("Document", {"id": "denied", "org_id": "org-2"}, source)
    return ontology, store


def _bare_explain_ontology() -> Ontology:
    ontology, _store = _explain_target()
    return ontology


def _raising_builder() -> Ontology:
    raise RuntimeError("builder exploded")


def test_arg_parser_captures_validate_and_explain_options() -> None:
    parser = cli._build_parser()

    validate = parser.parse_args(["validate", "module:ontology", "--json"])
    explain = parser.parse_args(
        [
            "explain",
            "module:ontology",
            "--consumer",
            "Reader:org:org-1",
            "--read",
            "Document:doc-1",
            "--json",
        ]
    )

    assert validate.command == "validate"
    assert validate.target == "module:ontology"
    assert validate.as_json is True
    assert explain.command == "explain"
    assert explain.consumer == "Reader:org:org-1"
    assert explain.read == "Document:doc-1"
    assert explain.store is None
    assert explain.as_json is True


def test_validate_warning_only_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["validate", _target("_warning_ontology")])

    captured = capsys.readouterr()
    assert code == 0
    assert "STORED_DERIVABLE" in captured.out
    assert "[WARN]" in captured.out


def test_validate_error_finding_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["validate", _target("_error_ontology"), "--json"])

    captured = capsys.readouterr()
    assert code == 1
    findings = json.loads(captured.out)
    assert any(finding["severity"] == "error" for finding in findings)


def test_validate_bogus_target_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["validate", "not_a_real_module:not_an_ontology"])

    captured = capsys.readouterr()
    assert code == 2
    assert "could not import" in captured.err
    assert "Traceback" not in captured.err


def test_validate_malformed_target_exits_two_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["validate", "malformed-target"])

    captured = capsys.readouterr()
    assert code == 2
    assert "target must use the form pkg.module:attr" in captured.err
    assert "Traceback" not in captured.err


def test_validate_missing_attribute_exits_two_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["validate", _target("missing_attribute")])

    captured = capsys.readouterr()
    assert code == 2
    assert "has no attribute 'missing_attribute'" in captured.err
    assert "Traceback" not in captured.err


def test_validate_builder_failure_exits_two_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["validate", _target("_raising_builder")])

    captured = capsys.readouterr()
    assert code == 2
    assert "could not call" in captured.err
    assert "builder exploded" in captured.err
    assert "Traceback" not in captured.err


def test_explain_malformed_consumer_exits_two_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(
        [
            "explain",
            _target("_explain_target"),
            "--consumer",
            "Reader:org",
            "--read",
            "Document:visible",
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "--consumer must use the form role:scope_level:scope_id" in captured.err
    assert "Traceback" not in captured.err


def test_explain_malformed_read_exits_two_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(
        [
            "explain",
            _target("_explain_target"),
            "--consumer",
            "Reader:org:org-1",
            "--read",
            "malformed-read",
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "--read must use the form Type:id" in captured.err
    assert "Traceback" not in captured.err


def test_erase_missing_store_is_distinct_and_does_not_create_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ontology = _bare_explain_ontology()
    missing_path = tmp_path / "mistyped-store.db"

    missing_code = cli.main(
        [
            "erase",
            _target("_bare_explain_ontology"),
            "--store",
            str(missing_path),
            "--type",
            "Document",
            "--id",
            "missing",
            "--operator",
            "cli-test",
        ]
    )
    missing_capture = capsys.readouterr()

    assert missing_code == 2
    assert not missing_path.exists()
    assert "SQLite store path does not exist" in missing_capture.err
    assert "OBJECT_ERASURE_NOT_FOUND" not in (
        missing_capture.out + missing_capture.err
    )

    existing_path = tmp_path / "empty-store.db"
    ObjectStore(ontology.registry, str(existing_path))
    existing_code = cli.main(
        [
            "erase",
            _target("_bare_explain_ontology"),
            "--store",
            str(existing_path),
            "--type",
            "Document",
            "--id",
            "missing",
            "--operator",
            "cli-test",
        ]
    )
    existing_capture = capsys.readouterr()

    assert existing_code == 1
    assert "refused: OBJECT_ERASURE_NOT_FOUND" in existing_capture.out
    assert missing_code != existing_code


def test_validate_examples_builder_works_in_a_subprocess() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ontary.cli",
            "validate",
            "examples.tickets.ontology:build_ontology",
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == []
    assert result.stderr == ""


def test_explain_visible_renders_the_trace(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(
        [
            "explain",
            _target("_explain_target"),
            "--consumer",
            "Reader:org:org-1",
            "--read",
            "Document:visible",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "verdict: visible" in captured.out
    assert "min_n:" in captured.out
    assert captured.err == ""


def test_explain_denied_renders_a_denied_trace(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(
        [
            "explain",
            _target("_explain_target"),
            "--consumer",
            "Reader:org:org-1",
            "--read",
            "Document:denied",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "verdict: denied" in captured.out
    assert "error_code: VISIBILITY_DENIED" in captured.out


def test_explain_not_found_says_read_returns_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(
        [
            "explain",
            _target("_explain_target"),
            "--consumer",
            "Reader:org:org-1",
            "--read",
            "Document:missing",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "verdict: not_found" in captured.out
    assert "read returns None / not found" in captured.out
    assert "would raise" not in captured.out


def test_explain_bare_builder_uses_in_memory_fallback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(
        [
            "explain",
            _target("_bare_explain_ontology"),
            "--consumer",
            "Reader:org:org-1",
            "--read",
            "Document:missing",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "verdict: not_found" in captured.out
    assert "read returns None / not found" in captured.out


def test_explain_store_option_reads_seeded_sqlite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ontology = _bare_explain_ontology()
    store_path = tmp_path / "cli-explain.db"
    store = ObjectStore(ontology.registry, str(store_path))
    store.insert(
        "Document",
        {"id": "sqlite-visible", "org_id": "org-1"},
        Source(source_system="cli-test"),
    )

    code = cli.main(
        [
            "explain",
            _target("_bare_explain_ontology"),
            "--consumer",
            "Reader:org:org-1",
            "--read",
            "Document:sqlite-visible",
            "--store",
            str(store_path),
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "verdict: visible" in captured.out
    assert captured.err == ""


def test_explain_rejects_unknown_type_before_reading(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(
        [
            "explain",
            _target("_explain_target"),
            "--consumer",
            "Reader:org:org-1",
            "--read",
            "Typo:missing",
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "UNKNOWN_OBJECT_TYPE" in captured.out
    assert "not_found" not in captured.out


@pytest.mark.parametrize("exception_type", [VisibilityError, ValidationFailed])
def test_explain_refusal_is_rendered_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exception_type: type[VisibilityError] | type[ValidationFailed],
) -> None:
    def raise_refusal(
        _runtime: OntologyRuntime,
        _consumer: Consumer,
        _object_type: str,
        _object_id: str,
    ) -> DecisionTrace:
        raise exception_type("operator refusal", code="VISIBILITY_DENIED")

    monkeypatch.setattr(OntologyRuntime, "explain_read", raise_refusal)

    code = cli.main(
        [
            "explain",
            _target("_explain_target"),
            "--consumer",
            "Reader:org:org-1",
            "--read",
            "Document:visible",
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "refused: VISIBILITY_DENIED: operator refusal" in captured.out
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_explain_json_is_a_decision_trace(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(
        [
            "explain",
            _target("_explain_target"),
            "--consumer",
            "Reader:org:org-1",
            "--read",
            "Document:visible",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["verdict"] == "visible"
    assert payload["min_n"]["outcome"] == "not_applicable"


def test_version_prints_ontary_version(capsys: pytest.CaptureFixture[str]) -> None:
    import ontary

    code = cli.main(["version"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == ontary.__version__
    assert captured.err == ""
