# 保存・同期経路のコードレビュー

ステータス: 完了
対象: `66add54263587b8546b3abcee2d6442032e291ad` Merge pull request #35 from tsunyan/dependabot/github_actions/github-actions-4b2c77c676（このコミットの親。レビュー自体は`4cdcfd4`で行った。付け替えの経緯は「検証」に記す）
仕様: [本文取得の再試行抑制](../specs/20260818-fetch-retry-suppression.ja.md)、[syncのquickモード](../specs/20260818-sync-quick-mode.ja.md)
レビュー者: Codex (2026-09-05)

## 結論

保存済み本文を失う不具合2件と、件数制限付きのRSS同期の後に未保存の記事を取り込めなくなる不具合1件を、一時DBと外部応答のmockで再現した。3件とも採用し、本コミットで修正した。

- 指摘1は、RSSのフィード本文による代替を、例外経路と同じく現在のrevisionを持たないresourceに限って直した。本文を持つresourceのページ取得が失敗すると、本文はそのまま残り、失敗はcaptureにだけ記録される。
- 指摘2は、text/plainの抽出結果が空または空白だけのとき、HTTP取得と保存済みpayloadの再抽出の両方で`no extractable text found`のwarningを付けて直した。保存側の既存の判定（warningがあり本文が空なら失敗として記録する）がそのまま働き、保存済み本文を空文字で上書きせず、新規resourceに空revisionも作らない。
- 指摘3は、`--limit`がフィードの一部だけを保存したとき、そのフィードのitemをvalidatorなしで保存するようにして直した。次回はそのフィードを無条件に取得するので、制限を外した同期で未保存の記事を取り込める。新しい永続状態は追加していない。

レビューの再現7ケースを回帰テストとして残した。修正前の`66add54`では7件とも失敗し、修正後は成功する。全テストは651 passed、1 skipped、48 subtests passed、ruffも成功した。

レビューで主に確認した範囲は、sync、本文・payloadの保存、reextract、ingest、raw/Source出力、画像OCRの実行・保存、snapshot/restoreである。全ソースの全分岐を網羅したわけではない。

## 指摘

### 1. RSSのページ取得失敗が保存済み全文をフィード抜粋で上書きする — 重大度: 高

**根拠:** [sync.py:220](../../feedian/sync.py)〜223、[store.py:399](../../feedian/store.py)〜409。

ページ取得結果が空で、RSS itemに`embedded_content`があると、既存本文の有無を確認せずに`page.text`をフィード本文で置き換える。その後の`_store_page`は非空本文として`record_resource_revision`を呼ぶ。revisionは同じ行をUPDATEするため、元の全文はDB内の過去revisionとしても残らない。

**再現:** RSS itemの`embedded_content`を`Short RSS excerpt`とし、同じresourceに`Previously archived complete article`を保存する。最新captureの`fetched_at`を`2000-01-01T00:00:00+00:00`にして通常のfull同期で再取得対象にし、ページ取得を本文なしのHTTP 404または503にする。コメント取得は無効にする。

- 404・503のそれぞれで、`fetch.workers=1`と`8`の双方を実行した。
- 4ケースすべてで保存本文が`Short RSS excerpt`になり、revision数は1のままだった。
- `SyncReport.failed`は0となり、取得失敗による全文消失として通知されない。

**影響:** サイト消失や一時障害だけで、以前取得できた全文が短い抜粋に不可逆に置き換わる。本文を別providerと共有している場合、そのproviderの表示・要約入力にも影響する。

**仕様との関係:** 確定した[本文取得の再試行抑制・最終案:85](../specs/20260818-fetch-retry-suppression.ja.md)は「**取得済み本文は失われない。** 一度取得に成功したresourceは、その後URLが404になっても本文を保持し続ける」と定める。RSSフォールバックを新規resourceに使うことや、そのwarningを残すという既存の判断は否定しない。保存済み本文を持つresourceにも適用されてしまう点が問題である。

**修正方針:** 既存の非空本文があるときは、その本文を保持して失敗captureだけを更新する。フィード本文へのフォールバックは本文が未保存の場合に適用する。すでに例外経路の[sync.py:200](../../feedian/sync.py)には既存revisionを確認する条件があるので、返却値による失敗経路も同じ保護方針にそろえる。

### 2. 空のtext/plainを成功扱いして本文を消し、空revisionを残す — 重大度: 中

**根拠:** [extract.py:482](../../feedian/extract.py)〜496、[extract.py:721](../../feedian/extract.py)〜726、[sync.py:558](../../feedian/sync.py)、[reextract.py:43](../../feedian/reextract.py)。

HTTP取得と保存済みpayloadの再抽出は、`text/plain`が空または空白だけでも`error=None`で返す。一方、保存側の失敗分岐は`error`と空本文の両方を要求する。その結果、非空の保存済み本文が空文字に上書きされ、`current_revision_id`も埋まったままになる。

**再現:** 非空本文を保存したresourceについて、HTTP応答を`200`、`Content-Type: text/plain`、bodyを`b'   '`にする。DNS検証とopenerだけをmockし、実際の`fetch_page_text`と`_store_page`を呼ぶ。

- 抽出結果は`text=''`、`error=None`で、保存後の本文は`''`になった。
- 同じ空白payloadを参照したまま非空本文を再度保存し、`reextract_stored_resources`を実行しても、本文が`''`になった。
- 再抽出結果は`processed=1, changed=1, failed=0`で、`current_revision_id`はNULLにならなかった。

**影響:** 空応答や空白応答で保存済み本文が失われる。新規resourceの場合も空revisionができ、quick同期の通常の本文未取得候補から外れる。

**仕様との関係:** [syncのquickモード・最終案:61](../specs/20260818-sync-quick-mode.ja.md)は「抽出結果が空なら revision を書かない」とし、空revisionによって本文未取得候補から押し出される問題を解消する方針を定めている。保存側の`error and not text`という条件自体は最終案と一致するため、text/plain抽出側の失敗通知の不足として修正できる。

**修正方針:** text/plainでも、正規化後の本文が空なら抽出失敗warningを付ける。HTTP取得と再抽出の両方を対象にし、既存の非空本文が保存され続けることと、新規の空revisionを作らないことを検証する。

### 3. 件数制限付きRSS同期が未保存記事まで取得済みとして条件付き取得する — 重大度: 中

**根拠:** [sync.py:515](../../feedian/sync.py)〜516、[sync.py:537](../../feedian/sync.py)〜539、[sync.py:745](../../feedian/sync.py)〜763、[rss.py:75](../../feedian/rss.py)〜88。

RSSのETag/Last-Modifiedは取得した全itemのmetadataに付与される。しかしcollectorはその後`limit`でitemを切り、選択したitemだけを保存する。次回は保存済みitemのmetadataからフィード全体のvalidatorを読み、残りが未保存でも条件付き取得に使う。304が返るとRSS取得は空listになるため、制限を外したfull同期でも残りを取得できない。

**再現:** 2記事を含むRSSを用意し、HTTP応答に`ETag: v1`を付ける。2回目以降、`If-None-Match`がある場合だけ304を返すopenerを使い、実際のRSS parserとsyncを実行する。

1. 空DBに`source='rss', quick=True, limit=1, fetch_pages=False, fetch_comments=False`で同期する。
2. 同じDBで`quick=False`とし、`limit`を指定せずに再同期する。
3. 1回目の`processed`は1、2回目は0。DBの`source_item`は1件のままで、送信validatorは順に`None`、`v1`だった。

**影響:** 少量取り込んで確認した後に全件同期する通常の手順で、フィードに現在も存在する未保存記事が取り込まれない。`--full`でもフィードが更新されるまで回復しない。フィードからすでに消えた記事の完全収集を要求する指摘ではない。

**修正方針:** フィードの一部だけを保存した段階では、その応答validatorを次回の取得省略に使わない。少なくとも、件数制限を外したfull同期で未保存分を収集できることを保証する。新しい永続状態を追加する前に、制限時のvalidator保存・再利用条件を狭める方法を検討する。

## 採否

| ID | 判定 | 理由・現在の対応 |
|---|---|---|
| 20260905-1 | 採用 | 本コミットで修正。フィード本文による代替を、現在のrevisionを持たないresourceに限った（[sync.py:220](../../feedian/sync.py)〜223）。 |
| 20260905-2 | 採用 | 本コミットで修正。空のtext/plainにwarningを付けた（[extract.py:482](../../feedian/extract.py)〜489、[extract.py:730](../../feedian/extract.py)〜733）。 |
| 20260905-3 | 採用 | 本コミットで修正。件数制限で一部だけ保存したフィードのvalidatorを保存しない（[sync.py:556](../../feedian/sync.py)〜559）。 |

レビュー時点では3件とも保留としていた。不具合は再現済みだったが、依頼がレビューであり、修正の判断と実装を含まなかったためである。修正で下した判断を以下に残す。

**指摘1の判定条件。** 代替を許す条件は、例外経路（[sync.py:200](../../feedian/sync.py)）と同じ`_resource_has_revision`にした。非空本文の有無を調べる条件を新しく作らなかったのは、失敗の記録時に`record_failed_fetch`が空本文のrevisionをNULLへ正規化するので、現在のrevisionの有無は本文の有無とほぼ一致するからである。例外は[syncのquickモード](../specs/20260818-sync-quick-mode.ja.md)のC案より前に書かれた空revisionで、最初の失敗でNULLへ正規化され、次の同期からフィード本文が入る。そのときページを取得して失敗すれば代替が、backoffや終端ステータスで取得しなければページ取得を伴わない経路（[sync.py:238](../../feedian/sync.py)の`rss-feed`）が書く。どちらの場合も本文は失われない。本文を持つresourceでは、代替の代わりに`_store_page`が失敗として記録する。これは抽出結果が空なら必ずwarningが付くことに依存しており、text/plainでその前提が欠けていたのが指摘2である。

この条件には副作用が1つある。以前に代替で保存した抜粋を持つresourceは、ページ取得が失敗し続ける間、フィード側で更新された本文を取り込まなくなる。修正前は失敗のたびに代替が抜粋を書き直していた。保存済み本文が全文か抜粋かを区別するにはcaptureの`extracted_by`を読む分岐が要る。ページ取得をしない経路と例外経路はもともと既存revisionを上書きしないので、そちらに合わせて区別しなかった。

ページ取得の失敗のうち`SyncReport.failed`が数えるのは、従来どおり例外になったものだけである。返却値による失敗は他providerのページ取得失敗と同じく、captureの`warning`、`http_status`、`consecutive_failures`に記録される。指摘の「全文消失として通知されない」は、全文が消えなくなったことで解消したと判断した。

**指摘2の修正位置。** 修正は抽出側に置いた。保存側の判定は確定仕様（syncのquickモード、最終案）が定めたものであり、レビューの指摘どおり不足していたのは抽出側の失敗通知だった。修正後は、HTML、PDF、browser経路と同じく、どの形式でも空の抽出結果はwarningを伴う。失敗captureは空の応答のpayloadを指すため、以前の正常なpayloadは参照されなくなり`delete_orphan_payloads`の対象になる。本文は残るが、そのpayloadからの再抽出はできなくなる。修正前から同じ挙動であり、本コミットでは変えていない。

**指摘3の条件の狭め方。** 件数制限でitemを1件でも落としたフィードについて、保存するitemの`feed_etag`と`feed_last_modified`を空にする。`_existing_rss_feed_metadata`は同じフィードで最後に更新されたitemのmetadataを読むため、次回の取得は無条件になり、制限なしの同期がそのフィードの残りを保存した時点でvalidatorも戻る。判定はフィード単位であり、制限で切られなかったフィードはvalidatorを保つ。quickで既知itemしか返らない状態が続くと、新しいitemが届くまで無条件の取得が続く。代償はRSSの取得1回分であり、新しい永続状態を足して避けるほどではないと判断した。修正前の件数制限付き同期で既に304に隠されている既存DBは、この修正では自動的に回復しない。フィードが更新されて200が返るまで待つことになる。

## 検証

レビュー時点（Codex、2026-09-05）の記録。ここでの「対象欄のコミット」はレビュー時点の`4cdcfd4`を指す。

- `rtk proxy .venv/Scripts/python.exe -m pytest -q` — 636 passed / 2 failed / 1 skipped、48 subtests passed。
- 上記の失敗2件をサンドボックス外で個別再実行 — 2 passed。
  - `tests/test_local_agent.py::test_timeout_kills_the_grandchild_not_only_the_process_it_started`
  - `tests/test_main.py::MainTests::test_successful_llm_summary_records_usage_without_page_content`
- 追加の再現は`TemporaryDirectory`内のSQLiteだけを使用し、外部ネットワークとLLMを呼ばずに実行した。
- 指摘1はHTTPステータス2種類×worker数2種類の4ケース、指摘2は実際のHTTP本文抽出と保存済みpayload再抽出の2ケース、指摘3は実際のRSS parserを通した2回連続同期の1ケースで、上記の観測結果をassertした。7ケースすべてが現在の不具合を再現した。これは修正後の回帰テスト成功を意味しない。
- `git rev-parse HEAD`は対象欄のコミットと一致する。修正コミットは未作成のため、修正コミットの親との照合は未実施。
- 実装・既存テストは変更していない。レビュー文書は指摘中のため未コミットである。

修正後（Claude Code、2026-10-11）の記録。

- 対象の付け替え。修正ブランチは`origin/main`の先端`66add54`から切った。`4cdcfd4`からの差分は依存更新と`c730c7d` fix: decode snapshot subprocess output as utf-8であり、`git diff --stat 4cdcfd4 66add54`は`.github/workflows/codeql.yml`、`feedian/snapshots.py`、`requirements.txt`、`tests/test_snapshots.py`の4ファイルだけを示す。指摘の根拠にある`feedian/sync.py`、`store.py`、`extract.py`、`reextract.py`、`rss.py`は同一なので、指摘の行番号は`66add54`でもそのまま有効である。なお指摘2の根拠のうち、再抽出のtext/plain分岐は`extract.py:724`〜729、再抽出側の失敗判定は`reextract.py:46`にある。指摘本文の`extract.py:721`と`reextract.py:43`はその少し手前を指している。
- 回帰テストを修正より先に書き、`66add54`の実装に対して実行した。レビューの再現7ケースはすべて失敗し、観測値もレビューと一致した。指摘1の4ケースは保存本文が`Short RSS excerpt`になり、指摘2のHTTP取得は保存本文が`''`、再抽出は`processed=1, changed=1, failed=0`、指摘3は2回目の同期の`processed`が0だった。
- 追加したテストと、それぞれが確かめること。
  - 指摘1: `tests/test_sync.py::test_a_failed_page_fetch_keeps_the_stored_body_instead_of_the_feed_excerpt`。HTTP 404・503と`fetch.workers` 1・8の組み合わせで4ケース。実際の`fetch_page_text`を通し、DNS検証とopenerだけを差し替える。本文を持つresource 2件と持たないresource 1件を同じ同期で失敗させる。前者は本文、`current_revision_id`、`extracted_by`を保ったまま、captureの`warning`、`http_status`、`consecutive_failures`だけが更新される。後者は従来どおりフィード本文で代替される。workers=8では3件がthread poolで取得される。
  - 指摘2: `tests/test_sync.py::test_an_empty_text_plain_response_keeps_the_stored_body`（HTTP取得）と`tests/test_reextract.py::test_reextracting_an_empty_text_plain_payload_keeps_the_stored_body`（保存済みpayloadの再抽出）。どちらも本文と`current_revision_id`が保たれ、captureに`no extractable text found`が残る。再抽出の結果は`processed=1, changed=0, failed=0`になった。
  - 指摘2の補足: `tests/test_sync.py::test_an_empty_text_plain_response_writes_no_revision_for_a_new_resource`は、新規resourceに空revisionを作らず、(B1)の候補に残ることを確かめる。`tests/test_extract.py`の`test_whitespace_only_plain_text_is_a_failed_extraction`と`test_whitespace_only_stored_plain_text_is_a_failed_extraction`は抽出結果そのものを、`test_plain_text_with_content_still_extracts_cleanly`は非空のtext/plainがwarningなしのままであることを確かめる。
  - 指摘3: `tests/test_sync.py::test_a_limited_rss_sync_does_not_let_the_feed_validator_hide_unstored_items`。実際のRSS parserとsyncを通す。修正後は2回目の同期が2件を処理し、`source_item`が2件になる。送信validatorは`None`、`None`、`v1`の順であり、全itemを保存した後の3回目は304を受けて0件を処理する。条件付き取得そのものは止めていない。
  - 指摘3の補足: `tests/test_sync.py::test_a_limit_blanks_validators_only_for_the_feed_it_cut`は、2つのフィードのうち件数制限で切られた側だけがvalidatorを失い、全itemが選ばれた側は保つことを確かめる。
  - 補足のテストのうち`test_plain_text_with_content_still_extracts_cleanly`は修正前から成功する退行防止用であり、他は修正前のソースで失敗することを確かめた。
- `python -m pytest -q` — 651 passed、1 skipped、48 subtests passed。修正前の`66add54`は639 passed、1 skipped、48 subtests passedであり、増えた12件は追加したテストである。レビュー時にサンドボックスで失敗した2件は、今回の環境では全体実行でも成功し、個別再実行でも2 passedだった。
- `python -m ruff check feedian tests` — All checks passed。
- `git diff --check` — 問題なし。
- テストはメインチェックアウトの`.venv`（Python 3.12.14）を使い、`PYTHONPATH`をworktreeへ向けて実行した。worktreeの`feedian`が読み込まれることは、`feedian.__file__`と、ソースだけを修正前に戻すと新規テストが失敗することで確かめた。このvenvの依存の一部は`requirements.txt`の固定版より古い（trafilatura 2.2.0、pypdf 6.16.1など）が、`pyproject.toml`の下限は満たす。
- 本コミットの作成後、`git rev-parse HEAD^`が対象欄の`66add54263587b8546b3abcee2d6442032e291ad`と一致することを確かめた。

## 規約化した項目

なし。指摘1・2の保存済み本文保護は、既存の[AGENTS.md](../../AGENTS.md)と確定仕様がすでに要求している。同じ要求を新規ルールとして重複追加せず、未検証だった分岐を回帰テストで補うことを推奨する。

修正後の追記。未検証だった分岐の回帰テストは本コミットで追加した（「検証」の修正後の記録を参照）。規約化した項目は引き続きない。
