"use client";

import { useState, useEffect } from "react";
import { api, McpServer, McpBundled, McpStatus } from "@/lib/api";

export function Mcp() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [bundled, setBundled] = useState<McpBundled[]>([]);
  const [statuses, setStatuses] = useState<McpStatus[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [actionMsg, setActionMsg] = useState("");

  const refresh = () => {
    setLoading(true);
    Promise.all([api.listMcpServers(), api.listMcpBundled(), api.getMcpStatus()])
      .then(([s, b, st]) => {
        setServers(s.servers);
        setBundled(b.bundled);
        setStatuses(st.statuses);
        setError("");
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    refresh();
  }, []);

  const handleRegister = (name: string) => {
    api
      .registerMcpBundled(name)
      .then(() => {
        setActionMsg(`Registered ${name}`);
        refresh();
        setTimeout(() => setActionMsg(""), 3000);
      })
      .catch((e) => setActionMsg(`Error: ${e}`));
  };

  const handleUnregister = (name: string) => {
    if (!confirm(`Unregister MCP server "${name}"?`)) return;
    api
      .unregisterMcp(name)
      .then(() => {
        setActionMsg(`Unregistered ${name}`);
        refresh();
        setTimeout(() => setActionMsg(""), 3000);
      })
      .catch((e) => setActionMsg(`Error: ${e}`));
  };

  if (loading) return <div className="text-text-dim">Loading MCP servers...</div>;
  if (error) return <div className="text-red">Error: {error}</div>;

  const getStatus = (name: string): McpStatus | undefined =>
    statuses.find((s) => s.name === name);

  return (
    <div className="space-y-6">
      {actionMsg && (
        <div className="card p-3 text-sm text-accent border-accent">{actionMsg}</div>
      )}

      {/* Registered servers */}
      <div>
        <h3 className="text-sm font-semibold text-text-dim mb-3">
          Registered Servers ({servers.length})
        </h3>
        {servers.length === 0 ? (
          <div className="card p-4 text-text-dim text-sm">
            No MCP servers registered. Register one from the bundled catalog below.
          </div>
        ) : (
          <div className="space-y-2">
            {servers.map((srv) => {
              const st = getStatus(srv.name);
              return (
                <div key={srv.name} className="card p-4">
                  <div className="flex items-center gap-3">
                    <span
                      className={`w-2.5 h-2.5 rounded-full ${
                        srv.connected ? "bg-green" : "bg-yellow"
                      }`}
                    />
                    <div className="flex-1">
                      <div className="font-medium">{srv.name}</div>
                      <div className="text-sm text-text-dim">
                        {srv.description || srv.transport}
                      </div>
                    </div>
                    <button
                      onClick={() => handleUnregister(srv.name)}
                      className="text-sm text-red hover:underline"
                    >
                      Remove
                    </button>
                  </div>
                  {st && st.tools.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1">
                      {st.tools.map((tool) => (
                        <span
                          key={tool}
                          className="text-xs px-2 py-0.5 bg-surface rounded border border-border"
                        >
                          {tool}
                        </span>
                      ))}
                    </div>
                  )}
                  {st && st.error && (
                    <div className="mt-2 text-sm text-red">{st.error}</div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Bundled catalog */}
      <div>
        <h3 className="text-sm font-semibold text-text-dim mb-3">
          Bundled Catalog ({bundled.length})
        </h3>
        <div className="grid md:grid-cols-2 gap-3">
          {bundled.map((b) => {
            const isRegistered = servers.some((s) => s.name === b.name);
            return (
              <div key={b.name} className="card p-4">
                <div className="flex items-start justify-between">
                  <div>
                    <div className="font-medium">{b.name}</div>
                    <div className="text-sm text-text-dim mt-1">
                      {b.description}
                    </div>
                  </div>
                  {isRegistered ? (
                    <span className="text-xs text-green">Registered</span>
                  ) : (
                    <button
                      onClick={() => handleRegister(b.name)}
                      className="text-sm text-accent hover:underline"
                    >
                      Register
                    </button>
                  )}
                </div>
                <div className="mt-2 text-xs text-text-dim">
                  <span>Transport: {b.transport}</span>
                  {b.args.length > 0 && (
                    <span className="ml-3">Args: {b.args.join(" ")}</span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
