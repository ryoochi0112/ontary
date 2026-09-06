"""Pins README/docs navigation and the documented SDK surface against code.

The compact T12 README links each reader-facing docs page, carries one
executable quickstart, and deliberately leaves detailed contracts to their
canonical pages. The cookbook's recipes are executable too. API-reference
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
from pydantic import BaseModel

import ontary
import ontary.connect
from ontary.declarations import Declarations
from ontary.errors import ERROR_CODES

README = Path(__file__).resolve().parent.parent / "README.md"
CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
_DOCS = Path(__file__).resolve().parent.parent / "docs"
API_REFERENCES = (_DOCS / "api-reference.md", _DOCS / "api-reference.ja.md")
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
        "OBJECT_ALREADY_ERASED",
        "OBJECT_RETIRE_NOT_FOUND",
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
# Codes that shipped in (or before) 0.8.0 and have since been removed from the
# catalogue. The two sets above are HISTORY: they record what those releases
# actually announced, and a released CHANGELOG section is never edited to match
# today's catalogue. Each cut task that deletes a code appends it here instead,
# which keeps the catalogue-diff assertion below honest in both directions --
# every live code is still accounted for, and nothing is quietly dropped.
REMOVED_SINCE_080_ERROR_CODES = frozenset(
    {
        "OBJECT_ERASURE_NOT_FOUND",
        "OBJECT_ALREADY_ERASED",
        "STORE_SCHEMA_INCOMPATIBLE",
        "EFFECT_NOT_DISPATCHABLE",
        "UNDECLARED_EFFECT",
        "EFFECT_NOT_SERIALIZABLE",
        "UPCAST_FAILED",
        "ONTOLOGY_DRIFT",
    }
)
NEW_ENGLISH_DOCS = tuple(
    _DOCS / name
    for name in (
        "storage.md",
        "connectors.md",
        "mcp-serving.md",
        "queries.md",
        "authority.md",
        "cookbook.md",
    )
)
READER_DOCS = tuple(
    sorted(
        path
        for path in _DOCS.rglob("*")
        if path.is_file() and path.suffix in {".html", ".md"}
    )
)
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_queries_document_visible_row_count_disclosure_reasoning() -> None:
    text = (_DOCS / "queries.md").read_text()

    assert "post-visibility" in text
    assert "can already enumerate the same rows" in text
    assert "deliberately not min-N-gated" in text
    assert "scoped away from every matching row receives `0`" in text
    assert "`count_contributors` remains the sole privacy-counting primitive" in text


def test_compatibility_describes_pypi_index() -> None:
    """Compatibility must describe PyPI as the index.

    A structural check rather than a prose snapshot: the compatibility
    section must make the positive PyPI claim and must not retain the
    pre-fork private-index claim.
    """
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
    "Finding",
    "InMemoryStore",
    "InternalError",
    "OntaryError",
    "ObjectStore",
    "OntologyClient",
    "Page",
    "PermissionDenied",
    "PostgresStore",
    "PreconditionFailed",
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
    "ontary.audit": {"CapabilityAccessRecord"},
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
    },
    "ontary.ontology": {"OntologyDef"},
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
    },
    "ontary.testing": {
        "FixedClock",
        "SequentialIds",
        "consumer",
        "make_store",
        "raises_code",
    },
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
        elif node.module == "ontary.testing":
            assert {alias.name for alias in node.names} <= {
                "FixedClock",
                "SequentialIds",
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

    # The released bullet is a historical record: it lists what 0.8.0 added,
    # including codes later cut. So it is compared against the historical set,
    # never against today's catalogue.
    assert sorted(listed_codes) == sorted(EXPECTED_080_NEW_ERROR_CODES)

    # Every code the catalogue holds today is still accounted for by one of the
    # historical sets, minus what has since been removed -- and nothing removed
    # is still live.
    assert REMOVED_SINCE_080_ERROR_CODES <= (
        EXPECTED_PRE_080_ERROR_CODES | EXPECTED_080_NEW_ERROR_CODES
    )
    assert not (REMOVED_SINCE_080_ERROR_CODES & set(ERROR_CODES))
    assert set(ERROR_CODES) == (
        EXPECTED_PRE_080_ERROR_CODES | EXPECTED_080_NEW_ERROR_CODES
    ) - REMOVED_SINCE_080_ERROR_CODES


def test_authority_page_names_every_declarations_field() -> None:
    section = (_DOCS / "authority.md").read_text()

    # Every field name (accounting for the min-N/min_n spelling) must have a
    # matching prose heading somewhere in the section.
    expected_headings = {
        "authority": "Authority",
        "capabilities": "Capabilities",
        "writeback": "Write-back",
        "reingest": "Re-ingest",
        "visibility_default": "Visibility default",
        "transaction_ownership": "Transaction ownership",
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
        # not hide a sibling public type such as ScopePolicy.
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
            # Callable/type aliases expose their argument types through
            # get_args(), not get_type_hints().
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
