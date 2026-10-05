"""Check the DDD explanation page against repository declarations and docs."""

from __future__ import annotations

import re
from pathlib import Path

import ontary

ROOT = Path(__file__).resolve().parent.parent
EN_PAGE = ROOT / "docs" / "coming-from-ddd.md"
JA_PAGE = ROOT / "docs" / "coming-from-ddd.ja.md"
REQUIRED_TERMS = (
    "ubiquitous language",
    "bounded context",
    "context map",
    "entity",
    "value object",
    "aggregate",
    "aggregate root",
    "invariant",
    "domain event",
    "repository",
    "factory",
    "domain service",
    "application service",
    "specification",
    "anti-corruption layer",
)


def _table_rows(md: str) -> list[list[str]]:
    """Return data cells from the page's term table, excluding its headings."""
    lines = md.splitlines()
    table_start = next(i for i, line in enumerate(lines) if line.startswith("|"))
    table_lines: list[str] = []
    for line in lines[table_start:]:
        if not line.startswith("|"):
            break
        table_lines.append(line)
    return [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in table_lines[2:]
    ]


def _page_path(target: str) -> str:
    """Normalize a link to its English page path, ignoring any anchor."""
    path = target.split("#", 1)[0]
    if path.endswith(".ja.md"):
        path = f"{path[:-len('.ja.md')]}.md"
    return path


def test_ddd_page_maps_every_required_term() -> None:
    md = EN_PAGE.read_text()
    rows = _table_rows(md)
    terms = [row[0].lower() for row in rows]
    assert set(REQUIRED_TERMS) <= set(terms)
    assert len(terms) == len(set(terms))
    assert "| DDD term | In ontary | What it means here |" in md
    for row in rows:
        assert len(row) == 3
        assert re.search(r"→ \[[^\]]+\]\([^)]+\)$", row[-1]), row


def test_ddd_page_names_only_real_constructs() -> None:
    reference = (ROOT / "docs" / "api-reference.md").read_text()
    identifiers = re.findall(
        r"`([^`]+)`", "\n".join(" | ".join(row) for row in _table_rows(EN_PAGE.read_text()))
    )
    assert identifiers
    for identifier in identifiers:
        assert identifier in ontary.__all__ or identifier in reference, identifier


def test_ddd_page_aggregate_row_is_honest() -> None:
    rows = _table_rows(EN_PAGE.read_text())
    row = next(row for row in rows if row[0].lower() == "aggregate")
    text = " | ".join(row)
    assert "Not declared" in text
    assert "no aggregate boundary or root" in text
    assert "one transaction" in text
    assert "one object" in text
    assert "Cross-object checks stay in the action" in text
    assert "ontology-design.md#rules-and-status-transitions" in text


def test_ddd_page_not_supported_links_roadmap() -> None:
    md = EN_PAGE.read_text()
    section = md.split("## Not supported\n", 1)[1].split("\n## ", 1)[0]
    anchors = re.findall(r"\]\(roadmap\.md#([^)]+)\)", section)
    assert set(anchors) == {"later--pulled-by-real-use", "decided-against"}
    roadmap = (ROOT / "docs" / "roadmap.md").read_text()
    # Verified in site/roadmap/index.html: punctuation is removed before
    # whitespace becomes hyphens; the spaces around the em dash yield "--".
    headings = re.findall(r"^#+ (.+)$", roadmap, re.MULTILINE)
    slugs = {re.sub(r"[^\w\s-]", "", heading.lower()).replace(" ", "-") for heading in headings}
    assert set(anchors) <= slugs
    items = [line for line in section.splitlines() if line.startswith("- ")]
    assert items
    for item in items:
        if item.startswith("- **Later**"):
            assert "roadmap.md#later--pulled-by-real-use" in item
        else:
            assert item.startswith("- **Decided against**")
            assert "roadmap.md#decided-against" in item


def test_ddd_page_has_no_code() -> None:
    assert "```" not in EN_PAGE.read_text()


def test_ddd_page_links_design_guide_and_getting_started() -> None:
    md = EN_PAGE.read_text()
    assert md.startswith("# Coming from Domain-Driven Design\n")
    assert "](ontology-design.md)" in md
    assert "](getting-started.md)" in md
    assert "You do not need Domain-Driven Design (DDD) to use ontary." in md
    assert "## Related pages\n" in md


def test_ddd_page_en_and_ja_have_the_same_rows_and_links() -> None:
    en_rows = _table_rows(EN_PAGE.read_text())
    ja_rows = _table_rows(JA_PAGE.read_text())

    assert len(en_rows) == len(ja_rows)
    for en_row, ja_row in zip(en_rows, ja_rows, strict=True):
        assert re.fullmatch(re.escape(en_row[0]) + r"（[^）]+）", ja_row[0])

        en_targets = re.findall(r"\]\(([^)]+)\)", " | ".join(en_row))
        ja_targets = re.findall(r"\]\(([^)]+)\)", " | ".join(ja_row))
        assert len(en_targets) == len(ja_targets)
        assert sum("#" in target for target in en_targets) == sum(
            "#" in target for target in ja_targets
        )
        assert [_page_path(target) for target in en_targets] == [
            _page_path(target) for target in ja_targets
        ]
