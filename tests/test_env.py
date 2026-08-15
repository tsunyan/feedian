from __future__ import annotations

import os

from feedian.cli import main
from feedian.env import load_env_file
from feedian.vault import user_env_path


def test_load_env_file_sets_missing_values_without_overwriting_environment(tmp_path, monkeypatch) -> None:
    path = tmp_path / ".env"
    path.write_text("ONE=from-file\nTWO='quoted'\n# ignored\n", encoding="utf-8")
    monkeypatch.setenv("ONE", "from-environment")
    monkeypatch.delenv("TWO", raising=False)

    load_env_file(path)

    assert __import__("os").environ["ONE"] == "from-environment"
    assert __import__("os").environ["TWO"] == "quoted"


def test_credentials_are_found_from_any_working_directory(tmp_path, monkeypatch) -> None:
    # A scheduled run starts in an arbitrary directory with no .env beside it.
    config_home = tmp_path / "config"
    monkeypatch.setenv("APPDATA", str(config_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    user_env = user_env_path()
    user_env.parent.mkdir(parents=True, exist_ok=True)
    user_env.write_text("OPENAI_API_KEY=from-user-config\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert main([]) == 0

    assert os.environ["OPENAI_API_KEY"] == "from-user-config"


def test_working_directory_env_wins_over_the_user_default(tmp_path, monkeypatch) -> None:
    config_home = tmp_path / "config"
    monkeypatch.setenv("APPDATA", str(config_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    user_env = user_env_path()
    user_env.parent.mkdir(parents=True, exist_ok=True)
    user_env.write_text("OPENAI_API_KEY=from-user-config\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("OPENAI_API_KEY=from-project\n", encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert main([]) == 0

    assert os.environ["OPENAI_API_KEY"] == "from-project"
