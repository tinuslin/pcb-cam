"""生成演示 / 自测用的 PCB 资料（Gerber 套件、Excellon、IPC-D-356、ODB++）。

用法：
    python tests/make_samples.py            # 生成到 tests/samples/
"""
from __future__ import annotations

import os
import shutil
import tarfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "samples")

W, H = 60.0, 40.0
RES = [(10.0, 30.0), (10.0, 22.0), (10.0, 14.0), (10.0, 6.0)]
SOIC_X1, SOIC_X2, SOIC_Y = 17.0, 23.0, 10.0
MOUNT = [(4.0, 4.0), (56.0, 4.0), (4.0, 36.0), (56.0, 36.0)]
VIAS = [(50.0, 14.0), (50.0, 26.0), (34.0, 30.0), (34.0, 6.0)]


def _c(v):
    return str(int(round(v * 1000000)))


class G:
    """极简 Gerber 生成器（3.6 格式、毫米）。"""

    def __init__(self):
        self.aps = []
        self.ops = []
        self.extra = []
        self.d = 10
        self.cur_d = None

    def ap(self, tpl, *params):
        d = self.d
        self.d += 1
        self.aps.append((d, tpl, params))
        return d

    def select(self, d):
        if self.cur_d != d:
            self.ops.append("D%d*" % d)
            self.cur_d = d

    def move(self, x, y):
        self.ops.append("X%sY%sD02*" % (_c(x), _c(y)))

    def line(self, x, y):
        self.ops.append("X%sY%sD01*" % (_c(x), _c(y)))

    def flash(self, x, y):
        self.ops.append("X%sY%sD03*" % (_c(x), _c(y)))

    def arc(self, x, y, i, j, cw=False):
        self.ops.append("G75*")
        self.ops.append("G0%d*" % (2 if cw else 3))
        self.ops.append("X%sY%sI%sJ%sD01*" % (_c(x), _c(y), _c(i), _c(j)))
        self.ops.append("G01*")

    def poly(self, pts):
        self.ops.append("G36*")
        self.move(*pts[0])
        self.ops.append("G01*")
        for p in pts[1:]:
            self.line(*p)
        self.line(*pts[0])
        self.ops.append("G37*")

    def pol(self, negative):
        self.ops.append("%LPC*%" if negative else "%LPD*%")

    def path(self, pts, d=None):
        if d:
            self.select(d)
        self.move(*pts[0])
        for p in pts[1:]:
            self.line(*p)

    def rect(self, x0, y0, x1, y1, d):
        self.path([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], d)

    def render(self, attrs=None):
        out = ["%FSLAX36Y36*%", "%MOMM*%", "%TF.Part,Single*%"]
        for a in attrs or []:
            out.append("%%TF.%s*%%" % a)
        out.extend(self.extra)
        for d, tpl, params in self.aps:
            p = "X".join(("%g" % v) for v in params)
            out.append("%%ADD%d%s,%s*%%" % (d, tpl, p) if p else "%%ADD%d%s*%%" % (d, tpl))
        out.extend(self.ops)
        out.append("M02*")
        return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# 各层
# --------------------------------------------------------------------------- #
def layer_outline():
    g = G()
    d = g.ap("C", 0.15)
    g.rect(0, 0, W, H, d)
    return g.render(["FileFunction,Profile,NP"])


def layer_copper_top():
    g = G()
    tr = g.ap("C", 0.30)
    tr2 = g.ap("C", 0.50)
    pad_r = g.ap("R", 1.60, 0.90)
    pad_soic = g.ap("R", 1.40, 0.60)
    pad_mount = g.ap("C", 2.20, 1.00)
    via = g.ap("C", 1.20, 0.60)
    g.extra.append("%AMTHERM*\n7,1,0,0,2.4,1.2,0.5,0*\n%")
    therm = g.ap("THERM")

    # 地铜箔（区域）
    g.poly([(38, 6), (56, 6), (56, 34), (38, 34)])
    # 铜箔挖空（负极性 LPC flash，Ø2mm 圆孔）
    void = g.ap("C", 2.00)
    g.pol(True)
    g.select(void)
    g.flash(45, 20)
    g.pol(False)

    # 散热焊盘（标准热焊盘画法：清除环 LPC -> 4 条辐条 LPD -> 环形焊盘宏光圈）
    clr = g.ap("C", 3.20)
    spoke_v = g.ap("R", 0.60, 3.40)
    spoke_h = g.ap("R", 3.40, 0.60)
    g.pol(True)
    g.select(clr)
    g.flash(46, 27)
    g.pol(False)
    g.select(spoke_v)
    g.flash(46, 27)
    g.select(spoke_h)
    g.flash(46, 27)
    g.select(therm)
    g.flash(46, 27)

    # 电阻焊盘
    for (cx, cy) in RES:
        g.select(pad_r)
        g.flash(cx - 2.0, cy)
        g.flash(cx + 2.0, cy)

    # SOIC-8 焊盘
    for i in range(8):
        y = SOIC_Y + (i - 3.5) * 1.27
        g.select(pad_soic)
        g.flash(SOIC_X1, y)
        g.flash(SOIC_X2, y)

    # 安装孔环
    for (x, y) in MOUNT:
        g.select(pad_mount)
        g.flash(x, y)

    # 过孔
    for (x, y) in VIAS:
        g.select(via)
        g.flash(x, y)

    # 走线
    for i, (cx, cy) in enumerate(RES):
        g.select(tr)
        g.move(cx + 2.0, cy)
        g.line(cx + 6.0, cy)
        g.line(30.0, cy)
        g.line(33.0, cy + (3.0 if cy < 20 else -3.0))
        g.line(37.0, cy + (3.0 if cy < 20 else -3.0))

    g.select(tr)
    for i in range(8):
        y = SOIC_Y + (i - 3.5) * 1.27
        g.move(SOIC_X2 + 0.7, y)
        if i % 2 == 0:
            g.line(28.0, y)
            g.line(28.0, 20.0 + i * 0.9)
            g.line(30.0, 20.0 + i * 0.9)
        else:
            g.line(29.5, y + 1.0)
            g.line(33.0, y + 1.0)

    # 一段圆弧走线（G75 + G03 多象限）
    g.select(tr2)
    g.move(33.0, 30.0)
    g.line(40.0, 30.0)
    g.arc(44.0, 26.0, 2.0, 0.0, cw=False)
    g.line(50.0, 26.0)
    return g.render(["FileFunction,Copper,L1,Top"])


def layer_copper_bottom():
    g = G()
    tr = g.ap("C", 0.40)
    pad_mount = g.ap("C", 2.20, 1.00)
    via = g.ap("C", 1.20, 0.60)
    for (x, y) in MOUNT:
        g.select(pad_mount)
        g.flash(x, y)
    for (x, y) in VIAS:
        g.select(via)
        g.flash(x, y)
    g.select(tr)
    g.path([(4, 4), (4, 36), (56, 36), (56, 4), (4, 4)])
    g.path([(10, 30), (10, 6)])
    g.path([(34, 30), (50, 26)])
    g.path([(34, 6), (50, 14)])
    return g.render(["FileFunction,Copper,L2,Bot"])


def layer_mask(top=True):
    g = G()
    pad_r = g.ap("R", 1.80, 1.10)
    pad_soic = g.ap("R", 1.60, 0.80)
    pad_mount = g.ap("C", 2.60)
    for (cx, cy) in RES:
        g.select(pad_r)
        g.flash(cx - 2.0, cy)
        g.flash(cx + 2.0, cy)
    for i in range(8):
        y = SOIC_Y + (i - 3.5) * 1.27
        g.select(pad_soic)
        g.flash(SOIC_X1, y)
        g.flash(SOIC_X2, y)
    for (x, y) in MOUNT:
        g.select(pad_mount)
        g.flash(x, y)
    name = "FileFunction,Soldermask,%s" % ("Top" if top else "Bot")
    return g.render([name])


def layer_paste():
    g = G()
    pad_r = g.ap("R", 1.40, 0.70)
    pad_soic = g.ap("R", 1.20, 0.50)
    for (cx, cy) in RES:
        g.select(pad_r)
        g.flash(cx - 2.0, cy)
        g.flash(cx + 2.0, cy)
    for i in range(8):
        y = SOIC_Y + (i - 3.5) * 1.27
        g.select(pad_soic)
        g.flash(SOIC_X1, y)
        g.flash(SOIC_X2, y)
    return g.render(["FileFunction,Solderpaste,Top"])


def layer_silk():
    g = G()
    ln = g.ap("C", 0.15)
    dot = g.ap("C", 0.35)
    for (cx, cy) in RES:
        g.rect(cx - 2.6, cy - 0.8, cx + 2.6, cy + 0.8, ln)
        g.select(dot)
        g.flash(cx - 2.0, cy - 1.4)
    g.rect(SOIC_X1 - 1.6, SOIC_Y - 5.2, SOIC_X2 + 1.6, SOIC_Y + 5.2, ln)
    g.select(dot)
    g.flash(SOIC_X1 - 2.2, SOIC_Y - 4.6)
    # 丝印标记：公司缩写用短线段表示
    g.select(ln)
    for k in range(3):
        x0 = 44 + k * 2.0
        g.path([(x0, 36.5), (x0 + 1.2, 36.5)])
        g.path([(x0, 36.5), (x0, 35.3)])
    return g.render(["FileFunction,Legend,Top"])


def drill_file():
    lines = ["M48", ";FILE_FORMAT=3:3", ";TYPE=PLATED", "METRIC,TZ", "T1C1.000", "T2C0.600", "T3C0.500", "%", "G90", "G05"]
    lines.append("T1")
    for (x, y) in MOUNT:
        lines.append("X%sY%s" % (_c6(x), _c6(y)))
    lines.append("T2")
    for (x, y) in VIAS[:2]:
        lines.append("X%sY%s" % (_c6(x), _c6(y)))
    lines.append("T3")
    for (x, y) in VIAS[2:]:
        lines.append("X%sY%s" % (_c6(x), _c6(y)))
    lines.append("M30")
    return "\n".join(lines) + "\n"


def _c6(v):
    return str(int(round(v * 1000)))


def ipc_file():
    rows = ["C  PCB-CAM demo netlist", "P  JOB DEMO-60x40", "P  UNITS CUST 0", "P  DIM N", "P  VER IPC-D-356A"]
    pts = []

    def add(net, ref, pin, x, y, hole=0, plated=True):
        pts.append((net, ref, pin, x, y, hole, plated))

    for i, (cx, cy) in enumerate(RES):
        add("GND", "R%d" % (i + 1), "2", cx + 2.0, cy)
        add("SIG%d" % (i + 1), "R%d" % (i + 1), "1", cx - 2.0, cy)
    for i in range(8):
        y = SOIC_Y + (i - 3.5) * 1.27
        add("SIG%d" % (i % 4 + 1), "U1", str(i + 1), SOIC_X1, y)
        add("VCC" if i % 2 == 0 else "GND", "U1", str(i + 1), SOIC_X2, y)
    for (x, y) in MOUNT:
        add("GND", "MH", "1", x, y, hole=1.0)
    for (x, y) in VIAS:
        add("GND", "VIA", "", x, y, hole=0.6)

    for net, ref, pin, x, y, hole, plated in pts:
        xi = int(round(x / 25.4 * 10000))
        yi = int(round(y / 25.4 * 10000))
        hole_s = ""
        if hole:
            hole_s = "D%04d%s" % (int(round(hole / 25.4 * 10000)), "P" if plated else "U")
        code = "317" if hole else "327"
        if ref == "VIA":
            rows.append("%s%-14s%-12s%sA01X%+07dY%+07d" % (code, net, "VIA", hole_s, xi, yi))
        else:
            rows.append("%s%-14s%-11s -%-3s%sA01X%+07dY%+07d" % (
                code, net, ref, pin, hole_s, xi, yi))
    rows.append("999")
    return "\n".join(rows) + "\n"


# --------------------------------------------------------------------------- #
# ODB++
# --------------------------------------------------------------------------- #
def odb_features_top():
    L = ["UNITS=MM", "ID=1", "F 60", "",
         "$0 r300", "$1 r1200", "$2 rect1600x900", "$3 rect1400x600", "$4 r2200", "$5 therm",
         "", "@0 .smd", "&0 0603", ""]
    # 地铜箔
    L += ["S 0 P 0", "OB 38 6", "OC 56 6", "OC 56 34", "OC 38 34", "OE", "SE"]
    # 走线
    for i, (cx, cy) in enumerate(RES):
        L.append("L %.3f %.3f %.3f %.3f 0 P 0" % (cx - 1.2, cy, cx + 6.0, cy))
    for i in range(8):
        y = SOIC_Y + (i - 3.5) * 1.27
        L.append("L %.3f %.3f %.3f %.3f 0 P 0" % (SOIC_X2 + 0.7, y, 29.0, y))
    L.append("L 33 30 50 26 0 P 0")
    # 焊盘
    for (cx, cy) in RES:
        L.append("P %.3f %.3f 2 P 0 0 0" % (cx - 2.0, cy))
        L.append("P %.3f %.3f 2 P 0 0 0" % (cx + 2.0, cy))
    for i in range(8):
        y = SOIC_Y + (i - 3.5) * 1.27
        L.append("P %.3f %.3f 3 P 0 0 0" % (SOIC_X1, y))
        L.append("P %.3f %.3f 3 P 0 0 0" % (SOIC_X2, y))
    for (x, y) in MOUNT:
        L.append("P %.3f %.3f 4 P 0 0 0" % (x, y))
    for (x, y) in VIAS:
        L.append("P %.3f %.3f 1 P 0 0 0" % (x, y))
    # 自定义符号（散热盘）
    L.append("P 46 20 5 P 0 0 0")
    L.append("P 46 27 5 P 0 0 0")
    return "\n".join(L) + "\n"


def odb_features_bottom():
    L = ["UNITS=MM", "ID=2", "$0 r400", "$1 r2200", "$2 r1200", ""]
    for (x, y) in MOUNT:
        L.append("P %.3f %.3f 1 P 0 0 0" % (x, y))
    for (x, y) in VIAS:
        L.append("P %.3f %.3f 2 P 0 0 0" % (x, y))
    L.append("L 4 4 4 36 0 P 0")
    L.append("L 4 36 56 36 0 P 0")
    L.append("L 56 36 56 4 0 P 0")
    L.append("L 56 4 4 4 0 P 0")
    L.append("L 34 30 50 26 0 P 0")
    L.append("L 34 6 50 14 0 P 0")
    return "\n".join(L) + "\n"


def odb_features_mask():
    L = ["UNITS=MM", "ID=3", "$0 rect1800x1100", "$1 rect1600x800", "$2 r2600", ""]
    for (cx, cy) in RES:
        L.append("P %.3f %.3f 0 P 0 0 0" % (cx - 2.0, cy))
        L.append("P %.3f %.3f 0 P 0 0 0" % (cx + 2.0, cy))
    for i in range(8):
        y = SOIC_Y + (i - 3.5) * 1.27
        L.append("P %.3f %.3f 1 P 0 0 0" % (SOIC_X1, y))
        L.append("P %.3f %.3f 1 P 0 0 0" % (SOIC_X2, y))
    for (x, y) in MOUNT:
        L.append("P %.3f %.3f 2 P 0 0 0" % (x, y))
    return "\n".join(L) + "\n"


def odb_features_silk():
    L = ["UNITS=MM", "ID=4", "$0 r150", ""]
    for (cx, cy) in RES:
        L += ["L %.3f %.3f %.3f %.3f 0 P 0" % (cx - 2.6, cy - 0.8, cx + 2.6, cy - 0.8),
              "L %.3f %.3f %.3f %.3f 0 P 0" % (cx + 2.6, cy - 0.8, cx + 2.6, cy + 0.8),
              "L %.3f %.3f %.3f %.3f 0 P 0" % (cx + 2.6, cy + 0.8, cx - 2.6, cy + 0.8),
              "L %.3f %.3f %.3f %.3f 0 P 0" % (cx - 2.6, cy + 0.8, cx - 2.6, cy - 0.8)]
    x0, y0, x1, y1 = SOIC_X1 - 1.6, SOIC_Y - 5.2, SOIC_X2 + 1.6, SOIC_Y + 5.2
    L += ["L %.3f %.3f %.3f %.3f 0 P 0" % (x0, y0, x1, y0),
          "L %.3f %.3f %.3f %.3f 0 P 0" % (x1, y0, x1, y1),
          "L %.3f %.3f %.3f %.3f 0 P 0" % (x1, y1, x0, y1),
          "L %.3f %.3f %.3f %.3f 0 P 0" % (x0, y1, x0, y0)]
    return "\n".join(L) + "\n"


def odb_features_drill():
    L = ["UNITS=MM", "ID=5", "$0 r1000", "$1 r600", "$2 r500", ""]
    for (x, y) in MOUNT:
        L.append("P %.3f %.3f 0 P 0 0 0" % (x, y))
    for (x, y) in VIAS[:2]:
        L.append("P %.3f %.3f 1 P 0 0 0" % (x, y))
    for (x, y) in VIAS[2:]:
        L.append("P %.3f %.3f 2 P 0 0 0" % (x, y))
    return "\n".join(L) + "\n"


def odb_symbol_therm():
    return "UNITS=MM\nID=9\nS 0 P 0\nOB -1.2 -1.2\nOC 1.2 -1.2\nOC 1.2 1.2\nOC -1.2 1.2\nOE\nSE\n"


def odb_matrix():
    def layer(row, name, typ, ctx="BOARD", polarity="POSITIVE"):
        return "\n".join(["LAYER", "ROW %d" % row, "CONTEXT %s" % ctx, "TYPE %s" % typ,
                          "NAME %s" % name, "POLARITY %s" % polarity, "END LAYER"])
    return "\n".join([
        "# MATRIX", layer(1, "top", "SIGNAL"), layer(2, "bottom", "SIGNAL"),
        layer(3, "soldermask_top", "SOLDER_MASK"), layer(4, "silkscreen_top", "SILK_SCREEN"),
        layer(10, "drill", "DRILL"),
    ]) + "\n"


def odb_netlist():
    L = ["H optimize N staggered N", "UNITS=MM", "$0 $NONE$", "$1 GND", "$2 VCC"]
    nets = {"GND": 1, "VCC": 2}
    for i, (cx, cy) in enumerate(RES):
        nets["SIG%d" % (i + 1)] = 3 + i
        L.append("$%d SIG%d" % (3 + i, i + 1))
    idx = 0
    for i, (cx, cy) in enumerate(RES):
        L.append("%d 0.45 %.3f %.3f T m e staggered 0 0 0" % (1, cx + 2.0, cy))
        L.append("%d 0.45 %.3f %.3f B m e staggered 0 0 0" % (3 + i, cx - 2.0, cy))
    for i in range(8):
        y = SOIC_Y + (i - 3.5) * 1.27
        L.append("%d 0.35 %.3f %.3f T m e staggered 0 0 0" % (3 + (i % 4), SOIC_X1, y))
        L.append("%d 0.35 %.3f %.3f T m e staggered 0 0 0" % (2 if i % 2 == 0 else 1, SOIC_X2, y))
    for (x, y) in MOUNT:
        L.append("1 0.5 %.3f %.3f T m e staggered 0 0 0" % (x, y))
    for (x, y) in VIAS:
        L.append("1 0.3 %.3f %.3f B m e staggered 0 0 0 v" % (x, y))
    return "\n".join(L) + "\n"


def odb_components():
    L = ["UNITS=MM", "ID=6", ""]
    for i, (cx, cy) in enumerate(RES):
        L.append("CMP %d %.3f %.3f 0.0 N R%d 0603 ;;ID=%d" % (i, cx, cy, i + 1, 10 + i))
        L.append("PRP Description 'RES 10K 1%%'")
    L.append("CMP 9 %.3f %.3f 0.0 N U1 SOIC-8 ;;ID=99" % (SOIC_X1 + 3.0, SOIC_Y))
    L.append("PRP Description 'OPAMP'")
    return "\n".join(L) + "\n"


def build_odb(root):
    files = {
        "matrix/matrix": odb_matrix(),
        "steps/pcb/layers/top/features": odb_features_top(),
        "steps/pcb/layers/bottom/features": odb_features_bottom(),
        "steps/pcb/layers/soldermask_top/features": odb_features_mask(),
        "steps/pcb/layers/silkscreen_top/features": odb_features_silk(),
        "steps/pcb/layers/drill/features": odb_features_drill(),
        "steps/pcb/symbols/therm/features": odb_symbol_therm(),
        "steps/pcb/profile": "UNITS=MM\nOB 0 0\nOC 60 0\nOC 60 40\nOC 0 40\nOE\nSE\n",
        "steps/pcb/eda/data": odb_netlist(),
        "steps/pcb/components/components": odb_components(),
        "steps/pcb/layers/top/attrlist": "UNITS=MM\n",
        "misc/info": "ODB++ demo job generated by PCB-CAM\n",
    }
    for rel, text in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)



# --------------------------------------------------------------------------- #
# 英制（inch）样例：验证 %MOIN / M72 解析与公制-英制换算
#   板框 1.90 x 1.40 in = 48.26 x 35.56 mm，原点 (0.050, 0.050) in
# --------------------------------------------------------------------------- #
def _inch_gerber_header(extra=""):
    return (
        "G04 Imperial demo (inch units)*\n"
        "%FSLAX24Y24*%\n"
        "%MOIN*%\n"
        + extra +
        "G01*\n"
        "%LPD*%\n"
    )


def inch_copper_top():
    """顶层铜箔：2 条 50mil 走线 + 3 个焊盘（矩形 60x40 mil、圆 50mil）。"""
    return (
        _inch_gerber_header("%ADD10C,0.050*%\n%ADD11R,0.060X0.040*%\n"
                            "%ADD12C,0.040*%\n%ADD13C,0.070*%\n")
        + "D10*\n"
        "X0500Y12000D02*\n"
        "X05000Y12000D01*\n"
        "X19500Y12000D01*\n"
        "X10000Y0500D02*\n"
        "X10000Y14500D01*\n"
        "D13*\n"
        "X12000Y12000D03*\n"
        "X15000Y12000D03*\n"
        "D12*\n"
        "X17000Y0800D03*\n"
        "D11*\n"
        "X19000Y0800D03*\n"
        "M02*\n"
    )


def inch_outline():
    return (
        _inch_gerber_header("%ADD10C,0.006*%\n")
        + "D10*\n"
        "X0500Y0500D02*\n"
        "X19500Y0500D01*\n"
        "X19500Y14500D01*\n"
        "X0500Y14500D01*\n"
        "X0500Y0500D01*\n"
        "M02*\n"
    )


def inch_drill():
    """英制钻孔：M72 + INCH,LZ + 2:4 格式（X012000 = 1.2000 in）。"""
    return (
        "M48\n"
        ";FILE_FORMAT=2:4\n"
        "INCH,LZ\n"
        "T1C0.040\n"
        "T2C0.032\n"
        "%\n"
        "G90\n"
        "G05\n"
        "T1\n"
        "X012000Y012000\n"
        "X015000Y012000\n"
        "T2\n"
        "X017000Y00800\n"
        "M30\n"
    )


def main():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT, exist_ok=True)

    geb = os.path.join(OUT, "demo-gerber")
    os.makedirs(geb, exist_ok=True)
    files = {
        "demo.gtl": layer_copper_top(),
        "demo.gbl": layer_copper_bottom(),
        "demo.gts": layer_mask(True),
        "demo.gbs": layer_mask(False),
        "demo.gto": layer_silk(),
        "demo.gtp": layer_paste(),
        "demo.gko": layer_outline(),
        "demo.drl": drill_file(),
        "demo.ipc": ipc_file(),
    }
    for name, text in files.items():
        with open(os.path.join(geb, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    # 英制（inch）样例：验证 %MOIN / M72 / 2:4 格式与公制换算
    inch = os.path.join(OUT, "demo-inch")
    os.makedirs(inch, exist_ok=True)
    inch_files = {
        "inch.gtl": inch_copper_top(),
        "inch.gko": inch_outline(),
        "inch.drl": inch_drill(),
    }
    for name, text in inch_files.items():
        with open(os.path.join(inch, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    # zip 包（常见交付形式）
    with zipfile.ZipFile(os.path.join(OUT, "demo-gerber.zip"), "w", zipfile.ZIP_DEFLATED) as zf:
        for name in files:
            zf.write(os.path.join(geb, name), arcname="demo/" + name)

    # ODB++ 目录 + tgz
    odb_dir = os.path.join(OUT, "demo-odb")
    build_odb(odb_dir)
    tgz = os.path.join(OUT, "demo-odb.tgz")
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(odb_dir, arcname="demo-odb")

    # XY / 贴片坐标数据（刻意留了几处问题：坐标偏差、面别错、板上没焊盘、板上独有）
    xy_csv = os.path.join(OUT, "demo-xy.csv")
    with open(xy_csv, "w", encoding="utf-8", newline="\n") as f:
        f.write(
            "Designator,Comment,Layer,Footprint,Mid X,Mid Y,Rotation\n"
            "R1,10K,TopLayer,0603,10.000,30.000,0\n"
            "R2,10K,TopLayer,0603,10.000,22.500,180\n"       # 与板上相差 0.5mm
            "R4,10K,BottomLayer,0603,10.000,6.000,90\n"      # 面别与板上不符
            "U1,OPAMP,TopLayer,SOIC-8,20.000,10.000,270\n"
            "X9,NC,TopLayer,0402,40.000,32.000,0\n"          # 板上没有这颗、也没有焊盘
        )                                                    # R3 故意缺失 -> 板上有、XY 没有

    print("样例已生成：")
    for root, _dirs, names in os.walk(OUT):
        for n in sorted(names):
            p = os.path.join(root, n)
            print("  %-58s %8.1f KB" % (os.path.relpath(p, OUT), os.path.getsize(p) / 1024.0))


if __name__ == "__main__":
    main()
