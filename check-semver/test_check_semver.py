#!/usr/bin/env python3
"""
Tests for check-semver.py -- graph-based semver ordering validation.

Integration tests create real git repositories in temporary directories
and exercise the core functions against actual commit DAG topologies.

Ordering integrity is enforced against STABLE releases only (no prerelease,
no build metadata); prerelease/build/non-SemVer tags are ignored for ordering.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from semver import Version

import pytest

# Import the module under test (same directory)
import importlib.util
import sys
import os

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("check_semver", _HERE / "check-semver.py")
check_semver = importlib.util.module_from_spec(_SPEC)
sys.modules["check_semver"] = check_semver
_SPEC.loader.exec_module(check_semver)


def V(s: str) -> Version:
    """Parse a SemVer string (no v-prefix)."""
    return Version.parse(s)


# ---------------------------------------------------------------------------
# Git repo test helper
# ---------------------------------------------------------------------------

@dataclass
class GitRepo:
    """Helper for creating and manipulating a temporary git repo."""

    path: Path

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True,
            text=True,
            check=check,
        )

    def commit(self, message: str = "commit") -> str:
        """Create an empty commit and return its SHA."""
        self.run("commit", "--allow-empty", "-m", message)
        return self.run("rev-parse", "HEAD").stdout.strip()

    def tag(self, name: str, ref: str | None = None) -> None:
        if ref is None:
            self.run("tag", name)
        else:
            self.run("tag", name, ref)

    def branch(self, name: str) -> None:
        """Create a new branch at HEAD and switch to it."""
        self.run("checkout", "-b", name)

    def checkout(self, name: str) -> None:
        self.run("checkout", name)

    def merge(self, branch: str, message: str | None = None) -> str:
        """Merge a branch into the current branch and return the merge commit SHA."""
        msg = message or f"Merge {branch}"
        self.run("merge", branch, "--no-ff", "-m", msg)
        return self.run("rev-parse", "HEAD").stdout.strip()

    def head_sha(self) -> str:
        return self.run("rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> GitRepo:
    """Create a bare-bones git repository for testing."""
    repo = GitRepo(path=tmp_path)
    repo.run("init", "-b", "main")
    repo.run("config", "user.email", "test@test.com")
    repo.run("config", "user.name", "Test")
    repo.commit("initial")
    return repo


# ---------------------------------------------------------------------------
# Unit tests -- pure functions, no git
# ---------------------------------------------------------------------------

class TestStripVPrefix:
    def test_strips_lowercase_v(self):
        assert check_semver.strip_v_prefix("v1.2.3") == "1.2.3"

    def test_strips_uppercase_v(self):
        assert check_semver.strip_v_prefix("V1.0.0") == "1.0.0"

    def test_no_prefix(self):
        assert check_semver.strip_v_prefix("1.2.3") == "1.2.3"

    def test_does_not_strip_non_version(self):
        assert check_semver.strip_v_prefix("version") == "version"


class TestParseTagVersion:
    def test_valid_version(self):
        assert check_semver.parse_tag_version("v1.2.3") == V("1.2.3")

    def test_prerelease(self):
        assert check_semver.parse_tag_version("v1.0.0-beta.1").prerelease == "beta.1"

    def test_multi_identifier_prerelease_parses(self):
        # PEP 440 could not parse this; SemVer can.
        assert check_semver.parse_tag_version("v1.0.0-alpha.beta").prerelease == "alpha.beta"

    def test_numeric_prerelease_is_prerelease(self):
        # PEP 440 mis-parsed 1.0.0-1 as a post-release (is_prerelease False).
        assert check_semver.parse_tag_version("v1.0.0-1").prerelease == "1"

    @pytest.mark.parametrize("bad", ["not-a-version", "v1.0", "v01.2.3", "1.0.0.0", "1.0.0.post1"])
    def test_invalid_raises(self, bad):
        with pytest.raises(ValueError):
            check_semver.parse_tag_version(bad)


class TestReleaseTags:
    def _tag(self, name, ver):
        return check_semver.TagInfo(name=name, timestamp=0, version=V(ver))

    def test_excludes_prerelease_and_build(self):
        tags = {
            "v1.0.0": self._tag("v1.0.0", "1.0.0"),
            "v1.1.0-beta.1": self._tag("v1.1.0-beta.1", "1.1.0-beta.1"),
            "v1.0.0+build.5": self._tag("v1.0.0+build.5", "1.0.0+build.5"),
        }
        rel = check_semver.release_tags(tags)
        assert set(rel) == {"v1.0.0"}


class TestDeriveVersionOutputs:
    def test_with_v_prefix(self):
        result = check_semver.derive_version_outputs("v1.2.3", V("1.2.3"))
        assert result == {"version": "1.2.3", "major": "1", "minor": "2", "major_minor": "1.2"}

    def test_uppercase_v_prefix_stripped(self):
        # E1: previously leaked "V1.2.3" into the version output.
        result = check_semver.derive_version_outputs("V1.2.3", V("1.2.3"))
        assert result["version"] == "1.2.3"

    def test_fallback_no_version(self):
        result = check_semver.derive_version_outputs("v1.2.3", None)
        assert result["version"] == "1.2.3"
        assert result["major"] == "1"


class TestComputeTagFlags:
    def _make_tag(self, name: str, ver_str: str) -> check_semver.TagInfo:
        return check_semver.TagInfo(name=name, timestamp=0, version=V(ver_str))

    def test_stable_is_latest(self):
        tags = {
            "v1.0.0": self._make_tag("v1.0.0", "1.0.0"),
            "v2.0.0": self._make_tag("v2.0.0", "2.0.0"),
        }
        is_pre, is_latest = check_semver.compute_tag_flags(tags, V("2.0.0"))
        assert not is_pre and is_latest

    def test_stable_not_latest(self):
        tags = {
            "v1.0.0": self._make_tag("v1.0.0", "1.0.0"),
            "v2.0.0": self._make_tag("v2.0.0", "2.0.0"),
        }
        is_pre, is_latest = check_semver.compute_tag_flags(tags, V("1.0.0"))
        assert not is_pre and not is_latest

    def test_prerelease(self):
        tags = {
            "v1.0.0": self._make_tag("v1.0.0", "1.0.0"),
            "v2.0.0-beta.1": self._make_tag("v2.0.0-beta.1", "2.0.0-beta.1"),
        }
        is_pre, is_latest = check_semver.compute_tag_flags(tags, V("2.0.0-beta.1"))
        assert is_pre and not is_latest

    def test_build_metadata_ignored_for_latest(self):
        # SemVer §10: v2.0.0 and v2.0.0+build are equal precedence; the build tag
        # is not customer-facing, so v2.0.0 is still latest.
        tags = {
            "v1.0.0": self._make_tag("v1.0.0", "1.0.0"),
            "v2.0.0": self._make_tag("v2.0.0", "2.0.0"),
            "v2.0.0+build.5": self._make_tag("v2.0.0+build.5", "2.0.0+build.5"),
        }
        is_pre, is_latest = check_semver.compute_tag_flags(tags, V("2.0.0"))
        assert not is_pre and is_latest

    def test_none_version(self):
        tags = {"v1.0.0": self._make_tag("v1.0.0", "1.0.0")}
        is_pre, is_latest = check_semver.compute_tag_flags(tags, None)
        assert not is_pre and not is_latest


class TestVerifyGraphOrder:
    """Unit-level tests for verify_graph_order using constructed data."""

    def _tag(self, name: str, ver_str: str) -> check_semver.TagInfo:
        return check_semver.TagInfo(name=name, timestamp=0, version=V(ver_str))

    def test_no_ancestors_no_descendants(self):
        current = self._tag("v1.0.0", "1.0.0")
        assert check_semver.verify_graph_order(current, {"v1.0.0": current}, set(), set()) == []

    def test_ancestor_violation(self):
        current = self._tag("v1.0.0", "1.0.0")
        higher = self._tag("v2.0.0", "2.0.0")
        tags = {"v1.0.0": current, "v2.0.0": higher}
        violations = check_semver.verify_graph_order(current, tags, {"v2.0.0"}, set())
        assert len(violations) == 1 and violations[0].kind == "ancestor"

    def test_descendant_violation(self):
        current = self._tag("v2.0.0", "2.0.0")
        lower = self._tag("v1.0.0", "1.0.0")
        tags = {"v2.0.0": current, "v1.0.0": lower}
        violations = check_semver.verify_graph_order(current, tags, set(), {"v1.0.0"})
        assert len(violations) == 1 and violations[0].kind == "descendant"

    def test_equal_version_ancestor_is_violation(self):
        current = self._tag("a", "1.0.0")
        same = self._tag("b", "1.0.0")
        tags = {"a": current, "b": same}
        violations = check_semver.verify_graph_order(current, tags, {"b"}, set())
        assert len(violations) == 1 and violations[0].kind == "ancestor"


# ---------------------------------------------------------------------------
# Integration tests -- real git repos
# ---------------------------------------------------------------------------

def _check(repo: GitRepo, tag_name: str) -> list[check_semver.Violation]:
    """
    Mirror main()'s ordering dispatch for a triggering tag and return violations.

    - Stable-release trigger  -> focused per-tag check over release tags.
    - Anything else (prerelease/build/non-SemVer) -> repo stable-integrity scan.
    """
    old_cwd = os.getcwd()
    try:
        os.chdir(repo.path)
        raw_tags = check_semver.fetch_all_tags_with_metadata()
        semver_tags = check_semver.parse_semver_tags(raw_tags)
        rel_tags = check_semver.release_tags(semver_tags)

        try:
            cv = check_semver.parse_tag_version(tag_name)
        except ValueError:
            cv = None
        is_release = cv is not None and not cv.prerelease and not cv.build

        if is_release:
            sha = check_semver.get_tag_commit_sha(tag_name)
            anc = check_semver.get_ancestor_tag_names(sha) - {tag_name}
            desc = check_semver.get_descendant_tag_names(sha) - {tag_name}
            return check_semver.verify_graph_order(rel_tags[tag_name], rel_tags, anc, desc)
        return check_semver.verify_repo_integrity(rel_tags)
    finally:
        os.chdir(old_cwd)


class TestLinearHistory:
    def test_correct_order(self, git_repo: GitRepo):
        git_repo.tag("v1.0.0")
        git_repo.commit("second"); git_repo.tag("v1.1.0")
        git_repo.commit("third"); git_repo.tag("v1.2.0")
        assert _check(git_repo, "v1.2.0") == []

    def test_wrong_order(self, git_repo: GitRepo):
        git_repo.tag("v1.0.0")
        git_repo.commit("second"); git_repo.tag("v1.2.0")
        git_repo.commit("third"); git_repo.tag("v1.1.0")
        violations = _check(git_repo, "v1.1.0")
        assert len(violations) == 1
        assert violations[0].kind == "ancestor"
        assert violations[0].other_tag.name == "v1.2.0"

    def test_stable_inversion_flagged(self, git_repo: GitRepo):
        """v2.0.0 ancestor, v1.9.0 descendant -> stable ordering violation."""
        git_repo.tag("v2.0.0")
        git_repo.commit("second"); git_repo.tag("v1.9.0")
        violations = _check(git_repo, "v1.9.0")
        assert len(violations) == 1 and violations[0].kind == "ancestor"


class TestBranchingNoRelation:
    def test_parallel_branches_no_violation(self, git_repo: GitRepo):
        git_repo.tag("v1.0.0")
        git_repo.branch("feature"); git_repo.commit("feature work"); git_repo.tag("v1.2.0-beta.1")
        git_repo.checkout("main"); git_repo.commit("patch"); git_repo.tag("v1.0.1")
        assert _check(git_repo, "v1.0.1") == []


class TestPrereleaseAndBuildIgnored:
    def test_beta_ancestor_never_violates(self, git_repo: GitRepo):
        """Accepted trade-off: a lower stable tag after a higher BETA ancestor is
        NOT a violation, because betas are excluded from ordering."""
        git_repo.tag("v1.16.0")
        git_repo.branch("feature"); git_repo.commit("feat"); git_repo.tag("v1.17.0-beta.55")
        git_repo.checkout("main"); git_repo.commit("hotfix"); git_repo.tag("v1.16.1")
        git_repo.merge("feature"); git_repo.tag("v1.16.2")
        # v1.17.0-beta.55 is now an ancestor of v1.16.2 but is a prerelease -> ignored.
        assert _check(git_repo, "v1.16.2") == []

    def test_stable_ancestor_after_merge_still_flagged(self, git_repo: GitRepo):
        """But a higher STABLE ancestor after a merge IS flagged."""
        git_repo.tag("v1.16.0")
        git_repo.branch("feature"); git_repo.commit("feat"); git_repo.tag("v1.17.0")
        git_repo.checkout("main"); git_repo.commit("hotfix"); git_repo.tag("v1.16.1")
        git_repo.merge("feature"); git_repo.tag("v1.16.2")
        violations = _check(git_repo, "v1.16.2")
        assert any(v.other_tag.name == "v1.17.0" for v in violations if v.kind == "ancestor")

    def test_build_metadata_tag_excluded_from_ordering(self, git_repo: GitRepo):
        """A +build tag that would otherwise invert ordering is ignored."""
        git_repo.tag("v2.0.0")
        git_repo.commit("second"); git_repo.tag("v1.0.0+build.5")
        # trigger the build tag: not a stable release -> integrity scan over {v2.0.0}
        assert _check(git_repo, "v1.0.0+build.5") == []
        # and the build tag does not appear as a descendant violation for v2.0.0
        assert _check(git_repo, "v2.0.0") == []


class TestPostMergeStable:
    def test_correct_version_after_merge(self, git_repo: GitRepo):
        git_repo.tag("v1.16.0")
        git_repo.branch("feature"); git_repo.commit("feat"); git_repo.tag("v1.16.5")
        git_repo.checkout("main"); git_repo.commit("hotfix"); git_repo.tag("v1.16.1")
        git_repo.merge("feature"); git_repo.tag("v1.17.0")
        assert _check(git_repo, "v1.17.0") == []


class TestDescendantCheck:
    def test_tag_old_commit_with_high_version(self, git_repo: GitRepo):
        first_sha = git_repo.head_sha()
        git_repo.tag("v1.0.0")
        git_repo.commit("second"); git_repo.tag("v1.1.0")
        git_repo.commit("third"); git_repo.tag("v1.2.0")
        git_repo.tag("v1.5.0", first_sha)
        violations = _check(git_repo, "v1.5.0")
        descendant = [v for v in violations if v.kind == "descendant"]
        assert {v.other_tag.name for v in descendant} == {"v1.0.0", "v1.1.0", "v1.2.0"}


class TestSingleAndNonSemver:
    def test_single_tag_passes(self, git_repo: GitRepo):
        git_repo.tag("v1.0.0")
        assert _check(git_repo, "v1.0.0") == []

    def test_non_semver_bystander_ignored(self, git_repo: GitRepo):
        git_repo.tag("v1.0.0"); git_repo.tag("release-candidate")
        git_repo.commit("second"); git_repo.tag("v1.1.0")
        assert _check(git_repo, "v1.1.0") == []

    def test_invalid_format_bystanders_ignored(self, git_repo: GitRepo):
        """v1.0, v01.2.3, v1.0.0.post1 are NOT valid SemVer -> skipped, no false violation."""
        git_repo.tag("v1.0.0")
        git_repo.tag("v1.0"); git_repo.tag("v01.2.3"); git_repo.tag("v1.0.0.post1")
        git_repo.commit("second"); git_repo.tag("v1.1.0")
        assert _check(git_repo, "v1.1.0") == []
        # confirm they were dropped from the parsed set
        os.chdir(git_repo.path)
        try:
            parsed = check_semver.parse_semver_tags(check_semver.fetch_all_tags_with_metadata())
        finally:
            os.chdir(_HERE)
        assert set(parsed) == {"v1.0.0", "v1.1.0"}


class TestNonSemverTriggerIntegrityFallback:
    def test_non_semver_trigger_still_catches_stable_inversion(self, git_repo: GitRepo):
        """A non-SemVer trigger tag doesn't fail, but an out-of-order stable pair
        elsewhere in the repo is still caught."""
        git_repo.tag("v2.0.0")
        git_repo.commit("second"); git_repo.tag("v1.0.0")  # inversion
        git_repo.commit("third"); git_repo.tag("nightly")  # non-semver trigger
        violations = _check(git_repo, "nightly")
        assert any(v.kind == "ancestor" and v.other_tag.name == "v2.0.0" for v in violations)

    def test_non_semver_trigger_clean_repo_passes(self, git_repo: GitRepo):
        git_repo.tag("v1.0.0")
        git_repo.commit("second"); git_repo.tag("v1.1.0")
        git_repo.commit("third"); git_repo.tag("nightly")
        assert _check(git_repo, "nightly") == []
