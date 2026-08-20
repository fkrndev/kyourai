"use client";

import { useState, useEffect, useCallback } from "react";
import { api, type SessionListResponse, type SessionDetail } from "@/lib/api";

export function Sessions() {
  const [list, setList] = useState<SessionListResponse | null>(null);
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadList = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.listSessions(50);
      setList(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load sessions");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadList();
  }, [loadList]);

  const showDetail = useCallback(async (id: string) => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.getSession(id);
      setDetail(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load session");
    } finally {
      setLoading(false);
    }
  }, []);

  if (loading && !list && !detail)
    return <div className="text-text-dim text-center py-20">Loading...</div>;
  if (error)
    return <div className="text-red text-center py-20">Error: {error}</div>;

  // Detail view
  if (detail) {
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-4">
          <button
            onClick={() => {
              setDetail(null);
              loadList();
            }}
            className="px-4 py-2 bg-surface border border-border rounded text-text text-sm hover:border-accent transition-colors"
          >
            ← Back to list
          </button>
          <h2 className="text-lg font-semibold">
            {detail.session.title || detail.session.id}
          </h2>
        </div>

        <div className="bg-surface border border-border rounded-lg p-4 max-h-[600px] overflow-y-auto space-y-4">
          {detail.messages.filter((m) => m.content).map((msg, i) => (
            <div
              key={i}
              className={`max-w-[80%] ${msg.role === "user" ? "ml-auto" : ""}`}
            >
              <div className="text-text-dim text-xs uppercase mb-1">{msg.role}</div>
              <div
                className={`p-3 rounded-lg text-sm whitespace-pre-wrap break-words ${
                  msg.role === "user"
                    ? "bg-accent text-white"
                    : "bg-bg border border-border"
                }`}
              >
                {msg.content.slice(0, 500)}
                {msg.content.length > 500 && "..."}
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  // List view
  if (!list || !list.data.length)
    return <div className="text-text-dim text-center py-20">No sessions found.</div>;

  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-border text-text-dim text-xs uppercase">
          <th className="text-left py-2 px-3">Title</th>
          <th className="text-left py-2 px-3">Source</th>
          <th className="text-left py-2 px-3">Model</th>
          <th className="text-left py-2 px-3">Messages</th>
          <th className="text-left py-2 px-3">Tools</th>
          <th className="text-left py-2 px-3">Started</th>
        </tr>
      </thead>
      <tbody>
        {list.data.map((s) => (
          <tr
            key={s.id}
            onClick={() => showDetail(s.id)}
            className="border-b border-border hover:bg-surface transition-colors cursor-pointer"
          >
            <td className="py-2 px-3">{(s.title || s.id).slice(0, 30)}</td>
            <td className="py-2 px-3">{s.source}</td>
            <td className="py-2 px-3">{(s.model || "").slice(0, 25)}</td>
            <td className="py-2 px-3">{s.message_count}</td>
            <td className="py-2 px-3">{s.tool_call_count}</td>
            <td className="py-2 px-3">
              {s.started_at ? new Date(s.started_at * 1000).toLocaleString() : ""}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
