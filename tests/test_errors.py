"""Tests for the error-code foundation (`ontary.errors`) and its retrofit
onto the store-layer exceptions."""

from __future__ import annotations

import ast
import copy
import pickle
from collections import Counter
from pathlib import Path

import pytest

import ontary
from ontary.actions import ActionError
from ontary.errors import (
    ERROR_CODES,
    AuthorityError,
    ConflictError,
    InternalError,
    OntaryError,
    PermissionDenied,
    PreconditionFailed,
    ValidationFailed,
    VisibilityError,
)

# Every retrofitted engine code, and the kind class that owns its taxonomy.
# The individual concrete exception defaults are guarded separately by the
# kind-hierarchy tests; this table keeps the catalog's code/kind contract
# exercised without naming those concrete classes.
STORE_EXCEPTION_CODES: dict[str, tuple[type[OntaryError], str]] = {
    "STORE_ERROR": (InternalError, "internal"),
    "STORE_BUSY": (ConflictError, "conflict"),
    "UNKNOWN_OBJECT_TYPE": (ValidationFailed, "validation"),
    "UNKNOWN_LINK_TYPE": (ValidationFailed, "validation"),
    "OBJECT_NOT_FOUND": (ValidationFailed, "validation"),
    "CARDINALITY_VIOLATION": (ConflictError, "conflict"),
    "INVALID_BATCH": (ValidationFailed, "validation"),
    "INVALID_CURSOR": (ValidationFailed, "validation"),
    "STORE_VERSION_UNSUPPORTED": (ConflictError, "conflict"),
    "INVALID_RECORD": (ValidationFailed, "validation"),
    "ONTOLOGY_DRIFT": (ConflictError, "conflict"),
    "STORE_SCHEMA_INCOMPATIBLE": (ConflictError, "conflict"),
    "AUTHORITY_ERROR": (AuthorityError, "authority"),
    "SOURCE_CREATE_REFUSED": (AuthorityError, "authority"),
    "UNDECLARED_SOURCE_WRITE": (AuthorityError, "authority"),
    "UNDECLARED_SOURCE_REMOVAL": (AuthorityError, "authority"),
    "CALLER_TRANSACTION_REFUSED": (ConflictError, "conflict"),
}

# Engine-level codes use the kind class appropriate to their public failure
# family. ActionError remains the handler-facing precondition vocabulary.
ENGINE_EXCEPTION_CODES: dict[str, tuple[type[OntaryError], str]] = {
    "PRECONDITION_FAILED": (PreconditionFailed, "precondition"),
    "PERMISSION_DENIED": (PermissionDenied, "permission"),
    "SCOPE_DENIED": (PermissionDenied, "permission"),
    "MIN_N_VIOLATION": (VisibilityError, "visibility"),
    "VISIBILITY_DENIED": (VisibilityError, "visibility"),
    "SCOPE_POLICY_ERROR": (ValidationFailed, "validation"),
    "FUNCTION_ERROR": (PreconditionFailed, "precondition"),
    "ONTOLOGY_INVALID": (ValidationFailed, "validation"),
    "UNDECLARED_CAPABILITY": (ValidationFailed, "validation"),
    "CAPABILITY_NOT_PROVIDED": (PreconditionFailed, "precondition"),
    "EFFECT_NOT_DISPATCHABLE": (PreconditionFailed, "precondition"),
    "UNDECLARED_EFFECT": (ValidationFailed, "validation"),
    "EFFECT_NOT_SERIALIZABLE": (ValidationFailed, "validation"),
    "UPCAST_FAILED": (ConflictError, "conflict"),
    "UNKNOWN_FIELD": (ValidationFailed, "validation"),
    "UNKNOWN_NAME": (ValidationFailed, "validation"),
    "INVALID_LIMIT": (ValidationFailed, "validation"),
    "AFTER_WITHOUT_LIMIT": (ValidationFailed, "validation"),
    "STALE_CURSOR": (ValidationFailed, "validation"),
    "INVALID_GROUP_BY": (ValidationFailed, "validation"),
    "NON_NUMERIC_AGGREGATE": (ValidationFailed, "validation"),
    "UNKNOWN_OPERATOR": (ValidationFailed, "validation"),
    "OPERATOR_TYPE_MISMATCH": (ValidationFailed, "validation"),
    "ENTITY_KEY_MISMATCH": (ValidationFailed, "validation"),
}

# T4 (multi-consumer-mcp): the two new permission-refusal codes the
# not-yet-built multi-consumer MCP server (T6) will raise -- registered here
# ahead of T6 so ERROR_CODES/README/API references are accurate before the
# raise sites exist. Neither class is constructed anywhere outside this test
# module yet; T6 is the only future consumer.
MCP_AUTH_EXCEPTION_CODES: dict[str, tuple[type[OntaryError], str]] = {
    "UNAUTHENTICATED": (PermissionDenied, "permission"),
    "CONSUMER_UNRESOLVED": (PermissionDenied, "permission"),
}

# These catalog entries are deliberately not emitted by a coded raise site:
# two are kind-level fallback codes retained for callers that construct the
# base kind, one is an ingest report code, and one is the MCP generic envelope.
# They remain in the catalog because they are stable wire/report values.
NON_RAISED_CODES: set[tuple[str, str]] = {
    ("STORE_ERROR", "internal"),
    ("AUTHORITY_ERROR", "authority"),
    ("OWNED_PROPERTY_REFUSED", "authority"),
    ("INTERNAL_ERROR", "internal"),
}


@pytest.mark.parametrize("code, spec", list(STORE_EXCEPTION_CODES.items()))
def test_store_code_maps_to_its_kind(
    code: str, spec: tuple[type[OntaryError], str]
) -> None:
    kind_cls, expected_kind = spec
    instance = kind_cls("boom", code=code)
    assert instance.code == code
    assert instance.kind == expected_kind


@pytest.mark.parametrize("code, spec", list(STORE_EXCEPTION_CODES.items()))
def test_store_code_registered_in_catalog(
    code: str, spec: tuple[type[OntaryError], str]
) -> None:
    _kind_cls, expected_kind = spec
    assert code in ERROR_CODES
    assert ERROR_CODES[code].kind == expected_kind


@pytest.mark.parametrize("code, spec", list(ENGINE_EXCEPTION_CODES.items()))
def test_engine_code_maps_to_its_kind(
    code: str, spec: tuple[type[OntaryError], str]
) -> None:
    kind_cls, expected_kind = spec
    instance = kind_cls("boom", code=code)
    assert instance.code == code
    assert instance.kind == expected_kind


@pytest.mark.parametrize("code, spec", list(ENGINE_EXCEPTION_CODES.items()))
def test_engine_code_registered_in_catalog(
    code: str, spec: tuple[type[OntaryError], str]
) -> None:
    _kind_cls, expected_kind = spec
    assert code in ERROR_CODES
    assert ERROR_CODES[code].kind == expected_kind


@pytest.mark.parametrize("code, spec", list(MCP_AUTH_EXCEPTION_CODES.items()))
def test_mcp_auth_code_maps_to_its_kind(
    code: str, spec: tuple[type[OntaryError], str]
) -> None:
    kind_cls, expected_kind = spec
    instance = kind_cls("boom", code=code)
    assert instance.code == code
    assert instance.kind == expected_kind


@pytest.mark.parametrize("code, spec", list(MCP_AUTH_EXCEPTION_CODES.items()))
def test_mcp_auth_code_registered_in_catalog(
    code: str, spec: tuple[type[OntaryError], str]
) -> None:
    _kind_cls, expected_kind = spec
    assert code in ERROR_CODES
    assert ERROR_CODES[code].kind == expected_kind


def test_unauthenticated_and_consumer_unresolved_describe_different_fixes() -> None:
    """Spec multi-consumer-mcp AC4/AC5: two codes, not one, because the two
    fixes differ -- a missing credential vs. a missing mapping. Mutation-
    tested: if the two descriptions were ever reworded to say the same
    generic thing (e.g. both just "check identity"), this fails."""
    unauthenticated = ERROR_CODES["UNAUTHENTICATED"].description
    consumer_unresolved = ERROR_CODES["CONSUMER_UNRESOLVED"].description
    assert unauthenticated != consumer_unresolved
    assert "configure authentication" in unauthenticated
    assert "map this principal" in consumer_unresolved
    # Neither description tells the other's fix.
    assert "configure authentication" not in consumer_unresolved
    assert "map this principal" not in unauthenticated


def test_forward_reference_to_unbuilt_server_is_laundered_once_it_exists() -> None:
    """The UNAUTHENTICATED/CONSUMER_UNRESOLVED descriptions say "not yet
    built" because, at authoring time, `build_multi_consumer_mcp_server`
    does not exist. That phrase ships verbatim into README.md and both
    api-reference docs via the table-sync tests, which will happily keep a
    published lie in sync forever. This is the tripwire: the day T6 lands
    `build_multi_consumer_mcp_server` on `ontary`, this test starts
    failing until every "not yet built" is removed from `ERROR_CODES`.

    T6 author: once `build_multi_consumer_mcp_server` exists, reword the
    UNAUTHENTICATED and CONSUMER_UNRESOLVED descriptions in
    `src/ontary/errors.py` to drop "not yet built -- see AC4/AC5" (the
    server is built now; AC4/AC5 no longer need flagging as pending), then
    re-run `make verify` so README.md and the api-reference docs pick up
    the reworded, synced descriptions.
    """
    import ontary

    if hasattr(ontary.mcp_server, "build_multi_consumer_mcp_server"):
        for code, info in ERROR_CODES.items():
            assert "not yet built" not in info.description, (
                f"ERROR_CODES[{code!r}] still says 'not yet built', but "
                "ontary.build_multi_consumer_mcp_server now exists -- "
                "reword this description in src/ontary/errors.py to drop "
                "the forward reference (see this test's docstring), then "
                "re-run `make verify` to resync README.md and the "
                "api-reference docs."
            )


def test_ontary_error_retains_an_explicit_code_verbatim() -> None:
    """An explicit code is stored verbatim, and the catalog supplies `kind`.

    Renamed and rewritten in 0.6.0. It used to assert message-only
    construction with code/kind coming from a class default -- both halves
    of which this release deleted, the default being the bug. A docstring
    still describing it would tell the next maintainer to reinstate it.
    """
    exc = ValidationFailed(
        "unregistered object type: 'widget'", code="UNKNOWN_OBJECT_TYPE"
    )
    assert str(exc) == "unregistered object type: 'widget'"
    assert exc.code == "UNKNOWN_OBJECT_TYPE"


def test_ontary_error_allows_explicit_code_override() -> None:
    exc = InternalError("custom", code="CUSTOM_CODE")
    assert exc.code == "CUSTOM_CODE"


def test_author_supplied_action_error_code_overrides_default() -> None:
    """AC7: an author can attach their own stable code to a precondition
    failure; it need not be registered in ERROR_CODES (that catalog covers
    engine codes, not every author-defined one)."""
    exc = ActionError("gap not acknowledged", code="GAP_NOT_ACKNOWLEDGED")
    assert exc.code == "GAP_NOT_ACKNOWLEDGED"
    assert exc.kind == "precondition"


def test_package_exports_declared_contracts_public_surface() -> None:
    """The curated root keeps authoring/runtime entries; engine contracts stay
    at their canonical defining modules after the T10 surface break."""
    import ontary
    from ontary.declarations import declarations
    from ontary.errors import ERROR_CODES, ErrorCodeInfo, Kind
    from ontary.store import WriteRecord

    assert getattr(Kind, "__args__", ()) == (
        "visibility",
        "permission",
        "precondition",
        "validation",
        "authority",
        "conflict",
        "internal",
    )
    assert isinstance(ErrorCodeInfo, type)
    assert isinstance(ERROR_CODES, dict)
    assert callable(declarations)
    assert isinstance(WriteRecord, type)
    assert ontary.declarations is declarations
    assert ontary.AuthorityError.__module__ == "ontary.errors"
    assert {"Kind", "ErrorCodeInfo", "ERROR_CODES", "WriteRecord"}.isdisjoint(
        set(ontary.__all__)
    )


def _source_code_literals() -> set[str]:
    """Collect literal ``code=`` values used by the engine source.

    The old guard walked exception class defaults.  That made deleting a
    legacy class look like deleting its code, even though the code still
    appeared at a real raise site.  Source inspection follows the actual
    behavior instead: every literal passed through a constructor/helper must
    remain registered in ``ERROR_CODES``.  The four intentional non-raise
    values are checked separately below.
    """
    source_root = Path(__file__).resolve().parent.parent / "src" / "ontary"
    codes: set[str] = set()
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "code"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    codes.add(keyword.value.value)
    return codes


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    """Map every ``import ... as alias`` binding back to the original name.

    Without this, ``from ontary.errors import ValidationFailed as _VF`` hides a
    kind class from both the hierarchy walk and the raise-site scan: the base
    (or the callee) reads as ``_VF``, which matches nothing.  One aliased
    import would be a cheaper escape than the check it bypasses.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for alias in node.names:
            if alias.asname is not None:
                aliases[alias.asname] = alias.name.rpartition(".")[2]
    return aliases


def _mentioned_name(node: ast.AST) -> str | None:
    """The class name a node mentions, however it mentions it.

    A rebinding can name its class directly (`ValidationFailed`), through a
    module (`errors.ValidationFailed`) or as a string a `getattr` resolves at
    runtime -- so the laundering tripwire has to see all three.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _static_name(node: ast.expr) -> str | None:
    """The class name a base or callee expression names, if it names one.

    ``ValidationFailed`` and ``errors.ValidationFailed`` name the same class:
    the module qualifier carries no extra meaning for this guard, so both
    resolve to the bare attribute name.  Anything with no static name at all
    (``_load_fastmcp()(...)``) returns ``None`` for the caller to fail closed
    on -- never to skip silently.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


_NO_MESSAGE = "<no positional message>"
_NO_CODE = "<no code= keyword>"

# A raise site's identity, in the order a reader resolves it: which file,
# which enclosing scope, which kind class, which code, which message. Every
# component is stable under insertions elsewhere in the file; no component
# is an absolute line number.
_Site = tuple[str, str, str, str, str]


def _call_qualnames(tree: ast.Module) -> dict[ast.Call, str]:
    """Map every call in ``tree`` to the dotted name of the scope enclosing it.

    This is the stable half of a raise site's identity, and it replaces the
    absolute line number this guard used to key on.  A line number is
    falsified by any insertion ABOVE it, in a file the inserter need not have
    read: five lines added to `client.py` at `ebd984e` moved four allowlist
    entries at once, and the resulting red accused an untouched raise site of
    a taxonomy violation instead of naming the stale entry.  An enclosing
    name survives every insertion, and moves only when the code it names
    actually moves.

    Decorators, defaults and base-class expressions are evaluated in the
    OUTER scope, so only a ``body`` re-scopes; anything else keeps the name
    it was reached with.
    """
    qualnames: dict[ast.Call, str] = {}

    def visit(node: ast.AST, enclosing: str) -> None:
        if isinstance(node, ast.Call):
            qualnames[node] = enclosing
        body_scope = enclosing
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body_scope = f"{enclosing}.{node.name}" if enclosing else node.name
        for field, value in ast.iter_fields(node):
            inner = body_scope if field == "body" else enclosing
            children = value if isinstance(value, list) else [value]
            for child in children:
                if isinstance(child, ast.AST):
                    visit(child, inner)

    visit(tree, "")
    return qualnames


def _call_code_expr(node: ast.Call) -> str:
    """The raise site's ``code=`` argument, as source text.

    Part of a site's identity, not just something the guard checks: six
    `IngestError` report rows share `bulk_upsert` and none carries a
    positional message, so file, scope, class and message alone name all six
    at once.  The code is what separates them.
    """
    for keyword in node.keywords:
        if keyword.arg == "code":
            return ast.unparse(keyword.value)
    return _NO_CODE


def _call_message(node: ast.Call) -> str:
    """The raise site's first positional argument, as source text.

    The enclosing name alone is not an identity: `_resolve_handler` raises
    ``ActionError(code="UNKNOWN_ACTION")`` twice, and
    `_api_name_for_action_params` does too.  Keyed on name and code alone
    those pairs merge, which would let one allowlist entry cover a raise it
    was never reviewed against -- and let a reordering swap the two while the
    guard stayed green.  The message is what actually distinguishes them, so
    it is part of the key.
    """
    return ast.unparse(node.args[0]) if node.args else _NO_MESSAGE


_INGEST_REPORT_ROW_CLASS = "IngestError"
_INGEST_REPORT_ROW_KIND = "<catalogued-per-record-kind>"
_INGEST_REPORT_ROW_KINDS = frozenset({"validation", "authority", "conflict"})


def _source_kind_classes() -> dict[str, str]:
    """Discover kind classes and effective ``kind`` or named category values.

    This is deliberately source-derived rather than a list of imported
    classes: a new ``OntaryError`` subclass, including one in another engine
    module, must automatically become part of the raise-site guard.
    """
    source_root = Path(__file__).resolve().parent.parent / "src" / "ontary"
    bases_by_class: dict[str, set[str]] = {}
    own_kinds: dict[str, str] = {}

    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        aliases = _import_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases: set[str] = set()
            for base in node.bases:
                name = _static_name(base)
                if name is None:
                    # The existing Generic[...] bases belong to model/helper
                    # classes and cannot participate in the error hierarchy.
                    # Every other unnameable base must fail closed: silently
                    # dropping it could hide a new kind class.
                    assert (
                        isinstance(base, ast.Subscript)
                        and isinstance(base.value, ast.Name)
                        and base.value.id == "Generic"
                    ), (
                        f"{path.relative_to(source_root)}:{node.lineno}: class "
                        f"{node.name} has unsupported non-name base "
                        f"{ast.unparse(base)!r}"
                    )
                    bases.add(base.value.id)
                    continue
                # An aliased base resolves to the class it actually is, so
                # `class X(_VF)` still joins the hierarchy under its real name.
                bases.add(aliases.get(name, name))
            bases_by_class[node.name] = bases
            for statement in node.body:
                if isinstance(statement, ast.Assign):
                    targets = statement.targets
                    value = statement.value
                elif isinstance(statement, ast.AnnAssign):
                    targets = [statement.target]
                    value = statement.value
                else:
                    continue
                if (
                    value is not None
                    and any(
                        isinstance(target, ast.Name) and target.id == "kind"
                        for target in targets
                    )
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    own_kinds[node.name] = value.value

    root_name = OntaryError.__name__
    kind_class_names: set[str] = set()
    while True:
        newly_found = {
            name
            for name, bases in bases_by_class.items()
            if name not in kind_class_names
            and (
                root_name in bases
                or any(base in kind_class_names for base in bases)
            )
        }
        if not newly_found:
            break
        kind_class_names.update(newly_found)

    # Named, exact report-row category: IngestError carries the kind
    # catalogued for each row's code, spanning the categories in
    # `_INGEST_REPORT_ROW_KINDS`.
    # Only the single-kind equality rule is inapplicable.
    assert _INGEST_REPORT_ROW_CLASS in kind_class_names
    assert _INGEST_REPORT_ROW_CLASS not in own_kinds
    own_kinds[_INGEST_REPORT_ROW_CLASS] = _INGEST_REPORT_ROW_KIND

    def effective_kind(class_name: str) -> str:
        if class_name in own_kinds:
            return own_kinds[class_name]
        for base in bases_by_class[class_name]:
            if base == root_name or base in kind_class_names:
                return effective_kind(base)
        raise AssertionError(
            f"{class_name} is an OntaryError subclass without an effective kind"
        )

    effective_kinds = {
        name: effective_kind(name) for name in kind_class_names
    }
    assert {
        name
        for name, kind in effective_kinds.items()
        if kind == _INGEST_REPORT_ROW_KIND
    } == {_INGEST_REPORT_ROW_CLASS}, (
        "the multi-kind ingest report-row category must remain exact to "
        f"{_INGEST_REPORT_ROW_CLASS}"
    )
    return effective_kinds


def test_kind_class_raise_sites_use_literal_catalog_codes() -> None:
    """Every kind-class construction names a catalog code of the same kind.

    "Construction" is the bug CLASS, not one syntax: a bare ``ValidationFailed
    (...)``, a qualified ``errors.ValidationFailed(...)`` and an aliased
    ``_VF(...)`` are all the same raise site, and each is resolved to the same
    class here.  A callee with no static name at all fails closed against
    ``allowed_unnameable_callees`` rather than being skipped.

    The four allowlists are exact source SITES (or, for the loader
    indirection, exact callee names), not class-name exemptions. A site is
    `_Site`: file, enclosing scope, class, `code=` expression, first
    positional message. It is deliberately not a line number. A line number
    is falsified by any insertion above it -- `ebd984e` added five lines to
    `client.py` and moved four entries at once -- and the red that follows
    accuses an untouched raise of a taxonomy violation rather than naming the
    stale entry. It is also not unique on its own: keyed on scope and code
    alone, `_resolve_handler`'s two UNKNOWN_ACTION raises merge, and so do
    `_api_name_for_action_params`'s, which would let one reviewed entry cover
    an unreviewed raise and let a reorder swap the two while this stayed
    green. Message and code together separate them.

    The named IngestError report-row category is the one multi-kind category;
    its codes must be in `_INGEST_REPORT_ROW_KINDS`. It adds exact forwarding
    and batch sites; every other IngestError site retains the literal and
    catalog assertions below, with only single-kind equality inapplicable.
    """
    source_root = Path(__file__).resolve().parent.parent / "src" / "ontary"
    kind_classes = _source_kind_classes()

    allowed_forwarding_sites: set[_Site] = {
        # ActionExecutor._deny forwards the caller's PERMISSION_DENIED or
        # SCOPE_DENIED code through the shared, identically audited helper.
        (
            "actions.py",
            "ActionExecutor._deny",
            "PermissionDenied",
            "code",
            "message",
        ),
    }
    allowed_kind_mismatches: set[_Site] = {
        # ActionExecutor._register reports an unregistered handler target as
        # the catalogued validation-kind UNKNOWN_ACTION code.
        (
            "actions.py",
            "ActionExecutor._register",
            "ActionError",
            "'UNKNOWN_ACTION'",
            "f'cannot register handler for unregistered action: {api_name!r}'",
        ),
        # ActionExecutor._resolve_handler reports an unregistered action as
        # UNKNOWN_ACTION; the handler-facing ActionError class remains
        # precondition-kind.
        (
            "actions.py",
            "ActionExecutor._resolve_handler",
            "ActionError",
            "'UNKNOWN_ACTION'",
            "f'unregistered action: {action_name!r}'",
        ),
        # ActionExecutor._resolve_handler reports a missing handler as
        # UNKNOWN_ACTION for the same deliberate ActionError/validation
        # taxonomy split.  Its sibling above shares this file, scope, class
        # and code -- only the message tells the two apart.
        (
            "actions.py",
            "ActionExecutor._resolve_handler",
            "ActionError",
            "'UNKNOWN_ACTION'",
            "f'no handler registered for action: {action_name!r}'",
        ),
        # OntologyClient._api_name_for_action_params rejects an undecorated
        # typed action as UNKNOWN_ACTION; the stable code is catalogued
        # validation-kind by design.
        (
            "client.py",
            "OntologyClient._api_name_for_action_params",
            "ActionError",
            "'UNKNOWN_ACTION'",
            "f'{cls.__name__!r} is not decorated with @ontology.action(...)"
            " -- it has no registered action api_name'",
        ),
        # OntologyClient._api_name_for_action_params rejects an action
        # declared on another ontology with the same deliberate
        # UNKNOWN_ACTION validation code.  Its sibling above shares this
        # file, scope, class and code -- only the message tells the two
        # apart.
        (
            "client.py",
            "OntologyClient._api_name_for_action_params",
            "ActionError",
            "'UNKNOWN_ACTION'",
            'f"{cls.__name__!r} (api_name {name!r}) was declared on a different'
            ' Ontology -- it is not part of this client\'s ontology"',
        ),
        # ActionExecutor._error reports parameter-shape refusal as the
        # catalogued validation-kind INVALID_PARAMS code.
        (
            "actions.py",
            "ActionExecutor._error",
            "ActionError",
            "'INVALID_PARAMS'",
            "message",
        ),
    }
    allowed_unnameable_callees: set[str] = {
        # `_load_fastmcp()(...)` builds the FastMCP server through the lazy
        # loader, so the callee is a call rather than a name. A loader
        # indirection cannot name a kind class -- but any OTHER unnameable
        # callee must fail this guard rather than slip through it.
        "_load_fastmcp",
    }
    ingest_report_row_forwarding_sites: set[_Site] = {
        # bulk_upsert forwards the `(code, reason)` returned by its record
        # validator into a report row; both keywords and the exact forwarded
        # expression stay pinned here, in the key.
        ("ingest.py", "bulk_upsert", "IngestError", "code", _NO_MESSAGE),
        # _map_objects remaps the index of an already-validated ingest report
        # row while forwarding that row's code unchanged.
        (
            "connect/mapper.py",
            "_map_objects",
            "IngestError",
            "e.code",
            _NO_MESSAGE,
        ),
    }
    ingest_batch_report_sites: set[_Site] = {
        # These are the two client raising-policy boundaries. Their code is
        # derived by IngestError from the first already-guarded report row.
        (
            "client.py",
            "OntologyClient.ingest",
            "IngestError",
            _NO_CODE,
            _NO_MESSAGE,
        ),
        (
            "client.py",
            "OntologyClient.ingest_links",
            "IngestError",
            _NO_CODE,
            _NO_MESSAGE,
        ),
    }
    # Counted, not collected into a set: a `_Site` is an identity, so two
    # sites answering to one entry means that entry covers a raise nobody
    # reviewed it against. Set membership cannot tell one match from two.
    seen_forwarding_sites: Counter[_Site] = Counter()
    seen_kind_mismatches: Counter[_Site] = Counter()
    seen_unnameable_callees: set[str] = set()
    seen_ingest_report_row_forwarding_sites: Counter[_Site] = Counter()
    seen_ingest_batch_report_sites: Counter[_Site] = Counter()

    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        aliases = _import_aliases(tree)
        qualnames = _call_qualnames(tree)
        relative_path = path.relative_to(source_root).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _static_name(node.func)
            if name is None:
                inner = node.func
                assert (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id in allowed_unnameable_callees
                ), (
                    f"{relative_path}:{node.lineno}: callee "
                    f"{ast.unparse(node.func)!r} has no static name, so this "
                    "guard cannot constrain what it constructs"
                )
                seen_unnameable_callees.add(inner.func.id)
                continue
            class_name = aliases.get(name, name)
            expected_kind = kind_classes.get(class_name)
            if expected_kind is None:
                continue

            site: _Site = (
                relative_path,
                qualnames[node],
                class_name,
                _call_code_expr(node),
                _call_message(node),
            )
            code_keywords = [
                keyword for keyword in node.keywords if keyword.arg == "code"
            ]
            if expected_kind == _INGEST_REPORT_ROW_KIND:
                keyword_names = [keyword.arg for keyword in node.keywords]
                if site in ingest_batch_report_sites:
                    assert not node.args, (
                        f"ingest batch-report site {site} must use no positional args"
                    )
                    assert keyword_names == ["report"], (
                        f"ingest batch-report site {site} must retain exactly "
                        "one report= keyword"
                    )
                    assert ast.unparse(node.keywords[0].value) == "report", (
                        f"ingest batch-report site {site} changed its report value"
                    )
                    seen_ingest_batch_report_sites[site] += 1
                    continue

                assert not node.args, (
                    f"ingest report-row site {site} must use no positional args"
                )
                assert keyword_names == ["index", "reason", "code"], (
                    f"ingest report-row site {site} must retain exactly "
                    "index=, reason=, and code= keywords"
                )
                if site in ingest_report_row_forwarding_sites:
                    assert len(code_keywords) == 1, (
                        f"ingest forwarding site {site} must retain one code= keyword"
                    )
                    seen_ingest_report_row_forwarding_sites[site] += 1
                    continue

            if site in allowed_forwarding_sites:
                assert len(code_keywords) == 1, (
                    f"allowlisted forwarding site {site} must retain one code= "
                    "keyword"
                )
                assert isinstance(code_keywords[0].value, ast.Name), (
                    f"allowlisted forwarding site {site} must forward a variable"
                )
                seen_forwarding_sites[site] += 1
                continue

            assert len(code_keywords) == 1, (
                f"{relative_path}:{node.lineno}: call {class_name} must have "
                "exactly one literal code= keyword"
            )
            code_value = code_keywords[0].value
            assert isinstance(code_value, ast.Constant) and isinstance(
                code_value.value, str
            ), (
                f"{relative_path}:{node.lineno}: call {class_name} must use a "
                "literal string code= value"
            )
            code = code_value.value
            assert code in ERROR_CODES, (
                f"{relative_path}:{node.lineno}: call {class_name} uses "
                f"unregistered code {code!r}"
            )
            catalog_kind = ERROR_CODES[code].kind
            if site in allowed_kind_mismatches:
                assert catalog_kind != expected_kind, (
                    f"allowlisted mismatch {site} is no longer a "
                    f"mismatch: catalog kind is {catalog_kind!r}"
                )
                seen_kind_mismatches[site] += 1
                continue
            if expected_kind == _INGEST_REPORT_ROW_KIND:
                # Literal IngestError report rows still passed the unchanged
                # literal/registered-code assertions above. Their codes span
                # three catalogued kinds, so only single-kind equality does not
                # apply to this named category.
                assert catalog_kind in _INGEST_REPORT_ROW_KINDS, (
                    f"{relative_path}:{node.lineno}: IngestError report row uses "
                    f"{code!r} of kind {catalog_kind!r}; report rows span only "
                    f"{_INGEST_REPORT_ROW_KINDS!r}"
                )
                continue
            assert catalog_kind == expected_kind, (
                f"{relative_path}:{node.lineno}: call {class_name} has kind "
                f"{expected_kind!r}, but ERROR_CODES[{code!r}].kind is "
                f"{catalog_kind!r}. If this split is deliberate, allowlist it "
                f"as {site!r}"
            )

    assert seen_forwarding_sites == Counter(allowed_forwarding_sites), (
        "allowlisted forwarding sites must remain exact, present and unambiguous: "
        f"missing={sorted(allowed_forwarding_sites - set(seen_forwarding_sites))}, "
        f"unexpected={sorted(set(seen_forwarding_sites) - allowed_forwarding_sites)}, "
        f"duplicated={sorted(s for s, n in seen_forwarding_sites.items() if n > 1)}"
    )
    assert seen_kind_mismatches == Counter(allowed_kind_mismatches), (
        "allowlisted kind mismatches must remain exact, present and unambiguous: "
        f"missing={sorted(allowed_kind_mismatches - set(seen_kind_mismatches))}, "
        f"unexpected={sorted(set(seen_kind_mismatches) - allowed_kind_mismatches)}, "
        f"duplicated={sorted(s for s, n in seen_kind_mismatches.items() if n > 1)}"
    )
    assert seen_unnameable_callees == allowed_unnameable_callees, (
        "allowlisted unnameable callees must remain exact and present: "
        f"missing={allowed_unnameable_callees - seen_unnameable_callees}, "
        f"unexpected={seen_unnameable_callees - allowed_unnameable_callees}"
    )
    assert seen_ingest_report_row_forwarding_sites == Counter(ingest_report_row_forwarding_sites), (
        "ingest report-row forwarding sites must remain exact, present and unambiguous: "
        f"missing={sorted(ingest_report_row_forwarding_sites - set(seen_ingest_report_row_forwarding_sites))}, "
        f"unexpected={sorted(set(seen_ingest_report_row_forwarding_sites) - ingest_report_row_forwarding_sites)}, "
        f"duplicated={sorted(s for s, n in seen_ingest_report_row_forwarding_sites.items() if n > 1)}"
    )
    assert seen_ingest_batch_report_sites == Counter(ingest_batch_report_sites), (
        "ingest batch-report sites must remain exact, present and unambiguous: "
        f"missing={sorted(ingest_batch_report_sites - set(seen_ingest_batch_report_sites))}, "
        f"unexpected={sorted(set(seen_ingest_batch_report_sites) - ingest_batch_report_sites)}, "
        f"duplicated={sorted(s for s, n in seen_ingest_batch_report_sites.items() if n > 1)}"
    )


_IDENTITY_FIXTURE = """
class C:
    @deco(ValidationFailed("decorator", code="ONTOLOGY_INVALID"))
    def m(self, a):
        if a:
            raise ActionError("first", code="UNKNOWN_ACTION")
        raise ActionError("second", code="UNKNOWN_ACTION")


def top():
    raise ActionError("first", code="UNKNOWN_ACTION")


def rows(i):
    return [
        IngestError(index=i, reason="r", code="UNKNOWN_OBJECT_TYPE"),
        IngestError(index=i, reason="r", code="OWNED_TYPE_REFUSED"),
    ]
"""


def _identity_keys(source: str) -> list[_Site]:
    """Every call in `source`, as the `_Site` identity the guard keys on."""
    tree = ast.parse(source)
    qualnames = _call_qualnames(tree)
    sites: list[_Site] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _static_name(node.func)
        if name is None:
            continue
        sites.append(
            ("f.py", qualnames[node], name, _call_code_expr(node), _call_message(node))
        )
    return sites


def test_raise_site_identity_survives_insertion_and_separates_siblings() -> None:
    """A raise site's identity does not move when unrelated lines do.

    This is the input the previous fixture could not construct at all. The
    guard keyed sites by absolute line number, so "the same site, after an
    insertion above it" had no expressible identity: `ebd984e` added five
    lines to `client.py` and four allowlist entries went stale at once, each
    reporting a taxonomy violation at a raise nobody had touched. Nothing
    could assert the property, because the key WAS the line number.

    The three assertions below are the three ways that key failed, and each
    reds against a plausible weaker key:
      - insertion-invariance reds if identity goes back to line numbers;
      - sibling separation reds if identity drops the message, which is the
        key F2 originally proposed (enclosing name plus code) and which
        merges the two raises `_resolve_handler` and
        `_api_name_for_action_params` each make;
      - code separation reds if identity drops the code expression, which
        merges `bulk_upsert`'s six message-less report rows;
      - outer-scope decorators red if a `def` is treated as re-scoping
        anything but its own body.
    """
    keys = _identity_keys(_IDENTITY_FIXTURE)
    shifted = _identity_keys("\n\n\n\n\n" + _IDENTITY_FIXTURE)
    assert keys == shifted, (
        "a raise site's identity must not change when lines are inserted "
        "above it -- that is the whole defect an absolute line number has"
    )

    siblings = [k for k in keys if k[1] == "C.m" and k[2] == "ActionError"]
    assert len(siblings) == 2, siblings
    assert siblings[0][:4] == siblings[1][:4], (
        "the two siblings must agree on file, scope, class and code -- "
        "otherwise this fixture is not testing the collision it claims to"
    )
    assert siblings[0] != siblings[1], (
        "two raises sharing a scope, a class and a code must still be "
        "separable, or one allowlist entry silently covers both"
    )

    decorator = [k for k in keys if k[2] == "ValidationFailed"]
    assert [k[1] for k in decorator] == ["C"], (
        "a decorator expression is evaluated in the enclosing scope, not in "
        f"the body of the def it decorates: {decorator}"
    )

    same_message_other_scope = [k for k in keys if k[1] == "top"]
    assert same_message_other_scope[0] not in siblings, (
        "an identical raise in another scope must be a different site"
    )

    # `bulk_upsert`'s shape: report rows carry no positional message, so the
    # code is the only thing separating them. Without it in the key, one
    # forwarding entry answers for every row in the function.
    rows = [k for k in keys if k[2] == "IngestError"]
    assert len(rows) == 2, rows
    assert rows[0][:3] == rows[1][:3], rows
    assert rows[0][4] == rows[1][4] == _NO_MESSAGE, rows
    assert rows[0] != rows[1], (
        "two raises sharing a scope, a class and a message must still be "
        "separable by their code, or one entry silently covers both"
    )


def test_every_kind_class_requires_an_explicit_code() -> None:
    """A kind class cannot be constructed without naming its code.

    This is what retired two source-level guards. They existed to catch a
    construction reached through a rebound name -- an alias, an assignment,
    a parameter, a `for` target, a `getattr` -- which silently shipped the
    class-default code in place of the intended one. Five review rounds
    established that no source scan closes that in a language where any name
    can be rebound; each round found one more binding shape.

    Removing the default makes the wrong construction unwriteable instead of
    undetectable, and the binding shape stops mattering: however the class is
    reached, omitting `code` raises here rather than shipping the wrong
    stable code to a consumer branching on it.

    Driven over the exported taxonomy rather than one class, so a NEW kind
    class inherits the requirement or fails here.
    """
    exported = [
        value
        for name in ontary.__all__
        if isinstance(value := getattr(ontary, name), type)
        and issubclass(value, OntaryError)
    ]
    assert len(exported) >= 8, "kind-class discovery returned too little to pin"

    for exception in [*exported, ActionError]:
        with pytest.raises(TypeError):
            exception("message")  # type: ignore[call-arg]
        with pytest.raises(ValueError):
            exception("message", code="")

        # ...and an explicit registered code still adopts its catalogued kind.
        instance = exception("message", code="STORE_BUSY")
        assert instance.code == "STORE_BUSY"
        assert instance.kind == ERROR_CODES["STORE_BUSY"].kind


def test_no_source_assignment_launders_a_kind_class() -> None:
    """No assignment under `src/ontary` rebinds or names a kind class.

    Scope is exactly that -- `ast.Assign` and `ast.AnnAssign` -- and the
    claim is not made any wider, because it was: an earlier version of this
    docstring said it forbade the laundering *capability*, and a function
    parameter, a `for` target and a comprehension all walked past it.
    `test_every_raise_resolves_to_a_known_class` is what covers those, by
    refusing an unresolvable name at the `raise` itself.

    Requiring `code` at construction (0.6.0) closed the OMITTED-code hole
    for every binding shape, and retired the raise-site resolver. It did not
    close this one: a *wrong but catalogued* code laundered through a rebound
    name is still writeable, and a review sweep of all 107 construction sites
    found ten that survive both verify arms without this test -- one of which
    turns a validation refusal into a bare INTERNAL_ERROR on the MCP wire.
    So this stays. Retiring it alongside the resolver was a regression.
    """
    source_root = Path(__file__).resolve().parent.parent / "src" / "ontary"
    guarded_names = set(_source_kind_classes()) | {OntaryError.__name__}
    assert len(guarded_names) > 1, "kind-class discovery returned nothing to guard"

    allowed_mentions: set[str] = {
        # The front door's export list: class NAMES as strings, no binding.
        "__all__",
        # The MCP error mapper holds the root class in a membership tuple and
        # the kind-class names as payload strings; neither rebinds a class.
        "_KNOWN_ERRORS",
        "_KIND_NAMES",
    }
    seen_mentions: set[str] = set()

    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        relative_path = path.relative_to(source_root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets: list[ast.expr] = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            if node.value is None:
                continue

            mentioned = {
                name
                for sub in ast.walk(node.value)
                for name in (_mentioned_name(sub),)
                if name in guarded_names
            }
            if not mentioned:
                continue

            target_names = {
                bound.id
                for target in targets
                for bound in ast.walk(target)
                if isinstance(bound, ast.Name)
            }
            assert target_names, (
                f"{relative_path}:{node.lineno}: assignment mentioning "
                f"{sorted(mentioned)} binds no readable name, so this guard "
                "cannot tell whether it launders a kind class"
            )
            assert target_names <= allowed_mentions, (
                f"{relative_path}:{node.lineno}: assignment to "
                f"{sorted(target_names)} mentions kind class(es) "
                f"{sorted(mentioned)}; construct a kind class under its own "
                "name so the raise-site guard can resolve it"
            )
            assert _static_name(node.value) is None, (
                f"{relative_path}:{node.lineno}: {sorted(target_names)} rebinds "
                f"{ast.unparse(node.value)!r} to a second name, which the "
                "raise-site guard cannot follow"
            )
            seen_mentions |= target_names

    assert seen_mentions == allowed_mentions, (
        "allowlisted kind-class mentions must remain exact and present: "
        f"missing={allowed_mentions - seen_mentions}, "
        f"unexpected={seen_mentions - allowed_mentions}"
    )


def test_a_coded_error_survives_pickle_and_copy() -> None:
    """A coded refusal still crosses a process boundary intact.

    `BaseException.__reduce__` rebuilds via `cls(*self.args)`, which stopped
    working when `code` became required -- unpickling raised `TypeError` and a
    worker raising a coded refusal came back to the parent as a broken pool,
    with the refusal destroyed. `docs/storage.md` documents outbox drainers
    running in separate processes, so this path is real.

    Driven over the exported taxonomy, and over an UNREGISTERED author code
    (AC7) whose `kind` cannot be re-derived from the catalog.
    """
    exported = [
        value
        for name in ontary.__all__
        if isinstance(value := getattr(ontary, name), type)
        and issubclass(value, OntaryError)
    ]
    assert len(exported) >= 8, "kind-class discovery returned too little to pin"

    for exception in [*exported, ActionError]:
        for code in ("STORE_BUSY", "GAP_NOT_ACKNOWLEDGED"):
            original = exception("message", code=code)
            for clone in (
                pickle.loads(pickle.dumps(original)),
                copy.copy(original),
                copy.deepcopy(original),
            ):
                assert type(clone) is exception
                assert clone.code == original.code
                assert clone.kind == original.kind
                assert str(clone) == "message"

    # `kind` must be CARRIED through the reduce, not re-derived. None of the
    # cases above can tell those apart -- not because their class defaults
    # happen to match (for `STORE_BUSY` most do not: `ValidationFailed`
    # defaults to validation and the catalogued kind is conflict), but
    # because re-constructing with the same code reproduces the same kind
    # either way. Only a kind assigned AFTER construction discriminates.
    mutated = ActionError("message", code="GAP_NOT_ACKNOWLEDGED")
    mutated.kind = "conflict"
    assert pickle.loads(pickle.dumps(mutated)).kind == "conflict"
    assert copy.deepcopy(mutated).kind == "conflict"


def test_error_codes_catalog_has_no_orphans() -> None:
    """Every catalog code is used by a raise site or an intentional fallback.

    Author-supplied override codes (e.g. a toy ``GAP_NOT_ACKNOWLEDGED``) are
    deliberately NOT part of this catalog (AC7's per-instance override is
    additive, not catalogued).  The source scan would catch a future
    engine-side literal that is missing from the catalog, while this exact
    set comparison catches a catalog row with no engine use.
    """
    raised_codes = _source_code_literals()
    carried_codes = raised_codes | {code for code, _kind in NON_RAISED_CODES}
    assert set(ERROR_CODES) == carried_codes
    for code, kind in NON_RAISED_CODES:
        assert ERROR_CODES[code].kind == kind
