---
name: portable-context
description: Export or import memory across agents using the KPC (Kyourai Portable Context) format.
user-invocable: true
---

# Portable Context Skill

When the user wants to transfer memory between agents (Kyourai, Claude,
ChatGPT, Cursor, etc.), use the portable context system.

## Export

```bash
kyourai memory export ~/my-context.kpc.json
```

This creates a JSON bundle with:
- Builtin memory entries (MEMORY.md, USER.md)
- Holographic facts with trust scores
- SHA-256 checksum for integrity verification

## Import

```bash
kyourai memory import ~/my-context.kpc.json --strategy skip_duplicates
```

Merge strategies:
- `skip_duplicates` (default): skip entries that already exist
- `overwrite`: replace existing entries with imported ones
- `append`: always add, even if duplicates exist

## MCP Server

Other agents can access Kyourai memory directly via the MCP server:

```bash
kyourai mcp-server
```

This exposes tools: `kyourai_export_memory`, `kyourai_import_memory`,
`kyourai_list_memory`, `kyourai_search_memory`.
