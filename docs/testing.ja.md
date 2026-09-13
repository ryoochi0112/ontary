# オントロジーのテスト

[English](testing.md) · **日本語** · [← README](../README.md)

## 決定論的テストが必要な理由

テストは実時間やランダムなIDに依存すべきではありません。`ontary.testing`モジュールは、決定論的なヘルパーを提供します。これらは公開された`ontary` APIのみで構成されています。実行にエンジンの内部機能やカスタムの`pytest`フィクスチャは不要です。

## 各種ヘルパー

このモジュールは、テスト環境の分離と制御に必要な関数とクラスを提供します。インメモリ・ストレージの新規生成、ユーザーやクロックのモック化、ID生成の制御ができます。これらの機能により、異なる環境でもアサーションを安定して実行できます。

`make_store`ヘルパーは、レジストリ用の空のインメモリ・ストアを新規作成します。`consumer`ヘルパーは、一般的な権限とスコープレベルを持つモックユーザーを生成します。`raises_code`は、コードブロックが想定通りのエラーコードを返すことを検証します。

`FixedClock`は、呼び出しのたびに同じタイムゾーン付き日時を返します。`SequentialIds`は、任意のプレフィックスを用いて予測可能なIDを生成します。どちらのヘルパーも、オントロジー・クライアントのバインド処理に直接適用できます。

## 初めてのテスト

はじめに、オントロジーのモジュールを設定します。オントロジーレジストリ、オブジェクト、アクション、検証ロジックを宣言します。このモジュールレベルのセットアップは、すべてのテストシナリオの基礎となります。

```python
from datetime import datetime, timezone
from ontary import (
    ActionContext, ActionError, ActionParams, DirectProperty,
    Ontology, OntologyObject, SelfScope, Source, prop, target,
)
from ontary.testing import (
    FixedClock, SequentialIds, consumer, make_store, raises_code,
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

ontology.validate()
```

エスカレーションの正常系を検証するため、最初のテスト関数を記述します。オントロジーを使用して、空のインメモリ・ストアを初期化します。テストデータを挿入したあと、定義したアクションを実行します。

```python
def test_escalate_success():
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
```

## エラーコードの検証

異常系のテストでは、特定のエラーコードを検証する必要があります。`raises_code`ヘルパーは、コードブロックが期待したエラーを発生させるか検証します。これにより、エラーメッセージの文言変更に影響されない、堅牢なテストを作成できます。

```python
def test_escalate_errors():
    store = make_store(ontology)
    source = Source(source_system="demo")
    ticket_id = store.insert(
        "Ticket", {"subject": "Invoice mismatch", "queue_id": "queue-a"}, source
    )

    agent = consumer(role="Agent", scope_level="queue", scope_id="queue-a")
    client = ontology.bind(store).for_consumer(agent)
    with raises_code("PRECONDITION_FAILED"):
        client.execute(EscalateTicket(ticket_id="missing"))

    viewer = consumer(role="Viewer", scope_level="queue", scope_id="queue-a")
    viewer_client = ontology.bind(store).for_consumer(viewer)
    with raises_code("PERMISSION_DENIED"):
        viewer_client.execute(EscalateTicket(ticket_id=ticket_id))

    assert viewer_client.get(Ticket, ticket_id).escalated is False
```

## 固定日時と固定ID

アクションの実行中に、IDやタイムスタンプが生成されることがよくあります。テストの決定論的な実行を保証するために、これらの値をモック化できます。`bind`呼び出しに固定クロックとIDファクトリを渡すと、動的な既定の動作を上書きできます。

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
