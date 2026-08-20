"""Smoke test for the holographic memory store + retrieval."""
import json
import os
import tempfile

test_home = tempfile.mkdtemp(prefix="kyourai_holo_")
os.environ["KYOURAI_HOME"] = test_home

from kyourai.memory.holographic.store import MemoryStore
from kyourai.memory.holographic.retrieval import FactRetriever
from kyourai.memory.holographic.provider import HolographicMemoryProvider

# Create store and add facts
store = MemoryStore(hrr_dim=1024)
retriever = FactRetriever(store=store, hrr_dim=1024)

# Add facts with entities
fid1 = store.add_fact("Andi works as a backend engineer at Tokopedia", category="user_pref")
fid2 = store.add_fact("The deployment uses Kubernetes with Helm charts", category="project")
fid3 = store.add_fact("Andi prefers Python over JavaScript for backend services", category="user_pref")
fid4 = store.add_fact("The API gateway is written in Go and deployed on Kubernetes", category="project")

print("Added facts:", fid1, fid2, fid3, fid4)

# Test keyword search
results = retriever.search("Python backend", limit=5)
print("\nSearch 'Python backend':", len(results), "results")
for r in results:
    print("  -", r["content"][:60], "(score:", round(r.get("score", 0), 3), ")")

# Test entity probe
results = retriever.probe("Andi", limit=5)
print("\nProbe 'Andi':", len(results), "results")
for r in results:
    print("  -", r["content"][:60], "(score:", round(r.get("score", 0), 3), ")")

# Test related
results = retriever.related("Kubernetes", limit=5)
print("\nRelated to 'Kubernetes':", len(results), "results")
for r in results:
    print("  -", r["content"][:60], "(score:", round(r.get("score", 0), 3), ")")

# Test multi-entity reason
results = retriever.reason(["Andi", "Kubernetes"], limit=5)
print("\nReason(['Andi', 'Kubernetes']):", len(results), "results")
for r in results:
    print("  -", r["content"][:60], "(score:", round(r.get("score", 0), 3), ")")

# Test trust feedback
feedback = store.record_feedback(fid1, helpful=True)
print("\nFeedback on fact", fid1, ":", feedback)

# Test list
facts = store.list_facts(limit=10)
print("\nList facts:", len(facts), "total")

# Test the provider wrapper
provider = HolographicMemoryProvider(config={"hrr_dim": 1024})
provider.initialize("test-session")

# Test fact_store tool
result = json.loads(provider.handle_tool_call("fact_store", {"action": "search", "query": "Python"}))
print("\nProvider fact_store search:", result.get("count"), "results")

result = json.loads(provider.handle_tool_call("fact_store", {"action": "probe", "entity": "Andi"}))
print("Provider fact_store probe:", result.get("count"), "results")

# Test fact_feedback tool
result = json.loads(provider.handle_tool_call("fact_feedback", {"action": "helpful", "fact_id": fid2}))
print("Provider fact_feedback:", result)

# Test system prompt block
prompt = provider.system_prompt_block()
print("\nSystem prompt block length:", len(prompt))
print("Contains 'Holographic':", "Holographic" in prompt)

provider.shutdown()
store.close()

print("\nAll holographic memory tests passed!")
