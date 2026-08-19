# syncとingestのスループット実装のコードレビュー

ステータス: 完了
対象: `0947088` docs: let a finalized specification be corrected when it is superseded（このコミットの親）
仕様: [syncとingestのスループット](../specs/20260819-sync-ingest-throughput.ja.md)
レビュー者: Codex (2026-08-19)

## 結論

提示された2回の `feedian sync --source all` の結果は期待どおりである。既知itemだけのquick syncで、Hatenaの全件走査を打ち切り、本文取得を行わず、収集した250件をskipとして数えている。`stopped_early=raindrop,hatena` も両providerが早期打ち切り条件に達したことを正しく表す。

確定仕様の主要な契約も満たしている。Hatenaの打ち切り判定はクエリごとに独立し、page fetch workerはDBへ触れず、同一resourceを同じchunkへ入れずにmain threadで再判定する。browser結果の合成、RSS fallback、保存、auditもmain threadに残る。ingestはbackendごとの並列枠と開始間隔をschedulerで管理し、fallbackの再計画とDB書き込みをmain threadで行う。中断時の未開始Futureのcancel、開始済みFutureのdrain、残存runの次回回収も実装とテストで確認した。

ただし、通常経路では表面化しない境界条件の指摘が2件ある。Hatena quickの明示的な `--limit` が検索クエリ間の重複を新規itemとして二重計上し、指定件数より少ない新規itemで収集を終え得る問題が1件、ingestのfallbackが同じ候補を `processed` へ二重計上し、進捗と最終集計が候補数を超える問題が1件である。保存済みデータの破損はなく、重大度: 高の指摘はない。

### 再レビュー — Codex (2026-08-19)

指摘1・2の直接の不具合は修正された。`limit` はrun内で一意な新規source IDだけを数え、fallbackも最終reportでは候補1件として数える。追加された回帰テストを含む全448テストも成功した。

一方、指摘1の修正が、確定仕様の「ページのitemがすべてDB由来の `known` に含まれるときだけ既知ページとする」条件を変えている。別クエリで先に収集しただけの新規itemを既知扱いするため、quickが後続pageの新規itemを取り逃し得る。また、指摘2の修正後も、並列ingestのprogressが完了候補数ではなくsubmit済み候補数を表示する。指摘3・4として採否を求める。重大度: 高の指摘はない。

### 再々レビュー — Codex (2026-08-19)

指摘3・4は提案どおり修正され、指摘1・2も再発していない。HatenaはDB由来の `known` だけで既知pageを判定しながら、limitはrun内で一意な新規source IDだけを数える。ingestは候補を最終集計する `account` で `processed` を加算し、並列実行でも完了数を順に報告する。全449テスト、最小再現、Ruff、compileallはいずれも成功した。機能上の追加指摘はない。

ただし、レビュー対象 `0947088` に対する修正とレビュー文書が `6e6531f`、追加修正と文書更新が `4ba9626` の2commitへ分かれている。`4ba9626^` は `6e6531f` であり、文書の `対象` が修正完了commitの直接の親ではない。指摘5の履歴整理だけが残るため、ステータスを「対応中」とする。

コミット構成については、`a9caddd` が仕様書だけを含む単独の `docs:` commitであり、その後にHatena、設定、page fetch、ingest、文書規約の実装commitが続くことを `git log` と `git show --stat` で確認した。現行設計の要約更新を要する3つのコードcommitでは、該当する `DESIGN.md` の変更が同じcommitに入っている。

## 指摘

### 1. Hatena quickのlimitがクエリ間の重複を新規itemへ二重計上する — 重大度: 中

**根拠:** `feedian/hatena.py:32`、`:246-249`、`:253-265`、`:305-313`、`:336-340`、確定仕様`:88-90`

Hatena収集は `https` と `http` の2検索を行い、同じbookmarkが両方の結果へ現れることを前提に `items_by_id` で戻り値を重複排除する。一方、quickの `new_items` と `page_new` は、source IDが既存DB由来の `known_ids` に無ければ、同じrunですでに `items_by_id` へ入ったIDでも毎回加算する。次のpageへ進む前に共有の `new_items >= limit` を見るため、第1クエリで見つけた新規itemが第2クエリにも現れると、その重複だけで予算を使い切り、後続pageの別の新規itemへ到達しない。

最小再現では、第1クエリに新規A、第2クエリの1ページ目に新規Aの重複と既知99件、2ページ目に新規Bを置き、`limit=2` とした。実際の結果はリクエスト2回、新規item 1件、新規Bなしだった。確定仕様はquickのlimitを「実際に取り込む新規itemの件数」と定め、クエリ間の重複排除を維持するとしているため、この二重計上はその契約を満たさない。

**影響:** `--limit` を明示したquick syncが、まだ取得可能な新規bookmarkを指定件数未満で打ち切る。次回syncで回収できる可能性はあるが、そのrunの件数指定は守られず、検索結果の重なり方によって取得件数が変わる。

**提案:** DB由来の `known_ids` とは別に、このrunで初めて見つけた新規source IDの集合を持つ。`new_items` と `page_new` は `known_ids` にもrun内集合にも無いIDだけを加算する。2検索にまたがる重複、新規itemが次pageにあるケース、および `limit=2` で新規2件を返すことを回帰テストへ追加する。

### 2. ingestのfallbackが同じ候補をprocessedへ二重計上する — 重大度: 低

**根拠:** `feedian/ingest.py:223-232`、`:256-287`、`:291-315`、`:323-334`、`:364-366`、`feedian/cli.py:712-718`、`:739-743`、`tests/test_ingest.py:591-627`

`processed` はcached候補では候補ごとに1回加算されるが、新規requestではjobをexecutorへsubmitするたびに加算される。fallback対象の一次jobが失敗すると同じ候補のfallback jobをqueueへ戻すため、そのsubmitでもう1回加算される。最小再現では候補1件の一次backendがrate limitとなりfallbackが成功したとき、保存結果は `created=1` と正しい一方、reportは `processed=2`、progress callbackは `2/1` だった。

**影響:** fallbackを使った `feedian ingest` の進捗が総候補数を超え、最終行も実際の候補数より大きい `processed` を表示する。保存内容、課金集計、一次・fallbackそれぞれの `llm_run` auditは正しい。

**提案:** candidateの処理件数とbackend試行件数を分ける。`processed` はcandidateを最終的にaccountした時点、または最初のjobを作った時点で1回だけ加算し、fallback submitでは増やさない。既存fallbackテストへ `report.processed == 1` と、progressのprocessedがtotalを超えないことを追加する。backend試行数が必要なら別の明示的なカウンタで表す。

### 3. run内で先に見たitemだけのページを既知ページとして打ち切る — 重大度: 中

**根拠:** `feedian/hatena.py:249`、`:309-317`、`:331-334`、`tests/test_hatena.py:337-352`、確定仕様`:86`、`:96`

指摘1の修正は、`seen_new` にすでに入ったsource IDについて `page_new` も増やさない。したがって、第1クエリで見つけた新規Aが第2クエリのpageに再登場し、そのpageの残りがDB既知itemなら、`page_new == 0` となって既知ページの連続数へ加算される。既定の `stop_after_known_pages=1` では、その場で第2クエリを打ち切る。

確定仕様`:86` が打ち切り条件としたのは「そのページのitemがすべて `known` に含まれる」ことである。`known` はsync開始時にDBから渡される集合であり、第1クエリで同じrun中に見ただけの新規Aは含まれない。確定仕様`:96` が受け入れたリスクも、DB既知itemが再ブックマークで先頭へ移動する場合であって、クエリ間の重複をDB既知へ読み替える変更ではない。

最小再現では、第1クエリに新規A、第2クエリの1ページ目にAの重複とDB既知99件、2ページ目に新規Bを置き、limitなし・既定閾値1とした。現在の実装は2リクエストで停止し、新規Aだけを返して新規Bへ到達しなかった。`test_a_page_of_bookmarks_already_collected_this_run_counts_as_known` はこの仕様不一致を期待値として固定している。

**影響:** `--limit` の有無にかかわらず、2つの検索クエリが重なると、後続pageにある新規bookmarkをquick syncが取り逃し得る。`--full` で回収できるため保存済みデータは壊れないが、確定仕様が打ち切りを許可していない入力で新規itemを見落とす。

**提案:** `page_new` とlimit用の一意件数を分ける。source IDが `known_ids` に無ければ、run内で既出でもそのpageは未知itemを含むため `page_new` を増やす。そのうえで `seen_new` に無いIDだけを `new_items` へ加算する。既存の `test_a_page_of_bookmarks_already_collected_this_run_counts_as_known` は、3ページ目へ進み新規Bを返す期待値へ反転する。

### 4. 並列ingestのprogressが完了数ではなくsubmit数を表示する — 重大度: 低

**根拠:** `feedian/ingest.py:223-232`、`:234-254`、`:323-338`、`feedian/cli.py:712-724`、確定仕様`:197-201`

新規候補の `processed` はprimary jobをsubmitした時点で加算される一方、progress callbackはFutureの完了後に `account` から呼ばれる。worker枠まで複数jobを先行submitするため、最初の1件が完了した時点で `processed` はすでに全候補数になり得る。3候補・3 workerの最小再現では、progressは `(3/3, created=1)`、`(3/3, created=2)`、`(3/3, created=3)` の順になった。

確定仕様はmain threadで `finish_llm_run`、`put_source_note`、集計、進捗をこの順に行う。直列実装でもprogress時の `processed` は、そのcallbackまでに処理を終えた候補の通し番号だった。最終reportの `processed=3` とprogress barのadvance回数は正しいが、詳細ログは最初の完了から `3/3` と表示する。

**影響:** 並列ingestのverboseな進捗が実際より先行し、長いLLM処理でも最初の完了時点で全件処理済みのように見える。保存内容と最終集計は正しい。

**提案:** 新規候補の `processed` はsubmit時ではなく、最終結果を集計する `account` の冒頭で1回だけ加算する。fallbackへ移る一次失敗では `account` を呼ばないため、fallbackの二重計上も再発しない。3候補・3 workerでprogressが `1/3`、`2/3`、`3/3` となる回帰テストを追加する。

### 5. 修正とレビュー文書が2commitに分かれている — 重大度: 低

**根拠:** `AGENTS.md:183-190`、`git log` の `0947088 -> 6e6531f -> 4ba9626`、`git rev-parse 4ba9626^`

レビュー対象は `0947088` だが、指摘1・2の修正とレビュー文書が `6e6531f`、指摘3・4の修正と同文書の更新が `4ba9626` に分かれている。現在のHEADで `git rev-parse 4ba9626^` は `6e6531f` を返すため、レビュー文書の `対象` は修正完了commitの直接の親ではない。

**影響:** コードの動作は変わらないが、「対象」とその全修正を1対1で追跡するコードレビュー文書の履歴不変条件を満たさない。

**提案:** `6e6531f` と `4ba9626`、および本再々レビューの文書更新を、`0947088` の直後の1 commitへsquashする。完了後に `git rev-parse <fix commit>^` が `0947088` を返すことを確認し、ステータスを「完了」へ戻す。

## 採否

| # | 指摘 | 重大度 | 採否 | 対応 |
|---|---|---|---|---|
| 1 | Hatena quickのlimitにおけるrun内重複 | 中 | 採用 | `seen_new` を持ち、run内で既出の新規source IDを再計上しない |
| 2 | fallback時のprocessed二重計上 | 低 | 採用 | `processed` はfallbackでないjobの投入時のみ加算する |
| 3 | run内既出itemだけのページによる早期打ち切り | 中 | 採用 | `page_new` と limit 用の一意件数を分離。既知判定は `known_ids` のみで行う |
| 4 | 並列ingestのprogressがsubmit数を表示 | 低 | 採用 | `processed` は submit 時ではなく `account` の冒頭で加算する |
| 5 | レビュー対象と修正commitの関係 | 低 | 採用 | 2 commitを `0947088` 直後の1 commitへsquashした |

### PR #18 のレビュー（Codex GitHub app / CodeRabbit, 2026-08-19）

PRを開いた際の自動レビュー。4件を採用した。

| # | 指摘 | 出所 | 重大度 | 採否 | 対応 |
|---|---|---|---|---|---|
| 6 | `fail_interrupted_llm_runs()` がplanningにあり、dry-runがlock外で書き込む | Codex P2 / CodeRabbit Critical | 高 | 採用 | 実行経路（dry-run returnの後）へ移した |
| 7 | (B1) 本文のみpassが直列のまま | Codex P1 | 中 | 採用 | `fetch.workers` を渡し、取得だけをworkerへ出した |
| 8 | `assert pending.fetch is not None` は `python -O` で除去される | CodeRabbit | 低 | 採用 | 明示的な分岐と `ValueError` に置き換えた |
| 9 | 改訂記録の `(後)` が現在の本文と一致しない | CodeRabbit | 低 | 採用 | 太字・リンク・`（改訂1）` を含む現在の本文を引用し直した |

#### 指摘6について — 置換対象を取り違えた

**これは私の編集ミスである。** `fail_interrupted_llm_runs()` を `ingest_source_notes` の冒頭へ入れるつもりで、`backend_id = canonical_backend_id(provider or backend)` を目印に置換したが、この行は `plan_source_notes`（`feedian/ingest.py:100`）にも同じ形で存在し、先頭の一致である planning 側へ入っていた。

結果として、**`feedian ingest --dry-run` が `vault_write_lock` の外でDBを書き換える**状態になっていた（`feedian/cli.py:691-699` はdry-run経路でlockを取らずにplanningを呼ぶ）。他プロセスが実行中の `llm_run` を失敗へ書き換え得るため、データ完全性の問題である。しかも私が付けたコメント「The vault write lock is held by the caller」自体が、その位置では偽だった。

既存テストが通っていたのは、`ingest_source_notes` が内部で `plan_source_notes` を呼ぶため、回収の効果だけは観測できていたからである。**呼ばれる位置を検証していなかった。** dry-runとplanningが書き込まないことを固定する回帰テストを追加した。

#### 指摘7について

確定仕様は `fetch.workers` を「(A)ループと(B1)passの双方」と定めているが、実装は(A)にしか渡していなかった。本文未取得のresourceが多いVault（取得失敗や中断の後）では、(B1)がtimeoutの総和を直列で払い続ける。`_fetch_urls` を足し、(A)と同じく取得だけをworkerへ出してDB書き込みとbrowser描画をmain threadに残した。(B1)は `unfetched_resources` がresourceごとに1行を返すので、(A)が要する同一resourceの繰り延べは不要である。

### 指摘5について

指摘は規約どおりであり、採用した。`AGENTS.md` のコードレビュー運用は、修正を1 commitへsquashして `対象` がちょうど1 commit前に留まることを求めている。指摘1・2の修正（`6e6531f`）と指摘3・4の修正（`4ba9626`）を、レビュー文書とともに `0947088` の直後の1 commitへまとめ直した。squash前の2 commitはpushしていないため、失われる公開履歴は無い。

**「指摘1の修正が指摘3を作り込んだ」という経緯は、gitの commit 境界ではなく本文書が記録する。** commitを分けたまま残す選択もあり得たが、規約が求める「対象と全修正の1対1対応」の利点のほうが大きい。経緯の記録という点では、commit境界より本文書の記述のほうが詳しく、かつ同じcommitに含まれる。

### 指摘3について — 前回の修正が作り込んだ不具合である

**指摘は正しい。これは指摘1の修正で私が持ち込んだものである。**

`seen_new` を `page_new` にも効かせたため、「別クエリで先に見つけた新規item」を既知item扱いし、確定仕様`:86` が定めた打ち切り条件（そのページのitemがすべてDB由来の `known` に含まれる）を狭めていた。`known` はsync開始時にDBから渡される集合であって、同じrunで見ただけのitemを含まない。

**さらに悪いことに、その挙動を固定する回帰テストを自分で書いていた。** 前回の採否欄には「修正の副次的な効果であり、収集が短くなる方向にしか働かない」と書いたが、これは判断として誤っている。quickの契約は「取得量を減らす」ことではなく「新着を取りこぼさない」ことであり、収集が短くなる変更は無条件に安全ではない。**挙動の変化に気づいていながら、確定仕様の条文に当てずに正当化した。** 仕様レビューで得た「境界の定義を実装の都合に合わせて動かしてはならない」という教訓が、実装側で再発した形である。

修正は指摘の提案どおり2つのカウンタを分けた。`page_new` は `known_ids` に無ければ加算し、`new_items`（limit用）は `seen_new` にも無い場合だけ加算する。誤った期待値を固定していたテストは
`test_a_page_holding_a_new_bookmark_is_not_a_known_page` へ反転させた。

指摘1の修正自体は有効なままである。切り分け条件（閾値2）で3リクエスト・新規2件を返すことを再確認した。

### 指摘4について

指摘は正しい。`processed` をsubmit時に加算していたため、worker数まで先行投入する構造では最初の1件が完了した時点で全候補数に達していた。集計する `account` の冒頭へ移した。fallbackへ移る一次失敗は `account` を通らないため、指摘2の二重計上も再発しない。3候補・3 workerで `(1,3) (2,3) (3,3)` となることを回帰テストで固定した。

### 指摘1について — 再現シナリオの補足

指摘の診断は正しく、修正した。ただし**提示された再現は、limitの二重計上ではなく早期打ち切りの規則を示している。**

提示されたシナリオ（第2クエリ1ページ目が「新規Aの重複 + 既知99件」）では、修正後も結果が変わらない。そのページには新規itemが1件も無いため、`stop_after_known_pages=1` の規則がそこで収集を打ち切るからである。これは設計どおりの挙動であり、確定仕様が「1回のsync間隔に100件以上の再ブックマークが挟まると取りこぼし得る」として明示的に受け入れたトレードオフと同じ形をしている。

二重計上だけを切り分けるには、`stop_after_known_pages=2` として全既知1ページでの打ち切りを外す必要がある。その条件で実測した。

| | リクエスト数 | 返した新規item | 新規Bへ到達 |
|---|---|---|---|
| 修正前 | 2 | A のみ | 到達しない |
| 修正後 | 3 | A と B | 到達する |

修正前は第2クエリでAを再計上して `limit=2` を使い切り、2ページ目のBへ進まなかった。指摘が述べた不具合そのものである。

回帰テストは切り分けた条件で固定した（`test_a_bookmark_found_by_both_queries_spends_the_limit_once`）。あわせて、run内で既出のitemだけを含むページが「既知ページ」として打ち切りに数えられることも固定した（`test_a_page_of_bookmarks_already_collected_this_run_counts_as_known`）。これは修正の副次的な効果であり、収集が短くなる方向にしか働かない。

再レビューの指摘3は、この副次的な効果を採らない。収集が短くなるだけでなく、確定仕様が継続を要求するpageで新規itemを取り逃すためである。

## 検証

- [x] `./.venv/Scripts/python.exe -m pytest -q` → 445 passed、29 subtests passed。既存のDeprecationWarning 2件のみ。
- [x] `./.venv/Scripts/python.exe -m ruff check feedian tests` → All checks passed!
- [x] `./.venv/Scripts/python.exe -m compileall -q feedian tests` → 成功。
- [x] `git diff --check 6b4523f..HEAD` → 問題なし。
- [x] 提示された実動作2回で、既知250件が `processed=0`、`skipped=250`、`fetched=0` となり、RaindropとHatenaの両方が早期打ち切りされることを確認。
- [x] 指摘1を2検索・3pageのfake responseで再現。`limit=2` に対し2pageで停止し、返した新規itemは1件だった。
- [x] 指摘2を既存fallback fixtureと同じ構成で再現。候補1件、fallback成功1件に対し `processed=2`、progressは `2/1` だった。
- [x] `git show --stat a9caddd` → 仕様書1ファイルだけの `docs:` commit。
- [x] `git log --reverse a9caddd^..0947088` → 仕様単独commitの後に5つの実装commitが続く。

### 修正後の検証（Claude Code, 2026-08-19）

- [x] 指摘1・2をいずれも修正前のコードで再現した。指摘2は `processed=2` / progress `2/1`、指摘1は切り分け条件で2リクエスト・新規1件。
- [x] 修正後、指摘2は `processed=1` / progress `1/1`、指摘1は切り分け条件で3リクエスト・新規2件。
- [x] `./.venv/Scripts/python.exe -m pytest -q` → 448 passed、29 subtests passed。
- [x] 回帰テスト3件を追加（hatena 2件、ingest 1件）。

### 指摘3・4の修正後の検証（Claude Code, 2026-08-19）

- [x] 指摘3を再現条件（既定閾値1、limitなし）で確認。修正後は3リクエストで新規Bへ到達する。
- [x] 指摘1の切り分け条件（閾値2、limit=2）で3リクエスト・新規2件を維持していることを再確認。
- [x] 全既知ページによる打ち切りが従来どおり働くことを既存テストで確認（`test_quick_stops_each_query_after_a_page_of_known_items`）。
- [x] 指摘4を3候補・3 workerで確認。progressは `(1,3) (2,3) (3,3)`。
- [x] `./.venv/Scripts/python.exe -m pytest -q` → 449 passed、29 subtests passed。

### 再レビューの検証（Codex, 2026-08-19）

- [x] `./.venv/Scripts/python.exe -m pytest -q` → 448 passed、29 subtests passed。既存のDeprecationWarning 2件のみ。
- [x] `./.venv/Scripts/python.exe -m ruff check feedian tests` → All checks passed!
- [x] `./.venv/Scripts/python.exe -m compileall -q feedian tests` → 成功。
- [x] `git diff --check 0947088..HEAD` → 問題なし。
- [x] 指摘3を2検索・3pageのfake responseで再現。limitなし・閾値1で2page後に停止し、新規Bへ到達しなかった。
- [x] 指摘4を3候補・3 workerで再現。progressは `3/3` のまま `created=1`、`2`、`3` と推移した。
- [x] `git rev-parse 6e6531f^` → `0947088`。レビュー文書と指摘1・2の修正は対象commitの直後の1 commitにまとまっている。

### 再々レビューの検証（Codex, 2026-08-19）

- [x] `./.venv/Scripts/python.exe -m pytest -q` → 449 passed、29 subtests passed。既存のDeprecationWarning 2件のみ。
- [x] `./.venv/Scripts/python.exe -m ruff check feedian tests` → All checks passed!
- [x] `./.venv/Scripts/python.exe -m compileall -q feedian tests` → 成功。
- [x] 指摘3の再現条件で3page目の新規Bへ到達し、新規itemを2件返した。
- [x] 指摘4の再現条件でprogressが `1/3`、`2/3`、`3/3`、`created` も1、2、3と推移した。
- [x] `git diff --check 0947088..HEAD` → 問題なし。
- [x] 指摘5に対応してsquashした。squash後の `git rev-parse <fix commit>^` は `0947088` を返す。

## 規約化した項目

なし。指摘1〜4はいずれも今回追加された収集・scheduler実装に固有であり、一般規約へ昇格する同種指摘の2回目ではない。指摘5は既存のコードレビュー運用規約の適用であり、新規の規約化は不要である。
