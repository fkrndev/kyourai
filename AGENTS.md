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
- `kyourai/api/server.py` — OpenAI-compatible API server (/v1/chat/completions, /v1/sessions, /v1/insights, /v1/health/detailed)
- `kyourai/providers/__init__.py` — Multi-provider adapters (OpenAI, Anthropic, Google, Bedrock, Ollama, Groq, Mistral)
- `kyourai/tui/__init__.py` — Terminal UI (Textual) — split pane chat + memory sidebar
- `kyourai/production.py` — Production hardening (config validation, graceful shutdown, health checks, structured logging)
- `kyourai/security/redaction.py` — Credential redaction (API keys, tokens, PII scanning)
- `kyourai/context/coding.py` — Coding context detection (git, language, framework → system prompt)
- `kyourai/context/sanitizer.py` — Message sanitization (role alternation, control chars, validation)
- `kyourai/usage.py` — Usage/pricing tracker (token usage + cost estimation per session)
- `kyourai/mcp/catalog.py` — MCP server catalog (discovery, registration, connection)
- `kyourai/tools/shell_hooks.py` — Shell hooks (pre/post command hooks: block, warn, run, log)
- `kyourai/tools/policy.py` — Tool policy system (allow/deny, sandbox, availability conditions)
- `kyourai/tools/goals.py` — Goal management (track objectives with progress, sub-goals, priorities)
- `kyourai/tools/link_understanding.py` — URL extraction with SSRF protection + safe fetching
- `kyourai/agent/subagent_enhanced.py` — Enhanced subagent system (registry, lifecycle, spawn modes, tool policy inheritance)
- `kyourai/tasks/flows.py` — Task flow orchestration (multi-step tasks, revision-based concurrency, SQLite persistence)
- `kyourai/trajectory.py` — Trajectory recording (bounded session events, payload sanitization, export)
- `kyourai/security/content.py` — External content security (prompt injection detection, content wrapping, homoglyph detection)
- `kyourai/secrets/resolver.py` — Multi-source secret resolution (env, file, exec, store with security validation)
- `kyourai/snapshot.py` — Snapshot/backup system (SQLite snapshots with SHA256 integrity, git backup)
- `kyourai/audit.py` — Audit event system (non-blocking queue, execution identity, SQLite persistence)
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
    detail), Search (FTS5), Chat (live). Runs on its own port (3000) and
    proxies API calls to FastAPI via next.config rewrites. Developer workflow:
    `kyourai serve` + `cd dashboard && npm run dev`.

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

16. **Multi-provider adapters**: `kyourai/providers/` wraps pydantic-ai's
    provider system with unified model string parsing (`provider:model`),
    API key validation, and fallback chains. Supports 7 providers: OpenAI,
    Anthropic, Google Gemini, AWS Bedrock, Ollama (local), Groq, Mistral.
    `kyourai providers` command lists all providers with API key status.

17. **Terminal UI (TUI)**: `kyourai tui` starts a Textual-based TUI with
    split-pane layout: chat log (left), memory sidebar showing recent facts
    with trust bars (right), input field (bottom). Keyboard shortcuts:
    Ctrl+M toggle memory, Ctrl+L clear, Ctrl+C quit. Falls back to TestModel
    if no API key configured.

18. **MCP server catalog**: `kyourai mcp` command group for discovering,
    registering, and connecting to external MCP servers. 10 bundled server
    templates (filesystem, sqlite, postgres, github, brave-search, puppeteer,
    memory, time, fetch, sequential-thinking). Catalog persisted in
    `~/.kyourai/mcp_catalog.json`. Auto-connect support for agent startup.

19. **Production hardening**: `kyourai/production.py` provides:
    - Config validation + migration (validate config.yaml at startup)
    - Graceful shutdown (SIGTERM/SIGINT handling, cleanup callbacks)
    - Detailed health check (DB, memory, home dir, config status)
    - Structured logging (JSON format for observability)
    CLI: `kyourai health`, `kyourai config-validate`.
    API: `/v1/health/detailed`.

20. **Credential redaction**: `kyourai/security/redaction.py` scans text
    for API keys (OpenAI, Anthropic, AWS, Google, Stripe), tokens (GitHub,
    GitLab, Slack, JWT, Bearer), private keys, and passwords. Replaces
    with `[REDACTED:type]` placeholders. Integrated into API server —
    all incoming messages are redacted before processing. Also provides
    `scan_for_secrets()` for auditing without redaction.

21. **Coding context detection**: `kyourai/context/coding.py` detects
    git repo (branch, status, remote), languages (by file extension count),
    frameworks (Python, JS, Rust, Go — by config files and dependencies),
    package managers, test frameworks, linters. Injected into system prompt
    at agent init. CLI: `kyourai context`.

22. **Usage/pricing tracker**: `kyourai/usage.py` tracks token usage
    (prompt_tokens, completion_tokens) and estimates cost per session.
    Pricing table for 30+ models across 7 providers. Persisted in session
    DB. CLI: `kyourai usage [--by-model]`.

23. **Message sanitization**: `kyourai/context/sanitizer.py` ensures
    message history is well-formed: strict role alternation, merge
    consecutive same-role messages, strip control characters, truncate
    long messages, ensure system prompt is first. `validate_messages()`
    for auditing.

24. **Advanced context compressor**: Multi-strategy compression in
    `kyourai/context/compressor.py`:
    - `sliding_window`: Keep only N recent messages
    - `importance`: Score messages by code blocks, tool calls, errors → keep top
    - `semantic`: Cluster by topic similarity → keep representatives
    - `summarize`: Full LLM summarization (default, most context preserved)

25. **Shell hooks**: `kyourai/tools/shell_hooks.py` — pre/post hooks for
    terminal commands. Actions: block (prevent dangerous commands), warn,
    run (execute post-commands like auto-test), log, notify. Pattern-based
    matching with regex. Configurable via `config.yaml` under `shell_hooks`.

26. **Enhanced subagent system**: `kyourai/agent/subagent_enhanced.py` —
    SubagentRegistry tracks all subagent runs with lifecycle states
    (pending → running → succeeded/failed/cancelled/timed_out/lost).
    Spawn modes: run (fire-and-forget) and collect (swarm parallel).
    Tool policy inheritance from parent. Depth limiting (max 3) and
    children limiting (max 5 per agent). Controller scope for session tree.

27. **Tool policy system**: `kyourai/tools/policy.py` — allow/deny lists
    with wildcard support, sandbox mode (workspace-only file access),
    availability conditions (env, config, auth, plugin-enabled), tool
    descriptors with owner tracking, policy composition/merge. Preset
    policies: SUBAGENT_SAFE_POLICY, READONLY_POLICY, SANDBOX_POLICY.

28. **Task flow orchestration**: `kyourai/tasks/flows.py` — multi-step
    task flows with revision-based optimistic concurrency, status lifecycle
    (queued → running → waiting → blocked → succeeded/failed/cancelled),
    delivery state tracking, parent-child relationships, SQLite persistence.

29. **Trajectory recording**: `kyourai/trajectory.py` — bounded session
    event recording to SQLite. Payload sanitization (secret redaction,
    size truncation, depth limits). Export to JSON bundles for debugging.
    Per-session database isolation. Automatic cleanup of old trajectories.

30. **External content security**: `kyourai/security/content.py` — prompt
    injection detection (6 pattern types), external content wrapping with
    random boundary markers, LLM special token sanitization, Unicode
    homoglyph detection/normalization, comprehensive content analysis.

31. **Multi-source secret resolution**: `kyourai/secrets/resolver.py` —
    resolve secrets from env, file (with JSON pointer), exec (command),
    store (custom providers), or plain text. Security validation for
    file access (permission checks) and exec commands (dangerous pattern
    blocking, allowlist). Caching with TTL.

32. **Link understanding**: `kyourai/tools/link_understanding.py` — URL
    extraction from text, SSRF protection (blocks private IPs, loopback,
    metadata endpoints, non-HTTP schemes), safe URL fetching with timeout
    and size limits, HTML text extraction.

33. **Goal management**: `kyourai/tools/goals.py` — track agent goals
    with status (active/completed/abandoned/deferred/blocked), priority
    (low/medium/high/critical), progress (0-100%), sub-goal hierarchy,
    tags, blockers, SQLite persistence.

34. **Snapshot/backup system**: `kyourai/snapshot.py` — SQLite database
    snapshots with SHA256 integrity verification, atomic writes with
    staging directories, sidecar file handling (WAL/SHM/journal),
    security hardening (private file modes), restore with verification.

35. **Audit event system**: `kyourai/audit.py` — non-blocking audit trail
    with background writer thread, execution identity context (thread-local),
    SQLite persistence with automatic pruning, severity levels, query API
    with filtering, statistics.
