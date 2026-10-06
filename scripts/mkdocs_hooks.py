"""MkDocs hook (registered in `mkdocs.yml` under `hooks:`).

Publishes `README.md` and `CHANGELOG.md` as site pages through the one-line
shims `docs/index.md` and `docs/changelog.md`, and rewrites repository-relative
links into site links. The Markdown sources are untouched, so they keep working
when read on GitHub; only the rendered site sees the rewritten targets.
Deliberately imports nothing from mkdocs: `make typecheck` and the unit tests
run in the dev environment, which does not install the `docs` group.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BLOB = "https://github.com/ryoochi0112/ontary/blob/main/"
#: site page shim -> repository file rendered in its place.
INCLUDES = {"index.md": "README.md", "changelog.md": "CHANGELOG.md"}
DESCRIPTION_FALLBACKS = {
    "index.md": "{site_description}",
    "changelog.md": "Every release's changes.",
}
LLMS_TITLE = "ontary"
SITE_SHIM_PREFIX = "<!-- site-page:"
_LINK = re.compile(r"\]\(([^)\s]+)")
_JA_PAGE = re.compile(r"(?:docs/)?([\w-]+)\.ja\.md(#.*)?")
_ENGLISH_TOGGLE = re.compile(r"\[English\]\(([\w-]+)\.md\)")
_KIND_LINE = re.compile(r"^\*[A-Z][^*]*\* — (.*)")


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


def on_config(config: Any) -> None:
    """Relax anchors for translated builds with English-only fallback pages."""
    i18n = config.plugins.get("i18n")
    if i18n is not None and i18n.current_language != "en":
        # MkDocs propagates the YAML shorthand into validation.links at load time.
        validation = getattr(config.validation, "links", config.validation)
        validation.anchors = logging.INFO


# Run after i18n's -100 on_config hook, without importing the docs dependency.
on_config.__dict__["mkdocs_priority"] = -101


def on_page_markdown(markdown: str, page: Any, config: Any, files: Any) -> str:
    src_path: str = page.file.src_path
    if src_path in INCLUDES:
        markdown = (ROOT / INCLUDES[src_path]).read_text()
    return rewrite_links(markdown, src_path, str(config.site_url))


def _is_en_page(value: str) -> bool:
    return not value.startswith("http") and not value.endswith(".ja.md")


def _nav_pages(nav: list[Any]) -> Iterator[tuple[str, str]]:
    for entry in nav:
        for label, value in entry.items():
            if isinstance(value, list):
                yield from _nav_pages(value)
            elif _is_en_page(value):
                yield label, value


def _page_url(src_path: str, site_url: str) -> str:
    path, _, fragment = src_path.partition("#")
    suffix = "" if path == "index.md" else f"{path.removesuffix('.md')}/"
    return f"{site_url.rstrip('/')}/{suffix}" + (f"#{fragment}" if fragment else "")


def _description(markdown: str, src_path: str, site_description: str) -> str:
    for line in markdown.splitlines()[:8]:
        match = _KIND_LINE.match(line)
        if match:
            text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", match.group(1))
            return re.sub(r"[`*_]", "", text)
    return DESCRIPTION_FALLBACKS.get(src_path, "").format(site_description=site_description)


def render_llms_txt(
    nav: list[Any], pages: Mapping[str, str], site_url: str, site_description: str
) -> str:
    """Render the English nav and page summaries in llmstxt.org format."""
    lines = [f"# {LLMS_TITLE}", "", f"> {site_description}", ""]

    def page_line(label: str, src_path: str) -> str:
        description = _description(pages[src_path], src_path, site_description)
        suffix = f": {description}" if description else ""
        return f"- [{label}]({_page_url(src_path, site_url)}){suffix}"

    overview = [
        page_line(label, value)
        for entry in nav
        for label, value in entry.items()
        if isinstance(value, str) and _is_en_page(value)
    ]
    if overview:
        lines.extend(["## Overview", "", *overview, ""])

    def section(label: str, children: list[Any], level: int) -> None:
        lines.extend([f"{'#' * level} {label}", ""])
        for entry in children:
            for child_label, value in entry.items():
                if isinstance(value, list):
                    if lines[-1]:
                        lines.append("")
                    section(child_label, value, level + 1)
                elif _is_en_page(value):
                    lines.append(page_line(child_label, value))
        if lines[-1]:
            lines.append("")

    for entry in nav:
        for label, value in entry.items():
            if isinstance(value, list):
                section(label, value, 2)
    return "\n".join(lines).rstrip() + "\n"


def render_llms_full(nav: list[Any], pages: Mapping[str, str], site_url: str) -> str:
    """Render every English nav page with absolute Markdown page links."""

    def absolute_link(match: re.Match[str]) -> str:
        target = match.group(1)
        if not target.startswith(("http://", "https://", "mailto:", "#")):
            path = target.partition("#")[0]
            if path.endswith(".md"):
                return f"]({_page_url(target, site_url)}"
        return match.group(0)

    rendered = []
    for label, src_path in _nav_pages(nav):
        body = _LINK.sub(absolute_link, rewrite_links(pages[src_path], src_path, site_url))
        rendered.append(f"# {label}\n\nSource: {_page_url(src_path, site_url)}\n\n{body.rstrip()}")
    return "\n\n---\n\n".join(rendered) + "\n"


def on_post_build(config: Any) -> None:
    """Write AI-readable docs once, during the root English build."""
    site_dir = Path(config["site_dir"])
    i18n = config.get("plugins", {}).get("i18n")
    if (i18n is not None and i18n.current_language != "en") or site_dir.name == "ja":
        return
    nav = config["nav"]
    pages = {
        src_path: (
            ROOT / INCLUDES[src_path] if src_path in INCLUDES else ROOT / "docs" / src_path
        ).read_text(encoding="utf-8")
        for _, src_path in _nav_pages(nav)
    }
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "llms.txt").write_text(
        render_llms_txt(nav, pages, config["site_url"], config["site_description"]),
        encoding="utf-8",
    )
    (site_dir / "llms-full.txt").write_text(
        render_llms_full(nav, pages, config["site_url"]), encoding="utf-8"
    )
