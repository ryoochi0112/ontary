# `ontary` — API リファレンス: Action と Function

[English](api-actions-functions.md) · **日本語** · [← API リファレンス](api-reference.ja.md)

*リファレンス* — Action と Function の契約を掲載します。[API リファレンス](api-reference.ja.md) と [はじめに](getting-started.ja.md) も参照してください。

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

`FunctionDef.parameters` と MCP の `list_functions` は、型付き入力を `name`、`type`、`description`、
`choices`、`fields`、`required`、`refers_to` を持つパラメータとして公開します。これは `scope_semantics` を
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

