"""Pins `scripts/mkdocs_hooks.py`: README/CHANGELOG become site pages and
repository-relative links become site links, without changing the Markdown
sources. Pure-function tests -- mkdocs itself is not a dev dependency."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from scripts.mkdocs_hooks import (
    INCLUDES,
    SITE_SHIM_PREFIX,
    on_config,
    on_post_build,
    render_llms_full,
    render_llms_txt,
    rewrite_links,
)

SITE = "https://ryoochi0112.github.io/ontary/"
BLOB = "https://github.com/ryoochi0112/ontary/blob/main/"
ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("language, expected", [("en", logging.WARNING), ("ja", logging.INFO)])
@pytest.mark.parametrize("nested", [False, True], ids=["flat", "validated"])
def test_config_relaxes_anchors_only_for_non_english(
    language: str, expected: int, nested: bool
) -> None:
    validation = SimpleNamespace(anchors=logging.WARNING)
    config = SimpleNamespace(
        plugins={"i18n": SimpleNamespace(current_language=language)},
        validation=SimpleNamespace(links=validation) if nested else validation,
    )
    on_config(config)
    assert validation.anchors == expected


def test_readme_links_become_site_links() -> None:
    text = (
        "[design](docs/ontology-design.md) [ja](docs/ontology-design.ja.md) "
        "[codes](docs/api-reference.md#error-codes) [log](CHANGELOG.md) "
        "[app](examples/tickets/README.md) [lic](LICENSE) [x](https://x.test/a)"
    )
    out = rewrite_links(text, "index.md", SITE)
    assert "(ontology-design.md)" in out
    assert f"({SITE}ja/ontology-design/)" in out
    assert "(api-reference.md#error-codes)" in out
    assert "(changelog.md)" in out
    assert f"({BLOB}examples/tickets/README.md)" in out
    assert f"({BLOB}LICENSE)" in out
    assert "(https://x.test/a)" in out


def test_docs_page_links_become_site_links() -> None:
    text = "[r](../README.md) [c](../CHANGELOG.md#x) [e](../examples/tickets/ontology.py) [a](api-reference.md#stores)"
    out = rewrite_links(text, "storage.md", SITE)
    assert "(index.md)" in out
    assert "(changelog.md#x)" in out
    assert f"({BLOB}examples/tickets/ontology.py)" in out
    assert "(api-reference.md#stores)" in out


def test_japanese_page_english_toggle_points_at_the_english_page() -> None:
    out = rewrite_links("[English](api-reference.md) · **日本語**", "api-reference.ja.md", SITE)
    assert f"[English]({SITE}api-reference/)" in out


def test_changelog_links_to_deleted_docs_go_to_github_not_the_site() -> None:
    out = rewrite_links("[old](docs/connectors.md) [cur](docs/storage.md)", "changelog.md", SITE)
    assert f"({BLOB}docs/connectors.md)" in out
    assert "(storage.md)" in out


def test_changelog_drops_only_retired_anchors() -> None:
    markdown = (
        "[a](docs/storage.md#storage-envelope) "
        "[b](docs/storage.md#moving-across-a-schema-version)"
    )
    out = rewrite_links(markdown, "changelog.md", SITE)
    assert "(storage.md)" in out
    assert "(storage.md#moving-across-a-schema-version)" in out
    retired_elsewhere = rewrite_links("[a](../docs/storage.md#storage-envelope)", "x.md", SITE)
    assert "#storage-envelope" in retired_elsewhere


def test_shims_exist_and_name_their_sources() -> None:
    for shim, source in INCLUDES.items():
        first_line = (ROOT / "docs" / shim).read_text().splitlines()[0]
        assert first_line.startswith(SITE_SHIM_PREFIX), shim
        assert source in first_line
        assert (ROOT / source).is_file()


@pytest.fixture
def llms_sources() -> tuple[list[Any], dict[str, str]]:
    nav = [
        {"Home": "index.md"},
        {
            "Guide": [
                {"First": "first.md"},
                {"Nested": [{"Bare": "bare.md"}, {"Remote": "https://example.test/"}]},
                {"Changelog": "changelog.md"},
            ]
        },
    ]
    pages = {
        "index.md": "# Readme\n\n[guide](docs/storage.md) [log](CHANGELOG.md)",
        "first.md": (
            "# First\n\n*Tutorial* — This page uses `code`, **bold**, _emphasis_, "
            "and [a link](bare.md).\n\n"
            "[home](../README.md#start) [bare](bare.md#anchor) [self](#here)"
        ),
        "bare.md": "# Bare\n\nNo kind line. [first](first.md)",
        "changelog.md": "# History\n\nRelease changes.",
        "first.ja.md": "日本語の内容",
    }
    return nav, pages


def test_llms_txt_nav_and_descriptions(llms_sources: tuple[list[Any], dict[str, str]]) -> None:
    nav, pages = llms_sources
    out = render_llms_txt(nav, pages, SITE, "SDK summary.")
    assert out.startswith("# ontary\n\n> SDK summary.\n\n## Overview\n\n")
    assert f"- [Home]({SITE}): SDK summary.\n" in out
    assert "\n## Guide\n\n" in out
    assert "\n### Nested\n\n" in out
    assert f"- [First]({SITE}first/): This page uses code, bold, emphasis, and a link.\n" in out
    assert f"- [Bare]({SITE}bare/)\n" in out
    assert f"- [Changelog]({SITE}changelog/): Every release's changes.\n" in out
    assert "Remote" not in out
    assert "日本語の内容" not in out


def test_kind_line_only_uses_first_eight_lines() -> None:
    pages = {"late.md": "\n" * 8 + "*Tutorial* — Too late."}
    out = render_llms_txt([{"Late": "late.md"}], pages, SITE, "Summary")
    assert f"- [Late]({SITE}late/)\n" in out


def test_llms_full_order_and_absolute_links(llms_sources: tuple[list[Any], dict[str, str]]) -> None:
    nav, pages = llms_sources
    out = render_llms_full(nav, pages, SITE)
    positions = [
        out.index(f"# {label}\n\nSource:") for label in ("Home", "First", "Bare", "Changelog")
    ]
    assert positions == sorted(positions)
    assert f"# Home\n\nSource: {SITE}\n\n# Readme" in out
    assert f"# First\n\nSource: {SITE}first/\n\n# First" in out
    assert f"[home]({SITE}#start)" in out
    assert f"[bare]({SITE}bare/#anchor)" in out
    assert f"[guide]({SITE}storage/)" in out
    assert f"[log]({SITE}changelog/)" in out
    assert out.count("\n\n---\n\n") == 3
    assert "Remote" not in out
    assert "日本語の内容" not in out
    assert all(target.startswith(("http", "#")) for target in re.findall(r"\]\(([^)\s]+)", out))


def _real_config() -> dict[str, Any]:
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("tag:yaml.org,2002:python/", lambda loader, suffix, node: None)
    return yaml.load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"), Loader=Loader)


def _nav_leaves(nav: list[Any]) -> list[tuple[str, str]]:
    leaves = []
    for entry in nav:
        for label, value in entry.items():
            if isinstance(value, list):
                leaves.extend(_nav_leaves(value))
            elif not value.startswith("http"):
                leaves.append((label, value))
    return leaves


def test_real_nav_labels_appear_in_both_outputs() -> None:
    config = _real_config()
    leaves = _nav_leaves(config["nav"])
    pages = {
        path: (ROOT / INCLUDES[path] if path in INCLUDES else ROOT / "docs" / path).read_text(
            encoding="utf-8"
        )
        for _, path in leaves
    }
    index = render_llms_txt(config["nav"], pages, SITE, config["site_description"])
    full = render_llms_full(config["nav"], pages, SITE)
    for label, _ in leaves:
        assert f"- [{label}](" in index
        assert f"# {label}\n\nSource: " in full
    assert SITE_SHIM_PREFIX not in full
    assert (ROOT / "README.md").read_text().splitlines()[0] in full
    assert (ROOT / "CHANGELOG.md").read_text().splitlines()[0] in full
    assert all(target.startswith(("http", "#")) for target in re.findall(r"\]\(([^)\s]+)", full))


def test_post_build_writes_root_english_only(tmp_path: Path) -> None:
    config = _real_config()
    config["site_dir"] = str(tmp_path / "site")
    plugin = SimpleNamespace(current_language="en")
    config["plugins"] = {"i18n": plugin}
    on_post_build(config)
    index = (tmp_path / "site" / "llms.txt").read_text(encoding="utf-8")
    full = (tmp_path / "site" / "llms-full.txt").read_text(encoding="utf-8")
    assert f"- [Home]({SITE}): {config['site_description']}\n" in index
    assert f"# Home\n\nSource: {SITE}\n\n#" in full
    assert SITE_SHIM_PREFIX not in full

    plugin.current_language = "ja"
    config["nav"] = [{"日本語": "index.md"}]
    on_post_build(config)
    assert (tmp_path / "site" / "llms.txt").read_text(encoding="utf-8") == index
    assert (tmp_path / "site" / "llms-full.txt").read_text(encoding="utf-8") == full

    config["site_dir"] = str(tmp_path / "site" / "ja")
    on_post_build(config)
    assert not (tmp_path / "site" / "ja" / "llms.txt").exists()
    assert not (tmp_path / "site" / "ja" / "llms-full.txt").exists()

    plugin.current_language = "en"
    on_post_build(config)
    assert not (tmp_path / "site" / "ja" / "llms.txt").exists()
    assert not (tmp_path / "site" / "ja" / "llms-full.txt").exists()
