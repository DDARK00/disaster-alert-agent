# MCP 서버 (`mcp_server/`)

결정론 도구만 제공한다(LLM 미사용). 실행: `.venv/bin/python mcp_server/server.py` (stdio). 환경: `DATA_MODE=replay`(기본) | `live`.

## 도구
| 이름 | 종류 | 입력 | 요점 |
|---|---|---|---|
| `resolve_location` | Tool | `text`, `history_region_id?`, `browser_region_id?` | status resolved/assumed/ambiguous/none, 후보와 근거 |
| `get_alert_status` | Tool | `region_id`, `as_of?`, `mode?` | 발효(alerts)·예고(pending)·최근 해제. 소스 상태와 문구 포함 |
| `get_disaster_messages` | Tool | `region_id`, `as_of?`, `lookback_hours`, `hazards?` | 지역 적용 문자. scope direct/sido_wide |
| `search_action_guide` | Tool | `hazard`, `phase?` | 행동요령 행(ID 포함). 없는 유형은 available=False |
| `list_hazards` | Tool | – | 유형과 행동요령 유무 |
| `guide://action/{hazard}` | Resource | – | 행동요령 전문 + 출처 |
| `disaster_briefing_prompt` | Prompt | `region`, `hazards`, `question` | 답변 작성 규칙(안전 문구 규칙) |

## 안전 규칙이 코드에 들어간 곳
- 특보 조회 실패·replay 범위 밖 → `source.status != ok`, 문구는 "정보 없음 … 안전하다는 뜻이 아닙니다". (`test_out_of_coverage_is_not_reported_as_no_risk`)
- 위험이 없을 때 문구는 "확인된 위험 정보 없음 (기준 시각, 조회 구역) … 안전을 보장하지 않습니다". "안전합니다"는 쓰지 않는다.
- 행동요령이 없는 유형(건조·열대야)은 다른 재난으로 대체하지 않는다.
- 모든 특보 응답에 `disclaimer`(공식 경보 아님) 포함.

## replay 범위 (2026-10-20 수집분)
- 기상특보: 2026-08-21 00:00 ~ 2026-10-20 10:18 (구역별, 이어 붙인 fixture 기준). 이보다 과거는 API 제약(60일)으로 재수집 불가.
- 재난문자: 2026-10-01 ~ 2026-10-20 10:18.
- replay에서 `as_of`를 생략하면 2026-10-19 23:59.

## 테스트
`sh/test.sh` (MCP 31건: 상태 접기 11, 지역 7, 서버 통합 9, capture 4). 컨테이너 실행은 `MCP_TRANSPORT=http` (`doc/도커.md`).
