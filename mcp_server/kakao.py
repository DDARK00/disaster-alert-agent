"""Kakao Local 라이브 조회 (ADR-011). 응답은 어떤 형태로도 저장하지 않는다 — fixture·로그·파일 없음.

요청마다 호출해 행정구역 코드·장소 ID 만 꺼내고 원문은 버린다. 장소명·좌표·주소는 반환하지 않는다.
링크 형식은 카카오맵 공식 가이드(apis.map.kakao.com/web/guide, URL 바로가기):
  지도   https://map.kakao.com/link/map/{장소ID}
  길찾기 https://map.kakao.com/link/to/{장소ID}
경로는 계산·판단하지 않는다. 링크로 넘기기까지만.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from capture import load_env, scrub

KEY_ENV = "KAKAO_REST_API_KEY"
BASE = "https://dapi.kakao.com/v2/local"
MAP_LINK = "https://map.kakao.com/link/map/{id}"
ROUTE_LINK = "https://map.kakao.com/link/to/{id}"
TIMEOUT = 5


def _get(path: str, params: dict, key: str, opener=urllib.request.urlopen) -> dict:
    req = urllib.request.Request(f"{BASE}/{path}?{urllib.parse.urlencode(params)}",
                                 headers={"Authorization": f"KakaoAK {key}"})
    with opener(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def lookup(query: str, opener=urllib.request.urlopen, key: str | None = None) -> dict:
    """키워드 검색 1위 장소 → 좌표로 법정동 코드. 반환: status, place_id, b_code, region_names, result_count.

    status: ok | not_found | failed | unavailable(키 없음). 실패는 '장소 정보 없음'일 뿐 안전 판단과 무관.
    """
    key = key if key is not None else load_env().get(KEY_ENV, "")
    if not key:
        return {"status": "unavailable", "detail": f"{KEY_ENV} 없음"}
    try:
        kw = _get("search/keyword.json", {"query": query, "size": 1}, key, opener)
        docs = kw.get("documents") or []
        count = (kw.get("meta") or {}).get("total_count", len(docs))
        if not docs:
            return {"status": "not_found", "result_count": 0}
        top = docs[0]
        if not str(top.get("id", "")).isdigit():  # 링크에 들어가므로 숫자 ID 만 허용
            return {"status": "failed", "detail": "장소 ID 형식 오류"}
        reg = _get("geo/coord2regioncode.json", {"x": top["x"], "y": top["y"]}, key, opener)
        b = next((d for d in reg.get("documents") or [] if d.get("region_type") == "B"), None)
        return {"status": "ok", "place_id": str(top["id"]), "result_count": count,
                "b_code": b["code"] if b else None,
                "region_names": [b.get("region_1depth_name", ""), b.get("region_2depth_name", "")] if b else []}
    except urllib.error.HTTPError as e:
        return {"status": "failed", "detail": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, StopIteration) as e:
        return {"status": "failed", "detail": scrub(f"{type(e).__name__}", key)}


def links(place_id: str) -> dict:
    return {"map": MAP_LINK.format(id=place_id), "route": ROUTE_LINK.format(id=place_id)}
