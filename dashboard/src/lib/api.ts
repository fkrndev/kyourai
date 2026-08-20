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

// -- MCP types --
export interface McpServer {
  name: string;
  transport: string;
  enabled: boolean;
  connected: boolean;
  description: string;
  auto_connect: boolean;
}

export interface McpBundled {
  name: string;
  description: string;
  transport: string;
  command: string;
  args: string[];
  env: Record<string, string>;
}

export interface McpStatus {
  name: string;
  connected: boolean;
  error: string;
  tools: string[];
}

// -- Goal types --
export interface Goal {
  goal_id: string;
  session_id: string;
  title: string;
  description: string;
  status: string;
  priority: string;
  progress: number;
  created_at: number;
  updated_at: number;
  completed_at: number;
  parent_goal_id: string;
  sub_goals: string[];
  outcome: string;
  blockers: string[];
  tags: string[];
  due_at: number;
}

export interface GoalSummary {
  total: number;
  active: number;
  completed: number;
  abandoned: number;
  blocked: number;
  avg_progress: number;
  high_priority: number;
  critical: number;
}

// -- Task types --
export interface TaskFlow {
  flow_id: string;
  title: string;
  status: string;
  revision: number;
  sync_mode: string;
  created_at: number;
  updated_at: number;
  completed_at: number;
  session_id: string;
  controller_id: string;
  steps: string[];
  current_step_index: number;
  state_json: string;
  progress_summary: string;
  terminal_summary: string;
}

export interface Task {
  task_id: string;
  flow_id: string;
  title: string;
  status: string;
  revision: number;
  created_at: number;
  started_at: number;
  completed_at: number;
  session_id: string;
  runtime: string;
  result: string;
  error: string;
  progress_summary: string;
  tool_use_count: number;
  last_tool_name: string;
  delivery_state: string;
}

// -- Audit types --
export interface AuditEvent {
  event_id: string;
  event_type: string;
  action: string;
  detail: string;
  timestamp: number;
  session_id: string;
  run_id: string;
  user_id: string;
  agent_id: string;
  severity: string;
  outcome: string;
  metadata: Record<string, unknown>;
}

// -- Provider types --
export interface Provider {
  id: string;
  name: string;
  has_key: boolean;
  models: string[];
}

// -- Coding context types --
export interface CodingContext {
  git: {
    is_repo: boolean;
    branch: string;
    dirty: boolean;
    remote: string;
  };
  languages: string[];
  frameworks: string[];
  package_managers: string[];
  test_frameworks: string[];
  linters: string[];
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

  healthDetailed: () =>
    fetchAPI<{
      status: string;
      version: string;
      timestamp: number;
      components: {
        name: string;
        healthy: boolean;
        detail: string;
        latency_ms: number;
      }[];
    }>("/v1/health/detailed"),

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

  // Usage
  getUsage: (days = 30) =>
    fetchAPI<{
      prompt_tokens: number;
      completion_tokens: number;
      total_tokens: number;
      cost_usd: number;
      calls: number;
      by_model: Record<string, {
        prompt_tokens: number;
        completion_tokens: number;
        cost_usd: number;
        calls: number;
      }>;
    }>(`/v1/usage?days=${days}`),

  getUsageByModel: (days = 30) =>
    fetchAPI<Record<string, {
      prompt_tokens: number;
      completion_tokens: number;
      total_tokens: number;
      cost_usd: number;
      calls: number;
    }>>(`/v1/usage?days=${days}&by_model=true`),

  // MCP
  listMcpServers: () =>
    fetchAPI<{ servers: McpServer[] }>("/v1/mcp/servers"),

  listMcpBundled: () =>
    fetchAPI<{ bundled: McpBundled[] }>("/v1/mcp/bundled"),

  registerMcpBundled: (name: string) =>
    fetchAPI<{ success: boolean; name: string; command: string }>(
      `/v1/mcp/register-bundled/${name}`,
      { method: "POST" }
    ),

  unregisterMcp: (name: string) =>
    fetchAPI<{ success: boolean; name: string }>(
      `/v1/mcp/servers/${name}`,
      { method: "DELETE" }
    ),

  getMcpStatus: () =>
    fetchAPI<{ statuses: McpStatus[] }>("/v1/mcp/status"),

  // Goals
  listGoals: (sessionId = "") =>
    fetchAPI<{
      active: Goal[];
      completed: Goal[];
      summary: GoalSummary;
    }>(`/v1/goals?session_id=${sessionId}`),

  createGoal: (data: {
    title: string;
    description?: string;
    priority?: string;
    session_id?: string;
    tags?: string[];
  }) =>
    fetchAPI<Goal>("/v1/goals", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  updateGoalProgress: (goalId: string, progress: number) =>
    fetchAPI<Goal>(`/v1/goals/${goalId}/progress?progress=${progress}`, {
      method: "POST",
    }),

  completeGoal: (goalId: string, outcome = "") =>
    fetchAPI<Goal>(`/v1/goals/${goalId}/complete`, {
      method: "POST",
      body: JSON.stringify({ outcome }),
    }),

  abandonGoal: (goalId: string) =>
    fetchAPI<Goal>(`/v1/goals/${goalId}`, { method: "DELETE" }),

  // Tasks
  listTaskFlows: (sessionId = "", status = "") =>
    fetchAPI<{
      flows: TaskFlow[];
      active_count: number;
    }>(`/v1/tasks/flows?session_id=${sessionId}&status=${status}`),

  getTaskFlow: (flowId: string) =>
    fetchAPI<{ flow: TaskFlow; tasks: Task[] }>(`/v1/tasks/flows/${flowId}`),

  createTaskFlow: (data: {
    title: string;
    steps?: string[];
    session_id?: string;
  }) =>
    fetchAPI<TaskFlow>("/v1/tasks/flows", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  cancelTaskFlow: (flowId: string) =>
    fetchAPI<TaskFlow>(`/v1/tasks/flows/${flowId}/cancel`, { method: "POST" }),

  listActiveTasks: (sessionId = "") =>
    fetchAPI<{ tasks: Task[]; flows: TaskFlow[] }>(
      `/v1/tasks/active?session_id=${sessionId}`
    ),

  // Audit
  getAuditEvents: (params: {
    event_type?: string;
    session_id?: string;
    severity?: string;
    limit?: number;
  } = {}) => {
    const qs = new URLSearchParams();
    if (params.event_type) qs.set("event_type", params.event_type);
    if (params.session_id) qs.set("session_id", params.session_id);
    if (params.severity) qs.set("severity", params.severity);
    if (params.limit) qs.set("limit", String(params.limit));
    return fetchAPI<{
      events: AuditEvent[];
      count: number;
    }>(`/v1/audit/events?${qs.toString()}`);
  },

  getAuditStats: (days = 7) =>
    fetchAPI<{
      total_events: number;
      by_type: Record<string, number>;
      by_severity: Record<string, number>;
      by_outcome: Record<string, number>;
      days: number;
    }>(`/v1/audit/stats?days=${days}`),

  // Providers
  listProviders: () =>
    fetchAPI<{ providers: Provider[] }>("/v1/providers"),

  // Coding context
  getCodingContext: () =>
    fetchAPI<CodingContext>("/v1/context"),
};
