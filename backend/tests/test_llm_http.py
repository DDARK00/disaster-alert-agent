"""OpenAI 호환 클라이언트 테스트 — 로컬 가짜 HTTP 서버 (실제 Ollama 없이 프로토콜 검증)."""
import asyncio
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "mcp_server"))

import server as mcp_module  # noqa: E402
from agent.deps import Deps, Settings  # noqa: E402
from agent.graph import build_graph  # noqa: E402
from agent.llm import LLMError, OpenAICompatLLM  # noqa: E402
from agent.mcp_client import MCPGateway  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    mode = "ok"
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.seen.append((self.path, body, self.headers.get("Authorization")))
        if Handler.mode == "500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"oops")
            return
        system = body["messages"][0]["content"]
        if Handler.mode == "bad_shape":
            content = None
            payload = {"unexpected": True}
        else:
            if "intent" in system:
                content = '<think>음...</think>```json\n{"intent": "go_out", "hazards": ["폭염"]}\n```'
            elif "조회 단계" in system:
                content = '{"steps": ["alerts", "messages", "guide"], "reason": "x"}'
            else:  # write: 규칙 위반 답변 → 검증이 막아야 함
                content = "<think>생각</think>지금은 안전합니다. 걱정하지 마세요."
            payload = {"choices": [{"message": {"content": content}}]}
        out = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)


class HttpLLM(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}/v1"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        Handler.mode, Handler.seen = "ok", []

    def llm(self, **kw):
        return OpenAICompatLLM(self.base, "qwen3:9b", api_key=kw.pop("api_key", ""), timeout=5, **kw)

    def test_request_shape_and_think_stripped(self):
        out = asyncio.run(self.llm(no_think=True, api_key="k").complete("sys intent", "질문", json_mode=True))
        self.assertNotIn("<think>", out)
        path, body, auth = Handler.seen[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual((body["model"], body["temperature"]), ("qwen3:9b", 0))
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertTrue(body["messages"][1]["content"].endswith("/no_think"))
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(auth, "Bearer k")

    def test_http_error_and_bad_shape_raise_llmerror(self):
        Handler.mode = "500"
        with self.assertRaises(LLMError):
            asyncio.run(self.llm().complete("s", "u"))
        Handler.mode = "bad_shape"
        with self.assertRaises(LLMError):
            asyncio.run(self.llm().complete("s", "u"))

    def test_connection_refused_raises_llmerror(self):
        bad = OpenAICompatLLM("http://127.0.0.1:1/v1", "m", timeout=2)
        with self.assertRaises(LLMError):
            asyncio.run(bad.complete("s", "u"))

    def graph_run(self, llm):
        async def go():
            gw = MCPGateway(mcp_module.mcp)
            await gw.start()
            try:
                g = build_graph(Deps(mcp=gw, llm=llm, settings=Settings(max_rewrites=1)))
                return (await g.ainvoke({"query": "경기도 광주 폭염 나가도 돼?", "mode": "replay", "as_of": "2026-08-24 12:00"}))["final"]
            finally:
                await gw.stop()
        return asyncio.run(go())

    def test_graph_with_http_llm_rejects_unsafe_answer(self):
        final = self.graph_run(self.llm())
        self.assertEqual(final["intent"], "go_out")
        self.assertIn("폭염", final["hazards"])
        self.assertEqual(final["answer_source"], "template_fallback")  # 모델이 '안전합니다'를 말해도 사용자에게 나가지 않음
        self.assertNotIn("안전합니다", final["answer"])
        self.assertTrue(final["validation"]["ok"])
        self.assertEqual(final["judgements"][0]["level"], "warning")  # 판정은 모델과 무관

    def test_graph_when_llm_server_errors(self):
        Handler.mode = "500"
        final = self.graph_run(self.llm())
        self.assertEqual(final["answer_source"], "template_llm_error")
        self.assertEqual(final["judgements"][0]["level"], "warning")
        self.assertGreaterEqual(len(final["meta"]["llm_errors"]), 2)  # intent·write (plan 은 코드 규칙, ADR-018)


if __name__ == "__main__":
    unittest.main()
