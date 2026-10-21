"""시연용 장애 주입 (기본 꺼짐, ADR-021). DEMO_FAULTS=1 인 개발·시연 환경에서만 요청의 fault 값을 받는다.

공개 서비스에 사용자가 장애를 일으키는 경로를 두지 않기 위해: 꺼져 있으면 fault 가 붙은 요청은 404, 시나리오 목록에서도 숨긴다.
바꾸는 곳은 백엔드 → MCP 경계뿐이다(요청마다 감싸는 래퍼 — 서버에 상태를 남기지 않는다). 원인 분류·문구는 실제 코드 경로 그대로.
외부 API 쪽 장애(잘못된 키·resultCode 99·잘린 JSON·시간 초과·한도)는 MCP 프로세스 안이라 여기서 만들지 않고 eval/faults.py 로만 본다.
"""
from __future__ import annotations

from fastmcp.exceptions import ToolError

FAULTS = ("mcp_down", "alert_tool_error", "shelter_down")


class FaultGateway:
    """kind: mcp_down(모든 호출 연결 실패) | alert_tool_error(특보 도구만 도구 오류) | shelter_down(쉼터 도구만 도구 오류)."""

    def __init__(self, inner, kind: str):
        assert kind in FAULTS, kind
        self.inner, self.kind = inner, kind

    @property
    def health(self):
        return self.inner.health

    def _check(self, name: str) -> None:
        if self.kind == "mcp_down":
            raise RuntimeError("Client failed to connect (demo fault)")
        if (self.kind, name) in (("alert_tool_error", "get_alert_status"), ("shelter_down", "get_shelters")):
            raise ToolError(f"{name} failed (demo fault)")

    async def tool(self, name: str, args: dict) -> dict:
        self._check(name)
        return await self.inner.tool(name, args)

    async def place(self, query: str) -> dict:
        self._check("resolve_place")
        return await self.inner.place(query)

    async def prompt(self, name: str, args: dict) -> str:
        self._check(name)
        return await self.inner.prompt(name, args)
