"""Filter compiler contract and the filtered paging storage seam."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from ontary import Ontology, OntologyObject, prop
from ontary.errors import ValidationFailed
from ontary.query._where import _evaluate_where_clause
from ontary.store import ObjectStore, Source, _sql
from ontary.store._core import StoreCore
from ontary.store._filter import RowFilter, ScopeTerm, WhereTerm, compile_filter
from ontary.store.sqlite import _ontary_instant


def _selected(term: WhereTerm, payloads: list[dict[str, Any]]) -> list[int]:
    fragment, params = compile_filter(RowFilter(where=(term,)), "sqlite")
    with sqlite3.connect(":memory:") as conn:
        conn.create_function("ontary_instant", 1, _ontary_instant, deterministic=True)
        return [
            i for i, payload in enumerate(payloads)
            if conn.execute(
                _sql.render(f"SELECT ({fragment}) FROM (SELECT {{p}} AS payload)", "sqlite"),
                [*params, json.dumps(payload)],
            ).fetchone()[0]
        ]


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
def test_compiler_binds_field_names_and_operands(dialect: _sql.Dialect) -> None:
    attack = "x'); DROP TABLE objects; --"
    for op, operand in [("eq", attack), ("ne", attack), ("contains", attack),
                        ("in", [attack, None])]:
        fragment, params = compile_filter(
            RowFilter(where=(WhereTerm("private_field", "str", op, operand),)), dialect
        )
        assert attack not in fragment
        assert "private_field" not in fragment
        assert attack in params
        assert any("private_field" in str(p) for p in params)
        assert fragment.count("{p}") == len(params)
    assert compile_filter(RowFilter(), dialect) == ("TRUE", [])
    with pytest.raises(NotImplementedError):
        compile_filter(RowFilter(scope=ScopeTerm((), "s", False)), dialect)


@pytest.mark.parametrize(
    ("op", "operand", "expected"),
    [("eq", "a", [0, 4]), ("eq", None, [2, 3, 4]),
     ("ne", "a", [1, 2, 3, 4]), ("in", ["a", None], [0, 2, 3, 4]),
     ("in", [], []), ("contains", "a", [0, 4])],
)
def test_string_null_missing_and_wrong_types(op: str, operand: Any, expected: list[int]) -> None:
    assert _selected(WhereTerm("v", "str", op, operand),
                     [{"v": "a"}, {"v": "b"}, {"v": None}, {}, {"v": 7}]) == expected


@pytest.mark.parametrize(("op", "expected"),
                         [("gt", [2, 5]), ("gte", [1, 2, 5]),
                          ("lt", [0, 5]), ("lte", [0, 1, 5])])
def test_numeric_operators(op: str, expected: list[int]) -> None:
    assert _selected(WhereTerm("v", "int", op, 2),
                     [{"v": 1}, {"v": 2}, {"v": 3}, {}, {"v": None}, {"v": "bad"}]) == expected


def test_conjunction_contains_and_large_integers() -> None:
    term = WhereTerm("v", "int", "and", (WhereTerm("v", "int", "gte", 2),
                                           WhereTerm("v", "int", "lt", 4)))
    assert _selected(term, [{"v": 1}, {"v": 2}, {"v": 4}, {"v": 2**60}]) == [1, 3]
    assert _selected(WhereTerm("v", "str", "contains", "%_\\"),
                     [{"v": "%_\\"}, {"v": "ANY"}, {"v": "%_"}]) == [0]


@pytest.fixture(params=["sqlite", "postgres"])
def filter_store(request: pytest.FixtureRequest) -> Iterator[StoreCore]:
    ontology = Ontology("filter", scope_levels=["team"])

    @ontology.object(layer="L0")
    class Item(OntologyObject):
        id: str = prop(primary_key=True)
        status: str
        score: float | None = None

    if request.param == "sqlite":
        store = ObjectStore(ontology.registry)
        try:
            yield store
        finally:
            store._conn.close()
        return
    dsn = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("ONTARY_TEST_POSTGRES_DSN is not set")
    import psycopg

    from ontary.store.postgres import PostgresStore

    schema = f"ontary_test_filter_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(f'CREATE SCHEMA "{schema}"')
    pg_store = None
    try:
        separator = "&" if "?" in dsn else "?"
        pg_store = PostgresStore(
            ontology.registry, f"{dsn}{separator}options=-csearch_path%3D{schema}"
        )
        yield pg_store
    finally:
        if pg_store is not None:
            pg_store._conn.close()
        with psycopg.connect(dsn, autocommit=True) as setup:
            setup.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_filtered_page_and_validation(filter_store: StoreCore) -> None:
    for i, status in enumerate(["open", "closed", "open"]):
        filter_store.insert("Item", {"id": str(i), "status": status}, Source(source_system="test"))
    row_filter = RowFilter(where=(WhereTerm("status", "str", "eq", "open"),))
    rows = filter_store.read_page_filtered("Item", row_filter, batch=1)
    assert [r.obj.payload["id"] for r in rows] == ["0"]
    rows = filter_store.read_page_filtered("Item", row_filter, rows[0].key)
    assert [r.obj.payload["id"] for r in rows] == ["2"]
    for kwargs in [{"batch": 0}, {"after_key": "unknown"}]:
        with pytest.raises(ValidationFailed) as plain:
            filter_store.read_page("Item", **kwargs)
        with pytest.raises(ValidationFailed) as filtered:
            filter_store.read_page_filtered("Item", row_filter, **kwargs)
        assert filtered.value.code == plain.value.code


@pytest.mark.parametrize(
    ("term", "payloads", "expected"),
    [
        (WhereTerm("v", "bool", "eq", True),
         [{"v": True}, {"v": False}, {"v": 1}, {}], [0, 2]),
        (WhereTerm("v", "json", "eq", None),
         [{"v": []}, {"v": {}}, {"v": None}, {}], [2, 3]),
        (WhereTerm("v", "json", "eq", {"nested": [1]}),
         [{"v": 1}, {}, {"v": None}], [0, 1, 2]),
        (WhereTerm("v", "date", "gte", "2026-01-02"),
         [{"v": "2026-01-01"}, {"v": "2026-01-02"}, {"v": "bad"}, {}], [1, 2]),
        (WhereTerm("v", "datetime", "gte", "2026-01-02T00:00:00+00:00"),
         [{"v": "2026-01-02T09:00:00+09:00"}, {"v": "2026-01-01T23:00:00Z"},
          {"v": "2026-01-02T00:00:00"}, {"v": "bad"}, {}], [0, 3]),
        (WhereTerm("v", "datetime", "eq", "2026-01-02T00:00:00+00:00"),
         [{"v": "2026-01-02T09:00:00+09:00"},
          {"v": "2026-01-02T00:00:00+00:00"}], [1]),
    ],
)
def test_scalar_types_and_datetime_awareness(
    term: WhereTerm, payloads: list[dict[str, Any]], expected: list[int]
) -> None:
    assert _selected(term, payloads) == expected


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
@pytest.mark.parametrize("prop_type,operand", [("int", 123), ("float", 1.23),
                                              ("date", "2026-01-01"),
                                              ("datetime", "2026-01-01T00:00:00Z")])
@pytest.mark.parametrize("op", ["eq", "ne", "gt", "gte", "lt", "lte"])
def test_compiler_parameter_count_for_typed_operators(dialect, prop_type, operand, op) -> None:
    fragment, params = compile_filter(
        RowFilter(where=(WhereTerm("hidden_field", prop_type, op, operand),)), dialect
    )
    assert fragment.count("{p}") == len(params)
    assert "hidden_field" not in fragment
    assert str(operand) not in fragment


def test_instant_parser_is_permissive_and_normalizes_offsets() -> None:
    assert _ontary_instant("bad") is None
    assert _ontary_instant(None) is None
    assert _ontary_instant("2026-01-02T09:00:00+09:00") == _ontary_instant("2026-01-02T00:00:00Z")
    assert _ontary_instant("2026-01-02T00:00:00").startswith("N|")


@pytest.mark.parametrize(("op", "operand", "expected"),
                         [("eq", float("inf"), []), ("ne", float("inf"), [0, 1, 2]),
                          ("lt", float("inf"), [0]), ("eq", float("nan"), []),
                          ("ne", float("nan"), [0, 1, 2])])
def test_nonfinite_numeric_operands(op: str, operand: float, expected: list[int]) -> None:
    assert _selected(WhereTerm("v", "float", op, operand),
                     [{"v": 1.0}, {}, {"v": None}]) == expected


@pytest.mark.parametrize(
    ("term", "actual", "expected"),
    [(WhereTerm("v", "str", "eq", "x'); DROP TABLE objects; --"),
      "x'); DROP TABLE objects; --", True),
     (WhereTerm("v", "str", "contains", "%_\\"), "prefix%_\\suffix", True),
     (WhereTerm("v", "str", "contains", "abc"), "ABC", False),
     (WhereTerm("v", "int", "gte", 2), 2, True),
     (WhereTerm("v", "int", "gte", 2), "wrong", True),
     (WhereTerm("v", "float", "eq", 1.0000000000000002), 1.0000000000000002, True),
     (WhereTerm("v", "bool", "eq", True), False, False),
     (WhereTerm("v", "date", "lt", "2026-01-02"), "2026-01-01", True),
     (WhereTerm("v", "datetime", "gte", "2026-01-02T00:00:00Z"),
      "2026-01-02T09:00:00+09:00", True),
     (WhereTerm("v", "datetime", "gte", "2026-01-02T00:00:00Z"),
      "2026-01-02T00:00:00", False),
     (WhereTerm("v", "datetime", "gte", "2026-01-02T00:00:00"),
      "2026-99-02T00:00:00Z", True)],
)
def test_scalar_predicates_on_backend_connection(
    filter_store: StoreCore, term: WhereTerm, actual: Any, expected: bool
) -> None:
    dialect = "sqlite" if isinstance(filter_store, ObjectStore) else "postgres"
    fragment, params = compile_filter(RowFilter(where=(term,)), dialect)
    sql = _sql.render(f"SELECT ({fragment}) FROM (SELECT {{p}} AS payload) AS sample", dialect)
    rows = _sql.execute(filter_store._conn, sql, [*params, json.dumps({"v": actual})],
                        dialect=dialect).rows
    assert bool(rows[0][0]) is expected


def test_sqlite_large_integer_operand_preserves_possible_float_matches() -> None:
    assert _selected(WhereTerm("v", "float", "lt", 10**20 + 1),
                     [{"v": 1e20}, {"v": 1.0}, {"v": None}]) == [0, 1]


@pytest.mark.parametrize("actual,operand", [(1e30, 10**30), (1e23, 10**23),
                                          (-1e30, -(10**30)), (-1e23, -(10**23))])
@pytest.mark.parametrize("op", ["eq", "ne", "gt", "gte", "lt", "lte"])
def test_large_float_int_comparisons_are_permissive(
    filter_store: StoreCore, actual: float, operand: int, op: str
) -> None:
    payload = {"id": "large", "status": "open", "score": actual}
    filter_store.insert("Item", payload, Source(source_system="test"))
    term = WhereTerm("score", "float", op, operand)
    expected = _evaluate_where_clause(payload, (term.field, term.prop_type, op, operand))
    rows = filter_store.read_page_filtered("Item", RowFilter(where=(term,)))
    assert not expected or [r.obj.payload["id"] for r in rows] == ["large"]


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
@pytest.mark.parametrize("op", ["eq", "ne", "contains", "in", "gt", "gte", "lt", "lte"])
def test_compiler_nul_operands_pass_through(dialect: _sql.Dialect, op: str) -> None:
    operand = ["open", "open\x00", None] if op == "in" else "open\x00"
    term = WhereTerm("status", "str", op, operand)
    assert compile_filter(RowFilter(where=(term,)), dialect) == ("TRUE", [])
    nested = WhereTerm("status", "str", "and", (term,))
    assert compile_filter(RowFilter(where=(nested,)), dialect) == ("(TRUE)", [])


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
def test_compiler_nul_field_passes_through(dialect: _sql.Dialect) -> None:
    term = WhereTerm("status\x00", "str", "eq", "open")
    assert compile_filter(RowFilter(where=(term,)), dialect) == ("TRUE", [])


@pytest.mark.parametrize("op", ["eq", "ne", "contains", "in", "gt", "gte", "lt", "lte"])
def test_filtered_page_with_nul_operand_is_permissive(filter_store: StoreCore, op: str) -> None:
    payloads = [{"id": str(i), "status": status}
                for i, status in enumerate(["open\x00", "prefixopen\x00suffix", "open", "z"])]
    for payload in payloads:
        filter_store.insert("Item", payload, Source(source_system="test"))
    operand = ["open", "open\x00"] if op == "in" else "open\x00"
    term = WhereTerm("status", "str", op, operand)
    expected = {payload["id"] for payload in payloads
                if _evaluate_where_clause(payload, (term.field, term.prop_type, op, operand))}
    rows = filter_store.read_page_filtered("Item", RowFilter(where=(term,)))
    ids = [r.obj.payload["id"] for r in rows]
    assert expected <= set(ids)
    assert ids == [payload["id"] for payload in payloads]


@pytest.mark.parametrize("op", ["eq", "ne", "contains"])
def test_filtered_page_with_nul_payload_is_permissive(filter_store: StoreCore, op: str) -> None:
    filter_store.insert("Item", {"id": "nul", "status": "open\u0000"},
                        Source(source_system="test"))
    filter_store.insert("Item", {"id": "plain", "status": "open"},
                        Source(source_system="test"))
    rows = filter_store.read_page_filtered(
        "Item", RowFilter(where=(WhereTerm("status", "str", op, "open"),))
    )
    ids = [r.obj.payload["id"] for r in rows]
    if op == "ne":
        assert ids == ["nul"]
    elif op == "contains" or not isinstance(filter_store, ObjectStore):
        assert ids == ["nul", "plain"]
    else:
        assert ids == ["plain"]


@pytest.mark.parametrize("actual", [float("nan"), float("inf"), -float("inf")],
                         ids=["nan", "infinity", "negative_infinity"])
@pytest.mark.parametrize(
    "term",
    [WhereTerm("score", prop_type, "ne", None)
     for prop_type in ("str", "int", "float", "bool", "date", "datetime", "json")]
    + [WhereTerm("score", "float", "gt", 0.0),
       WhereTerm("status", "str", "ne", None),
       WhereTerm("status", "str", "eq", None)],
)
def test_stored_nonfinite_payloads_are_permissive(
    filter_store: StoreCore, actual: float, term: WhereTerm
) -> None:
    payloads = [{"id": "1", "status": "open", "score": actual},
                {"id": "normal", "status": "open", "score": 1.0}]
    for payload in payloads:
        filter_store.insert("Item", payload, Source(source_system="test"))
    clause = (term.field, term.prop_type, term.op, term.operand)
    expected = {payload["id"] for payload in payloads
                if _evaluate_where_clause(payload, clause)}
    rows = filter_store.read_page_filtered("Item", RowFilter(where=(term,)))
    ids = {row.obj.payload["id"] for row in rows}
    assert expected <= ids
    # json.dumps stores these values outside strict JSON. Every term must
    # pass that entire row through, even a probe of a different field.
    assert "1" in ids
    assert ("normal" in ids) == ("normal" in expected)
