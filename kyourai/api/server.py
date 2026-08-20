"""OpenAI-compatible API server — expose Kyourai via /v1/chat/completions.

This lets any OpenAI-compatible client (Open WebUI, LobeChat, LibreChat,
curl, etc.) use Kyourai as a backend. The agent's memory system is
transparently injected — clients don't need to know about memory tools.

Endpoints:
  GET  /v1/models              — list available models
  GET  /v1/models/{id}         — get model info
  POST /v1/chat/completions    — chat completion (streaming + non-streaming)
  POST /v1/embeddings          — embeddings (not supported, returns 501)
  GET  /v1/sessions            — list session history
  GET  /v1/sessions/{id}       — get session with messages
  GET  /v1/sessions/search?q=  — full-text search session messages
  GET  /v1/insights            — usage analytics
  GET  /health                 — health check
  GET  /                       — web dashboard (pre-built Next.js static export)

Authentication: Bearer token via KYOURAI_API_KEY env var (optional).
If not set, no auth required (for local development).

Usage:
  kyourai serve --port 18789
  # then from any OpenAI-compatible client:
  # base_url = http://localhost:18789/v1
  # api_key = any string (if KYOURAI_API_KEY not set)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request/Response models (OpenAI-compatible)
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "kyourai"
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    # Accept and ignore other OpenAI params for compatibility
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    stop: str | list[str] | None = None
    user: str | None = None
    n: int | None = None
    seed: int | None = None

    model_config = {"extra": "allow"}


class ChatCompletionResponseChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionResponseChoice]
    usage: dict[str, int] = Field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})


class ChatCompletionChunkChoice(BaseModel):
    index: int
    delta: dict[str, str]
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChatCompletionChunkChoice]


class ModelInfo(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str = "kyourai"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelInfo]


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------

def create_app(
    agent_factory: Any = None,
    *,
    default_model: str = "openai:gpt-4o",
    api_key: str | None = None,
) -> FastAPI:
    """Create the FastAPI app.

    Args:
        agent_factory: callable that returns a KyouraiAgent instance.
                       If None, a default agent is created lazily.
        default_model: default model to use when client sends model="kyourai"
        api_key: optional API key for auth. If None, reads KYOURAI_API_KEY env.
    """
    app = FastAPI(title="Kyourai API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _api_key = api_key or os.environ.get("KYOURAI_API_KEY")
    _agent_cache: dict[str, Any] = {}  # session_id → agent

    def _get_agent(session_id: str, model: str | None = None):
        """Get or create an agent for a session."""
        if session_id in _agent_cache:
            return _agent_cache[session_id]
        if agent_factory:
            agent = agent_factory(session_id=session_id, model=model or default_model)
        else:
            from kyourai.agent import KyouraiAgent
            agent = KyouraiAgent(
                model=model or default_model,
                session_id=session_id,
                enable_curator=True,
            )
        _agent_cache[session_id] = agent
        return agent

    def _check_auth(request: Request):
        if not _api_key:
            return
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            if token == _api_key:
                return
        raise HTTPException(status_code=401, detail="Invalid API key")

    def _resolve_model(model: str) -> str:
        """Map OpenAI model names to Kyourai model names."""
        if model in ("kyourai", "kyourai/default"):
            return default_model
        if model.startswith("kyourai/"):
            return model[len("kyourai/"):]
        return model

    def _extract_user_message(messages: list[ChatMessage]) -> str:
        """Get the last user message."""
        for msg in reversed(messages):
            if msg.role == "user":
                return msg.content
        return ""

    def _build_history(messages: list[ChatMessage]) -> list[dict]:
        """Convert messages to Pydantic AI message history."""
        history = []
        for msg in messages[:-1]:  # exclude last (will be the prompt)
            history.append({"role": msg.role, "content": msg.content})
        return history

    # -- Routes --------------------------------------------------------------

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/v1/models")
    async def list_models(request: Request):
        _check_auth(request)
        models = [
            ModelInfo(id="kyourai", created=int(time.time())),
            ModelInfo(id="kyourai/default", created=int(time.time())),
        ]
        return ModelList(data=models)

    @app.get("/v1/models/{model_id}")
    async def get_model(model_id: str, request: Request):
        _check_auth(request)
        return ModelInfo(id=model_id, created=int(time.time()))

    @app.post("/v1/chat/completions")
    async def chat_completions(req: ChatCompletionRequest, request: Request):
        _check_auth(request)

        if not req.messages:
            raise HTTPException(status_code=400, detail="messages must not be empty")

        session_id = req.user or f"api-{request.client.host if request.client else 'unknown'}"
        model = _resolve_model(req.model)
        agent = _get_agent(session_id, model)

        user_message = _extract_user_message(req.messages)
        history = _build_history(req.messages)

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        if req.stream:
            return StreamingResponse(
                _stream_response(agent, user_message, history, completion_id, created, req.model),
                media_type="text/event-stream",
            )

        # Non-streaming
        try:
            output = await agent.run(user_message, message_history=history)
            agent.sync_turn(user_message, output)
        except Exception as e:
            logger.error("Chat completion failed: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

        return ChatCompletionResponse(
            id=completion_id,
            created=created,
            model=req.model,
            choices=[
                ChatCompletionResponseChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=output),
                    finish_reason="stop",
                )
            ],
        )

    async def _stream_response(agent, prompt, history, completion_id, created, model_name):
        """Stream SSE chunks matching OpenAI format."""
        try:
            collected = ""
            async for chunk in agent.run_stream(prompt, message_history=history):
                collected += chunk
                chunk_data = ChatCompletionChunk(
                    id=completion_id,
                    created=created,
                    model=model_name,
                    choices=[ChatCompletionChunkChoice(index=0, delta={"content": chunk})],
                )
                yield f"data: {chunk_data.model_dump_json()}\n\n"

            # Final chunk with finish_reason
            final = ChatCompletionChunk(
                id=completion_id,
                created=created,
                model=model_name,
                choices=[ChatCompletionChunkChoice(index=0, delta={}, finish_reason="stop")],
            )
            yield f"data: {final.model_dump_json()}\n\n"
            yield "data: [DONE]\n\n"

            agent.sync_turn(prompt, collected)
        except Exception as e:
            logger.error("Streaming failed: %s", e)
            error_chunk = {"error": {"message": str(e), "type": "server_error"}}
            yield f"data: {json.dumps(error_chunk)}\n\n"

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        _check_auth(request)
        # Embeddings are not supported — Kyourai uses HRR, not embeddings.
        raise HTTPException(status_code=501, detail="Kyourai uses HRR vectors, not embeddings. Use the MCP server for memory search.")

    # -- Session history endpoints ------------------------------------------

    @app.get("/v1/sessions")
    async def list_sessions(
        request: Request,
        limit: int = 50,
        offset: int = 0,
        source: str | None = None,
    ):
        """List session history."""
        _check_auth(request)
        from kyourai.state import SessionDB

        db = SessionDB()
        try:
            sessions = db.list_sessions(limit=limit, offset=offset, source=source)
            total = db.count_sessions(source=source)
        finally:
            db.close()
        return {"object": "list", "data": sessions, "total": total}

    @app.get("/v1/sessions/{session_id}")
    async def get_session(session_id: str, request: Request):
        """Get a specific session with its messages."""
        _check_auth(request)
        from kyourai.state import SessionDB

        db = SessionDB()
        try:
            session = db.get_session(session_id)
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            messages = db.get_messages(session_id, limit=200)
        finally:
            db.close()
        return {"session": session, "messages": messages}

    @app.get("/v1/sessions/search")
    async def search_sessions(request: Request, q: str, limit: int = 20):
        """Full-text search across session messages."""
        _check_auth(request)
        from kyourai.state import SessionDB

        db = SessionDB()
        try:
            results = db.search_messages(q, limit=limit)
        finally:
            db.close()
        return {"query": q, "results": results, "count": len(results)}

    # -- Insights endpoint ---------------------------------------------------

    @app.get("/v1/insights")
    async def insights(request: Request, days: int = 30):
        """Get usage insights and analytics."""
        _check_auth(request)
        from kyourai.state import SessionDB, InsightsEngine

        db = SessionDB()
        try:
            engine = InsightsEngine(db)
            report = engine.generate(days=days)
        finally:
            db.close()
        return report

    # -- Dashboard (pre-built Next.js static export) -----------------------

    _dashboard_dir = Path(__file__).resolve().parent.parent.parent / "dashboard" / "out"

    if _dashboard_dir.is_dir():
        # Serve Next.js static export at /
        # Individual HTML files (index.html, 404.html) via FileResponse
        # Assets (_next/*) via StaticFiles
        _assets_dir = _dashboard_dir / "_next"
        if _assets_dir.is_dir():
            app.mount("/_next", StaticFiles(directory=str(_assets_dir)), name="next-assets")

        @app.get("/", response_class=HTMLResponse)
        async def dashboard_index():
            index = _dashboard_dir / "index.html"
            if index.exists():
                return FileResponse(str(index))
            raise HTTPException(status_code=404, detail="Dashboard not built. Run: cd dashboard && npm run build:static")

        # Catch-all for client-side routing (e.g. /sessions, /chat)
        @app.get("/{path:path}")
        async def dashboard_catch_all(path: str):
            # Don't intercept API routes
            if path.startswith("v1/") or path == "health":
                raise HTTPException(status_code=404)
            file_path = _dashboard_dir / path
            if file_path.is_file():
                return FileResponse(str(file_path))
            # Fallback to index.html for client-side routing
            index = _dashboard_dir / "index.html"
            if index.exists():
                return FileResponse(str(index))
            raise HTTPException(status_code=404, detail="Not found")

    return app


def run_server(
    host: str = "127.0.0.1",
    port: int = 18789,
    *,
    model: str | None = None,
    api_key: str | None = None,
) -> None:
    """Run the API server (blocking)."""
    import uvicorn

    default_model = model or os.environ.get("KYOURAI_MODEL", "openai:gpt-4o")
    app = create_app(default_model=default_model, api_key=api_key)
    logger.info("Starting Kyourai API server on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
