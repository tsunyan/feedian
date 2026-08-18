# 到達不能ホストの取得コスト

ステータス: 確定

## 最終案

サービス終了したドメインと応答しないホストへの取得を、日常の`feedian sync`から恒久的に取り除く。手段は2つである。**失敗種別（`failure_kind`）を構造化して終端判定に使うこと**と、**取得タイムアウトを設定可能にして既定を30秒から5秒へ下げること**である。

草案の「目的と非目的」はそのまま維持する。並列化は扱わず、失敗種別の網羅的な分類も行わない。記録する種別は`"dns"`と`"timeout"`の2つだけである。

草案からの主な変更は3点である。レビュー1〜4を経て、(1) DNSとタイムアウトで異なる終端条件を持つ設計を**1本の規則へ統合**し、(2) browser用タイムアウトの伝播先を**2経路**とし、(3) 移行時に`consecutive_failures`をリセットする操作を追加した。

### A. 到達しないホストを終端扱いにする

**失敗種別。** `PageFetchResult`へ`failure_kind: str | None`を追加し、`fetch_capture`へ同名の列を追加する。取り得る値は`"dns"`・`"timeout"`・NULLのみである。SSL失敗も抽出失敗も種別を持たず、NULLのまま機構A（指数backoff）に任せる。

**`"dns"`の判定。** `validate_fetch_url`のDNS分岐に専用の例外型を導入する。

```python
# feedian/extract.py
class UnresolvableHostError(ValueError):
    """The hostname does not resolve. Distinct from the SSRF guard's rejection."""
```

`ValueError`のサブクラスとするため、既存の`except ValueError`はすべてそのまま動作する。`socket.gaierror`は**errnoで分類せず、すべて**この型へ変換する。一時的な名前解決失敗（`EAI_AGAIN`相当）と名前不存在を区別しないのは、POSIXの`EAI_*`とWindowsの`WSAHOST_NOT_FOUND`(11001)・`WSATRY_AGAIN`(11002)・`WSANO_DATA`(11004)の対応が自明でなく、実測環境がWindowsであるためである。**分類を細かくするのではなく、判断を遅らせることで安全性を得る**（後述の連続回数条件）。

**`failure_kind="dns"`を立てるのは、`fetch_page_text`冒頭の事前検証（`feedian/extract.py:220-223`）だけである。** `validate_fetch_url`は他に3箇所から呼ばれるが、いずれもNULLのままとする。

| 呼び出し元 | 扱い | 理由 |
|---|---|---|
| `fetch_page_text`の事前検証（`:221`） | **`"dns"`** | resource自身のホストが解決しないという事実 |
| `SafeRedirectHandler`（`:78`） | NULL | リダイレクト先1ホップの死でresourceを恒久的に切らない。`opener.open`の内側で送出され`except Exception`へ落ちる |
| browserのroute検証（`:739`） | NULL | 同上。`route.abort()`で処理済み |
| browserの最終URL検証（`:753`） | NULL | 同上 |

実装時、`UnresolvableHostError`を`try`ブロック全体で捕捉して`"dns"`を立ててはならない。リダイレクト経路の意味が変わる。

**`"timeout"`の判定。** 原因の例外型で判定し、**メッセージ文字列は見ない**。判定箇所は2つである。`opener.open`（`feedian/extract.py:241`）と`response.read`（`:245`）は同じ`try`ブロックにあるが、前者のタイムアウトは`URLError`に包まれ、後者は`TimeoutError`が直接届くためである。

- `except URLError as exc:` — `exc.reason`が`TimeoutError`のインスタンスなら`"timeout"`
- `except Exception as exc:` — `exc`自体が`TimeoutError`のインスタンスなら`"timeout"`

`socket.timeout`はPython 3.10以降`TimeoutError`の別名であるため、判定は`TimeoutError`のみでよい。実測した`WinError 10060`もこの型で届く。

**抑制規則。** `should_fetch_resource`の失敗分岐を次のとおりとする。

```
4. 失敗状態:
   1. http_status が terminal_http_statuses に含まれる → 取得しない
   2. failure_kind が terminal_failure_kinds に含まれ、
      consecutive_failures >= terminal_kind_failures → 取得しない          ← 追加
   3. consecutive_failures が 0 → 取得する（schema 7 からの移行行）
   4. それ以外 → backoff
```

条件2の意味は「**種類を問わない連続取得失敗が閾値以上で、かつ最新の失敗の種別が終端種別である**」である。連続DNS失敗回数でも連続タイムアウト回数でもない。既存の`consecutive_failures`をそのまま使い、種別別カウンタは追加しない。

DNSとタイムアウトを同じ条件にするのは、**「3回連続で取得に失敗したresourceは、種別が何であれ日常のsyncから外してよい」**という判断による。草案の「指針を当てた整理」でDNSを1回・タイムアウトを連続3回と分けた判断は、本判断で置き換わる。

`terminal_failure_count`も同じ条件へ揃える。集計は**HTTP status条件とfailure kind条件の和集合**であり、片方の設定が空でも他方を数える。両方が空のときにのみ0を返す。`feedian status`の`unreachable:`が実際の挙動と一致しなければならない。

**復旧経路。** 終端ステータスと同じく`--force-fetch`のみとする。新しいフラグは追加しない。

### B. 取得タイムアウトを設定可能にし、既定を下げる

`fetch.timeout_seconds`を追加し、`feedian/sync.py`の2箇所（item loopと(B1)pass）のハードコードを置き換える。**既定値を30から5へ下げる。**

現行の30秒は一度も発火していない。OS側が21秒でTCP接続を断念するためである。5秒にすればこちらが先に発火し、接続タイムアウト38件で約10分が削れる。

**5秒が何を意味するか。** Pythonのsocketタイムアウトは**1回のブロッキング操作ごとの上限**であり、通信全体の上限ではない。したがってconnectと各recvがそれぞれ独立に判定される。**総ダウンロード時間には上限がない**ため、低速でも着実に流れている大きなページは通る。切れるのは「5秒間まったく進まない」ホストだけである。

**browser経路は`fetch.browser_timeout_seconds`（既定30）を使う。** ブラウザの起動とレンダリングを含むため、5秒では正常な取得まで切ってしまう。伝播先は**2経路あり、両方へ渡す**。

1. 401/403/406のフォールバック `fetch_page_text_with_browser`（`feedian/extract.py:266-274`）
2. 静的HTMLの本文品質が低い場合の `render_html_with_browser`（`feedian/extract.py:349-355`）

2を落とすと、SPAなど正常な200応答から描画へ回る経路が5秒で切られる。片方だけの修正は採らない。

**引数は`fetch_page_text`の末尾へ`browser_timeout_seconds: int = 30`として追加する。** 既存の呼び出しは`feedian/sync.py`の2箇所のほか`feedian/__main__.py:635`・`:896`・`:1336`にあり、テストには位置引数の呼び出しがある。必須引数にするとこれらをすべて壊す。既定値を持たせ、**Vault同期だけが設定値を明示的に渡す契約**とすることで、本仕様の変更範囲をVault同期に保つ。

### C. 移行 — 既存の `consecutive_failures` をリセットする

**この操作は要件所有者の判断（2026-08-19）である。** レビュー3の指摘6が「一時DNS障害による恒久化は統合規則では解消せず、要件所有者が明示的に受け入れるか判断する必要がある」と保留した論点に対し、リスクを受け入れるのではなく**移行操作で閉じる**という結論を人間が選んだ。エージェントの提案ではない。

schema 8→9の移行を次の2文とする。

```sql
ALTER TABLE fetch_capture ADD COLUMN failure_kind TEXT;
UPDATE fetch_capture SET consecutive_failures = 1 WHERE consecutive_failures >= 2;
```

**理由は安全策ではなく、閾値の意味を揃えることである。** `terminal_kind_failures`は「新規則の下で観測した連続失敗の回数」として定義される。リセットしなければ、既存行だけが新規則の存在しなかった時代に稼いだ回数を持ち込み、新規に失敗し始めたresourceより早く終端に達する。同じ閾値が行によって違う意味を持つ状態は、説明も検証もできない。

結果として、レビュー1指摘1が挙げた事故も閉じる。移行対象の444件・38件は`consecutive_failures`が2〜3であり、リセットしなければ移行直後の一時的なDNS障害1回で終端に達し得た。リセット後、終端に達するには**移行後の失敗が2回**必要である。

**移行SQLは`fetched_at`を更新しない。** したがって移行直後の行は、最後の失敗から30分以上経っていれば即座にdueである（`feedian/store.py:889`、`:907-909`）。実際の時系列は次のとおりである。

| | 状態 | 判定 |
|---|---|---|
| 移行直後 | `consecutive_failures=1`、`fetched_at`は移行前のまま | 30分の待機は既に経過済みであることが多く、即due |
| 1回目の失敗 | `consecutive_failures=2`、`failure_kind`を記録、`fetched_at`更新 | 閾値未満なので終端にしない |
| 2回目の失敗（60分後以降） | `consecutive_failures=3` | |
| 次回の判定 | 閾値到達 | **終端** |

したがって本移行が保証するのは、**「一時的な障害1回では終端にならない」ことと「2回目の失敗まで60分以上空く」ことの2つだけ**である。「移行後3回の観測」や「常に90分以上」は保証しない。

より強い保証を作る手段はあるが、いずれも採らない。`fetched_at`を移行時刻へ更新すれば移行直後の30分待機も作れるが、**取得していない時刻を「最後に取得した時刻」として保存することになる。** `consecutive_failures`を0にすれば観測3回・実時間約90分になるが、取得が1回増えて約28分を追加で払ううえ、0は`should_fetch_resource`が「schema 7からの移行行＝回数未記録」と解釈する番兵と衝突する（`feedian/store.py:897-900`）。60分以上離れた失敗2回で単発事故は防げるため、これ以上は求めない。

**0ではなく1である。** `should_fetch_resource`は`consecutive_failures == 0`を「schema 7から移行した行なので1度取得して回数を得る」と解釈する（`feedian/store.py:897-900`）。0にすると680件すべてがbackoffを迂回して即座にdueとなり、移行直後に全件のフルコストを一度に払う。1ならbackoffの基準値（30分）に留まる。

**404/410の抑制は復活しない。** 終端ステータスの判定は`consecutive_failures`を参照せず`http_status`だけで決まる（`feedian/store.py:896`）。[前仕様](20260818-fetch-retry-suppression.ja.md)で日常から外れた分はそのまま外れたままである。

**代償は取得1回分である。** 1から3へ届くには失敗が2回要るため、終端到達までの取得がリセットなしの1回から2回になる。1回あたりDNS 211ホスト×7.2秒＋タイムアウト38件×5秒で約28分であり、**一度きり約28分**を追加で払う。恒久的な税（30日ごとに25分）の除去に対して見合う。副次的にSSL失敗46件などdns/timeout以外の失敗行もbackoffが30分へ戻り、数回のsyncのあいだ取得頻度が上がる（1回あたり5分程度）。

**この操作が安いのは今だけである。** backoff機構は前仕様で投入されたばかりで、`consecutive_failures`はまだ2〜3にしか育っていない。30日backoffまで育った行が存在する時期に同じリセットを行えば、それらをすべて日常へ引き戻すことになる。**将来この移行を先例として引かないこと。**

既存の`warning`から`hostname could not be resolved`を解析して`failure_kind`をbackfillする案は採らない。自由文の解析を移行へ持ち込まない。移行直後の`failure_kind`はすべてNULLであり、移行後の再取得で記録される。

### schema

SQLite schema versionを8から**9**へ上げる。`feedian migrate`による明示移行とする。

**migrationと`_create_schema`の両方へ列を追加する。** 現行の`fetch_capture`定義（`feedian/store.py:1403-1417`）は`http_status`の次が`fetched_at`であり、ここへ`failure_kind TEXT`を加える。migrationだけを変更すると、既存DBには列があるのに新規Vaultは`schema_version=9`で列を持たず、最初の`failure_kind`書き込みで`no such column`になる。**同じversionは同じtable構造を意味する**という契約は、`llm_run`について既に回帰テストで固定されている（`tests/test_store.py:570-605`）。`fetch_capture`にも同じ契約を適用する。

`failure_kind`の書き込み経路は`http_status`と同一とする。`record_failed_fetch`が渡された値を保存し、成功時と304時はNULLへ戻す。`http_status`と同じく**最新の失敗の状態**であり、保全対象ではない。**COALESCEしない。** DNS失敗の後にHTTP失敗が来たらNULLになる。古い失敗種別が残ると、現在の失敗と異なる理由で抑制され続ける。

### 設定

| キー | 既定 | 型 | 検証 |
|---|---|---|---|
| `terminal_failure_kinds` | `["dns", "timeout"]` | 文字列の配列 | 空を許す（機構を無効化）。要素は`"dns"`と`"timeout"`のみ許可。重複は排除する |
| `terminal_kind_failures` | 3 | 整数 | `bool`を拒否し、1以上 |
| `timeout_seconds` | 5 | 整数 | `bool`を拒否し、1以上 |
| `browser_timeout_seconds` | 30 | 整数 | `bool`を拒否し、1以上 |

`terminal_failure_kinds`の要素を既知の2値に限定するのは、綴り間違いを検出するためである。未知の値を静かに受理すると、誤りが「抑制されない」という形で現れて気づけない。

`fetch_retry_settings`（`feedian/vault.py:463`）は現在3要素のtupleを返し、`feedian/cli.py:241`と`feedian/sync.py:69`が位置で展開している。本仕様で5要素になるため、**frozen dataclassへ変更する**。位置展開のtupleが5要素になると、呼び出し側の取り違えを型が検出できない。

### 受け入れる不正確さ

- **移行後に3回連続で失敗し、その最後が一時的なDNS障害やタイムアウトだった行は終端になる。** 「HTTP 500 → SSL失敗 → 一時的なDNS障害」のような並びが該当する。`consecutive_failures`が種別を問わない以上、これは規則の定義そのものである。復旧は`--force-fetch`による。
- **初回応答に5秒以上かかるサイトは取りこぼす。** それを基準にして毎回のsyncが引きずられる不利益の方が大きいという判断である。`--full --force-fetch`で拾える。
- 上記はいずれも保存済みの本文を失わない。更新が止まるだけであり、`feedian status`の`unreachable:`に現れる。

### 検証

```
python -m pytest -q
```

**failure_kindの記録**

1. DNS解決不能で`failure_kind = "dns"`が保存される（`fetch_page_text`の事前検証経路）。
2. 私的アドレス拒否では`failure_kind`がNULLになる（SSRF防御とDNS死を混同しない）。
3. scheme違反・hostname無しでもNULLになる。
4. **リダイレクト先がDNS解決不能な場合はNULLになる。** 事前検証経路だけが`"dns"`を立てる。
5. `opener.open`のタイムアウトで`"timeout"`が保存される（`URLError.reason`の型で判定）。
6. `response.read`のタイムアウトで`"timeout"`が保存される（直接の`TimeoutError`）。
7. SSL失敗・HTTPエラーではNULLになる。
8. 成功時と304時に`failure_kind`がNULLへ戻る。
9. `failure_kind`はCOALESCEされない。DNS失敗の後にHTTP失敗が来たらNULLになる。

**抑制の判定**

10. `failure_kind`が終端種別で`consecutive_failures >= terminal_kind_failures`なら取得しない。`"dns"`と`"timeout"`の双方で検証する。
11. 閾値未満なら経過時間に応じて**再試行される**。双方で検証する。
12. `force=True`は`failure_kind`によらず取得する。
13. `terminal_failure_kinds`が空なら機構が無効になり、DNS失敗もbackoffに入る。`["dns"]`だけならタイムアウトは抑制されない。

**表示との一致**

14. `terminal_failure_count`が`failure_kind`による抑制も数え、`should_fetch_resource`と一致する。閾値に届いていない行は数えない。
15. `terminal_http_statuses=[]`かつ`terminal_failure_kinds=["dns"]`でDNS失敗を数える。その逆も数える。両方空なら0。

**タイムアウトの設定**

16. `fetch.timeout_seconds`が`fetch_page_text`へ渡る。`sync.py`にハードコードが残っていない。
17. `browser_timeout_seconds`が2経路（401/403/406、低品質HTML）の双方へ渡り、`timeout_seconds`ではない。引数省略時は30秒になる。
18. 4つの設定値それぞれの不正値（`1.5`・`True`・`"2"`・`0`・`-1`、および未知の種別文字列）が拒否される。

**移行**

19. schema 8から9への移行後、`consecutive_failures`が2以上だった行が1になる。0と1の行は変わらない。既存行の`failure_kind`はNULLである。
20. 移行後、`http_status`が404/410の行は依然として抑制される（終端ステータスは`consecutive_failures`に依存しない）。
21. 移行は`fetched_at`を更新しない。移行前の`fetched_at`から30分以上経っている行は、移行直後に即dueとなる。
22. 移行後の行は、1回のDNS失敗では終端にならない（`consecutive_failures`が2にしか達しないため）。2回目のDNS失敗で終端になる。
23. 新規DBとschema 8から9へ移行したDBの`PRAGMA table_info(fetch_capture)`が一致する（`llm_run`の同種テストに倣う）。
24. 新規DBでも`failure_kind`の記録、成功によるNULL化、304によるNULL化が動作する。

### 実装時の注意

実装コミットには`DESIGN.md`の要約更新を含め、本仕様へリンクする。`docs/specs/`は決定の理由を、`DESIGN.md`は現在の挙動を記す。

実装中に新たな判断が生じた場合は、まず草案の「判断の指針」を当てること。それでも決まらなければ本当の未決事項であり、仕様へ追記して合意を取る。

## 草案

### 背景 — 実測

[本文取得の再試行抑制](20260818-fetch-retry-suppression.ja.md) を投入した後の3回の実行である。

| run | fetched | 所要 | 1件あたり |
|---|---:|---:|---:|
| 1（移行直後） | 2,420 | 30分06秒 | 0.75秒 |
| 2 | 412 | 14分37秒 | 2.1秒 |
| 3 | 680 | 19分16秒 | 1.7秒 |

終端ステータスの抑制は効いている。取得件数は2,420から680へ減った。**しかし所要時間は比例して減らず、1件あたりはむしろ2〜3倍に悪化した。**

理由は残った候補の性質にある。404は即座にHTTPレスポンスが返るため速い。それを抑制した結果、**残ったのは遅いものばかり**になった。

対象Vaultの実URLに対して`fetch_page_text`を実測した結果を示す。

| カテゴリ | URL数 | 異なるホスト数 | 1件あたり実測 | 推定小計 |
|---|---:|---:|---|---:|
| 接続タイムアウト | 38 | 29 | **21.0秒**（2件とも21.0秒） | 13.3分 |
| DNS解決不能 | 444 | 211 | 7.2秒（キャッシュ済みは0.0秒） | — |
| 403/401 + browser fallback | 79 | — | 2.4秒 | 3.1分 |
| SSL失敗 | 46 | — | 0.2秒 | 0.2分 |
| その他 | 73 | — | 1.3秒 | 1.6分 |

**接続タイムアウトの38件だけで約13分**を占める。21.0秒はWindowsのTCP接続断念であり、現行の`timeout_seconds=30`より先に効いている。**こちらのタイムアウトは一度も発火していない。**

DNS解決不能は444件だがホストは211個で、負のキャッシュが効くためホスト単位のコストになる。上位は`headlines.yahoo.co.jp`（134件）、`japanese.engadget.com`（32件）、`matome.naver.jp`（28件）、`sankei.jp.msn.com`（8件）で、**いずれもサービス終了したドメイン**である。

### 現状の構造

前仕様は、ドメイン消滅を機構A（指数backoff）に任せると判断した。その理由は「`PageFetchResult`へ構造化した失敗種別を追加するのは変更範囲が大きい」「194件は数回の実行のうちに日常から外れる」であった。

**この判断は実測に照らして誤りである。**

- 件数は194ではなく**444件**であり、非終端候補680件の65%を占める。前仕様の見積もりは上位3ホストしか数えていなかった。
- 「数回の実行のうちに日常から外れる」は成立していない。現在の`consecutive_failures`は2〜3で、待機は1〜2時間である。2時間以上あけて実行すれば毎回フルコストを払う。
- backoffの上限は30日である。**211個の死んだホストは永久に消えず、30日ごとに25分の税を課し続ける。**

失敗理由が構造化されていないことが根本にある。`validate_fetch_url`は4つの異なる理由で同じ`ValueError`を投げる。

| 理由 | メッセージ |
|---|---|
| scheme違反 | `only http and https URLs are allowed` |
| hostname無し | `URL does not include a hostname` |
| **DNS解決不能** | `hostname could not be resolved: {hostname}` |
| 私的アドレス | `non-public address is not allowed: {address}` |

これらはすべて`error=f"blocked URL: {exc}"`となり、自由文でしか判別できない。**DNS解決不能と私的アドレス拒否はまったく性質が異なる**（前者は死んだホスト、後者はSSRF防御の作動）にもかかわらず、同じ形で記録されている。

タイムアウトについては、`timeout_seconds=30`が`feedian/sync.py`の2箇所（item loopと(B1)pass）に**ハードコード**されており、設定キーが存在しない。

### 判断の指針

[AGENTS.md「What Feedian is for」](../../AGENTS.md) に従う。日常での実用性を優先し、完全性は求めない。

本仕様における具体的な働きは次のとおりである。

- 死んだホストを**正確に判定すること**より、日常の`feedian sync`が**軽いこと**を優先する。
- ただし前仕様で「変更範囲が大きい」として退けた構造化を、今回は採る。**実測が判断を変えた。** 194件の推測に対しては割に合わなかったが、444件・時間の大半という実測に対しては見合う。
- 失敗種別は**今回抑制する2種類だけ**を記録する。あり得る失敗をすべて列挙しない。SSLも抽出失敗も種別を持たせない。

### 目的と非目的

**目的**

- サービス終了したドメイン、および応答を返さないホストへの取得を、日常の実行から恒久的に取り除く。
- 応答しないホストの1回あたりのコストを下げる。
- 取得タイムアウトを設定可能にする。現在ハードコードで、利用者が調整できない。

**非目的**

- **並列化は扱わない。** 別仕様とする。本仕様は「叩く回数と1回あたりのコストを減らす」ものであり、「同時に叩く」ものではない。性質が異なる変更であり、混ぜると評価もレビューも難しくなる。
- 失敗種別の網羅的な分類は行わない。SSL失敗と抽出失敗は種別を持たせず、機構A（backoff）に任せる。
- 私的アドレス拒否（SSRF防御）の扱いは変更しない。
- 終端ステータス（404/410）の扱いは変更しない。

### 設計案

#### A. 到達しないホストを終端扱いにする

**失敗種別の導入。** `PageFetchResult`へ`failure_kind: str | None`を追加し、`fetch_capture`へ同名の列を追加する。**記録する値は`"dns"`と`"timeout"`の2種類のみ**とし、それ以外の失敗はNULLのままとする。

判定経路は次のとおり。`validate_fetch_url`のDNS分岐に専用の例外型を導入する。

```python
# feedian/extract.py
class UnresolvableHostError(ValueError):
    """The hostname does not resolve. Distinct from the SSRF guard's rejection."""
```

`ValueError`のサブクラスとするため、既存の`except ValueError`はそのまま動作する。`fetch_page_text`のblocked URL分岐でこの型を判別し、`failure_kind="dns"`を設定する。私的アドレス拒否は従来どおりNULLである。

`"timeout"`は`URLError`の原因が`socket.timeout`（`TimeoutError`）である場合に設定する。**原因の例外型で判定し、メッセージ文字列は見ない。** 実測で観測した`WinError 10060`もこの型で届く。

**抑制。** `should_fetch_resource`の失敗分岐へ、終端ステータス判定と並べて種別判定を加える。

```
4. 失敗状態:
   1. http_status が terminal_http_statuses に含まれる → 取得しない
   2. failure_kind = "dns" かつ terminal_failure_kinds に含まれる → 取得しない        ← 追加
   3. failure_kind = "timeout" かつ terminal_failure_kinds に含まれ、
      consecutive_failures >= terminal_timeout_failures → 取得しない                ← 追加
   4. consecutive_failures が 0 → 取得する（移行行）
   5. それ以外 → backoff
```

`terminal_failure_count`も同じ条件へ揃える。`feedian status`の`unreachable:`が実際の挙動と一致していなければならない（前仕様のPRレビュー指摘5と同じ理由）。

**DNSとタイムアウトで条件を分ける理由。** DNSの解決失敗は決定的である。ホストが存在しないという事実であり、1回で判断してよい。一方タイムアウトは一時的な障害でも起こる。**1回の不調で生きているサイトを恒久的に切り捨てると、利用者はそれに気づけない**（黙って`unreachable`へ入るだけである）。連続回数を条件にすれば、この経路だけを塞げる。

既存の`consecutive_failures`をそのまま使うため、追加の状態は要らない。**現在の38件はすでに`consecutive_failures`が2〜3であり、既定値3なら次の実行で終端になる。** 速度改善は遅れない。

**復旧経路。** 終端ステータスと同じく`--force-fetch`のみとする。ドメインが復活した場合、あるいはサイトが遅いだけだった場合はこれで再取得できる。新しいフラグは追加しない。

#### B. 取得タイムアウトを設定可能にし、既定を下げる

`fetch.timeout_seconds`を追加し、`feedian/sync.py`のハードコードを置き換える。**既定値を30から5へ下げる。**

現行の30秒は一度も発火していない。OS側が21秒で断念するためである。5秒にすればこちらが先に発火し、38件×16秒＝**約10分が削れる**。

**5秒が何を意味するか。** Pythonのsocketタイムアウトは**1回のブロッキング操作ごとの上限**であり、通信全体の上限ではない。`opener.open(request, timeout=t)`は最終的に`sock.settimeout(t)`となり、connectと各recvがそれぞれ独立に判定される。したがって5秒は次を意味する。

- connectが5秒以内に完了すること
- リクエスト後、**最初の1バイトが5秒以内に返り始める**こと
- 本文の途中で5秒以上の無音が空かないこと

**総ダウンロード時間には上限がない。** 低速でも着実に流れている大きなページは通る。切れるのは「5秒間まったく進まない」ホストだけである。

障害中のホストや、初回応答に5秒以上かかるサイトは切り捨てられる。**それを基準にして毎回のsyncが引きずられる不利益の方が大きい**という判断である。取りこぼしは`--full --force-fetch`で拾える。

**browser fallbackは別のタイムアウトを持たせる。** `fetch.browser_timeout_seconds`（既定30）とする。ブラウザの起動とレンダリングを含む経路であり、5秒では正常な取得まで切ってしまう。

#### C. 既存データの扱い

移行直後、既存のcapture行は`failure_kind`がNULLである。DNS解決不能の444件と接続タイムアウトの38件は、**移行後の最初の1回で再取得され、そこで`failure_kind`が記録される。** その1回は従来どおりのコストがかかる（ただしタイムアウトは既に5秒へ下がっている）。以降、DNSは即座に、タイムアウトは`consecutive_failures`が既に2〜3であるため同じ回に終端となる。

既存の`warning`から`hostname could not be resolved`を解析してbackfillする案は採らない。前仕様と同じ理由である（自由文の解析を移行へ持ち込まない）。

### schema

SQLite schema versionを8から**9**へ上げる。`feedian migrate`による明示移行とする。

```sql
ALTER TABLE fetch_capture ADD COLUMN failure_kind TEXT;
```

書き込み経路は`http_status`と同一とする。`record_failed_fetch`が渡された値を保存し、成功時と304時はNULLへ戻す。`http_status`と同じく**最新の失敗の状態**であり、保全対象ではない（前仕様のレビュー指摘1と同じ理由。COALESCEしない）。

### 設定

| キー | 既定 | 型 | 検証 |
|---|---|---|---|
| `terminal_failure_kinds` | `["dns", "timeout"]` | 文字列の配列 | 空を許す（機構を無効化）。要素は`"dns"`と`"timeout"`のみ許可。重複は排除する |
| `terminal_timeout_failures` | 3 | 整数 | `bool`を拒否し、1以上 |
| `timeout_seconds` | 5 | 整数 | `bool`を拒否し、1以上 |
| `browser_timeout_seconds` | 30 | 整数 | `bool`を拒否し、1以上 |

`terminal_failure_kinds`の要素を既知の2値に限定するのは、綴り間違いを検出するためである。未知の値を静かに受理すると、誤りが「抑制されない」という形で現れて気づけない。

### 検証

```
python -m pytest -q
```

1. DNS解決不能の失敗で`failure_kind = "dns"`が保存される。
2. 私的アドレス拒否では`failure_kind`がNULLになる（SSRF防御とDNS死を混同しない）。
3. scheme違反・hostname無しでもNULLになる。
4. 接続タイムアウトで`failure_kind = "timeout"`が保存される。原因の型で判定し、メッセージ文字列に依存しない。
5. SSL失敗・HTTPエラーでは`failure_kind`がNULLになる。
6. `failure_kind = "dns"`の失敗は、`consecutive_failures`が1でも経過時間によらず再試行されない。
7. `failure_kind = "timeout"`は`consecutive_failures`が`terminal_timeout_failures`未満なら**再試行される**。到達したら再試行されない。
8. `force=True`は`failure_kind`によらず取得する。
9. `terminal_failure_kinds`が空なら機構が無効になり、DNS失敗もbackoffに入る。`["dns"]`だけならタイムアウトは抑制されない。
10. 成功時と304時に`failure_kind`がNULLへ戻る。
11. `failure_kind`はCOALESCEされない。DNS失敗の後にHTTP失敗が来たらNULLになる。
12. `terminal_failure_count`が`failure_kind`による抑制も数え、`should_fetch_resource`と一致する。連続回数に届いていないtimeoutは数えない。
13. `feedian status`の件数が`terminal_failure_kinds`の設定に依存する。
14. `fetch.timeout_seconds`が`fetch_page_text`へ渡る。`sync.py`にハードコードが残っていない。
15. browser fallbackへは`browser_timeout_seconds`が渡り、`timeout_seconds`ではない。
16. 4つの設定値それぞれの不正値（`1.5`・`True`・`"2"`・`0`・`-1`、および未知の種別文字列）が拒否される。
17. schema 8から9への移行後、既存行の`failure_kind`がNULLである。
18. 移行直後の`failure_kind`がNULLの失敗行は、`consecutive_failures`に応じたbackoffに従う（即座にdueにはならない。`consecutive_failures`は既に1以上のため）。

### 指針を当てた整理

草案作成時の未決事項3件は、要件所有者の判断（2026-08-19）で次のとおり定まった。

| # | 論点 | 結論 |
|---|---|---|
| 1 | browser fallbackのタイムアウトを分けるか | **分ける。** `browser_timeout_seconds`（既定30）。ブラウザ起動とレンダリングを含む経路を5秒で切ると、正常な取得まで失う |
| 2 | 接続タイムアウトも終端扱いにするか | **する。ただし連続`terminal_timeout_failures`回（既定3）を条件とする。** DNSと違い一時障害でも起こるため、1回で恒久的に切り捨てない |
| 3 | `timeout_seconds`の既定 | **5秒。** 操作単位の上限であり総ダウンロード時間は制限しない。障害中のホストや初回応答が遅いサイトを基準にして毎回のsyncが引きずられる不利益の方が大きい |

### 未決事項

なし。

実装中に新たな判断が生じた場合は、まず「判断の指針」を当てること。それでも決まらなければ本当の未決事項であり、仕様へ追記して合意を取る。

## レビュー

### レビュー1 — Codex (2026-08-19)

結論は**要修正**である。実測に基づいて、消滅したホストを日常のsyncから外し、1回の待ち時間も短縮する方針は妥当である。失敗種別を今回必要な2種類に限り、復旧経路を既存の`--force-fetch`へ寄せる設計も、変更範囲と日常の利益の釣り合いが取れている。

一方、DNSの失敗分類とタイムアウト回数の意味には、草案が述べる安全性を成立させない不一致がある。指摘1と2を解消し、例外経路と表示集計の境界を指摘3と4のとおり固定するまで、最終案へ進めない。

| 草案の提案 | 採否 | 理由 |
|---|---|---|
| DNS解決不能を1回で終端扱いする | 修正して採用 | NXDOMAINのような恒久的失敗には有効だが、現行の`socket.gaierror`一括捕捉では一時的なresolver障害まで含む |
| タイムアウトを既定3回で終端扱いする | 修正して採用 | 一時障害を1回で切らない方針は妥当。ただし既存の`consecutive_failures`はタイムアウトの連続回数ではない |
| `timeout_seconds`を5秒、browserを30秒に分ける | 採用 | HTTP取得の停止時間を下げつつ、起動と描画を含むbrowser経路を別枠にする境界は明確である |
| `failure_kind`を最新状態として保存し、成功・304でNULLへ戻す | 採用 | `http_status`と同じ状態遷移に揃い、古い失敗種別による誤抑制を防げる |
| 既存warningを解析せず、移行後の再取得で分類する | 採用 | 自由文解析をmigrationへ持ち込まず、既存の失敗回数を保ったまま構造化状態へ移れる |

#### 指摘1: 一時的なDNS障害まで1回で恒久抑制される — 重大度: 高

草案はDNS解決失敗を「ホストが存在しないという事実」として1回で終端にする（草案`:89-97`、`:115`）。しかし現行の`validate_fetch_url`は、`socket.getaddrinfo`が投げる**すべての**`socket.gaierror`を同じ`ValueError("hostname could not be resolved")`へ変換している（`feedian/extract.py:781-784`）。`socket.gaierror`には名前が存在しない場合だけでなく、`EAI_AGAIN`のような一時的な名前解決失敗も含まれる。

このまま専用例外へ型だけ置き換えると、端末側やDNS resolverの一時障害中に処理したresourceがすべて`failure_kind="dns"`となり、以後は`--force-fetch`まで再試行されない。単一サイトの取りこぼしではなく、一度の環境障害を多数の恒久状態として保存するため、日常の実用性にも反する。

専用例外を恒久的な名前不存在に限定し、少なくとも`EAI_AGAIN`は通常の一時失敗としてbackoffへ流すこと。どの`gaierror.errno`を`"dns"`とするかを仕様で列挙し、恒久ケースと一時ケースの回帰テストを分ける必要がある。環境差を避けてerrnoを限定できないなら、DNSにも連続回数条件を設ける案を選び、その判断を記録すること。

**採否: 修正して採用。** 消滅ドメインの終端化は採るが、`socket.gaierror`全体を消滅と同一視する分類は採らない。

#### 指摘2: `consecutive_failures`は連続タイムアウト回数ではない — 重大度: 中

草案は、一時障害を1回で恒久化しないため「連続`terminal_timeout_failures`回」を条件にすると説明する（草案`:107-115`、`:198`）。しかし現行の`record_failed_fetch`は失敗理由にかかわらず`consecutive_failures = consecutive_failures + 1`とする（`feedian/store.py:454-511`）。草案も`failure_kind`を最新の失敗だけを表す列としており、種類別の連続回数は持たない（草案`:153`）。

したがって「HTTP 500 → SSL失敗 → timeout」でも、最後のtimeoutは`consecutive_failures=3`となって即座に終端になる。移行後の既存行について「失敗回数が既に2〜3なので、最初に観測したtimeoutで同じ回に終端となる」とする説明（草案`:117`、`:141`）も、実際にはこの意味を積極的に利用している。これは「タイムアウトを3回観測してから切る」という安全性とは異なる。

次のどちらを仕様上の判断として明記する必要がある。

- 本当に連続タイムアウト回数を条件にするなら、種類別カウンタ、または失敗種別が変わったときに回数を1へ戻す状態遷移を追加する。
- 追加状態を避けるなら、条件を「連続取得失敗が閾値以上で、最新の失敗がtimeout」と正確に定義し、1回だけのtimeoutで終端になり得ることを受け入れる。その場合、名称、説明、検証項目7、移行時の説明も同じ意味へ揃える。

**採否: 修正して採用。** タイムアウトの終端化は採るが、既存カウンタを連続タイムアウト回数と呼ぶ説明は採らない。

#### 指摘3: 本文読み取り中の直接タイムアウトが分類対象から漏れる — 重大度: 中

草案は`URLError`の原因が`socket.timeout`の場合だけ`failure_kind="timeout"`にすると定める（草案`:99`）。現行のHTTP経路では`opener.open`だけでなく、同じtry block内の`response.read`もsocketタイムアウトの対象である（`feedian/extract.py:238-245`）。接続時の`OSError`は`URLError`へ包まれる一方、接続後の読み取りでは`TimeoutError`（`socket.timeout`）が直接届き、現行の汎用`except Exception`へ入る経路がある（`feedian/extract.py:283-286`）。

草案自身が5秒を「各recvで5秒以上の無音が空かないこと」と定義しているため（草案`:127-133`）、直接の`TimeoutError`を分類しなければ説明と保存状態が一致しない。`URLError.reason`の型判定と直接の`TimeoutError`の両方を対象にし、`opener.open`と`response.read`の2経路を別々に検証すること。メッセージ文字列を見ない方針はそのまま維持できる。

**採否: 修正して採用。** 型によるtimeout分類は採るが、`URLError`に包まれた経路だけへの限定は採らない。

#### 指摘4: HTTP終端設定が空のときの`unreachable`集計を固定する必要がある — 重大度: 低

草案は`terminal_failure_count`を`should_fetch_resource`と同じ条件へ揃え、`terminal_failure_kinds`の設定にも依存させる（草案`:113`、`:183-184`）。現行の`terminal_failure_count`は`terminal_http_statuses`が空なら即座に0を返す（`feedian/store.py:819-830`）。この早期returnを残したまま失敗種別の条件だけSQLへ加えると、HTTP終端を無効、DNS終端を有効にしたVaultで、実際には抑制されるDNS失敗が`unreachable: 0`と表示される。

集計はHTTP status条件とfailure kind条件の和集合であり、片方の設定が空でも他方を数える、と明記すること。検証項目へ「`terminal_http_statuses=[]`かつ`terminal_failure_kinds=["dns"]`でDNS失敗を数える」と、その逆、および両方が空なら0のケースを追加すれば、表示と取得判定の一致を固定できる。

**採否: 修正して採用。** 既存表示の拡張は採るが、HTTP側の空設定だけで全体を0にする挙動は採らない。

### レビュー2 — Claude Code (2026-08-19)

結論は**Codexの指摘4件をすべて採用**である。引用された`file:line`はすべて実コードで再現を確認した。事実誤認はない。

ただし指摘1と指摘2は、**個別にではなく統合して解消する**。Codexは両者に対してそれぞれ二択を提示しているが、どちらも「後者」を採ると規則が1本に畳まれ、草案が持っていたDNSとタイムアウトの非対称そのものが消える。前者どうし（errno列挙・種別別カウンタ）を採ると、機構が2つ増えたうえに指摘1と2が別々に残り続ける。

| Codexの指摘 | 採否 | 対応 |
|---|---|---|
| 1. 一時的DNS障害が1回で恒久抑制される | **採用** | errnoを列挙せず、DNSにも連続回数条件を課す。指摘2と統合 |
| 2. `consecutive_failures`は連続タイムアウト回数ではない | **採用** | 種別別カウンタを足さず、条件を正確に定義し直す。指摘1と統合 |
| 3. `response.read`の直接`TimeoutError`が漏れる | **採用** | `except Exception`分岐でも型判定する |
| 4. HTTP終端が空のとき`unreachable`が0になる | **採用** | 早期returnを外し、2条件の和集合として数える |

#### 指摘1・2への対応 — 終端条件を1本に畳む

草案の2条件（DNSは1回、タイムアウトは連続3回）を、次の1条件へ置き換える。

```
4. 失敗状態:
   1. http_status が terminal_http_statuses に含まれる → 取得しない
   2. failure_kind が terminal_failure_kinds に含まれ、
      consecutive_failures >= terminal_kind_failures → 取得しない          ← 草案の 2. と 3. を統合
   3. consecutive_failures が 0 → 取得する（移行行）
   4. それ以外 → backoff
```

条件の意味は「**連続取得失敗が閾値以上で、かつ最新の失敗の種別が終端種別である**」である。連続DNS失敗回数でも連続タイムアウト回数でもない。指摘2が求めた「名称・説明・検証項目を同じ意味へ揃える」を、この定義で満たす。

**指摘1がこれで消える理由。** 一時的なresolver障害は、閾値（既定3）に届くまで恒久化しない。実行間隔はbackoffにより時間単位で開くため、3回連続で同じ障害を踏むのは環境が実際に壊れている場合に限られる。`socket.gaierror`のerrnoを`EAI_AGAIN`と`EAI_NONAME`へ分類する必要がなくなり、`validate_fetch_url`は全`gaierror`を`UnresolvableHostError`にしてよい。

errno列挙を採らないのは、Codex自身が指摘の末尾で認めた環境差が実在するためである。POSIXの`EAI_*`に対しWindowsのgetaddrinfoは`WSAHOST_NOT_FOUND`(11001)・`WSATRY_AGAIN`(11002)・`WSANO_DATA`(11004)を返し、`socket.EAI_NONAME`との対応は自明でない。実測環境はWindowsであり、`AGENTS.md`の「最後の数%を閉じる機構を作らない」に正面から反する。**分類を細かくするのではなく、判断を遅らせることで安全性を得る。**

**失うもの。** 「新規に消えたドメインを1回で切る」性質は失われ、タイムアウトと同じ扱いになる。**速度改善は遅れない。** 実測の444件・38件はいずれも`consecutive_failures`が既に2〜3であり、既定3なら移行後1〜2回の実行で終端に届く。恒久的な税（30日ごとに25分）の除去という本仕様の目的は、そのまま達成される。

**受け入れる不正確さ。** 「HTTP 500 → SSL失敗 → DNS失敗」の順で失敗したresourceは、DNS失敗1回で終端になる。これは指摘2が示した二択の後者そのものであり、意図的に受け入れる。3回連続で失敗しているresourceは、種別が何であれ日常のsyncから外してよいという判断である。復旧経路は`--force-fetch`で変わらない。

#### 指摘3への対応

`opener.open`（`feedian/extract.py:241`）と`response.read`（`:245`）は同じtry blockにあり、後者のタイムアウトは`URLError`に包まれず`except Exception`（`:285-286`）へ直接届く。したがって分類は2箇所で行う。

- `except URLError as exc:` — `exc.reason`が`TimeoutError`のインスタンスなら`"timeout"`
- `except Exception as exc:` — `exc`自体が`TimeoutError`のインスタンスなら`"timeout"`

`socket.timeout`はPython 3.10以降`TimeoutError`の別名であるため、判定は`TimeoutError`のみでよい。メッセージ文字列を見ない方針は維持される。検証項目は`opener.open`側と`response.read`側を別々に立てる。

#### 指摘4への対応

`terminal_failure_count`（`feedian/store.py:819-830`）の早期returnを外し、集計を**HTTP status条件とfailure kind条件の和集合**とする。片方の設定が空でも他方を数え、両方が空のときにのみ0を返す。`should_fetch_resource`の失敗分岐と同じ形になる。

#### 指摘5: browser fallbackへタイムアウトが伝播していない — 重大度: 低

草案Bは`browser_timeout_seconds`（既定30）を分けると定めるが、現行の`fetch_page_text`は401/403/406のフォールバックで`fetch_page_text_with_browser(timeout_seconds=timeout_seconds)`と、**HTTP経路のタイムアウトをそのまま渡している**（`feedian/extract.py:271`）。設定を分けても、`fetch_page_text`が引数を1つしか受け取らなければ、browser経路は5秒で動くことになる。

`fetch_page_text`へ`browser_timeout_seconds`引数を追加し、`sync.py`から両方を渡すこと。実測で403/401 + browser fallbackは79件あり、5秒で切ると正常な取得まで失う。草案の意図どおりに動かすために必要な変更である。

#### 設定への影響

草案の`terminal_timeout_failures`を廃し、`terminal_kind_failures`とする。DNSとタイムアウトの双方に効くため、名称からtimeoutを外す。

| キー | 既定 | 型 | 検証 |
|---|---|---|---|
| `terminal_failure_kinds` | `["dns", "timeout"]` | 文字列の配列 | 草案どおり |
| `terminal_kind_failures` | 3 | 整数 | `bool`を拒否し、1以上 |
| `timeout_seconds` | 5 | 整数 | 草案どおり |
| `browser_timeout_seconds` | 30 | 整数 | 草案どおり |

なお`fetch_retry_settings`は現在3要素のtupleを返し、`feedian/cli.py:241`と`feedian/sync.py:69`が位置で展開している（`feedian/vault.py:463`）。本仕様で2つ増えて5要素になるため、**frozen dataclassへ変更する**こと。位置展開のtupleが5要素になると、呼び出し側の取り違えを型が検出できない。

#### 検証項目への影響

草案の検証項目のうち、次を差し替える。

- **6を削除**（DNSが`consecutive_failures=1`でも抑制される、は成立しない）。代わりに「`failure_kind="dns"`は`consecutive_failures`が`terminal_kind_failures`未満なら再試行される」を立てる。
- **7を一般化**し、種別をdnsとtimeoutの双方について同じ形で検証する。
- **12**の「連続回数に届いていないtimeoutは数えない」はdnsにも適用する。
- **4を2つに分割**し、`opener.open`のタイムアウトと`response.read`のタイムアウトを別々に検証する（指摘3）。
- **13へ追加** — `terminal_http_statuses=[]`かつ`terminal_failure_kinds=["dns"]`でDNS失敗を数える、その逆、両方空なら0（指摘4）。
- **15を強化** — browser fallbackが`browser_timeout_seconds`を受け取ることを、`fetch_page_text`の引数まで遡って検証する（指摘5）。
- **新規** — 一時的なDNS障害（`consecutive_failures=1`のDNS失敗）が終端にならず、backoffに従う。指摘1が防ごうとした事故そのものの回帰テストである。

### レビュー3 — Codex (2026-08-19)

結論は**要修正**である。レビュー2はレビュー1の指摘3と4を解消している。直接の`TimeoutError`を含む2経路の分類、およびHTTP statusとfailure kindの和集合による集計は、現行コードの分岐と一致する。指摘1と2を1本の規則へ統合し、`consecutive_failures`の意味を「種類を問わない連続取得失敗」と正しく定義し直した点も、追加状態を避ける設計判断として成立する。

ただし、その統合案は一時DNS障害による恒久化を**解消するのではなく、条件付きで受け入れる案**である。レビュー2内の安全性の説明と検証案は、この実際の条件へ揃っていない。また、レビュー2で新たに見つかったbrowser timeoutの伝播先が1経路不足している。次の2点を解消してから最終案へ進める必要がある。

| 対象 | 採否 | 理由 |
|---|---|---|
| レビュー1の指摘1・2を一般化した閾値へ統合する | 保留 | 規則自体は実装可能だが、一時DNS障害で既存失敗行を恒久化する挙動は要件所有者が明示的に受け入れる必要がある |
| レビュー1の指摘3への対応 | 採用 | `URLError.reason`と直接例外の両方を`TimeoutError`の型で判定できる |
| レビュー1の指摘4への対応 | 採用 | 片方の設定だけが空の場合を含め、取得判定と表示を一致させている |
| レビュー2の指摘5 | 修正して採用 | timeoutの分離は必要だが、401/403/406経路だけでなく低品質HTMLからのbrowser描画にも適用する必要がある |
| `fetch_retry_settings`をfrozen dataclassへ変更する | 採用 | 5個の設定値を位置で受け渡さず、名前で参照する方が取り違えを防げる |

#### 指摘6: 一時DNS障害の恒久化は解消されず、説明だけが解消済みとしている — 重大度: 中

レビュー2は条件を「種類を問わない連続取得失敗が閾値以上で、最新failure kindが終端種別」と正確に定義し（レビュー2`:277-288`）、「HTTP 500 → SSL失敗 → DNS失敗」でもDNS失敗1回で終端になることを意図的に受け入れている（`:296`）。この定義自体に曖昧さはない。

しかし同じ対応は、「一時的なresolver障害は閾値に届くまで恒久化しない」「3回連続で同じ障害を踏む場合に限られる」と説明している（`:290`）。これは定義と一致しない。特に本仕様の対象となる既存行は`consecutive_failures=2〜3`である（草案`:42`、レビュー2`:294`）。移行後の最初の実行でresolverが一時障害を起こせば、その**1回だけのDNS失敗**で多数の既存resourceが終端になり得る。レビュー1の指摘1が挙げた事故は、未失敗または失敗1回のresourceに対して狭まるだけで、既存の主対象については残る。

追加状態を避け、「3回連続で取得に失敗したresourceは、最新がDNSまたはtimeoutなら日常から外してよい」とする判断は、Feedianの指針から選択可能である。ただし一時DNS障害への防御として説明してはならない。レビュー2`:290`と検証案`:340`を、次の事実へ揃える必要がある。

- `consecutive_failures=1`のDNS失敗は終端にならない。
- `consecutive_failures=2`の行が次に一時DNS失敗すると終端になる。これは既知の不正確さとして受け入れる。
- その受け入れは、草案でDNSとtimeoutを分けた人間判断（草案`:193-199`）を変更するため、要件所有者が明示的に採否と理由を記録する。

**採否: 保留。** 統合規則を技術的に否定するものではないが、事故が解消したという説明では採用できない。残るリスクを正確に記録したうえで人間が決定する。

#### 指摘7: browser用タイムアウトの伝播先がもう1経路ある — 重大度: 中

レビュー2の指摘5は、401/403/406から`fetch_page_text_with_browser`へ入る経路（`feedian/extract.py:266-274`）だけを挙げている。しかしbrowser描画は、HTTP取得に成功した後、静的HTMLの本文品質が低い場合にも直接`render_html_with_browser`を呼ぶ（`feedian/extract.py:349-355`）。後者も現在はHTTP用の`timeout_seconds`を渡しているため、前者だけを直すとSPAなどの通常レスポンス経路は引き続き5秒で切られる。

`fetch_page_text`が受け取る`browser_timeout_seconds`は、次の両方へ渡すと明記すること。

1. 401/403/406の`fetch_page_text_with_browser`
2. 低品質な静的HTMLからの`render_html_with_browser`

また`fetch_page_text`はVault同期以外の既存経路からも呼ばれている（`feedian/__main__.py:635`、`:896`、`:1336`。テストにも位置引数の呼び出しがある）。新しい引数を必須にするとこれらをすべて壊す。関数末尾へ`browser_timeout_seconds: int = 30`を追加して既存呼び出しの挙動を保ち、Vault同期だけが設定値を明示的に渡す契約にするか、全呼び出し側を変更するかを固定すること。前者なら本仕様の変更範囲をVault同期へ保てる。

検証項目15は、401/403/406経路と低品質HTML経路の双方がbrowser用の値を受け取ること、および引数省略時に30秒となることへ広げる必要がある。

**採否: 修正して採用。** browser timeoutの分離は採るが、HTTP error経路だけを変更対象とする範囲は採らない。

### レビュー4 — Claude Code (2026-08-19)

指摘6・7をいずれも採用する。加えて、要件所有者の判断（2026-08-19）により**移行時に`consecutive_failures`をリセットする**を設計へ追加する。この追加が指摘6の残余の大半を閉じるため、先に述べる。

| 対象 | 採否 | 対応 |
|---|---|---|
| 指摘6（一時DNS障害の恒久化が残る） | **採用** | 統合規則は維持し、レビュー2`:290`の説明を訂正する。加えて移行時リセットで残余を狭める |
| 指摘7（browser timeoutの伝播先が1経路不足） | **採用** | 2経路へ渡す。引数は末尾の既定値付きとする |

#### 要件所有者の判断 — 統合規則を採用する

レビュー2の統合規則（連続失敗が`terminal_kind_failures`以上で、最新の失敗種別が終端種別）を採用する。「3回連続で取得に失敗したresourceは、種別が何であれ日常のsyncから外してよい」を明示的な判断として記録する。

草案の「指針を当てた整理」でDNSを1回、タイムアウトを連続3回と分けた判断は、本判断で置き換わる。種別別カウンタの追加（レビュー1指摘2の前者）とerrno分類（同指摘1の前者）は、いずれも採らない。`AGENTS.md`の「最後の数%を閉じる機構を作らない」に反するためである。

#### 移行時に`consecutive_failures`を1へ戻す

schema 8→9の移行で、次を実行する。

```sql
UPDATE fetch_capture SET consecutive_failures = 1 WHERE consecutive_failures >= 2;
```

**0ではなく1である。** `should_fetch_resource`は`consecutive_failures == 0`を「schema 7から移行した行なので1度取得して回数を得る」と解釈する（`feedian/store.py:897-900`）。0にすると680件すべてがbackoffを迂回して即座にdueとなり、移行直後に全件のフルコストを一度に払う。1ならbackoffの基準値（30分）に留まり、既存の意味も保たれる。

**404/410の抑制は復活しない。** 終端ステータスの判定は`consecutive_failures`を参照せず、`http_status`だけで決まる（`feedian/store.py:896`）。前仕様で日常から外れた1,740件はそのまま外れたままである。

**この操作の理由は安全策ではなく、閾値の意味を揃えることである。** `terminal_kind_failures`は「新規則の下で観測した連続失敗の回数」として定義される。リセットしなければ、既存行だけが新規則の存在しなかった時代に稼いだ回数を持ち込み、新規に失敗し始めたresourceより早く終端に達する。同じ閾値が行によって違う意味を持つ状態は、説明も検証もできない。

**代償は取得1回分である。**

| | 終端に届くまでの取得回数 |
|---|---|
| リセットなし（現在`consecutive_failures`=2〜3） | 1回 |
| 1へリセット | 2回 |

1から3へ届くには失敗が2回要るためである。1回あたりの費用はDNS 211ホスト×7.2秒＋タイムアウト38件×5秒で約28分であり、**一度きり約28分**を追加で払う。恒久的な税（30日ごとに25分）の除去という目的に対して見合う。

副次的に、SSL失敗46件・その他73件などdns/timeout以外の失敗行もbackoffが30分へ戻り、数回のsyncのあいだ取得頻度が上がる。1回あたり5分程度であり、回数を重ねれば元のbackoffへ戻る。

**この操作が安いのは今だけである。** backoff機構は[前仕様](20260818-fetch-retry-suppression.ja.md)で投入されたばかりで、`consecutive_failures`はまだ2〜3にしか育っていない。同じリセットを、30日backoffまで育った行が存在する時期に行えば、それらをすべて日常へ引き戻すことになる。**将来この移行を先例として引かないこと。**

#### 指摘6への対応 — レビュー2の説明を訂正する

レビュー2`:290`の「一時的なresolver障害は、閾値（既定3）に届くまで恒久化しない」は、規則としては正しいが、主対象の既存行が既に閾値の直下にあることを踏まえていない。Codexの指摘のとおりである。次を正とする。

- **統合規則だけでは、一時DNS障害による恒久化は解消しない。** レビュー1指摘1の事故は、未失敗または失敗1回のresourceに範囲が狭まるだけである。
- **移行時リセットを併せて初めて、既存行についても解消する。** リセット後、終端に達するには移行後の連続失敗が3回必要であり、backoffが30分・60分と開くため、実時間で1時間半以上にわたりresolverが失敗し続けた場合に限られる。
- **残る不正確さ。** 移行後に3回連続で失敗し、その最後が一時的なDNS障害だった行は終端になる。これはレビュー1指摘2で受け入れると決めた性質そのものであり、新しいリスクではない。復旧経路は`--force-fetch`である。

#### 指摘7への対応

browser描画の経路は2つある。`browser_timeout_seconds`は**両方**へ渡す。

1. 401/403/406のフォールバック（`feedian/extract.py:266-274`）
2. 静的HTMLの本文品質が低い場合の`render_html_with_browser`（`feedian/extract.py:349-355`）

2を落とすと、SPAなど正常な200応答から描画へ回る経路が5秒で切られる。実測の403/401 + browser fallbackが79件であるのに対し、2の経路は件数を測っていないが、Vault全体の取得成功経路に対して開いている。片方だけの修正は採らない。

**引数は`fetch_page_text`の末尾へ`browser_timeout_seconds: int = 30`として追加する。** 既存の呼び出しは`feedian/sync.py`の2箇所のほか`feedian/__main__.py:635`・`:896`・`:1336`にあり、テストには`fetch_page_text("https://example.com/article", 5, 1000)`のような位置引数の呼び出しがある。必須引数にするとこれらをすべて壊す。既定値を持たせ、Vault同期だけが設定値を明示的に渡す契約とすることで、本仕様の変更範囲をVault同期に保つ。

#### schema・検証項目への影響

schema 8→9の移行は次の2文となる。

```sql
ALTER TABLE fetch_capture ADD COLUMN failure_kind TEXT;
UPDATE fetch_capture SET consecutive_failures = 1 WHERE consecutive_failures >= 2;
```

草案の検証項目のうち、レビュー2で挙げた差し替えに加えて次を変更する。

- **17を拡張** — 移行後、`consecutive_failures`が2以上だった行が1になり、0と1の行は変わらない。`failure_kind`はNULLである。
- **18を差し替え** — 移行直後の行は`consecutive_failures=1`のbackoff（既定30分）に従い、即座にdueにはならない。
- **新規** — 移行で`http_status`が404/410の行が復活しないこと。終端ステータスの抑制は`consecutive_failures`に依存しない。
- **新規** — 移行後の行は、1回のDNS失敗では終端にならない（`consecutive_failures`が2にしか達しない）。レビュー2の「新規」項目はこちらへ統合する。
- **15を拡張**（指摘7）— 401/403/406経路と低品質HTML経路の双方がbrowser用の値を受け取る。引数省略時は30秒になる。

### レビュー5 — Codex (2026-08-19)

結論は**要修正**である。最終案はレビュー1〜4の技術的な指摘を概ね取り込み、終端条件、失敗種別の伝搬、browser timeoutの2経路、表示集計の和集合を実装可能な形へ揃えている。特に、移行時に既存の失敗回数を閾値直下から外すことで「既存行が一時DNS障害1回だけで終端になる」事故を防ぐ判断は成立する。

一方、移行後の時系列について、最終案が保証すると書いた回数・待機時間と、現行の`should_fetch_resource`が実際に使う状態に不一致がある。また、新規DBと移行DBを同じschema version 9として扱うための検証契約が不足している。この2点を解消するまで仕様単独コミットへ進めない。

| 対象 | 採否 | 理由 |
|---|---|---|
| `consecutive_failures >= 2`を1へ戻す移行 | 修正して採用 | 一時DNS障害1回での終端化は防げるが、`fetched_at`を保持する以上、即時dueにならないことや移行後3回の観測までは保証しない |
| 新規DBのschema 9 | 修正して採用 | migrationだけでなく`_create_schema`にも`failure_kind`が必要であり、両経路の同一性を検証しなければならない |

#### 指摘8: 失敗回数だけのリセットでは、記載された待機時間と観測回数にならない — 重大度: 中

最終案はschema 8→9で`consecutive_failures >= 2`を1へ戻し（最終案`:85-90`）、これにより「移行後の連続失敗が3回必要」「resolverが実時間で1時間半以上失敗し続けた場合に限られる」とする（`:92-96`）。検証項目21も、移行後の行は30分backoffに従い「即座にdueにはならない」と固定している（`:169-172`）。

しかし移行SQLは`fetched_at`を変更しない。現行の`should_fetch_resource`は、回数1なら待機を30分にするだけで、**既存の`fetched_at`から既に30分経過しているか**を判定する（`feedian/store.py:889`、`:907-909`）。したがって、移行時点で最後の失敗から30分以上経った行は直ちにdueである。最初の失敗で回数2になり、そこから60分後の2回目の失敗で回数3へ達して終端になる。実際の保証は次のとおりである。

- 一時DNS障害**1回だけ**では終端にならない。これは移行リセットにより保証される。
- 移行後に必要な失敗は3回ではなく**2回**である。移行で設定した1は、新規則の下で観測した失敗ではない。
- resolver障害が必要な実時間は、既存`fetched_at`の古さにより約60〜90分であり、常に90分以上ではない。
- 移行直後にdueかどうかは、回数だけでなく既存`fetched_at`の経過時間に依存する。

`fetched_at`を移行時刻へ更新すれば30分の待機は作れるが、取得していない時刻を「最後に取得した時刻」として保存するため採らない方がよい。現在の単純な移行を維持するなら、最終案`:92-100`、レビュー4`:583-607`、検証項目21を上記の実動作へ直すこと。どうしても「移行後3回の観測」または「常に90分以上」を要件にするなら、追加状態を含む別の設計判断が必要である。

**採否: 修正して採用。** 回数を1へ戻して単発事故を防ぐ移行は採るが、それを移行後3回の観測や固定の待機時間と説明する部分は採らない。

#### 指摘9: 新規DBがschema 9と同じ列を持つ検証がない — 重大度: 中

最終案は`fetch_capture.failure_kind`を追加し、schema versionを9へ上げる（最終案`:15`、`:106-110`）。しかし検証項目19はschema 8→9の移行だけを対象とし（`:167-170`）、新規DBを作る`_create_schema`の契約を固定していない。現行の`fetch_capture`定義は`http_status`の次が`fetched_at`であり（`feedian/store.py:1403-1417`）、実装ではここにも`failure_kind`を追加する必要がある。

migrationだけを変更すると、既存DBには列がある一方、新規Vaultは`schema_version=9`なのに列がない。同じversionでtable構造が分岐し、最初の`failure_kind`書き込みで`no such column`になる。既に`llm_run`には「freshとmigratedが同じ列を持つ」回帰テストがある（`tests/test_store.py:570-605`）ため、同じ契約を`fetch_capture`にも適用すること。

検証へ次を追加する必要がある。

1. 新規DBとschema 8→9移行DBの`PRAGMA table_info(fetch_capture)`が同じ列定義になる。
2. 新規DBでも`failure_kind`を伴う失敗記録、成功によるNULL化、304によるNULL化が動作する。

**採否: 修正して採用。** schema 9への列追加は採るが、移行経路だけを検証対象とする範囲は採らない。

### レビュー6 — Claude Code (2026-08-19)

指摘8・9をいずれも採用する。両方とも引用された`file:line`をコードで確認し、事実であることを検証した。**訂正は本節に留めず、最終案へ直接反映した。**

| Codexの指摘 | 採否 | 対応 |
|---|---|---|
| 8. 回数リセットだけでは記載の待機時間・観測回数にならない | **採用** | 最終案「C. 移行」の保証内容を実動作へ書き直し、検証項目21・22を差し替えた |
| 9. 新規DBがschema 9と同じ列を持つ検証がない | **採用** | 最終案「schema」へ`_create_schema`への列追加を明記し、検証項目23・24を追加した |

#### 確定後に最終案を修正した理由

確定した仕様は編集しないのが本リポジトリの規約である。しかし本文書はこの時点でまだ一度もコミットされていない。git上の記録になっていない以上、リンクも履歴も壊れず、既存のレビューが参照する対象も動かない。実装者が最初に読むのは最終案であり、そこに誤った観測回数と待機時間を残すことは、この文書の目的に正面から反する。**要件所有者の判断（2026-08-19）により、初回コミット前の訂正として最終案を直接修正した。**

草案とレビュー1〜5は一切改変していない。**レビュー4`:607`には指摘8が指摘したとおりの誤りが残っているが、これは訂正しない。** 追記のみとする規約に従う。誤った説明とその訂正の経緯が残ることに、ADRとしての価値がある。

コミット後に実装が仕様から逸脱した場合、その記録先は本節ではなく`docs/reviews/`である。最終案を直接修正できるのは、この初回コミットまでである。

#### 指摘8への対応 — 保証内容を実動作へ直した

移行SQLは`fetched_at`を更新しない。現行の`should_fetch_resource`は`consecutive_failures`から待機時間を決めたうえで、**既存の`fetched_at`からの経過時間**と比較する（`feedian/store.py:889`、`:907-909`）。移行対象の行は最後の失敗から数時間経っているため、リセット後の30分待機は既に満たされている。

したがって最終案`:94`（修正前）の「移行後の連続失敗が3回必要」「実時間で1時間半以上」および検証項目21の「即座にdueにはならない」は、いずれも成立しない。正しくは次のとおりで、最終案をこの内容へ書き直した。

- 移行後に必要な失敗は**2回**である。移行で設定した1は、新規則の下で観測した失敗ではない。
- 移行直後の行は、多くの場合**即dueである**。
- 保証されるのは「一時的な障害1回では終端にならない」ことと「2回目の失敗まで60分以上空く」ことの2つだけである。

**同じ文書内に正しい記述も存在していた。** 最終案の「代償は取得1回分である。1から3へ届くには失敗が2回要る」（`:100`）と、レビュー4の表（取得回数2回）は正しい。誤っていたのは安全性を説明した散文の側であり、設計そのものではない。指摘8は設計を否定していない。

**より強い保証を作る手段は検討のうえ採らなかった。** `fetched_at`を移行時刻へ更新すれば移行直後の30分待機も作れるが、取得していない時刻を「最後に取得した時刻」として保存することになる。`consecutive_failures`を0にすれば観測3回・実時間約90分になるが、取得が1回増えて約28分を追加で払ううえ、0は`should_fetch_resource`が「schema 7からの移行行＝回数未記録」と解釈する番兵と衝突する（`feedian/store.py:897-900`）。

要件所有者の判断は、**移行SQLを`= 1`のまま維持し、記述だけを実動作へ揃える**である。移行時リセットの目的は「一時DNS障害1回で既存行が終端になる」事故を防ぐことであり、60分以上離れた失敗2回という条件でそれは達成されている。3回目の観測を買うために追加の取得コストと番兵の意味の混濁を負うのは、`AGENTS.md`の「最後の数%を閉じる機構を作らない」に当たる。

#### 指摘9への対応

`_create_schema`の`fetch_capture`定義（`feedian/store.py:1403-1417`）は`http_status`の次が`fetched_at`であり、`failure_kind`を持たない。migrationだけを変更すると、既存DBには列があるのに新規Vaultは`schema_version=9`で列を持たず、最初の書き込みで`no such column`になる。同じversion番号が異なるtable構造を意味する状態は、そのまま本番の失敗になる。

同じ事故に対する回帰テストが`llm_run`について既に存在する（`tests/test_store.py:570-605`）。**repo内に踏襲すべき前例がありながら、検証項目から漏れていた。** 最終案のschema節へ両経路への列追加を明記し、検証項目へ次の2件を追加した。

1. 新規DBとschema 8から9へ移行したDBの`PRAGMA table_info(fetch_capture)`が一致する。
2. 新規DBでも`failure_kind`の記録、成功によるNULL化、304によるNULL化が動作する。

#### 最終案への反映箇所

| 節 | 変更 |
|---|---|
| C. 移行 | 「3回必要／1時間半以上」を削除し、時系列の表と保証内容の限定へ差し替え。`fetched_at`を更新しない事実と、却下した2案（`fetched_at`更新・`0`へのリセット）の理由を追記 |
| schema | `_create_schema`への列追加と、同一version同一構造の契約を追記 |
| 検証 | 21を「移行は`fetched_at`を更新しない／即dueになる」へ差し替え。22へ「2回目のDNS失敗で終端になる」を追加。23・24を新規追加（計24項目） |
