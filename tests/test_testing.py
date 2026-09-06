"""Tests for the public ``ontary.testing`` SDK-user helpers."""

from __future__ import annotations

import ast
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import ontary
from ontary import (
    ActionContext,
    ActionParams,
    EffectPayload,
    InMemoryStore,
    Ontology,
    OntologyObject,
    ValidationFailed,
    prop,
)
from ontary.ingest import IngestError
from ontary.testing import (
    FixedClock,
    SequentialIds,
    capture_effects,
    consumer,
    make_store,
    raises_code,
)


def _build_effect_ontology() -> tuple[Ontology, Any]:
    ontology = Ontology("testing-effects", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Record(OntologyObject):
        id: str = prop(primary_key=True)

    class Notice(EffectPayload):
        label: str

    notice = ontology.effect(Notice)

    class EmitParams(ActionParams):
        pass

    @ontology.action(
        EmitParams,
        target=Record,
        roles=["Operator"],
        effects=[notice],
        api_name="Emit",
    )
    def emit(ctx: ActionContext, _params: EmitParams) -> dict[str, str]:
        ctx.emit(Notice(label="captured"))
        return {"status": "ok"}

    return ontology, notice


def test_make_store_returns_a_fresh_store_for_the_ontology() -> None:
    ontology = Ontology("testing-store", scope_levels=["org"])

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Record(OntologyObject):
        id: str = prop(primary_key=True)

    first = make_store(ontology)
    second = make_store(ontology)

    assert isinstance(first, InMemoryStore)
    assert first is not second
    assert first.read_all("Record") == []
    assert second.read_all("Record") == []


def test_consumer_factory_builds_the_public_consumer_shape() -> None:
    value = consumer(
        actor_id="actor-7",
        role="Operator",
        scope_level="team",
        scope_id="team-3",
        kind="ai",
        principal="principal-7",
    )

    assert value.actor_id == "actor-7"
    assert value.role == "Operator"
    assert value.scope_level == "team"
    assert value.scope_id == "team-3"
    assert value.kind == "ai"
    assert value.principal == "principal-7"


def test_raises_code_matches_the_code_even_when_message_differs() -> None:
    with raises_code("EXPECTED"):
        raise ValidationFailed("message does not contain the expected code", code="EXPECTED")

    with pytest.raises(AssertionError):
        with raises_code("EXPECTED"):
            raise ValidationFailed("EXPECTED", code="OTHER")


def test_raises_code_rejects_a_block_that_raises_nothing() -> None:
    with pytest.raises(AssertionError, match="no error raised"):
        with raises_code("EXPECTED"):
            pass


def test_raises_code_matches_an_ingest_error_by_its_code() -> None:
    """`IngestError` is coded but not an `OntaryError` subclass; the helper
    must still match on its stable `.code` (review P1-a, T3xT11 seam)."""
    with raises_code("INVALID_RECORD"):
        raise IngestError(index=0, reason="missing required property", code="INVALID_RECORD")

    with pytest.raises(AssertionError):
        with raises_code("INVALID_RECORD"):
            raise IngestError(index=0, reason="unknown type", code="UNKNOWN_OBJECT_TYPE")


def test_raises_code_propagates_an_uncoded_exception_unchanged() -> None:
    with pytest.raises(RuntimeError, match="not a coded refusal"):
        with raises_code("EXPECTED"):
            raise RuntimeError("not a coded refusal")


def test_capture_effects_records_the_effect_without_an_outside_dispatch() -> None:
    ontology, notice = _build_effect_ontology()
    store = make_store(ontology)
    captured = capture_effects()
    client = ontology.bind(store, effects={notice: captured}).for_consumer(
        consumer(role="Operator")
    )

    assert client.execute("Emit", {}) == {"status": "ok"}
    assert len(captured.effects) == 1
    assert captured.records is captured.effects
    payload, meta = captured.effects[0]
    assert payload.model_dump() == {"label": "captured"}
    assert meta.action == "Emit"
    assert meta.attempt == 1


def test_fixed_clock_is_aware_and_constant() -> None:
    start = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)
    clock = FixedClock(start)

    assert clock() == start
    assert clock() == start
    assert clock().tzinfo is not None

    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2099, 1, 1, 12, 0))


def test_sequential_ids_are_deterministic_per_instance() -> None:
    ids = SequentialIds("run")

    assert [ids(), ids(), ids()] == ["run-1", "run-2", "run-3"]
    assert [SequentialIds("run")(), SequentialIds("run")()] == ["run-1", "run-1"]


def test_fixed_clock_and_sequential_ids_make_audit_bytes_identical() -> None:
    fixed = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)

    def run_once() -> list[bytes]:
        ontology, notice = _build_effect_ontology()
        store = make_store(ontology)
        runtime = ontology.bind(
            store,
            clock=FixedClock(fixed),
            id_factory=SequentialIds("run"),
            effects={notice: capture_effects()},
        )
        runtime.for_consumer(consumer(role="Operator")).execute("Emit", {})
        return [
            entry.model_dump_json().encode("utf-8")
            for entry in store.audit_entries()
        ]

    first = run_once()
    second = run_once()

    assert first
    assert first == second


def test_testing_module_imports_only_public_ontary_and_stdlib_names() -> None:
    module_path = Path(ontary.__file__).with_name("testing.py")
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "ontary":
                for alias in node.names:
                    if alias.name.startswith("_") or alias.name not in ontary.__all__:
                        violations.append(f"{module}:{alias.name}")
            elif module != "__future__" and module.split(".", 1)[0] not in sys.stdlib_module_names:
                violations.append(module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] not in sys.stdlib_module_names:
                    violations.append(alias.name)

    assert violations == []
