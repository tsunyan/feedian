# 説明画像のOCRとSourceノートの一意なファイル名

ステータス: 確定

## 最終案

### 目的と範囲

記事本文の理解に必要な文字を含む説明画像だけをOCRし、保存したOCR原文を既存の日本語要約へ追加する。写真、装飾イラスト、アイコン、ロゴ、広告は対象外とする。Feedianは完全な収集より日常の使用感を優先するため、画像gateは取りこぼしを許容して厳しめに設定する。

同時に、Sourceノートのファイル名へUUIDv7の完全な`resource_id`を使用する。現在の先頭8文字による衝突を解消し、SQLiteに残っている全current Sourceノートを一意なファイルへ再生成する。

この変更に含めないものは次のとおりとする。

- 本文またはOCR原文の日本語全文翻訳。
- Sourceノート生成を`ingest`から`render`へ移すこと。
- Raw、Comments、SourceのMarkdown生成経路の統合。
- HTML原本、画像bytes、PDFページの保存またはOCR。
- OCR原文をRawまたはSource Markdownへ表示すること。
- Rawノートとコメントノートの命名規則変更。
- 実行を跨ぐresource横断の画像解析cache。

### 処理フロー

```text
feedian sync
  └─ 本文原文と画像URLをSQLiteへ保存する。LLMは呼ばない。
  ↓
feedian enrich-images --limit N | --all
  ├─ 無料の名前gateと接頭辞denylist
  ├─ 画像取得とheader・寸法gate
  ├─ SVG text抽出、または説明画像判定とOCR
  └─ OCR現在値、無視理由、失敗と監査情報をSQLiteへ保存
  ↓
feedian ingest
  ├─ 本文原文と完了済みOCR原文から日本語要約を生成
  └─ 完全なresource_idを含むSource Markdownを従来の場所で生成
```

`sync`は画像を解析しない。`ingest`も画像を取得せず、その時点で保存済みの完了OCRだけを使う。OCRが未実行、無視、失敗、または空であっても、本文だけで従来どおり要約できる。

### 設定とmigration

Vault設定formatを3へ上げ、v2からv3への明示的migrationを追加する。top-levelの`image_ocr`を専用dataclassとしてparse・検証・renderし、未知fieldを拒否する。初版の設定は次のとおりとする。

| 設定 | 既定値 | 効く段階 | 意味 |
|---|---:|---|---|
| `image_ocr.workers` | `8` | 取得・gate | 全resourceで共有する画像処理の総並列数 |
| `image_ocr.timeout_seconds` | `15` | 取得・gate | 1回の画像HTTP取得timeout秒数 |
| `image_ocr.max_bytes` | `20971520` | 取得・gate | 1画像の取得上限。単位はbyte |
| `image_ocr.max_pixels` | `40000000` | 取得・gate | headerが宣言する総pixel数の上限 |
| `image_ocr.min_short_edge_pixels` | `200` | 取得・gate | rasterおよび寸法を読めるSVGの短辺下限 |
| `image_ocr.max_ocr_chars_per_image` | `2000` | OCR生成 | 画像1枚から保存するOCR原文の文字数上限 |
| `image_ocr.max_ocr_images_per_resource` | `8` | 要約への取り込み | `ingest`が要約へ取り込む説明画像数の上限 |
| `image_ocr.max_ocr_chars_per_resource` | `10000` | 要約への取り込み | `ingest`へ渡すOCR原文合計の文字数上限 |

全fieldを正の整数として検証し、`bool`を整数として受理しない。

**`max_ocr_images_per_resource`は`enrich-images`の動作に影響しない。** 画像取得数も解析数も保存件数も制限せず、`ingest`が要約requestへOCRを読み出す時だけposition順に最大8枚へ絞る。名前を`max_ocr_chars_per_resource`と揃えてあるのはこのためで、`max_ocr_*_per_resource`の2つはいずれも「1 resourceあたり要約へ何を渡すか」の上限である。枚数で切るか文字数で切るかだけが違う。

抽出された全候補は、この設定に関わらずgateを通る。8枚という上限は埋めるべき枠ではなく、gateを通った候補が3枚なら3枚だけが要約へ入る。

SQLite schema versionを10へ上げる。fresh schemaとversion 9からのmigration結果を同一にする。

### 画像取得のネットワーク要件

画像取得は本文取得と同じ`NetworkPolicy`を使う。最初のURLと全redirect先を検証し、許可されていないprivate address、`data:`、`blob:`、非HTTP(S) schemeを拒否する。DNS rebindingを含め、本文取得と同じ検証経路を共有し、画像専用の緩和を設けない。

**画像取得はHTTPだけで行う。** Browser、JavaScript、Service Workerを使わない。本文取得にあるbrowser fallbackを画像には持ち込まない。

これらは検証項目に留めず要件として守る。`--force`を含むどの実行modeでも緩めない。

### 画像gate

`extract_content_images`が返したcurrent revisionの全候補を対象とする。resource単位の候補数上限は設けない。gateは次の順で行う。

1. URLまたはファイル名が`@2x`、`@3x`、`logo`、`icon`、`avatar`、`profile`、`button`、`btn`、`banner`、`badge`、`sprite`、`spacer`、`blank`、`emoji`、`favicon`を示す候補を取得前に`ignored`とする。
2. URLが既知widget・商用endpointの接頭辞denylistに一致する候補を、取得前に`ignored`とする。初版のdenylistは次のとおりとし、code上の定数として持つ。設定化はしない。

```text
https?://b.hatena.ne.jp/entry/image/         はてなブックマーク数画像
https?://b.hatena.ne.jp/bc/                  同上（旧形式）
https?://pbs.twimg.com/amplify_video_thumb/  動画サムネイル
https?://pbs.twimg.com/ext_tw_video_thumb/   動画サムネイル
https?://pbs.twimg.com/tweet_video_thumb/    動画サムネイル
https?://pbs.twimg.com/card_img/             リンクカード画像
https?://pbs.twimg.com/cards/                同上
https?://pbs.twimg.com/media/                tweet添付画像（暫定除外）
```

3. 残った候補を`source_url`単位にまとめ、headerを取得する。
4. `Content-Type`を形式判定の正本とする。非画像MIME、TIFF、BMP、ICO、アニメーションを`ignored`とする。拡張子は補助情報に限る。
5. raster画像はheaderから寸法を読み、短辺200px未満を`ignored`とする。宣言pixel数またはbyte数が上限を超えた場合は、対象外ではなく資源保護上の`failed`とする。
6. gateを通ったraster画像だけを上限byte数まで完全取得し、LLM経路へ送る。

最初は`Range: bytes=0-8191`を使い、JPEG等で寸法が確定しなければ最大65536byteまで追加取得する。Rangeを無視して全体を返すserverでも`max_bytes`を超えて読まない。PNG、JPEG、GIF、WebP、対応するAVIFについて、寸法とアニメーションをheaderから判定する。完全decodeは行わず、Pillowを依存へ追加しない。対応形式なのに寸法を安全に確定できない入力は終端の`failed`とし、`ignored`にはしない。

gateは厳しめでよく、少数の説明画像を落とすことだけでは欠陥としない。全ての無視結果へ`ignored_reason`を保存し、理由別件数を報告する。`ignored_reason`はどの規則で落ちたかが分かる粒度とし、denylistでは`denylist:pbs.twimg.com/media`のように該当行を識別できる値にする。件数を数えてdenylistを見直せるようにするためである。`@2x` / `@3x`と`pbs.twimg.com/media`は最初に再評価する候補とするが、初版では除外する。

**gateは`--force`より優先する。** `--force`は取得と解析をやり直す指示であって、対象外判定を覆す指示ではない。gateで`ignored`とした画像は`--force`でも取得・解析しない。gateは費用の問題ではなく「説明画像かどうか」の判定であり、同じ入力に対して再実行で結論が変わる理由が無いためである。gateの構成そのものを変えた場合は、後述の取得前targetが変わることで対象へ戻る。

### SVG

`image/svg+xml`はLLMへ送らず、安全設定を施したXML parserで処理する。外部実体、DTD、外部network accessを無効化し、`max_bytes`を守る。

寸法はrootの有限で正の`width` / `height`を優先し、単位無しまたは`px`を受け入れる。取得できない場合は有限で正の`viewBox`幅・高さを使う。短辺200px未満は`small_dimensions`、寸法不明は`svg_unknown_dimensions`として`ignored`にする。

gateを通ったSVGでは`text`と`tspan`を文書順に読み、空白を正規化する。抽出可能な文字があれば`analysis_method='svg_text'`、`image_kind='explanatory'`、`analysis_status='completed'`として保存する。文字が無ければ`svg_without_text`として`ignored`にする。`foreignObject`、CSSによる生成内容、path化された文字の画像化は初版の対象外とする。

SVGでは`ocr_llm_run_id`、`analysis_backend`、`analysis_model`をNULLとする。指紋は画像SHA-256、正規化alt、SVG抽出器version、出力schema versionから作り、raster用のbackend・prompt指紋を流用しない。

### raster画像の説明判定とOCR

写真、装飾イラスト、アイコン、ロゴ、広告をさらに除外するため、gateを通ったraster画像をLLMで分類する。分類とOCRを別requestにせず、1画像につき1回の構造化出力で返す。

```json
{
  "image_kind": "explanatory",
  "ocr_text": "画像から読み取った原文",
  "ocr_truncated": false
}
```

`image_kind`は`explanatory`、`photo`、`decorative_illustration`、`icon_or_logo`、`advertisement`、`unknown`だけを許す。`explanatory`以外では`ocr_text`を空にして`ignored`とする。説明画像だが読める文字が無い場合は、空の`ocr_text`を持つ`completed`を許す。未使用の`use_for_summary` fieldは作らない。

OCRは見えている文字を原文のまま読み順に転記し、翻訳、要約、推測による補完をしない。画像1枚につき2,000文字で打ち切り、上限到達を`ocr_truncated=1`として保存する。promptで上限を指示し、構造化出力が途中で切れないようreasoning分を含む十分な`max_output_tokens`を与える。backend応答から出力上限到達を明示的に判定できる場合だけ`output_limit`として終端失敗にし、判定不能なprotocol errorは一過性失敗として扱う。

### backend契約

画像解析はVaultの`llm.backend`と`llm.model`を使い、`image_ocr`専用のbackend・model設定は作らない。fallbackへ自動切替しない。初版では次の3 backendを画像対応とする。

- `openai-responses`
- `codex-local`
- `claude-code-local`

`BackendCapabilities`へ画像解析対応、メッセージ総量上限、既存の`max_parallelism`を持たせる。総量上限は「上限なし」を表現できるようにする。対応しない`manus-api`では、planning後かつ画像取得前のpreflightで明確に失敗する。

3 backendは、画像fileまたはbytes、untrustedなURL・alt、OCR用schemaを受け取る共通methodを実装する。local backendでは画像pathをprompt文字列へ埋めず、各CLIの明示的な画像入力機構を使う。実装前に実際のflagと動作versionを検証し、verified versionのpreflight対象にする。agentの`view_image`等のtoolは無効のままとし、Feedianが選んだ1画像だけを渡す。認証、process隔離、audit、usage取得は既存backend契約を維持する。

global画像処理数は8だが、LLM requestはbackendの`max_parallelism`も守る。local backendは実効1並列であり、全件処理には数十時間以上かかり得る。開始時に実効LLM並列数を表示し、同一backendの画像解析実績があれば参考所要時間を表示する。実績がなければunknownとする。

### 並列処理、取得共有、結果伝播

記事ごとにexecutorを作らず、1回の`enrich-images`で全resource共通のexecutorを1つ持つ。画像取得・header判定・SVG抽出・LLM解析をglobalで最大8並列とする。**host単位の直列化は行わない。** HTML取得が`fetch.workers=8`をhost区別なく使うのと同じ方針であり、10以下の同時requestを事前に自主規制する理由が無いためである。HTTP 429と`Retry-After`は尊重する。これはhostが実際に制限を表明した場合の応答であり、事前の抑制とは別である。backend開始間隔はmain threadの投入条件で守り、workerをsleepで占有しない。

planning時に候補をまとめ、1回の実行中は次の単位で共有する。

- 画像取得、SHA-256、header・寸法gate: `source_url`単位。
- LLM分類とOCR: `(source_url, normalized_alt)`単位。

同じURLに異なるaltがあっても画像取得は1回だけ行う。workerは外部I/Oと解析だけを行い、SQLite、進捗、採否はmain threadが扱う。workerは画像bytesを内部で使うが、結果としてmain threadへ返さず、必要な一時fileもrequest終了後に削除する。taskは結果を反映する`resource_image`行のlistを持つ。

解析groupの結果は、選択batch外を含む該当行全てへ反映する。`--limit`は新規解析を開始するresource数の上限であり、既に支払った同一結果の伝播先を制限しない。伝播だけで追加の画像取得やLLM requestを行わない。進捗は選択resourceについて、関連する全taskが終端した時点で増やす。

Ctrl-C時は未開始taskをcancelし、開始済みtaskを回収する。未終端の監査runは失敗へ終端する。別processとの排他には既存の`vault_write_lock`を使う。

### SQLiteの現在値と試行履歴

OCRを`resource_revision.content_markdown`へ混ぜず、`resource_image`に現在値と最後の試行状態を分けて保存する。画像bytesは`payload`や`asset`へ保存しない。`resource_image`には少なくとも次を持たせる。

| 区分 | 列 | 意味 |
|---|---|---|
| 現在値 | `image_sha256` | 採用中結果が解析した画像bytesのSHA-256 |
| 現在値 | `analysis_status` | `pending` / `completed` / `ignored` / `failed` |
| 現在値 | `analysis_method` | `llm` / `svg_text` |
| 現在値 | `image_kind` | 画像分類 |
| 現在値 | `ocr_text` | OCRまたはSVGから得た原文 |
| 現在値 | `ocr_truncated` | 文字数上限で打ち切ったか |
| 現在値 | `ocr_char_limit` | 現在値を生成した時の画像単位上限 |
| 現在値 | `ignored_reason` | gateまたは分類による無視理由 |
| 現在値 | `ocr_llm_run_id` | 現在値を生成したLLM run。SVGではNULL |
| 現在値 | `analysis_input_fingerprint` | 採用中結果の再利用指紋 |
| 現在値 | `analysis_backend` | 実際に使ったbackend。SVGではNULL |
| 現在値 | `analysis_model` | 実際に使ったmodel。SVGではNULL |
| 現在値 | `analyzed_at` | 現在値を採用した時刻 |
| 試行 | `last_attempt_target` | 取得前にも計算できる試行抑止key |
| 試行 | `last_attempt_fingerprint` | SHA-256取得後の試行指紋。取得前失敗では空 |
| 試行 | `last_attempt_status` | 最後の試行の完了・無視・失敗 |
| 試行 | `last_failure_kind` | 一過性・終端を判定できる失敗種別 |
| 試行 | `transient_retry_used` | 同じtargetで次回1回再試行を消費したか |
| 試行 | `last_attempt_at` | 最後に試した時刻 |
| 試行 | `analysis_warning` | 秘密を含まない短い警告 |

`asset_id`と`use_for_summary`は追加しない。必要なCHECK、index、foreign keyをmigrationとfresh schemaの両方へ追加するが、初版では画像指紋によるresource横断cache tableやindexを作らない。

LLM経路は画像ごとに`llm_run.operation='image-ocr'`を記録する。requestにはURL、SHA-256、alt、prompt/schema識別子を保存し、画像bytesやbase64を保存しない。response、usage、実測price、backend metadata、所要時間、errorは既存の監査列を使う。SVG抽出はLLM runを作らず、画像解析試行として抽出器versionと結果を記録する。

### 再利用、再解析、失敗

rasterの採用結果の指紋は、画像SHA-256、正規化alt、backend ID、OCR prompt version、OCR schema versionから作る。`resource_revision_id`と`llm.model`は含めない。実際のmodelはauditに残す。model変更だけでは再解析せず、品質を比較する場合は`--force`を使う。

取得前の試行targetは、`source_url`、正規化alt、backend ID、prompt/schema version、取得・gateの上限設定、**名前gateのパターン集合、および接頭辞denylistの内容**から作り、画像SHA-256を含めない。targetが変わった場合は失敗抑止と一過性再試行済み状態をresetする。

gateの構成をtargetへ含めるのは、gateが`--force`より優先される（前述）ためである。denylistから1行を削除すれば、それだけでtargetが変わり、該当行が通常実行の対象へ戻って再評価される。含めなければ、暫定的に除外したものを戻す手段が`--force`しか無くなり、`--force`は選択resourceの全候補を巻き込むため、無関係な画像まで払い直すことになる。denylistへの追加も同様に、既存の`completed`行を対象へ戻して`ignored`へ落ち着かせる。

同じ行の採用結果指紋が一致すれば、通常実行では再取得・再解析しない。このため、同じURLの内容がserver側で差し替わっても自動検知せず、`--force`で確認する。backend、prompt/schema、altが変わった場合は通常実行で再解析する。model変更だけは対象にしない。

`max_ocr_chars_per_image`を引き上げた場合、`ocr_truncated=1`かつ保存済み上限が新しい値より小さい行だけを通常の再解析対象へ戻す。切り詰められていない行は上限変更だけで再解析しない。再取得不能なら、保存済みの切り詰め結果をcurrentとして残し、失敗した試行だけを記録する。

失敗を次のように扱う。

- 一過性: timeout、DNS・接続失敗、HTTP 429、HTTP 5xx、backend一時障害、判定不能なprotocol error。
- 終端: HTTP 404 / 410、壊れたまたは未対応の画像header、byte・pixel上限超過、明示的に判定できた出力上限到達。
- 対象外: 非画像MIME、除外形式、アニメーション、各gate条件。`failed`ではなく`ignored`。

一過性失敗は同じtargetについて次の通常実行で1回だけ再試行する。その再試行も失敗した場合と、終端失敗は`--force`またはtarget変更まで抑止する。上限超過は該当する上限を変更した時点でtargetが変わり、再試行可能になる。

新しい解析が失敗しても、同じ画像に対する既存の完了結果を先に消さない。成功したtransactionでのみ新しい現在値へ置き換える。

**画像bytesのSHA-256が変わったことまで確認できた場合は、`analysis_status`を`pending`へ戻し、他の現在値列はそのまま残す。** `ocr_text`、`image_kind`、`ocr_truncated`、`image_sha256`などは最後に判っていた値として保持し、物理削除しない。

この状態を表すために`analysis_status`へ新しい値を足さない。`pending`の意味は「未処理、または入力が変わって再処理が必要」であり、この状況にそのまま当てはまる。`ingest`の読み出し条件は`analysis_status='completed'`を含むので、`pending`へ戻した時点で要約入力から自動的に外れる。したがって「渡さないが消さない」は`pending`と現在値の保持だけで表現でき、値域の変更もmigrationでの`CHECK`変更も要らない。

**`pending`の行が非空の`ocr_text`を持ち得ることを明記する。** 草案§5にあった「`pending`では`ocr_text`は空」という対応は採らない。保持している値は監査と、再解析が失敗し続けた場合に最後に判っていた内容を追える状態を残すためのものであり、要約へは使われない。

この行は`pending`なので通常実行の対象条件（未処理画像）に該当するが、失敗の抑止は`last_failure_kind`と`transient_retry_used`が別に管理する。再解析が終端失敗した行は、`analysis_status`が`pending`のままでも通常実行では選ばれない。

`sync`時の`resource_image`は`(resource_id, source_url)`による差分更新とし、継続URLのIDと解析結果を維持してaltとpositionを更新する。新規URLは`pending`で追加する。抽出結果が0件の場合は、抽出失敗と正当な全削除を区別できないため、既存の完了OCRを削除しない。

### `enrich-images` command

```text
feedian enrich-images (--limit N | --all) [--force] [--dry-run] [--progress MODE]
```

`--limit N`と`--all`は相互排他的な必須選択肢とし、どちらも無ければusage errorで処理を開始しない。`--limit`は正のresource件数、`--all`はその時点の既定対象全件を意味する。`--dry-run`でも同じ選択を必須とし、外部取得とDB書き込みを行わない。

通常の対象は、次のいずれかを持つcurrent resourceとする。

- 未処理画像。
- 現在値と異なるtargetまたは採用結果指紋。
- 一過性失敗で、次回1回の再試行をまだ使っていない画像。
- 保存済み結果が切り詰め済みで、設定上限が保存時より増えた画像。

終端失敗、再試行済みの一過性失敗、指紋一致の完了・無視画像は通常対象外とする。

対象から`--limit N`件を選ぶ順序は、**resource revisionの作成時刻の古い順**とする。同時刻の並びを固定するため`resource_id`を第2キーにする。`ORDER BY`を指定せずSQLiteの行順に委ねてはならない。同じVaultへ同じcommandを実行したときに選択が変わらないようにするためである。古い順とするのは、繰り返し`--limit`を実行したときに未処理分を先頭から順に消化でき、どこまで進んだかが判りやすいためである。

`--force`は選択resourceの候補を再取得・再解析する。ただし前述のとおりgateはこれより優先し、gateで`ignored`とした画像は`--force`でも取得しない。

外部request前に次を表示する。

- 既定対象として残っているresource総数。
- 今回選択したresource数。
- 選択resourceの候補画像行数。
- 取得前gate後のdistinct `source_url`数。
- 指紋一致で処理不要な行数と、今回処理予定の解析group数。
- request数の多いhost。情報として示すもので、host単位の直列化は行わないため所要時間の制約要因ではない。
- global並列数、backend実効並列数、実測があれば参考所要時間。

寸法gate後の件数は取得前に分からないため、開始時は上限として示し、終了時に確定値を報告する。

`--all`を選んだ場合は、これから取得するdistinct `source_url`数を**そのVaultについて計算した実数で**表示する。参照Vaultの測定値を出力へ埋め込まない。参照Vaultでは取得前gate後に19,338件が残る（候補62,438件、名前gate後50,881件）が、これは仕様の根拠として本文に記録する数値であって、CLIが表示する内容ではない。

### `ingest`へのOCR追加

current revisionの`resource_image`から、`analysis_status='completed'`、`image_kind='explanatory'`、`ocr_text`が非空の結果をposition順に最大8枚読み出す。合計10,000文字で打ち切る。backendにメッセージ総量上限がある場合は、その残り枠と10,000文字の小さい方をOCR予算にする。

本文と各OCRを別のuntrusted blockとして構築する。URL、alt、OCRなどの外部値をXML風tagの属性へ直接連結しない。既存のuntrusted message構築処理を共通化し、delimiterを壊せない形へescapeする。切り詰めはwrapper生成後の自由な文字位置ではなく、画像block単位とblock内の安全な境界で行う。

OCRを持たないresourceでは、request本文、共通instructions、`prompt_version='source-note-v1'`を従来とbyte単位で維持する。OCRを持つresourceだけOCR blockを加え、`prompt_version='source-note-v2'`を使う。`IngestCandidate`がprompt versionを保持し、cache検索、legacy fingerprint promotion、run記録の全経路で同じ値を使う。要約result schemaは変更しない。

OCR追加・変更でrequest fingerprintが変わった要約済みresourceを選べるよう、`ingest`へ`--stale` modeを追加する。これはOCRだけでなく、現在のrequest fingerprintに一致する再利用可能な完了runを持たないresourceを選ぶ一般的なmodeとする。`--auto`とは排他的にし、`--dry-run`で対象数、再利用数、実測履歴に基づく既存の推定費用を表示する。

### Sourceノートの完全UUIDファイル名

Sourceノート名を次へ変更する。

```text
{sanitizeしたタイトル先頭60文字} - {完全なresource_id}.md
```

UUIDv7を切り詰めたりhash化したりしない。書き込み前に全current `source_note`の予定パスを構築し、resource IDの妥当性、current noteでのID重複、sanitize後のパス一意性、予定パスの既存file種別を検証する。probe fileは作らず、決定的に検証できるpath component条件を事前確認し、実際のatomic write失敗をblocking conflictとして扱う。

予定パスに同じ`resource_id`のFeedian管理Sourceノートがあれば、通常の更新としてDBのcurrent Markdownでatomicに置換する。同内容ならskipする。Sourceノートには`render_hash`を追加せず、予定パス上の手編集を保護しない。予定パスが別resourceの管理file、非管理file、またはfrontmatter解析不能fileなら上書きせずblocking conflictにする。

### 旧短縮名の移行

Sourceノート生成場所と呼び出し時点は変えず、`ingest`末尾の`render_source_notes`へ移行を組み込む。Source folder内の全Feedian管理Sourceノートを完全なfrontmatter `resource_id`で索引化する。タイトル変更で旧pathが変わるため、予定path周辺だけに走査を限定しない。

移行は次の順で行う。

1. DBの全current Sourceノートから完全UUIDのcanonical予定pathを作る。
2. 全予定pathを検証する。blocking conflictがあれば該当pathを上書きしない。
3. 競合のない全canonical fileをDBの`source_note.markdown`から同一directoryの一時fileへ書き、atomicに置換する。
4. canonical出力だけについて、frontmatter ID集合がDBのcurrent ID集合と一致することを確認する。
5. canonical fileの存在と内容を確認したresourceについてだけ、予定path外の旧管理fileを整理する。

旧fileを削除できるのは、`feedian_managed: true`、`feedian_kind: source`、DBにcurrent noteが存在し、CRLF/LFを正規化した内容がcurrent `source_note.markdown`と一致し、同内容のcanonical fileが存在する場合だけとする。条件を満たさない予定path外のfileは`protected`として残す。予定path上の正常な同一ID管理fileとは扱いを分ける。

途中の書き込み失敗後は旧fileの削除へ進まない。SQLiteを正本とし、短縮名衝突で既に欠落したfileもDBから全件再生成する。mtimeを新しさの判定に使わない。

`render_source_notes`は`written`、`skipped`、`migrated`、`protected`、`blocking_conflicts`を返す。blocking conflictだけがSource生成の終了コードを非0にする。canonical出力が完成している状態で残した予定path外のprotected fileは報告対象だが、それだけでは失敗にしない。`ingest --dry-run`でも同じ移行planを読み取り専用で表示する。

### 進捗、終了結果、費用

`enrich-images`は少なくとも次を報告する。

```text
resources=<選択resource数>
candidate_rows=<候補画像行数>
fetch_urls=<実際に取得したdistinct URL数>
analysis_groups=<LLMまたはSVG解析group数>
completed=<説明画像の完了行数>
ignored=<対象外行数>
reused_existing=<実行前から指紋一致していた行数>
shared_groups=<実行内で結果を共有したgroup数>
propagated_rows=<結果を反映した全行数>
propagated_resources=<選択外を含む反映resource数>
ocr_truncated=<2,000文字で打ち切った行数>
failed=<失敗行数>
input_tokens=<実測入力token数>
output_tokens=<実測出力token数>
cost_usd=<backendが返した実測額。得られなければunknown>
unpriced=<単価不明件数>
unmetered=<従量計測対象外件数>
```

`ignored_reason`と`failure_kind`の内訳も表示する。推定画像token、`estimated_cost_usd`、`max_cost_usd`は実装しない。費用と所要時間は小さい`--limit`の実測から判断する。画像失敗を本文取得失敗にせず処理を継続するが、1件以上の`failed`があればcommandは非0で終了する。

Source生成は`source_written`、`source_skipped`、`source_migrated`、`source_protected`、`source_blocking_conflicts`を表示する。

### 検証

少なくとも次を自動テストと実Vault copyで確認する。

1. SSRF対策を最初のURLと全redirectへ適用し、private address、非HTTP(S)、`data:`、`blob:`を拒否する。
2. 名前gate、接頭辞denylist、非画像・形式・アニメーション・200px gateが規則を識別できる`ignored_reason`付きで`ignored`になる。denylistから行を削除するとtargetが変わり、該当行だけが`--force`なしで再評価される。gateは`--force`より優先し、`ignored`とした画像を`--force`でも取得しない。
3. Range非対応、切断header、JPEGの64KiB再取得、巨大宣言寸法、byte上限を安全に扱う。
4. SVG parserがDTD、外部実体、network accessを使わず、寸法と`text` / `tspan`を規則どおり処理する。
5. 表、図、UI screenshotを`explanatory`として原文OCRし、写真、装飾、icon、logo、広告を無視する。
6. OCRが2,000文字で打ち切られ、flag・実行時上限が保存され、上限引き上げ後は切り詰め行だけが再解析される。
7. global executorが8を超えず、host単位の直列化を行わず、backend requestが`max_parallelism`を守り、workerがSQLiteを呼ばない。SSRF検証を最初のURLと全redirectへ適用し、browserを起動しない。
8. 同じURLの取得が1回、同じURL・altの解析が1回になり、結果を全該当行へ伝播する。
9. local CLIのverified versionだけが明示的な画像入力で動き、非対応versionは取得前に失敗する。
10. 一過性失敗が次回1回だけ再試行され、終端失敗と再試行済み失敗は`--force`まで抑止される。
11. reanalysis失敗時に既存OCRを失わず、SHA-256の変化を確認した行は`analysis_status='pending'`へ戻り、`ocr_text`を保持したまま要約から外れる。`--limit`の選択がrevision作成時刻の古い順で、同じ入力に対して安定する。
12. OCRなしrequestが従来とbyte単位で同じv1、OCRありrequestだけがv2となり、cache・run記録のversionが一致する。
13. `ingest --stale`がOCR追加その他でcurrent fingerprintを持たないresourceだけを選べる。
14. 完全UUID名で同一タイトルの全Sourceノートが別fileへ復元される。
15. 予定pathの同一ID管理fileは更新され、別ID・非管理・解析不能fileはblocking conflictになる。
16. 編集済みの予定path外fileを残し、安全条件を満たす旧短縮名だけを削除する。
17. Source canonical ID集合がDB current集合と一致し、protected extraを別集計できる。
18. config v2→v3、SQLite v9→v10、snapshot、restore、`PRAGMA quick_check`、`PRAGMA integrity_check`が成功する。
19. OCR未実行のVaultで既存のsync、ingest、render、snapshot動作が変わらない。
20. `python -m pytest -q`が成功する。

### 実装順序

1. Sourceノートの完全UUID名、事前検証、report、旧短縮名移行を先に実装する。
2. Vault config v3、SQLite schema v10、`resource_image`の差分更新と現在値・試行状態を実装する。
3. NetworkPolicyを共有する画像取得、header gate、安全なSVG抽出を実装する。
4. 3 backendの画像解析契約、verified version、構造化出力と監査を実装する。
5. `enrich-images`のplanning、global scheduler、共有、再試行、進捗とreportを実装する。
6. 保存済みOCR、conditional prompt version、`ingest --stale`を実装する。
7. `DESIGN.md`へ現在動作の要約と本仕様へのリンクを追加する。
8. 参照VaultのcopyでSource件数照合とmigrationを検証し、小さい`enrich-images --limit`で実測してから実Vaultへ適用する。

仕様確定commitはこの文書だけを`docs:` typeでcommitする。実装commitにはコード、migration、テスト、`DESIGN.md`更新を含め、仕様commitとは分ける。

## 改訂

### 改訂1 — Claude Code / tsunyan (2026-08-25)

対象箇所: 画像取得の安全要件、取得前gate、並列制御、再解析状態、commandの対象順と表示。

（前）

- 画像取得のSSRF要件は検証項目と実装順序にだけあり、本文の要件として独立していなかった。
- 取得前gateは完全一致URLのresource出現数を閾値10で数える頻度gateだった。
- 同一hostのHTTP requestを1並列へ制限していた。
- SHA-256変更後に旧OCRを要約から外しつつ保持する状態、`--limit`の選択順、`--force`とgateの優先関係が未定義だった。
- 参照Vaultの約50,400件という測定値をCLIの表示内容と混同していた。

（後）

- 本文取得と同じ`NetworkPolicy`を最初のURLと全redirectへ適用し、private address、非HTTP(S)、`data:`、`blob:`、DNS rebindingを拒否する。画像ではBrowser、JavaScript、Service Workerを使わない。
- 頻度gateとその設定を削除し、はてなブックマーク数画像、Twitter動画・カード・tweet添付画像を名指しする静的な接頭辞denylistへ置き換えた。名前gateとdenylistの内容を取得前targetへ含める。
- host単位の直列化を削除し、global 8並列を使う。HTTP 429と`Retry-After`は尊重する。
- SHA-256変更確認後は`analysis_status='pending'`へ戻し、非空の旧OCRを監査用に保持する。`--limit`はrevision作成時刻の古い順、同時刻は`resource_id`順とする。gateは`--force`より優先する。
- CLIは各Vaultについて計算した実数を表示し、参照Vaultではdenylist後に19,338 distinct URLだった値を仕様根拠としてのみ記録する。

理由: レビュー16〜25で、host分布とURL分布の実測、人間による厳しめgateの判断、状態遷移とCLI再現性の不足が明らかになったため。

根拠: レビュー16の指摘AX〜BD、レビュー17の決定AX・AY、レビュー18〜23の実測と決定、レビュー24の決定AZ・BA・BB・BD、レビュー25の反映記録。

### 改訂2 — Claude Code (2026-08-25)

対象箇所: 1resourceあたり要約へ取り込むOCR画像数の設定名と設定表。

（前）

```text
image_ocr.max_images_per_resource = 8
```

この名前は`enrich-images`が取得・解析する画像数の上限に見える一方、実際には`ingest`の読み出し時だけに使う決定となっていた。

（後）

```text
image_ocr.max_ocr_images_per_resource = 8
```

設定表へ「効く段階」列を加え、取得・gate、OCR生成、要約への取り込みを区別した。新しい設定は画像取得数、解析数、保存件数を制限せず、`ingest`がposition順に最大8枚を読み出す時だけ使う。

理由: `max_ocr_chars_per_resource`と命名を揃え、全候補をgateする決定との読み違いを防ぐため。

根拠: レビュー16の指摘BE、レビュー25の未処理記録、レビュー26の変更記録。

## 草案

### 背景

FeedianはHTML本文から記事領域の画像URLを抽出し、`resource_image`へURL、alt、表示順を保存している。しかし画像本体は取得せず、画像内の文字も要約入力に含めていない。そのため、本文の説明を図、表、グラフ、画面キャプチャなどへ委ねた記事では、要約が重要な情報を欠く。

SourceノートのMarkdownファイル名には、現在`resource_id`の先頭8文字だけを使っている。`resource_id`はUUIDv7であり、先頭8文字は48ビットのミリ秒時刻の上位32ビットにすぎない。同じ約65.536秒の間に生成したIDは同じ先頭8文字を持つため、同名タイトルでは出力パスが衝突する。

2026-08-23時点の参照Vaultでは、現行のSourceノート9,132件から計算される一意なファイル名は7,291件だけであり、1,841件が同じ出力パスへ重なる。衝突は280組あり、最大の組は47件である。DBの`source_note`は残っているが、後から書いたMarkdownが先行ファイルを上書きしている。

この変更では、説明画像から得たOCR原文を保存して要約へ渡すことと、Sourceノートのファイル名へ完全な`resource_id`を使用してDB上の全ノートを一意に書き出すことを扱う。

### 目的

1. 記事本文に含まれる説明画像を識別し、その画像だけからOCR原文を取得する。
2. 全記事で共有する画像実行枠を使い、候補画像を記事境界にかかわらず並列処理して待ち時間を抑える。
3. OCRの現在値、失敗状態、入力画像の指紋、採用したLLM実行をSQLiteへ保存する。
4. 本文原文とOCR原文を同じ要約入力に含め、日本語要約が画像内の情報を利用できるようにする。
5. Sourceノートのファイル名へ完全なUUIDv7の`resource_id`を使用し、異なるresourceが同じファイルへ書かれないようにする。
6. DBに残っているSourceノートを一意なパスへ再生成し、旧短縮名を安全に整理する。

### 非目的

- 本文またはOCR原文の日本語全文翻訳。
- Sourceノート生成を`ingest`から`render`へ移すこと。
- Raw、Comments、SourceのMarkdown生成経路を統合すること。
- HTML原本の保存。
- 写真、装飾イラスト、アイコン、ロゴ、広告の保存またはOCR。
- PDFページのOCR。PDFの画像化とページ単位OCRは別の取得経路であり、この変更に含めない。
- OCR原文をRawまたはSource Markdownへ表示すること。初版ではDB保存と要約入力だけを行う。
- Rawノートとコメントノートのファイル命名規則の変更。

### 全体フロー

```text
feedian sync
  ├─ provider metadataを保存
  ├─ HTMLを取得して本文を抽出
  └─ 本文と画像URLをSQLiteへ保存
       ※ 現行どおりHTML原本は保存しない
  ↓
feedian enrich-images
  ├─ main threadがresourceごとの候補画像を計画
  ├─ 全resourceで共有する画像実行枠へ投入
  │    ├─ 画像取得
  │    └─ 説明画像判定とOCRを1回の解析で実行
  └─ main threadが画像結果をresourceごとにSQLiteへ保存
  ↓
feedian ingest
  ├─ 本文原文と保存済みOCR原文から要約requestを構築
  ├─ 日本語要約を生成してsource_noteへ保存
  └─ Source Markdownを従来の場所で生成
       └─ 完全なresource_idをファイル名に使用
```

`feedian sync`は引き続き「LLMを呼ばずに外部sourceをSQLiteへ収集する」commandとする。画像の視覚判定とOCRは、明示的な`feedian enrich-images`で行う。これにより通常の同期が画像解析費用を暗黙に発生させず、OCRを行わない運用も維持できる。

`feedian ingest`は画像を取得またはOCRせず、その時点でSQLiteに保存されている完了済みOCRだけを利用する。OCRが未実行、無視、失敗のいずれであっても、本文だけで従来どおり要約できる。

### 1. 説明画像の範囲

OCR対象とする説明画像は、画像内の文字が記事内容の理解に寄与するものとする。

対象:

- 表、グラフ、チャート。
- 手順図、構成図、模式図。
- UI、ターミナル、コード、設定画面のスクリーンショット。
- スライド、解説パネル、文字を主内容とする画像。
- 記事中に埋め込まれたスキャン文書。

対象外:

- アイキャッチ写真、人物・風景・商品の写真。
- 装飾目的のイラスト。
- アイコン、ロゴ、アバター。
- 広告、バナー、トラッキングピクセル。
- 写真へ偶然写り込んだ看板など、記事の説明として配置されていない文字。

「イラスト」という画材だけでは判定しない。装飾イラストは除外するが、説明を担う模式図は対象とする。

既存の`extract_content_images`が記事領域の選択、広告・pixelの除外、URL重複排除を行う。この判定を第1段階として維持し、残った候補を視覚解析へ渡す。

### 2. 画像取得

画像取得は本文取得と同じ`NetworkPolicy`を使う。最初のURLと全redirect先を検証し、許可されていないprivate address、`data:`、`blob:`、非HTTP(S) schemeを拒否する。Browser、JavaScript、Service Workerは使わない。

取得には画像専用のtimeoutと最大byte数を設ける。応答の`Content-Type`が`image/*`でない場合、またはdecoderが画像として読めない場合は失敗とする。画像展開後のpixel数にも上限を置き、圧縮された小さな入力から過大なメモリを消費させない。

初期値は次のとおりとする。

| 設定 | 既定値 | 意味 |
|---|---:|---|
| `image_ocr.workers` | 8 | 全resourceで共有する画像executorの総並列数 |
| `image_ocr.max_images_per_resource` | 8 | 1resourceで解析する画像の上限 |
| `image_ocr.max_bytes` | 20 MiB | 1画像の取得上限 |
| `image_ocr.max_pixels` | 40,000,000 | decode後の総pixel数上限 |
| `image_ocr.timeout_seconds` | 15 | 1画像のHTTP取得timeout |
| `image_ocr.max_ocr_chars_per_resource` | 10,000 | ingestへ渡すOCR原文の合計上限 |

全て正の整数として検証し、`bool`を整数として受理しない。候補が上限を超える場合は、`resource_image.position`の小さいものから採用する。残りを「失敗」にはせず、未処理のまま残す。

### 3. 全記事で共有する画像並列処理

記事ごとにexecutorや固定の実行枠を持たない。`enrich-images`の実行全体で1つの画像executorを持ち、複数resourceの候補画像が同じ実行枠を共有する。画像が少ない記事が続いても空き枠を後続記事が利用できるようにする。

```text
main thread
  resource Aの候補を計画 ─┬─ image A1 ─┐
                           └─ image A2 ─┤
  resource Bの候補を計画 ─┬─ image B1 ─┼─ 共有image executor（全体で最大8）
                           └─ image B2 ─┤
  resource Cの候補を計画 ─── image C1 ─┘
  ↓ 完了した結果を回収
  resourceごとにDBへ保存して進捗を確定
```

resourceごとに`ThreadPoolExecutor`を作り直さない。schedulerはglobal枠とbackend枠に空きがある画像だけを投入し、executorのqueueへ無制限に積み上げない。1resourceあたりの候補は`image_ocr.max_images_per_resource`で制限するが、同時実行数は全resourceを合わせて`image_ocr.workers`を超えない。

main threadはresourceごとに未完了画像数を持つ。あるresourceの全画像が`completed`、`ignored`、`failed`、または`reused`へ終端した時点で、そのresourceの進捗を1増やす。完了順はresourceの入力順と一致しなくてよいが、保存する画像順とingestへ渡すOCR順は`resource_image.position`で決める。

確定済みの並列処理方針に従い、workerは外部I/Oと画像解析だけを行う。`VaultStore`、SQLite、進捗、採否判断はmain threadに残す。workerから返す値は、画像URL、SHA-256、MIME type、分類、OCR原文、監査情報、警告、および保存対象画像のbytesに限定する。

実効並列数は`min(image_ocr.workers, backend.capabilities.max_parallelism)`とする。backendの開始間隔も全画像で共有し、workerがsleepしたまま実行枠を占有しない。`ingest`のschedulerと同じく、main threadの投入条件として開始間隔を守る。

Ctrl-C時は未開始の画像taskをcancelし、開始済みtaskだけを回収する。main threadが開始記録を作ったまま結果を保存できなかった実行は失敗終端する。別processとの排他には既存の`vault_write_lock`を使う。

### 4. 説明画像判定とOCRの契約

説明画像判定とOCRを別々の外部requestにしない。1画像につき1回の構造化出力で、分類とOCR原文を同時に返す。

```json
{
  "image_kind": "explanatory",
  "ocr_text": "画像から読み取った原文",
  "use_for_summary": true
}
```

`image_kind`は次の列挙値だけを許す。

```text
explanatory
photo
decorative_illustration
icon_or_logo
advertisement
unknown
```

`use_for_summary`がtrueになれるのは`image_kind=explanatory`だけとする。それ以外では`ocr_text`を空文字へ正規化する。説明画像で読める文字が無い場合は、`image_kind=explanatory`、`use_for_summary=true`、`ocr_text=""`を許す。

OCRは見えている文字を原文のまま転記する。翻訳、要約、画像からの推論による文章補完は行わない。改行は可能な範囲で保持する。判読できない箇所を推測で埋めない。

初版はVaultの`llm.backend`と`llm.model`を使用する。`BackendCapabilities`へ画像解析対応の宣言を追加し、対応を宣言していないbackendではrequest開始前に失敗する。fallback backendへの自動切替は初版では行わない。画像解析の失敗と記事要約のfallbackを同じ規則へ結び付けないためである。

### 5. SQLiteへの保存

OCR原文は`resource_revision.content_markdown`へ混ぜない。本文原文と再生成可能な派生データの境界を維持し、OCR更新だけでresource revisionを作らない。

SQLite schema versionを10へ上げ、`resource_image`へ次の現在値を追加する。

| 列 | 型 | 意味 |
|---|---|---|
| `image_sha256` | `TEXT NOT NULL DEFAULT ''` | 実際に解析した画像bytesのSHA-256 |
| `analysis_status` | `TEXT NOT NULL DEFAULT 'pending'` | `pending` / `completed` / `ignored` / `failed` |
| `image_kind` | `TEXT NOT NULL DEFAULT 'unknown'` | 構造化出力の分類 |
| `ocr_text` | `TEXT NOT NULL DEFAULT ''` | 説明画像から得たOCR原文 |
| `ocr_llm_run_id` | `TEXT REFERENCES llm_run(llm_run_id)` | 現在値を生成した実行 |
| `asset_id` | `TEXT REFERENCES asset(asset_id)` | 保存した説明画像本体。対象外画像ではNULL |
| `analysis_input_fingerprint` | `TEXT NOT NULL DEFAULT ''` | 再利用判定用の入力指紋 |
| `analyzed_at` | `TEXT` | 完了・無視・失敗を確定した時刻 |
| `analysis_warning` | `TEXT` | 画像単位の失敗理由 |

`analysis_status`の意味は次のとおりとする。

| 状態 | 意味 | `ocr_text` |
|---|---|---|
| `pending` | 未処理または入力変更により再処理が必要 | 空 |
| `completed` | 説明画像の判定とOCRが完了 | 空または非空 |
| `ignored` | 説明画像ではない | 空 |
| `failed` | 取得または解析に失敗 | 空 |

`llm_run`には`operation='image-ocr'`として画像ごとの実行を記録する。`request_json`には画像bytesやbase64を重複保存せず、URL、画像SHA-256、alt、resource revision、プロンプトとschemaの識別情報を保存する。response、usage、price、backend metadata、所要時間、errorは既存列を使う。

入力指紋は少なくとも次を含む。

```text
画像SHA-256
alt
resource_revision_id
backend ID
model
OCR prompt version
OCR result schema version
```

同じ入力指紋の完了結果があれば外部requestを行わず再利用する。`--force`は再利用を無視する。

説明画像のbytesは既存のcontent-addressedな`payload`へ保存し、`asset`でresource、resource revision、source URLへ関連付ける。写真、装飾イラスト、アイコンなど対象外画像のbytesは保存せず、SHA-256、分類、実行履歴だけを残す。

画像取得またはOCRの失敗は`resource`の本文取得失敗にしない。`resource_image.analysis_status='failed'`とwarningを記録し、同じresourceの残り画像と後続resourceを処理する。commandの終了コードは、1件以上の画像が失敗した場合に非0とし、成功件数、無視件数、失敗件数を表示する。

### 6. `resource_image`の更新規則

現在の`replace_resource_images`はresourceの全画像行を削除してから挿入し直す。このままではsyncのたびにOCR結果を失うため、`(resource_id, source_url)`をキーとする差分更新へ変更する。

- 継続して存在するURLは同じ`resource_image_id`を維持し、`resource_revision_id`、alt、positionだけを更新する。
- 新しいURLは`pending`で追加する。
- 現在のHTMLから消えたURLは削除する。
- URLが同じでも、再取得した画像SHA-256が変われば現在のOCR値を消して`pending`へ戻す。
- SHA-256、backend、model、prompt version、schema versionが一致すれば、resource revisionが変わっても画像解析結果を再利用できる。ただしaltを解析入力に使うため、altが変わった場合は入力指紋が変わる。
- 画像行の削除によって参照されなくなったpayloadは、既存のorphan cleanupで削除できるよう参照集合へ`resource_image.asset_id`経由のpayloadを含める。

### 7. `enrich-images` command

```text
feedian enrich-images [--vault PATH] [--limit N] [--force] [--dry-run] [--progress MODE]
```

- 対象は、現在のresource revisionに属し、`resource_image`を1件以上持つresourceとする。
- 既定では`pending`と`failed`、または現在の解析設定と入力指紋が一致しない画像だけを対象にする。
- `--limit`はresource件数の上限であり、画像件数ではない。
- `--force`は対象resourceの候補画像を再取得・再解析する。
- `--dry-run`は対象resource数、候補画像数、再利用数、最大外部request数を表示し、DBと外部サービスを変更しない。
- progressはresourceの処理完了時に1増やす。画像taskの完了数をresource進捗へ混ぜない。

planningでは外部取得もDB書き込みも行わない。実行開始後、main threadが画像ごとの監査runを開き、workerへ投入し、回収後に完了または失敗へ終端する。

### 8. ingestへのOCR入力追加

`_source_rows`または同等の取得処理で、現在のresource revisionに属する`resource_image`のうち、次を満たすOCRをposition順に取得する。

```text
analysis_status = completed
image_kind = explanatory
ocr_textが空でない
```

本文とOCRは、信頼されない入力として明確に区切る。

```text
<untrusted_page_text>
本文原文
</untrusted_page_text>

<untrusted_image_ocr position="0" source="https://example.test/diagram.png">
OCR原文
</untrusted_image_ocr>
```

OCR原文の合計は`image_ocr.max_ocr_chars_per_resource`で切り、画像順を維持する。本文に適用している`max_article_chars`は従来どおり本文だけへ適用し、OCR枠とは分ける。token見積りは実際に送るOCRを含める。

要約request全体のfingerprintへOCRブロックが入るため、OCRが追加・変更されたresourceでは既存要約を再利用しない。OCRが無いresourceのrequestは、prompt versionの更新を除いて従来と同じ本文を持つ。OCR入力追加に合わせて要約prompt versionを更新する。要約のresult schemaは変えない。

OCRの失敗、無視、未実行を要約エラーにしない。保存済みの完了OCRだけを利用し、無ければ本文だけで要約する。

### 9. Sourceノートの完全UUIDファイル名

Sourceノートの出力名を次の形に変更する。

```text
{sanitizeしたタイトル先頭60文字} - {完全なresource_id}.md
```

例:

```text
記事タイトル - 019ff551-1234-7abc-8123-456789abcdef.md
```

UUIDv7を切り詰めたり、別のhashへ変換したりしない。DBとの対応を目視でき、DBが持つ一意性をそのままファイル名へ移せるためである。完全なIDを使っても、現在の60文字のタイトル上限と合わせたファイル名要素は一般的なWindowsの255文字制限内に収まる。

書き込み前に全current `source_note`の出力予定を組み立て、次を検証する。

1. `resource_id`がUUIDとして妥当である。
2. 同じ`resource_id`が複数のcurrent `source_note`へ現れない。
3. sanitize後の完全な出力パスが全件で一意である。
4. 出力先の既存ファイルが、同じ`resource_id`のFeedian管理Sourceノートであるか、存在しない。

異なる`resource_id`が同じ予定パスになる場合は書き込みを開始せず失敗する。完全なUUIDを使用するため通常は起きないが、一意性を暗黙の前提にせず検証する。

### 10. 旧短縮名からの移行

Sourceノート生成場所と呼び出しタイミングは変えない。従来どおり`ingest`の最後に`render_source_notes`が全current `source_note`を書き出す。その処理へ旧短縮名の照合と整理を追加する。

移行手順は次の順序とする。

1. Source folder内のFeedian管理Sourceノートをfrontmatterの完全な`resource_id`で索引化する。
2. DBの全current `source_note`について完全UUIDの予定パスを作る。
3. 全予定パスの一意性と既存ファイル競合を検証する。
4. DBの`source_note.markdown`から全予定ファイルを一時ファイルへ書き、同じdirectory内でatomicに置換する。
5. 出力後のfrontmatter `resource_id`集合がDBのcurrent `source_note.resource_id`集合と一致することを検証する。
6. 同じ`resource_id`の完全UUIDファイルが存在することを確認してから旧短縮名を整理する。

旧ファイルを削除できるのは、次を全て満たす場合だけとする。

- `feedian_managed: true`かつ`feedian_kind: source`である。
- frontmatterの`resource_id`がDBのcurrent `source_note`に存在する。
- ファイル本文が、そのresourceのcurrent `source_note.markdown`とbyte単位で一致する。
- 完全UUID名の新ファイルが同じ内容で存在する。

条件を満たさない旧ファイルは削除・上書きせず、競合として報告する。特に、利用者が編集したファイル、frontmatterを読めないファイル、DBに対応するcurrent noteが無いファイルは保護する。

既に短縮名の衝突でファイルが失われたresourceは、旧ファイルから復元しない。正本であるSQLiteの`source_note.markdown`から完全UUID名へ再生成する。既存ファイルの更新時刻を「新しさ」の判定に使わない。

競合が1件以上あれば非0で終了し、件数とパスを表示する。競合しないresourceの完全UUIDファイルは生成してよいが、旧ファイルの一括整理は集合照合が成功した範囲だけに限定する。

### 11. データ保持と再実行

- OCRは派生データだが、現在の要約が参照した原文であるため、採用した`ocr_text`と`llm_run`をsnapshot対象のSQLiteに含める。
- 説明画像のpayloadもsnapshot対象になる。対象外画像はbytesを保存しない。
- 同じ画像SHA-256と同じ解析入力指紋では完了結果を再利用する。
- modelまたはpromptを変えた場合は入力指紋が変わり、次の`enrich-images`で再解析対象になる。
- 再解析が失敗しても、既に完了したOCRを先に消さない。新しい結果が完了したtransactionで現在値を置き換える。画像bytes自体が変わった場合だけ、旧OCRを要約へ使わないため`pending`へ戻す。
- `resource_revision.content_markdown`とSourceノートに保存済みの本文を、OCR失敗によって削除または空文字へ置換しない。

### 12. 進捗と報告

`enrich-images`の終了行は少なくとも次を表示する。

```text
resources=<件数>
images=<候補数>
completed=<説明画像として完了した件数>
ignored=<対象外画像件数>
reused=<解析結果再利用件数>
failed=<失敗件数>
input_tokens=<入力token数>
output_tokens=<出力token数>
cost_usd=<既知なら合計、未知ならunknown>
```

1記事内の画像を並列処理しても、集計は画像結果をmain threadで回収した時点で行う。失敗内容にはresource IDと画像URLを含めるが、API key、画像bytes、base64、private pathを含めない。

Sourceノート生成は、少なくとも次を報告する。

```text
source_written=<新規・更新件数>
source_skipped=<同内容件数>
source_migrated=<旧短縮名を整理した件数>
source_conflicts=<保護した競合件数>
```

### 13. 検証

#### 画像抽出と取得

1. 記事領域内の通常画像だけが候補になり、広告、pixel、data URLが除外される。
2. `src`、lazy-load属性、`srcset`から選んだURLが従来どおり正規化される。
3. private host、DNS rebinding、private addressへのredirectを拒否する。
4. byte上限、pixel上限、timeout、非画像MIME、壊れた画像を画像単位の失敗として扱う。

#### 分類とOCR

5. 表、図、UI screenshotでは`explanatory`とOCR原文を保存する。
6. 写真、装飾イラスト、icon、logo、広告では`ignored`となり、OCR原文とpayloadを保存しない。
7. 構造化出力の未知field、未知の`image_kind`、型違反をprotocol errorとして記録する。
8. 分類とOCRが1画像1requestである。
9. 全resourceを合計した同時実行数が`image_ocr.workers`とbackend上限の小さい方を超えない。
10. 画像が少ない複数resourceのtaskが共有executorの空き枠を利用でき、記事ごとのpoolを作らない。
11. worker threadからSQLiteを呼ばない。
12. 画像1件の失敗後も同じresourceの他画像と後続resourceを処理する。

#### 保存と再利用

13. fresh schemaとversion 9からのmigration後schemaが一致する。
14. syncで同じ画像URLが残った場合に`resource_image_id`と完了OCRを維持する。
15. 画像SHA-256が変わった場合だけ旧OCRを要約対象から外す。
16. 同じ入力指紋では外部requestをせず結果を再利用する。
17. 説明画像だけが`payload`と`asset`へ保存される。
18. OCR再解析失敗時に直前の完了結果を失わない。

#### ingest

19. 本文とposition順のOCR原文が別のuntrusted blockとしてrequestへ入る。
20. OCRを含む実requestでtoken見積りと入力fingerprintを計算する。
21. OCR追加・変更後は旧要約を再利用しない。
22. OCR未実行、対象外、失敗、空OCRだけのresourceは本文だけで従来どおり要約できる。
23. 要約結果のschemaと日本語出力規則は変わらない。

#### Sourceファイル名と移行

24. 新しいSourceファイル名が完全な`resource_id`を含む。
25. 同一タイトルかつ近接時刻に生成した複数UUIDv7が別ファイルへ出力される。
26. DBのcurrent `source_note.resource_id`集合と出力後のSourceファイルのID集合が一致する。
27. 旧短縮名で上書きされていた複数ノートがDBから全件復元される。
28. 内容一致する旧短縮名だけが、新しい完全UUID名の存在確認後に整理される。
29. 編集済みファイル、別resourceのファイル、解析不能なファイルを保護し、競合として報告する。
30. 予定パスが既存の別resourceファイルと衝突する場合、上書きしない。

#### 回帰

31. `feedian sync`がLLMを呼ばない。
32. `feedian ingest --dry-run`が画像取得、OCR、DB書き込みを行わない。
33. OCRを一度も実行していないVaultで、sync、ingest、snapshot、restoreが従来どおり動く。
34. `python -m pytest -q`が成功する。
35. schema変更を含むため、実Vaultのcopyで`migrate`、`PRAGMA quick_check`、`PRAGMA integrity_check`、snapshot作成とrestoreを確認する。

### 14. 実装順序

1. schema version 10と`resource_image`の差分更新を実装する。
2. 画像取得policy、構造化出力、画像解析backend契約を実装する。
3. `enrich-images`のplanning、scheduler、並列実行、監査保存を実装する。
4. 保存済みOCRをingest requestへ追加し、prompt versionとtoken見積りを更新する。
5. Sourceファイル名を完全UUIDへ変更し、事前検証、再生成、旧短縮名整理を実装する。
6. `DESIGN.md`へ確定仕様の要約と本仕様へのリンクを追加する。
7. 参照Vaultのcopyで件数照合と移行を検証してから実Vaultへ適用する。

実装commitにはコード、schema migration、テスト、`DESIGN.md`の更新を含める。仕様が確定するまでは実装を開始しない。

## レビュー

### レビュー1 — Claude Code (2026-08-25)

草案の記述をコードへ突き合わせた。事実確認としては、schema version 9が現在値であること（`_migrate_v8_to_v9`が最後、`feedian/store.py:136-170`）、`resource_image`が既に`UNIQUE(resource_id, source_url)`を持つこと（`feedian/store.py:1518-1526`）、UUIDv7先頭8文字が約65.536秒の窓に対応すること、9,132 − 7,291 = 1,841という算術は、いずれも草案のとおりである。

以下は指摘のみを記す。採否は`最終案`で決める。

#### 高 — 1. §5と§6で入力指紋の定義が矛盾している

§5（L192-200）は入力指紋に`resource_revision_id`を含めると書き、§6（L216）は「SHA-256、backend、model、prompt version、schema versionが一致すれば、resource revisionが変わっても画像解析結果を再利用できる」と書く。両立しない。`resource_revision_id`が指紋に入る限り、本文が1文字変わって新しいrevisionが生まれるたびに全画像が再解析対象になる。これは§6の再利用規則が防ごうとしている費用そのものである。

加えて、既存の再利用検索`successful_llm_result()`（`feedian/store.py:1048-1077`）は`resource_revision_id`を検索キーにしている。`operation='image-ocr'`をこの経路へ載せると、同じ制約を自動的に引き継ぐ。

指紋から`resource_revision_id`を外し、再利用の正本を`llm_run`ではなく§5が既に用意している`resource_image.analysis_input_fingerprint`に置くのが素直である。`llm_run`は監査記録として`resource_revision_id`を持ってよいが、再利用判定には使わない。

#### 高 — 2. §6のorphan payload規則が逆向きになっている

`delete_orphan_payloads()`（`feedian/store.py:1361-1377`）は既に`SELECT payload_id FROM asset`を生存集合に含んでいる。§6最終項の「参照集合へ`resource_image.asset_id`経由のpayloadを含める」は、生存集合をさらに広げる指示であり、削除を可能にするどころか永久に生き残らせる。

必要なのは逆で、`resource_image`行を削除したとき、またはSHA-256が変わって旧解析結果を捨てるときに、対応する`asset`行も削除することである。`asset`は`resource_image`を参照していないので、行を消しても自動的には切れない。現状の文面のまま実装すると、記事から消えた画像のbytesがDBに残り続ける。

#### 高 — 3. 画像解析のbackend契約が定義されていない

§4は「`BackendCapabilities`へ画像解析対応の宣言を追加する」としか書いていない。しかし現在のbackend protocolは`summarize(model, item, page, language, ...)`（`feedian/llm_backends.py:132-229`）という文字列専用の契約で、画像bytesを渡す口が無い。新しいmethod名、引数（bytes / MIME / base64のどれを渡すか）、戻り値の型、`BackendAudit`との対応が決まっていないため、§14の手順2は現状の文面では着手できない。

併せて、既定backendの扱いを明記したい。API keyを持たない利用者が実際に走らせるのは`codex-local`と`claude-code-local`であり、どちらも`max_parallelism`が既定の1のままである（`feedian/llm_backends.py:307-314`, `630-657`、既定値は`BackendCapabilities`の`feedian/llm_backends.py:92`）。初版で画像解析を宣言するbackendが`openai-responses`だけなら、`image_ocr.workers=8`は既定構成では意味を持たない。「どのbackendが初版で対応するか」を仕様に書くべきである。

#### 高 — 3b. §6の差分更新が、抽出0件のときに完了済みOCRを消す

`_store_page`は`extract_content_images`の戻り値をそのまま`replace_resource_images`へ渡し、防御は`if not html: return`（`feedian/sync.py:587`）だけである。HTMLは取れたが記事領域のselectorが外れて抽出0件、という状態は現在も起こり得るが、今は「画像行が消えて次のsyncで戻る」だけで実害が無い。

§6の「現在のHTMLから消えたURLは削除する」を素直に実装すると、この状態がそのresourceの全OCR行とasset行の恒久的な削除になる。サイトのmarkupが変わっただけで、既に取得済みのOCR原文が失われる。`AGENTS.md`の「既に保存した本文を落とすのは常にバグ」に該当する。

削除は「抽出結果が1件以上あるとき」に限る、という条件を仕様へ書くべきである。

#### 高 — 3c. §6のSHA-256変更規則が到達不能である

「URLが同じでも、再取得した画像SHA-256が変われば現在のOCR値を消して`pending`へ戻す」（§6）は、再取得しないと判定できない。しかし§7の既定対象は`pending`・`failed`・指紋不一致の画像だけである。`completed`の行は再取得されないので、リモートの画像が差し替わってもこの規則は`--force`以外では一度も発火しない。

規則を残すなら再取得の契機（有効期限、`--recheck`のような選択肢）を定め、残さないなら§6から削って`--force`の挙動として書き直す。

#### 高 — 4. §10の集合一致検証と「保護」が両立しない

手順5は「出力後のfrontmatter `resource_id`集合がDBのcurrent `source_note.resource_id`集合と一致すること」を求める。一方L307は「DBに対応するcurrent noteが無いファイル」を保護する、つまり残すと書く。保護したファイルが1件あれば集合は永久に一致せず、L311により`ingest`は毎回非0で終了する。解消しない条件で恒久的に失敗する。

さらに、`ingest`の終了コードは現状`1 if report.failed else 0`（`feedian/cli.py:754`）で、`render_source_notes`は`(written, skipped)`しか返さない（`feedian/ingest.py:800-824`）。競合件数がどう終了コードへ届くかも未定義である。

方向としては、(a) 検証をDB集合 ⊆ ディスク集合へ緩める、(b) 「書き込みを止める競合」と「報告するだけの競合」を分ける、の二点を仕様で切り分ける必要がある。利用者が編集したファイルの存在は、正常系として扱うべき状態であって、恒久的なエラーではない。

#### 高 — 4b. §10の「byte単位で一致」が現行の比較方法と食い違う

L304は旧ファイル削除の条件を「ファイル本文が…byte単位で一致する」とするが、現行の同内容判定は`path.read_text(encoding="utf-8") == document`（`feedian/ingest.py:818`）で、Pythonのuniversal newlinesによりCRLFはLFへ正規化されて比較される。

Vaultはgit repositoryであり（`feedian/snapshots.py`のsnapshotはVault rootでcommit・tag・pushする）、`core.autocrlf`が効く環境でcheckoutされたSourceノートはCRLFになる。`feedian restore`はSQLiteだけを戻すのでMarkdownはcheckout由来のままである。この状態では、現行の比較は一致と判定し、§10の「byte単位」は不一致と判定する。

結果として、正常なVaultの旧ファイルが全件「保護すべき競合」になり、指摘4の恒久的な非0終了が現実に起きる。比較規則を現行と同じ改行正規化つきに揃えるか、書き出しが常に`newline="\n"`である（`feedian/ingest.py:822`）ことを前提に、比較前に改行を正規化すると明記する必要がある。

#### 中 — 5. §8が信頼できない値をタグ属性へ埋めている

`<untrusted_image_ocr position="0" source="https://example.test/diagram.png">`の`source`は攻撃者が制御できる文字列である。URLは`"`や`>`を含み得て、`urljoin`はそれを保存する。属性から抜け出して`</untrusted_image_ocr>`を偽造できれば、このブロックが作ろうとしている境界そのものが無効になる。`ocr_text`本文が閉じタグ文字列を含む場合も同じである。

属性を使わない（順序はブロックの並びで表現できる）か、escape規則を明示するかを決める必要がある。

#### 高 — 6. §8の切り詰めが`build_untrusted_message`の不変条件を壊す

`build_untrusted_message()`（`feedian/llm.py:444-452`）は、message全体が長すぎるときに末尾を切り、固定の`\n[Source text truncated.]\n</untrusted_page_text>`を付けて閉じタグを保証している。`build_manus_message()`のdocstring（`feedian/llm.py:434-440`）はこれを不変条件として明記している——「truncation keeps the closing tag, so the untrusted block can never be left open for the reminder to fall inside」。

本文の後ろにOCRブロックを並べると、切り詰めはOCRブロックの内部を切り、別のタグの閉じタグを付ける。`<untrusted_image_ocr>`が開いたまま、後続の指示文（`UNTRUSTED_INPUT_REMINDER`）がその内側へ落ちる。守るために書かれた不変条件が、まさにその形で破れる。

これは稀な事象ではなく既定で起きる。`MANUS_MAX_MESSAGE_CHARS`は4,500（`feedian/llm.py:18`）、`manus-api`の`max_article_chars`は3,000（`feedian/llm_backends.py:1082`）で、§2はOCRに10,000文字を割り当てている。本文が上限近くまであれば、OCRブロックは常に切り詰められる。

仕様として、(a) OCRの切り詰めは画像ブロック単位で落とす（ブロックの途中で切らない）、(b) OCR枠はbackendの上限から導出し固定値にしない、の二点を書くべきである。

#### 中 — 7. §5・§11の画像bytes保存は、v2で意図的に外した判断を戻している

`_migrate_v1_to_v2`のdocstringは「downloaded image bytes are deliberately removed」と明記し（`feedian/store.py:1638-1643`）、`DELETE FROM asset`を実行している（`feedian/store.py:1742`）。以来`asset`は空のままである。

草案はこれを戻すが、戻す理由が書かれていない。1resourceあたり8画像×20 MiBの上限で、しかも§11によりsnapshot対象に入る。

snapshotの構造が決定的である。`create_snapshot`はSQLite全体をbackupし、7zで1つのarchiveへ固め、private GitHub Releaseのassetとして公開し、さらに再downloadして検証する（`feedian/snapshots.py:80-118`）。PNG/JPEGは7zでこれ以上縮まないので、画像bytesはDBの増分がそのままarchiveの増分になり、snapshotのたびに全量を再度圧縮・upload・downloadすることになる。GitHub Releaseのasset上限は2 GiBで、これは遠い数字ではない。

`AGENTS.md`のデータ整合性条項はこの判断を縛らない。保存しなかったbytesは「保存済みデータ」ではなく、画像はURLから再取得できる。一方で「最後の数%を詰めるための機構を作らない」は正面から当たる。

**初版からpayload/asset保存を落とすことを推奨する。** SHA-256、分類、OCR原文、`llm_run`だけを残せば、再解析はURLからの再取得で足りる。これは指摘2も同時に消す。残すのであれば、保存用の別の（はるかに小さい）byte上限と、snapshot容量への影響の見積りを仕様へ書くべきである。

#### 中 — 8. `use_for_summary`に消費者がいない

§4は構造化出力に`use_for_summary`を含めるが、§5には対応する列が無く、§8の抽出条件は`image_kind = explanatory`かつ`ocr_text`非空である。どこからも読まれないfieldは必ず実装とずれる。落とすか、保存してこれを§8の判定条件にするかを決める。

#### 低 — 9. §3の`reused`が§5の`analysis_status`に存在しない

§3（L124）は終端状態として`completed` / `ignored` / `failed` / `reused`の4つを挙げるが、§5の`analysis_status`列挙は`pending` / `completed` / `ignored` / `failed`である。`reused`は実行結果の分類であって保存状態ではない、と明記しないと、実装者は5つ目のstatusを足し、§12の`reused=`を誤った場所から数える。

#### 低 — 10. §2の`max_bytes`の単位が未定義

§2 L105は全設定値を「正の整数として検証」するとしながら、`image_ocr.max_bytes`の既定値だけ`20 MiB`と単位付きで書いている。byte数（`20971520`）と書くか、接尾辞を解釈すると書くかを決める。

#### 低 — 11. §9のWindows文字数制限の根拠が正しくない

255はパス要素1つの上限であり、Windowsで実際に効くのはlong path未有効時の260文字のフルパス上限である。ファイル名は約102文字（タイトル60 + ` - ` + UUID 36 + `.md`）なので実害は考えにくいが、根拠として書くならVault側のパス長の余裕を示すべきである。

#### 低 — 12. §10手順1の走査範囲を絞れる

現行の`render_source_notes`は既に全noteを`read_text`して同内容判定をしている（`feedian/ingest.py:818`）ので、folder全体を読むこと自体は新しい費用ではない。増えるのは、DBの予定集合に無いファイル——移行完了後は基本的に0件——の分だけである。完全UUID名を持たないファイルへ走査を絞れば、移行後の追加費用をほぼ消せる。

#### 低 — 13. §13にCtrl-C時の検証項目が無い

§3はCtrl-C時の挙動（未開始taskのcancel、開始済み実行の失敗終端）を定めているが、§13にそれを確かめる項目が無い。中断が`pending`のまま残らないこと、あるいは失敗終端すること、いずれにせよ次回実行で正しく再開されることを検証項目に加えるべきである。

#### 低 — 14. §10手順4が同内容skipを消すが、§12は`source_skipped`を報告し続ける

手順4は「全予定ファイルを一時ファイルへ書き、同じdirectory内でatomicに置換する」と無条件の書き出しを指示するが、現行は内容が同じなら書かずにskipする（`feedian/ingest.py:818-820`）。§12の報告項目には`source_skipped=<同内容件数>`が残っている。毎回9,000件を書き直すのか、skipを維持するのかを決め、報告項目と揃える。

#### 低 — 15. タイトル変更で旧い完全UUID名のファイルが残る

ファイル名は`{タイトル} - {resource_id}.md`なので、要約の`note_title`が変わると同じ`resource_id`に対して別のパスが生まれる。§10の整理対象は「旧短縮名」だけなので、完全UUID名どうしの旧ファイルは残り続ける。移行後に恒久的に発生する状態であり、整理対象を「同じ`resource_id`を持つ、予定パス以外のFeedian管理Sourceノート」と定義し直すのが素直である。

#### 同意する判断

- 記事ごとにexecutorを作らず共有枠にする方針（§3）。
- `sync`をLLM非依存に保ち、画像解析を明示commandへ分ける方針（全体フロー）。
- OCR原文を`resource_revision.content_markdown`へ混ぜない判断（§5）。原文と派生データの境界が保たれる。
- 上書きで失われたノートを旧ファイルから復元せず、正本のSQLiteから再生成する判断（§10）。更新時刻を新しさの判定に使わないことも含めて正しい。
- 差分更新のキーを`(resource_id, source_url)`とする判断（§6）。既存の`UNIQUE(resource_id, source_url)`制約とそのまま一致する。

### レビュー2 — Codex (2026-08-25)

#### 結論

レビュー1は実装との照合が正確であり、主要な指摘を採用する。特に、画像解析のbackend契約、OCR入力の安全な切り詰め、再利用境界、Sourceファイル移行時の編集保護は、確定前に決めなければ実装へ着手できない。

初版では説明画像のbytesを`payload` / `asset`へ保存しない。保存対象は画像SHA-256、分類、OCR原文、採用した`llm_run`とする。画像bytesを保存しなくても、利用目的である要約入力は満たせる一方、SQLite、snapshot、upload、restoreへ永続的な容量負担を加えずに済む。この判断により、レビュー1の指摘2にあるorphan asset問題も初版の対象から外れる。

レビュー1末尾の「採否は`最終案`で決める」という記述だけでは、各指摘を受け入れたかが文書に残らない。本節で採否と理由を記録し、最終案を作る場合はこの結論を反映する。草案本文は履歴として変更しない。

#### レビュー1の採否

| 指摘 | 採否 | 理由 |
|---|---|---|
| 1. §5と§6の入力指紋の矛盾 | 採用 | `resource_revision_id`を画像解析の入力指紋と再利用条件から外す。監査用の`llm_run.resource_revision_id`には残してよい。再利用の正本は`resource_image.analysis_input_fingerprint`とし、画像SHA-256、alt、backend、model、prompt version、result schema versionで決める。 |
| 2. orphan payload規則が逆 | 採用 | 現行の`delete_orphan_payloads()`は既に`asset.payload_id`を生存集合へ含めており、草案の規則では削除できない。初版では画像bytesとasset自体を保存しないため、この削除規則と`resource_image.asset_id`列を設けない。 |
| 3. 画像解析backend契約が未定義 | 採用 | `analyze_image`に相当するmethod名、入力型、戻り値、監査情報、初期対応backendを確定仕様で定義する。画像bytesとMIME typeをworkerへ渡し、構造化された分類・OCR結果と`BackendAudit`相当の監査値を返す契約が必要である。対応しないbackendはrequest開始前に失敗させる。既定並列数8を実際に使うには、初期対応backendとその`max_parallelism`も明記しなければならない。 |
| 3b. 抽出0件で完了済みOCRを消す | 採用 | HTMLが存在しても画像抽出0件は「画像が無い」ことの十分な証拠ではない。抽出0件では既存`resource_image`とOCRを削除しない。1件以上を信頼して抽出できた場合だけ差分削除を行う。 |
| 3c. SHA-256変更規則へ到達できない | 採用 | 初版では`completed`画像を自動再取得しない。リモート画像の差し替え検知は`--force`時だけ行い、そのときSHA-256が変われば旧OCRを無効化する。TTLや`--recheck`は追加しない。 |
| 4. 集合一致検証と保護が両立しない | 修正して採用 | DBから生成したcanonical Sourceファイル集合はDBのcurrent `source_note`集合と完全一致させる。編集済み・孤立・解析不能な保護ファイルはcanonical集合から分けて報告する。単純な「DB集合 ⊆ ディスク集合」では欠落を検出できないため採らない。書き込みを妨げる予定パス競合と、予定パス外で保護する追加ファイルを別の結果として扱う。 |
| 4b. byte一致と改行変換が食い違う | 採用 | UTF-8 textとして読み、LF / CRLFを正規化してから比較する。checkout時の改行変換だけで編集済み競合にしない。 |
| 5. 信頼できない値をタグ属性へ埋める | 採用 | URLを擬似XML属性へ直接展開しない。画像順、URL、OCRを安全にserializeし、URLとOCR内のdelimiter文字が入力境界を閉じられない形式にする。 |
| 6. OCR切り詰めがuntrusted blockを開いたままにする | 採用 | OCR文字数を固定の10,000文字として本文上限へ加算しない。backendのmessage上限から本文・固定指示を引いた残量をOCR予算とし、画像ブロックを途中で切らず、収まる完全なブロックだけを追加する。全体のuntrusted入力は必ず既存の安全な閉じ方を通す。 |
| 7. 画像bytes保存が過去の容量判断を戻す | 採用 | 初版から`payload` / `asset`保存とsnapshot対象化を外す。画像SHA-256、分類、OCR原文、監査履歴だけを保存し、再解析が必要ならURLから再取得する。説明画像の永続保存が必要になった時点で、保存上限とsnapshot費用を別仕様で判断する。 |
| 8. `use_for_summary`に消費者がいない | 採用 | `use_for_summary`をresult schemaから削除する。`image_kind='explanatory'`かつ非空の`ocr_text`を要約入力条件の唯一の正本とする。 |
| 9. `reused`が保存状態に存在しない | 採用 | `reused`はcommand実行時の集計区分であり、`analysis_status`へ追加しない。再利用した行の保存状態は元の`completed`または`ignored`のままとする。 |
| 10. `max_bytes`の単位が未定義 | 採用 | config値はbyte数の整数とし、既定値を`20,971,520`と定義する。`MiB`接尾辞の解析は追加しない。 |
| 11. Windows文字数制限の根拠が不正確 | 修正して採用 | 255文字はpath componentの上限であり、フルパス全体の保証にはならない。完全UUIDを使う判断は維持し、書き込み前に実際の予定パスが実行環境で作成可能かを検証する。特定のWindows上限値を仕様の根拠にしない。 |
| 12. §10手順1の走査範囲を短縮名へ絞る | 不採用 | 移行後もresource titleの変更により、完全UUIDを持つ旧パスが残り得る。同じ`resource_id`を持つ予定パス外の管理ファイルを探す必要があるため、Feedian管理Sourceファイルの索引化は継続する。単なる全文再読を避ける実装上の最適化は、正しさを維持できる範囲で別途行ってよい。 |
| 13. Ctrl-C時の検証が無い | 採用 | 未開始taskのcancel、開始済み`llm_run`の失敗終端、強制終了後の次回回収、次回実行での再開を検証項目へ加える。 |
| 14. 無条件atomic置換と`source_skipped`が矛盾 | 採用 | 現行どおり正規化後の内容が同じファイルはskipする。新規・変更ファイルだけを同一directoryの一時ファイルからatomicに置換し、`source_skipped`を維持する。 |
| 15. タイトル変更で旧い完全UUID名が残る | 修正して採用 | 結論は正しいが、現行のファイル名はLLMの`note_title`ではなく`resource_revision.title`から作る。page titleが更新されると同じ問題が起きるため、整理対象を「同じ`resource_id`を持つ、現在の予定パス以外のFeedian管理Sourceノート」とする。 |

#### 追加指摘1 — `image_ocr` configの保存形式が未定義 — 重大度: 高

草案は`image_ocr.workers`などをtop-level設定のように参照するが、現行`VaultConfig`はtop-levelの未知fieldを拒否し、`render_vault_config`も`image_ocr`を書き出さない。`VaultConfig`上の型、JSON内の配置、parse、render、config format version、既存configのmigrationを確定仕様へ含める必要がある。

採否: **採用**。`image_ocr`を独立したtop-level objectとして追加し、専用dataclassで全fieldを厳密に検証する案を第一候補とする。既存configが明示的migrationを必要とするか、既定値で補える同一format内の追加とするかも最終案で決める。

#### 追加指摘2 — 再解析失敗時の現在値が矛盾している — 重大度: 高

§5は解析失敗時に`analysis_status='failed'`かつ空OCRを保存すると定める一方、§11は再解析失敗時に以前の完了OCRを維持すると定める。1行の`resource_image`で両方を同時に表現できない。

採否: **採用**。同じ画像SHA-256に対する再解析が失敗した場合は、`resource_image`の現在値を`completed`のまま変更せず、新しい失敗だけを`llm_run`とcommand集計へ記録する。`--force`で再取得した画像SHA-256が変わった場合は旧OCRを使えないため、旧現在値を外して`failed`または`pending`へ遷移する。現在採用中の結果と最新attemptの結果を同じstatusへ押し込まない。

#### 追加指摘3 — 予定パス上の編集済みファイルを上書きできる — 重大度: 高

§9の事前検証は、出力先の既存ファイルが同じ`resource_id`のFeedian管理Sourceノートなら許可する。そのファイルが利用者に編集され、DBのcurrent `source_note.markdown`と異なっていても、§10のatomic置換で上書きできる。旧短縮名だけでなく、現在の完全UUID予定パスそのものにも編集保護が必要である。

採否: **採用**。予定パスに同じ`resource_id`のファイルがあっても、改行正規化後の内容がDBと異なる場合は書き込みを止める競合とする。予定パス外の編集済みファイルは保護して報告し、canonical出力の生成を妨げない。

### レビュー3 — Claude Code (2026-08-25)

#### 結論

レビュー2の追加指摘1・2・3はいずれも実在する。3件ともレビュー1が落としていたものであり、指摘としては正しい。

ただし**追加指摘3の採否内容は`render_source_notes`の主機能を壊す**。そのまま実装すると要約を更新したSourceノートが二度と書き出されない。ここは確定前に決め直す必要がある。

加えて、採用済みの採否のうち数件は、実装に必要な前提が欠けているか、判断を保留したままである。採否3cは採否1・§7・§11 L318と噛み合っておらず、採否4はレビュー1の指摘4の後半（終了コードへの経路）を扱っていない。

#### 高 — A. 追加指摘3の採否が、Sourceノートの正常な更新経路を競合にしてしまう

採否は「予定パスに同じ`resource_id`のファイルがあっても、改行正規化後の内容がDBと異なる場合は書き込みを止める競合とする」とする。

しかし「ディスクの内容がDBと異なる」は、要約を作り直したあとの**正常な状態そのもの**である。`ingest`は`put_source_note`でmarkdownを更新し、その直後に`render_source_notes`が走る（`feedian/cli.py:740-746`）。このとき予定パスのファイルは必ず「DBと異なる」。採否をそのまま実装すると、要約が更新された全ノートが競合になり、Sourceノートの更新が止まる。

さらに悪いことに、区別する材料がDBに無い。`put_source_note`（`feedian/store.py:1217-1246`）は既存行を**その場でUPDATE**し、`superseded_at`をNULLへ戻す。過去のmarkdownは保存されない。したがって「Feedianが前回書いた内容」と「利用者が編集した内容」を、ディスク上の内容だけから見分けることはできない。

さらに、この採否は採否4がせっかく解いた問題を戻す。予定パスのファイルを保護すると、そのresourceはcanonical集合から外れる。採否4は「canonical集合はDB集合と完全一致」を求めるので、一致は崩れ、レビュー1の指摘4が挙げた恒久的な非0終了が復活する。

最終案は次のどちらかを選ぶ必要がある。

- **(a) 予定パスは現行どおり上書きする。** すなわち、予定パスが存在しないか、同じ`resource_id`を持つ`feedian_managed`なSourceノートである場合は内容を比較せず上書きし、それ以外（別resourceのファイル、frontmatterを読めないファイル、管理外のファイル）だけを書き込みを止める競合とする。§9の検証4はそのまま残る。これは現行の挙動（`feedian/ingest.py:818-822`）であり退行ではない。追加指摘3が指す危険は、§10が新しく導入する**削除**の側にあり、上書きの側は以前からこうだった。
- **(b) 保護するなら新しい状態を作る。** Feedianが最後に書いた内容のhashを、frontmatter（例: `feedian_content_sha256`）かDBの新しい列として持ち、それと一致するファイルだけを上書き可能とする。新機構なので、必要と判断するなら仕様へ明記して実装対象に含める。暗黙には成立しない。

**(a)を推す。** 理由は三つある。

1. 手編集は既にgitで復元できる。`source_folder`はsnapshotのたびにcommit対象へ入る（`feedian/snapshots.py:223-224`）。`feedian_content_sha256`は、gitが既に果たしている役割を二重に持つ機構になる。`AGENTS.md`のデータ保全条項も、この意味で(a)を妨げない。
2. 保護は自己修復を妨げる。書き込みが中断して壊れたファイルが予定パスに残った場合、それは内容がDBと異なるので(b)では競合として保護される。最も上書きしてほしいケースが、最も固く守られることになる。
3. 利用者がFeedian管理folderのSourceノートを手編集する運用は想定されておらず、そのために新しい状態を持ち込むのは`AGENTS.md`が言う「最後の数%を詰めるための機構」に当たる。

#### 高 — B. 採否6の「backendのmessage上限」が`BackendCapabilities`に存在しない

採否6は「backendのmessage上限から本文・固定指示を引いた残量をOCR予算とする」とする。方針は正しいが、その上限を読む先が無い。

`BackendCapabilities`（`feedian/llm_backends.py:85-93`）が持つのは`max_article_chars`だけで、message全体の上限を表すfieldは無い。実際の上限`MANUS_MAX_MESSAGE_CHARS = 4500`は`feedian/llm.py:18`のmodule定数であり、`build_manus_message`の内側からしか参照されない（`feedian/llm.py:441`）。

採否6を実装するには、message予算を`BackendCapabilities`へ足し、`build_untrusted_message`の呼び出し側がbackendごとの値を渡す形へ変える必要がある。これは§14の手順4に含まれる前提であり、最終案に書いておかないと実装時に見落とされる。

#### 低 — C. 採否7でbytesを保存しないなら、§3のworker戻り値も直す必要がある

§3 L126はworkerの戻り値に「保存対象画像のbytes」を含めている。採否7で`payload` / `asset`保存を落とすなら、bytesをmain threadへ返す理由が無くなる。返し続けると、最大で並列数×`max_bytes`（8 × 20 MiB）を保存もしないデータのために保持することになる。

最終案では§3のworker戻り値からbytesを外し、画像URL、SHA-256、MIME type、分類、OCR原文、監査情報、警告だけとする。

#### 高 — D. 追加指摘2の採否が、再解析の無限retryを生む

採否は「再解析が失敗した場合は`resource_image`の現在値を`completed`のまま変更せず、新しい失敗だけを`llm_run`とcommand集計へ記録する」とする。現在値を守る点は正しい。

しかしその行の`analysis_input_fingerprint`は旧いままになる。§7の既定対象は「`pending`と`failed`、または現在の解析設定と入力指紋が一致しない画像」なので、modelを変えて再解析に失敗した行は、次回も指紋不一致で選ばれ、また失敗する。抑止が無く、実行のたびに外部requestの費用が発生し続ける。

この repository には先例がある。取得失敗には`fetch_capture.consecutive_failures`と terminal failure 判定があり、[fetchのretryと抑止](docs/specs/20260818-fetch-retry-suppression.ja.md)で決着している。画像解析にも失敗回数の記録と抑止規則が要る。最終案で決めるべきである。

#### 高 — D2. 採否3cが採否1・§7と噛み合わず、再解析の既定経路を塞ぐ

採否3cは「初版では`completed`画像を自動再取得しない」とする。しかし§7の既定対象には「現在の解析設定と入力指紋が一致しない画像」が含まれ、§11 L318は「modelまたはpromptを変えた場合は入力指紋が変わり、次の`enrich-images`で再解析対象になる」と定めている。model・prompt・altが変わった`completed`行を自動で再取得しないなら、これらの経路がすべて`--force`専用になり、§11 L318が空文になる。

採否3cが本来指していたのは「指紋が一致したままの行について、リモート画像の差し替えを検知するための再取得はしない」という、より狭い規則である。**指紋不一致の行は既定で再取得・再解析する**という区別を最終案で明示する必要がある。指摘Dと同じ軸の話なので、まとめて決めるのがよい。

#### 中 — E. 追加指摘1の採否が保留したconfig format versionについて

採否は「既存configが明示的migrationを必要とするか、既定値で補える同一format内の追加とするか」を最終案送りにしている。この repository には**両方の先例がある**ので、どちらを引くかを意識して決める必要がある。

- top-level objectの`llm`はformat_version 2で入った。`migrate_vault_config`のv1許可集合に`llm`は無く（`feedian/vault.py:309`）、`load_vault_config`のv2許可集合にはある（`feedian/vault.py:227-229`）。
- 一方`llm.workers`はformat 2の**内側**で追加されている。`_parse_llm`のコメントがそう明記している（`feedian/vault.py:337-338`）——「Added inside format version 2. A config written before this key existed takes the default, so no migration is required to open it.」

したがって「fieldを足したらversionを上げる」という単純な規則は存在しない。区別は top-level か nested かにある。

`image_ocr`は新しいtop-level fieldであり、`load_vault_config`がversionで門番をしているのはまさにtop-levelの許可集合である（`feedian/vault.py:227-232`）。format_versionを2のまま`image_ocr`を書き出すと、新しいFeedianが書いたconfigを古いFeedianが読んだときに、「Vault config format 3 is newer than this Feedian version」という正しい診断ではなく「Unknown vault config field(s): image_ocr」という誤誘導的なエラーになる。

**`VAULT_CONFIG_VERSION`を3へ上げることを推す。** `migrate_vault_config`へv2→v3の経路を足し、`render_vault_config`へ`image_ocr`を追加する。なお`llm.workers`の前例が示すとおり、`image_ocr`の内側にあとからkeyを足す分にはversionを上げなくてよい。

#### 中 — H. 採否4が指摘4の後半（終了コード）を扱っていない

レビュー1の指摘4は二つのことを言っていた。集合一致検証が保護と両立しないことと、競合件数が終了コードへ届く経路が未定義であることである。採否4は前者だけを扱っている。

現状、`render_source_notes`は`(written, skipped)`しか返さず（`feedian/ingest.py:800`）、`ingest`の終了コードは`1 if report.failed else 0`である（`feedian/cli.py:746, 754`）。一方§12は`source_conflicts=<保護した競合件数>`の報告を求める。最終案で、`render_source_notes`の新しい戻り値の形と、どの競合が終了コードを非0にするかを定義する必要がある。指摘Aで「書き込みを止める競合」と「報告するだけの競合」を分けるなら、非0にするのは前者だけになるはずである。

#### 低 — F. 採否11の「実行環境で作成可能かを検証する」が曖昧

9,132件の予定パスを実際に作ってみる検証は高価であり、しかも他processとraceする。解決済みの出力directoryとファイル名から決定的にパス長を計算すれば足りる。probeするのか計算するのかを最終案で明記する。

#### 低 — G. 採否1の再利用は`analysis_input_fingerprint`へindexを要求する

再利用の正本を`resource_image.analysis_input_fingerprint`に置くと、§12の`reused`が意味を持つのは「別のresourceに現れた同一画像を引き当てる」場合である。これは行を跨ぐ検索になる。planning時に候補分の指紋をまとめて1回引くか、この列にindexを置くかのどちらかが要る。§5の列定義か§7のplanning記述に添えるべきである。

#### 同意した点

- **追加指摘1・2・3はいずれも実在する。** 1は`load_vault_config`の許可集合（`feedian/vault.py:227-232`）と`render_vault_config`（`feedian/vault.py:252-283`）で確認した。2は§5 L206と§11 L319が同じ1行の現在値へ両立しない要求をしている。3は§9の検証4が同一`resource_id`のファイルを許可する以上、成立する。レビュー1はこの3件を落としていた。
- **採否4の前半を支持し、レビュー1の元案を取り下げる。** 「canonical集合はDB集合と完全一致させ、保護ファイルは別集合として報告する」は、私が出した「DB集合 ⊆ ディスク集合へ緩める」より良い。欠落を検出できる性質を保ったまま、恒久的な非0終了を避けられる。後半については指摘Hを参照。
- **採否12（指摘12の不採用）を受け入れる。** 指摘15を採用した以上、`resource_id`で予定パス外の管理ファイルを探す必要があり、索引化は続けざるを得ない。指摘12は実質的に指摘15へ吸収された。
- **採否7を支持する。** 初版で画像bytesを保存しない判断により、容量・snapshot・orphan asset の三つの問題が同時に消える。
- 採否3b・5・8・9・10・13・14・15は、いずれも理由づけを含めて妥当である。採否3cは方向は正しいが範囲が広すぎる（指摘D2）。

### レビュー4 — Codex (2026-08-25)

#### 結論

レビュー3は、レビュー2の採否が残した実装不能または矛盾を正しく指摘している。A、B、C、D、D2、E、Hを採用し、Fは修正して採用する。Gは、初版の再利用範囲を同じ`resource_image`行に限定するため不採用とする。

追加指摘3の問題自体は存在するが、レビュー2の採否は誤っていた。過去にFeedianが書いた内容をDBが保持していない以上、現在の予定パスにある同一resourceの管理ファイルについて、正常な要約更新と利用者編集を内容差だけで区別できない。初版では新しいhash状態を増やさず、管理対象の予定パスは従来どおり更新可能とする。慎重な保護が必要なのは、予定パス外の旧ファイルを**削除する側**である。

#### レビュー3の採否

| 指摘 | 採否 | 理由 |
|---|---|---|
| A. 追加指摘3の採否が正常なSource更新を競合にする | 採用 | レビュー3の案(a)を採る。予定パスが存在しないか、同じ`resource_id`を持つFeedian管理Sourceノートなら、同内容はskipし、内容が変わればatomicに更新する。別resource、管理外、frontmatter解析不能だけを書き込み停止の競合とする。最後に書いたhashを新たに保存する案(b)は、現行で想定していないSourceノート手編集のために状態を増やすので採らない。予定パス外の旧ファイルは、DB current markdownとの正規化後一致を確認できる場合だけ削除し、それ以外は保護する。 |
| B. backendのmessage上限がcapabilityに無い | 採用 | `max_article_chars`は記事本文の制限であり、固定指示・metadata・wrapperを含むmessage全体の上限ではない。`BackendCapabilities`へmessage全体の上限を表すfieldを追加し、画像OCRを含むrequest builderが参照できる契約にする。Manusの4,500文字をmodule内だけの定数にせずcapabilityへ接続する。OCRは残量へ完全な画像ブロック単位で追加し、最後にbackend固有wrapperの安全な切り詰めを通す。 |
| C. bytesを保存しないのにworkerがbytesを返す | 採用 | workerは取得した画像bytesを同じworker内の画像解析requestへ使い、戻り値には含めない。main threadへ返すのは画像URL、SHA-256、MIME type、分類、OCR原文、監査情報、警告だけとする。これにより最大`image_ocr.workers × image_ocr.max_bytes`の不要な結果保持を避ける。 |
| D. 再解析失敗が無限retryになる | 修正して採用 | 問題は存在するが、本文fetchと同じ指数backoff・終端判定までは導入しない。明示commandであるため、同じtarget fingerprintで一度失敗した画像は既定実行の対象から外し、`--force`だけが再試行する。現在採用中の`analysis_input_fingerprint`とは別に、最後に試したtarget fingerprint、status、時刻を保存する。旧完了OCRがある場合は採用中の現在値を維持し、失敗attemptだけを別状態と`llm_run`へ記録する。 |
| D2. 採否3cが指紋不一致の再解析まで塞ぐ | 採用 | 「自動再取得しない」の対象を、現在のtarget fingerprintと完了済みfingerprintが一致する行に限定する。model、prompt、result schema、altの変更でtarget fingerprintが変わった行は、既定で画像を再取得して再解析する。fingerprintが同じままリモート画像だけが差し替わったかを調べる再取得は`--force`時だけ行う。 |
| E. top-level `image_ocr`とconfig format version | 採用 | `image_ocr`は新しいtop-level objectなので`VAULT_CONFIG_VERSION`を3へ上げ、v2→v3 migrationを追加する。v3の許可集合、専用dataclass、parse、renderを同時に実装する。将来`image_ocr` object内へ既定値付きfieldを追加するだけなら、既存の`llm.workers`と同様にversionを上げなくてよい。 |
| H. Source競合の戻り値と終了コードが未定義 | 採用 | `render_source_notes`は`written`、`skipped`、`migrated`、`protected`、`blocking_conflicts`を持つreportを返す。予定パス上の別resource・管理外・解析不能は`blocking_conflicts`として`ingest`を非0にする。予定パス外で削除条件を満たさず保護した旧ファイルは`protected`として報告するが、canonical出力がDB集合と一致していれば終了コードを非0にしない。 |
| F. 実行環境で作成可能かの検証が曖昧 | 修正して採用 | 9,132件のprobe fileは作らない。sanitize後のpath component長と、実行環境で既知の決定的なpath制約を事前検証する。最終的な作成可否はatomic writeの結果で判定し、失敗したpathをblocking conflictとして報告する。書き込み失敗後に旧ファイルの削除へ進まない。 |
| G. `analysis_input_fingerprint`へのindexが必要 | 不採用 | 初版の`reused`は同じ`resource_image`行が持つ採用済みfingerprintとの一致を意味し、別resourceの同一画像を横断検索しない。planningは対象行を読むだけなのでfingerprint単独indexは不要である。画像bytesを保存せず、URLが異なる同一画像を共有する機構も設けない。将来、画像SHA-256単位のresource横断再利用を追加する場合に別途indexと正本tableを設計する。 |

#### レビュー2の追加指摘3に対する訂正

レビュー2の次の採否を取り下げる。

> 予定パスに同じ`resource_id`のファイルがあっても、改行正規化後の内容がDBと異なる場合は書き込みを止める競合とする。

訂正後は次のとおりとする。

- **現在の予定パス** — 同じ`resource_id`を持つFeedian管理Sourceノートは、DB current markdownが変わればatomicに更新する。現行の正常な要約更新と自己修復を維持する。
- **予定パス外の旧ファイル** — 正規化後の内容がDB current markdownと一致し、新しい予定パスが同内容で存在するときだけ削除する。一致しなければ保護して報告する。
- **予定パス上の別resource・管理外・解析不能ファイル** — 上書きせずblocking conflictとする。

この区別により、canonical Source集合をDBのcurrent `source_note`集合と一致させながら、不確かな旧ファイルの破壊だけを避ける。

### レビュー5 — Claude Code (2026-08-25)

#### 結論

レビュー4の採否はB、C、D2、E、H、Fの修正採用のいずれも妥当であり、Gの不採用——初版で画像SHA-256単位のresource横断再利用を作らない——も範囲を絞る判断として支持する。

**ただし採否Aは、前提が事実と異なる。** レビュー3が「新しい機構が要る」として案(b)を退け、レビュー4がそれを引き継いだが、その機構は既にこのrepositoryに実装されて動いている。レビュー3の誤りであり、下の指摘Nで訂正する。

なお本文書は779行に達し、まだgit外にある。`AGENTS.md`が言うとおり、この状態のレビュー議論には backup が無い。§9/§10（ファイル名と移行）は、参照Vaultで1,841件が既に衝突している現在の問題であり、OCR側のどの決定にも依存しない。切りのよい境界で確定させ、実装を先行させることを検討したい。

#### 高 — N. 案(b)の機構は既に存在する。採否Aの却下理由が成り立たない

レビュー3は追加指摘3への対処として案(a)と案(b)を挙げ、案(b)——「Feedianが最後に書いた内容のhashを frontmatter に持つ」——を「新機構なので暗黙には成立しない」と書いた。採否Aはこれを「現行で想定していないSourceノート手編集のために状態を増やす」として却下した。

**この前提が誤っている。** 同じ機構がRawノート側に既にあり、`feedian render`が使っている。

- `_with_render_hash`（`feedian/renderer.py:327-329`）が、frontmatterへ`render_hash: <sha256>`を書き込む。
- `_is_unchanged_generated_document`（`feedian/renderer.py:357-364`）が、hash行を除いた本文のSHA-256とhash行を突き合わせ、そのファイルが生成後に触られていないかを判定する。
- `_write_generated`（`feedian/renderer.py:344-345`）は、判定が偽——つまり人が編集した——なら`"conflict"`を返して上書きしない。
- `_index_managed_paths`（`feedian/renderer.py:391-407`）がfrontmatterのidentityで管理ファイルを索引化し、`_reconcile_generated_path`（`feedian/renderer.py:409-430`）が、未編集の管理ファイルだけを新しい予定パスへmoveし、編集済みなら競合として`True`を返す。

これは§10の移行手順そのものである。しかも過去のmarkdownを覚えている必要が無い。hashはファイル自身の内容に対して自己検証するので、レビュー3が「区別する材料がDBに無い」と書いた障害は、この方式では最初から発生しない。`put_source_note`が履歴を残さないことは、案(b)にとって問題ではなかった。

採否Aのまま進めると、**同じVaultの中で、`raw_folder`のノートは手編集が保護され、`source_folder`のノートは黙って上書きされる**、という一貫しない振る舞いが残る。これは仕様の粗さではなく、製品としての非一貫性である。

最終案では次を決めるべきである。

1. Sourceノートにも`render_hash`を持たせ、`_is_unchanged_generated_document`と同じ判定で上書き可否を決めるか。
2. §10の移行を`_index_managed_paths` / `_reconcile_generated_path`の再実装ではなく、これらの再利用として書くか。

少なくとも、Rawノートと異なる扱いにするならその理由を書く必要がある。「機構が無いから」はもう理由にならない。

#### 高 — J. 採否Dが§7の既定対象を変え、一過性の失敗からの復帰を`--force`だけにしている

採否Dは「同じtarget fingerprintで一度失敗した画像は既定実行の対象から外し、`--force`だけが再試行する」とする。二つ問題がある。

第一に、§7 L226は既定対象に`failed`を含めると明記している。採否Dはこれを反転させるので、最終案で§7の既定対象を書き直す必要がある。現状は仕様内に矛盾が残っている。

第二に、この仕様が定義する失敗はほとんどが一過性である。§2はtimeout（既定15秒）、byte上限超過、非画像MIME、decode不能を画像単位の失敗とし、§5はbackend側の失敗も同じ`failed`へ落とす。遅いCDNが1回15秒を超えただけで、その画像は既定実行から恒久的に消える。復帰手段は`--force`しか無いが、`--force`は§7により対象resourceの候補画像を**全て再取得・再解析する**——最も安い種類の失敗から復帰するために、仕様中で最も高い操作を使うことになる。

この repository は既に区別する機構を持っている。取得失敗は`terminal_http_statuses`・`terminal_failure_kinds`・`terminal_kind_failures`で終端か一過性かを分け、`retry_base_minutes`・`retry_max_days`でbackoffする（[fetchのretryと抑止](docs/specs/20260818-fetch-retry-suppression.ja.md)）。採否Dは「明示commandだから」を理由に導入を見送るが、明示commandであることは失敗の性質を変えない。

最小限、終端と一過性を分けて一過性は次回1回だけ再試行するか、あるいは失敗行だけを安く再試行する選択肢（`--retry-failed`相当）を用意するか、どちらかを最終案で決めるべきである。

#### 低 — I. `ingest --dry-run`が移行の予定を表示しない

§10の移行は、実行前に予定を見る手段が乏しい。`ingest --dry-run`は`render_source_notes`へ到達しない。CLIはplanを表示して`return 0`する（`feedian/cli.py:700-708`）ため、`render_source_notes`を呼ぶ行（`feedian/cli.py:746`）まで進まず、`ingest_source_notes`自体も`if dry_run: return`で戻る（`feedian/ingest.py:194-195`）。ディスクを一切変更しない`enrich-images`には`--dry-run`がある（§7）のと対照的である。

危険度は低い。§10の削除条件は、正規化後の内容がDB current markdownと一致し、かつ同内容の新ファイルが存在することの両方を要求するので、失われる情報は無い。§14手順7も実Vault適用前に copy での検証を求めている。それでも、生成するcanonicalファイル数、削除する旧ファイルのパス、保護する競合を`ingest --dry-run`が読み取り専用で報告できれば、手順7の検証がそのまま楽になる。最終案で足すことを勧める。

なお、gitによる復元は部分的である。`source_folder`がgitへaddされるのは`create_snapshot`の実行時だけ（`feedian/snapshots.py:220-227`）なので、直近のsnapshot以降に書かれたファイルはgitに無い。

#### 低 — K. 採否Gの結果、`reused`の定義が宙に浮く

再利用を同じ`resource_image`行に限ると、指紋が既に一致している行は§7 L226の既定対象から外れる。つまり実行時にその行が処理されることは無く、`reused`という実行時の終端状態へ到達する経路が消える。

それでも§3 L124は`reused`を「画像が実行時に終端する状態」の一つとして挙げ続けており、§12は`reused=<解析結果再利用件数>`を報告し続ける。残る唯一の到達可能な意味は、planning時に「指紋一致で対象外にした画像の件数」——§7の`--dry-run`が表示する再利用数——である。

最終案でその定義を明記し、§3 L124を直すこと。放置すると、実装者は何も入れないruntime stateを作ることになる。

#### 低 — L. §12の集計名が採否Hの5値と対応していない

§12はSourceノート生成の報告として`source_written` / `source_skipped` / `source_migrated` / `source_conflicts`の4つを挙げる。採否Hが定めるreportは`written` / `skipped` / `migrated` / `protected` / `blocking_conflicts`の5つで、最後の2つは終了コードへの影響が異なる（`blocking_conflicts`だけが非0にする）。§12の`source_conflicts`1つでは両方を表せない。最終案で§12の出力行を書き直すこと。

#### 低 — M. 採否Bのmessage上限は、4 backendのうち1つにしか存在しない

`build_untrusted_message`の呼び出し元は3箇所ある。`build_manus_message`（`feedian/llm.py:441`）、`CodexLocalBackend.summarize`（`feedian/llm_backends.py:460`）、`ClaudeCodeLocalBackend.summarize`（`feedian/llm_backends.py:806`）である。後者2つは`max_message_chars`を渡さないので切り詰めが起きない。`openai-responses`はそもそもこの関数を通らない。

つまりmessage全体の上限を持つbackendは`manus-api`だけである。採否Bで追加するcapability fieldは「上限なし」を表現できる必要があり、その場合のOCR予算は`image_ocr.max_ocr_chars_per_resource`をそのまま使う、と決めておくべきである。書いておかないと、実装者がlocal backendへ架空の上限を作りかねない。

#### 同意した点

- **追加指摘3の訂正のうち、予定パスと予定パス外を分ける構造は支持する。** 保護を「削除する側」に厚く課す方向は正しい。ただし予定パスを内容差だけで判定するか`render_hash`で判定するかは、指摘Nのとおり決め直す必要がある。`render_hash`を使えば、正常な更新（hash一致）と手編集（hash不一致）を区別したうえで、レビュー4が守ろうとした「正常な要約更新を止めない」性質も同時に満たせる。
- **採否Gの不採用を支持する。** 画像bytesを保存しない以上、resource横断の再利用は正本tableを別に必要とする。初版の範囲外とする判断は妥当である。ただし`reused`の定義は残る（指摘K）。
- **採否Fの修正採用を支持する。** 事前の決定的なpath長検証と、atomic writeの結果による最終判定という二段構えは実装可能である。「書き込み失敗後に旧ファイルの削除へ進まない」は重要な安全側の指定であり、明示されたのがよい。
- 採否B・C・D2・E・Hは、いずれも実装に必要な水準まで具体化されている。

### レビュー6 — tsunyan (2026-08-25)

レビュー5が「人間の判断が必要」として挙げた項目について、次のとおり決定した。決定は口頭で示され、Claude Codeが記録した。

| 項目 | 決定 |
|---|---|
| N. Sourceノートの手編集保護 | **保護しない。** 手編集を守るのはRawノートの一部だけとし、それ以外は自動上書きでよい。 |
| J. 画像解析失敗の再試行 | **一過性エラーだけ、次回に1回だけ再試行する。** |
| 3. 画像解析に使うbackend | **`ingest`と同じく、Vaultの`llm.backend`と`llm.model`で指定されたものを使う。** |
| 4. §9/§10の分離 | **分離しない。** OCRと合わせて1つの仕様として実装する。 |

#### 決定の帰結

**N（保護しない）** — レビュー5の指摘Nは不採用となり、レビュー4の採否A（案(a)）が確定する。Sourceノートに`render_hash`を追加せず、`_index_managed_paths` / `_reconcile_generated_path`の再利用も行わない。§9の検証4はそのまま残る。

なお§10 L307の「利用者が編集したファイルを保護する」は、**予定パス外の旧ファイルについてのみ**成立する規定として読む。旧ファイルの削除条件（正規化後の内容がDB current markdownと一致し、かつ同内容の新ファイルが存在する）を満たさない編集済みファイルは、結果として残る。予定パス上では保護せず上書きする。この非対称は意図されたものである。

**J（一過性のみ1回）** — レビュー5の指摘Jは選択肢(b)で確定し、レビュー4の採否Dを修正する。最終案は次を定める必要がある。

- 失敗の分類。素案として、一過性 = timeout、接続失敗、HTTP 5xx、rate limit、backendの一時障害。終端 = HTTP 404 / 410、非画像`Content-Type`、decode不能、byte上限超過、pixel上限超過。後四者は画像bytesが変わらない限り必ず再失敗するため終端とする。
- 「次回1回だけ」を表現する状態。採否Dが導入する「最後に試したtarget fingerprint、status、時刻」に加え、失敗種別と再試行済みかどうかが要る。
- §7の既定対象の書き直し。`pending`、指紋不一致、および一過性failedで未再試行のもの。終端failedは`--force`のみ。
- 語彙は既存の`fetch_capture.failure_kind` / `consecutive_failures`および`fetch.terminal_failure_kinds`に揃える。同じ概念に別の名前を作らない。

**3（ingestと同じbackend）** — §4の記述が確定する。`image_ocr` configにbackend / model項目を作らない。capability宣言による門番も維持する。

参照Vaultの現在の設定は`llm.backend = "openai-responses"`、`llm.model = "gpt-5.6-terra"`である（`.feedian/config.json`）。従量課金のbackendであり`max_parallelism`は8なので、`image_ocr.workers`の既定値8はそのまま有効になる。

残る実装課題（人間の判断は不要）: 画像をbackendへ渡すmethodの契約。特に`llm.backend`がlocal agent（`codex-local` / `claude-code-local`）に設定された場合、現行は`build_untrusted_message`でテキストを渡すだけなので（`feedian/llm_backends.py:460, 806`）、画像の渡し方を仕様で決める必要がある。

**4（分離しない）** — §14の実装順序をそのまま維持する。文書がgit外に留まる期間は延びるため、確定を先延ばしにしない。

### レビュー7 — Claude Code (2026-08-25)

レビュー6の決定3により対象backendが確定したので、参照Vaultで初回実行の規模を実測した。読み取り専用で`.feedian/feedian.sqlite3`を集計した結果である。

```text
current revisionに属する resource        : 7,342
current revisionに属する resource_image  : 102,145
画像を1枚以上持つ resource               : 6,651
1 resourceあたりの平均画像数             : 15.4

max_images_per_resource=8 適用後の対象   : 37,146 枚
そのうち distinct な source_url          : 25,998 件
```

#### 高 — O. 初回実行は37,146 requestであり、その3割は同一URLの重複である

対象37,146枚に対して、実際に異なるURLは25,998件しかない。**11,148 request、全体の30%が、既に解析した同じURLをもう一度解析するための費用**になる。

重複は少数のURLに集中している。選択集合の中で10回以上現れるURLはわずか168件だが、それだけで8,601行を占める。中身はサイトのchromeである。

```text
740  https://b.st-hatena.com/images/entry-button/button-only@2x.png
679  https://anond.hatelabo.jp/assets/images/logo_anond@2x.png
679  https://anond.hatelabo.jp/assets/images/replies.gif
672  https://anond.hatelabo.jp/assets/images/common/open.gif
```

`logo_anond@2x.png`に対して「これはlogoである」と679回支払うことになる。

これはレビュー4の採否G（resource横断の再利用を初版に入れない）を再考する根拠になる。ただし採否Gが懸念した「画像SHA-256単位の正本tableとindexの設計」は不要である。**planning時に候補を`(source_url, alt_text)`でgroupし、代表1枚だけを解析して、結果を同じ組を持つ全行へ書けばよい。** 1回の実行内で完結するので、画像bytesを保存しないという決定とも矛盾せず、新しいtableもindexも要らない。§7のplanningへ1段落を足すだけで済む。

適用後のrequest数は**26,109**である。

これに伴い、次の3点を最終案へ伝播させる必要がある。

1. **採否Gは全面維持ではなく、部分的に覆る。** 実行を跨ぐSHA-256単位の再利用は依然として初版の対象外だが、実行内のresource横断の共有は入る。キーは画像SHA-256ではなく`(source_url, alt_text)`である。採否Gの訂正として記録すること。
2. **§3 L124のresource単位の進捗管理が成り立たなくなる。** 1つの画像taskが複数resourceの行を同時に終端させるため、「resourceごとの未完了画像数」では数えられない。taskは終端させる行のlistを持ち、Ctrl-C時の失敗終端もその単位で行う、と書き直す。
3. **レビュー5の指摘K（`reused`が到達不能）は覆る。** 実行内の共有により、`reused`は実行時に到達する結果区分になる。planning時に指紋一致で対象外にした件数と、実行内で代表の結果を流用した件数の2つの意味が生じるので、§12でどちらを報告するか（あるいは分けるか）を決めること。

groupのキーに`alt_text`を含めるのは、採否1が入力指紋にaltを含めると定めたためである。実測では、これによる取りこぼしはほぼ無い。

```text
distinct source_url              : 25,998
distinct (source_url, alt_text)  : 26,109
altが複数あるURL                 : 25,998件中 43件
```

altまで一致させても代表の数は111件しか増えない。指紋の定義を弱めずに、重複排除の効果をほぼそのまま得られる。

#### 高 — Q. `llm.model`を変えると、画像解析の費用を丸ごと払い直すことになる

レビュー6の決定3により、画像解析は`ingest`と同じ`llm.backend` / `llm.model`を使う。一方、採否1は入力指紋にbackendとmodelを含め、採否D2は「指紋が変わった行は**既定で**再取得・再解析する」と定めた。

この2つが結び付くと、要約用のmodelを変えただけで、次の`enrich-images`が約26,000枚を再解析する。要約modelの変更はこの repository では普通に起こることであり（`llm.model`の既定値も更新されてきた）、画像OCRの結果はmodelを変えても大きくは変わらない。黙って全額を払い直す崖になっている。

決定3の帰結として最終案で決めるべきである。選択肢は次のとおり。

- 画像解析の指紋からmodelを外し、backendとprompt / schema versionだけにする。modelを変えた再解析は`--force`のみ。
- 指紋にはmodelを残すが、model差分による再解析だけを明示flagの後ろに置く。
- 現状のままとし、崖の存在を仕様へ明記する。

一つ目を推す。OCRという作業の性質上、modelの違いは再実行を正当化しない。

#### 中 — R. 採否Dの「最後に試したtarget fingerprint」が、取得に失敗した画像では計算できない

決定J（一過性のみ1回再試行）は、採否Dが導入する「最後に試したtarget fingerprint」に依存する。しかし採否1の指紋は画像SHA-256を含み、そのSHA-256は画像を取得できて初めて求まる。timeout、DNS失敗、404、非画像`Content-Type`、byte上限超過——決定Jが分類しようとしている失敗のほとんどは、**SHA-256が存在しない**状態で起きる。指紋が計算できなければ、再試行の抑止キーも作れない。

最終案は、取得前に失敗した画像の抑止キーを別に定める必要がある。`(source_url, alt, backend, model, prompt version, schema version)`——SHA-256を除いたもの——で足りる。

同じ節で決めるべき点が2つある。

- **byte上限・pixel上限による失敗の「終端」は設定に依存する。** `image_ocr.max_bytes`や`max_pixels`を上げれば通るようになるが、これらの値は指紋に入っていない。上限を変えたら終端状態を解除する、と書くこと。書かないと、設定を緩めても対象が戻ってこない。
- **「再試行済み」flagは指紋が変わった時点でリセットする。** しないと、model / prompt / altが変わっても一度失敗した画像が二度と既定対象に戻らず、採否D2が塞いだ穴が開き直る。

なお§2はHTTP statusの扱いを何も定めていない。404 / 410を終端とするなら、§2へ追記が要る。

#### 低 — P. 第1段階のfilterは弱いが、これは費用ではなく「何を解析するか」の問題である

**当初この節は「重複chromeを除けば費用が下がる」と書いていた。実測すると逆だったので訂正する。**

1 resourceあたり平均15.4枚は記事本文の説明画像の数ではない。`_is_non_content_image`（`feedian/extract.py:825-836`）が見るのはclass / id / role / aria-labelの語と10px以下のwidth・heightだけで、上に挙げたchromeはどれも通り抜ける。選択された37,146行のうち8,601行がそれである。

しかし指摘Oの重複排除を通すと、その8,601行は**168 requestまで畳まれる**。chromeは既にほとんど費用を使っていない。

さらに、上限8枚は**行数**の上限である。chromeを候補から外すと空いた枠が別の画像で埋まり、その多くは重複しない固有URLなので、request数はむしろ増える。実測は次のとおり。

```text
現案（position順に上位8枚 → 重複排除）        : 37,146行 → 26,109 request
chrome除外（10 resource以上のURLを除いて上位8枚）: 31,411行 → 28,619 request
```

**行は5,735枚減るのに、requestは2,510件増える。** したがってchrome除外は費用削減策ではない。

残るのは品質の問題である。現案は8,601枠をchromeに使っており、その分だけ本物の図表が上限に押し出されている。押し出しを直すなら+2,510 requestを払うことになる。どちらを取るかは判断であり、私からは決められない。仕様としては、費用が下がるという理由で§1へfilterを足すことはしない、とだけ確定させたい。

#### 低 — S. 決定Nと決定4から伝播していない箇所

- **§10 L307は3つの分類を1文で扱っており、決定Nで扱いが割れる。** 「利用者が編集したファイル」は予定パス外でのみ保護される（削除条件を満たさないため結果的に残る）。一方「frontmatterを読めないファイル」と「DBに対応するcurrent noteが無いファイル」は、予定パス上にあれば採否Hの`blocking_conflicts`であって保護ではない。1文を分けること。
- **§13の検証29が決定Nと矛盾する。** 「編集済みファイル…を保護し、競合として報告する」は、予定パス上では成り立たなくなった。予定パス外に限定して書き直すこと。
- **レビュー6が`render_hash`の範囲を狭く書いている。** `feedian render`が書く管理ノートは`render_hash`で保護されており（`feedian/renderer.py:327-345`）、Rawノートの一部ではない。決定Nは利用者の運用（手編集するのはRawの一部だけ）に基づく方針であって、コードの現状の記述ではない。非対称は「`render`が書くノートは保護、`ingest`が書くSourceノートは非保護」と整理するのが正確である。
- **決定4は「仕様を分けない」であって「実装順序を変えない」ではない。** §14は§9/§10を手順5に置くが、1,841件の衝突は今日すでに起きており、OCR側のどの決定にも依存しない。手順1へ繰り上げることを勧める。1つの仕様のまま、先に効く方から実装できる。

#### 費用見積りについて

1 requestあたりの単価は、`gpt-5.6-terra`の画像入力価格を私は持っていないため、ここでは示さない。推測した数字を仕様の根拠にはしない。

代わりに、`--limit`による段階実行で実測するのが確実で安い。§7の`--limit`はresource件数の上限なので、`feedian enrich-images --limit 10`で実際の`input_tokens` / `output_tokens` / `cost_usd`が§12の終了行に出る。そこから指摘O適用後の26,109 requestへ外挿すればよい。

したがって、**画像requestの費用推定機構は実装しなくてよい。** §7の`--dry-run`は現行案どおり対象resource数、候補画像数、再利用数、最大外部request数を表示すれば足り、`ingest`が持つような`estimated_cost_usd` / `max_cost_usd`の仕組みを画像側へ作る必要はない。画像入力のtoken見積り方法が未定義だという問題も、これで解消する。

### レビュー8 — Claude Code (2026-08-25)

#### 指摘Pを取り下げる

レビュー7の指摘Pは、chromeの除外を「品質を取るか費用を取るか」の判断として提示した。**これは目的を取り違えている。**

この仕様の目的は説明画像のOCRである。説明画像でなければOCRは不要であり、logoをLLMへ送って「これはlogoです」と言われることは、安い工程ではなく**誤った工程**である。判断を要する trade-off ではなく、直すべき欠陥である。

取り違えの原因は`max_images_per_resource=8`を「埋めるべき枠」として扱ったことにある。上限は安全弁であって目標ではない。gateを通した候補が3枚なら3枚だけ解析すればよく、「chromeを除くと空いた枠が埋まってrequestが増える」という前回の計算は、枠を埋め続ける前提そのものが誤りだった。

以下、指摘Pを取り下げ、指摘Tへ置き換える。

#### 高 — T. §1/§2の段階構成が目的と合っていない

§1は「既存の`extract_content_images`が…この判定を第1段階として維持し、残った候補を視覚解析へ渡す」と書き、2段階を前提にしている。第1段階の`_is_non_content_image`（`feedian/extract.py:825-836`）が見るのは、class / id / role / aria-labelに現れる語と、10px以下のwidth・height属性だけである。

実測では、これを通り抜けた候補が1 resourceあたり平均15.4枚ある。記事本文の説明画像がそれだけあるはずがない。第1段階とLLMの間に、安価な判定が欠けている。

**3段階に組み直すことを提案する。**

```text
第1段階  extract_content_images        （現行、無料）
第2段階  安価なgate                    （新規）
          a. URL・ファイル名による除外   … 取得前、無料
          b. 取得後のサイズによる除外     … LLM呼び出し前、追加費用ゼロ
第3段階  LLMによる分類とOCR            （§4）
```

そして**`max_images_per_resource`は第2段階の後に適用する。** 現行案は§2 L105で「候補が上限を超える場合は`resource_image.position`の小さいものから採用する」としており、これがextract直後の生の候補に効くため、chromeが枠を先に埋めてしまう。

**(a) 名前による除外。** 表示用アセットはファイル名に痕跡を残す。参照Vaultでの実測は次のとおり（分母は current revision の候補102,145枚、重複を含む生の行数）。

```text
avatar / profile          11,071  (10.8%)
icon                       3,946  ( 3.9%)
logo                       2,954  ( 2.9%)
@2x / @3x                  2,712  ( 2.7%)
button / btn               1,895  ( 1.9%)
banner / badge             1,001  ( 1.0%)
emoji / favicon              271  ( 0.3%)
sprite / spacer / blank       48  ( 0.0%)
────────────────────────────────────────
合計（重複排除後）        20,900  (20.5%)
```

`@2x` / `@3x`は解像度違いの表示用アセットを示す接尾辞であり、説明画像に付く理由がない。残りも同様に、記事固有の説明ではなくサイトの装飾を指す語である。

**(b) 取得後のサイズによる除外。これが追加費用ゼロで効く。** §2は既に、解析の前に画像を取得している。したがってbytes数とdecode後の寸法は、LLMを呼ぶ前に手元にある。現行案はこれを`max_bytes` / `max_pixels`という**上限**にしか使っていない。**下限**が要る。

表、グラフ、UIのscreenshot、模式図が200px四方を下回ることは実質的にない。一方、名前gateを通り抜けた残りの最頻出URLはこれである。

```text
679  https://anond.hatelabo.jp/assets/images/replies.gif
672  https://anond.hatelabo.jp/assets/images/common/open.gif
```

数十pxのUI用gifで、名前には何の手がかりもない。下限寸法だけで確実に落ちる。§2の設定表へ`image_ocr.min_pixels`（または短辺の下限）と`image_ocr.min_bytes`を追加し、下回った画像は`ignored`として記録する——`failed`ではない。取得できているので失敗ではなく、対象外である。

**(c) 名前でもサイズでも落ちないものが残る。** 名前gate後の残りには、`https://b.hatena.ne.jp/entry/image/...`という形のURLが545回ずつ大量に現れる。はてなブックマークのwidgetが挿入するサムネイルで、記事本文の画像ではないが、名前にもサイズにも手がかりが乏しい。

これは記事のhostと画像のhostが異なる第三者widgetである。ただしCDNから本文画像を配信するサイトも普通にあるため、host不一致だけを根拠に落とすと本物を巻き込む。指摘Oの「多数のresourceに同じURLが現れる」という観測のほうが、この種を捉える指標として素直である。第2段階の(c)として、閾値を超える頻度で現れるURLを`ignored`にすることを検討したい。ただし(a)(b)ほど自明ではないので、最終案で採否を決めること。

#### 中 — U. 上限8枚の位置づけを書き直す

§2 L99の`max_images_per_resource`は安全弁であって、消費すべき予算ではない。gateを通った候補が上限に満たなければ、その枚数だけ解析する。§2 L105の「候補が上限を超える場合は`position`の小さいものから採用する」は、第2段階の後に適用する規則として書き直すこと。

名前gate通過後の分布は次のとおりで、大半のresourceは少数枚しか持たない。サイズgateを通せばさらに減る。

```text
 1枚 → 1,887 resource
 2枚 →   914
 3枚 →   447
 4枚 →   303
 5枚 →   270
```

#### 指摘O・Qとの関係

指摘O（`(source_url, alt_text)`による実行内の重複排除）は引き続き有効だが、適用順は第2段階の**後**である。gateで落ちた画像はそもそも代表を立てる必要がない。

指摘Q（`llm.model`変更で全画像を再解析する崖）は、gateによって対象が減るほど影響も小さくなるが、問題の性質は変わらない。別途決めること。

#### 未測定の部分

サイズgateの効果は、実際に画像を取得しないと測れない。`resource_image`はURLとaltしか保存しておらず、HTMLのwidth / height属性も永続化されていない（`feedian/extract.py:825-836`が読むだけで捨てている）。

必要なら、候補から数百枚を標本抽出してHEADまたは部分GETでサイズを測り、下限値の妥当な既定を決められる。外部サイトへの実アクセスを伴うため、実施の可否は指示を待つ。

### レビュー9 — Claude Code (2026-08-25)

第2段階のgateへ形式と重さの規則を入れる案について、参照Vaultで裏を取った。候補102,145枚のURL末尾から読める拡張子の分布は次のとおりである。

```text
(拡張子なし)  45,478  (44.5%)
jpg / jpeg    33,517  (32.8%)
png           10,796  (10.6%)
gif            7,376  ( 7.2%)
svg            3,122  ( 3.1%)
webp             685  ( 0.7%)
avif              29  ( 0.0%)
tiff / bmp / ico   0
```

#### 高 — V. 拡張子は44.5%で読めない。判定は`Content-Type`で行う

候補の44.5%はURL末尾に拡張子を持たない。CDNやimage proxyがquery文字列やpath segmentで形式を決めるためである。ファイル名で形式を判定する規則は、半分近くに適用できない。

§2は既に「応答の`Content-Type`が`image/*`でない場合は失敗とする」と定めており、形式は取得応答から確実に得られる。**形式による判定は拡張子ではなく`Content-Type`を正本とする**、と最終案へ書くこと。拡張子は補助にとどめる。

#### 中 — W. `image/tiff`等の除外は正しいが、この Vault では効果がない

`tiff`、`bmp`、`ico`は参照Vaultの候補に1件も現れない。規則としては正しく、書いておく価値はある（`image/tiff`、`image/bmp`、`image/x-icon`、`image/vnd.microsoft.icon`を`ignored`とする）が、費用や件数への効果は無い。効果を期待して優先度を上げるべき項目ではない。

#### 高 — X. アニメーションは「重さ」ではなく、header で正確に判定できる

「重いgifはアニメだから除外」という判断は方向として正しいが、閾値を調整する必要はない。アニメーションは形式のheaderに明示されている。

- GIF: Image Descriptor ブロックが複数あればアニメーション。
- WebP: `ANIM` チャンクの有無。
- PNG: `acTL` チャンクがあればAPNG。

いずれも先頭数KBで判定でき、重さの閾値と違って誤判定しない。**重さの代理指標ではなく、直接の判定を使うこと。** アニメーションは`ignored`とする。

#### 高 — Y. 「大きいpng」は除外してはいけない。狙っている信号はbytes/pixelである

「大きい jpg / png / webp も除外できるのでは」という案は、形式ごとに当否が割れる。

- **大きいJPEG** — 写真である蓋然性は高い。ただし§1は「記事中に埋め込まれたスキャン文書」を対象に含めており、スキャン文書は典型的に大きなJPEGである。単純な除外は対象を削る。
- **大きいPNG** — **これは除外できない。** 表、UI screenshot、模式図が最も普通に取る形がまさに「大きいPNG」である。この規則を入れると、この仕様が狙っている画像そのものが落ちる。
- **大きいWebP** — 近年のサイトは写真もscreenshotもWebPで配信する。形式からは何も言えない。

この案が捉えようとしている信号は、形式でも絶対的な大きさでもなく、**1 pixelあたりのbyte数**である。写真は全面にノイズがあるため圧縮が効かず、bytes/pixelが高い。表、screenshot、模式図は平坦な領域が広いため圧縮がよく効き、bytes/pixelが低い。この指標はJPEG / PNG / WebPを跨いで機能し、「大きいJPEGは写真」を、「大きいPNGはscreenshot」を潰さずに表現できる。

必要な入力はbyte数と寸法だけで、どちらも取得時に手に入る。追加費用は無い。

したがって第2段階(b)は次の3つで構成する。

```text
下限:      短辺 or 総pixel数が閾値未満 → ignored   （UI用の小さな部品）
上限:      既存の max_bytes / max_pixels           （資源保護、現行どおり）
bytes/pixel: 閾値を超える → ignored                （写真である蓋然性が高い）
```

bytes/pixelの閾値は実測なしに決めない。標本抽出で分布を見てから定めること。

#### 中 — Z. SVGはOCRの対象ではない。テキストを直接取り出せる

候補の3.1%、3,122枚がSVGである。SVGはvector形式で、図中の文字は`<text>`要素として**そのままテキストで入っている**。画像として描画してLLMへ送り、読み取らせる理由がない。

SVGは第2段階でLLM経路から外し、XMLから`<text>`の内容を直接取り出して`ocr_text`相当として保存すればよい。費用ゼロで、しかもOCRより正確である。`lxml`は既に依存に入っている。

ただし、文字を`<path>`へアウトライン化したSVGにはテキストが無い。その場合は`ocr_text`を空として`completed`扱いにするか、LLM経路へ回すかを最終案で決めること。前者を推す。§1が想定するSVGの説明図は、作図ツールが出力するテキスト付きのものが大半である。

#### 高 — AA. §2が要求する画像decodeに必要な依存が存在しない

§2は「decoderが画像として読めない場合は失敗とする」「画像展開後のpixel数にも上限を置く」と定めるが、`pyproject.toml`の`dependencies`に画像ライブラリが無い。Pillowも入っていない。9ラウンドのレビューが全て見落としていた。

選択肢は2つある。

1. **Pillowを依存へ追加する。** 完全なdecodeができる。新しい依存であり、判断が要る。
2. **headerだけを自前で解析する。** PNGの`IHDR`、JPEGの`SOF`マーカー、GIFのlogical screen descriptor、WebPのVP8 / VP8L / VP8Xヘッダから、形式・寸法・アニメーションの有無が読める。50行程度で、外部依存は増えない。

**2を推す。** 第2段階のgateに必要なのは形式・寸法・byte数・アニメーションの有無だけで、pixelの中身は要らない。しかも header は先頭数KBにあるため、**range GETで先頭だけ取得してgateを判定し、通ったものだけを完全に取得する**設計にできる。落とす画像の本体をそもそもdownloadしない。§2が現在想定している「全体を取得してからdecode」より安い。

この場合、§2の`max_pixels`は「decode後の総pixel数」ではなく「headerが宣言する寸法」に対する上限になる。decodeしないので資源枯渇の防止という目的はより直接的に果たされる。§2の文言を書き直すこと。

第3段階でLLMへ画像を渡すときも、bytesをそのまま渡すのであればdecodeは不要である。decodeが本当に必要な工程が他に無いか確認したうえで、依存追加の可否を決めること。

### レビュー10 — Claude Code (2026-08-25)

第2段階(b)の閾値を決めるため、実際に画像を取得して分布を測った。

**方法** — current revisionに属する候補の distinct URL 62,438件から、1 hostあたり最大6件に制限して600件を無作為抽出し、`Range: bytes=0-8191`で先頭8KBだけを取得した。形式・寸法・アニメーションの有無はheaderから読み、全byte数は`Content-Range`から得た。画像本体は保存していない。成功578件、失敗22件（HTTPError 20、URLError 2）。うちheaderを解析できて全byte数も判明したものが501件。

#### 実測 — byte数

```text
全形式（n=501）
  p10     0.9 KB      p75    63.5 KB
  p25     3.2 KB      p90   154.0 KB
  p50    18.3 KB      p95   223.3 KB
                      p99   815.2 KB
                      max  1870.1 KB  (1.83 MB)

形式別の最大
  png   n=148  median  16.4 KB   p95  326.5 KB   max 1.83 MB
  jpeg  n=303  median  23.3 KB   p95  200.8 KB   max 1.56 MB
  gif   n= 43  median   0.7 KB   p95   37.9 KB   max 0.06 MB
  webp  n=  7  median  44.6 KB   p95  146.9 KB   max 0.14 MB

5 MBを超える画像: 0 / 501
```

#### 高 — AB. 数十MB級の画像はこのVaultに存在しない。byte数は判定に使えない

「大きいpngは数十MB規模を想定していた」という前提は、実測では成立しない。**最大は1.83 MBで、5 MBを超える画像は501枚中1枚も無い。** p99でも815 KBである。

文字中心のPNGが数十MBにならない、という見立ては正しい。しかし写真もこのVaultでは数十MBにならない。webに載る画像は配信前に縮小・再圧縮されるためで、byte数の上限は写真と説明画像を分ける線にならない。

したがって`image_ocr.max_bytes`は**純粋な資源保護として残す**。既定20 MiBは実測p100の10倍以上あり、内容の判定には一切寄与しない。この値を下げても除外件数は変わらない。

#### 高 — AC. レビュー9の指摘Yで提案したbytes/pixelを取り下げる

指摘Yで「写真はbytes/pixelが高く、表やscreenshotは低い」と書いた。**実測はこれを支持しない。**

```text
bytes / pixel
  png   n=148  p10 0.083  p50 0.482  p90 1.953
  jpeg  n=303  p10 0.091  p50 0.243  p90 0.695
  gif   n= 43  p10 0.198  p50 0.527  p90 43.000
  webp  n=  7  p10 0.063  p50 0.095  p90 0.122
```

PNGの中央値0.482は、JPEGの0.243より**高い**。私の理屈の逆である。原因は明白で、bytes/pixelを支配するのは内容ではなく**codec**だからである。JPEGは非可逆で常に小さくなり、PNGは可逆で常に大きくなる。写真か図かの差は、その下に埋もれる。

形式を跨いだ閾値は引けない。形式ごとに引くことは理屈のうえでは可能だが、写真か図かの正解ラベルを持たないので閾値の根拠が作れない。**bytes/pixel規則は採用しない。** 下の指摘ADが示すとおり、寸法だけで用が足りる。

#### 高 — AD. 短辺の下限が、gate全体の仕事のほぼ全部を担う

```text
短辺（min(width, height)）の分布  n=501
  p5    17 px     p50   214 px     p90   669 px
  p10   32 px     p75   429 px     p99  1260 px
  p25   86 px

閾値を下回る割合
  < 100px : 27.3%      < 200px : 46.7%
  < 150px : 39.1%      < 300px : 60.3%
```

**短辺200pxの下限だけで、distinct候補の46.7%が落ちる。** 表、グラフ、UI screenshot、模式図が200px四方を切ることは実質的にないので、失うものはほぼ無い。第2段階で最も効く単一の規則であり、LLMを1回も呼ばずに済む。

レビュー9の名前gate（`@2x`、`logo`、`icon`など）と併用した場合の内訳は次のとおりである。

```text
短辺 < 200px のみ        234 / 501  (46.7%)
名前gate のみ             66 / 501  (13.2%)
どちらか（併用）         249 / 501  (49.7%)
  → 名前gateが単独で足す分  15枚  (3.0%)
```

**寸法gateがあれば、名前gateが追加で落とすのは3%にすぎない。** 名前で捕まる`logo`や`avatar`は、そもそも小さいからである。

ただし名前gateを捨ててはいけない。役割が違う。

- **名前gateは取得の前に効く。** HTTP requestそのものを13.2%減らす。
- **寸法gateは取得しないと効かない。** range GETで先頭8KBだけ取れば十分だが、requestは要る。

費用の階層が異なるので、両方を残し、名前gateを先に置く。ただし名前のパターンを増やす作業に労力を割く価値は小さい、と最終案へ書いておくこと。寸法gateが後ろで拾う。

#### 中 — AE. アニメーションとTIFFは規則としては正しいが、量が無い

- アニメーション: 501枚中**1枚**（`henkei_meishi.gif`、142×91、14.8 KB）。しかも短辺200px下限で落ちる。指摘Xのheader判定は正しく、実装も安いので入れてよいが、効果を見込む項目ではない。
- `image/tiff`、`image/bmp`、`image/x-icon`: 0件。指摘Wのとおり。
- GIF全体の中央値が0.7 KBである。このVaultのGIFはほぼ全てspacerやUI部品で、寸法gateが処理する。

#### 低 — AF. `image/*`を名乗らない応答が0.7%ある

Content-Typeの内訳は`image/jpeg` 58.0%、`image/png` 27.7%、`image/gif` 8.0%、`image/svg+xml` 3.5%、`image/webp` 1.4%、`image/avif` 0.2%であった。

これに加えて`text/html`が4件（0.7%）、Content-Type空が1件ある。HTTP 200でHTMLのエラーページやhotlink禁止ページを返すサイトである。§2の「`Content-Type`が`image/*`でなければ失敗」で正しく弾かれるが、これは**取得の失敗ではなく対象外**なので、`failed`ではなく`ignored`とすべきである。決定Jの再試行対象に入れる理由も無い。

`image/jpg`（非標準）が3件ある。`image/*`の前方一致で通るので実害は無い。

#### 第2段階の確定案

実測を踏まえ、第2段階を次のとおり提案する。

```text
(a) 取得前・無料
    1. 名前による除外        @2x @3x logo icon avatar profile button
                             banner badge sprite spacer blank emoji favicon
       → HTTP requestを13.2%削減。パターン拡張に労力はかけない。

(b) 先頭8KBのrange GET後・LLM呼び出し前
    2. Content-Typeが image/* でない        → ignored
    3. image/tiff, image/bmp, image/x-icon  → ignored
    4. アニメーション（header判定）          → ignored
    5. image/svg+xml                        → LLMを使わず<text>を直接抽出
    6. 短辺 < 200px                          → ignored   ★主力。46.7%
    7. 寸法 or byte数が上限超過              → failed（資源保護、現行どおり）

(c) 残ったものだけ完全取得し、第3段階のLLMへ渡す
```

採用しない案として、bytes/pixel（指摘AC）と、byte数の下限・内容判定用の上限（指摘AB）を明記しておくこと。どちらも実測が支持しなかった。

この構成で、distinct候補の約50%がLLMへ届く前に落ちる。名前gateが取得前に13.2%を落とすので、実際のHTTP requestも減る。

### レビュー11 — Claude Code (2026-08-25)

`enrich-images`を単独commandとして少しずつ実行できるか、という観点で草案を読み直した。commandの独立性は保たれているが、費用の面で独立していない箇所が1つある。

参照Vaultで確定している価格は次のとおりである（`feedian/estimate.py:39-46`のfallback表、および公式のmodel documentで一致を確認した）。

```text
gpt-5.6-terra    input $2.00 / 1M    cached input $0.20 / 1M    output $12.00 / 1M
```

**outputがinputの6倍**である。OCRは転記作業なので出力量が多く、この比率が費用構造を決める。

#### 高 — AG. §258のprompt version更新が、要約キャッシュを全件無効化する

§258はこう定める。

> OCR入力追加に合わせて要約prompt versionを更新する。

`PROMPT_VERSION`は`feedian/ingest.py:30`の単一のグローバル定数（`"source-note-v1"`）であり、再利用キーの一部として`successful_llm_result()`へ渡される（`feedian/ingest.py:653`）。これを上げると、**current resource 7,342件すべての要約キャッシュが一斉に無効になる。**

画像を1枚も持たないresource（実測で7,342件中691件）も、まだ`enrich-images`を実行していないresourceも、区別なく再要約の対象になる。

草案自身がこの形を認識してはいる。

> OCRが無いresourceのrequestは、**prompt versionの更新を除いて**従来と同じ本文を持つ。

差分がversion更新だけだと書いたうえで、それでも上げる設計になっている。費用の帰結は書かれていない。

**これは`enrich-images`の段階実行を無意味にする。** `--limit 10`で10件だけOCRしても、その後の最初の`ingest`が7,342件分の要約費用を発生させる。しかもその額は、レビュー10のgate適用後に見込まれる画像側の費用より大きい可能性が高い。「まず少し試す」が成立しない。

**prompt versionを2系統に分けることを提案する。**

- **使えるOCRを持たないresource** — promptは現行とbyte単位で同一である。`source-note-v1`のまま据え置き、再利用を維持する。
- **OCRを持つresource** — `<untrusted_image_ocr>`ブロックが入るので別のprompt shapeであり、`source-note-v2`とする。

`_candidate`（`feedian/ingest.py:618-661`）がrequestを組み立てた時点でOCRの有無は判っているので、そこで`prompt_version`を選べばよい。実装は小さい。

これにより、OCRが付いたresourceだけが要約し直される。10件OCRすれば10件だけ再要約となり、段階実行が最後まで成立する。

#### 高 — AH. `ocr_text`に生成時の上限が無い。費用の主成分がそこにある

§2の`image_ocr.max_ocr_chars_per_resource`（10,000）は、**ingestへ渡すときの上限**である。§4のOCR契約にも§5の`ocr_text`列にも、生成時の長さの制限が無い。

outputは$12/1Mで、inputの6倍である。1枚あたりの見当は次のようになる。

```text
input  765〜1,105 token 相当   ≈ $0.0015〜0.0022
output   200 token             ≈ $0.0024
         500 token             ≈ $0.0060
       1,000 token             ≈ $0.0120
       3,000 token             ≈ $0.0360   ← 上限が無いので到達しうる
```

密なscreenshotやスライドを転記させると、出力がinputの10倍以上の費用になる。上限が無いままでは、1枚の異常値が予算を食う。

**`image_ocr.max_ocr_chars_per_image`を§2の設定表へ追加し、promptにも同じ制限を書くこと。** 併せて、次の相互作用を仕様で決める必要がある。

構造化出力で`max_output_tokens`に到達すると、JSONが途中で切れて解析に失敗する。§13の項目7はこれをprotocol errorとして記録するとしているので、上限を厳しく設定すると「密な説明画像ほど失敗する」という逆の挙動になる。

したがって、**文字数の制限はpromptで指示し、`max_output_tokens`はそれより十分に余裕を持たせる**構成にする。promptの指示で収まらず`max_output_tokens`に到達した場合は、同じ入力で再試行しても同じ結果になるため、決定Jの分類では**終端**の失敗である（指摘Rの分類表へ追記すること）。

#### 低 — AI. 送信前の縮小は不要である。実測がそう示した

前回、image tokenを削るために送信前の縮小を提案した。**実測はこれを支持しない。**

レビュー10のgateを通過した252枚について、寸法は次のとおりである。

```text
長辺  p50  640px   p75  933px   p90 1,200px   max 3,968px
  長辺 > 1024px :  51 / 252  (20.2%)
  長辺 > 1536px :  11 / 252  ( 4.4%)

短辺が768pxを超えるもの: 35 / 252  (13.9%)
```

tilingは短辺を768pxへ縮めてからtileを数えるため、**短辺が768px以下の画像はそもそも縮小されず、client側で縮小してもtoken数は変わらない。** 該当するのは86.1%である。

残る13.9%についても、server側が同じ768pxまで縮小する。client側で先に縮めても結果は同じで、削減にならない。

したがって送信前の縮小は採用しない。upload帯域の節約は理屈のうえでは残るが、gate通過後の中央値は61.5 KBであり、意味のある量ではない。

**ただしこの結論は、tilingが短辺を基準寸法へ正規化するという前提に依存する。** `gpt-5.6-terra`のimage token算出規則は公式のmodel documentに記載が無く、裏を取れていない。生のpixel数に比例して課金する方式であれば、長辺1024pxを超える20.2%については縮小に意味が出る。

実装後に`enrich-images --limit 1`を寸法の異なる画像で数回実行すれば、`input_tokens`の実値から算出規則が判る。**その実測までは縮小を実装せず、判明した時点で必要なら追加する**のが安い。前提が確認できるまで依存を増やさない。

#### 高 — AJ. OCRが付いたresourceだけを要約し直す手段が存在しない

指摘AGはprompt versionを分けることで段階実行を守ろうとしたが、**守るべき段階実行そのものが`ingest`側に存在しない。**

- `--auto`は使えない。`_select_auto_candidates`（`feedian/ingest.py:672-689`）は`source_note`を既に持つresourceを`covered_ids`として除外し、`actionable`をそれ以外に限る。**一度要約したresourceは`--auto`の対象に二度と入らない。** OCRが付いても選ばれない。
- `--limit N`（非auto）も使えない。`plan_source_notes`は`all_candidates[:limit]`を取り（`feedian/ingest.py:131`）、`_source_rows`の順序は`ORDER BY rr.created_at ASC`（`feedian/ingest.py:613`）である。**変更があったN件ではなく、最も古いN件**が選ばれる。

つまり`enrich-images --limit 10`で10件にOCRを付けたあと、その10件の要約を更新する手段は、7,342件を対象にした非autoのフル実行しかない。指摘AGでprompt versionを分けても、再要約されるのはOCRを持つ10件だけになる——が、そこへ到達するために7,342件分のplanningとfingerprint計算を回し、`--limit`を付ければ古い順に切られて当の10件が入らない。

**§7または§8へ、OCRが変化したresourceを選ぶ規則が要る。** 案として、`ingest`へ`--changed`相当の選択modeを設け、「現在のrequest fingerprintに一致する完了`llm_run`を持たないresource」を対象にする。これはOCR追加に限らず、prompt versionやmodelを変えた場合にも正しく効く一般的な規則であり、既存の`successful_llm_result`の判定をそのまま選択条件へ使える。

これが無い限り、`enrich-images`が単独commandであることは実務上の段階実行を意味しない。

#### 検証後の補正

上記3件をコードへ突き合わせ直した結果、次を補正する。

**AGの前提条件が2つ抜けていた。**

1. **`SUMMARY_INSTRUCTIONS`はfingerprint対象のrequestに入っている**（`feedian/llm.py:542`、`build_summary_request`の`instructions`）。OCRに関する説明文をここへ足すと、OCRを持たないresourceのrequestもbyte単位で変わり、prompt versionを据え置いても再利用が壊れる。**OCRの記述はrequest本体のprompt側だけに置くこと。**
2. **`PROMPT_VERSION`は3箇所で使われている** — `feedian/ingest.py:653`（再利用検索）、`:308`（`promote_legacy_fingerprint`）、`:508`（実行の記録）。1箇所だけ分岐させると、OCR入りのfingerprintがv1として記録され、以後どちらのキーにも一致しなくなる。`IngestCandidate`のfieldとして持たせ、3箇所すべてがそれを読む形にすること。なお`IngestCandidate`は位置引数で構築されている（`feedian/ingest.py:659-661`、`:702-706`）。

**AGの根拠を1つ補強し、1つ弱める。**

- 補強 — グローバルなversion更新は、単にcacheを無効化するだけでなく、**未promoteのlegacy fingerprint行を恒久的に取り残す。** `_completed_llm_run`はv2検索とlegacy検索の**両方**で`prompt_version`を条件にしている（`feedian/store.py:1127-1139`）。`DESIGN.md:21`によればこの移行窓はまだ開いている。versionを上げた瞬間、未promoteの行は二度と拾われない。
- 弱める — 「7,342件すべての要約キャッシュ」は current resource の件数であって、完了した`source-note`実行を持つ件数ではない。実際に無効化されるのはそのうち要約済みのものに限られる。件数を主張する場合は`source_note`側で数え直すこと。

**AHの検出方法が未定義である。** `max_output_tokens`到達は2通りで表面化する。JSONが途中で切れれば`feedian/llm.py:330`の`LLMProtocolError`、reasoning tokenが予算を食い切って出力が空なら`feedian/llm.py:326`の`LLMProtocolError`である。**どちらも他のprotocol errorと区別が付かない。** 応答の`status`や`incomplete_details`を読む処理は存在しない。

したがって「`max_output_tokens`到達は終端の失敗」と決めるだけでは足りず、**何をもってそれと判定するかを仕様が指定しなければならない。** 指定しないと、区別できないまま全protocol errorが終端扱いになる。併せて、headroomはreasoning tokenの分も含めて確保すること。

またAHが提案する「promptで文字数を制限する」は、§4の「翻訳・要約・補完を行わず原文のまま転記する」と衝突しうる。**上限に達した場合は先頭から順に転記して打ち切る（要約しない）**と明記すること。

**AIに1つ穴がある。** 依存を増やさない結論は正しいが、headerを解析できない場合の分岐が未定義である。実測では578件中39件（6.7%）でheaderから寸法を取れなかった。

```text
image/svg+xml   20   … 別経路（指摘Z）で扱うので問題ない
image/jpeg      13   … SOFマーカーがEXIF/ICCの後ろにあり先頭8KBに無い
text/html        4   … Content-Typeで弾かれる
image/avif       1   … ISOBMFFのbox解析が必要
(空)             1
```

実質的な穴はJPEGの13件（2.2%）である。**先頭8KBで寸法が読めない場合の規則を決めること。** 範囲を広げて再取得する（EXIF/ICCは通常64KB以内）か、寸法不明として第3段階へ通すか。寸法不明を`ignored`にしてはいけない——短辺gateが46.7%を落とす主力である以上、判定できないものを落とすと本物を巻き込む。

**range GETの節約効果も控えめに書き直すこと。** gate通過後の中央値は61.5 KBで、64KBまで範囲を広げれば半数近くは結局全体を取得することになる。range GETが効くのは大きい側の裾であって、中央値ではない。

**この結論はレビュー9の指摘AAを補強する。** 縮小が不要なら、decodeが必要な工程は残らない。形式・寸法・アニメーションの有無はheaderから読めるので、**Pillowを依存へ追加する理由が無くなる。** 指摘AAの選択肢2（headerの自前解析、range GETと組み合わせ）で仕様を確定してよい。

### レビュー12 — Codex (2026-08-25)

#### 結論

レビュー6で記録された人間の決定4件は明確であり、そのまま最終案へ反映できる。レビュー11の指摘AG（OCRの有無によるprompt versionの分離）とAJ（現在のrequest fingerprintに一致する結果を持たないresourceの選択手段）も、OCRを少数ずつ適用できるようにするため必要である。

一方、レビュー8〜10で追加された第2段階gateは、構成自体は支持するが、現時点の測定から固定の除外条件まで確定することはできない。測定は候補画像の寸法・形式の分布を示しただけで、「説明画像を落とさない」ことを検証していない。また、重複排除と上限の適用順には、外部request数を増やす矛盾がある。以下を解消してから最終案にする必要がある。

#### 高 — AK. gateの測定に正解ラベルがなく、説明画像の取りこぼし率が分からない

レビュー10は短辺200px未満の候補が501件中46.7%であることを示した。しかし、各画像が説明画像・写真・装飾・アイコンのどれであるかは確認していない。寸法分布だけから「表、グラフ、UI screenshot、模式図が200px四方を切ることは実質的にない」と結論することはできない。

同様に、レビュー8が取得前gateへ挙げた`@2x` / `@3x`は表示密度を表す一般的な接尾辞であり、説明用のscreenshotや図にも付き得る。一律除外を正当化する信号ではない。

さらに、レビュー10の標本はdistinct URLからの一様抽出ではなく「1 hostあたり最大6件」に制限され、取得・header解析に成功した501件だけを割合の分母にしている。host間の偏りを抑える調査としては有用だが、その割合をVault全体のdistinct候補へそのまま外挿できない。

**採否: 修正して採用。** 3段階gateの構成は採用する。ただし短辺200pxと名前規則は暫定候補とし、無作為標本へ人間が説明画像／対象外の正解ラベルを付け、条件ごとのfalse negativeを確認してから既定値を確定する。少なくとも`@2x` / `@3x`は単独の除外条件にしない。測定では抽出母集団、random seed、取得失敗を含む件数を記録する。

#### 高 — AL. 重複排除をgate後に行うと、同じURLを繰り返し取得する

レビュー7は上位8枚の集合で11,148行、30%が重複URLであると測定した。それにもかかわらず、レビュー8は`(source_url, alt_text)`による共有を第2段階gateの後へ置いている。寸法gateにはRange GETが必要なので、この順序では同じURLを複数resourceから繰り返し取得した後に重複をまとめることになる。

画像bytesの取得結果は`source_url`だけで共有できる。一方、LLMの分類とOCRはaltを入力に含むため、解析結果の共有単位は`(source_url, alt_text)`である。この二つを同じ単位として扱わないこと。

**採否: 採用。** planningでネットワークアクセス前に候補をまとめ、1回の実行中は`source_url`単位で取得結果を共有する。取得前の安全な名前gate、URL単位のheader取得と寸法gate、`(source_url, alt_text)`単位のLLM解析、各`resource_image`行への結果反映、の順にする。同じURLに異なるaltがある場合も画像取得は1回だけとする。

#### 高 — AM. gate後に上限8枚を適用すると、画像取得数の安全弁がなくなる

レビュー8の指摘Uは`max_images_per_resource`を第2段階gateの後に適用するとする。これでは、LLMへ渡す画像は8枚以下でも、どれを残すか決めるためにresource内の全候補へRange GETする可能性がある。元の上限が担っていた外部request数と処理時間の抑制が失われる。

**採否: 修正して採用。** 「gateのために取得してよい候補数」と「LLMへ渡してよい画像数」を別の上限として定義する。前者はネットワークアクセス前、後者はgate後に適用し、どちらも全記事共通のexecutorで守る。既定値は、AKのラベル付き測定で説明画像の取りこぼしとrequest数を比較して決める。`max_images_per_resource=8`がどちらを指すのかを最終案で曖昧にしない。

#### 中 — AN. header自前解析とRange GETの失敗時規則が不足している

レビュー11は先頭8KBで寸法を読めないJPEGが存在すると確認したが、必要な再取得範囲と打ち切り条件は未決定である。また、Rangeを無視して全体を返す応答、`Content-Range`がない応答、壊れたheader、redirect後の形式変化、未対応形式をどう扱うかも定義されていない。自前解析を「50行程度」と見積もることは仕様上の根拠にならない。

**採否: 修正して採用。** Pillowを初版へ追加しない方針は維持する。ただし、対応するMIMEとheader、8KBで不足した場合の追加取得上限、Range非対応時も`max_bytes`を超えて読まないこと、寸法不明を`ignored`にしないこと、未対応形式の状態を明記する。形式ごとの正常・切断・巨大寸法・アニメーションを含むfixtureで解析器を検証する。

#### 中 — AO. SVGの直接抽出は画像OCRより広い別機能である

SVGの`<text>`抽出には`text`要素だけでなく、`tspan`、表示順、transform、CSS、非表示要素、`foreignObject`、外部実体を無効化したXML解析を考慮する必要がある。文字がpath化されたSVGとの結果差も大きい。レビュー9の「OCRより正確」という主張は、これらを定義しない限り成立しない。

**採否: 保留。** 初版の範囲を画像OCRに保つならSVGは`ignored`とする。直接抽出を採用する場合は、安全なXML解析、抽出対象、読み順、文字を持たないSVGの扱いを独立した要件として追加する。単純な`//text()`相当だけを暗黙に実装しない。

#### 高 — AP. `ingest`と同じbackendを使う決定だけでは画像を渡せない

レビュー6の決定3は設定の選択元を確定したが、`codex-local`と`claude-code-local`を含む各backendが画像入力を受ける契約は未定義のままである。これは実装課題ではなく、commandが利用可能かを決める仕様上の境界である。

**採否: 採用。** `BackendCapabilities`に画像解析対応の有無を持たせ、`enrich-images`はplanning後・画像取得前にpreflightする。非対応backendでは一部処理を始めず、backend名を含む明確なエラーで終了する。初版で対応するbackendと、各backendへ渡す画像表現・構造化応答・usage取得方法を最終案で列挙する。`image_ocr`専用のbackend/model設定を作らないという決定は維持する。

#### 中 — AQ. 出力上限到達を終端失敗とする判定がbackend契約にない

レビュー11の指摘AH自身が確認したとおり、現行コードでは`max_output_tokens`到達と他の`LLMProtocolError`を区別できない。区別できない状態で前者だけを終端失敗と書くと、実装者によって全protocol errorが終端になったり、すべて一過性になったりする。

**採否: 修正して採用。** backend応答から上限到達を明示的に判定できる場合だけ`output_limit`として終端失敗にする。判定不能なprotocol errorは一過性として次回1回の再試行対象にする。OCRは`max_ocr_chars_per_image`まで先頭から転記して打ち切り、要約しない。prompt上の文字数上限と、reasoningを含む`max_output_tokens`の余裕をbackend契約へ含める。

#### 低 — AR. OCR変更後の選択modeは意味に合う名前が必要である

レビュー11の指摘AJが提案する条件は「OCRが変化したresource」だけではなく、model、prompt、schemaその他のcurrent request fingerprintに一致する完了結果がないresource全般を選ぶ。`--changed`では何が変わったものを指すのか曖昧である。

**採否: 修正して採用。** 選択条件自体は採用し、現在のrequest fingerprintに再利用可能な完了結果がないresourceを選ぶ一般的なmodeとする。CLI名は`--stale`を第一候補とし、`--dry-run`で対象件数・再利用件数・推定費用を確認できるようにする。OCRを持たないresourceでは現行requestと`source-note-v1`を維持し、OCRを持つresourceだけOCR用prompt shapeとversionを使う。

### レビュー13 — tsunyan (2026-08-25)

レビュー12が人間の判断を必要とした項目について、次のとおり決定した。決定は口頭で示され、Codexが記録した。

| 項目 | 決定 |
|---|---|
| AK. 画像gateの厳しさ | **厳しめにする。** このアプリは完全な収集や高い再現率を目的とせず、日常の使用感と不要な処理を減らすことを優先する。 |
| AM. gate対象数 | **抽出された候補をすべてgate判定する。** gate前にresource単位の候補数上限を設けない。 |
| AO. SVG | **XMLのtextを安全に直接抽出する。** |
| AP. 初版backend | **`openai-responses`、`codex-local`、`claude-code-local`の3つすべてに対応する。** |
| Q. model変更時の再解析 | **modelをOCRの再利用指紋から外す。** model変更だけでは再解析せず、必要な場合は`--force`で行う。 |
| AH. 画像単位のOCR上限 | **2,000文字。** 上限へ達した結果を永続化・集計し、後の調査、上限調整、再取得に使えるようにする。 |
| 初回大量実行 | **`--limit`を必須とし、全件には`--all`を要求する。** 実行開始時に残件数を明示する。費用は推定せず実測だけを報告する。 |

#### 決定の帰結

**AK（厳しめのgate）** — Feedianの「日常の利用価値を優先し、最後の数%を追わない」という設計原則を、この判定へ明示的に適用する。説明画像の取りこぼしが少数生じても、それだけでは欠陥としない。保存済み本文やOCR済み結果を失うことは引き続き許容しない。

初版の既定gateは、レビュー10の確定案を基礎に次のようにする。

- 取得前にURLまたはファイル名が`@2x`、`@3x`、`logo`、`icon`、`avatar`、`profile`、`button`、`btn`、`banner`、`badge`、`sprite`、`spacer`、`blank`、`emoji`、`favicon`を示す候補を`ignored`とする。
- 10以上のresourceに同じ`source_url`が現れる候補を、共通chromeである蓋然性が高いものとして`ignored`とする。閾値は設定可能にし、既定値を10とする。
- 取得後に短辺が200px未満のraster画像を`ignored`とする。
- 非画像MIME、TIFF、BMP、ICO、アニメーションを`ignored`とする。
- `max_bytes`または`max_pixels`の上限超過は、対象外判定ではなく資源保護上の`failed`とする。

判定理由を`ignored_reason`として保存し、理由別件数を終了時に報告する。後で閾値を評価できるようにするが、初版前に正解ラベル付き調査を必須とはしない。レビュー12の指摘AKは、この人間判断により「ラベル付き測定後に既定値を確定する」という条件を不採用とする。

**AM（全候補をgate）** — `extract_content_images`が返した全候補をplanning対象にし、名前gateで除外されなかったdistinct `source_url`をすべて取得・header判定する。resource単位の取得候補数上限は設けない。`max_images_per_resource=8`は、gateを通過した後にLLMへ渡す画像数の上限だけを意味する。8枚を埋めるために除外済み候補を復活させない。

取得と解析の共有順序はレビュー12の採否ALを維持する。取得bytesとheader判定は1実行内で`source_url`単位に共有し、LLM結果は`(source_url, alt_text)`単位に共有する。画像取得・gate・LLM解析は全resource共通のexecutorへ投入し、`image_ocr.workers=8`を既定とする。backend requestだけは各`BackendCapabilities.max_parallelism`も同時に守る。

**AO（SVG text抽出）** — `image/svg+xml`はLLMへ送らず、安全設定を施したXML parserで表示テキストを抽出する。外部実体、DTD、外部network accessを無効化し、入力byte上限をrasterと同様に守る。`text`と`tspan`を文書順に読み、空白を正規化して`ocr_text`相当として保存する。文字をpath化したSVGなど抽出可能なtextが無いものは`ignored`とし、`ignored_reason=svg_without_text`を保存する。`foreignObject`、CSSによる生成内容、pathの画像化は初版の対象外とする。レビュー12の指摘AOは採用へ変更する。

**AP（3 backend対応）** — `BackendCapabilities`へ画像解析対応を宣言するfieldを追加し、`openai-responses`、`codex-local`、`claude-code-local`は初版で対応を宣言する。3つとも、画像fileまたは画像bytes、untrustedなURL・alt、OCR用の構造化出力schemaを受け取る共通methodを実装する。local backendについても、画像を単なるpath文字列としてpromptへ置かず、各CLIが提供する画像入力手段で渡す。backendごとの認証、隔離、audit、usage取得、`max_parallelism`は既存契約を維持する。`manus-api`は初版では非対応とし、画像取得前のpreflightで終了する。

**Q（modelを指紋から外す）** — OCR再利用の入力指紋は、画像SHA-256、正規化したalt、backend、OCR prompt version、出力schema version、および解析結果を変える設定から作る。`llm.model`は含めない。実際に使用したmodelはauditへ保存する。model変更後に品質を比較したい場合だけ`--force`で再取得・再解析する。backend変更、prompt/schema変更、alt変更、画像SHA-256変更による再解析規則は維持する。

**AH（2,000文字と追跡）** — `image_ocr.max_ocr_chars_per_image`の既定値を2,000とする。OCRは要約せず、画像内の読み順で先頭から転記し、上限で打ち切る。`resource_image`には少なくとも`ocr_truncated`、実行時の`ocr_char_limit`、`analysis_backend`、`analysis_model`、`analyzed_at`を保存する。終了時に`ocr_truncated=<件数>`を報告し、該当行をDBから列挙できるようにする。

設定した上限を後で引き上げた場合、`ocr_truncated=1`かつ保存済み`ocr_char_limit`が新しい上限より小さい行だけを既定の再解析対象へ戻す。切り詰められていない完了結果を上限変更だけで再解析しない。画像bytesは保存しないため、再解析時は保存済み`source_url`から再取得する。取得不能なら既存の切り詰め済みOCRをcurrent結果として残し、失敗した試行を別に記録する。

**初回大量実行** — `feedian enrich-images`は`--limit N`と`--all`を相互排他的な必須選択肢とし、どちらも無い場合は処理を開始せずusage errorにする。`--limit`はresource数を制限し、値は正の整数とする。`--all`はその時点の既定対象を全件処理する明示的な選択とする。`--dry-run`でも同じ選択を必須とする。

外部requestを始める前に、少なくとも次を表示する。

- 既定対象として残っているresource総数
- 今回選択したresource数
- 選択resource内の候補画像行数
- 取得前gate後のdistinct `source_url`数
- 指紋一致で処理不要な件数と、今回処理予定の解析group数

寸法gateは取得後でなければ結果が分からないため、開始時の数は上限として表示し、確定値は終了時に報告する。`estimated_cost_usd`と`max_cost_usd`は実装しない。終了時にはbackendが返した実測のinput/output token、課金額、unpriced/unmetered件数だけを報告する。

#### 記録時の補足

Qの「解析結果を変える設定」にはOCR promptと出力schemaのversionを含むが、`max_ocr_chars_per_image`は含めない。文字数上限の変更はAHの専用規則で扱い、上限を引き上げたときに切り詰め済みの行だけを再解析する。これにより、切り詰められていない完了結果まで指紋不一致になることを防ぐ。

### レビュー14 — Claude Code (2026-08-25)

レビュー12の採否はおおむね私の元案より正確である。特にALは私の誤りを正しており、AQとANも曖昧に残した部分を埋めている。同意点は末尾にまとめた。

レビュー13の決定のうち、**APはlocal backendでの所要時間が現実的でない**。またAMの帰結が測られていない。

なお本節の指摘ASは初稿で誤った根拠に基づいており、書き直した。経緯は当該節に残してある。

#### 高 — AS. 決定APの費用は隔離ではなく所要時間である。local backendは1画像1 processで直列に走る

**この節は初稿で誤った根拠を書いたため、書き直した。** 当初は`CODEX_DISABLED_FEATURES`（`feedian/llm_backends.py:238-252`）に含まれる`view_image`を「画像入力手段の無効化」と読み、決定APが`DESIGN.md:8`の隔離前提を崩すと主張した。これは誤りである。`view_image`はagentが自ら呼んでlocal fileを文脈へ読み込むツールであり、denylistが閉じるべきものはまさにそれである。Feedianが選んだ画像1枚をrequestに添えることは別の経路であり、`view_image`を無効にしたまま成立する。無効のまま画像を渡すのが**正しい姿勢**であって、緩和ではない。

「どちらのbackendもpromptを1つの文字列引数として受け取る」も誤りである。両者とも**stdin**で渡している（`stdin_text=prompt`、`feedian/llm_backends.py:499`と`:854`。codexは引数末尾の`-`がstdinを指す、`:486`）。

検証できる形の反論は別にある。**local backendは`max_parallelism=1`である。** `CodexLocalBackend`は`BackendCapabilities`の既定値1をそのまま使い（`feedian/llm_backends.py:92`, `:307-314`）、`ClaudeCodeLocalBackend`は明示的に1を設定している（`feedian/llm_backends.py:650`）。§3の実効並列数は`min(image_ocr.workers, backend.capabilities.max_parallelism)`なので、local backendでは**常に1**になる。

そして解析1件ごとにCLI processを1つ起動する。指摘ATの測定では、名前gateと頻度gateを通過した解析group（`(source_url, alt_text)`単位）が50,509件あり、寸法gateを通してもおそらく2万件台が残る。

```text
1件あたり 5秒  →  27,000件 ×  5秒 ÷ 1並列 ≈ 37時間
1件あたり15秒  →  27,000件 × 15秒 ÷ 1並列 ≈ 112時間
```

local agentの起動はAPI呼び出しより重く、実測なしに5秒側を仮定する根拠は無い。**`--limit`必須の決定があるので破綻はしないが、local backendでのフル解析は現実的な選択肢ではない。** 最終案はこれを費用として明記すること。

併せて、決定APの「各CLIが提供する画像入力手段で渡す」という一文は、仕様としては不足している。**各CLIの実際の画像入力flagと、それが動作を確認できたCLI versionを列挙すること。** この repository は既に`CODEX_VERIFIED_VERSIONS = ("0.147.0",)`でversionを固定し、preflightで照合している（`feedian/llm_backends.py:265`, `:378-382`）。画像入力も同じ扱いにすべきである。flag名を確認できないまま「対応する」と書くと、実装時に初めて成立しないことが判る。

なお、参照Vaultの現在の設定は`llm.backend = "openai-responses"`であり、`max_parallelism=8`である。決定APを維持しても、この利用者の実運用は影響を受けない。

#### 高 — AT. 決定AMの帰結 — `--all`は約50,000回のRange GETになる

決定AMは「抽出された候補をすべてgate判定する。gate前にresource単位の候補数上限を設けない」とした。その規模を測った。

```text
全候補行                     102,145
distinct source_url           62,438
  名前gate後                  50,881   (−18.5%)
  + 頻度gate(10 resource以上) 50,411   ( −0.9%)
```

**`--all`は約50,400回の外部HTTP requestになる。** 金銭費用ではなく、第三者サイトへのcrawlとしての時間と礼儀の問題である。`image_ocr.workers=8`で1件0.3秒と仮定しても50,411 × 0.3 ÷ 8 ≈ 31分で、0.3秒は楽観的な値である。失敗とtimeoutを含めればさらに延びる。`--limit`を必須にした決定はこれを緩和するが、**規模そのものを§7へ書いておくこと。** 単一hostへの集中を避ける配慮も要る。

**併せて、頻度gateの位置づけを書き直す必要がある。** 採否ALで重複排除を取得前へ移した結果、頻度gate（10 resource以上に現れるURL）が減らすdistinct URLは470件、0.9%にすぎない。レビュー7では8,601「行」を占めると測ったが、行の重複は取得前のdedupが既に処理する。**fetch回数の削減策としても、解析group数の削減策としても、もはや効果はほとんど無い。**

ただし**規則そのものは残すべきである。** 名前gateと200px下限の両方を通り抜ける第三者widgetのサムネイル——レビュー10で観測した`https://b.hatena.ne.jp/entry/image/...`形式が典型で、545 resourceずつ現れる——を捉える規則はこれしか無い。価値は費用ではなく**精度**にあり、8枠を占有する行（レビュー7の測定で8,601行）を本物の説明画像へ明け渡す点にある。最終案では、費用削減策ではなく精度のための規則として理由を書き直すこと。

判定の母集団も定義が要る。**閾値は現在のrevisionに属する候補全体に対して数えること。** 選択したbatch内で数えると、`--limit`と`--all`で同じURLの判定が変わり、同じ入力に対する結果が実行の切り方に依存する。

#### 中 — AU. `max_images_per_resource`とcross-resource解析groupの関係が未定義

決定AMは`max_images_per_resource=8`を「gateを通過した後にLLMへ渡す画像数の上限」と定めた。一方、採否ALにより解析の単位は`(source_url, alt_text)`のgroupであり、**1つのgroupは複数resourceにまたがる**。

resource Aがgateを12枚通過し8枚に絞られるとき、外れた4枚が他resourceと共有するgroupだったらどうなるか。そのgroupは別resourceのために解析される。得られた結果をAの行へ書くのか、書かないのか。

問題は実行順ではなく、**batchの構成に依存すること**である。Aの9枚目に`ocr_text`が入るかどうかが、同じ`--limit`のbatchにresource Bが入っていたかどうかで変わる。同じVault、同じ画像に対して、`--limit 10`を繰り返した場合と`--all`を1回実行した場合とで、保存される内容が違ってくる。

**解析結果はgroupの全行へ書くことを推す。** そのgroupは既に解析されており、費用は支払い済みである。書かずに捨てる理由が無い。そのうえで、**8枚の上限はingestがOCRを読み出す側でposition順に適用する**（§8）。こうすると保存内容がbatchの構成に依存せず、支払った結果も捨てずに済む。§2 L105の「positionの小さいものから採用する」は、保存時ではなくingestの読み出し時の規則として書き直すこと。

#### 中 — AV. SVG経路は寸法gateも`image_kind`も通らない

決定AOによりSVGはLLM経路を完全に外れ、XMLからのtext抽出へ回る。ここに2つの穴がある。

**1つ目。寸法gateが適用されない。** SVGはvector形式なのでraster用のheader解析では寸法を取れない（実測でも20件すべて取得できていない）。16pxのSVG iconもtext抽出へ入る。多くは`svg_without_text`で`ignored`になるが、`<text>`にサイト名を持つlogo SVGは`ocr_text`にゴミを入れる。root要素の`width` / `height`、無ければ`viewBox`から寸法を読み、rasterと同じ下限を適用すること。XML解析は決定AOでどのみち実装するので追加費用はほぼ無い。

**2つ目のほうが重い。LLMを呼ばないので、`image_kind`を決める主体が存在しない。** §8がingestへ渡す条件は`image_kind = explanatory`かつ`ocr_text`が非空である。SVG経路はこの分類を誰も行わないため、**`<text>`を持つSVGは、それが説明図であろうとlogoであろうと、無条件に要約入力へ入る。**

SVG経路について、§5の現在値をどう埋めるかが未定義である。

```text
analysis_status              completed でよいか
image_kind                   誰が何を根拠に決めるか
ocr_llm_run_id               外部実行が無い。NULL か、抽出を記録する別の行か
analysis_backend / model     決定AHで追加が決まったが、backendを呼んでいない
analysis_input_fingerprint   backend + prompt version + schema version が意味を持たない
```

最終案で埋めること。素案としては、`image_kind='explanatory'`固定（textを持つSVGは図表とみなす）、`ocr_llm_run_id=NULL`、backend/model欄は空、指紋は「SVG抽出器のversion + SHA-256 + alt」とする。いずれにせよ、raster経路の指紋定義をそのまま流用すると意味を成さない。

#### 低 — AW. §5の列定義が9ラウンド分の決定から乖離している

§5の表は草案のままだが、以降の決定で次が変わっている。

```text
追加   ignored_reason                              （決定AK）
追加   ocr_truncated / ocr_char_limit              （決定AH）
追加   analysis_backend / analysis_model           （決定AH）
追加   最後に試したtarget fingerprint / status / 時刻（採否D）
削除   asset_id                                    （採否2・採否7）
変更   analysis_input_fingerprint の定義からmodelを除外（決定Q）
```

最終案では列定義を1箇所へ集約すること。現状は決定が7つの節へ散っており、実装者が§5だけを読むと誤る。

#### 同意した点

- **ALは私の誤りである。** レビュー8とレビュー10で重複排除を第2段階gateの**後**に置いたのは間違いだった。寸法gateがRange GETを要する以上、その順序では同じURLを複数resourceから繰り返し取得したうえで重複をまとめることになる。取得の共有単位は`source_url`、解析の共有単位は`(source_url, alt_text)`という分離も正しい。採否ALの順序をそのまま採る。
- **AKの方法論批判は正当である。** レビュー10の測定は寸法と形式の分布を示しただけで、説明画像の取りこぼし率を検証していない。標本も1 hostあたり6件に制限し、解析に成功した501件を分母にしている。その割合をVault全体へ外挿できないという指摘はそのとおりで、「200px四方を切ることは実質的にない」と断定したのは行き過ぎだった。決定AK（厳しめ、ラベル付き調査を必須としない）は人間がその判断を引き取ったものとして支持する。`ignored_reason`を保存して後から評価できる設計になっているのがよい。
  - 事実として1点だけ記録する。名前gateの中で`@2x` / `@3x`が最も誤爆しやすい。retina表示用のscreenshotは`screenshot@2x.png`という名前を取り得るためで、Codexの指摘は的を射ている。実測では2,712行(2.7%)が該当する。`ignored_reason`で後から評価できるので初版で残す判断に異論は無いが、**最初に見直す候補**として記録しておく。
- **AQは私のAHより正確である。** 「判定不能なprotocol errorは一過性として次回1回の再試行対象にする」は、私が曖昧に残した部分を安全側で正しく埋めている。
- AN、AR、および決定AH（2,000文字、`ocr_truncated`の永続化、上限引き上げ時に切り詰め済み行だけを再解析）、決定Q（modelを指紋から外す）、`--limit` / `--all`の必須化と費用の実測報告は、いずれも妥当である。

### レビュー15 — Codex (2026-08-25)

#### 結論

レビュー14のAS〜AWはいずれも事実関係と設計上の穴を正しく指摘している。ただしASとATの規模は、レビュー13で人間が決定した「3 backend対応」と「全候補をgate判定する」を自動的に覆す理由ではない。`--limit`を必須にした目的も、まさに大規模処理を利用者が明示的に分割できるようにすることにある。決定を維持し、実行時間の明示、接続先hostへの配慮、batchに依存しない結果保存を追加する。

#### AS. local backendの所要時間 — 採否: 修正して採用

`codex-local`と`claude-code-local`を初版で対応する決定APは維持する。ただし、対応とはAPI backendと同程度のthroughputを保証する意味ではない。local backendの`max_parallelism=1`を守り、開始時のplanへ実効LLM並列数を表示する。`--all`選択時には、直近の同一backendにおける画像解析の実測時間があれば残り時間の参考値を表示し、実測が無ければ所要時間不明と表示する。費用推定は行わない。

各local CLIの画像入力flagと対応versionは、実装前に実CLIで確認し、既存のverified versionと同じくpreflight対象にする。画像入力を確認できないversionは、backend自体を暗黙にtext-onlyへ落とさず、`enrich-images`について非対応として画像取得前に終了する。agentの`view_image` toolは引き続き無効のままとし、Feedianが選択した1画像だけをCLIの明示的な画像入力へ渡す。

#### AT. 約50,000回のRange GET — 採否: 採用

全候補をgate判定する決定AMは維持し、参照Vaultでは`--all`が約50,400件のdistinct URL取得になり得ることを最終案の運用上の注意へ記載する。`--limit`はresource数を制限するため、通常運用では小さいbatchに分ける。`--all`はこの規模を理解した利用者が明示的に選ぶ操作とする。

画像取得は全体で`image_ocr.workers=8`を上限とし、同一hostには同時に1 requestだけ送る。HTTP 429と`Retry-After`を尊重し、一過性失敗の次回1回再試行という決定Jを維持する。開始時にはdistinct URL総数に加え、request数の多いhostを上位から表示する。host別の固定sleepやrobots.txt処理は、記事自身が参照する画像を利用者の明示操作で取得する初版の範囲には追加しない。

頻度gateの判定母集団は、選択batchではなくDB上のcurrent revisionに属する候補全体とする。同じ入力の判定を`--limit`値に依存させない。このgateはdistinct fetch数の削減策ではなく、共通chromeが記事ごとのOCR枠を占有しないための精度規則として位置付ける。

#### AU. resource上限と共有group — 採否: 採用

解析済みの`(source_url, alt_text)` groupの結果は、選択batch外を含む該当`resource_image`全行へ反映する。`--limit`は新たに解析を開始するresourceの選択上限であり、同じ解析結果の行への伝播上限ではない。伝播先で追加の画像取得やLLM requestは発生させない。終了時に`propagated_rows`と`propagated_resources`を報告し、`--limit`を超えるresourceの行が更新され得ることをusageとplanへ明記する。

`max_images_per_resource=8`は保存件数の上限ではなく、`ingest`が要約requestへOCRを組み込む際の読み出し上限とする。`image_kind='explanatory'`かつ`ocr_text`が非空のcurrent結果を`resource_image.position`順に最大8枚使う。これにより、同じgroupの結果を既に得ているのにbatch構成だけを理由として捨てることを防ぐ。

#### AV. SVG経路 — 採否: 修正して採用

SVGにも取得前の名前gateと頻度gateを適用する。XML rootの有限な数値の`width` / `height`を優先し、単位無しまたは`px`を受け入れる。取得できない場合は有限で正の`viewBox`幅・高さを使う。短辺200px未満は`ignored_reason=small_dimensions`、寸法を安全に確定できないものは厳しめのgate方針に従い`ignored_reason=svg_unknown_dimensions`とする。

gateを通過して抽出可能な`text`を持つSVGは、LLM分類を行わないため`image_kind='explanatory'`、`analysis_status='completed'`として保存する。`ocr_llm_run_id`、`analysis_backend`、`analysis_model`はNULLとし、別途`analysis_method='svg_text'`を保存する。指紋は画像SHA-256、正規化alt、SVG抽出器version、出力schema versionから作り、raster LLM経路のbackend/prompt指紋を流用しない。抽出器の監査情報はLLM runではなく画像解析の試行記録へ保存する。

#### AW. 永続化定義の乖離 — 採否: 採用

最終案では、草案§5とレビューで追加・削除・変更された列を統合したcurrent schemaを1つの表にまとめる。少なくともレビュー14のAWが列挙した変更に加え、`analysis_method`、一過性／終端の`failure_kind`、再試行済み状態、SVG抽出器versionを含める。`asset_id`と`use_for_summary`は初版schemaに含めない。

#### 人間判断の扱い

ASとATはレビュー13の決定後に得られた新しい規模の根拠であるため、判断を変える場合は人間がレビューを追加する。変更しない限り、3 backend対応、全候補gate、`--limit`必須、全件には`--all`必須という現在の決定を確定候補として扱う。AU、AV、AWは決定済み方針から一意に導ける実装詳細であり、追加の人間判断を要しない。

### レビュー16 — Claude Code (2026-08-25)

最終案を全体としてレビューした。14ラウンドの決定はほぼ完全に反映されている。特に、prompt versionを3箇所すべてで一致させる指定、共通instructionsのbyte単位維持、上限8枚を読み出し側へ移した定義、SVG経路の列をNULLと専用指紋で埋めた点、頻度gateの母集団をbatchでなくcurrent revision全体としたこと、実装順序でSourceノートを先頭に置いたこと、推定費用を作らない判断は、いずれも議論の結論を正しく落とし込んでいる。

以下は最終案そのものに対する新しい指摘である。うち2件は実測に基づく。

#### 高 — AX. 名前gate通過URLの47.5%が単一hostに集中し、「同一host 1並列」と組み合わさると`--all`が数時間になる

最終案は「global最大8並列」と「同一hostには同時に1 requestだけ」を併記する(L119)。後者は正しい配慮だが、host分布と組み合わせた帰結が測られていない。

```text
名前gate後のdistinct URL   50,881    distinct host  991

  24,144 (47.5%)  b.hatena.ne.jp
   7,408 (14.6%)  pbs.twimg.com          ← 上位2 hostで62.0%
     920 ( 1.8%)  cdn-ak.f.st-hatena.com
     714 ( 1.4%)  cdn.image.st-hatena.com
     701 ( 1.4%)  image.slidesharecdn.com
```

**`b.hatena.ne.jp`だけで24,144件が直列になる。** global並列数8は無関係で、この1 hostが全体の所要時間の下限を決める。

```text
1件 0.3秒 → 24,144 × 0.3 ÷ 1 ≈ 2.0時間
1件 1.0秒 → 24,144 × 1.0 ÷ 1 ≈ 6.7時間
```

レビュー15の採否ATを受けて、最終案は開始時に「request数の多いhost」を表示する(L209)。表示は入ったが、**そこから導かれる帰結が書かれていない。** L212は「約50,400件のdistinct URL取得になり得る」と件数だけを注意として挙げ、**所要時間を決めるのはworker数ではなくhost集中である**ことに触れていない。利用者は上位hostの一覧を見せられても、それが2時間を意味するのか6時間を意味するのか判断できない。

加えて、単一の第三者hostへ24,144 requestを送ることは、礼儀の面でも見過ごせない。レビュー15は「host別の固定sleepやrobots.txt処理は初版の範囲に追加しない」としたが、その判断は上位hostが全体の数%を占める場合を想定したものだろう。47.5%という実測はその前提と異なる。

指摘AYを採用すればこの集中はほぼ解消する。採用しない場合は、host別の所要時間見積りを開始時の表示へ加え、最大hostの件数を明示すること。

#### 高 — AY. 頻度gateを (host + path接頭辞) 単位にすると、除外が470件から30,586件になる

最終案L67の頻度gateは「同じ`source_url`が10以上のresourceに現れる」という**完全一致**の規則である。これを (host + 最終segmentを除いたpath) 単位へ一般化した場合を測った。

```text
名前gate後 distinct URL                     50,881

  現案（同一URL単位、閾値10）    除外    470   残り 50,411
  (host + path接頭辞)単位、閾値10 除外 30,586   残り 20,295
```

template単位の上位は次のとおりである。

```text
resource数 / URL数
   546 /  23,836   b.hatena.ne.jp/entry/image/https://anond.hatelabo.jp
 1,797 /   4,730   pbs.twimg.com/media
   679 /       2   anond.hatelabo.jp/assets/images
   672 /       1   anond.hatelabo.jp/assets/images/common
   456 /       1   s.tgstc.com/static/web
```

最上位のtemplateが、指摘AXのhost集中そのものである。**546 resourceにまたがる23,836件の異なるURL**で、はてなブックマークのwidgetが挿入するサムネイルである。URLが1件ずつ異なるため、完全一致の頻度gateでは1件も捕まえられない。同じ理由で名前gateも200px下限も通過する。

template単位へ一般化すれば、この23,836件が1つの規則で落ち、AXの所要時間問題も同時に解消する。

**ただし誤爆の型を明示しておく必要がある。** 同じ規則は、よく読むサイトが記事画像を共通CDN pathから配信している場合にも当たる。`pbs.twimg.com/media`（1,797 resource / 4,730 URL）は判断が分かれる例で、記事に埋め込まれた引用tweetの画像は説明画像でありうる。

決定AK（厳しめ、取りこぼしを許容する）に照らせばtemplate単位への一般化を推すが、これは新しい規則であり、除外量が65倍になる。**採否は人間の判断とする。** 採用する場合も、閾値は設定可能なまま残し、`ignored_reason`をtemplate単位と分かる値にして後から評価できるようにすること。

#### 中 — AZ. SSRF対策が最終案の本文から落ちている

草案§2は明示していた。

> 最初のURLと全redirect先を検証し、許可されていないprivate address、`data:`、`blob:`、非HTTP(S) schemeを拒否する。Browser、JavaScript、Service Workerは使わない。

最終案の画像gate節(L62-75)にはこの記述が無い。残っているのは実装順序の「NetworkPolicyを共有する画像取得」(L311)と検証項目1(L286)だけである。

検証項目に書いてあれば実装されるという前提は取れない。**要件は本文に書くこと。** Browser・JavaScript・Service Workerを使わないという制約も、最終案からは完全に消えている。画像取得はHTTPだけで行うと明記すること。

#### 中 — BA. SHA-256変更で旧OCRを要約から外す状態が定義されていない

L181はこう定める。

> 画像bytesのSHA-256が変わったことまで確認できた場合は旧OCRを`ingest`へ渡さないが、監査のため保存値を物理削除しない。

一方L216の読み出し条件は`analysis_status='completed'`、`image_kind='explanatory'`、`ocr_text`が非空である。渡さないためにはこのいずれかが偽になる必要があるが、`ocr_text`は残すと決めているので、変わるのは`analysis_status`か`image_kind`しかない。そして`analysis_status`の値域はL139で`pending` / `completed` / `ignored` / `failed`と定義されている。

草案§5にあった状態と`ocr_text`の対応表は最終案では落ちているため、この状態をどう表すかが決まっていない。`stale`のような値を値域へ足すのか、現在値とは別のflagで表すのかを明記すること。値域を変えるならL139の表とmigrationの両方に反映が要る。

#### 低 — BB. `--limit N`が選ぶresourceの順序が未定義

L193-200は通常の対象条件を列挙するが、そこから何件をどの順で取るかを定めていない。`ORDER BY`を指定しなければSQLiteの行順は不定であり、同じVaultに対する2回の実行が異なるresourceを選びうる。

`ingest`側で同じ問題があり、`_source_rows`の`created_at ASC`のために`--limit`が「変更のあったN件」ではなく「最も古いN件」を取っていた（指摘AJ）。同じ轍を踏まないよう、順序を明記すること。段階実行の用途では**新しい順**が実用的だと考える。

#### 低 — BC. L212が参照Vaultの数値をCLIの表示内容として書いている

> 参照Vaultで`--all`は約50,400件のdistinct URL取得になり得ることを運用上の注意として表示する。

commandが表示すべきは、その利用者のVaultについて計算した件数である。参照Vaultの実測値は仕様の根拠として本文に残す意味はあるが、CLIの出力内容として指定するものではない。文言を分けること。

#### 低 — BD. `--force`とgateの関係が未定義

L200は「`--force`は選択resourceの候補を再取得・再解析する」とだけ書く。名前gateと頻度gateも無視するのか、gateは常に適用されるのかが読み取れない。

gateで落とした画像は`--force`でも復活しない、と明記することを推す。gateは費用ではなく「説明画像かどうか」の判定なので、再実行で結論が変わる理由が無い。

#### 低 — BE. `image_ocr.max_images_per_resource`は`enrich-images`に影響しない

L49、L58、L216により、この設定はingestが要約へ読み出す枚数の上限であり、`enrich-images`の取得数にも保存件数にも影響しない。L58が明記しているので誤解の余地は小さいが、`image_ocr.`という接頭辞は`enrich-images`側の設定に見える。設定名か配置を見直す余地がある。

#### 補足

L101の「backend応答から出力上限到達を明示的に判定できる場合だけ`output_limit`として終端失敗にする」は、現行コードに応答の`status`や`incomplete_details`を読む処理が無いことを前提にすると、その解析の追加を含む。実装順序の手順4へ明示しておくと漏れない。

### レビュー17 — tsunyan (2026-08-25)

レビュー16のAXとAYについて、次のとおり決定した。決定は口頭で示され、Claude Codeが記録した。

| 項目 | 決定 |
|---|---|
| AX. 同一hostの直列化 | **host単位で直列化しない。** 高々10以下の同時requestで問題にするhostは考慮しない。HTML取得と同じ扱いとする。 |
| AY. 頻度gateの一般化 | **filterで落とせるなら落としてよい。** ただし規則の内容を分かりやすく示すこと。 |

#### 決定の帰結

**AX** — 最終案L119の「同一hostには同時に1 requestだけ送る」を削除する。画像取得の並列制御は`image_ocr.workers=8`のglobal上限だけとし、これは`fetch.workers=8`によるHTML取得と同じ方針である。

HTTP 429と`Retry-After`の尊重は維持する。これは「hostが実際に苦情を述べた場合の応答」であって、事前の自主規制とは別の話である。同様に、開始時に上位hostを表示する指定（L209）も情報としては残してよいが、所要時間の制約要因ではなくなる。

レビュー16の指摘AXが示した2〜6.7時間という下限は、この決定により消える。所要時間はglobal並列数だけで決まる。

**AY** — 頻度gateを完全一致URLから (host + 最終segmentを除いたpath) 単位へ一般化する。閾値の具体値はレビュー18の測定に基づいて決める。

### レビュー18 — Claude Code (2026-08-25)

#### AYの規則を平易に述べる

現在の頻度gateは「**まったく同じURL**が10以上の記事に出てきたら、それは記事固有の画像ではなくサイトの部品だろう」という規則である。

これが効かない相手がいる。はてなブックマークのwidgetは、記事ごとに違うURLを作る。

```text
https://b.hatena.ne.jp/entry/image/https://anond.hatelabo.jp/20260812100926
https://b.hatena.ne.jp/entry/image/https://anond.hatelabo.jp/20260811173015
                                    ~~~~~~~~~~~~~~~~~~~~~~~~~ ここが記事ごとに違う
```

同じ部品なのにURLが全部違うので、完全一致の規則は1件も捕まえられない。名前gateにも200px下限にも引っかからない。**これが23,836件ある。**

提案は、URLの末尾（ファイル名に当たる部分）を落として、**置き場所**だけで数えることである。

```text
比較する部分:  b.hatena.ne.jp/entry/image/https://anond.hatelabo.jp
無視する部分:  /20260812100926
```

置き場所が546記事にまたがっていれば、そこに置かれる画像は記事固有ではない、と判定する。1つの規則で23,836件が落ちる。

#### 高 — BF. 閾値10は本物の説明画像を巻き込む。100を推す

一般化そのものは正しいが、**レビュー16で挙げた閾値10は行き過ぎである。** 閾値10で落ちるtemplateを実際に列挙したところ、明確な部品と、本物の記事画像が混在していた。

```text
resource    URL  template                                     判定
     546  23836  b.hatena.ne.jp/entry/image/...               部品（はてブwidget）
    1797   4730  pbs.twimg.com/media                          判断が分かれる（引用tweet）
      93    281  m.media-amazon.com/images/I                  部品（affiliate商品画像）
      61    268  article-image-ix.nikkei.com                  ★本物（日経の記事画像）
      56    148  ecx.images-amazon.com/images/I               部品
     106    126  pbs.twimg.com/tweet_video_thumb              部品
      17    114  qiita-user-contents.imgix.net                ★本物（Qiitaの図・コード画面）
      45    113  images-fe.ssl-images-amazon.com/images/I     部品
      10    112  s.st-hatena.com                              部品
     133    103  b.st-hatena.com/images/users/gif/normal      部品（ユーザーicon）
      19     82  assets.st-note.com/img                       ★本物（note記事画像）
```

**Qiitaのuser contentsは、この機能が最も狙っている種類の画像である。** コード画面や構成図が置かれる場所で、17記事に現れるだけで閾値10に引っかかる。日経とnoteも同様に記事画像である。

閾値を上げると分離できる。

```text
閾値    除外URL    残り    落ちてしまう本物
  10     30,586  20,295    Qiita, 日経, note
  50     29,564  21,317    日経
 100     28,813  22,068    （なし）
 200     28,570  22,311    （なし）
 500     28,569  22,312    （なし）
```

**閾値100を推す。** 閾値10の効果の94%（28,813 / 30,586）を得ながら、Qiita・日経・noteの記事画像を全て残す。100と500でほとんど差が無いので、100は安全側に十分な余裕がある。

なお546を超える閾値にすると、最大の標的であるはてブwidget（546 resource）自体が残ってしまう。**有効な範囲は100〜500**であり、その中では小さい方を取る理由が無い。100と200の差は243件にすぎないので、100でよい。

`image_ocr.shared_url_resource_threshold`の既定値を10から100へ変更し、意味を「同じURL」から「同じ置き場所」へ改める。設定名も`shared_path_resource_threshold`のように実態へ合わせること。

#### 中 — BG. `pbs.twimg.com/media`だけは判断が残る

閾値100でも`pbs.twimg.com/media`（1,797 resource / 4,730 URL）は除外される。記事に埋め込まれたtweetの画像である。

引用tweetの画像は、多くは会話や写真だが、図表のscreenshotをtweetしたものが記事の説明として引用されている場合もある。決定AK（厳しめ、取りこぼしを許容する）に照らせば除外でよいと考えるが、4,730件は無視できない量なので記録しておく。

`ignored_reason`に置き場所単位の判定であることが分かる値を保存すれば、後から件数を数えて再評価できる。初版は除外のまま進めてよい。

#### 決定AXとBFを合わせた規模

```text
名前gate後のdistinct URL                50,881
  置き場所gate（閾値100）後             22,068
  → 短辺200px下限でさらに約半分が落ちる

所要時間: host直列化が無くなったので global 8並列だけで決まる
  22,068 ÷ 8 × 0.3秒 ≈ 14分
  22,068 ÷ 8 × 1.0秒 ≈ 46分
```

レビュー16のAXが示した2〜6.7時間は、決定AXとBFの両方によって解消する。最終案L212の「約50,400件」という運用上の注意も、22,068件へ更新すること。

### レビュー19 — tsunyan (2026-08-25)

`https://b.hatena.ne.jp/entry/image/https://...` 形式のURLははてなブックマークのブックマーク数画像であり、**全て捨ててよい**。頻度による判定ではなく、`https://b.hatena.ne.jp/entry/image/https://` のようなURL接頭辞で名指しして除外する。

決定は口頭で示され、Claude Codeが記録した。

### レビュー20 — Claude Code (2026-08-25)

#### 実測 — 接頭辞1本で24,131件

名前gate後のdistinct URL 50,881件に対し、`^https?://b\.hatena\.ne\.jp/entry/image/` は**24,131件**に一致する。レビュー18で測った23,836件はanond宛の分だけで、他サイト宛を含めるとこの数になる。除外後の残りは26,750件である。

`b.hatena.ne.jp`配下の他のpathも確認したが、同種のものはごく少数である。

```text
 24,131  b.hatena.ne.jp/entry/image     ← ブックマーク数画像
      4  b.hatena.ne.jp/bc/…            ← 旧形式のブックマーク数画像
      5  b.hatena.ne.jp/images/…        ← サイト部品
```

`b.hatena.ne.jp/bc/`も同じブックマーク数画像なので、併せて接頭辞へ含める。

#### 高 — BH. この接頭辞規則により、頻度gateは不要になる

レビュー18で閾値100の頻度gate（置き場所単位）を推したが、**接頭辞規則を入れるなら頻度gateは要らない。** 撤回する。

接頭辞で24,131件を落とした後、残る26,750件のうち明らかな部品は次で、合計約1,000件・3.7%にすぎない。

```text
647  Amazon商品画像（m.media-amazon / ecx.images-amazon / images-fe / images-na）
215  はてな静的資源（s.st-hatena.com、b.st-hatena.com/images/users/gif/normal）
126  pbs.twimg.com/tweet_video_thumb
```

一方、同じ規模帯には本物が並んでいる。

```text
4,730  pbs.twimg.com/media                  判断が分かれる（引用tweet）
  268  article-image-ix.nikkei.com          本物
  200  image.slidesharecdn.com/…            本物（スライド画像）
  116  www.ooedoonsen.jp/wp-content/uploads 本物
  114  qiita-user-contents.imgix.net        本物
  108  chart.apis.google.com                本物。文字どおりグラフ画像である
```

`chart.apis.google.com`が象徴的である。**Google Chart APIが生成したグラフそのもの**で、この機能が最も読みたい種類の画像でありながら、置き場所は多数の記事に共有される。頻度という指標では部品と区別できない。

したがって、残り3.7%の部品を落とすために閾値を下げれば、必ず本物を巻き込む。**頻度gateは、割に合う閾値が存在しない。**

**最終案から頻度gateを削除することを推す。** 具体的には次が消える。

- `image_ocr.shared_url_resource_threshold`の設定（L54）。
- 画像gate手順2（L67）。
- planning時にcurrent revision全体で出現resource数を数える処理と、その母集団をbatchに依存させないという指定（レビュー15のAT、レビュー16のBB周辺）。
- 閾値の調整という運用上の宿題。

代わりに置くのは、**既知のwidget・商用endpointを名指しする短い接頭辞listである。**

```text
https?://b.hatena.ne.jp/entry/image/     ブックマーク数画像
https?://b.hatena.ne.jp/bc/              旧形式のブックマーク数画像
```

`AGENTS.md`の「共通ケースを扱う単純な規則が、正確だが新しい状態を要する規則に勝る」に素直に当たる。頻度gateは新しいDB集計と閾値という状態を必要とするのに対し、接頭辞listは静的な文字列照合で済む。listの保守が要るという欠点はあるが、実測では1本で全体の47%を処理できている。

#### 接頭辞規則の値打ちは「取得しないこと」にある

ブックマーク数画像は小さいので、仮に取得すれば短辺200px下限でどのみち`ignored`になる。接頭辞規則の効果は分類精度ではなく、**24,131回のHTTP取得を発生させないこと**である。

この点で、削除する頻度gate（完全一致URL単位）とは価値の出どころが違う。レビュー15は頻度gateを「fetch削減ではなく精度規則」と位置付け直したが、接頭辞規則は逆に純粋なfetch削減策である。取得前gateとして名前gateと同じ位置に置く。

#### 中 — BI. `pbs.twimg.com/media`の判断は残る

4,730件で、接頭辞規則にも短辺200px下限にもかからない可能性が高い。記事に埋め込まれたtweetの画像である。

頻度gateを削除すると、これを落とす手段は接頭辞listへ`https?://pbs.twimg.com/media/`を加えることになる。閾値の副作用が無いぶん、判断としてはむしろ明快になる。

引用tweetの画像は多くが会話や写真だが、図表のscreenshotが記事の説明として引用される場合もある。決定AK（厳しめ）に照らせば除外でよいと考えるが、4,730件は無視できないため人間の判断とする。`ignored_reason`で後から件数を数えられるようにしておけば、初版で除外して後から戻すこともできる。

#### 決定AXとBHを合わせた規模

```text
名前gate後のdistinct URL                     50,881
  接頭辞gate（はてブ）後                     26,750
  （pbs.twimg.com/mediaも除外する場合）       22,020
  → 短辺200px下限でさらに約半分が落ちる

所要時間: host直列化が無いので global 8並列だけで決まる
  26,750 ÷ 8 × 0.3秒 ≈ 17分
  26,750 ÷ 8 × 1.0秒 ≈ 56分
```

最終案L212の「約50,400件」は26,750件へ更新する。

### レビュー21 — Claude Code (2026-08-25)

レビュー20のBI（`pbs.twimg.com/media`の扱い）について、拡張子で分けられるかを実測した。**分けられない。** ただし別の切り口で2,678件を判断なしに落とせることが分かった。

#### `pbs.twimg.com`の内訳 — `/media/`は全体の3割

まず前提の確認から。`pbs.twimg.com`のURLは名前gate前で15,523件あり、`/media/`はその一部にすぎない。

```text
 8,107  /profile_images/       アイコン。名前gateの`profile`で既に除外済み
 4,731  /media/                tweet本文に添付された画像
 1,571  /amplify_video_thumb/  動画のサムネイル
   526  /ext_tw_video_thumb/   動画のサムネイル
   457  /card_img/             リンクカードの画像
   126  /tweet_video_thumb/    動画のサムネイル
     4  /profile_banners/      名前gateの`profile`で除外済み
```

最大の`/profile_images/` 8,107件は、名前gateの`profile`が既に落としている。名前gateが実際に効いている例である。

`/media/`は「tweet本文の画像か」という問いに対しては**その通り**で、アイコンではない。

#### 高 — BJ. 拡張子では分けられない。Twitterが全てWebPへ再encodeしている

`/media/` 4,731件の形式とサイズ指定を集計した。

```text
形式                          サイズ指定
  webp  3,910  (82.6%)          medium  3,386  (71.6%)   幅1200px
  jpg     795  (16.8%)          small   1,199  (25.3%)   幅 680px
  png      26  ( 0.5%)          (指定なし) 145  ( 3.1%)
```

URLは`https://pbs.twimg.com/media/FrEGWHYagAAqr0t?format=webp&name=medium`の形で、形式はpath末尾ではなくquery文字列にある。

**82.6%がWebPである。** 「PNGなら文字中心のscreenshot、JPEGなら写真」という切り分けは成立しない。Twitterが元の形式に関わらずWebPへ再encodeして配信しているためで、URLに残る形式は投稿内容を反映していない。

サイズも効かない。`medium`は幅1200px、`small`は幅680pxで、どちらも短辺200px下限を余裕で超える。

**結論として、`/media/`を安価に選り分ける手段は無い。** 内容を見て判断できるのは第3段階のLLMだけである。

#### 判断なしに落とせるもの — 動画サムネイルとカード画像

一方、`/media/`以外には判断の要らないものがある。

- **動画のサムネイル** `/amplify_video_thumb/`、`/ext_tw_video_thumb/`、`/tweet_video_thumb/` 合計2,223件。動画の1フレームであり、説明画像ではない。
- **リンクカードの画像** `/card_img/`、`/cards/` 合計458件。リンク先のOGP画像で、記事の説明として置かれたものではない。

合計2,678件を接頭辞listへ追加できる。これらは`/media/`と違い判断の余地が無い。

なお、これらのpathはtweet ID等を含むためURLが1件ずつ異なり、レビュー18で検討した置き場所単位の頻度gateでは1つにまとまらない。頻度gateを削除する判断（指摘BH）を補強する材料でもある。

#### 現時点の規模

```text
名前gate後                              50,881
  − はてブ数画像（決定）        -24,135   26,746
  − twitter動画/カード           -2,678   24,068
  − twitter /media/（判断待ち）  -4,730   19,338
```

#### 中 — BK. `/media/`を残す費用は約$19〜66である

`/media/` 4,731件を残した場合、その全てが第3段階のLLMへ渡る（短辺下限を超えるため）。レビュー11で示した1枚あたり$0.004〜$0.014を当てると、**$19〜66**である。この幅は`gpt-5.6-terra`の画像token算出規則が公開されていないことによる。

これは「modelに選り分けてもらう費用」である。除外すればこの額を払わずに済むが、tweetされた図表のscreenshotが記事の説明として引用されている場合は取りこぼす。

決定AK（厳しめ、取りこぼしを許容する）に照らせば除外でよい。`ignored_reason`を専用の値にしておけば、後から件数を数えて戻す判断ができる。**採否は人間の判断とする。**

### レビュー22 — tsunyan (2026-08-25)

`pbs.twimg.com/media/`は**いったん除外する**。大半は説明画像ではないため、初版では取得も解析もしない。決定は口頭で示され、Claude Codeが記録した。

「いったん」であるから、後で戻せる形にしておくこと。

### レビュー23 — Claude Code (2026-08-25)

#### 取得前gateの確定形

レビュー17から22までで取得前gateの構成が変わった。最終案へ反映するため、ここに現在の形をまとめる。

**1. 名前gate（既存）** — URLまたはファイル名が次を含む候補を`ignored`とする。

```text
@2x  @3x  logo  icon  avatar  profile  button  btn
banner  badge  sprite  spacer  blank  emoji  favicon
```

**2. 接頭辞denylist（新規、頻度gateを置き換える）** — URLが次で始まる候補を`ignored`とする。

```text
https?://b.hatena.ne.jp/entry/image/        はてなブックマーク数画像
https?://b.hatena.ne.jp/bc/                 同上（旧形式）
https?://pbs.twimg.com/amplify_video_thumb/ 動画サムネイル
https?://pbs.twimg.com/ext_tw_video_thumb/  動画サムネイル
https?://pbs.twimg.com/tweet_video_thumb/   動画サムネイル
https?://pbs.twimg.com/card_img/            リンクカード画像
https?://pbs.twimg.com/cards/               同上
https?://pbs.twimg.com/media/               tweet添付画像（レビュー22で暫定除外）
```

**3. 頻度gateは削除する**（指摘BH）。`image_ocr.shared_url_resource_threshold`の設定、planning時の全体集計、閾値調整の運用も併せて消える。

参照Vaultでの効果は次のとおりである。

```text
current revisionの候補（distinct URL）          62,438
  名前gate後                                    50,881
  接頭辞denylist後                              19,338
  → この後に短辺200px下限とMIME・形式gateが効く

所要時間: 19,338 ÷ 8並列 × 0.3〜1.0秒 ≈ 12〜40分
```

最終案L212の「約50,400件」は19,338件へ更新する。

#### 中 — BL. denylistを取得前targetに含めないと、「いったん」が恒久になる

レビュー22は`pbs.twimg.com/media/`を暫定的に除外すると決めた。後で戻すには、denylistからその行を消したときに、既に`ignored`となった4,730件が通常実行の対象へ戻る必要がある。

最終案L167は取得前の試行targetを「`source_url`、正規化alt、backend ID、prompt/schema version、**取得・gateの上限設定**から作る」と定めている。denylistは「上限設定」ではないため、この文言のままでは含まれない。含まれなければ、denylistを変更しても`ignored`行のtargetは変わらず、L200により通常対象から外れたままになる。戻す手段は`--force`だけになり、`--force`は選択resourceの全候補を再取得・再解析するので、4,730件を戻すために無関係な画像まで払い直すことになる。

**取得前targetの構成要素に、名前gateのパターン集合と接頭辞denylistを明示的に含めること。** そうすればdenylistから1行消した時点でtargetが変わり、該当行だけが通常実行で再評価される。逆にdenylistへ追加した場合も、既に`completed`の行が対象へ戻って`ignored`へ落ち着く。

同じ理由で、`ignored_reason`はどの規則で落ちたかが分かる粒度にすること。`denylist:pbs.twimg.com/media`のように、規則の識別子を含める。件数を数えて戻す判断ができるようにするためである。

#### 最終案へ必要な修正の一覧

レビュー17以降の決定を反映するため、最終案の次の箇所に修正が要る。

| 箇所 | 修正内容 | 根拠 |
|---|---|---|
| L54 | `image_ocr.shared_url_resource_threshold`を削除 | 指摘BH |
| L54付近 | 接頭辞denylistを追加。初版は定数とし、設定化はしない | 指摘BH |
| L67 | 画像gate手順2を頻度gateから接頭辞denylistへ差し替え | 決定AY・BH |
| L119 | 「同一hostには同時に1 requestだけ送る」を削除 | 決定AX |
| L167 | 取得前targetへ名前gateパターンとdenylistを含める | 指摘BL |
| L209 | 「request数の多いhost」の表示は情報として残す（時間の制約要因ではない） | 決定AX |
| L212 | 「約50,400件」を19,338件へ。参照Vaultの数値とCLI表示を書き分ける | 指摘BC・BH |
| L287 | 検証項目2の「頻度gate」を「接頭辞denylist」へ | 指摘BH |

denylistを設定ではなく定数とすることを推す理由は、`AGENTS.md`の「新しい状態を要する規則より単純な規則」に従うためである。設定化すればconfig schema、検証、v3 migrationへの追加が発生する。実測ではdenylist 8行で候補の62%を処理できており、頻繁な更新は見込まれない。更新が必要になった時点で設定化を検討すればよい。

なお、レビュー16で挙げた残りの指摘のうちAZ（SSRF記述の本文への復帰）、BA（SHA-256変更時の状態定義）、BB（`--limit`の選択順序）、BD（`--force`とgateの関係）、BE（設定名）は未処理のままである。

### レビュー24 — tsunyan (2026-08-25)

レビュー16の未処理指摘について、次のとおり決定した。決定は口頭で示され、Claude Codeが記録した。

| 項目 | 決定 |
|---|---|
| AZ. SSRF記述の欠落 | **本文へ書く。** |
| BA. SHA-256変更時の状態 | **Claude Codeの判断に委ねる。** |
| BB. `--limit`の選択順序 | **古い順。** 実装が難しければ取得できた順でよい。 |
| BD. `--force`とgateの関係 | **gateを優先する。** |

BE（`image_ocr.max_images_per_resource`の設定名）については判断を示していない。

### レビュー25 — Claude Code (2026-08-25)

レビュー24の決定を最終案へ反映した。あわせてレビュー23の修正一覧も適用した。以下は変更内容の記録である。

#### AZ — ネットワーク要件を独立した節として本文へ追加

画像gate節の直前に「画像取得のネットワーク要件」を新設し、草案§2から落ちていた内容を復帰させた。

- 本文取得と同じ`NetworkPolicy`を使い、最初のURLと全redirect先を検証する。private address、`data:`、`blob:`、非HTTP(S) schemeを拒否する。DNS rebindingを含め本文取得と検証経路を共有し、画像専用の緩和を設けない。
- **画像取得はHTTPだけで行う。** Browser、JavaScript、Service Workerを使わない。本文取得にあるbrowser fallbackを画像へ持ち込まない。
- `--force`を含むどの実行modeでも緩めない。

検証項目7へSSRF検証とbrowser不使用の確認を追加した。検証項目に書いてあれば実装されるという前提を取らず、要件として本文に置いた。

#### BA — `analysis_status='pending'`とし、現在値列は保持する

新しい状態値を足さずに解決した。理由は次のとおりである。

`pending`の定義は「未処理、または入力が変わって再処理が必要」であり、画像が差し替わった状況にそのまま当てはまる。`ingest`の読み出し条件は`analysis_status='completed'`を含むため、`pending`へ戻した時点で要約入力から自動的に外れる。「渡さないが消さない」は、これと現在値列の保持だけで表現できる。

`stale`のような値を値域へ追加すると、`CHECK`制約、migration、状態遷移の場合分けが増える。得られるものは表示上の区別だけで、振る舞いは`pending`と同じである。増やさない方を採った。

あわせて、**`pending`の行が非空の`ocr_text`を持ち得ることを明記した。** 草案§5にあった「`pending`では`ocr_text`は空」という対応は採らない。保持する値は監査のためのもので、要約へは使われない。

`pending`は通常実行の対象条件に該当するが、失敗の抑止は`last_failure_kind`と`transient_retry_used`が別に管理する。再解析が終端失敗した行は`pending`のままでも通常実行で選ばれない、と明記した。

#### BB — revision作成時刻の古い順、`resource_id`を第2キーに

`ORDER BY`を指定せずSQLiteの行順に委ねてはならない、と明記した。同じVaultへ同じcommandを実行して選択が変わらないようにするためである。第2キーを置くのは同時刻の並びを固定するためで、決定にある「取れた順でいい」より強い保証だが、追加費用は無い。

古い順とする効果は、繰り返し`--limit`を実行したときに未処理分を先頭から順に消化でき、進捗が判りやすいことである。

なお`ingest`側の`--limit`が「変更のあったN件」ではなく「最も古いN件」を取ってしまう問題（レビュー11の指摘AJ）とは別件である。あちらは対象集合の選び方の問題で、`--stale` modeの追加で解決している。ここは対象集合が既に「処理が必要な画像を持つresource」に絞られたうえでの並び順なので、古い順で問題ない。

#### BD — gateを`--force`より優先

画像gate節へ次を明記した。

> `--force`は取得と解析をやり直す指示であって、対象外判定を覆す指示ではない。gateで`ignored`とした画像は`--force`でも取得・解析しない。

理由として、gateは費用の問題ではなく「説明画像かどうか」の判定であり、同じ入力に対して再実行で結論が変わる理由が無いことを書いた。gateの構成を変えた場合は取得前targetが変わることで対象へ戻る、という逃げ道も併記した。

#### レビュー23の修正一覧の適用

| 箇所 | 適用内容 |
|---|---|
| 設定表 | `image_ocr.shared_url_resource_threshold`を削除 |
| 画像gate手順2 | 頻度gateを接頭辞denylist 8行へ差し替え。code上の定数とし設定化しない |
| 画像gate | `ignored_reason`を`denylist:pbs.twimg.com/media`のように規則を識別できる粒度と規定 |
| 並列処理 | 「同一hostには同時に1 requestだけ送る」を削除し、削除の理由とHTTP 429の扱いを明記 |
| 取得前target | 名前gateのパターン集合と接頭辞denylistを構成要素へ追加（指摘BL） |
| command | 参照Vaultの数値をCLI出力へ埋め込まない旨を明記。19,338件は本文の根拠として記録 |
| 処理フロー図 | 「名前・頻度gate」を「名前gateと接頭辞denylist」へ |
| 検証項目2 | 頻度gateを接頭辞denylistへ。target変更による復帰とgate優先の確認を追加 |
| 検証項目7 | host直列化を行わないこと、SSRF検証、browser不使用を追加 |
| 検証項目11 | SHA-256変化時に`pending`かつ`ocr_text`保持となること、`--limit`の順序が安定することを追加 |

#### 未処理

- **BE** — `image_ocr.max_images_per_resource`は実際にはingestが読み出す枚数の上限であり、`enrich-images`の動作に影響しない。設定名の接頭辞が実態と合っていない。決定が示されていないため変更していない。名前を変えるならconfig v3の実装前に決める必要がある。
- **BK / レビュー21** — `pbs.twimg.com/media`はレビュー22で暫定除外と決定済みであり、denylistへ反映済みである。

### レビュー26 — Claude Code (2026-08-25)

レビュー16の指摘BE（設定名が実態と合っていない）について、人間から「適切な値に変更しておく」よう指示を受けた。最終案を次のとおり変更した。

#### 変更 — `max_images_per_resource` → `max_ocr_images_per_resource`

対になる設定が既に存在していた。

```text
image_ocr.max_ocr_chars_per_resource   10000   ingestへ渡すOCR原文合計の文字数上限
image_ocr.max_ocr_images_per_resource      8   ingestが要約へ取り込む説明画像数の上限
```

どちらも「1 resourceあたり要約へ何を渡すか」の上限で、枚数で切るか文字数で切るかだけが違う。`max_ocr_*_per_resource`という共通の形にすることで、両方が`ingest`側の設定であることが名前から読める。

旧名`max_images_per_resource`は「1 resourceで扱う画像の枚数」と読めてしまい、決定AM（全候補をgateし、resource単位の取得上限を設けない）と衝突する読み方を誘発していた。実際レビュー8の指摘Uからレビュー15の採否AUまで、この設定が何を数えるのかは繰り返し論点になっている。

`image_ocr`は初版で新設するtop-level objectであり、この名前を持つ既存configは存在しない。したがって改名にmigrationの考慮は不要である。config v3の実装前に確定させておけばよい。

#### 設定表へ「効く段階」列を追加

名前だけでは`enrich-images`と`ingest`のどちらに効くのかが読み取れないため、表へ段階を明示した。

```text
取得・gate        workers, timeout_seconds, max_bytes, max_pixels, min_short_edge_pixels
OCR生成           max_ocr_chars_per_image
要約への取り込み   max_ocr_images_per_resource, max_ocr_chars_per_resource
```

3段階に分かれることが表から直接読めるようになる。あわせて、`max_ocr_images_per_resource`が画像取得数・解析数・保存件数のいずれも制限しないこと、8枚は埋めるべき枠ではなくgateを通った候補が3枚なら3枚だけが要約へ入ることを、表の直後へ明記した。

#### 影響範囲

変更したのは最終案の設定表とその直後の段落だけである。草案とレビュー各節は追記専用のため、旧名`max_images_per_resource`が残っているが、これは各時点の記録として正しい。実装が参照するのは最終案である。

これでレビュー16で挙げた指摘AZ、BA、BB、BD、BEの5件がすべて処理済みとなった。

### レビュー27 — Codex (2026-08-25)

#### 結論

レビュー16〜26の指摘、採否、人間判断が最終案へ反映されていることを再確認した。未処理の仕様判断、実装を止める矛盾、データ保全上の欠陥は見つからなかったため、指摘なしで承認する。

実装可能性についても、現在の実CLIで次を確認した。

- Codex CLI 0.147.0は`codex exec --image <FILE>`で初回messageへ画像を添付できる。
- Claude Code 2.1.237は単一text入力用の画像flagを持たないが、`--input-format stream-json`でAgent SDK user messageのimage content blockを受け取る経路を持つ。実装はbase64画像をこのstreaming inputへ渡し、text-onlyのstdin modeへpath文字列を置かない。

最終案の修正履歴が`改訂`に無かった手続き上の不足は、改訂1・2として（前）・（後）・理由・根拠を追記して解消した。ステータスを`確定`とし、仕様単独commitの後に実装へ進める。
