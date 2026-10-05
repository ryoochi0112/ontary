# ドメイン駆動設計（DDD）から来た方へ

[English](coming-from-ddd.md) · **日本語**

*解説* — このページは馴染みのある用語を ontary に対応づけます。[設計ガイド](ontology-design.ja.md)では設計上の選択を説明し、コードは[はじめに](getting-started.ja.md)で確認できます。

ontary を使うために、ドメイン駆動設計（DDD）の知識は必要ありません。
すでに DDD を知っている方が、対応する構文と制約を見つけられるように、このページで用語を対応づけます。
宣言された構文ではなく、最も近い対応先を示す用語もあります。

## 用語ごとの対応

| DDD用語 | ontary での構文 | ここでの意味 |
| --- | --- | --- |
| Ubiquitous language（ユビキタス言語） | `OntologyObject`、アクション、Function に付ける業務上の名前 | 業務の中で人々が使う言葉で、物事、関係、操作、回答に名前を付けます。→ [はじめに](getting-started.ja.md) |
| Bounded context（境界づけられたコンテキスト） | 未サポート — 最も近いもの: 一つの操作に対する `Ontology` | オントロジーは一つの操作のモデルを宣言します。境界づけられたコンテキストや、コンテキスト間の関係は宣言しません。→ [オントロジーの宣言](api-reference.ja.md#オントロジーを宣言する) |
| Context map（コンテキストマップ） | 未サポート | リンクは一つのモデル内のオブジェクトを結びます。境界づけられたコンテキスト間の関係は宣言しません。→ [リンクとオブジェクト型で表すリンク](ontology-design.ja.md#links-and-object-backed-link-types) |
| Entity（エンティティ） | 主キーを持つ `OntologyObject` | 宣言されたオブジェクトは主キーによって安定した同一性を持ち、ストアはその行の履歴を保持します。→ [はじめに](getting-started.ja.md) |
| Value object（値オブジェクト） | struct 型の `prop` を持つ Struct プロパティ。列挙値には Choice プロパティ | Struct は独自の同一性を持たない値を保持します。通貨を伴う金額などです。→ [Struct プロパティとパラメーター](api-reference.ja.md#struct-プロパティとパラメーター) |
| Aggregate（集約） | 宣言なし — 最も近いもの: 状態遷移ごとに一つのアクションが一つのトランザクションで処理し、単一オブジェクトの事実をルールで検査します | ontary には集約境界も集約ルートもありません。アクションは一つのトランザクション内で状態遷移全体を所有します。ルールは一つのオブジェクトから決まる事実を検査します。複数オブジェクトにまたがる検査はアクション内に置きます。→ [ルールと状態遷移](ontology-design.ja.md#rules-and-status-transitions) |
| Aggregate root（集約ルート） | 宣言なし — 最も近いもの: アクションの対象 | アクションの対象は操作の対象を特定しますが、集約ルートや境界は宣言しません。→ [Action](api-reference.ja.md#action) |
| Invariant（不変条件） | `Ontology.rule` と `prop(transitions=...)` | ルールは一つのオブジェクトから決まる事実を検査し、遷移グラフは選択肢プロパティで許される移動を制限します。→ [ルールと状態遷移](ontology-design.ja.md#rules-and-status-transitions) |
| Domain event（ドメインイベント） | `Event`、`emits=`、`ctx.emit` | アクションはトランザクション内に業務上の事実を保存します。購読者への配信はまだサポートされておらず、Later の項目です。→ [イベント](ontology-design.ja.md#events) |
| Repository（リポジトリ） | 利用者向けの構文ではありません — 最も近いもの: エンジンのストアと `OntologyClient` による読み取り | ストアはオブジェクトを永続化し、クライアントの読み取りは利用者のセキュリティポリシーを適用します。→ [はじめに](getting-started.ja.md) |
| Factory（ファクトリ） | 未サポート — 最も近いもの: アクション内での `ActionContext` による作成 | 作成が業務操作である場合は、アクションのコンテキストを通じてオブジェクトを作成します。→ [`ActionContext`](api-reference.ja.md#actioncontext) |
| Domain service（ドメインサービス） | 導出した回答には Function、状態変更にはアクション | Function はガード付き読み取りで回答を導出し、アクションは状態遷移を所有します。→ [Function](api-reference.ja.md#function) |
| Application service（アプリケーションサービス） | 独立した構文ではありません — 最も近いもの: アクションと Function を呼び出す `OntologyClient` | クライアントは利用者向けの統制された操作を呼び出し、エンジンはセキュリティと監査を適用します。→ [はじめに](getting-started.ja.md) |
| Specification（仕様） | 宣言された構文ではありません — 最も近いもの: `where` フィルターまたは Function | フィルターは一致する行を選び、Function はガード付き読み取りから判断を導出できます。→ [Function](api-reference.ja.md#function) |
| Anti-corruption layer（腐敗防止層） | 未サポート — 最も近いもの: `OntologyClient` に取り込む前のアプリケーション側の対応づけ | 取り込み前にソースレコードを宣言済み型へ対応づけます。ontary には統合時の変換レイヤーを宣言する構文はありません。→ [バルク取り込み](api-reference.ja.md#バルク取り込み) |

## 未サポート

表に宣言されていないことは、追加予定であることを意味しません。
ロードマップには、次の制約と決定が記録されています。

- **Later** — 購読者へのイベント配信。イベントは現在、保存された事実のままです。→ [Roadmap](roadmap.md#later--pulled-by-real-use)
- **Later** — 時点を指定した履歴読み取り。ストアは行の履歴を保持しますが、クライアントの読み取りが返すのは現在のオブジェクトです。→ [Roadmap](roadmap.md#later--pulled-by-real-use)
- **Later** — インターフェースとポリモーフィックなオブジェクト型。プロパティの規約を共有しても、置き換え可能な型は宣言されません。→ [Roadmap](roadmap.md#later--pulled-by-real-use)
- **Later** — 複数の操作を一つのモデルに合成すること。宣言されたコンテキストマップはありません。→ [Roadmap](roadmap.md#later--pulled-by-real-use)
- **Decided against** — プロパティを持つリンク。関係に事実がある場合は、各参加者にリンクするオブジェクト型を使います。→ [Roadmap](roadmap.md#decided-against)

## 関連ページ

- [はじめに](getting-started.ja.md)では、小さなオントロジーを作成します。
- [オントロジー設計](ontology-design.ja.md)では、所有権、関係、ルール、イベント、セキュリティを説明します。
- [API リファレンス](api-reference.ja.md)では、宣言と実行時の動作を説明します。
- [オントロジーのテスト](testing.ja.md)では、状態変更と拒否を検証します。
- [Roadmap](roadmap.md)には、今後の作業と決定を記録しています。
