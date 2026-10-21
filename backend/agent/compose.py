"""근거 기반 답변 템플릿 (결정론). FakeLLM 의 출력이자, LLM 답변이 검증을 통과하지 못할 때의 안전한 대체."""
from __future__ import annotations

import re

from config.catalog import msg, reason

DISCLAIMER = msg("disclaimer")
RULE_LINE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
CITE_TAIL = re.compile(r"\s*\[[EMGS]\d+\]")
RAW_TAG = re.compile(r"\s*(\[[^\[\]\n]{1,30}\])")
NO_INFO, NO_RISK = msg("markers.no_info"), msg("markers.no_risk")


def is_gap_line(line: str) -> bool:
    """'정보 없음' 줄(조회 실패·범위 밖·데이터에 없음). '확인된 위험 정보 없음'은 판정 문장이라 제외."""
    return NO_INFO in line and NO_RISK not in line


def tidy_llm(text: str, judgements: list[dict] | None = None) -> str:
    """LLM 출력의 형식만 정돈한다 (검증 전, 문장은 건드리지 않는다 — 모든 내용 줄은 그대로 검증).
    - 내용 없는 구분선('---')·줄 끝 공백을 지운다.
    - '정보 없음' 줄에 붙은 근거 번호를 뗀다: 근거가 없다는 줄이라 번호는 무관한 근거를 가리킨다 (qwen 이 습관적으로 [G1]을 붙임, T-039).
    - 근거 번호 꼴이 아니고 공식 원문에도 없는 대괄호 태그를 뗀다: qwen 이 근거 데이터의 키 이름을 '[Gaps]'처럼 붙임 (T-039).
      공식 원문에 있는 태그([행정안전부] 등)는 둔다.
    검증기의 같은 규칙('정보 없음 줄에 근거 번호', '근거 번호 꼴이 아닌 태그')은 그대로 두고, 형식만 코드가 고친다."""
    texts = [e.get("text", "") for j in (judgements or []) for e in j["evidence"]]

    def raw_tag(m: re.Match) -> str:
        t = m.group(1)
        return m.group(0) if CITE_TAIL.fullmatch(t) or any(t in x for x in texts) else ""

    def fix(line: str) -> str:
        line = RAW_TAG.sub(raw_tag, line)
        return (CITE_TAIL.sub("", line) if is_gap_line(line) else line).rstrip()
    return "\n".join(fix(line) for line in text.splitlines() if not RULE_LINE.match(line))


def _cite(e: dict) -> str:
    return f"[{e['id']}]"


def zone_text(j: dict) -> str:
    """조회 구역은 표시 이름으로 (특보구역 코드는 사용자에게 보이지 않는다, 개발자 상세에만)."""
    return "·".join(j.get("zone_names") or j["zones"])


def gap_line(g: dict) -> str:
    """조회 실패·범위 밖·데이터에 없음 표준 문구 — 원인별 사유를 붙인다 (ADR-014). 템플릿과 write 지시문이 같은 문구를 쓴다."""
    if g["status"] == "unavailable":
        return msg("answer.gap_unavailable", source=g["source"], reason=msg("causes.not_in_data"))
    return msg("answer.gap", source=g["source"], reason=reason(g["status"], g.get("cause")))


def guide_heading(e: dict, evidence: list[dict]) -> str:
    """묶음 제목 — 한 지역에 행동요령 유형이 둘 이상일 때만 유형 이름을 붙인다 (템플릿·write 지시문 공용)."""
    many = len({x["hazard"] for x in evidence if x["kind"] == "guide"}) > 1
    return msg("answer.guide_group_hazard" if many else "answer.guide_group", label=e["group_label"], hazard=e["hazard"])


def guide_sources(evidence: list[dict]) -> str:
    return ", ".join(dict.fromkeys("행정안전부 " + (x.get("source") or "국민행동요령") for x in evidence if x["kind"] == "guide"))


def render_answer(judgements: list[dict], intent: str = "status") -> str:
    out: list[str] = []
    scope_label = msg("answer.message_scope")
    for j in judgements:
        out.append(f"### {j['region']}")
        if j.get("scope_note"):
            out.append(j["scope_note"])
        evs = j["evidence"]
        alerts = [e for e in evs if e["kind"] in ("alert", "pending", "released")]
        if j["level"] == "unknown":
            out.append(msg("answer.unknown"))
        elif j["level"] == "none":
            out.append(msg("answer.none", as_of=j["as_of"], zones=zone_text(j)))
        else:
            out.append(msg("answer.level", level_label=j["level_label"], as_of=j["as_of"]))
        for e in alerts:
            out.append(f"- {e['text']} {_cite(e)}" + (msg("answer.uncertain_suffix") if e.get("uncertain") else ""))
        if j.get("note"):
            out.append(j["note"])
        for e in [x for x in evs if x["kind"] == "message"]:
            body = e["text"].replace("\n", " ")
            out.append(f"- 최근 안내 문자({e['created_at']}, {scope_label.get(e['scope'], scope_label['direct'])}): "
                       f"{body[:90]}{'…' if len(body) > 90 else ''} {_cite(e)}")
        guides = [x for x in evs if x["kind"] == "guide"]
        if guides:
            out.append(msg("answer.guide_header", sources=guide_sources(evs)))
            out += j.get("guide_notes", [])  # 다른 유형의 행동요령을 쓴다는 안내 (열대야 → 폭염)
            last = None
            for e in guides:  # judge 가 묶음 순서대로 골라 둔 것을 그대로 (지금 할 일 / 미리 대비할 일 / 하지 말 것)
                head = (e["hazard"], e.get("group_label"))
                if e.get("group_label") and head != last:
                    out.append(guide_heading(e, evs))
                    last = head
                out.append(f"- {e['text']} {_cite(e)}")  # 원문 그대로 (출처는 제목 줄)
        last = None
        for e in [x for x in evs if x["kind"] == "shelter"]:  # 공식 목록에 있는 장소 (운영 여부 미확인 안내가 제목)
            if e["group_label"] != last:
                out.append(e["group_label"])
                last = e["group_label"]
            out.append(f"- {e['text']} {_cite(e)}")
        for g in j["gaps"]:
            if g["status"] == "not_queried":
                continue
            out.append(gap_line(g))
        if intent == "go_out":
            out.append(msg("answer.go_out"))
        out.append("")
    out.append(DISCLAIMER)
    return "\n".join(out)
