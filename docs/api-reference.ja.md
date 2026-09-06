# `ontary` — API リファレンス

[English](api-reference.md) · **日本語** · [← README](../README.md)

`ontary` のキュレーションされたフロントドア: `__all__` の **58 個の名前**。
残りのエンジン API は、`ontary.meta`、`ontary.store`、`ontary.connect` などの
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
- [統制された副作用](#統制された副作用)
- [セキュリティ](#セキュリティ)
- [ストア](#ストア)
- [バルク取り込み](#バルク取り込み)
- [`ontary.connect`](#ontaryconnect)
- [MCP サーバー](#mcp-サーバー)
- [記述子による宣言](#記述子による宣言)
- [エラーコード](#エラーコード)
- [例外階層](#例外階層)

---

## フロントドア

`__all__` はソート済み・重複なし・import 可能で、58 個を上限とします。オントロジーの
作者がエンジンの名前空間を選ばずに使う名前だけをここに置きます。

### Authoring vocabulary / 宣言用語彙

`ActionContext`、`ActionParams`、`BaseConnector`、`BoundQuery`、`CanonicalBatch`、
`CanonicalRecord`、`CapabilityHandle`、`Cardinality`、`Consumer`、`CustomResolver`、
`DirectProperty`、`EffectDispatcher`、`EffectHandle`、`EffectMeta`、
`EffectPayload`、`LinkBinding`、`LinkHandle`、`MappingSpec`、`ObjectBinding`、
`Ontology`、`OntologyObject`、`RawTables`、`RowVisibilityStore`、`SelfScope`、
`Sensitivity`、`Source`、`Store`、`ViaLink`、`oid`、`prop`、`ref`、`run_pipeline`、
`scope_ref`、`target`。

### Runtime entries / ランタイム項目

`Declarations`、`DrainReport`、`Finding`、`InMemoryStore`、`ObjectStore`、`OntologyClient`、
`OutboxRecord`、`Page`、`PostgresStore`、`RetryPolicy`、`ScopePolicy`、`TypedPage`、
`__version__`、`build_mcp_server`、`declarations`。

### Error classes / 例外クラス

`ActionError`、`AuthorityError`、`ConflictError`、`InternalError`、`OntaryError`、
`PermissionDenied`、`PreconditionFailed`、`ValidationFailed`、`VisibilityError`。

この 58 個の上限は `tests/test_docs.py` が検証するため、root export の増加を
見落としません。

```python
from ontary import Ontology, OntologyObject, Consumer, prop, target, Cardinality
```

Python 3.12+。コアパッケージの依存は `pydantic` のみ。エクストラ: `[mcp]`（MCP
サーバー）、`[dlt]`（dlt/duckdb によるコネクタ抽出）、`[bq]`（BigQuery 対応を追加）。

---

## エンジン API

以下の名前は意図的にフロントドアへ平坦化していません。エンジンを拡張したり高度な
統合を行ったりするときは、定義元サブモジュールから import してください。
`MappingValidationError` は connector の事前検証用の通常の `Exception` であり、
`ontary.connect` にあります。

### `ontary.actions`

`ActionExecutor`。

### `ontary.audit`

`CapabilityAccessRecord`、`EffectRecord`。

### `ontary.client`

`OntologyRuntime`。

### `ontary.connect`

`LinkSkip`、`MappingValidationError`、`RunReport`、`SourceConnector`、
`SourceLineage`、`map_batch`、`run_dlt_extract`、`to_date`、`to_datetime`、
`to_optional_date`、`to_optional_datetime`。

### `ontary.errors`

`ERROR_CODES`、`ErrorCodeInfo`、`Kind`。

### `ontary.fingerprint`

`OntologyFingerprint`、`fingerprint_ontology`。

### `ontary.functions`

`FunctionHandler`、`FunctionRegistry`。

### `ontary.ingest`

`IngestError`、`IngestReport`、`bulk_link`、`bulk_upsert`。

### `ontary.mcp_server`

`ConsumerResolver`、`build_multi_consumer_mcp_server`。

### `ontary.meta`

`ActionParameterDef`、`ActionTypeDef`、`FunctionDef`、`LinkTypeDef`、
`ObjectTypeDef`、`OntologyRegistry`、`PropertyDef`、`PropertyType`、`ScopeLevel`、
`Upcaster`。

### `ontary.migrate`

`MigrationFailure`、`MigrationReport`、`migrate_object_type`、`upcast_object_type`。

### `ontary.ontology`

`OntologyDef`。

### `ontary.outbox`

`DEFAULT_RETRY_POLICY`、`OutboxState`。

### `ontary.query`

`GuardedQuery`。

### `ontary.scope`

`Direction`、`RowVisibilityFn`、`ScopeRule`、`resolve_contributor`、
`resolve_owning_scope`。

### `ontary.security`

`ConsumerKind`、`covers_scope`。

### `ontary.store`

`AuditEntry`、`DEFAULT_BATCH`、`DEFAULT_TENANT`、`Lineage`、
`SCHEMA_VERSION`、`StoredObject`、`WriteRecord`、`accept_ontology_fingerprint`、
`check_ontology_fingerprint`。

### `ontary.testing`

SDK 利用者向けのテストヘルパーは `make_store`、`consumer`、`raises_code`、
`capture_effects`、`FixedClock`、`SequentialIds` です。`make_store(ontology)` は空の
`InMemoryStore` を新しく作り、`consumer(...)` は有効な `Consumer` を組み立て、
`raises_code(code)` はメッセージではなく機械可読なコードでエラーを検証します
（`ontary.ingest.IngestError` を含む任意の `OntaryError` に加え、安定した文字列
`.code` を公開する構造的に互換な作者定義のコード付き例外にも一致します）。
`capture_effects()` は外部へ配信せず、呼び出された `(payload, meta)` を `.effects` リストへ
記録する callable dispatcher を返します。`FixedClock(start)` はタイムゾーン付きの同じ
日時を毎回返し、naive な start は拒否します。`SequentialIds(prefix)` は
`prefix-1`、`prefix-2`、…という決定的な ID を返します。

### `ontary.upcast`

`upcast_payload`。

---

## オントロジーを宣言する

### `Ontology(name, scope_levels, min_n=3)`

宣言のファサード。`OntologyRegistry`、`ScopePolicy`、宣言されたハンドラを蓄積し、
ランタイムを払い出します。

| 引数 | 型 | 備考 |
| --- | --- | --- |
| `name` | `str` | MCP サーバー名の既定値にもなります。 |
| `scope_levels` | `list[str]` | 自分で決める階層（粗い方を後ろに）。例: `["queue", "org"]`。組み込みのレベルは存在しません。 |
| `min_n` | `int` = `3` | 集計に必要な異なる寄与者数の下限。 |

宣言用メソッド（`link` 以外はデコレータ）:

| メソッド | 役割 |
| --- | --- |
| `@ontology.object(...)` | `OntologyObject` サブクラスをオブジェクト型として登録 |
| `ontology.link(api_name, from_cls, to_cls, cardinality, ...)` | リンク型を登録し `LinkHandle` を返す |
| `@ontology.action(params_cls, ...)` | 型付き Action ハンドラを登録 |
| `@ontology.function(...)` | 導出値 Function を登録 |
| `ontology.capability(proto, ...)` | Capability を宣言し `CapabilityHandle` を返す |
| `ontology.effect(payload_cls, ...)` | Effect を宣言し `EffectHandle` を返す |
| `ontology.validate()` | 検証して登録を**凍結** |
| `ontology.bind(store, ...)` | `OntologyRuntime` を構築 |

`validate()`（および `.definition` への接触）はオントロジーを凍結します。以降の
`object`/`link`/`action`/`function` 呼び出しは例外になります。すべての宣言を終えた
あとに 1 回だけ呼んでください。

#### `@ontology.object(*, layer, owned=False, api_name=None, description=None, display_name=None, scope=None, contributor=None, row_visibility=None)`

デコレート対象の `OntologyObject` サブクラスを登録します。

- **`layer`** — 自分で決めるグルーピング文字列（`"L0"`、`"core"`、何でも）。
  エンジンの挙動には一切影響せず、MCP のイントロスペクション
  （`list_object_types`）にそのまま載るので、エージェントやツールから「作者が
  どうグルーピングしたか」が見えます。
- **`owned`** — `True` で型全体をオントロジー所有に（どのソースも書き込めません）。
  `dict` を渡すと特定プロパティを既定値付きで所有扱いに（例 `owned={"escalated": False}`）。
- **`scope`** — `"unscoped"`、またはスコープルールのリスト（[スコープポリシー](#スコープポリシー)
  参照）。解決は**宣言されたレベルごと**に走ります。各レベルについて、ルールは宣言順に
  試され、そのレベルを対象としていて かつ 値が解決できた最初のルールが採用されます。
- **`contributor`** — 行の背後にいる*人物*を特定するスコープルール。min-N の計数に使われます。
- **`row_visibility`** — `(store, consumer, obj_id, payload) -> bool`。スコープの上に
  重ねる行単位の追加ゲート。

#### `ontology.link(api_name, from_cls, to_cls, cardinality, *, description=None, identity_revealing=False, owned=False) -> LinkHandle`

`cardinality` は `Cardinality` 列挙体のメンバー、またはその名前文字列:
`ONE_TO_ONE`、`ONE_TO_MANY`、`MANY_TO_ONE`、`MANY_TO_MANY`。

`identity_revealing=True` は、**人間**コンシューマーからの traverse を拒否することを
意味します（AI コンシューマーは辿れます）。返される `LinkHandle` を
`client.traverse(link_cls, from_obj_or_id)` の第 1 引数に渡すと型付きの結果が得られます。

#### `OntologyObject`

宣言するオブジェクト型の基底クラス。`pydantic.BaseModel` のサブクラスなので、
バリデータも computed field も通常の注釈付き属性もそのまま使えます。

読み取り時にエンジンが 2 つの属性を付与します。これらはモデルのフィールドでは
**ありません**（宣言したプロパティと衝突しません）。

| 属性 | 型 | 意味 |
| --- | --- | --- |
| `lineage` | `Lineage \| None` | 行の出自と有効期間 |
| `redacted_fields` | `frozenset[str]` | *このコンシューマー*に対して伏せられたプロパティ |

`redacted_fields` は「あなたには見えない」と「実際に `None` が入っている」を区別する
ためのものです。可視だが値が無い optional フィールドも `None` になりますが、
こちらには決して現れません。

#### `prop(*, primary_key=False, sensitivity=None, scope_level=None, required=None, property_type=None, **field_kwargs)`

`pydantic.Field(...)` にオントロジーのメタデータを足したもの。認識されない kwargs は
そのまま `Field` に渡るので、`prop(default=None, description="...")` は期待どおりに
動きます。`prop()` は並行するフィールド体系では決してありません。同じクラス上で素の
注釈付きフィールドや `Field(...)` もそのまま機能します。

> **`sensitivity` を制限したプロパティは `X | None` で宣言する必要があります。**
> そうでない場合、クラス登録時にコード付きのバリデーションエラーになります —
> リダクションされた読み取りが `None` を返せる必要があるためです。

#### フィールドマーカー: `ref` / `target` / `scope_ref`

いずれも `OntologyObject` サブクラスを取り、追加の kwargs は `Field` に渡します。

| マーカー | 宣言内容 |
| --- | --- |
| `ref(cls)` | このパラメータは `cls` のオブジェクトを指す |
| `target(cls)` | …かつ Action の**対象**である（スコープはこれに対して強制される） |
| `scope_ref(cls)` | …かつ Action が作成する先の**スコープ**を指す |

これらが生成される `ActionParameterDef` の `refers_to` / `scope_semantics` を決めます。
つまりスコープ強制は、エンジンのハードコードではなく**作者が宣言する**ものです。

#### `ActionParams`

型付き Action パラメータモデルの基底クラス。フィールドには上記マーカーを使います。

```python
class EscalateTicketParams(ActionParams):
    ticket_id: str = target(Ticket)
    reason: str | None = None
```

---

## スコープポリシー

スコープは**宣言する**ものであり、推論されません。4 種類のルールが `ScopePolicy` を
構成します。通常は手で組み立てる必要はなく、`@ontology.object(scope=[...])` が行います。

| ルール | フィールド | レベルの解決元 |
| --- | --- | --- |
| `SelfScope` | `level` | オブジェクト自身の id |
| `DirectProperty` | `level`, `property_name` | 行のプロパティ |
| `ViaLink` | `link_api_name`, `direction`（`"from"`/`"to"`）, `parent_type` | リンクを辿って親へ（再帰的に） |
| `CustomResolver` | `level`, `fn(store, obj_type, obj_id) -> str \| None` | 任意の呼び出し側ロジック |

### `ScopePolicy`

| フィールド | 型 | 備考 |
| --- | --- | --- |
| `levels` | `list[str]` | |
| `unscoped_types` | `set[str]` | ここに挙げた型を `rules` にも書くことはできません。重複は `validate()` と全ての読み取りが `SCOPE_POLICY_ERROR` で拒否します。 |
| `rules` | `dict[str, list[ScopeRule]]` | |
| `contributor_rules` | `dict[str, list[ScopeRule]]` | |
| `row_visibility` | `dict[str, RowVisibilityFn]` | |
| `min_n` | `int` | |

### 解決ヘルパー

```python
resolve_owning_scope(policy, store, obj_type, obj_id) -> dict[str, str | None]
resolve_contributor(policy, store, obj_type, obj_id) -> str | None
covers_scope(policy, consumer, resolved) -> bool
```

`resolve_owning_scope` は宣言されたレベルごとに 1 エントリを返します。`covers_scope` は
**コンシューマーのレベルが未解決なら拒否します** — スコープ強制はフェイルオープンしません。

ルールが未宣言のオブジェクト型・リンク型・レベルを参照している場合は
`ValidationFailed`（`SCOPE_POLICY_ERROR`）になります。

---

## ランタイムとクライアント

```python
runtime = ontology.bind(store, capabilities={...}, effects={...})   # 1 回だけ
client  = runtime.for_consumer(consumer)                            # リクエストごとに安価に
```

### `Ontology.bind(store, *, clock=None, id_factory=None, capabilities=None, effects=None)`

`clock` はタイムゾーン付き `datetime` を返す callable で、デフォルトは
`datetime.now(timezone.utc)` です。`id_factory` は `str` を返す callable で、デフォルトは
UUID 形式の ID です。どちらも共有ランタイムに保存され、すべての
`for_consumer()` ビューに引き継がれます。`drain_effects(now=...)` を明示した場合は、
ランタイムの clock よりそちらが優先されます。

### `OntologyRuntime(ontology, store, handlers=None, *, clock=None, id_factory=None, capabilities=None, effects=None)`

1 つの `(ontology, store)` ペアに対する、コンシューマー非依存の共有機構 — クエリ層、
Action 実行器、バインド済みハンドラ — をちょうど 1 回だけ配線します。

- **`.for_consumer(consumer, *, capabilities=None, effects=None) -> OntologyClient`** —
  安価なビュー。1 プロセスで多数のコンシューマーを捌いても、再配線は起きません。
### `OntologyClient(ontology, store, consumer, *, capabilities=None, effects=None)`

ちょうど 1 つの `(ontology, store, consumer)` に束縛されます。直接構築しても動作し、
その場合は内部で使い捨てのランタイムを構築します。

| メソッド | 戻り値 |
| --- | --- |
| `.get(obj_type, obj_id)` | `T \| StoredObject \| None` |
| `.list(obj_type, where=None, *, limit=DEFAULT_READ_LIMIT, after=None, order_by=None)` | `list[T] \| list[StoredObject] \| TypedPage[T] \| Page` |
| `.traverse(obj_type, link, from_id, *, reverse=False)` または `.traverse(link_cls, from_obj_or_id, *, reverse=False)` | `list[T] \| list[StoredObject]` |
| `.aggregate(obj_type, value_field, where=None, *, func="mean")` | `float \| int` |
| `.aggregate_by(obj_type, value_field, group_by, where=None, *, func="mean")` | `dict[str, float \| int]` |
| `.count(obj_type, where=None)` | `int` |
| `.exists(obj_type, where=None)` | `bool` |
| `.count_contributors(obj_type, where=None)` | `int` |
| `.execute(params)` または `.execute(action, params)` | `dict[str, Any]` |
| `.call_function(api_name, params)` | `Any` |
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
> ISO-8601 文字列を返します。ストアが永続化する内容は変わりません — 型付き
> クライアントの取り出し時のハイドレーションだけがパースします。

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
`contains` のいずれか 1 つを指定する mapping を受け付けます。比較演算子は宣言済みの
`int`、`float`、`date` プロパティで使えます。`in` は宣言済み型の値のリストを取り、
`ne` はすべての宣言済み型に使え、`contains` は `str` の部分文字列検査です。未知の
演算子は `UNKNOWN_OPERATOR`、プロパティ型に合わない演算子または operand は
`OPERATOR_TYPE_MISMATCH` になります。この operand の型検査は、等価比較の唯一の
書き方であるベアのスカラーにも適用されます。例外は `None` で、これは null 判定と
して扱われ、そのプロパティを持たない行を選びます。宣言型が `float` のプロパティに
`int` の operand を渡すのは型の拡大であり、mismatch ではありません。mapping 形式の
`where` は、型付き、文字列、aggregate、Function、MCP のすべての読み取り surface で
共通です。

`DirectProperty` として宣言されたスコープルーティングキーが隠しフィールドである
場合、スコープキーの例外は、素の `eq` 値による等価一致と、明示的なリストを
operand とする `in` に限られます。`gt`、`gte`、`lt`、`lte`、`ne`、`contains`、
リストでない `in`、および不正な shape は、呼び出し側に値を学習させるため
`VISIBILITY_DENIED` で拒否されます。operand は実際に値を供給する必要があります。
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

### 集計

`aggregate(...)` と `aggregate_by(..., group_by=...)` は
`func="mean"|"count"|"sum"|"min"|"max"` を受け付けます。デフォルトは従来どおり
`"mean"` です。グループなしでは `count` が `int`、その他の関数が `float` の値を返し、
グループ付きでは各グループに対応する値を dict で返します。

両者は `where`/`group_by` の隠しフィールド検査、**異なる寄与者**に対する min-N、
型が宣言していない `value_field` に対する `UNKNOWN_FIELD`、そして宣言済みだが
非数値の `value_field` に対する `NON_NUMERIC_AGGREGATE`（どちらも行を 1 つも読む前に
検査されます）を強制します。隠された `value_field` を集計できるのは、`contributor_rules` を宣言した型に
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
`@ontology.action(params_cls, target=..., roles=[...], capabilities=(), effects=())`
でデコレートしたハンドラの組です。

`execute` はすべて同じパイプラインを通ります。

**登録済みか** → **ロールは許可されているか** → **宣言された target/scope パラメータを
スコープがカバーするか** → **事前条件** → **トランザクション内の副作用** → **追記専用の監査**

すべての試行が監査されます — `ok`、`denied`、`error` のいずれも。

### `ActionContext`

ハンドラが生のストアハンドルの代わりに受け取るもの。書き込みには Action 自身の
`Source` が自動で刻印されます。

| メンバー | 用途 |
| --- | --- |
| `.insert(obj_type, payload) -> str` | 作成。primary key を省略した payload には、ランタイムの `id_factory` から採番される（0.10; [docs/compatibility.md](compatibility.md#auto-minted-ids-come-from-id_factory-09--010) 参照） |
| `.update(obj_type, obj_id, changes)` | 更新 |
| `.create_link(link_api_name, from_id, to_id)` | リンク作成 |
| `.retire(obj_type, obj_id)` | オブジェクトをリタイアし、そのオブジェクト型についてリンク型が宣言している側でそのオブジェクトを参照するすべての live link を cascade-close |
| `.unlink(link_api_name, from_id, to_id)` | 1 本の live link を閉じる |
| `.read_current(obj_type, obj_id) -> StoredObject \| None` | 読み取り |
| `.read_all(obj_type) -> list[StoredObject]` | 現在行の列挙 |
| `.links_from(link_api_name, from_id) -> list[str]` | リンク走査 |
| `.links_to(link_api_name, to_id) -> list[str]` | リンク走査 |
| `.capability(handle) -> P` | 宣言済み Capability の取得 |
| `.emit(payload)` | 宣言済み Effect の発行 |
| `.consumer` | 呼び出し元の `Consumer` |

`read_current` と `read_all` は、信頼されたハンドラ向けの生の読み取りであり、
非 redaction・非 scope 制限です。意図的に consumer 向けの guarded query にはしていません。
とくに列挙を scope や sensitivity で絞ると、id allocator から既存行が隠れ、id の再利用を
引き起こし得ます。

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
`invocation_id`、および完全性レコード: `writes: list[WriteRecord]`、
`effects: list[EffectRecord]`、`capability_accesses: list[CapabilityAccessRecord]`。

**`kind: Literal["action", "function"]`** — このエントリを生成したもの。Action と
Function は 1 つのログを共有するため、読み手が両者を区別する手段が `kind` です（同じ
`api_name` の Action と Function を宣言することを妨げるものは何もありません）。
`function` エントリでは `action` に Function の api_name が入り、`target_type` は `""`
（Function に対象オブジェクト型はありません）、`writes`/`effects` は常に空です。

**`invocation_id: str | None`** — `execute()`（および監査対象の `call_function()`）
呼び出しごとに 1 つの id で、その呼び出しが書き込む**すべて**のエントリ（`denied`/`error`/`ok` のエントリと、Effect を持つ Action
では後続の `effects_dispatched` エントリ）に刻印されます。`pending` の Effect とその結果を
対応づけるときは、フィールド一致と追記順に頼らずこの値を使ってください — 同じ Action を
同じパラメータで 2 回呼ぶと、それ以外では区別できません。`None` はこのフィールドが存在
しなかった頃のエントリ（古いエンジンが書いたストアファイル）を意味し、後から捏造される
ことはありません。

- `WriteRecord` — `op`（`create`/`update`/`link`）、`object_type`、`link_type`、
  `object_id`、`from_id`、`to_id`
- `EffectRecord` — `api_name`、`payload`、`outcome`（`pending`/`dispatched`/`failed`）、`error`
- `CapabilityAccessRecord` — `api_name`、`count`

---

## Function

Function は `(query: BoundQuery, params: dict) -> Any` を `@ontology.function(...)` で
デコレートしたものです。ストアハンドルはハンドラに一切届かず、すでにガードされた
読み取りだけが渡ります。Function は導出値を返し、書き込みは行いません。

### `BoundQuery`

コンシューマーを固定した `GuardedQuery`: `.get`、`.list`、`.count`、`.exists`、`.traverse`、
`.aggregate`、`.aggregate_by`、`.count_contributors`、`.capability(handle)`。型付きオーバーロードは
`OntologyClient` と同様に機能します（`query.get(Ticket, id) -> Ticket | None`）。

Function の*パラメータ自体*はどちらの surface でも `dict[str, Any]` のままです —
型付き Function パラメータはこの API には含まれません。

`PreconditionFailed`（`FUNCTION_ERROR`）は、未宣言の api_name、重複登録、ハンドラ未バインドを
カバーします。

### Function の監査境界

監査対象となる call では、`OntologyClient.call_function` が `kind="function"` の監査エントリを 1 件追加します —
invocation id、params、outcome、handler の `capability_accesses` を記録します。`writes` と
`effects` は構造上空です。

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
AC10 の contributor exemption が含まれるため、Capability のない Function でも、その
consumer が自分では読めない field に対する `mean` や `count` を返せます。実際にそれを
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

## 統制された副作用

ハンドラが外界に求めるものはすべて**宣言**し、バインド時に提供する必要があります。
未宣言の利用は拒否され、利用はすべて監査されます。

### Capability — ハンドラが読む／呼ぶもの

```python
Clock = ontology.capability(ClockProto, name="clock")

@ontology.action(P, target=T, roles=["Agent"], capabilities=[Clock])
def handler(ctx, params):
    now = ctx.capability(Clock).now()
```

未宣言の Capability を要求すると `UNDECLARED_CAPABILITY`、宣言済みでもプロバイダが
バインドされていなければ `CAPABILITY_NOT_PROVIDED` になります。

### Effect — ハンドラが「起きてほしい」こと

```python
Notify = ontology.effect(NotifyPayload, api_name="Notify")

@ontology.action(P, target=T, roles=["Agent"], effects=[Notify])
def handler(ctx, params):
    ctx.emit(NotifyPayload(...))
```

Effect は**呼び出しではなくデータ**です。ハンドラはペイロードを発行するだけで、
ディスパッチはトランザクションの外で起こります。各 Effect には `EffectMeta`
（`action`、`actor_id`、`role`、`ts`、`effect_id`、`attempt`）が伴います。未宣言の
Effect の発行は `UNDECLARED_EFFECT`、宣言済みでもディスパッチャが無ければ
`EFFECT_NOT_DISPATCHABLE` になります。JSON 化できないペイロードは、トランザクション
の**内側**で `EFFECT_NOT_SERIALIZABLE` を送出します。アクションはロールバックされ、
外部へは何も送られません。

プロバイダは `ontology.bind(store, capabilities={...}, effects={...})`、または
クライアント単位で `for_consumer(...)` にバインドします。

### 永続的な配信

発行された Effect は、アクションのトランザクションの内側で `effect_outbox` テーブル
に書き込まれます。つまり配信すべき仕事が、オントロジーへの書き込みと一緒にコミット
されます。配信保証は **at-least-once** です。

| API | シグネチャ | 補足 |
| --- | --- | --- |
| `RetryPolicy` | `RetryPolicy(max_attempts=3, initial_backoff=1s, multiplier=2.0, max_backoff=5m, lease=60s)` | ランタイム／クライアント単位に `effect_retry=` でバインド。`max_attempts=1` は従来の at-most-once と同じ挙動になる。バックオフは決定的（ジッタなし）。 |
| `OntologyClient.drain_effects` | `drain_effects(*, limit=100, now=None) -> DrainReport` | 実行時刻に達した行をリース付きで確保し（二重送信を防ぐ）、そのクライアントのディスパッチャで試行して `DrainReport(claimed, delivered, retrying, failed, skipped)` を返す。`OntologyRuntime` にも同じメソッドがある。 |
| `OntologyClient.outbox` | `outbox() -> list[OutboxRecord]` | 全行の管理用リード。`pending`・`delivered`・打ち切り済みの `failed` をすべて含む。 |

`execute()` がコミット後に行う同期ディスパッチが、ポリシー上の 1 回目の試行です。
以降の再試行が自動で走ることはありません。**この SDK はスレッドを一切起動しない**
ので、障害からの回復が必要なら、ワーカー・cron・リクエスト末尾のいずれかから
`drain_effects()` を呼ぶ必要があります。そのクライアントにディスパッチャが無い
Effect の行は、失敗とは数えずリースを解放し、`skipped` に計上します。

行が `delivered` になるのは外部呼び出しから戻った**後**なので、その間にプロセスが
死ねば再配信されます。**ディスパッチャは `EffectMeta.effect_id` に対して冪等で
なければなりません**。この id は 1 回の発行につき 1 つで、再試行をまたいでも変わり
ません。`max_attempts` を使い切った行は `failed` になります。これは終端状態であり、
読み出せるデッドレターとして残りますが、二度と再試行されません。

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

### `Store` プロトコル

`OntologyClient` / `GuardedQuery` / `ActionExecutor` は具体的なバックエンドではなく、
このプロトコルに対して型付けされています。

```python
insert(obj_type, payload, source) -> str
update(obj_type, obj_id, payload_changes, source) -> None
read_current(obj_type, obj_id) -> StoredObject | None
read_last(obj_type, obj_id) -> StoredObject | None
read_all(obj_type) -> list[StoredObject]
read_page(obj_type, after_key=None, batch=500) -> list[PagedRow]
retire_object(object_type, obj_id) -> StoredObject
create_link(link_type, from_id, to_id) -> None
close_link(link_type, from_id, to_id) -> bool
links_from(link_type, from_id) -> list[str]
links_to(link_type, to_id) -> list[str]
links_from_asof(link_type, from_id, asof) -> list[str]
links_to_asof(link_type, to_id, asof) -> list[str]
append_audit(entry) -> None
audit_entries() -> list[AuditEntry]
transaction() -> ContextManager
capture_action_writes() -> ContextManager[list[WriteRecord]]
```

`read_current` は有効な行だけを返します。`read_last` は、その行が有効かどうかに
関わらず最新の行を返します。`links_from`/`links_to` は有効なリンクだけを返します。
`_asof` の 2 つは、指定した時刻以降に閉じられたリンクも返します。

履歴を見るこの 3 つの read は、呼び出し元が 1 つだけです。action executor の
target gate です。gate は「スコープ外」と「retire 済み」を区別する必要があります。
retire されたオブジェクトも、属していたスコープを持ち続けます。`ActionContext.retire`
はリンクを閉じるため、gate はオブジェクトの最後の行と、その行が閉じた時点で
持っていたリンクからスコープを解決します。

consumer の read は、あえてこれを使いません。retire 済みの行のスコープを解決すると、
その子オブジェクトが再び可視集合に入ります。すると母集団が `min_n` を超え、
retire 対象自身の行を含む集計値が返ってしまいます。`read_last` から
retire 済みの行を除外したり、`_asof` から閉じたリンクを除外したりするバックエンドを
書くと、retire されたオブジェクトの正当な所有者が拒否されます。

<!-- scope-asof-invariant:start -->
gate はこの連鎖を、**target 自身の行が閉じた時刻の断面で**解決します。連鎖上のリンクは
上下どちらの境界もその時刻で読みます。そのため、target が既に離れていた辺が target の
ために答えることはありません。

その時刻に `ViaLink` の hop が複数の親を見つけた場合は、連鎖が解決できた最初の親が
勝ちます。その順序は各バックエンドの行順ではなく、store が宣言する順序です。すなわち、
リンクの `valid_from` が最も早いものから、次にバイト順で最も小さい親 id からです。
作成順ではありません。`links` は単調増加のキーを宣言していないため、同じ tick で作られた
2 本のリンクは、どちらを先に書いたかではなく id で並びます。この順序が、どのスコープが
そのオブジェクトを所有するか、したがってこの gate がどの操作者を通すかを決めます。その
ため、すべてのバックエンドがこの順序で答えます。

祖先のオブジェクトは、その時刻に retire 済みでなければ採用します。ただし読むのは最新行です。
したがって payload、および payload から読む `DirectProperty` のキーは、その祖先が「いま」
持っている値です。その時刻より後に更新された祖先は、当時のスコープではなく現在のスコープを
答えます。オブジェクト側も断面にするには履歴を時刻指定で読む必要がありますが、`Store` に
その read はありません。生きている target には閉じた時刻がないため、consumer の read と
まったく同じ現在時刻で解決します。まだ存在するオブジェクトについて、この gate が緩むことは
ありません。
<!-- scope-asof-invariant:end -->

「retire 済みの行も許す」ではなく断面で解決するのは、素直な 2 つの解釈がそれぞれ逆向きに
壊れるからです。祖先を常に最新行から解決すると、ずっと前に retire された祖先が生きている
祖先に競り勝ち、もう所有権を持たない操作者を通してしまいます。逆に祖先を現在時刻だけで
解決すると、target が閉じた時点では生きていて、いま生きているスコープへの唯一の経路である
祖先を拒否します。解散したチームは元に戻せないため、正当な所有者が恒久的に拒否されます。
target の `valid_to` の時点では、先に retire された祖先は既に閉じており、後まで残った祖先は
閉じていません。断面を 1 つ選べば、両方が同時に解決します。

<!-- scope-denied-consequences:start -->
連鎖が解決でき、consumer がそのスコープを覆う場合、gate は handler 自身の拒否
（`OBJECT_ALREADY_RETIRED`）まで到達します。`SCOPE_DENIED` の意味は 1 つではありません。
raise は 2 か所にあります。1 つは、スコープを担うパラメータが `str` でない場合の多層防御の
拒否です。型の不一致は先にパラメータ検証が弾くため、通常の経路ではなく型混同に対する床です。
もう 1 つは、覆えることを示せないときに必ず発火します。これは 1 つではなく 2 つの状況です。
連鎖は解決できたが、この consumer がその外にいる場合。これはこの gate が変えていない従来
どおりの拒否です。あるいは、その時刻に連鎖が解決できない場合で、既定の deny が働きます。
この断面が扱うのは後者だけです。宣言されているルールの種類は 4 つで、それぞれが自身の hop で
retire に出会います。

- `SelfScope` はオブジェクト自身の id を返します。retire はこの id を奪いません。
- `DirectProperty` はスコープキーを payload から読みます。この gate が読むのは有効な行では
  なく最新行のため、retire は payload を変えません。
- `ViaLink` は `links` テーブルをたどります。たどり着いた親のどれも順に解決できないとき、
  この hop は何も解決しません。
  target が閉じるより前に retire された親もその一つです。その時刻に辺そのものは読める
  ことがありますが、親の行が採用されないため、この gate が尋ねる 1 つの時刻にその親は
  答えられません。target より後に retire された
  親は、同じ cascade の tick で閉じたものも含めて、いまも target のために解決します。
- `CustomResolver` は生の `Store` を受け取る作者のコードで、engine はその中に立ち入り
  ません。したがって、retire 済みのオブジェクトが何に解決するかは、この断面
  ではなく resolver 自身の責任です。素直な実装は `read_current` を読みますが、これは
  retire 済みのオブジェクトには `None` を返すため、そう書かれた resolver は拒否します。
  retire 後も precondition の拒否が必要な型は、2 つ目のルールを宣言してください。スコープ
  キーの列に置いた `DirectProperty` は retire 後も残ります。

各項目は 1 つの hop についての説明であり、1 つの target についての保証ではありません。
また、この 4 つが連鎖のすべてでもありません。`ScopePolicy.rules` は型ごとに**順序付きの
リスト**を持つため、target は宣言した数だけ hop を持ち、最初に解決できた hop が答えます。
さらに、自身のルールでは答えられない level は、より狭い level の canonical な
インスタンスを宣言する型があれば、そこへ登ります。これも 4 つのどれでもありません。engine 自身の hop に共通する
のは断面です。engine が読まなければならないオブジェクトが、target が閉じるより前に retire
されていれば、そのどの hop からたどり着いたかによらず、そこで拒否されます。したがって、
target の level に答える hop は、その target の他の hop がまったく触れないオブジェクトで
失敗することがあります。`CustomResolver` がこの断面の外にあるのは、その callable 自身が行う read に
ついてだけです。engine は作者のコードにその時刻を渡さないため、callable が自分でたどる祖先
は、retire 済みかどうかに関わらず callable の読み方どおりに読まれます。その**答え**は engine
に戻ります。そこから engine が読むオブジェクトは、この断面の規則どおりに拒否されます。より
狭い level の答えが指す canonical なインスタンスも、engine が続けてルールを尋ねるオブジェクト
も同じで、後者の行はその resolver が動く前に検査されます。

この一覧の拒否はすべて fail closed です。そのオブジェクト自身の所有者が拒否されるだけで
情報は出ません。
<!-- scope-denied-consequences:end -->

3 つの実装が同梱され、すべて同じ 192 assertion の適合性テストスイートで検証されています。

- **`ObjectStore`** — SQLite。履歴（close-old / insert-new）、リンク、監査ログ。
- **`InMemoryStore`** — 純 Python。ファイルも SQL も無し。テストやドッグフーディング向け。
- **`PostgresStore`** — PostgreSQL ベース。`postgres` extra で利用可能。

最外層の `transaction()` は、同じストア上の並行 writer に対して、読み取りに続く
書き込みを直列化します。再入可能であり、ネストした呼び出しは最外層の
トランザクションを共有します。

`ObjectStore(registry, path, *, busy_timeout=5.0)` は、SQLite がデータベース
ロックを待つ秒数を設定します。`ActionExecutor` は、外部サービスを呼ぶ
`ctx.capability()` を含む handler 本体全体で最外層の transaction、したがって
SQLite の write lock を保持します。競合する writer がその handler を待てる時間は、
自身の store の busy timeout までです。期限を超えると code `STORE_BUSY`
（`kind="conflict"`）が送出されます。正当な handler が既定値より長く実行される
場合は `busy_timeout` を増やすか、capability 呼び出しを短く保ってください。

`Source` — `source_system`、`source_id`、`extracted_at`。

`retire_object` は置き換え行を挿入せず、オブジェクトの current row を閉じます。リンクの
cascade は行いません。`close_link` は 3 つの ID が一致する 1 本の live link を閉じます。
3 つのバックエンドがすべてこれらの verb を実装します。
`ActionContext.retire` の cascade は、ストアのオブジェクト retirement primitive
の上位にあります。

### スキーマバージョニング

すべての SQLite ファイルは、作成時にエンジンの `SCHEMA_VERSION` を `PRAGMA
user_version` へ刻印します。Postgres も同じ番号を `schema_meta` に記録します。
どちらのバックエンドも移行のはしご（migration ladder）を持ちません。そのため、刻印が
一致しないストアはすべて、構築時点で `STORE_VERSION_UNSUPPORTED` として両方のバージョンを
挙げて拒否します。刻印が高い場合も低い場合も、未刻印で `objects` テーブルをすでに
持つ場合も同じです。スキーマバージョンをまたぐ移行はオペレーターの明示的な手順です。
対応する ontary バージョンで開くか、新しいストアにデータを移してください。

---

## バルク取り込み

```python
bulk_upsert(store, registry, obj_type, records, source) -> IngestReport
bulk_link(store, registry, link_type, pairs, source) -> IngestReport

client.ingest(
    obj_type, records, source, *,
    on_error: Literal["raise", "report"] = "raise",
) -> IngestReport
client.ingest_links(
    link_api_name, pairs, source, *,
    on_error: Literal["raise", "report"] = "raise",
) -> IngestReport
```

`bulk_upsert` と `bulk_link` はエンジン層であり、常に `IngestReport` を返します。
クライアントのメソッドはバッチを最後まで処理するため、別のレコードが失敗しても
有効なレコードはコミットされたままです。クライアントはデフォルトで失敗時に
`IngestError` を送出します。`.report` に完全なレポートが入り、メッセージには
コミット済みと失敗したレコード数が含まれます。`on_error="report"` を渡すと、
送出せずにレポートを返す従来の動作になります。

`IngestReport` — `inserted_ids: list[str]`、`errors: list[IngestError]`。レコードは
宣言された形状に対して検証されます。主キーの欠落、必須プロパティの欠落、未知の
プロパティ、型の不一致は `INVALID_RECORD` になります。オントロジー所有の型への
書き込みは `OWNED_TYPE_REFUSED`、オントロジー所有プロパティの指定は
`OWNED_PROPERTY_REFUSED` になります。

---

## `ontary.connect`

任意のソースシステムとオントロジーの間に置く、ソース非依存のステージング層。ベンダーを
差し替えてもオントロジーには手を入れずに済みます。エンジン名は 20 個です。

### 正規モデル

- **`CanonicalRecord`** — 基底クラス。`lineage: Lineage` スタンプ
  （`source_system`、`source_id`、`extracted_at`）が構築時に強制されます。
- **`CanonicalBatch`** — `entities: dict[str, list[CanonicalRecord]]`。
- **`RawTables`** — テーブル名をキーにしたソース形状の行。

### コネクタ

- **`SourceConnector`** — プロトコル。`extract()`（副作用あり。`make verify` からは
  呼ばれません）と `transform(raw) -> CanonicalBatch`（純粋。ユニットテスト対象）。
- **`BaseConnector`** — 便利な基底クラス。
- **`run_dlt_extract(source, pipeline_name, staging_dir) -> RawTables`** — dlt による
  抽出（`dlt` エクストラが必要）。

### マッピング

- **`MappingSpec`** — `object_bindings`、`link_bindings`。
- **`ObjectBinding`** — `entity`、`object_type`、`key_field`、`property_map`、
  `record_model`、`transform`。
- **`LinkBinding`** — `link_type`、`from_entity`、`from_key_field`、`to_entity`、
  `to_key_field`。
- **`map_batch(batch, mapping, registry, store, *, source_system, run_at) -> RunReport`**
- **`run_pipeline(connector, mapping, ontology, store, *, raw=None, run_at=None) -> RunReport`**

`oid(source_system, object_type, key) -> str` が、マッピングを冪等にする決定的な
オントロジー id を導出します。同じバッチを再実行しても重複ではなく upsert になります。

`RunReport` — `source_system`、`run_at`、`written`、`errors`、
`entities_absent_from_batch`、`links_created`、`links_resolved_same_batch`、
`links_resolved_via_store`、`links_skipped`、`link_skip_details`、`link_errors`。
どの binding も参照していないバッチエンティティは `ENTITY_KEY_MISMATCH` になります。
逆（binding はあるがバッチが出さないエンティティ）はエラーではなくレポートに計上されます。

### 変換ヘルパー

`to_date`、`to_datetime`、`to_optional_date`、`to_optional_datetime`。

---

## MCP サーバー

```python
from ontary.mcp_server import build_mcp_server

server = build_mcp_server(ontology, store, consumer, *, name=None,
                          capabilities=None, effects=None)  # -> FastMCP
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

`query_objects(obj_type, where=None, order_by=None, limit=None, after=None)` は MCP surface では常に
上限付きです。`limit` を省略するとサーバーのデフォルト上限 100 行を使い、明示する
場合の最大値は 1000 です。内部のページ付き読み取りが不透明な `next_cursor` を返し、
成功時のレスポンスは従来どおり行を `result` に置いたまま、同じ階層に `next_cursor`
を追加します（消化済みなら `null`）。同じ明示的な `limit` とともにカーソルを返して
次ページを取得してください。明示的な `limit` なしの `after` は
`AFTER_WITHOUT_LIMIT`、1 未満または 1000 超の値は `INVALID_LIMIT` になります。

`where` の文法は、素の scalar を等価一致として使うか、`gt`、`gte`、`lt`、`lte`、
`in`、`ne`、`contains` のいずれか 1 つを指定する mapping 形式です。演算子は宣言済み
プロパティ型に対して検証され、未知の演算子は `UNKNOWN_OPERATOR`、型に合わない演算子または
operand は `OPERATOR_TYPE_MISMATCH` になります。date の比較は保存される ISO 日付の順序を使います。
lineage フィールドは対象外で、未知のキーは `UNKNOWN_FIELD` になります。
`order_by` は宣言済み payload フィールド（デフォルトは昇順）、または
`(field, "asc"|"desc")` の組を受け付け、ページカーソルと組み合わせられます。
`count_objects` はコンシューマーに可視な行の件数を返し、min-N の対象外です。
`aggregate_objects` は `func="mean"|"count"|"sum"|"min"|"max"` を受け付け、
デフォルトは `"mean"` です。すべての関数に同じ min-N の公開判定が適用され、
グループ付きの空集合 `{}` も拒否されます。
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
    capabilities=None, effects=None,
    token_verifier=None, auth=None,
)  # -> FastMCP
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
AuthSettings`）。デプロイヤーが設定し、内部の `FastMCP(...)` 呼び出しへそのまま
渡されます — `FastMCP` は構築後にどちらを設定する public なセッターも公開して
いないため、ここが唯一の配線ポイントです。どちらも渡さないのは stdio 専用、
または意図的に認証なしのデプロイとして正当ですが、どちらか片方だけを渡すのは
実行時の状態ですらありません — `FastMCP.__init__` がその場で `ValueError` を
送出する（構築時点での fail-fast）ため、サーバーは構築されず、呼び出しも一切
発生しません。stdio には認証コンテキストが全くないため、この 2 引数の値に
関わらず stdio 上のマルチコンシューマーサーバーは常にすべての呼び出しを
`UNAUTHENTICATED` で拒否します。1 コンシューマー・stdio プロセスには
`build_mcp_server(ontology, store, consumer)` を使ってください。

**`stateless_http=True` で構築されます（ハードコード） — これは実装の細部ではなく、
実在するトランスポート上のトレードオフです。** FastMCP の既定の stateful streamable
HTTP は `initialize` request で 1 つのセッションタスクを起動し、そのセッションの
`Mcp-Session-Id` を持つ以降のすべての request をこのタスクで再利用します。つまり
後続 request が呼び出すツール本体は実際には**`initialize` request 自身のタスクの中で**
実行され、その認証コンテキストはタスク開始時に一度だけコピーされたものです —
これは上記の「解決結果はキャッシュされない」という前提を静かに破ります。失効・
再スコープされたトークンは、セッションが終わるまでその凍結されたバインディングの
まま提供され続けてしまいます。`stateless_http=True` は、すべての request に
まっさらなトランスポートとセッションタスクを与えることでこれを防ぎます — トークンを
凍結する余地自体をなくすということです。代償: このサーバーではセッションの再開
（resumability）ができません（SSE ストリーミング自体は `stateless_http` ではなく
FastMCP 自身の `json_response` 設定で制御されており、いずれの値でも影響を受けま
せん）。`build_mcp_server` は影響を受けません — そもそも 1 プロセスの生涯にわたって
構築時に束縛された 1 つの `Consumer` しか持たず、トークンを凍結するセッションが
存在しないからです。

`mcp` エクストラが必要です。

---

## 記述子による宣言

クラス宣言が構築されている、より低レベルの surface です。生成された、あるいはデータ
駆動のオントロジーに有用ですが、たいていの作者は `Ontology` を使うべきです。

| 型 | 主なフィールド |
| --- | --- |
| `ObjectTypeDef` | `api_name`, `display_name`, `description`, `layer`, `properties`, `primary_key`, `owned` |
| `PropertyDef` | `name`, `type`, `required`, `sensitivity`, `scope_level` |
| `LinkTypeDef` | `api_name`, `from_type`, `to_type`, `cardinality`, `description`, `identity_revealing`, `owned` |
| `ActionTypeDef` | `api_name`, `display_name`, `target_type`, `executable_by_roles`, `description`, `parameters`, `capabilities`, `effects` |
| `ActionParameterDef` | `name`, `type`, `required`, `refers_to`, `scope_semantics` |
| `FunctionDef` | `api_name`, `description`, `input_description`, `output_description`, `capabilities` |
| `Sensitivity` | `ai_usable`, `human_visible` |

`PropertyType` は `Literal["str", "int", "float", "bool", "datetime", "json"]`。

**`OntologyRegistry`** が記述子を保持し、相互参照を検証します。`validate()` は、
リンク端点の参照切れ、Action 対象の参照切れ、api_name の重複、properties に無い主キーに
対して `ValidationFailed`（`ONTOLOGY_INVALID`）を送出します。

**`OntologyDef`** は registry + スコープポリシー + 設定をまとめたもので、クライアントや
MCP サーバーがバインドする単位です。この SDK には**モジュールグローバルなレジストリが
意図的に存在しません**。したがって 2 つのオントロジーが 1 プロセス内で干渉せず共存できます。

**`Declarations`** / `declarations(...)` は宣言された契約をデータとして公開します —
MCP の `get_declarations` が返すものです: `authority`（モデル宣言・実行時チェック）、
`capabilities`（action/function ごとに宣言され、未提供・未宣言なら fail-closed。
provider 自体はサンドボックス化されない作者コード）、`effects`（action ごとに宣言され、
永続 outbox 経由で at-least-once、コミット後にディスパッチ）、`writeback`
（オントロジーへの書き込みはすべてオントロジー所有。実行時自身の外部書き込み経路は
宣言された effects だが、capability provider はインラインで外部書き込みもできるため、
これは宣言された規約であって強制された境界ではない）、`reingest`
（upsert-merge。所有プロパティは残り、削除はない）、`visibility_default`
（deny-by-default — 未解決のスコープは隠れる）、`transaction_ownership`
（実行時所有 — 呼び出し元が開いたトランザクションを拒否）、`ontology_evolution`
（フィンガープリント方式。宣言された型バージョンアップの upcaster チェーンが保存済み
バージョンをカバーするか、drift が明示的に受け入れられない限り拒否）、
`idempotency`（なし — 再試行は別個の監査済み試行になる）、`audit_scope`
（テナントスコープの管理者向けビュー。action は常に監査され、function は capability を
宣言した場合に限り監査される（function 単位の上書きがない限り）。ただし contributor
exemption を通じて hidden field をリリースする call は常に監査される）、そして `tenancy`（構築時にバインドされた
ストアインスタンスごとに 1 テナント）。`identity` を含みます: マルチコンシューマー
MCP サーバーでは、この実行時ではなくトランスポートによって証明され — 検証器が
設定されていないデプロイはデフォルトのアイデンティティを仮定するのではなく、
すべての呼び出しを拒否します。1 コンシューマーサーバー、あるいは直接 Python から
使う場合は、`Consumer` は構築時にオペレーターが主張するものであり、何もそれを
証明しません。検証済みプリンシパル（マルチコンシューマーのみ）は、呼び出し元が
渡した resolver（サンドボックス化されない信頼された作者コード）によって `Consumer`
にマッピングされ、トランスポートが証明した `principal` と解決された `actor` の
どちらも監査されるため、resolver がすべてのプリンシパルを 1 つの特権的な actor に
マッピングした場合、それはログ上で可視化されます。再配信された Effect の監査行は
`actor` は書き戻しますが `principal` は書き戻さず、`invocation_id` で元の行に
紐付けられます — そのため、それらの行では `principal` が `None` になります。
監査された行のすべてが `principal` を持つわけではありません。`min_n`
はオントロジー固有の唯一の答えで、オントロジー自身の `ScopePolicy.min_n`
から読み取られます。

---

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
| `CALLER_TRANSACTION_REFUSED` | Raised when `ActionExecutor.execute()` (or an ingest entry point, a later task) is called while the caller has already opened a `store.transaction()` block (declared-contracts §3 AC9). `transaction()` is reentrant, so a caller-owned outer transaction could roll back an action after the executor reported success and audited `ok`. The engine must own the transaction/audit boundary and refuses to nest inside the caller's. Deliberately NOT audited (spec §5): an audit row inside the caller's transaction could itself be rolled back, so the refusal is raised before any audit write. |
| `UPCAST_FAILED` | Raised when a stored row cannot be read as the current declared version. The message distinguishes two causes because their fixes differ: the chain has no step for the carried version (normally a row written by a NEWER ontology than this declaration, i.e. a downgrade, since `ontology.validate()` rejects an incomplete chain), or an author's upcaster raised on this payload. A read failure is deliberately not a silent fallback to the raw payload, which would hand a consumer data in a shape the declaration says does not exist. |
| `ONTOLOGY_DRIFT` | Raised at construction when the declared ontology is not the one this store's rows were written under (ontology-evolution AC3/AC4). It is refused BEFORE any query, for the same reason as the `STORE_VERSION_UNSUPPORTED` conflict: a store the engine cannot honestly serve must not answer a read half-correctly first. Drift is not hypothetical; the measured mild case is a row the typed reader refuses while the string reader returns it. The message names every changed type. The two explicit ways forward are to migrate rows (`ontary.migrate.migrate_object_type`, then `accept_ontology_fingerprint`) or accept drift at the call site with `ObjectStore(..., accept_ontology_drift=True)`, which proceeds and writes an audit entry. |
| `CARDINALITY_VIOLATION` | A link creation would violate its LinkTypeDef cardinality. |
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
| `SCOPE_DENIED` | The consumer's scope does not cover the action's declared target/scope parameter (code `SCOPE_DENIED`). Role refusals use `PERMISSION_DENIED`; both are kind permission and each raise site supplies the specific code. |
| `UNAUTHENTICATED` | A request carried no verified identity at all -- kind permission. `build_multi_consumer_mcp_server` raises this for every tool call, including introspection, that reaches it with no authenticated `AccessToken`: stdio (which has no auth context) or HTTP with no `token_verifier` configured. The fix is: configure authentication. It is deliberately separate from `CONSUMER_UNRESOLVED`: a missing credential; mapping the principal fixes a missing consumer, so callers can distinguish the two from `.code` alone. |
| `CONSUMER_UNRESOLVED` | A verified principal existed, but the author's `resolve_consumer` callback returned no `Consumer` for it -- kind permission. `build_multi_consumer_mcp_server` raises this when the callback returns `None` for an otherwise verified `AccessToken`; the fix is: map this principal. It remains distinct from `UNAUTHENTICATED`, where no credential was presented at all, so the two failures are distinguishable from `.code` alone. |

### `precondition`

| Code | Meaning |
| --- | --- |
| `CAPABILITY_NOT_PROVIDED` | A declared capability had no provider bound for this call. |
| `EFFECT_NOT_DISPATCHABLE` | A declared effect had no dispatcher bound for this call. |
| `FUNCTION_ERROR` | Registering/calling a Function failed: undeclared api_name, duplicate registration, or no handler bound. |
| `PRECONDITION_FAILED` | An action's precondition failed; the message names it. The conventional code for `ActionError` (kind precondition); an author may attach their own stable code instead (AC7), e.g. `raise ActionError("...", code="GAP_NOT_ACKNOWLEDGED")`. It is also used with overridden codes for unregistered/unhandled actions (`UNKNOWN_ACTION`) and parameter-validation failures (`INVALID_PARAMS`) -- see the `code=` overrides at those raise sites. |

### `validation`

| Code | Meaning |
| --- | --- |
| `AFTER_WITHOUT_LIMIT` | `GuardedQuery.get_objects`'s (or `OntologyClient.list`'s) `after` was given without `limit` (pagination-hardening T2 review P1) -- the unpaginated `Store.read_all` path has no page to resume, so ignoring `after` would let a caller that lost track of its limit silently re-read every visible row and duplicate work; a caller that genuinely wants everything passes no `after` at all. |
| `ENTITY_KEY_MISMATCH` | Raised by `map_batch` when a `CanonicalBatch`'s entity keys and the `MappingSpec`'s `ObjectBinding.entity` names disagree in the authoring-bug direction (spec m35-sdk-refactor AC10). The check is ONE-DIRECTIONAL: batch keys MUST be a subset of binding keys, so a typo such as `widgits` cannot be swallowed by `CanonicalBatch.get()` as zero objects. Binding keys are NOT required to be a subset of batch keys; a partial or incremental connector run may omit normal entities, which is counted in `RunReport.entities_absent_from_batch` instead of failing. |
| `INVALID_BATCH` | `Store.read_page`'s `batch` was < 1 (SQLite's LIMIT -1 means unlimited and InMemoryStore's negative slice drops rows -- both the opposite of a bounded read). |
| `INVALID_CURSOR` | Raised when `Store.read_page`'s `after_key` is malformed OR simply unknown. `after_key` is UNTRUSTED input: it reaches the store from an MCP client via a later page-filling loop, round-tripped from a previous page's cursor without any guarantee the caller did not tamper with it. As amended 2026-07-25 (T2 review, spec §5), it is a random per-row PAGE TOKEN (`objects.page_token`, uuid4 hex), not a decimal row id. Resolving token to row id through the unique index is the ONLY way to turn a cursor into row identity, so every string never issued for a real row (malformed, tampered, or made up) raises this same error on both backends. There is no distinct well-formed but out-of-range case from the old integer design's `OverflowError`/silent-empty-page divergence. A token issued for a row since superseded by `update` still resolves because lookup uses `row_id` independently of `valid_to`, so an in-flight cursor remains a valid resume point (spec §8). |
| `GROUP_KEY_COLLISION` | Two distinct `group_by` values in one selection release as the same dictionary key, so one cell would have to describe two populations. The released shape is `dict[str, ...]` -- a public return type and MCP's wire shape -- and `str()` is not injective over the values a group key can take: an optional property keys `None` on the rows that lack it, which collides with a row carrying the literal string `"None"`. The populations did not merge; the later one overwrote the earlier, so the released value (and, under `func="count"`, the released size) described whichever rows were inserted last, decided by nothing the caller supplied or could observe. Raised per group as each is released, AFTER that group's min-N check, so the release floor keeps precedence over a shape refusal. |
| `INVALID_GROUP_BY` | `GuardedQuery.aggregate_by`'s (or `BoundQuery`'s/`OntologyClient`'s) `group_by` cannot be a group key. Either it was falsy (e.g. "") -- the shared aggregation body branches on `group_by`'s truthiness, so a falsy-but-non-None value would otherwise silently collapse to the ungrouped path and return a float instead of a `dict[str, float]` -- or it names a property whose declared `PropertyType` is not groupable (`json`, whose values may be a `dict` or `list` and so need not be hashable; grouping by one used to raise a bare `TypeError` from inside the grouping loop, and `INTERNAL_ERROR` once it crossed the MCP boundary). The declared type is checked, not the stored values, so a `json` column that happens to hold only scalars refuses too rather than working until the first `dict` arrives. Both are checked in `aggregate_by`, where the `GuardedQuery`, `BoundQuery`, and client surfaces converge, before `_aggregate` runs, rather than relying on an assert removed by `python -O`. |
| `PAGE_NOT_ITERABLE` | A `Page`/`TypedPage` was iterated, indexed or measured directly instead of through `.items`. Both are pydantic models, so the inherited `BaseModel.__iter__` would otherwise yield `(field_name, value)` pairs -- `for row in page` hands back `('items', [...])` and `('next_cursor', ...)`, and the failure surfaces later as `AttributeError: 'tuple' object has no attribute 'payload'` at whatever touched the row. This refuses at the iteration itself and names `.items` and `limit=None`. |
| `INVALID_LIMIT` | `GuardedQuery.get_objects`'s (or `OntologyClient.list`'s) `limit` was < 1 -- a silently empty page would hide that the call was malformed rather than legitimately paginated. |
| `STALE_CURSOR` | An ordered walk's cursor resolved to a row that is no longer current; restart the ordered walk from the first page. |
| `EFFECT_NOT_SERIALIZABLE` | Raised inside the action transaction when an emitted payload cannot be JSON-encoded for the durable outbox (spec `durable-effect-outbox`). This deliberate M5 behavior change means the action rolls back and nothing is sent: before the outbox, an unencodable payload still dispatched while only the audit record degraded to `_safe_json_dumps`'s placeholder, but a durable work item is what a later attempt sends and must not deliver a placeholder as the author's data. Normal payloads use `model_dump(mode="json")` for datetime, UUID, Decimal, enums, and nested models; this takes an arbitrary Python object on a sufficiently loose field. |
| `INVALID_PARAMS` | A call's parameters failed declared-shape validation: an action's params, or a read parameter whose SHAPE is wrong -- an `order_by` that is neither a field name nor a (field, direction) pair, or a `where=` that is not a mapping of field name to condition. A parameter naming something that does not exist is `UNKNOWN_FIELD` instead; this code is about the shape, not the name. |
| `INVALID_RECORD` | A bulk_upsert record failed declared-shape validation (missing primary key, missing required property, unknown property, or a value that does not match its declared type). The validation kind carries the SAME `INVALID_RECORD` code that `bulk_upsert` already reports: from a caller's point of view, a record not matching the declaration is one failure regardless of which write path noticed. This closes the M9 hole where only ingest checked: `Store.insert`/`update` and therefore `ActionContext.insert`/`update` could commit a row missing a required property or carrying a wrong-typed value, report success, and leave the typed reader unable to hydrate it. The same code wraps a Pydantic `ValidationError` while hydrating a stored `OntologyObject` payload (for example, a non-ISO datetime string), never surfacing a bare traceback; a stored row failing declared-shape validation on read-back is the same failure class ingest carries on write. |
| `LINK_NOT_FOUND` | A link closure found no matching live link. |
| `MISSING_MAPPED_FIELD` | A map_batch record's property_map referenced a canonical field entirely absent from that record (not merely None). |
| `NON_NUMERIC_AGGREGATE` | `GuardedQuery.aggregate`'s `value_field` is declared a non-numeric `PropertyType` (anything other than `int`/`float`, such as str/json/datetime/bool). It is checked against the declared type before rows are iterated or coerced, so values that merely look numeric cannot bypass the type contract (spec `m35-sdk-refactor` §6 AC7). |
| `OBJECT_NOT_FOUND` | An update targeted a non-existent object. |
| `OBJECT_RETIRE_NOT_FOUND` | A retirement targeted an object with no stored row. |
| `ONTOLOGY_INVALID` | `OntologyRegistry.validate()` rejected a declaration because its cross-references were invalid. |
| `SCOPE_POLICY_ERROR` | A ScopePolicy declaration is unusable: a rule references an undeclared object type, link type, or scope level; a type declares an empty contributor rule list; or a type is listed in unscoped_types while also declaring scope rules. |
| `UNDECLARED_CAPABILITY` | A handler requested a capability its action or function did not declare. |
| `UNDECLARED_EFFECT` | An action emitted an effect it did not declare. |
| `UNKNOWN_ACTION` | An action name is unregistered on the OntologyRegistry, or has no handler bound to it. |
| `UNKNOWN_FIELD` | A typed `get`/`list` call named a key that is not one of the target class's declared properties (spec typed-authoring AC7). The existence-only check runs client-side before the guarded read layer; a hidden-but-declared key still reaches the visibility kind unchanged, and the string-form surface keeps its silent-non-match behavior (AC8). The error lives here since C3 of the staged refactor (previously `ontary.functions`, which re-exports it). |
| `UNKNOWN_LINK_TYPE` | An operation referenced an unregistered link type. |
| `UNKNOWN_NAME` | A typed `BoundQuery`/`OntologyClient` call named an unregistered object, link, action, or function -- e.g. an undecorated class, a class/`LinkHandle` registered on a different `Ontology`, or a link api_name absent from this registry (typed-authoring AC7 / typed-actions AC8). Typed lookup failures use the validation kind and live here since C3 of the staged refactor so `ontary._typed_api` can raise them below the runtime modules. |
| `UNKNOWN_OBJECT_TYPE` | An operation referenced an unregistered object type. |
| `UNKNOWN_OPERATOR` | A mapping-form `where` clause named an operator outside the declared set: `gt`, `gte`, `lt`, `lte`, `in`, `ne`, or `contains`. |
| `OPERATOR_TYPE_MISMATCH` | A mapping-form `where` operator is not valid for the property's declared type, or its operand is not a declared-type scalar. |

### `visibility`

| Code | Meaning |
| --- | --- |
| `MIN_N_VIOLATION` | An aggregate would be computed over fewer than min_n distinct contributors. |
| `VISIBILITY_DENIED` | A single-object read/write targeted an object outside the consumer's scope. |

*全 53 コード / 7 種別。*

---

## 例外階層

公開されている kind class は次の 9 型の階層です。`IngestError` は、レコード単位または
クライアント単位の取り込み失敗に使う、追加のコード付き `OntaryError` サブクラスです。
その code はレポート内の失敗に対応します。すべてのエラーは `ERROR_CODES` の安定した
`code` を持ちます。特定の種別を捕捉するには `except <KindClass> as e: e.code` を使い、
`IngestError` を含むすべてのコード付きエラーを捕捉するには `except OntaryError as e: e.code`
を使います。`MappingValidationError` は binding の事前検証に使う通常の `Exception` で、
この階層には含まれません。

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
