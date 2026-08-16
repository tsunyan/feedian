# LLM実行バックエンド

ステータス: 確定

> この文書は、先に作成された`docs/specs/llm-backends.ja.md`と、その後このチャットで行われた
> Claude CodeおよびCodexのレビューを、2026-08-16に事後的にADR形式へ再構成したものである。
> 以下のレビューはリアルタイムで追記された原文ではなく、会話に残った内容をCodexが要約して
> 採否と理由を記録した。元の文書は履歴資料として変更せず残す。

## 最終案

### 結論

FeedianのLLM実行層は、`backend`、`model`、`auth_mode`、`billing_mode`を分離して抽象化する。初期backendは次の4種とする。

| backend ID | 状態 | 用途 |
|---|---|---|
| `openai-responses` | stable、既定 | OpenAI Responses API |
| `manus-api` | stable | Manus API |
| `codex-local` | experimental、opt-in | ローカルCodex CLIまたはApp Server |
| `claude-code-local` | experimental、opt-in | 将来のClaude Code統合 |

`openai-responses`を既定のまま維持する。既定backendの名称変更だけで再生成やAPI再課金を発生させない。従量課金backendへの自動fallbackは既定で無効とする。

対象はSQLite/Vaultを用いる現行の`feedian ingest`経路とする。`process_bookmarks`、`process_hatena_export`、旧`config.json`の直接実行経路は今回の対象外とする。

### 確定事項

1. **Fingerprint互換性**
   - legacy fingerprintによる互換検索を1リリースだけ残す。
   - legacy結果の再利用に成功した際は、新形式の再利用キーを同時に保存する。
   - backend IDの`openai`から`openai-responses`への変更だけでLLMを再実行しない。
   - 互換期間終了後は新キーだけを利用する。

2. **CanonicalSummarySchema**
   - 初期schema versionは`1`とする。
   - `title`と`summary`は必須とする。
   - `tags` と `content_type` は空を許容する。現行データの結果再利用を優先するためである。
   - providerに送信する`ProviderOutputSchema`と、Feedian内部で検証する`CanonicalSummarySchema`は分離する。
   - 処理順序は「provider出力の解析 → 許容された正規化 → canonical schema検証」とする。
   - 正規化で救済できるのは、文字列の前後空白、既存実装と等価な型強制、文字数と要素数の切り詰めに限定する。必須フィールド欠落、解釈不能な型、不正JSONは失敗とし、source noteを作成しない。

3. **Vault config**
   - Vault configを`format_version = 2`へ上げ、LLM backend設定をVaultごとに保存する。
   - `feedian migrate`による明示的な移行を必要とし、読み込み時の暗黙移行は行わない。
   - 旧版Feedianは新format versionを明確なエラーで拒否する。未知キーを警告で読み飛ばすようには変更しない。

4. **認証と課金区分**
   - `auth_mode`と`billing_mode`は`llm_run`の第一級列として保存する。backend固有の詳細だけをmetadata JSONへ格納する。
   - `auth_mode`は`api-key`、`local-session`、`unknown`のいずれかとする。
   - `billing_mode`は`metered-api`、`subscription`、`unknown`のいずれかとする。
   - backendは必要な資格情報の種類をcapabilityとして宣言し、実行前のpreflightで検証する。API key不足とCLI login不足は区別して`BackendAuthError`とする。

5. **Source note frontmatter**
   - 初期リリースでは現行の`model`だけを維持し、`backend`は追加しない。
   - backendのprovenanceはDBの`llm_run`で追跡する。既存note全件に意味のない差分を発生させない。

6. **Local agentの安全ポリシー**
   - Web記事とRSS本文をuntrusted inputとして扱う。本文はstdinで渡し、argvに載せない。
   - 必須の安全条件は、untrusted inputがFeedian用の専用一時ディレクトリ外のファイル、shell、browser、MCP、networkにアクセスできないこととする。組み込みツールも無効化対象に含む。
   - Codexはまず`codex exec`の隔離条件をPoCで検証する。`read-only`の名称だけを適合根拠にしない。
   - CLIで必須条件を実証できない場合は、App Serverのrestricted read access、専用`readableRoots`、network offを使用する。
   - CLIまたはApp Serverのいずれでも必須条件を実証できなければ、その環境で`codex-local`を使用不可とし、本文送信前に`BackendPolicyError`で失敗させる。
   - 初期版で外部OS sandboxを必須実装にはしない。必須条件を満たすために必要と判明した場合は別の設計変更として導入する。
   - Claude Codeにも同じ安全条件と事前失敗原則を適用する。

7. **Raw responseとevent log**
   - local-agent backendのraw event log、全stdout、全stderrは既定で保存しない。
   - 保存するのは、最終的な構造化JSON、usage、backend、model、backend実装revision、CLIまたはserver version、`auth_mode`、`billing_mode`、結果status、実行時間、サニタイズ済みエラーとする。
   - エラー文はUTF-8で8 KiBを上限とし、API key、login token、認証header、ユーザーパス、記事本文、prompt、tool outputをredactionする。
   - raw debug logの保存機能は今回の対象外とし、必要になった時点でopt-in、保存期間、暗号化、redactionと操作者向け警告を別文書で定義する。

### 共通backend契約

backendは、次の責務を共通interface経由で提供する。

- stableで一意なbackend ID。`claude`のようにAPIとlocal agentを区別できないIDは禁止する。
- model、prompt version、canonical schema version、入力上限、認証方式、課金方式、usage取得可否、最大並行度、最小起動間隔を含むcapability。
- 認証、実行バイナリ、version、ポリシーを調べるpreflight。version取得はingest runごと1回だけ行いキャッシュする。
- canonical requestの受け取り、provider固有requestへの変換、実行、結果と監査情報の返却。
- provider固有の再試行、polling、rate limit制御。共通層はadapter全体を無条件に再実行しない。
- 1記事あたりの総wall-clock deadlineの遵守。HTTP requestやプロセス単位のtimeoutと区別する。

local-agent backendは1記事につき1processまたは1threadとし、初期の`max_parallelism`は`1`とする。一時ディレクトリは記事ごとに作成し、所有者のみ読み書き可能なpermissionを設定し、成功、失敗、timeout、中断のすべてで後始末する。

untrusted inputのラッピングは共通契約に含める。意味を変えない区切り、閉じタグの保持、指示の後置再掲、backendごとの入力上限による切り詰めは許容する。内容を変えるbackend固有のプロンプ自動書き換えは行わない。

### 永続化と再利用

DB schemaをversion 6へ移行し、`llm_run`に少なくとも次を追加する。

- `backend`
- `summary_schema_version`
- `fingerprint_version`
- `auth_mode`
- `billing_mode`
- `backend_metadata_json`

旧データのbackendはrequest JSONのlegacy `provider`から可能な範囲でbackfillする。判定できない値は`unknown`のまま残し、新規結果の再利用対象にしない。

新しい再利用検索は、resource revision、operation、backend、model、prompt version、summary schema version、logical input fingerprintがすべて一致した場合だけ成功とする。backendごとに本文の最大文字数が異なるため、結果をbackend境界を越えて再利用しない。

### 設定とfallback

設定はbackendとmodelを独立に持つ。fallbackを有効にする場合は、fallback先の`backend`と`model`を両方明示する。

```toml
[llm]
backend = "openai-responses"
model = "gpt-5.4-mini"

[llm.fallback]
enabled = false
backend = "manus-api"
model = "manus-1.6"
```

fallbackは、明示的に有効化され、失敗種別がfallback対象と定義された場合だけ行う。特に`subscription`の利用枠不足やlocal session不備から`metered-api`へ暗黙に切り替えない。

### Usageと表示

usageを取得できないrunは`unknown`とし、0として永続化または個別表示しない。集計では判明したtoken数だけを合計し、別途`unmetered_requests`を併記する。価格、token見積もり、usage historyはbackend-awareとし、未知のbilling modeをAPI課金と推定しない。

### 受け入れ条件

- `openai-responses`と`manus-api`の現行出力が等価に維持される。
- backend ID変更後も、legacy fingerprintと一致する既存結果はAPI再実行なしで再利用できる。
- 新規結果はbackend境界を越えて再利用されない。
- 正規化後のcanonical schema検証に失敗した場合、source noteと成功runを作成しない。
- fallbackは既定で発生せず、有効化時もbackendとmodelの明示が必須である。
- local-agent backendは、本文をargvへ出さず、制限外のファイル、shell、browser、MCP、networkの利用をprompt-injection fixtureから誘発されても拒否する。
- 安全ポリシーをpreflightで証明できないlocal-agent backendは、untrusted inputの送信前に失敗する。
- 一時ファイルは成功、失敗、timeout、中断のすべてで削除される。
- usage欠落は個別runで`unknown`と表示され、集計では`unmetered_requests`に計上される。
- source note frontmatterはbackend追加だけを理由に書き換えられない。

CIではfake runnerを用いたbackend契約テスト、migrationテスト、fingerprint互換テスト、schema正規化テスト、redactionテストを必須とする。実バイナリとログインが必要なCodexおよびClaude Codeの安全テストはopt-inの統合テストとし、未実行の場合は明示的にskipする。

### 実装順序

1. 共通の型、backend ID、capability、例外、canonical schemaを導入する。
2. DB schema version 6、legacy fingerprint互換検索、Vault config version 2の移行を実装する。
3. `openai-responses`と`manus-api`をadapterへ移し、現行挙動と再利用を回帰テストする。
4. fake runnerを使ってlocal-agent共通実行層、stdin受け渡し、deadline、cleanup、redactionを実装する。
5. `codex-local`のCLI PoCで安全条件、構造化出力、usage取得を検証し、必要な場合だけApp Serverへ切り替える。
6. 同一記事群で`openai-responses`、`manus-api`、`codex-local`を比較し、experimentalのまま結果を記録する。
7. Claude CodeのCLI契約と安全条件を別途確認した上で`claude-code-local`を追加する。

CodexまたはClaude Codeをstableもしくは既定backendへ昇格する場合は、品質、コスト、速度、利用枠、運用安定性の測定結果を根拠に別の意思決定文書で確定する。

## 草案

### 位置づけ

提案仕様。既存のOpenAI ResponsesおよびManusによる`ingest`の挙動を維持したまま、ローカルのCodexとClaude Codeを段階的に追加するための目標設計を定める。

### 目的

Feedianの要約処理を、特定ベンダーのAPIやCLIから分離する。抽象化の単位は「LLMモデル」ではなく「LLM実行バックエンド」とする。同じモデル提供元でも、直接APIとローカルエージェントCLIでは、認証、課金、権限、実行時間、出力、失敗時の扱いが異なるためである。

この仕様は次を実現する。

- OpenAI ResponsesとManusの既存挙動を共通契約の背後へ移す。
- Codex CLIとClaude Code CLIをローカル専用のopt-inバックエンドとして追加できるようにする。
- 将来、Anthropic APIなどをCLI版と混同せずに追加できるようにする。
- Web記事をエージェントへ渡す際の権限と永続化をFeedian側のポリシーで制約する。
- バックエンドごとに異なるusage、課金、監査情報を失わずに正規化する。

### 用語と識別子

`backend`は実行経路、`model`はその経路で選択するモデル、`auth_mode`は認証・課金経路を表す。これらを1つの`provider`値へまとめない。

初期の正式なバックエンド識別子は次のとおりとする。

| Backend ID | 実行方式 | 初期状態 |
| --- | --- | --- |
| `openai-responses` | OpenAI Responses API | 既定・stable |
| `manus-api` | Manusの非同期タスクAPI | stable |
| `codex-local` | ローカルCodex CLI | experimental・opt-in |
| `claude-code-local` | ローカルClaude Code CLI | experimental・opt-in |

`anthropic-api`は将来の直接API用に予約する。`claude`や`openai`のように実行経路が不明なIDは新設しない。

### アーキテクチャ

`ingest`はバックエンド固有の分岐を持たず、記事要約に必要な共通契約だけを利用する。

```text
IngestService
    |
    v
SummaryBackend
    +-- OpenAIResponsesBackend
    +-- ManusBackend
    +-- CodexCliBackend
    +-- ClaudeCodeCliBackend
    `-- AnthropicApiBackend       (future)
```

CodexとClaude Codeは、プロセス起動、標準入出力、タイムアウト、終了コード、プロセスツリー停止、一時作業ディレクトリを扱う内部部品`LocalAgentRunner`を共有してよい。ただし、コマンドライン構築、安全設定、イベント形式、usage解析は各バックエンドに残す。APIとCLIを同じ低水準インターフェースへ押し込まない。

### 共通契約

概念上の入力は`SummaryRequest`、出力は`BackendResult`とする。具体的なPython型名は実装時に変更してよいが、次の情報を欠落させてはならない。

`SummaryRequest`は次を含む。

- 元記事のタイトル、URL、抽出本文および既存メタデータ
- 出力言語
- Feedianのプロンプトバージョン
- JSON Schemaとスキーマバージョン
- モデル、推論強度、出力上限などの論理的生成設定
- 実行期限と安全ポリシー

`BackendResult`は次を含む。

- スキーマ検証済みの要約結果
- 正規化したusage。報告されない値は`0`ではなく不明値とする
- provider報告額とFeedian推定額を区別した課金情報
- 秘密情報を除去した実リクエストまたは起動引数
- 生レスポンスまたはイベントログ
- backend ID、モデル、実装リビジョン、CLI/APIのバージョンなどの実行メタデータ
- 警告およびリモートタスクIDなどの復旧情報

すべてのバックエンド出力は、保存前にFeedian側でも同じJSON Schemaで検証する。構造化出力機能があることだけに依存しない。不正な結果ではsource noteを生成しない。

### 能力と安全ポリシー

各バックエンドは少なくとも次の能力を宣言する。

- 実行種別: `http`または`local-agent`
- 厳密な構造化出力の可否
- ツール実行を完全に無効化できるか
- エージェント用ネットワークアクセスを無効化できるか
- ユーザー設定、プロジェクトルール、拡張機能を無視できるか
- セッションを永続化せず実行できるか
- usageおよび金額を報告できるか
- 実行中処理を安全にキャンセルできるか

能力は表示や分岐の参考情報ではなく、実行前検証に使う。Feedianが要求するポリシーを満たせない場合、設定を暗黙に弱めず`BackendPolicyError`で停止する。

Webページ、RSS本文、コメントおよび取得メタデータはすべて信頼できない入力として扱う。ローカルエージェントの初期ポリシーは次のとおりとする。

- 記事ごとに新しい独立実行を使い、会話を記事間で共有しない。
- Feedianが作成した専用一時ディレクトリを作業ディレクトリにする。
- shell、ファイル読み書き、ブラウザー、MCPなどのツールをすべて無効化する。
- 推論サービスとの通信を除き、エージェントが利用するネットワーク機能を無効化する。
- ユーザー設定、プロジェクトルール、skills、plugins、hooks、自動memoryを読み込まない。
- セッション、プロンプト履歴、記事本文をローカルへ永続化しない。
- 子プロセスへ渡す環境変数を必要最小限にし、監査ログへ秘密情報を残さない。

Claude Codeの初期アダプターは、非対話実行、bareモード、全ツール無効化、セッション非永続化、JSON出力、JSON Schema指定を組み合わせる。bareモードだけでは組み込みツールが残るため、全ツール無効化を別途必須とする。Codexアダプターも同等の結果となるsandbox、設定無視、rules無視、ephemeral実行を必須とする。CLIフラグ名はバージョン差をアダプター内へ閉じ込める。

### 設定と選択

目標設定では`backend`と`model`を分離する。

```json
{
  "llm": {
    "backend": "openai-responses",
    "model": null,
    "fallback": {
      "enabled": false,
      "backend": null
    }
  }
}
```

バックエンドの選択順は、コマンドラインの`--backend`、`LLM_BACKEND`、Vault設定、組込み既定値`openai-responses`とする。モデルは`--model`、バックエンド固有の環境変数、Vault設定、バックエンド既定値の順で解決する。APIキー、ログイントークン、その他の秘密情報はVault設定へ保存しない。

移行期間中は既存の`--provider openai|manus`と`LLM_PROVIDER`を、それぞれ`openai-responses`と`manus-api`への非推奨エイリアスとして受け付けてよい。`provider`は収集元にも使われる語であるため、新しいLLM設定と表示では`backend`を使う。

自動フォールバックはすべてのバックエンドで既定無効とする。特にローカルのサブスクリプション枠、認証、CLI障害を理由に、有料APIへ暗黙に切り替えない。有効化する場合は対象バックエンドを明示し、実行前プレビューと監査記録に表示する。

### 実行、失敗、キャンセル

ローカルCLIの初版は記事ごとに1プロセスを起動する。性能改善のための常駐プロセスやセッション再利用は、安全性と記事間分離を維持できる別仕様ができるまで導入しない。

共通エラー分類は少なくとも次を持つ。

- `BackendUnavailableError`: CLI未導入、サービス到達不能
- `BackendAuthError`: 認証不足または期限切れ
- `BackendRateLimitError`: 利用枠またはレート制限
- `BackendTimeoutError`: Feedian側の期限超過
- `BackendProtocolError`: JSON、イベント、スキーマの不正
- `BackendPolicyError`: 必須の安全ポリシーを満たせない

CLIのタイムアウトと中断では、親プロセスだけでなく当該プロセスツリーを終了させ、終了結果を監査記録へ残す。ManusのようにFeedianから停止できないリモートタスクでは、タスクIDと確認URLを必ずエラーおよび監査記録へ残す。

各ローカルアダプターは実行前にCLIの存在とバージョンを確認する。必須の安全機能または出力機能を持たないバージョンは、推測した代替フラグで実行せず、実行前に拒否する。検出したバージョンは監査記録へ残す。

### Usage、課金、監査、再利用

正規化usageは入力、キャッシュ済み入力、出力、推論その他を別フィールドで保持する。バックエンドが報告しない値は不明のままとし、欠損を無料またはゼロトークンと解釈しない。

課金情報は、バックエンド報告額、Feedianの単価表による推定額、認証・課金モードを分離する。ローカルCLIのサブスクリプション利用時にも、API換算額と実請求額を混同しない。

既存のLLM run監査には、論理リクエスト、秘密情報を除去した実リクエスト、生レスポンス、正規化結果、usage、課金情報、backend ID、モデル、実装リビジョンを保存する。記事本文を新しいデバッグログやCLIセッション履歴へ重複保存しない。

結果再利用キーには少なくとも、backend ID、モデル、プロンプトバージョン、スキーマバージョン、言語、生成設定、入力コンテンツのfingerprintを含める。別バックエンドの結果を同一モデル名だけで再利用しない。認証秘密そのものはキーへ含めない。

### 導入順序

1. 共通型、backend registry、factoryを追加し、OpenAIとManusを挙動変更なしで移す。
2. CLI、`ingest`、usage・価格計算にあるbackend固有分岐を各アダプターまたはprofileへ移す。
3. `LocalAgentRunner`と`codex-local`をexperimental backendとして追加する。
4. 同じrunner上に`claude-code-local`を追加する。
5. 同一記事集合で品質、構造化出力成功率、所要時間、usage、安全性を比較する。
6. 十分な評価後も、既定backendの変更は別の仕様変更として判断する。

### 対象外

- CodexまたはClaude CodeをCloudflare Workers内で直接起動すること。
- LLMにページ取得、ファイル操作、shell実行を任せること。
- バックエンド間でプロンプトを自動最適化し、意味を変えること。
- サブスクリプション利用枠を無制限または無料の処理能力として扱うこと。
- experimental backendの失敗を有料APIへ暗黙にフォールバックすること。

### 検証と受け入れ条件

- OpenAIとManusの既存テストおよび保存結果が移行前と同等である。
- backendとmodelを独立に選択でき、不明な組み合わせを実行前に拒否する。
- 共通Schema検証に失敗した結果からnoteを生成しない。
- ローカルCLIのテストで、ツール無効化、設定無視、非永続化、専用cwd、タイムアウト時のプロセスツリー停止を確認する。
- プロンプトインジェクションを含む記事を使い、ファイル、shell、ブラウザー、MCPへアクセスできないことを確認する。
- usage欠損がゼロとして保存・表示されない。
- cacheまたは既存runの再利用がbackendをまたがない。
- フォールバック無効時は、利用枠切れや認証失敗で別backendを呼ばない。
- 監査記録にAPIキー、ログイントークン、認証ヘッダーが含まれない。
- 未対応のCLIバージョンは記事本文を送信する前に拒否される。

### 参考資料

- [Codexの非対話実行](https://developers.openai.com/codex/non-interactive-mode)
- [Codex App Server](https://developers.openai.com/codex/app-server)
- [Claude Code CLIリファレンス](https://code.claude.com/docs/en/cli-usage)
- [Claude Codeの非対話実行](https://code.claude.com/docs/en/headless)

## レビュー

### レビュー1 — Claude Code (2026-08-16)

草案の方向性、特にbackend・model・auth modeの分離、capabilityの事前強制、fallback既定無効は妥当である。ただし、現行実装と衝突する事項と、実装前に決める必要がある事項が残っている。

#### 現行実装と衝突する事項

**A. fingerprint移行。** `_candidate`はOpenAI形式のrequestへ`provider`を追加してrequest全体をSHA-256でfingerprint化している。`openai`を`openai-responses`へ改名すると既存fingerprintと一致しなくなり、再利用可能だったLLM runが見つからなくなる。「legacy値をfingerprint入力に維持する」「再ingestを受容する」「旧キーによる互換検索を期間限定で残す」のどれかを決める必要がある。

**B. `llm_run`の保存形式。** `llm_run`にはmodel、prompt version、input fingerprintはあるが、backendを独立して検索・監査する列がない。`successful_llm_result`のWHERE句もbackendを持たない。backend列の追加と検索条件への反映を含むDB migrationを仕様化する必要がある。実装リビジョン、CLIバージョン、auth mode、課金区分の保存場所も決める必要がある。

**C. summary schema version。** DB schema versionとprompt versionは存在するが、要約結果Schema自体のversionは存在しない。新設すると再利用キーが変わるため、A・Bと同じ移行単位で扱う必要がある。

**D. 正規化と検証の順序。** `normalize_summary_result`は、長さと要素数の切り詰め、数値の文字列化、文字列から配列への変換などでprovider応答を救済する。Manusへ送るSchemaは未対応制約を除外するため、この救済は正常系の一部である。草案の「同じJSON Schemaで検証し、不正ならnoteを作らない」だけでは既存Manus挙動を失う。正規化してよい逸脱、拒否する逸脱、検証順序を明記する必要がある。

**E. Vault configの互換性。** 現在の`load_vault_config`はトップレベルの未知キーを拒否するため、`llm`を追加するにはconfig形式の変更が必要である。新configを旧Feedianが読む場合の扱いとして、format version更新または未知キー処理の変更を決める必要がある。

**F. 資格情報preflight。** 現在の`ingest_source_notes`は`MANUS_API_KEY`または`OPENAI_API_KEY`を直接要求する。CLIのログイン状態を使うlocal-agentには適用できない。資格情報種別をprofileまたはcapabilityへ追加し、preflightをbackendへ委譲する必要がある。

**G. 二つの実行経路。** modern `feedian ingest`とは別に、repository-root `config.json`とOpenAIを直接使うlegacy direct-export経路が存在する。今回の移行対象へ含めるか、対象外とするかを明記する必要がある。

#### 未定義の事項

- untrusted本文はargvへ載せずstdinまたは専用一時ファイルで渡し、一時ファイルの権限と異常終了時の削除を定める。
- backendごとの最大並行数と最小起動間隔を定める。
- 共通層とadapterのどちらがretryを担当するか定める。
- 記事1件の総所要上限と、HTTP、poll、process単位のtimeoutを区別する。
- `max_article_chars`、出力上限、reasoning、retry回数などの供給元を定める。
- `auth_mode`の値、設定、capability、監査上の位置づけを定める。
- source note frontmatterへbackendを追加するか、既存noteへの差分を許容するか決める。
- untrusted wrappingを共通規約へ上げ、意味を変えないsecurity envelopeと禁止するprompt最適化を区別する。
- CLI version検出を記事ごとではなくingest runごとに一度だけ行う。
- fallback先のmodelを明示するか、backend既定値を使うか決める。

#### 受け入れ条件の検証可能性

- fake runnerによる契約テストと、実CLI・認証を必要とするopt-in統合テストを分ける。
- per-runのusage欠損はunknownとして保存・表示し、集計値では0加算と`unmetered_requests`の併記を区別する。

#### 良い点

- backend・model・auth modeの分離と、曖昧な`claude`や`openai`というbackend IDを避けた点は妥当である。
- fallback既定無効は、local subscriptionの失敗から従量APIへ意図せず切り替わる課金事故を防ぐ。
- Claude Codeのbare modeだけに依存せず、全ツール無効化を別要件とした点は具体的である。

Claude Codeは、A、B、Dを実装着手前の最優先事項とした。

### レビュー2 — Codex (2026-08-16)

レビュー1は現行コードとの照合精度が高く、指摘の大半を採用する。ただし、fingerprint、DB migration、summary schemaは別々の問題ではなく一つの永続化形式移行として扱う。また、Vault configの未知キー許容、現行の並行度、監査メタデータの保存場所については補正が必要である。

#### レビュー1の採否

| 項目 | 採否 | 理由 |
| --- | --- | --- |
| A. fingerprint移行を明示する | 採用 | `request["provider"]`を含む現行hashはbackend改名で一致しなくなる。新旧キーの互換方針がないまま実装するとAPI再呼び出しを招く |
| B. `llm_run`にbackendを追加する | 修正して採用 | backendは再利用・usage検索用の第一級列が必要。ただし実装リビジョンやCLI versionは専用metadata JSONへ、課金詳細は既存`price_json`の拡張へ置けるため、すべてを個別列にする必要はない |
| C. summary schema versionを新設する | 採用 | prompt変更と出力契約変更は別概念であり、再利用境界にも必要。A・Bと同じDB migrationで導入する |
| D. 正規化と検証順序を定める | 修正して採用 | providerへ要求するSchemaと、正規化後に保存可能なSchemaを同一にすると既存挙動を維持できない。二種類のSchemaへ分ける |
| E. Vault config互換性を決める | 修正して採用 | `format_version = 2`へ上げ、未知キーは引き続き拒否する。旧Feedianが新configを安全に拒否することを許容し、未知キーを警告だけにしてOpenAI既定で続行する案は課金事故につながるため不採用 |
| F. backendへ資格情報preflightを委譲する | 修正して採用 | credential kindだけではCLIでAPI keyを使う場合を表せない。auth modeとbilling modeを分離し、backendのpreflightで解決する |
| G. legacy経路の範囲を明記する | 採用 | 初版はmodern SQLite/Vault `feedian ingest`だけを対象とする。`process_bookmarks`だけでなく`process_hatena_export`も対象外とする |
| 本文をargvへ載せない | 採用 | argvはprocess listへ露出し得る。untrustedなタイトル、URL、本文、コメントはstdinで渡す。Schemaファイルが必要な場合だけ専用一時ファイルを使う |
| 最大並行数と起動間隔 | 優先度を下げて採用 | 現行ingestは逐次処理であり直ちにマシンを圧迫しない。将来の並列化に備えprofileへ`max_parallelism`と`min_start_interval_seconds`を置き、local-agentの初期値を1にする |
| retry責務 | 採用 | adapterが一時エラー分類、transport retry、pollingを担当する。共通層は処理全体を盲目的に再試行せず、remote taskの重複作成を避ける |
| deadline定義 | 採用 | 記事1件のwall-clock deadlineを共通契約とし、各HTTP、poll、process timeoutと区別する。retryとpollingは同じ総予算を消費する |
| 生成パラメータの供給元 | 採用 | backend profileが既定値と制約を持ち、CLIとVault設定による許可されたoverrideを解決後の`SummaryRequest`へ固定する |
| `auth_mode`を具体化する | 修正して採用 | 認証と課金を一つの値へまとめず、`auth_mode`と`billing_mode`へ分離する |
| source noteへbackendを追加する | 初版では不採用 | backend provenanceはDB監査を正とし、frontmatterは既存のmodelのみを維持する。全noteへ不要な差分を出さず、利用者向け要件が生じたら別仕様で追加する |
| untrusted wrappingを共通化する | 採用 | 共通層は信頼境界、閉じタグ保持、切り詰め規則を定め、adapterはsystem channelの有無に応じて同じ意味をtransportへ写像する |
| CLI version検出をrunごとに一度にする | 採用 | backend preflightで一度検出してrun中にcacheし、検出結果を各監査記録から参照できるようにする |
| fallback先modelを決める | 採用 | fallbackはbackendとmodelを一組で指定する。実行前に解決済みの値を表示し、backend既定へ暗黙に委ねない |
| 契約テストと実CLIテストを分ける | 採用 | CIではfake runnerによるargv・stdin・policy・redactionの契約を必須検証し、実CLIのprompt injection試験は認証済み環境のopt-inテストとする |
| usageのunknownと集計0を分ける | 採用 | per-runの欠損はunknownのまま保持する。合計では数値へ加算せず、`unmetered_requests`を併記する |

#### 永続化形式移行の推奨

DB schemaをv6へ上げ、少なくとも次を`llm_run`へ追加する。

- `backend TEXT`
- `summary_schema_version TEXT`
- `fingerprint_version INTEGER`
- `auth_mode TEXT`
- `billing_mode TEXT`
- `backend_metadata_json TEXT`

既存行のbackendは`request_json.provider`を読み、`openai`を`openai-responses`へ、`manus`を`manus-api`へbackfillする。値が判定できない行は推測で別backendへ割り当てず`unknown`として残し、通常の再利用対象から外す。

新しい再利用検索は、少なくともresource revision、operation、backend、model、prompt version、summary schema version、logical input fingerprintをWHERE条件にする。backendは第一級列で分離するため、新しいlogical input fingerprintへbackend IDを重複して埋め込む必要はない。

現行fingerprintを持つ完了runは失わず、移行後1リリースの間だけlegacy fingerprintによる互換検索を許可する。互換検索で見つけた結果を再利用する場合は、その事実を監査またはprogressへ表示する。新形式のキーへ置き換えるためだけにLLM APIを再呼び出してはならない。

#### 二種類のsummary schema

`ProviderOutputSchema`はLLMへ要求する理想的な形を表す。OpenAIには完全なSchemaを送り、対応範囲の狭いbackendには意味を維持した部分Schemaを送る。

`CanonicalSummarySchema`は`normalize_summary_result`後にFeedianが保存できる形を表す。処理順序を次に固定する。

```text
provider response
  -> JSON objectの確認
  -> 既知フィールドの抽出
  -> 許可された型変換と切り詰め
  -> CanonicalSummarySchemaの検証
  -> BackendResult
  -> llm_runとsource noteの保存
```

既存挙動を維持する初期版では、数値から文字列、カンマまたは改行区切り文字列から配列、配列要素から文字列への変換を許可する。文字数と要素数の超過は切り詰める。結果全体がobjectでない場合、`note_title`または`summary`が正規化後に空の場合は拒否する。`tags`が空、`content_type`が空文字である現行の救済範囲は初期版では維持し、将来厳格化する場合はschema versionを上げる。

#### 安全ポリシーとlocal-agentの成立条件

untrustedなタイトル、URL、メタデータ、コメント、本文はすべてstdinで渡し、argvへ載せない。argvには固定の指示、model名、安全フラグ、Schemaファイルなどの制御情報だけを置く。CLIがSchemaファイルを要求する場合、Feedian専用の一時ディレクトリへ必要最小限のファイルだけを制限された権限で作り、成功、失敗、timeout、中断のすべてで`finally`相当の処理により削除する。

各記事は独立process、独立contextで実行する。local-agentの初期`max_parallelism`は1とし、session再利用や常駐processは別仕様なしに導入しない。CLI processへ継承する環境変数はallowlist方式とし、認証に必要な情報以外のsecretを渡さない。

`claude-code-local`は、非対話実行、bare mode、全tool無効化、session非永続化、JSON output、JSON Schemaを同時に満たすCLI versionだけを許可する。

`codex-local`は、read-onlyという名称だけでは採用条件を満たさない。Codex App Serverのread-only accessは既定でfull accessであるため、restricted readable roots、network無効化、user configとrulesの無視、ephemeral実行をPoCで確認する。CLI単体で全toolとfilesystem readを制限できない場合は、App Serverまたは外部OS sandboxを使用する。それでも必須ポリシーを満たせない環境ではbackendを利用不可とし、記事本文を送る前に`BackendPolicyError`を返す。

#### 認証、課金、usage、見積り

`auth_mode`は少なくとも`api-key`、`local-session`、`unknown`を表す。`billing_mode`は少なくとも`metered-api`、`subscription`、`unknown`を表す。backendは複数の組合せをsupportしてよく、preflightが今回のrunで解決した値を返す。secret自体はVault config、監査、argvへ保存しない。

usageの各値はnullableとし、欠損を0として永続化しない。集計時は判明している値だけを合算し、欠損run数を`unmetered_requests`として併記する。provider報告額、Feedian推定額、API換算参考額を別フィールドにし、subscription利用時のAPI換算額を実請求額として表示しない。

token countと価格計算もbackend固有である。backend profileまたはadapterは`token_estimator`と`pricing_strategy`を提供する。Claude向け入力をOpenAI tokenizerで数えた値を正確なtoken数として表示しない。見積り不能なbackendではunknownと理由を表示し、0 USDと表示しない。

usage履歴、output/input ratio、見積りの検索もbackend境界を越えてはならない。現在modelだけで絞り込む`_historical_output_ratio`を、backendと必要に応じてauthまたはbilling modeで分離する。

#### retry、deadline、cancel

`SummaryRequest.execution_deadline`は記事1件の開始から完了までのwall-clock上限とする。HTTP timeout、poll interval、process待機時間、retry delayはbackend固有だが、すべて同じ総deadlineを消費する。

adapterは一時エラーの分類、個々のtransport retry、remote task pollingを担当する。共通層はadapter全体を自動再試行しない。応答が不明なまま終了したcreate要求やlocal processを再実行すると、重複課金または重複taskを作る可能性があるためである。

local processのtimeoutまたは中断ではprocess tree全体を停止し、停止結果を監査へ残す。停止できないremote taskではtask IDと確認URLを必ず残す。

#### 監査とredaction

監査は論理request、secretを除去した実request、正規化結果、usage、課金、backend metadata、エラーと復旧情報を保持する。raw responseまたはevent logは保存前にredactし、stdoutとstderrを区別し、最大保存sizeを設ける。記事本文、認証header、token、ローカルのuser pathを新しいdebug logやsession履歴へ重複保存しない。

CLI versionはbackend preflightでingest runごとに一度検出する。必須flagまたは安全機能のないversionは、推測した代替flagで実行せず、記事本文を送る前に拒否する。

#### 設定と対象範囲の推奨

Vault configは`format_version = 2`へ上げ、`llm`を正式なトップレベルkeyとして追加する。未知keyは引き続きエラーにする。旧Feedianが新形式を拒否することは、安全な前方非互換として許容する。API key、login token、session tokenはVaultへ保存しない。

fallbackを有効にする場合は、少なくともbackendとmodelを明示する。

```json
{
  "llm": {
    "backend": "codex-local",
    "model": "example-model",
    "fallback": {
      "enabled": false,
      "backend": null,
      "model": null
    }
  }
}
```

初期対象はmodern SQLite/Vault workflowの`feedian ingest`だけとする。repository-root `config.json`を使うlegacy `process_bookmarks`、`process_hatena_export`、legacy estimateは変更しない。legacy経路の廃止またはbackend対応は別仕様とする。

source note frontmatterは初期版では既存の`model`だけを維持し、backend provenanceはDB監査を正とする。`llm_backend`を利用者向けfrontmatterへ追加する場合は、既存noteの差分と再render方針を決める別仕様とする。

#### テスト境界

通常CIで必須とする契約テストは次を含む。

- backendとmodelの独立選択および未対応組合せの拒否
- legacy fingerprint互換検索とbackendをまたがない再利用
- ProviderOutputSchema、正規化、CanonicalSummarySchemaの境界
- authおよびbilling modeのpreflight
- fallback無効時に別backendを呼ばないこと
- fake local runnerでのargv、stdin、環境変数allowlist、専用cwd、cleanup、timeout、process tree停止
- usage unknown、`unmetered_requests`、backend別履歴、見積り不能時の表示
- audit redactionと保存size上限

実CLIと認証済み環境を必要とするopt-in統合テストは次を含む。

- supported CLI versionごとの構造化出力
- prompt injectionを含む記事からfilesystem、shell、browser、MCP、network toolへ到達できないこと
- sessionとprompt履歴が永続化されないこと
- timeoutと中断後に子processが残らないこと

#### 導入順序の推奨

1. DB v6、backend列、summary schema version、fingerprint v2、legacy互換検索を導入する。
2. ProviderOutputSchemaとCanonicalSummarySchemaを分離し、既存OpenAI・Manus挙動を固定する。
3. 共通型、registry、factory、profile、preflightを導入し、OpenAI・Manusをadapterへ移す。
4. usage、token estimate、price estimate、履歴検索をbackend-awareにする。
5. `LocalAgentRunner`をfake runnerの契約テストとともに導入する。
6. `codex-local`の安全性PoCを行い、成立した環境だけexperimental backendとして有効化する。
7. `claude-code-local`をexperimental backendとして追加する。
8. 同一記事集合で品質、Schema成功率、latency、usage、課金表示、安全性を比較する。
9. 既定backendの変更は評価結果を基に別仕様で判断する。

#### 最終案で人間が決める事項

1. legacy fingerprint互換検索を1リリース残すか、migration時に新キーを生成するか。
2. `CanonicalSummarySchema`で空の`tags`と`content_type`を初期版も許容するか。
3. Vault configをformat version 2へ上げるか、LLM設定を当面環境変数とCLIだけに置くか。
4. `auth_mode`と`billing_mode`の列を第一級列にするか、backend metadataへ含めるか。
5. source note frontmatterをmodelのみのまま維持するか。
6. Codexの必須安全ポリシーをCLI、App Server、外部OS sandboxのどれで満たすか。
7. raw response/event logの保存size上限とredaction対象をどこまでにするか。
