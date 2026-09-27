"""Actions may target `scope="unscoped"` types (#35).

An unscoped type owns no scope, so before #35 every scope level resolved to
`None` and the action gate denied every consumer with `SCOPE_DENIED`:
reference data (products, workers, customers) could never be a business
action's target. Now a `target` parameter on an unscoped type skips the
scope gate, the action's `roles=` is its only gate, and the audit entry
names the parameter in `unscoped_params`. A `scope` parameter on an unscoped
type is refused when the policy is validated (`SCOPE_POLICY_ERROR`).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from conftest import raises_code

from ontary import (
    ActionContext,
    ActionParams,
    Consumer,
    Ontology,
    OntologyObject,
    SelfScope,
    Source,
    prop,
    scope_ref,
    target,
)
from ontary.client import OntologyClient
from ontary.errors import PermissionDenied, ValidationFailed
from ontary.store import Store

ConsumerFactory = Callable[..., Consumer]
StoreFactory = Callable[..., Store]


def _ontology() -> Ontology:
    ontology = Ontology("unscoped-targets", scope_levels=["team"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Product(OntologyObject):
        id: str = prop(primary_key=True)
        name: str

    @ontology.object(layer="L0", scope=[SelfScope(level="team")], owned=True)
    class Team(OntologyObject):
        id: str = prop(primary_key=True)
        focus: str | None = prop(default=None)

    class RenameProductParams(ActionParams):
        product_id: str = target(Product)
        name: str

    @ontology.action(
        RenameProductParams, target=Product, roles=["Operator"], api_name="RenameProduct"
    )
    def _rename(ctx: ActionContext, params: RenameProductParams) -> dict[str, str]:
        product = ctx.get(Product, params.product_id)
        assert product is not None
        product.name = params.name
        ctx.save(product)
        return {"product_id": params.product_id}

    class FocusTeamParams(ActionParams):
        team_id: str = target(Team)
        product_id: str = target(Product)

    @ontology.action(
        FocusTeamParams, target=Team, roles=["Operator"], api_name="FocusTeam"
    )
    def _focus(ctx: ActionContext, params: FocusTeamParams) -> dict[str, str]:
        team = ctx.get(Team, params.team_id)
        assert team is not None
        team.focus = params.product_id
        ctx.save(team)
        return {"team_id": params.team_id}

    ontology.validate()
    return ontology


def _seed(store: Store) -> None:
    src = Source(source_system="seed")
    store.insert("Product", {"id": "p1", "name": "Widget"}, src)
    store.insert("Team", {"id": "team-a"}, src)


@pytest.fixture
def ontology() -> Ontology:
    return _ontology()


def _client(
    ontology: Ontology,
    store: Store,
    make_consumer: ConsumerFactory,
    *,
    role: str = "Operator",
    scope_id: str = "team-a",
) -> OntologyClient:
    return OntologyClient(
        ontology,
        store,
        make_consumer(role=role, scope_level="team", scope_id=scope_id),
    )


def test_any_scope_may_execute_an_action_on_an_unscoped_target(
    ontology: Ontology, make_consumer: ConsumerFactory, make_store: StoreFactory
) -> None:
    store = make_store(ontology.registry)
    _seed(store)

    # A consumer whose scope has nothing to do with the product.
    _client(ontology, store, make_consumer, scope_id="team-z").execute(
        "RenameProduct", {"product_id": "p1", "name": "Gadget"}
    )

    current = store.read_current("Product", "p1")
    assert current is not None
    assert current.payload["name"] == "Gadget"
    entry = store.audit_entries()[-1]
    assert entry.outcome == "ok"
    assert entry.unscoped_params == ["product_id"]


def test_roles_remain_the_gate_for_an_unscoped_target(
    ontology: Ontology, make_consumer: ConsumerFactory, make_store: StoreFactory
) -> None:
    store = make_store(ontology.registry)
    _seed(store)

    with raises_code(PermissionDenied, "PERMISSION_DENIED"):
        _client(ontology, store, make_consumer, role="Viewer").execute(
            "RenameProduct", {"product_id": "p1", "name": "Gadget"}
        )

    entry = store.audit_entries()[-1]
    assert entry.outcome == "denied"
    # The role check runs before the scope gate, so nothing skipped it.
    assert entry.unscoped_params == []


def test_scoped_target_is_still_enforced_next_to_an_unscoped_one(
    ontology: Ontology, make_consumer: ConsumerFactory, make_store: StoreFactory
) -> None:
    store = make_store(ontology.registry)
    _seed(store)

    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        _client(ontology, store, make_consumer, scope_id="team-z").execute(
            "FocusTeam", {"team_id": "team-a", "product_id": "p1"}
        )
    denied = store.audit_entries()[-1]
    assert denied.outcome == "denied"
    assert denied.unscoped_params == ["product_id"]

    _client(ontology, store, make_consumer, scope_id="team-a").execute(
        "FocusTeam", {"team_id": "team-a", "product_id": "p1"}
    )
    ok = store.audit_entries()[-1]
    assert ok.outcome == "ok"
    assert ok.unscoped_params == ["product_id"]


def _ontology_with_scope_param_on_unscoped_type() -> Ontology:
    ontology = Ontology("unscoped-scope-param", scope_levels=["team"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Catalog(OntologyObject):
        id: str = prop(primary_key=True)

    class PublishParams(ActionParams):
        catalog_id: str = scope_ref(Catalog)

    @ontology.action(PublishParams, target=Catalog, roles=["Operator"], api_name="Publish")
    def _publish(ctx: ActionContext, params: PublishParams) -> dict[str, str]:
        return {"catalog_id": params.catalog_id}

    return ontology


def test_scope_parameter_on_an_unscoped_type_is_refused_at_validation() -> None:
    ontology = _ontology_with_scope_param_on_unscoped_type()

    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR") as exc_info:
        ontology.validate()
    assert "'Publish' parameter 'catalog_id'" in str(exc_info.value)


def test_diagnose_reports_a_scope_parameter_on_an_unscoped_type() -> None:
    findings = _ontology_with_scope_param_on_unscoped_type().diagnose()

    matching = [
        f
        for f in findings
        if f.code == "SCOPE_POLICY_ERROR" and "catalog_id" in f.message
    ]
    assert len(matching) == 1
    assert matching[0].severity == "error"
    assert matching[0].location == "ActionTypeDef['Publish'].parameters[0]"
