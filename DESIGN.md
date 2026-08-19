# Feedian design

## LLM backends

`ingest` のLLM実行は、安定したバックエンドID、モデル、認証方式、課金方式を別々の値として扱う。詳細な判断理由と受け入れ基準は [LLMバックエンド抽象化](docs/specs/20260816-llm-backends.ja.md) を参照する。

- `openai-responses` を既定、`manus-api` を既存の安定バックエンドとして登録する。
- `codex-local` は実験的なopt-inバックエンドである。Vaultや既存Git projectの外に記事ごとの一時cwdを作り、projectの`AGENTS.md`と`.codex/config.toml`から切り離す。さらにMCP設定を空で上書きし、既定で有効な全ツールを`--disable`で名指しで落とす。Codex CLIには一括の無効化スイッチがなく、`--sandbox read-only`は書き込みを禁じるだけで読み取りは許すため、隔離はこのdenylistが成立していることに依存する。
- denylistは測定したCLIバージョンでしか成立しない。`preflight`が`codex --version`と`codex login status`をingest runごとに1回確認し、未検証versionと未ログインは記事を送信する前に拒否する。検出したバージョンと無効化した機能名は`llm_run`の監査へ残す。
- local-agentのargvには制御情報だけを置き、記事本文は標準入力で渡す。監査用argvは実行ファイルとSchemaのローカルpathを除去して保存する。タイムアウト時はプロセスツリー全体を終了する。
- `codex-local`は`CODEX_HOME`をFeedian専用の`~/.feedian/codex-home`へ向ける。`--ignore-user-config`が対象とするのは`config.toml`だけで、同じディレクトリの`AGENTS.md`とskillsは読まれ続けるためである。認証はそのhomeで利用者が一度`codex login`を実行して用意し、既定の`~/.codex`は変更しない。keyring保存だと既定homeと認証を共有し得るので、`cli_auth_credentials_store="file"`をlogin確認と実行の双方でCLI overrideとして渡す。preflightはhomeに`AGENTS.md`、`plugins`、`rules`、`hooks`、`memories`、および`skills/.system`以外のskillが無いことを確認する。
- 子processの環境変数はallowlistで組み立て、version検出・login確認・`codex exec`へ同一の環境を渡す。providerのAPI keyは渡さない。
- **既知の制約:** CLI内蔵のsystem instructionsと内蔵skillsカタログは`CODEX_HOME`では除去できない。利用者由来の指示はすべて隔離済みである。
- `claude-code-local` は将来用に予約するが、CLI契約と隔離ポリシーが定義されるまで利用不可とする。
- Vault設定はformat version 2で`llm.backend`、`llm.model`、`llm.fallback`を持つ。version 1からは`feedian migrate`による明示移行が必要である。
- fallbackは既定で無効であり、有効化にはbackendとmodelの両方を明示する。実行前のプラン画面に宛先を表示し、無効なら`disabled`と示す。切り替わるのは`BackendUnavailableError`、`BackendRateLimitError`、`BackendTimeoutError`のときだけである。認証、ポリシー、プロトコルの失敗は設定または実装の不具合であり、別backendへの課金で覆い隠さない。fallbackの実行は宛先backendの`llm_run`として別に記録するため、監査上どちらが要約を作ったかが残る。
- SQLite schema version 6の`llm_run`はbackend、canonical schema version、fingerprint version、auth/billing mode、実装メタデータ、所要時間を監査情報として保存する。新規runの`request_json`は成否にかかわらず`logical`と`actual`の固定envelopeを使う。
- 再利用キーはbackend境界を越えない。旧fingerprintは移行期間中だけ検索し、再利用した記事の書き込み時にversion 2へ昇格する。計画は読み取りのみで、`--dry-run`は書き込まない。
- 再利用キーはrequest全体のハッシュである。旧キーは凍結した`LEGACY_V1_PROVIDER_SCHEMA`から組み立てるため、`PROVIDER_OUTPUT_SCHEMA`を変更しても移行期間中の検索は壊れない。`tests/test_ingest.py`が旧キーを実測値で固定し、この分離も検証する。
- providerへ要求する`PROVIDER_OUTPUT_SCHEMA`とFeedianが受理する`CANONICAL_SUMMARY_SCHEMA`は別オブジェクトである。前者はタグを1個以上求めるが、後者は空の`tags`と`content_type`を許容する。既存結果の再利用を優先するためであり、厳格化する場合はcanonical schema versionを上げる。
- 処理順序は「provider出力の解析 → 許容された正規化 → canonical schema検証」である。正規化は前後空白の除去、型強制、長さと要素数の切り詰めだけを救済し、検証に失敗した結果はsource noteにも成功runにもならない。
- source noteのfrontmatterは互換性のため`model`のみを維持する。backendの識別は`llm_run`監査記録で行う。

## Syncのモード

`feedian sync`はquickとfullの2モードを持ち、**CLIの既定はquick**である。判断理由と却下した代替案は [syncのquickモード](docs/specs/20260818-sync-quick-mode.ja.md) を参照する。

- quickは未取得のものだけを同期する。対象は「(A) `source_item`が無い新規item」と「(B1) URLを持ち`resource.current_revision_id`がNULLのresource」の和集合である。既知かつ本文取得済みのitemに対しては`upsert_canonical_item`も`should_fetch_resource`もコメント照会も行わない。未変更のVaultで毎回発生していたHatenaのブックマーク件数照会（件数の1/20のリクエスト）が消える。
- モードは実行開始時に一意に決まり、実行中に変更しない。最終full runからの経過日数などを理由とする自動昇格は行わない。完全同期の周期はOS scheduler、cron、`feedian run`といった外部のスケジュールへ委ねる。利用者が指定したモードと異なる処理を内部判断で実行しないためである。
- 既定の反転は**CLI層に限定する**。`sync_vault`の`quick`既定は`False`のままである。`_run_pipeline`はkeyword引数なしで`sync_vault`を呼ぶため、関数既定を反転させると週次runが黙ってquickになる。
- `--force-fetch`と`--force-comments`はfull専用であり、`--full`が無ければCLI errorとする。forceの「既知itemを強制的に再取得する」はquickの「既知itemに触れない」と正面から矛盾するため、暗黙の優先順位を設けない。
- ページ取得が失敗したときは`fetch_capture`だけを記録し、空本文の`resource_revision`を書かない。従来は失敗しても空revisionが書かれてresourceが「取得済み」に見え、`ingest`が本文なしで要約を作って課金していた。失敗記録は現在のrevisionが空本文であれば`resource.current_revision_id`をNULLへ正規化するが、`resource_revision`行は削除しない（`llm_run`など5つの外部キーが参照する）。同じ判定を`reextract`にも適用する。
- RaindropとHatenaは収集を早期に打ち切る。Raindropは`sort=-created`が作成日時の降順であることをAPIが定義している。Hatenaは検索APIの返却順を全件走査で実測し、6,987行・69ページ境界のすべてで`timestamp`が非増加であることを確認した（[syncとingestのスループット](docs/specs/20260819-sync-ingest-throughput.ja.md)）。契約は厳密な降順ではなく非増加である。同一`timestamp`のブックマークが実在するためである。Hatenaの打ち切り判定は検索クエリ（`https`と`http`）ごとに独立して持つ。新規ブックマークがどちらの全文検索トークンに載るかは事前に分からず、片方が1ページで止まりもう片方が走査を続けるのは正常な状態である。新着ゼロの実測で71リクエストが2リクエスト、5分10秒が約9秒になる。RSSはETag / Last-Modifiedによる条件付き取得が既にあるため変更しない。過去日付を保持したRaindropの一括インポートは降順の深い位置に着地するため、quickでは検出できない。Hatenaでは同一URLの再ブックマークが`timestamp`を更新して先頭へ移動するため、1回のsync間隔に100件以上の再ブックマークが挟まると新着を取りこぼし得る。復旧経路は`--full`であり、`fetch.quick_stop_after_known_pages`を上げれば緩和できる。
- (B1)はproviderの新着列挙から分離したDB駆動のpassであり、provider APIへのリクエストを要さない。本文取得だけを行い、provider metadata更新とコメント照会は伴わない。1つのresourceを複数のsource itemが共有する（同じ記事をRaindropとHatenaでブックマークした場合）ため、取得は1回だけ行い`sync_run_item`は全参照元へ記録する。`--limit`は(A)と(B1)の共有予算とし(A)を優先する。候補は最新`fetch_capture.fetched_at`の昇順で処理し、予算内で同じ先頭群だけが選ばれ続けることを防ぐ。
- SQLite schema version 7の`sync_run`は`mode`を保存する。`_due_providers`は`mode='full'`のrunだけを見るため、quickを繰り返しても`feedian run`の完全同期がdueでなくなることはない。
- quickだけを実行し続けると、provider側のmetadata差分、Hatenaコメントの増減、`refresh_days`到達による再取得、取得失敗本文の復旧が反映されない。これらは自動補正せず、完全同期の定期実行を案内する。

## 本文取得の再試行抑制

取得に失敗し続けるresourceは、再試行の間隔が伸びるか、まったく再試行されなくなる。判断理由と却下した代替案は [本文取得の再試行抑制](docs/specs/20260818-fetch-retry-suppression.ja.md) を参照する。

- 機構は2つある。**指数backoff**は連続失敗回数`n`に応じて`min(retry_base_minutes * 2^(n-1), retry_max_days)`まで待機を伸ばし、一時的な障害を自動的に復帰させる。**終端ステータス**（既定`[404, 410]`）は経過時間によらず再試行しない。
- 判定は`should_fetch_resource`にあり、**quickとfullの双方に効く**。`--full`は「既知itemも含めて全件処理する」という指示であって、「到達不能と分かっているURLを叩き直す」という指示ではない。復旧経路は`--force-fetch`のみで、新しいフラグは追加していない。
- 404を永久に再試行しない根拠は実測にある。参照Vaultの404は1,885件で、2020年以前のブックマークは23.0%が404、2024年以降は13.1%だが**その95%が`x.com`**である。`x.com`はログインを要求して404を返すため、リンクが消えたのではなく構造的に取得できない。`x.com`を除くと直近の404率は0.7%であり、永久に再試行しない対象が日常運用で積み上がることはない。
- **取得済みの本文は失われない。** 一度成功したresourceは、その後URLが404になっても本文を保持する。`record_failed_fetch`は非空の本文を持つrevisionに触れない。この保証の上に「取れた時のものが残るなら、後で404になっても構わない」という判断が成立する。
- `fetch_page_text`はHTTP起因の失敗にも`http_status`を設定する。browser fallbackが失敗した場合は**元のstatus**（401/403/406）を残す。失敗の原因を示すのは元のstatusであり、fallback側の内部statusではない。DNS解決不能・タイムアウト・その他の例外では`http_status`はNULLのままである。このうちDNS解決不能とタイムアウトは`failure_kind`として記録し、後述の終端規則が扱う。それ以外は指数backoffが扱う。
- 状態遷移は3経路ある。失敗は`consecutive_failures`を加算し、非空本文の成功は0へ戻し、304も0へ戻す。304は「保持中の本文がサーバー上の最新と同じ」と確認できた成功であり、これを失敗として数えると、失敗していない回数が次回のbackoffへ持ち越される。成功時に呼び出し側のwarningを消さないのは、RSSフォールバックが本文とページ取得エラーを同時に記録し、その理由をraw noteの`## Fetch Warning`が示すためである。**この1点だけは確定仕様の状態遷移表と異なる。** 仕様は成功時に`warning`をNULLにすると定めたが、判定はいずれも本文長0を必須条件とするため機構上は不要であり、消すとフォールバックの由来が失われる。逸脱の経緯は [実装のコードレビュー](docs/reviews/20260818-fetch-retry-suppression-implementation.ja.md) にある。
- SQLite schema version 8の`fetch_capture`は`consecutive_failures`と`http_status`を持つ。`retry_after`列は設けない。`fetched_at`と失敗回数から算出でき、列を持つと算出規則と保存値の二重管理になる。
- 移行行（`consecutive_failures = 0`）は指数式の定義域外である。本文が無くwarningがある移行行は**即座にdue**とし、本文がある行は`refresh_days`に従う。移行後の最初の1回は従来どおり全件を取得し、その1回で`http_status`と失敗回数が記録される。既存warningからの解析によるbackfillは行わない。
- `feedian status`は`unreachable:`として、現在のVault configの終端規則（終端ステータス、または終端種別と連続失敗回数。後述のとおり両者の和集合）に該当し本文を持たないresource数を表示する。抑制されたresourceが黙って消えないようにするためである。

## 到達不能ホストの取得コスト

サービス終了したドメインと応答しないホストへの取得を日常の`feedian sync`から取り除く。判断理由と却下した代替案は [到達不能ホストの取得コスト](docs/specs/20260819-unreachable-host-cost.ja.md) を参照する。

- `fetch_capture.failure_kind`に`"dns"`（ホスト解決不能）と`"timeout"`（接続・応答timeout）を記録する。SSL失敗や抽出失敗は種別を持たずNULLのままで、既存の指数backoffに任せる。
- 抑制規則は「`failure_kind`が`terminal_failure_kinds`に含まれ、かつ`consecutive_failures`が`terminal_kind_failures`（既定3）以上」であり、種別は問わない連続失敗回数の条件である。終端ステータス（404/410）の規則と和集合で`should_fetch_resource`・`terminal_failure_count`の双方に効く。復旧は`--force-fetch`のみ。
- 取得タイムアウトは`fetch.timeout_seconds`（既定5秒）で設定できる。browser fallback（401/403/406の代替取得、低品質HTMLの再描画）は別枠の`fetch.browser_timeout_seconds`（既定30秒）を使う。
- SQLite schema version 9の`fetch_capture`は`failure_kind`列を持つ。移行時に`consecutive_failures >= 2`の既存行を1へ戻す。**1は観測値ではなく基準値である。** 旧規則の下で稼いだ回数を持ち込ませないための正規化であり、これにより移行後の失敗2回で既定の閾値3へ達する。移行直後の一時的な失敗1回では終端化しない。0にしないのは、`consecutive_failures = 0`が「schema 7からの移行行」を意味し、全件が即dueになるためである。
