# restore trust anchorの実装のコードレビュー

ステータス: 完了
対象: `c5314b2` fix: close browser fallback network gaps (20260820-1)（このコミットの親）
仕様: [フェッチ・設定・復元の境界強化](../specs/20260820-fetch-config-integrity-hardening.ja.md)
レビュー者: Claude Code (2026-08-20)

## 結論

`/code-review`の8角度自動レビュー(finder 8並列 → 1票検証)を、`e6a3abd`が実装したrestore trust chain・CLI引数・設定bool厳密化・RSS ElementTree修正の部分(C・D・E節)に対して実行した。9件が検証を通過し、うち2件は重大度: 高/中の正確性バグとして自分で再現確認済みだった。空`--tag`がGit tag信頼アンカーを丸ごと迂回できる指摘と、不正な形のmanifest JSONで生の`AttributeError`が漏れる指摘の2件を採用し、本commitで修正した。README.mdの記述が古くなっている指摘も採用し、同じcommitで修正した。残り6件(altitude/reuse/efficiency/simplificationの設計上の指摘)は保留とした。

## 指摘

### 1. 空の`--tag`がGit tag信頼アンカーを迂回する — 重大度: 高

**根拠:** `feedian/restore.py:107`(修正前)

`_trusted_archive_sha256`は`tag`をそのまま`git show f"{tag}:.feedian/snapshot.json"`へ渡す。`tag`が空文字のとき`git show ":.feedian/snapshot.json"`となり、gitはこれを「タグ不在のエラー」ではなく「ローカルindexの当該pathを読む」と解釈する。実際に`git show ":docs/specs/....md"`を実行して確認し、exit 0でindexの内容が返ることを再現した(存在しないタグでは`fatal: invalid object name`でexit 128になることも確認済み)。

`argparse`の`required=True`(`feedian/cli.py`)は値が渡されたことだけを検査し、空文字であることまでは拒否しない。したがって`feedian restore --vault V --archive local.7z --tag ""`がCLIから直接到達可能で、改ざんしたarchiveと辻褄を合わせたmanifestという、section Cが閉じようとした攻撃そのものを素通りさせる。

**採否: 採用。** `_trusted_archive_sha256`の先頭で`tag`が非空文字であることを検証し、空または空白のみの場合は`git`へ渡す前に`ValueError`とした。`restore_database`と`download_and_restore`の両方がこの関数を経由するため、単一の関所で両CLI経路を塞げる。

### 2. 不正な形のmanifest JSONで`ValueError`ではなく生の`AttributeError`が漏れる — 重大度: 中

**根拠:** `feedian/restore.py:113-117`(修正前)

`json.JSONDecodeError`しか捕捉していないため、タグ付けされた`.feedian/snapshot.json`が構文的には正しいJSONでも期待した形(オブジェクト)でない場合(`null`・`[]`・`{"archive": "abc"}`等)、`manifest.get(...)`や`.get("sha256")`が`AttributeError: 'list' object has no attribute 'get'`を送出する。関数のdocstringは「thereis no fallback」で常に明確な例外を保証すると謳っており、この契約に反する。

**採否: 採用。** `manifest`が`dict`であることを`isinstance`で確認し、`archive`キーの値も`dict`であることを確認してから`.get("sha256")`する形に変更した。いずれかがdictでなければ、既存の「missing archive.sha256」という`ValueError`メッセージへ自然に合流する。

### 3. README.mdが`--archive`/`--tag`を排他フラグのまま記載している — 重大度: 低

**根拠:** `README.md:440`(修正前)、`feedian/cli.py`のargparse定義

`feedian restore --vault PATH (--archive FILE | --tag TAG)`という表記は、`--tag`が常に必須になった現在の挙動と一致しない。この表記のまま`--archive`だけを指定するとargparseの`SystemExit(2)`になる。

**採否: 採用。** `--tag`を常に必須、`--archive`を任意の補助引数として明記し、検証の仕組み(Git tagで固定された`.feedian/snapshot.json`との照合)を一文で説明する形に書き換えた。

## 保留とした指摘

以下6件は、正確性バグではなく設計・重複に関する指摘であり、今回のセキュリティ修正の緊急度に対して見送った。

| # | 指摘 | 重大度 | 理由 |
|---|---|---|---|
| 4 | `_trusted_archive_sha256`が`.feedian/snapshot.json`のpath/key形状を`snapshots.py`(書き込み側)と独立に知っている | 低〜中 | 書き込み側と読み込み側の契約が2ファイルに分散する設計上の懸念だが、現状は動作する。次に`snapshots.py`のmanifest形状を変える機会に合わせて共通化を検討する |
| 5 | `_trusted_archive_sha256`のmanifest sha256抽出が`restore_database`内の同種処理と重複している | 低 | 小さな重複であり、今回の修正範囲を広げてまで統合しない |
| 6 | `strict_bool_setting`が既存の`llm.fallback.enabled`のinline checkへ適用されていない | 低 | 既存のinline checkは既に`isinstance`で厳密であり、機能的なバグではない。将来`strict_bool_setting`を変更する際に見落とさないよう記録だけ残す |
| 7 | `_restore`のCLI層でrestore_database/download_and_restoreの分岐を持っている | 低〜中 | 現状唯一の呼び出し元でしか使われておらず、ドメイン層への統合は将来複数の呼び出し元ができた時点で検討する |
| 8 | archiveファイルを3回(hash・7z test・7z extract)逐次読む | 低 | 手動のdisaster recovery経路であり頻度が低いため、今回は見送る |
| 9 | `cli.py`のternary dispatchの条件が`args.tag`から`args.archive`へ変わったことにコメントがない | 軽微 | 可読性の指摘であり、機能に影響しない |

## 採否

| # | 指摘 | 重大度 | 採否 | 対応 |
|---|---|---|---|---|
| 1 | 空`--tag`がtrust anchorを迂回する | 高 | 採用 | 本commitで`_trusted_archive_sha256`冒頭に非空検証を追加した |
| 2 | 不正な形のmanifestで`AttributeError`が漏れる | 中 | 採用 | 本commitで`isinstance`チェックを追加した |
| 3 | README.mdの`--archive`/`--tag`表記が古い | 低 | 採用 | 本commitで書き換えた |
| 4〜9 | 設計・重複関連の指摘6件 | 低〜中 | 保留 | 上表の理由により今回は見送り |

## 検証

- [x] `.venv/Scripts/python.exe -m pytest -q` → 511 passed、43 subtests passed。
- [x] `.venv/Scripts/python.exe -m ruff check feedian tests` → All checks passed!
- [x] `.venv/Scripts/python.exe -m compileall -q feedian tests` → 成功。
- [x] `git diff --check` → 問題なし。
- [x] 新規回帰テスト`test_restore_rejects_an_empty_tag_instead_of_reading_the_local_index`が、空文字・空白のみの`--tag`をどちらも`ValueError`で拒否することを確認した。
- [x] 新規回帰テスト`test_restore_raises_valueerror_not_attributeerror_on_malformed_manifest`が、`null`・`[]`・`"oops"`・`{"archive": "abc"}`・`{"archive": []}`のいずれでも`ValueError`(`AttributeError`ではなく)を送出することを確認した。

## 規約化した項目

なし。今回の指摘はいずれも同種指摘の2回目ではない。
