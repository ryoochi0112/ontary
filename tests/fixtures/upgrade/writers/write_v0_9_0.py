"""FROZEN writer for the `tests/fixtures/upgrade/v0.9.0/` upgrade fixture.

Unlike `write_v0_7_0.py`/`write_v0_8_0.py` -- each run under an OLDER tag's
own venv, driving that tag's OLDER `examples.tickets` app -- this writer runs
under THIS crossing's own venv, driving THIS crossing's own `examples.tickets`
app (`Ticket` already at version 2 with `channel`, the owned `Escalation`
object, and all four business verbs: `EscalateTicket`, `OpenEscalation`,
`ResolveTicket`, `ArchiveTicket`). Decision A (this task's ledger entry,
2026-09-03): the tag `v0.9.0` does not exist yet when this fixture is
generated -- bumping `pyproject.toml` forces the fixture into the SAME commit
as the version bump, so it is generated from the CURRENT TREE (a scratch venv
installed from this repo's own local path, not a `git+...@v0.9.0` URL, which
would need network `make verify` never takes and would also trip the
current-tag doc-pin check at `tests/test_packaging.py`). That tree is the one
the `v0.9.0` tag will point at once it is cut.

Because the writer and the registry that will read this fixture back (at
least until the NEXT crossing) are the SAME registry, this fixture's
`as_written` and `expected_after_upgrade` sections are IDENTICAL by
construction: there is no older `Ticket` shape here for the upcaster to
apply, and no fingerprint drift for `check_ontology_fingerprint`
(`ontary.store.protocol`) to classify and accept -- `stored.digest ==
current.digest` returns early with nothing appended
(`tests/test_upgrade_fixtures.py`'s `_normalized_audit`/
`_current_ticket_payloads` read this fixture's own `expected.json` to learn
that, rather than assuming it from this file's tag). This fixture becomes the
OLDER one the NEXT crossing's writer upcasts, the same way this one's `Ticket`
v1->v2 upcast is exercised by `v0.7.0`/`v0.8.0` today (see `docs/v1-gate.md`'s
"AC4 fixture honesty" section for the fuller statement).

Exercises every 0.9.0 business verb at least once (`EscalateTicket`,
`OpenEscalation`, `ResolveTicket`, `ArchiveTicket`) plus `ticketStats` on both
queues (queue_a clears `min_n=2`; queue_b, with one ticket, refuses), mirroring
`write_v0_8_0.py`'s small, deterministic three-ticket seed (Org / two Queues /
two Agents / three Tickets / one Comment) rather than `fixtures.load_fixtures`'s
larger synthetic set -- this is the frozen crossing fixture, not app-demo data.
Writes:

- `tickets.sqlite` -- a real store file, opened and written by THIS tag's own
  `ontary.store.ObjectStore` (never synthesized DDL: spec `v1-acceptance-gate`
  §6 "AC4").
- `expected.json` -- the post-upgrade reads/aggregates/audit
  `tests/test_upgrade_fixtures.py` asserts, plus metadata: the observed
  `PRAGMA user_version` schema stamp and the recorded ontology fingerprint.

## How to run

Only ever under a v0.9.0 `ontary` venv installed from THIS repo's own local
path (never editable, never a git URL), with `examples.tickets` resolved from
THIS repo's own root via `PYTHONPATH` -- from this repo's root:

    uv venv /path/to/scratch/.venv
    uv pip install --python /path/to/scratch/.venv/bin/python /path/to/this/repo
    PYTHONPATH=/path/to/this/repo /path/to/scratch/.venv/bin/python \\
        tests/fixtures/upgrade/writers/write_v0_9_0.py

Installing from the local path (never editable) is what makes
`ontary.__version__` report real installed metadata (`"0.9.0"`) instead of the
`"0+unknown"` a bare `sys.path` import reports, and what the wrong-venv guard
below depends on. `PYTHONPATH` is the REPO ROOT (not its `src/`), exactly as
`write_v0_8_0.py` documents.

This script writes only two files, always at fixed paths next to it
(`../v0.9.0/tickets.sqlite`, `../v0.9.0/expected.json`, resolved from
`__file__`, never from the current working directory), and always starts by
deleting any pre-existing copy -- so running it twice regenerates both from
nothing rather than doubling their contents.

## Frozen

This file is FROZEN the moment it is first committed (spec
`v1-acceptance-gate` §6 "AC4"): it is written against the v0.9.0 API on
purpose and must never be "fixed up" to compile against a later one.
`tests/test_upgrade_fixtures.py` proves the FIXTURE still opens under the
CURRENT SDK; the fixture-honesty CI job (`.github/workflows/verify.yml`)
proves THIS SCRIPT, re-run unmodified once the `v0.9.0` tag exists, still
reproduces the committed `tickets.sqlite` byte-for-semantic-content -- see
`scripts/dump_store.py`.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ontary
from examples.tickets.ontology import NOTIFY_TICKET_ESCALATION, build_ontology
from ontary import Consumer, ObjectStore, Source, VisibilityError
from ontary.testing import FixedClock, SequentialIds, capture_effects

TAG = "v0.9.0"

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / TAG
_STORE_PATH = _FIXTURE_DIR / "tickets.sqlite"
_EXPECTED_PATH = _FIXTURE_DIR / "expected.json"


class _DeterministicAutoIds:
    """Replaces `uuid.uuid4` for the duration of this writer's run.

    `OpenEscalation` (`examples/tickets/ontology.py`, frozen application
    code this writer must not edit) calls `ActionContext.insert("Escalation",
    ...)` with no client-supplied id -- `ontary.store._shared.prepare_insert`'s
    own docstring says the plain-`uuid.uuid4()` primary-key fallback for that
    case is "deliberately store-owned and is not runtime-seamed: tests that
    need deterministic object IDs should pass explicit primary keys", which
    an `ActionParams` class with no id field cannot do. Monkeypatching
    `uuid.uuid4` at the stdlib module level (`ontary.store._shared` does
    `import uuid`, not `from uuid import uuid4`, so this reaches its call
    site) is the only lever left -- `uuid.uuid5`, a stable hash, not a
    counter racing `SequentialIds`' own ids, so the two id spaces can never
    collide. Every OTHER id this fixture pins (Org/Queue/Agent/Ticket/
    Comment, invocation/effect ids) is already minted deterministically via
    `ids = SequentialIds(...)` / explicit primary keys, unaffected by this
    patch: `objects.page_token` is the only other `uuid.uuid4()` call site in
    the store (`ontary/store/sqlite.py`), and it is already normalized away by
    `scripts/dump_store.py` (`_VOLATILE_COLUMNS`), so patching it too is
    harmless.
    """

    def __init__(self, tag: str) -> None:
        self._tag = tag
        self._count = 0

    def __call__(self) -> uuid.UUID:
        self._count += 1
        # The seed keeps the pre-fork `ontos-` spelling on purpose: it is an opaque
        # hash input, and the committed fixture's Escalation ids derive from it.
        # Renaming it would silently change those ids and red the honesty job.
        return uuid.uuid5(uuid.NAMESPACE_URL, f"ontos-fixture-writer:{self._tag}:{self._count}")

#: Arbitrary and fixed -- the exact instant carries no meaning beyond "the
#: same one on every run", which is what `ontary.testing.FixedClock` needs
#: of it.
_INSTANT = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"write_v0_9_0.py: {message}")


def _check_running_under_v0_9_0() -> None:
    """Guards a wrong-venv run (spec §8 edge case): a mismatched `ontary`
    version turns into an explicit, early message instead of a raw stack
    trace three steps later -- checked BEFORE anything is written to disk
    (this is the first statement `main()` runs).

    Does NOT catch every wrong-venv input on its own:
    `_check_ontary_did_not_resolve_from_this_repos_own_src` below is the
    ADDITIONAL guard (L31: additive, not a replacement) that catches an
    editable/dev install of THIS SAME version (which would pass this
    version check while still resolving `ontary` from `<repo-root>/src`
    rather than a real built distribution).
    """
    _require(
        ontary.__version__ == "0.9.0",
        "must run under the v0.9.0 ontary venv (see this module's docstring) -- "
        f"found ontary.__version__ == {ontary.__version__!r}",
    )


def _check_ontary_did_not_resolve_from_this_repos_own_src() -> None:
    """Guards the wrong-venv input the version check above cannot see: this
    repo's own editable dev venv (`make setup` = `uv sync`) also reports
    `ontary.__version__ == "0.9.0"` once `pyproject.toml` is bumped, but lands
    `ontary` at THIS repo's own `<repo-root>/src/ontary/__init__.py` rather than
    a venv's own `site-packages/ontary/` -- so compute `<repo-root>/src` from
    THIS SCRIPT's own `__file__` (never a hardcoded absolute path) and refuse
    if the resolved `ontary.__file__` sits anywhere inside it.
    """
    this_repo_src = Path(__file__).resolve().parents[4] / "src"
    module_file = ontary.__file__
    _require(module_file is not None, "the `ontary` module has no __file__ to check")
    assert module_file is not None
    ontary_path = Path(module_file).resolve()
    _require(
        this_repo_src not in ontary_path.parents,
        f"ontary resolved from {ontary_path}, which is INSIDE this repo's own "
        f"{this_repo_src} tree -- this is an editable/dev install, not a real "
        "v0.9.0 build. Re-run with --python pointed at a venv built from "
        "`uv pip install <this-repo-path>` (see this module's docstring)",
    )


def _check_examples_tickets_is_the_v0_9_0_shape(ontology: Any) -> None:
    """Guards the OTHER half of a wrong-venv run: `PYTHONPATH` pointing at
    the wrong tree while `ontary` itself is correctly the v0.9.0 install.
    `examples.tickets` at v0.9.0 declares exactly 6 object types (the T1
    `Escalation` addition) and a `Ticket` at version 2 with a `channel`
    property.
    """
    object_type_names = set(ontology.registry.object_types)
    _require(
        object_type_names == {"Org", "Queue", "Agent", "Ticket", "Comment", "Escalation"},
        "examples.tickets resolved to the WRONG object-type set "
        f"({sorted(object_type_names)!r}) -- PYTHONPATH must list THIS repo's "
        "root, so `import examples.tickets` resolves to this crossing's app",
    )
    ticket_def = ontology.registry.get_object_type("Ticket")
    channel_present = any(prop.name == "channel" for prop in ticket_def.properties)
    _require(
        ticket_def.version == 2 and channel_present,
        "examples.tickets resolved to a PRE-v0.9.0 Ticket shape "
        f"(version={ticket_def.version}, channel present={channel_present}) -- "
        "PYTHONPATH must list THIS repo's root BEFORE any other checkout, so "
        "`import examples.tickets` resolves to this crossing's app",
    )


def _read_schema_version(path: Path) -> int:
    """The `PRAGMA user_version` this tag's `ObjectStore` just stamped.

    Not exposed by any public `ontary` API, so this reads it the same way
    `scripts/dump_store.py` does: plain `sqlite3`, opened read-only, after
    the writing connection above has already committed and this function's
    own connection is closed again before any other writer touches the file.
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


#: This writer's own registry IS the registry that reads this fixture back
#: (at least until the next crossing lands), so there is no fingerprint
#: drift for `check_ontology_fingerprint` to classify -- `covered` is
#: genuinely empty, unlike `write_v0_7_0.py`/`write_v0_8_0.py`'s six-item
#: T1 list. Kept as a named constant (rather than inlined) so
#: `_frozen_writer_literal` (`tests/test_upgrade_fixtures.py`) can pin it the
#: same way it pins every other writer's version of this constant.
_ACCEPT_VERSIONED_ONTOLOGY_CHANGE_COVERED: list[str] = []

#: `fingerprint_ontology(<this writer's own examples.tickets registry>).digest`
#: -- read back from `store.read_ontology_fingerprint()` immediately after
#: `ObjectStore.__init__` stamps it, then hand-copied here. Unlike
#: `write_v0_8_0.py` (which cannot import a LATER SDK's registry from its own
#: older venv), this writer's own registry already IS the one that will read
#: the fixture back, so `as_written` and `expected_after_upgrade` share this
#: one value -- see `_ONTOLOGY_FINGERPRINT_EXPECTED_AFTER_UPGRADE` below.
_ACCEPT_VERSIONED_ONTOLOGY_CHANGE_CURRENT_DIGEST = (
    "c27e85c6385d794098ce730594a7391d2dd6d8403b93e9b25711e5d898240d92"
)

#: The FULL fingerprint object (`digest` + `types` + `versions`)
#: `store.read_ontology_fingerprint()` returns both immediately after this
#: writer's own `ObjectStore.__init__` AND after the CURRENT SDK later
#: re-opens this fixture -- the SAME value both times, because both opens
#: use the SAME registry (this crossing's own `examples.tickets`). Restated
#: here in full, mirroring `write_v0_8_0.py`'s own copy of this constant, so
#: a `tests/test_upgrade_fixtures.py` assertion against
#: `store.read_ontology_fingerprint()` directly has a literal to compare
#: against without reaching into the audit log.
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


def main() -> None:  # noqa: C901 -- a straight-line seed script, not a decision tree
    _check_running_under_v0_9_0()
    _check_ontary_did_not_resolve_from_this_repos_own_src()

    # See `_DeterministicAutoIds`: `OpenEscalation`'s handler mints its new
    # Escalation's primary key via the store's own `uuid.uuid4()` fallback,
    # with no runtime seam this writer can otherwise reach. Installed before
    # anything opens a store, so every auto-minted primary key this run
    # produces is reproducible across regenerations.
    uuid.uuid4 = _DeterministicAutoIds(TAG)  # type: ignore[assignment]

    ontology, _unused_memory_store = build_ontology()
    _check_examples_tickets_is_the_v0_9_0_shape(ontology)

    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-journal", "-wal", "-shm"):
        _STORE_PATH.with_name(_STORE_PATH.name + suffix).unlink(missing_ok=True)

    ids = SequentialIds("v090")
    clock = FixedClock(_INSTANT)
    src = Source(source_system="fixture-writer", source_id=TAG)
    store = ObjectStore(ontology.registry, str(_STORE_PATH))
    print(f"opened a fresh store at {_STORE_PATH}")

    # -- seed data: mirrors `write_v0_8_0.py`'s shape (one Org, two Queues,
    #    two Agents, three Tickets, one Comment) -- deliberately NOT
    #    `fixtures.load_fixtures`'s larger synthetic set, and NOT via
    #    `uuid.uuid4()`: `ontary.testing.SequentialIds` keeps every id
    #    deterministic at the source. Every Ticket carries `channel="email"`
    #    directly (v0.9.0's `Ticket` is already at version 2 -- there is no
    #    v1 row here for an upcaster to apply), which also keeps this
    #    fixture's post-upgrade Tickets consistent with v0.7.0/v0.8.0's own
    #    upcast-produced `channel="email"` value.
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
            "channel": "email",
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
            "channel": "email",
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
            "channel": "email",
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

    # -- every 0.9.0 business verb, at least once, so the fixture records a
    #    full-surface 0.9.0 store: `EscalateTicket` (the one v0.8.0-era verb,
    #    with its inline-dispatched effect), then the T1 `Escalation`
    #    lifecycle -- `OpenEscalation` -> `ResolveTicket` -> `ArchiveTicket`
    #    -- on a SECOND ticket, so escalation state is exercised end to end
    #    without disturbing ticket_1's own `EscalateTicket` evidence.
    #    `id_factory=ids` continues the SAME sequential counter used above,
    #    so invocation/effect ids stay deterministic too.
    dispatched = capture_effects()
    runtime = ontology.bind(
        store, clock=clock, id_factory=ids, effects={NOTIFY_TICKET_ESCALATION: dispatched}
    )
    agent_a = Consumer(
        actor_id=agent_1_id, role="Agent", scope_level="queue", scope_id=queue_a_id, kind="human"
    )
    client_a = runtime.for_consumer(agent_a)
    manager_a = Consumer(
        actor_id=agent_1_id,
        role="Manager",
        scope_level="queue",
        scope_id=queue_a_id,
        kind="human",
    )
    client_manager_a = runtime.for_consumer(manager_a)

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

    open_result = client_a.execute(
        "OpenEscalation",
        {
            "ticket_id": ticket_2_id,
            "agent_id": agent_1_id,
            "reason": "Repeated billing complaints",
        },
    )
    escalation_id = open_result["escalation_id"]
    print(f"executed OpenEscalation on ticket_2 -> escalation {escalation_id}")

    resolve_result = client_a.execute("ResolveTicket", {"escalation_id": escalation_id})
    _require(
        resolve_result == {"escalation_id": escalation_id},
        f"unexpected ResolveTicket result: {resolve_result!r}",
    )
    print(f"executed ResolveTicket on escalation {escalation_id}")

    archive_result = client_manager_a.execute(
        "ArchiveTicket", {"escalation_id": escalation_id}
    )
    _require(
        archive_result == {"escalation_id": escalation_id},
        f"unexpected ArchiveTicket result: {archive_result!r}",
    )
    print(f"executed ArchiveTicket on escalation {escalation_id} (Manager role)")

    queue_a_stats = client_a.call_function("ticketStats", {"queue_id": queue_a_id})
    print(f"computed ticketStats(queue_a) = {queue_a_stats}")

    # queue_b has exactly one Ticket -- below this ontology's `min_n=2`, so
    # this call refuses rather than releasing a mean over a single
    # contributor, exercising the app's own min-N guard (mirrored, for the
    # current shape, at `tests/test_examples_smoke.py`'s
    # `test_ticket_aggregate_enforces_min_n`).
    agent_b = Consumer(
        actor_id=agent_2_id, role="Agent", scope_level="queue", scope_id=queue_b_id, kind="human"
    )
    client_b = runtime.for_consumer(agent_b)
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
            "write_v0_9_0.py: expected queue_b's ticketStats call to refuse "
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
    fingerprint_dump = fingerprint.model_dump(mode="json")
    _require(
        fingerprint_dump == _ONTOLOGY_FINGERPRINT_EXPECTED_AFTER_UPGRADE,
        "the stamped fingerprint does not match this file's own "
        "_ONTOLOGY_FINGERPRINT_EXPECTED_AFTER_UPGRADE literal -- if this "
        "writer's business logic changed, regenerate that literal from this "
        "run's own printed fingerprint (see the printout below) before "
        "committing",
    )
    _require(
        fingerprint_dump["digest"] == _ACCEPT_VERSIONED_ONTOLOGY_CHANGE_CURRENT_DIGEST,
        "the stamped fingerprint's digest does not match this file's own "
        "_ACCEPT_VERSIONED_ONTOLOGY_CHANGE_CURRENT_DIGEST literal",
    )
    print(f"fingerprint digest = {fingerprint_dump['digest']}")

    tickets_as_written = {
        ticket_id: _stored_payload(store, "Ticket", ticket_id)
        for ticket_id in (ticket_1_id, ticket_2_id, ticket_3_id)
    }
    # Already at the current shape (v0.9.0's own Ticket is version 2 with
    # `channel`) -- no upcaster transform to apply, so `as_written` and
    # `expected_after_upgrade` are the SAME dict, unlike v0.7.0/v0.8.0.
    tickets_expected_after_upgrade = tickets_as_written
    audit_as_written = [entry.model_dump(mode="json") for entry in store.audit_entries()]
    # No fingerprint drift for check_ontology_fingerprint to classify (this
    # writer's registry IS the reading registry) -- `expected_after_upgrade`
    # is the SAME array, with no `AcceptVersionedOntologyChange` entry
    # appended, unlike v0.7.0/v0.8.0.
    audit_expected_after_upgrade = audit_as_written

    expected: dict[str, Any] = {
        "metadata": {
            "tag": TAG,
            "ontary_version": ontary.__version__,
            # `Z`, matching how pydantic serializes every other UTC instant in
            # this same file under `mode="json"` -- `_INSTANT` is always
            # exactly UTC, so this replacement is exact, never a lossy
            # approximation.
            "generated_at": _INSTANT.isoformat().replace("+00:00", "Z"),
            "schema_version": _read_schema_version(_STORE_PATH),
            "ontology_fingerprint": {
                "note": (
                    "Unlike v0.7.0/v0.8.0, `as_written` and "
                    "`expected_after_upgrade` are the SAME value here: this "
                    "writer's own `examples.tickets` registry IS the "
                    "registry that reads this fixture back (at least until "
                    "the NEXT crossing), so there is no fingerprint drift "
                    "for `check_ontology_fingerprint` (`ontary.store."
                    "protocol`) to classify -- `stored.digest == current."
                    "digest` returns early. This fixture becomes the OLDER "
                    "one the next crossing's writer upcasts, the same way "
                    "this crossing's writer upcasts v0.7.0/v0.8.0 today."
                ),
                "as_written": fingerprint_dump,
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
                "`as_written` IS `expected_after_upgrade` here: v0.9.0's own "
                "`Ticket` is already declared version 2 with `channel`, so "
                "this writer stores every Ticket at that shape directly -- "
                "there is no v1 row for the upcaster to transform, unlike "
                "v0.7.0/v0.8.0 (which store real v1 rows written by an "
                "OLDER registry). Every Ticket carries `channel: \"email\"`, "
                "matching what v0.7.0/v0.8.0's own upcast produces, so a "
                "reader comparing all three fixtures' post-upgrade Tickets "
                "sees the same value regardless of which crossing wrote it."
            ),
            "as_written": tickets_as_written,
            "expected_after_upgrade": tickets_expected_after_upgrade,
        },
        "aggregates": {
            "ticketStats": {
                "note": (
                    "This writer's seed puts two Tickets in queue_a and one "
                    "in queue_b, against this ontology's `min_n=2`. `values` "
                    "holds every queue whose call returned a mean; "
                    "`refused` holds every queue whose call raised "
                    "`VisibilityError` instead, keyed to the error `code` "
                    "it raised (never a value -- there isn't one)."
                ),
                "values": {queue_a_id: queue_a_stats},
                "refused": {queue_b_id: "MIN_N_VIOLATION"},
            },
        },
        "audit": {
            "note": (
                "`as_written` IS `expected_after_upgrade` here (five "
                "entries: EscalateTicket's two -- `ok` then "
                "`effects_dispatched`, its one declared effect dispatching "
                "inline -- plus one entry each for OpenEscalation, "
                "ResolveTicket, and ArchiveTicket, none of which declare "
                "effects). Unlike v0.7.0/v0.8.0, no "
                "`AcceptVersionedOntologyChange` entry is appended when the "
                "current SDK reopens this fixture: this writer's own "
                "registry already IS that SDK's registry, so "
                "`check_ontology_fingerprint` finds no drift to classify."
            ),
            "as_written": audit_as_written,
            "expected_after_upgrade": audit_expected_after_upgrade,
        },
        "outbox": [record.model_dump(mode="json") for record in store.outbox_entries()],
    }
    _EXPECTED_PATH.write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n")
    print(f"wrote {_STORE_PATH}")
    print(f"wrote {_EXPECTED_PATH}")


if __name__ == "__main__":
    main()
