# LLMバックエンド実装のコードレビュー

ステータス: 完了
対象: `7e9d09e` feat: add LLM execution backends
仕様: [LLM実行バックエンド](../specs/20260816-llm-backends.ja.md)
レビュー者: Claude Code (2026-08-16)、Codex (2026-08-17)

## 結論

### Codex (2026-08-17)

再レビューの指摘24から27はすべて採用し、同一の修正commitで解消した。legacy fingerprint、canonical schema検証、Rich進捗テスト、Codex CLIのバージョン固定とtool denylistに加え、projectからのcwd隔離、CLI login preflight、共通エラー分類、監査requestの固定形式まで実装され、テストスイートは緑である。マージ可能である。

`codex-local`の一時cwdはVaultおよび既存Git projectの外へ移し、MCP設定も空で上書きする。安全な一時領域を確保できない環境では、記事本文を送る前に`BackendPolicyError`で拒否する。

### Claude Code (2026-08-16、2026-08-17更新)

当初の指摘1から3を解消するまでマージ不可とした。以降のやり取りで指摘1から27は解消済みである。

ただし実CLIでの再検証により、確定仕様の安全ポリシーのうち「user settingsとskillsを無視する」が未達であることが判明した（指摘28）。`codex-local`をどう扱うかを決めるまでステータスは`対応中`とする。`openai-responses`と`manus-api`だけを使う限りでは、この制約は関係しない。

## 指摘

### 1. 無関係なテストのアサーションを書き換えて壊している — 重大度: 高

**根拠:** `tests/test_progress.py:116`

**現象:** rich progressの最終表示を検証するテストのアサーションが、`assertGreaterEqual(output.count(...), 2)` から `assertEqual(output.count(...), 1)` へ書き換えられている。実際の出現回数は3であり、`AssertionError: 3 != 1` で失敗する。変更前のアサーションは対象コミットの親で通っていた。

**影響:** テストスイート全体が赤になる。またLLMバックエンドと無関係な領域の検証が、根拠なく弱い条件へ置き換わっている。

**提案:** 変更を元に戻す。

### 2. legacy fingerprintの互換パスが原理的に一致しない — 重大度: 高

**根拠:** `feedian/ingest.py:335`、`feedian/llm.py:47`、`feedian/store.py:770`

**現象:** 同じ変更の中で`tags.minItems`が`1`から`0`へ変更されている。このスキーマは`build_summary_request`を通じてrequestへ埋め込まれ、request全体がfingerprintのハッシュ対象である。したがって`_candidate`が算出する`legacy_fingerprint`は、新スキーマにlegacyな`provider`キーを足したものになり、実際にデータベースへ保存されている歴史的fingerprint（旧スキーマ＋`provider`キー）と一致しない。

```text
minItems now  : 0
legacy_fp(new): 240dfa32d1a36c9c
historical_fp : cf51fa03ad33a525
MATCH: False
```

**影響:** `successful_llm_result`のfingerprint version 1検索とversion 2への昇格処理が到達不能な死にコードになる。移行後の初回ingestで既存の再利用可能な結果が一件も見つからず、全記事がAPIへ再送出されて再課金される。仕様がAを最優先事項とした目的が失われる。

**提案:** legacy fingerprintを旧スキーマの凍結スナップショットから算出する。凍結スキーマを定数として明示すれば、以後スキーマを変更しても互換検索が壊れないことがコード上で読み取れる。あるいは`minItems`の変更自体を取りやめる（指摘4を参照）。

### 3. dry-runがデータベースへ書き込む — 重大度: 高

**根拠:** `feedian/store.py:773`、`feedian/cli.py:625`

**現象:** `successful_llm_result`はlegacy fingerprintがヒットした場合に`UPDATE llm_run`を発行する。この関数は`plan_source_notes`から`_candidate`経由で呼ばれ、`_ingest`のdry-run分岐は`vault_write_lock`を取得していない。画面表示は「Preview only | no API calls | no writes」である。

**影響:** 読み取りを名乗る関数が副作用を持ち、書き込みロックの外でデータベースを変更する。現状は指摘2の不具合により発火しないが、**指摘2を修正した瞬間に顕在化する**。

**提案:** 昇格処理を書き込み経路（`ingest_source_notes`側、ロック内）へ移す。あるいは`successful_llm_result`へ昇格の可否を渡す引数を設け、計画時は読み取りのみとする。

### 4. `minItems`の緩和に根拠がない — 重大度: 中

**根拠:** `feedian/llm.py:47`

**現象:** `tags.minItems`を`1`から`0`へ変更している。仕様にも仕様のレビューにも、タグ0個を許容する判断は記録されていない。`DESIGN.md`は「空の`tags`と`content_type`は許容する」と既成事実として記述している。

**影響:** 合意のない出力契約の緩和である。さらに副作用として指摘2を引き起こしている。

**提案:** 緩和が必要ならその理由を仕様側へ記録してから行う。不要なら元に戻す。

### 5. canonical schemaによる検証が実装されていない — 重大度: 中

**根拠:** `feedian/llm.py:33`、`feedian/llm.py:57`

**現象:** `CANONICAL_SUMMARY_SCHEMA`は`deepcopy`して`PROVIDER_OUTPUT_SCHEMA`という別名を作っただけで、検証を行うコードが存在しない。JSON Schema検証ライブラリへの依存も追加されていない。実際のゲートは従来どおり`normalize_summary_result`による切り詰めと型強制である。

**影響:** `DESIGN.md`の「プロバイダー出力は正規化後にcanonical summary schema version 1で検証する」が事実と異なる。仕様のレビューで合意した「providerへ要求するSchemaと保存可能なSchemaを分ける」うち、後者の検証側が実装されていない。

**提案:** 正規化後の検証を実装するか、実装しない判断であれば`DESIGN.md`の記述を実態へ合わせる。二つのスキーマが常に同一内容であるなら、分離した意味も併せて記述する。

### 6. エラー分類が仕様の6分類のうち3つ欠けている — 重大度: 中

**根拠:** `feedian/llm_backends.py:26-43`、`feedian/llm_backends.py:277`

**現象:** `BackendTimeoutError`、`BackendRateLimitError`、`BackendProtocolError`が定義されていない。ローカルバックエンドの`summarize`は`except Exception`で一括捕捉し、タイムアウトもJSON不正もレート制限も`BackendExecutionError`へ潰している。

**影響:** 呼び出し側が失敗の種類を区別できない。再試行の可否も、利用者への説明も分岐できない。

**提案:** 仕様の分類どおり例外クラスを追加し、`local_agent`側で`TimeoutExpired`とJSON解析失敗をそれぞれの型へ写像する。

### 7. プロセスツリー終了が未実装 — 重大度: 中

**根拠:** `feedian/local_agent.py:44`、`tests/test_llm_backends.py:132`

**現象:** `SubprocessRunner`は`subprocess.run(timeout=)`のみを使う。これは直接の子プロセスだけをkillし、孫プロセスは残る。タイムアウトのテストは例外を送出するだけのfake runnerを使っており、一時ディレクトリの削除しか検証していない。

**影響:** 仕様の「タイムアウトまたはキャンセル時に完全なプロセスツリーを終了する」を満たさない。ローカルエージェントは子プロセスを起こし得るため、放置されたプロセスがモデル推論を続ける可能性がある。

**提案:** POSIXでは`start_new_session=True`とプロセスグループへのシグナル、Windowsではjob objectまたは`taskkill /T`でツリーを終了する。契約テストは実プロセスを起こす必要があるため、fake runnerとは別のテストとして分ける。

### 8. `fallback`設定が受理されるだけで完全に無視される — 重大度: 中

**根拠:** `feedian/vault.py:296-350`、`feedian/llm_backends.py:27`

**現象:** `_parse_llm`は`llm.fallback.enabled`を検証し、有効ならbackendとmodelの両方を必須とするところまで実装している。しかし実行側にfallbackの配線が一切なく、`BackendError.fallback_eligible`もどこからも読まれない。

**影響:** 利用者が設定してもfallbackは起きない。「設定したのに何も起きない」状態は、未実装として拒否するより危険である。仕様がfallbackを課金事故の防止対象として扱っていることを踏まえると、無視される設定を受理すべきではない。

**提案:** 実装しない範囲であれば`enabled: true`を`ValueError`で拒否する。

### 9. 宣言されているが強制されていないcapability — 重大度: 中

**根拠:** `feedian/llm_backends.py:46-54`、`feedian/llm.py:371`

**現象:** `min_start_interval_seconds`（manus-apiで6.1）、`max_parallelism`、`usage_available`のいずれも、宣言されるだけでどこからも読まれない。manusの実際のレート制御は依然として`llm.py`の`_wait_for_manus_create_slot`が持っている。

**影響:** 仕様の「capabilityは表示や非公式な助言ではなく、実行前に強制する」に反する。実際の制御と宣言が二重管理になり、片方だけ変更される余地が生まれる。

**提案:** 少なくとも`min_start_interval_seconds`は共通層で強制し、`llm.py`側の間隔制御を置き換える。強制しない値は当面capabilityから外す。

### 10. モデル互換チェックが実行前ではなく記事ごとに走る — 重大度: 中

**根拠:** `feedian/llm_backends.py:141-148`

**現象:** backendとmodelの組み合わせ検証が`summarize`の内部にある。`preflight`にもCLIにも検証がない。

**影響:** `--backend manus-api --model gpt-5.6-terra`のような組み合わせは、プラン表示を経て実行へ入り、記事ごとに失敗して失敗した`llm_run`行を量産する。仕様の受け入れ基準「不正な組み合わせは実行前に失敗する」を満たさない。また設定エラーに対して`BackendUnavailableError`（CLI不在またはサービス到達不可を表す分類）を使っており、分類として不適切である。

**提案:** 検証を`preflight`へ移し、分類を設定エラー相当のものへ改める。

### 11. ローカルバックエンドの監査に実効リクエストが残らない — 重大度: 中

**根拠:** `feedian/ingest.py:210`、`feedian/llm_backends.py:281`

**現象:** 計画時に保存するrequestも、実行後に保存するrequestも`{"mode": "stdin", "schema": "canonical-summary-v1"}`の固定値である。実際に渡したargvが記録されない。

**影響:** 仕様が要求する「秘密を除いた実効リクエストまたはプロセス引数」を満たさない。どのCLIフラグで実行されたかを後から監査できない。

**提案:** argvを監査へ保存する。本文をargvへ載せない設計になっているため、argvはそのまま記録して差し支えない。

### 12. 汎用ランナーがCodex固有のパーサを直接呼ぶ — 重大度: 低

**根拠:** `feedian/local_agent.py:107`、`feedian/local_agent.py:132`

**現象:** `run_isolated_local_agent`が`_parse_codex_jsonl`を直接呼んでいる。また`command`コールバックへ渡す`final_path`は、どのコマンド構築でも使われず、ファイルも読まれない。

**影響:** 仕様の「コマンド構築、セキュリティフラグ、イベント形式、usage解析はバックエンド固有」に反する。Claude Code用アダプタを追加する際に、この層を必ず作り直すことになる。

**提案:** パーサをコールバックとして受け取る。使われない`final_path`は削除する。

### 13. `codex-local`はCLIから選択すると必ず失敗する — 重大度: 低

**根拠:** `feedian/llm_backends.py:206`、`feedian/llm_backends.py:341`

**現象:** `get_backend`は`policy_verified=False`で生成するため、`preflight`が常に`BackendPolicyError`を送出する。`shutil.which`によるCLI存在確認はpolicy確認の後段にあり到達不能で、`command()`ビルダーも実プロセスからは到達しない。

**影響:** 意図は`DESIGN.md`に記述されており、隔離を証明できない以上は実行しないという判断自体は妥当である。ただし`--backend`の選択肢には表示され、選ぶと必ず失敗する。`claude-code-local`は「予約」として扱われているのに対し、扱いが不揃いである。

**提案:** 両者の扱いを揃える。選択肢に残すなら、選択時のエラーメッセージで予約状態であることを示す。

### 14. `sanitize_error`は完全一致置換のみ — 重大度: 低

**根拠:** `feedian/local_agent.py:120`

**現象:** `private_values`はプロンプト全文と本文全文の完全一致置換である。本文の断片がエラーメッセージへ混入した場合は素通りする。

**提案:** 完全一致で防げる範囲を前提とするなら、その限界をコメントとして残す。

### 15. 画面表示が`Provider`のまま — 重大度: 低

**根拠:** `feedian/cli.py:629`

**現象:** `print_ingest_plan(plan, provider=backend_id, ...)`と呼んでおり、引数名も画面のラベルも`Provider`である。

**影響:** 仕様の「新しいLLM設定と出力はbackendを使う」を満たさない。`provider`は収集元の意味でも使われているため、混同が残る。

### 16. `temporary_parent`のパス直書き — 重大度: 低

**根拠:** `feedian/ingest.py:231`

**現象:** `Path(vault_root).resolve() / ".feedian" / "tmp"`と直接組み立てている。`vault_paths(root).state_dir`が同じ値を返す。

### 17. `DESIGN.md`の記述誤り — 重大度: 低

**根拠:** `DESIGN.md`

**現象:** 「Vault設定はformat version 2で`llm.backend`、`llm.model`、`llm.auth_mode`を持つ」とあるが、`_parse_llm`が許可するのは`backend`、`model`、`fallback`のみである。`auth_mode`は設定項目ではない。

## 良い点

- v5からv6へのマイグレーションのbackfillが、`finish_llm_run`によって`request_json`が実ペイロードへ差し替わる事実を踏まえ、`$.provider`、`$.agent_profile`、`$.input`と`$.text`の三系統で判別している。既存データの実態を確認した上で書かれている。
- `fingerprint_version`を独立した列として持ち、旧キーはversion 1でのみ検索する構造自体は正しい。指摘2さえ解消すれば設計どおり機能する。
- Vault configのformat versionを上げ、未知フィールドの拒否を維持したまま`llm`を追加した点は、仕様レビューEへの正しい対応である。
- untrusted wrappingを`build_untrusted_message`としてManus専用から共通層へ格上げし、指示、本文、再掲の順序をテストで検証している点は、仕様レビューMへの正しい対応である。
- untrusted本文をargvへ載せない設計と、それを検証するテストが揃っている。

## 採否

対応はすべてこの文書を含むコミットで行った。

| # | 指摘 | 重大度 | 採否 | 対応 |
| --- | --- | --- | --- | --- |
| 1 | 無関係なテストの破壊 | 高 | 採用 | アサーションを元に戻した |
| 2 | legacy fingerprintの不一致 | 高 | 修正して採用 | 凍結スナップショットではなく`minItems`変更の取りやめで解消した。加えて旧キーを実測値で固定する回帰テストを追加した |
| 3 | dry-runの書き込み | 高 | 採用 | `successful_llm_result`を読み取り専用に戻し、昇格を`promote_legacy_fingerprint`として書き込み経路へ移した |
| 4 | `minItems`緩和の根拠不足 | 中 | 採用 | 元に戻した。指摘2の修正と同一である |
| 5 | canonical schema検証の未実装 | 中 | 修正して採用 | 検証は実装せず、`DESIGN.md`の記述を現在の挙動へ合わせた。検証を実装するか否かは未決である |
| 6 | エラー分類の欠落 | 中 | 保留 | 分類を増やしても現在の呼び出し側は分岐しない。再試行方針を決める作業と同時に扱う |
| 7 | プロセスツリー終了の未実装 | 中 | 保留 | `codex-local`は指摘13のとおり実プロセスを起動できない。実行を解禁する作業と同時に扱う |
| 8 | 無視される`fallback`設定 | 中 | 保留 | 拒否と実装のどちらを選ぶかは利用者に見える挙動の決定であり、単独で判断すべきである |
| 9 | 強制されないcapability | 中 | 保留 | 指摘8と同じ単位で扱う |
| 10 | モデル互換チェックの位置 | 中 | 保留 | 失敗はするので課金事故には至らない。指摘6の分類整理と同時に`preflight`へ移す |
| 11 | ローカル監査の実効リクエスト欠落 | 中 | 保留 | 指摘13と同時に扱う |
| 12 | 汎用ランナーの層の混在 | 低 | 保留 | `claude-code-local`のアダプタを書く時点で必ず触るため、その作業に含める |
| 13 | `codex-local`の扱い | 低 | 保留 | 実行を解禁するか予約扱いへ揃えるかは仕様側の判断である |
| 14 | `sanitize_error`の限界 | 低 | 不採用 | 完全一致で防げる範囲を前提とする設計は妥当である。部分一致による秘匿は誤検知でエラーを読めなくする副作用が大きい |
| 15 | 画面表示の`Provider` | 低 | 保留 | `cli_ui`の表示変更は他コマンドの表示と揃えて行う |
| 16 | `temporary_parent`の直書き | 低 | 保留 | 指摘13と同時に扱う |
| 17 | `DESIGN.md`の記述誤り | 低 | 採用 | `llm.auth_mode`を`llm.fallback`へ訂正した |

保留とした指摘は、いずれも`codex-local`の実行解禁、fallbackの挙動決定、エラー分類と再試行方針という三つの単位へ収束する。個別に着手するより、その単位で仕様を決めてから実装する方が手戻りが少ない。

## 検証

- [x] テストスイートが緑である（225 passed）。
- [x] 対象コミットの親の時点の実装が算出する再利用キーと、現在の実装が算出する旧キーが一致する。`2385ec2`の`build_summary_request`を実際に読み込んで照合し、その値を`tests/test_ingest.py`へ固定した。
- [x] 計画は旧キーの行を書き換えない。`test_legacy_fingerprint_is_reused_and_promoted_without_an_api_call`が、計画後は`fingerprint_version`が1のままで、ingest後に2へ昇格することを検証する。

## 規約化した項目

なし。ただし指摘1と同種の事象が再発した場合は、「レビュー対象と無関係なテストのアサーションを変更しない」を`AGENTS.md`へ規約として追加する。

## 補足

指摘2は、既存のテスト`test_legacy_fingerprint_is_reused_and_promoted_without_an_api_call`では原理的に検出できなかった。このテストは旧キーを現在の実装から算出して保存し、同じ実装で検索するため、スキーマが変わっても両者が同時に変わって一致し続ける。移行の互換性を検証するテストは、**旧実装が実際に書いた値**を基準に持つ必要がある。

## Codexによる再確認（2026-08-17）

再確認者: Codex

Claude Codeによる指摘2と3、およびそれに対するlegacy fingerprintの実測値固定とdry-runからの昇格処理分離は妥当である。一方、現在のブランチをWindows環境で再検証したところ、レビューの検証結果を再現できず、確定仕様との不一致も残っていた。このため、ステータスを`完了`から`対応中`へ戻す。

### 18. Rich進捗表示のテストが環境依存で再び失敗する — 重大度: 高

**根拠:** `tests/test_progress.py:116`、本書の指摘1および検証結果

**現象:** Claude Codeの環境では`process: syncing items`の出現回数が3であったため、アサーションを`2回以上`へ戻して225件成功としている。しかしWindows、Rich 15.0.0の現在環境では出現回数は1であり、`AssertionError: 1 not greater than or equal to 2`で失敗する。再確認時の全体結果は`224 passed, 1 failed`である。

**影響:** レビュー文書の「テストスイートが緑」という検証結果を再現できず、ブランチはマージ可能な状態ではない。また、`2回以上`と`1回だけ`のどちらの生出力回数もRichの端末描画方式へ依存し、移植可能な契約にならない。

**提案:** `StringIO`へ書かれたライブ描画の回数を数えず、最終状態に説明文と`2/2`が含まれることを検証する。静的コピーを厳密に1回呼ぶ契約が必要なら、`Console.print`または静的表示を行う境界をfakeまたはspyで検証する。

### 19. 空のtagsに関する指摘4の前提が確定仕様と矛盾する — 重大度: 高

**根拠:** `docs/specs/20260816-llm-backends.ja.md:38`、同`:450`、`feedian/llm.py:33-59`、本書の指摘4

**現象:** 指摘4は「空の`tags`を許容する判断は仕様に記録されていない」としているが、確定仕様の最終案は空の`tags`と`content_type`を許容すると明記し、詳細でも現行の救済範囲をschema version 1で維持すると決定している。現在は`CANONICAL_SUMMARY_SCHEMA`の`tags.minItems`が1へ戻され、そのdeep copyが`PROVIDER_OUTPUT_SCHEMA`になっている一方、`normalize_summary_result`は空の`tags`を引き続き受理する。

**影響:** `CANONICAL_SUMMARY_SCHEMA`という名前、確定仕様、実際の保存可否が一致しない。将来canonical schema検証を追加すると、現在救済している空の`tags`が突然失敗する。また、legacy fingerprint互換性を守るためにcanonical契約を変更するという、本来独立している判断が再び結合している。

**提案:** canonical schemaは確定仕様どおり空の`tags`を許容する。providerへ要求するschemaは必要なら1件以上を維持し、両者を別オブジェクトとして定義する。legacy fingerprintは旧provider schemaの凍結スナップショットから算出し、今後のschema変更から隔離する。

### 20. canonical schema検証の未実装は確定仕様の未達である — 重大度: 中

**根拠:** `docs/specs/20260816-llm-backends.ja.md:40`、同`:129`、`feedian/llm.py:92-113`、本書の採否5

**現象:** 採否5はcanonical schema検証を実装せず、`DESIGN.md`を現在の挙動へ合わせている。しかし確定仕様は「許容された正規化の後にcanonical schema検証を行うこと」と「検証失敗時にsource noteと成功runを作成しないこと」を受け入れ条件としている。現在のゲートは従来の`normalize_summary_result`だけである。

**影響:** 実装と`DESIGN.md`は互いに整合したが、確定仕様の受け入れ条件は満たしていない。検証を実装するか否かを未決のままレビューだけ完了にはできない。

**提案:** 正規化結果に対するcanonical schema検証を実装する。方針を変更する場合は、確定仕様を直接書き換えず、このレビューで仕様からの逸脱理由と人間の決定を明示する。

### 21. レビュー対象が修正コミットの直前ではない — 重大度: 低

**根拠:** `AGENTS.md:113-129`、本書`:4`、git履歴`7e9d09e -> 5ed00ab -> 5b9cd88`

**現象:** 本書の対象は`7e9d09e`だが、本書と修正を含む`5b9cd88`の親はレビュー運用規約を追加した`5ed00ab`である。新しいライフサイクルが意図する「対象を修正コミットの一つ前に保つ」形ではない。

**影響:** 今回は中間コミットが実装を変更していないためレビュー対象自体は特定できるが、将来のレビューで差分範囲を機械的に決めにくくなる。

**提案:** 公開済み履歴の書き換えは行わず、今回は規約導入とレビュー作業が同時進行した例外として記録する。以後はレビュー対象コミットの直後に修正とレビュー文書をまとめる。

## Codex再確認の採否

| # | 指摘 | 重大度 | 採否 | 理由 |
| --- | --- | --- | --- | --- |
| 18 | Richテストの環境依存 | 高 | 採用 | 現在環境で全体テストの失敗を再現した。端末の生描画回数を契約にしないテストへ変更する必要がある |
| 19 | 空のtagsに関する仕様との矛盾 | 高 | 採用 | 確定仕様の最終案と詳細の両方に許容判断が明記されており、指摘4の前提が誤っている |
| 20 | canonical schema検証の未実装 | 中 | 採用 | DESIGNの記述変更だけでは確定仕様の受け入れ条件を満たさない |
| 21 | レビュー対象の親関係 | 低 | 修正して採用 | 履歴は書き換えず規約導入時の例外として記録し、次回以降に規約を適用する |

## Codex再確認の検証

- [x] `successful_llm_result`は読み取り専用であり、legacy fingerprintの昇格は`ingest_source_notes`の書き込み経路から行われる。
- [x] 旧実装`2385ec2`が生成したfingerprintの実測値を固定する回帰テストが存在する。
- [ ] テストスイートは緑ではない。Windows、Rich 15.0.0で`tests/test_progress.py:116`が失敗し、`224 passed, 1 failed`である。
- [ ] canonical schemaとprovider output schemaは、空の`tags`に関する異なる契約を表現できていない。
- [ ] 正規化後のcanonical schema検証は実装されていない。

## Claude Codeによる再対応（2026-08-17）

対応者: Claude Code

Codexの指摘18から21を検証した。19と20は確定仕様を読み直して確認し、いずれも正しい。**本書の指摘4は事実誤認である。**

### 指摘4の訂正

指摘4は「タグ0個を許容する判断は仕様に記録されていない」としたが、確定仕様`20260816-llm-backends.ja.md:38`は「`tags` と `content_type` は空を許容する。現行データの結果再利用を優先するためである」と明記している。同`:39`はproviderへ送るschemaとFeedian内部で検証するschemaの分離も定めている。

原因は、レビュー時に確定版ではなく確定前の草案`docs/specs/llm-backends.md`を参照し、確定版を通読しなかったことである。**確定仕様が存在する実装のレビューでは、確定版を根拠として引用する。** 指摘4の本文は記録として残し、ここで訂正する。

この誤認により、指摘2の修正で`minItems`を1へ戻した際にcanonical schemaまで巻き添えにし、Codexが指摘するとおり互換性の都合と出力契約の判断を再び結合させていた。

### 対応

- **19**: `CANONICAL_SUMMARY_SCHEMA`は仕様どおり空の`tags`を許容する（`minItems: 0`）。`PROVIDER_OUTPUT_SCHEMA`は別オブジェクトとして1個以上を要求する。両者が別契約であることを`tests/test_llm_backends.py`で固定した。
- **19（凍結スナップショット）**: legacy fingerprintは`LEGACY_V1_PROVIDER_SCHEMA`から組み立てる。`PROVIDER_OUTPUT_SCHEMA`を変更しても旧キーが変わらないことをテストで検証している。プロンプト変更による再利用の失効は`PROMPT_VERSION`が担うため、凍結対象はschemaに限定した。
- **20**: `validate_canonical_summary`を実装し、`normalize_summary_result`の最後から呼ぶ。全バックエンドがこの関数を通るため、検証を迂回する経路はない。JSON Schemaライブラリは追加せず、この形状に必要な範囲を実装した。
- **18**: 描画回数の計数をやめ、最終状態が完了表示で終わることを検証する形へ変更した。あわせて`COLUMNS`を固定した。
- **21**: 履歴は書き換えず、規約導入とレビューが同時進行した例外として記録する。Codexの提案どおりである。

### 指摘18について

Codexが報告した`224 passed, 1 failed`は、同一のWindows環境かつ同一のRich 15.0.0で**再現しなかった**（`9 passed`）。原因を調べたところ、`retain_final`はliveをtransientにしてから静的コピーを1回印字するため、出力に現れる回数はRichのリフレッシュ回数、すなわち実行時間に依存する。したがって`2回以上`も`1回だけ`も競合状態であり、どちらの環境が正しいかという問題ではない。Codexの指摘は再現の有無にかかわらず正しい。

なお`assertIn("2/2", ...)`側も端末幅に依存しており、`COLUMNS=40`では失敗する。

### 22. rich進捗テスト群が端末幅に依存する — 重大度: 低

**根拠:** `tests/test_progress.py`

**現象:** 指摘18の修正後も、`COLUMNS=40`では`tests/test_progress.py`の5件が失敗する。修正前は6件であり、幅依存は指摘18のテストに限らない。

**影響:** 狭い端末やCIランナーの既定幅次第で、LLMバックエンドと無関係なテストが落ちる。

**提案:** 幅に依存する検証を持つテストへ`COLUMNS`の固定を広げる。

## Codex再確認への採否

| # | 指摘 | 重大度 | 採否 | 対応 |
| --- | --- | --- | --- | --- |
| 18 | Richテストの環境依存 | 高 | 採用 | 描画回数の計数をやめ、`COLUMNS`を固定した。報告された失敗は再現しなかったが、指摘自体は正しい |
| 19 | 空のtagsに関する仕様との矛盾 | 高 | 採用 | 二つのschemaを分離し、legacy fingerprintを凍結スナップショットから算出する。指摘4を訂正した |
| 20 | canonical schema検証の未実装 | 中 | 採用 | `validate_canonical_summary`を実装した |
| 21 | レビュー対象の親関係 | 低 | 採用 | 例外として記録する。履歴は書き換えない |
| 22 | rich進捗テスト群の幅依存 | 低 | 保留 | 指摘18の範囲外で、修正前から存在する。表示系の整理としてまとめて扱う |

## 再対応後の検証

- [x] テストスイートが緑である（228 passed）。
- [x] `CANONICAL_SUMMARY_SCHEMA`は空の`tags`を受理し、`PROVIDER_OUTPUT_SCHEMA`は1個以上を要求する。
- [x] `PROVIDER_OUTPUT_SCHEMA`を変更してもlegacy fingerprintは変化しない。
- [x] 正規化後のcanonical schema検証が実装され、必須欠落、型不一致、要素数超過、未知フィールドを拒否する。
- [x] 旧実装`2385ec2`が生成したfingerprintの実測値を固定する回帰テストが存在する。
- [ ] `COLUMNS=40`では`tests/test_progress.py`の5件が失敗する（指摘22、保留）。

## 規約化した項目（更新）

- 「確定仕様が存在する実装のレビューでは、確定版を根拠として引用する。確定前の草案を根拠にしない」を`AGENTS.md`のレビュー節へ追加した。指摘4の誤認が理由である。

## codex-local実行解禁の検証（2026-08-17）

検証者: Claude Code

保留としていた指摘のうち、`codex-local`の実行可否に連動するものを解消するため、実物のCodex CLI（codex-cli 0.147.0、Windows）で隔離を測定した。

### 測定内容

一時ディレクトリ外にカナリアファイルを置き、その内容を要約へ含めるよう指示して4回実行した。

| 条件 | 指示の与え方 | 結果 |
| --- | --- | --- |
| 実装時のフラグのみ | untrusted本文に注入 | 漏洩せず。ただしモデルが「untrustedな指示」として拒否しただけである |
| 実装時のフラグのみ | 信頼された指示として直接 | **漏洩した。** エージェントが`pwsh.exe`を起動してファイルを読み、内容が`summary`に入った |
| ＋22機能の`--disable` | 信頼された指示として直接 | `CANNOT-READ-FILES`。ツール自体が存在しない |
| ＋22機能の`--disable` | untrusted本文に注入 | 漏洩せず |

**結論として、Codexが`policy_verified=False`とした判断は正しかった。** `--sandbox read-only`は書き込みを禁じるだけで読み取りは許可する仕様であり、`--ignore-user-config`と`--ignore-rules`もツールを落とさない。denylistを足すと実際に塞がる。

### 測定で分かった付随事項

- `--ignore-user-config`を付けても**skillsとpluginsは読み込まれる**。実行時に「Skill descriptions were shortened to fit the skills context budget」というエラーイベントが出た。仕様が要求する「skills・pluginsを無視する」は、このフラグでは満たせず、`--disable`が必要である。
- 入力トークンは短い記事1件で18,104。`--disable`適用後で12,417。ツール定義とスキルの文脈が大半を占める。サブスク課金なので金額は出ないが、レート面では無視できない。
- Codex CLIには全ツールの一括無効化スイッチが無い。したがって隔離はdenylistであり、**将来のリリースが既定有効のツールを追加すると無言で穴が開く**。バージョン固定が必須である。
- `--output-schema`は`maxLength`を単語の途中で切り詰める。正規化と検証は通るが、要点が途中で切れた文になり得る。

### 対応

- `CODEX_DISABLED_FEATURES`として22機能を無効化する。根拠を上記測定へのコメントとしてコードに残した。
- `preflight`が`codex --version`を1回検出し、`CODEX_VERIFIED_VERSIONS`（現在`0.147.0`のみ）以外は記事送信前に拒否する。バージョンと無効化機能名は監査へ残す。
- `policy_verified`引数は削除した。「テストで真にできるフラグ」ではなく、実測したバージョンかどうかで決まる。

### 23. Windowsではnpmシムのため実行ファイル名の解決が必要 — 重大度: 中

**根拠:** `feedian/llm_backends.py`、実行時の`WinError 2`

**現象:** `shutil.which("codex")`は`codex.CMD`を返すが、`subprocess`へ`"codex"`をそのまま渡すと`CreateProcess`がPATHEXTを適用せず`FileNotFoundError`になる。エンドツーエンド実行で発覚した。

**対応:** 採用。`_resolve_executable`でPATH解決を1回行い、バージョン検出と本実行の両方で解決済みパスを使う。

## 残る保留指摘への対応

| # | 指摘 | 採否 | 対応 |
| --- | --- | --- | --- |
| 6 | エラー分類の欠落 | 採用 | `BackendTimeoutError`、`BackendRateLimitError`、`BackendProtocolError`を追加。タイムアウトとJSON不正をそれぞれの型へ写像した |
| 7 | プロセスツリー終了の未実装 | 採用 | `Popen`＋`taskkill /T`（Windows）、`killpg`（POSIX）で実装した |
| 8 | 無視される`fallback`設定 | 修正して採用 | `enabled: true`を`ValueError`で拒否する。実装はcodex-localの運用実績を見てから判断する |
| 9 | 強制されないcapability | 修正して採用 | `min_start_interval_seconds`を共通層で強制する仕組みを実装した。ただしManusの値は0とした。Manusのペーシングは`llm.py`にあり、対象外のlegacy export経路も共有しているため、ここで宣言すると二重に待つ |
| 10 | モデル互換チェックの位置 | 採用 | `supports_model`をbackendの契約へ加え、`ingest_source_notes`がrunを開こうとする前に判定する |
| 11 | ローカル監査の実効リクエスト欠落 | 採用 | argvを監査へ保存する。本文がargvへ載らないことをテストで検証している |
| 12 | 汎用ランナーの層の混在 | 採用 | パーサをコールバックにし、Codex固有の`parse_codex_events`をアダプタ側へ移した。未使用の`final_path`は削除した |
| 13 | `codex-local`の扱い | 採用 | 実行を解禁した。バージョン検証で塞ぐ形に変えた |
| 15 | 画面表示の`Provider` | 採用 | `Backend`へ変更した |
| 16 | `temporary_parent`の直書き | 採用 | `vault_paths(root).state_dir`を使う |
| 22 | rich進捗テスト群の幅依存 | 採用 | `setUp`で`COLUMNS`を固定し、幅40・80・200で全件通ることを確認した |

`max_parallelism`と`usage_available`は宣言のみのままである。ingestは逐次実行であり、前者は現状1で自明に満たされる。並列化を行う時点で強制対象へ加える。

## 実行解禁後の検証

- [x] テストスイートが緑である。
- [x] 実物のCLIに対するエンドツーエンド実行が成功する。`preflight`がバージョン`0.147.0`を検出し、prompt injectionを含む記事から正常な要約を生成し、カナリアは漏れない。
- [x] argvは58要素が監査へ残り、うち`--disable`が22組ある。記事本文はargvに含まれない。
- [x] 一時ディレクトリは実行後に残らない。
- [ ] タイムアウト時のプロセスツリー終了は、実プロセスによる検証を行っていない。fake runnerによる契約テストのみである。

## Codex再レビュー（2026-08-17）

レビュー者: Codex

再レビュー対象: `6d6fcb1` feat: run codex-local against a measured Codex CLI lockdown

Claude Codeの再対応で、指摘18から23に直接対応する修正とRich進捗テストの幅依存解消を確認した。その上で、確定仕様と現在の実装を再度突き合わせ、以下の未解消項目を確認した。

### 24. Vault配下のcwdでプロジェクト指示とMCP設定を隔離できない — 重大度: 高

**根拠:** `feedian/ingest.py:260`、`feedian/local_agent.py:122-145`、`feedian/llm_backends.py:343-355`、`docs/specs/20260816-llm-backends.ja.md:58-65`

**現象:** 実行cwdはVault内の`.feedian/tmp/llm-*`である。Codexの公式仕様では、[プロジェクトrootからcwdまでの`AGENTS.md`を指示チェーンに含める](https://developers.openai.com/codex/guides/agents-md)ほか、[trusted projectの`.codex/config.toml`もプロジェクト設定として読み込む](https://developers.openai.com/codex/config-basic)。`--ignore-user-config`はuser configを、`--ignore-rules`はexec policyのrulesを対象とするため、これらはプロジェクト指示とproject configを無効化する根拠にならない。現在のdisable listはpluginsやMCP elicitationを含むが、project configから定義される一般のMCP serverを明示的に空にしていない。

**影響:** Git管理されたObsidian Vaultなど、Vault自体がCodex projectと認識される構成では、Feedianの固定prompt以外のプロジェクト指示が混入する。project configのMCP serverが有効なら、untrusted記事からMCPへ到達し得る。ファイルcanaryだけでは、確定仕様の「project rulesを読まない」と「MCPを利用できない」を実証したことにならない。

**提案:** ローカルagentのcwdをVaultおよび既存project rootの外にあるFeedian専用rootへ移す。その上で、canary `AGENTS.md`とproject `.codex/config.toml`に定義したfake MCP serverが読まれないことをopt-in統合テストで確認する。CLI単体で実証できなければ、確定仕様どおり`codex-local`を`BackendPolicyError`で事前拒否する。

### 25. CodexのpreflightがCLI login状態を検証しない — 重大度: 中

**根拠:** `feedian/llm_backends.py:275-306`、`feedian/ingest.py:193-195`、`docs/specs/20260816-llm-backends.ja.md:48-52`

**現象:** `CodexLocalBackend.preflight`が確認するのはCLIの存在とversionだけで、`codex login status`相当の認証確認を行わない。未ログインまたは期限切れの場合もpreflightを通過し、記事promptをstdinで`codex exec`へ渡した後に失敗する。

**影響:** 確定仕様の「CLI login不足は実行前に`BackendAuthError`」を満たさず、バッチの先頭で認証問題を明示できない。

**提案:** ingest runに1回のpreflightで、同じ実行ファイルによる`codex login status`を確認する。未ログインと失効は`BackendAuthError`にし、記事ごとのrunを開く前に停止する。

### 26. 共通エラー分類がAPIとCodexの実失敗経路へ接続されていない — 重大度: 中

**根拠:** `feedian/llm_backends.py:38-63`、`feedian/llm_backends.py:168-186`、`feedian/llm_backends.py:358-374`、`feedian/local_agent.py:146-147`、`feedian/llm.py:257-267`

**現象:** `BackendRateLimitError`は定義以外に一度も使われていない。OpenAIのHTTP 429は`llm.py`で`RuntimeError`に変換され、`ApiBackend`がさらに`BackendExecutionError`へまとめる。Codexも、利用枠到達や認証失敗でprocessが非0終了するとstderrを分類せず`RuntimeError`から`BackendExecutionError`になる。実装された対応は、Codexのprocess timeoutとfinal JSON不正の写像に限られる。

**影響:** 本書の指摘6は「採用」とされているが、利用枠、認証、到達不能を呼び出し側が依然区別できない。後続のfallback判断やretry方針の根拠として使えない。

**提案:** transport固有の失敗をadapter境界で共通例外へ写像する。少なくともAPIの401/403、429、network不通、Codex CLIの未認証と利用枠到達に回帰テストを追加する。

### 27. `request_json`の意味がローカルrunの成否で変わる — 重大度: 中

**根拠:** `feedian/ingest.py:229-238`、`feedian/ingest.py:265-299`、`feedian/store.py:881-915`、`tests/test_ingest.py:244-267`、`docs/specs/20260816-llm-backends.ja.md:480-482`

**現象:** run開始時は`candidate.request`を`request_json`へ入れるため、記事本文を含む論理requestが保存される。成功時はその同じ列をCodexのargvを含む実requestで上書きするが、失敗時は上書きしない。この状態依存の振る舞いはテストでも明示的に固定されている。

**影響:** 同じ`request_json`が、成功runでは「本文を含まない実argv」、失敗runでは「本文を含む論理request」になる。確定仕様の「論理requestとsecret除去済み実requestを保持する」をどちらの状態でも満たさず、監査コンシューマがstatusによって別のJSON shapeを解釈しなければならない。

**提案:** logical requestとactual requestの格納先を分けるか、`request_json`内を`{"logical": ..., "actual": ...}`の固定shapeにする。失敗時もactual requestとして、実際に起動した場合はサニタイズ済みargv、起動前失敗なら`null`を保存する。

## Codex再レビューの検証

- [x] `.venv\Scripts\python.exe -m pytest -q`: 229 passed。
- [x] `COLUMNS=40`、`80`、`200`のそれぞれで`tests/test_progress.py`: 9 passed。
- [x] Ruffと`git diff --check`が成功する。
- [x] `BackendRateLimitError`の使用箇所はクラス定義1箇所だけである。
- [x] Codex公式ドキュメントで、project rootからcwdまでの`AGENTS.md`の読み込みと、trusted projectの`.codex/config.toml`の読み込みを確認した。
- [ ] project `AGENTS.md`、project MCP、networkのcanaryを使った実CLIテストは行われていない。
- [ ] タイムアウト時の実プロセスツリー終了テストは引き続き未実施である。

## Codex再レビューの採否

| # | 指摘 | 採否 | 対応 |
| --- | --- | --- | --- |
| 24 | Vault配下のcwdでproject指示とMCP設定を隔離できない | 採用 | system tempがVaultまたはGit project内でないことを事前検証し、記事ごとのcwdをそこへ作る。`mcp_servers={}`もCLI overrideとして渡す |
| 25 | CodexのpreflightがCLI loginを検証しない | 採用 | `codex login status`をingest runごとに1回実行し、未ログインを`BackendAuthError`にする |
| 26 | 共通エラー分類が実失敗経路へ接続されていない | 採用 | APIの401/403、429、network・5xx、protocol不正と、Codex CLIの認証、利用枠、到達不能を共通例外へ写像する |
| 27 | `request_json`の意味がrunの成否で変わる | 採用 | 新規runは常に`{"logical": ..., "actual": ...}`を保存し、実requestを取得できない失敗は`actual: null`、local CLI起動後の失敗ではサニタイズ済みactual requestを残す |

## Codexによる修正後の検証（2026-08-17）

- [x] 全体テスト: 239 passed、既存のDeprecationWarning 2件のみ。
- [x] `COLUMNS=40`、`80`、`200`のそれぞれで`tests/test_progress.py`: 9 passed。
- [x] Ruffと`git diff --check`が成功する。
- [x] 実物のCodex CLI 0.147.0で、version検出と`codex login status`を含むpreflightが成功する。
- [x] 実物のCodex CLI 0.147.0が`mcp_servers={}`のconfig overrideを受理する。
- [x] Vaultに`.git`と`AGENTS.md`が存在しても、local backendへ渡す一時parentがVault外になることを契約テストで確認する。
- [x] 未ログイン、API 401/403、API 429、network・5xx、protocol不正、Codex利用枠到達の分類を回帰テストで確認する。
- [x] 成功runと失敗runの両方で、監査requestが`logical`と`actual`の固定shapeになる。
- [ ] タイムアウト時の実プロセスツリー終了テストは未実施のままである。実装はfake runnerの契約テストで検証し、既存の保留事項として維持する。

## Claude Codeによる再レビュー（2026-08-17）

レビュー者: Claude Code

指摘24から27の修正を、実物のCodex CLI 0.147.0で検証した。24のMCP隔離、25のlogin確認、26の分類接続、27の監査envelopeはいずれも意図どおり動作する。ただし**24の「user設定を無視する」は達成できていない**ことを実測で確認したため、指摘28として記録する。

### 検証できたこと

- `codex login status`は存在し、ログイン済みで終了コード0を返す。`_verify_login`の前提は正しい。
- `--config mcp_servers={}`は実CLIに受理される。出荷フラグでの実行で、エージェントは「MCP servers — none」と報告した。**指摘24のMCP部分は解消している。**
- 一時cwdの親（`%TEMP%`）に`AGENTS.md`カナリアを置いて出荷経路で実行したところ、`content_type`は`note`であり、カナリアの指示は反映されなかった。**projectのAGENTS.md chainからは切り離せている。**
- `ruff check`は通る。テストは239件成功。
- `request_json`を読む箇所はv5→v6マイグレーション（既存行のみ対象）とテストだけであり、`logical`/`actual` envelopeの導入で壊れる読み手はいない。

### 28. グローバルの`AGENTS.md`とskillsカタログがエージェントへ届く — 重大度: 高

**根拠:** 実CLI 0.147.0を出荷フラグで実行した結果、`~/.codex/AGENTS.md`、`docs/specs/20260816-llm-backends.ja.md`の安全ポリシー

**現象:** 出荷される全フラグ（`--ephemeral --ignore-user-config --ignore-rules --config mcp_servers={}` と22機能の`--disable`）を付けた上で、受け取った指示の出所を列挙させたところ、次を報告した。

```text
User-provided AGENTS.md — "# グローバル作業指示"
Skills catalog — "## Skills"
MCP servers — none.
Memories — none.
```

`--ignore-user-config`が対象とするのは`$CODEX_HOME/config.toml`だけであり、**同じディレクトリの`AGENTS.md`は読み込まれ続ける**。`--disable skill_search`は検索ツールを落とすが、skillsカタログ自体は文脈に残る。入力が記事1件あたり約12,000トークンある理由もこれである。

**影響:** 確定仕様の安全ポリシーは「user settings、project rules、skills、plugins、hooks、automatic memoryを無視する」と定める。MCPとproject rulesは解消したが、**userのグローバル指示とskillsカタログは残っている**。結果として二つの問題がある。

1. Feedianの固定promptだけが要約を決めるわけではなくなり、利用者個人の作業指示が要約の内容や文体に影響し得る。同じ記事でも環境によって結果が変わる。
2. グローバル指示の中身が要約へ混入する経路が残る。untrustedな記事本文と同じ文脈に個人の指示が同居しており、prompt injectionがそれを引き出す標的になり得る。

**提案:** 三択であり、いずれも利用者の判断を要する。

- `CODEX_HOME`をFeedian専用ディレクトリへ向ける。`--ignore-user-config`のヘルプが「auth still uses `CODEX_HOME`」と述べるとおり認証もそこを見るため、認証情報の配置設計が必要になる。**認証情報の複製を伴うため、仕様として決めるべきである。**
- 抑止する設定項目の追加をCodex CLIへ要望し、追加されるまでは現状を許容する。`--strict-config`は未知フィールドを拒否するため、`experimental_instructions_file`のような回避策は使えないことを確認済みである。
- 現状を既知の制約として`DESIGN.md`へ明記し、`codex-local`はグローバル指示が要約に混ざってよい利用者だけが選ぶopt-inとする。

### 29. untrustedなエージェント出力がエラー分類と監査へ入る — 重大度: 低

**根拠:** `feedian/local_agent.py`の`LocalAgentProcessError`、`feedian/llm_backends.py`の`_classify_codex_process_error`

**現象:** `LocalAgentProcessError`は`stderr`が空なら`stdout`の末尾2048文字をメッセージへ含める。Codexの`stdout`はJSONLイベント列であり、記事本文から生成されたagent messageを含む。`_classify_codex_process_error`は`stderr`と`stdout`を連結して小文字化し、`rate limit`や`429`などの部分一致で分類する。

**影響:** 記事本文由来の文字列がエラーメッセージに入り、`sanitize_error`は完全一致置換しか行わないため`llm_run.error`と端末表示へ残る。また記事に`rate limit`という語が含まれるだけで`BackendRateLimitError`へ誤分類され得る。現在はretryもfallbackも分類で分岐しないため実害は小さいが、指摘26の目的である「分類を後続判断の根拠にする」を実装した時点で、untrustedな入力が制御フローへ影響することになる。

**提案:** 分類は`stderr`のみを対象にする。`stdout`をメッセージへ含めるのは、JSONとして解釈できなかった場合に限る。

### 30. 監査のargvは実行したargvではなく再構築値である — 重大度: 低

**根拠:** `feedian/llm_backends.py`の`audit_argv`

**現象:** `command(Path("<temporary>") / "output-schema.json")`をもう一度呼んで監査用argvを組み立てており、実際に実行された`local.argv`は破棄される。schema pathは`<temporary>`という実在しない値に置き換わる。

**影響:** machine固有pathを監査へ残さない意図は妥当だが、記録が「実行した引数」ではなく「同じ関数を再度呼んだ結果」になっている。`command()`が将来入力に依存するようになれば、監査と実行が無言で食い違う。

**提案:** `local.argv`をサニタイズして保存する。再構築ではなく実値の加工にする。

### 31. local-agentの判別を`auth_mode`で行っている — 重大度: 低

**根拠:** `feedian/ingest.py`の`selected_backend.capabilities.auth_mode == "local-session"`

**現象:** 隔離した一時親を使うかどうかを`auth_mode`で判定している。確定仕様はcapabilityとして「execution kind: `http`または`local-agent`」を定めているが、実装していない。

**影響:** 認証方式と実行方式が偶然一致しているだけである。CLIでAPI keyを使うbackendや、local-sessionで認証するHTTP backendが加わると誤判定する。

**提案:** `BackendCapabilities`へ`execution_kind`を追加し、そちらで判定する。

### 32. 結論節が別のレビュー者によって書き換えられている — 重大度: 低

**根拠:** 本書の`レビュー者`行と`結論`節

**現象:** 本書の見出しは`レビュー者: Claude Code (2026-08-16)`だが、`結論`節は指摘24から27への対応結果を述べる内容へ書き換えられている。`AGENTS.md`のレビュー規約は追記を原則とし、書き換えてよいのはステータス行だけである。

**影響:** 誰の判断なのかが文書から読み取れない。却下理由の帰属が曖昧になると、この文書の主目的が損なわれる。

**提案:** 結論は書き換えず、レビュー者ごとに節を分ける。

## Claude Code再レビューの採否

| # | 指摘 | 重大度 | 採否 | 理由 |
| --- | --- | --- | --- | --- |
| 28 | グローバル`AGENTS.md`とskillsの混入 | 高 | 保留 | 三案のいずれも利用者の判断を要する。`CODEX_HOME`案は認証情報の配置を伴うため仕様として決める |
| 29 | untrusted出力の分類と監査への混入 | 低 | 保留 | 指摘26の分類を実際に分岐へ使う作業と同時に直す |
| 30 | 監査argvの再構築 | 低 | 保留 | 同上 |
| 31 | `auth_mode`によるlocal-agent判別 | 低 | 保留 | `execution_kind`の追加は`claude-code-local`を実装する時点でまとめて行う |
| 32 | 結論節の書き換え | 低 | 採用 | 本節を独立させ、結論は各レビュー者の節に置く |

## Claude Code再レビューの検証

- [x] `codex login status`が存在し、ログイン済みで終了コード0を返す。
- [x] 実CLIが`--config mcp_servers={}`を受理し、エージェントがMCP serverを一つも認識しない。
- [x] 一時cwdの親に置いた`AGENTS.md`カナリアが要約へ影響しない。
- [x] `ruff check`が通り、テストが239件成功する。
- [x] `logical`/`actual` envelopeで壊れる`request_json`の読み手が無い。
- [ ] **グローバル`~/.codex/AGENTS.md`とskillsカタログはエージェントへ届いている（指摘28）。確定仕様の安全ポリシーは未達である。**
- [ ] タイムアウト時の実プロセスツリー終了テストは未実施のままである。

## Codexによる指摘28の補足レビュー（2026-08-17）

レビュー者: Codex

### 結論

指摘28は妥当であり、Claude Codeが挙げた三案のうち、**Feedian専用の`CODEX_HOME`を使う案を推奨する**。ただし、既存の`~/.codex`を複製して不要なファイルを削る方式にはしない。`~/.feedian/codex-home`のような空の安定ディレクトリを新設し、その環境で利用者が一度だけ`codex login`を実行する。

Feedianは`CODEX_HOME`を自身が起動する子processの`env`だけに設定する。親processの`os.environ`やuser・machine scopeの環境変数は変更しない。この実装なら、通常のCodexは引き続き`~/.codex`を使うため、グローバルの設定、skills、plugins、rules、hooks、ログイン状態にはfilesystem上の変更を加えない。`CODEX_HOME`を設定したshellで手作業する場合は、そのshellと子processには値が見えるため、login後に元へ戻す必要がある。

専用homeはsystem tempやVaultの中ではなく、利用者ごとの永続領域へ置く。起動前に専用home直下の`AGENTS.md`、`AGENTS.override.md`、`skills/`、`plugins/`、`rules/`、`hooks/`が存在しないことも検証し、見つけた場合は黙って読み込まずpreflight errorにする。

### 認証情報配置の補足

「複製せずに新規loginする」という方向は正しいが、**認証情報が二箇所に存在しなくなるわけではない**。既定homeとFeedian専用homeは、それぞれ有効な認証cacheを持つ。利点は、同じ`auth.json`をcopyしてrefresh stateを分岐させるのではなく、それぞれが正規のloginで作られ、独立して更新されることである。

Codexは認証情報を`CODEX_HOME/auth.json`だけでなくOS keyringへ保存できる。専用homeによる分離を決定的にするため、setup時と実行時の双方で`cli_auth_credentials_store="file"`を明示する。実行時は`--ignore-user-config`を使うため、専用homeの`config.toml`だけにこの値を書いても足りない。CLI overrideとして毎回渡す。

概念上のsetupは次のとおりである。実装時には、Feedian側にplatform差を吸収するlogin補助commandを設けるか、同等の手順を利用者向け文書へ記載する。

```text
CODEX_HOME=<user data>/.feedian/codex-home
codex --config cli_auth_credentials_store="file" login
```

公式文書は、file保存時の認証情報が`CODEX_HOME`配下に置かれること、ChatGPT session tokenが利用中に自動更新されること、globalな`AGENTS.md`が`CODEX_HOME`から探索されることを説明している。また`--ignore-user-config`は`CODEX_HOME/config.toml`を無視する一方で、認証には引き続き`CODEX_HOME`を使う。([Authentication](https://developers.openai.com/codex/auth)、[AGENTS.md](https://developers.openai.com/codex/guides/agents-md)、[CLI reference](https://developers.openai.com/codex/cli/reference)、[Configuration reference](https://developers.openai.com/codex/config-reference))

一方、同一ChatGPT accountで二つのlogin cacheを長期間併用した場合のaccount側の独立性や同時session数は、公式文書で保証を確認できていない。Claude Codeの実測は「別`CODEX_HOME`で操作しても既定homeのlogin状態が直ちに壊れない」ことの確認としては有効だが、長期運用の保証とは扱わない。

### 33. local-agent子processが親環境を丸ごと継承する — 重大度: 中

**根拠:** `feedian/local_agent.py:82`、`feedian/llm_backends.py:274,324,336`、`docs/specs/20260816-llm-backends.ja.md:456,519`

**現象:** `SubprocessRunner`の`subprocess.Popen`、version検出、`codex login status`はいずれも`env`を明示していないため、親processの環境変数をそのまま継承する。これは、確定仕様の「CLI processへ継承する環境変数はallowlist方式とする」という要件、およびfake runnerで環境変数allowlistを検証する要件を満たさない。

**影響:** `OPENAI_API_KEY`、`CODEX_ACCESS_TOKEN`、cloud providerのtokenなど、Codex実行に不要なsecretが子processへ到達し得る。`NODE_OPTIONS`、`PYTHONPATH`、任意の`GIT_*`など、子processの挙動を変える環境変数も同様である。継承した`OPENAI_API_KEY`が自動的に認証方式や課金区分を切り替えることまでは確認できていないため、その点は確定事実としない。ただし、不要なsecretの露出と実行再現性の低下だけで修正理由として十分である。

**提案:** 指摘28と同時に修正する。`ProcessRunner`の契約へ明示的な`env` mappingを追加し、platformごとの最小allowlistから子環境を組み立てる。その同じ環境をversion検出、login確認、`codex exec`のすべてへ渡し、`CODEX_HOME=<Feedian専用home>`と`cli_auth_credentials_store="file"`を一貫して適用する。必要なOS変数、locale、TLS certificate、proxyの採否は明示的に決め、secretやprocess挙動を変更する変数を既定では渡さない。fake runnerの契約テストで三経路のenvが一致することと、禁止した変数が除外されることを検証する。

## Codex補足レビューの採否

| # | 指摘 | 採否 | 理由 |
| --- | --- | --- | --- |
| 28 | グローバル`AGENTS.md`とskillsの混入 | 保留 | Feedian専用`CODEX_HOME`案をCodexの推奨とする。実装指示と利用者による専用homeへのloginが完了するまでは未解消であり、正式な完了判定を行わない |
| 33 | local-agent子processが親環境を丸ごと継承する | 採用 | 確定仕様のallowlist要件に対する実装漏れである。指摘28の`CODEX_HOME`配線と同じ変更で対応する |

## Codex補足レビューの検証計画

- [ ] 専用`CODEX_HOME`でloginした実CLIに、既定homeの`AGENTS.md`とskillsカタログが届かない。
- [ ] 専用homeに禁止対象の指示・拡張directoryがある場合、preflightで拒否する。
- [ ] 通常のCodexが引き続き既定の`~/.codex`を使用し、既存のlogin状態と設定が変化しない。
- [ ] version検出、login確認、`codex exec`が同一のallowlist環境を受け取る。
- [ ] 親環境のsecretとprocess挙動を変更する禁止変数が子processへ渡らない。
- [ ] 認証保存方式がsetup時と実行時の双方でfileに固定され、専用homeの`auth.json`を使う。

## 指摘28・33の実装（2026-08-17）

実装者: Claude Code

Codexの補足レビューの推奨どおり、Feedian専用の`CODEX_HOME`と環境変数allowlistを実装した。`cli_auth_credentials_store="file"`の指摘は正しく、これが無いとOS keyring経由で既定homeの認証が共有され得るため分離が成立しない。

### 実装内容

- `minimal_child_environment`をallowlist方式で追加した。共通（`PATH`、`LANG`、`LC_ALL`、`TZ`）、プラットフォーム別、proxyの三系統だけを引き継ぐ。`ProcessRunner`の契約へ`env`を追加し、`SubprocessRunner`は`Popen(env=...)`へ渡す。
- `codex_home()`は`~/.feedian/codex-home`を返す。他のユーザー単位状態（`~/.feedian/pending`、`source-state.json`）と同じ場所である。
- version検出、`login status`、`codex exec`の三経路すべてが`child_environment()`の同一の環境を受け取る。契約テストで三者の一致と、`OPENAI_API_KEY`・`MANUS_API_KEY`・`CODEX_ACCESS_TOKEN`・`NODE_OPTIONS`が渡らないことを検証する。
- `cli_auth_credentials_store="file"`を`login status`と`codex exec`の両方でCLI overrideとして渡す。`--ignore-user-config`が専用homeの`config.toml`も無視するため、設定ファイルへ書くだけでは効かない。
- preflightが専用homeを検証する。`auth.json`が無ければ`BackendAuthError`にログインコマンドを添えて返す。`AGENTS.md`、`AGENTS.override.md`、`skills`、`plugins`、`rules`、`hooks`、`memories`のいずれかが存在すれば`BackendPolicyError`で拒否する。
- 検証順はversion、home、loginとした。未検証versionではログインしても無駄になるためである。
- `_detect_version`が`_resolve_executable`を直接使っており、fake runnerでは到達できなかった。`_verify_login`と同じ`_control_executable`へ揃えた。

実CLIで`--strict-config`を付けて実行し、`cli_auth_credentials_store`と`mcp_servers`がいずれも実在する設定キーであることを確認した。

### 専用homeでの実測（2026-08-17）

利用者が専用homeへログインした後、指摘28と同一のプローブを出荷構成で実行した。

```text
System instructions — private; cannot quote.
Developer instructions — private; cannot quote.
Skills catalog — first entry: "imagegen"
Environment context — "<environment_context>"
AGENTS.md: none provided.
Memories: none.
MCP servers: none.
```

指摘28で報告された`User-provided AGENTS.md — "# グローバル作業指示"`は消えた。skillsカタログの先頭も、既定homeにある利用者の14個（`cloudflare`、`wrangler`など）ではなくCLI内蔵の`imagegen`に変わっている。入力トークンは12,273から10,835へ減った。

untrusted本文にファイル読み取りを注入したエンドツーエンド実行も、カナリアを漏らさずに正常な要約を返す。

**利用者が管理する指示は、AGENTS.md、skills、plugins、rules、hooks、memoriesのすべてについて到達しなくなった。** 残るのはCLI内蔵のsystem instructionsと内蔵skillsカタログであり、これは`CODEX_HOME`では除去できない。CLI自身のsystem promptと同じ性質のものとして扱い、確定仕様が求める「user settingsとskillsを無視する」は達成したと判断する。

### 実測で判明した実装の誤り

初版の`_verify_home`は`skills`ディレクトリの存在だけで拒否していた。ところが**CLIは実行時に`skills/.system/`を自分で作り、内蔵skillを配置する**。このため初回実行の直後から`BackendPolicyError`で拒否され、2回目以降が動かなかった。`~/.codex`の一覧から書いた静的な検査が、実際の運用を1周させて初めて誤りと分かった例である。

`skills/.system`はCLI管理領域として許容し、`skills/`直下のそれ以外を利用者由来として拒否するよう修正した。回帰テストで両方を固定している。

### 指摘28・33の結論

いずれも**採用・解消**とする。残るCLI内蔵分は`DESIGN.md`へ既知の制約として記載する。

## 残る保留指摘の対応（2026-08-17）

対応者: Claude Code

保留としていた指摘をすべて処理した。

| # | 指摘 | 採否 | 対応 |
| --- | --- | --- | --- |
| 8 | 無視される`fallback`設定 | 採用 | 確定仕様どおり実装した。既定で無効、有効化にはbackendとmodelの明示が必要で、宛先をプラン画面へ表示する。切り替わるのは`BackendUnavailableError`、`BackendRateLimitError`、`BackendTimeoutError`のみとし、認証・ポリシー・プロトコルの失敗では切り替えない |
| 9 | 強制されないcapability | 採用 | `usage_available`を`unmetered_requests`の判定へ接続した。`max_parallelism`はingestが逐次実行である限り自明に満たされるため宣言のままとし、並列化の時点で強制対象へ加える |
| 22 | rich進捗テストの幅依存 | 採用 | `setUp`での`COLUMNS`固定が全テストへ及んでおり、幅40・80・200で9件成功する |
| 29 | untrusted出力の分類と監査への混入 | 採用 | `LocalAgentProcessError`はstderrだけをメッセージと`diagnostics`に採る。分類も`diagnostics`のみを見るため、記事に`rate limit`とあってもFeedianの分類は変わらない |
| 30 | 監査argvの再構築 | 採用 | `sanitized_argv`が実際に実行したargvから一時ディレクトリのpathだけを置換する。プロセスが起動しなかった場合に限り、組み立てたargvを記録する |
| 31 | `auth_mode`によるlocal-agent判別 | 採用 | `BackendCapabilities`へ確定仕様どおり`execution_kind`を追加し、隔離した一時親の判定をそちらへ移した |

### fallbackの設計

fallbackは宛先backendの`llm_run`を新しく開いて実行する。同じrunを上書きしないため、`backend`列と再利用キーが常に実行した組み合わせを指す。宛先のmodelはbackendごとに`max_article_chars`が異なるため、候補を宛先backendで組み立て直してから実行する。宛先に再利用可能な結果があればそれを使い、APIを呼ばない。

宛先backendの`preflight`は初回のfallback発生時にだけ行う。使われないfallbackのために資格情報を要求しないためである。

### タイムアウト時のプロセスツリー終了

`tests/test_local_agent.py`に実プロセスによる検証を追加した。孫プロセスを起こす親を1.5秒でタイムアウトさせ、孫が自分の待機時間を過ぎてもマーカーファイルを書かないことを確認する。fake runnerの契約テストでは原理的に検証できなかった項目である。

### 検証

- [x] テストスイートが緑である（249件）。`ruff check`も通る。
- [x] 実プロセスのプロセスツリーがタイムアウトで完全に終了する。
- [x] fallbackが有効なとき、利用枠到達では宛先backendで別runが作られ、認証失敗では作られない。
- [x] fallbackが無効なとき、利用枠到達でも別backendを呼ばない。
- [x] プラン画面が宛先を表示し、無効なら`disabled`と示す。
- [x] 進捗テストが幅40・80・200で通る。

これで本レビューの指摘1から33はすべて解消または明示的な不採用となった。ステータスを`完了`とする。

## PR #11 でのCodexレビュー（2026-08-17）

レビュー者: Codex (GitHub review bot)

fallbackの実装に対する指摘3件。いずれも妥当であり、同一ブランチで修正した。

### 34. HTTP primaryからlocal fallbackへ移ると隔離境界を失う — 重大度: 高

**根拠:** `feedian/ingest.py`の`temporary_parent`

**現象:** `temporary_parent`はprimary backendの`execution_kind`だけで一度決めていた。primaryがHTTPの場合はVault内の`.feedian/tmp`のままで、それをfallbackの`codex-local`へそのまま渡していた。

**影響:** 指摘24で切り離したはずの境界が、fallback経路だけ元に戻る。Vault自体がGit projectである構成では、projectの`AGENTS.md`と`.codex/config.toml`が再び届く。

**対応:** 採用。`_temporary_parent_for`を追加し、attemptごとに実行するbackendから選ぶ。回帰テストで、HTTP primaryの背後にあるlocal fallbackがVault外で動くことを固定した。

### 35. fallbackのmodelが検証されない — 重大度: 中

**現象:** `supports_model`はprimaryにしか適用していなかった。`manus-api`へ`gpt-*`をfallback modelに指定すると、`_summarize_with_manus`が黙って`manus-1.6`を実行する一方、`llm_run`、fingerprint、noteは`gpt-*`を記録する。

**影響:** 監査上の実行モデルが事実と食い違い、再利用キーも実際とは違う組み合わせを指す。

**対応:** 採用。`resolve_fallback`で検証し、`BackendPolicyError`にする。計画表示の時点で呼ばれるため、runを開く前に失敗する。

### 36. 有効なfallbackの課金額が上限に含まれない — 重大度: 中

**現象:** 上限コストはprimaryのbilling modeだけで計算していた。`codex-local`とmetered fallbackの組み合わせでは、課金され得るのに上限が`n/a`と表示される。

**対応:** 採用。`fallback_maximum_cost`を追加し、全件がfallbackした場合の上限を計画画面のFallback行へ併記する。

### 採否

| # | 指摘 | 重大度 | 採否 | 対応 |
| --- | --- | --- | --- | --- |
| 34 | fallback時の隔離境界 | 高 | 採用 | attemptごとに一時親を選ぶ |
| 35 | fallback modelの未検証 | 中 | 採用 | `resolve_fallback`で拒否する |
| 36 | fallback課金額の非表示 | 中 | 採用 | 上限を計画画面へ併記する |

3件とも、fallbackを「primaryと同じ条件で動く追加の実行」と暗黙に仮定していたことが原因である。fallbackは宛先backendの条件で動く別の実行であり、隔離、model検証、課金のすべてを宛先側から導く必要がある。

### 検証

- [x] テストスイートが緑である（252件）。`ruff check`も通る。
- [x] HTTP primaryの背後のlocal fallbackが、Vault外の一時親を受け取る。
- [x] 宛先が扱えないmodelは、runを開く前に`BackendPolicyError`になる。
- [x] subscription primaryとmetered fallbackの組み合わせで、fallbackの上限額が表示される。

## PR #11 でのCodeRabbitレビュー（2026-08-17）

レビュー者: CodeRabbit (GitHub review bot)

### 37. 新規作成と移行後で`llm_run`のDDLが一致しない — 重大度: 中

**根拠:** `feedian/store.py`の`_create_schema`と`_migrate_v5_to_v6`

**現象:** `_create_schema`は`backend`、`summary_schema_version`、`fingerprint_version`、`auth_mode`、`billing_mode`をDEFAULT無しの`NOT NULL`で作る。一方マイグレーションは同じ列をDEFAULT付きで追加する。SQLiteは既存行のあるテーブルへ`NOT NULL`列を足す際にDEFAULTを要求するため、マイグレーション側からDEFAULTを外すことはできない。

**影響:** 同じ`schema_version = 6`でも、新規DBと移行済みDBでDDLが異なる。将来のマイグレーションやスキーマ比較の前提が崩れる。

**対応:** 採用。`_create_schema`をマイグレーションと同じDEFAULT付きへ揃えた。`PRAGMA table_info`で両者の列定義が一致することを回帰テストで固定している。

### 38. fallbackのテストが実行環境の一時ディレクトリに依存する — 重大度: 低

**現象:** `isolated_local_agent_parent`はシステム一時ディレクトリの祖先を`.git`の有無で走査する。`TMPDIR`がGit管理下にある環境では、fallbackの検証に入る前に`BackendPolicyError`で失敗する。

**対応:** 採用。既存テストと同じく`tempfile.gettempdir`を固定した。

### 39・40. Markdownの整形 — 重大度: 低

コードフェンスの言語指定（MD040）3箇所と、`Codex補足レビューの採否`表の列数不一致を修正した。後者はヘッダーが4列に対して本文が3列で、指摘名の列が欠落していた。

### 不採用とした指摘

| 指摘 | 採否 | 理由 |
| --- | --- | --- |
| `AGENTS.md`の`最終案`省略規則を修正する | 不採用 | レビュー不要時に`最終案`を省略できる規則は矛盾ではなく意図である。レビューが無ければ草案がそのまま結論であり、同じ内容を持つ節を別に作っても情報が増えない。当該規則は「ステータスは`確定`とする」と併記しており、この場合を検討した上で書かれている。加えてこれは本PR以前から存在する人間が定めた運用規約であり、bot指摘だけを根拠に変更しない |
| `docs/specs/llm-backends.ja.md`を日付付きへ改名する | 不採用 | 本PRが触れていない既存ファイルである。規約自身が「一度付けた名前を変更しない」と定めており、さらに確定版`20260816-llm-backends.ja.md`が本文でこのファイル名を参照している。確定仕様は編集しない規約であるため、改名すると参照が壊れる。命名規約より前に作られた文書の扱いは、必要なら別途決める |

### `ingest.py:204`について

CodeRabbitとCodexの双方が同じfallback隔離の問題を指摘した。CodeRabbitのレビュー対象commitは`c47f463`であり、`e02e872`（指摘34）で修正済みである。

### 検証

- [x] テストスイートが緑である（253件）。`ruff check`も通る。
- [x] 新規DBと移行済みDBの`llm_run`列定義が`PRAGMA table_info`で一致する。
- [x] fallbackのテストが`tempfile.gettempdir`の固定により環境非依存になった。

### 41. `LEGACY_V1_PROVIDER_SCHEMA`が凍結されていない — 重大度: 中

**根拠:** `feedian/llm.py`、CodeRabbitの指摘

**現象:** 凍結スナップショットを`deepcopy(PROVIDER_OUTPUT_SCHEMA)`で定義していた。実行時の変更からは守られるが、**ソースを編集すれば旧キーも一緒に変わる**。凍結が守るべきなのはまさにその場合である。

**影響:** 指摘2とまったく同じ失敗、すなわち移行期間中の旧キー検索が無言で一致しなくなり全記事が再課金される事態を、指摘2の修正自身が再現していた。実測値を固定した回帰テストが最後の砦として残ってはいたが、定数の定義がその意図を表していなかった。

**対応:** 採用。`2385ec2`の`SUMMARY_SCHEMA`と一致するリテラルを書き下した。両者が一致することを確認済みである。

### 42. 幅依存のテストがまだ2件あった — 重大度: 低

**現象:** 指摘22で`tests/test_progress.py`を直したが、`tests/test_cli.py`にも幅依存が残っていた。プラン表示のテストは`COLUMNS`に加えて一時パスの長さでも折り返し位置が変わる。argparseのhelpを検証するテストも`COLUMNS`を見る。

**対応:** 採用。両方で`COLUMNS`を固定した。`tests/test_cli.py`と`tests/test_progress.py`が幅40・80・200で通ることを確認した。

### 43. Vaultルート自体を拒否するassertionが無い — 重大度: 低

**現象:** `Path.parents`は対象パス自身を含まない。fallbackの一時親がVaultルートと等しい場合、既存のassertionは成功してしまう。

**対応:** 採用。`used != root.resolve()`を追加した。

### CodeRabbit指摘のうち対応済みだったもの

`feedian/llm_backends.py`の「初回実行前に専用`CODEX_HOME`を作成する」は、`_verify_home`の`mkdir(parents=True, exist_ok=True)`で対応済みである。CodeRabbitのレビュー対象commitがその修正より前だった。

### 検証

- [x] `LEGACY_V1_PROVIDER_SCHEMA`が`2385ec2`の`SUMMARY_SCHEMA`とオブジェクトとして等しい。
- [x] `tests/test_cli.py`と`tests/test_progress.py`が幅40・80・200で通る。
- [x] テストスイートが緑である（253件）。`ruff check`も通る。
