# フェッチ・設定・復元の境界強化実装のコードレビュー

ステータス: 完了
対象: `e6a3abd` fix: harden fetch config and restore boundaries（このコミットの親）
仕様: [フェッチ・設定・復元の境界強化](../specs/20260820-fetch-config-integrity-hardening.ja.md)
レビュー者: Codex (2026-08-20)

## 結論

確定仕様のdirect fetch、設定配線、restore trust chain、bool厳密化、ElementTree warningの主要契約は実装され、レビュー時点の全505テストも成功した。一方、Browser fallbackが`page.route()`だけを通信境界としていたため、Playwrightが別経路として扱うService Worker、popup初回navigation、WebSocketを検証できない重大度: 高の指摘が1件あった。また、CIを失敗させる未使用importと、現行動作を記録する`DESIGN.md`の欠落が各1件あった。

3件をすべて採用し、同じ修正commitで対応した。Browser fallbackはrenderごとのbrowser contextを作り、Service Workerを無効化し、HTTP routeをcontext全体へ登録した。WebSocketは記事抽出に不要なため、navigation前に一律で閉じる。context routeはpopupの初回navigationも覆う。未使用importを除去し、`DESIGN.md`にはdirect fetch、Browser fallback、policy、restore、bool厳密化の現行動作と確定仕様へのlinkを追加した。修正後も全505テストと43 subtestが成功し、Ruff、compileall、diff checkも成功した。追加指摘はない。

## 指摘

### 1. Browser fallbackに`page.route()`を通らない通信経路が残る — 重大度: 高

**根拠:** `feedian/extract.py:950-992`、確定仕様`:56`、[Playwright Page.route](https://playwright.dev/python/docs/api/class-page#page-route)、[Playwright Browser.new_page](https://playwright.dev/python/docs/api/class-browser#browser-new-page)

`render_html_with_browser`は`page.route("**/*", route_request)`のcallback内だけで`validate_fetch_url`を呼ぶ。しかしPlaywrightは、Service Workerが処理するrequestを`page.route()`ではinterceptしないと明記し、request interceptionではService Workerを`block`するよう推奨している。popupの最初のrequestも`page.route()`ではinterceptされず、`browser_context.route()`が必要である。WebSocketも`page.route_web_socket()`/`browser_context.route_web_socket()`という別APIを持ち、通常のrouteを通らない。

**影響:** 悪意あるページがbrowser fallback対象になると、Feedianのhostname検証を通らないbrowser通信をprivate hostへ開始できる。これは確定仕様が受容した「各requestを検証するがChromium内部の名前解決とのTOCTOUは残る」という範囲より広く、SSRF境界そのものを迂回する。

**提案:** renderごとに`service_workers="block"`のbrowser contextを作り、HTTP routeをcontextへ登録する。記事抽出に不要なWebSocketはcontextのWebSocket routeで一律に閉じる。routeとWebSocket routeはnavigation前に登録し、同じhostの繰り返しrequestも毎回検証されることを回帰テストで固定する。

### 2. `tests/test_rss.py`の未使用importがlint jobを失敗させる — 重大度: 低

**根拠:** `tests/test_rss.py:4`、`.github/workflows/ci.yml:73-84`

`unittest.mock.ANY`をimportしているが使用していない。`ruff check feedian tests`はF401で終了コード1となり、全pytestが成功してもCIのlint jobは失敗する。

**影響:** PRをmergeできない。実行時動作への影響はない。

**提案:** 未使用の`ANY`をimportから除去する。

### 3. 現行動作を記録する`DESIGN.md`が実装commitに含まれない — 重大度: 低

**根拠:** `git show --stat e6a3abd`、`AGENTS.md:68-88`

対象commitはDNS pinning、host単位のprivate allow-list、proxy無効化、fetch policy、Git tagを信頼点とするrestoreという現行動作を追加するが、`DESIGN.md`を変更していない。プロジェクト規約は、確定仕様の実装時に現行動作の要約と仕様へのlinkを`DESIGN.md`へ追加し、codeと同じcommitへ含めるよう求めている。

**影響:** `DESIGN.md`だけを読む利用者が現在のsecurity・restore境界を把握できず、判断理由を確定仕様へ遡る経路もない。

**提案:** direct fetch、Browser fallback、policy、restore trust chain、bool厳密化を現行動作として簡潔に要約し、確定仕様へlinkする。

## 採否

| # | 指摘 | 重大度 | 採否 | 対応 |
|---|---|---|---|---|
| 1 | Browser fallbackの未検証通信経路 | 高 | 採用 | 本commitでService Workerを無効化し、context HTTP routeとWebSocket遮断をnavigation前に登録した |
| 2 | `tests/test_rss.py`の未使用import | 低 | 採用 | 本commitで`ANY`を除去した |
| 3 | `DESIGN.md`の現行動作・仕様link欠落 | 低 | 採用 | 本commitで「フェッチと復元の境界」を追加した |

## 検証

- [x] `python -m pytest -q tests/test_security.py::BrowserFallbackNetworkBoundaryTests` → 1 passed。
- [x] `python -m pytest -q` → 505 passed、43 subtests passed。
- [x] `python -m ruff check feedian tests` → All checks passed!
- [x] `python -m compileall -q feedian tests` → 成功。
- [x] `git diff --check` → 問題なし。
- [x] 回帰テストで、browser contextが`service_workers="block"`で作られ、同一hostの2 requestとfinal URLがそれぞれ検証されることを確認した。
- [x] 回帰テストで、WebSocket routeがnavigation前に登録され、接続せずcode 1008で閉じられることを確認した。
- [x] 修正commitの親がレビュー対象`e6a3abd`になるcommit構成であることを確認した。

## 規約化した項目

なし。Browser fallbackのPlaywright固有境界、未使用import、設計要約の欠落はいずれも同種指摘の2回目ではない。`DESIGN.md`を実装commitへ含める規則は既存規約の適用であり、新規の規約化は不要である。
