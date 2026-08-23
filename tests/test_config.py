"""The `.env` loader — the floor under the environment, never an override.

The credentials this system needs are read from `os.environ` and nowhere else,
which is the right rule and left one gap: nothing read the file they are
written into. A shell can export them by hand. The launchd job at 08:00 cannot,
and a scheduled run that lost its channel is the exact failure `delivery.py` is
built to refuse — so the file has to reach the process, and on the terms below.
"""

from __future__ import annotations

from desk.config import load_env


def test_it_fills_in_what_the_environment_does_not_have(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DESK_TELEGRAM_CHAT_ID=626462558\n", encoding="utf-8")
    monkeypatch.delenv("DESK_TELEGRAM_CHAT_ID", raising=False)

    assert load_env(env_file) == ["DESK_TELEGRAM_CHAT_ID"]

    import os

    assert os.environ["DESK_TELEGRAM_CHAT_ID"] == "626462558"


def test_the_real_environment_wins(tmp_path, monkeypatch) -> None:
    """`DESK_ENGINE=replay uv run desk ...` has to keep meaning replay."""
    env_file = tmp_path / ".env"
    env_file.write_text("DESK_ENGINE=claude-code\n", encoding="utf-8")
    monkeypatch.setenv("DESK_ENGINE", "replay")

    assert load_env(env_file) == []

    import os

    assert os.environ["DESK_ENGINE"] == "replay"


def test_a_missing_file_is_not_an_error(tmp_path) -> None:
    """The offline path ships without one, and `desk demo` must still run."""
    assert load_env(tmp_path / "absent") == []


def test_comments_blanks_and_malformed_lines_are_skipped(tmp_path, monkeypatch) -> None:
    """A raise here would put the offending line — a secret — into a traceback."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n# a comment\nDESK_ONE=1\nthis line has no equals sign\n  DESK_TWO = 2 \n",
        encoding="utf-8",
    )
    for name in ("DESK_ONE", "DESK_TWO"):
        monkeypatch.delenv(name, raising=False)

    assert load_env(env_file) == ["DESK_ONE", "DESK_TWO"]

    import os

    assert os.environ["DESK_TWO"] == "2"


def test_quotes_around_a_value_are_stripped(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('DESK_QUOTED="a value"\n', encoding="utf-8")
    monkeypatch.delenv("DESK_QUOTED", raising=False)
    load_env(env_file)

    import os

    assert os.environ["DESK_QUOTED"] == "a value"


def test_the_real_env_file_is_ignored_by_git() -> None:
    """It holds a live bot token. This asserts the rule that keeps it out."""
    from desk.config import REPO_ROOT

    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in [line.strip() for line in ignored]
