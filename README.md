# Kyourai (記憶雷)

**Memory-first AI agent with team-native shared context and portable MCP memory.**

Kyourai (Japanese: 記憶 *kioku* "memory" + 雷 *rai* "lightning") is a self-hosted
AI agent built on [Pydantic AI](https://ai.pydantic.dev) that takes the memory
architecture from [Hermes Agent](https://github.com/NousResearch/hermes-agent)
and extends it with two capabilities the ecosystem is missing:

1. **Team-native memory** — shared + private memory for small teams (2–20 people),
   with lightweight RBAC and per-user isolation. No agent in the market does this
   well today.
2. **Portable context via MCP** — memory exports/imports through a standard MCP
   server, so your context follows you across agents (Claude Code, Cursor, OpenClaw,
   Hermes, …) instead of being locked into one tool.

## Memory architecture

| Layer | Component | Kyourai module |
|-------|-----------|----------------|
| 1 | Frozen-snapshot file memory (`MEMORY.md` / `USER.md`) | `kyourai.memory.builtin` |
| 2 | Pluggable memory providers (ABC + plugin system) | `kyourai.memory.provider` |
| 2a | Holographic HRR vector memory (self-hosted, no embedding API) | `kyourai.memory.holographic` |
| 3 | Memory manager orchestrator (prefetch, sync, tool routing) | `kyourai.memory.manager` |
| 4 | Curator — background memory maintenance (contradictions, trust decay) | `kyourai.memory.curator` |
| **T** | **Team-native layer** (shared + private memory, RBAC) | `kyourai.team` |
| **P** | **Portable context MCP server** (export/import across agents) | `kyourai.mcp` |

### Holographic HRR memory

The holographic memory uses Holographic Reduced Representations (HRR) — a
compositional vector algebra that enables queries no embedding database can do:

- **`probe(entity)`** — algebraically extract all facts about an entity
- **`related(entity)`** — find structurally adjacent facts
- **`reason([entity1, entity2])`** — compositional JOIN across multiple entities
- **`contradict()`** — find facts making conflicting claims (entity overlap + content divergence)

All vectors are stored as phase-encoded byte arrays in SQLite — no external
embedding API required. Falls back gracefully when numpy is unavailable.

## Quick start

```bash
pip install -e ".[mcp,dev]"
kyourai config init
kyourai chat
```

### CLI commands

```
kyourai chat                  # interactive chat with the agent
kyourai serve                 # start OpenAI-compatible API server
kyourai memory list           # list memory entries
kyourai memory add            # add a memory entry
kyourai memory search <query> # search holographic facts
kyourai memory facts          # list holographic facts
kyourai memory export <path>  # export memory to KPC file
kyourai memory import <path>  # import memory from KPC file
kyourai skills list           # list loaded skills
kyourai skills show <name>    # show skill content
kyourai skills create         # create a new skill scaffold
kyourai cron list             # list scheduled tasks
kyourai cron add              # add a scheduled task
kyourai cron remove <id>      # remove a task
kyourai cron run <id>         # run a task immediately
kyourai cron enable <id>      # enable a task
kyourai cron disable <id>     # disable a task
kyourai team create           # create a new team
kyourai team list             # list teams
kyourai team add-member       # add a member to a team
kyourai curator run           # run the curator manually
kyourai curator status        # show curator state
kyourai mcp-server            # start the MCP server (for other agents)
kyourai config show           # show current config
kyourai config init           # create a default config.yaml
```

### Team mode

```bash
# Admin creates a team
kyourai team create --name "Engineering" --user-id andi --display-name "Andi"

# Add members
kyourai team add-member --team-id <id> --user-id budi --display-name "Budi" --role editor --added-by andi

# Chat in team mode (shared + private memory)
kyourai chat --team <team-id> --user andi
```

### Portable context

Export your Kyourai memory and import it into another agent:

```bash
# Export
kyourai memory export ~/my-context.kpc.json

# Import on another machine / agent
kyourai memory import ~/my-context.kpc.json --strategy skip_duplicates
```

Or start the MCP server so other agents can access Kyourai memory directly:

```bash
kyourai mcp-server  # exposes tools: kyourai_export_memory, kyourai_import_memory,
                    # kyourai_list_memory, kyourai_search_memory
```

## Configuration

Config lives at `~/.kyourai/config.yaml` (or `$KYOURAI_HOME/config.yaml`):

```yaml
agent:
  model: openai:gpt-4o
memory:
  holographic:
    hrr_dim: 1024
    default_trust: 0.5
    min_trust_threshold: 0.3
curator:
  enabled: true
  interval_hours: 168  # 7 days
  min_idle_hours: 2
  stale_after_days: 30
  trust_floor: 0.1
  pin_threshold: 3
```

Secrets (API keys) go in `~/.kyourai/.env`, never in config.yaml.

## Skills

Skills are markdown instruction files (`SKILL.md`) that teach the agent how
and when to use tools. Each skill has YAML frontmatter and a markdown body.

```bash
kyourai skills list                    # list loaded skills
kyourai skills create --name my-skill  # create a new skill
```

Skills load from (highest precedence first):
1. Workspace: `<workspace>/skills/`
2. Personal: `~/.kyourai/skills/`
3. Bundled: shipped with kyourai

Gating: `requires.bins`, `requires.env`, `os` filters, and `always` flag.

## Cron scheduler

Schedule recurring tasks with cron expressions:

```bash
kyourai cron add --id daily-curator --schedule "0 9 * * *" --action curator
kyourai cron list
kyourai cron run daily-curator  # run immediately
```

Actions: `agent_turn` (run agent on prompt), `tool` (call memory tool),
`curator` (run memory curator). Tasks persist to `cron_state.json`.

## OpenAI-compatible API

Run Kyourai as an OpenAI-compatible server:

```bash
kyourai serve --port 18789
```

Then use from any OpenAI-compatible client (Open WebUI, LobeChat, curl):

```bash
curl http://localhost:18789/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"kyourai","messages":[{"role":"user","content":"Hello"}]}'
```

Set `KYOURAI_API_KEY` env var to require authentication.

## Testing

```bash
python tests/test_e2e.py       # end-to-end memory round-trip (37 checks)
python tests/test_features.py  # skills + cron + API server (46 checks)
```

## License

MIT.
