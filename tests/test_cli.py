"""CLI contract tests (T12)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import ontary.cli as cli
from ontary.authoring import Ontology, OntologyObject, prop
from ontary.diagnose import Finding
from ontary.scope import SelfScope


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
        owned={"missing": "x"},
        scope=[SelfScope(level="org")],
    )
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)

    return ontology


def _raising_builder() -> Ontology:
    raise RuntimeError("builder exploded")


def test_arg_parser_captures_validate_options() -> None:
    parser = cli._build_parser()

    validate = parser.parse_args(["validate", "module:ontology", "--json"])

    assert validate.command == "validate"
    assert validate.target == "module:ontology"
    assert validate.as_json is True


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


def test_version_prints_ontary_version(capsys: pytest.CaptureFixture[str]) -> None:
    import ontary

    code = cli.main(["version"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == ontary.__version__
    assert captured.err == ""


@pytest.mark.parametrize(
    ("builder", "expected_guide"),
    [
        (
            "_warning_ontology",
            "https://ryoochi0112.github.io/ontary/ontology-design/"
            "#normalization-and-derived-values",
        ),
        ("_error_ontology", None),
    ],
)
def test_validate_json_includes_guide_on_every_finding(
    builder: str, expected_guide: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["validate", _target(builder), "--json"])

    findings = json.loads(capsys.readouterr().out)
    assert findings
    assert all(finding["guide"] == expected_guide for finding in findings)


def test_validate_text_prints_guide_immediately_after_fix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.main(["validate", _target("_warning_ontology")])

    assert capsys.readouterr().out == (
        "[WARN] STORED_DERIVABLE at object Ticket, property avg_response_hours: "
        "looks like a stored aggregate; facts are stored once and derived by Functions\n"
        "  fix: declare a Function that computes it from source rows, "
        "or mark the type as a declared snapshot\n"
        "  guide: https://ryoochi0112.github.io/ontary/ontology-design/"
        "#normalization-and-derived-values\n"
    )


def test_render_findings_omits_guide_line_when_unset(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._render_findings(
        [Finding(
            code="ONTOLOGY_INVALID",
            severity="error",
            location="object Ticket",
            message="invalid declaration",
            fix_hint="repair the declaration",
        )],
        as_json=False,
    )

    assert capsys.readouterr().out == (
        "[ERROR] ONTOLOGY_INVALID at object Ticket: invalid declaration\n"
        "  fix: repair the declaration\n"
    )
