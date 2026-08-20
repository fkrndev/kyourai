"use client";

import { useState, useEffect } from "react";
import { api } from "@/lib/api";

export function Usage() {
  const [data, setData] = useState<{
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    cost_usd: number;
    calls: number;
    by_model: Record<
      string,
      {
        prompt_tokens: number;
        completion_tokens: number;
        cost_usd: number;
        calls: number;
      }
    >;
  } | null>(null);
  const [days, setDays] = useState(30);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api
      .getUsage(days)
      .then((d) => {
        setData(d);
        setError("");
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [days]);

  if (loading && !data) return <div className="text-text-dim">Loading usage...</div>;
  if (error) return <div className="text-red">Error: {error}</div>;
  if (!data) return null;

  const modelEntries = Object.entries(data.by_model).sort(
    (a, b) => b[1].cost_usd - a[1].cost_usd
  );

  return (
    <div className="space-y-6">
      {/* Time range selector */}
      <div className="flex gap-2">
        {[7, 30, 90].map((d) => (
          <button
            key={d}
            onClick={() => setDays(d)}
            className={`px-3 py-1.5 text-sm rounded border transition-colors ${
              days === d
                ? "border-accent text-accent"
                : "border-border text-text-dim hover:text-text"
            }`}
          >
            {d} days
          </button>
        ))}
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="card p-4">
          <div className="text-sm text-text-dim">Total Tokens</div>
          <div className="text-2xl font-bold mt-1 tabular-nums">
            {data.total_tokens.toLocaleString()}
          </div>
        </div>
        <div className="card p-4">
          <div className="text-sm text-text-dim">Prompt Tokens</div>
          <div className="text-2xl font-bold mt-1 tabular-nums">
            {data.prompt_tokens.toLocaleString()}
          </div>
        </div>
        <div className="card p-4">
          <div className="text-sm text-text-dim">Completion Tokens</div>
          <div className="text-2xl font-bold mt-1 tabular-nums">
            {data.completion_tokens.toLocaleString()}
          </div>
        </div>
        <div className="card p-4">
          <div className="text-sm text-text-dim">Est. Cost</div>
          <div className="text-2xl font-bold mt-1 tabular-nums text-accent">
            ${data.cost_usd.toFixed(4)}
          </div>
        </div>
      </div>

      {/* API calls */}
      <div className="card p-4">
        <div className="text-sm text-text-dim">API Calls</div>
        <div className="text-xl font-semibold mt-1 tabular-nums">
          {data.calls.toLocaleString()}
        </div>
      </div>

      {/* Per-model breakdown */}
      {modelEntries.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-text-dim mb-3">
            Usage by Model
          </h3>
          <div className="space-y-2">
            {modelEntries.map(([model, stats]) => (
              <div key={model} className="card p-4">
                <div className="flex items-center justify-between">
                  <span className="font-medium">{model}</span>
                  <span className="text-accent font-semibold tabular-nums">
                    ${stats.cost_usd.toFixed(4)}
                  </span>
                </div>
                <div className="grid grid-cols-3 gap-4 mt-3 text-sm">
                  <div>
                    <span className="text-text-dim">Prompt: </span>
                    <span className="tabular-nums">
                      {stats.prompt_tokens.toLocaleString()}
                    </span>
                  </div>
                  <div>
                    <span className="text-text-dim">Completion: </span>
                    <span className="tabular-nums">
                      {stats.completion_tokens.toLocaleString()}
                    </span>
                  </div>
                  <div>
                    <span className="text-text-dim">Calls: </span>
                    <span className="tabular-nums">{stats.calls}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
