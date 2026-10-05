"""The authenticated multi-consumer example in docs/mcp-serving.md runs as shown (#56).

The section's python fence is executed, then the HTTP request it shows is
replayed in-process through `httpx2.ASGITransport`, with and without its
`Authorization` header. Each actual response must equal the one the page
shows. A shown string of the form `<...>` matches any value, but only under
the keys in `PLACEHOLDER_KEYS`, so the page can mask per-run values (a
timestamp, the text copy of `structuredContent`) and nothing else.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer

_PAGE = Path(__file__).resolve().parent.parent / "docs" / "mcp-serving.md"
SECTION_HEADING = "## Serve many proven identities"
PLACEHOLDER_KEYS = frozenset({"valid_from", "text"})
_PLACEHOLDER = re.compile(r"^<.+>$")
_FENCE = re.compile(r"^```(\w+)\n(.*?)^```", re.DOTALL | re.MULTILINE)


@dataclass(frozen=True)
class HttpRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: Any


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: Any


def _section(text: str, heading: str) -> str:
    """The lines under `heading` up to the next heading of the same or a higher
    level, skipping `#` lines inside code fences."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    assert heading in lines, f"heading {heading!r} is missing"
    body: list[str] = []
    in_fence = False
    for line in lines[lines.index(heading) + 1 :]:
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("#"):
            other = len(line) - len(line.lstrip("#"))
            if other <= level and line[other : other + 1] == " ":
                break
        body.append(line)
    return "\n".join(body) + "\n"


def _fences(section: str) -> list[tuple[str, str]]:
    return [(lang, code) for lang, code in _FENCE.findall(section)]


def _split_message(fence: str) -> tuple[str, dict[str, str], Any]:
    """Start line, headers, and JSON body of an HTTP message fence."""
    head, _, body = fence.strip().partition("\n\n")
    start, *header_lines = head.splitlines()
    headers = {}
    for line in header_lines:
        name, sep, value = line.partition(":")
        assert sep, f"malformed header line {line!r}"
        headers[name.strip()] = value.strip()
    return start, headers, json.loads(body)


def parse_request(fence: str) -> HttpRequest:
    start, headers, body = _split_message(fence)
    method, path, _version = start.split(" ")
    return HttpRequest(method, path, headers, body)


def parse_response(fence: str) -> HttpResponse:
    start, headers, body = _split_message(fence)
    _version, status, _reason = start.split(" ", 2)
    return HttpResponse(int(status), headers, body)


def mismatches(shown: Any, actual: Any, path: str = "$", key: str | None = None) -> list[str]:
    """Every place `actual` differs from `shown`, honouring placeholders."""
    if isinstance(shown, str) and _PLACEHOLDER.match(shown):
        if key in PLACEHOLDER_KEYS:
            return []
        return [f"{path}: placeholder {shown!r} is not allowed under key {key!r}"]
    if isinstance(shown, dict) and isinstance(actual, dict):
        if shown.keys() != actual.keys():
            return [f"{path}: keys {sorted(shown)} != {sorted(actual)}"]
        return [m for k in shown for m in mismatches(shown[k], actual[k], f"{path}.{k}", k)]
    if isinstance(shown, list) and isinstance(actual, list):
        if len(shown) != len(actual):
            return [f"{path}: {len(shown)} items shown, {len(actual)} actual"]
        return [
            m
            for i, (s, a) in enumerate(zip(shown, actual, strict=True))
            for m in mismatches(s, a, f"{path}[{i}]", key)
        ]
    return [] if shown == actual else [f"{path}: shown {shown!r}, actual {actual!r}"]


# -- harness unit tests ------------------------------------------------------


def test_parse_request_reads_start_line_headers_and_json_body() -> None:
    request = parse_request(
        'POST /mcp HTTP/1.1\nHost: localhost:8000\nAuthorization: Bearer t\n\n{"id": 1}\n'
    )
    assert request == HttpRequest(
        "POST", "/mcp", {"Host": "localhost:8000", "Authorization": "Bearer t"}, {"id": 1}
    )


def test_parse_response_keeps_header_values_with_colons() -> None:
    response = parse_response(
        'HTTP/1.1 401 Unauthorized\nWWW-Authenticate: Bearer a="http://x:1"\n\n{"error": "e"}\n'
    )
    assert response == HttpResponse(401, {"WWW-Authenticate": 'Bearer a="http://x:1"'}, {"error": "e"})


@pytest.mark.parametrize("key", sorted(PLACEHOLDER_KEYS))
def test_placeholder_matches_any_value_under_an_allowed_key(key: str) -> None:
    assert mismatches({key: "<varies>"}, {key: "2026-10-05T00:00:00+00:00"}) == []


@pytest.mark.parametrize("key", ["subject", "error", "status", "next_cursor"])
def test_placeholder_is_rejected_under_any_other_key(key: str) -> None:
    assert mismatches({key: "<varies>"}, {key: "anything"}) != []


def test_mismatches_reports_changed_values_missing_keys_and_list_lengths() -> None:
    assert mismatches({"a": [1, 2]}, {"a": [1, 3]}) == ["$.a[1]: shown 2, actual 3"]
    assert mismatches({"a": 1}, {"a": 1, "b": 2}) != []
    assert mismatches([1], [1, 2]) != []


# -- the page ------------------------------------------------------------------


def _page_section() -> str:
    return _section(_PAGE.read_text(), SECTION_HEADING)


def test_section_has_one_program_one_request_and_two_responses() -> None:
    langs = [lang for lang, _ in _fences(_page_section())]
    assert langs == ["python", "http", "json", "http"], langs


def test_section_names_the_stand_in_and_where_the_401_comes_from() -> None:
    prose = _FENCE.sub("", _page_section())
    for term in ("stand-in", "JWT", "introspection", "401", "before ontary"):
        assert term in prose, f"mcp-serving.md auth example prose does not mention {term!r}"


def deploy_kwargs(program: str) -> dict[str, Any]:
    """The keyword arguments of the program's one `server.run(...)` call,
    without `transport`, which must be `"streamable-http"`."""
    calls = [
        node
        for node in ast.walk(ast.parse(program))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "server"
    ]
    assert len(calls) == 1, f"expected one server.run(...) call, found {len(calls)}"
    call = calls[0]
    assert not call.args, "server.run(...) must use keyword arguments only"
    kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords if kw.arg is not None}
    assert kwargs.pop("transport", None) == "streamable-http"
    return kwargs


def _replay(
    server: MCPServer, run_kwargs: dict[str, Any], requests: list[HttpRequest]
) -> list[httpx2.Response]:
    # Serve the app exactly as the reader's `server.run(...)` line configures it;
    # a keyword `streamable_http_app` does not take raises a TypeError here.
    app = server.streamable_http_app(**run_kwargs)

    async def send_all() -> list[httpx2.Response]:
        async with server.session_manager.run():
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://localhost:8000"
            ) as client:
                return [
                    await client.request(r.method, r.path, headers=r.headers, json=r.body)
                    for r in requests
                ]

    return asyncio.run(send_all())


def test_deploy_kwargs_reads_the_run_call() -> None:
    program = 'if __name__ == "__main__":\n    server.run(transport="streamable-http", json_response=True)\n'
    assert deploy_kwargs(program) == {"json_response": True}


def test_deploy_kwargs_rejects_a_missing_run_call_or_another_transport() -> None:
    with pytest.raises(AssertionError):
        deploy_kwargs("server = None\n")
    with pytest.raises(AssertionError):
        deploy_kwargs('server.run(transport="stdio")\n')


def test_example_runs_and_both_responses_match_the_page() -> None:
    (_, program), (_, request_fence), (_, ok_fence), (_, refused_fence) = _fences(_page_section())
    namespace: dict[str, Any] = {"__name__": "mcp_serving_example"}
    exec(compile(program, "docs/mcp-serving.md#serve-many-proven-identities", "exec"), namespace)
    server = namespace["server"]
    assert isinstance(server, MCPServer)

    request = parse_request(request_fence)
    assert request.headers.get("Authorization", "").startswith("Bearer ")
    anonymous = HttpRequest(
        request.method,
        request.path,
        {k: v for k, v in request.headers.items() if k != "Authorization"},
        request.body,
    )
    unknown = HttpRequest(
        request.method,
        request.path,
        {**request.headers, "Authorization": "Bearer unknown-token"},
        request.body,
    )
    ok, *refusals = _replay(server, deploy_kwargs(program), [request, anonymous, unknown])

    assert ok.status_code == 200, ok.text
    assert mismatches(json.loads(ok_fence), ok.json()) == []

    # Both a missing and an unknown bearer token get the page's 401.
    shown = parse_response(refused_fence)
    assert shown.status == 401
    for refused in refusals:
        assert refused.status_code == shown.status, refused.text
        for name, value in shown.headers.items():
            assert refused.headers.get(name) == value, name
        assert mismatches(shown.body, refused.json()) == []
