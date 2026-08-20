"use client";

import { useState, useEffect } from "react";
import { Insights } from "@/components/Insights";
import { Sessions } from "@/components/Sessions";
import { Search } from "@/components/Search";
import { Chat } from "@/components/Chat";
import { api } from "@/lib/api";

type Tab = "insights" | "sessions" | "search" | "chat";

const TABS: { id: Tab; label: string }[] = [
  { id: "insights", label: "Insights" },
  { id: "sessions", label: "Sessions" },
  { id: "search", label: "Search" },
  { id: "chat", label: "Chat" },
];

export default function Home() {
  const [activeTab, setActiveTab] = useState<Tab>("insights");
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    api
      .health()
      .then(() => setConnected(true))
      .catch(() => setConnected(false));
  }, []);

  return (
    <div className="min-h-screen flex flex-col">
      {/* Header */}
      <header className="border-b border-border px-6 py-4 flex items-center gap-3">
        <span className="text-2xl">⚡</span>
        <h1 className="text-xl font-semibold">Kyourai Dashboard</h1>
        <div className="ml-auto flex items-center gap-2 text-sm text-text-dim">
          <span
            className={`w-2 h-2 rounded-full ${connected ? "bg-green" : "bg-red"}`}
          />
          <span>{connected ? "Connected" : "Offline"}</span>
        </div>
      </header>

      {/* Tabs */}
      <nav className="border-b border-border px-6 flex gap-0">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`px-5 py-3 text-sm font-medium border-b-2 transition-colors ${
              activeTab === tab.id
                ? "text-accent border-accent"
                : "text-text-dim border-transparent hover:text-text"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      {/* Content */}
      <main className="flex-1 p-6 max-w-6xl mx-auto w-full">
        {activeTab === "insights" && <Insights />}
        {activeTab === "sessions" && <Sessions />}
        {activeTab === "search" && <Search />}
        {activeTab === "chat" && <Chat />}
      </main>
    </div>
  );
}
