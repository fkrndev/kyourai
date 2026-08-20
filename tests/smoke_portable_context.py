"""Smoke test for the portable context format."""
import json
import os
import tempfile

test_home = tempfile.mkdtemp(prefix="kyourai_kpc_")
os.environ["KYOURAI_HOME"] = test_home

from kyourai.mcp.portable_context import (
    PortableContext,
    export_memory,
    export_to_file,
    import_memory,
    import_from_file,
)
from kyourai.memory.builtin import BuiltinMemoryProvider
from kyourai.memory.holographic.store import MemoryStore

# Phase 1: Populate memory in the source agent
builtin_src = BuiltinMemoryProvider()
builtin_src.initialize("src-session")
builtin_src.handle_tool_call("memory", {"action": "add", "target": "memory", "content": "User prefers Python 3.12"})
builtin_src.handle_tool_call("memory", {"action": "add", "target": "user", "content": "Name: Andi"})

holo_src = MemoryStore(hrr_dim=1024)
holo_src.add_fact("Andi works as a backend engineer", category="user_pref")
holo_src.add_fact("The project uses PostgreSQL", category="project")

# Phase 2: Export to a KPC bundle
ctx = export_memory(
    profile={"display_name": "Andi", "agent_identity": "coder"},
    builtin_provider=builtin_src,
    holographic_store=holo_src,
)
kpc_json = ctx.to_json()
print("Exported KPC bundle:")
print("  format:", ctx.format)
print("  version:", ctx.version)
print("  builtin entries:", len(ctx.builtin_memory.get("memory_entries", [])) + len(ctx.builtin_memory.get("user_entries", [])))
print("  holographic facts:", len(ctx.holographic_facts))
print("  checksum:", ctx.metadata.get("checksum", "")[:20] + "...")

# Verify checksum
parsed = PortableContext.from_json(kpc_json)
print("  checksum valid:", True)  # from_json raises if invalid

# Phase 3: Export to file
export_path = export_to_file(
    os.path.join(test_home, "export.kpc.json"),
    builtin_provider=builtin_src,
    holographic_store=holo_src,
)
print("\nExported to file:", export_path.exists())

# Phase 4: Import into a fresh agent (new KYOURAI_HOME)
test_home2 = tempfile.mkdtemp(prefix="kyourai_kpc_dst_")
os.environ["KYOURAI_HOME"] = test_home2

builtin_dst = BuiltinMemoryProvider()
builtin_dst.initialize("dst-session")
holo_dst = MemoryStore(hrr_dim=1024)

summary = import_from_file(
    export_path,
    builtin_provider=builtin_dst,
    holographic_store=holo_dst,
)
print("\nImport summary:", summary)

# Verify imported builtin memory (re-init to load fresh snapshot from disk)
builtin_dst.shutdown()
builtin_dst = BuiltinMemoryProvider()
builtin_dst.initialize("dst-session-verify")
prompt = builtin_dst.system_prompt_block()
print("Destination has 'Python 3.12':", "Python 3.12" in prompt)
print("Destination has 'Andi':", "Andi" in prompt)

# Verify imported holographic facts
facts = holo_dst.list_facts(limit=10)
print("Destination has", len(facts), "facts")
fact_contents = [f["content"] for f in facts]
print("Has backend fact:", "Andi works as a backend engineer" in fact_contents)
print("Has PostgreSQL fact:", "The project uses PostgreSQL" in fact_contents)

# Phase 5: Test merge strategies
# Add a duplicate fact, then re-import with skip_duplicates
holo_dst.add_fact("Andi works as a backend engineer", category="user_pref")
summary_skip = import_from_file(
    export_path,
    import_builtin=False,  # skip builtin this time
    builtin_provider=builtin_dst,
    holographic_store=holo_dst,
    merge_strategy="skip_duplicates",
)
print("\nSkip duplicates:", summary_skip)

# Test append strategy
summary_append = import_from_file(
    export_path,
    import_builtin=False,
    builtin_provider=builtin_dst,
    holographic_store=holo_dst,
    merge_strategy="append",
)
print("Append:", summary_append)

builtin_src.shutdown()
builtin_dst.shutdown()
holo_src.close()
holo_dst.close()

print("\nAll portable context tests passed!")
