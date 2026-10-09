"""Batched traversal preserves guarded results and costs two frontier reads."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from functools import wraps
from typing import Any
from unittest.mock import Mock

import pytest
from conftest import raises_code
from test_store_conformance import STORE_FACTORIES, StoreFactory

from ontary import ActionContext, ActionParams, OntologyObject, Sensitivity, Source, prop, target
from ontary.client import OntologyRuntime
from ontary.errors import ValidationFailed, VisibilityError
from ontary.query import GuardedQuery
from ontary.scope import DirectProperty
from ontary.store import Store

SRC = Source(source_system="traverse-many-tests")
LEVELS = ["person", "team", "org"]
READ_STEPS = (
    "_current_row", "_last_row", "_live_object_rows", "_current_rows_many",
    "_link_ids_from", "_link_ids_to", "_link_ids_from_many", "_link_ids_to_many",
)
LINK_STEPS = READ_STEPS[4:]


@pytest.fixture(params=STORE_FACTORIES, ids=lambda f: f.__name__.removeprefix("_make_"))
def store_factory(request: pytest.FixtureRequest) -> StoreFactory:
    return request.param


@contextmanager
def count_reads(store: Store) -> Iterator[Counter[str]]:
    """Wrap storage steps on this instance, like tests/_fetch_counter.py."""
    calls: Counter[str] = Counter()

    def counted(name, original):
        @wraps(original)
        def read(*args, **kwargs):
            calls[name] += 1
            return original(*args, **kwargs)

        return read

    with pytest.MonkeyPatch.context() as patch:
        for name in READ_STEPS:
            patch.setattr(store, name, counted(name, getattr(store, name)))
        yield calls


def assert_frontier_cost(calls: Counter[str], reverse: bool, *, targets: bool = True) -> None:
    link_step = "_link_ids_to_many" if reverse else "_link_ids_from_many"
    assert calls[link_step] == 1
    assert sum(calls[name] for name in LINK_STEPS) + calls["_current_rows_many"] <= 2
    assert calls["_current_rows_many"] == int(targets)
    assert calls["_current_row"] == 0


@dataclass
class Graph:
    ontology: Any
    store: Store
    board_cls: type[OntologyObject]
    record_cls: type[OntologyObject]
    forward: Any
    backward: Any
    records: dict[str, dict[str, Any]]
    anchors: list[str]


@pytest.fixture
def graph(store_factory, make_ontology) -> Graph:
    ontology = make_ontology(name="traverse-many", scope_levels=LEVELS, min_n=1)

    @ontology.object(layer="L0", scope="unscoped")
    class Board(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[DirectProperty(level=level, property_name=f"{level}_id") for level in LEVELS],
    )
    class Record(OntologyObject):
        id: str = prop(primary_key=True)
        person_id: str | None = None
        team_id: str | None = None
        org_id: str | None = None
        allowed: bool
        secret: str | None = prop(default=None, sensitivity=Sensitivity(human_visible=False))
        ai_secret: str | None = prop(default=None, sensitivity=Sensitivity(ai_usable=False))

    forward = ontology.link("pins", Board, Record, "MANY_TO_MANY")
    backward = ontology.link("onBoard", Record, Board, "MANY_TO_MANY")
    ontology.link("author", Board, Record, "MANY_TO_MANY", identity_revealing=True)
    ontology.validate()
    store = store_factory(ontology.registry)
    store.bind_clock(lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    anchors = [f"a-{i:04d}" for i in range(210)]
    records = {
        f"r-{i:04d}": {
            "id": f"r-{i:04d}",
            "person_id": None if i % 13 == 0 else f"person-{1 if i % 4 == 0 else 2}",
            "team_id": None if i % 13 == 0 else f"team-{1 if i % 3 == 0 else 2}",
            "org_id": None if i % 13 == 0 else f"org-{1 if i % 2 == 0 else 2}",
            "allowed": i % 5 != 0,
            "secret": f"human-secret-{i}",
            "ai_secret": f"ai-secret-{i}",
        }
        for i in range(40)
    }
    with store.transaction():
        for anchor in anchors:
            store.insert("Board", {"id": anchor}, SRC)
        for payload in records.values():
            store.insert("Record", payload, SRC)
        for i, anchor in enumerate(anchors):
            # Fan-out 0..10, overlapping targets, and insertion order that
            # differs from the store's tie order at this fixed clock tick.
            for j in reversed(range(i % 11)):
                record_id = f"r-{(i * 7 + j * 3) % 40:04d}"
                store.create_link("pins", anchor, record_id)
                store.create_link("onBoard", record_id, anchor)
        store.create_link("author", anchors[1], "r-0007")
        store.retire_object("Record", "r-0039")
    return Graph(ontology, store, Board, Record, forward, backward, records, anchors)


def scoped_query(graph: Graph, make_policy) -> GuardedQuery:
    policy = make_policy(
        levels=LEVELS,
        unscoped_types={"Board"},
        rules=graph.ontology.definition.policy.rules,
        row_visibility={"Record": lambda _store, _consumer, _type, row: row["allowed"]},
        min_n=1,
    )
    return GuardedQuery(graph.store, graph.ontology.registry, policy)


@pytest.mark.parametrize("reverse", [False, True])
def test_generated_graph_matches_single_traversals_at_every_consumer_level(
    graph, make_policy, make_consumer, reverse,
) -> None:
    query = scoped_query(graph, make_policy)
    link = "onBoard" if reverse else "pins"
    anchors = list(reversed(graph.anchors)) + ["missing", graph.anchors[1], "missing"]
    canonical = list(dict.fromkeys(anchors))
    for level in LEVELS:
        for kind in ("human", "ai"):
            consumer = make_consumer(scope_level=level, scope_id=f"{level}-1", kind=kind)
            actual = query.traverse_many(consumer, link, iter(anchors), reverse=reverse)
            assert list(actual) == canonical
            assert actual == {
                anchor: query.traverse(consumer, link, anchor, reverse=reverse)
                for anchor in canonical
            }
            assert actual["missing"] == actual[graph.anchors[0]] == []
            returned = 0
            excluded = 0
            for anchor in canonical:
                target_ids = (
                    graph.store.links_to(link, anchor) if reverse
                    else graph.store.links_from(link, anchor)
                )
                assert target_ids == sorted(target_ids)
                visible_ids = [
                    obj_id for obj_id in target_ids
                    if obj_id != "r-0039" and graph.records[obj_id]["allowed"]
                    and graph.records[obj_id][f"{level}_id"] == f"{level}-1"
                ]
                assert [row.lineage.object_id for row in actual[anchor]] == visible_ids
                excluded += len(target_ids) - len(visible_ids)
                for row in actual[anchor]:
                    payload = dict(graph.records[row.lineage.object_id])
                    del payload["secret" if kind == "human" else "ai_secret"]
                    assert row.payload == payload
                    returned += 1
            assert returned > 0 and excluded > 0
    # Redaction must not mutate the store or share mutable output payloads.
    rows = query.traverse_many(
        make_consumer(kind="ai"), link, graph.anchors, reverse=reverse,
    )
    shared = [row for group in rows.values() for row in group if row.lineage.object_id == "r-0006"]
    assert len(shared) > 1
    shared[0].payload["secret"] = "changed"
    assert shared[1].payload["secret"] == "human-secret-6"
    assert graph.store.read_current("Record", "r-0006").payload["secret"] == "human-secret-6"


class AnchorId(str, Enum):
    ONE = "a-0001"


@pytest.mark.parametrize("reverse", [False, True])
def test_single_traverse_batches_target_reads(graph, make_policy, make_consumer, reverse) -> None:
    policy = make_policy(unscoped_types={"Board", "Record"})
    query = GuardedQuery(graph.store, graph.ontology.registry, policy)
    link = "onBoard" if reverse else "pins"
    expected = query.traverse(make_consumer(), link, "a-0010", reverse=reverse)
    assert len(expected) > 1
    with count_reads(graph.store) as calls:
        actual = query.traverse(make_consumer(), link, "a-0010", reverse=reverse)
    assert actual == expected
    assert sum(calls[name] for name in LINK_STEPS) + calls["_current_rows_many"] <= 2
    assert calls["_current_rows_many"] == 1 and calls["_current_row"] == 0


@pytest.mark.parametrize("reverse", [False, True])
def test_canonical_anchors_empty_frontiers_and_one_scope_cache(
    graph, make_policy, make_consumer, monkeypatch, reverse,
) -> None:
    query = scoped_query(graph, make_policy)
    consumer = make_consumer()
    link = "onBoard" if reverse else "pins"
    gate = Mock(wraps=query._require_coherent_scope)
    visible = Mock(wraps=query._visible)
    monkeypatch.setattr(query, "_require_coherent_scope", gate)
    monkeypatch.setattr(query, "_visible", visible)
    with count_reads(graph.store) as calls:
        assert query.traverse_many(consumer, link, iter(()), reverse=reverse) == {}
    assert not calls
    with count_reads(graph.store) as calls:
        assert query.traverse_many(consumer, link, ["missing"], reverse=reverse) == {"missing": []}
    assert_frontier_cost(calls, reverse, targets=False)
    gate.reset_mock()
    actual = query.traverse_many(
        consumer, link, iter([AnchorId.ONE, "missing", "a-0001", "a-0002"]), reverse=reverse,
    )
    assert list(actual) == ["a-0001", "missing", "a-0002"]
    assert all(type(key) is str for key in actual)
    gate.assert_called_once_with("Record")
    caches = [call.kwargs["scope_cache"] for call in visible.call_args_list]
    assert len(caches) > 1 and all(cache is caches[0] for cache in caches)
    # a-0001's target is repeated at a-0041; real scope reads are memoized
    # across anchors as well as across the three policy levels.
    with count_reads(graph.store) as calls:
        query.traverse_many(consumer, link, ["a-0001", "a-0041"], reverse=reverse)
    target_ids = graph.store.links_from("pins", "a-0041")
    assert "r-0007" in target_ids
    assert calls["_current_row"] == len(target_ids)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("anchors", [[], ["missing"], ["a-0001"]])
def test_incoherent_scope_is_refused_before_reads(
    graph, make_policy, make_consumer, reverse, anchors,
) -> None:
    query = scoped_query(graph, make_policy)
    query._policy.unscoped_types.add("Record")
    consumer = make_consumer()
    link = "onBoard" if reverse else "pins"
    with count_reads(graph.store) as calls:
        with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR") as many_error:
            query.traverse_many(consumer, link, iter(anchors), reverse=reverse)
        with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR") as single_error:
            query.traverse(consumer, link, "missing", reverse=reverse)
    assert str(many_error.value) == str(single_error.value)
    assert not calls


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("anchors", [[], ["missing"], ["a-0001"]])
def test_identity_denial_precedes_scope_and_storage(
    graph, make_policy, make_consumer, reverse, anchors,
) -> None:
    query = scoped_query(graph, make_policy)
    query._policy.unscoped_types.add("Board" if reverse else "Record")
    if reverse:
        query._policy.rules["Board"] = [DirectProperty(level="org", property_name="id")]
    consumer = make_consumer(kind="human")
    with count_reads(graph.store) as calls:
        with raises_code(VisibilityError, "VISIBILITY_DENIED") as many_error:
            query.traverse_many(consumer, "author", iter(anchors), reverse=reverse)
        with raises_code(VisibilityError, "VISIBILITY_DENIED") as single_error:
            query.traverse(consumer, "author", "missing", reverse=reverse)
    assert str(many_error.value) == str(single_error.value)
    assert not calls


@pytest.mark.parametrize("reverse", [False, True])
def test_ai_can_traverse_identity_links(graph, make_policy, make_consumer, reverse) -> None:
    query = scoped_query(graph, make_policy)
    # r-0007 is foreign to org-1; the identity gate permits AI but scope still applies.
    anchor = "r-0007" if reverse else "a-0001"
    for scope_id in ("org-1", "org-2"):
        consumer = make_consumer(kind="ai", scope_id=scope_id)
        actual = query.traverse_many(consumer, "author", [anchor], reverse=reverse)
        expected_id = "a-0001" if reverse else "r-0007"
        assert [row.lineage.object_id for row in actual[anchor]] == (
            [expected_id] if reverse or scope_id == "org-2" else []
        )


@pytest.mark.parametrize("reverse", [False, True])
def test_unknown_link_keeps_the_single_traversal_error(graph, make_consumer, reverse) -> None:
    query = GuardedQuery(graph.store, graph.ontology.registry, graph.ontology.definition.policy)
    with count_reads(graph.store) as calls:
        with raises_code(ValidationFailed, "UNKNOWN_LINK_TYPE") as many_error:
            query.traverse_many(make_consumer(), "unknown", [], reverse=reverse)
        with raises_code(ValidationFailed, "UNKNOWN_LINK_TYPE") as single_error:
            query.traverse(make_consumer(), "unknown", "missing", reverse=reverse)
    assert str(many_error.value) == str(single_error.value)
    assert not calls


@pytest.mark.parametrize("reverse", [False, True])
def test_frontier_read_bound_for_1_10_500_anchors(
    store_factory, make_ontology, make_consumer, reverse,
) -> None:
    ontology = make_ontology(name="traverse-cost")

    @ontology.object(layer="L0", scope="unscoped")
    class Anchor(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(layer="L0", scope="unscoped")
    class Target(OntologyObject):
        id: str = prop(primary_key=True)

    link = (
        ontology.link("frontier", Target, Anchor, "MANY_TO_MANY") if reverse
        else ontology.link("frontier", Anchor, Target, "MANY_TO_MANY")
    )
    ontology.validate()
    store = store_factory(ontology.registry)
    anchors = [f"a-{i:04d}" for i in range(500)]
    targets = [f"t-{i:02d}" for i in range(10)]
    with store.transaction():
        for anchor in anchors:
            store.insert("Anchor", {"id": anchor}, SRC)
        for obj_id in targets:
            store.insert("Target", {"id": obj_id}, SRC)
        for anchor in anchors:
            for obj_id in targets:
                store.create_link("frontier", obj_id if reverse else anchor, anchor if reverse else obj_id)
    consumer = make_consumer()
    query = GuardedQuery(store, ontology.registry, ontology.definition.policy)
    ctx = ActionContext(store, SRC, consumer, registry=ontology.registry)
    for count in (1, 10, 500):
        with count_reads(store) as calls:
            rows = query.traverse_many(consumer, "frontier", iter(anchors[:count]), reverse=reverse)
        assert_frontier_cost(calls, reverse)
        assert list(rows) == anchors[:count]
        assert all([row.lineage.object_id for row in group] == targets for group in rows.values())
        with count_reads(store) as calls:
            typed = ctx.traverse_many(link, iter(anchors[:count]), reverse=reverse)
        assert_frontier_cost(calls, reverse)
        assert all([obj.id for obj in group] == targets for group in typed.values())
        assert list(typed) == anchors[:count]
        with count_reads(store) as calls:
            single = query.traverse(consumer, "frontier", anchors[0], reverse=reverse)
        assert sum(calls[name] for name in LINK_STEPS) + calls["_current_rows_many"] <= 2
        assert calls["_current_rows_many"] == 1 and calls["_current_row"] == 0
        assert [row.lineage.object_id for row in single] == targets
        with count_reads(store) as calls:
            single_typed = ctx.traverse(link, anchors[0], reverse=reverse)
        assert sum(calls[name] for name in LINK_STEPS) + calls["_current_rows_many"] <= 2
        assert calls["_current_rows_many"] == 1 and calls["_current_row"] == 0
        assert [obj.id for obj in single_typed] == targets


def test_action_handler_hands_out_raw_independent_saveable_targets(
    store_factory, make_ontology, make_consumer,
) -> None:
    ontology = make_ontology(name="traverse-action")

    @ontology.object(layer="L0", scope="unscoped")
    class Anchor(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0", scope=[DirectProperty(level="org", property_name="org_id")],
        owned={"secret": None},
    )
    class Target(OntologyObject):
        id: str = prop(primary_key=True)
        org_id: str
        secret: str | None = prop(default=None, sensitivity=Sensitivity(human_visible=False))

    link = ontology.link("reaches", Anchor, Target, "MANY_TO_MANY", identity_revealing=True)

    class Params(ActionParams):
        anchor_id: str = target(Anchor)

    @ontology.action(Params, target=Anchor, roles=["Member"], api_name="Walk")
    def walk(ctx: ActionContext, params: Params) -> dict[str, Any]:
        with count_reads(store) as calls:
            rows = ctx.traverse_many(link, iter([params.anchor_id, Anchor(id="b"), "a", "missing"]))
        assert_frontier_cost(calls, False)
        assert list(rows) == ["a", "b", "missing"] and rows["missing"] == []
        assert {anchor: [obj.model_dump() for obj in group] for anchor, group in rows.items()} == {
            anchor: [obj.model_dump() for obj in ctx.traverse(link, anchor)] for anchor in rows
        }
        first, second = rows["a"][0], rows["b"][0]
        assert first is not second
        assert first.secret == second.secret == "raw-secret"
        assert first.org_id == "foreign-org"
        with count_reads(store) as calls:
            backwards = ctx.traverse_many(link, iter([first, "t", "missing"]), reverse=True)
        assert_frontier_cost(calls, True)
        assert list(backwards) == ["t", "missing"]
        assert [obj.id for obj in backwards["t"]] == ["a", "b"]
        assert backwards["missing"] == []
        with count_reads(store) as calls:
            assert ctx.traverse_many(link, iter(())) == {}
            assert ctx.traverse_many(link, iter(()), reverse=True) == {}
        assert not calls
        first.secret = "saved"
        ctx.save(first)
        ctx.save(second)
        return {"targets": len(rows["a"])}

    ontology.validate()
    store = store_factory(ontology.registry)
    store.bind_clock(lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    with store.transaction():
        for obj_id in ("a", "b"):
            store.insert("Anchor", {"id": obj_id}, SRC)
        for obj_id in ("t", "retired"):
            store.insert("Target", {"id": obj_id, "org_id": "foreign-org", "secret": "raw-secret"}, SRC)
            store.create_link("reaches", "a", obj_id)
        store.create_link("reaches", "b", "t")
        store.retire_object("Target", "retired")
    client = OntologyRuntime(ontology, store).for_consumer(make_consumer())
    assert client.execute("Walk", {"anchor_id": "a"}) == {"targets": 1}
    assert store.read_current("Target", "t").payload["secret"] == "saved"
    assert store.audit_entries()[-1].outcome == "ok"
