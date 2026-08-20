"use client";

import { useState, useEffect } from "react";
import { api, AuditEvent } from "@/lib/api";

export function Audit() {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [stats, setStats] = useState<{
    total_events: number;
    by_type: Record<string, number>;
    by_severity: Record<string, number>;
    by_outcome: Record<string, number>;
  } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [filterType, setFilterType] = useState("");
  const [filterSeverity, setFilterSeverity] = useState("");

  const refresh = () => {
    setLoading(true);
    Promise.all([
      api.getAuditEvents({
        event_type: filterType || undefined,
        severity: filterSeverity || undefined,
        limit: 100,
      }),
      api.getAuditStats(7),
    ])
      .then(([e, s]) => {
        setEvents(e.events);
        setStats(s);
        setError("");
      })
      .catch((err) => setError(String(err)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    refresh();
  }, [filterType, filterSeverity]);

  if (loading && !stats) return <div className="text-text-dim">Loading audit log...</div>;
  if (error) return <div className="text-red">Error: {error}</div>;

  const severityColor = (s: string) => {
    switch (s) {
      case "critical": return "text-red";
      case "error": return "text-red";
      case "warning": return "text-yellow";
      default: return "text-text-dim";
    }
  };

  const typeOptions = stats
    ? Object.keys(stats.by_type).sort()
    : [];

  return (
    <div className="space-y-6">
      {/* Stats */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div className="card p-3">
            <div className="text-xs text-text-dim">Total (7d)</div>
            <div className="text-xl font-bold tabular-nums">{stats.total_events}</div>
          </div>
          <div className="card p-3">
            <div className="text-xs text-text-dim">Errors</div>
            <div className="text-xl font-bold tabular-nums text-red">
              {(stats.by_severity["error"] || 0) + (stats.by_severity["critical"] || 0)}
            </div>
          </div>
          <div className="card p-3">
            <div className="text-xs text-text-dim">Warnings</div>
            <div className="text-xl font-bold tabular-nums text-yellow">
              {stats.by_severity["warning"] || 0}
            </div>
          </div>
          <div className="card p-3">
            <div className="text-xs text-text-dim">Successes</div>
            <div className="text-xl font-bold tabular-nums text-green">
              {stats.by_outcome["success"] || 0}
            </div>
          </div>
        </div>
      )}

      {/* By type breakdown */}
      {stats && Object.keys(stats.by_type).length > 0 && (
        <div className="card p-4">
          <h3 className="text-sm font-semibold text-text-dim mb-3">Events by Type</h3>
          <div className="flex flex-wrap gap-2">
            {Object.entries(stats.by_type)
              .sort((a, b) => b[1] - a[1])
              .map(([type, count]) => (
                <span
                  key={type}
                  className="text-xs px-2 py-1 bg-surface rounded border border-border"
                >
                  {type}: {count}
                </span>
              ))}
          </div>
        </div>
      )}

      {/* Filters */}
      <div className="flex gap-2">
        <select
          value={filterType}
          onChange={(e) => setFilterType(e.target.value)}
          className="px-3 py-1.5 bg-surface border border-border rounded text-sm"
        >
          <option value="">All types</option>
          {typeOptions.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <select
          value={filterSeverity}
          onChange={(e) => setFilterSeverity(e.target.value)}
          className="px-3 py-1.5 bg-surface border border-border rounded text-sm"
        >
          <option value="">All severities</option>
          <option value="info">Info</option>
          <option value="warning">Warning</option>
          <option value="error">Error</option>
          <option value="critical">Critical</option>
        </select>
        <button
          onClick={refresh}
          className="px-3 py-1.5 text-sm border border-border rounded hover:bg-surface"
        >
          Refresh
        </button>
      </div>

      {/* Events list */}
      <div>
        <h3 className="text-sm font-semibold text-text-dim mb-3">
          Recent Events ({events.length})
        </h3>
        {events.length === 0 ? (
          <div className="card p-4 text-text-dim text-sm">No audit events.</div>
        ) : (
          <div className="space-y-1 max-h-[600px] overflow-y-auto">
            {events.map((event) => (
              <div key={event.event_id} className="card p-3 text-sm">
                <div className="flex items-center gap-3">
                  <span className={`text-xs uppercase ${severityColor(event.severity)}`}>
                    {event.severity}
                  </span>
                  <span className="font-medium">{event.event_type}</span>
                  {event.action && (
                    <span className="text-text-dim">{event.action}</span>
                  )}
                  <span className="ml-auto text-xs text-text-dim tabular-nums">
                    {new Date(event.timestamp * 1000).toLocaleTimeString()}
                  </span>
                </div>
                {event.detail && (
                  <p className="text-text-dim mt-1 text-xs">{event.detail}</p>
                )}
                {event.outcome && (
                  <span className={`text-xs mt-1 inline-block ${
                    event.outcome === "success" ? "text-green" :
                    event.outcome === "failure" ? "text-red" : "text-text-dim"
                  }`}>
                    → {event.outcome}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
