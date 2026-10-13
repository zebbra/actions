"""Findings, ignore matching, the base/head diff and the fix-target rule."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass


class ConfigError(Exception):
    """Invalid config or uncovered lockfile. Exit code 2."""


class ToolError(Exception):
    """An audit tool failed (export, network, invalid JSON). Exit code 2, never a finding."""


def normalize(name: str, ecosystem: str) -> str:
    """PEP 503 name for uv; npm names are compared as-is."""
    if ecosystem == "uv":
        return re.sub(r"[-_.]+", "-", name).lower()
    return name


@dataclass(frozen=True)
class Finding:
    """One advisory on one installed version of a package in one target."""

    target: str
    ecosystem: str
    package: str
    version: str
    advisory: str  # the id the tool reports
    ids: frozenset[str]  # advisory id plus all aliases
    fix_versions: tuple[str, ...]

    @property
    def display_id(self) -> str:
        """GHSA id when the advisory has one (it links for both ecosystems), else the tool's id."""
        if self.advisory.startswith("GHSA-"):
            return self.advisory
        ghsa = sorted(i for i in self.ids if i.startswith("GHSA-"))
        return ghsa[0] if ghsa else self.advisory

    def same_advisory(self, other: Finding) -> bool:
        """Same target and package, and at least one shared id (version and id scheme may differ)."""
        return self.target == other.target and self.package == other.package and bool(self.ids & other.ids)


@dataclass(frozen=True)
class Ignore:
    id: str
    reason: str
    package: str | None = None
    issue: str | None = None

    @property
    def key(self) -> tuple[str, str | None]:
        return (self.id, self.package)

    def matches(self, finding: Finding) -> bool:
        if self.id not in finding.ids:
            return False
        return self.package is None or normalize(self.package, finding.ecosystem) == finding.package


def split_ignored(findings: Iterable[Finding], ignores: Iterable[Ignore]) -> tuple[list[Finding], list[Finding]]:
    """Return (active, ignored)."""
    ignores = list(ignores)
    active: list[Finding] = []
    ignored: list[Finding] = []
    for f in findings:
        (ignored if any(i.matches(f) for i in ignores) else active).append(f)
    return active, ignored


def unused_ignores(ignores: Iterable[Ignore], findings: Iterable[Finding]) -> list[Ignore]:
    findings = list(findings)
    return [i for i in ignores if not any(i.matches(f) for f in findings)]


def common_ignores(base: Iterable[Ignore], head: Iterable[Ignore]) -> list[Ignore]:
    """Ignores present in both configs, matched on id and package; the head entry is kept."""
    base_keys = {i.key for i in base}
    return [i for i in head if i.key in base_keys]


def new_findings(head: Iterable[Finding], base: Iterable[Finding]) -> list[Finding]:
    """Head findings with no base finding of the same target and package sharing an id."""
    base = list(base)
    return [h for h in head if not any(h.same_advisory(b) for b in base)]


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(set(findings), key=lambda f: (f.target, f.package, version_key(f.version), f.display_id))


_VERSION = re.compile(
    r"^v?(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_n>\d*))?"
    r"(?:[-_.]?(?:post|rev|r)[-_.]?(?P<post>\d*))?"
    r"(?:[-_.]?dev[-_.]?(?P<dev>\d*))?"
    r"(?:\+.*)?$",
    re.IGNORECASE,
)
_PRE_PHASE = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}


def version_key(version: str) -> tuple:
    """Sort key for PEP 440-style versions; unparseable versions sort lowest."""
    m = _VERSION.match(version.strip())
    if not m:
        return ((), (-2,), -1, (1,))
    release = [int(x) for x in m["release"].split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    if m["pre"]:
        pre: tuple = (0, _PRE_PHASE[m["pre"].lower()], int(m["pre_n"] or 0))
    elif m["dev"] is not None and m["post"] is None:
        pre = (-1,)
    else:
        pre = (1,)
    post = int(m["post"] or 0) if m["post"] is not None else -1
    dev: tuple = (0, int(m["dev"] or 0)) if m["dev"] is not None else (1,)
    return (tuple(release), pre, post, dev)


def fix_target(findings: Iterable[Finding]) -> str | None:
    """Max over the package's advisories of min(fix_versions > installed); None when no advisory has one."""
    picks: list[str] = []
    for f in findings:
        installed = version_key(f.version)
        above = [v for v in f.fix_versions if version_key(v) > installed]
        if above:
            picks.append(min(above, key=version_key))
    return max(picks, key=version_key) if picks else None
