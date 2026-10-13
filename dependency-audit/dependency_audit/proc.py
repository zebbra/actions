"""Subprocess runner, retry policy and the lockfile-only commit."""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from .model import ToolError

RETRY_DELAYS = (2, 8, 30)
BOT_NAME = "neops-deps-bot"
BOT_EMAIL = "neops-deps-bot@users.noreply.github.com"

T = TypeVar("T")


class GuardError(Exception):
    """A bot change would touch more than lockfiles. Exit code 2, nothing is committed."""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    log(f"$ {shlex.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def tail(text: str, lines: int = 5) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def with_retry(fn: Callable[[], T], what: str) -> T:
    """Call fn, retrying a ToolError after each delay in RETRY_DELAYS; the last error propagates."""
    for delay in (*RETRY_DELAYS, None):
        try:
            return fn()
        except ToolError as e:
            if delay is None:
                raise
            log(f"{what}: {e} (retrying in {delay}s)")
            sleep(delay)
    raise AssertionError("unreachable")


def changed_paths(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "-z", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(entry[3:] for entry in out.split("\0") if entry)


def require_clean(root: Path) -> None:
    dirty = changed_paths(root)
    if dirty:
        raise GuardError("--commit needs a clean working tree; changed: " + ", ".join(dirty))


def commit_lockfile(root: Path, lockfile: str, message: str) -> None:
    """Commit exactly `lockfile` as the bot; any other changed path raises GuardError."""
    changed = changed_paths(root)
    others = [p for p in changed if p != lockfile]
    if others:
        raise GuardError(f"refusing to commit: {', '.join(others)} changed besides {lockfile}")
    if not changed:
        return
    git = ["git", "-C", str(root), "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}"]
    for step in (["add", "--", lockfile], ["commit", "-q", "-m", message]):
        p = run(git + step)
        if p.returncode != 0:
            raise ToolError(f"git {step[0]} failed: {tail(p.stderr)}")
