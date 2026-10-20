"""FastAPI 백엔드 — /chat (JSON), /chat/stream (SSE), /scenarios, /health, /version.

Stateless: 서버는 대화 상태를 보관하지 않는다. 클라이언트가 history_region_id 를 요청마다 실어 보낸다.
SSE 이벤트: start · step · route · intent · location · plan · source · judgement · validation · token · final · error
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from langsmith import tracing_context

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from agent.deps import Deps, Settings  # noqa: E402
from config.catalog import VERSION_TAG, VERSIONS  # noqa: E402
from config.settings import load_dotenv_file  # noqa: E402
from agent.graph import build_graph  # noqa: E402
from agent.llm import BaseLLM, make_llm  # noqa: E402
from agent.mcp_client import MCPGateway, make_target  # noqa: E402

VERSION = "0.1.0"
SENTINEL = object()
CHUNK = 24


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def with_links(final: dict, events: list[dict]) -> dict:
    """지도 링크 칩은 그래프 상태(=트레이스)에 두지 않고 이벤트로만 받아 응답에 붙인다 (ADR-011)."""
    links = [e for e in events if e.get("event") == "links"]
    return {**final, "links": {k: v for k, v in links[-1].items() if k != "event"} if links else None}


def create_app(mcp_target=None, llm: BaseLLM | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        gw = MCPGateway(mcp_target if mcp_target is not None else make_target())
        await gw.start()
        deps = Deps(mcp=gw, llm=llm or make_llm(), settings=Settings.from_env())
        app.state.deps = deps
        app.state.graph = build_graph(deps)
        yield
        await gw.stop()

    app = FastAPI(title="재난·특보 상황 판단 에이전트", version=VERSION, lifespan=lifespan)
    # 읽기 전용 GET API. nginx 프록시(same-origin)에서는 불필요하고, 로컬에서 FE 를 다른 포트로 열 때만 쓴다.
    app.add_middleware(CORSMiddleware, allow_origins=Settings.from_env().cors_origins.split(","),
                       allow_methods=["GET"], allow_headers=["*"])

    def trace_config(init: dict) -> dict:  # LangSmith 루트 이름·검색용 메타데이터 (트레이싱 꺼져 있으면 무시됨)
        return {"run_name": "disaster_chat", "tags": [init["mode"], VERSION_TAG],
                "metadata": {"run_id": init["run_id"], "mode": init["mode"], "llm": app.state.deps.llm.name, **VERSIONS}}

    def init_state(message, mode, as_of, history_region_id, browser_region_id, run_id):
        return {"query": message, "mode": mode or app.state.deps.settings.data_mode, "as_of": as_of or "",
                "history_region_id": history_region_id or "", "browser_region_id": browser_region_id or "",
                "run_id": run_id}

    @app.get("/health")
    async def health():
        """MCP 연결 + 소스별 최근 live 호출 성공·실패와 원인 코드 (키·URL 없음, ADR-014)."""
        d = app.state.deps
        sources = None
        try:
            with tracing_context(enabled=False):  # 헬스체크(10초 주기)가 트레이스 한도를 쓰지 않도록 (T-022)
                sources = (await d.mcp.tool("source_health", {}))["sources"]
            mcp_ok = True
        except Exception:  # noqa: BLE001
            mcp_ok = False
        return JSONResponse({"status": "ok" if mcp_ok else "degraded", "mcp": mcp_ok, "llm": d.llm.name,
                             "data_mode": d.settings.data_mode, "sources": sources, "mcp_link": d.mcp.health},
                            status_code=200 if mcp_ok else 503)

    @app.get("/version")
    async def version():
        return {"version": VERSION}

    @app.get("/scenarios")
    async def scenarios():
        return json.loads((HERE / "scenarios.json").read_text(encoding="utf-8"))

    @app.get("/chat")
    async def chat(message: str = Query(..., min_length=1, max_length=500), mode: str | None = None,
                   as_of: str | None = None, history_region_id: str | None = None,
                   browser_region_id: str | None = None):
        run_id = uuid.uuid4().hex[:12]
        init = init_state(message, mode, as_of, history_region_id, browser_region_id, run_id)
        side: list[dict] = []
        state = await app.state.graph.ainvoke(init, config={**trace_config(init), "configurable": {"emit": side.append}})
        return with_links(state["final"], side)

    @app.get("/chat/stream")
    async def chat_stream(message: str = Query(..., min_length=1, max_length=500), mode: str | None = None,
                          as_of: str | None = None, history_region_id: str | None = None,
                          browser_region_id: str | None = None):
        run_id = uuid.uuid4().hex[:12]
        queue: asyncio.Queue = asyncio.Queue()
        init = init_state(message, mode, as_of, history_region_id, browser_region_id, run_id)

        side: list[dict] = []

        def emit(ev: dict) -> None:
            side.append(ev)
            queue.put_nowait(ev)

        async def runner():
            try:
                state = await app.state.graph.ainvoke(init, config={**trace_config(init), "configurable": {"emit": emit}})
                final = with_links(state["final"], side)
                text = final["answer"]
                for i in range(0, len(text), CHUNK):  # 검증을 통과한 답변만 스트리밍 (validate-then-stream)
                    queue.put_nowait({"event": "token", "text": text[i:i + CHUNK]})
                queue.put_nowait({"event": "final", **final})
            except Exception as e:  # noqa: BLE001
                queue.put_nowait({"event": "error", "message": f"{type(e).__name__}: {e}"[:300]})
            finally:
                queue.put_nowait(SENTINEL)

        async def gen():
            task = asyncio.create_task(runner())
            try:
                yield sse("start", {"run_id": run_id, "mode": init["mode"], "as_of": init["as_of"]})
                while True:
                    ev = await queue.get()
                    if ev is SENTINEL:
                        break
                    yield sse(ev["event"], ev)
            finally:  # 클라이언트가 끊으면 작업도 취소
                if not task.done():
                    task.cancel()

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


def app_factory() -> FastAPI:  # uvicorn --factory backend.server:app_factory
    load_dotenv_file(HERE.parent / ".env")
    return create_app()
