"""위험 수준 판정 (결정론). 합의된 규칙:
  - 발효 중 특보가 있으면 그 등급(주의보/경보/중대경보)을 그대로 쓴다.
  - 재난문자·행동요령은 '근거와 설명'으로만 쓰며 수준을 올리지 않는다.
  - 특보 조회가 실패/범위 밖이면 수준은 unknown (안전 아님).
수준: unknown < none < upcoming < advisory < warning < severe  (표시 순서일 뿐 'unknown'이 낮은 위험이라는 뜻이 아님)
"""
from __future__ import annotations

LEVEL_LABEL = {
    "unknown": "정보 없음",
    "none": "확인된 위험 정보 없음",
    "upcoming": "특보 발효 예고",
    "advisory": "주의보 발효 중",
    "warning": "경보 발효 중",
    "severe": "중대경보 발효 중",
}
STRESS_TO_LEVEL = {"주의보": "advisory", "경보": "warning", "중대경보": "severe"}
RANK = {"none": 1, "upcoming": 2, "advisory": 3, "warning": 4, "severe": 5}
MAX_MESSAGES, MAX_GUIDE_PER_HAZARD = 3, 4


def judge_region(alerts: dict, messages: dict | None, guides: dict[str, dict]) -> dict:
    """alerts/messages: MCP 도구 응답. guides: hazard → search_action_guide 응답."""
    ev: list[dict] = []
    gaps: list[dict] = []
    region_id = alerts["region_id"]

    def gap(name: str, resp: dict | None) -> None:
        if resp is None:
            gaps.append({"source": name, "status": "not_queried", "detail": "조회하지 않음"})
        elif resp["source"]["status"] != "ok":
            gaps.append({"source": name, "status": resp["source"]["status"], "detail": resp["source"]["detail"]})

    weather_ok = alerts["source"]["status"] == "ok"
    gap("기상특보", alerts)
    if messages is not None:
        gap("재난문자", messages)

    level, hazards, uncertain = "unknown", [], False
    if weather_ok:
        level = "none"
        for a in alerts["alerts"]:
            lv = STRESS_TO_LEVEL.get(a["level"], "advisory")
            if RANK[lv] > RANK[level]:
                level = lv
            if a["hazard"] not in hazards:
                hazards.append(a["hazard"])
            uncertain = uncertain or a["uncertain"]
            ev.append({"id": f"E{len(ev) + 1}", "kind": "alert", "hazard": a["hazard"], "level": a["level"],
                       "text": f"{a['hazard']}{a['level']} 발효 중 ({a['zone_name']}, 발효 {a['since']})"
                               + (", 해제 예정 " + a["release_at"] if a.get("release_at") else ""),
                       "ref": a["source_event"], "uncertain": a["uncertain"]})
        if level == "none" and alerts["pending"]:
            level = "upcoming"
        for a in alerts["pending"]:
            if a["hazard"] not in hazards:
                hazards.append(a["hazard"])
            ev.append({"id": f"E{len(ev) + 1}", "kind": "pending", "hazard": a["hazard"], "level": a["level"],
                       "text": f"{a['hazard']}{a['level']} 발효 예고 ({a['zone_name']}, 발효 예정 {a['since']})",
                       "ref": a["source_event"], "uncertain": a["uncertain"]})
        for a in alerts["recently_released"]:
            ev.append({"id": f"E{len(ev) + 1}", "kind": "released", "hazard": a["hazard"], "level": a["level"],
                       "text": f"{a['hazard']}{a['level']} 해제 ({a['zone_name']}, {a['released_at']})",
                       "ref": a["source_event"], "uncertain": False})

    msg_rows = []
    if messages is not None and messages["source"]["status"] == "ok":
        direct = [m for m in messages["messages"] if m["scope"] == "direct"]
        wide = [m for m in messages["messages"] if m["scope"] == "sido_wide"]
        for m in (direct + wide)[:MAX_MESSAGES]:
            mid = f"M{len(msg_rows) + 1}"
            msg_rows.append({"id": mid, "kind": "message", "sn": m["sn"], "created_at": m["created_at"],
                             "scope": m["scope"], "label": m["label"], "text": m["text"], "ref": f"SN{m['sn']}"})
        ev.extend(msg_rows)

    guide_rows = []
    for hz, g in guides.items():
        if not g.get("available"):
            gaps.append({"source": f"행동요령({hz})", "status": "unavailable",
                         "detail": g.get("reason", "행동요령 없음")})
            continue
        for r in g["rows"][:MAX_GUIDE_PER_HAZARD]:
            guide_rows.append({"id": f"G{len(guide_rows) + 1}", "kind": "guide", "hazard": hz, "phase": r["phase"],
                               "text": r["text"], "ref": r["id"]})
    ev.extend(guide_rows)

    note = None
    if weather_ok and level == "none" and msg_rows:
        note = "기상특보는 확인되지 않았지만 최근 해당 지역에 안내 문자가 있습니다. 문자는 수준 판정에 쓰지 않고 참고로만 표시합니다."
    return {"region_id": region_id, "region": alerts["region"], "level": level, "level_label": LEVEL_LABEL[level],
            "hazards": hazards, "uncertain": uncertain, "as_of": alerts["as_of"], "mode": alerts["mode"],
            "zones": alerts["zones"], "evidence": ev, "gaps": gaps, "note": note, "statement": alerts["statement"]}
