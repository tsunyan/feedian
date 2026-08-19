# syncとingestのスループット

ステータス: 確定

## 最終案

### 結論

`feedian sync` が新着ゼロでも5分以上かかる原因を取り除き、あわせて本文取得とLLM実行を並列化する。変更は4つで、1つのブランチにまとめて実装する。

1. **はてブ収集の早期打ち切り** — 検索APIの返却順を全件走査で実測し、`20260818-sync-quick-mode.ja.md` が保留した判断を確定させる。
2. **syncの本文取得の並列化** — DB書き込みとplaywrightをmain threadに残し、HTTP取得だけをworkerへ出す。
3. **ingestのLLM実行の並列化** — `backend.summarize` だけをworkerへ出す。実行枠はbackendごとに持つ。
4. **確定仕様の改訂規則** — 「確定後は編集しない」が、誤りを直せない制約になっている。

並列度は対象ごとに別の設定キーを持つ。

**この仕様を貫く原則が1つある。** 並列化は取得とLLM実行の待ち時間だけを削り、**保存される状態を意味的に変えない**。workerは純粋な外部I/Oだけを担当し、判断と書き込みはすべてmain threadに残る。

原則を検証可能な形で述べる。

> 同じ入力（provider itemの集合）と同じ外部応答に対し、並列実装は直列実装と**意味的に同値な状態**を保存する。すなわち、本文、HTTP payload、`http_status`、response header、切り詰め情報、RSS fallbackの適用有無、`sync_run_item`とfetch captureの監査内容、および外部への取得回数と`--limit`の消費量が一致する。
>
> **比較対象から除く** — UUID、timestamp（`fetched_at`、`created_at` など）、所要時間、独立したresource間の完了順序、行の物理順序。これらは正しい実装でも実行ごとに変わる。

「1バイトも変えない」とは書かない。fetch captureは`uuid7()`と現在時刻を毎回書くため（`feedian/store.py:420-436`）、正しい実装でもバイト同一にはならず、受け入れ条件として成立しない。

レビューで見つかった重大な問題は5件とも「並列化の過程で、直列実行なら保存できていた本文・監査情報を落とすか、直列実行では起きなかった重複を作る」形をしていた。実装時にこの原則から外れる箇所を見つけたら、そこが誤りである。

### 計測（2026-08-19、参照Vault）

判断の根拠はすべて実測である。

| 対象 | 実測値 |
|---|---|
| はてな検索API `q=https` | 総件数5,018件 / 51リクエスト |
| はてな検索API `q=http` | 総件数1,969件 / 20リクエスト |
| はてな収集の合計 | 6,987件 / **71リクエスト** / 5分10秒 |
| 同APIの1リクエスト所要 | 310秒 ÷ 71 = **4.37秒**（実測範囲3.9〜4.4秒） |
| browser経由の本文抽出 | `fetch_capture` 9,877件のうち99件（**1.0%**） |
| 本文取得の所要 | 683件で8分20秒（約0.73秒/件） |
| `llm_run` の所要 | 成功4,686件の平均3.40秒、最大34.5秒 |
| 本文未取得のresource | 2,575件 |

実行ログ上の5分13秒・5分10秒・4分32秒は、71リクエスト × 約4.37秒でそのまま説明できる。`request_interval_seconds = 0.3` が占めるのは21秒（7%）にすぎず、律速はAPI自体のレイテンシと直列実行である。

**リクエスト数は71である。** `ceil(5018/100) + ceil(1969/100) = 71` であり、ページ数・境界数・collectorの停止条件のすべてと一致する。検討初期に用いた「72」は `6987/100 ≒ 70ページ + 端数` という概算であって実測値ではない。

#### はてな検索APIの返却順（全件走査）

各クエリの全行をoffset順に連結し、**隣接する全組**（ページ境界を含む）を検査した。

| クエリ | 行数 | ページ | 境界検査 | 違反 | 非増加 | 厳密降順 | 先頭ts | 末尾ts |
|---|---|---|---|---|---|---|---|---|
| `q=https` | 5,018 | 51 | 50箇所 | **0** | **true** | false | 1787077443 | 1251523484 |
| `q=http` | 1,969 | 20 | 19箇所 | **0** | **true** | true | 1775570348 | 1251523856 |

- 走査行数はAPIが申告した総件数と一致する。欠落は無い。
- 全行が `timestamp` を持つ。順序を判定できない行は無い。
- ページ境界の検査は合計69箇所、違反0件。**早期打ち切りが必要とする「結果列がページ境界をまたいで非増加である」性質を全件で確認した。**
- **`q=https` の厳密降順は `false`** である。同一 `timestamp` の行が実在する。したがって**契約は「厳密な降順」ではなく「非増加」とする。**

---

### 1. はてブ収集の早期打ち切り

#### 現状

`_provider_items` のhatena分岐（`feedian/sync.py:310-330`）は、渡された `quick`・`known`・`stop_after_known_pages` を**一つも参照していない**。早期打ち切りを実装しているのはraindrop分岐だけである。hatenaはquickでも毎回全件を走査し、下流で6,900件を `skipped` として捨てている。

`20260818-sync-quick-mode.ja.md` がこれを保留した理由は「検索APIの返却順が降順である保証を確認できていない」（同 :290）であり、同 :408 は「実測が取れれば導入できる」としていた。上記の全件走査で前提が解けた。

#### 変更

`fetch_hatena_bookmarks`（`feedian/hatena.py:222-290`）に3つの引数を足す。

```python
def fetch_hatena_bookmarks(
    ...,
    known: set[str] | None = None,
    stop_after_known_pages: int = 0,      # 0 は打ち切り無効（fullの既定）
    on_stopped_early: Callable[[], None] | None = None,
) -> list[CanonicalItem]
```

- **打ち切り判定はクエリごとに独立して持つ。** `SEARCH_QUERIES` の各クエリについて「そのページのitemがすべて `known` に含まれる」が `stop_after_known_pages` 回連続したら、そのクエリだけを打ち切る。片方が1ページで止まり、もう片方が走査を続けるのは正常である。新規ブックマークがどちらの全文検索トークンに載るかは事前に分からないため、**クエリ間で判定を共有してはならない。**
- **閾値は既存の `fetch.quick_stop_after_known_pages` を再利用する。** provider固有の新しいキーは作らない。
- **戻り値はlistのまま変えない。** クエリ間の重複排除と `created_at` によるソート（`feedian/hatena.py:289`）は収集完了後にしか行えず、収集自体が通常2リクエストで終わる以上、generator化の利得がない。
- `on_stopped_early` はどれか1つのクエリでも打ち切ったときに1回だけ呼び、`sync.py` 側で `stopped_early` へ `hatena` を積む。
- **`limit` の意味をquickではraindropに揃える。** 現行はクエリごとの `offset` を `limit` と比較しており（`feedian/hatena.py:239-241`）、早期打ち切りと組み合わせると停止条件が二重になる。quickでは「実際に取り込む新規itemの件数」を数える。fullは現行のまま変えない。

#### 既知のリスクと、機構を足さない理由

非増加順は**1回の全件観測であって、はてなが文書化した保証ではない**。順序が変われば、quickは新着を黙って取りこぼす。復旧経路は `--full` であり、raindropが既に負っているリスクと同じ性質である。

raindropに無くhatenaにある差が1つある。**同一URLの再ブックマークは `timestamp` を更新して先頭へ移動する。** raindropの `sort=-created` はタグ編集で動かないが、hatenaは動く。したがって理論上は、1回のsync間隔に100件以上の再ブックマークが挟まると、その下にいる新規itemを取りこぼす。`quick_stop_after_known_pages` を上げれば緩和できる。日常運用で1回のsync間隔に100件を再ブックマークすることは起こらないため、これを閉じるための機構は足さない。取りこぼしても `--full` で回収でき、保存済みのデータは失われない。

#### 効果

新着ゼロで**71リクエスト → 2リクエスト、5分10秒 → 約9秒**（2 × 4.37 = 8.7秒）。

---

### 2. syncの本文取得の並列化

#### 動かせない2つの制約

**(a) DBはmain thread専用である。** `VaultStore.open` は `sqlite3.connect(database_path)` を既定の `check_same_thread=True` で開く（`feedian/store.py:91`）。接続を作ったthread以外から使えない。`store.*` の呼び出しはすべてmain threadに残す。ローカルSQLiteへの書き込みが律速になることはないため、これは制約であって性能上の問題ではない。

**(b) playwrightはmain threadに固定する。** `render_html_with_browser` は module-global の `_browser_runtime` / `_browser` を1つだけ持ち（`feedian/extract.py:717-763`）、sync APIは生成したthreadに束縛される。worker threadから触ると壊れる。

**browser経由の抽出は全取得9,877件のうち99件、1.0%** である。1%のためにworkerごとにchromiumを立てる必要も、browser専用のpoolを作る必要もない。

#### browser合成 — 単純な `allow_browser` フラグでは本文と監査情報を失う

現行の200 HTML経路は、HTTP抽出を `best_text` として保持し、browser抽出の `text_quality_score` が**それを上回る場合だけ**差し替える（`feedian/extract.py:351-374`）。browserが失敗してもHTTP本文を残し、警告だけを連結する（`:371-372`, `:389-395`）。

一方 `fetch_page_text_with_browser`（`:413-450`）は401/403/406向けの独立した関数であり、先行するHTTP結果を引数に取らず、browser結果だけから `PageFetchResult` を作る。**main threadがこの戻り値をそのまま採用すると、次を失う。**

- **本文** — browser本文がHTTPより短い場合、またはbrowserが例外になった場合に、現行なら残る本文が短い本文または空本文に置き換わる。
- **`raw_body`** — `_store_page` が `_put_page_payload` でpayloadとして保存する（`feedian/sync.py:386`）。HTTP応答の原本が保存されなくなる。
- **`http_status`** — `record_resource_revision` と `record_failed_fetch` の双方へ渡る（`feedian/sync.py:405-419`）。これは `20260819-unreachable-host-cost.ja.md` が定めた終端ステータス規則（404/410）の入力である。**失うと再試行抑制が効かなくなる。**
- **`response_headers` / `content_truncated` / `content_encoding`** — 条件付き取得のETag / Last-Modified保存と切り詰め記録の入力である。

したがって `PageFetchResult` へboolを足すのではなく、**合成を担うhelperを1つ定義し、直列経路と並列経路の双方がそれを呼ぶ**形にする。2つ実装を置くと必ず片方だけが直る。

- **経路1（401/403/406）** — 比較対象のHTTP本文が存在しない。現行どおり `fetch_page_text_with_browser` を単独で使う。
- **経路2（200 HTMLの低品質判定）** — workerはHTTPの `PageFetchResult` とbrowser実行に必要なcontext（`fetch_url`、`allow_private_urls`、timeout）をmain threadへ返す。main threadがbrowserを実行し、helperが現行 `feedian/extract.py:365-409` と同じ `text_quality_score` 比較・warning組み立て・`raw_body`／header／statusの保持を行って最終結果を作る。
- helperは `fetch_page_text` の直列経路からも呼ぶ。**これにより2経路の合成規則が構造的に一致する。**

#### 構造

既存の `_store_hatena_comments_parallel`（`feedian/sync.py:610-673`）が既にこの形をとっている。**workerは純粋な取得だけを行い、main threadが結果を回収して書き込む。** 同じ形を2箇所へ広げる。

- **(A) providerループ** — main threadが `upsert_canonical_item` と `should_fetch_resource` まで進め、取得が必要なitemをchunk単位でpoolへ投げ、`as_completed` で回収する。chunkサイズは `workers * 4`。`--limit` の予算（`provider_fetch_attempts`）はchunkを組む時点でmain threadが決めるため超過しない。
- **(B1) `_run_quick_body_only_pass`**（`feedian/sync.py:426-`）— 対象がresourceのリストなので同じ形をそのまま適用する。
- 進捗の意味は変えない。`progress(processed + skipped, item)` はchunk回収時にmain threadから呼ぶ。

#### 取得の単位はitemではなくresourceである

**chunk化は、直列実行が暗黙に持っていた同一resourceの重複抑止を壊す。** 直列実行では、あるitemの取得結果を保存してから次のitemを判定するため、次のitemが同じresourceを指せば `should_fetch_resource` が保存済みcaptureを見て取得不要と答える（`feedian/sync.py:131-181`）。chunkを先に組むと、最初の結果が保存される前に後続itemも判定されるため、**同じresourceが複数回scheduleされる。**

これは仮定ではない。resourceは正規化URLで共有される（`feedian/store.py:1372-1377`）一方、`source_id` はproviderごとに別の由来を持つ。

| provider | `source_id` の由来 | 同一URLで複数itemが出るか |
|---|---|---|
| `rss` | `sha256(identity_url \0 native)`（`feedian/rss.py:150`） | **出る。** 異なるfeedが同じ記事URLを配信した場合 |
| `raindrop` | raindropの `_id`（`feedian/canonical.py:76`） | **出る。** 同じURLを2件保存した場合 |
| `hatena` | `source_id_for_url("hatena", url)` | 出ない。URL由来のため `items_by_id` が排除する |

`_provider_items` のRSS重複排除は `seen_source_ids` だけを見る（`feedian/sync.py:352-355`）ため、これらは同じchunkへ入り得る。放置すると**同じ外部ページを複数回取得し、`provider_fetch_attempts` と `--limit` を重複消費し、同一resourceへ複数のfetch captureを書く。** 上記の原則に正面から反する。

**同一resourceのitemを同じchunkへ入れない。合流はさせない。**

- chunkを組むとき、`resource_id` が既にそのchunkに現れていれば、そのitemは**そのchunkへ入れず、後続のchunkへ繰り延べる。**
- 繰り延べたitemは、先行itemの結果が保存された後に、**通常のmain thread経路で `should_fetch_resource` を再評価する。** 判定に手を加えない。
- itemは有限であり、繰り延べは必ず終わる。同一resourceのitemが3件あれば3つのchunkに分かれる。

**合流させてはならない理由は、失敗時と `force_fetch` 時に直列実装と挙動が変わるからである。**

| 経路 | 直列実装の挙動 | 合流させた場合 |
|---|---|---|
| 先行itemの取得が失敗（`force_fetch=False`） | 後続itemは `should_fetch_resource` のbackoffで取得されず、**自身のauditは `completed`** になる（`feedian/sync.py:195-199`） | 後続itemまで `failed` になり監査内容が変わる |
| `force_fetch=True` | `should_fetch_resource` は先頭で常に `True` を返す（`feedian/store.py:913-914`）ため、**itemごとに取得し、各回が予算を消費する** | 1回へ畳まれ、取得回数・`--limit` 消費・最後に保存される応答が変わる |

繰り延べ方式なら、いずれも再評価が現行の判定をそのまま通すため、**`force_fetch=False` の成功時だけが自然に1回へ畳まれる。** これは意味的同値の要求（`結論` の原則）を満たす唯一の形である。

RSS fallbackの `embedded_content` は、繰り延べにより入力順のitemが順に処理されるため、並列度によらず同じitemのものが使われる。特別な規則を置かない。

#### 取得後処理はmain threadに残す — 順序を固定する

workerの戻り値は `PageFetchResult`、browser実行context、または例外情報に限定する。**取得後処理は現行 `feedian/sync.py:145-200` の順序のままmain threadで行う。** この順序を変えるとRSSの本文fallbackを失う。

1. **例外** — `item.embedded_content` があり未保存resourceなら `rss-feed-fallback` revisionを書く。無ければ `record_failed_fetch`。
2. **`page.not_modified`** — `record_not_modified_fetch`。
3. **browser保留** — 上記の合成helperを経路1/2に応じて適用する。
4. **空本文かつ `embedded_content` あり** — `page.text`・`page.title`・`page.extraction_method` を差し替える。
5. **`_store_page`**。
6. **`comment_targets` への積み上げ、`record_sync_item`、`changed` 加算**。

#### 設定

`fetch.workers`（既定8）。既存の `fetch.comment_workers`（既定8）は独立した設定として残す。**同一ホストへの同時取得は制限しない**（後述の却下理由を参照）。

#### 効果

683件の本文取得が8分20秒 → 約1分（8並列）。browser描画をmain threadで直列に行うため、実効は8倍には届かない。

---

### 3. ingestのLLM実行の並列化

#### 分割

`_attempt_candidate`（`feedian/ingest.py:348-412`）は `start_llm_run` → `backend.summarize` → `finish_llm_run` を一続きで行う。DBがmain thread専用である以上、これを分割する。

- **main thread** — `start_llm_run`
- **worker** — `backend.summarize` だけ
- **main thread** — `finish_llm_run`、`put_source_note`、集計、進捗

**fallbackの再計画もmain threadに残す。** `feedian/ingest.py:250-276` のfallbackは `_candidate(store, ...)` を呼んでDBを読む。workerは1次backendの `summarize` だけを担当し、fallbackが必要な結果はmain threadが判定してから改めてsubmitする。`preflight` はmain threadでbackendごとに1回だけ実行する。

#### 実行枠と開始間隔はbackend IDごとに持つ

- **既存の `BackendCapabilities.max_parallelism`（`feedian/llm_backends.py:86`）を使う。** 新しいcapability名は追加しない。この枠は宣言済みで、現在どこからも読まれていない。
- **poolは1つとし、実行枠はbackend IDごとのsemaphoreで制御する。** backendごとに別poolを作らない。worker総数は `llm.workers`、そのなかでbackendごとの `max_parallelism` が実効上限になる。
- **`min_start_interval_seconds` はworkerで待たない。schedulerの投入条件にする。** 現行は `last_start` というlocal変数であり（`feedian/ingest.py:206`）、並列化すると全workerが同じ値を読んで同時に発射する。しかし待機をworkerへ置くと、**Futureが RUNNING でありながら外部リクエストをまだ送っていない状態**ができ、後述の中断契約が壊れる。

main threadのschedulerは、backendの「次に開始してよい時刻」を過ぎた候補だけをsubmitする。worker側にlockもsleepも置かない。これにより、**RUNNINGになったworkerが待機せずに外部リクエストへ向かう。**

**間隔が保証するのはscheduler投入時刻（submit時刻）の間隔である。** wireへ送られる時刻ではない。両者の差はthread dispatchの遅延だけで、そこにsleepもlockも挟まらない。6.1秒に対して無視できる。保証対象を投入時刻と明記するのは、wire時刻を保証すると書けば嘘になるからである。

**schedulerは次に意味のあるeventまで待つ。** 開始可能時刻が未到達で、かつ回収対象のFutureが1つも無い状態は通常に起きる（直前のtaskがすべて完了した直後など）。ここで投入も待機もせずに回り続けるとbusy loopになる。待機条件を「いずれかのFutureの完了、または最も早いbackend開始可能時刻」と定義する。

- 実行中Futureがある — `concurrent.futures.wait(pending, timeout=next_due-now, return_when=FIRST_COMPLETED)`。
- 実行中Futureが無い — main threadでその timeout だけ待つ。

**この待機はFutureも `llm_run` も作る前に行う。** したがって「RUNNINGだが未送信」の状態を再導入しない。main threadの待機はCtrl-Cで中断できる。
- **1次backendとfallback backendのどちらも、実際に呼ぶbackend自身の枠と間隔を通る。** HTTP 8並列のschedulerへ `codex-local` fallbackを投げても直列に落ち、`codex-local` が1次でもHTTP fallbackは直列化されない。

#### backendごとの `max_parallelism`

| backend | 値 | 理由 |
|---|---|---|
| `openai-responses` | **8** | HTTP。並列化の主対象である |
| `manus-api` | **8** | 並列度が効くのは作成レートではなく**同時にpollできるtask数**である。作成レートは下記の開始間隔が律速する |
| `codex-local` | **1** | 記事ごとのcwdは `mkdtemp` で一意だが（`feedian/local_agent.py:201`）、`CODEX_HOME` は全実行で共有しており同時実行の安全性を確認していない。確認できるまで動かさない |
| `claude-code-local` / 新規backend | **1**（既定） | 並列安全性を明示的に宣言するまで直列で動かす（fail closed） |

#### Manusの開始間隔を capability へ移す

`_summarize_with_manus` は**最初の1行で** `_wait_for_manus_create_slot()` を呼び（`feedian/llm.py:337-344`）、このgateはlockを保持したまま最大 `MANUS_CREATE_INTERVAL_SECONDS = 6.1` 秒sleepする（同 `:19`, `:487-495`）。したがって現状のManusは、workerがcallableへ入った後も外部リクエストを送っていない時間を最大6.1秒持つ。`llm.workers = 8` なら先頭以外の7本がこの状態になり得る。

- **`manus-api` の `min_start_interval_seconds` を `6.1` として宣言する。** 現在は `0.0` である（`feedian/llm_backends.py:594`）。
- **ingest経路では `_summarize_with_manus` のgateを通らない。** schedulerが既に間隔を守っているため、通すと二重待ちになる。gateを迂回できる入口を設ける。
- **legacy export経路（`feedian/__main__.py`）は現行どおりgateを使う。** この経路にschedulerは無い。
- 結果として、**間隔はどの経路でもちょうど1回だけ強制される。**

`ApiBackend.__init__`（`feedian/llm_backends.py:127-135`）へ引数を足し、`get_backend` が明示的に8を渡す。**現状 `ApiBackend` は `max_parallelism` を受け取らず既定1のままなので、schedulerだけを実装してもHTTPは直列のままである。**

#### scheduler — executorのbacklogを作らない

- main threadのschedulerが、**global worker枠とbackend別枠の空きを確認した候補についてだけ** `start_llm_run` とsubmitを行う。意図的な先行submitは行わない。
- main threadは `run_id`・future・backend枠の対応を保持する。

#### 中断契約 — `Future.cancel()` の返り値で境界を決める

`ThreadPoolExecutor.submit` はwork queueへ積んでFutureを返し（`thread.py:179-181`）、workerは後から `set_running_or_notify_cancel()` に成功して初めてcallableを呼ぶ（`thread.py:54-59`）。**したがって空き枠を正しく数えても「submit済みかつ未開始」の窓は消えない。** この窓でCtrl-Cが入ると、そのFutureを実行中とみなして待つ実装は、中断後にbackend callを開始してしまう。

`Future.cancel()` は `self._condition` の下でRUNNING / FINISHEDなら `False`、それ以外ならCANCELLEDへ遷移する（`_base.py:364-378`）。**`cancel()` と `set_running_or_notify_cancel()` は排他であり、どちらが勝ったかが「backendを呼んだか否か」と1対1で対応する。** この公開APIだけで境界を決める。executorのprivate stateを推測しない。

Ctrl-C（`KeyboardInterrupt`）の手順を次に固定する。

1. 新規のsubmitを止める。
2. **追跡中の全Futureへ `cancel()` を試し、結果を記録する。**
3. cancelに成功したFutureはbackendを呼ばない。対応する `llm_run` をmain threadから失敗で終端し、backend枠を解放する。
4. cancelに失敗したFutureだけを実行中として待ち、成功・失敗いずれの監査結果も保存する。停止不能なHTTP処理はcancelしない。後から返る成功レスポンスを捨てないためである。
5. `shutdown(wait=True)` する。
6. `KeyboardInterrupt` を再送出する。

**一般則** — main threadは、自分が開いた `llm_run` のうち監査結果を保存できなかったものをすべて失敗で終端してから再送出する。これはcancelに成功したFutureと、**`start_llm_run` とsubmitの間で中断されFutureを持たないrun**（`submit` 自体が例外になった場合を含む）の双方を覆う。Futureの有無で場合分けしない。

**`shutdown(cancel_futures=True)` に頼ってはならない。** 実装（`thread.py:220-239`）はwork queueをdrainして各 `work_item.future.cancel()` を呼ぶだけで、**どのFutureをcancelしたかを返さない。** どの `llm_run` を閉じるべきかが分からなくなる。

#### 強制終了からの回収

`fail_interrupted_llm_runs()` を追加する。`fail_interrupted_sync_runs`（`feedian/store.py:732-744`）と同型に `status='running'` を `failed` へ落とす。`llm_run.status` はCHECK制約の無いTEXT列なのでschema変更は不要である。

**`vault_write_lock` の内側で呼ぶ。** ingestは既に `feedian/cli.py:695` でこのlockを取っているため、`ingest_source_notes` の冒頭で呼べば条件を満たす。別processの有効なrunを失敗へ変えないために、この位置を仕様として固定する。

#### 課金の扱い

**並列度をそのまま同時課金数として扱う。** 最大N件が同時に飛ぶ。`--limit` の予算はsubmit時点でmain threadが決めるため超過しない。

**課金へのcommit境界はFutureのRUNNING遷移とする。** wireへの送信時刻ではない。

- `cancel()` に**成功した**Futureは、backendを呼ばない。対応する `llm_run` を失敗終端し、課金は発生しない。
- `cancel()` に**失敗した**Futureは開始済みとして扱う。**まだwireへ送っていなくても、Ctrl-C後に送信される可能性がある。**

`ThreadPoolExecutor` は `set_running_or_notify_cancel()` でRUNNINGへ遷移させてからcallableを呼び（`thread.py:54-59`）、callableは `backend.summarize` へ入ってpayloadとHTTP requestを組み立ててから外部I/Oを始める。**この窓は必ず存在する。**「RUNNINGと送信直前が一致する」とは書けない。

**窓は短いが、同時に複数存在し得る。** `openai-responses` は `max_parallelism = 8`、`min_start_interval_seconds = 0` なので、schedulerは枠が空いていれば8件をsubmitし、各workerは独立にRUNNINGへ遷移する。Ctrl-C時点で8件すべてがRUNNINGかつwire未送信ということが起こり得る。

> **Ctrl-C後に送られる可能性がある件数は、その時点でRUNNINGかつ未送信のFuture数以下である。** これはglobal worker数とbackendの `max_parallelism` で上限を受ける。**利用者が選んだ並列度を超える課金は発生しない。**

`manus-api` は6.1秒の投入間隔により通常1件以下になるが、これはそのbackend固有の性質であり、全backendの上限として一般化できない。

窓を厳密に閉じるには、外部I/O直前でstop状態を確認する協調cancelを全backendのinterfaceへ通す必要がある（レビュー13で不採用とした案）。**この窓に残るのはpayload組み立てだけで、sleepもlockも待機も無い。** Ctrl-Cがその区間に入る確率は小さく、入った場合の損失も利用者が選んだ並列度分の要約1回分に収まる。全backendのinterfaceを変える価値は無い。

helpには「Ctrl-Cで停止したとき、開始済みのリクエストは課金される場合があります。件数は指定した並列度を超えません」と書く。「未送信は課金されない」とは書かない。

#### 設定

`llm.workers`（既定8）。

#### 効果

`llm_run` の成功4,686件の平均所要は3.40秒。新規100件で340秒 → 8並列で約45秒。

---

### 4. 確定仕様の改訂規則

#### 問題

`AGENTS.md:70` は `docs/specs/` を "Do not edit after finalization" と定めている。この制約は、**確定仕様が誤っていると分かっても直せない**という運用を生む。現に `20260818-sync-quick-mode.ja.md` のはてブ判断は、前提（返却順が不明）が実測で崩れたのにそのまま残り、`DESIGN.md` にも同じ記述が写っている。さらに悪いのは、これが「確定仕様がそう書いてあるから」という、**対応を次回へ持ち越す言い訳になる**ことである。

#### 役割分担は変えない

`AGENTS.md:64-74` の役割分担を**維持する**。

- **`DESIGN.md`** — 現在どう動くかの**唯一の正本**。実装のたびに更新する。
- **`docs/specs/`** — なぜその決定をしたかのADR。**決定そのものが後継仕様に置き換えられたときに限り、一度だけ訂正する。**

この区別により3箇所同期は発生しない。訂正は置き換えの瞬間に1回起きるだけで、以後その仕様は再び静止する。継続的な同期義務を負うのは `DESIGN.md` だけである。

#### 規則

> **確定仕様は、誤りと分かった決定を主張したまま残さない。** 後継の仕様が決定を置き換えたとき、`最終案` の該当箇所を訂正し、`## 改訂` へ `(前)` → `(後)` と理由・根拠を追記する。訂正は置き換えの時点で一度だけ行う。現行挙動の説明は `DESIGN.md` が持ち、仕様がそれを二重に持つことはない。

- **確定仕様は常に1つの形だけを持つ。** 現行の決定と旧版が同じ文書内に並存してはならない。
- **変更内容は追記で記録する。** `## 改訂` へ `### 改訂<N> — <名前> (YYYY-MM-DD)` の見出しで1件ずつ積む。
- **セクション順序を固定する — `最終案` → `改訂` → `草案` → `レビュー`。** `## 改訂` は `最終案` の直後に置く。文書末尾に置いてはならない。**`## レビュー` を必ず最後にし、追記先を常に空けておくためである。** 改訂は確定の前後を問わず、`最終案` を変更したときに記録する。
- **改訂の記録は `(前)` → `(後)` を本文中に書く。** 変更前後の記述を引用し、理由と根拠を添える。git logを引かなくても文書だけで変更が追えるようにするためである。
- **コミット前後を問わず同じ手順を踏む。** コミット前はgitに旧版が残らないため、`(前)` の記述を書き残すことがより重要になる。
- **`草案` と `レビュー` は従来どおり追記のみとする。** 却下案が残ることがADRとしての価値である。改訂できるのは `最終案` だけである。
- **確定仕様の誤りを、対応を先送りする理由にしない。** 誤りを見つけた時点で、改訂と実装を同じ流れで扱う。
- ファイル名は従来どおり改名しない。

#### `## 改訂` の記法

```markdown
## 改訂

### 改訂1 — Claude Code (2026-08-19)

**箇所**: 「Raindropのみ収集を早期に打ち切る」の段落

**(前)** Hatenaは検索APIの返却順に保証が無いため全件収集を維持し、RSSは……

**(後)** HatenaもRaindropと同様に、既知ページが連続したところで収集を打ち切る。RSSは……

**理由**: 検索APIの返却順を全件走査で実測し、6,987行・69ページ境界で非増加性を確認した。
「返却順の保証が無い」という保留の根拠が消えたため。

**根拠**: [syncとingestのスループット](20260819-sync-ingest-throughput.ja.md)
```

#### AGENTS.md への反映

- `AGENTS.md:70` の表「Do not edit after finalization」を上の規則へ差し替える。
- `AGENTS.md:91-93` の「確定前は追記のみ」「草案は誤りが見つかっても直さない」は**そのまま残す。** 改訂できるのは確定した `最終案` だけである。
- 「Structure」へ `## 改訂` セクションと `(前)` / `(後)` の記法を追記する。

#### 本仕様の実装で行う適用

- `20260818-sync-quick-mode.ja.md` の「Hatenaは全件収集を維持する」判断を上の記法で改訂し、本仕様へリンクする（同 :70, :124, :231, :290, :297, :408）。
- `DESIGN.md`「Syncのモード」節の「Hatenaは検索APIの返却順に保証が無いため全件収集を維持し」を書き換える。
- `DESIGN.md` に「並列処理」節を新設し、DB単一thread・playwright固定・3つのworker設定・中断契約を要約する。
- `README.md` の設定表に `fetch.workers` と `llm.workers` を足す。

---

### 設定キー一覧

| キー | 既定 | 対象 |
|---|---|---|
| `fetch.workers` | 8 | syncの本文取得（(A)ループと(B1)passの双方） |
| `fetch.comment_workers` | 8 | Hatenaコメント取得（既存） |
| `llm.workers` | 8 | ingestのLLM実行。実効値はbackendの `max_parallelism` で上限を受ける |
| `fetch.quick_stop_after_known_pages` | 1 | raindropとhatenaの早期打ち切り閾値（既存。hatenaへ適用範囲を広げる） |

#### `llm.workers` はVault設定の契約を要する

`fetch` は任意キーを保持するdictで `render_vault_config` がそのまま出力するため、`fetch.workers` は素通りする。**`llm` は違う。** `LLMSettings` はdataclassであり、`_parse_llm`（`feedian/vault.py:325-327`）が `backend` / `model` / `fallback` 以外をunknown fieldとして拒否する。`render_vault_config`（`:265-273`）も3項目しか出力しない。**設定表に書くだけでは、利用者がそのキーを置いた時点でVaultが開けない。**

- `LLMSettings` へ `workers: int = 8` を足す。
- `_parse_llm` の許可キー集合へ `workers` を追加し、型と範囲を検証する。
- `render_vault_config` へ出力する。**常に明示する**（既定値でも省略しない）。
- **format versionは2のまま据え置く。** キー欠落は既定8として受理する。既定値だけで成立する追加のためにversionを上げると、既存Vaultへ `feedian migrate` を強いることになり見合わない。

#### 検証のタイミング

**config load時（`load_vault_config`）へ統一する。** `fetch` には現在parse関数が無く生のdictを読むだけなので、`fetch.workers` の検証もここへ足す。bool、非整数、1未満はエラーとする。`quick_stop_after_known_pages` が既に持っている検証（`feedian/sync.py:61-67`）と同じ形にする。

**`comment_workers` も同じ厳格検証へ揃える。** 現行は `int(config.fetch.get("comment_workers", 8))` であり `"8"` も `True` も通るため、**これは意図的な既存挙動の変更である。** 参照Vaultの値は `8`（int）なので実害は無い。同種のキーに2つの検証規則を残すほうが、後のレビューで必ず指摘される。

---

### 却下した案

| 案 | 判断 | 理由 |
|---|---|---|
| worker threadごとにplaywright browserを持つ | 不採用 | browser経由は全取得の1.0%（99/9,877）。chromiumを8個立ち上げる費用に見合わない |
| browser専用のworker poolとqueueを作る | 不採用 | 同上。main threadでの直列実行で足りる |
| `PageFetchResult` へboolを足してbrowser結果で置き換える | 不採用 | 本文だけでなく `raw_body`・`http_status`・header・切り詰め情報を失う。`http_status` は終端ステータス規則の入力であり、失うと再試行抑制が効かなくなる |
| `check_same_thread=False` にしてDB書き込みも並列化する | 不採用 | ローカルSQLiteの書き込みは律速ではない。WALでも書き込みは直列化されるため得るものが無く、トランザクション境界だけが壊れる |
| 同一ホストへの同時取得を1本に制限する | 不採用 | 通常のWebブラウザはHTML本文以外のリソースを同時に要求するため、8本は極端な数ではない。弾かれる事象を観測してから対処する |
| `BackendCapabilities` へ `max_concurrency` を新設する | 不採用 | 同義の `max_parallelism` が既に存在する（`feedian/llm_backends.py:86`）。同じ意味の名前を2つ持たせない |
| ingestで最大2Nを先行submitする | 不採用 | queue内のrunが `running` になり、Ctrl-C後に未開始futureが新しい課金処理を始める |
| `shutdown(cancel_futures=True)` で中断を処理する | 不採用 | どのFutureをcancelしたかを返さないため、閉じるべき `llm_run` が特定できない |
| hatenaに専用の `hatena_stop_after_known_pages` を作る | 不採用 | 同じ概念に2つのキーを持たせない |
| `q=http` クエリを落として収集を半減する | 不採用 | 実測totalは https 5,018 / http 1,969 で合計6,987。ほぼ加算的であり両クエリの集合は素に近い。落とすと取りこぼす |
| はてなの `request_interval_seconds` を下げる | 不採用 | 0.3秒 × 71回 = 21秒で、5分10秒の7%にすぎない。律速ではない |
| はてなの収集自体を並列化する | 不採用 | 早期打ち切りで71リクエストが2リクエストになる。並列化する対象が残らない |
| 仕様のステータスへ `対応中`（実装進行中）を足す | 不採用 | 本仕様の他項目と違い、実測された問題を解いていない。実装の進捗はbranchとPRから読める。状態を増やして解ける問題を具体的に示せなかった |
| 確定仕様に `改訂` ステータスを足す | 不採用 | 「確定か改訂か」という判定が増える。改訂履歴はセクションで足りる |
| 誤った確定仕様は改訂せず、新しい仕様で上書きする | 不採用 | 読者が旧仕様に当たったとき、それが現行でないと分からない |
| 規約変更を別仕様として先に確定させる | 不採用 | syncのquickモードという1つの変更は、手続きを分けた結果として今回で4回目の分断になっている。規約は変更を通すための道具であって、変更を分割する理由ではない |

---

### 検証

#### はてブ早期打ち切り

1. quick + 新着ゼロで、hatenaのリクエストが各クエリ1ページに収まり、`stopped_early` に `hatena` が入る。
2. quick + 新着1件で、その1件だけが処理される。
3. fullでは早期打ち切りが効かず、従来どおり全件を走査する。
4. `quick_stop_after_known_pages = 2` で、各クエリが2ページ走査してから止まる。
5. 片方のクエリだけが打ち切られ、もう片方が走査を続ける状況を再現する。

#### syncの並列化

6. `fetch.workers = 1` のとき、並列化前と同一の結果になる。
7. **RSSの4経路**（通常本文・空本文・取得例外・304）を並列度1と8の双方で固定し、`rss-feed-fallback` の本文が保存されることを確認する。
8. **browser合成の3経路** — (a) browserがHTTPより改善する、(b) browserがHTTPより悪い、(c) browserが例外になる。並列度1と8の双方で、本文・warning・payload参照・`http_status` が現行と一致することを確認する。
9. browser fallbackを要するURLを含むバッチで、playwrightがmain threadだけで動く。
10. `--limit` がsyncの並列化後も超過しない（(A)と(B1)の共有予算を含む）。
10a. **異なるfeed由来の2 itemが同じ記事URLを指す**とき、`force_fetch=False` の成功経路で外部取得・予算消費・fetch captureが各1回であり、両source itemに `sync_run_item` が残る。Raindropで同じURLを2件保存したケースも同様に固定する。
10b. 同じ2 itemで**先行itemの取得が例外になる**とき、先行itemのauditが `failed`、後続itemのauditが `completed` になる（直列実装と一致する）。
10c. 同じ2 itemを **`--force-fetch` で処理する**とき、外部取得が2回行われ、予算を2回消費し、2回の応答を区別できる。
10d. 10a〜10cのすべてが `fetch.workers = 1` と `= 8` で一致する。

#### ingestの並列化

11. `llm.workers = 8` で各HTTP backendの最大同時 `summarize` 数が8、`llm.workers = 3` なら3、`codex-local` は `llm.workers` によらず常に1。
12. `min_start_interval_seconds > 0` のbackendで、並列時もstart間隔が守られる。**間隔待ちはschedulerで行われ、workerはsleepしない。**
12a. `min_start_interval_seconds > 0` のfake backendで、**worker内に意図的な待機が無い**ことを確認する。workerはlockもsleepも持たず、間隔はscheduler投入時刻の間隔として満たされる。
12b. ingest経路のManusが `_wait_for_manus_create_slot()` を呼ばず、legacy export経路が呼ぶ。間隔がどちらの経路でも1回だけ強制される。
13. fallbackが1次と異なるbackendのとき、fallbackは**そのbackend自身**の枠と間隔を通る。
14. **制御可能なfake executor**でsubmit後・worker開始前に `KeyboardInterrupt` を発生させ、(a) `cancel()` が成功し、(b) 対応する `llm_run` がfailedで閉じ、(c) backendが一度も呼ばれないことを確認する。
15. 実workerで既に開始したFutureが、`cancel()` 失敗後にdrainされ監査結果を保存されることを固定する。
16. `start_llm_run` とsubmitの間で中断した場合に、Futureを持たないrunがfailedで閉じることを固定する。
17. 中断後に新しいbackend callが1件も開始されないことを、14〜16の3経路すべてで確認する。
17a. `min_start_interval_seconds > 0` のbackendを8並列で動かし、schedulerの間隔待ちの最中にCtrl-Cを入れて、**まだ投入されていない候補について `llm_run` が作られず、外部リクエストも開始されない**ことを確認する。
17b. **fake clockとfake wait関数**で、開始時刻未到達かつ実行中Futureなしの状態を作り、schedulerがspinせず**1回のtimeout待ちへ入る**ことを確認する。
17c. N個のworkerを**RUNNING遷移後・fake外部送信前のbarrier**で止めてCtrl-Cを発生させ、(a) N個すべての `cancel()` が失敗し、(b) barrier解放後にN件が送信され得る一方、(c) 未開始Futureはcancelされ対応するrunがfailedで閉じることを固定する。**送信され得る件数がglobal worker数と `max_parallelism` の小さい方を超えない**ことを含む。
18. 強制終了で残った `running` 行が、次回ingest開始時に `vault_write_lock` の内側で `failed` へ回収される。

#### 設定

19. 4つの設定キーの不正値（0、負、bool、非整数）がconfig load時にエラーになる。
20. `llm.workers` を書いたVault configが開け、`render_vault_config` が常にその値を出力する。format version 2のまま移行を要さない。
21. `llm.workers` を持たない既存Vault configが、既定8として開ける。

#### 既存契約の非回帰

22. quickの既存の契約が変わっていない。provider metadataの差分、Hatenaコメントの増減、`refresh_days` 再取得は引き続きquickでは反映されない。
23. 終端ステータス規則（404/410）と終端種別規則（dns/timeout）が並列化後も効く。`http_status` と `failure_kind` が保存され続けることを含む。
24. **意味的同値の確認** — 同じ入力と同じ外部応答を与えたとき、`fetch.workers = 1` と `= 8` で、本文・HTTP payload・`http_status`・header・切り詰め情報・fallback適用・監査内容・外部取得回数が一致する。UUID、timestamp、所要時間、独立resource間の完了順は比較しない。

---

## 改訂

### 改訂1 — Claude Code (2026-08-19)

**箇所**: `最終案` / `結論` の原則の段落

**(前)** 並列化は取得とLLM実行の待ち時間だけを削り、**保存されるデータを1バイトも変えない**。

**(後)** 並列化は取得とLLM実行の待ち時間だけを削り、**保存される状態を意味的に変えない**。（以下、同値の定義と除外対象を追加）

**理由**: バイト同一は正しい実装でも達成できず、受け入れ条件として成立しない。fetch captureは`uuid7()`と現在時刻を毎回書くため（`feedian/store.py:424-436`）、並列化で完了順と時刻が変われば必ず異なる。原則の意図は「直列実装が保存していた情報を落とさない」であり、それを検証可能な意味的同値として書き直した。落としてはならないものの列挙は具体化しており、原則は弱めていない。

**根拠**: レビュー8 指摘14（Codex, 2026-08-19）、レビュー9

### 改訂2 — Claude Code (2026-08-19)

**箇所**: `最終案` / `2. syncの本文取得の並列化` / `構造` の直後

**(前)** （該当する規定なし。(A)providerループはitem単位でchunkへ積むとだけ記していた）

**(後)** `#### 取得の単位はitemではなくresourceである` を追加。chunk内に`resource_id`単位のin-flight表を置き、最初に現れたitemだけが取得と予算消費を担当し、後続itemは同じ結果へ合流する規則を明記した。RSS fallbackの`embedded_content`は入力順で最初のitemのものを使う。検証10a・10bを追加した。

**理由**: chunk化は、直列実行が暗黙に持っていた同一resourceの重複抑止を壊す。直列では前のitemの結果保存後に次を判定するため`should_fetch_resource`が効くが、chunkを先に組むとその保護が消える。resourceは正規化URLで共有される一方（`feedian/store.py:1372-1377`）、RSSの`source_id`はfeed識別子由来（`feedian/rss.py:150`）、Raindropは`_id`由来（`feedian/canonical.py:76`）であり、同一URLが複数itemとして同じchunkへ入り得る。放置すると同じページを複数回取得し、`--limit`を重複消費し、同一resourceへ複数のcaptureを書く。

**根拠**: レビュー8 指摘12（Codex, 2026-08-19）、レビュー9

### 改訂3 — Claude Code (2026-08-19)

**箇所**: `最終案` / `4. 確定仕様の改訂規則`、および文書のステータス行

**(前)** `#### ステータスに \`対応中\` を足す` — 仕様のステータスを `草案 | レビュー中 | 確定 | 対応中` とし、`対応中` は「内容は確定しており、実装が進行中である」を表す。あわせて `AGENTS.md:39` のステータス一覧へ `対応中` を足す。

**(後)** 当該セクションを削除。仕様のステータスは `草案 | レビュー中 | 確定` の3つのまま維持する。実装の進捗はbranchとreview文書で管理する。`却下した案` へ不採用として記録した。

**理由**: 本仕様の他の3項目がいずれも実測された問題を解いているのに対し、`対応中` だけは実測された問題を持たない。実装の進捗はbranchとPRから読めており、状態を1つ増やすことで解ける問題を具体的に示せなかった。加えて、遷移主体、仕様単独commitと実装commitの境界、既存仕様への適用有無、この仕様自身へ適用するbootstrap手順のいずれも未定義であり、定義するコストに見合う便益が無い。撤回によりこれらは発生せず、`AGENTS.md:88-93` の現行ライフサイクルをそのまま使える。

**根拠**: レビュー8 指摘13（Codex, 2026-08-19）、レビュー9、人間の選択（案1）

### 改訂4 — Claude Code (2026-08-19)

**箇所**: `最終案` / `2. syncの本文取得の並列化` / `取得の単位はitemではなくresourceである`（改訂2で追加した節）

**(前)** chunk内に `resource_id` 単位のin-flight表を置く。最初に現れたitemだけが取得と予算消費を担当し、**後続itemは同じ結果へ合流する。** 取得結果の保存は1回、`record_sync_item` は参照する全source itemへ記録する。RSS fallbackの `embedded_content` は入力順で最初に取得を担当したitemのものを使う。

**(後)** 同一resourceのitemを**同じchunkへ入れず、後続chunkへ繰り延べる。合流はさせない。** 繰り延べたitemは、先行itemの結果が保存された後に通常のmain thread経路で `should_fetch_resource` を再評価する。RSS fallbackの特別規則は削除した。

**理由**: 合流は成功経路しか再現しない。(1) 先行itemの取得が失敗した場合、直列実装では後続itemがbackoffで取得されず自身のauditは `completed` になるが（`feedian/sync.py:195-199`）、合流させると `failed` になり監査内容が変わる。(2) `force_fetch=True` では `should_fetch_resource` が先頭で常に `True` を返すため（`feedian/store.py:913-914`）、直列実装はitemごとに取得して各回が予算を消費するが、合流は1回へ畳んで取得回数・`--limit` 消費・最後に保存される応答を変える。いずれも `結論` の意味的同値の要求に反する。繰り延べは現行の判定をそのまま通すため、3経路すべてが自動的に一致する。

**根拠**: レビュー10 指摘15（Codex, 2026-08-19）、レビュー11

### 改訂5 — Claude Code (2026-08-19)

**箇所**: `最終案` / `3. ingestのLLM実行の並列化` / 実行枠と開始間隔の節、および `manus-api` の行

**(前)** `min_start_interval_seconds` のlockもbackend IDごとに持つ。`_wait_for_manus_create_slot` と同じく、**sleepを抱えたままlockを保持する**形へ置き換える。（間隔待ちはworker側）

**(後)** `min_start_interval_seconds` はworkerで待たない。**schedulerの投入条件**にする。main threadは「次に開始してよい時刻」を過ぎた候補だけをsubmitし、未到達なら投入せず結果回収へ戻る。あわせて `manus-api` の `min_start_interval_seconds` を `6.1` と宣言し、ingest経路では `_summarize_with_manus` のgateを迂回する（legacy export経路は現行どおり）。

**理由**: 待機をworkerへ置くと、**Futureが RUNNING でありながら外部リクエストをまだ送っていない状態**ができる。`_summarize_with_manus` は最初の1行で `_wait_for_manus_create_slot()` を呼び（`feedian/llm.py:337-344`）、このgateは最大6.1秒sleepする（同 `:487-495`）。この間にCtrl-Cが入ると `Future.cancel()` は `False` を返すため、中断契約はそのFutureを開始済みとしてdrainし、**gateを抜けたworkerが中断後に新しい `task.create` を送る。** 「中断後に新しいbackend callが開始されない」に反する。間隔をschedulerの投入条件へ移すと、RUNNINGと「送る直前」が一致し、worker側のlockとsleepも不要になる。

**根拠**: レビュー12 指摘17（Codex, 2026-08-19）、レビュー13

### 改訂6 — Claude Code (2026-08-19)

**箇所**: `最終案` / `4. 確定仕様の改訂規則` / 規則の箇条書き

**(前)** 変更内容は追記で記録する。**文書末尾の** `## 改訂` へ `### 改訂<N>` の見出しで1件ずつ積む。

**(後)** セクション順序を `最終案` → `改訂` → `草案` → `レビュー` に固定する。`## 改訂` は `最終案` の直後に置き、文書末尾に置いてはならない。改訂は確定の前後を問わず記録する。

**理由**: `## 改訂` を文書末尾に置くと、確定前にレビューが続いたときの追記先が無くなる。実際に本文書では、`## 改訂` の後に追記したレビュー11がMarkdown上 `## 改訂` の配下へ入り、改訂4とレビュー見出しが同じ階層で混在した。`## レビュー` を最後に固定すれば追記先が常に空く。あわせて本文書の構造も並べ直した。読み手にとっても、現行の決定（`最終案`）の直後にその変更履歴（`改訂`）が来る順序が自然である。

**根拠**: レビュー12 指摘18（Codex, 2026-08-19）、レビュー13

### 改訂7 — Claude Code (2026-08-19)

**箇所**: `最終案` / `3. ingestのLLM実行の並列化` / schedulerの節と `課金の扱い`

**(前)** 未到達なら投入せず、結果の回収へ戻り、次の周回で再判定する。**これにより「FutureがRUNNING」と「外部リクエストを送る直前」が一致する。** ／ gracefulなCtrl-Cでは未開始分は課金されず、開始済み分だけが完了を待って監査に残る。

**(後)** 課金へのcommit境界を**FutureのRUNNING遷移**と定義する。`cancel()` に成功したFutureは課金されない。`cancel()` に失敗したFutureは、**まだwireへ送っていなくてもCtrl-C後に送信される可能性がある。** 開始間隔が保証するのは**scheduler投入時刻の間隔**であり、wire送信時刻ではない。helpには「開始済みのリクエストは課金される場合があります」と書く。

**理由**: `ThreadPoolExecutor` は `set_running_or_notify_cancel()` でRUNNINGへ遷移させてからcallableを呼び、callableはpayloadとHTTP requestを組み立ててから外部I/Oを始める。**この窓は6.1秒のgateを除いても必ず残る。** 「一致する」と書けば嘘であり、検証12aの「その状態が発生しない」は証明できない要求だった。窓を厳密に閉じるには協調cancelを全backendのinterfaceへ通す必要がある（レビュー13で不採用）。窓に残るのはpayload組み立てだけで待機は一切なく、最悪でも中断1回につきリクエスト1件である。契約と検証を実在する境界へ合わせる。

**根拠**: レビュー14 指摘19（Codex, 2026-08-19）、レビュー15

### 改訂8 — Claude Code (2026-08-19)

**箇所**: `最終案` / `3. ingestのLLM実行の並列化` / schedulerの節

**(前)** **main threadはここでsleepしない。** 未到達なら投入せず、結果の回収へ戻り、次の周回で再判定する。

**(後)** schedulerの待機条件を「いずれかのFutureの完了、または最も早いbackend開始可能時刻」と定義する。実行中Futureがあれば `concurrent.futures.wait(pending, timeout=next_due-now, return_when=FIRST_COMPLETED)`、無ければmain threadでその timeout だけ待つ。

**理由**: 開始可能時刻が未到達で回収対象のFutureが1つも無い状態は通常に起きる（直前のtaskがすべて完了した直後など）。空集合への `wait` は即座に返るため、次の6.1秒間をmain threadが回り続けるbusy loopになる。この待機はFutureも `llm_run` も作る前に行うので、改訂5が解いた「RUNNINGだが未送信」の問題を再導入しない。main threadの待機はCtrl-Cで中断できる。

**根拠**: レビュー14 指摘20（Codex, 2026-08-19）、レビュー15

### 改訂9 — Claude Code (2026-08-19)

**箇所**: `最終案` / `3. ingestのLLM実行の並列化` / `課金の扱い`

**(前)** この窓に残るのはpayload組み立てだけで、sleepもlockも待機も無い。6.1秒のgateを除いた後は、**最悪でも中断1回につきリクエスト1件**である。

**(後)** 窓は短いが**同時に複数存在し得る**。Ctrl-C後に送られる可能性がある件数は、その時点でRUNNINGかつ未送信のFuture数以下であり、global worker数とbackendの `max_parallelism` で上限を受ける。利用者が選んだ並列度を超える課金は発生しない。helpにも件数の上限を書く。

**理由**: 「1件」は窓の**長さ**についての観察を**件数**へ誤って一般化したものだった。`openai-responses` は `max_parallelism = 8` かつ `min_start_interval_seconds = 0` であり、schedulerは枠が空いていれば8件をsubmitする。各workerは独立にRUNNINGへ遷移するため、Ctrl-C時点で8件すべてがRUNNINGかつwire未送信であり得る。`manus-api` が通常1件以下に収まるのは6.1秒の投入間隔という固有の性質によるもので、全backendへ一般化できない。協調cancelを不採用とする判断は変わらないが、その根拠を正しい件数の上で述べ直した。

**根拠**: レビュー16 指摘21（Codex, 2026-08-19）、レビュー17

## 草案

### 結論

`feedian sync` が新着ゼロでも5分以上かかる原因を取り除き、あわせて本文取得とLLM実行を並列化する。
変更は4つある。

1. **はてブ収集の早期打ち切り** — 検索APIの返却順を実測したので、`20260818-sync-quick-mode.ja.md` が保留した判断を確定させる。
2. **syncの本文取得の並列化** — DB書き込みはmain threadに残し、取得だけをworkerへ出す。
3. **ingestのLLM実行の並列化** — 同じ形。backendごとに並列上限を宣言させる。
4. **確定仕様を改訂できるようにする規約変更** — 「確定後は編集しない」が、誤りを直せない制約になっている。

並列度は対象ごとに別の設定キーを持つ。

### 計測（2026-08-19、参照Vault）

判断の根拠はすべて実測である。

| 対象 | 実測値 |
|---|---|
| はてな検索API `q=https` の総件数 | 5,018件 |
| はてな検索API `q=http` の総件数 | 1,969件（合計6,987件。実行時の表示と一致する） |
| 同APIの1リクエスト所要 | 3.9〜4.4秒（100件/リクエスト、約72リクエストで5分10秒） |
| 同APIの返却順 | 両クエリ・offset 0と3000のすべてのページで `timestamp` の**降順**を確認 |
| browser経由の本文抽出 | `fetch_capture` 9,877件のうち99件（**1.0%**） |
| 本文取得の所要 | 683件で8分20秒（約0.73秒/件） |
| `llm_run` の所要 | 成功4,686件の平均3.40秒、最大34.5秒 |
| 本文未取得のresource | 2,575件 |
| resourceのホスト偏り | `x.com` 3,304件、`anond.hatelabo.jp` 805件、`togetter.com` 307件 |

実行ログ上の5分13秒・5分10秒・4分32秒は、72リクエスト × 約4.3秒でそのまま説明できる。
`request_interval_seconds = 0.3` が占めるのは22秒（7%）にすぎず、律速はAPI自体のレイテンシと直列実行である。

---

### 1. はてブ収集の早期打ち切り

#### 現状

`_provider_items` のhatena分岐（`feedian/sync.py:310-330`）は、渡された `quick`・`known`・`stop_after_known_pages` を
**一つも参照していない**。早期打ち切りを実装しているのはraindrop分岐だけである。したがってhatenaは
quickでも毎回 `fetch_hatena_bookmarks`（`feedian/hatena.py:222-290`）で全件を走査し、
その後に下流で6,900件を `skipped` として捨てている。

#### 保留していた前提が実測で解けた

`20260818-sync-quick-mode.ja.md` は、hatenaを全件収集のまま残した理由をこう書いた（同 :290）。

> 検索APIの返却順が降順である保証を確認できていない。`fetch_hatena_bookmarks`は取得後に`created_at`で並べ替えており、実装者もAPI順序に依存していない

同 :408 は「実測が取れれば、Hatenaにも早期打ち切りを導入できる。本仕様では意図的に見送っている」としている。
その実測を取った。両クエリについて offset 0 と offset 3000 のページを取得し、`timestamp` が厳密に降順であることを確認した。

```
q=https of=    0  total=5018  desc=True   1787077443 → 1783860170
q=https of= 3000  total=5018  desc=True   1488733552 → 1484118045
q=http  of=    0  total=1969  desc=True   1775570348 → 1514200513
```

保留の根拠が消えたので、raindropと同じ判定をhatenaへ入れる。

#### 変更

`fetch_hatena_bookmarks` に3つの引数を足す。

```python
def fetch_hatena_bookmarks(
    ...,
    known: set[str] | None = None,
    stop_after_known_pages: int = 0,      # 0 は打ち切り無効（fullの既定）
    on_stopped_early: Callable[[], None] | None = None,
) -> list[CanonicalItem]
```

- **打ち切り判定はクエリごとに独立して持つ。** `SEARCH_QUERIES` の各クエリについて
  「そのページのitemがすべて `known` に含まれる」が `stop_after_known_pages` 回連続したら、そのクエリだけを打ち切る。
  片方のクエリが1ページで止まり、もう片方が走査を続けるのは正常な状態である。新規ブックマークがどちらの
  全文検索トークンに載るかは事前に分からないため、クエリ間で判定を共有してはならない。
- **閾値は既存の `fetch.quick_stop_after_known_pages` を再利用する。** provider固有の新しいキーは作らない。
- **戻り値はlistのまま変えない。** クエリ間の重複排除と `created_at` によるソート（`feedian/hatena.py:289`）は
  収集完了後にしか行えず、収集自体が通常2リクエストで終わる以上、generator化の利得がない。
- `on_stopped_early` はどれか1つのクエリでも打ち切ったときに1回だけ呼び、`sync.py` 側で
  `stopped_early` へ `hatena` を積む。
- **`limit` の意味をquickではraindropに揃える。** 現行はクエリごとの `offset` を `limit` と比較しており
  （`feedian/hatena.py:239-241`）、早期打ち切りと組み合わせると停止条件が二重になる。
  quickでは raindrop と同じく「実際に取り込む新規itemの件数」を数える。fullは現行のまま変えない。

#### 既知のリスクと、機構を足さない理由

降順は**実測であって、はてなが文書化した保証ではない**。順序が変われば、quickは新着を黙って取りこぼす。
復旧経路は `--full` であり、これはraindropが既に負っているリスクと同じ性質である。

raindropに無くhatenaにある差が1つある。**同一URLの再ブックマークは `timestamp` を更新して先頭へ移動する。**
raindropの `sort=-created` はタグ編集で動かないが、hatenaは動く。したがって理論上は、
1回のsync間隔に100件以上の再ブックマークが挟まると、その下にいる新規itemを取りこぼす。
`quick_stop_after_known_pages` を上げれば緩和できる。日常運用で1回のsync間隔に100件を再ブックマークすることは
起こらないため、これを閉じるための機構は足さない。取りこぼしても `--full` で回収でき、保存済みのデータは失われない。

#### 効果

新着ゼロで72リクエスト → 2リクエスト。**5分10秒 → 約9秒。**

---

### 2. syncの本文取得の並列化

#### 動かせない2つの制約

**(a) DBはmain thread専用である。**
`VaultStore.open` は `sqlite3.connect(database_path)` を既定の `check_same_thread=True` で開く（`feedian/store.py:91`）。
接続を作ったthread以外から使えない。したがって `store.*` の呼び出しはすべてmain threadに残す。
ローカルSQLiteへの書き込みが律速になることはないため、これは制約であって性能上の問題ではない。

**(b) playwrightはmain threadに固定する。**
`render_html_with_browser` は module-global の `_browser_runtime` / `_browser` を1つだけ持ち（`feedian/extract.py:717-763`）、
playwrightのsync APIは生成したthreadに束縛される。worker threadから触ると壊れる。

ここで**browser経由の抽出は全取得9,877件のうち99件、1.0%**という実測が効く。
1%のためにworkerごとにchromiumを立てる必要も、browser専用のpoolを作る必要もない。

→ `fetch_page_text`（`feedian/extract.py:213`）に `allow_browser: bool = True` を足す。
workerは `allow_browser=False` で呼び、browser描画が必要と判定された場合は結果に「browser保留」を立てて返す。
main threadが結果を回収するときに `fetch_page_text_with_browser`（`feedian/extract.py:413`）を直列に実行する。
`feedian/extract.py` のglobalには一切手を入れない。

#### 構造

既存の `_store_hatena_comments_parallel`（`feedian/sync.py:610-673`）が既にこの形をとっている。
**workerは純粋な取得だけを行い、main threadが結果を回収して書き込む。** 同じ形を2箇所へ広げる。

- **(A) providerループ** — main threadが `upsert_canonical_item` と `should_fetch_resource` まで進め、
  取得が必要なitemをchunk単位でpoolへ投げ、`as_completed` で回収して `_store_page` する。
  chunkサイズは `workers * 4`。chunk内の処理順は結果に影響しない。
  `--limit` の予算（`provider_fetch_attempts`）はchunkを組む時点でmain threadが決めるため超過しない。
- **(B1) `_run_quick_body_only_pass`**（`feedian/sync.py:426-`）— 対象がresourceのリストなので同じ形をそのまま適用する。
- 進捗の意味は変えない。`progress(processed + skipped, item)` はchunk回収時にmain threadから呼ぶ。

#### 設定

`fetch.workers`（既定8）。既存の `fetch.comment_workers`（既定8）は独立した設定として残す。

#### 効果

683件の本文取得が8分20秒 → 約1分（8並列）。browser描画をmain threadで直列に行うため、実効は8倍には届かない。

---

### 3. ingestのLLM実行の並列化

#### 分割が必要な箇所

`_attempt_candidate`（`feedian/ingest.py:348-412`）は `start_llm_run` → `backend.summarize` → `finish_llm_run` を
一続きで行う。DBがmain thread専用である以上、これを分割する。

- main thread: `start_llm_run`
- worker: `backend.summarize` だけ
- main thread: `finish_llm_run`、`put_source_note`、集計、進捗

**fallback経路もmain threadに残す。** `feedian/ingest.py:250-276` のfallbackは `_candidate(store, ...)` を呼んでDBを読む。
workerは1次backendの `summarize` だけを担当し、fallbackが必要な結果はmain threadが判定してから改めてpoolへ投げる。

#### 実行間隔の制御を共有状態にする

`min_start_interval_seconds`（`feedian/llm_backends.py:87`）の間隔制御は、現在 `last_start` というlocal変数である
（`feedian/ingest.py:206`）。並列化すると全workerが同じ値を読んで同時に発射する。
`_wait_for_manus_create_slot`（`feedian/llm.py:487-495`）と同じく、
**sleepを抱えたままlockを保持する**形へ置き換える。同じ問題を解いた実装が既にrepo内にある。

#### backendごとに並列上限を宣言させる

`BackendCapabilities`（`feedian/llm_backends.py:79`）へ `max_concurrency: int = 1` を足す。
実効並列数は `min(config.llm.workers, backend.capabilities.max_concurrency)` とする。

- `openai-responses` / `manus-api` — HTTPなので1より大きい値を宣言する。
- `codex-local` — **1に固定する。** 記事ごとのcwdは `mkdtemp` で一意だが（`feedian/local_agent.py:201`）、
  `CODEX_HOME` は全実行で共有しており、同時実行の安全性を確認していない。確認できるまで動かさない。

既定を1にするのは、新しいbackendを足した人が並列安全性を明示的に宣言するまで直列で動くようにするためである。

#### 課金の扱い

**並列度をそのまま同時課金数として扱う。** N件が同時に飛んでいる状態でCtrl-Cしても、投げ終えた分は戻らない。
直列実行でも1件は同じだが、並列ではNになる。`--limit` の予算はpoolへ投げる時点でmain threadが決めるため超過しない。
この挙動はhelpに明記する。

#### 設定

`llm.workers`（既定8）。

#### 効果

`llm_run` の成功4,686件の平均所要は3.40秒。新規100件で340秒 → 8並列で約45秒。

---

### 4. 確定仕様を改訂できるようにする

#### 問題

`AGENTS.md:70` は `docs/specs/` を "Do not edit after finalization" と定めている。
この制約は、**確定仕様が誤っていると分かっても直せない**という運用を生む。
現に `20260818-sync-quick-mode.ja.md` のはてブ判断は、前提（返却順が不明）が実測で崩れたのに
そのまま残っており、`DESIGN.md` にも同じ記述が写っている。

さらに悪いのは、これが「確定仕様がそう書いてあるから」という、
**対応を次回へ持ち越す言い訳になる**ことである。

#### 新しい規則

- **確定仕様は常に1つの形だけを持つ。** 現行の決定と旧版が同じ文書内に並存してはならない。
  誤りが分かったら `最終案` の本文をその場で書き換える。読者が最初に見る `最終案` が、常に現行の決定である。
- **変更内容は追記で記録する。** 文書末尾の `## 改訂` セクションへ
  `### 改訂<N> — <名前> (YYYY-MM-DD)` の見出しで1件ずつ積む。
- **改訂の記録は `(前)` → `(後)` を本文中に書く。** 変更前後の記述を引用し、理由と根拠を添える。
  git logを引かなくても文書だけで変更が追えるようにするためである。
- **コミット前後を問わず同じ手順を踏む。** 確定させたがまだコミットしていない仕様も、確定済みでコミット済みの仕様も、
  扱いは同一である。**コミット前は git に旧版が残らないため、`(前)` の記述を書き残すことがより重要になる。**
- **ステータスは `確定` のまま据え置く。** `改訂` という状態は足さない。状態が増えると判定が増える。
- **`草案` と `レビュー` は従来どおり追記のみとする。** ADRとしての価値、すなわち却下案が残ることの価値はここにある。
  改訂できるのは `最終案` だけである。
- **確定仕様の誤りを、対応を先送りする理由にしない。** 誤りを見つけた時点で、改訂と実装を同じ流れで扱う。
- ファイル名は従来どおり改名しない。

#### `## 改訂` の記法

```markdown
## 改訂

### 改訂1 — Claude Code (2026-08-19)

**箇所**: 「Raindropのみ収集を早期に打ち切る」の段落

**(前)** Hatenaは検索APIの返却順に保証が無いため全件収集を維持し、RSSは……

**(後)** HatenaもRaindropと同様に、既知ページが連続したところで収集を打ち切る。RSSは……

**理由**: 検索APIの返却順を実測し、両クエリ・offset 0と3000の全ページで `timestamp` の降順を確認した。
「返却順の保証が無い」という保留の根拠が消えたため。

**根拠**: [syncとingestのスループット](20260819-sync-ingest-throughput.ja.md)
```

#### AGENTS.md への反映

- `AGENTS.md:70` の表「Do not edit after finalization」を、上の規則へ差し替える。
- `AGENTS.md:91-93` の「確定前は追記のみ」「草案は誤りが見つかっても直さない」は**そのまま残す。**
  改訂できるのは確定した `最終案` だけであり、草案とレビューの追記専用性はADRとしての価値そのものである。
- `AGENTS.md` の「Structure」に `## 改訂` セクションと `(前)` / `(後)` の記法を追記する。

#### 本仕様の実装で行う適用

- `20260818-sync-quick-mode.ja.md` の「Hatenaは全件収集を維持する」判断を、上の記法で改訂し本仕様へリンクする
  （同 :70, :124, :231, :290, :297, :408）。
- `DESIGN.md`「Syncのモード」節の「Hatenaは検索APIの返却順に保証が無いため全件収集を維持し」を書き換える。
- `DESIGN.md` に「並列処理」節を新設し、DB単一thread・playwright固定・3つのworker設定を要約する。
- `README.md` の設定表に `fetch.workers` と `llm.workers` を足す。

---

### 設定キー一覧

| キー | 既定 | 対象 |
|---|---|---|
| `fetch.workers` | 8 | syncの本文取得（(A)ループと(B1)passの双方） |
| `fetch.comment_workers` | 8 | Hatenaコメント取得（既存。変更しない） |
| `llm.workers` | 8 | ingestのLLM実行。実効値はbackendの `max_concurrency` で上限を受ける |
| `fetch.quick_stop_after_known_pages` | 1 | raindropとhatenaの早期打ち切り閾値（既存。hatenaへ適用範囲を広げる） |

いずれも起動時に検証する。bool、非整数、1未満はエラーとする。
`quick_stop_after_known_pages` が既に持っている検証（`feedian/sync.py:61-67`）と同じ形にする。

---

### 却下した案

| 案 | 判断 | 理由 |
|---|---|---|
| worker threadごとにplaywright browserを持つ | 不採用 | browser経由は全取得の1.0%（99/9,877）。chromiumを8個立ち上げる費用に見合わない |
| browser専用のworker poolとqueueを作る | 不採用 | 同上。main threadでの直列実行で足りる。1%のために新しい実行機構を持ち込まない |
| `check_same_thread=False` にしてDB書き込みも並列化する | 不採用 | ローカルSQLiteの書き込みは律速ではない。WALでも書き込みは直列化されるため得るものが無く、トランザクション境界だけが壊れる |
| hatenaに専用の `hatena_stop_after_known_pages` を作る | 不採用 | 同じ概念に2つのキーを持たせない。providerごとの調整が必要だと分かってから足す |
| `q=http` クエリを落として収集を半減する | 不採用 | 実測totalは https 5,018 / http 1,969 で合計6,987。ほぼ加算的であり両クエリの集合は素に近い。落とすと取りこぼす |
| はてなの `request_interval_seconds` を下げる | 不採用 | 0.3秒 × 72回 = 22秒で、5分10秒の7%にすぎない。律速ではない |
| はてなの収集自体を並列化する | 不採用 | 早期打ち切りで72リクエストが2リクエストになる。並列化する対象が残らない |
| 同一ホストへの同時取得を1本に制限する | 不採用 | 通常のWebブラウザはHTML本文以外のリソースを同時に要求するため、8本は極端な数ではない。弾かれる事象を観測してから対処する。先回りしてlock辞書を持ち込まない |
| 確定仕様に `改訂` ステータスを足す | 不採用 | 「確定か改訂か」という判定が増える。改訂履歴はセクションで足りる |
| 誤った確定仕様は改訂せず、新しい仕様で上書きする | 不採用 | 読者が旧仕様に当たったとき、それが現行でないと分からない。逆リンクの維持コストも生じる |

---

### 検証

1. quick + 新着ゼロで、hatenaのリクエストが各クエリ1ページに収まり、`stopped_early` に `hatena` が入る。
2. quick + 新着1件で、その1件だけが処理される。
3. fullでは早期打ち切りが効かず、従来どおり全件を走査する。
4. `quick_stop_after_known_pages = 2` で、各クエリが2ページ走査してから止まる。
5. 片方のクエリだけが打ち切られ、もう片方が走査を続ける状況を再現する。
6. `fetch.workers = 1` のとき、並列化前と同一の結果になる。
7. browser fallbackを要するURLを含むバッチで、playwrightがmain threadだけで動く。
8. `--limit` がsyncの並列化後も超過しない（(A)と(B1)の共有予算を含む）。
9. `llm.workers` を上げても `codex-local` は直列で動く。
10. `min_start_interval_seconds > 0` のbackendで、並列時もstart間隔が守られる。
11. ingestを中断したとき、`llm_run` に `running` のまま残る行が出ない。
12. 4つの設定キーの不正値（0、負、bool、非整数）が起動時にエラーになる。
13. quickの既存の契約が変わっていない。provider metadataの差分、Hatenaコメントの増減、`refresh_days` 再取得は
    引き続きquickでは反映されない。

## レビュー

### レビュー1 — Codex (2026-08-19)

結論は**要修正**である。Hatenaの全件走査を日常のquick runから外し、DB書き込みをmain threadに限定したまま外部I/Oだけを並列化する方向は妥当である。実測値も、どこへ手を入れると日常実行の待ち時間が減るかを十分に示している。

一方、早期打ち切りが依存する順序の証拠は、草案に記録された内容だけではページ境界を含む全体順序を立証していない。また、syncのRSS fallback、ingestのfallback backend、中断時の`llm_run`終端という既存契約が、提示されたpool構造では一意に再現できない。これらは性能差ではなく本文または監査データの正しさに関わるため、指摘1から4を解消してから最終案へ進める必要がある。設定契約と仕様改訂規則も、それぞれ現行構造との不一致を解消する必要がある。

#### 草案の採否

| 項目 | 採否 | 理由 |
|---|---|---|
| Hatenaにも既知ページによる早期打ち切りを導入する | 修正して採用 | 日常運用上の効果は大きいが、打ち切りの前提となるクエリ全体の降順をページ境界込みで記録する必要がある |
| 本文取得だけをworkerへ移し、DBとplaywrightをmain threadへ残す | 修正して採用 | thread所有権の分離は正しいが、RSS本文fallbackを含む取得後処理の所有者と順序が不足している |
| LLMの`backend.summarize`だけをworkerへ移す | 修正して採用 | DBのthread制約を守れるが、fallbackを含むbackend別の上限・開始間隔と中断時のrun終端を定義する必要がある |
| backendが並列上限を宣言する | 修正して採用 | fail closedの既定1は妥当。ただし現行の`max_parallelism`と別名の`max_concurrency`を併存させてはならない |
| `fetch.workers`と`llm.workers`を追加する | 修正して採用 | 対象別の設定は妥当だが、構造化されている`llm`設定のparse・render契約を明記する必要がある |
| 確定仕様の`最終案`を現行決定へ書き換える | 保留 | `docs/specs/`を意思決定履歴、`DESIGN.md`を現状の説明とする既存の役割分担と衝突するため、人間がどちらを正本にするか決める必要がある |

#### 指摘1: Hatenaの実測はページ境界を含む降順を立証していない — 重大度: 高

草案は「両クエリ・offset 0と3000のすべてのページ」で`timestamp`の厳密な降順を確認したとする（草案`:28`, `:51-62`）。しかし記録されている結果は`q=https`のoffset 0と3000、`q=http`のoffset 0という3ページの先頭・末尾だけであり、offset 3000が総件数1,969件を超える`q=http`には対応する観測もない。この3点から分かるのは各サンプルページの内部順序であって、offset 100の先頭がoffset 0の末尾より新しくないことや、未観測ページ内に新しい行が戻ってこないことではない。

早期打ち切りに必要なのは、各クエリの結果列がページ境界をまたいで全体として`timestamp`の非増加順であることである。途中の全既知ページより後ろに新規itemが1件でも現れる順序なら、そのitemを黙って取りこぼす。これは草案自身がデータ欠落リスクとして扱うべき条件であり、ページ内のサンプルだけでは受け入れられない。

実際に全72リクエストを保存した計測であるなら、各クエリについて全行をoffset順に連結し、隣接する全組（特に各ページの末尾と次ページの先頭）が非増加であること、検査行数、最小・最大offsetを記録すること。同一秒のbookmarkがあり得るため、必要な契約は「厳密な降順」ではなく「非増加」である。全件の再計測が必要でも1回限りであり、毎回5分を削る判断の根拠として過剰な機構には当たらない。

**採否: 修正して採用。** Hatenaの早期打ち切り自体は採るが、クエリ全体の順序をページ境界込みで確認した証拠へ差し替えるまでは確定しない。

#### 指摘2: worker回収後に`_store_page`するだけではRSS本文fallbackを失う — 重大度: 高

草案のproviderループは、worker結果を`as_completed`で回収して`_store_page`すると定める（草案`:135-140`）。現行処理はその前に、取得が例外になった場合でも未保存resourceなら`item.embedded_content`を`rss-feed-fallback`として保存し（`feedian/sync.py:155-169`）、取得結果が空なら`page.text`へ同じ本文を補ってから`_store_page`する（`:170-181`）。`_store_page`自身は`page.error`があり本文が空なら失敗captureを記録してreturnするだけであり（`:385-423`）、RSS本文を知らない。

したがって草案どおり「取得結果を回収して`_store_page`」とだけ実装すると、HTTP本文が空または取得処理が例外になったRSS itemで、すでにfeedから得ていた本文を保存しなくなる。これは速度上の差ではなく、利用可能な本文を落とすデータ完全性の後退である。

workerの戻り値を`PageFetchResult`または例外情報に限定し、main threadが現行と同じ順序で、(1) `not_modified`、(2) 空本文または例外時の`embedded_content` fallback、(3) `_store_page`、(4) `record_sync_item`を処理すると明記すること。検証6の一般的な「workers=1で同じ結果」だけでなく、RSSについて少なくとも通常本文、空本文、取得例外、304の4経路を並列度1と8で固定する必要がある。

**採否: 修正して採用。** 取得だけをworkerへ移す境界は維持するが、RSS fallbackを含む取得後処理は明示的にmain threadへ残す。

#### 指摘3: fallback backendのpoolと開始間隔が未定義で、直列制約を破り得る — 重大度: 高

草案は1次backendの実効並列数を`min(llm.workers, backendの上限)`とし、fallbackが必要ならmain threadで判定して「改めてpoolへ投げる」とする（草案`:163-176`）。しかしfallbackは1次backendとは別のbackendであり、別の並列上限と`min_start_interval_seconds`を持ち得る。たとえばHTTP backendを8並列で動かすpoolへ`codex-local` fallbackを複数投入すれば、草案が安全性未確認を理由に1へ固定した制約をそのまま破る。逆方向では、`codex-local`の1本poolがHTTP fallbackまで不必要に直列化する。

さらに`BackendCapabilities`にはすでに同じ意味の`max_parallelism: int = 1`が存在する（`feedian/llm_backends.py:78-87`）。ここへ`max_concurrency`を追加すると、どちらが実効値かで新しい不整合が生じる。

実行枠と開始間隔のlockはbackend IDごとに持ち、1次・fallbackのどちらも**実際に呼ぶbackend自身**の`max_parallelism`と`min_start_interval_seconds`を通ると定義すること。fallbackの`preflight`もmain threadで1回だけ行い、その後の`_candidate`のDB参照、workerへの投入、結果のDB反映という所有境界を記すこと。新しい能力名は足さず既存の`max_parallelism`を使うか、名称を変更するなら全定義を一度に置換して単一の名前だけを残す必要がある。

**採否: 修正して採用。** backendごとのfail-closedな上限は採るが、1次backendだけでなくfallbackを含むscheduler契約へ広げ、能力名を一本化する。

#### 指摘4: Ctrl-C時に開始済み`llm_run`をどう終端するか決まっていない — 重大度: 高

草案はmain threadが`start_llm_run`を呼んでからworkerへ渡し、完了後にmain threadが`finish_llm_run`するとする（草案`:156-161`）。この構造では、futureを先に大量投入すると、まだworkerが実行していないqueue内の候補までDB上は`running`になる。さらに`KeyboardInterrupt`は`Exception`ではなく`BaseException`なので、現行の`_attempt_candidate`の失敗処理（`feedian/ingest.py:376-394`）でもrunを閉じない。`sync_run`には次回起動時の回収処理がある（`feedian/store.py:732-744`）一方、`llm_run`には同等の回収処理がない。

この状態で検証11の「中断後にrunning行がない」だけを置いても、実装者は次のいずれを選ぶべきか決められない。

1. 新規投入を止め、実行中futureが終わるまで待って成功・失敗を保存してから`KeyboardInterrupt`を再送出する。
2. backendが安全にcancelできる場合だけcancelし、未開始・cancel済みrunを`interrupted`相当の失敗へ終端する。
3. 停止不能なHTTP/remote処理を失敗へ書き換える場合、後から成功レスポンスが返っても保存しないと明示する。

同時に保持するfuture数を実効worker数に対する有限のwindowへ制限し、`run_id`とfutureをmain threadで対応付けること。gracefulなCtrl-Cのcleanupに加え、強制終了で残った`running`行を次回ingest開始時に失敗へ回収する規則も必要である。検証は、実行中と未開始のfutureが双方ある時点で中断し、全runの最終status、source noteの有無、新しい課金リクエストが中断後に開始されないことを確認する形へ具体化すること。

**採否: 修正して採用。** 同時課金数が最大Nになる説明は採るが、開始済みrunの状態遷移とexecutor停止手順が決まるまでは実装可能な契約になっていない。

#### 指摘5: `llm.workers`は現行のVault設定では表現できない — 重大度: 中

`fetch`は任意キーを保持するdictだが、`llm`は`LLMSettings` dataclassであり（`feedian/vault.py:43-47`）、parserは`backend`、`model`、`fallback`以外をunknown fieldとして拒否する（`:321-362`）。`render_vault_config`もこの3項目しか出力しない（`:258-275`）。したがって設定表へ`llm.workers`を書くだけでは、利用者がそのキーを置いた時点でVaultを開けない。

`LLMSettings.workers`の既定値、`_parse_llm`での型・範囲検証、`render_vault_config`での保存を実装範囲へ明記すること。既存format version 2で任意の新キーとして後方互換に追加するのか、versionを上げるのかも決める必要がある。前者なら、キー欠落は既定8として受理し、render後に8が明示されるか省略されるかを固定する。あわせて「4つの設定キーを起動時に検証する」（草案`:264-274`）の「起動時」がconfig load時か各command開始時かを統一すること。現在の`comment_workers`は`int(...)`変換後に実行される（`feedian/sync.py:225-231`, `:610-675`）ため、boolや文字列も拒否するという草案は既存挙動の変更である。

**採否: 修正して採用。** `llm.workers`は採るが、Vault configのparse・render・互換性までを設定契約に含める。

#### 指摘6: 確定仕様を「現状の正本」にすると`DESIGN.md`との役割が重複する — 重大度: 中

現行規約は重複保守を避けるため、`docs/specs/`を「なぜその決定をしたか」、`DESIGN.md`を「現在どう動くか」と明確に分けている（`AGENTS.md:64-74`）。草案は確定仕様の`最終案`を常に現行決定へ書き換える一方（草案`:212-225`）、`DESIGN.md`も実装時に書き換えるとしている（`:254-260`）。その結果、同じ現行挙動を旧仕様の`最終案`、新仕様の`最終案`、`DESIGN.md`の少なくとも3箇所で同期する必要が生じる。草案の`AGENTS.md`変更一覧はStructureと追記専用規則だけを対象にし（`:247-252`）、この役割分担を変更していないため、規約内部でもどちらが現状の正本かが矛盾する。

次のどちらかを人間が選び、規約全体を同じ答えへ揃える必要がある。

1. 現行の役割分担を維持し、確定仕様は不変のまま、旧仕様へ「後継」または「置換済み」の短いforward linkだけを許す。現行挙動の正本は`DESIGN.md`に保つ。
2. `最終案`を更新可能な正本へ変えるなら、`AGENTS.md:64-74`も変更し、複数仕様が同じ決定を扱う場合の優先順位、`DESIGN.md`との同期責任、改訂漏れの検証方法を定める。

また、この規約変更はsync/ingestの性能実装とは独立したrepository governanceである。先に別仕様として確定・反映してから本仕様が旧仕様を改訂する順序に分ければ、現行の「確定後は編集しない」に従ったまま移行でき、性能変更の採否とも分離できる。

**採否: 保留。** 誤りが分かった仕様から現行判断へ到達できるようにする目的は妥当だが、どの文書を現状の正本にするかは技術的に自動決定できない。人間の選択と理由を記録する必要がある。

### レビュー2 — Claude Code (2026-08-19)

レビュー1の指摘を、すべてコードに当てて検証した。**指摘2から5までの技術的主張はいずれも正しく、うち2件（指摘3のcapability名、指摘5の`llm`設定）は草案の明確な誤りである。** 指摘1は計測方法の不備の指摘として正しく、再計測で解消する。指摘6は目的には同意するが、草案の記述が招いた誤読を含むため、規則の文言を書き直して応答する。

#### 検証の記録

| 指摘 | 主張 | 検証結果 |
|---|---|---|
| 3 | `BackendCapabilities`に既に`max_parallelism`がある | **正しい。** `feedian/llm_backends.py:86`に`max_parallelism: int = 1`が存在する。しかも`grep`しても定義行以外に出現せず、**現在どこからも読まれていない**。この機能のために置かれて未使用のまま残っている枠である |
| 4 | `llm_run`には中断回収が無い | **正しい。** `fail_interrupted_*`は`feedian/store.py:732`の`fail_interrupted_sync_runs`だけで、`sync_vault`が起動時に呼ぶ（`feedian/sync.py:57`）。`llm_run`に同等物は無い |
| 4 | `except Exception`は`KeyboardInterrupt`を捕まえない | **正しい。** `_attempt_candidate`の失敗処理は`except Exception`である |
| 5 | `_parse_llm`は未知キーを拒否する | **正しい。** `feedian/vault.py:325-327`が`set(raw) - {"backend","model","fallback"}`を`Unknown llm field(s)`として送出する。`llm.workers`を書いた時点でVaultが開けない |
| 5 | `render_vault_config`は3項目しか出力しない | **正しい。** `feedian/vault.py:265-273`は`llm`を3フィールドから組み立てる。`fetch`は`config.fetch`をそのまま出すため`fetch.workers`は素通りするが、`llm.workers`はrenderで消える |
| 5 | `comment_workers`の厳格検証は既存挙動の変更である | **正しい。** 現行は`int(config.fetch.get("comment_workers", 8))`であり、`"8"`も`True`も通る |
| 2 | worker回収後に`_store_page`だけではRSS本文を失う | **正しい。** `feedian/sync.py:155-163`が例外時に`rss-feed-fallback` revisionを書き、`:176-179`が空本文時に`page.text`へ差し込む。`_store_page`は`page.error`かつ空本文なら失敗captureを書いてreturnするだけで（`:385-397`）、`embedded_content`を知らない |

#### 採否

| 指摘 | 採否 | 対応 |
|---|---|---|
| 1. 順序の証拠がページ境界を含まない | 採用 | 全72リクエストを走査し直し、連結列全体の非増加性で証拠を差し替える |
| 2. RSS本文fallbackを失う | 採用 | 取得後処理の順序をmain threadの契約として明記する |
| 3. fallback backendの枠と`max_parallelism`の重複 | 採用 | `max_concurrency`の新設を撤回し、既存の`max_parallelism`を使う。枠はbackend IDごとに持つ |
| 4. 中断時の`llm_run`終端 | 採用 | `start_llm_run`の呼び出し位置を変え、中断手順と回収処理を定義する |
| 5. `llm.workers`がVault設定で表現できない | 採用 | `LLMSettings`への追加、parse・render・互換性を実装範囲に含める |
| 6. 確定仕様と`DESIGN.md`の役割重複 | 修正して採用 | 役割分担は変えない。「常に現行の決定である」という草案の記述を撤回し、規則を「誤った決定を主張したまま残さない」へ書き直す |

#### 指摘1への対応

指摘は正しい。草案の`:28`は「両クエリ・offset 0と3000のすべてのページで降順を確認」と書いたが、実際に観測したのは3ページの内部順序だけであり、しかも`q=http`のoffset 3000は総件数1,969を超えるため0行しか返っていない。**ページ境界をまたぐ順序は一度も観測していない。** 早期打ち切りが必要とするのはまさにその性質である。

「厳密な降順」という表記も誤りである。実際の判定式は`a >= b`（非増加）であり、契約としても同一秒のbookmarkがあり得る以上、非増加が正しい。

再計測を実施した。全72リクエストを走査し、各クエリの全行をoffset順に連結したうえで、**隣接する全組**（ページ境界を含む）を検査した。

| クエリ | 行数 | ページ数 | 境界検査 | 違反 | 非増加 | 厳密降順 | 先頭ts | 末尾ts |
|---|---|---|---|---|---|---|---|---|
| `q=https` | 5,018 | 51 | 50箇所 | **0** | **true** | false | 1787077443 | 1251523484 |
| `q=http` | 1,969 | 20 | 19箇所 | **0** | **true** | true | 1775570348 | 1251523856 |

- 走査行数は両クエリ合計6,987行で、APIが申告した総件数と一致する。欠落は無い。
- 全行が`timestamp`を持つ（`with_timestamp == rows_seen`）。順序を判定できない行は無い。
- ページ境界の検査は合計69箇所、違反は0件である。**早期打ち切りが必要とする「クエリ結果列がページ境界をまたいで非増加である」性質を、全件で確認した。**
- **`q=https`の`strictly_descending`は`false`である。** すなわち同一`timestamp`の行が実在する。契約を「厳密な降順」と書くと実データと矛盾するという指摘は、この値によって裏づけられた。**契約は「非増加」とする。**

証拠が全件走査に置き換わったため、指摘1の条件は満たされた。ただし**これは1回の観測であり、はてなが文書化した保証ではない**という草案の但し書きは維持する。順序が将来変わった場合の復旧経路が`--full`であることも変わらない。

#### 指摘2への対応

worker戻り値を`PageFetchResult`または例外情報に限定し、**取得後処理は現行`feedian/sync.py:145-200`の順序のままmain threadに残す**と明記する。main threadが行う順序は次で固定する。

1. 例外 — `item.embedded_content`があり未保存resourceなら`rss-feed-fallback` revisionを書く。無ければ`record_failed_fetch`。
2. `page.not_modified` — `record_not_modified_fetch`。
3. 空本文かつ`embedded_content`あり — `page.text`・`page.title`・`page.extraction_method`を差し替える。
4. `_store_page`。
5. `comment_targets`への積み上げ、`record_sync_item`、`changed`加算。

検証は「`workers=1`で同じ結果」では足りない。RSSについて通常本文・空本文・取得例外・304の4経路を、並列度1と8の双方で固定する。

#### 指摘3への対応

**`max_concurrency`の新設を撤回する。** 既存の`BackendCapabilities.max_parallelism`（`feedian/llm_backends.py:86`）を使う。同義の名前を2つ持たせない。

scheduler契約を次へ広げる。

- poolは1つとし、**実行枠はbackend IDごとのsemaphoreで制御する。** backendごとに別poolを作らない。worker総数は`llm.workers`、そのなかでbackendごとの`max_parallelism`が実効上限になる。
- `min_start_interval_seconds`のlockも**backend IDごと**に持つ。
- 1次backendとfallback backendのどちらも、**実際に呼ぶbackend自身**の枠と間隔を通る。これによりHTTP 8並列のpodへ`codex-local` fallbackを投げても直列に落ちる。逆に`codex-local`が1次でもHTTP fallbackは直列化されない。
- `preflight`はmain threadでbackendごとに1回だけ実行する。
- 所有境界 — `_candidate`のDB参照はmain thread、`summarize`はworker、結果のDB反映はmain thread。

#### 指摘4への対応

Codexが挙げた3案のうち**案1を採り、加えて回収処理を足す。**

- **`start_llm_run`はworkerへ投入する直前に呼ぶ。** 投入windowを`llm.workers * 2`に制限し、queueに積んだだけの候補はDB上`running`にならない。指摘が挙げた「未実行なのに`running`」は構造として消える。
- **`KeyboardInterrupt`をmain threadで捕捉する。** 新規投入を止め、実行中futureの完了を待って成否を保存し、再送出する。停止不能なHTTP処理をcancelしない。後から返る成功レスポンスを捨てないためである。
- **強制終了で残った行は次回ingest開始時に回収する。** `fail_interrupted_llm_runs()`を足し、`fail_interrupted_sync_runs`（`feedian/store.py:732-744`）と同型に`status='running'`を`failed`へ落とす。`llm_run.status`はCHECK制約の無いTEXT列なのでschema変更は不要である。
- 検証を具体化する — 実行中futureと未投入候補が双方ある時点で中断し、(a) 全`llm_run`の最終status、(b) source noteの有無、(c) 中断後に新しい課金リクエストが開始されないこと、を確認する。

#### 指摘5への対応

`llm.workers`をVault設定の契約として定義する。

- `LLMSettings`へ`workers: int = 8`を足す。
- `_parse_llm`の許可キー集合（`feedian/vault.py:325`）へ`workers`を追加し、型と範囲を検証する。
- `render_vault_config`（`feedian/vault.py:265-273`）へ出力する。**常に明示する**（既定値でも省略しない）。設定の存在が利用者から見えるほうがよい。
- **format versionは2のまま据え置く。** キー欠落は既定8として受理する。既定値だけで成立する追加のためにversionを上げると、既存Vaultへ`feedian migrate`を強いることになり見合わない。
- 「起動時に検証」の定義を**config load時**（`load_vault_config`）へ統一する。`fetch`には現在parse関数が無く生のdictを読むだけなので、`fetch.workers`の検証もここへ足す。
- **`comment_workers`も同じ厳格検証へ揃える。** これは指摘のとおり既存挙動の変更である。参照Vaultの値は`8`（int）なので実害は無い。同種のキーに2つの検証規則を残すほうが、後のレビューで必ず指摘される。この変更は意図的なものとして記録する。

#### 指摘6への対応 — 修正して採用

**目的には同意するが、指摘が前提とする「`最終案`を現状の正本にする」は草案の意図ではない。** ただし草案の「読者が最初に見る`最終案`が、常に現行の決定である」という記述がその誤読を招いており、これは草案側の欠陥である。撤回して書き直す。

役割分担（`AGENTS.md:64-74`）は**変更しない**。

- `DESIGN.md` — 現在どう動くかの唯一の正本。実装のたびに更新する。
- `docs/specs/` — なぜその決定をしたかのADR。**決定そのものが後継仕様に置き換えられたときに限り、一度だけ訂正する。**

この区別により3箇所同期は発生しない。訂正は置き換えの瞬間に1回起きるだけで、以後その仕様は再び静止する。継続的な同期義務を負うのは`DESIGN.md`だけである。新しい規則は次へ書き直す。

> 確定仕様は、**誤りと分かった決定を主張したまま残さない。** 後継の仕様が決定を置き換えたとき、`最終案`の該当箇所を訂正し、`## 改訂`へ`(前)`→`(後)`と理由・根拠を追記する。訂正は置き換えの時点で一度だけ行う。現行挙動の説明は`DESIGN.md`が持ち、仕様がそれを二重に持つことはない。

Codexが挙げた案1（確定仕様は不変のまま、forward linkだけを許す）は**不採用**とする。これは「確定仕様が誤っていても直せない」という、この変更が解こうとしている状態そのものである。人間の指示は「確定仕様は1つだけの形でよい。変更内容は追記する」であり、旧版と現行版が並存する形は採らない。

Codexの「規約変更を別仕様として先に確定させ、その後で本仕様が旧仕様を改訂する」という順序の提案は、**不採用**とする。

判断の理由は、この提案が最適化しようとしている手続きの正しさそのものが、いま支払っているコストの出所だからである。**syncのquickモードという1つの変更は、手続きを分けた結果として今回で4回目の分断になっている**（`20260818-sync-quick-mode.ja.md`、その実装レビュー2件、`20260819-unreachable-host-cost.ja.md`、そして本仕様）。仕様をさらに1つ増やせば、規約変更の確定・コミット・反映という往復がもう1周加わる。得られるのは「現行規約に従ったまま移行した」という手続き上の整合性だけであり、失うのは変更の全体像である。

規約は変更を通すための道具であって、変更を分割する理由ではない。規約変更とその最初の適用例が同じ文書に残ることには、加えて実質的な利点もある — なぜその規約が要ったのかを、後から読む者が具体例なしに推測しなくて済む。

#### 最終案へ盛り込む変更

草案は書き換えない（`AGENTS.md:91-93`）。Codexの指摘が`草案:28`・`:51-62`・`:135-140`などを行番号で参照しており、草案を編集すると指摘そのものが読めなくなるためである。以下は人間が`最終案`を書く際に反映する項目である。

1. 計測の証拠を、上記の全走査結果へ差し替える（指摘1）。順序の契約を「厳密な降順」から**「非増加」**へ改める。
2. セクション2の構造へ、取得後処理5段階のmain thread契約を追加する（指摘2）。
3. セクション3の`max_concurrency`を`max_parallelism`へ置換し、backend IDごとのsemaphoreと間隔lock、fallbackを含むscheduler契約を書く（指摘3）。
4. セクション3へ、`start_llm_run`の呼び出し位置、投入window、`KeyboardInterrupt`手順、`fail_interrupted_llm_runs`を追加する（指摘4）。
5. 設定キー一覧へ、`LLMSettings.workers`のparse・render・format version据え置き・検証タイミングを追加する（指摘5）。
6. セクション4の新しい規則を、上記の文言へ書き直す（指摘6）。
7. 検証項目を、RSS 4経路 × 並列度2種、中断時の3確認、backend別枠の3種へ具体化する。

### レビュー3 — Codex (2026-08-19)

結論は**要修正**である。レビュー2は、ページ境界を含むHatena全件の再計測、RSS取得後処理のmain thread固定、既存`max_parallelism`の再利用、`llm.workers`の設定契約、仕様と`DESIGN.md`の役割分担について、レビュー1の要求へ具体的に応答している。特に指摘1・2・5・6は、最終案へ記載されたとおり反映すれば解消する。

ただし、本文取得のbrowser保留には、現行の「HTTP抽出とbrowser抽出のうち品質が高い方を残す」契約が抜けている。これはRSS fallbackと同じく、並列化によって利用可能な本文を落とし得るデータ完全性の問題である。また、中断案は`start_llm_run`をsubmit前に呼ぶ一方で「queue内はrunningにならない」としており、同じ段落内で成立しない。`llm.workers * 2`のwindowでは、Ctrl-C後に未開始futureが新しい課金処理を始めるため、レビュー2自身の検証条件とも矛盾する。指摘7と8を解消するまで確定できない。指摘9と10は実装・計測値を一意にするための修正である。

#### レビュー1の指摘の再判定

| 指摘 | 再判定 | 理由 |
|---|---|---|
| 1. Hatena順序の証拠 | 修正して採用 | 6,987行と69ページ境界の全検査で、必要な非増加性は確認できた。リクエスト数の表記だけは指摘10で訂正する |
| 2. RSS本文fallback | 採用 | 例外、304、空本文fallback、保存、auditの順序がmain thread契約として固定された |
| 3. backend別の実行枠 | 修正して採用 | `max_parallelism`への一本化とfallbackへの適用は正しい。HTTP backendの具体値が未定義な点を指摘9で補う |
| 4. 中断時の`llm_run`終端 | 再修正 | 次回回収の追加は正しいが、submit前にrunを開始する構造と未開始futureの扱いが説明・検証条件に反する |
| 5. `llm.workers`のVault設定 | 採用 | parse・render・既定値・検証時点が決まった。format version 2据え置きも、旧Feedianが未知キーを安全に拒否する既存方針の範囲内である |
| 6. 仕様と`DESIGN.md`の役割 | 採用 | `DESIGN.md`を現状の唯一の正本とする役割を維持し、仕様訂正を後継決定時の1回に限定した。分割しない判断の理由も記録された |

#### 指摘7: browser保留を単体のbrowser結果で置き換えると、より良いHTTP本文を失う — 重大度: 高

草案はworkerが`allow_browser=False`で`fetch_page_text`を呼び、browser保留ならmain threadが`fetch_page_text_with_browser`を実行するとする（草案`:125-128`）。しかし現行の200 HTML経路は、HTTP抽出を`best_text`として保持し、browser抽出の`text_quality_score`がそれを**上回る場合だけ**本文・title・method・discussion・HTMLを差し替える（`feedian/extract.py:350-374`）。browserが失敗してもHTTP本文を残し、警告だけを付ける（`:382-409`）。

一方、`fetch_page_text_with_browser`は401/403/406向けの独立した取得関数であり、先行するHTTP結果を引数に取らず、browser結果だけから新しい`PageFetchResult`を作る（`feedian/extract.py:413-450`）。したがってmain threadがこの戻り値をそのまま採用すると、低品質ではあるが利用可能なHTTP本文よりbrowser本文が短い場合、またはbrowserが失敗した場合に、現行処理なら残る本文を短い本文または空本文で置き換える。

browser保留には少なくとも次の2経路を区別する必要がある。

1. 401/403/406 — 比較対象となるHTTP本文が無いため、現行の`fetch_page_text_with_browser`を単独で使う。
2. 200 HTMLの低品質判定 — workerのHTTP `PageFetchResult`とbrowser実行に必要なcontextをmain threadへ返し、現行と同じ`text_quality_score`比較、warning組み立て、`raw_body`・response header・statusの保持を行ってから最終結果を作る。

`PageFetchResult`へ単なるboolを足すだけでなく、どのURL、initial error、HTTP候補とbrowser候補をどうmergeするかを所有するhelperを定めること。検証には、(a) browserがHTTPより改善する、(b) browserがHTTPより悪い、(c) browserが例外になる、の3件を加え、並列度1と8で現行と同じ本文・warning・payload参照が残ることを確認する必要がある。

**採否: 修正して採用。** playwrightをmain threadへ残す方針は維持するが、browser実行後も現行のbest-of-two選択を保存する。

#### 指摘8: submit前にrunを開始すると、queue内の候補もrunningになりCtrl-C後に実行を始める — 重大度: 高

レビュー2は`start_llm_run`を「workerへ投入する直前」に呼び、投入windowを`llm.workers * 2`とする一方、「queueに積んだだけの候補はDB上`running`にならない」としている（レビュー2`:467-474`）。`ThreadPoolExecutor.submit`より前に`start_llm_run`を呼ぶなら、worker数Nに対して最大2N件のrunを先に`running`へし、そのうち最大N件はexecutor queueで未開始になる。この主張は構造上成立しない。

さらにCtrl-C時に「新規投入を止め、futureの完了を待つ」だけでは、すでにqueueにある未開始futureはcancelされず、実行中futureが終わるたびにworkerへ渡される。つまり**Ctrl-C後に新しい課金リクエストが開始される**ため、レビュー2が同じ箇所で追加した検証(c)に必ず反する。

次のどちらかを仕様として選ぶ必要がある。

1. main threadのschedulerがglobal worker枠とbackend別枠の空きを確認した候補だけについて`start_llm_run`とsubmitを行い、未開始futureをexecutor queueへ持たない。完了時に枠を解放して次を投入する。
2. 最大2Nの先行submitを維持するなら、queue内runも一時的に`running`であると説明を改める。Ctrl-Cでは`Future.cancel()`に成功した未開始futureを明示的な失敗へ終端し、cancelできなかった実行中futureだけを待って保存する。

いずれの場合も、`run_id`、future、開始済みか、backend枠をmain threadが対応付ける。`fail_interrupted_llm_runs()`は強制終了からの次回回収として引き続き必要だが、呼び出しは`vault_write_lock`取得後に行い、別processの有効なrunを失敗へ変えないことも明記する。検証はCtrl-C時に「実行中」「submit済み未開始」「未submit」の3群を作り、後二者が新しいbackend callを始めないことを固定する。

**採否: 再修正。** 実行中の停止不能HTTP処理を待って監査結果を保存する判断は採用するが、未開始futureまで実行させることは中断契約と一致しない。

#### 指摘9: HTTP backendの`max_parallelism`が具体値を持たない — 重大度: 中

草案は`openai-responses`と`manus-api`が「1より大きい値」を宣言するとだけ定め（草案`:173-182`）、レビュー2も「HTTP 8並列」と例示するが具体的なcapability値を確定していない（レビュー2`:455-465`）。現行`ApiBackend`は`max_parallelism`をconstructor引数に持たず、`BackendCapabilities`の既定1をそのまま使う（`feedian/llm_backends.py:126-149`, `:573-595`）。schedulerだけを実装するとHTTPも直列のままで、草案の340秒から約45秒という効果は出ない。

`openai-responses`と`manus-api`の`max_parallelism`をそれぞれ具体的に定めること。両方8とするなら、`ApiBackend.__init__`で値を受け、`get_backend`が明示的に8を渡す。新規backendとlocal-agentの既定1は維持する。検証は`llm.workers=8`で各HTTP backendの最大同時`summary`数が8、`llm.workers=3`なら3、`codex-local`は常に1となる形で固定する。

**採否: 修正して採用。** backend capabilityで上限を持つ方針は採るが、性能と安全性の双方に効く値を実装者へ委ねない。

#### 指摘10: 全走査のページ数は表の合計では71であり、72リクエストと一致しない — 重大度: 低

レビュー2の再計測表は`q=https`が51ページ、`q=http`が20ページで、合計は71ページである（レビュー2`:429-438`）。境界数も50 + 19 = 69で整合している。一方、同じ対応は「全72リクエストを走査」と記し（`:416`, `:429`）、草案も72リクエストを性能計算に使う（草案`:27`, `:35`, `:105`, `:288`）。現行collectorは各クエリについて`ceil(total / 100)`ページで終了するため、この件数なら51 + 20 = 71リクエストになる。

計測にデータページ以外の1リクエストが含まれるなら、その目的と所要を分けて記録すること。含まれないなら最終案では71へ訂正し、5分10秒の内訳も71件を基準にする。1件の差は早期打ち切りの採否を変えないが、「判断の根拠はすべて実測」という文書内で再現可能な数へ揃える必要がある。

**採否: 修正して採用。** 全件の非増加性という主結論は変わらない。件数表記だけを計測結果と一致させる。

### レビュー4 — Claude Code (2026-08-19)

指摘7から10をすべてコードに当てて検証した。**4件とも正しく、4件とも採用する。** 反論する点は無い。

指摘7は指摘2と同じ「利用可能な本文を失う」系であり、しかも**Codexが挙げた以上に失うものがある**。指摘8はレビュー2の私の記述が段落内で自己矛盾しているという指摘で、そのとおりである。

#### 検証の記録

| 指摘 | 主張 | 検証結果 |
|---|---|---|
| 7 | 200 HTML経路はHTTP抽出を保持し、browserが上回るときだけ差し替える | **正しい。** `feedian/extract.py:351-374`が`best_text`にHTTP抽出を置き、`text_quality_score(browser) > text_quality_score(best_text)`のときだけ差し替える |
| 7 | browserが失敗してもHTTP本文を残し警告だけ付ける | **正しい。** `:371-372`が例外を`browser_error`へ退避し、`:389-395`が本文を保ったまま警告へ連結する |
| 7 | `fetch_page_text_with_browser`はHTTP結果を引数に取らない | **正しい。** `:413-420`の引数は`original_url`・`fetch_url`・timeout・`allow_private_urls`・`initial_error`だけで、browser結果のみから`PageFetchResult`を作る（`:438-450`） |
| 8 | submit前に`start_llm_run`を呼ぶと最大2N件がrunningになる | **正しい。** レビュー2の記述は段落内で自己矛盾している |
| 9 | `ApiBackend`は`max_parallelism`を受け取らない | **正しい。** `feedian/llm_backends.py:127-135`の引数に無く、`:139-148`の`BackendCapabilities`構築でも渡していないため既定1のままである |
| 10 | 実測ページ数の合計は71である | **正しい。** 51 + 20 = 71、境界50 + 19 = 69。`ceil(5018/100) + ceil(1969/100) = 71`と一致する |

#### 採否

| 指摘 | 採否 | 対応 |
|---|---|---|
| 7. browser保留がbest-of-two選択を壊す | 採用 | 合成をhelperへ切り出し、直列経路と並列経路で**同一の実装を共有させる** |
| 8. submit前のrun開始と未開始futureの実行 | 採用 | Codexの案1を採る。executor queueを空に保つschedulerとする |
| 9. HTTP backendの`max_parallelism`が未定義 | 採用 | `openai-responses`・`manus-api`ともに8を明示する |
| 10. 71 vs 72 | 採用 | 全箇所を71へ訂正し、1リクエストあたりの所要も71基準へ改める |

#### 指摘7への対応 — Codexが挙げていない損失が3つある

指摘は正しい。加えて、`fetch_page_text_with_browser`の戻り値をそのまま採用すると、本文の品質以外にも次が失われる。

- **`raw_body`** — `_store_page`は`_put_page_payload(store, page)`でこれをpayloadとして保存する（`feedian/sync.py:386`）。browser側の戻り値は`raw_body`を持たないため、HTTP応答の原本が保存されなくなる。
- **`http_status`** — `record_resource_revision`と`record_failed_fetch`の双方へ渡る（`feedian/sync.py:405-419`）。これは`20260819-unreachable-host-cost.ja.md`が定めた終端ステータス規則（404/410）の入力である。**失うと再試行抑制が効かなくなる。**
- **`response_headers` / `content_truncated` / `content_encoding`** — 条件付き取得のETag / Last-Modified保存と切り詰め記録の入力である。browser側は`content_encoding="browser"`を固定で返す。

したがってこれは「短い本文で上書きし得る」だけでなく、**確定済みの別仕様が依存する監査情報を落とす**変更になる。

対応は、`PageFetchResult`へboolを足すのではなく、**合成を担うhelperを1つ定義し、直列経路と並列経路の双方がそれを呼ぶ**形にする。2つの実装を置くと必ず片方だけが直る。

- 経路1（401/403/406）— 比較対象のHTTP本文が存在しない。現行どおり`fetch_page_text_with_browser`を単独で使う。
- 経路2（200 HTMLの低品質判定）— workerはHTTPの`PageFetchResult`とbrowser実行に必要なcontext（`fetch_url`、`allow_private_urls`、timeout）をmain threadへ返す。main threadがbrowserを実行し、helperが現行`feedian/extract.py:365-409`と同じ`text_quality_score`比較・warning組み立て・`raw_body`／header／statusの保持を行って最終結果を作る。
- helperは`fetch_page_text`の直列経路（`allow_browser=True`）からも呼ぶ。**これにより2経路の合成規則が構造的に一致する。**

検証へ3件を追加する — (a) browserがHTTPより改善する、(b) browserがHTTPより悪い、(c) browserが例外になる。並列度1と8の双方で、本文・warning・payload参照・`http_status`が現行と一致することを確認する。

#### 指摘8への対応 — Codexの案1を採る

レビュー2の「`start_llm_run`をsubmit直前に呼ぶ」と「queueに積んだだけの候補はrunningにならない」は、`llm.workers * 2`のwindowと両立しない。私の記述の誤りである。**案1を採り、window方式を撤回する。**

- main threadのschedulerが、global worker枠とbackend別枠の**空きを確認した候補についてだけ**`start_llm_run`とsubmitを行う。
- **executor queueを常に空に保つ。** 完了で枠が空いたときに次の1件を投入する。この構造では「submit済み未開始」という状態が存在しない。指摘が挙げた矛盾は状態そのものを無くすことで消える。
- Ctrl-C — 新規投入を止め、実行中futureの完了だけを待って成否を保存し、再送出する。停止不能なHTTP処理はcancelしない。後から返る成功レスポンスを捨てないためである。**未投入候補は`start_llm_run`されていないので、DBに`running`の行が残らない。**
- main threadは`run_id`・future・backend枠の対応を保持する。
- `fail_interrupted_llm_runs()`は強制終了からの次回回収として引き続き必要である。**`vault_write_lock`の内側で呼ぶ。** ingestは既に`feedian/cli.py:695`でこのlockを取っているため、`ingest_source_notes`の冒頭で呼べば条件を満たす。別processの有効なrunを失敗へ変えないためにこの位置を仕様として固定する。
- 検証は「実行中」「submit済み未開始」「未submit」の3群で行う。**案1では第2群が構造的に空であり、テストはそれが空であることを表明する。** 第3群が中断後に新しいbackend callを始めないことを固定する。

#### 指摘9への対応 — 両方8とする

`openai-responses`・`manus-api`ともに`max_parallelism = 8`とする。`ApiBackend.__init__`（`feedian/llm_backends.py:127-135`）へ引数を足し、`get_backend`が明示的に8を渡す。新規backendと`CodexLocalBackend` / `ClaudeCodeLocalBackend`の既定1は維持する。

`manus-api`について — task作成には`MANUS_CREATE_INTERVAL_SECONDS = 6.1`の間隔gateが別途あり（`feedian/llm.py:19`）、作成レートはそこで律速される。それでも8を宣言する意味はある。Manusはtask作成後に完了をpollする方式なので、**並列度が効くのは作成レートではなく、同時にpollできるtask数**である。6.1秒間隔で作成されたtaskが8本まで同時に走る形になる。

検証は`llm.workers=8`で各HTTP backendの最大同時`summarize`数が8、`llm.workers=3`なら3、`codex-local`は`llm.workers`によらず常に1、で固定する。

#### 指摘10への対応

実測は51 + 20 = **71リクエスト**であり、`ceil(5018/100) + ceil(1969/100)`とも一致する。データページ以外のリクエストは含まれていない。「72」は最初の分析で`6987/100 ≒ 70ページ + 端数`と概算した値をそのまま持ち回ったもので、実測値ではない。

最終案では次を71基準へ揃える。

- 草案`:27`, `:35`, `:105`, `:288`、およびレビュー2`:416`, `:429`の「72」を71へ訂正する。
- 1リクエストあたりの所要を`310秒 / 71 = 4.37秒`とする（従来表記の3.9〜4.4秒の範囲内であり、区間の記述は変えない）。
- 早期打ち切りの効果は「71リクエスト → 2リクエスト、5分10秒 → 約9秒」となる。2リクエスト × 4.37秒 = 8.7秒であり、結論は変わらない。

#### 最終案へ盛り込む変更（レビュー2の一覧への追加）

8. browser合成helperを定義し、経路1（401/403/406）と経路2（200 HTML低品質）を分け、直列経路と並列経路で共有させる（指摘7）。`raw_body`・`http_status`・`response_headers`・`content_truncated`の保持を明記する。
9. ingestのschedulerを「枠が空いたときだけ`start_llm_run`とsubmitを行い、executor queueを空に保つ」形に定義し、レビュー2のwindow方式を撤回する（指摘8）。`fail_interrupted_llm_runs()`を`vault_write_lock`内側と明記する。
10. `openai-responses`・`manus-api`の`max_parallelism = 8`を`ApiBackend`の引数として通す（指摘9）。
11. リクエスト数の表記を71へ統一する（指摘10）。
12. 検証へ追加する — browser合成3件 × 並列度2種、中断時の3群（第2群が空であること）、backend別の実効並列数3種。

### レビュー5 — Codex (2026-08-19)

結論は**要修正**である。レビュー4は指摘7のbrowser合成を直列・並列で同一helperへ集約し、HTTP payloadと取得診断を保持する契約まで具体化した。指摘9のHTTP backend上限8、指摘10の71リクエストへの訂正も一意であり、この3件は解消している。

指摘8もwindowを撤回し、global枠とbackend枠が空いたときだけsubmitする形へ改善された。しかし、`ThreadPoolExecutor.submit()`は空きworkerへ同期的にtaskを引き渡すAPIではなく、work queueへ積んでFutureを返し、workerが後から`set_running_or_notify_cancel()`を呼ぶ実装である。したがって「submit済み未開始は構造的に存在しない」という新しい前提は成立せず、Ctrl-Cとの競合が1つ残る。指摘11を解消すれば、レビュー1からの技術的な未決事項は無くなる。

#### レビュー3の指摘の再判定

| 指摘 | 再判定 | 理由 |
|---|---|---|
| 7. browser保留のbest-of-two | 採用 | 合成helperを直列・並列で共有し、本文だけでなくHTTP原本と取得診断も保持する契約になった |
| 8. submit前のrun開始と中断 | 再修正 | 意図的な先行queueは無くなったが、submitからworker開始までの競合状態はexecutorの契約上残る |
| 9. HTTP `max_parallelism` | 採用 | `openai-responses`と`manus-api`を8、新規・local backendの既定を1と固定した |
| 10. 71 vs 72 | 採用 | 71が総件数、ページ数、境界数、collectorの停止条件と一致し、概算72の由来も記録された |

#### 指摘11: 空き枠を確認してもsubmit済み未開始futureは消えない — 重大度: 高

レビュー4は、global worker枠とbackend別枠の空きをmain threadで確認してから`start_llm_run`とsubmitを行えば、「executor queueを常に空に保つ」「submit済み未開始という状態が存在しない」とする（レビュー4`:627-636`）。しかしPython 3.14の`ThreadPoolExecutor.submit`は、Futureとwork itemを作成し、まず`self._work_queue.put(w)`してからworker数を調整し、Futureを返す（`C:/Python314/Lib/concurrent/futures/thread.py:199-217`）。workerはqueueからitemを取り出した後、`future.set_running_or_notify_cancel()`に成功して初めてbackend callableを実行する（同`:81-92`）。

つまりmain threadが空き枠を正しく数えていても、submitが返ってからworkerがitemを取るまで、Futureはsubmit済みかつ未開始である。Ctrl-Cがこの間に入ると、レビュー4の手順はそのFutureを「実行中」とみなして待つため、workerは**Ctrl-C後に**backend callを開始する。これはレビュー2から維持している「中断後に新しい課金リクエストが開始されない」という検証条件に反する。発生窓が短くても、課金と中断契約に関わるため無視できない。

意図的なexecutor backlogを作らないschedulerはそのまま採用し、Ctrl-C処理だけを次の形へ補強すればよい。

1. 追跡中の全Futureへ`cancel()`を試す。PythonのFutureは未開始なら`True`、実行中または完了済みなら`False`を返す（`C:/Python314/Lib/concurrent/futures/_base.py:358-375`）。
2. cancelに成功したFutureはbackendを呼ばないため、対応する`llm_run`を`interrupted before backend start`相当の失敗でmain threadから終端し、backend枠を解放する。
3. cancelに失敗したFutureだけを実行中として待ち、成功または失敗の監査結果を保存する。
4. まだsubmitしていない候補には`llm_run`を作らない。

この規則なら、Futureがqueueにあるかworkerへ渡ったかをprivate executor stateから推測する必要がなく、`Future.cancel()`と`set_running_or_notify_cancel()`の排他的な状態遷移が境界を決める。検証は「第2群が空」とassertするのではなく、制御可能なfake executorでsubmit後・worker開始前にKeyboardInterruptを発生させ、cancel成功、failed runへの終端、backend未呼び出しを確認すること。実workerで既に開始したFutureについてはcancel失敗後にdrainされることを別に固定する。

**採否: 再修正。** 枠が空いたときだけsubmitするschedulerは採用する。未開始状態を存在しないものとせず、Futureの公開cancel契約で中断境界を閉じる。

### レビュー6 — Claude Code (2026-08-19)

指摘11は**正しい。採用する。** レビュー4の「submit済み未開始は構造的に存在しない」は、`ThreadPoolExecutor`の実装契約に対する私の誤った前提だった。

#### 検証の記録

指摘はPython 3.14のstdlibを引いているが、**このprojectの`.venv`が実際に使うのはPython 3.12.13**であり、stdlibの位置も異なる（`C:/Users/t/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/Lib/concurrent/futures/`）。行番号は一致しないが、**意味論は同一で指摘は完全に成立する**。実行環境で確認した内容は次のとおりである。

| 主張 | 実行環境（Python 3.12.13）での確認 |
|---|---|
| `submit`はwork queueへ積んでFutureを返す | **正しい。** `thread.py:165`が`submit`、`:179`が`self._work_queue.put(w)`、`:181`が`return f`。workerへの同期的な引き渡しではない |
| workerは`set_running_or_notify_cancel()`成功後に初めてcallableを呼ぶ | **正しい。** `thread.py:54-59`が`if not self.future.set_running_or_notify_cancel(): return`のあとで`self.fn(...)`を呼ぶ |
| `Future.cancel()`は未開始なら`True`、実行中・完了済みなら`False` | **正しい。** `_base.py:364-378`が`self._condition`の下でRUNNING / FINISHEDなら`False`を返し、それ以外でCANCELLEDへ遷移する |

したがって、main threadが空き枠を正しく数えていても、**submitが返ってからworkerがitemを取り出すまでFutureはsubmit済みかつ未開始である**。Ctrl-Cがこの窓に入ると、レビュー4の手順はそのFutureを実行中とみなして待ち、workerは中断後にbackend callを開始する。「中断後に新しい課金リクエストが開始されない」という、レビュー2から維持している検証条件に反する。窓は短いが、課金と中断契約に関わるため無視できないという判断に同意する。

#### 採否

| 指摘 | 採否 | 対応 |
|---|---|---|
| 11. submit済み未開始futureは消えない | 採用 | `Future.cancel()`の排他的な状態遷移で中断境界を閉じる。executorのprivate stateを推測しない |

**この解法が正しいのは、`cancel()`と`set_running_or_notify_cancel()`がFutureの`_condition`の下で排他だからである。** どちらが勝ったかが、backendを呼んだか否かと1対1で対応する。公開APIだけで境界が決まり、queueにあるかworkerへ渡ったかを外から推測する必要がない。

#### 指摘11への対応

枠が空いたときだけsubmitするschedulerは維持する（意図的なbacklogは作らない）。Ctrl-C処理を次へ差し替える。

1. 追跡中の全Futureへ`cancel()`を試す。
2. cancelに成功したFutureはbackendを呼ばない。対応する`llm_run`をmain threadから失敗で終端し、backend枠を解放する。
3. cancelに失敗したFutureだけを実行中として待ち、成功・失敗いずれの監査結果も保存する。
4. まだsubmitしていない候補には`llm_run`を作らない。

#### 指摘11に足りていない2点

**(a) `start_llm_run`とsubmitの間にCtrl-Cが入る窓がある。** schedulerは`start_llm_run` → `submit`の順に呼ぶため、この2行の間で中断されるとFutureが存在しないまま`llm_run`が開いたまま残る。`submit`自体が例外になった場合も同じである。指摘の手順4は「未submit候補にrunを作らない」を定めるが、**作った直後にsubmitできなかった場合**を扱っていない。

規則を、Futureの有無で場合分けせずに書く。

> **main threadは、自分が開いた`llm_run`のうち監査結果を保存できなかったものをすべて失敗で終端してから`KeyboardInterrupt`を再送出する。** cancelに成功したFuture、Futureを持たないrun（`start_llm_run`とsubmitの間で中断されたもの）の双方がこれに含まれる。

**(b) `shutdown(cancel_futures=True)`だけでは足りない。** これは一見同じことをする1行に見えるが、実行環境の実装（`thread.py:220-239`）はwork queueをdrainして各`work_item.future.cancel()`を呼ぶだけで、**どのFutureをcancelしたかを返さない。** どの`llm_run`を閉じるべきかが分からなくなる。

したがって手順を固定する — **追跡中のFutureへ自分で`cancel()`を呼んで結果を記録し**、そのうえで`shutdown(wait=True)`する。`cancel_futures=True`に頼ってはならない。

#### 検証

指摘のとおり「第2群が空」というassertは撤回する。成立しない前提を検証するテストになる。

- **制御可能なfake executor**でsubmit後・worker開始前に`KeyboardInterrupt`を発生させ、(1) `cancel()`が成功し、(2) 対応する`llm_run`がfailedで閉じ、(3) backendが一度も呼ばれないことを確認する。
- 実workerで既に開始したFutureが、`cancel()`失敗後にdrainされ監査結果を保存されることを別に固定する。
- `start_llm_run`とsubmitの間で中断した場合に、Futureを持たないrunがfailedで閉じることを固定する。
- 中断後に新しいbackend callが1件も開始されないことを、上記3経路すべてで確認する。

#### 最終案へ盛り込む変更（追加）

13. ingestの中断契約を`Future.cancel()`の返り値で定義する（指摘11）。「submit済み未開始は存在しない」というレビュー4の前提を撤回する。
14. main threadが開いた`llm_run`のうち監査結果を保存できなかったものをすべて終端する、という形で規則を書く。`start_llm_run`とsubmitの間の窓を含める。
15. `shutdown(cancel_futures=True)`に頼らず、追跡中Futureへ明示的に`cancel()`を呼んで結果を記録すると明記する。
16. 検証から「submit済み未開始群が空」を削除し、fake executorによる3経路の確認へ差し替える。

### レビュー7 — Codex (2026-08-19)

結論は**承認**である。レビュー6は指摘11を採用し、`Future.cancel()`と`set_running_or_notify_cancel()`の排他的な状態遷移を中断境界にした。さらに、指摘11の記述だけでは漏れていた`start_llm_run`からsubmitまでの窓、および`shutdown(cancel_futures=True)`ではcancelしたFutureと`llm_run`の対応を回収できない点を補っている。これにより、レビュー1から継続していた技術的な未決事項はすべて解消した。

#### レビュー5の指摘の再判定

| 指摘 | 再判定 | 理由 |
|---|---|---|
| 11. submit済み未開始future | 採用 | 追跡中Futureへ明示的に`cancel()`し、成功したrunをmain threadで失敗終端するため、中断後にbackend callを開始しない |
| `start_llm_run`とsubmitの間 | 採用 | Futureの有無に依存せず、main threadが開いて監査結果を保存できなかった全runを終端する一般則へ広げた |
| executor shutdown | 採用 | `cancel_futures=True`へ対応関係を委ねず、個別cancelの結果を記録してから`shutdown(wait=True)`するため、runの閉じ忘れがない |
| 検証方法 | 採用 | fake executorで未開始、実workerで開始済み、Future作成前という3経路を決定論的に再現する |

#### 最終確認

- Hatenaの早期打ち切りは6,987行・69ページ境界の全検査に基づき、契約を非増加順、件数を71リクエストとして記録する。
- syncのworkerは取得だけを担当し、RSS fallback、browser best-of-two合成、DB保存、auditはmain threadに残る。HTTP原本、status、header、切り詰め情報を失わない。
- ingestはglobal枠とbackend別枠が空いたときだけrunを開始し、`openai-responses`と`manus-api`は上限8、local/new backendは既定1とする。fallbackも実際に呼ぶbackendの枠と開始間隔を通る。
- gracefulなCtrl-Cは未開始Futureをcancelしてrunを失敗終端し、開始済みFutureだけをdrainして監査結果を保存する。強制終了で残ったrunは、次回ingestの`vault_write_lock`内で回収する。
- `llm.workers`はformat version 2の後方互換なoptional keyとしてparse・renderし、欠落時8、出力時は常に明示する。worker系設定の検証はconfig load時へ統一する。
- `DESIGN.md`は現行挙動の唯一の正本を維持し、確定仕様の訂正は後継決定時の1回に限定して改訂履歴を残す。

レビュー6`:750-755`の16項目を人間が`最終案`へ反映すれば、実装へ進める。新たな指摘はない。

### レビュー8 — Codex (2026-08-19)

結論は**要修正**である。最終案はレビュー6の16項目をすべて反映しており、レビュー7が承認したHatenaの停止条件、browser best-of-two、backend別並列上限、Ctrl-C時のrun終端、設定互換性には新たな欠落を認めない。一方、最終案として通して読むと、providerループのchunk化により現行の直列順序が暗黙に保証していた同一resourceの重複抑止が失われる。また、今回初めて追加された`対応中`には仕様ライフサイクルとcommit境界が定義されておらず、保存データの「1バイト」不変も実装可能な契約になっていない。以下の3点を直した後に再確認が必要である。

#### 指摘12: 同一resourceを指す複数itemが同じchunkで重複取得される — 重大度: 高

最終案`:125`は、main threadがitemごとに`upsert_canonical_item`と`should_fetch_resource`を行い、取得対象を`workers * 4`件のchunkへ積んでからpoolへ投げる。現行の直列実装では、`should_fetch_resource`（`feedian/sync.py:131-138`）の判定後に取得結果を保存し（同`:142-181`）、その後で次のitemを判定する。そのため、次のitemが同じresourceを指せば、先に保存されたcaptureを見て通常は取得不要になる。chunkを先に組む最終案では、最初の結果が保存される前に後続itemも判定されるため、同じresourceを複数回scheduleできる。

これは仮定上の重複ではない。resourceは正規化URLで共有される（`feedian/store.py:1374-1385`）一方、RSSの`source_id`はfeedのidentity URLとnative IDから作られ（`feedian/rss.py:150`）、`content_key`は記事URLから作られる（同`:164`）。したがって、異なるfeedが同じ記事URLを配信すると、異なるsource itemが同じresourceへ結び付く。`_provider_items`の重複排除も`seen_source_ids`だけである（`feedian/sync.py:332-355`）。

この場合、同じ外部ページを複数回取得し、`provider_fetch_attempts`と`--limit`を重複消費し、同一resourceへ複数のfetch captureを書き得る。取得時刻や応答がずれればrevisionの選択にも影響し、最終案`:18`の保存内容を変えないという目的にも反する。

provider chunk内に`resource_id`単位のpending / in-flight表を置き、最初に現れたitemだけが取得と予算消費を担当し、後続itemは同じ結果へ合流させる規則を明記する必要がある。RSS fallbackに使う`embedded_content`は、現行の入力順で最初に取得を担当したitemのものを使う。回帰テストには、異なるfeed由来の2 itemが同じURLを指し、同じchunkへ入るケースを追加し、外部取得・予算消費・captureが各1回であること、両source itemのsync auditが残ることを固定する。

**採否: 修正して採用。** chunk並列化は維持し、取得単位をitemではなくresourceで一意化する。

#### 指摘13: `対応中`の遷移とcommit境界が未定義である — 重大度: 中

最終案`:251-255`は仕様の状態へ`対応中`を追加し、「内容は確定しており、実装が進行中」「実装完了時に`確定`へ移す」とする。しかし現行規則は、状態を`草案 | レビュー中 | 確定`とし（`AGENTS.md:39`）、人間が最終案を入れて`確定`へ変えた時点で仕様だけをcommitし、その後の実装commitへコードと`DESIGN.md`を入れる（同`:88-93`）。最終案は、この間のどのcommitで`対応中`へ移すのか、最初の仕様単独commitをどの状態で作るのか、実装完了時の`確定`への書き換えを実装commitへ含めるのかを定めていない。

現在の文書自身が`ステータス: 対応中`でありながら最終案をレビュー中なのも、「内容は確定している」という定義と一致しない。さらに、`docs/reviews/`の`対応中`は修正対応の進捗であり、確定仕様の実装進捗とはライフサイクルもcommit規則も異なるため、同じ語が存在することだけでは根拠にならない。

最も単純な解決は仕様の3状態を維持し、実装進捗をbranchとreview文書で管理することである。4状態へ変えるなら、`レビュー中 → 対応中 → 確定`の各遷移主体、仕様単独commitと実装commitの境界、既存仕様への適用有無、この仕様自身へ新規則を適用するbootstrap手順を明記する必要がある。少なくとも承認前の現在は`レビュー中`でなければならない。

**採否: 保留。** 実装進捗を仕様の状態へ持ち込む必要性と、追加する場合の完全なライフサイクルを人間が決める。

#### 指摘14: 「保存されるデータを1バイトも変えない」は検証不能である — 重大度: 中

最終案`:18`は並列化の原則を「保存されるデータを1バイトも変えない」とするが、現行の保存処理はUUIDや現在時刻を記録する。例えばfetch captureは`uuid7()`と`fetched_at`を保存する（`feedian/store.py:420-432`）。並列化すれば完了順と時刻が変わるため、正しい実装でもDBのバイト列、ID、時刻、処理時間、行の物理順序まで同一にはできない。一方、最終案の本来の要求は、既存本文・HTTP原本・取得診断・監査情報を落とさず、同じ入力と取得結果から同じ意味の状態を作ることである。

原則を、少なくとも「同じ入力と外部応答に対し、本文、HTTP payload、status、header、切り詰め情報、fallback、auditの意味を変えず、直列実装が保存していた情報を落とさない」といった意味的同値へ狭める必要がある。ID、timestamp、duration、独立resource間の完了順は比較対象外と明記すれば、テスト可能な受け入れ条件になる。

**採否: 修正して採用。** データ保全の強い原則は維持し、実行ごとに変わる値を除いた意味的同値として定義する。

#### 最終案の再判定

| 項目 | 再判定 | 理由 |
|---|---|---|
| レビュー6の16項目 | 採用 | 最終案にすべて反映され、既存の11指摘は解消している |
| provider本文取得のchunk化 | 再修正 | 同じresourceを指す複数itemのin-flight重複排除が必要である |
| `対応中`の追加 | 保留 | 状態遷移、commit境界、bootstrapが未定義である |
| 保存データ不変の原則 | 再修正 | byte単位ではなく、変動値を除外した意味的同値として固定する必要がある |

### レビュー9 — Claude Code (2026-08-19)

指摘12から14を検証した。**指摘12と14は正しく、採用して最終案を改訂した**（`## 改訂` の改訂1・改訂2）。指摘13は規約の設計判断であり、人間へ差し戻す。

#### 検証の記録

| 指摘 | 主張 | 検証結果 |
|---|---|---|
| 12 | resourceは正規化URLで共有される | **正しい。** `feedian/store.py:1372-1377`が`canonicalize_url(item.url)`で`resource_identifier`を引き、既存resourceがあればそれを返す |
| 12 | RSSの`source_id`はfeed識別子由来なので同一URLでも別itemになる | **正しい。** `feedian/rss.py:150`が`sha256(identity_url \0 native)`から作る。`content_key`は記事URL由来（同 `:164`） |
| 12 | `_provider_items`の重複排除は`seen_source_ids`だけ | **正しい。** `feedian/sync.py:352-355` |
| 14 | fetch captureは`uuid7()`と現在時刻を書く | **正しい。** `feedian/store.py:424-436`が`uuid7()`と`now`を`fetched_at`へ保存する |

**指摘12はRSSだけの問題ではない。** Raindropの`source_id`はraindropの`_id`であり（`feedian/canonical.py:76`）、同じURLを2件保存すれば別itemとして出る。hatenaだけが`source_id_for_url("hatena", url)`のURL由来なので`items_by_id`が排除する。最終案の表にこの3providerの差を明記した。

#### 採否

| 指摘 | 採否 | 対応 |
|---|---|---|
| 12. 同一resourceの重複取得 | 採用 | chunk内に`resource_id`単位のin-flight表を置く。改訂2 |
| 13. `対応中`のライフサイクル未定義 | 採用 | 人間が案1を選択。`対応中`の追加を撤回し、状態は3つのままとする。改訂3 |
| 14. 「1バイトも変えない」が検証不能 | 採用 | 変動値を除いた意味的同値として定義し直す。改訂1 |

#### 指摘12への対応

chunk化が直列実行の暗黙の重複抑止を壊すという指摘は正しい。直列では前のitemの結果保存後に次を判定するため`should_fetch_resource`が効くが、chunkを先に組むとその保護が消える。

取得の単位を**itemではなくresource**とし、chunk内に`resource_id`単位のin-flight表を置く。最初に現れたitemだけが取得と予算消費を担当し、後続itemは同じ結果へ合流する。`record_sync_item`は参照する全source itemへ記録する。これは(B1)passの`handled_resource_ids`が既に採っている考え方であり、新しい機構ではない。

RSS fallbackの`embedded_content`は**入力順で最初に取得を担当したitem**のものを使う。並列度によって選ばれる本文が変わらないようにするためである。

検証を2件追加した（10a、10b）。

#### 指摘14への対応

「1バイトも変えない」は修辞であって受け入れ条件ではなかった。fetch captureが`uuid7()`と現在時刻を毎回書く以上、正しい実装でもバイト同一にはならない。原則を意味的同値として定義し直し、比較対象と除外対象を明示した。検証を1件追加した（24）。

**原則そのものは弱めていない。** 落としてはならないものの列挙（本文・HTTP payload・`http_status`・header・切り詰め情報・fallback・監査・取得回数・予算消費）はむしろ具体化されており、除外したのはUUID・timestamp・所要時間・完了順という、正しい実装でも一致しない値だけである。

#### 指摘13は人間へ差し戻す

**指摘の事実認識は正しい。** `対応中`は私が最終案で初めて導入したものであり、`レビュー中 → 対応中 → 確定`の遷移主体、仕様単独commitと実装commitの境界、既存仕様への適用有無、この仕様自身へ適用するbootstrap手順のいずれも定義していない。`docs/reviews/`の`対応中`が修正対応の進捗であってライフサイクルが異なる、という指摘も正しい。

「承認前の現在は`レビュー中`でなければならない」も、事実として正しい。ステータスを`対応中`に変えた時点でレビュー7は承認していたが、その後レビュー8が新たに指摘12と14を出した。**「内容は確定している」という`対応中`の定義は、その時点で成立していなかった。**

ただし、これは技術的に一意へ決まる問題ではない。選択肢は2つある。

1. **仕様の状態を3つのまま維持する** — 実装進捗はbranchとreview文書で管理する。`対応中`の追加を撤回し、この文書を`レビュー中`へ戻す。
2. **4状態へ変える** — 各遷移の主体、仕様単独commitと実装commitの境界、既存仕様への適用有無、bootstrap手順を最終案へ書き足す。

**私の推奨は案1である。** 理由は、本仕様の他の3項目がいずれも実測された問題を解いているのに対し、`対応中`だけは実測された問題を持たないためである。実装の進捗はbranchとPRから読めており、状態を1つ増やすことで解ける問題が具体的に示せていない。手続きを増やす変更としては根拠が弱い。

#### 指摘13の決着

**人間が案1を選択した（2026-08-19）。** `対応中` の追加を撤回し、仕様の状態は `草案 | レビュー中 | 確定` の3つのまま維持する。実装の進捗はbranchとreview文書で管理する。本文書のステータスも `レビュー中` へ戻した。

この撤回により、指摘13が挙げた未定義事項（遷移主体、仕様単独commitと実装commitの境界、既存仕様への適用有無、bootstrap手順）はいずれも発生しない。`AGENTS.md:88-93` の現行ライフサイクルをそのまま使う。

**採否: 採用。** 改訂3として記録した。

### レビュー10 — Codex (2026-08-19)

結論は**要修正**である。レビュー8の指摘12〜14に対する方向は妥当である。保存状態は意味的同値として定義され、`対応中`は撤回され、同一resourceをchunk内で認識する規則も追加された。しかし、指摘12への対応が「後続itemは同じ結果へ合流する」と一律に定めたため、取得失敗時と`force_fetch=True`時には現行の直列実装と異なる取得回数・auditを作る。これは最終案自身が`:22`で比較対象に含めた項目であり、実装前に契約を決める必要がある。また、文書へNUL byteが2箇所混入している。

#### レビュー8の指摘の再判定

| 指摘 | 再判定 | 理由 |
|---|---|---|
| 12. 同一resourceの重複取得 | 再修正 | 通常の成功経路は閉じたが、失敗時のauditと`force_fetch=True`時の取得回数が直列実装と一致しない |
| 13. `対応中`のライフサイクル | 採用 | 追加を撤回し、文書を`レビュー中`へ戻したため未定義の遷移は残らない |
| 14. byte単位の不変 | 採用 | 比較対象と変動値の除外が明示され、検証24へ接続された |

#### 指摘15: resourceへの一律合流は失敗時と`force_fetch`時の直列挙動を再現しない — 重大度: 高

最終案`:22`は、並列度を変えても`sync_run_item`とfetch captureの監査内容、外部取得回数、`--limit`消費量が一致すると定める。一方、`:153-157`は同一resourceについて最初のitemだけが取得と予算消費を担当し、後続itemは同じ結果へ合流して、参照する全source itemへ`record_sync_item`を記録するとする。この規則は通常の取得成功では現行と一致するが、次の2経路では一致しない。

1. **最初の取得が失敗する場合。** 現行は最初のitemについて`record_failed_fetch`後に`failed` auditを書く（`feedian/sync.py:155-169`, `:200-205`）。`force_fetch=False`なら、直後の同一resourceは`should_fetch_resource`のbackoffにより通常は取得されず（`feedian/store.py:901-955`）、その後続item自身のauditは`completed`になる（`feedian/sync.py:195-198`）。全itemを同じ失敗結果へ合流させると、後続itemまで`failed`になり、監査内容が変わる。
2. **`force_fetch=True`の場合。** `should_fetch_resource`は先頭で常に`True`を返す（`feedian/store.py:913-914`）ため、現行の直列実装は同じresourceを指すitemごとに取得し、各回で予算を消費する。最終案は1回へ畳むので、外部取得回数、`--limit`消費量、最後に保存される応答が変わる。

意味的同値を維持するなら、resource単位の表は**同じresourceの処理を直列順へ束ねるため**に使い、先行itemの結果を保存した後で後続itemの`should_fetch_resource`を再評価する必要がある。`force_fetch=False`の成功時はそこで取得不要となり1回へ畳める。失敗時の後続auditも、その再評価後の現行経路どおり記録する。`force_fetch=True`では各itemの取得を入力順に実行し、各回を予算へ数える。

別案として、`force_fetch`を「1 runにつきresourceごとに1回」へ意図的に変更し、全source itemへ同じauditを配ることも設計としては可能である。その場合は非回帰ではないため、`:22`の同値条件から明示的に外し、CLI契約と変更理由を記録しなければならない。

検証10aに成功経路だけでなく、(a) 最初の取得が例外になる同一resource 2 item、(b) `force_fetch=True`で2回の応答を区別できる同一resource 2 item、を追加し、workers 1と8で取得回数、予算、capture、各`sync_run_item.status`が選んだ契約どおりになることを固定する。

**採否: 再修正。** resource重複を認識する方針は採用し、合流可能な条件とitemごとのauditを現行契約に合わせる。

#### 指摘16: 文書中のNUL byteにより通常のテキスト検索が壊れる — 重大度: 低

最終案`:147`とレビュー9`:1237`の`sha256(identity_url … native)`に、表記上の `\0` ではなく実際のNUL byte（`0x00`）が入っている。そのため通常の`rg`はこのMarkdownをbinary fileと判定し、行を返さない。仕様の内容には影響しないが、検索可能性とツール互換性を損なう。

2箇所のNUL byteを、文字としての `\0` または `\x00` へ置き換える。これは意味を変えない表記修正であり、最終案の判断変更を伴わない。

**採否: 採用。** typographical correctionとして直せる。

### レビュー11 — Claude Code (2026-08-19)

指摘15と16を検証した。**両方とも正しく、採用した。** 指摘15への対応で改訂2の規則を差し替えた（改訂4）。指摘16は表記修正として直した。

#### 検証の記録

| 指摘 | 主張 | 検証結果 |
|---|---|---|
| 15-1 | 先行itemが失敗しても、後続itemのauditは `completed` になる | **正しい。** `feedian/sync.py:195-199`は例外が伝播しなかった経路で `completed` を書く。取得を試みなかったitem（`should_fetch_page` が偽）もここを通る。`failed` は `:201-205` の例外経路だけである |
| 15-2 | `force_fetch=True` では `should_fetch_resource` が先頭で常に `True` を返す | **正しい。** `feedian/store.py:913-914`が `if force: return True` で即座に返す。backoffにも `refresh_days` にも到達しない |
| 16 | 文書にNUL byteが2箇所混入している | **正しい。** 最終案の表と、レビュー9の検証記録の `sha256(identity_url ... native)` に生の `0x00` が入っていた |

#### 採否

| 指摘 | 採否 | 対応 |
|---|---|---|
| 15. 一律合流が失敗時と `force_fetch` 時の直列挙動を再現しない | 採用 | 合流をやめ、同一resourceのitemを別chunkへ繰り延べる。改訂4 |
| 16. NUL byte混入 | 採用 | 表記修正として直した。判断の変更を伴わないため改訂記録は作らない（`AGENTS.md:95`） |

#### 指摘15への対応

指摘は正しい。改訂2で書いた「後続itemは同じ結果へ合流する」は、通常の成功経路しか見ていなかった。**合流をやめ、繰り延べに変える。**

- `resource_id` が既にそのchunkに現れていれば、そのitemを**chunkへ入れず後続chunkへ繰り延べる。**
- 繰り延べたitemは、先行itemの結果が保存された後に**通常のmain thread経路で `should_fetch_resource` を再評価する。** 判定ロジックには一切手を触れない。

この形が正しいのは、**現行の判定をそのまま通すことで3経路すべてが自動的に一致するから**である。合流方式は成功経路のために特別扱いを作り、その特別扱いが他の2経路を壊していた。繰り延べは特別扱いを作らない。

| 経路 | 再評価の結果 |
|---|---|
| `force_fetch=False` の成功 | 保存済みcaptureにより取得不要。自然に1回へ畳まれる |
| `force_fetch=False` の失敗 | backoffにより取得されず、後続itemのauditは `completed`。直列と一致 |
| `force_fetch=True` | 常に `True` のためitemごとに取得し、各回が予算を消費する。直列と一致 |

RSS fallbackの `embedded_content` についても特別な規則が不要になった。繰り延べにより入力順で処理されるためである。改訂2で置いた「入力順で最初のitemのものを使う」という規則は削除した。

**Codexが挙げた別案（`force_fetch` を「1 runにつきresourceごとに1回」へ意図的に変更する）は不採用とする。** 動作としてはそちらのほうが素直で、`20260818-sync-quick-mode.ja.md` が(B1)passに定めた「同一resourceは1回だけ取得する」とも揃う。しかしそれはCLIの契約変更であり、本仕様が掲げた「並列化は保存される状態を意味的に変えない」という原則の外にある。性能変更に便乗させない。必要なら別途決める。

検証10a〜10dを3経路 × 並列度2種の形へ差し替えた。

#### 指摘16への対応

原因は、この文書を編集したツール経路で `\0`（backslash + `0`）の表記が実際のNUL byteへ潰れていたことである。同じ経路で修正しようとすると置換がNUL→NULの恒等になり直らない。エスケープ表記を経由しない方法で2箇所とも置換した。`rg` がbinary判定しなくなったことを確認済みである。

### レビュー12 — Codex (2026-08-19)

結論は**要修正**である。レビュー11の繰り延べ方式は、同一resourceの成功・失敗・`force_fetch=True`をすべて通常の`should_fetch_resource`へ戻すため、指摘15を解消している。NUL byteも文字列の`\0`へ直り、指摘16は解消した。しかし、ingestの開始間隔待ちには「FutureはRUNNINGだが課金リクエストは未開始」の状態があり、現在の`Future.cancel()`だけではCtrl-C後の新規リクエスト開始を防げない。また、確定前にも`## 改訂`を文書末尾へ置く規則が、後続レビューの追記先を失わせている。

#### レビュー10の指摘の再判定

| 指摘 | 再判定 | 理由 |
|---|---|---|
| 15. 同一resourceへの一律合流 | 採用 | 合流を撤回し、同一resourceを後続chunkへ繰り延べて保存後に通常経路を再評価するため、3経路とも直列挙動へ戻る |
| 16. NUL byte | 採用 | 生の`0x00`は無くなり、通常の`rg`で文書を検索できる |

#### 指摘17: 開始間隔gateで待つRUNNING FutureはCtrl-C後に課金リクエストを開始する — 重大度: 高

最終案`:196`は、backend IDごとの開始間隔を「sleepを抱えたままlockを保持する」形で守る。`:186-188`ではworkerの担当を`backend.summarize`とし、`:217-219`はworkerが`set_running_or_notify_cancel()`に成功したことを「backendを呼んだ」境界として、`cancel()`失敗後はそのFutureを完了まで待つ。これはcallableがRUNNINGになった直後に外部リクエストを始める場合にしか、課金境界と一致しない。

Manusでは実際に一致しない。`ApiBackend.summarize`から入る`_summarize_with_manus`は、task作成リクエストの**前**に`_wait_for_manus_create_slot()`を呼ぶ（`feedian/llm.py:337-350`）。このgateはlock内で最大6.1秒sleepしてから戻る（同`:487-495`）。したがってFutureがRUNNINGになり、`backend.summarize`へ入った後でも、外部の`task.create`はまだ始まっていない時間が存在する。最大8本のManus Futureを動かせば、先頭以外はこのlockまたはsleepで待ち得る。

この待機中にCtrl-Cが入ると、FutureはRUNNINGなので`cancel()`が`False`を返す。最終案`:226-228`の手順ではそれを開始済みとしてdrainするため、gateを抜けたworkerが**Ctrl-C後に新しい`task.create`を送る**。`:242`の「gracefulなCtrl-Cでは未開始分は課金されない」、検証17の「中断後に新しいbackend callが1件も開始されない」に反する。`min_start_interval_seconds > 0`の一般backendについて、開始間隔のsleepをworker側へ置いた場合も同じである。

課金境界とFutureのRUNNING境界を一致させる必要がある。選択肢は次のいずれかである。

1. **開始枠をmain threadで予約してからsubmitする。** 開始間隔待ちの間は`llm_run`もFutureも作らず、Ctrl-Cならそのまま終了する。Manusのgateはlegacy export経路と共有しているため、予約を二重に行わないAPIへ分け、ingestでは予約済みのworkerが直ちに`task.create`へ進むようにする。
2. **workerの開始待ちを協調的に中断可能にする。** Ctrl-Cでstop eventを立て、gateは`time.sleep`ではなくevent待ちを使う。外部リクエスト開始前にeventが勝ったworkerはbackendを呼ばず、main threadが対応するrunを失敗終端する。この場合、workerとbackendの境界へcancel tokenを通す契約が必要になる。

既存の検証14はsubmit後・worker開始前、15は外部処理開始済みを扱うが、その中間である「worker開始後・外部リクエスト開始前」を扱わない。`min_start_interval_seconds > 0`のfake backendとManus gateの双方でこの状態を決定論的に作り、Ctrl-C後に2本目の外部リクエストが始まらず、対応runがfailedで閉じることを追加検証する。

**採否: 再修正。** `Future.cancel()`を使う方針は維持し、課金開始前の待機をその境界の外へ出すか、協調cancel可能にする。

#### 指摘18: 文書末尾の`## 改訂`は後続レビューと両立しない — 重大度: 中

最終案`:274`は改訂記録を「文書末尾の`## 改訂`」へ積む。一方、現行規則はレビューroundを`## レビュー`配下へ追記し、確定前はappendする。この文書ではレビュー10の後に`## 改訂`と改訂1〜3が置かれ、その後に追記されたレビュー11（現在`:1375`）がMarkdown上は`## 改訂`の配下に入った。さらに改訂4とレビュー見出しが同じ階層で混在している。これは偶然の整形ミスではなく、末尾を改訂節が占有した後にもレビューが続くライフサイクルを規則が定義していないために起きた。

確定前のレビューが続く可能性を残すなら、少なくとも次を一意に決める必要がある。

- `## 改訂`は`確定`後にだけ作り、確定前の変更はレビューの採否として記録する。
- または`## 改訂`の配置を`## レビュー`より前へ固定し、各節へ追記することを許す。
- または複数の`## レビュー`節を許す構造と、その順序を規約化する。

現在の文書についても、レビュー11を`## レビュー`配下、改訂4を`## 改訂`配下へ置く必要がある。内容を書き換える必要はなく、見出しの所属だけの修正である。

**採否: 修正して採用。** 確定仕様の改訂履歴を残す方針は維持し、確定前の追加レビューが置けるsection順序とライフサイクルを定義する。

### レビュー13 — Claude Code (2026-08-19)

指摘17と18を検証した。**両方とも正しく、採用した。** 指摘17は改訂5、指摘18は改訂6として記録し、本文書の構造も並べ直した。

#### 検証の記録

| 指摘 | 主張 | 検証結果 |
|---|---|---|
| 17 | `_summarize_with_manus` はtask作成の前にgateを通る | **正しい。** `feedian/llm.py:337-344`の**最初の1行**が `_wait_for_manus_create_slot()` である。payload組み立ても `task.create` もその後にある |
| 17 | gateはlockを保持したまま最大6.1秒sleepする | **正しい。** `feedian/llm.py:487-495`が `with _manus_create_lock:` の内側でsleepする。`MANUS_CREATE_INTERVAL_SECONDS = 6.1`（同 `:19`） |
| 18 | レビュー11が `## 改訂` の配下へ入っている | **正しい。** 私が文書末尾へ追記したためである。改訂4とレビュー見出しが同じ階層で混在していた |

#### 採否

| 指摘 | 採否 | 対応 |
|---|---|---|
| 17. 開始間隔待ちのRUNNING Futureが中断後に課金する | 採用 | 間隔をschedulerの投入条件へ移す。改訂5 |
| 18. 文書末尾の `## 改訂` が後続レビューと両立しない | 採用 | セクション順序を固定し、本文書の構造も直した。改訂6 |

#### 指摘17への対応

指摘は正しい。私の中断契約は「FutureがRUNNING == backendを呼んだ」を前提にしていたが、**待機をworkerへ置く設計を自分で採ったせいで、その前提を自分で壊していた。** `llm.workers = 8` でManusを動かせば、先頭以外の7本が最大6.1秒この状態に入り得る。仮定上の窓ではない。

Codexが挙げた2案のうち**案1を採る。** ただし「main threadで枠を予約する」ではなく、**schedulerの投入条件**として実装する。main threadは間隔待ちでsleepせず、未到達なら投入を見送って結果回収へ戻る。main threadを止めないためである。

この形の利点は、**機構が増えるのではなく減る**ことである。worker側のlockもsleepも不要になり、「RUNNING」と「送る直前」が一致するので、レビュー6で決めた `Future.cancel()` の境界がそのまま正しくなる。

**案2（協調cancel）は不採用とする。** stop eventとcancel tokenをbackend interfaceへ通す必要があり、全backendの契約が変わる。間隔待ちをworkerに置かなければ不要な機構である。

Manusについては、間隔が2箇所で強制されないよう分ける。`manus-api` の `min_start_interval_seconds` を `6.1` と宣言し（現在は `0.0`）、ingest経路は `_summarize_with_manus` のgateを迂回する。legacy export経路（`feedian/__main__.py`）にschedulerは無いので現行どおりgateを使う。**間隔はどの経路でもちょうど1回だけ強制される。**

指摘のとおり検証に穴があった。既存の14はsubmit後・worker開始前、15は外部処理開始済みを扱うが、その中間を扱っていない。12a・12b・17aを追加した。

#### 指摘18への対応

指摘は正しく、原因は私にある。レビュー11と改訂4を**文書末尾へ追記した**ため、`## 改訂` の配下へ入った。規則が「文書末尾の `## 改訂`」と書いていたことが、この事故を招く形になっていた。

**セクション順序を `最終案` → `改訂` → `草案` → `レビュー` に固定する。** `## レビュー` を必ず最後にすることで、追記先が常に空く。Codexが挙げた3案のうち2番目に相当する。

1番目の案（`## 改訂` は `確定` 後にだけ作る）は**不採用**とする。確定前と確定後で手順が変わることになり、「コミット前後を問わず同じ処理」という決定と同じ形の分岐を持ち込む。3番目の案（複数の `## レビュー` 節を許す）も不採用とする。節が増えるだけで、順序を決める問題は残る。

本文書の構造も並べ直した。レビュー11を `## レビュー` 配下へ、改訂4を `## 改訂` 配下へ移した。**内容は1文字も変えていない。**

### レビュー14 — Codex (2026-08-19)

結論は**要修正**である。開始間隔をschedulerの投入条件へ移し、Manusのingest経路で内部gateを迂回する方針は、指摘17が問題にした最大6.1秒の待機をRUNNING Futureから除く。section順序も`最終案 → 改訂 → 草案 → レビュー`へ直り、指摘18は解消した。一方、「FutureがRUNNING」と「外部リクエスト開始」が一致するという前提は、待機を除いても`ThreadPoolExecutor`の状態遷移上は成立しない。さらに、開始可能時刻までsubmitもsleepもしないschedulerは、回収対象のFutureが無いとbusy loopになる。

#### レビュー12の指摘の再判定

| 指摘 | 再判定 | 理由 |
|---|---|---|
| 17. 開始間隔待ちのRUNNING Future | 再修正 | 長いgate待ちは除去したが、RUNNINGと外部I/O開始の間に必ず残る窓を「存在しない」としたため、課金境界の定義がまだ成立しない |
| 18. `## 改訂`と後続レビュー | 採用 | `## レビュー`が文書末尾へ移り、レビュー11・改訂4も正しいsectionへ整理された |

#### 指摘19: RUNNINGと外部リクエスト開始は同一の境界にならない — 重大度: 高

最終案`:209`は、worker側の待機を無くせば「FutureがRUNNING」と「外部リクエストを送る直前」が一致するとする。検証12aも「worker開始後・外部リクエスト開始前の状態が発生しない」ことを要求する（`:415`）。しかし最終案自身が`:241-243`で記録しているとおり、`ThreadPoolExecutor`はまず`set_running_or_notify_cancel()`でFutureをRUNNINGへ遷移させ、**その後に**callableを呼ぶ。callableはさらに`backend.summarize`へ入り、payloadやHTTP requestを組み立ててから外部I/Oを始める。gateを迂回しても、この順序と時間窓は必ず存在する。

したがって、RUNNING遷移後かつ最初の外部I/O前にCtrl-Cが入れば、`cancel()`は`False`を返し、workerはdrain対象になる。その後に外部リクエストを送るため、`:266`を文字どおり「Ctrl-C時点で未送信のリクエストは課金されない」と読むと依然として違反する。また、schedulerが「次に開始してよい時刻」をsubmit時刻から計算する場合、submit後にworkerが実際に外部I/Oへ入るまでの遅延が各taskで異なれば、実リクエストの開始間隔が指定値未満になる可能性も残る。

ここでは境界を次のどちらかに決めなければならない。

1. **RUNNINGを課金へのcommit境界と定義する。** `cancel()`に失敗したFutureは、まだwireへ送っていなくても「開始済み」とみなし、Ctrl-C後にリクエストを送り得る。`:266`とhelpは「cancelに成功した未開始Futureだけは課金されない」と書き換える。検証12aは成立しないため、「worker内に意図的な間隔待ちが無い」「Manusの内部gateを迂回する」ことへ置き換える。
2. **外部I/O開始を厳密な境界とする。** workerがI/O直前にstop stateを確認できる協調cancelまたは開始handshakeを設ける。Ctrl-Cが先なら送信せずrunを失敗終端し、workerが開始commitを先に取った場合だけdrainする。この厳密さを求めるなら、レビュー13で不採用にしたcancel token相当の機構が必要になる。

日常用途と実装の単純さを優先するなら案1で十分である。ただし、実際には存在する窓を「発生しない」と検証すること、およびwire送信時刻の保証として表現することはできない。開始間隔についても、submit／RUNNING／外部I/Oのどの時刻を保証するのかを同じ定義へ揃える必要がある。

**採否: 再修正。** 長いinterval待ちをworkerから除く変更は採用し、課金開始の契約と検証を実在するFuture境界へ合わせる。

#### 指摘20: 開始可能時刻までsubmitもsleepもしないとbusy loopになる — 重大度: 中

最終案`:209`は、開始可能時刻に未到達ならsubmitせず結果回収へ戻り、main threadはsleepしないとする。実行中Futureがあれば、その完了待ちを次のwake-upに利用できる。しかし全Futureが完了した直後や最初のtask完了後など、**回収対象が空で次のbackend開始時刻だけが未来**という状態は通常に起きる。空集合に対するpoll／`wait`は即座に返るため、次の6.1秒間をmain threadが回り続ける。

schedulerの待機条件を「Futureの完了または最も早いbackend開始可能時刻」と定義する必要がある。実行中Futureがあれば`concurrent.futures.wait(..., timeout=next_due-now, return_when=FIRST_COMPLETED)`を使い、無ければmain threadでそのtimeoutだけinterruptibleに待つ。これはFutureも`llm_run`も作る前の待機なので、指摘17のRUNNING問題を再導入しない。Ctrl-Cはmain threadの待機を中断できる。

fake clockとwait関数を使い、開始時刻未到達かつ実行中Futureなしの状態で、spinせず1回のtimeout待ちへ入ることを検証する。

**採否: 修正して採用。** 非blockingな結果回収の方針は維持し、次に意味のあるeventまでのtimeout待ちを加える。

### レビュー15 — Claude Code (2026-08-19)

指摘19と20を検証した。**両方とも正しく、採用した。** 改訂7・改訂8として記録した。

#### 検証の記録

| 指摘 | 主張 | 検証結果 |
|---|---|---|
| 19 | RUNNING遷移と外部I/O開始の間に窓が必ず残る | **正しい。** `set_running_or_notify_cancel()` の成功後に `self.fn(...)` が呼ばれ（`thread.py:54-59`）、そこから `backend.summarize` へ入ってpayloadとHTTP requestを組み立てる。gateを迂回しても順序は変わらない |
| 19 | 検証12aは証明できない要求である | **正しい。** 「その状態が発生しない」ことは、状態が構造的に存在する以上テストできない |
| 20 | 回収対象が空で開始時刻だけ未来の状態はbusy loopになる | **正しい。** 空集合への `wait` は即座に返る。直前のtaskがすべて完了した直後に通常起きる |

#### 採否

| 指摘 | 採否 | 対応 |
|---|---|---|
| 19. RUNNINGと外部I/O開始が一致しない | 採用（案1） | 課金境界をRUNNING遷移と定義し、契約・help・検証を実在する境界へ合わせる。改訂7 |
| 20. schedulerのbusy loop | 採用 | 待機条件を「Future完了または最早の開始可能時刻」と定義する。改訂8 |

#### 指摘19への対応

指摘は正しい。**改訂5で私が書いた「RUNNINGと送る直前が一致する」は誤りである。** 6.1秒のgateを除いても、`set_running_or_notify_cancel()` → callable → `summarize` → payload組み立て → 外部I/O という順序は残る。窓は縮んだが消えていない。

Codexが挙げた2案のうち**案1を採る。** 課金へのcommit境界をFutureのRUNNING遷移と定義し、契約とhelpを実態へ合わせる。

案2（外部I/O開始を厳密な境界とする）は**不採用**とする。協調cancelまたは開始handshakeを全backendのinterfaceへ通す必要があり、レビュー13で不採用にしたcancel token相当の機構になる。**窓に残るのはpayload組み立てだけで、sleepもlockも待機も無い。** 6.1秒のgateを除いた後は、最悪でも中断1回につきリクエスト1件である。この差を閉じるために全backendの契約を変える価値は無い。

開始間隔の保証時刻についても指摘のとおり定義を揃えた。**保証するのはscheduler投入時刻の間隔であり、wire送信時刻ではない。** 両者の差はthread dispatch遅延だけで、そこにsleepもlockも挟まらないため6.1秒に対して無視できる。wire時刻を保証すると書けば嘘になる。

helpの文言も直す。「未送信は課金されない」ではなく「開始済みのリクエストは課金される場合があります」とする。検証12aは「worker内に意図的な待機が無い」へ置き換えた。

#### 指摘20への対応

指摘は正しい。「main threadはsleepしない」と書いたが、**回収対象が空のときに何を待つのかを定義していなかった。** 空集合への `wait` は即座に返るので、次の開始可能時刻までmain threadが回り続ける。

待機条件を「いずれかのFutureの完了、または最も早いbackend開始可能時刻」と定義した。実行中Futureがあれば `wait(..., timeout=next_due-now, return_when=FIRST_COMPLETED)`、無ければmain threadでその timeout だけ待つ。

**この待機がFutureも `llm_run` も作る前である点が重要である。** 改訂5が解いた「RUNNINGだが未送信」の問題を再導入しない。待機はmain threadなのでCtrl-Cで中断できる。検証17bを追加した。

#### 3ラウンドかけた理由

指摘17・19・20は同じ1点をめぐっている。**「どこを課金の開始とみなすか」を、私が実装の都合で3回動かした。**

1. レビュー6 — `Future.cancel()` の排他性で境界を決めた。**正しかった。**
2. レビュー13 — 間隔待ちをworkerから外すために「RUNNINGと送信直前が一致する」と書いた。**境界の定義を、実装の性質ではなく願望で上書きした。** これが誤りである。
3. レビュー15 — 境界をRUNNINGに戻し、窓の存在を認めて契約とhelpをそちらへ合わせた。

### レビュー16 — Codex (2026-08-19)

結論は**要修正**である。指摘19はRUNNINGを課金commit境界と明記し、wire送信との窓を認める案1で解消した。指摘20もFuture完了または最早開始可能時刻までtimeout待ちする契約になり、busy loopは解消した。残る問題は、RUNNING後・wire送信前の窓からCtrl-C後に送られ得るリクエスト数を「最悪1件」としている点である。この上限は並列度8のbackendでは成立しない。

#### レビュー14の指摘の再判定

| 指摘 | 再判定 | 理由 |
|---|---|---|
| 19. RUNNINGと外部I/O開始の境界 | 採用 | RUNNINGを課金commit境界と定義し、cancel失敗後は未送信でも送られ得ることを契約・help・検証へ反映した |
| 20. schedulerのbusy loop | 採用 | Future完了または次の開始可能時刻をwake-up条件とし、Futureが空の経路にもtimeout待ちを定めた |

#### 指摘21: Ctrl-C後に送られ得るリクエストは最大1件ではない — 重大度: 中

最終案`:282`は、RUNNINGからwire送信までに残るのがpayload組み立てだけなので「最悪でも中断1回につきリクエスト1件」とする。しかし`openai-responses`は`max_parallelism = 8`、`min_start_interval_seconds = 0`であり、schedulerはglobal枠とbackend枠が空いていれば最大8件をsubmitする。executorの各workerは独立にFutureをRUNNINGへ遷移させるため、Ctrl-C時点で**複数のFutureが同時にRUNNINGかつwire未送信**であり得る。

その状態では全Futureの`cancel()`が`False`になり、採用したcommit契約どおり、各workerがCtrl-C後に送信してよい。したがって中断後に新たにwireへ出る件数の上限は1ではなく、**その時点でRUNNINGだが未送信のFuture数**であり、最悪ではbackendの実効並列度（既定設定のOpenAIなら8）である。Manusは6.1秒の投入間隔により通常1件以下になるが、そのbackend固有の性質を全backendの上限に一般化できない。

「最悪1件」を削除し、次のように契約を直す必要がある。

> RUNNING後・wire送信前の窓は短いが、同時に複数存在し得る。Ctrl-C後に送られる可能性がある件数は、その時点のRUNNING Future数以下であり、global worker数とbackendの`max_parallelism`で上限を受ける。利用者が選んだ並列度を超える課金は発生しない。

検証では、N個のworkerをRUNNING遷移後・fake外部送信前のbarrierで止めてCtrl-Cを発生させ、全N個の`cancel()`が失敗し、解放後にN件が送信され得る一方、未開始Futureはcancelされることを固定する。これは案1のcommit境界を負のケースまで正確に記録するテストになる。

**採否: 修正して採用。** RUNNINGを課金commit境界とする判断は維持し、窓の件数上限だけを実効並列度へ合わせる。

教訓は、**境界の定義を実装の都合に合わせて動かしてはならない**ということである。実装が境界に届かないなら、届かないことを契約に書く。実装時も同じ順序で考える。

### レビュー17 — Claude Code (2026-08-19)

指摘21は**正しい。採用した**（改訂9）。

#### 検証の記録

| 主張 | 検証結果 |
|---|---|
| `openai-responses` は `max_parallelism = 8`、`min_start_interval_seconds = 0` である | **正しい。** 最終案が定めた値であり、間隔gateを持たない |
| 複数Futureが同時にRUNNINGかつwire未送信であり得る | **正しい。** 各workerは独立に `set_running_or_notify_cancel()` を呼ぶ。schedulerが8件を投入していれば8件が同時にこの状態を取り得る |
| Manusの「通常1件以下」は固有の性質である | **正しい。** 6.1秒の投入間隔によるものであり、間隔を持たないbackendには当てはまらない |

#### 採否

| 指摘 | 採否 | 対応 |
|---|---|---|
| 21. Ctrl-C後に送られ得る件数の上限 | 採用 | 上限を実効並列度へ直し、helpと検証17cへ反映した。改訂9 |

#### 対応

指摘は正しい。**「最悪1件」は、窓の長さについての観察を件数へ誤って一般化したものだった。** payload組み立てが一瞬で終わることと、その状態にあるFutureが1つしかないことは別である。

上限を次へ直した。

> Ctrl-C後に送られる可能性がある件数は、その時点でRUNNINGかつ未送信のFuture数以下である。global worker数とbackendの `max_parallelism` で上限を受ける。**利用者が選んだ並列度を超える課金は発生しない。**

**協調cancelを不採用とする判断は変えない。** ただし根拠を正しい件数の上で述べ直した。窓に残るのはpayload組み立てだけで待機は無く、Ctrl-Cがその区間に入る確率は小さい。入った場合の損失も利用者が選んだ並列度分の要約1回分に収まる。全backendのinterfaceを変える価値は無い。

helpにも件数の上限を書く。検証17cを追加した。

#### 補足

レビュー16の末尾に、レビュー15の「教訓」段落と同一の文が重複して入っている。レビューroundは各レビュー者のものなので、こちらからは編集しない。内容の判断には影響しない。

### レビュー18 — Codex (2026-08-19)

結論は**承認**である。レビュー17は指摘21を採用し、Ctrl-C後にwireへ送られ得る件数の上限を「その時点でRUNNINGかつ未送信のFuture数」とした。global worker数とbackendの`max_parallelism`の双方で上限を受けるため、既定の`openai-responses`では最大8、開始間隔を持つManusでは通常それ以下になる。helpと検証17cも同じ契約へ揃っており、「最悪1件」という誤った上限は最終案から除かれた。

#### レビュー16の指摘の再判定

| 指摘 | 再判定 | 理由 |
|---|---|---|
| 21. Ctrl-C後に送られ得る件数の上限 | 採用 | 上限を実効並列度へ直し、複数のRUNNING Futureをbarrierで固定する検証17cを追加した |

#### 最終確認

- Hatenaの早期打ち切りは、71リクエスト・6,987行・69ページ境界の全件観測に基づき、クエリごとの非増加順を前提とする。未保証の外部API契約であるリスクと`--full`による復旧経路も明記されている。
- syncのworkerは外部取得だけを担当し、DB、browser合成、RSS fallback、保存、auditをmain threadに残す。同一resourceのitemは同じchunkへ入れず、保存後に通常の`should_fetch_resource`を再評価するため、成功・失敗・`force_fetch`の各経路で直列挙動を維持する。
- ingestはbackend別の実効並列度とscheduler投入間隔を持ち、Manusの間隔gateを経路ごとに1回だけ適用する。schedulerはFuture完了または次の開始可能時刻まで待つためbusy loopを作らない。
- Ctrl-Cでは未開始Futureを`cancel()`し、RUNNING Futureを課金commit済みとしてdrainする。cancel成功、RUNNING、`start_llm_run`からsubmitまでの窓、強制終了後の回収がそれぞれ監査上終端する。
- 設定のparse・render・既定値・厳格検証、確定仕様の改訂方法、section順序、`DESIGN.md`との役割分担が実装可能な粒度で定義されている。

レビュー1から継続した指摘1〜21はすべて最終案へ反映された。新たな指摘はない。人間がステータスを`確定`へ変更すれば、仕様単独の`docs:` commitへ進める。
