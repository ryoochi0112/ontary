# Changelog

Notable changes to `ontary`. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versioning follows
[docs/compatibility.md](docs/compatibility.md) — pre-1.0, so **a minor bump may break
you**.

## [0.7.0] — 2026-08-25

The DX-suite release: a stricter authoring front door, fail-loud ingest and
query validation, bounded MCP reads, operator diagnostics, and runnable
authoring/serving helpers. This is a pre-1.0 minor bump; read the migration
notes before upgrading.

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.7.0"
```

### Migration notes from 0.6.0

- **Front door exports (additive):** `ontary.__all__` now includes
  `OntologyClient`, `InMemoryStore`, `EffectMeta`, `EffectDispatcher`,
  `ScopePolicy`, `CustomResolver`, `ref`, `scope_ref`, `RetryPolicy`,
  `DrainReport`, `OutboxRecord`, and `Declarations`. Prefer the replacement
  root imports, for example `from ontary import OntologyClient`; canonical
  submodule imports such as `from ontary.client import OntologyClient` remain
  supported.

- **Ingest now raises by default:** `client.ingest()` and
  `client.ingest_links()` now default to `on_error="raise"`. Catch
  `ontary.ingest.IngestError` and inspect its `report`;
  `len(error.report.inserted_ids)` is the committed-record count. Pass
  `on_error="report"` to retain the old report-returning behavior. The
  committed prefix is still written before a later record fails.

- **`IngestError` is an exception, not a Pydantic model:** it now subclasses
  `Exception` and carries `report` plus the committed-record information;
  replace `isinstance(error, BaseModel)` with `isinstance(error, IngestError)`
  or `except IngestError`. Callers using `model_fields` should inspect the
  explicit exception fields (`index`, `reason`, `code`, and `report`) instead.
  `model_dump()` remains only a limited compatibility serializer for the old
  error-row fields; use `error.report.model_dump()` for the complete batch
  report.

- **Execute signature and result shape:** use `client.execute(params)` or
  `client.execute(params=params)` for a typed action, and
  `client.execute("ActionName", {"field": value})` (or the equivalent
  `action=...`, `params=...` keywords) for the string form. Replace callers
  that depended on the old positional-only parameter names. Action handlers
  may now return nested JSON-safe `dict[str, Any]`; callers that assumed every
  result value was a string should consume the JSON value tree instead.

- **Malformed `execute`/`traverse` calls now refuse as `INVALID_PARAMS`:**
  calling `execute` or `traverse` with a shape no overload accepts (for
  example the string form without params or `from_id`) now raises
  `ValidationFailed` with `code="INVALID_PARAMS"` instead of an
  `InternalError` with `code="INTERNAL_ERROR"`; `INTERNAL_ERROR` is reserved
  for genuine engine invariants.

- **String traversal positional order:** the string form changed from
  `client.traverse(obj_type, obj_id, link)` to
  `client.traverse(obj_type, link, from_id)`. All three arguments are `str`,
  so mypy cannot detect this migration; update positional call sites manually.
  The replacement typed form is `client.traverse(link_handle, from_id)`.

- **`BoundQuery.traverse` legacy carry:** `BoundQuery.traverse(from_id,
  via=link_handle)` deliberately remains supported, and string
  `BoundQuery.traverse(link, from_id)` remains unchanged. New code may use the
  handle-first `BoundQuery.traverse(link_handle, from_id)` form; treat the
  `via=` spelling as legacy if planning a future breaking cleanup.

- **Unknown `where` keys now refuse:** typed, string, aggregate, and MCP read
  surfaces validate `where` against declared payload fields. A typo now raises
  `ValidationFailed` with `code="UNKNOWN_FIELD"` instead of returning `[]`.
  Replace typo-tolerant empty-result handling with a declared field name and
  catch/branch on `UNKNOWN_FIELD`; lineage fields remain non-filterable.

- **Constructor and enforcement hardening:** `ScopePolicy` rejects
  `min_n < 1`, empty `levels`, and duplicate levels at construction, so an
  ontology declaration with `scope_levels=[]` is rejected when its policy is
  materialized instead of producing a usable definition. Use a non-empty,
  unique level list and `min_n >= 1`. Registry getters now raise
  `ValidationFailed` instead of bare `KeyError`: handle
  `UNKNOWN_OBJECT_TYPE`, `UNKNOWN_LINK_TYPE`, `UNKNOWN_ACTION`, or
  `UNKNOWN_NAME` as appropriate. Enforcement-path assertion failures are now
  real coded validation/errors, so replace `AssertionError` catches with the
  documented `ValidationFailed`/kind classes. Unknown-link traversal on the
  client and MCP surfaces intentionally remains `UNKNOWN_NAME`.

- **MCP consumer contract:** `query_objects` now defaults to a 100-row page,
  caps pages at 1000, and returns `next_cursor`; pass `limit` and the returned
  cursor as `after` to paginate. The `where` grammar is equality-only on
  payload fields. Tool parameters are validated inside the structured error
  envelope, and all successful results are JSON-normalized; return
  JSON-safe values and handle envelope errors by `code`/`kind`, not raw
  framework text. Grouped-aggregate `MIN_N_VIOLATION` messages now say
  `count withheld`; codes are unchanged, and message text is not a wire
  contract. Read tools carry `readOnlyHint`, while `execute_action` carries
  `destructiveHint` through `ToolAnnotations`. The MCP extra is now pinned to
  `mcp>=1.27.2,<2`.

- **New connector declaration requirement:** concrete `BaseConnector` and
  `SourceConnector` subclasses must define a class-level `name`; missing it
  now fails at class definition. Add `name = "your-source-system"` to the
  subclass (abstract connector bases may still omit it).

- **Fingerprint behavior for new property types:** `date` and `choices` are
  fingerprint-neutral for ontologies that do not use them, so upgrading the
  SDK alone leaves those digests byte-identical. An ontology that declares
  `choices=[...]` moves its digest; declaring a `date` property likewise
  changes that declaration's shape. Follow the [fingerprint acceptance
  procedure](docs/compatibility.md#fingerprints-and-07-property-types),
  including row migration before accepting a new choices constraint.

### Additions

- `Ontology.diagnose()` and `Finding`, including design-guide lint findings.
- Runtime-only explain traces via `explain_read()` and `explain_list()`.
- Deterministic test helpers in `ontary.testing`.
- CLI `validate`, `explain`, `version`, and localhost-only `serve --dev`.
- Five runnable recipes in the [cookbook](docs/cookbook.md).
- Runnable MCP and effects-drain examples under
  [`examples/tickets/`](examples/tickets/).

## [0.6.0] — 2026-08-22

The release that completes the SDK-simplification work. This is a pre-1.0 minor
bump, so the public Python surface and the MCP `error.type` field have breaking
changes even though the store schema does not.

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.6.0"
```

### Migration from 0.5.0

**0.5.0 was never tagged.** Its `Lineage` → `SourceLineage` migration is part of
the pre-0.6.0 history archived in `github.com/ryoochi0112/ontic`. A consumer coming
from the last published tag, `v0.4.0`, must apply that migration before the 0.6.0
changes. This note does not create or move a git tag.

- **BoundQuery reads:** replace `BoundQuery.get_object(obj_type, obj_id)` with
  `BoundQuery.get(obj_type, obj_id)`, and replace
  `BoundQuery.get_objects(obj_type, where, ...)` with
  `BoundQuery.list(obj_type, where, ...)`. There are no compatibility shims.
  The aggregate methods also gained typed overloads; that is additive and
  non-breaking.

- **`code` is now required when constructing a kind class.** The class-level
  default is gone: `ValidationFailed("msg")` raises `TypeError`, and
  `ValidationFailed("msg", code="INVALID_PARAMS")` is the replacement. Every
  code and its catalogued `kind` are unchanged — this changes only how an
  exception is *constructed*, never what travels on the wire. Passing an empty
  `code` raises `ValueError`.

  If you construct these exceptions yourself, name the code you mean at each
  site; the previous class defaults were `INVALID_PARAMS` (`ValidationFailed`),
  `PRECONDITION_FAILED` (`PreconditionFailed`, `ActionError`),
  `PERMISSION_DENIED`, `VISIBILITY_DENIED`, `AUTHORITY_ERROR`,
  `CARDINALITY_VIOLATION` (`ConflictError`) and `INTERNAL_ERROR`
  (`InternalError`), so passing those preserves today's behaviour exactly. If
  you only *catch* these exceptions, nothing changes.

  Why: a default meant that any construction reached through a rebound name —
  an alias, an assignment, a function parameter, a `for` target, a `getattr` —
  silently shipped the default code instead of the intended one, and the
  consumer branching on `.code` saw the wrong stable value. Five review rounds
  showed that no source-level guard closes this in a language where any name
  can be rebound: each round modelled one more binding shape and the next found
  another. Requiring the argument makes the wrong construction unwriteable
  rather than undetectable, and retired two guards that existed only to chase it.

- **Exception collapse:** the legacy exception names and the `CodedError` alias
  are deleted, not kept as aliases. Catch the kind class shown below and branch
  on its unchanged `code` when the specific refusal matters. `ActionError`
  remains the handler-facing precondition vocabulary.

  - `VisibilityError`: `MinNViolation` → (`VisibilityError`,
    `MIN_N_VIOLATION`); `VisibilityDenied` → (`VisibilityError`,
    `VISIBILITY_DENIED`).
  - `PermissionDenied`: `ActionPermissionError` → (`PermissionDenied`,
    `PERMISSION_DENIED`; also `SCOPE_DENIED`); `Unauthenticated` →
    (`PermissionDenied`, `UNAUTHENTICATED`); `ConsumerUnresolved` →
    (`PermissionDenied`, `CONSUMER_UNRESOLVED`).
  - `PreconditionFailed`: `CapabilityNotProvided` → (`PreconditionFailed`,
    `CAPABILITY_NOT_PROVIDED`); `EffectNotDispatchable` →
    (`PreconditionFailed`, `EFFECT_NOT_DISPATCHABLE`); `FunctionError` →
    (`PreconditionFailed`, `FUNCTION_ERROR`).
  - `ValidationFailed`: `EntityKeyMismatchError` → (`ValidationFailed`,
    `ENTITY_KEY_MISMATCH`); `UndeclaredCapability` → (`ValidationFailed`,
    `UNDECLARED_CAPABILITY`); `UndeclaredEffect` → (`ValidationFailed`,
    `UNDECLARED_EFFECT`); `EffectNotSerializable` → (`ValidationFailed`,
    `EFFECT_NOT_SERIALIZABLE`); `UnknownName` → (`ValidationFailed`,
    `UNKNOWN_NAME`); `UnknownField` → (`ValidationFailed`, `UNKNOWN_FIELD`);
    `OntologyValidationError` → (`ValidationFailed`, `ONTOLOGY_INVALID`);
    `HydrationError` → (`ValidationFailed`, `INVALID_RECORD`);
    `InvalidLimit` → (`ValidationFailed`, `INVALID_LIMIT`);
    `AfterWithoutLimit` → (`ValidationFailed`, `AFTER_WITHOUT_LIMIT`);
    `InvalidGroupBy` → (`ValidationFailed`, `INVALID_GROUP_BY`);
    `NonNumericAggregate` → (`ValidationFailed`, `NON_NUMERIC_AGGREGATE`);
    `ScopePolicyError` → (`ValidationFailed`, `SCOPE_POLICY_ERROR`);
    `UnknownObjectType` → (`ValidationFailed`, `UNKNOWN_OBJECT_TYPE`);
    `UnknownLinkType` → (`ValidationFailed`, `UNKNOWN_LINK_TYPE`);
    `ObjectNotFound` → (`ValidationFailed`, `OBJECT_NOT_FOUND`);
    `InvalidBatch` → (`ValidationFailed`, `INVALID_BATCH`);
    `InvalidCursor` → (`ValidationFailed`, `INVALID_CURSOR`); and
    `DeclaredShapeViolation` → (`ValidationFailed`, `INVALID_RECORD`).
  - `AuthorityError`: the removed store-specific `ontary.store.AuthorityError`
    → (`AuthorityError`, `AUTHORITY_ERROR`); `SourceCreateRefused` →
    (`AuthorityError`, `SOURCE_CREATE_REFUSED`); `UndeclaredSourceWrite` →
    (`AuthorityError`, `UNDECLARED_SOURCE_WRITE`). The `AuthorityError` kind
    class itself is the replacement; it was not removed.
  - `ConflictError`: `StoreBusy` → (`ConflictError`, `STORE_BUSY`);
    `CardinalityViolation` → (`ConflictError`, `CARDINALITY_VIOLATION`);
    `StoreVersionUnsupported` → (`ConflictError`,
    `STORE_VERSION_UNSUPPORTED`); `OntologyDrift` → (`ConflictError`,
    `ONTOLOGY_DRIFT`); `StoreSchemaIncompatible` → (`ConflictError`,
    `STORE_SCHEMA_INCOMPATIBLE`); `CallerTransactionRefused` →
    (`ConflictError`, `CALLER_TRANSACTION_REFUSED`); and `UpcastFailed` →
    (`ConflictError`, `UPCAST_FAILED`).
  - **The old root export `ontary.StoreError` was a base class, not an
    internal-error replacement.** Its fallback code `STORE_ERROR` remains in
    the catalog, but there is no one-to-one kind-class replacement: a store
    catch-all must become `except OntaryError:` (from `ontary.errors`, or the
    root `ontary.OntaryError`; alternatively use an explicit tuple of the kind
    classes below).
    `except InternalError:` is **NOT** equivalent and will not catch the
    concrete store failures. The 13 direct subclasses in the old
    `StoreError` hierarchy now land as follows: `StoreBusy` →
    (`ConflictError`, `STORE_BUSY`); `UnknownObjectType` →
    (`ValidationFailed`, `UNKNOWN_OBJECT_TYPE`); `UnknownLinkType` →
    (`ValidationFailed`, `UNKNOWN_LINK_TYPE`); `ObjectNotFound` →
    (`ValidationFailed`, `OBJECT_NOT_FOUND`); `CardinalityViolation` →
    (`ConflictError`, `CARDINALITY_VIOLATION`); `InvalidBatch` →
    (`ValidationFailed`, `INVALID_BATCH`); `InvalidCursor` →
    (`ValidationFailed`, `INVALID_CURSOR`); `StoreVersionUnsupported` →
    (`ConflictError`, `STORE_VERSION_UNSUPPORTED`);
    `DeclaredShapeViolation` → (`ValidationFailed`, `INVALID_RECORD`);
    `OntologyDrift` → (`ConflictError`, `ONTOLOGY_DRIFT`);
    `StoreSchemaIncompatible` → (`ConflictError`,
    `STORE_SCHEMA_INCOMPATIBLE`); the old store-specific `AuthorityError` →
    (`AuthorityError`, `AUTHORITY_ERROR`); and `CallerTransactionRefused` →
    (`ConflictError`, `CALLER_TRANSACTION_REFUSED`). The inherited
    `SourceCreateRefused` and `UndeclaredSourceWrite` are both
    `AuthorityError` failures, with their specific codes shown above.
  - `CodedError` was an alias, not a distinct code: replace it with the
    `OntaryError` base and inspect the concrete error's `code` and `kind` (the
    base default remains `INTERNAL_ERROR`).

  The authoritative migration rule is that every error `code` and `kind` value
  is **UNCHANGED**; those values are the wire/compatibility surface. The names
  to catch are now `OntaryError` plus `VisibilityError`, `PermissionDenied`,
  `PreconditionFailed`, `ValidationFailed`, `AuthorityError`, `ConflictError`,
  `InternalError`, and `ActionError`.

  The following old dual-inheritance relationships are also gone. Catch the
  kind class and code shown above instead: `UnknownName` was a
  (`ValidationFailed`, `KeyError`) → (`ValidationFailed`, `UNKNOWN_NAME`),
  `UnknownField` was a (`ValidationFailed`, `KeyError`) →
  (`ValidationFailed`, `UNKNOWN_FIELD`), `HydrationError` was a
  (`ValidationFailed`, `ValueError`) → (`ValidationFailed`, `INVALID_RECORD`),
  and `ActionPermissionError` was a (`PermissionDenied`, `ActionError`,
  `PermissionError`) → (`PermissionDenied`, `PERMISSION_DENIED` or
  `SCOPE_DENIED`). At 0.6.0, an existing `except KeyError:` around a typed
  read, `except ValueError:` around hydration, or `except ActionError:` (or
  `except PermissionError:`) around `client.execute(...)` silently stops
  catching these failures: there is no import or type error to expose the
  break.

- **MCP wire change:** in an MCP error payload, `error.type` now carries the
  kind-class name, such as `VisibilityError`, `PermissionDenied`, or
  `ValidationFailed`, instead of the legacy exception-class name.
  `error.code` and `error.kind` remain byte-stable. This is a **BREAKING**
  change for consumers that branch on `error.type`; branch on `code` or `kind`
  for the stable contract.

- **Deleted drift and identity features:** replace `assess_drift` by comparing
  `OntologyFingerprint` values directly, and replace `describe_drift` by
  comparing the fingerprints' `.types` mappings. `DriftAssessment` has no
  replacement object: use `OntologyFingerprint` for the comparison and handle
  the `ConflictError` with code `ONTOLOGY_DRIFT` (the former `OntologyDrift`)
  for the store's refusal. The fingerprint check-and-refuse behavior is
  **UNCHANGED**; only the descriptive-assessment API was removed. The deleted
  `resolve_identities`, `IdentityRule`, and `IdentityMatch` APIs have no SDK
  replacement: matching is caller-owned, and the `ontary.connect.identity`
  module is gone.

- **Front-door demotions:** `ontary.__all__` is exactly 45 names. T10 deleted
  nothing from the engine; names outside that curated front door remain
  importable from their canonical submodule. The complete inventory below is
  copied from `tests/test_docs.py`'s `DEMOTED_NAMES_BY_MODULE`, which is the
  source of truth for both the docs and the guard. Import `MappingValidationError`
  from `ontary.connect`.

  | Destination module | Names still importable there |
  | --- | --- |
  | `ontary.actions` | `ActionExecutor` |
  | `ontary.audit` | `CapabilityAccessRecord`, `EffectRecord` |
  | `ontary.authoring` | `ref`, `scope_ref` |
  | `ontary.client` | `OntologyClient`, `OntologyRuntime` |
  | `ontary.connect` | `LinkSkip`, `MappingValidationError`, `RunReport`, `SourceConnector`, `SourceLineage`, `map_batch`, `run_dlt_extract`, `to_date`, `to_datetime`, `to_optional_date`, `to_optional_datetime` |
  | `ontary.declarations` | `Declarations` |
  | `ontary.effects` | `EffectDispatcher`, `EffectMeta` |
  | `ontary.errors` | `ERROR_CODES`, `ErrorCodeInfo`, `Kind` |
  | `ontary.fingerprint` | `OntologyFingerprint`, `fingerprint_ontology` |
  | `ontary.functions` | `FunctionHandler`, `FunctionRegistry` |
  | `ontary.ingest` | `IngestError`, `IngestReport`, `bulk_link`, `bulk_upsert` |
  | `ontary.mcp_server` | `ConsumerResolver`, `build_multi_consumer_mcp_server` |
  | `ontary.meta` | `ActionParameterDef`, `ActionTypeDef`, `FunctionDef`, `LinkTypeDef`, `ObjectTypeDef`, `OntologyRegistry`, `PropertyDef`, `PropertyType`, `ScopeLevel`, `Upcaster` |
  | `ontary.migrate` | `MigrationFailure`, `MigrationReport`, `migrate_object_type`, `upcast_object_type` |
  | `ontary.ontology` | `OntologyDef` |
  | `ontary.outbox` | `DEFAULT_RETRY_POLICY`, `DrainReport`, `OutboxRecord`, `OutboxState`, `RetryPolicy` |
  | `ontary.query` | `GuardedQuery` |
  | `ontary.scope` | `CustomResolver`, `Direction`, `RowVisibilityFn`, `ScopePolicy`, `ScopeRule`, `resolve_contributor`, `resolve_owning_scope` |
  | `ontary.security` | `ConsumerKind`, `covers_scope` |
  | `ontary.store` | `AuditEntry`, `DEFAULT_BATCH`, `DEFAULT_TENANT`, `InMemoryStore`, `Lineage`, `SCHEMA_VERSION`, `StoredObject`, `WriteRecord`, `accept_ontology_fingerprint`, `check_ontology_fingerprint` |
  | `ontary.upcast` | `upcast_payload` |

- **Docs:** the README narrative is now split into Diátaxis-shaped English
  pages: [`docs/storage.md`](docs/storage.md), [`docs/connectors.md`](docs/connectors.md),
  [`docs/mcp-serving.md`](docs/mcp-serving.md), [`docs/effects.md`](docs/effects.md),
  [`docs/queries.md`](docs/queries.md), and [`docs/authority.md`](docs/authority.md).
  The README is a compact front door; the existing Japanese API-reference and
  ontology-design documents remain the maintained JA surfaces.

### Changed

- **Store compatibility:** the store schema was **NOT bumped**. `SCHEMA_VERSION`
  remains **9**, and existing SQLite store files open with no migration. SQLite
  and Postgres now share one SQL/DDL source; that is an internal consolidation
  with no behavior change, and it is why the Postgres `audit_log` column order
  changed on **FRESH-CREATE schemas only**.

- **The Postgres RLS session GUC is renamed `ontarydk.tenant` → `ontary.tenant`,
  and RLS policies are persisted database state.** A database created by ≤ 0.3.0
  has `tenant_isolation` policies compiled against the old GUC; `_init_schema`
  sees the schema stamp already current (still v9) and will not recreate them.
  Because RLS here is fail-closed, 0.4.0 against such a database reads zero rows.
  Run this once per existing RLS-enabled database before upgrading:

  ```sql
  ALTER POLICY tenant_isolation ON objects
      USING (tenant = current_setting('ontary.tenant', true))
      WITH CHECK (tenant = current_setting('ontary.tenant', true));
  ALTER POLICY tenant_isolation ON links
      USING (tenant = current_setting('ontary.tenant', true))
      WITH CHECK (tenant = current_setting('ontary.tenant', true));
  ALTER POLICY tenant_isolation ON audit_log
      USING (tenant = current_setting('ontary.tenant', true))
      WITH CHECK (tenant = current_setting('ontary.tenant', true));
  ALTER POLICY tenant_isolation ON effect_outbox
      USING (tenant = current_setting('ontary.tenant', true))
      WITH CHECK (tenant = current_setting('ontary.tenant', true));
  ```

Pre-0.6.0 history and tags v0.1.0–v0.4.0 live in github.com/ryoochi0112/ontic.
