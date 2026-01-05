#!/usr/bin/env python3
"""
generate_action_docs.py - Generate documentation for GitHub Actions.

This script generates markdown documentation for actions by:
1. Parsing action.yml for inputs, outputs, and metadata
2. Including optional DOCS.md content from the action folder
3. Generating both versioned docs (docs/<action>/vN.md) and action README

Usage:
    python generate_action_docs.py <action> <version>

Example:
    python generate_action_docs.py check-semver v2
"""

import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    print("PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    raise

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def escape_pipes(value: Any) -> str:
    """Escape pipe characters for markdown tables."""
    text = str(value) if value is not None else ""
    return text.replace("|", "\\|")


def load_action_yaml(action_name: str) -> Dict[str, Any]:
    """Load and parse the action.yml file."""
    action_path = Path(action_name) / "action.yml"
    if not action_path.is_file():
        raise FileNotFoundError(f"Missing action.yml at {action_path}")
    with action_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_docs_md(action_name: str) -> Optional[str]:
    """
    Load optional DOCS.md from the action folder.

    If DOCS.md exists, its content is loaded and H1 headings are stripped
    to avoid conflicting with the generated document structure.

    Args:
        action_name: Name of the action (folder name).

    Returns:
        Processed DOCS.md content, or None if file doesn't exist.
    """
    docs_path = Path(action_name) / "DOCS.md"
    if not docs_path.is_file():
        logger.debug(f"No DOCS.md found at {docs_path}")
        return None

    logger.info(f"📄 Including DOCS.md from {docs_path}")
    content = docs_path.read_text(encoding="utf-8")

    # Strip H1 headings (lines starting with single #)
    # This prevents title conflicts in the generated documentation
    lines = content.split("\n")
    filtered_lines = []
    for line in lines:
        # Match lines that start with exactly one # followed by space (H1)
        if re.match(r"^#\s+", line) and not re.match(r"^##", line):
            logger.debug(f"Stripping H1 heading: {line[:50]}...")
            continue
        filtered_lines.append(line)

    return "\n".join(filtered_lines).strip()


def render_markdown(
    action_name: str,
    version: str,
    data: Dict[str, Any],
    docs_content: Optional[str] = None,
    repo_web_base: Optional[str] = None,
) -> str:
    """
    Render the complete markdown documentation.

    Args:
        action_name: Name of the action.
        version: Version string (e.g., 'v2').
        data: Parsed action.yml data.
        docs_content: Optional content from DOCS.md to include.
        repo_web_base: Optional GitHub repository base URL.

    Returns:
        Complete markdown documentation string.
    """
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
    lines.append(f"      - uses: zebbra/actions/{action_name}@{action_name}/{version}")
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

    # Include DOCS.md content after Outputs (if available)
    if docs_content:
        lines.append("")
        lines.append(docs_content)

    # Technical
    lines.append("")
    lines.append("## Technical")
    lines.append(f"- runs.using: `{using}`")
    if repo_web_base:
        action_web = (
            f"{repo_web_base}/blob/{action_name}/{version}/{action_name}/action.yml"
        )
        lines.append(f"- action path: [{action_name}/action.yml]({action_web})")
    else:
        action_rel = f"../../{action_name}/action.yml"
        lines.append(f"- action path: [{action_name}/action.yml]({action_rel})")

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
    action_root = Path(action_name)
    file_paths = []
    if action_root.exists():
        for p in sorted(action_root.rglob("*")):
            if p.is_file():
                # Skip DOCS.md from file listing (it's internal)
                if p.name == "DOCS.md":
                    continue
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
    """Append links to other versions at the end of the documentation."""
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


def get_repo_web_base() -> Optional[str]:
    """Determine the GitHub repository web base URL from git remote."""
    try:
        remote_url = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"], text=True
        ).strip()
        if remote_url:
            if remote_url.startswith("git@github.com:"):
                path = remote_url[len("git@github.com:") :]
                if path.endswith(".git"):
                    path = path[:-4]
                return f"https://github.com/{path}"
            elif "github.com/" in remote_url:
                # Handles https://github.com/<owner>/<repo>[.git]
                path = remote_url.split("github.com/")[-1]
                if path.endswith(".git"):
                    path = path[:-4]
                return f"https://github.com/{path}"
    except Exception:
        pass
    return None


def main() -> int:
    """Main entry point for documentation generation."""
    if len(sys.argv) != 3:
        print("Usage: generate_action_docs.py <action> <version>", file=sys.stderr)
        return 2

    action = sys.argv[1]
    version = sys.argv[2]

    logger.info(f"🔧 Generating documentation for {action}/{version}")

    # Load action.yml
    data = load_action_yaml(action)

    # Load optional DOCS.md
    docs_content = load_docs_md(action)

    # Determine repository web base
    repo_web_base = get_repo_web_base()

    # Render markdown content
    content = render_markdown(action, version, data, docs_content, repo_web_base)

    # Write versioned documentation to docs/<action>/vN.md
    out_dir = Path("docs") / action
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{version}.md"
    out_path.write_text(content, encoding="utf-8")

    # Add other versions links
    add_other_versions_links(out_path, action, version)

    logger.info(f"✅ Generated versioned docs at {out_path}")

    # Generate action README.md (copy of generated docs for GitHub display)
    readme_path = Path(action) / "README.md"

    # Re-read the final content (includes "Other versions" section)
    final_content = out_path.read_text(encoding="utf-8")
    readme_path.write_text(final_content, encoding="utf-8")

    logger.info(f"✅ Generated action README at {readme_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
