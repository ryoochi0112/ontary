"""Deterministic scalar/operator grid for the storage prefilter contract."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ontary.store._filter import WhereTerm
from ontary.typesys import PropertyType

INJECTION = "x'); DROP TABLE objects; --"
MISSING = object()


@dataclass(frozen=True)
class StoredValue:
    label: str
    value: Any
    well_formed: bool


@dataclass(frozen=True)
class FilterCase:
    name: str
    family: str
    term: WhereTerm
    values: tuple[StoredValue, ...]


def _values(good: list[Any], bad: list[Any]) -> tuple[StoredValue, ...]:
    return tuple(
        [StoredValue(f"value-{i}", value, True) for i, value in enumerate(good)]
        + [StoredValue("null", None, True), StoredValue("missing", MISSING, True)]
        + [StoredValue(f"malformed-{i}", value, False) for i, value in enumerate(bad)]
    )


STRINGS = ["", "open", "Open", "OPEN", "closed", "%", "_", "\\", "'",
           "a%_\\'b", "aXXb", INJECTION, "before " + INJECTION + " after"]
NUMBERS = [-3, -1, 0, 1, 2, 3, 1.25, 1.5, 1.75]
EXTREME_NUMBERS = [
    sign * magnitude
    for magnitude in (2**53, 2**53 + 1, 2**63 - 1, 2**63, 2**64, 2**100)
    for sign in (1, -1)
] + [float(2**53 + 2), float(2**100), float("nan"), float("inf"), -float("inf")]
DATES = ["2026-02-14", "2026-02-15", "2026-02-16"]
INSTANTS = [
    "2026-02-14T23:59:59.999999Z", "2026-02-15T00:00:00Z",
    "2026-02-15T09:00:00+09:00", "2026-02-15T00:00:00+00:00",
    "2026-02-15T00:00:00.000001Z", "2026-02-15T00:00:00",
    "2026-02-14T23:59:59.999999", "2026-02-15T00:00:00.000001",
]
# json.dumps emits bare NaN/Infinity: invalid JSON for SQL, but still
# decodable by Python, so these exercise invalid-payload guards end to end.
WRONG = [[], {}, ["open"], {"nested": 1}, float("nan"), float("inf"), -float("inf")]

# json scalars use separate families so each operand's scalar comparison
# domain is explicit. Numbers outside SQL's exact domain remain permissive.
FAMILIES: dict[str, tuple[PropertyType, tuple[StoredValue, ...], list[Any]]] = {
    "str": ("str", _values(STRINGS, WRONG + [1, True, "nul\x00tail"]), ["open"]),
    "choices": ("str", _values(["open", "closed"],
                              WRONG + ["Open", "OPEN", 1, True, "nul\x00tail"]), ["open"]),
    "int": ("int", _values([-3, -1, 0, 1, 2, 3, -(2**53), 2**53 - 1, 2**53],
                          WRONG + ["1", True, 1.5] + EXTREME_NUMBERS), [1]),
    "float": ("float", _values(NUMBERS + [0.1, -0.1, 1e-308, 2**53 - 1, 2**53],
                              WRONG + ["1", True] + EXTREME_NUMBERS), [1.5]),
    "bool": ("bool", _values([False, True], WRONG + [0, 1, 2, "true"]), [True, False]),
    "date": ("date", _values(DATES, WRONG + [1, True, "2026-02-30", "20260215",
                                            "garbage", "nul\x00tail"]), [DATES[1]]),
    "datetime": ("datetime", _values(INSTANTS, WRONG + [1, True, "2026-02-30T00:00:00Z",
                                     "garbage", "20260215", "nul\x00tail",
                                     "2026-02-15 00:00:00+00:00"]),
                 [INSTANTS[1], INSTANTS[2], INSTANTS[5]]),
    "json-str": ("json", _values(STRINGS, WRONG + [1, True, "nul\x00tail"]), ["open"]),
    "json-number": ("json", _values(NUMBERS,
                                    WRONG + ["1", True] + EXTREME_NUMBERS), [1, 1.5]),
    "json-bool": ("json", _values([False, True], WRONG + [0, 1, 2, "true"]), [True, False]),
}


def _case(family: str, op: str, operand: Any, suffix: str) -> FilterCase:
    prop_type, values, _ = FAMILIES[family]
    probes = operand if op == "in" else [operand]
    # NUL operands cannot be bound on PG; extreme numeric operands also
    # widen the SQL domain. These rows cannot promise exact SQL equality.
    permissive_operand = any(
        (isinstance(value, str) and "\x00" in value)
        or (isinstance(value, int | float) and
            (not math.isfinite(value) or abs(value) > 2**53))
        for value in probes
    ) if op != "and" else False
    if permissive_operand:
        nul_operand = any(isinstance(value, str) and "\x00" in value for value in probes)
        values = tuple(StoredValue(v.label, v.value,
                       v.well_formed and not nul_operand and
                       (v.value is None or v.value is MISSING)) for v in values)
    return FilterCase(f"{family}-{op}-{suffix}", family,
                      WhereTerm("value", prop_type, op, operand), values)


def _grid() -> tuple[FilterCase, ...]:
    cases = []
    for family, (prop_type, _, operands) in FAMILIES.items():
        cases.append(_case(family, "eq", None, "null"))
        for i, operand in enumerate(operands):
            for op in ("eq", "ne"):
                cases.append(_case(family, op, operand, str(i)))
            cases.append(_case(family, "in", [operand, None], str(i)))
            cases.append(_case(family, "in", [operand], f"nonnull-{i}"))
        cases.append(_case(family, "in", [], "empty"))
        if prop_type == "str":
            for i, operand in enumerate(["open", "Open", "%", "_", "\\", "'",
                                         "%_\\'", "", "nul\x00"]):
                cases.append(_case(family, "contains", operand, str(i)))
        if prop_type in {"int", "float", "date", "datetime"}:
            for i, operand in enumerate(operands):
                for op in ("gt", "gte", "lt", "lte"):
                    cases.append(_case(family, op, operand, str(i)))
                terms = (WhereTerm("value", prop_type, "gte", operand),
                         WhereTerm("value", prop_type, "lte", operand))
                cases.append(_case(family, "and", terms, str(i)))
            low, high = ({"int": (0, 3), "float": (1.25, 1.75),
                          "date": (DATES[0], DATES[2]),
                          "datetime": (INSTANTS[0], INSTANTS[4])})[prop_type]
            cases.append(_case(family, "and", (WhereTerm("value", prop_type, "gt", low),
                              WhereTerm("value", prop_type, "lt", high)), "range"))
        if family in {"int", "float", "json-number"}:
            for i, operand in enumerate(EXTREME_NUMBERS):
                ops = ("eq", "ne", "in")
                if prop_type != "json":
                    ops += ("gt", "gte", "lt", "lte")
                for op in ops:
                    cases.append(_case(family, op,
                                       [operand, None] if op == "in" else operand, f"extreme-{i}"))
        if family in {"str", "choices", "json-str"}:
            for op in ("eq", "ne", "in"):
                cases.append(_case(family, op, ["nul\x00tail", None] if op == "in"
                                   else "nul\x00tail", "nul"))
    return tuple(cases)


FILTER_CASES = _grid()
