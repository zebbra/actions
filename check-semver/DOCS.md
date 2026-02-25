## Key Features
- **Git-graph aware**: Validates version ordering using commit ancestry, not timestamps. Tags on unrelated branches are never compared.
- **Efficient**: Two git traversals (`git tag --merged`, `git tag --contains`) plus a single `git for-each-ref` for metadata.
- **Bi-directional check**: Ancestor tags must have strictly lower versions, descendant tags must have strictly higher versions.
- **Informative output**: Detailed violation reports showing graph relationships with suggested fixes.
- **Prerelease support**: Correctly handles prerelease versions (e.g., `1.0.0-alpha`, `1.0.0-beta`).
- **Outputs**: Exposes `version`, `major`, `minor`, `major_minor`, `is_prerelease`, and `is_latest` for usage in subsequent steps (`is_latest` is based on the highest stable/non-prerelease SemVer tag across all branches).

## How it works

The action validates that semver tags respect the git commit graph. The invariant is:

> If commit A is an ancestor of commit B, then `version(tag_A) < version(tag_B)`.

Tags that are not in an ancestor/descendant relationship (e.g., on separate unmerged branches) are never compared. This naturally supports multi-branch workflows: beta tags on feature branches don't interfere with patch releases on the main branch.

### Ancestor check

All semver tags reachable from the current tag's commit (via `git tag --merged`) must have a strictly lower version. This catches the common case of tagging a version that's lower than what came before on the same branch.

### Descendant check

All semver tags whose commits descend from the current tag's commit (via `git tag --contains`) must have a strictly higher version. This catches the edge case of someone tagging an older commit with a version that's too high.

## Multi-branch workflows

Because the action uses commit ancestry rather than timestamps, multi-branch workflows work without any special configuration:

- **Feature branches with prereleases**: `v1.17.0-beta.55` on a feature branch does not conflict with `v1.16.1` on develop, because neither commit is an ancestor of the other.
- **Post-merge**: After merging the feature branch, `v1.17.0-beta.55` becomes an ancestor of the merge commit. Tagging the merge commit (or a descendant) as `v1.17.0` works because `1.17.0 > 1.17.0-beta.55` and `1.17.0 > 1.16.1`.
- **Maintenance branches**: Hotfixes to older release series on separate branches don't conflict with newer versions on the main line.

## Required refs and tag format

- The action must run on a tag ref; branch or PR refs fail with a summary entry explaining the unsupported ref type.
- Tags must be valid semantic versions (with optional leading `v`, e.g., `v1.2.3`); invalid tags fail fast and annotate the GitHub summary with the error and the tag name.

## Tag deletion events

When a tag is deleted via a push deletion (e.g., `git push --delete origin v1.2.3`), GitHub sets `github.event.deleted=true`.

- By default, the action **fails** on tag deletion events (`fail_on_deleted: 'true'`) while still reporting outputs.
- To **succeed** on deletion events, set `fail_on_deleted: 'false'`.
- Graph validation is skipped for deletion events because removing a tag cannot introduce an ordering violation.

## Breaking changes from v6

- The `allow_backports` input has been removed. The graph-based approach inherently handles branch separation without configuration. If you were using `allow_backports: 'true'`, no migration is needed -- the new behavior is equivalent (and more correct). If you were using the default strict mode, the new behavior is less restrictive for multi-branch workflows but equally strict within a single branch's lineage.

## Example Output

### Success

```
🔍 Starting semver order validation for tag 'v1.2.0' (version '1.2.0')...
   Mode: git-graph ancestry (commit DAG)

📋 Found 50 total tags in repository
✓  Parsed 48 valid semver tags
📍 Tag 'v1.2.0' points to commit a1b2c3d4e5f6
🔍 Found 47 ancestor tags for a1b2c3d4e5f6 (12ms)
🔍 Found 0 descendant tags for a1b2c3d4e5f6 (8ms)
📊 Validating against 45 ancestor semver tags and 0 descendant semver tags

======================================================================
✅ SEMVER ORDER VALIDATION PASSED
======================================================================
  Tag: v1.2.0 (version 1.2.0)
  Total semver tags in repo: 48
  Checked 45 ancestor tags and 0 descendant tags
======================================================================
```

### Failure (ancestor violation)

```
======================================================================
🚫 SEMVER ORDER VIOLATION DETECTED
======================================================================

📌 Ancestor violations (tags reachable from v1.16.2 with version >= 1.16.2):
--------------------------------------------------

  • v1.17.0-beta.55 (tagged 2026-02-20 08:12:57) is an ancestor but has version 1.17.0b55

  Suggested fix: use a version higher than all ancestors,
  e.g. 1.17.1 or higher.

======================================================================
Total: 1 violation(s) found (1 ancestor, 0 descendant)
======================================================================
```
