"""T9 design-guide lint coverage and mutation pins."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import ontary.diagnose as diagnose_module
from ontary import (
    ActionParams,
    Cardinality,
    InMemoryStore,
    ObjectStore,
    Ontology,
    OntologyObject,
    PostgresStore,
    SelfScope,
    Sensitivity,
    prop,
    target,
)

_SHIPPED_BACKEND_NAMES = {
    backend.__name__ for backend in (InMemoryStore, ObjectStore, PostgresStore)
}


def test_storage_envelope_keys_are_shipped_backend_class_names() -> None:
    assert set(diagnose_module.STORAGE_ENVELOPE) <= _SHIPPED_BACKEND_NAMES


def test_storage_envelope_records_have_positive_measurements_and_a_label() -> None:
    """Every field of every published record, structurally -- the S2 repair:

    one `StorageEnvelope` per backend means a published `rows` cannot exist
    without a `seconds_per_row` and a `label` alongside it (there is no
    third dict for either to be missing FROM). What this cannot catch: a
    record that is well-typed and positive but simply MEASURED WRONG (a
    fat-fingered `rows` or `seconds_per_row`) -- only re-running
    `scripts/scan_curve.py` for real, or a doc-vs-code drift caught by
    `tests/test_docs.py`, catches that, and only the doc catches it AT ALL
    if the wrong number was not pasted into both places identically.
    """
    for envelope in diagnose_module.STORAGE_ENVELOPE.values():
        assert type(envelope.rows) is int and envelope.rows > 0
        assert (
            type(envelope.seconds_per_row) is float and envelope.seconds_per_row > 0
        )
        assert type(envelope.label) is str and envelope.label


def test_storage_envelope_object_store_value_is_measured_ceiling() -> None:
    assert diagnose_module.STORAGE_ENVELOPE["ObjectStore"].rows == 32_000


def test_storage_envelope_postgres_value_is_measured_ceiling() -> None:
    assert diagnose_module.STORAGE_ENVELOPE["PostgresStore"].rows == 1_600


def test_storage_envelope_rows_times_seconds_per_row_stays_near_one_second() -> None:
    """One rule over the whole mapping (S2), not a row added per backend --
    the invariant E5's reviewer specified and E5's review carried forward.

    `rows` is supposed to be (a round number just under) the row count at
    which a governed read crosses one second, so `rows * seconds_per_row`
    should sit close under 1.0 for EVERY published record, not merely be
    positive. `seconds_per_row` was otherwise pinned by positivity alone
    (see the test above): a `seconds_per_row` fat-fingered by 10x or 0.1x
    stays positive and reds nothing else in this file. This is exactly the
    class of error that shipped in E5 -- `PostgresStore` published
    rows=10_000, seconds_per_row=0.000097 from a local machine 2.96x faster
    than the CI `postgres:16` job spec storage-envelope.md §3 requires.
    E5b reconciled that to rows=3_200, seconds_per_row=0.000312 from ONE CI
    run; E5c then found a SECOND CI run of the identical step, 77 minutes
    later, measuring 1.77x slower still (GitHub-hosted runners are shared
    VMs, not the "fixed hardware" spec §3 assumed, so no single CI sample
    supports a ceiling to two significant figures) and republished the
    conservative floor across every published sample: rows=1_800,
    seconds_per_row=0.000554. E5d then found a THIRD CI run, 3h27m after
    the second (4h44m after the first) -- its worst point (563.7us/row, at
    n=5,000, NOT the top of the tested range: the curve is non-monotone
    again) sat 1.5% above the crossing E5c's 1,800 implied, so applying the
    SAME rule a second time republished the floor once more: rows=1_700,
    seconds_per_row=0.000564. E5f then found a FOURTH CI run, roughly 8
    hours after the third -- its worst point (608.6us/row, at n=1,000,
    the BOTTOM of the tested range this time, not the top and not the
    middle: the curve is non-monotone yet again, and the slower class at
    n=25,000 flipped for a third time, back to `aggregate` at 587.7us/row)
    sat well above the crossing E5d's 1,700 implied (1,700 * 0.000609 =
    1.0353s, over one second), so applying the SAME rule a third time
    republished the floor once more: rows=1_600, seconds_per_row=0.000609.
    A 1.5% overage, or a 3.5% one, is over either way, by this rule's own
    design -- there is no "close enough" exception; that is the entire
    point of replacing a judgment call with arithmetic.

    What this does NOT catch: a `rows`/`seconds_per_row` pair that agrees
    with ITSELF (the product still lands in [0.9, 1.0]) but was measured
    wrong -- the exact shape of E5's bug, whose superseded pair
    (10_000 * 0.000097 = 0.97) also passes this band. That is not the
    whole residual, though: the exact-value pin right above already reds
    the moment `rows` moves on its own, `tests/test_docs.py`'s structural
    summary-ceiling pin reds the moment this module's `STORAGE_ENVELOPE`
    and `docs/storage.md`'s ceiling row disagree, and (E5c, the S2 closure)
    `tests/test_docs.py`'s doc-PARSING pin independently re-derives the
    ceiling from the doc's own raw per-run Postgres cost tables (all
    four, as of E5f) and reds if the published `rows` exceeds what those
    tables support. A wrong-but-self-consistent pair now ships green only
    if it is copied identically into FOUR places at once -- this module's
    constant, the doc's summary ceiling row, the exact-value pin above,
    AND the doc's raw per-run cost tables themselves -- verified by
    mutation: `rows=6_400, seconds_per_row=0.000156` (E5b's proven escape,
    same 0.998 product), changed in the first three alone leaves the raw
    tables un-fabricated, so `tests/test_docs.py`'s doc-parsing test reds
    (`rows=6,400 exceeds the one-second crossing (1,643.1 rows)`, the
    crossing implied by all four published runs) while every other test in
    the suite, including this one, stays green. Fabricating the fourth
    (the raw tables) is what is left to escape all tests, and is exactly
    what this task's reviewer protocol hands to a human: independently
    re-reading the actual CI job logs (`gh run view --job <id> --log`), not
    trusting the doc's own numbers.
    """
    for envelope in diagnose_module.STORAGE_ENVELOPE.values():
        assert 0.9 <= envelope.rows * envelope.seconds_per_row <= 1.0


class _PagedStore:
    def __init__(self, rows_by_type: dict[str, int]) -> None:
        self.rows_by_type = rows_by_type
        self.read_page_calls = 0

    def read_page(
        self, obj_type: str, after_key: str | None = None, batch: int = 500
    ) -> list[Any]:
        self.read_page_calls += 1
        start = 0 if after_key is None else int(after_key) + 1
        stop = min(start + batch, self.rows_by_type[obj_type])
        return [SimpleNamespace(key=str(index)) for index in range(start, stop)]


def _paged_store(backend_name: str, rows_by_type: dict[str, int]) -> _PagedStore:
    backend_type = type(backend_name, (_PagedStore,), {})
    return backend_type(rows_by_type)


def test_storage_envelope_finding_is_golden_and_pages_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        diagnose_module,
        "STORAGE_ENVELOPE",
        {
            "ObjectStore": diagnose_module.StorageEnvelope(
                rows=15_000, seconds_per_row=0.000065, label="SQLite"
            )
        },
    )
    store = _paged_store("ObjectStore", {"Comment": 0, "Ticket": 84_000})

    findings = _golden_ticket_ontology("response_hours").diagnose(
        store=store  # type: ignore[arg-type]
    )

    assert [finding.model_dump() for finding in findings] == [
        {
            "code": "STORAGE_ENVELOPE_EXCEEDED",
            "severity": "warn",
            "location": "Ticket",
            "message": (
                "Ticket holds 84,000 rows. The measured envelope for ObjectStore "
                "(SQLite) is ~15,000 rows for a governed aggregate or narrow-scope "
                "read under one second; at this size expect ~5.5s. Bounded, "
                "unordered page reads are unaffected."
            ),
            "fix_hint": (
                "Narrow the population with `where=` before aggregating, or split "
                "the type. See docs/storage.md#storage-envelope."
            ),
        }
    ]
    assert store.read_page_calls == 170


class _RaisingMidWalkStore(_PagedStore):
    """Succeeds for exactly one page of a designated type, then raises on
    every later call for that SAME type -- never on the first call.

    Reproduces the real, reachable failure modes `_storage_envelope_findings`'s
    `except Exception` in `diagnose.py` exists for: `PostgresStore` and
    `ObjectStore` both execute raw SQL through `store/_sql.py`'s `execute()`,
    which does not wrap driver errors, so a dropped connection or a query
    timeout surfaces mid-pagination as a bare `psycopg`/`sqlite3` exception;
    a corrupted payload fails `json.loads` inside `_row_to_stored` as a bare
    `ValueError`. All of those can just as easily happen on page 2, 3, or
    later as on page 1 -- this double models that by letting one page
    through first, so `row_count` has already accumulated something before
    the failure.
    """

    def __init__(self, rows_by_type: dict[str, int], *, fails_on: str) -> None:
        super().__init__(rows_by_type)
        self.fails_on = fails_on
        self._served_first_page = False

    def read_page(
        self, obj_type: str, after_key: str | None = None, batch: int = 500
    ) -> list[Any]:
        if obj_type == self.fails_on and self._served_first_page:
            self.read_page_calls += 1
            raise RuntimeError("connection dropped mid-walk")
        page = super().read_page(obj_type, after_key=after_key, batch=batch)
        if obj_type == self.fails_on:
            self._served_first_page = True
        return page


def test_storage_envelope_mid_walk_failure_is_isolated_per_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store that fails partway through ONE type's pagination must not
    blank out every other type's finding -- the inner `except Exception`'s
    actual job (see its comment in `diagnose.py`): per-type isolation
    within the rule, not keeping `diagnose()` from raising at all (the
    outer `_collect_findings` catch already guarantees that regardless).

    `Comment` has 20,000 rows -- over the 15,000 envelope used here, same
    as `Ticket` -- so if the induced failure did NOT stop its walk, it
    would ALSO cross the envelope and add its own finding ahead of
    `Ticket`'s (alphabetical order). `Comment` succeeds for one full page
    (500 rows, so the walk would keep going on its own -- 20,000 rows is
    nowhere near exhausted) and then raises on the very next call. The
    rule must report exactly `Ticket`'s finding: nothing for `Comment`
    (a failed count reads identically to "found to be under the
    envelope" -- see the same comment for why that is accepted), and no
    `DIAGNOSE_RULE_FAILED` (the failure never reaches the outer catch).
    """
    monkeypatch.setattr(
        diagnose_module,
        "STORAGE_ENVELOPE",
        {
            "ObjectStore": diagnose_module.StorageEnvelope(
                rows=15_000, seconds_per_row=0.000065, label="SQLite"
            )
        },
    )
    backend_type = type("ObjectStore", (_RaisingMidWalkStore,), {})
    store = backend_type({"Comment": 20_000, "Ticket": 84_000}, fails_on="Comment")

    findings = _golden_ticket_ontology("response_hours").diagnose(
        store=store  # type: ignore[arg-type]
    )

    assert [finding.model_dump() for finding in findings] == [
        {
            "code": "STORAGE_ENVELOPE_EXCEEDED",
            "severity": "warn",
            "location": "Ticket",
            "message": (
                "Ticket holds 84,000 rows. The measured envelope for ObjectStore "
                "(SQLite) is ~15,000 rows for a governed aggregate or narrow-scope "
                "read under one second; at this size expect ~5.5s. Bounded, "
                "unordered page reads are unaffected."
            ),
            "fix_hint": (
                "Narrow the population with `where=` before aggregating, or split "
                "the type. See docs/storage.md#storage-envelope."
            ),
        }
    ]
    # Comment: one successful full page (500 of 20,000 rows -- proving the
    # walk had far more left to do), then the induced raise on the very
    # next call -- 2 calls. Ticket: 84,000 rows at batch=500 takes 169
    # calls (see the golden pagination test above for the arithmetic).
    assert store.read_page_calls == 2 + 169


def test_storage_envelope_is_silent_without_store_or_under_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        diagnose_module,
        "STORAGE_ENVELOPE",
        {
            "ObjectStore": diagnose_module.StorageEnvelope(
                rows=3, seconds_per_row=0.000065, label="SQLite"
            )
        },
    )
    ontology = _golden_ticket_ontology("response_hours")

    assert ontology.diagnose() == []
    store = _paged_store("ObjectStore", {"Comment": 0, "Ticket": 3})
    assert ontology.diagnose(store=store) == []  # type: ignore[arg-type]


_UNPUBLISHED_BACKEND_NAMES = sorted(
    _SHIPPED_BACKEND_NAMES - set(diagnose_module.STORAGE_ENVELOPE)
)
assert _UNPUBLISHED_BACKEND_NAMES, (
    "every shipped backend now publishes a STORAGE_ENVELOPE record -- widen "
    "_SHIPPED_BACKEND_NAMES (or add a fake backend class) so this test still "
    "has an unpublished case to prove silent, rather than parametrizing over "
    "nothing"
)


@pytest.mark.parametrize("backend_name", _UNPUBLISHED_BACKEND_NAMES)
def test_storage_envelope_is_silent_for_unpublished_backend(
    backend_name: str,
) -> None:
    store = _paged_store(backend_name, {"Comment": 0, "Ticket": 84_000})

    assert (
        _golden_ticket_ontology("response_hours").diagnose(
            store=store  # type: ignore[arg-type]
        )
        == []
    )
    assert store.read_page_calls == 0


def _scoped_ontology(name: str, *, min_n: int = 3) -> Ontology:
    return Ontology(name, scope_levels=["org"], min_n=min_n)


def _golden_ticket_ontology(property_name: str = "avg_response_hours") -> Ontology:
    ontology = _scoped_ontology("golden")

    @ontology.object(layer="L0", api_name="Comment", scope=[SelfScope(level="org")])
    class Comment(OntologyObject):
        id: str = prop(primary_key=True)
        body: str = prop()

    @ontology.object(layer="L0", api_name="Ticket", scope=[SelfScope(level="org")])
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        avg_response_hours: float = prop()

    ontology.link(
        "ticket_comments",
        Ticket,
        Comment,
        Cardinality.ONE_TO_MANY,
        description="Ticket comments.",
    )
    if property_name != "avg_response_hours":
        ticket = ontology.registry.object_types["Ticket"]
        ticket.properties[1] = ticket.properties[1].model_copy(
            update={"name": property_name}
        )
    return ontology


def test_stored_derivable_golden_finding_is_byte_exact() -> None:
    findings = _golden_ticket_ontology().diagnose()

    assert len(findings) == 1
    dumped = findings[0].model_dump(mode="json")
    assert dumped == {
        "code": "STORED_DERIVABLE",
        "severity": "warn",
        "location": "object Ticket, property avg_response_hours",
        "message": (
            "looks like a stored aggregate; facts are stored once and derived "
            "by Functions"
        ),
        "fix_hint": (
            "declare a Function that computes it from Comment rows, or mark the "
            "type as a declared snapshot"
        ),
    }
    assert json.dumps(dumped) == (
        '{"code": "STORED_DERIVABLE", "severity": "warn", '
        '"location": "object Ticket, property avg_response_hours", '
        '"message": "looks like a stored aggregate; facts are stored once '
        'and derived by Functions", "fix_hint": "declare a Function that '
        'computes it from Comment rows, or mark the type as a declared '
        'snapshot"}'
    )


def test_stored_derivable_negative_for_plain_property() -> None:
    assert _golden_ticket_ontology("response_hours").diagnose() == []


def _ontology_with_action_name(api_name: str) -> Ontology:
    ontology = _scoped_ontology("action-name")

    @ontology.object(layer="L0", api_name="Ticket", scope=[SelfScope(level="org")])
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        status: str = prop()

    class Params(ActionParams):
        ticket_id: str = target(Ticket)

    @ontology.action(
        Params,
        target=Ticket,
        roles=["Operator"],
        api_name=api_name,
        display_name="Update status",
    )
    def handler(_ctx: Any, _params: Params) -> dict[str, Any]:
        return {}

    return ontology


@pytest.mark.parametrize(
    "api_name",
    ["UpdateStatus", "DeleteTicket", "RemoveTicket", "EraseTicket"],
)
def test_crud_action_name_positive(api_name: str) -> None:
    findings = _ontology_with_action_name(api_name).diagnose()

    assert [finding.code for finding in findings] == ["CRUD_ACTION_NAME"]
    assert findings[0].severity == "warn"


def test_crud_action_name_negative_when_only_api_name_uses_business_verb() -> None:
    assert _ontology_with_action_name("ApproveTicket").diagnose() == []


def _snapshot_named_ontology(description: str) -> Ontology:
    ontology = _scoped_ontology("snapshot-name")

    @ontology.object(
        layer="L0",
        api_name="TicketSnapshot",
        description=description,
        scope=[SelfScope(level="org")],
    )
    class TicketSnapshot(OntologyObject):
        id: str = prop(primary_key=True)

    return ontology


def test_forbidden_type_name_positive_without_declared_snapshot_marker() -> None:
    findings = _snapshot_named_ontology("A copied ticket value.").diagnose()

    assert [finding.code for finding in findings] == ["FORBIDDEN_TYPE_NAME"]
    assert findings[0].severity == "warn"


def test_forbidden_snapshot_name_negative_with_declared_snapshot_marker() -> None:
    assert (
        _snapshot_named_ontology(
            "A declared snapshot of Ticket at an observation time."
        ).diagnose()
        == []
    )


def _ontology_with_micro_action(parameter_name: str) -> Ontology:
    ontology = _scoped_ontology("micro-action")

    @ontology.object(layer="L0", api_name="Ticket", scope=[SelfScope(level="org")])
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        status: str = prop()

    class Params(ActionParams):
        ticket_id: str = target(Ticket)
        status: str

    @ontology.action(
        Params,
        target=Ticket,
        roles=["Operator"],
        api_name="ApproveTicket",
    )
    def handler(_ctx: Any, _params: Params) -> dict[str, Any]:
        return {}

    if parameter_name != "status":
        action = ontology.registry.action_types["ApproveTicket"]
        action.parameters[1] = action.parameters[1].model_copy(update={"name": parameter_name})
    return ontology


def test_micro_action_positive_for_single_property_shape() -> None:
    findings = _ontology_with_micro_action("status").diagnose()

    assert [finding.code for finding in findings] == ["MICRO_ACTION"]
    assert findings[0].severity == "warn"


def test_micro_action_negative_when_parameter_does_not_name_a_property() -> None:
    assert _ontology_with_micro_action("new_status").diagnose() == []


def _sensitive_ontology(*, scoped: bool) -> Ontology:
    ontology = _scoped_ontology("sensitive", min_n=4)
    scope = [SelfScope(level="org")] if scoped else None

    @ontology.object(layer="L0", api_name="Person", scope=scope)
    class Person(OntologyObject):
        id: str = prop(primary_key=True)
        email: str | None = prop(
            default=None,
            sensitivity=Sensitivity(human_visible=False),
        )

    return ontology


def test_unscoped_sensitive_positive_without_policy_scope_rule() -> None:
    findings = _sensitive_ontology(scoped=False).diagnose()

    assert [finding.code for finding in findings] == ["UNSCOPED_SENSITIVE"]
    assert findings[0].severity == "warn"


def test_unscoped_sensitive_negative_with_scope_rule_only() -> None:
    assert _sensitive_ontology(scoped=True).diagnose() == []


def _explicitly_unscoped_sensitive_ontology() -> Ontology:
    ontology = _scoped_ontology("explicit-unscoped", min_n=4)

    @ontology.object(layer="L0", api_name="Person", scope="unscoped")
    class Person(OntologyObject):
        id: str = prop(primary_key=True)
        email: str | None = prop(
            default=None,
            sensitivity=Sensitivity(human_visible=False),
        )

    return ontology


def test_unscoped_sensitive_negative_when_type_is_explicitly_unscoped() -> None:
    assert _explicitly_unscoped_sensitive_ontology().diagnose() == []


def _min_n_ontology(min_n: int) -> Ontology:
    ontology = _scoped_ontology("min-n", min_n=min_n)

    @ontology.object(layer="L0", api_name="Person", scope=[SelfScope(level="org")])
    class Person(OntologyObject):
        id: str = prop(primary_key=True)
        email: str | None = prop(
            default=None,
            sensitivity=Sensitivity(human_visible=False),
        )

    return ontology


def test_min_n_unset_positive_for_default_with_sensitive_property() -> None:
    findings = _min_n_ontology(3).diagnose()

    assert [finding.code for finding in findings] == ["MIN_N_UNSET"]
    assert findings[0].severity == "info"


def test_min_n_unset_negative_when_min_n_is_explicitly_non_default() -> None:
    assert _min_n_ontology(4).diagnose() == []


def test_combined_ontology_reports_three_lints_in_one_sweep() -> None:
    ontology = _scoped_ontology("combined")

    @ontology.object(layer="L0", api_name="Ticket", scope=[SelfScope(level="org")])
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        avg_response_hours: float = prop()

    @ontology.object(layer="L0", api_name="TicketV2", scope=[SelfScope(level="org")])
    class TicketV2(OntologyObject):
        id: str = prop(primary_key=True)

    class UpdateParams(ActionParams):
        ticket_id: str = target(Ticket)

    @ontology.action(
        UpdateParams,
        target=Ticket,
        roles=["Operator"],
        api_name="UpdateStatus",
    )
    def handler(_ctx: Any, _params: UpdateParams) -> dict[str, Any]:
        return {}

    findings = ontology.diagnose()

    assert len(findings) == 3
    assert {finding.code for finding in findings} == {
        "STORED_DERIVABLE",
        "CRUD_ACTION_NAME",
        "FORBIDDEN_TYPE_NAME",
    }


@pytest.mark.parametrize(
    ("code", "rule_name"),
    [
        ("STORED_DERIVABLE", "_stored_derivable_findings"),
        ("CRUD_ACTION_NAME", "_crud_action_name_findings"),
        ("FORBIDDEN_TYPE_NAME", "_forbidden_type_name_findings"),
        ("MICRO_ACTION", "_micro_action_findings"),
        ("UNSCOPED_SENSITIVE", "_unscoped_sensitive_findings"),
        ("MIN_N_UNSET", "_min_n_unset_findings"),
    ],
)
def test_each_lint_is_pinned_in_rules_tuple(code: str, rule_name: str) -> None:
    """Deleting any one rule from RULES must make its lint pin fail."""
    assert code
    assert any(rule.__name__ == rule_name for rule in diagnose_module.RULES)
