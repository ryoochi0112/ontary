# CLI リファレンス

[English](cli.md) · **日本語** · [← README](../README.md)

## 概要

本パッケージは、2つのコンソールスクリプトをインストールします。これらは `ontary`（`ontary.cli:main`）と `ontary-mcp`（`ontary.mcp_server:main`）です。

`ontary` はメインのエントリーポイントです。オントロジー（データの定義構造）の検証や、ローカル開発サーバーの起動に使用します。`ontary-mcp` は、カスタムの Model Context Protocol（MCP）サーバーを構築するためのガイダンスを提供します。

## ターゲットの形式

`ontary` コマンドは、`pkg.module:attr` 形式のターゲット指定を受け付けます。CLI は指定されたモジュールをインポートし、対象の属性を取り出します。

属性は `Ontology` インスタンス、または引数なしで `Ontology` を返す呼び出し可能オブジェクトである必要があります。または、`(Ontology, Store)` タプルを返す呼び出し可能オブジェクトも指定できます。これら以外の型の属性を指定した場合は、ロードに失敗します。

## ontary validate

`ontary validate TARGET [--json]` コマンドは、オントロジーを検証して診断結果を報告します。`ontology.diagnose()` を使用してターゲットを評価します。この完全なコレクター（収集機能）は、検証を失敗させるすべての検出事項を網羅して取得します。

検出事項は `[SEVERITY] CODE at LOCATION: MESSAGE` という形式で出力されます。各検出事項には、インデントされた `fix: HINT` 行が続きます。検出事項がない場合は、`No findings.` と出力されます。

### テキスト出力の例

```
[WARN] STORED_DERIVABLE at Ticket.total_amount: property looks like a stored aggregate
  fix: derive it with a Function instead of storing it
```

### JSON出力フォーマット

`--json` フラグを指定して実行すると、検出事項が JSON 配列として出力されます。各検出オブジェクトには、`code`、`severity`、`location`、`message`、および `fix_hint` キーが含まれます。`severity` キーの値は、`error`、`warn`、または `info` です。

### アドバイザリ警告コード

`diagnose` プロセスは、いくつかのアドバイザリ警告コードを出力することがあります。これらのコードは、`CRUD_ACTION_NAME`、`FORBIDDEN_TYPE_NAME`、`MICRO_ACTION`、`MIN_N_UNSET`、`STORED_DERIVABLE`、および `UNSCOPED_SENSITIVE` です。エラーには `ONTOLOGY_INVALID` というコードが使われます。

### オプション

| フラグ | 説明 |
|---|---|
| `--json` | 検出事項をテキストではなく JSON 配列として出力します。 |

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
| `ontary validate` | `0` | 検出事項がないか、または `error` 重要度の検出事項がない場合。 |
| `ontary validate` | `1` | 1つ以上の `error` 重要度の検出事項がある場合。 |
| `ontary validate` | `2` | ターゲットのロードまたは診断ができない場合。ロード失敗時は、標準エラー出力に `ontary: <reason>` が出力されます。 |
| `ontary serve` | `0` | サーバーが正常に停止した場合。 |
| `ontary serve` | `2` | ターゲットもしくはストアをロードできないか、または `mcp` エクストラがない場合。 |
| `ontary version` | `0` | バージョンの出力に成功した場合。 |
| `ontary-mcp` | `1` | インポートせずにスクリプトが直接実行された場合。案内メッセージを表示して終了します。 |
