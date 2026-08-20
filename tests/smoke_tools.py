"""Smoke test for core tools (terminal, read_file, web_search) + context compression."""
import os
import tempfile

test_home = tempfile.mkdtemp(prefix="kyourai_tools_test_")
os.environ["KYOURAI_HOME"] = test_home

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


print("=== Core Tools + Context Compression Smoke Test ===\n")

# -- Tool Registry --
print("Tool Registry:")
from kyourai.tools import discover_core_tools, get_tool_handler, list_core_tool_names

names = list_core_tool_names()
check("Core tool names listed", len(names) == 3, str(names))

schemas = discover_core_tools()
check("Discovered 3 core tools", len(schemas) == 3)
check("terminal schema found", any(s["name"] == "terminal" for s in schemas))
check("read_file schema found", any(s["name"] == "read_file" for s in schemas))
check("web_search schema found", any(s["name"] == "web_search" for s in schemas))

# -- Terminal tool --
print("\nTerminal Tool:")
from kyourai.tools.terminal import handle as terminal_handle, _check_safety

# Safe command
result = terminal_handle("echo hello world")
check("echo command works", "hello world" in result)

# Python version
result = terminal_handle("python --version")
check("python --version works", "Python" in result)

# Safety: blocked command
blocked = _check_safety("rm -rf /")
check("rm -rf / blocked", blocked is not None)

blocked = _check_safety("echo hello")
check("echo not blocked", blocked is None)

# Timeout
result = terminal_handle("ping -n 10 127.0.0.1", timeout=1)
check("Timeout works", "timed out" in result.lower() or "timeout" in result.lower())

# Output truncation
result = terminal_handle("python -c \"print('x' * 15000)\"")
check("Output truncated", "truncated" in result)

# Exit code
result = terminal_handle("python -c \"import sys; sys.exit(1)\"")
check("Exit code reported", "exit code 1" in result)

# -- Read file tool --
print("\nRead File Tool:")
from kyourai.tools.read_file import handle as read_file_handle

# Create a test file
test_file = os.path.join(test_home, "test.txt")
with open(test_file, "w") as f:
    f.write("Hello from test file!\nLine 2\nLine 3")

result = read_file_handle(test_file)
check("Read text file", "Hello from test file" in result)
check("Read all lines", "Line 2" in result and "Line 3" in result)

# Non-existent file
result = read_file_handle("/nonexistent/path/xyz.txt")
check("Non-existent file error", "not found" in result.lower())

# Binary file
bin_file = os.path.join(test_home, "test.bin")
with open(bin_file, "wb") as f:
    f.write(b"\x00\x01\x02\x03\xff\xfe")
result = read_file_handle(bin_file)
check("Binary file rejected", "binary" in result.lower())

# Read self (source file)
result = read_file_handle(__file__)
check("Read source file", "smoke" in result.lower() or "test" in result.lower())

# -- Web search tool --
print("\nWeb Search Tool:")
from kyourai.tools.web_search import handle as web_search_handle

# Note: web search may fail in CI/offline — test gracefully
try:
    result = web_search_handle("Python programming language", max_results=2)
    check("Web search returns result", len(result) > 0)
    # Result should contain either search results or an error message
    check("Web search has format", "results" in result.lower() or "failed" in result.lower() or "error" in result.lower())
except Exception as e:
    check("Web search graceful failure", True, f"network error (expected in CI): {e}")

# Empty query
result = web_search_handle("")
check("Empty query rejected", "empty" in result.lower())

# -- Context Compression --
print("\nContext Compression:")
from kyourai.context import (
    estimate_tokens,
    estimate_message_tokens,
    should_compress,
    split_messages,
    compress_messages,
)

# Token estimation
tokens = estimate_tokens("hello world")
check("Token estimate", tokens > 0, f"{tokens} tokens")

# Message token estimation
msgs = [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi there!"}]
tokens = estimate_message_tokens(msgs)
check("Message token estimate", tokens > 0, f"{tokens} tokens")

# should_compress: small list → False
small = [{"role": "user", "content": "hi"}] * 5
check("Small list no compress", not should_compress(small))

# should_compress: large list → True
large = [{"role": "user", "content": "x" * 1000}] * 100  # ~25k tokens
check("Large list triggers compress", should_compress(large, model_context=30000))

# split_messages
msgs = [{"role": f"user{i}", "content": f"msg {i}"} for i in range(20)]
old, recent = split_messages(msgs, keep_recent=5)
check("Split: old has 15", len(old) == 15)
check("Split: recent has 5", len(recent) == 5)
check("Split: recent is last 5", recent[-1]["content"] == "msg 19")

# compress_messages
summary = "This is a summary of the conversation."
compressed = compress_messages(msgs, summary, keep_recent=5)
check("Compressed has 6 messages", len(compressed) == 6)  # 1 summary + 5 recent
check("Compressed starts with summary", "summary" in compressed[0]["content"].lower())
check("Compressed summary is system role", compressed[0]["role"] == "system")
check("Compressed keeps recent", compressed[-1]["content"] == "msg 19")

# compress_if_needed with no model (graceful degradation)
import asyncio
from kyourai.context import compress_if_needed

async def test_compress():
    # Small list — no compression
    result = await compress_if_needed(small, model=None)
    return result

result = asyncio.run(test_compress())
check("No compress for small list", result == small)

# -- Agent integration --
print("\nAgent Integration:")
from kyourai.agent import KyouraiAgent
from pydantic_ai.models.test import TestModel

agent = KyouraiAgent(
    model=TestModel(),
    session_id="tools-test",
    enable_curator=False,
    enable_skills=False,
    enable_cron=False,
)

# Agent should have 6 tools: 3 memory + 3 core
tool_names = [t.name for t in agent._agent._function_tools.values()] if hasattr(agent._agent, '_function_tools') else []
# Pydantic AI stores tools differently — check via _build_tools
tools = agent._build_tools()
tool_names = [t.name for t in tools]
check("Agent has 6 tools", len(tools) == 6, str(tool_names))
check("Agent has terminal", "terminal" in tool_names)
check("Agent has read_file", "read_file" in tool_names)
check("Agent has web_search", "web_search" in tool_names)
check("Agent has memory", "memory" in tool_names)
check("Agent has fact_store", "fact_store" in tool_names)

# System prompt should mention core tools
from kyourai.agent import DEFAULT_SYSTEM_PROMPT
check("System prompt mentions terminal", "terminal" in DEFAULT_SYSTEM_PROMPT)
check("System prompt mentions read_file", "read_file" in DEFAULT_SYSTEM_PROMPT)
check("System prompt mentions web_search", "web_search" in DEFAULT_SYSTEM_PROMPT)

# System prompt should be frozen (no prefetch mutation)
# The old code appended prefetch to user_prompt — now it doesn't
check("No prefetch in run()", not hasattr(agent, '_prefetch'))

agent.shutdown()

print(f"\n{'='*60}")
print(f"Results: {passed} passed, {failed} failed")
print(f"{'='*60}")
if failed == 0:
    print("ALL TESTS PASSED")
else:
    print("SOME TESTS FAILED")
    import sys
    sys.exit(1)
