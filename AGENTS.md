# AGENTS.md — Kyourai project guide

## Build & install

```bash
pip install -e ".[mcp,dev]"
```

## Test commands

```bash
# End-to-end memory round-trip (37 checks)
python tests/test_e2e.py

# Skills + Cron + API server (46 checks)
python tests/test_features.py

# Individual smoke tests
python tests/smoke_builtin.py       # builtin memory (frozen snapshot)
python tests/smoke_holographic.py   # HRR store + retrieval
python tests/smoke_curator.py       # curator maintenance loop
python tests/smoke_team.py          # team-native layer + RBAC
python tests/smoke_portable_context.py  # KPC export/import
python tests/smoke_manager.py       # MemoryManager orchestrator
python tests/smoke_agent.py         # Pydantic AI agent core
python tests/smoke_state.py         # SessionDB + InsightsEngine (33 checks)
python tests/smoke_tools.py         # Core tools + context compression (42 checks)
python tests/smoke_tier2.py         # Error handling + retry + rate limit + subagent (38 checks)
python tests/smoke_tier25.py        # Verification + prompt builder + plugins (54 checks)
```

All tests use temp directories and clean up after themselves.

## Architecture overview

- `kyourai/memory/provider.py` — MemoryProvider ABC + RecallStatus
- `kyourai/memory/builtin.py` — BuiltinMemoryProvider (frozen snapshot, MEMORY.md/USER.md)
- `kyourai/memory/holographic/hrr.py` — HRR vector algebra (bind/unbind/bundle/encode)
- `kyourai/memory/holographic/store.py` — SQLite fact store with entity resolution + trust scoring
- `kyourai/memory/holographic/retrieval.py` — Hybrid FTS5/Jaccard/HRR retrieval + compositional queries
- `kyourai/memory/holographic/provider.py` — HolographicMemoryProvider (MemoryProvider impl)
- `kyourai/memory/manager.py` — MemoryManager orchestrator
- `kyourai/memory/curator.py` — Background memory maintenance (contradictions, trust decay)
- `kyourai/team/manager.py` — Team-native layer (shared/private memory, RBAC)
- `kyourai/mcp/portable_context.py` — KPC format (export/import)
- `kyourai/mcp/server.py` — MCP server (exposes memory to other agents)
- `kyourai/skills/loader.py` — Skills system (SKILL.md loader, gating, allowlists)
- `kyourai/skills/bundled/` — Bundled skills (memory-curator, portable-context)
- `kyourai/cron/__init__.py` — Cron scheduler (persistent scheduled tasks)
- `kyourai/tools/registry.py` — Tool auto-discovery and registration
- `kyourai/tools/terminal.py` — Shell command execution (safety checks, timeout, truncation)
- `kyourai/tools/read_file.py` — File reading (path validation, binary detection, size limit)
- `kyourai/tools/web_search.py` — DuckDuckGo web search (no API key required)
- `kyourai/context/compressor.py` — Context compression (token estimation, summary generation)
- `kyourai/state/db.py` — SessionDB (SQLite + FTS5 session/message store)
- `kyourai/state/insights.py` — InsightsEngine (usage analytics over sessions)
- `kyourai/api/server.py` — OpenAI-compatible API server (/v1/chat/completions, /v1/sessions, /v1/insights)
- `dashboard/` — Next.js dashboard (TypeScript + Tailwind, 4 tabs, API proxy to FastAPI)
- `kyourai/agent/_main.py` — KyouraiAgent (Pydantic AI + memory + skills + cron + session + tools + compression + retry + rate limit)
- `kyourai/agent/error_classifier.py` — Error classification (retryable vs fatal)
- `kyourai/agent/retry_utils.py` — Retry with exponential backoff + jitter
- `kyourai/agent/rate_limit_tracker.py` — Per-provider sliding window rate limiter
- `kyourai/agent/empty_response_guard.py` — Handle empty/whitespace LLM responses
- `kyourai/agent/title_generator.py` — Auto-title sessions from first exchange
- `kyourai/agent/subagent.py` — Subagent delegation (parallel task execution)
- `kyourai/agent/verification.py` — Output verification (claim detection, file existence, evidence check)
- `kyourai/agent/prompt_builder.py` — Dynamic system prompt builder (modular, data-driven)
- `kyourai/agent/plugin_system.py` — Plugin manager + hook registry (third-party extensions)
- `kyourai/cli.py` — CLI entry point (Click + Rich)
- `kyourai/config.py` — Config loader (config.yaml)
- `kyourai/constants.py` — Path resolution ($KYOURAI_HOME)
- `kyourai/logging.py` — Profile-aware logging (agent.log + errors.log)

## Key design decisions

1. **Frozen snapshot**: BuiltinMemoryProvider captures MEMORY.md/USER.md at init.
   Mid-session writes are saved to disk but don't appear in the system prompt
   until next session. This prevents context thrash.

2. **HRR over embeddings**: Holographic memory uses HRR vector algebra instead
   of embedding APIs. Enables compositional queries (probe, related, reason,
   contradict) that embedding databases cannot do. Self-hosted, no API costs.

3. **Trust scoring**: Facts have trust scores [0,1] adjusted by asymmetric
   feedback (helpful +0.05, unhelpful -0.10). Trust multiplies retrieval scores.

4. **Team isolation**: Memory is scoped by directory structure. KYOURAI_HOME
   is temporarily swapped per request via TeamMemoryRouter.

5. **Portable context**: KPC format is JSON with SHA-256 checksum. Supports
   skip_duplicates, overwrite, and append merge strategies.

6. **Session persistence**: SessionDB (SQLite + FTS5) stores every conversation
   turn. Enables session history browsing, full-text search across past
   conversations, and usage analytics via InsightsEngine. Schema is versioned
   for forward migration. Shared connection registry prevents write contention.

7. **Core tools**: terminal (shell exec with safety blocklist + timeout),
   read_file (path validation + binary detection), web_search (DuckDuckGo,
   no API key). Auto-discovered via tools/registry.py. Only fundamental,
   broadly useful tools belong here — niche capability should be a skill.

8. **Context compression**: When message history exceeds 80% of the model's
   context window, older messages are summarized into a single system message
   while keeping the N most recent verbatim. System prompt is never touched —
   only message_history is compressed, preserving prompt cache.

9. **Prompt caching**: System prompt is built once in __init__ and stays
   byte-stable for the conversation lifetime. Memory prefetch is NOT appended
   to the user prompt (that would mutate the prefix and invalidate cache).
   Memory context lives in the frozen system prompt (builtin snapshot) or is
   retrieved via tools (fact_store) during the conversation.

10. **Error handling**: Errors are classified as retryable (timeout, 429, 5xx),
    rate-limited (429 with retry-after), or fatal (401, 403, content policy).
    Retryable errors trigger exponential backoff with jitter. Rate limits are
    tracked per-provider via sliding window. Empty LLM responses are detected
    and retried up to 2 times before returning a fallback message.

11. **Subagent delegation**: The main agent can delegate subtasks to independent
    subagent instances, each with its own session. Supports batch delegation
    for parallel task execution. Results collected as DelegationResult.

12. **Web dashboard**: Next.js dashboard (`dashboard/`) with TypeScript +
    Tailwind. 4 tabs: Insights (cards + activity chart), Sessions (list +
    detail), Search (FTS5), Chat (live). Pre-built static export in
    `dashboard/out/` is served by FastAPI at `/` — no Node.js required for
    end users. Developer workflow: `cd dashboard && npm run dev` (hot reload
    with API proxy), `npm run build:static` to rebuild the export.

13. **Output verification**: When `agent.verify_output` is enabled in config,
    the agent's response is scanned for verifiable claims (tests pass, build
    succeeds, file created/modified). Claims are checked against tool results
    and filesystem. Warnings appended to output if claims can't be verified.
    Policy-only — never runs code itself, just checks evidence.

14. **Dynamic system prompt builder**: System prompt is assembled from modular
    components: base identity, memory context, tool descriptions, skills,
    user preferences (language, response style, code style), verification
    instructions, and extra instructions. Built ONCE at init, byte-stable
    for conversation lifetime (prompt caching invariant).

15. **Plugin system**: Plugins live in ~/.kyourai/plugins/ and are discovered
    at startup. Each plugin exports a register(ctx) function and optional
    PLUGIN_METADATA dict. Plugins can register hooks for agent events
    (pre_run, post_run, pre_tool, post_tool, session_start/end, memory_sync).
    Same trust model as pip install — only install plugins you trust.
