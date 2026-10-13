import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dependency_audit import proc, uv_backend
from dependency_audit.config import Target
from dependency_audit.model import ToolError, fix_target

FIXTURES = Path(__file__).parent / "fixtures"
TARGET = Target(name="backend", ecosystem="uv", path="backend", groups=("fb",))


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeRunner:
    """Answers `uv export` by writing a pylock and `pip-audit` with queued results."""

    def __init__(self, pylock="[[packages]]\nname = 'urllib3'\n", audits=()):
        self.pylock = pylock
        self.audits = list(audits)
        self.calls = []

    def __call__(self, cmd, cwd=None):
        self.calls.append(cmd)
        if cmd[:2] == ["uv", "export"]:
            Path(cmd[cmd.index("-o") + 1]).write_text(self.pylock)
            return completed()
        if "pip-audit" in cmd:
            return self.audits.pop(0)
        raise AssertionError(f"unexpected command {cmd}")


@pytest.fixture
def no_sleep(monkeypatch):
    delays = []
    monkeypatch.setattr(proc, "sleep", delays.append)
    return delays


def recorded():
    return json.loads((FIXTURES / "pip-audit-core.json").read_text())


def test_parse_merges_repeated_advisories_and_skips_unaudited():
    findings = uv_backend.parse_pip_audit(recorded(), "backend")
    assert {f.package for f in findings} == {"django", "ecdsa", "pyjwt", "urllib3"}
    rekeyed = [f for f in findings if f.package == "django" and f.advisory == "PYSEC-2026-3717"]
    assert len(rekeyed) == 1
    assert rekeyed[0].fix_versions == ("5.2.17", "6.0.8", "6.1.1")
    assert "GHSA-q238-5cxm-5c9h" in rekeyed[0].ids
    assert len([f for f in findings if f.package == "ecdsa"]) == 1


def test_pyjwt_fix_target_is_2_15_0():
    pyjwt = [f for f in uv_backend.parse_pip_audit(recorded(), "backend") if f.package == "pyjwt"]
    assert len(pyjwt) == 14
    assert fix_target(pyjwt) == "2.15.0"


def test_export_args_follow_scope_and_groups():
    assert uv_backend.export_args(TARGET, "runtime") == ["--no-dev", "--group", "fb"]
    assert uv_backend.export_args(TARGET, "all") == ["--all-groups", "--all-extras"]


def test_empty_export_skips_pip_audit(monkeypatch, tmp_path):
    runner = FakeRunner(pylock='lock-version = "1.0"\ncreated-by = "uv"\n')
    monkeypatch.setattr(uv_backend, "run", runner)
    assert uv_backend.audit(TARGET, tmp_path, "runtime", tmp_path / "work") == []
    assert [c[:2] for c in runner.calls] == [["uv", "export"]]


def test_findings_exit_1_with_json(monkeypatch, tmp_path):
    runner = FakeRunner(audits=[completed(1, json.dumps(recorded()))])
    monkeypatch.setattr(uv_backend, "run", runner)
    findings = uv_backend.audit(TARGET, tmp_path, "runtime", tmp_path / "work")
    assert {f.package for f in findings} == {"django", "ecdsa", "pyjwt", "urllib3"}
    pip_audit = runner.calls[1]
    assert "--ignore-vuln" not in pip_audit
    assert pip_audit[:3] == ["uvx", "--from", "pip-audit==2.10.1"]


def test_clean_exit_0(monkeypatch, tmp_path):
    clean = {"dependencies": [{"name": "six", "version": "1.17.0", "vulns": []}], "fixes": []}
    monkeypatch.setattr(uv_backend, "run", FakeRunner(audits=[completed(0, json.dumps(clean))]))
    assert uv_backend.audit(TARGET, tmp_path, "runtime", tmp_path / "work") == []


@pytest.mark.parametrize(
    "result",
    [
        completed(2, "", "ConnectionError: osv.dev"),
        completed(1, "not json", "Traceback"),
        completed(0, json.dumps(recorded())),
        completed(1, json.dumps({"dependencies": [], "fixes": []})),
    ],
    ids=["exit-2", "invalid-json", "exit-0-with-findings", "exit-1-without-findings"],
)
def test_tool_error_after_retries(monkeypatch, tmp_path, no_sleep, result):
    runner = FakeRunner(audits=[result] * 4)
    monkeypatch.setattr(uv_backend, "run", runner)
    with pytest.raises(ToolError):
        uv_backend.audit(TARGET, tmp_path, "runtime", tmp_path / "work")
    assert no_sleep == [2, 8, 30]
    assert sum("pip-audit" in c for c in runner.calls) == 4


def test_retry_recovers(monkeypatch, tmp_path, no_sleep):
    runner = FakeRunner(audits=[completed(2, "", "timeout"), completed(1, json.dumps(recorded()))])
    monkeypatch.setattr(uv_backend, "run", runner)
    assert uv_backend.audit(TARGET, tmp_path, "runtime", tmp_path / "work")
    assert no_sleep == [2]


def test_export_failure_is_tool_error(monkeypatch, tmp_path):
    monkeypatch.setattr(uv_backend, "run", lambda cmd, cwd=None: completed(2, "", "uv.lock needs to be updated"))
    with pytest.raises(ToolError, match="uv export failed"):
        uv_backend.audit(TARGET, tmp_path, "runtime", tmp_path / "work")


LOCK = """version = 1
[[package]]
name = "{name}"
version = "{version}"
"""


class FakeLock:
    """Answers `uv lock` by rewriting the lockfile per a scripted outcome and `uv tree` with dependents."""

    def __init__(self, lockfile, outcomes):
        self.lockfile = lockfile
        self.outcomes = outcomes  # spec -> (returncode, resulting version or None)
        self.calls = []

    def __call__(self, cmd, cwd=None):
        self.calls.append(cmd)
        if cmd[:2] == ["uv", "lock"]:
            spec = cmd[cmd.index("--upgrade-package") + 1]
            returncode, version = self.outcomes[spec]
            if version:
                name = spec.split("==")[0]
                self.lockfile.write_text(LOCK.format(name=name, version=version))
            return completed(returncode, "", "" if returncode == 0 else "No solution found")
        if cmd[:2] == ["uv", "tree"]:
            return completed(0, "django v4.2.30\n└── app v0.0.0 (group: prod)\n")
        raise AssertionError(cmd)


def pyjwt_findings():
    return [f for f in uv_backend.parse_pip_audit(recorded(), "backend") if f.package in ("pyjwt", "django")]


def test_fix_falls_back_to_newest_allowed_then_blocks(monkeypatch, tmp_path):
    target = Target(name="backend", ecosystem="uv", path=".")
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text(LOCK.format(name="django", version="4.2.30"))
    fake = FakeLock(
        lockfile,
        {
            "django==5.2.17": (1, None),
            "django": (0, "4.2.31"),  # resolves, but below the fix target
            "pyjwt==2.15.0": (0, "2.15.0"),
        },
    )
    monkeypatch.setattr(uv_backend, "run", fake)
    blocked = uv_backend.fix(target, tmp_path, pyjwt_findings(), commit=False)
    assert blocked == {"django": "required by app v0.0.0 (group: prod)"}
    specs = [c[c.index("--upgrade-package") + 1] for c in fake.calls if c[:2] == ["uv", "lock"]]
    assert specs == ["django==5.2.17", "django", "pyjwt==2.15.0"]
    assert uv_backend.installed(target, tmp_path) == {"pyjwt": ["2.15.0"]}


DJANGO_CONFLICT = """Using CPython 3.12.7
  × No solution found when resolving dependencies:
  ╰─▶ Because your project depends on django>=4.2.30,<5 and django==5.2.17, we can conclude that your project's requirements are unsatisfiable.
"""
SPLIT_CONFLICT = """error: No solution found when resolving dependencies for split (markers: python_full_version == '3.12.*')
  cause: Because your project depends on django>=4.2.30,<5 and django==5.2.17, we can conclude that your project's requirements are unsatisfiable.
"""
CHAIN = """  ╰─▶ Because aciclient<=1.3 depends on pyopenssl>=19.1.0,<20 and aciclient>=1.5,<=1.6 depends on pyopenssl>=23.0.0,<24, we can conclude that all of:
             aciclient<=1.6
         And because aciclient==1.7 depends on requests[socks]==2.32.5 and your project depends on aciclient, we can conclude that your project, all of:
             pyopenssl<19.1.0
         , requests!=2.32.5 are incompatible.
         And because your project depends on pyopenssl>=25,<26 and requests==2.33.0, we can conclude that your project's requirements are unsatisfiable.
"""
REQUIRES_PYTHON = """hint: While the active Python version is 3.12, the resolution failed for other Python versions.

hint: The `requires-python` value (>=3.9) includes Python versions that are not supported by your dependencies (e.g., anyio==4.14.2 only supports >=3.10). Consider using a more restrictive `requires-python` value (like >=3.10).
"""
DERIVED = """  cause: Because the requested Python version (>=3.9) does not satisfy Python>=3.10 and black==26.3.1 depends on Python>=3.10, we can conclude that black==26.3.1 cannot be used.
         And because ariadne-codegen<=0.16.0 depends on black==26.3.1 and only the following versions of ariadne-codegen are available:
hint: The `requires-python` value (>=3.9) includes Python versions that are not supported by your dependencies (e.g., black==26.3.1 only supports >=3.10). Consider using a more restrictive `requires-python` value (like >=3.10).
"""


@pytest.mark.parametrize(
    "stderr, package, wanted, reason",
    [
        (DJANGO_CONFLICT, "django", "5.2.17", "your project depends on django>=4.2.30,<5"),
        (SPLIT_CONFLICT, "django", "5.2.17", "your project depends on django>=4.2.30,<5"),
        (
            CHAIN,
            "requests",
            "2.33.0",
            "aciclient==1.7 depends on requests[socks]==2.32.5 and your project depends on aciclient",
        ),
        (
            REQUIRES_PYTHON,
            "anyio",
            "4.14.2",
            "The `requires-python` value (>=3.9) includes Python versions that are not supported by your "
            "dependencies (e.g., anyio==4.14.2 only supports >=3.10)",
        ),
        (
            DERIVED,
            "black",
            "26.3.1",
            "The `requires-python` value (>=3.9) includes Python versions that are not supported by your "
            "dependencies (e.g., black==26.3.1 only supports >=3.10)",
        ),
        ("error: Failed to fetch: https://pypi.org/simple/django/", "django", "5.2.17", ""),
    ],
)
def test_conflict_names_the_blocking_requirement(stderr, package, wanted, reason):
    assert uv_backend.conflict(stderr, package, wanted) == reason


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def test_commit_refuses_changes_beyond_the_lockfile(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "uv.lock").write_text("a")
    (tmp_path / "pyproject.toml").write_text("a")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    (tmp_path / "uv.lock").write_text("b")
    (tmp_path / "pyproject.toml").write_text("b")
    with pytest.raises(proc.GuardError, match="pyproject.toml changed besides uv.lock"):
        proc.commit_lockfile(tmp_path, "uv.lock", "fix(deps): x")
    git(tmp_path, "checkout", "--", "pyproject.toml")
    proc.commit_lockfile(tmp_path, "uv.lock", "fix(deps): bump x 1 -> 2 (GHSA-1)")
    assert git(tmp_path, "log", "-1", "--format=%an|%s") == "neops-deps-bot|fix(deps): bump x 1 -> 2 (GHSA-1)\n"
    assert git(tmp_path, "show", "--name-only", "--format=", "HEAD") == "uv.lock\n"


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_real_uv_fix_upgrades_and_blocks(tmp_path):
    shutil.copytree(FIXTURES / "uv-project", tmp_path, dirs_exist_ok=True)
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    target = Target(name="app", ecosystem="uv", path=".")
    findings = uv_backend.audit(target, tmp_path, "all", tmp_path.parent / "work")
    assert {f.package for f in findings} == {"idna", "urllib3"}
    blocked = uv_backend.fix(target, tmp_path, findings, commit=True)
    assert set(blocked) == {"idna"} and "idna<3.7" in blocked["idna"]
    log = git(tmp_path, "log", "--format=%an|%s", "HEAD~1..HEAD").splitlines()
    assert len(log) == 1 and log[0].startswith("neops-deps-bot|fix(deps): bump urllib3 2.2.1 -> ")
    after = uv_backend.audit(target, tmp_path, "all", tmp_path.parent / "work2")
    assert {f.package for f in after} == {"idna"}
