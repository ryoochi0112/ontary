"""R5 receipts and generated I1/I2 checks through the consumer read path."""

from __future__ import annotations

import importlib
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from _fetch_counter import count_fallbacks, count_fetched
from _filter_cases import CORPUS_COUNT, CORPUS_SEED, consumer_filter_cases
from pydantic import ValidationError
from test_pushdown import _bound, _ids

from ontary.errors import ValidationFailed, VisibilityError
from ontary.meta import ObjectTypeDef, OntologyRegistry, PropertyDef
from ontary.query import GuardedQuery
from ontary.scope import DirectProperty, ScopePolicy
from ontary.security import Consumer
from ontary.store import InMemoryStore, ObjectStore, Source
from ontary.store._core import StoreCore
from ontary.store._filter import pushable

SRC = Source(source_system="bind-domain-corpus")


def _registry() -> OntologyRegistry:
    registry = OntologyRegistry()
    registry.register_object_type(ObjectTypeDef(
        api_name="Receipt", display_name="Receipt", description="R5 corpus", layer="L0",
        primary_key="id", properties=[
            PropertyDef(name=name, type=kind, required=False)
            for name, kind in (("id", "str"), ("team_id", "str"), ("i", "int"),
                               ("j", "json"), ("s", "str"), ("f", "float"),
                               ("dt", "datetime"))
        ],
    ))
    registry.validate()
    return registry


def _seed(store: StoreCore) -> None:
    with store.transaction():
        for i in range(32):
            store.insert("Receipt", {
                "id": str(i), "team_id": "owned" if i < 24 else "foreign",
                "i": i, "j": i if i % 2 else str(i),
                "s": ("open", "closed", "café", "")[i] if i < 4 else str(i), "f": i / 2,
                "dt": "2026-02-15T00:00:00+00:00",
            }, SRC)
        # Null and missing values exercise the Python matcher as well as SQL.
        store.insert("Receipt", {"id": "null", "team_id": "owned",
                                 "i": None, "j": None, "s": None}, SRC)
        store.insert("Receipt", {"id": "missing", "team_id": "owned"}, SRC)


def _query(store: StoreCore, registry: OntologyRegistry) -> GuardedQuery:
    policy = ScopePolicy(levels=["team"], min_n=1, rules={
        "Receipt": [DirectProperty(level="team", property_name="team_id")],
    })
    policy.validate(registry)
    return GuardedQuery(store, registry, policy)


@dataclass
class CorpusStore:
    backend: str
    store: StoreCore
    query: GuardedQuery
    reference: GuardedQuery


def _pair(backend: str, store: StoreCore, registry: OntologyRegistry) -> CorpusStore:
    reference = InMemoryStore(registry)
    _seed(reference)
    _seed(store)
    return CorpusStore(backend, store, _query(store, registry), _query(reference, registry))


@pytest.fixture(scope="module", params=["sqlite", "postgres"])
def corpus_store(request: pytest.FixtureRequest) -> Iterator[CorpusStore]:
    registry = _registry()
    if request.param == "sqlite":
        store = ObjectStore(registry)
        try:
            yield _pair("sqlite", store, registry)
        finally:
            store._conn.close()
        return
    dsn = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("ONTARY_TEST_POSTGRES_DSN is not set")
    import psycopg
    from psycopg.conninfo import make_conninfo

    from ontary.store.postgres import PostgresStore

    schema = "ontary_test_corpus_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    store = None
    try:
        store = PostgresStore(registry, make_conninfo(dsn, options=f"-csearch_path={schema}"))
        yield _pair("postgres", store, registry)
    finally:
        if store is not None:
            store.close()
        with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _consumer(scope_id="owned") -> Consumer:
    return Consumer(actor_id="reader", role="Reader", scope_level="team",
                    scope_id=scope_id, kind="human")


def _read(query: GuardedQuery, where, path: str, scope_id="owned"):
    consumer = _consumer(scope_id)
    if path == "count":
        return query.count(consumer, "Receipt", where)
    if path == "aggregate":
        return query.aggregate(consumer, "Receipt", "i", where=where, func="sum")
    limit = 5 if path == "page" else None
    result = query.get_objects(consumer, "Receipt", where, limit=limit)
    rows = result.items if limit is not None else result
    return (_ids(rows), [row.payload for row in rows],
            result.has_more if limit is not None else False)


@pytest.mark.parametrize("path", ["unbounded", "page", "count", "aggregate"])
@pytest.mark.parametrize("kind", ["ints", "strs"])
@pytest.mark.parametrize("size", [1_000, 30_000])
def test_large_in_receipts(corpus_store: CorpusStore, size: int, kind: str, path: str) -> None:
    # Eight matches, with nonmatching rows both before and after them. Fillers
    # keep the exact receipt sizes without making every stored row a match.
    items = [*range(8, 16), *range(1_000, 1_000 + size - 8)]
    if kind == "strs":
        items = [str(item) for item in items]
    where = {"i" if kind == "ints" else "s": {"in": items}}
    expected = _read(corpus_store.reference, where, path)
    with count_fallbacks(corpus_store.store) as fallbacks:
        with count_fetched(corpus_store.store) as fetched:
            assert _read(corpus_store.query, where, path) == expected
        assert fallbacks.total() == 0
    if size == 30_000:
        matches = len(_read(corpus_store.reference, where, "unbounded")[0])
        assert matches == 8
        _bound(fetched, 6 if path == "page" else matches, "Receipt")


@pytest.mark.parametrize("path", ["unbounded", "page", "count", "aggregate"])
@pytest.mark.parametrize("probe", ["huge-gt", "huge-eq", "long-str", "surrogate-scope"])
def test_unbindable_receipts(corpus_store: CorpusStore, probe: str, path: str) -> None:
    where = {"huge-gt": {"i": {"gt": 10**5000}},
             "huge-eq": {"j": 10**5000}, "long-str": {"s": "x" * 70_000},
             "surrogate-scope": None}[probe]
    scope_id = "\ud800x" if probe == "surrogate-scope" else "owned"
    _assert_read_parity(corpus_store, where, path, scope_id)


def _assert_read_parity(data: CorpusStore, where, path: str, scope_id="owned") -> bool:
    with count_fallbacks(data.store) as fallbacks:
        try:
            expected = _read(data.reference, where, path, scope_id)
        except (ValidationFailed, ValidationError, VisibilityError) as refusal:
            with pytest.raises(type(refusal)) as actual:
                _read(data.query, where, path, scope_id)
            assert type(actual.value) is type(refusal)
            assert getattr(actual.value, "code", None) == getattr(refusal, "code", None)
            accepted = False
        else:
            assert _read(data.query, where, path, scope_id) == expected
            accepted = True
        assert fallbacks.total() == 0
    return accepted


def test_generated_consumer_corpus(corpus_store: CorpusStore, record_property, monkeypatch) -> None:
    started = time.perf_counter()
    accepted = 0
    count = 0
    coverage = {"compiled": 0, "outside": 0, "large_in": 0, "outside_in": 0}
    observed: set[str] = set()
    backend = importlib.import_module(type(corpus_store.store).__module__)
    original = backend.compile_filter

    def inspect_term(term, domain):
        if term.op == "and":
            return any([inspect_term(sub, domain) for sub in term.operand])
        if term.op == "in":
            if len(term.operand) >= 1_000:
                observed.add("large_in")
            outside = any(not pushable(value, domain) for value in term.operand)
            if outside:
                observed.add("outside_in")
            return outside
        return not pushable(term.operand, domain)

    def spy(row_filter, dialect, domain):
        observed.add("compiled")
        outside = any([inspect_term(term, domain) for term in row_filter.where])
        if row_filter.scope is not None:
            outside |= not pushable(row_filter.scope.scope_id, domain)
        if outside:
            observed.add("outside")
        return original(row_filter, dialect, domain)

    monkeypatch.setattr(backend, "compile_filter", spy)
    for case in consumer_filter_cases(corpus_store.store.bind_domain):
        observed.clear()
        try:
            was_accepted = _assert_read_parity(corpus_store, case.where, case.path, case.scope_id)
        except Exception as exc:
            # Never interpolate caller values: huge int repr itself can raise.
            exc.add_note(f"backend={corpus_store.backend} seed={CORPUS_SEED} "
                         f"case={case.index} field={case.field} operator={case.operator} "
                         f"operand_kind={case.operand_kind} path={case.path}")
            raise
        if was_accepted:
            accepted += 1
            for metric in observed:
                coverage[metric] += 1
        count += 1
    elapsed = time.perf_counter() - started
    print(f"R5 corpus backend={corpus_store.backend} count={count} seed={CORPUS_SEED} "
          f"seconds={elapsed:.3f} accepted={accepted} refused={count - accepted} "
          f"coverage={coverage}")
    assert count == CORPUS_COUNT >= 2_000
    assert accepted >= 1_200
    assert coverage["compiled"] == accepted
    assert coverage["outside"] >= 0.4 * accepted
    assert coverage["large_in"] >= 50
    assert coverage["outside_in"] >= 25
    record_property("corpus_backend", corpus_store.backend)
    record_property("corpus_count", count)
    record_property("corpus_seed", CORPUS_SEED)
    record_property("corpus_seconds", elapsed)
    for metric, measured in coverage.items():
        record_property("corpus_" + metric, measured)


@pytest.fixture(scope="module")
def latin1_database_store() -> Iterator[CorpusStore]:
    dsn = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("ONTARY_TEST_POSTGRES_DSN is not set")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from ontary.store.postgres import PostgresStore

    registry = _registry()
    name = "ontary_test_corpus_latin1_" + uuid.uuid4().hex[:12]
    with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as admin:
        try:
            admin.execute(sql.SQL(
                "CREATE DATABASE {} ENCODING 'LATIN1' "
                "LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0"
            ).format(sql.Identifier(name)))
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("Postgres role lacks CREATEDB for the LATIN1 database test")
        store = None
        try:
            store = PostgresStore(registry, make_conninfo(
                dsn, dbname=name, client_encoding="UTF8", options="-csearch_path=public",
            ))
            assert store._conn.info.encoding == "utf-8"
            assert "iso8859-1" in store.bind_domain.encodings
            yield _pair("postgres-latin1", store, registry)
        finally:
            if store is not None:
                store.close()
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


@pytest.mark.parametrize("path", ["unbounded", "page", "count", "aggregate"])
def test_latin1_database_receipt(latin1_database_store: CorpusStore, path: str) -> None:
    data = latin1_database_store
    _assert_read_parity(data, {"s": "中"}, path)
    with count_fallbacks(data.store) as fallbacks:
        # This read also pins connection usability after the encoding receipt.
        assert _read(data.query, None, "unbounded") == _read(data.reference, None, "unbounded")
        assert fallbacks.total() == 0
