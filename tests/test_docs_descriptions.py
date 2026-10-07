"""Docs pins for the agent-facing descriptions feature (#65, AC9 and AC11)."""

from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parent.parent / "docs"


def _row(text: str, prefix: str) -> str:
    rows = [line for line in text.splitlines() if line.strip().startswith(prefix)]
    assert len(rows) == 1, prefix
    return rows[0]


@pytest.mark.parametrize("name", ["api-authoring.md", "api-authoring.ja.md"])
def test_accept_rows_list_missing_description(name: str) -> None:
    text = (DOCS / name).read_text(encoding="utf-8")
    for prefix in ("| `@ontology.action(...)` | `accept=`", "| `@ontology.function(...)` | `accept=`"):
        assert "MISSING_DESCRIPTION" in _row(text, prefix), (name, prefix)


def test_design_guide_descriptions_section() -> None:
    text = (DOCS / "ontology-design.md").read_text(encoding="utf-8")
    heading = "### Descriptions for agents"
    assert text.count(heading) == 1
    section = text.split(heading, 1)[1]
    section = section.split("\n#", 1)[0]
    for needle in ("what an agent reads", "description=", "Field(description=...)"):
        assert needle in section, needle
