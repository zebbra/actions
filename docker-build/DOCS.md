## What this action does (beyond a plain docker build)

This action wraps Docker Buildx with:

- **Consistent tags/labels** via `docker/metadata-action` (SHA/ref tags, optional `latest`, automatic `main`/`develop` branch tags, plus custom rules).
- **GHA BuildKit cache** with a “smart” `cache-from` heuristic (branch/base branch + `develop` + `main`).
- **Safe cache export**: cache export to GHA includes `ignore-error=true` so cache quota/eviction issues (e.g. “failed to reserve cache”) **do not fail** your workflow.
- **Optional Quay retention** via `quay_expires_after` (no default label unless you set it).

## Recommended usage patterns

### Push on main with automatic branch tag, plus SHA tags everywhere

```yaml
- uses: zebbra/actions/docker-build@v2
  with:
    image_name: ghcr.io/your-org/your-image
    push: ${{ github.ref_name == 'main' }}
    include_tag_sha: "true"
    branch_tags_on_push: "true"
```

### Add `latest` only when check-semver says the tag is latest stable

```yaml
- uses: zebbra/actions/check-semver@v6
  id: semver

- uses: zebbra/actions/docker-build@v2
  with:
    image_name: ghcr.io/your-org/your-image
    push: "true"
    tags: |
      type=raw,value=${{ steps.semver.outputs.version }}
    include_tag_latest: "${{ steps.semver.outputs.is_latest == 'true' }}"
    branch_tags_on_push: "false"
```

### Beta tags: no special tags and expire after 20d

```yaml
- uses: zebbra/actions/check-semver@v6
  id: semver

- uses: zebbra/actions/docker-build@v2
  with:
    image_name: ghcr.io/your-org/your-image
    push: "true"
    tags: |
      type=raw,value=${{ steps.semver.outputs.version }}
    include_tag_latest: "false"
    branch_tags_on_push: "false"
    quay_expires_after: "20d"
```

### Reduce cache pressure when repos hit cache quota

If your org frequently hits cache limits, set `cache_to_mode` to `min` (smaller exports) while keeping the safety net:

```yaml
- uses: zebbra/actions/docker-build@v2
  with:
    image_name: ghcr.io/your-org/your-image
    push: "true"
    include_tag_sha: "true"
    cache_to_mode: "min"
```

## Cache behavior and knobs

- **Import (`cache-from`)**:
  - If `cache_from_overrides` is set, it is used verbatim and **disables** `smart_cache`.
  - With `smart_cache=true`, the action attempts cache scopes in this order:
    - `main` only when building `main`
    - `develop` then `main` when building `develop`
    - otherwise: `head`, `base` (if different), then `develop`, then `main`
- **Export (`cache-to`)**:
  - Always exports to GHA cache using `scope=${cache_write_scope or head-branch}`.
  - Uses `mode=${{ inputs.cache_to_mode }}` (`max` by default).
  - Includes `ignore-error=true` so export failures don’t fail the job.

## Tagging & push safety

- When `push: "true"`, the action **fails fast** if no tags are generated. Enable at least one of:
  - `include_tag_sha`, `include_tag_ref`, `include_tag_latest`, `branch_tags_on_push`, or provide custom `tags`.
- Custom `tags` are appended verbatim to `docker/metadata-action`’s `tags` input (one rule per line).
- `branch_tags_on_push` only applies on branch pushes and auto-adds:
  - `main` when branch is `main`
  - `develop` when branch is `develop`
- `include_tag_latest` is a plain toggle; keep SemVer/latest decisioning in your workflow (for example using `check-semver`).
- `quay_expires_after` is optional; when empty, no `quay.expires-after` label is set.

## Common gotchas

- **`platforms` + `load`**: Docker Buildx does not support `load` with multi-platform builds. If you set `platforms`, keep `load: "false"`.
- **Cache export isn’t guaranteed**: cache export is best-effort by design (to avoid workflow failures). If you rely on cache for speed, monitor build times and consider `cache_to_mode: "min"` or adjusting `cache_write_scope`.
- **Branch names in cache scope**: branch names have `/` replaced with `-` for the cache scope to keep scopes valid.
