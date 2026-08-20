"use client";

import { useState, useCallback } from "react";
import { api, type SearchResponse } from "@/lib/api";

export function Search() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const doSearch = useCallback(async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const result = await api.searchSessions(query);
      setResults(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Search failed");
    } finally {
      setLoading(false);
    }
  }, [query]);

  return (
    <div className="space-y-4">
      {/* Search bar */}
      <div className="flex gap-2">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && doSearch()}
          placeholder="Search session messages..."
          className="flex-1 px-4 py-2.5 bg-surface border border-border rounded-lg text-text text-sm focus:outline-none focus:border-accent"
        />
        <button
          onClick={doSearch}
          disabled={loading}
          className="px-6 py-2.5 bg-surface border border-border rounded-lg text-text text-sm hover:border-accent transition-colors disabled:opacity-50"
        >
          {loading ? "Searching..." : "Search"}
        </button>
      </div>

      {error && <div className="text-red text-sm">Error: {error}</div>}

      {/* Results */}
      {results && (
        <>
          <div className="text-text-dim text-sm">
            {results.count} result(s) for &quot;{results.query}&quot;
          </div>
          {results.results.length === 0 ? (
            <div className="text-text-dim text-center py-20">No results found.</div>
          ) : (
            <div className="space-y-2">
              {results.results.map((hit, i) => (
                <div
                  key={i}
                  className="bg-surface border border-border rounded-lg p-4 hover:border-accent transition-colors cursor-pointer"
                >
                  <div className="text-sm">
                    <span className="font-semibold">[{hit.role}]</span>{" "}
                    <span className="text-text-dim">
                      {hit.title || hit.session_id.slice(0, 20)}
                    </span>
                    {hit.timestamp && (
                      <span className="text-text-dim ml-2">
                        — {new Date(hit.timestamp * 1000).toLocaleString()}
                      </span>
                    )}
                  </div>
                  <div className="text-text-dim text-sm mt-1">
                    {hit.snippet || hit.content?.slice(0, 150)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
