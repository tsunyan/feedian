# 画像OCRの効率化と調整可能なgate

ステータス: 確定

## 最終案

本節はレビュー1・2の実測を踏まえた確定内容である。数値の出典は`## レビュー`にある。実測条件はbackend `openai-responses`、model `gpt-5.6-terra`、`enrich-images --limit 100`×2回、`llm_run` 764件（入力715,124 token、出力79,217 token）、`resource_image` 103,034行である。

### 目的

1. 画像解析に対応するすべてのbackendへ送る画像の入力tokenを減らす。実測では入力tokenの79.8%が画像本体で、長辺1,024px上限により全体の25.7〜38.7%を削減できる見込みである。**この幅は短辺下限を考慮しない上限値であり、全画像が同じ縦横比だと置いた感度分析である。** 短辺下限は縦横比2:1を超える画像で縮小を弱めるため、実測値はこの範囲を下回る。現行DBに画像寸法の記録が無いためこれ以上は詰められず、それを直すのが後述の監査情報である。実測がこの範囲を下回っても仕様の失敗ではない。
2. URL gateをcode変更なしでVaultごとに調整できるようにする。実測では、説明画像を1件も出さなかった5 hostだけで入力tokenの20.4%を消費しており、削減の主軸はこちらにある。
3. filter変更で無関係な完了画像を再解析しない。
4. planning値、実request数、共有後の行数、処理前後の残件を区別して読めるようにする。
5. 利用者が対処しない終端事象で、正常に完走したcommandを失敗扱いにしない。

### 全backend共通の画像縮小・正規化

`image_ocr.max_long_edge_pixels`を追加し、**既定値を`1024`とする**。SVGを除くraster画像について、既存のMIME、download byte数、pixel数、animated判定を通過した後、backendを呼ぶ前に共通前処理を行う。対象は`openai-responses`、`codex-local`、`claude-code-local`を含む画像解析対応backendすべてで、provider固有の画質指定は設けない。

前処理の規則は次のとおりである。

- EXIF orientationを反映した実表示方向を基準に寸法を求める。
- 縮小倍率は`k = min(1, max(max_long_edge_pixels / 長辺, (max_long_edge_pixels / 2) / 短辺))`とする。第1項が長辺上限、第2項が短辺下限であり、**上限1により拡大は起こらない。**
- 短辺下限を置くのは、長辺だけで切ると縦長の解説画像が読めない大きさまで潰れるためである。800×6,000の図は長辺1,024規則だけなら137×1,024になり文字が失われるが、この下限により512×3,840で止まる。縦横比2:1までは長辺規則がそのまま働く。
- **短辺下限が保証されるのは、原本の短辺が`max_long_edge_pixels / 2`（既定512px）以上の画像だけである。** 原本の短辺がそれ未満で長辺が上限を超える画像は`k=1`となり、縮小せず原本のまま渡す。たとえば200×200,000pxは`min_short_edge_pixels=200`と`max_pixels=40,000,000`の両gateを通るが、短辺を512pxへ合わせると2.56倍の拡大になるため、上限1がそれを禁じる。この経路の資源消費は既存の`max_bytes`と`max_pixels`が今日すでに縛っており、本仕様で悪化しない。専用gateも新しい設定値も設けない。気にするVaultは`max_pixels`を下げる。
- **正規化後のpixel数は、いかなる場合も原本のpixel数を超えない。** これを実装の不変条件とする。
- 縮小時は文字の輪郭を不必要に崩さない高品質な縮小filterを使い、metadataを除いたPNGへ再encodeする。
- 長辺が上限以下なら再encodeせず、取得した画像をそのままbackendへ渡す。
- animated rasterとSVGの扱いは現行仕様を維持する。SVGはLLMへ送らず、安全なXML text抽出だけを行う。
- 正規化後の一時画像はrequest終了時に破棄し、永続保存しない。

実測では画像tokenがおよそ`pixel/1024`（32×32 patch相当）で推移しており、費用と直結するのは長辺ではなくpixel面積である。面積上限のほうが制御としては素直だが、設定名としての分かりやすさを優先して長辺で表す。上の短辺下限は、その差から生じる縦長画像の潰れを塞ぐためのものである。

#### decoder依存

本仕様は画像decoderへの依存を新たに導入する。現行実装はheader解析だけで寸法を得ており、decoderを持たない。**採用libraryはPillow 12.3.0とし、`pyproject.toml`へ`Pillow>=12.3.0`、`requirements.txt`へ`Pillow==12.3.0`を追加する。** version方針は既存依存に合わせ、前者は下限、後者は正確なpinとする。

対応formatは必須と任意に分ける。**この区別が無いと、AVIF decoderを持たない実装が合格なのか不合格なのかが本文から決まらない。**

- **必須** — `image/png`、`image/jpeg`、`image/gif`、`image/webp`。これらは縮小に対応しなければならない。WebPはPillowのoptional build featureだが、利用者判断によりFeedianでは必須とする。画像取得前のpreflightで`PIL.features.check_module('webp')`を確認し、無効なinstallでは外部取得を始めず明確に失敗する。CIでもWebPのdecodeと縮小を必須testにする。
- **任意** — `image/avif`。Pillowの版と同梱codecによって可否が変わる。導入環境がAVIFをdecodeできない場合は下の未対応経路へ落ちて原本をそのまま渡す。これを不合格としない。

network越しの信頼できないbytesをdecodeする以上、decodeは既存の`max_bytes`と`max_pixels`を越えて資源を消費してはならない。**decompression bomb検知は警告で続行させず、必ず処理中止にする。** Pillowの`Image.MAX_IMAGE_PIXELS`を`max_pixels`に一致させるだけでは、上限超過から2倍以下は`DecompressionBombWarning`に留まる。decode処理を囲むscoped warning filterでこのwarningを例外へ変え、`Image.open`後かつpixel dataの`load`前にもPillowが認識した`width * height`を`max_pixels`と照合する。process全体の無関係なwarning filterは変更しない。

decodeできない場合の扱いは3つに分ける。**この区別は必須である。**

- **必須formatのdecoderが無い** — install不備として画像取得前のpreflightでcommandを失敗させる。PNG、JPEG、GIFはPillow本体、WebPは`PIL.features.check_module('webp')`で確認し、原本fallbackには入れない。
- **任意formatのcodecが無い、またはそのformatに未対応** — 縮小せず、取得した原本をそのままbackendへ渡す。対象はAVIFである。AVIFは現行の`SUPPORTED_RASTER_MIMES`に含まれ、今日そのまま渡って成功しているため、こちら側にdecoderが無いという都合で成功中のOCRを失ってはならない。
- **bytesが壊れていてdecodeできない** — `unavailable:unsupported_image_decoder`として終端無視にする。

`max_long_edge_pixels`の変更は新規解析へだけ適用する。**この値を`attempt_target`のpayloadへ入れてはならない。** `max_bytes`、`max_pixels`、`min_short_edge_pixels`は既にpayloadに入っているが、それらは解析の可否を変えるgateであるのに対し、`max_long_edge_pixels`は送る画像の中身を変えるだけで、model変更と同じ扱いになる。比較や再取得が必要なら`--force`を使う。実際の元寸法と送信寸法は`llm_run.request_json`へ保存する。

backend境界には正規化後（または原本）の画像実体、media type、送信寸法を渡す。**再encodeしたときは一時fileの拡張子も正規化後のものへ更新する。** `codex-local`は`media_type`を捨てて`image_path`だけをCLIへ渡すため、media typeの受け渡しだけでは不十分である。

### URL gate設定の外部化

現在code定数である名前tokenとURL接頭辞をVaultの`.feedian/config.json`へ移し、`image_ocr`に次を追加する。

```json
{
  "image_ocr": {
    "max_long_edge_pixels": 1024,
    "ignore_name_tokens": [
      "2x", "3x", "logo", "icon", "avatar", "profile", "button", "btn",
      "banner", "bnr", "badge", "sprite", "spacer", "blank", "emoji", "favicon"
    ],
    "ignore_url_prefixes": [
      "b.hatena.ne.jp/entry/image/",
      "b.hatena.ne.jp/bc/",
      "pbs.twimg.com/amplify_video_thumb/",
      "pbs.twimg.com/ext_tw_video_thumb/",
      "pbs.twimg.com/tweet_video_thumb/",
      "pbs.twimg.com/card_img/",
      "pbs.twimg.com/cards/",
      "pbs.twimg.com/media/",
      "i.ytimg.com/vi/",
      "lh3.googleusercontent.com/a/",
      "profile-image.kraken.asahi.com/"
    ]
  }
}
```

設定はcode既定値への追加ではなく、そのVaultで使う完全な有効値とする。利用者は項目を削除して既存gateを解除でき、追加して厳しくできる。新しいFeedian versionが利用者の知らない除外規則を既存Vaultへ自動追加しない。**この判断の根拠は、実測で削減の主軸がVaultごとの調整にあったことである。** 既定値へ追加する3規則（`bnr`、`i.ytimg.com/vi/`、`lh3.googleusercontent.com/a/`）の効果は入力tokenの0.97%にすぎない一方、説明画像を1件も出さなかった5 hostは20.4%を占めていた。後者は利用者が自分のVaultで刈り取るものであり、その手段を残すことが本節の目的である。

`ignore_name_tokens`は次の規則で扱う。

- 小文字ASCII英数字だけを許可する。
- URLのhostname、query、fragment、altは対象にせず、URL decodeしたpath segmentとbasenameを`[^a-z0-9]+`で分割した完全tokenだけに一致させる。
- 重複と順序を正規化してから保存・比較する。
- 正規表現、部分一致、globは許可しない。

完全token一致にしたため、`logo2`や`icon01`のように数字が続くsegmentは一致しない。Vault全体63,148 URLのうち該当は76件（0.12%）で、無視してよい水準である。

`ignore_url_prefixes`は`hostname/path-prefix`形式だけを許可する。scheme、query、fragment、userinfoは設定へ書かせない。hostnameは小文字化し、path prefixは先頭`/`を除く。redirect先ではなく、保存済み`source_url`へ取得前に適用する。redirectの安全性は従来どおりNetworkPolicyが担当する。

**hostnameは完全一致とし、番号付きshardは対象にしない。** 実測ではytimg系10 requestのうち`i.ytimg.com`が4件、`i1`〜`i4.ytimg.com`が6件で、後者には説明画像が1件含まれていた。shardまで広げるとその1件を落とすため、広げない。ytimg全体でも入力tokenの0.65%であり、精度を落としてまで拾う価値がない。同じ理由で、shardを持つhostを既定値へ足すときは代表hostだけを書く。

一致時の`ignored_reason`は`name_token:<token>`または`url_prefix:<設定値>`とし、どの設定で落ちたかをそのまま集計できるようにする。**現行の`name_pattern:` / `denylist:`形式で保存されている行は、移行時の再評価ですべて新形式へ上書きする。** 据え置くと理由別集計が新旧混在する。

### Vaultごとに追加を推奨する設定

既定値には入れないが、実測で効果が確認できたものを`DESIGN.md`とREADMEへ運用手順として記載する。**gate調整の機能だけを用意して使い方を書かないと使われない。** 手順は「`enrich-images`の`ignored_reason`別・host別の集計を見て、説明画像が出ていないhostを`ignore_url_prefixes`へ足す」である。

実測で得られた候補は次のとおりである。いずれも利用者の判断で足すものであり、既定値にはしない。

| 候補 | 一致 | 非説明 | 説明 | 節約token | 全体比 |
|---|---:|---:|---:|---:|---:|
| host `spotlight.fantia.jp` | 10 | 10 | 0 | 47,832 | 6.7% |
| host `media.vogue.co.jp` | 8 | 8 | 0 | 32,130 | 4.5% |
| host `dailyportalz.jp` | 8 | 8 | 0 | 25,493 | 3.6% |
| host `nazology.kusuguru.co.jp` | 22 | 22 | 0 | 21,891 | 3.1% |
| host `media.loom-app.com` | 29 | 29 | 0 | 18,752 | 2.6% |
| token `photo` | 32 | 29 | 3 | 59,189 | 8.3% |
| token `storage` | 34 | 32 | 2 | 32,004 | 4.5% |

`photo`と`storage`を既定値へ入れない判断は草案のまま維持する。`photo`は説明画像を3件落とし、`storage`は34件中32件が単一siteに由来する汎用性のない語である。ただし**落とすものと得るものの実数を上表に残す**ことで、あるVaultでこの取引を選ぶ判断ができるようにする。既定値は保守的に、調整は利用者に、という配分である。

### filter変更と再評価target

改善後のtargetはfilter一覧全体ではなく、各URLに対する**有効なgate判定**を含める。値は`name_token:<token>`、`url_prefix:<prefix>`、`pass`のいずれかである。

- 規則追加で新しく一致した行だけが`pass`からrule IDへ変わり、取得せずローカルに更新される。採用中の完了結果があればpayloadを保持した`ignored`、無ければ通常の`ignored`にする。
- 規則削除で該当行だけがrule IDから`pass`へ変わる。gateが保持していた完了結果は外部requestなしに`completed`へ戻し、それ以外だけを通常の取得・解析対象へ戻す。
- 一致しない行はfilter一覧が変わっても`pass`のままで、既存のLLM結果を再利用する。

**草案にあった`gate_version`は設けない。** gateは毎回URLからローカルに再計算されるため、規則やアルゴリズムを変えれば該当行の判定値が変わり、変わらない行は再解析する必要がない。`matched_rule`が既にその役割を果たしており、`gate_version`をbumpすべき具体的な状況が存在しない。用途の定義されないversion fieldは、後で「念のため上げる」運用に流れ、本仕様が防ごうとしている全画像の再解析を招く。全行の再解析が本当に必要になった場合は`--force`を使う。

config v3からの移行直後も、既存の完了・LLM分類無視結果が新gateで`pass`なら外部requestなしで新targetを採用する。旧gateで無視され、新gateで`pass`になった行だけを再評価する。target形式変更だけで全画像を再取得・再解析してはならない。

新規則に一致する行はローカルで次のように更新する。**gate一致と終端取得失敗は、利用者の意思と到達不能という異なる事象なので、同じ状態遷移へまとめない。**

| 一致前の状態 | 一致中の`analysis_status` | payload |
|---|---|---|
| `completed` | `ignored` | `ocr_text`、`image_kind='explanatory'`、`analysis_method`、`analysis_input_fingerprint`、`image_sha256`など採用済みpayloadを保持し、`ignored_reason`と`last_attempt_*`だけをgate結果へ更新する |
| 画像変更確認後の`pending` | `pending` | 保持中の旧payloadを上書きせず、gate結果を試行列へ記録する |
| 採用済み結果なし | `ignored` | 従来どおりgate無視結果を書く |

保存済み完了結果を保持したgate無視は、`analysis_status='ignored'`かつ`image_kind='explanatory'`、`analysis_method`と`analysis_input_fingerprint`が非NULLであることから、schema追加なしに識別する。規則を削除してtargetが`pass`へ変わった場合は、この行だけをローカルに`completed`へ戻して`ignored_reason`を消す。もともと`pending`だった行や採用済み結果の無い`ignored`行は復活させず、通常の取得・解析対象へ戻す。

同じrule IDが有効な間は、`last_attempt_status='ignored'`と同じ`last_attempt_target`を持つ行を、現在の`analysis_status`にかかわらずdueにしない。rule IDが変わった場合だけローカル更新し、`pass`へ変わった場合だけ上の復帰または通常処理を行う。

**gate抑止と終端抑止は`last_attempt_status`では区別できないので、`last_failure_kind`で判別する。** gate一致行は`last_failure_kind`がNULL、終端事象行は`unavailable:*`または`resource_limit:*`である。両者は`--force`の効き方が違うため、この判別を省いて1つの規則にまとめてはならない。

- **gate一致行** — `--force`でも取得と解析は行わない。ただし`_due`は`force`を最初に評価する（`feedian/image_ocr.py:435`）ので、`--force`時にこの行は再評価される。**そこで起きるのは抑止の解除ではなく、取得を伴わない冪等な再書き込みである。** 保持payloadは変化してはならない。gate優先は現行実装と同じく`_due`の下流にある取得前gate分岐で担保し、`_due`へ`force`より前段の判定を持ち込まない。`--force`の意味が呼び出し場所ごとに変わるためである。
- **終端事象行** — `--force`は抑止を解除し、再取得する。設定変更でtargetが変わった場合も同じである。

この分離と抑止が無いと、filter調整のたびに完了OCRが失われるか、`pending`行が毎回dueになって残件が収束しない。現行の取得前gate分岐は`ImageAnalysisResult(status="ignored", ignored_reason=reason)`を現在値の更新へ渡すため、`completed`行のpayloadを空で上書きする。本仕様はfilter調整を日常操作にするので、ここで可逆な状態遷移へ直す。実測では`photo`を足すだけで説明画像3件が一致する。

実測では、旧gateで無視されている5,173行のうち新gateで`pass`へ戻るのは12行・URLで11件だけだった。うち10件は`profile-image.kraken.asahi.com/<hash>`で、意味がhostnameにありpathが裸のhashのため、path tokenでは拾えない。これは上の既定`ignore_url_prefixes`へ加えることで0になる。残る1件は`t0.gstatic.com/faviconV2?…`で、segmentが`faviconV2`のためtokenが`faviconv2`となり`favicon`と一致しない。shardを持つhostのため既定値へは足さず、この1件が新たに解析対象になることを許容する。

### 終端画像の扱い

**列挙ではなく規則で定義する。取得が`transient=False`で失敗した場合はすべて終端事象として扱い、commandの終了コードを非0にしない。** 取得できなかった事実と理由は保存する。列挙形式にすると、次に別の非transient理由が現れるたびに仕様改訂が必要になる。

**終了コードを0にすることと、現在値を無視結果へ置き換えることは分離する。** 状態遷移は、採用済み結果の有無と、新しい画像SHA-256の差を確認できたかで決める。

| 終端事象前の状態 | 新しいSHA-256 | `analysis_status`とpayload |
|---|---|---|
| 採用済み結果あり（`completed`またはLLM分類済み`ignored`） | 取得不能で不明、または採用中と同じ | statusと採用済みpayloadをすべて維持し、`last_attempt_*`と`last_failure_kind`だけ更新する。`completed` OCRは引き続き`ingest`へ渡す |
| 採用済み結果あり | 採用中と異なることを確認 | `pending`へ戻し、旧payloadを保持する。旧OCRは`ingest`へ渡さない |
| 既に`pending`で旧payloadを保持 | 不明・同一・異なる | `pending`と旧payloadを維持し、試行列だけ更新する |
| 採用済み結果なし | 不問 | `ignored`とし、終端理由を書く |

確定済みの[説明画像のOCRとSourceノートの一意なファイル名](20260825-image-ocr-source-filenames.ja.md)は、再取得不能なら保存済み結果をcurrentのまま残し、新しい画像bytesのSHA-256が変わったことまで確認できた場合だけ`pending`へ戻すと定めている。この区別を維持する。404やheader取得失敗は保存済みOCRが古い証拠ではないため、`completed`を取り消さない。一方、bytes取得後のdecode不能でも新SHA-256が採用中と異なるなら、内容変更を確認できているので`pending`へ戻す。

現在値を保持する行では`ignored_reason`を上書きせず、終端理由を`last_failure_kind`へ保存する。終端事象の`last_attempt_status`は`ignored`とし、同じtargetでは`analysis_status='pending'`でもdueにしない。設定変更でtargetが変わるか`--force`が指定された場合だけ再試行する。

**この分離が無いと、保存済みのOCRを失う。** 現行の`_attempt_values`は`failed`なら試行列だけを更新して現在値に触れないが、`ignored`は`analysis_status`、`ocr_text`、`image_kind`、`image_sha256`をまとめて上書きする。非transient失敗を素朴に`ignored`へ変えると、完了行が404になった瞬間に保存済みOCRが空で置き換わる。実測Vaultには`completed`が591行、`http_404`が既に78行あり、link rotは前提であって例外ではない。

理由は次の形式に揃える。

```text
unavailable:http_<code>
unavailable:unsupported_image_header
unavailable:invalid_or_incomplete_header
unavailable:unsupported_image_decoder
resource_limit:max_bytes
resource_limit:max_pixels
```

同じtargetでは自動再試行しない。設定変更または`--force`で再試行できる。`max_bytes`と`max_pixels`は`attempt_target`のpayloadに含まれるため、値を上げれば自動的に再試行対象へ戻る。gateは従来どおり`--force`より優先する。

commandを非0にする`failed`は、再試行しても残った一過性network・backend障害（`transient=True`）、監査runを正常に閉じられない状態、DB書き込み失敗など、commandまたは外部serviceの動作確認を必要とするものに限定する。

実測では`failed`が94行あり、内訳は`http_404`が78行、`unsupported_image_header`が16行で、それ以外の失敗理由は0件だった。後者はすべて`assets.st-note.com`である。この94行は恒久的に残るため、現状では`enrich-images`が毎回終了コード1を返している。特定site全体のdenylist追加は行わない。

### 監査情報

rasterの`llm_run.request_json.logical`へ次を保存する。

- `image_sha256`
- 取得時の`media_type`
- `download_bytes`
- headerから得た元の`width`と`height`
- EXIF orientation反映後の元の`width`と`height`
- backendへ送った`width`、`height`、`media_type`
- 縮小を行ったかどうか、適用した`max_long_edge_pixels`、短辺下限規則が働いたかどうか

画像bytes、base64、API keyは保存しない。これらは既存JSON監査列へ入れるためSQLite schema列は増やさない。開始時requestに保存した`image_sha256`を完了時の論理requestで失わないよう、backend auditのrequest envelopeを統合する。実測で、完了時に`logical`が上書きされ`image_sha256`が残っていないことを確認した。

寸法を保存するのはこの仕様が最初である。現行の`resource_image`にも`llm_run`にも画像寸法の記録が無く、そのため今回の解析では縮小効果をaspect比の仮定つきでしか見積もれなかった。次回の実測を仮定なしで行えるようにすることが、この節の主目的である。

### reportの改善

planning値と完了値、解析groupと伝播行を別名で表示する。

開始時:

```text
remaining_resources_before
selected_resources
candidate_rows
prefetch_ignored_rows
planned_fetch_urls
planned_analysis_groups
reused_existing_rows
llm_parallelism
historical_seconds_per_request
expected_seconds
```

完了時:

```text
remaining_resources_before
remaining_resources_after
selected_resources
fetched_urls
llm_requests
llm_explanatory_groups
llm_ignored_groups
svg_completed_groups
svg_ignored_groups
gate_ignored_rows
postfetch_ignored_rows
terminal_unavailable_rows
retained_current_rows
transient_failed_rows
propagated_rows
propagated_resources
input_tokens
output_tokens
input_tokens_per_request_avg
input_tokens_per_request_p50
input_tokens_per_request_p95
input_tokens_per_request_max
```

`llm_requests`は**errorで閉じたrunを含む**。現行実装は失敗groupでも`start_llm_run`から`finish_llm_run(error=...)`までrunを開くため、この定義でなければ`llm_run`の増分と一致しない。

`gate_ignored_rows`（取得前）と`postfetch_ignored_rows`（`small_dimensions`、`animated`など取得後）を分けて出す。実測では無視理由の最大が`small_dimensions`の6,017行で、これは通信費を払った後の判定である。取得前gateの5,173行と混ぜて表示すると、節約できた通信量を過大に読む。

`terminal_unavailable_rows`は**終端事象が起きた行の数であり、その結果`ignored`になったかどうかを問わない。** 採用済み結果を持っていて`completed`を維持した行も含める。`gate_ignored_rows`が「無視した行」を数えるのに対し、こちらは「取得できなかった行」を数える。名前の非対称は意図したものである。

`retained_current_rows`は、終端事象またはgate一致でstatus・利用可否を変更する際に、採用済みpayloadを物理削除せず保持した行の数である。`gate_ignored_rows`または`terminal_unavailable_rows`の部分集合として併記し、「処理対象から外した」と「保存結果を捨てた」を区別する。

終了時には対象を読み直して`remaining_resources_after`を表示する。全`resource_image`の再scanが実行末尾に1回増えるが、この費用は許容する。

`--dry-run`では取得前に確定するfilter理由別件数を表示する。寸法gate後の件数と実tokenは従来どおり実行前に推定しない。

### 設定migration

Vault config formatを3から4へ上げる。migrationは現行code定数を`ignore_name_tokens`と`ignore_url_prefixes`へ展開し、新しい既定規則`bnr`、`i.ytimg.com/vi/`、`lh3.googleusercontent.com/a/`、`profile-image.kraken.asahi.com/`を加え、`max_long_edge_pixels=1024`を設定する。

**`@2x` / `@3x`から`2x` / `3x`への変更は値の移送ではなく意味の変更である。** 現行はURL全体に対する部分一致で`@`込みだが、新案はpath tokenなので`@`が消え、`/2x/`のようなsegmentにも一致する。migrationの説明文と`DESIGN.md`にこの差分を明記する。実測では、この変更を含めても旧gate無視行5,173行のうち`pass`へ戻るのは12行だけである。

文字列、配列、正規化後の重複、空文字、正でない`max_long_edge_pixels`、schemeやqueryを含むprefixを明示的に拒否する。未知fieldを拒否する既存方針は維持する。SQLite schema versionは上げない。

### 初版で採らない改善案

#### 分類とOCRの2段階化

実測では非説明画像がrequestの76.8%、入力tokenの77.4%を占めており、削減余地は長辺上限より一桁大きい。一方、説明画像は2 requestになり、状態・retry・監査が二段階になる。全backendで同じ条件の軽量分類を保証できず、小さい文字のある図を最初の分類で落とす可能性もある。まず1 requestのまま共通縮小とURL gateを実測し、その後も入力tokenが日常利用上問題なら別仕様で検討する。**その際は、本仕様で導入するdecoderを使い、分類passだけをさらに小さい寸法で送る形が有力である。**

#### reasoning effortの調整

実測では出力token 79,217のうち28,736（36.3%）がreasoning tokenで、`explanatory`に限れば41.1%だった。`openai-responses`は`{"effort": "low"}`を固定で送っている。出力単価が入力の8倍なら入力換算で約230,000 token相当となり、長辺上限による削減を上回る費用要因になりうる。ただしOCR精度への影響が未知で、backendごとに同じ意味の設定があるとも限らない。本仕様では**実Vaultでの確認で1水準だけ比較して数値を残すにとどめ、設定項目の追加は別仕様とする。**

#### resource横断のSHA-256 cache

764 LLM runは755種類のSHA-256で、正規化altまで含めると764件すべて異なった。加えて、保存済みOCR 177件のうち本文が重複していたのは5件だけで、いずれも単一shopの同一商品画像がsize違いURLで重複したものである。**重複そのものが3%に満たないため、新しいcache tableを追加する根拠がない。** URL queryの正規化による統合も同じ理由で採らない。

#### prompt cache最適化

764 requestすべてcached input tokenが0だった。理由は画像やaltがrequestごとに異なるからではなく、平均936 tokenの本requestがOpenAIのcache最小prefix長1,024を下回るためである。長辺上限を下げればさらに下回るので、今後も0で正しい。prompt構造を複雑にしてcacheを狙わない。

#### modelの小型化

画像入力token数そのものを減らす対策ではない。OCR専用model設定も既存backend契約を増やすため、本仕様には含めない。

#### resourceごとの候補画像数上限

従来どおり全候補をgateする。実測では画像position 0にも18件、position 10以降にも54件の説明画像があり、positionによる打ち切りは説明画像を落とす。altが空だったrequestは345件あり、そのうち108件が説明画像だったため、空altもgateに使わない。

### 検証

1. `max_long_edge_pixels`が正の整数だけを受け付け、既定値が1,024になる。
2. Pillow 12.3.0を使い、必須formatのPNG、JPEG、GIF、WebPについて、長辺が1,024pxを超える画像を縦横比を維持して縮小し、長辺が上限以下の画像を拡大・再encodeしない。WebP featureが無いinstallは画像取得前のpreflightで失敗し、CIでもWebPのdecode・縮小が成功する。
3. 原本の短辺が512px以上で縦横比が2:1を超える画像で短辺下限が働き、800×6,000の図が512×3,840になる。
4. 原本の短辺が512px未満の極端な縦長・横長画像は縮小されず原本のまま渡る。短辺200pxかつ総pixel数が`max_pixels`付近の画像（200×200,000pxなど）で拡大が起きない。
5. **正規化後のpixel数が、どの経路でも原本のpixel数を超えない。**
6. EXIF orientation、透過、壊れた画像、pixel上限超過を安全に処理する。
7. 任意formatのAVIFはdecodeできる環境では縮小し、codec未対応なら原本をbackendへ渡す。対応codecがあるのにbytesが壊れている場合だけ`unavailable:unsupported_image_decoder`になる。
8. `max_pixels + 1`の画像で`DecompressionBombWarning`が例外になり、pixel dataをloadしない。明示的な寸法照合も同じ入力を拒否し、process全体の無関係なwarning処理を変えない。
9. `openai-responses`、`codex-local`、`claude-code-local`へ同じ送信寸法の画像が渡り、provider固有設定の有無で前処理が変わらない。再encode時は一時fileの拡張子も正規化後のものになる。
10. `max_long_edge_pixels`を変えても`attempt_target`が変わらず、既存の成功結果が再解析されない。
11. 監査JSONへ元寸法、送信寸法、media type、byte数、SHA-256、縮小有無、短辺下限の適用有無が残る。
12. 名前tokenはURL pathだけに完全一致し、hostname、query、alt、部分文字列、数字接尾辞では一致しない。
13. URL prefixは正規化したhostnameとpathにだけ一致し、hostnameは完全一致で番号付きshardに一致せず、不正な設定を拒否する。
14. 完了OCRを持つ行に新しいgate規則が一致すると、`analysis_status='ignored'`になって`ignored_reason`だけが更新され、OCR・分類・指紋・SHA-256は保持される。採用済み結果の無い一致行は通常の`ignored`になる。無関係な完了行を再取得・再解析しない。
15. 同じrule IDを維持した2回目の実行では、保持payloadの有無にかかわらず対象resource、DB update、外部requestが0になり、残件が収束する。**`--force`を付けた実行ではgate一致行が再評価されるが、取得も解析も行われず、保持payloadは1 byteも変化しない。同じ`--force`で終端事象行は再取得される。** `last_failure_kind`でこの2つを判別していることを確認する。
16. gate規則を削除すると、保持した完了結果だけが外部requestなしに`completed`へ戻り、`ignored_reason`が消える。もともと`pending`または採用済み結果の無い`ignored`だった行だけが通常対象へ戻る。
17. config v3からv4へのmigrationで既存の完了・LLM無視結果を外部requestなしに維持し、`name_pattern:` / `denylist:`形式の`ignored_reason`が新形式へ上書きされる。
18. `transient=False`のfetch失敗が理由付きの終端事象になり、それだけなら終了コード0になる。404、410、未対応header、資源上限、decode不能を含み、採用済み結果を持たない行だけが`ignored`になる。
19. 完了OCRを持つ行を`--force`し、取得が404 / 410になるか、新SHA-256が不明または採用中と同じ状態でdecode不能になっても、`analysis_status='completed'`と全payloadが維持され、OCRが引き続き`ingest`へ入り、終了コードが0になる。
20. 新SHA-256が採用中と異なることを確認してから終端事象になった行だけが`pending`へ戻る。非空OCRなど旧payloadは保持されるが`ingest`へは入らず、同じtargetの次回実行では自動再試行しない。
21. backend一時障害とDB書き込み失敗は終了コード非0のままになる。
22. dry-runが取得前filter件数を読み取り専用で報告する。
23. 同一DB状態において、完了時の`remaining_resources_after`が次回dry-runの開始値と一致する。
24. group件数と伝播行件数を別々に報告し、`llm_requests`がerrorで閉じたrunを含めて`llm_run`増分と一致する。
25. `gate_ignored_rows`と`postfetch_ignored_rows`が分けて報告される。
26. `retained_current_rows`が、gate一致または終端事象で採用済みpayloadを保持した行の実数と一致する。
27. 入力tokenの平均、p50、p95、最大が保存済みusageから再計算した値と一致する。
28. OCRなしのsync、ingest、render、snapshot動作が変わらない。
29. `python -m pytest -q`、`PRAGMA quick_check`、`PRAGMA integrity_check`が成功する。

### 実Vaultでの確認

費用・tokenを事前推定しない方針を維持する。実装前に、実Vaultの保存済み説明画像177件から代表6件を選び、仕様の倍率式とLANCZOS filterで1,024・1,536・2,048pxの比較用PNGを18枚作成した。人間が原寸比較し、**1,024pxを既定値として問題ない**と判断済みである。実装後はVault copyで小さい`--limit`を使い、製品経路がこの判断どおりに動くかを確認する。

1. `--dry-run --limit 20`で追加filterの理由別件数と、既存結果の不要な再解析が0であることを確認する。
2. `--limit 20`を実行し、共通縮小後のrequest数、入力・出力token、p50、p95、最大、所要時間を記録する。
3. `max_long_edge_pixels=1,024`で正規化した説明画像を一時fileへ出力し、事前比較と同じ寸法・見た目になることをspot checkする。実装差で可読性が失われた場合だけ停止して再判断し、通常は1,536・2,048で追加の有料requestを行わない。**この確認には、保存済み説明画像のうちOCR文字数上位から2件を必ず含める**（実測の中央値は185字、最大は1,195字）。1,024pxが壊すのは平均的な画像ではなく小さな文字が密に入った図表であり、事前比較の6件が典型例から選ばれていた場合に失敗様態を取りこぼすためである。
4. 縦長の解説画像を1件以上含めて、短辺下限が働いた場合の可読性を確認する。
5. reasoning effortを1段階下げた場合の出力tokenとOCR精度を1水準だけ比較し、数値を記録する。設定項目化の判断は別仕様へ送る。
6. 比較しない全件を`--force`しない。

### 実装順序

1. config v4とfilter設定のparse・検証・render・migrationを実装する。
2. filter判定単位のtargetと、既存targetを外部requestなしで移行する処理を実装する。
3. 新しい既定filter、dry-run内訳、終端画像の規則ベースの扱いを実装する。**gate一致と終端事象は別の状態遷移として実装し、採用済みpayloadを上書きしない。** 同じgate targetと終端targetのdue抑止、gate削除時のローカル復帰を含め、検証14〜20をこの段階で通す。
4. 全backend共通の画像縮小・正規化と監査情報を実装する。Pillow 12.3.0依存、WebP feature preflight、scopedな`DecompressionBombWarning`の例外化、decode寸法の明示的な再確認をこの段階で行う。
5. reportのplanning・実績・残件表示を分離する。
6. 自動テスト、README、`DESIGN.md`を更新する。`DESIGN.md`にはdecoderのformat対応表と、gate調整の運用手順を含める。
7. Vault copyと小さい実Vault batchで製品経路を実測し、決定済みの`max_long_edge_pixels=1024`で可読性とtoken削減を確認する。

### 未決定事項の結論

草案の3点はいずれも決着した。

1. **長辺上限** — 1,024とする。実測で入力tokenの79.8%が画像本体であり、1,024なら25.7〜38.7%、2,048なら5.3〜12.7%の削減になる（いずれも目的1に記した上限値）。さらに実Vaultの代表6件・18枚を人間が原寸比較し、1,024で問題ないと確認したため、この値を確定する。
2. **filter一覧をVaultごとの完全な有効値とするか** — する。既定値の効果が0.97%、Vaultごとの調整余地が20.4%という実測が根拠である。
3. **終端無視への変更** — 変更する。ただし列挙ではなく`transient=False`という規則で定義する。

## 改訂

### 改訂1 — Codex (2026-08-26)

**本項は確定前の最終案編集の記録である。** 確定した決定が後から覆された記録ではない。確定後の改訂は改訂2から始まる。

レビュー5の指摘19〜22と、レビュー6・7に記録した人間判断を最終案へ反映した。変更前後は次のとおりである。

1. **終端取得失敗の状態遷移**
   - (前) 「完了結果を持つ行は、終端取得失敗なら一律に`pending`へ戻し、保存値は残すが`ingest`へ渡さない」
   - (後) 「新SHA-256が不明または同一なら採用中statusとpayloadを維持し、異なることを確認した場合だけ`pending`へ戻す。採用済み結果が無い行だけ`ignored`にする」
   - 理由: 確定済み仕様は、再取得不能では保存済み結果をcurrentのまま残し、SHA-256変更を確認した場合だけ旧OCRを要約から外すと決定している。404は内容変更の証拠ではなく、`pending`へ戻すと保存済みOCRを日常利用から失う。証拠はレビュー5の指摘19、`20260825-image-ocr-source-filenames.ja.md:198,208,210`である。

2. **gate一致とdue抑止**
   - (前) 「gate一致と終端事象を共通経路で`pending`へ戻し、規則削除時に元結果を復活させる」
   - (後) 「gate一致時は、完了payloadを保持した`ignored`、既存`pending`の維持、通常の`ignored`に分ける。同じrule IDはdueにせず、規則削除時は保持完了payloadだけをローカルに`completed`へ戻す」
   - 理由: 現行の`_due`は`pending`を毎回選び、残件が収束しない。gate一致は利用者の除外意思、終端失敗は到達不能であり、同じ状態遷移ではない。証拠はレビュー4の指摘18とレビュー5の指摘20、`feedian/image_ocr.py:434-445`である。

3. **Pillowのdecompression bomb対策**
   - (前) 「`Image.MAX_IMAGE_PIXELS=max_pixels`にすれば上限超過が例外になる」
   - (後) 「`Image.MAX_IMAGE_PIXELS`を設定し、scoped warning filterで`DecompressionBombWarning`を例外化し、`load`前にPillowが認識した寸法も明示的に照合する」
   - 理由: Pillowは`MAX_IMAGE_PIXELS`超過ではwarning、2倍超過で初めて`DecompressionBombError`を出す。設定だけでは40M超から80M以下を処理し得る。証拠はレビュー5の指摘21とPillow公式Image・Security文書である。

4. **WebP必須対応**
   - (前) 「WebPは必須formatだが、Pillowの依存追加だけで対応を保証する」
   - (後) 「Pillow 12.3.0を採用し、必須formatのdecoder不足は画像取得前のpreflightで失敗させる。WebP featureを明示確認し、CIでdecode・縮小を必須testにする。原本fallbackは任意formatのAVIFだけに限定する」
   - 理由: WebPはPillowのoptional build featureであり、package versionだけでは全install環境のlibwebp対応を保証できない。利用者がWebP必須を選択したため、任意対応案は不採用とし、preflightとCIで保証する。証拠はレビュー5の指摘22とレビュー6である。

5. **長辺上限の人間確認**
   - (前) 「実装後に1,024・1,536・2,048pxを比較し、読める最小値を後で決める」
   - (後) 「実Vaultの説明画像177件から代表6件を選んだ18枚を人間が原寸比較し、1,024pxを既定値として確定した。実装後は製品経路が同じ結果になることだけをspot checkする」
   - 理由: 可読性は数値だけで決められないため人間判断を待っていたが、比較が完了した。証拠はレビュー7である。

## 草案

### 背景

本仕様は、[説明画像のOCRとSourceノートの一意なファイル名](20260825-image-ocr-source-filenames.ja.md)を実装し、実Vaultで2回の`enrich-images --limit 100`を実行した後の改善案である。既存仕様の目的である「完全性より日常の使用感を優先し、gateは厳しめでよい」は維持する。

2回の実測は次のとおりだった。`completed`、`ignored`、`failed`は共有先へ伝播した行数であり、LLM request数とは異なる。

| 実行 | resource | 候補行 | 取得URL | 実解析group | LLM request | 入力token | 出力token | 所要時間 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1回目 | 100 | 1,405 | 757 | 417 | 390 | 363,540 | 32,308 | 2分28秒 |
| 2回目 | 100 | 1,428 | 716 | 383 | 374 | 351,584 | 46,909 | 2分39秒 |
| 合計 | 200 | — | — | — | 764 | 715,124 | 79,217 | — |

764回のLLM結果は次のとおりだった。

| `image_kind` | request数 | 割合 |
|---|---:|---:|
| `explanatory` | 177 | 23.2% |
| `photo` | 396 | 51.8% |
| `decorative_illustration` | 79 | 10.3% |
| `icon_or_logo` | 43 | 5.6% |
| `advertisement` | 66 | 8.6% |
| `unknown` | 3 | 0.4% |

入力tokenは1 requestあたり平均936、中央値563、90 percentile 1,717、95 percentile 2,700、最大12,781だった。prompt cacheは764件すべて0だった。保存済みの重複しないOCR結果は177件である。

現行実装には、取得したraster画像をLLMへ渡す前のbackend共通の縮小処理がない。そのため、画像の入力方式が異なるbackendごとに個別調整するより、送信前の画像そのものを共通に正規化した方が、入力token、転送量、遅延の外れ値を一貫して抑えられる。

### 目的

1. 説明画像OCRの実用性を保ちながら、画像解析に対応するすべてのbackendへ送る画像の入力tokenと外部requestを減らす。
2. URL gateをcode変更なしでVaultごとに調整できるようにする。
3. filter変更で無関係な完了画像を再解析しない。
4. planning値、実request数、共有後の行数、処理前後の残件を区別し、実行結果を読みやすくする。
5. 消失画像や未対応headerのように利用者が対処しない終端事象で、正常に完走したcommandを失敗扱いにしない。

### 対象外

- OCR精度の最大化。
- 画像bytesの永続保存。
- resourceごとの候補画像数上限。従来どおり全候補をgateする。
- 写真判定のための新しいローカル画像認識model。
- 任意正規表現によるURL filter。
- 初版での分類とOCRの2 request化。
- 初版でのresource横断・実行横断のSHA-256 cache。
- OCR確認用一時fileを製品commandとして常設すること。

### 改善後の処理フロー

```text
current resource_image
  ↓
設定済みURL prefix・名前token gate
  ├─ 一致: 取得せずignored
  └─ 不一致
       ↓
画像取得、MIME・寸法・資源上限gate
       ↓
SVG: 安全なXML text抽出
raster: backend共通の画像縮小・正規化
       ↓
       選択中backendによる説明画像判定とOCR
  ↓
現在値、監査、実測usage、処理前後の残件を保存・表示
```

分類とOCRは引き続き1画像1 requestで行う。まずbackend共通の画像縮小と取得前gateを改善し、その実測後にだけ2段階化を再検討する。

### 全backend共通の画像縮小・正規化

`image_ocr.max_long_edge_pixels`を追加し、既定値を`2048`とする。SVGを除くraster画像について、既存のMIME、download byte数、pixel数、animated判定を通過した後、backendを呼ぶ前に共通前処理を行う。対象は画像解析に対応するすべてのbackendであり、現在の`openai-responses`、`codex-local`、`claude-code-local`に加えて、将来追加する画像対応backendにも同じ規則を適用する。特定providerだけの画質指定は設けない。

前処理の規則は次のとおりである。

- EXIF orientationを反映した実表示方向を基準に寸法を求める。
- 長辺が`max_long_edge_pixels`を超える場合だけ、縦横比を維持して長辺を上限まで縮小する。拡大はしない。
- 縮小時は文字の輪郭を不必要に崩さない高品質な縮小filterを使い、metadataを除いたPNGへ再encodeする。
- 長辺が上限以下なら再encodeせず、取得した画像をそのままbackendへ渡す。
- animated rasterとSVGの扱いは現行仕様を維持する。SVGはLLMへ送らず、安全なXML text抽出だけを行う。
- decode時にも既存の`max_bytes`と`max_pixels`を越えて資源を消費しない。安全にdecodeできない画像は`unavailable:unsupported_image_decoder`として終端無視にする。
- 正規化後の一時画像はrequest終了時に破棄し、永続保存しない。

decoder libraryやbackendごとのCLI/API表現は実装詳細とし、仕様では固定しない。backend境界には元画像または正規化後画像の実体、media type、送信寸法を渡し、どのbackendでも同じ前処理結果を解析させる。

`max_long_edge_pixels`の変更は新規解析へだけ適用する。model変更と同様、この値だけでは既存の成功結果を自動再解析せず、比較または再取得したい場合は`--force`を使う。実際の元寸法と送信寸法は`llm_run.request_json`へ保存する。

### URL gate設定の外部化

現在code定数である名前tokenとURL接頭辞をVaultの`.feedian/config.json`へ移し、`image_ocr`に次を追加する。

```json
{
  "image_ocr": {
    "max_long_edge_pixels": 2048,
    "ignore_name_tokens": [
      "2x", "3x", "logo", "icon", "avatar", "profile", "button", "btn",
      "banner", "bnr", "badge", "sprite", "spacer", "blank", "emoji", "favicon"
    ],
    "ignore_url_prefixes": [
      "b.hatena.ne.jp/entry/image/",
      "b.hatena.ne.jp/bc/",
      "pbs.twimg.com/amplify_video_thumb/",
      "pbs.twimg.com/ext_tw_video_thumb/",
      "pbs.twimg.com/tweet_video_thumb/",
      "pbs.twimg.com/card_img/",
      "pbs.twimg.com/cards/",
      "pbs.twimg.com/media/",
      "i.ytimg.com/vi/",
      "lh3.googleusercontent.com/a/"
    ]
  }
}
```

設定はcode既定値への追加ではなく、そのVaultで使う完全な有効値とする。利用者は項目を削除して既存gateを解除でき、追加して厳しくできる。新しいFeedian versionが利用者の知らない除外規則を既存Vaultへ自動追加しない。

`ignore_name_tokens`は次の規則で扱う。

- 小文字ASCII英数字だけを許可する。
- URLのhostname、query、fragment、altは対象にせず、URL decodeしたpath segmentとbasenameを`[^a-z0-9]+`で分割した完全tokenだけに一致させる。
- 重複と順序を正規化してから保存・比較する。
- 正規表現、部分一致、globは許可しない。

`ignore_url_prefixes`は`hostname/path-prefix`形式だけを許可する。scheme、query、fragment、userinfoは設定へ書かせない。hostnameは小文字化し、path prefixは先頭`/`を除く。redirect先ではなく、保存済み`source_url`へ取得前に適用する。redirectの安全性は従来どおりNetworkPolicyが担当する。

一致時の`ignored_reason`は`name_token:<token>`または`url_prefix:<設定値>`とし、どの設定で落ちたかをそのまま集計できるようにする。

### 実測から追加する既定filter

2回分のLLM判定に対してURLとaltを集計した。明確な意味を持つ次だけを既定値へ追加する。

| 候補 | 非説明 | 説明 | 採否 |
|---|---:|---:|---|
| 名前token `bnr` | 5 | 0 | 既定値へ追加。`banner`の一般的な略記 |
| `i.ytimg.com/vi/` | 4 | 0 | 既定値へ追加。YouTube動画thumbnail |
| `lh3.googleusercontent.com/a/` | 5 | 0 | 既定値へ追加。Google account avatar |
| 名前token `hero` | 10 | 0 | 既定値へ入れない。説明図がheroになる可能性があり、必要なVaultだけで追加する |
| 名前token `photo` | 31 | 6 | 不採用。実測で説明画像を落とす |
| 名前token `thumb` | 17 | 3 | 不採用。実測で説明画像を落とす |
| `storage` | 32 | 2 | 不採用。保存場所を示すだけで内容を表さない |
| 記事site・画像CDN単位のprefix | sample内では0件の説明画像を含むものが複数 | 未知 | 既定値へ入れない。200 resourceだけでpublisher全体を除外しない |

altが空だったrequestは345件あり、そのうち108件が説明画像だった。空altはgateに使わない。画像position 0にも18件、position 10以降にも54件の説明画像があったため、positionと候補枚数による打ち切りも追加しない。

### filter変更と再評価target

現在の取得前targetはfilter集合全体をhashへ含めるため、規則を1つ追加するだけで、その規則に一致しない完了画像までtarget不一致になる。この形のまま設定を外部化すると、調整のたびに無関係なLLM requestを払い直す危険がある。

改善後のtargetはfilter一覧全体ではなく、各URLに対する**有効なgate判定**を含める。

```text
gate_version
matched_rule = name_token:<token> | url_prefix:<prefix> | pass
```

- 規則追加で新しく一致した行だけが`pass`からrule IDへ変わり、取得せず`ignored`へ更新される。
- 規則削除で該当行だけがrule IDから`pass`へ変わり、通常の取得・解析対象へ戻る。
- 一致しない行はfilter一覧が変わっても`pass`のままで、既存のLLM結果を再利用する。
- gateのアルゴリズム自体を変え、全行を再評価する必要がある場合だけ`gate_version`を上げる。

config v3からの移行直後も、既存の完了・LLM分類無視結果が新gateで`pass`なら外部requestなしで新targetを採用する。新規則に一致する行はローカルで`ignored`へ更新する。旧gateで無視され、新gateで`pass`になった行だけを未処理へ戻す。target形式変更だけで全画像を再取得・再解析してはならない。

### 監査情報

画像tokenとgateを次回実測で評価できるよう、rasterの`llm_run.request_json.logical`へ次を保存する。

- `image_sha256`
- 取得時の`media_type`
- `download_bytes`
- headerから得た元の`width`と`height`
- EXIF orientation反映後の元の`width`と`height`
- backendへ送った`width`、`height`、`media_type`
- 縮小を行ったかどうかと、適用した`max_long_edge_pixels`

画像bytes、base64、API keyは保存しない。これらは既存JSON監査列へ入れるためSQLite schema列は増やさない。開始時requestに保存した`image_sha256`を完了時の論理requestで失わないよう、backend auditのrequest envelopeを統合する。

### reportの改善

planning値と完了値、解析groupと伝播行を別名で表示する。

開始時:

```text
remaining_resources_before
selected_resources
candidate_rows
prefetch_ignored_rows
planned_fetch_urls
planned_analysis_groups
reused_existing_rows
llm_parallelism
historical_seconds_per_request
expected_seconds
```

完了時:

```text
remaining_resources_before
remaining_resources_after
selected_resources
fetched_urls
llm_requests
llm_explanatory_groups
llm_ignored_groups
svg_completed_groups
svg_ignored_groups
gate_ignored_rows
terminal_unavailable_rows
transient_failed_rows
propagated_rows
propagated_resources
input_tokens
output_tokens
input_tokens_per_request_avg
input_tokens_per_request_p50
input_tokens_per_request_p95
input_tokens_per_request_max
```

現在の終了行にある`remaining_resources`は処理開始時の値であり、1回目は6,786のまま表示されたが、次回dry-runでは6,292へ減っていた。終了時には対象を読み直して`remaining_resources_after`を表示する。`analysis_groups`も開始時の上限と終了時の実数で意味が違うため、同じ名前を使わない。

`--dry-run`では取得前に確定するfilter理由別件数を表示する。寸法gate後の件数と実tokenは従来どおり実行前に推定しない。

### 終端画像の扱い

HTTP 404 / 410、非画像MIME、未対応または壊れた画像header、byte・pixel上限超過は、その画像からOCRできなくても利用者が対処する必要がない。取得できなかった事実と理由を保存したうえで終端の`ignored`として扱い、commandの終了コードを非0にしない。

理由は次の形式に揃える。

```text
unavailable:http_404
unavailable:http_410
unavailable:unsupported_image_header
resource_limit:max_bytes
resource_limit:max_pixels
```

同じtargetでは自動再試行しない。設定変更または`--force`で再試行できる。ただしgateは従来どおり`--force`より優先する。

commandを非0にする`failed`は、再試行しても残った一過性network・backend障害、監査runを正常に閉じられない状態、DB書き込み失敗など、commandまたは外部serviceの動作確認を必要とするものに限定する。

実測ではHTTP 404が78行、`unsupported_image_header`が16行あり、後者はすべて`assets.st-note.com`だった。これらのために全体を失敗扱いするより、理由を残して次へ進む方がFeedianの用途に合う。特定site全体のdenylist追加は行わない。

### 設定migration

Vault config formatを3から4へ上げる。migrationは現行code定数を`ignore_name_tokens`と`ignore_url_prefixes`へ展開し、新しい既定規則`bnr`、`i.ytimg.com/vi/`、`lh3.googleusercontent.com/a/`を加え、`max_long_edge_pixels=2048`を設定する。

文字列、配列、正規化後の重複、空文字、正でない`max_long_edge_pixels`、schemeやqueryを含むprefixを明示的に拒否する。未知fieldを拒否する既存方針は維持する。SQLite schema versionは上げない。

### 初版で採らない改善案

#### 分類とOCRの2段階化

非説明画像が76.8%だったため、軽量な分類を先に行い、説明画像だけOCRする案には削減余地がある。一方、説明画像は2 requestになり、状態・retry・監査が二段階になる。全backendで同じ条件の軽量分類を保証できず、小さい文字のある図を最初の分類で落とす可能性もある。まず1 requestのまま共通縮小とURL gateを実測し、その後も入力tokenが日常利用上問題なら別仕様で検討する。

#### resource横断のSHA-256 cache

764 LLM runは755種類のSHA-256だったが、正規化altまで含めると764件すべて異なり、今回のsampleで再利用可能なrunは0件だった。新しいcache tableを追加する根拠がないため採らない。

#### prompt cache最適化

764 requestすべてcached input tokenが0だった。画像、URL、altがrequestごとに異なり、共通promptも短い。prompt構造を複雑にしてcacheを狙わない。

#### modelの小型化

画像入力token数そのものを減らす対策ではない。OCR専用model設定も既存backend契約を増やすため、本仕様には含めない。

### 検証

1. `max_long_edge_pixels`が正の整数だけを受け付け、既定値が2,048になる。
2. 長辺が2,048pxを超えるPNG、JPEG、WebP、GIF、AVIFを縦横比を維持して縮小し、長辺が上限以下の画像を拡大・再encodeしない。
3. EXIF orientation、透過、極端な縦長・横長、壊れた画像、pixel上限超過を安全に処理する。
4. `openai-responses`、`codex-local`、`claude-code-local`へ同じ送信寸法の画像が渡り、provider固有設定の有無で前処理が変わらない。
5. 監査JSONへ元寸法、送信寸法、media type、byte数、SHA-256、縮小有無が残る。
6. 名前tokenはURL pathだけに完全一致し、hostname、query、alt、部分文字列では一致しない。
7. URL prefixは正規化したhostnameとpathにだけ一致し、不正な設定を拒否する。
8. filter追加で新規一致行だけを取得前に無視し、無関係な完了行を再取得・再解析しない。
9. filter削除で該当する旧gate無視行だけが通常対象へ戻る。
10. config v3からv4へのmigrationで既存の完了・LLM無視結果を外部requestなしに維持する。
11. HTTP 404 / 410、未対応header、資源上限が理由付き終端無視になり、それだけなら終了コード0になる。
12. backend一時障害とDB書き込み失敗は終了コード非0のままになる。
13. dry-runが取得前filter件数を読み取り専用で報告する。
14. 完了時の`remaining_resources_after`が次回dry-runの開始値と一致する。
15. group件数と伝播行件数を別々に報告し、`llm_requests`が`llm_run`増分と一致する。
16. 入力tokenの平均、p50、p95、最大が保存済みusageから再計算した値と一致する。
17. OCRなしのsync、ingest、render、snapshot動作が変わらない。
18. `python -m pytest -q`、`PRAGMA quick_check`、`PRAGMA integrity_check`が成功する。

### 実Vaultでの確認

費用・tokenを事前推定しない方針を維持する。実装後はVault copyで小さい`--limit`を使い、次を実測する。

1. `--dry-run --limit 20`で追加filterの理由別件数と、既存結果の不要な再解析が0であることを確認する。
2. 現在選択しているbackendで`--limit 20`を実行し、共通縮小後のrequest数、入力・出力token、p50、p95、最大、所要時間を記録する。
3. OCR確認用一時fileを同じ形式で出力し、説明画像として有用な文字が残っているか人間が確認する。
4. 必要なら同じ20 resourceを`--force`で`max_long_edge_pixels`を十分大きい値にした結果と比較する。比較しない全件を`--force`しない。
5. 2,048pxで使用感を損なわず入力tokenまたは所要時間の実測が改善すれば既定値として採用する。改善しない、またはOCRが読めなくなる場合は実測に基づいて上限値を調整する。

### 実装順序

1. config v4とfilter設定のparse・検証・render・migrationを実装する。
2. filter判定単位のtargetと、既存targetを外部requestなしで移行する処理を実装する。
3. 新しい既定filter、dry-run内訳、終端画像の扱いを実装する。
4. 全backend共通の画像縮小・正規化と監査情報を実装する。
5. reportのplanning・実績・残件表示を分離する。
6. 自動テスト、README、`DESIGN.md`を更新する。
7. Vault copyと小さい実Vault batchで実測し、最終案の`max_long_edge_pixels`を決める。

### 未決定事項

最終化前に人間が決める項目は次の3点である。本草案の推奨を併記する。

1. 全backend共通の`max_long_edge_pixels`を2,048にするか。推奨は2,048。
2. filter一覧をcode既定値への追加ではなく、Vaultごとの完全な有効値として保存するか。推奨は完全な有効値。
3. HTTP 404 / 410、未対応header、資源上限を終了コード0の終端無視へ変えるか。推奨は変更する。

## レビュー

### レビュー1 — Claude Code (2026-08-26)

材料は本草案、`temp/feedian-image-ocr-20260826.md`（explanatory 177件のOCR出力）、および現行実装（`feedian/image_ocr.py`、`feedian/llm_backends.py`、`feedian/vault.py`、`pyproject.toml`）である。草案の事実主張のうち、config format 3、gate定数の内容、`attempt_target`のpayload、`report.remaining_resources`が開始時値であること、`return 1 if report.failed else 0`、`{"logical": ..., "actual": ...}`envelope、開始時requestの`image_sha256`が完了時に上書きされること、共通縮小処理が存在しないことは、いずれも実装と一致していることを確認した。

#### 指摘1 — 入力token 715,124の内訳が不明なまま、pixel上限を対策に選んでいる（重大度: 高）

背景表にbackend名がない。3つのbackendは`input_tokens`の意味が違う。`openai-responses`はAPIのusageだが、`codex-local`と`claude-code-local`はCLIのturn usage（`feedian/llm_backends.py:1428`、`feedian/llm_backends.py:1289`）で、CLI自身のsystem promptやtool定義を含む。後者なら715,124のうち相当部分がCLI側の固定overheadであり、画像を縮小しても減らない。

分布そのものも、画像pixelだけでは説明できない。OpenAIは送信画像を2048×2048へ内接させてから課金tokenを決めるので、client側で長辺2048を課しても課金tokenは原理的にほとんど変わらず、減るのは転送量と遅延である。Anthropicは長辺1568へ縮小してから概ね`(w×h)/750`で、上限は1,600 token程度になる。どちらの式でも1画像で12,781 tokenには届かない。

「prompt cacheは764件すべて0」の説明も疑わしい。OpenAIのprompt cacheは1,024 token以上の共通prefixを要求する。平均936 tokenの本requestはその閾値を下回るので、画像やaltがrequestごとに違うからではなく、そもそもcacheの対象外だったと考えた方が整合する。縮小すればさらに下回るため、cacheが今後も0であること自体は正しい。

最終化の前に`llm_run`から次を確認して本文に書くこと。(a) 実測に使ったbackend。(b) 715,124 tokenのうち画像pixel由来・prompt/alt/URL由来・CLI overhead由来の内訳。(c) 12,781 tokenのrequestの実体。これが確認できないと、`max_long_edge_pixels`の効果は検証も反証もできない。

#### 指摘2 — decoder依存の追加を「実装詳細」に押し込んでいる（重大度: 高）

現行はdecoderを一切持たず、header解析だけで寸法を得ている（`feedian/image_ocr.py:344-431`）。依存も`pyproject.toml`の8件だけである。全backend共通の縮小は、新しいdecoder依存（実質Pillow、AVIFを含めるなら追加plugin）の導入であり、同時にnetwork越しの信頼できないbytesをdecodeする新しい攻撃面の導入でもある。これは「decoder libraryは実装詳細とし、仕様では固定しない」で済ませてよい粒度ではない。少なくとも、追加する依存と、AVIF・WebP・GIFの対応可否を仕様の決定として書くこと。検証2はAVIFの縮小を要求しているので、なおさら必要である。

あわせて、fallbackが機能後退になっている。AVIFとWebPは現行の`SUPPORTED_RASTER_MIMES`に含まれ（`feedian/image_ocr.py:56`）、そのままbackendへ渡って成功している。「安全にdecodeできない画像は`unavailable:unsupported_image_decoder`として終端無視にする」と書くと、長辺2048超のAVIFは、decoderがAVIFに対応していないというこちら側の都合だけで、今日成功しているOCRが終端無視へ変わる。分岐を2つに分けること。codecが無い・未対応なら縮小せず原本をそのまま送る（現行動作の維持）。bytesが壊れているなら`unavailable:unsupported_image_decoder`。

#### 指摘3 — 既定filterの根拠表が旧matcherで集計されている（重大度: 中）

提示された一時出力に`https://i4.ytimg.com/vi/Sw3s28C84FI/hqdefault.jpg`がexplanatoryとして保存されている（`temp/feedian-image-ocr-20260826.md:3162`）。同じ2回の実測から出た結果なので、表の「`i.ytimg.com/vi/` — 非説明4、説明0」は、hostnameを`i.ytimg.com`に完全一致で数えた結果であって、YouTube thumbnailに説明画像が無いことを示していない。

同時に、`ignore_url_prefixes`はhostname完全一致なので、既定値`i.ytimg.com/vi/`は兄弟host `i1`〜`i4.ytimg.com`を捕まえられない。捕まえないままで構わないが、番号付きshardをどう扱うかを明記すること。捕まえるように広げると、上の1件を実際に落とす。

表の各行について、(1) request単位の件数か結果単位の件数か、(2) どのmatcherで集計したかを明記すること。`photo`が表では説明6件、一時出力では3件と食い違うのも、おそらく同じ原因である。

参考として、保存済みexplanatory 177件のURLに既定の`ignore_name_tokens` 16個を当てた結果は、path限定・URL全体のどちらでも一致0件だった。**token既定値は今回のsampleで誤爆しない**。差が出たのは既定値に含めない`storage`だけで、URL全体なら3件、path限定なら2件である。

#### 指摘4 — `max_long_edge_pixels`の期待効果が数量化されていない（重大度: 中）

一時出力の177 URLのうち、URL自身に寸法hintを持つ43件で長辺2048超は1件だけだった（`2047x2700.png`、しかも`width=533`で配信されている）。入力tokenの中央値563も、大半の画像が十分小さいことと整合する。つまりこの機構が効くのは分布の裾だけで、request数は1件も減らない。

「外れ値を一貫して抑えられる」は正しいが、目的1「入力tokenと外部requestを減らす」の主要な手段として置くのは実測に合わない。目的1から切り離し、「遅延と転送量の外れ値に上限を与える」として位置づけ直すこと。そのうえで実装順序4は、1〜3を実測して裾が実際に問題だと確認できた場合にだけ着手する条件付きにすること。指摘1と2で払うcostは、この効果に見合うと確認できてから払う価値がある。

#### 指摘5 — `gate_version`の役割が循環している（重大度: 中）

新targetは各URLの`matched_rule`を持つので、gate規則を変えれば該当行のrule IDだけが変わり、一致しない行は`pass`のまま残る。再評価は常にローカルで全行に対して行われる。よって「gateのアルゴリズム自体を変え、全行を再評価する必要がある場合だけ`gate_version`を上げる」は、`matched_rule`が既に満たしている条件を言い換えているだけで、bumpすべき具体的状況を定義していない。

同じ`matched_rule`値の意味が変わる場合、たとえば`pass`の定義自体が変わって既存の`pass`行を再解析させたい場合に限る、と書くこと。該当する状況が無いなら`gate_version`を落とすこと。用途の定義されないfieldは、後で「念のため上げておく」運用に流れ、全画像の再解析という本仕様が防ごうとしているものを招く。

#### 指摘6 — 終端扱いの列挙が目的5より狭い（重大度: 中）

目的5は「利用者が対処しない終端事象」だが、列挙は404 / 410 / `unsupported_image_header` / `max_bytes` / `max_pixels`の5つに限られている。現行codeにはこれ以外にも非transientなfailedがある。

- `invalid_or_incomplete_header`（`feedian/image_ocr.py:385`）。本文の「壊れた画像header」に当たるはずだが列挙に無い。
- 403 / 401 / 451などの非transientな4xx。`transient = exc.code == 429 or exc.code >= 500`（`feedian/image_ocr.py:421`）なので、これらは`failed`のまま残る。画像のhotlink禁止による403は、この用途で最も起きやすい「対処しようがない」失敗であり、1件出れば終了コードは1のままになる。
- `validate_fetch_url`のValueError経路（`feedian/image_ocr.py:429`）。

列挙ではなく「fetch結果が`transient=False`ならすべて終端」と規則で定義し、そこから外す例外だけを挙げる形にすること。少なくとも403を含めるか、含めない理由を書くこと。

#### 指摘7 — 理由文字列の改名に対する既存行の扱いが無い（重大度: 低）

`name_pattern:` / `denylist:`（`feedian/image_ocr.py:119-132`）から`name_token:` / `url_prefix:`へ変わり、値も`denylist:pbs.twimg.com/media`（末尾スラッシュなし）から`url_prefix:pbs.twimg.com/media/`（あり）へ変わる。新gateでも無視のままの行の`ignored_reason`を書き換えるのか据え置くのかが未定義で、据え置けばreason別集計が新旧混在する。移行時に再評価する行はすべて新形式で上書きする、と明記すること。

#### 指摘8 — `@2x` / `@3x` から `2x` / `3x` への変更は値の移送ではない（重大度: 低）

現行は`lowered`に対する部分一致で`@`込みである（`feedian/image_ocr.py:124-127`）。新案はpath tokenなので`@`が消え、`/2x/`のようなsegmentにも一致する。migrationを「現行code定数の展開」と書くと等価変換に読めるので、意味が変わる点として差分に明記すること。

#### 指摘9 — `max_long_edge_pixels`を`attempt_target`へ入れないことを明記する（重大度: 低）

`max_bytes`、`max_pixels`、`min_short_edge_pixels`は既に`attempt_target`のpayloadに入っている（`feedian/image_ocr.py:143-145`）。本文の「model変更と同様、この値だけでは既存の成功結果を自動再解析しない」はpayloadへ入れないという意味だが、実装者は素直に隣へ足す。「`attempt_target`のpayloadへは入れない」と一行書くこと。

#### 指摘10 — 再encode時にmedia typeだけでは足りない（重大度: 低）

backend境界は`image_path`と`media_type`である（`feedian/llm_backends.py:145`）。`codex-local`は`del media_type`してpathだけをCLIへ渡す（`feedian/llm_backends.py:712`）。PNGへ再encodeするなら一時fileの拡張子も更新しないと、codex経路だけ旧拡張子のまま渡る。「media typeを渡す」に加えて、一時fileの拡張子も正規化後のものにする、と書くこと。

#### 指摘11 — 検証項目の詰め（重大度: 低）

- 検証14「`remaining_resources_after`が次回dry-runの開始値と一致する」は、間にsyncやingestが走らないことが前提になる。「同一DB状態で」と条件を付けること。
- 検証15「`llm_requests`が`llm_run`増分と一致する」は、errorで閉じたrunを数えるかが未定義である。現行は失敗groupでも`start_llm_run`から`finish_llm_run(error=...)`までrunを開く（`feedian/image_ocr.py:693`、`feedian/image_ocr.py:736`）。定義を書くこと。
- `remaining_resources_after`の算出は`resource_image`の全再scanになる。実行末尾に1回増えるcostを許容すると書くこと。

#### 未決定事項への意見

1. **`max_long_edge_pixels`を2,048にするか** — 指摘1と4が解けるまで決められない。実測backendが自前で2048または1568へ内接させているなら、client側の上限は課金tokenではなく転送量と遅延のためのものになり、その目的なら2,048である必然性は薄い。内訳を確認してから決めること。
2. **filter一覧をVaultごとの完全な有効値として保存するか** — 賛成。新versionが利用者の知らない除外規則を既存Vaultへ足さない点が、gate調整を利用者の手に残すという本仕様の狙いと一致している。
3. **404 / 410 / 未対応header / 資源上限を終了コード0の終端無視へ変えるか** — 賛成。ただし指摘6のとおり、列挙ではなく`transient=False`という規則で定義すること。

#### 参考（指摘ではない観測）

- 一時出力177件のOCR本文は172種類だった。重複は`ec.sp-mapple.jp`と`cdn.shopify.com`の同一商品画像がsize違いURLで重複した5 request分だけである。「resource横断のSHA-256 cacheを採らない」判断の根拠は、SHA-256が全件異なるからというより、**重複そのものが177件中5件しかない**ことに置く方が強い。URL query正規化による統合も、この件数なら機構を足す価値がない。
- OCR文字数は中央値185、最大1,195で、`max_ocr_chars_per_image=2000`に触れた結果は0件だった。今回のsampleで切り詰めは起きていない。
- 一方で30文字以下が17件ある（「現在地」「京都市中心図」「一時 / 危篤に」など）。explanatory判定は通るがSourceノートにはほぼ寄与しない。出力側の下限gateは本仕様の範囲外だが、対象外へ「今回は扱わない」と一行残しておくと、次に同じ疑問が出たときに早い。
- 出力tokenが1回目32,308から2回目46,909へ増えている一方、request数は390から374へ減っている。1 requestあたり83から125へ増えた計算になる。資源の内容差なのかmodelの揺れなのかは、次の実測で見ておくとよい。

### レビュー2 — Claude Code (2026-08-26)

実Vault（`D:\GitHub\@`）のDBを読み取り専用のcopyで解析し、レビュー1で「確認が必要」と書いた点を実測した。対象は`llm_run`の`operation='image-ocr'` 764件と`resource_image` 103,034行である。レビュー1の指摘のうち、実測で撤回するもの、確定したもの、新たに出たものを分けて記す。

#### 実測条件（指摘1への回答）

backendは`openai-responses`、modelは`gpt-5.6-terra`、764件すべて`completed`、実行は2026-08-25T15:56–16:23 UTCだった。CLI backendではないので、CLI側の固定overheadという懸念は当たらない。草案の背景表の数値（764件、715,124、79,217、平均936、中央値563、p90 1,717、p95 2,700、最大12,781、cached 0）はすべて`llm_run`と一致した。

入力tokenの内訳は次のとおりである。prompt文は`image_ocr_prompt`をo200k_baseで再tokenizeして求めた。

| 区分 | token | 比率 | 1 requestあたり |
|---|---:|---:|---|
| 画像 | 570,677 | 79.8% | 平均747、p50 367、p90 1,482、p95 2,519、最大12,570 |
| prompt文（指示＋URL＋alt） | 144,447 | 20.2% | 平均189、p50 184、最大443 |
| 合計 | 715,124 | 100% | 平均936 |

最大の12,570 tokenは`img.cf.47news.jp/public/photo/a158825a…/photo.jpg`（`image/jpeg`、`photo`判定）だった。12,570×1,024 ≈ 12.9 Mpixelで、32×32 patch換算の`pixel/1024`とよく一致する。つまりこのmodelの画像tokenはpixel数にほぼ比例し、頭打ちがない。

#### 指摘1を撤回する

レビュー1で「OpenAIは送信画像を2048×2048へ内接させてから課金tokenを決めるので、client側の上限では課金tokenはほとんど変わらない」と書いたが、**`gpt-5.6-terra`には当てはまらない。誤りなので撤回する。** 画像tokenはpixelに比例し、1画像で12,570 tokenまで実際に出ている。したがって長辺上限は課金tokenを実際に減らす。CLI overheadを疑った部分も、backendが`openai-responses`だったので当たらない。

一方、prompt cacheが全件0である理由については、レビュー1の見立てのままでよい。平均936 tokenはOpenAIのcache最小prefix長1,024を下回るので、そもそも対象外である。草案の「画像、URL、altがrequestごとに異なるから」という説明は、事実ではあっても働いている理由ではない。縮小すればさらに下回るので、今後も0で正しい。

#### 指摘4を確定する — 長辺上限の効果は測れた

画像token ≈ `pixel/1024`から、長辺Lをaspect比rで`L=√(pixel·r)`と置いて、上限値ごとの削減を求めた。

| 長辺上限 | r=1.0（正方） | r=1.33（4:3） | r=1.78（16:9） |
|---:|---|---|---|
| 1,024 | 154 req、全体 −25.7% | 201 req、−32.3% | 264 req、−38.7% |
| 1,536 | 45 req、−12.7% | 55 req、−16.7% | 98 req、−21.2% |
| **2,048** | **24 req、−5.3%** | **30 req、−8.9%** | **45 req、−12.7%** |
| 2,560 | 4 req、−1.5% | 18 req、−3.3% | 25 req、−7.0% |

2,048は764 requestのうち24〜45件（3〜6%）にしか掛からず、入力token全体では5〜13%の削減である。request数は1件も減らない。効くのは裾だけ、というレビュー1の見立ては数値として確定した。

同時に、**2,048という値の選択根拠が弱いことも見えた。1,536にすると削減はおよそ2倍（13〜21%）、1,024なら3〜5倍（26〜39%）になる。** ここから先はtoken量の問題ではなく、縮小後にOCRが読めるかという問題である。未決定事項1を「2,048にするか」で決めるのではなく、実Vaultでの確認の3で1,024・1,536・2,048の3水準のOCR出力を人間が読み比べ、読める最小値を採るべきである。草案の確認手順4は「2,048 対 十分大きい値」の2水準しか比べないので、この形に直すこと。

#### 指摘3を確定する — ytimgは仕様の書き方どおりでは取り逃がす

764件のうちytimg系は10件あり、内訳は次のとおりだった。

| host | req | 判定 |
|---|---:|---|
| `i.ytimg.com` | 4 | 非説明4 |
| `i1`〜`i4.ytimg.com` | 6 | 非説明5、**説明1**（`i4.ytimg.com/vi/Sw3s28C84FI/hqdefault.jpg`） |

草案の表の「`i.ytimg.com/vi/` — 非説明4、説明0」はhostname完全一致では正しい。しかし**同じ規則の対象になるはずのshardが6件あり、そこに説明画像が1件含まれている。** 番号付きshardを捕まえないと決めるなら明記すること。捕まえるように広げると、この1件を落とす。いずれにせよ10件で4,662 tokenなので、全体の0.65%であり、規則の是非より表の作り方の問題である。

#### 指摘6を一部弱める — 403は今回発生していない

`resource_image`の`failed`は94行で、`http_404`が78行、`unsupported_image_header`が16行。草案の記述と完全に一致し、それ以外の失敗理由は1件も無かった。**レビュー1で「403が最も起きやすい」と書いたが、200 resourceの範囲では0件だったので根拠が無い。** ただし94行はすべて恒久的な`failed`として残り、以後の`enrich-images`は毎回終了コード1を返す。終端扱いへ変える必要性そのものは確定である。列挙ではなく`transient=False`という規則で定義せよ、という提案も維持する。列挙形式だと、次に別の非transient理由が出たときにまた仕様改訂が要る。

#### 指摘8を実測で棄却する — 移行時のflipは12行

旧gateで無視されている5,173行に新gate（path token＋hostname完全一致prefix）を当てたところ、`pass`に戻るのは**12行・URLで11件**だけだった。`@2x`の2,606行、`icon`の1,294行、`logo`の797行はすべて新gateでも一致する。数字接尾辞の取り逃がし（`logo2`、`icon01`、`banner300`など）もVault全体63,148 URL中76件、0.12%である。**migrationのcostは無視してよい。**

ただし、戻る11件の中身は設計上の示唆になる。10件が`profile-image.kraken.asahi.com/<hash>`で、意味がすべてhostnameにあり、pathは裸のhashである。残り1件は`t0.gstatic.com/faviconV2?…`で、segmentが`faviconV2`のため token は`faviconv2`になり`favicon`と一致しない。hostnameを対象にしないという判断は精度のために正しいが、その穴は`ignore_url_prefixes`（hostnameを見る側）で塞ぐ設計になっている。**migrationでこの2件のprefixを併せて入れれば、flipは0にできる。** 仕様の移行節にそう書くこと。

#### 指摘12 — reasoning tokenが出力の36.3%を占めているが、草案に記述が無い（重大度: 中）

`openai-responses`の画像requestは`"reasoning": {"effort": "low"}`を固定で送っている（`feedian/llm_backends.py:344`）。実測の出力token 79,217のうち**28,736（36.3%）がreasoning token**であり、`explanatory`に限れば41.1%に達する。

| `image_kind` | 出力token | うちreasoning | 比率 |
|---|---:|---:|---:|
| `explanatory` | 60,593 | 24,932 | 41.1% |
| `photo` | 11,970 | 2,306 | 19.3% |
| `decorative_illustration` | 3,192 | 918 | 28.8% |
| `advertisement` | 2,228 | 540 | 24.2% |
| `icon_or_logo` | 1,162 | 40 | 3.4% |
| 合計 | 79,217 | 28,736 | 36.3% |

出力tokenは入力tokenより単価が高い。仮に8倍なら、28,736 tokenは入力換算で約230,000 token相当、全入力715,124の32%に当たる。**つまりreasoning effortの見直しは、長辺上限2,048の削減（38,000〜91,000 token）より大きい費用要因になりうる。** 本仕様は費用の事前推定をしない方針だが、それは「推定しない」であって「測った差を無視する」ではない。実Vaultでの確認へ、`effort`を下げたときのOCR精度と出力tokenを1水準だけ比べる項目を足すこと。下げられないなら、その判断を根拠付きで対象外へ書くこと。

#### 指摘13 — 新しい既定filterは0.97%、per-Vault設定は20.4%（重大度: 中）

草案が既定値へ追加する3規則を764件へ当てた結果は次のとおりである。

| 追加規則 | 一致 | 非説明 | 説明 | 節約token | 全体比 |
|---|---:|---:|---:|---:|---:|
| `bnr` | 5 | 5 | 0 | 3,299 | 0.46% |
| `i.ytimg.com/vi/` | 4 | 4 | 0 | 1,935 | 0.27% |
| `lh3.googleusercontent.com/a/` | 5 | 5 | 0 | 1,728 | 0.24% |
| 合計 | 14 | 14 | 0 | 6,962 | **0.97%** |

誤爆は0件で、追加そのものに反対はしない。しかし効果は1%未満である。一方、host別に入力tokenを集計すると、**説明画像を1件も出さなかった5 hostだけで146,098 token、全体の20.4%を消費していた。**

| host | req | 入力token | 全体比 | 説明画像 |
|---|---:|---:|---:|---:|
| `spotlight.fantia.jp` | 10 | 47,832 | 6.7% | 0 |
| `media.vogue.co.jp` | 8 | 32,130 | 4.5% | 0 |
| `dailyportalz.jp` | 8 | 25,493 | 3.6% | 0 |
| `nazology.kusuguru.co.jp` | 22 | 21,891 | 3.1% | 0 |
| `media.loom-app.com` | 29 | 18,752 | 2.6% | 0 |
| 5 host合計 | 77 | 146,098 | **20.4%** | 0 |

「200 resourceだけでpublisher全体を既定値から除外しない」という草案の判断は正しい。だがこの表は、**本仕様の実効的な削減は既定値ではなく目的2（Vaultごとの調整）から来る**ことを示している。未決定事項2で「完全な有効値」を推すのは、この20.4%を利用者が自分で刈り取れるようにするためだ、という根拠を最終案へ書くこと。あわせて、READMEか`DESIGN.md`に「reason別集計を見てhostを追加する」運用手順を1段落置くこと。集計は出るが使い方が書かれていない機能は使われない。

なお、この5 hostは1 requestあたり平均1,897 tokenと大きく、長辺上限の対象とも重なる。gateで落とせばそもそも縮小の出番が減る。実装順序で1〜3を先に置いた草案の判断は、この点でも正しい。

#### 指摘14 — `photo`と`storage`の不採用は、費用を出したうえで再検討する価値がある（重大度: 低）

764件に対する再集計は次のとおりである。

| 候補 | 一致 | 非説明 | 説明 | 節約token | 失う説明のtoken |
|---|---:|---:|---:|---:|---:|
| token `photo` | 32 | 29 | 3 | 59,189（8.3%） | 2,936 |
| token `storage` | 34 | 32 | 2 | 32,004（4.5%） | 3,309 |
| 合計 | 66 | 61 | 5 | 91,193（**12.8%**） | 6,245 |

草案はどちらも「実測で説明画像を落とす」として不採用にした。落とすのは事実だが、**2つ合わせて入力tokenの12.8%であり、長辺上限2,048の削減幅と同等かそれ以上を、新しい依存もdecoderも無しに得られる。** 代償は177件の説明画像のうち5件である。「完全性より日常の使用感を優先する」「利用者に何も損させない欠落はbugではない」という`AGENTS.md`の原則に照らすと、これは検討に値する取引に見える。

ただし`storage`の34件中32件は`japan.cnet.com`由来で、実質1 siteのfilterである。汎用tokenとしての正当性は薄く、「保存場所を示すだけで内容を表さない」という草案の理由付けの方が筋が良い。**`storage`は不採用のままでよい。`photo`だけ、5件中3件を失って8.3%を取るかどうかを人間が決める形にすること。** 判断がどちらでも、この数値を根拠として残しておけば、次に同じ議論が起きない。

#### その他の実測値（指摘ではない）

- `image_kind`別の入力token消費は`photo` 53.7%、`explanatory` 22.6%、`decorative_illustration` 13.4%、`advertisement` 6.8%、`icon_or_logo` 3.3%、`unknown` 0.2%。**非説明画像がrequestの76.8%、入力tokenの77.4%を占める。** 2段階化を初版で採らない判断は妥当だが、削減余地の大きさは長辺上限の5〜13%とは桁が違う。次の実測後に再検討する、という草案の書き方のままでよい。
- `duration_ms`は合計2,119秒、平均2.77秒、p50 1.80秒、p95 8.39秒、最大39.95秒。所要時間の裾は入力tokenの裾とよく対応しており、長辺上限を「遅延の外れ値に上限を与える」と位置づけ直すレビュー1の提案は、この分布からも支持される。
- `resource_image`の現況は`pending` 88,912、`ignored` 13,437、`completed` 591、`failed` 94。無視理由の最大は`small_dimensions`の6,017行で、これは取得後の寸法gateなので通信費は既に払っている。取得前には判定できないため対策は無いが、gate別の内訳を出すときにこの行が「取得済み」であることが読み取れる表示にすること。取得前gate（`name_pattern:*`合計5,173行）と混ぜると、節約できた通信量を過大に読む。

### レビュー3 — Codex (2026-08-26)

最終案を、確定済みの[説明画像のOCRとSourceノートの一意なファイル名](20260825-image-ocr-source-filenames.ja.md)および現行実装と照合した。レビュー1・2の指摘の多くは反映されているが、次の3点は実装前に最終案の修正が必要である。

#### 指摘15 — 短辺下限が「拡大しない」規則およびpixel上限と両立しない（重大度: 高）

最終案は、長辺が上限を超えた画像を縮小する一方で拡大はせず（24行目）、縮小後の短辺が既定512pxを下回る場合は短辺が512pxになる倍率で止める（25行目）としている。検証3も、縦横比が2:1を超える画像では短辺が512pxを下回らないことを無条件に要求している。しかし既存gateは短辺200px以上を許可するため、たとえば200×200,000pxの画像はちょうど`max_pixels=40,000,000`を通過する。この画像の短辺を512pxにすると2.56倍の拡大になり、512×512,000px、約262M pixelとなって「拡大しない」と`max_pixels`の双方に反する。拡大しなければ、今度は短辺512px以上という検証を満たさない。

倍率を、たとえば`min(1, max(max_long_edge_pixels / long_edge, (max_long_edge_pixels / 2) / short_edge))`のように上限1で定義し、短辺下限は「原本の短辺が下限以上の場合だけ保証する」と限定する必要がある。原本の短辺が下限未満の極端な縦長・横長画像について、原本をそのまま渡すか、別のgateで無視するかも決めること。検証3・4には短辺200pxかつ総pixel数が上限付近の例を加え、正規化後のpixel数が原本を超えないことを確認するべきである。

採否: 採用。現行文のままでは実装がどちらの規則を破るべきか決められず、悪い選択ではdecoderが既存の資源上限を超えるallocationを行うためである。

#### 指摘16 — 終端失敗の`ignored`化が既存の完了OCRを失わせ得る（重大度: 高）

確定済み仕様は「新しい解析が失敗しても、同じ画像に対する既存の完了結果を先に消さない」と定めている（`20260825-image-ocr-source-filenames.ja.md:208`）。一方、今回の最終案は`transient=False`の取得失敗をすべて終端の`ignored`にする（126行目）。現行の`_attempt_values`は`failed`なら試行列だけを更新するが、`ignored`なら`analysis_status`、`ocr_text`、`image_kind`、`image_sha256`などの現在値列をまとめて上書きする（`feedian/image_ocr.py:534-549`）。したがって単純に非一過性失敗のstatusを`ignored`へ変える実装では、`--force`などで再取得した既存`completed`行が404になったとき、保存済みOCRを空の無視結果で置き換え、`ingest`からも外してしまう。

終端事象で終了コードを0にすることと、現在値を`ignored`へ置き換えることを分離する必要がある。既存の完了現在値がある行ではそれを全列保持し、`last_attempt_*`と取得不能理由だけを更新して成功扱いの終了コードにする。現在値が無い行だけを終端`ignored`にする、という状態遷移を本文と検証へ追加すること。少なくとも「完了OCRを持つ行を`--force`し、取得が404 / 410またはdecode不能になってもOCR現在値と要約入力が維持される」testが必要である。

採否: 採用。これは費用や完全性の取引ではなく、すでに保存した正しいデータを失う経路であり、プロジェクトのデータ完全性規則と確定済み仕様の双方に反するためである。

#### 指摘17 — decoder依存と必須対応formatが依然として未決定である（重大度: 高）

レビュー1の指摘2は、追加するdecoder依存とAVIF・WebP・GIFの対応可否を仕様の決定として書くよう求めていた。最終案35行目も「依存するlibraryとその対応formatは実装詳細ではなく本仕様の決定事項」と認めたが、実際のlibrary名、version方針、必須codecを決めず、依存追加時に`DESIGN.md`へ書くとして実装段階へ先送りしている。その一方で検証2はPNG、JPEG、WebP、GIF、AVIFをすべて縮小することを要求し、39行目はcodec未対応なら原本を渡すことを許している。AVIF decoderを持たない実装が検証2に不合格なのか、39行目の許容経路なのかを判定できない。

最終案で採用libraryと必須対応formatを決めること。すべてを必須にしないなら、検証2を「仕様で必須としたformatは縮小し、任意codecの未導入formatは原本へfallbackする」に直し、必須集合と任意集合を明示する必要がある。decompression bomb検知が警告で続行するlibraryでは、`max_pixels`超過を必ず処理中止にする設定も受け入れ基準へ含めること。

採否: 採用。依存と対応formatを仕様決定と呼びながら値を決めておらず、相反する受け入れ結果がどちらも本文に適合してしまうためである。

#### 結論

指摘15〜17はいずれも採用とし、現時点では確定不可と判断する。短辺下限の倍率と極端なaspect比の扱い、既存完了OCRを保持する終端状態遷移、decoder libraryと必須formatを最終案へ反映した後に再レビューする。

### レビュー4 — Claude Code (2026-08-26)

レビュー3を現行実装と確定済み仕様に当てて検証した。**指摘15〜17はいずれも再現し、引用された行番号（24・25・35・39・126）もすべて正確だった。結論の「確定不可」に同意する。** そのうえで、指摘16と同じ欠陥が別経路にもあり、レビュー3が触れていないので指摘18として加える。

#### 指摘15の検証 — 採用

再現した。`min_short_edge_pixels`は200、`max_pixels`は40,000,000（`feedian/vault.py:56-57`）で、判定は`width * height > settings.max_pixels`（`feedian/image_ocr.py:390`）である。200×200,000pxはちょうど上限と等しく超過しないため両gateを通る。最終案25行目の下限をこれに当てると短辺倍率が2.56倍になり、24行目の「拡大はしない」と`max_pixels`を同時に破る。指摘のとおりで、私の書き落としである。

提示された倍率式`k = min(1, max(C/L, (C/2)/S))`は正しい。検算すると、4,000×3,000は`k=0.256`で1,024×768（長辺規則どおり）、800×6,000は`k=0.64`で512×3,840（最終案の例と一致）、200×200,000は`max`が2.56となりclampして`k=1`で原本のままになる。

**残された決定（原本の短辺が下限未満の極端な画像の扱い）については、縮小せず原本のまま渡す方を採ることを提案する。** 専用gateも新しい設定値も設けない。理由は、この経路の資源消費が既存の`max_bytes`と`max_pixels`で今日すでに縛られており、本仕様で悪化しないためである。実測764件の画像token最大は12,570で、この経路が問題になった実例は無い。`AGENTS.md`の「最後の数%を閉じるための機構を作らない」に照らして、可能性のためだけに状態を増やさない。気になるVaultは`max_pixels`を下げれば対処できる。

検証3は「縦横比が2:1を超える画像で短辺下限が働く」を無条件に要求しているので、**「原本の短辺が下限以上の場合に限る」と条件を付ける**。あわせて指摘のとおり、短辺200pxかつ総pixel数が上限付近の例を検証へ加え、**正規化後のpixel数が原本を超えないこと**を不変条件として明記する。

#### 指摘16の検証 — 採用（3件中もっとも重い）

再現した。コード上の経路まで確認できた。

`_attempt_values`は`result.status == "failed"`なら`common`だけを返して現在値列に触れない（`feedian/image_ocr.py:534`）。`ignored`はその分岐を抜けるため、`analysis_status`、`ocr_text`、`image_kind`、`image_sha256`、`ignored_reason`、`analyzed_at`をまとめて上書きする（同535-549）。`ImageAnalysisResult.ocr_text`の既定値は`""`（同77）なので、上書きされる値は空文字である。

これは確定済み仕様の「新しい解析が失敗しても、同じ画像に対する既存の完了結果を先に消さない」（`20260825-image-ocr-source-filenames.ja.md:208`）に正面から反する。`AGENTS.md`の「すでに保存したbodyを落とすことは常にbug」「日常の使用感を優先する原則は、確定した仕様やdata-integrity規則を上書きしない」にも当たる。

**到達可能性も確認した。理論上の経路ではない。** `_due`は`last_attempt_target`の不一致だけで真になる（`feedian/image_ocr.py:433`）。`--force`のほか、`max_bytes`・`max_pixels`・`min_short_edge_pixels`・`prompt_version`・backendの変更でもtargetは変わる。実測Vaultには`completed`が591行あり、`http_404`が既に78行出ている。link rotは時間とともに増えるので、完了行が後から404になる状況は例外ではなく前提である。

提案された分離（終了コードを0にすることと現在値を置き換えることを分ける）を採る。**ただし残す状態は`ignored`ではなく`pending`とすべきである。** 確定済み仕様は、同じ「渡さないが消さない」を表すために`pending`＋現在値保持を選んでおり、その理由として`ingest`の読み出し条件が`analysis_status='completed'`を含むため`pending`にすれば要約入力から自動的に外れること、値域を増やさずCHECK制約のmigrationも要らないことを挙げている（同210-214）。終端失敗も同じ形に揃えるのが一貫する。指摘が求めるtestに加えて、**「`pending`へ戻った行が非空の`ocr_text`を保持し、かつ`ingest`の入力には現れない」**を検証へ加える。

#### 指摘17の検証 — 採用

そのとおりである。最終案35行目は「依存するlibraryとその対応formatは実装詳細ではなく本仕様の決定事項」と書きながら、同じ文の後半で「追加する時点で`DESIGN.md`へ記載する」と実装段階へ送っている。決定事項だと宣言して決定していないので、レビュー1の指摘2は解決していない。私の書き方の不備である。

検証2（PNG・JPEG・WebP・GIF・AVIFをすべて縮小する）と39行目（codec未対応なら原本へfallbackしてよい）が矛盾し、AVIF decoderを持たない実装の合否が本文から決まらない、という読みも正しい。必須集合と任意集合を分けて書き、検証2を必須集合に対する要求へ限定する。

decompression bomb検知を警告で続行させず必ず処理中止にする、という受け入れ基準の追加も採用する。

#### 指摘18 — gate一致による`ignored`化も同じ経路で完了OCRを破壊する（重大度: 高）

指摘16と同じ欠陥が、取得失敗ではなく**filter変更**の経路にもある。レビュー3はこちらを挙げていない。

最終案は移行と規則追加について「新規則に一致する行はローカルで`ignored`へ更新する」（463行目）としている。この更新も`_attempt_values`を通るため、`completed`で実際のOCRを持つ行が新しい規則に一致した瞬間、`ocr_text`が空文字で上書きされる。実装側も取得前gateの分岐が`ImageAnalysisResult(status="ignored", ignored_reason=reason)`をそのまま`apply_image_analysis`へ渡している（`feedian/image_ocr.py:670`、`feedian/image_ocr.py:674`）。

この経路は現行実装にも既に存在する。filter定数を1つ変えると全行のtargetが変わって`due`になり、新規則に一致した完了行が空の無視結果へ置き換わる。**しかし現行のfilterはcode定数で、変更はreleaseに伴う稀な事象だった。本仕様は目的2でfilter調整を日常操作にする。** 稀な事故を常設の操作へ格上げすることになるので、本仕様で塞ぐ必要がある。

実測で具体化できる。Vault所有者が「Vaultごとに追加を推奨する設定」の`photo`を`ignore_name_tokens`へ足すと説明画像3件が一致し、現状の実装ではその3件の保存済みOCRが消える。「gateに一致させたのだからもう要らない」という意思表示ではあるが、確定済み仕様は同じ理屈が成り立つSHA-256変更時ですら現在値を残す方を選んでいる。**gate一致行も`pending`＋現在値保持とし、`ignored_reason`だけを更新する**のが一貫する。設定を戻せば元の結果がそのまま復活するという可逆性も得られ、これは「足して試し、合わなければ戻す」ことを前提にした本仕様の趣旨に合う。

なお、現在値をまったく持たない行（初回から`pending`の行）は従来どおり素直に`ignored`でよい。区別すべきなのは「一度`completed`になった行」だけである。

#### 結論

レビュー3の結論「現時点では確定不可」に同意する。指摘15・16・17に指摘18を加えた4点を最終案へ反映してから再レビューする。

最終案を修正する際は、レビュー3が24・25・35・39・126行目を、本レビューが463行目を参照している点に注意する。行がずれるため、修正後に該当箇所の新旧を本レビューへ追記して参照を追えるようにする。


#### 最終案への反映記録（2026-08-26）

指摘15〜18を最終案へ反映した。レビュー3が最終案の行番号を参照しているため、参照先の新旧を残す。**行番号は反映後にずれているので、以降は下表の新行を見ること。**

まず、上の指摘18で最終案の該当箇所を「463行目」と書いたのは誤りである。463行目は`## 草案`側の同文で、最終案の該当箇所は反映前の120行目だった。指摘の内容は変わらない。

| 指摘 | 反映前 | 反映後 | 変更の要点 |
|---|---|---|---|
| 15 | 24-25行目 | 24-27行目 | 「長辺を上限まで縮小する。拡大はしない」＋「短辺が下限を下回る場合は短辺がその値になる倍率で止める」の2条を、倍率式`k = min(1, max(C / 長辺, (C / 2) / 短辺))`の1条へ置き換えた。短辺下限は原本の短辺が下限以上の場合だけ保証されると限定し、原本の短辺が下限未満の画像は`k=1`で原本のまま渡すと明記した。「正規化後のpixel数は原本のpixel数を超えない」を不変条件として追加した。 |
| 17 | 35行目 | 37-44行目 | 「依存するlibraryと対応formatは決定事項とし、追加時に`DESIGN.md`へ記載する」を、採用library＝Pillow、必須format＝PNG / JPEG / GIF / WebP、任意format＝AVIF、version方針＝`pyproject.toml`は下限のみ・`requirements.txt`はpin、へ置き換えた。decompression bomb検知を警告で続行させず`Image.MAX_IMAGE_PIXELS`を`max_pixels`に一致させて例外にする、を追加した。 |
| 16 | 126行目 | 139-147行目 | 「`transient=False`の失敗はすべて終端の`ignored`とする」を「終端事象として扱う」に改め、終了コードと現在値置き換えの分離を状態遷移表として追加した。完了現在値を持つ行は`pending`へ戻して`ocr_text`などを保持し、現在値を持たない行だけ`ignored`にする。確定済み仕様がこの形を選んでいる理由と、分離しない場合に失われるものを本文へ書いた。 |
| 18 | 120行目 | 129-133行目 | 「新規則に一致する行はローカルで`ignored`へ更新する」を、終端事象と同じ状態遷移を使う形へ置き換えた。gate一致は「もう解析しない」であって「保存済みの結果を捨てる」ではない、可逆性が足して試す運用に必要である、という理由と、`photo`追加で説明画像3件が該当するという実測を添えた。 |

これに伴う付随変更は次のとおりである。

- 検証を22項目から28項目へ増やした。検証2をformat必須・任意で分け、検証3に「原本の短辺が512px以上で」の条件を付け、検証4（原本の短辺が下限未満なら縮小しない）、検証5（正規化後のpixel数が原本を超えない）、検証8（`max_pixels`超過が処理中止になる）を追加した。指摘16・18に対する検証18・19・20を追加した。
- 完了時reportへ`retained_pending_rows`を追加した。「無視した」と「結果を捨てた」を別の出来事として数えるためである。
- 実装順序3へ、`pending`へ戻す状態遷移を終端事象とgate一致で共通の1経路として実装すること、`_attempt_values`の`ignored`分岐が完了現在値を上書きしないようにすることを明記した。実装順序4のdecoder追加をPillowに具体化した。

未着手の項目は無い。レビュー3の結論に従い、この状態で再レビューを求める。

### レビュー5 — Codex (2026-08-26)

レビュー4と反映後の最終案を、確定済み仕様、現行の選択条件、Pillow公式文書へ再度照合した。指摘15の倍率式と検証追加は解決している。指摘18も、gate一致時に保存値を物理削除しないという問題提起は妥当である。一方、指摘16への修正は「再取得できない」と「画像bytesが変わったことを確認した」を同じ`pending`へまとめたため、確定済み仕様の状態遷移と一致しない。指摘17もlibrary名は決まったが、資源上限とWebP対応に未解決点が残る。

#### 指摘19 — 終端取得失敗で`completed`を`pending`へ戻してはならない（重大度: 高）

レビュー4は、終端取得失敗とgate一致を「渡さないが消さない」という同じ状況として`pending`へ揃えた。しかし両者は異なる。gate一致は利用者が今後そのOCRを使わないと決めた状態だが、404、410、decode不能、資源上限は新しい画像内容を一度も取得できておらず、保存済みOCRが古くなった証拠ではない。

確定済み仕様はこの区別を既に決めている。再取得不能なら「保存済みの切り詰め結果をcurrentとして残し、失敗した試行だけを記録する」（`20260825-image-ocr-source-filenames.ja.md:198`）、新しい解析が失敗しても既存の完了結果を先に消さない（同208行目）とする一方、`pending`へ戻すのは**画像bytesのSHA-256が変わったことまで確認できた場合だけ**である（同210行目、検証15）。現行の`completed_image_ocr`も`analysis_status='completed'`だけを要約へ渡す（`feedian/store.py:1317-1319`）。したがって最終案145・148行目と検証18・19は値を物理的には残すが、link rotが起きた瞬間から正しい保存済みOCRを日常の要約から失わせる。これはレビュー3の指摘16が求めた「要約入力を維持する」にも反する。

終端取得失敗では、既存の`completed`行について`analysis_status`を含む現在値をすべて維持し、`last_attempt_*`と取得不能理由だけを更新すること。`pending`へ戻すのはSHA-256変更を確認した場合と、利用者が意図して除外したgate一致の場合に限定する。検証18は`analysis_status='completed'`と要約入力の維持を要求する形へ直し、検証19は終端取得失敗から削除する必要がある。reportも、終端失敗で保持した行を`retained_pending_rows`へ数えてはならない。

採否: レビュー4の指摘16対応を不採用とする。保存値を物理削除しない点は採用するが、取得不能だけでcurrent判定と要約利用を取り消す理由がなく、確定済み仕様が明示的に選んだ状態遷移を逆転させるためである。

#### 指摘20 — `pending`へ戻した終端・gate行は現行のdue判定で毎回再処理される（重大度: 高）

最終案163行目は同じtargetで自動再試行しないと定めるが、現行の`_due`は同じtargetかつ`last_attempt_status != 'failed'`でも、`analysis_status`が`completed`または`ignored`でなければ`True`を返す（`feedian/image_ocr.py:434-445`）。レビュー4の状態遷移で`pending`へ戻した行は、`last_attempt_status='ignored'`を保存しても次回実行で再びdueになる。

終端取得失敗では指摘19のとおり`completed`を維持すれば現行判定でも抑止できる。しかしgate一致の完了行は`pending`にするため、同じtargetの次回実行を明示的に抑止する条件が別途必要である。この定義が無いと、取得前gateなので外部requestは避けられても、対象resourceと`retained_pending_rows`が毎回残り、`remaining_resources_after`が0へ収束しない。規則削除でtargetが`pass`へ変わった場合だけローカルに`completed`へ復帰し、同じrule IDの間はdueにしない状態遷移を本文へ追加すること。検証20には、規則を維持した2回目の実行で対象resource、DB update、外部requestがいずれも0になる確認も必要である。

採否: 採用。現在の最終案は保存形式だけを定め、再選択の抑止条件を定めておらず、目的3「filter変更で無関係な完了画像を再解析しない」と残件reportの収束を満たせないためである。

#### 指摘21 — `Image.MAX_IMAGE_PIXELS=max_pixels`だけでは上限超過を例外にできない（重大度: 高）

最終案44行目は、Pillowの`Image.MAX_IMAGE_PIXELS`を`max_pixels`に一致させれば超過が例外になるとしている。しかしPillow公式文書では、pixel数が`MAX_IMAGE_PIXELS`を超えた時点では`DecompressionBombWarning`であり、`DecompressionBombError`になるのはその2倍を超えた場合である。警告を例外へ変えるには`warnings.simplefilter('error', Image.DecompressionBombWarning)`が別途必要だと明記されている（[Pillow Image module](https://pillow.readthedocs.io/en/stable/reference/Image.html)）。公式のsecurity guidanceも、`MAX_IMAGE_PIXELS`を有効にしたうえで`DecompressionBombWarning`をerrorとして扱うよう求めている（[Pillow Security](https://pillow.readthedocs.io/en/stable/handbook/security.html)）。

したがって、`MAX_IMAGE_PIXELS=max_pixels`だけを受け入れ基準にすると、40M超から80M以下は警告のまま処理が続き得る。警告をdecode処理のscoped filterで例外化し、header gate後もPillowが認識した寸法を`max_pixels`と照合してからpixel dataをloadする、と仕様へ明記すること。全processのwarning filterを無関係な処理まで変更しないことも必要である。検証8は`max_pixels + 1`の画像が例外経路へ入り、警告を出しただけでdecodeを続行しないことを確認する形に具体化する。

採否: 修正して採用。Pillow採用と上限を警告で続行させない方針は正しいが、記載されたAPI設定だけではその方針を実現しないためである。

#### 指摘22 — PillowのWebP対応は追加codecなしには保証されない（重大度: 中）

最終案41行目はWebPを必須formatとし、「Pillowが追加codecなしで扱える」としている。しかしPillow公式のformat文書はWebPにlibwebp 0.5.0以上が必要だとし（[Pillow Image file formats](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#webp)）、source build文書もlibwebpを外部libraryとして列挙している（[Building from source](https://pillow.readthedocs.io/en/stable/installation/building-from-source.html#external-libraries)）。Pillowのversion下限とpinだけでは、全install環境のWebP featureを保証しない。

WebPを必須のままにするなら、package導入だけでなく`PIL.features.check_module('webp')`によるpreflightとCI確認を受け入れ基準へ加え、使えないinstallを明確に失敗させる必要がある。そこまで保証しないならAVIFと同じ任意集合へ移すこと。検証2も、WebP featureが無い環境を必ず不合格にするのか、原本fallbackで合格にするのかを一意に定める必要がある。

採否: 修正して採用。必須formatを決めた点は指摘17への前進だが、optional build featureを依存fileだけで必須化できるという前提が成立しないためである。

#### 結論

指摘15は解決済み、指摘18は採用とする。指摘16は指摘19の理由で未解決、指摘17は指摘21・22の理由で一部未解決である。指摘19〜22を最終案へ反映し、終端取得失敗とgate一致を別の状態遷移として定義した後に再レビューする。現時点では確定不可と判断する。

### レビュー6 — Codex (2026-08-26)

#### WebP対応についての人間判断

利用者がWebPを必須対応と決定した。したがって指摘22の選択肢のうち、WebPをAVIFと同じ任意集合へ移す案は不採用とする。最終案41行目の必須集合は維持し、`PIL.features.check_module('webp')`を画像取得前のpreflightで確認して、無効なinstallでは明確に失敗させる。CIでもWebPのdecode・縮小testを必須とする。

採否: 修正して採用。WebP必須という製品判断を採用し、optional build featureを依存fileだけでは保証できないという指摘22の技術的問題はpreflightとCIで解消する。最終案へこの保証を追記するまでは指摘22を完了扱いにしない。

### レビュー7 — Codex (2026-08-26)

#### 長辺上限についての人間判断

実Vaultで保存済みの説明画像177件を確認し、文書、地図、ニュース字幕、SNS画面を含む代表6件について、仕様の倍率式とLANCZOS filterで1,024・1,536・2,048pxの3水準、合計18枚の比較用PNGを作成した。利用者が個別画像を原寸で比較し、**1,024pxを既定値として問題ない**と判断した。

採否: 採用。最終案19行目、config例、migration、検証1、未決定事項の結論にある`max_long_edge_pixels=1024`を維持する。可読性を理由に1,536または2,048へ上げる必要はなく、この人間判断に関する未決定事項は解消した。

### レビュー8 — Claude Code (2026-08-26)

改訂1を反映した現在の最終案を、レビュー5〜7を読む前に一度自力で通読し、そのうえで各ラウンドと突き合わせた。

#### 独立に再発見した2点は、既に正しく直っている

読み始めた時点で、(a) 終端取得失敗で`completed`を`pending`へ戻すと保存済みOCRが`completed_image_ocr`（`feedian/store.py:1318`）の条件から外れて要約入力を失う、(b) `pending`は`_due`の最終分岐で毎回真になる（`feedian/image_ocr.py:434-445`）ので残件が収束しない、の2つを問題として書き出していた。どちらもレビュー5の指摘19・20が先に挙げており、改訂1で反映済みである。**現在の本文でこの2点は解消していることを確認した。**

改訂1の内容も検算した。`_due`は435行目の`if force or ... return True`が最初の文で、そこを通ると`pending`は`analysis_status in {'completed','ignored'}`に該当せず真を返す。指摘20の記述は正確である。`20260825-image-ocr-source-filenames.ja.md:208,210`の「再取得不能ならcurrentのまま、SHA-256変更を確認した場合だけ`pending`」という区別を、404とdecode不能へそのまま適用した改訂1-1の判断も、確定済み仕様と整合している。

schema追加なしの識別規則（`analysis_status='ignored'`かつ`image_kind='explanatory'`かつ`analysis_method`・`analysis_input_fingerprint`が非NULL）も成立することを確認した。LLM分類の`ignored`は`image_kind`が`explanatory`以外に限られ（`feedian/image_ocr.py:478-482`）、`explanatory`は必ず`completed`になる（同483-486）。SVG経路も`method="svg_text"`、`image_kind="explanatory"`を書くので（同211-214）、保持対象として同じ規則で拾える。**この規則は取りこぼしも誤検出も無い。**

以下は現在の本文に残る指摘である。

#### 指摘23 — gate抑止と終端抑止で`--force`の扱いが違うのに、判別方法が書かれていない（重大度: 中）

最終案は同じ`last_attempt_status='ignored'`に対して、`--force`の効き方を2通りに定めている。

- gate一致 — 「gateは`--force`より優先するため、`--force`でもこの抑止を越えない」
- 終端事象 — 「同じtargetでは`analysis_status='pending'`でもdueにしない。設定変更でtargetが変わるか`--force`が指定された場合だけ再試行する」

両者は`last_attempt_status`では区別できない。実装側の判別材料は`last_failure_kind`（終端は`unavailable:*` / `resource_limit:*`、gateはNULL）しかないが、本文はそれを指定していない。**実装順序3が「同じgate targetと終端targetのdue抑止」と一続きに書いているため、実装者は1つの規則として書き、どちらかの`--force`挙動を落とす。** 判別に使う列を本文で名指しすること。

あわせて、gate側の表現も直すべきである。`_due`は435行目で`force`を最初に見るので、`--force`時にgate行は再評価される。実際に起きるのは「抑止を越えない」ではなく「再評価はされるが取得は行われず、保持payloadを壊さない冪等な書き込みになる」である。現行実装でもgate優先はこの形（`_due`の下流の取得前gate分岐）で実現されており、`_due`の中ではない。現在の文言のままだと、`_due`に`force`より前段のgate判定を入れる実装になり、`--force`の意味が場所によって変わる。検証15は`--force`なしの2回目実行を見ているので、この差を検出できない。**`--force`時にgate保持行のpayloadが変化しないことを確かめる検証項目を足すこと。**

#### 指摘24 — 目的1の削減率は短辺下限を入れる前の見積りで、上限値である（重大度: 中）

目的1と未決定事項1の「1,024なら25.7〜38.7%」は、レビュー2で長辺上限だけを当てて算出した値である。その後に短辺下限（`k`の第2項）が入ったが、見積りは再計算されていない。**短辺下限は縦横比2:1超の画像で縮小を弱めるので、実際の削減はこの範囲より必ず小さい。** 提示した3水準（r=1.0 / 1.33 / 1.78）はいずれも2以下で、下限が一度も効かない条件だった。

さらに、この幅は「全画像が同じ縦横比だったら」という感度分析であって、実分布に対する信頼区間ではない。現行DBに画像寸法が保存されていないため（それを直すのが本仕様の監査情報である）、これ以上は詰められない。

数値を書き換える必要はないが、**「短辺下限を考慮しない上限値であり、縦横比の一様仮定に基づく感度分析である」と一文添えること。** 実装後の実測がこの範囲を下回っても仕様の失敗ではない、と読めるようにしておく必要がある。

#### 指摘25 — 人間確認に使った代表6件の選び方が記録されていない（重大度: 中）

実Vaultでの確認は「保存済み説明画像177件から代表6件を選び、18枚を原寸比較して1,024pxで問題ないと判断済み」としている。この判断は本仕様で唯一、数値では決められないとして人間に委ねた論点であり、未決定事項1の根拠にもなっている。**しかし選定基準が書かれていない。**

1,024pxが壊すのは、平均的な画像ではなく、小さな文字が密に入った図表である。「代表」を典型例として選んだのなら、失敗様態を体系的に外している可能性がある。実測ではOCR文字数の中央値が185字、最大が1,195字で、上位は日本語の統計グラフや制度説明図に偏っていた。

6件をどう選んだか（文字数上位から、文字の最小サイズから、host分散から、など）を1行で記録すること。**典型例から選んでいた場合は、文字数上位の数件を追加で確認すること。** 事後に「1,024で読めない図がある」と分かったとき、この1行があるかどうかで再判断の costが変わる。

#### 指摘26 — `terminal_unavailable_rows`の定義が状態遷移と噛み合っていない（重大度: 低）

終端事象で`completed`を維持した行は、`ignored`にならない。この行を`terminal_unavailable_rows`に数えるのかが本文から決まらない。reportの説明は`retained_current_rows`を「`gate_ignored_rows`または`terminal_unavailable_rows`の部分集合として併記」としているので、数える前提に読めるが、そうすると名前の`unavailable`が「無視した行」ではなく「取得できなかった行」を指すことになり、`gate_ignored_rows`との対称性が崩れる。どちらの定義かを一文で決めること。検証26は実数一致しか見ないので、この曖昧さを検出しない。

#### 指摘27 — `## 改訂`を`確定`前に使っている（重大度: 低・手続き）

`AGENTS.md`の`## 改訂`は、**確定した仕様の決定が後から覆されたとき**の手続きである。「ステータスは`確定`のまま」と明記されており、確定前の最終案の編集を記録する枠ではない。本文書のステータスは`レビュー中`なので、改訂1は本来レビュー往復の一部である。

このまま`確定`させると、確定後に本当の改訂が必要になったとき`改訂2`から始まり、`改訂1`が確定後の変更だったように読める。改訂の履歴は、後から「なぜこの決定が変わったのか」を追うためのものなので、確定前後の境界が消えるのは避けたい。**改訂1の冒頭に「確定前の最終案編集の記録である」と一文入れるだけで足りる。** 内容は有用なので、削除や移動は勧めない。

#### 表記

検証7の「codec未対応なら原本をbackendへ渡る」は「原本をbackendへ渡す」。意味を変えない修正なので、いつ直しても構わない。

#### 結論

指摘19〜22の反映は正しく、確定済み仕様との整合も取れている。残る4点（23〜26）は、いずれも実装者が本文だけを読んで作業したときに分岐を誤る箇所であり、指摘27は手続き上の記録の問題である。**指摘23だけは実装前に文言を直す必要がある。** 24・25・26・27は一文ずつの追記で済み、これらを入れれば`確定`にして差し支えない。

#### 採否と反映（2026-08-26）

| 指摘 | 採否 | 反映 |
|---|---|---|
| 23 | 採用 | gate抑止と終端抑止を`last_failure_kind`（gateはNULL、終端は`unavailable:*` / `resource_limit:*`）で判別すると明記し、`--force`時の挙動を2項目に分けた。gate側の「`--force`でも抑止を越えない」を「再評価はされるが取得を伴わない冪等な再書き込みであり、保持payloadは変化しない」に改め、`_due`へ`force`より前段の判定を持ち込まないことを禁止として書いた。検証15に`--force`実行の要求を追加した。 |
| 24 | 採用 | 目的1へ「短辺下限を考慮しない上限値であり、全画像が同じ縦横比だと置いた感度分析である」「実測がこの範囲を下回っても仕様の失敗ではない」を追記した。未決定事項1からも目的1を参照させた。 |
| 25 | 修正して採用 | 選定基準そのものは人間しか記録できないため本文には書かない。代わりに、実Vaultでの確認3へ「保存済み説明画像のうちOCR文字数上位から2件を必ず含める」を要求として追加し、理由（1,024pxが壊すのは平均的な画像ではなく小さな文字が密な図表であること）を添えた。事前比較の6件が典型例から選ばれていた場合の取りこぼしを、実装後の確認で塞ぐ。 |
| 26 | 採用 | `terminal_unavailable_rows`を「終端事象が起きた行の数であり、`ignored`になったかを問わない。`completed`を維持した行も含む」と定義し、`gate_ignored_rows`との名前の非対称が意図的であることを明記した。 |
| 27 | 採用 | 改訂1の冒頭へ「本項は確定前の最終案編集の記録である。確定後の改訂は改訂2から始まる」を追加した。内容は有用なので移動も削除もしない。 |
| 表記 | 採用 | 検証7の「原本をbackendへ渡る」を「渡す」に直した。 |

指摘25について、本文に選定基準を書けなかった点だけ残る。人間が6件をどう選んだかが分かれば、実Vaultでの確認3へ1行追記すればよい。追記が無くても、上の「文字数上位2件を必ず含める」で判断の抜けは塞がれている。

以上をもって未処理の指摘は無い。ステータスを`確定`にした。
