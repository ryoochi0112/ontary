# `ontary` — API リファレンス: ストアとバルク取り込み

[English](api-stores.md) · **日本語** · [← API リファレンス](api-reference.ja.md)

*リファレンス* — ストアとバルク取り込みの契約を掲載します。[API リファレンス](api-reference.ja.md) と [ストレージ・テナンシー・スキーマ](storage.md) も参照してください。

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

`insert` は、同じ object type に有効な行がすでにある primary key を受け付けません。
`ConflictError`（code `OBJECT_ALREADY_EXISTS`）で拒否し、ストアは変更しません。
1 つのオブジェクトが持つ有効な行は最大 1 行です。SQL バックエンドは部分ユニーク
インデックスでもこれを保証します。retire 済みのオブジェクトの id は再び insert
でき、新しい有効な行が始まります。

`insert` と `update` は `date` / `datetime` の値を、ISO-8601 文字列または `date` /
オフセット付き `datetime` オブジェクトとして受け取ります。[date と datetime の値](#date-と-datetime-の値) を参照してください。

`create_link` は両端に有効なオブジェクトを必要とします。各 id はリンク型が宣言する
`from_type` / `to_type` で検索します。存在しない id、retire 済みの id、別の型としてのみ
有効な id は `ValidationFailed`（code `LINK_ENDPOINT_NOT_FOUND`）で拒否し、リンク行は
書き込みません。端点の検査はカーディナリティより先に行います。同じアクション
トランザクション内で先に insert したオブジェクトは有効として扱います。

`create_link` は冪等です。有効なリンクと同一のリンクを作成する呼び出しは、どの
カーディナリティでも no-op になります。何も書き込まず、`WriteRecord` も記録せず、
正常に返ります。以前は MANY_TO_MANY のリンクが重複し、MANY_TO_ONE のリンクは自分自身に
対して `CARDINALITY_VIOLATION` で拒否されていました。同一リンクの検査はカーディナリティ
より先に行います。対象は有効なリンクだけです。`close_link` の後は同じペアを再び
リンクでき、閉じた行は履歴として残ります。SQL バックエンドは有効なリンクに対する
部分ユニークインデックスでもこれを保証します。

primary key は変更できません。`update` は、primary key を別の値に変える変更を
`ValidationFailed`（code `PRIMARY_KEY_IMMUTABLE`）で拒否し、ストアは変更しません。
これにより、ストアの id と payload の primary key は常に一致します。現在と同じ値を
渡す場合は変更とみなさず、受け付けます。新しいキーが必要なときは、オブジェクトを
retire してから新しく insert してください。

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

3 つの実装が同梱され、すべて同じ適合性テストスイートで検証されています。

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

### date と datetime の値

`date` / `datetime` 型のプロパティの値は、どの書き込み経路でも同じ方法で検査します。
対象は `Store.insert` / `update`、`ActionContext.create` / `save`、`bulk_upsert` /
`client.ingest`、Action パラメータ（MCP の `execute_action` を含む）です。

| 書き込む値 | `date` プロパティ | `datetime` プロパティ |
| --- | --- | --- |
| `date` オブジェクト | `YYYY-MM-DD` として保存 | 拒否 |
| オフセット付き（aware）の `datetime` オブジェクト | 拒否 | 自身の `isoformat()` の綴りで、オフセットを保って保存 |
| naive な `datetime` オブジェクト（`tzinfo` なし） | 拒否 | 拒否 |
| `"YYYY-MM-DD"` 文字列 | そのまま保存 | 拒否（時刻部分が必要） |
| `"20261005"` などの別の ISO-8601 日付表記 | 拒否 | — |
| 時刻を含む ISO-8601 文字列（オフセットの有無を問わず、`Z` を含む） | — | そのまま保存 |
| その他の文字列 | 拒否 | 拒否 |

- **正規化はしません。** ストアは綴りをそのまま保存します。オフセットを UTC に
  変換せず、`Z` も `Z` のままです。`eq` と `in` のフィルターはこの綴りで一致を
  判定します。比較演算子は時点（instant）で比較します
  （[フィルター、順序、読み取り上限](api-reading.ja.md#フィルター順序読み取り上限)を参照）。
- **naive な文字列は受け付け、naive なオブジェクトは拒否します。** naive な ISO
  *文字列* は書いたとおりに保存します。naive な `datetime` *オブジェクト* は拒否
  します。どの時点を指すかは `tzinfo=` を付けて示す必要があるためです。naive な値と
  オフセット付きの値を 1 つのプロパティに混在させないでください。両者の間に順序は
  定義されていません。
- **拒否時のコード。** 拒否された値は、`Store.insert` / `update` と
  `ActionContext.create` / `save` では `ValidationFailed`（`INVALID_RECORD`）を
  送出します。取り込みではレコードごとにレポートへ `INVALID_RECORD` が載ります。
  Action パラメータはハンドラの実行前に `INVALID_PARAMS` で拒否されます。`where` の
  operand は `OPERATOR_TYPE_MISMATCH` で拒否されます。

```python
from datetime import datetime, timedelta, timezone

from ontary import InMemoryStore, Ontology, OntologyObject, Source, prop
from ontary.errors import ValidationFailed

ontology = Ontology("shifts", scope_levels=["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped")
class Shift(OntologyObject):
    id: str = prop(primary_key=True)
    starts_at: datetime


ontology.validate()
store = InMemoryStore(ontology.registry)
source = Source(source_system="roster")
jst = timezone(timedelta(hours=9))

store.insert("Shift", {"id": "s1", "starts_at": datetime(2026, 10, 5, 9, tzinfo=jst)}, source)
store.insert("Shift", {"id": "s2", "starts_at": "2026-10-05T00:00:00Z"}, source)
assert store.read_current("Shift", "s1").payload["starts_at"] == "2026-10-05T09:00:00+09:00"
assert store.read_current("Shift", "s2").payload["starts_at"] == "2026-10-05T00:00:00Z"

try:
    store.insert("Shift", {"id": "s3", "starts_at": datetime(2026, 10, 5, 9)}, source)
except ValidationFailed as exc:
    assert exc.code == "INVALID_RECORD"  # a naive datetime object
else:
    raise AssertionError("a naive datetime object must be refused")
```

### スキーマバージョニング

すべての SQLite ファイルは、作成時にエンジンの `SCHEMA_VERSION` を `PRAGMA
user_version` へ刻印します。Postgres も同じ番号を `schema_meta` に記録します。
どちらのバックエンドも移行のはしご（migration ladder）を持ちません。そのため、刻印が
一致しないストアはすべて、構築時点で `STORE_VERSION_UNSUPPORTED` として両方のバージョンを
挙げて拒否します。刻印が高い場合も低い場合も、未刻印で `objects` テーブルをすでに
持つ場合も同じです。スキーマバージョンをまたぐ移行はオペレーターの明示的な手順です。
対応する ontary バージョンで開くか、新しいストアにデータを移してください。
drop して作り直す手順は [storage.md](storage.md#moving-across-a-schema-version) にあります。

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

`client.ingest` はオブジェクト型の名前を文字列（`"Ticket"`）で受け取り、`client.ingest_links` は
リンクの `api_name` を受け取ります。型付きの読み取り（`get`、`list`、`traverse`）はクラスまたは
`LinkHandle` を受け取ります。ingest が型名を受け取るのは、レコードが通常ソースシステムから
素の dict として届き、型付きモデルより先に存在することが多いためです。クラスが手元にある場合は
`Ticket.__name__` を渡してください。宣言した `api_name` が異なる場合はそちらを渡します。

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
`OWNED_PROPERTY_REFUSED` になります。端点に有効な行がないリンクのペアは、
カーディナリティ違反と同様にペア単位で `LINK_ENDPOINT_NOT_FOUND` として拒否します。
他のペアはそのまま登録されるため、オブジェクトをリンクより先に ingest してください。
リンクの読み込みの再実行は冪等です。有効なリンクと同一のペアは no-op になり、
`inserted_ids` にはそのまま含まれるため、2 回目の実行は 1 回目と同じレポートを返します。

`date` / `datetime` の値は、ISO-8601 文字列または `date` / オフセット付き
`datetime` オブジェクトとして書き込みます。naive な `datetime` オブジェクトは
`INVALID_RECORD` で拒否されます。[date と datetime の値](#date-と-datetime-の値) を参照してください。

---

