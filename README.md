# actions
Zebbra internal GitHub Actions & Workflows

Private source-of-truth for reusable GitHub Actions within the `zebbra` organization.

## Usage

Reference actions by namespaced tags:

```yaml
steps:
  - uses: actions/checkout@v4
  - name: Check SemVer
    uses: zebbra/actions/check-semver@check-semver/v2
  - name: Build image (no push)
    uses: zebbra/actions/docker-build@docker-build/v1
    with:
      image_name: ghcr.io/zebbra/my-service
      context: .
      push: false
```

Notes:
- Use `<action>/latest` for a moving pointer, or `<action>/vN` for a fixed version.
- Pushing Docker images requires `permissions: { contents: read, packages: write }`.

## Creating Actions

Each action lives in its own folder at the repository root:

```
<action-name>/
├── action.yml          # Required: Action definition
├── DOCS.md             # Optional: Extended documentation (included in generated docs)
├── README.md           # Auto-generated: Do not edit manually
└── ...                 # Other action files (scripts, requirements, etc.)
```

### Action Structure

1. **`action.yml`** (required): Standard GitHub Action definition with:
   - `name`: Display name
   - `description`: Brief description
   - `inputs`: Action inputs with descriptions and defaults
   - `outputs`: Action outputs
   - `runs`: Execution configuration

2. **`DOCS.md`** (optional): Extended documentation that gets included in generated docs. Use this for:
   - Detailed usage examples
   - Configuration explanations
   - Migration guides
   - Example outputs
   
   **Note**: H1 headings (`# Title`) are automatically stripped to avoid conflicts with the generated title.

3. **`README.md`** (auto-generated): Created by the documentation generator. **Do not edit manually** - changes will be overwritten.

## Documenting Actions

Documentation is auto-generated using `.github/workflows/scripts/generate_action_docs.py`.

### What Gets Generated

The generator creates documentation from multiple sources:

| Content | Source |
|---------|--------|
| Title & description | `action.yml` |
| Quick usage example | Auto-generated |
| Inputs table | `action.yml` inputs |
| Outputs table | `action.yml` outputs |
| Extended documentation | `DOCS.md` (if exists) |
| Technical info | `action.yml` runs config |
| File listing | Directory scan |
| Other versions | `docs/<action>/` scan |

### Generated Files

When documentation is generated, two files are created:

1. **`docs/<action>/vN.md`**: Versioned documentation
2. **`<action>/README.md`**: Copy for GitHub display when browsing the action folder

### Running the Generator

```bash
# Install dependencies
pip install pyyaml

# Generate docs for an action
python .github/workflows/scripts/generate_action_docs.py <action> <version>

# Example
python .github/workflows/scripts/generate_action_docs.py check-semver v2
```

## Releasing

Manual only:
- Run the `Release actions` workflow and choose an action.
- Provide `version` like `v3` to set explicitly, or leave empty to auto-derive the next integer (starts at `v0`).

Each release creates `<action>/vN` and moves `<action>/latest`.

## Latest versions
<!-- LATEST_TAGS_START -->
| Action | Latest version | Documentation |
| ------ | -------------- | ------------- |
| check-semver | [check-semver/v1](docs/check-semver/v1.md) | [Documentation](docs/check-semver/v1.md) |
| docker-build | (none) | (n/a) |
| enforce-pr-label | [enforce-pr-label/v1](docs/enforce-pr-label/v1.md) | [Documentation](docs/enforce-pr-label/v1.md) |
<!-- LATEST_TAGS_END -->
