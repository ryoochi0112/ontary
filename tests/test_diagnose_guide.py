"""Guide URLs stay aligned with the published design guide."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import ontary.diagnose as diagnose_module
from ontary.diagnose import Finding


def _assert_guide_targets_exist() -> None:
    root = Path(__file__).resolve().parents[1]
    headings = re.findall(
        r"^#+\s+(.+)$", (root / "docs/ontology-design.md").read_text(), re.MULTILINE
    )
    slugs = {
        re.sub(r"[^\w\s-]", "", heading.lower()).replace(" ", "-")
        for heading in headings
    }
    for anchors in diagnose_module.GUIDE_ANCHORS.values():
        assert anchors
        for anchor in anchors:
            assert anchor in slugs, f"Guide heading missing: {anchor}"
    site_url = re.search(
        r"^site_url:\s*(\S+)$", (root / "mkdocs.yml").read_text(), re.MULTILINE
    )
    assert site_url is not None
    assert diagnose_module.GUIDE_URL.startswith(site_url.group(1))


def test_guide_contract_maps_all_current_advisory_codes() -> None:
    assert diagnose_module.GUIDE_URL == (
        "https://ryoochi0112.github.io/ontary/ontology-design/"
    )
    assert diagnose_module.GUIDE_ANCHORS == {
        "STORED_DERIVABLE": ("normalization-and-derived-values",),
        "MICRO_ACTION": ("action-sprawl",),
        "CRUD_ACTION_NAME": ("action-sprawl", "retirement-and-removal"),
        "FORBIDDEN_TYPE_NAME": ("the-time-machine",),
        "UNSCOPED_SENSITIVE": ("security-design",),
        "MIN_N_UNSET": ("security-design",),
    }
    assert diagnose_module.ADVISORY_CODES == diagnose_module.GUIDE_ANCHORS.keys()


def test_guide_anchors_are_headings_and_url_uses_site_url() -> None:
    _assert_guide_targets_exist()


def test_guide_heading_guard_rejects_wrong_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        diagnose_module.GUIDE_ANCHORS, "STORED_DERIVABLE", ("missing-heading",)
    )
    with pytest.raises(AssertionError, match="Guide heading missing: missing-heading"):
        _assert_guide_targets_exist()


def test_guide_site_url_guard_rejects_wrong_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diagnose_module, "GUIDE_URL", "https://example.com/design/")
    with pytest.raises(AssertionError):
        _assert_guide_targets_exist()


@pytest.mark.parametrize(
    "code",
    [
        "ONTOLOGY_INVALID",
        "SCOPE_POLICY_ERROR",
        "INVALID_RECORD",
        "RULE_VIOLATED",
        "DIAGNOSE_RULE_FAILED",
    ],
)
def test_error_findings_have_no_guide(code: str) -> None:
    finding = diagnose_module._error(code, "declaration", "invalid", "repair it")

    assert finding.guide is None
    assert finding.model_dump(mode="json")["guide"] is None


def test_finding_guide_is_optional_and_follows_fix_hint() -> None:
    assert list(Finding.model_fields) == [
        "code", "severity", "location", "message", "fix_hint", "guide"
    ]
    finding = Finding(
        code="CUSTOM", severity="info", location="custom", message="note", fix_hint="fix"
    )
    assert finding.guide is None
