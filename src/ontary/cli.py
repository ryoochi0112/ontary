"""The stdlib command-line surface for ontology authors and operators."""

from __future__ import annotations

import argparse
import getpass
import importlib
import json
import sys
from pathlib import Path
from typing import Literal, Protocol, cast

from ontary.authoring import Ontology
from ontary.diagnose import Finding
from ontary.erase import EraseReport, erase_object
from ontary.errors import OntaryError, ValidationFailed, VisibilityError
from ontary.explain import DecisionTrace
from ontary.security import Consumer
from ontary.store import InMemoryStore, ObjectStore, Store

__all__ = ("main",)

_DEV_HOST = "127.0.0.1"
_DEFAULT_DEV_PORT = 8000


class _LoadFailure(Exception):
    """A target or store could not be loaded for a CLI command."""


class _MCPSettings(Protocol):
    host: str
    port: int


class _MCPServer(Protocol):
    settings: _MCPSettings

    def run(
        self,
        transport: Literal["streamable-http"] = "streamable-http",
    ) -> None: ...


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

    explain = commands.add_parser(
        "explain", help="explain one governed object read"
    )
    explain.add_argument("target", metavar="pkg.module:attr")
    explain.add_argument(
        "--consumer",
        required=True,
        metavar="ROLE:SCOPE_LEVEL:SCOPE_ID",
    )
    explain.add_argument("--read", required=True, metavar="Type:id")
    explain.add_argument("--store", metavar="PATH")
    explain.add_argument("--json", action="store_true", dest="as_json")

    erase = commands.add_parser(
        "erase",
        help="erase one object's content and leave an audited tombstone",
        description=(
            "Erase one object by type and id from a store.\n\n"
            "Erasure purges content from current and historical rows while "
            "structure and lineage survive in audited tombstone rows."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    erase.add_argument("target", metavar="pkg.module:attr")
    erase.add_argument("--store", required=True, metavar="PATH")
    erase.add_argument("--type", required=True, dest="object_type", metavar="T")
    erase.add_argument("--id", required=True, dest="object_id", metavar="ID")
    erase.add_argument(
        "--operator",
        default=None,
        metavar="NAME",
        help="operator identity for the audit entry (default: OS user)",
    )

    serve = commands.add_parser(
        "serve",
        help="serve an ontology over localhost MCP for development",
        description=(
            "Serve an ontology over localhost MCP for development.\n\n"
            "The dev consumer sees unscoped rows only unless the ontology "
            "declares matching dev scopes.\n"
            "Use `ontary explain` to debug hidden rows."
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


def _parse_consumer(value: str) -> Consumer:
    parts = value.split(":")
    if len(parts) != 3 or any(not part for part in parts):
        raise _LoadFailure(
            "--consumer must use the form role:scope_level:scope_id"
        )
    role, scope_level, scope_id = parts
    return Consumer(
        actor_id="ontary-cli",
        role=role,
        scope_level=scope_level,
        scope_id=scope_id,
        kind="human",
    )


def _parse_read(value: str) -> tuple[str, str]:
    object_type, separator, object_id = value.partition(":")
    if not separator or not object_type or not object_id:
        raise _LoadFailure("--read must use the form Type:id")
    return object_type, object_id


def _render_refusal(exc: OntaryError, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "verdict": "refused",
                    "error_code": exc.code,
                    "message": str(exc),
                }
            )
        )
        return
    print(f"refused: {exc.code}: {exc}")


def _render_trace(trace: DecisionTrace, as_json: bool) -> None:
    if as_json:
        print(trace.model_dump_json())
        return

    print(f"read: {trace.object_type}:{trace.object_id}")
    print(f"verdict: {trace.verdict}")
    if trace.verdict == "not_found":
        print("read returns None / not found")
    print(f"error_code: {trace.error_code}")

    for rule in trace.rules:
        print(
            "rule: "
            + json.dumps(rule.model_dump(mode="json"), sort_keys=True)
        )
    for redaction in trace.redactions:
        print(
            "redaction: "
            + json.dumps(redaction.model_dump(mode="json"), sort_keys=True)
        )
    print(
        "min_n: "
        + json.dumps(trace.min_n.model_dump(mode="json"), sort_keys=True)
    )


def _render_erase_report(report: EraseReport) -> None:
    print(f"erase: {report.object_type}:{report.object_id}")
    print(f"operator: {report.operator}")
    print(f"erased_at: {report.erased_at.isoformat()}")
    print(f"outcome: {report.outcome}")
    print(f"code: {report.code}")
    print(f"object_rows_purged: {report.object_rows_purged}")
    print(f"audit_entries_purged: {report.audit_entries_purged}")
    print(f"outbox_rows_purged: {report.outbox_rows_purged}")


def _run_erase(
    target: str,
    object_type: str,
    object_id: str,
    store_path: str,
    operator: str | None,
) -> int:
    try:
        ontology, _returned_store = _load_target(target)
    except _LoadFailure as exc:
        _print_load_failure(exc)
        return 2

    try:
        # Resolve the type before exercising the store so a typo is reported
        # as a coded refusal rather than as an erasure of a missing id.
        ontology.registry.get_object_type(object_type)

        store_file = Path(store_path)
        if not store_file.exists():
            raise _LoadFailure(
                f"SQLite store path does not exist: {store_path!r}"
            )
        if not store_file.is_file():
            raise _LoadFailure(
                f"SQLite store path is not a file: {store_path!r}"
            )

        store: Store = _open_sqlite(ontology, store_path)
        if operator is None:
            operator = getpass.getuser()

        report = erase_object(store, object_type, object_id, operator=operator)
    except OntaryError as exc:
        _render_refusal(exc, as_json=False)
        return 1
    except _LoadFailure as exc:
        _print_load_failure(exc)
        return 2
    except Exception as exc:
        _print_load_failure(_LoadFailure(f"could not erase {target!r}: {exc}"))
        return 2

    _render_erase_report(report)
    return 0


def _run_explain(
    target: str,
    consumer_value: str,
    read_value: str,
    store_path: str | None,
    as_json: bool,
) -> int:
    try:
        ontology, returned_store = _load_target(target)
    except _LoadFailure as exc:
        _print_load_failure(exc)
        return 2

    try:
        consumer = _parse_consumer(consumer_value)
        object_type, object_id = _parse_read(read_value)
        # Resolve the type before constructing/exercising the runtime so a
        # typo cannot be misreported as a normal not_found trace.
        ontology.registry.get_object_type(object_type)

        if store_path is not None:
            store: Store = _open_sqlite(ontology, store_path)
        elif returned_store is not None:
            store = returned_store
        else:
            store = InMemoryStore(ontology.registry)

        runtime = ontology.bind(store)
        trace = runtime.explain_read(consumer, object_type, object_id)
    except (VisibilityError, ValidationFailed) as exc:
        _render_refusal(exc, as_json)
        return 1
    except _LoadFailure as exc:
        _print_load_failure(exc)
        return 2
    except Exception as exc:
        _print_load_failure(_LoadFailure(f"could not explain {target!r}: {exc}"))
        return 2

    _render_trace(trace, as_json)
    return 0


def _start_dev_server(
    server: _MCPServer,
    *,
    host: str,
    port: int,
) -> None:
    """Start the MCP HTTP transport after enforcing the dev-only bind."""
    if host != _DEV_HOST:
        raise ValueError(
            f"ontary serve --dev is localhost-only; host must be {_DEV_HOST}"
        )
    server.settings.host = host
    server.settings.port = port
    server.run(transport="streamable-http")


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
    # guarded FastMCP import and supplies the established installation hint.
    try:
        from ontary.mcp_server import build_mcp_server

        server = cast(
            _MCPServer,
            build_mcp_server(
                ontology,
                store,
                consumer,
                name=f"{ontology.name} (ontary dev)",
            ),
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
    if args.command == "explain":
        return _run_explain(
            args.target,
            args.consumer,
            args.read,
            args.store,
            args.as_json,
        )
    if args.command == "erase":
        return _run_erase(
            args.target,
            args.object_type,
            args.object_id,
            args.store,
            args.operator,
        )
    if args.command == "serve":
        return _run_serve(args.target, args.store, args.port)

    # `argparse` makes this unreachable when the parser is used normally.
    parser.error("a subcommand is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
