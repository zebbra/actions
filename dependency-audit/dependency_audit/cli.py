"""`dependency-audit` command line: check | diff | full | fix. Exit codes: 0 ok, 1 findings, 2 tool or config error."""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import TypeVar

from . import __version__, config as config_mod, npm_backend, uv_backend
from .config import Config, Target
from .discover import check, tracked_lockfiles
from .model import (
    ConfigError,
    Finding,
    ToolError,
    common_ignores,
    fix_target,
    new_findings,
    normalize,
    sort_findings,
    split_ignored,
    unused_ignores,
    version_key,
)
from .proc import GuardError, log, require_clean
from .render import Row, diff_summary, full_summary, issue_body, pr_body, skipped_summary

BACKENDS = {"uv": uv_backend, "npm": npm_backend}
T = TypeVar("T")
EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *args], check=check, capture_output=True, text=True)


def repo_root() -> Path:
    p = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if p.returncode != 0:
        raise ConfigError("not inside a git repository")
    return Path(p.stdout.strip())


def labels(config: Config) -> dict[str, str]:
    return {t.name: t.label for t in config.targets}


def audit(target: Target, root: Path, scope: str, work: Path) -> list[Finding]:
    return BACKENDS[target.ecosystem].audit(target, root, scope, work)


def finding_json(f: Finding) -> dict:
    data = asdict(f)
    data["ids"] = sorted(f.ids)
    data["fix_versions"] = list(f.fix_versions)
    return data


class Output:
    """Writes the files named by the common options and prints the summary."""

    def __init__(self, args: argparse.Namespace):
        self.args = args

    def _write(self, option: str, content: str, append: bool = False) -> None:
        path = getattr(self.args, option, None)
        if path:
            with open(path, "a" if append else "w", encoding="utf-8") as fh:
                fh.write(content)

    def summary(self, markdown: str) -> None:
        print(markdown)
        self._write("summary_out", markdown + "\n", append=True)

    def json(self, data: dict) -> None:
        self._write("json_out", json.dumps(data, indent=2) + "\n")

    def pr_body(self, body: str) -> None:
        self._write("pr_body_out", body)

    def issue_body(self, body: str) -> None:
        self._write("issue_body_out", body)


def load_checked(args: argparse.Namespace, root: Path) -> Config:
    config = config_mod.load(root / args.config)
    check(config, root)
    return config


def cmd_check(args: argparse.Namespace, root: Path, out: Output) -> int:
    config = load_checked(args, root)
    out.summary(
        "## Dependency audit: config check passed\n\n"
        f"{len(tracked_lockfiles(root))} tracked lockfile(s) covered by {len(config.targets)} target(s) "
        "and the excludes.\n"
    )
    return EXIT_OK


@contextlib.contextmanager
def base_tree(root: Path, base: str, work: Path) -> Iterator[Path]:
    path = work / "base"
    p = git(root, "worktree", "add", "--detach", str(path), base, check=False)
    if p.returncode != 0:
        raise ToolError(f"git worktree add {base} failed: {p.stderr.strip()}")
    try:
        yield path
    finally:
        git(root, "worktree", "remove", "--force", str(path), check=False)


def base_config(root: Path, base: str, path: str) -> Config | None:
    p = git(root, "show", f"{base}:{path}", check=False)
    if p.returncode != 0:
        return None
    try:
        return config_mod.parse(tomllib.loads(p.stdout), f"{base}:{path}")
    except (ConfigError, tomllib.TOMLDecodeError) as e:
        log(f"base config ignored: {e}")
        return None


def cmd_diff(args: argparse.Namespace, root: Path, out: Output) -> int:
    config = load_checked(args, root)
    base = args.base
    if git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False).returncode != 0:
        raise ToolError(f"base revision {base} is not available (fetch it first)")
    paths = sorted({t.lockfile for t in config.targets} | {args.config})
    if git(root, "diff", "--quiet", base, "HEAD", "--", *paths, check=False).returncode == 0:
        out.summary(skipped_summary(base))
        out.json({"mode": "diff", "base": base, "skipped": True})
        return EXIT_OK
    previous = base_config(root, base, args.config)
    ignores = common_ignores(previous.ignores if previous else (), config.ignores)
    head: list[Finding] = []
    base_findings: list[Finding] = []
    with tempfile.TemporaryDirectory(prefix="dependency-audit-") as tmp:
        work = Path(tmp)
        with base_tree(root, base, work) as base_root:
            for t in config.targets:
                head += audit(t, root, t.scope, work / "head" / t.name)
                if (base_root / t.lockfile).is_file():
                    base_findings += audit(t, base_root, t.scope, work / "base-audit" / t.name)
    new = new_findings(head, base_findings)
    new_active, new_ignored = split_ignored(new, ignores)
    pre_active, pre_ignored = split_ignored([f for f in head if f not in new], config.ignores)
    out.summary(
        diff_summary(
            base,
            labels(config),
            sort_findings(new_active),
            sort_findings(pre_active),
            sort_findings(new_ignored + pre_ignored),
        )
    )
    out.json(
        {
            "mode": "diff",
            "base": base,
            "skipped": False,
            "new": [finding_json(f) for f in sort_findings(new_active)],
            "pre_existing": [finding_json(f) for f in sort_findings(pre_active)],
            "ignored": [finding_json(f) for f in sort_findings(new_ignored + pre_ignored)],
        }
    )
    for f in sort_findings(new_active):
        print(f"::error::new finding: {f.target} {f.package} {f.version} {f.display_id}")
    return EXIT_FINDINGS if new_active else EXIT_OK


def cmd_full(args: argparse.Namespace, root: Path, out: Output) -> int:
    config = load_checked(args, root)
    findings: list[Finding] = []
    with tempfile.TemporaryDirectory(prefix="dependency-audit-") as tmp:
        for t in config.targets:
            findings += audit(t, root, t.scope, Path(tmp) / t.name)
    active, ignored = split_ignored(findings, config.ignores)
    gating = {t.name for t in config.targets if t.fail_on == "any"}
    failing = sort_findings(f for f in active if f.target in gating)
    reported = sort_findings(f for f in active if f.target not in gating)
    unused = unused_ignores(config.ignores, findings)
    for i in unused:
        print(f"::warning::unused ignore {i.id}" + (f" ({i.package})" if i.package else ""))
    out.summary(full_summary(labels(config), failing, reported, sort_findings(ignored), unused))
    out.json(
        {
            "mode": "full",
            "failing": [finding_json(f) for f in failing],
            "reported": [finding_json(f) for f in reported],
            "ignored": [finding_json(f) for f in sort_findings(ignored)],
            "unused_ignores": [asdict(i) for i in unused],
        }
    )
    return EXIT_FINDINGS if failing else EXIT_OK


def has_fix(f: Finding) -> bool:
    if f.ecosystem == "uv":
        return fix_target([f]) is not None
    return bool(f.fix_versions)


def runtime_packages(target: Target, root: Path, work: Path) -> set[str]:
    """Packages the target ships (its runtime scope)."""
    if target.ecosystem == "uv":
        pylock = uv_backend.export(target, root, work, "runtime")
        return {normalize(p["name"], "uv") for p in tomllib.loads(pylock.read_text()).get("packages", [])}
    lock = json.loads((root / target.lockfile).read_text())
    return {
        path.rsplit("node_modules/", 1)[1]
        for path, entry in lock.get("packages", {}).items()
        if "node_modules/" in path and not entry.get("dev")
    }


def cmd_fix(args: argparse.Namespace, root: Path, out: Output) -> int:
    if args.commit and args.dry_run:
        raise ConfigError("--commit and --dry-run exclude each other")
    config = load_checked(args, root)
    if args.commit:
        require_clean(root)
    targets = sorted(config.targets, key=lambda t: list(BACKENDS).index(t.ecosystem))
    snapshots = {t.lockfile: (root / t.lockfile).read_bytes() for t in targets}
    errors: list[tuple[str, str]] = []
    pre: dict[str, list[Finding]] = {}
    post: dict[str, list[Finding]] = {}
    runtime: dict[str, set[str]] = {}
    blockers: dict[str, dict[str, str]] = {}

    def attempt(t: Target, step: str, fn: Callable[[], T]) -> T | None:
        try:
            return fn()
        except ToolError as e:
            print(f"::error::dependency-audit tool error, not a finding ({t.name}, {step}): {e}")
            errors.append((t.name, f"{step}: {e}"))
            return None

    with tempfile.TemporaryDirectory(prefix="dependency-audit-") as tmp:
        work = Path(tmp)
        for t in targets:
            found = attempt(t, "audit", lambda: audit(t, root, "all", work / "pre" / t.name))
            if found is not None:
                pre[t.name] = found
                runtime[t.name] = (
                    attempt(t, "scope", lambda: runtime_packages(t, root, work / "runtime" / t.name)) or set()
                )
        for t in targets:
            if t.name not in pre:
                continue
            reasons = attempt(t, "fix", lambda: BACKENDS[t.ecosystem].fix(t, root, pre[t.name], args.commit))
            if reasons is None and args.commit:
                git(root, "checkout", "--", t.lockfile, check=False)
            blockers[t.name] = reasons or {}
            found = attempt(t, "re-audit", lambda: audit(t, root, "all", work / "post" / t.name))
            if found is not None:
                post[t.name] = found
        rows = fix_rows(targets, root, pre, post, runtime)
        if args.dry_run:
            for lockfile, content in snapshots.items():
                (root / lockfile).write_bytes(content)

    fixed = [
        f for t in targets if t.name in post for f in pre[t.name] if not any(f.same_advisory(p) for p in post[t.name])
    ]
    blocked: list[Finding] = []
    no_fix: list[Finding] = []
    blocker_text: dict[Finding, str] = {}
    for t in targets:
        for f in post.get(t.name, []):
            if any(i.matches(f) for i in config.ignores):
                continue
            if has_fix(f):
                blocked.append(f)
                blocker_text[f] = blockers.get(t.name, {}).get(f.package, "unknown")
            else:
                no_fix.append(f)
    now_fixable = [i for i in config.ignores if any(i.matches(f) for f in fixed)]
    unused = [] if len(pre) < len(targets) else unused_ignores(config.ignores, [f for fs in pre.values() for f in fs])
    report_only = {t.name for t in targets if t.fail_on == "none"}
    issue = issue_body(
        labels(config),
        dry_run=args.dry_run,
        fixable=rows,
        blocked=sort_findings(blocked),
        blockers=blocker_text,
        no_fix=sort_findings(no_fix),
        now_fixable=now_fixable,
        unused=unused,
        report_only=report_only,
        errors=errors,
    )
    body = "" if args.dry_run else pr_body(rows, has_triage=bool(issue))
    out.pr_body(body)
    out.issue_body(issue)
    verb = "fixable (dry run)" if args.dry_run else "fixed"
    out.summary(
        f"## Dependency audit (fix): {len(rows)} package(s) {verb}, {len(blocked)} blocked, "
        f"{len(no_fix)} without fix\n\n" + (body or "") + ("\n" + issue if issue else "")
    )
    out.json(
        {
            "mode": "fix",
            "dry_run": args.dry_run,
            "fixed": [asdict(r) for r in rows],
            "blocked": [finding_json(f) | {"blocker": blocker_text[f]} for f in sort_findings(blocked)],
            "no_fix": [finding_json(f) for f in sort_findings(no_fix)],
            "ignored_now_fixable": [asdict(i) for i in now_fixable],
            "unused_ignores": [asdict(i) for i in unused],
            "errors": [{"target": t, "error": e} for t, e in errors],
        }
    )
    return EXIT_OK


def fix_rows(
    targets: list[Target],
    root: Path,
    pre: dict[str, list[Finding]],
    post: dict[str, list[Finding]],
    runtime: dict[str, set[str]],
) -> list[Row]:
    rows: list[Row] = []
    for t in targets:
        if t.name not in post:
            continue
        fixed: dict[str, list[Finding]] = {}
        for f in pre[t.name]:
            if not any(f.same_advisory(p) for p in post[t.name]):
                fixed.setdefault(f.package, []).append(f)
        if not fixed:
            continue
        now = BACKENDS[t.ecosystem].installed(t, root)
        for package, fs in sorted(fixed.items()):
            rows.append(
                Row(
                    target=t.label,
                    package=package,
                    from_versions=tuple(sorted({f.version for f in fs}, key=version_key)),
                    to_versions=tuple(now.get(package, ["removed"])),
                    ids=tuple(sorted({f.display_id for f in fs})),
                    scope="runtime" if package in runtime.get(t.name, set()) else "dev",
                )
            )
    return rows


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help=f"config file (default {config_mod.DEFAULT_PATH})")
    for name, what in (
        ("--json-out", "machine-readable report"),
        ("--summary-out", "Markdown summary, appended (pass $GITHUB_STEP_SUMMARY in CI)"),
        ("--pr-body-out", "bot PR body (fix mode; empty when nothing was fixed)"),
        ("--issue-body-out", "triage issue body (fix mode; empty when there is nothing to triage)"),
    ):
        common.add_argument(name, default=argparse.SUPPRESS, metavar="FILE", help=what)
    p = argparse.ArgumentParser(prog="dependency-audit", description=__doc__, parents=[common])
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check", parents=[common], help="every tracked lockfile is a target or excluded")
    diff = sub.add_parser("diff", parents=[common], help="fail only on findings absent at --base")
    diff.add_argument("--base", required=True, help="git revision to compare against")
    sub.add_parser("full", parents=[common], help="fail on any non-ignored finding in a fail-on=any target")
    fix = sub.add_parser("fix", parents=[common], help="upgrade vulnerable packages in the lockfiles in place")
    fix.add_argument(
        "--commit", action="store_true", help="commit each upgrade as neops-deps-bot (clean tree required)"
    )
    fix.add_argument("--dry-run", action="store_true", help="restore the lockfiles after reporting")
    return p


COMMANDS = {"check": cmd_check, "diff": cmd_diff, "full": cmd_full, "fix": cmd_fix}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    args.config = getattr(args, "config", config_mod.DEFAULT_PATH)
    out = Output(args)
    try:
        return COMMANDS[args.command](args, repo_root(), out)
    except ConfigError as e:
        message = f"config error: {e}"
    except (ToolError, GuardError) as e:
        message = f"tool error, not a finding: {e}"
    except Exception as e:
        message = f"tool error, not a finding: {type(e).__name__}: {e}"
    print(f"::error::dependency-audit {message}".replace("\n", "%0A"))
    out.summary(f"## Dependency audit: {message.splitlines()[0]}\n\n```\n{message}\n```\n")
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
