# CLI reference

[← README](../README.md) · [API reference](api-reference.md)

## Overview

The package installs two console scripts: `ontary` (`ontary.cli:main`) and `ontary-mcp` (`ontary.mcp_server:main`).

The `ontary` script is the main entrypoint for validating ontologies and running local development servers. The `ontary-mcp` script provides guidance for building custom Model Context Protocol (MCP) servers.

## Target Form

The `ontary` commands accept a target specifier in the format `pkg.module:attr`. The CLI imports the specified module and extracts the attribute.

The attribute must be an `Ontology` instance, a zero-argument callable returning an `Ontology`, or a callable returning an `(Ontology, Store)` tuple. Any other attribute type results in a load failure.

## ontary validate

The `ontary validate TARGET [--json] [--strict]` command validates an ontology and reports diagnostics. It evaluates the target using `ontology.diagnose()`. This complete collector includes every finding, including advisories that do not make the command exit with a failure code by default.

The command outputs findings as `[SEVERITY] CODE at LOCATION: MESSAGE`. Each finding has an indented `fix: HINT` line. A finding with a guide link also has an indented `guide: URL` line after `fix:`. With no findings, it prints `No findings.`

By default, only findings with severity `error` make the command exit with code 1. The `--strict` flag also makes any remaining `warn` finding exit with code 1. Findings with severity `info` do not affect the exit code, and accepted findings are omitted by `diagnose()`.

### Text Output Example

```
[WARN] STORED_DERIVABLE at object Ticket, property avg_response_hours: the name reads as a score or aggregate
  fix: if Ticket.avg_response_hours is computed from other rows, derive it with a Function; if it is recorded from outside, add accept="STORED_DERIVABLE" to the property
  guide: https://ryoochi0112.github.io/ontary/ontology-design/#normalization-and-derived-values
```

### JSON Output Format

When called with the `--json` flag, the command prints a JSON array of findings. Each finding object contains the keys `code`, `severity`, `location`, `message`, `fix_hint`, and `guide`. The `guide` value is the guide URL or `null` when the finding has no guide link. The `severity` key contains `error`, `warn`, or `info`. The `--strict` flag applies the same exit-code rule when JSON output is selected.

### Advisory Finding Codes

`diagnose()` may emit the following advisory findings. Each row describes the declaration shape that triggers its code.

| Code | Trigger |
|---|---|
| `AUDIT_TYPE` | An object type name ends in `AuditLog`, `AuditEntry`, `AuditTrail`, `AuditRecord`, or `AuditEvent`. |
| `CRUD_ACTION_NAME` | The first word of an action or Function API name is `Set`, `Update`, `Create`, `Delete`, `Remove`, or `Erase`, ignoring letter case. |
| `FORBIDDEN_TYPE_NAME` | An object type name ends in `V` plus digits, `History`, or a year from 1900 to 2099, or ends in `Snapshot` without `snapshot=True`. |
| `FREE_TEXT_STATUS` | A `status` or `*_status` property has type `str` and declares no choices. |
| `MICRO_ACTION` | An action has one non-target parameter whose name matches a property on its target type. |
| `MIN_N_UNSET` | A sensitive property is declared while `min_n` remains at its default value of 3. |
| `STORED_DERIVABLE` | A property name has an aggregate prefix or suffix such as `avg_`, `total_`, or `_score`. |
| `UNSCOPED_SENSITIVE` | A sensitive property belongs to an object type with no scope rule and no explicit unscoped declaration. |

`MIN_N_UNSET` has severity `info`; the other advisory codes have severity `warn`. Errors use `ONTOLOGY_INVALID` for declarations, `INVALID_RECORD` for stored rows that fail hydration, and `RULE_VIOLATED` for stored rows that break a declared rule.

### Options

| Flag | Description |
|---|---|
| `--json` | Print findings as a JSON array instead of text. |
| `--strict` | Exit with code 1 if any `warn` finding remains, in addition to errors. |

## ontary serve

The `ontary serve TARGET --dev [--store PATH] [--port N]` command serves an ontology over a local MCP server for development. The `--dev` flag is required. This flag acknowledges that you are running a localhost-only development server.

The server binds to `127.0.0.1` on default port 8000. It uses a streamable HTTP transport.

### Store Selection

The server determines its data store using a strict precedence. First, it uses the store specified by the `--store` path. Second, it uses the store returned by the target builder tuple. Otherwise, it uses a fresh `InMemoryStore` instance.

### Development Consumer

The dev server uses a default consumer with specific properties. These properties include `actor_id="ontary-dev"`, `role="ontary-dev"`, `scope_level="dev"`, `scope_id="localhost"`, and `kind="human"`.

The consumer sees unscoped rows only. It cannot see scoped rows unless the ontology declares matching dev scopes. It cannot execute actions on scoped ontologies.

Hidden rows are governed by `ScopePolicy`. See [api-reference.md#scope-policy](api-reference.md#scope-policy) for details. The served MCP server is named `"<ontology name> (ontary dev)"`. For production deployment, see [mcp-serving.md](mcp-serving.md).

### Options

| Flag | Description |
|---|---|
| `--dev` | Acknowledges running a local development server. Required. |
| `--store PATH` | Path to a SQLite file. |
| `--port N` | Server port number (1 to 65535). Defaults to 8000. |

### Example Invocation

```bash
ontary serve your_app.ontology:ontology --dev --store ./dev.sqlite --port 8000
```

## ontary version

The `ontary version` command prints the installed package version. It then exits with code 0.

## ontary-mcp

The `ontary-mcp` command cannot serve an ontology directly. Running `ontary-mcp` or `python -m ontary.mcp_server` exits with a message. The message instructs you to call `build_mcp_server(ontology, store, consumer).run()` from your own entrypoint script. If the `mcp` extra is missing, the script reports the missing extra first.

## Dependency Requirements

The `ontary.cli` module can be imported without the `mcp` extra. Only `ontary serve` and `ontary-mcp` require the `mcp` extra. If the `mcp` extra is missing, `ontary serve` reports the error. The error names the range to install.

## Exit codes

| Command | Code | Meaning |
|---|---|---|
| `ontary validate [--strict]` | `0` | No findings have severity `error`; with `--strict`, no findings have severity `warn`. `info` findings do not affect the exit code. |
| `ontary validate [--strict]` | `1` | One or more findings have severity `error`, or `--strict` is set and one or more findings have severity `warn`. |
| `ontary validate` | `2` | Target cannot be loaded or diagnosed. Load failures print `ontary: <reason>` to stderr. |
| `ontary serve` | `0` | The server stops normally. |
| `ontary serve` | `2` | Target or store cannot load, or `mcp` extra is missing. |
| `ontary version` | `0` | Successfully printed version. |
| `ontary-mcp` | `1` | Script run directly instead of importing. Exits with guidance message. |
