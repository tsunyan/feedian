# 説明画像のOCRとSourceノートの一意なファイル名実装のコードレビュー

ステータス: 完了
対象: `454521e` feat: add image OCR and full Source IDs（このコミットの親）
仕様: [説明画像のOCRとSourceノートの一意なファイル名](../specs/20260825-image-ocr-source-filenames.ja.md)
レビュー者: Claude Code (2026-08-25)

## 結論

9件をすべて採用し、本コミットで修正した。本文とOCRの予算・blockを分離し、外部値でdelimiterを閉じられない形へescapeした。静的GIF、LLM解析timeout、DNS失敗分類、current revisionの読み出しも個別に修正した。

画像処理は、取得と解析を同じglobal worker枠で流すbounded pipelineへ変更した。取得済み一時fileと取得中URLの合計をworker数以内に保ち、backend枠に空きがある解析だけを投入する。これにより一時fileの大量滞留、解析開始の遅延、待機だけのworker、queue時間を含む`duration_ms`を同時に解消した。Sourceノート側の実装には変更を要する指摘が無かった。

## 指摘

### 1. OCRが本文へ連結され`max_article_chars`で切り捨てられる — 重大度: 高

**根拠:** `feedian/ingest.py:716-720`、`feedian/llm.py:571-609`（本コミットで未変更）、確定仕様「ingestへのOCR追加」

`_page`はOCR blockを`content_markdown`へ連結して`PageFetchResult.text`に入れる。`build_prompt`は`content = content[:max_article_chars]`で本文を切るため、OCRはこの同じ枠を奪い合う。確定仕様は本文の`max_article_chars`とOCRの`max_ocr_chars_per_resource`を別の予算として定めている。

**影響:** `openai-responses`（`max_article_chars=10_000`）で本文12,000文字・OCR 2,000文字のresourceでは、`content[:10000]`により`<feedian_image_ocr>`ブロックが丸ごと消える。それでも`IngestCandidate.prompt_version`は`ocr_text`が非空なので`source-note-v2`のままである。つまり**OCRが1文字も届かないまま既存のv1要約cacheだけを失い、全額を払って同じ内容の要約を作り直す。** 本文が上限直前（例: 9,990文字）の場合は開始tagの途中で切れ、閉じられていないblockがmodelへ渡る。`manus-api`（`max_article_chars=3_000`）ではOCRを持つほぼ全resourceがこの状態になる。

**提案:** OCRを`page.text`へ連結せず、`build_prompt`が本文とは別のuntrusted blockとして組み立てる。本文へ`max_article_chars`、OCRへ`max_ocr_chars_per_resource`をそれぞれ適用し、OCRの切り詰めは画像block単位で行う。

### 2. 静的GIFをアニメーションと誤判定して全て捨てる — 重大度: 高

**根拠:** `feedian/image_ocr.py:220`

`raster_dimensions`はGIFのアニメーション判定に`data.count(b"\x2c") > 1`を使う。`0x2C`はImage Separatorだが、同じbyte値はglobal color tableや画像データ中にも何の意味も無く現れる。256色paletteは768 byteあり、`0x2C`が複数回含まれる確率は非常に高い。

実際に確認した。ごく普通のgrayscale paletteを持つ静的な300×200のGIF89aヘッダに対し、`raster_dimensions('image/gif', data)`は`(300, 200, True)`を返す。header窓の`0x2C`出現数は5であった。

**影響:** `fetch_image`が`ImageFetchResult("ignored", ..., reason="animated")`を返し、GIFで保存された図表・表・手順図が完全取得もOCRもされずに捨てられる。`0x2C`は約256 byteに1回の割合で現れるので、数百byteを超えるGIFはほぼ全て該当する。確定仕様はheaderによるアニメーション判定を「重さの閾値と違って誤判定しない」ことを根拠に採用しており、その前提が成立していない。

**提案:** Image Separatorを数えるのではなく、GIFのblock構造を先頭から歩いて数える。あるいはNETSCAPE2.0 Application Extensionの有無で判定する。いずれも先頭数KBで足りる。

### 3. LLMの画像解析へ画像取得用の15秒timeoutを渡している — 重大度: 高

**根拠:** `feedian/image_ocr.py:423`、`feedian/ingest.py:563`、確定仕様の設定表

`_analysis_from_fetch`は`backend.analyze_image(..., timeout_seconds=settings.timeout_seconds, ...)`を呼ぶ。`image_ocr.timeout_seconds`の既定値は15で、確定仕様の設定表では「1回の画像HTTP取得timeout秒数」、効く段階は「取得・gate」と定義されている。LLM requestの制限時間ではない。

`ApiBackend.analyze_image`はこの値をbase64画像を含むResponses APIへの`urlopen` timeoutに使い、`CodexLocalBackend.analyze_image`はCLI起動を含むsubprocess全体の予算に使う。比較として、ingestははるかに小さいテキスト要約に`timeout_seconds=60`を渡している。

**影響:** 20秒かかる正常なvision requestが`BackendTimeoutError`となり、一過性失敗として記録され、次回1回だけ再試行され、それも失敗すれば`--force`まで抑止される。**応答が遅いだけの健全な画像が恒久的に処理されない。** local backendはCLI起動だけで数秒かかるため、より確実に発生する。

**提案:** LLM requestのtimeoutを取得のtimeoutから分離する。`image_ocr.analysis_timeout_seconds`のような別設定を設けるか、ingestと同じ60秒を使う。設定を追加する場合は、取得前targetの構成要素に含めるかどうかも決める。

### 4. OCR原文をescapeせずuntrusted blockへ連結している — 重大度: 高

**根拠:** `feedian/ingest.py:705-720`、`feedian/llm_backends.py:100-110`、確定仕様「ingestへのOCR追加」

`_candidate`は`f"[Image {index + 1} OCR; untrusted original-language text]\n{str(image['ocr_text'])}"`でOCR原文をそのまま埋め、`_page`が`<feedian_image_ocr>`で包み、`build_prompt`がさらに`<untrusted_page_text>`で包む。どの段階にもescapeが無い。

一方`image_ocr_prompt`はmodelへ「transcribe visible text exactly in reading order」と指示する。画像内に`</untrusted_page_text>`という文字列が描かれていれば、そのとおり忠実に転記されてDBへ保存される。

確定仕様は「URL、alt、OCRなどの外部値をXML風tagの属性へ直接連結しない。既存のuntrusted message構築処理を共通化し、delimiterを壊せない形へescapeする」と定めている。

**影響:** 攻撃者が用意した画像1枚で、次回以降そのresourceの要約requestにおいてuntrusted blockを閉じ、後続の文字列を信頼される位置へ置ける。OCRという工程の性質上、転記の忠実さがそのまま攻撃面になる。

**提案:** OCR原文とURL・altについて、blockのdelimiterに使う文字列を無害化してから埋める。無害化はOCR保存時ではなくprompt構築時に行い、DBには原文を残す。

### 5. DNS解決失敗を終端失敗として扱っている — 重大度: 中

**根拠:** `feedian/image_ocr.py:374`、`feedian/extract.py:114`、確定仕様の失敗分類

`validate_fetch_url`は名前解決に失敗すると`UnresolvableHostError`を送出し、これは`class UnresolvableHostError(ValueError)`である。`fetch_image`の`except (URLError, TimeoutError)`はこれを捕まえないため、後段の`except ValueError`へ落ちて`transient=False`を返す。

**影響:** `_attempt_values`が`last_failure_kind`に`transient:`接頭辞を付けず、`_due`が終端失敗として扱う。**一時的なDNS障害が起きた1回の実行で、影響を受けた画像が通常実行から恒久的に除外される。** 復帰手段は`--force`だけで、`--force`は選択resourceの全候補を巻き込む。確定仕様は「DNS・接続失敗」を一過性に分類しており、`fetch_page_text`は同じ条件を`failure_kind="dns"`として明示的に区別している。

**提案:** `except ValueError`より前に`UnresolvableHostError`を捕まえ、`transient=True`かつ`failure_kind="dns"`として返す。SSRF拒否（純粋な`ValueError`）は終端のまま残す。

### 6. 解析開始前に全画像を一時fileへ展開する — 重大度: 中

**根拠:** `feedian/image_ocr.py:612-620`、`feedian/image_ocr.py:700-703`、確定仕様の並列処理節

取得用の`ThreadPoolExecutor`ブロックが`as_completed`で全futureを消費して`fetched_by_url`を完成させてから、解析用のexecutorが始まる。一時fileの削除は2つ目のexecutorが終わった後にまとめて行われる。

**影響:** `enrich-images --all`は参照Vaultで取得前gate通過後に約19,000件が残る。中央値61.5 KB、p90 223 KBなので、`.feedian/tmp`配下に**同時に1〜4 GB程度の一時fileが滞留する。** また最後の1件の取得が終わるまで解析が1件も始まらない。process強制終了やdisk fullで全件が孤児として残る。`feedian/cli.py`のcleanupは正常にunwindする例外にしか効かない。確定仕様は「必要な一時fileもrequest終了後に削除する」と定めている。

**提案:** 取得と解析をpipelineにし、1件の取得が終わったら解析へ回して解析完了時にその一時fileを削除する。あるいは未解析の一時file数へ上限を設け、上限に達したら取得を止める。

### 7. `duration_ms`がqueue待ち時間を含む — 重大度: 中

**根拠:** `feedian/image_ocr.py:655`、`feedian/image_ocr.py:663`、`feedian/image_ocr.py:565-575`

`futures[future] = (key, group, run_id, time.monotonic())`はfutureを**submitした時点**の時刻を記録し、完了時に`round((time.monotonic() - started_at) * 1000)`を`duration_ms`として保存する。submitはループ内で全group分が一気に行われるため、この差分は実行時間ではなくqueue待ち時間を含む。

**影響:** `settings.workers=8`で5,000 groupを処理する場合、5,000件目の`started_at`は実行開始直後だが実際の処理は最後なので、保存される`duration_ms`は実行全体のwall clockに近づく。`enrich_images`はこの列の`AVG(duration_ms)`から`historical_seconds_per_image`と`expected_seconds`を計算するため、**次回実行のplanが表示する予想所要時間が桁違いに大きくなる。** 確定仕様は費用推定を作らない代わりに実測の表示へ依存しているので、この値が壊れると判断材料が無くなる。

**提案:** 計測をworker内で行い、backend呼び出しの直前と直後の差分を`duration_ms`とする。あわせて、全groupを一度にsubmitせず空き枠がある分だけ投入する形に改めれば、この問題と指摘8が同時に解消する。

### 8. backend並列gateをworker内で取得している — 重大度: 中

**根拠:** `feedian/image_ocr.py:418`、`feedian/image_ocr.py:642`、`feedian/ingest.py:458-481`

`backend_gate = threading.Semaphore(effective_parallelism)`を`_analysis_from_fetch`の内部で`with backend_gate:`として取得している。executorは`max_workers=settings.workers`（8）で作られる。

**影響:** `codex-local`と`claude-code-local`は`capabilities.max_parallelism`が1なので、**7本のworker threadが何も送信していない状態でsemaphoreをblockして待つ。** `ThreadPoolExecutor`はcallableを呼ぶ前にfutureをRUNNINGへ遷移させるため、これらは`Future.cancel()`で止められない。Ctrl-Cを押しても、blockした各workerが順にgateを取得して自分のCLI呼び出しを終えるまで実行が終わらない。`ingest.py`の`_BackendGate`はまさにこれを避けるためにsubmit前へ採否を移した実装で、そのdocstringが破られている不変条件を明記している。

**提案:** `_BackendGate`と同じく、main threadの投入条件としてbackend枠を判定する。空きが無いgroupはsubmitしない。

### 9. `completed_image_ocr`にcurrent revisionのfilterが無い — 重大度: 低

**根拠:** `feedian/store.py:1309-1330`、`feedian/store.py:1290-1300`、`feedian/store.py:1248-1262`

`resource_images_for_enrichment`は`ri.resource_revision_id = r.current_revision_id`で絞るが、`completed_image_ocr`は`resource_id`だけで絞る。`replace_resource_images`は抽出結果が0件のとき何も更新せずに戻るため、行が古い`resource_revision_id`を持ったまま残ることがある。

**影響:** 抽出器が一度失敗したresourceでは、その行が`enrich-images`の候補集合から外れて再評価されなくなる一方、`ingest`は同じ行のOCRを要約へ渡し続ける。確定仕様は読み出し側も「current revisionの`resource_image`から」と定めている。データを失わないための保全とは別に、2つのqueryの母集団が食い違っている。

**提案:** `completed_image_ocr`にも`resource`をjoinして`ri.resource_revision_id = r.current_revision_id`を課すか、両者の母集団を意図的に変えている旨を仕様側へ記録する。

## 採否

| 指摘 | 採否 | 対応 |
|---|---|---|
| 1. OCRが`max_article_chars`で切り捨てられる | 採用 | 本コミットで本文と画像別のfield・blockへ分離し、本文だけを`max_article_chars`で切るよう修正 |
| 2. 静的GIFをアニメーションと誤判定 | 採用 | 本コミットでGIF block構造を走査し、実在するImage Descriptorだけを数えるよう修正 |
| 3. LLMへ15秒timeoutを渡している | 採用 | 本コミットで取得15秒と分離し、LLM解析を60秒へ修正 |
| 4. OCR原文をescapeしていない | 採用 | 本コミットで各OCRを個別にHTML escapeし、URL・altもdelimiterを壊せないJSONへ変更。画像promptを`image-ocr-v2`へ更新 |
| 5. DNS失敗を終端扱い | 採用 | 本コミットで`UnresolvableHostError`を`dns`の一過性失敗へ分類 |
| 6. 全画像を先に一時fileへ展開 | 採用 | 本コミットで取得・解析をbounded pipeline化し、画像group完了時に一時fileを削除 |
| 7. `duration_ms`がqueue待ちを含む | 採用 | 本コミットでworker内の解析開始直前から完了直後だけを計測 |
| 8. backend gateをworker内で取得 | 採用 | 本コミットでmain threadがbackend空き枠を確認してから投入するschedulerへ変更 |
| 9. `completed_image_ocr`のfilter | 採用 | 本コミットで`resource`をjoinし、current revisionと一致する画像だけへ限定 |

## 検証

レビュー時点の確認と、修正後の検証を記録する。

- `python -m pytest -q` — 589 passed, 1 skipped, 48 subtests passed。
- 指摘2は`raster_dimensions('image/gif', <静的GIFのheader + grayscale palette + Image Descriptor 1個>)`を実行し、`(300, 200, True)`が返ることを確認した。
- 指摘1・3・5は`git diff main...HEAD`と該当箇所の読解による。`feedian/llm.py`は本コミットで変更されておらず、`build_prompt`の切り詰めが従来のまま残っていることを確認した。
- `python -m pytest -q` — 594 passed、1 skipped、48 subtests passed。
- `python -m ruff check feedian tests` — All checks passed。
- `git diff --check` — 問題なし。
- 回帰テストで、12,000文字の本文が10,000文字へ切られてもOCRが残り、OCRなしrequestは従来requestと一致することを確認した。
- 回帰テストで、palette内に複数の`0x2C`を持つ静的GIFと2 frame GIFを正しく区別した。
- 回帰テストで、画像HTTP取得は設定値を使う一方、LLM解析には60秒が渡ることを確認した。
- 回帰テストで、OCR・URL・altに閉じtagが含まれてもuntrusted delimiterを閉じられないことを確認した。
- 回帰テストで、DNS解決失敗が`transient:dns`へ至る一過性結果になることを確認した。
- 回帰テストで、取得と解析がpipeline動作し、一時file数がglobal worker数以内、backend同時解析数がcapability以内、終了時に一時fileが残らないことを確認した。
- 回帰テストで、current revisionと一致しない画像のOCRを`completed_image_ocr`が返さないことを確認した。

## 規約化した項目

現時点では無し。
