"""Compare documented MCP read result keys with live results in both languages."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from test_mcp_honest_results import (
    READ_TOOLS,
    SRC,
    _arguments,
    _call,
    _insert_ticket,
    _ontology,
    _server,
)
from test_mcp_list_completeness import _store

from ontary.store import ObjectStore

DOCS = Path(__file__).resolve().parents[1] / "docs"


def _documented_read_results(page: str) -> dict[str, tuple[set[str], set[str]]]:
    """Parse fixed columns: tool, top-level keys, row keys, result, scope policy."""
    rows: dict[str, tuple[set[str], set[str]]] = {}
    for line in (DOCS / page).read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5 or not re.fullmatch(r"`\w+`", cells[0]):
            continue
        tool = cells[0].strip("`")
        assert tool not in rows, f"{page}: duplicate result policy for {tool}"
        keys = []
        for cell in cells[1:3]:
            assert cell == "—" or re.fullmatch(r"`\w+`(, `\w+`)*", cell), (
                page, tool, "key columns must contain only backticked names or —", cell,
            )
            keys.append(set(re.findall(r"`(\w+)`", cell)))
        rows[tool] = (keys[0], keys[1])
    assert set(rows) == set(READ_TOOLS), f"{page}: missing or extra read result policies"
    return rows


@pytest.mark.parametrize("page", ["api-mcp.md", "api-mcp.ja.md"])
@pytest.mark.parametrize("mode", ["scoped", "unscoped", "row-rule"])
@pytest.mark.parametrize("populated", [False, True])
def test_documented_read_results_match_the_server(
    page: str, mode: str, populated: bool,
) -> None:
    documented = _documented_read_results(page)
    ontology = _ontology(mode, hidden_routing=True)
    store = ObjectStore(ontology.registry)
    store.insert("Team", {"id": "a"}, SRC)
    if populated:
        _insert_ticket(store, "t-1")
    server, _ = _server(ontology, store)

    calls = [(tool, _arguments(tool)) for tool in READ_TOOLS]
    if populated:
        calls.append(("traverse_links", {
            "obj_type": "Ticket", "obj_id": "t-1", "link_api_name": "inTeam",
        }))
    for tool, arguments in calls:
        response = _call(server, tool, arguments)
        top_keys, row_keys = documented[tool]
        # `total` is documented as a top-level key but appears only with include_total.
        assert set(response) == top_keys - {"total"}, (page, tool, response)
        result = response["result"]
        if tool == "count_objects":
            assert type(result) is int
            assert row_keys == set(), (page, tool, "counts have no object row")
            continue
        if tool == "get_object":
            results = [] if result is None else [result]
        else:
            assert isinstance(result, list), (page, tool, result)
            results = result
        assert bool(results) is populated, (page, tool, result)
        for row in results:
            assert set(row) == row_keys, (page, tool, row)


PAGED_TOOLS = ("query_objects", "traverse_links")
GOLDEN_ARGS = {
    "obj_type": "Team", "obj_id": "a", "link_api_name": "inTeam",
    "reverse": True, "limit": 2, "include_total": True,
}


def _tool_rows(page: str) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in (DOCS / page).read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if re.fullmatch(r"`\w+`", cells[0]):
            rows.setdefault(cells[0].strip("`"), cells)
    return rows


@pytest.mark.parametrize("page", ["api-mcp.md", "api-mcp.ja.md"])
@pytest.mark.parametrize("tool", PAGED_TOOLS)
def test_paged_tools_document_paging_arguments_and_total(page: str, tool: str) -> None:
    arguments_rows = [
        cells for line in (DOCS / page).read_text(encoding="utf-8").splitlines()
        if (cells := [c.strip() for c in line.strip().strip("|").split("|")])[0] == f"`{tool}`"
        and len(cells) == 3 and cells[1].startswith("`obj_type`")
    ]
    assert len(arguments_rows) == 1, (page, tool)
    optional = set(re.findall(r"`(\w+)`", arguments_rows[0][2]))
    assert {"limit", "after", "include_total"} <= optional, (page, tool, optional)
    top_keys = _documented_read_results(page)[tool][0]
    assert {"has_more", "next_cursor", "total"} <= top_keys, (page, tool, top_keys)


def _golden_block(page: str) -> str:
    text = (DOCS / page).read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, re.DOTALL)
    golden = [b for b in blocks if '"has_more": true' in b and '"total"' in b]
    assert len(golden) == 1, f"{page}: expected exactly one golden sample block"
    return golden[0]


def _parse_golden(block: str) -> dict[str, object]:
    # The documented block uses `{...}` for elided payloads; read it as `{}`.
    return json.loads(block.replace("{...}", "{}"))  # type: ignore[no-any-return]


def _mask(response: dict[str, object]) -> dict[str, object]:
    masked = dict(response)
    masked["next_cursor"] = "<opaque>"
    masked["result"] = [
        {
            key: ({} if key in ("payload", "lineage") else [])
            for key in row  # row bodies are masked; only the row key set is compared
        }
        for row in response["result"]  # type: ignore[attr-defined]
    ]
    return masked


@pytest.mark.parametrize("page", ["api-mcp.md", "api-mcp.ja.md"])
def test_golden_sample_equals_a_live_call(page: str) -> None:
    documented = _parse_golden(_golden_block(page))
    ontology, store = _store(218)
    server, _ = _server(ontology, store)
    live = _call(server, "traverse_links", dict(GOLDEN_ARGS))

    assert live["has_more"] is True and live["total"] == 218 and len(live["result"]) == 2
    assert documented["has_more"] is True
    assert documented["total"] == 218
    assert len(documented["result"]) == 2  # type: ignore[arg-type]
    assert documented == _mask(live), page
