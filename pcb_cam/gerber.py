"""Gerber RS-274X（Extended Gerber）解析器。

支持：
  * %FS% 格式声明（前/后零省略、绝对/增量、X/Y 整数与小数位）
  * %MO% 单位（mm / inch）
  * %AD% 标准光圈（C / R / O / P，含中心孔）
  * %AM% 光圈宏（原语 1/20/21/4/5/6/7/24 + 变量与算术表达式 + 负曝光）
  * %LP% 极性（正/负）、%AB% 光圈块、%SR% 步进重复
  * G01/G02/G03 插补、G74/G75 单/多象限圆弧、G36/G37 区域
  * X2 属性（TF/TA/TO/TD/IP/IF/LN 等）与废弃图像变换（MI/IR/SF/IO/AS）

输出：图元列表（毫米坐标）
  {"t":"path",   "w":线宽, "p":[[x,y],...]}
  {"t":"poly",   "p":[[x,y],...]}
  {"t":"circle", "c":[x,y], "r":r}
  {"t":"ring",   "c":[x,y], "r":r, "ri":ri}
  {"t":"poly2",  "out":[...], "in":[[...],...]}
  可选 "neg": 1 表示负极性（该图元清除已有图形）
"""
from __future__ import annotations

import math
import re

from . import geom

_TWO_PI = geom.TWO_PI


class Aperture:
    __slots__ = ("code", "tpl", "params", "hole")

    def __init__(self, code, tpl, params, hole=0.0):
        self.code = code
        self.tpl = tpl
        self.params = params
        self.hole = hole


class GerberImage:
    """单个 Gerber 层的解析结果。"""

    def __init__(self, name=""):
        self.name = name
        self.objects = []          # 图元（顺序敏感，含 neg 标记）
        self.bbox = None
        self.warnings = []
        self.attrs = {}
        self.apertures = {}
        self.stats = {
            "draws": 0, "arcs": 0, "flashes": 0, "regions": 0,
            "objects": 0, "skipped": 0,
            "min_width": None, "area": 0.0,
        }

    def to_dict(self):
        st = dict(self.stats)
        st["objects"] = len(self.objects)
        return {
            "name": self.name,
            "objects": self.objects,
            "bbox": payload_bbox(self.bbox),
            "warnings": self.warnings,
            "attrs": self.attrs,
            "stats": st,
            "apertures": [
                {"code": a.code, "shape": a.tpl, "params": a.params, "hole": a.hole}
                for a in self.apertures.values()
            ],
        }


def payload_bbox(b):
    if not b:
        return None
    return [round(b[0], 4), round(b[1], 4), round(b[2], 4), round(b[3], 4)]


class GerberParser:
    def __init__(self, text: str, name: str = ""):
        self.img = GerberImage(name)
        self.text = text.replace("\r", "")
        self.warned = set()

        # 格式
        self.scale = 1.0            # 文件单位 -> mm
        self.src_unit = "mm"        # 源文件声明的单位（%MOIN / %MOMM）
        self.zs = "L"               # L 前零省略 / T 后零省略 / D 无省略
        self.notation = "A"         # A 绝对 / I 增量
        self.xi, self.xd = 3, 6
        self.yi, self.yd = 3, 6
        self.fmt_seen = False
        self.unit_seen = False

        # 状态
        self.apertures = {}
        self.macros = {}
        self.cur_ap = None
        self.cur = (0.0, 0.0)
        self.interp = 1
        self.quadrant = "multi"
        self.neg = False
        self.objects = []

        # 区域
        self.region = False
        self.contour = []

        # 光圈块 / 步进重复
        self.blocks = {}
        self.ab_code = None
        self.ab_buf = None
        self.sr = None
        self.sr_buf = None

        # 废弃图像变换
        self.mirror_x = False
        self.mirror_y = False
        self.rotation = 0.0
        self.sf = 1.0
        self.ox = 0.0
        self.oy = 0.0
        self.axis_swap = False

        # 统计
        self._bbox = None
        self._min_width = None
        self._area = 0.0

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def warn(self, key, msg):
        if key not in self.warned:
            self.warned.add(key)
            self.img.warnings.append(msg)

    def _select_ap(self, code):
        ap = self.apertures.get(code)
        if ap is None:
            ap = self.img.apertures.get(code)
        if ap is None:
            self.warn("ap%d" % code, "D%d 光圈未定义（已忽略相关操作）" % code)
            self.cur_ap = None
        else:
            self.cur_ap = ap

    def _num(self, s, axis="X"):
        """按 %FS% 声明把字符串坐标转成浮点（文件单位）。"""
        if s is None or s == "":
            return None
        if "." in s:
            try:
                return float(s)
            except ValueError:
                return None
        neg = s.startswith("-")
        dg = s.lstrip("+-")
        if not dg.isdigit():
            return None
        intd, decd = (self.xi, self.xd) if axis in ("X", "I") else (self.yi, self.yd)
        total = max(1, intd + decd)
        if self.zs == "T":
            if len(dg) < total:
                dg = dg.ljust(total, "0")
        elif self.zs == "D":
            pass
        else:
            if len(dg) < total:
                dg = dg.rjust(total, "0")
        try:
            v = int(dg) / (10.0 ** decd)
        except ValueError:
            return None
        return -v if neg else v

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def parse(self):
        text = self.text
        i, n = 0, len(text)
        while i < n:
            c = text[i]
            if c in " \t\n":
                i += 1
                continue
            if c == "%":
                j = text.find("%", i + 1)
                if j < 0:
                    j = n
                try:
                    self._ext(text[i + 1:j])
                except Exception as exc:      # 容错：单条扩展指令失败不影响整体
                    self.warn("ext", "扩展指令解析异常：%s" % exc)
                i = j + 1
            else:
                j = text.find("*", i)
                if j < 0:
                    j = n
                try:
                    self._std(text[i:j])
                except Exception as exc:
                    self.warn("std", "绘图指令解析异常：%s" % exc)
                i = j + 1
        self._finish()
        return self.img

    def _finish(self):
        if self.contour:
            self._push_contour(self.contour)
            self.contour = []
        if self.region:
            self._emit_region()
            self.region = False
        if not self.fmt_seen:
            self.warn("nofs", "缺少 %FS% 格式声明，已按 3.6 / 前零省略推定")
        if not self.unit_seen:
            self.warn("nomo", "缺少 %MO% 单位声明，已按毫米处理")
        if not self.objects:
            self.warn("empty", "未解析出任何图形")
        self.img.objects = self.objects
        self.img.bbox = self._bbox
        self.img.stats["min_width"] = (
            round(self._min_width * self.scale * (self.sf or 1.0), 4)
            if self._min_width is not None else None
        )
        self.img.stats["area"] = round(self._area, 3)
        self.img.stats["source_unit"] = self.src_unit
        self.img.apertures = self.apertures

    # ------------------------------------------------------------------ #
    # 扩展指令
    # ------------------------------------------------------------------ #
    def _ext(self, body):
        stmts = [s.strip() for s in body.split("*")]
        k = 0
        while k < len(stmts):
            s = stmts[k]
            k += 1
            if not s:
                continue
            code = s[:2].upper()
            if code == "AM":
                name = s[2:].strip()
                body_parts = []
                while k < len(stmts):
                    ps = stmts[k]
                    k += 1
                    if ps:
                        body_parts.append(ps)
                if name:
                    self.macros[name] = body_parts
                continue
            if code == "FS":
                m = re.match(r"^FS([LTD]?)([AI]?)X(\d)(\d)Y(\d)(\d)", s)
                if m:
                    self.zs = m.group(1) or "L"
                    self.notation = m.group(2) or "A"
                    self.xi, self.xd = int(m.group(3)), int(m.group(4))
                    self.yi, self.yd = int(m.group(5)), int(m.group(6))
                    self.fmt_seen = True
            elif code == "MO":
                u = s[2:4].upper()
                if u == "IN":
                    self.scale = geom.MM_PER_INCH
                    self.src_unit = "inch"
                elif u == "MM":
                    self.scale = 1.0
                    self.src_unit = "mm"
                self.unit_seen = True
            elif code == "AD":
                self._parse_ad(s)
            elif code == "LP":
                # LPD = 正性（绘制），LPC = 负性（清除）
                self.neg = s[2:3].upper() == "C"
            elif code == "AB":
                self._parse_ab(s)
            elif code == "SR":
                self._parse_sr(s)
            elif code in ("TF", "TA", "TO", "TD"):
                self._parse_attr(s)
            elif code == "AS":
                self.axis_swap = "BY" in s.upper()
            elif code == "IR":
                try:
                    self.rotation = float(s[2:] or 0)
                except ValueError:
                    pass
            elif code == "MI":
                v = s[2:4].upper()
                self.mirror_x = "X" in v
                self.mirror_y = "Y" in v
            elif code == "SF":
                try:
                    self.sf = float(s[2:].split("A")[0] or 1.0)
                except ValueError:
                    self.sf = 1.0
            elif code in ("IO", "OF"):
                m = re.search(r"X([+-]?[\d.]+)", s)
                if m:
                    self.ox = float(m.group(1)) * self.scale
                m = re.search(r"Y([+-]?[\d.]+)", s)
                if m:
                    self.oy = float(m.group(1)) * self.scale
            elif code in ("IP", "IN", "LN", "LM", "LR", "LS", "IF", "FA", "IJ", "KO"):
                if code == "LN":
                    self.img.name = s[2:].split(",")[0] or self.img.name
            else:
                # 未知扩展指令：忽略
                self.warn("ext_" + code, "忽略未知扩展指令 %%%s%%" % code)

    def _parse_attr(self, s):
        # %TF.FileFunction,Copper,L1,Top*%   %TA.AperFunction,SMDPad*%   %TD*%
        code = s[:2].upper()
        if code == "TD":
            return
        body = s[2:]
        if not body:
            return
        key, _, val = body.partition(",")
        key = key.strip()
        if code == "TF":
            self.img.attrs[key] = val.strip()
        elif code == "TO":
            if key.startswith(".P"):
                self.img.attrs.setdefault("_pad_attrs", []).append(val)

    def _parse_ad(self, s):
        m = re.match(r"^ADD(\d+)([^,]+),?(.*)$", s)
        if not m:
            self.warn("ad", "无法解析光圈定义：%s" % s[:40])
            return
        code = int(m.group(1))
        tpl = m.group(2).strip()
        raw = m.group(3).strip()
        nums = []
        for part in raw.split("X"):
            part = part.strip()
            if not part:
                continue
            try:
                nums.append(float(part))
            except ValueError:
                nums.append(0.0)
        hole = 0.0
        up = tpl.upper()
        if up == "C":
            if len(nums) >= 2:
                hole = abs(nums[1])
        elif up == "R":
            if len(nums) >= 3:
                hole = abs(nums[2])
        elif up == "O":
            if len(nums) >= 3:
                hole = abs(nums[2])
        elif up == "P":
            if len(nums) >= 4:
                hole = abs(nums[3])
        ap = Aperture(code, tpl, nums, hole)
        self.apertures[code] = ap
        self.img.apertures[code] = ap

    def _parse_ab(self, s):
        # %AB D12*% 开始定义块；%AB*% 结束
        m = re.search(r"D(\d+)", s)
        if m and self.ab_buf is None:
            self.ab_code = int(m.group(1))
            self.ab_buf = []
        else:
            if self.ab_code is not None and self.ab_buf is not None:
                self.blocks[self.ab_code] = self.ab_buf
            self.ab_code = None
            self.ab_buf = None

    def _parse_sr(self, s):
        m = re.match(r"^SRX(\d+)Y(\d+)I([\d.\-]+)J([\d.\-]+)", s)
        if m and self.sr_buf is None:
            dx = self._num(m.group(3), "I") or 0.0
            dy = self._num(m.group(4), "J") or 0.0
            self.sr = (max(1, int(m.group(1))), max(1, int(m.group(2))), dx, dy)
            self.sr_buf = []
        else:
            self._flush_sr()

    def _flush_sr(self):
        if self.sr and self.sr_buf:
            nx, ny, dx, dy = self.sr
            base = self.sr_buf
            for ix in range(nx):
                for iy in range(ny):
                    if ix == 0 and iy == 0:
                        for obj, ng in base:
                            self._store(obj, ng)
                        continue
                    ox, oy = ix * dx * self.scale, iy * dy * self.scale
                    for obj, ng in base:
                        self._store(self._translate(obj, ox, oy), ng)
        self.sr = None
        self.sr_buf = None

    # ------------------------------------------------------------------ #
    # 标准指令
    # ------------------------------------------------------------------ #
    def _std(self, s):
        s = s.strip()
        if not s:
            return
        head = s[:3].upper()
        if head == "G04" or head == "M02" or head == "M00" or head == "M01" or head == "M30":
            return
        pairs = re.findall(r"([GDMXYIJ])([+-]?[0-9.]+)", s)
        if not pairs:
            return
        x = y = iv = jv = None
        for letter, val in pairs:
            if letter == "G":
                try:
                    g = int(float(val))
                except ValueError:
                    continue
                if g in (1, 2, 3):
                    self.interp = g
                elif g == 36:
                    self.region = True
                    self.contour = []
                elif g == 37:
                    if self.contour:
                        self._push_contour(self.contour)
                        self.contour = []
                    self._emit_region()
                    self.region = False
                elif g == 74:
                    self.quadrant = "single"
                elif g == 75:
                    self.quadrant = "multi"
                elif g == 70:
                    self.scale = geom.MM_PER_INCH
                    self.unit_seen = True
                elif g == 71:
                    self.scale = 1.0
                    self.unit_seen = True
                elif g == 90:
                    self.notation = "A"
                elif g == 91:
                    self.notation = "I"
                # G54/G55 旧式前缀、G05 等忽略
            elif letter == "D":
                try:
                    d = int(float(val))
                except ValueError:
                    continue
                if d in (1, 2, 3):
                    self._op(d, x, y, iv, jv)
                    x = y = iv = jv = None
                else:
                    self._select_ap(d)
            elif letter == "M":
                continue
            else:
                v = self._num(val, letter)
                if v is None:
                    continue
                if letter == "X":
                    x = v
                elif letter == "Y":
                    y = v
                elif letter == "I":
                    iv = v
                elif letter == "J":
                    jv = v

    # ------------------------------------------------------------------ #
    # 操作码
    # ------------------------------------------------------------------ #
    def _op(self, dcode, x, y, iv, jv):
        if self.notation == "I":
            tx = self.cur[0] + (0.0 if x is None else x)
            ty = self.cur[1] + (0.0 if y is None else y)
        else:
            tx = self.cur[0] if x is None else x
            ty = self.cur[1] if y is None else y
        target = (tx, ty)

        if dcode == 2:
            if self.region and self.contour:
                self._push_contour(self.contour)
                self.contour = []
            self.cur = target
            return

        if dcode == 3:
            if self.region:
                self.warn("flash_in_region", "区域内出现 D03 闪光，已忽略")
                self.cur = target
                return
            self._flash(tx, ty)
            self.cur = target
            return

        # D01 绘制
        if self.region:
            pts = self._seg(self.cur, target, iv, jv)
            if not self.contour:
                self.contour = [self.cur]
            self.contour.extend(pts[1:])
            self.cur = target
            return

        ap = self.cur_ap
        if ap is None:
            self.warn("draw_no_ap", "存在未选择光圈的绘制，已忽略")
            self.cur = target
            return
        pts = self._seg(self.cur, target, iv, jv)
        w = self._pen_width(ap)
        if w <= 0:
            self.warn("zero_ap", "0 尺寸光圈用于绘制，已按 0.01mm 细线显示")
            w = 0.01
        self._add({"t": "path", "p": pts, "w": w})
        self.img.stats["draws"] += 1
        if len(pts) > 2:
            self.img.stats["arcs"] += 1
        if w > 0 and (self._min_width is None or w < self._min_width):
            self._min_width = w
        if ap.tpl.upper() != "C":
            self._flash(tx, ty)
        self.cur = target

    def _seg(self, p0, p1, iv, jv):
        if self.interp == 1:
            return [p0, p1]
        return self._arc_pts(p0, p1, iv, jv)

    def _arc_pts(self, p0, p1, iv, jv):
        sx, sy = p0
        ex, ey = p1
        iv = 0.0 if iv is None else iv
        jv = 0.0 if jv is None else jv
        cw = self.interp == 2
        full = abs(sx - ex) < 1e-9 and abs(sy - ey) < 1e-9

        if full and (iv or jv):
            cx, cy = sx + iv, sy + jv
            r = math.hypot(iv, jv)
            if r < 1e-9:
                return [p0, p1]
            a0 = math.atan2(sy - cy, sx - cx)
            sw = -_TWO_PI if cw else _TWO_PI
            pts = geom.arc_points(cx, cy, r, a0, sw)
            pts[0] = (sx, sy)
            pts[-1] = (ex, ey)
            return pts

        if self.quadrant == "single":
            best = None
            for sxs in (1, -1):
                for sys_ in (1, -1):
                    cx, cy = sx + sxs * abs(iv), sy + sys_ * abs(jv)
                    r0 = math.hypot(sx - cx, sy - cy)
                    if r0 < 1e-9:
                        continue
                    r1 = math.hypot(ex - cx, ey - cy)
                    if abs(r0 - r1) > max(0.01, r0 * 0.02):
                        continue
                    a0 = math.atan2(sy - cy, sx - cx)
                    a1 = math.atan2(ey - cy, ex - cx)
                    sw = geom.norm_sweep(a0, a1, cw, full)
                    if full:
                        sw = -_TWO_PI if cw else _TWO_PI
                    if abs(sw) > math.pi / 2 + 1e-6:
                        continue
                    best = (cx, cy, r0, a0, sw)
                    break
                if best:
                    break
            if best is None:
                return [p0, p1]
            cx, cy, r, a0, sw = best
        else:
            cx, cy = sx + iv, sy + jv
            r = math.hypot(sx - cx, sy - cy)
            if r < 1e-9:
                return [p0, p1]
            a0 = math.atan2(sy - cy, sx - cx)
            a1 = math.atan2(ey - cy, ex - cx)
            sw = geom.norm_sweep(a0, a1, cw, full)
            if full:
                sw = -_TWO_PI if cw else _TWO_PI

        pts = geom.arc_points(cx, cy, r, a0, sw)
        pts[0] = (sx, sy)
        pts[-1] = (ex, ey)
        return pts

    def _pen_width(self, ap):
        p = ap.params
        tpl = ap.tpl.upper()
        if tpl == "C":
            return p[0] if p else 0.0
        if tpl == "R":
            return min(p[0], p[1]) if len(p) >= 2 else (p[0] if p else 0.0)
        if tpl == "O":
            return min(p[0], p[1]) if len(p) >= 2 else (p[0] if p else 0.0)
        if tpl == "P":
            dia = p[0] if p else 0.0
            n = int(p[1]) if len(p) > 1 else 3
            return dia * math.cos(math.pi / max(3, n))
        bb = None
        for neg, sh in self._macro_shapes(ap.tpl, p):
            if neg:
                continue
            bb = geom.merge_bbox(bb, self._shape_bbox(sh))
        if bb is None:
            return 0.0
        return min(bb[2] - bb[0], bb[3] - bb[1])

    @staticmethod
    def _shape_bbox(sh):
        if sh[0] == "circle":
            _, cx, cy, r = sh
            return (cx - r, cy - r, cx + r, cy + r)
        if sh[0] == "ring":
            _, cx, cy, r, _ri = sh
            return (cx - r, cy - r, cx + r, cy + r)
        return geom.bbox(sh[1])

    # ------------------------------------------------------------------ #
    # 闪光
    # ------------------------------------------------------------------ #
    def _flash(self, x, y):
        ap = self.cur_ap
        if ap is None:
            self.warn("flash_no_ap", "存在未选择光圈的闪光，已忽略")
            return
        p = ap.params
        tpl = ap.tpl.upper()
        hole = ap.hole
        self.img.stats["flashes"] += 1
        if tpl == "C":
            r = (p[0] / 2.0) if p else 0.0
            if r <= 0:
                return
            if hole > 0:
                self._add({"t": "ring", "c": [x, y], "r": r, "ri": hole / 2.0})
            else:
                self._add({"t": "circle", "c": [x, y], "r": r})
            self._track_width(r * 2)
            return
        if tpl == "R":
            w = p[0] if p else 0.0
            h = p[1] if len(p) > 1 else w
            poly = geom.rect_poly(x, y, w, h)
            if hole > 0:
                self._add({"t": "poly2", "out": poly,
                           "in": [geom.circle_poly(x, y, hole / 2.0, 32)]})
            else:
                self._add({"t": "poly", "p": poly})
            self._track_width(min(w, h))
            return
        if tpl == "O":
            w = p[0] if p else 0.0
            h = p[1] if len(p) > 1 else w
            poly = geom.obround_poly(x, y, w, h)
            if hole > 0:
                self._add({"t": "poly2", "out": poly,
                           "in": [geom.circle_poly(x, y, hole / 2.0, 32)]})
            else:
                self._add({"t": "poly", "p": poly})
            self._track_width(min(w, h))
            return
        if tpl == "P":
            dia = p[0] if p else 0.0
            n = int(p[1]) if len(p) > 1 else 3
            rot = p[2] if len(p) > 2 else 0.0
            poly = geom.regular_poly(x, y, dia, n, rot)
            if hole > 0:
                self._add({"t": "poly2", "out": poly,
                           "in": [geom.circle_poly(x, y, hole / 2.0, 32)]})
            else:
                self._add({"t": "poly", "p": poly})
            self._track_width(min(dia, dia * math.cos(math.pi / max(3, n)) * 2))
            return
        # 宏光圈：把正/负曝光原语合成一个"闪光组"。按 Gerber 规范（X2 澄清），
        # 宏内曝光关的原语只清除**该宏自身**的图像，不影响其下已绘制的图形，
        # 因此负性项在独立缓冲内合成后整体贴回，而不是直接擦除图层。
        shapes = self._macro_shapes(ap.tpl, p)
        if not shapes:
            self.warn("macro_" + ap.tpl, "光圈宏 %s 未定义或为空" % ap.tpl)
            return
        bb = None
        for neg, sh in shapes:
            bb = geom.merge_bbox(bb, self._shape_bbox(sh))
        if bb:
            self._track_width(min(bb[2] - bb[0], bb[3] - bb[1]))
        items = []
        has_dark = False
        for neg, sh in shapes:
            obj = self._shape_to_obj(sh, x, y)
            if obj is None:
                continue
            if neg:
                has_dark = True
            items.append((neg, obj))
        if has_dark:
            self._add_group(items)
        else:
            for _neg, obj in items:
                self._add(obj)

    def _track_width(self, w):
        if w and w > 0 and (self._min_width is None or w < self._min_width):
            self._min_width = w

    def _shape_to_obj(self, sh, ox, oy):
        kind = sh[0]
        if kind == "circle":
            _, cx, cy, r = sh
            if r <= 0:
                return None
            return {"t": "circle", "c": [ox + cx, oy + cy], "r": r}
        if kind == "ring":
            _, cx, cy, r, ri = sh
            if r <= 0:
                return None
            if ri <= 0:
                return {"t": "circle", "c": [ox + cx, oy + cy], "r": r}
            return {"t": "ring", "c": [ox + cx, oy + cy], "r": r, "ri": ri}
        pts = [(ox + px, oy + py) for px, py in sh[1]]
        if len(pts) < 3:
            return None
        return {"t": "poly", "p": pts}

    # ------------------------------------------------------------------ #
    # 光圈宏
    # ------------------------------------------------------------------ #
    def _macro_shapes(self, name, params):
        """返回 [(negative, shape)]，shape 见 _shape_to_obj。"""
        prims = self.macros.get(name)
        if prims is None:
            return []
        env = {"$%d" % (k + 1): float(v) for k, v in enumerate(params)}
        out = []
        for raw in prims:
            s = raw.strip()
            if not s:
                continue
            m = re.match(r"^\$(\d+)\s*=\s*(.+)$", s)
            if m:
                env["$" + m.group(1)] = self._expr(m.group(2), env)
                continue
            if s.startswith("0") and (len(s) == 1 or s[1] in " ,"):
                continue                                  # 注释原语
            parts = [p.strip() for p in s.split(",")]
            try:
                code = int(float(parts[0]))
            except ValueError:
                continue
            vals = [self._expr(p, env) for p in parts[1:]]

            if code == 1 and len(vals) >= 4:
                on, x, y, d = vals[0], vals[1], vals[2], vals[3]
                if d > 0:
                    out.append((not on, ("circle", x, y, d / 2.0)))
            elif code in (20, 21) and len(vals) >= 7:
                on, w, x1, y1, x2, y2, ang = vals[:7]
                length = math.hypot(x2 - x1, y2 - y1)
                if length > 1e-9 and w > 0:
                    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                    a = math.degrees(math.atan2(y2 - y1, x2 - x1)) + ang
                    out.append((not on, ("poly", geom.rect_poly(mx, my, length, w, a))))
            elif code == 4 and len(vals) >= 3:
                on = vals[0]
                npts = int(round(vals[1]))
                coords = vals[2:2 + npts * 2]
                rotv = vals[2 + npts * 2] if len(vals) > 2 + npts * 2 else 0.0
                if len(coords) >= 6:
                    pts = [(coords[i], coords[i + 1]) for i in range(0, len(coords) - 1, 2)]
                    if rotv:
                        pts = [geom.rot(px, py, rotv) for px, py in pts]
                    out.append((not on, ("poly", pts)))
            elif code == 5 and len(vals) >= 5:
                on, nv, x, y, dia = vals[:5]
                rotv = vals[5] if len(vals) > 5 else 0.0
                out.append((not on, ("poly", geom.regular_poly(x, y, dia, int(nv), rotv))))
            elif code == 6 and len(vals) >= 8:
                on, x, y, dia, gap, maxr, cw_, cl_ = vals[:8]
                rotv = vals[8] if len(vals) > 8 else 0.0
                r = dia / 2.0
                k = 0
                while r > 0 and k < max(1, int(maxr)):
                    ri = max(0.0, r - gap)
                    if ri > 0:
                        out.append((not on, ("ring", x, y, r, ri)))
                    else:
                        out.append((not on, ("circle", x, y, r)))
                    r -= gap
                    k += 1
                    if gap <= 1e-9:
                        break
                if cw_ > 0 and cl_ > 0:
                    out.append((not on, ("poly", geom.rect_poly(x, y, cl_, cw_, rotv))))
                    out.append((not on, ("poly", geom.rect_poly(x, y, cw_, cl_, rotv))))
            elif code == 7 and len(vals) >= 6:
                on, x, y, outer, inner, gap = vals[:6]
                rotv = vals[6] if len(vals) > 6 else 0.0
                # 原语 7 本身就是一个"带 4 个缺口的环"，无负曝光语义
                for _neg, sh in geom.thermal_shapes(x, y, outer, inner, gap, rotv):
                    out.append((not on, sh))
            elif code == 24 and len(vals) >= 5:
                on, w, h, x, y = vals[:5]
                rotv = vals[5] if len(vals) > 5 else 0.0
                if w > 0 and h > 0:
                    out.append((not on, ("poly", geom.rect_poly(x, y, w, h, rotv))))
        return out

    def _expr(self, txt, env):
        t = str(txt).strip().replace(" ", "")
        if not t:
            return 0.0
        if t in env:
            return float(env[t])
        try:
            return float(t)
        except ValueError:
            pass
        t = re.sub(r"\$(\d+)", lambda m: repr(env.get("$" + m.group(1), 0.0)), t)
        if not re.fullmatch(r"[0-9eE+\-*/().]+", t):
            return 0.0
        try:
            return float(eval(t, {"__builtins__": {}}, {}))     # noqa: S307 - 仅算术
        except Exception:
            return 0.0

    # ------------------------------------------------------------------ #
    # 区域
    # ------------------------------------------------------------------ #
    def _push_contour(self, pts):
        if len(pts) < 2:
            return
        contour = list(pts)
        if abs(contour[0][0] - contour[-1][0]) > 1e-9 or abs(contour[0][1] - contour[-1][1]) > 1e-9:
            contour.append(contour[0])
        self.img.stats["regions"] += 1
        if len(contour) >= 3:
            self._add({"t": "poly", "p": contour})   # 区域=各轮廓填充的并集

    def _emit_region(self):
        # 轮廓在 _push_contour 中逐个输出（并集语义）
        self.contour = []

    # ------------------------------------------------------------------ #
    # 输出
    # ------------------------------------------------------------------ #
    def _store(self, obj, neg):
        if obj is None:
            return
        if neg:
            obj["neg"] = 1
        self.objects.append(obj)
        bb = geom.obj_bbox(obj)
        self._bbox = geom.merge_bbox(self._bbox, bb)
        if not neg:
            self._area += geom.obj_area(obj)

    def _add(self, obj, neg=False):
        if obj is None:
            return
        obj = self._conv(obj)
        negative = bool(neg) or self.neg
        if self.ab_buf is not None:
            self.ab_buf.append((obj, negative))
            return
        if self.sr_buf is not None:
            self.sr_buf.append((obj, negative))
            return
        self._store(obj, negative)

    def _add_group(self, items):
        """items: [(neg, obj)]，obj 为文件单位绝对坐标；输出为自包含的闪光组。"""
        out = []
        bb = None
        for neg, obj in items:
            conv = self._conv(obj)
            bb = geom.merge_bbox(bb, geom.obj_bbox(conv))
            if neg:
                conv = dict(conv)
                conv["neg"] = 1
            out.append(conv)
        if not out or bb is None:
            return
        group = {"t": "group", "b": [round(bb[0], 4), round(bb[1], 4),
                                     round(bb[2], 4), round(bb[3], 4)],
                 "items": out}
        # LP 为负性时整体反相（正/负互换）
        if self.neg:
            for it in group["items"]:
                if it.get("neg"):
                    it.pop("neg", None)
                else:
                    it["neg"] = 1
        self._store(group, False)

    def _conv(self, obj):
        s = self.scale
        sf = self.sf
        ax = self.axis_swap
        rotv = self.rotation
        mx, my = self.mirror_x, self.mirror_y
        ox, oy = self.ox, self.oy

        def f(p):
            x, y = p
            if ax:
                x, y = y, x
            x *= sf
            y *= sf
            if rotv:
                x, y = geom.rot(x, y, rotv)
            if mx:
                x = -x
            if my:
                y = -y
            return [round(x * s + ox, 4), round(y * s + oy, 4)]

        t = obj.get("t")
        if t == "path":
            pts = [f(p) for p in obj["p"]]
            return {"t": "path", "w": round(obj["w"] * s * sf, 4), "p": pts}
        if t == "poly":
            return {"t": "poly", "p": [f(p) for p in obj["p"]]}
        if t == "circle":
            c = f(obj["c"])
            return {"t": "circle", "c": c, "r": round(obj["r"] * s * sf, 4)}
        if t == "ring":
            c = f(obj["c"])
            return {"t": "ring", "c": c, "r": round(obj["r"] * s * sf, 4),
                    "ri": round(obj["ri"] * s * sf, 4)}
        if t == "poly2":
            return {"t": "poly2", "out": [f(p) for p in obj["out"]],
                    "in": [[f(p) for p in h] for h in obj["in"]]}
        return obj

    def _translate(self, obj, dx, dy):
        t = obj.get("t")
        if t in ("path", "poly"):
            return {"t": t, "w": obj.get("w"), "p": [[x + dx, y + dy] for x, y in obj["p"]]} \
                if t == "path" else {"t": t, "p": [[x + dx, y + dy] for x, y in obj["p"]]}
        if t in ("circle", "ring"):
            out = dict(obj)
            out["c"] = [obj["c"][0] + dx, obj["c"][1] + dy]
            return out
        if t == "poly2":
            return {"t": "poly2",
                    "out": [[x + dx, y + dy] for x, y in obj["out"]],
                    "in": [[[x + dx, y + dy] for x, y in h] for h in obj["in"]]}
        return obj


def parse_gerber(text: str, name: str = "") -> GerberImage:
    return GerberParser(text, name).parse()
