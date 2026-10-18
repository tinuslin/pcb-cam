"""单位换算与格式化：全局统一以 mm 存储，展示时按需转 mil / inch。

对外接口：
    units.norm("mil")                 -> "mil"        # 归一化单位名
    units.to_mm(v, "mil")             -> 0.0254       # 指定单位 -> mm
    units.from_mm(v, "mil")           -> 39.37        # mm -> 指定单位
    units.fmt(v_mm, "mil")            -> "63.0"       # 只给数值（不带单位）
    units.len(v_mm, "mil")            -> "63.0 mil"   # 数值 + 单位
    units.dual(v_mm)                  -> "1.600 mm（63.0 mil）"
    units.dual_short(v_mm)            -> "1.600 / 63.0"
    units.convert(v, src, dst)        -> 任意单位间换算

约定：解析器（Gerber %MOIN / Excellon M72 / ODB++ UNITS=INCH / XY 文件）
一律先折算成 mm 再进内存，因此板子内部所有几何值都是 mm。
"""
from __future__ import annotations

import re

MM_PER_INCH = 25.4
MIL_PER_MM = 1000.0 / MM_PER_INCH          # 39.3700787402
INCH_PER_MM = 1.0 / MM_PER_INCH

# 1 个单位等于多少 mm
FACTOR = {"mm": 1.0, "mil": MM_PER_INCH / 1000.0, "inch": MM_PER_INCH}

# 各单位的默认小数位：mil 数值大、inch 数值小，取不同精度便于阅读
DECIMALS = {"mm": 3, "mil": 1, "inch": 4}

_ALIASES = {
    "mm": "mm", "millimeter": "mm", "millimetre": "mm", "millimeters": "mm",
    "millimetres": "mm", "毫米": "mm", "公制": "mm", "metric": "mm",
    "mil": "mil", "mils": "mil", "thou": "mil", "毫英寸": "mil",
    "inch": "inch", "in": "inch", "inches": "inch", "英寸": "inch",
    "英制": "inch", "imperial": "inch", '"': "inch",
}


def norm(unit, default="mm"):
    """把各种写法归一化成 mm / mil / inch。"""
    if unit is None:
        return default
    s = str(unit).strip().lower()
    if not s:
        return default
    if s in FACTOR:
        return s
    return _ALIASES.get(s, default)


def is_metric(unit):
    return norm(unit) == "mm"


def to_mm(value, unit="mm"):
    """把指定单位的值换成 mm。"""
    if value is None:
        return None
    return float(value) * FACTOR[norm(unit)]


def from_mm(value, unit="mm"):
    """把 mm 换成指定单位。"""
    if value is None:
        return None
    return float(value) / FACTOR[norm(unit)]


def convert(value, src="mm", dst="mm"):
    return from_mm(to_mm(value, src), dst)


def _round_str(v, nd):
    """固定小数位（CAM 里对齐比省零更重要）；顺手消掉 -0.000 这种负零。"""
    s = ("%." + str(nd) + "f") % v
    if s.startswith("-") and float(s) == 0:
        s = s[1:]
    return s


def fmt(value_mm, unit="mm", nd=None):
    """把 mm 值按目标单位格式化，只返回数字部分。"""
    if value_mm is None:
        return "-"
    u = norm(unit)
    v = from_mm(value_mm, u)
    nd = DECIMALS[u] if nd is None else nd
    return _round_str(v, nd)


def len(value_mm, unit="mm", nd=None):
    """数值 + 单位，例如 "1.6 mm" / "63 mil" / "0.063 in"。"""
    u = norm(unit)
    txt = fmt(value_mm, u, nd)
    if txt == "-":
        return "-"
    return txt + " " + ("in" if u == "inch" else u)


def other_unit(unit="mm"):
    """给一个单位，返回另一个常用单位：mm <-> mil。"""
    return "mil" if norm(unit) == "mm" else "mm"


def dual(value_mm, primary="mm", nd=None):
    """双单位显示：主单位 + 另一个单位的括号值。"""
    if value_mm is None:
        return "-"
    p = norm(primary)
    o = other_unit(p)
    main = len(value_mm, p, nd)
    if main == "-":
        return "-"
    return "%s（%s）" % (main, len(value_mm, o, nd))


def dual_short(value_mm, primary="mm", nd=None, sep=" / "):
    """表格里用的紧凑双单位：1.600 / 63.0（单位写在表头）。"""
    if value_mm is None:
        return "-"
    p = norm(primary)
    o = other_unit(p)
    return fmt(value_mm, p, nd) + sep + fmt(value_mm, o, nd)


def pair(x_mm, y_mm, primary="mm", nd=None):
    """坐标对：(x, y) 主单位显示。"""
    return "%s, %s" % (fmt(x_mm, primary, nd), fmt(y_mm, primary, nd))


def dual_pair(x_mm, y_mm, primary="mm", nd=None):
    """坐标对双单位：X 12.700 / 500.0 · Y 8.000 / 315.0。"""
    return "X %s · Y %s" % (dual_short(x_mm, primary, nd),
                            dual_short(y_mm, primary, nd))


def unit_scale_text(unit="mm"):
    """单位说明，报告/界面用。"""
    u = norm(unit)
    return {"mm": "公制 mm", "mil": "英制 mil", "inch": "英制 inch"}[u]


_XY_UNIT_RE = re.compile(r"^(mm|mil|inch|in|thou|um|微米|毫米|英寸)$", re.I)


def unit_from_header(text):
    """从表头/文件行里猜单位，例如 "X (mil)"、"units=inch"。返回 mm/mil/inch 或空串。"""
    if not text:
        return ""
    m = re.search(r"(?:unit[s]?\s*[=:]\s*)([A-Za-z\u4e00-\u9fff]+)", text, re.I)
    if m:
        u = norm(m.group(1), default="")
        if u:
            return u
    for tok in re.findall(r"[\(\[]\s*([A-Za-z\u4e00-\u9fff]{2,6})\s*[\)\]]", text):
        u = norm(tok, default="")
        if u:
            return u
    if re.search(r"\binch(es)?\b", text, re.I):
        return "inch"
    if re.search(r"\bmils?\b", text, re.I):
        return "mil"
    if re.search(r"\bmm\b", text, re.I):
        return "mm"
    return ""
