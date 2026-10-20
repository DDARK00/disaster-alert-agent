"""답변 검증 (결정론). 위반이 있으면 재작성 → 그래도 실패하면 템플릿 답변으로 대체한다."""
from __future__ import annotations

import re

FORBIDDEN = ["안전합니다", "안전해요", "안전하니", "안전합니다만", "걱정하지 마", "걱정 마", "문제없", "문제 없", "괜찮습니다",
             "괜찮아요", "위험하지 않", "위험 없", "안심하"]
CITE = re.compile(r"\[([EMG]\d+)\]")


def validate(answer: str, judgements: list[dict]) -> dict:
    v: list[str] = []
    ids = {e["id"] for j in judgements for e in j["evidence"]}

    for w in FORBIDDEN:
        if w in answer:
            v.append(f"금지 표현: '{w}'")
    if "공식 경보" not in answer or "119" not in answer:
        v.append("공식 경보 아님/119 안내 문구 누락")

    cited = set(CITE.findall(answer))
    for c in cited - ids:
        v.append(f"존재하지 않는 근거 인용: [{c}]")

    for line in answer.splitlines():
        s = line.strip()
        if s.startswith(("-", "•", "*")) and not CITE.search(s) and "정보 없음" not in s and "안전을 보장" not in s:
            v.append(f"근거 없는 항목: '{s[:40]}'")

    for j in judgements:
        if j["region"] not in answer:
            v.append(f"지역 누락: {j['region']}")
        if j["level"] == "unknown" and "정보 없음" not in answer:
            v.append("조회 실패인데 '정보 없음' 표기 없음")
        if j["level"] == "none" and "확인된 위험 정보 없음" not in answer:
            v.append("위험 없음 표현이 규정 문구가 아님")
        if j["level"] in ("advisory", "warning", "severe", "upcoming"):
            alert_ids = {e["id"] for e in j["evidence"] if e["kind"] in ("alert", "pending")}
            if alert_ids and not (alert_ids & cited):
                v.append(f"{j['region']}: 발효 중 특보 근거 인용 없음")
        for g in j["gaps"]:
            if g["status"] in ("failed", "no_coverage") and "정보 없음" not in answer:
                v.append(f"{g['source']} 조회 실패 미표기")
    return {"ok": not v, "violations": v}
