# `ontary` — API リファレンス: オントロジーの宣言

[English](api-authoring.md) · **日本語** · [← API リファレンス](api-reference.ja.md)

*リファレンス* — オントロジーの宣言と記述子の契約を掲載します。[API リファレンス](api-reference.ja.md) と [はじめに](getting-started.ja.md) も参照してください。

## オントロジーを宣言する

### `Ontology(name, scope_levels, min_n=3)`

宣言のファサード。`OntologyRegistry`、`ScopePolicy`、宣言されたハンドラを蓄積し、
ランタイムを払い出します。

| 引数 | 型 | 備考 |
| --- | --- | --- |
| `name` | `str` | MCP サーバー名の既定値にもなります。 |
| `scope_levels` | `list[str]` | 自分で決める階層（粗い方を後ろに）。例: `["queue", "org"]`。組み込みのレベルは存在しません。 |
| `min_n` | `int` = `3` | 集計に必要な異なる寄与者数の下限。 |

空または重複した `scope_levels`、1 未満の `min_n` は、`.definition` を最初に
構築した時点で `ValidationFailed`（`ONTOLOGY_INVALID`）として拒否されます。

宣言用メソッド（`link` 以外はデコレータ）:

| メソッド | 役割 |
| --- | --- |
| `@ontology.object(...)` | `OntologyObject` サブクラスをオブジェクト型として登録 |
| `ontology.link(api_name, from_cls, to_cls, cardinality, ...)` | リンク型を登録し `LinkHandle` を返す |
| `@ontology.action(params_cls, ...)` | 型付き Action ハンドラを登録 |
| `@ontology.function(...)` | 導出値 Function を登録 |
| `ontology.capability(proto, ...)` | Capability を宣言し `CapabilityHandle` を返す |
| `ontology.validate(store=None)` | 検証して登録を**凍結**。ストアを渡すと、もう復元できない保存済み行も拒否 |
| `ontology.diagnose(store=None)` | 例外も凍結もせず全 `Finding` を返す。ストアを渡すと行もスイープ |
| `ontology.bind(store, ...)` | `OntologyRuntime` を構築 |

Action の `api_name` の既定値は、ハンドラ名ではなく params クラス名です。
`@ontology.action(ShipOrder, ...)` は `ShipOrder` として登録されます。`execute(name, dict)`、
監査エントリ、MCP の `execute_action` はこの名前を使います。別の名前にするには
`api_name="ship_order"` を渡してください。一方、Function の `api_name` の既定値はハンドラ名です。

`validate()`（および `.definition` への接触）はオントロジーを凍結します。以降の
`object`/`link`/`action`/`function` 呼び出しは例外になります。すべての宣言を終えた
あとに 1 回だけ呼んでください。

**教えてくれるチェック。** 以前は `validate()` を通って後で失敗していた 2 つの
モデリングミスを、書いた場所で検出します。

- `target(...)` パラメータがすべて `target=` と別の型を指す Action は、
  `@ontology.action(...)` の時点で `ONTOLOGY_INVALID` として拒否されます。
  メッセージには Action、パラメータ、両方の型が入ります。以前はスコープゲートが
  パラメータ側のオブジェクトを検査し、監査エントリと MCP は `target=` を示していました。
  `target()` パラメータのうち 1 つは `target=` の型を指す必要があります。
  別の型を指す追加の `target()` パラメータは許されます。手組みの
  `OntologyRegistry` にも `validate()` と `diagnose()` が同じ規則を適用します。
- オントロジー編集（プロパティの必須化、型の変更、`choices` の絞り込み）より前に
  書かれた行は、最初に読んだときにだけ失敗します。ストアはオントロジーの
  指紋を記録しないためです。`diagnose(store=store)` は `Store.read_all` で現在の
  行をすべて読み、型とプロパティごとに `INVALID_RECORD` の Finding を 1 件返します。
  Finding には復元に失敗する行数が入ります。`validate(store=store)` はそれらを
  `INVALID_RECORD` として送出します。全行を読むため、スイープはストアを渡したとき
  だけ動き、`bind()` では動きません。

```python
ontology.validate(store=store)  # 編集したオントロジーを提供する前に
```

#### `@ontology.object(*, layer, owned=False, api_name=None, description=None, display_name=None, scope: Literal["unscoped"] | Sequence[ScopeRule] | None = None, contributor: Sequence[ScopeRule] | None = None, row_visibility: RowVisibilityFn | None = None)`

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
- **`accept`** — この型が受け入れる lint コード（`FORBIDDEN_TYPE_NAME`、`AUDIT_TYPE`）
  1 つ、またはそのシーケンス。**`snapshot`** — `True` でスナップショット型として宣言。
  どちらも[アドバイザリーな finding](#advisory-findings)で説明します。

`scope`・`contributor`・`row_visibility` はデコレート時に形を検査します。
`"unscoped"` の綴り間違い、リストで包んでいない単一ルール、callable でない
`row_visibility` は、その場で `ValidationFailed`（`ONTOLOGY_INVALID`）になります。
メッセージにはクラス名・引数名・渡された値・受け付ける形が入ります。`Literal`
注釈により、綴り間違いの文字列は mypy でもエラーになります。

#### `ontology.link(api_name, from_cls, to_cls, cardinality: Cardinality | Literal["ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_ONE", "MANY_TO_MANY"], *, description=None, identity_revealing=False, owned=False) -> LinkHandle`

`cardinality` は `Cardinality` 列挙体のメンバー、またはその名前文字列:
`ONE_TO_ONE`、`ONE_TO_MANY`、`MANY_TO_ONE`、`MANY_TO_MANY`。それ以外の文字列は
`ValidationFailed`（`ONTOLOGY_INVALID`）で拒否され、メッセージにこの 4 つの名前が
列挙されます。`Literal` 注釈により、綴り間違いは mypy でもエラーになります。

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

#### `prop(*, primary_key=False, sensitivity=None, scope_level=None, required=None, property_type=None, choices=None, transitions=None, **field_kwargs)`

`pydantic.Field(...)` にオントロジーのメタデータを足したもの。認識されない kwargs は
そのまま `Field` に渡るので、`prop(default=None, description="...")` は期待どおりに
動きます。`prop()` は並行するフィールド体系では決してありません。同じクラス上で素の
注釈付きフィールドや `Field(...)` もそのまま機能します。

> **`sensitivity` を制限したプロパティは `X | None` で宣言する必要があります。**
> そうでない場合、クラス登録時にコード付きのバリデーションエラーになります —
> リダクションされた読み取りが `None` を返せる必要があるためです。

`choices=["open", "closed"]` は `str` プロパティの値をその集合に限定します。
それ以外の値は、どの書き込み経路でも拒否されます。

`accept="STORED_DERIVABLE"`（または `"FREE_TEXT_STATUS"`、あるいはそれらのシーケンス）は、
そのプロパティに対するアドバイザリーな finding を accept します。
[アドバイザリーな finding](#advisory-findings)を参照してください。

`transitions=TransitionDef(initial=(...), moves={...})` は choice プロパティで
許可する状態遷移を宣言します。文字列、または値が文字列の `Enum` メンバー
（通常の `Enum` または `StrEnum`）を状態として指定できます。
主キーには transitions を宣言できません。

グラフは `prop(transitions=...)` で指定し、`TransitionDef` は
`ontary.meta` から import します。`ontary.__all__` からは公開されません。

グラフに反する書き込みは `TRANSITION_NOT_ALLOWED` で拒否されます。メッセージには、
現在の状態、要求された状態、許可された遷移先が含まれます。

```text
TRANSITION_NOT_ALLOWED: Order 'o-1': status cannot move from 'pending' to 'shipped'; allowed from 'pending': ['paid']
```

この行はエラーコードとメッセージを並べたものです。メッセージ（`str(exc)`）にはコードが
含まれないため、コードは `exc.code` から読み取ってください。

#### `Ontology.rule(cls, name, *, message)`

`@ontology.object` で `cls` を登録した後、型付きの述語をデコレートします。

```python
@ontology.rule(Order, "shipped_needs_payment", message="payment required")
def shipped_needs_payment(order: Order) -> bool:
    return order.status != OrderStatus.SHIPPED or order.paid_at is not None
```

述語は保存済みの行全体から復元されたオブジェクトを受け取り、そのオブジェクトだけを
読み取る必要があります。名前とメッセージは型宣言に表示され、関数本体はエクスポート
されるスキーマデータには含まれません。`ontology.definition` でオントロジーが固定
されるまでルールを登録できます。

新しい行がルールを満たさない書き込みは `RULE_VIOLATED` で拒否されます。メッセージには、
ルール名とそのメッセージが含まれます。

```text
RULE_VIOLATED: Order 'o-1': rule 'shipped_needs_payment': payment required
```

アクションは新しい状態を代入して保存します。宣言済みの遷移やルールをアクション内で
事前チェックしません。書き込みはエンジンが拒否するため、テストではエンジンのコードを
期待してください。

#### 選択肢プロパティ: `Enum` と `Literal`

プロパティに、値が文字列の `Enum`（`StrEnum` など）か、文字列の `Literal[...]` を
注釈します。これは `str` と `choices` の組と同じ形を宣言します。`PropertyDef` は
`type="str"` になり、`choices` は宣言順のメンバー値になります。

```python
class Status(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


@ontology.object(layer="L0", scope="unscoped")
class Ticket(OntologyObject):
    id: str = prop(primary_key=True)
    status: Status
    kind: Literal["bug", "task"]
```

- **保存。** ストアはメンバーの文字列値を保持します。すべての書き込み経路が
  `Enum` メンバーとその値の両方を受け付けます。対象は `ingest`、`ctx.create`、
  `ctx.save`、Action パラメータ、`where` フィルタです。それ以外の値は拒否されます
  （書き込みでは `INVALID_RECORD`、Action では `INVALID_PARAMS`）。`where` フィルタは
  選択肢外の値を拒否しません。どの行にも一致しないだけです。
- **`choices` を宣言した場所だけ。** `Enum` メンバーを値に展開するのは宣言であり、
  Python の型ではありません。`prop(choices=...)` のプロパティは、値が選択肢に含まれる
  メンバーを受け付けます。同じ宣言だからです。`choices` の無い素の `str` プロパティは、
  従来どおり `Enum` メンバーを拒否します（書き込みでは `INVALID_RECORD`、`where`
  フィルタでは `OPERATOR_TYPE_MISMATCH`）。
- **読み取り。** 型付きの読み取りは `Enum` メンバー（`Literal` なら文字列）を返すので、
  `mypy` がフィールドの型を絞り込みます。dict の読み取り、MCP の結果、
  `aggregate_by` のキーは素の文字列を返します。
- **Action パラメータ。** `ActionParams` のフィールドに同じ注釈を付けると、その
  `ActionParameterDef` に `choices` が付きます。型付きハンドラはメンバーを受け取ります。
  `str` のパラメータに `prop(choices=[...])` を付けても、プロパティと同じく
  `choices` が付きます。ハンドラは文字列を受け取ります。
- **MCP。** `list_object_types` と `list_action_types` は、許される値を `choices` に
  列挙します（無い場合は `null`）。
- **宣言時に拒否されるもの**（`ONTOLOGY_INVALID`）: `IntEnum` や `Literal[1, 2]` の
  ような文字列でないメンバー、選択肢の注釈と `prop(choices=...)` の併用、主キーへの
  選択肢の注釈です。

#### Struct プロパティとパラメーター

プロパティまたは `ActionParams` のフィールドに、フラットな Pydantic `BaseModel` を
注釈します。たとえば `amount: Money` は struct プロパティを宣言します。Action パラメーター
にも同じ注釈を付けられます。

```python
class Money(BaseModel):
    value: float
    currency: str


class Order(OntologyObject):
    amount: Money
```

生成される `PropertyDef` または `ActionParameterDef` は `type="struct"` と、
`StructFieldDef` の空でない `fields` tuple を持ちます。各内側の宣言には `name`、
scalar の `type`（`str`、`int`、`float`、`bool`、`date`、`datetime`）、任意の文字列
`choices`、および `required` が含まれます。struct 以外の宣言では `fields=None` です。
外側を `Money | None` と注釈するとプロパティまたはパラメーターが optional になり、
内側の optional な注釈はそのフィールドを optional にします。dict で省略した optional な
内側のフィールドは明示的な `null` として保存され、モデルにデフォルトがなくても型付き読み取りでは
`None` になります。

書き込みにはモデルのインスタンスか、それと同等の dict を渡せます。内側の宣言に対して
検証されます。不正なオブジェクトの書き込みは `INVALID_RECORD`、不正な Action
パラメーターは `INVALID_PARAMS` になり、メッセージは `amount.currency` のような内側の
パスを示します。型付き読み取りはモデルを返し、文字列および MCP の読み取りは通常の
オブジェクトを返します。struct の更新は値全体を置き換えます。sensitivity は
プロパティ全体に適用されます。

struct はフラットです。入れ子のモデル、モデルの list や map、内側の alias、内側の
プロパティメタデータ、`RootModel`、struct の主キーは宣言時に拒否されます。フラットな `BaseModel` 注釈に
`prop(property_type="json")` を指定すると不透明な JSON として保存されますが、その他の
明示的な property type は拒否されます。内側のフィールドによる
検索はサポートされません。struct プロパティに対する `where` 条件は
`OPERATOR_TYPE_MISMATCH`、`order_by` は `INVALID_PARAMS`、`group_by` は
`INVALID_GROUP_BY` になります。数値集計関数は `NON_NUMERIC_AGGREGATE` になります
（`count` は対象外です）。MCP の `list_object_types` と `list_action_types` は、すべての
プロパティとパラメーターに `fields` を含めます。struct 以外は `null`、struct は
`name`、`type`、`required`、`choices` を持つオブジェクトの list です。

#### フィールドマーカー: `ref` / `target` / `scope_ref`

いずれも `OntologyObject` サブクラスを取り、追加の kwargs は `Field` に渡します。

| マーカー | 宣言内容 |
| --- | --- |
| `ref(cls)` | このパラメータは `cls` のオブジェクトを指す |
| `target(cls)` | …かつ Action の**対象**である（スコープはこれに対して強制される）。unscoped な型には強制するスコープがないため、`roles=` だけがゲートになる。`target()` パラメータのうち 1 つは Action の `target=` の型を指す |
| `scope_ref(cls)` | …かつ Action が作成する先の**スコープ**を指す。unscoped な型は指せない（`SCOPE_POLICY_ERROR`） |

これらが生成される `ActionParameterDef` の `refers_to` / `scope_semantics` を決めます。
つまりスコープ強制は、エンジンのハードコードではなく**作者が宣言する**ものです。

#### `ActionParams`

型付き Action パラメータモデルの基底クラス。フィールドには上記マーカーを使います。

```python
class EscalateTicketParams(ActionParams):
    ticket_id: str = target(Ticket)
    reason: str | None = None
```

`date` / `datetime` 型のパラメータは [date と datetime の値](api-stores.ja.md#date-と-datetime-の値) のプロパティの規則に従い、
拒否されると `INVALID_PARAMS` になります。MCP の `execute_action` では値が JSON
文字列なので、文字列の規則が適用されます。

#### イベント: `Event`・`@ontology.event`・`emits=`・`ctx.emit`・`client.events`

イベントは、Action が記録する業務上の事実です（たとえば「注文が出荷された」）。監査では
ありません。監査は「誰が何をしたか」を記録し、イベントは「何が起きたか」を記録します。
イベントは `Event` のサブクラスとして宣言します。フィールドの型規則は `ActionParams` と
同じで、スカラー・選択肢・フラットな struct を使えます。`Sensitivity` もプロパティと同様に
使え、制限付きフィールドは省略可能にします。イベントには primary key、transitions、
`scope_level` はありません。

```python
from ontary import Event

@ontology.event(description="An order has shipped.")
class OrderShipped(Event):
    carrier: str
```

`@ontology.event(*, description=None, api_name=None, accept=())` は、クラスを
`EventTypeDef` として登録します。クラスは `Event` のサブクラスである必要があり、1 つの
クラスに付けられるのは 1 回だけです。違反すると `ONTOLOGY_INVALID` を送出します。
`accept=` は `"EVENT_NEVER_EMITTED"` を受け取ります。

Action は、送出してよいイベントを `emits=[...]` に列挙します。`ActionTypeDef.emits` には
その api_name が入り、MCP の `list_action_types` に `emits` として表示されます。同じ
`Ontology` に登録されたイベントではないクラスを `emits` に渡すと、宣言時に
`ONTOLOGY_INVALID` になります。

```python
@ontology.action(ShipOrder, target=Order, roles=["ops"], emits=[OrderShipped])
def ship(ctx: ActionContext, p: ShipOrder) -> dict[str, Any]:
    order = ctx.get(Order, p.order_id)
    ...
    ctx.emit(OrderShipped(carrier=p.carrier))
    return {}
```

`ctx.emit(event, *, about=None)` は、Action のトランザクション内でイベントを記録します。
対象（subject）の既定値は、Action が解決した対象オブジェクトです。作成系の Action のように
対象 id がない場合は、`about=<object>` を渡します。そのオブジェクトは、このコンテキスト
（`get`・`all`・`create`・`traverse`）が渡したもので、Action の対象型でなければなりません。
1 回の呼び出しで複数のイベントを送出でき、同じ型を 2 回送出することもできます。イベントは
送出順に保持されます。イベントの `ts` は `ctx.now()` です。拒否はハンドラ内で送出される
ため、Action 全体がロールバックされ、`error` として監査されます。

| ケース | コード |
| --- | --- |
| イベントクラスがこの `Ontology` に登録されていない | `UNKNOWN_NAME` |
| イベントが Action の `emits` にない | `UNDECLARED_EVENT` |
| 対象を解決できない、対象型ではない、このコンテキストが渡していない、またはまだ行がない | `EVENT_SUBJECT_INVALID` |

`client.events(event_type=None, /, *, about=None, since=None, until=None)` は、クライアントの
コンシューマーが見られるイベントを、送出順の `list[EventRecord[E]]` で返します。`about` は
オブジェクトか `(cls, id)` のタプルです。`since` は含み、`until` は含みません。どちらも
タイムゾーン付きでなければなりません。ページングはありません。イベントクラスを渡すと
各ペイロードがそのクラスになり、渡さない場合は保存されたフィールドを保持する素の `Event`
になります。

```python
records = client.events(OrderShipped, about=(Order, "o-1"))
records[0].payload.carrier
```

対象の最新の行がコンシューマーのスコープと `row_visibility` を通るとき、イベントは
見えます。退役した対象では、その行は退役前の最後の行です。対象がスコープを移ると、新しい
スコープは履歴全体を見られ、古いスコープは何も見られません。宣言が削除されたイベント型は
隠れます。コンシューマーの種類に対して `Sensitivity` で制限されたペイロードフィールドは、
ペイロードでは `None` になり、`redacted_fields` に載ります。

`EventRecord` は frozen なジェネリッククラスです。フィールドは `event_type`、
`about_type`、`about_id`、`ts`、`invocation_id`、`payload`、
`redacted_fields: frozenset[str]` です。

イベントは呼び出しの監査行に保存されるため、Action と一緒にコミットまたはロールバックされます。
`AuditEntry.events` はそれらをマスクせずに列挙します。[`AuditEntry`](api-actions-functions.ja.md#auditentry) を
参照してください。

<a id="advisory-findings"></a>

#### アドバイザリーな finding: `Finding.guide`・`accept`・`snapshot`

`ontology.diagnose()` は、アドバイザリーなコード（モデリングの lint とセキュリティの
lint。一覧は [CLI リファレンス](cli.ja.md)）ごとに `Finding` を返します。`Finding` の
フィールドは `code`、`severity`、`location`、`message`、`fix_hint`、`guide` です。

- **`Finding.guide`**（`str | None`、デフォルトは `None`）は、その finding を説明する
  公開済み英語デザインガイドの該当セクションの URL です。`ontary.diagnose.GUIDE_URL`
  と `ontary.diagnose.GUIDE_ANCHORS` から `GUIDE_URL + "#" + anchor` として作られます。
  `ontary.diagnose.ADVISORY_CODES` のすべてのコードが設定します。ガイドのセクションが
  ない finding（`ONTOLOGY_INVALID`、`SCOPE_POLICY_ERROR`、`INVALID_RECORD`、
  `RULE_VIOLATED`、`DIAGNOSE_RULE_FAILED`）は `None` のままです。
- **`accept`** は、finding の原因になった宣言の場所で「これは意図したものです」と
  伝えます。lint コードを 1 つ、またはコードのシーケンスを渡します。accept した
  finding は `diagnose()`、`ontary validate` のテキスト出力、`--json` のどれにも
  現れません。下の表にないコードは拒否されます（最後の項目を参照）。

  | 宣言 | `accept` 引数 | 受け付けるコード |
  | --- | --- | --- |
  | `prop(...)` | `accept=` | `STORED_DERIVABLE`, `FREE_TEXT_STATUS` |
  | `@ontology.object(...)` | `accept=` | `FORBIDDEN_TYPE_NAME`, `AUDIT_TYPE` |
  | `@ontology.action(...)` | `accept=` | `CRUD_ACTION_NAME`, `MICRO_ACTION` |
  | `@ontology.function(...)` | `accept=` | `CRUD_ACTION_NAME` |
  | `@ontology.event(...)` | `accept=` | `EVENT_NEVER_EMITTED` |

  受け付ける名前は、`ontary.meta` の `Literal` エイリアス `PropertyLint`、
  `ObjectLint`、`ActionLint`、`FunctionLint`、`EventLint` です。`ontary` からは export されません
  が、誤ったコードを呼び出し側の `mypy` エラーにします。同じ `accept` フィールド
  （`tuple[str, ...]`、デフォルトは `()`）は `PropertyDef`、`ObjectTypeDef`、
  `ActionTypeDef`、`FunctionDef` にもあり、手で組み立てた記述子もデコレートした
  クラスと同じように動きます。
- **`snapshot`**（`bool`、デフォルトは `False`）は `@ontology.object(...)` の引数で、
  その型を時点のスナップショットとして宣言します。免除されるのは 2 つの finding です。
  その型のプロパティに対する `STORED_DERIVABLE` と、`Snapshot` という名前の接尾辞に
  対する `FORBIDDEN_TYPE_NAME` です。`V<数字>`、年、`History` の名前は免除されません。
  `snapshot=True` のない `*Snapshot` の型は、これまでどおり警告されます。
  `description` の「declared snapshot」という文字列には効果がありません。
- **拒否されるコード。** 宣言が受け付けられないコードを渡すと、宣言を組み立てる
  時点で `ValidationFailed`（コード `ONTOLOGY_INVALID`）が発生します。メッセージには、
  宣言、拒否されたコード、その宣言が受け付けるコードが含まれます。未知のコード、
  別の種類の宣言向けのコード、エラーコード、セキュリティ lint のコード
  （`UNSCOPED_SENSITIVE`、`MIN_N_UNSET`）が対象です。lint コードは finding のコード
  であり、raise されないため、`ERROR_CODES` にも[エラーコード表](api-reference.ja.md#エラーコード)にも
  ありません。

```python
@ontology.object(layer="L0", scope="unscoped", snapshot=True)
class AccountSnapshot(OntologyObject):
    id: str = prop(primary_key=True)
    health_score: int  # STORED_DERIVABLE は出ない: 宣言済みのスナップショット型

@ontology.object(layer="L0", scope="unscoped")
class Applicant(OntologyObject):
    id: str = prop(primary_key=True)
    credit_score: int = prop(accept="STORED_DERIVABLE")  # 外部の信用情報機関から記録
```

---

<a id="scope-policy"></a>

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

## 記述子による宣言

クラス宣言が構築されている、より低レベルの surface です。生成された、あるいはデータ
駆動のオントロジーに有用ですが、たいていの作者は `Ontology` を使うべきです。

| 型 | 主なフィールド |
| --- | --- |
| `ObjectTypeDef` | `api_name`, `display_name`, `description`, `layer`, `properties`, `primary_key`, `rules`, `owned` |
| `PropertyDef` | `name`, `type`, `choices`, `fields`, `transitions`, `required`, `sensitivity`, `scope_level` |
| `LinkTypeDef` | `api_name`, `from_type`, `to_type`, `cardinality`, `description`, `identity_revealing`, `owned` |
| `ActionTypeDef` | `api_name`, `display_name`, `target_type`, `executable_by_roles`, `description`, `parameters`, `capabilities` |
| `ActionParameterDef` | `name`, `type`, `choices`, `fields`, `required`, `refers_to`, `scope_semantics` |
| `StructFieldDef` | `name`, `type`, `choices`, `required` |
| `TransitionDef` | `initial`, `moves` |
| `RuleDef` | `name`, `message`, `check` |
| `FunctionDef` | `api_name`, `description`, `input_description`, `output_description`, `parameters`, `capabilities` |
| `Sensitivity` | `ai_usable`, `human_visible` |

`PropertyType` は `Literal["str", "int", "float", "bool", "date", "datetime", "json", "struct"]`。

`StructFieldDef` はフラットな内側のフィールドを 1 つ記述します。`type` は scalar の
プロパティ型で、`choices` は `str` フィールドに使う任意の文字列 tuple、`required` の
既定値は `True` です。`PropertyDef.fields` と `ActionParameterDef.fields` は
`type="struct"` では空でない tuple、それ以外の型では `None` です。MCP の schema は
tuple を list として出力し、常に `fields` キーを含めます。

`TransitionDef` は choice プロパティで許可する状態を記述します。`initial` は空でない
開始状態の tuple で、`moves` はすべての choice から許可する遷移先への対応です。
終端状態には空の tuple を使います。すべての状態は宣言済みの choice に含めます。
`PropertyDef.transitions` に設定します。

`RuleDef` は新しい行全体を受け取る名前付き述語を
`check: Callable[[dict[str, Any]], bool]` として記述します。`name` と `message` は
空にできず、同じ `ObjectTypeDef.rules` tuple 内の名前は一意である必要があります。
rule の check は渡されたオブジェクトだけを読み取るようにしてください。
`model_dump()` は `check` を除外するため、公開する宣言には rule の名前とメッセージ
だけが含まれます。

**`OntologyRegistry`** が記述子を保持し、相互参照を検証します。`validate()` は、
リンク端点の参照切れ、Action 対象の参照切れ、api_name の重複、properties に無い主キーに
対して `ValidationFailed`（`ONTOLOGY_INVALID`）を送出します。

**`OntologyDef`** は registry + スコープポリシー + 設定をまとめたもので、クライアントや
MCP サーバーがバインドする単位です。この SDK には**モジュールグローバルなレジストリが
意図的に存在しません**。したがって 2 つのオントロジーが 1 プロセス内で干渉せず共存できます。

**`Declarations`** / `declarations(...)` は宣言された契約をデータとして公開します —
MCP の `get_declarations` が返すものです: `authority`（モデル宣言・実行時チェック）、
`capabilities`（action/function ごとに宣言され、未提供・未宣言なら fail-closed。
provider 自体はサンドボックス化されない作者コード）、`writeback`
（オントロジーへの書き込みはすべてオントロジー所有。実行時自身の外部書き込み経路は
持たないが、capability provider はインラインで外部書き込みもできるため、
これは宣言された規約であって強制された境界ではない）、`reingest`
（upsert-merge。所有プロパティは残り、削除はない）、`visibility_default`
（deny-by-default — 未解決のスコープは隠れる）、`transaction_ownership`
（実行時所有 — 呼び出し元が開いたトランザクションを拒否）、
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
マッピングした場合、それはログ上で可視化されます。
監査された行のすべてが `principal` を持つわけではありません。`min_n`
はオントロジー固有の唯一の答えで、オントロジー自身の `ScopePolicy.min_n`
から読み取られます。

---

