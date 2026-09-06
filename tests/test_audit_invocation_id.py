"""Every audit entry one `execute()` writes carries the same invocation id.

The problem this solves: entries were matched only by field values and append
order. Re-enter the SAME action with the SAME params and the rows become
indistinguishable, so a reader cannot tell which attempt was the one that
failed.

An id minted per `execute()` call and stamped on every entry that call writes
makes the correlation a value rather than an inference.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import raises_code

from ontary import (
    ActionContext,
    ActionError,
    ActionParams,
    Consumer,
    ObjectStore,
    Ontology,
    OntologyObject,
    SelfScope,
    Source,
    prop,
    target,
)
from ontary.client import OntologyClient
from ontary.errors import PermissionDenied


def _build() -> tuple[Ontology, Any, Any]:
    ontology = Ontology(name="inv", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope=[SelfScope(level="org")], owned=True)
    class Org(OntologyObject):
        id: str = prop(primary_key=True)
        name: str
        touched: bool | None = None

    class TouchParams(ActionParams):
        org_id: str = target(Org)

    @ontology.action(TouchParams, target=Org, roles=["Admin"], api_name="Touch")
    def _touch(ctx: ActionContext, params: TouchParams) -> dict[str, str]:
        if ctx.read_current("Org", params.org_id) is None:
            raise ActionError("no such org", code="PRECONDITION_FAILED")
        ctx.update("Org", params.org_id, {"touched": True})
        return {"org_id": params.org_id}

    class FailParams(ActionParams):
        org_id: str = target(Org)

    @ontology.action(FailParams, target=Org, roles=["Admin"], api_name="Fail")
    def _fail(ctx: ActionContext, params: FailParams) -> dict[str, str]:
        raise ActionError("always fails", code="PRECONDITION_FAILED")

    ontology.validate()
    return ontology, Org, (TouchParams, FailParams)


def _client(ontology: Ontology, store: Any, handles: Any, role: str = "Admin") -> OntologyClient:
    return OntologyClient(
        ontology,
        store,
        Consumer(actor_id="a1", role=role, scope_level="org", scope_id="org-1", kind="human"),
    )


@pytest.fixture
def world() -> tuple[Ontology, Any, Any]:
    ontology, _org, handles = _build()
    store = ObjectStore(ontology.registry)
    store.insert("Org", {"id": "org-1", "name": "Acme"}, Source(source_system="t"))
    return ontology, store, handles


def test_two_identical_calls_are_distinguishable(world: Any) -> None:
    """The motivating case. Same action, same params, twice -- previously the
    two rows were indistinguishable, so a reader could not say which attempt a
    given row belonged to."""
    ontology, store, handles = world
    client = _client(ontology, store, handles)
    touch_params = handles[0]

    client.execute(touch_params(org_id="org-1"))
    client.execute(touch_params(org_id="org-1"))

    entries = store.audit_entries()
    assert len(entries) == 2
    first, second = entries[0].invocation_id, entries[1].invocation_id

    assert first is not None and second is not None
    assert first != second, "two calls must not share an id"

    # Everything else about the two rows is identical, which is precisely
    # why field-matching could not tell them apart.
    assert len({(e.action, e.outcome, tuple(sorted(e.params))) for e in entries}) == 1


def test_denied_attempt_is_stamped(world: Any) -> None:
    ontology, store, handles = world
    client = _client(ontology, store, handles, role="Intern")
    touch_params = handles[0]

    with raises_code(PermissionDenied, "PERMISSION_DENIED"):
        client.execute(touch_params(org_id="org-1"))

    entries = store.audit_entries()
    assert [e.outcome for e in entries] == ["denied"]
    assert entries[0].invocation_id is not None


def test_failed_precondition_is_stamped(world: Any) -> None:
    ontology, store, handles = world
    client = _client(ontology, store, handles)
    fail_params = handles[1]

    with pytest.raises(ActionError):
        client.execute(fail_params(org_id="org-1"))

    entries = store.audit_entries()
    assert [e.outcome for e in entries] == ["error"]
    assert entries[0].invocation_id is not None


def test_ids_are_unique_across_outcomes(world: Any) -> None:
    """A denial, a failure and a success in one process must not collide."""
    ontology, store, handles = world
    touch_params, fail_params = handles[0], handles[1]

    with raises_code(PermissionDenied, "PERMISSION_DENIED"):
        _client(ontology, store, handles, role="Intern").execute(touch_params(org_id="org-1"))
    with pytest.raises(ActionError):
        _client(ontology, store, handles).execute(fail_params(org_id="org-1"))
    _client(ontology, store, handles).execute(touch_params(org_id="org-1"))

    ids = [e.invocation_id for e in store.audit_entries()]
    assert all(i is not None for i in ids)
    # denied, error, ok -- three calls, three distinct ids.
    assert len(set(ids)) == 3
