"""Consumer read bounds, cursor parity, and injection safety on SQL stores."""

from __future__ import annotations

import os
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from _fetch_counter import count_fetched

from ontary.errors import ValidationFailed
from ontary.meta import Cardinality, LinkTypeDef, ObjectTypeDef, OntologyRegistry, PropertyDef
from ontary.query import GuardedQuery
from ontary.scope import DirectProperty, ScopePolicy, SelfScope, ViaLink
from ontary.security import Consumer
from ontary.store import InMemoryStore, ObjectStore, Source
from ontary.store._core import StoreCore
from ontary.store._filter import RowFilter

INJECTION = "x'); DROP TABLE objects; --"
MATCHES = tuple(50 + 90 * i for i in range(10))
OWNED = frozenset((*MATCHES[:7], 30, 31, 32))
TYPES = ("WhereRows", "ScopeRows", "BothRows", "ViaRows", "InjectionRows", "Team")
SRC = Source(source_system="pushdown-test")


def _registry() -> OntologyRegistry:
    registry = OntologyRegistry()
    for name in TYPES:
        registry.register_object_type(ObjectTypeDef(
            api_name=name, display_name=name, description="Pushdown bounds", layer="L0",
            primary_key="id", properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="team_id", type="str", required=False),
                PropertyDef(name="status", type="str", required=False),
                PropertyDef(name="rank", type="int", required=False),
            ],
        ))
    registry.register_link_type(LinkTypeDef(
        api_name="belongsTo", from_type="ViaRows", to_type="Team",
        cardinality=Cardinality.MANY_TO_ONE, description="Governing team",
    ))
    registry.validate()
    return registry


def _seed(store: StoreCore) -> None:
    # One transaction per backend keeps the public write-path seeds fast.
    with store.transaction():
        for team in ("owned", "foreign"):
            store.insert("Team", {"id": team}, SRC)
        for name in TYPES[:4]:
            for i in range(1000):
                owned = name == "WhereRows" or (
                    i in MATCHES if name == "ScopeRows" else i in OWNED
                )
                store.insert(name, {
                    "id": str(i), "team_id": "owned" if owned else "foreign",
                    "status": "open" if i in MATCHES else "closed", "rank": 1000 - i,
                }, SRC)
        # Seven matching rows are owned, three are foreign. A closed owned
        # row also has a link, so the reference must enforce where and scope.
        for i in (*MATCHES, 30):
            store.create_link("belongsTo", str(i), "owned" if i in OWNED else "foreign")
        for i, owner in enumerate((INJECTION, "owned", INJECTION, "x", "foreign")):
            store.insert("InjectionRows", {
                "id": str(i), "team_id": owner, "status": "open", "rank": i,
            }, SRC)


@dataclass
class SeededStore:
    store: StoreCore
    registry: OntologyRegistry

    def query(self, obj_type: str, shape: str = "direct") -> GuardedQuery:
        rules = [DirectProperty(level="team", property_name="team_id")]
        if shape == "self":
            rules = [SelfScope(level="team")]
        elif shape == "both":
            rules.append(SelfScope(level="team"))
        elif shape == "via":
            rules = [ViaLink(link_api_name="belongsTo", direction="from", parent_type="Team")]
        policy = ScopePolicy(levels=["team"], min_n=1, rules={
            obj_type: rules, "Team": [SelfScope(level="team")],
        })
        policy.validate(self.registry)
        return GuardedQuery(self.store, self.registry, policy)


@pytest.fixture(scope="module", params=["sqlite", "postgres"])
def seeded(request: pytest.FixtureRequest) -> Iterator[SeededStore]:
    registry = _registry()
    if request.param == "sqlite":
        store = ObjectStore(registry)
        try:
            _seed(store)
            yield SeededStore(store, registry)
        finally:
            store._conn.close()
        return

    dsn = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("ONTARY_TEST_POSTGRES_DSN is not set")
    import psycopg

    from ontary.store.postgres import PostgresStore

    schema = "ontary_test_pushdown_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    store = None
    try:
        separator = "&" if "?" in dsn else "?"
        store = PostgresStore(registry, f"{dsn}{separator}options=-csearch_path%3D{schema}")
        _seed(store)
        yield SeededStore(store, registry)
    finally:
        if store is not None:
            store._conn.close()
        with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture(scope="module")
def reference() -> SeededStore:
    registry = _registry()
    store = InMemoryStore(registry)
    _seed(store)
    return SeededStore(store, registry)


def _consumer(scope_id: str = "owned") -> Consumer:
    return Consumer(actor_id="reader", role="Reader", scope_level="team",
                    scope_id=scope_id, kind="human")


def _ids(rows) -> list[str]:
    return [row.lineage.object_id for row in rows]


def _bound(counter: Counter[str], maximum: int, obj_type: str | None = None) -> None:
    fetched = counter.total() if obj_type is None else counter[obj_type]
    assert fetched <= maximum, f"fetched={fetched}, bound={maximum}, by_type={dict(counter)}"


@pytest.mark.parametrize("limit", [5, None])
def test_where_bound(seeded: SeededStore, limit: int | None) -> None:
    query = seeded.query("WhereRows")
    with count_fetched(seeded.store) as counter:
        result = query.get_objects(_consumer(), "WhereRows", {"status": "open"}, limit=limit)
    expected = [str(i) for i in MATCHES]
    assert _ids(result.items if limit is not None else result) == (
        expected[:limit] if limit is not None else expected
    )
    if limit is not None:
        assert result.has_more and result.next_cursor is not None
    _bound(counter, 6 if limit is not None else 10)


@pytest.mark.parametrize("shape", ["direct", "self", "both"])
@pytest.mark.parametrize("limit", [5, None])
def test_scope_bound(seeded: SeededStore, shape: str, limit: int | None) -> None:
    query = seeded.query("ScopeRows", shape)
    # SelfScope identifies one unique object; it cannot own ten distinct ids.
    # The mixed DirectProperty/SelfScope shape retains the ten-row population.
    consumer = _consumer(str(MATCHES[0]) if shape == "self" else "owned")
    expected = [str(i) for i in (MATCHES[:1] if shape == "self" else MATCHES)]
    with count_fetched(seeded.store) as counter:
        result = query.get_objects(consumer, "ScopeRows", limit=limit)
    assert _ids(result.items if limit is not None else result) == (
        expected[:limit] if limit is not None else expected
    )
    if limit is not None:
        assert result.has_more == (len(expected) > limit)
    _bound(counter, 1 if shape == "self" else (6 if limit is not None else 10))


@pytest.mark.parametrize("limit", [5, None])
def test_combined_bound(seeded: SeededStore, limit: int | None) -> None:
    query = seeded.query("BothRows")
    expected = [str(i) for i in MATCHES if i in OWNED]
    assert len(expected) == 7 and len(OWNED) == len(MATCHES) == 10
    with count_fetched(seeded.store) as counter:
        result = query.get_objects(_consumer(), "BothRows", {"status": "open"}, limit=limit)
    assert _ids(result.items if limit is not None else result) == (
        expected[:limit] if limit is not None else expected
    )
    if limit is not None:
        assert result.has_more and result.next_cursor is not None
    _bound(counter, len(expected) + (limit is not None))


@pytest.mark.parametrize("limit", [5, None])
def test_via_link_where_bound(seeded: SeededStore, reference: SeededStore, limit) -> None:
    query = seeded.query("ViaRows", "via")
    expected = reference.query("ViaRows", "via").get_objects(
        _consumer(), "ViaRows", {"status": "open"}, limit=limit,
    )
    with count_fetched(seeded.store) as counter:
        result = query.get_objects(_consumer(), "ViaRows", {"status": "open"}, limit=limit)
    rows = result.items if limit is not None else result
    expected_rows = expected.items if limit is not None else expected
    assert _ids(rows) == _ids(expected_rows)
    assert [row.payload for row in rows] == [row.payload for row in expected_rows]
    assert len(rows) == (5 if limit is not None else 7)
    if limit is not None:
        assert result.has_more == expected.has_more
    # Storage-step wrappers key counts by obj_type: Team resolution reads are
    # recorded too, but only ViaRows contributes to the listing-row bound.
    assert counter["Team"] > 0
    _bound(counter, 10, "ViaRows")


@pytest.mark.parametrize("path", ["page", "unbounded", "ordered", "count", "aggregate", "exists"])
def test_all_read_path_bounds(seeded: SeededStore, path: str) -> None:
    query = seeded.query("BothRows")
    consumer = _consumer()
    where = {"status": "open"}
    expected = [str(i) for i in MATCHES if i in OWNED]
    with count_fetched(seeded.store) as counter:
        if path == "page":
            result = query.get_objects(consumer, "BothRows", where, limit=5)
            assert _ids(result.items) == expected[:5]
            assert result.has_more
        elif path == "unbounded":
            assert _ids(query.get_objects(consumer, "BothRows", where, limit=None)) == expected
        elif path == "ordered":
            result = query.get_objects(consumer, "BothRows", where, limit=5, order_by="rank")
            assert _ids(result.items) == list(reversed(expected))[:5]
            assert result.has_more
        elif path == "count":
            assert query.count(consumer, "BothRows", where) == len(expected)
        elif path == "aggregate":
            assert query.aggregate(consumer, "BothRows", "rank", where=where, func="sum") == sum(
                1000 - int(obj_id) for obj_id in expected
            )
        else:
            assert query.exists(consumer, "BothRows", where)
    _bound(counter, {"page": 6, "exists": 1}.get(path, len(expected)))


def test_cursor_walk_and_resume(seeded: SeededStore) -> None:
    query = seeded.query("BothRows")
    expected = [str(i) for i in MATCHES if i in OWNED]
    seen = []
    after = None
    cursors = set()
    while True:
        with count_fetched(seeded.store) as counter:
            page = query.get_objects(_consumer(), "BothRows", {"status": "open"},
                                     limit=3, after=after)
        _bound(counter, 4)
        ids = _ids(page.items)
        assert ids == expected[len(seen):len(seen) + 3]
        if len(seen) == 3:
            assert ids[0] == expected[expected.index(seen[-1]) + 1]
        seen.extend(ids)
        if not page.has_more:
            assert page.next_cursor is None
            break
        assert page.next_cursor is not None and page.next_cursor not in cursors
        cursors.add(page.next_cursor)
        after = page.next_cursor
    assert Counter(seen) == Counter(expected)


@pytest.mark.parametrize("order_by", [None, "rank"])
def test_cursor_reused_with_different_where(seeded: SeededStore, reference: SeededStore,
                                           order_by: str | None) -> None:
    def resume(data):
        query = data.query("BothRows")
        first = query.get_objects(_consumer(), "BothRows", {"status": "open"},
                                  limit=3, order_by=order_by)
        assert first.has_more and first.next_cursor is not None
        try:
            page = query.get_objects(_consumer(), "BothRows", {"status": "closed"},
                                     limit=3, order_by=order_by, after=first.next_cursor)
        except ValidationFailed as exc:
            return ("refused", exc.code)
        return ("page", _ids(page.items), page.has_more)

    assert resume(seeded) == resume(reference)


def _row_count(store: StoreCore) -> int:
    # Seeds are immutable and all rows live: the store's public reads check
    # the objects population without hand-written SQL or a table-name operand.
    return sum(len(store.read_all(name)) for name in TYPES)


def test_scope_id_injection(seeded: SeededStore) -> None:
    before = _row_count(seeded.store)
    query = seeded.query("InjectionRows")
    with count_fetched(seeded.store) as counter:
        rows = query.get_objects(_consumer(INJECTION), "InjectionRows", limit=None)
    assert _ids(rows) == ["0", "2"]
    assert all(row.payload["team_id"] == INJECTION for row in rows)
    _bound(counter, 2)
    assert _row_count(seeded.store) == before == 4007


def test_where_key_injection_refused_before_fetch(seeded: SeededStore) -> None:
    before = _row_count(seeded.store)
    query = seeded.query("InjectionRows")
    with count_fetched(seeded.store) as counter:
        with pytest.raises(ValidationFailed) as refusal:
            query.get_objects(_consumer(), "InjectionRows", {INJECTION: "open"}, limit=None)
    assert refusal.value.code == "UNKNOWN_FIELD"
    assert counter.total() == 0
    assert _row_count(seeded.store) == before == 4007


def test_fetch_counter_steps_and_restoration(seeded: SeededStore) -> None:
    store = seeded.store
    names = ("_all_rows", "_page_rows", "_filtered_page_rows", "_current_row", "_last_row")
    originals = {name: getattr(store, name) for name in names}
    instance_originals = {name: store.__dict__[name] for name in names if name in store.__dict__}
    with pytest.raises(RuntimeError, match="restore"):
        with count_fetched(store) as counter:
            query = seeded.query("InjectionRows")
            assert _ids(query.get_objects(_consumer(), "InjectionRows", limit=None)) == ["1"]
            assert counter.total() == 1
            assert len(store.read_all("InjectionRows")) == 5
            assert len(store.read_page("InjectionRows", batch=2)) == 2
            assert len(store.read_page_filtered("InjectionRows", RowFilter(), batch=3)) == 3
            assert store.read_current("InjectionRows", "0") is not None
            assert store.read_current("InjectionRows", "missing") is None
            assert store.read_last("InjectionRows", "0") is not None
            assert store.read_last("InjectionRows", "missing") is None
            assert counter.total() == 13
            raise RuntimeError("restore")
    assert all(getattr(store, name) == original for name, original in originals.items())
    assert {name: store.__dict__[name] for name in names if name in store.__dict__} == instance_originals


@pytest.mark.parametrize("limit", [None, 5])
@pytest.mark.parametrize("probe", ["operand", "in", "scope_id"])
@pytest.mark.parametrize("shape", ["direct", "self", "both"])
def test_surrogate_bind_safety(seeded: SeededStore, reference: SeededStore,
                               limit: int | None, probe: str, shape: str) -> None:
    consumer = _consumer("\ud800" if probe == "scope_id" else
                         str(MATCHES[0]) if shape == "self" else "owned")
    where = ({"status": "\ud800"} if probe == "operand" else
             {"status": {"in": ["\ud800"]}} if probe == "in" else None)
    expected = reference.query("ScopeRows", shape).get_objects(
        consumer, "ScopeRows", where, limit=limit)
    result = seeded.query("ScopeRows", shape).get_objects(
        consumer, "ScopeRows", where, limit=limit)
    rows = result.items if limit is not None else result
    expected_rows = expected.items if limit is not None else expected
    assert _ids(rows) == _ids(expected_rows) == []
    if limit is not None:
        assert result.has_more == expected.has_more
        assert not result.has_more
        assert result.next_cursor is None and expected.next_cursor is None


@pytest.fixture
def latin1_store() -> Iterator[SeededStore]:
    dsn = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("ONTARY_TEST_POSTGRES_DSN is not set")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from ontary.store.postgres import PostgresStore

    registry = _registry()
    schema = "ontary_test_pushdown_encoding_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    store = None
    try:
        # Preserve existing DSN options while selecting the isolated schema and
        # the real client bind domain that exposed the regression.
        options = conninfo_to_dict(dsn).get("options", "")
        options += f" -csearch_path={schema} -cclient_encoding=LATIN1"
        store = PostgresStore(registry, make_conninfo(dsn, options=options))
        assert store._conn.info.encoding == "iso8859-1"
        yield SeededStore(store, registry)
    finally:
        if store is not None:
            store._conn.close()
        with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.parametrize("limit", [None, 1])
@pytest.mark.parametrize("probe", ["scope_id", "operand", "in"])
def test_connection_encoding_bind_safety(latin1_store: SeededStore,
                                         limit: int | None, probe: str) -> None:
    reference = SeededStore(InMemoryStore(latin1_store.registry), latin1_store.registry)
    payloads = [
        {"id": "encoding-0", "team_id": "日本", "status": "開"},
        {"id": "encoding-1", "team_id": "日本", "status": "closed"},
        {"id": "encoding-2", "team_id": "owned", "status": "開"},
        {"id": "encoding-3", "team_id": "owned", "status": "closed"},
    ]
    for seeded_store in (latin1_store, reference):
        with seeded_store.store.transaction():
            for payload in payloads:
                seeded_store.store.insert("ScopeRows", payload, SRC)
    consumer = _consumer("日本" if probe == "scope_id" else "owned")
    where = ({"status": "開"} if probe == "operand" else
             {"status": {"in": ["closed", "開"]}} if probe == "in" else None)
    expected = reference.query("ScopeRows").get_objects(
        consumer, "ScopeRows", where, limit=limit)
    result = latin1_store.query("ScopeRows").get_objects(
        consumer, "ScopeRows", where, limit=limit)
    expected_ids = (["encoding-0", "encoding-1"] if probe == "scope_id" else
                    ["encoding-2"] if probe == "operand" else
                    ["encoding-2", "encoding-3"])
    rows = result.items if limit is not None else result
    expected_rows = expected.items if limit is not None else expected
    assert _ids(rows) == _ids(expected_rows) == expected_ids[:limit]
    if limit is not None:
        assert result.has_more == expected.has_more == (len(expected_ids) > limit)
        assert (result.next_cursor is not None) == (expected.next_cursor is not None)
