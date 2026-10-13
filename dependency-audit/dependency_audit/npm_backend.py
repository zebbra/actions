"""npm targets: `npm audit` on the lockfile alone, `npm audit fix` with a per-package `npm update` fallback."""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from collections import defaultdict
from pathlib import Path

from . import GITHUB_PACKAGES, NPM_REGISTRY
from .config import Target
from .model import Finding, ToolError, sort_findings
from .proc import commit_lockfile, log, run, tail, with_retry

IN_RANGE = "in range"
# npm error codes that mean the registry could not be reached or refused the request: a tool error, not a blocker.
REGISTRY_CODES = r"E401|E403|E404|ENEEDAUTH|E500|E502|E503|E504|ETIMEDOUT|ECONNRESET|ECONNREFUSED|ENOTFOUND|EAI_AGAIN"
REGISTRY_ERROR = re.compile(rf"^npm (?:error|ERR!) code (?:{REGISTRY_CODES})\b", re.MULTILINE)


def audit_cmd(target: Target, root: Path, scope: str) -> list[str]:
    cmd = ["npm", "audit", "--prefix", str(root / target.path), "--package-lock-only", "--json"]
    cmd.append(f"--registry={NPM_REGISTRY}")
    if scope == "runtime":
        cmd.append("--omit=dev")
    return cmd


def npm_audit(target: Target, root: Path, scope: str) -> dict:
    p = run(audit_cmd(target, root, scope))
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise ToolError(f"npm audit exited {p.returncode} without valid JSON: {tail(p.stderr)}") from None
    if not isinstance(data, dict) or "error" in data:
        error = data.get("error", {}) if isinstance(data, dict) else {}
        raise ToolError(f"npm audit failed: {error.get('summary') or error.get('code') or tail(p.stderr)}")
    if p.returncode not in (0, 1) or not isinstance(data.get("vulnerabilities"), dict):
        raise ToolError(f"npm audit exited {p.returncode} without a vulnerability report: {tail(p.stderr)}")
    return data


def fix_versions(fix_available: object) -> tuple[str, ...]:
    """`fixAvailable`: true -> a fix inside the manifest ranges; an object -> the package@version npm would install."""
    if fix_available is True:
        return (IN_RANGE,)
    if isinstance(fix_available, dict):
        major = " (semver major)" if fix_available.get("isSemVerMajor") else ""
        return (f"{fix_available.get('name')}@{fix_available.get('version')}{major}",)
    return ()


def _instances(packages: dict, name: str) -> list[str]:
    return [p for p in packages if p == f"node_modules/{name}" or p.endswith(f"/node_modules/{name}")]


def parse_npm_audit(data: dict, lock: dict, target: str) -> list[Finding]:
    """One finding per advisory object in `via` and installed version; string `via` entries are skipped."""
    packages = lock.get("packages", {})
    findings: list[Finding] = []
    for name, entry in data.get("vulnerabilities", {}).items():
        advisories = [v for v in entry.get("via", []) if isinstance(v, dict)]
        if not advisories:
            continue
        nodes = entry.get("nodes") or _instances(packages, name)
        versions = sorted({packages.get(n, {}).get("version") for n in nodes} - {None}) or ["unknown"]
        fixes = fix_versions(entry.get("fixAvailable"))
        for advisory in advisories:
            ghsa = str(advisory.get("url", "")).rstrip("/").rsplit("/", 1)[-1]
            ids = {str(advisory["source"])} | ({ghsa} if ghsa.startswith("GHSA-") else set())
            for version in versions:
                findings.append(
                    Finding(
                        target=target,
                        ecosystem="npm",
                        package=name,
                        version=version,
                        advisory=ghsa if ghsa.startswith("GHSA-") else str(advisory["source"]),
                        ids=frozenset(ids),
                        fix_versions=fixes,
                    )
                )
    return sort_findings(findings)


def _lock(target: Target, root: Path) -> dict:
    try:
        return json.loads((root / target.lockfile).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ToolError(f"cannot read {target.lockfile}: {e}") from None


def audit(target: Target, root: Path, scope: str, work_dir: Path) -> list[Finding]:
    data = with_retry(lambda: npm_audit(target, root, scope), f"npm audit `{target.name}`")
    return parse_npm_audit(data, _lock(target, root), target.name)


def installed(target: Target, root: Path) -> dict[str, list[str]]:
    """Package name -> installed versions, from the target's package-lock.json."""
    versions: dict[str, set[str]] = defaultdict(set)
    for path, entry in _lock(target, root).get("packages", {}).items():
        if "node_modules/" in path and "version" in entry:
            versions[path.rsplit("node_modules/", 1)[1]].add(entry["version"])
    return {name: sorted(v) for name, v in versions.items()}


def npm_error(stderr: str, returncode: int) -> str:
    """The first two `npm error` lines without the debug-log pointer, else the last stderr line or the exit code."""
    lines = [
        line.removeprefix("npm error").removeprefix("npm ERR!").strip()
        for line in stderr.splitlines()
        if line.startswith(("npm error", "npm ERR!"))
    ]
    lines = [line for line in lines if line and not line.startswith(("A complete log", "Log files were not written"))]
    return "; ".join(lines[:2]) or tail(stderr, 1) or f"exit {returncode}"


def userconfig(directory: Path) -> list[str]:
    """`--userconfig` mapping `@zebbra` to GitHub Packages when NPM_TOKEN is set, else no arguments.

    The file holds the literal `${NPM_TOKEN}`, which npm expands from the environment, so the token is never written.
    """
    if not os.environ.get("NPM_TOKEN"):
        return []
    path = directory / "npmrc"
    host = GITHUB_PACKAGES.removeprefix("https:")
    path.write_text(f"@zebbra:registry={GITHUB_PACKAGES}\n{host}/:_authToken=${{NPM_TOKEN}}\n")
    return ["--userconfig", str(path)]


def fix(target: Target, root: Path, findings: list[Finding], commit: bool) -> dict[str, str]:
    """`npm audit fix` within semver ranges, per-package `npm update` on failure; return package -> blocker.

    A registry or auth error raises ToolError after the restore: every further call would fail the same way.
    """
    fixable = sorted({f.package for f in findings if f.fix_versions})
    reasons = {
        f.package: f"needs {f.fix_versions[0]}" for f in findings if f.fix_versions and f.fix_versions[0] != IN_RANGE
    }
    if not fixable:
        return reasons
    lockfile, manifest = root / target.lockfile, root / target.manifest
    common = ["--prefix", str(root / target.path), "--package-lock-only", "--ignore-scripts"]

    def attempt(cmd: list[str]) -> str | None:
        """Run cmd; restore lockfile and manifest and return the error when it fails or edits the manifest."""
        before = (lockfile.read_bytes(), manifest.read_bytes())
        p = run(cmd)
        if p.returncode != 0:
            error = npm_error(p.stderr, p.returncode)
        elif manifest.read_bytes() != before[1]:
            error = f"{target.manifest} would change"
        else:
            return None
        lockfile.write_bytes(before[0])
        manifest.write_bytes(before[1])
        if p.returncode != 0 and REGISTRY_ERROR.search(p.stderr):
            hint = ""
            if "@zebbra" in p.stderr and not os.environ.get("NPM_TOKEN"):
                hint = " (@zebbra packages resolve from GitHub Packages: set the `npm-token` input)"
            raise ToolError(f"{shlex.join(cmd[:3])} could not reach the registry: {error}{hint}")
        return error

    with tempfile.TemporaryDirectory(prefix="dependency-audit-", dir=os.environ.get("RUNNER_TEMP")) as tmp:
        common += userconfig(Path(tmp))
        error = attempt(["npm", "audit", "fix", *common, f"--registry={NPM_REGISTRY}"])
        if error:
            log(f"target `{target.name}`: npm audit fix failed ({error}); updating per package")
            for package in fixable:
                update_error = attempt(["npm", "update", package, *common])
                if update_error:
                    reasons.setdefault(package, f"npm update failed: {update_error}")
    for package in fixable:
        reasons.setdefault(package, "no version inside the manifest ranges fixes it")
    if commit:
        commit_lockfile(root, target.lockfile, f"fix(deps): npm audit fix in {target.path}")
    return reasons
