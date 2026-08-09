"""설정 파일(`~/.sextant/config.toml`) 로딩과 프로젝트 등록.

왜 DB가 아니라 설정 파일인가:
Phase 2에서 저장 계층이 SQLite → Postgres 로 바뀌어도 "이 머신의 어느 경로가
어느 project 인가"는 **그 머신에서만 의미가 있는 정보**다. DB에 넣으면 이관할
때 따라다니고 다른 머신에서는 깨진 경로가 된다. 그래서 활동 데이터(DB)와
머신 로컬 매핑(설정 파일)을 처음부터 분리한다.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import tomli_w

CONFIG_VERSION = 1

#: Claude API 키는 **환경변수로만** 읽는다. 설정 파일에 절대 쓰지 않는다.
API_KEY_ENV = "ANTHROPIC_API_KEY"

#: 테스트가 실제 홈 디렉토리를 오염시키지 않도록 열어둔 우회로.
HOME_ENV = "SEXTANT_HOME"


# ─────────────────────────────── 경로 ───────────────────────────────


def sextant_home() -> Path:
    raw = os.environ.get(HOME_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".sextant"


def config_path() -> Path:
    return sextant_home() / "config.toml"


def db_path() -> Path:
    return sextant_home() / "sextant.db"


def logs_dir(project: str) -> Path:
    return sextant_home() / "logs" / project


def troubles_dir(project: str) -> Path:
    return sextant_home() / "troubles" / project


def _path_key(path: Path) -> str:
    """경로 비교용 정규화 키.

    Windows에서는 대소문자가 구분되지 않고 `C:/x` 와 `C:\\x` 가 섞여 들어온다.
    등록된 경로와 현재 경로를 비교할 때는 반드시 이 함수를 통과시킨다.
    """
    return os.path.normcase(str(Path(path).expanduser().resolve()))


# ─────────────────────────────── 모델 ───────────────────────────────


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    registered_at: str


@dataclass
class Config:
    version: int = CONFIG_VERSION
    projects: dict[str, Project] = field(default_factory=dict)

    #: 내 커밋으로 인정할 이메일들.
    #: 한 사람이 레포마다 다른 이메일로 커밋한다 — 회사 메일, GitHub noreply,
    #: 개인 메일이 섞인다. 레포의 user.email 하나만 믿으면 내 작업 대부분이
    #: '남의 커밋' 으로 걸러진다. 그래서 project 가 아니라 사람 단위로 둔다.
    identity_emails: list[str] = field(default_factory=list)

    def is_mine(self, email: str) -> bool:
        known = {item.strip().lower() for item in self.identity_emails}
        return email.strip().lower() in known

    def add_identity(self, email: str) -> bool:
        """새로 추가했으면 True. 이미 있거나 빈 문자열이면 False."""
        normalized = email.strip()
        if not normalized or self.is_mine(normalized):
            return False
        self.identity_emails.append(normalized)
        return True

    def by_path(self, path: Path) -> Project | None:
        key = _path_key(path)
        for project in self.projects.values():
            if _path_key(project.path) == key:
                return project
        return None

    def register(self, name: str, path: Path) -> Project:
        """이름으로 프로젝트를 등록한다. 같은 이름이 있으면 경로를 갱신한다.

        경로가 이미 다른 이름으로 등록돼 있으면 거부한다. 한 레포가 두 개의
        project 로 쪼개지면 일지가 양쪽에 나뉘어 쌓여서 되돌리기 어렵다.
        """
        existing = self.by_path(path)
        if existing is not None and existing.name != name:
            raise ValueError(
                f"이 경로는 이미 '{existing.name}' 로 등록돼 있습니다: {existing.path}"
            )
        project = Project(
            name=name,
            path=Path(path).expanduser().resolve(),
            registered_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.projects[name] = project
        return project


# ──────────────────────────── 로드 / 저장 ────────────────────────────


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        return Config()

    with path.open("rb") as fp:
        raw = tomllib.load(fp)

    projects: dict[str, Project] = {}
    for name, entry in (raw.get("projects") or {}).items():
        projects[name] = Project(
            name=name,
            path=Path(entry["path"]),
            registered_at=entry.get("registered_at", ""),
        )

    identity = (raw.get("identity") or {}).get("emails") or []

    return Config(
        version=raw.get("version", CONFIG_VERSION),
        projects=projects,
        identity_emails=list(identity),
    )


def save_config(config: Config) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, object] = {"version": config.version}
    if config.identity_emails:
        payload["identity"] = {"emails": list(config.identity_emails)}
    if config.projects:
        payload["projects"] = {
            name: {
                "path": project.path.as_posix(),
                "registered_at": project.registered_at,
            }
            for name, project in sorted(config.projects.items())
        }

    with path.open("wb") as fp:
        tomli_w.dump(payload, fp)
    return path


# ────────────────────────── 현재 위치 해석 ──────────────────────────


def find_git_root(start: Path | None = None) -> Path | None:
    """현재 디렉토리에서 위로 올라가며 git 레포 루트를 찾는다.

    `.git` 이 디렉토리가 아니라 **파일**인 경우도 레포로 인정한다 —
    git worktree 에서는 `.git` 이 실제 저장소를 가리키는 파일이다.
    (대상 레포 중 worktree 를 쓰는 곳이 있어서 이 처리가 필요하다.)
    """
    current = Path(start or Path.cwd()).expanduser().resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def resolve_project(config: Config, start: Path | None = None) -> Project | None:
    """현재 위치가 속한 등록된 프로젝트를 돌려준다. 없으면 None."""
    root = find_git_root(start)
    if root is None:
        return None
    return config.by_path(root)


def api_key() -> str | None:
    return os.environ.get(API_KEY_ENV) or None
