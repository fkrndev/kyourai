"""Smoke test for SessionDB + InsightsEngine."""
import os
import tempfile

test_home = tempfile.mkdtemp(prefix="kyourai_state_test_")
os.environ["KYOURAI_HOME"] = test_home

from kyourai.state import SessionDB, InsightsEngine

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


print("=== SessionDB + InsightsEngine Smoke Test ===\n")

# Create DB
db = SessionDB()
check("SessionDB created", db is not None)

# Create session
sid = db.create_session("test-session-1", source="cli", model="openai:gpt-4o", title="Test Session")
check("Session created", sid == "test-session-1")

# Get session
s = db.get_session("test-session-1")
check("Session retrieved", s is not None and s["id"] == "test-session-1")
check("Session has model", s.get("model") == "openai:gpt-4o")
check("Session has source", s.get("source") == "cli")

# Add messages
mid1 = db.add_message("test-session-1", role="user", content="How do I deploy to Kubernetes?")
mid2 = db.add_message("test-session-1", role="assistant", content="You can use Helm charts to deploy to Kubernetes.")
check("Messages added", mid1 > 0 and mid2 > 0)

# Add turn (convenience)
db.add_turn("test-session-2", "What is Python?", "Python is a programming language.")
check("Turn added (auto-create session)", db.get_session("test-session-2") is not None)

# Get messages
msgs = db.get_messages("test-session-1")
check("Get messages returns results", len(msgs) == 2)
check("Messages in order", msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant")

# Session aggregates updated
s = db.get_session("test-session-1")
check("Session message_count updated", s["message_count"] == 2)

# List sessions
sessions = db.list_sessions(limit=10)
check("List sessions returns results", len(sessions) >= 2)
check("List newest first", sessions[0]["id"] == "test-session-2")

# FTS search
results = db.search_messages("Kubernetes")
check("FTS search returns results", len(results) > 0)
check("FTS search finds correct content", "Kubernetes" in results[0].get("content", ""))

# FTS search with no match
results = db.search_messages("nonexistent_xyz123")
check("FTS search no match returns empty", len(results) == 0)

# Count sessions
count = db.count_sessions()
check("Count sessions", count >= 2)

# End session
ended = db.end_session("test-session-1")
check("End session succeeds", ended)
s = db.get_session("test-session-1")
check("Session has ended_at", s["ended_at"] is not None)

# Update session
updated = db.update_session("test-session-1", title="Updated Title")
check("Update session succeeds", updated)
s = db.get_session("test-session-1")
check("Session title updated", s["title"] == "Updated Title")

# -- InsightsEngine --
print("\nInsights Engine:")
engine = InsightsEngine(db)

# Generate report (365 days to include our test data)
report = engine.generate(days=365)
check("Report generated", report is not None)
check("Report not empty", not report.get("empty"))

ov = report.get("overview", {})
check("Overview has total_sessions", ov.get("total_sessions", 0) >= 2)
check("Overview has total_messages", ov.get("total_messages", 0) >= 4)
check("Overview has avg_messages", ov.get("avg_messages_per_session", 0) > 0)

models = report.get("models", [])
check("Models breakdown has entries", len(models) > 0)
check("Models breakdown has openai:gpt-4o", any(m["model"] == "openai:gpt-4o" for m in models))

sources = report.get("sources", [])
check("Sources breakdown has entries", len(sources) > 0)

activity = report.get("activity", {})
check("Activity has by_day", "by_day" in activity)

top = report.get("top_sessions", [])
check("Top sessions has entries", len(top) > 0)

# Empty report (0 days = no data)
empty_report = engine.generate(days=0)
check("Empty report handled", empty_report.get("empty") is True)

# -- Cleanup --
db.delete_session("test-session-1")
db.delete_session("test-session-2")
check("Delete session works", db.get_session("test-session-1") is None)

db.close()
check("DB closed without error", True)

print(f"\n{'='*60}")
print(f"Results: {passed} passed, {failed} failed")
print(f"{'='*60}")
if failed == 0:
    print("ALL TESTS PASSED")
else:
    print("SOME TESTS FAILED")
    import sys
    sys.exit(1)
