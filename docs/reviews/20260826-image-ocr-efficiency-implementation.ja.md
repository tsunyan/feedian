# 画像OCRの効率化と調整可能なgateのコードレビュー

ステータス: 完了
対象: 本文書と同じcommitに含まれる実装変更（親は`4c63adb docs: finalize image OCR efficiency and adjustable gate spec`）。実装は一度もcommitされないまま本レビューを受けたため、規約が想定する「実装commitの次に修正とレビュー文書」という形にならない。分割し直すcostのほうが高いので、実装・指摘1〜7の修正・本文書を1 commitにまとめた。`git rev-parse <このcommit>^`は仕様commitを指す。
仕様: [画像OCRの効率化と調整可能なgate](../specs/20260826-image-ocr-efficiency.ja.md)
レビュー者: Claude Code (2026-08-26)

## 結論

確定仕様の主要な決定はいずれも実装されている。`k = min(1, max(C / 長辺, (C / 2) / 短辺))`の倍率式、gate一致と終端事象を分けた状態遷移、`last_attempt_status='ignored'`によるdue抑止、legacy targetからの外部requestなし移行、Pillowのdecompression bomb対策は、実際に動かして期待どおりに振る舞うことを確認した。`python -m pytest -q`は631 passed / 1 skipped / 48 subtests passedで通る。

指摘は5件で、うち1件が重大度「高」である。**指摘1は、local diskのI/O失敗を「decoderが読めない画像」として恒久的な終端無視に落とすもので、本仕様が終了コード0と自動再試行の抑止をセットにしたことで沈黙する。** 指摘2は、gate一致がLLM分類済み行のpayloadを捨てるため、filterを試して戻すと再解析costを払い直す。どちらも仕様が達成しようとした性質（保存済み結果を壊さない、filter調整で再解析しない）の裏をかいている。

初回の5件と再レビューの指摘6をすべて採用し、作業ツリーで修正した。指摘2と、その修正範囲を限定する指摘6は実装だけでなく確定仕様の状態遷移にも関わるため、仕様の`改訂2`・`改訂3`へ変更前後・理由・証拠を記録した。その後の実Vault実行で指摘7・8が出た。指摘7は修正し、指摘8は確定仕様の改訂を要するため保留とした。全体回帰は637 passed / 1 skipped、Ruffは成功している。その後PR #23のCodexレビューで指摘9が出たので修正した。指摘1〜7と9の対応が終わり、保留の指摘8も理由を記録したので、本文書のステータスは`完了`である。

## 検証した挙動

実際に`_prepare_raster`を呼んで確認した結果を残す。

| 入力 | 送信寸法 | `resized` | `short_edge_floor_applied` |
|---|---|---|---|
| 4000×3000 | 1024×768 | True | False |
| 800×6000 | 512×3840 | True | True |
| 800×900 | 800×900 | False | False |
| 250×3000 | 250×3000 | False | True |
| 300×700 | 300×700 | False | True |

800×6000が512×3840になるのは最終案の例と一致する。原本の短辺が512px未満の250×3000が縮小されないのも仕様どおりである。倍率式の性質として、縮小後の短辺は必ず512px以上になるか原本のままなので、`min_short_edge_pixels=200`を下回る縮小は起こり得ない。

decompression bombは`max_pixels=1,000,000`に対して1200×1200（1.44M pixel）を`max_pixels`で拒否した。Pillowが2倍以下ではwarningしか出さない帯域を、scoped filterの例外化が正しく捕まえている。壊れたbytesは`unsupported_image_decoder`になる。Pillow 12.3.0でWebPとAVIFがどちらも利用可能で、`preflight_image_decoders()`は`{'pillow_version': '12.3.0', 'webp': True}`を返す。

## 指摘

### 1. localのfile書き込み失敗がdecoder不良として恒久終端になる — 重大度: 高

`feedian/image_ocr.py:525`の`except (UnidentifiedImageError, OSError, SyntaxError, ValueError)`は、`try`の中にある一時fileの書き込みまで覆っている。`_save_normalized_png`の`normalized.save(path)`と、素通し経路の`_write_temporary_image`が、どちらもこの`try`の内側にある。

`temporary_parent`に通常fileを指して実際に呼んだ結果は次のとおりである。

```text
resize path,  unwritable parent -> failed 'unsupported_image_decoder' transient=False
passthrough,  unwritable parent -> failed 'unsupported_image_decoder' transient=False
```

`transient=False`なので`complete_group`の`terminal`分岐に入り、`unavailable:unsupported_image_decoder`として記録され、`_due`は同じtargetで二度と選ばない。終了コードは0のままである。**disk fullや一時的な書き込み拒否という完全にlocalな事象が、「この画像は読めない」という恒久的な事実として保存され、しかも誰にも通知されない。**

これは後退でもある。変更前は`_write_temporary_image`のOSErrorが`fetch_image`の`except Exception`まで上がり、`transient=True`として次回1回再試行され、終了コードも1だった。

`.feedian/tmp`はVault内にあり、10万行規模のVaultとsnapshotを抱える環境でdisk fullは想像上の話ではない。decodeの失敗と、decode結果を書き出す先の失敗は、別の事象として扱う必要がある。

対処案: decodeを行う範囲だけを`try`で囲み、一時fileの書き込みは外へ出す。書き込みのOSErrorは従来どおり`transient=True`の失敗として扱う。

### 2. gate一致がLLM分類済み行のpayloadを捨てる — 重大度: 中

`feedian/image_ocr.py:791`の`_gate_values`は`analysis_status == "completed"`のときだけpayloadを保持する。一方、同じfileの`_has_adopted_result`（`feedian/image_ocr.py:646`）は`ignored`かつ`analysis_method`と`analysis_input_fingerprint`が非NULLの行も「採用済み結果あり」と判定し、`_terminal_values`はそちらを使っている。**同じ「採用済み結果」の定義が、gate経路と終端経路で食い違っている。**

結果として、`classification:photo`のようなLLM分類済み`ignored`行に新しいgate規則が一致すると、最後の`_attempt_values`分岐へ落ちて`analysis_method`、`analysis_input_fingerprint`、`image_kind`、`image_sha256`が空で上書きされる。

具体的には、Vault所有者が`photo` tokenを試しに追加すると、実測で29行の分類済み行がpayloadを失う。その後「厳しすぎた」と判断して規則を外すと、それらの行は`_retained_completed_gate`にもlegacy adopt条件にも該当しないため、全件が再取得・再解析される。**filterを試して戻す往復に再解析costがかかるのは、目的3が防ごうとしたものそのものである。**

最終案のgate表は`completed`と「採用済み結果なし」の2つしか書いておらず、終端表の「`completed`またはLLM分類済み`ignored`」と非対称になっている。実装は最終案どおりだが、仕様側の非対称が実害を生んでいる。仕様を終端側に揃え、`_gate_values`でも`_has_adopted_result`を使うのが筋が通る。復帰条件も`image_kind='explanatory'`固定ではなく、保持payloadの有無で判定する必要がある。

### 3. 縮小していない画像に`short_edge_floor_applied`が立つ — 重大度: 中

`feedian/image_ocr.py:496`の`short_edge_floor_applied = short_scale > long_scale`は、`scale >= 1.0`の早期returnより前で評価される。そのため、**縦横比が2:1を超える画像は、長辺が上限以下で一度も縮小されていなくてもtrueになる。**

上の表のとおり、300×700、700×300、250×3000はいずれも`resized=False`でありながら`short_edge_floor_applied=True`を返す。この値は`llm_run.request_json.logical`へ保存され、検証11が要求する「短辺下限の適用有無」そのものである。次回の実測で「短辺下限がどれくらい効いたか」を数えると、規則が一度も働いていない画像が大半を占める数字が出る。

監査fieldを足した目的は次の判断材料を作ることなので、意味が合っていないと足した意味がない。早期returnの側では`False`を返すか、`resized and short_edge_floor_applied`の形で保存する。

### 4. token percentileが0のとき`unknown`と表示される — 重大度: 低

`feedian/image_ocr.py:1365`とその次の2行は`report.input_tokens_per_request_p50 or 'unknown'`という形で、0を`unknown`へ潰す。

usageを返さないbackendでは`usage.get("input_tokens", 0)`が0になり、sampleが全て0になる。percentileは0と正しく計算されているのに、完了行は「計算できなかった」と表示する。`is None`で判定すること。`input_tokens_per_request_avg`は既に`is not None`で正しく書かれているので、3行だけ揃っていない。

### 5. 自由文の例外messageが終端理由の語彙に混ざる — 重大度: 低

`feedian/image_ocr.py:609`の`except ValueError as exc`は`reason = str(exc)`を使う。`_terminal_reason`がこれに`unavailable:`を付けるため、`last_failure_kind`と`failure_kinds` counterのkeyが例外messageそのものになる。

`validate_fetch_url`がprivate addressやscheme違反を弾いたとき、`unavailable:Refusing to fetch ...`のような文字列が保存され、CLIは`failure_kind[その文]=1`を出す。最終案は理由を固定形式に揃えると定めており、理由別集計はVault調整の手順が依存する出力である。messageごとにbucketが割れると集計にならない。`unavailable:blocked_url`のような固定値へ丸め、messageは`analysis_warning`へ入れるのが妥当である。

### 6. `_retained_gate_result`がgate由来でない古いtargetまで拾う — 重大度: 中（指摘2の修正で発生）

指摘2の修正で入った`_retained_gate_result`（`feedian/image_ocr.py:669`）は、次の5条件だけで「gateが保持した結果」と判定する。

- `analysis_status == 'ignored'`
- `_has_adopted_result(row)`
- `last_attempt_status == 'ignored'`
- `last_failure_kind`が空
- `last_attempt_target != target`

**このどこにも「直前がgate一致だった」ことを示す条件が無い。** LLM分類済み`ignored`行は`analysis_status='ignored'`、`analysis_method='llm'`、`analysis_input_fingerprint`非NULL、`last_attempt_status='ignored'`、`last_failure_kind`空をすべて満たすので、targetが何らかの理由で古くなっただけで条件が成立する。`_row_action`（`feedian/image_ocr.py:893`）は`_retained_gate_result`をlegacy adopt判定より前に置くため、その行は`adopt`になり、targetだけ書き換えて再解析されない。

確定仕様の該当箇所は「**規則を削除してtargetが`pass`へ変わった場合**」と条件を限定している。実装の条件はそれより広く、gateと無関係なtarget変化まで拾う。`attempt_target`のpayloadには`backend`、`prompt_version`、`schema_version`、`timeout_seconds`、`max_bytes`、`max_pixels`、`min_short_edge_pixels`が入っており、これらのどれが変わっても同じことが起きる。

合成行に対して`_row_action`を直接呼んだ結果は次のとおりである。gateは`pass`、backendを`openai-responses`から`claude-code-local`へ変えてtargetを古くした。

| 行 | 変更後の`_row_action` |
|---|---|
| LLM分類済み`ignored`（`image_kind='photo'`、`analysis_method='llm'`） | `adopt`（再解析しない） |
| `completed`（`image_kind='explanatory'`） | `fetch`（再解析する） |
| SVG `completed`（`analysis_method='svg_text'`） | `fetch`（再解析する） |

`max_bytes`を変えた場合もLLM分類済み行は`adopt`になる。

つまり**backendを切り替えても`IMAGE_OCR_PROMPT_VERSION`を上げても、過去に非説明と分類された行だけは新しいbackend・promptで再判定されない。** 実測Vaultでは`completed` 591行が再解析される一方、`classification:*` 2,161行が黙って据え置かれる。この非対称はcodeを読んでも予想できない。

分岐前のmainでは、targetが変わればLLM分類済み行も`fetch`になっていたので後退でもある。新しいpromptやmodelが以前は見落とした説明画像を拾えるようになっても、それらの行には二度と機会が回らない。

対処案: gate由来であることを判定に入れる。schemaを増やさずに済む手掛かりとして、`_gate_values`が書く行は`last_attempt_fingerprint`がNULLになる（`_attempt_only_values`のfingerprint既定が空文字で、非採用経路の`_attempt_values`も空文字を渡す）のに対し、LLM解析が書いた行には`analysis_fingerprint`の値が入る。`_retained_gate_result`へ`not row["last_attempt_fingerprint"]`を足せば、gateが書いた行だけに絞れる。ただし`_adopt_target_values`は`analysis_input_fingerprint`を`last_attempt_fingerprint`へ書くので、一度adoptした行との相互作用を確認したうえで採否を決めること。

### 7. malformed URLの`InvalidURL`が一過性失敗に分類される — 重大度: 中（実Vault実行で判明）

`fetch_image`の最後の`except Exception as exc`（`feedian/image_ocr.py:634`）は`reason=type(exc).__name__.lower()`、`transient=True`を返す。`http.client.InvalidURL`は`HTTPException`の子で`OSError`でも`ValueError`でもないため、ここへ落ちる。

実Vaultの`--limit 100`実行で`failure_kind[invalidurl]=9`が出た。DBを見ると9行すべて同じURLだった。

```text
https://japan.cnet.com/storage/2025/02/07/4886ab6ccbd89ddcc0f9da71cb6e2d41/t/276/207/d/A - 1 (1).jpeg
```

pathに素のspaceと括弧が入っており、`http.client`がrequest lineを組めずに弾いている。**このURLが後から有効になることはない。** それを`transient:invalidurl`として保存しているため、次の通常実行でもう1回だけ無駄に再取得を試み、その実行の終了コードも1になる。

これは確定仕様の目的5「利用者が対処しない終端事象で、正常に完走したcommandを失敗扱いにしない」に反する。取得側の失敗なので、`transient=False`にするだけで既存の終端規則（`transient=False`のfetch失敗はすべて終端）にそのまま乗り、仕様改訂は要らない。理由語彙は`unavailable:blocked_url`か、新設するなら`unavailable:invalid_url`が候補になる。

### 8. 解析側の非transient失敗が`transient_failed_rows`に数えられる — 重大度: 中（実Vault実行で判明）

`extract_svg_text`は`unsafe_svg`と`invalid_svg`を`ImageAnalysisResult(status="failed")`で返し、`transient`は既定の`False`のままである（`feedian/image_ocr.py:236`、`feedian/image_ocr.py:240`）。

`complete_group`の`terminal`判定は`fetched.status == "failed" and not fetched.transient`（`feedian/image_ocr.py:1164`）なので、取得は成功して解析だけが失敗したこれらは終端に入らない。続く`else`で`report.transient_failed_rows += count`（`feedian/image_ocr.py:1256`）が`result.transient`を見ずに加算し、`cli.py:614`の`return 1 if report.transient_failed_rows else 0`が終了コード1にする。

実Vaultの`--limit 300`実行で`failure_kind[unsafe_svg]=13`が出た。DB上は26行あり、実体はApp Storeバッジ（`kyoko-np.net/images/App_Store_JP.svg`）とLinkedInアイコン（`business.nikkei.com/images/onb/2024/ico_linkedin_R.svg`）だった。DTDまたはentity宣言を含むSVGで、**利用者に対処のしようがなく、内容も装飾で価値が無い。**

`_due`は`last_failure_kind`が`transient:`で始まらないので再試行しない。状態としては収束するが、fieldの名前（`transient_failed_rows`）と終了コードの意味が実態と合っていない。

確定仕様の終端規則は「**取得が**`transient=False`で失敗した場合」と取得側に限定しており、解析側の非transient失敗をどう扱うかを決めていない。一方、先行する確定仕様[説明画像のOCRとSourceノートの一意なファイル名](../specs/20260825-image-ocr-source-filenames.ja.md)は終端の列挙に「明示的に判定できた出力上限到達」という解析側の事象を含めている。**解析側にも終端がある前提と、取得側だけに限定した規則が食い違っている。** 指摘7と違い、これは仕様の決定（改訂）を要する。

参考として、現在Vaultには`analysis_status='failed'`の行が151行ある。指摘7・8を終端扱いにすると、この大半が終了コードに影響しなくなる。

### 9. `gate_decision`がparse不能なURLで実行全体を落とす — 重大度: 高（PR #23のCodexレビューで判明）

`gate_decision`（`feedian/image_ocr.py:148`）は`urlsplit(source_url)`をそのまま呼ぶ。角括弧が閉じていないURLに対してPythonの`urlsplit`は`ValueError: Invalid IPv6 URL`を送出する。

```text
>>> urlsplit("http://[bad/chart.png")
ValueError: Invalid IPv6 URL
```

`enrich_images`はplanを組む段階で全行分の`targets`、`gates`、`legacy_targets`を作り、末尾の残件再scanでも全行に対して`gate_decision`と`attempt_target`を呼ぶ。**どれも取得の前なので、保存済みURLが1件でもparse不能だと`enrich-images`全体がtraceback で落ちる。** 取得側の終端処理まで到達しない。

`AGENTS.md`の「1ページの取得が壊れても、記録して次へ進む」という方針に正面から反する。またこの仕様が目的5で「正常に完走したcommandを失敗扱いにしない」と決めた線より手前で、完走そのものができなくなる。

`gate_decision`で`ValueError`を捕捉して`pass`を返せば、その行は通常どおり`fetch_image`へ流れる。実測すると`fetch_image`は`invalid_fetch_response` / `transient=False`を返すので、既存の終端規則で`unavailable:invalid_fetch_response`として記録され、実行は続き終了コードは0になる。gate側に終端判定を持ち込む必要はない。

## 仕様との差分（指摘ではない）

- `_due`のgate分岐（`feedian/image_ocr.py:620`付近）は`force`より前に置かれており、`--force`でもgate行がdueにならない。最終案は「`_due`へ`force`より前段の判定を持ち込まない」「`--force`時に再評価はされるが冪等な再書き込みになる」と書いている。観測できる結果（取得しない、payloadが変わらない）は同じかより強いので実害は無いが、検証15の文面とは一致しない。仕様文を実装に合わせるか、実装をコメントで補うかを決めておきたい。
- `_legacy_attempt_target`は移行が終わった後も全行・毎回計算される。1行あたりJSON serializeとSHA-256が1回増え、末尾の残件再scanでもう1回増える。10万行規模では無視できる量だが、移行完了後に落とせる処理であることを覚えておく価値はある。
- `preflight_image_decoders()`と backend の model 検査が`if planned_fetch_urls:`の内側へ移った。取得予定が無ければ画像非対応backendでも失敗しなくなる。目的5の方向と一致しており、後退ではないと判断した。

## 採否

| 指摘 | 採否 | 対応 |
|---|---|---|
| 1 | 採用 | Pillowのdecode・変換範囲だけでdecoder例外を捕捉し、一時fileの書き込みをその外へ出した。書き込み`OSError`は`temporary_image_io`の一時失敗となり、終了コード非0と自動再試行を維持する。 |
| 2 | 修正して採用 | `_gate_values`の採用済み判定を`_has_adopted_result`へ統一した。LLM分類済み`ignored`は分類payloadと理由を保持し、gate削除時は外部requestなしに新targetを採用する。確定仕様の`改訂2`にも同じ状態遷移を反映した。 |
| 3 | 採用 | `short_edge_floor_applied`は実際に縮小し、かつ短辺下限が長辺規則より縮小倍率を大きくした場合だけ`true`とした。非縮小画像は常に`false`になる。 |
| 4 | 採用 | p50・p95・最大の表示を`None`判定へ変更し、計算値0を`unknown`へ潰さないようにした。 |
| 5 | 採用 | URL policy違反を固定理由`blocked_url`へ丸め、自由文は`warning`から`analysis_warning`へ渡すようにした。その他の未知の`ValueError`も`invalid_fetch_response`へ固定した。 |
| 6 | 採用 | `_retained_gate_result`へ`last_attempt_fingerprint IS NULL`を追加し、gateが書いた試行だけを復帰対象に限定した。通常のLLM分類結果とgate往復後にローカル採用済みの結果はfingerprintを持つため、backend・prompt・schema・資源gateの変更では`fetch`になって再解析される。確定仕様の`改訂3`にも識別条件を反映した。 |
| 7 | 採用 | `fetch_image`へ`except InvalidURL`を足し、`reason='blocked_url'`・`transient=False`にした。既存の終端規則にそのまま乗るため仕様改訂は不要。理由語彙は新設せず`blocked_url`へ丸め、例外messageは`warning`から`analysis_warning`へ残す。`tests/test_image_ocr.py`に`test_malformed_url_is_terminal_and_not_retried`を追加した。 |
| 8 | 保留 | 解析側の非transient失敗（`unsafe_svg`、`invalid_svg`、`output_limit`）を終端に含めるかは、確定仕様の終端規則が取得側に限定していることの是非そのものであり、改訂を要する。report fieldの名前と終了コードの定義も併せて決める必要があるため、本PRの範囲に入れず別仕様へ回す。実害は「終端事象が発生した実行だけ終了コードが1になる」ことに限られ、`_due`は再試行せず状態は収束する。データ損失も再解析ループも無いことは確認済みである。 |
| 9 | 採用 | `gate_decision`の`urlsplit`を`try`で囲み、`ValueError`なら`pass`を返すようにした。その行は通常どおり取得へ流れ、`fetch_image`が`invalid_fetch_response`の終端として分類する。`tests/test_image_ocr.py`に`test_unparsable_url_passes_the_gate_instead_of_aborting_the_run`を追加した。 |

## 検証

- `python -m pytest -q tests/test_image_ocr.py` — 32 passed
- `python -m pytest -q` — 636 passed, 1 skipped
- `python -m ruff check feedian tests` — 成功
- passthroughとresizeの両経路で一時file書き込みを`OSError("disk full")`にし、`reason='temporary_image_io'`、`transient=True`になることを確認した。
- LLM分類済み`ignored`へgateを追加して削除する往復で、`analysis_method`、`image_kind`、`ignored_reason`、`image_sha256`、`analysis_input_fingerprint`が維持され、backend呼び出しが0件のままになることを確認した。
- 非縮小の20×2,000px画像で`short_edge_floor_applied=False`、token percentileが0のreportで`p50=0`、`p95=0`、`max=0`になることを確認した。
- URL policyの自由文拒否理由が`blocked_url`へ固定され、詳細が`warning`へ残ることを確認した。
- `_prepare_raster`の直接呼び出しによる寸法・flag・例外分類の確認（本文の表と指摘1・3の再現）
- `preflight_image_decoders()`の実行（Pillow 12.3.0、WebP有効、AVIF有効）

再レビュー (2026-08-26):

- `python -m pytest -q` — 636 passed, 1 skipped, 48 subtests passed
- `_prepare_raster`を直接呼び、300×700・700×300・250×3000が`short_edge_floor_applied=False`、800×6,000だけが`True`になることを確認した（指摘3の修正）。
- `temporary_parent`へ通常fileを指し、縮小経路と素通し経路の両方で`OSError`が`_prepare_raster`の外へ抜けることを確認した。`fetch_image`の新しい`except OSError`が`temporary_image_io` / `transient=True`へ落とす（指摘1の修正）。
- `extract.py:1061,1063,1074`の例外messageが`blocked_url`のmarkerと実際に一致すること、`image_ocr.py:374`の`ValueError("max_bytes")`が`max_bytes`へ落ちることを確認した（指摘5の修正）。
- p50・p95・最大の3行がすべて`is not None`判定になっていることを確認した（指摘4の修正）。
- 合成行に対する`_row_action`で指摘6を再現した。
- 指摘6の修正後、gate適用前のLLM分類済み`ignored`と、gate追加・削除を一往復した同じ行の両方で、backend変更後の`_row_action`が`fetch`になることを確認した。gate削除そのものは従来どおり外部request 0件でローカル採用される。

再レビュー2 (2026-08-26) — レビュー者による独立確認:

- `python -m pytest -q` — 636 passed, 1 skipped, 48 subtests passed / `python -m ruff check feedian tests` — All checks passed
- 合成行に対する`_row_action`で、backendを変えてtargetを古くしたLLM分類済み`ignored`行が`fetch`へ戻り、`completed`行との非対称が解消したことを確認した。
- gate追加から削除までの往復を`_gate_values`・`_restore_gate_values`・`_adopt_target_values`で辿り、`completed`は`restore`、LLM分類済み`ignored`は`adopt`となり、どちらもpayloadを保ったまま次回`none`へ収束することを確認した。
- `tests/test_image_ocr.py:654`の`test_gate_round_trip_preserves_llm_ignored_payload_without_reanalysis`が、往復の前後どちらでもbackend変更時に`fetch`になることを検証している。回帰側の否定条件まで含まれており、検証16の要求を満たす。

実Vault実行後 (2026-08-26) — 指摘7の修正確認:

- `python -m pytest -q` — 637 passed, 1 skipped, 48 subtests passed
- `python -m ruff check feedian tests` — All checks passed / `git diff --check` — 問題なし
- Vault DBを読み取り専用copyで確認し、`transient:invalidurl` 9行が同一のmalformed URL、`unsafe_svg` 26行がApp StoreバッジとLinkedInアイコンであることを特定した。
- 既存の9行は`last_failure_kind`が`transient:`で始まり`transient_retry_used`が未使用のため、次の通常実行で1回だけ再取得され、そこで`unavailable:blocked_url`の終端へ落ちて収束する。


## 規約化した項目

なし。今回の9件は同種指摘の2回目には該当しない。
