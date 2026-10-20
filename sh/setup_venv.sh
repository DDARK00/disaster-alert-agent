#!/usr/bin/env bash
# 로컬 개발용 가상환경 (도커를 쓰지 않을 때)
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r backend/requirements.txt
echo "OK: .venv 준비됨"
