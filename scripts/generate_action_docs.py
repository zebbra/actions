#!/usr/bin/env python3

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional
import subprocess

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    print("PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    raise


def escape_pipes(value: Any) -> str:
    text = str(value) if value is not None else ""
    return text.replace("|", "\\|")


def load_action_yaml(action_name: str) -> Dict[str, Any]:
    action_path = Path(".github/actions") / action_name / "action.yml"
    if not action_path.is_file():
        raise FileNotFoundError(f"Missing action.yml at {action_path}")
    with action_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def render_markdown(
    action_name: str,
    version: str,
    data: Dict[str, Any],
    repo_web_base: Optional[str] = None,
) -> str:
    name = data.get("name", action_name)
    desc = data.get("description", "")
    runs = data.get("runs", {}) or {}
    using = runs.get("using", "composite")
    steps = runs.get("steps", []) or []
    inputs = data.get("inputs") or {}
    outputs = data.get("outputs") or {}

    lines = []
    lines.append(f"# `{action_name}/{version}` - {name}")
    if desc:
        lines.append("")
        lines.append(desc)

    # Quick usage example
    lines.append("")
    lines.append("## Quick usage")
    lines.append("```yaml")
    lines.append("jobs:")
    lines.append("  example:")
    lines.append("    runs-on: ubuntu-latest")
    lines.append("    steps:")
    lines.append("      - uses: actions/checkout@v4")
    lines.append(
        f"      - uses: zebbra/actions/.github/actions/{action_name}@{action_name}/{version}"
    )
    lines.append("      # with:")
    lines.append("      #   <input_name>: <value>")
    lines.append("```")

    # Inputs
    lines.append("")
    lines.append("## Inputs")
    if inputs:
        lines.append("| Name | Required | Default | Description |")
        lines.append("| ---- | -------- | ------- | ----------- |")
        for key, meta in inputs.items():
            meta = meta or {}
            req = str(meta.get("required", False)).lower()
            default = meta.get("default", "")
            desc_i = meta.get("description", "")
            if isinstance(desc_i, str) and ("\n" in desc_i):
                # Normalize and join lines to avoid breaking markdown tables
                parts = [ln.strip() for ln in desc_i.splitlines() if ln.strip()]
                desc_i = " ".join(parts)
            name_cell = f"`{escape_pipes(key)}`"
            default_cell = f"`{escape_pipes(default)}`" if default != "" else ""
            lines.append(
                f"| {name_cell} | {escape_pipes(req)} | {default_cell} | {escape_pipes(desc_i)} |"
            )
    else:
        lines.append("(none)")

    # Outputs
    lines.append("")
    lines.append("## Outputs")
    if outputs:
        lines.append("| Name | Description |")
        lines.append("| ---- | ----------- |")
        for key, meta in outputs.items():
            desc_o = meta.get("description", "") if isinstance(meta, dict) else ""
            name_cell = f"`{escape_pipes(key)}`"
            lines.append(f"| {name_cell} | {escape_pipes(desc_o)} |")
    else:
        lines.append("(none)")

    # Technical
    lines.append("")
    lines.append("## Technical")
    lines.append(f"- runs.using: `{using}`")
    if repo_web_base:
        action_web = f"{repo_web_base}/blob/{action_name}/{version}/.github/actions/{action_name}/action.yml"
        lines.append(
            f"- action path: [.github/actions/{action_name}/action.yml]({action_web})"
        )
    else:
        action_rel = f"../../.github/actions/{action_name}/action.yml"
        lines.append(
            f"- action path: [.github/actions/{action_name}/action.yml]({action_rel})"
        )

    # Referenced actions
    lines.append("")
    lines.append("### Referenced actions")
    referenced = []
    for s in steps:
        if isinstance(s, dict) and "uses" in s:
            referenced.append(str(s["uses"]))
    if referenced:
        for r in referenced:
            lines.append(f"- `{r}`")
    else:
        lines.append("(none)")

    # Files list (link to all files under the action directory)
    lines.append("")
    lines.append("### Files")
    action_root = Path(".github/actions") / action_name
    file_paths = []
    if action_root.exists():
        for p in sorted(action_root.rglob("*")):
            if p.is_file():
                if repo_web_base:
                    repo_path = p.as_posix()
                    web_link = (
                        f"{repo_web_base}/blob/{action_name}/{version}/{repo_path}"
                    )
                    file_paths.append((str(p), web_link))
                else:
                    rel_link = Path("../../") / p
                    file_paths.append((str(p), str(rel_link)))
    if file_paths:
        for display, link in file_paths:
            lines.append(f"- [{display}]({link})")
    else:
        lines.append("(none)")

    return "\n".join(lines) + "\n"


def add_other_versions_links(doc_path: Path, action_name: str, version: str) -> None:
    folder = doc_path.parent
    versions = []
    if folder.is_dir():
        for p in folder.glob("v*.md"):
            base = p.stem
            if base != version:
                try:
                    _ = int(base[1:])
                    versions.append(base)
                except Exception:
                    continue
    versions = sorted(set(versions), key=lambda s: int(s[1:]))

    with doc_path.open("a", encoding="utf-8") as f:
        f.write("\n## Other versions\n")
        if versions:
            for v in versions:
                f.write(f"- [{v}](./{v}.md)\n")
        else:
            f.write("(none)\n")


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: generate_action_docs.py <action> <version>", file=sys.stderr)
        return 2
    action = sys.argv[1]
    version = sys.argv[2]

    data = load_action_yaml(action)

    # Determine repository web base (e.g., https://github.com/<owner>/<repo>) from git remote
    repo_web_base: Optional[str] = None
    try:
        remote_url = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"], text=True
        ).strip()
        if remote_url:
            if remote_url.startswith("git@github.com:"):
                path = remote_url[len("git@github.com:") :]
                if path.endswith(".git"):
                    path = path[:-4]
                repo_web_base = f"https://github.com/{path}"
            elif "github.com/" in remote_url:
                # Handles https://github.com/<owner>/<repo>[.git]
                path = remote_url.split("github.com/")[-1]
                if path.endswith(".git"):
                    path = path[:-4]
                repo_web_base = f"https://github.com/{path}"
    except Exception:
        repo_web_base = None

    content = render_markdown(action, version, data, repo_web_base)

    out_dir = Path("docs") / action
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{version}.md"
    out_path.write_text(content, encoding="utf-8")

    add_other_versions_links(out_path, action, version)

    print(f"Generated docs at {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
