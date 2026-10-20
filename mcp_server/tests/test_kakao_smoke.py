"""선택 스모크: 실제 Kakao 호출. KAKAO_SMOKE=1 이고 키가 있을 때만 돈다 (기본 skip — 쿼터·비결정성).
결과는 저장·출력하지 않고 형태만 확인한다.   KAKAO_SMOKE=1 .venv/bin/python -m unittest mcp_server/tests/test_kakao_smoke.py
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp_server"))
import kakao  # noqa: E402
from capture import load_env  # noqa: E402
from regions import region_for_admin  # noqa: E402

ENABLED = os.environ.get("KAKAO_SMOKE") == "1" and bool(load_env().get(kakao.KEY_ENV))


@unittest.skipUnless(ENABLED, "KAKAO_SMOKE=1 과 KAKAO_REST_API_KEY 필요")
class Smoke(unittest.TestCase):
    def test_known_places_map_to_supported_regions(self):
        for q, rid in [("곤지암", "gyeonggi_gwangju"), ("광주 상무지구", "gwangju_metro")]:
            r = kakao.lookup(q)
            self.assertEqual(r["status"], "ok", q)
            self.assertTrue(r["place_id"].isdigit())
            self.assertEqual(region_for_admin(r["region_names"]), rid, q)

    def test_unknown_place(self):
        self.assertEqual(kakao.lookup("zzzz없는장소qq")["status"], "not_found")
