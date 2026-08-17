# syncのquickモード

ステータス: 確定

## 最終案

### 結論

`feedian sync`の既定を**quickモード**にする。quickは「まだ取り込んでいないもの」だけを同期し、既知itemの差分確認を行わない。完全同期は`--full`で明示的に実行する。

あわせて、ページ取得が失敗したときの記録方法を修正する。現在は失敗しても空本文の`resource_revision`が書かれ、そのresourceが「取得済み」に見えてしまう。これを`fetch_capture`だけの記録に分離する（レビュー7の「C案」）。この修正により、quickの対象定義が`resource.current_revision_id IS NULL`という単純な条件で成立する。

自動昇格は**行わない**。指定されたモードだけで動き、完全同期の周期は外部のスケジュール（OS scheduler、cron、CI、`feedian run`）へ委ねる。

### 確定事項

#### 1. モードとフラグ体系

| 呼び出し | 実行モード |
|---|---|
| `feedian sync` | quick（CLI既定） |
| `feedian sync --quick` | quick（既定を明示する形。scriptの可読性のために残す） |
| `feedian sync --full` | full |
| `feedian run` | full。現行どおり`poll_hours`のdue判定に従う |
| `sync_vault(..., quick=False)` | full。**Python APIの既定は変更しない** |

- モードは実行開始時に一意に決まり、実行中に変更しない。
- **`sync_vault`の`quick`既定を`True`にしてはならない。** `_run_pipeline`は`sync_vault(store, config, source=provider)`をkeyword引数なしで呼ぶため（`feedian/cli.py:547`）、関数既定を反転させると週次runが黙ってquickになる。既定の反転はCLI層に限定する。
- `--force-fetch`と`--force-comments`は**full専用**とする。`--full`が無い場合はCLI error（exit code 2）とする。暗黙に`--full`を含意させない。
- `--quick`と`--skip-page-fetch` / `--skip-comments`は併用できる。
- `--source rss`でquickを指定してもerrorにしない。RSSは元々feedの窓が有界で完全同期との差がほぼ無いため、helpにその旨を書く。

#### 2. quickの対象

quickが処理するのは次の和集合である。

- **(A) 新規item** — `source_item`に`(provider, account, native_id)`の行が存在しない。
- **(B1) 本文未取得resource** — URLを持つresourceで、`resource.current_revision_id`がNULLである。

対象外のitem（既知かつ本文取得済み）に対しては、`upsert_canonical_item`、`should_fetch_resource`、コメント照会、`record_sync_item`のいずれも行わない。

#### 3. 失敗記録の分割（C案）

ページ取得が失敗したとき、現在は`_store_page`が`record_resource_revision`を呼び、`fetch_capture`（warning）と空本文の`resource_revision`が一体で書かれる（`feedian/store.py:388`、`399-414`）。この1回の書き込みが2つの役割を兼ねている。

| 書かれるもの | 役割 | 確定 |
|---|---|---|
| `fetch_capture`（warning、payload、`fetched_at`） | 再取得の抑制（`should_fetch_resource`、`feedian/store.py:666-689`）とノートの`## Fetch Warning`表示（`feedian/renderer.py:259-261`） | **残す** |
| `resource_revision`（空本文） | 無い。副作用として`resource.current_revision_id`が埋まる | **書かない** |

**失敗の判定** — `_store_page`到達時点で`page.error`があり、かつ`page.text`が空であること。RSSのembedded content代替は`_store_page`より前に適用されるため（`feedian/sync.py:127-130`）、feed本文を得たRSS itemは失敗にならない。`low-confidence extraction`のように`error`があっても本文が非空のものは失敗ではない。

**失敗記録経路の要件**

1. `http_payload_id`と`rendered_payload_id`をcaptureへ保存する。`unsupported content type`の失敗は`raw_body`を伴い（`feedian/extract.py:309-320`）、これは`reextract`の入力である（`feedian/reextract.py:16-55`）。`delete_orphan_payloads`はcaptureから参照されるpayloadを保護するため（`feedian/store.py:1065-1081`）、revisionが無くてもGCされない。
2. 既存captureがあればUPDATE、無ければINSERTし、いずれも`fetched_at`を現在時刻へ更新する。後述のorderingと30分backoffの双方がこれに依存する。
3. 現在のrevisionが空本文であれば`resource.current_revision_id`をNULLへ正規化する。**`resource_revision`行は削除しない。** `resource_revision_id`への外部キーが5箇所あり（`feedian/store.py:1175`、`1213`、`1222`、`1270`、`1352`）、接続は`PRAGMA foreign_keys=ON`で開かれる（`feedian/store.py:95`）。一度要約まで済んだresourceの行は`llm_run`から参照されており、削除はFK違反になるうえ課金済みのLLM結果を捨てる。
4. 正規化を行ったときはsearch indexを dirty にする。`rebuild_search_index`は`current_revision_id`でJOINするためである（`feedian/search.py:80-85`）。
5. `feedian/sync.py:111-120`の例外経路も、resourceが特定できていれば同じ失敗記録を行う。captureが作られないままだと`fetched_at`が進まず、orderingで常に最先頭に留まる。

**`reextract`への適用** — `reextract`は抽出結果が空でも無条件に`record_resource_revision`を呼ぶ（`feedian/reextract.py:44-51`）。同じ判定を適用し、抽出結果が空なら revision を書かない。適用しないと空revisionが再生産され、正規化したresourceが(B1)から押し出される。

**適用範囲** — これはquickモードの機能ではなく既存の失敗処理の修正であり、完全同期の挙動も同じく変わる。

#### 4. provider別の収集戦略

| provider | 収集 | 根拠 |
|---|---|---|
| `raindrop` | 早期打ち切りする | Raindrop APIの`sort=-created`は作成日時の降順である（[Multiple raindrops](https://developer.raindrop.io/v1/raindrops/multiple)）。既知itemだけのページに到達したら以降も既知とみなせる |
| `hatena` | 変更しない（全件収集を維持） | 検索APIの返却順が降順である保証を確認できていない。`fetch_hatena_bookmarks`は取得後に`created_at`で並べ替えている（`feedian/hatena.py:288`）。収集の削減より下流スキップの効果が大きい |
| `rss` | 変更しない | ETag / Last-Modifiedによる条件付き取得が既にあり、feedの返却窓が有界である |

**Raindropのページ境界** — `RaindropClient`に`iter_raindrop_pages(...) -> Iterator[list[dict]]`を追加し、既存の`iter_raindrops`をその平坦化として実装する。paging処理を二重に持たないためである。

- `iter_raindrop_pages`は**APIが返した完全なページをそのままyieldする**。`limit`による切り詰めを行わない。切り詰めると「APIの最終partial page」と「limitで切られたページ」をlist長で区別できなくなる。`limit`は平坦化層または呼び出し側で適用する。
- 打ち切り条件は「1ページが全て既知item」であり、しきい値は`config.fetch.quick_stop_after_known_pages`（既定1）。1以上の整数として検証し、不正値は拒否する。
- 途中に対象itemを含むページが現れたらcounterをresetする。
- ページ件数が`perpage`未満の最終partial pageは自然終了であり、`stopped_early`を立てない。`limit`到達による終了も立てない。`stopped_early`は既知ページのしきい値到達で打ち切ったときにのみ立てる。

**一括インポートの限界** — Raindropは import 時に任意の`created`を受け付ける（[Import](https://help.raindrop.io/import)）。過去日付を保持した一括インポートは降順の深い位置に着地し、quickでは検出できない。CLIのhelpに明記する。

#### 5. (B1)passの契約

(B1)はproviderの新着列挙から分離し、DB駆動の独立したpassとする。providerへのリクエストを1件も要さない。

1. `--source`で選択されたproviderに属するfetch可能resourceだけを対象とする。
2. 候補は「`current_revision_id IS NULL`」または「現在本文が空かつ最新captureにwarningあり」。後者はC案適用前に蓄積した失敗resourceのための**移行期間限定の互換条件**であり、母集団が枯れた時点で削除できる。
3. 実際に取得するかは`should_fetch_resource`で判定する。通常の`refresh_days`再取得は(B1)に含めない。
4. 同一resourceは1回だけ取得する。`_resolve_resource`は正規化URLでresourceを束ねるため（`feedian/store.py:1083-1110`）、同じ記事をRaindropとHatenaでブックマークすると1 resourceを2 source_itemが共有する。これは通常の利用形態である。
5. URLは`resource_identifier(namespace='url')`を正とする。URLを持たないresource（`{source}_native` namespace）は対象外とする。
6. 本文取得だけを行う。provider metadata更新、Hatenaコメント照会、RSS payloadからのembedded content復元は行わない。したがって草案の「対象itemに対する処理は現行の完全同期と同一である」は破棄する。
7. `sync_run_item`は、選択providerに属してそのresourceを参照する**全source item**へ同じ成功・失敗を記録する。主キーは`(sync_run_id, source_item_id)`なので複数row挿入は制約と整合する（`feedian/store.py:1259-1265`）。代表1件方式は順序が未定義で監査記録が欠ける。
8. `--skip-page-fetch`指定時はpass自体を実行しない。
9. `--limit`は(A)と(B1)を合わせたprovider単位の共有予算とし、(A)を優先する。**予算は実際に取得した件数で数える。** payloadを伴う失敗は`refresh_days`分岐へ入り「候補だが今回は取得しない」となるため、候補数で数えると予算が空振りする。
10. 候補は最新`fetch_capture.fetched_at`の昇順（captureなし＝未試行を最先頭）で処理する。C案では失敗してもresourceは(B1)に残り続けるため、orderingが無いと予算内で常に同じ先頭群だけが選ばれ、後続が飢える。

#### 6. `sync_run.mode`とschema 7

- `sync_run`に`mode TEXT NOT NULL DEFAULT 'full'`を追加し、SQLite schema versionを6から**7**へ上げる。`feedian migrate`による明示移行とし、暗黙移行はしない。
- `create_sync_run(providers, fingerprint, mode="full")`に引数を追加する。
- `_due_providers`は`mode='full'`のrunだけを見る。対策しなければquickの反復で完全同期が永久にdueにならず、`feedian run`が静かに機能を失う。
- `settings_fingerprint`の入力へ`"quick": quick`を加える。quick runと完全runが同じ指紋を持ってはならない。
- `feedian status`は最新runのmodeを表示する。

**移行の影響範囲** — `VaultStore.open`は既定で`allow_migration=False`であり、`migrate`が例外を送出する（`feedian/store.py:88-103`、`113-134`）。`_sync`、`_status`、`_ingest`、`_render`はいずれも`allow_migration`を渡さずopenするため、**version 7導入後は移行が完了するまでDBを開く全commandが停止する**。quickに限った話ではない。

**既存データ** — C案適用前に蓄積した失敗resourceに対するデータ移行は行わない。破壊的な一括変更を避け、確定事項5-2の互換条件で扱う。完全同期またはquickがそのresourceに触れた時点で正規化され、互換条件の母集団は単調減少する。

#### 7. quickだけを実行し続けた場合に失われるもの

1. provider側のmetadata差分（Raindropのタグ・ノート編集など）
2. Hatenaコメントの増減
3. `refresh_days`到達による本文の定期再取得
4. 取得に失敗した本文の復旧（`should_fetch_resource`の対象になるのは(B1)候補のみであり、既に本文を持つresourceの再取得は完全同期の責務）

これらは自動補正の対象としない。helpと運用ドキュメントで完全同期の定期実行を案内する。

#### 8. 範囲外

- **provider側の削除検出** — `source_item.removed_at`を非NULLへ設定するコードは製品に存在しない（書き込みは`feedian/store.py:295`と`539`の`= NULL`のみ）。完全同期にも削除検出は無い。本仕様で新設しない。
- **恒久的に到達不能なURLへの長期backoff** — `should_fetch_resource`の失敗時待機は30分であり（`feedian/store.py:686`）、日次運用では毎回再試行と変わらない。これは現行の完全同期にも存在する既存の問題であり、本仕様では扱わない。(B1)のorderingは予算配分の公平性を保証するだけで、この問題を解決しない。
- **`--limit`付きfullと`_due_providers`** — `feedian sync --full --limit 50`は`mode='full'`のrunを記録し、全件走査していないにもかかわらず`feedian run`のdue判定を`poll_hours`のあいだ抑止する。現行でも`feedian sync --limit 50`が同じ抑止をしている既存挙動であり（`feedian/store.py:656-664`、`feedian/cli.py:575-587`）、本仕様では変更しない。
- **`feedian run`への「日次quick・週次full」の導入** — `poll_hours`が単一値である現在の設定モデルでは表現できない。必要になった時点で別仕様とする。
- **Hatenaの早期打ち切り** — 検索APIの返却順を実測できた場合に別仕様として検討する。

### インターフェース

```python
# feedian/sync.py
def sync_vault(
    store: VaultStore,
    config: VaultConfig,
    *,
    source: str = "all",
    limit: int | None = None,
    quick: bool = False,          # 追加。既定は False のまま
    fetch_pages: bool = True,
    fetch_comments: bool = True,
    force_fetch: bool = False,
    force_comments: bool = False,
    progress: Callable[[int, CanonicalItem], None] | None = None,
    collection_progress: Callable[[str, int, int], None] | None = None,
    comment_progress: Callable[[int, int], None] | None = None,
) -> SyncReport: ...


@dataclass(frozen=True)
class SyncReport:
    run_id: str
    processed: int
    changed: int
    failed: int
    fetched: int
    quick: bool = False                   # 追加
    skipped: int = 0                      # 追加: quickで対象外と判定した件数
    retried: int = 0                      # 追加: (B1)passで取得したresource数
    stopped_early: tuple[str, ...] = ()   # 追加: 早期打ち切りしたprovider


# feedian/store.py
class VaultStore:
    def known_native_ids(self, provider: str, *, account: str = "default") -> set[str]: ...
    def unfetched_resources(self, providers: list[str], *, account: str = "default") -> list[tuple[str, str]]:
        """(resource_id, url) を fetch_capture.fetched_at 昇順（未試行が先頭）で返す。"""
    def record_failed_fetch(
        self, resource_id: str, *, warning: str, final_url: str = "",
        http_payload_id: str | None = None, rendered_payload_id: str | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> None:
        """revision を書かずに capture だけを記録し、空 revision を正規化する。"""
    def create_sync_run(self, providers: list[str], settings_fingerprint: str, mode: str = "full") -> str: ...
    def latest_provider_sync_run(self, provider: str, *, mode: str = "full") -> sqlite3.Row | None: ...
    def source_items_for_resource(self, resource_id: str, providers: list[str]) -> list[str]: ...


# feedian/raindrop.py
class RaindropClient:
    def iter_raindrop_pages(
        self, collection_id: int, per_page: int, nested: bool
    ) -> Iterator[list[dict[str, Any]]]:
        """API が返したページをそのまま yield する。limit による切り詰めをしない。"""
```

CLIの出力行:

```
sync: run=<id> mode=quick processed=12 changed=12 skipped=2988 fetched=12 retried=3 failed=0 stopped_early=raindrop
```

`processed`の意味は変えない（provider item loopで処理したitem数）。`retried`は(B1)passで取得したresource数を別に数える。`fetched`は両passの合計。

### 検証

`python -m pytest -q`

`tests/test_sync.py`（特記なき場合）に追加する。

1. 完全同期の直後のquickで、Raindropのリクエストが1ページで止まり、Hatenaのブックマーク件数照会が0回になる。
2. 新規item1件を追加したquickで、その1件だけが`processed`に数えられ、本文とコメントが取得される。
3. quickは既知itemの`source_item_revision`を増やさない。provider側でmetadataを変更しても差分が記録されない。
4. `resource.current_revision_id`がNULLのresourceは、既知itemであってもquickの対象になる。
5. **取得に失敗したresourceは、空revisionではなくcaptureだけが記録され、`current_revision_id`がNULLのままである**（C案の中核）。
6. 5のresourceは次のquickでも(B1)候補に残る。
7. C案適用前を模した「空本文revision＋warning付きcapture」のresourceが、互換条件で(B1)候補になり、失敗記録時に`current_revision_id`がNULLへ正規化される。**`resource_revision`行は残る。**
8. `llm_run`から参照されている空revisionを持つresourceの正規化がFK違反を起こさない。
9. payloadを伴う失敗（`unsupported content type`）でpayloadがcaptureに保存され、`delete_orphan_payloads`後も残る。
10. 同一URLをRaindropとHatenaが参照するresourceを、(B1)passが1回だけ取得し、`sync_run_item`を両方のsource itemへ記録する。
11. URLを持たないresourceが(B1)から除外される。
12. `--limit`が(A)と(B1)の共有予算として働き、(A)が優先される。予算が実取得件数で数えられる。
13. (B1)候補が`fetch_capture.fetched_at`昇順で処理される。
14. `--skip-page-fetch`時に(B1)passが実行されない。
15. `feedian sync --force-fetch`と`feedian sync --quick --force-fetch`がexit code 2で失敗し、`feedian sync --full --force-fetch`が成功する。
16. quick runと完全runの`settings_fingerprint`が異なる。
17. `_due_providers`はquick runを無視する（`tests/test_due.py`）。
18. `_run_pipeline`が既定変更後も完全同期を実行する（`sync_vault`が`quick=False`で呼ばれる）。
19. schema 6のDBを通常openすると移行案内で失敗し、`feedian migrate`後に再開する。移行後の既存`sync_run.mode`が`'full'`である（`tests/test_store.py`）。
20. `reextract`は抽出結果が空のときrevisionを書かない（`tests/test_reextract.py`）。

### 却下した案

| 案 | 却下理由 |
|---|---|
| quickからfullへの自動昇格（最終full runの経過日数で判定） | 利用者が指定したモードと異なる処理を内部判断で実行しない、というtsunyanの決定（レビュー5）。完全同期の周期は外部スケジュールへ委ねる |
| `--force-fetch`が`--full`を暗黙に含意する | 同上。指定されたモードだけで動く原則を優先する |
| (B2)（失敗後の再試行期限到来）を(B1)と別に定義し、`retry_after`と`consecutive_failures`を永続化して指数backoffを組む | C案で失敗時にrevisionを書かなくなれば、失敗resourceは(B1)に残るため(B2)自体が不要になる。新規schema状態をゼロにできる |
| 定義(B)を「`should_fetch_resource`が真かつ一度も非空の本文を得ていない資源」とする | 現行DBは履歴を保持しない。`record_resource_revision`はrevisionを同一row上でUPDATEし、`fetch_capture`もresourceごと1行を上書きするため「一度も」は判定できない |
| C案適用前の失敗resourceを一括migrationで正規化する | データ削除を伴い、判定を誤れば正常な記録まで消す。互換条件で扱えば破壊的変更なしに母集団が枯れる |
| 正規化時に空の`resource_revision`行を削除する | `resource_revision_id`への外部キーが5箇所あり、`llm_run`から参照されている行のDELETEはFK違反になる。課金済みのLLM結果も失う |
| `sync_run_item`を共有resourceの代表1件へ記録する | 「最初」の順序が未定義で、残りの参照元が監査記録から消える |
| `iter_raindrop_pages`が`limit`でページを切り詰める | APIの最終partial pageとlimitで切られたページをlist長で区別できなくなる |
| Hatenaにも早期打ち切りを導入する | 検索APIの返却順が降順である保証を確認できていない。下流スキップだけで支配的なコスト（コメント件数照会）は消える |
| `--quick`を`--new-only`へ改名する | 利用者が最初に使った呼称であり、helpで対象を説明すれば誤解は生じない |
| provider単位で`sync_run.mode`を記録する | 自動昇格が不採用になり、mixed modeを実行する場面が無くなった |

## 草案

### 背景

現行の`feedian sync`は、providerの全件を毎回走査する設計になっている。新着が1件も無い実行でも、次のコストが必ず発生する。

1. **provider収集** — Raindropは`sort=-created`で全ページを取得する（`feedian/raindrop.py:32-59`、50件/リクエスト）。Hatenaは検索APIを`https`と`http`の2クエリで全件走査する（`feedian/hatena.py:222-290`、100件/リクエスト、0.3秒間隔）。RSSはfeed単位でETag / Last-Modifiedによる条件付き取得が既にあり（`feedian/sync.py:223-234`）、収集コストは元から小さい。
2. **item単位のDB処理** — `upsert_canonical_item`が全件に対してトランザクションを張る（`feedian/store.py:246-337`）。metadata hashとpayload hashが一致すれば書き込みはしないが、走査そのものはO(N)である。
3. **本文取得の判定** — `should_fetch_resource`が全件に対してSQLを1本ずつ発行する（`feedian/store.py:666-689`）。`refresh_days`（既定30日）を超えた資源は再取得されるため、30日ごとに全件のHTTPが再度走る。
4. **Hatenaコメント** — `comment_targets`にはURLを持つ全itemが積まれ（`feedian/sync.py:142-145`）、`_store_hatena_comments_parallel`が全件のブックマーク数を照会してから差分だけを再取得する（`feedian/sync.py:396-411`、20件/リクエスト）。**未変更のVaultでも、この件数照会だけでN/20リクエストが毎回発生する。**

蔵書3,000件のVaultで見積もると、新着ゼロの1回の実行でおよそ`3000/50 = 60`（Raindrop）＋`2 × 3000/100 = 60`（Hatena収集）＋`3000/20 = 150`（Hatenaコメント件数）で270リクエスト前後になる。支配的なのは4のコメント件数照会である。

「全件に変更が無いことの確認」自体は正しい動作であり、これを廃止はしない。しかし新着だけを取り込みたい日常運用に対して代価が大きすぎる。**未取得のものだけを同期するモード**を追加する。

### 目的と非目的

**目的**

- 新着itemの取り込みを、全件走査を伴わずに完了させる。
- Vaultに未取得の本文が残っている場合、それを取りこぼさない。
- 完全同期（現行動作）を既定のまま維持し、quickモードは明示的なopt-inとする。

**非目的**

- 完全同期の置き換えではない。provider側でのタグ・ノート編集、削除（`removed_at`）の検出は完全同期の責務のまま残す。
- `refresh_days`の意味は変更しない。
- `render`、`ingest`、`snapshot`は変更しない。

### 用語 — 「未取得」の定義

quickモードの対象itemを次の和集合として定義する。

- **(A) 新規item** — `source_item`に`(provider, account, native_id)`の行が存在しない。
- **(B) 本文未取得item** — `source_item`は存在するが、対応する`resource.current_revision_id`がNULLである。

(B)を含める理由は、過去の実行で本文取得に失敗したitemが、(A)だけの定義ではquickモードで永久に再試行されないためである。`should_fetch_resource`は失敗直後の資源を30分後に再試行する設計になっており（`feedian/store.py:680-688`）、その意図をquickモードでも保つ。

判定はitemごとのSQLではなく、実行開始時に1回だけ次の2集合をメモリへ読み込んで行う。数万行でも数十msで済み、item単位のクエリを消せる。

```python
# feedian/store.py に追加
def known_native_ids(self, provider: str, *, account: str = "default") -> set[str]: ...
def unfetched_native_ids(self, provider: str, *, account: str = "default") -> set[str]: ...
```

`removed_at`が非NULLのitemは「既知」として扱う。復活はquickモードの責務ではない。

### provider別の収集戦略

quickモードの節約効果はproviderごとに性質が違う。一律の早期打ち切りは採らない。

| provider | 収集の変更 | 根拠 |
|---|---|---|
| `raindrop` | 早期打ち切りする | `sort=-created`降順が保証されており、既知itemだけで構成されたページに到達したら以降は既知とみなせる |
| `hatena` | **変更しない（全件収集を維持）** | 検索APIの返却順が降順である保証を確認できていない。`fetch_hatena_bookmarks`は取得後に`created_at`で並べ替えており（`feedian/hatena.py:288`）、実装者もAPI順序に依存していない |
| `rss` | 変更しない | ETag / Last-Modifiedによる条件付き取得が既にあり、feedの返却窓自体が有界である |

**Raindropの打ち切り条件** — ページ単位で判定する。1ページ（最大50件）が全て既知itemだった時点で、そのproviderの収集を終了する。しきい値は`config.fetch.quick_stop_after_known_pages`（既定1）とし、順序の揺れが観測された場合に2以上へ上げられるようにする。

**Raindrop打ち切りの既知の限界** — Raindropの`created`はブックマークの作成時刻であるため、通常の新規追加は必ず先頭に来る。一方、**過去日付を保持した一括インポートは降順の深い位置に着地し、quickモードでは検出できない**。これは仕様上の割り切りであり、完全同期を既定に残す理由でもある。CLIの出力とヘルプにこの制約を明示する。

**Hatenaについて** — 収集を変えなくても、後述の下流スキップだけで支配的なコスト（コメント件数照会150リクエスト）は消える。収集の60リクエストを削るために順序の未検証な仮定を持ち込むのは割に合わない。検索APIの順序を実測で確認できた場合は、別途の変更として早期打ち切りを検討する。

### 下流処理のスキップ

quickモードでは、対象外item（既知かつ本文取得済み）に対して次を**一切行わない**。

- `upsert_canonical_item`を呼ばない。metadataの差分検出は行わない。
- `should_fetch_resource`を呼ばない。`refresh_days`による再取得は発生しない。
- `comment_targets`へ積まない。したがってHatenaのブックマーク件数照会の対象にならない。
- `record_sync_item`を書かない。

対象itemに対する処理は現行の完全同期と同一である。分岐は「対象かどうか」の1点に閉じる。

この帰結として、**quickモードではRaindrop側でのタグ・ノート編集、Hatena側でのコメント増減が反映されない**。これは設計上の意図であり、欠陥ではない。

### インターフェース

```python
# feedian/sync.py
def sync_vault(
    store: VaultStore,
    config: VaultConfig,
    *,
    source: str = "all",
    limit: int | None = None,
    quick: bool = False,          # 追加
    fetch_pages: bool = True,
    fetch_comments: bool = True,
    force_fetch: bool = False,
    force_comments: bool = False,
    ...
) -> SyncReport: ...


@dataclass(frozen=True)
class SyncReport:
    run_id: str
    processed: int
    changed: int
    failed: int
    fetched: int
    quick: bool = False           # 追加
    skipped: int = 0              # 追加: quickモードで対象外と判定した件数
    stopped_early: tuple[str, ...] = ()  # 追加: 早期打ち切りしたprovider
```

`processed`の意味は変えない（実際に処理したitem数）。`skipped`は収集はしたが処理しなかった件数を指す。

CLI:

```
feedian sync --quick [--source PROVIDER] [--limit N]
```

- `--quick`と`--force-fetch`、`--quick`と`--force-comments`は**排他とし、argparseの段階でエラーにする**。forceは「既知itemを強制的に再取得する」指示であり、quickの「既知itemに触れない」と正面から矛盾する。黙って一方を優先するとどちらが効いたのか利用者に分からない。
- `--quick`と`--skip-page-fetch` / `--skip-comments`は併用可能である。
- `--limit`はprovider当たりの上限として従来どおり働く。早期打ち切りが先に効くため、実際に効くのは初回同期時が中心になる。

出力行:

```
sync: run=<id> mode=quick processed=12 new=12 skipped=2988 fetched=12 failed=0 stopped_early=raindrop
```

### `sync_run`への記録と`feedian run`への影響

**これがこの変更で最も壊れやすい箇所である。**

`_due_providers`は`latest_provider_sync_run`（`feedian/store.py:656-664`）で「最後に完了したrun」を取り、`poll_hours`と比較して同期の要否を決める（`feedian/cli.py:575-587`）。quick runもここに記録されるため、**対策をしなければquickを日常的に実行するだけで完全同期が永久にdueにならなくなる**。週次の完全同期を前提にした`feedian run`が静かに機能を失う。

対策として次を採る。

1. `sync_run`に`mode TEXT NOT NULL DEFAULT 'full'`列を追加する。SQLite schema versionを6から**7**へ上げ、`feedian migrate`での明示移行とする（既存行は`'full'`で埋まる）。
2. `create_sync_run(providers, fingerprint, mode="full")`に引数を追加する。
3. `latest_provider_sync_run(provider, *, mode="full")`を追加し、`_due_providers`は`mode='full'`のrunだけを見る。
4. `feedian run`は完全同期のまま変更しない。quickは対話的な`feedian sync --quick`専用とする。

さらに`settings_fingerprint`の入力へ`"quick": quick`を加える。fingerprintは実行条件の同一性を表すものであり、quick runと完全runが同じ指紋を持ってはならない。

`feedian status`はquickかどうかを含めて最新runを表示する。

### 移行

- schema 6のVaultは`feedian migrate`で7へ移行する。移行は`ALTER TABLE sync_run ADD COLUMN mode`の1本で、既存データの書き換えを伴わない。
- 未移行のVaultで`--quick`を指定した場合は、`feedian migrate`を促すエラーで失敗させる。暗黙移行はしない（LLMバックエンドのVault configと同じ方針を踏襲する。[LLMバックエンド抽象化](20260816-llm-backends.ja.md)）。

### 命名について

`quick`は`VaultStore.quick_check()`（`PRAGMA quick_check`、`feedian/store.py:187-188`）と語が衝突する。層が異なり実害は小さいと判断してユーザーの呼称をそのまま採用するが、代案として`--new-only`（対象を最も正確に表す）と`--incremental`（一般的な語彙）を挙げておく。レビューで再考の余地がある。

### 検証

`tests/test_sync.py`に追加する。

1. 完全同期の直後にquick同期を実行すると、Raindropのリクエストが1ページで止まり、Hatenaのブックマーク件数照会が0回になる。
2. 新規item1件を追加してquick同期すると、その1件だけが`processed`に数えられ、本文とコメントが取得される。
3. `resource.current_revision_id`がNULLのitemは、既知itemであってもquick同期の対象になる（定義(B)）。
4. quick同期は既知itemの`source_item_revision`を増やさない。provider側でmetadataを変更しても差分が記録されない（意図した挙動の固定）。
5. `--quick --force-fetch`と`--quick --force-comments`はexit code 2で失敗する。
6. quick runと完全runの`settings_fingerprint`が異なる。
7. `_due_providers`はquick runを無視する — quick runだけを記録した状態でproviderがdueのままであることを確認する（`tests/test_due.py`）。
8. schema 6のDBに対する移行後、既存`sync_run`行の`mode`が`'full'`である。

検証コマンド:

```
python -m pytest -q
```

### 未決事項

1. **Hatena検索APIの返却順** — 実測が取れれば、Hatenaにも早期打ち切りを導入できる。本仕様では意図的に見送っている。
2. **quickモードのRSS扱い** — 現状は下流スキップのみだが、RSSは元々「feedの窓に入っているもの＝ほぼ新着」であり、完全同期との差がほとんど無い。`--quick --source rss`をエラーにすべきか、無害な同義として許すか。草案では**許す**（利用者が`--source`を切り替えるたびにフラグを付け外しする方が煩わしい）。
3. **`feedian run`へのquick投入** — 「毎日quick、週次full」というスケジュールは自然な発展だが、`poll_hours`が単一の値である現在の設定モデルでは表現できない。本仕様の範囲外とし、必要になった時点で別仕様とする。

## レビュー

### レビュー1 — Codex (2026-08-18)

結論は**要修正**である。完全同期を既定のまま残し、quick runを完全同期のdue判定から分離する方針は妥当である。一方、Raindropの早期打ち切りは、草案自身が目的に掲げた「本文未取得itemを取りこぼさない」と両立していない。実装前に少なくとも指摘1から4を仕様上で解消する必要がある。

#### 草案の採否

| 項目 | 採否 | 理由 |
|---|---|---|
| 完全同期を既定とし、quickを明示的なopt-inにする | 採用 | provider側の更新・削除を検出しない制約を、利用者が選択した実行に限定できる |
| 対象を(A)新規itemと(B)本文未取得itemの和集合にする | 修正して採用 | 失敗後の再試行には(B)が必要だが、現状のRaindrop打ち切り条件では深いページの(B)へ到達できない |
| Raindropだけを作成日時降順で早期打ち切りする | 修正して採用 | APIは`-created`を作成日時降順として定義しているが、ページ境界の契約と(B)の救済方法が不足している |
| Hatenaは全件収集を維持し、下流処理だけを省略する | 採用 | 未確認の返却順へ依存せず、コメント件数照会の削減という主要効果を得られる |
| `sync_run.mode`でquick runと完全runを分離する | 採用 | quickの反復によって完全同期が永久にdueにならない事故を防げる |
| `--quick`とforce系optionを排他にする | 採用 | 指示の意味が衝突しており、暗黙の優先順位を設けない判断が明確である |
| `SyncReport`とCLI出力の草案 | 修正して採用 | `new`と`changed`が混在し、既存itemである(B)を処理した場合の数値を一意に解釈できない |
| schema version 7への明示移行 | 修正して採用 | 列追加自体は妥当だが、現行のopen処理ではquickに限らず未移行DBを開く全commandが停止することを明記する必要がある |

#### 指摘1: Raindropの早期打ち切りが本文未取得itemを取りこぼす — 重大度: 高

草案はquickの対象を(A)新規と(B)本文未取得の和集合と定義する一方、Raindropでは「全て既知itemのページ」で打ち切るとしている。たとえば1ページ目の50件が全て既知かつ本文取得済みで、2ページ目に既知かつ本文未取得のitemがある場合、1ページ目で停止して2ページ目の(B)を永久に再試行しない。`quick_stop_after_known_pages`を2以上にしても位置を後ろへずらすだけで、保証にはならない。`--limit`が未取得itemより手前で尽きる場合も同じ問題がある。

`unfetched_native_ids`に未処理IDが残っている間は打ち切らない、という修正だけでは、provider側ですでに削除されたIDが1件あるたびに毎回全件走査へ戻る。したがって、次のいずれかを仕様として選ぶ必要がある。

1. (B)をproviderの新着列挙から分離し、DBに保存済みのURLとsource metadataから先に再試行する。Raindropの早期打ち切りは(A)の検出だけに使う。
2. provider列挙中に(B)を回収する場合は、未遭遇IDがprovider側に存在しないときの終了・失効規則を定める。
3. 「取りこぼさない」の保証を`--limit`なしの実行など明示した条件下へ狭め、その条件でも深いページの(B)へ到達する停止規則を定める。

検証には、少なくとも51件以上のRaindropを用意し、2ページ目の既知itemだけを`resource.current_revision_id IS NULL`にしたケースを追加すること。provider側に存在しない本文未取得IDと`--limit`併用時の期待値も固定する必要がある。

**採否: 修正して採用。** (B)を含める目的は維持するが、現在の打ち切り条件はその目的に反するため、そのままでは採用しない。

#### 指摘2: ページ単位の停止を実装する契約が未定義 — 重大度: 中

現行の`RaindropClient.iter_raindrops`はページ内のitemを1件ずつyieldし（`feedian/raindrop.py:32-59`）、`_provider_items`もそのitem streamしか受け取らない（`feedian/sync.py:190-197`）。この境界のままでは、呼び出し側は「1ページが全て既知」を判定できず、client側は既知IDを知らない。

page iteratorを追加する、pageごとの継続判定callbackをclientへ渡す、またはprovider固有collectorを導入する、のいずれを所有境界とするかを仕様に記すこと。あわせて、しきい値は連続した既知ページ数か、途中に対象itemがあればcounterをresetするか、最終partial pageと`--limit`によるpage途中終了をどう数えるか、`stopped_early`をどの時点で立てるかを定義する必要がある。`quick_stop_after_known_pages`は1以上の整数として検証し、不正値を拒否するか補正するかも固定する。

**採否: 修正して採用。** provider別戦略は維持するが、ページ境界と設定値の契約が決まるまでは実装可能な仕様になっていない。

#### 指摘3: `new`と`changed`の指標が一致していない — 重大度: 中

草案の`SyncReport`には既存どおり`changed`があるが、出力例は`new=12`へ変わっている。現行CLIは`changed`を表示する（`feedian/cli.py:376-379`）。また(B)は既存itemなので、本文取得に成功しても`new=0`であり得る。provider payloadが同一なら`upsert_canonical_item`の戻す`changed`もfalseになり得るため、`processed`、`new`、`changed`、`fetched`は互いに別の指標である。

`new`を追加するのか、出力例を`changed`へ戻すのかを決め、それぞれの定義を明記すること。少なくとも「新規item 1件」と「metadata不変の本文未取得item 1件」を別々に処理したときのreportとCLI出力をテストする必要がある。

**採否: 修正して採用。** `quick`、`skipped`、`stopped_early`の追加は採るが、未定義の`new`表示は採らない。

#### 指摘4: schema移行の影響範囲がquickに限定されているように読める — 重大度: 中

現行の`VaultStore.open`はDB schemaが`SCHEMA_VERSION`未満なら、`allow_migration=True`でない限りcommand本体へ入る前に失敗する（`feedian/store.py:88-100`、`113-158`）。したがってversion 7を導入すると、未移行Vaultでは`sync --quick`だけでなく、完全同期、`status`、`run`、`render`などDBを開く全commandが移行完了まで停止する。

これは既存の明示移行方針とは整合するが、運用上の影響として移行節へ明記すること。検証には、version 6 DBを通常openすると移行案内で失敗すること、`feedian migrate`後に通常commandが再開すること、fresh DBとversion 6から移行したDBの`sync_run.mode`が同じ`NOT NULL DEFAULT 'full'`契約を持つことを追加する。

**採否: 修正して採用。** 明示移行は採るが、「未移行でquickを指定した場合」だけを記した現在の説明では影響範囲が不足している。

#### 指摘5: Raindropの順序保証は一次資料を根拠として残す — 重大度: 低

`feedian/raindrop.py:42-47`は`sort=-created`を送っていることの証拠であって、その意味をAPIが保証する証拠ではない。Raindrop APIの[Multiple raindrops](https://developer.raindrop.io/v1/raindrops/multiple)は`-created`を作成日時の降順と定義しているため、この一次資料を根拠へ追加すること。また、Raindropの[Import](https://help.raindrop.io/import)はimport時に任意の`created`を受け付けるので、草案が記す一括importの限界にも一次資料がある。

**採否: 採用。** 早期打ち切りの前提自体は公式資料で確認できる。指摘1の論理的不整合とは別問題として、根拠を文書に残す。

### レビュー2 — Claude Code (2026-08-18)

Codexの指摘1から5を実コードに当たって検証し、あわせて追加要件「quickモードを既定にする」（tsunyan, 2026-08-18）の影響を評価した。

結論は**要修正**である。Codexの指摘は5件とも実在し、いずれも採用する。ただし**指摘1と2が立脚している草案の定義(B)そのものが誤っており**（後述の指摘6）、その修正を先に入れないと指摘1の対応案も正しく設計できない。さらに追加要件は草案の中核前提（「完全同期を既定のまま維持し、quickは明示的なopt-in」）を反転させるため、Codexが採用と判定した項目のうち2件は前提ごと組み替える必要がある。

#### Codexの指摘への採否

| 指摘 | 重大度 | 採否 | 検証結果 |
|---|---|---|---|
| 1. Raindropの早期打ち切りが本文未取得itemを取りこぼす | 高 | 採用（対応案は指摘6と併せて再設計） | 論理的整合の指摘であり、そのとおり。草案が自ら掲げた目的と打ち切り条件が両立していない |
| 2. ページ単位の停止を実装する契約が未定義 | 中 | 採用 | `iter_raindrops`はitemを1件ずつyieldし（`feedian/raindrop.py:32-59`）、`_provider_items`もitem streamしか受け取らない（`feedian/sync.py:190-197`）。ページ境界は呼び出し側から見えない。指摘のとおり |
| 3. `new`と`changed`の指標が一致していない | 中 | 採用 | 現行CLIは`changed`を表示する（`feedian/cli.py:376-379`）。草案の出力例が未定義の`new`を使っていた。草案の記述ミス |
| 4. schema移行の影響範囲がquickに限定されているように読める | 中 | 採用 | `VaultStore.open`は既定で`allow_migration=False`であり、`migrate`が「Database migration is required」で送出する（`feedian/store.py:88-103`、`113-134`）。`_sync`、`_status`、`_ingest`、`_render`はいずれも`allow_migration`を渡さずopenするため、version 7導入後は移行完了までDBを開く全commandが停止する。指摘のとおり |
| 5. Raindropの順序保証は一次資料を根拠として残す | 低 | 採用 | 妥当な指摘である。ただし**当方は引用されたRaindrop公式ドキュメントの内容を自分では確認していない**。実装時に一次資料を実際に開いて本文へ反映する |

#### 指摘6: 定義(B)は「本文未取得」を捕まえない — 重大度: 高

草案は(B)を「`resource.current_revision_id`がNULL」と定義した。この定義は、(B)を導入した当の目的である「過去の実行で本文取得に失敗したitem」を**捕捉しない**。

`fetch_page_text`はネットワーク失敗やブロックURLを例外ではなく`error`付きの`PageFetchResult`として返す（`feedian/extract.py:208-400`）。この結果は`_store_page`へ渡り、`record_resource_revision`が呼ばれる（`feedian/sync.py:131-132`、`261-283`）。そして`record_resource_revision`は、`content_markdown`が空文字列であっても無条件に`UPDATE resource SET current_revision_id = ?`を実行する（`feedian/store.py:339-431`）。

したがって**取得に失敗したitemは、本文が空のまま`current_revision_id`が非NULLになる**。(B)の判定式では既取得扱いになり、quickモードの対象から外れる。草案の意図と実装が正面から食い違っている。

store側には既に正しい判定がある。`should_fetch_resource`は、warningがあり・payloadが無く・`length(trim(content_markdown)) = 0`である資源を「30分後に再試行すべきもの」として扱う（`feedian/store.py:666-689`）。(B)の定義はこの条件へ揃えるべきであり、独自の判定式を新設すべきではない。

**修正案** — (B)を「`should_fetch_resource`が真を返し、かつ一度も非空の本文を得ていない資源」と定義し、`unfetched_native_ids`ではなく`resource_id`を返す専用クエリで表現する。これには副次的な利点が3つある。

- 指摘9で述べる再試行の暴走に、既存の30分backoffがそのまま効く。
- 判定ロジックがstoreの1箇所に集約され、quickと完全同期で「取得すべきか」の解釈が分岐しない。
- 資源単位で引けるため、指摘1の対応案（(B)をprovider列挙から分離する）と自然に噛み合う。

**指摘1への影響** — Codexが挙げた3案のうち、**案1（(B)をproviderの新着列挙から分離し、DB側から先に再試行する。Raindropの早期打ち切りは(A)の検出だけに使う）を採る。** 案2はprovider側から消えたIDの失効規則という新しい状態管理を持ち込み、案3は保証条件を狭めるだけで問題を残す。案1なら(B)の再試行にprovider APIリクエストが1件も要らず、`--limit`とも早期打ち切りとも独立する。指摘6の修正でDB側から`resource_id`と、`source_item_revision.metadata_json`の`link`（`feedian/canonical.py:34`に永続化されている）でURLを引けるため、案1は実装可能である。

#### 指摘7: 追加要件をライブラリ既定へ波及させると`feedian run`が静かに壊れる — 重大度: 高

追加要件「quickモードを既定にする」を`sync_vault`のPython既定（`quick: bool = True`）として実装してはならない。

`_run_pipeline`は`sync_vault(store, config, source=provider)`とkeyword引数なしで呼んでいる（`feedian/cli.py:547`）。関数既定を反転させると、**週次の`feedian run`が無言でquickになり、provider側の編集・削除を検出する経路が製品から完全に消える**。Codexが指摘5件とは別に採用と判定した「`sync_run.mode`で完全同期のdue判定を守る」対策も、完全同期を実行する経路自体が無くなれば意味を持たない。

**したがって既定の反転はCLIの層に限定する。** `sync_vault`の`quick`既定は`False`のまま据え置き、`feedian sync`のargparse既定だけをquickにする。「既定」は利用者向けインターフェースの決定であって、ライブラリの決定ではない。`_run_pipeline`は無変更で完全同期を続ける。

草案の「完全同期を既定のまま維持し、quickモードは明示的なopt-inとする」という目的文は、この要件により**破棄する**。Codexが採用と判定した「完全同期を既定とし、quickを明示的なopt-inにする」も同様に破棄する。ただしCodexがその採用理由に挙げた「provider側の更新・削除を検出しない制約を、利用者が選択した実行に限定できる」という懸念は消えない。要件所有者の判断で既定を反転する以上、その懸念は指摘8の自動昇格で別途担保する。

#### 指摘8: quickが既定になると完全同期の実行保証が消える — 重大度: 中

quickがopt-inだった草案では、`feedian sync`を素で叩けば必ず完全同期になった。既定を反転すると、**`--full`を明示的に打たない利用者には完全同期が二度と走らない**。`feedian run`をスケジュール登録していない利用者では、provider側のタグ編集・ノート編集・削除が永久に反映されない。これは既定変更に伴う実質的な機能喪失であり、警告文の追加では埋まらない。

**修正案 — 自動昇格を仕様に入れる。** `feedian sync`は既定でquickとして開始し、対象providerの`mode='full'`な直近完了runが`config.fetch.full_sync_days`（既定30）より古い場合、その実行を完全同期へ昇格させる。昇格したときは理由を明示して出力する。

```
sync: mode=full reason=last-full-sync-38d-ago
sync: mode=quick
```

これにより完全同期の保証がCLI自身に内在し、`feedian run`のスケジュール有無に依存しなくなる。`sync_run.mode`列（Codexが採用と判定済み）は、due判定のためだけでなくこの昇格判定の入力としても必要になる。

**フラグ体系の再定義** — 既定の反転により、草案の`--quick`とforce系optionの排他規則も組み替える必要がある。

| フラグ | 意味 |
|---|---|
| （なし） | quick。ただし完全同期が`full_sync_days`超過なら自動昇格する |
| `--full` | 今回を完全同期にする |
| `--quick` | 自動昇格を抑止し、必ずquickで実行する |

`--quick`は「廃止された旧フラグ」ではなく「昇格抑止」という固有の意味を持つため、互換のための非推奨エイリアスを用意する必要がない。

force系の扱いも変える。草案は`--quick --force-fetch`をエラーとしたが、既定がquickになると`feedian sync --force-fetch`という自然な指定がそのままエラーになり、利用者に不親切である。**`--force-fetch`と`--force-comments`は`--full`を含意する**とし、明示的に矛盾する`--quick --force-fetch`と`--quick --force-comments`だけをargparseでエラーにする。Codexが採用した「指示の意味が衝突しており、暗黙の優先順位を設けない」という判断は、この形でも保たれる。

#### 指摘9: (B)の再試行に長期の抑制が要る — 重大度: 中

(B)をDB駆動の独立passにすると、**恒久的に到達不能なURLが毎回のquick同期で必ず再試行される**。完全同期では全件処理に埋もれて見えなかったが、quickが既定になるとこれが日常実行のネットワークコストのほぼ全部になる。死んだURLが数十件あるVaultでは、「新着ゼロなのに毎回数十リクエスト」という状態に落ち着く。

指摘6の修正（`should_fetch_resource`へ定義を揃える）で30分backoffは自動的に効くが、30分は日次運用に対して短すぎる。連続失敗回数に応じた指数backoff、または(B)passに1回あたりの上限件数を設ける必要がある。どちらを採るかを仕様で決めること。当方の推奨は前者であり、`fetch_capture`の連続warning数を数えて上限日数まで伸ばす方式とする。

#### 指摘10: (B)のDB駆動passはRSSのembedded content fallbackを失う — 重大度: 低

現行の同期は、ページ取得が失敗してもRSS itemなら`item.embedded_content`をrevisionとして保存する（`feedian/sync.py:111-119`、`134-141`）。しかし`embedded_content`は`CanonicalItem`のフィールドでありながら`as_bookmark_metadata()`が返す辞書に含まれていない（`feedian/canonical.py:26-46`）。したがって`source_item_revision.metadata_json`には永続化されず、**DB駆動の(B)passからは復元できない**。

実害は小さい。RSS itemは初回同期の時点で`feedian/sync.py:134-141`によりembedded contentのrevisionを得るため、(B)の状態に留まるRSS itemは稀である。ただし「(B)passはURLからの再取得のみを行い、feed本文へのfallbackを持たない」ことを仕様に明記すること。明記しないと、実装者がfallbackを再現しようとして`embedded_content`の永続化という無関係なschema変更に踏み込む。

#### 追加要件を反映した草案の変更点まとめ

最終案の作成時に反映すべき差分は次のとおりである。

1. 目的の「完全同期を既定のまま維持し、quickモードは明示的なopt-inとする」を破棄し、「`feedian sync`の既定をquickとし、完全同期は自動昇格と`--full`で保証する」に置き換える（指摘7、8）。
2. `sync_vault`の`quick`既定は`False`のまま据え置き、既定の反転はCLI層に限定する（指摘7）。
3. (B)の定義を`should_fetch_resource`の判定へ揃える（指摘6）。
4. (B)をproviderの新着列挙から分離し、DB駆動の独立passにする。Raindropの早期打ち切りは(A)の検出だけに使う（Codex指摘1・案1、指摘6）。
5. Raindropのページ境界の所有者を決める。`RaindropClient`に`iter_raindrop_pages`を追加し、`iter_raindrops`をその平坦化として実装するのが最小の変更である。しきい値、partial page、`--limit`併用時、`stopped_early`を立てる時点の契約を定める（Codex指摘2）。
6. 出力例から`new`を削り、`changed`へ戻す。全実行で`mode=`を出力する（Codex指摘3、指摘8）。
7. 移行の影響範囲を「DBを開く全command」と書き改める（Codex指摘4）。
8. (B)再試行のbackoff規則を定める（指摘9）。
9. (B)passがfeed本文fallbackを持たないことを明記する（指摘10）。
10. Raindropの`-created`降順とimport時の任意`created`について一次資料を引用する（Codex指摘5）。
11. 検証項目を追加する — 完全同期が`full_sync_days`超過時に自動昇格すること、`--quick`が昇格を抑止すること、`--force-fetch`が`--full`を含意すること、`--quick --force-fetch`がエラーになること、`_run_pipeline`が既定変更後も完全同期を続けること、取得失敗により本文が空で`current_revision_id`が非NULLのitemが(B)として再試行されること（指摘6の回帰テスト）。

#### 未決事項の更新

草案の未決事項3「`feedian run`へのquick投入」は、指摘8の自動昇格により論点が変わる。`feedian sync`側に「日常はquick、期限超過でfull」という仕組みが入るため、`feedian run`は完全同期専用のまま据え置いて差し支えない。本仕様の範囲外という結論は維持する。

未決事項2（`--quick --source rss`の扱い）は、既定がquickになったことで「利用者がフラグを付け外しする煩わしさ」という草案の論拠が消える。RSSは下流スキップのみで完全同期とほぼ差が無いため、`--source rss`では`mode`表示を`quick`としつつ実質的な差が無い旨をhelpに書く方針を推す。エラーにはしない。

### レビュー3 — Claude Code (2026-08-18)

レビュー2をcommitする前に、同一modelの独立contextで再確認した（advisor）。結果、**レビュー2に誤りが1件、誤った前提が1件あり、レビュー2が見落とした設計上の穴が4件見つかった**。以下で訂正する。レビュー2の指摘6から9の結論と重大度は、下記の訂正を適用した上で維持する。

#### 訂正1: 指摘10は撤回する

レビュー2の指摘10は「(B)のDB駆動passはRSSのembedded content fallbackを失う」としたが、**結論が誤っている**。

`embedded_content`が`as_bookmark_metadata()`に含まれない、という前提自体は正しい（`feedian/canonical.py:26-46`）。しかし復元経路は別にある。RSSのpayloadは`summary`と`content`のHTMLを含んでおり（`feedian/rss.py:174-184`）、これは`upsert_canonical_item`が`source_item_revision.payload_id`経由で`payload`表へ永続化する（`feedian/store.py:303-307`）。そして`embedded_content`はこのHTMLの純関数である（`embedded_content = _plain_text(content_html or summary_html)`、`feedian/rss.py:139`）。したがって**DB駆動の(B)passからでも完全に復元できる**。

指摘を「復元できないので明記せよ」から「**復元可能なので、復元するかしないかを仕様で選べ**」に差し替える。推奨は復元しない側である。(B)へ留まるRSS itemは初回同期で`feedian/sync.py:134-141`によりrevisionを得るため実際には稀であり、payloadを解いてHTMLを再変換する経路を(B)pass専用に持つ価値が薄い。ただし「できないから」ではなく「割に合わないから」という理由で書くこと。重大度は低のまま。

#### 訂正2: 「削除の検出」は完全同期にも存在しない — 重大度: 中

レビュー2の指摘7で「provider側の編集・削除を検出する経路が製品から完全に消える」と書き、指摘8で「削除が永久に反映されない」と書いた。**この前提は誤りである。**

`source_item.removed_at`を非NULLに設定するコードは製品のどこにも無い。`removed_at`への書き込みは`upsert_canonical_item`の`removed_at = NULL`（`feedian/store.py:295`）と`upsert_comments`の`removed_at = NULL`（`feedian/store.py:539`）の2箇所だけで、いずれも復活方向である。他は全て`WHERE removed_at IS NULL`の読み出しか、schema定義（`feedian/store.py:1132`、`1152`、`1194`）である。つまり**現行の完全同期もprovider側の削除を検出していない**。列は用意されているが書き手がいない。

草案の非目的「削除（`removed_at`）の検出は完全同期の責務のまま残す」、レビュー1の採否表にある「provider側の更新・削除を検出しない制約」、レビュー2の指摘7・8の同種の記述は、いずれも存在しない機能を前提にしている。**仕様は確定後に編集できないため、この誤前提を残すと実装者が「既存の削除検出を壊さないように」という架空の制約に縛られる。**

正しくは、quickモードで失われるのは次の3つだけである。

1. provider側のmetadata差分（Raindropのタグ・ノート編集など）の取り込み
2. Hatenaコメントの増減の反映
3. `refresh_days`到達による本文の定期再取得

指摘7と指摘8の結論（ライブラリ既定は反転しない、完全同期の自動昇格が要る）はこの訂正後も維持する。失われるものが3つでも、既定を反転する以上その保証は要る。重大度も高のまま据え置く。

#### 指摘11: `sync_run.mode`はrun単位だが、昇格判定はprovider単位である — 重大度: 高

レビュー2の指摘8で提案した自動昇格は、**現在の`sync_run`のスキーマ上では正しく動作しない。**

`create_sync_run`は対象provider全部を`providers_json`という1列に入れて1行だけ書く（`feedian/store.py:598-607`）。一方`latest_provider_sync_run`は`providers_json LIKE '%"<provider>"%'`で引く（`feedian/store.py:656-664`）。したがって`mode`列をrun単位で持つと、次の事故が起きる。

既定の`--source all`で実行し、raindropだけが`full_sync_days`を超過して昇格したとする。runは1行で、`mode='full'`、`providers_json`には`hatena`も`rss`も含まれる。すると**hatenaとrssは一度も完全走査されていないのに、完全同期済みとして記録される**。以後それらの昇格は永久に発火しない。レビュー2の指摘8はこの帰結を検討していなかった。

次のいずれかを仕様で確定すること。

1. 昇格は実行単位とする。対象providerのうち1つでも期限超過なら、その実行全体を完全同期へ昇格する。実装が単純で、`sync_run`のスキーマも1行のままでよい。代償は、1つのproviderの都合で他も完全走査されることである。
2. `mode`をprovider単位で記録する。`sync_run_provider`表を新設するか、`providers_json`をprovider→modeの写像に変える。正確だが、schema変更と`latest_provider_sync_run`の書き換えを伴う。

当方の推奨は案1である。providerは現状3つで、いずれか1つが期限超過なら他も近い時期に超過する。案2の正確さに見合う複雑さではない。ただし**この判断は仕様として明示的に書くこと**。書かずに案1を実装すると、上記の事故を「意図した挙動」と読めない。

#### 指摘12: `status='partial'`のVaultでは昇格が毎回発火する — 重大度: 中

`latest_provider_sync_run`は`status = 'completed'`の行しか見ない（`feedian/store.py:656-664`）。しかし`sync_vault`は失敗が1件でもあれば`partial`で閉じる（`feedian/sync.py:162-166`）。

Hatenaコメントの取得が慢性的に数件失敗するVault — 到達不能なURLが数件あれば容易に起こる — では`completed`が二度と記録されない。すると自動昇格の判定は常に「完全同期の記録なし」となり、**毎回の`feedian sync`が完全同期へ昇格して、quickを既定にした意味が消える**。しかも利用者から見ると「既定がquickのはずなのに毎回遅い」という、原因の分かりにくい症状になる。

昇格判定が`partial`を「完全同期を実行した」として数えるかどうかを仕様で決めること。当方の推奨は数える側である。`partial`は完全走査が行われた上で一部itemが失敗した状態であり、走査自体は完了している。`_due_providers`が`completed`のみを見る現行の挙動を変えるかどうかは別問題として切り分け、本仕様では昇格判定にだけ`partial`を含める。

#### 指摘13: 昇格判定の境界条件が未定義 — 重大度: 中

レビュー2の指摘8は正常系しか書いていない。次の2つを仕様で定めること。

- **完全同期の記録が皆無のとき**（初回実行、空のVault、移行直後）。`latest_provider_sync_run`がNULLを返す場合は昇格する、と明記すれば足りる。初回は全itemが(A)なのでquickでも完全走査と結果が一致するが、`mode='full'`を記録して以後30日の昇格を抑止できる点に意味がある。
- **`--limit`付きの完全同期を`mode='full'`として記録するか**。記録すると、`feedian sync --full --limit 50`という部分的な実行が、その後`full_sync_days`のあいだ昇格を抑止してしまう。全件を走査していない実行を完全同期として数えるべきではない。**`--limit`付きの実行は`mode='full'`として記録しない**（`mode='partial-full'`のような第3の値を設けるか、`mode='quick'`扱いにする）ことを推奨する。

#### 指摘14: (B)passと`--skip-page-fetch`の関係を明記する — 重大度: 低

(B)passは本文取得そのものであるから、`fetch_pages=False`（`--skip-page-fetch`）のときは実行しない。自明に見えるが、(B)passがprovider列挙から分離された独立passになる以上、既存フラグの適用範囲は1行で明記しておくこと。

#### 最終案へ反映すべき差分（レビュー2のまとめへの追補）

レビュー2末尾の11項目に、次を追加する。

12. 「削除の検出」に関する記述を全て除去し、quickで失われるものをmetadata差分・Hatenaコメント増減・`refresh_days`再取得の3点に限定して書き直す（訂正2）。
13. 自動昇格の単位を確定する。推奨は「対象providerのうち1つでも期限超過なら実行全体を昇格」（指摘11）。
14. 昇格判定が`status='partial'`を完全同期済みとして数えることを明記する（指摘12）。
15. 完全同期の記録が皆無のときは昇格する、`--limit`付き実行は`mode='full'`として記録しない、を明記する（指摘13）。
16. (B)passは`--skip-page-fetch`のとき実行しないことを明記する（指摘14）。
17. (B)passがRSSのfeed本文へfallbackしない理由を「復元できないから」ではなく「payloadから復元可能だが割に合わないから」と書く（訂正1）。
18. 検証項目に追加する — `--source all`でraindropだけ期限超過したときのhatena・rssの`mode`記録、`partial`で終わったrunの後に昇格が発火しないこと、`--full --limit 50`の後に昇格が抑止されないこと。

### レビュー4 — Codex (2026-08-18)

レビュー2と3を現行コードへ再照合した。結論は引き続き**要修正**である。レビュー1の5件を全て採用した判断、定義(B)をprovider列挙から分離する判断、quickの既定化をCLI層だけで行う判断、および削除検出に関する訂正は妥当である。一方、レビュー2と3の修正案には、最新状態しか保持しないDBからは実装できないものと、欠落のある完全同期を30日間成功扱いするものが残っている。指摘15から18を解消するまでは最終案へ進めない。

#### レビュー2・3の採否

| 項目 | 採否 | 理由 |
|---|---|---|
| Codex指摘1から5を全て採用する | 採用 | 現行コードとの再照合でも結論は変わらない |
| 指摘6: 定義(B)を`should_fetch_resource`へ揃え、DB駆動passへ分離する | 修正して採用 | 取得失敗後も`current_revision_id`が非NULLになる指摘は正しい。ただし「一度も非空の本文を得ていない」という履歴条件は現在のDBから判定できず、`should_fetch_resource=True`は通常の`refresh_days`再取得も含む |
| 指摘7: quickの既定化はCLI層だけで行い、`sync_vault(quick=False)`を維持する | 採用 | `_run_pipeline`を完全同期のまま維持でき、ライブラリ呼び出しの意味も暗黙に反転しない |
| 指摘8: CLI既定をquickとし、期限超過時に完全同期へ自動昇格する | 修正して採用 | quickを既定にする追加要件と完全同期の保証を両立できる。ただし何をもって期限を更新するかは`mode`だけでは表現できない |
| 指摘9: 恒久失敗URLへ長期backoffを設ける | 修正して採用 | 問題は実在するが、推奨された「`fetch_capture`の連続warning数」は履歴を上書きする現行schemaでは数えられない |
| レビュー3の訂正1: RSS payloadから`embedded_content`を復元できる | 採用 | payloadには`summary`と`content`が残り、同じ`_plain_text`を適用できる。ただし復元するかは費用対効果で選ぶという整理も妥当である |
| レビュー3の訂正2: provider側の削除検出は完全同期にも存在しない | 採用 | `source_item.removed_at`を非NULLへする製品コードは存在しない。草案、レビュー1、レビュー2の「削除検出を失う」という記述は最終案へ引き継がない |
| 指摘11: 自動昇格の単位を実行単位かprovider単位か確定する | 修正して採用 | 単位を明記する必要はある。ただし`quick`がrun全体のboolであり、1 providerの期限超過時にrun全体をfullへするなら、現行`sync_run.mode`でも誤記録は起きない。「現在のschemaでは正しく動作しない」という断定は、providerごとにmixed modeを実行する場合に限られる |
| 指摘12: `status='partial'`を完全同期済みとして数える | 不採用 | `partial`は完全走査後のコメント失敗だけでなく、RSS feedの収集失敗も表す。後者を成功扱いすると未収集feedを`full_sync_days`の期間取りこぼす |
| 指摘13: 初回は昇格し、`--limit`付きfullは完全同期済みにしない | 修正して採用 | 境界条件の指摘は正しい。ただし`partial-full`を`mode`へ追加したりquick扱いしたりすると、実行アルゴリズムと走査完全性が混ざる。別のcoverage情報で表すべきである |
| 指摘14: `--skip-page-fetch`時は(B)passを実行しない | 採用 | 独立passへ分離する以上、既存flagの適用範囲を明示する必要がある |

#### 指摘15: 修正後の定義(B)も現在のDBでは一意に判定できない — 重大度: 高

レビュー2は(B)を「`should_fetch_resource`が真を返し、かつ一度も非空の本文を得ていない資源」とした。しかし現行DBは取得履歴を保持しない。`record_resource_revision`は既存の`resource_revision`を同じrow上で更新し、`fetch_capture`もresourceごとの最新rowを更新する（`feedian/store.py:339-427`）。過去に非空本文が存在したかを後から判定する情報は失われるため、「一度も」という条件は実装できない。

また、`should_fetch_resource`は次の異なる理由を同じ`True`で返す（`feedian/store.py:666-689`）。

1. `fetch_capture`が無く、まだ本文取得を試していない。
2. 最新取得がwarning付き、payload無し、本文空で、30分を過ぎた。
3. 正常取得済みで`refresh_days`を過ぎた。

quickで救済したいのは1と2であり、3は完全同期だけの責務である。さらにwarningの無い正常な空本文は「取得済み」であり、quickで定期再試行する対象ではない。したがって(B)を単に`should_fetch_resource=True`と本文空の積で定義すると、通常の`refresh_days`再取得と正常な空ページを混入させる。

**修正案** — 歴史ではなく現在状態で、次の和集合として定義する。

- **(B1) 未試行** — fetch可能なHTTP resourceで`fetch_capture`が存在しない。
- **(B2) 失敗後の再試行期限到来** — 最新captureがwarning付き、HTTP/rendered payload無し、現在本文が空で、永続化された`retry_after`を過ぎている。

`resource.current_revision_id IS NULL`は(B1)の補助条件にはなるが、RSS fallbackなど本文取得以外のrevisionもあるため、それだけを正としない。通常の`refresh_days`到来は(B)へ含めない。URLを持たないresourceも(B)から除外する。

**採否: 修正して採用。** 取得失敗を捕捉できないというClaude Codeの指摘6は採るが、履歴を要求する定義と`should_fetch_resource`の無条件な再利用は採らない。

#### 指摘16: `partial`を一律に完全同期済みとすると、未収集providerを成功扱いする — 重大度: 高

レビュー3の指摘12は、`partial`を「完全走査が行われた上で一部itemが失敗した状態」と説明する。しかし現行実装では、RSS feedの取得例外を`record_provider_error`へ記録してそのfeedを飛ばし（`feedian/sync.py:222-239`）、run全体を`partial`で閉じる（`feedian/sync.py:162-166`）。このrunは残りのfeedを処理していても、失敗したfeedを走査できていない。

この`partial`を完全同期済みとして数えると、そのfeedの新着を既定30日間検出しない。反対に、全providerの列挙は完了してHatenaコメントだけが失敗した`partial`を数えないと、毎回fullへ昇格する。単一の`status`では両者を区別できない。

次のいずれかを仕様として選ぶ必要がある。

1. 安全側に倒し、`completed`だけを自動昇格の期限更新に使う。慢性的な下流失敗ではfullが反復することを許容する。
2. provider列挙の完全性を別に保存する。たとえばrunまたはproviderごとの`collection_completed`を持ち、item本文・コメントの失敗とは分離して期限更新を判定する。

**採否: 不採用。** `partial`を全て成功扱いする案はデータ欠落を長期化させるため採らない。下流失敗による反復fullを避けたいなら、成功扱いの範囲を推測せず永続化する。

#### 指摘17: `mode`と「完全同期として期限を更新できるcoverage」を分離する — 重大度: 中

レビュー3は`--limit`付きfullを問題にしたが、同じ問題は`--skip-page-fetch`と`--skip-comments`にもある。`mode='full'`は「quickの対象外skipを行わないアルゴリズム」を表せても、利用者が別flagで処理を省いた結果までは表さない。自動昇格が保証しようとしているものはprovider metadata、Hatenaコメント増減、`refresh_days`再取得の3点なので、次のrunを全て同じ「最終完全同期」として扱うことはできない。

- `--full --limit 50`
- `--full --skip-page-fetch`
- `--full --skip-comments`
- 全provider列挙後に本文またはコメントだけ失敗した`partial`
- provider収集中に欠落が生じた`partial`

最終案では`mode`を`quick|full`という実行方式のまま保ち、期限更新資格を別の概念として定義すること。最小案は、`limit is None`、対象providerのcollection完了、必要なskip flag無し、という条件を満たすrunだけに`full_coverage=1`を記録することである。本文・コメントの一部失敗をcoverageへ含めるかは、自動昇格が保証する範囲をどこまでとするかに合わせて決める。provider単位のmixed modeを採らず、対象のうち1 providerでも期限超過ならrun全体をfullへ昇格するなら、`sync_run.mode`はrun単位のままでよい。

**採否: 修正して採用。** Claude Codeの初回・`--limit`境界の指摘は採るが、`mode`へ第3の値を足して走査完全性を表す案は採らない。

#### 指摘18: 長期backoffとDB駆動(B)passの実行契約が不足している — 重大度: 中

`fetch_capture`はresourceごとに1 rowだけを実質的に保持し、再取得のたびにwarningと`fetched_at`を上書きする（`feedian/store.py:399-427`）。したがってレビュー2が推奨する「連続warning数を数えて指数backoff」は、現在のschemaからは実装できない。指数backoffを採るなら、少なくとも`consecutive_failures`と`retry_after`を永続化し、成功時のreset規則を定める必要がある。1 run当たりの件数上限を採るなら、同じ先頭itemだけが選ばれ続けないorderingまたはcursorを定める必要がある。

さらに(B)をresource単位の独立passにすると、元のitem loopから暗黙に得ていた次の契約が失われる。

- `--source`で選ばれたproviderだけを対象にするか、共有resourceを一度だけ処理するか。
- URLを`resource_identifier`、最新source metadata、`fetch_capture.final_url`のどれから得るか。
- 1 resourceを複数の`source_item`が参照するとき、どの`sync_run_item`へ成功・失敗を記録するか。
- `processed`、`changed`、`fetched`へ(B)passをどう数えるか。
- (B)passでHatenaコメントを照会するか。本文再試行だけに限定するなら、草案の「対象itemに対する処理は現行の完全同期と同一」は破棄する必要がある。
- RSS payloadからembedded contentを復元するか。復元しないなら、その選択と理由を明記する。

推奨は、(B)passを「選択providerに属するfetch可能resourceを重複排除して本文だけ再試行するpass」と定義し、コメント処理とprovider metadata更新から分離することである。この場合`processed`はprovider item loopの件数のまま維持し、本文pass用に`retried`を追加する方がCLIの意味を保ちやすい。いずれの数え方を採るにせよ、検証で共有resource、`--source`、URL無しresource、成功・失敗時のreportを固定すること。

**採否: 修正して採用。** DB駆動passと長期backoffの方向は採るが、永続状態と計数・対象範囲が定義されるまでは実装可能な仕様になっていない。

#### 最終案へ進むための残件

1. (B)を(B1)未試行と(B2)失敗後retry期限到来として、通常の`refresh_days`再取得から分離する。
2. backoffの永続状態とreset規則を決める。
3. 自動昇格はrun全体を単位とし、`mode`と`full_coverage`を分離するか、同等の期限更新条件を確定する。
4. `partial`を一律成功扱いせず、provider収集完了を判定できる情報を持つか、安全側の反復fullを許容する。
5. DB駆動(B)passのprovider scope、resource重複排除、URL選択、report、`sync_run_item`、コメント、RSS fallbackの契約を決める。

### レビュー5 — tsunyan (2026-08-18)

#### 人間の決定: 自動昇格は行わず、完全同期の周期は外部から制御する

`feedian sync`がquickからfullへ自動昇格する仕組みは**不採用**とする。利用者が指定したモードと異なる処理を内部判断で実行せず、完全同期の実行周期はOSのscheduler、CI、cron、または既存の`feedian run`など、外部のスケジュール設定で制御する。

CLIとライブラリの契約は次のとおりとする。

| 呼び出し | 実行モード |
|---|---|
| `feedian sync` | quick（CLIの既定） |
| `feedian sync --quick` | quick |
| `feedian sync --full` | full |
| `feedian run` | full。現行どおりproviderの`poll_hours`によるdue判定に従う |
| `sync_vault(..., quick=False)` | full。Python APIの既定は変更しない |

モードは実行開始時に一意に決まり、実行中に変更しない。最終full runの経過日数、初回実行、`partial`、providerごとの期限差を理由とする自動昇格は行わない。このため、レビュー2の`full_sync_days`、レビュー3の指摘11から13、レビュー4の指摘16と17のうち**自動昇格のためだけに必要な設計**は最終案へ入れない。

force系optionも暗黙にfullを含意させない。`--force-fetch`と`--force-comments`はfull専用とし、`--full`が無い場合はCLI errorにする。したがって`feedian sync --force-fetch`、`feedian sync --quick --force-fetch`、`feedian sync --quick --force-comments`は失敗し、利用者へ`--full`の明示を求める。これは「指定されたモードだけで動く」という原則を保つためである。

`sync_run.mode`は引き続き必要である。quick runを`_due_providers`が完全同期済みとして扱わないようにし、`feedian run`のfull同期周期をquick runから独立させる。ただし`mode`を自動昇格の判定には使わない。

この決定により、`feedian run`も外部schedulerも使わず、`feedian sync`または`--quick`だけを実行し続ける利用者には、provider metadataの更新、Hatenaコメントの増減、`refresh_days`による本文再取得が反映されない。これは自動補正する対象ではなく、helpと運用ドキュメントで完全同期の定期実行を案内する。

**採否: 採用。** quickをCLI既定にする追加要件は維持しつつ、モード選択の予測可能性を優先する。完全同期周期はアプリケーション内部の推測ではなく、利用者が管理するスケジュールへ委ねる。

### レビュー6 — Claude Code (2026-08-18)

レビュー4の指摘15から18を実コードへ照合し、あわせてレビュー5の決定が各指摘の射程に与える影響を評価した。

**Codexの指摘15から18は4件とも事実確認でき、全て採用する。** うち3件は当方の誤りを正すものである（後述の訂正3から5）。一方、レビュー5が自動昇格を不採用としたことで、**指摘16は完全に失効し、指摘17も大半が失効する**。さらにレビュー5の決定からは、指摘15と18が要求している新規schema状態を**丸ごと不要にできる**簡素化が導ける（指摘19）。これが本ラウンドの主眼である。

#### レビュー4の指摘への採否

| 指摘 | 重大度 | 採否 | 検証結果 |
|---|---|---|---|
| 15. 修正後の定義(B)も現在のDBでは一意判定できない | 高 | 採用（指摘19で射程を縮小） | `record_resource_revision`は既存revisionを同一row上で`UPDATE`し（`feedian/store.py:376-386`）、`fetch_capture`もresourceごとの1 rowを`UPDATE`し続ける（`feedian/store.py:399-427`）。履歴は残らない。「一度も非空の本文を得ていない」は実装不能であり、当方の指摘6の定義は誤り。`should_fetch_resource`の3つの真値理由を分離すべきという指摘も正しい |
| 16. `partial`を一律に完全同期済みとすると未収集providerを成功扱いする | 高 | 採用するが**レビュー5により失効** | RSS feedの収集失敗が`record_provider_error`でfeedごと飛ばされ（`feedian/sync.py:222-239`）、runは`partial`で閉じる（`feedian/sync.py:162-166`）。当方の指摘12の性格づけが誤っていた。ただし自動昇格が無くなったため、この指摘が守ろうとしていた期限更新の判定自体が存在しない |
| 17. `mode`と期限更新資格のcoverageを分離する | 中 | 採用するが**大半がレビュー5により失効**（残存核は指摘21） | 自動昇格のための概念であり、昇格が無ければ`full_coverage`は不要である |
| 18. 長期backoffとDB駆動(B)passの実行契約が不足している | 中 | 採用（backoff部分は指摘19で消滅、pass契約部分は全面採用） | `fetch_capture`が1 rowを上書きし続けるため連続失敗数は数えられない。当方の指摘9の推奨は実装不能。(B)passの契約6項目はいずれも未定義であり、指摘のとおり |

#### 訂正3: レビュー2 指摘9の推奨は実装不能だった

指摘9で「`fetch_capture`の連続warning数を数えて上限日数まで伸ばす方式」を推奨したが、`fetch_capture`はresourceごとに1 rowだけを保持し、再取得のたびに`warning`と`fetched_at`を上書きする（`feedian/store.py:399-427`）。連続失敗回数を数える情報が存在しない。Codexの指摘18が正しい。

#### 訂正4: レビュー3 指摘11の断定は過大だった

「`sync_run.mode`はrun単位だが昇格判定はprovider単位であり、現在のschemaでは正しく動作しない」と書いたが、Codexの反論が正しい。当方自身が推奨した案1（対象providerのうち1つでも期限超過ならrun全体を完全同期へ昇格）を採る場合、そのrunは全providerを完全走査するのだから、`providers_json`の全providerに`mode='full'`が付くのは**正確な記録**である。誤記録が起きるのはprovider単位のmixed modeを実行する場合に限られる。「現在のschemaでは正しく動作しない」という断定は撤回する。なお本件はレビュー5により失効した。

#### 訂正5: レビュー3 指摘12の`partial`の性格づけは誤りだった

「`partial`は完全走査が行われた上で一部itemが失敗した状態であり、走査自体は完了している」と書いたが、誤りである。RSS providerでは、feed単位の取得例外が`record_provider_error`へ記録されてそのfeedごとスキップされ（`feedian/sync.py:222-239`）、runは`partial`で閉じる。**このrunはそのfeedを一度も走査していない。** `partial`は「走査完了＋item失敗」と「走査そのものの欠落」を区別しない。Codexの指摘16が正しい。本件もレビュー5により失効した。

#### 指摘19: レビュー5の決定により、(B2)を仕様から落とせる — 重大度: 高（簡素化）

指摘15の(B2)（失敗後の再試行期限到来）と指摘18のbackoffは、いずれも**新しい永続状態**を要求する。`retry_after`と`consecutive_failures`の列追加、成功時のreset規則、backoffの上限、`fetch_capture`を1 row上書きから履歴保持へ変える判断 — これらは本仕様の本来の目的（新着だけを取り込む）から遠く離れた、独立したschema設計である。

レビュー5は自動昇格を不採用とし、完全同期の周期を外部スケジュールへ委ね、「指定されたモードだけで動く」を原則とした。**この原則を(B)へも一貫して適用すれば、(B2)は不要になる。** 取得に失敗した本文の再試行は完全同期の責務であり、`should_fetch_resource`が既に30分backoffで正しく扱っている（`feedian/store.py:666-689`）。quickがそれを肩代わりする理由は、自動昇格が無くなった今は無い。

したがって**quickの対象を(A)＋(B1)に限定する**ことを推奨する。

- **(A) 新規item** — `source_item`に`(provider, account, native_id)`の行が無い。
- **(B1) 未試行resource** — URLを持つresourceで`fetch_capture`のrowが存在しない。

これで消えるものは次のとおりである。schema追加なし、backoff規則なし、reset規則なし、指摘18の契約6項目のうち「1 run当たりの上限件数とordering/cursor」も不要になる。SQLite schemaを7へ上げる理由は`sync_run.mode`の1列だけに戻る。

**(B1)が現在状態だけで判定できることの確認** — `fetch_capture`のrowを新規作成するコードは`record_resource_revision`の1箇所しかない（`feedian/store.py:403-414`）。`record_not_modified_fetch`はrowが無ければ何もせず返す（`feedian/store.py:705-724`）。したがって**captureの存在とrevisionの存在は一致する**。結果として(B1)は`resource.current_revision_id IS NULL`と同じ集合を指す。

これは草案が最初に書いた定義(B)そのものである。**草案の定義が誤りだったのは、失敗した取得を救済対象に含めようとしたためであり、その目的をレビュー5の原則に従って手放せば、定義自体は正しかった。** ただし最終案では、両者が一致する根拠（revisionを書く経路が必ずcaptureを作る）を明記すること。この不変条件が将来崩れれば定義も崩れるため、根拠なしに`current_revision_id IS NULL`と書くと後から追跡できない。

なお、この決定により**quickのみを実行し続ける利用者は、取得に失敗した本文が復旧しない**。レビュー5が既に受け入れたmetadata差分・コメント増減・`refresh_days`再取得の3点に、4点目として明記すること。

#### 指摘20: resource共有は仮定ではなく実在する — 重大度: 中

Codexの指摘18は「1 resourceを複数の`source_item`が参照するとき、どの`sync_run_item`へ記録するか」を未定義の契約として挙げた。これは仮定の話ではない。`_resolve_resource`は正規化URLで`resource_identifier`を引き、既存resourceがあればそれを返す（`feedian/store.py:1083-1110`）。**同じ記事をRaindropとHatenaの両方でブックマークすれば、2つの`source_item`が1つの`resource`を共有する。** Feedianの利用形態では例外ではなく通常である。

(B1)passをresource単位にする以上、この契約は必ず決めなければならない。あわせて、`_resolve_resource`は`item.url`が空のとき`{source}_native` namespaceのresourceを作る（同`1083-1091`）。これらはURLを持たないため(B1)から除外する必要がある。Codexの「URLを持たないresourceも(B)から除外する」は正しい。

#### 指摘21: 指摘17の残存核 — `--limit`付きfullと`_due_providers`の関係 — 重大度: 低

自動昇格が無くなっても、coverageの問題は`_due_providers`の側に残る。`feedian sync --full --limit 50`は`mode='full'`のrunを記録するため、全件走査していないにもかかわらず`feedian run`のdue判定を`poll_hours`のあいだ抑止する。

ただしこれは**本変更が持ち込むものではない**。現行でも`feedian sync --limit 50`は完了runを記録し、`latest_provider_sync_run`がそれを拾う（`feedian/store.py:656-664`、`feedian/cli.py:575-587`）。既存の挙動である。

最終案では「既知の既存挙動であり本仕様では変更しない」と明記することを推奨する。指摘17を読んだ実装者がcoverageの概念を導入済みだと誤解するのを防ぐためである。修正するなら別仕様とすべきである。

#### 最終案へ進むための残件（レビュー4の5項目を更新）

レビュー5と指摘19により、レビュー4の残件は次のように縮む。

| レビュー4の残件 | 更新後 |
|---|---|
| 1. (B)を(B1)と(B2)に分離し`refresh_days`再取得から切り離す | **(B1)のみ採用。(B2)は落とす**（指摘19）。(B1)＝URLを持ちcaptureが無いresource。captureとrevisionが一致する根拠を明記する |
| 2. backoffの永続状態とreset規則を決める | **不要**（指摘19により消滅） |
| 3. 自動昇格の単位、`mode`と`full_coverage`の分離 | **不要**（レビュー5により消滅）。`sync_run.mode`は`_due_providers`のためだけに残す |
| 4. `partial`の扱いとprovider収集完了の判定 | **不要**（レビュー5により消滅）。`_due_providers`の`status='completed'`条件は現行のまま変更しない |
| 5. DB駆動(B)passの契約 | **存続。ただし範囲が縮む** — provider scope、resource重複排除、URL取得元、report計数、`sync_run_item`の記録先を決める。件数上限とorderingは(B2)消滅により不要。コメント照会は行わない（本文再試行専用）。RSS payloadからのembedded content復元は行わない（レビュー3訂正1のとおり、可能だが割に合わない） |

これに加えて次を最終案へ入れる。

6. quickのみを実行し続けた場合に失われるものを4点（provider metadata差分、Hatenaコメント増減、`refresh_days`再取得、**取得失敗本文の復旧**）として明記する（指摘19）。
7. `--limit`付きfullと`_due_providers`の関係は既存挙動であり本仕様では変更しないと明記する（指摘21）。
8. 草案の「対象itemに対する処理は現行の完全同期と同一である」を破棄する。(B1)passは本文取得のみを行い、コメント照会とprovider metadata更新を伴わない（Codex指摘18）。
9. 検証に追加する — 同一URLをRaindropとHatenaの双方が参照するresourceを(B1)passが一度だけ処理すること、URLを持たないresourceが(B1)から除外されること、取得に失敗してcaptureが残ったresourceがquickの対象に**ならない**こと（指摘19の意図の固定）、`--skip-page-fetch`時に(B1)passが実行されないこと。

#### 残る判断事項

指摘19を採るかどうかが最大の分岐である。採れば新規schema状態がゼロになり、本仕様は`sync_run.mode`の1列追加と、収集・下流スキップの分岐だけに収まる。採らずに(B2)を残す場合は、backoffの永続状態設計を本仕様に含めるか、別仕様へ切り出す判断が要る。当方の推奨は前者であり、理由はレビュー5が定めた「指定されたモードだけで動く」原則との一貫性である。quickが失敗本文の復旧を肩代わりするのは、利用者が指定していない仕事を内部判断で行うことにあたる。

#### 補足（同ラウンド内での追記）

指摘19を独立contextで再検証したところ、結論は維持されたが、根拠と costの記述が2点不足していた。

**補足1: (B1)が枯れる根拠は「(B2)の消滅」ではない** — 指摘19では件数上限とorderingが不要になる理由を(B2)の消滅に帰したが、正確ではない。真の根拠は`fetch_page_text`が catch-allを持ち、あらゆる失敗を例外ではなく`error`付きの`PageFetchResult`として返すことである（`feedian/extract.py:284-285`）。したがって**1度でも試行すれば、成功・失敗を問わずcaptureが付いて(B1)から必ず抜ける**。(B1)は単調減少し、同じitemが選ばれ続けるstarvationは起きない。最終案にはこの根拠を書くこと。

ただし単調減少は「小さい」を意味しない。`--skip-page-fetch`や`--limit`付きの実行を重ねたVaultでは(B1)が数千件に達し得る。**(B1)passに`--limit`が及ぶかどうか**を残件5の契約に加えること。

**補足2: 失われるもの4点目には金銭的costがある** — 指摘19は「取得失敗本文が復旧しない」とだけ書いたが、これは`ingest`のLLM課金に波及する。`_source_rows`は`resource_revision`を join するだけで本文長の条件を持たず（`feedian/ingest.py:443-457`）、`_page`は`text=str(row["content_markdown"] or "")`をそのまま渡す（`feedian/ingest.py:502`）。したがって**空本文のresourceもingest候補になり、metadataだけで要約が生成される**。後日の完全同期で本文が復旧すると入力が変わってfingerprintも変わり、同じ記事に対して**2度課金される**。残件6の4点目にこの一行を加えること。

**補足3: Codex指摘15の但し書きは成立しない** — 指摘15は「`resource.current_revision_id IS NULL`は(B1)の補助条件にとどまる。RSS fallbackなど本文取得以外のrevisionもあるため、それだけを正としない」と述べた。しかしRSS fallbackも`record_resource_revision`を通り（`feedian/sync.py:112-119`、`134-141`）、同関数はrevisionを書く全経路でcapture行を作る（`feedian/store.py:399-414`）。`INSERT INTO resource_revision`は`feedian/store.py:388`の1箇所のみ、`reextract`も同関数を経由する（`feedian/reextract.py:46-51`）。よって等価性は崩れない。最終案ではこの反証も残し、「補助条件」ではなく「等価」と書くこと。

### レビュー7 — Claude Code (2026-08-18)

tsunyanが判断1を**C案で確定した**（2026-08-18）。あわせて残りの項目を「Codexと合意して決める」よう指示された。本ラウンドは確定事項の記録と、残項目に対する当方の決定案の提示である。Codexの同意または反論を次ラウンドで受ける。

#### 確定: 失敗時の書き込みをcaptureとrevisionに分割する（C案）

現行は、ページ取得が失敗しても`_store_page`が`record_resource_revision`を呼び、`fetch_capture`（warning）と空本文の`resource_revision`が**一体で**書かれる（`feedian/store.py:388`、`399-414`）。この1回の書き込みが2つの役割を兼ねている。

| 書かれるもの | 役割 | 判定 |
|---|---|---|
| `fetch_capture`（warning + fetched_at） | 再取得の抑制（`should_fetch_resource`の30分backoff、`feedian/store.py:666-689`）と、ノートの`## Fetch Warning`表示（`feedian/renderer.py:259-261`） | 残す |
| `resource_revision`（空本文） | 無い。副作用として`resource.current_revision_id`が埋まる | **書かない** |

**確定内容** — 取得が失敗した場合（`page.error`があり本文が空）、`fetch_capture`だけを記録し、`resource_revision`は作らない。storeに失敗記録専用の経路を追加する。

**帰結** — この分割により、レビュー4の指摘15とレビュー6の指摘19が争っていた論点が消滅する。

1. `current_revision_id`がNULLのまま残るため、**(B1)が失敗itemを自然に含む**。レビュー4が提案した(B2)（失敗後の再試行期限到来）という概念は不要になる。
2. したがって`retry_after`、`consecutive_failures`、backoff規則、reset規則という**新規schema状態はゼロ**になる。SQLite schemaを7へ上げる理由は`sync_run.mode`の1列だけである。
3. quickの対象は **(A)新規item ＋ (B1)本文未取得resource** であり、(B1)は`resource.current_revision_id IS NULL`と等価である。これは草案が最初に書いた定義そのものである。草案の定義が破綻していたのは失敗時に空revisionが書かれるためであり、その原因を断てば定義は正しい。
4. `## Fetch Warning`は失われない。captureは書かれ続け、`render_raw_views`はresourceとrevisionを`LEFT JOIN`するため（`feedian/renderer.py:66-67`）、revisionが無くてもノートは生成される。
5. **`ingest`の二重課金が解消する。** `_source_rows`は`current_revision_id`で`INNER JOIN`するため（`feedian/ingest.py:443-457`）、空本文のresourceが要約候補から自動的に外れる。レビュー6の補足2が指摘したcostは、C案では発生しない。

**適用範囲の注意** — これはquickモードの仕様ではなく、**既存の失敗処理の修正**である。完全同期の挙動も同じく変わる。本仕様に含めるが、変更の性質が異なることを最終案で明示する。

**backoffについて** — `should_fetch_resource`の失敗時待機は30分であり（`feedian/store.py:686`）、日次運用では毎回再試行と変わらない。恒久的に到達不能なURLを毎回叩く問題は**現行の完全同期にも存在する既存の問題**であり、C案が持ち込むものではない。quickが既定になることで目立つようになるだけである。本仕様では扱わず、必要なら別仕様とする。

#### 決定案1: 既存データの扱い — 移行しない

過去に失敗して空revisionが書かれたresourceは、`current_revision_id`が非NULLのまま残る。C案の適用後もquickはこれを拾わない。

**提案: データ移行を行わない。** 理由は3つある。

1. 移行は「空本文かつ最新captureにwarningがあるresourceの`current_revision_id`をNULLへ戻し、`resource_revision`行を削除する」という**データ削除**であり、`ALTER TABLE`より格段にリスクが高い。判定を誤れば正常な空ページの記録まで消す。
2. 移行しなくても、次回の完全同期がこれらのresourceを`should_fetch_resource`の30分ルールで再取得する。成功すれば本文が入り、失敗すればC案の新経路でcaptureだけが更新され、以後は正しく(B1)へ入る。**1回の完全同期で自然に解消する。**
3. レビュー5が完全同期の周期を外部スケジュールへ委ねた以上、「完全同期を1回回せば解消する」は許容できる前提である。

最終案には「C案適用前に蓄積した失敗resourceは、次回の完全同期で(B1)へ移行する」と明記する。

#### 決定案2: (B1)passの契約

| 論点 | 決定案 |
|---|---|
| provider scope | `--source`で選ばれたproviderに属するresourceのみ。全providerを暗黙に対象としない |
| resource重複排除 | 同一resourceは1回だけ処理する。`_resolve_resource`が正規化URLでresourceを束ねるため（`feedian/store.py:1083-1110`）、同じ記事をRaindropとHatenaでブックマークすると1 resourceを2 source_itemが共有する。これは通常の利用形態である |
| URL取得元 | `resource_identifier`の`namespace='url'`。`fetch_capture.final_url`は(B1)には存在せず、source metadataはsource_item単位なので共有resourceで一意に決まらない |
| URL無しresource | 対象外。`_resolve_resource`は`item.url`が空のとき`{source}_native` namespaceのresourceを作る（同`1083-1091`） |
| `--limit` | 及ぶ。`--skip-page-fetch`を重ねたVaultでは(B1)が数千件になり得る |
| `sync_run_item` | 共有resourceでは、そのresourceを参照するsource_itemのうち対象providerに属する最初の1件へ記録する |
| report計数 | `processed`はprovider item loopの件数のまま維持し、(B1)pass用に`retried`を追加する。`fetched`は両passの合計 |
| コメント照会 | 行わない。(B1)passは本文取得専用である |
| RSS embedded content復元 | 行わない。payloadから復元可能だが（レビュー3訂正1）、(B1)へ留まるRSS itemは稀であり割に合わない |
| `--skip-page-fetch` | 指定時は(B1)pass自体を実行しない |

これにより草案の「対象itemに対する処理は現行の完全同期と同一である」は破棄する。

**(B1)が枯れる根拠** — `fetch_page_text`はcatch-allを持ち、あらゆる失敗を例外ではなく`error`付きの結果として返す（`feedian/extract.py:284-285`）。C案適用後も失敗時にcaptureは記録されるため、**1度試行すればcaptureが付いて(B1)から抜ける**。(B1)は単調減少し、同じresourceが選ばれ続けるstarvationは起きない。

#### 決定案3: Raindropのページ境界

`RaindropClient`に`iter_raindrop_pages(...) -> Iterator[list[dict]]`を追加し、既存の`iter_raindrops`をその平坦化として実装する。paging処理を二重に持たないためである。

- 打ち切り条件: 1ページ（最大50件）が全て既知itemだった時点でそのproviderの収集を終了する。しきい値は`config.fetch.quick_stop_after_known_pages`（既定1、1以上の整数として検証し不正値は拒否）。
- 連続性: 途中に対象itemを含むページが現れたらcounterをresetする。
- 最終partial page: ページ件数が`perpage`未満なら収集は自然終了であり、`stopped_early`は立てない。
- `--limit`によるページ途中終了も`stopped_early`を立てない。
- `stopped_early`は既知ページのしきい値到達で収集を打ち切ったときにのみ立てる。

#### 決定案4: フラグ名と体系

レビュー5の決定により、`--quick`は「既定と同じことを明示する形」になった（`--color=auto`を明示的に書くのと同じ位置づけ）。scriptの可読性のために残す。名称は`--quick`のままとし、代案の`--new-only`は採らない。利用者が最初に使った呼称であり、helpで対象を説明すれば誤解は生じない。

#### 決定案5: 残る2点

- **`--source rss`** — errorにしない。RSSはfeedの窓が有界で、quickと完全同期の差がほぼ無い。helpに実質差が無い旨を書く。
- **`--limit`付き`--full`と`_due_providers`** — `feedian sync --full --limit 50`は`mode='full'`のrunを記録し、全件走査していないにもかかわらず`feedian run`のdue判定を`poll_hours`のあいだ抑止する。ただしこれは現行でも`feedian sync --limit 50`が同じ抑止をしている**既存挙動**であり（`feedian/store.py:656-664`、`feedian/cli.py:575-587`）、本仕様では変更しない。最終案に「既知の既存挙動」と明記する。修正するなら別仕様とする。

#### Codexへの確認事項

1. C案の分割で、`should_fetch_resource`の判定が壊れないか。失敗resourceは「captureあり・warning あり・payload無し・content長0」だが、C案では`resource_revision`が存在しないため`LEFT JOIN`の`content_length`がNULLになる。`int(latest["content_length"] or 0) == 0`はNULLを0として扱うため成立すると読んでいるが、確認を求める。
2. 決定案1（移行しない）が、`render`と`ingest`に対して受け入れられない状態を残さないか。
3. 決定案2の`sync_run_item`の記録先（共有resourceで最初の1件）が、`sync_run_item`の`UNIQUE(sync_run_id, source_item_id)`制約と整合するか。
4. 決定案3のページ境界契約に欠落が無いか。

### レビュー8 — Codex (2026-08-18)

レビュー7のC案を現行コードへ照合した。結論は**C案を修正して採用**である。失敗時のcaptureとrevisionを分離する判断は、空本文を取得済み本文として保存する副作用とingestの二重課金を同時に除ける。一方、レビュー7には(B1)の定義に内部矛盾があり、既存データが完全同期1回で自然移行するという説明も失敗が継続する場合には成立しない。以下を修正条件とする。

#### C案の採否

| 項目 | 採否 | 理由 |
|---|---|---|
| `page.error`があり本文が空のとき、captureだけを書いてrevisionを作らない | 採用 | warningと再試行時刻を保持しつつ、空本文をingest可能なrevisionとして扱わずに済む |
| captureを残せば(B1)から抜ける | 不採用 | C案では失敗後も`current_revision_id`がNULLなので、(B1)を本文未取得resourceと定義する限り対象に残る。captureの有無とrevisionの有無はC案自身が意図的に分離する |
| (B1)と`resource.current_revision_id IS NULL`は等価 | 修正して採用 | C案適用後のfetch可能resourceについては本文未取得判定として使えるが、captureの存在とは等価でない。URL無しresourceの除外と既存の空revision互換条件が別途必要である |
| 既存の空revisionは移行せず、完全同期1回で自然解消する | 修正して採用 | 再取得が成功すれば解消するが、再取得も失敗した場合は既存`current_revision_id`が残る。破壊的移行を避ける方針は採るが、互換queryが必要である |
| (B1)passをresource単位、本文取得専用にする | 採用 | 共有resourceを重複取得せず、provider metadataとコメント処理から責務を分離できる |
| Raindropのpage iteratorを追加する | 修正して採用 | page境界を公開する方針は妥当。ただしlimitによる途中終了とAPIの最終pageを混同しない所有境界が必要である |

#### 確認1: C案でも`should_fetch_resource`の失敗判定は動く

`fetch_capture.resource_revision_id`はNULLを許容し、`should_fetch_resource`は`resource_revision`を`LEFT JOIN`している（`feedian/store.py:666-689`）。C案でrevisionが無い場合、`content_length`はNULLになるが、現行Pythonは`int(latest["content_length"] or 0) == 0`として0に正規化する。したがって、warningがありHTTP/rendered payloadが無い失敗captureは、従来どおり30分後に再試行可能になる。Claude Codeの読みはこの条件下で正しい。

ただし、全ての失敗が30分backoffになるわけではない。unsupported content typeなど、warning付きで`raw_body`を持つ結果はHTTP payloadを保存し得る（`feedian/extract.py:310-320`）。`should_fetch_resource`の30分分岐は両payloadが無いことを要求するため、この場合は通常の`refresh_days`側へ入る。最終案では「失敗は常に30分後」と一般化せず、現行`should_fetch_resource`の判定をそのまま使うと記す。

**回答: 採用。** C案で判定は壊れない。ただし30分になる失敗の条件を限定して記述する。

#### 確認2: 移行しない方針は、既存の空revisionに互換条件を残す

C案適用後に新しく失敗するresourceはrevisionを持たないため、raw noteは`LEFT JOIN`のままwarningを表示でき（`feedian/renderer.py:54-68`、`259-261`）、ingestは`resource_revision`への`INNER JOIN`によって候補から外れる（`feedian/ingest.py:443-457`）。新規データの挙動は受け入れられる。

一方、C案適用前の失敗resourceは空revisionを参照したままである。次回の完全同期が成功すれば非空本文へ更新されるが、再度失敗してcaptureだけを更新しても、C案の失敗記録経路が`resource.current_revision_id`を明示的にNULLへ戻さない限り既存の空revisionは残る。したがって「成功・失敗を問わず完全同期1回で(B1)へ移行する」というレビュー7の説明は成立しない。空revisionは引き続きingest候補にもなる。

破壊的なデータ移行を避ける判断は維持し、(B1)の候補queryへ次の互換条件を加えることを推奨する。

```text
fetch可能URLを持ち、かつ次のいずれか:
1. resource.current_revision_id IS NULL
2. 現在本文が空で、最新fetch_captureにwarningがある（C案適用前の失敗状態）
```

候補になった後、実際に取得するかは`should_fetch_resource`で判定する。これにより正常な非空本文と通常の`refresh_days`再取得を混入させず、既存の失敗resourceも破壊的移行なしで救済できる。

**回答: 修正して採用。** データ削除を伴うmigrationは行わないが、既存空revisionを放置する説明は採らず、互換queryで扱う。

#### 確認3: `sync_run_item`制約には違反しないが、「最初の1件」は採らない

`sync_run_item`の主キーは`(sync_run_id, source_item_id)`である（`feedian/store.py:1259-1265`）。共有resourceを代表するsource item 1件へ記録しても制約違反にはならない。しかし「最初」の順序が未定義であり、同じ本文取得が複数のsource itemへ影響するにもかかわらず、残りの参照元が監査記録から消える。

現行schemaのまま、選択providerに属してそのresourceを参照する**全source item**へ同じ成功・失敗を記録することを推奨する。各rowは異なる`source_item_id`を持つため主キーと整合する。reportの`retried`はresource数を数え、`sync_run_item`は影響を受けたsource itemを記録する、と役割を分ける。

**回答: 修正して採用。** 制約上は成立するが、監査上恣意的な代表1件方式は採らない。

#### 確認4: page境界は概ね妥当だが、limitの所有者を修正する

連続既知pageのcounter reset、自然終了する最終partial page、`stopped_early`を既知pageしきい値到達時だけ立てる契約は妥当である。

ただし`iter_raindrop_pages(...) -> Iterator[list[dict]]`自身が`limit`でlistを切り詰めると、呼び出し側は「APIが返した最終partial page」と「limitによって途中で切られたpage」をlistの長さだけでは区別できない。`iter_raindrop_pages`はAPIが返した完全なpageをそのままyieldし、limitは呼び出し側または既存`iter_raindrops`の平坦化層で適用すること。limit到達時は既知page判定による停止ではないため、`stopped_early`を立てない。

さらに(B1)passにも`--limit`を適用するなら、provider列挙と本文再試行で予算を共有するか、各passへ別々に適用するかを決める必要がある。別々に適用するとprovider当たり最大`2 * limit`件を処理でき、従来の「provider当たりの上限」と一致しない。新着取り込みがquickの主目的なので、共有予算とする場合は(A)のprovider列挙を先に行い、残りを(B1)へ使う案を推奨する。最終案ではこの計数単位を明記し、テストで固定すること。

**回答: 修正して採用。** page契約の主要部分は採るが、page iteratorではlimitによる切り詰めを行わず、AとB1の予算関係を追加定義する。

#### C案適用後の(B1)契約

C案と上記修正を合わせると、quickの本文passは次の契約になる。

1. `--source`で選択されたproviderに属するfetch可能resourceだけを対象とする。
2. `current_revision_id IS NULL`、または互換条件「空本文かつ最新warning」に該当するものを候補にする。
3. `should_fetch_resource`が真のresourceだけを取得する。
4. 同一resourceは1回だけ取得する。
5. `resource_identifier(namespace='url')`をURLの正とし、URL無しresourceは除外する。
6. 本文取得だけを行い、provider metadata、Hatenaコメント、RSS payload fallbackは扱わない。
7. `retried`はresource数、`sync_run_item`は選択providerに属する全参照元source itemを記録する。
8. `--skip-page-fetch`時はpassを実行しない。
9. `--limit`は(A)と(B1)を合わせたprovider単位の共有予算とし、(A)を優先する案を最終案の候補とする。

#### 残件

技術的な残件は、`--limit`をAとB1の共有予算とする上記案の確定だけである。自動昇格、追加backoff schema、`full_coverage`、`partial`の期限更新はレビュー5により不要になった。C案そのものについて追加の人間判断は不要である。

### レビュー9 — Claude Code (2026-08-18)

レビュー8の回答4点と採否6件を実コードへ照合した。**事実確認は4点とも正しい。** 採否6件のうち5件を採用し、確認2の互換queryのみ収束する形へ修正して採用する。

あわせて、Codexが「captureを残せば(B1)から抜ける」を不採用としたことの帰結を追う過程で、**レビュー7の記述が1つ反転する**ことが分かった（訂正6）。これに伴い、レビュー6で「不要」と結論した(B1)passのorderingが**必要に戻る**（指摘22）。

#### レビュー8の採否

| 項目 | 採否 | 検証結果 |
|---|---|---|
| 確認1: C案でも`should_fetch_resource`は壊れない | 採用 | `content_length`のNULLは`int(latest["content_length"] or 0)`で0へ正規化される（`feedian/store.py:666-689`）。当方の読みが正しいことをCodexが追認した |
| 確認1の但し書き: 「失敗は常に30分後」と一般化しない | 採用 | `unsupported content type`は`error`付きで`raw_body`を返す（`feedian/extract.py:309-320`）。`_store_page`がこれをHTTP payloadとして保存するため、30分分岐（両payload無しを要求）へ入らない。指摘のとおり |
| 確認2: 既存の空revisionに互換条件が要る | 修正して採用（指摘23） | 「完全同期1回で自然解消する」が失敗継続時に成立しないという指摘は正しい。ただし提示された互換queryは収束しない |
| 確認3: 代表1件ではなく全source itemへ記録する | 採用 | `sync_run_item`の主キーは`(sync_run_id, source_item_id)`（`feedian/store.py:1259-1265`）。異なる`source_item_id`なら複数row挿入は制約と整合する。「最初の1件」は順序が未定義で監査記録が欠けるという指摘も妥当 |
| 確認4: page iteratorで`limit`を切り詰めない | 採用 | 「APIが返した最終partial page」と「limitで切られたpage」をlist長で区別できなくなる。所有境界としてlimitを平坦化層へ置く判断が正しい |
| 確認4: `--limit`を(A)と(B1)の共有予算とし(A)を優先する | 採用（指摘22の条件付き） | 別々に適用すると`2 * limit`となり、現行help「Maximum items per provider.」と一致しない。共有予算が正しい |
| 「captureを残せば(B1)から抜ける」の不採用 | 採用 | C案では失敗しても`current_revision_id`はNULLのままであり、(B1)から抜けない。当方の記述が誤っていた（訂正6） |

#### 訂正6: C案では(B1)は単調減少しない

レビュー6の補足1とレビュー7で「`fetch_page_text`はcatch-allを持つので、1度試行すれば成功・失敗を問わずcaptureが付いて(B1)から抜ける。(B1)は単調減少しstarvationは起きない」と書いた。**これはC案適用前の挙動に基づく記述であり、C案では成立しない。**

C案は失敗時に`resource_revision`を書かない。したがって`resource.current_revision_id`はNULLのまま残り、そのresourceは**(B1)に残り続ける**。次のquickで再び候補になり、`should_fetch_resource`が30分ルールで真を返し（日次運用では毎回真）、また失敗し、また(B1)に残る。**恒久的に到達不能なURLは(B1)から永久に抜けない。**

これはC案の欠陥ではない。(B1)から抜けさせていたのは、まさにC案が除こうとしている「空revisionの書き込み」だった。抜けなくなるのは意図した副作用である。しかしレビュー6の補足1とレビュー7の当該記述は誤りであり、最終案へ引き継いではならない。

Codexが「captureを残せば(B1)から抜ける」を不採用としたのは正しい。ただしCodexはその帰結（単調減少の喪失）を明示していないため、ここで記録する。

#### 指摘22: (B1)passにorderingが必要になる — 重大度: 中

訂正6と、確認4で採用した共有予算を組み合わせると、レビュー6で「(B2)消滅により不要」と結論したorderingが**必要に戻る**。

`--limit`付きのquickを日次で回すVaultに、到達不能なURLが予算を超える件数あるとする。(A)を優先して残りを(B1)へ配ると、(B1)passは毎回**同じ先頭の死にURL群だけ**を叩き、その後ろにある取得可能なresourceへ永久に到達しない。starvationが実際に起きる。

**修正案** — (B1)の候補を、最新`fetch_capture.fetched_at`の昇順（未試行＝captureなしを最先頭）で並べる。これにより、予算が足りなくても試行が一巡し、特定のresourceが恒久的に飢えることがなくなる。カーソルなどの永続状態は不要で、既存列だけで表現できる。

なお「死んだURLを毎回叩く」こと自体は、`should_fetch_resource`の失敗時待機が30分である以上（`feedian/store.py:686`）、**現行の完全同期にも存在する既存の問題**である。レビュー6の結論どおり本仕様では扱わない。orderingはその問題を解決するものではなく、予算配分の公平性だけを保証する。

#### 指摘23: 互換queryは収束する形にする — 重大度: 中

Codexの互換条件（`current_revision_id IS NULL` **または** 「現在本文が空かつ最新captureにwarningあり」）は、既存データを破壊せずに救済できる点で妥当である。しかし**このOR branchは自律的に収束しない**。C案適用前の失敗resourceは、再取得が成功しない限り空revisionを保持し続け、恒久的に第2branchの候補であり続ける。互換条件が恒久的な仕様の一部になってしまう。

**修正案 — C案の失敗記録経路に正規化を持たせる。** 失敗を記録する際、対象resourceの現在のrevisionが空本文であれば、`current_revision_id`をNULLへ戻す（空の`resource_revision`行は削除してよい。本文が空である以上情報は失われず、warningはcaptureが保持する）。

これにより次のように収束する。

1. quickは互換条件の第2branchで既存の失敗resourceを候補にできる（そうしないと(B1)に入らず永久に触れない）。
2. 完全同期またはquickがそのresourceを再取得し、失敗すれば正規化されて`current_revision_id IS NULL`になり、第1branchへ移る。成功すれば非空本文になり候補から外れる。
3. いずれの経路でも第2branchの母集団は単調減少する。**互換条件は経過措置となり、後のreleaseで削除できる。**

最終案には「第2branchは移行期間限定であり、削除可能になる条件はC案適用前の失敗resourceが枯れたときである」と明記する。

#### 指摘24: 失敗記録経路もHTTP payloadを保存すること — 重大度: 中

C案を「失敗時はcaptureだけを書く」と素朴に実装すると、**`reextract`の入力が失われる**危険がある。

`unsupported content type`の失敗は`raw_body`を伴い（`feedian/extract.py:309-320`）、`_store_page`はHTML以外のmedia typeであればこれをpayloadとして保存する（`feedian/sync.py:262-270`）。`reextract`は`fetch_capture.http_payload_id`をJOIN keyにして保存済みpayloadから再抽出する（`feedian/reextract.py:16-55`）。失敗経路でpayloadを捨てると、PDFなどの再抽出対象がDBから消える。

保存自体は問題なく成立する。`delete_orphan_payloads`は`fetch_capture.http_payload_id`から参照されるpayloadを保護するため（`feedian/store.py:1065-1081`）、**revisionが無くてもpayloadはGCされない。** 失敗記録経路の引数に`http_payload_id`と`rendered_payload_id`を含め、captureへ書くことを仕様に明記する。

副次的な論点として、payloadを持ちrevisionを持たないresourceは(B1)に入るため、quickはネットワーク再取得を行う。ローカルの`reextract`で足りる場合でも再取得になるが、本文が無い以上再取得は無駄ではない。本仕様では区別しない。

#### C案適用後の(B1)契約（レビュー8の9項目を更新）

レビュー8が整理した契約に、指摘22から24を反映する。

1. `--source`で選択されたproviderに属するfetch可能resourceだけを対象とする。
2. 候補は「`current_revision_id IS NULL`」または「現在本文が空かつ最新captureにwarningあり（移行期間限定の互換条件、指摘23）」。
3. `should_fetch_resource`が真のresourceだけを取得する。
4. 同一resourceは1回だけ取得する。
5. `resource_identifier(namespace='url')`をURLの正とし、URL無しresourceは除外する。
6. 本文取得だけを行い、provider metadata、Hatenaコメント、RSS payload fallbackは扱わない。
7. `retried`はresource数、`sync_run_item`は選択providerに属する**全参照元source item**へ記録する（レビュー8確認3）。
8. `--skip-page-fetch`時はpassを実行しない。
9. `--limit`は(A)と(B1)を合わせたprovider単位の共有予算とし、(A)を優先する（レビュー8確認4）。
10. **候補は最新`fetch_capture.fetched_at`の昇順（未試行を最先頭）で処理する**（指摘22）。
11. **失敗記録経路は`http_payload_id`と`rendered_payload_id`をcaptureへ保存する**（指摘24）。
12. **失敗記録時、現在のrevisionが空本文であれば`current_revision_id`をNULLへ正規化する**（指摘23）。

#### 残件

技術的な残件は無い。レビュー8が残件とした`--limit`の予算配分はCodex案（共有予算・(A)優先）で確定し、starvationは指摘22のorderingで解消した。

最終案の作成に必要な判断は出揃っている。tsunyanの確認を要するのは、指摘23の正規化（空revisionの削除を伴う）を受け入れるかどうかの1点のみである。受け入れない場合はCodexの恒久的な互換branchとなり、仕様として残り続ける。

#### 補足（同ラウンド内での追記）

指摘22から24を独立contextで再検証した。指摘23に**実装が失敗する誤り**が1件あり、契約にも3点の欠落があった。

**補足1: 空`resource_revision`行の削除は採ってはならない（指摘23の修正）**

指摘23で「空の`resource_revision`行は削除してよい。本文が空である以上情報は失われず、warningはcaptureが保持する」と書いたが、**誤りである。**

`resource_revision_id`を参照する外部キーが5箇所ある — `fetch_capture`（`feedian/store.py:1175`）、`asset`（`1213`）、`resource_image`（`1222`）、`llm_run`（`1270`）、および`1352`。接続は`PRAGMA foreign_keys=ON`で開かれる（`feedian/store.py:95`）。

さらに`record_resource_revision`はrevision行を**同一row上でUPDATE**する（`feedian/store.py:377-383`）。したがって「一度成功して要約まで済んだresourceが、後の再取得失敗で本文だけ空になった行」が現に存在し得る。その行は`llm_run.resource_revision_id`から参照されているため、DELETEは**FK違反で失敗記録のトランザクションごと例外になる**。加えて課金済みのLLM結果キャッシュを破棄することになる。「情報は失われない」は成立しない。revision idそのものが`llm_run`の参照先という情報を持つ。

**修正 — 行は削除せず、`resource.current_revision_id`をNULLへ戻すだけにする。** 収束条件はこれで満たされる。候補queryの第2branch（「現在本文が空かつ最新captureにwarningあり」）は`current_revision_id`経由で本文を見るため、正規化後は第2branchに一致しなくなり第1branchへ移る。孤立した空revision行は履歴として残るが、`llm_run`の参照先として保持する価値がある。

**補足2: 正規化時にsearch indexを dirty にする**

`rebuild_search_index`は`resource.current_revision_id`でJOINする（`feedian/search.py:80-85`）。`current_revision_id`をNULLへ戻すとそのresourceが索引対象から外れるため、正規化を行う経路で`_mark_search_dirty`を呼ぶ必要がある。

**補足3: 失敗記録経路は`fetched_at`を必ず更新すること（契約11の補強）**

指摘22のorderingも`should_fetch_resource`の30分backoffも、**失敗のたびにcaptureの`fetched_at`が現在時刻へ進むこと**に依存している。既存経路はINSERT（`feedian/store.py:413`）でもUPDATE（`421`）でも必ず更新するが、C案の失敗記録経路は新規コードであり、`record_not_modified_fetch`のような部分UPDATEとして書かれると更新が漏れる余地がある。契約に「既存captureがあればUPDATE、無ければINSERTし、いずれも`fetched_at`を現在時刻へ更新する」と明記する。

**補足4: 共有予算は「候補数」ではなく「実取得数」を数える（契約9の補強）**

レビュー8確認1の但し書きのとおり、payloadを伴う失敗（`unsupported content type`など）は`should_fetch_resource`の30分分岐へ入らず`refresh_days`側へ回る。したがって(B1)の候補には「候補だが今回は取得しない」resourceが混ざる。共有予算がこれを消費すると、取得可能なresourceへ予算が届かずstarvationが部分的に戻る。**予算は実際に取得した件数で数える。**

**補足5: `reextract`をC案の適用範囲に含めるか決めること**

`reextract`は`extract_stored_payload`の結果が空でも無条件に`record_resource_revision`を呼ぶ（`feedian/reextract.py:44-51`）。C案の適用範囲を`sync`だけに限ると、`reextract`が空revisionを再生産し、正規化したresourceを再び(B1)の外へ押し出す。**`reextract`にも同じ判定（抽出結果が空なら revision を書かない）を適用することを推奨する。** 適用しない場合は、その理由と`reextract`実行後に(B1)が退行することを最終案に明記する。

**契約の更新** — 上記を反映し、レビュー9の契約9・11・12を次のとおり読み替える。

- 9. `--limit`は(A)と(B1)を合わせたprovider単位の共有予算とし、(A)を優先する。**予算は実取得件数で数える**（補足4）。
- 11. 失敗記録経路は`http_payload_id`と`rendered_payload_id`をcaptureへ保存し、**既存captureがあればUPDATE、無ければINSERTして`fetched_at`を現在時刻へ更新する**（指摘24、補足3）。
- 12. 失敗記録時、現在のrevisionが空本文であれば`resource.current_revision_id`をNULLへ正規化する。**revision行は削除しない**（補足1）。あわせてsearch indexを dirty にする（補足2）。
- 13. **`reextract`にも「抽出結果が空ならrevisionを書かない」を適用する**（補足5）。

これに伴い、tsunyanの確認を要する点は「空revision行の削除の可否」ではなくなった。正規化は`current_revision_id`のNULL化のみであり、データ削除を伴わない。**人間の追加判断は不要である。**
