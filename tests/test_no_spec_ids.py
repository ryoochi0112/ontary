"""Keep internal spec and task references out of reader-facing source and docs."""

from __future__ import annotations

import re
import tokenize
from pathlib import Path

import pytest

PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bAC\d+"),
    re.compile(r"\bT\d+[a-z]?\b"),
    re.compile(r"§"),
    re.compile(r"\bspec\b", re.IGNORECASE),
    re.compile(r"\bM\d+(\.\d+)?[a-z]?\b"),
    re.compile(r"\bC\d+\b"),
    re.compile(r"\bP[0-2]\b"),
    re.compile(r"\b[RQ]\d+\b"),
    re.compile(r"原則"),
)

_PENDING: frozenset[str] = frozenset(
    {
        "docs/api-reference.ja.md",
        "docs/api-reference.md",
        "src/ontary/__init__.py",
        "src/ontary/_typed_api.py",
        "src/ontary/actions.py",
        "src/ontary/audit.py",
        "src/ontary/authoring.py",
        "src/ontary/client.py",
        "src/ontary/declarations.py",
        "src/ontary/errors.py",
        "src/ontary/functions.py",
        "src/ontary/ingest.py",
        "src/ontary/mcp_server.py",
        "src/ontary/meta.py",
        "src/ontary/model.py",
        "src/ontary/ontology.py",
        "src/ontary/query.py",
        "src/ontary/scope.py",
        "src/ontary/security.py",
        "src/ontary/store/_shared.py",
        "src/ontary/store/_sql.py",
        "src/ontary/store/inmemory.py",
        "src/ontary/store/postgres.py",
        "src/ontary/store/protocol.py",
        "src/ontary/store/sqlite.py",
        "src/ontary/store/values.py",
        "src/ontary/typesys.py",
    }
)

_ROOT = Path(__file__).resolve().parent.parent
_SRC_ROOT = _ROOT / "src" / "ontary"
_SOURCE_FILES = tuple(sorted(_SRC_ROOT.rglob("*.py")))
_API_REFERENCE_FILES = (
    _ROOT / "docs" / "api-reference.md",
    _ROOT / "docs" / "api-reference.ja.md",
)
_ISO_DATE_PREFIX = re.compile(r"\d{4}-\d{2}-\d{2}$")
_ISO_TIME_SUFFIX = re.compile(r":\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?(?![\w:])")

_SCANNED_TOKEN_TYPES = {tokenize.COMMENT, tokenize.STRING}
_FSTRING_MIDDLE = getattr(tokenize, "FSTRING_MIDDLE", None)
if _FSTRING_MIDDLE is not None:
    _SCANNED_TOKEN_TYPES.add(_FSTRING_MIDDLE)


def _matches(text: str) -> list[tuple[int, str]]:
    """Return (character offset, token) pairs in source order."""
    matches: list[tuple[int, str]] = []
    for pattern_index, pattern in enumerate(PATTERNS):
        for match in pattern.finditer(text):
            if pattern_index == 1 and _is_iso_timestamp(text, match.start(), match.end()):
                continue
            matches.append((match.start(), match.group()))
    matches.sort(key=lambda item: item[0])
    return matches


def _is_iso_timestamp(text: str, start: int, end: int) -> bool:
    """Do not mistake the ISO date-time separator and hour for a task id."""
    return bool(_ISO_DATE_PREFIX.search(text[:start]) and _ISO_TIME_SUFFIX.match(text, end))


def find_spec_ids(source: str) -> list[tuple[int, str]]:
    """Find spec-id patterns only in Python comments and string contents."""
    lines = source.splitlines(keepends=True) + [""]
    readline = iter(lines).__next__
    found: list[tuple[int, str]] = []
    for item in tokenize.generate_tokens(readline):
        if item.type not in _SCANNED_TOKEN_TYPES:
            continue
        for offset, token in _matches(item.string):
            line = item.start[0] + item.string.count("\n", 0, offset)
            found.append((line, token))
    return found


def find_spec_ids_in_text(text: str) -> list[tuple[int, str]]:
    """Find spec-id patterns in plain text, with one-based line numbers."""
    return [(text.count("\n", 0, offset) + 1, token) for offset, token in _matches(text)]


_PATTERN_SAMPLES = (
    (0, "DSO/domain module (spec AC1).", "AC1"),
    (
        1,
        "T1 covers object-type authoring (`OntologyObject`, `prop()`, `Ontology.object()`).",
        "T1",
    ),
    (2, "specs/ontary-platform.md §3 AC6/AC7, §5, §7).", "§"),
    (3, "DSO/domain module (spec AC1).", "spec"),
    (4, "`_lineage` (the engine's frozen `Lineage`, M3.5 T7).", "M3.5"),
    (5, "Shared low-level typed-model layer (C1 of the staged refactor).", "C1"),
    (6, "pagination-hardening T2 review P1)", "P1"),
    (7, "**Cost to be aware of (spec §5 Q5 / §8 R5):**", "Q5"),
    (
        8,
        "ontology, only derive values from already-guarded reads (原則1 / spec AC3).",
        "原則",
    ),
)


@pytest.mark.parametrize(("pattern_index", "snippet", "expected"), _PATTERN_SAMPLES)
def test_find_spec_ids_flags_each_pattern_kind(
    pattern_index: int, snippet: str, expected: str
) -> None:
    """Each pattern is pinned to wording copied from a current source comment/docstring."""
    assert PATTERNS[pattern_index].search(snippet) is not None
    assert (1, expected) in find_spec_ids(f'"""{snippet}"""')


def test_find_spec_ids_ignores_code_and_legitimate_text() -> None:
    """Identifiers, issue numbers, type variables, timestamps, layer names, and ordinary words are safe."""
    source = """
# See issue #49.
type_spec = "ordinary value"
ParamSpec = object()
T = TypeVar("T")
timestamp = "2026-01-01T12:00"
label = "specify"
layer = "L0"
"""
    assert find_spec_ids(source) == []


def test_find_spec_ids_scans_f_string_text() -> None:
    """Python 3.12+ exposes f-string text as FSTRING_MIDDLE tokens."""
    if _FSTRING_MIDDLE is None:
        pytest.skip("tokenize does not expose FSTRING_MIDDLE on this Python")
    assert find_spec_ids('label = f"Current AC8: {name}"') == [(1, "AC8")]


def test_find_spec_ids_in_text_reports_line_numbers() -> None:
    assert find_spec_ids_in_text("Overview\nAC10 and §6\n") == [
        (2, "AC10"),
        (2, "§"),
    ]


def _assert_pending_ratchet(path: str, hits: list[tuple[int, str]]) -> None:
    if path in _PENDING:
        assert hits, f"{path}: now clean; remove it from _PENDING"
        return
    details = "\n".join(f"{path}:{line}: {token}" for line, token in hits)
    assert not hits, details


@pytest.mark.parametrize("path", _SOURCE_FILES, ids=lambda path: path.relative_to(_ROOT).as_posix())
def test_src_has_no_spec_ids(path: Path) -> None:
    relative_path = path.relative_to(_ROOT).as_posix()
    _assert_pending_ratchet(relative_path, find_spec_ids(path.read_text()))


@pytest.mark.parametrize(
    "path",
    _API_REFERENCE_FILES,
    ids=lambda path: path.relative_to(_ROOT).as_posix(),
)
def test_api_reference_has_no_spec_ids(path: Path) -> None:
    relative_path = path.relative_to(_ROOT).as_posix()
    _assert_pending_ratchet(relative_path, find_spec_ids_in_text(path.read_text()))
