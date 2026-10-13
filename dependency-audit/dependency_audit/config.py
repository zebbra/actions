"""Parse and validate `.github/dependency-audit.toml`."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .model import ConfigError, Ignore

DEFAULT_PATH = ".github/dependency-audit.toml"
LOCKFILES = {"uv": "uv.lock", "npm": "package-lock.json"}
MANIFESTS = {"uv": "pyproject.toml", "npm": "package.json"}
BUILTIN_EXCLUDES = (".make_scripts/**",)

_TOP_KEYS = {"target", "exclude", "ignore"}
_TARGET_KEYS = {"name", "ecosystem", "path", "scope", "groups", "fail-on"}
_EXCLUDE_KEYS = {"glob", "reason"}
_IGNORE_KEYS = {"id", "package", "reason", "issue"}


@dataclass(frozen=True)
class Target:
    name: str
    ecosystem: str  # uv | npm
    path: str  # repo-relative directory, "." for the root
    scope: str = "runtime"  # runtime | all
    groups: tuple[str, ...] = ()
    fail_on: str = "any"  # any | none

    @property
    def lockfile(self) -> str:
        return str(PurePosixPath(self.path) / LOCKFILES[self.ecosystem])

    @property
    def manifest(self) -> str:
        return str(PurePosixPath(self.path) / MANIFESTS[self.ecosystem])

    @property
    def label(self) -> str:
        return f"{self.name} ({self.ecosystem})"


@dataclass(frozen=True)
class Exclude:
    glob: str
    reason: str


@dataclass(frozen=True)
class Config:
    targets: tuple[Target, ...]
    excludes: tuple[Exclude, ...] = ()
    ignores: tuple[Ignore, ...] = field(default_factory=tuple)


def load(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return parse(data, str(path))


def parse(data: dict, source: str = DEFAULT_PATH) -> Config:
    _keys(data, _TOP_KEYS, source)
    targets = tuple(_target(t, f"{source} [[target]] #{n}") for n, t in enumerate(_tables(data, "target", source), 1))
    if not targets:
        raise ConfigError(f"{source}: at least one [[target]] is required")
    names = [t.name for t in targets]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ConfigError(f"{source}: duplicate target name(s): {', '.join(duplicates)}")
    excludes = tuple(
        Exclude(**_strings(e, _EXCLUDE_KEYS, {"glob", "reason"}, f"{source} [[exclude]] #{n}"))
        for n, e in enumerate(_tables(data, "exclude", source), 1)
    )
    ignores = tuple(
        Ignore(**_strings(i, _IGNORE_KEYS, {"id", "reason"}, f"{source} [[ignore]] #{n}"))
        for n, i in enumerate(_tables(data, "ignore", source), 1)
    )
    return Config(targets=targets, excludes=excludes, ignores=ignores)


def _tables(data: dict, key: str, source: str) -> list[dict]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise ConfigError(f"{source}: `{key}` must be an array of tables ([[{key}]])")
    return value


def _keys(table: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(unknown)}")


def _strings(table: dict, allowed: set[str], required: set[str], where: str) -> dict:
    _keys(table, allowed, where)
    for key in sorted(required):
        if not isinstance(table.get(key), str) or not table[key].strip():
            raise ConfigError(f"{where}: `{key}` is required and must be a non-empty string")
    for key, value in table.items():
        if not isinstance(value, str):
            raise ConfigError(f"{where}: `{key}` must be a string")
    return dict(table)


def _target(t: dict, where: str) -> Target:
    _keys(t, _TARGET_KEYS, where)
    for key in ("name", "ecosystem", "path"):
        if not isinstance(t.get(key), str) or not t[key].strip():
            raise ConfigError(f"{where}: `{key}` is required and must be a non-empty string")
    _choice(t, "ecosystem", ("uv", "npm"), where)
    scope = _choice(t, "scope", ("runtime", "all"), where, default="runtime")
    fail_on = _choice(t, "fail-on", ("any", "none"), where, default="any")
    groups = t.get("groups", [])
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
        raise ConfigError(f"{where}: `groups` must be an array of strings")
    if groups and t["ecosystem"] != "uv":
        raise ConfigError(f"{where}: `groups` applies to uv targets only")
    path = PurePosixPath(t["path"])
    if path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"{where}: `path` must be relative to the repository root")
    return Target(
        name=t["name"],
        ecosystem=t["ecosystem"],
        path=str(path),
        scope=scope,
        groups=tuple(groups),
        fail_on=fail_on,
    )


def _choice(t: dict, key: str, allowed: tuple[str, ...], where: str, default: str | None = None) -> str:
    value = t.get(key, default)
    if value not in allowed:
        raise ConfigError(f"{where}: `{key}` must be one of {', '.join(allowed)} (got {value!r})")
    return value
