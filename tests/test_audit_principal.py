"""`principal` on `Consumer` and `AuditEntry` -- the identity a transport
PROVED, kept separate from the actor a request RESOLVED to (spec
`multi-consumer-mcp` AC8/AC9).

Test matrix #1 (field defaults) plus #2 (the structural AST-walk over every
`AuditEntry(...)` call site in `src/ontary` -- spec §7/§9: a missed site
would silently write `principal = None` and no functional test would
notice, so it is guarded structurally rather than per-site) plus #3/#4/#5
(integration coverage, through a REAL store, that a principal-stamped
consumer's audit entries actually carry it: ok, denied, error, and the
function-audit boundary).

#2 is FAIL-CLOSED: every `AuditEntry(...)` construction under `src/ontary`
must pass `principal=`, unless its `(module, enclosing function)` pair is on
the small, reasoned `_ALLOWLIST` below of sites that are genuinely
consumer-free (engine migrations). This is deliberately NOT "look for the
`actor=<x>.actor_id, role=<x>.role` shape a Consumer-built call happens to
have today" -- that shape is exactly what a future call site is free to not
match (e.g. building the entry from an already-built `AuditEntry`) while
still being a real, consumer-derived, must-stamp site. Keying the allowlist on the
ENCLOSING FUNCTION NAME rather than a line number means it survives
unrelated edits; asserting every allowlist entry is actually matched by a
real call site means a renamed/deleted allowlisted function fails loudly
rather than rotting silently (the same class of staleness T3's reviewer
caught in `_init_schema`).
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import raises_code

from ontary import (
    ActionContext,
    ActionParams,
    Consumer,
    Ontology,
    OntologyObject,
    prop,
)
from ontary.audit import AuditEntry
from ontary.client import OntologyClient
from ontary.errors import PermissionDenied
from ontary.store import Store

ConsumerFactory = Callable[..., Consumer]
StoreFactory = Callable[..., Store]


def test_consumer_principal_defaults_to_none() -> None:
    consumer = Consumer(
        actor_id="ryo", role="Admin", scope_level="org", scope_id="org-1", kind="human"
    )
    assert consumer.principal is None


def test_audit_entry_principal_defaults_to_none() -> None:
    entry = AuditEntry(
        actor="ryo",
        role="Admin",
        action="Touch",
        target_type="Org",
        outcome="ok",
    )
    assert entry.principal is None


def test_consumer_principal_round_trips_through_model_dump() -> None:
    consumer = Consumer(
        actor_id="ryo",
        role="Admin",
        scope_level="org",
        scope_id="org-1",
        kind="human",
        principal="svc-agent-7",
    )
    assert consumer.model_dump()["principal"] == "svc-agent-7"


def test_audit_entry_principal_round_trips_through_model_dump() -> None:
    entry = AuditEntry(
        actor="ryo",
        role="Admin",
        action="Touch",
        target_type="Org",
        outcome="ok",
        principal="svc-agent-7",
    )
    assert entry.model_dump()["principal"] == "svc-agent-7"


# -- #2: structural guard, FAIL-CLOSED -- every AuditEntry(...) site must --
# -- pass principal=, unless its (module, enclosing function) is allowlisted -

_SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "ontary"


def _is_audit_entry_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id == "AuditEntry"
    return isinstance(fn, ast.Attribute) and fn.attr == "AuditEntry"


# The ONLY sites where an `AuditEntry(...)` is built without a `Consumer`
# available to stamp a `principal` from at all -- verified by hand against
# every one of the 6 `AuditEntry(...)` call sites under `src/ontary` on
# 2026-09-06 (`grep -rn "AuditEntry(" src/ontary`), not merely inherited
# from a claim. Every other site passes `principal=` directly. Keyed on the
# ENCLOSING FUNCTION NAME (not a line number) so it survives edits elsewhere
# in the file; `test_allowlist_entries_all_still_match_a_real_site` asserts
# every entry here is still actually matched by a real, unstamped call, so a
# rename/deletion of the function fails loudly instead of rotting silently.
_ALLOWLIST: dict[tuple[str, str], str] = {
    ("migrate.py", "_audit_migration"): (
        "engine-driven object-type migration (`migrate_object_type`) -- "
        "no consumer initiates it, so there is nothing to stamp a "
        "principal from"
    ),
    ("protocol.py", "check_ontology_fingerprint"): (
        "engine-driven ontology-fingerprint check/migration (both its "
        "audit sites) -- no consumer initiates it"
    ),
}


def _audit_entry_calls_by_function(tree: ast.Module) -> list[tuple[ast.Call, str]]:
    """Every `AuditEntry(...)` call in `tree`, paired with the name of its
    nearest enclosing function (`"<module>"` if none). Walking with an
    explicit function-name parameter, rather than `ast.walk`, is what lets
    the guard key on WHAT FUNCTION a call lives in rather than a line
    number."""
    calls: list[tuple[ast.Call, str]] = []

    def walk(node: ast.AST, func_name: str) -> None:
        for child in ast.iter_child_nodes(node):
            next_name = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else func_name
            )
            if _is_audit_entry_call(child):
                assert isinstance(child, ast.Call)
                calls.append((child, func_name))
            walk(child, next_name)

    walk(tree, "<module>")
    return calls


def _find_unstamped_audit_entry_sites(root: Path) -> tuple[list[str], set[tuple[str, str]]]:
    """FAIL CLOSED (spec §7/§9): every `AuditEntry(...)` call under `root`
    must pass `principal=`, unless its `(module filename, enclosing
    function)` is in `_ALLOWLIST`. Returns the offending sites (any
    unstamped call NOT on the allowlist) and the subset of the allowlist
    actually matched by a real, unstamped call in this tree -- so a caller
    can separately detect a ROTTED allowlist entry (one naming a function
    that no longer exists, or no longer calls `AuditEntry` unstamped)."""
    offenders: list[str] = []
    used: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node, func_name in _audit_entry_calls_by_function(tree):
            keywords = {kw.arg for kw in node.keywords if kw.arg is not None}
            if "principal" in keywords:
                continue
            key = (path.name, func_name)
            if key in _ALLOWLIST:
                used.add(key)
                continue
            try:
                display = path.relative_to(root.parent.parent)
            except ValueError:
                display = path
            offenders.append(f"{display}:{node.lineno} (in {func_name!r})")
    return offenders, used


def test_every_audit_entry_call_stamps_principal_or_is_allowlisted() -> None:
    offenders, _used = _find_unstamped_audit_entry_sites(_SRC_DIR)
    assert offenders == [], (
        "AuditEntry(...) call(s) do not pass principal= and are not on the "
        "allowlist of genuinely consumer-free sites (spec multi-consumer-mcp "
        "AC8/§9) -- an invisible audit hole. Offending site(s), file:line "
        "(enclosing function): " + ", ".join(offenders)
    )


def test_allowlist_entries_all_still_match_a_real_site() -> None:
    """The allowlist's OTHER failure mode: an entry naming a `(module,
    function)` that no longer matches any real, unstamped `AuditEntry(...)`
    call -- because the function was renamed, deleted, or started passing
    `principal=` itself -- must fail loudly rather than rot silently. Same
    staleness class T3's reviewer caught in `_init_schema`."""
    _offenders, used = _find_unstamped_audit_entry_sites(_SRC_DIR)
    missing = set(_ALLOWLIST) - used
    assert missing == set(), (
        "allowlist entry/entries no longer match any unstamped "
        f"AuditEntry(...) call site in src/ontary -- rotted: {sorted(missing)}"
    )


def test_structural_guard_catches_a_new_unstamped_site(tmp_path: Path) -> None:
    """Regression test for the guard itself: a site missing `principal=`
    and not on the allowlist must be reported, not silently accepted -- this
    is what the reviewer's mutation test (adding an unstamped site) exercises."""
    (tmp_path / "unstamped.py").write_text(
        "from ontary.store import AuditEntry\n"
        "\n"
        "def f(consumer):\n"
        "    return AuditEntry(\n"
        "        actor=consumer.actor_id,\n"
        "        role=consumer.role,\n"
        "        action='X',\n"
        "        target_type='Y',\n"
        "        outcome='ok',\n"
        "    )\n",
        encoding="utf-8",
    )

    offenders, _used = _find_unstamped_audit_entry_sites(tmp_path)

    assert len(offenders) == 1
    assert "unstamped.py:4 (in 'f')" in offenders[0]


def test_structural_guard_catches_the_already_built_entry_style(tmp_path: Path) -> None:
    """The style P1-1 found the OLD (consumer-derived-shape) guard blind to:
    an `AuditEntry(...)` built from an ALREADY-BUILT entry's own fields
    (`pending_entry.actor`/`.role`) rather than a `Consumer` directly. The fail-closed guard does not care what the values come
    from -- only that `principal=` is present -- so this is caught too."""
    (tmp_path / "already_built.py").write_text(
        "from ontary.store import AuditEntry\n"
        "\n"
        "def f(pending_entry):\n"
        "    return AuditEntry(\n"
        "        actor=pending_entry.actor,\n"
        "        role=pending_entry.role,\n"
        "        action=pending_entry.action,\n"
        "        target_type=pending_entry.target_type,\n"
        "        outcome='ok',\n"
        "    )\n",
        encoding="utf-8",
    )

    offenders, _used = _find_unstamped_audit_entry_sites(tmp_path)

    assert len(offenders) == 1
    assert "already_built.py:4 (in 'f')" in offenders[0]


def test_structural_guard_passes_once_the_site_stamps_principal(tmp_path: Path) -> None:
    """The other direction: the SAME site, with `principal=` added, is not
    reported."""
    (tmp_path / "stamped.py").write_text(
        "from ontary.store import AuditEntry\n"
        "\n"
        "def f(consumer):\n"
        "    return AuditEntry(\n"
        "        actor=consumer.actor_id,\n"
        "        role=consumer.role,\n"
        "        principal=consumer.principal,\n"
        "        action='X',\n"
        "        target_type='Y',\n"
        "        outcome='ok',\n"
        "    )\n",
        encoding="utf-8",
    )

    offenders, _used = _find_unstamped_audit_entry_sites(tmp_path)
    assert offenders == []


def test_structural_guard_does_not_flag_an_allowlisted_site(tmp_path: Path) -> None:
    """A call inside a real allowlisted `(module, function)` pair -- here
    `migrate.py`'s `_audit_migration` -- is exempt from requiring
    `principal=`, AND is recorded as a MATCH (not silently ignored), so the
    rot-detection test below has something real to check against."""
    (tmp_path / "migrate.py").write_text(
        "from ontary.store import AuditEntry\n"
        "\n"
        "def _audit_migration(record):\n"
        "    return AuditEntry(\n"
        "        actor='engine',\n"
        "        role='engine',\n"
        "        action='X',\n"
        "        target_type='Y',\n"
        "        outcome='ok',\n"
        "    )\n",
        encoding="utf-8",
    )

    offenders, used = _find_unstamped_audit_entry_sites(tmp_path)
    assert offenders == []
    assert ("migrate.py", "_audit_migration") in used


def test_structural_guard_flags_a_rotted_allowlist_entry(tmp_path: Path) -> None:
    """The allowlist's rot-detection mechanism itself, exercised directly:
    on a tree containing NONE of the allowlisted sites (here, simply
    empty), none of `_ALLOWLIST`'s entries can have been matched --
    mirroring what `test_allowlist_entries_all_still_match_a_real_site`
    would report as a failure if a real allowlisted site disappeared from
    `src/ontary`."""
    _offenders, used = _find_unstamped_audit_entry_sites(tmp_path)
    assert used == set()
    assert set(_ALLOWLIST) - used == set(_ALLOWLIST)


# -- #3/#4/#5: integration, through a real store ----------------------------


def _ontology() -> tuple[Ontology, Any]:
    ontology = Ontology("audit-principal", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Record(OntologyObject):
        id: str = prop(primary_key=True)

    class TouchParams(ActionParams):
        id: str

    @ontology.action(TouchParams, target=Record, roles=["Operator"], api_name="Touch")
    def _touch(ctx: ActionContext, params: TouchParams) -> dict[str, str]:
        ctx.insert("Record", {"id": params.id})
        return {"id": params.id}

    class AdminOnlyParams(ActionParams):
        id: str

    @ontology.action(
        AdminOnlyParams, target=Record, roles=["Admin"], api_name="AdminOnly"
    )
    def _admin_only(ctx: ActionContext, params: AdminOnlyParams) -> dict[str, str]:
        ctx.insert("Record", {"id": params.id})
        return {"id": params.id}

    class FailParams(ActionParams):
        id: str

    @ontology.action(FailParams, target=Record, roles=["Operator"], api_name="Fail")
    def _fail(ctx: ActionContext, params: FailParams) -> dict[str, str]:
        raise RuntimeError("handler always fails")

    @ontology.function(api_name="pureButAudited", audit=True)
    def _pure_but_audited(_query: Any, _params: dict[str, Any]) -> int:
        return 7

    ontology.validate()
    return ontology


def test_ok_action_writes_principal_on_its_audit_entry(
    make_consumer: ConsumerFactory, make_store: StoreFactory
) -> None:
    ontology = _ontology()
    store = make_store(ontology.registry)
    client = OntologyClient(
        ontology,
        store,
        make_consumer(
            actor_id="ryo",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
            principal="svc-agent-7",
        ),
    )

    client.execute("Touch", {"id": "r1"})

    entries = store.audit_entries()
    assert entries[-1].outcome == "ok"
    assert entries[-1].principal == "svc-agent-7"
    assert entries[-1].actor == "ryo"


def test_denied_action_writes_principal_on_its_audit_entry(
    make_consumer: ConsumerFactory, make_store: StoreFactory
) -> None:
    ontology = _ontology()
    store = make_store(ontology.registry)
    client = OntologyClient(
        ontology,
        store,
        make_consumer(
            actor_id="ryo",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
            principal="svc-agent-7",
        ),
    )

    with raises_code(PermissionDenied, "PERMISSION_DENIED"):
        client.execute("AdminOnly", {"id": "r1"})

    entries = store.audit_entries()
    assert entries[-1].outcome == "denied"
    assert entries[-1].principal == "svc-agent-7"


def test_error_action_writes_principal_on_its_audit_entry(
    make_consumer: ConsumerFactory, make_store: StoreFactory
) -> None:
    ontology = _ontology()
    store = make_store(ontology.registry)
    client = OntologyClient(
        ontology,
        store,
        make_consumer(
            actor_id="ryo",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
            principal="svc-agent-7",
        ),
    )

    with pytest.raises(RuntimeError, match="handler always fails"):
        client.execute("Fail", {"id": "r1"})

    entries = store.audit_entries()
    assert entries[-1].outcome == "error"
    assert entries[-1].principal == "svc-agent-7"


def test_single_consumer_execution_leaves_principal_none(
    make_consumer: ConsumerFactory, make_store: StoreFactory
) -> None:
    """Direct-Python / single-consumer execution never stamps a principal
    (spec AC8) -- the default `None` still round-trips through a real store,
    not just the model."""
    ontology = _ontology()
    store = make_store(ontology.registry)
    client = OntologyClient(
        ontology,
        store,
        make_consumer(
            actor_id="ryo",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
            principal=None,
        ),
    )

    client.execute("Touch", {"id": "r1"})

    assert store.audit_entries()[-1].principal is None


def test_function_boundary_writes_principal_on_its_audit_entry(
    make_consumer: ConsumerFactory, make_store: StoreFactory
) -> None:
    ontology = _ontology()
    store = make_store(ontology.registry)
    client = OntologyClient(
        ontology,
        store,
        make_consumer(
            actor_id="ryo",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
            principal="svc-agent-7",
        ),
    )

    assert client.call_function("pureButAudited", {}) == 7

    entries = store.audit_entries()
    assert len(entries) == 1
    assert entries[0].kind == "function"
    assert entries[0].principal == "svc-agent-7"
