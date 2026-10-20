"""위험 수준 판정 (결정론). 합의된 규칙:
  - 발효 중 특보가 있으면 그 등급(주의보/경보/중대경보)을 그대로 쓴다.
  - 재난문자·행동요령은 '근거와 설명'으로만 쓰며 수준을 올리지 않는다.
  - 특보 조회가 실패/범위 밖이면 수준은 unknown (안전 아님).
수준: unknown < none < upcoming < advisory < warning < severe  (표시 순서일 뿐 'unknown'이 낮은 위험이라는 뜻이 아님)
"""
from __future__ import annotations

from config.catalog import RULES, msg, reason

from .validator import URL, shelter_link  # 본문 링크 금지와 같은 기준

LEVEL_LABEL: dict[str, str] = RULES["levels"]["labels"]
STRESS_TO_LEVEL: dict[str, str] = RULES["levels"]["stress_to_level"]
RANK: dict[str, int] = RULES["levels"]["rank"]
MAX_MESSAGES = RULES["evidence"]["max_messages"]
GROUPS = RULES["guide_groups"]
NICHE_WORDS = tuple(RULES["niche_words"])
FORBIDDEN = tuple(RULES["forbidden"])


def guide_group(r: dict) -> str | None:
    """행 하나 → 묶음 (now | prepare | dont | None). 금지 표현 키워드가 단계보다 먼저다."""
    if r["phase"] in ("info",):
        return None
    if any(w in r["text"] for w in GROUPS["dont_words"]):
        return "dont"
    return GROUPS["phase_to_group"].get(r["phase"])


def group_label(group: str, active: bool) -> str:
    return msg(f"guide_groups.{'now_inactive' if group == 'now' and not active else group}")


def group_guides(rows: list[dict], active: bool) -> list[tuple[str, dict]]:
    """'그래서 어떻게': 지금 할 일 / 미리 대비할 일 / 하지 말 것 (ADR-016). 묶음별 상한, 원문 순서 유지.
    LLM 은 여기서 고른 항목만 풀어 쓴다."""
    order = GROUPS["order_active"] if active else GROUPS["order_inactive"]
    out = []
    for grp in order:
        picked = [r for r in rows if guide_group(r) == grp][:GROUPS["limits"][grp]]
        out += [(grp, r) for r in picked]
    return out


def judge_region(alerts: dict, messages: dict | None, guides: dict[str, dict],
                 shelters: dict[str, dict] | None = None) -> dict:
    """alerts/messages: MCP 도구 응답. guides: hazard → search_action_guide 응답. shelters: hazard → get_shelters 응답.
    쉼터는 근거([S#])로만 쓰고 수준을 바꾸지 않는다. 조회 실패만 '정보 없음' gap — 범위 데이터 없음 등은 조용히 뺀다."""
    ev: list[dict] = []
    gaps: list[dict] = []
    region_id = alerts["region_id"]

    def gap(name: str, resp: dict | None) -> None:
        if resp is None:
            gaps.append({"source": name, "status": "not_queried", "detail": "조회하지 않음"})
        elif resp["source"]["status"] != "ok":
            st, cause = resp["source"]["status"], resp["source"].get("cause")
            gaps.append({"source": name, "status": st, "cause": cause, "reason": reason(st, cause),
                         "detail": resp["source"]["detail"]})

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
                       "text": f"{a['hazard']}{a['level']} 발효 중 ({a['zone_name']}, "
                               + (f"발효 {a['since']}" if a.get("since") else msg("judge.since_before_window"))
                               + ")"
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
    active_hazards = {a["hazard"] for a in alerts["alerts"]} if weather_ok else set()
    for hz, g in guides.items():
        if g.get("available"):
            # 일반 시민 대상: 축산·농업·작업장 대상 문구는 뒤로 미룬다 (안정 정렬, 삭제하지 않음)
            g = {**g, "rows": sorted(g["rows"], key=lambda r: any(k in r["text"] for k in NICHE_WORDS))}
        if not g.get("available"):
            # cause 가 있으면 조회 실패(정보 없음), 없으면 공식 데이터에 해당 항목이 없음
            st = "failed" if g.get("cause") else "unavailable"
            gaps.append({"source": f"행동요령({hz})", "status": st, "cause": g.get("cause"),
                         "reason": reason(st, g["cause"]) if g.get("cause") else msg("causes.not_in_data"),
                         "detail": g.get("reason", "행동요령 없음")})
            continue
        # 공식 원문이라도 금지 표현("…지붕이 안전하니…")이 든 행은 근거로 쓰지 않는다 — 고쳐 쓰지 않고 뺀다 (T-024)
        # 본문 링크는 검증기가 막으므로(링크는 칩으로만) URL 이 든 원문 행도 근거에서 뺀다 (T-030)
        rows = [r for r in g["rows"] if not any(w in r["text"] for w in FORBIDDEN) and not URL.search(r["text"])]
        for group, r in group_guides(rows, active=hz in active_hazards):
            # [G#] 인용에 소스(기관·API 이름)를 함께 싣는다 (ADR-013)
            guide_rows.append({"id": f"G{len(guide_rows) + 1}", "kind": "guide", "hazard": hz, "phase": r["phase"],
                               "group": group, "group_label": group_label(group, hz in active_hazards),
                               "text": r["text"], "ref": r["id"], "source": g.get("source_label") or g.get("source")})
    ev.extend(guide_rows)

    n_shelter = 0
    for hz, sh in (shelters or {}).items():
        if sh.get("status") == "failed":
            gaps.append({"source": f"{msg('shelters.source_name')}({hz})", "status": "failed", "cause": sh.get("cause"),
                         "reason": reason("failed", sh.get("cause")), "detail": sh.get("reason", "")})
            continue
        if sh.get("status") != "ok":
            continue  # 해당 없음·범위 데이터 없음: 쉼터 없이 정상 응답 (실패가 아님)
        header = msg("shelters.header", kind_label=sh["kind_label"], hazard=hz, source=sh.get("source_label") or sh["source"])
        for s_ in sh["shelters"]:
            n_shelter += 1
            ev.append({"id": f"S{n_shelter}", "kind": "shelter", "hazard": hz, "text": f"{s_['name']} — {s_['address']}",
                       "group_label": header, "ref": s_["id"], "source": sh.get("source_label") or sh["source"],
                       "map_url": shelter_link(s_["address"])})

    note = None
    if weather_ok and level == "none" and msg_rows:
        note = msg("judge.messages_only_note")
    return {"region_id": region_id, "region": alerts["region"], "level": level, "level_label": LEVEL_LABEL[level],
            "hazards": hazards, "uncertain": uncertain, "as_of": alerts["as_of"], "mode": alerts["mode"],
            "zones": alerts["zones"], "zone_names": alerts.get("zone_names") or alerts["zones"], "evidence": ev, "gaps": gaps, "note": note, "statement": alerts["statement"]}
