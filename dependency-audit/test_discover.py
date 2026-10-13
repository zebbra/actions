import subprocess

import pytest

from dependency_audit.config import parse
from dependency_audit.discover import check, glob_regex, tracked_lockfiles, uncovered
from dependency_audit.model import ConfigError


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for path in [
        "backend/uv.lock",
        "uv.lock",
        "web/package-lock.json",
        "examples/demo/uv.lock",
        ".make_scripts/mkdocs-documentation/uv.lock",
        "docs/my-uv.lock",
    ]:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("")
    (tmp_path / "untracked").mkdir()
    (tmp_path / "untracked/uv.lock").write_text("")
    subprocess.run(["git", "-C", str(tmp_path), "add", "--", ".", ":!untracked"], check=True)
    return tmp_path


def config(*extra_targets, excludes=()):
    targets = [{"name": "backend", "ecosystem": "uv", "path": "backend"}, *extra_targets]
    return parse({"target": targets, "exclude": [{"glob": g, "reason": "r"} for g in excludes]})


ROOT = {"name": "root", "ecosystem": "uv", "path": "."}
WEB = {"name": "web", "ecosystem": "npm", "path": "web"}


def test_tracked_lockfiles_lists_exact_names_only(repo):
    assert tracked_lockfiles(repo) == [
        ".make_scripts/mkdocs-documentation/uv.lock",
        "backend/uv.lock",
        "examples/demo/uv.lock",
        "uv.lock",
        "web/package-lock.json",
    ]


def test_check_flags_uncovered_lockfile(repo):
    with pytest.raises(ConfigError) as e:
        check(config(ROOT), repo)
    assert "web/package-lock.json is neither a target nor excluded" in str(e.value)
    assert "examples/demo/uv.lock is neither a target nor excluded" in str(e.value)
    assert ".make_scripts" not in str(e.value)


def test_check_passes_with_targets_excludes_and_builtin_make_scripts(repo):
    check(config(ROOT, WEB, excludes=["examples/**"]), repo)


def test_check_flags_target_without_tracked_lockfile(repo):
    missing = {"name": "gone", "ecosystem": "npm", "path": "backend"}
    with pytest.raises(ConfigError, match="target `gone`: backend/package-lock.json is not tracked"):
        check(config(ROOT, WEB, missing, excludes=["examples/**"]), repo)


def test_glob_semantics():
    assert glob_regex("examples/**").match("examples/a/b/uv.lock")
    assert not glob_regex("examples/*").match("examples/a/uv.lock")
    assert glob_regex("examples/*/uv.lock").match("examples/a/uv.lock")
    assert not glob_regex(".make_scripts/**").match("docs/.make_scripts/uv.lock")
    assert glob_regex("**/.make_scripts/**").match("docs/.make_scripts/x/uv.lock")


def test_uncovered_honours_exclude_globs():
    lockfiles = ["backend/uv.lock", "examples/demo/uv.lock", ".make_scripts/x/uv.lock"]
    assert uncovered(config(), lockfiles) == ["examples/demo/uv.lock"]
    assert uncovered(config(excludes=["examples/**"]), lockfiles) == []
