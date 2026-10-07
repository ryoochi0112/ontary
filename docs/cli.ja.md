# CLI リファレンス

[English](cli.md) · **日本語** · [← README](../README.md)

*リファレンス* — `ontary` のコマンドラインツールを調べるためのページで、最初に読むページは[はじめに](getting-started.ja.md)です。

## 概要

本パッケージは、2つのコンソールスクリプトをインストールします。これらは `ontary`（`ontary.cli:main`）と `ontary-mcp`（`ontary.mcp_server:main`）です。

`ontary` はメインのエントリーポイントです。オントロジー（データの定義構造）の検証や、ローカル開発サーバーの起動に使用します。`ontary-mcp` は、カスタムの Model Context Protocol（MCP）サーバーを構築するためのガイダンスを提供します。

## ターゲットの形式

`ontary` コマンドは、`pkg.module:attr` 形式のターゲット指定を受け付けます。CLI は指定されたモジュールをインポートし、対象の属性を取り出します。

属性は `Ontology` インスタンス、または引数なしで `Ontology` を返す呼び出し可能オブジェクトである必要があります。または、`(Ontology, Store)` タプルを返す呼び出し可能オブジェクトも指定できます。これら以外の型の属性を指定した場合は、ロードに失敗します。

## ontary validate

`ontary validate TARGET [--json] [--strict]` コマンドは、オントロジーを検証して診断結果を報告します。`ontology.diagnose()` を使用してターゲットを評価します。この完全なコレクターは、既定では終了コードに影響しないアドバイザリを含む、すべての診断結果を収集します。

検出事項は `[SEVERITY] CODE at LOCATION: MESSAGE` という形式で出力されます。各検出事項には、インデントされた `fix: HINT` 行が続きます。ガイドへのリンクがある場合は、その次にインデントされた `guide: URL` 行が続きます。検出事項がない場合は、`No findings.` と出力されます。

通常は `error` 重要度の検出事項がある場合に限り、終了コードが 1 になります。`--strict` を指定すると、残っている `warn` 重要度の検出事項がある場合も終了コードが 1 になります。`info` 重要度の検出事項は終了コードに影響しません。受け入れ済みの検出事項は `diagnose()` から除外されます。

### テキスト出力の例

```
[WARN] STORED_DERIVABLE at object Ticket, property avg_response_hours: the name reads as a score or aggregate
  fix: if Ticket.avg_response_hours is computed from other rows, derive it with a Function; if it is recorded from outside, add accept="STORED_DERIVABLE" to the property
  guide: https://ryoochi0112.github.io/ontary/ontology-design/#normalization-and-derived-values
```

### JSON出力フォーマット

`--json` フラグを指定して実行すると、検出事項が JSON 配列として出力されます。各検出オブジェクトには、`code`、`severity`、`location`、`message`、`fix_hint`、および `guide` キーが含まれます。`guide` の値はガイド URL です。ガイドへのリンクがない検出事項では `null` になります。`severity` キーの値は、`error`、`warn`、または `info` です。JSON 出力でも `--strict` は同じ終了コード規則を適用します。

### アドバイザリ検出コード

`diagnose()` は次のアドバイザリ検出事項を出力することがあります。各行に、そのコードが発生する宣言の形を示します。

| コード | 発生条件 |
|---|---|
| `AUDIT_TYPE` | オブジェクト型名の末尾が `AuditLog`、`AuditEntry`、`AuditTrail`、`AuditRecord`、または `AuditEvent` の場合に発生します。 |
| `CRUD_ACTION_NAME` | Action または Function の API 名の先頭語が、大文字と小文字を区別せず `Set`、`Update`、`Create`、`Delete`、`Remove`、または `Erase` の場合に発生します。 |
| `EVENT_NEVER_EMITTED` | 登録されたイベントがどの Action の `emits` にも含まれていない場合に発生します。Action の `emits=[...]` に追加するか削除してください。イベントに `accept="EVENT_NEVER_EMITTED"` を指定すると受け入れられます。 |
| `FORBIDDEN_TYPE_NAME` | オブジェクト型名が `V` と数字、`History`、または 1900〜2099 の年で終わる場合に発生します。`snapshot=True` がない `Snapshot` 末尾も対象です。 |
| `FREE_TEXT_STATUS` | `status` または `*_status` という名前の `str` 型プロパティに選択肢が宣言されていない場合に発生します。 |
| `MICRO_ACTION` | Action の対象以外のパラメーターが1つだけあり、その名前が対象型のプロパティ名と一致する場合に発生します。 |
| `MISSING_DESCRIPTION` | Action または Function の `description=` がないか空で、エージェントには既定の文しか見えない場合に発生します。 |
| `MIN_N_UNSET` | 機微なプロパティが宣言され、`min_n` が既定値の 3 のままの場合に発生します。 |
| `STORED_DERIVABLE` | プロパティ名に `avg_`、`total_`、`_score` などの集計を表す接頭辞または接尾辞がある場合に発生します。 |
| `UNSCOPED_SENSITIVE` | スコープ規則も明示的な非スコープ宣言もないオブジェクト型に機微なプロパティがある場合に発生します。 |

`MIN_N_UNSET` の重要度は `info` です。ほかのアドバイザリコードの重要度は `warn` です。エラーには、宣言の不備に `ONTOLOGY_INVALID`、保存済み行のハイドレーション失敗に `INVALID_RECORD`、宣言済みルールへの違反に `RULE_VIOLATED` を使います。

### オプション

| フラグ | 説明 |
|---|---|
| `--json` | 検出事項をテキストではなく JSON 配列として出力します。 |
| `--strict` | `error` に加え、残っている `warn` 検出事項がある場合に終了コード 1 で終了します。 |

## ontary serve

`ontary serve TARGET --dev [--store PATH] [--port N]` コマンドは、開発用にローカル MCP サーバー経由でオントロジーを提供します。`--dev` フラグは必須です。このフラグは、ローカルホスト限定の開発サーバーを実行していることを承認するものです。

サーバーは `127.0.0.1` のデフォルトポート 8000 にバインドします。ストリーム可能な HTTP トランスポートを使用します。

### ストアの選択

サーバーは、厳密な優先順位に従ってデータストアを決定します。第一に、`--store` パスで指定されたストアを使用します。第二に、ターゲットビルダーのタプルから返されたストアを使用します。それ以外の場合は、新しい `InMemoryStore` インスタンスを使用します。

### 開発用コンシューマー

開発サーバーは、特定のプロパティを持つデフォルトのコンシューマーを使用します。プロパティには、`actor_id="ontary-dev"`、`role="ontary-dev"`、`scope_level="dev"`、`scope_id="localhost"`、および `kind="human"` が含まれます。

コンシューマーは、スコープが設定されていない行のみを参照できます。オントロジーが対応する開発用スコープを宣言していない限り、スコープ付きの行は参照できません。スコープ付きオントロジーに対しては、アクションを実行できません。

非表示の行は `ScopePolicy` によって制御されます。詳細は [api-reference.md#scope-policy](api-reference.md#scope-policy) を参照してください。起動された MCP サーバーの名前は `"<ontology name> (ontary dev)"` になります。本番環境へのデプロイについては、[mcp-serving.md](mcp-serving.md) を参照してください。

### オプション

| フラグ | 説明 |
|---|---|
| `--dev` | ローカル開発サーバーを実行していることを承認します。必須です。 |
| `--store PATH` | SQLite ファイルへのパスです。 |
| `--port N` | サーバーのポート番号（1 から 65535）です。デフォルトは 8000 です。 |

### 実行例

```bash
ontary serve your_app.ontology:ontology --dev --store ./dev.sqlite --port 8000
```

## ontary version

`ontary version` コマンドは、インストールされているパッケージのバージョンを出力します。その後、終了コード 0 で終了します。

## ontary-mcp

`ontary-mcp` コマンドは、オントロジーを直接提供できません。`ontary-mcp` または `python -m ontary.mcp_server` を実行すると、メッセージを表示して終了します。メッセージは、独自のエントリーポイントスクリプトから `build_mcp_server(ontology, store, consumer).run()` を呼び出すように指示します。`mcp` エクストラ（追加オプション）がない場合、スクリプトはまずその不足を報告します。

## 依存関係の要件

`ontary.cli` モジュールは、`mcp` エクストラなしでインポートできます。`ontary serve` と `ontary-mcp` のみが `mcp` エクストラを必要とします。`mcp` エクストラがない場合、`ontary serve` はエラーを報告します。エラーメッセージには、インストールすべき対象の範囲が示されます。

## 終了コード

| コマンド | コード | 意味 |
|---|---|---|
| `ontary validate [--strict]` | `0` | `error` 重要度の検出事項がありません。`--strict` 指定時は `warn` 重要度の検出事項もありません。`info` は終了コードに影響しません。 |
| `ontary validate [--strict]` | `1` | `error` 重要度の検出事項がある場合、または `--strict` 指定時に `warn` 重要度の検出事項がある場合。 |
| `ontary validate` | `2` | ターゲットのロードまたは診断ができない場合。ロード失敗時は、標準エラー出力に `ontary: <reason>` が出力されます。 |
| `ontary serve` | `0` | サーバーが正常に停止した場合。 |
| `ontary serve` | `2` | ターゲットもしくはストアをロードできないか、または `mcp` エクストラがない場合。 |
| `ontary version` | `0` | バージョンの出力に成功した場合。 |
| `ontary-mcp` | `1` | インポートせずにスクリプトが直接実行された場合。案内メッセージを表示して終了します。 |
