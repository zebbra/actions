# Zebbra GitHub Actions
This repository contains Zebbra internal GitHub Actions & Workflows, aka a private source-of-truth for reusable GitHub Actions within the `zebbra` organization. **All actions are private and not published to the GitHub Marketplace.**

## Usage

You can reference actions by namespaced tags, here are some examples:

```yaml
steps:
  - uses: actions/checkout@v4
  - name: Check SemVer
    uses: zebbra/actions/check-semver@check-semver/v2 # or check-semver/latest
  - name: Build image (no push)
    uses: zebbra/actions/docker-build@docker-build/v1 # or docker-build/latest
    with:
      image_name: ghcr.io/zebbra/my-service
      context: .
      push: false
  - name: Release GitHub Release
    uses: zebbra/actions/github-release@github-release/latest # or e.g. github-release/v1
    with:
      release_beta_tags: false
```

> **📝 Notes:**
> - Use `<action>/latest` for a moving pointer, or `<action>/vN` for a fixed version.
> - Pushing Docker images requires `permissions: { contents: read, packages: write }`.

## Available Actions
The following actions are currently available in this repository: 

<!-- LATEST_TAGS_START -->
| Action | Latest version | Documentation |
| ------ | -------------- | ------------- |
| check-semver | [check-semver/v7](docs/check-semver/v7.md) | [Documentation](docs/check-semver/v7.md) |
| docker-build | [docker-build/v6](docs/docker-build/v6.md) | [Documentation](docs/docker-build/v6.md) |
| enforce-pr-label | [enforce-pr-label/v1](docs/enforce-pr-label/v1.md) | [Documentation](docs/enforce-pr-label/v1.md) |
| github-release | [github-release/v2](docs/github-release/v2.md) | [Documentation](docs/github-release/v2.md) |
<!-- LATEST_TAGS_END -->

## Creating Actions
Feel free to create new actions, each action lives in its own folder at the repository root. **Make sure to create a new release after creating a new action.**

### Folder layout (typical)

```
<action-name>/
├── action.yml          # Required: action definition
├── DOCS.md             # Optional: narrative docs (included in generated docs)
├── README.md           # Auto-generated: do not edit manually
├── test_*.py           # Optional: pytest tests (run by CI and release gate)
└── ...                 # Implementation files (scripts, dependencies, etc.)
```

### Files and what they’re for

- **`action.yml` (required)**: The GitHub Action definition:
  - **`name` / `description`**: Marketplace-facing metadata
  - **`inputs` / `outputs`**: Public interface for workflows
  - **`runs`**: How the action executes
- **`DOCS.md` (optional)**: Extended documentation that gets merged into the generated docs (examples, configuration details, gotchas).
- **`*.py` (optional)**: Python implementation scripts (when bash isn’t sufficient).
- **`test_*.py` (optional)**: Pytest tests for the action’s custom code. CI runs these automatically for changed actions, and the release workflow gates on them.
- **`requirements.txt` (optional)**: Python dependencies for the action.
- **`Dockerfile` (optional)**: Docker-based action implementation (when needed).

## Releasing
To create a new version of an action, you need to:
- Run the `Release actions` workflow and choose an action.
- Provide `version` like `v3` to set explicitly, or leave empty to auto-derive the next integer (starts at `v0`).

Each release creates `<action>/vN` and moves `<action>/latest` to the new version.


## How to document actions


Action documentation is **auto-generated on release** using `.github/workflows/scripts/generate_action_docs.py`. You can provide custom documentation via a `<action>/DOCS.md` file it will then be included in the generated documentation.


  > **📝 Notes:**
  > - **You should edit**: `action.yml` (API + metadata) and optionally `<action>/DOCS.md` (narrative docs).
  > - **You should <ins>not</ins> edit**: generated files under `docs/<action>/vN.md` and `<action>/README.md` (they’re overwritten on release).
  > - **Docs.md:** The `<action>/DOCS.md` file will be stripped of any H1 headings and should be concise and focused on narrative/examples/gotchas/migrations and not duplicate the generated sections.

### What eventually gets generated:


| Content | Source |
|---------|--------|
| Title & description | `action.yml` |
| Usage snippet | Auto-generated |
| Inputs | `action.yml` → `inputs` |
| Outputs | `action.yml` → `outputs` |
| Extended docs | `<action>/DOCS.md` (optional) |
| Technical details | `action.yml` → `runs.*` |
| Files list | Directory scan |
| Other versions | `docs/<action>/` scan |





