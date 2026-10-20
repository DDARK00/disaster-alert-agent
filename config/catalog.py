"""rules.yaml · prompts/messages.yaml · prompts/*.md 로더. 파일·키가 빠지면 import 시점(기동 시)에 실패한다.

프롬프트 파일 형식: '=== 이름 ===' 줄로 구역을 나눈다. 구역 내용은 다음 구역 표시 직전의 줄바꿈 하나만 뺀 원문.
자리표시자는 string.Template 의 $name (빠진 값은 KeyError).
"""
from __future__ import annotations

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
    "write": ["user", "ids_none", "must_header", "must_none", "must_unknown", "must_gap"],
    "rewrite": ["user"],
    "briefing": ["system"],
}
REQUIRED_MESSAGES = [
    "supported_regions", "disclaimer", "fixed.emergency", "fixed.out_of_scope", "fixed.ask_region",
    "answer.unknown", "answer.none", "answer.level", "answer.uncertain_suffix", "answer.message_scope",
    "answer.guide_header", "answer.gap", "answer.go_out", "judge.messages_only_note",
    "mcp.alert_failed", "mcp.alert_active", "mcp.alert_none", "mcp.alert_none_pending", "mcp.alert_none_no_pending",
    "mcp.messages_failed", "mcp.messages_count", "mcp.alert_unreachable",
]
REQUIRED_RULES = ["emergency_words", "intent_words", "hazard_synonyms", "generic_disaster_words", "region_words",
                  "forbidden", "levels", "evidence", "niche_words"]
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


def render(template: str, **kw) -> str:
    return Template(template).substitute(**kw)


def msg(key: str, **kw):
    """messages.yaml 의 문구. 문자열이면 $자리표시자를 채운다 (supported_regions 는 기본 제공)."""
    v = _get(MESSAGES, key)
    if not isinstance(v, str):
        return v
    return render(v, supported_regions=MESSAGES["supported_regions"], **kw)


def prompt(name: str, section: str = "system", **kw) -> str:
    return render(PROMPTS[name][section], **kw)


def words_regex(words: list[str]) -> re.Pattern:
    """단어 목록 → 정규식. 단어 안의 공백은 공백 0개 이상(\\s*), 나머지는 글자 그대로."""
    return re.compile("|".join(re.escape(w).replace(r"\ ", r"\s*") for w in words))
