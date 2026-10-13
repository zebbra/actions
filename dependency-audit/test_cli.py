"""CLI modes on real git repos; the backends' audit and fix are replaced by lockfile-driven fakes.

A fake lockfile is JSON: {"packages": {name: version}, "vulns": [[package, version, advisory, [aliases], [fixes]]]}.
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dependency_audit import cli
from dependency_audit.model import Finding, ToolError, normalize

CONFIG = """
[[target]]
name = "backend"
ecosystem = "uv"
path = "backend"
{extra}
"""

URLLIB3 = ["urllib3", "2.7.0", "PYSEC-2026-4175", ["GHSA-8988-9cw3-xx77"], ["2.8.0"]]
MULTIDICT = ["multidict", "6.7.1", "CVE-2026-104874", ["GHSA-54p9-h82j-f925"], ["6.9.1"]]
DJANGO = ["django", "4.2.30", "PYSEC-2026-4035", ["GHSA-wvqv-fj8w-qmhm"], ["5.2.17", "6.0.8"]]
ECDSA = ["ecdsa", "0.19.2", "PYSEC-2026-1325", ["GHSA-wj6h-64fc-37mp"], []]


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def write_lock(root, vulns, packages=None, path="backend/uv.lock"):
    packages = packages or {v[0]: v[1] for v in vulns}
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(json.dumps({"packages": packages, "vulns": vulns}, indent=1) + "\n")


def write_config(root, extra=""):
    (root / ".github").mkdir(exist_ok=True)
    (root / ".github/dependency-audit.toml").write_text(CONFIG.format(extra=extra))


def commit(root, message="change"):
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def fake_audit(target, root, scope, work):
    calls.append((str(root), target.name))
    lock = json.loads((Path(root) / target.lockfile).read_text())
    if lock.get("error"):
        raise ToolError(lock["error"])
    return [
        Finding(
            target=target.name,
            ecosystem=target.ecosystem,
            package=normalize(package, target.ecosystem),
            version=version,
            advisory=advisory,
            ids=frozenset([advisory, *aliases]),
            fix_versions=tuple(fixes),
        )
        for package, version, advisory, aliases, fixes in lock["vulns"]
    ]


def fake_installed(target, root):
    return {k: [v] for k, v in json.loads((Path(root) / target.lockfile).read_text())["packages"].items()}


def fake_fix(target, root, findings, commit):
    """Upgrades every package whose fix target is listed in the lock's "allowed" map; the rest is blocked."""
    lockfile = Path(root) / target.lockfile
    lock = json.loads(lockfile.read_text())
    blocked = {}
    for f in findings:
        allowed = lock.get("allowed", {}).get(f.package)
        if allowed and f.fix_versions:
            lock["vulns"] = [v for v in lock["vulns"] if v[0] != f.package]
            lock["packages"][f.package] = allowed
        elif f.fix_versions:
            blocked[f.package] = "app v0.0.0"
    lockfile.write_text(json.dumps(lock, indent=1) + "\n")
    return blocked


calls: list = []


@pytest.fixture
def repo(tmp_path, monkeypatch):
    calls.clear()
    fake = SimpleNamespace(audit=fake_audit, installed=fake_installed, fix=fake_fix)
    monkeypatch.setattr(cli, "BACKENDS", {"uv": fake, "npm": fake})
    monkeypatch.setattr(cli, "runtime_packages", lambda t, root, work: {"urllib3", "django"})
    git(tmp_path, "init", "-q")
    write_config(tmp_path)
    write_lock(tmp_path, [URLLIB3])
    (tmp_path / "README.md").write_text("readme\n")
    commit(tmp_path, "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(*argv, tmp):
    out = tmp.parent / "report.json"
    summary = tmp.parent / "summary.md"
    summary.write_text("")
    code = cli.main([*argv, "--json-out", str(out), "--summary-out", str(summary)])
    report = json.loads(out.read_text()) if out.exists() else None
    if out.exists():
        out.unlink()
    return code, report, summary.read_text()


def ids(findings):
    return [(f["package"], f["advisory"]) for f in findings]


def test_g1_no_lockfile_change_skips_without_audit(repo):
    base = git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("changed\n")
    commit(repo)
    code, report, summary = run("diff", "--base", base, tmp=repo)
    assert code == 0 and report["skipped"] is True
    assert "no dependency change" in summary
    assert calls == []


def test_g2_added_vulnerable_package_fails_with_exactly_that_finding(repo):
    base = git(repo, "rev-parse", "HEAD")
    write_lock(repo, [URLLIB3, MULTIDICT])
    commit(repo)
    code, report, summary = run("diff", "--base", base, tmp=repo)
    assert code == 1
    assert ids(report["new"]) == [("multidict", "CVE-2026-104874")]
    assert ids(report["pre_existing"]) == [("urllib3", "PYSEC-2026-4175")]
    assert "### New (fails the check)" in summary


def test_g3_unrelated_bump_with_drift_passes_and_lists_drift(repo):
    base = git(repo, "rev-parse", "HEAD")
    write_lock(repo, [URLLIB3], packages={"urllib3": "2.7.0", "six": "1.17.0"})
    commit(repo)
    code, report, summary = run("diff", "--base", base, tmp=repo)
    assert code == 0
    assert report["new"] == []
    assert ids(report["pre_existing"]) == [("urllib3", "PYSEC-2026-4175")]
    assert "### Pre-existing (warning)" in summary


def test_version_bump_and_rekeyed_advisory_are_not_new(repo):
    base = git(repo, "rev-parse", "HEAD")
    write_lock(repo, [["urllib3", "2.7.1", "GHSA-8988-9cw3-xx77", ["CVE-2026-97687"], ["2.8.0"]]])
    commit(repo)
    code, report, _ = run("diff", "--base", base, tmp=repo)
    assert code == 0 and report["new"] == []


IGNORE_MULTIDICT = '[[ignore]]\nid = "GHSA-54p9-h82j-f925"\nreason = "Not reachable."\n'


def test_g6_vulnerability_and_its_ignore_in_one_pr_fails(repo):
    base = git(repo, "rev-parse", "HEAD")
    write_lock(repo, [URLLIB3, MULTIDICT])
    write_config(repo, IGNORE_MULTIDICT)
    commit(repo)
    code, report, _ = run("diff", "--base", base, tmp=repo)
    assert code == 1
    assert ids(report["new"]) == [("multidict", "CVE-2026-104874")]


def test_ignore_landed_first_applies_in_diff(repo):
    write_config(repo, IGNORE_MULTIDICT)
    base = commit(repo, "ignore first")
    write_lock(repo, [URLLIB3, MULTIDICT])
    commit(repo)
    code, report, _ = run("diff", "--base", base, tmp=repo)
    assert code == 0
    assert ids(report["ignored"]) == [("multidict", "CVE-2026-104874")]


def test_target_missing_at_base_counts_as_empty(repo):
    base = git(repo, "rev-parse", "HEAD")
    write_config(repo, '[[target]]\nname = "sdk"\necosystem = "uv"\npath = "sdk"\n')
    write_lock(repo, [MULTIDICT], path="sdk/uv.lock")
    commit(repo)
    code, report, _ = run("diff", "--base", base, tmp=repo)
    assert code == 1 and ids(report["new"]) == [("multidict", "CVE-2026-104874")]
    assert git(repo, "worktree", "list").count("\n") == 0


def test_full_fails_only_on_gating_targets_and_warns_on_unused_ignores(repo, capsys):
    write_config(
        repo,
        '[[target]]\nname = "docs"\necosystem = "uv"\npath = "docs"\nfail-on = "none"\n'
        '[[ignore]]\nid = "GHSA-zzzz-zzzz-zzzz"\nreason = "r"\n',
    )
    write_lock(repo, [MULTIDICT], path="docs/uv.lock")
    write_lock(repo, [])
    commit(repo)
    code, report, _ = run("full", tmp=repo)
    assert code == 0
    assert ids(report["reported"]) == [("multidict", "CVE-2026-104874")]
    assert "::warning::unused ignore GHSA-zzzz-zzzz-zzzz" in capsys.readouterr().out
    write_lock(repo, [URLLIB3])
    commit(repo)
    code, report, _ = run("full", tmp=repo)
    assert code == 1 and ids(report["failing"]) == [("urllib3", "PYSEC-2026-4175")]


def test_tool_error_exits_2_and_is_not_a_finding(repo, capsys):
    (repo / "backend/uv.lock").write_text(json.dumps({"error": "pip-audit exited 2: ConnectionError"}))
    commit(repo)
    code, report, summary = run("full", tmp=repo)
    assert code == 2 and report is None
    assert "tool error, not a finding" in capsys.readouterr().out
    assert "tool error, not a finding" in summary


@pytest.mark.parametrize(
    "extra, message",
    [('[[ignore]]\nid = "X"\n', "`reason` is required"), ('unknown = "x"\n', "unknown key(s): unknown")],
)
def test_config_errors_exit_2(repo, capsys, extra, message):
    write_config(repo, extra)
    commit(repo)
    code, _, _ = run("full", tmp=repo)
    assert code == 2
    assert message in capsys.readouterr().out


def test_check_flags_uncovered_lockfile(repo, capsys):
    write_lock(repo, [], path="tools/uv.lock")
    write_lock(repo, [], path=".make_scripts/mkdocs/uv.lock")
    commit(repo)
    assert run("check", tmp=repo)[0] == 2
    assert "tools/uv.lock is neither a target nor excluded" in capsys.readouterr().out
    write_config(repo, '[[exclude]]\nglob = "tools/**"\nreason = "Dev tooling."\n')
    commit(repo)
    assert run("check", tmp=repo)[0] == 0


def fix_repo(repo):
    lock = {
        "packages": {"urllib3": "2.7.0", "django": "4.2.30", "ecdsa": "0.19.2", "multidict": "6.7.1"},
        "vulns": [URLLIB3, DJANGO, ECDSA, MULTIDICT],
        "allowed": {"urllib3": "2.8.0", "multidict": "6.9.1"},
    }
    (repo / "backend/uv.lock").write_text(json.dumps(lock, indent=1) + "\n")
    write_config(
        repo,
        IGNORE_MULTIDICT + '[[ignore]]\nid = "GHSA-zzzz-zzzz-zzzz"\nreason = "r"\n',
    )
    commit(repo)


def test_fix_classifies_and_renders(repo, tmp_path):
    fix_repo(repo)
    pr, issue = tmp_path.parent / "pr.md", tmp_path.parent / "issue.md"
    code = cli.main(["fix", "--pr-body-out", str(pr), "--issue-body-out", str(issue)])
    assert code == 0
    pr_body, issue_body = pr.read_text(), issue.read_text()
    assert "| backend (uv) | `multidict` | 6.7.1 → 6.9.1 |" in pr_body
    assert "| backend (uv) | `urllib3` | 2.7.0 → 2.8.0 |" in pr_body
    assert "| runtime |" in pr_body and "| dev |" in pr_body
    assert "Not fixed here: see {triage_issue}" in pr_body
    blocked = issue_body.split("### Blocked")[1].split("###")[0]
    assert "`django`" in blocked and "app v0.0.0" in blocked
    assert "`ecdsa`" in issue_body.split("### No fix available")[1].split("###")[0]
    assert "GHSA-54p9-h82j-f925" in issue_body.split("### Ignored, now fixable")[1].split("###")[0]
    assert "GHSA-zzzz-zzzz-zzzz" in issue_body.split("### Ignored, unused")[1]
    assert "bot not configured" not in issue_body.lower()


def test_fix_dry_run_restores_lockfiles_and_lists_fixable(repo, tmp_path):
    fix_repo(repo)
    before = (repo / "backend/uv.lock").read_text()
    pr, issue = tmp_path.parent / "pr.md", tmp_path.parent / "issue.md"
    assert cli.main(["fix", "--dry-run", "--pr-body-out", str(pr), "--issue-body-out", str(issue)]) == 0
    assert (repo / "backend/uv.lock").read_text() == before
    assert pr.read_text() == ""
    assert "Bot not configured: fixable findings are listed here and no PR is opened." in issue.read_text()
    assert "`urllib3` | 2.7.0 → 2.8.0" in issue.read_text()


def test_fix_tool_error_lands_in_issue(repo, tmp_path):
    (repo / "backend/uv.lock").write_text(json.dumps({"error": "npm audit failed: E401"}))
    commit(repo)
    issue = tmp_path.parent / "issue.md"
    assert cli.main(["fix", "--issue-body-out", str(issue)]) == 0
    assert "### Tool errors" in issue.read_text() and "E401" in issue.read_text()


def test_fix_with_nothing_to_report_writes_empty_bodies(repo, tmp_path):
    write_lock(repo, [])
    commit(repo)
    pr, issue = tmp_path.parent / "pr.md", tmp_path.parent / "issue.md"
    assert cli.main(["fix", "--pr-body-out", str(pr), "--issue-body-out", str(issue)]) == 0
    assert pr.read_text() == "" and issue.read_text() == ""


def test_fix_commit_requires_clean_tree(repo, capsys):
    (repo / "README.md").write_text("dirty\n")
    assert cli.main(["fix", "--commit"]) == 2
    assert "clean working tree" in capsys.readouterr().out
