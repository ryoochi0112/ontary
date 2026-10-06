# `ontary` — API リファレンス: MCP サーバー

[English](api-mcp.md) · **日本語** · [← API リファレンス](api-reference.ja.md)

*リファレンス* — MCP サーバーの契約を掲載します。[API リファレンス](api-reference.ja.md) と [はじめに](getting-started.ja.md) も参照してください。

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

