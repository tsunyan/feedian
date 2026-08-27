# ドキュメントの実装追従と整理

ステータス: 確定

## 最終案

### 結論

`README.md`を実装へ追従させ、`docs/`に残る規約制定前の文書を整理する。**コードは1行も変更しない。**

現在のREADMEは`llm.backend`導入前・sync quickモード導入前の記述のまま残っており、載っている
コマンドの一部は実行するとCLIエラーになる。原因は分量ではなく、実装と同じ内容を人手で
二重管理したことである。したがって分割ではなく内容の訂正と、二重管理をやめる仕組みに向かう。

`DESIGN.md`は更新しない。挙動が変わらないためである。

### 確定事項

#### 1. READMEを実装へ追従させる（草案A、全項採用）

| 項 | 内容 | 実装の根拠 |
|---|---|---|
| A1 | `sync`に`--quick`（既定）と`--full`を記載し、`--force-fetch`・`--force-comments`が`--full`必須であることを明記する。壊れているレシピを`feedian sync --source all --full --force-fetch --force-comments`へ直す。quickが検出しないものを1段落で述べ、`DESIGN.md`の「Syncのモード」へ導線を張る | `feedian/cli.py:100-113`、`feedian/cli.py:241-242` |
| A2 | `ingest`のオプション表へ`--backend`を追加する。`--provider`は表に載せず、「引き続き受理されるが非推奨」の1行に留める | `feedian/cli.py:187`、`feedian/cli.py:190`、`feedian/llm_backends.py:49` |
| A3 | backendとmodelの解決順を実装どおりに書き換え、backend別の環境変数と既定modelを表にする。modelがconfigを見るのは選択backendが設定backendと一致するときだけである条件を明記する | `feedian/cli.py:758-785` |
| A4 | `config.json`の例を、一時ディレクトリで`feedian init`を実行した実出力から貼る。以後手書きしない | `feedian/vault.py:99-118`、`feedian/vault.py:303-319` |
| A5 | フィールド表へ`llm.backend`、`llm.model`、`llm.fallback.*`、`fetch.timeout_seconds`、`fetch.browser_timeout_seconds`、`fetch.terminal_failure_kinds`、`fetch.terminal_kind_failures`を追加する。fallbackは表の1行では足りないため短い段落を1つ置く | 同上 |
| A6 | 冒頭の「OpenAI or Manus」を4 backendの記述へ改め、`.env`例の`OPENAI_MODEL`を既定の`gpt-5.6-terra`へ揃える | `feedian/llm_backends.py:1385` |

**A1を最優先とする。** READMEどおりに打つとエラーになるコマンドが載っている状態が、
本仕様で直す最も実害のある欠陥である。

#### 2. 用語を固定する（草案B）

`provider`は収集元（Raindrop / Hatena / RSS）だけを指し、LLM実行系は`backend`と呼ぶ。
LLM文脈の`provider`を`backend`へ置換する。ただし環境変数`LLM_PROVIDER`、フラグ`--provider`、
コード上の名前を指す箇所は固有名として残す。「How it works」直後に使い分けの1文を置く。

#### 3. 導線と章順（草案C）

- READMEへ短いブロックを1つ置き、`DESIGN.md`（現在どう動くか）、`docs/specs/`（なぜその決定に
  なったか）、`docs/reviews/`（レビューと採否）の役割と場所を示す。
- `Vault and configuration selection`と`Vault config fields`を`Command reference`の後ろへ移す。
  移動のみで内容は変えない。
- **READMEの分割は行わない。** 破綻の原因は分量ではなく陳腐化であり、分割しても陳腐化は
  直らず辿る段数が増えるだけである。分量が問題として再浮上したら、そのとき単独で判断する。

#### 4. docs/の整理（草案D。D3の保留はレビュー1で解除し、レビュー3で範囲を絞った）

| 項 | 内容 |
|---|---|
| D1 | `docs/specs/llm-backends.ja.md`を削除する。見出し階層と最初の見出し名を除き`20260816-llm-backends.ja.md`の`## 草案`と完全一致するため、失われる内容は無い |
| D2 | `20260816-llm-backends.ja.md`に`## 改訂`を新設し、改訂1として前書きの「元の文書は履歴資料として変更せず残す」を訂正する。前書きは規約が定める改訂対象（`最終案`）ではないが、「確定した仕様書に既知の誤りを残さない」という趣旨を優先して同じ手続きで扱う |
| D3 | `docs/specs/llm-backends.md`（英語版）を削除する。**完了済みレビュー`docs/reviews/20260816-llm-backends-implementation.ja.md:310`は編集せず、ファイルパスをそのまま残す。** そのパスは、誤って根拠にした草案が英語版であったことを特定する歴史的証拠であり、削除後も`git show 7e9d09e:docs/specs/llm-backends.md`で参照できるため、ファイルを消してもレビューの再現性は壊れない |
| D4 | `docs/plans/estimate.md:3`の`REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development ...`を削除する。現行の委譲ドクトリンと矛盾する命令形の指示であり、読んだエージェントが従い得る。文書の残りは実装計画の記録として残す |
| D5 | `AGENTS.md`へ「`docs/plans/`は規約制定前の実装計画の歴史的記録であり、現行の手順ではない。新規に追加しない」の1行を足す |
| D6 | `AGENTS.md`へ「命名規約の制定前に作られた文書は現状のまま残す。改名はしない」の1行を足す。この判断は`docs/reviews/20260816-llm-backends-implementation.ja.md:821`で一度下されているが、規約本体に無いため再燃する。規約へ移して決着させる |

D1がかつて`docs/reviews/20260816-llm-backends-implementation.ja.md:821`で**不採用**になった件は、
そこで却下されたのが「改名」案であること、および`AGENTS.md`に後から`## 改訂`の手続きが加わり
「確定仕様は編集しない」という前提が成り立たなくなったことにより、判断を変える。

#### 5. Legacy direct-export modeは削除しない（レビュー2）

削除の条件「不要であること」が成立しない。legacy経路にしか無く現行CLIに代替が無い機能が
4つある。

| legacy機能 | 現行の代替 |
|---|---|
| `--list-collections` | 無し。**現行configの`providers.raindrop.collection_id`を調べる唯一の手段** |
| `--sync-raindrop-summary` | 無し |
| `--sync-raindrop-tags` | 無し |
| `--rename-existing` | 無し（legacy固有の概念） |

READMEのLegacy direct-export mode節は残し、次の1行を足す。

- `--list-collections`は、現行のVault configで`providers.raindrop.collection_id`を設定する際に
  IDを調べる手段である。

これは現在のREADMEに欠けている情報であり、legacy節を「旧機能の置き場」から「現行でも使う
場面がある」記述へ変える。

legacyの削除自体は、上の4機能を現行へ移すか捨てるかを先に決める**別の仕様**として扱う。
本仕様に含めるとドキュメント修正が機能削除の判断待ちで止まる。判断材料はレビュー2に残した。

#### 6. 触れないもの

- `DESIGN.md`の内容更新と目次追加。
- Quick startの手順5と`enrich-images`節の重複。手順5は「なぜこの工程を挟むか」、コマンド節は
  「何を受け付けるか」を書いており読者が違う。
- コードの挙動、テスト、確定済み仕様の内容（D2の訂正を除く）。完了済みレビュー文書の本文。

### 変更するファイル

| ファイル | 操作 |
|---|---|
| `README.md` | 修正（1〜3、5） |
| `AGENTS.md` | 2行追加（D5、D6） |
| `docs/specs/llm-backends.ja.md` | 削除（D1） |
| `docs/specs/llm-backends.md` | 削除（D3） |
| `docs/specs/20260816-llm-backends.ja.md` | `## 改訂`を新設し改訂1を記録、前書き1文を訂正（D2） |
| `docs/plans/estimate.md` | 3行目を削除（D4） |

### 検証

1. `feedian --help`と各サブコマンドの`--help`を、READMEのusage行およびオプション表と1つずつ
   突き合わせ、差分が無いことを確認する。
2. 一時Vaultで`feedian init`を実行し、生成された`config.json`とREADMEの例の差分がゼロである
   ことを確認する。
3. READMEに載る全コマンド例をargparseに通し、parser errorにならないことを確認する（実行はしない）。
   A1の壊れたレシピはこの検査で検出できなければならない。
4. `README.md`と`docs/`の相対リンクが全て解決すること、特にD1・D3の削除後に
   `llm-backends.ja.md`と`llm-backends.md`への参照が残っていないことを確認する。
5. `.\.venv\Scripts\python.exe -m pytest`

### コミット構成

ブランチは`docs/documentation-consistency`とする。

1. 本仕様書を単独で`docs:`型のコミットにする。
2. 実装を1コミットにまとめる。上の「変更するファイル」7件が同じコミットに入る。
3. **`DESIGN.md`の更新は伴わない。** `AGENTS.md`のライフサイクルは実装コミットに`DESIGN.md`の
   要約更新を含めると定めるが、それは挙動が変わる場合の規定である。

## 改訂

### 改訂1 — Claude Code (2026-08-27)

**対象:** `最終案` 確定事項4の表 D3、および「変更するファイル」の表

**(前)**

> | D3 | `docs/specs/llm-backends.md`（英語版）を削除する。あわせて
> `docs/reviews/20260816-llm-backends-implementation.ja.md:310`から**ファイルパスだけを除去**し、
> 「確定前の草案を参照し」とする。（後略） |

> | `docs/reviews/20260816-llm-backends-implementation.ja.md` | `:310`からファイルパスのみ除去（D3） |

**(後)**

> | D3 | `docs/specs/llm-backends.md`（英語版）を削除する。**完了済みレビュー
> `docs/reviews/20260816-llm-backends-implementation.ja.md:310`は編集せず、ファイルパスを
> そのまま残す。**（後略） |

「変更するファイル」の表から`docs/reviews/20260816-llm-backends-implementation.ja.md`の行を削除した。
あわせて確定事項4の見出しを「レビュー1で D3 の保留を解除」から「D3の保留はレビュー1で解除し、
レビュー3で範囲を絞った」へ、確定事項6の除外を「D2・D3の訂正を除く」から「D2の訂正を除く」へ改め、
完了済みレビュー文書の本文を触れないものとして明記した。

**理由:** 最終案が同文書の`レビュー3`と矛盾していた。`レビュー3`はレビュー1のパス除去案を
**不採用**とし、「英語版の削除自体は採用し、完了済みレビューの`:310`は編集せずそのまま残す」と
決めている。最終案はその判断を反映しないままレビュー1の案を採用しており、確定仕様が
自身のレビュー記録と食い違う状態になっていた。

**根拠:** 原因は手続きの側にある。最終案を書いた時点で`レビュー3`は既にこのファイルへ
追記されていたが（`7b98b37`に含まれる）、執筆者が編集前にファイルを読み直さず、
レビュー1と2だけを前提に書いた。`レビュー3`の論拠自体は成立する。`:310`のパスは、
誤って参照した草案が英語版であったことを特定する情報であり、除去すると
「確定前の草案を参照し」となってどちらの草案か分からなくなる。また当該レビューの対象コミット
`7e9d09e`にはこのファイルが含まれ、`git show 7e9d09e:docs/specs/llm-backends.md`で今も
取得できるため、作業ツリーから削除してもレビューの再現性は損なわれない。

実装側の対応と、この矛盾を検出したPRレビューの採否は
[ドキュメント整合性実装のPRレビュー](../reviews/20260827-documentation-consistency-pr-review.ja.md)に記録した。

## 草案

### 背景

`DESIGN.md`は確定した仕様に追従できているが、`README.md`は`llm.backend`導入前・sync
quickモード導入前の記述のまま残っている。その結果、READMEに載っているコマンドの一部が
**実行するとCLIエラーになる**状態にある。あわせて`docs/`に規約制定前の文書が整理されないまま
残り、片方は現行の委譲ドクトリンと矛盾するエージェント向け指示を含む。

この文書は、コードを変更せずドキュメントだけを実装へ追従させる範囲を定める。

### 対象と対象外

対象は`README.md`、`AGENTS.md`の一部、`docs/`配下の整理である。

対象外は次のとおり。

- コードの挙動変更。本件でコードは1行も変更しない。
- `DESIGN.md`の内容更新。挙動が変わらないため更新すべき記述が無い。
- 確定済み仕様書の内容変更。ただしD2で述べる1文の訂正だけは例外とする。

### 原則

1. **READMEは「現行の使い方」だけを載せる。** 実装が隠しているフラグを再公開しない。
2. **用語を1つに固定する。** `provider`は収集元（Raindrop / Hatena / RSS）だけを指し、
   LLM実行系は`backend`と呼ぶ。フロー図の書き換えで既にこの方向へ動いている。
3. **手で書き写す表は作らない。** 実出力から生成できるものは生成した結果を貼る。今回の
   乖離の大半は、実装と同じ内容を人手で二重管理したことが原因である。

### A. READMEの実装追従

#### A1. syncのquick/fullモード（最優先）

`--quick`（既定）と`--full`がREADMEに一切無い。`feedian/cli.py:100-113`が両フラグを定義し、
`feedian/cli.py:241-242`が`--force-fetch`と`--force-comments`に`--full`を必須としている。

このためREADMEの次の記述が実際に壊れている。

- レシピ「Refresh an article and its Hatena discussion」の
  `feedian sync --source all --force-fetch --force-comments`は、現在parser errorで終了する。
- `status`節だけが`sync --full --force-fetch`に言及しており、READMEのどこにも定義の無い
  フラグを参照している。
- フィールド表の`fetch.quick_stop_after_known_pages`は、quickが何かを説明せずに
  「quick sync中の」と書いている。

変更内容は次のとおり。

- `sync`のusage行へ`[--quick | --full]`を追加する。
- オプション表へ`--quick`（既定）と`--full`を追加し、`--force-fetch`・`--force-comments`の
  行に「`--full`が必須」を明記する。
- 壊れているレシピを`feedian sync --source all --full --force-fetch --force-comments`へ直す。
- quickが検出しないもの（provider側のmetadata編集、コメントの増減、`refresh_days`到達に
  よる再取得、取得失敗本文の復旧）を1段落で述べ、`DESIGN.md`の「Syncのモード」へ導線を張る。
  READMEに判断理由まで書かない。

#### A2. ingestのbackend選択

`feedian/cli.py:187`の`--backend`が現行のフラグであり、選択肢は`feedian/llm_backends.py:49`の
`openai-responses` / `manus-api` / `codex-local` / `claude-code-local`である。
`feedian/cli.py:190`の`--provider`は`argparse.SUPPRESS`で意図的に隠された後方互換エイリアス
であり、READMEはこの隠された方だけを載せている。

- `--backend`を`ingest`のオプション表へ追加する。
- `--provider`はREADMEに載せない。隠されているフラグを文書で再公開すると、後方互換の寿命が
  設計意図より延びるためである。既存利用者向けの記述は「`--provider`は引き続き受理されるが
  非推奨」の1行に留める。

#### A3. backendとmodelの解決順

`feedian/cli.py:758-785`の実際の順序は次のとおりで、READMEの記述と一致しない。

- backend: `--backend` → `--provider`（legacy） → `LLM_BACKEND` → `LLM_PROVIDER`（legacy）
  → `config.llm.backend`
- model: `--model` → backend別の環境変数 → `config.llm.model`（**選択したbackendが設定の
  backendと一致するときだけ**） → backendの既定

READMEは既定を「Built-in default `openai`」と書いているが、実際の既定はVault configの
`llm.backend`である。またmodelの「backendが一致するときだけconfigを見る」条件が抜けており、
これは利用者が実際に踏む種類の落とし穴である（backendを一時的に切り替えると、設定した
modelは使われない）。

backend別の環境変数と既定modelを表にする。

| backend | 環境変数 | 既定model |
|---|---|---|
| `openai-responses` | `OPENAI_MODEL` | `gpt-5.6-terra` |
| `manus-api` | `MANUS_MODEL` | `manus-1.6` |
| `codex-local` | `CODEX_MODEL` | backendの既定に従う |
| `claude-code-local` | `ANTHROPIC_MODEL` | 無し（明示が必須） |

`claude-code-local`でmodel未指定が`BackendPolicyError`になることは
`feedian/cli.py:781-784`にあるため、表の脚注として書く。

#### A4. config.jsonの例を実出力と一致させる

READMEは「`feedian init` creates this structure」と書いているが、`feedian/vault.py:99-118`と
`feedian/vault.py:303-319`が実際に書き出す`fetch`の7キー
（`retry_base_minutes`、`retry_max_days`、`terminal_http_statuses`、`terminal_failure_kinds`、
`terminal_kind_failures`、`timeout_seconds`、`browser_timeout_seconds`）と`llm.fallback`が
例から欠けている。しかも`retry_*`と`terminal_http_statuses`は**下のフィールド表には載っている**
ため、同じREADMEの中で例と表が矛盾している。

- 一時ディレクトリで`feedian init`を実行し、生成された`config.json`をそのまま貼る。
- 以後この例を手書きしない。原則3のとおりである。

#### A5. フィールド表の欠落

表に無い設定を追加する。

- `llm.backend`、`llm.model`
- `llm.fallback.enabled`、`llm.fallback.backend`、`llm.fallback.model`
- `fetch.timeout_seconds`（既定5秒）、`fetch.browser_timeout_seconds`（既定30秒）
- `fetch.terminal_failure_kinds`（既定`["dns", "timeout"]`）、`fetch.terminal_kind_failures`（既定3）

**fallbackはREADME全体で一度も触れられていない。** 表の1行では足りないため、短い段落を
1つ置く。既定で無効であること、有効化にはbackendとmodelの両方を明示すること、切り替わるのは
`BackendUnavailableError`・`BackendRateLimitError`・`BackendTimeoutError`の3つだけで、
認証・ポリシー・プロトコルの失敗では切り替わらないこと、実行は宛先backendの`llm_run`として
別に記録されることを述べる。理由は`DESIGN.md`と仕様書に譲る。

#### A6. 冒頭と`.env`例

- 冒頭の「optionally uses an LLM — OpenAI or Manus」を4 backendの記述へ改める。
- `.env`例の`OPENAI_MODEL=gpt-5.6-luna`を既定の`gpt-5.6-terra`へ揃える。「例であって既定では
  ない」と注記する案もあるが、注記が要らない方を採る。

### B. 用語の統一

- `provider`は収集元だけを指す語として使う。LLM文脈の`provider`は`backend`へ置換する。
- ただし固有名は置換しない。環境変数`LLM_PROVIDER`、フラグ`--provider`、および
  「provider出力スキーマ」のようにコード上の名前を指す箇所はそのまま残す。
- READMEの「How it works」直後に、2語の使い分けを述べる1文を置く。

### C. 構造とナビゲーション

#### C1. READMEからdocsへの導線（採用）

676行のREADMEから`DESIGN.md`にも`docs/`にもリンクが1本も無い。`AGENTS.md`は`DESIGN.md`を
「現在の挙動を記述する唯一の場所」と定めているのに、入口から辿れない。

READMEに短いブロックを1つ置き、`DESIGN.md`（現在どう動くか）、`docs/specs/`（なぜその決定に
なったか）、`docs/reviews/`（レビューと採否）の役割と場所を示す。`AGENTS.md`の役割分担と
同じ言い方を使う。

#### C2. 章順（採用）

100行を超えるVault設定リファレンスがコマンドリファレンスの前に挟まっている。
`Vault and configuration selection`と`Vault config fields`を`Command reference`の後ろへ移す。
読者はまずコマンドを探し、必要になってから設定を引くためである。移動のみで内容は変えない。

### D. docs/の整理

#### D1. `docs/specs/llm-backends.ja.md`の削除（採用）

見出しの階層と最初の見出し名を除き、`20260816-llm-backends.ja.md`の`## 草案`セクションと
完全に一致する。差分は「状態」→「位置づけ」の1行と末尾の空行だけである。したがって削除
しても失われる内容は無い。

この扱いは`docs/reviews/20260816-llm-backends-implementation.ja.md:821`で一度
**不採用**になっている。ただしそこで却下されたのは「日付付きへ改名する」案であり、理由は
(a) 規約が改名を禁じていること、(b) 確定仕様が本文でこのファイル名を参照しており、確定仕様は
編集しない規約だったため参照が壊れること、の2点だった。同レビューは
「命名規約より前に作られた文書の扱いは、必要なら別途決める」と結んでいる。本項がその
「別途」にあたる。

今回、判断が変わる根拠は次の2点である。

- 提案は改名ではなく削除であり、(a)の改名禁止に抵触しない。
- `AGENTS.md`に「最終案の改訂」手続きが後から加わり、確定仕様を`## 改訂`として訂正する
  正規の経路ができた。(b)の「確定仕様は編集しない」という前提はもう成り立たない。

#### D2. `20260816-llm-backends.ja.md`の1文の訂正（採用、ただし要判断）

D1を実行すると、`docs/specs/20260816-llm-backends.ja.md:5-8`の
「元の文書は履歴資料として変更せず残す」が事実と異なる記述になる。`AGENTS.md`は
「確定した仕様書は、誤りと分かっている決定を残さない」と定めているため、放置できない。

- 当該文書に`## 改訂`を新設し（節順は`最終案` → `改訂` → `草案` → `レビュー`）、
  改訂1として`(前)`・`(後)`・理由・根拠を記録する。
- 本文の当該1文を「元の文書の内容は本文書の`## 草案`として取り込んだ」へ改める。

**判断が要る点:** 改訂手続きは対象を`最終案`と定めているが、この1文は`最終案`の前に置かれた
前書きにある。規約の文言どおりなら対象外だが、趣旨（確定仕様に既知の誤りを残さない）からは
対象である。**趣旨を優先して同じ手続きで扱うことを提案する。**

#### D3. `docs/specs/llm-backends.md`（英語版）の扱い（保留）

`llm-backends.ja.md`と同一内容の英語版であり、D1と同じ理屈なら削除できる。しかし
`docs/reviews/20260816-llm-backends-implementation.ja.md:310`が、このファイル名を
「確定版ではなく確定前の草案を参照してしまった」という誤りの証拠として引用している。
削除するとレビュー文書が指す対象が消える。

判断材料が`llm-backends.ja.md`と異なるため、**この1件だけ人間の判断を仰ぐ。**

#### D4. `docs/plans/estimate.md`の古いエージェント指示の削除（採用）

`docs/plans/estimate.md:3`に
`REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development ...`という指示が残っている。
現行の`CLAUDE.md`の委譲ドクトリンと矛盾し、しかも命令形でエージェントに宛てられているため、
読んだエージェントが従い得る。当該行を削除する。文書の残りは実装計画の記録として残す。

#### D5. `docs/plans/`の位置づけを`AGENTS.md`へ記す（採用）

`docs/plans/`は`AGENTS.md`が定義していない第3のディレクトリである。中身は`docs/specs/`と
重複しない実装計画だが、現行手順の一部と誤読され得る。`AGENTS.md`へ
「`docs/plans/`は規約制定前の実装計画の歴史的記録であり、現行の手順ではない。新規に追加
しない」の1行を足す。

#### D6. 規約制定前の無日付文書の扱いを確定する（採用）

`estimate`・`sync-rate-limits`・`vault-recovery`の`.ja.md`と`.md`は規約制定前の文書であり、
改名も削除もしない。`AGENTS.md`へ「命名規約の制定前に作られた文書は現状のまま残す。改名は
しない」の1行を足す。この判断は`docs/reviews/20260816-llm-backends-implementation.ja.md:821`で
一度下されているが、規約本体に無いため同じ議論が再燃する。規約へ移して決着させる。

### 却下・保留した案

- **READMEの分割**（設定リファレンスを別ファイルへ切り出す）: **却下**。現在の破綻は分量では
  なく内容の陳腐化である。分割しても陳腐化は直らず、辿る段数が1つ増えるだけになる。今回は
  内容の正しさに集中する。分量が問題として再浮上したら、そのとき単独で判断する。
- **Legacy direct-export mode節の削除**: **保留**。まだ動作するコード経路であり、READMEから
  消すと存在自体を辿れなくなる。存続方針の1行を添えるに留める案を推すが、コード側の廃止方針
  と一緒に決めるべき事柄である。
- **`DESIGN.md`への目次追加**: **保留**。9節でまだ目次が要る分量ではなく、追加すると更新箇所が
  1つ増える。
- **Quick startの手順5と`enrich-images`節の重複解消**: **保留**。重複ではあるが、手順5は
  「なぜこの工程を挟むか」、コマンド節は「何を受け付けるか」を書いており、読者が違う。今回は
  触らない。

### 検証

1. `feedian --help`と各サブコマンドの`--help`をREADMEのusage行・オプション表と1つずつ突き合わせ、
   差分が無いことを確認する。
2. 一時Vaultで`feedian init`を実行し、生成された`config.json`とREADMEの例の差分がゼロである
   ことを確認する。
3. READMEに載る全コマンド例をargparseに通し、parser errorにならないことを確認する（実行はしない）。
   A1の壊れたレシピはこの検査で検出できるべきである。
4. `docs/`と`README.md`の相対リンクが全て解決することを確認する。特にD1の削除後に
   `llm-backends.ja.md`への参照が残っていないこと。
5. `.\.venv\Scripts\python.exe -m pytest`

### コミット構成

- 本仕様の確定後、仕様書を単独で`docs:`型のコミットにする。
- 実装は1コミットにまとめる。`README.md`、`AGENTS.md`、`docs/specs/llm-backends.ja.md`の削除、
  `20260816-llm-backends.ja.md`の改訂1、`docs/plans/estimate.md`の1行削除が同じコミットに入る。
- **`DESIGN.md`の更新は伴わない。** 挙動が変わらないためである。`AGENTS.md`のライフサイクルは
  実装コミットに`DESIGN.md`の要約更新を含めると定めるが、それは挙動が変わる場合の規定である。

### 未決事項

1. D2の改訂手続きを前書きの1文に適用してよいか（規約の文言では対象外、趣旨では対象）。
2. D3の英語版`llm-backends.md`を削除するか、レビューの参照先として残すか。
3. 保留にしたLegacy direct-export modeの存続方針。

## レビュー

### レビュー1 — tsunyan (2026-08-27)

草案の「未決事項」3点に対する判断。

**1. D2の改訂手続きを前書きの1文へ適用してよいか → 採用。**

草案の提案どおり、規約の趣旨を優先して`## 改訂`として扱う。

**2. 英語版`docs/specs/llm-backends.md`の扱い → 削除する。参照の文言も削除してよい。**

理由は「内容の参照ではないため」。`docs/reviews/20260816-llm-backends-implementation.ja.md:310`が
このファイル名を挙げているのは、レビュー時に誤って参照した対象を特定するためであって、
文書の内容を根拠として引用しているのではない。したがってファイル名が消えてもレビューの
論旨は成立する。

草案D3の保留を解除し、次を採用する。

- `docs/specs/llm-backends.md`を削除する。
- 同レビュー`:310`の「確定前の草案`docs/specs/llm-backends.md`を参照し」から**ファイルパスだけを
  除去**し、「確定前の草案を参照し」とする。文そのものは残す。この文はレビューの誤りの原因を
  記録し、そこから「確定仕様が存在する実装のレビューでは確定版を根拠として引用する」という
  規則を導いている箇所であり、記録としての価値はその論旨にある。
- 完成済みレビュー文書への追記以外の編集にあたるが、意味を変えない参照先の除去であり、
  規約が仕様書について認める「意味を変えない訂正」と同じ性質と解釈する。

**3. Legacy direct-export modeの存続 → 不要であれば削除してよい。**

「不要かどうか」の判定はレビュー2に委ねる。

### レビュー2 — Claude Code (2026-08-27)

レビュー1の判断3が付した条件「不要なら」を調査した。**結論は「不要ではない」であり、
本仕様では削除しない。**

#### 代替の無い機能が4つある

legacy経路にしか存在せず、現行CLI（`feedian/cli.py`）に代替が無い機能。

| legacy機能 | 現行の代替 | 影響 |
|---|---|---|
| `--list-collections` | 無し | **現行configの`providers.raindrop.collection_id`を調べる唯一の手段** |
| `--sync-raindrop-summary` | 無し | 生成した要約をRaindropのnoteへ書き戻す経路が消える |
| `--sync-raindrop-tags` | 無し | 生成タグをRaindropへ追記する経路が消える |
| `--rename-existing` | 無し | legacy固有の概念であり、消えても現行に影響しない |

決定的なのは`--list-collections`である。READMEのフィールド表は
`providers.raindrop.collection_id`を現行の設定項目として載せているが、そのIDを調べる手段は
legacy CLIにしかない。legacyは「置き換えられた旧ワークフロー」ではなく、**現行ワークフローの
設定に必要な部品を握っている**状態にある。

`RaindropClient`の`get_root_collections`、`get_child_collections`、`update_raindrop_note`、
`append_raindrop_tags`（`feedian/raindrop.py:70-130`）はこの4機能のためだけに存在しており、
現行経路からは呼ばれていない。

#### 削除の波及範囲はドキュメントに収まらない

- legacy専用モジュールは`feedian/config.py`だけである。`llm.py`、`estimate.py`、`markdown.py`、
  `raindrop.py`、`hatena.py`はいずれも現行経路と共有しており、まとめて消せない。
- `feedian/__main__.py:1467`の`main`は現行・legacy双方の共通エントリポイントであり、
  現行コマンドを`cli.main`へ委譲している。ファイル削除ではなく本体約1400行の切り出しになる。
- `tests/test_main.py`（1007行）と`tests/test_config.py`、`config.example.json`が付随する。
- `DESIGN.md`の2箇所がlegacyの扱いを明記している（`Config.allow_private_urls`の全面フラグ、
  legacy export経路にschedulerが無いこと）。これらは確定仕様
  [フェッチ・設定・復元の境界強化](20260820-fetch-config-integrity-hardening.ja.md)と
  [syncとingestのスループット](20260819-sync-ingest-throughput.ja.md)に由来するため、
  削除するなら両仕様の改訂が要る。

#### 本仕様での扱い

草案の対象外「コードは1行も変更しない」を維持する。READMEのLegacy direct-export mode節は残し、
次の1行を足す。

- `--list-collections`は、現行のVault configで`providers.raindrop.collection_id`を設定する際に
  IDを調べる手段である。

これは現在のREADMEに欠けている情報であり、legacy節を「旧機能の置き場」から
「現行でも使う場面がある」記述へ変える。

legacyの削除自体は、上の4機能を現行へ移すか捨てるかを先に決める別の仕様として扱う。
本仕様に含めると、ドキュメント修正が機能削除の判断待ちで止まる。

### レビュー3 — Codex (2026-08-27)

結論は**2点を修正して採用**である。READMEの実装追従、日本語版・英語版の重複文書の削除、
legacy節の存続には追加の異論はない。

**1. レビュー1の、完了済みレビューから`docs/specs/llm-backends.md`のパスを除去する提案 → 不採用。**

`docs/reviews/20260816-llm-backends-implementation.ja.md:310`のパスは、誤って根拠にした
文書を特定する歴史的証拠である。同レビューの対象コミット`7e9d09e`には
実際に`docs/specs/llm-backends.md`が含まれており、作業ツリーから削除した後も
`git show 7e9d09e:docs/specs/llm-backends.md`で参照できる。したがって、削除はレビューの
再現性を壊さない。反対にパスを除去すると、どの草案を参照したかを特定する手がかりが
失われる。同文書が「指摘4の本文は記録として残し」とする方針とも整合しない。

英語版`docs/specs/llm-backends.md`の削除自体は採用し、完了済みレビューの`:310`は
編集せずそのまま残す。

**2. D6で`AGENTS.md`へ追加する文言 → 修正して採用。**

提案された「命名規約の制定前に作られた文書は現状のまま残す」は無条件の文言である。
その一方で、同じ実装コミットは規約制定前の`llm-backends.ja.md`と
`llm-backends.md`を、重複を理由に削除する。このままでは、追加した規約に同じコミットで
違反するように読める。

D6の趣旨は、無日付であることだけを理由に過去文書を整理しないことである。
`AGENTS.md`には次のように記す。

> 命名規約の制定前に作られた無日付文書は、無日付であることだけを理由に改名または削除しない。

この文言なら、`estimate`・`sync-rate-limits`・`vault-recovery`の無日付文書を残す判断と、
D1・D3で内容の重複を根拠に別途削除する判断が両立する。
