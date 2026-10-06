"""Shared API reference corpus and Markdown heading helpers (stdlib only)."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

DOCS: Path = Path(__file__).resolve().parent.parent / "docs"
HUB: dict[str, Path] = {
    "en": DOCS / "api-reference.md",
    "ja": DOCS / "api-reference.ja.md",
}
API_REFERENCE_PAGES: dict[str, tuple[Path, ...]] = {
    lang: (
        hub,
        *(
            path
            for path in sorted(DOCS.glob("api-*.md"))
            if path not in HUB.values() and path.name.endswith(".ja.md") == (lang == "ja")
        ),
    )
    for lang, hub in HUB.items()
}


def api_reference_text(lang: str) -> str:
    """Read a language's hub and leaves in stable order."""
    return "\n\n".join(path.read_text(encoding="utf-8") for path in API_REFERENCE_PAGES[lang])


def slugify(title: str) -> str:
    """Mirror pymdownx.slugs.slugify(case='lower') for heading anchors."""
    title = unicodedata.normalize("NFC", title).lower()
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", title))


def headings(text: str) -> list[tuple[int, str]]:
    """Read ATX heading levels and titles outside backtick or tilde fences."""
    result: list[tuple[int, str]] = []
    fence = ""
    for line in text.splitlines():
        if fence:
            closing = r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*"
            if re.fullmatch(closing, line):
                fence = ""
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if opening and not (opening.group(1).startswith("`") and "`" in opening.group(2)):
            fence = opening.group(1)
            continue
        heading = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$", line)
        if heading:
            title = re.sub(r"(?:^|[ \t]+)#+[ \t]*$", "", heading.group(2) or "").strip()
            result.append((len(heading.group(1)), title))
    return result
