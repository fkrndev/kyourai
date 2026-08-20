"""Coding context detection — detect git repo, language, framework, inject to system prompt.

When the agent starts in a directory, this module detects:
  - Git repository (branch, status, remote)
  - Primary language (Python, JavaScript, TypeScript, Rust, Go, etc.)
  - Framework (pytest, next.js, fastapi, express, django, etc.)
  - Package manager (pip, npm, yarn, pnpm, cargo, go)
  - Test framework
  - Linting/formatting tools

This context is injected into the system prompt so the agent knows what
kind of project the user is working in.

Usage:
  from kyourai.context.coding import detect_coding_context
  ctx = detect_coding_context()
  # → CodingContext(repo=True, branch="main", language="python", ...)
  prompt = ctx.to_prompt()
  # → "You are working in a Python project using pytest..."
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CodingContext:
    """Detected coding context for the current directory."""
    directory: str = ""
    is_git_repo: bool = False
    git_branch: str = ""
    git_status: str = ""  # "clean", "dirty", "unknown"
    git_remote: str = ""
    languages: list[str] = field(default_factory=list)
    primary_language: str = ""
    frameworks: list[str] = field(default_factory=list)
    package_managers: list[str] = field(default_factory=list)
    test_frameworks: list[str] = field(default_factory=list)
    linters: list[str] = field(default_factory=list)
    project_name: str = ""
    has_readme: bool = False
    has_tests: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_prompt(self) -> str:
        """Generate a system prompt section describing the coding context."""
        if not self.directory:
            return ""

        lines: list[str] = []

        # Project overview
        if self.project_name:
            lines.append(f"Project: {self.project_name}")
        lines.append(f"Working directory: {self.directory}")

        # Git info
        if self.is_git_repo:
            git_info = f"Git: branch={self.git_branch}"
            if self.git_status == "dirty":
                git_info += " (uncommitted changes)"
            elif self.git_status == "clean":
                git_info += " (clean)"
            if self.git_remote:
                # Show just the repo name from URL
                remote = self.git_remote.split("/")[-1].replace(".git", "")
                git_info += f", remote={remote}"
            lines.append(git_info)

        # Languages
        if self.languages:
            lang_str = ", ".join(self.languages[:3])
            lines.append(f"Languages: {lang_str}")

        # Frameworks
        if self.frameworks:
            lines.append(f"Frameworks: {', '.join(self.frameworks)}")

        # Package managers
        if self.package_managers:
            lines.append(f"Package managers: {', '.join(self.package_managers)}")

        # Test frameworks
        if self.test_frameworks:
            lines.append(f"Test frameworks: {', '.join(self.test_frameworks)}")

        # Linters
        if self.linters:
            lines.append(f"Linters/formatters: {', '.join(self.linters)}")

        if not lines:
            return ""

        return "## Coding Context\n\n" + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


# Language detection by file extension
LANG_BY_EXTENSION: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".swift": "swift",
    ".m": "objc",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".lua": "lua",
    ".r": "r",
    ".dart": "dart",
    ".elixir": "elixir",
    ".ex": "elixir",
    ".exs": "elixir",
    ".clj": "clojure",
    ".hs": "haskell",
    ".ml": "ocaml",
    ".nim": "nim",
    ".v": "v",
    ".zig": "zig",
}

# Framework detection by config file / directory
FRAMEWORK_CHECKS: list[tuple[str, str, str]] = [
    # (file/dir to check, framework name, language)
    ("pyproject.toml", "python", "python"),
    ("setup.py", "python", "python"),
    ("requirements.txt", "pip", "python"),
    ("Pipfile", "pipenv", "python"),
    ("poetry.lock", "poetry", "python"),
    ("package.json", "node", "javascript"),
    ("tsconfig.json", "typescript", "typescript"),
    ("Cargo.toml", "cargo", "rust"),
    ("go.mod", "go-module", "go"),
    ("Gemfile", "bundler", "ruby"),
    ("composer.json", "composer", "php"),
    ("pom.xml", "maven", "java"),
    ("build.gradle", "gradle", "java"),
    ("build.gradle.kts", "gradle", "kotlin"),
    ("mix.exs", "mix", "elixir"),
    ("CMakeLists.txt", "cmake", "c"),
    ("Makefile", "make", "c"),
]

# Framework detection by package.json dependencies
NPM_FRAMEWORKS: dict[str, str] = {
    "next": "next.js",
    "react": "react",
    "vue": "vue",
    "nuxt": "nuxt",
    "express": "express",
    "fastify": "fastify",
    "svelte": "svelte",
    "angular": "angular",
    "@angular/core": "angular",
    "gatsby": "gatsby",
    "remix": "remix",
    "astro": "astro",
    "electron": "electron",
    "tailwindcss": "tailwind",
    "prisma": "prisma",
    "trpc": "trpc",
    "vite": "vite",
    "webpack": "webpack",
    "rollup": "rollup",
    "esbuild": "esbuild",
}

# Python framework detection by imports in pyproject.toml / requirements.txt
PYTHON_FRAMEWORKS: dict[str, str] = {
    "fastapi": "fastapi",
    "flask": "flask",
    "django": "django",
    "pydantic": "pydantic",
    "pytest": "pytest",
    "streamlit": "streamlit",
    "gradio": "gradio",
    "celery": "celery",
    "scrapy": "scrapy",
    "selenium": "selenium",
    "playwright": "playwright",
    "pandas": "pandas",
    "numpy": "numpy",
    "torch": "pytorch",
    "tensorflow": "tensorflow",
    "transformers": "transformers",
    "langchain": "langchain",
    "pydantic-ai": "pydantic-ai",
    "textual": "textual",
    "rich": "rich",
    "click": "click",
}

# Test framework detection
TEST_FRAMEWORKS: dict[str, str] = {
    "pytest": "pytest",
    "unittest": "unittest",
    "jest": "jest",
    "vitest": "vitest",
    "mocha": "mocha",
    "jasmine": "jasmine",
    "cypress": "cypress",
    "playwright": "playwright",
    "rspec": "rspec",
    "cargo": "cargo-test",
    "go": "go-test",
}

# Linter/formatter detection
LINTER_FILES: dict[str, str] = {
    ".eslintrc": "eslint",
    ".eslintrc.js": "eslint",
    ".eslintrc.json": "eslint",
    ".eslintrc.yml": "eslint",
    "eslint.config.mjs": "eslint",
    ".prettierrc": "prettier",
    ".prettierrc.json": "prettier",
    "prettier.config.js": "prettier",
    ".flake8": "flake8",
    "setup.cfg": "flake8",  # if contains flake8 section
    ".ruff.toml": "ruff",
    "ruff.toml": "ruff",
    ".pylintrc": "pylint",
    "pyproject.toml": "ruff",  # if contains ruff config
    ".gitleaks.toml": "gitleaks",
    ".pre-commit-config.yaml": "pre-commit",
}


def detect_coding_context(directory: str | Path | None = None) -> CodingContext:
    """Detect coding context from the current or specified directory.

    Args:
        directory: Directory to scan. If None, uses current directory.

    Returns:
        CodingContext with detected information
    """
    if directory is None:
        directory = os.getcwd()
    dir_path = Path(directory).resolve()

    ctx = CodingContext(directory=str(dir_path))

    if not dir_path.exists() or not dir_path.is_dir():
        return ctx

    # Detect git
    _detect_git(dir_path, ctx)

    # Detect languages
    _detect_languages(dir_path, ctx)

    # Detect frameworks and package managers
    _detect_frameworks(dir_path, ctx)

    # Detect test frameworks
    _detect_test_frameworks(dir_path, ctx)

    # Detect linters
    _detect_linters(dir_path, ctx)

    # Detect project name
    _detect_project_name(dir_path, ctx)

    # Check for README and tests
    ctx.has_readme = any(
        (dir_path / name).exists()
        for name in ["README.md", "README.rst", "README.txt", "README"]
    )
    ctx.has_tests = (dir_path / "tests").is_dir() or (dir_path / "test").is_dir()

    return ctx


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> str:
    """Run a git command and return output."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _detect_git(dir_path: Path, ctx: CodingContext) -> None:
    """Detect git repository info."""
    git_dir = dir_path / ".git"
    if not git_dir.exists():
        # Check parent directories
        current = dir_path
        while current != current.parent:
            if (current / ".git").exists():
                git_dir = current / ".git"
                break
            current = current.parent

    if not git_dir.exists():
        return

    ctx.is_git_repo = True

    # Branch
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], dir_path)
    ctx.git_branch = branch or "unknown"

    # Status
    status = _run_git(["status", "--porcelain"], dir_path)
    ctx.git_status = "dirty" if status else "clean"

    # Remote
    remote = _run_git(["remote", "get-url", "origin"], dir_path)
    ctx.git_remote = remote


def _detect_languages(dir_path: Path, ctx: CodingContext) -> None:
    """Detect programming languages by counting file extensions."""
    ext_counts: dict[str, int] = {}

    try:
        for item in dir_path.rglob("*"):
            if item.is_file() and not _should_skip_path(item):
                ext = item.suffix.lower()
                if ext in LANG_BY_EXTENSION:
                    lang = LANG_BY_EXTENSION[ext]
                    ext_counts[lang] = ext_counts.get(lang, 0) + 1
    except (PermissionError, OSError):
        pass

    # Sort by count, take top 3
    sorted_langs = sorted(ext_counts.items(), key=lambda x: x[1], reverse=True)
    ctx.languages = [lang for lang, _ in sorted_langs[:3]]
    ctx.primary_language = sorted_langs[0][0] if sorted_langs else ""


def _detect_frameworks(dir_path: Path, ctx: CodingContext) -> None:
    """Detect frameworks and package managers."""
    frameworks: list[str] = []
    package_managers: list[str] = []

    for filename, framework, lang in FRAMEWORK_CHECKS:
        if (dir_path / filename).exists():
            if framework in ("pip", "pipenv", "poetry", "cargo", "go-module",
                             "bundler", "composer", "maven", "gradle", "npm", "node"):
                package_managers.append(framework)
            else:
                frameworks.append(framework)

    # Check package.json for JS frameworks
    pkg_json = dir_path / "package.json"
    if pkg_json.exists():
        try:
            import json
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            for dep_name, fw_name in NPM_FRAMEWORKS.items():
                if dep_name in deps:
                    frameworks.append(fw_name)
        except Exception:
            pass

    # Check Python deps for frameworks
    for req_file in ["requirements.txt", "pyproject.toml"]:
        filepath = dir_path / req_file
        if filepath.exists():
            try:
                content = filepath.read_text(encoding="utf-8")
                for dep_name, fw_name in PYTHON_FRAMEWORKS.items():
                    if dep_name in content.lower():
                        if fw_name not in frameworks:
                            frameworks.append(fw_name)
            except Exception:
                pass

    ctx.frameworks = frameworks
    ctx.package_managers = package_managers


def _detect_test_frameworks(dir_path: Path, ctx: CodingContext) -> None:
    """Detect test frameworks."""
    test_frameworks: list[str] = []

    # Check for test directories/files
    test_dirs = ["tests", "test", "__tests__", "spec", "specs"]
    for d in test_dirs:
        if (dir_path / d).is_dir():
            # Check what's inside
            test_dir = dir_path / d
            for item in test_dir.iterdir():
                if item.suffix == ".py" and "pytest" not in test_frameworks:
                    test_frameworks.append("pytest")
                elif item.suffix in (".js", ".ts") and "jest" not in test_frameworks:
                    test_frameworks.append("jest")
                elif item.suffix == ".rs" and "cargo-test" not in test_frameworks:
                    test_frameworks.append("cargo-test")
                elif item.suffix == ".go" and "go-test" not in test_frameworks:
                    test_frameworks.append("go-test")
            break

    # Check package.json for test frameworks
    pkg_json = dir_path / "package.json"
    if pkg_json.exists():
        try:
            import json
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            for dep_name, fw_name in TEST_FRAMEWORKS.items():
                if dep_name in deps and fw_name not in test_frameworks:
                    test_frameworks.append(fw_name)
        except Exception:
            pass

    # Check Python deps
    for req_file in ["requirements.txt", "pyproject.toml"]:
        filepath = dir_path / req_file
        if filepath.exists():
            try:
                content = filepath.read_text(encoding="utf-8")
                for dep_name, fw_name in TEST_FRAMEWORKS.items():
                    if dep_name in content.lower() and fw_name not in test_frameworks:
                        test_frameworks.append(fw_name)
            except Exception:
                pass

    ctx.test_frameworks = test_frameworks


def _detect_linters(dir_path: Path, ctx: CodingContext) -> None:
    """Detect linters and formatters."""
    linters: list[str] = []

    for filename, linter in LINTER_FILES.items():
        filepath = dir_path / filename
        if filepath.exists():
            if linter not in linters:
                linters.append(linter)

    ctx.linters = linters


def _detect_project_name(dir_path: Path, ctx: CodingContext) -> None:
    """Detect project name from directory or config files."""
    # Try package.json
    pkg_json = dir_path / "package.json"
    if pkg_json.exists():
        try:
            import json
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            name = data.get("name")
            if name:
                ctx.project_name = name
                return
        except Exception:
            pass

    # Try pyproject.toml
    pyproject = dir_path / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text(encoding="utf-8")
            match = re.search(r'name\s*=\s*["\']([^"\']+)["\']', content)
            if match:
                ctx.project_name = match.group(1)
                return
        except Exception:
            pass

    # Try Cargo.toml
    cargo = dir_path / "Cargo.toml"
    if cargo.exists():
        try:
            content = cargo.read_text(encoding="utf-8")
            match = re.search(r'name\s*=\s*["\']([^"\']+)["\']', content)
            if match:
                ctx.project_name = match.group(1)
                return
        except Exception:
            pass

    # Fall back to directory name
    ctx.project_name = dir_path.name


def _should_skip_path(path: Path) -> bool:
    """Check if a path should be skipped during language detection."""
    skip_dirs = {
        "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
        ".next", ".nuxt", "dist", "build", ".tox", ".eggs", "target",
        ".cache", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    }
    for part in path.parts:
        if part in skip_dirs:
            return True
    return False
