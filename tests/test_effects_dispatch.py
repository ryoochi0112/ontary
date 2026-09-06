"""T8: post-commit effect dispatch, isolation, and best-effort finalization."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest

from ontary.actions import ActionContext
from ontary.authoring import ActionParams, Ontology, OntologyObject, prop
from ontary.client import OntologyClient
from ontary.effects import EffectMeta, EffectPayload
from ontary.security import Consumer
from ontary.store import AuditEntry, ObjectStore

ConsumerFactory = Callable[..., Consumer]


def _effects_dispatch_fixture() -> tuple[Ontology, ObjectStore, dict[str, Any]]:
    ontology = Ontology("effect-dispatch", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Record(OntologyObject):
        id: str = prop(primary_key=True)

    class Notice(EffectPayload):
        label: str
        items: list[str]

    class Archive(EffectPayload):
        label: str

    class Webhook(EffectPayload):
        label: str

    notice = ontology.effect(Notice)
    archive = ontology.effect(Archive)
    webhook = ontology.effect(Webhook)

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
        ctx.insert("Record", {"id": "rolled-back"})
        ctx.emit(Notice(label="never", items=[]))
        raise RuntimeError("handler failed")

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
        ctx.emit(Notice(label="first", items=[]))
        ctx.emit(Notice(label="second", items=[]))
        return {"status": "twice"}

    class EmitThreeParams(ActionParams):
        pass

    @ontology.action(
        EmitThreeParams,
        target=Record,
        roles=["Operator"],
        effects=[notice, archive, webhook],
        api_name="EmitThree",
    )
    def emit_three(ctx: ActionContext, _params: EmitThreeParams) -> dict[str, str]:
        ctx.emit(Notice(label="first", items=[]))
        ctx.emit(Archive(label="second"))
        ctx.emit(Webhook(label="third"))
        return {"status": "committed"}

    class EmitOneParams(ActionParams):
        token: str

    @ontology.action(
        EmitOneParams,
        target=Record,
        roles=["Operator"],
        effects=[notice],
        api_name="EmitOne",
    )
    def emit_one(ctx: ActionContext, params: EmitOneParams) -> dict[str, str]:
        ctx.emit(Notice(label=params.token, items=[]))
        return {"token": params.token}

    class InnerParams(ActionParams):
        token: str

    @ontology.action(
        InnerParams,
        target=Record,
        roles=["Operator"],
        api_name="Inner",
    )
    def inner(ctx: ActionContext, params: InnerParams) -> dict[str, str]:
        ctx.insert("Record", {"id": params.token})
        return {"token": params.token}

    class SnapshotTwiceParams(ActionParams):
        pass

    @ontology.action(
        SnapshotTwiceParams,
        target=Record,
        roles=["Operator"],
        effects=[notice],
        api_name="SnapshotTwice",
    )
    def snapshot_twice(
        ctx: ActionContext, _params: SnapshotTwiceParams
    ) -> dict[str, str]:
        payload = Notice(label="snapshot", items=["initial"])
        ctx.emit(payload)
        payload.items.append("between")
        ctx.emit(payload)
        payload.items.append("after")
        return {"status": "snapshotted"}

    store = ObjectStore(ontology.registry)
    return ontology, store, {
        "notice": notice,
        "archive": archive,
        "webhook": webhook,
    }


def test_handler_emit_then_raise_invokes_no_dispatcher(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, store, handles = _effects_dispatch_fixture()
    calls: list[str] = []

    def dispatch(payload: EffectPayload, _meta: EffectMeta) -> None:
        calls.append(str(payload.model_dump()["label"]))

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

    with pytest.raises(RuntimeError, match="handler failed"):
        client.execute("EmitThenRaise", {})

    assert calls == []
    assert store.read_all("Record") == []
    assert [entry.outcome for entry in store.audit_entries()] == ["error"]


def test_emission_order_is_dispatch_order_and_duplicates_are_not_deduped(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, store, handles = _effects_dispatch_fixture()
    calls: list[str] = []

    def dispatch(payload: EffectPayload, _meta: EffectMeta) -> None:
        calls.append(str(payload.model_dump()["label"]))

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

    assert client.execute("EmitTwice", {}) == {"status": "twice"}

    assert calls == ["first", "second"]
    entries = [
        entry for entry in store.audit_entries() if entry.action == "EmitTwice"
    ]
    assert [entry.outcome for entry in entries] == ["ok", "effects_dispatched"]
    assert [effect.api_name for effect in entries[1].effects] == ["Notice", "Notice"]
    assert [effect.outcome for effect in entries[1].effects] == [
        "dispatched",
        "dispatched",
    ]


def test_dispatch_failure_is_per_effect_and_does_not_change_action_result(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, store, handles = _effects_dispatch_fixture()
    calls: list[str] = []

    def fail_first(payload: EffectPayload, _meta: EffectMeta) -> None:
        calls.append(str(payload.model_dump()["label"]))
        raise RuntimeError("first transport broke")

    def dispatch(payload: EffectPayload, _meta: EffectMeta) -> None:
        calls.append(str(payload.model_dump()["label"]))

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
        effects={
            handles["notice"]: fail_first,
            handles["archive"]: dispatch,
            handles["webhook"]: dispatch,
        },
    )

    assert client.execute("EmitThree", {}) == {"status": "committed"}

    assert calls == ["first", "second", "third"]
    finalized = next(
        entry
        for entry in store.audit_entries()
        if entry.action == "EmitThree" and entry.outcome == "effects_dispatched"
    )
    # `retrying`, not `failed`: since the durable outbox, an attempt that
    # raises spends one of the policy's attempts and schedules another.
    # `failed` now means the budget is exhausted.
    assert [effect.outcome for effect in finalized.effects] == [
        "retrying",
        "dispatched",
        "dispatched",
    ]
    assert [effect.error for effect in finalized.effects] == [
        "first transport broke",
        None,
        None,
    ]
    # The isolation claim this test exists for, restated against the outbox:
    # the failed effect is still pending (retryable) while the other two are
    # done, rather than one failure poisoning the batch.
    assert [(row.api_name, row.state) for row in store.outbox_entries()] == [
        ("Notice", "pending"),
        ("Archive", "delivered"),
        ("Webhook", "delivered"),
    ]


def test_pending_record_is_durable_before_dispatch_starts(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, store, handles = _effects_dispatch_fixture()
    observations: list[tuple[str, list[str]]] = []

    def dispatch(_payload: EffectPayload, _meta: EffectMeta) -> None:
        matching = [
            entry
            for entry in store.audit_entries()
            if entry.action == "EmitOne" and entry.params == {"token": "visible"}
        ]
        observations.append(
            (matching[0].outcome, [effect.outcome for effect in matching[0].effects])
        )
        assert not any(
            entry.outcome == "effects_dispatched" for entry in matching
        )

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

    client.execute("EmitOne", {"token": "visible"})

    assert observations == [("ok", ["pending"])]


def test_reentrant_execute_interleaves_without_corrupting_outer_entries(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, store, handles = _effects_dispatch_fixture()
    client: OntologyClient

    def dispatch(_payload: EffectPayload, _meta: EffectMeta) -> None:
        assert client.execute("Inner", {"token": "nested"}) == {"token": "nested"}

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

    assert client.execute("EmitOne", {"token": "outer"}) == {"token": "outer"}

    entries = store.audit_entries()
    outer_pending = next(
        entry
        for entry in entries
        if entry.action == "EmitOne"
        and entry.params == {"token": "outer"}
        and entry.outcome == "ok"
    )
    outer_final = next(
        entry
        for entry in entries
        if entry.action == "EmitOne"
        and entry.params == {"token": "outer"}
        and entry.outcome == "effects_dispatched"
    )
    inner_entry = next(
        entry
        for entry in entries
        if entry.action == "Inner" and entry.params == {"token": "nested"}
    )

    assert entries.index(outer_pending) < entries.index(inner_entry) < entries.index(
        outer_final
    )
    assert outer_pending.effects[0].outcome == "pending"
    assert outer_final.effects[0].outcome == "dispatched"
    assert outer_pending.effects[0].payload == outer_final.effects[0].payload
    assert inner_entry.outcome == "ok"
    assert inner_entry.effects == []


def test_finalization_append_failure_is_swallowed_and_leaves_pending(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, store, handles = _effects_dispatch_fixture()
    calls: list[str] = []

    def dispatch(payload: EffectPayload, _meta: EffectMeta) -> None:
        calls.append(str(payload.model_dump()["label"]))

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

    def fail_finalization(entry: AuditEntry) -> None:
        if entry.outcome == "effects_dispatched":
            raise RuntimeError("audit SQL failed")
        append_audit(entry)

    with patch.object(store, "append_audit", side_effect=fail_finalization):
        assert client.execute("EmitOne", {"token": "pending"}) == {
            "token": "pending"
        }

    assert calls == ["pending"]
    matching = [
        entry
        for entry in store.audit_entries()
        if entry.action == "EmitOne" and entry.params == {"token": "pending"}
    ]
    assert len(matching) == 1
    assert matching[0].outcome == "ok"
    assert [effect.outcome for effect in matching[0].effects] == ["pending"]


def test_dispatcher_receives_only_minimal_frozen_effect_meta(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, store, handles = _effects_dispatch_fixture()
    seen: list[EffectMeta] = []

    def dispatch(_payload: EffectPayload, meta: EffectMeta) -> None:
        seen.append(meta)

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

    client.execute("EmitOne", {"token": "meta"})

    assert len(seen) == 1
    meta = seen[0]
    assert set(type(meta).model_fields) == {
        "action",
        "actor_id",
        "role",
        "ts",
        # Added by the durable outbox: the idempotency key and which try this
        # is. Both are delivery bookkeeping -- neither is a path to ontology
        # state, which is what this test guards.
        "effect_id",
        "attempt",
    }
    assert meta.action == "EmitOne"
    assert meta.actor_id == "operator-1"
    assert meta.role == "Operator"
    assert meta.attempt == 1
    assert meta.effect_id == store.outbox_entries()[0].effect_id
    pending = next(
        entry
        for entry in store.audit_entries()
        if entry.action == "EmitOne" and entry.outcome == "ok"
    )
    assert meta.ts == pending.ts
    assert not hasattr(meta, "store")
    assert not hasattr(meta, "consumer")
    assert not hasattr(meta, "ctx")


def test_dispatch_uses_independent_deep_snapshots(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, store, handles = _effects_dispatch_fixture()
    received: list[list[str]] = []

    def dispatch(payload: EffectPayload, _meta: EffectMeta) -> None:
        items = payload.model_dump()["items"]
        assert isinstance(items, list)
        received.append(items)
        payload.items.append("from-dispatcher")

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

    assert client.execute("SnapshotTwice", {}) == {"status": "snapshotted"}

    assert received == [["initial"], ["initial", "between"]]
    matching = [
        entry
        for entry in store.audit_entries()
        if entry.action == "SnapshotTwice"
    ]
    assert [effect.payload["items"] for effect in matching[0].effects] == [
        ["initial"],
        ["initial", "between"],
    ]
    assert [effect.payload["items"] for effect in matching[1].effects] == [
        ["initial"],
        ["initial", "between"],
    ]


# -- whole-branch review remediations (2026-07-26) ---------------------------


def test_dispatcher_exception_with_raising_str_is_still_isolated(
    make_consumer: ConsumerFactory,
) -> None:
    """Per-effect isolation must survive an exception that cannot be rendered.

    Review finding: the `except` handler recorded `str(exc)`, which runs author
    code. An exception whose `__str__` raises therefore propagated out of the
    handler AFTER the writes and the pending row had already committed -- so
    `execute()` raised, the remaining effects were never attempted, and no
    finalization row was written. Three stated guarantees (AC12's "never
    propagated", per-effect isolation, and the finalization append) were all
    broken by the error path itself, and no test noticed because every existing
    dispatcher failure raised an ordinary, renderable exception.
    """
    ontology, store, handles = _effects_dispatch_fixture()
    dispatched: list[str] = []

    class Unrenderable(Exception):
        def __str__(self) -> str:
            raise RuntimeError("this exception cannot be rendered")

    def failing(_payload: EffectPayload, _meta: EffectMeta) -> None:
        raise Unrenderable

    def recording(label: str) -> Any:
        def _dispatch(_payload: EffectPayload, _meta: EffectMeta) -> None:
            dispatched.append(label)

        return _dispatch

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
        effects={
            handles["notice"]: failing,
            handles["archive"]: recording("archive"),
            handles["webhook"]: recording("webhook"),
        },
    )

    # Does not raise, despite the unrenderable failure in the FIRST dispatcher.
    assert client.execute("EmitThree", {}) == {"status": "committed"}
    # The remaining two still ran, in emission order.
    assert dispatched == ["archive", "webhook"]

    final = [e for e in store.audit_entries() if e.outcome == "effects_dispatched"]
    assert len(final) == 1
    assert [(r.api_name, r.outcome) for r in final[0].effects] == [
        ("Notice", "retrying"),
        ("Archive", "dispatched"),
        ("Webhook", "dispatched"),
    ]
    # The failure is recorded with a placeholder rather than lost or raised.
    failed = next(r for r in final[0].effects if r.outcome == "retrying")
    assert failed.error is not None
    assert "Unrenderable" in failed.error
    # And the unrenderable failure did not stop the row from being booked as a
    # real attempt: it is retryable, with the placeholder as its last error.
    notice_row = next(r for r in store.outbox_entries() if r.api_name == "Notice")
    assert (notice_row.state, notice_row.attempts) == ("pending", 1)
    assert notice_row.last_error == failed.error


def test_keyboard_interrupt_from_a_dispatcher_propagates_by_design(
    make_consumer: ConsumerFactory,
) -> None:
    """The narrowed AC12: a process-control exception is NOT isolated.

    Fifth-pass review finding (2026-07-26) — and the finding is about honesty, not
    behavior. §9's test matrix CLAIMED this was pinned once AC12 was narrowed to
    exclude `KeyboardInterrupt`/`SystemExit`, but no such test existed. A matrix row
    asserting coverage that is absent is worse than an empty cell: it tells the next
    reader the boundary is guarded.

    The behavior is deliberate. Swallowing a Ctrl-C to keep a delivery promise is
    the wrong trade — the operator asked the process to stop — so the interrupt
    propagates, later dispatches do not run, and finalization is skipped. What still
    holds unconditionally is that the ontology writes and the durable `pending`
    record committed before dispatch began, so the intent is recoverable.
    """
    ontology, store, handles = _effects_dispatch_fixture()
    dispatched: list[str] = []

    def interrupting(_payload: EffectPayload, _meta: EffectMeta) -> None:
        raise KeyboardInterrupt

    def recording(label: str) -> Any:
        def _dispatch(_payload: EffectPayload, _meta: EffectMeta) -> None:
            dispatched.append(label)

        return _dispatch

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
        effects={
            handles["notice"]: interrupting,
            handles["archive"]: recording("archive"),
            handles["webhook"]: recording("webhook"),
        },
    )

    with pytest.raises(KeyboardInterrupt):
        client.execute("EmitThree", {})

    # Later dispatches did NOT run, and no finalization row was written.
    assert dispatched == []
    outcomes = [e.outcome for e in store.audit_entries()]
    assert outcomes == ["ok"]
    assert "effects_dispatched" not in outcomes

    # But the pending record IS durable -- it committed with the writes, before
    # dispatch began, so what the action intended to send remains recoverable.
    pending = store.audit_entries()[0]
    assert [r.outcome for r in pending.effects] == ["pending", "pending", "pending"]
