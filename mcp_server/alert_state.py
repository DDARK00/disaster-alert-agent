"""기상특보 이벤트(getPwnCd) → 특정 시각의 '발효 상태'로 접기 (결정론, LLM 미사용).

API는 현재 상태가 아니라 발표/해제 이벤트 로그를 준다 (트러블슈팅 T-011).
- 이벤트를 (areaCode, warnVar)별로 (tmFc, tmSeq) 순으로 적용한다.
- 발효/해제 '효력 시각'은 startTime / endTime (없으면 0 → tmFc). 효력 시각이 as_of 이후면 pending.
- cancel == "1" (취소된 특보)인 이벤트는 무시한다.
- 시각은 KST 문자열 YYYYMMDDHHmm 이며 JSON에서는 int 로도 온다.

보수 원칙: 의미가 불확실한 코드는 '발효 중'으로 둔다(놓침이 오탐보다 위험). uncertain=True 로 표시.
"""
from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

HAZARD_MAP = Path(__file__).resolve().parent.parent / "data" / "hazard_map.json"

CMD_ANNOUNCE, CMD_RELEASE, CMD_EXTEND, CMD_CORRECT, CMD_CHANGE, CMD_CHANGE_RELEASE = (
    "1", "2", "3", "6", "7", "8")
STRESS_NAME = {0: "주의보", 1: "경보", 2: "중대경보"}


@lru_cache(maxsize=1)
def _hazards_by_var() -> dict[int, str]:
    hm = json.loads(HAZARD_MAP.read_text(encoding="utf-8"))["hazards"]
    return {v["warnVar"]: name for name, v in hm.items()}


def parse_kst(v) -> datetime | None:
    """202610181630 (int|str) → datetime. 0/빈 값/None → None."""
    s = str(v).strip() if v is not None else ""
    if not s or s == "0":
        return None
    s = s.ljust(12, "0")[:12]
    return datetime.strptime(s, "%Y%m%d%H%M")


def normalize_items(items) -> list[dict]:
    """JSON/XML 변환 과정에서 단건이 dict 로 오는 경우, 타입 혼재를 정리한다."""
    if items is None:
        return []
    if isinstance(items, dict):
        items = [items]
    out = []
    for i in items:
        out.append({
            "areaCode": str(i["areaCode"]),
            "areaName": i.get("areaName", ""),
            "warnVar": int(i["warnVar"]),
            "warnStress": int(i.get("warnStress") or 0),
            "command": str(i["command"]),
            "cancel": str(i.get("cancel", "0")),
            "tmFc": parse_kst(i.get("tmFc")),
            "tmSeq": int(i.get("tmSeq") or 0),
            "startTime": parse_kst(i.get("startTime")),
            "endTime": parse_kst(i.get("endTime")),
        })
    return out


def fold_alerts(items, as_of: datetime, area_codes: set[str] | None = None) -> list[dict]:
    """as_of 시점의 특보 상태 목록. status: active | pending_start | released.

    반환은 (구역, 종류)별 1건이며 released 도 포함한다 (호출자가 필터).
    """
    events = [e for e in normalize_items(items)
              if e["cancel"] != "1" and e["tmFc"] and e["tmFc"] <= as_of
              and (area_codes is None or e["areaCode"] in area_codes)]
    events.sort(key=lambda e: (e["tmFc"], e["tmSeq"]))
    hazards = _hazards_by_var()

    state: dict[tuple[str, int], dict] = {}
    for e in events:
        key = (e["areaCode"], e["warnVar"])
        cur = state.get(key)
        cmd = e["command"]
        if cmd == CMD_RELEASE:
            eff = e["endTime"] or e["tmFc"]
            if eff <= as_of:
                state[key] = {**(cur or _base(e, hazards)), "status": "released",
                              "release_at": None, "released_at": eff, "last_event": _ev(e)}
            elif cur and cur["status"] != "released":
                cur["release_at"] = eff  # 해제 예고: 아직 발효 중
                cur["last_event"] = _ev(e)
            continue

        eff = e["startTime"] or e["tmFc"]
        uncertain = cmd in (CMD_CORRECT, CMD_CHANGE_RELEASE)
        if cmd == CMD_EXTEND and cur and cur["status"] != "released":
            stress = cur["stress"]  # 연장은 강도 유지
        else:
            stress = e["warnStress"]
        new = {**_base(e, hazards), "stress": stress, "stress_name": STRESS_NAME.get(stress, str(stress)),
               "status": "active" if eff <= as_of else "pending_start",
               "since": eff if (cur is None or cur["status"] == "released") else cur.get("since", eff),
               "release_at": None, "uncertain": uncertain or (cur or {}).get("uncertain", False) and cmd == CMD_EXTEND,
               "last_event": _ev(e)}
        if eff > as_of and cur and cur["status"] == "active":
            # 효력 전 변경 예고: 현재는 기존 상태 유지, 예고만 기록
            cur["pending_change"] = {"stress": stress, "at": eff}
            cur["last_event"] = _ev(e)
            continue
        state[key] = new
    return sorted(state.values(), key=lambda s: (s["area_code"], s["warn_var"]))


def _base(e: dict, hazards: dict[int, str]) -> dict:
    return {"area_code": e["areaCode"], "area_name": e["areaName"], "warn_var": e["warnVar"],
            "hazard": hazards.get(e["warnVar"], f"코드{e['warnVar']}"),
            "stress": e["warnStress"], "stress_name": STRESS_NAME.get(e["warnStress"], str(e["warnStress"])),
            "since": None, "uncertain": False}


def _ev(e: dict) -> dict:
    return {"tmFc": e["tmFc"], "tmSeq": e["tmSeq"], "command": e["command"]}


def in_effect(states: list[dict]) -> list[dict]:
    """발효 중(active)만. pending_start 는 '예고'로 따로 다룬다."""
    return [s for s in states if s["status"] == "active"]
