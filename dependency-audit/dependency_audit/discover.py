"""Tracked lockfiles and their coverage by targets and excludes."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

from .config import BUILTIN_EXCLUDES, LOCKFILES, Config
from .model import ConfigError


def tracked_lockfiles(root: Path) -> list[str]:
    """Repo-relative paths of tracked `uv.lock` and `package-lock.json` files."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", *(f"*{name}" for name in LOCKFILES.values())],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    names = set(LOCKFILES.values())
    return sorted(p for p in out.split("\0") if p and PurePosixPath(p).name in names)


def glob_regex(glob: str) -> re.Pattern[str]:
    """`**` matches across directories, `*` and `?` stay within one path segment."""
    parts: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**", i):
            parts.append(".*")
            i += 2
        elif glob[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(parts) + r"\Z")


def uncovered(config: Config, lockfiles: list[str]) -> list[str]:
    """Lockfiles that are neither a target's lockfile nor matched by an exclude glob."""
    targets = {t.lockfile for t in config.targets}
    globs = [glob_regex(g) for g in (*BUILTIN_EXCLUDES, *(e.glob for e in config.excludes))]
    return [p for p in lockfiles if p not in targets and not any(g.match(p) for g in globs)]


def check(config: Config, root: Path) -> None:
    """Raise ConfigError when a target has no tracked lockfile or a tracked lockfile is not covered."""
    lockfiles = tracked_lockfiles(root)
    problems = [
        f"target `{t.name}`: {t.lockfile} is not tracked" for t in config.targets if t.lockfile not in lockfiles
    ]
    problems += [f"{p} is neither a target nor excluded" for p in uncovered(config, lockfiles)]
    if problems:
        raise ConfigError("check failed:\n  " + "\n  ".join(problems))
