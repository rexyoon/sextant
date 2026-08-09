"""테스트용 git 레포 픽스처.

실제 git 을 돌린다. `git log` 출력 형식·numstat·타임존 처리는 파싱 대상이
git 자신이라, 가짜로 만든 문자열을 파싱하는 테스트는 진짜 회귀를 못 잡는다.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

DEFAULT_NAME = "Rex"
DEFAULT_EMAIL = "rex@example.com"


def run_git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    merged = {**os.environ, **(env or {})}
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=merged,
        check=True,
    )
    return completed.stdout


def commit_file(
    repo: Path,
    filename: str,
    content: str | bytes,
    message: str,
    when: str = "2026-08-09T08:30:00+09:00",
    name: str = DEFAULT_NAME,
    email: str = DEFAULT_EMAIL,
) -> str:
    """파일 하나를 쓰고 커밋한 뒤 커밋 해시를 돌려준다.

    `when` 은 오프셋을 포함한 ISO 문자열이다. 오프셋을 바꿔가며 커밋할 수
    있어야 정렬·날짜 경계 테스트가 가능하다.
    """
    target = repo / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")

    run_git(repo, "add", "--", filename)
    run_git(
        repo,
        "commit",
        "-m",
        message,
        env={
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_AUTHOR_DATE": when,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_COMMITTER_DATE": when,
        },
    )
    return run_git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init")
    run_git(repo, "config", "user.name", DEFAULT_NAME)
    run_git(repo, "config", "user.email", DEFAULT_EMAIL)
    run_git(repo, "config", "commit.gpgsign", "false")
    return repo


@pytest.fixture
def sextant_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`~/.sextant` 대신 tmp 를 쓰게 한다. 실제 홈을 절대 건드리지 않는다."""
    home = tmp_path / "sextant-home"
    monkeypatch.setenv("SEXTANT_HOME", str(home))
    return home
