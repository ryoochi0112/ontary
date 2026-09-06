# Ontology design

[English](ontology-design.md) · **日本語**

`Ontology` と `OntologyObject` でオントロジーを宣言する前、あるいは下位の
`ObjectTypeDef`、`PropertyDef`、`LinkTypeDef`、`ActionTypeDef`、`FunctionDef`
といった記述子を生成する前に、オントロジーが何を意味すべきかを決める人と
コーディングエージェント向けのガイドです。第二の API リファレンスではなく
設計ガイドとして使い、境界、所有権、関係、振る舞い、名前、セキュリティを
選ぶためのものです。

Palantir Foundry の
[Best practices](https://www.palantir.com/docs/foundry/ontology/ontology-best-practices/)、
[Structural guidance](https://www.palantir.com/docs/foundry/ontology/ontology-structural-guidance/)、
[Anti-patterns](https://www.palantir.com/docs/foundry/ontology/ontology-anti-patterns/)
を ontary 向けに独自に蒸留したものです。Foundry の Workshop、Object Views、MDO は
アプリケーションまたはプラットフォームの概念であり、SDK の範囲外です。ontary には
対応するオーサリング構文はありません。

有用なオントロジーは、テーブルに別名を付ける以上のことをします。ビジネスオブジェクトに
安定した同一性を与え、関係を明示し、統制されたビジネス操作を公開し、ガード付き
読み取りで答えを導出し、各コンシューマーがどの事実を見られるかを宣言します。ソース
ベンダー、現在の組織図、ユーザーインターフェースが変わっても、結果はなお意味を
保つべきです。

## Core principles

### Domain-driven design

ドメインの言語と意思決定から始め、エクスポートの形から始めないでください。
`ObjectTypeDef` は安定した同一性を持つビジネス概念を表し、`PropertyDef` はその概念に
関する事実を表し、`LinkTypeDef` は関係に名前を付け、`ActionTypeDef` は許可された
ビジネス上の遷移を表し、`FunctionDef` は導出された問いへの答えを返します。

実際のワークフローから外側へ広げます。人々が区別する名詞、実行を許可されている
動詞、それらの動詞が守る不変条件、ユーザーが繰り返し計算する問いを特定します。
[チケットの例](../examples/tickets/ontology.py) では、Org、Queue、Ticket、Comment は
同一性とライフサイクルがそれぞれ異なるため、別々の概念として表します。
チケットのエスカレーション処理は、ストレージの編集ではなく、ドメイン上の結果を表す名前です。

物理的な統合はそのモデルの背後に置きます。ソース列がプロパティの根拠になり得ますが、
オブジェクトの境界や公開名を支配してはいけません。

*Source: Palantir, "Ontology design: Best practices".*

### Don't repeat yourself (rule of three)

権威ある事実は一か所に一度だけ保存し、リンクで到達するか `FunctionDef` で導出します。
意味がまだ固まっていない間は、似た形を二つ別々に保てます。三回目の繰り返しで、
共有する考えが本当のオブジェクトなのか、共有の命名規約なのか、単なる実装コードの
共通部分なのかを決めます。それより早く抽象化すると、偶然の類似がオントロジーに
固定されがちです。

DRY が当てはまるのは綴りだけでなく意味にも及びます。人物の地域を関連レコードすべてに
コピーすると、プロパティの型が同じでも同じ問いへの答えが複数できます。逆に、status と
名付けられた二つのプロパティは、異なるライフサイクルを記述しているなら統合する必要は
ありません。テキストの類似ではなく、ドメイン上の同一性と所有権で判断してください。

*Source: Palantir, "Ontology design: Best practices".*

### Open for extension, closed for modification

既存の型の意味を変えずに新しいワークフローを受け入れられる設計を選びます。新しい
`ObjectTypeDef`、`LinkTypeDef`、`ActionTypeDef`、`FunctionDef` の宣言は、安定した
コアを拡張できます。既存の API 名、プロパティの意味、リンクの方向、Action の結果は、
人間向けアプリケーションとエージェントにとっての契約です。

既存のオブジェクト形が本当に進化するときは、同じ型を意図的に変更し、その履歴は
ストアの行履歴に任せます。投機的な拡張ポイントを先に作らず、
新しいユースケースに合わせて古いプロパティの意味を黙って書き換えないでください。

*Source: Palantir, "Ontology design: Best practices".*

### Composition over deep hierarchies

深い継承ツリーを発明するより、`LinkTypeDef` 関係でドメイン概念を合成します。リンクは
両端の同一性、方向、`Cardinality` をレビュー可能にします。`owned` による権限宣言も
リンク自身に載せられます。

継承は Python クラスの実装技法であり、ドメイン関係の代わりにはなりません。複数の
リンクに参加できる小さなオブジェクト型を選びます。複数の型が共通の導出問いを必要と
するとき、`FunctionDef` がオントロジー上の親を共有していると主張せずに横断して読めます。

*Source: Palantir, "Ontology design: Best practices".*

## Structural guidance

### Normalization and derived values

各事実はそれを所有する境界に記録します。必要な場所からはコピーせず、そこへリンクします。
とくに入力は一度だけ保存し、スコア、ロールアップ、分類、その他の導出答えは
`FunctionDef` で計算します。Function はガード付き読み取りを使い、書き込みは行いません。
チケット例の [ticket statistics Function](../examples/tickets/ontology.py) は、競合する合計を
永続化するのではなく、Ticket の事実から集計値を導出しています。

意図的な例外は、宣言された時点のスナップショットだけです。ドメインが、ある時点での
スコアや決定を保存しなければならないなら、対象と観測時刻を含むスナップショット
オブジェクトを明示的にモデル化します。通常のキャッシュ、レポート結果、重複した現在値を
スナップショットと呼ばないでください。日常的な履歴にクローンは不要です。保存された
行はすでに `valid_from` と `valid_to` を持ちます。

正規化するのは意味上の事実であり、物理値を盲目的にすべて分割するわけではありません。
オブジェクト自身の状態の一部であるプロパティは、そのオブジェクトに置いたままで構いません。
独立した同一性やライフサイクル、多数の参加者、別の所有権、包含オブジェクト上では
きれいに表現できないセキュリティがあるときに切り出します。

*Source: Palantir, "Ontology design: Structural guidance".*

### Structs

Foundry の struct はプロパティ内にネストしたフィールドをまとめます。**同等物はまだ
ありません。最も近い近似は、同じ `ObjectTypeDef` 上の別プロパティ、あるいはグループが
独自の同一性、ライフサイクル、再利用、セキュリティを持つときのリンク先オブジェクト型
です。** `PropertyDef` にはネストしたプロパティ型の宣言がありません。

親と常に一緒に作成・保護・変更される、小さく分離不能な値のグループには別プロパティを
選びます。グループが繰り返し可能、共有、独立に統制される、Action の対象になるときは、
別のオブジェクト型と `LinkTypeDef` を選びます。記述子を短くするためだけに不透明な
ネストデータを一プロパティに押し込まないでください。そうすると、フィールドが
オントロジー水準の命名とセキュリティレビューから隠れます。

*Source: Palantir, "Ontology design: Structural guidance".*

### Interfaces

Foundry の interface はオブジェクト型間の共通契約を定義します。**同等物はまだありません。
最も近い近似は、共有のプロパティ規約と複数型にまたがる Function です。** ontary には
interface 宣言がなく、二つの `ObjectTypeDef` インスタンスが互換的に置き換え可能だという
約束もありません。

複数の型が同じ概念を公開するとき、該当する `PropertyDef` 宣言に同じ意味と命名を与え、
その規約を文書化し、オントロジー自身のテストで検証します。複数型にまたがる一つの
導出操作がコンシューマーに必要なときは `FunctionDef` を使います。共有の同一性と
ライフサイクルが浮かび上がったら、型が一つの共通オブジェクトへリンクすべきかを
再検討します。

*Source: Palantir, "Ontology design: Structural guidance".*

### Links and object-backed link types

関係の意味が端点、方向、`Cardinality`、統制フラグで捉えられるときは `LinkTypeDef` を
使います。関係にドメインから名前を付け、両端を検証し、ソースではなく Action がリンクを
作るときは `owned` を宣言します。ドメインの例では、リンクによって通常の包含、
同一性を明かす traverse、Action が所有する関係を区別できます。

Foundry の object-backed link type は関係にプロパティを付けます。**同等物はまだありません。
`LinkTypeDef` にはペイロードプロパティがなく、最も近い近似は、各参加者を `LinkTypeDef`
でつなぐ明示的な関係 `ObjectTypeDef` です。** 役割、有効日、順位、出自など、関係自体に
属する事実にはその形を使います。関係オブジェクトは、そのライフサイクルに `ActionTypeDef`
が必要になるときの正しい対象にもなります。

同じ関連を、制約のない外部キープロパティとリンクの両方で独立にエンコードしないでください。
プロパティがスコープや寄与者の解決に必要な場合を除きます。両方が必要なら、一つの
不変条件として扱い、一緒に埋めます。

*Source: Palantir, "Ontology design: Structural guidance".*

### Naming conventions

オブジェクト型には単数のビジネス名詞、リンクには具体的な関係句、Action にはビジネス
動詞、Function には問いかけ型または結果指向の名前を使います。名前は、データベース
スキーマ、ベンダー API、チームの略語、現在の UI を知らないドメイン専門家にも意味が
通るべきです。

公開 API 名は永続的な識別子として扱います。表示テキストと説明は、エージェントが正しい
操作を選べるほど明確にしつつ、一つの名前に複数の意味を載せないでください。基礎となる
意味が一致するときだけ、プロパティ名を揃えます。V2 のような実装接尾辞、テーブル接頭辞、
一時的なプロジェクトラベルは避けてください。

*Source: Palantir, "Ontology design: Structural guidance".*

### Retirement and removal

リタイアはビジネス上の動詞です。`ActionTypeDef` を `OffboardEmployee` や `CancelSubscription` の
ようなビジネス上の結果を表す名前でモデル化し、アクション名に `DeleteEmployee` や
`RemoveEmployee` を決して使わないでください。宣言した Action の handler 内では、エンジンの
`ActionContext.retire` と `ActionContext.unlink` を呼び出して遷移を実行します。アクション名は
ドメインの言葉のままにし、汎用の delete/remove Action を公開しないでください。

`Store.retire_object` は `valid_to` を設定して現在の行を閉じますが、delete ではありません。
系譜、履歴、監査は残ります。リタイアしたオブジェクトは current reads からは除外されますが、
履歴にはなお存在します。`ActionContext.retire` では、そのオブジェクト型についてリンク型が
宣言している側でそのオブジェクトを参照するすべての live link も、同じ transaction で
cascade-close されます。

erasure はオントロジーの概念ではなく、この SDK は erasure の動詞を提供しません。ライフサイクルの
動詞はリタイアです。right-to-erasure 要求（GDPR/APPI）に応じるために `Erase*` や `Delete*` の
Action、Function、client operation、MCP tool を宣言しないでください。保存されたバイト列の破棄は
データベースを運用する側の運用上の判断であり、宣言されたオントロジーの外で決めます。オブジェクトを
リタイアするビジネス Action とは分離して扱ってください。監査はエンジンの `AuditEntry` のままです。
記録のための型や Action を宣言しないでください。

統制された Action は、型全体が `owned=True` を宣言している場合に限りオブジェクトをリタイア
できます。リンクを close できるのは、そのリンク型が `owned=True` を宣言している場合だけです。
`owned` プロパティの map による部分所有では不十分です。ソース由来データは Action でリタイアも
close もできません。試みると authority refusal がコード `UNDECLARED_SOURCE_REMOVAL` で発生します。
リタイアの cascade がソース由来のリンクに達した場合、アクションの transaction 全体がロールバック
されます。部分的な cascade はありません。

### Security design

セキュリティは各アプリケーションが後付けするフィルタではなく、意味モデルの一部です。
オブジェクトとリンクグラフとともに `ScopePolicy` を設計します。その `row_visibility`
ルールは、レコード全体がコンシューマーに存在するかを決め、`min_n` は集計が少なすぎる
数の異なる寄与者を記述するのを防ぎます。集計が失敗すると `VisibilityError`（コード
MIN_N_VIOLATION）、見えないレコードや禁止された機微操作では `VisibilityError`（コード
VISIBILITY_DENIED）が発生します。

`PropertyDef` にフィールドの意味を宣言します。`scope_level` はスコープ解決に参加し、
`sensitivity` は `Sensitivity` ポリシーを保持します。`ai_usable` で AI コンシューマー
からデータを隠し、`human_visible` で人間コンシューマーから隠します。これらは単一の
機密フラグではなく、別々の決定です。制限されたプロパティは省略可能のままにし、
リダクションが値なしを返せるようにします。

各宣言だけでなく組み合わせをテストします。たとえば HR ドメインでは、ポリシーによって
候補者のメールを AI から隠し、人口統計と同一性フィールドを人間から隠し、機密行を除去し、
min-N のために異なる候補者を数えることができます。これが意図した形です。一つの
`ScopePolicy` がすべてのコンシューマーに強制され、
コンシューマー種別ごとに意味的リダクションが行われます。

*Source: Palantir, "Ontology design: Structural guidance".*

## Anti-patterns

### System Silos

**Anti-pattern:** オントロジーを一つのベンダー、コネクタ、ソースシステムの形に合わせ、
そのテーブル名、識別子、癖が公開ドメインモデルになること。

**Why it fails:** ベンダー差し替えがオントロジー移行になり、コンシューマーは統合の詳細を
覚え、同じ現実世界の実体がソース固有の複数オブジェクトとして到着し、安定した同一性が
なくなります。

**In ontary:** オントロジーをベンダー非依存に保ちます。`ObjectTypeDef` と
`LinkTypeDef` の名前は抽出物ではなくドメインから付けます。ソースデータは
`OntologyClient.ingest` 経由で読み込み、各レコードを宣言済みの型へ写します。
抽出物の形をそのまま通してはいけません。系譜を残すには `Source` を使い、`owned`
宣言でソース由来の事実とオントロジー所有の状態を分けます。

*Source: Palantir, "Ontology design: Anti-patterns".*

### The Kitchen Sink

**Anti-pattern:** ソースが出せるからという理由で、利用可能な列とあらゆる関心事を一つの
オブジェクト型に載せること。

**Why it fails:** ほとんどのプロパティが省略可能または曖昧になり、無関係なライフサイクルが
衝突し、セキュリティが粗くなり、小さな一部を使うだけでも巨大な形全体を理解しなければ
なりません。

**In ontary:** `ObjectTypeDef` を一つの同一性とライフサイクルにまとめて保ちます。独立に
統制される概念や繰り返し可能な概念は別オブジェクト型へ移し、`LinkTypeDef` で結びます。
オブジェクトの事実である `PropertyDef` 宣言だけを残し、レポート形のフィールドを足すのではなく
`FunctionDef` でコンシューマー固有の答えを導出します。

*Source: Palantir, "Ontology design: Anti-patterns".*

### Department Silos

**Anti-pattern:** 部門、ワークフロー、アクセスグループごとに、同じビジネス実体の別バージョンを
宣言すること。

**Why it fails:** 同一性と事実がドリフトし、部門横断のリンクに突合ロジックが必要になり、
基礎ドメインが変わっていないのに組織変更がスキーマ変更を強います。

**In ontary:** 一つのドメイン同一性に対して一つの `ObjectTypeDef` を宣言し、部門固有の
プロセスオブジェクトは `LinkTypeDef` で接続します。共有実体の可視性は複製ではなく
`ScopePolicy`、`PropertyDef` の sensitivity、`row_visibility` で統制します。部門固有の
振る舞いは、適切にスコープされた `ActionTypeDef` 宣言に置きます。

*Source: Palantir, "Ontology design: Anti-patterns".*

### The God Object

**Anti-pattern:** 一つの中心オブジェクトが、ドメインのほぼすべてのプロパティ、リンク、
Action、ライフサイクルを所有すること。

**Why it fails:** 無関係な変更が一スキーマで競合し、カーディナリティが暗黙のフィールド集合に
なり、権限が例外に散らばり、一ワークフローの変更がそのオブジェクトのすべてのコンシューマーに
リスクを及ぼします。

**In ontary:** 安定した同一性を持つ概念を、焦点を絞った `ObjectTypeDef` 宣言に分け、
レビュー済みの `Cardinality` を持つ明示的な `LinkTypeDef` 関係で合成します。各
`ActionTypeDef` は、そのライフサイクルを変えるオブジェクトを対象にします。グラフは
接続されたままにしつつ、一つのルートオブジェクトによる所有と接続を混同しないでください。

*Source: Palantir, "Ontology design: Anti-patterns".*

### The Golden Hammer

**Anti-pattern:** ログ、導出メトリクス、ランタイムメタデータ、インフラ機構を含め、あらゆる
関心事を別のオントロジーオブジェクト型として表すこと。

**Why it fails:** グラフが実装の残骸で満ち、コンシューマーはドメイン状態とエンジン状態を
区別できず、重複したプラットフォーム記録が独自の不整合なライフサイクルとセキュリティ規則を
持ちます。

**In ontary:** 関心事に合う構文を選びます。事実には `PropertyDef`、関係には
`LinkTypeDef`、導出答えには `FunctionDef`、統制された遷移には `ActionTypeDef` です。
監査はエンジンの追記専用 `AuditEntry` です。それを鏡写しする監査オブジェクト型を
宣言してはいけません。Workshop、Object Views、MDO はアプリケーション／プラットフォームの
関心事であり SDK のドメイン構文ではないため、オントロジーから外します。

*Source: Palantir, "Ontology design: Anti-patterns".*

### Action Sprawl

**Anti-pattern:** 変更可能なプロパティごとに setter や CRUD Action を生成し、それを
運用オントロジーと呼ぶこと。

**Why it fails:** 呼び出し側が低レベル編集からワークフローを再構成しなければならず、
事前条件と権限が setter 間でドリフトし、部分更新が可能になり、監査記録がビジネス意図ではなく
ストレージ操作を記述します。

**In ontary:** ビジネス動詞で名付け、実際の結果に沿った小さな `ActionTypeDef` 集合を
宣言します。一つの Action が、対象、許可ロール、宣言された capability、
オントロジー所有の書き込みを含む、不変条件を保つ遷移全体を所有すべきです。汎用の create、
update、set-property Action を公開してはいけません。
チケット例の [ticket-escalation Action](../examples/tickets/ontology.py) はチケットを
エスカレーションし、その名前がレビュアーに何が起きたかを伝えます。

*Source: Palantir, "Ontology design: Anti-patterns".*

### The Time Machine

**Anti-pattern:** 履歴を別オブジェクトやオブジェクト型としてモデル化すること。
`Survey2024`、`SurveyResponseV2`、エンティティごとの `*History` クローンなど。

**Why it fails:** すべてのコンシューマーがどの版を問い合わせるべきかを知る必要があり、
リンクがクローンに扇状に広がり、「現在状態」が問い合わせではなく慣習になります。

**In ontary:** オブジェクト型は一つ宣言します。ストアが行履歴（`valid_from`/`valid_to`、
close-old-insert-new）を保持するため、履歴はストレージの関心事であり、モデリング問題では
ありません。時点の値を第一級にする必要があるなら、スナップショット型（例:
`EngagementScoreSnapshot`）として明示的に宣言します。導出可能な事実の複製として
認められるのはこれだけです。履歴は行履歴であり、`V2` 型は使いません。

*Source: Palantir, "Ontology design: Anti-patterns".*

### The Misnomer

**Anti-pattern:** 馴染みはあるが不正確なビジネス語、ソースシステムのラベル、曖昧な
技術名を、オブジェクト、リンク、Action、プロパティに使うこと。

**Why it fails:** コンシューマーごとに同じ宣言への意味づけがずれ、エージェントが説明から
誤った操作を選び、後の作者がモデルを直す代わりに別名と例外で補います。

**In ontary:** 各 `ObjectTypeDef`、`LinkTypeDef`、`ActionTypeDef`、`FunctionDef` の
API 名が、一つの正確なドメイン意味を述べるようにします。境界と不変条件は説明に記録し、
Action にはビジネス動詞を使います。名前が誤っているなら、明示的なスキーマとコンシューマー
移行を計画し、誤解を招く V2 の双子を作ったり、古い名前の意味を黙って変えたりしないで
ください。

*Source: Palantir, "Ontology design: Anti-patterns".*
