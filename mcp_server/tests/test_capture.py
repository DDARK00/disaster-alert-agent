"""capture 래퍼: 키 마스킹, API 레벨 오류 기록, 저장 실패 내성 (네트워크는 가짜로 대체)."""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp_server"))
import capture  # noqa: E402

KEY = "AbC%2Bdef%3D%3D"  # 이미 URL 인코딩된 키 (data.go.kr 스타일)


class FakeResp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(body: dict, echo_key: bool = False):
    def _open(req, timeout=0):
        b = dict(body)
        if echo_key:
            b["echo"] = req.full_url
        seen.append(req.full_url)
        return FakeResp(json.dumps(b).encode())
    seen = []
    _open.seen = seen
    return _open


class Capture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"WEATHER_ALERTS_SERVICE_KEY": KEY})
        self.env.start()
        self.p1 = mock.patch.object(capture, "FIXTURES", Path(self.tmp.name))
        self.p2 = mock.patch.object(capture, "COUNTER", Path(self.tmp.name) / "_c.json")
        self.p3 = mock.patch.object(capture, "ROOT", Path(self.tmp.name))  # .env 없음 → os.environ 사용
        for p in (self.p1, self.p2, self.p3):
            p.start()

    def tearDown(self):
        for p in (self.p1, self.p2, self.p3, self.env):
            p.stop()
        self.tmp.cleanup()

    def test_key_not_double_encoded_and_masked_everywhere(self):
        op = fake_urlopen({"response": {"header": {"resultCode": "00", "resultMsg": "OK"}}}, echo_key=True)
        with mock.patch.object(urllib.request, "urlopen", op), mock.patch.object(capture, "load_env", lambda: {"WEATHER_ALERTS_SERVICE_KEY": KEY}):
            rec = capture.call("weather", "getPwnCd", {"numOfRows": 1}, "t")
        self.assertIn("serviceKey=AbC%2Bdef%3D%3D", op.seen[0])  # 한 번만 인코딩
        dumped = json.dumps(rec, ensure_ascii=False)
        for variant in ("AbC%2Bdef%3D%3D", "AbC+def==", "AbC%252Bdef"):
            self.assertNotIn(variant, dumped)
        saved = Path(self.tmp.name, "weather").glob("t__*.json")
        self.assertNotIn("AbC", next(saved).read_text(encoding="utf-8"))
        self.assertTrue(rec["meta"]["api_ok"])

    def test_api_level_error_recorded_despite_http_200(self):
        op = fake_urlopen({"response": {"header": {"resultCode": "99", "resultMsg": "최대 조회 기간은 60일"}}})
        with mock.patch.object(urllib.request, "urlopen", op), mock.patch.object(capture, "load_env", lambda: {"WEATHER_ALERTS_SERVICE_KEY": KEY}):
            rec = capture.call("weather", "getPwnCd", {}, "t")
        self.assertEqual(rec["meta"]["http_status"], 200)
        self.assertFalse(rec["meta"]["api_ok"])
        self.assertEqual(rec["meta"]["api_result_code"], "99")
        self.assertIn("API 오류", capture.summarize(rec))

    def test_unwritable_fixture_dir_does_not_break_call(self):
        blocker = Path(self.tmp.name, "file")
        blocker.write_text("x")
        op = fake_urlopen({"response": {"header": {"resultCode": "00", "resultMsg": "OK"}}})
        with mock.patch.object(capture, "FIXTURES", blocker / "sub"), mock.patch.object(capture, "COUNTER", blocker / "c.json"), \
                mock.patch.object(urllib.request, "urlopen", op), mock.patch.object(capture, "load_env", lambda: {"WEATHER_ALERTS_SERVICE_KEY": KEY}):
            rec = capture.call("weather", "getPwnCd", {}, "t")
        self.assertIsNone(rec["meta"]["saved_to"])
        self.assertTrue(rec["meta"]["api_ok"])

    def test_network_exception_message_is_scrubbed(self):
        def boom(req, timeout=0):
            raise urllib.error.URLError(f"failed for {req.full_url}")
        with mock.patch.object(urllib.request, "urlopen", boom), mock.patch.object(capture, "load_env", lambda: {"WEATHER_ALERTS_SERVICE_KEY": KEY}):
            rec = capture.call("weather", "getPwnCd", {}, "t")
        self.assertIsNotNone(rec["meta"]["error"])
        self.assertNotIn("AbC", rec["meta"]["error"])


if __name__ == "__main__":
    unittest.main()
