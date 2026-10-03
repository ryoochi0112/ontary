"""Unit tests for the shared store-clock building blocks (#46)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ontary import _runtime
from ontary.errors import PreconditionFailed
from ontary.store._shared import StoreClock, WriteCapture, ensure_not_before, iso_instant
from ontary.store.values import _utcnow_iso
from ontary.testing import FixedClock, raises_code

UTC_T = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_bind_none_without_installed_returns_default_and_installs_nothing() -> None:
    sc = StoreClock()
    assert sc.bind(None) is _runtime.default_clock
    assert sc.bind(None) is _runtime.default_clock


def test_bind_none_with_installed_returns_installed() -> None:
    sc = StoreClock()
    c = FixedClock(UTC_T)
    sc.bind(c)
    assert sc.bind(None) is c


def test_bind_installs_when_none_installed() -> None:
    sc = StoreClock()
    c = FixedClock(UTC_T)
    assert sc.bind(c) is c


def test_bind_same_clock_is_idempotent() -> None:
    sc = StoreClock()
    c = FixedClock(UTC_T)
    sc.bind(c)
    assert sc.bind(c) is c


def test_bind_different_clock_conflicts() -> None:
    sc = StoreClock()
    a, b = FixedClock(UTC_T), FixedClock(UTC_T)
    sc.bind(a)
    with raises_code("CLOCK_CONFLICT"):
        sc.bind(b)
    with pytest.raises(PreconditionFailed) as info:
        sc.bind(b)
    assert repr(a) in str(info.value) and repr(b) in str(info.value)


def test_capture_instant_wins_over_clock() -> None:
    sc, cap = StoreClock(), WriteCapture()
    sc.bind(FixedClock(UTC_T))
    with cap.capture(at="2020-01-01T00:00:00.000000+00:00"):
        assert sc.now_iso(cap) == "2020-01-01T00:00:00.000000+00:00"
    assert sc.now_iso(cap) == iso_instant(UTC_T)


def test_now_iso_defaults_to_runtime_clock() -> None:
    sc, cap = StoreClock(), WriteCapture()
    assert len(sc.now_iso(cap)) == 32


def test_naive_refused_everywhere() -> None:
    naive = datetime(2026, 1, 1)
    with raises_code("CLOCK_NOT_TIMEZONE_AWARE"):
        iso_instant(naive)
    sc, cap = StoreClock(), WriteCapture()
    sc.bind(lambda: naive)
    with raises_code("CLOCK_NOT_TIMEZONE_AWARE"):
        sc.now_iso(cap)


def test_non_utc_converted_to_utc() -> None:
    jst = timezone(timedelta(hours=9))
    assert iso_instant(datetime(2026, 1, 2, 12, 0, tzinfo=jst)) == (
        "2026-01-02T03:00:00.000000+00:00"
    )


def test_spelling_matches_utcnow_iso() -> None:
    mine, theirs = iso_instant(UTC_T), _utcnow_iso()
    assert len(mine) == len(theirs) == 32
    assert mine.endswith("+00:00") and theirs.endswith("+00:00")
    assert mine[10] == theirs[10] == "T"


def test_ensure_not_before() -> None:
    with pytest.raises(PreconditionFailed) as info:
        ensure_not_before(
            "2026-01-02T00:00:00.000000+00:00",
            "2026-01-01T00:00:00.000000+00:00",
            what="Ticket t-1",
        )
    assert info.value.code == "CLOCK_REGRESSION"
    assert "Ticket t-1" in str(info.value)
    assert "2026-01-02T00:00:00" in str(info.value)
    assert "2026-01-01T00:00:00" in str(info.value)
    same = "2026-01-02T00:00:00.000000+00:00"
    ensure_not_before(same, same, what="x")


def test_ensure_not_before_accepts_short_legacy_spelling() -> None:
    ensure_not_before("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01.000000+00:00", what="x")
    with raises_code("CLOCK_REGRESSION"):
        ensure_not_before("2026-01-01T00:00:01+00:00", "2026-01-01T00:00:00.500000+00:00", what="x")


def test_capture_clears_on_exit_and_exception() -> None:
    cap = WriteCapture()
    assert cap.instant is None
    with cap.capture(at="a"):
        assert cap.instant == "a"
    assert cap.instant is None
    with pytest.raises(RuntimeError):
        with cap.capture(at="b"):
            raise RuntimeError("boom")
    assert cap.instant is None
    assert cap.active is False
    with cap.capture():
        assert cap.instant is None
