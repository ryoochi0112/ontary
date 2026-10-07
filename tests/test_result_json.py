"""The shared Function/Action result encoder (#167)."""

from __future__ import annotations

import copy
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

import pytest

from ontary._result_json import encode_result
from ontary.errors import PreconditionFailed

AWARE = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
NAIVE = datetime(2026, 10, 6, 9, 0)
DAY = date(2026, 10, 6)

VALUES = {
    "aware": (AWARE, "2026-10-06T09:00:00+00:00"),
    "naive": (NAIVE, "2026-10-06T09:00:00"),
    "date": (DAY, "2026-10-06"),
}


def _shapes(value: Any) -> dict[str, Any]:
    return {
        "top": value,
        "dict": {"at": value},
        "list": [value],
        "list_of_dicts": [{"at": value}, {"at": value}],
    }


@pytest.mark.parametrize("shape", ["top", "dict", "list", "list_of_dicts"])
@pytest.mark.parametrize("kind", sorted(VALUES))
def test_dates_encode_to_iso_in_every_nesting_shape(kind: str, shape: str) -> None:
    value, iso = VALUES[kind]
    result = _shapes(value)[shape]
    snapshot = copy.deepcopy(result)

    encoded = encode_result("fn", result, require_dict=False)

    assert encoded == _shapes(iso)[shape]
    assert result == snapshot  # input not mutated
    assert type(result) is type(snapshot)


def test_input_object_is_not_mutated_and_copy_is_returned() -> None:
    inner = {"at": AWARE}
    result = {"rows": [inner]}
    encoded = encode_result("fn", result, require_dict=True)
    assert inner == {"at": AWARE}
    assert result["rows"][0] is inner
    assert encoded is not result
    assert encoded["rows"][0] is not inner


def test_encoding_matches_the_store_spelling() -> None:
    from ontary.typesys import _to_storage_scalar

    assert encode_result("fn", AWARE, require_dict=False) == _to_storage_scalar(
        AWARE, "datetime"
    )
    assert encode_result("fn", DAY, require_dict=False) == _to_storage_scalar(
        DAY, "date"
    )


class Plain(Enum):
    A = "a"


class Mixin(str, Enum):
    A = "a"


@pytest.mark.parametrize("value", [True, 1, 0, 0.5, None, "x"])
def test_json_scalars_pass_unchanged(value: Any) -> None:
    out = encode_result("fn", value, require_dict=False)
    assert out == value
    assert type(out) is type(value)


def test_str_mixin_enum_passes_unchanged() -> None:
    out = encode_result("fn", {"s": Mixin.A}, require_dict=True)
    assert out["s"] is Mixin.A


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            {"rows": [{"when": {1, 2}}]},
            "'fn': result is not JSON-serializable at "
            'result["rows"][0]["when"] (set)',
        ),
        (
            {"rows": [(1, 2)]},
            "'fn': result is not JSON-serializable at "
            'result["rows"][0] (tuple)',
        ),
        (
            {"x": float("inf")},
            "'fn': result is not JSON-serializable at "
            'result["x"] (non-finite float)',
        ),
        (
            [float("nan")],
            "'fn': result is not JSON-serializable at result[0] "
            "(non-finite float)",
        ),
        (
            {"rows": [{1: "a"}]},
            "'fn': result is not JSON-serializable at "
            'result["rows"][0] (non-str key 1)',
        ),
        (
            {"e": Plain.A},
            "'fn': result is not JSON-serializable at "
            'result["e"] (Plain)',
        ),
        (
            [Decimal("1.5")],
            "'fn': result is not JSON-serializable at result[0] (Decimal)",
        ),
    ],
)
def test_refusals_name_handler_path_and_type(result: Any, expected: str) -> None:
    with pytest.raises(PreconditionFailed) as info:
        encode_result("fn", result, require_dict=False)
    assert str(info.value) == expected
    assert info.value.code == "RESULT_NOT_JSON"
    assert info.value.kind == "precondition"


@pytest.mark.parametrize("result", [[1], None, "s", 3])
def test_require_dict_refuses_non_dict(result: Any) -> None:
    with pytest.raises(PreconditionFailed) as info:
        encode_result("act", result, require_dict=True)
    assert str(info.value) == (
        f"'act': handler returned {type(result).__name__}; "
        "action results must be JSON-safe dictionaries"
    )
    assert info.value.code == "RESULT_NOT_JSON"
    assert info.value.kind == "precondition"


def test_require_dict_accepts_dict() -> None:
    assert encode_result("act", {"a": 1}, require_dict=True) == {"a": 1}
