## What it does

This action creates a GitHub Release for the current ref **only if** the ref name is an eligible tag.

## Tag eligibility

- Stable tags (default): `vMAJOR.MINOR.PATCH` (example: `v1.0.0`)
- Beta tags (optional): `vMAJOR.MINOR.PATCH-beta.N` (example: `v1.0.0-beta.1`)

By default, beta tags are skipped. If `release_beta_tags` is enabled, beta tags are released and marked as prereleases.

## Non-matching refs

If the current `GITHUB_REF_NAME` is not an eligible tag, the action **does nothing and exits successfully** (with a short info log explaining why).


