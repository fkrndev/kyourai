"""Smoke tests for skills, cron, and API server."""
import json
import os
import tempfile
import time

test_home = tempfile.mkdtemp(prefix="kyourai_features_")
os.environ["KYOURAI_HOME"] = test_home
os.environ["PYTHONIOENCODING"] = "utf-8"

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


print("=== Skills, Cron, API Server Tests ===\n")

# ---------------------------------------------------------------------------
# 1. Skills System
# ---------------------------------------------------------------------------
print("1. Skills System")

from kyourai.skills import SkillLoader, Skill

# Test bundled skills load
loader = SkillLoader()
loader.load_all()
all_skills = loader.all_skills()
check("Bundled skills loaded", len(all_skills) >= 2, f"{len(all_skills)} skills")
check("memory-curator skill found", any(s.name == "memory-curator" for s in all_skills))
check("portable-context skill found", any(s.name == "portable-context" for s in all_skills))

# Test eligible skills
eligible = loader.eligible_skills()
check("Skills are eligible", len(eligible) >= 2)

# Test prompt block generation
prompt = loader.build_prompt_block()
check("Prompt block generated", len(prompt) > 0)
check("Prompt contains skill name", "memory-curator" in prompt)

# Test skill list block
list_block = loader.build_skill_list_block()
check("Skill list block generated", "memory-curator" in list_block)

# Test get_skill
skill = loader.get_skill("memory-curator")
check("get_skill works", skill is not None)
check("Skill has description", skill.description != "")

# Test allowlist
restricted = SkillLoader(allowlist=["memory-curator"])
restricted.load_all()
check("Allowlist filters skills", len(restricted.all_skills()) == 1)
check("Allowlist keeps correct skill", restricted.get_skill("memory-curator") is not None)
check("Allowlist excludes others", restricted.get_skill("portable-context") is None)

# Test $skill reference resolution
refs = loader.resolve_reference("Use $memory-curator and $portable-context to help")
check("Reference resolution works", len(refs) == 2)

# Test custom skill creation
custom_dir = os.path.join(test_home, "skills", "my-custom")
os.makedirs(custom_dir, exist_ok=True)
with open(os.path.join(custom_dir, "SKILL.md"), "w") as f:
    f.write("""---
name: my-custom
description: A custom test skill.
---

# My Custom Skill

Do something useful here.
""")

loader2 = SkillLoader()
loader2.load_all()
check("Custom skill loaded", loader2.get_skill("my-custom") is not None)

# Test gating (missing binary)
gated_dir = os.path.join(test_home, "skills", "gated-skill")
os.makedirs(gated_dir, exist_ok=True)
with open(os.path.join(gated_dir, "SKILL.md"), "w") as f:
    f.write("""---
name: gated-skill
description: A gated skill requiring a missing binary.
metadata:
  openclaw:
    requires:
      bins: ["nonexistent-binary-xyz"]
---

# Gated Skill

This should not load.
""")
loader3 = SkillLoader()
loader3.load_all()
gated = loader3.get_skill("gated-skill")
check("Gated skill not eligible", gated is None or not gated.eligible)

# ---------------------------------------------------------------------------
# 2. Cron Scheduler
# ---------------------------------------------------------------------------
print("\n2. Cron Scheduler")

from kyourai.cron import CronScheduler, CronTask, _compute_next_run

# Test next run computation
next_run = _compute_next_run("0 9 * * *")
check("Next run computed", next_run is not None)

next_run_every_min = _compute_next_run("* * * * *")
check("Every-minute schedule valid", next_run_every_min is not None)

# Test invalid cron
invalid = _compute_next_run("invalid cron")
check("Invalid cron returns None", invalid is None)

# Test add/list/remove
results = []

def fake_agent_fn(prompt, skill):
    results.append(f"agent:{prompt}")
    return f"ran {prompt}"

def fake_curator_fn():
    results.append("curator")
    return {"summary": "ok"}

scheduler = CronScheduler(agent_run_fn=fake_agent_fn, curator_fn=fake_curator_fn)

task = scheduler.add_task("test-curator", "0 9 * * *", action="curator")
check("Task added", task.task_id == "test-curator")
check("Task has next run", task.next_run_at is not None)

tasks = scheduler.list_tasks()
check("Task in list", len(tasks) == 1)

# Test run now
result = scheduler.run_task_now("test-curator")
check("Task runs immediately", result is not None)
check("Curator was called", "curator" in results)

# Verify state updated
task_after = scheduler.get_task("test-curator")
check("Run count incremented", task_after.run_count == 1)
check("Last run recorded", task_after.last_run_at is not None)

# Test disable/enable
scheduler.disable_task("test-curator")
check("Task disabled", not scheduler.get_task("test-curator").enabled)
scheduler.enable_task("test-curator")
check("Task enabled", scheduler.get_task("test-curator").enabled)

# Test remove
scheduler.remove_task("test-curator")
check("Task removed", scheduler.get_task("test-curator") is None)

# Test agent_turn action
task2 = scheduler.add_task("test-agent", "* * * * *", action="agent_turn", prompt="Hello")
result = scheduler.run_task_now("test-agent")
check("Agent turn executed", "agent:Hello" in results)
scheduler.remove_task("test-agent")

# Test persistence (new scheduler loads from disk)
scheduler2 = CronScheduler(agent_run_fn=fake_agent_fn, curator_fn=fake_curator_fn)
# Add a task, then create a new scheduler to verify it loads
scheduler2.add_task("persistent-task", "0 6 * * *", action="curator")
scheduler3 = CronScheduler()
check("Task persisted to disk", scheduler3.get_task("persistent-task") is not None)
scheduler3.remove_task("persistent-task")

# ---------------------------------------------------------------------------
# 3. OpenAI-compatible API Server
# ---------------------------------------------------------------------------
print("\n3. OpenAI-compatible API Server")

from fastapi.testclient import TestClient
from kyourai.api.server import create_app

# Create app with test model (no API key needed)
app = create_app(default_model="test", api_key=None)
client = TestClient(app)

# Test health
resp = client.get("/health")
check("Health endpoint", resp.status_code == 200 and resp.json()["status"] == "ok")

# Test list models
resp = client.get("/v1/models")
check("List models", resp.status_code == 200)
models = resp.json()["data"]
check("Models include kyourai", any(m["id"] == "kyourai" for m in models))
check("Models include kyourai/default", any(m["id"] == "kyourai/default" for m in models))

# Test get model
resp = client.get("/v1/models/kyourai")
check("Get model", resp.status_code == 200 and resp.json()["id"] == "kyourai")

# Test chat completion (non-streaming)
resp = client.post("/v1/chat/completions", json={
    "model": "kyourai",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": False,
})
check("Chat completion status", resp.status_code == 200)
data = resp.json()
check("Chat completion has id", "id" in data)
check("Chat completion has choices", len(data["choices"]) > 0)
check("Chat completion has content", data["choices"][0]["message"]["content"] is not None)

# Test streaming (use TestClient with stream context)
with client.stream("POST", "/v1/chat/completions", json={
    "model": "kyourai",
    "messages": [{"role": "user", "content": "Hi"}],
    "stream": True,
}) as resp:
    check("Streaming status", resp.status_code == 200)
    chunks = []
    for line in resp.iter_lines():
        if line and line.startswith("data: "):
            chunk_data = line[6:]
            if chunk_data == "[DONE]":
                break
            chunks.append(json.loads(chunk_data))
    check("Streaming produces chunks", len(chunks) > 0)
    check("Streaming has content deltas", any("content" in c["choices"][0]["delta"] for c in chunks if c["choices"]))

# Test auth (with API key)
app_auth = create_app(default_model="test", api_key="secret-key")
client_auth = TestClient(app_auth)

resp = client_auth.get("/v1/models")
check("Auth rejects no key", resp.status_code == 401)

resp = client_auth.get("/v1/models", headers={"Authorization": "Bearer wrong"})
check("Auth rejects wrong key", resp.status_code == 401)

resp = client_auth.get("/v1/models", headers={"Authorization": "Bearer secret-key"})
check("Auth accepts correct key", resp.status_code == 200)

# Test embeddings (not supported)
resp = client.post("/v1/embeddings", json={"input": "test", "model": "kyourai"})
check("Embeddings returns 501", resp.status_code == 501)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'='*60}")
print(f"Results: {passed} passed, {failed} failed")
print(f"{'='*60}")
if failed > 0:
    print("FAILED")
    exit(1)
else:
    print("ALL TESTS PASSED")
