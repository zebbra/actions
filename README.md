# actions
Zebbra internal GitHub Actions & Workflows

Private source-of-truth for reusable GitHub Actions within the `zebbra` organization.

## Usage

Reference actions by namespaced tags:

```yaml
steps:
  - uses: actions/checkout@v4

  - name: Build image (no push)
    uses: zebbra/actions/.github/actions/docker-build@docker-build/v1
    with:
      image_name: ghcr.io/zebbra/my-service
      context: .
      push: false

  - name: Check SemVer
    uses: zebbra/actions/.github/actions/check-semver@check-semver/v1
```

Notes:
- Use `<action>/latest` for a moving pointer, or `<action>/vN` for a fixed version.
- Pushing Docker images requires `permissions: { contents: read, packages: write }`.

## Releasing

Manual only:
- Run the `Release actions` workflow and choose an action.
- Provide `version` like `v3` to set explicitly, or leave empty to auto-derive the next integer (starts at `v0`).

Each release creates `<action>/vN` and moves `<action>/latest`.

## Latest versions
<!-- LATEST_TAGS_START -->
| Action | Latest version | Documentation |
| ------ | -------------- | ------------- |
| check-semver | [check-semver/v0](docs/check-semver/v0.md) | [Documentation](docs/check-semver/v0.md) |
| docker-build | [docker-build/v1](docs/docker-build/v1.md) | [Documentation](docs/docker-build/v1.md) |
<!-- LATEST_TAGS_END -->
