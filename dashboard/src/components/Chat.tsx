"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { api } from "@/lib/api";

interface ChatMessage {
  role: string;
  content: string;
}

export function Chat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const historyRef = useRef<ChatMessage[]>([]);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  const send = useCallback(async () => {
    if (!input.trim() || sending) return;

    const userMsg = input.trim();
    setInput("");
    setSending(true);
    setError(null);

    // Add user message immediately
    setMessages((prev) => [...prev, { role: "user", content: userMsg }]);
    historyRef.current.push({ role: "user", content: userMsg });

    try {
      const response = await api.chat([...historyRef.current]);
      const reply = response.choices?.[0]?.message?.content || "(empty response)";
      setMessages((prev) => [...prev, { role: "assistant", content: reply }]);
      historyRef.current.push({ role: "assistant", content: reply });
    } catch (e) {
      const errMsg = e instanceof Error ? e.message : "Failed to send message";
      setError(errMsg);
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: `Error: ${errMsg}` },
      ]);
    } finally {
      setSending(false);
    }
  }, [input, sending]);

  return (
    <div className="flex flex-col h-[calc(100vh-200px)]">
      {/* Messages */}
      <div className="flex-1 overflow-y-auto bg-surface border border-border rounded-lg p-4 mb-3 space-y-4">
        {messages.length === 0 ? (
          <div className="text-text-dim text-center py-20">
            Send a message to start chatting with Kyourai.
          </div>
        ) : (
          messages.map((msg, i) => (
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
                {msg.content}
              </div>
            </div>
          ))
        )}
        <div ref={messagesEndRef} />
      </div>

      {error && <div className="text-red text-sm mb-2">{error}</div>}

      {/* Input */}
      <div className="flex gap-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="Type a message..."
          disabled={sending}
          className="flex-1 px-4 py-3 bg-surface border border-border rounded-lg text-text text-sm focus:outline-none focus:border-accent disabled:opacity-50"
        />
        <button
          onClick={send}
          disabled={sending}
          className="px-6 py-3 bg-accent text-white rounded-lg text-sm font-medium hover:opacity-90 transition-opacity disabled:opacity-50"
        >
          {sending ? "Sending..." : "Send"}
        </button>
      </div>
    </div>
  );
}
