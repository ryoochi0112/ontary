"""T7: effect emission and the atomic pending outbox record."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest
from conftest import raises_code

from ontary.actions import ActionContext
from ontary.authoring import ActionParams, Ontology, OntologyObject, prop
from ontary.client import OntologyClient
from ontary.effects import EffectMeta, EffectPayload
from ontary.errors import PreconditionFailed, ValidationFailed
from ontary.security import Consumer
from ontary.store import AuditEntry, ObjectStore

ConsumerFactory = Callable[..., Consumer]


def _effects_emit_fixture() -> tuple[Ontology, ObjectStore, dict[str, Any]]:
    ontology = Ontology("effect-emission", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Record(OntologyObject):
        id: str = prop(primary_key=True)

    class Notice(EffectPayload):
        items: list[str]

    class OtherNotice(EffectPayload):
        items: list[str]

    notice = ontology.effect(Notice)
    ontology.effect(OtherNotice)

    class EmitSnapshotParams(ActionParams):
        pass

    @ontology.action(
        EmitSnapshotParams,
        target=Record,
        roles=["Operator"],
        effects=[notice],
        api_name="EmitSnapshot",
    )
    def emit_snapshot(
        ctx: ActionContext, _params: EmitSnapshotParams
    ) -> dict[str, str]:
        ctx.insert("Record", {"id": "snapshot"})
        payload = Notice(items=["before"])
        assert ctx.emit(payload) is None
        # AC9: `emit` performs NOTHING -- no dispatcher has run at this point,
        # and this is the only place that can still be observed now that T8
        # dispatches for real after commit.
        _MID_HANDLER_DISPATCH_COUNTS.append(len(_DISPATCH_LOG))
        payload.items.append("after")
        return {"id": "snapshot"}

    class EmitTwiceParams(ActionParams):
        pass

    @ontology.action(
        EmitTwiceParams,
        target=Record,
        roles=["Operator"],
        effects=[notice],
        api_name="EmitTwice",
    )
    def emit_twice(ctx: ActionContext, _params: EmitTwiceParams) -> dict[str, str]:
        payload = Notice(items=["first"])
        ctx.emit(payload)
        payload.items.append("second")
        ctx.emit(payload)
        payload.items.append("third")
        return {}

    class EmitThenRaiseParams(ActionParams):
        pass

    @ontology.action(
        EmitThenRaiseParams,
        target=Record,
        roles=["Operator"],
        effects=[notice],
        api_name="EmitThenRaise",
    )
    def emit_then_raise(
        ctx: ActionContext, _params: EmitThenRaiseParams
    ) -> dict[str, str]:
        ctx.insert("Record", {"id": "raised"})
        ctx.emit(Notice(items=["never-committed"]))
        raise RuntimeError("handler failed after emit")

    class EmitUndeclaredParams(ActionParams):
        pass

    @ontology.action(
        EmitUndeclaredParams,
        target=Record,
        roles=["Operator"],
        effects=[notice],
        api_name="EmitUndeclared",
    )
    def emit_undeclared(
        ctx: ActionContext, _params: EmitUndeclaredParams
    ) -> dict[str, str]:
        ctx.insert("Record", {"id": "undeclared"})
        ctx.emit(OtherNotice(items=["not declared by this action"]))
        return {}

    class EmitNothingParams(ActionParams):
        pass

    @ontology.action(
        EmitNothingParams,
        target=Record,
        roles=["Operator"],
        effects=[notice],
        api_name="EmitNothing",
    )
    def emit_nothing(
        ctx: ActionContext, _params: EmitNothingParams
    ) -> dict[str, str]:
        ctx.insert("Record", {"id": "nothing"})
        return {"id": "nothing"}

    store = ObjectStore(ontology.registry)
    return ontology, store, {"notice": notice}


_DISPATCH_LOG: list[EffectPayload] = []
"""Dispatch log visible to BOTH the dispatcher and the handlers in `_fixture`.

Module-level on purpose: T7's claim is that `emit` performs nothing *during the
handler body*, and once T8 added real post-commit dispatch that can only be
observed from INSIDE the handler -- by the time `execute()` returns, dispatch has
correctly already happened. pytest runs sequentially, and
`_dispatcher_calls()` clears the log per test.
"""

_MID_HANDLER_DISPATCH_COUNTS: list[int] = []
"""How many effects had been dispatched at the moment each handler finished
emitting. Every entry must be 0."""


def _dispatcher_calls() -> tuple[list[EffectPayload], Any]:
    _DISPATCH_LOG.clear()
    _MID_HANDLER_DISPATCH_COUNTS.clear()

    def dispatch(payload: EffectPayload, _meta: EffectMeta) -> None:
        _DISPATCH_LOG.append(payload)

    return _DISPATCH_LOG, dispatch


def test_emit_is_data_only_and_records_deep_snapshot_as_pending(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, store, handles = _effects_emit_fixture()
    calls, dispatch = _dispatcher_calls()
    client = OntologyClient(
        ontology,
        store,
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
        effects={handles["notice"]: dispatch},
    )

    assert client.execute("EmitSnapshot", {}) == {"id": "snapshot"}

    # AC9, observed where it is still observable: nothing was dispatched while the
    # handler was still running. (Before T8 this was asserted as `calls == []`
    # AFTER execute() returned, which only held because dispatch did not exist
    # yet -- T8 correctly invalidated that form.)
    assert _MID_HANDLER_DISPATCH_COUNTS == [0]
    # And what DID go out afterwards is the deep snapshot, not the payload the
    # handler mutated after emitting it.
    assert [p.model_dump() for p in calls] == [{"items": ["before"]}]
    # TWO entries once an effect was emitted (AC14): the durable `pending`
    # record committed with the writes, then T8's best-effort finalization.
    # An action that emits NOTHING still produces exactly one -- pinned
    # separately by test_declared_effect_but_no_emission_keeps_single_audit_entry.
    entries = store.audit_entries()
    assert [e.outcome for e in entries] == ["ok", "effects_dispatched"]
    # The pending record carries the SNAPSHOT taken at emit time, so the
    # handler's later mutation of the payload never reached the audit log.
    # `effect_id` is a uuid minted per emission, so it is compared to the
    # outbox row rather than to a literal -- the two must be the same value,
    # which is what makes the audit entry and the work item one story.
    effect_id = store.outbox_entries()[0].effect_id
    assert [effect.model_dump() for effect in entries[0].effects] == [
        {
            "api_name": "Notice",
            "payload": {"items": ["before"]},
            "outcome": "pending",
            "error": None,
            "effect_id": effect_id,
        }
    ]
    assert [effect.model_dump() for effect in entries[1].effects] == [
        {
            "api_name": "Notice",
            "payload": {"items": ["before"]},
            "outcome": "dispatched",
            "error": None,
            "effect_id": effect_id,
        }
    ]


def test_same_payload_emitted_twice_records_two_independent_ordered_snapshots(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, store, handles = _effects_emit_fixture()
    _calls, dispatch = _dispatcher_calls()
    client = OntologyClient(
        ontology,
        store,
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
        effects={handles["notice"]: dispatch},
    )

    client.execute("EmitTwice", {})

    effects = store.audit_entries()[0].effects
    assert [effect.api_name for effect in effects] == ["Notice", "Notice"]
    assert [effect.payload for effect in effects] == [
        {"items": ["first"]},
        {"items": ["first", "second"]},
    ]
    assert effects[0].payload["items"] is not effects[1].payload["items"]


def test_handler_emit_then_raise_rolls_back_writes_and_has_no_pending_row(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, store, handles = _effects_emit_fixture()
    calls, dispatch = _dispatcher_calls()
    client = OntologyClient(
        ontology,
        store,
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
        effects={handles["notice"]: dispatch},
    )

    with pytest.raises(RuntimeError, match="handler failed after emit"):
        client.execute("EmitThenRaise", {})

    assert store.read_all("Record") == []
    assert calls == []
    entries = store.audit_entries()
    assert len(entries) == 1
    assert entries[0].outcome == "error"
    assert entries[0].effects == []


def test_pending_audit_append_failure_rolls_back_ontology_writes(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, store, handles = _effects_emit_fixture()
    _calls, dispatch = _dispatcher_calls()
    client = OntologyClient(
        ontology,
        store,
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
        effects={handles["notice"]: dispatch},
    )
    append_audit = store.append_audit

    def fail_ok_append(entry: AuditEntry) -> None:
        if entry.outcome == "ok":
            raise RuntimeError("forced pending append failure")
        append_audit(entry)

    with patch.object(store, "append_audit", side_effect=fail_ok_append):
        with pytest.raises(RuntimeError, match="forced pending append failure"):
            client.execute("EmitSnapshot", {})

    assert store.read_all("Record") == []
    entries = store.audit_entries()
    assert len(entries) == 1
    assert entries[0].outcome == "error"
    assert entries[0].effects == []


def test_undeclared_emit_rolls_back_handler_write(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, store, handles = _effects_emit_fixture()
    _calls, dispatch = _dispatcher_calls()
    client = OntologyClient(
        ontology,
        store,
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
        effects={handles["notice"]: dispatch},
    )

    with raises_code(ValidationFailed, "UNDECLARED_EFFECT"):
        client.execute("EmitUndeclared", {})

    assert store.read_all("Record") == []
    assert store.audit_entries()[0].outcome == "error"


def test_missing_declared_dispatcher_is_preflight_audited_and_store_unchanged(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, store, _handles = _effects_emit_fixture()
    client = OntologyClient(
        ontology,
        store,
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
    )

    with patch.object(store, "transaction", wraps=store.transaction) as transaction:
        with raises_code(PreconditionFailed, "EFFECT_NOT_DISPATCHABLE"):
            client.execute("EmitSnapshot", {})

    # The sole transaction belongs to append_audit(error); the engine-owned
    # action transaction never opened.
    assert transaction.call_count == 1
    assert store.read_all("Record") == []
    entries = store.audit_entries()
    assert len(entries) == 1
    assert entries[0].outcome == "error"
    assert entries[0].writes == []
    assert entries[0].effects == []


def test_declared_effect_but_no_emission_keeps_single_audit_entry(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, store, handles = _effects_emit_fixture()
    _calls, dispatch = _dispatcher_calls()
    client = OntologyClient(
        ontology,
        store,
        make_consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
        effects={handles["notice"]: dispatch},
    )

    assert client.execute("EmitNothing", {}) == {"id": "nothing"}

    entries = store.audit_entries()
    assert len(entries) == 1
    assert entries[0].outcome == "ok"
    assert entries[0].effects == []
