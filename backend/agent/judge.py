"""위험 수준 판정 (결정론). 합의된 규칙:
  - 발효 중 특보가 있으면 그 등급(주의보/경보/중대경보)을 그대로 쓴다.
  - 재난문자·행동요령은 '근거와 설명'으로만 쓰며 수준을 올리지 않는다.
  - 특보 조회가 실패/범위 밖이면 수준은 unknown (안전 아님).
수준: unknown < none < upcoming < advisory < warning < severe  (표시 순서일 뿐 'unknown'이 낮은 위험이라는 뜻이 아님)
"""
from __future__ import annotations

import re

from config.catalog import RULES, msg, reason

from .validator import URL, shelter_link  # 본문 링크 금지와 같은 기준

LEVEL_LABEL: dict[str, str] = RULES["levels"]["labels"]
STRESS_TO_LEVEL: dict[str, str] = RULES["levels"]["stress_to_level"]
RANK: dict[str, int] = RULES["levels"]["rank"]
MAX_MESSAGES = RULES["evidence"]["max_messages"]
GROUPS = RULES["guide_groups"]
NICHE_WORDS = tuple(RULES["niche_words"])
FORBIDDEN = tuple(RULES["forbidden"])
MUST_ACTIONS: dict[str, dict] = RULES["must_actions"]
SENTENCE = re.compile(r"(?<=[.!?])\s+|(?<=[.!?])(?=\S)|\s-\s|-\s|[●※]")
NON_TEXT = re.compile(r"[^가-힣A-Za-z0-9]")


def is_prohibition(text: str) -> bool:
    """문장의 핵심 서술이 금지인가 (ADR-020). 문장·쉼표로 나눈 조각 중 금지 조각이 있고 권고 조각이 없을 때만 True.
    섞인 문장("물을 많이 마시고, 카페인 음료는 마시지 않습니다")은 False — 쪼개지 않고 단계 묶음에 둔다."""
    pro = rec = 0
    for sent in SENTENCE.split(text):
        segs = [x.strip(" .") for x in re.split(r"[,，]", sent) if x.strip(" .")]
        for i, seg in enumerate(segs):
            if any(w in seg for w in GROUPS["dont_words"]) or seg.endswith(tuple(GROUPS["dont_endings"])):
                pro += 1
            elif seg.endswith(tuple(GROUPS["recommend_endings"])) or i == len(segs) - 1:
                rec += 1
    return pro > 0 and rec == 0


def _bigrams(text: str) -> set[str]:
    t = NON_TEXT.sub("", text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similarity(a: str, b: str) -> float:
    """공백·기호를 뺀 글자 2-gram Dice 계수 (0~1)."""
    x, y = _bigrams(a), _bigrams(b)
    return 2 * len(x & y) / (len(x) + len(y)) if x and y else 0.0


def similar(a: str, b: str) -> bool:
    return similarity(a, b) >= GROUPS["similar_threshold"]


def guide_group(r: dict) -> str | None:
    """행 하나 → 묶음 (now | prepare | dont | None). 금지 서술이 단계보다 먼저다."""
    if r["phase"] in ("info",):
        return None
    if is_prohibition(r["text"]):
        return "dont"
    return GROUPS["phase_to_group"].get(r["phase"])


def must_rows(rows: list[dict], hazard: str) -> list[dict]:
    """필수 행동(증상·응급 대처, rules.yaml must_actions) — 우선순위 순, 단계 무관."""
    out: list[dict] = []
    for words in (MUST_ACTIONS.get(hazard) or {}).get("rows", []):
        out += [r for r in rows if all(w in r["text"] for w in words) and r not in out]
    return out


def cap_guides(rows: list[dict], active_hazards: set[str]) -> list[dict]:
    """지역 전체 행동요령 상한 (rules.yaml guide_groups.max_total, K7). 남길 우선순위: 필수 행동(symptom) > 묶음마다 첫 행
    (묶음이 통째로 사라지지 않게 — '하지 말 것'이 빠지면 위험) > 단계 일치(발효 중이면 during, 아니면 prepare) > 고른 순서.
    남긴 행은 원래 순서(묶음 순서) 그대로 — 원문은 바꾸지 않는다."""
    limit = GROUPS["max_total"]
    if len(rows) <= limit:
        return rows

    firsts = {next(i for i, x in enumerate(rows) if (x["hazard"], x["group"]) == k)
              for k in {(x["hazard"], x["group"]) for x in rows}}

    def rank(i_e):
        i, e = i_e
        phase_ok = e["phase"] == ("during" if e["hazard"] in active_hazards else "prepare")
        return (0 if e["group"] == "symptom" else 1 if i in firsts else 2 if phase_ok else 3, i)
    keep = {i for i, _ in sorted(enumerate(rows), key=rank)[:limit]}
    return [e for i, e in enumerate(rows) if i in keep]


def group_label(group: str, active: bool) -> str:
    return msg(f"guide_groups.{'now_inactive' if group == 'now' and not active else group}")


def group_guides(rows: list[dict], active: bool, hazard: str = "", seen: list[str] | None = None,
                 limits: dict | None = None) -> list[tuple[str, dict]]:
    """'그래서 어떻게': 지금 할 일 / 증상이 있으면 / 미리 대비할 일 / 하지 말 것 (ADR-016, ADR-020). 묶음별 상한, 원문 순서 유지.
    증상이 있으면(symptom)은 발효 중일 때만, must_actions 행. 거의 같은 행(seen 포함)은 건너뛰고 다음 후보로 채운다.
    LLM 은 여기서 고른 항목만 풀어 쓴다."""
    order = GROUPS["order_active"] if active else GROUPS["order_inactive"]
    seen = seen if seen is not None else []
    must = must_rows(rows, hazard) if active else []
    limits = limits or GROUPS["limits"]
    out = []
    for grp in order:
        cands = must if grp == "symptom" else [r for r in rows if guide_group(r) == grp and r not in must]
        n = 0
        for r in cands:
            if n >= limits[grp]:
                break
            if any(similar(r["text"], t) for t in seen):
                continue
            seen.append(r["text"])
            out.append((grp, r))
            n += 1
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

    guide_rows, guide_notes = [], []
    active_hazards = {a["hazard"] for a in alerts["alerts"]} if weather_ok else set()
    seen: list[str] = []  # 지역 안에서 이미 고른 행 (유형이 달라도 같은 문장은 한 번만 — 태풍·호우 공통 문장 등)
    shown = {hz for hz, g in guides.items() if g.get("available") and not (
        g.get("borrowed_from") in guides and guides[g["borrowed_from"]].get("available"))}
    limits = GROUPS["limits_multi"] if len(shown) > 1 else GROUPS["limits"]
    for hz, g in sorted(guides.items(), key=lambda kv: bool(kv[1].get("borrowed_from"))):  # 자기 행동요령이 있는 유형 먼저
        if g.get("available") and hz not in shown:
            continue  # 열대야 + 폭염: 같은 폭염 행동요령이므로 한 번만

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
        picked = group_guides(rows, active=hz in active_hazards, hazard=g.get("borrowed_from") or hz, seen=seen, limits=limits)
        if not picked:  # 받았지만 고를 행이 없음 — 0줄로 조용히 나가지 않고 '해당 항목이 없습니다'로 밝힌다
            gaps.append({"source": f"행동요령({hz})", "status": "unavailable", "cause": None,
                         "reason": msg("causes.not_in_data"), "detail": "선택된 행 없음"})
            continue
        if g.get("borrowed_from"):
            guide_notes.append(msg("answer.guide_borrowed", hazard=hz, base=g["borrowed_from"]))
        for group, r in picked:
            # [G#] 인용에 소스(기관·API 이름)를 함께 싣는다 (ADR-013). id 는 전체 상한 적용 뒤 매긴다
            guide_rows.append({"kind": "guide", "hazard": hz, "phase": r["phase"],
                               "group": group, "group_label": group_label(group, hz in active_hazards),
                               "text": r["text"], "ref": r["id"], "source": g.get("source_label") or g.get("source"),
                               **({"borrowed_from": g["borrowed_from"]} if g.get("borrowed_from") else {})})
    guide_rows = cap_guides(guide_rows, active_hazards)
    for i, e in enumerate(guide_rows, 1):
        e["id"] = f"G{i}"
    ev.extend(guide_rows)

    n_shelter = 0
    for hz, sh in (shelters or {}).items():
        if sh.get("status") == "failed":
            gaps.append({"source": f"{msg('shelters.source_name')}({hz})", "status": "failed", "cause": sh.get("cause"),
                         "reason": reason("failed", sh.get("cause")), "detail": sh.get("reason", "")})
            continue
        if sh.get("status") != "ok":
            continue  # 해당 없음·범위 데이터 없음: 쉼터 없이 정상 응답 (실패가 아님)
        header = msg("shelters.header", kind_label=sh["kind_label"])  # 출처(통합대피소)는 근거의 source 에
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
            "zones": alerts["zones"], "zone_names": alerts.get("zone_names") or alerts["zones"], "evidence": ev, "gaps": gaps, "note": note, "guide_notes": guide_notes, "statement": alerts["statement"]}


def evidence_view(j: dict) -> dict:
    """판정 결과 → 화면용 구조화 근거 (SSE evidence, ADR-019). 코드가 고른 근거를 종류·묶음별로 나눌 뿐 문장을 만들지 않는다.
    LLM 문장이 아니므로 검증 전에 보내도 된다."""
    ev = j["evidence"]
    groups: list[dict] = []
    for e in (x for x in ev if x["kind"] == "guide"):
        if not groups or groups[-1]["group"] != e["group"] or groups[-1]["hazard"] != e["hazard"]:
            groups.append({"group": e["group"], "label": e["group_label"], "hazard": e["hazard"], "items": []})
        groups[-1]["items"].append(e)
    shelters = [x for x in ev if x["kind"] == "shelter"]
    return {"region_id": j["region_id"], "region": j["region"], "level": j["level"], "level_label": j["level_label"],
            "as_of": j["as_of"], "scope_note": j.get("scope_note"), "note": j.get("note"),
            "alerts": [x for x in ev if x["kind"] in ("alert", "pending", "released")],
            "messages": [x for x in ev if x["kind"] == "message"],
            "guide_groups": groups,
            "shelters": {"header": shelters[0]["group_label"], "items": shelters} if shelters else None,
            "gaps": [g for g in j["gaps"] if g["status"] != "not_queried"]}
