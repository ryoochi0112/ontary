"""Tests for `ontary.connect.base`: `BaseConnector` + the shared date/
datetime coercion utilities (spec connector-framework.md AC11, ontary task
T10)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from inspect import isabstract
from typing import Any

import pytest

from ontary.connect import (
    BaseConnector,
    CanonicalBatch,
    RawTables,
    SourceConnector,
    to_date,
    to_datetime,
    to_optional_date,
    to_optional_datetime,
)


class Widget(BaseConnector):
    name = "widgets"

    def extract(self) -> RawTables:
        return {}

    def transform(self, raw: RawTables) -> CanonicalBatch:
        return CanonicalBatch()


def test_base_connector_lineage_stamps_name_and_extracted_at() -> None:
    connector = Widget(extracted_at=datetime(2026, 1, 1, 12, 0, 0))
    lineage = connector.lineage("w1")
    assert lineage.source_system == "widgets"
    assert lineage.source_id == "w1"
    assert lineage.extracted_at == datetime(2026, 1, 1, 12, 0, 0)


def test_base_connector_lineage_is_deterministic_across_calls() -> None:
    extracted_at = datetime(2026, 1, 1)
    connector = Widget(extracted_at=extracted_at)
    assert connector.lineage("a") == connector.lineage("a")
    assert connector.lineage("a") != connector.lineage("b")


def test_base_connector_extract_default_raises_not_implemented() -> None:
    class Incomplete(BaseConnector):
        name = "incomplete"

    connector = Incomplete(extracted_at=datetime(2026, 1, 1))
    with pytest.raises(NotImplementedError):
        connector.extract()


@pytest.mark.parametrize("base", [BaseConnector, SourceConnector])
def test_concrete_connector_name_is_checked_at_class_definition(base: type[Any]) -> None:
    class TestConnector(base):
        name = "test"

        def extract(self) -> RawTables:
            return {}

        def transform(self, raw: RawTables) -> CanonicalBatch:
            return CanonicalBatch()

    assert TestConnector.name == "test"

    with pytest.raises(TypeError, match=r"TestConnector.*name"):

        class TestConnector(base):
            def extract(self) -> RawTables:
                return {}

            def transform(self, raw: RawTables) -> CanonicalBatch:
                return CanonicalBatch()


@pytest.mark.parametrize("base", [BaseConnector, SourceConnector])
def test_abstract_connector_without_name_is_definable(base: type[Any]) -> None:
    class AbstractConnector(base, ABC):
        @abstractmethod
        def transform(self, raw: RawTables) -> CanonicalBatch:
            raise NotImplementedError

    assert isabstract(AbstractConnector)


# -- to_datetime / to_optional_datetime --------------------------------------


def test_to_datetime_accepts_datetime_passthrough() -> None:
    value = datetime(2026, 1, 1, 9, 30)
    assert to_datetime(value) is value


def test_to_datetime_accepts_iso8601_string() -> None:
    assert to_datetime("2026-01-01T09:30:00") == datetime(2026, 1, 1, 9, 30)


def test_to_datetime_rejects_malformed_input() -> None:
    bad: Any = 12345
    with pytest.raises(TypeError):
        to_datetime(bad)


def test_to_datetime_rejects_malformed_string() -> None:
    with pytest.raises(ValueError):
        to_datetime("not-a-datetime")


def test_to_optional_datetime_passes_through_none() -> None:
    assert to_optional_datetime(None) is None


def test_to_optional_datetime_parses_non_none() -> None:
    assert to_optional_datetime("2026-01-01T00:00:00") == datetime(2026, 1, 1)


# -- to_date / to_optional_date ----------------------------------------------


def test_to_date_accepts_date_passthrough() -> None:
    value = date(2026, 1, 1)
    assert to_date(value) is value


def test_to_date_downcasts_datetime() -> None:
    assert to_date(datetime(2026, 1, 1, 9, 30)) == date(2026, 1, 1)


def test_to_date_accepts_iso8601_string() -> None:
    assert to_date("2026-01-01") == date(2026, 1, 1)


def test_to_date_rejects_malformed_input() -> None:
    bad: Any = 12345
    with pytest.raises(TypeError):
        to_date(bad)


def test_to_date_rejects_malformed_string() -> None:
    with pytest.raises(ValueError):
        to_date("not-a-date")


def test_to_optional_date_passes_through_none() -> None:
    assert to_optional_date(None) is None


def test_to_optional_date_parses_non_none() -> None:
    assert to_optional_date("2026-01-01") == date(2026, 1, 1)
