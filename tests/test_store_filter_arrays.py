"""Bounded IN parameters and final Python parity on both SQL backends."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any

import pytest

from ontary.meta import ObjectTypeDef, OntologyRegistry, PropertyDef
from ontary.query import GuardedQuery
from ontary.query._pushdown import _where_term
from ontary.query._where import CompiledWhere
from ontary.scope import ScopePolicy
from ontary.security import Consumer
from ontary.store import InMemoryStore, ObjectStore, Source, _sql
from ontary.store._core import StoreCore
from ontary.store._filter import (
    SQLITE_DOMAIN,
    BindDomain,
    RowFilter,
    ScopeTerm,
    WhereTerm,
    compile_filter,
    filter_is_selective,
)
from ontary.typesys import PropertyType

KINDS: dict[str, PropertyType] = {
    "i": "int", "s": "str", "f": "float", "b": "bool", "j": "json",
    "d": "date", "t": "datetime",
}
CONSUMER = Consumer(actor_id="reader", role="Reader", scope_level="team",
                    scope_id="owned", kind="human")


class Number(IntEnum):
    ONE = 1


class Text(str, Enum):
    ONE = "1"


class FloatValue(float):
    pass


@dataclass
class ArrayStores:
    dialect: _sql.Dialect
    store: StoreCore
    reference: InMemoryStore
    registry: OntologyRegistry

    def query(self, store: StoreCore) -> GuardedQuery:
        policy = ScopePolicy(levels=["team"], unscoped_types={"Rows"}, min_n=1)
        policy.validate(self.registry)
        return GuardedQuery(store, self.registry, policy)


@pytest.fixture(scope="module", params=["sqlite", "postgres"])
def array_stores(request: pytest.FixtureRequest) -> Iterator[ArrayStores]:
    registry = OntologyRegistry()
    registry.register_object_type(ObjectTypeDef(
        api_name="Rows", display_name="Rows", description="Array filters", layer="L0",
        primary_key="id", properties=[PropertyDef(name="id", type="str"), *[
            PropertyDef(name=field, type=kind, required=False) for field, kind in KINDS.items()
        ]],
    ))
    registry.validate()
    schema = None
    dsn = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
    if request.param == "sqlite":
        store = ObjectStore(registry)
    else:
        if not dsn:
            pytest.skip("ONTARY_TEST_POSTGRES_DSN is not set")
        import psycopg
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        from ontary.store.postgres import PostgresStore

        schema = "ontary_test_filter_arrays_" + uuid.uuid4().hex
        with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
            conn.execute(f'CREATE SCHEMA "{schema}"')
        options = conninfo_to_dict(dsn).get("options", "") + f" -csearch_path={schema}"
        store = PostgresStore(registry, make_conninfo(dsn, options=options))
    reference = InMemoryStore(registry)
    try:
        payloads = [
            {"id": str(i), "i": i, "s": str(i), "f": i / 2, "b": bool(i % 2), "j": i,
             "d": "2026-02-15", "t": "2026-02-15T00:00:00Z"}
            for i in (-1, *range(12), 29_999, 30_000, 2**53, 2**53 + 1, 2**63 - 1,
                      -(2**53), -(2**53) - 1, -(2**63))
        ]
        payloads.extend([
            {"id": "null", **dict.fromkeys(KINDS)}, {"id": "missing"},
            {"id": "text", "s": "é", "j": "open"},
            {"id": "bool", "j": True}, {"id": "list", "j": [1]},
            {"id": "dict", "j": {"a": 1}}, {"id": "fraction", "f": 1.0000000000000002},
            *[{"id": f"string-{i}", "s": value, "j": value}
              for i, value in enumerate(["é", "É", "e\u0301", "é ", "ß", "ss", "1", "01"])],
            *[{"id": f"float-{i}", "f": value, "j": value}
              for i, value in enumerate([0.1, 1.0000000000000002, 1e-308, 1e100, -1e100])],
        ])
        # Include historical shapes without making the write validator the judge.
        for target in (store, reference):
            with target.transaction():
                for payload in payloads:
                    target._insert_object_row("Rows", payload["id"], json.dumps(payload),
                                              "2026-02-15T00:00:00+00:00",
                                              Source(source_system="array-tests"))
        yield ArrayStores(request.param, store, reference, registry)
    finally:
        store._conn.close()
        if schema is not None:
            with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
                conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _assert_query_parity(data: ArrayStores, where: dict[str, Any]) -> None:
    actual = data.query(data.store)
    reference = data.query(data.reference)
    for limit in (None, 5):
        got = actual.get_objects(CONSUMER, "Rows", where, limit=limit)
        expected = reference.get_objects(CONSUMER, "Rows", where, limit=limit)
        got_rows = got if limit is None else got.items
        expected_rows = expected if limit is None else expected.items
        assert [row.payload for row in got_rows] == [row.payload for row in expected_rows]
        if limit is not None:
            assert got.has_more == expected.has_more
            assert (got.next_cursor is not None) == (expected.next_cursor is not None)
    assert actual.count(CONSUMER, "Rows", where) == reference.count(CONSUMER, "Rows", where)


@pytest.mark.parametrize("field", ["i", "s"])
def test_thirty_thousand_items_have_constant_parameters_and_query_parity(
    array_stores: ArrayStores, field: str,
) -> None:
    items = list(range(30_000)) if field == "i" else [str(i) for i in range(30_000)]
    counts = []
    for size in (1, 984, 30_000):
        fragment, params = compile_filter(
            RowFilter(where=(WhereTerm(field, KINDS[field], "in", items[:size]),)),
            array_stores.dialect, SQLITE_DOMAIN,
        )
        assert fragment.count("{p}") == len(params)
        counts.append(len(params))
    assert counts == [3, 3, 3]  # One array, two field markers.
    _assert_query_parity(array_stores, {field: {"in": items}})


@pytest.mark.parametrize("field,items", [
    ("i", [None, 1, 1, 2]), ("s", ["1", None, "1", "é"]),
    ("j", ["open", 1, 1.5, None, "open"]), ("i", []), ("s", []), ("j", []),
    ("j", ["open", 1, 1.5, "open"]), ("j", ["open", 1, True, False, None]),
    ("b", [True, False, True, None]), ("f", [1.0000000000000002, 0.1, None]),
    ("d", ["2026-02-15", None]), ("t", ["2026-02-15T00:00:00Z", None]),
])
def test_in_null_mixed_duplicates_and_empty_query_parity(
    array_stores: ArrayStores, field: str, items: list[Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    if None in items:
        # Public operand validation currently rejects nullable IN lists (and
        # bool on json). Supply the supported IR at the validated-clause seam
        # to exercise fetching, paging, count and the unchanged Python judge.
        compiled = CompiledWhere(((field, KINDS[field], "in", items),))
        monkeypatch.setattr("ontary.query._guarded._compile_where", lambda *_: compiled)
    _assert_query_parity(array_stores, {field: {"in": items}})


@pytest.mark.parametrize("field,operand,plain", [
    ("i", Number.ONE, 1), ("s", Text.ONE, "1"), ("f", FloatValue(0.5), 0.5),
])
@pytest.mark.parametrize("op", ["eq", "in", "and"])
def test_scalar_subclasses_still_push_down_and_preserve_rows(
    array_stores: ArrayStores, field: str, operand: Any, plain: Any, op: str,
) -> None:
    kind = KINDS[field]
    value = [operand, operand] if op == "in" else operand
    clause = (field, kind, op, ((field, kind, "ne", operand),) if op == "and" else value)
    term = _where_term(clause)
    normalized = term.operand[0].operand if op == "and" else (
        term.operand[0] if op == "in" else term.operand
    )
    assert type(normalized) is type(plain)
    assert normalized == plain
    assert filter_is_selective(RowFilter(where=(term,)), SQLITE_DOMAIN)
    condition = {"ne": operand} if op == "and" else {"in": value} if op == "in" else value
    _assert_query_parity(array_stores, {field: condition})


@pytest.mark.parametrize("field,op", [("i", "gt"), ("j", "eq")])
def test_huge_integer_operands_return_python_rows_without_string_conversion(
    array_stores: ArrayStores, field: str, op: str,
) -> None:
    if array_stores.dialect != "sqlite":
        pytest.skip("SQLite huge-int regression")
    value = 10**5000
    assert compile_filter(RowFilter(where=(WhereTerm(field, KINDS[field], op, value),)),
                          "sqlite", SQLITE_DOMAIN) == ("TRUE", [])
    _assert_query_parity(array_stores, {field: {"gt": value} if op == "gt" else value})


@pytest.mark.parametrize("kind,items", [
    ("str", ["é", "é", "e\u0301", "ß"]), ("str", [None]),
    ("int", [1, 1, 2]), ("int", [None, 2**53, 2**53 + 1]),
    ("float", [0.1, 1.0000000000000002, 1e-308, -0.0]),
    ("float", [float(2**53 + 2), 1e100, None]), ("bool", [True, False, True]),
    ("date", ["2026-02-15", None]), ("datetime", ["2026-02-15T00:00:00Z"]),
    ("json", [None]), ("json", ["open", 1, 1.5, True, False, None, "open"]),
    ("json", [1, 2, 0.1]), ("json", [True, False]), ("json", []),
])
def test_in_prefilter_is_exactly_the_or_of_scalar_eq(
    array_stores: ArrayStores, kind: PropertyType, items: list[Any],
) -> None:
    dialect = array_stores.dialect
    term = WhereTerm("j", kind, "in", items)
    actual, actual_params = compile_filter(RowFilter(where=(term,)), dialect, SQLITE_DOMAIN)
    parts, expected_params = [], []
    for item in items:
        part, params = compile_filter(
            RowFilter(where=(WhereTerm("j", kind, "eq", item),)), dialect, SQLITE_DOMAIN,
        )
        parts.append(part)
        expected_params.extend(params)
    expected = " OR ".join(f"({part})" for part in parts) or "FALSE"
    # A real TEXT column prevents PostgreSQL from constant-folding guarded casts.
    query = _sql.render(
        f"SELECT ({actual}), ({expected}) FROM objects WHERE object_type = 'Rows'",
        dialect,
    )
    for got, want in _sql.execute(array_stores.store._conn, query,
                                  [*actual_params, *expected_params], dialect=dialect).rows:
        assert bool(got) == bool(want)
    assert len(actual_params) <= 5  # Three kinds and two fields, independent of duplicates.


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
def test_each_json_kind_uses_one_array_and_retains_duplicates(dialect: _sql.Dialect) -> None:
    items = ["é", 1, True, "é", 1.0000000000000002, False, None]
    fragment, params = compile_filter(
        RowFilter(where=(WhereTerm("j", "json", "in", items),)), dialect, SQLITE_DOMAIN,
    )
    assert len(params) == fragment.count("{p}") == 5
    if dialect == "sqlite":
        assert [json.loads(value) for value in params[:3]] == [
            ["é", "é"], [1, 1.0000000000000002], [1, 0],
        ]
        assert fragment.count("json_each({p})") == 3
    else:
        assert params[:3] == [["é", "é"], ["1", "1.0000000000000002"], [1, 0]]
        assert fragment.count("ANY({p}::numeric[])") == 2
        assert fragment.count("ANY({p}::text[])") == 1


def test_array_strings_compare_byte_exactly(array_stores: ArrayStores) -> None:
    fragment, params = compile_filter(
        RowFilter(where=(WhereTerm("s", "str", "in", ["é", "ß", "1"]),)),
        array_stores.dialect, SQLITE_DOMAIN,
    )
    rows = _sql.execute(array_stores.store._conn, _sql.render(
        f"SELECT id FROM objects WHERE id LIKE {{p}} AND ({fragment}) ORDER BY id",
        array_stores.dialect,
    ), ["string-%", *params], dialect=array_stores.dialect).rows
    assert [row[0] for row in rows] == ["string-0", "string-4", "string-6"]


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
def test_one_nan_in_item_disables_the_whole_clause(dialect: _sql.Dialect) -> None:
    row_filter = RowFilter(where=(WhereTerm("f", "float", "in", [1.0, float("nan")]),))
    assert compile_filter(row_filter, dialect, SQLITE_DOMAIN) == ("TRUE", [])
    assert not filter_is_selective(row_filter, SQLITE_DOMAIN)


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
@pytest.mark.parametrize("op", ["eq", "ne", "contains", "in", "gt", "gte", "lt", "lte"])
@pytest.mark.parametrize("value", [
    pytest.param(10**5000, id="huge-int"), pytest.param(2**63, id="above-int64"),
    pytest.param(float("inf"), id="infinity"), pytest.param("a" * 65_537, id="long-text"),
    pytest.param(Number.ONE, id="int-enum"), pytest.param(Text.ONE, id="str-enum"),
])
def test_rejected_operands_bind_nothing(dialect: _sql.Dialect, op: str, value: Any) -> None:
    term = WhereTerm("j", "json", op, [1, value] if op == "in" else value)
    row_filter = RowFilter(where=(term,))
    assert compile_filter(row_filter, dialect, SQLITE_DOMAIN) == ("TRUE", [])
    assert not filter_is_selective(row_filter, SQLITE_DOMAIN)


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
@pytest.mark.parametrize("domain,value", [
    (BindDomain(("utf-8", "ascii")), "é"),
    (BindDomain(("utf-8",), max_bytes=3), "four"),
    (SQLITE_DOMAIN, "a" * 65_537),
])
def test_fields_and_scope_ids_obey_the_entire_domain(
    dialect: _sql.Dialect, domain: BindDomain, value: str,
) -> None:
    filters = [
        RowFilter(where=(WhereTerm(value, "str", "eq", "a"),)),
        RowFilter(where=(WhereTerm(value, "str", "in", []),)),
        RowFilter(scope=ScopeTerm((("self", None),), value, False)),
        RowFilter(scope=ScopeTerm((("prop", "id"),), value, False)),
        RowFilter(scope=ScopeTerm((("prop", value), ("self", None)), "id", False)),
    ]
    for row_filter in filters:
        assert compile_filter(row_filter, dialect, domain) == ("TRUE", [])
        assert not filter_is_selective(row_filter, domain)
