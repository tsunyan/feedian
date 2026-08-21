# Claude Code API キー対応のコードレビュー

ステータス: 完了
対象: `c7fbe3e833d372438e32c3a1fedae431bb753bda feat: support Claude Code API credentials`（このコミットの親）
仕様: [Claude Code API キー対応](../specs/20260821-claude-code-api-key-support.ja.md)
レビュー者: mistral/codestral-latest (2026-08-21)

## 結論

指摘2件はいずれも現行実装では再現せず、不採用とする。コード変更は不要である。
モデル名の検証と、互換APIでモデル名が未指定の場合のエラー処理は、確定仕様どおり既に実装され、受け入れテストでも確認されている。

## 指摘

受領したレビューの原文を、判断の根拠としてそのまま記録する。

```text
• The patch introduces several issues that need to be addressed, including missing model validation and error handling.

  Full review comments:

  - [P2] Missing model validation for claude-code-local backend — D:\GitHub\feedian\feedian\llm_backends.py:681-690
    The claude-code-local backend does not validate the model parameter before use. This could lead to unexpected behavior or errors when
    an invalid model is provided. The issue is located in the feedian/llm_backends.py file, lines 681-690. The backend should validate the
    model parameter to ensure it is supported before proceeding with the request.

  - [P2] Missing error handling for missing model configuration — D:\GitHub\feedian\feedian\cli.py:681-690
    The claude-code-local backend does not handle the case where the model is not explicitly configured. This could lead to unexpected
    behavior or errors when the model is not provided. The issue is located in the feedian/cli.py file, lines 681-690. The backend should
    validate the model parameter to ensure it is provided before proceeding with the request.
```

### 1. `claude-code-local` にモデル名の検証がない — 重大度: 中

元レビューは `feedian/llm_backends.py:681-690` を根拠として、`claude-code-local` が空文字列や不正なモデル名を受け入れ、Claude Code APIへ無効なリクエストを送ると指摘した。

実際には、`feedian/llm_backends.py:662-667` の `supports_model()` が空文字列、移動エイリアス、不正な文字列を拒否する。さらに `feedian/llm_backends.py:795-796` の `summarize()` はリクエストを構築する前にこの検証を呼び、違反時は `BackendPolicyError` を送出する。元レビューが示した行は認証情報の選択処理であり、モデル検証の実装箇所ではない。

主張どおりであれば、不正なモデル名が外部APIへのリクエストまで到達するコストが生じる。しかし現行実装では到達しない。

### 2. 互換APIでモデル名が未設定の場合のエラー処理がない — 重大度: 中

元レビューは `feedian/cli.py:681-690` を根拠として、互換APIのモデル名を設定しなかった場合に空のモデル名で処理が進み、実行時エラーになると指摘した。

実際には、`feedian/cli.py:687-690` がコマンドライン引数、環境変数、vault設定、バックエンド既定値の順でモデル名を選択する。公式APIには `feedian/llm_backends.py:659-660` の既定モデルがある一方、互換APIには既定モデルがないため、未指定時は `feedian/cli.py:692-697` が明示的に `BackendPolicyError` を送出する。

主張どおりであれば、設定不備が明確なポリシーエラーではなく外部APIの実行時エラーとして現れるコストが生じる。しかし現行実装は設定不備を外部API呼び出し前に検出する。

## 採否

| 指摘 | 採否 | 理由 |
|---|---|---|
| 1 | 不採用 | `supports_model()` と `summarize()` に検証が実装済みで、空文字列・移動エイリアス・不正な文字列は外部API呼び出し前に拒否されるため。 |
| 2 | 不採用 | 互換APIのモデル名が選択順のどこにも存在しない場合、CLIが明示的に `BackendPolicyError` を送出するため。公式APIだけは仕様どおり既定モデルを持つ。 |

## 検証

- `tests/test_llm_backends.py:413-416` で公式APIの既定モデルと許可・拒否条件を確認した。
- `tests/test_llm_backends.py:468-471` で互換APIの明示モデルと移動エイリアス・不正な文字列の拒否を確認した。
- `feedian/cli.py:687-697` を確認し、互換APIでモデル名が未指定の場合に外部API呼び出し前のポリシーエラーとなることを確認した。
- `tests/test_llm_backends.py` を実行し、対象テストがすべて成功することを確認した。

## 規約化した項目

なし。モデル名の検証契約と互換APIでの明示指定要件は、既存の確定仕様と受け入れテストで既に固定されている。
