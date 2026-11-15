# -*- coding: utf-8 -*-
"""自测：装载各类样例，打印层 / 钻孔 / 网表 / 分析结果，并校验关键数值。

用法:  python tests/selftest.py
退出码 0 表示全部通过。
"""
import os
import shutil
import sys
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pcb_cam import components as compmod  # noqa: E402
from pcb_cam import dxf as dxfm  # noqa: E402
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
    # DXF 图纸：5 个 CAD 图层、mm 单位、60 x 40 板框
    bd = dump("dxf", os.path.join(BASE, "demo.dxf"), "dxf", 5, 0,
              expect_size=(60.0, 40.0))

    # ---- DXF / DWG 导入 ----
    print("=" * 74)
    print("DXF / DWG")
    if bd:
        check((bd.dxf or {}).get("units") == "mm", "DXF $INSUNITS=4 识别为 mm")
        check((bd.dxf or {}).get("blocks") == 1, "DXF BLOCKS 块定义被识别")
        check(any(o.get("t") == "text" for l in bd.layers for o in l["objects"]),
              "DXF TEXT / MTEXT 生成可渲染的 text 图元")
        check(any(l["kind"] == "outline" for l in bd.layers),
              "DXF 图层名 OUTLINE 自动判为外形层")
        warns = (bd.analysis or {}).get("warnings") or []
        check(not any("未找到铜箔层" in w or "未找到钻孔" in w for w in warns),
              "CAD 图纸不套用 PCB 制造资料的完整性告警")
        with open(os.path.join(BASE, "demo.dxf"), "r", encoding="utf-8") as f:
            txt = f.read()
        check(project.sniff_text(txt) == "dxf", "sniff_text 正确识别 ASCII DXF")
        check(project.sniff_text("%FSLAX24Y24*%") == "gerber", "Gerber 未被 DXF 嗅探误判")

        # 无转换器时必须给出导出指引，而不是假装读得懂二进制
        fake_dwg = os.path.join(BASE, "bridge.dwg")
        with open(fake_dwg, "wb") as f:
            f.write(b"AC1015\x00\x01binary-dwg")
        try:
            old = os.environ.pop("PCB_CAM_DWG2DXF", None), os.environ.pop("PCB_CAM_ODA", None)
            with mock.patch.object(dxfm.shutil, "which", return_value=None):
                try:
                    dxfm.convert_dwg(fake_dwg)
                    check(False, "无转换器时应抛出带指引的错误")
                except ValueError as exc:
                    check("另存为" in str(exc) and "DXF" in str(exc),
                          "无转换器时给出「另存为 DXF」操作指引")

            # 模拟本机装了 dwg2dxf：转换 -> 解析 -> 出板
            dxf_path = os.path.join(BASE, "demo.dxf")
            os.environ["PCB_CAM_DWG2DXF"] = dxf_path     # 只作为「存在的可执行文件」占位
            def fake_run(cmd, timeout=120):
                shutil.copyfile(dxf_path, cmd[2])
                return 0, b"ok"
            with mock.patch.object(dxfm, "_run", fake_run):
                converted, conv = dxfm.convert_dwg(fake_dwg)
                check(conv == "LibreDWG dwg2dxf" and os.path.getsize(converted) > 100,
                      "DWG -> DXF 转换器桥接产出可用 DXF")
                bw = project.load_board(fake_dwg)
                check(bw.format == "dxf" and len(bw.layers) == 5,
                      "DWG 经转换后与 DXF 走同一套图层渲染链路")
                check(any("dwg2dxf" in w for w in bw.warnings),
                      "摘要提示本次所用的 DWG 转换器")
        finally:
            os.environ.pop("PCB_CAM_DWG2DXF", None)
            os.remove(fake_dwg)
            for k, v in zip(("PCB_CAM_DWG2DXF", "PCB_CAM_ODA"), old):
                if v is not None:
                    os.environ[k] = v

    # ---- 层的类型手动选择 ----
    print("=" * 74)
    print("LAYER RECLASSIFY")
    brc = project.load_board(os.path.join(BASE, "demo.dxf"))
    idx = next((i for i, l in enumerate(brc.layers) if l["name"] == "DOC_NOTES"), None)
    check(idx is not None, "找到待改类型的 CAD 图层")
    if idx is not None:
        before = [l["name"] for l in brc.layers]
        project.reclassify_layer(brc, idx, "outline", "both")
        check(brc.layers[idx]["kind"] == "outline" and brc.layers[idx]["side"] == "both",
              "手动改类型/板面即时生效")
        check([l["name"] for l in brc.layers] == before, "改类型不重排层顺序（前端缓存不错位）")
        check(project.board_summary(brc)["layers"][idx]["kind_source"] == "manual",
              "摘要里标记手动来源")
        project.reclassify_layer(brc, idx, "auto")
        check(brc.layers[idx]["kind"] == "doc", "'自动' 可按文件名复原")

    # ---- 元件长 / 宽 / 类型 ----
    print("=" * 74)
    print("COMPONENT SIZE / MOUNT")
    check(compmod.parse_size("1.6x0.8mm") == (1.6, 0.8), "尺寸字符串 1.6x0.8mm 解析")
    check(compmod.parse_size("0603") == (1.6, 0.8), "封装代号 0603 -> 1.6 x 0.8 mm")
    check(compmod.normalize_mount("THT") == "TH" and
          compmod.normalize_mount("Surface Mount") == "SMD", "贴装类型归一化 SMD / TH")
    bo = project.load_board(os.path.join(BASE, "demo-odb"))
    if bo:
        r1 = next((c for c in bo.components if c.get("ref") == "R1"), None)
        check(bool(r1) and abs((r1.get("length") or 0) - 1.6) < 1e-6 and
              abs((r1.get("width") or 0) - 0.8) < 1e-6 and r1.get("mount") == "SMD",
              "ODB++ PRP 属性里的长/宽/贴装类型被导出")
        check(all((c.get("length") or 0) > 0 for c in bo.components),
              "板上每个器件都推断出了长度（属性 / 焊盘 / 封装代号三级兜底）")
        head = xy.csv_header("mm")
        for col in ("Length(mm)", "Width(mm)", "Mount", "SizeSrc"):
            check(col in head, "坐标 CSV 表头含 %s" % col)
        rows = xy.rows_from_board(bo, unit="mm")
        check(bool(rows) and len(rows[0]) == len(head) and
              rows[0][head.index("Mount")] in ("SMD", "TH", ""),
              "坐标 CSV 行含长/宽/类型且列数对齐")
    with open(os.path.join(BASE, "demo-xy.csv"), "r", encoding="utf-8") as f:
        xd = xy.parse_xy(f.read(), "demo-xy.csv")
    check(bool(xd.entries) and abs((xd.entries[0].get("length") or 0) - 1.6) < 1e-6
          and xd.entries[0].get("mount") == "SMD",
          "XY 文件的 Length / Width / Mount 列被解析")

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
