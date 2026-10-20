=== user ===
근거 데이터(JSON):
$facts

출력 형식: 지역마다 '### 지역명' 제목, 항목은 '- ' 로 시작하고 끝에 대괄호로 근거 ID를 붙인다.
인용 가능한 근거 ID: $ids. 이 목록에 없는 ID는 절대 쓰지 않는다.
=== ids_none ===
없음 — 어떤 [ID]도 쓰지 않는다
=== must_header ===
반드시 그대로 포함할 문장:
=== must_none ===
### $region 아래에 '확인된 위험 정보 없음 (기준 $as_of)' 문장
=== must_unknown ===
### $region 아래에 '정보 없음 — 안전하다는 뜻이 아닙니다.' 문장
=== must_gap ===
### $region 아래에 '- 정보 없음: $source ($status) — 안전하다는 뜻이 아닙니다.'
