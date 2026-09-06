"""Operator-only erasure orchestration (lifecycle-queries AC9-AC11)."""

from __future__ import annotations

import ast
import itertools
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import raises_code

import ontary
import ontary.erase as erase_module
from ontary.actions import ActionContext
from ontary.audit import AuditEntry
from ontary.client import OntologyClient
from ontary.erase import erase_object
from ontary.errors import ValidationFailed
from ontary.meta import ObjectTypeDef, OntologyRegistry, PropertyDef
from ontary.outbox import OutboxRecord
from ontary.store import InMemoryStore, ObjectStore, Source, Store


def _registry() -> OntologyRegistry:
    registry = OntologyRegistry()
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Team",
            display_name="Team",
            description="A team",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="name", type="str"),
            ],
            primary_key="id",
        )
    )
    registry.validate()
    return registry


StoreFactory = Callable[[OntologyRegistry], Store]


def _make_object_store(registry: OntologyRegistry) -> Store:
    return ObjectStore(registry)


def _make_in_memory_store(registry: OntologyRegistry) -> Store:
    return InMemoryStore(registry)


POSTGRES_DSN = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
_postgres_schema_counter = itertools.count()


def _make_postgres_store(registry: OntologyRegistry) -> Store:
    from ontary.store.postgres import PostgresStore

    assert POSTGRES_DSN is not None
    schema = f"ontary_test_erase_{next(_postgres_schema_counter)}"
    import psycopg

    with psycopg.connect(POSTGRES_DSN, autocommit=True) as setup:
        setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        setup.execute(f'CREATE SCHEMA "{schema}"')
    separator = "&" if "?" in POSTGRES_DSN else "?"
    return PostgresStore(
        registry, f"{POSTGRES_DSN}{separator}options=-csearch_path%3D{schema}"
    )


STORE_FACTORIES: list[StoreFactory] = [
    _make_object_store,
    _make_in_memory_store,
]
if POSTGRES_DSN:
    STORE_FACTORIES.append(_make_postgres_store)


@pytest.fixture(params=STORE_FACTORIES, ids=lambda f: f.__name__.removeprefix("_make_"))
def store(request: pytest.FixtureRequest) -> Store:
    factory: StoreFactory = request.param
    return factory(_registry())


def test_erase_object_audits_operator_time_target_and_all_counts(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    object_id = store.insert(
        "Team",
        {"id": "team-1", "name": "historical secret"},
        Source(source_system="connector"),
    )
    store.update(
        "Team",
        object_id,
        {"name": "current secret"},
        Source(source_system="connector"),
    )
    started_at = datetime(2026, 8, 25, 9, 30, tzinfo=timezone.utc)
    store.append_audit(
        AuditEntry(
            ts=started_at,
            actor="alice",
            role="DataSteward",
            action="UpdateTeam",
            target_type="Team",
            target_id=object_id,
            params={"name": "audit secret"},
            outcome="ok",
            invocation_id="inv-team-1",
        )
    )
    store.enqueue_effects(
        [
            OutboxRecord(
                effect_id="effect-team-1",
                invocation_id="inv-team-1",
                seq=0,
                api_name="notify",
                payload={"message": "outbox secret"},
                action="UpdateTeam",
                actor_id="alice",
                role="DataSteward",
                emitted_at=started_at,
                state="delivered",
                attempts=1,
                next_attempt_at=started_at,
                updated_at=started_at,
            )
        ]
    )
    erased_at = started_at + timedelta(minutes=5)
    monkeypatch.setattr(erase_module, "_utcnow", lambda: erased_at)

    report = erase_object(store, "Team", object_id, operator="privacy-ops@example.com")

    assert report.model_dump() == {
        "object_type": "Team",
        "object_id": object_id,
        "operator": "privacy-ops@example.com",
        "erased_at": erased_at,
        "outcome": "erased",
        "code": None,
        "object_rows_purged": 2,
        "audit_entries_purged": 1,
        "outbox_rows_purged": 1,
    }
    assert store.read_current("Team", object_id) is None
    assert store.outbox_entries()[0].payload == {}

    prior, erasure = store.audit_entries()
    assert prior.params == {}
    assert erasure.ts == erased_at
    assert erasure.kind == "erasure"
    assert erasure.actor == "privacy-ops@example.com"
    assert erasure.role == "operator"
    assert erasure.action == "erase_object"
    assert erasure.target_type == "Team"
    assert erasure.target_id == object_id
    assert erasure.params == {
        "object_rows_purged": 2,
        "audit_entries_purged": 1,
        "outbox_rows_purged": 1,
    }
    assert erasure.outcome == "erased"


def test_erase_object_second_call_is_coded_no_op(store: Store) -> None:
    object_id = store.insert(
        "Team",
        {"id": "team-idempotent", "name": "secret"},
        Source(source_system="connector"),
    )
    first = erase_object(store, "Team", object_id, operator="first-operator")
    after_first = store.audit_entries()

    second = erase_object(store, "Team", object_id, operator="second-operator")

    assert second.object_type == "Team"
    assert second.object_id == object_id
    assert second.operator == "second-operator"
    assert second.erased_at == first.erased_at
    assert second.outcome == "no_op"
    assert second.code == "OBJECT_ALREADY_ERASED"
    assert second.object_rows_purged == 0
    assert second.audit_entries_purged == 0
    assert second.outbox_rows_purged == 0
    assert store.audit_entries() == after_first


def test_erase_object_audits_raw_store_purge_without_marker(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    object_id = store.insert(
        "Team",
        {"id": "team-raw-purge", "name": "raw purge secret"},
        Source(source_system="connector"),
    )
    raw_result = store.erase_object_content("Team", object_id)
    assert raw_result == (1, 0, 0)
    assert store.audit_entries() == []

    erased_at = datetime(2026, 8, 27, 10, 11, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(erase_module, "_utcnow", lambda: erased_at)

    report = erase_object(store, "Team", object_id, operator="privacy-ops")

    assert report.model_dump() == {
        "object_type": "Team",
        "object_id": object_id,
        "operator": "privacy-ops",
        "erased_at": erased_at,
        "outcome": "erased",
        "code": None,
        "object_rows_purged": 0,
        "audit_entries_purged": 0,
        "outbox_rows_purged": 0,
    }
    erasures = [entry for entry in store.audit_entries() if entry.kind == "erasure"]
    assert len(erasures) == 1
    assert erasures[0].ts == erased_at
    assert erasures[0].params == {
        "object_rows_purged": 0,
        "audit_entries_purged": 0,
        "outbox_rows_purged": 0,
    }
    assert erasures[0].outcome == "erased"


def test_erase_object_purges_late_arriving_audit_content(store: Store) -> None:
    object_id = store.insert(
        "Team",
        {"id": "team-late-arriving", "name": "initial secret"},
        Source(source_system="connector"),
    )
    first = erase_object(store, "Team", object_id, operator="first-operator")

    store.append_audit(
        AuditEntry(
            actor="alice",
            role="DataSteward",
            action="LogGrievance",
            target_type="Team",
            target_id=object_id,
            params={"note": "PII-LATE-ARRIVING-FREE-TEXT"},
            outcome="ok",
        )
    )

    second = erase_object(store, "Team", object_id, operator="second-operator")

    assert first.outcome == "erased"
    assert second.outcome == "erased"
    assert second.code is None
    assert second.object_rows_purged == 0
    assert second.audit_entries_purged == 1
    assert second.outbox_rows_purged == 0
    late_action = next(
        entry for entry in store.audit_entries() if entry.action == "LogGrievance"
    )
    assert late_action.params == {}
    assert len([entry for entry in store.audit_entries() if entry.kind == "erasure"]) == 2


def test_erase_object_reingested_same_id_is_erased_again(store: Store) -> None:
    object_id = store.insert(
        "Team",
        {"id": "team-reingested", "name": "first secret"},
        Source(source_system="connector"),
    )
    first = erase_object(store, "Team", object_id, operator="first-operator")
    first_evidence = store.audit_entries()[-1].model_copy(deep=True)
    store.insert(
        "Team",
        {"id": object_id, "name": "reingested secret"},
        Source(source_system="connector"),
    )

    second = erase_object(store, "Team", object_id, operator="second-operator")

    assert second.outcome == "erased"
    assert second.code is None
    assert second.object_rows_purged == 1
    assert store.read_current("Team", object_id) is None
    erasures = [entry for entry in store.audit_entries() if entry.kind == "erasure"]
    assert len(erasures) == 2
    assert erasures[0] == first_evidence
    assert erasures[0].actor == "first-operator"
    assert erasures[0].ts == first.erased_at
    assert erasures[0].params == {
        "object_rows_purged": 1,
        "audit_entries_purged": 0,
        "outbox_rows_purged": 0,
    }


def test_erase_object_never_existed_uses_erasure_specific_refusal(store: Store) -> None:
    with raises_code(ValidationFailed, "OBJECT_ERASURE_NOT_FOUND"):
        erase_object(store, "Team", "never-existed", operator="privacy-ops")

    assert store.audit_entries() == []


def test_erase_object_missing_id_does_not_match_another_object(store: Store) -> None:
    existing_id = store.insert(
        "Team",
        {"id": "team-existing", "name": "content must survive"},
        Source(source_system="connector"),
    )

    with raises_code(ValidationFailed, "OBJECT_ERASURE_NOT_FOUND"):
        erase_object(store, "Team", "team-missing", operator="privacy-ops")

    assert store.audit_entries() == []
    existing = store.read_current("Team", existing_id)
    assert existing is not None
    assert existing.payload == {
        "id": "team-existing",
        "name": "content must survive",
    }


def test_erase_object_never_existed_does_not_purge_referencing_audit(
    store: Store,
) -> None:
    store.append_audit(
        AuditEntry(
            actor="alice",
            role="DataSteward",
            action="ReferenceTeam",
            target_type="Company",
            target_id="company-1",
            params={
                "subject": {
                    "object_type": "Team",
                    "object_id": "ghost-team",
                    "note": "must survive refusal",
                }
            },
            outcome="ok",
        )
    )
    before = store.audit_entries()

    with raises_code(ValidationFailed, "OBJECT_ERASURE_NOT_FOUND"):
        erase_object(store, "Team", "ghost-team", operator="privacy-ops")

    assert store.audit_entries() == before


def _referenced_names(path: Path) -> set[str]:
    """Every name a module defines, imports, calls or reads an attribute of.

    Deliberately coarse: erasure machinery is forbidden on these surfaces, so
    a bare mention is enough to fail -- no call-graph walk, no reachability
    argument, and no exemption for a mention that "is not really a call".

    What that does and does not buy, stated exactly, because the difference
    is the difference between a tripwire and a security boundary:

    - CAUGHT: any DIRECT reference to a machinery name -- import, call,
      attribute read, def/class of that name -- whatever the wrapping method
      is called. `def purge(self): return self._store.erase_object_content(...)`
      fails on the attribute, not on the method's name.
    - NOT CAUGHT: a name assembled at runtime. Both
      `getattr(store, "erase_object_content")` and
      `getattr(store, "erase_" + "object_content")` pass -- this walks name
      BINDINGS, and neither spelling binds one. Collecting `ast.Constant`
      would close the first and still not the second.

    That gap is left open on purpose. Chasing runtime-assembled names with a
    wider source scan has no endpoint -- the next spelling is always one
    concatenation away -- so this stays the cheap tripwire on the ordinary
    way the layering regresses (someone reaches for the verb directly), and
    the CONTAINMENT stays where it can be complete: the destructive verbs
    exist only on `Store` and the orchestrator, and `ActionContext` hands out
    no store at all (see the context-surface pins in `test_actions.py`).
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
            names.update((node.module or "").split("."))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
    return names


def test_erasure_is_absent_from_author_client_and_mcp_surfaces() -> None:
    assert "erase_object" not in ontary.__all__
    assert not hasattr(ontary, "erase_object")
    assert not any(name.startswith("erase") for name in vars(OntologyClient))
    assert not any(name.startswith("erase") for name in vars(ActionContext))

    source = Path(ontary.__file__).with_name("mcp_server.py").read_text()
    tree = ast.parse(source)
    tool_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "tool"
            for decorator in node.decorator_list
        )
    }
    assert "erase_object" not in tool_names
    assert not any(name.startswith("erase") for name in tool_names)

    # Every assertion above is a NAME test: it proves nothing is CALLED
    # erase*, and a working erasure surface named `purge_object` or `forget`
    # passes all of them. What is actually forbidden is the CAPABILITY, so
    # pin the machinery instead -- no consumer-facing module may so much as
    # mention the orchestrator or the store's destructive verbs, whatever the
    # method wrapping them is called. The names are derived, never hand-typed,
    # so renaming the machinery cannot quietly empty this set.
    machinery = set(erase_module.__all__) | {
        name for name in vars(Store) if "eras" in name
    }
    assert {"erase_object", "erase_object_content", "object_erasure_state"} <= machinery
    for module_name in ("client.py", "actions.py", "mcp_server.py"):
        referenced = _referenced_names(Path(ontary.__file__).with_name(module_name))
        assert machinery.isdisjoint(referenced), (
            f"{module_name} reaches erasure machinery: "
            f"{sorted(machinery & referenced)}"
        )
