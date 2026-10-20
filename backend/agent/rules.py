"""규칙 기반 판단 (LLM 미사용): 위급 표현 감지, 범위 밖 질문, 재난 유형 추출, 의도 분류.

LLM 이 실패하거나 엉뚱한 값을 내도 이 모듈이 안전망이다 (recall 우선: 애매하면 더 많이 잡는다).
"""
from __future__ import annotations

import re

SUPPORTED_REGIONS_TEXT = "광주광역시, 경기도 광주시"

EMERGENCY_PATTERNS = [
    r"물이?\s*차(오|올)", r"침수(되|돼|됐)", r"갇(혔|혀)", r"떠내려", r"살려", r"구조(해|요청)", r"도와(줘|주세요|주십시오)",
    r"무너(졌|지고|질)", r"불이?\s*(났|나고|붙)", r"다쳤", r"의식이?\s*(없|잃)", r"숨을?\s*못", r"대피(해야|중|하고)\s*있",
    r"지금\s*당장", r"급해", r"위급",
]
EMERGENCY_TEXT = (
    "위급한 상황이면 지금 바로 119(소방·구조) 또는 112(경찰)에 전화하세요. "
    "안전한 높은 곳이나 튼튼한 건물로 이동하고, 위험 지역에서 벗어나세요. "
    "이 서비스는 공식 경보가 아니며, 위급 상황의 판단을 대신하지 않습니다."
)
OUT_OF_SCOPE_TEXT = (
    "이 서비스는 특보·재난문자·행동요령 정보를 지역별로 정리해 드립니다. "
    f"현재 지원 지역은 {SUPPORTED_REGIONS_TEXT}입니다. 재난·특보 관련 질문을 지역과 함께 해 주세요. "
    "(예: '경기도 광주 강풍특보 났는데 지금 나가도 돼?')"
)
ASK_REGION_TEXT = (
    f"어느 지역인지 알려 주세요. 현재 지원 지역은 {SUPPORTED_REGIONS_TEXT}입니다. "
    "(예: '광주광역시', '경기도 광주시')"
)

# 재난 유형 동의어 (hazard_map.json 의 이름과 일치해야 함 — 테스트가 검증)
HAZARD_SYNONYMS: dict[str, list[str]] = {
    "강풍": ["강풍", "돌풍", "바람이", "바람도", "바람은", "바람 많이", "바람 세", "강한 바람"],
    "호우": ["호우", "폭우", "집중호우", "장마", "소나기", "비가", "비 많이", "비 와", "비 오", "비가 많이", "빗물", "침수"],
    "한파": ["한파", "추위", "동파", "얼어", "기온이 떨어"],
    "건조": ["건조특보", "건조주의보", "건조경보", "건조"],
    "폭풍해일": ["폭풍해일", "해일"],
    "풍랑": ["풍랑", "파도", "높은 파도"],
    "태풍": ["태풍"],
    "대설": ["대설", "폭설", "눈이 많이", "눈이 와", "눈이 오", "적설"],
    "황사": ["황사", "미세먼지"],
    "폭염": ["폭염", "무더위", "더위", "찜통"],
    "열대야": ["열대야"],
}
GENERIC_DISASTER_WORDS = ["특보", "재난", "경보", "주의보", "날씨", "위험", "안전", "대피", "재난문자", "행동요령", "기상"]
REGION_WORDS = ["광주", "경기", "전남", "전라", "서울", "부산", "대구", "인천", "대전", "울산", "세종", "강원", "충", "경상", "제주"]

GO_OUT = re.compile(r"(나가|나갈|외출|이동|출근|퇴근|운전|가도|가야|가려|출발|등산|산책|성묘)")
GUIDE = re.compile(r"(행동요령|어떻게|뭘\s*해야|무엇을|대비|준비|조심|주의사항)")


def is_emergency(text: str) -> bool:
    return any(re.search(p, text) for p in EMERGENCY_PATTERNS)


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
