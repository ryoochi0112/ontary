"""Pins the MkDocs navigation sections and Japanese section titles."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
MKDOCS = ROOT / "mkdocs.yml"
SECTION_TITLES = (
    "Home",
    "Tutorials",
    "How-to guides",
    "Reference",
    "Explanation",
    "Project",
)
SECTION_OF: dict[str, str] = {
    "getting-started.md": "Tutorials",
    "tutorial-leave-requests.md": "Tutorials",
    "testing.md": "How-to guides",
    "storage.md": "How-to guides",
    "mcp-serving.md": "How-to guides",
    "api-reference.md": "Reference",
    "cli.md": "Reference",
    "compatibility.md": "Reference",
    "ontology-design.md": "Explanation",
    "roadmap.md": "Project",
    "changelog.md": "Project",
    "releasing.md": "Project",
}
EXPECTED_JA_NAV_TRANSLATIONS: dict[str, str] = {
    "Tutorials": "チュートリアル",
    "How-to guides": "ハウツーガイド",
    "Reference": "リファレンス",
    "Explanation": "解説",
    "Project": "プロジェクト",
}
ORIENTATION_MARKERS: dict[str, str] = {
    "Tutorials": "*Tutorial* — ",
    "How-to guides": "*How-to guide* — ",
    "Reference": "*Reference* — ",
    "Explanation": "*Explanation* — ",
    "Project": "*Project* — ",
}
JA_ORIENTATION_MARKERS: dict[str, str] = {
    "Tutorials": "*チュートリアル* — ",
    "How-to guides": "*ハウツーガイド* — ",
    "Reference": "*リファレンス* — ",
    "Explanation": "*解説* — ",
    "Project": "*プロジェクト* — ",
}


def _nav_sections() -> list[tuple[str, list[str]]]:
    """Return each top-level nav title and its Markdown page files."""
    lines = MKDOCS.read_text().splitlines()
    nav_index = lines.index("nav:")
    sections: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current_pages: list[str] = []

    for line in lines[nav_index + 1 :]:
        if line.strip() and not line.startswith(" "):
            break

        top_level = re.fullmatch(r"  - ([^:]+):(?: (.+))?", line)
        if top_level is not None:
            if current_title is not None:
                sections.append((current_title, current_pages))
            current_title = top_level.group(1)
            page = top_level.group(2)
            current_pages = [page] if page is not None else []
            continue

        nested_page = re.fullmatch(r"      - [^:]+: (.+)", line)
        if current_title is not None and nested_page is not None:
            current_pages.append(nested_page.group(1))

    if current_title is not None:
        sections.append((current_title, current_pages))
    return sections


def _ja_nav_translations() -> dict[str, str]:
    """Return nav_translations from the Japanese i18n language entry."""
    lines = MKDOCS.read_text().splitlines()
    ja_index = next(
        (index for index, line in enumerate(lines) if line == "        - locale: ja"),
        None,
    )
    if ja_index is None:
        return {}

    language_end = len(lines)
    for index in range(ja_index + 1, len(lines)):
        line = lines[index]
        if line.strip() and len(line) - len(line.lstrip()) <= 8:
            language_end = index
            break

    translations_index = next(
        (
            index
            for index in range(ja_index + 1, language_end)
            if lines[index] == "          nav_translations:"
        ),
        None,
    )
    if translations_index is None:
        return {}

    translations: dict[str, str] = {}
    for line in lines[translations_index + 1 : language_end]:
        match = re.fullmatch(r"            ([^:]+):\s*(.*?)\s*", line)
        if match is not None:
            translations[match.group(1)] = match.group(2)
        elif line.strip() and len(line) - len(line.lstrip()) <= 10:
            break
    return translations


def test_nav_sections_have_the_expected_titles_and_page_mapping() -> None:
    sections = _nav_sections()
    titles = [title for title, _pages in sections]
    assert len(sections) == 6
    assert titles == list(SECTION_TITLES)
    assert sections[0] == ("Home", ["index.md"])

    actual_section_of = {
        page: title
        for title, pages in sections
        if title != "Home"
        for page in pages
    }
    assert actual_section_of == SECTION_OF


def test_every_english_docs_page_is_in_exactly_one_section() -> None:
    section_page_counts = Counter(
        page for _title, pages in _nav_sections()[1:] for page in pages
    )
    for path in sorted(DOCS.glob("*.md")):
        if path.name.endswith(".ja.md") or path.read_text().startswith(
            "<!-- site-page:"
        ):
            continue
        count = section_page_counts[path.name]
        assert count == 1, f"{path.name} appears in {count} of the five sections"


def test_every_japanese_docs_page_has_an_english_sibling() -> None:
    for path in sorted(DOCS.glob("*.ja.md")):
        english_name = f"{path.name[:-len('.ja.md')]}.md"
        assert (DOCS / english_name).is_file(), (
            f"{path.name} has no English sibling {english_name}"
        )


def test_japanese_nav_translations_match_the_five_sections() -> None:
    translations = _ja_nav_translations()
    section_titles = {
        title for title, _pages in _nav_sections() if title != "Home"
    }
    assert set(translations) == section_titles
    assert translations == EXPECTED_JA_NAV_TRANSLATIONS


def _linked_page_section(source: Path, target: str) -> str | None:
    """Return the nav section for a relative Markdown link under docs/.

    Japanese pages use the same section as their English sibling. Links to
    README, CHANGELOG, external URLs, anchors, and non-nav pages are ignored.
    """
    target = target.strip()
    if not target or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target):
        return None

    target_path = target.split()[0].strip("<>").split("#", 1)[0].split("?", 1)[0]
    if not target_path or target_path.startswith("/"):
        return None

    linked_path = (source.parent / target_path).resolve()
    try:
        linked_name = linked_path.relative_to(DOCS.resolve()).name
    except ValueError:
        return None

    if linked_name.endswith(".ja.md"):
        linked_name = f"{linked_name[:-len('.ja.md')]}.md"
    return SECTION_OF.get(linked_name)


def test_every_section_page_has_a_marked_cross_section_orientation_line() -> None:
    english_markers = tuple(ORIENTATION_MARKERS.values())
    japanese_markers = tuple(JA_ORIENTATION_MARKERS.values())

    for english_name, section in SECTION_OF.items():
        english_path = DOCS / english_name
        english_text = english_path.read_text()
        if english_text.startswith("<!-- site-page:"):
            continue

        language_pages = [(english_path, ORIENTATION_MARKERS, english_markers)]
        japanese_path = DOCS / f"{english_name[:-len('.md')]}.ja.md"
        if japanese_path.is_file():
            language_pages.append(
                (japanese_path, JA_ORIENTATION_MARKERS, japanese_markers)
            )

        for path, markers_by_section, all_markers in language_pages:
            lines = path.read_text().splitlines()
            first_heading = next(
                (index for index, line in enumerate(lines) if line.startswith("## ")),
                len(lines),
            )
            marker_lines = [
                (index, line)
                for index, line in enumerate(lines[:8])
                if index < first_heading and line.startswith(all_markers)
            ]
            assert len(marker_lines) == 1, (
                f"{path.name} needs one orientation marker within its first 8 lines "
                "and before its first level-two heading"
            )

            orientation_index, orientation_line = marker_lines[0]
            expected_marker = markers_by_section[section]
            assert orientation_line.startswith(expected_marker), (
                f"{path.name} needs the {section!r} marker {expected_marker!r}"
            )
            linked_sections = [
                linked_section
                for match in re.finditer(r"\]\(([^)]+)\)", orientation_line)
                if (
                    linked_section := _linked_page_section(path, match.group(1))
                )
                is not None
            ]
            assert any(linked_section != section for linked_section in linked_sections), (
                f"{path.name} orientation line must link to a page in another section"
            )
            assert orientation_index < first_heading


def test_readme_where_to_go_lists_the_five_nav_groups_in_order() -> None:
    lines = (ROOT / "README.md").read_text().splitlines()
    section_start = lines.index("## Where to go")
    section_end = next(
        (
            index
            for index in range(section_start + 1, len(lines))
            if lines[index].startswith("## ")
        ),
        len(lines),
    )
    section_lines = lines[section_start:section_end]
    group_rows = [
        re.fullmatch(r"\| \*\*(.+)\*\* \| \|", line)
        for line in section_lines
    ]
    groups = [match.group(1) for match in group_rows if match is not None]
    expected_groups = [title for title, _pages in _nav_sections() if title != "Home"]

    assert groups == expected_groups
    site_row = (
        "| The docs site (EN / 日本語) | "
        "https://ryoochi0112.github.io/ontary/ |"
    )
    assert site_row in section_lines
    assert section_lines.index(site_row) < next(
        index for index, match in enumerate(group_rows) if match is not None
    )
