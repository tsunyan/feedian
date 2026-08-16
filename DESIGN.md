# Feedian design

## LLM backends

`ingest` のLLM実行は、安定したバックエンドID、モデル、認証方式、課金方式を別々の値として扱う。詳細な判断理由と受け入れ基準は [LLMバックエンド抽象化](docs/specs/20260816-llm-backends.ja.md) を参照する。

- `openai-responses` を既定、`manus-api` を既存の安定バックエンドとして登録する。
- `codex-local` は実験的なopt-inバックエンドである。記事ごとの一時ディレクトリ、標準入力、無効化したユーザー設定とルール、ネットワーク・ツール・シェル・一時ディレクトリ外の読み取り禁止を証明できる場合だけ実行する。現在のCLI構成では必要な隔離を証明できないため、実プロセスを起動する前にポリシーエラーとなる。
- `claude-code-local` は将来用に予約するが、CLI契約と隔離ポリシーが定義されるまで利用不可とする。
- Vault設定はformat version 2で`llm.backend`、`llm.model`、`llm.auth_mode`を持つ。version 1からは`feedian migrate`による明示移行が必要で、fallbackは既定で無効とする。
- SQLite schema version 6の`llm_run`はbackend、canonical schema version、fingerprint version、auth/billing mode、実装メタデータ、所要時間を監査情報として保存する。
- 再利用キーはbackend境界を越えない。旧fingerprintは移行期間中だけ検索し、ヒット時にversion 2へ昇格する。
- プロバイダー出力は正規化後にcanonical summary schema version 1で検証する。必須タイトル・要約の欠落は失敗とし、空の`tags`と`content_type`は許容する。
- source noteのfrontmatterは互換性のため`model`のみを維持する。backendの識別は`llm_run`監査記録で行う。
