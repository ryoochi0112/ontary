# オントロジーのテスト

[English](testing.md) · **日本語** · [← README](../README.md)

*ハウツーガイド* — 決定論的なシナリオでオントロジーをテストする手順を示すページで、ヘルパーの一覧は [API リファレンス](api-reference.ja.md)にあります。

## 決定論的テストが必要な理由

テストは実時間やランダムなIDに依存すべきではありません。`ontary.testing`モジュールは、given/when/then のシナリオビルダーと、その土台となる低レベルのヘルパーを提供します。エンジンの内部機能のインポートやカスタムの`pytest`フィクスチャは不要で、失敗は`AssertionError`になります。

## 初めてのテスト

はじめに、オントロジーのモジュールを設定します。オントロジーレジストリ、オブジェクト、アクション、検証ロジックを宣言します。このモジュールレベルのセットアップは、すべてのテストシナリオの基礎となります。

```python
from datetime import datetime, timezone
from ontary import (
    ActionContext, ActionError, ActionParams, DirectProperty, Event,
    Ontology, OntologyObject, SelfScope, Source, prop, target,
)
from ontary.testing import (
    FixedClock, SequentialIds, consumer, make_store, raises_code, scenario,
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

@ontology.event(description="A ticket was escalated.")
class TicketEscalated(Event):
    reason: str

@ontology.action(
    EscalateTicket, target=Ticket, roles=["Agent"],
    display_name="Escalate ticket", description="Mark a ticket urgent.",
    api_name="EscalateTicket", emits=[TicketEscalated],
)
def escalate(ctx: ActionContext, params: EscalateTicket) -> dict[str, str]:
    ticket = ctx.get(Ticket, params.ticket_id)
    if ticket is None:
        raise ActionError("ticket does not exist", code="PRECONDITION_FAILED")
    ticket.escalated = True
    ctx.save(ticket)
    ctx.emit(TicketEscalated(reason="urgent"))
    return {"ticket_id": params.ticket_id}

ontology.validate()
```

ここからシナリオビルダーで最初のテストを書きます。シナリオは、検証したい業務ルールをそのまま読める形にします。つまり、開始状態を与え（given）、アクターがアクションを実行し（when）、結果が成り立つ（then）ことを確かめます。すべてのシナリオは `then` で終わります。最後のステップが `when` のままだと何も検証しないため、`then`、`then_result`、`then_error`、`then_absent`、`then_link`、`then_no_link`、`then_event` のいずれかで締めてください。

`scenario(ontology)` は、何かを準備する前に、新しい `InMemoryStore`、`2026-01-01T00:00:00Z` の `FixedClock`、`SequentialIds("id")` をバインドします。`given` は型付きオブジェクトを受け取り、`when` には `by=` のコンシューマーが必須です。

```python
def test_escalate_success():
    agent = consumer(role="Agent", scope_level="queue", scope_id="queue-a")
    (scenario(ontology)
        .given(
            Queue(id="queue-a", name="Billing"),
            Ticket(id="t-1", subject="Invoice mismatch", queue_id="queue-a"),
        )
        .when(EscalateTicket(ticket_id="t-1"), by=agent)
        .then(Ticket, "t-1", escalated=True))
```

`then(cls, pk, **fields)` は、指定したプロパティだけを、保存されている現在の行と比較します。特定のコンシューマー向けのマスキングは行われません。マスキングを検証したいときは、そのコンシューマーとして `client.get` で読んでください。

検証に失敗すると `AssertionError` になり、オブジェクト、ステップ、値が異なる各プロパティが示されます。

```text
then: Ticket 't-1' does not match after step 1 (EscalateTicket by Agent):
  escalated: expected False, got True
```

`then_result(expected)` はアクションの戻り値を検証します。`then_absent(cls, pk)` は現在の行が存在しないこと、`then_link(handle, from_, to)` と `then_no_link(handle, from_, to)` はリンクの有無を検証し、`given_link(handle, from_, to)` はリンクを準備します。1つのシナリオに複数の `when` を置くことができ、`then` は最後のステップに適用されます。検証していない失敗ステップがあると、次の `when` でシナリオが止まります。

## イベントの検証

`then_event(event, *, about=None)` は、直前の `when` ステップが等しいイベントを送出したことを確認します。イベントの型が同じで、ペイロードが等しい必要があります。`about` をオブジェクトか id 文字列で渡すと、イベントの対象（subject）も一致しなければなりません。`then` と同じく監査エントリをマスクせずに読むため、コンシューマーに見えるものの検証にはなりません。それを検証したいときは、そのコンシューマーとして `client.events` で読んでください。

```python
def test_escalate_emits_event():
    agent = consumer(role="Agent", scope_level="queue", scope_id="queue-a")
    (scenario(ontology)
        .given(
            Queue(id="queue-a", name="Billing"),
            Ticket(id="t-1", subject="Invoice mismatch", queue_id="queue-a"),
        )
        .when(EscalateTicket(ticket_id="t-1"), by=agent)
        .then_event(TicketEscalated(reason="urgent"), about="t-1"))
```

検証に失敗すると `AssertionError` になり、実際に送出されたイベントが列挙されます。失敗したステップは、そのエラーコードとともに報告されます。

## エラーコードの検証

異常系は、メッセージではなく安定したエラーコードで検証します。`then_error(code)` は、直前のアクションがそのコードで失敗したことを確認します。さらに、失敗したステップが何も変更していないことも証明します。現在のオブジェクトとリンクが、ステップ前の状態と一致している必要があります。この確認を自分で書く必要はありません。

```python
def test_escalate_precondition_failed():
    agent = consumer(role="Agent", scope_level="queue", scope_id="queue-a")
    (scenario(ontology)
        .given(Queue(id="queue-a", name="Billing"))
        .when(EscalateTicket(ticket_id="missing"), by=agent)
        .then_error("PRECONDITION_FAILED"))

def test_viewer_cannot_escalate():
    viewer = consumer(role="Viewer", scope_level="queue", scope_id="queue-a")
    (scenario(ontology)
        .given(
            Queue(id="queue-a", name="Billing"),
            Ticket(id="t-1", subject="Invoice mismatch", queue_id="queue-a"),
        )
        .when(EscalateTicket(ticket_id="t-1"), by=viewer)
        .then_error("PERMISSION_DENIED"))
```

## 環境の上書き

既定値を置き換えるには、`scenario()` に `store=`、`clock=`、`id_factory=`、`capabilities=` を渡します。たとえば、同じシナリオを SQLite や Postgres で動かせます。上書きするときは、空のストアを渡してください。別のクロックで書き込まれたデータを持つストアでは、`CLOCK_REGRESSION` や `CLOCK_CONFLICT` が発生することがあります。シナリオは準備の前にクロックをバインドするため、`given` の順序が原因でこのエラーになることはありません。

## 土台となるヘルパー

シナリオは小さなヘルパーの上に作られており、それらを直接使うこともできます。生のストア書き込みや一括取り込みなど、シナリオで表せない手順が必要なときに使ってください。

`make_store(ontology)` は空の `InMemoryStore` を新しく作ります。`consumer(...)` はテスト向けの既定値で有効な `Consumer` を組み立てます。`raises_code(code)` は、ブロックがそのコードのエラーを発生させることを検証するコンテキストマネージャーで、メッセージは無視します。`FixedClock(start)` はタイムゾーン付きの同じ日時を毎回返し、`SequentialIds(prefix)` は `prefix-1`、`prefix-2`、…を返します。どちらも `ontology.bind(...)` に渡せます。

同じエスカレーションを低レベルのヘルパーで書くと、次のようになります。

```python
def test_escalate_with_low_level_helpers():
    store = make_store(ontology)
    source = Source(source_system="demo")
    store.insert("Queue", {"id": "queue-a", "name": "Billing"}, source)
    ticket_id = store.insert(
        "Ticket", {"subject": "Invoice mismatch", "queue_id": "queue-a"}, source
    )
    agent = consumer(role="Agent", scope_level="queue", scope_id="queue-a")
    client = ontology.bind(store).for_consumer(agent)
    client.execute(EscalateTicket(ticket_id=ticket_id))
    assert client.get(Ticket, ticket_id).escalated is True

    with raises_code("PRECONDITION_FAILED"):
        client.execute(EscalateTicket(ticket_id="missing"))
```

## MCP サーバーをプロセス内でテストする

ソケットを開かずに、クライアントと同じ呼び方でサーバーをテストします。
ASGI アプリを `ASGITransport` 経由の `httpx2.AsyncClient` で呼び出します
（`httpx2` は `[mcp]` extra に含まれます）。通常のテスト関数からは `asyncio.run` で呼びます。

ここでは Starlette の同期版 `TestClient` を使わないでください。`TestClient` はアプリを
別スレッドで実行しますが、SQLite の `ObjectStore` の接続はスレッドをまたげません。
HTTP 呼び出しは 200 を返しますが、ツールの結果はすべて `INTERNAL_ERROR` になります。
`ASGITransport` はアプリをテストと同じスレッドで動かすため、どのストアでも同じテストが動きます。

```python
import asyncio

import httpx2
from ontary import MCPServer, ObjectStore, build_mcp_server

def test_get_ticket_over_mcp_in_process():
    store = ObjectStore(ontology.registry)  # SQLite, in memory
    source = Source(source_system="demo")
    store.insert("Queue", {"id": "queue-a", "name": "Billing"}, source)
    store.insert(
        "Ticket", {"subject": "Invoice mismatch", "queue_id": "queue-a"}, source
    )
    agent = consumer(role="Agent", scope_level="queue", scope_id="queue-a")
    server: MCPServer = build_mcp_server(ontology, store, agent)
    app = server.streamable_http_app(stateless_http=True, json_response=True)

    async def call_tool(name, arguments):
        async with server.session_manager.run():
            # Keep the port: the default Host check refuses a portless host.
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                base_url="http://localhost:8000",
            ) as client:
                response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                    headers={"Accept": "application/json, text/event-stream"},
                )
        return response.json()["result"]["structuredContent"]

    result = asyncio.run(call_tool("query_objects", {"obj_type": "Ticket"}))
    assert [row["payload"]["subject"] for row in result["result"]] == ["Invoice mismatch"]
```

`stateless_http=True` にすると、`initialize` のハンドシェイクなしで `tools/call`
リクエストを 1 件だけ送れます。呼び出し元を認証するサーバーについては
[MCP での提供](mcp-serving.md)を参照してください。

## 固定日時と固定ID

アクションは、実行中にIDやタイムスタンプを生成します。`bind`に固定クロックとIDファクトリを渡すと、どちらも決定論的になります。クロックはストアに設定されるため、`bulk_upsert`、`bulk_link`、ストアへの直接書き込みにも適用されます。

```python
def test_fixed_time_and_ids():
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    id_factory = SequentialIds("t")
    assert clock() == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert id_factory() == "t-1"

    store = make_store(ontology)
    runtime = ontology.bind(store, clock=clock, id_factory=id_factory)
    assert runtime is not None
```

シードの前にバインドしてください。別のクロックで書き込んだデータがあると、後の更新や retire で`CLOCK_REGRESSION`が発生することがあります。次の例は、バインド後に`bulk_upsert`でシードし、アクションを実行します。そして`valid_from`と監査の`ts`が固定時刻と等しいことを確認します。

```python
def test_whole_test_is_deterministic():
    from ontary.ingest import bulk_upsert

    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = make_store(ontology)
    runtime = ontology.bind(store, clock=FixedClock(t), id_factory=SequentialIds("t"))
    source = Source(source_system="demo")
    report = bulk_upsert(
        store, ontology.registry, "Ticket",
        [{"id": "t-0", "subject": "Invoice mismatch", "queue_id": "queue-a"}],
        source,
    )
    assert report.errors == []

    agent = consumer(role="Agent", scope_level="queue", scope_id="queue-a")
    runtime.for_consumer(agent).execute(EscalateTicket(ticket_id="t-0"))

    stored = store.read_current("Ticket", "t-0")
    assert stored is not None
    assert stored.lineage.valid_from == t.isoformat(timespec="microseconds")
    assert store.audit_entries()[-1].ts == t
```

## テストとしての診断機能

オントロジーの診断機能は、アーキテクチャや設計上の問題を特定するのに役立ちます。これらの診断チェックは、テストスイートに組み込めます。このテストは`error`の検出時のみ失敗し、注意喚起の警告（warning）は通過します。

```python
def test_diagnostics():
    errors = [f for f in ontology.diagnose() if f.severity == "error"]
    assert errors == []
```

## 関連ページ

オントロジーアプリケーションのセットアップと実行に関する詳細はこちらを参照してください。開発中のスキーマ検証については、CLIツールの使用方法を確認してください。また、実稼働する具体例を確認できます。

- [はじめに](getting-started.md)
- [APIリファレンス](api-reference.md)
- [コマンドラインインターフェース](cli.md)
- [チケットの例](../examples/tickets/README.md)
