"""Dedicated unit tests for `ontary.security`: `Consumer` + `covers_scope`
(spec AC17).

`tests/test_scope.py` already exercises `covers_scope` incidentally, on a
"library"-shaped toy ontology, as part of testing `resolve_owning_scope`.
This module is the DEDICATED truth table for `covers_scope` itself, using
`team`/`company`-named levels to match the spec's own vocabulary for the
exact-match-only limitation (AC17), and does not touch `resolve_owning_scope`
at all -- `covers_scope` only looks at a `resolved: dict[level, id | None]`
mapping plus a `Consumer`, never the store/registry.
"""

from __future__ import annotations

from collections.abc import Callable

from ontary.scope import ScopePolicy
from ontary.security import Consumer, covers_scope

LEVELS = ["team", "company"]


PolicyFactory = Callable[..., ScopePolicy]
ConsumerFactory = Callable[..., Consumer]


# -- same level + id -> covers -----------------------------------------------


def test_same_level_and_id_covers(
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    consumer = make_consumer(
        actor_id="u1",
        role="TeamOwner",
        scope_level="team",
        scope_id="team-a",
        kind="human",
    )
    resolved = {"team": "team-a", "company": "company-1"}
    assert covers_scope(make_policy(levels=LEVELS, min_n=3), consumer, resolved) is True


def test_same_level_and_id_covers_at_company_level_too(
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    consumer = make_consumer(
        actor_id="u1",
        role="Admin",
        scope_level="company",
        scope_id="company-1",
        kind="human",
    )
    resolved = {"team": "team-a", "company": "company-1"}
    assert covers_scope(make_policy(levels=LEVELS, min_n=3), consumer, resolved) is True


# -- different id at same level -> denies ------------------------------------


def test_different_id_at_same_level_denies(
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    consumer = make_consumer(
        actor_id="u1",
        role="TeamOwner",
        scope_level="team",
        scope_id="team-a",
        kind="human",
    )
    resolved = {"team": "team-b", "company": "company-1"}
    assert covers_scope(make_policy(levels=LEVELS, min_n=3), consumer, resolved) is False


def test_different_id_at_company_level_denies(
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    consumer = make_consumer(
        actor_id="u1",
        role="Admin",
        scope_level="company",
        scope_id="company-1",
        kind="human",
    )
    resolved = {"team": "team-a", "company": "company-2"}
    assert covers_scope(make_policy(levels=LEVELS, min_n=3), consumer, resolved) is False


# -- unresolved dimension never fails open ------------------------------------


def test_unresolved_dimension_at_consumer_level_denies(
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    consumer = make_consumer(
        actor_id="u1",
        role="TeamOwner",
        scope_level="team",
        scope_id="team-a",
        kind="human",
    )
    resolved = {"team": None, "company": "company-1"}
    assert covers_scope(make_policy(levels=LEVELS, min_n=3), consumer, resolved) is False


# -- KNOWN LIMITATION (spec AC17): exact-match only --------------------------


def test_company_scoped_consumer_does_NOT_cover_a_child_teams_rows(
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    """Pinned known limitation (spec AC17): `covers_scope` only compares the
    scope id resolved AT THE CONSUMER'S OWN `scope_level`. A company-level
    consumer (`scope_level="company"`) is therefore checked against the
    `resolved["company"]` dimension ONLY -- there is no hierarchy-widening
    rule that also grants it every team under that company. Even though
    `team-a` genuinely belongs to `company-1` (and the company-scoped
    consumer's `scope_id` IS `company-1`), a row whose ONLY resolved
    dimension happens to differ at "team" is irrelevant here: the point is
    that `covers_scope` never climbs from "team" up to "company" or vice
    versa on the consumer's behalf -- it is a single exact-match lookup at
    one level, by construction (see `ontary.security.covers_scope`
    docstring: "a future rule ... could change without breaking callers" --
    i.e. parent-covers-child is explicitly deferred, not yet implemented).

    This is not a bug: M3.5 explicitly defers "parent covers child scope
    coverage" (spec §5 open-questions table: "defer; document the
    exact-match limitation. Decide the rule when a real consumer needs
    it."). This test exists to make the CURRENT behavior visible and
    intentional, not to prescribe the eventual rule.
    """
    consumer = make_consumer(
        actor_id="u1",
        role="Admin",
        scope_level="company",
        scope_id="company-1",
        kind="human",
    )
    # A row that resolves ONLY at "team" (e.g. its "company" dimension is
    # unresolved even though team-a is, in the domain's real hierarchy, a
    # child of company-1) is denied -- `covers_scope` looks at
    # `resolved["company"]`, which is None here, not at `resolved["team"]`.
    resolved = {"team": "team-a", "company": None}
    assert covers_scope(make_policy(levels=LEVELS, min_n=3), consumer, resolved) is False


def test_team_scoped_consumer_does_not_get_company_wide_access(
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    # The mirror image: a team-scoped consumer is checked at "team" only,
    # never automatically granted every row under its parent company just
    # because the two share a company id.
    consumer = make_consumer(
        actor_id="u1",
        role="TeamOwner",
        scope_level="team",
        scope_id="team-a",
        kind="human",
    )
    resolved = {"team": None, "company": "company-1"}
    assert covers_scope(make_policy(levels=LEVELS, min_n=3), consumer, resolved) is False
