"""A read's identifier and its `where=` are interpreted, never handed to Python
(remediation Track F4 + F5).

Two parameters on the same six read surfaces shared one failure mode: a value
the engine could not interpret was passed through to Python instead of being
refused with a catalogued code. Both were filed as low-priority curiosities and
both were understated.

**F4 -- `value_field`.** `_property_type` returned `None` for a name the type
does not declare, and the declared-type gate was guarded by `prop_type is not
None`, so the gate was skipped entirely rather than failing closed. The filed
symptom -- `MIN_N_VIOLATION` naming a field that does not exist -- is only the
half where no row carries the name. The store DELIBERATELY accepts undeclared
payload keys (`meta.declared_shape_violation`: a `ViaLink`-scoped row may
legitimately carry the source's own foreign key), and on those rows `aggregate`
computed and released `mean`/`count`/`sum`/`min`/`max` over a field no
declaration governs -- no `Sensitivity`, no scope routing, no type contract --
and `float()` raised a bare `ValueError`/`TypeError` on a non-numeric one.
`where=`, `order_by` and `group_by` all already refuse that same name with
`UNKNOWN_FIELD`; `value_field` was the only identifier parameter that did not,
and the TYPED overload already refused it (`_validate_field_name`). That is the
two-spellings shape B1 and G2 both had.

**F5 -- `where=`.** `_normalize_where` called `where.items()` unguarded, so a
non-dict raised a bare `AttributeError` on all six read surfaces (not just
`exists`, as filed) through `GuardedQuery`, `OntologyClient` and `BoundQuery`.
The typed spelling is not safe either: it never reaches `_normalize_where`,
because `validate_where_keys` does `set(where)` first -- shredding a string into
its characters and reporting them as field names, and raising a bare `TypeError`
on an `int`. Worst and entirely unfiled: `if not where` treated a FALSY non-dict
as "no filter", so `where=""`/`0`/`[]`/`False` silently matched every row. The
natural mistake behind the whole item -- `exists(type, id)` confused with
`get_object(consumer, type, id)` -- is therefore loud for `"r0"` and silently
answers `True` against the entire table for `""`.

Neither is a disclosure, and both verdicts were re-derived rather than assumed.
F4's undeclared key is already readable per-row by the same consumer (`_redact`
strips only DECLARED hidden fields), so the aggregate released nothing new;
that per-row gap is separate and deliberately not addressed here. F5's rows all
passed the ordinary visibility gates. Both are silent wrong answers, which is
what F3 turned out to be too.

The inputs the existing suite structurally could not construct:
  - F4: a row CARRYING an undeclared payload key. Every fixture in the suite
    inserts declared properties only, so `value_field` naming something the
    type does not declare could only ever select zero rows -- the case that
    refuses anyway, which is why a green suite never saw the release.
  - F5: a `where=` that is not a dict. Grep across `tests/` finds zero calls
    passing one, typed or untyped, truthy or falsy.
"""

from __future__ import annotations

import asyncio
import json
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


def _build(
    min_n: int = 1,
) -> tuple[OntologyDef, type[OntologyObject], ObjectStore]:
    """An unscoped type with a numeric, a hidden numeric, and a `json`
    property, so every neighbouring `value_field` verdict is reachable from
    the same fixture and a fix cannot pass by narrowing one of them."""
    ontology = Ontology(name="readshape", scope_levels=["org"], min_n=min_n)

    @ontology.object(layer="L0", scope="unscoped")
    class Record(OntologyObject):
        id: str = prop(primary_key=True)
        score: float
        group_id: str | None = None
        label: str | None = None
        bucket: dict[str, Any] | None = None
        secret: float | None = prop(
            default=None, sensitivity=Sensitivity(human_visible=False)
        )

    definition = ontology.definition
    return definition, Record, ObjectStore(definition.registry)


def _seed(store: ObjectStore, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        store.insert("Record", row, SRC)


def _declared_rows(count: int = 6) -> list[dict[str, Any]]:
    return [
        {
            "id": f"r{i}",
            "score": float(i),
            "group_id": "g1" if i < 3 else "g2",
        }
        for i in range(count)
    ]


def _mcp(definition: OntologyDef, store: ObjectStore, tool: str, **args: Any) -> Any:
    server = build_mcp_server(definition, store, CONSUMER)
    result = asyncio.run(server.call_tool(tool, args))
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


# =========================================================================
# F4 -- an unknown `value_field`
# =========================================================================


def _aggregate_surfaces(
    definition: OntologyDef, store: ObjectStore
) -> dict[str, Any]:
    """`aggregate` reached every way a caller can reach it.

    `BoundQuery` is the author surface and `OntologyClient`'s string overload
    the consumer one; G2 records a fix that threaded some branches of this
    mixin and left others untouched, so a refusal real on one spelling and
    absent on another is the failure this mapping exists to catch.
    """
    guarded = GuardedQuery(store, definition.registry, definition.policy)
    client = OntologyClient(definition, store, CONSUMER)
    return {
        "GuardedQuery": lambda *a, **k: guarded.aggregate(CONSUMER, *a, **k),
        "OntologyClient": client.aggregate,
        "BoundQuery": BoundQuery(guarded, CONSUMER).aggregate,
    }


@pytest.mark.parametrize(
    "surface", ["GuardedQuery", "OntologyClient", "BoundQuery"]
)
def test_unknown_value_field_refuses_instead_of_naming_a_min_n_threshold(
    surface: str,
) -> None:
    """The filed symptom: an undeclared name answered `MIN_N_VIOLATION`.

    A refusal that describes a contributor threshold for a field the ontology
    never declared tells the caller to go find more people, when what happened
    is that they made a typo.
    """
    definition, _Record, store = _build(min_n=3)
    _seed(store, _declared_rows())

    aggregate = _aggregate_surfaces(definition, store)[surface]
    with raises_code(ValidationFailed, "UNKNOWN_FIELD"):
        aggregate("Record", "scoer")


def test_unknown_value_field_refuses_on_aggregate_by_too() -> None:
    """`aggregate_by` funnels through the same `_aggregate`, and the ledger
    named only `aggregate`. Pinned separately so a fix cannot be threaded
    into the ungrouped branch alone."""
    definition, _Record, store = _build(min_n=3)
    _seed(store, _declared_rows())
    guarded = GuardedQuery(store, definition.registry, definition.policy)

    with raises_code(ValidationFailed, "UNKNOWN_FIELD"):
        guarded.aggregate_by(CONSUMER, "Record", "scoer", "group_id")


def test_unknown_value_field_refuses_over_the_mcp_wire() -> None:
    definition, _Record, store = _build(min_n=1)
    _seed(store, _declared_rows(1))

    payload = _mcp(
        definition, store, "aggregate_objects", obj_type="Record", value_field="scoer"
    )

    assert payload["error"]["code"] == "UNKNOWN_FIELD"


def test_unknown_value_field_matches_its_three_sibling_parameters() -> None:
    """The four identifier parameters of one read surface must agree.

    `where=`, `order_by` and `group_by` already answered `UNKNOWN_FIELD` for
    this exact name while `value_field` returned a number or a min-N refusal.
    Asserting the four together is what stops a later change from fixing one
    and letting the others drift -- a single-parameter pin cannot see that.
    """
    definition, _Record, store = _build(min_n=1)
    _seed(store, _declared_rows(1))
    guarded = GuardedQuery(store, definition.registry, definition.policy)

    codes = []
    for call in (
        lambda: guarded.get_objects(CONSUMER, "Record", where={"scoer": 1.0}),
        lambda: guarded.get_objects(CONSUMER, "Record", order_by="scoer"),
        lambda: guarded.aggregate_by(CONSUMER, "Record", "score", "scoer"),
        lambda: guarded.aggregate(CONSUMER, "Record", "scoer"),
    ):
        with pytest.raises(ValidationFailed) as excinfo:
            call()
        codes.append(excinfo.value.code)

    assert codes == ["UNKNOWN_FIELD"] * 4


# -- the half the ledger missed: rows that CARRY the undeclared key ---------


def _seed_with_undeclared_key(store: ObjectStore, value: Any) -> None:
    """Rows carrying `shadow`, a key no property declares.

    `meta.declared_shape_violation` allows this deliberately -- a row whose
    scope resolves `ViaLink` may still carry the source's own foreign key --
    so this is a sanctioned shape, not a broken fixture.
    """
    for i in range(4):
        store.insert(
            "Record",
            {"id": f"r{i}", "score": float(i), "shadow": value(i)},
            SRC,
        )


@pytest.mark.parametrize("func", ["mean", "count", "sum", "min", "max"])
def test_an_undeclared_payload_key_is_never_aggregated(func: str) -> None:
    """The input the old suite could not construct, and the reason "low
    priority" did not survive re-derivation.

    Every reduction released a real number computed over a field with no
    declared type, no `Sensitivity`, and no scope routing -- while `where=`,
    `order_by` and `group_by` all refused that same name. The engine's own
    position is that an undeclared payload key is not addressable by name;
    `value_field` was the sole dissenter.
    """
    definition, _Record, store = _build(min_n=3)
    _seed_with_undeclared_key(store, lambda i: 100.0 + i)
    guarded = GuardedQuery(store, definition.registry, definition.policy)

    with raises_code(ValidationFailed, "UNKNOWN_FIELD"):
        guarded.aggregate(CONSUMER, "Record", "shadow", func=func)


def test_an_undeclared_payload_key_is_not_released_over_the_mcp_wire() -> None:
    definition, _Record, store = _build(min_n=3)
    _seed_with_undeclared_key(store, lambda i: 100.0 + i)

    payload = _mcp(
        definition, store, "aggregate_objects", obj_type="Record", value_field="shadow"
    )

    assert "result" not in payload
    assert payload["error"]["code"] == "UNKNOWN_FIELD"


@pytest.mark.parametrize(
    ("label", "value"),
    [("str", lambda i: f"txt{i}"), ("dict", lambda i: {"k": i})],
)
def test_an_undeclared_key_refuses_rather_than_raising_a_bare_python_error(
    label: str, value: Any
) -> None:
    """The skipped type gate let `float()` fail on the data.

    A `str`-valued undeclared key raised `ValueError: could not convert string
    to float: 'txt0'` and a `dict`-valued one a bare `TypeError` -- an ordinary
    caller mistake surfacing as an uncaught Python exception, which over MCP
    also forwarded a raw stored payload value into the error message. The
    declared sibling (`value_field="label"`, a declared `str`) has always
    answered `NON_NUMERIC_AGGREGATE` before reading a row.
    """
    definition, _Record, store = _build(min_n=3)
    _seed_with_undeclared_key(store, value)
    guarded = GuardedQuery(store, definition.registry, definition.policy)

    with raises_code(ValidationFailed, "UNKNOWN_FIELD"):
        guarded.aggregate(CONSUMER, "Record", "shadow")


# -- companions: what the fix must NOT narrow (rstaff L31) ------------------


def test_a_declared_value_field_still_aggregates() -> None:
    """Without this, "refuse an unknown `value_field`" is indistinguishable
    from "refuse `value_field`"."""
    definition, _Record, store = _build(min_n=1)
    _seed(store, _declared_rows())
    guarded = GuardedQuery(store, definition.registry, definition.policy)

    assert guarded.aggregate(CONSUMER, "Record", "score") == 2.5
    assert guarded.aggregate_by(CONSUMER, "Record", "score", "group_id") == {
        "g1": 1.0,
        "g2": 4.0,
    }


def test_a_hidden_value_field_still_refuses_on_visibility_not_existence() -> None:
    """The L31 receipt, and the answer to "is this a new oracle?".

    A declared-but-hidden field passes the existence check and must still
    reach `VISIBILITY_DENIED`. Answering `UNKNOWN_FIELD` for it would both
    leak "no such field" about a field that exists and make that gate
    unreachable -- exactly what Track B's M9 pinned for `group_by`.
    """
    definition, _Record, store = _build(min_n=1)
    _seed(store, _declared_rows())
    guarded = GuardedQuery(store, definition.registry, definition.policy)

    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        guarded.aggregate(CONSUMER, "Record", "secret")


def test_a_non_numeric_declared_value_field_keeps_its_own_refusal() -> None:
    """`NON_NUMERIC_AGGREGATE` is a different complaint from "no such field"
    and must not be collapsed into the new one."""
    definition, _Record, store = _build(min_n=1)
    _seed(store, _declared_rows())
    guarded = GuardedQuery(store, definition.registry, definition.policy)

    with raises_code(ValidationFailed, "NON_NUMERIC_AGGREGATE"):
        guarded.aggregate(CONSUMER, "Record", "label")
    with raises_code(ValidationFailed, "NON_NUMERIC_AGGREGATE"):
        guarded.aggregate(CONSUMER, "Record", "bucket")


def test_an_unknown_object_type_still_pre_empts_the_value_field_check() -> None:
    """Ordering, held from Track B: an unregistered type refuses as one even
    when the `value_field` is also unknown."""
    definition, _Record, store = _build(min_n=1)
    guarded = GuardedQuery(store, definition.registry, definition.policy)

    with raises_code(ValidationFailed, "UNKNOWN_OBJECT_TYPE"):
        guarded.aggregate(CONSUMER, "Nope", "scoer")


def test_a_hidden_group_by_still_pre_empts_an_unknown_value_field() -> None:
    """The new check must sit BELOW the visibility gates, so a denial the
    consumer is not entitled to see is never pre-empted by a complaint about
    a name. Measured shipped behaviour before the fix, held after."""
    definition, _Record, store = _build(min_n=1)
    _seed(store, _declared_rows())
    guarded = GuardedQuery(store, definition.registry, definition.policy)

    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        guarded.aggregate_by(CONSUMER, "Record", "scoer", "secret")


def test_the_typed_overloads_refusal_is_unchanged() -> None:
    """The typed spelling already refused, through a different check
    (`_validate_field_name` against `model_fields`). It must keep doing so
    on its own path -- Track B's G2 lesson was a fix that patched one class
    and left the sibling pin green because it never drove that branch."""
    definition, Record, store = _build(min_n=1)
    _seed(store, _declared_rows())
    client = OntologyClient(definition, store, CONSUMER)

    with raises_code(ValidationFailed, "UNKNOWN_FIELD") as excinfo:
        client.aggregate(Record, "scoer")
    # The EXACT typed message, not a substring both refusals share. Since
    # `_aggregate` now refuses the same name with the same code, a loose
    # assertion here would stay green with `_validate_field_name` deleted --
    # a pin nothing can red is not a pin (rstaff L18). The string-surface
    # refusal spells it `unknown field(s) [...]`, matching `order_by` and
    # `group_by`; the client-side one is singular and unbracketed.
    assert str(excinfo.value) == "Record: value_field names unknown field 'scoer'"


# =========================================================================
# F5 -- a `where=` that is not a mapping
# =========================================================================


def _read_surfaces(definition: OntologyDef, store: ObjectStore) -> dict[str, Any]:
    """Every read that accepts `where=`, on every spelling.

    Six methods times three surfaces plus the typed overloads: the ledger
    filed `exists` alone, and the defect was in the one snapshot helper they
    all share.
    """
    guarded = GuardedQuery(store, definition.registry, definition.policy)
    client = OntologyClient(definition, store, CONSUMER)
    bound = BoundQuery(guarded, CONSUMER)
    return {
        "GuardedQuery.get_objects": lambda w: guarded.get_objects(
            CONSUMER, "Record", w
        ),
        "GuardedQuery.count": lambda w: guarded.count(CONSUMER, "Record", w),
        "GuardedQuery.exists": lambda w: guarded.exists(CONSUMER, "Record", w),
        "GuardedQuery.aggregate": lambda w: guarded.aggregate(
            CONSUMER, "Record", "score", w
        ),
        "GuardedQuery.aggregate_by": lambda w: guarded.aggregate_by(
            CONSUMER, "Record", "score", "group_id", w
        ),
        "GuardedQuery.count_contributors": lambda w: guarded.count_contributors(
            CONSUMER, "Record", w
        ),
        "OntologyClient.list": lambda w: client.list("Record", w),
        "OntologyClient.count": lambda w: client.count("Record", w),
        "OntologyClient.exists": lambda w: client.exists("Record", w),
        "OntologyClient.aggregate": lambda w: client.aggregate("Record", "score", w),
        "OntologyClient.count_contributors": lambda w: client.count_contributors(
            "Record", w
        ),
        "BoundQuery.list": lambda w: bound.list("Record", w),
        "BoundQuery.count": lambda w: bound.count("Record", w),
        "BoundQuery.exists": lambda w: bound.exists("Record", w),
        "BoundQuery.aggregate": lambda w: bound.aggregate("Record", "score", w),
    }


def _read_surface_names() -> list[str]:
    _definition, _Record, _store = _build()
    return list(_read_surfaces(_definition, _store).keys())


_READ_SURFACE_NAMES = _read_surface_names()


@pytest.mark.parametrize("surface", _READ_SURFACE_NAMES)
def test_a_truthy_non_mapping_where_refuses_on_every_read_surface(
    surface: str,
) -> None:
    """The filed call was `client.exists("Record", "r0")` on one method.

    It is the shared snapshot helper that was unguarded, so every read
    carrying a `where=` raised the same bare `AttributeError`.
    """
    definition, _Record, store = _build(min_n=1)
    _seed(store, _declared_rows(3))

    read = _read_surfaces(definition, store)[surface]
    with raises_code(ValidationFailed, "INVALID_PARAMS"):
        read("r0")


@pytest.mark.parametrize("operand", [["r0"], 7, 1.5, ("a", "b"), {"a"}, True])
def test_every_non_mapping_where_shape_refuses_alike(operand: Any) -> None:
    """A `str` was the filed shape; a list, a number, a tuple, a set and a
    bool all reached `.items()` the same way. One rule, not a `str` special
    case."""
    definition, _Record, store = _build(min_n=1)
    _seed(store, _declared_rows(3))
    guarded = GuardedQuery(store, definition.registry, definition.policy)

    with raises_code(ValidationFailed, "INVALID_PARAMS"):
        guarded.count(CONSUMER, "Record", operand)


@pytest.mark.parametrize("falsy", ["", 0, [], (), set(), 0.0, False])
def test_a_falsy_non_mapping_where_refuses_rather_than_matching_everything(
    falsy: Any,
) -> None:
    """The half nothing filed, and the only one that was silent.

    `if not where` treated these as "no filter", so a caller who believed
    they were narrowing received the entire visible population -- `count` 3
    of 3, `exists` True. The mistake this whole item came from,
    `exists(type, id)` for `get_object(consumer, type, id)`, is loud for an
    id of `"r0"` and silently answers True for an id of `""` or `0`.
    """
    definition, _Record, store = _build(min_n=1)
    _seed(store, _declared_rows(3))
    guarded = GuardedQuery(store, definition.registry, definition.policy)

    with raises_code(ValidationFailed, "INVALID_PARAMS"):
        guarded.count(CONSUMER, "Record", falsy)
    with raises_code(ValidationFailed, "INVALID_PARAMS"):
        guarded.exists(CONSUMER, "Record", falsy)


@pytest.mark.parametrize("operand", ["r0", 7, ["r0"]])
def test_the_typed_spelling_refuses_on_shape_not_on_shredded_field_names(
    operand: Any,
) -> None:
    """The typed surface was filed as safe. It is not, and it fails its own
    way: it never reaches the snapshot helper, because `validate_where_keys`
    does `set(where)` first. A string was shredded into its characters and
    reported as field names (`where= names unknown field(s) ['0', 'r']`), and
    an `int` raised a bare `TypeError`. `mypy --strict` bounds only a literal;
    a `where` typed `Any` -- one built from JSON or connector-fed data -- is
    not flagged at all.
    """
    definition, Record, store = _build(min_n=1)
    _seed(store, _declared_rows(3))
    client = OntologyClient(definition, store, CONSUMER)

    with raises_code(ValidationFailed, "INVALID_PARAMS"):
        client.exists(Record, operand)


def test_the_mcp_boundary_still_refuses_before_the_engine_does() -> None:
    """The one bound the ledger got right: the wire surface already checked
    the shape. Pinned so the new in-process refusal cannot be mistaken for
    the thing keeping MCP safe."""
    definition, _Record, store = _build(min_n=1)
    _seed(store, _declared_rows(3))

    payload = _mcp(definition, store, "count_objects", obj_type="Record", where="r0")

    assert payload["error"]["code"] == "INVALID_PARAMS"
    assert "must be an object" in payload["error"]["message"]


# -- companions: what the fix must NOT narrow (rstaff L31) ------------------


def test_an_empty_mapping_where_still_means_no_filter() -> None:
    """`{}` is a dict and must keep short-circuiting to "no filter".

    The fix has to check the TYPE before the falsy test, not instead of it.
    No test pinned this before -- `where={}` appears nowhere in the suite --
    so the obvious wrong fix (refuse everything falsy) had nothing to catch it.
    """
    definition, Record, store = _build(min_n=1)
    _seed(store, _declared_rows(3))
    guarded = GuardedQuery(store, definition.registry, definition.policy)
    client = OntologyClient(definition, store, CONSUMER)

    assert guarded.count(CONSUMER, "Record", {}) == 3
    assert guarded.exists(CONSUMER, "Record", {}) is True
    assert client.count("Record", {}) == 3
    assert client.count(Record, {}) == 3


def test_an_omitted_where_still_means_no_filter() -> None:
    definition, _Record, store = _build(min_n=1)
    _seed(store, _declared_rows(3))
    guarded = GuardedQuery(store, definition.registry, definition.policy)

    assert guarded.count(CONSUMER, "Record", None) == 3
    assert guarded.count(CONSUMER, "Record") == 3


def test_a_real_mapping_where_still_filters() -> None:
    """The companion that makes every refusal above meaningful: without it,
    "refuse a non-mapping" is indistinguishable from "refuse `where=`"."""
    definition, _Record, store = _build(min_n=1)
    _seed(store, _declared_rows())
    guarded = GuardedQuery(store, definition.registry, definition.policy)

    assert guarded.count(CONSUMER, "Record", {"group_id": "g1"}) == 3
    assert guarded.count(CONSUMER, "Record", {"score": {"gte": 4.0}}) == 2
    assert guarded.exists(CONSUMER, "Record", {"group_id": "nope"}) is False


def test_a_malformed_mapping_keeps_its_own_operator_refusals() -> None:
    """A dict whose CONTENTS are wrong is a different complaint from a
    `where=` that is not a dict, and the shape check must not swallow it."""
    definition, _Record, store = _build(min_n=1)
    _seed(store, _declared_rows())
    guarded = GuardedQuery(store, definition.registry, definition.policy)

    with raises_code(ValidationFailed, "UNKNOWN_OPERATOR"):
        guarded.count(CONSUMER, "Record", {"score": {"gt": 1, "regex": 2}})
    with raises_code(ValidationFailed, "OPERATOR_TYPE_MISMATCH"):
        guarded.count(CONSUMER, "Record", {"score": {"gt": "abc"}})
    with raises_code(ValidationFailed, "UNKNOWN_FIELD"):
        guarded.count(CONSUMER, "Record", {"scoer": 1.0})
