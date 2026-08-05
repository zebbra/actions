#!/usr/bin/env python3
"""
check-semver.py - Verify semantic versioning order in git repositories.

This script ensures that semantic version tags respect the git commit graph:
if commit A is an ancestor of commit B, then version(tag_A) < version(tag_B).

Tags on unrelated branches (no ancestor/descendant relationship) are never
compared, which naturally supports multi-branch workflows without configuration.

Validation:
  - Ancestor check: all semver tags reachable from the current tag's commit
    must have a strictly lower version.
  - Descendant check: all semver tags whose commits descend from the current
    tag's commit must have a strictly higher version.

Algorithm complexity: O(n) where n is the number of tags (two git traversals).
"""

from dataclasses import dataclass
from datetime import datetime
import semver
import json
import subprocess
import sys
import os
import logging
import time

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
    version: semver.Version

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


def parse_tag_version(tag_name: str) -> semver.Version:
    """
    Parse a tag name into a semver.Version instance (SemVer 2.0.0), allowing v-prefixes.

    Raises:
        ValueError: if the tag is not a valid semantic version.
    """
    normalized = strip_v_prefix(tag_name)
    return semver.Version.parse(normalized)


def release_tags(semver_tags: dict[str, "TagInfo"]) -> dict[str, "TagInfo"]:
    """
    Customer-facing stable releases only: exclude prereleases and build metadata.

    Ordering integrity is enforced against these tags alone; prerelease (`-beta.N`,
    `-rc`, ...) and build-metadata (`+...`) tags are ignored for ordering.
    """
    return {
        name: t
        for name, t in semver_tags.items()
        if not t.version.prerelease and not t.version.build
    }


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


def append_github_outputs(outputs: dict[str, str]) -> None:
    """
    Append key/value pairs to the GitHub step output file when available.

    The function is intentionally quiet if GITHUB_OUTPUT is not present to keep
    local runs uncluttered.
    """
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        logger.debug("GITHUB_OUTPUT is not set; skipping output write.")
        return

    with open(output_path, "a", encoding="utf-8") as output_file:
        for key, value in outputs.items():
            output_file.write(f"{key}={value}\n")

    logger.info(
        "📤 Exported outputs: %s",
        ", ".join(f"{key}={value}" for key, value in outputs.items()),
    )


def derive_version_outputs(
    ref_name: str,
    current_version: semver.Version | None,
) -> dict[str, str]:
    """Build output values for the currently processed tag."""
    version_without_v = strip_v_prefix(ref_name)

    if current_version is not None:
        major = str(current_version.major)
        minor = str(current_version.minor)
        major_minor = f"{major}.{minor}"
    else:
        # Fallback for edge cases where the deleted ref is not a semver tag.
        parts = version_without_v.split(".")
        major = parts[0] if len(parts) >= 1 else ""
        minor = parts[1] if len(parts) >= 2 else ""
        major_minor = ".".join(parts[:2]) if len(parts) >= 2 else version_without_v

    return {
        "version": version_without_v,
        "major": major,
        "minor": minor,
        "major_minor": major_minor,
    }


@dataclass
class Violation:
    """Represents a semver ordering violation in the commit graph."""

    kind: str  # "ancestor" or "descendant"
    current_tag: TagInfo
    other_tag: TagInfo

    def __str__(self) -> str:
        if self.kind == "ancestor":
            return (
                f"{self.other_tag.name} (tagged {self.other_tag.formatted_date}) "
                f"is an ancestor of {self.current_tag.name} but has version "
                f"{self.other_tag.version} >= {self.current_tag.version}"
            )
        return (
            f"{self.other_tag.name} (tagged {self.other_tag.formatted_date}) "
            f"descends from {self.current_tag.name} but has version "
            f"{self.other_tag.version} <= {self.current_tag.version}"
        )


def configure_git_safe_directory() -> None:
    """Configure git safe.directory for GitHub Actions environment."""
    workspace = os.environ.get("GITHUB_WORKSPACE", "/github/workspace")
    logger.debug("Configuring safe.directory: %s", workspace)

    result = subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", workspace],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error("🚫 Failed to configure git safe.directory: %s", result.stderr)
        sys.exit(1)


def get_tag_commit_sha(tag_name: str) -> str:
    """
    Resolve a tag to the commit SHA it ultimately points to.

    Handles both lightweight and annotated tags by dereferencing to the commit.

    Raises:
        SystemExit: If git command fails.
    """
    result = subprocess.run(
        ["git", "rev-list", "-n", "1", tag_name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error(
            "🚫 Failed to resolve tag '%s' to commit: %s",
            tag_name,
            (result.stderr or "").strip(),
        )
        sys.exit(1)

    sha = result.stdout.strip()
    logger.debug("Resolved tag '%s' to commit %s", tag_name, sha[:12])
    return sha


def fetch_all_tags_with_metadata() -> list[tuple[str, int]]:
    """
    Fetch all tags with their creation timestamps using a single git command.

    Returns:
        List of (tag_name, timestamp) tuples.

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
        logger.error("🚫 Failed to fetch tags: %s", result.stderr)
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
                logger.warning("⚠️  Could not parse timestamp for tag: %s", tag_name)

    logger.info("📋 Found %d total tags in repository", len(tags))
    return tags


def parse_semver_tags(raw_tags: list[tuple[str, int]]) -> dict[str, TagInfo]:
    """
    Parse raw tags into TagInfo objects, filtering out non-semver tags.

    Args:
        raw_tags: List of (tag_name, timestamp) tuples.

    Returns:
        Dictionary mapping tag name to TagInfo for valid semver tags.
    """
    semver_tags: dict[str, TagInfo] = {}
    invalid_count = 0

    for tag_name, timestamp in raw_tags:
        try:
            parsed_version = parse_tag_version(tag_name)
            semver_tags[tag_name] = TagInfo(
                name=tag_name, timestamp=timestamp, version=parsed_version
            )
        except ValueError:
            invalid_count += 1
            logger.debug("Skipping non-semver tag: %s", tag_name)

    if invalid_count > 0:
        logger.info("⚠️  Skipped %d non-semver tags", invalid_count)

    logger.info("✓  Parsed %d valid semver tags", len(semver_tags))
    return semver_tags


def get_ancestor_tag_names(commit_sha: str) -> set[str]:
    """
    Get all tag names reachable from the given commit (ancestors and self).

    Uses `git tag --merged <sha>` which returns tags whose tagged commits
    are ancestors of (or equal to) the given commit.
    """
    t0 = time.monotonic()
    result = subprocess.run(
        ["git", "tag", "--merged", commit_sha],
        capture_output=True,
        text=True,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000

    if result.returncode != 0:
        logger.error(
            "🚫 Failed to get ancestor tags for %s: %s",
            commit_sha[:12],
            (result.stderr or "").strip(),
        )
        sys.exit(1)

    names = {
        line.strip()
        for line in result.stdout.strip().split("\n")
        if line.strip()
    }
    logger.info(
        "🔍 Found %d ancestor tags for %s (%.0fms)",
        len(names),
        commit_sha[:12],
        elapsed_ms,
    )
    return names


def get_descendant_tag_names(commit_sha: str) -> set[str]:
    """
    Get all tag names whose tagged commits are descendants of the given commit.

    Uses `git tag --contains <sha>` which returns tags where the given commit
    is reachable from the tag's commit.
    """
    t0 = time.monotonic()
    result = subprocess.run(
        ["git", "tag", "--contains", commit_sha],
        capture_output=True,
        text=True,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000

    if result.returncode != 0:
        logger.error(
            "🚫 Failed to get descendant tags for %s: %s",
            commit_sha[:12],
            (result.stderr or "").strip(),
        )
        sys.exit(1)

    names = {
        line.strip()
        for line in result.stdout.strip().split("\n")
        if line.strip()
    }
    logger.info(
        "🔍 Found %d descendant tags for %s (%.0fms)",
        len(names),
        commit_sha[:12],
        elapsed_ms,
    )
    return names


def verify_graph_order(
    current_tag: TagInfo,
    semver_tags: dict[str, TagInfo],
    ancestor_names: set[str],
    descendant_names: set[str],
) -> list[Violation]:
    """
    Verify semver ordering against the commit graph.

    - Ancestor tags (reachable from current) must have version < current.
    - Descendant tags (current reachable from them) must have version > current.

    Args:
        current_tag: The tag being validated.
        semver_tags: All semver tags keyed by name.
        ancestor_names: Tag names reachable from current tag's commit (excluding self).
        descendant_names: Tag names whose commits descend from current tag's commit (excluding self).

    Returns:
        List of Violation objects for any ordering issues found.
    """
    violations: list[Violation] = []

    for name in sorted(ancestor_names):
        tag = semver_tags.get(name)
        if tag is None:
            continue
        if tag.version >= current_tag.version:
            violations.append(
                Violation(kind="ancestor", current_tag=current_tag, other_tag=tag)
            )

    for name in sorted(descendant_names):
        tag = semver_tags.get(name)
        if tag is None:
            continue
        if tag.version <= current_tag.version:
            violations.append(
                Violation(kind="descendant", current_tag=current_tag, other_tag=tag)
            )

    return violations


def verify_repo_integrity(rel_tags: dict[str, TagInfo]) -> list[Violation]:
    """
    Fallback check used when the trigger tag is not a stable release (prerelease,
    build metadata, or non-SemVer). Validate every release tag against its release
    ancestors so genuinely out-of-order stable tags are still caught. Only ancestor
    sets are checked, so each disordered pair is reported once.
    """
    violations: list[Violation] = []
    for name, tag in rel_tags.items():
        sha = get_tag_commit_sha(name)
        ancestor_names = get_ancestor_tag_names(sha) - {name}
        violations.extend(
            verify_graph_order(tag, rel_tags, ancestor_names, set())
        )
    return violations


def print_violation_report(
    violations: list[Violation],
    current_tag: TagInfo,
) -> None:
    """Print a detailed report of all violations with suggested fixes."""
    ancestor_violations = [v for v in violations if v.kind == "ancestor"]
    descendant_violations = [v for v in violations if v.kind == "descendant"]

    logger.error("")
    logger.error("=" * 70)
    logger.error("🚫 SEMVER ORDER VIOLATION DETECTED")
    logger.error("=" * 70)

    if ancestor_violations:
        logger.error("")
        logger.error(
            "📌 Ancestor violations (tags reachable from %s with "
            "version >= %s):",
            current_tag.name,
            current_tag.version,
        )
        logger.error("-" * 50)
        for v in ancestor_violations:
            logger.error("")
            logger.error(
                "  • %s (tagged %s) is an ancestor but has version %s",
                v.other_tag.name,
                v.other_tag.formatted_date,
                v.other_tag.version,
            )
        logger.error("")
        logger.error("  Suggested fix: use a version higher than all ancestors,")
        highest = max(v.other_tag.version for v in ancestor_violations)
        logger.error(
            "  e.g. %s.%s.%s or higher.",
            highest.major,
            highest.minor,
            highest.patch + 1,
        )

    if descendant_violations:
        logger.error("")
        logger.error(
            "📌 Descendant violations (tags descending from %s with "
            "version <= %s):",
            current_tag.name,
            current_tag.version,
        )
        logger.error("-" * 50)
        for v in descendant_violations:
            logger.error("")
            logger.error(
                "  • %s (tagged %s) descends from this commit but has "
                "version %s",
                v.other_tag.name,
                v.other_tag.formatted_date,
                v.other_tag.version,
            )
        logger.error("")
        logger.error(
            "  Suggested fix: delete %s and re-tag with a version lower "
            "than all descendants,",
            current_tag.name,
        )
        lowest = min(v.other_tag.version for v in descendant_violations)
        logger.error(
            "  or delete the descendant tags. Lowest descendant: %s",
            lowest,
        )

    logger.error("")
    logger.error("=" * 70)
    logger.error(
        "Total: %d violation(s) found (%d ancestor, %d descendant)",
        len(violations),
        len(ancestor_violations),
        len(descendant_violations),
    )
    logger.error("=" * 70)


def print_success_report(
    current_tag: TagInfo,
    semver_tags: dict[str, TagInfo],
    ancestor_count: int,
    descendant_count: int,
) -> None:
    """Print a summary of successfully validated tags."""
    logger.info("")
    logger.info("=" * 70)
    logger.info("✅ SEMVER ORDER VALIDATION PASSED")
    logger.info("=" * 70)
    logger.info(
        "  Tag: %s (version %s)", current_tag.name, current_tag.version
    )
    logger.info("  Total semver tags in repo: %d", len(semver_tags))
    logger.info(
        "  Checked %d ancestor tags and %d descendant tags",
        ancestor_count,
        descendant_count,
    )
    logger.info("=" * 70)


def compute_tag_flags(
    semver_tags: dict[str, TagInfo],
    current_version: semver.Version | None,
) -> tuple[bool, bool]:
    """
    Determine whether the triggering tag is prerelease and latest stable.

    Latest is determined by the highest stable release (no prerelease, no build
    metadata) in the repository.
    """
    is_prerelease = bool(
        current_version is not None and current_version.prerelease
    )

    if current_version is None:
        logger.warning(
            "⚠️  Could not evaluate latest-stable status because current version is unavailable."
        )
        return is_prerelease, False

    stable_tags = list(release_tags(semver_tags).values())
    if not stable_tags:
        logger.warning(
            "⚠️  No stable semver tags found; marking is_latest=false for tag '%s'.",
            current_version,
        )
        return is_prerelease, False

    latest_stable = max(stable_tags, key=lambda tag: tag.version)
    is_latest = current_version == latest_stable.version and not is_prerelease
    logger.info(
        "🏷️  Tag classification: is_prerelease=%s, is_latest=%s (latest stable=%s)",
        "true" if is_prerelease else "false",
        "true" if is_latest else "false",
        latest_stable.name,
    )
    return is_prerelease, is_latest


def main() -> None:
    """Main entry point for semver validation."""
    ref_type = os.environ.get("GITHUB_REF_TYPE", "").lower()
    ref_name = os.environ.get("GITHUB_REF_NAME", "")
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

    current_version: semver.Version | None = None
    try:
        current_version = parse_tag_version(ref_name)
    except ValueError as exc:
        # A non-SemVer trigger tag (e.g. `nightly`, `latest`, or a malformed
        # version) is not customer-facing: we do not fail on it. We still validate
        # the repository's stable-release ordering (repo-integrity fallback) so a
        # real out-of-order release elsewhere is caught.
        logger.warning(
            "⚠️  Trigger tag '%s' is not a valid semantic version: %s — ignoring it "
            "for ordering; will verify stable-release integrity.",
            ref_name,
            str(exc),
        )

    logger.info(
        "🔍 Starting semver order validation for tag '%s' (version '%s')...",
        ref_name,
        current_version if current_version is not None else "unknown",
    )
    logger.info("   Mode: git-graph ancestry (commit DAG)")
    if deleted_event:
        logger.info("   Event: ref deletion detected (github.event.deleted=true)")
        logger.info(
            "   Policy: fail_on_deleted=%s",
            "true" if fail_on_deleted else "false",
        )
    logger.info("")

    configure_git_safe_directory()

    raw_tags = fetch_all_tags_with_metadata()

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

    semver_tags = parse_semver_tags(raw_tags)

    if not semver_tags:
        logger.info("ℹ️  No valid semver tags found. Nothing to validate.")
        append_summary(
            [
                SUMMARY_HEADER,
                "ℹ️ Info: No valid semver tags found",
                f"- Ref: tag `{ref_name}` (version `{current_version if current_version is not None else 'unknown'}`)",
                "- No semver tags were available to order.",
                "",
            ]
        )
        sys.exit(0)

    # --- Deletion events: skip graph validation ---
    # Removing a tag cannot introduce a DAG ordering violation;
    # just enforce the fail_on_deleted policy and emit outputs.
    if deleted_event:
        is_prerelease, is_latest = compute_tag_flags(semver_tags, current_version)
        step_outputs = derive_version_outputs(ref_name, current_version)
        step_outputs["is_prerelease"] = "true" if is_prerelease else "false"
        step_outputs["is_latest"] = "true" if is_latest else "false"

        if fail_on_deleted:
            logger.error("")
            logger.error("🚫 Failing due to tag deletion event (fail_on_deleted=true).")
            append_summary(
                [
                    SUMMARY_HEADER,
                    "🚫 Failed: Tag deletion event",
                    f"- Ref: tag `{ref_name}` (version `{current_version if current_version is not None else 'unknown'}`)",
                    "- Reason: github.event.deleted=true and fail_on_deleted=true",
                    "",
                ]
            )
            sys.exit(1)

        logger.info("")
        logger.info("✅ Tag deletion event ignored (fail_on_deleted=false).")
        append_github_outputs(step_outputs)
        append_summary(
            [
                SUMMARY_HEADER,
                "✅ Passed: Tag deletion event ignored",
                f"- Ref: tag `{ref_name}` (version `{current_version if current_version is not None else 'unknown'}`)",
                "- Note: github.event.deleted=true but fail_on_deleted=false",
                "",
            ]
        )
        sys.exit(0)

    # --- Ordering validation (stable releases only) ---
    # Prerelease and build-metadata tags are not customer-facing; they are excluded
    # from ordering. A stable-release trigger gets the focused per-tag check; any
    # other trigger (prerelease/build/non-SemVer) falls back to a repo-wide
    # stable-integrity scan so out-of-order stable releases are still caught.
    rel_tags = release_tags(semver_tags)
    is_release_trigger = (
        current_version is not None
        and not current_version.prerelease
        and not current_version.build
    )

    if is_release_trigger:
        commit_sha = get_tag_commit_sha(ref_name)
        logger.info("📍 Tag '%s' points to commit %s", ref_name, commit_sha[:12])

        ancestor_names = get_ancestor_tag_names(commit_sha) - {ref_name}
        descendant_names = get_descendant_tag_names(commit_sha) - {ref_name}

        ancestor_count = len([n for n in ancestor_names if n in rel_tags])
        descendant_count = len([n for n in descendant_names if n in rel_tags])
        logger.info(
            "📊 Validating against %d ancestor release tags and %d descendant release tags",
            ancestor_count,
            descendant_count,
        )

        current_tag = semver_tags[ref_name]
        violations = verify_graph_order(
            current_tag, rel_tags, ancestor_names, descendant_names
        )
    else:
        logger.info(
            "📊 Trigger tag is not a stable release; verifying repository "
            "stable-release integrity across %d release tags.",
            len(rel_tags),
        )
        violations = verify_repo_integrity(rel_tags)

    if violations:
        anchor = semver_tags.get(ref_name)
        if anchor is not None:
            print_violation_report(violations, anchor)
        else:
            logger.error("")
            logger.error("🚫 SEMVER ORDER VIOLATION DETECTED (repository integrity)")
            for v in violations:
                logger.error("  • %s", v)
        summary_lines = [
            SUMMARY_HEADER,
            "🚫 Failed: SemVer ordering violation",
            f"- Ref: tag `{ref_name}` (version `{current_version if current_version is not None else 'unknown'}`)",
            "- Mode: git-graph ancestry (stable releases only)",
            f"- Violations detected: {len(violations)}",
            "- Violations:",
        ]
        for v in violations:
            summary_lines.append(f"  - {v}")
        summary_lines.append("")
        append_summary(summary_lines)
        sys.exit(1)

    logger.info("")
    logger.info("=" * 70)
    logger.info("✅ SEMVER ORDER VALIDATION PASSED")
    logger.info("=" * 70)
    if is_release_trigger:
        logger.info("  Tag: %s (version %s)", ref_name, current_version)
    else:
        logger.info(
            "  Trigger tag '%s' ignored for ordering (not a stable release).",
            ref_name,
        )
    logger.info("  Release tags in repo: %d", len(rel_tags))
    logger.info("=" * 70)

    if current_version is not None:
        is_prerelease, is_latest = compute_tag_flags(semver_tags, current_version)
        step_outputs = derive_version_outputs(ref_name, current_version)
        step_outputs["is_prerelease"] = "true" if is_prerelease else "false"
        step_outputs["is_latest"] = "true" if is_latest else "false"
        append_github_outputs(step_outputs)
    append_summary(
        [
            SUMMARY_HEADER,
            "✅ Passed: SemVer ordering verified",
            f"- Ref: tag `{ref_name}` (version `{current_version if current_version is not None else 'unknown'}`)",
            "- Mode: git-graph ancestry (stable releases only)",
            f"- Release tags: {len(rel_tags)}",
            "",
        ]
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
