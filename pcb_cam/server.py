"""本地 HTTP 服务：静态页 + JSON API（仅标准库）。

启动：
    python -m pcb_cam.server --port 8790
    python -m pcb_cam.server --port 8790 --open
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import project, report, units, xy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "static")
SAMPLES_DIR = os.path.join(ROOT, "tests", "samples")

STATE = {"board": None, "summary": None, "path": None, "error": None, "xy": None}
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "PCBCAM/1.0"

    def log_message(self, fmt, *args):
        if "--verbose" in sys.argv:
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ------------------------------------------------------------------ #
    def do_GET(self):
        try:
            u = urlparse(self.path)
            path = u.path
            qs = parse_qs(u.query)
            if path in ("/", "/index.html"):
                return self._file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
            if path == "/api/state":
                with LOCK:
                    if STATE["summary"] is None:
                        return self._json({"loaded": False, "path": None,
                                           "samples": _sample_list()})
                    return self._json({"loaded": True, "summary": STATE["summary"],
                                       "samples": _sample_list()})
            if path == "/api/layer":
                i = int((qs.get("i") or ["0"])[0])
                with LOCK:
                    b = STATE["board"]
                if b is None:
                    return self._json({"error": "尚未加载任何资料"}, 400)
                try:
                    data = project.layer_objects(b, i)
                except IndexError as exc:
                    return self._json({"error": str(exc)}, 400)
                return self._json(data)
            if path == "/api/report":
                with LOCK:
                    b = STATE["board"]
                    st = STATE.get("xy")
                if b is None:
                    return self._json({"error": "尚未加载任何资料"}, 400)
                unit_opt = (qs.get("units") or ["both"])[0].strip().lower()
                if unit_opt not in ("both", "mm", "mil", "inch"):
                    unit_opt = "both"
                extra = {"units": unit_opt}
                if st and st.get("check"):
                    d = st["data"]
                    extra.update({"xy": st["check"], "xy_name": d.name,
                                  "xy_units": d.units, "xy_yflip": d.yflip})
                md = report.build_report(b, extra).replace("\n", "\r\n")
                download = (qs.get("download") or ["0"])[0] == "1"
                body = md.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                if download:
                    name = (b.name or "board") + "_analysis.md"
                    self.send_header("Content-Disposition",
                                     "attachment; filename*=UTF-8''%s" % _q(name))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/xy":
                return self._json(_xy_payload())
            if path == "/api/components":
                with LOCK:
                    b = STATE["board"]
                if b is None:
                    return self._json({"error": "尚未加载任何资料"}, 400)
                items = [{"ref": c.get("ref"), "x": c.get("x"), "y": c.get("y"),
                          "rot": c.get("rot"), "side": c.get("side") or "",
                          "package": c.get("package") or "", "part": c.get("part") or "",
                          "value": c.get("value") or ""}
                         for c in (b.components or [])]
                return self._json({"count": len(items), "items": items})
            if path == "/api/export/components":
                src = (qs.get("source") or ["board"])[0]
                unit_opt = (qs.get("units") or ["mm"])[0].strip().lower()
                if unit_opt not in ("mm", "mil", "inch", "both"):
                    unit_opt = "mm"
                with LOCK:
                    b = STATE["board"]
                    st = STATE.get("xy")
                if src == "xy":
                    if not st:
                        return self._json({"error": "尚未导入 XY 数据"}, 400)
                    rows = xy.rows_from_xy(st["data"], st.get("check"), unit=unit_opt)
                    name = (st["data"].name or "xy") + "_xy.csv"
                else:
                    if b is None:
                        return self._json({"error": "尚未加载任何资料"}, 400)
                    rows = xy.rows_from_board(b, unit=unit_opt)
                    name = (b.name or "board") + "_components.csv"
                if unit_opt != "mm":
                    stem, ext = os.path.splitext(name)
                    name = "%s_%s%s" % (stem, "mm-mil" if unit_opt == "both" else unit_opt, ext)
                body = xy.to_csv(rows, unit=unit_opt).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Disposition",
                                 "attachment; filename*=UTF-8''%s" % _q(name))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/browse":
                p = (qs.get("path") or [""])[0]
                show_all = (qs.get("all") or ["0"])[0] == "1"
                return self._json(_browse(p, show_all))
            if path == "/api/sample":
                name = (qs.get("name") or ["gerber"])[0]
                target = _sample_path(name)
                if not target:
                    return self._json({"error": "示例不存在：%s" % name}, 400)
                return self._json(self._load(target))
            return self._json({"error": "not found"}, 404)
        except Exception:
            traceback.print_exc()
            return self._json({"error": "服务端异常：%s" % traceback.format_exc(limit=2)}, 500)

    # ------------------------------------------------------------------ #
    def do_POST(self):
        try:
            u = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return self._json({"error": "请求体不是合法 JSON"}, 400)
            if u.path == "/api/load":
                p = (payload.get("path") or "").strip().strip('"')
                if not p:
                    return self._json({"error": "请提供资料路径"}, 400)
                p = os.path.expandvars(os.path.expanduser(p))
                if not os.path.isabs(p):
                    p = os.path.join(ROOT, p)
                if not os.path.exists(p):
                    return self._json({"error": "路径不存在：%s" % p}, 400)
                return self._json(self._load(p))
            if u.path == "/api/xy":
                p = (payload.get("path") or "").strip().strip('"')
                if not p:
                    return self._json({"error": "请提供 XY 数据文件路径"}, 400)
                p = os.path.expandvars(os.path.expanduser(p))
                if not os.path.isabs(p):
                    p = os.path.join(ROOT, p)
                if not os.path.isfile(p):
                    return self._json({"error": "文件不存在：%s" % p}, 400)
                opts = {
                    "units": payload.get("units") or "auto",
                    "yflip": payload.get("yflip", "auto"),
                    "tol": _fnum(payload.get("tol"), 0.2),
                    "radius": _fnum(payload.get("radius"), 4.0),
                }
                return self._json(_load_xy(p, opts))
            if u.path == "/api/xy/clear":
                with LOCK:
                    STATE["xy"] = None
                return self._json({"loaded": False})
            return self._json({"error": "not found"}, 404)
        except Exception:
            traceback.print_exc()
            return self._json({"error": "服务端异常：%s" % traceback.format_exc(limit=2)}, 500)

    # ------------------------------------------------------------------ #
    def _load(self, path):
        b = project.load_board(path)
        s = project.board_summary(b)
        with LOCK:
            STATE["board"] = b
            STATE["summary"] = s
            STATE["path"] = path
            st = STATE.get("xy")
        if st:                                   # 换了板子，XY 校验跟着重算
            try:
                o = st["opts"]
                xy.align(st["data"], b.bbox, units=o.get("units", "auto"),
                         yflip=o.get("yflip", "auto"), board=b)
                st["check"] = xy.check(st["data"], b, tol=o.get("tol", 0.2),
                                       radius=o.get("radius", 4.0))
            except Exception:
                traceback.print_exc()
        return s

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        if not os.path.isfile(path):
            return self._json({"error": "缺少文件：%s" % path}, 404)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _fnum(v, dflt):
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def _xy_payload():
    with LOCK:
        st = STATE.get("xy")
    if not st:
        return {"loaded": False}
    d = st["data"].to_dict(limit=20000)
    d["loaded"] = True
    d["check"] = st.get("check")
    d["opts"] = st.get("opts")
    return d


def _load_xy(path, opts):
    text = project._read_text(path)
    data = xy.parse_xy(text, os.path.basename(path), path)
    with LOCK:
        b = STATE["board"]
    if b is not None and b.bbox:
        xy.align(data, b.bbox, units=opts.get("units", "auto"),
                 yflip=opts.get("yflip", "auto"), board=b)
    chk = xy.check(data, b, tol=opts.get("tol", 0.2), radius=opts.get("radius", 4.0)) \
        if b is not None else None
    with LOCK:
        STATE["xy"] = {"data": data, "check": chk, "path": path, "opts": opts}
    return _xy_payload()


def _q(s):
    from urllib.parse import quote
    return quote(s, safe="")


def _sample_list():
    if not os.path.isdir(SAMPLES_DIR):
        return []
    return sorted(n for n in os.listdir(SAMPLES_DIR)
                  if not n.startswith(".") and n != "__pycache__")


def _sample_path(name):
    if not os.path.isdir(SAMPLES_DIR):
        return None
    cands = []
    if name in ("gerber", "demo-gerber", "g"):
        cands = [os.path.join(SAMPLES_DIR, "demo-gerber")]
    elif name in ("odb", "odbpp", "demo-odb", "o"):
        cands = [os.path.join(SAMPLES_DIR, "demo-odb"),
                 os.path.join(SAMPLES_DIR, "demo-odb.tgz")]
    elif name in ("zip",):
        cands = [os.path.join(SAMPLES_DIR, "demo-gerber.zip")]
    elif name in ("inch", "imperial", "demo-inch", "i"):
        cands = [os.path.join(SAMPLES_DIR, "demo-inch")]
    else:
        cands = [os.path.join(SAMPLES_DIR, name)]
    for c in cands:
        if os.path.exists(c):
            return c
    return None


_ARCHIVE_EXT = {"zip", "tgz", "tar", "gz", "bz2", "rar", "7z"}
_GERBER_EXT = {"gbr", "ger", "pho", "art", "gdo", "gx", "gcx", "cmp", "sol", "stc", "sts"}
# 只有这些扩展名才值得按文件名猜类别，避免 dumpstack.log / win10.exe 之类误判
_KNOWN_EXT = set(project._EXT_KIND.keys()) | _GERBER_EXT
_MAX_DIRS = 400
_MAX_FILES = 2000
_HINT_BUDGET = 60          # 最多为多少个同级子目录做「内含资料」探测


def _file_kind(name):
    """返回文件类别（copper/mask/drill/.../gerber/archive），非 PCB 资料返回空串。"""
    stem, _, ext = name.rpartition(".")
    ext = ext.lower()
    if not ext or not stem:
        return ""
    if ext in _ARCHIVE_EXT:
        return "archive"
    if ext in _GERBER_EXT:
        return "gerber"
    if re.fullmatch(r"g(p?)\d{1,2}", ext):
        return "copper"
    if ext not in _KNOWN_EXT:
        return ""
    if ext == "txt" and not re.search(r"drill|nc|hole", stem.lower()):
        return ""                     # 普通 txt 不当钻孔文件
    try:
        kind, _side = project.classify_name(name)
    except Exception:
        return ""
    return "" if kind in (None, "", "other") else kind


def _drives():
    out = []
    try:
        cands = list(os.listdrives())          # Python 3.12+
    except (AttributeError, OSError):
        import string
        cands = [c + ":\\" for c in string.ascii_uppercase if os.path.exists(c + ":\\")]
    for d in cands:
        out.append({"name": d.rstrip("\\/"), "path": d})
    return out


def _quick_places():
    home = os.path.expanduser("~")
    one = os.environ.get("OneDrive") or ""
    cands = [
        ("桌面", os.path.join(one, "Desktop") if one else "", os.path.join(home, "Desktop")),
        ("下载", os.path.join(home, "Downloads")),
        ("文档", os.path.join(one, "Documents") if one else "", os.path.join(home, "Documents")),
        ("用户目录", home),
        ("工作目录", os.path.dirname(ROOT)),
        ("演示样例", SAMPLES_DIR),
    ]
    out, seen = [], set()
    for item in cands:
        for p in item[1:]:
            if p and os.path.isdir(p):
                key = os.path.normcase(os.path.abspath(p))
                if key not in seen:
                    seen.add(key)
                    out.append({"name": item[0], "path": os.path.abspath(p)})
                break
    return out


def _crumbs(path):
    drive, tail = os.path.splitdrive(os.path.abspath(path))
    cur = (drive + os.sep) if drive else os.sep
    out = [{"name": drive.rstrip("\\/") or "/", "path": cur}]
    for part in [p for p in tail.replace("/", "\\").split("\\") if p]:
        cur = os.path.join(cur, part)
        out.append({"name": part, "path": cur})
    return out


def _dir_hint(path):
    """粗判目录内容：('odb', 0) / ('pcb', 文件数) / ('archive', 0) / ('', 0)。"""
    entries = []
    try:
        with os.scandir(path) as it:
            for i, e in enumerate(it):
                if i >= 600:
                    break
                entries.append(e)
    except OSError:
        return "", 0
    names = {e.name.lower() for e in entries}
    if "matrix" in names:
        for e in entries:
            if e.name.lower() == "steps" and e.is_dir():
                return "odb", 0
    n = 0
    for e in entries:
        try:
            if e.is_file() and _file_kind(e.name) not in ("", "archive"):
                n += 1
        except OSError:
            pass
    if n:
        return "pcb", n
    if "matrix" in names:
        return "odb", 0
    for e in entries:
        if os.path.splitext(e.name)[1].lstrip(".").lower() in _ARCHIVE_EXT:
            return "archive", 0
    return "", 0


def _browse(path, show_all=False):
    """列出目录内容，供前端「打开资料」对话框使用。"""
    raw = (path or "").strip().strip('"')
    p = os.path.abspath(os.path.expandvars(os.path.expanduser(raw))) if raw \
        else os.path.expanduser("~")
    if os.path.isfile(p):
        p = os.path.dirname(p)
    res = {"path": p, "parent": None, "crumbs": [], "drives": _drives(),
           "quick": _quick_places(), "dirs": [], "files": [],
           "hint": "", "npcb": 0, "truncated": False, "error": None}
    if not os.path.isdir(p):
        res["error"] = "目录不存在或不可访问"
        return res
    stripped = p.rstrip("\\/")
    par = os.path.dirname(stripped)
    res["parent"] = par if (par and par != p and len(par) >= 2) else None
    res["crumbs"] = _crumbs(p)
    try:
        entries = sorted(os.scandir(p), key=lambda e: e.name.lower())
    except OSError as exc:
        res["error"] = "无法读取目录：%s" % exc
        return res
    dirs, files = [], []
    for e in entries:
        name = e.name
        if name.startswith("$") or name.startswith("."):
            continue
        try:
            isdir = e.is_dir()
        except OSError:
            continue
        full = os.path.join(p, name)
        if isdir:
            if len(dirs) < _MAX_DIRS:
                dirs.append({"name": name, "path": full, "hint": "", "npcb": 0})
            else:
                res["truncated"] = True
            continue
        kind = _file_kind(name)
        if not kind and not show_all:
            continue
        kind = kind or "other"
        if len(files) >= _MAX_FILES:
            res["truncated"] = True
            break
        try:
            size = e.stat().st_size
        except OSError:
            size = 0
        files.append({"name": name, "path": full, "size": size, "kind": kind})
    if len(dirs) <= _HINT_BUDGET:
        for d in dirs:
            d["hint"], d["npcb"] = _dir_hint(d["path"])
    res["dirs"] = dirs
    res["files"] = files
    res["hint"], res["npcb"] = _dir_hint(p)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="PCB-CAM 本地查看/分析服务")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--path", default="", help="启动时自动加载的资料路径")
    args = ap.parse_args(argv)

    if args.path:
        try:
            with LOCK:
                STATE["board"] = project.load_board(args.path)
                STATE["summary"] = project.board_summary(STATE["board"])
                STATE["path"] = args.path
            print("已加载：%s" % args.path)
        except Exception as exc:
            print("预加载失败：%s" % exc)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d/" % (args.host, args.port)
    print("=" * 58)
    print(" PCB-CAM 已启动：%s" % url)
    print(" 支持：Gerber(RS-274X) / Excellon / ODB++(目录·tgz·zip) / IPC-D-356")
    print(" 关闭窗口或 Ctrl+C 退出")
    print("=" * 58)
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
