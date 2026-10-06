"""Check the DDD explanation page against repository declarations and docs."""

from __future__ import annotations

import re
from pathlib import Path

from docs_corpus import api_reference_text

import ontary

ROOT = Path(__file__).resolve().parent.parent
EN_PAGE = ROOT / "docs" / "coming-from-ddd.md"
JA_PAGE = ROOT / "docs" / "coming-from-ddd.ja.md"
BANNED_EN = (
    r"\bDDD\b",
    "domain-driven",
    "aggregate root",
    "bounded context",
    "value object",
    "domain event",
    "ubiquitous language",
    "anti-corruption",
)
BANNED_JA = (
    "ドメイン駆動",
    "集約ルート",
    "境界づけられたコンテキスト",
    "値オブジェクト",
    "ドメインイベント",
    "ユビキタス言語",
    "腐敗防止",
)
ALLOWED_PAGES = (
    "coming-from-ddd.md",
    "coming-from-ddd.ja.md",
    "roadmap.md",
)
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


def _ddd_violations(text: str, *, readme: bool = False) -> list[str]:
    """Return line-numbered matches for banned vocabulary in Markdown text."""
    violations: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if readme and "docs/coming-from-ddd" in line:
            continue
        visible_line = re.sub(r"\]\([^)]+\)", "]()", line)
        for phrase in (*BANNED_EN, *BANNED_JA):
            for match in re.finditer(phrase, visible_line, flags=re.IGNORECASE):
                violations.append(f"{line_number}: {match.group(0)}")
    return violations


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


def test_no_other_page_uses_ddd_vocabulary() -> None:
    paths = [ROOT / "README.md"]
    paths.extend(
        path
        for path in sorted((ROOT / "docs").glob("*.md"))
        if path.name not in ALLOWED_PAGES
        and not path.read_text().startswith("<!-- site-page:")
    )
    failures = []
    for path in paths:
        violations = _ddd_violations(path.read_text(), readme=path.name == "README.md")
        failures.extend(
            f"{path.relative_to(ROOT)}:{violation}" for violation in violations
        )
    assert failures == [], "\n".join(failures)


def test_ddd_matcher_catches_a_planted_phrase() -> None:
    assert _ddd_violations("a bounded context") == ["1: bounded context"]
    assert _ddd_violations("[x](coming-from-ddd.md)") == []
    readme_line = "See [Coming from Domain-Driven Design](docs/coming-from-ddd.md)"
    assert _ddd_violations(readme_line, readme=True) == []


def test_design_guide_links_the_ddd_page() -> None:
    guides = (
        (ROOT / "docs" / "ontology-design.md", "coming-from-ddd.md"),
        (ROOT / "docs" / "ontology-design.ja.md", "coming-from-ddd.ja.md"),
    )
    for path, target in guides:
        text = path.read_text()
        heading = "### Start from the domain's language"
        assert heading in text, path.name
        assert "### Domain-driven design" not in text, path.name
        start = text.index(heading)
        end = text.find("\n### ", start + len(heading))
        section = text[start:] if end == -1 else text[start:end]
        assert f"]({target})" in section, path.name
        assert _ddd_violations(section) == [], path.name


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
    reference = api_reference_text("en")
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
