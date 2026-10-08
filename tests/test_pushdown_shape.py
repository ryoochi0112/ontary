"""Query prefilter classification, paging seams, and scope-cache priming."""

from __future__ import annotations

import pytest
from conftest import (
    ConsumerFactory,
    ObjectTypeFactory,
    PolicyFactory,
    RegistryFactory,
    StoreFactory,
    raises_code,
)
from test_pagination import _CallCountingStore

from ontary.errors import ValidationFailed
from ontary.query import GuardedQuery
from ontary.query._pushdown import (
    _fetch_page,
    build_row_filter,
    filter_is_exact,
    scope_pushdown,
)
from ontary.query._where import CompiledWhere, _compile_where, _normalize_where
from ontary.scope import (
    CustomResolver,
    DirectProperty,
    ScopeRule,
    SelfScope,
    ViaLink,
    _ScopeReadCache,
    resolve_owning_scope,
)
from ontary.store import DEFAULT_BATCH, Source
from ontary.store._filter import RowFilter, ScopeTerm, WhereTerm

SRC = Source(source_system="test")
DIRECT = DirectProperty(level="org", property_name="owner")
SELF = SelfScope(level="org")


@pytest.mark.parametrize(
    ("rules", "row_visibility", "unscoped", "expected"),
    [
        ([SELF], False, False, "sql"),
        ([DIRECT], False, False, "sql"),
        ([DIRECT, SELF], False, False, "sql"),
        ([DIRECT, ViaLink(link_api_name="parent", direction="from", parent_type="Org")],
         False, False, "python"),
        ([DIRECT, CustomResolver(level="other", fn=lambda *_: None)],
         False, False, "python"),
        ([DIRECT], True, False, "python"),
        ([], False, True, "unscoped"),
    ],
    ids=["SelfScope", "DirectProperty", "both", "+ViaLink", "+CustomResolver",
         "+row_visibility", "unscoped"],
)
def test_scope_shapes(
    make_policy: PolicyFactory,
    rules: list[ScopeRule],
    row_visibility: bool,
    unscoped: bool,
    expected: str,
) -> None:
    policy = make_policy(
        rules={"Item": rules} if rules else {},
        unscoped_types={"Item"} if unscoped else set(),
        row_visibility={"Item": lambda *_: True} if row_visibility else {},
    )
    assert scope_pushdown(policy, "Item") == expected
    assert filter_is_exact(policy, "Item") == (expected != "python")


@pytest.fixture
def registry(make_registry: RegistryFactory, make_object_type: ObjectTypeFactory):
    return make_registry(object_types=[make_object_type(
        "Item", ("owner", "str"), ("status", "str"), ("rank", "int"),
    )])


def test_compiled_where_preserves_clauses_and_python_matcher(registry) -> None:
    matcher = _compile_where(registry, "Item", _normalize_where({
        "status": {"in": ["open", "pending"]}, "rank": {"gte": 2, "lt": 5},
    }))
    assert isinstance(matcher, CompiledWhere)
    assert matcher.clauses == (
        ("status", "str", "in", ["open", "pending"]),
        ("rank", "int", "and", (("rank", "int", "gte", 2), ("rank", "int", "lt", 5))),
    )
    assert matcher({"status": "open", "rank": 3})
    assert not matcher({"status": "open", "rank": 5})
    assert not matcher({"status": "closed", "rank": 3})


def test_build_filter_preserves_conjunction_and_scope_rule_order(
    registry, make_policy: PolicyFactory, make_consumer: ConsumerFactory,
) -> None:
    policy = make_policy(levels=["team", "org"], rules={"Item": [
        DIRECT, DirectProperty(level="team", property_name="team_id"), SELF,
    ]})
    matcher = _compile_where(registry, "Item", _normalize_where({
        "rank": {"gte": 2, "lt": 5},
    }))
    assert build_row_filter(registry, policy, make_consumer(), "Item", matcher) == RowFilter(
        where=(WhereTerm("rank", "int", "and", (
            WhereTerm("rank", "int", "gte", 2), WhereTerm("rank", "int", "lt", 5),
        )),),
        scope=ScopeTerm((("prop", "owner"), ("self", None)), "org-1", True),
    )


@pytest.mark.parametrize("unscoped", [False, True])
def test_python_scope_keeps_where_only(
    registry, make_policy: PolicyFactory, make_consumer: ConsumerFactory, unscoped: bool,
) -> None:
    policy = make_policy(
        rules={} if unscoped else {"Item": [DIRECT]},
        unscoped_types={"Item"} if unscoped else set(),
        row_visibility={"Item": lambda *_: True},
    )
    matcher = _compile_where(registry, "Item", _normalize_where({"status": "open"}))
    assert scope_pushdown(policy, "Item") == "python"
    assert not filter_is_exact(policy, "Item")
    assert build_row_filter(registry, policy, make_consumer(), "Item", matcher) == RowFilter(
        where=(WhereTerm("status", "str", "eq", "open"),),
    )
    assert build_row_filter(registry, policy, make_consumer(), "Item", None) is None


def test_missing_consumer_level_cannot_climb(
    registry, make_policy: PolicyFactory, make_consumer: ConsumerFactory,
) -> None:
    policy = make_policy(rules={"Item": [DIRECT]})
    consumer = make_consumer(scope_level="unknown")
    assert build_row_filter(registry, policy, consumer, "Item", None) == RowFilter(
        scope=ScopeTerm((), "org-1", False),
    )


def test_broader_consumer_can_climb_from_types_only_narrower_rule(
    registry, make_object_type: ObjectTypeFactory, make_policy: PolicyFactory,
    make_consumer: ConsumerFactory, make_store: StoreFactory,
) -> None:
    registry.register_object_type(make_object_type("Team", ("org_id", "str")))
    policy = make_policy(levels=["team", "org"], rules={
        "Item": [DirectProperty(level="team", property_name="owner")],
        "Team": [SelfScope(level="team"), DirectProperty(level="org", property_name="org_id")],
    })
    store = make_store(registry)
    store.insert("Team", {"id": "team-1", "org_id": "org-1"}, SRC)
    store.insert("Item", {"id": "one", "owner": "team-1", "status": "open", "rank": 1}, SRC)
    consumer = make_consumer()
    assert build_row_filter(registry, policy, consumer, "Item", None) == RowFilter(
        scope=ScopeTerm((), "org-1", True),
    )
    narrow = make_consumer(scope_level="team", scope_id="team-1")
    assert build_row_filter(registry, policy, narrow, "Item", None).scope.climb_possible is False
    assert [row.lineage.object_id for row in GuardedQuery(store, registry, policy).get_objects(
        consumer, "Item", limit=None,
    )] == ["one"]


def test_fetch_shim_uses_plain_page_for_wrapper(
    registry, make_policy: PolicyFactory, make_store: StoreFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(registry)
    query = GuardedQuery(store, registry, make_policy(unscoped_types={"Item"}))
    calls = []
    monkeypatch.setattr(store, "read_page_filtered", lambda *args, **kwargs: calls.append(
        (args, kwargs)
    ) or [])
    row_filter = RowFilter(where=(WhereTerm("status", "str", "eq", "open"),))
    assert _fetch_page(query, "Item", row_filter, None, 7) == []
    assert calls == [(("Item", row_filter), {"after_key": None, "batch": 7})]
    wrapper = _CallCountingStore(store, max_calls=1)
    wrapped_query = GuardedQuery(wrapper, registry, query._policy)
    assert _fetch_page(wrapped_query, "Item", row_filter, None, 7) == []
    assert wrapper.read_page_calls == 1
    assert len(calls) == 1


def test_prime_judges_fetched_version_and_replaces_cached_current(
    registry, make_policy: PolicyFactory, make_store: StoreFactory,
) -> None:
    store = make_store(registry)
    store.insert("Item", {"id": "one", "owner": "org-1", "status": "open", "rank": 1}, SRC)
    fetched = store.read_current("Item", "one")
    cache = _ScopeReadCache()
    store.update("Item", "one", {"owner": "org-2"}, SRC)
    assert cache.read_current(store, "Item", "one").payload["owner"] == "org-2"
    cache._prime(fetched)
    policy = make_policy(rules={"Item": [DIRECT]})
    assert resolve_owning_scope(policy, store, "Item", "one", cache=cache) == {"org": "org-1"}
    assert cache.read_last(store, "Item", "one").payload["owner"] == "org-2"


@pytest.mark.parametrize("path", ["page", "unbounded", "ordered", "count", "exists", "aggregate"])
@pytest.mark.parametrize("python_scope", [False, True])
def test_read_paths_use_prefilter_batches_and_prime_rows(
    registry, make_policy: PolicyFactory, make_consumer: ConsumerFactory,
    make_store: StoreFactory, monkeypatch: pytest.MonkeyPatch, path: str, python_scope: bool,
) -> None:
    store = make_store(registry)
    for index in range(4):
        store.insert("Item", {"id": str(index), "owner": "org-1", "status": "open",
                              "rank": index}, SRC)
    policy = make_policy(rules={"Item": [DIRECT]}, min_n=1,
                         row_visibility={"Item": lambda *_: True} if python_scope else {})
    query = GuardedQuery(store, registry, policy)
    consumer = make_consumer()
    calls = []
    original = store.read_page_filtered
    original_all = store.read_all_filtered

    def fetch(obj_type, row_filter, after_key=None, batch=DEFAULT_BATCH):
        calls.append((row_filter, batch))
        return original(obj_type, row_filter, after_key=after_key, batch=batch)

    def fetch_all(obj_type, row_filter):
        calls.append((row_filter, None))
        return original_all(obj_type, row_filter)

    def reread(*_args):
        pytest.fail("scope resolution re-read the row being judged")

    monkeypatch.setattr(store, "read_page_filtered", fetch)
    monkeypatch.setattr(store, "read_all_filtered", fetch_all)
    monkeypatch.setattr(store, "read_current", reread)
    where = {"status": "open"}
    if path == "page":
        assert len(query.get_objects(consumer, "Item", where, limit=2).items) == 2
    elif path == "ordered":
        assert len(query.get_objects(consumer, "Item", where, limit=2, order_by="rank").items) == 2
    elif path == "unbounded":
        assert len(query.get_objects(consumer, "Item", where, limit=None)) == 4
    elif path == "count":
        assert query.count(consumer, "Item", where) == 4
    elif path == "exists":
        assert query.exists(consumer, "Item", where)
    else:
        assert query.aggregate(consumer, "Item", where=where, func="count") == 4
    # Full-stream reads judge one statement's snapshot (no batch); pages and
    # exists walk batches sized by `filter_is_exact`.
    if path in {"unbounded", "count", "aggregate"}:
        expected_batch = None
    elif python_scope:
        expected_batch = DEFAULT_BATCH
    else:
        expected_batch = 1 if path == "exists" else 3
    assert calls
    assert all(batch == expected_batch for _, batch in calls)
    assert all(f.where == (WhereTerm("status", "str", "eq", "open"),) for f, _ in calls)
    assert all((f.scope is None) == python_scope for f, _ in calls)


@pytest.mark.parametrize("path", [
    "page", "unbounded", "ordered", "count", "exists", "aggregate", "aggregate_by",
])
def test_read_paths_judge_where_and_scope_after_permissive_fetch(
    registry, make_policy: PolicyFactory, make_consumer: ConsumerFactory,
    make_store: StoreFactory, monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    """Every read rejects non-matching and out-of-scope rows from a SQL superset."""
    store = make_store(registry)
    for obj_id, owner, status, rank in [
        ("closed-first", "org-1", "closed", 0),
        ("open-high", "org-1", "open", 4),
        ("closed-between", "org-1", "closed", 1),
        ("open-foreign", "org-2", "open", 2),
        ("open-low", "org-1", "open", 3),
        ("closed-last", "org-1", "closed", 5),
    ]:
        store.insert("Item", {"id": obj_id, "owner": owner, "status": status,
                              "rank": rank}, SRC)
    query = GuardedQuery(store, registry, make_policy(rules={"Item": [DIRECT]}, min_n=1))
    consumer = make_consumer()

    def permissive_rows(obj_type, _row_filter, after_row_id, batch):
        return store._page_rows(obj_type, after_row_id, batch)

    def permissive_all_rows(obj_type, _row_filter):
        return store._all_rows(obj_type)

    # Keep real rows, cursor validation, and batch limits, while forcing a
    # superset that makes the Python where and visibility checks essential.
    monkeypatch.setattr(store, "_filtered_page_rows", permissive_rows)
    monkeypatch.setattr(store, "_filtered_all_rows", permissive_all_rows)
    where = {"status": "open"}
    if path in {"page", "ordered"}:
        order_by = "rank" if path == "ordered" else None
        expected = ["open-low", "open-high"] if path == "ordered" else ["open-high", "open-low"]
        first = query.get_objects(consumer, "Item", where, limit=1, order_by=order_by)
        assert [row.lineage.object_id for row in first.items] == expected[:1]
        assert first.has_more
        assert first.next_cursor is not None
        second = query.get_objects(consumer, "Item", where, limit=1, order_by=order_by,
                                   after=first.next_cursor)
        assert [row.lineage.object_id for row in second.items] == expected[1:]
        assert not second.has_more
        assert second.next_cursor is None
    elif path == "unbounded":
        assert [row.lineage.object_id for row in query.get_objects(
            consumer, "Item", where, limit=None,
        )] == ["open-high", "open-low"]
    elif path == "count":
        assert query.count(consumer, "Item", where) == 2
    elif path == "exists":
        assert query.exists(consumer, "Item", where)
        assert not query.exists(consumer, "Item", {"status": "pending"})
        assert not query.exists(consumer, "Item", {"status": "open", "rank": {"lt": 3}})
    elif path == "aggregate":
        assert query.aggregate(consumer, "Item", where=where, func="count") == 2
        assert query.aggregate(consumer, "Item", "rank", where=where, func="sum") == 7.0
    else:
        assert query.aggregate_by(consumer, "Item", None, "status", where, func="count") == {
            "open": 2,
        }
        assert query.aggregate_by(consumer, "Item", "rank", "status", where, func="sum") == {
            "open": 7.0,
        }


def test_ordered_foreign_where_cursor_continues_and_retired_cursor_is_stale(
    registry, make_policy: PolicyFactory, make_consumer: ConsumerFactory,
    make_store: StoreFactory,
) -> None:
    store = make_store(registry)
    for index, status in enumerate(["closed", "closed", "open", "open"]):
        store.insert("Item", {"id": str(index), "owner": "org-1", "status": status,
                              "rank": index}, SRC)
    query = GuardedQuery(store, registry, make_policy(rules={"Item": [DIRECT]}))
    consumer = make_consumer()
    first = query.get_objects(consumer, "Item", limit=1, order_by="rank")
    resumed = query.get_objects(consumer, "Item", {"status": "open"}, limit=2,
                                order_by="rank", after=first.next_cursor)
    assert [row.lineage.object_id for row in resumed.items] == ["2", "3"]
    store.retire_object("Item", "0")
    with raises_code(ValidationFailed, "STALE_CURSOR"):
        query.get_objects(consumer, "Item", {"status": "open"}, limit=2,
                          order_by="rank", after=first.next_cursor)


def test_ordered_matching_cursor_does_not_fetch_unfiltered_rows(
    registry, make_policy: PolicyFactory, make_consumer: ConsumerFactory,
    make_store: StoreFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(registry)
    for index in range(4):
        store.insert("Item", {"id": str(index), "owner": "org-1", "status": "open",
                              "rank": index}, SRC)
    query = GuardedQuery(store, registry, make_policy(rules={"Item": [DIRECT]}))
    consumer = make_consumer()
    first = query.get_objects(consumer, "Item", limit=1, order_by="rank")

    def unfiltered(*_args, **_kwargs):
        pytest.fail("an ordered cursor present in the filtered stream needs no unfiltered read")

    monkeypatch.setattr(store, "read_page", unfiltered)
    resumed = query.get_objects(consumer, "Item", limit=2, order_by="rank", after=first.next_cursor)
    assert [row.lineage.object_id for row in resumed.items] == ["1", "2"]
