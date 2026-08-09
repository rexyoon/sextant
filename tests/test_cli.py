from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from sextant import config as config_mod
from sextant.cli import app
from sextant.store import ActivityType
from sextant.store.sqlite import SQLiteStore

from .conftest import commit_file, run_git

runner = CliRunner()


def _text(result) -> str:
    """click 버전에 따라 stderr 가 분리되기도 해서 둘 다 합쳐서 본다."""
    parts = [result.output or ""]
    try:
        parts.append(result.stderr or "")
    except ValueError:
        pass
    return "\n".join(parts)


@pytest.fixture
def in_repo(git_repo: Path, sextant_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(git_repo)
    return git_repo


def _store() -> SQLiteStore:
    return SQLiteStore(config_mod.db_path())


# ─────────────────────────────── init ───────────────────────────────


def test_init_registers_repo(in_repo: Path, sextant_home: Path):
    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, _text(result)
    assert (sextant_home / "config.toml").exists()

    configuration = config_mod.load_config()
    assert set(configuration.projects) == {"repo"}
    assert configuration.projects["repo"].path.resolve() == in_repo.resolve()


def test_init_accepts_custom_name(in_repo: Path):
    result = runner.invoke(app, ["init", "--name", "koala-back"])

    assert result.exit_code == 0, _text(result)
    assert set(config_mod.load_config().projects) == {"koala-back"}


def test_init_creates_database_immediately(in_repo: Path):
    runner.invoke(app, ["init"])
    assert config_mod.db_path().exists()


def test_init_outside_repo_fails(tmp_path: Path, sextant_home: Path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 1
    assert "git 레포가 아닙니다" in _text(result)


def test_init_rejects_second_name_for_same_path(in_repo: Path):
    runner.invoke(app, ["init", "--name", "first"])

    result = runner.invoke(app, ["init", "--name", "second"])

    assert result.exit_code == 1
    assert "이미" in _text(result)
    assert set(config_mod.load_config().projects) == {"first"}


def test_init_from_subdirectory_registers_repo_root(in_repo: Path, monkeypatch):
    nested = in_repo / "src" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, _text(result)
    registered = next(iter(config_mod.load_config().projects.values()))
    assert registered.path.resolve() == in_repo.resolve()


def test_init_seeds_identity_from_repo_config(in_repo: Path):
    runner.invoke(app, ["init"])

    assert config_mod.load_config().identity_emails == ["rex@example.com"]


def test_init_me_adds_identity(in_repo: Path):
    result = runner.invoke(app, ["init", "--me", "old@corp.co.kr", "--me", "me@naver.com"])

    assert result.exit_code == 0, _text(result)
    configuration = config_mod.load_config()
    assert configuration.is_mine("old@corp.co.kr")
    assert configuration.is_mine("me@naver.com")


def test_init_does_not_overwrite_identity_from_another_repo(
    in_repo: Path, tmp_path: Path, monkeypatch
):
    """다른 레포에서 등록한 이메일이 새 레포 등록으로 사라지면 안 된다."""
    runner.invoke(app, ["init", "--me", "old@corp.co.kr"])

    second = tmp_path / "other"
    second.mkdir()
    run_git(second, "init")
    run_git(second, "config", "user.name", "Rex")
    run_git(second, "config", "user.email", "another@example.com")
    monkeypatch.chdir(second)
    runner.invoke(app, ["init", "--name", "other"])

    assert config_mod.load_config().is_mine("old@corp.co.kr")


def test_init_reports_unrecognised_authors(in_repo: Path):
    commit_file(in_repo, "a.py", "x\n", "내 커밋")
    commit_file(
        in_repo, "b.py", "y\n", "예전 메일", name="Rex", email="old@corp.co.kr"
    )

    result = runner.invoke(app, ["init"])
    output = _text(result)

    assert "작성자 분포" in output
    assert "✓" in output and "✗" in output
    assert "sextant init --me old@corp.co.kr" in output


# ─────────────────────────────── note ───────────────────────────────


def test_note_requires_registered_project(in_repo: Path):
    result = runner.invoke(app, ["note", "메모"])

    assert result.exit_code == 1
    assert "sextant init" in _text(result)


def test_note_is_stored(in_repo: Path):
    runner.invoke(app, ["init", "--name", "proj"])

    result = runner.invoke(app, ["note", "오늘 락 경합을 잡았다"])

    assert result.exit_code == 0, _text(result)
    with _store() as store:
        from datetime import datetime

        today = datetime.now().astimezone().date()
        found = store.activities_on("proj", today)

    assert [item.raw_text for item in found] == ["오늘 락 경합을 잡았다"]
    assert found[0].type is ActivityType.NOTE


def test_notes_accumulate(in_repo: Path):
    runner.invoke(app, ["init", "--name", "proj"])
    runner.invoke(app, ["note", "첫째"])
    runner.invoke(app, ["note", "둘째"])

    with _store() as store:
        assert len(store.known_refs("proj", ActivityType.NOTE)) == 0
        from datetime import datetime

        today = datetime.now().astimezone().date()
        assert len(store.activities_on("proj", today)) == 2


# ─────────────────────────────── sync ───────────────────────────────


def test_sync_stores_commits(in_repo: Path):
    commit_file(in_repo, "a.py", "x\n", "feat: 하나")
    commit_file(in_repo, "b.py", "y\n", "feat: 둘")
    runner.invoke(app, ["init", "--name", "proj"])

    result = runner.invoke(app, ["sync"])

    assert result.exit_code == 0, _text(result)
    assert "신규 2건" in _text(result)

    with _store() as store:
        assert len(store.known_refs("proj", ActivityType.COMMIT)) == 2


def test_sync_is_idempotent(in_repo: Path):
    commit_file(in_repo, "a.py", "x\n", "feat: 하나")
    runner.invoke(app, ["init", "--name", "proj"])
    runner.invoke(app, ["sync"])

    result = runner.invoke(app, ["sync"])

    assert result.exit_code == 0, _text(result)
    assert "신규 0건" in _text(result)
    with _store() as store:
        assert len(store.known_refs("proj", ActivityType.COMMIT)) == 1


def test_sync_since_filters_by_author_date(in_repo: Path):
    commit_file(in_repo, "old.py", "x\n", "옛날", when="2026-01-05T10:00:00+09:00")
    commit_file(in_repo, "new.py", "y\n", "최근", when="2026-08-09T10:00:00+09:00")
    runner.invoke(app, ["init", "--name", "proj"])

    result = runner.invoke(app, ["sync", "--since", "2026-06-01"])

    assert result.exit_code == 0, _text(result)
    assert "신규 1건" in _text(result)

    with _store() as store:
        from datetime import date

        stored = store.activities_between("proj", date(2026, 1, 1), date(2026, 12, 31))
    assert [item.raw_text.splitlines()[0] for item in stored] == ["최근"]


def test_sync_rejects_bad_since_format(in_repo: Path):
    runner.invoke(app, ["init", "--name", "proj"])

    result = runner.invoke(app, ["sync", "--since", "2026/08/09"])

    assert result.exit_code == 1
    assert "YYYY-MM-DD" in _text(result)


def test_sync_skips_other_authors_by_default(in_repo: Path):
    commit_file(in_repo, "mine.py", "x\n", "내 커밋")
    commit_file(
        in_repo,
        "theirs.py",
        "y\n",
        "동료 커밋",
        name="Teammate",
        email="mate@example.com",
    )
    runner.invoke(app, ["init", "--name", "proj"])

    result = runner.invoke(app, ["sync"])

    assert "신규 1건" in _text(result)
    assert "제외: mate@example.com 1건" in _text(result)


def test_sync_collects_all_of_my_emails(in_repo: Path):
    """한 사람이 이메일 여러 개로 커밋해도 전부 내 것으로 수집돼야 한다."""
    commit_file(in_repo, "a.py", "x\n", "현재 메일")
    commit_file(in_repo, "b.py", "y\n", "회사 메일", name="Rex", email="old@corp.co.kr")
    commit_file(in_repo, "c.py", "z\n", "개인 메일", name="Rex", email="me@naver.com")
    runner.invoke(app, ["init", "--me", "old@corp.co.kr", "--me", "me@naver.com"])

    result = runner.invoke(app, ["sync"])

    assert "신규 3건" in _text(result)
    assert "제외:" not in _text(result)


def test_sync_all_authors_includes_everyone(in_repo: Path):
    commit_file(in_repo, "mine.py", "x\n", "내 커밋")
    commit_file(
        in_repo,
        "theirs.py",
        "y\n",
        "동료 커밋",
        name="Teammate",
        email="mate@example.com",
    )
    runner.invoke(app, ["init", "--name", "proj"])

    result = runner.invoke(app, ["sync", "--all-authors"])

    assert "신규 2건" in _text(result)


def test_sync_requires_registered_project(in_repo: Path):
    result = runner.invoke(app, ["sync"])

    assert result.exit_code == 1
    assert "sextant init" in _text(result)


def test_sync_on_empty_repo_reports_zero(in_repo: Path):
    runner.invoke(app, ["init", "--name", "proj"])

    result = runner.invoke(app, ["sync"])

    assert result.exit_code == 0, _text(result)
    assert "커밋 0건" in _text(result)
