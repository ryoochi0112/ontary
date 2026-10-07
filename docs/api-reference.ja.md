# `ontary` — API リファレンス

[English](api-reference.md) · **日本語** · [← README](../README.md)

*リファレンス* — `ontary` の公開名を調べるための一覧ページで、使い方は[はじめに](getting-started.ja.md)と[オントロジーのテスト](testing.ja.md)で確認できます。

`ontary` のキュレーションされたフロントドア: `__all__` の **46 個の名前**。
残りのエンジン API は、`ontary.meta`、`ontary.store` などの
定義元サブモジュールから利用します。

これは調べ物のためのドキュメントです。「宣言する → バインドする → 読む → 配信する」
という流れの解説は [README](../README.md) から始めてください。

このページは API リファレンスの索引です。各項目の詳細は次の 7 ページにあります。
[オントロジーの宣言](api-authoring.ja.md)、[ランタイムとクライアント](api-runtime.ja.md)、
[読み取り](api-reading.ja.md)、[Action と Function](api-actions-functions.ja.md)、
[ストアとバルク取り込み](api-stores.ja.md)、[MCP サーバー](api-mcp.ja.md)、
[エラーコード](api-errors.ja.md)。

**目次**

- [フロントドア](#フロントドア)
- [エンジン API](#エンジン-api)
- [オントロジーを宣言する](#オントロジーを宣言する)
- [スコープポリシー](#スコープポリシー)
- [ランタイムとクライアント](#ランタイムとクライアント)
- [読み取り](#読み取り)
- [Action](#action)
- [Function](#function)
- [Capabilities](#capabilities)
- [セキュリティ](#セキュリティ)
- [ストア](#ストア)
- [バルク取り込み](#バルク取り込み)
- [MCP サーバー](#mcp-サーバー)
- [記述子による宣言](#記述子による宣言)
- [エラーコード](#エラーコード)
- [例外階層](#例外階層)

---

## フロントドア

`__all__` はソート済み・重複なし・import 可能で、ちょうど 46 個です。オントロジーの
作者がエンジンの名前空間を選ばずに使う名前だけをここに置きます。

### Authoring vocabulary / 宣言用語彙

`ActionContext`、`ActionParams`、`BoundQuery`、`CapabilityHandle`、`Cardinality`、
`Consumer`、`CustomResolver`、`DirectProperty`、`Event`、`FunctionParams`、`LinkHandle`、`Ontology`、
`OntologyObject`、`RowVisibilityStore`、`SelfScope`、`Sensitivity`、`Source`、
`Store`、`ViaLink`、`prop`、`ref`、`scope_ref`、`target`。

### Runtime entries / ランタイム項目

`Declarations`、`EventRecord`、`Finding`、`InMemoryStore`、`MCPServer`、`ObjectStore`、`OntologyClient`、
`Page`、`PostgresStore`、`ScopePolicy`、`TypedPage`、
`__version__`、`build_mcp_server`、`declarations`。

`MCPServer` は `mcp` SDK のサーバークラスです。MCP ビルダーの戻り値なので再エクスポートしています。
利用には `[mcp]` extra が必要です（`pip install 'ontary[mcp]'`）。
extra がなくても `import ontary` は動きます。`MCPServer` に触れたときだけ、
インストールコマンドを示す `ImportError` になります。core のみの環境では
`from ontary import *` も同じ `ImportError` になります。`__all__` の全名前を取得するためです。

### Error classes / 例外クラス

`ActionError`、`AuthorityError`、`ConflictError`、`InternalError`、`OntaryError`、
`PermissionDenied`、`PreconditionFailed`、`ValidationFailed`、`VisibilityError`。

この 46 個という個数は `tests/test_docs.py` が厳密に検証するため、root export の増加を
見落としません。

```python
from ontary import Ontology, OntologyObject, Consumer, prop, target, Cardinality
```

Python 3.12+。コアパッケージの依存は `pydantic` のみ。エクストラ: `[mcp]`（MCP
サーバー）、`[postgres]`（`PostgresStore` バックエンド）。

---

## エンジン API

以下の名前は意図的にフロントドアへ平坦化していません。エンジンを拡張したり高度な
統合を行ったりするときは、定義元サブモジュールから import してください。

### `ontary.actions`

`ActionExecutor`。

### `ontary.audit`

`CapabilityAccessRecord`。

### `ontary.client`

`OntologyRuntime`。

### `ontary.errors`

`ERROR_CODES`、`ErrorCodeInfo`、`Kind`。

### `ontary.functions`

`FunctionHandler`、`FunctionRegistry`。

### `ontary.ingest`

`IngestError`、`IngestReport`、`bulk_link`、`bulk_upsert`。

### `ontary.mcp_server`

`ConsumerResolver`、`build_multi_consumer_mcp_server`。

### `ontary.meta`

`ActionParameterDef`、`ActionTypeDef`、`FunctionDef`、`LinkTypeDef`、
`ObjectTypeDef`、`OntologyRegistry`、`PropertyDef`、`StructFieldDef`、`TransitionDef`、`RuleDef`、`PropertyType`、`ScopeLevel`。

### `ontary.ontology`

`OntologyDef`。

### `ontary.query`

`GuardedQuery`。

### `ontary.scope`

`Direction`、`RowVisibilityFn`、`ScopeRule`、`resolve_contributor`、
`resolve_owning_scope`。

### `ontary.security`

`ConsumerKind`、`covers_scope`。

### `ontary.store`

`AuditEntry`、`DEFAULT_BATCH`、`DEFAULT_TENANT`、`Lineage`、
`SCHEMA_VERSION`、`StoredObject`、`WriteRecord`。

### `ontary.testing`

SDK 利用者向けのテストヘルパーは `make_store`、`consumer`、`raises_code`、
`FixedClock`、`SequentialIds`、`Scenario`、`scenario` です。`make_store(ontology)` は空の
`InMemoryStore` を新しく作り、`consumer(...)` は有効な `Consumer` を組み立て、
`raises_code(code)` はメッセージではなく機械可読なコードでエラーを検証します
（`ontary.ingest.IngestError` を含む任意の `OntaryError` に加え、安定した文字列
`.code` を公開する構造的に互換な作者定義のコード付き例外にも一致します）。
`FixedClock(start)` はタイムゾーン付きの同じ
日時を毎回返し、naive な start は拒否します。`SequentialIds(prefix)` は
`prefix-1`、`prefix-2`、…という決定的な ID を返します。

`scenario(ontology, *, store=None, clock=None, id_factory=None, capabilities=None)`
は、即時実行でチェーン可能な `Scenario` を返します。ストア、クロック、ID ファクトリは
準備の書き込みより前に一度だけバインドされます。既定は、新しい `InMemoryStore`、
`2026-01-01T00:00:00Z` の `FixedClock`、`SequentialIds("id")` です。上書きするときは
空のストアを渡してください。すべてのメソッドは同じ `Scenario` を返します。

| メソッド | 意味 |
|---|---|
| `given(*objects)` | 型付きの `OntologyObject` インスタンスを準備します。最初の `when` の前でのみ使えます。ストアが拒否した場合は、オブジェクトとエラーコードを示す `AssertionError` として再送出されます。 |
| `given_link(handle, from_, to)` | 型付きの `LinkHandle` でリンクを準備します。端点はオブジェクトまたは id 文字列です。最初の `when` の前でのみ使えます。 |
| `when(params, *, by)` | 1 つの `ActionParams` を、コンシューマー `by`（必須）として権限管理下で実行します。直前のステップが失敗し、`then_error` で検証されていない場合は、先に `AssertionError` を送出します。 |
| `then(cls, pk, **fields)` | 直前のステップが成功していることを要求し、指定した各プロパティを、マスキングされていない現在の行と `==` で比較します。宣言されていないフィールドは `INVALID_RECORD` になります。 |
| `then_result(expected)` | 成功していることと、戻り値が `expected` と等しいことを要求します。 |
| `then_error(code)` | `code` で失敗していることと、現在のオブジェクトとリンクがステップ前の状態と等しいことを要求します。失敗を検証済みにするのはこのメソッドだけです。 |
| `then_absent(cls, pk)` | 現在の行が存在しないことを要求します。成功後でも失敗後でも使えます。 |
| `then_link(handle, from_, to)` | リンクが現在の状態に存在することを要求します。 |
| `then_no_link(handle, from_, to)` | リンクが現在の状態に存在しないことを要求します。 |

検証に失敗すると `AssertionError` になります。すべてのシナリオは `then*` で終わります。
`when` で終わるシナリオは何も検証しません。

---

## オントロジーを宣言する

`Ontology` コンストラクタ、`@ontology.object`、`link`、`prop`、ルール、選択肢と struct プロパティ、フィールドマーカー、`ActionParams`、イベント、アドバイザリーな finding。詳細: [オントロジーの宣言](api-authoring.ja.md)

#### Struct プロパティとパラメーター

struct プロパティとパラメーターの契約は [オントロジーの宣言](api-authoring.ja.md#struct-プロパティとパラメーター) を参照してください。

<a id="scope-policy"></a>

## スコープポリシー

宣言されたスコープルール、`ScopePolicy` のフィールド、スコープ解決ヘルパー。詳細: [オントロジーの宣言](api-authoring.ja.md)

## ランタイムとクライアント

`Ontology.bind`、`OntologyRuntime`、`OntologyClient` による共有ランタイムとコンシューマービューのバインド、型付き・動的読み取り、ハイドレーション、リダクションの挙動。詳細: [ランタイムとクライアント](api-runtime.ja.md)

## 読み取り

`StoredObject`、`Lineage`、フィルター、順序、読み取り上限、`Page`/`TypedPage[T]` のページネーション、`traverse`、可視行のカウント、集計、`GuardedQuery` の契約を掲載します。詳細: [読み取り](api-reading.ja.md)

## Action

Action は型付きパラメータと `@ontology.action` で宣言するハンドラの組で、`ActionContext` がハンドラの操作を提供し、権限（Authority）が書き込みと削除を制御し、`AuditEntry` レコードが各試行を記録します。詳細: [Action と Function](api-actions-functions.ja.md)

### `ActionContext`

ハンドラのコンテキストの契約は [Action と Function](api-actions-functions.ja.md#actioncontext) を参照してください。

## Function

`@ontology.function` は、`query.now()` を含む `BoundQuery` のガードされた読み取りから導出値を計算する Function を宣言し、Function の監査境界が記録する call を決定します。詳細: [Action と Function](api-actions-functions.ja.md)

## Capabilities

宣言された Capability はアクションと Function のハンドラに外界の依存を提供し、プロバイダはランタイムまたはクライアント単位でバインドされ、利用はすべて監査されます。詳細: [ランタイムとクライアント](api-runtime.ja.md)

## セキュリティ

`Consumer` が呼び出し元を識別し、スコープの強制、機微度リダクション、min-N の寄与者閾値、本人特定リンクのガードがアクセスを制御します。詳細: [ランタイムとクライアント](api-runtime.ja.md)

<a id="stores"></a>

## ストア

`Store` プロトコル、SQLite と Postgres バックエンド、date/datetime の値の規則、スキーマバージョニング。詳細: [ストアとバルク取り込み](api-stores.ja.md)。

### date と datetime の値

値の書き込み規則は [ストアとバルク取り込み](api-stores.ja.md#date-と-datetime-の値) を参照してください。

## バルク取り込み

`bulk_upsert`、`bulk_link`、クライアントの取り込みメソッド、検証と取り込みレポート。詳細: [ストアとバルク取り込み](api-stores.ja.md)

<a id="mcp-server"></a>

## MCP サーバー

`build_mcp_server` はイントロスペクション、読み取り、アクション、Function の 14 個のガード付きツールを公開し、`build_multi_consumer_mcp_server` は共有ランタイムで多数の検証済みアイデンティティに配信します。詳細: [MCP サーバー](api-mcp.ja.md)

<a id="descriptor-authoring"></a>

## 記述子による宣言

記述子による宣言、`Declarations` / `declarations(...)`、レガシーハンドラ。詳細: [オントロジーの宣言](api-authoring.ja.md)

<a id="error-codes"></a>

## エラーコード

`ERROR_CODES` レジストリにある安定したコード、7 種別ごとの意味、コード数の概要。詳細: [エラーコード](api-errors.ja.md)

## 例外階層

公開されている例外の親クラス、送出される場面、安定したコードによる捕捉。詳細: [エラーコード](api-errors.ja.md)
