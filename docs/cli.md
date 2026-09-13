# CLI reference

[← README](../README.md) · [API reference](api-reference.md)

## Overview

The package installs two console scripts: `ontary` (`ontary.cli:main`) and `ontary-mcp` (`ontary.mcp_server:main`).

The `ontary` script is the main entrypoint for validating ontologies and running local development servers. The `ontary-mcp` script provides guidance for building custom Model Context Protocol (MCP) servers.

## Target Form

The `ontary` commands accept a target specifier in the format `pkg.module:attr`. The CLI imports the specified module and extracts the attribute.

The attribute must be an `Ontology` instance, a zero-argument callable returning an `Ontology`, or a callable returning an `(Ontology, Store)` tuple. Any other attribute type results in a load failure.

## ontary validate

The `ontary validate TARGET [--json]` command validates an ontology and reports diagnostics. It evaluates the target using `ontology.diagnose()`. This complete collector includes every finding that would cause validation to fail.

The command outputs findings as `[SEVERITY] CODE at LOCATION: MESSAGE`. Each finding has an indented `fix: HINT` line. With no findings, it prints `No findings.`

### Text Output Example

```
[WARN] STORED_DERIVABLE at Ticket.total_amount: property looks like a stored aggregate
  fix: derive it with a Function instead of storing it
```

### JSON Output Format

When called with the `--json` flag, the command prints a JSON array of findings. Each finding object contains the keys `code`, `severity`, `location`, `message`, and `fix_hint`. The `severity` key contains `error`, `warn`, or `info`.

### Advisory Warning Codes

The `diagnose` process may emit several advisory warning codes. These codes are `CRUD_ACTION_NAME`, `FORBIDDEN_TYPE_NAME`, `MICRO_ACTION`, `MIN_N_UNSET`, `STORED_DERIVABLE`, and `UNSCOPED_SENSITIVE`. Errors use the code `ONTOLOGY_INVALID`.

### Options

| Flag | Description |
|---|---|
| `--json` | Print findings as a JSON array instead of text. |

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
| `ontary validate` | `0` | No findings or no findings have severity `error`. |
| `ontary validate` | `1` | One or more findings have severity `error`. |
| `ontary validate` | `2` | Target cannot be loaded or diagnosed. Load failures print `ontary: <reason>` to stderr. |
| `ontary serve` | `0` | The server stops normally. |
| `ontary serve` | `2` | Target or store cannot load, or `mcp` extra is missing. |
| `ontary version` | `0` | Successfully printed version. |
| `ontary-mcp` | `1` | Script run directly instead of importing. Exits with guidance message. |
