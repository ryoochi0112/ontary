"""CLI contract tests (T12)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import ontary.cli as cli
from ontary import Ontology, OntologyObject, SelfScope, Sensitivity, prop
from ontary.diagnose import Finding


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


def _clean_ontology() -> Ontology:
    ontology = Ontology("cli-clean", scope_levels=["org"])

    @ontology.object(layer="L0", scope=[SelfScope(level="org")])
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        title: str

    return ontology


def _accepted_warning_ontology() -> Ontology:
    ontology = Ontology("cli-accepted", scope_levels=["org"])

    @ontology.object(layer="L0", scope=[SelfScope(level="org")])
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        avg_response_hours: float = prop(accept="STORED_DERIVABLE")

    return ontology


def _info_only_ontology() -> Ontology:
    ontology = Ontology("cli-info", scope_levels=["org"])

    @ontology.object(layer="L0", scope=[SelfScope(level="org")])
    class Person(OntologyObject):
        id: str = prop(primary_key=True)
        email: str | None = prop(
            default=None,
            sensitivity=Sensitivity(human_visible=False),
        )

    return ontology


def _raising_builder() -> Ontology:
    raise RuntimeError("builder exploded")


def test_arg_parser_captures_validate_options() -> None:
    parser = cli._build_parser()

    validate = parser.parse_args(["validate", "module:ontology", "--json"])

    assert validate.command == "validate"
    assert validate.target == "module:ontology"
    assert validate.as_json is True

    strict = parser.parse_args(["validate", "module:ontology", "--strict"])
    assert strict.strict is True


def test_validate_warning_only_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["validate", _target("_warning_ontology")])

    captured = capsys.readouterr()
    assert code == 0
    assert "STORED_DERIVABLE" in captured.out
    assert "[WARN]" in captured.out


def test_validate_strict_warning_exits_one_and_json_is_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    normal_code = cli.main(["validate", _target("_warning_ontology"), "--json"])
    normal_output = capsys.readouterr().out

    strict_code = cli.main(
        ["validate", _target("_warning_ontology"), "--strict", "--json"]
    )
    strict_output = capsys.readouterr().out

    assert normal_code == 0
    assert strict_code == 1
    assert json.loads(strict_output) == json.loads(normal_output)
    assert any(item["severity"] == "warn" for item in json.loads(strict_output))


def test_validate_strict_clean_ontology_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["validate", _target("_clean_ontology"), "--strict", "--json"])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == []


def test_validate_strict_info_only_ontology_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(
        ["validate", _target("_info_only_ontology"), "--strict", "--json"]
    )

    findings = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [item["code"] for item in findings] == ["MIN_N_UNSET"]
    assert [item["severity"] for item in findings] == ["info"]


def test_validate_strict_accepted_warning_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(
        ["validate", _target("_accepted_warning_ontology"), "--strict", "--json"]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out) == []


def test_validate_error_finding_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["validate", _target("_error_ontology"), "--json"])

    captured = capsys.readouterr()
    assert code == 1
    findings = json.loads(captured.out)
    assert any(finding["severity"] == "error" for finding in findings)


def test_validate_strict_error_finding_still_exits_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["validate", _target("_error_ontology"), "--strict", "--json"])

    assert code == 1
    findings = json.loads(capsys.readouterr().out)
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
            "--strict",
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
        "the name reads as a score or aggregate\n"
        "  fix: if Ticket.avg_response_hours is computed from other rows, "
        "derive it with a Function; if it is recorded from outside, "
        'add accept="STORED_DERIVABLE" to the property\n'
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
