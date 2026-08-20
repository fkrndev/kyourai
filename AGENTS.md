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
- `kyourai/api/server.py` — OpenAI-compatible API server (/v1/chat/completions)
- `kyourai/agent.py` — KyouraiAgent (Pydantic AI + memory + skills + cron wiring)
- `kyourai/cli.py` — CLI entry point (Click + Rich)
- `kyourai/config.py` — Config loader (config.yaml)
- `kyourai/constants.py` — Path resolution ($KYOURAI_HOME)

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
