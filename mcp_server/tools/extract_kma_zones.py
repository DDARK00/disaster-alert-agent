"""기상청 특보구역코드 xlsx(공식 활용가이드 첨부, 저장소 밖) → data/kma_zones.json (구역 표, 커밋 대상).

xlsx 는 doc/api_spec/(gitignore)에만 두고, 저장소에는 여기서 뽑은 중간 JSON 만 넣는다. 표준 라이브러리만 쓴다.
    .venv/bin/python mcp_server/tools/extract_kma_zones.py <특보구역코드안내.xlsx>
"""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "kma_zones.json"
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
COLS = ["REG_ID", "TM_ST", "TM_ED", "REG_SP", "REG_UP", "REG_KO", "REG_NAME"]


def rows(path: str, sheet: str) -> list[list[str]]:
    z = zipfile.ZipFile(path)
    shared = [("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
              for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS)]
    out = []
    for r in ET.fromstring(z.read(f"xl/worksheets/{sheet}.xml")).iter(f"{{{NS['m']}}}row"):
        vals = {}
        for c in r.findall("m:c", NS):
            col = re.match(r"[A-Z]+", c.get("r")).group(0)
            v = c.find("m:v", NS)
            if v is None:
                continue
            vals[col] = shared[int(v.text)] if c.get("t") == "s" else v.text
        out.append([vals.get(chr(ord("A") + i), "") for i in range(len(COLS))])
    return out


def main(path: str) -> None:
    data = rows(path, "sheet2")
    head, body = data[0], [r for r in data[1:] if r[0]]
    if [re.sub(r"[#\-\s]", "", h) for h in head] != COLS:  # 머리줄은 '# REG_ID', 'REG_KO----' 처럼 장식이 붙어 있다
        raise SystemExit(f"예상한 열이 아님: {head}")
    zones = [dict(zip(COLS, (str(v).strip() for v in r))) for r in body]
    OUT.write_text(json.dumps({"_note": "기상청 기상특보 조회서비스 활용가이드 첨부 '특보구역코드안내(260601)' Sheet2 에서 추출 "
                                        "(mcp_server/tools/extract_kma_zones.py). REG_UP=상위 구역, REG_NAME=표시 이름.",
                               "zones": zones}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(zones)} 구역 → {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main(sys.argv[1])
