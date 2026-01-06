# Check SemVer Documentation

## Key Features
- **Efficient**: O(n log n) complexity using a single git command
- **Configurable**: Choose between strict or branch-aware validation
- **Informative output**: Detailed violation reports with suggested fixes
- **Prerelease support**: Correctly handles prerelease versions (e.g., `1.0.0-alpha`, `1.0.0-beta`)
- **Outputs**: exposes `version`, `major`, `minor`, and `major_minor` for usage in subsequent steps

## Validation Modes

### Strict Mode (default)

In strict mode, **all tags must be in global semver order**. This means:
- ❌ `v1.0.1` tagged after `v1.1.0` is **not allowed**
- ❌ `v1.0.1` tagged after `v1.0.2` is **not allowed**

Use this mode when:
- You have a single release branch
- You don't use backported hotfixes to older versions
- You want maximum enforcement of versioning discipline

### Branch-Aware Mode

In branch-aware mode, **tags are grouped by major.minor series**. This allows backports:
- ✅ `v1.0.1` tagged after `v1.1.0` is **allowed** (different series)
- ❌ `v1.0.1` tagged after `v1.0.2` is **not allowed** (same series, wrong order)

Use this mode when:
- You maintain multiple release branches (e.g., LTS releases)
- You backport security fixes to older versions
- You follow a typical Git Flow or similar branching strategy

To enable branch-aware mode:

```yaml
- uses: zebbra/actions/check-semver@check-semver/v2
  with:
    allow_backports: 'true'
```

## Example Output

### Success (Strict Mode)

```
🔍 Starting semver order validation...
   Mode: strict (no backports)

📋 Found 50 total tags in repository
✓  Parsed 48 valid semver tags
🔒 Checking strict global order for 48 tags

======================================================================
✅ SEMVER ORDER VALIDATION PASSED
======================================================================
  Validated 48 tags in strict global order
  Mode: strict (no backports)

  • Range: 1.0.0 → 2.1.4
======================================================================
```

### Success (Branch-Aware Mode)

```
🔍 Starting semver order validation...
   Mode: branch-aware (backports allowed)

📋 Found 50 total tags in repository
✓  Parsed 48 valid semver tags
📦 Grouped tags into 5 release series: 1.0, 1.1, 1.2, 2.0, 2.1

======================================================================
✅ SEMVER ORDER VALIDATION PASSED
======================================================================
  Validated 48 tags across 5 release series
  Mode: branch-aware (backports allowed)

  • 1.0.x: 1.0.0 → 1.0.5 (6 tags)
  • 1.1.x: 1.1.0 → 1.1.3 (4 tags)
  • 1.2.x: 1.2.0 → 1.2.2 (3 tags)
  • 2.0.x: 2.0.0 → 2.0.4 (5 tags)
  • 2.1.x: 2.1.0 → 2.1.4 (5 tags)
======================================================================
```

### Failure

```
======================================================================
🚫 SEMVER ORDER VIOLATION DETECTED
======================================================================

📌 Global Order Violations (strict mode)
--------------------------------------------------

  Problem:
    • v1.0.2 was tagged on 2024-03-15 10:30:00
    • v1.0.3 was tagged on 2024-03-10 14:20:00
    • But v1.0.2 < v1.0.3 semantically

  Suggested fixes:
    1. Delete the incorrect tag:
       git tag -d v1.0.2
       git push --delete origin v1.0.2

    2. Or, if v1.0.2 should come after v1.0.3,
       consider re-tagging as v1.0.4 or higher

======================================================================
Total: 1 violation(s) found

💡 Tip: If you use backported releases (e.g., hotfixes to older
   branches), set ALLOW_BACKPORTS=true to enable branch-aware mode.
======================================================================
```
