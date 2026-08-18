# syncのquickモードPRレビューのコードレビュー

ステータス: 完了
対象: `15938db` fix: stop replaying validators for a resource with no body（このコミットの親）
仕様: [syncのquickモード](../specs/20260818-sync-quick-mode.ja.md)
レビュー者: Codex / CodeRabbit (2026-08-18)

## 結論

[PR #15](https://github.com/tsunyan/feedian/pull/15) に付いた自動レビュー2件を検討した。指摘は9件で、実バグが2件、性能・観測性の改善が2件、事実誤認が2件、判断の相違が1件、範囲外が2件である。

**実バグ2件はいずれも例外経路にあり、通常のテストでは踏まない。** 1件は`sync_vault`全体を巻き添えで落とす`UnboundLocalError`、もう1件は(B1)passの例外経路が失敗captureを残さないために、当該resourceが毎回予算を先取りして後続を飢えさせるものである。後者は確定仕様3-5がitem loopに対して明示していた要件を(B1)passへ適用し漏れていた。

Codexの重大度P1が2件付いているが、**いずれもコミット履歴に関する事実誤認である。** PRの集約差分（merge base `fcbb7de`）を見てコミット列を見ていないことが原因と見られる。

## 指摘

### 1. 対象コミットがレビュー修正の直前にない（Codex, P1） — 重大度: 高（主張）

**主張:** 「レビューは`b470042`を対象と称するが、このコミットの親は`fcbb7de`であり、`b470042`は履歴の祖先ではない」。

**検証結果: 事実誤認。**

```
$ git merge-base --is-ancestor b470042 15938db && echo ancestor
ancestor
$ git log --oneline --first-parent 15938db | head -4
15938db fix: stop replaying validators for a resource with no body
b470042 feat: sync only what is not yet stored by default
f456b78 docs: add the sync quick mode specification
fcbb7de Merge pull request #13 ...
```

`b470042`は`15938db`の**直接の親**である。`fcbb7de`はPRのmerge baseであってコミットの親ではない。前回レビューの指摘5に対して`d703bb9`と`b806c45`をsquashし、この不変条件は既に満たしてある。

### 2. 確定仕様を実装と同一コミットで導入している（Codex, P1） — 重大度: 高（主張）

**主張:** 「このコミットは確定仕様を実装・テスト・レビュー・`DESIGN.md`更新と一緒に導入している。仕様は実装前に`docs:`で単独コミットする必要がある」。

**検証結果: 事実誤認。**

```
$ git show --stat --format= f456b78
 docs/specs/20260818-sync-quick-mode.ja.md | 1149 +++++++++++++++++++++++++++++
 1 file changed, 1149 insertions(+)
```

仕様は`f456b78`で**単独・`docs:`型**でコミットされており、実装`b470042`はその後である。AGENTS.md:56-62のライフサイクルどおりに実施済みである。

### 3. (B1)passの例外経路が失敗captureを残さない（Codex, P2） — 重大度: 中

**根拠:** `feedian/sync.py`の`_run_quick_body_only_pass`の`except`節

**現象:** `fetch_page_text`が例外を送出した場合（リクエスト後のデコードや抽出で起きる。当該関数のcatch-allの外側）、この節は`failed`を増やして監査行を書くだけで`record_failed_fetch`を呼ばない。resourceはcaptureを持たないままとなる。

**影響:** `fetched_at`が進まないため、確定事項5-10の「最新`fetch_capture.fetched_at`の昇順（未試行を最先頭）」により**当該resourceが毎回最先頭に来る**。`--limit`付きの実行では予算を先取りし続け、後続のresourceが永久に処理されない。確定事項3-5はitem loopの例外経路に同じ要件を課しており、(B1)passへの適用が漏れていた。

### 4. `should_fetch_page`が未束縛になる経路がある（CodeRabbit, Major） — 重大度: 高

**根拠:** `feedian/sync.py`のitem loop、`try`節内の代入と`except`節の参照

**現象:** `should_fetch_page`は`try`節の中で代入される。`store.should_fetch_resource`がここで例外を送出すると変数は未束縛のまま`except`節へ入り、`_audited_source_items(..., fetched_body=should_fetch_page)`が`UnboundLocalError`を送出する。

**影響:** 二次例外が`except`節から伝播し、item単位の失敗記録を行わないまま`sync_vault`全体が`failed`で終了する。`should_fetch_resource`はSQL実行と`datetime.fromisoformat`を行うため、`fetch_capture.fetched_at`が不正な値のときに現実に起こり得る。

### 5. レビュー文書の本文とステータス行が矛盾する（CodeRabbit, Minor） — 重大度: 低

**主張:** 前回レビュー文書の再々レビュー節に「指摘5の履歴整理を終えるまでステータスは『対応中』とする」とあり、ステータス行の「完了」と矛盾する。

### 6. `reextract`の空抽出が`ReextractReport`に現れない（CodeRabbit, nitpick） — 重大度: 低

再抽出しても本文が得られなかった件数がどのカウンタにも現れない。`failed`は例外だけを数える。

### 7. `unfetched_resources`が`fetch_capture`全体にウィンドウ関数を適用する（CodeRabbit, nitpick） — 重大度: 低

**根拠:** `feedian/store.py`の`unfetched_resources`

`lfc`サブクエリが`fetch_capture`全体に`ROW_NUMBER()`を適用する。`sync_vault`はproviderごとに`unfetched_resources([provider])`を呼ぶため、同期1回で全表ウィンドウ計算が複数回走る。なお静的解析のSQLインジェクション指摘は誤検出であり、f-stringで埋めているのは`?`プレースホルダの個数だけである（レビュー側も誤検出と明記）。

### 8. `limit`未指定のquickに再試行件数の上限がない（CodeRabbit, nitpick） — 重大度: 低

`--limit`が`None`のとき(B1)passは`should_fetch_resource`を通過した候補すべてを1回の実行で取得する。CLIの既定がquickになったため、本文未取得resourceが多いVaultでは既定実行が長時間のHTTP取得列になる。

### 9. 取得失敗を`fetched`・`retried`と"completed"監査に含めている（CodeRabbit, nitpick） — 重大度: 低

`fetch_page_text`はHTTPエラーやブロックされたURLを例外にせず`PageFetchResult.error`で返す。この場合`_store_page`は`record_failed_fetch`を呼びrevisionを作らないが、(B1)pass側は`retried`と`fetched`を加算し`sync_run_item`へ`"completed"`を記録する。

## 採否

| # | 指摘 | 重大度 | 採否 | 状態 |
|---|---|---|---|---|
| 1 | 対象コミットが直前にない | 高（主張） | 不採用 | 事実誤認。`git log`で反証 |
| 2 | 仕様が実装と同一コミット | 高（主張） | 不採用 | 事実誤認。`git show f456b78`で反証 |
| 3 | (B1)pass例外経路の失敗capture | 中 | 採用 | 本コミットで修正 |
| 4 | `should_fetch_page`の未束縛 | 高 | 採用 | 本コミットで修正 |
| 5 | レビュー文書のステータス矛盾 | 低 | 不採用 | 下記 |
| 6 | `reextract`の空抽出カウンタ | 低 | 保留 | 下記 |
| 7 | ウィンドウ関数の走査範囲 | 低 | 採用 | 本コミットで修正 |
| 8 | (B1)再試行件数の上限 | 低 | 保留 | 下記 |
| 9 | 取得失敗の計数と監査 | 低 | 修正して採用 | 下記 |

### 指摘1・2を不採用とした理由

どちらも`git`の履歴で直接反証できる。両方が同じ誤り方をしていることから、**PRの集約差分（merge base `fcbb7de`からの1つの差分）を見てコミット列を見ていない**ものと考えられる。コミットの粒度や順序に関する指摘は、レビュー側がPR差分しか見ていない可能性があるため、`git log`で確認してから対応すること。

### 指摘5を不採用とした理由

AGENTS.mdは「**文書の状態はステータス行から判断する。節の有無から判断しない**」と定めており、ステータス行が正である。当該文はCodexの再々レビュー節にある「指摘5を終えるまで対応中とする」という条件文であり、条件が満たされたことは同文書の`採否`と`検証`に記録済みである。

加えて、**レビュー者が書いた節の文言を後から書き換えると、その時点で何が指摘されていたかという記録が失われる。** レビュー文書の価値は経緯が追えることにあるため、レビュー者の記述は保存し、決着は`採否`に書く。

### 指摘6・8を保留とした理由

いずれも妥当な改善だが、本PRの範囲外である。

- **指摘6**は`ReextractReport`とCLI出力の形を変える。`reextract`の観測性の改善であり、quickモードとは独立に議論すべきである。
- **指摘8**は`config.fetch`へ新しい上限を追加する提案であり、確定仕様が定めた(B1)passの契約（`--limit`のみを予算とする）の変更にあたる。仕様を編集せずに既定の上限を足すことはできない。

ただし指摘8の問題意識は正しい。既定がquickになった以上、`--skip-page-fetch`での一括取り込み直後などに(B1)候補が大量に残る状況は起こり得る。**別仕様として起票する。**

### 指摘9を修正して採用した理由

提案は「本文を取得できなかった再試行を`failed`として集計し、`sync_run_item`へ`"failed"`を記録する」だった。**この形では採らない。**

`_sync`は`return 1 if report.failed else 0`であり、提案どおりにすると**恒久的に到達不能なURLが1件でもあるVaultでは`feedian sync`が毎回exit code 1を返す**。cronやschedulerから見ると常に失敗し続けることになる。item loopは同じ状況を`failed`に数えておらず、そちらとの整合も崩れる。

一方、指摘の観察自体は正しい。`retried`は当方が本仕様で新設したカウンタであり、確定仕様は「(B1)passで取得したresource数」と定めている。本文を得られなかった再試行を含めると、利用者は未取得の滞留が解消しているのか判別できない。

**`retried`だけを「本文を得られたresource数」に改めた。** `fetched`は試行数のまま（item loopと同じ意味）、`sync_run_item`の状態と`failed`の意味は変更しない。これにより`retried`は「滞留が解消しているか」への答えになり、exit codeの回帰も起きない。

## 検証

- [x] `./.venv/Scripts/python.exe -m pytest -q` → 308 passed（修正前306件＋回帰テスト2件）。
- [x] `./.venv/Scripts/python.exe -m ruff check feedian tests` → All checks passed!
- [x] 指摘1: `git merge-base --is-ancestor b470042 15938db`が真であることを確認した。
- [x] 指摘2: `git show --stat --format= f456b78`が仕様書1ファイルのみであることを確認した。
- [x] 指摘3: (B1)passで`fetch_page_text`が例外を送出したとき、`fetch_capture`にwarningと`fetched_at`が記録され、`current_revision_id`がNULLのままであることを`tests/test_sync.py`で固定した。
- [x] 指摘4: `store.should_fetch_resource`が例外を送出しても、run全体ではなくitem単位の失敗として記録されることを`tests/test_sync.py`で固定した。
- [x] 回帰テストの有効性: 2件の修正を差し戻して該当テストが失敗することを確認した（`git stash push -- feedian/sync.py`）。
- [x] 指摘7: 相関サブクエリへの置換後も既存の`unfetched_resources`テスト（候補集合・互換条件・URL無し除外・順序・provider絞り込み・重複排除）が通ることを確認した。索引`fetch_capture_resource_idx (resource_id, fetched_at)`が効き、`feedian/reextract.py`が既に同じ形を使っている。
- [x] 指摘9: `retried`の意味変更後も既存テスト（`retried == 1` / `retried == 0`）が通ることを確認した。

## 規約化した項目

なし。ただし次の観察を記録する。**自動レビューはPRの集約差分を見ており、コミット列を見ていないことがある。** 本レビューでは重大度P1の2件がこれに起因する事実誤認だった。コミットの粒度・順序・親子関係に関する指摘は`git log`で確認してから対応すること。同種の誤りが次のレビューでも現れた場合は、AGENTS.mdへ規約として昇格する。
