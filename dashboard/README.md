# Kyourai Dashboard (Next.js)

Modern web dashboard for Kyourai — analytics, session history, search, and live chat.

## Quick start

```bash
# 1. Start the Kyourai API server
kyourai serve --port 8000

# 2. Start the dashboard (in another terminal)
cd dashboard
npm run dev
```

Dashboard runs at http://localhost:3000 and proxies API calls to http://localhost:8000.

## Features

- **Insights** — overview cards (sessions, messages, tools, facts), model breakdown, tool usage, 14-day activity chart
- **Sessions** — list all sessions, click through to view message history
- **Search** — full-text search across all session messages (FTS5)
- **Chat** — live chat with the Kyourai agent

## Tech stack

- Next.js 16 (App Router, Turbopack)
- TypeScript
- Tailwind CSS v4
- Zero external API client — just fetch()

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `KYOURAI_BACKEND` | `http://localhost:8000` | FastAPI backend URL (for dev proxy) |
| `NEXT_PUBLIC_API_BASE` | `""` (same origin) | API base URL in production |

## Production build

```bash
npm run build
npm start
```

Or export as static site and serve from FastAPI:

```bash
npm run build  # outputs to .next/
```

## Project structure

```
dashboard/
  src/
    app/
      layout.tsx      # Root layout (dark theme, fonts)
      page.tsx        # Main page (tab navigation)
      globals.css     # Tailwind + CSS variables
    components/
      Insights.tsx    # Analytics tab
      Sessions.tsx    # Session list + detail
      Search.tsx      # FTS5 search
      Chat.tsx        # Live chat
    lib/
      api.ts          # API client + TypeScript types
  next.config.ts      # Rewrites: /v1/* → FastAPI
```
