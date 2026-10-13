"""Markdown for the job summary, the bot PR body and the triage issue body.

Only ids, advisory links, package names, versions and repo-owned config text are rendered; advisory titles and
descriptions from the tools never are. Every rendered string passes through `text` or `code`, so nothing mentions
people or cross-links issues.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass

from . import __version__
from .model import Finding, Ignore, version_key

MAX_BODY = 65536
PR_MARKER = "<!-- dependency-audit:pr -->"
ISSUE_MARKER = "<!-- dependency-audit:triage -->"
TRIAGE_PLACEHOLDER = "{triage_issue}"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


_TEXT_ESCAPES = str.maketrans(
    {"&": "&amp;", "@": "&#64;", "#": "&#35;", "<": "&lt;", ">": "&gt;", "|": "\\|", "\n": " "}
)


def text(value: str) -> str:
    """Plain table text: `@` and `#` become entities, pipes and angle brackets are escaped."""
    return value.translate(_TEXT_ESCAPES)


def code(value: str) -> str:
    return "`" + value.replace("`", "'").replace("|", "\\|").replace("\n", " ") + "`"


def table_row(cells: Iterable[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def advisory(id_: str) -> str:
    if not _SAFE_ID.match(id_):
        return text(id_)
    if id_.startswith("GHSA-"):
        return f"[{id_}](https://github.com/advisories/{id_})"
    if id_.startswith(("PYSEC-", "CVE-")):
        return f"[{id_}](https://osv.dev/vulnerability/{id_})"
    return id_


def versions(values: Iterable[str]) -> str:
    return ", ".join(text(v) for v in sorted(set(values), key=version_key))


def run_url() -> str | None:
    server, repo, run = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else None


def cap(body: str, limit: int = MAX_BODY) -> str:
    """Truncate at a line break so the body fits GitHub's limit, with a note pointing at the run."""
    if len(body) <= limit:
        return body
    url = run_url()
    note = (
        f"\n\n_Truncated at {limit} characters; full report: {url}_\n"
        if url
        else (f"\n\n_Truncated at {limit} characters; the full report is in the job summary._\n")
    )
    cut = body[: limit - len(note)]
    cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
    return cut + note


@dataclass(frozen=True)
class Row:
    """One package in one target in a fix report."""

    target: str  # target label, e.g. "backend (uv)"
    package: str
    from_versions: tuple[str, ...]
    to_versions: tuple[str, ...]
    ids: tuple[str, ...]
    scope: str  # runtime | dev
    note: str = ""


def findings_table(findings: Iterable[Finding], labels: dict[str, str], extra: dict[Finding, str] | None = None) -> str:
    rows = sorted(findings, key=lambda f: (f.target, f.package, version_key(f.version), f.display_id))
    header = "| Target | Package | Installed | Advisory | Fixed in |" + (" Note |" if extra is not None else "")
    lines = [header, "|---|---|---|---|---|" + ("---|" if extra is not None else "")]
    for f in rows:
        cells = [text(labels.get(f.target, f.target)), code(f.package), text(f.version), advisory(f.display_id)]
        cells.append(versions(f.fix_versions) or "none")
        if extra is not None:
            cells.append(text(extra.get(f, "")))
        lines.append(table_row(cells))
    return "\n".join(lines)


def ignores_table(ignores: Iterable[Ignore]) -> str:
    lines = ["| Advisory | Package | Reason | Issue |", "|---|---|---|---|"]
    for i in ignores:
        package = code(i.package) if i.package else "any"
        lines.append(f"| {advisory(i.id)} | {package} | {text(i.reason)} | {code(i.issue) if i.issue else ''} |")
    return "\n".join(lines)


def rows_table(rows: Iterable[Row]) -> str:
    lines = ["| Target | Package | From → To | Advisories | Scope |", "|---|---|---|---|---|"]
    for r in rows:
        ids = ", ".join(advisory(i) for i in r.ids)
        lines.append(
            table_row(
                [
                    text(r.target),
                    code(r.package),
                    f"{versions(r.from_versions)} → {versions(r.to_versions)}",
                    ids,
                    r.scope,
                ]
            )
        )
    return "\n".join(lines)


def section(title: str, body: str) -> str:
    return f"### {title}\n\n{body}\n"


def skipped_summary(base: str) -> str:
    return (
        "## Dependency audit: no dependency change\n\n"
        f"No target lockfile and no config changed since {code(base)}; no advisory database was queried.\n"
    )


def diff_summary(
    base: str,
    labels: dict[str, str],
    new: list[Finding],
    pre_existing: list[Finding],
    ignored: list[Finding],
) -> str:
    verdict = f"{len(new)} new finding(s)" if new else "no new findings"
    parts = [f"## Dependency audit (diff against {code(base)}): {verdict}\n"]
    if new:
        parts.append(section("New (fails the check)", findings_table(new, labels)))
    if pre_existing:
        parts.append(
            section(
                "Pre-existing (warning)",
                "Present at the base as well; the nightly run proposes fixes and lists the rest in the open "
                "`dependency-audit` issue.\n\n" + findings_table(pre_existing, labels),
            )
        )
    if ignored:
        parts.append(section("Ignored", findings_table(ignored, labels)))
    return "\n".join(parts)


def full_summary(
    labels: dict[str, str],
    failing: list[Finding],
    reported: list[Finding],
    ignored: list[Finding],
    unused: list[Ignore],
) -> str:
    verdict = f"{len(failing)} finding(s) in gating targets" if failing else "no findings in gating targets"
    parts = [f"## Dependency audit (full): {verdict}\n"]
    if failing:
        parts.append(section('Failing (`fail-on = "any"`)', findings_table(failing, labels)))
    if reported:
        parts.append(section('Reported only (`fail-on = "none"`)', findings_table(reported, labels)))
    if ignored:
        parts.append(section("Ignored", findings_table(ignored, labels)))
    if unused:
        parts.append(section("Unused ignores (warning)", ignores_table(unused)))
    return "\n".join(parts)


def pr_body(rows: list[Row], has_triage: bool) -> str:
    """Bot PR body; the action replaces TRIAGE_PLACEHOLDER with the triage issue reference."""
    if not rows:
        return ""
    parts = [
        PR_MARKER,
        "Lockfile-only security refresh by the nightly dependency audit. Upgrades stay within manifest constraints.",
        "",
        rows_table(rows),
        "",
    ]
    if has_triage:
        parts.append(f"Not fixed here: see {TRIAGE_PLACEHOLDER} (blocked / no fix / stale ignores).")
    parts.append(
        f"Generated by zebbra/actions dependency-audit {__version__}; rerun: Actions → Dependency audit → Run workflow."
    )
    return cap("\n".join(parts) + "\n")


def issue_body(
    labels: dict[str, str],
    *,
    dry_run: bool,
    fixable: list[Row],
    blocked: list[Finding],
    blockers: dict[Finding, str],
    no_fix: list[Finding],
    now_fixable: list[Ignore],
    unused: list[Ignore],
    report_only: set[str],
    errors: list[tuple[str, str]],
) -> str:
    """Triage issue body, or "" when there is nothing to triage."""
    parts: list[str] = []
    if errors:
        lines = "\n".join(f"- {text(labels.get(t, t))}: {code(message)}" for t, message in errors)
        parts.append(
            section(
                "Tool errors", "The audit could not complete for these targets (tool error, not a finding).\n\n" + lines
            )
        )
    if dry_run and fixable:
        parts.append(
            section(
                "Fixable (bot not configured)",
                "Bot not configured: fixable findings are listed here and no PR is opened.\n\n" + rows_table(fixable),
            )
        )
    if blocked:
        parts.append(
            section("Blocked (a fix exists, constraints prevent it)", findings_table(blocked, labels, blockers))
        )
    if no_fix:
        parts.append(section("No fix available", findings_table(no_fix, labels)))
    if now_fixable:
        parts.append(section("Ignored, now fixable (remove the ignore by hand)", ignores_table(now_fixable)))
    if unused:
        parts.append(section("Ignored, unused (matches nothing)", ignores_table(unused)))
    if not parts:
        return ""
    gating = sorted(labels[t] for t in labels if t not in report_only)
    reporting = sorted(labels[t] for t in report_only)
    header = [
        ISSUE_MARKER,
        "Findings the nightly dependency audit cannot fix. "
        "This issue is updated in place on every run and closed when empty.",
        "",
        f"Gating targets: {', '.join(text(t) for t in gating) or 'none'}. "
        f"Report-only targets: {', '.join(text(t) for t in reporting) or 'none'}.",
        "",
    ]
    footer = [f"Generated by zebbra/actions dependency-audit {__version__}."]
    return cap("\n".join(header) + "\n" + "\n".join(parts) + "\n" + "\n".join(footer) + "\n")
