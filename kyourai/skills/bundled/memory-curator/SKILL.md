---
name: memory-curator
description: Run the memory curator to find contradictions, prune low-trust facts, and decay trust scores.
user-invocable: true
---

# Memory Curator Skill

When the user asks to clean up memory, find contradictions, or maintain the
memory store, use the curator.

## Actions

1. **Run curator**: Ask the user to run `kyourai curator run --force` from the
   CLI, or call the curator programmatically.

2. **Check status**: Run `kyourai curator status` to see the last run time
   and summary.

3. **Find contradictions**: The curator automatically finds facts that share
   entities but have divergent content — these are potential contradictions
   that need human review.

4. **Prune low-trust**: Facts with trust_score < 0.1 that have never been
   retrieved are flagged as prune candidates. They are NOT deleted — only
   flagged for review.

## Important

- The curator NEVER deletes facts automatically — it only flags them.
- Pinned facts (helpful_count >= 3) bypass all pruning.
- The curator runs automatically every 7 days when the agent is idle.
