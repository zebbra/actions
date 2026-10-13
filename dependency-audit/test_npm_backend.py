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
