// API client for Kyourai backend
// In dev, Next.js proxies /v1/* to FastAPI (see next.config.ts)
// In production, set NEXT_PUBLIC_API_BASE env var

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface Session {
  id: string;
  title: string | null;
  source: string;
  model: string;
  message_count: number;
  tool_call_count: number;
  started_at: number | null;
  ended_at: number | null;
}

export interface SessionListResponse {
  data: Session[];
  total: number;
  limit: number;
  offset: number;
}

export interface Message {
  role: string;
  content: string;
  timestamp: number | null;
}

export interface SessionDetail {
  session: Session;
  messages: Message[];
}

export interface SearchHit {
  session_id: string;
  title: string | null;
  role: string;
  content: string;
  snippet: string;
  timestamp: number | null;
}

export interface SearchResponse {
  results: SearchHit[];
  count: number;
  query: string;
}

export interface InsightsOverview {
  total_sessions: number;
  active_sessions: number;
  total_messages: number;
  total_tool_calls: number;
  total_facts: number;
  avg_messages_per_session: number;
}

export interface InsightsModel {
  model: string;
  sessions: number;
  messages: number;
  tool_calls: number;
}

export interface InsightsTool {
  tool_name: string;
  call_count: number;
}

export interface InsightsActivity {
  by_day: [string, number][];
  by_hour: [number, number][];
}

export interface InsightsResponse {
  overview: InsightsOverview;
  models: InsightsModel[];
  tools: InsightsTool[];
  activity: InsightsActivity;
  top_sessions: Session[];
  empty: boolean;
}

export interface ChatCompletionRequest {
  model: string;
  messages: { role: string; content: string }[];
  stream?: boolean;
}

export interface ChatCompletionResponse {
  id: string;
  choices: {
    index: number;
    message: { role: string; content: string };
    finish_reason: string;
  }[];
}

// ---------------------------------------------------------------------------
// API functions
// ---------------------------------------------------------------------------

async function fetchAPI<T>(path: string, options?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
  });
  if (!resp.ok) {
    throw new Error(`API error ${resp.status}: ${await resp.text()}`);
  }
  return resp.json();
}

export const api = {
  // Health
  health: () => fetchAPI<{ status: string; version: string }>("/health"),

  // Sessions
  listSessions: (limit = 50, offset = 0) =>
    fetchAPI<SessionListResponse>(`/v1/sessions?limit=${limit}&offset=${offset}`),

  getSession: (id: string) =>
    fetchAPI<SessionDetail>(`/v1/sessions/${id}`),

  searchSessions: (query: string, limit = 20) =>
    fetchAPI<SearchResponse>(
      `/v1/sessions/search?q=${encodeURIComponent(query)}&limit=${limit}`
    ),

  // Insights
  getInsights: (days = 30) =>
    fetchAPI<InsightsResponse>(`/v1/insights?days=${days}`),

  // Chat
  chat: (messages: { role: string; content: string }[]) =>
    fetchAPI<ChatCompletionResponse>("/v1/chat/completions", {
      method: "POST",
      body: JSON.stringify({
        model: "kyourai",
        messages,
        stream: false,
      }),
    }),
};
