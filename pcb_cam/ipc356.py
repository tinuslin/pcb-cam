"""IPC-D-356 / IPC-D-356A 网表解析器。

记录格式（定宽列 + 空格分隔的混合写法均兼容）：
  C ...            注释
  P JOB/UNITS/...  参数
  317 / 327 / 127  测试点记录（第 2 位：1=通孔，2=贴片；第 3 位固定 7）
  999              结束

测试点记录关键字段：
  净名 / 位号 / 引脚 / 孔径与镀通 / 测试面 / X,Y 坐标 / 焊盘尺寸 / 旋转 / 阻焊
"""
from __future__ import annotations

import re

_KEY_RE = re.compile(r"([A-Z])([+-]?\d+(?:\.\d+)?)")
_REF_RE = re.compile(r"\s+([A-Za-z][A-Za-z0-9_.]*)\s*-\s*([A-Za-z0-9_.]+)")


class Netlist:
    def __init__(self, name="", source=""):
        self.name = name
        self.source = source
        self.nets = {}          # net -> [point]
        self.points = []
        self.warnings = []
        self.units = "inch"

    def add(self, net, ref, pin, x, y, **kw):
        pt = {"net": net, "ref": ref, "pin": pin, "x": round(x, 4), "y": round(y, 4)}
        pt.update(kw)
        self.points.append(pt)
        self.nets.setdefault(net, []).append(pt)

    def to_dict(self):
        return {
            "name": self.name,
            "source": self.source,
            "units": self.units,
            "net_count": len(self.nets),
            "point_count": len(self.points),
            "nets": {k: len(v) for k, v in sorted(self.nets.items())},
            "points": self.points[:4000],
            "warnings": self.warnings,
        }


def parse_ipc356(text: str, name: str = "", source: str = "") -> Netlist:
    nl = Netlist(name, source or name)
    scale = 25.4        # CUST -> 英寸
    div = 10000.0       # 坐标分辨率
    metric = False

    for raw in text.replace("\r", "").split("\n"):
        s = raw.rstrip()
        if not s.strip():
            continue
        su = s.upper()
        if su.startswith("P UNITS"):
            if "METRIC" in su or "MM" in su:
                metric = True
                scale = 1.0
                div = 1000.0
                nl.units = "mm"
            elif "CUST" in su or "INCH" in su:
                metric = False
                scale = 25.4
                div = 10000.0
                nl.units = "inch"
            continue
        if su.startswith("C") and not su.startswith("CMP"):
            if s.startswith("C ") or s.startswith("C\t") or su.startswith("C "):
                continue
        if su.startswith("999"):
            break
        if not re.match(r"^\d{3}", s):
            continue
        code = s[:3]
        if code[2] != "7":
            continue
        rest = s[3:]

        mref = _REF_RE.search(rest)
        if mref:
            net = rest[:mref.start()].strip()
            ref, pin = mref.group(1), mref.group(2)
            tail = rest[mref.end():]
        else:
            parts = rest.split(None, 1)
            net = parts[0].strip() if parts else ""
            ref, pin = "VIA", ""
            tail = parts[1] if len(parts) > 1 else ""

        coords = re.findall(r"X([+-]?\d+)(?:\.\d+)?Y([+-]?\d+)", tail)
        if not coords:
            continue
        try:
            xv = float(coords[0][0]) / div * scale
            yv = float(coords[0][1]) / div * scale
        except ValueError:
            continue
        pad = None
        if len(coords) > 1:
            try:
                pad = (float(coords[1][0]) / div * scale,
                       float(coords[1][1]) / div * scale)
            except ValueError:
                pad = None

        hole = None
        plated = None
        mh = re.search(r"D(\d+)([PU])", tail)
        if mh:
            hole = float(mh.group(1)) / div * scale
            plated = mh.group(2) == "P"

        ms = re.search(r"A(\d{2})", tail)
        side = ms.group(1) if ms else ""
        mr = re.search(r"R(\d{1,3})", tail)
        rot = float(mr.group(1)) if mr else 0.0
        mm_ = re.search(r"S(\d)", tail)
        mask = int(mm_.group(1)) if mm_ else None

        kind = "pth" if code[1] == "1" else ("smd" if code[1] == "2" else "other")
        nl.add(net, ref, pin, xv, yv, hole=hole, plated=plated, side=side,
               rot=rot, mask=mask, kind=kind, code=code, pad=pad)

    if not nl.points:
        nl.warnings.append("未解析出任何测试点（可能不是 IPC-D-356 文件）")
    return nl


def compare(a: Netlist, b: Netlist):
    """比较两份网表，返回差异摘要。"""
    na, nb = set(a.nets), set(b.nets)
    only_a = sorted(na - nb)
    only_b = sorted(nb - na)
    diff = []
    for net in sorted(na & nb):
        ca, cb = len(a.nets[net]), len(b.nets[net])
        if ca != cb:
            diff.append({"net": net, "a": ca, "b": cb})
    return {
        "a": {"name": a.name, "nets": len(na), "points": len(a.points)},
        "b": {"name": b.name, "nets": len(nb), "points": len(b.points)},
        "only_a": only_a[:200],
        "only_a_count": len(only_a),
        "only_b": only_b[:200],
        "only_b_count": len(only_b),
        "count_diff": diff[:200],
        "count_diff_total": len(diff),
    }
