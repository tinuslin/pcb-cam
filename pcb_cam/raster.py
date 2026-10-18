"""纯标准库光栅化 + PNG 输出（用于 headless 预览，不依赖 Pillow）。

只支持本工具用到的图元：多边形填充（含孔洞）、折线描边、圆、圆环。
负极性图元（neg=1）在图层缓冲里擦除像素，再整体合成到底图。
"""
from __future__ import annotations

import math
import struct
import zlib


class LayerBuf:
    """单个图层的 RGBA 缓冲（透明底，可被负极性擦除）。"""

    def __init__(self, w, h):
        self.w = w
        self.h = h
        self.rgba = bytearray(w * h * 4)
        self.dirty = None      # (x0, y0, x1, y1)

    def _touch(self, x0, y0, x1, y1):
        if self.dirty is None:
            self.dirty = [x0, y0, x1, y1]
        else:
            d = self.dirty
            d[0] = min(d[0], x0)
            d[1] = min(d[1], y0)
            d[2] = max(d[2], x1)
            d[3] = max(d[3], y1)

    def fill_polys(self, contours, color, erase=False):
        """偶奇规则填充多个轮廓（后一个轮廓可形成孔）。"""
        edges = []
        for pts in contours:
            n = len(pts)
            if n < 3:
                continue
            for i in range(n):
                x1, y1 = pts[i]
                x2, y2 = pts[(i + 1) % n]
                if y1 == y2:
                    continue
                if y1 > y2:
                    x1, y1, x2, y2 = x2, y2, x1, y1
                edges.append((x1, y1, x2, y2))
        if not edges:
            return
        ymin = max(0, int(min(e[1] for e in edges)))
        ymax = min(self.h - 1, int(max(e[3] for e in edges)) + 1)
        r, g, b = color
        w = self.w
        rgba = self.rgba
        touched = False
        for y in range(ymin, ymax + 1):
            yc = y + 0.5
            xs = []
            for x1, y1, x2, y2 in edges:
                if y1 <= yc < y2:
                    t = (yc - y1) / (y2 - y1)
                    xs.append(x1 + t * (x2 - x1))
            if len(xs) < 2:
                continue
            xs.sort()
            base = y * w * 4
            for k in range(0, len(xs) - 1, 2):
                xa = max(0, int(math.ceil(xs[k] - 0.5)))
                xb = min(w - 1, int(math.floor(xs[k + 1] - 0.5)))
                if xb < xa:
                    continue
                touched = True
                for x in range(xa, xb + 1):
                    i = base + x * 4
                    if erase:
                        rgba[i] = 0
                        rgba[i + 1] = 0
                        rgba[i + 2] = 0
                        rgba[i + 3] = 0
                    else:
                        rgba[i] = r
                        rgba[i + 1] = g
                        rgba[i + 2] = b
                        rgba[i + 3] = 255
                self._touch(xa, y, xb, y) if touched else None
        return

    def stroke(self, pts, width_px, color, erase=False):
        if not pts or len(pts) < 2:
            return
        r = max(width_px / 2.0, 0.55)
        if r <= 0.8:
            # 直接用细线扫描
            for i in range(1, len(pts)):
                self._thin_line(pts[i - 1], pts[i], color, erase)
            return
        for i in range(1, len(pts)):
            (x1, y1), (x2, y2) = pts[i - 1], pts[i]
            dx, dy = x2 - x1, y2 - y1
            L = math.hypot(dx, dy)
            if L < 1e-9:
                continue
            nx, ny = -dy / L * r, dx / L * r
            quad = [(x1 + nx, y1 + ny), (x2 + nx, y2 + ny),
                    (x2 - nx, y2 - ny), (x1 - nx, y1 - ny)]
            self.fill_polys([quad], color, erase)
            self.circle(x1, y1, r, color, erase)
            self.circle(x2, y2, r, color, erase)

    def _thin_line(self, p1, p2, color, erase):
        x1, y1 = p1
        x2, y2 = p2
        steps = int(max(abs(x2 - x1), abs(y2 - y1))) + 1
        steps = min(steps, 20000)
        r, g, b = color
        for k in range(steps + 1):
            t = k / steps
            x = int(round(x1 + (x2 - x1) * t))
            y = int(round(y1 + (y2 - y1) * t))
            if 0 <= x < self.w and 0 <= y < self.h:
                i = (y * self.w + x) * 4
                if erase:
                    self.rgba[i] = self.rgba[i + 1] = self.rgba[i + 2] = self.rgba[i + 3] = 0
                else:
                    self.rgba[i] = r
                    self.rgba[i + 1] = g
                    self.rgba[i + 2] = b
                    self.rgba[i + 3] = 255
                self._touch(x, y, x, y)

    def circle(self, cx, cy, r_px, color, erase=False, segments=36):
        if r_px <= 0.55:
            self._thin_line((cx, cy), (cx, cy), color, erase)
            return
        pts = [(cx + r_px * math.cos(2 * math.pi * k / segments),
                cy + r_px * math.sin(2 * math.pi * k / segments)) for k in range(segments)]
        self.fill_polys([pts], color, erase)

    def ring(self, cx, cy, r_px, ri_px, color, erase=False, segments=40):
        outer = [(cx + r_px * math.cos(2 * math.pi * k / segments),
                  cy + r_px * math.sin(2 * math.pi * k / segments)) for k in range(segments)]
        inner = [(cx + max(ri_px, 0.4) * math.cos(2 * math.pi * k / segments),
                  cy + max(ri_px, 0.4) * math.sin(2 * math.pi * k / segments)) for k in range(segments)]
        self.fill_polys([outer, inner], color, erase)

    def blit_to_rgba(self, dst, dst_w, dx, dy):
        """把本缓冲按 alpha 覆盖到另一张 RGBA 缓冲（用于闪光组）。"""
        if self.dirty is None:
            return
        x0, y0, x1, y1 = self.dirty
        x0 = max(0, int(x0)); y0 = max(0, int(y0))
        x1 = min(self.w - 1, int(x1)); y1 = min(self.h - 1, int(y1))
        h = len(dst) // 4 // dst_w if dst_w else 0
        rgba = self.rgba
        for y in range(y0, y1 + 1):
            ty = y + dy
            if ty < 0 or ty >= h:
                continue
            for x in range(x0, x1 + 1):
                tx = x + dx
                if tx < 0 or tx >= dst_w:
                    continue
                i = (y * self.w + x) * 4
                a = rgba[i + 3]
                if not a:
                    continue
                j = (ty * dst_w + tx) * 4
                if a >= 250 or dst[j + 3] == 0:
                    dst[j] = rgba[i]
                    dst[j + 1] = rgba[i + 1]
                    dst[j + 2] = rgba[i + 2]
                    dst[j + 3] = 255
                else:
                    ia = 255 - a
                    dst[j] = (rgba[i] * a + dst[j] * ia) // 255
                    dst[j + 1] = (rgba[i + 1] * a + dst[j + 1] * ia) // 255
                    dst[j + 2] = (rgba[i + 2] * a + dst[j + 2] * ia) // 255
                    dst[j + 3] = max(dst[j + 3], a)

    def composite_to(self, dst, alpha=1.0):
        if self.dirty is None:
            return
        x0, y0, x1, y1 = self.dirty
        x0 = max(0, int(x0)); y0 = max(0, int(y0))
        x1 = min(self.w - 1, int(x1)); y1 = min(self.h - 1, int(y1))
        rgba = self.rgba
        w = self.w
        a_mul = max(0.0, min(1.0, alpha))
        for y in range(y0, y1 + 1):
            row = y * w
            for x in range(x0, x1 + 1):
                i = (row + x) * 4
                a = rgba[i + 3]
                if not a:
                    continue
                if a_mul < 1.0:
                    a = int(a * a_mul)
                j = (row + x) * 3
                if a >= 250:
                    dst[j] = rgba[i]
                    dst[j + 1] = rgba[i + 1]
                    dst[j + 2] = rgba[i + 2]
                else:
                    ia = 255 - a
                    dst[j] = (rgba[i] * a + dst[j] * ia) // 255
                    dst[j + 1] = (rgba[i + 1] * a + dst[j + 1] * ia) // 255
                    dst[j + 2] = (rgba[i + 2] * a + dst[j + 2] * ia) // 255


class Canvas:
    def __init__(self, w, h, bg=(14, 19, 25)):
        self.w = w
        self.h = h
        self.buf = bytearray(w * h * 3)
        row = bytes(bg) * w
        for y in range(h):
            self.buf[y * w * 3:(y + 1) * w * 3] = row

    def new_layer(self):
        return LayerBuf(self.w, self.h)

    def line_direct(self, x1, y1, x2, y2, color):
        steps = int(max(abs(x2 - x1), abs(y2 - y1))) + 1
        steps = max(1, min(steps, 20000))
        r, g, b = color
        for k in range(steps + 1):
            t = k / steps
            x = int(round(x1 + (x2 - x1) * t))
            y = int(round(y1 + (y2 - y1) * t))
            if 0 <= x < self.w and 0 <= y < self.h:
                j = (y * self.w + x) * 3
                self.buf[j] = r
                self.buf[j + 1] = g
                self.buf[j + 2] = b


def write_png(path, w, h, rgb):
    raw = bytearray()
    stride = w * 3
    for y in range(h):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def hex_rgb(s, default=(255, 255, 255)):
    s = (s or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return default
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return default
