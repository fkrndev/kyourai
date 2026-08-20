"""Smoke test for TUI module — verify it imports and can be instantiated."""
import sys
import os
import tempfile

sys.path.insert(0, ".")

# Use temp KYOURAI_HOME
tmp = tempfile.mkdtemp()
os.environ["KYOURAI_HOME"] = tmp
os.environ["PYTHONIOENCODING"] = "utf-8"

from kyourai.tui import KyouraiTUI, run_tui
from kyourai.providers import (
    list_providers,
    parse_model_string,
    resolve_model,
    resolve_with_fallback,
    get_provider_info,
    ProviderConfig,
    FallbackResult,
)

# Test TUI instantiation
app = KyouraiTUI(model="test")
assert app.model == "test"
assert app.agent is None  # not initialized until on_mount
print("TUI instantiation: OK")

# Test provider config
from kyourai.providers import _PROVIDERS
assert len(_PROVIDERS) == 7
assert "openai" in _PROVIDERS
assert "anthropic" in _PROVIDERS
assert "ollama" in _PROVIDERS
print(f"Provider registry: OK ({len(_PROVIDERS)} providers)")

# Test parse_model_string edge cases
assert parse_model_string("openai:gpt-4o") == ("openai", "gpt-4o")
assert parse_model_string("anthropic:claude-3.5-sonnet") == ("anthropic", "claude-3.5-sonnet")
assert parse_model_string("ollama:llama3.2") == ("ollama", "llama3.2")
assert parse_model_string("bedrock:anthropic.claude-3-5-sonnet") == ("bedrock", "anthropic.claude-3-5-sonnet")
assert parse_model_string("groq:llama-3.3-70b") == ("groq", "llama-3.3-70b")
assert parse_model_string("mistral:mistral-large") == ("mistral", "mistral-large")
assert parse_model_string("google:gemini-2.0-flash") == ("google", "gemini-2.0-flash")
assert parse_model_string("gpt-4o") == ("openai", "gpt-4o")  # default
assert parse_model_string("test") == ("openai", "test")  # unknown → openai
print("parse_model_string edge cases: OK")

# Test get_provider_info
info = get_provider_info("openai")
assert info is not None
assert info["name"] == "openai"
assert info["env_key"] == "OPENAI_API_KEY"

info = get_provider_info("nonexistent")
assert info is None
print("get_provider_info: OK")

# Test resolve_model with missing API key (should raise ValueError)
try:
    resolve_model("anthropic:claude-3.5-sonnet")
    # If ANTHROPIC_API_KEY is set, this might succeed
    print("resolve_model anthropic: OK (key set)")
except ValueError as e:
    assert "ANTHROPIC_API_KEY" in str(e)
    print(f"resolve_model anthropic: OK (correctly raises ValueError)")

# Test fallback chain
import asyncio
async def test_fallback():
    result = await resolve_with_fallback(["nonexistent:foo", "anthropic:claude-3.5-sonnet"])
    assert not result.success
    assert result.error is not None
    return result

result = asyncio.run(test_fallback())
print(f"resolve_with_fallback: OK (returns {result.success})")

# Test CLI providers command exists
from kyourai.cli import main
from click.testing import CliRunner
runner = CliRunner()
cli_result = runner.invoke(main, ["providers"])
assert cli_result.exit_code == 0
assert "openai" in cli_result.output
assert "anthropic" in cli_result.output
assert "ollama" in cli_result.output
print("CLI providers command: OK")

print("\n=== All TUI + Providers tests passed! ===")
