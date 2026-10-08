"""Batch sizing depends on storage filtering, not just scope shape."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from test_pagination import _CallCountingStore

from ontary.errors import ValidationFailed
from ontary.meta import ObjectTypeDef, OntologyRegistry, PropertyDef
from ontary.query import GuardedQuery
from ontary.scope import DirectProperty, ScopePolicy, SelfScope
from ontary.security import Consumer
from ontary.store import DEFAULT_BATCH, InMemoryStore, ObjectStore, Source

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
