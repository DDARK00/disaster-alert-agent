"""위험 수준 판정 (결정론). 합의된 규칙:
  - 발효 중 특보가 있으면 그 등급(주의보/경보/중대경보)을 그대로 쓴다.
  - 재난문자·행동요령은 '근거와 설명'으로만 쓰며 수준을 올리지 않는다.
  - 특보 조회가 실패/범위 밖이면 수준은 unknown (안전 아님).
수준: unknown < none < upcoming < advisory < warning < severe  (표시 순서일 뿐 'unknown'이 낮은 위험이라는 뜻이 아님)
"""
from __future__ import annotations

from config.catalog import RULES, msg

LEVEL_LABEL: dict[str, str] = RULES["levels"]["labels"]
STRESS_TO_LEVEL: dict[str, str] = RULES["levels"]["stress_to_level"]
RANK: dict[str, int] = RULES["levels"]["rank"]
MAX_MESSAGES, MAX_GUIDE_PER_HAZARD = RULES["evidence"]["max_messages"], RULES["evidence"]["max_guide_per_hazard"]
NICHE_WORDS = tuple(RULES["niche_words"])
FORBIDDEN = tuple(RULES["forbidden"])


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
        if g.get("available"):
            # 일반 시민 대상: 축산·농업·작업장 대상 문구는 뒤로 미룬다 (안정 정렬, 삭제하지 않음)
            g = {**g, "rows": sorted(g["rows"], key=lambda r: any(k in r["text"] for k in NICHE_WORDS))}
        if not g.get("available"):
            gaps.append({"source": f"행동요령({hz})", "status": "unavailable",
                         "detail": g.get("reason", "행동요령 없음")})
            continue
        # 공식 원문이라도 금지 표현("…지붕이 안전하니…")이 든 행은 근거로 쓰지 않는다 — 고쳐 쓰지 않고 뺀다 (T-024)
        rows = [r for r in g["rows"] if not any(w in r["text"] for w in FORBIDDEN)]
        for r in rows[:MAX_GUIDE_PER_HAZARD]:
            guide_rows.append({"id": f"G{len(guide_rows) + 1}", "kind": "guide", "hazard": hz, "phase": r["phase"],
                               "text": r["text"], "ref": r["id"]})
    ev.extend(guide_rows)

    note = None
    if weather_ok and level == "none" and msg_rows:
        note = msg("judge.messages_only_note")
    return {"region_id": region_id, "region": alerts["region"], "level": level, "level_label": LEVEL_LABEL[level],
            "hazards": hazards, "uncertain": uncertain, "as_of": alerts["as_of"], "mode": alerts["mode"],
            "zones": alerts["zones"], "evidence": ev, "gaps": gaps, "note": note, "statement": alerts["statement"]}
