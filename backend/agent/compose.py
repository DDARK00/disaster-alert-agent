"""근거 기반 답변 템플릿 (결정론). FakeLLM 의 출력이자, LLM 답변이 검증을 통과하지 못할 때의 안전한 대체."""
from __future__ import annotations

DISCLAIMER = "공식 경보가 아닙니다. 위급할 때는 119·112, 공식 채널(기상청, 재난문자, 국민재난안전포털)을 확인하세요."


def _cite(e: dict) -> str:
    return f"[{e['id']}]"


def render_answer(judgements: list[dict], intent: str = "status") -> str:
    out: list[str] = []
    for j in judgements:
        out.append(f"### {j['region']}")
        evs = j["evidence"]
        alerts = [e for e in evs if e["kind"] in ("alert", "pending", "released")]
        if j["level"] == "unknown":
            out.append("정보 없음 — 기상특보를 조회하지 못했습니다. 이는 안전하다는 뜻이 아닙니다.")
        elif j["level"] == "none":
            out.append(f"확인된 위험 정보 없음 (기준 {j['as_of']}, 조회 구역 {', '.join(j['zones'])}). 안전을 보장하지 않습니다.")
        else:
            out.append(f"{j['level_label']} (기준 {j['as_of']})")
        for e in alerts:
            out.append(f"- {e['text']} {_cite(e)}" + (" (해석이 불확실한 발표코드 — 공식 확인 필요)" if e.get("uncertain") else ""))
        if j.get("note"):
            out.append(j["note"])
        for e in [x for x in evs if x["kind"] == "message"]:
            body = e["text"].replace("\n", " ")
            out.append(f"- 최근 안내 문자({e['created_at']}, {'시도 전체' if e['scope'] == 'sido_wide' else '해당 지역'}): "
                       f"{body[:90]}{'…' if len(body) > 90 else ''} {_cite(e)}")
        guides = [x for x in evs if x["kind"] == "guide"]
        if guides:
            out.append("행동요령 (행정안전부 자연재난국민행동요령):")
            for e in guides:
                out.append(f"- [{e['hazard']}] {e['text']} {_cite(e)}")
        for g in j["gaps"]:
            if g["status"] == "not_queried":
                continue
            out.append(f"- 정보 없음: {g['source']} ({g['status']}) — 안전하다는 뜻이 아닙니다.")
        if intent == "go_out":
            out.append("※ 외출 가능 여부는 이 서비스가 판단하지 않습니다. 위 확인된 정보와 행동요령을 참고해 직접 판단하세요.")
        out.append("")
    out.append(DISCLAIMER)
    return "\n".join(out)
