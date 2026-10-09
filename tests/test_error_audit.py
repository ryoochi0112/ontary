"""Every non-ok audit entry says why it failed and on what (#49).

Before #49 only the `ok` entry carried `target_id`, and no entry carried the
error code: an `error` or `denied` entry said an attempt failed, but not on
which object or with which catalogued code. Now every action entry carries the
requested target id, and every non-ok entry (action or function) carries
`error_code`. An exception without a `code` is recorded as `INTERNAL_ERROR`,
the same code the MCP server reports for it.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from conftest import raises_code

from ontary import (
    ActionContext,
    ActionParams,
    BoundQuery,
    Consumer,
    Ontology,
    OntologyObject,
    SelfScope,
    Source,
    prop,
    target,
)
from ontary.actions import ActionError
from ontary.client import OntologyClient
from ontary.errors import PermissionDenied, PreconditionFailed
from ontary.store import Store
from ontary.store.schema import SCHEMA_VERSION

ConsumerFactory = Callable[..., Consumer]
StoreFactory = Callable[..., Store]


class Clock:
    def now(self) -> str:
        return "2026-09-27T00:00:00+00:00"


def _ontology() -> Ontology:
    ontology = Ontology("error-audit", scope_levels=["team"], min_n=1)

    @ontology.object(layer="L0", scope=[SelfScope(level="team")], owned=True)
    class Team(OntologyObject):
        id: str = prop(primary_key=True)
        focus: str | None = prop(default=None)

    clock = ontology.capability(Clock)

    class FocusParams(ActionParams):
        team_id: str = target(Team)
        focus: str

    @ontology.action(FocusParams, target=Team, roles=["Operator"], api_name="Focus")
    def _focus(ctx: ActionContext, params: FocusParams) -> dict[str, str]:
        if params.focus == "refuse":
            raise ActionError("focus refused", code="PRECONDITION_FAILED")
        if params.focus == "custom":
            raise ActionError("author code", code="TEAM_FROZEN")
        if params.focus == "crash":
            raise KeyError("boom")
        team = ctx.get(Team, params.team_id)
        assert team is not None
        team.focus = params.focus
        ctx.save(team)
        return {"team_id": params.team_id}

    class DefaultFocusParams(FocusParams):
        team_id: str = target(Team, required=False, default="team-a")
        focus: str = "refuse"

    @ontology.action(DefaultFocusParams, target=Team, roles=["Operator"], api_name="DefaultFocus")
    def _default_focus(ctx: ActionContext, params: DefaultFocusParams) -> dict[str, str]:
        return _focus(ctx, params)

    class StampParams(ActionParams):
        team_id: str = target(Team)

    @ontology.action(
        StampParams,
        target=Team,
        roles=["Operator"],
        api_name="Stamp",
        capabilities=[clock],
    )
    def _stamp(ctx: ActionContext, params: StampParams) -> dict[str, str]:
        return {"at": ctx.capability(clock).now()}

    @ontology.function(api_name="explodes", audit=True)
    def _explodes(_query: BoundQuery) -> int:
        raise RuntimeError("boom")

    @ontology.function(api_name="refuses", audit=True)
    def _refuses(_query: BoundQuery) -> int:
        raise PreconditionFailed("not now", code="FUNCTION_ERROR")

    ontology.validate()
    return ontology


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


def _store(ontology: Ontology, make_store: StoreFactory) -> Store:
    store = make_store(ontology.registry)
    store.insert("Team", {"id": "team-a"}, Source(source_system="seed"))
    return store


# -- action entries: target_id + error_code on every non-ok outcome ----------


@pytest.mark.parametrize(
    ("focus", "code"),
    [
        ("refuse", "PRECONDITION_FAILED"),
        # An author-supplied code that is not in the catalogue is kept as-is.
        ("custom", "TEAM_FROZEN"),
    ],
)
def test_handler_error_entry_carries_target_and_the_raised_code(
    ontology: Ontology,
    make_consumer: ConsumerFactory,
    make_store: StoreFactory,
    focus: str,
    code: str,
) -> None:
    store = _store(ontology, make_store)

    with raises_code(ActionError, code):
        _client(ontology, store, make_consumer).execute(
            "Focus", {"team_id": "team-a", "focus": focus}
        )

    entry = store.audit_entries()[-1]
    assert entry.outcome == "error"
    assert entry.target_id == "team-a"
    assert entry.error_code == code


def test_uncoded_handler_exception_is_audited_as_internal_error(
    ontology: Ontology, make_consumer: ConsumerFactory, make_store: StoreFactory
) -> None:
    store = _store(ontology, make_store)

    with pytest.raises(KeyError):
        _client(ontology, store, make_consumer).execute(
            "Focus", {"team_id": "team-a", "focus": "crash"}
        )

    entry = store.audit_entries()[-1]
    assert entry.outcome == "error"
    assert entry.target_id == "team-a"
    assert entry.error_code == "INTERNAL_ERROR"


def test_role_denial_carries_target_and_code(
    ontology: Ontology, make_consumer: ConsumerFactory, make_store: StoreFactory
) -> None:
    store = _store(ontology, make_store)

    with raises_code(PermissionDenied, "PERMISSION_DENIED"):
        _client(ontology, store, make_consumer, role="Viewer").execute(
            "Focus", {"team_id": "team-a", "focus": "x"}
        )

    entry = store.audit_entries()[-1]
    assert entry.outcome == "denied"
    assert entry.target_id == "team-a"
    assert entry.error_code == "PERMISSION_DENIED"


def test_scope_denial_carries_target_and_code(
    ontology: Ontology, make_consumer: ConsumerFactory, make_store: StoreFactory
) -> None:
    store = _store(ontology, make_store)

    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        _client(ontology, store, make_consumer, scope_id="team-z").execute(
            "Focus", {"team_id": "team-a", "focus": "x"}
        )

    entry = store.audit_entries()[-1]
    assert entry.outcome == "denied"
    assert entry.target_id == "team-a"
    assert entry.error_code == "SCOPE_DENIED"


def test_invalid_params_entry_carries_target_and_code(
    ontology: Ontology, make_consumer: ConsumerFactory, make_store: StoreFactory
) -> None:
    store = _store(ontology, make_store)

    with raises_code(ActionError, "INVALID_PARAMS"):
        _client(ontology, store, make_consumer).execute("Focus", {"team_id": "team-a"})

    entry = store.audit_entries()[-1]
    assert entry.outcome == "error"
    assert entry.target_id == "team-a"
    assert entry.error_code == "INVALID_PARAMS"


def test_missing_capability_entry_carries_target_and_code(
    ontology: Ontology, make_consumer: ConsumerFactory, make_store: StoreFactory
) -> None:
    store = _store(ontology, make_store)

    with raises_code(PreconditionFailed, "CAPABILITY_NOT_PROVIDED"):
        _client(ontology, store, make_consumer).execute("Stamp", {"team_id": "team-a"})

    entry = store.audit_entries()[-1]
    assert entry.outcome == "error"
    assert entry.target_id == "team-a"
    assert entry.error_code == "CAPABILITY_NOT_PROVIDED"


def test_ok_entry_has_no_error_code(
    ontology: Ontology, make_consumer: ConsumerFactory, make_store: StoreFactory
) -> None:
    store = _store(ontology, make_store)

    _client(ontology, store, make_consumer).execute(
        "Focus", {"team_id": "team-a", "focus": "x"}
    )

    entry = store.audit_entries()[-1]
    assert entry.outcome == "ok"
    assert entry.target_id == "team-a"
    assert entry.error_code is None


@pytest.mark.parametrize(
    ("role", "scope_id", "params", "exc_type", "code", "outcome"),
    [
        ("Viewer", "team-a", {}, PermissionDenied, "PERMISSION_DENIED", "denied"),
        ("Operator", "team-a", {"focus": 42}, ActionError, "INVALID_PARAMS", "error"),
        ("Operator", "team-z", {}, PermissionDenied, "SCOPE_DENIED", "denied"),
        ("Operator", "team-a", {}, ActionError, "PRECONDITION_FAILED", "error"),
    ],
)
def test_defaults_are_recorded_on_role_validation_scope_and_handler_refusals(
    ontology: Ontology,
    make_consumer: ConsumerFactory,
    make_store: StoreFactory,
    role: str,
    scope_id: str,
    params: dict[str, object],
    exc_type: type[Exception],
    code: str,
    outcome: str,
) -> None:
    store = _store(ontology, make_store)
    assert store.read_current("Team", "team-a") is not None
    before = params.copy()

    with pytest.raises(exc_type) as exc:
        _client(ontology, store, make_consumer, role=role, scope_id=scope_id).execute(
            "DefaultFocus", params
        )

    assert exc.value.code == code
    assert params == before
    entry = store.audit_entries()[-1]
    assert entry.outcome == outcome
    assert entry.params == {"team_id": "team-a", "focus": "refuse", **before}
    assert entry.target_id == "team-a"
    assert entry.error_code == code
    assert entry.writes == []


# -- function entries: error_code only (a function has no target) ------------


@pytest.mark.parametrize(
    ("api_name", "exc_type", "code"),
    [
        ("explodes", RuntimeError, "INTERNAL_ERROR"),
        ("refuses", PreconditionFailed, "FUNCTION_ERROR"),
    ],
)
def test_function_error_entry_carries_the_code(
    ontology: Ontology,
    make_consumer: ConsumerFactory,
    make_store: StoreFactory,
    api_name: str,
    exc_type: type[Exception],
    code: str,
) -> None:
    store = _store(ontology, make_store)

    with pytest.raises(exc_type):
        _client(ontology, store, make_consumer).call_function(api_name, {})

    entry = store.audit_entries()[-1]
    assert entry.kind == "function"
    assert entry.outcome == "error"
    assert entry.target_id is None
    assert entry.error_code == code


# -- storage ------------------------------------------------------------------


def test_audit_store_schema_is_14() -> None:
    """The events column follows error_code in the current audit schema."""
    assert SCHEMA_VERSION == 14
