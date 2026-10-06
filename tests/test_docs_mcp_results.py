"""Compare documented MCP read result keys with live results in both languages."""

from __future__ import annotations

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
        assert set(response) == top_keys, (page, tool, response)
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
