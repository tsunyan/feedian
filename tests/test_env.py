from __future__ import annotations

from feedian.env import load_env_file


def test_load_env_file_sets_missing_values_without_overwriting_environment(tmp_path, monkeypatch) -> None:
    path = tmp_path / ".env"
    path.write_text("ONE=from-file\nTWO='quoted'\n# ignored\n", encoding="utf-8")
    monkeypatch.setenv("ONE", "from-environment")
    monkeypatch.delenv("TWO", raising=False)

    load_env_file(path)

    assert __import__("os").environ["ONE"] == "from-environment"
    assert __import__("os").environ["TWO"] == "quoted"
