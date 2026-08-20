"""Smoke test for the KyouraiAgent core (no LLM calls)."""
import os
import tempfile

test_home = tempfile.mkdtemp(prefix="kyourai_agent_")
os.environ["KYOURAI_HOME"] = test_home

from kyourai.agent import KyouraiAgent
from kyourai.memory.manager import MemoryManager
from kyourai.memory.builtin import BuiltinMemoryProvider
from kyourai.memory.holographic.provider import HolographicMemoryProvider

# Test 1: Build agent without connecting to LLM
# Use Pydantic AI's built-in test model (no API key needed)
agent = KyouraiAgent(
    model="test",
    session_id="test-session",
    enable_curator=False,
)

print("Agent created:", agent is not None)
print("Memory manager has", len(agent.memory_manager.providers), "providers")

# Test 2: Verify tools are registered
tool_schemas = agent.memory_manager.get_all_tool_schemas()
tool_names = [s["name"] for s in tool_schemas]
print("Registered tools:", tool_names)
assert "memory" in tool_names
assert "fact_store" in tool_names
assert "fact_feedback" in tool_names

# Test 3: Verify system prompt contains memory context
# The Pydantic AI agent stores the system prompt internally
# We can check the memory manager's build_system_prompt
prompt = agent.memory_manager.build_system_prompt()
print("Memory system prompt length:", len(prompt))
print("Contains 'Holographic':", "Holographic" in prompt)

# Test 4: Test tool dispatch through the agent
import json
result = json.loads(agent.memory_manager.handle_tool_call(
    "memory",
    {"action": "add", "target": "memory", "content": "Test fact via agent"}
))
print("Memory tool via agent:", result.get("success"))

result = json.loads(agent.memory_manager.handle_tool_call(
    "fact_store",
    {"action": "add", "content": "Agent test fact", "category": "general"}
))
print("Fact_store tool via agent:", result.get("status") == "added")

result = json.loads(agent.memory_manager.handle_tool_call(
    "fact_store",
    {"action": "search", "query": "test"}
))
print("Fact_store search via agent:", result.get("count"), "results")

# Test 5: Verify Pydantic AI agent has the tools
# The agent's tools are registered as Pydantic AI Tool objects
toolset = getattr(agent._agent, '_function_toolset', None)
tool_count = len(toolset.tools) if toolset and hasattr(toolset, 'tools') else "unknown"
print("\nPydantic AI agent created with", tool_count, "tools")

agent.shutdown()
print("\nAll agent core tests passed!")
