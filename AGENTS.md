# Agent instructions (documentation policy)

## Do not edit generated docs

The following files are auto-generated on release and **must not** be edited manually:

- `docs/**` (e.g. `docs/<action>/vN.md`)
- `<action>/README.md` for each action folder
- The root `README.md` section between `<!-- LATEST_TAGS_START -->` and `<!-- LATEST_TAGS_END -->`

CI enforces this on pull requests.

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


