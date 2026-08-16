# LLMバックエンド実装のコードレビュー

ステータス: 対応中
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

```
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

```
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
