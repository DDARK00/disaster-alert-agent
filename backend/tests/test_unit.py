"""순수 모듈 단위 테스트: rules / judge / validator / compose / retry / llm."""
import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "mcp_server"))

from agent import compose, judge, rules  # noqa: E402
from agent.llm import FakeLLM, extract_json, make_llm  # noqa: E402
from agent.retry import with_retry  # noqa: E402
from agent.validator import validate  # noqa: E402


class Rules(unittest.TestCase):
    def test_hazard_names_match_hazard_map(self):
        hm = json.loads((ROOT / "data/hazard_map.json").read_text(encoding="utf-8"))["hazards"]
        self.assertTrue(set(rules.HAZARD_SYNONYMS) <= set(hm), set(rules.HAZARD_SYNONYMS) - set(hm))

    def test_routes(self):
        self.assertEqual(rules.route("지하차도에 물이 차오르고 있어"), "emergency")
        self.assertEqual(rules.route("살려주세요 갇혔어요"), "emergency")
        self.assertEqual(rules.route("경기도 광주 강풍특보 났는데 나가도 돼?"), "disaster")
        self.assertEqual(rules.route("광주 도로상황"), "disaster")
        self.assertEqual(rules.route("오늘 점심 뭐 먹지"), "out_of_scope")
        self.assertEqual(rules.route(""), "out_of_scope")

    def test_emergency_beats_everything(self):
        self.assertEqual(rules.route("호우 때문에 집에 물이 차오르고 있어 도와줘"), "emergency")

    def test_hazards_and_intent(self):
        self.assertEqual(rules.parse_hazards("비가 많이 와서 바람도 세")["hazards"], ["강풍", "호우"])
        self.assertIn("폭염", rules.parse_hazards("폭염 때문에 부모님이 걱정돼")["hazards"])
        self.assertEqual(rules.detect_intent("지금 나가도 돼?"), "go_out")
        self.assertEqual(rules.detect_intent("행동요령 알려줘"), "guide")
        self.assertEqual(rules.detect_intent("광주 특보 있어?"), "status")


def alert_resp(alerts=(), pending=(), status="ok", released=()):
    a = lambda h, lv: {"zone": "L1", "zone_name": "광주시", "hazard": h, "warn_var": 1, "level": lv, "since": "2026-08-24 11:00",
                       "uncertain": False, "release_at": None, "announced_at": "x", "source_event": f"L1/{h}"}
    return {"region_id": "r", "region": "경기도 광주시", "mode": "replay", "as_of": "2026-08-24 12:00", "zones": ["L1"],
            "source": {"name": "기상특보", "status": status, "detail": "d", "window": None},
            "alerts": [a(h, lv) for h, lv in alerts], "pending": [a(h, lv) for h, lv in pending],
            "recently_released": released, "statement": "s"}


def msg_resp(n=1, status="ok"):
    return {"source": {"status": status, "detail": "d"},
            "messages": [{"sn": i, "created_at": "2026-08-24 10:00:00", "scope": "direct", "label": "폭염", "text": f"문자{i}"} for i in range(n)]}


class Judge(unittest.TestCase):
    def test_level_is_max_alert_level(self):
        j = judge.judge_region(alert_resp(alerts=[("폭염", "주의보"), ("호우", "경보")]), msg_resp(0), {})
        self.assertEqual((j["level"], j["hazards"]), ("warning", ["폭염", "호우"]))

    def test_messages_never_raise_level(self):
        j = judge.judge_region(alert_resp(), msg_resp(3), {})
        self.assertEqual(j["level"], "none")
        self.assertIsNotNone(j["note"])
        self.assertEqual(len([e for e in j["evidence"] if e["kind"] == "message"]), 3)

    def test_upcoming(self):
        j = judge.judge_region(alert_resp(pending=[("열대야", "주의보")]), None, {})
        self.assertEqual(j["level"], "upcoming")

    def test_weather_failure_is_unknown_not_none(self):
        j = judge.judge_region(alert_resp(status="failed"), msg_resp(0), {})
        self.assertEqual(j["level"], "unknown")
        self.assertTrue(any(g["source"] == "기상특보" for g in j["gaps"]))

    def test_message_failure_keeps_alert_level_and_records_gap(self):
        j = judge.judge_region(alert_resp(alerts=[("폭염", "경보")]), msg_resp(0, status="failed"), {})
        self.assertEqual(j["level"], "warning")
        self.assertTrue(any(g["source"] == "재난문자" for g in j["gaps"]))

    def test_missing_guide_recorded_as_gap(self):
        j = judge.judge_region(alert_resp(alerts=[("건조", "주의보")]), None,
                               {"건조": {"available": False, "reason": "없음", "rows": []}})
        self.assertTrue(any("행동요령(건조)" in g["source"] for g in j["gaps"]))

    def test_evidence_ids_unique(self):
        j = judge.judge_region(alert_resp(alerts=[("폭염", "경보")]), msg_resp(2),
                               {"폭염": {"available": True, "rows": [{"phase": "during", "text": "물을 마신다", "id": "g-1"}]}})
        ids = [e["id"] for e in j["evidence"]]
        self.assertEqual(len(ids), len(set(ids)))


class Validator(unittest.TestCase):
    def setUp(self):
        self.j = [judge.judge_region(alert_resp(alerts=[("폭염", "경보")]), msg_resp(1),
                                     {"폭염": {"available": True, "rows": [{"phase": "during", "text": "물을 마신다", "id": "g-1"}]}})]

    def test_template_always_passes(self):
        for intent in ("status", "go_out", "guide"):
            v = validate(compose.render_answer(self.j, intent), self.j)
            self.assertTrue(v["ok"], v["violations"])

    def test_template_passes_for_none_unknown_and_ambiguous(self):
        cases = [[judge.judge_region(alert_resp(), msg_resp(2), {})],
                 [judge.judge_region(alert_resp(status="no_coverage"), None, {})],
                 [judge.judge_region(alert_resp(), None, {}), judge.judge_region({**alert_resp(alerts=[("폭염", "주의보")]), "region": "광주광역시"}, None, {})]]
        for js in cases:
            v = validate(compose.render_answer(js, "status"), js)
            self.assertTrue(v["ok"], v["violations"])

    def test_forbidden_phrases_rejected(self):
        bad = compose.render_answer(self.j) + "\n지금은 안전합니다."
        self.assertFalse(validate(bad, self.j)["ok"])

    def test_uncited_bullet_rejected(self):
        bad = compose.render_answer(self.j) + "\n- 창문을 열어 두세요"
        v = validate(bad, self.j)
        self.assertFalse(v["ok"])
        self.assertTrue(any("근거 없는 항목" in x for x in v["violations"]))

    def test_unknown_citation_rejected(self):
        self.assertFalse(validate(compose.render_answer(self.j) + " [E99]", self.j)["ok"])

    def test_missing_disclaimer_rejected(self):
        text = compose.render_answer(self.j).replace(compose.DISCLAIMER, "")
        self.assertFalse(validate(text, self.j)["ok"])

    def test_unknown_requires_info_absent_wording(self):
        js = [judge.judge_region(alert_resp(status="failed"), None, {})]
        self.assertFalse(validate("### 경기도 광주시\n문제가 없습니다.\n" + compose.DISCLAIMER, js)["ok"])

    def test_none_requires_standard_phrase(self):
        js = [judge.judge_region(alert_resp(), None, {})]
        self.assertFalse(validate("### 경기도 광주시\n특보가 없습니다.\n" + compose.DISCLAIMER, js)["ok"])


class Infra(unittest.TestCase):
    def test_retry_recovers_transient_but_not_permanent(self):
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise asyncio.TimeoutError()
            return "ok"
        self.assertEqual(asyncio.run(with_retry(flaky, base_delay=0)), "ok")

        async def permanent():
            raise ValueError("x")
        with self.assertRaises(ValueError):
            asyncio.run(with_retry(permanent, base_delay=0))

    def test_extract_json_tolerates_noise(self):
        self.assertEqual(extract_json('<think>음</think>```json\n{"a": 1}\n```'), {"a": 1})
        with self.assertRaises(ValueError):
            extract_json("json 아님")

    def test_make_llm(self):
        self.assertIsInstance(make_llm({}), FakeLLM)
        self.assertEqual(make_llm({"LLM_BACKEND": "openai_compat", "LLM_MODEL": "qwen3:9b"}).name, "openai_compat:qwen3:9b")
        with self.assertRaises(ValueError):
            make_llm({"LLM_BACKEND": "x"})


if __name__ == "__main__":
    unittest.main()
