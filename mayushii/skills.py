"""Skill selection and injection — LLM picks skills, symlinks inject them.

Skills come from an external repo (e.g. cbx1/skills). Each skill has a SKILL.md
with frontmatter (name, description) and a body (instructions for the agent).

The orchestrator agent or the CLI can ask an LLM to select relevant skills
for a given task, then symlink them into the workspace's .claude/skills/ directory
so Claude Code auto-loads them.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


SKILLS_REPO_DEFAULT = Path.home() / ".mayushii" / "skills"


@dataclass
class SkillMeta:
    name: str
    description: str
    path: Path
    has_scripts: bool = False
    has_references: bool = False


def discover_skills_repo() -> Path:
    env = os.environ.get("MAYUSHII_SKILLS_REPO")
    if env:
        return Path(env)
    return SKILLS_REPO_DEFAULT


def load_catalog(skills_repo: Path | None = None) -> list[SkillMeta]:
    """Read all SKILL.md frontmatter — lightweight, just name + description."""
    repo = skills_repo or discover_skills_repo()
    if not repo.exists():
        return []

    catalog: list[SkillMeta] = []
    frontmatter_re = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)

    for skill_dir in sorted(repo.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        content = skill_file.read_text(errors="ignore")
        match = frontmatter_re.match(content)
        if not match:
            continue

        try:
            fm = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            continue

        catalog.append(SkillMeta(
            name=fm.get("name", skill_dir.name),
            description=fm.get("description", ""),
            path=skill_dir,
            has_scripts=(skill_dir / "scripts").is_dir(),
            has_references=(skill_dir / "references").is_dir(),
        ))

    return catalog


def format_catalog_for_llm(catalog: list[SkillMeta]) -> str:
    """Format catalog as a compact string for the LLM skill-selection call."""
    lines = []
    for s in catalog:
        flags = []
        if s.has_scripts:
            flags.append("scripts")
        if s.has_references:
            flags.append("refs")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"- {s.name}: {s.description}{suffix}")
    return "\n".join(lines)


def select_skills_via_llm(
    task_description: str,
    role: str,
    catalog: list[SkillMeta],
    max_skills: int = 4,
) -> list[str]:
    """Use a cheap LLM call to pick relevant skills for a task.

    Returns a list of skill names.
    Falls back to empty list if the API call fails.
    """
    try:
        import anthropic
    except ImportError:
        print("[mayushii] WARNING: anthropic SDK not installed, cannot select skills via LLM", file=sys.stderr)
        return []

    catalog_text = format_catalog_for_llm(catalog)
    if not catalog_text:
        return []

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": (
                    f"You are selecting skills for an AI agent.\n\n"
                    f"Task: {task_description}\n"
                    f"Agent role: {role}\n"
                    f"Max skills: {max_skills}\n\n"
                    f"Available skills:\n{catalog_text}\n\n"
                    f"Return ONLY a JSON array of skill names. Example: [\"debug\", \"backend\"]\n"
                    f"Pick only skills directly relevant to this task and role."
                ),
            }],
        )
    except Exception as e:
        print(f"[mayushii] WARNING: LLM skill selection failed: {e}", file=sys.stderr)
        return []

    text = response.content[0].text.strip()
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if not match:
        print(f"[mayushii] WARNING: LLM returned non-JSON skill selection: {text[:100]}", file=sys.stderr)
        return []

    try:
        names = json.loads(match.group())
        valid = {s.name for s in catalog}
        selected = [n for n in names if n in valid][:max_skills]
        rejected = [n for n in names if n not in valid]
        if rejected:
            print(f"[mayushii] WARNING: LLM selected unknown skills (ignored): {rejected}", file=sys.stderr)
        return selected
    except (json.JSONDecodeError, TypeError):
        return []


def inject_skills(workspace: Path, skill_names: list[str], skills_repo: Path | None = None) -> list[Path]:
    """Symlink skills into workspace/.claude/skills/ so Claude Code auto-loads them."""
    repo = skills_repo or discover_skills_repo()
    skills_dir = workspace / ".claude" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    injected: list[Path] = []
    repo_resolved = repo.resolve()
    for name in skill_names:
        src = repo / name
        if not src.exists():
            print(f"[mayushii] WARNING: skill '{name}' not found at {src}", file=sys.stderr)
            continue
        # Validate path stays within skills repo (prevent traversal)
        if not src.resolve().is_relative_to(repo_resolved):
            print(f"[mayushii] WARNING: skill '{name}' escapes skills repo, skipping", file=sys.stderr)
            continue
        dst = skills_dir / name
        if not dst.resolve().parent == skills_dir.resolve() or ".." in name:
            print(f"[mayushii] WARNING: skill '{name}' would escape skills dir, skipping", file=sys.stderr)
            continue
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        dst.symlink_to(src)
        injected.append(dst)

    return injected


def remove_skills(workspace: Path) -> None:
    """Clean up injected skill symlinks."""
    skills_dir = workspace / ".claude" / "skills"
    if not skills_dir.exists():
        return
    for item in skills_dir.iterdir():
        if item.is_symlink():
            item.unlink()
