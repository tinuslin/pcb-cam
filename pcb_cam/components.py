"""元件外形尺寸（长 / 宽）与贴装类型（SMD / TH）的推断（纯标准库）。

来源优先级（都写进 ``size_source``，方便下游判断可信度）：

    manual   用户在界面里手工填过（最高，推断不会覆盖）
    attr     ODB++ ``PRP`` 属性 / XY 文件里的 Length、Width、Mount 列
    geometry 板上铜箔焊盘外接框 + 钻孔位置（有焊盘就能估，属估算值）
    package  按常见封装代号查表（0603 / SOIC-8 / DIP-8 …，属经验值）
    unknown  什么线索都没有

约定：``length`` 取外接框的较长边，``width`` 取较短边，单位一律 mm；
``mount`` 只取 ``SMD`` / ``TH`` / 空串（未知），不猜第三种。
几何推断得到的是**焊盘外接框**而非塑封体，因此通常略大于实物本体，
下厂前请以封装规格书或贴片厂 BOM 为准。
"""
from __future__ import annotations

import math
import re

from . import geom

# 常见片式元件 / 封装的标称本体尺寸（mm，长 x 宽）
_PACKAGE_SIZE = {
    "0201": (0.6, 0.3), "0402": (1.0, 0.5), "0603": (1.6, 0.8),
    "0805": (2.0, 1.25), "1206": (3.2, 1.6), "1210": (3.2, 2.5),
    "1812": (4.5, 3.2), "2010": (5.0, 2.5), "2512": (6.3, 3.2),
    "sot-23": (2.9, 1.3), "sot-223": (6.5, 3.5), "sot-89": (4.5, 2.5),
    "soic-8": (4.9, 3.9), "soic-14": (8.7, 3.9), "soic-16": (9.9, 3.9),
    "tssop-8": (3.0, 3.0), "msop-8": (3.0, 3.0),
    "sma": (4.3, 2.6), "smb": (4.6, 3.6), "smc": (6.2, 5.6),
}

_SMD_HINTS = ("smd", "smt", "chip", "0201", "0402", "0603", "0805", "1206",
              "1210", "1812", "2010", "2512", "sot", "soic", "sop", "tssop",
              "msop", "qfp", "qfn", "dfn", "bga", "lga", "lcc", "plcc", "sma", "smb", "smc")
_TH_HINTS = ("dip", "dil", "sip", "to-220", "to220", "to-92", "to92", "to-3",
             "to-247", "hdr", "header", "pga", "tht", "through", "插件", "直插", "针座")

_SIZE_KEYS = ("length", "bodylength", "bodyl", "sizex", "dimx", "长度", "长", "本体长", "元件长")
_WIDTH_KEYS = ("width", "bodywidth", "bodyw", "sizey", "dimy", "宽度", "宽", "本体宽", "元件宽")
_SIZE2_KEYS = ("size", "bodysize", "body_size", "dimensions", "外形尺寸", "尺寸", "封装尺寸", "packagesize")
_MOUNT_KEYS = ("mount", "mounttype", "mount_type", "mounting", "technology",
               "componenttype", "component_type", "packagetype", "smt", "smd",
               "贴装", "装配类型", "贴装类型", "元件类型")

_NUM = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_PAIR = re.compile(
    r"([+-]?\d+(?:\.\d+)?)\s*(?:mm|mil|in|inch)?\s*[x×*,/、 ]\s*"
    r"([+-]?\d+(?:\.\d+)?)", re.I)


def _norm_key(s):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", str(s or "").lower())


def parse_size(text):
    """解析尺寸字符串 -> ``(length, width)``（mm）。

    支持 ``"1.6x0.8"`` / ``"1.6 X 0.8 mm"`` / ``"1.6,0.8"`` / ``"0603"`` /
    ``"60x30mil"``；只给一个数时返回 ``(v, None)``。
    """
    s = str(text or "").strip()
    if not s:
        return None, None
    low = s.lower()
    factor = 0.0254 if re.search(r"\bmil\b|毫英寸", low) else (
        25.4 if re.search(r"\bin(ch)?\b|英寸", low) else 1.0)
    m = _PAIR.search(s)
    if m:
        a, b = float(m.group(1)) * factor, float(m.group(2)) * factor
        if a > 0 and b > 0:
            return round(max(a, b), 4), round(min(a, b), 4)
    code = _norm_key(s)
    if code in _PACKAGE_SIZE:
        return _PACKAGE_SIZE[code]
    n = _NUM.search(s)
    if n:
        v = float(n.group(0)) * factor
        if v > 0:
            return round(v, 4), None
    return None, None


def normalize_mount(text):
    """把各种写法归一到 ``SMD`` / ``TH`` / 空串。"""
    v = _norm_key(text)
    if not v:
        return ""
    if any(k in v for k in ("tht", "throughhole", "through", "插件", "直插", "通孔")) \
            or v in ("th", "pth", "dip", "dil", "pin", "insert"):
        return "TH"
    if any(k in v for k in ("smd", "smt", "surface", "surface mount".replace(" ", ""),
                            "贴片", "表贴")):
        return "SMD"
    if v in ("th", "pth"):
        return "TH"
    return ""


def package_size(package):
    """按封装代号查标称本体尺寸，返回 (length, width) 或 (None, None)。"""
    p = str(package or "").strip().lower()
    if not p:
        return None, None
    key = re.sub(r"[^0-9a-z-]", "", p)
    if key in _PACKAGE_SIZE:
        return _PACKAGE_SIZE[key]
    # 例如 "R0603" / "0603R" / "C_0805" 里带代号
    for code, size in _PACKAGE_SIZE.items():
        if re.search(r"(?<![0-9])%s(?![0-9])" % re.escape(code), key):
            return size
    return None, None


def package_mount(package):
    p = str(package or "").lower()
    if not p:
        return ""
    if any(k in p for k in _TH_HINTS):
        return "TH"
    if any(k in p for k in _SMD_HINTS):
        return "SMD"
    return ""


def attrs_of(component):
    """把 ODB++ PRP 属性与顶层字段合成一个可查表。"""
    out = {}
    for k, v in (component.get("props") or {}).items():
        out[_norm_key(k)] = v
    for k in ("length", "width", "size", "mount", "mount_type",
              "body_size", "technology", "package_size"):
        v = component.get(k)
        if v not in (None, ""):
            out[_norm_key(k)] = v
    return out


def _lookup(attrs, keys):
    for k in keys:
        nk = _norm_key(k)
        if nk in attrs:
            return attrs[nk]
    for nk, v in attrs.items():
        if any(nk == _norm_key(k) or nk.startswith(_norm_key(k)) for k in keys):
            return v
    return None


def _pad_list(board):
    pads = []
    for layer in board.layers:
        if layer.get("kind") != "copper":
            continue
        for o in layer.get("objects") or []:
            if o.get("neg") or o.get("t") in ("path", "text", None):
                continue
            bb = geom.obj_bbox(o)
            if not bb:
                continue
            w, h = bb[2] - bb[0], bb[3] - bb[1]
            if min(w, h) <= 0 or max(w, h) > 12.0:
                continue
            pads.append((bb, o))
    return pads


def enrich_components(board):
    """给 ``board.components`` 补齐 length / width / mount / size_source。

    返回补齐条数。不会覆盖 ``size_source == "manual"`` 的条目。
    """
    comps = board.components or []
    if not comps:
        return 0
    pads = _pad_list(board)
    holes = (board.drills.holes if board.drills else []) or []
    centers = []
    for c in comps:
        try:
            centers.append((float(c.get("x")), float(c.get("y"))))
        except (TypeError, ValueError):
            centers.append((float("nan"), float("nan")))
    done = 0

    for i, c in enumerate(comps):
        if c.get("size_source") == "manual":
            continue
        attrs = attrs_of(c)
        raw_len = _lookup(attrs, _SIZE_KEYS)
        raw_wid = _lookup(attrs, _WIDTH_KEYS)
        raw_size = _lookup(attrs, _SIZE2_KEYS)
        raw_mount = _lookup(attrs, _MOUNT_KEYS)

        length = _to_pos_num(raw_len)
        width = _to_pos_num(raw_wid)
        if raw_size not in (None, ""):
            sl, sw = parse_size(raw_size)
            length = length if length is not None else sl
            width = width if width is not None else sw
            if length is not None and width is None and sw is not None:
                width = sw
        mount = normalize_mount(raw_mount)
        source = "attr" if (length is not None or width is not None or mount) else ""
        pkg = c.get("package") or c.get("part") or ""

        # 几何：找本元件附近的焊盘，取外接框（半径按相邻元件间距收敛）
        try:
            x, y = float(c.get("x")), float(c.get("y"))
        except (TypeError, ValueError):
            x = y = None
        near = None
        if x is not None and pads:
            dmin = min((math.hypot(x - px, y - py) for j, (px, py) in enumerate(centers)
                        if j != i and math.isfinite(px) and math.isfinite(py)),
                       default=float("inf"))
            radius = min(7.0, max(1.2, dmin * 0.48))
            bb = None
            found = 0
            for pbb, _o in pads:
                cx, cy = (pbb[0] + pbb[2]) / 2.0, (pbb[1] + pbb[3]) / 2.0
                if math.hypot(cx - x, cy - y) <= radius:
                    bb = geom.merge_bbox(bb, pbb)
                    found += 1
            if bb and found:
                near = bb
        if near:
            gl = round(max(near[2] - near[0], near[3] - near[1]), 4)
            gw = round(min(near[2] - near[0], near[3] - near[1]), 4)
            if length is None:
                length = gl
            if width is None:
                width = gw
            if not source:
                source = "geometry"
            if not mount and holes:
                has_hole = any(
                    near[0] - 0.45 <= h["x"] <= near[2] + 0.45 and
                    near[1] - 0.45 <= h["y"] <= near[3] + 0.45 for h in holes)
                mount = "TH" if has_hole else "SMD"

        # 封装代号兜底
        pl, pw = package_size(pkg)
        if length is None and pl is not None:
            length, width = pl, (width if width is not None else pw)
            source = source or "package"
        elif width is None and pw is not None and length is not None:
            width = pw
        if not mount:
            mount = package_mount(pkg)
            if mount and not source:
                source = "package"

        c["length"] = round(length, 4) if length is not None else None
        c["width"] = round(width, 4) if width is not None else None
        c["mount"] = mount or ""
        c["size_source"] = source or "unknown"
        done += 1
    return done


def _to_pos_num(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v) if float(v) > 0 else None
    m = _NUM.search(str(v))
    if not m:
        return None
    f = float(m.group(0))
    return f if f > 0 else None


def merge_board_attrs(entries, board):
    """把板上器件的长/宽/类型补进 XY 条目（按位号匹配，不覆盖条目自带值）。"""
    if not board or not entries:
        return 0
    by_ref = {(c.get("ref") or "").strip(): c for c in (board.components or [])}
    n = 0
    for e in entries:
        c = by_ref.get((e.get("ref") or "").strip())
        if not c:
            continue
        for k in ("length", "width", "mount", "package", "value"):
            if not e.get(k) and c.get(k):
                e[k] = c[k]
                n += 1
        # 条目自带尺寸/类型时来源以条目为准，不把板上的 geometry 标签贴过来
        if not e.get("size_source") and not any(
                e.get(k) for k in ("length", "width", "mount")) and c.get("size_source"):
            e["size_source"] = c["size_source"]
            n += 1
    return n
