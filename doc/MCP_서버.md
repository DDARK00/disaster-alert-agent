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
| `get_shelters` | Tool | `region_id`, `hazard`, `mode?` | 공식 목록(통합대피소)에 있는 쉼터·대피장소 — 폭염(무더위쉼터)·한파(한파쉼터)·지진(옥외대피장소)만. 그 밖의 유형은 `not_applicable`. status ok/failed(cause)/no_range/no_coverage. 이름·도로명 주소·관리번호만(좌표 없음), 상한 `rules.yaml shelters.limit`, 가까운 순 아님 |
| `list_hazards` | Tool | – | 유형과 행동요령 유무, 쉼터 구분(`shelter`) |
| `source_health` | Tool | – | 소스별 최근 live 호출 성공·실패 시각, 원인 코드, 실패 횟수 (키·URL 없음, 프로세스 메모리) |
| `guide://action/{hazard}` | Resource | – | 행동요령 전문 + 출처 |
| `disaster_briefing_prompt` | Prompt | `region`, `hazards`, `question` | 답변 작성 규칙(안전 문구 규칙) |

## 안전 규칙이 코드에 들어간 곳
- 특보 조회 실패·replay 범위 밖 → `source.status != ok`, 문구는 "정보 없음 … 위험 여부를 알 수 없으니 공식 채널에서 확인". (`test_out_of_coverage_is_not_reported_as_no_risk`)
- live 실패는 `source.cause`(원인 코드)와 원인별 문구(`causes.*`)를 싣는다. 분류는 `failures.py`(코드 표·키워드는 rules.yaml `api_errors`), ADR-014. 키 미설정이면 호출하지 않고 `api_auth_error`.
- 위험이 없을 때 문구는 "확인된 위험 정보 없음 (기준 시각, 조회 구역) … 공식 채널도 함께 확인해 주세요"(ADR-016). "안전합니다"는 쓰지 않는다.
- 행동요령이 없는 유형(건조)은 다른 재난으로 대체하지 않는다. 예외는 `hazard_map.json`의 `guide_from`으로 정한 같은 계열뿐(열대야 → 폭염, 응답에 `borrowed_from`, 답변에 그 사실을 밝힘, ADR-020). 열대야는 무더위쉼터(`shelter: heat`)도 쓴다.
- 모든 특보 응답에 `disclaimer`(공식 경보 아님) 포함.

## 데이터 소스 (ADR-013, ADR-015)
- `data/sources.yaml`: 소스별 엔드포인트·키 이름(새/옛)·이용조건·출처·일일 한도·유형 코드·캐시·상태. `registry.py`가 기동 시 pydantic으로 검사(상태 값, `*_SERVICE_KEY` 이름 규칙, guide 소스의 쪽 크기·5자리 유형 코드, hazard_map `source`·`action_cate2`와의 일치). 어기면 서버가 시작하지 않는다.
- `guides.py`: 행동요령 4종 공통 어댑터. 쪽 넘기기(totalCount + 꽉 찬 쪽이면 한 쪽 더), live 일 1회 캐시·fixture 폴백, 정규화(제목·빈 행 제외, 앞 기호 제거, `Q : … A : …` → 한 줄), 대상 필터(rules.yaml `guide_audience`). fixture 갱신: `.venv/bin/python mcp_server/guides.py capture [소스]` (실제 호출 ~20건).
- `schemas.py`: 외부 응답 pydantic 모델(봉투·행동요령·대피소·재난문자·기상특보). live 응답 검증 실패 → `response_invalid`.
- fixture: `data/fixtures/{doing,guide_social,guide_life,guide_civil_defense}/cate_<코드>_p<n>__<시각>.json` (2026-10-20 수집, 커밋). 예전 단일 파일(`doing/cate_<코드>__…`)은 지우지 않았고 쪽 단위 파일이 있으면 그것을 쓴다(T-031).

## 지역 해석 (`regions.py`, ADR-017)
- 데이터: `data/regions.json` = 손으로 쓴 광주 두 곳 + 특보구역표에서 생성한 전국 지역(광역시 7·시군 156). 생성: `.venv/bin/python mcp_server/tools/gen_regions.py` (입력 `data/kma_zones.json`, `data/region_build.yaml`; 구역표 xlsx에서 중간 JSON을 다시 뽑을 때는 `mcp_server/tools/extract_kma_zones.py <xlsx>`).
- 별칭은 긴 것부터, 겹치는 짧은 별칭은 무시. 정식 이름·`시도 + 이름`은 강한 별칭(확정), 줄임말은 약한 별칭(가정), 일상어와 겹치는 줄임말은 제외.
- `status`: resolved / assumed / ambiguous / none. `ambiguous`에 `ask=True`면 되묻기(동명 고성군, 여러 광역시의 같은 구 이름). '광주' 묶음(`show_both_groups`)만 둘 다 조회. `none`에 `province`가 있으면 도 이름만 온 것 → 시·군 되묻기(도 전체는 조회하지 않음).
- `districts`: 광역시 구 이름으로 정해진 지역 → 답변에 "시 전체 기준" 표준 문구.
- 재난문자 매칭은 시도 허용 목록(신구 명칭) × 시군 이름(`dssp_prefixes`), `○○ 전체`·시도 이름만 온 문자는 `sido_wide`. 카카오 행정구역 → 지역은 같은 목록(`kakao_prefixes`)에서 가장 긴 접두어.

## 쉼터·대피장소 (`shelters.py`)
- 재난 유형 → 구분은 `hazard_map.json`의 `shelter`(heat·cold·quake, 코드가 정함). 구분 → 요청 코드·이름 필터·상한·정렬·캐시 시간은 `config/rules.yaml` `shelters`(데이터).
- 구분 코드와 이름이 1:1이 아니다(코드 1에도 "한파쉼터,무더위쉼터"가 섞임) → 폭염은 이름에 "무더위", 한파는 "한파"가 있어야 채택. 지진은 구분 3·4.
- 지역 범위: `regions.json` `shelter_area.bbox`(위경도 사각형)로 요청, 사각형에 섞이는 이웃 시군 시설은 `shelter_area.addr_prefixes`(도로명 주소 앞부분, 광주는 `전남광주통합특별시`·`광주광역시` 모두)로 거른다. 범위 데이터가 없는 지역은 `no_range` — 실패가 아니므로 답변에 "정보 없음"을 붙이지 않고 쉼터 없이 답한다.
- 정렬: 사용자 좌표를 받지 않으므로 **가까운 순이 아니다**. 필터 뒤 주소·이름·관리번호 순으로 정렬해 상한(3)개. 같은 응답이면 같은 결과(응답 순서와 무관, 테스트로 고정). 주소 필터 뒤 상한이 안 차면 다음 쪽(최대 `max_pages`).
- live: (지역, 구분) 단위 캐시 `cache_hours`(6시간, `sources.yaml cache: region_hours`), 실패하면 커밋된 fixture로 폴백(`origin=fixture_fallback`, `fallback_cause`). 저장본도 없으면 `failed`+원인 코드. 응답은 pydantic(`ShelterRow`)으로 검증, 형식 이상은 `response_invalid`. 호출 수·일일 한도는 capture 카운터.
- replay: 커밋된 fixture `data/fixtures/shelter/<지역>_cd<코드>_p<n>__<시각>.json`(2026-10-20 수집). 쉼터 목록은 시각에 따라 접지 않으므로 replay 기준 시각과 관계없이 같은 저장본을 쓴다. 갱신: `.venv/bin/python mcp_server/shelters.py capture [지역 ID]`(지역당 약 6건).
- 운영 여부·운영 시간 필드는 응답에 없다. 백엔드는 `[S#]` 근거와 고정 제목 문구(`messages.yaml shelters.header`, "운영 여부·운영 시간은 이 목록에 없어 확인되지 않았습니다")로만 안내하고, 이름·주소로 카카오맵 **검색 링크**(`https://map.kakao.com/link/search/…`)를 카드에 칩으로 붙인다(Kakao API 호출·응답 저장 없음, 경로 판단 없음).

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
`sh/test.sh` — MCP 쪽: 상태 접기, 지역 해석(전국 해석 표·신구 표기·생성본 최신 여부), 서버 통합, capture, 장애 원인, 행동요령 어댑터, 쉼터 어댑터(주소 필터·이름 필터·상한·결정성·캐시·폴백), Kakao mock. Kakao 실호출 스모크 2건은 선택 실행이라 기본 skip. 컨테이너 실행은 `MCP_TRANSPORT=http` (`doc/도커.md`).
