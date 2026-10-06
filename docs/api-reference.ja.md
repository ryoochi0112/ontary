# `ontary` — API リファレンス

[English](api-reference.md) · **日本語** · [← README](../README.md)

*リファレンス* — `ontary` の公開名を調べるための一覧ページで、使い方は[はじめに](getting-started.ja.md)と[オントロジーのテスト](testing.ja.md)で確認できます。

`ontary` のキュレーションされたフロントドア: `__all__` の **46 個の名前**。
残りのエンジン API は、`ontary.meta`、`ontary.store` などの
定義元サブモジュールから利用します。

これは調べ物のためのドキュメントです。「宣言する → バインドする → 読む → 配信する」
という流れの解説は [README](../README.md) から始めてください。

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

## スコープポリシー

宣言されたスコープルール、`ScopePolicy` のフィールド、スコープ解決ヘルパー。詳細: [オントロジーの宣言](api-authoring.ja.md)

## ランタイムとクライアント

```python
runtime = ontology.bind(store, capabilities={...})                  # 1 回だけ
client  = runtime.for_consumer(consumer)                            # リクエストごとに安価に
```

### `Ontology.bind(store, *, clock=None, id_factory=None, capabilities=None)`

`clock` はタイムゾーン付き `datetime` を返す callable で、デフォルトは
`datetime.now(timezone.utc)` です。`id_factory` は `str` を返す callable で、デフォルトは
UUID 形式の ID です。どちらも共有ランタイムに保存され、すべての
`for_consumer()` ビューに引き継がれます。

`clock=c` を付けてバインドすると、`c` がストアに設定されます。以降、そのストアへの
すべての書き込みが `c` を使います。対象は、アクションの書き込み、
`ontary.ingest.bulk_upsert` / `bulk_link`、ストアの直接呼び出しです。
`valid_from` / `valid_to` はこのクロックから決まります。

- **1 回の呼び出しにつき 1 つの時刻。** アクションはクロックをちょうど 1 回読みます。
  その時刻が、`ctx.now()` の戻り値、アクションが書き込むすべての行とリンクの
  `valid_from` / `valid_to`（`retire` が閉じるリンクを含む）、その呼び出しのすべての
  監査エントリ（ok、denied、error）の `ts` になります。
- **`CLOCK_CONFLICT`。** 同じストアを別のクロックオブジェクトでバインドすると、
  `PreconditionFailed` が発生します。同じクロックオブジェクト、または `clock=` なしなら
  問題ありません。`clock=` なしでバインドしたランタイムは、ストアに設定済みのクロックを使います。
- **`CLOCK_REGRESSION`。** 閉じる行やリンクの `valid_from` より前の時刻での書き込みは、
  `PreconditionFailed` になります。アクション内ではアクション全体がロールバックされ、
  error 監査エントリがコードを記録します。同じ時刻は許可されます。
- **`CLOCK_NOT_TIMEZONE_AWARE`。** naive な `datetime` を返すクロックは
  `ValidationFailed` になり、何も保存されません。

先にバインドしてから、シードしてください。別のクロックでシードしたデータがあると、
過去に設定したクロックでの最初の更新や retire で `CLOCK_REGRESSION` が発生することがあります。

### `OntologyRuntime(ontology, store, handlers=None, *, clock=None, id_factory=None, capabilities=None)`

1 つの `(ontology, store)` ペアに対する、コンシューマー非依存の共有機構 — クエリ層、
Action 実行器、バインド済みハンドラ — をちょうど 1 回だけ配線します。

- **`.for_consumer(consumer, *, capabilities=None) -> OntologyClient`** —
  安価なビュー。1 プロセスで多数のコンシューマーを捌いても、再配線は起きません。
### `OntologyClient(ontology, store, consumer, *, capabilities=None)`

ちょうど 1 つの `(ontology, store, consumer)` に束縛されます。直接構築しても動作し、
その場合は内部で使い捨てのランタイムを構築します。

| メソッド | 戻り値 |
| --- | --- |
| `.get(obj_type, obj_id)` | `T \| StoredObject \| None` |
| `.list(obj_type, where=None, *, limit=DEFAULT_READ_LIMIT, after=None, order_by=None)` | `list[T] \| list[StoredObject] \| TypedPage[T] \| Page` |
| `.traverse(obj_type, link, from_id, *, reverse=False)` または `.traverse(link_cls, from_obj_or_id, *, reverse=False)` | `list[T] \| list[StoredObject]` |
| `.aggregate(obj_type, value_field=None, where=None, *, func="mean")` | `float \| int` |
| `.aggregate_by(obj_type, value_field, group_by, where=None, *, func="mean")` | `dict[str, float \| int]` |
| `.count(obj_type, where=None)` | `int` |
| `.exists(obj_type, where=None)` | `bool` |
| `.count_contributors(obj_type, where=None)` | `int` |
| `.execute(params)` または `.execute(action, params)` | `dict[str, Any]` |
| `.call_function(params: FunctionParams)` または `.call_function(api_name: str, params: dict[str, Any] \| None = None)` | `Any` |
| `.ingest(obj_type, records, source, *, on_error="raise")` | `IngestReport`（失敗があれば `IngestError`） |
| `.ingest_links(link_api_name, pairs, source, *, on_error="raise")` | `IngestReport`（失敗があれば `IngestError`） |

**型付き surface と動的 surface。** 作者の*クラス*を渡すと型付きインスタンスが返ります
（`client.get(Ticket, id) -> Ticket | None`。mypy が推論し、キャストは不要）。*文字列*を
渡すと `StoredObject` が返ります — こちらが動的な形で、MCP のワイヤ形式でもあり、汎用
ツールが使う形です。`.traverse` は型付きの場合、`LinkHandle` を第 1 引数に取ります
（`client.traverse(link_cls, from_obj_or_id)`）。

すべての呼び出しの `where` / `order_by` / `group_by` / `value_field` のキーは、
型の宣言済みプロパティと照合されます。文字列形式でも型付き形式でも同じです。
未知のキーは、黙って何にもマッチしたり数値を返したりせず `UNKNOWN_FIELD` に
なります。保存された行がたまたま持っているキーも同じです。宣言されていない
payload キーは書き込みでは許されますが、読み取りで名前指定はできません。
*宣言済みだが隠されている*キーは、値を開示する
操作では `VisibilityError` と `VISIBILITY_DENIED` で拒否されます。スコープルーティングと
Function 集計の狭い例外は、下記の「フィルター」と「集計」で説明します。

オブジェクト読み取りで `limit` を省略すると `DEFAULT_READ_LIMIT=1000` が適用され、
`Page`（型付き呼び出しでは `TypedPage`）が返ります。`limit=None` を明示すると
上限なしのリスト形式になります。正の整数を指定するとページになります。
`order_by` は宣言済み payload フィールド（デフォルトは昇順）、または
`(field, "asc"|"desc")` の組を受け付けます。未知のフィールドは
`UNKNOWN_FIELD`、隠されたフィールドは `VISIBILITY_DENIED` で拒否されます。
`DirectProperty` のスコープルーティングキーであっても同じです。等価一致の
`where` にある例外とは異なり、`order_by` は常に非除外の隠しフィールド検査を使います。

**surface 間の非対称性が 2 つあります。**どちらも見落としやすい点です。

> **ハイドレーション。** 型付き読み取りは `datetime` 型のプロパティを実際の
> `datetime` にパースします（Pydantic 経由）。文字列 surface は保存されたままの
> ISO-8601 文字列を返します。ストアが永続化する内容を書き換えることはありません —
> 型付きクライアントの取り出し時のハイドレーションだけがパースします。書き込みで
> 受け付ける値は [date と datetime の値](api-stores.ja.md#date-と-datetime-の値) を参照してください。

> **リダクションの形。** リダクションされたフィールドは、型付き surface では `None`
> として返り、名前が `redacted_fields` に載ります。しかし**文字列 surface と MCP では
> そのキー自体が `payload` から消えます** — `redacted_fields` に相当するものもありません。
> MCP 越しに読むときは防御的に（`payload["email"]` ではなく `payload.get("email")`）、
> そしてキーが無いことを「保存されていない」と解釈しないでください。単にあなたから
> 隠されているだけかもしれません。

---

## 読み取り

`StoredObject`、`Lineage`、フィルター、順序、読み取り上限、`Page`/`TypedPage[T]` のページネーション、`traverse`、可視行のカウント、集計、`GuardedQuery` の契約を掲載します。詳細: [読み取り](api-reading.ja.md)

## Action

Action は型付きパラメータと `@ontology.action` で宣言するハンドラの組で、`ActionContext` がハンドラの操作を提供し、権限（Authority）が書き込みと削除を制御し、`AuditEntry` レコードが各試行を記録します。詳細: [Action と Function](api-actions-functions.ja.md)

### `ActionContext`

ハンドラのコンテキストの契約は [Action と Function](api-actions-functions.ja.md#actioncontext) を参照してください。

## Function

`@ontology.function` は、`query.now()` を含む `BoundQuery` のガードされた読み取りから導出値を計算する Function を宣言し、Function の監査境界が記録する call を決定します。詳細: [Action と Function](api-actions-functions.ja.md)

## Capabilities

ハンドラが外界に求めるものはすべて**宣言**し、バインド時に提供する必要があります。
未宣言の利用は拒否され、利用はすべて監査されます。Capability はハンドラが読む／呼ぶ
ものです。

```python
Mailer = ontology.capability(MailerProto, name="mailer")

@ontology.action(P, target=T, roles=["Agent"], capabilities=[Mailer])
def handler(ctx, params):
    ctx.capability(Mailer).send(...)
```

現在時刻には Capability ではなく、アクションでは `ctx.now()`、Function では `query.now()` を使います。

未宣言の Capability を要求すると `UNDECLARED_CAPABILITY`、宣言済みでもプロバイダが
バインドされていなければ `CAPABILITY_NOT_PROVIDED` になります。

プロバイダは `ontology.bind(store, capabilities={...})`、またはクライアント単位で
`for_consumer(...)` にバインドします。

---

## セキュリティ

### `Consumer`

| フィールド | 型 |
| --- | --- |
| `actor_id` | `str` |
| `role` | `str` |
| `scope_level` | `str` — オントロジーが宣言したレベルのいずれか |
| `scope_id` | `str` |
| `kind` | `Literal["human", "ai"]` |

エンジンが強制する 4 つの仕組み:

1. **スコープ可視性** — 宣言された `ScopePolicy` を通じて解決。未解決のレベルは拒否。
2. **機微度リダクション** — プロパティごとの `Sensitivity(ai_usable, human_visible)`。
   隠されたフィールドは `None` として読め、`redacted_fields` に名前が載ります。
3. **min-N** — 異なる寄与者が `min_n` 未満の集計は、コード `MIN_N_VIOLATION` の
   `VisibilityError`。寄与者は宣言された `contributor` ルールから求められるため、
   同一人物の複数行では閾値を満たしません。計数の対象は選択された全行ではなく、
   `value_field` を持つ行だけです。`min_n` 人のうち一人だけが回答した集団では、
   その回答は公開されません。寄与者が解決できない行は、一行ごとではなく全体で
   一つの未知の識別子として数えます。リンクを閉じても寄与者を retire しても、
   一人の複数行が公開可能な集団に変わることはありません。
4. **本人特定リンク** — 人間コンシューマーには、対象を解決する前に拒否されます。

`covers_scope(policy, consumer, resolved) -> bool` が唯一のカバー判定ルールであり、
読み取り経路と書き込み経路が共有します。

---

## ストア

`Store` プロトコル、SQLite と Postgres バックエンド、date/datetime の値の規則、スキーマバージョニング。詳細: [ストアとバルク取り込み](api-stores.ja.md)。

### date と datetime の値

値の書き込み規則は [ストアとバルク取り込み](api-stores.ja.md#date-と-datetime-の値) を参照してください。

## バルク取り込み

`bulk_upsert`、`bulk_link`、クライアントの取り込みメソッド、検証と取り込みレポート。詳細: [ストアとバルク取り込み](api-stores.ja.md)

## MCP サーバー

```python
from ontary.mcp_server import build_mcp_server

server = build_mcp_server(ontology, store, consumer, *, name=None,
                          capabilities=None)  # -> MCPServer
```

1 サーバープロセスにつき 1 つの `Consumer` アイデンティティ。宣言済みハンドラは
**バインド済みで届きます** — 登録用コールバックはありません。

12 個のツール。いずれも Python surface と同じガードの対象です。読み取り専用ツールには
`ToolAnnotations(readOnlyHint=True)`、`execute_action` には
`ToolAnnotations(destructiveHint=True)` が付きます。

| ツール | 用途 | Annotation |
| --- | --- | --- |
| `list_object_types` | イントロスペクション | `readOnlyHint=True` |
| `list_link_types` | イントロスペクション | `readOnlyHint=True` |
| `list_action_types` | イントロスペクション（パラメータ定義を含む） | `readOnlyHint=True` |
| `list_functions` | イントロスペクション | `readOnlyHint=True` |
| `get_declarations` | 宣言された契約のバンドル | `readOnlyHint=True` |
| `get_object` | 単一読み取り | `readOnlyHint=True` |
| `query_objects` | フィルタ／ページ付き読み取り | `readOnlyHint=True` |
| `count_objects` | 可視行の件数 | `readOnlyHint=True` |
| `aggregate_objects` | 集計読み取り | `readOnlyHint=True` |
| `traverse_links` | リンクを辿る | `readOnlyHint=True` |
| `execute_action` | Action の実行 | `destructiveHint=True` |
| `call_function` | Function の呼び出し | `readOnlyHint=True` |

`list_object_types` はすべてのプロパティに `transitions` キーを含めます。グラフがない場合は
`null`、ある場合は完全な `initial` 状態リストと `moves` の対応を返します。各オブジェクト型には
`rules` リストもあり、各ルールの `name` と `message` を含みます。ルールのコードは含まれません。

`query_objects(obj_type, where=None, order_by=None, limit=None, after=None)` は MCP surface では常に
上限付きです。`limit` を省略するとサーバーのデフォルト上限 100 行を使い、明示する
場合の最大値は 1000 です。内部のページ付き読み取りが不透明な `next_cursor` を返し、
成功時のレスポンスは従来どおり行を `result` に置いたまま、同じ階層に `next_cursor`
を追加します（消化済みなら `null`）。同じ明示的な `limit` とともにカーソルを返して
次ページを取得してください。明示的な `limit` なしの `after` は
`AFTER_WITHOUT_LIMIT`、1 未満または 1000 超の値は `INVALID_LIMIT` になります。

`where` の文法は、素の scalar を等価一致として使うか、`gt`、`gte`、`lt`、`lte`、
`in`、`ne`、`contains` を指定する mapping 形式です。1 つの mapping に複数の演算子を
書くと AND 条件になり、`{"gte": a, "lt": b}` は範囲指定です。演算子は宣言済み
プロパティ型に対して検証され、未知の演算子は `UNKNOWN_OPERATOR`、型に合わない演算子または
operand は `OPERATOR_TYPE_MISMATCH` になります。date の比較は保存される ISO 日付の順序を使い、
datetime の比較は UTC オフセットをまたいで瞬間で行います。
lineage フィールドは対象外で、未知のキーは `UNKNOWN_FIELD` になります。
`order_by` は宣言済み payload フィールド（デフォルトは昇順）、または
`(field, "asc"|"desc")` の組を受け付け、ページカーソルと組み合わせられます。
`count_objects` はコンシューマーに可視な行の件数を返し、min-N の対象外です。
開示するのは `query_objects` が既に一覧する内容だけであり、min-N でリリース
された件数が必要な場合は `value_field` 不要の `aggregate_objects(func="count")`
を使ってください。
`aggregate_objects` は `func="mean"|"count"|"sum"|"min"|"max"` を受け付け、
デフォルトは `"mean"` です。すべての関数に同じ min-N の公開判定が適用され、
グループ付きの空集合 `{}` も拒否されます。`value_field` は `func="count"` では
省略可能で、省略すると可視な行すべてをカウントします。それ以外の func では必須
であり、指定がなければ `INVALID_PARAMS` になります。
`traverse_links` は、委譲先の `OntologyClient.traverse`／`GuardedQuery.traverse` に
`limit`／`after` とカーソルの API がないため、今回もページなしのリストです。
`reverse=true` を渡すとリンクの target 側から辿って source 側のオブジェクトを返します。
本人特定リンクの拒否は両方向で対称です。

オブジェクトは `{"payload": {...}, "lineage": {...}}` としてシリアライズされます
（`None` は `None` のまま）。エラーは下表のコードを返し、分類できないものは内部情報を
呼び出し側に漏らさないよう `INTERNAL_ERROR` になります。

`mcp` エクストラが必要です。

### マルチコンシューマー配信

```python
from ontary.mcp_server import build_multi_consumer_mcp_server, ConsumerResolver

server = build_multi_consumer_mcp_server(
    ontology, store, *, resolve_consumer, name=None,
    capabilities=None,
    token_verifier=None, auth=None,
)  # -> MCPServer
```

1 サーバープロセスで**多数の証明済みアイデンティティ**を提供します — `consumer` 引数は
ありません。1 つの `OntologyRuntime`（1 つの `GuardedQuery`、1 つの `ActionExecutor`、
一度だけバインドされた宣言済みハンドラ）だけを構築し、各呼び出しは軽量な
`runtime.for_consumer(...)` ビューを取得します。

呼び出しごとに、その request の MCP 検証済み `AccessToken` をトランスポート自身の
contextvar から読み取り、呼び出し元が渡した `ConsumerResolver`
（`resolve_consumer(token) -> Consumer | None`）を呼び、resolver が返った**あとで**
トークン（`subject`、なければ `client_id`）から `Consumer.principal` をスタンプします —
そのため resolver は誰が認証したかを偽装できません。解決結果はキャッシュされません。
失効・再スコープされたトークンが古いバインディングのまま提供されることはありません。

`build_mcp_server` と同じ 12 個のツールで、イントロスペクションを含め同じ 3 通りの
fail-closed 挙動をします。

| 条件 | コード |
| --- | --- |
| request に検証済み `AccessToken` がない（`get_access_token()` が `None` を返す — トークン未提示、または `token_verifier` 未設定の HTTP。stdio では常にこれに該当） | `UNAUTHENTICATED` |
| 検証済みプリンシパルに対して `resolve_consumer` が `None` を返す | `CONSUMER_UNRESOLVED` |
| `resolve_consumer` が意図的な `OntaryError` 以外の例外を送出する | 汎用の `INTERNAL_ERROR` — トークン材料を含みうるため、送出されたメッセージは呼び出し側に届きません |

この SDK はトークンを検証も発行もしません — `token_verifier` と `auth` は MCP 自身の
型です（`mcp.server.auth.provider.TokenVerifier` / `mcp.server.auth.settings.
AuthSettings`）。デプロイヤーが設定し、内部の `MCPServer(...)` 呼び出しへそのまま
渡されます — `MCPServer` は構築後にどちらを設定する public なセッターも公開して
いないため、ここが唯一の配線ポイントです。どちらも渡さないのは stdio 専用、
または意図的に認証なしのデプロイとして正当ですが、どちらか片方だけを渡すのは
実行時の状態ですらありません — `MCPServer.__init__` がその場で `ValueError` を
送出する（構築時点での fail-fast）ため、サーバーは構築されず、呼び出しも一切
発生しません。stdio には認証コンテキストが全くないため、この 2 引数の値に
関わらず stdio 上のマルチコンシューマーサーバーは常にすべての呼び出しを
`UNAUTHENTICATED` で拒否します。1 コンシューマー・stdio プロセスには
`build_mcp_server(ontology, store, consumer)` を使ってください。

**トランスポートのオプションは `run()`/`streamable_http_app()` に渡します。**
ビルダーはセッションモードを強制しません。
mcp 2.x では、`stateless_http`、`json_response`、`transport_security`、`host` は
`run()` と `streamable_http_app()` のキーワード引数です。
ASGI アプリはソケットを bind しないため、`port` は `run()` のキーワード引数です。
stateful セッションでも、各 request はその request 自身のトークンを解決します。
`tests/test_mcp_multi_consumer.py` は ASGI 境界で両方のモードを固定しています。
`build_mcp_server` は構築時に束縛された 1 つの `Consumer` だけを持ち、request ごとに
解決するアイデンティティはありません。

`mcp` エクストラが必要です。

---

## 記述子による宣言

記述子による宣言、`Declarations` / `declarations(...)`、レガシーハンドラ。詳細: [オントロジーの宣言](api-authoring.ja.md)

## エラーコード

`ERROR_CODES` レジストリにある安定したコード、7 種別ごとの意味、コード数の概要。詳細: [エラーコード](api-errors.ja.md)

## 例外階層

公開されている例外の親クラス、送出される場面、安定したコードによる捕捉。詳細: [エラーコード](api-errors.ja.md)
