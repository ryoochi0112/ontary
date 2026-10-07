"""`GuardedQuery.traverse_page`: a link traversal read one page at a time
(list-completeness #63/#64, T2).

The page keeps `limit` VISIBLE linked rows and peeks one more to set
`has_more`; hidden rows never count. The cursor is the object id of the last
kept row, and resuming from an id this consumer cannot resume from -- not
linked, made up, linked but invisible, or unlinked since the last page --
refuses with one identical `STALE_CURSOR` message, so `after=<guess>` is no
oracle for "X is linked here but hidden from you". Every case runs on every
store backend (Postgres joins when `ONTARY_TEST_POSTGRES_DSN` is set).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable

import pytest
from conftest import raises_code

from ontary.authoring import Ontology, OntologyObject, prop
from ontary.errors import ValidationFailed, VisibilityError
from ontary.meta import Sensitivity
from ontary.query import GuardedQuery, Page
from ontary.scope import DirectProperty
from ontary.security import Consumer
from ontary.store import Source, Store
from ontary.store.inmemory import InMemoryStore

SRC = Source(source_system="test")
POSTGRES_DSN = os.environ.get("ONTARY_TEST_POSTGRES_DSN")

StoreFactory = Callable[..., Store]

ontology = Ontology("traverse-paging", scope_levels=["team"], min_n=1)
TEAM = [DirectProperty(level="team", property_name="team_id")]


@ontology.object(layer="L0", scope=TEAM)
class Board(OntologyObject):
    id: str = prop(primary_key=True)
    team_id: str


@ontology.object(layer="L0", scope=TEAM)
class Ticket(OntologyObject):
    id: str = prop(primary_key=True)
    team_id: str
    secret: str | None = prop(default=None, sensitivity=Sensitivity(human_visible=False))


@ontology.object(layer="L0", scope=TEAM)
class Author(OntologyObject):
    id: str = prop(primary_key=True)
    team_id: str


# Reverse from a Board walks its tickets; forward from a Board walks the
# tickets it pins. Both directions therefore have many targets.
ontology.link("onBoard", Ticket, Board, "MANY_TO_ONE")
ontology.link("pins", Board, Ticket, "MANY_TO_MANY")
ontology.link("authoredBy", Ticket, Author, "MANY_TO_ONE", identity_revealing=True)
ontology.validate()

BOARD = "board-a"


def _human() -> Consumer:
    return Consumer(
        actor_id="reader", role="Reader", scope_level="team", scope_id="a", kind="human"
    )


BACKENDS = ["memory", "sqlite"] + (["postgres"] if POSTGRES_DSN else [])
# (link_type, reverse): both directions anchored at BOARD.
DIRECTIONS = [("onBoard", True), ("pins", False)]


@pytest.fixture(params=BACKENDS)
def store(request: pytest.FixtureRequest, make_store: StoreFactory) -> Store:
    registry = ontology.registry
    if request.param == "memory":
        return InMemoryStore(registry)
    if request.param == "sqlite":
        return make_store(registry)
    import psycopg

    assert POSTGRES_DSN is not None
    schema = f"ontary_traverse_paging_{uuid.uuid4().hex}"
    with psycopg.connect(POSTGRES_DSN, autocommit=True) as setup:
        setup.execute(f'CREATE SCHEMA "{schema}"')
    separator = "&" if "?" in POSTGRES_DSN else "?"
    postgres_store = make_store(
        registry,
        dsn=f"{POSTGRES_DSN}{separator}options=-csearch_path%3D{schema}",
        backend="postgres",
    )
    request.addfinalizer(lambda: _drop_schema(postgres_store, schema))
    return postgres_store


def _drop_schema(postgres_store: Store, schema: str) -> None:
    import psycopg

    from ontary.store.postgres import PostgresStore

    assert POSTGRES_DSN is not None
    assert isinstance(postgres_store, PostgresStore)
    postgres_store.close()
    with psycopg.connect(POSTGRES_DSN, autocommit=True) as setup:
        setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


class _Seed:
    """Insert tickets and link each one to BOARD in the direction under test."""

    def __init__(self, store: Store, link_type: str, reverse: bool) -> None:
        self.store = store
        self.link_type = link_type
        self.reverse = reverse
        self._n = 0
        store.insert("Board", {"id": BOARD, "team_id": "a"}, SRC)

    def tickets(self, count: int, *, team: str, linked: bool = True) -> list[str]:
        ids: list[str] = []
        for _ in range(count):
            ticket_id = f"t-{self._n:04d}"
            self._n += 1
            self.store.insert(
                "Ticket", {"id": ticket_id, "team_id": team, "secret": "s"}, SRC
            )
            if linked:
                self.link(ticket_id)
            ids.append(ticket_id)
        return ids

    def link(self, ticket_id: str) -> None:
        if self.reverse:
            self.store.create_link(self.link_type, ticket_id, BOARD)
        else:
            self.store.create_link(self.link_type, BOARD, ticket_id)

    def unlink(self, ticket_id: str) -> None:
        if self.reverse:
            assert self.store.close_link(self.link_type, ticket_id, BOARD)
        else:
            assert self.store.close_link(self.link_type, BOARD, ticket_id)


def _page(
    seed: _Seed, *, limit: int, after: str | None = None
) -> tuple[list[str], bool, str | None]:
    gq = GuardedQuery(seed.store, ontology.registry, ontology.definition.policy)
    page = gq.traverse_page(
        _human(), seed.link_type, BOARD, reverse=seed.reverse, limit=limit, after=after
    )
    assert isinstance(page, Page)
    for row in page.items:
        assert "secret" not in row.payload
    return [row.payload["id"] for row in page.items], page.has_more, page.next_cursor


def _traverse_ids(seed: _Seed) -> list[str]:
    gq = GuardedQuery(seed.store, ontology.registry, ontology.definition.policy)
    rows = gq.traverse(_human(), seed.link_type, BOARD, reverse=seed.reverse)
    return [row.payload["id"] for row in rows]


@pytest.mark.parametrize(("link_type", "reverse"), DIRECTIONS)
def test_250_linked_rows_chain_into_100_100_50_in_traverse_order(
    store: Store, link_type: str, reverse: bool
) -> None:
    seed = _Seed(store, link_type, reverse)
    visible = seed.tickets(250, team="a")

    first, more1, cursor1 = _page(seed, limit=100)
    second, more2, cursor2 = _page(seed, limit=100, after=cursor1)
    third, more3, cursor3 = _page(seed, limit=100, after=cursor2)

    assert [len(first), len(second), len(third)] == [100, 100, 50]
    assert (more1, more2, more3) == (True, True, False)
    assert cursor1 == first[-1]
    assert cursor2 == second[-1]
    assert cursor3 is None
    union = first + second + third
    assert len(union) == len(set(union))
    assert set(union) == set(visible)
    # The page walk keeps the store's link order: it equals `traverse`.
    assert union == _traverse_ids(seed)


@pytest.mark.parametrize(("link_type", "reverse"), DIRECTIONS)
def test_exactly_limit_linked_rows_has_no_more_and_no_cursor(
    store: Store, link_type: str, reverse: bool
) -> None:
    seed = _Seed(store, link_type, reverse)
    visible = seed.tickets(100, team="a")

    ids, has_more, cursor = _page(seed, limit=100)

    assert sorted(ids) == sorted(visible)
    assert has_more is False
    assert cursor is None


@pytest.mark.parametrize(("link_type", "reverse"), DIRECTIONS)
def test_hidden_linked_rows_never_count(
    store: Store, link_type: str, reverse: bool
) -> None:
    seed = _Seed(store, link_type, reverse)
    visible = seed.tickets(100, team="a")
    seed.tickets(50, team="b")

    ids, has_more, cursor = _page(seed, limit=100)

    assert sorted(ids) == sorted(visible)
    assert has_more is False
    assert cursor is None


@pytest.mark.parametrize(("link_type", "reverse"), DIRECTIONS)
def test_visible_row_behind_hidden_linked_rows_still_counts(
    store: Store, link_type: str, reverse: bool
) -> None:
    seed = _Seed(store, link_type, reverse)
    seed.tickets(3, team="a")
    seed.tickets(5, team="b")
    seed.tickets(1, team="a")

    ids, has_more, cursor = _page(seed, limit=3)
    assert has_more is True
    assert cursor == ids[-1]

    rest, rest_more, rest_cursor = _page(seed, limit=3, after=cursor)
    assert len(rest) == 1
    assert rest_more is False
    assert rest_cursor is None
    assert ids + rest == _traverse_ids(seed)


@pytest.mark.parametrize(("link_type", "reverse"), DIRECTIONS)
def test_every_unresumable_cursor_is_one_identical_stale_cursor(
    store: Store, link_type: str, reverse: bool
) -> None:
    seed = _Seed(store, link_type, reverse)
    seed.tickets(4, team="a")
    (unlinked,) = seed.tickets(1, team="a", linked=False)
    (hidden_linked,) = seed.tickets(1, team="b")

    _ids, has_more, cursor = _page(seed, limit=2)
    assert has_more is True
    assert cursor is not None
    seed.unlink(cursor)

    messages: dict[str, str] = {}
    for case, after in {
        "unlinked": unlinked,
        "made-up": "no-such-ticket",
        "linked-but-invisible": hidden_linked,
        "link-closed-since-last-page": cursor,
    }.items():
        with raises_code(ValidationFailed, "STALE_CURSOR") as excinfo:
            _page(seed, limit=2, after=after)
        messages[case] = str(excinfo.value)

    assert len(set(messages.values())) == 1, messages


def test_identity_revealing_link_denies_a_human_before_cursor_or_limit(
    store: Store,
) -> None:
    store.insert("Ticket", {"id": "t-1", "team_id": "a"}, SRC)
    store.insert("Author", {"id": "author-1", "team_id": "a"}, SRC)
    store.create_link("authoredBy", "t-1", "author-1")
    gq = GuardedQuery(store, ontology.registry, ontology.definition.policy)

    for reverse, anchor in [(False, "t-1"), (True, "author-1")]:
        with raises_code(VisibilityError, "VISIBILITY_DENIED"):
            gq.traverse_page(
                _human(),
                "authoredBy",
                anchor,
                reverse=reverse,
                limit=0,
                after="no-such-ticket",
            )


@pytest.mark.parametrize("limit", [0, -1])
def test_limit_below_one_is_invalid_limit(store: Store, limit: int) -> None:
    seed = _Seed(store, "onBoard", True)
    seed.tickets(1, team="a")

    with raises_code(ValidationFailed, "INVALID_LIMIT"):
        _page(seed, limit=limit)
