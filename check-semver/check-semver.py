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


def get_allow_backports() -> bool:
    """
    Check if backport mode is enabled via environment variable.

    Returns:
        True if ALLOW_BACKPORTS is set to 'true' (case-insensitive), False otherwise.
    """
    value = os.environ.get("ALLOW_BACKPORTS", "false").lower()
    return value == "true"


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
            parsed_version = version.parse(tag_name)
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
    # Check configuration
    allow_backports = get_allow_backports()

    logger.info("🔍 Starting semver order validation...")
    if allow_backports:
        logger.info("   Mode: branch-aware (backports allowed)")
    else:
        logger.info("   Mode: strict (no backports)")
    logger.info("")

    # Step 1: Configure git for GitHub Actions environment
    configure_git_safe_directory()

    # Step 2: Fetch all tags with timestamps (single git command, O(n))
    raw_tags = fetch_tags_with_timestamps()

    if not raw_tags:
        logger.info("ℹ️  No tags found in repository. Nothing to validate.")
        sys.exit(0)

    # Step 3: Parse and filter to valid semver tags (O(n))
    semver_tags = parse_semver_tags(raw_tags)

    if not semver_tags:
        logger.info("ℹ️  No valid semver tags found. Nothing to validate.")
        sys.exit(0)

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
        sys.exit(1)
    else:
        print_success_report(semver_tags, allow_backports)
        sys.exit(0)


if __name__ == "__main__":
    main()
