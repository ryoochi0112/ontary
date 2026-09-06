"""MkDocs hook (registered in `mkdocs.yml` under `hooks:`).

Publishes `README.md` and `CHANGELOG.md` as site pages through the one-line
shims `docs/index.md` and `docs/changelog.md`, and rewrites repository-relative
links into site links. The Markdown sources are untouched, so they keep working
when read on GitHub; only the rendered site sees the rewritten targets.
Deliberately imports nothing from mkdocs: `make typecheck` and the unit tests
run in the dev environment, which does not install the `docs` group.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BLOB = "https://github.com/ryoochi0112/ontary/blob/main/"
#: site page shim -> repository file rendered in its place.
INCLUDES = {"index.md": "README.md", "changelog.md": "CHANGELOG.md"}
SITE_SHIM_PREFIX = "<!-- site-page:"
_LINK = re.compile(r"\]\(([^)\s]+)")
_JA_PAGE = re.compile(r"(?:docs/)?([\w-]+)\.ja\.md(#.*)?")
_ENGLISH_TOGGLE = re.compile(r"\[English\]\(([\w-]+)\.md\)")


def _doc_pages() -> set[str]:
    return {p.name for p in (ROOT / "docs").glob("*.md")}


def _site_link(target: str, from_root: bool, doc_pages: set[str]) -> str:
    path, _, fragment = target.partition("#")
    anchor = f"#{fragment}" if fragment else ""
    if from_root:
        if path == "CHANGELOG.md":
            return f"changelog.md{anchor}"
        if path.startswith("docs/"):
            name = path[len("docs/") :]
            return f"{name}{anchor}" if name in doc_pages else f"{BLOB}{path}{anchor}"
        return f"{BLOB}{path}{anchor}"
    if path == "../README.md":
        return f"index.md{anchor}"
    if path == "../CHANGELOG.md":
        return f"changelog.md{anchor}"
    if path.startswith("../"):
        return f"{BLOB}{path[3:]}{anchor}"
    return target


def rewrite_links(markdown: str, src_path: str, site_url: str) -> str:
    """Rewrite every Markdown link target in `markdown` for the page at
    `src_path` (a path relative to `docs/`, e.g. `index.md`,
    `api-reference.ja.md`). `site_url` ends with `/`."""
    from_root = src_path in INCLUDES
    doc_pages = _doc_pages()

    def rewrite(match: re.Match[str]) -> str:
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            return match.group(0)
        ja = _JA_PAGE.fullmatch(target)
        if ja:
            return f"]({site_url}ja/{ja.group(1)}/{ja.group(2) or ''}"
        return f"]({_site_link(target, from_root, doc_pages)}"

    if src_path.endswith(".ja.md"):
        markdown = _ENGLISH_TOGGLE.sub(rf"[English]({site_url}\1/)", markdown)
    return _LINK.sub(rewrite, markdown)


def on_page_markdown(markdown: str, page: Any, config: Any, files: Any) -> str:
    src_path: str = page.file.src_path
    if src_path in INCLUDES:
        markdown = (ROOT / INCLUDES[src_path]).read_text()
    return rewrite_links(markdown, src_path, str(config.site_url))
