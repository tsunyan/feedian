# 本文取得の再試行抑制

ステータス: 確定

## 最終案

### 結論

取得に失敗し続けるresourceの再試行を抑制し、日常の`feedian sync`から無駄な通信を取り除く。機構は2つある。

- **A. 指数backoff** — 連続失敗回数に応じて再試行の間隔を伸ばす。一時的な障害は時間を置いて自動的に復帰する。
- **B. 終端ステータス** — 404と410は`--force-fetch`以外で再試行しない。

抑制はquickとfullの双方に効かせる。`--force-fetch`が唯一の復旧経路であり、新しいフラグは追加しない。

あわせて、本プロダクトの判断の指針を`AGENTS.md`と`README.md`へ記録する。

### 確定事項

#### 1. 判断の指針

このプロダクトが求めるのは**日常での実用性**であって、アカデミックな完璧さでも、魚拓サービスのような完全なスナップショットでもない。

`AGENTS.md`へ規約より前に置く。規約同士の優先順位を決めるのはこの指針であるためである。`README.md`へは利用者が用途の合致を判断できるよう`## Scope`として置く。

**適用限界を必ず併記する。** 指針は次を上書きしない。

- 明示された要件
- 確定した仕様
- データ保全の規約（保存済みの本文を失う・壊すことは常に不具合である）

これらで決まらないトレードオフにのみ用いる。限界を書かなければ、「実用性」を名目に要件や確定仕様を覆す口実になり得る。

#### 2. 適用範囲

抑制の判定は`should_fetch_resource`へ置き、**quickとfullの双方に効かせる。**

`--full`は「既知itemも含めて全件処理する」という指示であって、「到達不能と分かっているURLを叩き直す」という指示ではない。現行の完全同期も同じ2,577件を毎回取得しており、抑制の利益は完全同期にも等しくある。

`should_fetch_resource`は`force=True`で即座に真を返すため、`--force-fetch`（`--full`必須）がそのまま復旧経路になる。**新しいフラグは追加しない。**

初回実行では抑制すべき失敗履歴が存在しないため効果は無い。仕組み上の当然の帰結である。

#### 3. 判定の順序

`should_fetch_resource`は次の順で判定する。既存の失敗分岐の条件（warningがあり、HTTP/rendered payloadが無く、本文長が0）は変更しない。payloadを伴う失敗は従来どおり`refresh_days`側へ回る。

1. `force`が真 → 取得する。
2. captureが無い → 取得する。
3. 非空の本文を保持している → 従来どおり`refresh_days`で判定する。
4. 失敗状態（上記の既存条件）:
   1. `http_status`が`terminal_http_statuses`に含まれる → **取得しない。**
   2. `consecutive_failures`が0 → **取得する**（移行行。確定事項7を参照）。
   3. それ以外 → `min(retry_base_minutes * 2^(n-1), retry_max_days)`を経過していれば取得する。`n`は`consecutive_failures`。

#### 4. 機構A — 指数backoff

| 連続失敗 | 待機（既定値の場合） |
|---:|---|
| 1 | 30分 |
| 2 | 1時間 |
| 3 | 2時間 |
| 4 | 4時間 |
| … | … |
| 12以上 | 30日（上限） |

日次運用なら1週間ほどで待機が1日を超え、復旧しないresourceは日常の実行から外れる。

#### 5. 機構B — 終端ステータス

`terminal_http_statuses`（既定`[404, 410]`）に該当する失敗は、経過時間によらず再試行しない。

**根拠 — 対象Vaultの実測。** 404の1,885件をブックマーク年で層別すると次のとおりである。

| 層 | 404率 |
|---|---|
| 2020年以前 | 1,332 / 5,797 = 23.0% |
| 2024年以降 | 494 / 3,781 = 13.1% |
| 2024年以降（`x.com`を除く） | 26 / 3,781 = **0.7%** |

2024年以降の404の**468件（95%）が`x.com`**である。`x.com`はログインを要求して404を返すため、リンクが消えたのではなく構造的に取得できない。残りは古いブックマークほど404率が高く、新しいものはほぼ無い。すなわち404は「本当に消えた過去の記事」と「ログイン壁」の2種類で構成され、**どちらも再試行に意味がない。**

新規itemが404になる確率は0.7%であり、永久に再試行しない対象が日常運用で積み上がることもない。

**取得済み本文は失われない。** 一度取得に成功したresourceは、その後URLが404になっても本文を保持し続ける。`record_failed_fetch`は非空の本文を持つrevisionに触れないためである。この保証の上に「取れた時のものが残るなら、後で404になっても構わない」という判断が成立する。

#### 6. HTTPステータスの伝搬と状態遷移

**現行コードには失敗経路の`http_status`が存在しない。** これを補わなければ機構Bは1件も成立しない。

| `feedian/extract.py` | 経路 | 対応 |
|---|---|---|
| `:281` | その他のHTTP error（**404はここ**） | `http_status = exc.code`を設定する |
| `:276` | 401/403/406でbrowser fallbackも失敗 | **元のstatus**を設定する。失敗の原因を示すのは元のstatusであり、fallback側の内部statusではない |
| `:223` | blocked URL（DNS解決不能を含む） | NULLのまま。機構Aが扱う |
| `:283` | URLError（タイムアウト等） | NULLのまま。機構Aが扱う |
| `:285` | catch-all | NULLのまま。機構Aが扱う |

`_store_page`は`page.http_status`を`record_failed_fetch`と`record_resource_revision`の双方へ渡す。

**状態遷移**

| 経路 | `consecutive_failures` | `warning` | `http_status` |
|---|---|---|---|
| 失敗（`record_failed_fetch`） | +1 | 設定 | 渡された値（HTTP以外はNULL） |
| 成功（`record_resource_revision`で非空本文） | 0 | NULL | 渡された値 |
| 304（`record_not_modified_fetch`） | **0** | **NULL** | **304** |

304は「保持中の本文がサーバー上の最新と同じ」と確認できた**成功**である。`resource_fetch_validators`は非空の本文を保持しているときだけvalidatorを返すため、304は本文を持つresourceでのみ発生する。したがって「成功 → 一時的な失敗 → 304」の遷移が起こり得るため、304も失敗回数を解除しなければ、失敗していない回数が次回のbackoffへ持ち越される。304は終端ステータスに含めない。

#### 7. schema 8 と既存データ

SQLite schema versionを7から**8**へ上げる。`feedian migrate`による明示移行とし、暗黙移行はしない。

```sql
ALTER TABLE fetch_capture ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fetch_capture ADD COLUMN http_status INTEGER;
```

`retry_after`列は設けない。`fetched_at`と`consecutive_failures`から算出できるためであり、列を持つと算出規則と保存値の二重管理になる。

**移行行の読み方**（確定事項3-4-2）

| 移行行の状態 | 判定 |
|---|---|
| 本文が無く、既存の`warning`がある | **即座にdue。** 従来の30分ルールも適用しない |
| 本文がある | 従来どおり`refresh_days`に従う |

指数式`base * 2^(n-1)`は定義域が`n >= 1`である。`n = 0`を代入すると15分という意図しない値になるため、上表で明示的に上書きする。

**移行後の最初の1回は従来どおり全件を取得する**（対象Vaultで約30分）。その1回で`http_status`と失敗回数が記録され、404の1,885件が終端として外れ、残る約690件が機構Aへ入る。既存`warning`からの解析によるbackfillは行わない（却下した案を参照）。

#### 8. 設定

`config.fetch`へ追加する。検証は型ごとに分ける。

| キー | 既定 | 型 | 検証 |
|---|---|---|---|
| `retry_base_minutes` | 30 | 整数 | `bool`を拒否し、1以上 |
| `retry_max_days` | 30 | 整数 | `bool`を拒否し、1以上 |
| `terminal_http_statuses` | `[404, 410]` | 整数の配列 | 空を許す（機構Bの無効化）。各要素は`bool`を拒否し100以上599以下。重複は排除する |

#### 9. 可視性

`feedian status`へ、再試行しないresourceの件数を表示する。値の定義は次のとおりとする。

> 現在のVault configの`terminal_http_statuses`に含まれる`http_status`を最新captureに持ち、本文が無く、`removed_at IS NULL`のresource数

`terminal_http_statuses`が空なら0になる。`status_counts()`の署名は変えず、専用のqueryを追加してCLI層からstatus集合を渡す。設定と無関係な固定集計にはしない。

#### 10. 範囲外

- **並列化は扱わない。** 別仕様とする。本仕様は「叩く回数を減らす」ものであり「速く叩く」ものではない。順序が逆だと、無駄な通信を高速に大量発行することになる。
- resourceを削除・除外しない。抑制は取得しないだけであり、`removed_at`には触れない。
- `refresh_days`による正常なresourceの定期再取得は変更しない。
- payloadを伴う失敗（unsupported content typeなど）の扱いは変更しない。既存条件により`refresh_days`側へ回るため、日常の実行を圧迫しない。

### インターフェース

```python
# feedian/store.py — VaultStore
def record_failed_fetch(
    self, resource_id: str, *, warning: str, final_url: str = "",
    http_payload_id: str | None = None, rendered_payload_id: str | None = None,
    response_headers: dict[str, str] | None = None,
    http_status: int | None = None,          # 追加
) -> None: ...

def record_resource_revision(self, resource_id: str, *, ..., http_status: int | None = None) -> tuple[str, bool]: ...

def should_fetch_resource(
    self, resource_id: str, *, refresh_days: int, force: bool = False,
    retry_base_minutes: int = 30,             # 追加
    retry_max_days: int = 30,                 # 追加
    terminal_http_statuses: tuple[int, ...] = (404, 410),   # 追加
) -> bool: ...

def terminal_failure_count(self, terminal_http_statuses: tuple[int, ...]) -> int: ...   # 追加
```

`record_not_modified_fetch`は署名を変えず、成功遷移として失敗回数・warning・statusを更新する。

### 検証

```
python -m pytest -q
```

文書の変更（`AGENTS.md`、`README.md`、`DESIGN.md`）はテストの対象外だが、実装と同一のコミットに含める。

1. 連続失敗が増えるごとに`should_fetch_resource`が真を返す間隔が伸びる。
2. 待機が`retry_max_days`を超えない。
3. 非空本文の取得に成功すると`consecutive_failures`が0へ戻る。
4. `force=True`は`consecutive_failures`と`http_status`によらず真を返す。
5. 終端ステータス（404）の失敗は、経過時間によらず再試行されない。
6. 終端ステータスになったresourceが、既に保持している非空の本文を失わない。
7. **404の失敗で`http_status = 404`が保存される。**
8. DNS解決不能（blocked URL）とタイムアウトでは`http_status`がNULLになり、機構Aのbackoffに入る。
9. browser fallbackが失敗したとき、fallback側ではなく元のstatus（403など）が保存される。
10. 304が`consecutive_failures`を0へ、`warning`をNULLへ戻し、`http_status`を304にする。
11. 「成功 → 失敗 → 304 → 失敗」の順で、最後の失敗の`consecutive_failures`が1になる（2にならない）。
12. 移行直後の`consecutive_failures = 0`かつ本文なしかつwarningありの行が即座にdueになる。
13. 移行直後の本文がある行は`refresh_days`に従う。
14. 設定値の不正な型が拒否される — `retry_base_minutes`に`1.5`・`True`・`"2"`・`0`・`-1`、`terminal_http_statuses`に`404`（配列でない）・`[True]`・`[99]`・`[600]`。
15. `terminal_http_statuses`が空リストなら機構Bが無効になり、404も機構Aのbackoffに入る。
16. `feedian status`の件数が設定に依存し、空リストで0になる。
17. 完全同期でも抑制が効く。
18. 抑制されたresourceはquickの(B1)passの`--limit`予算を消費しない。
19. schema 7から8への移行後、既存行の`consecutive_failures`が0、`http_status`がNULLである。

### 却下した案

| 案 | 却下理由 |
|---|---|
| 404を「30日間隔」とし、`consecutive_failures`の初期値引き上げで表現する | 実測では404が「消えた過去の記事」と「ログイン壁の`x.com`」で構成され、いずれも再試行に意味がない。永久に再試行しない方が単純で、初期値を操作する算術も不要になる |
| 移行時に既存`warning`から`HTTP <数字>`を解析して`http_status`をbackfillする | 自由文の解析を移行へ持ち込む機械仕掛けにあたる。代償は移行後1回だけの約30分であり、判断の指針に照らして見合わない |
| `PageFetchResult`へ構造化した失敗種別を追加し、ドメイン消滅を終端扱いする | 変更範囲が大きい。194件は機構Aのbackoffで数回の実行のうちに日常から外れる |
| `warning`の前方一致でドメイン消滅を判定する | 自由文への依存が残る。同上の理由で機構Aに任せる |
| 403を`terminal_http_statuses`に含める | bot対策由来だと復活し得る。含めなくても機構Aが間隔を伸ばす |
| `no extractable text found`（12件）を特別扱いする | 日常の実行への影響が無い。抽出器の改善で解決し得る領域を再試行の仕組みで塞がない |
| `retry_after`列を持つ | `fetched_at`と`consecutive_failures`から算出できる。算出規則と保存値の二重管理になる |
| 再試行の抑制と並列化を1つの仕様にまとめる | 触る場所も判断の種類も違う。抑制を先に入れないと、無駄な通信を高速に大量発行することになる |
| 抑制を無視する新しいフラグを追加する | `should_fetch_resource(force=True)`が既に境界として存在し、`--force-fetch`がそのまま復旧経路になる |
| 抑制対象のresourceを`removed_at`で除外する | 取得しないことと、Vaultから消えることは別である。renderとingestの対象からも消えてしまう |

## 草案

### 背景 — 実測

[syncのquickモード](20260818-sync-quick-mode.ja.md) を投入した直後の実行結果である。

```
sync: mode=quick processed=0 changed=0 skipped=6541 fetched=2589 retried=12 failed=0 stopped_early=raindrop
  process: collecting hatena bookmarks  100% 6985/6985  0:05:36
  process: syncing all items              0% 0/0        0:30:24
```

新着ゼロにもかかわらず本文取得が2589回発生し、本文を得られたのは12件だった。約30分はこの取得に費やされている。既知item 6541件のスキップとRaindropの早期打ち切りは設計どおり動いており、**支配的なコストは「取得できない本文の再試行」へ移った。**

対象Vaultの内訳は次のとおりである。

| | 件数 |
|---|---|
| resource 総数（`removed_at IS NULL`） | 9,871 |
| `current_revision_id IS NULL`（(B1)候補） | 2,577 |
| 空本文＋warning capture（互換分岐） | 6 |

失敗の内訳（最新captureのwarning、上位）:

| 件数 | warning |
|---:|---|
| 1,885 | `HTTP 404` |
| 134 | `blocked URL: hostname could not be resolved: headlines.yahoo…` |
| 65 | `HTTP 403; browser fallback failed: browser HTTP 403` |
| 37 | 接続タイムアウト（WinError 10060） |
| 32 | `blocked URL: hostname could not be resolved: japanese.engadg…` |
| 32 | `[SSL: CERTIFICATE_VERIFY_FAILED]` |
| 28 | `blocked URL: hostname could not be resolved: matome.naver.jp` |
| 27 | `HTTP 400` |
| 12 | `no extractable text found` |
| 12 | `HTTP 520` |

**2,577件の大半は恒久的に到達不能である。** 404が1,885件、サービス終了によるドメイン消滅が194件。これらが毎回の`feedian sync`で再取得される。

### 現状の構造

再試行が抑制されない理由は3つある。

1. **失敗時の待機が30分しかない。** `should_fetch_resource`は、warningがありpayloadが無く本文が空のcaptureに対して`timedelta(minutes=30)`で判定する（`feedian/store.py:666-689`）。日次運用では毎回再試行と変わらない。
2. **失敗の履歴が残らない。** `fetch_capture`はresourceごとに1行を保持し、再取得のたびに上書きする（`feedian/store.py:399-427`）。連続何回失敗したかを数える情報が存在しない。
3. **失敗の種類が構造化されていない。** `PageFetchResult.http_status`は算出されている（`feedian/extract.py:44`、`296`、`307`、`317`）が、`fetch_capture`に対応する列が無く**捨てられている**。判別できるのは自由文の`warning`だけである。

なお、quickモードで失敗resourceが(B1)候補から抜けないのは意図した設計である（`current_revision_id`がNULLのまま残るため。[quickモード仕様](20260818-sync-quick-mode.ja.md) の訂正6）。抜けさせるのではなく、再試行の間隔を伸ばすのが本仕様の方針である。

### 判断の指針

このプロダクトが求めるのは**日常での実用性**であって、アカデミックな完璧さでも、魚拓サービスのような完全なスナップショットでもない。

この指針は本仕様の判断の土台であると同時に、**本仕様の成果物の一部として`AGENTS.md`と`README.md`へ記録する。** 本仕様に限らず判断が割れたときに参照できるようにするためである。`AGENTS.md`では規約より前に置く（規約同士の優先順位を決めるのはこの指針であるため）、`README.md`では利用者が用途の合致を判断できるよう`## Scope`として置く。

本仕様における具体的な働きは次のとおりである。

- 到達不能なURLを**取りこぼさないこと**より、日常の`feedian sync`が**軽いこと**を優先する。
- 「本当に恒久的に死んでいるか」を正確に判定する必要はない。**日常の実行から外れれば足りる。**
- そのために新しい状態・schema・自由文の解析を増やすなら、増やさない側を選ぶ。
- ただし**既に保存した本文を失う・壊すことは許容しない。** 指針が緩めるのは「どこまで取りに行くか」であって、「取ったものの正しさ」ではない。

草案作成時に4点あった未決事項は、この指針を当てるとすべて答えが出た（後述「指針を当てた整理」）。

### 目的と非目的

**目的**

- 恒久的に到達不能なURLの再試行頻度を下げ、日常の`feedian sync`から無駄な通信を取り除く。
- 一時的な障害（タイムアウト、5xx）は、時間を置いて自動的に再試行され続ける。
- 利用者が明示的に要求したとき（`--force-fetch`）は、抑制を無視して取得する。
- 判断の指針を`AGENTS.md`と`README.md`へ記録し、以後の判断で参照できるようにする。

**非目的**

- **並列化は扱わない。** 別仕様とする。本仕様は「叩く回数を減らす」ものであり、「速く叩く」ものではない。順序が逆だと、無駄な通信を高速に大量発行することになる。
- resourceを削除・除外しない。抑制はあくまで間隔の話であり、対象から消さない。
- `refresh_days`による正常なresourceの定期再取得は変更しない。

### 適用範囲 — quickとfullの双方に効かせる

抑制の判定を`should_fetch_resource`へ置き、**quickとfullの両方に効かせる。**

`--full`は「既知itemも含めて全件処理する」という指示であって、「到達不能と分かっているURLを叩き直す」という指示ではない。現行の完全同期も同じ2,577件を毎回取得しており、抑制の利益は完全同期にも等しくある。

抑制を無視して取得する手段は既にある。`should_fetch_resource`は`force=True`で即座に真を返す（`feedian/store.py:667-668`）ため、`--force-fetch`がそのまま逃げ道になる。**新しいフラグは追加しない。**

初回実行では抑制すべき失敗履歴が存在しないため、効果は無い。これは仕組み上の当然の帰結であり、問題ではない。

### 設計案

#### A. 連続失敗回数に応じた指数backoff（主機構）

`fetch_capture`へ連続失敗回数を持たせ、待機時間を`base * 2^(n-1)`で伸ばす。上限を設ける。

| 連続失敗 | 待機（base=30分、上限30日） |
|---:|---|
| 1 | 30分 |
| 2 | 1時間 |
| 3 | 2時間 |
| 4 | 4時間 |
| 5 | 8時間 |
| 6 | 16時間 |
| 7 | 32時間 |
| … | … |
| 12以上 | 30日（上限） |

成功時（非空本文のrevisionを記録したとき）に0へリセットする。

この機構だけでも、日次運用なら1週間ほどで待機が1日を超え、死んだリンクは日常の実行から外れる。一時的な障害も自動的に復帰する。

#### B. 終端ステータスは再試行しない（補助機構）

`fetch_capture`へHTTPステータスを保存し、**終端ステータス（既定で404と410）の失敗は`force`以外で再試行しない。** 待機時間の計算に載せず、`should_fetch_resource`が偽を返す。

**根拠 — 対象Vaultの実測。** 404の1,885件をホスト別に見ると、上位10ホストで59%を占める。

| 件数 | ホスト |
|---:|---|
| 468 | `x.com` |
| 184 | `www3.nhk.or.jp` |
| 126 | `anond.hatelabo.jp` |
| 93 | `www.asahi.com` |
| 53 | `news.yahoo.co.jp` |

ブックマーク年で層別すると性質がはっきり分かれる。

| 層 | 404率 |
|---|---|
| 2020年以前 | 1,332 / 5,797 = 23.0% |
| 2024年以降 | 494 / 3,781 = 13.1% |
| 2024年以降（`x.com`を除く） | 26 / 3,781 = **0.7%** |

2024年以降の404の**468件（95%）が`x.com`**である。`x.com`はログインを要求して404を返すため、リンクが消えたのではなく**構造的に取得できない**。再試行してもこの状態は変わらない。

残りの層は、古いブックマークほど404率が高く（23%）、新しいものはほぼ無い（0.7%）。すなわち404は「本当に消えた過去の記事」と「ログイン壁」の2種類で構成され、**どちらも再試行に意味がない。**

新規に取り込むitemが404になる確率は0.7%であり、永久無視の対象が日常運用で積み上がることもない。

**復旧経路。** `--force-fetch`（`--full`必須）が唯一の再取得手段である。サイト移転で復活した場合や、将来`x.com`へ認証を通せるようになった場合はこれを使う。新しいフラグは追加しない。

**取得済み本文は失われない。** 一度取得に成功したresourceは、その後URLが404になっても本文を保持し続ける。`record_failed_fetch`は非空の本文を持つrevisionに触れないためである（`tests/test_store.py`の`test_record_failed_fetch_on_a_good_revision_leaves_it_untouched`で固定済み）。「取れた時のものが残るなら、後で404になっても構わない」という前提はこの保証の上に成立する。

**可視性。** 永久に再試行しないresourceは黙って消えるのではなく、`feedian status`に件数を表示する。利用者が`--force-fetch`を使う判断をできるようにするためである。

#### C. 既存データの扱い

移行直後、既存の2,577件は`consecutive_failures = 0`で`http_status`も未記録である。したがって**移行後の最初の1回は従来どおり全件を取得する**（約30分）。その1回で404の1,885件が終端として記録され、以降は再試行されない。残る約690件は機構Aのbackoffに入る。

移行時に既存の`warning`から`HTTP <数字>`を解析して`http_status`を埋める案もあるが、採らない（「指針を当てた整理」を参照）。

### schema

SQLite schema versionを7から**8**へ上げる。`feedian migrate`による明示移行とする。

```sql
ALTER TABLE fetch_capture ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fetch_capture ADD COLUMN http_status INTEGER;
```

`retry_after`列は設けない。`fetched_at`と`consecutive_failures`から算出できるためである。列を増やすと、算出規則と保存値の二重管理になる。

**書き込み経路**

- `record_failed_fetch` — `consecutive_failures`を加算し、`http_status`を保存する。現在の引数に`http_status`を追加する。
- `record_resource_revision` — 非空の本文を記録したとき`consecutive_failures`を0へ戻し、`http_status`を保存する。
- `record_not_modified_fetch` — 304は本文を保持している場合にしか発生しないため、変更しない。

### 設定

`config.fetch`へ追加する。既定値は上表のとおり。

| キー | 既定 | 意味 |
|---|---|---|
| `retry_base_minutes` | 30 | 連続失敗1回目の待機。現行の30分と一致する |
| `retry_max_days` | 30 | 待機の上限 |
| `terminal_http_statuses` | `[404, 410]` | `force`以外で再試行しないステータス。空リストで機構Bを無効化できる |

いずれも1以上の整数として検証する（quickモードの`quick_stop_after_known_pages`と同じ扱い）。

### 検証

`python -m pytest -q`

文書の変更（`AGENTS.md`、`README.md`）はテストの対象外だが、実装と同一のコミットに含める。`DESIGN.md`の更新も同様である。

1. 連続失敗が増えるごとに`should_fetch_resource`が真を返す間隔が伸びる。
2. 待機が`retry_max_days`を超えない。
3. 非空本文の取得に成功すると`consecutive_failures`が0へ戻る。
4. `force=True`は`consecutive_failures`の値によらず真を返す。
5. 終端ステータス（404）の失敗は、経過時間によらず再試行されない。
6. 終端ステータスでも`force=True`なら取得する。
6b. 終端ステータスになったresourceが、既に保持している非空の本文を失わない。
6c. `feedian status`が再試行しないresourceの件数を表示する。
7. 抑制されたresourceは(B1)passの`--limit`予算を消費しない（現行の`should_fetch_resource`ゲートで既にそうなっているが、回帰として固定する）。
8. `record_failed_fetch`が`http_status`を保存する。HTTP以外の失敗（DNS、SSL）ではNULLになる。
9. schema 7から8への移行後、既存行の`consecutive_failures`が0、`http_status`がNULLである（Cを採らない場合）。
10. 完全同期でも抑制が効く。

### 指針を当てた整理

草案作成時に未決としていた4点は、「判断の指針」を当てると次のように定まる。**いずれも機構Aへ寄せ、機構Bは最小に保つ**という同じ結論になる。

| # | 論点 | 結論 | 理由 |
|---|---|---|---|
| 1 | 移行時に既存`warning`から`http_status`をbackfillするか | **行わない** | 自由文の解析を移行へ持ち込む「機械仕掛け」にあたる。代償は移行後1回だけの約30分であり、日常の実行が軽くなればそれで足りる |
| 2 | ドメイン消滅（194件）を終端扱いするか | **機構Aに任せる** | `PageFetchResult`へ構造化した失敗種別を足すのは変更範囲が大きく、`warning`の前方一致は自由文依存が残る。指数backoffだけでも数回の実行で日常から外れる |
| 3 | 403（65件）を`terminal_http_statuses`に含めるか | **含めない** | bot対策由来だと復活し得る。含めなくても機構Aが間隔を伸ばすため、日常の実行に残らない |
| 4 | `no extractable text found`（12件）を対象にするか | **特別扱いしない** | 12件であり、日常の実行への影響が無い。抽出器の改善で解決し得る領域を再試行の仕組みで塞がない |

結果として、機構Bの`terminal_http_statuses`は既定`[404, 410]`のまま残る。実測では404の1,885件が「消えた過去の記事」と「ログイン壁の`x.com`」で構成され、いずれも再試行に意味がない。機構Aだけに任せると2,577件が実用的な間隔へ達するまで6〜7回（1回30分）を要し、「日常の実行が軽い」という目的に届かない。

### 未決事項

なし。上表のとおり、当初の4点は指針で解決した。

実装中に新たな判断が生じた場合も、まず「判断の指針」を当てること。それでも決まらなければ、それは本当の未決事項であり、仕様へ追記して合意を取る。

## レビュー

### レビュー1 — Codex (2026-08-18)

結論は**要修正**である。実測を根拠に、日常のsyncから恒久的な取得失敗を外し、`--force-fetch`を復旧経路とする方針は妥当である。一方、現行コードでは404の`http_status`が保存層まで到達せず、成功扱いの304も失敗状態を解除しない。このままでは主機構の状態遷移が実装者の推測に任される。指摘1〜5を仕様で解消するまで最終案へ進めない。

また、「判断の指針」を`AGENTS.md`で将来の規約の優先順位に使うことは、本仕様の技術設計を超えてリポジトリ全体へ効く。指摘6だけは人間の明示承認が必要である。

| 草案の提案 | 採否 | 理由 |
|---|---|---|
| 連続失敗回数による指数backoff | 修正して採用 | 日常の通信量を減らしつつ一時障害を定期的に再試行できる。ただし成功・失敗・304・移行行の状態遷移を補う必要がある |
| 404と410を終端ステータスとする | 修正して採用 | 実測上の利益は大きいが、HTTPステータスの伝搬経路と設定反映を仕様化しないと機能しない |
| 既存warningからのbackfillは行わない | 採用 | 自由文解析をmigrationに持ち込まず、一度だけの再取得で構造化された状態へ移れる |
| quickとfullの双方で抑制し、`--force-fetch`で無視する | 採用 | 既存の`should_fetch_resource(force=...)`境界に置けばモード間の重複を避けられる |
| 判断の指針を`README.md`と`AGENTS.md`へ記録する | 保留 | `README.md`の用途説明は妥当。`AGENTS.md`で他の規約の解釈基準にするには、適用限界を含む文面を人間が承認する必要がある |

#### 指摘1: 404のHTTPステータスが保存経路へ渡らない — 重大度: 高

草案は`PageFetchResult.http_status`が算出済みとし（草案`:50`）、`record_failed_fetch`へ渡して終端ステータスを保存するとしている（`:118`、`:169-170`）。しかし現行の`fetch_page_text`は304と正常responseでは`http_status`を設定する一方、直接のHTTP errorは`PageFetchResult(url=..., error=f"HTTP {exc.code}")`だけを返す（`feedian/extract.py:254-281`）。また`_store_page`は`page.http_status`を`record_failed_fetch`へ渡していない（`feedian/sync.py:377-386`）。

このまま実装すると、最大の対象で1,885件ある404がhttp_status=NULLとして記録され、機構Bがほぼ効かない。HTTP errorの全return経路でステータスを`PageFetchResult`へ残し、`_store_page`から成功・失敗の両方の保存メソッドへ渡すことを書く必要がある。ブラウザfallbackを行う401、403、406は、fallbackが成功した場合と失敗した場合のどちらのstatusを最終結果とするかも定めること。

**採否: 修正して採用。** HTTP statusの構造化は採るが、現行コードに存在しない伝搬経路を既存と見なす説明は修正する。

#### 指摘2: 304が連続失敗状態をリセットしない — 重大度: 中

草案は「成功時（非空本文のrevisionを記録したとき）に0へリセット」とし（`:112`）、`record_not_modified_fetch`は変更しないとしている（`:171`）。しかし304は、保持中の本文がサーバー上の最新状態と同じだと確認できた成功である。現行メソッドはURL、validator、`fetched_at`だけを更新し、warningや新設予定の失敗回数・statusは触らない（`feedian/store.py:853-872`）。

成功本文→一時的な取得失敗→304と遷移したとき、失敗回数とwarningを残すと「連続失敗」ではない回数が次回のbackoffへ持ち越される。`record_not_modified_fetch`も304という成功遷移として、`consecutive_failures=0`、warningの解除、終端でないHTTP statusへの更新を行うこと。この遷移の回帰テストも追加する必要がある。

**採否: 修正して採用。** 成功時resetは採るが、成功を新規revision作成だけに限定する定義は採らない。

#### 指摘3: migrationで追加した失敗回数0の待機規則が未定義 — 重大度: 中

指数backoffの式は`base * 2^(n-1)`であり（`:98`）、定義域は連続失敗回数1以上である。一方、schema 7の全既存行は`consecutive_failures=0`へ移行し（`:152`）、草案は「移行後の最初の1回は従来どおり全件を取得」とする。しかし`n=0`の既存warning行を即時dueにするのか、現行の30分を待つのかが書かれていない。式をそのまま適用すると半分の15分という意図しない値になる。

草案の移行方針どおりにするなら、「本文が無く、既存warningがあり、`consecutive_failures=0`の移行行は即時due」と明記すること。本文がある移行行は従来どおり`refresh_days`に従うことも対で書き、migration直後の回帰テストを追加する必要がある。

**採否: 修正して採用。** backfillを行わない判断は採るが、0という互換値の読み方を仕様で固定する。

#### 指摘4: 設定値の検証契約が自己矛盾している — 重大度: 低

草案は`terminal_http_statuses`に空リストを指定すると機構Bを無効化できるとする一方（`:181`）、続けて「いずれも1以上の整数として検証」としている（`:183`）。後者は整数キー2つには適用できるが、リストには適用できない。

`retry_base_minutes`と`retry_max_days`は`bool`を含まない1以上の整数、`terminal_http_statuses`は空を許す整数のJSON arrayとし、各要素の許容範囲、重複の扱い、`bool`の拒否を個別に定義すること。不正値を拒否するテストも検証項目へ加える必要がある。

**採否: 修正して採用。** 設定で調整可能にする提案は採るが、一括の整数検証は採らない。

#### 指摘5: `feedian status`の「再試行しないresource数」の定義が足りない — 重大度: 低

草案は終端ステータスのresource数を`feedian status`に表示するとする（`:148`）が、`terminal_http_statuses`はVaultごとに変更でき、空にもできる。現行の`status_counts()`はconfigを受け取らず、各tableの全行数を返すだけである（`feedian/store.py:729-735`）。

表示値は「現在のVault configの`terminal_http_statuses`に含まれ、最新captureが失敗状態にあり、`removed_at IS NULL`のresource数」と定義し、空リストなら0になることを固定すること。CLI層がconfigを渡すのか、storeの専用queryがstatus集合を受け取るのかは実装詳細でよい。

**採否: 修正して採用。** 可視性の追加は採るが、設定と無関係な固定集計にはしない。

#### 指摘6: `AGENTS.md`の判断指針はリポジトリ全体の人間判断である — 重大度: 中

草案は「日常での実用性」を、本仕様の根拠にするだけでなく、`AGENTS.md`で「規約より前」に置き「規約同士の優先順位」を決めるとしている（`:54-65`）。これはfetch retryの設計判断ではなく、将来の仕様・レビュー・不具合判定の全てへ波及するプロダクトガバナンスである。実際に現在の作業ツリーにも`AGENTS.md`と`README.md`の変更があるが、まだ本仕様は草案である。

この指針を全体規約へ昇格するかは人間が明示的に決定する必要がある。採用する場合も、「明示された要件、確定仕様、データ保全の規約を上書きせず、それらで決まらないトレードオフのみに使う」という適用限界を明記すること。`README.md`のScopeとして用途を説明することは、このガバナンス判断と分離して採用できる。

**採否: 保留。** レビュー者の判断でリポジトリ全体の優先原則には昇格しない。人間の採否と理由を次のレビューに記録する。

#### 指摘6への人間の決定（2026-08-18）

**採用。** 「日常の実用性」を規約より上位の判断指針として`AGENTS.md`に置くことは、人間の明示指示である。したがって指摘6の保留は解消し、現在の作業ツリーにある`AGENTS.md`と`README.md`の方向性を採用する。決定理由は、日常の実用性を本プロダクト全体のトレードオフ判断に使うという要件所有者の意思による。

この決定により、最終案へ進む前の残件は技術上の指摘1〜5となる。

### レビュー2 — Claude Code (2026-08-18)

レビュー1の指摘1〜5を実コードへ照合した。**5件とも事実であり、すべて採用する。** 指摘6は人間の決定により採用済みで、あわせて提示された適用限界の明記も採る。

指摘1は当方の草案の記述誤りに起因する。**このまま実装すると本仕様の中核である機構Bが一切機能しない**ため、最終案では必ず解消すること。

#### レビュー1の採否

| 指摘 | 重大度 | 採否 | 検証結果 |
|---|---|---|---|
| 1. 404のHTTPステータスが保存経路へ渡らない | 高 | 採用 | 事実。詳細は下記 |
| 2. 304が連続失敗状態をリセットしない | 中 | 採用 | `record_not_modified_fetch`は`final_url`・validator・`fetched_at`のみ更新し、`warning`に触れない（`feedian/store.py:853-872`）。指摘のとおり |
| 3. 移行行の`consecutive_failures=0`の待機規則が未定義 | 中 | 採用 | 草案は`base * 2^(n-1)`の定義域をn≥1としか書いておらず、n=0を素直に代入すると15分になる。仕様の欠落 |
| 4. 設定値の検証契約が自己矛盾 | 低 | 採用 | 草案は`terminal_http_statuses`に空リストを許すと書きながら「いずれも1以上の整数として検証」と続けている。リストに適用できない |
| 5. `feedian status`の集計定義が不足 | 低 | 採用 | `status_counts(self)`はconfigを受け取らない（`feedian/store.py:729`）。設定依存の集計は現行の署名では表現できない |
| 6. `AGENTS.md`の指針はリポジトリ全体の人間判断 | 中 | 採用済み（人間の決定） | 適用限界の明記も採る。下記 |

#### 指摘1の確認と、草案の誤りの訂正

草案は「`PageFetchResult.http_status`は算出されている（`feedian/extract.py:44`、`296`、`307`、`317`）」と書いた。**この引用は誤りである。** 挙げた行はいずれも成功経路であり、失敗経路には`http_status`が無い。

| `feedian/extract.py` | 経路 | `http_status` |
|---|---|---|
| `:223` | blocked URL（DNS解決不能を含む） | **無し** |
| `:276` | 401/403/406でbrowser fallbackも失敗 | **無し** |
| `:281` | その他のHTTP error（**404はここ**） | **無し** |
| `:283` | URLError（タイムアウト等） | **無し** |
| `:285` | catch-all | **無し** |
| `:257` | 304 | 有り |
| `:296`、`:307`、`:317` | 成功・unsupported content type等 | 有り |

さらに`_store_page`は`page.http_status`を`record_failed_fetch`へ渡していない（`feedian/sync.py`の失敗分岐）。

したがって草案のまま実装すると、**対象Vaultの1,885件の404はすべて`http_status = NULL`として記録され、機構Bの終端判定は1件も成立しない。** 機構Aだけが動く状態になり、本仕様が解こうとした「移行後2回目以降も30分かかる」問題が残る。

**最終案での解消方針**

1. `fetch_page_text`のHTTP起因の失敗returnへ`http_status`を設定する。対象は`:281`（その他のHTTP error）と`:276`（browser fallback失敗）。
2. HTTP以外の失敗（`:223` blocked URL、`:283` URLError、`:285` catch-all）は`http_status`をNULLのままとする。これらは機構Aが扱う（「指針を当てた整理」の2に対応）。
3. `_store_page`は`page.http_status`を`record_failed_fetch`と`record_resource_revision`の双方へ渡す。
4. **browser fallbackの最終statusを定める。** fallbackが失敗した場合は**元のstatus（401/403/406）を保存する** — 失敗の原因を示すのは元のstatusであり、fallback側の内部statusではない。fallbackが成功した場合は本文が得られているため失敗判定に関与せず、`consecutive_failures`は0へリセットされる。

#### 指摘2の確認 — 到達可能性

Codexの指摘する遷移は実際に到達可能である。`resource_fetch_validators`は非空の本文を保持しているときだけvalidatorを返すため（前仕様のレビュー対応で追加）、304は本文を持つresourceでのみ発生する。したがって「成功 → 一時的な取得失敗 → 304」の順で、失敗回数1が解消されないまま残る。

**最終案での解消方針** — `record_not_modified_fetch`を成功遷移として扱い、`consecutive_failures = 0`、`warning = NULL`、`http_status = 304`を書く。304は終端ステータスに含めない。

#### 指摘3の解消方針

移行行（`consecutive_failures = 0`）の読み方を次のとおり固定する。指数式は適用しない。

| 移行行の状態 | 判定 |
|---|---|
| 本文が無く、既存の`warning`がある | **即座にdue**（従来の30分ルールも適用しない） |
| 本文がある | 従来どおり`refresh_days`に従う |

これにより草案の「移行後の最初の1回は従来どおり全件を取得する」が実装可能な形になる。その1回で`http_status`と失敗回数が記録され、以降は機構AとBが効く。

#### 指摘4の解消方針

検証を種類ごとに分ける。

| キー | 型 | 検証 |
|---|---|---|
| `retry_base_minutes` | 整数 | `bool`を拒否し、1以上 |
| `retry_max_days` | 整数 | `bool`を拒否し、1以上 |
| `terminal_http_statuses` | 整数の配列 | 空を許す。各要素は`bool`を拒否し100以上599以下。重複は排除する |

#### 指摘5の解消方針

`feedian status`が表示する値を次のとおり定義する。

> 現在のVault configの`terminal_http_statuses`に含まれる`http_status`を最新captureに持ち、本文が無く、`removed_at IS NULL`のresource数

`terminal_http_statuses`が空なら0になる。`status_counts()`の署名は変えず、専用のqueryを追加してCLI層からstatus集合を渡す。

#### 指摘6 — 人間の決定を受けての対応

人間の決定により`AGENTS.md`への記載は採用済みである。あわせてCodexが求めた**適用限界の明記を採る。**

指針が「規約より前」に置かれる以上、それが何を上書きしないのかを書かないと、明示された要件や確定仕様を「実用性」の名目で覆す口実になり得る。作業ツリーの`AGENTS.md`には既にデータ保全に関する限界（`This never licenses losing or corrupting stored data.`）を書いてあるが、要件と確定仕様への言及が無い。次を追加する。

> 明示された要件、確定した仕様、データ保全の規約を上書きしない。それらで決まらないトレードオフにのみ用いる。

`README.md`の`## Scope`は利用者向けの用途説明であり、この限界の記述は不要である。

#### 最終案へ反映すべき差分

1. `fetch_page_text`のHTTP起因の失敗returnへ`http_status`を設定し、`_store_page`から保存メソッドへ渡す。browser fallback失敗時は元のstatusを保存する（指摘1）。
2. `record_not_modified_fetch`を成功遷移として`consecutive_failures`・`warning`・`http_status`を更新する（指摘2）。
3. 移行行（失敗回数0）の待機規則を「本文なし＋warningあり＝即座にdue」と明記する（指摘3）。
4. 設定値の検証を型ごとに分ける（指摘4）。
5. `feedian status`の集計定義を設定依存として明記する（指摘5）。
6. `AGENTS.md`の指針へ適用限界を追記する（指摘6）。
7. 草案の`http_status`に関する記述（成功経路の行番号を根拠に「算出済み」としている箇所）は誤りであり、最終案では上記1の内容へ置き換える。
8. 検証項目へ追加する — 404の失敗で`http_status = 404`が保存されること、DNS解決不能でNULLになること、browser fallback失敗で元のstatusが保存されること、304が失敗回数とwarningを解除すること、移行直後の失敗回数0の行が即座にdueになること、設定値の不正な型が拒否されること。

#### 残る判断事項

無い。指摘1〜5は技術的に解消方針が定まり、指摘6は人間の決定が出ている。最終案を書ける状態である。
