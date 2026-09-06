"""Full public-surface coverage guard for the tickets reference application."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Iterable
from pathlib import Path

import ontary
import ontary.migrate
import ontary.testing
from ontary.cli import _build_parser

UNEXERCISED_ALLOWLIST: dict[str, str] = {
    "AuthorityError": "specialized authority-denial branch is engine-owned",
    "CapabilityHandle": "advanced capability authoring is outside the tickets domain",
    "ConflictError": "concurrent-write conflicts require a race the reference app avoids",
    "CustomResolver": "tickets uses declarative direct and link-based scope resolution",
    "Declarations": "app-side use would change the app's declarations, which the frozen upgrade fixtures pin (test_upgrade_fixtures.py:243); not adoptable until the next fixture generation",
    "InternalError": "engine-internal failures are not a reference-app workflow",
    "RowVisibilityStore": "visibility storage is composed internally by the governed client",
    "ScopePolicy": "app-side use would change the app's declarations, which the frozen upgrade fixtures pin (test_upgrade_fixtures.py:243); not adoptable until the next fixture generation",
    "declarations": "app-side use would change the app's declarations, which the frozen upgrade fixtures pin (test_upgrade_fixtures.py:243); not adoptable until the next fixture generation",
    "ref": "app-side use would change the app's declarations, which the frozen upgrade fixtures pin (test_upgrade_fixtures.py:243); not adoptable until the next fixture generation",
    "scope_ref": "app-side use would change the app's declarations, which the frozen upgrade fixtures pin (test_upgrade_fixtures.py:243); not adoptable until the next fixture generation",
}

CI_ONLY = {"PostgresStore": "CI-only, offline DoD"}

ROOT = Path(__file__).resolve().parents[1]
E2E_PATH = ROOT / "tests" / "test_examples_tickets_e2e.py"
SCAN_PATHS = (
    *sorted((ROOT / "examples" / "tickets").rglob("*.py")),
    E2E_PATH,
    ROOT / "tests" / "test_examples_smoke.py",
    ROOT / "tests" / "test_upgrade_fixtures.py",
)
_parser = _build_parser()
_subparsers = [
    action
    for action in _parser._actions
    if isinstance(action, argparse._SubParsersAction)
]
assert len(_subparsers) == 1, (
    "ontary CLI parser must contain exactly one argparse subparsers action"
)
# Derive this from argparse so a new subcommand cannot ship unexercised behind a stale literal.
CLI_SUBCOMMANDS: set[str] = set(_subparsers[0].choices)
assert CLI_SUBCOMMANDS, "ontary CLI parser must expose at least one subcommand"
DOCUMENTED_SUBSURFACE = {"diagnose", "build_multi_consumer_mcp_server"}

# This is a vocabulary of type-asserting constructs, not an exemption list or
# allowlist. `cast` is intentionally omitted because it type-checks nothing: it
# is a no-op for both the runtime and mypy. The remaining constructs are the
# only app-side checking positions for 10 public names: Page, TypedPage,
# OutboxRecord, Finding, MigrationReport, MigrationFailure, OntaryError,
# PermissionDenied, PreconditionFailed, and ValidationFailed.
_TYPE_ASSERTING_ARGUMENT_INDEX = {
    "isinstance": 1,
    "issubclass": 1,
    "raises": 0,
    "warns": 0,
}


def _root_name(node: ast.expr) -> str | None:
    """The leftmost name of a dotted chain: `ontary.testing.X` -> `ontary`."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _final_name(node: ast.expr) -> str | None:
    """The last dotted component of a callee or owner expression."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


class _ReferenceCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.adopted: set[str] = set()
        self.mentions: set[str] = set()
        self.strings: set[str] = set()
        self.argv_commands: set[str] = set()
        self.mentions_by_name: dict[str, set[str | None]] = {}
        self._function: str | None = None

    def _record_adopted(self, node: ast.AST) -> None:
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        else:
            return
        self.adopted.add(name)

    def _record_adopted_deep(self, node: ast.AST) -> None:
        """Adopt every name in `node`, except inside a nested call's arguments.

        Spec §3 lists an ordinary call argument as non-adoption, so a walk that
        descended into one contradicted the rule it implements:
        `@register(Handler)` adopts `register`, not `Handler`.
        """
        skipped = {
            id(descendant)
            for parent in ast.walk(node)
            if isinstance(parent, ast.Call)
            for argument in (*parent.args, *(word.value for word in parent.keywords))
            for descendant in ast.walk(argument)
        }
        for child in ast.walk(node):
            if id(child) not in skipped:
                self._record_adopted(child)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for imported in node.names:
            if imported.name != "*":
                self.mentions.add(imported.name)
                self.mentions_by_name.setdefault(imported.name, set()).add(self._function)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self.mentions.add(node.id)
        self.mentions_by_name.setdefault(node.id, set()).add(self._function)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.mentions.add(node.attr)
        self.mentions_by_name.setdefault(node.attr, set()).add(self._function)
        if isinstance(node.value, ast.Name):
            self._record_adopted(node.value)
        # Adopt the accessed attr only through the `ontary` package. Recording
        # it for EVERY access adopted a public name whenever any object had a
        # member of the same name -- `statement.target`, an `ast.AnnAssign`
        # field, against the public `ontary.target`. A CALLED attribute is
        # unaffected: position 1 adopts it from the callee, whatever its owner.
        if _root_name(node.value) == "ontary":
            self._record_adopted(node)
        self.generic_visit(node)

    def _record_argv_command(self, node: ast.expr) -> None:
        """Record an argv token as an exercised subcommand, if it is one.

        A subcommand is a POSITIONAL token. A leading `-` marks an option, so
        `["-m", "ontary.cli", "--help"]` exercises no command, and the empty
        string is not a token at all.
        """
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value
            and not node.value.startswith("-")
        ):
            self.argv_commands.add(node.value)

    def visit_Call(self, node: ast.Call) -> None:
        self._record_adopted(node.func)
        final_name = _final_name(node.func)
        argument_index = _TYPE_ASSERTING_ARGUMENT_INDEX.get(final_name or "")
        if argument_index is not None and len(node.args) > argument_index:
            self._record_adopted_deep(node.args[argument_index])
        # `cli.main([...])`, not every callee whose final name is `main`: the
        # reference app ships three unrelated `main()` entrypoints of its own
        # (`run_connector.py`, `run_mcp.py`, `run_effects.py`), and any of them
        # could otherwise mint a subcommand the real CLI never ran.
        is_cli_main = (
            final_name == "main"
            and isinstance(node.func, ast.Attribute)
            and _final_name(node.func.value) == "cli"
        )
        if is_cli_main and node.args:
            argv = node.args[0]
            if isinstance(argv, (ast.List, ast.Tuple)) and argv.elts:
                self._record_argv_command(argv.elts[0])
        self.generic_visit(node)

    def _record_subprocess_command(self, elements: list[ast.expr]) -> None:
        for marker, command in zip(elements, elements[1:], strict=False):
            if isinstance(marker, ast.Constant) and marker.value == "ontary.cli":
                self._record_argv_command(command)

    def visit_List(self, node: ast.List) -> None:
        self._record_subprocess_command(node.elts)
        self.generic_visit(node)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        self._record_subprocess_command(node.elts)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        # A value-less annotation binds nothing at module or function level, so
        # it checks nothing there (`925c185`). In a CLASS body the same syntax
        # declares a pydantic field, and `visit_ClassDef` adopts it instead.
        if node.value is not None:
            self._record_adopted_deep(node.annotation)
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        if node.annotation is not None:
            self._record_adopted_deep(node.annotation)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        self._record_adopted(node.value)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self._record_adopted_deep(node.type)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.strings.add(node.value)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        previous = self._function
        self._function = node.name
        for decorator in node.decorator_list:
            self._record_adopted_deep(decorator)
        if node.returns is not None:
            self._record_adopted_deep(node.returns)
        self.generic_visit(node)
        self._function = previous

    # Two real methods over one shared body, not `visit_AsyncFunctionDef =
    # visit_FunctionDef`: the alias is a `--strict` [assignment] error, because
    # `NodeVisitor` declares the async hook as taking `AsyncFunctionDef`. The
    # alias also type-checked nothing while this file sat outside `make
    # typecheck`; it is now inside it.
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for base in node.bases:
            self._record_adopted_deep(base)
        for decorator in node.decorator_list:
            self._record_adopted_deep(decorator)
        for statement in node.body:
            # DIRECT statements of the class body only. A bare annotation in a
            # method body is a function-level declaration that binds nothing,
            # so walking the whole ClassDef here would admit it wrongly.
            if isinstance(statement, ast.AnnAssign) and statement.value is None:
                self._record_adopted_deep(statement.annotation)
        self.generic_visit(node)


def _collect_references() -> _ReferenceCollector:
    collector = _ReferenceCollector()
    assert SCAN_PATHS, "reference-app AST scan unexpectedly has no paths"
    for path in SCAN_PATHS:
        assert path.is_file(), f"reference-app AST scan path is missing: {path}"
        collector.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    assert collector.adopted, (
        "reference-app AST scan found no adopted names in the eight recognized positions: "
        "call targets, annotations (arguments, returns, valued assignments, and "
        "class-body fields), base classes, decorators, attribute owners/accesses, "
        "subscript owners, exception types, or type-asserting arguments"
    )
    assert collector.adopted <= collector.mentions, (
        "strict adopted-name set must be a subset of the loose mention set"
    )
    assert collector.argv_commands, (
        "reference-app AST scan found no argv-shaped CLI commands in main([...]) or "
        '[..., "ontary.cli", <cmd>, ...]'
    )
    assert collector.argv_commands <= collector.strings, (
        "argv-shaped command set must be a subset of the old any-string set"
    )
    return collector


def _stale_allowlist_entries(
    allowlist: Iterable[str], collector: _ReferenceCollector
) -> list[tuple[str, str]]:
    """Allowlist entries the reference app now names in ANY position.

    Reads `mentions`, the LOOSE set, deliberately: an entry claiming "not
    exercised" is obsolete the moment the app names the symbol at all, not only
    where that name would count as adopted. Narrowing this to `adopted` would
    leave an import-only or plain-`Assign` reference silently exempt -- the
    entry would keep its exemption while the app had already outgrown it.

    That reason used to be a comment at the call site, and a comment is not a
    gate: repointing the call at `collector.adopted` reds NOTHING in this file's
    surface test. Measured at `bdb8ca9`, `UNEXERCISED_ALLOWLIST` intersects
    `mentions` and `adopted` identically -- both empty -- so the reference-app
    scan cannot tell the two inputs apart. It never will: the case that
    separates them is a stale entry, which the surface test exists to forbid.
    `test_stale_detection_reads_the_loose_mention_set` is therefore the only
    place this choice can be pinned, and it is pinned on a synthetic allowlist.
    """
    return sorted(("name", name) for name in set(allowlist) & collector.mentions)


def _reference_scopes(name: str, collector: _ReferenceCollector) -> set[str | None]:
    """Every scope in which the reference app names `name`; `None` is module scope.

    Reads `mentions_by_name`, the LOOSE index, deliberately. The scope pin's job
    is to catch a CI-only name escaping its gated factory, and the escape shapes
    that matter most are the ones that adopt nothing: a module-level
    `from ontary.store.postgres import PostgresStore`, or a plain `_ALIAS =
    PostgresStore`. Both record `None` here and red the pin. Narrowing this
    index to adopting positions would let either shape sit at module scope
    unnoticed.

    That reason used to be a comment at the call site, and a comment is not a
    gate. Measured at `bdb8ca9`, before the pin below existed: deleting EITHER
    recording arm -- the one in `visit_ImportFrom` or the one in `visit_Name` --
    left every test in this file green. `PostgresStore` is named twice inside
    `_make_postgres_store`, by a local import and by a call, so either arm alone
    still satisfies the pin. The reference-app scan cannot carry this receipt
    for the same reason it could not carry `_stale_allowlist_entries`'s: its one
    fixture is redundant exactly where the pin needs to discriminate.
    `test_scope_pin_reads_the_loose_mention_index` is where it lives instead.
    """
    return collector.mentions_by_name.get(name, set())


def _postgres_factory_is_dsn_gated(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test_names = {child.id for child in ast.walk(node.test) if isinstance(child, ast.Name)}
        body_names = {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}
        if "POSTGRES_DSN" in test_names and "_make_postgres_store" in body_names:
            return True
    return False


def test_v1_reference_app_covers_the_public_surface() -> None:
    """Every public name is referenced or has a current, reasoned exception."""
    root_surface = set(ontary.__all__)
    testing_surface = set(ontary.testing.__all__)
    migrate_surface = set(ontary.migrate.__all__)
    assert root_surface, "ontary.__all__ unexpectedly empty"
    assert testing_surface, "ontary.testing.__all__ unexpectedly empty"
    assert migrate_surface, "ontary.migrate.__all__ unexpectedly empty"

    name_surface = root_surface | testing_surface | migrate_surface | DOCUMENTED_SUBSURFACE
    # CLI subcommands deliberately have no allowlist: every command must be exercised.
    full_surface = {("name", n) for n in name_surface} | {
        ("cli", c) for c in CLI_SUBCOMMANDS
    }
    assert full_surface, "public-surface enumeration unexpectedly empty"

    collector = _collect_references()
    referenced = {("name", n) for n in name_surface & collector.adopted} | {
        ("cli", c) for c in CLI_SUBCOMMANDS & collector.argv_commands
    }

    reasons = UNEXERCISED_ALLOWLIST | CI_ONLY
    empty_reasons = sorted(name for name, reason in reasons.items() if not reason.strip())
    assert not empty_reasons, f"coverage exceptions need non-empty reasons: {empty_reasons}"

    unknown = {("name", k) for k in reasons} - full_surface
    assert not unknown, (
        "coverage exceptions name no public surface: "
        f"{sorted(f'{kind}:{name}' for kind, name in unknown)}"
    )

    # Coverage is strict, but staleness keeps the old loose input on purpose.
    # The reason, and the receipt for it, live on `_stale_allowlist_entries`.
    stale = _stale_allowlist_entries(UNEXERCISED_ALLOWLIST, collector)
    assert not stale, (
        "stale unexercised allowlist entries must be deleted: "
        f"{[f'{kind}:{name}' for kind, name in stale]}"
    )

    missing_ci = {("name", k) for k in CI_ONLY} - referenced
    assert not missing_ci, (
        "CI-only public names lack a static reference: "
        f"{sorted(f'{kind}:{name}' for kind, name in missing_ci)}"
    )
    for name in CI_ONLY:
        # Coverage is strict, but the scope pin keeps the old loose input on
        # purpose. The reason, and its receipt, live on `_reference_scopes`.
        locations = _reference_scopes(name, collector)
        assert locations == {"_make_postgres_store"}, (
            f"CI-only {name} must be referenced only inside _make_postgres_store; "
            f"found scopes: {sorted(str(location) for location in locations)}"
        )
    e2e_tree = ast.parse(E2E_PATH.read_text(encoding="utf-8"), filename=str(E2E_PATH))
    assert _postgres_factory_is_dsn_gated(e2e_tree), (
        "_make_postgres_store must be registered only behind the POSTGRES_DSN gate"
    )

    missing = full_surface - referenced - {
        ("name", k) for k in UNEXERCISED_ALLOWLIST
    }
    assert not missing, (
        "reference app does not exercise public surface names: "
        f"{sorted(f'{kind}:{name}' for kind, name in missing)}"
    )


# --- Positional pins -------------------------------------------------------
#
# The gate above cannot see positions 6 and 7. Measured at `bdb8ca9` by
# replacing each visitor with a bare `generic_visit` and re-running the scan:
# `visit_Subscript` loses 26 adopted names, all of them local variables, and
# `visit_ExceptHandler` loses nothing at all -- in both cases ZERO public
# names, so the gate stays green through either deletion. That is a guard
# clause with no test.
#
# Both positions are kept rather than dropped. Each states what adoption
# *means* -- a subscripted public generic and a caught public error are both
# genuine app-side use -- so deleting them buys strictness now and a spurious
# red later, when the reference app adopts a public name in exactly that shape.
# The cheapest way to silence such a red is a new `UNEXERCISED_ALLOWLIST`
# entry, and that surface only ever shrinks. Kept means pinned: the two tests
# below are the only receipts for those deletions.
#
# Mutation receipt, run at `bdb8ca9`. Each mutant reds exactly one test below
# and leaves the gate above GREEN -- which is the hole, stated as a result:
#
#   delete `visit_Subscript`                       -> subscript pin reds
#   delete `visit_ExceptHandler`                   -> except pin reds
#   except `_record_adopted_deep` -> `_record_adopted`  -> except pin reds
#   subscript `_record_adopted(node.value)` -> deep walk -> subscript pin reds
#   except walks `node` instead of `node.type`     -> except pin reds
#
# The last two are the anti-vacuity arms: they red on a LOOSER collector, so
# neither pin can be satisfied by a visitor that adopts everything it sees.


def _collect_source(source: str) -> _ReferenceCollector:
    """Run the collector over one literal snippet, isolating a single position.

    Deliberately not `_collect_references`: that function reads the reference
    app, which is what fails to exercise these two positions in the first place.
    """
    collector = _ReferenceCollector()
    collector.visit(ast.parse(source))
    return collector


def test_subscript_owner_is_an_adoption_position() -> None:
    """Position 6: a name subscripted outside an annotation is adopted."""
    # The owner is a bare `Name` on purpose. `pkg.Owner[X]` would adopt `Owner`
    # through position 5 whether or not `visit_Subscript` exists, so an
    # attribute owner is the one shape this fixture structurally must not use.
    collector = _collect_source("value = SubscriptOwner[SliceName]\n")

    assert "SubscriptOwner" in collector.adopted, (
        "a name subscripted outside an annotation must be adopted"
    )
    # The slice sits one node from the owner and must stay unadopted: this is
    # what lets the fixture tell the position apart from a blanket walk over
    # the whole `Subscript`, which would pass the assertion above vacuously.
    assert "SliceName" in collector.mentions, "the slice must still count as a mention"
    assert "SliceName" not in collector.adopted, (
        "only the subscript owner is adopted, never the slice"
    )


def test_caught_exception_type_is_an_adoption_position() -> None:
    """Position 7: the type of an `except` clause is adopted, tuples included."""
    # Bare `Name` types for the same reason as above: `except pkg.Error:` is
    # adopted by position 5 regardless, so it could not tell the two apart.
    collector = _collect_source(
        "try:\n"
        "    run()\n"
        "except HandlerError:\n"
        "    fallback_flag\n"
        "try:\n"
        "    run()\n"
        "except (FirstTupledError, SecondTupledError):\n"
        "    pass\n"
    )

    assert "HandlerError" in collector.adopted, "a caught exception type must be adopted"
    # The tuple form is the arm that needs the DEEP walk. Narrowing
    # `_record_adopted_deep` to `_record_adopted` here would leave the single
    # type adopted and silently drop both of these.
    assert {"FirstTupledError", "SecondTupledError"} <= collector.adopted, (
        "every type in a tupled except clause must be adopted"
    )
    # A name in the handler BODY is a mention, not an adoption -- the fixture's
    # proof that it distinguishes the two sets.
    assert "fallback_flag" in collector.mentions, "a handler body name is still a mention"
    assert "fallback_flag" not in collector.adopted, (
        "a name used in the handler body is not adopted by the except clause"
    )


def test_stale_detection_reads_the_loose_mention_set() -> None:
    """Staleness fires on a mention, not only on an adoption.

    Mutation receipt, run at `bdb8ca9`: repointing `_stale_allowlist_entries`
    at `collector.adopted` reds THIS test and nothing else -- the surface test
    above stays green, which is exactly why this pin has to exist separately.
    """
    collector = _collect_source(
        "from ontary import ImportedOnlyName\n_UNUSED = AssignedOnlyName\n"
    )

    # The two mention-only shapes named in the docstring above. Neither is an
    # adoption, and that gap is the whole reason the loose input is worth
    # defending; a fixture without it could not tell the two sets apart.
    for name in ("ImportedOnlyName", "AssignedOnlyName"):
        assert name in collector.mentions, f"{name} must be a mention"
        assert name not in collector.adopted, f"{name} must not be an adoption"

    assert _stale_allowlist_entries(
        {"ImportedOnlyName", "AssignedOnlyName"}, collector
    ) == [("name", "AssignedOnlyName"), ("name", "ImportedOnlyName")], (
        "an allowlist entry the app merely mentions is stale and must be reported"
    )
    # Not vacuous: an entry the app never names at all stays exempt, so this
    # test cannot be satisfied by a helper that reports every entry it is given.
    assert _stale_allowlist_entries({"NeverNamedElsewhere"}, collector) == [], (
        "an allowlist entry the app never names is not stale"
    )


def test_scope_pin_reads_the_loose_mention_index() -> None:
    """The scope index records mention-only references, and records their scope.

    Mutation receipt, run at `bdb8ca9`:

        drop `visit_ImportFrom`'s recording  -> this test only, surface GREEN
        drop `visit_Name`'s recording        -> this test only, surface GREEN
        drop `_function` tracking            -> this test AND the surface test
        helper answers every lookup          -> this test only

    The first two arms are the whole reason this test exists: they are the ones
    the reference-app scan cannot see. The third is already caught upstream and
    is listed so the receipt is not read as claiming more than it proves.
    """
    # Four distinct names, one per (shape x scope) cell. Sharing a name across
    # cells is what makes the reference app blind here: `PostgresStore` is
    # covered twice inside its factory, so either recording arm alone keeps the
    # real pin satisfied. This fixture must not repeat that mistake.
    collector = _collect_source(
        "from pkg import ImportedOnlyName\n"
        "_ALIAS = AssignedOnlyName\n"
        "def _gated_factory():\n"
        "    from pkg import ScopedImportName\n"
        "    _local = ScopedAssignName\n"
    )

    # None of the four is adopted anywhere. That is the point: narrowing the
    # index to adopting positions would lose every one of them.
    for name in (
        "ImportedOnlyName",
        "AssignedOnlyName",
        "ScopedImportName",
        "ScopedAssignName",
    ):
        assert name not in collector.adopted, f"{name} must be mention-only"

    # Module scope reads as `None`, which is what reds the real pin: a set
    # holding anything but the gated factory's own name fails equality there.
    assert _reference_scopes("ImportedOnlyName", collector) == {None}, (
        "a module-level import must be recorded at module scope"
    )
    assert _reference_scopes("AssignedOnlyName", collector) == {None}, (
        "a module-level plain assignment must be recorded at module scope"
    )
    assert _reference_scopes("ScopedImportName", collector) == {"_gated_factory"}, (
        "a function-local import must be recorded under its enclosing function"
    )
    assert _reference_scopes("ScopedAssignName", collector) == {"_gated_factory"}, (
        "a function-local assignment must be recorded under its enclosing function"
    )
    # Not vacuous: an unreferenced name has no scopes, so this test cannot be
    # satisfied by an index that answers every lookup.
    assert _reference_scopes("NeverReferenced", collector) == set(), (
        "a name the app never references must have no recorded scope"
    )


def test_class_body_field_annotation_is_a_checking_position() -> None:
    """A bare annotation in a CLASS body is adopted; elsewhere it still is not.

    `subject: str` inside a declared model is not decorative: pydantic reads
    `__annotations__` and builds a validating field from it, so the annotation
    is genuine app-side use of the type. The same syntax at module or function
    level really does create no binding, which is what `925c185` (T5) was aimed
    at, and it stays excluded.

    Mutation receipt, run at `aaa8c5e`:

        drop the `visit_ClassDef` field loop      -> this test only, surface GREEN
        drop `visit_AnnAssign`'s value guard      -> this test only, surface GREEN

    The second arm is the naive form of this change -- admitting the construct
    everywhere instead of only in a class body -- and it is what the three
    negative assertions below exist to reject.
    """
    collector = _collect_source(
        "module_level: ModuleBodyName\n"
        "class _Model:\n"
        "    field: ClassBodyName\n"
        "def _factory():\n"
        "    local: FunctionBodyName\n"
        "class _Holder:\n"
        "    def method(self):\n"
        "        inner: MethodBodyName\n"
    )

    assert "ClassBodyName" in collector.adopted, (
        "a bare field annotation in a class body must be adopted"
    )
    # A method body is a FUNCTION body that happens to sit inside a class, so
    # the class-body rule must not reach it. This is the arm that separates
    # "direct statements of `node.body`" from "anything under a ClassDef".
    for name in ("ModuleBodyName", "FunctionBodyName", "MethodBodyName"):
        assert name in collector.mentions, f"{name} must still be a mention"
        assert name not in collector.adopted, (
            f"{name} is a bare annotation outside a class body and binds nothing, "
            "so it must not be adopted"
        )


def test_only_the_cli_module_main_registers_a_subcommand() -> None:
    """`cli.main([...])` is the CLI; any other callee named `main` is not.

    The reference app defines three unrelated `main()` entrypoints of its own
    (`run_connector.py`, `run_mcp.py`, `run_effects.py`), so matching on the
    final name alone lets a foreign entrypoint mint a subcommand that the real
    CLI never ran -- a silent GREEN, which is the failure this whole gate
    exists to prevent.

    Tightening is safe here in a way it was not for item C: CLI subcommands
    deliberately have no allowlist, so a shape this rule declines to recognise
    must be fixed by writing the call as `cli.main(...)`, never by an
    exemption. A bare `main([...])` from `from ontary.cli import main` is
    therefore out of scope on purpose.

    Mutation receipt, run at `aaa8c5e`: matching on `final_name == "main"`
    alone -- the rule before this change -- reds this test and nothing else.
    """
    collector = _collect_source(
        'cli.main(["audit"])\n'
        'runner.main(["smuggled_by_attribute"])\n'
        'main(["smuggled_by_bare_name"])\n'
    )

    assert collector.argv_commands == {"audit"}, (
        "only a `main` reached through a `cli` owner may register a subcommand"
    )


def test_an_option_token_is_not_a_subcommand() -> None:
    """Both argv rules record positional tokens only.

    `[..., "ontary.cli", "--help"]` took the next string unconditionally, so an
    option registered as an exercised subcommand. A subcommand is positional,
    so a leading `-` disqualifies a token, and so does the empty string.

    Mutation receipt, run at `aaa8c5e`: dropping the leading-`-` guard reds
    this test and nothing else; dropping the non-empty guard likewise.
    """
    collector = _collect_source(
        'cli.main(["--help"])\n'
        'cli.main([""])\n'
        'subprocess.run([sys.executable, "-m", "ontary.cli", "--version"])\n'
        'subprocess.run([sys.executable, "-m", "ontary.cli", "doctor"])\n'
    )

    # `doctor` is the positive control: without it this test could be satisfied
    # by a rule that records nothing at all.
    assert collector.argv_commands == {"doctor"}, (
        "an option or empty token must not register as a subcommand"
    )


def test_only_an_ontary_rooted_attribute_access_is_adopted() -> None:
    """Position 5b adopts the accessed attr only through the `ontary` package.

    Recording it for EVERY attribute access adopted a public name whenever any
    object anywhere happened to have a member of the same name. That is live in
    the reference app: `statement.target` at `test_upgrade_fixtures.py:165` is
    `ast.AnnAssign.target` and has nothing to do with the public `ontary.target`.
    It is masked today only because `target(Ticket)` is independently called
    six times -- "matches tokens, not use", the root cause in §1, surviving in
    the last position that still worked that way.

    Measured before narrowing: position 5b was the sole provenance of exactly
    one public name, `__version__`, whose owner is `ontary`. Narrowing costs 59
    non-public coincidences and zero public names.

    Mutation receipt, run at `57f9b87`: recording the attr unconditionally --
    the rule before this change -- reds this test and nothing else.
    """
    collector = _collect_source(
        "ontary.PublicViaPackage\n"
        "ontary.testing.PublicViaSubpackage\n"
        "statement.CoincidingName\n"
        "client.CalledCoincidence()\n"
    )

    assert "PublicViaPackage" in collector.adopted, "`ontary.X` must adopt X"
    assert "PublicViaSubpackage" in collector.adopted, "`ontary.sub.X` must adopt X"
    assert "CoincidingName" in collector.mentions, "a foreign attr is still a mention"
    assert "CoincidingName" not in collector.adopted, (
        "an attribute of a non-`ontary` object must not be adopted by its name alone"
    )
    # The `ontology.diagnose()` shape, which the handoff wrongly believed
    # depended on 5b: a CALLED attribute is adopted by position 1 regardless of
    # its owner, so narrowing 5b cannot cost it.
    assert "CalledCoincidence" in collector.adopted, (
        "a called attribute stays adopted through the call-target position"
    )


def test_a_deep_walk_stops_at_a_nested_call_argument() -> None:
    """`_record_adopted_deep` does not descend into a nested call's arguments.

    Spec §3 already lists an ordinary call argument as non-adoption, so the
    walk contradicted the rule it implements. Measured at `57f9b87`: the
    breadth is the sole provenance of NOTHING, so narrowing it is free.

    Mutation receipt: restoring the plain `ast.walk` walk reds this test and
    nothing else.
    """
    collector = _collect_source(
        "@register(DecoratorArgument)\n"
        "class _Model(Base, keyword=KeywordArgument):\n"
        "    field: Annotated[FieldType, Meta(NestedMetaArgument)] = 1\n"
    )

    assert {"register", "Base", "Annotated", "FieldType", "Meta"} <= collector.adopted, (
        "the decorator, the base, and the annotation's own names stay adopted"
    )
    for name in ("DecoratorArgument", "KeywordArgument", "NestedMetaArgument"):
        assert name in collector.mentions, f"{name} is still a mention"
        assert name not in collector.adopted, (
            f"{name} is an ordinary call argument, which spec §3 excludes"
        )


def test_an_async_function_is_visited_like_a_sync_one() -> None:
    """`async def` reaches the same decorator, return, and scope arms.

    The reference app contains ZERO `async def` (measured at `44a9cf9`: the
    four scan paths hold none), so the production fixture cannot discriminate
    here at all -- deleting the async hook entirely left every test in this
    file green. This is the L18 shape: the receipt has to be unit-level over a
    synthetic source.

    Mutation receipt: replacing `visit_AsyncFunctionDef`'s body with a bare
    `self.generic_visit(node)` reds this test and nothing else. Without the
    hook, `generic_visit` still descends -- so the names survive as mentions,
    which is why each assertion below pairs `adopted` against `mentions`
    instead of testing presence alone.
    """
    collector = _collect_source(
        "@async_decorator\n"
        "async def _worker() -> AsyncReturn:\n"
        "    value = ScopedName\n"
    )

    assert "async_decorator" in collector.adopted, (
        "an async function's decorator is a checking position (rule A, 4)"
    )
    assert "AsyncReturn" in collector.adopted, (
        "an async function's return annotation is a checking position (rule A, 2)"
    )
    # The scope arm is the sharpest of the three: `generic_visit` would still
    # reach `ScopedName` and record it, but at module scope (`None`), because
    # only the hook sets `self._function`. A CI-only name escaping into an
    # async helper is exactly what the scope pin exists to catch.
    assert collector.mentions_by_name.get("ScopedName") == {"_worker"}, (
        "a name inside an async body must be scoped to that function, not module scope"
    )
