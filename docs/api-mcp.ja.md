# `ontary` — API リファレンス: MCP サーバー

[English](api-mcp.md) · **日本語** · [← API リファレンス](api-reference.ja.md)

*リファレンス* — MCP サーバーの契約を掲載します。[API リファレンス](api-reference.ja.md) と [はじめに](getting-started.ja.md) も参照してください。

<a id="mcp-server"></a>

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

ツールの引数。公開される入力スキーマはすべての引数を任意としています。各ツールが自分で引数を
検証するためです。必須の引数がないと `INVALID_PARAMS`（`missing required tool parameter`）で
拒否されます。`aggregate_objects` は、`func` が `"count"` 以外のとき `value_field` も必要です。
ない場合、エンジンが `INVALID_PARAMS`（`value_field is required`）で拒否します。`execute_action` と
`call_function` は、Action や Function のパラメータをパラメータ名をキーとする 1 つの `params`
オブジェクトで受け取ります。パラメータがなくても `{}` を渡してください。`list_action_types` と
`list_functions` は各パラメータの名前・型・`description`・必須かどうかを公開します。
`description` は説明がないパラメータでは `null` です。

| ツール | 必須の引数 | 任意の引数 |
| --- | --- | --- |
| `list_object_types`, `list_link_types`, `list_action_types`, `list_functions`, `get_declarations` | — | — |
| `get_object` | `obj_type`, `obj_id` | — |
| `query_objects` | `obj_type` | `where`, `order_by`, `limit`, `after`, `include_total` （既定値 `false`） |
| `count_objects` | `obj_type` | `where` |
| `aggregate_objects` | `obj_type` | `value_field`, `group_by`, `where`, `func` （既定値 `"mean"`） |
| `traverse_links` | `obj_type`, `obj_id`, `link_api_name` | `reverse` （既定値 `false`）, `limit`, `after`, `include_total` （既定値 `false`） |
| `execute_action` | `api_name`, `params` | — |
| `call_function` | `api_name`, `params` | — |

`list_object_types` はすべてのプロパティに `transitions` キーを含めます。グラフがない場合は
`null`、ある場合は完全な `initial` 状態リストと `moves` の対応を返します。各オブジェクト型には
`rules` リストもあり、各ルールの `name` と `message` を含みます。ルールのコードは含まれません。

`query_objects(obj_type, where=None, order_by=None, limit=None, after=None, include_total=False)` は MCP surface では常に
上限付きです。`limit` を省略するとサーバーのデフォルト上限 100 行を使い、明示する
場合の最大値は 1000 です。内部のページ付き読み取りが不透明な `next_cursor` を返し、
成功時のレスポンスは従来どおり行を `result` に置いたまま、同じ階層に `next_cursor`
と `has_more` を追加します。`next_cursor` が `null` になるのは `has_more` が `false` のときだけです。
可視な最後の行でちょうど終わるページにもカーソルはありません。同じ明示的な `limit` とともにカーソルを返して
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
`traverse_links` も同じようにページ付けします。`limit` を省略すると既定の 100 行を 1 ページ返し
（明示する場合の最大値は 1000 です）、`next_cursor` と `has_more` の規則も同じです。`limit` なしの
`after` は `AFTER_WITHOUT_LIMIT` になります。

ページ付きの 2 つのツールは `include_total`（デフォルトは `false`）を受け付けます。`true` にすると、
レスポンスのトップレベルに `total` を追加します。値は呼び出し元に可視な行数で、`count_objects` と
同じ数です。min-N による公開判定は行いません。ページとはトランザクションが別なので、2 つの読み取りの間に
書き込みがあると `total` とページが食い違うことがあります。`include_total` がなければ `total` キーはありません。
`limit=0` と `include_total=true` の組み合わせは件数のみのモードです。`result` は `[]`、
`next_cursor` は `null`、`has_more` は `total > 0` で、`order_by` は無視し、`after` は
`INVALID_LIMIT` で拒否します。`include_total` なしの `limit=0` は `INVALID_LIMIT` です。

`reverse=true` を渡すとリンクの target 側から辿って source 側のオブジェクトを返します。
本人特定リンクの拒否は両方向で対称です。

レスポンスのキーとオブジェクト行のシリアライズは[読み取り結果](#read-results)を参照してください。
エラーは下表のコードを返し、分類できないものは内部情報を
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

<a id="read-results"></a>

## 読み取り結果

次の表は、成功した読み取りレスポンスのトップレベルのキーと、オブジェクト行のキーをすべて示します。
エラー時はエラーエンベロープを使います。

| ツール | トップレベルのキー | 行のキー | `result` の値 | スコープの方針 |
| --- | --- | --- | --- | --- |
| `get_object` | `result` | `payload`, `lineage`, `redacted_fields` | オブジェクト行、または存在しない・退役したオブジェクトに対する `null` です。 | `scope_limited` はありません。スコープ外の ID は `VISIBILITY_DENIED` で拒否します。 |
| `query_objects` | `result`, `next_cursor`, `has_more`, `total`, `scope_limited` | `payload`, `lineage`, `redacted_fields` | オブジェクト行のリストです。 | `scope_limited` は検索対象の型の宣言に従います。 |
| `count_objects` | `result`, `scope_limited` | — | 可視行の件数を表す整数です。 | `scope_limited` は件数を数える型の宣言に従います。 |
| `traverse_links` | `result`, `next_cursor`, `has_more`, `total`, `scope_limited` | `payload`, `lineage`, `redacted_fields` | オブジェクト行のリストです。 | `scope_limited` は結果の型の宣言に従います。 |

`scope_limited` は、成功した検索・件数取得・リンク走査で常に返す真偽値です。
空のリストや件数がゼロの場合も返します。
結果の型が `unscoped_types` に含まれないか、`row_visibility` ルールを持つ場合に限り `true` になります。
`traverse_links` の結果の型は、通常はリンクの `to_type` です。
`reverse=true` の場合は `from_type` です。
このマーカーは、このコンシューマーと型に対する宣言だけで決まります。
保存されたデータには依存しません。
隠された行の件数も、隠された行があったかどうかも開示しません。
`get_object` は、結果が `null` の場合も拒否する場合も、`scope_limited` を返しません。

オブジェクト行の形は `{"payload": {...}, "lineage": {...}, "redacted_fields": [...]}` です。
`redacted_fields` は、このコンシューマーに隠されたプロパティ名をソートしたリストです。
隠されたプロパティがなければ `[]` です。
隠されたキーは `payload` に含めません。
`null` を設定することはありません。
人間のコンシューマーには、`human_visible=False` のフィールドを列挙します。
AI のコンシューマーには、`ai_usable=False` のフィールドを列挙します。
`DirectProperty` で宣言したスコープ振り分け用のキーが隠される場合も列挙します。

`total` は、呼び出しが `include_total=true` を指定した場合にだけ返します。

エージェントが「チーム `a` に紐づくチケットは何件ですか。最初の 2 件も見せてください」と尋ねます。
`traverse_links` を `obj_type="Team"`、`obj_id="a"`、`link_api_name="inTeam"`、`reverse=true`、
`limit=2`、`include_total=true` で呼ぶと、次の結果を得ます。

```json
{"result": [{"payload": {...}, "lineage": {...}, "redacted_fields": []},
            {"payload": {...}, "lineage": {...}, "redacted_fields": []}],
 "next_cursor": "<opaque>", "has_more": true, "total": 218, "scope_limited": true}
```

エージェントは次のように答えます。「チーム a に紐づくチケットは 218 件です（見えている範囲の件数で、
スコープによって見えない行がある可能性があります）。最初の 2 件を示します。残りもページ送りで確認できます。」

短いページや `next_cursor: null` は、呼び出し元に可視な選択範囲で「これ以上行がない」ことを意味します。
「隠された行があった」ことを意味するものではありません。
どちらも、その選択範囲の外に行が存在するかどうかは示しません。

5 つの読み取りツール（`query_objects`、`count_objects`、`get_object`、
`traverse_links`、`aggregate_objects`）に宣言されていない `obj_type` を渡すと、
`UNKNOWN_OBJECT_TYPE` のエラーエンベロープを返します。
この検査は、`limit` の上限やリンクの解決など、ほかのどの検査よりも先に走ります。
レスポンスには `result` も `scope_limited` も含みません。
そのため、`scope_limited: true` の空リストは、常に宣言済みの型に対する結果です。
たとえば `obj_type="Tickte"` は次を返します。

```json
{"error": {"type": "ValidationFailed", "message": "unregistered object type: 'Tickte'", "code": "UNKNOWN_OBJECT_TYPE", "kind": "validation"}}
```

---
