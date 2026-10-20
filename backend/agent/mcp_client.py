"""MCP 서버 게이트웨이. 서버 기동 시 1회 연결해 모든 요청이 공유한다 (stateless 서비스, 연결만 싱글턴).

target:
  - FastMCP 인스턴스      → 프로세스 내 연결 (테스트)
  - "http://host:port/mcp" → Streamable HTTP (Docker 구성)
  - StdioTransport        → 서브프로세스 (로컬 개발). 환경변수는 명시한 것만 전달된다 (T-015).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from langsmith import traceable, tracing_context

from config.settings import Settings

ROOT = Path(__file__).resolve().parents[2]


def make_target(env: dict | None = None):
    s = Settings.from_env(env)
    if s.mcp_url:
        return s.mcp_url
    return StdioTransport(command=s.mcp_python or sys.executable,
                          args=[str(ROOT / "mcp_server" / "server.py")],
                          env={"DATA_MODE": s.data_mode, "PATH": os.environ.get("PATH", "")})


KST = timezone(timedelta(hours=9))


def mcp_cause(e: BaseException) -> str:
    """MCP 호출 예외 → 원인 코드 (ADR-014). 도구 안에서 난 예외는 ToolError, 그 밖(연결 끊김·기동 실패)은 연결 불가."""
    return "mcp_tool_error" if isinstance(e, ToolError) else "mcp_unreachable"


class MCPGateway:
    def __init__(self, target):
        self._target = target
        self._client: Client | None = None
        # 백엔드 → MCP 연결의 최근 성공·실패 (/health 용, 프로세스 메모리, 사용자 상태 아님)
        self.health = {"last_ok_at": None, "last_fail_at": None, "last_cause": None}

    def _note(self, cause: str | None) -> None:
        now = datetime.now(KST).isoformat(timespec="seconds")
        if cause is None:
            self.health["last_ok_at"] = now
        else:
            self.health.update(last_fail_at=now, last_cause=cause)

    async def start(self) -> None:
        self._client = Client(self._target)
        await self._client.__aenter__()

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    @traceable(run_type="tool", name="mcp_tool", process_inputs=lambda i: {"name": i.get("name"), "args": i.get("args")})
    async def tool(self, name: str, args: dict) -> dict:
        assert self._client is not None, "MCPGateway.start() 먼저"
        try:
            r = await self._client.call_tool(name, args)
        except Exception as e:
            self._note(mcp_cause(e))
            raise
        self._note(None)
        if r.data is not None:
            return r.data
        return json.loads(r.content[0].text)

    # Kakao 결과는 트레이스에 요약값만 남긴다 (ADR-011): 상태·법정동 코드·결과 수. 링크·장소 ID 는 남기지 않는다.
    @traceable(run_type="tool", name="resolve_place",
               process_inputs=lambda i: {"query": i.get("query")},
               process_outputs=lambda o: {k: o.get(k) for k in ("status", "b_code", "result_count", "region_id")})
    async def place(self, query: str) -> dict:
        with tracing_context(enabled=False):  # 하위 mcp_tool span 에 원문이 남지 않도록
            return await self.tool("resolve_place", {"query": query})

    async def prompt(self, name: str, args: dict) -> str:
        assert self._client is not None
        r = await self._client.get_prompt(name, args)
        return r.messages[0].content.text
