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
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

ROOT = Path(__file__).resolve().parents[2]


def make_target(env: dict | None = None):
    env = env if env is not None else os.environ
    url = env.get("MCP_URL")
    if url:
        return url
    return StdioTransport(command=env.get("MCP_PYTHON", sys.executable),
                          args=[str(ROOT / "mcp_server" / "server.py")],
                          env={"DATA_MODE": env.get("DATA_MODE", "replay"), "PATH": os.environ.get("PATH", "")})


class MCPGateway:
    def __init__(self, target):
        self._target = target
        self._client: Client | None = None

    async def start(self) -> None:
        self._client = Client(self._target)
        await self._client.__aenter__()

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    async def tool(self, name: str, args: dict) -> dict:
        assert self._client is not None, "MCPGateway.start() 먼저"
        r = await self._client.call_tool(name, args)
        if r.data is not None:
            return r.data
        return json.loads(r.content[0].text)

    async def prompt(self, name: str, args: dict) -> str:
        assert self._client is not None
        r = await self._client.get_prompt(name, args)
        return r.messages[0].content.text
