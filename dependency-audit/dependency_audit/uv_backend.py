"""uv targets: export the lockfile, audit it with pip-audit, upgrade single packages."""

from __future__ import annotations

import json
import re
import tomllib
from collections import defaultdict
from pathlib import Path

from . import PIP_AUDIT
from .config import Target
from .model import Finding, ToolError, fix_target, normalize, sort_findings, version_key
from .proc import commit_lockfile, log, run, tail, with_retry


def export_args(target: Target, scope: str) -> list[str]:
    if scope == "all":
        return ["--all-groups", "--all-extras"]
    return ["--no-dev", *(arg for group in target.groups for arg in ("--group", group))]


def export(target: Target, root: Path, out_dir: Path, scope: str) -> Path:
    """Export the target's lockfile to `out_dir/pylock.toml` (local workspace packages left out)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "pylock.toml"
    cmd = ["uv", "export", "--directory", str(root / target.path), "--format", "pylock.toml", "--frozen"]
    cmd += ["--no-emit-local", "--quiet", "-o", str(out), *export_args(target, scope)]
    p = run(cmd)
    if p.returncode != 0 or not out.is_file():
        raise ToolError(f"uv export failed for target `{target.name}`: {tail(p.stderr)}")
    return out


def parse_pip_audit(data: dict, target: str) -> list[Finding]:
    """Findings from pip-audit JSON; repeated advisories on one package version are merged."""
    merged: dict[tuple[str, str, str], tuple[set[str], set[str]]] = {}
    for dep in data.get("dependencies", []):
        package = normalize(dep["name"], "uv")
        for vuln in dep.get("vulns", []):
            ids, fixes = merged.setdefault((package, dep["version"], vuln["id"]), (set(), set()))
            ids.update([vuln["id"], *vuln.get("aliases", [])])
            fixes.update(vuln.get("fix_versions", []))
    return sort_findings(
        Finding(
            target=target,
            ecosystem="uv",
            package=package,
            version=version,
            advisory=advisory,
            ids=frozenset(ids),
            fix_versions=tuple(sorted(fixes, key=version_key)),
        )
        for (package, version, advisory), (ids, fixes) in merged.items()
    )


def pip_audit(directory: Path) -> dict:
    """Run pip-audit on the pylock in `directory`; exit 1 with valid JSON means findings."""
    p = run(
        ["uvx", "--from", PIP_AUDIT, "pip-audit", "--locked", "-f", "json", "--progress-spinner", "off", str(directory)]
    )
    if p.returncode not in (0, 1):
        raise ToolError(f"pip-audit exited {p.returncode}: {tail(p.stderr)}")
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise ToolError(f"pip-audit exited {p.returncode} without valid JSON: {tail(p.stderr)}") from None
    if not isinstance(data, dict) or "dependencies" not in data:
        raise ToolError("pip-audit JSON has no `dependencies`")
    has_vulns = any(dep.get("vulns") for dep in data["dependencies"])
    if (p.returncode == 1) != has_vulns:
        raise ToolError(
            f"pip-audit exited {p.returncode} with {'some' if has_vulns else 'no'} findings: {tail(p.stderr)}"
        )
    return data


def audit(target: Target, root: Path, scope: str, work_dir: Path) -> list[Finding]:
    pylock = export(target, root, work_dir, scope)
    if not tomllib.loads(pylock.read_text()).get("packages"):
        log(f"target `{target.name}`: export has no packages, pip-audit skipped")
        return []
    return parse_pip_audit(with_retry(lambda: pip_audit(work_dir), f"pip-audit `{target.name}`"), target.name)


def installed(target: Target, root: Path) -> dict[str, list[str]]:
    """Package name -> locked versions, from the target's uv.lock."""
    versions: dict[str, list[str]] = defaultdict(list)
    lock = tomllib.loads((root / target.lockfile).read_text())
    for package in lock.get("package", []):
        if "version" in package:
            versions[normalize(package["name"], "uv")].append(package["version"])
    return dict(versions)


def _lock(target: Target, root: Path, package: str, wanted: str | None = None) -> tuple[bool, str]:
    """`uv lock --upgrade-package` for `package` (pinned to `wanted` if given); return (success, blocker).

    A failure without "No solution found" (network, index, uv itself) restores the lockfile and raises ToolError.
    """
    spec = f"{package}=={wanted}" if wanted else package
    lockfile = root / target.lockfile
    before = lockfile.read_bytes()
    p = run(["uv", "lock", "--directory", str(root / target.path), "--upgrade-package", spec])
    if p.returncode != 0 and "No solution found" not in p.stderr:
        lockfile.write_bytes(before)
        raise ToolError(f"uv lock --upgrade-package {spec} failed: {tail(p.stderr)}")
    return p.returncode == 0, conflict(p.stderr, package, wanted or "")


def conflict(stderr: str, package: str, wanted: str) -> str:
    """The part of a uv resolution error that names what keeps `package` below `wanted`, or ""."""
    lines = [line.lstrip(" ╰─▶").strip().removeprefix("cause: ") for line in stderr.splitlines()]
    hints = [
        line.removeprefix("hint: ").split(". Consider")[0]
        for line in lines
        if line.startswith("hint: The `requires-python`")
    ]
    for hint in hints:
        if f"{package}==" in hint:
            return hint
    clauses = [
        line.removeprefix(prefix).split(", we can conclude")[0]
        for line in lines
        for prefix in ("Because ", "And because ")
        if line.startswith(prefix)
    ]
    requirement = re.compile(rf"depends on {re.escape(package)}(\[[^\]]*\])?(?P<spec>[<>=!~][^ ,]*)")
    for clause in clauses:
        if any(m["spec"] != f"=={wanted}" for m in requirement.finditer(clause)):
            return clause.replace(f" and {package}=={wanted}", "")
    return (clauses or hints or [""])[0]


def _reaches(target: Target, root: Path, package: str, wanted: str) -> bool:
    return all(version_key(v) >= version_key(wanted) for v in installed(target, root).get(package, []))


def blocker(target: Target, root: Path, package: str) -> str:
    """Direct dependents of `package`, from `uv tree --invert`."""
    p = run(
        ["uv", "tree", "--directory", str(root / target.path), "--frozen", "--invert", "--package", package]
        + ["--depth", "1", "--all-groups"]
    )
    lines = [line.lstrip("├└│─ ").strip() for line in p.stdout.splitlines()[1:] if line.strip()]
    return "; ".join(lines) if p.returncode == 0 and lines else tail(p.stderr, 1) or "unknown"


def fix(target: Target, root: Path, findings: list[Finding], commit: bool) -> dict[str, str]:
    """Upgrade each vulnerable package to its fix target, one at a time; return package -> blocker."""
    lockfile = root / target.lockfile
    by_package: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_package[f.package].append(f)
    blocked: dict[str, str] = {}
    for package in sorted(by_package):
        wanted = fix_target(by_package[package])
        if wanted is None:
            continue
        before = lockfile.read_bytes()
        old = ", ".join(sorted(set(f.version for f in by_package[package]), key=version_key))
        ok, reason = _lock(target, root, package, wanted)
        if not (ok and _reaches(target, root, package, wanted)):
            lockfile.write_bytes(before)
            ok, _ = _lock(target, root, package)
            ok = ok and _reaches(target, root, package, wanted)
        if not ok:
            lockfile.write_bytes(before)
            blocked[package] = reason or f"required by {blocker(target, root, package)}"
            log(f"target `{target.name}`: {package} {old} -> {wanted} blocked: {blocked[package]}")
            continue
        new = ", ".join(sorted(installed(target, root).get(package, ["removed"]), key=version_key))
        log(f"target `{target.name}`: {package} {old} -> {new}")
        if commit:
            fixed = [f for f in by_package[package] if (t := fix_target([f])) and version_key(t) <= version_key(wanted)]
            ids = ", ".join(sorted({f.display_id for f in fixed}))
            commit_lockfile(root, target.lockfile, f"fix(deps): bump {package} {old} -> {new} ({ids})")
    return blocked
