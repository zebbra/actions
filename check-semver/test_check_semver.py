#!/usr/bin/env python3
"""
Tests for check-semver.py -- graph-based semver ordering validation.

Integration tests create real git repositories in temporary directories
and exercise the core functions against actual commit DAG topologies.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from packaging.version import Version, InvalidVersion

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

    def tag(self, name: str) -> None:
        self.run("tag", name)

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
    repo.run("init")
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

    def test_empty_string(self):
        assert check_semver.strip_v_prefix("") == ""

    def test_v_only(self):
        assert check_semver.strip_v_prefix("v") == "v"


class TestParseTagVersion:
    def test_valid_version(self):
        v = check_semver.parse_tag_version("v1.2.3")
        assert v == Version("1.2.3")

    def test_prerelease(self):
        v = check_semver.parse_tag_version("v1.0.0-beta.1")
        assert v.is_prerelease

    def test_no_prefix(self):
        v = check_semver.parse_tag_version("2.0.0")
        assert v == Version("2.0.0")

    def test_invalid_raises(self):
        with pytest.raises(InvalidVersion):
            check_semver.parse_tag_version("not-a-version")


class TestDeriveVersionOutputs:
    def test_with_v_prefix(self):
        result = check_semver.derive_version_outputs("v1.2.3", Version("1.2.3"))
        assert result == {
            "version": "1.2.3",
            "major": "1",
            "minor": "2",
            "major_minor": "1.2",
        }

    def test_without_v_prefix(self):
        result = check_semver.derive_version_outputs("1.2.3", Version("1.2.3"))
        assert result == {
            "version": "1.2.3",
            "major": "1",
            "minor": "2",
            "major_minor": "1.2",
        }

    def test_fallback_no_version(self):
        result = check_semver.derive_version_outputs("v1.2.3", None)
        assert result["version"] == "1.2.3"
        assert result["major"] == "1"
        assert result["minor"] == "2"


class TestComputeTagFlags:
    def _make_tag(self, name: str, ver_str: str) -> check_semver.TagInfo:
        return check_semver.TagInfo(
            name=name, timestamp=0, version=Version(ver_str)
        )

    def test_stable_is_latest(self):
        tags = {
            "v1.0.0": self._make_tag("v1.0.0", "1.0.0"),
            "v2.0.0": self._make_tag("v2.0.0", "2.0.0"),
        }
        is_pre, is_latest = check_semver.compute_tag_flags(tags, Version("2.0.0"))
        assert not is_pre
        assert is_latest

    def test_stable_not_latest(self):
        tags = {
            "v1.0.0": self._make_tag("v1.0.0", "1.0.0"),
            "v2.0.0": self._make_tag("v2.0.0", "2.0.0"),
        }
        is_pre, is_latest = check_semver.compute_tag_flags(tags, Version("1.0.0"))
        assert not is_pre
        assert not is_latest

    def test_prerelease(self):
        tags = {
            "v1.0.0": self._make_tag("v1.0.0", "1.0.0"),
            "v2.0.0-beta.1": self._make_tag("v2.0.0-beta.1", "2.0.0b1"),
        }
        is_pre, is_latest = check_semver.compute_tag_flags(tags, Version("2.0.0b1"))
        assert is_pre
        assert not is_latest

    def test_none_version(self):
        tags = {"v1.0.0": self._make_tag("v1.0.0", "1.0.0")}
        is_pre, is_latest = check_semver.compute_tag_flags(tags, None)
        assert not is_pre
        assert not is_latest


# ---------------------------------------------------------------------------
# Integration tests -- verify_graph_order with real Violation objects
# ---------------------------------------------------------------------------

class TestVerifyGraphOrder:
    """Unit-level tests for verify_graph_order using constructed data."""

    def _tag(self, name: str, ver_str: str) -> check_semver.TagInfo:
        return check_semver.TagInfo(
            name=name, timestamp=0, version=Version(ver_str)
        )

    def test_no_ancestors_no_descendants(self):
        current = self._tag("v1.0.0", "1.0.0")
        violations = check_semver.verify_graph_order(
            current, {"v1.0.0": current}, set(), set()
        )
        assert violations == []

    def test_ancestor_violation(self):
        current = self._tag("v1.0.0", "1.0.0")
        higher = self._tag("v2.0.0", "2.0.0")
        tags = {"v1.0.0": current, "v2.0.0": higher}
        violations = check_semver.verify_graph_order(
            current, tags, {"v2.0.0"}, set()
        )
        assert len(violations) == 1
        assert violations[0].kind == "ancestor"

    def test_descendant_violation(self):
        current = self._tag("v2.0.0", "2.0.0")
        lower = self._tag("v1.0.0", "1.0.0")
        tags = {"v2.0.0": current, "v1.0.0": lower}
        violations = check_semver.verify_graph_order(
            current, tags, set(), {"v1.0.0"}
        )
        assert len(violations) == 1
        assert violations[0].kind == "descendant"

    def test_equal_version_ancestor_is_violation(self):
        current = self._tag("v1.0.0-a", "1.0.0")
        same = self._tag("v1.0.0-b", "1.0.0")
        tags = {"v1.0.0-a": current, "v1.0.0-b": same}
        violations = check_semver.verify_graph_order(
            current, tags, {"v1.0.0-b"}, set()
        )
        assert len(violations) == 1
        assert violations[0].kind == "ancestor"

    def test_non_semver_ancestor_ignored(self):
        current = self._tag("v1.0.0", "1.0.0")
        tags = {"v1.0.0": current}
        violations = check_semver.verify_graph_order(
            current, tags, {"not-semver"}, set()
        )
        assert violations == []


# ---------------------------------------------------------------------------
# Integration tests -- real git repos
# ---------------------------------------------------------------------------

def _resolve_and_check(repo: GitRepo, tag_name: str) -> list[check_semver.Violation]:
    """
    Run the full graph-based check for a tag in the given repo.

    Returns the list of violations (empty means pass).
    """
    old_cwd = os.getcwd()
    try:
        os.chdir(repo.path)

        raw_tags = check_semver.fetch_all_tags_with_metadata()
        semver_tags = check_semver.parse_semver_tags(raw_tags)

        commit_sha = check_semver.get_tag_commit_sha(tag_name)
        ancestor_names = check_semver.get_ancestor_tag_names(commit_sha) - {tag_name}
        descendant_names = check_semver.get_descendant_tag_names(commit_sha) - {tag_name}

        current_tag = semver_tags[tag_name]
        return check_semver.verify_graph_order(
            current_tag, semver_tags, ancestor_names, descendant_names
        )
    finally:
        os.chdir(old_cwd)


class TestLinearHistory:
    def test_correct_order(self, git_repo: GitRepo):
        """A(v1.0.0) -> B(v1.1.0) -> C(v1.2.0): checking v1.2.0 should pass."""
        git_repo.tag("v1.0.0")
        git_repo.commit("second")
        git_repo.tag("v1.1.0")
        git_repo.commit("third")
        git_repo.tag("v1.2.0")

        violations = _resolve_and_check(git_repo, "v1.2.0")
        assert violations == []

    def test_wrong_order(self, git_repo: GitRepo):
        """A(v1.0.0) -> B(v1.2.0) -> C(v1.1.0): checking v1.1.0 should fail."""
        git_repo.tag("v1.0.0")
        git_repo.commit("second")
        git_repo.tag("v1.2.0")
        git_repo.commit("third")
        git_repo.tag("v1.1.0")

        violations = _resolve_and_check(git_repo, "v1.1.0")
        assert len(violations) == 1
        assert violations[0].kind == "ancestor"
        assert violations[0].other_tag.name == "v1.2.0"


class TestBranchingNoRelation:
    def test_parallel_branches_no_violation(self, git_repo: GitRepo):
        """
        main: A(v1.0.0) -> B(v1.0.1)
        feature: A -> C(v1.2.0-beta.1)  (unmerged)

        Checking v1.0.1 should pass because v1.2.0-beta.1 is not an ancestor.
        """
        git_repo.tag("v1.0.0")

        git_repo.branch("feature")
        git_repo.commit("feature work")
        git_repo.tag("v1.2.0-beta.1")

        git_repo.checkout("main")
        git_repo.commit("patch")
        git_repo.tag("v1.0.1")

        violations = _resolve_and_check(git_repo, "v1.0.1")
        assert violations == []


class TestColleagueScenario:
    def test_develop_patch_while_feature_has_beta(self, git_repo: GitRepo):
        """
        develop: A(v1.16.0) -> B(v1.16.1)
        feature: A -> C(v1.17.0-beta.55)  (unmerged)

        Checking v1.16.1 should pass.
        """
        git_repo.tag("v1.16.0")

        git_repo.branch("feature")
        git_repo.commit("feature work")
        git_repo.tag("v1.17.0-beta.55")

        git_repo.checkout("main")
        git_repo.commit("hotfix")
        git_repo.tag("v1.16.1")

        violations = _resolve_and_check(git_repo, "v1.16.1")
        assert violations == []


class TestPostMerge:
    def test_correct_version_after_merge(self, git_repo: GitRepo):
        """
        After merging feature(v1.17.0-beta.55) into develop(v1.16.1),
        tagging v1.17.0 on the merge commit should pass.
        """
        git_repo.tag("v1.16.0")

        git_repo.branch("feature")
        git_repo.commit("feature work")
        git_repo.tag("v1.17.0-beta.55")

        git_repo.checkout("main")
        git_repo.commit("hotfix")
        git_repo.tag("v1.16.1")

        git_repo.merge("feature")
        git_repo.tag("v1.17.0")

        violations = _resolve_and_check(git_repo, "v1.17.0")
        assert violations == []

    def test_incorrect_version_after_merge(self, git_repo: GitRepo):
        """
        After merging feature(v1.17.0-beta.55) into develop(v1.16.1),
        tagging v1.16.2 on the merge commit should FAIL because
        v1.17.0-beta.55 is now an ancestor and 1.16.2 < 1.17.0b55.
        """
        git_repo.tag("v1.16.0")

        git_repo.branch("feature")
        git_repo.commit("feature work")
        git_repo.tag("v1.17.0-beta.55")

        git_repo.checkout("main")
        git_repo.commit("hotfix")
        git_repo.tag("v1.16.1")

        git_repo.merge("feature")
        git_repo.tag("v1.16.2")

        violations = _resolve_and_check(git_repo, "v1.16.2")
        assert len(violations) >= 1
        ancestor_violations = [v for v in violations if v.kind == "ancestor"]
        assert any(
            v.other_tag.name == "v1.17.0-beta.55" for v in ancestor_violations
        )


class TestDescendantCheck:
    def test_tag_old_commit_with_high_version(self, git_repo: GitRepo):
        """
        A(v1.0.0) -> B(v1.1.0) -> C(v1.2.0)

        Then tag A also as v1.5.0. Checking v1.5.0 should fail:
        v1.0.0 (same commit), v1.1.0 and v1.2.0 are descendants with
        version <= 1.5.0. v1.0.0 appears because git tag --contains
        includes the commit itself.
        """
        first_sha = git_repo.head_sha()
        git_repo.tag("v1.0.0")

        git_repo.commit("second")
        git_repo.tag("v1.1.0")

        git_repo.commit("third")
        git_repo.tag("v1.2.0")

        git_repo.run("tag", "v1.5.0", first_sha)

        violations = _resolve_and_check(git_repo, "v1.5.0")
        descendant_violations = [v for v in violations if v.kind == "descendant"]
        assert len(descendant_violations) == 3
        names = {v.other_tag.name for v in descendant_violations}
        assert names == {"v1.0.0", "v1.1.0", "v1.2.0"}


class TestSingleTag:
    def test_single_tag_passes(self, git_repo: GitRepo):
        """A single tag in the repo should always pass."""
        git_repo.tag("v1.0.0")

        violations = _resolve_and_check(git_repo, "v1.0.0")
        assert violations == []


class TestNonSemverTagsIgnored:
    def test_non_semver_not_compared(self, git_repo: GitRepo):
        """
        A(v1.0.0, release-candidate) -> B(v1.1.0)
        Non-semver tags should be silently ignored.
        """
        git_repo.tag("v1.0.0")
        git_repo.tag("release-candidate")
        git_repo.commit("second")
        git_repo.tag("v1.1.0")

        violations = _resolve_and_check(git_repo, "v1.1.0")
        assert violations == []


class TestEqualVersionOnAncestor:
    def test_same_version_on_ancestor_is_violation(self, git_repo: GitRepo):
        """
        A(v1.0.0) -> B(v1.0.0): same semver on descendant is a violation.
        We create two tags with identical version strings on different commits.
        """
        git_repo.tag("v1.0.0")
        git_repo.commit("second")
        # Can't have two tags with the same name, so use the version without prefix
        git_repo.tag("1.0.0")

        violations = _resolve_and_check(git_repo, "1.0.0")
        assert len(violations) == 1
        assert violations[0].kind == "ancestor"
        assert violations[0].other_tag.name == "v1.0.0"


class TestPrereleaseOrderingWithinBranch:
    def test_beta_to_release(self, git_repo: GitRepo):
        """
        A(v1.0.0-beta.1) -> B(v1.0.0-beta.2) -> C(v1.0.0)
        Should pass: each is strictly higher than its ancestor.
        """
        git_repo.tag("v1.0.0-beta.1")
        git_repo.commit("second")
        git_repo.tag("v1.0.0-beta.2")
        git_repo.commit("third")
        git_repo.tag("v1.0.0")

        violations = _resolve_and_check(git_repo, "v1.0.0")
        assert violations == []

        violations = _resolve_and_check(git_repo, "v1.0.0-beta.2")
        assert violations == []
