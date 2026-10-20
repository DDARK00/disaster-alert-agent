import sys
from pathlib import Path

# 저장소 루트의 config/ 를 import 하기 위해 (backend/ 가 app-dir 이어도 동작)
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
