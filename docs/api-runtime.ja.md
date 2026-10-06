# `ontary` — API リファレンス: ランタイムとクライアント

[English](api-runtime.md) · **日本語** · [← API リファレンス](api-reference.ja.md)

*リファレンス* — ランタイムとクライアントの契約を掲載します。[API リファレンス](api-reference.ja.md) と [はじめに](getting-started.ja.md) も参照してください。

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

