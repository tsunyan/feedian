# 本文取得の再試行抑制実装のコードレビュー

ステータス: 完了
対象: `859c6bb` feat: stop retrying fetches that will not succeed（このコミットの親）
仕様: [本文取得の再試行抑制](../specs/20260818-fetch-retry-suppression.ja.md)
レビュー者: Codex (2026-08-18)

## 結論

全テスト340件とRuffは成功したが、確定仕様の状態遷移に反する指摘が1件、不正な設定を黙って隠す指摘が1件、schema 7から8への実移行値を検証していない指摘が1件ある。指摘1は実際のstoreで「404失敗→HTTP以外の失敗」を再現し、最新warningがDNS失敗であるにもかかわらず`http_status=404`が残り、十分な時間経過後も`should_fetch_resource=False`になることを確認した。

レビュー時点の実装は`859c6bb`として確定した。指摘の修正と本文書は、そのコミットの直後の1コミットにまとめてある。

## 指摘

### 1. HTTP以外の最新失敗に過去の終端statusが残る — 重大度: 中

**根拠:** `feedian/store.py:507-517`、仕様書`:101-109`

**現象:** `record_failed_fetch`のUPDATEは`http_status = COALESCE(?, http_status)`としている。そのため、最初に404を記録し、`--force-fetch`で再試行した最新の失敗がDNS・SSL・timeoutなど`http_status=None`であっても、captureの`http_status`は404のまま残る。最小再現では`warning='DNS failure', http_status=404, consecutive_failures=2`となり、`fetched_at`を十分過去にしても`should_fetch_resource(...)=False`となった。

`tests/test_store.py:1179-1195`は、後続の`http_status=None`の後も404を保持することを正しい契約として固定しており、確定仕様の「HTTP以外の失敗ではNULL」と逆である。

**影響:** 確定仕様は`http_status`を最新失敗の状態とし、HTTP以外の失敗ではNULLと定めている。過去の404が残ると、最新失敗は機構Aのbackoff後に再試行すべきなのに、機構Bが恒久的に抑制する。`--force-fetch`が成功するまで自動復帰できない。

**提案:** UPDATEでもINSERTと同じく`http_status = ?`とし、渡されたNULLをそのまま最新状態として保存する。「404→`force=True`でHTTP以外の失敗→backoff期限後」に再取得可能になる回帰テストを追加する。

### 2. `feedian status`が不正な再試行設定を黙って無視する — 重大度: 低

**根拠:** `feedian/cli.py:238-245`、仕様書`:133-149`

**現象:** `_status`は`fetch_retry_settings(config)`の`ValueError`を捕捉し、`terminal_http_statuses=None`として続行する。その場合、commandは成功扱いで終了し、仕様が必須とする`unreachable:`行だけが表示されない。

**影響:** `terminal_http_statuses=404`や`[99]`といった誤設定を、利用者は`feedian status`で検出できない。後で`sync`を実行したときに初めて失敗し、statusの可視性要件も満たさない。

**提案:** この`try/except ValueError`を外し、不正なconfigを他の利用経路と同じく拒否する。誤設定で`status`が失敗し、原因を示すテストを追加する。

### 3. schema 7から8への移行行の初期値を検証していない — 重大度: 低

**根拠:** 仕様書`:202-209`、`feedian/store.py:1938-1950`、`tests/test_store.py:1022-1040`

**現象:** `_migrate_v7_to_v8`は2列を追加するが、実在のschema 7 DBに既存`fetch_capture`行を置いてmigrationし、`consecutive_failures=0`と`http_status IS NULL`を確認するテストが無い。既存のv6 migrationテストは最終versionと8への到達は通るが、新規列の値を検査しない。

**影響:** 確定仕様の検証項目19を満たさない。schema版を8と記録しても、既存行の互換値が期待どおりかをテストが保証せず、将来migration SQLが変更されたときに初回due判定を壊し得る。

**提案:** schema 7のfixtureへwarning付きの既存captureを作り、`allow_migration=True`で開いた後にversion、2列の存在、既存行の0/NULL、その行が即時dueになることを1本の回帰テストで固定する。

## 採否

| # | 指摘 | 重大度 | 採否 | 状態 |
|---|---|---|---|---|
| 1 | 最新失敗のHTTP status | 中 | 採用 | 本コミットで修正 |
| 2 | statusの設定検証 | 低 | 採用 | 本コミットで修正 |
| 3 | schema 7から8への移行値 | 低 | 採用 | 本コミットで修正 |

3件とも実コードで再現条件を確認したうえで採用した。以下は採用にあたっての判断と、あわせて記録すべき仕様からの逸脱である。

### 指摘1の原因は実装者への指示にあった

`http_status = COALESCE(?, http_status)`は実装者の判断ではなく、**架構側の指示**である。「`http_payload_id`と`final_url`と同じように、渡されなかった値で既存を消さないこと」と伝えた。

この類推が誤っていた。payload idは失うと`reextract`の入力（PDF等の生バイト）が`delete_orphan_payloads`で回収されるため保全対象である。一方`http_status`は**最新の失敗が何であったか**を表す状態であり、保全対象ではない。過去の値を残すと、確定事項6の表が定める「渡された値（HTTP以外はNULL）」と逆になる。

帰結は指摘の指す恒久停止である。404で抑制されたresourceを`--force-fetch`で再試行し、その回がDNS失敗やtimeoutだった場合、captureは404のまま残る。最新の失敗は一時障害であり機構Aのbackoffで再試行されるべきなのに、機構Bが恒久的に抑制し続ける。**機構Aへ戻る経路が存在しない。**

UPDATEをINSERTと同じ`http_status = ?`へ揃えた。あわせて、逆向きの契約を固定していた`test_record_failed_fetch_stores_and_keeps_an_http_status_across_a_later_none`を差し替え、「404 → 一時失敗 → backoff期限後に再びdue」を確認する`test_a_transient_failure_after_a_terminal_one_becomes_due_again`を追加した。

### 指摘2は一貫性の問題でもあった

`try/except ValueError`は架構側が「診断コマンドが設定不備で落ちるのは本末転倒」として指示したものである。しかし`_status`は既に`load_vault_config(root)`を無防備に呼んでおり、**providersの誤設定では落ちるのに、retry設定の誤設定では落ちない**という一貫性のない状態だった。

握り潰しを外し、他の設定と同じ扱いに戻した。CLIの最上位ハンドラがエラーパネルを表示するため、利用者には原因が示される。

### 仕様からの逸脱: 成功時にwarningをNULLにしない

確定事項6の状態遷移表は、成功（非空本文の`record_resource_revision`）で`warning`をNULLにすると定めている。**実装はこれに従っていない。呼び出し側が渡したwarningをそのまま保存する。**

理由は2つある。

1. **機構上不要である。** `should_fetch_resource`の失敗分岐、`unfetched_resources`の候補判定、`terminal_failure_count`のいずれも本文長0を必須条件としている。本文を保持するresourceはwarningの値によらずこれらに到達しない。
2. **provenanceを壊す。** RSSフォールバックは`_store_page`より前に適用され、ページ取得が失敗したまま feed 本文が代わりに入る。このとき`record_resource_revision`は非空本文とページ取得エラーを同時に記録し、raw noteの`## Fetch Warning`がその理由を読者に示す。warningを消すと、なぜこの本文がページではなくfeedのものなのかが失われる。既存テスト`test_sync_keeps_embedded_rss_content_when_page_fetch_has_no_text`がこの表示を固定している。

清潔な成功では呼び出し側が`warning=None`を渡すため、古いwarningは従来どおり上書きされる。したがって「成功後に失敗時のwarningが残る」ことはない。

`record_not_modified_fetch`側（304）はNULL化したままとした。warningを渡す呼び出し側が存在せず、かつ304は「保持中の本文が最新」と確認できた成功であるため、失敗由来のwarningを残すと古い情報が表示され続ける。

確定仕様は編集しない。この逸脱と理由をここに記録する。

## 検証

- [x] `./.venv/Scripts/python.exe -m pytest -q` → 343 passed（レビュー時点340件＋回帰テスト3件）。
- [x] `./.venv/Scripts/python.exe -m ruff check feedian tests` → All checks passed!
- [x] 指摘1の最小再現 → `warning='DNS failure', http_status=404, consecutive_failures=2`、十分な経過時間後も`should_fetch_resource=False`。
- [x] `git diff --check` → 空白エラーなし。
- [x] 指摘1: UPDATEを`http_status = ?`へ変更。逆向きの契約を固定していたテストを差し替え、`test_record_failed_fetch_replaces_a_terminal_status_with_a_later_transient_one`と`test_a_transient_failure_after_a_terminal_one_becomes_due_again`で固定した。
- [x] 指摘2: `try/except ValueError`を除去。`test_status_rejects_an_invalid_terminal_http_statuses_config`で、`[99]`を書いた設定に対し`feedian status`がexit code 1を返しキー名を示すことを固定した。
- [x] 指摘3: `_downgrade_to_v7`を追加し、`test_v7_migration_backfills_retry_state_on_a_failed_capture`で、schema 7の既存capture行が移行後に`consecutive_failures=0`・`http_status IS NULL`となり、`fetched_at`が1分前でも即座にdueになることを固定した。
- [x] 回帰テストの有効性: store層の修正を差し戻して該当テストが失敗することを確認した。
- [x] レビュー対象コミットのhashとsummaryを「対象」へ記録した。

## 規約化した項目

なし。指摘1から3は今回の確定仕様に固有の実装不一致である。

ただし観察を1つ記録する。**指摘1と指摘2はいずれも架構側が実装者へ与えた指示に起因しており、実装者の判断ミスではない。** 既存コードの類推（payload idの保全）と、単独では妥当に見える配慮（診断コマンドを落とさない）が、確定仕様の状態遷移および既存コードの一貫性と衝突した。仕様が状態遷移を表で定めている箇所では、実装指示を書く前にその表と照合すること。同種の誤りが次のレビューでも現れた場合は、`AGENTS.md`へ規約として昇格する。
