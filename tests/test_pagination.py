"""Tests for pagination-hardening T2: page-filling `GuardedQuery.get_objects`
(spec §3 AC3/AC4/AC5/AC6/AC7/AC8, §6 "Page filling") and the `OntologyClient.
list` typed/string surfaces built on top of it.

The toy ontology is a "library" domain (Library/Shelf/Widget), matching the
shape of `test_query.py`'s Library/Shelf/Book: `Widget` is scoped by its
shelf (`DirectProperty`), and a shelf resolves up to its library via
`ViaLink` -- so a shelf-scoped ("narrow") consumer sees only its own
shelf's widgets, and a library-scoped ("broad") consumer sees every widget
across every shelf in that library (hierarchy climbing, same mechanism
`test_query.py` exercises for `aggregate`). That asymmetry is exactly what
the AC8 test below needs: two consumers with very different amounts of
data hidden from them, whose pages must still be structurally identical.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from conftest import raises_code

import ontary.query as query_module
from ontary.authoring import Ontology, OntologyObject, prop
from ontary.client import OntologyClient
from ontary.errors import ValidationFailed
from ontary.meta import (
    Cardinality,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
    Sensitivity,
)
from ontary.query import GuardedQuery, Page, TypedPage
from ontary.scope import DirectProperty, ScopePolicy, SelfScope, ViaLink
from ontary.security import Consumer
from ontary.store import Source, Store
from ontary.store.inmemory import InMemoryStore

SRC = Source(source_system="test")
LEVELS = ["shelf", "library"]


RegistryFactory = Callable[..., OntologyRegistry]
PolicyFactory = Callable[..., ScopePolicy]
StoreFactory = Callable[..., Store]


# -- toy ontology: Library / Shelf / Widget ---------------------------------


def _pagination_registry(make_registry: RegistryFactory) -> OntologyRegistry:
    registry = make_registry()
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Library",
            display_name="Library",
            description="A library building",
            layer="L0",
            properties=[PropertyDef(name="id", type="str")],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Shelf",
            display_name="Shelf",
            description="A shelf inside a library",
            layer="L0",
            properties=[PropertyDef(name="id", type="str")],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Widget",
            display_name="Widget",
            description="A paginated leaf object, scoped by its shelf",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="shelf_id", type="str", required=False),
                PropertyDef(name="category", type="str", required=False),
            ],
            primary_key="id",
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="inLibrary",
            from_type="Shelf",
            to_type="Library",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Shelf -> its library",
        )
    )
    registry.validate()
    return registry


def _pagination_policy(make_policy: PolicyFactory) -> ScopePolicy:
    return make_policy(
        levels=LEVELS,
        rules={
            "Library": [SelfScope(level="library")],
            "Shelf": [
                SelfScope(level="shelf"),
                ViaLink(
                    link_api_name="inLibrary", direction="from", parent_type="Library"
                ),
            ],
            "Widget": [DirectProperty(level="shelf", property_name="shelf_id")],
        },
        min_n=1,
    )

def _narrow(scope_id: str = "shelf-vis") -> Consumer:
    return Consumer(
        actor_id="u-narrow", role="Member", scope_level="shelf", scope_id=scope_id, kind="human"
    )


def _broad(scope_id: str = "lib-1") -> Consumer:
    return Consumer(
        actor_id="u-broad", role="Member", scope_level="library", scope_id=scope_id, kind="human"
    )


def _seed_library(
    store: Store,
    hidden_shelves: int,
    library_id: str = "lib-1",
    visible_shelf_id: str = "shelf-vis",
) -> None:
    """One library, one visible shelf (narrow consumer's own shelf), and
    `hidden_shelves` OTHER shelves in the SAME library (invisible to the
    narrow consumer, but visible to a library-scoped broad consumer)."""
    store.insert("Library", {"id": library_id}, SRC)
    store.insert("Shelf", {"id": visible_shelf_id}, SRC)
    store.create_link("inLibrary", visible_shelf_id, library_id)
    for i in range(hidden_shelves):
        shelf_id = f"shelf-hidden-{i}"
        store.insert("Shelf", {"id": shelf_id}, SRC)
        store.create_link("inLibrary", shelf_id, library_id)


def _insert_hidden_widgets(store: Store, count: int) -> list[str]:
    """One widget per hidden shelf (`_seed_library` must have created at
    least `count` hidden shelves already)."""
    ids = []
    for i in range(count):
        wid = f"w-hidden-{i}"
        store.insert(
            "Widget", {"id": wid, "shelf_id": f"shelf-hidden-{i}", "category": "a"}, SRC
        )
        ids.append(wid)
    return ids


def _insert_visible_widgets(
    store: Store, count: int, shelf_id: str = "shelf-vis", category: str = "a"
) -> list[str]:
    ids = []
    for i in range(count):
        wid = f"w-vis-{i}"
        store.insert(
            "Widget", {"id": wid, "shelf_id": shelf_id, "category": category}, SRC
        )
        ids.append(wid)
    return ids


class _CallCountingStore:
    """Wraps a `Store`, counting (and optionally bounding) the number of
    `read_page` calls a single walk makes -- the regression pin spec §11
    asks for: a fill-loop bug that never terminates on a short store batch
    (e.g. looping on "page not yet full" instead) trips the `max_calls`
    assertion below and fails FAST, instead of hanging CI. Delegates every
    other `Store` member to the wrapped instance unchanged."""

    def __init__(self, inner: Store, max_calls: int | None = None) -> None:
        self._inner = inner
        self._max_calls = max_calls
        self.read_page_calls = 0

    def read_page(
        self, obj_type: str, after_key: str | None = None, batch: int = 500
    ) -> Any:
        self.read_page_calls += 1
        if self._max_calls is not None:
            assert self.read_page_calls <= self._max_calls, (
                "GuardedQuery.get_objects's fill loop exceeded the expected "
                f"read_page call bound ({self._max_calls}) -- regression pin "
                "for spec pagination-hardening §11's fill-loop termination "
                "risk: the loop must exit on a short store batch (exhausted), "
                "never on 'page not yet full'"
            )
        return self._inner.read_page(obj_type, after_key=after_key, batch=batch)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# -- AC3: exact-limit pages, incl. a large invisible prefix -----------------


def test_page_exactly_limit_with_large_invisible_prefix_spanning_batches(
    monkeypatch: pytest.MonkeyPatch,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """A page holds EXACTLY `limit` items whenever that many visible rows
    remain -- even behind a 200-row invisible prefix that spans several
    store batches. `DEFAULT_BATCH` is patched down to 20 so the prefix
    genuinely forces > 1 `read_page` round-trip before the fill loop
    reaches the first visible row (spec §6/AC3)."""
    monkeypatch.setattr(query_module, "DEFAULT_BATCH", 20)
    registry = _pagination_registry(make_registry)
    raw_store = make_store(registry)
    _seed_library(raw_store, hidden_shelves=200)
    _insert_hidden_widgets(raw_store, count=200)
    visible_ids = _insert_visible_widgets(raw_store, count=20)
    # Bounded (spec §11's fill-loop termination risk): every fill-loop test
    # in this module wraps its store in `_CallCountingStore` with a
    # `max_calls` bound, so a regression that turns "store exhausted" into
    # an infinite loop fails FAST here instead of hanging the whole suite.
    counting_store = _CallCountingStore(raw_store, max_calls=50)
    gq = GuardedQuery(counting_store, registry, _pagination_policy(make_policy))

    page = gq.get_objects(_narrow(), "Widget", limit=5)

    assert isinstance(page, Page)
    assert [w.payload["id"] for w in page.items] == visible_ids[:5]
    assert page.next_cursor is not None
    # The 200-row hidden prefix, batched at 20/call, forces multiple
    # read_page round-trips before the fill loop even reaches a visible row.
    assert counting_store.read_page_calls > 1


# -- AC4: cursor round-trip, no gaps, no repeats -----------------------------


def test_cursor_round_trip_matches_unpaginated_no_gaps_no_repeats(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Concatenating every page (via `next_cursor` round-tripped as
    `after`) reproduces the unpaginated visible list exactly, in order,
    with no gaps and no repeats (quiescent store -- spec AC4/§8).

    Bound via `_CallCountingStore` (spec §11's fill-loop termination risk):
    this test drives its OWN outer `pages < 100` bound on the number of
    `gq.get_objects` CALLS, but that does nothing to stop a single call's
    INNER `read_page` fill loop from spinning forever if the short-batch
    exit is ever deleted -- a raw, unbounded store here would hang the
    whole suite before the outer bound is ever reached."""
    registry = _pagination_registry(make_registry)
    raw_store = make_store(registry)
    store = _CallCountingStore(raw_store, max_calls=200)
    _seed_library(store, hidden_shelves=30)
    visible_ids: list[str] = []
    for i in range(30):
        store.insert(
            "Widget", {"id": f"w-hidden-{i}", "shelf_id": f"shelf-hidden-{i}", "category": "a"}, SRC
        )
        if i % 2 == 0:
            wid = f"w-vis-{i}"
            store.insert("Widget", {"id": wid, "shelf_id": "shelf-vis", "category": "a"}, SRC)
            visible_ids.append(wid)
    gq = GuardedQuery(store, registry, _pagination_policy(make_policy))
    consumer = _narrow()

    unpaginated = gq.get_objects(consumer, "Widget")
    assert [w.payload["id"] for w in unpaginated] == visible_ids

    walked: list[str] = []
    cursor: str | None = None
    limit = 4  # does not evenly divide len(visible_ids) == 15
    pages = 0
    while True:
        page = gq.get_objects(consumer, "Widget", limit=limit, after=cursor)
        walked.extend(w.payload["id"] for w in page.items)
        pages += 1
        assert pages < 100, "cursor walk did not converge -- possible infinite loop"
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert walked == visible_ids
    assert len(walked) == len(set(walked))
    assert pages > 1


# -- short page / all-invisible tail termination -----------------------------


def test_short_page_none_cursor_and_all_invisible_tail_terminates(
    monkeypatch: pytest.MonkeyPatch,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Fewer visible rows than `limit` remain -> `next_cursor is None`
    (never a non-None cursor on a short page, spec §8). An all-invisible
    TAIL (200 hidden rows AFTER the visible ones) must terminate on the
    store's own short-batch signal, never loop 'until the page is full'
    (spec §11's named risk) -- `_CallCountingStore`'s bound turns a
    regression into a fast `AssertionError` instead of a CI hang."""
    monkeypatch.setattr(query_module, "DEFAULT_BATCH", 20)
    registry = _pagination_registry(make_registry)
    raw_store = make_store(registry)
    _seed_library(raw_store, hidden_shelves=200)
    visible_ids = _insert_visible_widgets(raw_store, count=20)
    _insert_hidden_widgets(raw_store, count=200)
    # Generous but finite: real termination needs ~10-15 read_page calls
    # here; a true infinite loop would blow past this in one test instead
    # of hanging the whole suite.
    counting_store = _CallCountingStore(raw_store, max_calls=50)
    gq = GuardedQuery(
        counting_store, registry, _pagination_policy(make_policy)
    )

    page = gq.get_objects(_narrow(), "Widget", limit=25)  # > 20 visible rows

    assert [w.payload["id"] for w in page.items] == visible_ids
    assert page.next_cursor is None


# -- AC6: limit < 1 --------------------------------------------------------


def test_limit_below_one_raises_invalid_limit(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _pagination_registry(make_registry)
    store = make_store(registry)
    _seed_library(store, hidden_shelves=0)
    _insert_visible_widgets(store, count=3)
    gq = GuardedQuery(store, registry, _pagination_policy(make_policy))

    with raises_code(ValidationFailed, "INVALID_LIMIT"):
        gq.get_objects(_narrow(), "Widget", limit=0)

    with raises_code(ValidationFailed, "INVALID_LIMIT"):
        gq.get_objects(_narrow(), "Widget", limit=-1)


# -- limit >= remaining rows -------------------------------------------------


def test_limit_at_least_remaining_returns_one_full_page_and_none_cursor(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """A short page (`limit` exceeds the remaining visible rows) is exactly
    the fill-loop's exhaustion exit -- bound via `_CallCountingStore` (spec
    §11) so a regression that deletes it fails fast instead of hanging."""
    registry = _pagination_registry(make_registry)
    raw_store = make_store(registry)
    _seed_library(raw_store, hidden_shelves=5)
    _insert_hidden_widgets(raw_store, count=5)
    visible_ids = _insert_visible_widgets(raw_store, count=3)
    store = _CallCountingStore(raw_store, max_calls=50)
    gq = GuardedQuery(store, registry, _pagination_policy(make_policy))

    page = gq.get_objects(_narrow(), "Widget", limit=10)

    assert [w.payload["id"] for w in page.items] == visible_ids
    assert page.next_cursor is None


# -- AC8: narrow vs. broad consumer pages are structurally indistinguishable


def test_narrow_and_broad_consumer_pages_structurally_indistinguishable(
    monkeypatch: pytest.MonkeyPatch,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """A narrow (shelf-scoped) consumer has 200 OTHER shelves' widgets
    hidden from it; a broad (library-scoped) consumer sees all of them
    (hierarchy climbing -- same library). Both still get a `limit`-sized
    page with the SAME item count, and `Page`/`TypedPage` carry no
    count/gap field by construction (see `Page`'s fields) -- there is
    nothing in the page shape for either consumer to inspect.

    The cursor VALUE is not evidence either -- but for the OPPOSITE reason
    an earlier revision of this docstring claimed (spec §5's T2 amendment,
    a reviewer-falsified claim this replaces): it is NOT because the row
    identity is "a single global sequence... comparing the two values
    could never recover" anything -- a reviewer proved exactly that
    recovery IS possible from a decimal row-id cursor (subtract two of a
    consumer's own consecutive cursors and the hidden-row count falls out
    exactly; see `test_ac8_cursor_is_not_parseable_as_a_row_count` below).
    The cursor is safe here because it is no longer the row identity at
    all: it is a random, per-row PAGE TOKEN with no arithmetic relationship
    to row identity, position, or write volume -- there is nothing for
    either consumer to compare.
    """
    monkeypatch.setattr(query_module, "DEFAULT_BATCH", 20)
    registry = _pagination_registry(make_registry)
    raw_store = make_store(registry)
    _seed_library(raw_store, hidden_shelves=200)
    _insert_hidden_widgets(raw_store, count=200)
    _insert_visible_widgets(raw_store, count=20)
    store = _CallCountingStore(raw_store, max_calls=50)
    gq = GuardedQuery(store, registry, _pagination_policy(make_policy))

    narrow_page = gq.get_objects(_narrow(), "Widget", limit=5)
    broad_page = gq.get_objects(_broad(), "Widget", limit=5)

    assert len(narrow_page.items) == len(broad_page.items) == 5
    assert narrow_page.next_cursor is not None
    assert broad_page.next_cursor is not None
    assert set(Page.model_fields) == {"items", "next_cursor"}


def test_ac8_cursor_is_not_parseable_as_a_row_count(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """THE regression pin for the P0 this task closes (spec §5's T2
    amendment): before the fix, `next_cursor` was a DECIMAL row id, so a
    narrow consumer could subtract two of its OWN consecutive cursors and
    recover exactly how many rows its scope hid -- proven on real data:
    cursors `['23', '27', '35', '37']` over the consumer's own visible rows
    yield hidden counts `[3, 7, 1]` by simple subtraction, and a page whose
    cursor advanced by 20 while only 3 items were kept reveals 17 hidden
    rows. This test reproduces that exact shape -- a SECRET, VARYING number
    of hidden widgets between each visible one -- and asserts every cursor
    the narrow consumer receives is NOT parseable as an int at all, so the
    subtraction the old design permitted cannot even be attempted.

    Mutating `PagedRow.key`/`next_cursor` back to a decimal row id (the
    pre-T2 design) makes this test FAIL immediately: `int(cursor)` would
    succeed for every cursor instead of raising.
    """
    registry = _pagination_registry(make_registry)
    store = make_store(registry)
    _seed_library(store, hidden_shelves=40)
    hidden_counts = [3, 7, 1, 5, 2]  # secret to the narrow consumer
    visible_ids: list[str] = []
    hidden_index = 0
    for i, gap in enumerate(hidden_counts):
        for _ in range(gap):
            store.insert(
                "Widget",
                {
                    "id": f"w-hidden-{hidden_index}",
                    "shelf_id": f"shelf-hidden-{hidden_index % 40}",
                    "category": "a",
                },
                SRC,
            )
            hidden_index += 1
        wid = f"w-vis-{i}"
        store.insert(
            "Widget", {"id": wid, "shelf_id": "shelf-vis", "category": "a"}, SRC
        )
        visible_ids.append(wid)

    gq = GuardedQuery(store, registry, _pagination_policy(make_policy))
    consumer = _narrow()

    cursors: list[str] = []
    cursor: str | None = None
    for _ in range(len(visible_ids)):
        page = gq.get_objects(consumer, "Widget", limit=1, after=cursor)
        assert [w.payload["id"] for w in page.items] == [visible_ids[len(cursors)]]
        cursor = page.next_cursor
        assert cursor is not None
        cursors.append(cursor)

    for c in cursors:
        with pytest.raises(ValueError):
            int(c)


# -- where= composes with pagination -----------------------------------------


def test_where_filter_composes_with_pagination(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """`where=` is applied BEFORE a row counts toward `limit` (same
    filter-then-count order as the unpaginated path). The second call below
    is a short page (only one `category=a` row remains) -- exactly the
    fill-loop's exhaustion exit -- so the store is bound via
    `_CallCountingStore` (spec §11)."""
    registry = _pagination_registry(make_registry)
    raw_store = make_store(registry)
    _seed_library(raw_store, hidden_shelves=0)
    for i in range(6):
        raw_store.insert(
            "Widget",
            {"id": f"w-{i}", "shelf_id": "shelf-vis", "category": "a" if i % 2 == 0 else "b"},
            SRC,
        )
    store = _CallCountingStore(raw_store, max_calls=50)
    gq = GuardedQuery(store, registry, _pagination_policy(make_policy))

    page = gq.get_objects(_narrow(), "Widget", where={"category": "a"}, limit=2)

    assert [w.payload["id"] for w in page.items] == ["w-0", "w-2"]
    assert all(w.payload["category"] == "a" for w in page.items)
    assert page.next_cursor is not None

    page2 = gq.get_objects(
        _narrow(), "Widget", where={"category": "a"}, limit=2, after=page.next_cursor
    )
    assert [w.payload["id"] for w in page2.items] == ["w-4"]
    assert page2.next_cursor is None


# -- AC5: limit=None is unchanged --------------------------------------------


def test_limit_none_returns_exactly_todays_list_type_and_contents(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _pagination_registry(make_registry)
    store = make_store(registry)
    _seed_library(store, hidden_shelves=2)
    _insert_hidden_widgets(store, count=2)
    visible_ids = _insert_visible_widgets(store, count=4)
    gq = GuardedQuery(store, registry, _pagination_policy(make_policy))

    result = gq.get_objects(_narrow(), "Widget")

    assert isinstance(result, list)
    assert not isinstance(result, Page)
    assert [w.payload["id"] for w in result] == visible_ids


# -- invalid after= propagates unwrapped -------------------------------------


def test_invalid_after_cursor_propagates_uncaught(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _pagination_registry(make_registry)
    store = make_store(registry)
    _seed_library(store, hidden_shelves=0)
    _insert_visible_widgets(store, count=3)
    gq = GuardedQuery(store, registry, _pagination_policy(make_policy))

    with raises_code(ValidationFailed, "INVALID_CURSOR"):
        gq.get_objects(_narrow(), "Widget", limit=2, after="not-an-integer")


# -- P1: after= without limit= is a coded refusal, not a silent full re-read -


def test_after_without_limit_raises_coded_error_on_guarded_query(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """A resume loop that lost track of its own `limit` (a caller bug) must
    not silently re-read every visible row from scratch just because
    `after` alone was passed with no `limit` (T2 review P1) -- that would
    duplicate whatever work the caller already did with the rows it
    walked. Even a GARBAGE cursor value must trip this the same way,
    proving it's checked before the cursor is ever resolved against the
    store."""
    registry = _pagination_registry(make_registry)
    store = make_store(registry)
    _seed_library(store, hidden_shelves=0)
    _insert_visible_widgets(store, count=3)
    gq = GuardedQuery(store, registry, _pagination_policy(make_policy))

    with raises_code(ValidationFailed, "AFTER_WITHOUT_LIMIT"):
        gq.get_objects(_narrow(), "Widget", after="some-cursor")

    with raises_code(ValidationFailed, "AFTER_WITHOUT_LIMIT"):
        gq.get_objects(_narrow(), "Widget", after="not-even-a-real-token")


def test_after_without_limit_raises_on_client_list_string_and_typed() -> None:
    """`OntologyClient.list` must forward `after` through to
    `GuardedQuery.get_objects` on EVERY path -- including `limit is None`
    -- so this refusal cannot be silently dropped by a surface that never
    even passes the argument along."""
    consumer = Consumer(
        actor_id="u1", role="Member", scope_level="team", scope_id="t1", kind="human"
    )
    client, Widget = _typed_client(consumer)

    with raises_code(ValidationFailed, "AFTER_WITHOUT_LIMIT"):
        client.list("Widget", after="some-cursor")

    with raises_code(ValidationFailed, "AFTER_WITHOUT_LIMIT"):
        client.list(Widget, after="some-cursor")


# -- AC4: exact-multiple boundary (limit divides visible rows evenly) -------


def test_exact_multiple_limit_yields_cursor_then_one_empty_final_page(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """4 visible rows with `limit=4`: the page fills EXACTLY at the last
    row, so it still carries a non-`None` cursor (spec AC4 -- a full page
    is never distinguishable from a page that merely happens to end at a
    batch boundary), and the follow-up call with that cursor returns one
    EMPTY final page with `next_cursor is None`, not an error and not a
    non-empty page. The follow-up call is a short (zero-row) page --
    exactly the fill-loop's exhaustion exit -- so the store is bound via
    `_CallCountingStore` (spec §11)."""
    registry = _pagination_registry(make_registry)
    raw_store = make_store(registry)
    _seed_library(raw_store, hidden_shelves=0)
    visible_ids = _insert_visible_widgets(raw_store, count=4)
    store = _CallCountingStore(raw_store, max_calls=50)
    gq = GuardedQuery(store, registry, _pagination_policy(make_policy))

    page = gq.get_objects(_narrow(), "Widget", limit=4)
    assert [w.payload["id"] for w in page.items] == visible_ids
    assert page.next_cursor is not None

    final_page = gq.get_objects(_narrow(), "Widget", limit=4, after=page.next_cursor)
    assert final_page.items == []
    assert final_page.next_cursor is None


# -- OntologyClient.list: string + typed surfaces ----------------------------


def _build_typed_ontology() -> tuple[Ontology, type[OntologyObject]]:
    ontology = Ontology(name="pagination-typed", scope_levels=["team"])

    @ontology.object(layer="L0", scope="unscoped")
    class Widget(OntologyObject):
        id: str = prop(primary_key=True)
        category: str
        secret: str | None = prop(
            default=None, sensitivity=Sensitivity(human_visible=False)
        )

    return ontology, Widget


def _typed_client(consumer: Consumer) -> tuple[OntologyClient, type[OntologyObject]]:
    """`OntologyClient` built on a `_CallCountingStore`-wrapped
    `InMemoryStore` (spec §11's fill-loop termination risk): the typed
    client test below drives a short (exhausted) final page, exactly the
    fill loop's exhaustion exit, so every test built on this helper is
    bounded against a regression turning that into an infinite loop."""
    ontology, Widget = _build_typed_ontology()
    definition = ontology.definition
    definition.validate()
    store: Any = _CallCountingStore(InMemoryStore(definition.registry), max_calls=50)
    client = OntologyClient(definition, store, consumer)
    for i in range(12):
        client.ingest(
            "Widget",
            [{"id": f"w-{i}", "category": "a", "secret": "s"}],
            SRC,
        )
    return client, Widget


def test_client_typed_list_returns_typed_page_with_hydrated_redacted_fields() -> None:
    consumer = Consumer(
        actor_id="u1", role="Member", scope_level="team", scope_id="t1", kind="human"
    )
    client, Widget = _typed_client(consumer)

    page = client.list(Widget, limit=5)

    assert isinstance(page, TypedPage)
    assert len(page.items) == 5
    assert all(isinstance(w, Widget) for w in page.items)
    assert all(w.secret is None for w in page.items)
    assert all("secret" in w.redacted_fields for w in page.items)
    assert page.next_cursor is not None

    rest = client.list(Widget, limit=100, after=page.next_cursor)
    assert isinstance(rest, TypedPage)
    assert len(rest.items) == 7
    assert rest.next_cursor is None


def test_client_string_list_returns_page_of_stored_object() -> None:
    consumer = Consumer(
        actor_id="u1", role="Member", scope_level="team", scope_id="t1", kind="human"
    )
    client, _Widget = _typed_client(consumer)

    page = client.list("Widget", limit=5)

    assert isinstance(page, Page)
    assert len(page.items) == 5
    assert all("secret" not in w.payload for w in page.items)
    assert page.next_cursor is not None


def test_client_list_limit_none_unchanged() -> None:
    consumer = Consumer(
        actor_id="u1", role="Member", scope_level="team", scope_id="t1", kind="human"
    )
    client, Widget = _typed_client(consumer)

    typed = client.list(Widget)
    assert isinstance(typed, list)
    assert len(typed) == 12

    stringly = client.list("Widget")
    assert isinstance(stringly, list)
    assert len(stringly) == 12
