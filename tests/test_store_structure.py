"""Structural guards: backends hold no write rules and no inline write SQL.

The write rules live once in ``StoreCore`` / ``_shared``; the write SQL lives once
in ``_sql`` templates. These tests parse the backend modules and fail if a rule or a
write statement creeps back in, naming the file and line.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from ontary.store import _shared

_STORE_DIR = Path(__file__).resolve().parent.parent / "src" / "ontary" / "store"
_BACKENDS = ("inmemory.py", "sqlite.py", "postgres.py")
_SQL_BACKENDS = ("sqlite.py", "postgres.py")

_FIXED_FORBIDDEN = frozenset(
    {
        "prepare_insert",
        "merge_update",
        "canonical_id",
        "ensure_not_before",
        "object_already_exists",
        "retire_object_refusal",
        "live_link_not_found",
        "cardinality_violation",
        "Cardinality",
    }
)
_ALLOWED_CONFLICT_CODES = frozenset({"STORE_BUSY", "STORE_VERSION_UNSUPPORTED"})
_WRITE_SQL = re.compile(
    r"\b(?:INSERT\s+INTO\s+(?:objects|links|audit_log)|UPDATE\s+(?:objects|links))\b",
    re.IGNORECASE,
)


def _check_names() -> frozenset[str]:
    return frozenset(n for n in vars(_shared) if n.startswith("check_"))


def _forbidden_names() -> frozenset[str]:
    return _FIXED_FORBIDDEN | _check_names()


def _parse(source: str, filename: str) -> ast.Module:
    return ast.parse(source, filename=filename)


def _conflict_code(call: ast.Call) -> object:
    for kw in call.keywords:
        if kw.arg == "code" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def _is_name(node: ast.AST, name: str) -> bool:
    return (isinstance(node, ast.Name) and node.id == name) or (
        isinstance(node, ast.Attribute) and node.attr == name
    )


def rule_violations(source: str, filename: str) -> list[str]:
    """Return 'file:line: message' for every write-rule reference in ``source``."""
    forbidden = _forbidden_names()
    tree = _parse(source, filename)
    allowed_conflict_nodes: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call)
            and _is_name(node.exc.func, "ConflictError")
            and _conflict_code(node.exc) in _ALLOWED_CONFLICT_CODES
        ):
            allowed_conflict_nodes.add(id(node.exc.func))

    found: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(node, ast.ImportFrom | ast.Import):
            names = [a.name.split(".")[-1] for a in node.names]
            names += [a.asname for a in node.names if a.asname]
        for name in names:
            if name in forbidden:
                found.append(f"{filename}:{node.lineno}: references {name!r}")
            elif name == "ConflictError":
                if isinstance(node, ast.ImportFrom | ast.Import):
                    continue  # the import itself; uses are checked at each raise
                if id(node) not in allowed_conflict_nodes:
                    found.append(
                        f"{filename}:{node.lineno}: ConflictError outside a raise with "
                        f"code= one of {sorted(_ALLOWED_CONFLICT_CODES)}"
                    )
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "CARDINALITY_VIOLATION" in node.value
        ):
            found.append(f"{filename}:{node.lineno}: contains 'CARDINALITY_VIOLATION'")
    return found


def sql_violations(source: str, filename: str) -> list[str]:
    """Return 'file:line: message' for every inline write-SQL string literal."""
    found: list[str] = []
    for node in ast.walk(_parse(source, filename)):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _WRITE_SQL.search(node.value)
        ):
            found.append(f"{filename}:{node.lineno}: inline write SQL literal")
    return found


def test_the_derived_check_helper_set_is_not_empty() -> None:
    names = _check_names()
    assert names, "no check_* names found in ontary.store._shared"
    assert "check_link_endpoints" in names


@pytest.mark.parametrize("backend", _BACKENDS)
def test_backend_holds_no_write_rules(backend: str) -> None:
    path = _STORE_DIR / backend
    violations = rule_violations(path.read_text(), str(path))
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize("backend", _SQL_BACKENDS)
def test_backend_holds_no_inline_write_sql(backend: str) -> None:
    path = _STORE_DIR / backend
    violations = sql_violations(path.read_text(), str(path))
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    ("snippet", "expected_fragment"),
    [
        ("from ontary.store._shared import canonical_id\n", "x.py:1: references 'canonical_id'"),
        (
            "import ontary.store._shared as s\ns.merge_update()\n",
            "x.py:2: references 'merge_update'",
        ),
        ("from ontary.store._shared import check_link_endpoints\n", "check_link_endpoints"),
        ("x = Cardinality.ONE\n", "x.py:1: references 'Cardinality'"),
        ("x = 'CARDINALITY_VIOLATION'\n", "x.py:1: contains 'CARDINALITY_VIOLATION'"),
        ("raise ConflictError('m', code='OTHER')\n", "x.py:1: ConflictError outside a raise"),
        ("raise ConflictError('m')\n", "x.py:1: ConflictError outside a raise"),
        ("e = ConflictError('m', code='STORE_BUSY')\n", "x.py:1: ConflictError outside a raise"),
    ],
)
def test_rule_detector_catches_each_defect(snippet: str, expected_fragment: str) -> None:
    assert any(expected_fragment in v for v in rule_violations(snippet, "x.py"))


@pytest.mark.parametrize(
    "snippet",
    [
        "raise ConflictError('m', code='STORE_BUSY')\n",
        "raise ConflictError('m', code='STORE_VERSION_UNSUPPORTED') from exc\n",
        "from ontary.errors import ConflictError\n",
    ],
)
def test_rule_detector_allows_storage_errors(snippet: str) -> None:
    assert rule_violations(snippet, "x.py") == []


@pytest.mark.parametrize(
    "literal",
    [
        "INSERT INTO objects (a) VALUES (1)",
        "insert into links (a) values (1)",
        "INSERT  INTO audit_log (a) VALUES (1)",
        "UPDATE objects SET a = 1",
        "UPDATE links SET a = 1",
    ],
)
def test_sql_detector_catches_each_write_statement(literal: str) -> None:
    hits = sql_violations(f"q = {literal!r}\n", "x.py")
    assert hits == ["x.py:1: inline write SQL literal"]


def test_sql_detector_ignores_reads_and_other_tables() -> None:
    assert (
        sql_violations("q = 'SELECT * FROM objects'\nr = 'UPDATE schema_meta SET v = 1'\n", "x.py")
        == []
    )
