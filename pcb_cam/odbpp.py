"""ODB++ 数据解析器。

支持目录、.tgz/.tar.gz、.zip 三种载体，解析：
  * matrix/matrix        层叠表（NAME / TYPE / CONTEXT / POLARITY / ROW）
  * steps/<step>/layers/<layer>/features   P / L / A / S / T 记录
  * $ 符号表 + 标准符号名（r/s/rect/oval/di/oct/tri/el…）+ symbols/<name>/features 自定义符号
  * DRILL 层 → 钻孔表；ROUT 层 → 铣槽
  * steps/<step>/eda/data 或 netlists/*/netlist → 网表
坐标统一换算为毫米。
"""
from __future__ import annotations

import io
import math
import os
import re
import tarfile
import zipfile

from . import geom

_INCH = 25.4


# --------------------------------------------------------------------------- #
# 虚拟文件系统
# --------------------------------------------------------------------------- #
def _norm(p: str) -> str:
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


class DirVFS:
    def __init__(self, root):
        self.root = root

    def files(self):
        out = []
        for dirpath, _dirs, names in os.walk(self.root):
            for n in names:
                full = os.path.join(dirpath, n)
                out.append(_norm(os.path.relpath(full, self.root)))
        return out

    def exists(self, path):
        return os.path.isfile(os.path.join(self.root, path))

    def read(self, path):
        full = os.path.join(self.root, path)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as f:
            data = f.read()
        return _decode(data)


class TarVFS:
    def __init__(self, path):
        self.tf = tarfile.open(path, "r:*")

    def _members(self):
        return [m for m in self.tf.getmembers() if m.isfile()]

    def files(self):
        return [_norm(m.name) for m in self._members()]

    def _get(self, path):
        target = _norm(path)
        for m in self._members():
            if _norm(m.name) == target:
                return m
        return None

    def exists(self, path):
        return self._get(path) is not None

    def read(self, path):
        m = self._get(path)
        if m is None:
            return None
        f = self.tf.extractfile(m)
        return _decode(f.read() if f else b"")


class ZipVFS:
    def __init__(self, path):
        self.zf = zipfile.ZipFile(path)

    def files(self):
        return [_norm(n) for n in self.zf.namelist() if not n.endswith("/")]

    def exists(self, path):
        try:
            self.zf.getinfo(_norm(path))
            return True
        except KeyError:
            return False

    def read(self, path):
        try:
            return _decode(self.zf.read(_norm(path)))
        except KeyError:
            return None


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "ignore")


def open_vfs(path):
    low = path.lower()
    if os.path.isdir(path):
        return DirVFS(path)
    if low.endswith((".tgz", ".tar.gz", ".tar")):
        return TarVFS(path)
    if low.endswith(".zip"):
        return ZipVFS(path)
    # 通过文件头猜测
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:2] == b"PK":
        return ZipVFS(path)
    return TarVFS(path)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
class ODBJob:
    def __init__(self, name=""):
        self.name = name
        self.step = ""
        self.layers = []
        self.holes = []
        self.slots = []
        self.tools = {}
        self.netlist = None
        self.matrix = []
        self.warnings = []
        self.bbox = None
        self.components = []


# --------------------------------------------------------------------------- #
# matrix
# --------------------------------------------------------------------------- #
def _parse_matrix(text):
    layers = []
    cur = None
    for line in text.split("\n"):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        up = s.upper()
        if up == "LAYER":
            cur = {}
            continue
        if up == "END LAYER":
            if cur is not None:
                layers.append(cur)
            cur = None
            continue
        if cur is None:
            continue
        parts = s.split(None, 1)
        key = parts[0].upper()
        val = parts[1].strip() if len(parts) > 1 else ""
        cur[key] = val
    if cur:
        layers.append(cur)
    return layers


# --------------------------------------------------------------------------- #
# 符号
# --------------------------------------------------------------------------- #
_STD_SYMBOLS = [
    (r"^r([\d.]+)$", "circle"),
    (r"^s([\d.]+)$", "square"),
    (r"^rect([\d.]+)x([\d.]+)$", "rect"),
    (r"^oval([\d.]+)x([\d.]+)$", "oval"),
    (r"^el([\d.]+)x([\d.]+)$", "ellipse"),
    (r"^di([\d.]+)x([\d.]+)$", "diamond"),
    (r"^oct([\d.]+)x([\d.]+)$", "octagon"),
    (r"^tri([\d.]+)x([\d.]+)$", "triangle"),
    (r"^hole([\d.]+)$", "hole"),
]


class SymbolResolver:
    def __init__(self, vfs, step):
        self.vfs = vfs
        self.step = step
        self.cache = {}
        self.depth = 0
        self.warnings = []

    def resolve(self, name, unit_scale):
        """返回 [(negative, shape)]，mm，形状以原点为中心。"""
        key = (name, round(unit_scale, 6))
        if key in self.cache:
            return self.cache[key]
        shapes = self._standard(name, unit_scale)
        if shapes is None:
            shapes = self._custom(name, unit_scale)
        self.cache[key] = shapes
        return shapes

    def _standard(self, name, unit_scale):
        nm = (name or "").strip().lower()
        for pat, kind in _STD_SYMBOLS:
            m = re.match(pat, nm)
            if not m:
                continue
            vals = [float(v) * 0.001 * unit_scale for v in m.groups()]   # 名字里的数值为 1/1000 单位
            if kind == "circle":
                r = vals[0] / 2.0
                return [(False, ("circle", 0.0, 0.0, r))] if r > 0 else []
            if kind == "square":
                return [(False, ("poly", geom.rect_poly(0, 0, vals[0], vals[0])))]
            if kind == "rect":
                return [(False, ("poly", geom.rect_poly(0, 0, vals[0], vals[1])))]
            if kind == "oval":
                return [(False, ("poly", geom.obround_poly(0, 0, vals[0], vals[1])))]
            if kind == "ellipse":
                w, h = vals[0], vals[1]
                pts = [(w / 2 * math.cos(geom.TWO_PI * k / 48),
                        h / 2 * math.sin(geom.TWO_PI * k / 48)) for k in range(48)]
                return [(False, ("poly", pts))]
            if kind == "diamond":
                w, h = vals[0], vals[1]
                pts = [(0, h / 2), (w / 2, 0), (0, -h / 2), (-w / 2, 0)]
                return [(False, ("poly", pts))]
            if kind == "octagon":
                d = min(vals[0], vals[1])
                return [(False, ("poly", geom.regular_poly(0, 0, d * 1.0824, 8, 22.5)))]
            if kind == "triangle":
                b, h = vals[0], vals[1]
                pts = [(-b / 2, -h / 2), (b / 2, -h / 2), (0, h / 2)]
                return [(False, ("poly", pts))]
            if kind == "hole":
                r = vals[0] / 2.0
                return [(True, ("circle", 0.0, 0.0, r))] if r > 0 else []
        return None

    def _custom(self, name, unit_scale):
        path = "steps/%s/symbols/%s/features" % (self.step, name)
        if not self.vfs.exists(path):
            return []
        text = self.vfs.read(path) or ""
        if self.depth > 3:
            return []
        self.depth += 1
        try:
            res = parse_features(text, unit_scale, self, symbol_name=name)
            out = []
            for obj in res["objects"]:
                neg = bool(obj.pop("neg", 0))
                out.append((neg, _obj_to_shape(obj)))
            return out
        finally:
            self.depth -= 1


def _obj_to_shape(obj):
    t = obj.get("t")
    if t == "circle":
        return ("circle", obj["c"][0], obj["c"][1], obj["r"])
    if t == "ring":
        return ("ring", obj["c"][0], obj["c"][1], obj["r"], obj["ri"])
    if t == "path":
        # 路径近似为矩形（描边）
        p = obj.get("p") or []
        w = obj.get("w", 0.0)
        if len(p) >= 2:
            (x1, y1), (x2, y2) = p[0], p[-1]
            L = max(geom.path_length(p), 1e-6)
            a = math.degrees(math.atan2(y2 - y1, x2 - x1))
            return ("poly", geom.rect_poly((x1 + x2) / 2, (y1 + y2) / 2, L, max(w, 1e-6), a))
        return ("poly", [])
    return ("poly", obj.get("p") or obj.get("out") or [])


# --------------------------------------------------------------------------- #
# features
# --------------------------------------------------------------------------- #
def parse_features(text, unit_scale, resolver, symbol_name=""):
    """解析 ODB++ features 文件，返回 dict。"""
    objects = []
    warnings = []
    symtab = {}
    attr_names = {}
    attr_vals = {}
    stats = {"draws": 0, "flashes": 0, "regions": 0, "objects": 0,
             "min_width": None, "area": 0.0}
    bbox = None
    src_unit = "mm"
    scale = unit_scale
    cur_surface = None      # 当前 surface 的轮廓集合
    contour = None
    cx = cy = 0.0

    def add(obj, neg=False):
        nonlocal bbox
        if not obj:
            return
        if neg:
            obj["neg"] = 1
        objects.append(obj)
        bbox = geom.merge_bbox(bbox, geom.obj_bbox(obj))
        if not neg:
            stats["area"] += geom.obj_area(obj)

    def track_width(w):
        if w and w > 0 and (stats["min_width"] is None or w < stats["min_width"]):
            stats["min_width"] = w

    for raw in text.split("\n"):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("UNITS="):
            u = s.split("=", 1)[1].strip().upper()
            if u.startswith("INCH"):
                scale = _INCH
                src_unit = "inch"
            elif u.startswith("MICRON"):
                scale = 0.001
                src_unit = "micron"
            else:
                scale = 1.0
                src_unit = "mm"
            continue
        if s[0] == "$":
            m = re.match(r"^\$(\d+)\s+(.+?)(\s+I)?$", s)
            if m:
                symtab[int(m.group(1))] = m.group(2).strip()
            continue
        if s[0] == "@":
            m = re.match(r"^@(\d+)\s+(\S+)", s)
            if m:
                attr_names[int(m.group(1))] = m.group(2)
            continue
        if s[0] == "&":
            m = re.match(r"^&(\d+)\s+(.*)$", s)
            if m:
                attr_vals[int(m.group(1))] = m.group(2)
            continue
        if s.startswith("ID=") or s.startswith("F "):
            continue

        code = s[0]
        code2 = s[:2].upper()
        # 去掉属性尾巴（;...）
        body = s
        sem = s.find(";")
        if sem >= 0:
            body = s[:sem]
        f = body.split()

        if code == "P" and len(f) >= 5:
            try:
                x = float(f[1]) * scale
                y = float(f[2]) * scale
                sym_i = int(float(f[3]))
                pol = f[4].upper()
                rot = 0.0
                if len(f) >= 8:
                    try:
                        orient_def = int(float(f[6]))
                        rot = float(f[7]) if orient_def > 0 else 0.0
                    except ValueError:
                        rot = 0.0
            except (ValueError, IndexError):
                continue
            shapes = resolver.resolve(symtab.get(sym_i, ""), scale)
            if not shapes:
                warnings.append("符号未找到：%s" % symtab.get(sym_i, "#%d" % sym_i))
                stats["objects"] += 1
                continue
            for neg, sh in shapes:
                obj = _place_shape(sh, x, y, rot)
                if obj:
                    add(obj, neg or pol == "N")
                    stats["flashes"] += 1
                    track_width(_shape_min(obj))
        elif code == "L" and len(f) >= 6:
            try:
                x1 = float(f[1]) * scale
                y1 = float(f[2]) * scale
                x2 = float(f[3]) * scale
                y2 = float(f[4]) * scale
                w_sym = int(float(f[5]))
                pol = f[6].upper() if len(f) > 6 else "P"
            except (ValueError, IndexError):
                continue
            w = 0.0
            for neg, sh in resolver.resolve(symtab.get(w_sym, ""), scale):
                if not neg:
                    w = max(w, _shape_min_shape(sh))
            if w <= 0:
                w = 0.05
            add({"t": "path", "w": round(w, 4), "p": [[round(x1, 4), round(y1, 4)],
                                                       [round(x2, 4), round(y2, 4)]]},
                pol == "N")
            stats["draws"] += 1
            track_width(w)
        elif code == "A" and len(f) >= 8:
            try:
                x1 = float(f[1]) * scale
                y1 = float(f[2]) * scale
                x2 = float(f[3]) * scale
                y2 = float(f[4]) * scale
                ax = float(f[5]) * scale
                ay = float(f[6]) * scale
                sym_i = int(float(f[7]))
                pol = f[8].upper() if len(f) > 8 else "P"
            except (ValueError, IndexError):
                continue
            r = math.hypot(x1 - ax, y1 - ay)
            if r <= 0:
                continue
            a0 = math.atan2(y1 - ay, x1 - ax)
            a1 = math.atan2(y2 - ay, x2 - ax)
            sw = geom.norm_sweep(a0, a1, True)
            pts = geom.arc_points(ax, ay, r, a0, sw)
            pts[0] = (x1, y1)
            pts[-1] = (x2, y2)
            w = 0.0
            for neg, sh in resolver.resolve(symtab.get(sym_i, ""), scale):
                if not neg:
                    w = max(w, _shape_min_shape(sh))
            add({"t": "path", "w": round(w or 0.05, 4),
                 "p": [[round(px, 4), round(py, 4)] for px, py in pts]}, pol == "N")
            stats["draws"] += 1
            track_width(w)
        elif code == "S" and len(f) >= 2:
            try:
                sym_i = int(float(f[1]))
                pol = f[2].upper() if len(f) > 2 else "P"
            except (ValueError, IndexError):
                sym_i, pol = 0, "P"
            cur_surface = {"pol": pol, "contours": [], "sym": symtab.get(sym_i, "")}
            contour = None
        elif code2 == "OB" and len(f) >= 3:
            if cur_surface is None:
                cur_surface = {"pol": "P", "contours": [], "sym": ""}
            contour = []
            cur_surface["contours"].append(contour)
            cx, cy = float(f[1]) * scale, float(f[2]) * scale
            contour.append((cx, cy))
        elif code2 == "OC" and len(f) >= 3:
            if contour is None:
                contour = []
                if cur_surface is None:
                    cur_surface = {"pol": "P", "contours": [], "sym": ""}
                cur_surface["contours"].append(contour)
            cx, cy = float(f[1]) * scale, float(f[2]) * scale
            contour.append((cx, cy))
        elif code2 == "OS" and len(f) >= 6:
            try:
                xe, ye = float(f[1]) * scale, float(f[2]) * scale
                ax, ay = float(f[3]) * scale, float(f[4]) * scale
                d = f[5].upper()
            except (ValueError, IndexError):
                continue
            r = math.hypot(cx - ax, cy - ay)
            if r > 0:
                a0 = math.atan2(cy - ay, cx - ax)
                a1 = math.atan2(ye - ay, xe - ax)
                cw = d.startswith("C") and not d.startswith("CC")
                sw = geom.norm_sweep(a0, a1, cw)
                pts = geom.arc_points(ax, ay, r, a0, sw)
                if contour is None:
                    contour = [(cx, cy)]
                    if cur_surface is None:
                        cur_surface = {"pol": "P", "contours": [], "sym": ""}
                    cur_surface["contours"].append(contour)
                contour.extend(pts[1:])
            cx, cy = xe, ye
        elif code2 == "OE":
            contour = None
        elif code2 == "SE":
            if cur_surface:
                for c in cur_surface["contours"]:
                    if len(c) >= 3:
                        add({"t": "poly", "p": [[round(px, 4), round(py, 4)] for px, py in c]},
                            cur_surface["pol"] == "N")
                        stats["regions"] += 1
            cur_surface = None
            contour = None
        elif code in ("T", "B"):
            stats["objects"] += 1
            continue
        else:
            # 未知记录
            continue
        stats["objects"] += 1

    stats["objects"] = len(objects)
    stats["source_unit"] = src_unit
    return {"objects": objects, "bbox": bbox, "warnings": warnings[:40], "stats": stats,
            "symbols": len(symtab)}


def _shape_min(obj) -> float:
    return _shape_min_shape(_obj_to_shape(obj))


def _shape_min_shape(sh) -> float:
    if sh[0] == "circle":
        return sh[3] * 2
    if sh[0] == "ring":
        return sh[3] * 2
    b = geom.bbox(sh[1])
    if not b:
        return 0.0
    return min(b[2] - b[0], b[3] - b[1])


def _place_shape(sh, x, y, rot_deg=0.0):
    kind = sh[0]
    if kind == "circle":
        _, sx, sy, r = sh
        px, py = geom.rot(sx, sy, rot_deg)
        return {"t": "circle", "c": [round(x + px, 4), round(y + py, 4)], "r": round(r, 4)}
    if kind == "ring":
        _, sx, sy, r, ri = sh
        px, py = geom.rot(sx, sy, rot_deg)
        return {"t": "ring", "c": [round(x + px, 4), round(y + py, 4)],
                "r": round(r, 4), "ri": round(ri, 4)}
    pts = sh[1]
    if not pts:
        return None
    pts = [geom.rot(px, py, rot_deg) for px, py in pts]
    return {"t": "poly", "p": [[round(x + px, 4), round(y + py, 4)] for px, py in pts]}


# --------------------------------------------------------------------------- #
# 网表
# --------------------------------------------------------------------------- #
def parse_odb_netlist(text):
    nets = {}
    points = []
    scale = 1.0
    for raw in text.split("\n"):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("UNITS="):
            u = s.split("=", 1)[1].strip().upper()
            scale = _INCH if u.startswith("INCH") else 1.0
            continue
        if s.startswith("H "):
            continue
        if s.startswith("$"):
            m = re.match(r"^\$(\d+)\s+(.+)$", s)
            if m:
                nets[int(m.group(1))] = m.group(2).strip()
            continue
        f = s.split()
        if f and f[0].strip("-").isdigit() and len(f) >= 4:
            try:
                num = int(f[0])
                x = float(f[2]) * scale
                y = float(f[3]) * scale
            except ValueError:
                continue
            side = f[4] if len(f) > 4 else ""
            via = f[-1].lower() == "v"
            points.append({"net": nets.get(num, "#%d" % num), "x": round(x, 4),
                           "y": round(y, 4), "side": side, "via": via})
    counts = {}
    for p in points:
        counts[p["net"]] = counts.get(p["net"], 0) + 1
    return {"nets": counts, "points": points, "net_count": len(nets)}


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
_KIND_MAP = {
    "SIGNAL": "copper", "POWER_GROUND": "copper", "MIXED": "copper",
    "SOLDER_MASK": "mask", "MASK": "mask", "COVERLAY": "mask", "COVERCOAT": "mask",
    "SILK_SCREEN": "silk", "SOLDER_PASTE": "paste",
    "DRILL": "drill", "ROUT": "rout", "PUNCH": "rout",
    "DOCUMENT": "doc", "COMPONENT": "component",
}


def parse_odb(vfs, name="", step=None):
    job = ODBJob(name)
    files = vfs.files()
    if not any(f.startswith("matrix/") for f in files) and not any(f.startswith("steps/") for f in files):
        raise ValueError("不是有效的 ODB++ 数据（未找到 matrix/ 或 steps/）")

    mtext = vfs.read("matrix/matrix") or ""
    matrix = _parse_matrix(mtext)
    job.matrix = matrix

    steps = sorted({f.split("/")[1] for f in files
                    if f.startswith("steps/") and f.count("/") >= 2})
    if not steps:
        raise ValueError("ODB++ 中未找到任何 step")
    job.step = step if (step and step in steps) else ("pcb" if "pcb" in steps else steps[0])
    st = job.step

    # 层目录
    layer_dirs = sorted({f.split("/")[2] for f in files
                         if f.startswith("steps/%s/layers/" % st) and f.count("/") >= 3})

    order = {}
    for i, m in enumerate(matrix):
        order[m.get("NAME", "")] = (i, m)

    names = [m.get("NAME") for m in matrix if m.get("NAME")]
    for d in layer_dirs:
        if d not in names:
            names.append(d)

    resolver = SymbolResolver(vfs, st)

    copper_layers = [n for n in names
                     if _KIND_MAP.get((order.get(n, (0, {}))[1].get("TYPE", "") or "").upper()) == "copper"
                     or (n not in order and "copper" in n.lower())]
    first_cu = copper_layers[0] if copper_layers else None
    last_cu = copper_layers[-1] if copper_layers else None

    for idx, lname in enumerate(names):
        fpath = "steps/%s/layers/%s/features" % (st, lname)
        if not vfs.exists(fpath):
            continue
        text = vfs.read(fpath) or ""
        meta = order.get(lname, (idx, {}))[1]
        ltype = (meta.get("TYPE") or "").upper()
        kind = _KIND_MAP.get(ltype, "")
        if not kind:
            low = lname.lower()
            if "copper" in low or low in ("top", "bottom") or "signal" in low:
                kind = "copper"
            elif "mask" in low or low.startswith("sm"):
                kind = "mask"
            elif "silk" in low or "legend" in low:
                kind = "silk"
            elif "paste" in low:
                kind = "paste"
            elif "drill" in low:
                kind = "drill"
            elif "rout" in low:
                kind = "rout"
            else:
                kind = "doc"

        if kind == "copper":
            if lname == first_cu:
                side = "top"
            elif lname == last_cu:
                side = "bottom"
            else:
                side = "inner"
        elif kind in ("mask", "silk", "paste"):
            low = lname.lower()
            if "top" in low or "comp" in low or low.endswith("_t"):
                side = "top"
            elif "bot" in low or "sold" in low or low.endswith("_b"):
                side = "bottom"
            else:
                side = "both"
        elif kind in ("drill", "rout"):
            side = "both"
        else:
            side = "none"

        res = parse_features(text, 1.0, resolver, symbol_name=lname)
        layer = {
            "name": lname,
            "file": "steps/%s/layers/%s/features" % (st, lname),
            "kind": kind,
            "side": side,
            "objects": res["objects"],
            "bbox": res["bbox"],
            "stats": res["stats"],
            "warnings": res["warnings"],
            "matrix": meta,
        }
        job.layers.append(layer)
        job.warnings.extend(res["warnings"])

        if kind == "drill":
            for obj in res["objects"]:
                if obj.get("t") == "circle":
                    d = round(obj["r"] * 2, 4)
                    job.holes.append({"x": obj["c"][0], "y": obj["c"][1], "d": d,
                                      "tool": 0, "plated": True, "layer": lname})
                elif obj.get("t") == "ring":
                    d = round(obj["r"] * 2, 4)
                    job.holes.append({"x": obj["c"][0], "y": obj["c"][1], "d": d,
                                      "tool": 0, "plated": True, "layer": lname})
        elif kind == "rout":
            for obj in res["objects"]:
                if obj.get("t") == "path":
                    job.slots.append({"p": obj["p"], "w": obj.get("w", 0.2), "layer": lname})

    # 板框 profile
    prof_path = "steps/%s/profile" % st
    if vfs.exists(prof_path):
        objs, bb = parse_profile(vfs.read(prof_path) or "")
        if objs:
            job.layers.append({
                "name": "profile",
                "file": prof_path,
                "kind": "outline",
                "side": "none",
                "objects": objs,
                "bbox": bb,
                "stats": {"draws": len(objs), "flashes": 0, "regions": 0,
                          "objects": len(objs), "min_width": 0.1, "area": 0.0},
                "warnings": [],
                "matrix": {"TYPE": "PROFILE"},
            })

    for h in job.holes:
        key = round(h["d"], 3)
        t = job.tools.setdefault(key, {"d": key, "count": 0, "plated": h["plated"]})
        t["count"] += 1
        h["tool"] = key

    # 网表
    cand = ["steps/%s/eda/data" % st, "steps/%s/netlists/cadnet/netlist" % st,
            "steps/%s/netlists/netlist" % st]
    for f in files:
        if f.startswith("steps/%s/netlists/" % st) and f.endswith("/netlist") and f not in cand:
            cand.append(f)
    for path in cand:
        if vfs.exists(path):
            try:
                job.netlist = parse_odb_netlist(vfs.read(path) or "")
                job.netlist["source"] = path
            except Exception as exc:
                job.warnings.append("网表解析失败 %s：%s" % (path, exc))
            break

    # 器件
    comp_path = "steps/%s/components/components" % st
    if vfs.exists(comp_path):
        try:
            job.components = _parse_components(vfs.read(comp_path) or "")
        except Exception as exc:
            job.warnings.append("器件表解析失败：%s" % exc)

    bb = None
    for layer in job.layers:
        bb = geom.merge_bbox(bb, layer.get("bbox"))
    for h in job.holes:
        bb = geom.merge_bbox(bb, (h["x"] - h["d"] / 2, h["y"] - h["d"] / 2,
                                  h["x"] + h["d"] / 2, h["y"] + h["d"] / 2))
    job.bbox = bb
    return job


def parse_profile(text, unit_scale=1.0):
    """解析 steps/<step>/profile（板框轮廓），返回闭合折线图元。"""
    scale = unit_scale
    contours = []
    cur = None
    cx = cy = 0.0
    for raw in text.split("\n"):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("UNITS="):
            u = s.split("=", 1)[1].strip().upper()
            scale = _INCH if u.startswith("INCH") else 1.0
            continue
        c2 = s[:2].upper()
        f = s.split()
        if c2 == "OB" and len(f) >= 3:
            cur = [(float(f[1]) * scale, float(f[2]) * scale)]
            contours.append(cur)
            cx, cy = cur[0]
        elif c2 == "OC" and len(f) >= 3:
            if cur is None:
                cur = []
                contours.append(cur)
            cx, cy = float(f[1]) * scale, float(f[2]) * scale
            cur.append((cx, cy))
        elif c2 == "OS" and len(f) >= 6:
            try:
                xe, ye = float(f[1]) * scale, float(f[2]) * scale
                ax, ay = float(f[3]) * scale, float(f[4]) * scale
                d = f[5].upper()
            except (ValueError, IndexError):
                continue
            r = math.hypot(cx - ax, cy - ay)
            if r > 0:
                a0 = math.atan2(cy - ay, cx - ax)
                a1 = math.atan2(ye - ay, xe - ax)
                cw = d.startswith("C") and not d.startswith("CC")
                pts = geom.arc_points(ax, ay, r, a0, geom.norm_sweep(a0, a1, cw))
                if cur is None:
                    cur = [(cx, cy)]
                    contours.append(cur)
                cur.extend(pts[1:])
            cx, cy = xe, ye
        elif c2 in ("OE", "SE"):
            cur = None
    objects = []
    bb = None
    for c in contours:
        if len(c) < 2:
            continue
        pts = [[round(px, 4), round(py, 4)] for px, py in c]
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        objects.append({"t": "path", "w": 0.1, "p": pts})
        bb = geom.merge_bbox(bb, geom.bbox(c))
    return objects, bb


_COMP_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]*$")


def _unesc(v):
    """还原 ODB++ 引号字符串里的转义：``%%`` -> ``%``（规范要求 % 双写），
    以及 ``\\,`` ``\\;`` ``\\(`` ``\\)`` ``\\'`` ``\\"`` 这类反斜杠转义。"""
    if not v:
        return v
    out = []
    i, n = 0, len(v)
    while i < n:
        ch = v[i]
        if ch == "%" and i + 1 < n and v[i + 1] == "%":
            out.append("%")
            i += 2
            continue
        if ch == "\\" and i + 1 < n:
            out.append(v[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_components(text):
    """解析 steps/<step>/components/components。

    记录格式：``CMP <pkg_ref> <x> <y> <rot> <mirror> <name> <part> [<pkg>]``
    紧随的 ``PRP <属性> '<值>'`` 作为属性；文件末尾的 ``TOP`` / ``BOT`` 段声明所在面。
    """
    comps = []
    by_name = {}
    by_idx = {}
    cur = None
    mode = None
    scale = 1.0

    def assign(tok):
        t = tok.strip().strip("'\"")
        if not t:
            return
        if re.fullmatch(r"\d+", t):
            c = by_idx.get(int(t))
        else:
            c = by_name.get(t.upper())
        if c is not None:
            c["side"] = mode

    for raw in text.split("\n"):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.upper().startswith("UNITS="):
            u = s.split("=", 1)[1].strip().upper()
            scale = _INCH if u.startswith("INCH") else 1.0
            continue
        if s.upper().startswith("ID="):
            continue
        if s.startswith(";;"):
            continue
        head = s.split(None, 1)[0].upper()
        if head in ("TOP", "BOT"):
            mode = "top" if head == "TOP" else "bottom"
            parts = s.split()[1:]
            for tok in parts:
                assign(tok)
            continue
        if head == "CMP":
            body = s.split(";;")[0]
            f = body.split()
            try:
                x = float(f[2]) * scale
                y = float(f[3]) * scale
            except (ValueError, IndexError):
                cur = None
                continue
            name = f[6] if len(f) > 6 else ""
            cur = {
                "x": x, "y": y,
                "rot": float(f[4]) if len(f) > 4 and re.match(r"^[+-]?[\d.]+$", f[4]) else None,
                "mirror": f[5] if len(f) > 5 else "",
                "ref": name,
                "part": f[7] if len(f) > 7 else "",
                "package": f[8] if len(f) > 8 else "",
                "value": "",
                "side": "bottom" if (len(f) > 5 and f[5].upper() in ("M", "Y")) else "",
                "props": {},
            }
            comps.append(cur)
            if name:
                by_name[name.upper()] = cur
            if len(f) > 1 and re.fullmatch(r"\d+", f[1]):
                by_idx[int(f[1])] = cur
            continue
        if head == "PRP":
            if cur is not None and len(s.split(None, 2)) >= 3:
                k = s.split(None, 2)[1]
                v = _unesc(s.split(None, 2)[2].strip().strip("'\""))
                cur["props"][k] = v
                if k.lower() in ("description", "comment", "value") and not cur["value"]:
                    cur["value"] = v
            continue
        if mode:
            for tok in s.split():
                if _COMP_NAME_RE.match(tok) or re.fullmatch(r"\d+", tok):
                    assign(tok)
    return comps


def load_odb(path, name="", step=None):
    vfs = open_vfs(path)
    return parse_odb(vfs, name or os.path.basename(path), step=step)
