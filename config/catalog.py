"""rules.yaml · prompts/messages.yaml · prompts/*.md 로더. 파일·키가 빠지면 import 시점(기동 시)에 실패한다.

프롬프트 파일 형식: '=== 이름 ===' 줄로 구역을 나눈다. 구역 내용은 다음 구역 표시 직전의 줄바꿈 하나만 뺀 원문.
자리표시자는 string.Template 의 $name (빠진 값은 KeyError).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from string import Template

import yaml

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "config" / "rules.yaml"
PROMPTS_DIR = ROOT / "prompts"

REQUIRED_PROMPTS = {
    "intent": ["system"],
    "plan": ["system", "user"],
    "write": ["user", "region_ids", "must_cite", "guide_groups", "shelter_groups", "ids_none", "must_header", "must"],
    "rewrite": ["user"],
    "briefing": ["system"],
}
REQUIRED_MESSAGES = [
    "supported_regions", "disclaimer", "fixed.emergency", "fixed.out_of_scope", "fixed.ask_region", "fixed.ask_region_candidates", "fixed.ask_region_province", "region.district_scope", "fixed.service_unavailable", "fixed.not_in_data",
    "answer.unknown", "answer.none", "answer.level", "answer.uncertain_suffix", "answer.message_scope",
    "answer.guide_header", "answer.guide_group", "guide_groups.now", "guide_groups.now_inactive",
    "guide_groups.prepare", "guide_groups.dont", "answer.gap", "answer.gap_unavailable", "answer.go_out", "judge.messages_only_note", "judge.since_before_window",
    "mcp.alert_failed", "mcp.alert_active", "mcp.alert_none", "mcp.alert_none_pending", "mcp.alert_none_no_pending",
    "mcp.messages_failed", "mcp.messages_count", "mcp.alert_unreachable",
    "kakao.route_notice", "kakao.map_label", "kakao.route_label",
    "shelters.header", "shelters.source_name", "shelters.map_label", "shelters.map_notice",
]
# 조회 실패 원인 코드 (ADR-014). MCP(외부 API)·백엔드(MCP 연결) 공용. 문구는 messages.yaml causes.<코드>
CAUSE_CODES = ("api_auth_error", "rate_limited", "api_http_error", "api_result_error", "api_timeout", "network_error",
               "response_invalid", "mcp_unreachable", "mcp_tool_error")
REQUIRED_MESSAGES += [f"causes.{c}" for c in CAUSE_CODES] + ["causes.no_coverage", "causes.unknown", "causes.not_in_data"]
# 표준 문구가 검증기 기준 조각을 포함해야 템플릿 fallback 이 항상 검증을 통과한다
MARKER_CHECKS = {
    "no_info": ["fixed.not_in_data", "fixed.service_unavailable", "answer.unknown", "answer.gap", "answer.gap_unavailable", "mcp.alert_failed", "mcp.alert_unreachable", "mcp.messages_failed"],
    "no_risk": ["answer.none", "mcp.alert_none"],
    "no_guarantee": ["answer.none"],
    "official": ["disclaimer", "fixed.emergency", "fixed.service_unavailable", "fixed.not_in_data"],
    "emergency_call": ["disclaimer", "fixed.emergency", "fixed.service_unavailable", "fixed.not_in_data"],
    "not_in_data": ["causes.not_in_data", "fixed.not_in_data"],
    "shelter_notice": ["shelters.header"],
}
REQUIRED_RULES = ["emergency_words", "intent_words", "hazard_synonyms", "generic_disaster_words", "region_words",
                  "forbidden", "levels", "evidence", "niche_words", "region_suffixes", "api_errors", "guide_audience", "not_in_official_data", "guide_groups", "shelters"]
SECTION = re.compile(r"^=== (\w+) ===\n", re.M)


def _get(d: dict, dotted: str):
    for k in dotted.split("."):
        d = d[k]
    return d


def _load_yaml(path: Path, required: list[str]) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    missing = []
    for k in required:
        try:
            _get(data, k)
        except (KeyError, TypeError):
            missing.append(k)
    if missing:
        raise RuntimeError(f"{path.name}: 필수 항목 누락 {missing}")
    return data


def _load_prompt(name: str, sections: list[str]) -> dict[str, str]:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise RuntimeError(f"프롬프트 파일 없음: prompts/{name}.md")
    text = path.read_text(encoding="utf-8")
    marks = list(SECTION.finditer(text))
    out = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end]
        out[m.group(1)] = body[:-1] if body.endswith("\n") else body
    missing = [s for s in sections if s not in out]
    if missing:
        raise RuntimeError(f"prompts/{name}.md: 구역 누락 {missing}")
    return out


RULES = _load_yaml(RULES_PATH, REQUIRED_RULES)
MESSAGES = _load_yaml(PROMPTS_DIR / "messages.yaml", REQUIRED_MESSAGES)
PROMPTS = {name: _load_prompt(name, secs) for name, secs in REQUIRED_PROMPTS.items()}


def _check_consistency() -> None:
    errs = []
    for mk, keys in MARKER_CHECKS.items():
        frag = _get(MESSAGES, f"markers.{mk}")
        errs += [f"{k} 에 '{frag}' 없음" for k in keys if frag not in _get(MESSAGES, k)]

    def strings(d):
        if isinstance(d, dict):
            for v in d.values():
                yield from strings(v)
        elif isinstance(d, str):
            yield d
    for s in list(strings(MESSAGES)) + [t for p in PROMPTS.values() for t in p.values()]:
        errs += [f"금지 표현 '{w}' 포함: {s[:30]}" for w in RULES["forbidden"] if w in s and f"'{w}'" not in s]
    if errs:
        raise RuntimeError("문구·규칙 불일치: " + "; ".join(errs))


_check_consistency()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


# LangSmith 메타데이터용: 어떤 프롬프트·문구·규칙 버전으로 답했는지 추적
VERSIONS = {**{f"prompt:{n}": _sha(PROMPTS_DIR / f"{n}.md") for n in REQUIRED_PROMPTS},
            "messages": _sha(PROMPTS_DIR / "messages.yaml"), "rules": _sha(RULES_PATH)}
VERSION_TAG = "prompts@" + hashlib.sha256("".join(sorted(VERSIONS.values())).encode()).hexdigest()[:8]


def render(template: str, **kw) -> str:
    return Template(template).substitute(**kw)


def msg(key: str, **kw):
    """messages.yaml 의 문구. 문자열이면 $자리표시자를 채운다 (supported_regions 는 기본 제공)."""
    v = _get(MESSAGES, key)
    if not isinstance(v, str):
        return v
    return render(v, supported_regions=MESSAGES["supported_regions"], **kw)


def reason(status: str, cause: str | None = None) -> str:
    """소스 상태·원인 코드 → 사용자 문구 (gap·statement 의 $reason)."""
    if status == "no_coverage":
        return msg("causes.no_coverage")
    return msg(f"causes.{cause}") if cause in CAUSE_CODES else msg("causes.unknown")


def prompt(name: str, section: str = "system", **kw) -> str:
    return render(PROMPTS[name][section], **kw)


def words_regex(words: list[str]) -> re.Pattern:
    """단어 목록 → 정규식. 단어 안의 공백은 공백 0개 이상(\\s*), 나머지는 글자 그대로."""
    return re.compile("|".join(re.escape(w).replace(r"\ ", r"\s*") for w in words))
