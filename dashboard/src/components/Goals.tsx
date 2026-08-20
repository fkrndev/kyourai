"use client";

import { useState, useEffect } from "react";
import { api, Goal, GoalSummary } from "@/lib/api";

export function Goals() {
  const [active, setActive] = useState<Goal[]>([]);
  const [completed, setCompleted] = useState<Goal[]>([]);
  const [summary, setSummary] = useState<GoalSummary | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newPriority, setNewPriority] = useState("medium");

  const refresh = () => {
    setLoading(true);
    api
      .listGoals()
      .then((d) => {
        setActive(d.active);
        setCompleted(d.completed);
        setSummary(d.summary);
        setError("");
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    refresh();
  }, []);

  const handleCreate = () => {
    if (!newTitle.trim()) return;
    api
      .createGoal({ title: newTitle, priority: newPriority })
      .then(() => {
        setNewTitle("");
        setShowForm(false);
        refresh();
      })
      .catch((e) => setError(String(e)));
  };

  const handleProgress = (goalId: string, progress: number) => {
    api.updateGoalProgress(goalId, progress).then(refresh).catch((e) => setError(String(e)));
  };

  const handleComplete = (goalId: string) => {
    api.completeGoal(goalId).then(refresh).catch((e) => setError(String(e)));
  };

  const handleAbandon = (goalId: string) => {
    if (!confirm("Abandon this goal?")) return;
    api.abandonGoal(goalId).then(refresh).catch((e) => setError(String(e)));
  };

  if (loading) return <div className="text-text-dim">Loading goals...</div>;
  if (error) return <div className="text-red">Error: {error}</div>;

  const priorityColor = (p: string) => {
    switch (p) {
      case "critical": return "text-red";
      case "high": return "text-yellow";
      case "medium": return "text-accent";
      default: return "text-text-dim";
    }
  };

  return (
    <div className="space-y-6">
      {/* Summary */}
      {summary && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <div className="card p-3">
            <div className="text-xs text-text-dim">Total</div>
            <div className="text-xl font-bold tabular-nums">{summary.total}</div>
          </div>
          <div className="card p-3">
            <div className="text-xs text-text-dim">Active</div>
            <div className="text-xl font-bold tabular-nums text-green">{summary.active}</div>
          </div>
          <div className="card p-3">
            <div className="text-xs text-text-dim">Completed</div>
            <div className="text-xl font-bold tabular-nums text-accent">{summary.completed}</div>
          </div>
          <div className="card p-3">
            <div className="text-xs text-text-dim">Blocked</div>
            <div className="text-xl font-bold tabular-nums text-yellow">{summary.blocked}</div>
          </div>
          <div className="card p-3">
            <div className="text-xs text-text-dim">Avg Progress</div>
            <div className="text-xl font-bold tabular-nums">{summary.avg_progress}%</div>
          </div>
        </div>
      )}

      {/* Create button */}
      <button
        onClick={() => setShowForm(!showForm)}
        className="px-4 py-2 text-sm border border-border rounded hover:bg-surface transition-colors"
      >
        {showForm ? "Cancel" : "+ New Goal"}
      </button>

      {/* Create form */}
      {showForm && (
        <div className="card p-4 space-y-3">
          <input
            type="text"
            placeholder="Goal title..."
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            className="w-full px-3 py-2 bg-surface border border-border rounded text-sm focus:outline-none focus:border-accent"
          />
          <select
            value={newPriority}
            onChange={(e) => setNewPriority(e.target.value)}
            className="px-3 py-2 bg-surface border border-border rounded text-sm"
          >
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
            <option value="critical">Critical</option>
          </select>
          <button
            onClick={handleCreate}
            className="px-4 py-2 text-sm bg-accent text-white rounded hover:opacity-90"
          >
            Create
          </button>
        </div>
      )}

      {/* Active goals */}
      <div>
        <h3 className="text-sm font-semibold text-text-dim mb-3">
          Active Goals ({active.length})
        </h3>
        {active.length === 0 ? (
          <div className="card p-4 text-text-dim text-sm">No active goals.</div>
        ) : (
          <div className="space-y-2">
            {active.map((goal) => (
              <div key={goal.goal_id} className="card p-4">
                <div className="flex items-center gap-3">
                  <span className={`text-xs uppercase ${priorityColor(goal.priority)}`}>
                    {goal.priority}
                  </span>
                  <span className="font-medium flex-1">{goal.title}</span>
                  <span className="text-sm text-text-dim tabular-nums">
                    {goal.progress}%
                  </span>
                </div>
                {goal.description && (
                  <p className="text-sm text-text-dim mt-1">{goal.description}</p>
                )}
                {/* Progress bar */}
                <div className="mt-3 h-2 bg-surface rounded-full overflow-hidden">
                  <div
                    className="h-full bg-accent transition-all"
                    style={{ width: `${goal.progress}%` }}
                  />
                </div>
                {/* Actions */}
                <div className="flex gap-2 mt-3">
                  <button
                    onClick={() => handleProgress(goal.goal_id, Math.min(100, goal.progress + 25))}
                    className="text-xs px-2 py-1 border border-border rounded hover:bg-surface"
                  >
                    +25%
                  </button>
                  <button
                    onClick={() => handleComplete(goal.goal_id)}
                    className="text-xs px-2 py-1 border border-border rounded text-green hover:bg-surface"
                  >
                    Complete
                  </button>
                  <button
                    onClick={() => handleAbandon(goal.goal_id)}
                    className="text-xs px-2 py-1 border border-border rounded text-red hover:bg-surface"
                  >
                    Abandon
                  </button>
                </div>
                {goal.tags.length > 0 && (
                  <div className="flex gap-1 mt-2">
                    {goal.tags.map((tag) => (
                      <span key={tag} className="text-xs px-2 py-0.5 bg-surface rounded">
                        {tag}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Completed goals */}
      {completed.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-text-dim mb-3">
            Completed ({completed.length})
          </h3>
          <div className="space-y-1">
            {completed.slice(0, 10).map((goal) => (
              <div key={goal.goal_id} className="card p-3 flex items-center gap-3">
                <span className="text-green">✓</span>
                <span className="text-sm flex-1">{goal.title}</span>
                {goal.outcome && (
                  <span className="text-xs text-text-dim">{goal.outcome}</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
