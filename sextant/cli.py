"""SEXTANT CLI 진입점.

여기서는 설정·수집·저장을 잇기만 한다. 로직은 각 모듈에 둔다 —
Phase 2 에서 이 CLI 가 API 클라이언트로 바뀌어도 아래 계층은 그대로 쓴다.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import List, NoReturn, Optional

import typer

from . import config as config_mod
from .collect import git as git_mod
from .collect.note import build_note
from .store import Store
from .store.sqlite import SQLiteStore


def _force_utf8_output() -> None:
    """표준 출력을 UTF-8 로 고정한다.

    이 머신의 기본 인코딩은 cp949 다. 그대로 두면 두 가지가 터진다.
    1) Rich 가 박스 문자를 못 쓴다고 판단해 ASCII 로 깨진 표를 그린다.
    2) cp949 에 없는 문자(이모지, 한자 등)가 커밋 메시지·diff 에 섞여 들어오면
       UnicodeEncodeError 로 프로세스가 죽는다. 남의 레포를 읽는 도구라
       입력 문자를 우리가 통제할 수 없으므로 이건 가정이 아니라 시간 문제다.

    파일 입출력도 같은 이유로 항상 `encoding="utf-8"` 을 명시한다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # pytest 등이 바꿔치기한 스트림
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


_force_utf8_output()

app = typer.Typer(
    name="sextant",
    help="개발 활동을 기록해 개발 일지와 트러블슈팅 문서를 만드는 개인용 도구.",
    no_args_is_help=True,
    add_completion=False,
)


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


def _warn(message: str) -> None:
    typer.secho(message, fg=typer.colors.YELLOW, err=True)


def _not_yet(command: str, step: str) -> NoReturn:
    _warn(f"'sextant {command}' 는 아직 구현 전입니다 ({step}).")
    raise typer.Exit(code=2)


def _open_store() -> Store:
    return SQLiteStore(config_mod.db_path())


def _current() -> tuple[config_mod.Config, config_mod.Project]:
    configuration = config_mod.load_config()
    project = config_mod.resolve_project(configuration)
    if project is None:
        _fail(
            "등록된 프로젝트가 아닙니다. 레포 안에서 'sextant init' 를 먼저 실행하세요."
        )
    return configuration, project


def _report_authors(root: Path, configuration: config_mod.Config) -> None:
    """레포의 작성자 분포를 보여준다.

    한 사람이 회사 메일·GitHub noreply·개인 메일을 섞어 쓰기 때문에, 등록
    시점에 눈으로 확인시키지 않으면 내 커밋 대부분이 조용히 빠진 채로
    일지가 만들어진다. 조용한 누락이 잘못된 수집보다 나쁘다.
    """
    counts = git_mod.author_email_counts(root)
    if not counts:
        return

    typer.echo("")
    typer.echo("작성자 분포:")
    unknown: list[str] = []
    for email, count in sorted(counts.items(), key=lambda item: -item[1]):
        mine = configuration.is_mine(email)
        typer.echo(f"  {'✓' if mine else '✗'} {count:>4}건  {email}")
        if not mine:
            unknown.append(email)

    if unknown:
        typer.echo("")
        typer.echo("✗ 표시된 커밋은 수집에서 빠집니다. 내 것이라면:")
        for email in unknown:
            typer.echo(f"  sextant init --me {email}")


@app.command()
def init(
    name: Optional[str] = typer.Option(
        None, "--name", help="프로젝트 이름. 생략하면 레포 디렉토리명을 쓴다."
    ),
    me: Optional[List[str]] = typer.Option(
        None, "--me", help="내 커밋으로 인정할 이메일. 여러 번 쓸 수 있다."
    ),
) -> None:
    """현재 디렉토리의 git 레포를 프로젝트로 등록한다."""
    root = git_mod.repo_root()
    if root is None:
        _fail("git 레포가 아닙니다. 레포 안에서 실행하세요.")

    configuration = config_mod.load_config()
    try:
        project = configuration.register(name or root.name, root)
    except ValueError as exc:
        _fail(str(exc))

    for email in me or []:
        configuration.add_identity(email)

    # 이 레포의 git user.email 도 항상 내 것으로 넣는다.
    # `--me` 를 준 경우에만 건너뛰면, 추가하려던 옛 이메일만 등록되고 정작
    # 지금 쓰는 이메일이 빠진다. add_identity 는 덧붙이기만 하므로 다른
    # 레포에서 등록해둔 이메일이 지워질 걱정은 없다.
    detected = git_mod.author_email(root)
    if detected:
        configuration.add_identity(detected)

    saved_to = config_mod.save_config(configuration)

    # DB 를 지금 만들어 둔다. 첫 note/sync 까지 미루면 권한·경로 문제가
    # 등록이 끝난 뒤에야 드러나서 원인을 찾기 어렵다.
    _open_store().close()

    typer.echo(f"등록: {project.name}  →  {project.path}")
    typer.echo(f"설정: {saved_to}")
    typer.echo(f"DB  : {config_mod.db_path()}")
    _report_authors(root, configuration)


@app.command()
def note(
    text: str = typer.Argument(..., help="남길 메모."),
) -> None:
    """자유 메모를 activity 로 저장한다."""
    _, project = _current()

    with _open_store() as store:
        saved = store.add_activity(build_note(project.name, text))

    assert saved is not None  # NOTE 는 ref 가 없어 중복으로 걸릴 수 없다
    typer.echo(f"메모 저장 (project={project.name}, id={saved.id})")


@app.command()
def sync(
    since: Optional[str] = typer.Option(
        None, "--since", help="YYYY-MM-DD. 이 날짜(포함) 이후 커밋만 수집한다."
    ),
    all_authors: bool = typer.Option(
        False, "--all-authors", help="내 커밋뿐 아니라 모든 작성자의 커밋을 수집한다."
    ),
) -> None:
    """git log 를 읽어 COMMIT activity 로 저장한다."""
    configuration, project = _current()

    since_date: date | None = None
    if since is not None:
        try:
            since_date = date.fromisoformat(since)
        except ValueError:
            _fail(f"--since 는 YYYY-MM-DD 형식이어야 합니다: {since!r}")

    try:
        commits = git_mod.read_commits(project.path)
    except git_mod.GitError as exc:
        _fail(f"git 을 읽지 못했습니다: {exc}")

    scanned = len(commits)

    # 기본이 '내 커밋만' 인 이유: 이 도구가 만드는 건 내 역량의 근거다.
    # 팀 레포에서 남의 커밋까지 넣으면 일지도 프로필도 사실과 어긋난다.
    # 대신 제외된 건 이메일별로 전부 출력한다 — 조용히 버리지 않는다.
    excluded: dict[str, int] = {}
    if not all_authors:
        if not configuration.identity_emails:
            _warn(
                "내 이메일이 등록돼 있지 않아 작성자 필터를 건너뜁니다. "
                "'sextant init --me <email>' 로 등록하세요."
            )
        else:
            kept = []
            for commit in commits:
                if configuration.is_mine(commit.author_email):
                    kept.append(commit)
                else:
                    excluded[commit.author_email] = (
                        excluded.get(commit.author_email, 0) + 1
                    )
            commits = kept

    skipped_old = 0
    if since_date is not None:
        kept = [c for c in commits if c.authored_at.date() >= since_date]
        skipped_old = len(commits) - len(kept)
        commits = kept

    with _open_store() as store:
        saved = store.add_activities(
            git_mod.to_activity(commit, project.name) for commit in commits
        )

    duplicates = len(commits) - len(saved)

    typer.echo(
        f"{project.name}: 커밋 {scanned}건 확인 → 신규 {len(saved)}건 저장"
        f" (이미 있음 {duplicates}건)"
    )
    for email, count in sorted(excluded.items(), key=lambda item: -item[1]):
        typer.echo(f"  제외: {email} {count}건  ('sextant init --me {email}' 로 포함)")
    if skipped_old:
        typer.echo(f"  {since} 이전 커밋 {skipped_old}건 제외")


@app.command()
def log(
    date_option: Optional[str] = typer.Option(
        None, "--date", help="YYYY-MM-DD. 생략하면 오늘."
    ),
) -> None:
    """그 날의 activity 로 개발 일지 초안(DRAFT)을 생성한다."""
    _not_yet("log", "Phase 1-B")


@app.command()
def show(
    date_option: Optional[str] = typer.Option(
        None, "--date", help="YYYY-MM-DD. 생략하면 오늘."
    ),
) -> None:
    """생성된 일지를 터미널에서 확인한다."""
    _not_yet("show", "Phase 1-B")


@app.command()
def approve(
    date_option: Optional[str] = typer.Option(
        None, "--date", help="YYYY-MM-DD. 생략하면 오늘."
    ),
) -> None:
    """수정한 마크다운을 되읽어 DB에 반영하고 APPROVED 로 전환한다."""
    _not_yet("approve", "Phase 1-B")


if __name__ == "__main__":  # pragma: no cover
    app()
