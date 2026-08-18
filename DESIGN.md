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
- Raindropのみ収集を早期に打ち切る。`sort=-created`が作成日時の降順であることをAPIが定義しているためである。Hatenaは検索APIの返却順に保証が無いため全件収集を維持し、RSSはETag / Last-Modifiedによる条件付き取得が既にあるため変更しない。過去日付を保持したRaindropの一括インポートは降順の深い位置に着地するため、quickでは検出できない。
- (B1)はproviderの新着列挙から分離したDB駆動のpassであり、provider APIへのリクエストを要さない。本文取得だけを行い、provider metadata更新とコメント照会は伴わない。1つのresourceを複数のsource itemが共有する（同じ記事をRaindropとHatenaでブックマークした場合）ため、取得は1回だけ行い`sync_run_item`は全参照元へ記録する。`--limit`は(A)と(B1)の共有予算とし(A)を優先する。候補は最新`fetch_capture.fetched_at`の昇順で処理し、予算内で同じ先頭群だけが選ばれ続けることを防ぐ。
- SQLite schema version 7の`sync_run`は`mode`を保存する。`_due_providers`は`mode='full'`のrunだけを見るため、quickを繰り返しても`feedian run`の完全同期がdueでなくなることはない。
- quickだけを実行し続けると、provider側のmetadata差分、Hatenaコメントの増減、`refresh_days`到達による再取得、取得失敗本文の復旧が反映されない。これらは自動補正せず、完全同期の定期実行を案内する。
