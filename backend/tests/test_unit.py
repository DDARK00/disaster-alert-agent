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
from config.catalog import msg  # noqa: E402


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

    def test_guide_rows_prefer_general_public(self):
        rows = [{"phase": "during", "text": "가축 폐사 시 신고합니다", "id": "a"},
                {"phase": "during", "text": "충분한 물을 마십니다", "id": "b"}]
        j = judge.judge_region(alert_resp(alerts=[("폭염", "경보")]), None, {"폭염": {"available": True, "rows": rows}})
        g = [e for e in j["evidence"] if e["kind"] == "guide"]
        self.assertEqual([e["ref"] for e in g], ["b", "a"])  # 순서만 바뀌고 삭제되지 않음

    def test_evidence_ids_unique(self):
        j = judge.judge_region(alert_resp(alerts=[("폭염", "경보")]), msg_resp(2),
                               {"폭염": {"available": True, "rows": [{"phase": "during", "text": "물을 마신다", "id": "g-1"}]}})
        ids = [e["id"] for e in j["evidence"]]
        self.assertEqual(len(ids), len(set(ids)))


class GuideQuality(unittest.TestCase):
    """K2 유사 행 중복 제거 · K3 금지 문장 판정 · K4 필수 행동 · 행동요령 0줄 표준 문구 (ADR-020)."""
    HEAT = [  # 실제 폭염(01009) 행 (2026-10-20 fixture 원문)
        {"id": "g1", "phase": "during", "text": "냉방이 되지 않는 실내에서는 햇볕을 가리고 맞바람이 불도록 환기를 합니다."},
        {"id": "g2", "phase": "during", "text": "냉방이 되지 않는 실내에서는 햇볕이 실내에 들어오지 않도록 하고, 환기가 잘 되도록 선풍기를 켜고 창문이나 출입문을 열어둡니다."},
        {"id": "g3", "phase": "prepare", "text": "무더위에는 카페인이 들어간 음료나 주류는 삼가고, 생수나 이온음료를 마시는 것이 좋습니다."},
        {"id": "g4", "phase": "during", "text": "물을 많이 마시고, 카페인이 들어간 음료나 주류는 마시지 않습니다."},
        {"id": "g5", "phase": "during", "text": "창문이 닫힌 자동차 안에는 노약자나 어린이를 홀로 남겨두지 않습니다."},
        {"id": "g6", "phase": "info", "text": "열사병 증상 : 고온 다습한 환경에서 몸의 열이 발산하지 못하여 생기는 병이다."},
        {"id": "g7", "phase": "info", "text": "열사병 대처요령 : 즉시 119에 신고하고, 환자를 그늘로 옮겨 겉옷을 벗기고, 미지근한 물로 몸을 적셔 체온이 내려가도록 한 후 의사·의료기관 등의 지시에 따른다"},
        {"id": "g8", "phase": "info", "text": "열경련 대처요령 : 환자는 그늘에서 쉬게 하고 소금을 물에 녹여 섭취하게 해주어야 한다. 의사의 진료에 따라 조치한다."},
        {"id": "g9", "phase": "info", "text": "화상 대처요령 : 그늘로 환자를 이동시켜 햇빛에 노출되지 않게 한다."},
    ]

    def pick(self, active=True, hz="폭염", rows=None):
        j = judge.judge_region(alert_resp(alerts=[(hz, "경보")] if active else []), None,
                               {hz: {"available": True, "rows": rows or self.HEAT}})
        return j, {e["ref"]: e["group"] for e in j["evidence"] if e["kind"] == "guide"}

    def test_mixed_sentence_is_not_dont(self):
        self.assertFalse(judge.is_prohibition(self.HEAT[3]["text"]))   # 권고 + 금지 (G7 사례) → 할 일 쪽
        self.assertFalse(judge.is_prohibition(self.HEAT[2]["text"]))   # 금지 + 권고
        self.assertTrue(judge.is_prohibition(self.HEAT[4]["text"]))    # 순수 금지
        self.assertFalse(judge.is_prohibition(self.HEAT[0]["text"]))   # 순수 권고
        self.assertFalse(judge.is_prohibition("당황하지 않고 침착하게 대피합니다."))
        self.assertTrue(judge.is_prohibition("좌우로 왔다 갔다 하거나 손을 놓고 운전하지 말며, 과속을 하지 맙시다."))

    def test_near_duplicates_keep_first_only(self):
        self.assertTrue(judge.similar(self.HEAT[0]["text"], self.HEAT[1]["text"]))
        self.assertTrue(judge.similar(self.HEAT[2]["text"], self.HEAT[3]["text"]))
        self.assertFalse(judge.similar(self.HEAT[0]["text"], self.HEAT[4]["text"]))
        _, got = self.pick()
        self.assertIn("g1", got)
        self.assertNotIn("g2", got)
        self.assertEqual(sum(r in got for r in ("g3", "g4")), 1)
        texts = [e["text"] for e in self.pick()[0]["evidence"] if e["kind"] == "guide"]
        self.assertFalse([1 for i, a in enumerate(texts) for b in texts[i + 1:] if judge.similar(a, b)])

    def test_symptom_actions_when_active_only(self):
        _, got = self.pick()
        self.assertEqual([r for r, g in got.items() if g == "symptom"], ["g7", "g8"])  # 우선순위 순, 상한 2, 증상 설명 행 제외
        self.assertEqual(got["g5"], "dont")
        _, got = self.pick(active=False)
        self.assertNotIn("symptom", got.values())

    def test_symptom_rows_must_be_cited(self):
        j, _ = self.pick()
        ok = compose.render_answer([j])
        self.assertTrue(validate(ok, [j])["ok"])
        sid = next(e["id"] for e in j["evidence"] if e.get("group") == "symptom")
        bad = "\n".join(l for l in ok.splitlines() if f"[{sid}]" not in l)
        self.assertIn(f"필수 행동요령 [{sid}] 인용 없음", " ".join(validate(bad, [j])["violations"]))

    def test_group_headings_required_and_rule_lines_tidied(self):
        j, _ = self.pick()
        ok = compose.render_answer([j])
        no_head = "\n".join(l for l in ok.splitlines() if not l.startswith("▶"))
        self.assertTrue(any("묶음 제목 없음" in x for x in validate(no_head, [j])["violations"]))
        tidied = compose.tidy_llm(ok.replace("\n▶", "\n---\n▶"))
        self.assertEqual(tidied, ok)
        self.assertTrue(validate(tidied, [j])["ok"])

    def test_total_cap_keeps_must_actions_and_every_group(self):
        from config.catalog import RULES
        extra = [{"id": f"p{i}", "phase": "prepare", "text": t} for i, t in enumerate(
            ["단수에 대비하여 생수를 준비합니다.", "에어컨, 선풍기 등을 사용할 수 있도록 사전에 정비합니다.",
             "여름철에는 음식이 쉽게 상할 수 있으므로 외부에 오랫동안 방치된 것은 먹지 않습니다."])]
        j, got = self.pick(rows=self.HEAT + extra)
        self.assertLessEqual(len(got), RULES["guide_groups"]["max_total"])
        self.assertEqual({"now", "symptom", "prepare", "dont"}, set(got.values()))
        self.assertEqual([r for r, g in got.items() if g == "symptom"], ["g7", "g8"])
        self.assertEqual([e["id"] for e in j["evidence"] if e["kind"] == "guide"], [f"G{i}" for i in range(1, len(got) + 1)])

    def test_zero_selected_rows_say_not_in_data(self):
        rows = [{"id": "x", "phase": "after", "text": "피해를 입은 경우 신고합니다."}]
        j, got = self.pick(active=False, hz="강풍", rows=rows)
        self.assertFalse(got)
        self.assertTrue(any(g["source"] == "행동요령(강풍)" and g["status"] == "unavailable" for g in j["gaps"]))
        ans = compose.render_answer([j])
        self.assertIn(msg("markers.not_in_data"), ans)
        self.assertTrue(validate(ans, [j])["ok"])


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

    def test_cross_region_citation_and_partial_alerts_rejected(self):
        g = judge.judge_region(alert_resp(alerts=[("폭염", "경보"), ("열대야", "주의보")]), None, {})
        m = judge.judge_region({**alert_resp(alerts=[("호우", "주의보")]), "region": "광주광역시"}, None, {})
        js = [g, m]
        self.assertTrue(validate(compose.render_answer(js), js)["ok"])
        # 경기 광주 섹션에서 E2 를 빼고, 광주광역시 섹션에 없는 E2 를 인용
        bad = ("### 경기도 광주시\n- 폭염경보 [E1]\n\n### 광주광역시\n- 호우주의보 [E1] [E2]\n\n" + compose.DISCLAIMER)
        v = validate(bad, js)["violations"]
        self.assertIn("경기도 광주시: 특보 근거 [E2] 인용 없음", v)
        self.assertIn("광주광역시: 이 지역 근거가 아닌 인용 [E2]", v)
        self.assertIn("지역 섹션 밖 인용: [E1]", validate("- [E1]\n" + bad, js)["violations"])

    def test_shelter_citation_needs_existing_id_and_notice(self):
        sh = {"폭염": {"status": "ok", "kind_label": "무더위쉼터", "source": "행정안전부 통합대피소", "source_label": "통합대피소",
                     "shelters": [{"id": "T1", "name": "가경로당", "address": "경기도 광주시 경안로 1 (경안동)", "kind_name": "x"}]}}
        js = [judge.judge_region(alert_resp(alerts=[("폭염", "경보")]), None, {}, sh)]
        e = [x for x in js[0]["evidence"] if x["kind"] == "shelter"][0]
        self.assertEqual(e["id"], "S1")
        self.assertEqual(e["map_url"], "https://map.kakao.com/link/search/"
                         "%EA%B2%BD%EA%B8%B0%EB%8F%84%20%EA%B4%91%EC%A3%BC%EC%8B%9C%20%EA%B2%BD%EC%95%88%EB%A1%9C%201")
        ok = compose.render_answer(js)
        self.assertTrue(validate(ok, js)["ok"], validate(ok, js))
        self.assertIn("존재하지 않는 근거 인용: [S2]", validate(ok.replace("[S1]", "[S2]"), js)["violations"])
        no_notice = "\n".join(l for l in ok.splitlines() if "운영 여부" not in l)
        self.assertIn("쉼터 인용인데 운영 여부·시간 미확인 안내 없음", validate(no_notice, js)["violations"])
        self.assertFalse(validate(ok + "\n가까운 쉼터로 가세요.", js)["ok"])

    def test_shelter_failure_is_gap_but_no_range_is_silent(self):
        js = judge.judge_region(alert_resp(), None, {}, {"폭염": {"status": "failed", "cause": "api_timeout", "reason": "x"},
                                                        "한파": {"status": "no_range", "reason": "x"}})
        self.assertEqual([(g["source"], g["cause"]) for g in js["gaps"]], [("쉼터 목록(폭염)", "api_timeout")])
        self.assertTrue(validate(compose.render_answer([js]), [js])["ok"])

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


class Catalog(unittest.TestCase):
    """config/, prompts/ 로 옮긴 데이터가 안전 기준을 약하게 만들지 않는지 고정한다 (ADR-012)."""

    def test_validator_markers_and_forbidden_are_not_weakened(self):
        from agent import validator
        self.assertEqual(validator.MK, {"no_info": "정보 없음", "no_risk": "확인된 위험 정보 없음", "no_guarantee": "공식 채널도 함께 확인",
                                        "official": "공식 경보", "emergency_call": "119",
                                        "not_in_data": "해당 항목이 없습니다",
                                        "shelter_notice": "운영 여부·운영 시간은 이 목록에 없어",
                                        "guide_borrowed": "전용 항목이 없어"})
        for w in ["안전합니다", "안전해요", "걱정하지 마", "문제없", "괜찮습니다", "위험 없", "안심하", "가까운 쉼터", "운영 중인 쉼터"]:
            self.assertIn(w, validator.FORBIDDEN)

    def test_guide_rows_with_forbidden_words_are_excluded(self):
        # 실제 폭풍해일 행동요령 1행: "…지붕이 안전하니 높은 곳으로…" → 근거에서 빠지고 템플릿이 검증을 통과해야 한다
        rows = [{"phase": "during", "text": "경우에 따라서는 지붕이 안전하니 높은 곳으로 이동합시다.", "id": "g-1"},
                {"phase": "during", "text": "높은 곳으로 대피합니다.", "id": "g-2"}]
        js = [judge.judge_region(alert_resp(alerts=[("폭풍해일", "경보")]), None, {"폭풍해일": {"available": True, "rows": rows}})]
        self.assertEqual([e["ref"] for e in js[0]["evidence"] if e["kind"] == "guide"], ["g-2"])
        self.assertTrue(validate(compose.render_answer(js), js)["ok"])

    def test_guide_rows_with_urls_are_excluded(self):
        # 실제 미세먼지 행동요령 "에어코리아(…, www.airkorea.or.kr)" → 본문 링크 금지라 근거에서 뺀다 (T-030)
        rows = [{"phase": "prepare", "text": "에어코리아(www.airkorea.or.kr)", "id": "g-1"},
                {"phase": "prepare", "text": "기상청, 지자체 홈페이지 등", "id": "g-2"}]
        js = [judge.judge_region(alert_resp(), None, {"미세먼지": {"available": True, "rows": rows}})]
        self.assertEqual([e["ref"] for e in js[0]["evidence"] if e["kind"] == "guide"], ["g-2"])
        self.assertTrue(validate(compose.render_answer(js), js)["ok"])

    def test_guide_groups_now_prepare_dont(self):
        rows = [{"phase": "during", "text": "실내에서 휴식합니다.", "id": "d1"},
                {"phase": "during", "text": "외출을 하지 맙시다.", "id": "d2"},
                {"phase": "prepare", "text": "생수를 준비합니다.", "id": "p1"},
                {"phase": "info", "text": "폭염이란 무엇인가", "id": "i1"}]
        js = [judge.judge_region(alert_resp(alerts=[("폭염", "경보")]), None, {"폭염": {"available": True, "rows": rows}})]
        got = [(e["group"], e["ref"]) for e in js[0]["evidence"] if e["kind"] == "guide"]
        self.assertEqual(got, [("now", "d1"), ("prepare", "p1"), ("dont", "d2")])  # info 제외, 금지 키워드가 단계보다 먼저
        text = compose.render_answer(js)
        for label in ("지금 할 일", "미리 대비할 일", "하지 말 것"):
            self.assertIn(label, text)
        self.assertTrue(validate(text, js)["ok"])
        # 특보가 없는 유형: 미리 대비할 일이 먼저, '지금 할 일' 대신 '발생하면 할 일'
        js = [judge.judge_region(alert_resp(), None, {"폭염": {"available": True, "rows": rows}})]
        self.assertEqual([e["group_label"] for e in js[0]["evidence"] if e["kind"] == "guide"],
                         ["미리 대비할 일", "발생하면 할 일", "하지 말 것"])

    def test_zone_codes_not_in_answer(self):
        a = alert_resp()
        a["zone_names"] = ["광주시"]
        js = [judge.judge_region(a, None, {})]
        text = compose.render_answer(js)
        self.assertIn("조회 구역 광주시", text)
        self.assertNotIn("L1", text)

    def test_not_in_official_data_route(self):
        for q in ["차가 물에 잠기면 어떻게 탈출해?", "지하차도 차량 침수 시 탈출 방법 알려줘"]:
            self.assertEqual(rules.route(q), "not_in_data", q)
        self.assertEqual(rules.route("지하차도에 물이 차오르고 있어"), "emergency")  # 위급이 먼저
        self.assertEqual(rules.route("경기도 광주 지하차도 침수 위험 있어?"), "disaster")  # 탈출 요령 질문이 아님

    def test_unavailable_guide_must_be_stated(self):
        js = [judge.judge_region(alert_resp(), None, {"열대야": {"available": False, "reason": "없음", "rows": []}})]
        text = compose.render_answer(js)
        self.assertIn("해당 항목이 없습니다", text)
        self.assertTrue(validate(text, js)["ok"])
        self.assertFalse(validate(text.replace("해당 항목이 없습니다", ""), js)["ok"])

    def test_emergency_words_compile_with_flexible_spaces(self):
        for t in ["물이차오르고", "물  차올라", "불이 났어요", "의식이없어요", "대피하고  있어요", "지금당장"]:
            self.assertTrue(rules.is_emergency(t), t)
        self.assertFalse(rules.is_emergency("물 마시기"))

    def test_missing_marker_or_forbidden_wording_fails_at_startup(self):
        from config import catalog
        orig = catalog.MESSAGES["answer"]["unknown"]
        try:
            catalog.MESSAGES["answer"]["unknown"] = "조회하지 못했습니다."
            with self.assertRaises(RuntimeError):
                catalog._check_consistency()
            catalog.MESSAGES["answer"]["unknown"] = orig + " 지금은 안전합니다."
            with self.assertRaises(RuntimeError):
                catalog._check_consistency()
        finally:
            catalog.MESSAGES["answer"]["unknown"] = orig
        catalog._check_consistency()

    def test_missing_prompt_section_fails(self):
        from config import catalog
        with self.assertRaises(RuntimeError):
            catalog._load_prompt("write", ["user", "없는_구역"])


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
