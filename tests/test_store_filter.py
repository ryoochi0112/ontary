"""Filter compiler contract and the filtered paging storage seam."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from importlib import import_module
from typing import Any

import pytest
from _fetch_counter import count_fallbacks

from ontary import Ontology, OntologyObject, prop
from ontary.errors import ValidationFailed
from ontary.query._where import _evaluate_where_clause
from ontary.store import ObjectStore, Source, _sql
from ontary.store._core import StoreCore
from ontary.store._filter import (
    SQLITE_DOMAIN,
    BindDomain,
    RowFilter,
    ScopeTerm,
    WhereTerm,
    compile_filter,
    compile_row_filter,
    filter_is_selective,
    pushable,
)
from ontary.store.sqlite import _ontary_instant


def _selected(term: WhereTerm, payloads: list[dict[str, Any]]) -> list[int]:
    fragment, params = compile_filter(RowFilter(where=(term,)), "sqlite", SQLITE_DOMAIN)
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
            RowFilter(where=(WhereTerm("private_field", "str", op, operand),)), dialect, SQLITE_DOMAIN
        )
        assert attack not in fragment
        assert "private_field" not in fragment
        bound_operands = (json.loads(params[0]) if dialect == "sqlite" else params[0]) \
            if op == "in" else params
        assert attack in bound_operands
        assert any("private_field" in str(p) for p in params)
        assert fragment.count("{p}") == len(params)
    assert compile_filter(RowFilter(), dialect, SQLITE_DOMAIN) == ("TRUE", [])
    assert compile_filter(RowFilter(scope=ScopeTerm((), "s", False)), dialect, SQLITE_DOMAIN) == ("FALSE", [])


@pytest.mark.parametrize(
    "scope,selective",
    [(None, True), (ScopeTerm((), "owned", False), True),
     (ScopeTerm((), "owned", True), False),
     (ScopeTerm((("self", None),), "owned", True), True),
     (ScopeTerm((("prop", "team_id"),), "owned", True), True),
     (ScopeTerm((("self", None),), "nul\x00", False), False),
     (ScopeTerm((("prop", "team_id"),), "\ud800", False), False),
     (ScopeTerm((("prop", "\ud800"),), "owned", False), False)],
)
def test_filter_selectivity_scope(scope: ScopeTerm | None, selective: bool) -> None:
    assert filter_is_selective(RowFilter(scope=scope), SQLITE_DOMAIN) is selective
    # A selective where cannot make an unconditional scope component exact.
    assert filter_is_selective(RowFilter(
        where=(WhereTerm("status", "str", "eq", "open"),), scope=scope,
    ), SQLITE_DOMAIN) is selective


@pytest.mark.parametrize(
    "term,selective",
    [(WhereTerm("v", "str", "eq", "open"), True),
     (WhereTerm("v", "json", "eq", None), True),
     (WhereTerm("v", "json", "eq", 1), True),
     (WhereTerm("v", "str", "in", []), True),
     (WhereTerm("v", "str", "in", ["open", None]), True),
     (WhereTerm("v", "json", "eq", {}), False),
     (WhereTerm("v", "json", "eq", []), False),
     (WhereTerm("v", "struct", "eq", None), False),
     (WhereTerm("v", "json", "in", ["open", {}]), False),
     (WhereTerm("v", "str", "eq", "nul\x00"), False),
     (WhereTerm("v", "str", "eq", "\ud800"), False),
     (WhereTerm("\udfff", "str", "eq", "open"), False),
     (WhereTerm("v", "str", "in", ["open", "\ud800"]), False),
     (WhereTerm("v", "str", "and", ()), False),
     (WhereTerm("v", "str", "and", (
         WhereTerm("v", "str", "and", (WhereTerm("v", "str", "eq", "\ud800"),)),
     )), False),
     (WhereTerm("v", "str", "and", (
         WhereTerm("v", "str", "eq", "\ud800"),
         WhereTerm("v", "str", "eq", "open"),
     )), False)],
)
def test_filter_selectivity_where(term: WhereTerm, selective: bool) -> None:
    assert filter_is_selective(RowFilter(where=(term,)), SQLITE_DOMAIN) is selective
    # Any unconditional top-level term prevents small batches, even alongside
    # a selective where term and scope term.
    assert filter_is_selective(RowFilter(
        where=(WhereTerm("status", "str", "eq", "open"), term),
        scope=ScopeTerm((("self", None),), "owned", False),
    ), SQLITE_DOMAIN) is selective


@pytest.fixture(params=["sqlite", "postgres"], scope="module")
def scope_connection(request: pytest.FixtureRequest) -> Iterator[tuple[_sql.Dialect, Any]]:
    if request.param == "sqlite":
        conn = sqlite3.connect(":memory:")
        try:
            yield "sqlite", conn
        finally:
            conn.close()
        return
    dsn = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("ONTARY_TEST_POSTGRES_DSN is not set")
    import psycopg

    # Keep payloads in a real TEXT column so PostgreSQL cannot constant-fold
    # guarded JSON casts. The connection's temporary table never touches public.
    with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
        conn.execute("CREATE TEMP TABLE ontary_test_scope (row_index INTEGER, id TEXT, payload TEXT)")
        yield "postgres", conn


def _scope_selected(
    connection: tuple[_sql.Dialect, Any], row_filter: RowFilter,
    rows: list[tuple[str, dict[str, Any] | str]],
) -> list[int]:
    dialect, conn = connection
    fragment, params = compile_filter(row_filter, dialect, SQLITE_DOMAIN)
    assert fragment.count("{p}") == len(params)
    if dialect == "postgres":
        conn.execute("TRUNCATE pg_temp.ontary_test_scope")
        for i, (obj_id, payload) in enumerate(rows):
            raw = payload if isinstance(payload, str) else json.dumps(payload)
            conn.execute("INSERT INTO pg_temp.ontary_test_scope VALUES (%s, %s, %s)",
                         [i, obj_id, raw])
        sql = _sql.render(
            f"SELECT row_index FROM pg_temp.ontary_test_scope WHERE ({fragment}) "
            "ORDER BY row_index", dialect
        )
        result = _sql.execute(conn, sql, params, dialect=dialect).rows
        return [row[0] for row in result]
    sql = _sql.render(
        f"SELECT ({fragment}) FROM (SELECT CAST({{p}} AS TEXT) AS id, "
        "CAST({p} AS TEXT) AS payload) AS sample", dialect
    )
    selected = []
    for i, (obj_id, payload) in enumerate(rows):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        result = _sql.execute(conn, sql, [*params, obj_id, raw], dialect=dialect).rows
        if result[0][0]:
            selected.append(i)
    return selected


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
def test_scope_compiler_binds_property_names_and_scope_id(dialect: _sql.Dialect) -> None:
    scope_id = "x'); DROP TABLE objects; --"
    field = "private_scope_field"
    for rules in [(("self", None),), (("prop", field),),
                  (("prop", field), ("self", None)),
                  (("self", None), ("prop", field))]:
        fragment, params = compile_filter(RowFilter(scope=ScopeTerm(rules, scope_id, False)),
                                          dialect, SQLITE_DOMAIN)
        assert scope_id not in fragment
        assert field not in fragment
        assert scope_id in params
        if any(kind == "prop" for kind, _ in rules):
            assert any(field in str(p) for p in params)
        assert fragment.count("{p}") == len(params)


@pytest.mark.parametrize(
    ("rules", "expected"),
    [((("prop", "team_id"), ("self", None)), [1, 2, 3]),
     ((("self", None), ("prop", "team_id")), [0, 2, 3])],
)
def test_scope_rule_order(scope_connection, rules, expected) -> None:
    rows = [("team", {"team_id": "other"}), ("other", {"team_id": "team"}),
            ("team", {"team_id": None}), ("team", {}), ("other", {})]
    scope = ScopeTerm(rules, "team", False)
    assert _scope_selected(scope_connection, RowFilter(scope=scope), rows) == expected


@pytest.mark.parametrize("climb_possible", [False, True])
def test_scope_null_and_missing_fall_through(scope_connection, climb_possible: bool) -> None:
    rows = [("other", {"team_id": None, "backup_id": "team"}),
            ("other", {"backup_id": "team"}),
            ("other", {"team_id": "other", "backup_id": "team"}),
            ("other", {"team_id": "", "backup_id": "team"}),
            ("other", {"team_id": None, "backup_id": None}), ("other", {})]
    scope = ScopeTerm((("prop", "team_id"), ("prop", "backup_id")), "team", climb_possible)
    expected = [0, 1, 4, 5] if climb_possible else [0, 1]
    assert _scope_selected(scope_connection, RowFilter(scope=scope), rows) == expected


@pytest.mark.parametrize("climb_possible", [False, True])
def test_scope_resolved_mismatch_does_not_climb(scope_connection, climb_possible: bool) -> None:
    scope = ScopeTerm((("prop", "team_id"),), "team", climb_possible)
    rows = [("team", {"team_id": "other"}), ("other", {"team_id": "team"})]
    assert _scope_selected(scope_connection, RowFilter(scope=scope), rows) == [1]
    scope = ScopeTerm((("self", None),), "team", climb_possible)
    assert _scope_selected(scope_connection, RowFilter(scope=scope), rows) == [0]


@pytest.mark.parametrize("actual", [0, 7, 2**53 + 1, 10**100, 1.5, True, False, [], {}])
def test_scope_nonstring_properties_are_permissive(scope_connection, actual: Any) -> None:
    scope = ScopeTerm((("prop", "team_id"), ("self", None)), "unmatched", False)
    assert _scope_selected(scope_connection, RowFilter(scope=scope),
                           [("other", {"team_id": actual})]) == [0]


@pytest.mark.parametrize("scope_id", ["team", "", "é", "x'); DROP TABLE objects; --"])
def test_scope_strings_compare_exactly(scope_connection, scope_id: str) -> None:
    values = [scope_id, scope_id.upper(), scope_id + " ", scope_id + "x", "e\u0301"]
    rows = [(value, {"team_id": value}) for value in values]
    expected = [i for i, value in enumerate(values) if value == scope_id]
    for rules in [(("self", None),), (("prop", "team_id"),)]:
        scope = ScopeTerm(rules, scope_id, False)
        assert _scope_selected(scope_connection, RowFilter(scope=scope), rows) == expected


@pytest.mark.parametrize("rules", [(("self", None),), (("prop", "team_id"),),
                                  (("prop", "team_id"), ("self", None))])
@pytest.mark.parametrize("payload", ["not json", "{", '{"team_id": "other", "score": NaN}',
                                    '{"team_id": "other", "score": Infinity}',
                                    '{"team_id": "other", "score": -Infinity}'])
def test_scope_invalid_json_is_permissive(scope_connection, rules, payload: str) -> None:
    scope = ScopeTerm(rules, "team", False)
    assert _scope_selected(scope_connection, RowFilter(scope=scope), [("other", payload)]) == [0]


def test_scope_nul_property_value_is_permissive(scope_connection) -> None:
    scope = ScopeTerm((("prop", "team_id"), ("self", None)), "team", False)
    rows = [("other", {"team_id": "other\x00suffix"})]
    assert _scope_selected(scope_connection, RowFilter(scope=scope), rows) == [0]


def test_scope_postgres_rejected_json_is_permissive(scope_connection) -> None:
    if scope_connection[0] != "postgres":
        return
    for rules in [(("self", None),), (("prop", "team_id"),)]:
        scope = ScopeTerm(rules, "team", False)
        rows = [("other", {"team_id": "other", "unrelated": "nul\x00"}),
                ("other", '{"team_id": "other", "unrelated": 1e1000000}'),
                ("other", '{"team_id": "other", "unrelated": "\\ud800"}')]
        assert _scope_selected(scope_connection, RowFilter(scope=scope), rows) == [0, 1, 2]


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
@pytest.mark.parametrize(
    "scope",
    [ScopeTerm((("self", None),), "team\x00", False),
     ScopeTerm((("prop", "team_id"),), "team\x00", False),
     ScopeTerm((("prop", "team\x00id"), ("self", None)), "team", False)],
)
def test_scope_nul_parameters_are_not_bound(dialect: _sql.Dialect, scope: ScopeTerm) -> None:
    assert compile_filter(RowFilter(scope=scope), dialect, SQLITE_DOMAIN) == ("TRUE", [])


@pytest.mark.parametrize("climb_possible", [False, True])
def test_scope_empty_rules(scope_connection, climb_possible: bool) -> None:
    scope = ScopeTerm((), "team", climb_possible)
    assert compile_filter(RowFilter(scope=scope), scope_connection[0], SQLITE_DOMAIN) == (
        "TRUE" if climb_possible else "FALSE", []
    )
    rows = [("team", {}), ("other", {"team_id": "team"})]
    assert _scope_selected(scope_connection, RowFilter(scope=scope), rows) == (
        [0, 1] if climb_possible else []
    )


def test_scope_and_where_are_combined(scope_connection) -> None:
    scope = ScopeTerm((("prop", "team_id"), ("self", None)), "team", False)
    row_filter = RowFilter(where=(WhereTerm("status", "str", "eq", "open"),
                                 WhereTerm("score", "int", "gte", 2)), scope=scope)
    rows = [("a", {"status": "open", "score": 2, "team_id": "team"}),
            ("b", {"status": "closed", "score": 2, "team_id": "team"}),
            ("c", {"status": "open", "score": 2, "team_id": "other"}),
            ("team", {"status": "open", "score": 2}),
            ("d", {"status": "open", "score": 1, "team_id": "team"}),
            ("e", {"status": "open", "score": float("nan"), "team_id": "other"})]
    assert _scope_selected(scope_connection, row_filter, rows) == [0, 3, 5]


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
        RowFilter(where=(WhereTerm("hidden_field", prop_type, op, operand),)), dialect, SQLITE_DOMAIN
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
    selected = _selected(WhereTerm("v", "float", op, operand),
                         [{"v": 1.0}, {}, {"v": None}])
    if pushable(operand, SQLITE_DOMAIN):
        assert selected == expected
    else:
        assert set(expected) <= set(selected)


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
    fragment, params = compile_filter(RowFilter(where=(term,)), dialect, SQLITE_DOMAIN)
    sql = _sql.render(f"SELECT ({fragment}) FROM (SELECT {{p}} AS payload) AS sample", dialect)
    rows = _sql.execute(filter_store._conn, sql, [*params, json.dumps({"v": actual})],
                        dialect=dialect).rows
    assert bool(rows[0][0]) is expected


def test_sqlite_large_integer_operand_preserves_possible_float_matches() -> None:
    assert {0, 1} <= set(_selected(WhereTerm("v", "float", "lt", 10**20 + 1),
                                  [{"v": 1e20}, {"v": 1.0}, {"v": None}]))


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
    assert compile_filter(RowFilter(where=(term,)), dialect, SQLITE_DOMAIN) == ("TRUE", [])
    nested = WhereTerm("status", "str", "and", (term,))
    assert compile_filter(RowFilter(where=(nested,)), dialect, SQLITE_DOMAIN) == ("(TRUE)", [])


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
def test_compiler_nul_field_passes_through(dialect: _sql.Dialect) -> None:
    term = WhereTerm("status\x00", "str", "eq", "open")
    assert compile_filter(RowFilter(where=(term,)), dialect, SQLITE_DOMAIN) == ("TRUE", [])


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


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
@pytest.mark.parametrize("value", ["\ud800", "\udfff", "prefix\ud800suffix", "nul\x00tail"])
@pytest.mark.parametrize("op", ["eq", "ne", "contains", "gt", "gte", "lt", "lte"])
def test_unbindable_where_strings_are_not_bound(dialect: _sql.Dialect, value: str, op: str) -> None:
    for term in (WhereTerm(value, "str", op, "open"),
                 WhereTerm("status", "str", op, value)):
        assert compile_filter(RowFilter(where=(term,)), dialect, SQLITE_DOMAIN) == ("TRUE", [])


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
@pytest.mark.parametrize("value", ["\ud800", "\udfff", "nul\x00tail"])
@pytest.mark.parametrize("items", [[None, "open"], ["open", None]])
def test_unbindable_in_item_is_not_bound(dialect: _sql.Dialect, value: str, items) -> None:
    for operands in ([value, *items], [*items, value]):
        term = WhereTerm("status", "str", "in", operands)
        assert compile_filter(RowFilter(where=(term,)), dialect, SQLITE_DOMAIN) == ("TRUE", [])


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
@pytest.mark.parametrize("value", ["\ud800", "\udfff", "nul\x00tail"])
def test_unbindable_nested_and_strings_are_not_bound(dialect: _sql.Dialect, value: str) -> None:
    unsafe = WhereTerm("status", "str", "eq", value)
    nested = WhereTerm("status", "str", "and", (
        WhereTerm("status", "str", "and", (unsafe,)),))
    assert compile_filter(RowFilter(where=(nested,)), dialect, SQLITE_DOMAIN) == ("((TRUE))", [])
    safe = WhereTerm("status", "str", "eq", "open")
    fragment, params = compile_filter(RowFilter(where=(
        WhereTerm("status", "str", "and", (nested, safe)),)), dialect, SQLITE_DOMAIN)
    safe_fragment, safe_params = compile_filter(RowFilter(where=(safe,)), dialect, SQLITE_DOMAIN)
    assert fragment == f"(((TRUE)) AND {safe_fragment})"
    assert params == safe_params


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
@pytest.mark.parametrize("value", ["\ud800", "\udfff", "prefix\ud800suffix", "nul\x00tail"])
def test_unbindable_scope_strings_are_not_bound(dialect: _sql.Dialect, value: str) -> None:
    for scope in (ScopeTerm((("self", None),), value, False),
                  ScopeTerm((("prop", "team_id"),), value, False),
                  ScopeTerm((("prop", value), ("self", None)), "owned", False),
                  ScopeTerm((("self", None), ("prop", value)), "owned", False)):
        assert compile_filter(RowFilter(scope=scope), dialect, SQLITE_DOMAIN) == ("TRUE", [])


def test_json_loaded_surrogate_operand_is_not_bound() -> None:
    value = json.loads('"\\ud800"')
    assert value == "\ud800"
    for dialect in ("sqlite", "postgres"):
        assert compile_filter(RowFilter(where=(WhereTerm("status", "str", "eq", value),)),
                              dialect, SQLITE_DOMAIN) == ("TRUE", [])


@pytest.mark.parametrize("op", ["eq", "ne", "contains", "in", "gt", "gte", "lt", "lte"])
def test_connection_encoding_where_bind_safety(op: str) -> None:
    operand = ["open", "開", None] if op == "in" else "開"
    term = WhereTerm("status", "str", op, operand)
    row_filter = RowFilter(where=(term,))
    assert compile_filter(row_filter, "postgres", domain=BindDomain(("iso8859-1",))) == ("TRUE", [])
    assert not filter_is_selective(row_filter, domain=BindDomain(("iso8859-1",)))
    assert filter_is_selective(row_filter, SQLITE_DOMAIN)
    assert compile_filter(row_filter, "postgres", SQLITE_DOMAIN) == compile_filter(
        row_filter, "postgres", domain=BindDomain(("utf-8",)))
    assert compile_filter(row_filter, "postgres", SQLITE_DOMAIN)[1]


@pytest.mark.parametrize("value", ["日本", "nul\x00", "\ud800"])
def test_connection_encoding_fields_and_scope_bind_safety(value: str) -> None:
    filters = [RowFilter(where=(WhereTerm(value, "str", "eq", "open"),)),
               RowFilter(scope=ScopeTerm((("self", None),), value, False)),
               RowFilter(scope=ScopeTerm((("prop", "team_id"),), value, False)),
               RowFilter(scope=ScopeTerm((("prop", value),), "owned", False))]
    for row_filter in filters:
        assert compile_filter(row_filter, "postgres", domain=BindDomain(("iso8859-1",))) == ("TRUE", [])
        assert not filter_is_selective(row_filter, domain=BindDomain(("iso8859-1",)))


def test_connection_encoding_nested_and_bind_safety() -> None:
    unsafe = WhereTerm("status", "str", "eq", "開")
    safe = WhereTerm("status", "str", "eq", "café")
    nested = WhereTerm("status", "str", "and", (
        WhereTerm("status", "str", "and", (unsafe,)), safe))
    row_filter = RowFilter(where=(nested,))
    fragment, params = compile_filter(row_filter, "postgres", domain=BindDomain(("iso8859-1",)))
    safe_fragment, safe_params = compile_filter(RowFilter(where=(safe,)), "postgres", SQLITE_DOMAIN)
    assert fragment == f"((TRUE) AND {safe_fragment})"
    assert params == safe_params
    assert not filter_is_selective(row_filter, domain=BindDomain(("iso8859-1",)))


@pytest.mark.parametrize("row_filter", [
    RowFilter(where=(WhereTerm("état", "str", "eq", "café"),)),
    RowFilter(where=(WhereTerm("status", "str", "in", ["café", None]),)),
    RowFilter(scope=ScopeTerm((("self", None),), "équipe", False)),
    RowFilter(scope=ScopeTerm((("prop", "équipe"),), "café", False)),
])
def test_connection_encoding_bindable_sql_is_unchanged(row_filter: RowFilter) -> None:
    expected = compile_filter(row_filter, "postgres", SQLITE_DOMAIN)
    assert expected[1]
    assert compile_filter(row_filter, "postgres", domain=BindDomain(("iso8859-1",))) == expected
    assert filter_is_selective(row_filter, domain=BindDomain(("iso8859-1",)))


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
@pytest.mark.parametrize("op", ["eq", "ne", "gt", "gte", "lt", "lte"])
def test_nan_operand_is_outside_the_bind_domain(dialect: _sql.Dialect, op: str) -> None:
    row_filter = RowFilter(where=(WhereTerm("v", "float", op, float("nan")),))
    assert compile_filter(row_filter, dialect, SQLITE_DOMAIN) == ("TRUE", [])
    assert not filter_is_selective(row_filter, SQLITE_DOMAIN)


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
@pytest.mark.parametrize("kind", ["int", "float", "json"])
@pytest.mark.parametrize("op", ["eq", "ne", "gt", "gte", "lt", "lte", "in"])
@pytest.mark.parametrize("operand", [2**53, -(2**53), 2**53 + 1, -(2**53) - 1, 1e100])
def test_compiled_numeric_operand_exactness(dialect, kind, op, operand) -> None:
    term = WhereTerm("v", kind, op, [None, 1, operand] if op == "in" else operand)
    row_filter = RowFilter(where=(term,))
    compiled = compile_row_filter(row_filter, dialect, SQLITE_DOMAIN)
    assert compiled.exact is (dialect == "sqlite" or abs(operand) <= 2**53)
    assert (compiled.sql, compiled.params) == compile_filter(row_filter, dialect, SQLITE_DOMAIN)
    assert filter_is_selective(row_filter, SQLITE_DOMAIN, dialect) is compiled.exact


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
@pytest.mark.parametrize("op", ["gt", "gte", "lt", "lte"])
@pytest.mark.parametrize("operand,sqlite_exact,pg_exact", [
    ("2026-01-01T23:59:00Z", True, True),
    ("2026-01-01T23:59:00.123456+09:00", True, True),
    ("2026-01-01T23:59", True, False),
    ("2026-01-01 23:59:00+00:00", True, False),
    ("2026-01-01T23:59:00+00:00:01", True, False),
    ("２０２６-01-01T23:59:00Z", False, False),
    ("not-an-instant", False, False),
])
def test_compiled_datetime_operand_exactness(dialect, op, operand, sqlite_exact, pg_exact) -> None:
    row_filter = RowFilter(where=(WhereTerm("v", "datetime", op, operand),))
    compiled = compile_row_filter(row_filter, dialect, SQLITE_DOMAIN)
    assert compiled.exact is (sqlite_exact if dialect == "sqlite" else pg_exact)
    assert (compiled.sql, compiled.params) == compile_filter(row_filter, dialect, SQLITE_DOMAIN)


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
@pytest.mark.parametrize("path", ["page", "all"])
@pytest.mark.parametrize("compiler", ["compile_filter", "compile_row_filter"])
def test_compiler_errors_propagate_before_prefilter_guard(dialect, path, compiler, monkeypatch) -> None:
    from ontary.store.postgres import PostgresStore

    # These read entry points need no connection before compilation, so the
    # PostgreSQL guard placement is also pinned in the offline suite.
    store = object.__new__(ObjectStore if dialect == "sqlite" else PostgresStore)
    store.bind_domain = SQLITE_DOMAIN
    error = ValueError("compiler failed")
    compiled_filters = []

    def fail_compile(row_filter, actual_dialect, domain):
        compiled_filters.append((row_filter, actual_dialect, domain))
        raise error

    def unexpected_guard(*args, **kwargs):
        pytest.fail("compiler errors must propagate before entering the prefilter guard")

    module = type(store).__module__ if compiler == "compile_filter" else "ontary.store._filter"
    monkeypatch.setattr(import_module(module), compiler, fail_compile)
    monkeypatch.setattr(_sql, "prefilter", unexpected_guard)
    row_filter = RowFilter(where=(WhereTerm("status", "str", "eq", "open"),))
    with count_fallbacks(store) as fallbacks:
        with pytest.raises(ValueError) as caught:
            if path == "page":
                store.read_page_filtered("Item", row_filter, batch=1)
            else:
                store.read_all_filtered("Item", row_filter)
        assert fallbacks.total() == 0
    assert caught.value is error
    assert compiled_filters == [(row_filter, dialect, SQLITE_DOMAIN)]
