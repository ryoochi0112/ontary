"""The string `ActionContext` members are deprecated (#41, PR 2 of 2).

Each string member still works but emits a `DeprecationWarning` that names
its typed replacement and the removal release (0.18.0). The typed members,
and `retire`'s own internal link cascade, emit nothing.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from ontary import ActionContext, Consumer, ObjectStore, Ontology, OntologyObject, Source, prop
from ontary.model import LinkHandle

ontology = Ontology("deprecated-ctx", scope_levels=["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Customer(OntologyObject):
    id: str = prop(primary_key=True)
    name: str


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Order(OntologyObject):
    id: str = prop(primary_key=True)
    status: str


orderOf: LinkHandle[Order, Customer] = ontology.link(
    "orderOf", Order, Customer, "MANY_TO_ONE", owned=True
)

ontology.validate()


@pytest.fixture
def ctx() -> Iterator[ActionContext]:
    store = ObjectStore(ontology.registry)
    seed = Source(source_system="seed")
    store.insert("Customer", {"id": "c1", "name": "Ada"}, seed)
    store.insert("Order", {"id": "o1", "status": "new"}, seed)
    store.create_link("orderOf", "o1", "c1")
    consumer = Consumer(
        actor_id="clerk-1", role="Clerk", scope_level="org", scope_id="org-1", kind="human"
    )
    with store.transaction():
        yield ActionContext(
            store,
            Source(source_system="action:Test"),
            consumer,
            registry=ontology.registry,
            id_factory=lambda: "gen-1",
        )


_DEPRECATED: list[tuple[str, Callable[[ActionContext], Any], str]] = [
    ("insert", lambda c: c.insert("Order", {"status": "new"}), "create"),
    ("update", lambda c: c.update("Order", "o1", {"status": "shipped"}), "save"),
    ("create_link", lambda c: c.create_link("orderOf", "o1", "c1"), "link"),
    ("read_current", lambda c: c.read_current("Order", "o1"), "get"),
    ("read_all", lambda c: c.read_all("Order"), "all"),
    ("links_from", lambda c: c.links_from("orderOf", "o1"), "traverse"),
    ("links_to", lambda c: c.links_to("orderOf", "c1"), "traverse"),
    ("unlink", lambda c: c.unlink("orderOf", "o1", "c1"), "unlink"),
    ("retire", lambda c: c.retire("Order", "o1"), "retire"),
]


@pytest.mark.parametrize(
    ("member", "call", "replacement"), _DEPRECATED, ids=[row[0] for row in _DEPRECATED]
)
def test_string_member_warns_once_and_names_its_replacement(
    ctx: ActionContext,
    member: str,
    call: Callable[[ActionContext], Any],
    replacement: str,
) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        call(ctx)

    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    # One warning per call: `retire`'s cascade closes `orderOf` through the
    # internal path, which must not add a second warning.
    assert len(deprecations) == 1
    message = str(deprecations[0].message)
    assert f"ActionContext.{member}(" in message
    assert f"ctx.{replacement}(" in message
    assert "0.18.0" in message
    # stacklevel points at the handler's own call, not at ontary internals.
    assert deprecations[0].filename == __file__


def test_string_members_still_work(ctx: ActionContext) -> None:
    with pytest.warns(DeprecationWarning):
        new_id = ctx.insert("Order", {"status": "new"})
    with pytest.warns(DeprecationWarning):
        stored = ctx.read_current("Order", new_id)
    assert new_id == "gen-1"
    assert stored is not None
    assert stored.payload["status"] == "new"


def test_typed_members_do_not_warn(ctx: ActionContext) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        order = ctx.get(Order, "o1")
        assert order is not None
        ctx.all(Order)
        customers = ctx.traverse(orderOf, order)
        ctx.traverse(orderOf, customers[0], reverse=True)
        order.status = "shipped"
        ctx.save(order)
        created = ctx.create(Order, status="new")
        ctx.link(orderOf, created, "c1")
        ctx.unlink(orderOf, created, "c1")
        ctx.retire(order)  # cascades through the internal unlink path
        ctx.retire(Order, created.id)
