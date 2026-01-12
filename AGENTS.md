# Agent instructions

## Do not edit generated docs

The following files are auto-generated on release and **must not** be edited manually:

- `docs/**` (e.g. `docs/<action>/vN.md`)
- `<action>/README.md` for each action folder
- The root `README.md` section between `<!-- LATEST_TAGS_START -->` and `<!-- LATEST_TAGS_END -->`

CI enforces this on pull requests.

## Action structure guidelines

- Prefer marketplace actions and composite `action.yml` (YAML-first) over custom code.
- Use bash in `run:` steps for small glue logic (parsing, gating, formatting) before reaching for Python.
- Only add custom code when it meaningfully improves correctness or reduces complexity; keep it small, well-logged, and avoid unnecessary dependencies.
- Keep action docs in `<action>/DOCS.md` only (narrative/gotchas). Generated docs are overwritten on release.

## Writing new agent rules / Cursor rules

- `AGENTS.md` is the **source of truth** for repo-wide guidelines (human-friendly, reviewable in PRs).
- `.cursor/rules/*.mdc` are **Cursor-enforced** rules that are automatically injected into the agent context.
- Keep `AGENTS.md` and Cursor rules **in sync**:
  - When adding/changing repo-wide guidelines in `AGENTS.md`, also add/update a corresponding Cursor rule (short, non-duplicative).
  - When adding/changing a Cursor rule, also add/update the corresponding section in `AGENTS.md` (source of truth).
- Keep Cursor rules **short and non-duplicative** to avoid context bloat: prefer “hard guardrails + pointer to `AGENTS.md`”.
- Prefer **one concern per rule file** (e.g. docs policy vs action structure) and name files accordingly.
- Use `alwaysApply: true` only for rules that should apply in every session; otherwise keep them scoped/minimal.

## Where to write documentation

- Add hand-written action documentation only in optional `<action>/DOCS.md`.
- Keep `DOCS.md` concise and focused on narrative/examples/gotchas/migrations.
- Do **not** include or duplicate auto-generated sections in `DOCS.md`: title/description, quick usage snippet, inputs table, outputs table, technical `runs.*` info, referenced actions, files list, other versions links.
- Avoid H1 (`# ...`) headings in `DOCS.md` (the generator strips H1 lines).

## How docs are generated (source of truth)

- Trigger: `.github/workflows/release-action.yml`
- Generator: `.github/workflows/scripts/generate_action_docs.py <action> <version>`
- Sources: `action.yml` (+ optional `DOCS.md`)
- Writes: `docs/<action>/<version>.md` and `<action>/README.md`



