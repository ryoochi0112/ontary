"""T6 governed capability access, refusal, and action audit records."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol

import pytest
from conftest import raises_code

from ontary.actions import ActionContext, ActionError
from ontary.audit import AuditEntry, CapabilityAccessRecord
from ontary.authoring import ActionParams, Ontology, OntologyObject, prop
from ontary.errors import PreconditionFailed, ValidationFailed
from ontary.functions import BoundQuery
from ontary.security import Consumer
from ontary.store import ObjectStore, Source

ConsumerFactory = Callable[..., Consumer]


class _Reader(Protocol):
    def read(self, value: str) -> str: ...


class _Provider:
    def __init__(self) -> None:
        self.arguments: list[str] = []

    def read(self, value: str) -> str:
        self.arguments.append(value)
        return "private-result"


class _TransactionTrackingStore(ObjectStore):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Set BEFORE super().__init__(): a base-constructor step that opened a
        # transaction through `self` would run an override reading subclass
        # state before this __init__ body had a chance to set it. Counting
        # starts after construction either way (`action_transactions` is about
        # actions), so the reset below is what the assertions depend on.
        self.action_transactions = 0
        self._appending_audit = False
        super().__init__(*args, **kwargs)
        self.action_transactions = 0

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        if not self._appending_audit:
            self.action_transactions += 1
        with super().transaction() as connection:
            yield connection

    def append_audit(self, entry: AuditEntry) -> None:
        self._appending_audit = True
        try:
            super().append_audit(entry)
        finally:
            self._appending_audit = False


def _capability_fixture() -> dict[str, Any]:
    foreign_ontology = Ontology("foreign", scope_levels=["org"], min_n=1)
    foreign = foreign_ontology.capability(_Reader, name="foreignReader")

    ontology = Ontology("capabilities", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped")
    class Record(OntologyObject):
        id: str = prop(primary_key=True)

    declared = ontology.capability(_Reader, name="declaredReader")
    undeclared = ontology.capability(_Reader, name="undeclaredReader")
    mode = {"value": "ok"}

    class RunParams(ActionParams):
        pass

    @ontology.action(
        RunParams,
        target=Record,
        roles=["Operator"],
        capabilities=[declared],
        api_name="Run",
    )
    def run(ctx: ActionContext, _params: RunParams) -> dict[str, str]:
        selected = mode["value"]
        if selected == "undeclared":
            ctx.capability(undeclared)
        if selected == "foreign":
            ctx.capability(foreign)
        if selected == "write":
            ctx.insert("Record", {"id": "must-not-exist"})
        provider = ctx.capability(declared)
        provider.read("private-argument")
        provider.read("private-argument")
        provider.read("private-argument")
        if selected == "raise":
            raise ActionError("after capability access", code="PRECONDITION_FAILED")
        return {"status": "ok"}

    @ontology.function(api_name="Read", capabilities=[declared])
    def read(query: BoundQuery, _params: dict[str, Any]) -> str:
        selected = mode["value"]
        if selected == "undeclared":
            return query.capability(undeclared).read("function-argument")
        if selected == "foreign":
            return query.capability(foreign).read("function-argument")
        return query.capability(declared).read("function-argument")

    ontology.validate()
    return {
        "ontology": ontology,
        "record": Record,
        "declared": declared,
        "undeclared": undeclared,
        "foreign": foreign,
        "mode": mode,
    }


def test_action_access_count_is_provider_retrievals_not_provider_calls(
    make_consumer: ConsumerFactory,
) -> None:
    fixture = _capability_fixture()
    provider = _Provider()
    store = ObjectStore(fixture["ontology"].registry)
    client = fixture["ontology"].bind(
        store, capabilities={fixture["declared"]: provider}
    ).for_consumer(
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
            principal=None,
        )
    )

    assert client.execute("Run", {}) == {"status": "ok"}

    entry = store.audit_entries()[-1]
    assert entry.outcome == "ok"
    assert entry.capability_accesses == [
        CapabilityAccessRecord(api_name="declaredReader", count=1)
    ]
    assert provider.arguments == ["private-argument"] * 3
    serialized = entry.model_dump_json()
    assert "private-argument" not in serialized
    assert "private-result" not in serialized


def test_action_access_then_raise_records_access_on_error_without_values(
    make_consumer: ConsumerFactory,
) -> None:
    fixture = _capability_fixture()
    fixture["mode"]["value"] = "raise"
    provider = _Provider()
    store = ObjectStore(fixture["ontology"].registry)
    client = fixture["ontology"].bind(
        store, capabilities={fixture["declared"]: provider}
    ).for_consumer(
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
            principal=None,
        )
    )

    with pytest.raises(ActionError, match="after capability access"):
        client.execute("Run", {})

    entry = store.audit_entries()[-1]
    assert entry.outcome == "error"
    assert entry.capability_accesses == [
        CapabilityAccessRecord(api_name="declaredReader", count=1)
    ]
    serialized = entry.model_dump_json()
    assert "private-argument" not in serialized
    assert "private-result" not in serialized


def test_action_refuses_undeclared_capability_even_when_provider_is_bound(
    make_consumer: ConsumerFactory,
) -> None:
    fixture = _capability_fixture()
    fixture["mode"]["value"] = "undeclared"
    store = ObjectStore(fixture["ontology"].registry)
    client = fixture["ontology"].bind(
        store,
        capabilities={
            fixture["declared"]: _Provider(),
            fixture["undeclared"]: _Provider(),
        },
    ).for_consumer(
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
            principal=None,
        )
    )

    with raises_code(ValidationFailed, "UNDECLARED_CAPABILITY"):
        client.execute("Run", {})

    assert store.audit_entries()[-1].capability_accesses == []


def test_action_declared_but_unprovided_is_preflight_audited_and_store_safe(
    make_consumer: ConsumerFactory,
) -> None:
    fixture = _capability_fixture()
    fixture["mode"]["value"] = "write"
    store = _TransactionTrackingStore(fixture["ontology"].registry)
    client = fixture["ontology"].bind(store).for_consumer(
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
            principal=None,
        )
    )

    with raises_code(PreconditionFailed, "CAPABILITY_NOT_PROVIDED"):
        client.execute("Run", {})

    assert store.action_transactions == 0
    assert store.read_all("Record") == []
    entry = store.audit_entries()[-1]
    assert entry.outcome == "error"
    assert entry.capability_accesses == []


def test_action_refuses_foreign_registry_handle_without_recording_access(
    make_consumer: ConsumerFactory,
) -> None:
    fixture = _capability_fixture()
    fixture["mode"]["value"] = "foreign"
    store = ObjectStore(fixture["ontology"].registry)
    client = fixture["ontology"].bind(
        store, capabilities={fixture["declared"]: _Provider()}
    ).for_consumer(
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
            principal=None,
        )
    )

    with raises_code(ValidationFailed, "UNKNOWN_NAME"):
        client.execute("Run", {})

    assert store.audit_entries()[-1].capability_accesses == []


def test_action_context_unprovided_access_does_not_increment(
    make_consumer: ConsumerFactory,
) -> None:
    fixture = _capability_fixture()
    accesses: list[CapabilityAccessRecord] = []
    ctx = ActionContext(
        ObjectStore(fixture["ontology"].registry),
        Source(source_system="action:test"),
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
            principal=None,
        ),
        registry=fixture["ontology"].registry,
        declared_capabilities=frozenset({"declaredReader"}),
        capability_accesses=accesses,
    )

    with raises_code(PreconditionFailed, "CAPABILITY_NOT_PROVIDED"):
        ctx.capability(fixture["declared"])

    assert accesses == []


def test_function_refuses_undeclared_capability_even_when_provider_is_bound(
    make_consumer: ConsumerFactory,
) -> None:
    fixture = _capability_fixture()
    fixture["mode"]["value"] = "undeclared"
    client = fixture["ontology"].bind(
        ObjectStore(fixture["ontology"].registry),
        capabilities={
            fixture["declared"]: _Provider(),
            fixture["undeclared"]: _Provider(),
        },
    ).for_consumer(
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
            principal=None,
        )
    )

    with raises_code(ValidationFailed, "UNDECLARED_CAPABILITY"):
        client.call_function("Read", {})



def test_function_refuses_declared_but_unprovided_capability(
    make_consumer: ConsumerFactory,
) -> None:
    fixture = _capability_fixture()
    client = fixture["ontology"].bind(
        ObjectStore(fixture["ontology"].registry)
    ).for_consumer(
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
            principal=None,
        )
    )

    with raises_code(PreconditionFailed, "CAPABILITY_NOT_PROVIDED"):
        client.call_function("Read", {})



def test_function_refuses_foreign_registry_handle(
    make_consumer: ConsumerFactory,
) -> None:
    fixture = _capability_fixture()
    fixture["mode"]["value"] = "foreign"
    client = fixture["ontology"].bind(
        ObjectStore(fixture["ontology"].registry),
        capabilities={fixture["declared"]: _Provider()},
    ).for_consumer(
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
            principal=None,
        )
    )

    with raises_code(ValidationFailed, "UNKNOWN_NAME"):
        client.call_function("Read", {})
