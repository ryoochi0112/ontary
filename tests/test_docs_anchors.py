"""Pin inbound API anchors, leaf sizes, and translation heading structure."""

from __future__ import annotations

import re
from itertools import zip_longest
from pathlib import Path

from docs_corpus import API_REFERENCE_PAGES, DOCS, HUB, headings, slugify

from scripts.mkdocs_hooks import RETIRED_ANCHORS

ROOT = DOCS.parent
_INBOUND = re.compile(r"(api-[a-z-]+(?:\.ja)?\.md)#([^\s)\]\"'`]+)")
_CHANGELOG_DEEP_LINK = re.compile(r"\]\((docs/[\w.-]+\.md)#([^)\s]+)\)")
_EXPLICIT_ID = re.compile(r'<a id="([^"]+)"')


def _anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return {slugify(title) for _, title in headings(text)} | set(_EXPLICIT_ID.findall(text))


def test_changelog_deep_links_resolve_or_are_declared_retired() -> None:
    links = set(_CHANGELOG_DEEP_LINK.findall((ROOT / "CHANGELOG.md").read_text(encoding="utf-8")))
    targets = {f"{path}#{fragment}" for path, fragment in links}
    assert RETIRED_ANCHORS <= targets, f"Stale retired anchors: {RETIRED_ANCHORS - targets}"
    for path, fragment in sorted(links):
        target = f"{path}#{fragment}"
        live = fragment in _anchors(ROOT / path)
        if target in RETIRED_ANCHORS:
            assert not live, f"{target} resolves again; remove it from RETIRED_ANCHORS"
        else:
            assert live, f"CHANGELOG.md: {target} has no anchor; restore it or retire it"


def test_every_inbound_api_reference_anchor_resolves() -> None:
    sources = [ROOT / "README.md"]
    for directory, pattern in (
        (DOCS, "*.md"),
        (ROOT / "examples", "*.md"),
        (ROOT / "src" / "ontary", "*.py"),
        (ROOT / "tests", "*.py"),
    ):
        sources.extend(sorted(directory.rglob(pattern)))
    excluded = {Path(__file__).resolve(), Path(__file__).with_name("docs_corpus.py").resolve()}
    pairs: dict[tuple[str, str], Path] = {}
    for source in sources:
        if source.resolve() in excluded:
            continue
        for match in _INBOUND.finditer(source.read_text(encoding="utf-8")):
            pairs.setdefault((match.group(1), match.group(2)), source)
    assert len(pairs) >= 16, f"Expected at least 16 inbound anchor pairs; found {len(pairs)}"
    counts = {
        lang: sum(target.endswith(".ja.md") == (lang == "ja") for target, _ in pairs)
        for lang in ("en", "ja")
    }
    print(
        f"Inbound API anchor inventory: {len(pairs)} distinct pairs "
        f"({counts['en']} EN + {counts['ja']} JA)"
    )
    for (target, fragment), source in sorted(pairs.items()):
        path = DOCS / target
        message = f"{source.relative_to(ROOT)}: {target}#{fragment}"
        assert path.is_file(), f"{message}: target file missing"
        slugs = {slugify(title) for _, title in headings(path.read_text(encoding="utf-8"))}
        assert fragment in slugs, f"{message}: no matching heading"


def test_slugify_matches_mkdocs_on_known_anchors() -> None:
    for title, expected in (
        ("`ActionContext`", "actioncontext"),
        ("Date and datetime values", "date-and-datetime-values"),
        ("Error codes", "error-codes"),
        ("Scope policy", "scope-policy"),
        ("date と datetime の値", "date-と-datetime-の値"),
    ):
        assert slugify(title) == expected, title


def test_api_reference_leaves_stay_under_600_lines() -> None:
    leaves = [path for path in sorted(DOCS.glob("api-*.md")) if path not in HUB.values()]
    assert isinstance(leaves, list)
    for path in leaves:
        count = len(path.read_text(encoding="utf-8").splitlines())
        assert count <= 600, f"{path.name}: {count} lines exceeds 600"


def test_api_reference_pages_have_en_ja_heading_level_parity() -> None:
    for en in API_REFERENCE_PAGES["en"]:
        ja = en.with_suffix(".ja.md")
        assert ja.is_file(), f"{en.name}: missing sibling {ja.name}"
        en_levels = [level for level, _ in headings(en.read_text(encoding="utf-8"))]
        ja_levels = [level for level, _ in headings(ja.read_text(encoding="utf-8"))]
        first_difference = next(
            (
                index
                for index, (left, right) in enumerate(zip_longest(en_levels, ja_levels))
                if left != right
            ),
            None,
        )
        assert en_levels == ja_levels, (
            f"{en.name} / {ja.name}: heading levels first differ at index {first_difference}"
        )
