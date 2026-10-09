# `ontary` — API リファレンス: 読み取り

[English](api-reading.md) · **日本語** · [← API リファレンス](api-reference.ja.md)

*リファレンス* — 読み取りの契約を掲載します。[API リファレンス](api-reference.ja.md) と [はじめに](getting-started.ja.md) も参照してください。

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

どちらも `items`、不透明な `next_cursor: str | None`、`has_more: bool` を持ちます。consumer surface では
`limit` の省略で上記の既定上限を使ったページになり、`limit=None` は明示的な上限なしリストの
指定です。宣言された Function 内では `BoundQuery.list` は `limit` の省略または `None` で bare
list を返し、正の `limit` を渡すとページを返します。

契約:

- **ページサイズは正確。** 可視な行がまだ `limit` 件残っている限り、ページはちょうど
  `limit` 件を持ちます — ストア読み取りより下流のフィルタがページを短くすることは
  ありません。短いページは常に「もう行が無い」を意味し、「一部が隠された」ではありません。
- **カーソルは不透明。** 行ごとのランダムなトークンです。行 id でも件数でも順序でも
  ありません。パースせず、保存して `after=` に返すだけにしてください。
- **`next_cursor` が `None` になるのは `has_more` が `False` のときだけです。**
  `has_more` が `True` のとき、`next_cursor` は最後に返した行の不透明なキーです。
  ちょうど埋まった最後のページが、空ページへ続くカーソルを返すことはなくなりました。
  よって `while page.next_cursor is not None:` のループは常に正しく終了します。
- **`has_more` は可視な行だけを数えます。** スコープで見えない行が原因で `True` に
  なることはありません。
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
ハンドル先頭の型付き形式を受け付けます。型付きハンドル形式にページネーションはありません。

文字列形式はページングできます。
`client.traverse("Team", "inTeam", "a", reverse=True, limit=100, after=cursor)` は
`Page` を返します。`limit` を省略すると、従来どおり全件のリストを返します。
`limit` なしの `after` は `AFTER_WITHOUT_LIMIT`、`limit < 1` は `INVALID_LIMIT` に
なります。現在の可視なリンク先の行を指さなくなった traverse カーソル（リンクが
閉じられた、または行が retire された、もしくは見えなくなった場合）は
`STALE_CURSOR` になります。先頭ページからやり直してください。ループは他のページと
同じく `while page.next_cursor is not None:` で回します。

返される対象行にも通常どおり可視性チェックが適用されます。identity-revealing な
リンクの traverse は、human コンシューマーに対しては対象を返す前に `VisibilityError`
（`VISIBILITY_DENIED`）になります。AI コンシューマーには対象行への通常のスコープと
sensitivity の強制が適用されます。`reverse=True` はリンクの対象側から辿り、
identity-revealing の拒否は双方向で対称です。

### `traverse_many`

`traverse_many` は、1 回の呼び出しで多数のアンカーから同じリンクを辿ります。形式は
`traverse` と同じく 2 通りです。型付き形式は
`client.traverse_many(LinkHandle, anchors, reverse=False)` で、各アンカーにはオブジェクト
またはその id を渡します。文字列形式は
`client.traverse_many(obj_type, link, anchor_ids, reverse=False)` です。

```python
by_comment = client.traverse_many("Comment", "commentOnTicket", [comment_a, comment_b])
# {"comment-a": [<ticket row>], "comment-b": []}
```

戻り値は `{anchor_id: [rows]}` の `dict` です。キーは重複を除いたアンカー id で、最初に
現れた順に並びます。重複したアンカーは 1 つのキーにまとまります。未知のアンカーは `[]`
になります。各リストのスコープによる絞り込み、リダクション、行の順序はアンカーごとに
`traverse` と同じで、identity-revealing なリンクの拒否も同じように適用されます。
`reverse=True` はリンクの対象側から辿ります。

アンカーに `str` や `bytes` をそのまま渡すと、`INVALID_PARAMS` で拒否されます。1 つの
アンカーから辿るときは、`["comment-a"]` のようにリストに入れて渡してください。

アンカーの数によらず、読み取りの回数は一定です（リンクの読み取り 1 回とオブジェクトの
読み取り 1 回）。ページングはなく、`limit` と `after` は受け付けません。
`traverse_many` はまだ MCP では使えません。

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

### 読み取りにおけるスコープ制限とリダクション

Python の読み取りは、返す前にスコープ、行の可視性、`Sensitivity` を適用します。
行が隠されたかどうかを、呼び出しごとに伝える Python の読み取りはありません。
隠されたフィールドの現れ方は、行を返すどのメソッドでも同じです。
型付きの読み取りは、リダクションされたフィールドを `None` として返し、その名前を `redacted_fields` に載せます。
文字列形式の読み取りは、`payload` からそのキーを除き、`redacted_fields` に相当するものはありません。

- **`get`** は、存在しないオブジェクトや退役したオブジェクトに対して `None` を返します。
  コンシューマーのスコープや行の可視性の外にあるオブジェクトでは、`VISIBILITY_DENIED` の `VisibilityError` を送出します。
  リダクションされたフィールドは、上の型付きと文字列形式の規則に従います。
- **`list`** は、見える行だけを返し、リダクションは同じ二つの規則に従います。
  短いページは、常に「これ以上の行はない」を意味します。
- **`count`** は、見える行だけを数えます。
  すべての行からスコープで外れたコンシューマーには `0` が返ります。
- **`exists`** は、見える行だけを調べます。
  すべての行からスコープで外れたコンシューマーには `False` が返ります。
- **`traverse`** は、見える対象行だけを返し、リダクションは同じ二つの規則に従います。
- **`events`** は、見えるイベントだけを返します。
  リダクションされたペイロードのフィールドは `None` で、レコードの `redacted_fields` に名前が載ります。

MCP surface は異なります。読み取り結果に `scope_limited` フラグと、行ごとの `redacted_fields` が付きます。
Python surface は変わりません。
[MCP の読み取り結果](api-mcp.ja.md#読み取り結果) を参照してください。

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

文字列で型を指定する読み取りは、すべて未登録のオブジェクト型を
`UNKNOWN_OBJECT_TYPE` で拒否します。対象は `list`、`count`、`get`、`exists`、
`traverse`、`aggregate`、`aggregate_by`、`count_contributors`、`GuardedQuery` の
各メソッド、Function 本体での `BoundQuery` の文字列読み取りです。型の検査は
ほかのどの検査よりも先に、行を読む前に走ります。そのため拒否の結果は、
呼び出し元のスコープによらず同じです。`traverse` では、未登録のアンカー型が
この拒否の対象です。未知のリンクは引き続き `UNKNOWN_NAME` です。

### `GuardedQuery(store, registry, policy)`

呼び出しごとに明示的な `consumer` を取る、エンジンレベルの読み取り経路:
`.get_object`、`.get_objects`、`.traverse`、`.aggregate`、`.aggregate_by`、
`.count`、`.exists`、`.count_contributors`。通常の呼び出し側は代わりに
`OntologyClient` を使います。

---

