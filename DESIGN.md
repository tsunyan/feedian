# Feedian design

## LLM backends

`ingest` のLLM実行は、安定したバックエンドID、モデル、認証方式、課金方式を別々の値として扱う。詳細な判断理由と受け入れ基準は [LLMバックエンド抽象化](docs/specs/20260816-llm-backends.ja.md) を参照する。

- `openai-responses` を既定、`manus-api` を既存の安定バックエンドとして登録する。
- `codex-local` は実験的なopt-inバックエンドである。記事ごとの一時ディレクトリ、標準入力、無効化したユーザー設定とルール、ネットワーク・ツール・シェル・一時ディレクトリ外の読み取り禁止を証明できる場合だけ実行する。現在のCLI構成では必要な隔離を証明できないため、実プロセスを起動する前にポリシーエラーとなる。
- `claude-code-local` は将来用に予約するが、CLI契約と隔離ポリシーが定義されるまで利用不可とする。
- Vault設定はformat version 2で`llm.backend`、`llm.model`、`llm.fallback`を持つ。version 1からは`feedian migrate`による明示移行が必要で、fallbackは既定で無効とする。
- SQLite schema version 6の`llm_run`はbackend、canonical schema version、fingerprint version、auth/billing mode、実装メタデータ、所要時間を監査情報として保存する。
- 再利用キーはbackend境界を越えない。旧fingerprintは移行期間中だけ検索し、再利用した記事の書き込み時にversion 2へ昇格する。計画は読み取りのみで、`--dry-run`は書き込まない。
- 再利用キーはrequest全体のハッシュであるため、要約スキーマを変更すると移行期間中の旧キー検索が無言で一致しなくなる。`tests/test_ingest.py`が旧キーを実測値で固定している。
- プロバイダーへ要求するスキーマはタグを1個以上とするが、保存前の正規化は長さと要素数を切り詰めて救済し、空の`tags`と`content_type`を許容する。必須の`note_title`と`summary`の欠落だけを失敗とする。
- source noteのfrontmatterは互換性のため`model`のみを維持する。backendの識別は`llm_run`監査記録で行う。
