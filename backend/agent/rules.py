"""규칙 기반 판단 (LLM 미사용): 위급 표현 감지, 범위 밖 질문, 재난 유형 추출, 의도 분류.

LLM 이 실패하거나 엉뚱한 값을 내도 이 모듈이 안전망이다 (recall 우선: 애매하면 더 많이 잡는다).
"""
from __future__ import annotations

from config.catalog import RULES, msg, words_regex

SUPPORTED_REGIONS_TEXT = msg("supported_regions")

EMERGENCY = words_regex(RULES["emergency_words"])
EMERGENCY_TEXT = msg("fixed.emergency")
OUT_OF_SCOPE_TEXT = msg("fixed.out_of_scope")
ASK_REGION_TEXT = msg("fixed.ask_region")

# 재난 유형 동의어 (hazard_map.json 의 이름과 일치해야 함 — 테스트가 검증)
HAZARD_SYNONYMS: dict[str, list[str]] = RULES["hazard_synonyms"]
GENERIC_DISASTER_WORDS = RULES["generic_disaster_words"]
REGION_WORDS = RULES["region_words"]

GO_OUT = words_regex(RULES["intent_words"]["go_out"])
GUIDE = words_regex(RULES["intent_words"]["guide"])


REGION_SUFFIXES = RULES["region_suffixes"]


def place_candidate(place, query: str) -> str:
    """LLM 이 뽑은 장소 표현을 검사한다 (코드). 질문 원문에 그대로 있고, 지명·행정구역 접미만이 아닐 때만 쓴다."""
    if not isinstance(place, str):
        return ""
    place = place.strip()
    if not place or len(place) > 40 or place not in query:
        return ""
    rest = place
    for w in sorted(REGION_WORDS, key=len, reverse=True):
        rest = rest.replace(w, " ")
    tokens = [t for t in rest.split() if t not in REGION_SUFFIXES]
    return place if "".join(tokens) else ""


def is_emergency(text: str) -> bool:
    return bool(EMERGENCY.search(text))


def parse_hazards(text: str) -> dict:
    found: list[str] = []
    for name, words in HAZARD_SYNONYMS.items():
        if any(w in text for w in words):
            found.append(name)
    # '건조'는 다른 단어 속에 흔히 들어가지 않지만 '미세먼지'→황사 같은 약한 연결은 힌트일 뿐이다.
    return {"hazards": found, "unmatched": []}


def detect_intent(text: str) -> str:
    if GO_OUT.search(text):
        return "go_out"
    if GUIDE.search(text):
        return "guide"
    return "status"


def is_out_of_scope(text: str) -> bool:
    """재난·지역·유형 어느 신호도 없으면 범위 밖."""
    if parse_hazards(text)["hazards"]:
        return False
    if any(w in text for w in GENERIC_DISASTER_WORDS):
        return False
    if any(w in text for w in REGION_WORDS):
        return False
    return True


def route(text: str) -> str:
    """emergency | out_of_scope | disaster  (위급 판정이 항상 우선)."""
    text = text.strip()
    if is_emergency(text):
        return "emergency"
    if not text or is_out_of_scope(text):
        return "out_of_scope"
    return "disaster"
