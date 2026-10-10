# `ontary` — API リファレンス: エラーコード

[English](api-errors.md) · **日本語** · [← API リファレンス](api-reference.ja.md)

*リファレンス* — 安定したエラーコードと例外階層を掲載します。[API リファレンス](api-reference.ja.md) と [オントロジーのテスト](testing.ja.md) も参照してください。

## エラーコード

送出されるすべてのエラーは安定したコードを持ちます。`OntaryError` が基底で、
`ERROR_CODES: dict[str, ErrorCodeInfo]` が機械可読なレジストリです。

> 以下のコード表は、ソースの `ERROR_CODES` から**自動生成**しています。翻訳による
> 乖離を避けるため、説明文は原文（英語）のままです。


### `authority`

| Code | Meaning |
| --- | --- |
| `AUTHORITY_ERROR` | Fallback code for the authority-refusal family (`AuthorityError`); every concrete refusal a captured write can trigger carries its own more specific code instead (e.g. SOURCE_CREATE_REFUSED, UNDECLARED_SOURCE_WRITE). It is the base for `ObjectStore.capture_action_writes` refusals where a write inside an action's capture context crosses the source-backed/ontology-owned line. |
| `OWNED_PROPERTY_REFUSED` | A bulk_upsert record supplied a value for a property declared ontology-owned on an otherwise source-backed object type. |
| `OWNED_TYPE_REFUSED` | A bulk_upsert/bulk_link record targeted an object or link type that is declared whole-type ontology-owned; no source may supply its rows. |
| `SOURCE_CREATE_REFUSED` | A captured `insert` targeted an object type that is not declared whole-type ontology-owned (`ObjectTypeDef.owned is True`). |
| `UNDECLARED_SOURCE_REMOVAL` | A captured retirement or link closure targeted an object or link type that is not declared ontology-owned. |
| `UNDECLARED_SOURCE_WRITE` | A captured `update` touched a property, or a `create_link` targeted a link type, that is not declared ontology-owned. |

### `conflict`

| Code | Meaning |
| --- | --- |
| `CALLER_TRANSACTION_REFUSED` | Raised when `ActionExecutor.execute()` (or an ingest entry point, a later task) is called while the caller has already opened a `store.transaction()` block. `transaction()` is reentrant, so a caller-owned outer transaction could roll back an action after the executor reported success and audited `ok`. The engine must own the transaction/audit boundary and refuses to nest inside the caller's. Deliberately NOT audited: an audit row inside the caller's transaction could itself be rolled back, so the refusal is raised before any audit write. |
| `CARDINALITY_VIOLATION` | A link creation would violate its LinkTypeDef cardinality. |
| `OBJECT_ALREADY_EXISTS` | An insert used a primary key that already has a live row of the same object type; update that object instead, or retire it first. |
| `OBJECT_ALREADY_RETIRED` | A retirement targeted an object whose current row is already closed. |
| `STORE_VERSION_UNSUPPORTED` | Raised at store construction when the store's schema stamp is not this engine's `SCHEMA_VERSION` -- a SQLite file's `PRAGMA user_version`, or a Postgres database's `schema_meta` row. Neither backend carries a migration ladder: a store written by a different ontary schema shape is REFUSED, never migrated in place and never adopted. An unstamped store that already has an `objects` table is refused for the same reason -- stamping a shape this engine cannot read would be a lying stamp, and every later query would fail as a confusing uncoded SQL error instead. The message names BOTH the store's and the engine's versions, so an operator knows exactly what to upgrade; the way forward is a matching ontary version, or a fresh store the data is migrated into. |
| `STORE_BUSY` | A SQLite transaction could not acquire or retain its database lock within ObjectStore's configured busy timeout; retry after the competing writer finishes or increase busy_timeout. This is a conflict, not a precondition: retrying is the remedy, and the kind travels on the MCP wire so callers can branch on retryability. |

### `internal`

| Code | Meaning |
| --- | --- |
| `INTERNAL_ERROR` | An unclassified failure the MCP surface refuses to describe further, to avoid leaking internals to the caller. |
| `STORE_ERROR` | Fallback code for an unclassified store-layer error. |

### `permission`

| Code | Meaning |
| --- | --- |
| `PERMISSION_DENIED` | The consumer's role is not permitted to execute the action (code `PERMISSION_DENIED`). The permission kind also covers scope refusals under `SCOPE_DENIED`; each raise site supplies the specific code. |
| `SCOPE_DENIED` | The consumer's scope does not cover the action's declared target, scope or reference (`ref()`) parameter (code `SCOPE_DENIED`). Role refusals use `PERMISSION_DENIED`; both are kind permission and each raise site supplies the specific code. |
| `UNAUTHENTICATED` | A request carried no verified identity at all -- kind permission. `build_multi_consumer_mcp_server` raises this for every tool call, including introspection, that reaches it with no authenticated `AccessToken`: stdio (which has no auth context) or HTTP with no `token_verifier` configured. The fix is: configure authentication. It is deliberately separate from `CONSUMER_UNRESOLVED`: a missing credential; mapping the principal fixes a missing consumer, so callers can distinguish the two from `.code` alone. |
| `CONSUMER_UNRESOLVED` | A verified principal existed, but the author's `resolve_consumer` callback returned no `Consumer` for it -- kind permission. `build_multi_consumer_mcp_server` raises this when the callback returns `None` for an otherwise verified `AccessToken`; the fix is: map this principal. It remains distinct from `UNAUTHENTICATED`, where no credential was presented at all, so the two failures are distinguishable from `.code` alone. |

### `precondition`

| Code | Meaning |
| --- | --- |
| `CAPABILITY_NOT_PROVIDED` | A declared capability had no provider bound for this call. |
| `CLOCK_CONFLICT` | A store already has a different clock installed; a store has one clock. Bind with the same clock object, or with no clock to use the one already installed. |
| `CLOCK_REGRESSION` | The store clock reads earlier than the valid_from of the version a write would close; the clock went backwards. Fix the clock (it must never run behind the data it wrote) and retry. |
| `FUNCTION_ERROR` | Registering/calling a Function failed: undeclared api_name, duplicate registration, or no handler bound. |
| `PRECONDITION_FAILED` | An action's precondition failed; the message names it. The conventional code for `ActionError` (kind precondition); an author may attach their own stable code instead, e.g. `raise ActionError("...", code="GAP_NOT_ACKNOWLEDGED")`. It is also used with overridden codes for unregistered/unhandled actions (`UNKNOWN_ACTION`) and parameter-validation failures (`INVALID_PARAMS`) -- see the `code=` overrides at those raise sites. |
| `RESULT_NOT_JSON` | A Function or Action handler result could not be encoded for the JSON boundary both surfaces share. A handler may return JSON scalars, lists, dicts with `str` keys, and `date`/`datetime` objects, which become ISO 8601 strings in the spelling the store keeps. The message names the handler and the key path of the first offending value. Action results must also be dicts. |
| `TRANSITION_NOT_ALLOWED` | A governed property changed to a state not allowed by its declared transition graph; action starts must be initial states. |

### `validation`

| Code | Meaning |
| --- | --- |
| `CLOCK_NOT_TIMEZONE_AWARE` | A clock returned a naive datetime; an instant must be timezone-aware. Return datetime values with a tzinfo, such as datetime.now(timezone.utc). |
| `AFTER_WITHOUT_LIMIT` | `GuardedQuery.get_objects`'s (or `OntologyClient.list`'s) `after` was given without `limit` -- the unpaginated `Store.read_all` path has no page to resume, so ignoring `after` would let a caller that lost track of its limit silently re-read every visible row and duplicate work; a caller that genuinely wants everything passes no `after` at all. |
| `INVALID_BATCH` | `Store.read_page`'s `batch` was < 1 (SQLite's LIMIT -1 means unlimited and InMemoryStore's negative slice drops rows -- both the opposite of a bounded read). |
| `INVALID_CURSOR` | Raised when `Store.read_page`'s `after_key` is malformed OR simply unknown. `after_key` is UNTRUSTED input: it reaches the store from an MCP client via a later page-filling loop, round-tripped from a previous page's cursor without any guarantee the caller did not tamper with it. As amended 2026-07-25, it is a random per-row PAGE TOKEN (`objects.page_token`, uuid4 hex), not a decimal row id. Resolving token to row id through the unique index is the ONLY way to turn a cursor into row identity, so every string never issued for a real row (malformed, tampered, or made up) raises this same error on both backends. There is no distinct well-formed but out-of-range case from the old integer design's `OverflowError`/silent-empty-page divergence. A token issued for a row since superseded by `update` still resolves because lookup uses `row_id` independently of `valid_to`, so an in-flight cursor remains a valid resume point. |
| `GROUP_KEY_COLLISION` | Two distinct `group_by` values in one selection release as the same dictionary key, so one cell would have to describe two populations. The released shape is `dict[str, ...]` -- a public return type and MCP's wire shape -- and `str()` is not injective over the values a group key can take: an optional property keys `None` on the rows that lack it, which collides with a row carrying the literal string `"None"`. The populations did not merge; the later one overwrote the earlier, so the released value (and, under `func="count"`, the released size) described whichever rows were inserted last, decided by nothing the caller supplied or could observe. Raised per group as each is released, AFTER that group's min-N check, so the release floor keeps precedence over a shape refusal. |
| `INVALID_GROUP_BY` | `GuardedQuery.aggregate_by`'s (or `BoundQuery`'s/`OntologyClient`'s) `group_by` cannot be a group key. Either it was falsy (e.g. "") -- the shared aggregation body branches on `group_by`'s truthiness, so a falsy-but-non-None value would otherwise silently collapse to the ungrouped path and return a float instead of a `dict[str, float]` -- or it names a property whose declared `PropertyType` is not groupable (`json`, whose values may be a `dict` or `list` and so need not be hashable; grouping by one used to raise a bare `TypeError` from inside the grouping loop, and `INTERNAL_ERROR` once it crossed the MCP boundary). The declared type is checked, not the stored values, so a `json` column that happens to hold only scalars refuses too rather than working until the first `dict` arrives. Both are checked in `aggregate_by`, where the `GuardedQuery`, `BoundQuery`, and client surfaces converge, before `_aggregate` runs, rather than relying on an assert removed by `python -O`. |
|  | struct プロパティもグループ化できません。`group_by` に指定すると、行を読む前にこのコードになります。 |
| `PAGE_NOT_ITERABLE` | A `Page`/`TypedPage` was iterated, indexed or measured directly instead of through `.items`. Both are pydantic models, so the inherited `BaseModel.__iter__` would otherwise yield `(field_name, value)` pairs -- `for row in page` hands back `('items', [...])` and `('next_cursor', ...)`, and the failure surfaces later as `AttributeError: 'tuple' object has no attribute 'payload'` at whatever touched the row. This refuses at the iteration itself and names `.items` and `limit=None`. |
| `INVALID_LIMIT` | `GuardedQuery.get_objects`'s (or `OntologyClient.list`'s) `limit` was < 1 -- a silently empty page would hide that the call was malformed rather than legitimately paginated. |
| `STALE_CURSOR` | An ordered walk's or a link traversal's cursor no longer names a current row this consumer can resume from; restart from the first page. |
| `INVALID_PARAMS` | A call's parameters failed declared-shape validation: an action's params, or a read parameter whose SHAPE is wrong -- an `order_by` that is neither a field name nor a (field, direction) pair, or a `where=` that is not a mapping of field name to condition. A parameter naming something that does not exist is `UNKNOWN_FIELD` instead; this code is about the shape, not the name. |
| `INVALID_RECORD` | A bulk_upsert record failed declared-shape validation (missing primary key, missing required property, unknown property, or a value that does not match its declared type). The validation kind carries the SAME `INVALID_RECORD` code that `bulk_upsert` already reports: from a caller's point of view, a record not matching the declaration is one failure regardless of which write path noticed. This closes the hole where only ingest checked: `Store.insert`/`update` and therefore `ActionContext.insert`/`update` could commit a row missing a required property or carrying a wrong-typed value, report success, and leave the typed reader unable to hydrate it. The same code wraps a Pydantic `ValidationError` while hydrating a stored `OntologyObject` payload (for example, a non-ISO datetime string), never surfacing a bare traceback; a stored row failing declared-shape validation on read-back is the same failure class ingest carries on write. `Ontology.diagnose(store=...)` reports, per type and property, the stored rows that would fail hydration under the current ontology, and `Ontology.validate(store=...)` raises this code for them. Events use the same code: `ctx.emit` refuses a payload value that does not match its declared type (for example, a naive datetime), and a typed `client.events` read raises it for a stored payload that no longer fits its event class. |
| `LINK_ENDPOINT_NOT_FOUND` | A link creation named an endpoint id with no live row of the link type's declared endpoint type -- missing or retired; a link needs a live object at both ends. |
| `LINK_NOT_FOUND` | A link closure found no matching live link. |
| `NON_NUMERIC_AGGREGATE` | `GuardedQuery.aggregate`'s `value_field` is declared a non-numeric `PropertyType` (anything other than `int`/`float`, such as str/json/datetime/bool). It is checked against the declared type before rows are iterated or coerced, so values that merely look numeric cannot bypass the type contract. `func="count"` is exempt and accepts any declared type. |
| `OBJECT_NOT_FOUND` | An update targeted a non-existent object. |
| `OBJECT_NOT_LOADED` | ActionContext.save got an object this action context did not hand out; load it with ctx.get(...) or ctx.create(...) first, so only the fields the handler changed are written. |
| `OBJECT_RETIRE_NOT_FOUND` | A retirement targeted an object with no stored row. |
| `PRIMARY_KEY_IMMUTABLE` | An update tried to change an object's primary key; a primary key is immutable, so retire the object and insert a new one instead. |
| `RULE_VIOLATED` | A declared object rule returned false or raised while checking the full new row. |
| `ONTOLOGY_INVALID` | A declaration was rejected: `validate()` found invalid cross-references, or an authoring call (`@ontology.object(...)`, `ontology.link(...)`, `.definition`) refused a kwarg of the wrong shape: a misspelled `scope`/`cardinality` literal, a rule not wrapped in a list, a non-callable `row_visibility`, empty `scope_levels`, or `min_n` below 1. |
| `SCOPE_POLICY_ERROR` | A ScopePolicy declaration is unusable: a rule references an undeclared object type, link type, or scope level; a type declares an empty contributor rule list; a type is listed in unscoped_types while also declaring scope rules; or an action's scope parameter refers to an unscoped type. |
| `UNDECLARED_CAPABILITY` | A handler requested a capability its action or function did not declare. |
| `UNDECLARED_EVENT` | An action emitted an event type it did not declare. |
| `EVENT_SUBJECT_INVALID` | An emitted event's subject could not be resolved to a valid target object. |
| `UNKNOWN_ACTION` | An action name is unregistered on the OntologyRegistry, or has no handler bound to it. |
| `UNKNOWN_EVENT_TYPE` | An operation referenced an unregistered event type. |
| `UNKNOWN_FIELD` | A typed `get`/`list` call named a key that is not one of the target class's declared properties. The existence-only check runs client-side before the guarded read layer; a hidden-but-declared key still reaches the visibility kind unchanged, and the string-form surface keeps its silent-non-match behavior. The error lives here (previously `ontary.functions`, which re-exports it). |
| `UNKNOWN_LINK_TYPE` | An operation referenced an unregistered link type. |
| `UNKNOWN_NAME` | A typed `BoundQuery`/`OntologyClient` call named an unregistered object, link, action, or function -- e.g. an undecorated class, a class/`LinkHandle` registered on a different `Ontology`, or a link api_name absent from this registry. Typed lookup failures use the validation kind and live here so `ontary._typed_api` can raise them below the runtime modules. |
| `UNKNOWN_OBJECT_TYPE` | An operation referenced an unregistered object type. |
| `UNKNOWN_OPERATOR` | A mapping-form `where` clause named an operator outside the declared set: `gt`, `gte`, `lt`, `lte`, `in`, `ne`, or `contains` -- or was an empty mapping. A mapping with several operators is validated key by key, so one unknown key refuses the whole clause. |
| `OPERATOR_TYPE_MISMATCH` | A mapping-form `where` operator is not valid for the property's declared type (comparisons need `int`, `float`, `date`, or `datetime`; `contains` needs `str`), or its operand is not a declared-type scalar. |
|  | struct プロパティに対する `where` 条件もこのコードになります。メッセージは `where is not supported on struct property 'amount'` のように示され、内側のフィールドパスは使えません。 |

### `visibility`

| Code | Meaning |
| --- | --- |
| `MIN_N_VIOLATION` | An aggregate would be computed over fewer than min_n distinct contributors. |
| `VISIBILITY_DENIED` | A single-object read/write targeted an object outside the consumer's scope. |

*全 59 コード / 7 種別。*

---

## 例外階層

公開されている kind class は次の 9 型の階層です。`IngestError` は、レコード単位または
クライアント単位の取り込み失敗に使う、追加のコード付き `OntaryError` サブクラスです。
その code はレポート内の失敗に対応します。すべてのエラーは `ERROR_CODES` の安定した
`code` を持ちます。特定の種別を捕捉するには `except <KindClass> as e: e.code` を使い、
`IngestError` を含むすべてのコード付きエラーを捕捉するには `except OntaryError as e: e.code`
を使います。

| 例外 | 親クラス | 送出される場面 |
| --- | --- | --- |
| `OntaryError` | `Exception` | すべてのコード付きエラーの根。コード付きエラーをすべて捕捉するために使います。 |
| `VisibilityError` | `OntaryError` | 可視性ルールが読み書きまたは隠しフィールド操作を拒否したとき、または集計の寄与者が `min_n` 未満のとき。 |
| `PermissionDenied` | `OntaryError` | 呼び出し元に必要な role、scope、または認証済み identity がないとき。 |
| `PreconditionFailed` | `OntaryError` | 必要な operation または action の前提条件を満たさないとき。 |
| `ValidationFailed` | `OntaryError` | 呼び出し元の入力、宣言、レコード、その他の値の検証に失敗したとき。 |
| `AuthorityError` | `OntaryError` | source または呼び出し元が、宣言された権限の範囲外へ書き込もうとしたとき。 |
| `ConflictError` | `OntaryError` | 要求された操作が store、ontology、schema、または link の状態と衝突したとき。 |
| `InternalError` | `OntaryError` | 失敗について、これ以上具体的なコード分類がないとき。 |
| `ActionError` | `PreconditionFailed` | action handler の前提条件に失敗したとき。`code="PRECONDITION_FAILED"` か独自の安定コードを指定します。 |
| `IngestError` | `OntaryError` | レポート内の失敗に対応する安定コードを持つ、レコード単位またはクライアント単位の取り込み失敗。 |
