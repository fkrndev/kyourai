"""Smoke test for Tier 2: error classifier, retry, rate limit, title gen, empty guard, subagent."""
import os
import tempfile
import asyncio

test_home = tempfile.mkdtemp(prefix="kyourai_tier2_test_")
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


print("=== Tier 2: Error Handling + Retry + Rate Limit + Title + Subagent ===\n")

# -- Error Classifier --
print("Error Classifier:")
from kyourai.agent.error_classifier import classify_error, ErrorCategory

# Retryable: timeout
c = classify_error(TimeoutError("connection timed out"))
check("Timeout is retryable", c.category == ErrorCategory.RETRYABLE, c.message)

# Retryable: connection error
c = classify_error(ConnectionError("connection refused"))
check("Connection refused is retryable", c.category == ErrorCategory.RETRYABLE)

# Fatal: auth error
c = classify_error(Exception("invalid api key"))
check("Invalid API key is fatal", c.category == ErrorCategory.FATAL)

# Fatal: content policy
c = classify_error(Exception("content policy violation"))
check("Content policy is fatal", c.category == ErrorCategory.FATAL)

# Rate limited: 429
c = classify_error(Exception("rate limited"), status_code=429)
check("429 is rate_limited", c.category == ErrorCategory.RATE_LIMITED)

# Retryable: 503
c = classify_error(Exception("service unavailable"), status_code=503)
check("503 is retryable", c.category == ErrorCategory.RETRYABLE)

# Fatal: 401
c = classify_error(Exception("unauthorized"), status_code=401)
check("401 is fatal", c.category == ErrorCategory.FATAL)

# Retry-after parsing
c = classify_error(Exception("rate limited"), status_code=429, response_body="retry-after: 30")
check("Retry-after parsed", c.retry_after == 30.0)

# Unknown error
c = classify_error(RuntimeError("something weird"))
check("Unknown is unknown/retryable", c.category in (ErrorCategory.UNKNOWN, ErrorCategory.RETRYABLE))

# -- Retry Utils --
print("\nRetry Utils:")
from kyourai.agent.retry_utils import retry_with_backoff, retry_sync

# Successful on first try
async def test_success():
    call_count = [0]
    async def fn():
        call_count[0] += 1
        return "ok"
    result = await retry_with_backoff(fn, max_retries=3, base_delay=0.01)
    return result, call_count[0]

result, count = asyncio.run(test_success())
check("Success on first try", result == "ok" and count == 1)

# Retry on transient then succeed
async def test_retry_then_success():
    call_count = [0]
    async def fn():
        call_count[0] += 1
        if call_count[0] < 2:
            raise ConnectionError("connection refused")
        return "ok"
    result = await retry_with_backoff(fn, max_retries=3, base_delay=0.01)
    return result, call_count[0]

result, count = asyncio.run(test_retry_then_success())
check("Retry then success", result == "ok" and count == 2)

# Fatal error — no retry
async def test_fatal_no_retry():
    call_count = [0]
    async def fn():
        call_count[0] += 1
        raise Exception("invalid api key")
    try:
        await retry_with_backoff(fn, max_retries=3, base_delay=0.01)
        return "no_error", call_count[0]
    except Exception:
        return "raised", call_count[0]

result, count = asyncio.run(test_fatal_no_retry())
check("Fatal error no retry", result == "raised" and count == 1)

# Sync retry
def test_sync_retry():
    call_count = [0]
    def fn():
        call_count[0] += 1
        if call_count[0] < 2:
            raise ConnectionError("timeout")
        return "ok"
    result = retry_sync(fn, max_retries=3, base_delay=0.01)
    return result, call_count[0]

result, count = test_sync_retry()
check("Sync retry works", result == "ok" and count == 2)

# -- Rate Limit Tracker --
print("\nRate Limit Tracker:")
from kyourai.agent.rate_limit_tracker import RateLimitTracker

tracker = RateLimitTracker(limits={"test": 3, "default": 2})

# Can request initially
check("Can request initially", tracker.can_request("test"))

# Record requests up to limit
tracker.record("test")
tracker.record("test")
tracker.record("test")  # 3 requests (limit * 0.8 = 2, so 3rd should be blocked)
check("Over limit blocked", not tracker.can_request("test"))

# Different provider not affected
check("Different provider ok", tracker.can_request("default"))

# Status
status = tracker.get_status("test")
check("Status has count", status["requests_in_window"] >= 3)
check("Status has limit", status["limit"] > 0)

# Reset
tracker.reset("test")
check("Reset works", tracker.can_request("test"))

# -- Empty Response Guard --
print("\nEmpty Response Guard:")
from kyourai.agent.empty_response_guard import guard_response, is_empty_response

check("None is empty", is_empty_response(None))
check("Empty string is empty", is_empty_response(""))
check("Whitespace is empty", is_empty_response("   \n\t  "))
check("Punctuation only is empty", is_empty_response("...,,,!!!"))
check("Real content not empty", not is_empty_response("Hello world"))

# Guard: empty → should retry
output, should_retry = guard_response("", retry_count=0)
check("Empty triggers retry", should_retry == True)

# Guard: empty after max retries → fallback
output, should_retry = guard_response("", retry_count=2, max_retries=2)
check("Empty after max → fallback", should_retry == False and "apologize" in output.lower())

# Guard: non-empty → no retry
output, should_retry = guard_response("Hello!", retry_count=0)
check("Non-empty no retry", should_retry == False and output == "Hello!")

# -- Title Generator --
print("\nTitle Generator:")
from kyourai.agent.title_generator import generate_title_sync, generate_title

# Sync fallback
title = generate_title_sync("How do I deploy a Python app to Kubernetes?", "Use Helm charts...")
check("Sync title generated", len(title) > 0)
check("Sync title reasonable", "deploy" in title.lower() or "python" in title.lower() or "kubernetes" in title.lower())

# Short message
title = generate_title_sync("Hi", "Hello!")
check("Short message title", len(title) > 0)

# Empty message
title = generate_title_sync("", "")
check("Empty message fallback", "untitled" in title.lower())

# Async with no model (fallback)
async def test_async_title():
    return await generate_title("Fix memory leak in worker", "Try using gc.collect...", model=None)

title = asyncio.run(test_async_title())
check("Async fallback title", len(title) > 0)

# -- Subagent Delegation --
print("\nSubagent Delegation:")
from kyourai.agent.subagent import SubagentDelegator, DelegationResult
from kyourai.agent import KyouraiAgent
from pydantic_ai.models.test import TestModel

agent = KyouraiAgent(
    model=TestModel(),
    session_id="subagent-test-parent",
    enable_curator=False,
    enable_skills=False,
    enable_cron=False,
)

# Delegate a task
async def test_delegate():
    result = await agent.delegate("Say hello", timeout=10.0)
    return result

result = asyncio.run(test_delegate())
check("Delegation returns result", isinstance(result, DelegationResult))
check("Delegation has task", result.task == "Say hello")
check("Delegation has session_id", result.session_id.startswith("subagent-"))
check("Delegation success", result.success, result.error or "")

# Delegate batch
async def test_delegate_batch():
    results = await agent.delegate_batch(["Task 1", "Task 2", "Task 3"], timeout=10.0)
    return results

results = asyncio.run(test_delegate_batch())
check("Batch delegation returns 3", len(results) == 3)
check("All batch results successful", all(r.success for r in results))

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
