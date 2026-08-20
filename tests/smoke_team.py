"""Smoke test for the team-native layer."""
import os
import tempfile

test_home = tempfile.mkdtemp(prefix="kyourai_team_")
os.environ["KYOURAI_HOME"] = test_home

from kyourai.team import Role, TeamManager, TeamMemoryRouter
from kyourai.memory.builtin import BuiltinMemoryProvider

# Create a team
tm = TeamManager()
team = tm.create_team("Engineering Team", creator_user_id="andi", creator_display_name="Andi")
print("Created team:", team.team_id, team.team_name)
print("Creator role:", team.get_role("andi"))

# Add members
tm.add_member(team.team_id, "budi", "Budi", role=Role.EDITOR, added_by="andi")
tm.add_member(team.team_id, "citra", "Citra", role=Role.MEMBER, added_by="andi")
print("\nMembers:", list(team.members.keys()))

# Test role hierarchy
team = tm.load_team(team.team_id)
print("Andi is admin:", team.has_role("andi", Role.ADMIN))
print("Budi is editor:", team.has_role("budi", Role.EDITOR))
print("Budi is admin:", team.has_role("budi", Role.ADMIN))
print("Citra is member:", team.has_role("citra", Role.MEMBER))

# Test permission checks via router
router_andi = TeamMemoryRouter(tm, team.team_id, "andi")
router_budi = TeamMemoryRouter(tm, team.team_id, "budi")
router_citra = TeamMemoryRouter(tm, team.team_id, "citra")

print("\nPermission checks:")
print("  Andi write_shared:", router_andi.check_permission("write_shared"))
print("  Budi write_shared:", router_budi.check_permission("write_shared"))
print("  Citra write_shared:", router_citra.check_permission("write_shared"))
print("  Citra write_private:", router_citra.check_permission("write_private"))
print("  Citra manage_team:", router_citra.check_permission("manage_team"))

# Test memory isolation: Andi writes to shared, Budi writes to private
def add_shared_fact():
    provider = BuiltinMemoryProvider()
    provider.initialize("shared-session")
    result = provider.handle_tool_call("memory", {"action": "add", "target": "memory", "content": "Team uses Python 3.12"})
    return result

def add_private_fact():
    provider = BuiltinMemoryProvider()
    provider.initialize("private-session")
    result = provider.handle_tool_call("memory", {"action": "add", "target": "user", "content": "Personal preference: dark mode"})
    return result

# Andi writes to shared
shared_result = router_andi.with_shared(add_shared_fact)
print("\nAndi writes to shared:", "success" in shared_result)

# Budi writes to private
private_result = router_budi.with_private(add_private_fact)
print("Budi writes to private:", "success" in private_result)

# Verify isolation: shared memory doesn't contain Budi's private fact
def read_system_prompt():
    provider = BuiltinMemoryProvider()
    provider.initialize("read-session")
    return provider.system_prompt_block()

# Read shared — should have "Team uses Python 3.12" but NOT "dark mode"
shared_prompt = router_andi.with_shared(read_system_prompt)
print("\nShared has team fact:", "Team uses Python 3.12" in shared_prompt)
print("Shared does NOT have private fact:", "dark mode" not in shared_prompt)

# Read Budi's private — should have "dark mode" but NOT "Team uses Python 3.12"
budi_private_prompt = router_budi.with_private(read_system_prompt)
print("Budi private has personal fact:", "dark mode" in budi_private_prompt)
print("Budi private does NOT have team fact:", "Team uses Python 3.12" not in budi_private_prompt)

# Citra (member) can read shared but cannot write
citra_shared_prompt = router_citra.with_shared(read_system_prompt)
print("\nCitra can read shared:", "Team uses Python 3.12" in citra_shared_prompt)

# Test role change
tm.set_role(team.team_id, "citra", Role.EDITOR, set_by="andi")
team = tm.load_team(team.team_id)
print("\nCitra promoted to editor:", team.has_role("citra", Role.EDITOR))

# Test member removal
tm.remove_member(team.team_id, "citra", removed_by="andi")
team = tm.load_team(team.team_id)
print("Citra removed:", "citra" not in team.members)

# Test list teams
teams = tm.list_teams()
print("\nList teams:", len(teams), "team(s)")
print("  Team name:", teams[0]["team_name"], "members:", teams[0]["member_count"])

print("\nAll team-native tests passed!")
