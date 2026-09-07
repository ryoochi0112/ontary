"""The stdlib command-line surface for ontology authors and operators."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from typing import TYPE_CHECKING

from ontary.authoring import Ontology
from ontary.diagnose import Finding
from ontary.security import Consumer
from ontary.store import InMemoryStore, ObjectStore, Store

if TYPE_CHECKING:
    # TYPING ONLY, and it must stay that way: `ontary.cli` is importable
    # without the `mcp` extra, which is why `_run_serve` defers
    # `build_mcp_server` behind a try/except ImportError. `from __future__
    # import annotations` (above) makes every reference below a string, so
    # this line never runs at import time. Same pattern, and same reason, as
    # `ontary.mcp_server`'s own TYPE_CHECKING block.
    from mcp.server.mcpserver import MCPServer

__all__ = ("main",)

_DEV_HOST = "127.0.0.1"
_DEFAULT_DEV_PORT = 8000


class _LoadFailure(Exception):
    """A target or store could not be loaded for a CLI command."""


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ontary")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser(
        "validate", help="validate an ontology and report diagnostics"
    )
    validate.add_argument("target", metavar="pkg.module:attr")
    validate.add_argument("--json", action="store_true", dest="as_json")

    serve = commands.add_parser(
        "serve",
        help="serve an ontology over localhost MCP for development",
        description=(
            "Serve an ontology over localhost MCP for development.\n\n"
            "The dev consumer sees unscoped rows only unless the ontology "
            "declares matching dev scopes.\n"
            "Hidden rows are governed by ScopePolicy; see "
            "docs/api-reference.md#scope-policy"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    serve.add_argument("target", metavar="pkg.module:attr")
    serve.add_argument(
        "--dev",
        required=True,
        action="store_true",
        help="acknowledge that this is a localhost-only development server",
    )
    serve.add_argument("--store", metavar="PATH")
    serve.add_argument(
        "--port",
        type=_parse_port,
        default=_DEFAULT_DEV_PORT,
        metavar="N",
    )

    commands.add_parser("version", help="print the installed ontary version")
    return parser


def _load_target(target: str) -> tuple[Ontology, Store | None]:
    module_name, separator, attribute_name = target.partition(":")
    if not separator or not module_name or not attribute_name:
        raise _LoadFailure(
            "target must use the form pkg.module:attr"
        )

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise _LoadFailure(
            f"could not import {module_name!r}: {exc}"
        ) from exc

    try:
        candidate: object = getattr(module, attribute_name)
    except AttributeError as exc:
        raise _LoadFailure(
            f"module {module_name!r} has no attribute {attribute_name!r}"
        ) from exc

    if callable(candidate):
        try:
            candidate = candidate()
        except Exception as exc:
            raise _LoadFailure(
                f"could not call {target!r}: {exc}"
            ) from exc

    if isinstance(candidate, Ontology):
        return candidate, None

    # Existing example builders return `(Ontology, ObjectStore)`.  Accepting
    # that shape keeps the example usable while the public target contract
    # remains an Ontology instance or a zero-argument builder.
    if isinstance(candidate, tuple) and len(candidate) == 2:
        ontology, store = candidate
        if isinstance(ontology, Ontology) and isinstance(store, Store):
            return ontology, store

    raise _LoadFailure(
        f"{target!r} did not resolve to an Ontology or a zero-argument "
        "callable returning one"
    )


def _open_sqlite(ontology: Ontology, path: str) -> ObjectStore:
    try:
        return ObjectStore(ontology.registry, path)
    except Exception as exc:
        raise _LoadFailure(f"could not open SQLite store {path!r}: {exc}") from exc


def _print_load_failure(exc: _LoadFailure) -> None:
    print(f"ontary: {exc}", file=sys.stderr)


def _render_findings(findings: list[Finding], as_json: bool) -> None:
    if as_json:
        print(json.dumps([finding.model_dump(mode="json") for finding in findings]))
        return

    if not findings:
        print("No findings.")
        return

    for finding in findings:
        print(
            f"[{finding.severity.upper()}] {finding.code} at "
            f"{finding.location}: {finding.message}"
        )
        print(f"  fix: {finding.fix_hint}")


def _run_validate(target: str, as_json: bool) -> int:
    try:
        ontology, _returned_store = _load_target(target)
    except _LoadFailure as exc:
        _print_load_failure(exc)
        return 2

    # `diagnose` is the complete collector, including every finding that
    # `validate()` would reject.
    try:
        findings = ontology.diagnose()
    except Exception as exc:
        _print_load_failure(_LoadFailure(f"could not diagnose {target!r}: {exc}"))
        return 2

    _render_findings(findings, as_json)
    return 1 if any(finding.severity == "error" for finding in findings) else 0


def _start_dev_server(
    server: MCPServer,
    *,
    host: str,
    port: int,
) -> None:
    """Start the MCP HTTP transport after enforcing the dev-only bind.

    `server` is annotated with the VENDORED `MCPServer`, not a local
    Protocol, so that `mypy --strict` checks this function against the class
    that actually runs. The previous shape -- a hand-written Protocol reached
    through a `cast` -- asserted the vendored surface instead of checking it,
    and mypy stayed green over `server.settings.host = host` even though mcp
    2.x's `Settings` has no such field, so `ontary serve --dev` shipped
    raising `ValueError` at runtime. Keep the real class here: it is what
    turns the next vendored-API drift back into a `make verify` failure.

    One drift this CANNOT catch: `MCPServer.run` is a single
    `run(transport="stdio", **kwargs: Any)` with no overloads, so mypy
    accepts any keyword and any transport. Omitting `transport=` below would
    silently serve stdio and discard host/port. That one is held by
    `tests/test_cli_serve.py::test_serve_passes_dev_configuration_to_real_mcp_server`,
    which asserts the exact transport and kwargs against a real `MCPServer`.
    """
    if host != _DEV_HOST:
        raise ValueError(
            f"ontary serve --dev is localhost-only; host must be {_DEV_HOST}"
        )
    server.run(transport="streamable-http", host=host, port=port)


def _run_serve(target: str, store_path: str | None, port: int) -> int:
    try:
        ontology, returned_store = _load_target(target)
    except _LoadFailure as exc:
        _print_load_failure(exc)
        return 2

    if store_path is not None:
        try:
            store: Store = _open_sqlite(ontology, store_path)
        except _LoadFailure as exc:
            _print_load_failure(exc)
            return 2
    elif returned_store is not None:
        store = returned_store
    else:
        store = InMemoryStore(ontology.registry)

    consumer = Consumer(
        actor_id="ontary-dev",
        role="ontary-dev",
        scope_level="dev",
        scope_id="localhost",
        kind="human",
    )

    # Keep the optional dependency out of the CLI import graph. Importing
    # this module is safe without the extra; its builder performs the existing
    # guarded MCPServer import and supplies the established installation hint.
    try:
        from ontary.mcp_server import build_mcp_server

        server = build_mcp_server(
            ontology,
            store,
            consumer,
            name=f"{ontology.name} (ontary dev)",
        )
        _start_dev_server(server, host=_DEV_HOST, port=port)
    except ImportError as exc:
        _print_load_failure(_LoadFailure(str(exc)))
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "version":
        import ontary

        print(ontary.__version__)
        return 0
    if args.command == "validate":
        return _run_validate(args.target, args.as_json)
    if args.command == "serve":
        return _run_serve(args.target, args.store, args.port)

    # `argparse` makes this unreachable when the parser is used normally.
    parser.error("a subcommand is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
