"""XY / 元件中心坐标数据的导入、对齐、校验与导出（纯标准库）。

支持的常见导出格式：
  - Altium / Protel      ``Designator,Comment,Layer,Footprint,Mid X,Mid Y,Rotation``
  - KiCad .pos           ``### Module positions`` / ``## Unit = mm`` / ``Ref Val Package PosX PosY Rot Side``
  - PADS / Allegro / CAM350 / 各家 txt、csv、tsv：列名可以是 Ref/RefDes/位号、X/MidX/PosX、Y、Rot/Angle
  - 无表头               ``R1 10.5 20.5 90 top``（位号 + 两个数 + 可选角度/面）

对外单位统一为 **mm**，坐标系与 Gerber 一致（Y 向上）。
"""
from __future__ import annotations

import os
import re

from . import geom, units

# --------------------------------------------------------------------------- #
# 列名识别
# --------------------------------------------------------------------------- #
_KEYS = {
    "ref": ({"ref", "refdes", "designator", "desig", "des", "comp", "component", "part",
             "partref", "refno", "refnum", "位号", "元件", "元件号", "元件位号", "零件", "零件号"},
            ("refdes", "designator", "位号", "元件位号")),
    "x": ({"x", "midx", "centerx", "posx", "refx", "locx", "coordx", "xpos", "x坐标", "坐标x",
           "xmm", "xin", "xmil", "xcenter"},
          ("midx", "centerx", "posx", "refx", "locx", "coordx", "xmm", "x坐标", "坐标x")),
    "y": ({"y", "midy", "centery", "posy", "refy", "locy", "coordy", "ypos", "y坐标", "坐标y",
           "ymm", "yin", "ymil", "ycenter"},
          ("midy", "centery", "posy", "refy", "locy", "coordy", "ymm", "y坐标", "坐标y")),
    "rot": ({"rot", "rotation", "angle", "orient", "orientation", "rotate", "角度", "旋转", "旋转角"},
            ("rotation", "orient", "angle", "角度")),
    "side": ({"side", "layer", "tb", "mountside", "boardside", "面", "层", "所在层", "板面"},
             ("side", "boardside", "mountside")),
    "package": ({"package", "footprint", "pattern", "pkg", "footprintname", "封装", "封装名"},
                ("package", "footprint", "pattern", "封装")),
    "value": ({"value", "comment", "val", "comment1", "值", "参数", "标称", "标称值", "型号"},
              ("value", "comment", "标称")),
}
_KEY_ORDER = ("ref", "x", "y", "rot", "side", "package", "value")

_UNIT_FACTORS = {
    "mm": 1.0, "millimeter": 1.0, "millimetre": 1.0, "millimeters": 1.0, "毫米": 1.0,
    "cm": 10.0, "um": 0.001, "micron": 0.001,
    "inch": 25.4, "inches": 25.4, "in": 25.4, "英寸": 25.4,
    "mil": 0.0254, "mils": 0.0254, "thou": 0.0254, "毫英寸": 0.0254,
}

_REF_RE = re.compile(r"^[A-Za-z]{1,5}\d{1,4}[A-Za-z]?$")
_NUM_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def _norm_key(s):
    """列名归一化：'Mid X (mm)' -> 'midxmm'，'位号' -> '位号'。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", str(s or "").lower())


def _num(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip().replace(" ", "")
    if not t:
        return None
    m = _NUM_RE.search(t)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _side_of(text):
    v = _norm_key(text)
    if not v:
        return ""
    if v.startswith("b") or v in ("2", "bot", "bottom", "back", "下", "底层", "背面"):
        return "bottom"
    if v.startswith("t") or v.startswith("f") or v in ("1", "top", "front", "上", "顶层", "正面"):
        return "top"
    if v.startswith("l") and len(v) <= 3:
        return "bottom" if v.startswith("l2") else "top"
    return ""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
class XyData:
    def __init__(self):
        self.name = ""
        self.path = ""
        self.entries = []          # 已对齐（mm）
        self.base = []             # 文件原始数值（文件单位）
        self.units = ""            # 文件里声明的单位（可能为空）
        self.unit_factor = 1.0     # 实际采用的比例
        self.yflip = False
        self.header = []
        self.columns = {}
        self.warnings = []
        self.bbox = None
        self.raw_rows = 0

    # -- 对外 --
    def to_dict(self, limit=None):
        items = self.entries if limit is None else self.entries[:limit]
        return {
            "name": self.name,
            "path": self.path,
            "units": self.units or ("mm" if self.unit_factor == 1.0 else
                                    "inch" if self.unit_factor == 25.4 else "mil"),
            "unit_factor": self.unit_factor,
            "yflip": self.yflip,
            "count": len(self.entries),
            "bbox": [round(v, 4) for v in self.bbox] if self.bbox else None,
            "columns": {k: (self.header[i] if 0 <= i < len(self.header) else str(i))
                        for k, i in self.columns.items()},
            "warnings": self.warnings[:40],
            "entries": items,
        }


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _detect_delim(lines):
    for d in ("\t", ",", ";", "|"):
        hits = sum(1 for ln in lines[:60] if ln.count(d) >= 2)
        if hits >= max(2, len(lines[:60]) // 4):
            return d
    return None


def _split(line, delim):
    if delim is None:
        return line.split()
    out, cur, q = [], [], None
    for ch in line:
        if q:
            if ch == q:
                q = None
            else:
                cur.append(ch)
        elif ch in ("'", '"'):
            q = ch
        elif ch == delim:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return out


def _map_columns(header):
    norm = [_norm_key(h) for h in header]
    assigned, used = {}, set()
    for key in _KEY_ORDER:                       # 先整体精确匹配
        exact = _KEYS[key][0]
        for i, n in enumerate(norm):
            if i in used or not n:
                continue
            if n in exact:
                assigned[key] = i
                used.add(i)
                break
    for key in _KEY_ORDER:                       # 再前缀/包含
        if key in assigned:
            continue
        pref = _KEYS[key][1]
        for i, n in enumerate(norm):
            if i in used or not n:
                continue
            if any(n.startswith(p) or p in n for p in pref):
                assigned[key] = i
                used.add(i)
                break
    return assigned


def _find_header(lines, delim):
    limit = min(len(lines), 80)
    for i in range(limit):
        cells = _split(lines[i], delim)
        if len(cells) < 2:
            continue
        cols = _map_columns(cells)
        if "x" in cols and "y" in cols:
            return i, cells, cols
    return None, None, None


def parse_xy(text, name="", path=""):
    """解析 XY 数据文本，坐标保留文件原始数值（未缩放）。"""
    data = XyData()
    data.name = name or (os.path.basename(path) if path else "xy")
    data.path = path
    if not text:
        data.warnings.append("文件为空")
        return data

    lines = [ln.rstrip("\r\n") for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]

    # 注释里带的单位信息（KiCad / CAM350 常见）
    for ln in lines[:40]:
        if ln.lstrip().startswith(("#", "//", ";")):
            m = re.search(r"unit[s]?\s*[=:]\s*([A-Za-z\u4e00-\u9fff]+)", ln, re.I)
            if m:
                u = _norm_key(m.group(1))
                if u in _UNIT_FACTORS:
                    data.units = u
            continue

    body = [ln for ln in lines if ln.strip() and not re.match(r"^\s*(#|//|;)", ln)]
    if not body:
        data.warnings.append("没有可用数据行（全是注释或空行）")
        return data

    delim = _detect_delim(body)
    hi, header, cols = _find_header(body, delim)
    rows = []
    positional = header is None
    if not positional:
        data.header = header
        data.columns = cols
        for ln in body[hi + 1:]:
            rows.append(_split(ln, delim))
    else:
        # 无表头：位号 + 两个数（可选角度、面）
        data.columns = {"ref": 0, "x": 1, "y": 2}
        for ln in body:
            cells = _split(ln, delim)
            ref_i = None
            for i, c in enumerate(cells[:3]):
                if _REF_RE.match(c.strip()):
                    ref_i = i
                    break
            if ref_i is None:
                continue
            nums, side = [], ""
            for j in range(ref_i + 1, len(cells)):
                v = _num(cells[j])
                if v is not None:
                    nums.append(v)
                elif not side:
                    side = _side_of(cells[j])
            if len(nums) < 2:
                continue
            data.base.append({
                "ref": cells[ref_i].strip(),
                "x": nums[0], "y": nums[1],
                "rot": nums[2] if len(nums) > 2 else None,
                "side": side, "package": "", "value": "",
            })
        if data.base:
            data.header = ["Ref", "X", "Y"]
            data.warnings.append("未找到表头，按「位号 X Y [角度] [面]」解析")

    # 单位来自表头（如 "X (mil)"）
    for h in data.header or []:
        u = _norm_key(h)
        for key in ("mil", "inch", "mm", "um"):
            if u.endswith(key) and key in _UNIT_FACTORS:
                data.units = key
                break
        if data.units:
            break

    if not positional:
        cols = data.columns
        for cells in rows:
            if not cells:
                continue
            data.raw_rows += 1

            def cell(k):
                i = cols.get(k)
                if i is None or i >= len(cells):
                    return ""
                return cells[i].strip()

            x, y = _num(cell("x")), _num(cell("y"))
            if x is None or y is None:
                continue
            data.base.append({
                "ref": cell("ref") or ("#%d" % len(data.base)),
                "x": x, "y": y,
                "rot": _num(cell("rot")),
                "side": _side_of(cell("side")),
                "package": cell("package"),
                "value": cell("value"),
            })
    data.raw_rows = max(data.raw_rows, len(data.base))

    data.entries = [dict(e) for e in data.base]
    if not data.base:
        data.warnings.append("没有识别出任何坐标行（检查表头列名或分隔符）")
    data.unit_factor = _UNIT_FACTORS.get(data.units, 1.0)
    for e in data.entries:
        e["x"] *= data.unit_factor
        e["y"] *= data.unit_factor
    _recalc_bbox(data)
    return data


def _recalc_bbox(data):
    xs = [e["x"] for e in data.entries]
    ys = [e["y"] for e in data.entries]
    data.bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else None


# --------------------------------------------------------------------------- #
# 对齐（单位 + Y 方向）
# --------------------------------------------------------------------------- #
def _score(pts, bb):
    if not bb or not pts:
        return 0.0
    x0, y0, x1, y1 = bb
    pad = 3.0
    inside = sum(1 for x, y in pts if x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad)
    ratio = inside / len(pts)
    w = max(p[0] for p in pts) - min(p[0] for p in pts)
    h = max(p[1] for p in pts) - min(p[1] for p in pts)
    bw, bh = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
    fit = 1.0 - min(1.0, (abs(w - bw) / bw + abs(h - bh) / bh) / 2.0)
    return ratio * 3.0 + max(0.0, fit)


def _score_pads(idx, pts, tol=0.6, probe=40, radius=3.0):
    """候选朝向的「离焊盘有多近」得分（0..1）。

    注意不能用「点是否落在焊盘内」判断：元件坐标通常是封装**中心**，
    落在两排焊盘之间，正确的坐标也命中不了任何焊盘。
    改用「到最近焊盘的距离」做连续打分，正确朝向的平均距离会明显更小。
    """
    if idx is None or not pts:
        return 0.0
    step = max(1, len(pts) // probe)
    sample = pts[::step]
    acc = 0.0
    for x, y in sample:
        best = idx.nearest(x, y, radius)
        d = radius if best is None else best[0]
        acc += max(0.0, 1.0 - d / radius)
    return acc / float(len(sample))


def align(data, board_bbox=None, units="auto", yflip="auto", board=None):
    """把 XY 数据对齐到板坐标系：自动判断 单位（mm/inch/mil）与 Y 方向。

    ``board`` 传入 Board 时，会用板上焊盘做命中率打分（比板框贴合度可靠得多），
    能正确识别 CAD 常见的「Y 轴向下」导出。
    """
    if not data.base:
        return data
    if board is not None and not board_bbox:
        board_bbox = getattr(board, "bbox", None)

    idx = None
    if board is not None:
        try:
            idx = PadIndex(board)
        except Exception:
            idx = None

    cands = []
    if units and units != "auto":
        f = _UNIT_FACTORS.get(_norm_key(units))
        if f:
            cands = [f]
    if not cands:
        cands = [data.unit_factor or 1.0]
        for f in (1.0, 25.4, 0.0254):
            if f not in cands:
                cands.append(f)

    flips = [False, True] if yflip == "auto" else [str(yflip).lower() in ("1", "true", "yes", "on")]

    best = None
    for f in cands:
        pts = [(e["x"] * f, e["y"] * f) for e in data.base]
        cy = ((max(p[1] for p in pts) + min(p[1] for p in pts)) / 2.0) if pts else 0.0
        if board_bbox:
            cy = (board_bbox[1] + board_bbox[3]) / 2.0
        for fl in flips:
            test = [(x, (2 * cy - y) if fl else y) for x, y in pts]
            s = _score(test, board_bbox)
            hp = _score_pads(idx, test)          # 0..1
            s += hp * 10.0                       # 焊盘命中是压倒性证据
            if best is None or s > best[0]:
                best = (s, f, fl, cy, hp)
    _s, factor, flip, cy, _hp = best

    out = []
    for e in data.base:
        x = e["x"] * factor
        y = e["y"] * factor
        if flip:
            y = 2 * cy - y
        d = dict(e)
        d["x"], d["y"] = x, y
        out.append(d)
    data.entries = out
    data.unit_factor = factor
    data.yflip = flip
    if not data.units:
        data.units = "mm" if factor == 1.0 else ("inch" if factor == 25.4 else "mil")
    _recalc_bbox(data)
    return data


# --------------------------------------------------------------------------- #
# 焊盘索引与校验
# --------------------------------------------------------------------------- #
class PadIndex:
    """铜层焊盘的网格索引，用来快速判定「某坐标上有没有焊盘」。

    只收录「像焊盘」的图元：走线（path）与大面积铺铜（>12mm）不算焊盘，
    否则铺铜会把任意坐标都判成"有焊盘"，校验就没意义了。
    """

    MAX_PAD = 12.0

    def __init__(self, board, cell=2.0):
        self.cell = cell
        self.buckets = {}
        self.big = []
        for layer in board.layers:
            if layer.get("kind") != "copper":
                continue
            side = layer.get("side") or ""
            if side not in ("top", "bottom"):
                nm = (layer.get("name") or "").lower()
                side = "bottom" if ("bot" in nm or "l2" in nm) else "top"
            for o in layer.get("objects") or []:
                bb = geom.obj_bbox(o)
                if not bb or not _pad_like(o, bb):
                    continue
                item = (bb, o, side)
                span = (bb[2] - bb[0]) * (bb[3] - bb[1])
                if span > 400.0:                      # 大面积铺铜单独放，避免撑爆格子
                    self.big.append(item)
                    continue
                for cx in range(int(bb[0] // cell), int(bb[2] // cell) + 1):
                    for cy in range(int(bb[1] // cell), int(bb[3] // cell) + 1):
                        self.buckets.setdefault((cx, cy), []).append(item)

    def at(self, x, y, tol=0.0):
        c = (int(x // self.cell), int(y // self.cell))
        pool = self.buckets.get(c) or []
        hits = []
        for bb, o, side in list(pool) + self.big:
            if bb[0] - tol <= x <= bb[2] + tol and bb[1] - tol <= y <= bb[3] + tol:
                if geom.pad_covers(o, x, y, tol):
                    hits.append((o, side))
        return hits

    def nearest(self, x, y, radius=4.0):
        """半径内离该点最近的焊盘，返回 (dist, obj, side) 或 None。"""
        n = int(radius // self.cell) + 1
        cx, cy = int(x // self.cell), int(y // self.cell)
        best = None
        seen = set()
        for gx in range(cx - n, cx + n + 1):
            for gy in range(cy - n, cy + n + 1):
                for item in self.buckets.get((gx, gy)) or ():
                    if id(item) in seen:
                        continue
                    seen.add(id(item))
                    if _bb_dist(item[0], x, y) > radius:
                        continue
                    d = geom.obj_dist(item[1], x, y)
                    if d <= radius and (best is None or d < best[0]):
                        best = (d, item[1], item[2])
        for item in self.big:
            d = geom.obj_dist(item[1], x, y)
            if d <= radius and (best is None or d < best[0]):
                best = (d, item[1], item[2])
        return best


def _pad_like(obj, bb):
    """判断一个图元像不像焊盘（排除走线与大面积铺铜）。"""
    t = obj.get("t")
    if t == "path":
        return False
    if t in ("circle", "ring") and obj.get("r", 0) > PadIndex.MAX_PAD / 2.0:
        return False
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    return max(w, h) <= PadIndex.MAX_PAD


def _bb_dist(bb, x, y):
    dx = max(bb[0] - x, 0.0, x - bb[2])
    dy = max(bb[1] - y, 0.0, y - bb[3])
    if dx <= 0 and dy <= 0:
        return 0.0
    return (dx * dx + dy * dy) ** 0.5


def infer_sides(components, board, tol=1.0):
    """按几何焊盘推断器件所在面（顶层/底层）。returns 修正条数。"""
    if not components or not board.layers:
        return 0
    idx = PadIndex(board)
    fixed = 0
    for c in components:
        try:
            x, y = float(c.get("x")), float(c.get("y"))
        except (TypeError, ValueError):
            continue
        sides = {s for _o, s in idx.at(x, y, tol)}
        if not sides:
            nb = idx.nearest(x, y, 3.0)
            if nb:
                sides = {nb[2]}
        if len(sides) == 1:
            s = sides.pop()
            if c.get("side") != s:
                c["side"] = s
                fixed += 1
    return fixed


def check(data, board, tol=0.5, radius=4.0):
    """拿 XY 数据和板上的铜箔焊盘/器件做交叉校验。

    tol    —— 点是否直接落在焊盘上的容差
    radius —— 器件中心通常不在焊盘上（比如 SOIC 两排之间），允许找半径内最近焊盘
    """
    idx = PadIndex(board)
    bb = board.bbox
    comps = {}
    for c in board.components or []:
        r = (c.get("ref") or "").strip()
        if r:
            comps[r] = c

    stats = {}
    out, maxdev = [], 0.0
    seen = set()
    for e in data.entries:
        x, y = e["x"], e["y"]
        rec = {"ref": e["ref"], "x": round(x, 4), "y": round(y, 4),
               "side": e.get("side") or "", "rot": e.get("rot"),
               "package": e.get("package") or "", "value": e.get("value") or "",
               "status": "ok", "dev": None, "note": "", "pad": None}
        seen.add(e["ref"])
        if bb and not (bb[0] - 0.5 <= x <= bb[2] + 0.5 and bb[1] - 0.5 <= y <= bb[3] + 0.5):
            rec["status"] = "outside"
            rec["note"] = "超出板框"
        else:
            hits = idx.at(x, y, tol)
            sides = {s for _o, s in hits}
            if hits:
                rec["pad"] = 0.0
            else:
                nb = idx.nearest(x, y, radius)
                if nb:
                    rec["pad"] = round(nb[0], 3)
                    sides = {nb[2]}
                    rec["note"] = "中心距最近焊盘 %.2f mm" % nb[0]
                else:
                    rec["status"] = "no_pad"
                    rec["note"] = "%.1f mm 内没有铜箔焊盘" % radius
            if rec["status"] == "ok" and rec["side"] and sides and rec["side"] not in sides:
                rec["status"] = "wrong_side"
                rec["note"] = "焊盘在%s" % "/".join(
                    {"top": "顶层", "bottom": "底层"}.get(x, x) for x in sorted(sides))
        c = comps.get(e["ref"])
        if c:
            try:
                dev = ((float(c["x"]) - x) ** 2 + (float(c["y"]) - y) ** 2) ** 0.5
                rec["dev"] = round(dev, 4)
                if dev > maxdev:
                    maxdev = dev
                if dev > max(tol, 0.05) and rec["status"] == "ok":
                    rec["status"] = "offset"
                    rec["note"] = "与板上器件相差 %.3f mm" % dev
            except (KeyError, TypeError, ValueError):
                pass
        stats[rec["status"]] = stats.get(rec["status"], 0) + 1
        out.append(rec)

    board_only = sorted(r for r in comps if r not in seen)
    return {
        "total": len(out),
        "stats": stats,
        "tolerance": tol,
        "radius": radius,
        "max_offset": round(maxdev, 4),
        "bbox": [round(v, 4) for v in data.bbox] if data.bbox else None,
        "ref_matched": sum(1 for r in out if r["dev"] is not None),
        "board_components": len(comps),
        "board_only": board_only[:300],
        "board_only_count": len(board_only),
        "entries": out,
    }


# --------------------------------------------------------------------------- #
# 导出（坐标可输出 mm / mil / inch，或双单位并排）
# --------------------------------------------------------------------------- #
_UNIT_HEAD = {"mm": "mm", "mil": "mil", "inch": "in"}


def csv_header(unit="mm"):
    """unit: mm / mil / inch / both（both = mm 与 mil 并列两列）。"""
    u = units.norm(unit, default="mm")
    if str(unit).lower() == "both":
        return ["RefDes", "Side", "X(mm)", "X(mil)", "Y(mm)", "Y(mil)", "Rotation",
                "Package", "Value", "Status", "Dev(mm)", "Dev(mil)", "Source"]
    h = _UNIT_HEAD.get(u, "mm")
    return ["RefDes", "Side", "X(%s)" % h, "Y(%s)" % h, "Rotation", "Package",
            "Value", "Status", "Dev(%s)" % h, "Source"]


# 兼容旧名字
_CSV_HEADER = csv_header("mm")


def _coord(v_mm, unit, both_flags):
    """按单位输出坐标；both_flags 为真时返回 [mm, mil] 两个值。"""
    if v_mm is None or v_mm == "":
        return ["", ""] if both_flags else [""]
    try:
        f = float(v_mm)
    except (TypeError, ValueError):
        return [v_mm, v_mm] if both_flags else [v_mm]
    if both_flags:
        return [units.fmt(f, "mm"), units.fmt(f, "mil")]
    return [units.fmt(f, units.norm(unit, default="mm"))]


def rows_from_board(board, unit="mm"):
    both = str(unit).lower() == "both"
    rows = []
    for c in board.components or []:
        rows.append([
            c.get("ref") or "", c.get("side") or "",
            *_coord(c.get("x"), unit, both), *_coord(c.get("y"), unit, both),
            _f(c.get("rot")),
            c.get("package") or "", c.get("value") or "",
            "", "", *([""] if both else []), "ODB++",
        ])
    return rows


def rows_from_xy(data, checked=None, unit="mm"):
    both = str(unit).lower() == "both"
    by_ref = {}
    if checked:
        by_ref = {r["ref"]: r for r in checked.get("entries") or []}
    rows = []
    for e in data.entries:
        c = by_ref.get(e["ref"]) or {}
        rows.append([
            e["ref"], e.get("side") or "",
            *_coord(e.get("x"), unit, both), *_coord(e.get("y"), unit, both),
            _f(e.get("rot")),
            e.get("package") or "", e.get("value") or "",
            c.get("status") or "",
            *_coord(c.get("dev"), unit, both), "XY",
        ])
    return rows


def to_csv(rows, header=None, unit="mm"):
    header = header or csv_header(unit)
    def q(v):
        s = "" if v is None else str(v)
        if any(ch in s for ch in ',"\n'):
            s = '"' + s.replace('"', '""') + '"'
        return s
    out = ["\ufeff" + ",".join(header)]
    for r in rows:
        out.append(",".join(q(v) for v in r))
    return "\r\n".join(out) + "\r\n"


def _f(v):
    if v is None or v == "":
        return ""
    try:
        return "%.4f" % float(v)
    except (TypeError, ValueError):
        return str(v)
