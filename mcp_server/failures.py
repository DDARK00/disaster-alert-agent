"""외부 API 호출 실패 원인 분류 (결정론) + 소스별 최근 성공·실패 기록 (ADR-014).

원인 코드 (status=failed 일 때 cause 로 붙는다. no_coverage 는 실패가 아니라 범위 밖이므로 cause 없음)
  api_auth_error   서비스키 미설정·미등록·만료 (HTTP 401/403 포함)
  rate_limited     호출 한도 초과 (제공기관 응답 또는 자체 일일 카운터)
  api_http_error   HTTP 상태가 200 이 아님
  api_result_error HTTP 200 이지만 resultCode 가 정상이 아님 (예: 99)
  api_timeout      시간 초과
  network_error    연결 실패 등 네트워크 오류
  response_invalid 응답 형식 이상 (JSON 아님, 잘린 JSON, 헤더 없음, 스키마 불일치)
코드 표·키워드는 config/rules.yaml 의 api_errors (데이터). 판정은 이 모듈(코드).

기록(HEALTH)은 프로세스 메모리에만 두고 키·URL 을 담지 않는다. 사용자 상태가 아니므로 stateless 원칙과 무관.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from config.catalog import CAUSE_CODES, RULES

log = logging.getLogger("mcp.sources")
ERR = RULES["api_errors"]
CAUSES = tuple(c for c in CAUSE_CODES if not c.startswith("mcp_"))  # 외부 API 쪽 원인만
KST = timezone(timedelta(hours=9))
HEALTH: dict[str, dict] = {}


def classify(rec: dict, ok_codes: tuple[str, ...] = ("0", "00")) -> str | None:
    """capture.call 레코드 → 원인 코드. 정상이면 None."""
    m = rec["meta"]
    kind = m.get("error_kind")
    if kind == "key_missing":
        return "api_auth_error"
    if kind == "daily_limit":
        return "rate_limited"
    if kind == "timeout":
        return "api_timeout"
    if m.get("error"):
        return "network_error"
    code = m.get("api_result_code")
    text = " ".join(str(x) for x in (code, m.get("api_result_msg"), (rec.get("text") or "")[:2000]) if x)
    if code in ERR["limit_codes"] or any(w in text for w in ERR["limit_words"]):
        return "rate_limited"
    if code in ERR["auth_codes"] or any(w in text for w in ERR["auth_words"]) or m.get("http_status") in (401, 403):
        return "api_auth_error"
    if m.get("http_status") != 200:
        return "api_http_error"
    if not isinstance(rec.get("json"), dict) or code is None:
        return "response_invalid"
    if code not in ok_codes:
        return "api_result_error"
    return None


def detail(rec: dict, cause: str) -> str:
    """개발자용 상세 (키·URL 없음 — capture 가 이미 마스킹했고 URL 은 싣지 않는다)."""
    m = rec["meta"]
    parts = [f"HTTP {m.get('http_status')}"]
    if m.get("api_result_code") is not None:
        parts.append(f"resultCode={m['api_result_code']} {m.get('api_result_msg') or ''}".strip())
    if m.get("error"):
        parts.append(str(m["error"])[:120])
    return " ".join(parts)


def note(source: str, cause: str | None, info: str = "") -> None:
    """소스별 최근 성공·실패 기록 + 실패 로그 한 줄."""
    now = datetime.now(KST).isoformat(timespec="seconds")
    h = HEALTH.setdefault(source, {"last_ok_at": None, "last_fail_at": None, "last_cause": None, "fail_count": 0})
    if cause is None:
        h["last_ok_at"] = now
        return
    h.update(last_fail_at=now, last_cause=cause, fail_count=h["fail_count"] + 1)
    log.warning("source=%s cause=%s %s", source, cause, info)


def snapshot() -> dict:
    return {k: dict(v) for k, v in HEALTH.items()}
