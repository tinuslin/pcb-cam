# -*- coding: utf-8 -*-
"""自测：装载各类样例，打印层 / 钻孔 / 网表 / 分析结果，并校验关键数值。

用法:  python tests/selftest.py
退出码 0 表示全部通过。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pcb_cam import project  # noqa: E402
from pcb_cam import report  # noqa: E402
from pcb_cam import units  # noqa: E402
from pcb_cam import xy  # noqa: E402

BASE = os.path.join(HERE, "samples")
FAIL = []


def check(cond, msg):
    print("    [%s] %s" % ("OK" if cond else "FAIL", msg))
    if not cond:
        FAIL.append(msg)


def dump(tag, path, expect_fmt=None, expect_layers=None, expect_holes=None,
         expect_size=(60.0, 40.0)):
    print("=" * 74)
    print("LOAD %s" % path)
    if not os.path.exists(path):
        check(False, "%s 样例缺失" % tag)
        return None
    b = project.load_board(path)
    s = project.board_summary(b)

    bb = s["bbox"]
    if bb:
        print("  bbox   : %.2f, %.2f -> %.2f, %.2f  (%.2f x %.2f mm)" % (
            bb[0], bb[1], bb[2], bb[3], bb[2] - bb[0], bb[3] - bb[1]))
    print("  format : %s   step=%s   layers=%d" % (s["format"], s.get("step") or "-", len(s["layers"])))
    for L in s["layers"]:
        print("    - %-20s kind=%-8s side=%-6s obj=%-4d opa=%.2f" % (
            L["name"], L["kind"], L["side"], L["count"], L["opacity"]))

    d = s.get("drills")
    if d:
        print("  drills : %d holes, %d slots, %d tools, plated=%s" % (
            len(d["holes"]), len(d["slots"]), len(d["tools"]), d["plated"]))
        for t in d["tools"]:
            print("      T%-3s d=%.3fmm  n=%-3d %s" % (t["num"], t["d"], t["count"], "PTH" if t["plated"] else "NPTH"))
    else:
        print("  drills : (none)")

    for nl in s.get("netlists", []):
        print("  netlist: %s <- %s  nets=%d points=%d" % (
            nl["name"], nl["source"], nl["net_count"], nl["point_count"]))

    an = s.get("analysis") or {}
    if an:
        ring = an.get("annular") or {}
        print("  analysis: min_width=%s copper_area=%.2fcm2 min_ring=%s thin_ring=%s no_pad=%s" % (
            an.get("min_trace_width"), an.get("copper_area_cm2") or 0.0,
            ring.get("min_ring"), ring.get("thin_ring"), ring.get("no_pad")))
        for w in (an.get("warnings") or [])[:5]:
            print("    ! %s" % w)
    if s.get("warnings"):
        print("  warnings: %s" % (s["warnings"][:5],))

    if expect_fmt:
        check(s["format"] == expect_fmt, "%s 格式识别 = %s (期望 %s)" % (tag, s["format"], expect_fmt))
    if expect_layers is not None:
        check(len(s["layers"]) == expect_layers, "%s 层数 = %d (期望 %d)" % (
            tag, len(s["layers"]), expect_layers))
    if expect_holes is not None:
        got = len(d["holes"]) if d else 0
        check(got == expect_holes, "%s 孔数 = %d (期望 %d)" % (tag, got, expect_holes))
    if bb and expect_size:
        ew, eh = expect_size
        check(abs((bb[2] - bb[0]) - ew) < 0.05 and abs((bb[3] - bb[1]) - eh) < 0.05,
              "%s 板尺寸 = %.2f x %.2f (期望 %.0f x %.0f)" % (
                  tag, bb[2] - bb[0], bb[3] - bb[1], ew, eh))
    return b


def main():
    b1 = dump("gerber-dir", os.path.join(BASE, "demo-gerber"), "gerber", 7, 8)
    dump("gerber-zip", os.path.join(BASE, "demo-gerber.zip"), "gerber", 7, 8)
    dump("odb-dir", os.path.join(BASE, "demo-odb"), "odbpp", 5, 8)
    dump("odb-tgz", os.path.join(BASE, "demo-odb.tgz"), "odbpp", 5, 8)
    # 单文件只含顶层铜箔，包围盒必然小于板框，故不校验尺寸
    dump("single", os.path.join(BASE, "demo-gerber", "demo.gtl"), "gerber", 1, None,
         expect_size=None)
    # 英制（inch）样例：%MOIN / M72 / 2:4，换算后应与 1.90 x 1.40 in 完全一致
    bi = dump("inch", os.path.join(BASE, "demo-inch"), "gerber", 2, 3,
              expect_size=(48.26, 35.56))

    if b1:
        md = report.build_report(b1)
        out = os.path.join(HERE, "report-demo.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write(md)
        check(len(md) > 500, "Markdown 报告生成 %d 字符 -> %s" % (len(md), out))

    # ---- 英制资料：解析与显示 ----
    if bi:
        an = bi.analysis or {}
        ui = an.get("units_in") or {}
        check(ui.get("layers") == ["inch"], "英制 Gerber 源单位识别 = inch")
        check(ui.get("drill") == "inch", "英制 Excellon 源单位识别 = inch")
        di = an.get("drill") or {}
        check(abs((di.get("min_dia") or 0) - 0.8128) < 1e-3,
              "0.032 in 孔径 = %.4f mm（期望 0.8128）" % (di.get("min_dia") or 0))
        md_in = report.build_report(bi, {"units": "both"})
        check(any("1900.0" in ln for ln in md_in.splitlines() if ln.startswith("- 板面尺寸")),
              "英制板宽 48.26 mm = 1900.0 mil 同时出现在报告")

    # ---- 单位：公制 / 英制 ----
    print("=" * 74)
    print("UNITS mm <-> mil <-> inch")
    check(abs(units.to_mm(1000, "mil") - 25.4) < 1e-9, "1000 mil = 25.4 mm")
    check(abs(units.convert(25.4, "mm", "inch") - 1.0) < 1e-9, "25.4 mm = 1 in")
    check(abs(units.from_mm(1.0, "mil") - 39.3700787) < 1e-6, "1 mm = 39.3701 mil")
    check(units.fmt(25.4, "mil") == "1000.0", "格式化 25.4mm -> 1000.0 mil")
    check("mm" in units.dual(1.6) and "mil" in units.dual(1.6), "双单位 1.6 mm / mil")
    if b1:
        md_both = report.build_report(b1, {"units": "both"})
        md_mil = report.build_report(b1, {"units": "mil"})
        md_mm = report.build_report(b1, {"units": "mm"})
        size_mm = [l for l in md_mm.splitlines() if l.startswith("- 板面尺寸")][0]
        size_mil = [l for l in md_mil.splitlines() if l.startswith("- 板面尺寸")][0]
        check("mil" not in size_mm.split("（")[0], "公制报告板面尺寸用 mm")
        check("mil" in size_mil and "mm" not in size_mil.split("**")[1], "英制报告板面尺寸用 mil")
        check("mil" in md_both.splitlines()[7], "双单位报告同时给出 mm 与 mil")
        head = xy.csv_header("both")
        check("X(mm)" in head and "X(mil)" in head, "CSV 双单位表头含 X(mm)/X(mil)")
        bob = project.load_board(os.path.join(BASE, "demo-odb"))
        rows_both = xy.rows_from_board(bob, unit="both")
        check(bool(rows_both) and len(rows_both[0]) == len(head),
              "CSV 双单位行列数一致（%d 列 / %d 行）" % (len(head), len(rows_both)))
        rows_mil = xy.rows_from_board(bob, unit="mil")
        check(bool(rows_mil) and len(rows_mil[0]) == len(xy.csv_header("mil")),
              "CSV 英制列数一致")

    print("=" * 74)
    if FAIL:
        print("FAILED %d 项:" % len(FAIL))
        for m in FAIL:
            print("  -", m)
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
