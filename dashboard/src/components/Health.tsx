"use client";

import { useState, useEffect } from "react";
import { api } from "@/lib/api";

export function Health() {
  const [data, setData] = useState<{
    status: string;
    version: string;
    timestamp: number;
    components: {
      name: string;
      healthy: boolean;
      detail: string;
      latency_ms: number;
    }[];
  } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const refresh = () => {
    setLoading(true);
    api
      .healthDetailed()
      .then((d) => {
        setData(d);
        setError("");
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 10000);
    return () => clearInterval(interval);
  }, []);

  if (loading && !data) return <div className="text-text-dim">Loading health...</div>;
  if (error) return <div className="text-red">Error: {error}</div>;
  if (!data) return null;

  const allHealthy = data.components.every((c) => c.healthy);

  return (
    <div className="space-y-6">
      {/* Overview */}
      <div className="card p-4">
        <div className="flex items-center gap-3">
          <span
            className={`w-3 h-3 rounded-full ${allHealthy ? "bg-green" : "bg-red"}`}
          />
          <h2 className="text-lg font-semibold">
            {allHealthy ? "All Systems Healthy" : "Issues Detected"}
          </h2>
          <span className="ml-auto text-sm text-text-dim">
            v{data.version}
          </span>
        </div>
        <p className="text-sm text-text-dim mt-2">
          Last checked: {new Date(data.timestamp * 1000).toLocaleTimeString()}
        </p>
      </div>

      {/* Components */}
      <div className="space-y-2">
        {data.components.map((comp) => (
          <div key={comp.name} className="card p-4 flex items-center gap-3">
            <span
              className={`w-2.5 h-2.5 rounded-full ${
                comp.healthy ? "bg-green" : "bg-red"
              }`}
            />
            <div className="flex-1">
              <div className="font-medium">{comp.name}</div>
              <div className="text-sm text-text-dim">{comp.detail}</div>
            </div>
            <span className="text-sm text-text-dim tabular-nums">
              {comp.latency_ms}ms
            </span>
          </div>
        ))}
      </div>

      <button
        onClick={refresh}
        className="px-4 py-2 text-sm border border-border rounded hover:bg-surface transition-colors"
      >
        Refresh
      </button>
    </div>
  );
}
