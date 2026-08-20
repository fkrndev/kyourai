"use client";

import { useState, useEffect } from "react";
import { api, TaskFlow, Task } from "@/lib/api";

export function Tasks() {
  const [flows, setFlows] = useState<TaskFlow[]>([]);
  const [activeCount, setActiveCount] = useState(0);
  const [selectedFlow, setSelectedFlow] = useState<{
    flow: TaskFlow;
    tasks: Task[];
  } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newSteps, setNewSteps] = useState("");

  const refresh = () => {
    setLoading(true);
    api
      .listTaskFlows()
      .then((d) => {
        setFlows(d.flows);
        setActiveCount(d.active_count);
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
    const steps = newSteps
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    api
      .createTaskFlow({ title: newTitle, steps })
      .then(() => {
        setNewTitle("");
        setNewSteps("");
        setShowForm(false);
        refresh();
      })
      .catch((e) => setError(String(e)));
  };

  const handleCancel = (flowId: string) => {
    if (!confirm("Cancel this flow?")) return;
    api.cancelTaskFlow(flowId).then(refresh).catch((e) => setError(String(e)));
  };

  const handleSelect = (flowId: string) => {
    api
      .getTaskFlow(flowId)
      .then((d) => setSelectedFlow(d))
      .catch((e) => setError(String(e)));
  };

  if (loading) return <div className="text-text-dim">Loading tasks...</div>;
  if (error) return <div className="text-red">Error: {error}</div>;

  const statusColor = (status: string) => {
    switch (status) {
      case "running": return "text-accent";
      case "succeeded": return "text-green";
      case "failed": return "text-red";
      case "cancelled": return "text-text-dim";
      case "blocked": return "text-yellow";
      default: return "text-text-dim";
    }
  };

  return (
    <div className="space-y-6">
      {/* Summary */}
      <div className="card p-4 flex items-center gap-4">
        <div>
          <span className="text-sm text-text-dim">Active Flows: </span>
          <span className="font-bold text-green tabular-nums">{activeCount}</span>
        </div>
        <div>
          <span className="text-sm text-text-dim">Total: </span>
          <span className="font-bold tabular-nums">{flows.length}</span>
        </div>
        <button
          onClick={() => setShowForm(!showForm)}
          className="ml-auto px-3 py-1.5 text-sm border border-border rounded hover:bg-surface"
        >
          {showForm ? "Cancel" : "+ New Flow"}
        </button>
      </div>

      {/* Create form */}
      {showForm && (
        <div className="card p-4 space-y-3">
          <input
            type="text"
            placeholder="Flow title..."
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            className="w-full px-3 py-2 bg-surface border border-border rounded text-sm focus:outline-none focus:border-accent"
          />
          <input
            type="text"
            placeholder="Steps (comma-separated)..."
            value={newSteps}
            onChange={(e) => setNewSteps(e.target.value)}
            className="w-full px-3 py-2 bg-surface border border-border rounded text-sm focus:outline-none focus:border-accent"
          />
          <button
            onClick={handleCreate}
            className="px-4 py-2 text-sm bg-accent text-white rounded hover:opacity-90"
          >
            Create Flow
          </button>
        </div>
      )}

      {/* Flow detail modal */}
      {selectedFlow && (
        <div className="card p-4 border-accent">
          <div className="flex items-center gap-3 mb-3">
            <h3 className="font-semibold flex-1">{selectedFlow.flow.title}</h3>
            <button
              onClick={() => setSelectedFlow(null)}
              className="text-text-dim hover:text-text"
            >
              ✕
            </button>
          </div>
          {/* Steps progress */}
          {selectedFlow.flow.steps.length > 0 && (
            <div className="flex gap-1 mb-4">
              {selectedFlow.flow.steps.map((step, i) => (
                <div
                  key={i}
                  className={`flex-1 text-xs text-center py-1.5 rounded ${
                    i < selectedFlow.flow.current_step_index
                      ? "bg-green text-white"
                      : i === selectedFlow.flow.current_step_index
                      ? "bg-accent text-white"
                      : "bg-surface text-text-dim"
                  }`}
                >
                  {step}
                </div>
              ))}
            </div>
          )}
          {/* Tasks */}
          {selectedFlow.tasks.length > 0 && (
            <div className="space-y-1">
              {selectedFlow.tasks.map((task) => (
                <div key={task.task_id} className="flex items-center gap-2 text-sm">
                  <span className={`w-2 h-2 rounded-full ${
                    task.status === "succeeded" ? "bg-green" :
                    task.status === "failed" ? "bg-red" :
                    task.status === "running" ? "bg-accent" : "bg-text-dim"
                  }`} />
                  <span className="flex-1">{task.title}</span>
                  <span className={`text-xs ${statusColor(task.status)}`}>
                    {task.status}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Flows list */}
      <div>
        <h3 className="text-sm font-semibold text-text-dim mb-3">
          All Flows ({flows.length})
        </h3>
        {flows.length === 0 ? (
          <div className="card p-4 text-text-dim text-sm">No task flows yet.</div>
        ) : (
          <div className="space-y-2">
            {flows.map((flow) => (
              <div
                key={flow.flow_id}
                className="card p-4 cursor-pointer hover:border-accent transition-colors"
                onClick={() => handleSelect(flow.flow_id)}
              >
                <div className="flex items-center gap-3">
                  <span className={`text-xs uppercase ${statusColor(flow.status)}`}>
                    {flow.status}
                  </span>
                  <span className="font-medium flex-1">{flow.title}</span>
                  {flow.steps.length > 0 && (
                    <span className="text-sm text-text-dim tabular-nums">
                      {flow.current_step_index}/{flow.steps.length}
                    </span>
                  )}
                  {(flow.status === "running" || flow.status === "queued" || flow.status === "waiting" || flow.status === "blocked") && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        handleCancel(flow.flow_id);
                      }}
                      className="text-xs text-red hover:underline"
                    >
                      Cancel
                    </button>
                  )}
                </div>
                {flow.progress_summary && (
                  <p className="text-sm text-text-dim mt-1">{flow.progress_summary}</p>
                )}
                <p className="text-xs text-text-dim mt-1">
                  {new Date(flow.created_at * 1000).toLocaleString()}
                </p>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
