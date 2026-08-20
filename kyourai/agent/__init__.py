"""Kyourai agent package — core agent + supporting modules.

The main KyouraiAgent class lives in _main.py (moved from agent.py to
convert agent into a package). Supporting modules (error_classifier,
retry_utils, rate_limit_tracker, etc.) live alongside.
"""

from kyourai.agent._main import KyouraiAgent, DEFAULT_SYSTEM_PROMPT
from kyourai.agent.error_classifier import classify_error, ErrorCategory, ErrorClassification
from kyourai.agent.retry_utils import retry_with_backoff, retry_sync
from kyourai.agent.rate_limit_tracker import RateLimitTracker
from kyourai.agent.empty_response_guard import guard_response, is_empty_response
from kyourai.agent.title_generator import generate_title, generate_title_sync
from kyourai.agent.subagent import SubagentDelegator, DelegationResult
from kyourai.agent.verification import verify_output, VerificationResult, detect_claims
from kyourai.agent.prompt_builder import build_system_prompt
from kyourai.agent.plugin_system import PluginManager, PluginInfo, PluginContext, HookRegistry

__all__ = [
    "KyouraiAgent",
    "DEFAULT_SYSTEM_PROMPT",
    "classify_error",
    "ErrorCategory",
    "ErrorClassification",
    "retry_with_backoff",
    "retry_sync",
    "RateLimitTracker",
    "guard_response",
    "is_empty_response",
    "generate_title",
    "generate_title_sync",
    "SubagentDelegator",
    "DelegationResult",
    "verify_output",
    "VerificationResult",
    "detect_claims",
    "build_system_prompt",
    "PluginManager",
    "PluginInfo",
    "PluginContext",
    "HookRegistry",
]
