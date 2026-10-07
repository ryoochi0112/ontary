"""Pins the shape of the `ontary.query` package: exports, size cap, concern split."""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
import textwrap
from pathlib import Path

import ontary
import ontary.query
from ontary.query import GuardedQuery

QUERY_DIR = Path(ontary.query.__file__).parent
PUBLIC_NAMES = [
    "DEFAULT_READ_LIMIT",
    "AggregateFunc",
    "AggregateValue",
    "GuardedQuery",
    "OrderBy",
    "Page",
    "TypedPage",
]
MAX_LINES = 800

# `ontary.__all__` as released in v0.25.0 (commit 5418ac5).
RELEASED_TOP_LEVEL_ALL = [
    "ActionContext",
    "ActionError",
    "ActionParams",
    "AuthorityError",
    "BoundQuery",
    "CapabilityHandle",
    "Cardinality",
    "ConflictError",
    "Consumer",
    "CustomResolver",
    "Declarations",
    "DirectProperty",
    "Event",
    "EventRecord",
    "Finding",
    "FunctionParams",
    "InMemoryStore",
    "InternalError",
    "LinkHandle",
    "MCPServer",
    "ObjectStore",
    "OntaryError",
    "Ontology",
    "OntologyClient",
    "OntologyObject",
    "Page",
    "PermissionDenied",
    "PostgresStore",
    "PreconditionFailed",
    "RowVisibilityStore",
    "ScopePolicy",
    "SelfScope",
    "Sensitivity",
    "Source",
    "Store",
    "TypedPage",
    "ValidationFailed",
    "ViaLink",
    "VisibilityError",
    "__version__",
    "build_mcp_server",
    "declarations",
    "prop",
    "ref",
    "scope_ref",
    "target",
]


def test_query_all_is_exactly_the_public_names() -> None:
    assert sorted(ontary.query.__all__) == sorted(PUBLIC_NAMES)


def test_public_names_import_in_a_fresh_interpreter() -> None:
    code = f"from ontary.query import {', '.join(PUBLIC_NAMES)}"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_every_query_module_stays_under_the_size_cap() -> None:
    files = sorted(QUERY_DIR.glob("*.py"))
    assert files
    too_long = {
        f.name: n
        for f in files
        if (n := len(f.read_text(encoding="utf-8").splitlines())) > MAX_LINES
    }
    assert not too_long, f"over {MAX_LINES} lines: {too_long}"


def _defining_modules(names: list[str]) -> set[str]:
    modules: set[str] = set()
    for name in names:
        owners = [
            f.stem
            for f in QUERY_DIR.glob("_*.py")
            if f.stem != "__init__" and _defines(f.read_text(encoding="utf-8"), name)
        ]
        assert len(owners) == 1, f"{name} defined in {owners}"
        modules.add(owners[0])
    return modules


def _defines(source: str, name: str) -> bool:
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.ClassDef) and node.name == name:
            return True
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return True
        if isinstance(node, ast.AnnAssign) and (
            isinstance(node.target, ast.Name) and node.target.id == name
        ):
            return True
        if isinstance(node, ast.TypeAlias) and node.name.id == name:
            return True
    return False


def test_each_concern_lives_in_one_distinct_submodule() -> None:
    where = _defining_modules(["_normalize_where", "_evaluate_where"])
    paging = _defining_modules(["Page", "TypedPage", "DEFAULT_READ_LIMIT", "_refuse_page_walk"])
    aggregation = _defining_modules(["_min_n_violation_message", "AggregateFunc", "AggregateValue"])
    disclosure = _defining_modules(["_ReadDisclosure", "_where_disclosure"])
    concerns = [where, paging, aggregation, disclosure]
    assert all(len(c) == 1 for c in concerns), concerns
    names = [next(iter(c)) for c in concerns]
    assert len(set(names)) == 4, names
    assert "_guarded" not in names
    assert _defines((QUERY_DIR / "_guarded.py").read_text("utf-8"), "GuardedQuery")


def _delegate_call(source: str) -> ast.Call:
    """The call a delegate returns; fails unless the body is that one `return`."""
    func = ast.parse(textwrap.dedent(source)).body[0]
    assert isinstance(func, ast.FunctionDef)
    body = func.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # docstring
    assert len(body) == 1, f"{func.name} is not a one-statement delegate"
    stmt = body[0]
    assert isinstance(stmt, ast.Return)
    assert isinstance(stmt.value, ast.Call)
    assert isinstance(stmt.value.func, ast.Name), "must call a module-level function"
    return stmt.value


def test_aggregate_and_redact_are_delegates() -> None:
    # Each delegate is at most 10 lines: one `return <module function>(...)`.
    guarded = sys.modules["ontary.query._guarded"]
    for member, module in (
        (GuardedQuery._aggregate, "ontary.query._aggregate"),
        (GuardedQuery._redact, "ontary.query._disclosure"),
    ):
        source = inspect.getsource(member)
        assert len(source.splitlines()) <= 10, member.__name__
        call = _delegate_call(source)
        assert isinstance(call.func, ast.Name)
        target = getattr(guarded, call.func.id)
        assert target.__module__ == module, (member.__name__, target.__module__)


def test_top_level_exports_are_unchanged() -> None:
    assert sorted(ontary.__all__) == RELEASED_TOP_LEVEL_ALL
    assert ontary.Page is ontary.query.Page
    assert ontary.TypedPage is ontary.query.TypedPage
