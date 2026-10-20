"""데이터 소스 레지스트리 로더 (data/sources.yaml, ADR-013). import 시점(기동 시)에 형식을 검사한다.

검사: 필수 필드·타입(pydantic), 상태 값, 환경변수 이름 규칙(*_SERVICE_KEY), guide 소스의 쪽 크기·유형 코드(5자리),
hazard_map.json 의 source 가 레지스트리에 있는 active guide 소스인지. 어기면 RuntimeError 로 시작하지 않는다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "data" / "sources.yaml"
HAZARD_MAP = ROOT / "data" / "hazard_map.json"
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*_SERVICE_KEY$")


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    provider: str
    portal_id: str | None
    api_id: str
    base: str
    env: str
    env_legacy: str | None
    license: str
    attribution: str
    daily_limit: int | None
    kind: Literal["alerts", "messages", "guide", "shelter"]
    page_size: int | None = None
    categories: dict[str, str] | None = None
    cache: Literal["request", "daily", "region_hours"]
    fixture_dir: str
    status: Literal["active", "evaluating", "blocked"]

    @field_validator("env", "env_legacy")
    @classmethod
    def _env(cls, v):
        if v is not None and not ENV_NAME.match(v):
            raise ValueError(f"환경변수 이름 규칙(*_SERVICE_KEY) 위반: {v}")
        return v

    @field_validator("categories")
    @classmethod
    def _cats(cls, v):
        if v is not None:
            bad = [k for k in v if not re.fullmatch(r"\d{5}", k)]
            if bad:
                raise ValueError(f"유형 코드는 5자리: {bad}")
        return v


def _load() -> dict[str, Source]:
    try:
        raw = yaml.safe_load(PATH.read_text(encoding="utf-8"))["sources"]
        out = {name: Source(**v) for name, v in raw.items()}
    except (KeyError, TypeError, ValidationError) as e:
        raise RuntimeError(f"data/sources.yaml 형식 오류: {e}") from e
    errs = []
    for name, s in out.items():
        if s.kind == "guide" and (not s.page_size or not s.categories):
            errs.append(f"{name}: guide 소스는 page_size·categories 필요")
    hazards = json.loads(HAZARD_MAP.read_text(encoding="utf-8"))["hazards"]
    for hz, v in hazards.items():
        src, cate = v.get("source"), v.get("action_cate2")
        if cate is None:
            continue
        s = out.get(src)
        if s is None or s.kind != "guide":
            errs.append(f"hazard_map {hz}: source '{src}' 가 guide 소스가 아님")
        elif cate not in (s.categories or {}):
            errs.append(f"hazard_map {hz}: {cate} 가 {src}.categories 에 없음")
    if errs:
        raise RuntimeError("data/sources.yaml 불일치: " + "; ".join(errs))
    return out


SOURCES: dict[str, Source] = _load()


def get(name: str) -> Source:
    return SOURCES[name]


def usable(name: str) -> bool:
    """active 만 서비스 응답에 쓴다 (evaluating·blocked 는 사용 안 함, ADR-013)."""
    return SOURCES[name].status == "active"
