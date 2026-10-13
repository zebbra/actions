import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dependency_audit import npm_backend, proc
from dependency_audit.config import Target
from dependency_audit.model import ToolError

FIXTURES = Path(__file__).parent / "fixtures"
TARGET = Target(name="web", ecosystem="npm", path=".")


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def report():
    return json.loads((FIXTURES / "npm-audit.json").read_text())


def lock():
    return json.loads((FIXTURES / "npm-package-lock.json").read_text())


@pytest.fixture
def project(tmp_path):
    shutil.copy(FIXTURES / "npm-package-lock.json", tmp_path / "package-lock.json")
    (tmp_path / "package.json").write_text('{"name": "fixture-app"}\n')
    return tmp_path


def by_package(findings):
    result = {}
    for f in findings:
        result.setdefault(f.package, []).append(f)
    return result


def test_object_via_entries_become_findings_and_string_via_is_skipped():
    findings = by_package(npm_backend.parse_npm_audit(report(), lock(), "web"))
    assert set(findings) == {"lodash", "@nestjs/common", "ip"}
    nest = findings["@nestjs/common"][0]
    assert nest.ids == frozenset({"GHSA-cj7v-w2c7-cp7c", "1103412"})
    assert nest.advisory == "GHSA-cj7v-w2c7-cp7c"
    assert nest.ecosystem == "npm"


def test_installed_versions_come_from_package_lock_nodes():
    lodash = by_package(npm_backend.parse_npm_audit(report(), lock(), "web"))["lodash"]
    assert sorted(f.version for f in lodash) == ["4.17.21", "4.17.4"]


def test_installed_versions_fall_back_to_every_instance_without_nodes():
    data = report()
    del data["vulnerabilities"]["lodash"]["nodes"]
    lodash = by_package(npm_backend.parse_npm_audit(data, lock(), "web"))["lodash"]
    assert sorted(f.version for f in lodash) == ["4.17.21", "4.17.4"]


def test_fix_available_shapes():
    findings = by_package(npm_backend.parse_npm_audit(report(), lock(), "web"))
    assert findings["lodash"][0].fix_versions == ("in range",)
    assert findings["@nestjs/common"][0].fix_versions == ("@nestjs/common@11.1.6 (semver major)",)
    assert findings["ip"][0].fix_versions == ()


def test_audit_command_uses_public_registry_and_scope(project):
    runtime = npm_backend.audit_cmd(TARGET, project, "runtime")
    assert "--registry=https://registry.npmjs.org/" in runtime and "--package-lock-only" in runtime
    assert runtime[-1] == "--omit=dev"
    assert "--omit=dev" not in npm_backend.audit_cmd(TARGET, project, "all")


def test_audit_parses_exit_1_report(monkeypatch, project):
    monkeypatch.setattr(npm_backend, "run", lambda cmd, cwd=None: completed(1, json.dumps(report())))
    assert len(npm_backend.audit(TARGET, project, "runtime", project)) == 4


@pytest.mark.parametrize(
    "result",
    [
        completed(
            1, json.dumps({"error": {"code": "ENOLOCK", "summary": "This command requires an existing lockfile."}})
        ),
        completed(1, "npm ERR! network", ""),
        completed(0, json.dumps({"message": "unexpected"})),
    ],
    ids=["npm-error-json", "invalid-json", "no-report"],
)
def test_tool_errors(monkeypatch, project, result):
    monkeypatch.setattr(proc, "sleep", lambda s: None)
    monkeypatch.setattr(npm_backend, "run", lambda cmd, cwd=None: result)
    with pytest.raises(ToolError):
        npm_backend.audit(TARGET, project, "runtime", project)


def test_fix_falls_back_per_package_on_eresolve(monkeypatch, project):
    calls = []

    def fake(cmd, cwd=None):
        calls.append(cmd[:3])
        if cmd[1] == "audit":
            (project / "package-lock.json").write_text("{}")  # partial write before the failure
            return completed(1, "", "npm ERR! code ERESOLVE")
        return completed(0)

    monkeypatch.setattr(npm_backend, "run", fake)
    findings = npm_backend.parse_npm_audit(report(), lock(), "web")
    reasons = npm_backend.fix(TARGET, project, findings, commit=False)
    assert calls == [["npm", "audit", "fix"], ["npm", "update", "@nestjs/common"], ["npm", "update", "lodash"]]
    assert json.loads((project / "package-lock.json").read_text()) == lock()
    assert reasons["@nestjs/common"] == "needs @nestjs/common@11.1.6 (semver major)"
    assert reasons["lodash"] == "no version inside the manifest ranges fixes it"
    assert "ip" not in reasons


def test_fix_reverts_manifest_edits(monkeypatch, project):
    def fake(cmd, cwd=None):
        (project / "package.json").write_text('{"name": "fixture-app", "dependencies": {"lodash": "^4.17.23"}}')
        return completed(0)

    monkeypatch.setattr(npm_backend, "run", fake)
    reasons = npm_backend.fix(TARGET, project, npm_backend.parse_npm_audit(report(), lock(), "web"), commit=False)
    assert (project / "package.json").read_text() == '{"name": "fixture-app"}\n'
    assert reasons["lodash"] == "npm update failed: package.json would change"


CRASH = (
    "npm error Cannot read properties of null (reading 'edgesOut')\n"
    "npm error A complete log of this run can be found in: /home/runner/.npm/_logs/debug-0.log\n"
)


def test_fix_reverts_a_crash_that_wrote_the_lockfile_and_reports_the_error(monkeypatch, project):
    def fake(cmd, cwd=None):
        (project / "package-lock.json").write_text("{}")  # partial write before the crash
        if cmd[1] == "update" and cmd[2] == "lodash":
            return completed(0)
        return completed(1, "", CRASH)

    monkeypatch.setattr(npm_backend, "run", fake)
    findings = npm_backend.parse_npm_audit(report(), lock(), "web")
    reasons = npm_backend.fix(TARGET, project, findings, commit=False)
    assert reasons["@nestjs/common"] == "needs @nestjs/common@11.1.6 (semver major)"
    assert reasons["lodash"] == "no version inside the manifest ranges fixes it"
    assert (project / "package-lock.json").read_text() == "{}"  # the successful update is kept


def test_fix_crash_without_a_successful_update_restores_the_lockfile(monkeypatch, project):
    def fake(cmd, cwd=None):
        (project / "package-lock.json").write_text("{}")
        return completed(1, "", CRASH)

    monkeypatch.setattr(npm_backend, "run", fake)
    reasons = npm_backend.fix(TARGET, project, npm_backend.parse_npm_audit(report(), lock(), "web"), commit=False)
    assert json.loads((project / "package-lock.json").read_text()) == lock()
    assert reasons["lodash"] == "npm update failed: Cannot read properties of null (reading 'edgesOut')"


ZEBBRA_404 = (
    "npm error code E404\n"
    "npm error 404 Not Found - GET https://registry.npmjs.org/@zebbra%2fneops-nest-graphql-client - Not found\n"
    "npm error A complete log of this run can be found in: /home/runner/.npm/_logs/debug-0.log\n"
)


@pytest.mark.parametrize("command", ["audit", "update"])
def test_registry_error_restores_the_lockfile_and_is_a_tool_error(monkeypatch, project, command):
    monkeypatch.delenv("NPM_TOKEN", raising=False)

    def fake(cmd, cwd=None):
        (project / "package-lock.json").write_text("{}")
        if cmd[1] == command:
            return completed(1, "", ZEBBRA_404)
        return completed(1, "", "npm error code ERESOLVE\n")

    monkeypatch.setattr(npm_backend, "run", fake)
    with pytest.raises(ToolError, match="could not reach the registry: code E404; 404 Not Found") as e:
        npm_backend.fix(TARGET, project, npm_backend.parse_npm_audit(report(), lock(), "web"), commit=False)
    assert "set the `npm-token` input" in str(e.value)
    assert json.loads((project / "package-lock.json").read_text()) == lock()


@pytest.fixture
def runner_temp(monkeypatch, tmp_path_factory):
    path = tmp_path_factory.mktemp("runner-temp")
    monkeypatch.setenv("RUNNER_TEMP", str(path))
    return path


@pytest.fixture
def npm_calls(monkeypatch, project, runner_temp):
    """Records each npm command with the userconfig it points to, read while the command runs."""
    calls = []

    def fake(cmd, cwd=None):
        config = Path(cmd[cmd.index("--userconfig") + 1]) if "--userconfig" in cmd else None
        calls.append((cmd, config, config.read_text() if config else None))
        return completed(1, "", "npm error code ERESOLVE\n")

    monkeypatch.setattr(npm_backend, "run", fake)
    return calls


def test_npm_token_maps_the_zebbra_scope_in_a_temporary_userconfig(monkeypatch, project, npm_calls, runner_temp):
    monkeypatch.setenv("NPM_TOKEN", "token-value")
    npm_backend.fix(TARGET, project, npm_backend.parse_npm_audit(report(), lock(), "web"), commit=False)
    assert [cmd[1] for cmd, _, _ in npm_calls] == ["audit", "update", "update"]
    for cmd, config, content in npm_calls:
        assert config.parent.parent == runner_temp and not config.exists()
        assert content == "@zebbra:registry=https://npm.pkg.github.com\n//npm.pkg.github.com/:_authToken=${NPM_TOKEN}\n"
    assert "--registry=https://registry.npmjs.org/" in npm_calls[0][0]
    assert sorted(p.name for p in project.iterdir()) == ["package-lock.json", "package.json"]


def test_without_npm_token_npm_keeps_its_own_userconfig(monkeypatch, project, npm_calls):
    monkeypatch.delenv("NPM_TOKEN", raising=False)
    npm_backend.fix(TARGET, project, npm_backend.parse_npm_audit(report(), lock(), "web"), commit=False)
    assert npm_calls and all(config is None for _, config, _ in npm_calls)


def test_installed_reads_every_instance(project):
    assert npm_backend.installed(TARGET, project)["lodash"] == ["4.17.21", "4.17.4"]


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_real_npm_audit_fix(tmp_path):
    shutil.copytree(FIXTURES / "npm-project", tmp_path, dirs_exist_ok=True)
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True)
    findings = npm_backend.audit(TARGET, tmp_path, "all", tmp_path)
    assert {(f.package, f.version, f.advisory) for f in findings} == {("minimist", "1.2.5", "GHSA-xvch-5gv4-984h")}
    npm_backend.fix(TARGET, tmp_path, findings, commit=True)
    log = subprocess.run(["git", "-C", str(tmp_path), "log", "-1", "--format=%an|%s"], capture_output=True, text=True)
    assert log.stdout == "neops-deps-bot|fix(deps): npm audit fix in .\n"
    assert npm_backend.audit(TARGET, tmp_path, "all", tmp_path) == []
