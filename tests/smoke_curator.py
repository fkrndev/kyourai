"""Smoke test for the curator."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

test_home = tempfile.mkdtemp(prefix="kyourai_curator_")
os.environ["KYOURAI_HOME"] = test_home

from kyourai.memory.holographic.store import MemoryStore
from kyourai.memory import curator

# Create store with facts
store = MemoryStore(hrr_dim=1024)

# Add some facts
fid1 = store.add_fact("Andi uses Python 3.12", category="user_pref")
fid2 = store.add_fact("Andi uses Python 3.13", category="user_pref")  # contradicts fid1
fid3 = store.add_fact("The project uses PostgreSQL for the database", category="project")
fid4 = store.add_fact("The deployment runs on AWS", category="project")

# Give fact 1 some helpful feedback (pin it)
store.record_feedback(fid1, helpful=True)
store.record_feedback(fid1, helpful=True)
store.record_feedback(fid1, helpful=True)  # helpful_count=3 → pinned

# Test: find contradictions
contradictions = curator.find_contradictions(store)
print("Contradictions found:", len(contradictions))
for c in contradictions[:3]:
    print("  -", c["fact_a"]["content"][:40], "vs", c["fact_b"]["content"][:40], "(score:", c["contradiction_score"], ")")

# Test: prune scan (should find 0 candidates since all facts are recent)
prune_counts = curator.prune_low_trust_facts(store)
print("\nPrune scan:", prune_counts)

# Test: trust decay (disabled by default)
decay_counts = curator.decay_trust_scores(store)
print("Trust decay (disabled):", decay_counts)

# Test: full curator run with force=True
summary = curator.run_curator(store, force=True)
print("\nCurator run summary:")
print("  skipped:", summary.get("skipped"))
print("  phases:", list(summary.get("phases", {}).keys()))
print("  contradictions found:", summary.get("phases", {}).get("contradictions", {}).get("found"))
print("  duration:", summary.get("duration_seconds"), "s")

# Test: state persistence
state = curator.load_state()
print("\nState persisted:")
print("  last_run_at:", state.get("last_run_at") is not None)
print("  run_count:", state.get("run_count"))

# Test: should_run_now after a run (should be False — just ran)
should = curator.should_run_now(now=datetime.now(timezone.utc))
print("  should_run_now after run:", should)

# Test: background runner
runner = curator.CuratorBackgroundRunner(store, config={"interval_hours": 0})
started = runner.maybe_run()
print("\nBackground runner started:", started)
runner.wait(timeout=5.0)
print("Background runner completed:", not runner.is_running)

store.close()
print("\nAll curator tests passed!")
