#!/usr/bin/env python3
"""
check-semver.py - Verify semantic versioning order in git repositories.

This script ensures that semantic version tags are in correct chronological order.

Modes:
  - STRICT (default): All tags must be in global semver order. No backports allowed.
  - BRANCH_AWARE: Tags are grouped by major.minor series. Backports across series
    are allowed (e.g., v1.0.1 after v1.1.0), but order within each series is enforced.

Configuration:
  Set ALLOW_BACKPORTS=true to enable branch-aware mode.

Algorithm complexity: O(n log n) where n is the number of tags.
"""

from dataclasses import dataclass
from datetime import datetime
from collections import defaultdict
from packaging import version
import json
import subprocess
import sys
import os
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)
SUMMARY_HEADER = "## Check SemVer"

def get_github_event() -> dict:
    """
    Load the GitHub event payload from GITHUB_EVENT_PATH when available.

    Returns:
        The event payload dict, or {} if not available / unreadable.
    """
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        logger.debug("GITHUB_EVENT_PATH is not set; skipping event payload read.")
        return {}

    try:
        with open(event_path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except OSError as exc:
        logger.warning("⚠️  Could not read GitHub event payload: %s", exc)
        return {}
    except json.JSONDecodeError as exc:
        logger.warning("⚠️  Could not parse GitHub event payload JSON: %s", exc)
        return {}


def is_deleted_ref_event(event: dict) -> bool:
    """
    Detect whether the current workflow run was triggered by a ref deletion.

    For push events, GitHub includes a top-level boolean field `deleted`.
    """
    deleted = event.get("deleted")
    return bool(deleted) if deleted is not None else False


def remote_tag_exists(tag_name: str, remote: str = "origin") -> bool:
    """
    Check whether a tag exists on the given git remote.

    Notes:
        - This is stricter than checking local refs because the checkout step may
          recreate local tag refs from the event SHA even if the tag was deleted
          on the remote before this script runs.
    """
    # If origin isn't configured (e.g. local runs), skip the remote check.
    remote_url = subprocess.run(
        ["git", "remote", "get-url", remote],
        capture_output=True,
        text=True,
    )
    if remote_url.returncode != 0:
        logger.warning(
            "⚠️  Remote '%s' is not configured; skipping remote tag existence check.",
            remote,
        )
        return True

    ref = f"refs/tags/{tag_name}"
    result = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", remote, ref],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error(
            "🚫 Failed to query remote tags on '%s': %s",
            remote,
            (result.stderr or "").strip() or "unknown error",
        )
        return False

    return bool((result.stdout or "").strip())


@dataclass
class TagInfo:
    """Structured representation of a git tag with semver metadata."""

    name: str
    timestamp: int
    version: version.Version

    @property
    def major_minor(self) -> str:
        """Returns the major.minor group identifier (e.g., '1.0' for v1.0.3)."""
        return f"{self.version.major}.{self.version.minor}"

    @property
    def formatted_date(self) -> str:
        """Returns human-readable date string."""
        return datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S")


def strip_v_prefix(tag_name: str) -> str:
    """
    Remove a leading 'v'/'V' from tags that use the common v-prefix pattern.

    The prefix is only stripped when followed by a digit to avoid mangling tags
    such as "version" or similar strings.
    """
    if (
        tag_name
        and tag_name[0].lower() == "v"
        and len(tag_name) > 1
        and tag_name[1].isdigit()
    ):
        return tag_name[1:]
    return tag_name


def parse_tag_version(tag_name: str) -> version.Version:
    """
    Parse a tag name into a packaging.version.Version instance, allowing v-prefixes.

    Raises:
        version.InvalidVersion: if the tag is not a valid semantic version.
    """
    normalized = strip_v_prefix(tag_name)
    return version.Version(normalized)


def append_summary(lines: list[str]) -> None:
    """
    Append lines to the GitHub step summary when available.

    The function is intentionally quiet if the summary file is not present to
    keep local runs uncluttered.
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        logger.debug("GITHUB_STEP_SUMMARY is not set; skipping summary write.")
        return

    with open(summary_path, "a", encoding="utf-8") as summary_file:
        summary_file.write("\n".join(lines))
        summary_file.write("\n")


@dataclass
class Violation:
    """Represents a semver ordering violation."""

    series: str  # "global" for strict mode, or "major.minor" for branch-aware mode
    earlier_tag: TagInfo
    later_tag: TagInfo

    def __str__(self) -> str:
        if self.series == "global":
            return (
                f"{self.later_tag.name} (tagged {self.later_tag.formatted_date}) "
                f"comes after {self.earlier_tag.name} (tagged {self.earlier_tag.formatted_date}) "
                f"but has a lower version number"
            )
        return (
            f"In series {self.series}.x: "
            f"{self.later_tag.name} (tagged {self.later_tag.formatted_date}) "
            f"comes after {self.earlier_tag.name} (tagged {self.earlier_tag.formatted_date}) "
            f"but has a lower version number"
        )


def configure_git_safe_directory() -> None:
    """Configure git safe.directory for GitHub Actions environment."""
    workspace = os.environ.get("GITHUB_WORKSPACE", "/github/workspace")
    logger.debug(f"Configuring safe.directory: {workspace}")

    result = subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", workspace],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error(f"🚫 Failed to configure git safe.directory: {result.stderr}")
        sys.exit(1)


def fetch_tags_with_timestamps() -> list[tuple[str, int]]:
    """
    Fetch all tags with their creation timestamps using a single git command.

    Returns:
        List of (tag_name, timestamp) tuples, sorted by timestamp (oldest first).

    Raises:
        SystemExit: If git command fails.
    """
    result = subprocess.run(
        [
            "git",
            "for-each-ref",
            "--sort=creatordate",
            "--format=%(refname:short)\t%(creatordate:unix)",
            "refs/tags",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error(f"🚫 Failed to fetch tags: {result.stderr}")
        sys.exit(1)

    tags = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) == 2:
            tag_name, timestamp_str = parts
            try:
                timestamp = int(timestamp_str)
                tags.append((tag_name, timestamp))
            except ValueError:
                logger.warning(f"⚠️  Could not parse timestamp for tag: {tag_name}")

    logger.info(f"📋 Found {len(tags)} total tags in repository")
    return tags


def parse_semver_tags(raw_tags: list[tuple[str, int]]) -> list[TagInfo]:
    """
    Parse raw tags into TagInfo objects, filtering out non-semver tags.

    Args:
        raw_tags: List of (tag_name, timestamp) tuples.

    Returns:
        List of TagInfo objects for valid semver tags.
    """
    semver_tags = []
    invalid_count = 0

    for tag_name, timestamp in raw_tags:
        try:
            parsed_version = parse_tag_version(tag_name)
            semver_tags.append(
                TagInfo(name=tag_name, timestamp=timestamp, version=parsed_version)
            )
        except version.InvalidVersion:
            invalid_count += 1
            logger.debug(f"Skipping non-semver tag: {tag_name}")

    if invalid_count > 0:
        logger.info(f"⚠️  Skipped {invalid_count} non-semver tags")

    logger.info(f"✓  Parsed {len(semver_tags)} valid semver tags")
    return semver_tags


def group_by_major_minor(tags: list[TagInfo]) -> dict[str, list[TagInfo]]:
    """
    Group tags by their major.minor version prefix.

    This enables branch-aware checking, allowing backports across different
    release series while enforcing order within each series.

    Args:
        tags: List of TagInfo objects (should already be sorted by timestamp).

    Returns:
        Dictionary mapping major.minor strings to lists of TagInfo objects.
    """
    groups: dict[str, list[TagInfo]] = defaultdict(list)

    for tag in tags:
        groups[tag.major_minor].append(tag)

    logger.info(
        f"📦 Grouped tags into {len(groups)} release series: {', '.join(sorted(groups.keys()))}"
    )
    return dict(groups)


def verify_series_order(series: str, tags: list[TagInfo]) -> list[Violation]:
    """
    Verify that tags within a series are in correct semver order.

    Tags are already sorted by timestamp (chronological order). This function
    checks that the semver order matches the chronological order.

    Args:
        series: The series identifier ("global" for strict mode, or "major.minor").
        tags: List of TagInfo objects in chronological order.

    Returns:
        List of Violation objects for any ordering issues found.
    """
    violations = []

    for i in range(len(tags) - 1):
        current = tags[i]
        next_tag = tags[i + 1]

        # If next tag has lower version but came later chronologically, it's a violation
        if next_tag.version < current.version:
            violations.append(
                Violation(series=series, earlier_tag=current, later_tag=next_tag)
            )

    return violations


def verify_global_order(tags: list[TagInfo]) -> list[Violation]:
    """
    Verify that all tags are in strict global semver order.

    This is the strict mode - no backports allowed. Any tag with a lower
    version number appearing after a higher version is a violation.

    Args:
        tags: List of TagInfo objects in chronological order.

    Returns:
        List of Violation objects for any ordering issues found.
    """
    return verify_series_order("global", tags)


def print_violation_report(violations: list[Violation], allow_backports: bool) -> None:
    """Print a detailed report of all violations with suggested fixes."""
    logger.error("")
    logger.error("=" * 70)
    logger.error("🚫 SEMVER ORDER VIOLATION DETECTED")
    logger.error("=" * 70)

    # Group violations by series for cleaner output
    by_series: dict[str, list[Violation]] = defaultdict(list)
    for v in violations:
        by_series[v.series].append(v)

    for series, series_violations in sorted(by_series.items()):
        logger.error("")
        if series == "global":
            logger.error("📌 Global Order Violations (strict mode)")
        else:
            logger.error(f"📌 Series: {series}.x")
        logger.error("-" * 50)

        for violation in series_violations:
            logger.error("")
            logger.error("  Problem:")
            logger.error(
                f"    • {violation.later_tag.name} was tagged on {violation.later_tag.formatted_date}"
            )
            logger.error(
                f"    • {violation.earlier_tag.name} was tagged on {violation.earlier_tag.formatted_date}"
            )
            logger.error(
                f"    • But {violation.later_tag.name} < {violation.earlier_tag.name} semantically"
            )
            logger.error("")
            logger.error("  Suggested fixes:")
            logger.error("    1. Delete the incorrect tag:")
            logger.error(f"       git tag -d {violation.later_tag.name}")
            logger.error(f"       git push --delete origin {violation.later_tag.name}")
            logger.error("")
            logger.error(
                f"    2. Or, if {violation.later_tag.name} should come after {violation.earlier_tag.name},"
            )

            # Calculate suggested next version
            suggested = f"{violation.earlier_tag.version.major}.{violation.earlier_tag.version.minor}.{violation.earlier_tag.version.micro + 1}"
            logger.error(f"       consider re-tagging as {suggested} or higher")

    logger.error("")
    logger.error("=" * 70)
    if allow_backports:
        logger.error(
            f"Total: {len(violations)} violation(s) found across {len(by_series)} series"
        )
    else:
        logger.error(f"Total: {len(violations)} violation(s) found")
        logger.error("")
        logger.error("💡 Tip: If you use backported releases (e.g., hotfixes to older")
        logger.error(
            "   branches), set ALLOW_BACKPORTS=true to enable branch-aware mode."
        )
    logger.error("=" * 70)


def print_success_report(tags: list[TagInfo], allow_backports: bool) -> None:
    """Print a summary of successfully validated tags."""
    logger.info("")
    logger.info("=" * 70)
    logger.info("✅ SEMVER ORDER VALIDATION PASSED")
    logger.info("=" * 70)

    if allow_backports:
        # Group by series for detailed output
        groups: dict[str, list[TagInfo]] = defaultdict(list)
        for tag in tags:
            groups[tag.major_minor].append(tag)

        logger.info(f"  Validated {len(tags)} tags across {len(groups)} release series")
        logger.info("  Mode: branch-aware (backports allowed)")
        logger.info("")

        for series in sorted(groups.keys()):
            series_tags = groups[series]
            if series_tags:
                first = series_tags[0].name
                last = series_tags[-1].name
                count = len(series_tags)
                if count == 1:
                    logger.info(f"  • {series}.x: {first}")
                else:
                    logger.info(f"  • {series}.x: {first} → {last} ({count} tags)")
    else:
        logger.info(f"  Validated {len(tags)} tags in strict global order")
        logger.info("  Mode: strict (no backports)")
        logger.info("")
        if tags:
            first = tags[0].name
            last = tags[-1].name
            logger.info(f"  • Range: {first} → {last}")

    logger.info("=" * 70)


def main() -> None:
    """Main entry point for semver validation."""
    ref_type = os.environ.get("GITHUB_REF_TYPE", "").lower()
    ref_name = os.environ.get("GITHUB_REF_NAME", "")
    allow_backports = os.environ.get("ALLOW_BACKPORTS", "false").lower() == "true"
    fail_on_deleted = os.environ.get("FAIL_ON_DELETED", "true").lower() == "true"
    event = get_github_event()
    deleted_event = is_deleted_ref_event(event)

    if ref_type != "tag":
        logger.error(
            "🚫 Semver validation requires a tag ref, but '%s' was provided (name: '%s').",
            ref_type or "unknown",
            ref_name or "unknown",
        )
        append_summary(
            [
                SUMMARY_HEADER,
                "🚫 Failed: Ref is not a tag",
                f"- Ref type: {ref_type or 'unknown'}",
                f"- Ref name: {ref_name or 'unknown'}",
                "- This action only validates annotated or lightweight tags.",
                "",
            ]
        )
        sys.exit(1)

    if not ref_name:
        logger.error("GITHUB_REF_NAME is empty; cannot determine tag for validation.")
        append_summary(
            [
                SUMMARY_HEADER,
                "🚫 Failed: Missing tag name",
                "- Ref: not available",
                "- Error: GITHUB_REF_NAME was not provided.",
                "",
            ]
        )
        sys.exit(1)

    current_version: version.Version | None = None
    try:
        current_version = parse_tag_version(ref_name)
    except version.InvalidVersion as exc:
        # On deletion events, the deleted ref may not be relevant for integrity checks;
        # we still validate the repository tags and report order.
        if deleted_event:
            logger.warning(
                "⚠️  Deleted tag ref '%s' is not a valid semantic version: %s",
                ref_name,
                str(exc),
            )
        else:
            logger.error(
                "🚫 Tag '%s' is not a valid semantic version: %s", ref_name, str(exc)
            )
            append_summary(
                [
                    SUMMARY_HEADER,
                    "🚫 Failed: Tag is not a valid semantic version",
                    f"- Ref: tag `{ref_name}`",
                    f"- Error: {exc}",
                    "",
                ]
            )
            sys.exit(1)

    logger.info(
        "🔍 Starting semver order validation for tag '%s' (version '%s')...",
        ref_name,
        current_version if current_version is not None else "unknown",
    )
    if allow_backports:
        logger.info("   Mode: branch-aware (backports allowed)")
    else:
        logger.info("   Mode: strict (no backports)")
    if deleted_event:
        logger.info("   Event: ref deletion detected (github.event.deleted=true)")
        logger.info(
            "   Policy: fail_on_deleted=%s",
            "true" if fail_on_deleted else "false",
        )
    logger.info("")

    # Step 1: Configure git for GitHub Actions environment
    configure_git_safe_directory()

    # Step 2: Fetch all tags with timestamps (single git command, O(n))
    raw_tags = fetch_tags_with_timestamps()

    # Ensure the triggering tag exists on the remote at runtime (non-delete events).
    if not deleted_event and not remote_tag_exists(ref_name):
        logger.error(
            "🚫 Tag '%s' is not present on the remote. It may have been deleted after the workflow was triggered.",
            ref_name,
        )
        append_summary(
            [
                SUMMARY_HEADER,
                "🚫 Failed: Trigger tag no longer exists on remote",
                f"- Ref: tag `{ref_name}`",
                "- Reason: tag was not found on remote (`git ls-remote --tags --refs origin`)",
                "",
            ]
        )
        sys.exit(1)

    # Ensure the triggering tag still exists (it may have been deleted after the run started).
    tag_names = {name for name, _ts in raw_tags}
    if ref_name not in tag_names:
        if deleted_event:
            logger.info(
                "ℹ️  Tag '%s' is not present in repository tags (expected for deletion events).",
                ref_name,
            )
        else:
            logger.error(
                "🚫 Tag '%s' is not present in repository tags. It may have been deleted after the workflow started.",
                ref_name,
            )
            append_summary(
                [
                    SUMMARY_HEADER,
                    "🚫 Failed: Trigger tag no longer exists",
                    f"- Ref: tag `{ref_name}`",
                    "- Reason: tag was not found in `refs/tags` during validation",
                    "",
                ]
            )
            sys.exit(1)

    if not raw_tags:
        logger.info("ℹ️  No tags found in repository. Nothing to validate.")
        append_summary(
            [
                SUMMARY_HEADER,
                "ℹ️ Info: No tags found",
                f"- Ref: tag `{ref_name}` (version `{current_version}`)",
                "- No tags were available to validate.",
                "",
            ]
        )
        sys.exit(0)

    # Step 3: Parse and filter to valid semver tags (O(n))
    semver_tags = parse_semver_tags(raw_tags)

    if not semver_tags:
        logger.info("ℹ️  No valid semver tags found. Nothing to validate.")
        append_summary(
            [
                SUMMARY_HEADER,
                "🚫 Failed: No valid semver tags found",
                f"- Ref: tag `{ref_name}` (version `{current_version}`)",
                "- Unable to validate ordering because no semver tags were detected.",
                "",
            ]
        )
        sys.exit(1)

    # Step 4: Verify order based on mode
    all_violations: list[Violation] = []

    if allow_backports:
        # Branch-aware mode: group by major.minor and check within each series
        groups = group_by_major_minor(semver_tags)
        for series, tags in groups.items():
            violations = verify_series_order(series, tags)
            all_violations.extend(violations)
    else:
        # Strict mode: verify global order across all tags
        logger.info(f"🔒 Checking strict global order for {len(semver_tags)} tags")
        all_violations = verify_global_order(semver_tags)

    # Step 5: Report results
    if all_violations:
        print_violation_report(all_violations, allow_backports)
        first_violation = all_violations[0]
        append_summary(
            [
                SUMMARY_HEADER,
                "🚫 Failed: SemVer ordering violation",
                f"- Ref: tag `{ref_name}` (version `{current_version if current_version is not None else 'unknown'}`)",
                f"- Mode: {'branch-aware (backports allowed)' if allow_backports else 'strict (no backports)'}",
                f"- Violations detected: {len(all_violations)}",
                f"- Example: {first_violation.later_tag.name} after {first_violation.earlier_tag.name} ({first_violation.series})",
                "",
            ]
        )
        sys.exit(1)
    else:
        print_success_report(semver_tags, allow_backports)

        if deleted_event:
            if fail_on_deleted:
                logger.error("")
                logger.error("🚫 Failing due to tag deletion event (fail_on_deleted=true).")
                logger.error("✅ SemVer order is intact.")
                append_summary(
                    [
                        SUMMARY_HEADER,
                        "🚫 Failed: Tag deletion event",
                        f"- Ref: tag `{ref_name}` (version `{current_version if current_version is not None else 'unknown'}`)",
                        f"- Mode: {'branch-aware (backports allowed)' if allow_backports else 'strict (no backports)'}",
                        f"- SemVer order: intact (validated {len(semver_tags)} tags)",
                        f"- Range checked: {semver_tags[0].name} → {semver_tags[-1].name}",
                        "- Reason: github.event.deleted=true and fail_on_deleted=true",
                        "",
                    ]
                )
                sys.exit(1)

            logger.info("")
            logger.info("✅ Tag deletion event ignored (fail_on_deleted=false).")
            logger.info("✅ SemVer order is intact.")
            append_summary(
                [
                    SUMMARY_HEADER,
                    "✅ Passed: Tag deletion event ignored",
                    f"- Ref: tag `{ref_name}` (version `{current_version if current_version is not None else 'unknown'}`)",
                    f"- Mode: {'branch-aware (backports allowed)' if allow_backports else 'strict (no backports)'}",
                    f"- SemVer order: intact (validated {len(semver_tags)} tags)",
                    f"- Range checked: {semver_tags[0].name} → {semver_tags[-1].name}",
                    "- Note: github.event.deleted=true but fail_on_deleted=false",
                    "",
                ]
            )
            sys.exit(0)

        append_summary(
            [
                SUMMARY_HEADER,
                "✅ Passed: SemVer ordering verified",
                f"- Ref: tag `{ref_name}` (version `{current_version if current_version is not None else 'unknown'}`)",
                f"- Mode: {'branch-aware (backports allowed)' if allow_backports else 'strict (no backports)'}",
                f"- Tags validated: {len(semver_tags)}",
                f"- Range checked: {semver_tags[0].name} → {semver_tags[-1].name}",
                "",
            ]
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
