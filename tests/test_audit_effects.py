from __future__ import annotations

from ontary.audit import AuditEntry, CapabilityAccessRecord, EffectRecord


def test_audit_effect_record_models_and_empty_defaults() -> None:
    pending = EffectRecord(
        api_name="notify",
        payload={"message": "hello"},
        outcome="pending",
    )
    access = CapabilityAccessRecord(api_name="llm", count=3)

    assert pending.error is None
    assert access.model_dump() == {"api_name": "llm", "count": 3}

    entry = AuditEntry(
        actor="alice",
        role="Member",
        action="NoEffects",
        target_type="Ticket",
        outcome="ok",
    )
    assert entry.effects == []
    assert entry.capability_accesses == []
