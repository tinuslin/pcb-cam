"""共享几何工具。

约定：
* 全部输出坐标为 **毫米**，Gerber/ODB++ 坐标系（Y 轴向上）。
* 只依赖 Python 标准库。
"""
from __future__ import annotations

import math

MM_PER_INCH = 25.4
TWO_PI = 2.0 * math.pi


# --------------------------------------------------------------------------- #
# 基础变换
# --------------------------------------------------------------------------- #
def rot(px: float, py: float, deg: float):
    """绕原点旋转（角度制，逆时针为正）。"""
    if not deg:
        return px, py
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    return px * ca - py * sa, px * sa + py * ca


def rot_about(px: float, py: float, cx: float, cy: float, deg: float):
    if not deg:
        return px, py
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    dx, dy = px - cx, py - cy
    return cx + dx * ca - dy * sa, cy + dx * sa + dy * ca


# --------------------------------------------------------------------------- #
# 圆弧展平
# --------------------------------------------------------------------------- #
def arc_points(cx: float, cy: float, r: float, a0: float, sweep: float, tol: float = 0.01) -> list:
    """返回弧上的点（含起止点）。sweep 为弧度，正=逆时针，负=顺时针。tol=弦高误差(mm)。"""
    if r <= 1e-12:
        return [(cx, cy)]
    if abs(sweep) >= TWO_PI - 1e-9:
        sweep = TWO_PI if sweep > 0 else -TWO_PI
    if tol <= 0:
        tol = 0.01
    ratio = 1.0 - min(1.0, tol / r)
    step = 2.0 * math.acos(max(-1.0, min(1.0, ratio))) if r > tol else math.pi / 4.0
    step = max(step, 0.02)          # 最多 ~314 段/整圆
    n = max(1, min(2048, int(math.ceil(abs(sweep) / step))))
    pts = []
    for k in range(n + 1):
        a = a0 + sweep * k / n
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def norm_sweep(a0: float, a1: float, cw: bool, full_circle: bool = False) -> float:
    """给定起始角与终止角，返回带方向的扫掠角（弧度）。"""
    if full_circle:
        return -TWO_PI if cw else TWO_PI
    if cw:
        s = a0 - a1
        while s <= 1e-12:
            s += TWO_PI
        return -s
    s = a1 - a0
    while s <= 1e-12:
        s += TWO_PI
    return s


# --------------------------------------------------------------------------- #
# 多边形构造
# --------------------------------------------------------------------------- #
def circle_poly(cx: float, cy: float, r: float, n: int = 64) -> list:
    n = max(8, min(256, n))
    return [(cx + r * math.cos(TWO_PI * k / n), cy + r * math.sin(TWO_PI * k / n)) for k in range(n)]


def rect_poly(cx: float, cy: float, w: float, h: float, deg: float = 0.0) -> list:
    hw, hh = w / 2.0, h / 2.0
    pts = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    return [(cx + x, cy + y) for x, y in (rot(px, py, deg) for px, py in pts)]


def obround_poly(cx: float, cy: float, w: float, h: float, deg: float = 0.0) -> list:
    """跑道形（两端半圆）。"""
    if abs(w - h) < 1e-9:
        return circle_poly(cx, cy, w / 2.0, 48)
    if w > h:
        r = h / 2.0
        d = w / 2.0 - r
        pts = []
        for k in range(25):                      # 右半圆 -90° -> 90°
            a = -math.pi / 2 + math.pi * k / 24
            pts.append((d + r * math.cos(a), r * math.sin(a)))
        for k in range(25):                      # 左半圆 90° -> 270°
            a = math.pi / 2 + math.pi * k / 24
            pts.append((-d + r * math.cos(a), r * math.sin(a)))
    else:
        r = w / 2.0
        d = h / 2.0 - r
        pts = []
        for k in range(25):                      # 上半圆 0° -> 180°
            a = math.pi * k / 24
            pts.append((r * math.cos(a), d + r * math.sin(a)))
        for k in range(25):                      # 下半圆 180° -> 360°
            a = math.pi + math.pi * k / 24
            pts.append((r * math.cos(a), -d + r * math.sin(a)))
    return [(cx + x, cy + y) for x, y in (rot(px, py, deg) for px, py in pts)]


def regular_poly(cx: float, cy: float, dia: float, n: int, deg: float = 0.0) -> list:
    n = max(3, min(64, int(n)))
    r = dia / 2.0
    pts = []
    for k in range(n):
        a = math.radians(deg) + TWO_PI * k / n
        pts.append((r * math.cos(a), r * math.sin(a)))
    return [(cx + x, cy + y) for x, y in pts]


def thermal_shapes(cx: float, cy: float, outer_dia: float, inner_dia: float,
                   gap: float, deg: float = 0.0) -> list:
    """Gerber 原语 7（散热焊盘）：外环被 4 个宽度为 gap 的缺口打断。

    原语 7 是**一个整体形状**（带缺口的环），不是"环 + 负性擦除"，因此这里
    直接给出 4 段环形扇形的多边形，不产生任何 negative 图元。
    返回 [(False, shape)]，shape 为元组形式（与 _shape_to_obj 对应）。
    """
    out_r = outer_dia / 2.0
    in_r = max(0.0, inner_dia / 2.0)
    if out_r <= 1e-9:
        return []
    if in_r > out_r:
        in_r = 0.0
    g = max(0.0, gap)

    def half(radius):
        """半径为 radius 处，缺口对应的角度半宽（弧度）。"""
        if radius <= 1e-9 or g <= 1e-9:
            return 0.0
        return math.asin(min(0.98, (g / 2.0) / radius))

    h_out = half(out_r)
    h_in = half(in_r) if in_r > 1e-9 else 0.0
    base = math.radians(deg)
    out = []
    for k in range(4):
        a0 = base + math.pi / 2.0 * k + h_out
        a1 = base + math.pi / 2.0 * (k + 1) - h_out
        if a1 <= a0:
            continue
        span = a1 - a0
        outer = arc_points(cx, cy, out_r, a0, span)
        if in_r <= 1e-9:
            pts = [(cx, cy)] + outer
        else:
            b1 = a1 - h_out + h_in
            b0 = a0 + h_out - h_in
            if b1 <= b0:
                b1, b0 = a1, a0
            inner = arc_points(cx, cy, in_r, b1, -(b1 - b0))
            pts = outer + inner
        out.append((False, ("poly", pts)))
    return out


# --------------------------------------------------------------------------- #
# 度量
# --------------------------------------------------------------------------- #
def bbox(pts, cur=None):
    """返回 (x0, y0, x1, y1)；cur 为已有包围盒可合并。"""
    if not pts:
        return cur
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    b = (min(xs), min(ys), max(xs), max(ys))
    if cur is None:
        return b
    return (min(cur[0], b[0]), min(cur[1], b[1]), max(cur[2], b[2]), max(cur[3], b[3]))


def obj_bbox(obj):
    t = obj.get("t")
    if t == "path" or t == "poly":
        return bbox(obj.get("p") or [])
    if t in ("circle", "ring"):
        cx, cy = obj["c"]
        r = obj.get("r", 0.0)
        return (cx - r, cy - r, cx + r, cy + r)
    if t == "poly2":
        return bbox(obj.get("out") or [])
    if t == "group":
        b = obj.get("b")
        return tuple(b) if b else None
    return None


def merge_bbox(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def poly_area(pts) -> float:
    """鞋带公式，返回绝对值面积。"""
    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    x0, y0 = pts[0]
    for i in range(1, n - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        s += (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
    return abs(s) * 0.5


def path_length(pts) -> float:
    total = 0.0
    for i in range(1, len(pts)):
        total += math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
    return total


def obj_area(obj) -> float:
    t = obj.get("t")
    if t == "poly":
        return poly_area(obj.get("p") or [])
    if t == "circle":
        return math.pi * obj["r"] ** 2
    if t == "ring":
        return math.pi * (obj["r"] ** 2 - obj.get("ri", 0.0) ** 2)
    if t == "path":
        p = obj.get("p") or []
        w = obj.get("w", 0.0)
        return path_length(p) * w + math.pi * (w / 2.0) ** 2 * 2
    if t == "poly2":
        a = poly_area(obj.get("out") or [])
        for hole in obj.get("in") or []:
            a -= poly_area(hole)
        return max(0.0, a)
    if t == "group":
        return sum(obj_area(it) for it in obj.get("items") or [] if not it.get("neg"))
    return 0.0


def point_in_poly(x: float, y: float, poly) -> bool:
    inside = False
    n = len(poly)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)):
            xint = (xj - xi) * (y - yi) / (yj - yi + 1e-30) + xi
            if x < xint:
                inside = not inside
        j = i
    return inside


def dist_point_seg(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 1e-18:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def obj_contains(obj, x: float, y: float, tol: float = 0.0) -> bool:
    """点是否落在图元实体内部（用于环宽/连通性估算）。"""
    t = obj.get("t")
    if t == "circle":
        return math.hypot(x - obj["c"][0], y - obj["c"][1]) <= obj["r"] + tol
    if t == "ring":
        d = math.hypot(x - obj["c"][0], y - obj["c"][1])
        return obj.get("ri", 0.0) - tol <= d <= obj["r"] + tol
    if t == "poly":
        return point_in_poly(x, y, obj.get("p") or [])
    if t == "poly2":
        if not point_in_poly(x, y, obj.get("out") or []):
            return False
        for hole in obj.get("in") or []:
            if point_in_poly(x, y, hole):
                return False
        return True
    if t == "group":
        for it in obj.get("items") or []:
            if it.get("neg"):
                continue
            if obj_contains(it, x, y, tol):
                for dk in obj.get("items") or []:
                    if dk.get("neg") and obj_contains(dk, x, y, tol):
                        return False
                return True
        return False
    if t == "path":
        p = obj.get("p") or []
        w = obj.get("w", 0.0) / 2.0 + tol
        for i in range(1, len(p)):
            if dist_point_seg(x, y, p[i - 1][0], p[i - 1][1], p[i][0], p[i][1]) <= w:
                return True
        return False
    return False


def obj_dist(obj, x: float, y: float) -> float:
    """点到图元实体的最短距离（内部为 0）。"""
    t = obj.get("t")
    if t in ("circle", "ring"):
        d = math.hypot(x - obj["c"][0], y - obj["c"][1])
        return max(0.0, d - obj["r"])
    if t == "poly2":
        pts = obj.get("out") or []
    elif t == "poly":
        pts = obj.get("p") or []
    elif t == "path":
        pts = obj.get("p") or []
        w = obj.get("w", 0.0) / 2.0
        if len(pts) < 2:
            return 0.0 if pts else float("inf")
        return max(0.0, min(dist_point_seg(x, y, pts[i - 1][0], pts[i - 1][1],
                                           pts[i][0], pts[i][1])
                            for i in range(1, len(pts))) - w)
    else:
        b = obj_bbox(obj)
        if not b:
            return float("inf")
        dx = max(b[0] - x, 0.0, x - b[2])
        dy = max(b[1] - y, 0.0, y - b[3])
        return math.hypot(dx, dy)
    if len(pts) < 3:
        return float("inf")
    if point_in_poly(x, y, pts):
        return 0.0
    n = len(pts)
    return min(dist_point_seg(x, y, pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
               for i in range(n))


def pad_covers(obj, x: float, y: float, tol: float = 0.0) -> bool:
    """焊盘是否盖住该点：环形焊盘按外圆算（孔内的点也算被覆盖），poly2 按外轮廓算。"""
    t = obj.get("t")
    if t == "ring":
        return math.hypot(x - obj["c"][0], y - obj["c"][1]) <= obj["r"] + tol
    if t == "poly2":
        out = obj.get("out") or []
        if point_in_poly(x, y, out):
            return True
        if tol > 0:
            for i in range(len(out)):
                a, b = out[i - 1], out[i]
                if dist_point_seg(x, y, a[0], a[1], b[0], b[1]) <= tol:
                    return True
        return False
    return obj_contains(obj, x, y, tol)


def fmt_mm(v: float) -> float:
    return round(v, 4)
