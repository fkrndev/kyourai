"""Smoke test for Tier 2.5: verification, prompt builder, plugin system."""
import os
import tempfile
import asyncio

test_home = tempfile.mkdtemp(prefix="kyourai_tier25_test_")
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


print("=== Tier 2.5: Verification + Prompt Builder + Plugin System ===\n")

# -- Verification System --
print("Verification System:")
from kyourai.agent.verification import (
    detect_claims,
    verify_output,
    VerificationResult,
    format_verification_warning,
)

# Detect claims: tests pass
claims = detect_claims("I ran the tests and all tests pass successfully.")
check("Detect 'tests pass'", any(c["type"] == "tests_pass" for c in claims), str(claims))

# Detect claims: build succeeds
claims = detect_claims("The build succeeds without errors.")
check("Detect 'build succeeds'", any(c["type"] == "build_succeeds" for c in claims))

# Detect claims: file created
claims = detect_claims("I created the file `src/main.py` for you.")
check("Detect 'file created'", any(c["type"] == "file_created" for c in claims))
check("Extract file path", any(c["detail"] == "src/main.py" for c in claims if c["type"] == "file_created"))

# Detect claims: file modified
claims = detect_claims("I modified `config.yaml` to add the new setting.")
check("Detect 'file modified'", any(c["type"] == "file_modified" for c in claims))

# Detect claims: no claims
claims = detect_claims("Hello, how can I help you?")
check("No claims in plain text", len(claims) == 0)

# Verify output: no claims → pass
result = verify_output("Just a regular response.")
check("No claims → pass", result.passed and result.checks_run == 0)

# Verify output: file claim, file doesn't exist → fail
result = verify_output("I created `nonexistent_file_xyz.py` for you.", verify_files=True)
check("File not exist → fail", not result.passed)
check("File warning present", result.has_warnings)

# Verify output: file claim, file exists → pass
test_file = os.path.join(test_home, "exists.py")
with open(test_file, "w") as f:
    f.write("# test")
result = verify_output(f"I created `{test_file}` for you.", verify_files=True)
check("File exists → pass", result.passed, result.warnings)

# Verify output: tests pass claim, no tool results → warning
result = verify_output("All tests pass!", tool_results=None)
check("Tests claim no evidence → warning", result.has_warnings)

# Verify output: tests pass claim, with evidence → pass
result = verify_output(
    "All tests pass!",
    tool_results=[{"output": "Ran 5 tests in 0.1s\n\nOK\nAll tests pass"}],
)
check("Tests claim with evidence → pass", result.passed, result.warnings)

# Format warning
warning = format_verification_warning(VerificationResult(passed=False, warnings=["Test warning"]))
check("Warning format", "Verification Warnings" in warning and "Test warning" in warning)

# Empty warning
warning = format_verification_warning(VerificationResult(passed=True))
check("No warning when passed", warning == "")

# -- Prompt Builder --
print("\nPrompt Builder:")
from kyourai.agent.prompt_builder import build_system_prompt

# Basic prompt
prompt = build_system_prompt()
check("Base prompt has identity", "Kyourai" in prompt)
check("Base prompt has memory", "memory" in prompt.lower())

# With memory prompt
prompt = build_system_prompt(memory_prompt="## Memory Context\nYou know about Project X.")
check("Memory prompt included", "Project X" in prompt)

# With tool schemas
prompt = build_system_prompt(tool_schemas=[
    {"name": "terminal", "description": "Execute shell commands"},
    {"name": "read_file", "description": "Read file contents"},
])
check("Tool section included", "terminal" in prompt and "read_file" in prompt)
check("Tool section has header", "## Tools" in prompt)

# With skills
prompt = build_system_prompt(skills_prompt="## Available Skills\n- memory-curator: Manage memory")
check("Skills section included", "memory-curator" in prompt)

# With config preferences
prompt = build_system_prompt(config={
    "agent": {
        "language": "id",
        "response_style": "concise",
        "code_style": "type hints, dataclasses",
    }
})
check("Language preference", "Respond in id" in prompt)
check("Response style preference", "concise" in prompt)
check("Code style preference", "type hints" in prompt)

# With verification enabled
prompt = build_system_prompt(verify_output=True)
check("Verification section included", "Output Verification" in prompt)

# With extra instructions
prompt = build_system_prompt(extra_instructions="Always use Python 3.12.")
check("Extra instructions included", "Python 3.12" in prompt)
check("Extra instructions last", prompt.rstrip().endswith("Python 3.12."))

# Empty config → no preferences section
prompt = build_system_prompt(config={})
check("Empty config → no prefs", "User Preferences" not in prompt)

# -- Plugin System --
print("\nPlugin System:")
from kyourai.agent.plugin_system import (
    PluginManager,
    PluginInfo,
    PluginContext,
    HookRegistry,
    HOOK_TYPES,
)

# Hook registry
hooks = HookRegistry()
check("Hook types defined", len(HOOK_TYPES) >= 6)

# Register and run hooks
called = [0]
def my_hook(output):
    called[0] += 1
    return output.upper()

hooks.register("post_run", my_hook)
results = hooks.run_hooks("post_run", "hello")
check("Hook called", called[0] == 1)
check("Hook result", results == ["HELLO"])

# Unknown hook type
hooks.register("unknown_event", lambda x: x)  # should warn, not crash
check("Unknown hook ignored", len(hooks.get_hooks("unknown_event")) == 0)

# Plugin manager with no plugins dir
pm = PluginManager(plugins_dir=os.path.join(test_home, "nonexistent_plugins"))
discovered = pm.discover()
check("No plugins dir → empty", len(discovered) == 0)

# Create a test plugin
plugins_dir = os.path.join(test_home, "plugins")
os.makedirs(plugins_dir, exist_ok=True)

plugin_code = '''
PLUGIN_METADATA = {
    "name": "test_plugin",
    "version": "1.0.0",
    "description": "A test plugin",
    "author": "Test",
}

def register(ctx):
    """Register plugin hooks."""
    def post_run_hook(output):
        return output + " [plugin-processed]"
    ctx.hooks.register("post_run", post_run_hook)
'''

plugin_path = os.path.join(plugins_dir, "test_plugin.py")
with open(plugin_path, "w") as f:
    f.write(plugin_code)

# Discover plugins
pm = PluginManager(plugins_dir=plugins_dir)
discovered = pm.discover()
check("Plugin discovered", len(discovered) == 1)
check("Plugin name correct", discovered[0].name == "test_plugin")

# Load plugin (need a mock agent)
class MockAgent:
    pass

mock_agent = MockAgent()
loaded = pm.load("test_plugin", mock_agent)
check("Plugin loaded", loaded)
check("Plugin in loaded list", "test_plugin" in pm.get_loaded_plugins())

# Check metadata was read
info = pm.get_plugin_info()[0]
check("Plugin version read", info.version == "1.0.0")
check("Plugin description read", info.description == "A test plugin")

# Run hook
results = pm.run_hooks("post_run", "test output")
check("Plugin hook executed", len(results) == 1)
check("Plugin hook modified output", "[plugin-processed]" in results[0])

# Unload
unloaded = pm.unload("test_plugin")
check("Plugin unloaded", unloaded)
check("Plugin removed from loaded", "test_plugin" not in pm.get_loaded_plugins())

# Plugin with no register function
bad_plugin = os.path.join(plugins_dir, "bad_plugin.py")
with open(bad_plugin, "w") as f:
    f.write("# no register function here\nx = 1\n")

pm2 = PluginManager(plugins_dir=plugins_dir)
pm2.discover()
loaded = pm2.load("bad_plugin", mock_agent)
check("Plugin without register fails", not loaded)

# Plugin that crashes
crash_plugin = os.path.join(plugins_dir, "crash_plugin.py")
with open(crash_plugin, "w") as f:
    f.write("def register(ctx):\n    raise RuntimeError('boom')\n")

pm3 = PluginManager(plugins_dir=plugins_dir)
pm3.discover()
loaded = pm3.load("crash_plugin", mock_agent)
check("Crashing plugin fails gracefully", not loaded)

# Shutdown
pm.shutdown()
check("Shutdown clears hooks", len(pm.hooks.get_hooks("post_run")) == 0)

# -- Agent Integration --
print("\nAgent Integration:")
from kyourai.agent import KyouraiAgent
from pydantic_ai.models.test import TestModel

agent = KyouraiAgent(
    model=TestModel(),
    session_id="tier25-test",
    enable_curator=False,
    enable_skills=False,
    enable_cron=False,
)

# Agent should have verify_output flag
check("Agent has verify_output flag", hasattr(agent, "_verify_output"))
check("Verify default False", agent._verify_output == False)

# Agent should have plugin_manager (lazy)
check("Agent has plugin_manager attr", hasattr(agent, "_plugin_manager"))
check("Plugin manager None initially", agent._plugin_manager is None)

# load_plugins should work (even with no plugins)
infos = agent.load_plugins()
check("load_plugins returns list", isinstance(infos, list))

# Agent should have load_plugins and get_plugins methods
check("Agent has load_plugins", hasattr(agent, "load_plugins"))
check("Agent has get_plugins", hasattr(agent, "get_plugins"))

# System prompt should still work (built with prompt_builder)
check("System prompt not empty", len(agent._system_prompt) > 100)
check("System prompt has Kyourai", "Kyourai" in agent._system_prompt)

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
