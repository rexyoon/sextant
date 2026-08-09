"""설정 계층 테스트.

`SEXTANT_HOME` 을 tmp_path 로 돌려 실제 홈 디렉토리를 건드리지 않는다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sextant import config as cfg


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "sextant-home"
    monkeypatch.setenv(cfg.HOME_ENV, str(home))
    return home


def _make_repo(root: Path, git_as_file: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if git_as_file:
        # git worktree 는 .git 이 디렉토리가 아니라 파일이다.
        (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    else:
        (root / ".git").mkdir()
    return root


def test_home_follows_env(isolated_home):
    assert cfg.sextant_home() == isolated_home
    assert cfg.config_path() == isolated_home / "config.toml"
    assert cfg.db_path() == isolated_home / "sextant.db"


def test_load_returns_empty_config_when_missing():
    config = cfg.load_config()
    assert config.projects == {}
    assert config.version == cfg.CONFIG_VERSION


def test_save_then_load_roundtrip(tmp_path):
    repo = _make_repo(tmp_path / "repos" / "koala-back")

    config = cfg.Config()
    config.register("koala-back", repo)
    cfg.save_config(config)

    reloaded = cfg.load_config()
    assert set(reloaded.projects) == {"koala-back"}
    assert reloaded.projects["koala-back"].path == repo.resolve()
    assert reloaded.projects["koala-back"].registered_at


def test_register_rejects_same_path_under_different_name(tmp_path):
    repo = _make_repo(tmp_path / "repos" / "one")

    config = cfg.Config()
    config.register("first", repo)
    with pytest.raises(ValueError, match="이미 'first'"):
        config.register("second", repo)


def test_register_same_name_updates_path(tmp_path):
    old = _make_repo(tmp_path / "repos" / "old")
    new = _make_repo(tmp_path / "repos" / "new")

    config = cfg.Config()
    config.register("proj", old)
    config.register("proj", new)

    assert len(config.projects) == 1
    assert config.projects["proj"].path == new.resolve()


def test_by_path_ignores_separator_and_case(tmp_path):
    repo = _make_repo(tmp_path / "repos" / "MixedCase")

    config = cfg.Config()
    config.register("proj", repo)

    # 구분자가 섞인 표기로도 같은 프로젝트를 찾아야 한다.
    as_posix = Path(repo.as_posix())
    assert config.by_path(as_posix) is not None
    assert config.by_path(tmp_path / "repos" / "nope") is None


def test_find_git_root_walks_up(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)

    assert cfg.find_git_root(nested) == repo.resolve()


def test_find_git_root_accepts_worktree_git_file(tmp_path):
    repo = _make_repo(tmp_path / "worktree", git_as_file=True)
    assert cfg.find_git_root(repo) == repo.resolve()


def test_find_git_root_returns_none_outside_repo(tmp_path):
    plain = tmp_path / "no-repo"
    plain.mkdir()
    assert cfg.find_git_root(plain) is None


def test_resolve_project_from_subdirectory(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)

    config = cfg.Config()
    config.register("proj", repo)

    resolved = cfg.resolve_project(config, nested)
    assert resolved is not None
    assert resolved.name == "proj"


def test_resolve_project_none_when_unregistered(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    assert cfg.resolve_project(cfg.Config(), repo) is None


def test_identity_survives_save_and_load(tmp_path):
    repo = _make_repo(tmp_path / "repo")

    config = cfg.Config()
    config.register("proj", repo)
    assert config.add_identity("Me@Corp.co.kr") is True
    assert config.add_identity("me@corp.co.kr") is False  # 대소문자만 다르면 중복
    assert config.add_identity("  ") is False
    cfg.save_config(config)

    reloaded = cfg.load_config()
    assert reloaded.identity_emails == ["Me@Corp.co.kr"]
    assert reloaded.is_mine("ME@CORP.CO.KR") is True
    assert reloaded.is_mine("someone@else.com") is False


def test_api_key_reads_env_only(monkeypatch):
    monkeypatch.delenv(cfg.API_KEY_ENV, raising=False)
    assert cfg.api_key() is None

    monkeypatch.setenv(cfg.API_KEY_ENV, "sk-test")
    assert cfg.api_key() == "sk-test"


def test_api_key_never_written_to_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.API_KEY_ENV, "sk-should-not-persist")
    repo = _make_repo(tmp_path / "repo")

    config = cfg.Config()
    config.register("proj", repo)
    path = cfg.save_config(config)

    assert "sk-should-not-persist" not in path.read_text(encoding="utf-8")
