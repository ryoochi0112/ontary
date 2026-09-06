"""Pins `scripts/mkdocs_hooks.py`: README/CHANGELOG become site pages and
repository-relative links become site links, without changing the Markdown
sources. Pure-function tests -- mkdocs itself is not a dev dependency."""

from __future__ import annotations

from pathlib import Path

from scripts.mkdocs_hooks import INCLUDES, SITE_SHIM_PREFIX, rewrite_links

SITE = "https://ryoochi0112.github.io/ontary/"
BLOB = "https://github.com/ryoochi0112/ontary/blob/main/"
ROOT = Path(__file__).resolve().parent.parent


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


def test_shims_exist_and_name_their_sources() -> None:
    for shim, source in INCLUDES.items():
        first_line = (ROOT / "docs" / shim).read_text().splitlines()[0]
        assert first_line.startswith(SITE_SHIM_PREFIX), shim
        assert source in first_line
        assert (ROOT / source).is_file()
