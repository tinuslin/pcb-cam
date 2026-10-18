"""Markdown 分析报告生成。

尺寸一律以 mm 计算，报告默认同时给出公制与英制（mil / inch），
可通过 extra["units"] = "both" | "mm" | "mil" | "inch" 切换。
"""
from __future__ import annotations

import datetime
import os

from . import units

_KIND_CN = {
    "copper": "铜箔", "mask": "阻焊", "silk": "丝印", "paste": "钢网",
    "outline": "外形", "drill": "钻孔", "rout": "铣槽", "doc": "文档",
    "component": "器件", "other": "其它",
}
_SIDE_CN = {"top": "顶层", "bottom": "底层", "inner": "内层", "both": "双面", "none": "-"}
_XY_CN = {"ok": "正常", "offset": "坐标偏差", "wrong_side": "面别不符",
          "no_pad": "无焊盘", "outside": "超出板框"}

_UNIT_OPTS = ("both", "mm", "mil", "inch")


class _Fmt:
    """按报告单位设置格式化尺寸。"""

    def __init__(self, opt="both"):
        o = str(opt or "both").strip().lower()
        self.opt = o if o in _UNIT_OPTS else "both"
        self.primary = "mm" if self.opt == "both" else self.opt

    # 表头里的单位说明，例如 "mm / mil"
    @property
    def head(self):
        if self.opt == "both":
            return "mm / mil"
        return "in" if self.opt == "inch" else self.opt

    def cell(self, v_mm, nd=None):
        """表格单元格：双单位时紧凑写法，单单位时纯数字。"""
        if v_mm is None:
            return "-"
        if self.opt == "both":
            return units.dual_short(v_mm, "mm", nd)
        return units.fmt(v_mm, self.opt, nd)

    def full(self, v_mm, nd=None):
        """正文：带单位后缀。"""
        if v_mm is None:
            return "-"
        if self.opt == "both":
            return units.dual(v_mm, "mm", nd)
        return units.len(v_mm, self.opt, nd)

    def val(self, v_mm, nd=None):
        """只给主单位数字（用于需要自行拼单位的句子）。"""
        if v_mm is None:
            return "-"
        return units.fmt(v_mm, self.primary, nd)

    @property
    def name(self):
        return "in" if self.primary == "inch" else self.primary


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _short_path(path):
    """项目内资料显示相对路径，避免报告里出现本机绝对路径。"""
    try:
        rel = os.path.relpath(path, _ROOT)
    except ValueError:            # 不同盘符
        return path
    return path if rel.startswith("..") else rel.replace("\\", "/")


def build_report(board, extra: dict | None = None) -> str:
    s = extra or {}
    F = _Fmt(s.get("units") or "both")
    a = board.analysis or {}
    lines = []
    name = board.name or os.path.basename(board.path)
    lines.append("# PCB 资料分析报告 — %s" % name)
    lines.append("")
    lines.append("- 数据来源：`%s`" % _short_path(board.path))
    lines.append("- 数据格式：**%s**%s" % (board.format.upper(),
                                       ("（step: %s）" % board.step) if board.step else ""))
    lines.append("- 生成时间：%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("- 尺寸单位：**%s**%s" % (
        {"both": "公制 mm + 英制 mil", "mm": "公制 mm",
         "mil": "英制 mil", "inch": "英制 inch"}[F.opt],
        "（1 mil = 0.0254 mm，1 inch = 25.4 mm）" if F.opt == "both" else ""))
    ui = a.get("units_in") or {}
    src = []
    if ui.get("layers"):
        src.append("图形层 " + " / ".join(
            {"mm": "mm 公制", "inch": "inch 英制", "micron": "µm"}.get(u, u)
            for u in ui["layers"]))
    if ui.get("drill"):
        src.append("钻孔 " + {"mm": "mm 公制", "inch": "inch 英制"}.get(
            ui["drill"], ui["drill"]))
    if src:
        lines.append("- 源文件单位：%s（解析后统一折算为 mm 计算）" % "；".join(src))
    size = a.get("board_size")
    if size:
        w, h = size["w"], size["h"]
        if F.opt == "both":
            lines.append("- 板面尺寸：**%s × %s**（%s × %s mil；%s × %s in）" % (
                units.len(w, "mm", 2), units.len(h, "mm", 2),
                units.fmt(w, "mil", 1), units.fmt(h, "mil", 1),
                units.fmt(w, "inch", 3), units.fmt(h, "inch", 3)))
        elif F.opt == "mm":
            lines.append("- 板面尺寸：**%s × %s**（%s × %s in）" % (
                units.len(w, "mm", 2), units.len(h, "mm", 2),
                units.fmt(w, "inch", 3), units.fmt(h, "inch", 3)))
        else:
            lines.append("- 板面尺寸：**%s × %s**" % (
                units.len(w, F.opt), units.len(h, F.opt)))
    kinds = {}
    for l in board.layers:
        kinds[l["kind"]] = kinds.get(l["kind"], 0) + 1
    detail = " / ".join("%s %d" % (_KIND_CN.get(k, k), n)
                        for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))
    lines.append("- 图层数量：%d（%s）" % (a.get("layer_count", 0), detail or "-"))
    lines.append("- 图元总数：%s" % _num(a.get("total_objects")))
    area = a.get("copper_area_cm2")
    if area is not None:
        lines.append("- 铜箔面积：%s cm²（%s in²）" % (
            _num(area), _num(float(area) / 6.4516, 2)))
    lines.append("")

    lines.append("## 一、层清单")
    lines.append("")
    lines.append("| # | 层名 | 类型 | 板面 | 图元数 | 最小线宽(%s) | 铜面积(mm²) | 文件 |"
                 % F.head)
    lines.append("|---|---|---|---|---|---|---|---|")
    for i, l in enumerate(board.layers):
        st = l.get("stats") or {}
        lines.append("| %d | %s | %s | %s | %d | %s | %s | `%s` |" % (
            i + 1, l["name"], _KIND_CN.get(l["kind"], l["kind"]),
            _SIDE_CN.get(l["side"], l["side"]), len(l["objects"]),
            F.cell(st.get("min_width")), _num(st.get("area")),
            os.path.basename(l.get("file") or "")))
    lines.append("")

    d = a.get("drill")
    lines.append("## 二、钻孔统计")
    lines.append("")
    if d:
        lines.append("- 孔数：**%d**，槽数：%d，刀具数：%d"
                     % (d["holes"], d["slots"], d["tools"]))
        lines.append("- 孔径范围：%s ~ %s" % (F.full(d.get("min_dia")), F.full(d.get("max_dia"))))
        lines.append("- 镀通属性：%s" % ("镀通孔 (PTH)" if d.get("plated") else "非镀通 / 未标注"))
        lines.append("")
        if board.drills and board.drills.tools:
            lines.append("| 刀具 | 孔径(%s) | 孔数 | 备注 |" % F.head)
            lines.append("|---|---|---|---|")
            for num, t in sorted(board.drills.tools.items()):
                note = "镀通" if t.get("plated") else "非镀通"
                lines.append("| T%s | %s | %d | %s |" % (num, F.cell(t["d"]), t["count"], note))
            lines.append("")
    else:
        lines.append("_未发现钻孔数据。_")
        lines.append("")

    ring = a.get("annular") or {}
    lines.append("## 三、工艺快速检查（DRC-lite）")
    lines.append("")
    lines.append("| 检查项 | 结果 |")
    lines.append("|---|---|")
    mw = a.get("min_trace_width")
    lines.append("| 最小线宽 | %s %s |" % (
        F.full(mw), "⚠️ 偏细" if (mw is not None and mw < 0.09) else "✅"))
    lines.append("| 最小环宽 | %s |" % F.full(ring.get("min_ring")))
    lines.append("| 环宽 < %s 的孔 | %s 个 %s |" % (
        F.full(0.05, 2), ring.get("thin_ring", 0), "⚠️" if ring.get("thin_ring") else "✅"))
    lines.append("| 找不到铜环的孔 | %s 个 |" % ring.get("no_pad", 0))
    lines.append("| 图层完整性 | %d 层，%s钻孔 |" % (
        a.get("layer_count", 0), "有" if d else "无"))
    lines.append("")
    if ring.get("samples"):
        lines.append("环宽偏小的孔（示例）：")
        lines.append("")
        lines.append("| X(%s) | Y(%s) | 孔径(%s) | 环宽(%s) | 所在层 |"
                     % (F.head, F.head, F.head, F.head))
        lines.append("|---|---|---|---|---|")
        for sp in ring["samples"]:
            lines.append("| %s | %s | %s | %s | %s |" % (
                F.cell(sp["x"]), F.cell(sp["y"]), F.cell(sp["d"]),
                F.cell(sp["ring"]), sp["layer"]))
        lines.append("")

    cmp = a.get("netlist_compare")
    lines.append("## 四、网表")
    lines.append("")
    if board.netlists:
        for nl in board.netlists:
            lines.append("- `%s`：网络 %d 个，测试点 %d 个"
                         % (os.path.basename(nl.source or nl.name), len(nl.nets), len(nl.points)))
        lines.append("")
        if cmp:
            lines.append("**网表比对**：%s vs %s" % (cmp["a"]["name"], cmp["b"]["name"]))
            lines.append("")
            lines.append("- 仅存在于 A 的网络：%d 个" % cmp["only_a_count"])
            lines.append("- 仅存在于 B 的网络：%d 个" % cmp["only_b_count"])
            lines.append("- 同一网络但点数不同：%d 个" % cmp["count_diff_total"])
            lines.append("")
            if cmp["only_a"]:
                lines.append("A 独有：`%s`" % "`, `".join(cmp["only_a"][:15]))
                lines.append("")
            if cmp["only_b"]:
                lines.append("B 独有：`%s`" % "`, `".join(cmp["only_b"][:15]))
                lines.append("")
            if cmp["count_diff"]:
                lines.append("点数差异（前 15）：")
                lines.append("")
                lines.append("| 网络 | A 点数 | B 点数 |")
                lines.append("|---|---|---|")
                for row in cmp["count_diff"][:15]:
                    lines.append("| %s | %d | %d |" % (row["net"], row["a"], row["b"]))
                lines.append("")
    else:
        lines.append("_未提供网表文件（可用 IPC-D-356 或 ODB++ 网表做比对）。_")
        lines.append("")

    warns = a.get("warnings") or []
    lines.append("## 五、风险与提示")
    lines.append("")
    if warns:
        for w in warns:
            lines.append("- %s" % w)
    else:
        lines.append("- 未发现明显问题。")
    lines.append("")

    if board.components:
        lines.append("## 六、器件清单（前 30）")
        lines.append("")
        lines.append("| 位号 | 面 | 封装 | 值 | X(%s) | Y(%s) | 旋转 |"
                     % (F.head, F.head))
        lines.append("|---|---|---|---|---|---|---|")
        for c in board.components[:30]:
            lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                c.get("ref", "") or "-",
                "底" if (c.get("side") or "") == "bottom" else ("顶" if c.get("side") else "-"),
                c.get("package") or "-", c.get("value") or "-",
                F.cell(c.get("x")), F.cell(c.get("y")),
                _fmt(c.get("rot"), 0) if c.get("rot") is not None else "-"))
        if len(board.components) > 30:
            lines.append("")
            lines.append("_共 %d 个器件，此处仅列前 30；完整清单用「导出 CSV」。_" % len(board.components))
        lines.append("")

    chk = s.get("xy")
    if chk and chk.get("total"):
        st = chk.get("stats") or {}
        lines.append("## 七、贴片坐标（XY）校验")
        lines.append("")
        lines.append("- 校验文件：`%s`" % (s.get("xy_name") or "-"))
        lines.append("- 条目数：%d；容差 %s；单位 %s%s" % (
            chk.get("total", 0), F.full(chk.get("tolerance") or 0),
            s.get("xy_units") or "mm", "（Y 已镜像）" if s.get("xy_yflip") else ""))
        lines.append("- 结果：**正常 %d** / 偏差 %d / 面别不符 %d / 无焊盘 %d / 越界 %d%s" % (
            st.get("ok", 0), st.get("offset", 0), st.get("wrong_side", 0),
            st.get("no_pad", 0), st.get("outside", 0),
            ("；板上多出 %d 个未在坐标文件中" % chk["board_only_count"])
            if chk.get("board_only_count") else ""))
        if chk.get("max_offset"):
            lines.append("- 最大坐标偏差：**%s**" % F.full(chk["max_offset"]))
        bad = [e for e in (chk.get("entries") or []) if e.get("status") != "ok"]
        if bad:
            lines.append("")
            lines.append("| 位号 | 状态 | X(%s) | Y(%s) | 说明 |" % (F.head, F.head))
            lines.append("|---|---|---|---|---|")
            for e in bad[:40]:
                lines.append("| %s | %s | %s | %s | %s |" % (
                    e.get("ref", ""), _XY_CN.get(e.get("status"), e.get("status")),
                    F.cell(e.get("x")), F.cell(e.get("y")), e.get("note") or ""))
            if len(bad) > 40:
                lines.append("")
                lines.append("_异常项共 %d 条，此处仅列前 40。_" % len(bad))
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("_本报告由 PCB-CAM 自动生成；环宽等指标为几何估算值，量产前请以专业 CAM 软件复核。_")
    return "\n".join(lines)


def _fmt(v, nd=3):
    if v is None:
        return "-"
    try:
        return ("%." + str(nd) + "f") % float(v)
    except (TypeError, ValueError):
        return "-"


def _num(v, nd=0):
    if v is None:
        return "-"
    try:
        if nd:
            return "{:,.{}f}".format(float(v), nd)
        return "{:,.0f}".format(float(v))
    except (TypeError, ValueError):
        return "-"
