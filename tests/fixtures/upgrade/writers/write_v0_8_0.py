"""FROZEN writer for the `tests/fixtures/upgrade/v0.8.0/` upgrade fixture.

Seeds EXACTLY the shape `examples.tickets.fixtures.load_fixtures` seeds at
v0.8.0 (Org / two Queues / two Agents / three Tickets / one Comment -- NOT
via that function itself, see below: `git show v0.8.0:examples/tickets/
fixtures.py`), runs the one governed action that exists at v0.8.0
(`EscalateTicket`, whose effect dispatches inline), and calls `ticketStats`
on both queues: queue_a (two tickets) clears this ontology's `min_n=2` and
returns a value; queue_b (one ticket) does not and refuses with
`VisibilityError`/`MIN_N_VIOLATION` -- v0.8.0's real 3-ticket seed already
exercises both the aggregate leg and the refusal leg of AC4 with ZERO
divergence from what that release's reference app actually wrote. Then
writes:

- `tickets.sqlite` -- a real store file, opened and written by THIS tag's own
  `ontary.store.ObjectStore` (never synthesized DDL: spec `v1-acceptance-gate`
  §6 "AC4" -- DDL synthesis proves schema SHAPE, never that a tag's actual
  code wrote the file).
- `expected.json` -- the post-upgrade reads/aggregates/audit
  `tests/test_upgrade_fixtures.py` (T8, not this task) will assert, plus
  metadata: the observed `PRAGMA user_version` schema stamp and the recorded
  ontology fingerprint.

## How to run

Only ever under a v0.8.0 `ontary` venv, with `examples.tickets` resolved from
a v0.8.0 CHECKOUT -- never from this repo's own tree, which has since moved
on. From this repo's root:

    git worktree add /tmp/ontary-v0.8.0 v0.8.0
    uv venv /tmp/ontary-v0.8.0/.venv
    uv pip install --python /tmp/ontary-v0.8.0/.venv/bin/python /tmp/ontary-v0.8.0
    PYTHONPATH=/tmp/ontary-v0.8.0 /tmp/ontary-v0.8.0/.venv/bin/python \\
        tests/fixtures/upgrade/writers/write_v0_8_0.py
    git worktree remove /tmp/ontary-v0.8.0

`PYTHONPATH` is the WORKTREE ROOT (not its `src/`): `examples.tickets`
resolves from there, while `ontary` itself resolves from the venv's INSTALLED
distribution -- built from that same worktree via `uv pip install
<worktree-path>`, never a `git+...ontary@vX` URL. A git URL would need
network for a step `make verify` never takes, and would also trip the
current-tag doc-pin check at `tests/test_packaging.py`
(`test_documented_install_ref_matches_the_current_version`). Installing
from the local path is also what makes `ontary.__version__` report real
installed metadata (`"0.8.0"`) instead of the `"0+unknown"` a bare
`sys.path` import reports -- which is exactly the signal the version guard
below depends on.

This script writes only two files, always at fixed paths next to it
(`../v0.8.0/tickets.sqlite`, `../v0.8.0/expected.json`, resolved from
`__file__`, never from the current working directory), and always starts by
deleting any pre-existing copy -- so running it twice regenerates both from
nothing rather than doubling their contents.

## Frozen

This file is FROZEN the moment it is first committed (spec
`v1-acceptance-gate` §6 "AC4"): it is written against the v0.8.0 API on
purpose and must never be "fixed up" to compile against a later one -- a
later ontary removing something this file uses is not a bug in this file.
`tests/test_upgrade_fixtures.py` (T8) proves the FIXTURE still opens under
the CURRENT SDK; the fixture-honesty CI job (T9, `.github/workflows/
verify.yml`) proves THIS SCRIPT, re-run unmodified under the v0.8.0 venv,
still reproduces the committed `tickets.sqlite` byte-for-semantic-content --
see `scripts/dump_store.py`. A change here with no matching fixture
regeneration is exactly the drift that job exists to catch.

## Freeze exemption (T7, 2026-08-31)

This file and `../v0.8.0/expected.json` were edited and REGENERATED once,
after both were first committed FROZEN at `78e22b7` (T6), under a
human-approved freeze exemption (ledger `v1-acceptance-gate/T7`,
`amendment-2`, approved via the `/build` forcing question).

T7 (a later task, `../writers/write_v0_7_0.py`) was told to record
`metadata.ontology_fingerprint` as a labeled `{note, as_written,
expected_after_upgrade}` object -- fixing a shortcoming of what this file
used to commit under that same key: the flat, PRE-upgrade fingerprint only,
with no note distinguishing it from a post-upgrade value. T7's own review
then found the two committed fixtures' `metadata.ontology_fingerprint`
DISAGREE IN SHAPE as a result -- flat here, wrapped there -- so
`tests/test_upgrade_fixtures.py` (T8, depends on both T6 and T7) could not
open both fixtures with ONE parametrized reader per spec AC4/§7: either
shape `KeyError`s on the other, and a `.get()`-tolerant reader would only
trade that crash for silently VACATING the fingerprint assertion on
whichever tag it skips -- the exact failure class this feature keeps
reopening. ONE of the two shapes had to move; T7's is the better one (it
records `as_written` too, not only the post-upgrade value), so THIS file
moved to match it, never the reverse.

Why this is not a policy breach: spec `v1-acceptance-gate` §11 makes the
AC5/T9 fixture-honesty CI job the freeze's enforcement, and names the one
accepted residual explicitly: "a simultaneous edit of writer+fixture ...
that requires deliberate action, and the diff shows both files." This edit
is exactly that -- deliberate, visible in one diff, on an UNMERGED branch,
to a fixture that has never shipped in a release. The freeze protects
RELEASED artifacts from silent rot; it does not forbid iterating on a
fixture before the first release that carries it.

`../v0.8.0/tickets.sqlite` is UNCHANGED by this edit: only `expected.json`'s
`metadata.ontology_fingerprint` SHAPE moved, from a flat object to the
`{note, as_written, expected_after_upgrade}` wrapper -- `as_written` is the
SAME `fingerprint.model_dump(mode="json")` value the flat shape used to
record directly, and `expected_after_upgrade` is the SAME
`_ONTOLOGY_FINGERPRINT_EXPECTED_AFTER_UPGRADE` literal `write_v0_7_0.py`
already carries (identical by construction, not merely by assumption:
v0.7.0 and v0.8.0's `examples/tickets` are the same tree, `a02c4f5`, so the
CURRENT registry either fixture's `expected_after_upgrade` describes is the
same one). Nothing about how the store itself is opened, seeded, or
governed changed -- neither wrong-venv guard below moved, and the seed,
the one `EscalateTicket` execution, and the two `ticketStats` calls are
byte-for-byte what T6 committed. Confirmed by `scripts/dump_store.py`: this
edit's sha256 over the regenerated `tickets.sqlite` matches the sha256 over
the file it replaced, `5192b8cd303a1be94f7a914ae0e915e1822fe9674284e819f7a
314b0bcb5d601` -- see this task's report for the transcript.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ontary
from examples.tickets.ontology import NOTIFY_TICKET_ESCALATION, build_ontology
from ontary import Consumer, ObjectStore, Source, VisibilityError
from ontary.testing import FixedClock, SequentialIds, capture_effects

TAG = "v0.8.0"

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / TAG
_STORE_PATH = _FIXTURE_DIR / "tickets.sqlite"
_EXPECTED_PATH = _FIXTURE_DIR / "expected.json"

#: Arbitrary and fixed -- the exact instant carries no meaning beyond "the
#: same one on every run", which is what `ontary.testing.FixedClock` needs
#: of it.
_INSTANT = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"write_v0_8_0.py: {message}")


def _check_running_under_v0_8_0() -> None:
    """Guards a wrong-OLDER-venv run (spec §8 edge case): a v0.7.0
    `ObjectStore` refuses a v9 file outright, so this assertion is what
    turns that refusal into an explicit, early message instead of a raw
    stack trace three steps later -- checked BEFORE anything is written to
    disk (this is the first statement `main()` runs).

    Breaking input this fixture cannot itself construct: running this exact
    file under the v0.7.0 venv (a second, differently-versioned venv rather
    than anything this fixture's own data can express) -- exercised
    directly, see the T6 report for the transcript.

    Does NOT catch every wrong-venv input: `pyproject.toml` at THIS repo's
    own HEAD still declares `version = "0.8.0"`, so `ontary.__version__` is
    `"0.8.0"` there too, and running this script under HEAD's own venv
    passes this exact assertion (a prior review of this file, P1-3, found
    this docstring FALSELY claiming that case was "proven separately" --
    it was not proven, and this assertion does not catch it; corrected
    here, not silently). `_check_ontary_did_not_resolve_from_this_repos_own_src`
    below is the ADDITIONAL guard (L31: additive, not a replacement) that
    catches that specific input.
    """
    _require(
        ontary.__version__ == "0.8.0",
        "must run under the v0.8.0 ontary venv (see this module's docstring) -- "
        f"found ontary.__version__ == {ontary.__version__!r}",
    )


def _check_ontary_did_not_resolve_from_this_repos_own_src() -> None:
    """Guards the wrong-venv input the version check above CANNOT see
    (P1-3, prior review of this file): this repo's own HEAD checkout still
    declares `version = "0.8.0"` in `pyproject.toml`, so running this
    script under HEAD's own dev venv -- e.g. forgetting the `--python
    /tmp/ontary-v0.8.0/.venv/bin/python` half of the documented run recipe
    above, so a bare `python`/`uv run` falls back to this repo's own
    `.venv` -- passes `ontary.__version__ == "0.8.0"` too, and silently
    writes a fixture built from HEAD's `src/ontary`, not the tag's.

    There is no ENGINE-CODE marker this could look for instead: `git diff
    v0.8.0 HEAD -- src/ontary` is EMPTY as of this writing (T1 touched only
    `examples/tickets` and tests) -- the two engines really are
    byte-identical today, so nothing about calling INTO `ontary` can tell
    them apart. What differs is INSTALLATION PROVENANCE, not code: a real
    v0.8.0-tag venv installs `ontary` as a BUILT distribution (`uv pip
    install /tmp/ontary-v0.8.0`, never editable -- see the docstring above),
    landing it under that venv's OWN `site-packages/ontary/`; this repo's
    own dev venv (`make setup` = `uv sync`) installs it EDITABLE, landing
    it at THIS repo's `<repo-root>/src/ontary/__init__.py` verbatim -- so
    compute `<repo-root>/src` from THIS SCRIPT's own `__file__` (never a
    hardcoded absolute path: this file always lives at `<repo-root>/tests/
    fixtures/upgrade/writers/write_v0_8_0.py`, whatever that repo root
    actually is, locally or in the T9 CI honesty job's checkout) and
    refuse if the resolved `ontary.__file__` sits anywhere inside it.

    Breaking input proven to red: running this file, UNMODIFIED, under
    THIS repo's own HEAD venv with `PYTHONPATH` correctly set to a v0.8.0
    worktree checkout (i.e. exactly the P1-3 repro) -- exercised directly,
    see the T6 report for the transcript: exit non-zero, no file written.
    """
    this_repo_src = Path(__file__).resolve().parents[4] / "src"
    module_file = ontary.__file__
    _require(module_file is not None, "the `ontary` module has no __file__ to check")
    assert module_file is not None
    ontary_path = Path(module_file).resolve()
    _require(
        this_repo_src not in ontary_path.parents,
        f"ontary resolved from {ontary_path}, which is INSIDE this repo's own "
        f"{this_repo_src} tree -- this is HEAD's own venv (or an editable "
        "install of it), not a real v0.8.0 tag venv. Re-run with --python "
        "pointed at a venv built from `uv pip install <v0.8.0-worktree-path>` "
        "(see this module's docstring)",
    )


def _check_examples_tickets_is_the_v0_8_0_shape(ontology: Any) -> None:
    """Guards the OTHER half of a wrong-venv run the version check above
    cannot see: `PYTHONPATH` pointing at the WRONG checkout while `ontary`
    itself is correctly the v0.8.0 install (e.g. `PYTHONPATH` omitted
    entirely, silently falling back to THIS repo's own `examples/tickets`
    via the current working directory). `examples.tickets` at v0.8.0
    (dispatcher-verified fact, this task's brief) declares exactly 5 object
    types and a `Ticket` at version 1 with no `channel` property; both
    became false only once T1 landed on this branch. If either check fails
    here, `examples.tickets` did not resolve from the v0.8.0 worktree named
    on the command line.

    Breaking input proven to red: running this file with `PYTHONPATH` unset
    (or pointed at this repo's own root) instead of a v0.8.0 worktree --
    exercised directly, see the T6 report.
    """
    object_type_names = set(ontology.registry.object_types)
    _require(
        object_type_names == {"Org", "Queue", "Agent", "Ticket", "Comment"},
        "examples.tickets resolved to the WRONG object-type set "
        f"({sorted(object_type_names)!r}) -- PYTHONPATH must list the v0.8.0 "
        "worktree checkout, so `import examples.tickets` resolves to that "
        "tag's app, not today's",
    )
    ticket_def = ontology.registry.get_object_type("Ticket")
    channel_present = any(prop.name == "channel" for prop in ticket_def.properties)
    _require(
        ticket_def.version == 1 and not channel_present,
        "examples.tickets resolved to a POST-v0.8.0 Ticket shape "
        f"(version={ticket_def.version}, channel present={channel_present}) -- "
        "PYTHONPATH must list the v0.8.0 worktree checkout BEFORE this "
        "repo's own root, so `import examples.tickets` resolves to that "
        "tag's app, not today's",
    )


def _read_schema_version(path: Path) -> int:
    """The `PRAGMA user_version` this tag's `ObjectStore` just stamped.

    Not exposed by any public `ontary` API (there is no `store.
    schema_version`), so this reads it the same way `scripts/dump_store.py`
    does: plain `sqlite3`, opened read-only, after the writing connection
    above has already committed and this function's own connection is
    closed again before any other writer touches the file.
    """
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        _require(row is not None, "PRAGMA user_version returned no row")
        assert row is not None
        return int(row[0])
    finally:
        conn.close()


def _stored_payload(store: ObjectStore, obj_type: str, obj_id: str) -> dict[str, Any]:
    row = store.read_current(obj_type, obj_id)
    _require(row is not None, f"expected a stored {obj_type}/{obj_id} row, found none")
    assert row is not None
    return row.payload


#: The `covered` list `check_ontology_fingerprint` (`ontary.store.protocol`)
#: builds by walking every changed fingerprint key in SORTED
#: `f"{kind}:{api_name}"` order and classifying each: T1 added six brand
#: new declarations with no stored rows -- the `Escalation` object, its two
#: links (`escalationAssignedTo`, `escalationOnTicket`), and three new
#: actions (`ArchiveTicket`, `OpenEscalation`, `ResolveTicket`) -- plus one
#: VERSIONED change, `Ticket`'s v1->v2 upcaster chain. This writer runs
#: under the v0.8.0 venv and so cannot import HEAD's `examples.tickets` to
#: derive this list itself (that is exactly what the wrong-venv guards
#: above exist to keep it from doing by accident) -- like the
#: `"channel": "email"` transform below, it is a hand-verified
#: reproduction of a LATER SDK's real behaviour, not something computed
#: here. Verified once by actually opening a copy of THIS writer's own
#: regenerated `tickets.sqlite` with the CURRENT `examples.tickets.
#: build_ontology()` under this repo's HEAD venv and reading back
#: `store.audit_entries()[2].params["covered"]` verbatim -- see the T6
#: report for the transcript. `git diff v0.8.0 HEAD -- src/ontary` was
#: EMPTY at verification time (only `examples/tickets` and tests changed
#: under T1) -- if a later change ever adds, removes, or renames a T1-era
#: declaration, `tests/test_upgrade_fixtures.py` (T8) will red on this
#: exact list, which is the point: T5b's own acceptance criteria name
#: keeping this fixture green as a condition of any further edit here.
_ACCEPT_VERSIONED_ONTOLOGY_CHANGE_COVERED: list[str] = [
    "action 'ArchiveTicket': newly declared (no stored rows)",
    "action 'OpenEscalation': newly declared (no stored rows)",
    "action 'ResolveTicket': newly declared (no stored rows)",
    "link 'escalationAssignedTo': newly declared (no stored rows)",
    "link 'escalationOnTicket': newly declared (no stored rows)",
    "object 'Escalation': newly declared (no stored rows)",
    "object 'Ticket': version 1 -> 2, covered by declared upcasters",
]

#: `fingerprint_ontology(<the CURRENT examples.tickets registry>).digest` --
#: verified the same way as `_ACCEPT_VERSIONED_ONTOLOGY_CHANGE_COVERED`
#: above (same transcript covers both), for the same reason: this writer,
#: confined to the v0.8.0 venv, has no way to import HEAD's `ontary`/
#: `examples.tickets` to compute a hash over a registry it cannot see.
_ACCEPT_VERSIONED_ONTOLOGY_CHANGE_CURRENT_DIGEST = (
    "c27e85c6385d794098ce730594a7391d2dd6d8403b93e9b25711e5d898240d92"
)

#: `check_ontology_fingerprint` stamps this entry's `ts` straight from
#: `datetime.now(timezone.utc)` inside `ObjectStore.__init__` -- unlike
#: every OTHER timestamp in this fixture, there is no `clock=` seam on a
#: bare `ObjectStore(registry, path)` constructor call for ANY writer
#: (this one or T8's) to inject into, so it is genuinely not reproducible.
#: A self-describing placeholder, exactly like `scripts/dump_store.py`'s
#: own volatile-column placeholders: T8 must ignore/normalize this ONE
#: field on the third entry rather than compare it.
_ACCEPT_VERSIONED_ONTOLOGY_CHANGE_TS_PLACEHOLDER = (
    "<real wall-clock time at fixture OPEN, not reproducible -- ignore/normalize>"
)

#: The FULL fingerprint object (`digest` + `types` + `versions`)
#: `store.read_ontology_fingerprint()` returns once the CURRENT SDK opens
#: this fixture and re-stamps it (`check_ontology_fingerprint` ->
#: `store.write_ontology_fingerprint(current, adopted=False)`,
#: `ontary.store.protocol`) -- its `digest` is the SAME string as
#: `_ACCEPT_VERSIONED_ONTOLOGY_CHANGE_CURRENT_DIGEST` above BY
#: CONSTRUCTION (both are `fingerprint_ontology(<current registry>)`, read
#: two different ways), restated here in full so a T8 assertion against
#: `store.read_ontology_fingerprint()` directly has a literal to compare
#: against without reaching into the audit log's `params`. Hand-verified
#: the same way as the two constants above (same transcript covers all
#: three): this writer cannot import HEAD's registry to compute this
#: itself. IDENTICAL to `write_v0_7_0.py`'s own copy of this constant
#: (cross-checked, not assumed: v0.7.0 and v0.8.0's `examples/tickets` are
#: the same tree, `a02c4f5`, so the CURRENT registry being fingerprinted is
#: the same one either way, and cannot differ by which OLD fixture is
#: opening it). Added under the T7 freeze exemption (see this module's
#: docstring): `metadata.ontology_fingerprint` below now records this SAME
#: `{note, as_written, expected_after_upgrade}` shape `write_v0_7_0.py`
#: uses, in place of the flat, PRE-upgrade-only fingerprint this fixture
#: used to commit under that key.
_ONTOLOGY_FINGERPRINT_EXPECTED_AFTER_UPGRADE: dict[str, Any] = {
    "digest": "c27e85c6385d794098ce730594a7391d2dd6d8403b93e9b25711e5d898240d92",
    "types": {
        "action:ArchiveTicket": "1da106e2b56d8969a4109a9da525b8d06e80db4172346f3a31876ea8624755e2",
        "action:EscalateTicket": "b78c472439957a13431bb6e1d153299671d4a024b7ba887b9054fa9147f8ba61",
        "action:OpenEscalation": "3a9dea9096b308982db1b13720185fc231ddf624337ae5981f3fcf64cd148bd7",
        "action:ResolveTicket": "160b693f6f111250435e4f56f0859aa537da3358f200394e7c96bbfbb6773034",
        "effect:NotifyTicketEscalation": "632cc602ecc33de3de0c0b9e89489ff71bbd60248f0207f09f25bb5ecbb8f0cc",
        "function:ticketStats": "416059e7a16dbb3c38161e0d19d7615752565610c8f8ce3ed22c2fd810fa8fe4",
        "link:commentByAgent": "b289740c27eb19a3d144ee5def7bb1d0d8cc20eff34746c1550e859a4e5eb910",
        "link:commentOnTicket": "8c15b70ab4cd34dab3805aab107e8d096fcaa3cd9fa05e0698e2b00264e8ed54",
        "link:escalationAssignedTo": "2a3debac070606ab0f9adf39e9106131bbd137671aca8dabb13e7b8378da9cc6",
        "link:escalationOnTicket": "e3ce9fa5e1ecf4025d7dc955b9848ff17171a131a2f27b4ebd5361c016964e9e",
        "link:queueOfOrg": "f5cd564f25b3388fec02960d481830ad8f757c8acedc6be94db886c3a4ebb7df",
        "link:ticketInQueue": "f12ec842da4ea41783f3a2f1c90da8da086f06fd96993ecc47c04ae8b6da8c06",
        "object:Agent": "d346e4ce43f57f7b02587103f98873582caea820f99ae135f8c8307ae9e14398",
        "object:Comment": "a8a999911712d98e1ac4db0172d356d7642f408be41560d1b18413954b8f70a6",
        "object:Escalation": "e6c7d27c02f4e7d102eb4c39e58d06414f7b7fca62b49940dc4c67ebf7c54301",
        "object:Org": "4472e03643f29b4b02cbd6e440a4327b2b5d9ebaaf8346b098558cd761dced6d",
        "object:Queue": "6380ef060baa7c09692f372a7bdb85dca2b1896289b696f44d5a8aa07981b94f",
        "object:Ticket": "fd20bba6978589881f816f053ffcdc338b92ece8ced1930e049da4433ccf891b",
    },
    "versions": {
        "Agent": 1,
        "Comment": 1,
        "Escalation": 1,
        "Org": 1,
        "Queue": 1,
        "Ticket": 2,
    },
}


def _accept_versioned_ontology_change_entry(previous_digest: str) -> dict[str, Any]:
    """The one audit entry `expected.json` cannot record `as_written`: the
    third entry `store.audit_entries()` will hold once a LATER SDK opens
    this fixture and `check_ontology_fingerprint` admits T1's changes as
    version-covered (spec `v1-acceptance-gate` §6 "AC4" audit leg; prior
    review of this file, P1-1 -- this fixture used to omit it entirely).

    `previous_digest` is the one field of the three under `params` this
    writer DOES know live (it is this run's own stamped fingerprint, read
    back from the store below, never re-typed here) -- only
    `current_digest`/`covered` are the hand-verified constants above.

    Breaking input this fixture structurally cannot construct: a T8 author
    slicing the asserted array back down to the original two entries would
    make this addition pointless, but nothing in THIS file can prove that
    T8 doesn't do that -- T8 (not this task) is where that assertion has
    to live. What this function CAN and does prove is that the literal
    T8 will need is actually present and correctly shaped in
    `expected.json` for T8 to assert against, rather than silently absent.
    """
    return {
        "ts": _ACCEPT_VERSIONED_ONTOLOGY_CHANGE_TS_PLACEHOLDER,
        "invocation_id": None,
        "actor": "ontary",
        "role": "system",
        "principal": None,
        "action": "AcceptVersionedOntologyChange",
        "target_type": "",
        "target_id": None,
        "params": {
            "previous_digest": previous_digest,
            "current_digest": _ACCEPT_VERSIONED_ONTOLOGY_CHANGE_CURRENT_DIGEST,
            "covered": _ACCEPT_VERSIONED_ONTOLOGY_CHANGE_COVERED,
        },
        "outcome": "ok",
        "writes": [],
        "effects": [],
        "capability_accesses": [],
        "kind": "migration",
    }


def main() -> None:  # noqa: C901 -- a straight-line seed script, not a decision tree
    _check_running_under_v0_8_0()
    _check_ontary_did_not_resolve_from_this_repos_own_src()

    ontology, _unused_memory_store = build_ontology()
    _check_examples_tickets_is_the_v0_8_0_shape(ontology)

    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-journal", "-wal", "-shm"):
        _STORE_PATH.with_name(_STORE_PATH.name + suffix).unlink(missing_ok=True)

    ids = SequentialIds("v080")
    clock = FixedClock(_INSTANT)
    src = Source(source_system="fixture-writer", source_id=TAG)
    store = ObjectStore(ontology.registry, str(_STORE_PATH))
    print(f"opened a fresh store at {_STORE_PATH}")

    # -- seed data: EXACTLY the shape `examples.tickets.fixtures.load_fixtures`
    #    seeds at v0.8.0 (one Org, two Queues, two Agents, three Tickets, one
    #    Comment -- `git show v0.8.0:examples/tickets/fixtures.py`). NOT via
    #    `load_fixtures` itself: that function mints every id via
    #    `uuid.uuid4()`, which is exactly the residual volatility this
    #    writer exists to avoid AT THE SOURCE (`ontary.testing.SequentialIds`
    #    instead), not paper over later in the dump. Two tickets land in
    #    queue_a (clears this ontology's `min_n=2`) and one in queue_b (does
    #    not) -- v0.8.0's real seed already exercises both the aggregate leg
    #    and the refusal leg of AC4 below with ZERO divergence from what
    #    that release's reference app actually wrote (a prior review of
    #    this file, P1-2, found a 4th, invented ticket here; removed).
    org_id = ids()
    store.insert("Org", {"id": org_id, "name": "Acme Support"}, src)

    queue_a_id = ids()
    store.insert("Queue", {"id": queue_a_id, "name": "Billing"}, src)
    queue_b_id = ids()
    store.insert("Queue", {"id": queue_b_id, "name": "Onboarding"}, src)
    store.create_link("queueOfOrg", queue_a_id, org_id)
    store.create_link("queueOfOrg", queue_b_id, org_id)

    agent_1_id = ids()
    store.insert(
        "Agent", {"id": agent_1_id, "display_name": "Ada", "email": "ada@acme.test"}, src
    )
    agent_2_id = ids()
    store.insert(
        "Agent", {"id": agent_2_id, "display_name": "Bo", "email": "bo@acme.test"}, src
    )

    ticket_1_id = ids()
    store.insert(
        "Ticket",
        {
            "id": ticket_1_id,
            "subject": "Invoice mismatch",
            "age_hours": 4.0,
            "status": "open",
            "queue_id": queue_a_id,
        },
        src,
    )
    ticket_2_id = ids()
    store.insert(
        "Ticket",
        {
            "id": ticket_2_id,
            "subject": "Refund request",
            "age_hours": 12.0,
            "status": "open",
            "queue_id": queue_a_id,
        },
        src,
    )
    ticket_3_id = ids()
    store.insert(
        "Ticket",
        {
            "id": ticket_3_id,
            "subject": "Welcome call",
            "age_hours": 1.0,
            "status": "open",
            "queue_id": queue_b_id,
        },
        src,
    )
    for ticket_id, queue_id in (
        (ticket_1_id, queue_a_id),
        (ticket_2_id, queue_a_id),
        (ticket_3_id, queue_b_id),
    ):
        store.create_link("ticketInQueue", ticket_id, queue_id)

    comment_id = ids()
    store.insert("Comment", {"id": comment_id, "text": "Looking into this now."}, src)
    store.create_link("commentOnTicket", comment_id, ticket_1_id)
    store.create_link("commentByAgent", comment_id, agent_1_id)
    print("seeded 1 Org, 2 Queues, 2 Agents, 3 Tickets, 1 Comment")

    # -- one governed action + its effect, so the fixture exercises the
    #    write/audit/outbox surfaces T8 will assert against, not only
    #    raw-inserted data. `id_factory=ids` continues the SAME sequential
    #    counter used above, so invocation/effect ids stay deterministic
    #    too. No separate `drain_effects()` call: `ActionExecutor.execute`
    #    already attempts delivery INLINE, post-commit, for any effect
    #    whose handle has a bound dispatcher (`_dispatch_inline`, read
    #    directly to confirm this) -- the outbox row is therefore already
    #    resolved by the time `execute()` returns, and draining again would
    #    only prove `claim_due_effects` correctly finds nothing left to
    #    claim, not exercise anything new.
    dispatched = capture_effects()
    runtime = ontology.bind(
        store, clock=clock, id_factory=ids, effects={NOTIFY_TICKET_ESCALATION: dispatched}
    )
    agent_a = Consumer(
        actor_id=agent_1_id, role="Agent", scope_level="queue", scope_id=queue_a_id, kind="human"
    )
    client_a = runtime.for_consumer(agent_a)
    escalate_result = client_a.execute(
        "EscalateTicket", {"ticket_id": ticket_1_id, "reason": "SLA exceeded"}
    )
    _require(
        escalate_result == {"ticket_id": ticket_1_id},
        f"unexpected EscalateTicket result: {escalate_result!r}",
    )
    _require(
        len(dispatched.effects) == 1,
        f"expected EscalateTicket's one effect to dispatch inline, got {dispatched.effects!r}",
    )
    print("executed EscalateTicket on ticket_1; its effect dispatched inline")

    agent_b = Consumer(
        actor_id=agent_2_id, role="Agent", scope_level="queue", scope_id=queue_b_id, kind="human"
    )
    client_b = runtime.for_consumer(agent_b)
    queue_a_stats = client_a.call_function("ticketStats", {"queue_id": queue_a_id})
    print(f"computed ticketStats(queue_a) = {queue_a_stats}")

    # queue_b has exactly one Ticket (v0.8.0's real seed, unmodified -- P1-2)
    # -- below this ontology's `min_n=2`, so this call refuses rather than
    # releasing a mean over a single contributor. Exercised deliberately,
    # not avoided: this is the app's OWN min-N guard (mirrored, for the
    # current shape, at `tests/test_examples_smoke.py`'s
    # `test_ticket_aggregate_enforces_min_n`), and recording the refusal
    # here is strictly more faithful to what v0.8.0 actually seeded than
    # inventing a 4th ticket to make queue_b clear the bar too would have
    # been. `ticketStats` declares no capabilities and releases no hidden
    # field, so `FunctionDef.audited` is `False` and this refusal appends
    # NOTHING to the audit log (`OntologyClient.call_function`, read
    # directly to confirm this) -- it costs nothing else in this fixture.
    try:
        client_b.call_function("ticketStats", {"queue_id": queue_b_id})
    except VisibilityError as exc:
        _require(
            exc.code == "MIN_N_VIOLATION",
            f"expected queue_b's ticketStats call to refuse with "
            f"MIN_N_VIOLATION, got code {exc.code!r}",
        )
        print(f"ticketStats(queue_b) refused: VisibilityError/{exc.code}")
    else:
        raise SystemExit(
            "write_v0_8_0.py: expected queue_b's ticketStats call to refuse "
            "(only one Ticket, below min_n=2) -- it returned a value instead"
        )

    # -- everything expected.json needs is now read back from the store
    #    itself (ground truth), never hand-predicted.
    fingerprint = store.read_ontology_fingerprint()
    _require(
        fingerprint is not None,
        "the store must have a stamped ontology fingerprint immediately after "
        "ObjectStore.__init__",
    )
    assert fingerprint is not None

    tickets_as_written = {
        ticket_id: _stored_payload(store, "Ticket", ticket_id)
        for ticket_id in (ticket_1_id, ticket_2_id, ticket_3_id)
    }
    tickets_expected_after_upgrade = {
        ticket_id: {**payload, "channel": "email"}
        for ticket_id, payload in tickets_as_written.items()
    }
    audit_as_written = [entry.model_dump(mode="json") for entry in store.audit_entries()]

    expected: dict[str, Any] = {
        "metadata": {
            "tag": TAG,
            "ontary_version": ontary.__version__,
            # `Z`, matching how pydantic serializes every other UTC instant in
            # this same file (`AuditEntry.ts`, etc.) under `mode="json"` --
            # `_INSTANT` is always exactly UTC, so this replacement is exact,
            # never a lossy approximation.
            "generated_at": _INSTANT.isoformat().replace("+00:00", "Z"),
            "schema_version": _read_schema_version(_STORE_PATH),
            "ontology_fingerprint": {
                "note": (
                    "`as_written` is the fingerprint THIS writer's own "
                    "`ObjectStore.__init__` stamped when it first opened an "
                    "empty file -- the v0.8.0 `examples.tickets` declarations, "
                    "5 object types, `Ticket` at version 1. "
                    "`expected_after_upgrade` is the CURRENT `examples.tickets` "
                    "registry's fingerprint, i.e. what `check_ontology_"
                    "fingerprint` (`ontary.store.protocol`) re-stamps via "
                    "`write_ontology_fingerprint(current, adopted=False)` the "
                    "moment a LATER SDK opens this file and classifies every "
                    "T1 change as version-covered -- the SAME digest as "
                    "`audit.expected_after_upgrade`'s third entry's "
                    "`params.current_digest`, restated here in full "
                    "(`digest`+`types`+`versions`) so a T8 assertion against "
                    "`store.read_ontology_fingerprint()` directly has a "
                    "literal to compare against without reaching into the "
                    "audit log. This writer runs under v0.8.0 and cannot "
                    "import HEAD's registry to compute `expected_after_"
                    "upgrade` itself, so -- like `audit.expected_after_"
                    "upgrade`'s third entry -- it is a hand-verified literal, "
                    "not something computed here."
                ),
                "as_written": fingerprint.model_dump(mode="json"),
                "expected_after_upgrade": _ONTOLOGY_FINGERPRINT_EXPECTED_AFTER_UPGRADE,
            },
        },
        "ids": {
            "org_id": org_id,
            "queue_a_id": queue_a_id,
            "queue_b_id": queue_b_id,
            "agent_1_id": agent_1_id,
            "agent_2_id": agent_2_id,
            "ticket_1_id": ticket_1_id,
            "ticket_2_id": ticket_2_id,
            "ticket_3_id": ticket_3_id,
            "comment_id": comment_id,
        },
        "tickets": {
            "note": (
                "`as_written` is the exact v1-shape payload this writer stored "
                "(no `channel` key: Ticket was declared version=1 at v0.8.0). "
                '`expected_after_upgrade` is that same payload with `channel: '
                "\"email\"` merged in, mirroring the `_ticket_v1_to_v2` "
                "upcaster's documented `{**payload, \"channel\": \"email\"}` "
                "transform (ontary v1-acceptance-gate spec, T1). This writer "
                "runs under v0.8.0 and cannot import or call that upcaster "
                "itself, so the transform is reproduced here as a literal, "
                "for a later test to assert the CURRENT SDK reproduces it "
                "on read."
            ),
            "as_written": tickets_as_written,
            "expected_after_upgrade": tickets_expected_after_upgrade,
        },
        "aggregates": {
            "ticketStats": {
                "note": (
                    "v0.8.0's real seed (unmodified, P1-2) puts two Tickets "
                    "in queue_a and one in queue_b, against this ontology's "
                    "`min_n=2`. `values` holds every queue whose call "
                    "returned a mean; `refused` holds every queue whose "
                    "call raised `VisibilityError` instead, keyed to the "
                    "error `code` it raised (never a value -- there isn't "
                    "one)."
                ),
                "values": {queue_a_id: queue_a_stats},
                "refused": {queue_b_id: "MIN_N_VIOLATION"},
            },
        },
        "audit": {
            "note": (
                "`as_written` is exactly what THIS writer's two "
                "`EscalateTicket` entries recorded -- nothing else, because "
                "nothing has yet evaluated this store's ontology "
                "fingerprint against a LATER registry. "
                "`expected_after_upgrade` appends the ONE further entry "
                "`check_ontology_fingerprint` (`ontary.store.protocol`) "
                "writes the moment the CURRENT SDK's `ObjectStore.__init__` "
                "classifies every T1 change here as version-covered -- "
                "`AcceptVersionedOntologyChange`, the record of WHY the "
                "fingerprint change was admitted, not merely that it was. "
                "So `as_written` is a PREFIX of `expected_after_upgrade`, "
                "and a T8 assertion must cover the full three-entry array, "
                "never slice it back down to two (a prior review of this "
                "file, P1-1, found this array missing that third entry "
                "entirely). The one exception is that third entry's `ts`: "
                "see `_ACCEPT_VERSIONED_ONTOLOGY_CHANGE_TS_PLACEHOLDER`'s "
                "own comment in this script -- it is a real wall-clock "
                "stamp with no reproducible seam, so T8 must ignore/"
                "normalize that ONE field rather than compare it, exactly "
                "like `scripts/dump_store.py`'s own volatile-column "
                "placeholders."
            ),
            "as_written": audit_as_written,
            "expected_after_upgrade": [
                *audit_as_written,
                _accept_versioned_ontology_change_entry(
                    fingerprint.model_dump(mode="json")["digest"]
                ),
            ],
        },
        "outbox": [record.model_dump(mode="json") for record in store.outbox_entries()],
    }
    _EXPECTED_PATH.write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n")
    print(f"wrote {_STORE_PATH}")
    print(f"wrote {_EXPECTED_PATH}")


if __name__ == "__main__":
    main()
