#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
组合收益回测中枢 1.0.4 — Linux 桌面版启动器

把 APK 里的单页应用（assets/index.html）本地化为 Linux 原生体验：
  * 内置 HTTP 服务，固定端口托管页面（origin 稳定 → localStorage 数据不丢）
  * 同时实现 /ns WebDAV 中转，替代 Android 原生桥，坚果云备份/恢复照常可用
  * 优先用 Chromium 系浏览器的 --app 模式开独立窗口，否则退回默认浏览器
  * 页面心跳停止（窗口关闭）一段时间后服务自动退出

用法：
  ./pnlhub  启动（默认固定端口 17812）
  ./pnlhub --port 18000   指定端口（换端口 = 换 origin = 换一份本地数据，谨慎）
  ./pnlhub --browser none 只起服务，不自动开窗口
  ./pnlhub --version
"""

import argparse
import base64
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse, parse_qs
from urllib.request import Request, build_opener, urlopen, ProxyHandler
from urllib.error import HTTPError, URLError

APP_NAME = "pnlhub"
VERSION = "1.0.4"
DEFAULT_PORT = 17812
DAV_ROOT = "https://dav.jianguoyun.com/dav/"
UA = "pnlhub/%s (Linux)" % VERSION
IDLE_LIMIT = int(os.environ.get("PD_IDLE") or os.environ.get("STOCK_IDLE") or 180)   # 页面无心跳多久后退出（秒）
BOOT_GRACE = 240       # 从启动到第一次打开页面之间的最长等待（秒）

try:
    from embedded import HTML_B64, ICON_B64
except Exception:      # 开发态：直接读文件
    _here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(_here, "index.html"), "rb") as _f:
        HTML_B64 = base64.b64encode(_f.read()).decode()
    _icon = os.path.join(_here, "pnlhub.png")
    ICON_B64 = base64.b64encode(open(_icon, "rb").read()).decode() if os.path.exists(_icon) else ""

HTML = base64.b64decode(HTML_B64)
ICON = base64.b64decode(ICON_B64) if ICON_B64 else b""


# --------------------------------------------------------------------------
# 运行期状态
# --------------------------------------------------------------------------
class State:
    last_seen = 0.0        # 最近一次页面请求（心跳）
    served = False         # 是否已经送出过页面
    lock = threading.Lock()
    stop = threading.Event()


# --------------------------------------------------------------------------
# 同花顺投资账本：打开导出页 + 监控下载目录
#
# 页面点「同花顺导入」时走这里：
#   POST /ths/open      → 用系统默认浏览器打开投资账本，回监控目录
#   GET  /ths/pending   → 列出下载目录里刚导出的 .xlsx/.xls
#   GET  /ths/file?id=  → 吐出文件字节，交给页面用 SheetJS 解析
#   POST /ths/consume   → 标记已处理（成功的挪进「已导入同花顺」子目录）
#
# 只认「最近 15 分钟内落地」的文件，避免把历史导出误当成本次结果；
# id 由 mtime+size 生成，只在扫描结果里索引，不接受任意路径 —— 杜绝路径穿越。
# --------------------------------------------------------------------------
LEDGER_URL = "https://tzzb.10jqka.com.cn/"
THS_FRESH = 900        # 只认 15 分钟内落地的新文件（秒）
THS_LIMIT = 8          # 一次最多列 8 个候选
THS_EXT = (".xlsx", ".xls")
THS_DONE_DIR = "已导入同花顺"

_ths_index = {}        # id -> 绝对路径
_ths_done = set()      # 已消费过的 id
_ths_lock = threading.Lock()


def _ths_dirs():
    """候选下载目录：中文「下载」优先，其次 Downloads，再读 XDG 配置。"""
    home = os.path.expanduser("~")
    out = []
    for name in ("下载", "Downloads"):
        p = os.path.join(home, name)
        if os.path.isdir(p):
            out.append(p)
    try:
        cfg = os.path.join(home, ".config", "user-dirs.dirs")
        with open(cfg, "r", encoding="utf-8") as f:
            for line in f:
                m = re.match(r'\s*XDG_DOWNLOAD_DIR\s*=\s*"([^"]+)"', line)
                if m:
                    p = m.group(1).replace("$HOME", home)
                    if os.path.isdir(p) and p not in out:
                        out.append(p)
    except Exception:                                  # noqa: BLE001
        pass
    return out


def _ths_primary_dir():
    ds = _ths_dirs()
    return ds[0] if ds else os.path.expanduser("~")


def _ths_id(path):
    try:
        st = os.stat(path)
    except OSError:
        return ""
    return "%x-%x" % (int(st.st_mtime), st.st_size)


def _ths_pending():
    """列出下载目录里新鲜的导出文件（按落地时间从新到旧）。"""
    now = time.time()
    found = {}
    for d in _ths_dirs():
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            low = n.lower()
            if not low.endswith(THS_EXT) or n.startswith("~$") or n.startswith("."):
                continue
            p = os.path.join(d, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if not os.path.isfile(p) or now - st.st_mtime > THS_FRESH:
                continue
            fid = _ths_id(p)
            if not fid or fid in _ths_done:
                continue
            with _ths_lock:
                _ths_index[fid] = p
            found[fid] = {"id": fid, "name": n, "dir": d,
                          "size": st.st_size, "age": int(now - st.st_mtime)}
    items = sorted(found.values(), key=lambda x: x["age"])
    return items[:THS_LIMIT]


def _ths_path(fid):
    """id → 路径。索引里没有就重扫一次（服务重启后索引是空的）。"""
    with _ths_lock:
        p = _ths_index.get(fid)
    if p and os.path.isfile(p):
        return p
    _ths_pending()
    with _ths_lock:
        p = _ths_index.get(fid)
    return p if (p and os.path.isfile(p)) else ""


def _ths_consume(fid, ok):
    """标记已处理。导入成功时把文件挪进子目录，下次不会再被当成新导出。"""
    with _ths_lock:
        _ths_done.add(fid)
        p = _ths_index.pop(fid, "")
    if not (ok and p and os.path.isfile(p)):
        return
    dst_dir = os.path.join(os.path.dirname(p), THS_DONE_DIR)
    try:
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, os.path.basename(p))
        if os.path.exists(dst):
            stem, ext = os.path.splitext(os.path.basename(p))
            dst = os.path.join(dst_dir, "%s-%s%s" % (stem, time.strftime("%m%d%H%M%S"), ext))
        os.replace(p, dst)
    except Exception:                                  # noqa: BLE001
        pass


NO_OPEN = False        # --no-open / --browser none 时不许任何地方再弹浏览器


def _ths_open_browser(url):
    """用系统默认浏览器开新标签页（不要复用 --app 窗口，否则会顶掉本应用窗口）。"""
    if NO_OPEN:
        return False
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:                                  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# 坚果云 WebDAV 中转
# --------------------------------------------------------------------------
class DavError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status
        self.msg = msg


def _dav(method, path, user, pwd, body=None, headers=None, timeout=25):
    """向坚果云发一个 WebDAV 请求，返回 (status, headers, bytes)。"""
    url = DAV_ROOT + quote(path.lstrip("/"), safe="/")
    auth = base64.b64encode(("%s:%s" % (user, pwd)).encode("utf-8")).decode("ascii")
    h = {
        "Authorization": "Basic " + auth,
        "User-Agent": UA,
        "Accept": "*/*",
    }
    if headers:
        h.update(headers)
    req = Request(url, data=body, method=method, headers=h)
    ctx = ssl.create_default_context()
    try:
        with urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, dict(r.headers), r.read()
    except HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() or b""
    except URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.timeout):
            raise DavError(504, "连接坚果云超时，请检查网络")
        raise DavError(502, "连接坚果云失败：%s" % reason)
    except socket.timeout:
        raise DavError(504, "连接坚果云超时，请检查网络")


def _parse_propfind(xml_bytes):
    """取出 PROPFIND 响应里的文件名（仅本层）。"""
    text = xml_bytes.decode("utf-8", "replace")
    names = []
    for href in re.findall(r"<[^>]*href[^>]*>(.*?)</[^>]*href>", text, re.S | re.I):
        p = unquote(href.strip())
        name = p.rstrip("/").rsplit("/", 1)[-1]
        if name:
            names.append(name)
    return names


def _split_dir(path):
    """把「/目录/文件.json」拆成目录「/目录」。"""
    p = unquote(path or "/")
    if not p.startswith("/"):
        p = "/" + p
    if p.endswith("/"):
        return p.rstrip("/")
    return p.rsplit("/", 1)[0]


LEGACY_DIRS = {"Position Details备份": "Dividends备份"}   # 新目录名 -> APK 时期的旧目录名


def _legacy_path(path):
    """把 /Position Details备份/xxx 映射成 /Dividends备份/xxx；不是新目录名则返回 None。"""
    p = unquote(path or "/")
    if not p.startswith("/"):
        p = "/" + p
    for new, old in LEGACY_DIRS.items():
        seg = "/" + new
        if p == seg or p.startswith(seg + "/"):
            return "/" + old + p[len(seg):]
    return None


def _ensure_dir(path, user, pwd):
    """确保远程目录存在，返回创建提示或空串。"""
    folder = _split_dir(path).strip("/")
    if not folder:
        return ""
    parts = folder.split("/")
    cur = ""
    created = []
    for seg in parts:
        cur = (cur + "/" + seg) if cur else seg
        st, _, _ = _dav("PROPFIND", cur + "/", user, pwd, headers={"Depth": "0"})
        if st in (404, 409):
            st2, _, _ = _dav("MKCOL", cur + "/", user, pwd)
            if st2 in (201, 200, 204, 405):
                created.append(cur)
        elif st == 401:
            raise DavError(401, "账号或密码错误（坚果云请使用「第三方应用密码」）")
    return ("已创建远程目录 /%s" % "/".join(created)) if created else ""


def _prune(path, user, pwd, keep):
    """保留最新的 keep 份 backup_YYYYMMDD_HHMMSS.json。"""
    folder = _split_dir(path).strip("/")
    if not folder:
        return
    st, _, body = _dav("PROPFIND", folder + "/", user, pwd, headers={"Depth": "1"})
    if st >= 300:
        return
    hist = sorted(n for n in _parse_propfind(body)
                  if re.match(r"^backup_\d{8}_\d{6}\.json$", n))
    for name in hist[:max(0, len(hist) - int(keep))]:
        _dav("DELETE", "%s/%s" % (folder, name), user, pwd)


def dav_relay(method, raw_path, user, pwd, body, hdrs):
    """把浏览器的 /ns 请求翻译成坚果云 WebDAV 调用，返回 (status, dict)。"""
    path = unquote(raw_path or "/")
    if not path.startswith("/"):
        path = "/" + path
    probe = (hdrs.get("X-NS-Probe") or "").strip() == "1"
    want_list = (hdrs.get("X-NS-List") or "").strip() == "1"
    try:
        keep = int((hdrs.get("X-NS-Keep") or "0").strip() or 0)
    except ValueError:
        keep = 0
    if not user or not pwd:
        return 401, {"error": "请先在设置里填写坚果云账号与第三方应用密码"}

    # ---- 连接测试：确认账号可用 + 目录就绪 ----
    if probe:
        tip = _ensure_dir(path, user, pwd)
        st, _, _ = _dav("GET", path.lstrip("/"), user, pwd)
        if st == 401:
            return 401, {"error": "账号或密码错误（坚果云请使用「第三方应用密码」）"}
        out = {"ok": True}
        if tip:
            out["mkdir"] = tip
        return 200, out

    # ---- 列目录（历史版本列表）----
    if want_list or path.endswith("/"):
        st, _, raw = _dav("PROPFIND", path.lstrip("/") , user, pwd, headers={"Depth": "1"})
        if st in (404, 409) or (st < 300 and not _parse_propfind(raw)):
            alt = _legacy_path(path)          # 兼容 APK 时期的旧目录
            if alt:
                st2, _, raw2 = _dav("PROPFIND", alt.lstrip("/"), user, pwd, headers={"Depth": "1"})
                if st2 < 300 and _parse_propfind(raw2):
                    print("列目录：新目录为空，改用旧目录 /%s" % alt.strip("/"))
                    return 200, {"files": _parse_propfind(raw2)}
        if st in (404, 409):
            return 404, {"files": [], "error": "远程目录不存在，先备份一次即可创建"}
        if st == 401:
            return 401, {"error": "账号或密码错误（坚果云请使用「第三方应用密码」）"}
        if st >= 300:
            return st, {"error": "HTTP %s" % st}
        return 200, {"files": _parse_propfind(raw)}

    # ---- 上传 ----
    if method in ("PUT", "POST"):
        st, _, _ = _dav("PUT", path.lstrip("/"), user, pwd, body=body,
                        headers={"Content-Type": "application/json"})
        if st in (404, 409):
            _ensure_dir(path, user, pwd)
            st, _, _ = _dav("PUT", path.lstrip("/"), user, pwd, body=body,
                            headers={"Content-Type": "application/json"})
        if st == 401:
            return 401, {"error": "账号或密码错误（坚果云请使用「第三方应用密码」）"}
        if st >= 300:
            return st, {"error": "HTTP %s" % st}
        if keep > 0:
            try:
                _prune(path, user, pwd, keep)
            except DavError:
                pass
        return 200, {"ok": True}

    # ---- 下载 ----
    if method in ("GET", "HEAD"):
        st, _, raw = _dav("GET", path.lstrip("/"), user, pwd)
        if st == 404:
            # 新目录里没有：回退去旧目录找一次，让 APK 时期的备份能直接恢复
            alt = _legacy_path(path)
            if alt:
                st2, _, raw2 = _dav("GET", alt.lstrip("/"), user, pwd)
                if st2 == 200:
                    print("恢复：新目录无此文件，已从旧目录 /%s 取回" % alt.strip("/"))
                    st, raw = st2, raw2
        if st == 404:
            return 404, {"error": "云端暂无备份文件：%s" % path}
        if st == 401:
            return 401, {"error": "账号或密码错误（坚果云请使用「第三方应用密码」）"}
        if st >= 300:
            return st, {"error": "HTTP %s" % st}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return 502, {"error": "云端文件不是有效的备份 JSON"}
        if not isinstance(data, dict):
            return 502, {"error": "云端备份格式不正确"}
        return 200, data

    # ---- 其它（DELETE 等）直接转发 ----
    st, _, _ = _dav(method, path.lstrip("/"), user, pwd, body=body)
    if st == 401:
        return 401, {"error": "账号或密码错误（坚果云请使用「第三方应用密码」）"}
    if st >= 300:
        return st, {"error": "HTTP %s" % st}
    return 200, {"ok": True}


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "pnlhub/%s" % VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):     # 静音
        pass

    # ---- 小工具 ----
    def _send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _touch(self):
        with State.lock:
            State.last_seen = time.time()

    # ---- 路由 ----
    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("HEAD")

    def do_PUT(self):
        self._route("PUT")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def do_PROPFIND(self):
        self._route("PROPFIND")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET,HEAD,PUT,POST,DELETE,PROPFIND,OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _route(self, method):
        path = urlparse(self.path).path
        self._touch()

        if path in ("/", "/index.html"):
            with State.lock:
                State.served = True
            self._send(200, HTML, "text/html; charset=utf-8")
            return
        if path in ("/hold.png", "/stock.png", "/favicon.ico", "/icon.png"):
            if ICON:
                self._send(200, ICON, "image/png")
            else:
                self._send(404, {"error": "no icon"})
            return
        if path == "/ping":
            self._send(200, "pong", "text/plain; charset=utf-8")
            return
        if path == "/health":
            self._send(200, {"app": APP_NAME, "version": VERSION, "ok": True})
            return
        if path == "/ns":
            n = 0
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            body = self.rfile.read(n) if n else None
            try:
                status, data = dav_relay(
                    method,
                    self.headers.get("X-NS-Path") or "/",
                    (self.headers.get("X-NS-User") or "").strip(),
                    (self.headers.get("X-NS-Pass") or "").strip(),
                    body,
                    self.headers,
                )
            except DavError as e:
                status, data = e.status, {"error": e.msg}
            except Exception as e:                      # noqa: BLE001
                status, data = 500, {"error": "中转异常：%s" % e}
            self._send(status, data)
            return
        # ---- 同花顺投资账本 ----
        if path in ("/ths/open", "/ths/consume"):
            # 这两个是 POST：把 body 读掉，否则 keep-alive 连接上残留的
            # 请求体会被当成下一个请求的开头，出现莫名其妙的 400
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n:
                try:
                    self.rfile.read(n)
                except Exception:                  # noqa: BLE001
                    pass
        if path == "/ths/open":
            opened = _ths_open_browser(LEDGER_URL)
            self._send(200, {"ok": True, "url": LEDGER_URL, "opened": opened,
                             "dir": _ths_primary_dir()})
            return
        if path == "/ths/pending":
            self._send(200, {"files": _ths_pending()})
            return
        if path == "/ths/file":
            fid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            p = _ths_path(fid)
            if not p:
                self._send(404, {"error": "文件已不存在或已导入"})
                return
            try:
                with open(p, "rb") as f:
                    data = f.read()
            except OSError as e:
                self._send(500, {"error": str(e)})
                return
            self._send(200, data, "application/octet-stream")
            return
        if path == "/ths/consume":
            q = parse_qs(urlparse(self.path).query)
            _ths_consume((q.get("id") or [""])[0], (q.get("ok") or ["0"])[0] == "1")
            self._send(200, {"ok": True})
            return
        self._send(404, {"error": "not found"})


# --------------------------------------------------------------------------
# 窗口 & 生命周期
# --------------------------------------------------------------------------
CHROMES = [
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    "microsoft-edge", "microsoft-edge-stable", "brave-browser", "vivaldi-stable",
    "chromium-freeworld", "ungoogled-chromium",
]


def _which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def data_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def _port_file():
    return os.path.join(data_dir(), "last_port")


def _read_last_port():
    try:
        with open(_port_file(), "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:                                  # noqa: BLE001
        return 0


def _write_last_port(port):
    """记住这次实际用的端口：端口被占顺延过之后，下次启动还能找回同一个实例。"""
    try:
        with open(_port_file(), "w", encoding="utf-8") as f:
            f.write(str(port))
    except Exception:                                  # noqa: BLE001
        pass


def open_window(url, mode="auto"):
    """优先用 Chromium 系 --app 开独立窗口。"""
    if mode == "none":
        return "none"
    if mode in ("auto", "chromium"):
        for name in CHROMES:
            exe = _which(name)
            if not exe:
                continue
            profile = os.path.join(data_dir(), "profile")
            os.makedirs(profile, exist_ok=True)
            args = [exe, "--app=" + url, "--window-size=1200,860",
                    "--user-data-dir=" + profile,
                    "--no-first-run", "--no-default-browser-check",
                    "--disable-features=Translate", "--class=" + APP_NAME]
            try:
                subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return exe
            except OSError:
                continue
        if mode == "chromium":          # 指定了 chromium 系但一个都没装
            return "manual"
    try:
        webbrowser.open(url, new=1)
        return "browser"
    except Exception:                                  # noqa: BLE001
        return "manual"


def watchdog():
    """页面心跳断了就退出，避免留一个后台进程。"""
    started = time.time()
    while not State.stop.wait(5):
        now = time.time()
        with State.lock:
            seen, served = State.last_seen, State.served
        if served and now - seen > IDLE_LIMIT:
            State.stop.set()
            os._exit(0)
        if not served and now - started > BOOT_GRACE:
            State.stop.set()
            os._exit(0)


def already_running(port):
    """端口上是否已经跑着另一个 hold 实例。
    注意：必须显式绕过 http_proxy，否则系统代理会劫持对 127.0.0.1 的探测，
    导致误判「没在运行」→ 起第二个实例 → 端口顺延 → 看到空账本。"""
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:%d/health" % port, timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data.get("app") == APP_NAME
    except Exception:                                  # noqa: BLE001
        return False


def port_free(port):
    """端口能不能自己占（有 SO_REUSEADDR，避免上个实例的 TIME_WAIT 挡住重启）。"""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port(preferred):
    """返回 (端口, 是否被迫顺延)。顺延意味着换了 origin，也就是换了另一份数据。"""
    if port_free(preferred):
        return preferred, False
    p = preferred
    while p < 65000:
        p += 1
        if port_free(p):
            return p, True
    raise SystemExit("没有可用端口")


def main():
    # 输出改为行缓冲：从 .desktop 或管道启动时也能及时看到提示
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:                                  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(prog=APP_NAME, description="组合收益回测中枢 %s" % VERSION)
    ap.add_argument("--port", type=int, default=None,
                    help="默认 %d；改端口等于换一份本地数据" % DEFAULT_PORT)
    ap.add_argument("--browser", default=os.environ.get("PD_BROWSER") or os.environ.get("STOCK_BROWSER") or "auto",
                    choices=["auto", "chromium", "default", "none"])
    ap.add_argument("--no-open", action="store_true", help="只启动服务，不开窗口")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args()

    if args.version:
        print("%s %s" % (APP_NAME, VERSION))
        return

    # --no-open / --browser none：整场运行都不许弹浏览器，
    # 「同花顺导入」也不能例外（否则测试和托管场景会莫名跳出窗口）
    global NO_OPEN
    NO_OPEN = bool(args.no_open or args.browser == "none")

    # 端口来源：--port > PD_PORT > 默认值。显式指定时不去认领上次顺延的实例，
    # 否则用户想用另一个端口开一份独立数据，会被悄悄带回旧窗口。
    env_port = os.environ.get("PD_PORT") or os.environ.get("STOCK_PORT")
    explicit = args.port is not None or bool(env_port)
    port = args.port if args.port is not None else int(env_port or DEFAULT_PORT)

    if already_running(port):
        url = "http://127.0.0.1:%d/" % port
        print("组合收益回测中枢 已在运行：%s" % url)
        open_window(url, "none" if (args.no_open or args.browser == "none") else args.browser)
        return

    # 上次可能因为端口被占而顺延过：认回那个实例，别开第二个窗口（那是另一份数据）
    last = _read_last_port()
    if not explicit and last and last != port and already_running(last):
        url = "http://127.0.0.1:%d/" % last
        print("组合收益回测中枢 已在运行：%s（端口 %d 被占用，之前顺延到此）" % (url, port))
        open_window(url, "none" if (args.no_open or args.browser == "none") else args.browser)
        return

    wanted = port
    port, shifted = pick_port(wanted)
    if shifted:
        print("!" * 62)
        print("注意：端口 %d 被其它程序占用，组合收益回测中枢 已改用 %d。" % (wanted, port))
        print("      回测中枢数据是按「端口」分开存的 —— 这个端口上看到的是空账本，")
        print("      不是原来的数据。想让旧数据回来：先关掉占用 %d 的程序再启动。"
              % wanted)
        print("!" * 62)
    url = "http://127.0.0.1:%d/" % port

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        raise SystemExit("启动失败：%s" % e)
    httpd.daemon_threads = True
    _write_last_port(port)

    threading.Thread(target=watchdog, daemon=True).start()

    print("组合收益回测中枢 %s  —  %s" % (VERSION, url))
    print("窗口关闭后自动退出；数据保存在本机浏览器（origin: 127.0.0.1:%d）" % port)
    if not (args.no_open or args.browser == "none"):
        how = open_window(url, args.browser)
        if how == "manual":
            print("没找到可用浏览器，请手动访问：%s" % url)

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
