"""지역 정규화와 모호 지명 해석 (결정론, LLM 미사용).

- resolve_location: 사용자 표현 → 후보 지역 ID와 점수/근거
- message_applies : 재난문자 수신지역(RCPTN_RGN_NM)이 특정 지역에 적용되는지
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "regions.json"

# 점수 가중치: 명시적 수식어 > 대화 이력 > 브라우저 위치 > 약한 별칭
W_STRONG, W_HISTORY, W_BROWSER, W_WEAK = 10, 5, 2, 3
MARGIN_RESOLVED = 5  # 1,2위 차이가 이 이상이면 확정


@lru_cache(maxsize=1)
def load_regions() -> dict:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    return {r["id"]: r for r in data["regions"]}


def resolve_location(text: str, history_region_id: str | None = None,
                     browser_region_id: str | None = None) -> dict:
    """status: resolved(확정) / assumed(가정 칩 표시) / ambiguous(둘 다 표시) / none."""
    regions = load_regions()
    scores: dict[str, int] = {}
    reasons: dict[str, list[str]] = {}

    def add(rid: str, w: int, why: str) -> None:
        scores[rid] = scores.get(rid, 0) + w
        reasons.setdefault(rid, []).append(f"{why}(+{w})")

    for rid, r in regions.items():
        for a in r["aliases_strong"]:
            if a in text:
                add(rid, W_STRONG, f"명시 표현 '{a}'")
        for a in r["aliases_weak"]:
            if a in text:
                add(rid, W_WEAK, f"약한 표현 '{a}'")
    if history_region_id in regions:
        add(history_region_id, W_HISTORY, "대화 이력")
    if browser_region_id in regions:
        add(browser_region_id, W_BROWSER, "브라우저 위치")

    ranked = sorted(scores.items(), key=lambda t: (-t[1], t[0]))
    cands = [{"id": rid, "name": regions[rid]["display"], "score": sc, "reasons": reasons[rid]}
             for rid, sc in ranked]
    if not cands:
        return {"status": "none", "chosen": None, "candidates": []}

    # 약한 별칭만 겹친 경우(점수 동률)는 모호 → 호출자가 둘 다 보여준다.
    top = ranked[0][1]
    second = ranked[1][1] if len(ranked) > 1 else 0
    if len(ranked) == 1 or top - second >= MARGIN_RESOLVED:
        status = "resolved" if top >= W_STRONG or len(ranked) == 1 and top >= W_HISTORY else "assumed"
        return {"status": status, "chosen": ranked[0][0], "candidates": cands}
    if top > second:
        return {"status": "assumed", "chosen": ranked[0][0], "candidates": cands}
    return {"status": "ambiguous", "chosen": None, "candidates": cands}


def message_applies(region_id: str, rcptn_rgn_nm: str) -> str | None:
    """'direct' | 'sido_wide' | None.  RCPTN_RGN_NM 은 쉼표 결합, 끝 공백, '전체' 접미가 섞여 있다."""
    r = load_regions()[region_id]
    result = None
    for raw in rcptn_rgn_nm.split(","):
        name = raw.strip()
        if name.endswith(" 전체"):
            name = name[: -len(" 전체")].strip()
        if not name:
            continue
        if any(name == p or name.startswith(p + " ") for p in r["dssp_prefixes"]):
            return "direct"
        if name in r["dssp_sido_wide"]:
            result = "sido_wide"
    return result
