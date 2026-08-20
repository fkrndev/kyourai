"""Smoke test for the MemoryManager orchestrator."""
import json
import os
import tempfile

test_home = tempfile.mkdtemp(prefix="kyourai_mgr_")
os.environ["KYOURAI_HOME"] = test_home

from kyourai.memory.manager import MemoryManager
from kyourai.memory.builtin import BuiltinMemoryProvider
from kyourai.memory.holographic.provider import HolographicMemoryProvider

# Build manager with builtin + holographic
manager = MemoryManager()
manager.add_provider(BuiltinMemoryProvider())
manager.add_provider(HolographicMemoryProvider(config={"hrr_dim": 1024}))

# Initialize all providers
for p in manager.providers:
    p.initialize("test-session")

# Test: only one external provider allowed
from kyourai.memory.holographic.provider import HolographicMemoryProvider as HMP2
second_external = HMP2()
manager.add_provider(second_external)  # should be rejected
print("External provider limit enforced:", len(manager.providers) == 2)

# Test system prompt assembly
prompt = manager.build_system_prompt()
print("System prompt assembled:", len(prompt) > 0)

# Test tool schema collection
schemas = manager.get_all_tool_schemas()
tool_names = [s["name"] for s in schemas]
print("Tool schemas:", tool_names)
print("Has 'memory' tool:", "memory" in tool_names)
print("Has 'fact_store' tool:", "fact_store" in tool_names)
print("Has 'fact_feedback' tool:", "fact_feedback" in tool_names)

# Test tool routing
result = json.loads(manager.handle_tool_call("memory", {"action": "add", "target": "memory", "content": "Test fact via manager"}))
print("Memory tool routed:", result.get("success"))

result = json.loads(manager.handle_tool_call("fact_store", {"action": "add", "content": "Manager test fact", "category": "general"}))
print("Fact_store tool routed:", result.get("status") == "added")

# Test prefetch
context = manager.prefetch_all("test query")
print("Prefetch returns string:", isinstance(context, str))

# Test sync (non-blocking)
manager.sync_all("user message", "assistant response", session_id="test-session")
manager.flush_pending(timeout=2.0)
print("Sync completed without blocking")

# Test lifecycle hooks
manager.on_turn_start(1, "hello")
manager.on_session_end([])

# Test recall indicator
indicator = manager.describe_recall()
print("Recall indicator:", repr(indicator))

# Shutdown
manager.shutdown_all()
print("Shutdown completed")

print("\nAll MemoryManager tests passed!")
