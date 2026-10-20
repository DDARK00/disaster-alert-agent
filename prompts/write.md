=== user ===
근거 데이터(JSON):
$facts

출력 형식: 지역마다 '### 지역명' 제목, 항목은 '- ' 로 시작하고 끝에 대괄호로 근거 ID를 붙인다.
근거 ID는 지역마다 따로 매겨져 있다. 각 지역 제목 아래에는 그 지역의 ID만 쓴다.
$region_ids
=== region_ids ===
* ### $region 아래 — 인용 가능: $ids. 이 목록에 없는 ID는 절대 쓰지 않는다.
=== must_cite ===
* ### $region 아래 — 특보 $alert_ids 는 하나도 빠뜨리지 말고 각각 '- ' 항목으로 인용한다.
=== ids_none ===
없음 — 어떤 [ID]도 쓰지 않는다
=== must_header ===
반드시 그대로 포함할 문장:
=== must ===
### $region 아래에 다음 문장을 그대로: $sentence
