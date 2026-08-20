# Claude Code API キー対応

ステータス: 確定

## 最終案

### 結論

既存の`claude-code-local` backendに、API credentialを使ったClaude Code CLIの非対話実行を追加する。
Anthropic公式endpointでは`ANTHROPIC_API_KEY`を使い、Anthropic Messages互換APIでは任意の
`ANTHROPIC_BASE_URL`と、`ANTHROPIC_API_KEY`または`ANTHROPIC_AUTH_TOKEN`のどちらか一方を使う。
実効`auth_mode`は`api-key`とし、`billing_mode`は公式endpointでは`metered-api`、互換APIでは
料金体系を推測せず`unknown`とする。backend IDは追加しない。

`claude-code-local`はあくまでローカルprocessとして`claude`を起動するbackendである。
直接Anthropic Messages APIを呼ぶ`anthropic-api`とは実行経路が異なるため、`ApiBackend`を継承せず、
Codex CLIのcommand builderやevent parserも流用しない。共通化するのは、process起動、stdin、deadline、
process tree停止、一時directory cleanupを担う`LocalAgentRunner`相当の層だけとする。

Claude Codeのbare modeはOAuth資格情報とsystem keychainを読まないため、
`local-session` / `subscription`は初版では利用不可のまま残す。安全条件とsubscription認証を同時に
満たす実行契約が将来確認できた場合に限り、別の仕様で有効化する。

### 対象と対象外

対象は次のとおりとする。

- 保存済みresourceを要約する既存の`feedian ingest`から`claude-code-local`を選択できるようにする。
- 公式endpointの`ANTHROPIC_API_KEY`、または互換APIの`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`を
  preflightで検証する。
- 任意の`ANTHROPIC_BASE_URL`でAnthropic Messages互換APIまたはgatewayへ接続する。
- Claude Code固有の安全なargv、JSON response parser、error分類、usage・cost監査を実装する。
- fake runnerによる契約試験と、実CLIを使うopt-in統合試験を追加する。

次は対象外とする。

- `claude-code-api`というbackend IDの追加。
- 直接Anthropic Messages APIを呼ぶ`anthropic-api`の実装。
- Claude CodeのOAuth、Claude.ai subscription、`CLAUDE_CODE_OAUTH_TOKEN`による認証。
- `ingest --url`、`summarize_from_url()`その他のURL入力機能。
- session再利用、常駐process、初期値を超える並列実行。
- Bedrock、Vertex、Foundry固有のAPI形式、credential、provider設定。
- `apiKeyHelper`、`ANTHROPIC_CUSTOM_HEADERS`、gateway model discovery。

### Backend契約

`claude-code-local`の初期capabilityは次の値とする。

| 項目 | 値 |
|---|---|
| `backend` | `claude-code-local` |
| `execution_kind` | `local-agent` |
| `auth_mode` | `api-key` |
| `billing_mode` | 公式endpointは`metered-api`、互換APIは`unknown` |
| `max_article_chars` | `10_000` |
| `usage_available` | `true`。個々のusage値は欠損可能 |
| `max_parallelism` | `1` |
| `min_start_interval_seconds` | `0.0` |
| credential | 公式endpointは`ANTHROPIC_API_KEY`。互換APIは`ANTHROPIC_API_KEY`または`ANTHROPIC_AUTH_TOKEN`のどちらか一方 |
| API endpoint | `ANTHROPIC_BASE_URL`が空ならAnthropic公式、設定時は検証・正規化した互換API |
| model環境変数 | `ANTHROPIC_MODEL` |
| 組込み既定model | 公式endpointだけ`claude-sonnet-5`。互換APIでは既定値なし |

backend IDは認証方式や接続先ではなく実行経路を表す。`claude-code-local`の実行結果は、backend ID、
model、endpoint fingerprint、prompt version、summary schema version、language、生成設定、入力fingerprintを
再利用境界にする。credentialそのものはfingerprint、Vault config、argv、監査へ含めない。

`ANTHROPIC_BASE_URL`は親processで検証・正規化し、正規化済みURLのSHA-256を`endpoint_fingerprint`として
logical requestとbackend metadataへ入れる。URLそのものは内部host名やrouting pathを含み得るため監査へ
保存しない。未設定時もAnthropic公式endpointを表す固定fingerprintを使い、公式endpointと互換API、
互換API同士で結果を再利用しない。

公式endpointの`supports_model()`はAnthropic APIの固定model IDだけを受け付ける。互換APIでは、
`--model`、`ANTHROPIC_MODEL`、または同じbackendのVault設定から明示されたgateway固有IDを受け付ける。
gateway固有IDは1文字以上200文字以下で、ASCII英数字と`._:/@-`だけを許可する。どちらのendpointでも
少なくとも`default`、`best`、`sonnet`、`opus`、`haiku`、`opusplan`の可変aliasを拒否する。

modelの選択順は次のとおりとする。

1. `feedian ingest --model`。
2. `ANTHROPIC_MODEL`。
3. 選択backendがVault設定と同じ場合の`llm.model`。
4. 公式endpointだけ組込み既定値`claude-sonnet-5`。

解決したmodelは`--model`で明示的にClaude Codeへ渡す。`ANTHROPIC_MODEL`は親processでの選択にだけ
使い、子processの環境へは渡さない。互換APIで1から3のいずれにもmodelがなければ、記事送信前に
`BackendPolicyError`とする。

### クラス境界

`ClaudeCodeLocalBackend`は`ApiBackend`を継承せず、`LLMBackend` protocolを直接実装する。
`CodexLocalBackend`とも継承関係を持たない。次の部品だけをlocal-agent共通層から利用する。

- isolatedな一時cwdの作成とcleanup。
- stdinによるuntrusted inputの受け渡し。
- process deadline、process tree停止、exit statusの取得。
- stdoutとstderrの分離。
- audit用argvのsanitization。

Claude Code側には専用のcommand builder、preflight、JSON parser、process error classifierを置く。
`parse_codex_events()`は使用しない。`summarize_from_url()`は追加しない。page fetchはこれまでどおり
backend呼出し前に完了させ、`summarize()`は共通契約の`item`と`page`だけを受け取る。

### Preflight

新しいrequestが1件以上あるingest runでは、記事本文を子processへ渡す前にpreflightを1回実行し、
結果をrun中でcacheする。preflightは次の順序で検証する。

1. `claude` executableを`PATH`から解決する。存在しなければ`BackendUnavailableError`とする。
2. `ANTHROPIC_BASE_URL`をtrimする。空なら公式endpointとする。設定時は絶対URLとしてparseし、userinfo、
   query、fragmentを拒否する。remote endpointは`https`だけを許可し、`http`は`localhost`、
   `127.0.0.0/8`、`::1`のloopbackだけを許可する。schemeとhostを小文字化し、既定portと末尾slashを
   除いて正規化する。不正なURLは`BackendPolicyError`とする。
3. 公式endpointではtrimした`ANTHROPIC_API_KEY`を必須とする。互換APIではtrimした
   `ANTHROPIC_API_KEY`または`ANTHROPIC_AUTH_TOKEN`のどちらか一方だけを必須とし、両方または両方なしを
   記事送信前に拒否する。値はerror、metadata、fingerprintへ含めない。
4. 互換APIでは明示modelが存在し、上記のgateway model ID規則を満たすことを検証する。
5. `claude --version`を実行し、versionを取得する。対応範囲は`2.1.205`以上`3.0.0`未満とする。
   範囲外または解釈できないversionは`BackendPolicyError`とする。`2.1.205`未満では不正な
   `--json-schema`が無視され得るため、構造化出力の安全条件を満たさない。将来のmajor versionは
   CLI契約を再検証してから対応範囲へ加える。

`claude --help`はすべてのflagを列挙する契約ではないため、help文字列の有無をfeature判定に使わない。
対応範囲内のflag契約はfake runnerとopt-in実CLI試験で固定する。

version検出にも、実行時と同じallowlist方式の子process環境を使う。preflight metadataには
`implementation_revision`、Claude Code version、sanitizedしたexecutable名、実効`auth_mode`、
実効`billing_mode`、`endpoint_kind=official|custom`、`endpoint_fingerprint`、
`credential_transport=x-api-key|bearer-token`を含める。credential、完全なuser path、base URLは含めない。

### 子process環境と隔離

子process環境は`minimal_child_environment()`を基礎にし、次だけを明示的に追加する。

- `ANTHROPIC_API_KEY`または`ANTHROPIC_AUTH_TOKEN`: preflightで選択したcredentialだけ。
- `ANTHROPIC_BASE_URL`: 互換APIを選択した場合だけ正規化済みURL。
- `CLAUDE_CONFIG_DIR`: requestごとのisolated一時directory内の空directory。
- `DISABLE_AUTOUPDATER=1`。
- `DISABLE_TELEMETRY=1`。
- `DISABLE_ERROR_REPORTING=1`。
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`。

`OPENAI_API_KEY`、`MANUS_API_KEY`、`CLAUDE_CODE_OAUTH_TOKEN`、選択しなかったAnthropic credential、
`ANTHROPIC_CUSTOM_HEADERS`、親processが持つその他のsecret、`ANTHROPIC_MODEL`は渡さない。gateway model
discoveryは有効化しない。一時cwdと`CLAUDE_CONFIG_DIR`はVaultとGit repositoryの外へ作成し、成功、
失敗、timeout、KeyboardInterruptのすべてで削除する。

### Claude Code command

記事ごとに独立processを起動する。commandの論理形は次のとおりとする。

```text
claude -p
  --bare
  --tools ""
  --no-chrome
  --no-session-persistence
  --max-turns 1
  --output-format json
  --json-schema <PROVIDER_OUTPUT_SCHEMAをcompact JSON化した値>
  --model <固定model ID>
  --effort low
  "stdinのrequestを要約するという固定指示"
```

shell文字列ではなくargv配列で起動し、`--tools`の次には空文字列を1引数として渡す。
`--json-schema`はfile pathではなくJSON Schema文字列を受け取るClaude Codeの契約に合わせる。
audit用argvではschema本文を`<schema>`、一時pathを`<temporary>`へ置換する。

argvへ置くpromptは、stdinのrequestを処理するよう指示する固定文字列だけとする。summary入力、
untrusted title、URL、metadata、comment、記事本文はすべてstdinへ渡し、argvへ含めない。stdinは
既存の`build_summary_request()`と`build_untrusted_message()`で組み立てる。

初版の`reasoning_effort`は、現在のingestが渡す`low`だけを許可する。それ以外は黙って変換せず
`BackendPolicyError`とする。Claude Code CLIにはFeedianの`max_output_tokens`を直接強制するflagが
ないため、初版では論理requestとprompt上の出力目標として保持し、実usageを監査する。
local-agent adapterの外側でrequest全体を再試行せず、Claude Code自身のretryと共通のwall-clock
deadlineに委ねる。

### Responseの解釈

`--output-format json`のstdoutはClaude Code専用parserで単一JSON objectとして解釈する。
次の条件を満たさないresponseは`BackendProtocolError`とし、source noteと成功runを作らない。

- stdout全体がUTF-8のJSON objectであり、前後に非JSON文字列がない。
- commandが成功を報告している。
- `structured_output`が存在し、objectである。
- `structured_output`が`PROVIDER_OUTPUT_SCHEMA`を満たす。
- 正規化後のresultがFeedianのcanonical summary schemaを満たす。

responseの`usage`からinput、cache creation、cache read、output tokenをnullable値として正規化する。
欠損を`0`にしない。`total_cost_usd`はClaude Code clientの推定額であり、providerの確定請求額ではないため、
`cli_estimated_cost_usd`として保存する。responseが報告する実model ID、Claude Code version、session IDは
backend metadataへ保存するが、sessionをresumeする機能は提供しない。

監査へ保存するraw responseとstderrは既存のsize上限とredactionを通す。stdoutは記事由来の内容を
含み得るため、process errorの分類に使わない。

### Error分類

timeoutは共通runnerから`BackendTimeoutError`へ変換し、process treeを停止する。non-zero exitは
Claude Code専用classifierでstderrだけを調べ、次へ分類する。

| 条件 | Feedian error |
|---|---|
| API key不足、401、認証失敗 | `BackendAuthError` |
| 429、rate limit | `BackendRateLimitError` |
| service unavailable、overloaded、network失敗 | `BackendUnavailableError` |
| JSON、schema、structured output不正 | `BackendProtocolError` |
| その他のnon-zero exit | `BackendExecutionError` |

stderrはUTF-8 replacement decoding後の末尾2 KiBだけを分類に使う。API key、authorization header、
user path、記事本文、prompt、tool outputをredactしてからerrorと監査へ保存する。stdout内の文字列で
error種別が変わらないことを契約試験で確認する。

### CLIと設定

`BACKEND_IDS`、Vault configのbackend allowlist、`get_backend()`には既存の
`claude-code-local`だけを残す。`claude-code-api`と`anthropic-api`の分岐は追加しない。
`feedian/cli.py`のbackend別model環境変数は`claude-code-local: ANTHROPIC_MODEL`とする。

`.env.example`には秘密値を含めず、公式endpointと互換APIの設定方法を追加する。

```dotenv
# Anthropic official endpoint: metered API billing.
ANTHROPIC_API_KEY=your-anthropic-api-key
# Optional pinned model ID. Defaults to claude-sonnet-5.
ANTHROPIC_MODEL=claude-sonnet-5

# Optional Anthropic Messages-compatible API or gateway.
# ANTHROPIC_BASE_URL=https://gateway.example.com/anthropic
# A gateway may use ANTHROPIC_API_KEY (x-api-key) or ANTHROPIC_AUTH_TOKEN (Bearer).
# Set exactly one credential; never set both.
# ANTHROPIC_AUTH_TOKEN=your-gateway-token
```

公式endpointの利用例は次のとおりとする。

```powershell
$env:ANTHROPIC_API_KEY = "..."
feedian ingest --backend claude-code-local --model claude-sonnet-5
```

Bearer tokenを使う互換APIの利用例は次のとおりとする。

```powershell
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
$env:ANTHROPIC_AUTH_TOKEN = "..."
$env:ANTHROPIC_BASE_URL = "https://gateway.example.com/anthropic"
feedian ingest --backend claude-code-local --model my-gateway/claude-sonnet-5
```

`feedian ingest`は保存済みresourceを対象とするため、URLやinput fileを位置引数に渡さない。
API key、model、backendを`backend:model`の1文字列へ結合しない。

### 保存データと互換性

backend IDを変更せずDB schemaも変更しない。既存の`claude-code-local`は利用不可だったため、
成功resultの移行は行わない。既存のfailed runは履歴として保持する。

新しい実装は`BACKEND_IMPLEMENTATION_REVISION`を更新する。runごとに`auth_mode=api-key`、実効
`billing_mode`、Claude Code version、実model ID、endpoint kind、endpoint fingerprint、credential
transportを監査へ保存する。認証secret自体はfingerprintへ含めないが、backend ID、model ID、endpoint
fingerprintが異なる結果を横断して再利用しない。互換APIではCLIが返したcostも請求額とみなさず、
unpriced / unknownとして扱う。保存済みresource本文、成功run、source noteを削除または書き換えない。

### 影響範囲

- `feedian/llm_backends.py`: Claude Code専用backend、endpoint・credential preflight、command、parser、error分類。
- `feedian/local_agent.py`: requestごとの`CLAUDE_CONFIG_DIR`を子process環境へ渡せる隔離境界とredaction。
- `feedian/cli.py`: `claude-code-local`のmodel環境変数を`ANTHROPIC_MODEL`へ変更。
- `feedian/ingest.py`: endpoint fingerprintをlogical requestと再利用境界へ追加。
- `.env.example`: `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL`とmodelの例。
- `tests/test_llm_backends.py`: fake runnerによるbackend契約。
- `tests/test_local_agent.py`: 環境allowlist、path・secret redaction、cleanup。
- `tests/test_ingest.py`または同等のCLI試験: backend/model選択、監査、並列数上限。
- `DESIGN.md`: 実装commitで現行挙動の要約と本仕様へのlinkを追加。

### 受け入れ条件

CIで必須のfake runner試験は次を満たす。

1. credential不足・重複、endpoint不正、互換APIのmodel不足、executable不足、対応範囲外または
   解釈不能なversionが記事送信前に失敗する。
2. preflightは新しいrequestがあるrunにつき1回だけ実行される。
3. argvに`-p`、`--bare`、空の`--tools`、`--no-chrome`、`--no-session-persistence`、
   `--max-turns 1`、JSON出力、Schema、固定model、`--effort low`が含まれる。
4. title、URL、metadata、comment、記事本文、API keyがargvとaudit argvに含まれず、本文はstdinだけにある。
5. 子process環境にはallowlist、選択credential、互換API選択時の正規化済み`ANTHROPIC_BASE_URL`だけが入り、
   他providerのsecret、OAuth token、選択しなかったcredential、custom headerがない。
6. 正しい`structured_output`を正規化でき、欠損、非object、Schema違反、末尾garbageを拒否する。
7. usageの欠損をunknownで保持し、`total_cost_usd`を確定請求額として扱わない。互換APIのbillingと
   costはunknownのままにする。
8. auth、rate limit、unavailable、protocol、execution、timeoutを区別する。
9. API key、Bearer token、authorization header、base URL、user path、本文、prompt、tool outputを
   errorと監査からredactする。
10. timeout、failure、interruptの後に子processと一時directoryを残さない。
11. `max_parallelism=1`を超えてClaude Code processを同時起動しない。
12. backend ID、model ID、endpoint fingerprint、prompt/schema versionが異なる結果を再利用しない。
13. OpenAI、Manus、Codex backendの既存契約試験が変更なしで通る。
14. `ANTHROPIC_BASE_URL`未設定時は公式endpointを選び、子processへbase URLを渡さず
    `billing_mode=metered-api`とする。
15. 互換API URLを正規化し、remote HTTP、userinfo、query、fragmentを拒否し、loopback HTTPだけを許可する。
16. 互換APIでは`ANTHROPIC_API_KEY`と`ANTHROPIC_AUTH_TOKEN`の片方だけを渡し、transportを監査する。
17. 互換APIでは明示されたgateway model IDを使い、可変aliasと不正文字列を拒否する。
18. 同じbackend・model・本文でもendpoint fingerprintが異なればcache hitしない。

認証済み環境だけで実行するopt-in統合試験は次を満たす。

- 対応versionのClaude Code CLIからSchema適合した要約を取得できる。
- prompt injectionを含む記事でもBash、Read、Edit、browser、MCP、subagentを利用できない。
- 実行後にClaude Codeのsession、prompt history、記事本文を永続化しない。
- timeoutまたは中断後にClaude Codeとその子processを残さない。
- API keyがstdout、stderr、audit、作成noteへ出ない。
- opt-inの互換API環境が指定された場合、正規化したbase URLへ接続し、gateway固有modelで要約できる。

opt-in試験はAPI課金が発生することを明示し、credentialがない場合は理由付きでskipする。

### 検証コマンド

```powershell
python -m pytest tests/test_llm_backends.py tests/test_local_agent.py tests/test_ingest.py
python -m pytest
```

実CLI試験は専用markerまたは環境変数で明示的に有効化し、通常のCIでは実行しない。

### 実装とcommitの順序

書き換え前の`5b67645`は草案、実装、`.env.example`、`DESIGN.md`を同じ`feat:` commitに含めていた。
この混在commitは公開branchの履歴に残さず、次のcommit topologyに組み直す。

1. 確定した本仕様だけを`docs:` commitにする。
2. review済みの最終案に従って実装を修正する。
3. code、test、`.env.example`、`DESIGN.md`を同じ実装commitにする。
4. `git show --stat <spec commit>`で仕様commitが本文書だけを含むことを確認する。

### 根拠

- [Claude Codeの環境変数](https://code.claude.com/docs/en/env-vars)
- [Claude CodeのLLM gateway設定](https://code.claude.com/docs/en/llm-gateway)
- [Claude Codeの非対話実行](https://code.claude.com/docs/en/headless)
- [Claude Code CLIリファレンス](https://code.claude.com/docs/en/cli-usage)
- [Claude Code model設定](https://code.claude.com/docs/en/model-config)
- [Claude model IDとversioning](https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions)
- [Claude Sonnet 5](https://platform.claude.com/docs/en/about-claude/models/whats-new-sonnet-5)
- [LLMバックエンド抽象化](20260816-llm-backends.ja.md)

## 改訂

### 改訂1 — Codex (2026-08-21)

人間によるレビュー4を受け、Claude Code CLIからAnthropic Messages互換APIへ接続する契約を最終案へ
追加した。

#### 対象箇所1 — 対象endpointと認証

（前）

> `ANTHROPIC_API_KEY`を使ったClaude Code CLIの非対話実行を追加する。初版で有効にする認証・課金の
> 組合せは`api-key` / `metered-api`だけとする。Claude Code以外のgatewayは対象外とする。

（後）

> `ANTHROPIC_BASE_URL`が空ならAnthropic公式endpointへ接続し、設定時は検証・正規化したAnthropic
> Messages互換APIへ接続する。公式endpointは`ANTHROPIC_API_KEY`、互換APIは
> `ANTHROPIC_API_KEY`または`ANTHROPIC_AUTH_TOKEN`のどちらか一方で認証する。

理由は、Claude Codeが公式に`ANTHROPIC_BASE_URL`によるgateway接続と、`x-api-key` / Bearerの2種類の
credential transportを提供しており、企業gatewayやローカル互換APIを別backendなしで日常利用できるため
である。

#### 対象箇所2 — modelと課金

（前）

> Anthropic APIの固定model IDだけを受け付け、組込み既定modelを`claude-sonnet-5`、
> `billing_mode`を`metered-api`とする。

（後）

> 公式endpointでは固定Anthropic model IDを使う。互換APIでは明示されたgateway固有model IDを許可し、
> 組込み既定値を使わない。互換APIの料金体系は推測せず`billing_mode=unknown`とする。

理由は、base URLはmodelを選択せず、gatewayごとにmodel IDと課金体系が異なるためである。可変aliasは
引き続き拒否し、同じ設定名が将来別modelへ変わることによる誤再利用を防ぐ。

#### 対象箇所3 — 結果再利用と監査

（前）

> backend ID、model、prompt/schema version、language、生成設定、入力fingerprintを再利用境界にする。

（後）

> 正規化済みbase URLのSHA-256である`endpoint_fingerprint`をlogical request、再利用境界、backend
> metadataへ追加する。URLとcredentialそのものは保存しない。

理由は、同じbackend IDとmodel名でも接続先が違えば意味、実装、データ取扱いが異なり、結果を共有すると
保存データの正しさを損なうためである。

#### 対象箇所4 — 安全条件と検証

（前）

> 子processへ`ANTHROPIC_API_KEY`だけを追加し、CI必須の受け入れ条件を13件とする。

（後）

> 選択したcredentialと、互換APIの場合だけ正規化済み`ANTHROPIC_BASE_URL`を子processへ渡す。remote
> endpointはHTTPSに限定し、HTTPはloopbackだけを許可する。userinfo、query、fragmentを拒否し、
> endpoint、credential、model、cache分離を含む受け入れ条件を18件とする。

理由は、credential漏洩、誤接続、内部endpointの監査露出、endpointをまたぐcache再利用を記事送信前に
防ぐためである。

根拠は[Claude Codeの環境変数](https://code.claude.com/docs/en/env-vars)、
[Claude CodeのLLM gateway設定](https://code.claude.com/docs/en/llm-gateway)、
[Claude Code model設定](https://code.claude.com/docs/en/model-config)である。

### 改訂2 — Codex (2026-08-21)

#### 対象箇所 — 実装とcommitの順序

（前）

> 現在の`5b67645`は草案、実装、`.env.example`、`DESIGN.md`を同じ`feat:` commitに含めているため、
> 公開前にcommit topologyを組み直す。本文書のレビュー完了と人による確定後、本仕様だけを`docs:` commitにし、
> review済みの最終案に従ったcode、test、`.env.example`、`DESIGN.md`を実装commitにする。

（後）

> 書き換え前の`5b67645`は草案、実装、`.env.example`、`DESIGN.md`を同じ`feat:` commitに含めていた。
> この混在commitは公開branchの履歴に残さない。確定した本仕様だけを`docs:` commitにし、review済みの
> 最終案に従ったcode、test、`.env.example`、`DESIGN.md`を後続の実装commitにする。

理由は、仕様確定と履歴整理を実行した後にも「現在のcommit」と書かれていると、確定文書が既に解消した
作業状態を現在形で指し続けるためである。決定したcommit境界は維持し、実行前の状態だけを履歴上の事実へ
直した。

証拠は`git show --stat 5b67645b41f0ac8f5e1d156cdc36db669c97bd01`であり、同commitに本仕様、
`feedian/llm_backends.py`、`.env.example`、`DESIGN.md`が含まれていた。

## 草案

## 背景と目的

`feedian` で Claude Code CLI を利用する場合、現在は「ローカルセッション（ログイン）方式」のみが実装されている。本仕様では API キー認証方式を追加し、以下を実現する:

- **多環境対応**: CI/CD や Docker コンテナなど、Interactive TTY が限定的な環境でも `CLAUDE_API_KEY` を経由して利用可能にする
- **スループット向上**: API キー方式は Codex 並の並列実行（4 コア）をサポートし、ローカルセッションより高い処理能力を提供する
- **使い勝手の拡張**: `claude-code-api` backend ID で直接 API キーのみで動作するモードを提供

## 現状

[`llm_backends.py:L544`](feedian\llm_backends.py:L544) に `ClaudeCodeLocalBackend` が定義されているが、現在は以下:

```python
class ClaudeCodeLocalBackend:
    auth_mode = "local-session"  # ログイン必須
    def preflight(self):
        raise BackendPolicyError("unavailable until verified")
```

## 設計方針

### 1. `ApiBackend` を継承して API キー認証をサポート

[`ApiBackend`](feedian\llm_backends.py:L127) は既に:
- `__init__(api_key_name=..., model_name=...)` で API キー名とモデル名を受け付ける
- `_api_key` を保持し、各呼び出しで認証を確認する構造を持っている

これを `ClaudeCodeLocalBackend` が継承し、2 つのモードを実装する:

| モード | 認証手段 | executable | 使用ケース |
|---|---|---|---|
| `local-session` | Codex login / auth.json | `codex` | Interactive TTY を有する環境 |
| `api-key` | `CLAUDE_API_KEY` env var | `claude` | CI/CD、Docker など |

### 2. Backend ID の拡張

既存の backend:
```python
BACKEND_IDS = (
    "openai-responses",   # ✅
    "manus-api",          # ✅
    "codex-local",        # ✅
    "claude-code-local",  # ⚠️ ローカルセッションのみ（未実装）
)
```

追加:
```python
BACKEND_IDS = (
    ...,
    "claude-code-local",   # Codex-like local agent (login)
    "claude-code-api",     # API キー認証モード
)

BACKEND_ALIASES = {
    "openai": "openai-responses",
    "manus": "manus-api",
}
```

### 3. URL の扱い方

現在 [`sync_vault`](feedian\sync.py:L57) と [`ingest`](feedian\cli.py) は既に `item.url` を保持し、[`fetch_page_text`](feedian\extract.py:L352) で HTTP を経由している。これを利用する:

```python
# 現状（既に機能）
sync_vault()         # vault から item.list → fetch_page_text(url, ...)
ingest file.json     # local file → item.url に保存

# これに追加
ingest --url https://...    # direct URL fetch (via sync policy)
```

## 仕様詳細

### `ClaudeCodeLocalBackend` の変更

```python
class ClaudeCodeLocalBackend(ApiBackend):
    """
    Claude Code CLI backend. Supports two auth modes:
    - "local-session": Codex-like ephemeral agent session (codex executable)
    - "api-key": Direct invocation via CLAUDE_API_KEY environment variable
    """

    capabilities = BackendCapabilities(
        backend="claude-code-local",
        execution_kind="local-agent",
        auth_mode="local-session|api-key",  # ← 両方をサポート
        billing_mode="subscription",
        max_article_chars=10_000,
        usage_available=True,
        max_parallelism=4,
    )

    def __init__(
        self,
        *,
        runner: ProcessRunner | None = None,
        executable: str = "claude",  # ← API キーモード用の CLI 名
        model_name: str = "",
        api_key_name: str = "CLAUDE_API_KEY",  # ← デフォルトの env var 名
    ):
        super().__init__(
            backend="claude-code-local",
            provider="anthropic",
            api_key_name=api_key_name,
            model_name=model_name or os.environ.get("CLAUDE_MODEL", ""),
            max_article_chars=self.capabilities.max_article_chars,
            usage_available=self.capabilities.usage_available,
            max_parallelism=self.capabilities.max_parallelism,
        )
```

#### `preflight()` の実装

API キーモード時は認証チェックのみで、ローカルセッションモードでは Codex 同様に検証:

```python
def preflight(self) -> dict[str, Any]:
    """Verify auth mode and setup the executable path."""

    # API key モード：env var が存在するかの確認
    if self.capabilities.auth_mode == "api-key":
        if not os.environ.get(self.api_key_name):
            raise BackendAuthError(
                f"Missing {self.api_key_name} environment variable"
            )

    metadata = {
        "executable": self._resolve_executable(),
        "auth_mode": self.capabilities.auth_mode,
    }

    return dict(metadata)
```

#### `summarize()` の実装

既存の Codex 実装を流用し、ローカルセッションのみで動作:

```python
def summarize(self, ...) -> BackendAudit:
    """Execute Claude Code CLI in ephemeral mode.

    Only "local-session" auth_mode permits this path. The API key mode uses the
    shared fetch_page_text() to prepare payloads for stdin consumption.
    """

    # ローカルセッションのみ許可
    if self.capabilities.auth_mode != "local-session":
        raise BackendPolicyError(
            f"{self.backend} requires local-session auth, got: {self.capabilities.auth_mode}"
        )

    # Codex 実装をそのまま使用
    def command(schema_path: Path) -> tuple[str, ...]:
        return (
            self._resolve_executable(),
            "exec", "--ephemeral",
            "--model", model or self.default_model(),
            "--output-schema", str(schema_path),
            "-",
        )

    # 既存の parse_codex_events() を使用
    local = run_isolated_local_agent(
        command=command, stdin_text=prompt, ...
    )

    return BackendAudit(...)
```

### `get_backend()` の拡張

新しい backend ID で `ApiBackend` 経由で直接 API キー認証モードを有効にする:

```python
def get_backend(value: str) -> LLMBackend:
    if backend == "codex-local":
        return CodexLocalBackend()

    # Claude Code API キーモード（新）
    if backend == "claude-code-api":
        return ClaudeCodeLocalBackend(
            executable="claude",  # コマンド名
            model_name=os.environ.get("CLAUDE_MODEL", ""),
            api_key_name="CLAUDE_API_KEY",  # デフォルト env var
        )

    if backend == "anthropic-api":  # アリバブ別 API キー（別機能）
        return ApiBackend(
            provider="anthropic",
            api_key_name="ANTHROPIC_API_KEY",
            model_name=os.environ.get("CLAUDE_MODEL", ""),
            max_parallelism=4,
        )

    return ClaudeCodeLocalBackend()  # デフォルトは local-session モード（ローカルのみ）
```

### `summarize_from_url()` の追加機能

`ingest --url` と同期で利用する HTTP fetch:

```python
def summarize_from_url(
    self,
    *,
    url: str,  # direct URL argument (not item.url)
    policy: FetchPolicy,
):
    """Fetch and summarize content directly from a URL.

    Used by: ingest --url https://..., sync_vault with fetch_pages=true
    The backend delegates HTTP fetching to extract.fetch_page_text(), then
    summarizes the fetched payload.
    """

    # 共用の policy を経由して fetch (feedian の設計原則に従う)
    page = fetch_page_text(
        url=url,
        policy=policy,
        max_chars=self.capabilities.max_article_chars,
    )

    if not page.text:
        raise BackendProtocolError(f"fetch for {url} yielded no text")

    item = {"url": str(url)}  # minimal context

    return self.summarize(
        model=model or self.default_model(),
        item=item,
        page=page,
        language="en",  # fetch_page_text() で検出される言語を使う
        **_timeout_kwargs,
    )
```

## 使用方法

### API キー設定（`.env.example`）

```bash
# Existing
OPENAI_API_KEY=your-openai-api-key
MANUS_API_KEY=your-manus-api-key

# New: Claude Code API キーモード用
CLAUDE_API_KEY=your-claude-api-key          # default env var
CLAUDE_MODEL=gpt-5.6-terra                  # optional, default via model mapping
```

### CLI での利用

#### API キーモード

```bash
# Claude Code を直接使う
claude-code-local summarize \
    --model gpt-5.6-terra \
    --url https://example.com/bookmark.json

# ingest コマンドに追加 --url フラグ:
feedian ingest \
    --model claude-code-api:gpt-5.6-terra \
    --url https://example.com/feed  # direct URL fetch
```

#### ローカルセッションモード（既存）

```bash
# Codex-style login 必須
codex login          # auth.json を作成

feedian ingest \
    --model codex-local:gpt-5.6-terra \
    https://example.com/bookmark.json
```

### Backend 経由での利用

```python
from feedian.llm_backends import get_backend, PageFetchResult

# API キーモード
backend = get_backend("claude-code-api")  # CLAUDE_API_KEY を使用
result = backend.summarize(...)

# ローカルセッションモード
backend = get_backend("claude-code-local")  # codex-style login 必須
result = backend.summarize(...)
```

## 影響範囲

- [`feedian/llm_backends.py`](feedian\llm_backends.py): `ClaudeCodeLocalBackend` のクラス定義と `get_backend()` を拡張
- `.env.example`: `CLAUDE_API_KEY` と `CLAUDE_MODEL` の文を追加
- [`docs/reviews/`](docs\reviews\): 実装完了後にコードレビューを行う

## リスク

| リスク | 対応策 |
|---|---|
| API キーが漏洩 | CI/CD で secrets manager から読み込む、`.env` を `.gitignore` に含める |
| `local-session` と `api-key` の混乱を招く可能性 | `get_backend()` で明示的に mode 指定し、デフォルトはローカルセッションのみ動作 |
| URL fetch と API キーの組み合わせが混同される | `summarize_from_url()` を経由したのみで、直接 URL パラメータを受け付けないようにする |

## 検証コマンド

```bash
# API キーモードでの動作確認
CLAUDE_API_KEY="..." feedian ingest --model claude-code-api:gpt-5.6-terra \
    "https://example.com/bookmark.json"

# ローカルセッションでの動作確認
codex login
feedian ingest --model codex-local:gpt-5.6-terra \
    "https://example.com/bookmark.json"

# backend 一覧
python -c "from feedian.llm_backends import get_backend; print(get_backend('claude-code-api'))"
```

## レビュー

### レビュー1 — Codex (2026-08-21)

#### 結論

**要修正**。Claude Code CLIをAPIキーで非対話実行できるようにする目的は採用できるが、現案は
Claude Code CLI、直接Anthropic API、Codex CLIという3つの実行経路を混同している。このままでは
記載された環境変数、モデル、コマンドが動作せず、既存の確定仕様が分離した`backend`、
`auth_mode`、`billing_mode`も正しく監査できない。

修正の中心は次の2点である。

1. Claude Code CLIを使う限りbackend IDは`claude-code-local`のままとし、初版は安全条件と両立する
   `api-key` / `metered-api`だけを有効化する。`local-session` / `subscription`は安全条件を満たす
   別の実行契約が実証されるまで利用不可とする。直接HTTP APIを追加するなら、予約済みの
   `anthropic-api`を別adapterとして別仕様で定義する。
2. Claude Code固有の認証、モデル、非対話CLI、構造化出力、安全隔離を公式契約に合わせ、Codexの
   argvとevent parserを流用しない。

#### 指摘

##### 1. `claude-code-api`は実行経路を表さず、CLIと直接APIを混同する — 重大度: 高

草案は`claude-code-api`を追加しながら、実体には`ClaudeCodeLocalBackend`と`claude` executableを
使うとしている（57–63行、184–209行）。これは直接Anthropic APIではなく、APIキーで認証した
ローカルClaude Code CLIである。一方、`ApiBackend`の継承案（32–43行、86–123行）は
`execution_kind="http"`、`auth_mode="api-key"`、`billing_mode="metered-api"`を設定する既存クラスの
意味と衝突する（`feedian/llm_backends.py:127-224`）。

確定済みの[LLMバックエンド抽象化](20260816-llm-backends.ja.md)は、backendを実行経路として一意にし、
`claude-code-local`をローカルCLI、`anthropic-api`を将来の直接APIとして分離している
（同文書77–80行、173–200行）。APIキーはbackend IDではなくauth modeの差である。

**判定: `claude-code-api`と`ApiBackend`継承は不採用。** `claude-code-local`にAPIキー認証を追加する。
直接APIが目的なら独立した`AnthropicApiBackend`を設計する。両者を同じクラスへ押し込まない。

##### 2. 認証変数、モデル変数、モデル例、ログイン方式がClaude Codeの契約と一致しない — 重大度: 高

草案の`CLAUDE_API_KEY`、`CLAUDE_MODEL`、`gpt-5.6-terra`、`codex login / auth.json`という組合せ
（40–43行、106–118行、193–205行、253–303行）はClaude Codeでは使えない。公式の
[環境変数一覧](https://code.claude.com/docs/en/env-vars)ではAPIキーは`ANTHROPIC_API_KEY`、モデルは
`ANTHROPIC_MODEL`である。[モデル設定](https://code.claude.com/docs/en/model-config)は`sonnet`、
`opus`、`haiku`等のaliasまたはAnthropicの完全なmodel IDを受け付ける。`gpt-5.6-terra`は
Claude Codeのモデルではない。また、local sessionはClaude Code自身のログイン情報を使い、
Codexの`auth.json`や`codex login`を使わない。

**判定: 修正して採用。** `ANTHROPIC_API_KEY`と`ANTHROPIC_MODEL`へ修正し、既定モデルは
Claude Codeが解決できるaliasまたは検証済みの完全IDとする。Claude CodeのログインとCodexの
ログインを別物として記述し、`supports_model()`の受け入れ条件も定める。

##### 3. Codexのargvとevent parserはClaude Codeへ流用できない — 重大度: 高

草案の`claude exec --ephemeral --output-schema ...`と`parse_codex_events()`（148–181行）は
Codex CLIの契約であり、Claude Code CLIの契約ではない。Claude Codeの公式
[非対話実行](https://code.claude.com/docs/en/headless)と
[CLIリファレンス](https://code.claude.com/docs/en/cli-usage)では、非対話実行は`claude -p`、
構造化出力は`--output-format json --json-schema <schema>`、session非永続化は
`--no-session-persistence`で指定する。返却JSONもCodex JSONL eventとは異なり、構造化結果は
`structured_output`に入る。

確定仕様も、process runnerだけを共有し、command構築、安全設定、event形式、usage解析は
backend固有に残すと決定している（`20260816-llm-backends.ja.md:196-200`）。

**判定: Codex実装のそのままの流用は不採用。** Claude Code専用のcommand builder、response parser、
process error分類、usage/cost正規化を実装し、共通化は`LocalAgentRunner`までに限定する。

##### 4. `auth_mode="local-session|api-key"`は監査値として無効である — 重大度: 高

草案はcapabilityへ集合を文字列連結した`auth_mode="local-session|api-key"`と固定の
`billing_mode="subscription"`を置く（96–104行）が、`preflight()`では
`auth_mode == "api-key"`を判定する（125–145行）。この比較は成立せず、APIキー利用時の
従量課金も`subscription`として記録される。

確定仕様は`auth_mode`を`api-key` / `local-session` / `unknown`、`billing_mode`を
`metered-api` / `subscription` / `unknown`のいずれかとし、preflightが今回のrunで解決した
単一値を返すと定めている（`20260816-llm-backends.ja.md:462-466`）。公式仕様上も、
`ANTHROPIC_API_KEY`が存在すると非対話実行ではログイン済みでもAPIキーが優先される。

**判定: 修正して採用。** 対応可能な組合せと今回解決した値を別に表現し、監査・見積り・再利用へは
単一の実効値を渡す。初版は`api-key` / `metered-api`だけを許可し、選択したキーだけを子processの
allowlistへ加える。`local-session` / `subscription`は安全な実行契約を別途実証するまで拒否する。

##### 5. local-agentの必須安全条件がcommand例から欠落している — 重大度: 高

草案のcommand例（166–179行）にはbare mode、全tool無効化、session非永続化、user/project設定の
遮断、CLI capability/version確認がない。Claude Codeのbare modeだけでもBash、Read、Edit toolは
残るため、記事中のprompt injectionからfilesystemやshellへ到達できる。また、公式の
[非対話実行](https://code.claude.com/docs/en/headless)ではbare modeはOAuth資格情報とsystem keychainを
読まないと明記されており、草案が同時に約束する`local-session` / `subscription`とも両立しない。

確定仕様は、非対話、bare mode、全tool無効化、session非永続化、JSON、JSON Schemaを同時に満たす
CLI versionだけを許可し、記事送信前にpreflightで拒否することを必須としている
（`20260816-llm-backends.ja.md:248-252`、454–484行）。

**判定: 修正して採用。** `--bare --tools ""`、`--no-session-persistence`、分離された一時cwd、
環境変数allowlist、stdout/stderrのsize上限とredaction、対応CLI機能のpreflightを仕様へ明記する。

##### 6. `max_parallelism=4`には根拠がなく、確定仕様の初期値1に反する — 重大度: 中

草案はAPIキー方式なら「4コア」でスループットが上がるとし（15–17行）、backendの最大並行度を4に
固定する（96–104行）。認証方式を変えてもローカルprocessの安全性、rate limit、費用上限が自動的に
4並列対応になるわけではない。測定結果も受け入れ条件も示されていない。

確定仕様はlocal-agentの初期`max_parallelism`を1とし、session再利用や常駐processは別仕様なしに
導入しないと決定している（`20260816-llm-backends.ja.md:454-458`）。

**判定: 4並列は不採用。** 初期値1を維持し、実CLI統合試験、rate limit、費用表示、停止時の課金、
マシン負荷を測定した別の判断でのみ引き上げる。

##### 7. URL ingestは本件と無関係で、現在のCLI・責務分離とも一致しない — 重大度: 中

草案は`ingest --url`とbackend上の`summarize_from_url()`を追加する（71–82行、212–249行）が、
現在の`feedian ingest`はDBに保存済みのresourceからsource noteを作るcommandであり、URL引数も
`--url`も持たない（`feedian/cli.py:162-186`、`feedian/ingest.py:164-374`）。page fetchはbackendの
手前で完了し、backendは既に`item`と`page`を共通契約で受け取る。例示した
`--model claude-code-api:gpt-5.6-terra`も、backendとmodelを別optionにする現行CLI契約に反する。

**判定: 本仕様では不採用。** URL入力と`summarize_from_url()`を対象外へ戻す。必要なら別仕様で
sourceの保存、fetch policy、重複排除、失敗記録まで含むend-to-end契約を決める。利用例は
`feedian ingest --backend claude-code-local --model sonnet`の形にする。

##### 8. 影響範囲と検証条件が不足している — 重大度: 中

影響範囲は`llm_backends.py`と`.env.example`だけを挙げる（306–310行）が、少なくともClaude Code
専用parser、子process環境allowlist、秘密情報redaction、CLIのモデル環境変数解決、fake runnerの
backend契約試験が必要である。新backend IDを追加する案を維持するなら、registry、CLI choices、
Vault configのallowlist、fallback、fingerprint、監査表示も変更対象になる。検証コマンド
（320–334行）は構文例だけで、認証優先順位、安全隔離、構造化出力、timeout、cleanup、redactionを
確認しない。

**判定: 修正して採用。** 影響範囲を実際の責務単位で列挙し、CI必須のfake runner試験と、認証済み
環境だけで行うopt-in統合試験を受け入れ条件として追加する。APIキーがaudit、argv、stderr、保存済み
raw responseへ出ない試験を必須にする。

##### 9. 仕様のライフサイクルとcommit topologyが規約に反している — 重大度: 高

文書は`ステータス: 草案`のまま空の`最終案`と`改訂`を持ち（3–9行）、タイトルにも禁止されている
「仕様書」を含む（1行）。さらに`git show --stat 5b67645`では、この草案、実装、`.env.example`、
`DESIGN.md`が`feat: Claude Code API キー認証対応`という同一commitに含まれている。草案はレビュー中に
commitせず、人が最終案を書いて確定した後に仕様だけを`docs:` commitとし、その後の実装commitで
`DESIGN.md`を更新するという本repositoryの規約を満たしていない。

**判定: 要是正。** レビュー結果を反映した新しい最終案は人が作成し、空の`最終案`・`改訂`節は
確定前には置かない。公開前のbranchでcommit topologyを規約どおりに組み直し、仕様commit単独で
`git show --stat <spec commit>`を確認してから実装へ進む。

#### 採否まとめ

| 項目 | 採否 | 理由 |
|---|---|---|
| Claude Code CLIのAPIキー認証を追加する | 修正して採用 | 日常運用とCIには有用だが、公式の認証変数と実行契約へ合わせる必要がある |
| `claude-code-api`を新設する | 不採用 | 認証方式であって実行経路ではなく、`claude-code-local`と監査・再利用境界が重複する |
| `ClaudeCodeLocalBackend`を`ApiBackend`から継承する | 不採用 | local processとHTTP transportのcapability、実行、error分類を混同する |
| Codexのcommandとevent parserを流用する | 不採用 | Claude Code固有のargvとresponse schemaに一致しない |
| 実効auth/billing modeをpreflightで解決する | 修正して採用 | 初版は`api-key` / `metered-api`の単一組合せだけを許可する |
| 初期4並列 | 不採用 | 確定仕様の初期値1を覆す測定根拠がない |
| URL ingestを同時追加する | 不採用 | 認証対応とは独立したsource/fetch機能であり、現行CLI契約にも存在しない |
| fake runnerとopt-in実CLI試験を追加する | 採用 | 安全条件、認証、構造化出力を再現可能に検証するために必要である |

#### 推奨する次の草案の境界

- 対象は`claude-code-local`のClaude Code専用adapterとAPIキー認証に限定する。
- backend IDは維持し、初版の実効値を`api-key` / `metered-api`に限定する。subscription認証は
  bare modeと両立する安全な方法が実証されるまで利用不可とする。
- 認証は`ANTHROPIC_API_KEY`、モデルは`ANTHROPIC_MODEL`または`--model`で解決する。
- `claude -p --bare --tools "" --no-session-persistence --output-format json --json-schema ...`を基礎に、
  Claude Code専用JSON parserを用いる。
- 初期`max_parallelism`は1とし、URL ingestと直接`anthropic-api`は対象外とする。
- fake runner契約試験をCI必須、実CLIのprompt injection・session非永続化試験をopt-inとする。

### レビュー2 — Codex (2026-08-21)

レビュー1の全指摘を採用し、人の指示により`最終案`へ反映した。ステータスは追加レビューを受けられる
ように`レビュー中`とする。レビュー1に記載した草案の行番号は、`最終案`挿入前の行番号である。
参照対象は各指摘に併記した識別子とcode例でも特定できるため、草案とレビュー1は書き換えない。

| 指摘 | 採否 | 最終案での対応 |
|---|---|---|
| 1. CLIと直接APIの混同 | 採用 | backend IDを`claude-code-local`に維持し、`ApiBackend`継承と`anthropic-api`実装を対象外にした |
| 2. 認証変数・model・loginの誤り | 採用 | `ANTHROPIC_API_KEY`、`ANTHROPIC_MODEL`、固定ID`claude-sonnet-5`へ統一した |
| 3. Codex argv/parserの流用 | 採用 | Claude Code専用command builder、JSON parser、error classifierを必須にした |
| 4. auth/billingのunion文字列 | 採用 | 初版の実効値を`api-key` / `metered-api`の単一組合せに限定した |
| 5. local-agent安全条件 | 採用 | bare、全tool無効化、Chrome無効化、session非永続化、一時config、環境allowlist、version gateを定めた |
| 6. 根拠のない4並列 | 採用 | `max_parallelism=1`とし、並列引上げを対象外にした |
| 7. URL ingestの混入 | 採用 | URL入力と`summarize_from_url()`を対象外にし、既存の保存済みresource契約を維持した |
| 8. 影響範囲・検証不足 | 採用 | 変更対象、13件のCI受け入れ条件、5件のopt-in統合条件を定めた |
| 9. 文書・commit lifecycle違反 | 採用 | タイトルとstatusを修正し、commit topologyの是正手順を最終案に明記した。commit再構成自体は未実施 |

### レビュー3 — Claude Code (2026-08-21)

#### 結論

**承認（確定可能）**。レビュー1および2を経て修正された`最終案`を検証した。
草案段階で問題視された「直接APIとの混同」「Codex CLI契約の誤流用」「安全条件の欠落」「根拠のない並列数」「無関係なURL ingest機能の混入」が適切に整理・削除され、確定済みのLLMバックエンド仕様（`20260816-llm-backends.ja.md`）およびClaude Code CLIの公式仕様・安全原則に完全に合致する設計となっている。

本仕様の最終案に従って実装を進めることを承認する。人の指示によりステータスを`確定`へ変更した後、明記された手順通りコミットトポロジーを整えて実装へ移行されたい。

#### 検証・確認事項

##### 1. 実行経路とバックエンド抽象化の適合性

`claude-code-local` backend IDを維持し、`ApiBackend`や`CodexLocalBackend`を継承せず独立した`LLMBackend`プロトコル実装とした点は、実行経路を一意にし、`auth_mode`（`api-key`）と`billing_mode`（`metered-api`）で認証・課金区分を管理するバックエンド抽象化原則と完全に一致している。

##### 2. Claude Code CLI 非対話実行と安全隔離の妥当性

`claude -p --bare --tools "" --no-chrome --no-session-persistence --max-turns 1 --output-format json --json-schema ... --model ... --effort low` による引数構成、プロンプト・記事本文の`stdin`限定渡し、環境変数の厳格なallowlist（`ANTHROPIC_API_KEY`と隔離用`CLAUDE_CONFIG_DIR`等）、および記事由来の内容を含む`stdout`をエラー分類に使用しない（`stderr`のみ使用）原則により、Prompt Injection攻撃に対する安全性が十分に担保されている。

##### 3. バージョンゲートと固定モデルIDポリシー

`claude --version` によるバージョン検証（`2.1.205`以上`3.0.0`未満）と、`supports_model()` での可変エイリアス（`sonnet`, `opus` 等）の拒否・固定モデルID（`claude-sonnet-5` 等）への強制は、構造化出力の互換性確保および将来のモデル変更に伴う過去結果の誤再利用を防ぐために適切である。

##### 4. 実装時の留意事項: スキーマ渡しの引数サニタイズ

`feedian/local_agent.py` の `run_isolated_local_agent` は現在ファイルパス型のスキーマ（`--output-schema <path>`）を想定したサニタイズを行っている。Claude Code CLIはインラインJSON文字列（`--json-schema <schema>`）を受け取るため、サニタイズ処理においてスキーマ文字列を `<schema>` に安全に置換し、`audit_argv` に不用意な長大文字列や誤認識が含まれないよう実装側で整合性を確保すること。

##### 5. コミットトポロジーの是正手順

コミット `5b67645` に草案・初期実装・`DESIGN.md` が混在している問題に対し、最終案278–289行目に「1. 人による最終案確認と確定」「2. 仕様書単独の `docs:` コミット」「3. 最終案に準拠した実装・テスト・`DESIGN.md`の統合コミット」という是正手順が明示されていることを確認した。

#### 採否まとめ

| 項目 | 採否 | 理由 |
|---|---|---|
| `最終案` の設計・契約 | 採用 | 安全隔離・認証・バージョン制限・監査仕様が規約通り網羅されている |
| `claude-code-local` への限定 | 採用 | 実行経路と認証モードの分離原則に適合 |
| CI用fake runner & opt-in実CLIテスト | 採用 | 回帰防止と実際のCLI動作確認の両立が可能 |
| コミットトポロジー是正手順 | 採用 | 規約に準拠した履歴管理を実現できる |

### レビュー4 — t (2026-08-21)

#### 指摘

人間の利用者から、Anthropic公式APIだけでなく、Anthropic Messages API互換のゲートウェイやローカルサーバーも `claude-code-local` から利用できるようにしてほしいとの変更要求があった。互換API対応は次の契約を満たす必要がある。

1. 新しいバックエンドIDや直接HTTP実装は追加せず、既存の `claude-code-local` と Claude Code CLI の `ANTHROPIC_BASE_URL` を利用する。
2. 公式エンドポイントでは `ANTHROPIC_API_KEY` を使い、互換APIでは `ANTHROPIC_API_KEY`（`x-api-key`）または `ANTHROPIC_AUTH_TOKEN`（Bearer）のどちらか一方を使えるようにする。両方の指定は曖昧なので拒否する。
3. 互換APIではゲートウェイ固有のモデルIDを明示指定させ、既定モデルや可変エイリアスに依存しない。
4. 互換APIの利用料金はFeedianから確定できないため、課金種別とコストを `unknown` として扱う。
5. 異なる互換API間で生成結果を再利用しないよう、正規化したベースURLのフィンガープリントを論理リクエストと保存メタデータへ含める。ただしベースURL自体は保存しない。
6. リモートの互換APIはHTTPSのみ許可し、HTTPはループバックに限定する。userinfo、query、fragmentを含むURLは拒否する。
7. 子プロセスには選択した認証情報だけを渡し、未選択の認証情報、カスタムヘッダー、その他の秘密情報を引き継がない。

#### 採否

| 指摘 | 採否 | 理由 |
|---|---|---|
| `ANTHROPIC_BASE_URL` による互換API対応 | 採用 | Claude Code CLIが提供するゲートウェイ経路を再利用でき、Feedian側に別のHTTPバックエンドを増やさずに要件を満たせるため。最終案と改訂1へ反映した。 |
| APIキーとBearerトークンの排他的な認証選択 | 採用 | 代表的な互換ゲートウェイの認証方式を扱いつつ、どの認証情報を送るかを決定的にできるため。最終案と改訂1へ反映した。 |
| 互換APIでの明示的なモデル指定 | 採用 | ゲートウェイごとにモデル名と利用可能モデルが異なり、Feedianが安全な既定値を決められないため。最終案と改訂1へ反映した。 |
| 互換APIの課金・コストを `unknown` とする | 採用 | Feedianからゲートウェイ固有の料金体系を検証できず、公式Anthropic APIと同じ従量課金だと誤表示しないため。最終案と改訂1へ反映した。 |
| エンドポイント単位の再利用分離 | 採用 | 同じモデル名でもエンドポイントごとに実体や応答が異なり得る一方、URLそのものの永続化は秘密情報や内部構成を漏らす可能性があるため。最終案と改訂1へ反映した。 |
| ベースURLと子プロセス環境の安全制約 | 採用 | 平文通信、URL内資格情報、意図しない秘密情報の継承を防ぐため。最終案と改訂1へ反映した。 |

### レビュー5 — Claude Code (2026-08-21)

#### 結論

**承認（確定可能）**。人間（t）によるレビュー4の指摘、およびそれを受けて追記された「改訂1」と「最終案」の更新内容を再検証した。
Anthropic Messages API互換ゲートウェイ・ローカルエンドポイントへの接続機能が、Feedianのセキュリティ原則、監査整合性、データ完全性（再利用境界の厳格な分離）を保ったまま適切に整理されている。

本仕様の最終案に従って仕様を確定し、実装へ進むことを承認する。

#### 検証・確認事項

##### 1. エンドポイント検証とSSRF・平文通信対策の妥当性

`ANTHROPIC_BASE_URL` に対し、リモート接続時のHTTPS強制、HTTPのループバック（`localhost`, `127.0.0.0/8`, `::1`）限定、`userinfo` / `query` / `fragment` の拒否、および scheme/host の小文字化・末尾スラッシュ除去の正規化を Preflight 段階で義務付けている。これにより、平文通信による意図しない資格情報の露出や、不正な内部ネットワークへのアクセス（SSRF）が安全に防止されている。

##### 2. `endpoint_fingerprint` による結果再利用境界の隔離とプライバシー保護

URL文字列そのものを保存・監査せず、正規化済みURLの SHA-256（`endpoint_fingerprint`）を論理リクエスト・再利用境界・メタデータに含める設計は、内部ホスト名やルーティングパスの漏洩を防ぎつつ、異なるエンドポイント間での結果誤再利用を確実に防止できる。未設定時（公式エンドポイント）にも固定フィンガープリントを割り当てて分離を徹底している点が優れている。

##### 3. 認証情報の排他的選択と子プロセス環境の隔離

公式エンドポイント（`ANTHROPIC_API_KEY`）と互換API（`ANTHROPIC_API_KEY` または `ANTHROPIC_AUTH_TOKEN` のいずれか1つ）の排他制御が明確に定義され、未指定や複数指定を Preflight で拒否する契約により、CLIへの意図しない資格情報の露出やヘッダ衝突を回避できている。また、子プロセス環境には選択した資格情報と正規化済みURLのみを渡し、未選択の環境変数や `ANTHROPIC_CUSTOM_HEADERS`、gateway model discovery 等を引き継がない点も安全である。

##### 4. モデル指定規則と課金モードの扱い

互換APIでは Feedian 側で安全な既定モデルを仮定せず、`--model` / `ANTHROPIC_MODEL` / Vault設定からの明示指定を必須化（未指定時は `BackendPolicyError`）とし、ゲートウェイ固有モデルID（1〜200文字、`[A-Za-z0-9._:/@-]`）のみ許可し可変エイリアス（`sonnet` 等）を拒否するルールは、誤実行・誤再利用を防ぐために適切である。また、互換APIの課金体系を推測せず `billing_mode=unknown` およびコスト `unknown` として記録する設計は、従量課金との誤認を防ぎ正確である。

##### 5. CI受け入れ条件（18項目）の網羅性

互換API接続の正規化、資格情報の単一選択、ゲートウェイモデルID検証、およびエンドポイントフィンガープリント不一致時のキャッシュ分離（項目14〜18）が CI 受け入れ条件として追加されており、回帰防止のためのテスト要件が十分に定義されている。

#### 採否まとめ

| 項目 | 採否 | 理由 |
|---|---|---|
| `ANTHROPIC_BASE_URL` 互換API対応 | 採用 | `claude-code-local` の実行経路を維持しつつゲートウェイ利用が可能 |
| `endpoint_fingerprint` による再利用境界分離 | 採用 | URL漏洩防止とキャッシュ混同防止を両立 |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` 排他選択 | 採用 | 認証情報の曖昧性を排除し、安全にCLIへ伝達可能 |
| 互換APIでの明示モデル必須化・可変エイリアス拒否 | 採用 | モデルIDの将来の変動による誤再利用を防止 |
| 互換APIの `billing_mode=unknown` | 採用 | 不正確な従量課金表示・見積りを防止 |

### レビュー6 — Codex (2026-08-21)

#### 結論

**表現を修正して承認（確定可能）**。レビュー5の承認結論と、互換API対応に関する最終案の契約は採用する。ただし、レビュー5が述べたセキュリティ上の保証には、最終案が実際に定めた対策より強い表現が2点ある。レビュー5は履歴として変更せず、本レビューで保証範囲を訂正する。最終案の変更は不要である。

#### 指摘

##### 1. URL検証だけではSSRF防止を保証できない

レビュー5の「エンドポイント検証とSSRF・平文通信対策の妥当性」は、HTTPS強制、HTTPのループバック限定、`userinfo` / `query` / `fragment` の拒否によって「不正な内部ネットワークへのアクセス（SSRF）が安全に防止されている」と評価した。

これらの規則は、リモートendpointへの平文送信やURL内credentialを防ぐ。一方で、HTTPSのprivate IP、DNS rebinding、接続時に解決されたIP、redirect先は制限していないため、SSRF防止そのものは保証しない。ただし、`ANTHROPIC_BASE_URL`は記事などの非信頼入力から生成されず、信頼された運用者が設定する構成値である。したがって最終案にSSRF対策を追加するのではなく、保証を「設定ミスによる平文送信とURL内credentialの防止」に限定する。

##### 2. URLのSHA-256は秘密性を保証しない

レビュー5の「`endpoint_fingerprint` による結果再利用境界の隔離とプライバシー保護」は、正規化済みURLのSHA-256によって内部host名やrouting pathの漏洩を防げると評価した。

`endpoint_fingerprint`はURLの平文保存を避け、異なるendpoint間の結果再利用を分離する。一方で、saltを使わない決定的なhashであるため、候補URLを推測できる者は照合でき、URLの秘密性までは保証しない。最終案の目的は秘密化ではなく、平文URLを保存しないことと再利用境界を決定的に分離することである。

#### 採否

| 指摘 | 採否 | 理由 |
|---|---|---|
| レビュー5の承認結論 | 採用 | 2点はいずれもレビュー表現の保証範囲に関する訂正であり、互換API対応の契約や確定可能という判断を覆さないため。 |
| HTTPS強制とURL検証 | 修正して採用 | 平文送信とURL内credentialを防ぐ対策として採用する。SSRF防止を保証するという評価は、private IP、DNS、redirectの制御がないため採用しない。 |
| `endpoint_fingerprint` による再利用境界分離 | 修正して採用 | 平文URLの非保存とendpoint単位の再利用分離として採用する。hashがURLの秘密性を保証するという評価は採用しない。 |
| 最終案への追加修正 | 不採用 | 最終案はSSRF防止やhashによる秘密性を契約しておらず、修正対象はレビュー5の評価表現だけであるため。 |
