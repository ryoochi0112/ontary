"""`Page.has_more` / `TypedPage.has_more` and the tight `next_cursor`
(list-completeness #63/#64, T1).

`has_more` is true exactly when at least one more VISIBLE row follows the
page, and `next_cursor` is non-null exactly when `has_more` is true. The page
builders peek one visible row past `limit` to decide; rows hidden by scope
never count. Every case runs on the row-id path and the `order_by` path, on
both the string surface (`Page`) and the typed surface (`TypedPage`), and on
every store backend (Postgres joins when `ONTARY_TEST_POSTGRES_DSN` is set).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from ontary.authoring import Ontology, OntologyObject, prop
from ontary.client import OntologyClient
from ontary.meta import Sensitivity
from ontary.query import Page, TypedPage
from ontary.scope import DirectProperty
from ontary.security import Consumer
from ontary.store import Source, Store
from ontary.store.inmemory import InMemoryStore

SRC = Source(source_system="test")
POSTGRES_DSN = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
LIMIT = 5

StoreFactory = Callable[..., Store]

ontology = Ontology("page-has-more", scope_levels=["team"], min_n=1)


@ontology.object(layer="L0", scope=[DirectProperty(level="team", property_name="team_id")])
class Ticket(OntologyObject):
    id: str = prop(primary_key=True)
    team_id: str
    rank: int
    secret: str | None = prop(default=None, sensitivity=Sensitivity(human_visible=False))


ontology.validate()


def _consumer() -> Consumer:
    return Consumer(actor_id="reader", role="Reader", scope_level="team", scope_id="a", kind="human")


BACKENDS = ["memory", "sqlite"] + (["postgres"] if POSTGRES_DSN else [])
SURFACES = ["string", "typed"]
ORDERS = ["row-id", "order-by"]


@pytest.fixture(params=BACKENDS)
def store(request: pytest.FixtureRequest, make_store: StoreFactory) -> Store:
    registry = ontology.registry
    if request.param == "memory":
        return InMemoryStore(registry)
    if request.param == "sqlite":
        return make_store(registry)
    # A fresh schema per store, as in `test_store_conformance.py`, so the
    # test never depends on what else lives in the shared database.
    import psycopg

    assert POSTGRES_DSN is not None
    schema = f"ontary_page_has_more_{uuid.uuid4().hex}"
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
    # Close the store first: its connection holds the schema open.
    assert isinstance(postgres_store, PostgresStore)
    postgres_store.close()
    with psycopg.connect(POSTGRES_DSN, autocommit=True) as setup:
        setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


class _Rows:
    """Insert tickets with a strictly increasing `rank`, so insertion (row-id)
    order and `order_by=("rank", "asc")` order are the same order."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._rank = 0

    def add(self, count: int, *, team: str) -> list[str]:
        ids: list[str] = []
        for _ in range(count):
            object_id = f"t-{self._rank:04d}"
            self._store.insert(
                "Ticket",
                {"id": object_id, "team_id": team, "rank": self._rank, "secret": "s"},
                SRC,
            )
            ids.append(object_id)
            self._rank += 1
        return ids


def _read(
    store: Store, surface: str, order: str, limit: int, after: str | None = None
) -> tuple[list[str], bool, str | None]:
    client = OntologyClient(ontology.definition, store, _consumer())
    order_by: Any = ("rank", "asc") if order == "order-by" else None
    if surface == "typed":
        typed = client.list(Ticket, limit=limit, after=after, order_by=order_by)
        assert isinstance(typed, TypedPage)
        for ticket in typed.items:
            assert ticket.secret is None
            assert "secret" in ticket.redacted_fields
        return [t.id for t in typed.items], typed.has_more, typed.next_cursor
    page = client.list("Ticket", limit=limit, after=after, order_by=order_by)
    assert isinstance(page, Page)
    for row in page.items:
        assert "secret" not in row.payload
    return [r.payload["id"] for r in page.items], page.has_more, page.next_cursor


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("surface", SURFACES)
def test_exactly_limit_visible_rows_has_no_more_and_no_cursor(
    store: Store, surface: str, order: str
) -> None:
    visible = _Rows(store).add(LIMIT, team="a")

    ids, has_more, cursor = _read(store, surface, order, LIMIT)

    assert ids == visible
    assert has_more is False
    assert cursor is None


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("surface", SURFACES)
def test_limit_plus_one_visible_rows_has_more_and_one_row_follows(
    store: Store, surface: str, order: str
) -> None:
    visible = _Rows(store).add(LIMIT + 1, team="a")

    ids, has_more, cursor = _read(store, surface, order, LIMIT)
    assert ids == visible[:LIMIT]
    assert has_more is True
    assert cursor is not None

    rest, rest_more, rest_cursor = _read(store, surface, order, LIMIT, after=cursor)
    assert rest == visible[LIMIT:]
    assert rest_more is False
    assert rest_cursor is None


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("surface", SURFACES)
def test_hidden_rows_after_the_page_never_count(
    store: Store, surface: str, order: str
) -> None:
    rows = _Rows(store)
    visible = rows.add(LIMIT, team="a")
    rows.add(50, team="b")

    ids, has_more, cursor = _read(store, surface, order, LIMIT)

    assert ids == visible
    assert has_more is False
    assert cursor is None


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("surface", SURFACES)
def test_visible_row_behind_hidden_rows_still_counts(
    store: Store, surface: str, order: str
) -> None:
    rows = _Rows(store)
    visible = rows.add(LIMIT, team="a")
    rows.add(50, team="b")
    last = rows.add(1, team="a")

    ids, has_more, cursor = _read(store, surface, order, LIMIT)
    assert ids == visible
    assert has_more is True
    assert cursor is not None

    rest, rest_more, rest_cursor = _read(store, surface, order, LIMIT, after=cursor)
    assert rest == last
    assert rest_more is False
    assert rest_cursor is None


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("surface", SURFACES)
def test_chained_pages_over_250_rows_are_100_100_50(
    store: Store, surface: str, order: str
) -> None:
    rows = _Rows(store)
    visible: list[str] = []
    for _ in range(5):
        visible.extend(rows.add(50, team="a"))
        rows.add(3, team="b")

    sizes: list[int] = []
    flags: list[bool] = []
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        ids, has_more, cursor = _read(store, surface, order, 100, after=cursor)
        sizes.append(len(ids))
        flags.append(has_more)
        seen.extend(ids)
        assert (cursor is not None) is has_more
        if not has_more:
            break

    assert sizes == [100, 100, 50]
    assert flags == [True, True, False]
    assert len(seen) == len(set(seen))
    assert set(seen) == set(visible)
