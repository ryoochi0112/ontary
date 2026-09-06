"""Pins README/docs navigation and the documented SDK surface against code.

The compact T12 README links each reader-facing docs page, carries one
executable quickstart, and deliberately leaves detailed contracts to their
canonical pages. The cookbook's five recipes are executable too. API-reference
tables, declarations, error codes, and the curated `ontary.__all__` remain
independently pinned.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import sys
from pathlib import Path
from typing import get_args, get_origin, get_type_hints

import pytest
from pydantic import BaseModel, ValidationError

import ontary
import ontary.connect
import ontary.diagnose as diagnose_module
from ontary.declarations import Declarations
from ontary.effects import EffectMeta, EffectPayload
from ontary.errors import ERROR_CODES
from tests.test_upgrade_fixtures import (
    _COMPLETED_MAJOR_LAST_MINOR,
    REQUIRED_FIXTURE_TAGS,
)

README = Path(__file__).resolve().parent.parent / "README.md"
CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
_DOCS = Path(__file__).resolve().parent.parent / "docs"
RELEASING = _DOCS / "releasing.md"
API_REFERENCES = (_DOCS / "api-reference.md", _DOCS / "api-reference.ja.md")
# The evidence CENSUS for the PostgresStore envelope derivation (E5e, S2
# closure of coverage clause c'): the CI run ids `docs/storage.md` is
# permitted to publish `#### Run <id>` sections for. This is an EQUALITY
# pin, not a subset check -- a run removed from the doc (heading + table
# together) reds by going missing from the parse below, and a run added
# to the doc without a matching edit here reds as an unvouched-for extra,
# so the ceiling can never silently re-derive over fewer (or more)
# samples than a human actually reviewed. Adding a fourth CI run
# legitimately means adding its id here in the SAME commit as the doc
# edit.
PUBLISHED_POSTGRES_RUN_IDS = frozenset(
    {33623316425, 33630090428, 33651633373, 33697469943}
)
# T3 (envelope-backlog, clause c''): the wider evidence CENSUS -- every CI run of
# the `postgres` job's storage-envelope curve step that has actually been READ,
# whether or not it went on to earn a `#### Run <id>` table above (a run earns one
# only if it SETS or LOWERS the floor; T3's own two new runs did neither). This is
# an EQUALITY pin against `docs/storage.md`'s observed-log table (see
# `test_storage_envelope_observed_run_log_matches_pinned_constant` below), and
# PUBLISHED_POSTGRES_RUN_IDS is required to be a SUBSET of this set: a run cannot
# earn a table without first being recorded as observed. Before this pin, the
# published set was silently SELECTED from a larger, invisible pool -- two CI runs
# (33704034596, 33705109711) were read and discarded from the doc entirely, with no
# trace in `docs/`, `src/`, or `tests/` that they had ever run. This closes that
# silent SELECTION, not silent OMISSION: a run nobody records here at all -- never
# read, or read and never written down -- stays invisible to every guard, because
# the offline suite cannot reach GitHub to discover CI history on its own.
OBSERVED_POSTGRES_RUN_IDS = frozenset(
    {
        33623316425,
        33630090428,
        33651633373,
        33697469943,
        33704034596,
        33705109711,
    }
)
EXPECTED_PRE_080_ERROR_CODES = frozenset(
    {
        "AFTER_WITHOUT_LIMIT",
        "AUTHORITY_ERROR",
        "CALLER_TRANSACTION_REFUSED",
        "CAPABILITY_NOT_PROVIDED",
        "CARDINALITY_VIOLATION",
        "CONSUMER_UNRESOLVED",
        "EFFECT_NOT_DISPATCHABLE",
        "EFFECT_NOT_SERIALIZABLE",
        "ENTITY_KEY_MISMATCH",
        "FUNCTION_ERROR",
        "INTERNAL_ERROR",
        "INVALID_BATCH",
        "INVALID_CURSOR",
        "INVALID_GROUP_BY",
        "INVALID_LIMIT",
        "INVALID_PARAMS",
        "INVALID_RECORD",
        "MIN_N_VIOLATION",
        "MISSING_MAPPED_FIELD",
        "NON_NUMERIC_AGGREGATE",
        "OBJECT_NOT_FOUND",
        "ONTOLOGY_DRIFT",
        "ONTOLOGY_INVALID",
        "OWNED_PROPERTY_REFUSED",
        "OWNED_TYPE_REFUSED",
        "PERMISSION_DENIED",
        "PRECONDITION_FAILED",
        "SCOPE_DENIED",
        "SCOPE_POLICY_ERROR",
        "SOURCE_CREATE_REFUSED",
        "STORE_BUSY",
        "STORE_ERROR",
        "STORE_SCHEMA_INCOMPATIBLE",
        "STORE_VERSION_UNSUPPORTED",
        "UNKNOWN_ACTION",
        "UNKNOWN_FIELD",
        "UNKNOWN_LINK_TYPE",
        "UNKNOWN_NAME",
        "UNKNOWN_OBJECT_TYPE",
        "UNAUTHENTICATED",
        "UNDECLARED_CAPABILITY",
        "UNDECLARED_EFFECT",
        "UNDECLARED_SOURCE_WRITE",
        "UPCAST_FAILED",
        "VISIBILITY_DENIED",
    }
)
EXPECTED_080_NEW_ERROR_CODES = frozenset(
    {
        "OBJECT_ERASURE_NOT_FOUND",
        "OBJECT_RETIRE_NOT_FOUND",
        "OBJECT_ALREADY_ERASED",
        "OBJECT_ALREADY_RETIRED",
        "LINK_NOT_FOUND",
        "STALE_CURSOR",
        "UNDECLARED_SOURCE_REMOVAL",
        "UNKNOWN_OPERATOR",
        "OPERATOR_TYPE_MISMATCH",
        "PAGE_NOT_ITERABLE",
        "GROUP_KEY_COLLISION",
    }
)
NEW_ENGLISH_DOCS = tuple(
    _DOCS / name
    for name in (
        "storage.md",
        "connectors.md",
        "mcp-serving.md",
        "effects.md",
        "queries.md",
        "authority.md",
        "cookbook.md",
        "v1-gate.md",
    )
)
# The release runbook is an operational document, not a reader-facing API page;
# keep it out of the README reachability census because its dedicated guard below
# checks its presence and release-specific contract directly.
OPERATIONAL_DOCS = frozenset({_DOCS / "releasing.md"})
READER_DOCS = tuple(
    sorted(
        path
        for path in _DOCS.rglob("*")
        if path.is_file()
        and path.suffix in {".html", ".md"}
        and path not in OPERATIONAL_DOCS
    )
)
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "verify.yml"
HONESTY_JOB = "upgrade-fixture-honesty"


def test_queries_document_visible_row_count_disclosure_reasoning() -> None:
    text = (_DOCS / "queries.md").read_text()

    assert "post-visibility" in text
    assert "can already enumerate the same rows" in text
    assert "deliberately not min-N-gated" in text
    assert "scoped away from every matching row receives `0`" in text
    assert "`count_contributors` remains the sole privacy-counting primitive" in text


def test_release_runbook_and_compatibility_describe_index_rollback() -> None:
    """The release runbook must make rollback safe, and compatibility must
    describe PyPI as the index.

    These are structural checks rather than a prose snapshot: the rollback
    section must carry the yank/never-delete/never-re-publish policy, while the
    compatibility section must make the positive PyPI claim and must not retain
    the pre-fork private-index claim.
    """
    assert RELEASING.is_file(), f"missing release runbook: {RELEASING}"
    releasing = RELEASING.read_text()
    rollback_match = re.search(
        r"(?ms)^## Rollback\s*\n(?P<body>.*?)(?=^## |\Z)",
        releasing,
    )
    assert rollback_match is not None, "releasing.md is missing its Rollback section"
    rollback = rollback_match.group("body")
    assert re.search(r"(?i)\byank(?:ing)?\b", rollback), (
        "releasing.md's Rollback section must yank the bad PyPI version"
    )
    assert re.search(r"(?i)\b(?:do not|never)\s+delete\b", rollback), (
        "releasing.md's Rollback section must say not to delete the release"
    )
    assert re.search(r"(?i)\b(?:do not|never)\s+re-?publish\b", rollback), (
        "releasing.md's Rollback section must say not to re-publish"
    )
    assert re.search(r"(?i)\bPyPI\b", rollback)
    assert "PEP 592" in rollback
    for stale in ("Artifact Registry", "gcloud", "pkg.dev", "WIF"):
        assert stale not in releasing, (
            f"releasing.md still describes the pre-fork private index: {stale!r}"
        )

    compatibility = (_DOCS / "compatibility.md").read_text()
    versioning = compatibility[
        compatibility.index("## Versioning") : compatibility.index(
            "## What counts as a breaking change"
        )
    ]
    assert re.search(r"(?i)published on \[PyPI\]", versioning), (
        "compatibility.md's Versioning section must describe the PyPI index"
    )
    assert "git+https://github.com/ryoochi0112/ontary@v" in versioning, (
        "compatibility.md's Versioning section must keep the public git-ref fallback"
    )
    assert not re.search(
        r"(?i)\b(?:not|isn't)\s+on an index\b", compatibility
    ), (
        "compatibility.md still claims the SDK is not on an index"
    )
    assert not re.search(r"(?i)private (?:Artifact Registry )?index", compatibility), (
        "compatibility.md still describes the pre-fork private index"
    )


#: The runbook's fenced commands are copy-pasted onto a maintainer's own laptop, so a
#: command word that resolves to nothing there is a shipped defect rather than a typo.
_RELEASING_BASH_FENCE = re.compile(r"(?ms)^```bash[ \t]*\n(?P<body>.*?)^```[ \t]*$")

#: A bare ``python`` command word. The boundaries exclude the three spellings that are
#: safe in this runbook: ``python3``, a path-qualified interpreter such as
#: ``/tmp/ontary-clean-X.Y.Z/bin/python``, and a flag like ``--python 3.12``.
_BARE_PYTHON = re.compile(r"(?<![\w/.-])python(?![\w.])")

#: ``uv run`` is the one accepted way to reach an interpreter: uv is already a hard
#: prerequisite of every block, and it provisions the interpreter itself. The exemption
#: is anchored to the same command -- a separator between ``uv run`` and the match ends
#: it, so ``python -c ... && uv run x`` stays an offender.
_UV_RUN_PREFIX = re.compile(r"\buv run\b[^;&|]*$")


def _strip_shell_comments(line: str) -> str:
    """Drop a shell comment so prose *about* a command cannot stand in for it.

    A guard that filters a whole block by substring is trippable by the block's own
    explanatory comment, which pressures the next editor to delete the explanation
    rather than the defect. Whole-line and trailing comments both go; a ``#`` inside a
    quoted string stays, since it is data rather than a comment.
    """
    kept: list[str] = []
    quote = ""
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            break
        kept.append(char)
    return "".join(kept)


def test_release_runbook_bash_blocks_never_invoke_a_bare_python() -> None:
    """No fenced command in the runbook may depend on a bare ``python``.

    macOS with Homebrew ships ``python3`` and no ``python``, and some Linux images
    ship neither, so ``python -c ...`` dies with ``command not found``. Wrapped in
    ``test "$(...)" = "X.Y.Z"`` it then exits 1 and misreads as a tag/version
    mismatch, which sends the maintainer after the wrong problem. Nothing earlier in
    that block supplies an interpreter either: ``uv sync`` runs on the *next* line,
    so there is no ``.venv`` yet.
    """
    blocks = [
        match.group("body")
        for match in _RELEASING_BASH_FENCE.finditer(RELEASING.read_text())
    ]
    assert blocks, f"no fenced bash block found in {RELEASING}"

    offenders: list[str] = []
    for body in blocks:
        for line in body.splitlines():
            command = _strip_shell_comments(line)
            for match in _BARE_PYTHON.finditer(command):
                if _UV_RUN_PREFIX.search(command[: match.start()]):
                    continue
                offenders.append(line.strip())

    assert not offenders, (
        "docs/releasing.md invokes a bare `python`, which does not exist on macOS "
        "with Homebrew nor on several Linux images. Reach the interpreter through "
        f"`uv run` instead. Offending lines: {offenders}"
    )


MOVED_DOC_NAMES = (
    "MinNViolation",
    "VisibilityDenied",
    "CodedError",
    "get_object",
    "get_objects",
    "assess_drift",
    "resolve_identities",
    "ActionPermissionError",
    "IdentityRule",
)

# `StoreBusy` was removed with the old exception surface; `STORE_BUSY` remains
# the error code. Keep the class name in the same stale-name inventory so the
# reader-facing pages cannot teach the removed name again.
STALE_DOC_NAMES = (*MOVED_DOC_NAMES, "StoreBusy")
BOUND_QUERY_STALE_NAMES = ("get_object", "get_objects")

# The API break deliberately keeps the author-facing names used by the live
# examples at the package root. Engine-only names remain mapped to their
# canonical submodules below; a regression that puts one back on the front
# door is caught by that exact demotion map.
FRONT_DOOR_RUNTIME_NAMES = {
    "ActionError",
    "AuthorityError",
    "ConflictError",
    "CustomResolver",
    "Declarations",
    "DrainReport",
    "EffectDispatcher",
    "EffectMeta",
    "Finding",
    "InMemoryStore",
    "InternalError",
    "OntaryError",
    "ObjectStore",
    "OntologyClient",
    "OutboxRecord",
    "Page",
    "PermissionDenied",
    "PostgresStore",
    "PreconditionFailed",
    "RetryPolicy",
    "ScopePolicy",
    "TypedPage",
    "ValidationFailed",
    "VisibilityError",
    "__version__",
    "build_mcp_server",
    "declarations",
    "ref",
    "scope_ref",
}

# Every name removed from the old 119-name root surface has one canonical
# defining submodule.  Keep this inventory explicit: deleting a module export,
# misspelling a destination, or accidentally restoring a demoted name makes a
# focused assertion fail instead of relying on documentation prose.
DEMOTED_NAMES_BY_MODULE = {
    "ontary.actions": {"ActionExecutor"},
    "ontary.audit": {"CapabilityAccessRecord", "EffectRecord"},
    "ontary.client": {"OntologyRuntime"},
    "ontary.connect": {
        "LinkSkip",
        "MappingValidationError",
        "RunReport",
        "SourceConnector",
        "SourceLineage",
        "map_batch",
        "run_dlt_extract",
        "to_date",
        "to_datetime",
        "to_optional_date",
        "to_optional_datetime",
    },
    "ontary.errors": {"ERROR_CODES", "ErrorCodeInfo", "Kind"},
    "ontary.explain": {
        "DecisionTrace",
        "MinNTrace",
        "RedactionTrace",
        "ScanReport",
        "ScopePathStep",
        "ScopeRuleTrace",
    },
    "ontary.fingerprint": {"OntologyFingerprint", "fingerprint_ontology"},
    "ontary.functions": {"FunctionHandler", "FunctionRegistry"},
    "ontary.ingest": {"IngestError", "IngestReport", "bulk_link", "bulk_upsert"},
    "ontary.mcp_server": {"ConsumerResolver", "build_multi_consumer_mcp_server"},
    "ontary.meta": {
        "ActionParameterDef",
        "ActionTypeDef",
        "FunctionDef",
        "LinkTypeDef",
        "ObjectTypeDef",
        "OntologyRegistry",
        "PropertyDef",
        "PropertyType",
        "ScopeLevel",
        "Upcaster",
    },
    "ontary.migrate": {
        "MigrationFailure",
        "MigrationReport",
        "migrate_object_type",
        "upcast_object_type",
    },
    "ontary.ontology": {"OntologyDef"},
    "ontary.outbox": {
        "DEFAULT_RETRY_POLICY",
        "OutboxState",
    },
    "ontary.query": {"GuardedQuery"},
    "ontary.scope": {
        "Direction",
        "RowVisibilityFn",
        "ScopeRule",
        "resolve_contributor",
        "resolve_owning_scope",
    },
    "ontary.security": {"ConsumerKind", "covers_scope"},
    "ontary.store": {
        "AuditEntry",
        "DEFAULT_BATCH",
        "DEFAULT_TENANT",
        "Lineage",
        "SCHEMA_VERSION",
        "StoredObject",
        "WriteRecord",
        "accept_ontology_fingerprint",
        "check_ontology_fingerprint",
    },
    "ontary.testing": {
        "FixedClock",
        "SequentialIds",
        "capture_effects",
        "consumer",
        "make_store",
        "raises_code",
    },
    "ontary.upcast": {"upcast_payload"},
}


def _read_readme() -> str:
    return README.read_text()


def test_new_english_pages_are_reachable_and_cross_linked() -> None:
    readme = _read_readme()
    for path in READER_DOCS:
        relative = path.relative_to(README.parent).as_posix()
        assert relative in readme, f"README does not reach {relative}"

    for path in NEW_ENGLISH_DOCS:
        text = path.read_text()
        assert "../README.md" in text, f"{path.name} does not link back to README"
        assert "api-reference.md" in text, f"{path.name} does not link to API reference"


#: The gate checklist is `v1.0.0`'s precondition list, so a box's state is a claim about
#: the project rather than formatting. Parsed per decision rather than substring-matched:
#: a bare `- [x]` search would match Decision C's already-checked box and pass while A
#: stayed open.
_V1_GATE_DECISION_BOX = re.compile(
    r"(?m)^- \[(?P<box>[ x])\] \*\*Decision (?P<letter>[A-C]) \u2014 (?P<topic>[^:*]+):\*\*"
)


def test_v1_gate_decision_a_is_resolved_and_recorded() -> None:
    """Decision A is resolved, and the box and the durable record agree.

    Resolved 2026-09-06 (human, at the fork of `ontos` into `ontary`): the
    audience is public, served by PyPI through trusted publishing. This
    supersedes the 2026-09-04 pre-fork decision (internal, private index). A
    checked box with no section recording *what* was decided is not a durable
    project record, and a section with an unchecked box understates the gate --
    so both are pinned, together with the supersession, so the doc cannot
    contradict itself about its own release preconditions.
    """
    gate = (_DOCS / "v1-gate.md").read_text()

    boxes = {
        match.group("letter"): match.group("box")
        for match in _V1_GATE_DECISION_BOX.finditer(gate)
    }
    assert set(boxes) == {"A", "B", "C"}, (
        f"v1-gate.md's decision checklist no longer parses as A/B/C: {boxes}"
    )
    assert boxes["A"] == "x", (
        "docs/v1-gate.md's Decision A box must be checked -- the audience was "
        "resolved 2026-09-06 (public, PyPI)"
    )

    section = re.search(
        r"(?ms)^## Decision A \u2014 audience\s*\n(?P<body>.*?)(?=^## |\Z)", gate
    )
    assert section is not None, (
        "v1-gate.md checks Decision A's box but has no `## Decision A \u2014 audience` "
        "section recording the decision"
    )
    body = section.group("body")

    assert "**Resolved.**" in body, (
        "Decision A's section must open **Resolved.**, as Decision C's does"
    )
    assert "https://pypi.org/project/ontary/" in body, (
        "Decision A's section must name the index the audience is served from"
    )
    for pattern, missing in (
        (r"(?i)\bpublic\b", "that the audience is public"),
        (r"(?i)trusted publishing", "how PyPI is reached"),
        (r"(?i)supersedes", "that the pre-fork internal decision is superseded"),
        (r"(?i)git[- ]ref", "that the git-ref install stays documented"),
        (r"(?i)\byank", "that rollback is by yank, not deletion"),
    ):
        assert re.search(pattern, body), (
            f"Decision A's section does not record {missing}"
        )

    assert not re.search(r"(?i)decisions? A[^.]*\bunresolved\b", gate), (
        "v1-gate.md records Decision A as resolved but still says it is unresolved "
        "elsewhere; Decision C's closing paragraph now covers B only"
    )


def test_v1_gate_release_table_matches_upgrade_fixture_ceiling() -> None:
    gate = _DOCS / "v1-gate.md"
    assert gate.is_file(), f"missing v1 gate document: {gate}"
    text = gate.read_text()
    table_match = re.search(
        r"(?ms)^## Release table\n.*?"
        r"\| Major \| Last completed minor \|\n"
        r"\| --- \| --- \|\n"
        r"(?P<rows>(?:\| \d+ \| v\d+\.\d+\.0 \|\n?)+)",
        text,
    )
    assert table_match is not None, "v1-gate.md is missing its release table"

    recorded: dict[int, int] = {}
    for row in table_match.group("rows").splitlines():
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        assert len(cells) == 2, f"malformed v1-gate release-table row: {row!r}"
        major = int(cells[0])
        tag_match = re.fullmatch(r"v(\d+)\.(\d+)\.0", cells[1])
        assert tag_match is not None, f"malformed release tag in row: {row!r}"
        tag_major, last_minor = (int(part) for part in tag_match.groups())
        assert tag_major == major, f"major mismatch in release-table row: {row!r}"
        assert major not in recorded, f"duplicate major in release table: {major}"
        recorded[major] = last_minor

    assert recorded == _COMPLETED_MAJOR_LAST_MINOR


def test_storage_envelope_doc_ceiling_matches_published_constants() -> None:
    """`docs/storage.md`'s stated row ceiling is a STRUCTURAL pin against
    `ontary.diagnose.STORAGE_ENVELOPE`, not a hand-copied number that can
    silently drift from the code a `Finding` message actually quotes.
    Equality, not a subset check, so a backend added to one side and
    forgotten on the other reds either way.

    T2 (envelope-backlog): the ceiling table prints no `seconds_per_row`
    column -- only "~N rows" -- so the equality above left `seconds_per_row`
    completely unbound here: it could move by any amount in
    `STORAGE_ENVELOPE` and this test stayed green, since nothing below ever
    read that field (confirmed by mutation probe, not assumed -- changing
    only `seconds_per_row` left this specific test passing before this
    docstring paragraph and its assertion below existed). It is bound below
    without inventing a doc figure the table never prints: the ceiling row
    is itself a claim -- "a governed read stays under one second below ~N
    rows" -- so this re-derives `documented_rows *
    STORAGE_ENVELOPE[backend].seconds_per_row` and requires it inside the
    same [0.9, 1.0] one-second-crossing band `tests/test_diagnose_lints.py`'s
    `test_storage_envelope_rows_times_seconds_per_row_stays_near_one_second`
    already polices code-internally -- verifying the DOC's own claim against
    the code, rather than hand-copying a second figure the table never
    states. A `seconds_per_row` change large enough to break the "under one
    second" story this table tells now reds HERE too, not only in
    `docs/v1-gate.md`'s prose-pair pin or that code-internal band test.
    """
    storage = _DOCS / "storage.md"
    text = storage.read_text()
    table_match = re.search(
        r"(?m)^\| Backend \| Label \| Under-one-second ceiling \|\n"
        r"\| --- \| --- \| --- \|\n"
        r"(?P<rows>(?:\|.+\|\n?)+)",
        text,
    )
    assert table_match is not None, "storage.md is missing its envelope ceiling table"

    documented: dict[str, tuple[str, int]] = {}
    for row in table_match.group("rows").splitlines():
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        assert len(cells) == 3, f"malformed storage envelope ceiling row: {row!r}"
        backend = cells[0].strip("`")
        ceiling_match = re.fullmatch(r"~([\d,]+) rows", cells[2])
        assert ceiling_match is not None, f"malformed ceiling cell: {row!r}"
        documented[backend] = (cells[1], int(ceiling_match.group(1).replace(",", "")))

    published = {
        backend: (envelope.label, envelope.rows)
        for backend, envelope in diagnose_module.STORAGE_ENVELOPE.items()
    }
    assert documented == published

    for backend, (_, doc_rows) in documented.items():
        envelope = diagnose_module.STORAGE_ENVELOPE[backend]
        product = doc_rows * envelope.seconds_per_row
        assert 0.9 <= product <= 1.0, (
            f"storage.md's ceiling table claims {backend} stays under one "
            f"second below ~{doc_rows:,} rows, but "
            f"STORAGE_ENVELOPE[{backend!r}].seconds_per_row="
            f"{envelope.seconds_per_row} puts {doc_rows:,} * "
            f"seconds_per_row={product:.4f}s outside the [0.9, 1.0] "
            "one-second-crossing band -- the doc's ceiling no longer "
            "matches the published per-row rate"
        )


def _postgres_job_scan_curve_row_counts() -> frozenset[int]:
    """S2 (T4, envelope-backlog): the row-count SET each published run's
    table below is required to contain, EXACTLY -- sourced from the SAME
    `postgres` job in `.github/workflows/verify.yml` that actually invokes
    `scripts/scan_curve.py` in CI, not a second `{1000, 5000, 25000}` literal
    hand-copied into this file. If a human changes the CI step's row counts,
    this function's return value changes with it and the per-run assertion
    below reds until `docs/storage.md` is updated to match.

    Reuses `test_fixture_honesty_matrix_covers_required_fixture_tags`'s
    job-block slice above to isolate the `postgres` job's own text first, so
    a `scan_curve.py` invocation appearing in some OTHER job (there is none
    today) could never be mistaken for this one. Positional row-count
    arguments are whatever `scripts/scan_curve.py`'s own `_parse_args` would
    collect into `args.sizes` (`nargs="*"`, `type=int`, consumed up to the
    first `-`-prefixed flag): every whitespace-separated token on the
    invocation's own shell line -- possibly backslash-continued onto a
    second physical line, as it is today -- starting right after
    `scan_curve.py`.
    """
    text = WORKFLOW.read_text()
    parts = text.split("\njobs:\n", 1)
    assert len(parts) == 2, f"{WORKFLOW} has no top-level `jobs:` block"
    job_matches = list(re.finditer(r"(?m)^  ([a-z][a-z0-9-]*):$", parts[1]))
    job_blocks = {
        match.group(1): parts[1][
            match.end() : (
                job_matches[index + 1].start()
                if index + 1 < len(job_matches)
                else len(parts[1])
            )
        ]
        for index, match in enumerate(job_matches)
    }
    job_block = job_blocks.get("postgres", "")
    assert job_block.strip(), (
        f"{WORKFLOW} job `postgres` block not found or empty -- update this "
        "parser if the workflow shape changed"
    )

    invocation = re.search(r"scan_curve\.py((?:\\\n|[^\n])*)", job_block)
    assert invocation is not None, (
        f"{WORKFLOW} job `postgres` has no `scripts/scan_curve.py` "
        "invocation -- the storage-envelope curve step may have been "
        "renamed, removed, or moved to a different job"
    )
    tail = invocation.group(1).replace("\\\n", " ")
    row_counts: list[int] = []
    for token in tail.split():
        if not re.fullmatch(r"\d+", token):
            break
        row_counts.append(int(token))
    assert row_counts, (
        f"{WORKFLOW}'s `scripts/scan_curve.py` invocation in job `postgres` "
        "has no positional row-count arguments before its first `--` flag"
    )
    assert len(row_counts) == len(set(row_counts)), (
        f"{WORKFLOW}'s `scripts/scan_curve.py` invocation in job `postgres` "
        f"repeats a row count: {row_counts}"
    )
    return frozenset(row_counts)


def test_storage_envelope_postgres_ceiling_is_derived_from_the_published_curves() -> None:
    """S2 closure (E5c): make the PostgresStore derivation EXECUTABLE.

    E5 and E5b each hand-copied a `rows` figure into three places (the code
    constant, the doc's summary ceiling row, and
    `tests/test_diagnose_lints.py`'s exact-value pin) and a human catch on
    the job log was the only thing that ever caught a wrong-but-self-consistent
    triple (E5b's proven escape: `rows=6_400`, twice the then-published
    ceiling, shipped green). This test replaces that with ONE generating
    rule: parse every Postgres run table `docs/storage.md` publishes (each
    `#### Run <id>` section is one CI job's raw evidence), compute the
    worst -- i.e. SLOWEST -- per-row rate across every row and both linear
    read classes (`aggregate`, narrow-scope; the bounded-page column is
    flat and carries no ceiling, per this section's own prose), and assert
    the published `STORAGE_ENVELOPE["PostgresStore"].rows` sits at or below
    the one-second crossing that rate implies. Paste a slower run 3's table
    in and this assertion re-derives itself without touching this file (S2:
    not another hand-copied constant -- `docs/storage.md`, `docs/v1-gate.md`,
    `CHANGELOG.md`, and `tests/test_diagnose_lints.py` each also carry this
    pair, or its `rows` half alone, as hand-written prose today; this assertion
    does not add a further hand-copied site to that list, and does not claim
    any of those four is itself guarded -- verify each independently before
    relying on it).

    E5e (S2 closure of coverage clause c'): the SET of run ids this parse is
    allowed to range over is itself pinned, as `PUBLISHED_POSTGRES_RUN_IDS`
    at module scope -- an equality, not a subset, against the parsed
    `#### Run <id>` headings. Without this, deleting a run's heading and
    table together (reverting to an earlier, since-superseded rate) is
    indistinguishable from that run never having existed: the census
    invariant below stays satisfied (heading and table vanish together) and
    the ceiling silently re-derives over fewer samples. Adding an
    unpinned run is symmetrically a defect -- a sample this constant does
    not vouch for -- so the check is `{parsed ids} == PUBLISHED_POSTGRES_RUN_IDS`,
    not a subset in either direction.

    T4 (envelope-backlog, E5e backlog items 3 and 4): PUBLISHED_POSTGRES_RUN_IDS
    (and the census invariant above) vouch for which RUNS are published, never
    for what ROWS are inside each run's own table -- a row could be deleted,
    duplicated, or added inside an otherwise-pinned table while every check
    above still passed, as long as whatever rows remained still parsed as
    well-formed cells. Two further invariants close that: (1) each run's table
    must contain EXACTLY the row-count SET `.github/workflows/verify.yml`'s
    `postgres` job actually invokes `scripts/scan_curve.py` with -- parsed
    structurally by `_postgres_job_scan_curve_row_counts` above, not a second
    `{1000, 5000, 25000}` literal hand-copied here (S2), so changing the CI
    step's row counts without updating the doc to match reds until it is; (2)
    `len(run_headings) == len(parsed_run_ids)`, so a `#### Run <id>` heading
    duplicated verbatim (same id, same table, copy-pasted a second time) reds
    on the heading-count-vs-distinct-id-count mismatch instead of deduping
    away under the SET equality check above.

    Residual bound, stated explicitly: this closes "which row counts are
    present" in each run's table, never "are the numbers in those rows
    honest" -- a cell REWRITTEN IN PLACE with a wrong-but-well-shaped number
    (the right row count, a fabricated latency) still ships green. The only
    backstop for that remains a human re-reading `gh run view --job <id>
    --log`, same as T3 established for the observed log below.

    L23: no millisecond figure is asserted here -- only a relation between
    numbers PARSED from the doc and the published constant, and nothing is
    measured at test time.

    Non-vacuousness (mutation-probed, per the task's mutation-receipt
    protocol rather than a permanent fixture in this suite): renaming the
    `#### Run` marker so no heading matches, or corrupting a table's header
    row so it matches nowhere in its own window, reds this test with an
    explicit assertion message identifying what is missing -- it does not
    silently collect zero samples. Two further invariants close two more
    ways a run's table can go missing from the parse without tripping that:
    each run's table must be the first non-blank content beneath its own
    heading, so shape-breaking a run's table in place cannot make the parse
    fall through to a well-formed cost table sitting later in that SAME
    run's window; and every cost-table-shaped block anywhere in the
    document must be claimed by exactly one run's own window, in that SAME
    first-non-blank-content position, or be the single such block tolerated
    before the very first `#### Run` heading, so a run's table cannot
    vanish from the parse by demoting its own heading to a different level
    while the table itself stays intact and visible to a human reader, and
    a foreign cost-shaped table pasted in anywhere else in the document
    cannot hide from the census either. Each run's window is bounded at the
    next markdown heading of any level, or at EOF if the run's heading is
    the last one in the document.

    T5 (envelope-backlog, DX hole 4): the invariant above KEYS ON POSITION,
    never on a document-wide COUNT of every `table_header`-shaped match (the
    pre-T5 shape: assert the count equals `len(run_headings) + 1`,
    hand-reserving the "+1" for the `ObjectStore`/SQLite table above the
    runs). That count coupled this Postgres-only guard to a COMPLETELY
    DIFFERENT backend's table staying in one exact shape, at one exact
    position: a benign SQLite-side edit (a reordered column) that changed
    or broke the SQLite table's own match reds a guard whose job is
    auditing Postgres run tables, with no Postgres-side edit at all.

    A first cut at this fix keyed the position check on "is this block the
    first content beneath SOME heading that failed to parse as `#### Run
    <id>`" -- which excludes the `ObjectStore`/SQLite table correctly (it
    is never first content beneath `### Cost by operation and row count`; a
    plain paragraph, `` `ObjectStore` (SQLite): ``, always introduces it
    first) but is STRICTLY NARROWER than the doc-wide count it replaced: a
    stray cost-shaped block that is neither first content beneath any
    heading (e.g. pasted after a caption paragraph, mid-section) nor
    claimed by any run's own window went completely unflagged, wherever it
    sat -- even directly beneath a brand-new heading, if a caption line
    happened to sit between that heading and the pasted table. The
    invariant above closes that by keying on CLAIMS, not on "first content
    beneath a heading" alone: every `table_header` match in the WHOLE
    document must be claimed by exactly one run's own `[heading.end(),
    window_end)` window, in the exact first-non-blank-content position the
    per-run loop below already requires -- or it is the single block
    tolerated before the very first `#### Run` heading, the one place,
    structurally, where a different backend's own table can precede all
    Postgres run evidence. A match that is neither is an orphan, REGARDLESS
    of what (if anything) precedes it. The `ObjectStore`/SQLite table stays
    excluded not because of its own shape, caption, or column order, but
    because it is that (at most one) tolerated block before the first run
    heading.

    T5 (envelope-backlog, DX hole 5): every raw `int(...)` conversion below
    that parses doc text is guarded by an explicit shape check first (a
    regex `fullmatch`), asserting a message that names the run id and the
    expected shape, rather than surfacing Python's own bare, context-free
    exception. The acceptance's own literal framing for this hole -- delete
    a run's heading and leave its table's rows in place, so they flow into
    an adjacent run's window -- is UNREACHABLE for any of the four
    currently published runs: deleting (or demoting) a published run's
    `#### Run <id>` heading trips the T3/E5e set-equality assertion above
    FIRST (`{parsed ids} == PUBLISHED_POSTGRES_RUN_IDS`), long before the
    per-run loop below -- where this fix actually lives -- is ever reached.
    This was instead verified with a REACHABLE substitute, documented here
    rather than silently swapped in: leak another table's own header-shaped
    block (its literal header and separator rows) directly after a run's
    own last data row, with NO heading touched at all. The `rows` capture
    group below is a greedy run of consecutive pipe-delimited lines; it
    does not stop at a run's own last legitimate row, so a leaked header
    row immediately following it is swallowed as if it were one more data
    row of THIS SAME run's own table. That leaves the swallowed table's own
    HEADER row -- not its separator row, which sits one line further down
    and is never reached, since the header row's own malformed cell raises
    first -- parsed as if it were a data row, whose first cell is the
    literal string `'Rows'`. Pre-T5 this raised a bare `ValueError: invalid
    literal for int() with base 10: 'Rows'`, naming no run at all.
    """
    storage = _DOCS / "storage.md"
    text = storage.read_text()

    run_headings = list(re.finditer(r"(?m)^#### Run (?P<run_id>\d+)\b.*$", text))
    assert run_headings, (
        "storage.md has no `#### Run <id>` heading -- no Postgres run table "
        "can be found, so this test would otherwise pass vacuously"
    )

    run_id_list = [int(heading.group("run_id")) for heading in run_headings]
    parsed_run_ids = set(run_id_list)
    # T4 (envelope-backlog, E5e backlog item 4): a `#### Run <id>` heading
    # duplicated verbatim (same id, same table, copy-pasted a second time)
    # dedupes away under the SET equality check below -- parsed_run_ids
    # would still equal PUBLISHED_POSTGRES_RUN_IDS, silently absorbing the
    # duplicate. Catch it here, against the LIST length, before that check
    # ever runs.
    duplicated_run_ids = sorted(rid for rid in parsed_run_ids if run_id_list.count(rid) > 1)
    assert len(run_id_list) == len(parsed_run_ids), (
        f"storage.md has {len(run_id_list)} `#### Run <id>` headings but only "
        f"{len(parsed_run_ids)} distinct run ids -- duplicated heading id(s): "
        f"{duplicated_run_ids}. A repeated run id dedupes away under plain set "
        "equality against PUBLISHED_POSTGRES_RUN_IDS and must fail here instead "
        "of silently passing as if the run had been published exactly once"
    )
    missing_run_ids = PUBLISHED_POSTGRES_RUN_IDS - parsed_run_ids
    extra_run_ids = parsed_run_ids - PUBLISHED_POSTGRES_RUN_IDS
    assert parsed_run_ids == PUBLISHED_POSTGRES_RUN_IDS, (
        "storage.md's `#### Run <id>` headings do not match the pinned "
        f"evidence census PUBLISHED_POSTGRES_RUN_IDS={sorted(PUBLISHED_POSTGRES_RUN_IDS)} -- "
        f"missing run ids (pinned but not found in the doc): {sorted(missing_run_ids)}; "
        f"extra run ids (found in the doc but not pinned): {sorted(extra_run_ids)}. "
        "A run cannot be silently removed (or its id silently changed) without "
        "this failing -- adding a legitimate new run means editing "
        "PUBLISHED_POSTGRES_RUN_IDS in the same commit as the doc edit"
    )

    table_header = (
        r"\| Rows \| Bounded page \(`limit=1000`\) \| `aggregate` "
        r"\(~99% visible\) \| narrow-scope page \(~1% visible\) \|\n"
        r"\| ---: \| ---: \| ---: \| ---: \|\n"
        r"(?P<rows>(?:\|.+\|\n?)+)"
    )
    # A run's evidence window ends at the NEXT markdown heading of any level,
    # not at EOF. Bounding only at the next `#### Run` heading (the prior
    # bug) let a shape-broken LAST run's table search past its own section
    # and silently bind to any later cost-shaped table in the document --
    # discarding that run's real (corrupted) evidence without failing.
    next_heading = re.compile(r"(?m)^#{1,6} ")
    # A run's heading can go missing from `run_headings` (e.g. demoted from
    # `#### Run` to `##### Run`) while its cost table stays perfectly intact
    # -- that table would then never enter the per-run loop below, silently
    # dropping the run from the parse while it stays fully visible to a
    # human reader. T5 (envelope-backlog, DX hole 4): catch that by KEYING
    # ON POSITION, not on a document-wide COUNT of `table_header`-shaped
    # blocks (the pre-T5 shape hand-reserved a "+1" for the
    # `ObjectStore`/SQLite table above the runs -- coupling this
    # Postgres-only guard to that unrelated backend's table staying in one
    # exact shape and position; a benign SQLite-side edit (a reordered
    # column) then reds a Postgres audit with no Postgres-side edit at all).
    #
    # For each `#### Run` heading, compute the SAME `[heading.end(),
    # window_end)` window the per-run loop below computes, and -- if that
    # window's own first non-blank content is a `table_header` match --
    # treat that match's position as CLAIMED by this run (the exact
    # position the per-run loop's own "first content" check requires
    # below). A `table_header` match that is not claimed by any run this
    # way is an orphan UNLESS it is the single such match that sits before
    # the very first `#### Run` heading -- the one place, structurally,
    # where a different backend's own table (`ObjectStore`/SQLite, today)
    # can precede all Postgres run evidence; a SECOND stray match there is
    # exactly as much an orphan as one anywhere else. Unlike a check keyed
    # on "first content beneath ANY heading", a match that is NOT first
    # content beneath whatever (if anything) precedes it is not exempt
    # either -- it is simply unclaimed, and is flagged regardless of what
    # immediately precedes it.
    claimed_table_starts: set[int] = set()
    for heading in run_headings:
        boundary = next_heading.search(text, heading.end())
        window_end = boundary.start() if boundary is not None else len(text)
        window = text[heading.end() : window_end]
        run_candidates = list(re.finditer(table_header, window))
        if run_candidates and window[: run_candidates[0].start()].strip() == "":
            claimed_table_starts.add(heading.end() + run_candidates[0].start())

    any_heading = list(re.finditer(r"(?m)^#{1,6} .*$", text))

    def _nearest_heading_text(pos: int) -> str:
        owner = None
        for candidate in any_heading:
            if candidate.start() > pos:
                break
            owner = candidate
        return owner.group(0).strip() if owner is not None else "(no heading precedes it)"

    preamble_end = run_headings[0].start()
    preamble_slot_claimed = False
    orphaned_tables: list[str] = []
    for stray_table in re.finditer(table_header, text):
        if stray_table.start() in claimed_table_starts:
            continue
        if stray_table.start() < preamble_end and not preamble_slot_claimed:
            preamble_slot_claimed = True
            continue
        line_no = text.count("\n", 0, stray_table.start()) + 1
        orphaned_tables.append(
            f"line {line_no} (nearest heading: {_nearest_heading_text(stray_table.start())!r})"
        )
    assert not orphaned_tables, (
        "storage.md has a Postgres-cost-table-shaped block (the `| Rows | "
        "Bounded page ... |` shape) that is not claimed by any `#### Run "
        "<id>` heading's own window (as the first non-blank content "
        "beneath it, same as the per-run loop below requires) and is not "
        "the single such block tolerated before the first `#### Run` "
        f"heading -- orphan(s): {'; '.join(orphaned_tables)}. A run's table "
        "can go missing from this parse without ever failing an assertion "
        "inside the per-run loop below if its own `#### Run` heading stops "
        "matching (e.g. demoted to a different heading level) while the "
        "table itself is untouched, or a foreign cost-shaped table can be "
        "pasted in anywhere else in the document without tripping anything "
        "else"
    )
    cost_cell = re.compile(r"([\d,]+\.\d+)ms")
    # T4 (envelope-backlog): the row-count SET each run's table below must
    # contain, EXACTLY -- sourced from the workflow file, not a second
    # {1000, 5000, 25000} literal hand-copied here (S2). PUBLISHED_POSTGRES_RUN_IDS
    # only vouches for which runs are present; it says nothing about what
    # rows are inside them.
    expected_row_counts = _postgres_job_scan_curve_row_counts()

    worst_us_per_row = 0.0
    sample_count = 0
    for heading in run_headings:
        run_id = heading.group("run_id")
        boundary = next_heading.search(text, heading.end())
        window_end = boundary.start() if boundary is not None else len(text)
        window = text[heading.end() : window_end]
        table_matches = list(re.finditer(table_header, window))
        assert table_matches, (
            f"run {run_id}'s `#### Run` heading in storage.md has no Postgres "
            "cost table (the `| Rows | Bounded page ... |` shape) beneath it "
            "before the next markdown heading (or EOF) -- the table shape may "
            "have changed, or was pushed out of this run's own section"
        )
        assert len(table_matches) == 1, (
            f"run {run_id}'s section in storage.md has "
            f"{len(table_matches)} Postgres cost tables before the next "
            "heading -- exactly one is expected per run, so the evidence "
            "for this run is unambiguous"
        )
        table_match = table_matches[0]
        # The table must be the first non-blank content beneath the run's
        # own heading. Without this, shape-breaking a run's own table in
        # place (so it no longer matches `table_header`) while a
        # well-formed cost table sits later in that SAME run's window
        # (still well inside the next-heading boundary above) leaves
        # exactly one match -- that later, unrelated table -- and the run's
        # real (corrupted) evidence is silently discarded rather than
        # failing loudly.
        leading = window[: table_match.start()]
        assert leading.strip() == "", (
            f"run {run_id}'s Postgres cost table is not the first content "
            f"beneath its heading in storage.md -- found other text first: "
            f"{leading.strip()!r}. A run's own table must immediately "
            "follow its heading, so a shape-broken table cannot be "
            "silently skipped in favour of a cost-shaped table sitting "
            "later in the same run's section"
        )
        rows_text = table_match.group("rows").splitlines()
        assert rows_text, f"run {run_id}'s Postgres cost table has no data rows"
        run_row_counts: list[int] = []
        for row in rows_text:
            cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
            assert len(cells) == 4, (
                f"run {run_id}: malformed Postgres cost table row: {row!r}"
            )
            # T5 (envelope-backlog, DX hole 5): a bare `int(...)` here raises
            # Python's own context-free `ValueError` on malformed input. The
            # acceptance's literal "delete a heading, leave its rows behind"
            # framing cannot reach this code for any published run -- the
            # T3/E5e set-equality assertion above catches a missing heading
            # first -- so this is verified via a reachable substitute
            # instead: another table's own header-shaped block (its header
            # and separator rows) leaked directly after a run's own last
            # data row, no heading touched. The greedy `rows` capture above
            # swallows that leaked block as if it were more of this run's
            # own data, so its first cell ends up being the leaked table's
            # HEADER row's own literal `'Rows'` -- not its separator row,
            # never reached first. Name the run and the expected shape
            # before that swallowed value ever reaches `int(...)`.
            raw_row_count = cells[0].replace(",", "")
            assert re.fullmatch(r"\d+", raw_row_count), (
                f"run {run_id}: malformed row-count cell {cells[0]!r} in row "
                f"{row!r} -- expected a plain (optionally comma-grouped) "
                "integer in the `Rows` column. Another table's header or "
                "separator row may have leaked into this run's own row "
                "capture (e.g. pasted directly after this run's own last "
                "row, with no heading in between)"
            )
            n = int(raw_row_count)
            assert n > 0, f"run {run_id}: non-positive row count: {row!r}"
            run_row_counts.append(n)
            for cell in (cells[2], cells[3]):  # `aggregate`, narrow-scope page
                match = cost_cell.fullmatch(cell)
                assert match is not None, (
                    f"run {run_id}: malformed cost cell {cell!r} in row {row!r}"
                )
                us_per_row = float(match.group(1).replace(",", "")) * 1000.0 / n
                worst_us_per_row = max(worst_us_per_row, us_per_row)
                sample_count += 1

        # T4: this run's table must contain EXACTLY the CI-invoked row-count
        # SET -- no row missing, none extra, none duplicated within this ONE
        # run's table. `PUBLISHED_POSTGRES_RUN_IDS` above vouches only for
        # which runs exist; this is the census of what is INSIDE each one.
        parsed_row_counts = set(run_row_counts)
        assert len(run_row_counts) == len(parsed_row_counts), (
            f"run {run_id}'s Postgres cost table repeats a row count -- rows "
            f"seen, in table order: {run_row_counts}"
        )
        missing_n = expected_row_counts - parsed_row_counts
        extra_n = parsed_row_counts - expected_row_counts
        assert parsed_row_counts == expected_row_counts, (
            f"run {run_id}'s Postgres cost table has row counts "
            f"{sorted(parsed_row_counts)}, but `scripts/scan_curve.py` is invoked "
            f"with row counts {sorted(expected_row_counts)} in {WORKFLOW}'s "
            f"`postgres` job -- missing: {sorted(missing_n)}; extra "
            f"(unvouched-for): {sorted(extra_n)}. Every published run's table "
            "must contain EXACTLY the row counts CI actually measures for that "
            "step -- no row can go missing, get duplicated, or gain an extra "
            "one, without the doc's own evidence window silently narrowing or "
            "padding what it claims to cover"
        )

    assert sample_count > 0, (
        "parsed zero Postgres cost samples from storage.md -- this doc-parsing "
        "test must not pass vacuously"
    )

    worst_seconds_per_row = worst_us_per_row / 1_000_000.0
    one_second_crossing = 1.0 / worst_seconds_per_row
    published_rows = diagnose_module.STORAGE_ENVELOPE["PostgresStore"].rows
    assert published_rows <= one_second_crossing, (
        f"published PostgresStore rows={published_rows:,} exceeds the "
        f"one-second crossing ({one_second_crossing:,.1f} rows) implied by "
        "the slowest per-row rate in storage.md's own published Postgres "
        "run tables -- re-derive `STORAGE_ENVELOPE[\"PostgresStore\"]` from "
        "the worst rate across every row of every run table instead of "
        "raising the constant on its own"
    )


def test_storage_envelope_objectstore_ceiling_is_derived_from_the_published_curve() -> None:
    """T10 (envelope-backlog): give `ObjectStore` the SAME generating-rule
    guard `test_storage_envelope_postgres_ceiling_is_derived_from_the_published_curves`
    above gives `PostgresStore` -- before this test, the `ObjectStore` ceiling
    had NO derivation guard at all. The retired doc-wide count T5 replaced
    (`main:tests/test_docs.py:462`, `assert len(doc_wide_tables) ==
    len(run_headings) + 1`) never checked the SQLite table's own numbers
    either; it only proved SOME cost-table-shaped block existed at that
    position in the document. Deleting the table dropped the ceiling to a
    completely unvouched-for hand-copied constant, with nothing in the
    suite noticing.

    This test locates the `ObjectStore` (SQLite) cost table by anchoring on
    its own introducer paragraph, `` `ObjectStore` (SQLite): ``, and bounds
    its search window at whichever comes first: the next markdown heading,
    or the `` `PostgresStore` `` introducer paragraph that follows it in
    the doc today. Within that window it requires there be EXACTLY ONE
    cost-table-shaped block (same four-column shape the Postgres tables
    use) -- neither zero (the table went missing) nor more than one (an
    ambiguous or duplicated table). It then parses every data row, keying
    each CELL by its own HEADER TEXT rather than by column position: T5
    decoupled the Postgres audit from this table's exact shape specifically
    so the SQLite table's columns could be reordered without redding that
    guard, and this test must not silently re-couple them by assuming a
    fixed column order of its own. Reordering the `` `aggregate` `` and
    narrow-scope columns (header cells and every data row's cells, moved
    together) is mutation-proven below to leave this test green.

    Bounded, unordered pages are flat (per this section's own prose above)
    and carry no ceiling; only the two LINEAR classes -- `` `aggregate` ``
    and narrow-scope page -- are read for the worst (slowest) per-row rate.
    Re-derived directly from `docs/storage.md`'s published numbers, not
    copied from any ledger row (this branch's standing rule: every number
    in ledger prose is UNVERIFIED until re-derived from the primary
    source): the worst rate is 30.894us/row (the 100,000-row `` `aggregate`
    `` cell, 3,089.4ms / 100,000), implying a one-second crossing of
    1 / 0.000030894 ~= 32,369 rows -- at or above the published
    `STORAGE_ENVELOPE["ObjectStore"].rows == 32,000`, so this test passes
    today.

    L23: no millisecond figure is asserted here -- only a relation between
    numbers parsed from the doc and the published constant.

    Non-vacuousness: `sample_count` (one sample per linear-class cell
    across every data row) must be greater than zero, so an
    empty-but-well-formed table cannot pass this test by never entering the
    per-row loop.

    Residual bound, stated explicitly (mirroring the Postgres test's own):
    this closes "is `STORAGE_ENVELOPE[\"ObjectStore\"]` at or below the
    ceiling this doc's own published SQLite numbers imply", never "are
    those SQLite numbers themselves honest" -- they are laptop-measured
    (unlike the Postgres numbers, which are CI-measured and independently
    re-readable via `gh run view --log`) and cannot be re-derived from any
    CI artifact; a wholesale rewrite of the table to faster-but-fabricated
    numbers still ships green. That backstop remains a human re-running
    `scripts/scan_curve.py` locally, same class of residual gap the
    Postgres test's own docstring names for its "numbers rewritten in
    place" case.
    """
    storage = _DOCS / "storage.md"
    text = storage.read_text()

    introducer = re.search(r"`ObjectStore` \(SQLite\):", text)
    assert introducer is not None, (
        "storage.md has no `` `ObjectStore` (SQLite): `` introducer "
        "paragraph -- the SQLite cost table cannot be located, so this "
        "test would otherwise pass vacuously"
    )

    next_heading = re.compile(r"(?m)^#{1,6} ")
    heading_boundary = next_heading.search(text, introducer.end())
    postgres_intro = re.search(r"`PostgresStore`", text[introducer.end() :])
    boundary_candidates = [len(text)]
    if heading_boundary is not None:
        boundary_candidates.append(heading_boundary.start())
    if postgres_intro is not None:
        boundary_candidates.append(introducer.end() + postgres_intro.start())
    window_end = min(boundary_candidates)
    window = text[introducer.end() : window_end]

    # Key cells by HEADER TEXT, not by position, so the SQLite table's
    # columns can be reordered (T5's decoupling) without redding this
    # audit either.
    header_cell_patterns = {
        "rows": re.compile(r"Rows"),
        "bounded": re.compile(r"Bounded page \(`limit=1000`\)"),
        "aggregate": re.compile(r"`aggregate` \(~99% visible\)"),
        "narrow": re.compile(r"narrow-scope page \(~1% visible\)"),
    }

    lines = window.splitlines()
    table_headers: list[tuple[int, dict[int, str]]] = []
    for line_index in range(len(lines) - 1):
        header_line = lines[line_index].strip()
        if not (header_line.startswith("|") and header_line.endswith("|")):
            continue
        cells = [cell.strip() for cell in header_line.strip("|").split("|")]
        if len(cells) != 4:
            continue
        key_by_index: dict[int, str] = {}
        for index, cell in enumerate(cells):
            for key, pattern in header_cell_patterns.items():
                if pattern.fullmatch(cell):
                    key_by_index[index] = key
                    break
        if set(key_by_index.values()) != set(header_cell_patterns):
            continue
        separator_line = lines[line_index + 1].strip()
        if not re.fullmatch(r"(?:\| ---: ){4}\|", separator_line):
            continue
        table_headers.append((line_index, key_by_index))

    assert table_headers, (
        "storage.md's `ObjectStore` (SQLite) section (between its own "
        "introducer paragraph and the next heading or the `PostgresStore` "
        "introducer, whichever comes first) has no cost table (the "
        "`| Rows | Bounded page ... |` shape, in any column order) -- this "
        "test would otherwise pass vacuously"
    )
    assert len(table_headers) == 1, (
        f"storage.md's `ObjectStore` (SQLite) section has "
        f"{len(table_headers)} cost-table-shaped headers -- exactly one is "
        "expected"
    )
    header_line_index, key_by_index = table_headers[0]
    index_by_key = {key: index for index, key in key_by_index.items()}

    data_lines: list[str] = []
    cursor = header_line_index + 2
    while cursor < len(lines) and lines[cursor].strip().startswith("|"):
        data_lines.append(lines[cursor])
        cursor += 1
    assert data_lines, "storage.md's `ObjectStore` cost table has no data rows"

    cost_cell = re.compile(r"([\d,]+\.\d+)ms")
    worst_us_per_row = 0.0
    sample_count = 0
    for row in data_lines:
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        assert len(cells) == 4, f"malformed `ObjectStore` cost table row: {row!r}"
        raw_row_count = cells[index_by_key["rows"]].replace(",", "")
        assert re.fullmatch(r"\d+", raw_row_count), (
            f"malformed row-count cell {cells[index_by_key['rows']]!r} in "
            f"row {row!r} of storage.md's `ObjectStore` cost table -- "
            "expected a plain (optionally comma-grouped) integer"
        )
        n = int(raw_row_count)
        assert n > 0, f"non-positive row count in storage.md's `ObjectStore` cost table: {row!r}"
        for key in ("aggregate", "narrow"):  # bounded page is flat, no ceiling
            cell = cells[index_by_key[key]]
            match = cost_cell.fullmatch(cell)
            assert match is not None, (
                f"malformed `{key}` cost cell {cell!r} in row {row!r} of "
                "storage.md's `ObjectStore` cost table"
            )
            us_per_row = float(match.group(1).replace(",", "")) * 1000.0 / n
            worst_us_per_row = max(worst_us_per_row, us_per_row)
            sample_count += 1

    assert sample_count > 0, (
        "parsed zero `ObjectStore` cost samples from storage.md -- this "
        "doc-parsing test must not pass vacuously"
    )

    worst_seconds_per_row = worst_us_per_row / 1_000_000.0
    one_second_crossing = 1.0 / worst_seconds_per_row
    published_rows = diagnose_module.STORAGE_ENVELOPE["ObjectStore"].rows
    assert published_rows <= one_second_crossing, (
        f"published ObjectStore rows={published_rows:,} exceeds the "
        f"one-second crossing ({one_second_crossing:,.1f} rows) implied by "
        "the slowest per-row rate in storage.md's own published ObjectStore "
        "cost table -- re-derive `STORAGE_ENVELOPE[\"ObjectStore\"]` from "
        "the worst rate across every row of the table instead of raising "
        "the constant on its own"
    )


def test_storage_envelope_observed_run_log_matches_pinned_constant() -> None:
    """T3 (envelope-backlog, clause c''): the published run set was silently
    SELECTED from a larger pool -- every CI run that was actually read, not
    just the ones that went on to earn a `#### Run <id>` table. Before this
    test, two runs (`33704034596`, `33705109711`) were read and discarded
    from `docs/storage.md` with no trace anywhere in the tree that they had
    ever executed: the doc's own "measured four times" prose was, at that
    point, simply false -- the step had run six times.

    This test closes that with the SAME two-part shape
    `test_storage_envelope_postgres_ceiling_is_derived_from_the_published_curves`
    above uses for the published census: a generating rule that parses
    `docs/storage.md`'s observed-log table into a set of ids (S2 -- a real
    parse tied to the doc's own text, not a second hand-typed constant
    floating free of it), plus TWO invariants against the module-level
    `OBSERVED_POSTGRES_RUN_IDS` constant:

    1. `PUBLISHED_POSTGRES_RUN_IDS <= OBSERVED_POSTGRES_RUN_IDS` (subset, not
       equality): a run cannot earn a table without first being recorded as
       observed. Checked FIRST, against the two Python constants alone,
       before the doc is even read -- so dropping a still-published id out of
       `OBSERVED_POSTGRES_RUN_IDS` reds here regardless of what the doc says.
    2. `{parsed ids} == OBSERVED_POSTGRES_RUN_IDS` (equality, like the
       published census above): a row silently deleted from the doc's
       observed log, or a row silently added without a matching edit to the
       constant, reds either way, naming exactly which id is missing or
       extra.

    Accepted residual bound, stated rather than left implicit: this pin
    closes silent SELECTION (a run that was read and then discarded from the
    published tables now stays visible in the observed log) but NOT silent
    OMISSION -- a run that was never recorded here at all, whether never
    read or read and never written down, stays invisible to this test and to
    every other guard in this suite, because the offline test suite cannot
    reach GitHub to discover CI history on its own.

    Non-vacuousness (mutation-probed per the task's mutation-receipt
    protocol): deleting a row from the doc's observed-log table reds
    invariant 2 above, naming the id that went missing; adding a row without
    editing `OBSERVED_POSTGRES_RUN_IDS` reds invariant 2 the other way,
    naming the unvouched-for extra; dropping a still-published id from
    `OBSERVED_POSTGRES_RUN_IDS` reds invariant 1, before the doc is even
    parsed.
    """
    missing_from_observed = PUBLISHED_POSTGRES_RUN_IDS - OBSERVED_POSTGRES_RUN_IDS
    assert not missing_from_observed, (
        "PUBLISHED_POSTGRES_RUN_IDS is not a subset of OBSERVED_POSTGRES_RUN_IDS -- "
        f"{sorted(missing_from_observed)} earned a `#### Run <id>` table in "
        "docs/storage.md without first being recorded as observed. A run cannot be "
        "published without being observed; add the missing id(s) to "
        "OBSERVED_POSTGRES_RUN_IDS (and to storage.md's observed-log table) in the "
        "same commit that published them"
    )

    storage = _DOCS / "storage.md"
    text = storage.read_text()

    table_header = (
        r"(?m)^\| CI run id \| `postgres` job id \| Worst rate \(any row count, "
        r"either linear class\) \| Published as \|\n"
        r"\| ---: \| ---: \| ---: \| :--- \|\n"
        r"(?P<rows>(?:\|.+\|\n?)+)"
    )
    match = re.search(table_header, text)
    assert match is not None, (
        "storage.md is missing its `#### Observed log` table (the `| CI run id | "
        "`postgres` job id | ... |` shape) -- this test would otherwise pass "
        "vacuously"
    )

    parsed_observed_ids: set[int] = set()
    for row in match.group("rows").splitlines():
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        assert len(cells) == 4, (
            f"malformed row in storage.md's observed-run log: {row!r}"
        )
        id_match = re.fullmatch(r"\d+", cells[0].replace(",", ""))
        assert id_match is not None, (
            f"malformed CI run id cell in storage.md's observed-run log: {row!r}"
        )
        parsed_observed_ids.add(int(id_match.group()))

    assert parsed_observed_ids, (
        "parsed zero ids from storage.md's observed-run log -- this test would "
        "otherwise pass vacuously"
    )

    missing_from_doc = OBSERVED_POSTGRES_RUN_IDS - parsed_observed_ids
    extra_in_doc = parsed_observed_ids - OBSERVED_POSTGRES_RUN_IDS
    assert parsed_observed_ids == OBSERVED_POSTGRES_RUN_IDS, (
        "storage.md's observed-log table does not match the pinned evidence "
        f"census OBSERVED_POSTGRES_RUN_IDS={sorted(OBSERVED_POSTGRES_RUN_IDS)} -- "
        f"missing from the doc (pinned but no row found): {sorted(missing_from_doc)}; "
        f"extra in the doc (a row present but not pinned): {sorted(extra_in_doc)}. "
        "A run cannot be silently dropped from this log, or silently added to it, "
        "without editing OBSERVED_POSTGRES_RUN_IDS in the same commit as the doc "
        "edit"
    )


def test_v1_gate_envelope_pair_matches_published_constants() -> None:
    """`docs/v1-gate.md`'s quoted envelope pair is a STRUCTURAL pin against
    `ontary.diagnose.STORAGE_ENVELOPE`, not a hand-copied pair that can go
    stale the moment the code constant moves. E5c's own P0 finding was that
    this exact paragraph quoted the retracted `3,200 rows / 0.000312s/row`
    pair with nothing in `make verify` to catch it -- `docs/storage.md`'s
    ceiling table was pinned (see
    `test_storage_envelope_doc_ceiling_matches_published_constants` above)
    but this second, independently hand-copied prose pair was not. L23: no
    millisecond figure is asserted here -- only a relation between numbers
    PARSED from the doc's prose and the published constant, and nothing is
    measured at test time.
    """
    v1_gate = _DOCS / "v1-gate.md"
    text = v1_gate.read_text()

    pair = re.compile(
        r"`(?P<backend>ObjectStore|PostgresStore)` \([^)]+\) holds "
        r"\*\*(?P<rows>[\d,]+)\s+rows\*\* at \*\*(?P<seconds>[\d.]+)s/row\*\*"
    )
    documented = {
        match.group("backend"): (
            int(match.group("rows").replace(",", "")),
            float(match.group("seconds")),
        )
        for match in pair.finditer(text)
    }
    assert documented, (
        "v1-gate.md has no `` `<Backend>` (<Label>) holds **N rows** at "
        "**Ss/row** `` pair in its Decision C paragraph -- this test would "
        "otherwise pass vacuously"
    )

    published = {
        backend: (envelope.rows, envelope.seconds_per_row)
        for backend, envelope in diagnose_module.STORAGE_ENVELOPE.items()
    }
    assert documented == published, (
        f"v1-gate.md's Decision C paragraph quotes {documented!r} but "
        f"`STORAGE_ENVELOPE` publishes {published!r} -- re-copy the pair "
        "from the code constant (docs/storage.md#storage-envelope quotes "
        "the same pair and is pinned against it too)"
    )


def test_changelog_current_version_envelope_pair_matches_published_constants() -> None:
    """`CHANGELOG.md`'s current-version section quotes the SAME envelope pair
    `docs/storage.md` and `docs/v1-gate.md` do -- "...stays under one second
    below ~32,000 rows on `ObjectStore` ... and ~1,600 rows on
    `PostgresStore`..." -- and until T2 (envelope-backlog) this THIRD,
    independently hand-copied `rows` figure had zero structural coverage:
    confirmed by mutation probe, changing it left every test in this file
    (and `tests/test_packaging.py`, which reads `CHANGELOG.md` for an
    unrelated install-ref concern) green.

    Pinned RELEASE-RELATIVELY, not globally: this ranges ONLY over the
    CHANGELOG section whose heading is `## [<ontary.__version__>]` --
    today `## [0.9.0]`, since ledger decision 1 makes no version bump on
    this branch. A released section is HISTORY: once a future release
    re-derives the envelope, THIS section's prose must stay published
    verbatim and must never be hand-edited just to keep this test green.
    The next crossing bumps `ontary.__version__` and adds a NEW dated
    section above this one; this test then finds and binds THAT heading
    instead, and `## [0.9.0]` falls out of its search window entirely,
    untouched -- widen by releasing again, never by editing a past section.

    Like `test_storage_envelope_doc_ceiling_matches_published_constants`
    above, this prose never prints a `seconds_per_row` figure either --
    only "~N rows" -- so `seconds_per_row` is bound the same way: the
    quoted row count is itself a claim ("stays under one second below ~N
    rows"), so `documented_rows * STORAGE_ENVELOPE[backend].seconds_per_row`
    must land in the same [0.9, 1.0] one-second-crossing band used
    throughout this file and in `tests/test_diagnose_lints.py`.
    """
    version = ontary.__version__
    text = CHANGELOG.read_text()
    heading = re.search(rf"(?m)^## \[{re.escape(version)}\][^\n]*\n", text)
    assert heading is not None, (
        f"CHANGELOG.md has no `## [{version}]` heading matching "
        f"ontary.__version__={version!r} -- a release-relative pin must be "
        "able to find the CURRENT version's own section by that heading; "
        "if a version bump just landed, add its dated section first"
    )
    next_heading = re.search(r"(?m)^## ", text[heading.end() :])
    section_end = heading.end() + next_heading.start() if next_heading else len(text)
    section = text[heading.end() : section_end]

    pair = re.compile(
        r"~(?P<rows>[\d,]+) rows on `(?P<backend>ObjectStore|PostgresStore)`"
    )
    documented = {
        match.group("backend"): int(match.group("rows").replace(",", ""))
        for match in pair.finditer(section)
    }
    assert documented, (
        f"CHANGELOG.md's `## [{version}]` section has no `~N rows on "
        "`<Backend>`` envelope mention -- this test would otherwise pass "
        "vacuously, and NOT because the pair moved to a different section: "
        "only the section whose heading matches ontary.__version__ is ever "
        "searched"
    )

    published_rows = {
        backend: envelope.rows
        for backend, envelope in diagnose_module.STORAGE_ENVELOPE.items()
    }
    assert documented == published_rows, (
        f"CHANGELOG.md's `## [{version}]` section quotes {documented!r} rows "
        f"but `STORAGE_ENVELOPE` publishes {published_rows!r} -- a released "
        "section's prose is history and must not be hand-edited to chase a "
        "constant that moved; if this version's own envelope measurement is "
        "genuinely still being finalized, land the correction under a NEW "
        "`## [Unreleased]` (or the next version's) section instead"
    )

    for backend, doc_rows in documented.items():
        envelope = diagnose_module.STORAGE_ENVELOPE[backend]
        product = doc_rows * envelope.seconds_per_row
        assert 0.9 <= product <= 1.0, (
            f"CHANGELOG.md's `## [{version}]` section claims {backend} stays "
            f"under one second below ~{doc_rows:,} rows, but "
            f"STORAGE_ENVELOPE[{backend!r}].seconds_per_row="
            f"{envelope.seconds_per_row} puts {doc_rows:,} * "
            f"seconds_per_row={product:.4f}s outside the [0.9, 1.0] "
            "one-second-crossing band"
        )


def test_fixture_honesty_job_claimed_by_docs_exists_in_ci() -> None:
    """Both docs call the honesty job the writer-freeze enforcement; pin it.

    The claim is only true while the job exists, and a reader can only check it
    if the docs name the job and its workflow. Deleting or renaming the job, or
    dropping either name from either doc, reds here. This pins the artifact,
    not the prose around it: a doc can still describe a live job wrongly.
    """
    workflow_path = WORKFLOW.relative_to(WORKFLOW.parents[2]).as_posix()
    parts = WORKFLOW.read_text().split("\njobs:\n", 1)
    assert len(parts) == 2, f"{workflow_path} has no top-level `jobs:` block"
    jobs = set(re.findall(r"(?m)^  ([a-z][a-z0-9-]*):$", parts[1]))
    assert HONESTY_JOB in jobs, (
        f"docs call `{HONESTY_JOB}` the writer-freeze enforcement, but "
        f"{workflow_path} defines only {sorted(jobs)}"
    )

    for path in (_DOCS / "v1-gate.md", EXAMPLES / "tickets" / "README.md"):
        text = path.read_text()
        assert HONESTY_JOB in text, f"{path.name} does not name the {HONESTY_JOB} job"
        assert workflow_path in text, f"{path.name} does not name {workflow_path}"


def _honesty_job_block() -> tuple[str, str]:
    """Return (`workflow_path`, the `HONESTY_JOB` job's raw YAML block).

    Shared by every test that inspects the `upgrade-fixture-honesty` job's
    shape, so the job-block-splitting regex lives in exactly one place.
    """
    workflow_path = WORKFLOW.relative_to(WORKFLOW.parents[2]).as_posix()
    parts = WORKFLOW.read_text().split("\njobs:\n", 1)
    assert len(parts) == 2, f"{workflow_path} has no top-level `jobs:` block"

    job_matches = list(
        re.finditer(r"(?m)^  ([a-z][a-z0-9-]*):$", parts[1])
    )
    job_blocks = {
        match.group(1): parts[1][
            match.end() : (
                job_matches[index + 1].start()
                if index + 1 < len(job_matches)
                else len(parts[1])
            )
        ]
        for index, match in enumerate(job_matches)
    }
    job_block = job_blocks.get(HONESTY_JOB, "")
    assert job_block.strip(), (
        f"{workflow_path} job `{HONESTY_JOB}` block not found or empty; expected a "
        "non-empty two-space-indented top-level job block (update this parser if the "
        "workflow shape changed)"
    )
    return workflow_path, job_block


def test_fixture_honesty_job_falls_back_only_for_the_untagged_current_version() -> None:
    """D-1: the honesty job's untagged-current-version fallback, by shape only.

    This proves the YAML SHAPE of the fallback exists; it cannot prove the
    fallback executes correctly (that only runs in GitHub Actions -- L30).
    Each required literal is mutation-proven by deleting it from a scratch
    copy of the workflow (probe protocol) and confirming this test reds
    naming that literal.
    """
    workflow_path, job_block = _honesty_job_block()

    required_literals = (
        (
            "git ls-remote --tags origin",
            "the resolve step must query the remote for the tag before falling back",
        ),
        (
            "pyproject.toml",
            "the resolve step must read the current version from pyproject.toml",
        ),
        (
            '[ "${{ matrix.tag }}" = "v$version" ]',
            "the fallback must be keyed on the matrix tag literally equalling "
            "v<current version>, not any looser check",
        ),
        (
            "describe --tags --exact-match",
            "the tagged-mode proof must still exact-match the checked-out tag",
        ),
        (
            "::warning::",
            "an untagged-current fallback run must emit a visible warning so the "
            "log cannot read as a tagged run",
        ),
    )
    for literal, why in required_literals:
        assert literal in job_block, (
            f"{workflow_path} job `{HONESTY_JOB}` is missing {literal!r}: {why}"
        )
    assert "untagged-current" in job_block, (
        f"{workflow_path} job `{HONESTY_JOB}`'s `::warning::` must name the "
        "untagged-current fallback mode"
    )


def test_fixture_honesty_matrix_covers_required_fixture_tags() -> None:
    workflow_path, job_block = _honesty_job_block()

    tag_lines = re.findall(r"(?m)^\s+tag:\s*\[([^\]\n]*)\]\s*$", job_block)
    assert len(tag_lines) == 1, (
        f"{workflow_path} job `{HONESTY_JOB}` expected exactly one inline "
        f"`tag: [vX.Y.Z, ...]` matrix line, found {len(tag_lines)}; update this parser "
        "if the matrix changed to YAML block style or fromJSON"
    )
    tags = {tag.strip() for tag in tag_lines[0].split(",") if tag.strip()}
    assert tags, (
        f"{workflow_path} job `{HONESTY_JOB}` expected a non-empty inline "
        "`tag: [vX.Y.Z, ...]` matrix list; update this parser if the matrix shape changed"
    )

    required_tags = set(REQUIRED_FIXTURE_TAGS)
    assert tags == required_tags, (
        f"{workflow_path} job `{HONESTY_JOB}` matrix must equal REQUIRED_FIXTURE_TAGS; "
        f"missing {sorted(required_tags - tags)}, extra {sorted(tags - required_tags)}"
    )


def test_v1_gate_l30_bullet_names_every_ci_only_job() -> None:
    """S2 (T6, envelope-backlog): census EVERY CI-only job, not a hand-typed set.

    `docs/v1-gate.md`'s L30 bullet used to name exactly two jobs (the
    `postgres` e2e job and the `upgrade-fixture-honesty` job) as the ones
    `make verify` never runs -- an undercount found by re-deriving from
    `.github/workflows/verify.yml` itself: `package` (the bare-`pip install`
    packaging smoke test) is CI-only too, and was never restated as such.
    This closes it with ONE generating rule instead of a third fixed
    literal: parse `verify.yml`'s own top-level job ids (the same
    `jobs:`-split-plus-`^  <job-id>:$`-regex census used by
    `_postgres_job_scan_curve_row_counts` and
    `test_fixture_honesty_matrix_covers_required_fixture_tags` above),
    subtract `verify` -- the one job with a local equivalent (`make verify`,
    run by both arms of this feature's own DoD) -- and assert every
    REMAINING id is named in the L30 bullet's own text. Add a fourth CI-only
    job to `verify.yml` and this reds naming that job id, with no edit to
    this file, until `docs/v1-gate.md` is updated to match.

    Scoped to the bullet's OWN text, not the whole document: several of the
    surrounding bullets in this same "Release checklist" section also name
    `upgrade-fixture-honesty` (e.g. "extend the `upgrade-fixture-honesty`
    matrix"), so checking the whole page would make deleting a job's name
    from the L30 bullet specifically invisible to this test -- it would
    still find that name elsewhere on the page and pass regardless. The
    bullet is isolated by matching from its own literal "Restate the **L30
    CI-only blind spot**" opening through to (not including) the next
    top-level `- ` list item or heading.

    Matching is plain substring containment of the LITERAL job id against
    the bullet's own text -- not a fuzzy or aliased match -- which is why
    the L30 bullet was rewritten (this same task) to hold each job id in
    backticks alongside its prose gloss, rather than relying on the prior
    purely-descriptive phrasing ("the Postgres e2e job", "the
    fixture-honesty job") that never printed a literal job id at all.
    """
    text = WORKFLOW.read_text()
    parts = text.split("\njobs:\n", 1)
    assert len(parts) == 2, f"{WORKFLOW} has no top-level `jobs:` block"
    job_ids = re.findall(r"(?m)^  ([a-z][a-z0-9-]*):$", parts[1])
    assert job_ids, f"{WORKFLOW} defines no top-level jobs under `jobs:`"
    ci_only_job_ids = [job_id for job_id in job_ids if job_id != "verify"]
    assert ci_only_job_ids, (
        f"{WORKFLOW} defines no CI-only job besides `verify` -- nothing for "
        "docs/v1-gate.md's L30 bullet to name"
    )

    gate_path = _DOCS / "v1-gate.md"
    gate_text = gate_path.read_text()
    bullet_match = re.search(
        r"(?ms)^- Restate the \*\*L30 CI-only blind spot\*\*.*?(?=\n- |\n#|\Z)",
        gate_text,
    )
    assert bullet_match is not None, (
        f"{gate_path} has no 'Restate the L30 CI-only blind spot' bullet in "
        "its Release checklist -- update this parser if the bullet's own "
        "wording changed"
    )
    bullet_text = bullet_match.group(0)

    for job_id in ci_only_job_ids:
        assert job_id in bullet_text, (
            f"`.github/workflows/verify.yml` job `{job_id}` is CI-only (it "
            "is not `verify`, the one job `make verify` mirrors) but "
            f"{gate_path}'s L30 CI-only blind spot bullet does not name "
            f"`{job_id}` -- a release engineer restating that bullet would "
            f"never be told `{job_id}` has no local pre-merge proof"
        )


def test_readme_and_new_pages_have_no_broken_relative_markdown_links() -> None:
    link_pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)")
    sources = (README, *NEW_ENGLISH_DOCS)
    for source in sources:
        for target in link_pattern.findall(source.read_text()):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path_text = target.split("#", 1)[0]
            if not path_text:
                continue
            target_path = (source.parent / path_text).resolve()
            assert target_path.exists(), (
                f"{source.relative_to(README.parent)} links to missing "
                f"relative path {target!r}"
            )


def test_new_pages_do_not_reintroduce_removed_or_renamed_api_names() -> None:
    violations: list[str] = []

    # The six newly added English pages have no legitimate use for any removed
    # or renamed API name.
    for path in NEW_ENGLISH_DOCS:
        text = path.read_text()
        stale = [name for name in STALE_DOC_NAMES if name in text]
        for name in stale:
            line_number = next(
                index for index, line in enumerate(text.splitlines(), start=1) if name in line
            )
            violations.append(
                f"{path.name}:{line_number} contains removed/renamed name: {name}"
            )

    # The API references document some still-live engine and MCP names.  Only
    # the BoundQuery section is the renamed Python surface, so keep this check
    # scoped there instead of exempting `get_object` by name everywhere.
    for path in API_REFERENCES:
        text = path.read_text()
        other_stale = [
            name
            for name in STALE_DOC_NAMES
            if name not in BOUND_QUERY_STALE_NAMES and name in text
        ]
        violations.extend(
            f"{path.name} contains removed/renamed name: {name}" for name in other_stale
        )

        section_match = re.search(
            r"(?ms)^### `BoundQuery`\n.*?(?=^### |\Z)",
            text,
        )
        assert section_match is not None, f"{path.name} is missing the BoundQuery section"
        bound_query_stale = [
            name for name in BOUND_QUERY_STALE_NAMES if name in section_match.group()
        ]
        violations.extend(
            f"{path.name} under ### `BoundQuery` contains removed/renamed name: {name}"
            for name in bound_query_stale
        )

    assert not violations, "stale documentation names:\n" + "\n".join(violations)


def test_readme_error_codes_section_points_to_the_pinned_table() -> None:
    """T12 removes the old standalone README error-code section.

    The full table is still written once and pinned once by
    `test_api_reference_error_table_matches_error_codes_exactly` below. This
    guard now checks the API-reference row in the new Where-to-go table, so the
    pointer survives the README rewrite without duplicating the catalog.
    """
    readme_text = _read_readme()
    where_to_go = readme_text[readme_text.index("## Where to go") :]
    assert "docs/api-reference.md#error-codes" in where_to_go


def test_readme_quickstart_executes_verbatim() -> None:
    """The README's one promised quickstart must run exactly as published.

    The AST check keeps every root import on the curated front door; executing
    the extracted block catches a snippet that only looks plausible after a
    docs condensation.
    """
    readme = _read_readme()
    match = re.search(
        r"<!-- quickstart-runnable:start -->\n"
        r"```python\n(.*?)```\n"
        r"<!-- quickstart-runnable:end -->",
        readme,
        re.DOTALL,
    )
    assert match is not None, "README quickstart markers or Python fence are missing"
    code = match.group(1)
    tree = ast.parse(code, filename="README.md#quickstart")
    imported: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module != "ontary":
            continue
        imported.update(alias.name for alias in node.names if alias.name != "*")
    assert imported, "README quickstart imports no names from the ontary front door"
    assert imported <= set(ontary.__all__)
    assert not [name for name in imported if not hasattr(ontary, name)]
    namespace: dict[str, object] = {}
    exec(compile(code, "README.md#quickstart", "exec"), namespace)


COOKBOOK_RECIPES = (
    "scoped-type",
    "testing-action",
    "type-evolution",
    "explain-hidden-row",
    "serve-dev",
)


@pytest.mark.parametrize("recipe", COOKBOOK_RECIPES)
def test_cookbook_recipes_execute_verbatim(recipe: str) -> None:
    """Every cookbook recipe is run from the exact Python fence we publish."""
    cookbook = _DOCS / "cookbook.md"
    text = cookbook.read_text()
    marker = re.escape(f"cookbook-{recipe}-runnable")
    match = re.search(
        rf"<!-- {marker}:start -->\n"
        rf"```python\n(.*?)```\n"
        rf"<!-- {marker}:end -->",
        text,
        re.DOTALL,
    )
    assert match is not None, f"missing runnable marker for cookbook recipe {recipe!r}"
    code = match.group(1)
    tree = ast.parse(code, filename=f"docs/cookbook.md#{recipe}")
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module == "ontary":
            imported = {alias.name for alias in node.names if alias.name != "*"}
            assert imported <= set(ontary.__all__)
        elif node.module == "ontary.explain":
            assert {alias.name for alias in node.names} <= {"DecisionTrace"}
        elif node.module == "ontary.migrate":
            assert {alias.name for alias in node.names} <= {"upcast_object_type"}
        elif node.module == "ontary.testing":
            assert {alias.name for alias in node.names} <= {
                "FixedClock",
                "SequentialIds",
                "capture_effects",
                "consumer",
                "make_store",
                "raises_code",
            }
        elif node.module is not None and node.module.startswith("ontary"):
            raise AssertionError(
                f"cookbook recipe {recipe!r} imports engine name(s) from {node.module!r}"
            )
    namespace: dict[str, object] = {}
    exec(compile(code, f"docs/cookbook.md#{recipe}", "exec"), namespace)


def test_readme_is_within_line_budget() -> None:
    """T12's README skeleton has a hard 250-line budget."""
    assert len(_read_readme().splitlines()) <= 250


def _parse_api_reference_error_rows(text: str) -> dict[str, str]:
    """Parse every `| \\`CODE\\` | description |` row out of an API
    reference's per-kind error tables into `{code: description}`.

    The API references group the codes by `kind` into one table per kind
    (the kind is the heading, so it is not repeated per row) -- unlike the former
    README flat `| Code | Kind | Description |` table.
    """
    rows: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\| `([A-Z0-9_]+)` \| (.*?) \|$", line.strip())
        if match is not None:
            rows[match.group(1)] = match.group(2)
    return rows


@pytest.mark.parametrize("path", API_REFERENCES, ids=lambda p: p.name)
def test_api_reference_error_table_matches_error_codes_exactly(path: Path) -> None:
    """The API references' error tables are generated from `ERROR_CODES`;
    pin the sole published tables so a new/renamed code can never leave a stale
    description sitting in the docs.

    Descriptions are deliberately NOT translated in the Japanese reference
    (they are source strings), so both files must match `ERROR_CODES`
    verbatim.
    """
    assert path.exists(), f"missing API reference: {path}"
    rows = _parse_api_reference_error_rows(path.read_text())

    # The exception-index table at the end of the file uses a different
    # column count and is filtered out by the row regex above; what remains
    # must be exactly the error-code set.
    assert set(rows) == set(ERROR_CODES), (
        f"{path.name}: error-code table drifted from ERROR_CODES; "
        f"missing={sorted(set(ERROR_CODES) - set(rows))}, "
        f"extra={sorted(set(rows) - set(ERROR_CODES))}"
    )
    for code, description in rows.items():
        assert description == " ".join(ERROR_CODES[code].description.split()), (
            f"{path.name}: {code} description drifted from ERROR_CODES"
        )


@pytest.mark.parametrize("path", API_REFERENCES, ids=lambda p: p.name)
def test_api_reference_error_summary_matches_error_codes(path: Path) -> None:
    """The summary line derives both published counts from the live catalog."""
    text = path.read_text()
    if path.name.endswith(".ja.md"):
        pattern = r"^\*全 (?P<count>\d+) コード / (?P<kinds>\d+) 種別。\*$"
    else:
        pattern = r"^\*(?P<count>\d+) codes across (?P<kinds>\d+) kinds\.\*$"
    match = re.search(pattern, text, re.MULTILINE)
    assert match is not None, f"{path.name}: missing error-code summary line"
    assert int(match.group("count")) == len(ERROR_CODES)
    assert int(match.group("kinds")) == len({info.kind for info in ERROR_CODES.values()})


def test_changelog_new_error_codes_match_catalog_diff() -> None:
    """The 0.8.0 migration bullet lists the checked-in catalog diff."""

    changelog = (README.parent / "CHANGELOG.md").read_text()
    start = changelog.index("- **New error codes:**")
    end = changelog.find("\n\n", start)
    bullet = changelog[start:] if end == -1 else changelog[start:end]
    listed_codes = re.findall(r"`([A-Z][A-Z0-9_]+)`", bullet)

    assert EXPECTED_080_NEW_ERROR_CODES <= set(ERROR_CODES)
    assert set(ERROR_CODES) - EXPECTED_080_NEW_ERROR_CODES == EXPECTED_PRE_080_ERROR_CODES
    assert sorted(listed_codes) == sorted(EXPECTED_080_NEW_ERROR_CODES)


def test_effect_meta_has_exact_dispatch_safe_field_set() -> None:
    # Exact, not a subset: the point of this assertion is that WIDENING
    # `EffectMeta` fails a test rather than passing review, because the field
    # set is what keeps a dispatcher from being handed a path back into
    # ontology state. `effect_id` and `attempt` (durable outbox) are delivery
    # bookkeeping -- a uuid and an int, neither of which reaches the store.
    assert set(EffectMeta.model_fields) == {
        "action",
        "actor_id",
        "role",
        "ts",
        "effect_id",
        "attempt",
    }


def test_effect_payload_subclass_rejects_attribute_reassignment() -> None:
    class Notification(EffectPayload):
        message: str

    payload = Notification(message="before")

    with pytest.raises(ValidationError):
        payload.message = "after"


def test_readme_governed_effects_snippet_executes_verbatim() -> None:
    effects_text = (_DOCS / "effects.md").read_text()
    match = re.search(
        r"<!-- governed-effects-runnable:start -->\n"
        r"```python\n(.*?)```\n"
        r"<!-- governed-effects-runnable:end -->",
        effects_text,
        re.DOTALL,
    )
    assert match is not None
    namespace: dict[str, object] = {}
    exec(compile(match.group(1), "docs/effects.md", "exec"), namespace)
    assert len(namespace["delivered"]) == 1


def test_readme_governed_effects_states_the_honest_contract() -> None:
    section = (_DOCS / "effects.md").read_text()
    normalized = " ".join(section.split())

    required = [
        "Capabilities are declared reads or calls to the outside world",
        "Delivery is at-least-once.",
        "must therefore be idempotent on `EffectMeta.effect_id`",
        "pending audit row proves committed intent",
        "provider accesses",
        "A direct registry invocation has no client boundary",
        "Effects are therefore declared and audited, not sandboxed or filtered",
        "A provider can perform an outward write inline",
        "Every audit entry written by an action invocation carries the same",
    ]
    for statement in required:
        assert statement in normalized


def test_authority_page_names_every_declarations_field() -> None:
    section = (_DOCS / "authority.md").read_text()

    # Every field name (accounting for the min-N/min_n spelling) must have a
    # matching prose heading somewhere in the section.
    expected_headings = {
        "authority": "Authority",
        "capabilities": "Capabilities",
        "effects": "Effects",
        "writeback": "Write-back",
        "reingest": "Re-ingest",
        "visibility_default": "Visibility default",
        "transaction_ownership": "Transaction ownership",
        "ontology_evolution": "Ontology evolution",
        "idempotency": "Idempotency",
        "audit_scope": "Audit scope",
        "tenancy": "Tenancy",
        "identity": "Identity",
        "min_n": "min-N",
    }
    for field_name in Declarations.model_fields:
        heading = expected_headings[field_name]
        assert heading in section, (
            f"Declarations field {field_name!r} has no matching heading "
            f"{heading!r} in the authority page's Declared answers section"
        )


@pytest.mark.parametrize("path", API_REFERENCES, ids=lambda p: p.name)
def test_api_reference_declarations_prose_names_every_declarations_field(
    path: Path,
) -> None:
    """P1-2 (`multi-consumer-mcp` T7 review): mirror the README's
    `model_fields`-exhaustive pin above for the API references. Before this,
    neither reference's `Declarations` paragraph named anything but
    `identity` -- the exact hole T4 was bounced for (an EN edit with no test
    keeping JA in lockstep). Driven off `Declarations.model_fields`, not a
    hand-maintained list (lesson L-c), so a future field silently missing
    from either reference's prose fails this rather than waiting for a
    reviewer to notice by eye.
    """
    text = path.read_text()
    start = text.index("Declarations`**")
    end = text.index("\n## ", start)
    section = text[start:end]
    for field_name in Declarations.model_fields:
        assert f"`{field_name}`" in section, (
            f"{path.name}: Declarations field {field_name!r} has no "
            f"matching `{field_name}` mention in the API reference's "
            "Declarations paragraph"
        )


def test_ontary_all_is_sorted_unique_and_importable() -> None:
    all_names = ontary.__all__
    # This is the hard front-door budget.  A 59th name is an intentional
    # regression: it makes the authoring vocabulary larger than the contract.
    assert len(all_names) <= 58, (
        "ontary.__all__ exceeds the front-door budget: "
        f"{len(all_names)} names"
    )
    assert len(all_names) == len(set(all_names)), (
        "ontary.__all__ has duplicate name(s): "
        f"{[n for n in all_names if all_names.count(n) > 1]}"
    )
    assert all_names == sorted(all_names), (
        "ontary.__all__ has drifted out of sorted order -- re-run "
        "sorted() over the list after adding a name rather than "
        "eyeballing insertion order"
    )
    missing = [n for n in all_names if not hasattr(ontary, n)]
    assert not missing, f"ontary.__all__ names not importable off ontary: {missing}"
    missing_runtime = sorted(FRONT_DOOR_RUNTIME_NAMES - set(all_names))
    assert not missing_runtime, (
        "required runtime/front-door names are missing from ontary.__all__: "
        f"{missing_runtime}"
    )


def _ontary_types_in_annotation(annotation: object) -> set[type[object]]:
    found: set[type[object]] = set()

    def visit(value: object) -> None:
        if get_origin(value) is not None:
            for argument in get_args(value):
                visit(argument)
            return
        if inspect.isclass(value):
            module_name = getattr(value, "__module__", "")
            if isinstance(module_name, str) and module_name.startswith("ontary"):
                found.add(value)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                visit(item)
        for argument in get_args(value):
            visit(argument)

    visit(annotation)
    return found


def _public_type_hints(member: object) -> tuple[object, ...]:
    if isinstance(member, property):
        member = member.fget
    if member is None or not callable(member):
        return ()
    try:
        return tuple(get_type_hints(member).values())
    except (NameError, TypeError):
        # Optional extras can leave a return annotation unresolved in a core-only
        # install (for example FastMCP). Resolved signatures still contribute
        # through their other reachable members. Retry with unresolved names
        # bound to an opaque placeholder so one missing forward reference does
        # not hide a sibling public type such as RetryPolicy.
        function = getattr(member, "__func__", member)
        globalns = dict(getattr(function, "__globals__", {}))
        for _ in range(8):
            try:
                return tuple(get_type_hints(member, globalns=globalns).values())
            except NameError as exc:
                missing_name = getattr(exc, "name", None)
                if not isinstance(missing_name, str):
                    break
                globalns[missing_name] = object
            except TypeError:
                break
        return ()


def test_ontary_signature_closure() -> None:
    """The curated front door is closed under its public type signatures.

    Walk public methods/properties, callback aliases, and Pydantic model fields
    from every root export. The demotion inventory is the intentional boundary
    for engine-only types; `PagedRow` is explicitly engine-internal in
    `ontary.__init__`. A newly named public ontary type outside those exceptions
    must be importable from the root as well.
    """
    root_names = set(ontary.__all__)
    demoted_names = {
        name for names in DEMOTED_NAMES_BY_MODULE.values() for name in names
    }
    internal_names = {"PagedRow"}
    signature_bridges = {("ontary.client", "OntologyRuntime")}
    pending: list[object] = [getattr(ontary, name) for name in ontary.__all__]
    seen: set[type[object]] = set()
    missing: set[str] = set()

    while pending:
        member = pending.pop()
        annotations: list[object] = []

        if inspect.isclass(member):
            annotations.extend(_public_type_hints(member))
            if issubclass(member, BaseModel):
                annotations.extend(
                    field.annotation for field in member.model_fields.values()
                )
            if member in seen:
                continue
            seen.add(member)
            for method_name, method in inspect.getmembers(member):
                if method_name.startswith("_") and method_name != "__init__":
                    continue
                annotations.extend(_public_type_hints(method))
        elif inspect.isroutine(member) or isinstance(member, property):
            annotations.extend(_public_type_hints(member))
        else:
            # Callable/type aliases such as EffectDispatcher expose their
            # argument types through get_args(), not get_type_hints().
            annotations.append(member)

        for annotation in annotations:
            for candidate in _ontary_types_in_annotation(annotation):
                candidate_name = candidate.__name__
                qualified_name = f"{candidate.__module__}.{candidate_name}"
                module = sys.modules.get(candidate.__module__)
                module_exports = getattr(module, "__all__", ())
                is_public_type = (
                    candidate_name in module_exports or candidate_name == "Declarations"
                )
                if (
                    is_public_type
                    and candidate_name not in root_names
                    and candidate_name not in demoted_names
                    and candidate_name not in internal_names
                ):
                    missing.add(qualified_name)
                if candidate not in seen and (
                    candidate_name in root_names
                    or (candidate.__module__, candidate_name) in signature_bridges
                ):
                    pending.append(candidate)

    assert not missing, (
        "ontary.__all__ is not closed under its public signatures; missing root "
        f"exports: {sorted(missing)}"
    )


def test_demoted_names_remain_importable_at_canonical_submodules() -> None:
    """Demotion changes the root surface, not the engine's defining modules.

    The guard rejects both failure modes: a name disappearing from its
    canonical module (for example `MappingValidationError` from
    `ontary.connect`) and an accidental re-export back onto the curated root.
    """
    demoted = {
        name for names in DEMOTED_NAMES_BY_MODULE.values() for name in names
    }
    assert demoted.isdisjoint(set(ontary.__all__)), (
        "demoted names were restored to the front door: "
        f"{sorted(demoted & set(ontary.__all__))}"
    )
    still_bound = sorted(name for name in demoted if hasattr(ontary, name))
    assert not still_bound, (
        "demoted names are still bound on the ontary module: "
        f"{still_bound}"
    )

    missing: list[str] = []
    for module_name, names in DEMOTED_NAMES_BY_MODULE.items():
        module = importlib.import_module(module_name)
        missing.extend(
            f"{module_name}.{name}"
            for name in sorted(names)
            if not hasattr(module, name)
        )
    assert not missing, f"demoted names missing from canonical modules: {missing}"


def test_examples_import_front_door_names_from_ontary() -> None:
    """Examples must use the curated root for every front-door symbol.

    The root-import side is AST-driven over the whole `examples/` tree, so a
    newly added example import fails until its name is deliberately added to
    the front door.  The submodule side catches the inverse mistake: a name
    that is still in `__all__` must not be taught through a deeper namespace.
    """
    front_door_names = set(ontary.__all__)
    root_imports: set[str] = set()
    root_missing: list[str] = []
    offenders: list[str] = []

    for path in sorted(EXAMPLES.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            relative_path = path.relative_to(EXAMPLES.parent)
            if node.module == "ontary":
                for imported in node.names:
                    if imported.name == "*":
                        continue
                    root_imports.add(imported.name)
                    if imported.name not in front_door_names:
                        root_missing.append(
                            f"{relative_path}:{node.lineno}: {imported.name}"
                        )
                    elif not hasattr(ontary, imported.name):
                        root_missing.append(
                            f"{relative_path}:{node.lineno}: {imported.name} (not importable)"
                        )
                continue
            if node.module is None or not node.module.startswith("ontary."):
                continue
            for imported in node.names:
                if imported.name in front_door_names:
                    offenders.append(f"{relative_path}:{node.lineno}: {imported.name}")

    assert not root_missing, (
        "examples import non-front-door names from `ontary`; use the canonical "
        "engine submodule for demoted names:\n" + "\n".join(root_missing)
    )
    assert not offenders, (
        "examples import front-door names from ontary submodules; use "
        "`from ontary import ...` instead:\n" + "\n".join(offenders)
    )
    assert root_imports, "examples AST scan found no front-door imports"


@pytest.mark.parametrize("path", API_REFERENCES, ids=lambda p: p.name)
def test_api_reference_export_coverage(path: Path) -> None:
    """Both references list the complete root and demoted engine surfaces.

    Checking isolated heading sections prevents a name mentioned only in a
    later narrative example from masquerading as a front-door or canonical
    engine listing.  Removing one listed name, or moving it under the wrong
    submodule heading, therefore fails with the exact missing location.
    """
    text = path.read_text()

    def section(start: str, end: str) -> str:
        start_index = text.index(start)
        end_index = text.index(end, start_index)
        return text[start_index:end_index]

    if path.name.endswith(".ja.md"):
        front_heading = "## フロントドア"
        engine_heading = "## エンジン API"
        authoring_heading = "## オントロジーを宣言する"
    else:
        front_heading = "## Front door"
        engine_heading = "## Engine surface"
        authoring_heading = "## Authoring an ontology"

    front_door = section(front_heading, engine_heading)
    missing_front_door = [
        name for name in ontary.__all__ if f"`{name}`" not in front_door
    ]
    assert not missing_front_door, (
        f"{path.name}: front-door section omits {missing_front_door}"
    )

    engine_surface = section(engine_heading, authoring_heading)
    missing_engine: list[str] = []
    for module_name, names in DEMOTED_NAMES_BY_MODULE.items():
        heading = f"### `{module_name}`"
        if heading not in engine_surface:
            missing_engine.extend(f"{module_name}.{name}" for name in sorted(names))
            continue
        module_start = engine_surface.index(heading)
        next_heading = engine_surface.find("\n### `", module_start + len(heading))
        module_section = engine_surface[module_start:]
        if next_heading != -1:
            module_section = engine_surface[module_start:next_heading]
        missing_engine.extend(
            f"{module_name}.{name}"
            for name in sorted(names)
            if f"`{name}`" not in module_section
        )
    assert not missing_engine, (
        f"{path.name}: engine-surface coverage is missing {missing_engine}"
    )


def test_readme_headline_counts_match_reality() -> None:
    """T12 deliberately drops README headline counts instead of pinning copies.

    The old export/error/exception/test figures moved with the narrative and
    are no longer prose claims. This guard remains to prevent a stale count
    from quietly returning in the compact skeleton; the API references retain
    their own reality-based guards below.
    """
    readme = _read_readme()
    normalized = re.sub(r"[*_`]", "", readme)
    assert not re.search(
        r"\b[\d,]+\s+(?:exports?|tests?|error codes?|"
        r"exception (?:classes?|types?)|code lines?)\b",
        normalized,
        re.IGNORECASE,
    )


def _parse_api_reference_error_kinds(text: str) -> dict[str, str]:
    """Map each error code to the `### \\`kind\\`` heading it is filed under.

    The per-kind grouping is the only place the API references state a code's
    kind, so a code filed under the wrong heading makes the page contradict
    itself -- and `kind` travels on the MCP wire, where callers branch on it to
    decide whether retrying is worthwhile.
    """
    kinds: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        heading = re.match(r"^### `([a-z_]+)`\s*$", line.strip())
        if heading is not None:
            current = heading.group(1)
            continue
        row = re.match(r"^\| `([A-Z0-9_]+)` \| (.*?) \|$", line.strip())
        if row is not None and current is not None:
            kinds[row.group(1)] = current
    return kinds


@pytest.mark.parametrize("path", API_REFERENCES, ids=lambda p: p.name)
def test_api_reference_files_each_code_under_its_declared_kind(path: Path) -> None:
    """Catch a code drifting from the kind heading it is grouped under.

    The sibling test pins the SET of codes and each description, but not the
    heading a code sits beneath -- so reclassifying a code in `ERROR_CODES`
    left the tables filing it under the old kind, in both languages, with the
    prose in the same file already stating the new one.
    """
    filed = _parse_api_reference_error_kinds(path.read_text())
    drifted = {
        code: (under, ERROR_CODES[code].kind)
        for code, under in filed.items()
        if code in ERROR_CODES and under != ERROR_CODES[code].kind
    }

    # Without this, removing every `### kind` heading would leave `filed`
    # empty and the drift check would pass on nothing.
    assert set(filed) == set(ERROR_CODES)
    assert drifted == {}, f"{path.name}: codes filed under the wrong kind: {drifted}"


def test_no_doc_teaches_a_code_less_kind_class_construction() -> None:
    """No published sample constructs a kind class without naming its code.

    `code` became required in 0.6.0, so a sample that omits it does not just
    read wrong -- it raises `TypeError`, and inside an action handler that is
    not a `_KNOWN_ERRORS` match, so a clean precondition refusal reaches the
    consumer as an opaque INTERNAL_ERROR with the message suppressed. A
    published documentation sample shipped exactly that for one review round
    because the docs guards only regexed selected prose.
    """
    kind_classes = (
        "ActionError",
        "AuthorityError",
        "ConflictError",
        "IngestError",
        "InternalError",
        "OntaryError",
        "PermissionDenied",
        "PreconditionFailed",
        "ValidationFailed",
        "VisibilityError",
    )
    # Markup may sit between the keyword and the class name in rendered
    # documentation -- allow any run of tags and whitespace between them. An
    # earlier version of this pattern did
    # not, and passed the very sample it was written to catch.
    pattern = re.compile(
        r"(?:raise|=)(?:\s|</?[^>]+>)*("
        + "|".join(kind_classes)
        + r")(?:</?[^>]+>)*\("
    )
    paths = [
        *sorted(_DOCS.rglob("*.md")),
        Path(__file__).resolve().parent.parent / "README.md",
    ]
    assert len(paths) > 5, "doc scan found too few files to be meaningful"

    for path in paths:
        text = path.read_text()
        for match in pattern.finditer(text):
            # Scan to this call's own closing paren rather than a paragraph
            # window: the PRECONDITION_FAILED catalog row contains a literal
            # `code=` in its surrounding prose, so a window-based check let
            # the sample inside it lose its argument undetected.
            depth, index = 1, match.end()
            while index < len(text) and depth:
                depth += (text[index] == "(") - (text[index] == ")")
                index += 1
            assert depth == 0, (
                f"{path.name}: construction of {match.group(1)} has no "
                "balanced closing paren, so this guard cannot bound the call "
                "-- it would otherwise scan to EOF and be satisfied by any "
                "later `code=` in the file"
            )
            tail = text[match.end() : index - 1]
            assert "code=" in tail, (
                f"{path.name}: sample constructs {match.group(1)} without a "
                "code= argument, which raises TypeError as of 0.6.0"
            )
