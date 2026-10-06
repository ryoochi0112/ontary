"""Declaration-only read marks and their private client seam (#62, T1)."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import (
    ConsumerFactory,
    OntologyFactory,
    PolicyFactory,
    StoreFactory,
    raises_code,
)

from ontary.authoring import Ontology, OntologyObject, prop
from ontary.client import OntologyClient
from ontary.errors import ValidationFailed
from ontary.meta import Cardinality, Sensitivity
from ontary.ontology import OntologyDef
from ontary.query import GuardedQuery
from ontary.scope import DirectProperty, RowVisibilityStore, ScopePolicy
from ontary.security import Consumer, ConsumerKind
from ontary.store import Source

SRC = Source(source_system="test")


@pytest.fixture
def ticket_ontology(make_ontology: OntologyFactory) -> tuple[Ontology, type[OntologyObject]]:
    ontology = make_ontology(name="read-marks", scope_levels=["team", "org"])

    @ontology.object(layer="L0", scope="unscoped")
    class Team(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[
            DirectProperty(level="team", property_name="team_id"),
            DirectProperty(level="org", property_name="org_id"),
        ],
    )
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        team_id: str | None = prop(
            default=None, sensitivity=Sensitivity(human_visible=False, ai_usable=False)
        )
        org_id: str
        blocked: bool
        z_note: str | None = prop(default=None, sensitivity=Sensitivity(human_visible=False))
        a_signal: str | None = prop(default=None, sensitivity=Sensitivity(ai_usable=False))
        sparse_secret: str | None = prop(
            default=None, sensitivity=Sensitivity(human_visible=False, ai_usable=False)
        )
        resolution: str | None = None

    ontology.link("inTeam", Ticket, Team, Cardinality.MANY_TO_ONE)
    ontology.definition.validate()
    return ontology, Ticket


def _row_visible(
    _store: RowVisibilityStore,
    _consumer: Consumer,
    _obj_type: str,
    payload: dict[str, Any],
) -> bool:
    return not payload["blocked"]


def _ticket_policy(
    make_policy: PolicyFactory, *, unscoped: bool, row_rule: bool
) -> ScopePolicy:
    return make_policy(
        levels=["team", "org"],
        unscoped_types={"Team", "Ticket"} if unscoped else {"Team"},
        rules={} if unscoped else {
            "Ticket": [
                DirectProperty(level="team", property_name="team_id"),
                DirectProperty(level="org", property_name="org_id"),
            ],
        },
        row_visibility={"Ticket": _row_visible} if row_rule else {},
    )


@pytest.mark.parametrize("unscoped", [False, True], ids=["scoped", "unscoped"])
@pytest.mark.parametrize("row_rule", [False, True], ids=["no-row-rule", "row-rule"])
@pytest.mark.parametrize("kind", ["human", "ai"])
@pytest.mark.parametrize("covers_rows", [False, True], ids=["uncovered", "covered"])
def test_scope_limited_mirrors_visible_deny_branches(
    unscoped: bool,
    row_rule: bool,
    kind: ConsumerKind,
    covers_rows: bool,
    ticket_ontology: tuple[Ontology, type[OntologyObject]],
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    make_consumer: ConsumerFactory,
) -> None:
    ontology, _ = ticket_ontology
    policy = _ticket_policy(make_policy, unscoped=unscoped, row_rule=row_rule)
    policy.validate(ontology.registry)
    store = make_store(ontology.registry)
    for owner in ("team-a", "team-b", "team-c"):
        store.insert("Team", {"id": owner}, SRC)
        for blocked in (False, True):
            store.insert("Ticket", {
                "id": f"{owner}-{blocked}", "team_id": owner,
                "org_id": "org-1", "blocked": blocked,
            }, SRC)
    consumer = make_consumer(
        kind=kind, scope_level="org", scope_id="org-1" if covers_rows else "org-other"
    )
    query = GuardedQuery(store, ontology.registry, policy)

    limited = query.scope_limited("Ticket")
    assert limited is (not unscoped or row_rule)
    for row in store.read_all("Ticket"):
        visible = query._visible(consumer, row)
        assert visible is (
            (unscoped or covers_rows) and (not row_rule or not row.payload["blocked"])
        )
        if not limited:
            assert visible is True
        if not visible:
            assert limited is True


@pytest.mark.parametrize("kind", ["human", "ai"])
@pytest.mark.parametrize("unscoped", [False, True], ids=["scoped", "unscoped"])
@pytest.mark.parametrize("row_rule", [False, True], ids=["no-row-rule", "row-rule"])
def test_read_marks_are_independent_of_hidden_rows(
    kind: ConsumerKind,
    unscoped: bool,
    row_rule: bool,
    ticket_ontology: tuple[Ontology, type[OntologyObject]],
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    make_consumer: ConsumerFactory,
) -> None:
    ontology, _ = ticket_ontology
    policy = _ticket_policy(make_policy, unscoped=unscoped, row_rule=row_rule)
    definition = OntologyDef("read-marks", ontology.registry, policy)
    definition.validate()
    consumer = make_consumer(kind=kind, scope_level="team", scope_id="team-a")
    hidden_store = make_store(ontology.registry)
    visible_store = make_store(ontology.registry)
    hidden_store.insert("Ticket", {
        "id": "ticket-1", "team_id": "team-b", "org_id": "org-1", "blocked": True,
    }, SRC)
    visible_store.insert("Ticket", {
        "id": "ticket-1", "team_id": "team-a", "org_id": "org-1", "blocked": False,
    }, SRC)
    hidden_client = OntologyClient(definition, hidden_store, consumer)
    visible_client = OntologyClient(definition, visible_store, consumer)

    assert hidden_client.count("Ticket") == (1 if unscoped and not row_rule else 0)
    assert visible_client.count("Ticket") == 1
    expected_fields = (
        ("sparse_secret", "team_id", "z_note") if kind == "human"
        else ("a_signal", "sparse_secret", "team_id")
    )
    expected = (not unscoped or row_rule, expected_fields)
    for client in (hidden_client, visible_client):
        assert client._query.scope_limited("Ticket") is expected[0]
        assert client._query.redacted_fields(consumer, "Ticket") == expected[1]
        assert client._read_marks("Ticket") == expected


@pytest.mark.parametrize("kind", ["human", "ai"])
def test_redacted_fields_match_typed_reads_including_hidden_routing_key(
    kind: ConsumerKind,
    ticket_ontology: tuple[Ontology, type[OntologyObject]],
    make_store: StoreFactory,
    make_consumer: ConsumerFactory,
) -> None:
    ontology, Ticket = ticket_ontology
    store = make_store(ontology.registry)
    store.insert("Ticket", {
        "id": "ticket-1", "team_id": "team-a", "org_id": "org-1", "blocked": False,
        "z_note": "internal note", "a_signal": "internal signal",
    }, SRC)
    consumer = make_consumer(kind=kind, scope_level="team", scope_id="team-a")
    client = OntologyClient(ontology, store, consumer)
    typed = client.get(Ticket, "ticket-1")
    raw = client.get("Ticket", "ticket-1")
    assert typed is not None
    assert raw is not None

    fields = client._query.redacted_fields(consumer, "Ticket")
    assert fields == tuple(sorted(typed.redacted_fields))
    assert "team_id" in fields
    assert "sparse_secret" in fields
    assert "resolution" not in fields
    assert all(field not in raw.payload for field in fields)
    stored = store.read_current("Ticket", "ticket-1")
    assert stored is not None
    assert set(stored.payload) - set(raw.payload) == set(fields) - {"sparse_secret"}
    assert client._read_marks("Ticket") == (True, fields)
    assert client._read_marks("Team") == (False, ())


@pytest.mark.parametrize("unscoped", [False, True], ids=["scoped", "unscoped"])
@pytest.mark.parametrize("row_rule", [False, True], ids=["no-row-rule", "row-rule"])
def test_read_marks_never_consult_store_or_row_predicate(
    unscoped: bool,
    row_rule: bool,
    ticket_ontology: tuple[Ontology, type[OntologyObject]],
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    make_consumer: ConsumerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology, _ = ticket_ontology
    policy = _ticket_policy(make_policy, unscoped=unscoped, row_rule=row_rule)

    def unexpected_access(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("read marks must use declarations only")

    if row_rule:
        policy.row_visibility["Ticket"] = unexpected_access
    client = OntologyClient(
        OntologyDef("read-marks", ontology.registry, policy),
        make_store(ontology.registry), make_consumer(),
    )

    class UnreadableStore:
        def __getattr__(self, _name: str) -> Any:
            unexpected_access()

    monkeypatch.setattr(client._query, "_store", UnreadableStore())
    assert client._read_marks("Ticket") == (
        not unscoped or row_rule, ("sparse_secret", "team_id", "z_note")
    )


def test_read_marks_refuse_incoherent_scope_policy(
    ticket_ontology: tuple[Ontology, type[OntologyObject]],
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    make_consumer: ConsumerFactory,
) -> None:
    ontology, _ = ticket_ontology
    policy = _ticket_policy(make_policy, unscoped=False, row_rule=False)
    policy.unscoped_types.add("Ticket")
    query = GuardedQuery(make_store(ontology.registry), ontology.registry, policy)
    consumer = make_consumer()

    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR"):
        query.scope_limited("Ticket")
    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR"):
        query.redacted_fields(consumer, "Ticket")


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_link_target_type_uses_result_endpoint(
    reverse: bool,
    ticket_ontology: tuple[Ontology, type[OntologyObject]],
    make_store: StoreFactory,
    make_consumer: ConsumerFactory,
) -> None:
    ontology, _ = ticket_ontology
    client = OntologyClient(ontology, make_store(ontology.registry), make_consumer())

    assert client._link_target_type("inTeam", reverse=reverse) == (
        "Ticket" if reverse else "Team"
    )
    with raises_code(ValidationFailed, "UNKNOWN_NAME") as marks_error:
        client._link_target_type("unknown-link", reverse=reverse)
    with raises_code(ValidationFailed, "UNKNOWN_NAME") as traverse_error:
        client.traverse("Team" if reverse else "Ticket", "unknown-link", "id", reverse=reverse)
    assert str(marks_error.value) == str(traverse_error.value)
