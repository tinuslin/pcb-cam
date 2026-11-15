"""DXF（AutoCAD ASCII 交换格式）解析 + DWG 转换桥接（纯标准库）。

为什么是 DXF：DWG 是闭源二进制格式，标准库无法直接解码；所有 CAD
（AutoCAD / LibreCAD / KiCad / Altium / 立创 EDA …）都能另存为 ASCII DXF。
因此本模块解析 DXF，并把 ``.dwg`` 交给**本机已安装的转换器**（可选），
没有转换器时给出明确的导出指引，而不是假装能读二进制。

输出坐标统一折算为 **mm**，Y 轴向上（与 Gerber 一致）。
输出图元复用 Gerber 的对象模型：path / poly / circle / ring / poly2 / group，
另新增 ``text`` 图元（TEXT / MTEXT / ATTDEF）。

支持的实体：
    LINE, CIRCLE, ARC, ELLIPSE, LWPOLYLINE(含 bulge 弧), POLYLINE+VERTEX,
    POINT, SOLID/3DFACE/TRACE, TEXT, MTEXT, INSERT(块展开，含阵列),
    DIMENSION(引用匿名块), HATCH(按轮廓), SPLINE(折线逼近), LEADER
单位：$INSUNITS（16 种）/ $MEASUREMENT，缺省按 mm 并给出提示。
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile

from . import geom

# $INSUNITS -> 毫米系数（DXF R2000 起）
_INSUNITS_MM = {
    0: 1.0, 1: 25.4, 2: 304.8, 3: 1609344.0, 4: 1.0, 5: 10.0,
    6: 1000.0, 7: 1_000_000.0, 8: 0.0000254, 9: 0.0254, 10: 914.4,
    11: 1e-7, 12: 1e-6, 13: 0.001, 14: 100.0, 15: 10000.0,
    16: 100000.0, 17: 1e12, 18: 1.495978707e11,
}
_INSUNITS_NAME = {
    0: "unitless", 1: "inch", 2: "feet", 3: "miles", 4: "mm", 5: "cm",
    6: "m", 7: "km", 8: "µin", 9: "mil", 10: "yards", 11: "Å",
    12: "nm", 13: "µm", 14: "dm", 15: "dam", 16: "hm", 17: "Gm",
}

DWG_HINT = (
    "DWG 是二进制格式，纯标准库不能直接解码。请在 CAD 里「另存为 AutoCAD DXF "
    "（ASCII / R12 以上）」后再打开；或在服务器上安装转换器并设置环境变量 "
    "PCB_CAM_DWG2DXF（LibreDWG dwg2dxf）或 PCB_CAM_ODA（ODA File Converter），"
    "即可直接双击 .dwg 打开。"
)

_STROKE = 0.05          # DXF 无宽度线条的默认描边宽度（mm）


# --------------------------------------------------------------------------- #
# 基础读取
# --------------------------------------------------------------------------- #
def is_dxf(text: str) -> bool:
    """粗略判断文本是否为 ASCII DXF（供 sniff_text 使用）。"""
    if not text:
        return False
    if text.lstrip().startswith("AutoCAD Binary DXF"):
        return True
    lines = [ln.strip() for ln in text[:4000].replace("\r\n", "\n").split("\n")]
    for i, ln in enumerate(lines[:24]):
        u = ln.upper()
        if u == "SECTION" and i > 0 and lines[i - 1].lstrip("-").isdigit():
            return True
        if u in ("ENTITIES", "HEADER", "BLOCKS") and i > 0 and lines[i - 1] == "2":
            return True
    return False


def _pairs(text):
    """把 DXF 拆成 (组码, 值) 序列。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    i = 0
    n = len(lines)
    while i + 1 < n:
        c = lines[i].strip()
        if not c:
            i += 1
            continue
        try:
            code = int(c)
        except ValueError:
            i += 1
            continue
        out.append((code, lines[i + 1].rstrip()))
        i += 2
    return out


def _records(pairs):
    """按组码 0 切分实体记录。"""
    recs, cur = [], None
    for code, val in pairs:
        if code == 0:
            if cur is not None:
                recs.append(cur)
            cur = {"type": val.strip().upper(), "g": []}
        elif cur is not None:
            cur["g"].append((code, val))
    if cur is not None:
        recs.append(cur)
    return recs


def _g1(rec, code, default=None):
    for c, v in rec["g"]:
        if c == code:
            return v.strip()
    return default


def _f(rec, code, default=0.0):
    v = _g1(rec, code)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _i(rec, code, default=0):
    try:
        return int(float(_g1(rec, code, default)))
    except (ValueError, TypeError):
        return default


def _all(rec, code):
    return [v.strip() for c, v in rec["g"] if c == code]


def _header(pairs):
    """抓 HEADER 变量：$INSUNITS / $MEASUREMENT / $ACADVER。"""
    out = {}
    for i, (code, val) in enumerate(pairs):
        if code == 9 and val.strip().startswith("$") and i + 1 < len(pairs):
            out[val.strip().upper()] = pairs[i + 1][1].strip()
    return out


def _sections(records):
    out, cur = {}, None
    for rec in records:
        t = rec["type"]
        if t == "SECTION":
            cur = (_g1(rec, 2, "") or "").strip().upper()
            out.setdefault(cur, []).append(rec)
        elif t == "ENDSEC":
            cur = None
        elif t == "EOF":
            break
        elif cur:
            out.setdefault(cur, []).append(rec)
    return out


# --------------------------------------------------------------------------- #
# 数值 / 几何小工具
# --------------------------------------------------------------------------- #
def _num(v, default=0.0):
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return default


def _xy_pairs(rec, xc=10, yc=20):
    """LWPOLYLINE / HATCH / SPLINE 的成对坐标（保持出现顺序）。"""
    pts, px = [], None
    for c, v in rec["g"]:
        if c == xc:
            px = _num(v, None)
        elif c == yc and px is not None:
            pts.append((px, _num(v)))
            px = None
    return pts


def _bulge_pts(p1, p2, b):
    """带凸度(bulge)的线段展平成弧点（不含起点，含终点）。"""
    if abs(b) < 1e-9:
        return [p2]
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    L = math.hypot(dx, dy)
    if L < 1e-12:
        return [p2]
    r = L * (1.0 + b * b) / (4.0 * abs(b))
    h = L * (1.0 - b * b) / (4.0 * b)
    mx, my = (p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0
    cx, cy = mx - h * dy / L, my + h * dx / L
    a0 = math.atan2(p1[1] - cy, p1[0] - cx)
    sweep = 4.0 * math.atan(b)
    arc = geom.arc_points(cx, cy, r, a0, sweep, tol=0.02)
    return arc[1:] or [p2]


def _catmull(pts, steps=6):
    """样条控制点做 Catmull-Rom 折线逼近（DXF SPLINE 的简化）。"""
    if len(pts) < 3:
        return pts
    out = []
    for i in range(len(pts) - 1):
        p0 = pts[max(0, i - 1)]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[min(len(pts) - 1, i + 2)]
        for k in range(steps):
            t = k / float(steps)
            t2, t3 = t * t, t * t * t
            out.append(tuple(
                0.5 * ((2 * p1[j]) + (-p0[j] + p2[j]) * t
                       + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t2
                       + (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t3)
                for j in (0, 1)))
    out.append(pts[-1])
    return out


_MTEXT_RE = re.compile(r"\\[A-Za-z][^;]*;|\\P|\\~|[{}]")


def _clean_text(s):
    s = _MTEXT_RE.sub(lambda m: "\n" if m.group(0) == "\\P" else "", s or "")
    return s.replace("\n", " ").strip()


def _mtext(rec):
    parts = []
    for c, v in rec["g"]:
        if c in (1, 3):
            parts.append(v.strip())
    return _clean_text("".join(parts))


# --------------------------------------------------------------------------- #
# 仿射变换（INSERT 块展开用）
# --------------------------------------------------------------------------- #
_ID = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mul(m1, m2):
    """先 m2 再 m1。矩阵按 (a,b,c,d,e,f)：x'=ax+cy+e, y'=bx+dy+f。"""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
            a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)


def _trs(tx, ty, sx=1.0, sy=1.0, deg=0.0):
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    rot = (ca, sa, -sa, ca, 0.0, 0.0)
    sca = (sx, 0.0, 0.0, sy, 0.0, 0.0)
    tra = (1.0, 0.0, 0.0, 1.0, tx, ty)
    return _mul(tra, _mul(rot, sca))


def _apply(m, p):
    a, b, c, d, e, f = m
    return (a * p[0] + c * p[1] + e, b * p[0] + d * p[1] + f)


def _k(m):
    a, b, c, d = m[:4]
    return math.sqrt(abs(a * d - b * c)) or 1.0


def _xform_obj(o, m):
    """把一个图元按仿射矩阵变换；返回新对象。"""
    k = _k(m)
    t = o.get("t")
    if t in ("path", "poly"):
        r = dict(o)
        r["p"] = [_apply(m, p) for p in (o.get("p") or [])]
        if "w" in r:
            r["w"] = r["w"] * k
        return r
    if t == "poly2":
        r = dict(o)
        r["out"] = [_apply(m, p) for p in (o.get("out") or [])]
        r["in"] = [[_apply(m, p) for p in h] for h in (o.get("in") or [])]
        return r
    if t in ("circle", "ring"):
        r = dict(o)
        r["c"] = _apply(m, o["c"])
        r["r"] = o["r"] * k
        if "ri" in r:
            r["ri"] = r["ri"] * k
        return r
    if t == "text":
        r = dict(o)
        r["p"] = _apply(m, o["p"])
        r["h"] = o.get("h", 1.0) * k
        r["r"] = o.get("r", 0.0) + math.degrees(math.atan2(m[1], m[0]))
        return r
    if t == "group":
        r = dict(o)
        r["items"] = [_xform_obj(it, m) for it in (o.get("items") or [])]
        b = o.get("b")
        if b:
            corners = [_apply(m, p) for p in
                       ((b[0], b[1]), (b[2], b[1]), (b[0], b[3]), (b[2], b[3]))]
            r["b"] = (min(p[0] for p in corners), min(p[1] for p in corners),
                      max(p[0] for p in corners), max(p[1] for p in corners))
        return r
    return dict(o)


# --------------------------------------------------------------------------- #
# 实体 -> 图元
# --------------------------------------------------------------------------- #
class _Ctx:
    def __init__(self, scale, blocks, warnings):
        self.scale = scale
        self.blocks = blocks
        self.warnings = warnings
        self.types = {}
        self.depth = 0


def _sc(ctx, v):
    return v * ctx.scale


def _path(pts, w=_STROKE):
    return {"t": "path", "p": pts, "w": w}


def _entity(rec, ctx, vertices=None):
    """返回 (图元列表, 层名)。"""
    t = rec["type"]
    layer = _g1(rec, 8, "0") or "0"
    ctx.types[t] = ctx.types.get(t, 0) + 1
    s = ctx.scale

    def P(xc=10, yc=20):
        return (_sc(ctx, _f(rec, xc)), _sc(ctx, _f(rec, yc)))

    if t == "LINE":
        return [_path([P(10, 20), P(11, 21)])], layer
    if t == "CIRCLE":
        c, r = P(), _sc(ctx, _f(rec, 40))
        if r <= 0:
            return [], layer
        pts = geom.circle_poly(c[0], c[1], r, 48)
        return [_path(pts + [pts[0]])], layer
    if t == "ARC":
        c, r = P(), _sc(ctx, _f(rec, 40))
        a0 = _f(rec, 50)
        a1 = _f(rec, 51)
        if r <= 0:
            return [], layer
        sweep = a1 - a0
        while sweep <= 1e-9:
            sweep += 360.0
        pts = geom.arc_points(c[0], c[1], r, math.radians(a0),
                              math.radians(sweep), tol=0.02)
        return [_path(pts)], layer
    if t == "ELLIPSE":
        c = P()
        mx, my = _f(rec, 11), _f(rec, 21)
        ratio = _f(rec, 40, 1.0)
        a0, a1 = _f(rec, 41), _f(rec, 42)
        major = (mx * s, my * s)
        if math.hypot(*major) < 1e-12:
            return [], layer
        if a1 <= a0:
            a1 += 2 * math.pi
        n = max(16, min(240, int((a1 - a0) / 0.09) + 2))
        pts = []
        for k in range(n + 1):
            a = a0 + (a1 - a0) * k / n
            x = major[0] * math.cos(a) - (major[1] * ratio) * math.sin(a)
            y = major[1] * math.cos(a) + (major[0] * ratio) * math.sin(a)
            pts.append((c[0] + x, c[1] + y))
        return [_path(pts)], layer
    if t in ("LWPOLYLINE", "POLYLINE"):
        raw = _xy_pairs(rec) if t == "LWPOLYLINE" else [
            (_f(v, 10), _f(v, 20)) for v in (vertices or [])]
        if len(raw) < 2:
            return [], layer
        pts = [(_sc(ctx, x), _sc(ctx, y)) for x, y in raw]
        bulges = [_num(v, 0.0) for v in _all(rec, 42)] if t == "LWPOLYLINE" else []
        closed = bool(_i(rec, 70) & 1)
        out = [pts[0]]
        segs = pts + ([pts[0]] if closed else [])
        for k in range(len(segs) - 1):
            b = bulges[k] if k < len(bulges) else 0.0
            out.extend(_bulge_pts(segs[k], segs[k + 1], b))
        return [_path(out)], layer
    if t == "POINT":
        c = P()
        d = _sc(ctx, 0.25)
        return [_path([(c[0] - d, c[1]), (c[0] + d, c[1])]),
                _path([(c[0], c[1] - d), (c[0], c[1] + d)])], layer
    if t in ("SOLID", "3DFACE", "TRACE"):
        pts = []
        for a, b in ((10, 20), (11, 21), (12, 22), (13, 23)):
            x, y = _f(rec, a, None), _f(rec, b, None)
            if x is None or y is None:
                continue
            pts.append((_sc(ctx, x), _sc(ctx, y)))
        if len(pts) < 3:
            return [], layer
        # DXF SOLID 的第 3/4 点顺序是交叉的，改成外轮廓顺序
        if len(pts) == 4:
            pts = [pts[0], pts[1], pts[3], pts[2]]
        return [{"t": "poly", "p": pts}], layer
    if t in ("TEXT", "ATTRIB", "ATTDEF"):
        txt = _clean_text(_g1(rec, 1, "") or "")
        if not txt:
            return [], layer
        c = P()
        h = _sc(ctx, _f(rec, 40, 2.5)) or _sc(ctx, 2.5)
        if _i(rec, 72) or _i(rec, 73):
            ax, ay = _f(rec, 11, None), _f(rec, 21, None)
            if ax is not None and ay is not None:
                c = (_sc(ctx, ax), _sc(ctx, ay))
        return [{"t": "text", "p": [c[0], c[1]], "s": txt,
                 "h": h, "r": _f(rec, 50)}], layer
    if t == "MTEXT":
        txt = _mtext(rec)
        if not txt:
            return [], layer
        c = P()
        h = _sc(ctx, _f(rec, 40, 2.5)) or _sc(ctx, 2.5)
        return [{"t": "text", "p": [c[0], c[1]], "s": txt,
                 "h": h, "r": _f(rec, 50)}], layer
    if t == "SPLINE":
        pts = _xy_pairs(rec, 11, 21) or _xy_pairs(rec, 10, 20)
        if len(pts) < 2:
            return [], layer
        ctx.warnings.append("SPLINE 样条以折线逼近（%d 个控制点）" % len(pts))
        return [_path([(_sc(ctx, x), _sc(ctx, y)) for x, y in _catmull(pts)])], layer
    if t in ("LEADER", "HATCH"):
        pts = [(_sc(ctx, x), _sc(ctx, y)) for x, y in _xy_pairs(rec)]
        if len(pts) < 2:
            return [], layer
        if t == "HATCH":
            ctx.warnings.append("HATCH 填充按边界线条显示，不还原填充图案")
        return [_path(pts)], layer
    if t == "INSERT":
        return _insert(rec, ctx, layer)
    if t == "DIMENSION":
        name = _g1(rec, 2, "") or ""
        block = ctx.blocks.get(name.upper())
        if block:
            return _expand_block(block, rec, ctx, layer, array=False)
        ctx.warnings.append("DIMENSION 未找到关联块 %s，已跳过标注文字" % (name or "?"))
        return [], layer
    if t in ("VERTEX", "SEQEND", "BLOCK", "ENDBLK", "TABLE", "LAYER",
             "ENDTAB", "ENDSEC", "SECTION", "EOF", "VIEWPORT"):
        return [], layer
    ctx.warnings.append("暂不支持的 DXF 实体 %s，已跳过" % t)
    return [], layer


def _insert(rec, ctx, layer):
    """INSERT：展开块定义（含矩形阵列），仿射变换后转成图元。"""
    name = (_g1(rec, 2, "") or "").upper()
    block = ctx.blocks.get(name)
    if not block or ctx.depth > 6:
        ctx.warnings.append("INSERT 引用了不存在的块 %s，已跳过" % (name or "?"))
        return [], layer
    return _expand_block(block, rec, ctx, layer, array=True)


def _expand_block(block, rec, ctx, layer, array=True):
    base = block["base"]
    ins = (_sc(ctx, _f(rec, 10)), _sc(ctx, _f(rec, 20)))
    sx = _f(rec, 41, 1.0) or 1.0
    sy = _f(rec, 42, 1.0) or 1.0
    deg = _f(rec, 50)
    cols = max(1, _i(rec, 70, 1)) if array else 1
    rows = max(1, _i(rec, 71, 1)) if array else 1
    dx = _sc(ctx, _f(rec, 44))
    dy = _sc(ctx, _f(rec, 45))
    ctx.depth += 1
    out = []
    try:
        for row in range(rows):
            for col in range(cols):
                # 阵列间距随块自身旋转/缩放一起变换
                off = (col * dx, row * dy)
                px = ins[0] + off[0] * math.cos(math.radians(deg)) \
                    - off[1] * math.sin(math.radians(deg))
                py = ins[1] + off[0] * math.sin(math.radians(deg)) \
                    + off[1] * math.cos(math.radians(deg))
                shift = (-base[0] * ctx.scale, -base[1] * ctx.scale)
                m = _mul(_trs(px, py, sx, sy, deg),
                         (1.0, 0.0, 0.0, 1.0, shift[0], shift[1]))
                for entity, verts in block["entities"]:
                    objs, _lay = _entity(entity, ctx, verts)
                    for o in objs:
                        out.append(_xform_obj(o, m))
    finally:
        ctx.depth -= 1
    return out, layer


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def parse_dxf(text, name=""):
    """解析 ASCII DXF，返回按 CAD 图层分组的图元（mm 坐标）。"""
    warnings = []
    if not text or not text.strip():
        raise ValueError("DXF 文件为空：%s" % name)
    if text.lstrip().startswith("AutoCAD Binary DXF"):
        raise ValueError("这是二进制 DXF，请在 CAD 里另存为 ASCII DXF（%s）" % name)

    pairs = _pairs(text)
    if not pairs:
        raise ValueError("不是有效的 DXF（没有组码/值对）：%s" % name)
    head = _header(pairs)
    insunits = None
    try:
        insunits = int(float(head.get("$INSUNITS", "0")))
    except (ValueError, TypeError):
        pass
    scale = _INSUNITS_MM.get(insunits, 1.0)
    if insunits in (None, 0):
        measurement = head.get("$MEASUREMENT")
        if measurement is not None:
            scale = 1.0 if str(measurement).strip() in ("1", "1.0") else 25.4
            warnings.append("DXF 未声明 $INSUNITS，按 $MEASUREMENT 推断为 %s"
                            % ("mm" if scale == 1.0 else "inch"))
        else:
            warnings.append("DXF 未声明单位，按 mm 处理；如尺寸不对请检查 CAD 导出单位")
    units = _INSUNITS_NAME.get(insunits, "mm" if scale == 1.0 else "inch")

    sections = _sections(_records(pairs))

    # 图层表（取颜色索引，仅作展示参考）
    layer_colors = {}
    for rec in sections.get("TABLES", []):
        if rec["type"] == "LAYER":
            nm = (_g1(rec, 2, "") or "0").upper()
            layer_colors[nm] = _i(rec, 62, 7)

    # 块定义
    blocks = {}
    cur_name, cur_base, cur_ents = None, (0.0, 0.0), []
    for rec in sections.get("BLOCKS", []):
        t = rec["type"]
        if t == "BLOCK":
            cur_name = (_g1(rec, 2, "") or "").upper()
            cur_base = (_f(rec, 10), _f(rec, 20))
            cur_ents = []
        elif t == "ENDBLK":
            if cur_name:
                blocks[cur_name] = {"name": cur_name, "base": cur_base,
                                    "entities": cur_ents}
            cur_name, cur_ents = None, []
        elif cur_name:
            cur_ents.append((rec, []))

    ctx = _Ctx(scale, blocks, warnings)
    by_layer = {}
    ents = sections.get("ENTITIES", [])
    i = 0
    while i < len(ents):
        rec = ents[i]
        verts = []
        if rec["type"] == "POLYLINE":
            j = i + 1
            while j < len(ents) and ents[j]["type"] in ("VERTEX", "SEQEND"):
                if ents[j]["type"] == "VERTEX":
                    verts.append(ents[j])
                j += 1
            i = j
        else:
            i += 1
        try:
            objs, lay = _entity(rec, ctx, verts)
        except Exception as exc:                     # 单个坏实体不该毁掉整张图
            warnings.append("实体 %s 解析失败：%s" % (rec["type"], exc))
            continue
        if not objs:
            continue
        tgt = by_layer.setdefault(lay, {"name": lay, "objects": [], "bbox": None})
        for o in objs:
            tgt["objects"].append(o)
            bb = geom.obj_bbox(o)
            if bb:
                tgt["bbox"] = geom.merge_bbox(tgt["bbox"], bb)

    layers = []
    for lay in sorted(by_layer, key=str.lower):
        d = by_layer[lay]
        layers.append({
            "name": d["name"],
            "objects": d["objects"],
            "bbox": d["bbox"],
            "color_index": layer_colors.get(lay.upper(), 7),
            "warnings": [],
        })
    if not layers:
        warnings.append("没有解析出可显示的图形（检查文件是否为 ASCII DXF，或实体是否都在块里）")

    total = sum(len(l["objects"]) for l in layers)
    return {
        "name": name,
        "units": units,
        "unit_scale": scale,
        "layers": layers,
        "blocks": len(blocks),
        "warnings": warnings,
        "stats": {"objects": total, "entities": sum(ctx.types.values()),
                  "types": ctx.types},
    }


# --------------------------------------------------------------------------- #
# DWG -> DXF 的可选桥接
# --------------------------------------------------------------------------- #
def _run(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(str(exc))
    return p.returncode, (p.stdout or b"") + (p.stderr or b"")


def convert_dwg(path):
    """把 .dwg 转成 ASCII DXF，返回 (dxf_path, 转换器说明)。

    依次尝试：LibreDWG ``dwg2dxf``（含 PCB_CAM_DWG2DXF 指定路径）、
    ODA File Converter（含 PCB_CAM_ODA 指定路径）、本机 ezdxf 的 odafc 插件。
    三者都没有时抛出带操作指引的 ValueError。
    """
    path = os.path.abspath(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    tmp = tempfile.mkdtemp(prefix="pcbcam_dwg_")
    out = os.path.join(tmp, stem + ".dxf")

    dwg2dxf = os.environ.get("PCB_CAM_DWG2DXF") or shutil.which("dwg2dxf")
    if dwg2dxf and (os.path.isfile(dwg2dxf) or shutil.which(dwg2dxf)):
        code, log = _run([dwg2dxf, "-o", out, path])
        if os.path.isfile(out) and os.path.getsize(out) > 100:
            return out, "LibreDWG dwg2dxf"
        raise ValueError("dwg2dxf 转换失败（退出码 %d）：%s\n%s"
                         % (code, log.decode("utf-8", "ignore")[:400], DWG_HINT))

    oda = os.environ.get("PCB_CAM_ODA") or shutil.which("ODAFileConverter") \
        or shutil.which("ODAFileConverter.exe")
    if oda:
        code, log = _run([oda, os.path.dirname(path), tmp, "ACAD2018", "DXF",
                          "0", "1", os.path.basename(path)])
        for f in os.listdir(tmp):
            if f.lower().endswith(".dxf") and os.path.getsize(os.path.join(tmp, f)) > 100:
                return os.path.join(tmp, f), "ODA File Converter"
        raise ValueError("ODA File Converter 未产出 DXF（退出码 %d）：%s\n%s"
                         % (code, log.decode("utf-8", "ignore")[:400], DWG_HINT))

    try:
        import ezdxf                                  # 可选第三方，不装也能跑
        from ezdxf.addons import odafc
        doc = odafc.readfile(path)
        doc.saveas(out)
        if os.path.isfile(out):
            return out, "ezdxf + ODA"
    except Exception:
        pass

    raise ValueError(DWG_HINT)
