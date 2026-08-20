"""Curator — background memory maintenance loop.

Ported from Hermes Agent (agent/curator.py), adapted for Kyourai's memory system.

The curator is an inactivity-triggered background task that periodically
maintains the memory store. It runs when the agent is idle and the last
curator run was longer than ``interval_hours`` ago.

Responsibilities:
  - Find and flag contradictory facts via HRR vector analysis
  - Prune low-trust facts (below trust_floor after N days unused)
  - Decay trust scores for facts not retrieved in a long time
  - Persist curator state (.curator_state) across runs

Strict invariants:
  - Never auto-deletes facts — only flags them for review
  - Pinned facts (helpful_count >= pin_threshold) bypass all pruning
  - Runs on a background thread, never blocks the agent
  - All operations are logged and summarised in .curator_state
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kyourai.constants import get_kyourai_home, get_curator_state_path
from kyourai.utils import atomic_json_write

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = 24 * 7       # 7 days
DEFAULT_MIN_IDLE_HOURS = 2            # agent must be idle for 2h before curator runs
DEFAULT_STALE_AFTER_DAYS = 30         # facts not retrieved in 30d are "stale"
DEFAULT_TRUST_FLOOR = 0.1             # facts below this trust are pruning candidates
DEFAULT_PIN_THRESHOLD = 3             # facts with helpful_count >= 3 are pinned
DEFAULT_TRUST_DECAY_PER_MONTH = 0.0   # monthly trust decay (0 = disabled)


# ---------------------------------------------------------------------------
# .curator_state — persistent scheduler + status
# ---------------------------------------------------------------------------

def _default_state() -> dict[str, Any]:
    return {
        "last_run_at": None,
        "last_run_duration_seconds": None,
        "last_run_summary": None,
        "paused": False,
        "run_count": 0,
    }


def load_state() -> dict[str, Any]:
    """Load curator state from .curator_state file."""
    path = get_curator_state_path()
    if not path.exists():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base = _default_state()
            base.update({k: v for k, v in data.items() if k in base})
            return base
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed to read curator state: %s", e)
    return _default_state()


def save_state(data: dict[str, Any]) -> None:
    """Save curator state to .curator_state file."""
    path = get_curator_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_json_write(path, data, indent=2, sort_keys=True)
    except Exception as e:
        logger.debug("Failed to save curator state: %s", e)


def set_paused(paused: bool) -> None:
    state = load_state()
    state["paused"] = bool(paused)
    save_state(state)


def is_paused() -> bool:
    return bool(load_state().get("paused"))


# ---------------------------------------------------------------------------
# Config access (reads from the passed config dict, not a global)
# ---------------------------------------------------------------------------

def _get_config(config: dict | None, key: str, default: Any) -> Any:
    if not config:
        return default
    curator_cfg = config.get("curator", {})
    if not isinstance(curator_cfg, dict):
        return default
    return curator_cfg.get(key, default)


# ---------------------------------------------------------------------------
# Idle / interval check
# ---------------------------------------------------------------------------

def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def should_run_now(
    config: dict | None = None,
    now: datetime | None = None,
) -> bool:
    """Return True if the curator should run immediately.

    Gates:
      - curator.enabled == True (default: True)
      - not paused
      - last_run_at present AND older than interval_hours

    First-run behavior: seeds last_run_at to "now" and defers the first
    real pass by one full interval.
    """
    if not _get_config(config, "enabled", True):
        return False
    if is_paused():
        return False

    state = load_state()
    last = _parse_iso(state.get("last_run_at"))
    if last is None:
        if now is None:
            now = datetime.now(timezone.utc)
        try:
            state["last_run_at"] = now.isoformat()
            state["last_run_summary"] = (
                "deferred first run — curator seeded, will run after one interval"
            )
            save_state(state)
        except Exception as e:
            logger.debug("Failed to seed curator last_run_at: %s", e)
        return False

    if now is None:
        now = datetime.now(timezone.utc)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    interval_hours = int(_get_config(config, "interval_hours", DEFAULT_INTERVAL_HOURS))
    interval = timedelta(hours=interval_hours)
    return (now - last) >= interval


# ---------------------------------------------------------------------------
# Maintenance operations
# ---------------------------------------------------------------------------

def find_contradictions(
    store,
    config: dict | None = None,
    threshold: float = 0.3,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Find potentially contradictory facts using HRR vector analysis.

    Returns list of contradiction pairs with scores.
    Does NOT modify the store — just reports findings.
    """
    from kyourai.memory.holographic.retrieval import FactRetriever
    retriever = FactRetriever(store=store, hrr_dim=store.hrr_dim)
    return retriever.contradict(threshold=threshold, limit=limit)


def prune_low_trust_facts(
    store,
    config: dict | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Flag low-trust, long-unused facts for review. Does NOT delete.

    A fact is a pruning candidate when:
      - trust_score < trust_floor
      - retrieval_count == 0 (never retrieved)
      - helpful_count < pin_threshold (not pinned by user feedback)
      - created > stale_after_days ago

    This function only LOGS the candidates — actual removal requires
    explicit user confirmation or the fact_store remove tool.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    trust_floor = float(_get_config(config, "trust_floor", DEFAULT_TRUST_FLOOR))
    stale_days = int(_get_config(config, "stale_after_days", DEFAULT_STALE_AFTER_DAYS))
    pin_threshold = int(_get_config(config, "pin_threshold", DEFAULT_PIN_THRESHOLD))
    stale_cutoff = now - timedelta(days=stale_days)

    counts = {"checked": 0, "prune_candidates": 0, "pinned": 0, "recent": 0}

    try:
        rows = store._conn.execute(
            """SELECT fact_id, content, trust_score, retrieval_count,
                      helpful_count, created_at, updated_at
               FROM facts"""
        ).fetchall()
    except Exception as e:
        logger.warning("Curator prune scan failed: %s", e)
        return counts

    for row in rows:
        counts["checked"] += 1
        fact_id = row["fact_id"]
        trust = row["trust_score"]
        retrieval_count = row["retrieval_count"]
        helpful_count = row["helpful_count"]

        # Pinned facts bypass pruning
        if helpful_count >= pin_threshold:
            counts["pinned"] += 1
            continue

        # Check age
        created_str = row["created_at"]
        try:
            created = datetime.fromisoformat(str(created_str).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        if created > stale_cutoff:
            counts["recent"] += 1
            continue

        # Pruning candidate
        if trust < trust_floor and retrieval_count == 0:
            counts["prune_candidates"] += 1
            logger.info(
                "Curator: prune candidate fact_id=%d trust=%.2f retrievals=%d: %s",
                fact_id, trust, retrieval_count, row["content"][:80],
            )

    return counts


def decay_trust_scores(
    store,
    config: dict | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Apply temporal trust decay to facts not retrieved recently.

    Decay formula: trust -= decay_per_month * months_since_last_retrieval
    Clamped to [0, 1]. Pinned facts (helpful_count >= pin_threshold) are exempt.

    Default decay_per_month is 0.0 (disabled) — opt in via config.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    decay_per_month = float(_get_config(config, "trust_decay_per_month", DEFAULT_TRUST_DECAY_PER_MONTH))
    if decay_per_month <= 0:
        return {"decayed": 0, "checked": 0, "exempt": 0}

    pin_threshold = int(_get_config(config, "pin_threshold", DEFAULT_PIN_THRESHOLD))
    counts = {"decayed": 0, "checked": 0, "exempt": 0}

    try:
        rows = store._conn.execute(
            """SELECT fact_id, trust_score, helpful_count, updated_at
               FROM facts"""
        ).fetchall()
    except Exception as e:
        logger.warning("Curator decay scan failed: %s", e)
        return counts

    for row in rows:
        counts["checked"] += 1
        if row["helpful_count"] >= pin_threshold:
            counts["exempt"] += 1
            continue

        updated_str = row["updated_at"]
        try:
            updated = datetime.fromisoformat(str(updated_str).replace("Z", "+00:00"))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        months_since = (now - updated).days / 30.0
        if months_since < 1:
            continue

        decay = decay_per_month * months_since
        new_trust = max(0.0, min(1.0, row["trust_score"] - decay))
        if new_trust != row["trust_score"]:
            store._conn.execute(
                "UPDATE facts SET trust_score = ?, updated_at = CURRENT_TIMESTAMP WHERE fact_id = ?",
                (new_trust, row["fact_id"]),
            )
            counts["decayed"] += 1

    if counts["decayed"] > 0:
        store._conn.commit()

    return counts


# ---------------------------------------------------------------------------
# Full curator run
# ---------------------------------------------------------------------------

def run_curator(
    store,
    config: dict | None = None,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run a full curator pass on the given memory store.

    Returns a summary dict with counts from each phase.
    Does NOT delete any facts — only flags, logs, and decays trust.

    Set force=True to bypass the interval gate (for `kyourai curator run`).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if not force and not should_run_now(config=config, now=now):
        return {"skipped": True, "reason": "interval gate not met"}

    start = datetime.now(timezone.utc)
    summary: dict[str, Any] = {"skipped": False, "phases": {}}

    # Phase 1: Find contradictions
    try:
        contradictions = find_contradictions(store, config=config)
        summary["phases"]["contradictions"] = {
            "found": len(contradictions),
            "top_pairs": [
                {
                    "fact_a": c["fact_a"]["content"][:80],
                    "fact_b": c["fact_b"]["content"][:80],
                    "score": c["contradiction_score"],
                }
                for c in contradictions[:5]
            ],
        }
        if contradictions:
            logger.info("Curator found %d potential contradictions", len(contradictions))
    except Exception as e:
        summary["phases"]["contradictions"] = {"error": str(e)}
        logger.warning("Curator contradiction scan failed: %s", e)

    # Phase 2: Prune low-trust candidates
    try:
        prune_counts = prune_low_trust_facts(store, config=config, now=now)
        summary["phases"]["prune_scan"] = prune_counts
    except Exception as e:
        summary["phases"]["prune_scan"] = {"error": str(e)}
        logger.warning("Curator prune scan failed: %s", e)

    # Phase 3: Trust decay
    try:
        decay_counts = decay_trust_scores(store, config=config, now=now)
        summary["phases"]["trust_decay"] = decay_counts
    except Exception as e:
        summary["phases"]["trust_decay"] = {"error": str(e)}
        logger.warning("Curator trust decay failed: %s", e)

    # Persist state
    duration = (datetime.now(timezone.utc) - start).total_seconds()
    state = load_state()
    state["last_run_at"] = start.isoformat()
    state["last_run_duration_seconds"] = round(duration, 2)
    state["last_run_summary"] = summary
    state["run_count"] = int(state.get("run_count", 0)) + 1
    save_state(state)

    summary["duration_seconds"] = round(duration, 2)
    summary["run_count"] = state["run_count"]
    return summary


# ---------------------------------------------------------------------------
# Background thread runner
# ---------------------------------------------------------------------------

class CuratorBackgroundRunner:
    """Manages a background thread for inactivity-triggered curator runs.

    The agent loop calls maybe_run() periodically. The runner checks if
    the curator should run (interval gate + idle check) and spawns a
    background thread if so. Only one curator thread runs at a time.
    """

    def __init__(
        self,
        store,
        config: dict | None = None,
        is_idle_fn: callable | None = None,
    ):
        self._store = store
        self._config = config or {}
        self._is_idle_fn = is_idle_fn or (lambda: True)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running = False

    def maybe_run(self, now: datetime | None = None) -> bool:
        """Check if curator should run and spawn a background thread if so.

        Returns True if a run was started, False if skipped.
        """
        if not should_run_now(config=self._config, now=now):
            return False

        min_idle = float(_get_config(self._config, "min_idle_hours", DEFAULT_MIN_IDLE_HOURS))
        if not self._is_idle_fn():
            return False

        with self._lock:
            if self._running:
                return False
            self._running = True

        def _run():
            try:
                run_curator(self._store, config=self._config, now=now)
            except Exception as e:
                logger.warning("Background curator run failed: %s", e)
            finally:
                with self._lock:
                    self._running = False

        self._thread = threading.Thread(target=_run, daemon=True, name="kyourai-curator")
        self._thread.start()
        return True

    @property
    def is_running(self) -> bool:
        return self._running

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the current curator run to finish. Returns True if completed."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()
