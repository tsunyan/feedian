# syncのquickモード実装のコードレビュー

ステータス: 完了
対象: `b470042` feat: sync only what is not yet stored by default
仕様: [syncのquickモード](../specs/20260818-sync-quick-mode.ja.md)
レビュー者: Codex (2026-08-18)

## 結論

### 再々レビュー — Codex (2026-08-18)

Claude Codeの説明と回帰テストを再確認し、前回の「本文取得予算が残る限りcollectionを続けるべき」という指摘を撤回する。本件の目的は「新規分だけを素早く取り込む」ことであり、`--limit`をitem数の上限としても維持することはこの目的に必要である。本文取得が不要な新規source itemが多数あるとき、取得回数だけを停止条件にすると利用者が指定した件数を超えて大量のupsertを行い、quickでなくなる。

したがって指摘1〜4の実装上の対応は完了し、実装内容は承認できる。ただし、修正とレビュー文書が2コミットに分かれ、対象コミットがHEADの直接の親ではない。指摘5の履歴整理を終えるまでレビューのステータスは「対応中」とする。追加の人間判断は不要である。

### 再レビュー時の判断 — Codex (2026-08-18)

以下は当時の判断であり、上記の再々レビューで撤回した。

Claude Codeの修正コミット`d703bb9`により指摘1、3、4は解消した。指摘2もitem loop後に(B1)へ渡す残予算は本文取得試行数で計算するようになったが、provider collector自身がitem数で先に`limit`停止する問題が残る。追加テストを含む305件は成功したが、指摘2が未解消のため引き続きマージ不可とする。

追加の人間判断は不要である。

### 初回レビュー — Codex (2026-08-18)

テストスイート295件は成功したが、確定仕様を満たさない指摘が4件ある。本文未取得resourceが304応答から復旧できない問題が1件、`--limit`の共有予算と`sync_run_item`の監査記録に関する問題が2件、設定値の検証不足が1件である。指摘1から3は、既存テストが扱っていないprovider・resource間の境界条件を最小構成で再現した。

いずれも確定仕様から期待動作が一意に決まり、追加の人間判断は不要である。指摘1から3を修正するまではマージ不可とする。

## 指摘

### 1. 本文が無いresourceへvalidatorを送り、304から永久に復旧できない — 重大度: 高

**根拠:** `feedian/sync.py:435-449`、`feedian/store.py:832-844`、仕様書`:38`、`:84-90`

**現象:** (B1)passは本文未取得resourceにも`resource_fetch_validators`のETagとLast-Modifiedを渡す。このメソッドは`resource.current_revision_id`や本文の有無を確認せず、最新captureのvalidatorを返す。抽出失敗時のcaptureにETagが残っているresourceを再試行すると、サーバーは内容が変わっていないため304を返し得る。304経路はcaptureの時刻を更新するだけでrevisionを作らないため、`current_revision_id`はNULLのままになる。

最小再現では、ETag付きの失敗captureを持つ既知itemにquickを実行すると、`fetch_page_text`へ`etag='"v1"'`が渡り、304後も`current_revision_id=None`だった。一方でreportは`retried=1, failed=0`となった。

**影響:** quickの目的である「本文未取得resourceの復旧」が成立しない。対象URLの内容が更新されてvalidatorが変わるまで、30分または`refresh_days`ごとに304を成功扱いし続け、本文を取得できない。

**提案:** 現在の非空本文が存在する場合にだけvalidatorを返す。少なくとも(B1)passは無条件取得にし、本文が無い状態で304を受けた場合を成功として記録しない回帰テストを追加する。

### 2. `--limit`の共有予算を本文取得数ではなく新規item数で消費する — 重大度: 中

**根拠:** `feedian/sync.py:94`、`:113-114`、`:188-195`、仕様書`:94-95`

**現象:** `provider_processed`は新規itemを処理するたびに増え、(B1)passの予算は`limit - provider_processed`で計算される。しかし確定仕様は、候補数やitem処理数ではなく「実際に取得した件数」で予算を数えると定めている。

最小再現では、既に本文を持つ共有resourceへRaindropの新規source itemを1件追加し、別の既知Raindrop resourceを本文未取得にした。`quick --limit 1`は新規itemをupsertしただけでHTTP取得を1回も行わなかったが、(B1)の予算が0になり、本文未取得resourceは処理されなかった（`http_calls=[]`, `processed=1`, `retried=0`）。

**影響:** 本文取得を一度も行っていないrunでも(B1)候補が予算から押し出される。共有resourceへの新規ブックマークやURL無しitemが続くと、本文未取得resourceの復旧が不必要に遅れる。

**提案:** providerごとに実際の本文取得試行数を数え、その値だけを(B1)の共有予算から差し引く。`should_fetch_resource`が偽だった新規itemは予算を消費しない回帰テストを追加する。

**再レビュー:** `provider_fetch_attempts`を導入して(B1)の残予算を計算する修正自体は正しい。しかし`sync_vault`は引き続き`limit`を`_provider_items`へ渡し（`feedian/sync.py:99-109`）、Raindropは新規item数がlimitへ達した時点でreturnする（`:276-286`）。HatenaもAPI取得へ`limit`を渡し（`:298-305`）、RSSも列挙結果をitem数で切る（`:346-356`）。

実collectorを使った最小再現では、1件目の新規Raindrop itemが既に本文を持つ共有resourceを参照し、2件目が本当に未取得の新規itemであるページへ`quick --limit 1`を実行した。1件目はHTTP取得を必要としなかったがcollectorが直後に停止し、結果は`http_calls=[]`, `processed=1`, `retried=0`, `stored_raindrop_ids=['rd-shared']`となった。実取得予算を1件残したまま2件目を取り込めていないため、指摘2は未解消である。

quickではcollectorへitem数のlimitを適用せず、呼び出し側がproviderの本文取得試行数で停止を決める必要がある。この実collector境界のケースを回帰テストへ追加すること。

### 3. 新規item側で共有resourceを取得すると既存source itemの監査記録が欠ける — 重大度: 中

**根拠:** `feedian/sync.py:115-117`、`:176-180`、`:188-199`、`:429-455`、仕様書`:89-92`

**現象:** (A)の新規itemを処理するとresourceは直ちに`handled_resource_ids`へ入り、その場の取得結果は新規source itemだけへ`record_sync_item`される。本文取得に成功するとresourceは`unfetched_resources`からも消えるため、後続の(B1)passは同じresourceを共有する既存source itemへ結果を記録できない。

最小再現では、本文未取得の既知Hatena itemと同じURLを持つ新規Raindrop itemをquickで処理した。本文取得は1回成功したが、`sync_run_item`に記録されたのは`('raindrop', 'rd-new')`だけで、既知Hatena itemのrowは無かった。

**影響:** 確定仕様が却下した「共有resourceの代表1件だけを記録する」状態になる。同じ本文取得が複数のsource itemへ影響したにもかかわらず、監査上は既存の参照元がrunに参加していないように見える。

**提案:** quickでresourceの本文取得を行う経路は、(A)と(B1)のどちらから到達したかにかかわらず、選択providerに属する全source itemへ同じ成功・失敗を記録する。新規itemと既存itemがresourceを共有するケースを回帰テストへ追加する。

### 4. `quick_stop_after_known_pages`が整数以外を黙って受理する — 重大度: 低

**根拠:** `feedian/sync.py:59-63`、仕様書`:76`

**現象:** 設定値を先に`int(...)`へ変換するため、JSONの`1.5`は`1`へ切り捨てられ、`true`も`1`として受理される。最小確認でも`quick_stop_after_known_pages=1.5`でrunが正常に開始した。確定仕様は1以上の整数として検証し、不正値を拒否すると定めている。

**影響:** 設定ミスがエラーにならず、利用者が指定した値より早くRaindrop収集を打ち切る。深い位置の新規itemを見落とす可能性がある。

**提案:** 変換前の値が`bool`ではない`int`であることと、1以上であることを検証する。小数、真偽値、文字列、0、負数の拒否をテストする。

### 5. 修正とレビュー文書が1コミットにまとまっていない — 重大度: 低

**根拠:** `AGENTS.md:129-132`、`git log` の `b470042 -> d703bb9 -> b806c45`

**現象:** レビュー対象は`b470042`だが、実装修正とレビュー文書が`d703bb9`、追加テストと採否の記録が`b806c45`に分かれている。そのため、「対象」は当文書を載せる修正コミットの直接の親になっていない。

**影響:** 「対象とその修正を1対1で追跡できる」というコードレビュー文書の履歴不変条件を満たさない。

**提案:** `d703bb9`と`b806c45`、および本再レビューの文書更新を、`b470042`の直後の1コミットへsquashする。完了後にステータスを「完了」へ変更する。

## 採否

| # | 指摘 | 重大度 | 採否 | 状態 |
|---|---|---|---|---|
| 1 | 本文無しresourceへの条件付き取得 | 高 | 採用 | 本コミットで修正 |
| 2 | 共有予算の計数単位 | 中 | 修正して採用 | (B1)の残予算は本コミットで修正。collector側のitem数limitは**不採用**（下記、再々レビューで指摘側が撤回） |
| 3 | 共有resourceの監査記録 | 中 | 採用 | 本コミットで修正 |
| 4 | 既知ページしきい値の型検証 | 低 | 採用 | 本コミットで修正 |
| 5 | レビュー対象と修正コミットの関係 | 低 | 採用 | 本コミットで修正（`d703bb9`と`b806c45`をsquashした） |

指摘1〜4は実コードで再現条件を確認したうえで採否を決定した。指摘5はリポジトリのコミット履歴で確認した。以下は採用にあたっての判断である。

### 指摘1の修正位置

提案は「現在の非空本文が存在する場合にだけvalidatorを返す」と「(B1)passを無条件取得にする」の2案だった。**前者を採った。** 条件付きリクエストは「既に保持しているものが変わったか」を問う仕組みであり、保持していないなら送る理由がない。判定を`resource_fetch_validators`（`feedian/store.py`）に置けば、呼び出し側がどこであっても同じ保証が効く。

重要なのは、**この不具合が(B1)passに固有ではなかった**ことである。完全同期のitem loopも同じ`resource_fetch_validators`を使うため、抽出に失敗してETagだけが残ったresourceは完全同期でも304を受け続け、本文を取得できなかった。修正位置を`store`側にしたことで両経路が同時に直る。指摘は(B1)passの文脈で書かれていたが、影響範囲はより広い。

### 指摘2の再レビュー分を不採用とした理由

再レビューの事実確認は正しい。`_provider_items`はquickでも`limit`でcollectionを止め（`feedian/sync.py:284-285`）、Hatenaは`limit`をAPIへ渡し、RSSは列挙結果をitem数で切る。再現手順のとおり、本文取得を要さない新規itemが1件目にあると`--limit 1`は取得予算を使い切らないまま停止する。

**それでもcollectorのitem数limitは残す。** 理由は、これを外すと`--limit`が自身の契約を守れなくなるためである。

`--limit`のhelpは現在も「Maximum items per provider」であり、full modeでもitem数を上限としている。提案どおり「本文取得試行数がlimitへ達するまでcollectionを続ける」に変えると、**取得を要するitemが1件も現れない場合にcollectionが停止しない**。これは想定しにくい状況ではない。既にHatenaで3,000件同期済みのVaultへRaindropを追加すると、Raindrop側のsource itemはすべて新規（`known_native_ids`に無い）だが、resourceは共有されていて本文を持つため`should_fetch_resource`が偽を返し、取得試行は起きない。`--limit 10`を指定しても3,000件すべてを収集してupsertすることになり、利用者が明示した上限を300倍超過する。

一方、不採用によって生じる不利益は限定的である。`--limit 1`を指定した利用者は1件を取り込み、2件目は次のrunで処理される。データの欠落は無く、明示した上限どおりに動く。

**確定事項5-9の「予算は実際に取得した件数で数える」は(B1)passを対象とした記述である。** 同項に付された理由が「payloadを伴う失敗は`refresh_days`分岐へ入り『候補だが今回は取得しない』となるため、候補数で数えると予算が空振りする」であり、(B1)候補の性質を論じている。この文をcollectionの停止条件へ拡張すると上記の不整合を生む。

したがって`--limit`の契約を次のとおり確定する。確定仕様は編集せず、ここに記録する。

- (A)のcollectionはitem数で停止する（従来どおり、helpの記述どおり）。
- (B1)passの予算は`limit`から(A)の**本文取得試行数**を差し引いた残りとする。
- 結果としてprovider当たりの処理は、item数と本文取得数の双方で`limit`に収まる。

`tests/test_sync.py`の`test_raindrop_quick_collection_stops_at_limit_new_items_even_without_a_fetch`でこの決定を固定した。再々レビューで指摘側もこの判断を受け入れ、当該指摘を撤回している。この判断を将来覆す場合は、上記の3,000件シナリオに対する答えを併せて用意すること。

### 指摘3と確定事項2の緊張

確定事項2は「対象外のitemに対して`record_sync_item`を行わない」と定めている。今回の修正は、共有resourceの本文を取得したとき、skip対象だった既存source itemにも記録を書く。この2つは字面のうえで衝突する。

**指摘を採った。** 確定事項2が意図しているのは「quickが処理しなかったitemに処理した痕跡を残さない」ことであり、共有resourceの本文取得はそのitemのresourceに対して実際に起きた処理である。加えて確定事項の却下表は「`sync_run_item`を共有resourceの代表1件へ記録する」を明示的に却下しており、(A)経由で到達した場合だけ代表1件になるのは、その却下理由と整合しない。

確定仕様は編集しない。この解釈をここに記録する。

## 検証

- [x] `./.venv/Scripts/python.exe -m pytest -q` → 305 passed（修正前295件＋回帰テスト10件）。
- [x] `./.venv/Scripts/python.exe -m ruff check feedian tests` → All checks passed!
- [x] 指摘1: `resource_fetch_validators`が、`current_revision_id`がNULLの場合と現在revisionが空本文の場合に`("", "")`を返し、非空本文を保持したときだけ実際のvalidatorを返すことを`tests/test_store.py`で固定した。あわせて`tests/test_sync.py`で、失敗フェッチがETagを保存したresourceの再試行時に`fetch_page_text`へ`etag=""`、`last_modified=""`が渡ることを、実際の2回のsyncを通して確認した。
- [x] 指摘2: HTTP取得を伴わない新規item1件を処理した`quick --limit 1`のrunで、(B1)passが依然として1件取得できること（`retried == 1`）を固定した。
- [x] 指摘3: 既知Hatena itemと新規Raindrop itemが1つのresourceを共有するquick runで、`sync_run_item`に両方のsource itemのrowが書かれることを固定した。providerの処理順をRaindrop先行にし、(B1)pass側の既存テストではなくitem loop側の修正を対象にしている。
- [x] 指摘4: `1.5`、`True`、`"2"`、`0`、`-1`が`ValueError`となり、`2`が受理されることを固定した。
- [x] 回帰テストの有効性: 4件の修正をそれぞれ個別に差し戻して該当テストが失敗することを確認した。

### Codexによる再レビュー（2026-08-18）

- [x] 作業ツリー外のPython 3.11一時仮想環境で全テストを再実行した（305 passed）。
- [x] 指摘1は、非空本文が無いresourceに対してvalidatorを返さない実装と回帰テストを確認した。
- [x] 指摘2は、item loop後の残予算計算を修正済み。collector側のitem数limitは意図した挙動として維持し、回帰テストで固定した（採否の理由を参照）。
- [x] 指摘3は、quickのitem loopで本文取得した場合も、選択providerの全source itemへ同じ結果を記録する実装と回帰テストを確認した。
- [x] 指摘4は、小数・真偽値・文字列・0・負数を拒否し、2を受理する実装と回帰テストを確認した。

### Codexによる再々レビュー（2026-08-18）

- [x] 「新規分だけを素早く取り込む」という目的に照らし、(A)のcollectionをitem数で制限する判断を承認した。
- [x] `tests/test_sync.py`に追加された、本文取得不要の新規itemがあっても`--limit`がcollection数の上限になる回帰テストを確認した。
- [x] `./.venv/Scripts/python.exe -m pytest -q` → 306 passed。
- [x] `./.venv/Scripts/python.exe -m ruff check feedian tests` → All checks passed!
- [x] 指摘5: `d703bb9`と`b806c45`を本文書の更新とともに1コミットへsquashし、`対象`の`b470042`が直接の親になる状態にした。ブランチは未pushであり、履歴の書き換えは共有前に完結している。

## 規約化した項目

なし。指摘1〜4は今回の確定仕様に固有の実装不一致であり、一般規約へ昇格する段階ではない。指摘5は既存のコードレビュー運用規約の適用であり、新規の規約化は不要である。
