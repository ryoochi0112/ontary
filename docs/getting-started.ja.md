# はじめに

[English](getting-started.md) · **日本語** · [← README](../README.md)

このチュートリアルでは、実際に動作する最初のオントロジーを10分で構築します。オブジェクト、アクション、関数を定義します。

## インストール

Model Context Protocol（MCP）をサポートするパッケージをインストールします。Python 3.12以降が必要です。

```bash
pip install "ontary[mcp]"
```

コアライブラリは `pydantic` のみに依存します。必要に応じて、`postgres` エクストラが `PostgresStore` を追加します。`ontary` は1.0未満であるため、常に正確なバージョンを固定してください。マイナーリリースで破壊的変更が導入される可能性があります。

## オブジェクトタイプの宣言

ファイル `app.py` を作成します。最初に、オントロジーとオブジェクトタイプを宣言します。

```python
from ontary import DirectProperty, Ontology, OntologyObject, SelfScope, prop

ontology = Ontology(name="tickets", scope_levels=["queue"])

@ontology.object(layer="L0", scope=[SelfScope(level="queue")])
class Queue(OntologyObject):
    id: str = prop(primary_key=True)
    name: str

@ontology.object(
    layer="L0", owned={"escalated": False},
    scope=[DirectProperty(level="queue", property_name="queue_id")],
)
class Ticket(OntologyObject):
    id: str = prop(primary_key=True)
    subject: str
    queue_id: str = prop(scope_level="queue")
    escalated: bool | None = prop(default=False)
```

`Ontology` はタイプと検証を管理します。`L0` レイヤーのオブジェクトは、スコープルールを使用してアクセスを保護します。

## アクションの宣言

アクションはビジネスの動詞を表します。一般的なCRUDのセッターではなく、状態遷移を捉えます。

```python
from ontary import ActionContext, ActionError, ActionParams, target

class EscalateTicket(ActionParams):
    ticket_id: str = target(Ticket)

@ontology.action(
    EscalateTicket, target=Ticket, roles=["Agent"],
    display_name="Escalate ticket", description="Mark a ticket urgent.",
    api_name="EscalateTicket",
)
def escalate(ctx: ActionContext, params: EscalateTicket) -> dict[str, str]:
    if ctx.read_current("Ticket", params.ticket_id) is None:
        raise ActionError("ticket does not exist", code="PRECONDITION_FAILED")
    ctx.update("Ticket", params.ticket_id, {"escalated": True})
    return {"ticket_id": params.ticket_id}
```

このアクションは `Ticket` オブジェクトを対象とします。存在を確認し、ステータスを安全に更新します。

## 派生値関数の追加

関数は状態を変更せずに値を計算します。データベースへの直接の書き込みアクセス権はありません。

```python
from typing import Any
from ontary import BoundQuery

@ontology.function(
    description="Total tickets in a queue.",
    input_description="A queue_id.",
    output_description="Count of tickets.",
    api_name="ticketCount",
)
def ticket_count(query: BoundQuery, params: dict[str, Any]) -> int:
    return query.count("Ticket", where={"queue_id": params["queue_id"]})
```

`validate` を呼び出す前に、すべての関数を宣言する必要があります。この関数はキュー内のチケット数をカウントします。

## 検証と診断

`validate` メソッドはオントロジー定義を凍結します。これ以降にオブジェクトやアクションを宣言しようとすると失敗します。

```python
ontology.validate()

for f in ontology.diagnose():
    print(f.severity, f.code, f.location, f.fix_hint)
```

診断は推奨チェックを実行します。`STORED_DERIVABLE` や `CRUD_ACTION_NAME` などの診断ルールは警告を返します。これらの警告で実行が停止することはありません。

## ストアとコンシューマーのバインド

インメモリのストアを作成し、初期データを投入します。セキュリティポリシーを適用するコンシューマーを定義します。

```python
from ontary import Consumer, ObjectStore, Source

store = ObjectStore(ontology.registry)
source = Source(source_system="demo")

store.insert("Queue", {"id": "queue-a", "name": "Billing"}, source)
ticket_id = store.insert(
    "Ticket", {"subject": "Invoice mismatch", "queue_id": "queue-a"}, source
)

agent = Consumer(
    actor_id="agent-1", role="Agent", scope_level="queue",
    scope_id="queue-a", kind="human"
)
client = ontology.bind(store).for_consumer(agent)
```

データをディスクに保存するには `ObjectStore(registry, "tickets.db")` を使用します。`OntologyClient` が安全な読み取りを処理します。

## アクションの実行と値の読み戻し

クライアントを使用して、アクションの実行とプロパティの照会を行います。

```python
client.execute(EscalateTicket(ticket_id=ticket_id))
assert client.get(Ticket, ticket_id).escalated is True
assert client.call_function("ticketCount", {"queue_id": "queue-a"}) == 1
```

クライアントはスコープ、機密性、行レベルの可視性ルールを適用します。また、`list`、`traverse`、`call_function` も使用できます。

## AIエージェントへの提供

Model Context Protocol を介してオントロジーを公開できます。デフォルトのトランスポートとして stdio を使用します。

```python
from ontary import build_mcp_server

server = build_mcp_server(ontology, store, agent)
```

ポート8000で開発用サーバーを実行するには、CLIを使用します。これは `127.0.0.1` にのみバインドされます。

```bash
ontary serve app:ontology --dev --store ./dev.sqlite --port 8000
```

CLIツールを使用してファイルを検証します。終了コード0は、成功または警告のみであることを示します。

```bash
ontary validate app:ontology
```

## プログラム全体

`app.py` の実行可能な完全なコードは以下の通りです。

```python
from typing import Any
from ontary import (
    ActionContext, ActionError, ActionParams, BoundQuery, Consumer,
    DirectProperty, ObjectStore, Ontology, OntologyObject, SelfScope,
    Source, build_mcp_server, prop, target,
)

ontology = Ontology(name="tickets", scope_levels=["queue"])

@ontology.object(layer="L0", scope=[SelfScope(level="queue")])
class Queue(OntologyObject):
    id: str = prop(primary_key=True)
    name: str

@ontology.object(
    layer="L0", owned={"escalated": False},
    scope=[DirectProperty(level="queue", property_name="queue_id")],
)
class Ticket(OntologyObject):
    id: str = prop(primary_key=True)
    subject: str
    queue_id: str = prop(scope_level="queue")
    escalated: bool | None = prop(default=False)

class EscalateTicket(ActionParams):
    ticket_id: str = target(Ticket)

@ontology.action(
    EscalateTicket, target=Ticket, roles=["Agent"],
    display_name="Escalate ticket", description="Mark a ticket urgent.",
    api_name="EscalateTicket",
)
def escalate(ctx: ActionContext, params: EscalateTicket) -> dict[str, str]:
    if ctx.read_current("Ticket", params.ticket_id) is None:
        raise ActionError("ticket does not exist", code="PRECONDITION_FAILED")
    ctx.update("Ticket", params.ticket_id, {"escalated": True})
    return {"ticket_id": params.ticket_id}

@ontology.function(
    description="Total tickets in a queue.",
    input_description="A queue_id.",
    output_description="Count of tickets.",
    api_name="ticketCount",
)
def ticket_count(query: BoundQuery, params: dict[str, Any]) -> int:
    return query.count("Ticket", where={"queue_id": params["queue_id"]})

ontology.validate()

for f in ontology.diagnose():
    print(f.severity, f.code, f.location, f.fix_hint)

store = ObjectStore(ontology.registry)
source = Source(source_system="demo")

store.insert("Queue", {"id": "queue-a", "name": "Billing"}, source)
ticket_id = store.insert(
    "Ticket", {"subject": "Invoice mismatch", "queue_id": "queue-a"}, source
)

agent = Consumer(
    actor_id="agent-1", role="Agent", scope_level="queue",
    scope_id="queue-a", kind="human"
)

client = ontology.bind(store).for_consumer(agent)
client.execute(EscalateTicket(ticket_id=ticket_id))

assert client.get(Ticket, ticket_id).escalated is True
assert client.call_function("ticketCount", {"queue_id": "queue-a"}) == 1

server = build_mcp_server(ontology, store, agent)
```

## 次のステップ

詳細なアーキテクチャ概念やセキュリティルールは、以下のトピックを参照してください。

* 設計ルールについては、[Ontology Design](ontology-design.md) を参照してください。事実を一度だけ保存し、`ScopePolicy`、`Sensitivity`、min-N でセキュリティを宣言します。
* [API Reference](api-reference.md) で、すべてのクラスとタイプを確認します。
* ストレージのオプションについては、[Storage](storage.md) を参照してください。
* エージェント統合の詳細については、[MCP Serving](mcp-serving.md) を参照してください。
* コマンドラインツールについては、[CLI Reference](cli.md) を参照してください。
* テストのガイドラインについては、[Testing](testing.md) を参照してください。
* 具体的な動作例については、[Tickets Example](../examples/tickets/README.md) を参照してください。
