"""工程装载与分析：识别 Gerber / Excellon / ODB++ / IPC-D-356，汇总成一块板。

对外主要接口：
    board = load_board(path)          # 目录 / 压缩包 / 单文件
    summary = board_summary(board)    # 可 JSON 化的摘要（不含图元）
    objs = layer_objects(board, i)    # 第 i 层的图元
    md = build_report(board)          # Markdown 报告（report.py）
"""
from __future__ import annotations

import math
import os
import re
import tempfile
import zipfile

from . import excellon as ex
from . import geom
from . import gerber as gb
from . import ipc356 as ipc
from . import odbpp
from . import xy

# --------------------------------------------------------------------------- #
# 层分类
# --------------------------------------------------------------------------- #
_EXT_KIND = {
    "gtl": ("copper", "top"), "gbl": ("copper", "bottom"),
    "gts": ("mask", "top"), "gbs": ("mask", "bottom"),
    "gto": ("silk", "top"), "gbo": ("silk", "bottom"),
    "gtp": ("paste", "top"), "gbp": ("paste", "bottom"),
    "gko": ("outline", "none"), "gm1": ("outline", "none"), "gml": ("outline", "none"),
    "oln": ("outline", "none"), "gm": ("outline", "none"), "out": ("outline", "none"),
    "drl": ("drill", "both"), "drd": ("drill", "both"), "xln": ("drill", "both"),
    "exc": ("drill", "both"), "nc": ("drill", "both"), "ncd": ("drill", "both"),
    "tap": ("drill", "both"), "txt": ("drill", "both"), "drill": ("drill", "both"),
    "ipc": ("netlist", "none"), "356": ("netlist", "none"), "net": ("netlist", "none"),
}

_PALETTE = {
    ("copper", "top"): "#e8503a",
    ("copper", "bottom"): "#3b7dd8",
    ("copper", "inner"): "#d9a036",
    ("mask", "top"): "#5fae4e",
    ("mask", "bottom"): "#4f9a6a",
    ("mask", "both"): "#5fae4e",
    ("silk", "top"): "#dfe6ef",
    ("silk", "bottom"): "#aebbcb",
    ("silk", "both"): "#c8d2dd",
    ("paste", "top"): "#9aa3ad",
    ("paste", "bottom"): "#8a939d",
    ("paste", "both"): "#9aa3ad",
    ("outline", "none"): "#ffd166",
    ("outline", "both"): "#ffd166",
    ("drill", "both"): "#f2c94c",
    ("rout", "both"): "#ff9f43",
    ("rout", "none"): "#ff9f43",
    ("doc", "none"): "#8b9dc3",
    ("component", "none"): "#8b9dc3",
    ("other", "none"): "#8899aa",
}

_INNER_COLORS = ["#d9a036", "#9b6bd6", "#39a78e", "#c05fa0", "#7aa6d9"]

# 各类型默认不透明度：阻焊/钢网半透明，便于同时看到铜箔
_KIND_OPACITY = {
    "copper": 1.0, "mask": 0.45, "paste": 0.55, "silk": 0.95,
    "outline": 1.0, "rout": 1.0, "doc": 0.8, "component": 0.8, "other": 0.9,
}


def layer_color(kind, side, index=0):
    key = (kind, side)
    if key in _PALETTE:
        return _PALETTE[key]
    if kind == "copper" and side == "inner":
        return _INNER_COLORS[index % len(_INNER_COLORS)]
    return _PALETTE.get((kind, "none"), "#8899aa")


def classify_name(name: str):
    """按文件名/扩展名推断层类型与板面。"""
    base = os.path.basename(name).lower()
    stem, _, ext = base.rpartition(".")
    if not stem:
        stem, ext = base, ""
    if ext in _EXT_KIND:
        return _EXT_KIND[ext]
    m = re.fullmatch(r"g(\d{1,2})", ext)
    if m:
        return ("copper", "inner")
    m = re.fullmatch(r"gp(\d{1,2})", ext)
    if m:
        return ("copper", "inner")

    low = base
    if re.search(r"(outline|edge|board[_-]?out|profile|keepout|gko)", low):
        return ("outline", "none")
    if re.search(r"(mask|solder|sm[_-])", low):
        side = "top" if re.search(r"(top|front|f[._])", low) else (
            "bottom" if re.search(r"(bot|back|b[._])", low) else "both")
        return ("mask", side)
    if re.search(r"(silk|legend|sst|sst|assy)", low):
        side = "top" if re.search(r"(top|front|f[._])", low) else (
            "bottom" if re.search(r"(bot|back|b[._])", low) else "both")
        return ("silk", side)
    if re.search(r"(paste|stencil|pst)", low):
        side = "top" if re.search(r"(top)", low) else ("bottom" if re.search(r"(bot)", low) else "both")
        return ("paste", side)
    if re.search(r"(drill|npth|pth)", low):
        return ("drill", "both")
    if re.search(r"(copper|inner|sig|power|gnd|plane|in\d)", low):
        side = "top" if re.search(r"(top|l1|f[._]cu)", low) else (
            "bottom" if re.search(r"(bot|ln|b[._]cu)", low) else "inner")
        return ("copper", side)
    return ("other", "none")


# --------------------------------------------------------------------------- #
# 板对象
# --------------------------------------------------------------------------- #
class Board:
    def __init__(self):
        self.path = ""
        self.name = ""
        self.format = "gerber"
        self.step = ""
        self.layers = []          # 几何层（drill 层不进入此列表）
        self.drills = None        # DrillProgram / ODB 钻孔集合
        self.netlists = []        # [Netlist]
        self.components = []
        self.warnings = []
        self.bbox = None
        self.analysis = {}
        self.tmpdir = None


def _new_layer(name, path, kind, side, objects, bbox, stats, warnings, attrs=None):
    return {
        "name": name,
        "file": path,
        "kind": kind,
        "side": side,
        "objects": objects,
        "bbox": bbox,
        "stats": stats,
        "warnings": list(warnings or []),
        "attrs": attrs or {},
        "color": layer_color(kind, side, len(name)),
        "visible": True,
        "opacity": _KIND_OPACITY.get(kind, 1.0),
    }


# --------------------------------------------------------------------------- #
# 单文件 / 目录 / 压缩包
# --------------------------------------------------------------------------- #
def sniff_text(text: str) -> str:
    head = text[:4000]
    stripped = head.lstrip()
    if "%FS" in head or stripped.startswith("%") or (
            stripped.startswith("G04") and "%" in head):
        return "gerber"
    if re.search(r"^\s*M48", head, re.M) or re.search(r"^\s*T\d+C[\d.]", head, re.M):
        return "excellon"
    if re.search(r"^\s*P (JOB|UNITS)", head, re.M) or re.search(r"^\s*(317|327|127)\S", head, re.M):
        return "ipc356"
    if "UNITS=" in head and ("$" in head or re.search(r"^[PLAS] ", head, re.M)):
        return "odb_features"
    return "unknown"


def _read_text(path):
    with open(path, "rb") as f:
        data = f.read()
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "ignore")


def _is_odb_dir(path):
    return (os.path.isfile(os.path.join(path, "matrix", "matrix"))
            or os.path.isdir(os.path.join(path, "steps")))


def load_board(path, step=None):
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise FileNotFoundError("路径不存在：%s" % path)
    if os.path.isdir(path):
        if _is_odb_dir(path):
            return _board_from_odb(path, step)
        return _scan_dir(path)
    low = path.lower()
    if low.endswith(".zip"):
        return _from_zip(path, step)
    if low.endswith((".tgz", ".tar.gz", ".tar")):
        try:
            return _board_from_odb(path, step)
        except Exception:
            return _from_archive_extract(path)
    return _from_single(path)


def _make_tmp(path):
    tag = re.sub(r"[^0-9A-Za-z]+", "_", os.path.basename(path))[:24]
    d = os.path.join(tempfile.gettempdir(), "pcbcam_" + tag)
    os.makedirs(d, exist_ok=True)
    return d


def _from_zip(path, step=None):
    with zipfile.ZipFile(path) as zf:
        names = [n.replace("\\", "/") for n in zf.namelist()]
        is_odb = any(n.startswith("matrix/") for n in names) or any(n.startswith("steps/") for n in names)
        if is_odb:
            dest = _make_tmp(path)
            zf.extractall(dest)
            root = _find_odb_root(dest) or dest
            return _board_from_odb(root, step)
    dest = _make_tmp(path)
    with zipfile.ZipFile(path) as zf:
        zf.extractall(dest)
    return _scan_dir(dest, source=path)


def _from_archive_extract(path):
    import tarfile
    dest = _make_tmp(path)
    with tarfile.open(path, "r:*") as tf:
        tf.extractall(dest)
    root = _find_odb_root(dest)
    if root:
        return _board_from_odb(root)
    return _scan_dir(dest, source=path)


def _find_odb_root(root):
    """在解包目录中寻找 ODB++ 根（允许外面多套一层目录）。"""
    if _is_odb_dir(root):
        return root
    try:
        entries = os.listdir(root)
    except OSError:
        return None
    for entry in entries:
        cand = os.path.join(root, entry)
        if os.path.isdir(cand) and _is_odb_dir(cand):
            return cand
    return None


def _board_from_odb(path, step=None):
    vfs = odbpp.open_vfs(path)
    job = odbpp.parse_odb(vfs, name=os.path.basename(path.rstrip("/\\")), step=step)
    b = Board()
    b.path = path
    b.name = job.name
    b.format = "odbpp"
    b.step = job.step
    b.warnings = list(job.warnings)
    b.components = job.components

    for layer in job.layers:
        kind = layer["kind"]
        if kind == "drill":
            continue
        name = layer["name"]
        b.layers.append(_new_layer(
            name=name, path=layer["file"], kind=kind, side=layer["side"],
            objects=layer["objects"], bbox=layer["bbox"], stats=layer["stats"],
            warnings=layer["warnings"], attrs={"matrix": layer.get("matrix") or {}},
        ))

    drill = ex.DrillProgram(os.path.basename(path))
    # 按孔径重新编号刀具（ODB++ 内部以尺寸标识）
    by_d = {}
    for h in job.holes:
        by_d.setdefault(round(h["d"], 3), []).append(h)
    tools = {}
    holes = []
    for i, d in enumerate(sorted(by_d), start=1):
        tools[i] = {"d": d, "count": len(by_d[d]), "plated": True}
        for h in by_d[d]:
            h["tool"] = i
            holes.append(h)
    drill.holes = sorted(holes, key=lambda h: (h["d"], h["x"], h["y"]))
    drill.slots = job.slots
    drill.tools = tools
    drill.stats = {"holes": len(drill.holes), "slots": len(drill.slots), "tools": len(tools)}
    drill.units = "mm"
    bb = None
    for h in job.holes:
        bb = geom.merge_bbox(bb, (h["x"] - h["d"] / 2, h["y"] - h["d"] / 2,
                                  h["x"] + h["d"] / 2, h["y"] + h["d"] / 2))
    for s in job.slots:
        for p in s.get("p") or []:
            bb = geom.merge_bbox(bb, (p[0], p[1], p[0], p[1]))
    drill.bbox = bb
    b.drills = drill

    if job.netlist:
        nl = ipc.Netlist("ODB++ 网表", job.netlist.get("source", ""))
        nl.units = "mm"
        for p in job.netlist.get("points", []):
            nl.add(p.get("net", ""), "VIA" if p.get("via") else "", "", p["x"], p["y"],
                   side=p.get("side", ""), kind="via" if p.get("via") else "pth")
        b.netlists.append(nl)

    _finalize(b)
    return b


def _scan_dir(root, source=None):
    root_odb = _find_odb_root(root)
    if root_odb:
        b = _board_from_odb(root_odb)
        if source:
            b.path = source
            b.name = os.path.basename(source.rstrip("/\\"))
        return b
    b = Board()
    b.path = source or root
    b.name = os.path.basename((source or root).rstrip("/\\"))
    files = []
    for dirpath, _dirs, names in os.walk(root):
        for n in sorted(names):
            files.append(os.path.join(dirpath, n))
    files.sort()
    _load_files(b, files)
    _finalize(b)
    return b


def _load_files(b, files):
    used = set()
    drill_progs = []
    for f in files:
        low = f.lower()
        if low.endswith((".md", ".pdf", ".jpg", ".png", ".xls", ".xlsx", ".doc", ".docx",
                         ".zip", ".tgz", ".tar", ".gz", ".rar", ".7z", ".csv", ".json")):
            continue
        try:
            text = _read_text(f)
        except Exception as exc:
            b.warnings.append("读取失败 %s：%s" % (os.path.basename(f), exc))
            continue
        kind = sniff_text(text)
        base = os.path.basename(f)
        if kind == "gerber":
            img = gb.parse_gerber(text, base)
            lk, side = classify_name(base)
            if img.stats.get("min_width") is None and not img.objects:
                b.warnings.append("%s 未解析出图形（已跳过）" % base)
                continue
            name = _uniq(img.name or os.path.splitext(base)[0], used)
            b.layers.append(_new_layer(name, f, lk, side, img.objects, img.bbox,
                                       img.stats, img.warnings, img.attrs))
            b.warnings.extend("%s：%s" % (base, w) for w in img.warnings)
        elif kind == "excellon":
            prog = ex.parse_excellon(text, base)
            drill_progs.append(prog)
        elif kind == "ipc356":
            nl = ipc.parse_ipc356(text, base, source=f)
            b.netlists.append(nl)
            b.warnings.extend("%s：%s" % (base, w) for w in nl.warnings)
        elif kind == "odb_features":
            b.warnings.append("%s 是 ODB++ 的 features 文件，请直接加载整个 ODB++ 目录/压缩包" % base)
        else:
            # 未知类型，忽略
            continue

    if drill_progs:
        drill = ex.DrillProgram(drill_progs[0].name)
        for i, p in enumerate(drill_progs):
            drill.holes.extend(p.holes)
            drill.slots.extend(p.slots)
            for num, t in p.tools.items():
                key = num if i == 0 else num + 100 * (i + 1)
                while key in drill.tools:
                    key += 1
                drill.tools[key] = {"d": t["d"], "count": t["count"], "plated": t["plated"]}
                for h in drill.holes:
                    if h.get("tool") == num and h.get("_prog") is None:
                        h["_prog"] = i
            drill.bbox = geom.merge_bbox(drill.bbox, p.bbox)
        for h in drill.holes:
            h.pop("_prog", None)
        drill.stats = {"holes": len(drill.holes), "slots": len(drill.slots), "tools": len(drill.tools)}
        drill.units = drill_progs[0].units
        b.drills = drill


def _uniq(name, used):
    base = name or "layer"
    cand = base
    i = 2
    while cand in used:
        cand = "%s_%d" % (base, i)
        i += 1
    used.add(cand)
    return cand


def _from_single(path):
    b = Board()
    b.path = path
    b.name = os.path.splitext(os.path.basename(path))[0]
    text = _read_text(path)
    kind = sniff_text(text)
    base = os.path.basename(path)
    if kind == "gerber":
        img = gb.parse_gerber(text, base)
        lk, side = classify_name(base)
        b.layers.append(_new_layer(img.name or b.name, path, lk, side, img.objects,
                                   img.bbox, img.stats, img.warnings, img.attrs))
        b.warnings.extend(img.warnings)
    elif kind == "excellon":
        b.drills = ex.parse_excellon(text, base)
        b.warnings.extend(b.drills.warnings)
    elif kind == "ipc356":
        b.netlists.append(ipc.parse_ipc356(text, base, source=path))
        b.format = "netlist"
        b.warnings.append("仅加载了网表文件，没有图形层")
    else:
        raise ValueError("无法识别的文件类型：%s" % base)
    _finalize(b)
    return b


# --------------------------------------------------------------------------- #
# 汇总分析
# --------------------------------------------------------------------------- #
def _finalize(b):
    # 层排序：铜箔按 top/inner/bottom，其余按类型
    order = {"copper": 0, "mask": 1, "silk": 2, "paste": 3,
             "outline": 4, "rout": 5, "doc": 6, "component": 7, "other": 8}
    b.layers.sort(key=lambda l: (order.get(l["kind"], 9), l.get("name", "")))
    # 内层铜箔配色
    inner = [l for l in b.layers if l["kind"] == "copper" and l["side"] == "inner"]
    for i, l in enumerate(inner):
        l["color"] = _INNER_COLORS[i % len(_INNER_COLORS)]

    bb = None
    for l in b.layers:
        bb = geom.merge_bbox(bb, l.get("bbox"))
    if b.drills and b.drills.bbox:
        bb = geom.merge_bbox(bb, b.drills.bbox)
    b.bbox = bb
    # 器件所在面：ODB++ 的 mirror 标记并不总是可靠，用铜箔焊盘几何反推一次
    if b.components:
        for c in b.components:
            if not c.get("package"):
                c["package"] = c.get("part") or ""
        try:
            xy.infer_sides(b.components, b, tol=1.5)
        except Exception:
            pass
    b.analysis = _analyze(b)


def _analyze(b):
    copper = [l for l in b.layers if l["kind"] == "copper"]
    masks = [l for l in b.layers if l["kind"] == "mask"]
    silks = [l for l in b.layers if l["kind"] == "silk"]
    outline = [l for l in b.layers if l["kind"] == "outline"]
    holes = (b.drills.holes if b.drills else [])
    slots = (b.drills.slots if b.drills else [])

    warns = list(b.warnings)
    if not copper:
        warns.append("未找到铜箔层")
    else:
        sides = {l["side"] for l in copper}
        if "top" in sides and "bottom" not in sides and len(copper) == 1:
            pass
        elif "top" in sides and "bottom" not in sides:
            warns.append("有顶层铜箔但缺少底层铜箔，请确认资料完整性")
    if not b.drills:
        warns.append("未找到钻孔文件（.drl / Excellon）")

    min_widths = []
    for l in copper:
        w = (l.get("stats") or {}).get("min_width")
        if w:
            min_widths.append({"layer": l["name"], "w": w})
    min_w = min((m["w"] for m in min_widths), default=None)

    # 环宽检查
    ring = _annular_check(copper, holes)

    # 网表比对
    cmp = None
    if len(b.netlists) >= 2:
        cmp = ipc.compare(b.netlists[0], b.netlists[1])

    area_cm2 = sum(((l.get("stats") or {}).get("area") or 0.0) for l in copper) / 100.0
    drill_stats = None
    if b.drills:
        dias = [h["d"] for h in holes]
        drill_stats = {
            "holes": len(holes),
            "slots": len(slots),
            "tools": len(b.drills.tools),
            "min_dia": round(min(dias), 3) if dias else None,
            "max_dia": round(max(dias), 3) if dias else None,
            "plated": bool(b.drills.plated),
        }
        if dias:
            if min(dias) < 0.15:
                warns.append("存在小于 0.15mm 的钻孔（最小 %.3fmm），请确认工艺能力" % min(dias))
    if min_w is not None and min_w < 0.09:
        warns.append("最小线宽 %.3fmm 偏细（<0.09mm），请确认工厂能力" % min_w)

    size = None
    if b.bbox:
        size = {"w": round(b.bbox[2] - b.bbox[0], 3),
                "h": round(b.bbox[3] - b.bbox[1], 3)}

    total_objects = sum(len(l["objects"]) for l in b.layers)

    # 源文件单位：Gerber %MOIN/%MOMM、ODB++ UNITS=、Excellon M71/M72
    layer_units = []
    for l in b.layers:
        u = (l.get("stats") or {}).get("source_unit") or "mm"
        if u not in layer_units:
            layer_units.append(u)
    drill_unit = (b.drills.units if b.drills else None)
    units_in = {"layers": layer_units, "drill": drill_unit}
    if len(layer_units) > 1:
        warns.append("图层源单位不一致（%s），已统一折算为 mm，请核实换算是否正确"
                     % " / ".join(layer_units))
    if drill_unit and layer_units and units_in["drill"] not in layer_units \
            and len(layer_units) == 1:
        warns.append("钻孔文件单位为 %s，图形层为 %s（已统一折算为 mm，请核实）"
                     % (drill_unit, layer_units[0]))

    return {
        "board_size": size,
        "layer_count": len(b.layers),
        "copper_count": len(copper),
        "mask_count": len(masks),
        "silk_count": len(silks),
        "outline_count": len(outline),
        "total_objects": total_objects,
        "copper_area_cm2": round(area_cm2, 2),
        "min_widths": min_widths,
        "min_trace_width": min_w,
        "drill": drill_stats,
        "annular": ring,
        "netlist_compare": cmp,
        "units_in": units_in,
        "warnings": warns,
    }


def _annular_check(copper, holes, cap=600000):
    """粗算环宽（焊盘半径 - 钻孔半径）与“孔无铜”数量。"""
    pads = []
    for l in copper:
        if l["side"] not in ("top", "bottom", "both", "inner"):
            continue
        for o in l["objects"]:
            if o.get("neg"):
                continue
            if o.get("t") not in ("circle", "ring", "poly", "poly2", "group"):
                continue
            bb = geom.obj_bbox(o)
            if not bb:
                continue
            pads.append((o, bb, l["name"]))
    if not holes:
        return {"checked": 0, "min_ring": None, "thin_ring": 0, "no_pad": 0, "samples": []}
    if not pads:
        return {"checked": 0, "min_ring": None, "thin_ring": 0, "no_pad": len(holes), "samples": []}

    cell = 3.0
    grid = {}
    for idx, (_o, bb, _ln) in enumerate(pads):
        x0, y0 = int(math.floor(bb[0] / cell)), int(math.floor(bb[1] / cell))
        x1, y1 = int(math.floor(bb[2] / cell)), int(math.floor(bb[3] / cell))
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                grid.setdefault((gx, gy), []).append(idx)

    checked = 0
    no_pad = 0
    thin = 0
    min_ring = None
    samples = []
    limit = max(1, min(len(holes), cap // max(1, len(pads)) or 1))
    step = max(1, len(holes) // limit)

    for h in holes[::step]:
        x, y, d = h["x"], h["y"], h["d"]
        cand = grid.get((int(math.floor(x / cell)), int(math.floor(y / cell))), [])
        best = None
        best_layer = ""
        for idx in cand:
            o, bb, lname = pads[idx]
            if not (bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3]):
                continue
            if not _pad_covers(o, x, y):
                continue
            r = _ring_of(o, x, y)
            if r is None:
                continue
            val = r - d / 2.0
            if best is None or val < best:
                best = val
                best_layer = lname
        checked += 1
        if best is None:
            no_pad += 1
            continue
        if min_ring is None or best < min_ring:
            min_ring = best
        if best < 0.05:
            thin += 1
            if len(samples) < 8:
                samples.append({"x": round(x, 2), "y": round(y, 2),
                                "d": round(d, 3), "ring": round(best, 3), "layer": best_layer})
    return {
        "checked": checked,
        "min_ring": round(min_ring, 3) if min_ring is not None else None,
        "thin_ring": thin,
        "no_pad": no_pad,
        "samples": samples,
        "note": "基于焊盘中心包含关系估算，供快速筛查",
    }


def _pad_covers(obj, x, y):
    """焊盘是否覆盖该点（环形焊盘按外圆判断，孔内的点也算被焊盘覆盖）。"""
    return geom.pad_covers(obj, x, y)


def _ring_of(obj, x, y):
    t = obj.get("t")
    if t in ("circle", "ring"):
        return obj["r"]
    if t == "group":
        b = obj.get("b") or [0, 0, 0, 0]
        return min(b[2] - b[0], b[3] - b[1]) / 2.0
    pts = obj.get("p") or obj.get("out") or []
    if len(pts) < 3:
        return None
    best = None
    for i in range(len(pts)):
        a = pts[i]
        b = pts[(i + 1) % len(pts)]
        dist = geom.dist_point_seg(x, y, a[0], a[1], b[0], b[1])
        if best is None or dist < best:
            best = dist
    return best


# --------------------------------------------------------------------------- #
# 供前端使用
# --------------------------------------------------------------------------- #
def board_summary(b: Board):
    layers = []
    for i, l in enumerate(b.layers):
        layers.append({
            "index": i,
            "name": l["name"],
            "file": os.path.basename(l.get("file") or ""),
            "kind": l["kind"],
            "side": l["side"],
            "color": l["color"],
            "count": len(l["objects"]),
            "bbox": [round(v, 3) for v in l["bbox"]] if l.get("bbox") else None,
            "stats": l.get("stats") or {},
            "warnings": l.get("warnings") or [],
            "visible": l.get("visible", True),
            "opacity": l.get("opacity", 1.0),
        })
    drills = None
    if b.drills:
        d = b.drills
        drills = {
            "holes": d.holes,
            "slots": d.slots,
            "tools": [{"num": n, "d": round(t["d"], 4), "count": t["count"],
                       "plated": t["plated"]} for n, t in sorted(d.tools.items())],
            "stats": d.stats,
            "units": d.units,
            "plated": d.plated,
        }
    nets = []
    for nl in b.netlists:
        nets.append({
            "name": nl.name,
            "source": os.path.basename(nl.source or ""),
            "units": nl.units,
            "net_count": len(nl.nets),
            "point_count": len(nl.points),
            "top_nets": sorted(nl.nets.items(), key=lambda kv: -len(kv[1]))[:20],
        })
    return {
        "path": b.path,
        "name": b.name,
        "format": b.format,
        "step": b.step,
        "bbox": [round(v, 3) for v in b.bbox] if b.bbox else None,
        "layers": layers,
        "drills": drills,
        "netlists": nets,
        "components": b.components[:5000],
        "analysis": b.analysis,
        "warnings": b.analysis.get("warnings", []) if b.analysis else b.warnings,
    }


def layer_objects(b: Board, index: int):
    if index < 0 or index >= len(b.layers):
        raise IndexError("layer index out of range")
    l = b.layers[index]
    return {"index": index, "name": l["name"], "kind": l["kind"], "side": l["side"],
            "color": l["color"], "objects": l["objects"]}
