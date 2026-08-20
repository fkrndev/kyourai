"""Skills system — SKILL.md loader with frontmatter, gating, and allowlists.

Ported from OpenClaw's skills concept, adapted for Kyourai.

Skills are markdown instruction files that teach the agent how and when to
use tools. Each skill lives in a directory containing a SKILL.md file with
YAML frontmatter and a markdown body.

Loading order (highest precedence first):
  1. Workspace skills: <workspace>/skills/
  2. Personal skills:  ~/.kyourai/skills/
  3. Bundled skills:   shipped with kyourai

Gating:
  - requires.bins: all binaries must exist on PATH
  - requires.anyBins: at least one binary must exist
  - requires.env: each env var must exist
  - os: platform filter (darwin, linux, win32)
  - always: include even when requires.* checks fail

Skills with disable-model-invocation: true stay out of the system prompt
but can still be invoked via /skill-name.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kyourai.constants import get_kyourai_home, get_skills_dir

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)
_MAX_SKILL_BODY_BYTES = 32_768  # 32KB — trim large skills
_MAX_SKILLS = 200


@dataclass
class Skill:
    """A loaded skill with metadata and body."""
    name: str
    description: str
    body: str
    source_path: Path
    user_invocable: bool = True
    disable_model_invocation: bool = False
    command_dispatch: str | None = None  # "tool" for direct tool dispatch
    command_tool: str | None = None
    command_arg_mode: str = "raw"
    homepage: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Gating
    requires_bins: list[str] = field(default_factory=list)
    requires_any_bins: list[str] = field(default_factory=list)
    requires_env: list[str] = field(default_factory=list)
    requires_config: list[str] = field(default_factory=list)
    os_filter: list[str] = field(default_factory=list)
    always: bool = False
    # Runtime
    eligible: bool = True
    skip_reason: str = ""

    @property
    def base_dir(self) -> Path:
        return self.source_path.parent

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "user_invocable": self.user_invocable,
            "disable_model_invocation": self.disable_model_invocation,
            "eligible": self.eligible,
            "skip_reason": self.skip_reason,
            "source": str(self.source_path),
        }


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from markdown content. Returns (metadata, body)."""
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as e:
        logger.warning("Failed to parse skill frontmatter: %s", e)
        return {}, content
    if not isinstance(meta, dict):
        return {}, content
    body = match.group(2).strip()
    return meta, body


def _check_binary(bin_name: str) -> bool:
    return shutil.which(bin_name) is not None


def _check_env(var_name: str) -> bool:
    return bool(os.environ.get(var_name))


def _get_platform() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "win32"
    return sys.platform


class SkillLoader:
    """Discovers, loads, and gates skills from multiple roots.

    Usage:
        loader = SkillLoader()
        loader.load_all()
        for skill in loader.eligible_skills():
            print(skill.name, skill.description)
        prompt = loader.build_prompt_block()
    """

    def __init__(
        self,
        workspace_skills_dir: Path | None = None,
        personal_skills_dir: Path | None = None,
        bundled_skills_dir: Path | None = None,
        allowlist: list[str] | None = None,
    ):
        self._workspace_dir = workspace_skills_dir
        self._personal_dir = personal_skills_dir or get_skills_dir()
        self._bundled_dir = bundled_skills_dir or Path(__file__).parent / "bundled"
        self._allowlist = set(allowlist) if allowlist is not None else None
        self._skills: dict[str, Skill] = {}  # name → Skill (deduped by precedence)
        self._platform = _get_platform()

    def load_all(self) -> dict[str, Skill]:
        """Load skills from all roots, applying precedence and gating.

        Returns the deduplicated skill map (name → Skill).
        """
        self._skills.clear()

        # Load in precedence order (lowest first, so higher overwrites)
        roots = [
            self._bundled_dir,            # bundled (lowest)
            self._personal_dir,           # personal (higher)
        ]
        if self._workspace_dir:
            roots.insert(0, self._workspace_dir)  # workspace (highest)

        for root in roots:
            if not root.exists():
                continue
            for skill in self._discover_root(root):
                self._skills[skill.name] = skill

        # Apply allowlist
        if self._allowlist is not None:
            self._skills = {
                name: skill for name, skill in self._skills.items()
                if name in self._allowlist
            }

        # Apply gating
        for skill in self._skills.values():
            self._apply_gating(skill)

        # Trim to max
        if len(self._skills) > _MAX_SKILLS:
            logger.warning("Too many skills (%d), trimming to %d", len(self._skills), _MAX_SKILLS)
            self._skills = dict(list(self._skills.items())[:_MAX_SKILLS])

        eligible = sum(1 for s in self._skills.values() if s.eligible)
        logger.info("Loaded %d skills (%d eligible)", len(self._skills), eligible)
        return self._skills

    def _discover_root(self, root: Path) -> list[Skill]:
        """Find all SKILL.md files under a root directory (up to 6 levels deep)."""
        skills = []
        for skill_md in root.rglob("SKILL.md"):
            if len(skills) >= _MAX_SKILLS:
                break
            skill = self._load_skill_file(skill_md)
            if skill is not None:
                skills.append(skill)
        return skills

    def _load_skill_file(self, path: Path) -> Skill | None:
        """Load a single SKILL.md file."""
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to read skill %s: %s", path, e)
            return None

        meta, body = _parse_frontmatter(content)
        name = meta.get("name") or path.parent.name
        description = meta.get("description", "")

        if not name:
            logger.warning("Skill %s has no name, skipping", path)
            return None

        # Validate name (lowercase, digits, hyphens)
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", name):
            logger.warning("Skill name '%s' is invalid (use lowercase, digits, hyphens)", name)
            return None

        # Trim body if too large
        if len(body) > _MAX_SKILL_BODY_BYTES:
            body = body[:_MAX_SKILL_BODY_BYTES] + "\n\n<!-- skill body trimmed -->\n"

        # Extract gating from metadata.openclaw.requires
        openclaw_meta = meta.get("metadata", {}).get("openclaw", {}) if isinstance(meta.get("metadata"), dict) else {}
        requires = openclaw_meta.get("requires", {}) if isinstance(openclaw_meta, dict) else {}

        skill = Skill(
            name=name,
            description=str(description),
            body=body,
            source_path=path,
            user_invocable=meta.get("user-invocable", True),
            disable_model_invocation=meta.get("disable-model-invocation", False),
            command_dispatch=meta.get("command-dispatch"),
            command_tool=meta.get("command-tool"),
            command_arg_mode=meta.get("command-arg-mode", "raw"),
            homepage=meta.get("homepage"),
            metadata=meta.get("metadata", {}) if isinstance(meta.get("metadata"), dict) else {},
            requires_bins=list(requires.get("bins", [])),
            requires_any_bins=list(requires.get("anyBins", [])),
            requires_env=list(requires.get("env", [])),
            requires_config=list(requires.get("config", [])),
            os_filter=list(meta.get("os", [])),
            always=bool(meta.get("always", False)),
        )
        return skill

    def _apply_gating(self, skill: Skill) -> None:
        """Check gating conditions and set skill.eligible."""
        # OS filter
        if skill.os_filter and self._platform not in skill.os_filter:
            skill.eligible = False
            skill.skip_reason = f"os filter {skill.os_filter} excludes {self._platform}"
            return

        if skill.always:
            skill.eligible = True
            return

        # requires.bins — ALL must exist
        for bin_name in skill.requires_bins:
            if not _check_binary(bin_name):
                skill.eligible = False
                skill.skip_reason = f"missing binary: {bin_name}"
                return

        # requires.anyBins — at least one must exist
        if skill.requires_any_bins:
            if not any(_check_binary(b) for b in skill.requires_any_bins):
                skill.eligible = False
                skill.skip_reason = f"missing all binaries: {skill.requires_any_bins}"
                return

        # requires.env — ALL must exist
        for var_name in skill.requires_env:
            if not _check_env(var_name):
                skill.eligible = False
                skill.skip_reason = f"missing env var: {var_name}"
                return

        skill.eligible = True

    def all_skills(self) -> list[Skill]:
        """Return all loaded skills (including ineligible)."""
        return list(self._skills.values())

    def eligible_skills(self) -> list[Skill]:
        """Return only eligible skills."""
        return [s for s in self._skills.values() if s.eligible]

    def model_visible_skills(self) -> list[Skill]:
        """Return eligible skills visible to the model (not disable-model-invocation)."""
        return [s for s in self.eligible_skills() if not s.disable_model_invocation]

    def user_invocable_skills(self) -> list[Skill]:
        """Return eligible, user-invocable skills."""
        return [s for s in self.eligible_skills() if s.user_invocable]

    def get_skill(self, name: str) -> Skill | None:
        """Get a skill by name. Returns None if not found or ineligible."""
        skill = self._skills.get(name)
        if skill and skill.eligible:
            return skill
        return None

    def build_prompt_block(self) -> str:
        """Build a system prompt block from all model-visible skills.

        Skills are listed with their name, description, and body instructions.
        """
        visible = self.model_visible_skills()
        if not visible:
            return ""

        lines = ["## Skills\n"]
        for skill in visible:
            lines.append(f"### {skill.name}")
            if skill.description:
                lines.append(f"*{skill.description}*\n")
            # Resolve {baseDir} references
            body = skill.body.replace("{baseDir}", str(skill.base_dir))
            lines.append(body)
            lines.append("")

        return "\n".join(lines)

    def build_skill_list_block(self) -> str:
        """Build a compact skill list for slash-command discovery."""
        visible = self.user_invocable_skills()
        if not visible:
            return "No skills available."
        lines = []
        for skill in visible:
            desc = skill.description[:80] if skill.description else ""
            lines.append(f"  /{skill.name} — {desc}")
        return "\n".join(lines)

    def resolve_reference(self, text: str) -> list[Skill]:
        """Resolve $skill-name references in text. Returns matched skills.

        Supports $name syntax (like OpenClaw). Common uppercase env vars
        ($HOME, $PATH) are NOT treated as skill references.
        """
        _ENV_VARS = {"$HOME", "$PATH", "$EDITOR", "$USER", "$SHELL", "$TERM"}
        skills = []
        for match in re.finditer(r"\$([a-z][a-z0-9-]*)", text):
            ref = match.group(0)
            if ref.upper() in _ENV_VARS:
                continue
            skill = self.get_skill(match.group(1))
            if skill:
                skills.append(skill)
        return skills
