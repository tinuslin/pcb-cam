# -*- coding: utf-8 -*-
"""打印 ui_smoke.js 的 JSON 结果摘要（供 run_tests.bat 调用）。"""
import json
import sys

for p in sys.argv[1:]:
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    b = d.get("board") or {}
    errs = d.get("errors") or []
    print("  %-7s board=%-12s layers=%-2s objects=%-4d drawOps(fill=%d,stroke=%d) "
          "neg=%-2d ok=%s" % (
              b.get("format", "?"), b.get("name", "?"), b.get("layers"),
              d.get("objTotal") or 0,
              (d.get("drawOps") or {}).get("fill", 0),
              (d.get("drawOps") or {}).get("stroke", 0),
              (d.get("compositeModes") or {}).get("destination-out", 0),
              d.get("ok")))
    if errs:
        print("    errors: %s" % errs)
    bad = [s for s in (d.get("steps") or []) if s[1] != "ok"]
    if bad:
        print("    failed steps: %s" % bad)
    if not d.get("ok"):
        sys.exit(1)
