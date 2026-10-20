# MCP 서버 (`mcp_server/`)

결정론 도구만 제공한다(LLM 미사용). 실행: `.venv/bin/python mcp_server/server.py` (stdio). 환경: `DATA_MODE=replay`(기본) | `live`.

## 도구
| 이름 | 종류 | 입력 | 요점 |
|---|---|---|---|
| `resolve_location` | Tool | `text`, `history_region_id?`, `browser_region_id?` | status resolved/assumed/ambiguous/none, 후보와 근거 |
| `resolve_place` | Tool | `query` | Kakao Local 라이브 조회(저장 없음, ADR-011). status ok/not_found/failed/unavailable, 지원 지역이면 `region_id`, 법정동 코드, 장소 ID, 링크(`/link/map/{id}`, `/link/to/{id}`), 고정 안내 문구. 장소명·좌표는 반환하지 않음 |
| `get_alert_status` | Tool | `region_id`, `as_of?`, `mode?` | 발효(alerts)·예고(pending)·최근 해제. 소스 상태와 문구 포함 |
| `get_disaster_messages` | Tool | `region_id`, `as_of?`, `lookback_hours`, `hazards?` | 지역 적용 문자. scope direct/sido_wide |
| `search_action_guide` | Tool | `hazard`, `phase?`, `mode?` | 행동요령 행(ID 포함). 소스는 hazard_map `source`로 코드가 정함(자연·사회·생활안전·민방위). `source`·`source_label`·`origin`(fixture/live/live_cache/fixture_fallback)·`excluded_non_citizen`. 없는 유형은 available=False |
| `list_hazards` | Tool | – | 유형과 행동요령 유무 |
| `source_health` | Tool | – | 소스별 최근 live 호출 성공·실패 시각, 원인 코드, 실패 횟수 (키·URL 없음, 프로세스 메모리) |
| `guide://action/{hazard}` | Resource | – | 행동요령 전문 + 출처 |
| `disaster_briefing_prompt` | Prompt | `region`, `hazards`, `question` | 답변 작성 규칙(안전 문구 규칙) |

## 안전 규칙이 코드에 들어간 곳
- 특보 조회 실패·replay 범위 밖 → `source.status != ok`, 문구는 "정보 없음 … 안전하다는 뜻이 아닙니다". (`test_out_of_coverage_is_not_reported_as_no_risk`)
- live 실패는 `source.cause`(원인 코드)와 원인별 문구(`causes.*`)를 싣는다. 분류는 `failures.py`(코드 표·키워드는 rules.yaml `api_errors`), ADR-014. 키 미설정이면 호출하지 않고 `api_auth_error`.
- 위험이 없을 때 문구는 "확인된 위험 정보 없음 (기준 시각, 조회 구역) … 안전을 보장하지 않습니다". "안전합니다"는 쓰지 않는다.
- 행동요령이 없는 유형(건조·열대야)은 다른 재난으로 대체하지 않는다.
- 모든 특보 응답에 `disclaimer`(공식 경보 아님) 포함.

## 데이터 소스 (ADR-013, ADR-015)
- `data/sources.yaml`: 소스별 엔드포인트·키 이름(새/옛)·이용조건·출처·일일 한도·유형 코드·캐시·상태. `registry.py`가 기동 시 pydantic으로 검사(상태 값, `*_SERVICE_KEY` 이름 규칙, guide 소스의 쪽 크기·5자리 유형 코드, hazard_map `source`·`action_cate2`와의 일치). 어기면 서버가 시작하지 않는다.
- `guides.py`: 행동요령 4종 공통 어댑터. 쪽 넘기기(totalCount + 꽉 찬 쪽이면 한 쪽 더), live 일 1회 캐시·fixture 폴백, 정규화(제목·빈 행 제외, 앞 기호 제거, `Q : … A : …` → 한 줄), 대상 필터(rules.yaml `guide_audience`). fixture 갱신: `.venv/bin/python mcp_server/guides.py capture [소스]` (실제 호출 ~20건).
- `schemas.py`: 외부 응답 pydantic 모델(봉투·행동요령·대피소·재난문자·기상특보). live 응답 검증 실패 → `response_invalid`.
- fixture: `data/fixtures/{doing,guide_social,guide_life,guide_civil_defense}/cate_<코드>_p<n>__<시각>.json` (2026-10-20 수집, 커밋). 예전 단일 파일(`doing/cate_<코드>__…`)은 지우지 않았고 쪽 단위 파일이 있으면 그것을 쓴다(T-031).

## 문구·설정의 위치 (ADR-012)
- `statement` 문구와 면책은 `prompts/messages.yaml`(`mcp.*`, `disclaimer`), 브리핑 프롬프트 원문은 `prompts/briefing.md`.
- 환경변수는 `config/settings.py`(`DATA_MODE`, `MCP_TRANSPORT/HOST/PORT/PATH`). 서비스 키는 Settings 밖 — `capture.py`만 읽는다.
- 컨테이너는 `config/`, `prompts/`를 함께 복사한다.

## Kakao (ADR-011)
- 키 `KAKAO_REST_API_KEY`는 mcp에서만 읽는다(`capture.load_env`). 헤더 `Authorization: KakaoAK …`로만 보내고 URL·로그에 남기지 않는다.
- 키워드 검색 1위 → `coord2regioncode`(법정동). 응답 원문은 메모리에서 요약 후 버린다 — fixture·파일·로그 없음(테스트로 확인).
- 행정구역 매핑은 `regions.json`의 `kakao_prefixes`(시도+시군구 이름). 광주권은 `전남광주통합특별시 ○구`로 옴(T-025).
- 테스트는 mock(`tests/test_kakao.py`). 실호출 스모크는 선택: `KAKAO_SMOKE=1 .venv/bin/python -m unittest mcp_server/tests/test_kakao_smoke.py`.

## replay 범위 (2026-10-20 수집분)
- 기상특보: 2026-08-21 00:00 ~ 2026-10-20 10:18 (구역별, 이어 붙인 fixture 기준). 이보다 과거는 API 제약(60일)으로 재수집 불가.
- 재난문자: 2026-10-01 ~ 2026-10-20 10:18.
- replay에서 `as_of`를 생략하면 2026-10-19 23:59.
- replay는 커밋된 fixture만 읽는다. live 캡처(`*_live__*`, gitignore)는 섞지 않는다(T-023).

## 테스트
`sh/test.sh` (MCP 41건 중 2건은 선택 스모크로 skip: 상태 접기 11, 지역 7, 서버 통합 10, capture 4, Kakao mock 7). 컨테이너 실행은 `MCP_TRANSPORT=http` (`doc/도커.md`).
