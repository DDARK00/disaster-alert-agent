"""외부 API 응답 경계 검증 (pydantic). 초안: doc/API_응답_스키마.md §7.

검증 실패(ValidationError)는 원인 코드 response_invalid 로 이어진다 (ADR-014).
필드는 실제 응답(2026-10-20 호출)에서 관측한 것만 요구하고, 나머지는 무시한다(extra 허용 — 제공기관이 필드를 늘려도 깨지지 않게).
LangGraph State 는 TypedDict 로 둔다 (여기는 MCP 쪽 외부 경계만).
"""
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")


class Header(BaseModel):
    resultCode: str
    resultMsg: str | None = None
    errorMsg: str | None = None

    @field_validator("resultCode", mode="before")
    @classmethod
    def _code(cls, v):
        return str(v)


# ── safetydata (재난문자·행동요령·대피소) 공통 봉투 ──
class Envelope(BaseModel, Generic[T]):
    header: Header
    numOfRows: int | None = None
    pageNo: int | None = None
    totalCount: int = 0
    body: list[T] | None = None


class GuideRow(BaseModel):
    """행동요령 4종 공통 (자연·사회·생활안전·민방위)."""
    actRmks: str | None = None
    contentsUrl: str | None = None
    safety_cate1: str
    safety_cate_nm1: str | None = None
    safety_cate2: str
    safety_cate_nm2: str | None = None
    safety_cate3: str | None = None
    safety_cate_nm3: str | None = None
    safety_cate4: str | None = None


class ShelterRow(BaseModel):
    REARE_NM: str
    RONA_DADDR: str
    LAT: float
    LOT: float
    SHLT_SE_CD: str
    SHLT_SE_NM: str
    MNG_SN: str

    @field_validator("SHLT_SE_CD", "MNG_SN", mode="before")
    @classmethod
    def _s(cls, v):
        return str(v)


class MessageRow(BaseModel):
    """긴급재난문자 (DSSP-IF-00247) — sources._msg_row 가 쓰는 필드만."""
    SN: int
    CRT_DT: str = Field(pattern=r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$")
    MSG_CN: str
    RCPTN_RGN_NM: str
    DST_SE_NM: str | None = ""
    EMRG_STEP_NM: str | None = ""


# ── 기상청 (WthrWrnInfoService getPwnCd) ──
class WeatherItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    areaCode: str
    areaName: str | None = ""
    warnVar: int
    warnStress: int | None = 0
    command: str
    cancel: str | None = "0"
    tmFc: int | str
    tmSeq: int | str | None = 0
    startTime: int | str | None = None
    endTime: int | str | None = None

    @field_validator("areaCode", "command", "cancel", mode="before")
    @classmethod
    def _s(cls, v):
        return None if v is None else str(v)


class WeatherItems(BaseModel):
    item: list[WeatherItem] | WeatherItem | None = None


class WeatherBody(BaseModel):
    items: WeatherItems | str | None = None   # 결과 0건이면 빈 문자열로 오기도 한다
    totalCount: int | None = None


class WeatherResponse(BaseModel):
    header: Header
    body: WeatherBody | None = None


class WeatherEnvelope(BaseModel):
    response: WeatherResponse

    def items(self) -> list[dict]:
        b = self.response.body
        if b is None or not isinstance(b.items, WeatherItems) or b.items.item is None:
            return []
        it = b.items.item
        return [x.model_dump() for x in (it if isinstance(it, list) else [it])]
