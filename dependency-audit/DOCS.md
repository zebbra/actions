## How it works

The action runs the `dependency-audit` CLI from this folder (stdlib-only Python, started with `uvx`). It audits every
target listed in `.github/dependency-audit.toml`:

- **uv targets**: `uv export --format pylock.toml --frozen --no-emit-local`, then the pinned `pip-audit --locked`.
- **npm targets**: `npm audit --package-lock-only` against `https://registry.npmjs.org/`. Nothing is installed.

| Mode | Fails (exit 1) on | Used by |
|---|---|---|
| `check` | never; exit 2 when a tracked `uv.lock` / `package-lock.json` is neither a target nor excluded | every other mode runs it first |
| `diff` | findings in the PR's result that are absent at the base | `pull_request`, `push` |
| `full` | any non-ignored finding in a `fail-on = "any"` target | release tags, `make dependency-audit` |
| `fix` | never; tool errors go to the triage issue | `schedule`, `workflow_dispatch` |

Exit code 2 is a tool or config error (export failed, invalid JSON, network error after three retries with 2s, 8s
and 30s backoff). It is reported as "tool error, not a finding".

## Config file

One `.github/dependency-audit.toml` per repository. Unknown keys, a missing `reason` or duplicate target names fail
with exit 2.

```toml
[[target]]
name = "backend"              # unique; used in reports and commit messages
ecosystem = "uv"              # uv | npm
path = "backend"              # directory containing the lockfile
scope = "runtime"             # runtime | all   (default runtime)
groups = []                   # uv only: extra dependency groups that ship
fail-on = "any"               # any | none      (full mode only; diff mode always gates)

[[exclude]]                   # lockfiles deliberately not audited
glob = "examples/**"
reason = "Example projects are not shipped."

[[ignore]]
id = "GHSA-xxxx-xxxx-xxxx"    # matched against the advisory id and all its aliases
package = "python-jose"       # optional, narrows the match
reason = "Not reachable: … (caused-by: …; fix-when: …)"
issue = "zebbra/neops-core#2239"   # optional
```

- `scope = "runtime"` audits what the artifact ships: uv exports with `--no-dev` plus one `--group` per `groups`
  entry, npm audits with `--omit=dev`. `scope = "all"` adds every group and extra (uv) or dev dependencies (npm).
- `.make_scripts/**` is excluded by default. Globs use `**` for any depth and `*` within one directory.
- Ignore ids: GHSA ids match both ecosystems. PYSEC and CVE ids match uv findings only, because `npm audit` reports
  carry a GHSA id and a numeric advisory id. A CVE ignore aimed at an npm package shows up as an unused ignore.
- An ignore without `package` hides the advisory in every package and target.

## Caller workflow

```yaml
name: Dependency audit
on:
  pull_request:
  push: { branches: [develop, main] }
  schedule: [{ cron: "17 3 * * 1-5" }]
  workflow_dispatch:
permissions: { contents: read }
jobs:
  gate:
    name: gate
    if: github.event_name == 'pull_request' || github.event_name == 'push'
    runs-on: ubuntu-latest
    concurrency: { group: "dep-audit-gate-${{ github.ref }}", cancel-in-progress: "${{ github.event_name == 'pull_request' }}" }
    steps:
      - uses: actions/checkout@v5
        with: { fetch-depth: 2 }
      - uses: zebbra/actions/dependency-audit@dependency-audit/latest
  fix:
    name: fix
    if: github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-latest
    concurrency: { group: "dep-audit-fix-${{ github.repository }}", cancel-in-progress: false }
    permissions: { contents: read, issues: write }   # writes go through the App token
    steps:
      - uses: actions/checkout@v5
        with: { ref: "${{ github.event.repository.default_branch }}" }
      - uses: zebbra/actions/dependency-audit@dependency-audit/latest
        with:
          app-id: ${{ vars.DEPS_BOT_APP_ID }}
          app-private-key: ${{ secrets.DEPS_BOT_PRIVATE_KEY }}
          npm-token: ${{ secrets.NPM_TOKEN }}
```

Release gate, as a step in the tag workflow:

```yaml
- uses: zebbra/actions/dependency-audit@dependency-audit/latest
  with: { mode: full }
```

Keep `mode: full` explicit there: `auto` maps `workflow_dispatch` to `fix`.

## Diff gate

- On `pull_request` the checkout is the merge commit, so the base is `HEAD^1` (the base-branch tip). On `push` it is
  `github.event.before`; a first push to a new branch (all-zero `before`) runs `full`.
- When no target lockfile and no config changed between base and head, the gate passes with "no dependency change"
  and queries no advisory database.
- Otherwise it audits the head and a worktree of the base with the head's targets. A head finding is new when no base
  finding of the same target and package shares an id with it, so version bumps, re-keyed ids (PYSEC to GHSA) and
  advisories published after the base was cut never fail a PR.
- Only ignores present in both the base and the head config (same `id` and `package`) apply. A PR that adds a
  vulnerable version and its ignore together fails; land the ignore in its own PR first.
- `fail-on` does not apply: a PR never introduces a finding, also in report-only targets.

## Nightly bot (fix mode)

1. Audits every target with `scope = all`, including ignored findings.
2. uv: per package, upgrades to the highest of the lowest fixed versions of its advisories
   (`uv lock --upgrade-package pkg==version`), falls back once to the newest allowed version, and otherwise records
   the package as blocked with uv's explanation of the conflict (or its dependents from `uv tree --invert`).
   A `uv lock` failure that is not a resolution conflict (network, index) is a tool error for the target.
   One commit per package: `fix(deps): bump <pkg> <from> -> <to> (<ids>)`.
3. npm: `npm audit fix --package-lock-only --ignore-scripts` (no `--force`, so it stays within semver ranges); when
   it exits non-zero (ERESOLVE, an npm crash) or changes `package.json` it reverts and runs `npm update <pkg>` per
   package, reverting each failed update the same way. One commit per target.
4. Re-audits and sorts findings into fixed, blocked, no fix, ignored but now fixable, and unused ignores.
5. Opens or force-updates one PR from `bot/dependency-audit` to the default branch, labelled `pr-security`;
   reviewers come from CODEOWNERS. The branch is rebuilt from the default branch on every run and deleted when it
   has no diff.
6. Keeps one open issue labelled `dependency-audit` (found by its `<!-- dependency-audit:triage -->` marker) with
   blocked and unfixable findings, stale ignores and tool errors; it is closed when empty. The action creates the
   `dependency-audit` and `pr-security` labels when they are missing (colours as in project-infrastructure).

Bot commits touch lockfiles only; the CLI refuses to commit when anything else changed. Ignore entries,
`pyproject.toml` and `package.json` are never edited.

Reports carry ids, advisory links, package names and versions only. Advisory titles and descriptions are left out,
and `@` and `#` are escaped, so nothing mentions people or cross-links issues. Bodies are capped at 65536 characters.

### GitHub App

The PR is opened with a GitHub App token so that it triggers CI. The App needs Contents, Pull requests, Issues and
Workflows (read and write) and Metadata (read): the rebuilt bot branch carries every workflow change of the default
branch. Without `app-id` and `app-private-key` the bot runs dry: lockfiles are restored, nothing is pushed, and the
triage issue (written with `GITHUB_TOKEN`, so the job needs `issues: write`) lists the fixable findings under
"bot not configured".

## Local use

```bash
uvx --from "git+https://github.com/zebbra/actions@dependency-audit/latest#subdirectory=dependency-audit" dependency-audit full
uvx --from "git+https://github.com/zebbra/actions@dependency-audit/latest#subdirectory=dependency-audit" dependency-audit fix
```

`full` matches the release gate. `fix` upgrades the lockfiles in place without committing; add `--dry-run` to only
report. `diff --base <rev>` reproduces the PR gate. `--json-out`, `--summary-out`, `--pr-body-out` and
`--issue-body-out` write the reports to files.

## Gotchas

- GitHub disables scheduled workflows after 60 days without repository activity; an open bot PR does not count.
  Re-enable the workflow in the Actions tab.
- Repositories created from a template inherit the config, the caller and the CODEOWNERS lines; their bot runs dry
  until the App is installed there.
- `npm audit` always queries `registry.npmjs.org`, because GitHub Packages has no audit endpoint. `npm-token` is
  only needed when `npm audit fix` or `npm update` resolves `@zebbra/*` packages. When `NPM_TOKEN` is set, the CLI
  passes those commands a temporary npm userconfig (`@zebbra:registry=https://npm.pkg.github.com` plus an auth line
  that reads `${NPM_TOKEN}` from the environment) in place of `~/.npmrc`, so no `.npmrc` or `setup-node` is needed.
  A registry or auth error (no token, a rejected token) is a tool error in the triage issue.
- Most npm fixes need a semver-major bump. They show up as blocked with the version npm proposes.
- Supported lockfiles: `uv.lock` and `package-lock.json`. yarn v1, poetry and Pipfile lockfiles are not audited.
- Tool pins (`pip-audit`, the npm registry) live in `dependency_audit/__init__.py`; the uv version is pinned in
  `action.yml`.
