# API 명세 확인 (doc/api_spec 기준)

> 확인일 2026-10-20. 명세 문서만 읽은 결과이며, **실제 호출 응답은 아직 확인 전**이다. 호출로 확인된 항목은 확인 후 `확인됨`으로 바꾼다.
> 키 이름: `DSSP_SERVICE_KEY`(재난문자) · `DSSP_DOING_SERVICE_KEY`(행동요령) · `WEATHER_ALERTS_SERVICE_KEY`(기상특보)

## 1. 기상특보 조회서비스 (`WEATHER_ALERTS_SERVICE_KEY`)

- 베이스: `http://apis.data.go.kr/1360000/WthrWrnInfoService` (명세상 **http**, 전송 암호화 "없음")
- 인증: `serviceKey`(URL 인코딩). 응답: XML 기본, `dataType=JSON` 지정 가능. 호출당 평균 100~150ms, 초당 30tps.
- 오퍼레이션 10개. 본 프로젝트에서 쓸 것:

| 오퍼레이션 | 용도 | 핵심 파라미터 |
|---|---|---|
| `getPwnCd` | **구역·종류별 특보 조회 (구조화)** — 주력 | `areaCode`, `warningType`, `fromTmFc`, `toTmFc` |
| `getPwnStatus` | 최근 7일 특보현황 (**자유 텍스트** `t6`, `t7`) | 페이징만 |
| `getWthrWrnList` / `getWthrWrnMsg` | 특보 목록 / 통보문 | `stnId`, `fromTmFc`, `toTmFc` |
| 그 외 | 기상정보·속보·예비특보 | MVP 제외 |

### 코드표 (확정 근거: 명세)

| `warnVar`/`warningType` | 종류 | 비고 |
|---|---|---|
| 1 | 강풍 | |
| 2 | 호우 | |
| 3 | 한파 | |
| 4 | 건조 | |
| 5 | 폭풍해일 | |
| 6 | 풍랑 | |
| 7 | 태풍 | |
| 8 | 대설 | |
| 9 | 황사 | |
| 12 | 폭염 | |
| 13 | 열대야 | 2026-06-01 신설 |

(10, 11은 비어 있음. 안개·지진해일은 이 API에 없음.)

- `warnStress`: 0 주의보 / 1 경보 / 2 중대경보
- `command`: 1 발표 / 2 해제 / 3 연장 / 6 정정 / 7 변경발표 / 8 변경해제
- `cancel`: 0 정상 / 1 취소된 특보
- 응답 필드(`getPwnCd`): `stnId, tmFc, tmSeq, areaCode, areaName, warnVar, warnStress, command, startTime, endTime, allEndTime, cancel`
- 시각 형식: `YYYYMMDDHHmm` 문자열

### 구역코드 xlsx (`...특보구역코드안내(260601).xlsx`)

- 두 시트: Sheet1(areaCode, 구역명, 시작일, 종료일), Sheet2(REG_ID, REG_SP 레벨, **REG_UP 상위 코드**, 약칭, 정식명). 약 413행.
- **광주 중복이 코드로 구분된다.**

| areaCode | 이름 | 상위 |
|---|---|---|
| `L1013400` | 광주시 | 경기도 `L1010000` |
| `L1130000` | 광주광역시 | 전국 |
| `L1130100` | 광주광역시 (2026-05-31 신설) | `L1130000` |
| `L1130110` / `L1130120` | 광주서부 / 광주동부 (2026-05-31 신설) | `L1130100` |

- 2026-05-31 개편으로 시작일이 2026-05-31인 구역이 있다. 과거 replay 데이터와 구역 체계가 다를 수 있다 → 시점별 구역 매핑 필요.
- `stnId`(발표 관서): 109 서울(서울·인천·경기), 156 광주(광주·전남) 등.

## 2. 재난문자 (`DSSP_SERVICE_KEY`)

- 경로: `/V2/api/DSSP-IF-00247` (**호스트는 명세에 없음** — 이용가이드에서 확인 필요)
- 요청: `serviceKey, numOfRows, pageNo, returnType=json, crtDt(YYYYMMDD 조회 시작일), rgnNm(시도, 시군구명)`
- 응답 래퍼: `header{resultCode:"00", resultMsg, errorMsg}`, `numOfRows, pageNo, totalCount`, `body[]`
- 필드: `SN, CRT_DT("YYYY/MM/DD HH:mm:ss"), MSG_CN, RCPTN_RGN_NM, RCPTN_RGN_ID, EMRG_STEP_NM/ID, DST_SE_NM/ID, REG_YMD, MDFCN_YMD`
- 긴급단계: 긴급재난 / 안전안내 / 위급재난
- 재해구분(35종): 호우·강풍·폭염·풍랑·한파·태풍·대설·건조·황사·안개·지진해일 등 → **hazard 매핑 대상**
- 샘플(`dssp_example.json`)은 2023-09-19의 실종·교통통제 문자 10건뿐이라 호우/폭염 사례는 **아직 없음**. `totalCount` 63,488.

## 3. 자연재난 국민행동요령 (`DSSP_DOING_SERVICE_KEY`)

- 경로: `/V2/api/DSSP-IF-20588` (호스트 미기재), 갱신주기 1일, 데이터 갱신일 2026-10-17
- 요청: `serviceKey, numOfRows, pageNo, returnType, safety_cate(카테고리 코드)`
- 응답 필드: `actRmks`(문장 1개), `contentsUrl`, `safety_cate1~4`, `safety_cate_nm1~3`
- 구조(샘플): `자연재난(01) > 태풍(01001) > 예보시/특보 중/이후(01001001/002/003)`. **행(row) 하나가 행동요령 문장 하나.**
- 재난유형 목록에 "풍수해(태풍, 호우, 대설)"이 한 묶음으로 있고, **강풍·풍랑·건조는 독립 항목이 보이지 않는다.**
- 샘플의 `totalCount`는 61, `contentsUrl`은 null.

## 4. 이 확인으로 정해진 것 / 새로 생긴 과제

정해짐
1. hazard enum 후보 = 기상특보 11종(위 코드표). 재난문자의 재해구분과 이름이 대부분 겹친다.
2. 광주 모호성은 구역코드 두 개(`L1013400` / `L1130000`)로 결정론적으로 구분 가능.
3. 행동요령은 **특보 단계(예보시 / 특보 중 / 이후)** 와 대응시킬 수 있다 → 판정 결과에 따라 어느 단계 문장을 보여줄지 코드로 선택.

새 과제
1. 행동요령에 강풍·풍랑·건조가 있는지 → 전체 카테고리 목록 호출로 확인. 없으면 태풍/풍수해로 폴백하는 매핑 규칙 필요.
2. 재난문자·행동요령 API의 **호스트 주소와 JSON 응답의 실제 모양** 확인.
3. 기상특보 JSON의 실제 키 구조(`response.body.items.item`) 확인. XML 예제는 `resultCode`가 `0`, 명세 표는 `00`이므로 둘 다 정상으로 처리.
4. 재난문자에서 호우·폭염·강풍 사례가 실제로 조회되는지(최근/과거 구간).
5. 키는 모두 URL 인코딩 이슈가 있을 수 있음 (트러블슈팅 문서 참고).

## 5. 실제 호출 결과 (2026-10-20, `mcp_server/capture.py`)

| 항목 | 결과 |
|---|---|
| 기상특보 | `http://apis.data.go.kr/1360000/WthrWrnInfoService/getPwnCd` + `dataType=JSON` 성공, 55~80ms. 응답 `response.header/body.items.item[]`. 오늘은 풍랑 해제 2건뿐 |
| 재난문자 | 호스트 `https://www.safetydata.go.kr/V2/api/DSSP-IF-00247` 성공. 최신 데이터 2026-10-19까지. `crtDt` 이후 구간(`crtDt=20261001` → 384건, `20250101` → 40,731건, `20230101` → 63,489건) |
| 행동요령 | 호스트 `https://www.safetydata.go.kr/V2/api/DSSP-IF-20588` 성공. 카테고리 지정 필요 (T-012) |

- 재난문자 10월 분포: 기타 159, 강풍 68, 호우 53, 화재 43, 산불 20 … (라벨 신뢰도는 T-010)
- 행동요령 카테고리 → `data/hazard_map.json`: 태풍 01001(61행), 홍수 01002(34), 호우 01003(69), 강풍 01004(28), 대설 01005(66), 한파 01006(56), 풍랑 01007(11), 황사 01008(40), 폭염 01009(60), 가뭄 01010(49), 지진 01011(110), 지진해일 01012(12), 해일 01013(56), 산사태 01014(15), 화산폭발 01015(18). 01016 이상은 비어 있음.
- 기상특보 ↔ 행동요령 대응에서 **건조(4), 열대야(13)만 행동요령 없음**. 폭풍해일(5)은 해일(01013)로 연결.
- 호출 횟수(오늘): weather 2 / dssp 5 / doing 34. 저장 위치 `data/fixtures/<source>/`.
