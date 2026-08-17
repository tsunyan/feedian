# LLM実行バックエンド

## 状態

提案仕様。既存のOpenAI ResponsesおよびManusによる`ingest`の挙動を維持したまま、ローカルのCodexとClaude Codeを段階的に追加するための目標設計を定める。

## 目的

Feedianの要約処理を、特定ベンダーのAPIやCLIから分離する。抽象化の単位は「LLMモデル」ではなく「LLM実行バックエンド」とする。同じモデル提供元でも、直接APIとローカルエージェントCLIでは、認証、課金、権限、実行時間、出力、失敗時の扱いが異なるためである。

この仕様は次を実現する。

- OpenAI ResponsesとManusの既存挙動を共通契約の背後へ移す。
- Codex CLIとClaude Code CLIをローカル専用のopt-inバックエンドとして追加できるようにする。
- 将来、Anthropic APIなどをCLI版と混同せずに追加できるようにする。
- Web記事をエージェントへ渡す際の権限と永続化をFeedian側のポリシーで制約する。
- バックエンドごとに異なるusage、課金、監査情報を失わずに正規化する。

## 用語と識別子

`backend`は実行経路、`model`はその経路で選択するモデル、`auth_mode`は認証・課金経路を表す。これらを1つの`provider`値へまとめない。

初期の正式なバックエンド識別子は次のとおりとする。

| Backend ID | 実行方式 | 初期状態 |
| --- | --- | --- |
| `openai-responses` | OpenAI Responses API | 既定・stable |
| `manus-api` | Manusの非同期タスクAPI | stable |
| `codex-local` | ローカルCodex CLI | experimental・opt-in |
| `claude-code-local` | ローカルClaude Code CLI | experimental・opt-in |

`anthropic-api`は将来の直接API用に予約する。`claude`や`openai`のように実行経路が不明なIDは新設しない。

## アーキテクチャ

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

## 共通契約

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

## 能力と安全ポリシー

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

## 設定と選択

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

## 実行、失敗、キャンセル

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

## Usage、課金、監査、再利用

正規化usageは入力、キャッシュ済み入力、出力、推論その他を別フィールドで保持する。バックエンドが報告しない値は不明のままとし、欠損を無料またはゼロトークンと解釈しない。

課金情報は、バックエンド報告額、Feedianの単価表による推定額、認証・課金モードを分離する。ローカルCLIのサブスクリプション利用時にも、API換算額と実請求額を混同しない。

既存のLLM run監査には、論理リクエスト、秘密情報を除去した実リクエスト、生レスポンス、正規化結果、usage、課金情報、backend ID、モデル、実装リビジョンを保存する。記事本文を新しいデバッグログやCLIセッション履歴へ重複保存しない。

結果再利用キーには少なくとも、backend ID、モデル、プロンプトバージョン、スキーマバージョン、言語、生成設定、入力コンテンツのfingerprintを含める。別バックエンドの結果を同一モデル名だけで再利用しない。認証秘密そのものはキーへ含めない。

## 導入順序

1. 共通型、backend registry、factoryを追加し、OpenAIとManusを挙動変更なしで移す。
2. CLI、`ingest`、usage・価格計算にあるbackend固有分岐を各アダプターまたはprofileへ移す。
3. `LocalAgentRunner`と`codex-local`をexperimental backendとして追加する。
4. 同じrunner上に`claude-code-local`を追加する。
5. 同一記事集合で品質、構造化出力成功率、所要時間、usage、安全性を比較する。
6. 十分な評価後も、既定backendの変更は別の仕様変更として判断する。

## 対象外

- CodexまたはClaude CodeをCloudflare Workers内で直接起動すること。
- LLMにページ取得、ファイル操作、shell実行を任せること。
- バックエンド間でプロンプトを自動最適化し、意味を変えること。
- サブスクリプション利用枠を無制限または無料の処理能力として扱うこと。
- experimental backendの失敗を有料APIへ暗黙にフォールバックすること。

## 検証と受け入れ条件

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

## 参考資料

- [Codexの非対話実行](https://developers.openai.com/codex/non-interactive-mode)
- [Codex App Server](https://developers.openai.com/codex/app-server)
- [Claude Code CLIリファレンス](https://code.claude.com/docs/en/cli-usage)
- [Claude Codeの非対話実行](https://code.claude.com/docs/en/headless)
