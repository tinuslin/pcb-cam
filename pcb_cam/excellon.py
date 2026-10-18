"""Excellon 钻孔 / 铣槽文件解析器。

支持：
  * M48 文件头与无头文件（直接 T1 开始）
  * INCH / METRIC、M71 / M72，;FILE_FORMAT=2:4 等格式声明
  * 刀具表 T1C0.035 / T01C.035 / T1F00S00C0.800
  * 前零 / 后零省略自动判别（按包围盒合理性择优）
  * G81 / G82 / G83 钻孔、G85 槽孔、G00/G01 + M15/M16 铣槽
  * 镀通 / 非镀通推断（文件注释 + 文件名）

输出：孔列表 + 槽列表，坐标毫米。
"""
from __future__ import annotations

import math
import re

from . import geom

_TOOL_RE = re.compile(r"^T0*(\d+)((?:[A-Z][+-]?[\d.]+)*)C([\d.]+)", re.I)
_XY_RE = re.compile(r"X([+-]?[\d.]+)Y([+-]?[\d.]+)|X([+-]?[\d.]+)|Y([+-]?[\d.]+)")


class DrillProgram:
    def __init__(self, name=""):
        self.name = name
        self.holes = []          # {"x","y","d","tool","plated"}
        self.slots = []          # {"p":[[x,y],...], "w":width, "tool":n}
        self.tools = {}          # tool num -> {"d": mm, "count": n, "plated": bool}
        self.bbox = None
        self.warnings = []
        self.units = "mm"
        self.plated = True
        self.stats = {"holes": 0, "slots": 0, "tools": 0}

    def to_dict(self):
        return {
            "name": self.name,
            "holes": self.holes,
            "slots": self.slots,
            "tools": [
                {"num": n, "d": round(t["d"], 4), "count": t["count"], "plated": t["plated"]}
                for n, t in sorted(self.tools.items())
            ],
            "bbox": [round(v, 4) for v in self.bbox] if self.bbox else None,
            "warnings": self.warnings,
            "stats": self.stats,
            "plated": self.plated,
        }


def _plated_hint(name: str, text: str):
    up = (name or "").upper()
    head = text[:4000].upper()
    if "NON_PLATED" in head or "NON-PLATED" in head or "NPTH" in head or "NONPLATED" in head:
        return False
    if "TYPE=PLATED" in head or "PLATED" in head:
        return True
    if re.search(r"(NPTH|NON[_-]?PLATED|NONPLT)", up):
        return False
    if re.search(r"(PTH|PLATED|DRL)", up):
        return True
    return None


def parse_excellon(text: str, name: str = "", plated_hint=None) -> DrillProgram:
    prog = DrillProgram(name)
    raw = text.replace("\r", "")
    lines = raw.split("\n")

    units = None                 # 'mm' | 'in'
    fmt = None                   # (int, dec)
    zero_mode = None             # 'LZ' | 'TZ'
    tools_raw = {}               # num -> raw dia string
    events = []                  # (kind, ...)
    in_header = False
    header_done = False
    seen_body = False
    plated = plated_hint if plated_hint is not None else _plated_hint(name, raw)
    metric_hint = False

    for line in lines:
        s = line.strip()
        if not s:
            continue
        su = s.upper()
        if su.startswith(";"):
            # 注释：可能携带格式/单位信息
            if "FILE_FORMAT" in su:
                m = re.search(r"(\d+)\s*[:.]\s*(\d+)", s)
                if m:
                    fmt = (int(m.group(1)), int(m.group(2)))
            if "METRIC" in su:
                metric_hint = True
            if "TYPE=NON" in su:
                plated = False
            continue
        if su == "M48":
            in_header = True
            continue
        if su in ("%", "M95"):
            in_header = False
            header_done = True
            continue
        if su.startswith("INCH"):
            units = "in"
            zero_mode = "TZ" if "TZ" in su else ("LZ" if "LZ" in su else zero_mode)
            continue
        if su.startswith("METRIC") or su.startswith("MM"):
            units = "mm"
            zero_mode = "TZ" if "TZ" in su else ("LZ" if "LZ" in su else zero_mode)
            continue
        if su in ("M71",):
            units = "mm"
            continue
        if su in ("M72",):
            units = "in"
            continue
        if su.startswith("M30") or su.startswith("M00") or su.startswith("M02"):
            break

        m = _TOOL_RE.match(s)
        if m:
            num = int(m.group(1))
            tools_raw[num] = m.group(3)
            if not in_header:
                events.append(("tool", num))
            continue

        if re.match(r"^T0*(\d+)$", s, re.I):
            num = int(re.match(r"^T0*(\d+)$", s, re.I).group(1))
            events.append(("tool", num))
            seen_body = True
            if not header_done:
                header_done = True
            continue

        if su.startswith("G90"):
            continue
        if su.startswith("G91"):
            events.append(("incremental", True))
            continue
        if su.startswith("G05"):
            events.append(("drillmode",))
            continue
        if su.startswith("G85"):
            mxy = _XY_RE.search(s[3:]) if len(s) > 3 else None
            events.append(("slot_g85", mxy.group(0) if mxy else ""))
            seen_body = True
            continue
        if su.startswith(("G00", "G01", "G02", "G03", "G1", "G0")) or su[0] in "XY":
            seen_body = True
            events.append(("move", s))
            continue
        if su.startswith("M15"):
            events.append(("down",))
            continue
        if su.startswith(("M16", "M17")):
            events.append(("up",))
            continue
        if su.startswith("R") and re.match(r"^R\d+$", su):
            events.append(("repeat", int(su[1:])))
            continue
        # 其它指令忽略（DETECT/ICI/VER/FMAT 等）

    if units is None:
        units = "mm" if metric_hint or re.search(r"(METRIC|MM)", raw[:2000].upper()) else "in"
    if fmt is None:
        fmt = (3, 3) if units == "mm" else (2, 4)
    if zero_mode is None:
        # 依据数字串是否以 0 开头粗判
        zero_mode = "TZ" if raw.count("X0") > raw.count("X00") else "LZ"

    unit_scale = geom.MM_PER_INCH if units == "in" else 1.0
    total = fmt[0] + fmt[1]
    decd = fmt[1]

    def coord(tok: str, pad_right: bool):
        if "." in tok:
            v = float(tok)
            return v * unit_scale
        neg = tok.startswith("-")
        dg = tok.lstrip("+-")
        if not dg.isdigit():
            return None
        if pad_right:
            if len(dg) < total:
                dg = dg.ljust(total, "0")
        v = int(dg) / (10.0 ** decd)
        return (-v if neg else v) * unit_scale

    def materialize(pad_right: bool):
        holes, slots = [], []
        tools = {}
        cur_tool = None
        x = y = 0.0
        incr = False
        routing = False
        down = False
        route_pts = []
        route_tool = None
        bbox = None
        for ev in events:
            kind = ev[0]
            if kind == "incremental":
                incr = True
            elif kind == "tool":
                cur_tool = ev[1]
                if ev[1] not in tools and ev[1] in tools_raw:
                    tools[ev[1]] = 0
            elif kind == "down":
                down = True
                routing = True
                route_pts = [(x, y)]
                route_tool = cur_tool
            elif kind == "up":
                if down and len(route_pts) > 1:
                    w = _tool_dia(tools_raw.get(route_tool, "0"), unit_scale, units)
                    slots.append({"p": [[round(px, 4), round(py, 4)] for px, py in route_pts],
                                  "w": round(w, 4), "tool": route_tool})
                    for px, py in ((route_pts[0][0], route_pts[0][1]),):
                        bbox = geom.merge_bbox(bbox, (px - w / 2, py - w / 2, px + w / 2, py + w / 2))
                down = False
                routing = False
                route_pts = []
            elif kind == "slot_g85":
                tok = ev[1]
                m = _XY_RE.search(tok) if tok else None
                if m:
                    sx, sy = x, y
                    nx = coord(m.group(1), pad_right) if m.group(1) else None
                    ny = coord(m.group(2), pad_right) if m.group(2) else None
                    if nx is not None:
                        x = x + nx if incr else nx
                    if ny is not None:
                        y = y + ny if incr else ny
                    w = _tool_dia(tools_raw.get(cur_tool, "0"), unit_scale, units)
                    slots.append({"p": [[round(sx, 4), round(sy, 4)], [round(x, 4), round(y, 4)]],
                                  "w": round(w, 4), "tool": cur_tool})
                    bbox = geom.merge_bbox(bbox, (min(sx, x), min(sy, y), max(sx, x), max(sy, y)))
            elif kind == "move":
                stmt = ev[1]
                up = stmt.upper()
                is_rapid = up.startswith("G00") or up.startswith("G0")
                go_route = up.startswith("G01") or up.startswith("G1")
                m = re.search(r"X([+-]?[\d.]+)", stmt)
                ny = re.search(r"Y([+-]?[\d.]+)", stmt)
                nx = m.group(1) if m else None
                nvy = ny.group(1) if ny else None
                if nx is None and nvy is None:
                    continue
                px, py = x, y
                if nx is not None:
                    vx = coord(nx, pad_right)
                    if vx is not None:
                        x = x + vx if incr else vx
                if nvy is not None:
                    vy = coord(nvy, pad_right)
                    if vy is not None:
                        y = y + vy if incr else vy
                if is_rapid:
                    # 快速定位：只移动，不下钻
                    if down and routing:
                        route_pts.append((x, y))
                    continue
                if down and routing:
                    route_pts.append((x, y))
                    bbox = geom.merge_bbox(bbox, (min(px, x), min(py, y), max(px, x), max(py, y)))
                elif go_route and not routing:
                    # 无下刀指令但出现 G01 走刀：按铣槽处理
                    w = _tool_dia(tools_raw.get(cur_tool, "0"), unit_scale, units)
                    if w > 0:
                        slots.append({"p": [[round(px, 4), round(py, 4)], [round(x, 4), round(y, 4)]],
                                      "w": round(w, 4), "tool": cur_tool})
                        bbox = geom.merge_bbox(bbox, (min(px, x), min(py, y), max(px, x), max(py, y)))
                else:
                    d = _tool_dia(tools_raw.get(cur_tool, "0"), unit_scale, units) if cur_tool is not None else 0.0
                    if cur_tool is None or d <= 0:
                        continue
                    holes.append({"x": round(x, 4), "y": round(y, 4), "d": round(d, 4),
                                  "tool": cur_tool, "plated": bool(plated) if plated is not None else True})
                    tools[cur_tool] = tools.get(cur_tool, 0) + 1
                    bbox = geom.merge_bbox(bbox, (x - d / 2, y - d / 2, x + d / 2, y + d / 2))
            elif kind == "drillmode":
                routing = False
        return holes, slots, tools, bbox

    best = None
    for pad_right in (False, True):
        holes, slots, tools, bbox = materialize(pad_right)
        score = _plausible(bbox, len(holes) + len(slots))
        cand = (score, pad_right, holes, slots, tools, bbox)
        if best is None or cand[0] > best[0]:
            best = cand

    score, pad_right, holes, slots, tools, bbox = best
    prog.holes = holes
    prog.slots = slots
    prog.bbox = bbox
    prog.units = "inch" if units == "in" else "mm"
    if plated is not None:
        prog.plated = bool(plated)
    for num, cnt in tools.items():
        d = _tool_dia(tools_raw.get(num, "0"), unit_scale, units)
        prog.tools[num] = {"d": d, "count": cnt, "plated": prog.plated}
    prog.stats = {"holes": len(holes), "slots": len(slots), "tools": len(prog.tools)}
    if score <= 0 and (holes or slots):
        prog.warnings.append("坐标尺度可疑，请确认 Excellon 单位/格式声明")
    if not holes and not slots:
        prog.warnings.append("未解析出任何钻孔（可能是二进制 Excellon 或格式特殊）")
    return prog


def _tool_dia(raw: str, unit_scale: float, units: str) -> float:
    try:
        if "." in raw:
            return float(raw) * unit_scale
        v = int(raw)
        # 无小数点：按 1/1000 单位解释（inch: 035 -> 0.035in；metric: 800 -> 0.8mm）
        return (v / 1000.0) * unit_scale
    except Exception:
        return 0.0


def _plausible(bbox, count) -> int:
    if bbox is None:
        return 0
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    diag = math.hypot(w, h)
    if count <= 0:
        return 0
    if 2.0 <= diag <= 1500.0:
        return 2
    if 0.2 <= diag < 2.0:
        return 1
    return -1
