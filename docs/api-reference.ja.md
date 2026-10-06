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

### `StoredObject`

| フィールド | 型 |
| --- | --- |
| `payload` | `dict[str, Any]` |
| `lineage` | `Lineage` |

payload と lineage を分離した凍結オブジェクトです。`_object_type` のようなキーを
紛れ込ませた素の dict では決してありません。

### `Lineage`

`object_type`、`object_id`、`valid_from`、`valid_to`、`source_system`、`source_id`、
`extracted_at`。

### フィルター、順序、読み取り上限

`where` は等価一致の素の scalar、または `gt`、`gte`、`lt`、`lte`、`in`、`ne`、
`contains` をキーとする mapping を受け付けます。複数のキーを持つ mapping は、その
1 つのフィールドに対する AND 条件です。`{"gte": a, "lt": b}` は半開区間の範囲指定、
`{"contains": "x", "ne": "x"}` は 1 つの値を除いた部分文字列一致になります。空の
mapping は拒否されます。比較演算子は宣言済みの `int`、`float`、`date`、`datetime`
プロパティで使えます。`date` と `datetime` の operand は、プロパティが保存するのと
同じ ISO-8601 文字列か、`date` 値・オフセット付きの `datetime` 値（先にその綴りへ
変換されます）です。`datetime` には時刻部分が必要です。日付
だけの文字列は、オフセット付きの列に決して一致しない naive な深夜 0 時として扱われる
のではなく、`OPERATOR_TYPE_MISMATCH` になります。4 つの比較演算子は `datetime` を瞬間
として比較するため、別の UTC オフセットで書いた operand も同じ時刻なら一致します。
保存値と operand のオフセット有無が異なる（naive と aware）行は一致しません。
`datetime` に対する `eq`、`ne`、`in` は従来どおり保存された文字列そのものと一致
させます。`in` は宣言済み型の値の
リストを取り、`ne` はすべての宣言済み型に使え、`contains` は `str` の部分文字列
検査です。未知の
演算子は `UNKNOWN_OPERATOR`、プロパティ型に合わない演算子または operand は
`OPERATOR_TYPE_MISMATCH` になります。この operand の型検査は、等価比較の唯一の
書き方であるベアのスカラーにも適用されます。例外は `None` で、これは null 判定と
して扱われ、そのプロパティを持たない行を選びます。宣言型が `float` のプロパティに
`int` の operand を渡すのは型の拡大であり、mismatch ではありません。mapping 形式の
`where` は、型付き、文字列、aggregate、Function、MCP のすべての読み取り surface で
共通です。

struct プロパティに対する `where` 条件は `OPERATOR_TYPE_MISMATCH` になります。
メッセージは `where is not supported on struct property 'amount'` のようにプロパティ名を
示します。`amount.currency` のような内側のパスでは検索できません。内側のフィールドは
トップレベルのプロパティではありません。

mapping 値は演算子構文なので、宣言済み `json` プロパティの等価比較を
`where={"data": {"kind": "a"}}` とは書けません — その mapping は値ではなく演算子として
解釈されます。1 要素の `in` リストで包んでください:
`where={"data": {"in": [{"kind": "a"}]}}` が等価比較の回避策です。

`DirectProperty` として宣言されたスコープルーティングキーが隠しフィールドである
場合、スコープキーの例外は、素の `eq` 値による等価一致と、明示的なリストを
operand とする単独の `in` に限られます。`gt`、`gte`、`lt`、`lte`、`ne`、`contains`、
リストでない `in`、不正な shape、および複数キーの mapping（`in` を含むものも）は、
呼び出し側に値を学習させるため `VISIBILITY_DENIED` で拒否されます。operand は実際に値を供給する必要があります。
`str`、`int`、`float`、`bool`、`date` のいずれかです。したがって `None`、`None` を
含むリスト、空の `in` リストも拒否されます。素の null は事前知識を必要とせず、
行ごとの null 探索になるためです。読める フィールドに対する null 絞り込みは
従来どおりです。`where` の mapping と、その中の各条件は、公開境界で一度だけ
読み取ります。すべての gate と行マッチャーはその一つのスナップショットを使うため、
二度目の読み取りで別の答えを返す mapping が、ある述語として分類されながら別の
述語として実行されることはありません。

`where` は mapping であるか、省略されるかのどちらかです。文字列・リスト・数値
などはすべて、どの読み取り surface でも `INVALID_PARAMS` になります。*偽値*も
同じです。`""`、`0`、`[]`、`False` は「フィルターなし」として扱わず拒否します。
空の **mapping** `{}` は従来どおりフィルターなしを意味し、`None` も同じです。

`order_by` は宣言済み payload フィールドを受け付け、デフォルトでは昇順になります。
`(field, "asc"|"desc")` の組で方向を明示できます。未知のフィールドは
`UNKNOWN_FIELD`、隠されたフィールドは `VISIBILITY_DENIED` で拒否されます。
隠されたスコープキーも対象です。返される順位が値を開示するためです。
`group_by` にもスコープキーの例外はありません。返される dict のキーがグループ値を
開示するためです。ページカーソルとも組み合わせられます。

consumer surface（`OntologyClient` と `GuardedQuery`）では、`limit` を省略すると
`DEFAULT_READ_LIMIT=1000` が適用され、`Page`（型付き呼び出しでは `TypedPage`）が返ります。
`limit=None` を明示する場合は上限なしのリスト形式になります。宣言された Function 内では
`BoundQuery.list` は `limit` を省略すると上限なしで bare list を返し、正の `limit` を指定した
場合に `Page` が返ります。

### ページネーション — `Page` / `TypedPage[T]`

どちらも `items` と不透明な `next_cursor: str | None` を持ちます。consumer surface では
`limit` の省略で上記の既定上限を使ったページになり、`limit=None` は明示的な上限なしリストの
指定です。宣言された Function 内では `BoundQuery.list` は `limit` の省略または `None` で bare
list を返し、正の `limit` を渡すとページを返します。

契約:

- **ページサイズは正確。** 可視な行がまだ `limit` 件残っている限り、ページはちょうど
  `limit` 件を持ちます — ストア読み取りより下流のフィルタがページを短くすることは
  ありません。短いページは常に「もう行が無い」を意味し、「一部が隠された」ではありません。
- **カーソルは不透明。** 行ごとのランダムなトークンです。行 id でも件数でも順序でも
  ありません。パースせず、保存して `after=` に返すだけにしてください。
- **`next_cursor is None` は「埋まる前に尽きた」の意味。** 最後の行でちょうど埋まった
  ページもカーソルを持ち、次の呼び出しが最後の空ページを返します。よって
  `while next_cursor is not None` のループは 1 ページ早く止まることなく正しく終了します。
- **順序なしの walk では、途中の更新で行が重複することはありますが、取りこぼしは
  起こりません。** ストアは close-old / insert-new です。順序付き walk では、ソート値が
  カーソルより前に移動した行は静かに取りこぼされ、後ろに移動した行は重複します。
  順序付き walk が欠落も重複も起こさないのは、ストアが静止している場合だけです。
- `limit` なしの `after` → `AFTER_WITHOUT_LIMIT`。`limit < 1` → `INVALID_LIMIT`。
  不正または未知のカーソル → `INVALID_CURSOR`。順序付き walk は、自身のカーソル行への
  書き込み（retire や行を差し替える `update` を含む）の後は再開できず、`STALE_CURSOR`
  になります。先頭ページからやり直してください。

**順序付きページのスケール上の注意。** 順序付きページは現状、`limit` や `where` の
絞り込みに関わらず、毎回そのオブジェクト型全体を展開してソートします。実測した
20,000 行のケースでは、`order_by` ありの `limit=10` が 20,000 行すべてを読み、
なしでは 500 行でした。1 ページあたり O(N log N)、順序付き walk 全体で O(N² log N)
です。

### `traverse`

型付き形式は `client.traverse(link_cls, from_obj_or_id)` です。文字列形式は
`client.traverse("Comment", "commentOnTicket", comment_id)` のように、ソース型・
リンク API 名・ソース id をこの順で渡します。Function 内の `BoundQuery` も同じ
ハンドル先頭の型付き形式を受け付けます。

返される対象行にも通常どおり可視性チェックが適用されます。identity-revealing な
リンクの traverse は、human コンシューマーに対しては対象を返す前に `VisibilityError`
（`VISIBILITY_DENIED`）になります。AI コンシューマーには対象行への通常のスコープと
sensitivity の強制が適用されます。`reverse=True` はリンクの対象側から辿り、
identity-revealing の拒否は双方向で対称です。

### 可視行のカウント

`count(obj_type, where=None)` は、コンシューマーのスコープと行可視性のチェックを
適用した後に一致する行数を返します。`exists(...)` は同じ post-visibility の選択が
空でないかを返します。どちらも `list` と同じ `where` 演算子文法を受け付けます。
一致する行すべてからスコープ外に置かれたコンシューマーは、プライバシー拒否ではなく
`count` から `0`、`exists` から `False` を受け取ります。

この 2 つの操作は意図的に min-N の対象外です。開示するのは post-visibility
フィルター後の行集合のサイズだけであり、コンシューマーは
`list(..., limit=None)` で同じ行を列挙できるため、min-N 拒否を加えても開示保護は
増えません。`count_contributors` は引き続き唯一のプライバシー計数プリミティブです。
可視行のカウントとは異なり、集計の背後にある distinct な貢献者母集団を解決するため、
集計と同じ min-N のリリース規律を保ちます。

### 集計

`aggregate(...)` と `aggregate_by(..., group_by=...)` は
`func="mean"|"count"|"sum"|"min"|"max"` を受け付けます。デフォルトは従来どおり
`"mean"` です。グループなしでは `count` が `int`、その他の関数が `float` の値を返し、
グループ付きでは各グループに対応する値を dict で返します。

`func="count"` は宣言済みのどのフィールド型でも受け付けます。そのフィールドを
持つ行数を数えるだけで、値を `float` に変換することはありません。`value_field` も
省略（または `None` を渡す）でき、その場合は選択範囲内の可視な行すべてをカウント
します。min-N はそれらの行の寄与者に対して（グループ付きならグループごとに）
適用されます。`count` 以外の func で `value_field` を省略すると `INVALID_PARAMS`
になり、メッセージは func 名を挙げて `value_field` が必須であると伝えます。

両者は `where`/`group_by` の隠しフィールド検査、**異なる寄与者**に対する min-N、
型が宣言していない `value_field` に対する `UNKNOWN_FIELD`、そして `mean`/`sum`/`min`/`max`
の下で宣言済みだが非数値の `value_field` に対する `NON_NUMERIC_AGGREGATE`
（`count` は対象外です。どちらの検査も行を 1 つも読む前に行われます）を強制します。
隠された `value_field` を集計できるのは、`contributor_rules` を宣言した型に
対する、著者が宣言した Function から、`func="mean"` または `func="count"` を使う場合だけです。
コンシューマーの surface — client、型付き、MCP — からこの操作を行うと
`VisibilityError` と `VISIBILITY_DENIED` になります。`sum`、`min`、`max` は引き続き拒否されます。
すべての関数に同じ min-N の公開判定が適用されます。可視な選択が
空の場合は、グループなしでもグループ付きでも `MIN_N_VIOLATION` になります。グループ付き
の空集合 `{}` も同じように拒否され、空の dict は返りません。

`group_by` は、この surface の他のフィールド名と同じように検証されます。型付き
surface だけでなく、**すべての** surface が対象です。型が宣言していない名前は
`where` のキーや `order_by` と同じく `UNKNOWN_FIELD` になります。どの行にも一致せず、
グループなしの値をキー `"None"` で返すことはありません。falsy な `group_by` が黙って
非グループ経路に落ちることもありません。ただしそちらのコードは surface によって
異なります。文字列 surface と `BoundQuery` では `INVALID_GROUP_BY`、型付き surface
では（クラスプロパティ検査が先に走るため）`UNKNOWN_FIELD` です。

`json` として宣言された `group_by` は `INVALID_GROUP_BY` になります。その値は `dict` や
`list` になり得るため、ハッシュ可能とは限らないからです。検査対象は保存された値では
なく**宣言された型**です。したがって、たまたまスカラーしか保持していない `json`
プロパティも拒否されます。最初の `dict` が届くまで動いてしまうことはありません。
struct プロパティを `group_by` に指定した場合も `INVALID_GROUP_BY` になります。宣言済みの
struct 値はグループキーとしてサポートされません。

異なる 2 つのグループ値が同じ dict キーとして公開される場合は `GROUP_KEY_COLLISION`
になります。多くはオプショナルなプロパティが原因です。値を持たない行のキーが `None` に
なり、文字列リテラル `"None"` を持つ行と衝突します。公開されるセル 1 つが記述できる
母集団は 1 つだけです。この検査は各グループの公開時に、そのグループの min-N 判定の
あとで走ります。min-N が差し止める選択は引き続き `MIN_N_VIOLATION` になります。

`aggregate`、`aggregate_by`、`count_contributors` は、未登録のオブジェクト型を
`UNKNOWN_OBJECT_TYPE` で拒否します。これは `where=` を渡したときの従来の挙動と
同じです。

### `GuardedQuery(store, registry, policy)`

呼び出しごとに明示的な `consumer` を取る、エンジンレベルの読み取り経路:
`.get_object`、`.get_objects`、`.traverse`、`.aggregate`、`.aggregate_by`、
`.count`、`.exists`、`.count_contributors`。通常の呼び出し側は代わりに
`OntologyClient` を使います。

---

## Action

Action は、型付きパラメータクラスと、
`@ontology.action(params_cls, target=..., roles=[...], capabilities=())`
でデコレートしたハンドラの組です。

`execute` はすべて同じパイプラインを通ります。

**登録済みか** → **ロールは許可されているか** → **宣言された target/scope パラメータを
スコープがカバーするか** → **事前条件** → **トランザクション内の副作用** → **追記専用の監査**

すべての試行が監査されます — `ok`、`denied`、`error` のいずれも。

### `ActionContext`

ハンドラが生のストアハンドルの代わりに受け取るもの。書き込みには Action 自身の
`Source` が自動で刻印されます。

**型付きメンバー。** オントロジー自身のクラスと `LinkHandle` を受け取ります。そのため
`mypy` がクラス、戻り値の型、リンクの各端点の型、`save` 前にハンドラが代入する属性を
検査します。`create` のキーワード名は実行時にだけ検査されます。

| メンバー | 用途 |
| --- | --- |
| `.get(cls, obj_id) -> T \| None` | 現在のオブジェクトを 1 件読む |
| `.all(cls) -> list[T]` | `cls` の現在のオブジェクトすべて |
| `.create(cls, **values) -> T` | 作成し、そのオブジェクトを返す。primary key を省略するとランタイムの `id_factory` から採番する。すでに有効な primary key は `OBJECT_ALREADY_EXISTS` で拒否される。宣言されていないプロパティ名は `INVALID_RECORD` で拒否される |
| `.save(obj)` | このコンテキストが `obj` を渡した時点から変わった宣言済みプロパティだけを書く。変更がなければ何も書かない。`get`・`all`・`create`・`traverse` で得たオブジェクトだけを保存できる（それ以外は `OBJECT_NOT_LOADED`）。primary key の変更は `PRIMARY_KEY_IMMUTABLE` で拒否される |
| `.link(handle, from_, to)` | リンク作成。各端はオブジェクトかその id で、型は handle が決める。両端はリンク型が宣言する端点型の有効なオブジェクトである必要があり、そうでなければ `LINK_ENDPOINT_NOT_FOUND` で拒否。有効なリンクと同一なら no-op |
| `.unlink(handle, from_, to)` | 1 本の live link を閉じる。端の渡し方は `link` と同じ |
| `.traverse(handle, anchor) -> list[To]` | `anchor` からリンクされた `To` オブジェクト。`reverse=True` ならそこへリンクしている `From` オブジェクト |
| `.retire(obj)` / `.retire(cls, obj_id)` | オブジェクトをリタイアし、接続するすべての有効なリンクを閉じる |

`create` に渡す値や `save` 前に代入する値が `date` / `datetime` の場合は
[date と datetime の値](api-stores.ja.md#date-と-datetime-の値) に従います。拒否された値は `INVALID_RECORD` になります。

型付きハンドラは、オブジェクトを読み、変更し、保存します。

```python
order = ctx.get(Order, params.order_id)
order.status = "shipped"          # mypy がフィールド名と型を検査する
ctx.save(order)                   # `status` だけを書く
```

**0.18.0 で削除されました。** 以下の型付きメンバーへ移行してください。
文字列形式の `retire` と `unlink` は `ValidationFailed` を送出します。
`code` は `INVALID_PARAMS` です。
`unlink` は位置引数専用です。キーワード引数で呼ぶと `TypeError` が発生します。

| 削除された呼び出し | 型付きの移行先 |
| --- | --- |
| `.insert(obj_type, payload)` | `.create(cls, **values)` |
| `.update(obj_type, obj_id, changes)` | `.get(cls, obj_id)` + 代入 + `.save(obj)` |
| `.create_link(link_api_name, from_id, to_id)` | `.link(handle, from_, to)` |
| `.retire(obj_type, obj_id)` | `.retire(obj)` / `.retire(cls, obj_id)` |
| `.unlink(link_api_name, from_id, to_id)` | `.unlink(handle, from_, to)` |
| `.read_current(obj_type, obj_id)` | `.get(cls, obj_id)` |
| `.read_all(obj_type)` | `.all(cls)` |
| `.links_from(link_api_name, from_id)` | `.traverse(handle, anchor)` |
| `.links_to(link_api_name, to_id)` | `.traverse(handle, anchor, reverse=True)` |

**その他のメンバー。**

| メンバー | 用途 |
| --- | --- |
| `.capability(handle) -> P` | 宣言済み Capability の取得 |
| `.consumer` | 呼び出し元の `Consumer` |
| `.emit(event, *, about=None)` | この呼び出しに、宣言済みのイベントを記録する。「オントロジーを宣言する」のイベントの節を参照 |
| `.now() -> datetime` | 呼び出しの唯一の時刻（`Ontology.bind` を参照）。`datetime.now()` や時刻用 Capability の代わりに使います |

`get` と `all` は信頼されたハンドラ向けの読み取りです。生データを返し、redaction と scope の制限を適用しません。
consumer 向けの guarded query ではありません。scope や sensitivity で列挙を絞ると、id allocator から既存行が隠れます。
その結果、id が再利用される可能性があります。

`retire` と `unlink` がハンドラーから使う SDK の唯一の除去操作です。これらはエンジンが所有する
Action transaction の内側でだけ呼び出せます。`retire` はオブジェクトの current row を
閉じ、そのオブジェクト型についてリンク型が宣言している側でそのオブジェクトを参照するすべての
live link を同じ transaction で cascade-close します。オブジェクトと各リンクの closure は
`AuditEntry.writes` に記録されます。
`unlink` は一致する 1 本の live link を閉じます。

事前条件の失敗には `ActionError` を送出します。0.6.0 から `code` は必須です —
`PRECONDITION_FAILED` か、独自の安定コードを指定してください。

### 権限（Authority）

宣言されていない書き込みは拒否されます。型全体が `owned=True` でないオブジェクト型を
Action が作成しようとすると `SOURCE_CREATE_REFUSED`、オントロジー所有と宣言されていない
ソース由来プロパティの更新やソース由来リンクの作成は `UNDECLARED_SOURCE_WRITE` に
なります。ソースデータとオントロジー所有の状態は分離されたままです。

削除にも同じ authority 境界が適用されます。`retire` にはオブジェクト型全体の
`owned=True` 宣言が、`unlink` にはリンク型の `owned=True` 宣言が必要です。そうでなければ
`UNDECLARED_SOURCE_REMOVAL` になります。オブジェクトの retirement cascade がソース由来
リンクに達した場合は、先に閉じたリンクも含めて Action transaction 全体がロールバック
されます。

呼び出し側がすでにストアトランザクションを保持している状態での `execute()` は拒否
されます（`CALLER_TRANSACTION_REFUSED`、監査対象外）。さもないと、適用され監査された
Action が監査ログの下でロールバックされうるためです。

### `AuditEntry`

`ts`、`actor`、`role`、`action`、`target_type`、`target_id`、`params`、`outcome`、
`invocation_id`、`error_code`、および完全性レコード: `writes: list[WriteRecord]`、
`capability_accesses: list[CapabilityAccessRecord]`、`events: list[EmittedEvent]`。

**`kind: Literal["action", "function"]`** — このエントリを生成したもの。Action と
Function は 1 つのログを共有するため、読み手が両者を区別する手段が `kind` です（同じ
`api_name` の Action と Function を宣言することを妨げるものは何もありません）。
`function` エントリでは `action` に Function の api_name が入り、`target_type` は `""`
（Function に対象オブジェクト型はありません）、`writes` は常に空です。

**`invocation_id: str | None`** — `execute()`（および監査対象の `call_function()`）
呼び出しごとに 1 つの id で、その呼び出しが書き込む**すべて**のエントリに刻印されます。
エントリを対応づけるときは、フィールド一致と追記順に頼らずこの値を使ってください — 同じ Action を
同じパラメータで 2 回呼ぶと、それ以外では区別できません。`None` はこのフィールドが存在
しなかった頃のエントリ（古いエンジンが書いたストアファイル）を意味し、後から捏造される
ことはありません。

**`events: list[EmittedEvent]`** — `ok` の action エントリが送出したイベントを、送出順に
マスクせず列挙します。監査は管理者向けのビューだからです。`denied` と `error` のエントリでは
常に空です。

**`unscoped_params: list[str]`** — `scope="unscoped"` の型を指すため、Action の
スコープゲートを通らなかった `target(...)` パラメータの一覧です。これらのパラメータでは、
Action の `roles=` だけがゲートでした。スコープを持つパラメータがすべてスコープ検査を
受けた場合は空です。ゲートより前に書かれたエントリ（ロールによる拒否など）でも空です。

**`target_id: str | None`** — `action` エントリでは、呼び出し側が Action の宣言済み
対象パラメータに渡した id です。`denied` と `error` を含むすべての結果で記録されます。
その id が存在しない場合も記録します。Action が対象パラメータを宣言していない場合と、
すべての `function` エントリでは `None` です。

**`error_code: str | None`** — `denied` または `error` のエントリが失敗した理由です。
送出された例外のカタログ済み `code`（`PERMISSION_DENIED`、`SCOPE_DENIED`、
`INVALID_PARAMS`、ハンドラ独自の `ActionError` のコードなど）が入ります。ハンドラが
送出した素の `KeyError` のように `code` を持たない例外は、MCP サーバーと同じく
`INTERNAL_ERROR` として記録します。`ok` エントリでは `None` です。

- `WriteRecord` — `op`（`create`/`update`/`link`）、`object_type`、`link_type`、
  `object_id`、`from_id`、`to_id`
- `CapabilityAccessRecord` — `api_name`、`count`
- `EmittedEvent` — `event_type`、`about_type`、`about_id`、`payload`（保存形式）

---

## Function

Function ハンドラは `BoundQuery` を受け取ります。型付き Function は `FunctionParams` の
サブクラスも `@ontology.function(params_cls, ...)` で宣言します。`FunctionParams` は未知のフィールドを
拒否します。その型注釈が入力検証と MCP のパラメータスキーマを定義します。ストアハンドルはハンドラに一切届かず、
すでにガードされた読み取りだけが渡ります。Function は導出値を返し、書き込みは行いません。

```python
class TicketStatsParams(FunctionParams):
    queue_id: str

@ontology.function(TicketStatsParams, api_name="ticketStats")
def ticket_stats(query: BoundQuery, params: TicketStatsParams) -> float:
    mean = query.aggregate("Ticket", "age_hours", where={"queue_id": params.queue_id})
    assert isinstance(mean, float)
    return mean

client.call_function(TicketStatsParams(queue_id="q1"))
```

Function の宣言には 2 つの形式があります。型付き Function では `FunctionParams` の
サブクラスを宣言します。上の例のようにインスタンスを渡すか、関数名と dict を渡せます
（例: `client.call_function("ticketStats", {"queue_id": "q1"})`）。dict は検証されます。
ハンドラには `TicketStatsParams` のインスタンスが渡されます。
入力がない Function は params クラスを省略し、`query` だけを受け取ります。

```python
@ontology.function(api_name="health")
def health(query: BoundQuery) -> bool:
    return query.exists("Ticket")

client.call_function("health")
client.call_function("health", {})
```

params クラスを指定しない場合、ハンドラは既定値なしの `query` をちょうど 1 つだけ受け取る必要があります。
それ以外のハンドラは、宣言時に `ValidationFailed` と `code="ONTOLOGY_INVALID"` で拒否されます。
`(query, params: dict)` などの dict 形式のハンドラは 0.20.0 で削除しました。
代わりに `FunctionParams` サブクラスを宣言してください。

未知のフィールド、必須フィールドの不足、不正な型、宣言した選択肢にない値は、ハンドラの実行前に
`ValidationFailed` と `code="INVALID_PARAMS"` を送出します。入力がない Function に空でない params を
渡した場合も、同じコードで拒否します。

`FunctionDef.parameters` と MCP の `list_functions` は、型付き入力を `name`、`type`、`choices`、
`fields`、`required`、`refers_to` を持つパラメータとして公開します。これは `scope_semantics` を
除いた Action パラメータと同じ形式です。入力がない Function では `[]` になります。
`client.call_function` は、関数名でも `FunctionParams` のインスタンスでもない引数を
`ValidationFailed` と `code="INVALID_PARAMS"` で拒否します。

### `BoundQuery`

コンシューマーを固定した `GuardedQuery`: `.get`、`.list`、`.count`、`.exists`、`.traverse`、
`.aggregate`、`.aggregate_by`、`.count_contributors`、`.capability(handle)`、`.now()`。型付きオーバーロードは
`OntologyClient` と同様に機能します（`query.get(Ticket, id) -> Ticket | None`）。

`query.now()` は、ランタイムにバインドされた clock（`ctx.now()` と同じ clock）から、その call の
単一の時刻を返します。clock は最初の呼び出しで読まれ、同じ Function call 内の以降の呼び出しは
その時刻を返します。時刻に依存する Function では、`datetime.now()` や clock の Capability ではなく
これを使います。Capability を宣言すると、すべての call が監査対象になります。

```python
@ontology.function(api_name="overdueIds")
def overdue_ids(query: BoundQuery) -> list[str]:
    now = query.now()
    return [order.id for order in query.list(WorkOrder) if order.due < now]
```

入力がない Function は、引数が 1 つのハンドラ `(query)` として宣言し、params なしまたは `{}` で呼び出します。

`PreconditionFailed`（`FUNCTION_ERROR`）は、未宣言の api_name、重複登録、ハンドラ未バインドを
カバーします。

### Function の監査境界

監査対象となる call では、`OntologyClient.call_function` が `kind="function"` の監査エントリを 1 件追加します —
invocation id、params、outcome、handler の `capability_accesses` を記録します。`writes`
は構造上空です。

Function を監査するかは `FunctionDef.audited` による**条件付き**です。

| 宣言 | 監査されるか |
| --- | --- |
| capability を宣言 | ✅ yes（デフォルト） |
| 何も宣言しない | ❌ no（デフォルト） |
| `@ontology.function(audit=True)` | ✅ yes、常に |
| `@ontology.function(audit=False)` | ❌ no — ただし release の場合を除く |

Function は Action よりはるかに頻繁に実行されます。Capability を宣言しない Function は
プロセス外に到達できず、読むものはすべて guarded query layer によってすでに制限されます。
すべての call を監査すると、得られるものに対して write amplification が大きすぎるため、
デフォルトでは監査人が実際に確認する意味のあるケースを記録します。

エラーパスも監査されます。ハンドラが外界に到達してから raise した場合、その時点ですでに
世界に影響を与えているためです。

**hidden field の release は宣言にかかわらず監査されます。** guarded query layer の境界には
contributor rules を持つ型の作者が宣言した Function に対する免除が含まれるため、
Capability のない Function でも、可視の母集団全体に限り、consumer が自分では読めない
field に対する `mean` や `count` を返せます。実際にそれを
行った call では `call_function` がエントリを追加します — `audit=False` も例外ではありません。
個人に結びつく数値をリリースしたことを、ontology が監査対象外にできないためです。

監査の trace は宣言ではなく**release**に従います。

| call | 監査されるか |
| --- | --- |
| exemption を通じて hidden field をリリースした | ✅ yes、宣言にかかわらず |
| consumer がもともと読める field を集計した | 上表どおり |
| exemption を開いたが min-N で拒否された | 上表どおり — 何もリリースされない |
| ontology 内のどこかで `contributor_rules` を宣言した | 上表どおり — 宣言だけでは release ではない |

1 つの call に監査理由が 2 つあっても、追加されるエントリは 1 件です。

裸の `FunctionRegistry.call` には**境界がありません** — この guarantee は、write gate が
store ではなく `execute()` に属するのと同じく、client surface に属します。

---

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
