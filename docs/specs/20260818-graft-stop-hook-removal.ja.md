# graftのStopフック削除

ステータス: 確定

**この文書は実装後に記録として書いた。** 症状の切り分けと修正を先に行ったため、事前の草案とレビューの往復は存在しない。
以下の`最終案`は、実際に行った変更とその根拠を後からまとめたものであり、レビュー見出しの日付が実際の時系列を表すことはない。

## 最終案

### 結論

`.claude/settings.json`から`Stop`フックだけを削除する。graftの終了時バックグラウンド同期は行わない。

Windowsで回答完了のたびに黒いコンソールウィンドウが一瞬表示される問題の原因は、このフックが起動する
graft側のデタッチ済み子プロセスだった。graftはクエリ時にグラフを自己更新するため、終了時のeagerな同期は
そもそも冗長である。

### 症状

Windows 11上で、Claude Codeの回答完了・デスクトップ通知と同時に黒いコンソールウィンドウが一瞬表示される。
Codex側でも同一の症状が観測されていた。

### 原因

`@nanonets/graft/dist/claude/hooks.js:213-228`の`handleStop`が、同期スクリプトを次の形で起動している。

```js
const child = spawn(process.execPath, [syncRun, dir], { detached: true, stdio: 'ignore' });
child.unref();
```

Windowsでは`detached: true`かつ`windowsHide: true`が無い場合、子プロセスに新しいコンソールが割り当てられ、
ウィンドウが可視化される。`stdio: 'ignore'`は出力を捨てるだけで、ウィンドウの生成は抑止しない。

この処理は`stats.dirty`が真のときにのみ走る。作業ツリーが汚れている開発中は毎ターン該当するため、
回答完了のたびに再現していた。

### 確定事項

#### 1. 削除するもの

`.claude/settings.json`の`hooks.Stop`配列全体。`graft-hooks.cjs stop`を呼ぶエントリはこれ1件のみ。

#### 2. 残すもの

| 設定 | 理由 |
|---|---|
| `SessionStart` | オリエンテーション注入。`emit()`あり |
| `UserPromptSubmit` | プロンプト関連ノードの注入。`emit()`あり |
| `PostToolUse` | 編集追従とトークン節約集計。`emit()`あり |
| `statusLine` / `subagentStatusLine` | 黒窓と無関係 |
| `permissions.allow`のgraft 4件 | CLIの実行に必要 |
| `footerLinksRegexes` | 黒窓と無関係 |

デスクトップ通知は`.claude/settings.json`の管轄外であり、変更していない。

#### 3. 削除が安全である根拠

- **表示の損失がない。** `hooks.js`内で`emit()`を呼ぶのは`PostToolUse`(L149)、`SessionStart`(L236)、
  `UserPromptSubmit`(L285)の3箇所だけで、`handleStop`は何も出力しない。
- **グラフの鮮度が落ちない。** `graft ask`はクエリ前に自分でグラフを更新する。実測でも
  `[graft] refreshed the graph (1 file changed) before answering`を出力し、作業ツリー上の未コミットの
  変更（`sync_vault`の`quick`引数）を反映した結果を返した。

### 却下した案

#### A. フック呼び出し側に`windowsHide`を追加する / GUIランチャー経由で起動する

不採用。Codex側で試して効果がなかった。原因が判明した今ならその理由も説明できる。窓を開いているのは
フックプロセス自身ではなく、そこからさらに`spawn`されるgraftの孫プロセスであり、呼び出し側の設定は
そこまで伝播しない。

#### B. グローバルインストール済みの`hooks.js`に`windowsHide: true`を直接当てる

不採用。技術的には唯一の未検証ルートだが、`C:\Users\t\AppData\Roaming\npm\node_modules`配下への
直接パッチはこのリポジトリで管理できず、graftを更新するたびに消える。修正が消えたことに気づけない形の
対処は、黒窓が再発したときに原因の再調査を強いる。upstreamへの報告が筋である。

#### C. `Stop`を`PostToolUse`の`post-edit-sync`に置き換える

不採用。`post-edit-sync`は`handlePostEdit`のあとに同じ`handleStop`を呼ぶ（`hooks.js:252-256`）ため、
発火頻度が上がるだけで黒窓は解消しない。

### 再発条件

graftのホスト連携インストーラは4種のフックを一括で書き込む（`dist/hosts/codex-hooks.js:59-62`が
`SessionStart` / `UserPromptSubmit` / `PostToolUse` / `Stop`を定義）。**`graft init`相当を再実行すると
`Stop`が復活し、黒窓も再発する。** 再実行後は`Stop`を再度削除すること。

### 検証

| 項目 | 結果 |
|---|---|
| `.claude/settings.json`のJSONパース | OK |
| 残存フック | `PostToolUse` / `UserPromptSubmit` / `SessionStart` |
| 再起動後の`UserPromptSubmit` | 発火を確認（プロンプトへのgraftコンテキスト注入） |
| 再起動後の`PostToolUse` | 一時ファイルの書き込みで発火を確認 |
| 回答完了時の黒窓 | 再現しない |
| `graft ask --source` | 正常応答。グラフの自動更新も確認 |
| `graft skeleton` | 正常応答 |
| `graft check` | wiring graphはコードと同期 |

### 補足: `🌱`行との無関係性

調査中、「回答末尾の`🌱 graft saved ~N tokens this turn`が出なくなった」という観測があったが、これは
今回の削除とは無関係である。この行はフックの出力ではなく、`dist/claude/format.js:195`の指示に従って
エージェント自身が書く文章であり、**そのターンで実際にgraftを呼んだ場合にのみ**出る。graftコマンドを
叩かなかったターンに出ないのは仕様どおりの挙動である。

なお`UserPromptSubmit`が注入するパックはpointers-only（`--source`なし）で`[graft] tokens saved ≈`行を
持たないため、プロンプトフックだけが動いたターンにもこの行は出ない。

### 今後

終了時のeagerな同期が本当に必要になった場合は、`handleStop`の`spawn`に`windowsHide: true`を加える
修正をupstream（`@nanonets/graft`）へ提案する。ローカルでの対処は行わない。
