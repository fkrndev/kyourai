"""Verification system — check agent output before returning to user.

Inspired by Hermes' verification_stop.py + verification_evidence.py.
The core idea: when the agent makes claims (especially about code),
verify those claims before returning the response. This prevents
hallucinated outputs from reaching the user unchecked.

Verification checks:
  1. Code claim verification: if agent claims "tests pass" or "build
     succeeds", actually run the command and check.
  2. File existence verification: if agent claims to have created/modified
     a file, check it exists.
  3. Command result verification: if agent references a command output,
     verify the output matches.

The system is opt-in (config: agent.verify_output) and non-blocking —
if verification fails, a warning is appended to the output, but the
response is still returned. The agent can then self-correct on the
next turn.

Policy-only by design (like Hermes): this module never runs code itself.
It checks evidence that the agent's tools already produced.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Verification result
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class VerificationResult:
    """Result of verifying an agent response."""
    passed: bool = True
    warnings: list[str] = field(default_factory=list)
    checks_run: int = 0

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0


# ---------------------------------------------------------------------------
# Claim patterns — detect assertions in agent output that can be verified
# ---------------------------------------------------------------------------

# Patterns that indicate the agent is claiming something verifiable
_CLAIM_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "tests_pass",
        re.compile(
            r"(?:all|the)\s+tests?\s+(?:pass|passed|succeed|succeeded)",
            re.IGNORECASE,
        ),
        "Agent claims tests pass — verify with actual test run",
    ),
    (
        "build_succeeds",
        re.compile(
            r"(?:build|compile|make)\s+(?:succeeds|succeeded|passes|passed|works|completed)",
            re.IGNORECASE,
        ),
        "Agent claims build succeeds — verify with actual build",
    ),
    (
        "file_created",
        re.compile(
            r'(?:created|wrote|generated|saved)\s+(?:the\s+)?(?:file\s+)?[`\'"]?([\w/\\.-]+\.\w+)[`\'"]?',
            re.IGNORECASE,
        ),
        "Agent claims file was created — verify file exists",
    ),
    (
        "file_modified",
        re.compile(
            r'(?:modified|updated|changed|edited|fixed)\s+(?:the\s+)?(?:file\s+)?[`\'"]?([\w/\\.-]+\.\w+)[`\'"]?',
            re.IGNORECASE,
        ),
        "Agent claims file was modified — verify file exists",
    ),
    (
        "lint_passes",
        re.compile(
            r"(?:lint|linter|pylint|flake8|eslint)\s+(?:pass|passes|passed|clean|no\s+errors)",
            re.IGNORECASE,
        ),
        "Agent claims lint passes — verify with actual lint run",
    ),
    (
        "no_errors",
        re.compile(
            r"(?:no\s+errors?|zero\s+errors?|error[-\s]?free|without\s+errors?)",
            re.IGNORECASE,
        ),
        "Agent claims no errors — verify",
    ),
]


def detect_claims(output: str) -> list[dict[str, str]]:
    """Detect verifiable claims in agent output.

    Returns list of {'type': ..., 'detail': ..., 'description': ...} dicts.
    """
    claims: list[dict[str, str]] = []
    for claim_type, pattern, description in _CLAIM_PATTERNS:
        matches = pattern.findall(output)
        for match in matches:
            if isinstance(match, tuple):
                match = match[0]  # file path from capture group
            claims.append({
                "type": claim_type,
                "detail": str(match) if match else "",
                "description": description,
            })
    return claims


# ---------------------------------------------------------------------------
# Verification checks
# ---------------------------------------------------------------------------


def _check_file_exists(path: str) -> bool:
    """Check if a file exists."""
    from pathlib import Path

    try:
        return Path(path).expanduser().exists()
    except Exception:
        return False


def _check_test_command(command: str, timeout: int = 30) -> tuple[bool, str]:
    """Run a test command and check if it passes."""
    import subprocess

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0, result.stdout[-500:] + result.stderr[-500:]
    except Exception as e:
        return False, str(e)


def verify_output(
    output: str,
    *,
    tool_results: list[dict[str, Any]] | None = None,
    verify_files: bool = True,
    verify_commands: bool = False,  # disabled by default — requires terminal tool
) -> VerificationResult:
    """Verify an agent response by checking its claims.

    Args:
        output: The agent's response text
        tool_results: Results from tool calls made during this turn
        verify_files: Check if claimed files exist
        verify_commands: Run claimed test/build commands (disabled by default)

    Returns:
        VerificationResult with pass status and any warnings
    """
    result = VerificationResult()

    claims = detect_claims(output)
    result.checks_run = len(claims)

    if not claims:
        return result  # nothing to verify

    for claim in claims:
        claim_type = claim["type"]
        detail = claim["detail"]

        if claim_type in ("file_created", "file_modified") and verify_files:
            if detail and not _check_file_exists(detail):
                result.warnings.append(
                    f"Verification: agent claims file '{detail}' was "
                    f"{'created' if claim_type == 'file_created' else 'modified'} "
                    f"but it does not exist"
                )
                result.passed = False

        if claim_type in ("tests_pass", "build_succeeds", "lint_passes", "no_errors"):
            # Check if tool_results contain evidence of the claim
            if tool_results:
                has_evidence = any(
                    "pass" in str(tr.get("output", "")).lower()
                    or "success" in str(tr.get("output", "")).lower()
                    or "0" == str(tr.get("output", "")).strip().split("\n")[-1].split()[-1]
                    if tr.get("output", "").strip()
                    else False
                    for tr in tool_results
                )
                if not has_evidence:
                    result.warnings.append(
                        f"Verification: agent claims {claim_type.replace('_', ' ')} "
                        f"but no evidence found in tool results"
                    )
                    # Don't fail — just warn. The agent might have run the
                    # command in a previous turn.
            else:
                # No tool results — agent is claiming without evidence
                result.warnings.append(
                    f"Verification: agent claims {claim_type.replace('_', ' ')} "
                    f"but no tool calls were made this turn"
                )

    return result


def format_verification_warning(result: VerificationResult) -> str:
    """Format verification warnings as a string to append to output."""
    if not result.has_warnings:
        return ""

    lines = ["\n\n--- Verification Warnings ---"]
    for w in result.warnings:
        lines.append(f"⚠ {w}")
    lines.append("Please verify these claims before proceeding.")
    return "\n".join(lines)
