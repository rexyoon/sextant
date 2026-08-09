from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..store import Activity, ActivityType

# 커밋 메시지에는 개행이 들어가므로 줄 단위로 자를 수 없다.
# 텍스트에 나올 일이 없는 제어문자를 레코드/필드 구분자로 쓴다.
_RECORD = "\x1e"
_FIELD = "\x1f"
_LOG_FORMAT = _RECORD + _FIELD.join(["%H", "%aI", "%an", "%ae", "%s", "%b"]) + _FIELD


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class FileChange:
    path: str
    insertions: int | None
    deletions: int | None

    @property
    def is_binary(self) -> bool:
        return self.insertions is None


@dataclass(frozen=True)
class Commit:
    sha: str
    authored_at: datetime
    author_name: str
    author_email: str
    subject: str
    body: str
    files: tuple[FileChange, ...]

    @property
    def short_sha(self) -> str:
        return self.sha[:8]

    def to_raw_text(self) -> str:
        """DB 에 남길 본문.

        **패치 전문은 저장하지 않는다.** 이유 두 가지.
        1) 커밋 해시를 들고 있으니 패치는 언제든 git 에서 다시 뽑을 수 있다.
           같은 내용을 SQLite 에 복사해두면 레포를 통째로 두 벌 갖는 셈이다.
        2) 패치에는 시크릿이 섞여 있을 수 있다. 스캐너(Phase 1-A)는 LLM 전송
           직전에 도는데, 그 전에 DB 에 원문을 박아두면 시크릿이 디스크에
           한 벌 더 남는다.
        따라서 여기에는 메시지와 파일별 증감만 남기고, 실제 diff 는 일지를
        만드는 시점에 해시로 다시 읽는다.
        """
        lines = [self.subject]

        body = self.body.strip()
        if body:
            lines += ["", body]

        if self.files:
            lines += ["", "--- 변경 파일 ---"]
            for change in self.files:
                if change.is_binary:
                    lines.append(f"  (binary)  {change.path}")
                else:
                    lines.append(f"  +{change.insertions} -{change.deletions}  {change.path}")

        return "\n".join(lines)


def run_git(repo: Path, *args: str) -> str:
    """레포에서 git 을 실행하고 stdout 을 돌려준다.

    encoding 을 UTF-8 로 못박는 이유: 생략하면 이 머신 기본값인 cp949 로
    디코딩돼서, 남의 레포 커밋 메시지에 든 문자에서 깨지거나 터진다.
    errors="replace" 는 그래도 못 읽는 바이트가 있을 때 죽지 않게 하려는 것 —
    수집이 커밋 하나 때문에 통째로 멈추는 편이 더 나쁘다.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git 을 찾을 수 없습니다. PATH 를 확인하세요.") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()
        raise GitError(detail or f"git {' '.join(args)} 가 실패했습니다.")

    return completed.stdout


def repo_root(start: Path | None = None) -> Path | None:
    """현재 위치가 속한 git 레포 루트. 레포가 아니면 None.

    경로를 직접 훑는 `config.find_git_root` 와 달리 git 에게 직접 묻는다.
    등록(`init`)은 한 번뿐이라 subprocess 비용이 무의미하고, worktree·submodule
    같은 경우를 git 이 알아서 정확히 답해준다.
    """
    base = Path(start or Path.cwd())
    try:
        output = run_git(base, "rev-parse", "--show-toplevel")
    except GitError:
        return None
    text = output.strip()
    return Path(text) if text else None


def author_email(repo: Path) -> str | None:
    try:
        return run_git(repo, "config", "user.email").strip() or None
    except GitError:
        return None


def author_email_counts(repo: Path) -> dict[str, int]:
    """작성자 이메일별 커밋 수. 등록 시점에 신원 누락을 눈으로 잡으라고 쓴다."""
    if not has_commits(repo):
        return {}

    counts: dict[str, int] = {}
    for line in run_git(repo, "log", "--format=%ae").splitlines():
        email = line.strip()
        if email:
            counts[email] = counts.get(email, 0) + 1
    return counts


def has_commits(repo: Path) -> bool:
    try:
        run_git(repo, "rev-parse", "--verify", "HEAD")
    except GitError:
        return False
    return True


def read_commits(repo: Path) -> list[Commit]:
    """레포의 전체 커밋을 최신순으로 읽는다.

    `--since` 를 git 에 넘기지 않고 파이썬에서 거르는 이유:
    git 의 `--since` 는 **커밋터 날짜** 기준인데 우리가 쓰는 건 작성자 날짜다.
    리베이스한 커밋에서 둘이 갈라지면 조용히 빠진다. 대상 레포 규모가
    수십~수백 커밋이라 전부 읽고 정확히 거르는 쪽이 낫다.
    """
    if not has_commits(repo):
        return []
    raw = run_git(repo, "log", "--numstat", f"--format={_LOG_FORMAT}")
    return _parse_log(raw)


def to_activity(commit: Commit, project: str) -> Activity:
    return Activity.create(
        project=project,
        type=ActivityType.COMMIT,
        raw_text=commit.to_raw_text(),
        occurred=commit.authored_at,
        ref=commit.sha,
    )


def _parse_log(raw: str) -> list[Commit]:
    commits: list[Commit] = []

    for chunk in raw.split(_RECORD):
        if not chunk.strip():
            continue

        parts = chunk.split(_FIELD)
        if len(parts) < 7:
            continue  # 형식이 깨진 레코드는 통째로 버린다

        sha, authored, name, email, subject, body, trailing = parts[:7]
        commits.append(
            Commit(
                sha=sha.strip(),
                authored_at=datetime.fromisoformat(authored.strip()),
                author_name=name,
                author_email=email,
                subject=subject,
                body=body,
                files=tuple(_parse_numstat(trailing)),
            )
        )

    return commits


def _parse_numstat(text: str) -> list[FileChange]:
    changes: list[FileChange] = []

    for line in text.splitlines():
        if not line.strip():
            continue
        columns = line.split("\t")
        if len(columns) < 3:
            continue

        insertions, deletions = columns[0], columns[1]
        # 경로에 탭이 들어갈 수 있어 나머지를 다시 붙인다.
        path = "\t".join(columns[2:])
        changes.append(
            FileChange(
                path=path,
                # 바이너리 파일은 git 이 증감을 '-' 로 준다.
                insertions=None if insertions == "-" else int(insertions),
                deletions=None if deletions == "-" else int(deletions),
            )
        )

    return changes
