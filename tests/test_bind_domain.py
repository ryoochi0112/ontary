from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import IntEnum

import pytest

from ontary.store._filter import SQLITE_DOMAIN, BindDomain, pushable


class _IntValue(IntEnum):
    ONE = 1


class _StringValue(str):
    pass


_DOMAINS = [
    pytest.param(SQLITE_DOMAIN, 0, id="sqlite"),
    pytest.param(BindDomain(("utf-8", "iso8859-1")), 1, id="utf8-latin1"),
    pytest.param(BindDomain(("ascii",)), 2, id="ascii"),
]

_CASES = [
    pytest.param(None, (True, True, True), id="none"),
    pytest.param(True, (True, True, True), id="true"),
    pytest.param(False, (True, True, True), id="false"),
    pytest.param(0, (True, True, True), id="zero"),
    pytest.param(2**63 - 1, (True, True, True), id="max-int64"),
    pytest.param(-(2**63), (True, True, True), id="min-int64"),
    pytest.param(2**63, (False, False, False), id="above-int64"),
    pytest.param(-(2**63) - 1, (False, False, False), id="below-int64"),
    pytest.param(10**5000, (False, False, False), id="huge-int"),
    pytest.param(1.5, (True, True, True), id="finite-float"),
    pytest.param(float("nan"), (False, False, False), id="nan"),
    pytest.param(float("inf"), (False, False, False), id="positive-infinity"),
    pytest.param(float("-inf"), (False, False, False), id="negative-infinity"),
    pytest.param("abc", (True, True, True), id="ascii-text"),
    pytest.param("", (True, True, True), id="empty-text"),
    pytest.param("a\x00b", (False, False, False), id="nul-text"),
    pytest.param("\ud800", (False, False, False), id="leading-surrogate"),
    pytest.param("x\udfff", (False, False, False), id="trailing-surrogate"),
    pytest.param("中", (True, False, False), id="cjk-text"),
    pytest.param("é", (True, True, False), id="latin-text"),
    pytest.param("a" * 65_536, (True, True, True), id="max-bytes"),
    pytest.param("a" * 65_537, (False, False, False), id="over-max-bytes"),
    pytest.param("中" * 21_846, (False, False, False), id="over-max-utf8-bytes"),
    pytest.param(_IntValue.ONE, (False, False, False), id="int-enum"),
    pytest.param(_StringValue("abc"), (False, False, False), id="str-subclass"),
    pytest.param(datetime(2026, 1, 1), (False, False, False), id="datetime"),
    pytest.param(Decimal("1"), (False, False, False), id="decimal"),
    pytest.param([1], (False, False, False), id="list"),
    pytest.param({"a": 1}, (False, False, False), id="dict"),
    pytest.param((1,), (False, False, False), id="tuple"),
]


@pytest.mark.parametrize("domain,domain_index", _DOMAINS)
@pytest.mark.parametrize("value,expected", _CASES)
def test_pushable_accepts_only_values_in_the_bind_domain(
    domain: BindDomain, domain_index: int, value: object,
    expected: tuple[bool, bool, bool],
) -> None:
    assert pushable(value, domain) is expected[domain_index]


@pytest.mark.parametrize(
    "value,domain",
    [
        pytest.param(object(), SQLITE_DOMAIN, id="unsupported-value"),
        pytest.param("abc", BindDomain(("not-a-codec",)), id="unknown-encoding"),
    ],
)
def test_pushable_returns_false_instead_of_raising(
    value: object, domain: BindDomain,
) -> None:
    assert pushable(value, domain) is False
