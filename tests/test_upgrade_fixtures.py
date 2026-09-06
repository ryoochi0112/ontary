"""Offline compatibility gate for the versioned tickets fixtures."""

from __future__ import annotations

import ast
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

import ontary
from examples.tickets.ontology import build_ontology
from ontary import Consumer, ObjectStore, VisibilityError
from ontary.fingerprint import fingerprint_ontology
from ontary.migrate import (
    MigrationFailure,
    MigrationReport,
    migrate_object_type,
    upcast_object_type,
)
from ontary.store import SCHEMA_VERSION
from scripts.dump_store import dump

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "upgrade"
_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_WRITER_VERSION_PATTERN = re.compile(r"^write_v(\d+)_(\d+)_0\.py$")
_COMPLETED_MAJOR_LAST_MINOR: dict[int, int] = {
    0: 10,  # Hand-edited release record: 0.x's last completed minor is v0.10.0.
}
_ACCEPT_VERSIONED_CHANGE_TS = (
    "<real wall-clock time at fixture OPEN, not reproducible -- ignore/normalize>"
)


def _required_fixture_tags() -> tuple[str, ...]:
    match = _VERSION_PATTERN.fullmatch(ontary.__version__)
    assert match is not None, (
        "the fixture ladder requires a release version, got "
        f"ontary.__version__={ontary.__version__!r}"
    )
    major, minor, _patch = (int(part) for part in match.groups())
    writer_versions = {
        (int(writer_match.group(1)), int(writer_match.group(2)))
        for path in (FIXTURE_ROOT / "writers").iterdir()
        if path.is_file()
        and (writer_match := _WRITER_VERSION_PATTERN.fullmatch(path.name)) is not None
    }

    required: list[str] = []
    for required_major in range(major + 1):
        first_minor = 7 if required_major == 0 else 0
        literal_last_minor = _COMPLETED_MAJOR_LAST_MINOR.get(
            required_major, first_minor
        )
        writer_last_minor = max(
            (
                writer_minor
                for writer_major, writer_minor in writer_versions
                if writer_major == required_major
            ),
            default=first_minor,
        )
        current_last_minor = minor if required_major == major else first_minor
        last_minor = max(
            first_minor,
            literal_last_minor,
            writer_last_minor,
            current_last_minor,
        )
        required.extend(
            f"v{required_major}.{required_minor}.0"
            for required_minor in range(first_minor, last_minor + 1)
        )
    return tuple(required)


REQUIRED_FIXTURE_TAGS = _required_fixture_tags()


def _read_expected(path: Path) -> dict[str, Any]:
    expected: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    assert set(expected) == {"aggregates", "audit", "ids", "metadata", "outbox", "tickets"}

    metadata = expected["metadata"]
    assert set(metadata) == {
        "generated_at",
        "ontology_fingerprint",
        "ontary_version",
        "schema_version",
        "tag",
    }
    fingerprint = metadata["ontology_fingerprint"]
    assert set(fingerprint) == {"note", "as_written", "expected_after_upgrade"}
    for state in ("as_written", "expected_after_upgrade"):
        assert set(fingerprint[state]) == {"digest", "types", "versions"}

    assert set(expected["ids"]) == {
        "agent_1_id",
        "agent_2_id",
        "comment_id",
        "org_id",
        "queue_a_id",
        "queue_b_id",
        "ticket_1_id",
        "ticket_2_id",
        "ticket_3_id",
    }
    assert set(expected["tickets"]) == {"note", "as_written", "expected_after_upgrade"}
    assert set(expected["aggregates"]) == {"ticketStats"}
    assert set(expected["aggregates"]["ticketStats"]) == {
        "note",
        "refused",
        "values",
    }
    assert expected["aggregates"]["ticketStats"]["values"], (
        "ticketStats values must pin at least one successful queue"
    )
    assert expected["aggregates"]["ticketStats"]["refused"], (
        "ticketStats refusals must pin at least one under-minimum queue"
    )
    assert set(expected["tickets"]["expected_after_upgrade"]) == set(
        expected["tickets"]["as_written"]
    ), "upgraded ticket expectations must cover every as-written ticket"
    assert set(expected["audit"]) == {"note", "as_written", "expected_after_upgrade"}
    assert isinstance(expected["outbox"], list)
    return expected


def _fingerprint_from_dump(store_dump: dict[str, Any]) -> dict[str, Any]:
    rows = store_dump["tables"]["ontology_fingerprint"]
    assert len(rows) == 1, "fixture must contain exactly one ontology fingerprint row"
    row = rows[0]
    return {
        "digest": row["digest"],
        "types": json.loads(row["types"]),
        "versions": json.loads(row["versions"]),
    }


def _current_ticket_payloads(
    store_dump: dict[str, Any],
    *,
    upcast_expected: bool,
    as_written_type_version: int,
) -> dict[str, dict[str, Any]]:
    """`upcast_expected` and `as_written_type_version` are both derived by the
    caller from the SAME fixture's own `expected.json`
    (`tickets.as_written != tickets.expected_after_upgrade`, and
    `metadata.ontology_fingerprint.as_written.versions["Ticket"]`), never
    from the tag string -- a widened rule (S2), not a per-tag branch.
    v0.7.0/v0.8.0 were written by an OLDER app whose `Ticket` was version 1
    (no `channel`), so opening them under the CURRENT registry exercises the
    upcaster chain. A same-version fixture (e.g. v0.9.0, written by the app
    it will be read back by) is already at the current `Ticket` shape when
    written -- there is no upcast left to exercise, and asserting one would
    be false. The stored `type_version` is pinned for EVERY fixture, upcast-
    expected or not: a same-version fixture has no upcast to exercise, but
    its rows must still be stamped with the version its own writer declared,
    and nothing else in this suite pins that once there is no upcast to
    prove happened."""
    payloads: dict[str, dict[str, Any]] = {}
    for row in store_dump["tables"]["objects"]:
        if row["object_type"] != "Ticket" or row["valid_to"] is not None:
            continue
        payload: dict[str, Any] = json.loads(row["payload"])
        assert row["type_version"] == as_written_type_version, (
            f"frozen fixture must contain v{as_written_type_version} Ticket rows, "
            f"got type_version={row['type_version']} for ticket id={payload.get('id')!r}"
        )
        if upcast_expected:
            assert "channel" not in payload, "v1 Ticket rows must require the current upcaster"
        payloads[payload["id"]] = payload
    return payloads


def _frozen_writer_literal(path: Path, name: str) -> Any:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in statement.targets
        ):
            return ast.literal_eval(statement.value)
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == name
        ):
            return ast.literal_eval(statement.value)
    raise AssertionError(f"frozen writer {path} does not declare {name}")


def _normalized_audit(
    store: ObjectStore, *, as_written_count: int, migration_expected: bool
) -> list[dict[str, Any]]:
    """`as_written_count`/`migration_expected` are both read from the SAME
    fixture's own `expected.json` by the caller (never hardcoded, never
    tag-branched -- S2): a fixture whose `audit.as_written !=
    audit.expected_after_upgrade` was written under an OLDER registry, so
    opening it under the current one must append exactly one
    `AcceptVersionedOntologyChange` entry (`check_ontology_fingerprint`,
    `ontary.store.protocol`, `stored.digest == current.digest` returns early
    with nothing appended otherwise). A same-version fixture (its own writer
    IS the reading registry, e.g. v0.9.0 read back immediately after this
    task lands) has no fingerprint drift to accept, so nothing is appended."""
    entries = [entry.model_dump(mode="json") for entry in store.audit_entries()]
    expected_count = as_written_count + (1 if migration_expected else 0)
    assert len(entries) == expected_count, (
        f"expected {as_written_count} as-written entries "
        f"{'plus one accepted migration entry' if migration_expected else 'unchanged'}, "
        f"got {len(entries)}"
    )
    if migration_expected:
        assert entries[-1]["action"] == "AcceptVersionedOntologyChange"
        entries[-1]["ts"] = _ACCEPT_VERSIONED_CHANGE_TS
    return entries


def test_every_minor_has_a_committed_upgrade_fixture() -> None:
    """Every required minor from v0.7 onward is carried forward."""
    version_match = _VERSION_PATTERN.fullmatch(ontary.__version__)
    assert version_match is not None
    current_major = int(version_match.group(1))
    for completed_major in range(current_major):
        assert completed_major in _COMPLETED_MAJOR_LAST_MINOR, (
            "completed-major fixture ceiling needs a hand-edited release record: "
            f"major {completed_major}"
        )
    for completed_major, literal_last_minor in _COMPLETED_MAJOR_LAST_MINOR.items():
        ceiling_tag = f"v{completed_major}.{literal_last_minor}.0"
        ceiling_writer = (
            FIXTURE_ROOT
            / "writers"
            / f"write_v{completed_major}_{literal_last_minor}_0.py"
        )
        assert ceiling_writer.is_file(), (
            f"completed-major fixture ceiling {ceiling_tag} has no frozen writer evidence: "
            f"{ceiling_writer}"
        )
        writer_minors = {
            int(writer_match.group(2))
            for path in (FIXTURE_ROOT / "writers").iterdir()
            if path.is_file()
            and (writer_match := _WRITER_VERSION_PATTERN.fullmatch(path.name)) is not None
            and int(writer_match.group(1)) == completed_major
        }
        assert not writer_minors or max(writer_minors) <= literal_last_minor, (
            f"completed-major fixture ceiling for {completed_major}.x is stale: "
            f"recorded v{completed_major}.{literal_last_minor}.0 but frozen writer "
            f"evidence reaches v{completed_major}.{max(writer_minors)}.0"
        )
    for tag in REQUIRED_FIXTURE_TAGS:
        fixture_dir = FIXTURE_ROOT / tag
        assert fixture_dir.is_dir(), f"required upgrade fixture directory is missing: {fixture_dir}"
        assert (fixture_dir / "tickets.sqlite").is_file(), (
            f"required upgrade store is missing: {fixture_dir / 'tickets.sqlite'}"
        )
        assert (fixture_dir / "expected.json").is_file(), (
            f"required expected state is missing: {fixture_dir / 'expected.json'}"
        )


@pytest.mark.parametrize("tag", REQUIRED_FIXTURE_TAGS)
def test_ticket_fixture_opens_silently_and_matches_expected(tag: str, tmp_path: Path) -> None:
    fixture_dir = FIXTURE_ROOT / tag
    expected = _read_expected(fixture_dir / "expected.json")
    metadata = expected["metadata"]
    assert metadata["tag"] == tag
    assert metadata["ontary_version"] == tag.removeprefix("v")

    source_path = fixture_dir / "tickets.sqlite"
    copied_path = tmp_path / "tickets.sqlite"
    shutil.copyfile(source_path, copied_path)

    # Both derived from THIS fixture's own expected.json (S2: a widened rule,
    # never a per-tag branch) -- true for v0.7.0/v0.8.0 (written by an older
    # registry, so opening under the current one upcasts/migrates), false for
    # a same-version fixture like v0.9.0 (its own writer IS the registry
    # reading it back, so there is nothing to upcast or migrate).
    ticket_upcast_expected = (
        expected["tickets"]["as_written"] != expected["tickets"]["expected_after_upgrade"]
    )
    migration_expected = expected["audit"]["as_written"] != expected["audit"]["expected_after_upgrade"]

    as_written_dump = dump(copied_path)
    assert as_written_dump["schema_version"] == metadata["schema_version"]
    expected_fingerprint = metadata["ontology_fingerprint"]
    assert _fingerprint_from_dump(as_written_dump) == expected_fingerprint["as_written"]
    assert (
        _current_ticket_payloads(
            as_written_dump,
            upcast_expected=ticket_upcast_expected,
            as_written_type_version=expected_fingerprint["as_written"]["versions"]["Ticket"],
        )
        == expected["tickets"]["as_written"]
    )

    ontology, _unused_memory_store = build_ontology()
    live_fingerprint = fingerprint_ontology(ontology.registry).model_dump(mode="json")
    assert expected_fingerprint["expected_after_upgrade"] == live_fingerprint

    # No accept_ontology_drift escape hatch: construction itself must classify
    # the old fingerprint as wholly version-covered and open silently.
    store = ObjectStore(ontology.registry, str(copied_path))
    upgraded_dump = dump(copied_path)
    assert upgraded_dump["schema_version"] == SCHEMA_VERSION
    assert upgraded_dump["schema_version"] == metadata["schema_version"]
    stored_fingerprint = store.read_ontology_fingerprint()
    assert stored_fingerprint is not None
    assert stored_fingerprint.model_dump(mode="json") == live_fingerprint

    expected_tickets = expected["tickets"]["expected_after_upgrade"]
    ticket_rows = store.read_all("Ticket")
    actual_tickets = {row.payload["id"]: row.payload for row in ticket_rows}
    assert actual_tickets == expected_tickets
    assert all(payload["channel"] == "email" for payload in actual_tickets.values())

    id_markers = {
        "org_id": ("Org", "name", "Acme Support"),
        "queue_a_id": ("Queue", "name", "Billing"),
        "queue_b_id": ("Queue", "name", "Onboarding"),
        "agent_1_id": ("Agent", "display_name", "Ada"),
        "agent_2_id": ("Agent", "display_name", "Bo"),
        "ticket_1_id": ("Ticket", "subject", "Invoice mismatch"),
        "ticket_2_id": ("Ticket", "subject", "Refund request"),
        "ticket_3_id": ("Ticket", "subject", "Welcome call"),
        "comment_id": ("Comment", "text", "Looking into this now."),
    }
    actual_ids: dict[str, str] = {}
    for id_name, (object_type, field, value) in id_markers.items():
        matching_rows = [
            row
            for row in store.read_all(object_type)
            if row.payload.get(field) == value
        ]
        assert len(matching_rows) == 1, (
            f"fixture must contain exactly one {object_type} with {field}={value!r}"
        )
        actual_ids[id_name] = str(matching_rows[0].payload["id"])
    assert expected["ids"] == actual_ids

    stats = expected["aggregates"]["ticketStats"]
    queue_to_actor = {
        expected["ids"]["queue_a_id"]: expected["ids"]["agent_1_id"],
        expected["ids"]["queue_b_id"]: expected["ids"]["agent_2_id"],
    }
    store_queue_ids = {row.payload["queue_id"] for row in ticket_rows}
    assert set(stats["values"]) | set(stats["refused"]) == store_queue_ids
    runtime = ontology.bind(store)
    for queue_id, value in stats["values"].items():
        client = runtime.for_consumer(
            Consumer(
                actor_id=queue_to_actor[queue_id],
                role="Agent",
                scope_level="queue",
                scope_id=queue_id,
                kind="human",
            )
        )
        assert client.call_function("ticketStats", {"queue_id": queue_id}) == value
    for queue_id, error_code in stats["refused"].items():
        selected = [
            row
            for row in store.read_all("Ticket")
            if row.payload["queue_id"] == queue_id
        ]
        assert len(selected) == 1, "the min-N refusal must not pass on an empty selection"
        client = runtime.for_consumer(
            Consumer(
                actor_id=queue_to_actor[queue_id],
                role="Agent",
                scope_level="queue",
                scope_id=queue_id,
                kind="human",
            )
        )
        with pytest.raises(VisibilityError) as raised:
            client.call_function("ticketStats", {"queue_id": queue_id})
        assert raised.value.code == error_code

    as_written_audit_count = len(expected["audit"]["as_written"])
    actual_audit = _normalized_audit(
        store, as_written_count=as_written_audit_count, migration_expected=migration_expected
    )
    assert actual_audit[:as_written_audit_count] == expected["audit"]["as_written"]
    if migration_expected:
        accept_entry = actual_audit[-1]
        assert accept_entry["params"]["current_digest"] == live_fingerprint["digest"]
        live_covered = accept_entry["params"]["covered"]
        assert (
            live_covered
            == expected["audit"]["expected_after_upgrade"][-1]["params"]["covered"]
        )
    else:
        live_covered = []
    assert actual_audit == expected["audit"]["expected_after_upgrade"]

    writer_path = FIXTURE_ROOT / "writers" / f"write_{tag.replace('.', '_')}.py"
    assert _frozen_writer_literal(
        writer_path, "_ACCEPT_VERSIONED_ONTOLOGY_CHANGE_CURRENT_DIGEST"
    ) == live_fingerprint["digest"]
    assert _frozen_writer_literal(
        writer_path, "_ACCEPT_VERSIONED_ONTOLOGY_CHANGE_COVERED"
    ) == live_covered
    assert _frozen_writer_literal(
        writer_path, "_ONTOLOGY_FINGERPRINT_EXPECTED_AFTER_UPGRADE"
    ) == live_fingerprint
    assert [record.model_dump(mode="json") for record in store.outbox_entries()] == expected[
        "outbox"
    ]

    upcast_report = upcast_object_type(
        store, ontology.registry, "Ticket", dry_run=True
    )
    assert isinstance(upcast_report, MigrationReport)
    assert upcast_report.scanned == 3
    assert upcast_report.changed == 3
    assert upcast_report.failures == []

    invalid_report = migrate_object_type(
        store,
        ontology.registry,
        "Ticket",
        lambda payload: {**payload, "channel": "fax"},
        dry_run=True,
    )
    assert isinstance(invalid_report, MigrationReport)
    assert invalid_report.scanned == 3
    assert invalid_report.changed == 0
    assert len(invalid_report.failures) == 3
    assert all(isinstance(failure, MigrationFailure) for failure in invalid_report.failures)

    # Once open #1 records the classified versioned change, later opens see
    # the current fingerprint and must not append duplicate audit evidence.
    reopened_twice = ObjectStore(ontology.registry, str(copied_path))
    reopened_thrice = ObjectStore(ontology.registry, str(copied_path))
    assert (
        _normalized_audit(
            reopened_twice,
            as_written_count=as_written_audit_count,
            migration_expected=migration_expected,
        )
        == expected["audit"]["expected_after_upgrade"]
    )
    assert (
        _normalized_audit(
            reopened_thrice,
            as_written_count=as_written_audit_count,
            migration_expected=migration_expected,
        )
        == expected["audit"]["expected_after_upgrade"]
    )
