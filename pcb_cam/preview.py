"""headless PNG 预览（不依赖 Pillow）。

用法：
    python -m pcb_cam.preview 资料路径 -o out.png -w 1800 --theme dark
    python -m pcb_cam.preview tests/samples/demo-odb.tgz -o odb.png --layers top,soldermask_top
"""
from __future__ import annotations

import argparse
import math
import os
import sys

from . import project, raster, units

THEMES = {
    "dark": {"bg": (13, 18, 24), "drill": (242, 201, 76), "hole": (13, 18, 24)},
    "light": {"bg": (240, 243, 248), "drill": (196, 146, 20), "hole": (255, 255, 255)},
}


def _draw_obj(buf, o, to_px, scale, color):
    """把一个图元画进缓冲；to_px 负责世界坐标->该缓冲像素坐标。"""
    erase = bool(o.get("neg"))
    t = o.get("t")
    if t == "path":
        pts = [to_px(p) for p in o["p"]]
        buf.stroke(pts, max((o.get("w") or 0.02) * scale, 0.9), color, erase)
    elif t == "poly":
        buf.fill_polys([[to_px(p) for p in o["p"]]], color, erase)
    elif t == "circle":
        cx, cy = to_px(o["c"])
        buf.circle(cx, cy, o["r"] * scale, color, erase)
    elif t == "ring":
        cx, cy = to_px(o["c"])
        buf.ring(cx, cy, o["r"] * scale, o["ri"] * scale, color, erase)
    elif t == "poly2":
        contours = [[to_px(p) for p in o["out"]]]
        for h in o.get("in") or []:
            contours.append([to_px(p) for p in h])
        buf.fill_polys(contours, color, erase)
    elif t == "group":
        b = o.get("b") or [0, 0, 0, 0]
        gx0, gy0 = to_px((b[0], b[3]))
        gx1, gy1 = to_px((b[2], b[1]))
        gw = int(math.ceil(gx1 - gx0)) + 2
        gh = int(math.ceil(gy1 - gy0)) + 2
        if gw <= 0 or gh <= 0 or gw * gh > 8_000_000:
            return
        tmp = raster.LayerBuf(gw, gh)
        ox, oy = gx0, gy0
        for it in o["items"]:
            _draw_obj(tmp, it, lambda p: (to_px(p)[0] - ox, to_px(p)[1] - oy), scale, color)
        tmp.blit_to_rgba(buf.rgba, buf.w, int(round(gx0)), int(round(gy0)))


STATUS_RGB = {
    "ok": (47, 158, 107),
    "offset": (224, 160, 30),
    "wrong_side": (224, 160, 30),
    "no_pad": (224, 67, 67),
    "outside": (200, 72, 156),
}


def _cross(buf, x, y, color, arm=7.0, thick=1.3, ring=0.0):
    """在缓冲上画一个十字标记（可选外圈），用于元件 / 定位点的可视化核对。"""
    buf.stroke([(x - arm, y), (x + arm, y)], thick, color)
    buf.stroke([(x, y - arm), (x, y + arm)], thick, color)
    if ring > 0:
        buf.ring(x, y, ring + thick, ring - thick * 0.4, color)


def render_board(board, out_path, width=1600, theme="dark", margin=26,
                 only_layers=None, with_drills=True,
                 marks=None, target=None):
    """渲染成 PNG。

    ``marks``   : [(x, y, status)] —— 元件坐标十字（status 决定颜色）
    ``target``  : (x, y) —— 定位目标点，画蓝色准星 + 全幅虚线十字
    """
    bb = board.bbox or (0.0, 0.0, 1.0, 1.0)
    w_mm = max(1e-3, bb[2] - bb[0])
    h_mm = max(1e-3, bb[3] - bb[1])
    inner = max(120, int(width) - margin * 2)
    scale = inner / w_mm
    W = int(w_mm * scale + margin * 2)
    H = int(h_mm * scale + margin * 2)
    th = THEMES.get(theme, THEMES["dark"])
    cv = raster.Canvas(W, H, th["bg"])
    ox = margin - bb[0] * scale
    oy = margin + bb[3] * scale

    def toS(p):
        return (ox + p[0] * scale, oy - p[1] * scale)

    for layer in board.layers:
        if only_layers and layer["name"] not in only_layers:
            continue
        col = raster.hex_rgb(layer.get("color"))
        alpha = float(layer.get("opacity", 1.0) or 1.0)
        lb = cv.new_layer()
        for o in layer.get("objects") or []:
            _draw_obj(lb, o, toS, scale, col)
        lb.composite_to(cv.buf, alpha)

    if with_drills and board.drills:
        dl = cv.new_layer()
        dcol = th["drill"]
        for h in board.drills.holes:
            cx, cy = toS((h["x"], h["y"]))
            r = max(h["d"] / 2.0 * scale, 1.6)
            dl.circle(cx, cy, r, dcol)
            dl.circle(cx, cy, max(r - 1.3, 0.6), th["hole"])
        for s in board.drills.slots:
            pts = [toS(p) for p in s.get("p") or []]
            dl.stroke(pts, max((s.get("w") or 0.2) * scale, 1.4), dcol)
        dl.composite_to(cv.buf)

    if marks:
        ml = cv.new_layer()
        arm = max(7.0, W * 0.010)
        thick = max(1.2, W * 0.0018)
        ring = max(3.2, W * 0.0052)
        for item in marks:
            try:
                mx, my, st = item[0], item[1], (item[2] if len(item) > 2 else "ok")
            except (TypeError, IndexError):
                continue
            col = STATUS_RGB.get(st or "ok", (143, 163, 186))
            px, py = toS((mx, my))
            _cross(ml, px, py, col, arm=arm, thick=thick, ring=ring)
        ml.composite_to(cv.buf)

    if target:
        tl = cv.new_layer()
        tx, ty = toS((target[0], target[1]))
        dash = (78, 161, 255)
        for x0 in range(0, W, 14):
            tl.stroke([(x0, ty), (min(x0 + 8, W), ty)], 1.0, dash)
        for y0 in range(0, H, 14):
            tl.stroke([(tx, y0), (tx, min(y0 + 8, H))], 1.0, dash)
        tl.ring(tx, ty, 11.0, 9.6, dash)
        tl.circle(tx, ty, 2.4, dash)
        tl.composite_to(cv.buf)

    raster.write_png(out_path, W, H, cv.buf)
    return out_path, W, H


def main(argv=None):
    ap = argparse.ArgumentParser(description="PCB 资料 PNG 预览")
    ap.add_argument("path")
    ap.add_argument("-o", "--out", default="")
    ap.add_argument("-w", "--width", type=int, default=1600)
    ap.add_argument("--theme", default="dark", choices=list(THEMES))
    ap.add_argument("--layers", default="", help="逗号分隔的层名，只渲染这些层")
    ap.add_argument("--no-drill", action="store_true")
    ap.add_argument("--comps", action="store_true", help="叠加板上器件坐标十字")
    ap.add_argument("--xy", default="", help="叠加 XY 坐标文件，并按校验状态着色")
    ap.add_argument("--goto", default="", help="定位目标点，如 20,10（单位由 --units 指定）")
    ap.add_argument("--units", default="mm", choices=["mm", "mil", "inch"],
                    help="--goto 输入单位与打印单位（默认 mm）")
    args = ap.parse_args(argv)

    board = project.load_board(args.path)
    out = args.out or os.path.splitext(os.path.basename(args.path.rstrip("/\\")))[0] + ".png"
    only = [s.strip() for s in args.layers.split(",") if s.strip()] if args.layers else None

    marks, legend = [], []
    if args.comps:
        for c in board.components or []:
            marks.append((c.get("x"), c.get("y"), "ok"))
        legend.append("板上器件 %d 个（绿十字）" % len(marks))
    if args.xy:
        from . import project as _pr, xy as _xy
        text = _pr._read_text(args.xy)
        data = _xy.parse_xy(text, os.path.basename(args.xy), args.xy)
        _xy.align(data, board.bbox, board=board)
        chk = _xy.check(data, board, tol=0.2)
        for e in chk["entries"]:
            marks.append((e["x"], e["y"], e["status"]))
        st = chk["stats"]
        legend.append("XY %d 个：%s" % (
            chk["total"], " / ".join("%s=%d" % (k, v) for k, v in sorted(st.items()))))
    target = None
    if args.goto:
        try:
            gx, gy = [float(v) for v in args.goto.replace("，", ",").split(",")[:2]]
            f = units.FACTOR[units.norm(args.units)]
            target = (gx * f, gy * f)
            legend.append("定位目标 %g, %g %s = %s × %s（蓝色准星）" % (
                gx, gy, units.norm(args.units),
                units.len(target[0], "mm", 2), units.len(target[1], "mm", 2)))
        except (ValueError, IndexError):
            print("! --goto 格式应为 X,Y")

    render_board(board, out, args.width, args.theme, only_layers=only,
                 with_drills=not args.no_drill, marks=marks, target=target)

    a = board.analysis
    print("已生成：%s" % os.path.abspath(out))
    print("  格式 %s / 图层 %d / 图元 %s" % (board.format.upper(), len(board.layers),
                                          a.get("total_objects")))
    for line in legend:
        print("  " + line)
    if a.get("board_size"):
        w, h = a["board_size"]["w"], a["board_size"]["h"]
        print("  板面 %s × %s（%s × %s mil）" % (
            units.len(w, "mm", 2), units.len(h, "mm", 2),
            units.fmt(w, "mil", 1), units.fmt(h, "mil", 1)))
    print("  最小线宽 %s，最小环宽 %s" % (
        units.dual(a.get("min_trace_width")),
        units.dual((a.get("annular") or {}).get("min_ring"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
