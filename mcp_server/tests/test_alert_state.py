"""fold_alerts 단위 테스트 — 실제 fixture(2026-10-17~19 가평군, 2026-08 광주시/광주서부)와 합성 케이스."""
import glob
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp_server"))
from alert_state import fold_alerts, in_effect  # noqa: E402


def load(pattern):
    f = sorted(glob.glob(str(ROOT / "data/fixtures/weather" / pattern)))[-1]
    with open(f, encoding="utf-8") as fp:
        body = json.load(fp)["json"]["response"]["body"]
    return (body.get("items") or {}).get("item") or []


def t(s):
    return datetime.strptime(s, "%Y%m%d%H%M")


def by_hazard(states):
    return {s["hazard"]: s for s in states}


class RealFixtures(unittest.TestCase):
    def test_gapyeong_oct18(self):
        items = load("getPwnCd_20261017_19__*.json")
        z = {"L1011400"}
        self.assertEqual(by_hazard(in_effect(fold_alerts(items, t("202610181200"), z))), {})  # 아직 발표 전
        h = by_hazard(in_effect(fold_alerts(items, t("202610181400"), z)))
        self.assertEqual(set(h), {"강풍"})  # 13:00 강풍주의보
        h = by_hazard(in_effect(fold_alerts(items, t("202610181700"), z)))
        self.assertEqual(set(h), {"강풍", "호우"})  # 16:30 호우주의보 추가
        self.assertEqual(in_effect(fold_alerts(items, t("202610181830"), z)), [])  # 18:00 둘 다 해제

    def test_gyeonggi_gwangju_heat_august(self):
        items = load("getPwnCd_L1013400_60d__*.json")
        z = {"L1013400"}
        h = by_hazard(in_effect(fold_alerts(items, t("202608231030"), z)))
        self.assertEqual(h, {}, "08-23 10:00 발표 / 11:00 발효 → 10:30 은 아직 발효 전")
        st = fold_alerts(items, t("202608231030"), z)
        self.assertEqual(st[0]["status"], "pending_start")
        h = by_hazard(in_effect(fold_alerts(items, t("202608241200"), z)))
        self.assertEqual(h["폭염"]["stress_name"], "경보")  # 변경발표로 경보 상향
        self.assertEqual(h["폭염"]["since"], t("202608241100"))  # 경보로 바뀐 시각 (최초 주의보 발효 08-23 11:00 아님)
        h = by_hazard(in_effect(fold_alerts(items, t("202608261200"), z)))
        self.assertEqual(h["폭염"]["stress_name"], "주의보")  # 08-25 16:00 변경발표(주의보로 하향)
        self.assertIn("열대야", h)
        h = by_hazard(in_effect(fold_alerts(items, t("202608271700"), z)))
        self.assertEqual(set(h), {"폭염"})  # 열대야 해제
        self.assertEqual(in_effect(fold_alerts(items, t("202608281700"), z)), [])  # 폭염 해제

    def test_gwangju_seobu_rain_aug27(self):
        items = load("getPwnCd_L1130110_60d__*.json")
        z = {"L1130110"}
        h = by_hazard(in_effect(fold_alerts(items, t("202608271700"), z)))
        self.assertIn("호우", h)
        h = by_hazard(in_effect(fold_alerts(items, t("202608271900"), z)))
        self.assertNotIn("호우", h)

    def test_release_first_in_window_means_active_before(self):  # T-026 (평가 세트가 찾은 놓침)
        items = load("getPwnCd_L1130110_60d__*.json")
        z = {"L1130110"}
        h = by_hazard(in_effect(fold_alerts(items, t("202608221200"), z)))
        self.assertIn("열대야", h)  # 창 안 첫 이벤트가 08-23 16:00 해제 → 08-22 에는 발효 중
        self.assertTrue(h["열대야"]["inferred"])
        self.assertIsNone(h["열대야"]["since"])
        self.assertNotIn("열대야", by_hazard(in_effect(fold_alerts(items, t("202608231700"), z))))  # 해제 후

    def test_release_first_synthetic(self):
        rel = {"areaCode": "L1", "warnVar": 13, "warnStress": 0, "command": "2", "cancel": "0", "tmFc": 202608231600,
               "tmSeq": 1, "startTime": 0, "endTime": 202608231600}
        self.assertEqual(in_effect(fold_alerts([rel], t("202608231700"))), [])  # 해제 이후
        self.assertEqual(len(in_effect(fold_alerts([rel], t("202608221200")))), 1)
        ann = {**rel, "command": "1", "tmFc": 202608231000, "startTime": 202608231100, "endTime": 0}
        self.assertEqual(in_effect(fold_alerts([ann, rel], t("202608221200"))), [])  # 첫 이벤트가 발표면 그 전엔 없음

    def test_no_events_means_empty_not_safe(self):
        # 빈 결과는 '위험 없음 확정'이 아니다. 판정 계층에서 '확인된 위험 정보 없음'으로 표현한다.
        self.assertEqual(fold_alerts([], t("202610201200")), [])


class Synthetic(unittest.TestCase):
    def ev(self, **kw):
        base = dict(areaCode="Z1", areaName="테스트", warnVar=1, warnStress=0, command="1", cancel="0",
                    tmFc=202610200900, tmSeq=1, startTime=202610200900, endTime=0)
        base.update(kw)
        return base

    def test_cancelled_event_ignored(self):
        items = [self.ev(cancel="1")]
        self.assertEqual(fold_alerts(items, t("202610201000")), [])

    def test_future_announcement_is_pending(self):
        items = [self.ev(tmFc=202610200900, startTime=202610201100)]
        s = fold_alerts(items, t("202610201000"))[0]
        self.assertEqual(s["status"], "pending_start")

    def test_release_announced_but_not_yet_effective(self):
        items = [self.ev(), self.ev(command="2", tmFc=202610201000, tmSeq=2, startTime=0, endTime=202610201200)]
        s = fold_alerts(items, t("202610201100"))[0]
        self.assertEqual(s["status"], "active")  # 해제 효력 전: 아직 발효 중
        self.assertIsNotNone(s["release_at"])
        s = fold_alerts(items, t("202610201300"))[0]
        self.assertEqual(s["status"], "released")

    def test_extend_keeps_stress(self):
        items = [self.ev(warnStress=1), self.ev(command="3", warnStress=0, tmFc=202610201000, tmSeq=2, startTime=0)]
        s = fold_alerts(items, t("202610201100"))[0]
        self.assertEqual((s["status"], s["stress"]), ("active", 1))

    def test_change_release_is_conservative(self):
        items = [self.ev(warnStress=1), self.ev(command="8", warnStress=0, tmFc=202610201000, tmSeq=2, startTime=0, endTime=0)]
        s = fold_alerts(items, t("202610201100"))[0]
        self.assertEqual(s["status"], "active")
        self.assertTrue(s["uncertain"])

    def test_order_by_tmfc_then_seq(self):
        items = [self.ev(command="2", tmFc=202610201000, tmSeq=5, startTime=0, endTime=202610201000), self.ev()]
        s = fold_alerts(items, t("202610201100"))[0]
        self.assertEqual(s["status"], "released")

    def test_single_dict_item_and_mixed_types(self):
        item = self.ev(tmFc="202610200900", startTime="202610200900", warnVar="1", warnStress="0")
        self.assertEqual(fold_alerts(item, t("202610201000"))[0]["status"], "active")


if __name__ == "__main__":
    unittest.main()
