#!/usr/bin/env bash
# 로컬 개발용 가상환경 (도커를 쓰지 않을 때)
set -euo pipefail
cd "$(dirname "$0")/.."
# 다른 머신에서 만든 .venv는 인터프리터 경로가 달라 깨진다 (T-018) → 실행 안 되면 다시 만든다
if [ -d .venv ] && ! .venv/bin/pip --version >/dev/null 2>&1; then
  echo "깨진 .venv 감지 → 재생성"; rm -rf .venv
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r backend/requirements.txt
echo "OK: .venv 준비됨"
