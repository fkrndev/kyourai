"use client";

import { useState, useEffect, useCallback } from "react";
import { api, type InsightsResponse } from "@/lib/api";

export function Insights() {
  const [data, setData] = useState<InsightsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState(30);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.getInsights(days);
      setData(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load insights");
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    load();
  }, [load]);

  if (loading)
    return <div className="text-text-dim text-center py-20">Loading insights...</div>;
  if (error)
    return <div className="text-red text-center py-20">Error: {error}</div>;
  if (!data || data.empty)
    return <div className="text-text-dim text-center py-20">No session data for the selected period.</div>;

  const ov = data.overview;

  return (
    <div className="space-y-8">
      {/* Controls */}
      <div className="flex items-center gap-3">
        <label className="text-text-dim text-sm">Days:</label>
        <input
          type="number"
          value={days}
          min={1}
          max={365}
          onChange={(e) => setDays(parseInt(e.target.value) || 30)}
          className="w-24 px-3 py-1.5 bg-surface border border-border rounded text-text text-sm focus:outline-none focus:border-accent"
        />
        <button
          onClick={load}
          className="px-4 py-1.5 bg-surface border border-border rounded text-text text-sm hover:border-accent transition-colors"
        >
          Refresh
        </button>
      </div>

      {/* Overview cards */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
        <Card label="Sessions" value={ov.total_sessions} color="text-accent" />
        <Card label="Active" value={ov.active_sessions} color="text-green" />
        <Card label="Messages" value={ov.total_messages} color="text-accent" />
        <Card label="Tool Calls" value={ov.total_tool_calls} color="text-purple" />
        <Card label="Facts" value={ov.total_facts} color="text-purple" />
        <Card label="Avg Msgs" value={Math.round(ov.avg_messages_per_session || 0)} color="text-accent" />
      </div>

      {/* Activity chart */}
      {data.activity?.by_day && data.activity.by_day.length > 0 && (
        <div>
          <h3 className="text-text-dim text-sm font-medium uppercase tracking-wide mb-3">
            Activity (Last 14 Days)
          </h3>
          <ActivityChart data={data.activity.by_day.slice(-14)} />
        </div>
      )}

      {/* Models table */}
      {data.models && data.models.length > 0 && (
        <div>
          <h3 className="text-text-dim text-sm font-medium uppercase tracking-wide mb-3">
            Models
          </h3>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-text-dim text-xs uppercase">
                <th className="text-left py-2 px-3">Model</th>
                <th className="text-left py-2 px-3">Sessions</th>
                <th className="text-left py-2 px-3">Messages</th>
                <th className="text-left py-2 px-3">Tool Calls</th>
              </tr>
            </thead>
            <tbody>
              {data.models.map((m) => (
                <tr key={m.model} className="border-b border-border hover:bg-surface transition-colors">
                  <td className="py-2 px-3">{m.model}</td>
                  <td className="py-2 px-3">{m.sessions}</td>
                  <td className="py-2 px-3">{m.messages}</td>
                  <td className="py-2 px-3">{m.tool_calls}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Tools table */}
      {data.tools && data.tools.length > 0 && (
        <div>
          <h3 className="text-text-dim text-sm font-medium uppercase tracking-wide mb-3">
            Tool Usage
          </h3>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-text-dim text-xs uppercase">
                <th className="text-left py-2 px-3">Tool</th>
                <th className="text-left py-2 px-3">Calls</th>
              </tr>
            </thead>
            <tbody>
              {data.tools.map((t) => (
                <tr key={t.tool_name} className="border-b border-border hover:bg-surface transition-colors">
                  <td className="py-2 px-3 font-mono">{t.tool_name}</td>
                  <td className="py-2 px-3">{t.call_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Card({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className="bg-surface border border-border rounded-lg p-4">
      <div className="text-text-dim text-xs uppercase tracking-wide">{label}</div>
      <div className={`text-2xl font-semibold mt-1 ${color}`}>{value}</div>
    </div>
  );
}

function ActivityChart({ data }: { data: [string, number][] }) {
  const maxVal = Math.max(...data.map((d) => d[1]), 1);

  return (
    <div>
      <div className="flex items-end gap-1 h-16">
        {data.map((d, i) => {
          const height = (d[1] / maxVal) * 100;
          return (
            <div
              key={i}
              className="flex-1 bg-accent rounded-t-sm min-h-[2px] transition-all hover:bg-accent/80"
              style={{ height: `${height}%` }}
              title={`${d[0]}: ${d[1]} msgs`}
            />
          );
        })}
      </div>
      <div className="flex gap-1 mt-1">
        {data.map((d, i) => (
          <div key={i} className="flex-1 text-[10px] text-text-dim text-center">
            {i % 2 === 0 ? d[0].slice(5) : ""}
          </div>
        ))}
      </div>
    </div>
  );
}
