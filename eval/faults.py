"""장애 주입 (테스트·평가 공용). 실제 코드 경로를 그대로 쓰고 네트워크 경계(urlopen)와 MCP 연결만 바꾼다.

API 장애 (프로세스 내 MCP 서버, mode=live 로 호출):
  wrong_key       기상청 HTTP 403 + SERVICE_KEY_IS_NOT_REGISTERED_ERROR / safetydata resultCode 30 (2026-10-20 실제 응답 모양, T-028)
  result_99       HTTP 200 + resultCode 99
  truncated_json  HTTP 200 + 잘린 JSON
  timeout         소켓 시간 초과
  rate_limited    resultCode 22 (LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR)
MCP 장애:
  mcp_down        모든 MCP 호출이 연결 실패 (DownGateway)
  shelter_down    쉼터 도구(get_shelters)만 실패, 나머지는 replay 그대로 (ShelterDownGateway) — 쉼터가 빠져도 답변은 정상
키는 가짜 값이며 fixture·카운터는 임시 폴더에 쓴다 (저장소의 data/ 를 건드리지 않는다).
"""
from __future__ import annotations

import io
import json
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "mcp_server", ROOT / "backend", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

API_FAULTS = ("wrong_key", "result_99", "truncated_json", "timeout", "rate_limited")
EXPECTED_CAUSE = {"wrong_key": "api_auth_error", "result_99": "api_result_error", "truncated_json": "response_invalid",
                  "timeout": "api_timeout", "rate_limited": "rate_limited", "mcp_down": "mcp_unreachable",
                  "shelter_down": "mcp_tool_error"}
FAKE_KEYS = {"WEATHER_ALERTS_SERVICE_KEY": "FAKEKEY0001", "DSSP_SERVICE_KEY": "FAKEKEY0002",
             "DSSP_MESSAGE_SERVICE_KEY": "FAKEKEY0002", "DSSP_SHELTER_SERVICE_KEY": "FAKEKEY0003"}


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _body(kind: str, weather: bool) -> bytes:
    def hdr(code, m):
        h = {"resultCode": code, "resultMsg": m}
        return {"response": {"header": h}} if weather else {"header": h, "body": None}
    if kind == "result_99":
        return json.dumps(hdr("99", "UNKNOWN_ERROR")).encode()
    if kind == "rate_limited":
        return json.dumps(hdr("22", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR")).encode()
    if kind == "wrong_key":
        return json.dumps(hdr("30", "SERVICE KEY IS NOT REGISTERED ERROR")).encode()
    if kind == "truncated_json":
        return b'{"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL' if weather else b'{"header": {"resultCode": "00"'
    raise ValueError(kind)


def fake_urlopen(kind: str):
    def _open(req, timeout=0):
        url = req.full_url
        weather = "WthrWrnInfoService" in url
        if kind == "timeout":
            raise urllib.error.URLError(socket.timeout("timed out"))
        if kind == "wrong_key" and weather:
            body = json.dumps({"OpenAPI_ServiceResponse": {"cmmMsgHeader": {
                "errMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "returnReasonCode": "30"}}}).encode()
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(body))
        return _Resp(_body(kind, weather))
    return _open


@contextmanager
def api_fault(kind: str):
    """외부 API 가 kind 대로 실패하게 만든다 (프로세스 내 MCP 서버에만 적용)."""
    import capture
    import failures
    assert kind in API_FAULTS, kind
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as st:
        st.enter_context(mock.patch.object(capture, "FIXTURES", Path(tmp)))
        st.enter_context(mock.patch.object(capture, "COUNTER", Path(tmp) / "_c.json"))
        st.enter_context(mock.patch.object(capture, "ROOT", Path(tmp)))
        st.enter_context(mock.patch.object(capture, "load_env", lambda *a, **k: dict(FAKE_KEYS)))
        st.enter_context(mock.patch.object(urllib.request, "urlopen", fake_urlopen(kind)))
        st.enter_context(mock.patch.dict(failures.HEALTH, clear=True))
        yield


class DownGateway:
    """MCP 서버가 내려간 상태: 모든 호출이 연결 실패 (fastmcp Client 가 내는 것과 같은 RuntimeError)."""

    def __init__(self):
        self.health = {"last_ok_at": None, "last_fail_at": None, "last_cause": None}

    async def start(self):
        return None

    async def stop(self):
        return None

    async def tool(self, name, args):
        self.health["last_cause"] = "mcp_unreachable"
        raise RuntimeError("Client failed to connect: All connection attempts failed")

    async def place(self, query):
        return await self.tool("resolve_place", {"query": query})

    async def prompt(self, name, args):
        return await self.tool(name, args)


def shelter_down_gateway():
    """쉼터 도구만 도구 내부 오류(ToolError)로 실패하는 게이트웨이 (프로세스 내 MCP 서버)."""
    from fastmcp.exceptions import ToolError

    from agent.mcp_client import MCPGateway

    class ShelterDownGateway(MCPGateway):
        async def tool(self, name, args):
            if name == "get_shelters":
                raise ToolError("shelter tool failed (fault injection)")
            return await super().tool(name, args)
    import server as mcp_module
    return ShelterDownGateway(mcp_module.mcp)
