from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path

import pytest

from sextant.collect import git as git_mod
from sextant.store import ActivityType

from .conftest import commit_file, run_git

KST = timezone(timedelta(hours=9))


def test_empty_repo_has_no_commits(git_repo: Path):
    assert git_mod.has_commits(git_repo) is False
    assert git_mod.read_commits(git_repo) == []


def test_reads_commit_metadata(git_repo: Path):
    sha = commit_file(git_repo, "a.py", "print(1)\n", "feat: 첫 커밋")

    commits = git_mod.read_commits(git_repo)

    assert len(commits) == 1
    commit = commits[0]
    assert commit.sha == sha
    assert commit.subject == "feat: 첫 커밋"
    assert commit.author_email == "rex@example.com"
    assert commit.short_sha == sha[:8]


def test_author_date_keeps_original_offset(git_repo: Path):
    commit_file(git_repo, "a.py", "x\n", "msg", when="2026-08-09T08:30:00+09:00")

    commit = git_mod.read_commits(git_repo)[0]

    assert commit.authored_at.utcoffset() == timedelta(hours=9)
    assert commit.authored_at.replace(tzinfo=None).isoformat() == "2026-08-09T08:30:00"


def test_numstat_is_parsed(git_repo: Path):
    commit_file(git_repo, "a.py", "one\ntwo\nthree\n", "세 줄 추가")

    commit = git_mod.read_commits(git_repo)[0]

    assert len(commit.files) == 1
    change = commit.files[0]
    assert change.path == "a.py"
    assert change.insertions == 3
    assert change.deletions == 0
    assert change.is_binary is False


def test_binary_file_has_no_line_counts(git_repo: Path):
    commit_file(git_repo, "logo.bin", b"\x00\x01\x02\x00", "바이너리 추가")

    change = git_mod.read_commits(git_repo)[0].files[0]

    assert change.is_binary is True
    assert change.insertions is None
    assert change.deletions is None


def test_multiline_message_survives_record_parsing(git_repo: Path):
    commit_file(
        git_repo,
        "a.py",
        "x\n",
        "fix: 락 경합 해결\n\n원인은 비관적 락 범위가 넓었던 것.\n트랜잭션을 쪼갰다.",
    )

    commit = git_mod.read_commits(git_repo)[0]

    assert commit.subject == "fix: 락 경합 해결"
    assert "트랜잭션을 쪼갰다." in commit.body


def test_non_cp949_characters_do_not_break_collection(git_repo: Path):
    """cp949 에 없는 문자가 커밋 메시지에 있어도 읽혀야 한다."""
    commit_file(git_repo, "a.py", "x\n", "feat: 배포 🚀 完了")

    commit = git_mod.read_commits(git_repo)[0]

    assert "🚀" in commit.subject
    assert "完了" in commit.subject


def test_raw_text_has_message_and_stats_but_no_patch(git_repo: Path):
    secret_line = "API_KEY = 'sk-do-not-store-this'\n"
    commit_file(git_repo, "a.py", secret_line, "chore: 설정 추가")

    raw = git_mod.read_commits(git_repo)[0].to_raw_text()

    assert "chore: 설정 추가" in raw
    assert "+1 -0  a.py" in raw
    # 패치 전문을 담지 않으므로 파일 내용은 DB 로 흘러들지 않는다.
    assert "sk-do-not-store-this" not in raw


def test_commits_are_returned_newest_first(git_repo: Path):
    commit_file(git_repo, "a.py", "1\n", "첫째", when="2026-08-09T09:00:00+09:00")
    commit_file(git_repo, "b.py", "2\n", "둘째", when="2026-08-10T09:00:00+09:00")

    subjects = [commit.subject for commit in git_mod.read_commits(git_repo)]

    assert subjects == ["둘째", "첫째"]


def test_author_email_reads_repo_config(git_repo: Path):
    assert git_mod.author_email(git_repo) == "rex@example.com"


def test_repo_root_from_nested_directory(git_repo: Path):
    nested = git_repo / "src" / "deep"
    nested.mkdir(parents=True)

    found = git_mod.repo_root(nested)

    assert found is not None
    assert found.resolve() == git_repo.resolve()


def test_repo_root_outside_repo(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()

    assert git_mod.repo_root(plain) is None


def test_run_git_raises_on_failure(git_repo: Path):
    with pytest.raises(git_mod.GitError):
        git_mod.run_git(git_repo, "cat-file", "-p", "deadbeef")


def test_to_activity_maps_fields(git_repo: Path):
    sha = commit_file(git_repo, "a.py", "x\n", "feat: 매핑")

    activity = git_mod.to_activity(git_mod.read_commits(git_repo)[0], "proj")

    assert activity.project == "proj"
    assert activity.type is ActivityType.COMMIT
    assert activity.ref == sha
    assert activity.local_date == "2026-08-09"
    assert activity.occurred_utc == "2026-08-08T23:30:00+00:00"


def test_worktree_is_a_valid_repo_root(git_repo: Path, tmp_path: Path):
    """worktree 는 .git 이 파일이다. 수집 대상에서 빠지면 안 된다."""
    commit_file(git_repo, "a.py", "x\n", "초기")
    worktree = tmp_path / "wt"
    run_git(git_repo, "worktree", "add", "-b", "side", str(worktree))

    assert (worktree / ".git").is_file()
    found = git_mod.repo_root(worktree)
    assert found is not None
    assert found.resolve() == worktree.resolve()
