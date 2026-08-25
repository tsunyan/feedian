# 画像OCR修正の再レビューとメッセージ総量上限の未配線

ステータス: 完了
対象: `ffb2e77` fix: address image OCR review findings（このコミットの親）
仕様: [説明画像のOCRとSourceノートの一意なファイル名](../specs/20260825-image-ocr-source-filenames.ja.md)
前レビュー: [説明画像のOCRとSourceノートの一意なファイル名実装のコードレビュー](20260825-image-ocr-source-filenames-implementation.ja.md)
レビュー者: Claude Code (2026-08-25)

## 結論

前レビューの9件はすべて修正されており、退行は見つからなかった。重大度: 高の4件は実際に実行して確認した。

- 指摘1・4 — 本文12,000文字とOCR 1件のrequestで、本文だけが10,000文字へ切られOCR blockが残ること、OCR内の`</untrusted_page_text>`が`&lt;/untrusted_page_text&gt;`へescapeされること、`</untrusted_page_text>`の出現数が1のままであることを確認した。
- 指摘2 — `raster_dimensions('image/gif', ...)`で、palette由来の`0x2C`を5個持つ静的GIFが`False`、2 frameが`True`、Graphic Control Extensionを挟んだ両者も正しく判定されることを確認した。前レビューで`True`を返していた再現ケースが解消している。
- 指摘3・5 — `IMAGE_ANALYSIS_TIMEOUT_SECONDS = 60`による分離と、`except UnresolvableHostError`が`except ValueError`より前に置かれ`transient:dns`となることを確認した。
- 指摘6・7・8 — bounded pipelineへの書き換えで3件が同時に解消している。`deque.rotate`による中間取り出しの復元順序を検算し、`finally`が`ThreadPoolExecutor.__exit__`の後に走るため一時fileと開いた`llm_run`の後始末が確実であることも確認した。
- 指摘9 — `resource`のjoinによるcurrent revisionへの限定を確認した。

commit構成も規約どおりである。`git rev-parse ffb2e77^`は`454521e`で前レビューの`対象`と一致し、仕様は`f5dbee5`で単独commitされ、修正とレビュー文書は同一commitに入っている。`python -m pytest -q`は594 passed、1 skipped、48 subtests passedである。

一方、**前レビューが見落としていた未配線が1件ある。** `BackendCapabilities.message_size_limit_bytes`が宣言されているだけで、書き込む側も読む側も存在しない。`454521e`から続くもので`ffb2e77`の退行ではないが、確定仕様が明示的に求めた配線であるため記録する。

指摘1は修正して採用し、本コミットで対応した。既存field名の`bytes`は実際のManus上限と文字列builderの単位に合わないため、`max_message_chars`へ改名した。Manusだけが4,500文字を宣言し、他backendの`None`は上限なしを表す。OCR予算は本文長だけで概算せず、固定指示、metadata、本文、wrapper、escape後のOCR blockを含む完成messageの実文字数から求める。上限内へOCRが1文字も入らない場合はv2へ切り替えず、OCRなしのv1 requestをbyte単位で維持する。

## 指摘

### 1. `BackendCapabilities.message_size_limit_bytes`が誰からも読み書きされていない — 重大度: 低

**根拠:** `feedian/llm_backends.py:102`、`feedian/llm.py:19`、`feedian/llm.py:442`、確定仕様`:136`および`:257`

確定仕様は2箇所でこの配線を求めている。

> `BackendCapabilities`へ画像解析対応、メッセージ総量上限、既存の`max_parallelism`を持たせる。総量上限は「上限なし」を表現できるようにする。（`:136`）

> 合計10,000文字で打ち切る。backendにメッセージ総量上限がある場合は、その残り枠と10,000文字の小さい方をOCR予算にする。（`:257`）

実装は前半だけを満たしている。`message_size_limit_bytes: int | None = None`はdataclassに存在するが、`grep`しても`feedian/`配下に他の出現が無い。どのbackendも値を設定せず全て既定の`None`のままで、参照する処理も無い。

後半は実装されていない。`_candidate`は`completed_image_ocr(..., max_chars=image_settings.max_ocr_chars_per_resource)`を無条件に呼び、backendの残り枠を考慮しない。実際の上限である`MANUS_MAX_MESSAGE_CHARS = 4500`は`feedian/llm.py:19`のmodule定数のままで、`build_manus_message`（`feedian/llm.py:442`）からしか参照されない。確定仕様がこの定数をcapabilityへ接続するよう求めた状態は達成されていない。

**影響:** 破損は起きない。`ffb2e77`で`build_untrusted_message`の切り詰めが開いているtagを閉じるよう修正されたため、`manus-api`でmessage全体が上限を超えた場合もOCR blockは末尾から削られるだけで、delimiterは壊れない。したがって実害は「OCR予算をbackendごとに最適化できない」ことに留まる。

問題は将来にある。**宣言だけあって何も繋がっていないfieldは、繋がっているつもりで参照されるか、意味を失って腐るかのどちらかになる。** 既に`ffb2e77`のOCR block追加はこのfieldを使わずに実装されており、次に総量上限を扱う実装者は、値が常に`None`であることに気付かないまま条件分岐を書きうる。

**提案:** どちらかに寄せる。

- 配線する。`ApiBackend`のmanus構成へ`message_size_limit_bytes=MANUS_MAX_MESSAGE_CHARS`を渡し、`build_manus_message`と`_candidate`のOCR予算計算がcapabilityから読む。`None`は「上限なし」として扱い、その場合は`max_ocr_chars_per_resource`をそのまま使う。
- 落とす。fieldを削除し、確定仕様の`:136`と`:257`を`改訂`で訂正する。OCR予算をbackendごとに変えない判断を、理由とともに記録する。

配線を推す。`manus-api`は画像解析非対応なので、OCRを持つVaultで`llm.backend`をmanusへ切り替えた場合だけこの経路に入る。稀ではあるが、その稀な場合にOCRが黙って末尾から削られるより、予算として先に効かせるほうが結果が読める。

## 採否

| 指摘 | 採否 | 対応 |
|---|---|---|
| 1. `message_size_limit_bytes`が未配線 | 修正して採用 | 本コミットで`max_message_chars`へ改名し、Manusの4,500文字とrequest builder・実送信wrapperを接続 |

## 検証

再レビュー時点の確認と修正後の検証を記録する。

- `python -m pytest -q` — 594 passed, 1 skipped, 48 subtests passed。
- `grep -rn "message_size_limit_bytes" feedian/` — `feedian/llm_backends.py:102`の宣言1件のみ。
- `grep -rn "MANUS_MAX_MESSAGE_CHARS" feedian/` — `feedian/llm.py:19`の定義と`:442`の使用のみ。
- 前レビュー9件の修正確認は「結論」に記載したとおり、実行による確認と読解の併用による。
- `git rev-parse ffb2e77^` = `454521e`、`git show --stat f5dbee5` = 仕様ファイル1件のみ。
- `python -m pytest -q` — 修正後は596 passed、1 skipped、48 subtests passed。
- `python -m ruff check feedian tests` — All checks passed。
- `git diff --check` — 問題なし。
- 回帰テストで、Manusだけが`max_message_chars=4500`、他backendが`None`を宣言することを確認した。
- 回帰テストで、escape後のOCRを含む完成messageが4,500文字以内となり、後段の安全用切り詰めを発動しないことを確認した。
- 回帰テストで、OCRが1文字も入らない上限では`source-note-v1`を維持し、OCRなしrequestと一致することを確認した。

## 規約化した項目

現時点では無し。
