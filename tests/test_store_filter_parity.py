"""Differential prefilter parity against real, paged SQLite/Postgres rows."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from _filter_cases import FAMILIES, FILTER_CASES, INJECTION, MISSING, SURROGATES, FilterCase

from ontary.meta import ObjectTypeDef, OntologyRegistry, PropertyDef
from ontary.query._where import _evaluate_where_clause, _WhereClause
from ontary.scope import DirectProperty, ScopePolicy, SelfScope, resolve_owning_scope
from ontary.security import Consumer, covers_scope
from ontary.store import ObjectStore, Source
from ontary.store._core import StoreCore
from ontary.store._filter import RowFilter, ScopeTerm, WhereTerm


@dataclass
class PopulatedStore:
    backend: str
    store: StoreCore
    payloads: dict[str, dict[str, dict[str, Any]]]


def _registry() -> OntologyRegistry:
    registry = OntologyRegistry()
    for family, (prop_type, _, _) in FAMILIES.items():
        registry.register_object_type(ObjectTypeDef(
            api_name=family, display_name=family, description="Parity grid", layer="L0",
            primary_key="id", properties=[PropertyDef(name="id", type="str"),
                PropertyDef(name="value", type=prop_type, required=False,
                            choices=("open", "closed") if family == "choices" else None)],
        ))
    for name in ("ScopeRows", "InjectionRows"):
        registry.register_object_type(ObjectTypeDef(
            api_name=name, display_name=name, description="Parity grid", layer="L0",
            primary_key="id", properties=[PropertyDef(name="id", type="str"),
                PropertyDef(name="value", type="str", required=False)],
        ))
    registry.validate()
    return registry


@pytest.fixture(scope="module", params=["sqlite", "postgres"])
def populated_store(request: pytest.FixtureRequest) -> Iterator[PopulatedStore]:
    registry = _registry()
    schema = None
    dsn = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
    if request.param == "sqlite":
        store = ObjectStore(registry)
    else:
        if not dsn:
            pytest.skip("ONTARY_TEST_POSTGRES_DSN is not set")
        import psycopg

        from ontary.store.postgres import PostgresStore

        schema = "ontary_test_filter_" + uuid.uuid4().hex
        with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
            conn.execute(f'CREATE SCHEMA "{schema}"')
        separator = "&" if "?" in dsn else "?"
        store = PostgresStore(registry, f"{dsn}{separator}options=-csearch_path%3D{schema}")
    payloads = {}
    try:
        for family, (_, values, _) in FAMILIES.items():
            payloads[family] = {
                v.label: {"id": v.label, **({} if v.value is MISSING else {"value": v.value})}
                for v in values
            }
        payloads["ScopeRows"] = {
            "owned": {"id": "owned", "value": "owned"},
            "other": {"id": "other", "value": "owned"},
            "conflict": {"id": "conflict", "value": "other"},
            "null": {"id": "null", "value": None},
            "missing": {"id": "missing"},
            "numeric": {"id": "numeric", "value": 7},
            "boolean": {"id": "boolean", "value": True},
        }
        payloads["InjectionRows"] = {
            str(i): {"id": str(i), "value": value}
            for i, value in enumerate([INJECTION, "open", "x", INJECTION,
                                       "before " + INJECTION, "", None])
        }
        # Trusted raw-payload storage seam: exercise the real TEXT column and
        # decoder without action/declared-shape validation or normalization.
        with store.transaction():
            for family, rows in payloads.items():
                for obj_id, payload in rows.items():
                    encoded = json.dumps(payload)
                    store._insert_object_row(family, obj_id, encoded,
                                             "2026-02-15T00:00:00+00:00",
                                             Source(source_system="parity"))
                    rows[obj_id] = json.loads(encoded)
        yield PopulatedStore(request.param, store, payloads)
    finally:
        store._conn.close()
        if schema is not None:
            with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
                conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _walk(store: StoreCore, family: str, row_filter: RowFilter) -> set[str]:
    ids = []
    after = None
    cursors = set()
    while True:
        page = store.read_page_filtered(family, row_filter, after_key=after, batch=3)
        assert len(page) <= 3
        if not page:
            break
        ids.extend(row.obj.lineage.object_id for row in page)
        after = page[-1].key
        assert after not in cursors, "paging cursor did not advance"
        cursors.add(after)
    assert len(ids) == len(set(ids)), "paged walk repeated a row"
    return set(ids)


def _clause(term: WhereTerm) -> _WhereClause:
    operand = tuple(_clause(sub) for sub in term.operand) if term.op == "and" else term.operand
    return term.field, term.prop_type, term.op, operand


@pytest.mark.parametrize("case", FILTER_CASES, ids=lambda case: case.name)
def test_where_parity(populated_store: PopulatedStore, case: FilterCase) -> None:
    rows = populated_store.payloads[case.family]
    selected = _walk(populated_store.store, case.family, RowFilter(where=(case.term,)))
    expected = {obj_id for obj_id, payload in rows.items()
                if _evaluate_where_clause(payload, _clause(case.term))}
    detail = (f"backend={populated_store.backend}, type={case.term.prop_type}, "
              f"op={case.term.op}, operand={case.term.operand!r}")
    missing = expected - selected
    assert not missing, f"SQL dropped Python matches: {detail}, rows={[(i, rows[i]) for i in missing]!r}"
    well_formed = {value.label for value in case.values if value.well_formed}
    different = (selected ^ expected) & well_formed
    assert not different, f"Exact parity failed: {detail}, rows={[(i, rows[i]) for i in different]!r}"


@pytest.mark.parametrize("rules", [(("prop", "value"),), (("self", None),),
    (("prop", "value"), ("self", None)), (("self", None), ("prop", "value"))])
@pytest.mark.parametrize("scope_id", ["owned", "conflict", "null", "missing", "7", "True"])
def test_scope_parity(populated_store: PopulatedStore, rules, scope_id: str) -> None:
    policy = ScopePolicy(levels=["team"], rules={"ScopeRows": [
        SelfScope(level="team") if kind == "self" else
        DirectProperty(level="team", property_name=field) for kind, field in rules
    ]})
    consumer = Consumer(actor_id="reader", role="Reader", scope_level="team",
                        scope_id=scope_id, kind="human")
    rows = populated_store.payloads["ScopeRows"]
    expected = {obj_id for obj_id in rows if covers_scope(policy, consumer,
        resolve_owning_scope(policy, populated_store.store, "ScopeRows", obj_id))}
    selected = _walk(populated_store.store, "ScopeRows",
                     RowFilter(scope=ScopeTerm(rules, scope_id, False)))
    assert expected <= selected, (populated_store.backend, rules, scope_id, expected - selected)
    exact = {obj_id for obj_id, payload in rows.items()
             if payload.get("value") is None or isinstance(payload["value"], str)}
    assert selected & exact == expected & exact


def test_scope_consumer_level_has_no_rule(populated_store: PopulatedStore) -> None:
    policy = ScopePolicy(levels=["unknown", "team"], rules={
        "ScopeRows": [DirectProperty(level="team", property_name="value")],
    })
    consumer = Consumer(actor_id="reader", role="Reader", scope_level="unknown",
                        scope_id="owned", kind="human")
    expected = {obj_id for obj_id in populated_store.payloads["ScopeRows"]
                if covers_scope(policy, consumer, resolve_owning_scope(
                    policy, populated_store.store, "ScopeRows", obj_id))}
    assert expected == set()
    assert _walk(populated_store.store, "ScopeRows",
                 RowFilter(scope=ScopeTerm((), "owned", False))) == expected


def test_where_operand_injection(populated_store: PopulatedStore) -> None:
    store = populated_store.store
    before = store._conn.execute("SELECT count(*) FROM objects").fetchone()[0]
    expected = {obj_id for obj_id, payload in populated_store.payloads["InjectionRows"].items()
                if payload["value"] == INJECTION}
    assert _walk(store, "InjectionRows", RowFilter(where=(
        WhereTerm("value", "str", "eq", INJECTION),))) == expected
    assert store._conn.execute("SELECT count(*) FROM objects").fetchone()[0] == before


@pytest.mark.parametrize("case", [case for case in FILTER_CASES if "surrogate" in case.name],
                         ids=lambda case: case.name)
def test_surrogate_where_python_result_parity(populated_store: PopulatedStore, case: FilterCase) -> None:
    rows = populated_store.payloads[case.family]
    selected = _walk(populated_store.store, case.family, RowFilter(where=(case.term,)))
    expected = {obj_id for obj_id, payload in rows.items()
                if _evaluate_where_clause(payload, _clause(case.term))}
    assert expected <= selected
    actual = {obj_id for obj_id in selected
              if _evaluate_where_clause(rows[obj_id], _clause(case.term))}
    assert actual == expected


@pytest.mark.parametrize("scope_id", SURROGATES)
@pytest.mark.parametrize("rules", [(("self", None),), (("prop", "value"),),
    (("prop", "value"), ("self", None)), (("self", None), ("prop", "value"))])
def test_surrogate_scope_python_result_parity(populated_store: PopulatedStore, rules, scope_id: str) -> None:
    policy = ScopePolicy(levels=["team"], rules={"ScopeRows": [
        SelfScope(level="team") if kind == "self" else
        DirectProperty(level="team", property_name=field) for kind, field in rules
    ]})
    consumer = Consumer(actor_id="reader", role="Reader", scope_level="team",
                        scope_id=scope_id, kind="human")
    def visible(obj_id: str) -> bool:
        return covers_scope(policy, consumer, resolve_owning_scope(
            policy, populated_store.store, "ScopeRows", obj_id))
    expected = {obj_id for obj_id in populated_store.payloads["ScopeRows"] if visible(obj_id)}
    selected = _walk(populated_store.store, "ScopeRows",
                     RowFilter(scope=ScopeTerm(rules, scope_id, False)))
    assert expected <= selected
    assert {obj_id for obj_id in selected if visible(obj_id)} == expected == set()
