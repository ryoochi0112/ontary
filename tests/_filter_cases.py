"""Deterministic scalar/operator grid for the storage prefilter contract."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import IntEnum
from typing import Any

from ontary.store._filter import BindDomain, WhereTerm, pushable
from ontary.typesys import PropertyType, validate_scalar

INJECTION = "x'); DROP TABLE objects; --"
SURROGATES = ("\ud800", "\udfff", "prefix\ud800suffix")
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


def _surrogate_cases() -> tuple[FilterCase, ...]:
    cases = []
    # Unbindable operands deliberately widen the entire clause to TRUE.
    # These new cases promise containment, with Python deciding final matches.
    for family in ("str", "choices", "json-str"):
        prop_type, values, _ = FAMILIES[family]
        permissive_values = tuple(StoredValue(v.label, v.value, False) for v in values)
        for i, value in enumerate(SURROGATES):
            for op, operand in (("eq", value), ("ne", value), ("contains", value),
                                ("in", ["open", value, None])):
                cases.append(FilterCase(f"{family}-{op}-surrogate-{i}", family,
                                        WhereTerm("value", prop_type, op, operand),
                                        permissive_values))
    return tuple(cases)


FILTER_CASES = _grid() + _surrogate_cases()


CORPUS_SEED = 680_005
CORPUS_COUNT = 2_048


class CorpusInt(IntEnum):
    ONE = 1


class CorpusText(str):
    pass


@dataclass(frozen=True)
class ConsumerFilterCase:
    index: int
    where: dict[str, Any]
    scope_id: Any
    path: str
    field: str
    operator: str
    operand_kind: str


def consumer_filter_cases(domain: BindDomain):
    """Draw validation-passing per-field operands, split by the bind domain.

    Half the scalar draws use each field's outside pool. Mixed `in` lists
    alternate domains, with a fixed share of large lists. A small refusal
    sample retains wrong-type coverage through the same public read path.
    """
    rng = random.Random(CORPUS_SEED)
    strings = ["", "owned", "foreign", "open", "closed", "café", "中",
               "nul\x00tail", "\ud800", "\udfff", "\ud800x", "x" * 70_000,
               CorpusText("open")]
    integers = [-3, 0, 1, 7, -(2**63), 2**63 - 1,
                2**63, -(2**63) - 1, 10**5000, CorpusInt.ONE]
    floats = [-1.25, 0.0, 1.5, float("nan"), float("inf"), -float("inf")]
    wrong = [[], {"nested": 1}, CorpusInt.ONE, CorpusText("open"),
             datetime(2026, 2, 15, tzinfo=timezone.utc), Decimal("1.5")]
    values = {
        "i": integers,
        "f": floats,
        "s": strings,
        "j": [*integers, *floats, *strings, [], {"nested": 1}],
        "dt": ["2026-02-14T00:00:00+00:00", "2026-02-15T00:00:00+00:00",
               datetime(2026, 2, 15, tzinfo=timezone.utc)],
    }
    field_types = {"i": "int", "f": "float", "s": "str", "j": "json", "dt": "datetime"}
    pools = {}
    for field, candidates in values.items():
        valid = [v for v in candidates if validate_scalar(v, field_types[field]) is None]
        pools[field] = ([v for v in valid if pushable(v, domain)],
                        [v for v in valid if not pushable(v, domain)])
    scope_inside = [v for v in strings if type(v) is str and pushable(v, domain)]
    scope_outside = [v for v in strings if type(v) is str and not pushable(v, domain)]

    def draw(field, outside):
        return rng.choice(pools[field][int(outside)])

    operators = ("eq", "ne", "in", "gt", "gte", "in", "lt", "lte", "contains", "and")
    paths = ("unbounded", "page", "count", "aggregate")
    for index in range(CORPUS_COUNT):
        op = operators[index % len(operators)]
        field = ("s" if op == "contains" else rng.choice(("i", "f", "dt"))
                 if op in {"gt", "gte", "lt", "lte", "and"}
                 else rng.choice(("i", "j", "s", "f", "dt")))
        outside = (index + index // len(operators)) % 2 == 1
        operand = draw(field, outside)
        if op == "eq" and isinstance(operand, dict):
            # A bare mapping is public operator syntax, not JSON equality.
            op = "ne"
        if op == "in":
            ordinal = index // 5
            length = (rng.randint(1_000, 3_000) if ordinal % 2 == 0 else
                      rng.choice((0, 1, 2, 3, rng.randrange(3_001))))
            if index == 2:
                length = 3_000
            elif index == 5:
                length = 0
            operand = [draw(field, n % 2 == 1) for n in range(length)]
        # Refused inputs are deliberate and sparse; accepted subclasses also
        # remain in the field pools to test their consumer normalization.
        if index % 31 == 0:
            operand = wrong[(index // 31) % len(wrong)]
        condition = (operand if op == "eq" else
                     {"gte": operand, "lte": draw(field, not outside)} if op == "and" else
                     {op: operand})
        where = {field: condition}
        if rng.randrange(4) == 0:
            extra = "s" if field != "s" else "i"
            where[extra] = draw(extra, not outside)
        scope_id = (rng.choice(scope_outside) if (index // 2) % 2 else
                    rng.choice(["owned", "owned", *scope_inside]))
        if index % 127 == 0:
            scope_id = rng.choice((None, 7, []))
        kind = (f"list[{len(operand)}]" if isinstance(operand, list)
                else type(operand).__name__)
        yield ConsumerFilterCase(index, where, scope_id,
                                 paths[(index // len(operators)) % len(paths)], field, op, kind)
