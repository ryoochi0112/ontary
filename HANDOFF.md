# HANDOFF — ontary roadmap M0, issue #40 (`validate()`/`diagnose()` miss clear modelling mistakes)

Session scratch for the next session. Drop this file before the branch merges.

## Verified state

Anchored to `main @ 9af1f18`, working tree clean, 2026-09-27.

- **DONE, verified:** #39 merged as PR #82 at `9af1f18`; branch deleted local + origin; unreleased.
- **In flight (作業中):** #40 design locked (below); **no code written yet**. This branch
  `fix/checks-that-teach-40` holds only this file.
- **Unverified (未検証):** nothing.

Reproduced at `9af1f18` (one-liners, not tests yet):

| Mistake | Today |
| --- | --- |
| `class P(ActionParams): ticket_id: str = target(Ticket)` + `@ontology.action(P, target=Org, roles=[...])` | `validate()` passes, `diagnose()` empty. Registry only checks `target_type` / `refers_to` exist, never that they agree (`meta.py` `_validate_action_references`, `diagnose.py` `_registry_action_findings`). Run time: scope gate follows the param (Ticket), audit + MCP say Org. |
| Ingest `{"id": "t1"}` under `Ticket(id)`; reopen the same SQLite file with `Ticket(id, priority: str)` | store open, `bind()`, `validate()`, `diagnose()` all pass; first `get`/`list` raises `INVALID_RECORD` from `model.py` `hydrate` ("Field required"). Store persists only `SCHEMA_VERSION`, no registry fingerprint. |

Repro snippet for case 2: `ObjectStore(registry, path)` (registry FIRST), `client.ingest("Ticket", rows,
source=Source(source_system=, source_id=, extracted_at=))` (api_name string, not the class).

## Decisions (locked with Ryo, 2026-09-27, lettered options)

1. **Case 2 runs as an explicit sweep, not at `bind()`/store open** (option B):
   `Ontology.diagnose(store=None)` returns extra `Finding`s when a store is given;
   `Ontology.validate(store=None)` raises on them. `bind()` stays O(1).
2. **Case 1 is refused at `@ontology.action` decoration time** with `ONTOLOGY_INVALID`
   (same shape as #39: name the action, the param, its `target()` type, and `target=`),
   AND the same rule lands in `OntologyRegistry.validate()` (`_validate_action_references`)
   and as a `diagnose()` finding (`_registry_action_findings`) so hand-built registries get it.
   Rule: every param with `scope_semantics == "target"` must have `refers_to == action.target_type`.
3. Store-sweep defaults (offered, not objected to):
   - Finding code reuses **`INVALID_RECORD`** (no new error code; `ADDED_SINCE_080_ERROR_CODES`
     + docs error tables untouched). Broaden its catalogue description if the wording needs it
     (then patch BOTH `docs/api-reference.md` and `.ja.md` rows — `test_docs.py` pins the text).
   - Location names type + property (`ObjectTypeDef['Ticket'].properties['priority']`);
     message gives the count of current rows missing the property. One finding per
     (type, property), never per row.
   - Also report a stored value whose type no longer matches the declared property
     (`hydrate` refuses that with the same code via `_storage_scalar_violation`).
   - Sweep uses `Store.read_all(obj_type)` on current rows only — works on all three
     backends with no new store method. `diagnose(store=...)` must not raise mid-sweep.

## Open decisions

None blocking. Judgment calls for the next session (mention in the PR):
- Whether `validate(store=...)` raises `INVALID_RECORD` or `ONTOLOGY_INVALID` (recommend
  the finding's own code, `INVALID_RECORD`, so the exception matches the finding).
- Whether `OntologyDef.diagnose`/`validate` (the `ontology.py` side) also grow `store=`.
  `resolve_definition` callers only need `Ontology`; keep it on `Ontology` unless cheap.

## Environment

```bash
cd /Users/ryoochi/ontary
git switch fix/checks-that-teach-40
make setup            # uv sync --all-extras
make verify           # ruff + mypy --strict + pytest (offline DoD)
export ONTARY_TEST_POSTGRES_DSN=postgresql://ryoochi@localhost:5432/ontary_test
uv run pytest -q tests/test_store_conformance.py tests/test_store_postgres.py   # Postgres gate
make docs-build       # strict mkdocs
```

## Next actions (in order)

1. **Failing tests first** (TDD): case 1 in `tests/test_authoring.py` (decoration-time refusal
   naming action/param/both types; nothing registered) + `tests/test_diagnose.py` (finding on a
   hand-built registry; `validate()` raises). Case 2 in `tests/test_diagnose.py` against
   `InMemoryStore(registry)`: missing required property → one `INVALID_RECORD` finding with count;
   type-mismatched stored value → finding; clean store → no findings; `validate(store)` raises.
   Run, watch them fail.
2. Implement: `authoring.py` `action()` decorator check after `_derive_action_params`;
   `meta.py` `_validate_action_references` rule; `diagnose.py` `_registry_action_findings` rule
   + new `_store_row_findings(definition, store)`; `Ontology.diagnose(store=None)` /
   `validate(store=None)`.
3. Docs EN + JA (`diagnose`/`validate` signature blocks, a "checks that teach" paragraph
   listing both rules), CHANGELOG `[Unreleased] / Fixed` with `Fixes ontary#40`.
4. `make verify` + Postgres gate + `make docs-build`; delete this HANDOFF.md; commit → push →
   PR (`Fixes #40`), merge is Ryo's. On "merged": sync main, delete branch, next M0 issue.

## Pointers

- Issue: https://github.com/ryoochi0112/ontary/issues/40 · milestone M0 · roadmap `docs/roadmap.md` ("Checks that teach")
- Code: `src/ontary/authoring.py` (`action()` ~L700, `diagnose()` ~L932, `bind()` ~L954),
  `src/ontary/meta.py` (`_validate_action_references` ~L489), `src/ontary/diagnose.py`
  (`_registry_action_findings` L261, `_collect_findings` L757), `src/ontary/model.py`
  (`hydrate` L82), `src/ontary/store/protocol.py` (`read_all` L161)
- Precedent for PR shape: PR #82 (tests-first, EN+JA docs, CHANGELOG, gates table)
- Memory: `~/.claude/projects/-Users-ryoochi-ontary/memory/ontary-authoring-errors-39.md`
