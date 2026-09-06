"""Tests for ontary.connect.canonical: lineage-stamped canonical staging
schema + connector contract (spec connector-framework §5 canonical.py, §3
AC1-AC2, §7 edge cases)."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from ontary.connect import (
    CanonicalBatch,
    CanonicalRecord,
    SourceConnector,
    SourceLineage,
)


class Widget(CanonicalRecord):
    """A toy canonical record used only to exercise the base class."""

    widget_key: str


def make_lineage(source_system: str = "acme") -> SourceLineage:
    return SourceLineage(source_system=source_system, extracted_at=datetime(2026, 1, 1))


def test_canonical_record_without_lineage_is_rejected() -> None:
    """AC1: a canonical record without lineage fails at model construction."""
    with pytest.raises(ValidationError):
        Widget(widget_key="w1")  # type: ignore[call-arg]


def test_canonical_record_with_lineage_constructs() -> None:
    widget = Widget(widget_key="w1", lineage=make_lineage())
    assert widget.widget_key == "w1"
    assert widget.lineage.source_system == "acme"
    assert widget.lineage.source_id is None


def test_source_lineage_requires_source_system_and_extracted_at() -> None:
    with pytest.raises(ValidationError):
        SourceLineage(extracted_at=datetime(2026, 1, 1))  # type: ignore[call-arg]


def test_canonical_batch_accepts_partial_batches() -> None:
    """AC2: a source that only has widgets emits only widgets."""
    widget = Widget(widget_key="w1", lineage=make_lineage())
    batch = CanonicalBatch(entities={"widgets": [widget]})
    assert batch.entities == {"widgets": [widget]}
    assert batch.get("widgets") == [widget]
    assert batch.get("missing_entity") == []


def test_canonical_batch_defaults_to_empty() -> None:
    batch = CanonicalBatch()
    assert batch.entities == {}


class IncompleteConnector(SourceConnector):
    """A connector that inherits the protocol's default (raising) bodies."""

    name = "incomplete"


def test_source_connector_defaults_raise_not_implemented() -> None:
    """AC2: protocol defaults raise NotImplementedError loudly."""
    connector = IncompleteConnector()
    with pytest.raises(NotImplementedError):
        connector.extract()
    with pytest.raises(NotImplementedError):
        connector.transform({})
