"""Batch sizing depends on storage filtering, not just scope shape."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from test_pagination import _CallCountingStore

from ontary.errors import ValidationFailed
from ontary.meta import ObjectTypeDef, OntologyRegistry, PropertyDef
from ontary.query import GuardedQuery
from ontary.scope import DirectProperty, ScopePolicy, SelfScope
from ontary.security import Consumer
from ontary.store import DEFAULT_BATCH, InMemoryStore, ObjectStore, Source
from ontary.store._filter import SQLITE_DOMAIN, BindDomain, RowFilter, WhereTerm
from ontary.store.postgres import PostgresStore

SRC = Source(source_system="batching-test")
READER = Consumer(actor_id="reader", role="Reader", scope_level="team",
                  scope_id="owned", kind="human")


@pytest.fixture(params=["memory", "wrapper", "sqlite"])
def data(request: pytest.FixtureRequest) -> Iterator[tuple]:
    registry = OntologyRegistry()
    registry.register_object_type(ObjectTypeDef(
        api_name="Item", display_name="Item", description="Batch sizing", layer="L0",
        primary_key="id", properties=[
            PropertyDef(name="id", type="str"),
            PropertyDef(name="team_id", type="str"),
            PropertyDef(name="status", type="str"),
            PropertyDef(name="rank", type="int"),
        ],
    ))
    registry.validate()
    raw = ObjectStore(registry) if request.param == "sqlite" else InMemoryStore(registry)
    with raw.transaction():
        for i in range(12):
            raw.insert("Item", {"id": str(i), "team_id": "owned",
                                "status": "open" if i % 2 == 0 else "closed",
                                "rank": i}, SRC)
    store = _CallCountingStore(raw) if request.param == "wrapper" else raw
    try:
        yield request.param, registry, raw, store
    finally:
        if isinstance(raw, ObjectStore):
            raw._conn.close()


def _query(registry, store, *, unscoped=False, python_scope=False):
    policy = ScopePolicy(
        levels=["team"], min_n=1,
        rules={} if unscoped else {
            "Item": [DirectProperty(level="team", property_name="team_id")],
        },
        unscoped_types={"Item"} if unscoped else set(),
        row_visibility={"Item": lambda *_: True} if python_scope else {},
    )
    policy.validate(registry)
    return GuardedQuery(store, registry, policy)


def _record_pages(monkeypatch, store):
    calls = []
    for name in ("read_page", "read_page_filtered"):
        original = getattr(store, name)

        def record(*args, _original=original, _name=name, **kwargs):
            calls.append((_name, kwargs.get("after_key"), kwargs["batch"]))
            return _original(*args, **kwargs)

        monkeypatch.setattr(store, name, record)
    return calls


@pytest.mark.parametrize("path", ["page", "exists"])
def test_operand_outside_store_domain_uses_default_batches(data, monkeypatch, path):
    backend, registry, raw, store = data
    monkeypatch.setattr(raw, "bind_domain", BindDomain(("iso8859-1",)))
    query = _query(registry, store)
    calls = _record_pages(monkeypatch, store)
    if path == "exists":
        assert not query.exists(READER, "Item", {"status": "中"})
    else:
        assert query.get_objects(READER, "Item", {"status": "中"}, limit=2).items == []
    method = "read_page" if backend == "wrapper" else "read_page_filtered"
    assert calls == [(method, None, DEFAULT_BATCH)]


@pytest.mark.parametrize("path", ["page", "ordered", "exists"])
@pytest.mark.parametrize("selection", ["where", "scope", "none", "python"])
def test_batch_sizes_at_store_boundary(data, monkeypatch, path, selection):
    backend, registry, _raw, store = data
    query = _query(registry, store, unscoped=selection == "none",
                   python_scope=selection == "python")
    calls = _record_pages(monkeypatch, store)
    where = {"status": "open"} if selection in ("where", "python") else None
    if path == "exists":
        assert query.exists(READER, "Item", where)
    else:
        page = query.get_objects(READER, "Item", where, limit=2,
                                 order_by="rank" if path == "ordered" else None)
        assert [row.payload["id"] for row in page.items] == (
            ["0", "2"] if selection in ("where", "python") else ["0", "1"]
        )
        assert page.has_more
    # Reverting store awareness makes memory/wrappers fetch tiny batches;
    # reverting the ordered sizing makes even SQL walk the stream in tiny batches.
    exact = backend == "sqlite" and selection in ("where", "scope")
    expected_batch = (1 if path == "exists" else 3) if exact and path != "ordered" else DEFAULT_BATCH
    expected_method = "read_page" if backend == "wrapper" or selection == "none" else "read_page_filtered"
    assert calls == [(expected_method, None, expected_batch)]


def test_ordered_unfiltered_resume_walks_stream_once(data, monkeypatch):
    _backend, registry, _raw, store = data
    query = _query(registry, store, unscoped=True)
    first = query.get_objects(READER, "Item", limit=2, order_by="rank")
    assert first.next_cursor is not None
    calls = _record_pages(monkeypatch, store)
    page = query.get_objects(READER, "Item", limit=2, order_by="rank",
                             after=first.next_cursor)
    assert [row.payload["id"] for row in page.items] == ["2", "3"]
    assert calls == [("read_page", None, DEFAULT_BATCH)]


def test_ordered_unfiltered_stale_cursor_validated_without_second_walk(data, monkeypatch):
    _backend, registry, raw, store = data
    query = _query(registry, store, unscoped=True)
    first = query.get_objects(READER, "Item", limit=2, order_by="rank")
    assert first.next_cursor is not None
    raw.update("Item", "1", {"rank": 99}, SRC)
    calls = _record_pages(monkeypatch, store)
    with pytest.raises(ValidationFailed) as refusal:
        query.get_objects(READER, "Item", limit=2, order_by="rank",
                          after=first.next_cursor)
    assert refusal.value.code == "STALE_CURSOR"
    assert calls == [("read_page", None, DEFAULT_BATCH),
                     ("read_page", first.next_cursor, 1)]


def test_ordered_filtered_foreign_cursor_fallback_uses_large_batches(data, monkeypatch):
    backend, registry, _raw, store = data
    query = _query(registry, store)
    first = query.get_objects(READER, "Item", {"status": "closed"},
                              limit=2, order_by="rank")
    assert first.next_cursor is not None
    calls = _record_pages(monkeypatch, store)
    page = query.get_objects(READER, "Item", {"status": "open"},
                             limit=2, order_by="rank", after=first.next_cursor)
    assert [row.payload["id"] for row in page.items] == ["4", "6"]
    method = "read_page" if backend == "wrapper" else "read_page_filtered"
    expected = [(method, None, DEFAULT_BATCH)]
    if backend == "sqlite":
        expected += [("read_page", first.next_cursor, 1),
                     ("read_page", None, DEFAULT_BATCH)]
    assert calls == expected


@pytest.mark.parametrize("path", ["page", "exists"])
@pytest.mark.parametrize("where", [None, {"status": "open"}])
def test_climbing_scope_uses_default_batches(monkeypatch, path, where):
    registry = OntologyRegistry()
    for name, properties in [
        ("Team", [PropertyDef(name="company_id", type="str")]),
        ("Ticket", [PropertyDef(name="team_id", type="str"),
                    PropertyDef(name="status", type="str")]),
    ]:
        registry.register_object_type(ObjectTypeDef(
            api_name=name, display_name=name, description="Climbing scope", layer="L0",
            primary_key="id", properties=[PropertyDef(name="id", type="str"), *properties],
        ))
    registry.validate()
    policy = ScopePolicy(levels=["team", "company"], min_n=1, rules={
        "Team": [SelfScope(level="team"),
                 DirectProperty(level="company", property_name="company_id")],
        "Ticket": [DirectProperty(level="team", property_name="team_id")],
    })
    policy.validate(registry)
    consumer = Consumer(actor_id="reader", role="Reader", scope_level="company",
                        scope_id="owned", kind="human")
    store = ObjectStore(registry)
    try:
        with store.transaction():
            for team in ("owned", "foreign"):
                store.insert("Team", {"id": team, "company_id": team}, SRC)
            for i in range(DEFAULT_BATCH + 3):
                store.insert("Ticket", {
                    "id": str(i), "status": "open",
                    "team_id": "foreign" if i < DEFAULT_BATCH else "owned",
                }, SRC)
        calls = _record_pages(monkeypatch, store)
        query = GuardedQuery(store, registry, policy)
        if path == "exists":
            assert query.exists(consumer, "Ticket", where)
        else:
            page = query.get_objects(consumer, "Ticket", where, limit=5)
            assert [row.payload["id"] for row in page.items] == [
                str(i) for i in range(DEFAULT_BATCH, DEFAULT_BATCH + 3)
            ]
            assert not page.has_more and page.next_cursor is None
        assert len(calls) == 2
        assert calls[0] == ("read_page_filtered", None, DEFAULT_BATCH)
        assert calls[1][0] == "read_page_filtered" and calls[1][1] is not None
        assert calls[1][2] == DEFAULT_BATCH
    finally:
        store._conn.close()


STATIC_FILTER_CASES = [
    pytest.param({"at": {"gt": "2026-01-01T23:59"}},
                 WhereTerm("at", "datetime", "gt", "2026-01-01T23:59"), [], id="S1"),
    pytest.param({"at": {"gt": "2026-01-01 23:59:00+00:00"}},
                 WhereTerm("at", "datetime", "gt", "2026-01-01 23:59:00+00:00"), [], id="S2"),
    pytest.param({"rank": {"gt": 2**60}},
                 WhereTerm("rank", "int", "gt", 2**60), [], id="S3"),
    pytest.param({"rank": {"in": [2**60, 1]}},
                 WhereTerm("rank", "int", "in", [2**60, 1]), ["1"], id="S4"),
    pytest.param({"rank": {"gt": 2**60, "lt": 10}},
                 WhereTerm("rank", "int", "and", (
                     WhereTerm("rank", "int", "gt", 2**60),
                     WhereTerm("rank", "int", "lt", 10),
                 )), [], id="S5"),
]


@pytest.fixture(scope="module", params=["sqlite", "postgres"])
def sql_data(request: pytest.FixtureRequest) -> Iterator[tuple]:
    registry = OntologyRegistry()
    registry.register_object_type(ObjectTypeDef(
        api_name="Item", display_name="Item", description="Compiler exactness", layer="L0",
        primary_key="id", properties=[
            PropertyDef(name="id", type="str"),
            PropertyDef(name="rank", type="int"),
            PropertyDef(name="at", type="datetime"),
        ],
    ))
    registry.validate()
    schema = None
    store = None
    if request.param == "postgres":
        dsn = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("ONTARY_TEST_POSTGRES_DSN is not set")
        import psycopg
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        schema = "ontary_test_batching_" + uuid.uuid4().hex
        with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
            conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        if schema is None:
            store = ObjectStore(registry)
        else:
            options = conninfo_to_dict(dsn).get("options", "") + f" -csearch_path={schema}"
            store = PostgresStore(registry, make_conninfo(dsn, options=options))
        with store.transaction():
            for i in range(12):
                store.insert("Item", {"id": str(i), "rank": i,
                                      "at": "2026-01-01T12:00:00+00:00"}, SRC)
        yield request.param, registry, store
    finally:
        if store is not None:
            store._conn.close()
        if schema is not None:
            with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
                conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
@pytest.mark.parametrize("where,term,expected_ids", STATIC_FILTER_CASES)
def test_store_static_prefilter_exactness(dialect, where, term, expected_ids):
    # Exactness belongs to the compiler, so these PG unit pins need no DSN.
    store = object.__new__(ObjectStore if dialect == "sqlite" else PostgresStore)
    store.bind_domain = SQLITE_DOMAIN
    assert store.prefilter_exact(RowFilter(where=(term,))) is (dialect == "sqlite")


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
@pytest.mark.parametrize("nested", [False, True])
def test_store_prefilter_exactness_with_any_inexact_and_child(dialect, nested):
    store = object.__new__(ObjectStore if dialect == "sqlite" else PostgresStore)
    store.bind_domain = SQLITE_DOMAIN
    # S5's large-but-bindable operand is exact on SQLite. A bind-domain TRUE
    # child additionally pins the same any-child rule on both dialects.
    unsafe = WhereTerm("rank", "int", "gt", 2**63)
    if nested:
        unsafe = WhereTerm("rank", "int", "and", (unsafe,))
    safe = WhereTerm("rank", "int", "lt", 10)
    for children in ((unsafe, safe), (safe, unsafe)):
        term = WhereTerm("rank", "int", "and", children)
        assert not store.prefilter_exact(RowFilter(where=(term,)))


@pytest.mark.parametrize("path", ["exists", "page"])
@pytest.mark.parametrize("where,term,expected_ids", [
    *STATIC_FILTER_CASES,
    pytest.param({"rank": {"gt": 10}}, WhereTerm("rank", "int", "gt", 10),
                 ["11"], id="control"),
])
def test_static_operand_first_batch_at_store_boundary(
    sql_data, monkeypatch, path, where, term, expected_ids,
):
    dialect, registry, store = sql_data
    query = _query(registry, store, unscoped=True)
    batches = []
    original = store._filtered_page_rows

    def record(obj_type, row_filter, after_row_id, batch):
        batches.append(batch)
        return original(obj_type, row_filter, after_row_id, batch)

    monkeypatch.setattr(store, "_filtered_page_rows", record)
    if path == "exists":
        assert query.exists(READER, "Item", where) is bool(expected_ids)
    else:
        page = query.get_objects(READER, "Item", where, limit=2)
        assert [row.payload["id"] for row in page.items] == expected_ids
        assert not page.has_more
    exact = dialect == "sqlite" or term.operand == 10
    assert batches[0] == ((1 if path == "exists" else 3) if exact else DEFAULT_BATCH)


@pytest.mark.parametrize("path", ["exists", "page"])
def test_sql_wrapper_uses_default_batches(sql_data, monkeypatch, path):
    _dialect, registry, raw = sql_data
    store = _CallCountingStore(raw)
    query = _query(registry, store, unscoped=True)
    calls = _record_pages(monkeypatch, store)
    if path == "exists":
        assert query.exists(READER, "Item", {"rank": {"gt": 10}})
    else:
        page = query.get_objects(READER, "Item", {"rank": {"gt": 10}}, limit=2)
        assert [row.payload["id"] for row in page.items] == ["11"]
    assert calls == [("read_page", None, DEFAULT_BATCH)]
