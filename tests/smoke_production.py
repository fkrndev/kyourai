"""Smoke test for production hardening, security, coding context, usage, sanitizer, shell hooks."""
import sys
import os
import tempfile
import asyncio

sys.path.insert(0, ".")

# Use temp KYOURAI_HOME
tmp = tempfile.mkdtemp()
os.environ["KYOURAI_HOME"] = tmp
os.environ["PYTHONIOENCODING"] = "utf-8"

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}: {detail}")

# ---------------------------------------------------------------------------
print("\n--- MCP Catalog ---")
from kyourai.mcp.catalog import MCPCatalog, BUNDLED_SERVERS, MCPServerConfig

catalog = MCPCatalog()
check("catalog instantiation", catalog is not None)
check("bundled servers exist", len(BUNDLED_SERVERS) >= 5, f"got {len(BUNDLED_SERVERS)}")
check("list_servers empty initially", len(catalog.list_servers()) == 0)

# Register a bundled server
cfg = catalog.register_bundled("filesystem")
check("register bundled filesystem", cfg is not None, "register returned None")
check("filesystem registered", catalog.get_server("filesystem") is not None)

# List bundled
bundled = catalog.list_bundled()
check("list_bundled returns list", len(bundled) > 0)
check("filesystem in bundled", any(b["name"] == "filesystem" for b in bundled))

# Unregister
result = catalog.unregister("filesystem")
check("unregister filesystem", result is True)
check("filesystem unregistered", catalog.get_server("filesystem") is None)

# Register custom
cfg = catalog.register("my-server", command="echo", args=["hello"], description="test")
check("register custom", cfg is not None)
check("custom registered", catalog.get_server("my-server") is not None)

# Status
statuses = catalog.status()
check("status returns list", len(statuses) == 1)
check("status has my-server", statuses[0]["name"] == "my-server")

# ---------------------------------------------------------------------------
print("\n--- Production Hardening ---")
from kyourai.production import (
    validate_config, GracefulShutdown, HealthChecker,
    setup_structured_logging, StructuredFormatter,
    ValidationResult, HealthReport, HealthStatus,
)

# Config validation
result = validate_config()
check("validate_config returns ValidationResult", isinstance(result, ValidationResult))
check("config valid (no config file)", result.valid)
check("config has defaults", "agent" in result.config)

# Graceful shutdown
shutdown = GracefulShutdown()
check("GracefulShutdown instantiation", shutdown is not None)
check("not shutting down initially", not shutdown.is_shutting_down)

called = []
shutdown.register(lambda: called.append(1))
check("register callback", len(shutdown._callbacks) == 1)

# Health checker
checker = HealthChecker()
check("HealthChecker instantiation", checker is not None)
report = checker.check_all()
check("check_all returns HealthReport", isinstance(report, HealthReport))
check("report has components", len(report.components) > 0)
check("report has version", report.version == "0.1.0")
check("report to_dict", "status" in report.to_dict())

# Structured logging
setup_structured_logging(level="DEBUG")
check("setup_structured_logging no error", True)

# ---------------------------------------------------------------------------
print("\n--- Credential Redaction ---")
from kyourai.security import (
    redact_text, redact_dict, redact_messages, scan_for_secrets,
    PATTERNS,
)

check("patterns exist", len(PATTERNS) >= 10, f"got {len(PATTERNS)}")

# OpenAI key
result = redact_text("my key is sk-abcdefghijklmnopqrstuvwxyz123456")
check("redact openai key", "[REDACTED:openai-api-key]" in result.text)
check("redaction recorded", len(result.redactions) > 0)

# AWS key
result = redact_text("aws key AKIAIOSFODNN7EXAMPLE")
check("redact aws key", "[REDACTED:aws-access-key]" in result.text)

# GitHub token
result = redact_text("ghp_1234567890abcdefghijklmnopqrstuvwxyz")
check("redact github token", "[REDACTED:github-token]" in result.text)

# Private key
result = redact_text("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAI...\n-----END RSA PRIVATE KEY-----")
check("redact private key", "[REDACTED:private-key]" in result.text)

# JWT
result = redact_text("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123456")
check("redact jwt", "[REDACTED:jwt]" in result.text)

# No false positive
result = redact_text("hello world this is normal text")
check("no false positive", not result.was_redacted)

# Redact dict
result = redact_dict({"api_key": "sk-test123", "name": "user"})
check("redact dict sensitive key", result["api_key"] == "[REDACTED:sensitive-key]")
check("redact dict preserves non-sensitive", result["name"] == "user")

# Redact messages
result = redact_messages([
    {"role": "user", "content": "my key is sk-abcdefghijklmnopqrstuvwxyz123456"},
    {"role": "assistant", "content": "ok"},
])
check("redact_messages preserves structure", len(result) == 2)
check("redact_messages redacts content", "[REDACTED:openai-api-key]" in result[0]["content"])

# Scan for secrets
findings = scan_for_secrets("key: sk-abcdefghijklmnopqrstuvwxyz123456, aws: AKIAIOSFODNN7EXAMPLE")
check("scan_for_secrets finds items", len(findings) >= 2)

# ---------------------------------------------------------------------------
print("\n--- Coding Context ---")
from kyourai.context.coding import detect_coding_context, CodingContext

# Detect in kyourai repo (should be Python)
ctx = detect_coding_context(".")
check("detect_coding_context returns CodingContext", isinstance(ctx, CodingContext))
check("directory set", ctx.directory != "")
check("is git repo", ctx.is_git_repo, "kyourai should be a git repo")
check("git branch set", ctx.git_branch != "")
check("python detected", "python" in ctx.languages)
check("primary language python", ctx.primary_language == "python")
check("has readme", ctx.has_readme)
check("has tests", ctx.has_tests)
check("project name set", ctx.project_name != "")

# to_prompt
prompt = ctx.to_prompt()
check("to_prompt non-empty", len(prompt) > 0)
check("to_prompt has Coding Context header", "## Coding Context" in prompt)
check("to_prompt has git info", "Git:" in prompt)

# Non-git directory
ctx2 = detect_coding_context(tempfile.mkdtemp())
check("non-git dir not git repo", not ctx2.is_git_repo)

# ---------------------------------------------------------------------------
print("\n--- Usage Tracker ---")
from kyourai.usage import UsageTracker, estimate_cost, get_pricing, UsageEntry, UsageTotal

# Pricing
pricing = get_pricing("gpt-4o")
check("gpt-4o pricing exists", pricing["input"] > 0)

pricing = get_pricing("openai:gpt-4o")
check("openai:gpt-4o pricing (strip prefix)", pricing["input"] > 0)

pricing = get_pricing("unknown-model")
check("unknown model zero pricing", pricing["input"] == 0.0)

# Estimate cost
cost = estimate_cost("gpt-4o", 1_000_000, 500_000)
check("estimate_cost gpt-4o", cost > 0)
check("estimate_cost reasonable", 5.0 < cost < 15.0)

cost = estimate_cost("test", 1000, 1000)
check("estimate_cost test model zero", cost == 0.0)

# Tracker
tracker = UsageTracker()
entry = tracker.record(
    session_id="test-session",
    model="openai:gpt-4o",
    prompt_tokens=1000,
    completion_tokens=500,
)
check("record returns UsageEntry", isinstance(entry, UsageEntry))
check("entry has cost", entry.cost_usd > 0)

# Get session total
total = tracker.get_session_total("test-session")
check("get_session_total returns UsageTotal", isinstance(total, UsageTotal))
check("total has prompt tokens", total.total_prompt_tokens == 1000)
check("total has completion tokens", total.total_completion_tokens == 500)
check("total has cost", total.total_cost_usd > 0)

# Get totals
totals = tracker.get_totals(days=30)
check("get_totals returns UsageTotal", isinstance(totals, UsageTotal))
check("totals has entries", totals.entry_count > 0)

# By model
by_model = tracker.get_by_model(days=30)
check("get_by_model returns dict", isinstance(by_model, dict))
check("by_model has gpt-4o", "openai:gpt-4o" in by_model)

# ---------------------------------------------------------------------------
print("\n--- Message Sanitizer ---")
from kyourai.context.sanitizer import sanitize_messages, validate_messages

# Empty messages
result = sanitize_messages([])
check("sanitize empty", result == [])

# Filter empty
result = sanitize_messages([
    {"role": "user", "content": ""},
    {"role": "user", "content": "hello"},
])
check("filter empty messages", len(result) == 1)

# Merge consecutive
result = sanitize_messages([
    {"role": "user", "content": "hello"},
    {"role": "user", "content": "world"},
])
check("merge consecutive same role", len(result) == 1)
check("merged content", "hello" in result[0]["content"] and "world" in result[0]["content"])

# Strip control chars
result = sanitize_messages([
    {"role": "user", "content": "hello\x00\x01world"},
])
check("strip control chars", "\x00" not in result[0]["content"])

# System first
result = sanitize_messages([
    {"role": "user", "content": "hi"},
    {"role": "system", "content": "be helpful"},
])
check("system first", result[0]["role"] == "system")

# Validate
issues = validate_messages([
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hi"},
])
check("validate good messages", len(issues) == 0)

issues = validate_messages([
    {"role": "user", "content": "hello"},
    {"role": "user", "content": "world"},
])
check("validate catches alternation violation", len(issues) > 0)

# ---------------------------------------------------------------------------
print("\n--- Shell Hooks ---")
from kyourai.tools.shell_hooks import ShellHookManager, ShellHook, HookResult

manager = ShellHookManager()
check("ShellHookManager instantiation", manager is not None)

# Register pre-hook
hook = manager.register_pre(
    name="block-rm-rf",
    pattern=r"rm -rf /",
    action="block",
    message="Blocked: rm -rf / is dangerous",
)
check("register pre-hook", hook is not None)

# Test pre-hook blocking
result = manager.run_pre_hooks("rm -rf /")
check("pre-hook blocks dangerous command", result.blocked)
check("pre-hook has warning", len(result.warnings) > 0)

# Test pre-hook non-matching
result = manager.run_pre_hooks("ls -la")
check("pre-hook non-matching not blocked", not result.blocked)

# Register post-hook
hook = manager.register_post(
    name="auto-test",
    pattern=r"git commit",
    action="run",
    command="pytest --tb=short",
)
check("register post-hook", hook is not None)

# Test post-hook
result = manager.run_post_hooks("git commit -m 'test'")
check("post-hook matches git commit", len(result.post_commands) > 0)
check("post-hook command is pytest", "pytest" in result.post_commands[0])

# List hooks
hooks = manager.list_hooks()
check("list_hooks has pre", len(hooks["pre"]) > 0)
check("list_hooks has post", len(hooks["post"]) > 0)

# Disable/enable
manager.disable("block-rm-rf")
result = manager.run_pre_hooks("rm -rf /")
check("disabled hook doesn't block", not result.blocked)

manager.enable("block-rm-rf")
result = manager.run_pre_hooks("rm -rf /")
check("re-enabled hook blocks", result.blocked)

# ---------------------------------------------------------------------------
print("\n--- Advanced Context Compressor ---")
from kyourai.context.compressor import (
    compress_sliding_window, compress_importance_based,
    compress_semantic, compress_multi_strategy,
    COMPRESSION_STRATEGIES, _score_message_importance,
)

check("strategies exist", len(COMPRESSION_STRATEGIES) == 4)

# Sliding window
messages = [{"role": "user", "content": f"msg {i}"} for i in range(20)]
result = compress_sliding_window(messages, keep_recent=5)
check("sliding_window keeps 5", len(result) == 5)
check("sliding_window keeps recent", result[-1]["content"] == "msg 19")

# Importance scoring
score = _score_message_importance({"role": "user", "content": "```python\nprint('hi')\n```"})
check("code block scores high", score > 0.3)

score = _score_message_importance({"role": "system", "content": "be helpful"})
check("system message scores high", score > 0.5)

score = _score_message_importance({"role": "user", "content": "ok"})
check("short message scores low", score < 0.3)

# Importance-based compression
messages = []
for i in range(20):
    messages.append({"role": "user", "content": f"message number {i}"})
    messages.append({"role": "assistant", "content": f"```python\ncode_{i}()\n```" if i % 3 == 0 else f"reply {i}"})
flat = messages
result = compress_importance_based(flat, keep_recent=4, keep_ratio=0.3)
check("importance compression reduces messages", len(result) < len(flat))
check("importance keeps recent", len(result) >= 4)

# ---------------------------------------------------------------------------
print(f"\n=== Results: {passed} passed, {failed} failed ===")
if failed > 0:
    sys.exit(1)
else:
    print("ALL TESTS PASSED")
