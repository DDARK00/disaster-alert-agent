"""외부 API 호출 래퍼 — 모든 응답을 fixture로 저장하고 호출 횟수를 센다.

원칙
- 서비스키는 화면·로그·저장 파일 어디에도 남기지 않는다 (URL, 응답 본문, 예외 메시지 모두 마스킹).
- 모든 호출은 data/fixtures/<source>/ 에 JSON 한 개로 저장된다 (메타 + 본문).
- 호출 횟수는 data/fixtures/_call_counter.json 에 일자별로 누적한다 (쿼터 낭비 방지).
- 의존성 없음(표준 라이브러리만).

사용:  python3 mcp_server/capture.py <probe> [<probe> ...]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "data" / "fixtures"
COUNTER = FIXTURES / "_call_counter.json"
MASK = "***"

# 소스별 설정: env 키 이름 / 베이스 URL / 서비스키 파라미터 이름
SOURCES = {
    "weather": {
        "env": "WEATHER_ALERTS_SERVICE_KEY",
        "base": "http://apis.data.go.kr/1360000/WthrWrnInfoService",
    },
    "dssp": {
        "env": "DSSP_SERVICE_KEY",
        "base": "https://www.safetydata.go.kr/V2/api/DSSP-IF-00247",
    },
    "doing": {
        "env": "DSSP_DOING_SERVICE_KEY",
        "base": "https://www.safetydata.go.kr/V2/api/DSSP-IF-20588",
    },
}


def load_env(path: Path = ROOT / ".env") -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.split("  #")[0].strip().strip("\"'")
    return env


def _key_variants(key: str) -> set[str]:
    """마스킹 대상: 원본, 디코딩본, 인코딩본."""
    dec = urllib.parse.unquote(key)
    return {key, dec, urllib.parse.quote(dec, safe=""), urllib.parse.quote_plus(dec)} - {""}


def scrub(text: str, key: str) -> str:
    for v in _key_variants(key):
        text = text.replace(v, MASK)
    return text


def _count(source: str) -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    data = json.loads(COUNTER.read_text()) if COUNTER.exists() else {}
    today = datetime.now().strftime("%Y-%m-%d")
    day = data.setdefault(today, {})
    day[source] = day.get(source, 0) + 1
    COUNTER.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return day[source]


def call(source: str, operation: str, params: dict, tag: str, key_param: str = "serviceKey",
         timeout: int = 20) -> dict:
    """한 번 호출하고 fixture로 저장한다. 저장된 dict를 돌려준다."""
    cfg = SOURCES[source]
    key = load_env()[cfg["env"]]
    # 이미 인코딩된 키(%포함)가 이중 인코딩되지 않도록 한 번 디코딩 후 인코딩한다.
    query = urllib.parse.urlencode({key_param: urllib.parse.unquote(key), **params})
    url = cfg["base"] + (f"/{operation}" if operation else "") + "?" + query
    n = _count(source)

    status, body, err = None, None, None
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mini-project-capture/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:  # 4xx/5xx 도 본문을 보존한다
        status, raw = e.code, e.read()
    except Exception as e:  # noqa: BLE001
        raw, err = b"", scrub(f"{type(e).__name__}: {e}", key)
    elapsed = int((time.time() - t0) * 1000)

    text = scrub(raw.decode("utf-8", errors="replace"), key)
    try:
        body = json.loads(text)
    except ValueError:
        body = None

    record = {
        "meta": {
            "source": source,
            "operation": operation,
            "mode": "live",
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "http_status": status,
            "elapsed_ms": elapsed,
            "call_no_today": n,
            "url": scrub(url, key),
            "error": err,
        },
        "json": body,
        "text": None if body is not None else text[:20000],
    }
    out = FIXTURES / source
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out / f"{tag}__{stamp}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    record["meta"]["saved_to"] = str(path.relative_to(ROOT))
    return record


def summarize(rec: dict, max_chars: int = 1800) -> str:
    m = rec["meta"]
    head = f"[{m['source']}/{m['operation'] or '-'}] http={m['http_status']} {m['elapsed_ms']}ms call#{m['call_no_today']} -> {m['saved_to']}"
    if m["error"]:
        return head + f"\n  error: {m['error']}"
    body = rec["json"] if rec["json"] is not None else rec["text"]
    s = json.dumps(body, ensure_ascii=False)[:max_chars] if rec["json"] is not None else (body or "")[:max_chars]
    return head + "\n  " + s


# ---- 프로브 정의 (최소 호출로 응답 모양 확인) ----
TODAY = datetime.now().strftime("%Y%m%d")

PROBES = {
    "weather_status": lambda: call("weather", "getPwnStatus",
                                   {"numOfRows": 3, "pageNo": 1, "dataType": "JSON"}, "getPwnStatus"),
    "weather_pwncd": lambda: call("weather", "getPwnCd",
                                  {"numOfRows": 30, "pageNo": 1, "dataType": "JSON"}, "getPwnCd_today"),
    "dssp": lambda: call("dssp", "",
                         {"numOfRows": 5, "pageNo": 1, "returnType": "json", "crtDt": TODAY,
                          "rgnNm": "광주광역시"}, "latest_gwangju"),
    "doing": lambda: call("doing", "",
                          {"numOfRows": 5, "pageNo": 1, "returnType": "json"}, "first_page"),
}

if __name__ == "__main__":
    names = sys.argv[1:] or list(PROBES)
    for name in names:
        print(summarize(PROBES[name]()))
        print()
