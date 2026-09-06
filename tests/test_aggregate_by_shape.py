"""`aggregate_by`'s released dictionary is a faithful shape (remediation Track B).

Three defects shared one method and two root causes.

`aggregate_by` validated only that `group_by` was *non-empty*. Every other
identifier parameter on the read surface -- `where` keys, `order_by`, and the
typed overload's own `group_by` -- is checked against the type's declared
properties; the string-form `group_by` was the one that was not. So an unknown
name became `payload.get(...) -> None` for every row, collapsing the selection
into a single group whose released key was the string `"None"` (B1), and a
`json`-declared name reached `dict.setdefault` with an unhashable value and
raised a bare `TypeError` -- `INTERNAL_ERROR`, "internal server error", once it
crossed the MCP boundary (B3).

Separately, `out[str(key)]` is not injective over the group-key domain: an
absent optional property keys `None` and collides with a row literally carrying
the string `"None"`. The two populations did not merge -- the later one
silently OVERWROTE the earlier, so the released cell described one population
while `func="count"` reported that population's size for a call backed by both
(B2). Which one won was decided by insertion order, identically on sqlite and
Postgres.

B1/B3 are one fix (validate the identifier); B2 survives it and needs its own
(the `str()` cast). They are pinned separately -- as the ledger's B4 requires --
because one shared assertion over the released dictionary could not tell a
later refactor which of the two it had broken.

The inputs this suite's predecessors structurally could not construct:
`group_by` naming a field the type does not declare; a `json`-declared group
key; and two distinct group keys whose `str()` forms are equal, which needs an
OPTIONAL group_by property (so some rows key `None`) beside a row carrying the
literal `"None"`. Every existing `group_by` fixture in the suite names a
required scope-routing property, so none of the three was reachable.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import raises_code

from ontary import (
    BoundQuery,
    Consumer,
    ObjectStore,
    Ontology,
    OntologyObject,
    Sensitivity,
    Source,
    prop,
)
from ontary.client import OntologyClient
from ontary.errors import ValidationFailed, VisibilityError
from ontary.mcp_server import build_mcp_server
from ontary.ontology import OntologyDef
from ontary.query import GuardedQuery

SRC = Source(source_system="test")
CONSUMER = Consumer(
    actor_id="u1",
    role="Member",
    scope_level="org",
    scope_id="org-1",
    kind="human",
)


def _build(min_n: int = 1) -> tuple[OntologyDef, type[OntologyObject], ObjectStore]:
    """A deliberately UNSCOPED type with an OPTIONAL group key and a
    `json` property.

    `min_n` defaults to 1 so the shape defects are visible rather than masked
    by a release floor; the one pin that needs the floor raises it explicitly.
    Optionality is what makes B2 constructible at all -- a required group key
    can never produce the `None` half of the collision.
    """
    ontology = Ontology(name="aggshape", scope_levels=["org"], min_n=min_n)

    @ontology.object(layer="L0", scope="unscoped")
    class Record(OntologyObject):
        id: str = prop(primary_key=True)
        score: float
        group_id: str | None = None
        bucket: dict[str, Any] | None = None
        secret: float | None = prop(
            default=None, sensitivity=Sensitivity(human_visible=False)
        )

    definition = ontology.definition
    return definition, Record, ObjectStore(definition.registry)


def _seed(store: ObjectStore, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        store.insert("Record", row, SRC)


def _surfaces(
    definition: OntologyDef, store: ObjectStore
) -> dict[str, Any]:
    """Every surface grouped aggregation is reachable through.

    `_TypedReadMixin` has four independent branches -- typed and string,
    grouped and ungrouped -- and the ledger records a fix that threaded only
    some of them while the untouched ones stayed green (G2/M6b). A refusal
    that is real on one spelling and absent on another is the failure mode
    this mapping exists to make unmissable.
    """
    guarded = GuardedQuery(store, definition.registry, definition.policy)
    client = OntologyClient(definition, store, CONSUMER)
    return {
        "GuardedQuery": lambda *a, **k: guarded.aggregate_by(CONSUMER, *a, **k),
        "OntologyClient": client.aggregate_by,
        "BoundQuery": BoundQuery(guarded, CONSUMER).aggregate_by,
    }


def _mcp_aggregate(
    definition: OntologyDef, store: ObjectStore, **arguments: Any
) -> Any:
    server = build_mcp_server(definition, store, CONSUMER)
    result = asyncio.run(server.call_tool("aggregate_objects", arguments))
    return result[1]


# -- B1: an unknown `group_by` is refused, not silently collapsed ------------


@pytest.mark.parametrize("surface", ["GuardedQuery", "OntologyClient", "BoundQuery"])
def test_unknown_group_by_refuses_instead_of_collapsing_to_one_group(
    surface: str,
) -> None:
    """The defect: `group_by="group_i"` returned `{"None": <ungrouped mean>}`.

    Every row missed the field, so all six landed in one `None`-keyed group and
    the caller received the ungrouped mean labelled as a group. Nothing was
    disclosed that `aggregate` would not release -- the collapsed cell IS the
    ungrouped mean -- so this is a silent wrong answer, not a leak.
    """
    definition, _Record, store = _build()
    _seed(
        store,
        [
            {"id": f"r{i}", "score": float(i), "group_id": "g1" if i < 3 else "g2"}
            for i in range(6)
        ],
    )

    aggregate_by = _surfaces(definition, store)[surface]
    with raises_code(ValidationFailed, "UNKNOWN_FIELD"):
        aggregate_by("Record", "score", "group_i")


def test_unknown_group_by_refuses_over_the_mcp_wire() -> None:
    definition, _Record, store = _build()
    _seed(store, [{"id": "r0", "score": 1.0, "group_id": "g1"}])

    payload = _mcp_aggregate(
        definition, store, obj_type="Record", value_field="score", group_by="group_i"
    )

    assert payload["error"]["code"] == "UNKNOWN_FIELD"


def test_unknown_group_by_matches_the_where_and_order_by_refusal() -> None:
    """The three identifier parameters of one read surface must agree.

    `where=` and `order_by` already refused this exact name with
    `UNKNOWN_FIELD`; `group_by` alone returned a number. Asserting the codes
    together is what stops a future change from fixing one and leaving the
    others to drift -- a single-parameter pin cannot see that.
    """
    definition, _Record, store = _build()
    _seed(store, [{"id": "r0", "score": 1.0, "group_id": "g1"}])
    guarded = GuardedQuery(store, definition.registry, definition.policy)

    codes = []
    for call in (
        lambda: guarded.get_objects(CONSUMER, "Record", where={"group_i": "g1"}),
        lambda: guarded.get_objects(CONSUMER, "Record", order_by="group_i"),
        lambda: guarded.aggregate_by(CONSUMER, "Record", "score", "group_i"),
    ):
        with pytest.raises(ValidationFailed) as excinfo:
            call()
        codes.append(excinfo.value.code)

    assert codes == ["UNKNOWN_FIELD", "UNKNOWN_FIELD", "UNKNOWN_FIELD"]


def test_a_declared_group_by_still_groups() -> None:
    """The companion to every refusal above: grouping itself is untouched.

    Without this, "refuse an unknown `group_by`" is indistinguishable from
    "refuse `group_by`", and the mutation that deletes grouping outright would
    leave the four pins above green.
    """
    definition, Record, store = _build()
    _seed(
        store,
        [
            {"id": f"r{i}", "score": float(i), "group_id": "g1" if i < 3 else "g2"}
            for i in range(6)
        ],
    )
    client = OntologyClient(definition, store, CONSUMER)

    assert client.aggregate_by("Record", "score", "group_id") == {
        "g1": pytest.approx(1.0),
        "g2": pytest.approx(4.0),
    }
    assert client.aggregate_by(Record, "score", "group_id") == {
        "g1": pytest.approx(1.0),
        "g2": pytest.approx(4.0),
    }


def test_hidden_group_by_still_refuses_on_visibility_not_existence() -> None:
    """A declared-but-hidden group key keeps its VISIBILITY refusal.

    The new existence check runs first and must not swallow it: reporting
    `UNKNOWN_FIELD` for a field the ontology DOES declare would tell a
    consumer that a hidden field does not exist, and would retire the
    `group_by_hidden` gate by making it unreachable (rstaff L31 -- additive
    only).
    """
    definition, _Record, store = _build()
    _seed(store, [{"id": "r0", "score": 1.0, "secret": 9.0}])
    guarded = GuardedQuery(store, definition.registry, definition.policy)

    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        guarded.aggregate_by(CONSUMER, "Record", "score", "secret")


# -- B3: a `json`-declared group key is refused with a catalogued code -------


@pytest.mark.parametrize("surface", ["GuardedQuery", "OntologyClient", "BoundQuery"])
@pytest.mark.parametrize(
    "value", [{"k": "v"}, ["a"]], ids=["dict", "list"]
)
def test_json_declared_group_by_refuses_with_a_catalogued_code(
    surface: str, value: Any
) -> None:
    """The defect: `TypeError: cannot use 'dict' as a dict key`, raised from
    `dict.setdefault` deep inside `_aggregate` -- a bare Python exception where
    the sibling gate for `value_field` already produced a coded refusal."""
    definition, _Record, store = _build()
    _seed(store, [{"id": f"j{i}", "score": 1.0, "bucket": value} for i in range(3)])

    aggregate_by = _surfaces(definition, store)[surface]
    with raises_code(ValidationFailed, "INVALID_GROUP_BY"):
        aggregate_by("Record", "score", "bucket")


def test_json_group_by_refuses_on_the_declaration_not_the_stored_values() -> None:
    """Refused even when every stored value happens to be a hashable scalar.

    This is the pin that distinguishes a DECLARATION check from a rescued
    `TypeError`. A fix that merely caught the unhashable case would leave a
    `json` group key working on scalar data and failing on the first dict a
    connector delivered -- the same data-dependent silence the defect had.
    """
    definition, _Record, store = _build()
    _seed(store, [{"id": f"s{i}", "score": 1.0, "bucket": i} for i in range(3)])

    client = OntologyClient(definition, store, CONSUMER)
    with raises_code(ValidationFailed, "INVALID_GROUP_BY"):
        client.aggregate_by("Record", "score", "bucket")


def test_json_group_by_is_a_validation_error_not_an_internal_one_over_mcp() -> None:
    """Over the wire the bare `TypeError` reached `_try`'s catch-all and
    became `INTERNAL_ERROR` / "internal server error", so an ordinary
    declaration mistake was reported to the consumer as an engine fault with
    no way to tell the two apart."""
    definition, _Record, store = _build()
    _seed(store, [{"id": "j0", "score": 1.0, "bucket": {"k": "v"}}])

    payload = _mcp_aggregate(
        definition, store, obj_type="Record", value_field="score", group_by="bucket"
    )

    assert payload["error"]["code"] == "INVALID_GROUP_BY"
    assert payload["error"]["kind"] == "validation"


def test_json_declared_value_field_keeps_its_own_refusal() -> None:
    """`value_field` and `group_by` refuse a `json` property with DIFFERENT
    codes, and must keep doing so: the value gate is about arithmetic
    (`NON_NUMERIC_AGGREGATE`), the key gate about hashability."""
    definition, _Record, store = _build()
    _seed(store, [{"id": "j0", "score": 1.0, "bucket": {"k": "v"}, "group_id": "g"}])

    client = OntologyClient(definition, store, CONSUMER)
    with raises_code(ValidationFailed, "NON_NUMERIC_AGGREGATE"):
        client.aggregate_by("Record", "bucket", "group_id")


# -- B2: distinct group keys never collapse into one released cell -----------


@pytest.mark.parametrize("surface", ["GuardedQuery", "OntologyClient", "BoundQuery"])
def test_absent_group_key_does_not_collide_with_the_literal_string_none(
    surface: str,
) -> None:
    """The defect: two populations, one released cell, no signal.

    Rows missing the optional `group_id` key `None`; rows carrying the string
    `"None"` key `"None"`; `str()` maps both to `"None"` and the later
    assignment won. Measured `{'None': 99.0}` for populations whose means were
    1.0 and 99.0 -- the 1.0 population vanished from a dictionary that gave no
    hint it had ever existed.
    """
    definition, _Record, store = _build()
    _seed(
        store,
        [
            *[{"id": f"a{i}", "score": 1.0} for i in range(3)],
            *[{"id": f"b{i}", "score": 99.0, "group_id": "None"} for i in range(3)],
        ],
    )

    aggregate_by = _surfaces(definition, store)[surface]
    with raises_code(ValidationFailed, "GROUP_KEY_COLLISION"):
        aggregate_by("Record", "score", "group_id")


def test_group_key_collision_refuses_whichever_population_was_inserted_first() -> None:
    """Which population survived was decided by insertion order -- nothing the
    caller supplied, observed, or could have known. Both orders are pinned
    because a fix that only ever saw one of them would look correct while the
    other still released a number.
    """
    absent_first = [
        *[{"id": f"a{i}", "score": 1.0} for i in range(3)],
        *[{"id": f"b{i}", "score": 99.0, "group_id": "None"} for i in range(3)],
    ]
    literal_first = [
        *[{"id": f"b{i}", "score": 99.0, "group_id": "None"} for i in range(3)],
        *[{"id": f"a{i}", "score": 1.0} for i in range(3)],
    ]

    for rows in (absent_first, literal_first):
        definition, _Record, store = _build()
        _seed(store, rows)
        client = OntologyClient(definition, store, CONSUMER)
        with raises_code(ValidationFailed, "GROUP_KEY_COLLISION"):
            client.aggregate_by("Record", "score", "group_id")


def test_group_key_collision_refuses_the_count_that_under_reported() -> None:
    """`func="count"` was the sharpest symptom and gets its own pin.

    It returned `{'None': 3}` for a call backed by six rows, while the
    ungrouped `count` over the same selection returned 6 -- a self-inconsistent
    pair from one selection. Pinning only the mean would leave a fix free to
    thread the mean path and not this one.
    """
    definition, _Record, store = _build()
    _seed(
        store,
        [
            *[{"id": f"a{i}", "score": 1.0} for i in range(3)],
            *[{"id": f"b{i}", "score": 99.0, "group_id": "None"} for i in range(3)],
        ],
    )
    client = OntologyClient(definition, store, CONSUMER)

    assert client.aggregate("Record", "score", func="count") == 6
    with raises_code(ValidationFailed, "GROUP_KEY_COLLISION"):
        client.aggregate_by("Record", "score", "group_id", func="count")


def test_group_key_collision_refuses_over_the_mcp_wire() -> None:
    definition, _Record, store = _build()
    _seed(
        store,
        [
            *[{"id": f"a{i}", "score": 1.0} for i in range(3)],
            *[{"id": f"b{i}", "score": 99.0, "group_id": "None"} for i in range(3)],
        ],
    )

    payload = _mcp_aggregate(
        definition, store, obj_type="Record", value_field="score", group_by="group_id"
    )

    assert payload["error"]["code"] == "GROUP_KEY_COLLISION"


def test_an_absent_group_key_alone_still_releases_its_group() -> None:
    """Grouping on an optional property is a legitimate query and stays one.

    Without this pin, "refuse the collision" and "refuse an absent group key"
    are the same green, and the cheaper wrong fix -- drop rows that lack the
    property, or refuse the `None` group outright -- would pass every
    assertion above.
    """
    definition, _Record, store = _build()
    _seed(
        store,
        [
            *[{"id": f"a{i}", "score": 1.0} for i in range(3)],
            *[{"id": f"b{i}", "score": 99.0, "group_id": "real"} for i in range(3)],
        ],
    )
    client = OntologyClient(definition, store, CONSUMER)

    assert client.aggregate_by("Record", "score", "group_id") == {
        "None": pytest.approx(1.0),
        "real": pytest.approx(99.0),
    }


def test_group_key_collision_does_not_release_a_sub_floor_population() -> None:
    """min-N still wins over the collision refusal.

    min-N is measured per PRE-collision group, so the collision never smuggled
    a sub-threshold population out -- the bound that keeps B2 a correctness
    defect rather than a disclosure one. A collision check placed after the
    floor would invert that, and this is the assertion that would catch it.
    """
    definition, _Record, store = _build(min_n=3)
    _seed(
        store,
        [
            {"id": "a0", "score": 100.0},
            *[{"id": f"b{i}", "score": 1.0, "group_id": "None"} for i in range(3)],
        ],
    )
    client = OntologyClient(definition, store, CONSUMER)

    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        client.aggregate_by("Record", "score", "group_id")


# -- an unregistered object type refuses with the code the catalogue declares


@pytest.mark.parametrize(
    "call_name",
    ["aggregate", "aggregate_by", "count_contributors"],
)
def test_unregistered_object_type_refuses_on_every_aggregate_surface(
    call_name: str,
) -> None:
    """Found while re-deriving B1, and the same root: identifiers were not
    validated.

    `aggregate`, `aggregate_by` and `count_contributors` answered
    `MIN_N_VIOLATION` for a type the registry has never heard of -- naming a
    threshold for a population that cannot exist, and contradicting the
    catalogue's own `UNKNOWN_OBJECT_TYPE` row. The same three calls WITH a
    `where=` already refused correctly, so one method disagreed with itself
    depending on an unrelated argument.
    """
    definition, _Record, store = _build()
    _seed(store, [{"id": "r0", "score": 1.0, "group_id": "g1"}])
    client = OntologyClient(definition, store, CONSUMER)
    calls = {
        "aggregate": lambda: client.aggregate("Nope", "score"),
        "aggregate_by": lambda: client.aggregate_by("Nope", "score", "group_id"),
        "count_contributors": lambda: client.count_contributors("Nope"),
    }

    with raises_code(ValidationFailed, "UNKNOWN_OBJECT_TYPE"):
        calls[call_name]()


def test_unregistered_object_type_agrees_with_the_where_form() -> None:
    """The anchor for the pin above: the `where=`-bearing spelling of the same
    call already refused this way, and the two spellings must not diverge
    again."""
    definition, _Record, store = _build()
    _seed(store, [{"id": "r0", "score": 1.0, "group_id": "g1"}])
    client = OntologyClient(definition, store, CONSUMER)

    for call in (
        lambda: client.aggregate("Nope", "score"),
        lambda: client.aggregate("Nope", "score", {"group_id": "g1"}),
        lambda: client.count_contributors("Nope"),
        lambda: client.count_contributors("Nope", {"group_id": "g1"}),
    ):
        with raises_code(ValidationFailed, "UNKNOWN_OBJECT_TYPE"):
            call()
