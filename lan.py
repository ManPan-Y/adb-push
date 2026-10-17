# -*- coding: utf-8 -*-
"""
lan.py
======
「网卡直连」传输引擎：不经过 adb、不依赖 USB，直接走局域网。

两部分
------
* LanServer：本机开一个 HTTP 服务，安卓版 / 另一台电脑可以直接读写共享目录。
* LanClient：连到对端（安卓版 App 或另一台 Windows）读写对方共享目录。
* sync_lan：把「差异同步 + 双校验 + 断点续传 + 并行」套到局域网通道上。

协议细节见 docs/LAN_PROTOCOL.md。只用标准库。
"""

from __future__ import annotations

import errno
import hashlib
import http.client
import json
import os
import posixpath
import shutil
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Tuple

import adb_utils as U
import transfer as T

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

DEFAULT_PORT = 47820          # HTTP 传输端口
DISCOVERY_PORT = 47821        # UDP 发现端口
MAGIC = "YHGP/1"
TOKEN_HEADER = "X-YHGP-Token"
PART_SUFFIX = ".adbp.part"    # 未完成的文件后缀（两端一致）
SHARE_DIRNAME = "shared"
IO_BLOCK = 256 * 1024
MAX_CHUNK = 16 * 1024 * 1024

_HASH_ALGOS = ("md5", "sha1", "sha256")


class LanError(RuntimeError):
    """局域网传输相关异常。"""


# --------------------------------------------------------------------------- #
# 路径工具
# --------------------------------------------------------------------------- #

def normalize_rel(rel: str) -> str:
    """把用户输入的相对路径规范成 `a/b/c`；空串代表共享根目录。"""
    rel = (rel or "").replace("\\", "/").strip()
    while rel.startswith("/"):
        rel = rel[1:]
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise LanError("路径不能包含 ..：{}".format(rel))
    return "/".join(parts)


def _canonical(path: str) -> str:
    """
    规范化绝对路径。

    Windows 上 os.path.realpath() 对**尚不存在**的路径可能返回 `\\\\?\\C:\\...`
    这种扩展长度前缀（目录还没创建时很常见），拿它做前缀比较必然失败，
    因此这里先去掉前缀，并统一用 normpath。
    """
    full = os.path.realpath(path) if os.path.exists(path) else os.path.abspath(path)
    if full.startswith("\\\\?\\"):
        full = full[4:]
    return os.path.normpath(full)


def _safe_join(root: str, rel: str) -> str:
    """把相对路径安全地拼到共享根目录下，越界直接报错。"""
    rel = normalize_rel(rel)
    root_abs = _canonical(root)
    full = _canonical(os.path.join(root_abs, rel.replace("/", os.sep))) if rel else root_abs
    probe_full = os.path.normcase(full)
    probe_root = os.path.normcase(root_abs)
    if probe_full != probe_root and not probe_full.startswith(probe_root + os.sep):
        raise LanError("路径越界：{}".format(rel))
    return full


def default_share_dir(app_dir: str) -> str:
    """默认共享目录：程序目录下的 shared/。"""
    return os.path.join(app_dir, SHARE_DIRNAME)


# --------------------------------------------------------------------------- #
# 服务端
# --------------------------------------------------------------------------- #

class _LanHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    root: str = ""
    name: str = ""
    token: str = ""
    writable: bool = True
    on_log: Optional[Callable[[str], None]] = None


class _LanHandler(BaseHTTPRequestHandler):
    """极简 HTTP/1.1 处理器，只实现 YHGP/1 需要的几个接口。"""

    protocol_version = "HTTP/1.1"
    server_version = "YHGP/1.0"
    sys_version = ""

    # 默认实现会往 stderr 刷日志，这里静默（日志统一走 on_log）
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    # ------------------------------------------------------------ 基础 --
    @property
    def st(self) -> _LanHTTPServer:
        return self.server  # type: ignore[return-value]

    def _log(self, msg: str) -> None:
        if self.st.on_log:
            self.st.on_log(msg)

    def _authorized(self) -> bool:
        if not self.st.token:
            return True
        if self.headers.get(TOKEN_HEADER, "") == self.st.token:
            return True
        self._json({"ok": False, "error": "令牌不正确"}, 401)
        return False

    def _json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _query(self) -> Tuple[str, Dict[str, str]]:
        parts = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        return parts.path, q

    # ------------------------------------------------------------ 路由 --
    def do_GET(self):  # noqa: N802
        if not self._authorized():
            return
        path, q = self._query()
        try:
            if path == "/api/info":
                self._json(self._info())
            elif path == "/api/stat":
                self._json(self._stat(q.get("path", "")))
            elif path == "/api/list":
                self._json(self._list(q.get("path", "")))
            elif path == "/api/hash":
                self._json(self._hash(q.get("path", ""), q.get("algo", "md5")))
            elif path == "/api/file":
                self._file(q.get("path", ""))
            else:
                self._json({"ok": False, "error": "未知接口"}, 404)
        except LanError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:                              # noqa: BLE001
            self._json({"ok": False, "error": repr(exc)}, 500)

    def do_PUT(self):  # noqa: N802
        if not self._authorized():
            return
        path, q = self._query()
        try:
            if path == "/api/file":
                self._put_file(q.get("path", ""), q.get("offset", "0"))
            else:
                self._json({"ok": False, "error": "未知接口"}, 404)
        except LanError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:                              # noqa: BLE001
            self._json({"ok": False, "error": repr(exc)}, 500)

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            return
        path, q = self._query()
        try:
            if path == "/api/mkdir":
                self._mkdir(q.get("path", ""))
            elif path == "/api/move":
                self._move(q.get("from", ""), q.get("to", ""))
            else:
                self._json({"ok": False, "error": "未知接口"}, 404)
        except LanError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:                              # noqa: BLE001
            self._json({"ok": False, "error": repr(exc)}, 500)

    def do_DELETE(self):  # noqa: N802
        if not self._authorized():
            return
        path, q = self._query()
        try:
            if not self.st.writable:
                raise LanError("对端为只读共享")
            full = _safe_join(self.st.root, q.get("path", ""))
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
            elif os.path.exists(full):
                os.remove(full)
            self._json({"ok": True})
        except LanError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:                              # noqa: BLE001
            self._json({"ok": False, "error": repr(exc)}, 500)

    # ------------------------------------------------------- 接口实现 --
    def _info(self) -> dict:
        root = self.st.root
        free = 0
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            pass
        return {"magic": MAGIC, "name": self.st.name, "platform": "windows",
                "version": "1.0", "root": root.replace("\\", "/"),
                "writable": self.st.writable, "sep": os.sep, "free": free}

    def _stat(self, rel: str) -> dict:
        full = _safe_join(self.st.root, rel)
        if not os.path.exists(full):
            return {"ok": True, "exists": False}
        st = os.stat(full)
        return {"ok": True, "exists": True,
                "type": "d" if os.path.isdir(full) else "f",
                "size": 0 if os.path.isdir(full) else st.st_size,
                "mtime": int(st.st_mtime)}

    def _list(self, rel: str) -> dict:
        full = _safe_join(self.st.root, rel)
        if not os.path.isdir(full):
            raise LanError("不是目录：{}".format(rel or "/"))
        entries = []
        with os.scandir(full) as it:
            for e in it:
                try:
                    is_dir = e.is_dir()
                    size = 0 if is_dir else e.stat().st_size
                    mtime = int(e.stat().st_mtime)
                except OSError:
                    continue
                entries.append({"name": e.name, "type": "d" if is_dir else "f",
                                "size": size, "mtime": mtime})
        entries.sort(key=lambda x: (x["type"] != "d", x["name"].lower()))
        return {"ok": True, "path": normalize_rel(rel), "entries": entries}

    def _hash(self, rel: str, algo: str) -> dict:
        algo = algo if algo in _HASH_ALGOS else "md5"
        full = _safe_join(self.st.root, rel)
        if not os.path.isfile(full):
            raise LanError("文件不存在：{}".format(rel))
        h = hashlib.new(algo)
        size = 0
        with open(full, "rb") as fp:
            while True:
                buf = fp.read(1024 * 1024)
                if not buf:
                    break
                size += len(buf)
                h.update(buf)
        return {"ok": True, "hash": h.hexdigest(), "size": size, "algo": algo}

    def _file(self, rel: str) -> None:
        full = _safe_join(self.st.root, rel)
        if not os.path.isfile(full):
            self._json({"ok": False, "error": "文件不存在：{}".format(rel)}, 404)
            return
        size = os.path.getsize(full)
        start = 0
        rng = (self.headers.get("Range") or "").strip()
        if rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            if spec.endswith("-"):
                try:
                    start = int(spec[:-1] or 0)
                except ValueError:
                    start = 0
            elif "-" in spec:
                a, b = spec.split("-", 1)
                try:
                    start = int(a or 0)
                except ValueError:
                    start = 0
        start = max(0, min(start, size))

        self.send_response(206 if start else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-YHGP-Size", str(size))
        self.send_header("Content-Length", str(size - start))
        if start:
            self.send_header("Content-Range", "bytes {}-{}/{}".format(start, size - 1, size))
        self.end_headers()
        sent = 0
        try:
            with open(full, "rb") as fp:
                fp.seek(start)
                while True:
                    buf = fp.read(IO_BLOCK)
                    if not buf:
                        break
                    self.wfile.write(buf)
                    sent += len(buf)
        except (BrokenPipeError, ConnectionResetError):
            self._log("下载中断：{}（已发送 {} 字节）".format(rel, sent))

    def _put_file(self, rel: str, offset_text: str) -> None:
        if not self.st.writable:
            raise LanError("对端为只读共享")
        full = _safe_join(self.st.root, rel)
        try:
            offset = int(offset_text or 0)
        except ValueError:
            raise LanError("offset 非法")
        if offset < 0:
            raise LanError("offset 非法")

        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            raise LanError("缺少 Content-Length")

        os.makedirs(os.path.dirname(full) or self.st.root, exist_ok=True)
        cur = os.path.getsize(full) if os.path.exists(full) else 0
        if cur < offset:
            raise LanError("偏移不连续（现有 {}，请求写入 {}）".format(cur, offset))

        mode = "r+b" if cur >= offset and os.path.exists(full) else "wb"
        with open(full, mode) as fp:
            if mode == "r+b":
                fp.seek(offset)
                fp.truncate()
            written = 0
            while written < length:
                block = self.rfile.read(min(IO_BLOCK, length - written))
                if not block:
                    break
                fp.write(block)
                written += len(block)
            fp.flush()
        if written != length:
            raise LanError("请求体不完整（{} / {}）".format(written, length))
        self._json({"ok": True, "size": os.path.getsize(full)})

    def _mkdir(self, rel: str) -> None:
        if not self.st.writable:
            raise LanError("对端为只读共享")
        full = _safe_join(self.st.root, rel)
        os.makedirs(full, exist_ok=True)
        self._json({"ok": True})

    def _move(self, src_rel: str, dst_rel: str) -> None:
        if not self.st.writable:
            raise LanError("对端为只读共享")
        src = _safe_join(self.st.root, src_rel)
        dst = _safe_join(self.st.root, dst_rel)
        if not os.path.exists(src):
            raise LanError("源不存在：{}".format(src_rel))
        os.makedirs(os.path.dirname(dst) or self.st.root, exist_ok=True)
        os.replace(src, dst)
        self._json({"ok": True, "size": os.path.getsize(dst) if os.path.isfile(dst) else 0})


class LanServer:
    """本机局域网共享服务（HTTP + UDP 发现）。"""

    def __init__(self, root: str, port: int = DEFAULT_PORT,
                 name: Optional[str] = None, token: str = "",
                 writable: bool = True,
                 on_log: Optional[Callable[[str], None]] = None):
        self.root = os.path.abspath(root)
        self.port = int(port)
        self.name = name or socket.gethostname()
        self.token = token or ""
        self.writable = writable
        self.on_log = on_log
        self._httpd: Optional[_LanHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._udp: Optional[socket.socket] = None
        self._udp_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ------------------------------------------------------------- 生命周期 --
    def start(self) -> str:
        if self.running:
            return self.address
        os.makedirs(self.root, exist_ok=True)
        httpd = _LanHTTPServer(("0.0.0.0", self.port), _LanHandler)
        httpd.root = self.root
        httpd.name = self.name
        httpd.token = self.token
        httpd.writable = self.writable
        httpd.on_log = self.on_log
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        kwargs={"poll_interval": 0.3},
                                        daemon=True)
        self._thread.start()
        self._start_discovery()
        self._log("网卡共享已开启：{}:{}｜目录 {}".format(
            self._primary_ip(), self.port, self.root))
        return self.address

    def stop(self) -> None:
        self._stop.set()
        if self._udp is not None:
            try:
                self._udp.close()
            except OSError:
                pass
            self._udp = None
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:                                  # noqa: BLE001
                pass
            self._httpd = None
        self._log("网卡共享已停止。")

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def address(self) -> str:
        return "{}:{}".format(self._primary_ip(), self.port)

    # ------------------------------------------------------------- 发现 --
    def _start_discovery(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", DISCOVERY_PORT))
            sock.settimeout(0.5)
        except OSError as exc:
            self._log("UDP 发现端口不可用：{}（仍可用 IP 直连）".format(exc))
            return
        self._udp = sock
        payload = {"magic": MAGIC, "name": self.name, "platform": "windows",
                   "version": "1.0", "port": self.port}

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                except OSError:
                    break
                text = data.decode("utf-8", "replace").strip()
                if not text.startswith(MAGIC):
                    continue
                try:
                    sock.sendto(json.dumps(payload, ensure_ascii=False).encode("utf-8"), addr)
                except OSError:
                    pass

        self._udp_thread = threading.Thread(target=loop, daemon=True)
        self._udp_thread.start()

    def _primary_ip(self) -> str:
        return local_ip()

    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)


# --------------------------------------------------------------------------- #
# 发现 / 本机地址
# --------------------------------------------------------------------------- #

def local_ip() -> str:
    """取本机用于局域网通信的 IPv4（不实际发包）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def local_ipv4_addresses() -> List[str]:
    """枚举本机所有 IPv4 地址（用于广播发现）。"""
    result = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            result.add(info[4][0])
    except OSError:
        pass
    result.add(local_ip())
    return [ip for ip in result if not ip.startswith("127.")]


def _broadcast_targets() -> List[str]:
    targets = {"255.255.255.255"}
    for ip in local_ipv4_addresses():
        parts = ip.split(".")
        if len(parts) == 4:
            targets.add(".".join(parts[:3] + ["255"]))
    return sorted(targets)


@dataclass
class LanPeer:
    """发现到的一台对端。"""
    host: str
    port: int
    info: dict = field(default_factory=dict)
    token: str = ""

    @property
    def name(self) -> str:
        return str(self.info.get("name") or self.host)

    @property
    def platform(self) -> str:
        return str(self.info.get("platform") or "unknown")

    @property
    def address(self) -> str:
        return "{}:{}".format(self.host, self.port)

    def __str__(self) -> str:
        return "[{}] {} ({})".format(
            "安卓" if self.platform == "android" else self.platform, self.address, self.name)


def discover(timeout: float = 1.5, port: int = DISCOVERY_PORT,
             extra_hosts: Optional[List[str]] = None,
             token: str = "") -> List[LanPeer]:
    """
    UDP 广播发现局域网内开启了共享的对端，返回 LanPeer 列表。

    extra_hosts 可传已知 IP（跨网段 / 广播被挡时兜底）。
    """
    peers: Dict[str, LanPeer] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", 0))
        sock.settimeout(0.3)
        payload = (MAGIC + " DISCOVER").encode("utf-8")
        for host in _broadcast_targets() + list(extra_hosts or []):
            try:
                sock.sendto(payload, (host, port))
            except OSError:
                continue
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                info = json.loads(data.decode("utf-8", "replace"))
            except ValueError:
                continue
            if str(info.get("magic")) != MAGIC:
                continue
            try:
                p = int(info.get("port") or DEFAULT_PORT)
            except (TypeError, ValueError):
                p = DEFAULT_PORT
            peer = LanPeer(addr[0], p, info, token)
            peers[peer.address] = peer
    finally:
        sock.close()
    return list(peers.values())


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #

class LanClient:
    """连接对端共享目录的客户端。所有 path 均为相对路径。"""

    def __init__(self, host: str, port: int = DEFAULT_PORT,
                 token: str = "", timeout: float = 20.0):
        self.host = host
        self.port = int(port)
        self.token = token or ""
        self.timeout = timeout

    def __str__(self) -> str:
        return "{}:{}".format(self.host, self.port)

    # ------------------------------------------------------------ 底层 --
    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {"Connection": "close"}
        if self.token:
            h[TOKEN_HEADER] = self.token
        if extra:
            h.update(extra)
        return h

    def _conn(self, timeout: Optional[float] = None) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port,
                                          timeout=timeout or self.timeout)

    def _url(self, path: str, query: dict) -> str:
        q = {k: v for k, v in query.items() if v not in (None, "")}
        text = urllib.parse.urlencode(q)
        return path + ("?" + text if text else "")

    def _json(self, method: str, path: str, query: dict,
              body: Optional[bytes] = None, timeout: Optional[float] = None) -> dict:
        conn = self._conn(timeout)
        try:
            headers = self._headers({"Content-Type": "application/octet-stream"})
            conn.request(method, self._url(path, query), body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                data = {"ok": False, "error": raw[:200].decode("utf-8", "replace")}
            if resp.status >= 400 or data.get("ok") is False:
                raise LanError(data.get("error") or "HTTP {}".format(resp.status))
            return data
        except (OSError, http.client.HTTPException) as exc:
            raise LanError("连接失败 {}：{}".format(self, exc)) from exc
        finally:
            conn.close()

    # ------------------------------------------------------------ 接口 --
    def info(self) -> dict:
        return self._json("GET", "/api/info", {}, timeout=6)

    def stat(self, rel: str) -> dict:
        return self._json("GET", "/api/stat", {"path": normalize_rel(rel)})

    def list_dir(self, rel: str) -> List[dict]:
        data = self._json("GET", "/api/list", {"path": normalize_rel(rel)}, timeout=60)
        return list(data.get("entries") or [])

    def hash(self, rel: str, algo: str = "md5") -> Optional[str]:
        try:
            data = self._json("GET", "/api/hash", {"path": normalize_rel(rel), "algo": algo},
                              timeout=600)
            return str(data.get("hash") or "") or None
        except LanError:
            return None

    def mkdirs(self, rel: str) -> None:
        rel = normalize_rel(rel)
        if rel:
            self._json("POST", "/api/mkdir", {"path": rel})

    def move(self, src_rel: str, dst_rel: str) -> None:
        self._json("POST", "/api/move", {"from": normalize_rel(src_rel),
                                         "to": normalize_rel(dst_rel)})

    def remove(self, rel: str) -> None:
        try:
            self._json("DELETE", "/api/file", {"path": normalize_rel(rel)}, timeout=120)
        except LanError:
            pass

    # ------------------------------------------------------------ 传输 --
    def download(self, rel: str, local: str,
                 on_bytes: Optional[Callable[[int], None]] = None,
                 resume: bool = True,
                 cancel_flag: Optional[Callable[[], bool]] = None
                 ) -> Tuple[bool, str, bool]:
        """下载单个文件到本地，支持 Range 断点续传。"""
        st = self.stat(rel)
        if not st.get("exists") or st.get("type") != "f":
            return False, "远端文件不存在：{}".format(rel), False
        size = int(st.get("size") or 0)
        local = os.path.abspath(local)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        part = local + PART_SUFFIX

        offset = os.path.getsize(part) if (resume and os.path.exists(part)) else 0
        if not resume:
            if os.path.exists(part):
                os.remove(part)
            offset = 0
        if offset > size:
            try:
                os.remove(part)
            except OSError:
                pass
            offset = 0
        resumed = offset > 0

        if offset < size:
            conn = self._conn(timeout=300)
            try:
                headers = self._headers({"Range": "bytes={}-".format(offset)})
                conn.request("GET", self._url("/api/file", {"path": normalize_rel(rel)}),
                             headers=headers)
                resp = conn.getresponse()
                if resp.status not in (200, 206):
                    raw = resp.read()[:200].decode("utf-8", "replace")
                    return False, "下载失败：HTTP {} {}".format(resp.status, raw), resumed
                if resp.status == 200:
                    offset = 0
                    mode = "wb"
                else:
                    mode = "ab"
                with open(part, mode) as out:
                    while True:
                        if cancel_flag and cancel_flag():
                            return False, "已取消", resumed
                        buf = resp.read(IO_BLOCK)
                        if not buf:
                            break
                        out.write(buf)
                        offset += len(buf)
                        if on_bytes:
                            on_bytes(len(buf))
            except (OSError, http.client.HTTPException) as exc:
                return False, "下载中断：{}".format(exc), resumed
            finally:
                conn.close()

            got = os.path.getsize(part) if os.path.exists(part) else 0
            if got != size:
                if got > size:
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                    return False, "下载长度异常（{} / {}），已丢弃断点".format(got, size), resumed
                return False, "下载不完整（{} / {}），重跑将续传".format(got, size), resumed

        try:
            os.replace(part, local)
        except OSError as exc:
            return False, "写入本地失败：{}".format(exc), resumed
        return True, "", resumed

    def upload(self, local: str, rel: str, chunk_size: int = T.DEFAULT_CHUNK,
               resume: bool = True,
               on_bytes: Optional[Callable[[int], None]] = None,
               cancel_flag: Optional[Callable[[], bool]] = None
               ) -> Tuple[bool, str, bool]:
        """
        上传单个文件：先写 `<rel>.adbp.part`，按 offset 追加，完成后改名。

        对端返回 409（偏移不连续）时自动从 0 重传该文件。
        """
        size = os.path.getsize(local)
        rel = normalize_rel(rel)
        part_rel = rel + PART_SUFFIX
        chunk_size = max(1024 * 1024, min(int(chunk_size), MAX_CHUNK))

        self.mkdirs(posixpath.dirname(rel))

        st = self.stat(part_rel)
        offset = int(st.get("size") or 0) if (resume and st.get("exists")) else 0
        if not resume or offset > size:
            self.remove(part_rel)
            offset = 0
        resumed = offset > 0

        if offset < size:
            with open(local, "rb") as fp:
                fp.seek(offset)
                while offset < size:
                    if cancel_flag and cancel_flag():
                        return False, "已取消", resumed
                    want = min(chunk_size, size - offset)
                    data = fp.read(want)
                    if len(data) != want:
                        return False, "读取本地文件失败", resumed
                    try:
                        res = self._json("PUT", "/api/file",
                                         {"path": part_rel, "offset": offset},
                                         body=data, timeout=600)
                    except LanError as exc:
                        if "偏移不连续" in str(exc) or "409" in str(exc):
                            self.remove(part_rel)
                            offset = 0
                            fp.seek(0)
                            continue
                        return False, str(exc), resumed
                    got = int(res.get("size") or 0)
                    if got != offset + want:
                        return False, "对端长度异常（{} / {}）".format(got, offset + want), resumed
                    offset += want
                    if on_bytes:
                        on_bytes(want)

        got = int(self.stat(part_rel).get("size") or 0)
        if got != size:
            return False, "上传不完整（{} / {}）".format(got, size), resumed
        self.move(part_rel, rel)
        return True, "", resumed


# --------------------------------------------------------------------------- #
# 远端枚举
# --------------------------------------------------------------------------- #

def remote_manifest(client: LanClient, root_rel: str,
                    on_log: Optional[Callable[[str], None]] = None
                    ) -> Dict[str, Tuple[int, int]]:
    """
    递归列出对端目录下所有文件：{相对 root 的路径: (size, mtime)}。

    root 是文件时键为 ""。
    """
    root_rel = normalize_rel(root_rel)
    st = client.stat(root_rel)
    if not st.get("exists"):
        raise LanError("对端路径不存在：{}".format(root_rel or "/"))
    if st.get("type") == "f":
        return {"": (int(st.get("size") or 0), int(st.get("mtime") or 0))}

    result: Dict[str, Tuple[int, int]] = {}
    stack: List[str] = [""]
    while stack:
        sub = stack.pop()
        base_rel = posixpath.join(root_rel, sub) if sub else root_rel
        try:
            entries = client.list_dir(base_rel)
        except LanError as exc:
            if on_log:
                on_log("[警告] 读取目录失败 {}：{}".format(base_rel, exc))
            continue
        for e in entries:
            name = str(e.get("name") or "")
            if not name:
                continue
            rel = posixpath.join(sub, name) if sub else name
            if e.get("type") == "d":
                stack.append(rel)
            else:
                result[rel] = (int(e.get("size") or 0), int(e.get("mtime") or 0))
    return result


# --------------------------------------------------------------------------- #
# 同步编排（与 ADB 通道同参数、同语义）
# --------------------------------------------------------------------------- #

@dataclass
class _Item:
    dest_key: str
    within: str
    src: str
    size: int
    mtime: int


def _join_local(root: str, within: str) -> str:
    return os.path.join(root, *within.split("/")) if within else root


def sync_lan(peer_host: str,
             source: str,
             target: str,
             port: int = DEFAULT_PORT,
             token: str = "",
             direction: str = "push",
             mode: str = "diff",
             verify: str = "hash",
             hash_algo: str = "md5",
             resume: bool = True,
             chunk_size: int = T.DEFAULT_CHUNK,
             jobs: int = T.DEFAULT_JOBS,
             dry_run: bool = False,
             on_log: Optional[Callable[[str], None]] = None,
             on_file: Optional[Callable[[str, str, str, str], None]] = None,
             on_progress: Optional[Callable[[int, int, int, int, int, float], None]] = None,
             cancel_flag: Optional[Callable[[], bool]] = None) -> T.SyncStats:
    """
    通过网卡与对端同步文件（语义与 transfer.sync_path 一致）。

    push：本机 source -> 对端 target（相对路径）
    pull：对端 source（相对路径） -> 本机 target
    """
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    def emit(rel: str, dest: str, action: str, detail: str) -> None:
        if on_file:
            on_file(rel, dest, action, detail)

    client = LanClient(peer_host, port, token)
    info = client.info()
    log("对端：{}｜共享目录 {}".format(info.get("name") or peer_host,
                                    info.get("root") or "-"))
    push = (direction == "push")
    jobs = max(1, int(jobs))

    # ---------- 1) 清单 ----------
    if push:
        src_root = os.path.abspath(source)
        if not os.path.exists(src_root):
            raise LanError("本地源不存在：{}".format(src_root))
        src_is_dir = os.path.isdir(src_root)
        base_name = os.path.basename(src_root.rstrip("\\/")) or src_root
        raw = T.list_local_files(src_root)
    else:
        src_root = normalize_rel(source)
        st = client.stat(src_root)
        if not st.get("exists"):
            raise LanError("对端源不存在：{}".format(src_root or "/"))
        src_is_dir = (st.get("type") == "d")
        base_name = posixpath.basename(src_root) or "root"
        raw = remote_manifest(client, src_root, on_log=log)

    if not raw:
        raise LanError("源为空或不可读：{}".format(src_root))

    items: List[_Item] = []
    for within, (size, mtime) in raw.items():
        dest_key = "{}/{}".format(base_name, within) if (src_is_dir and within) else base_name
        if push:
            src_path = _join_local(src_root, within)
        else:
            src_path = posixpath.join(src_root, within) if within else src_root
        items.append(_Item(dest_key, within, src_path, size, mtime))

    log("通道：网卡 {}｜方向：{}｜模式：{}｜校验：{}｜续传：{}｜并发：{}｜试运行：{}".format(
        client, "推送 本机→对端" if push else "拉取 对端→本机",
        mode, verify, "开" if resume else "关", jobs, "开" if dry_run else "关"))
    log("源：{}".format(src_root))
    log("目标：{}".format(target if push else os.path.abspath(target)))

    # ---------- 2) 目标清单 ----------
    if push:
        dest_root = normalize_rel(target)
        try:
            dest_manifest = remote_manifest(client, dest_root, on_log=log)
        except LanError:
            dest_manifest = {}

        def remote_get_hash(it: _Item) -> Optional[str]:
            return client.hash(posixpath.join(dest_root, it.dest_key), hash_algo)
    else:
        dest_root = os.path.abspath(target)
        dest_manifest = T.list_local_files(dest_root) if os.path.isdir(dest_root) else {}

        def remote_get_hash(it: _Item) -> Optional[str]:
            rel = posixpath.join(src_root, it.within) if it.within else src_root
            return client.hash(rel, hash_algo)

    # ---------- 3) 决策 ----------
    plan: List[_Item] = []
    skipped = 0
    compare: List[_Item] = []
    for it in items:
        dest_path = (posixpath.join(dest_root, it.dest_key) if push
                     else _join_local(dest_root, it.dest_key))
        dentry = dest_manifest.get(it.dest_key)
        if dentry is None:
            plan.append(it)
            continue
        d_size, d_mtime = dentry
        if mode == "overwrite":
            plan.append(it)
        elif mode == "skip-existing":
            skipped += 1
            emit(it.dest_key, dest_path, "skip", "对端已存在")
        elif mode == "newer":
            if it.mtime > d_mtime:
                plan.append(it)
            else:
                skipped += 1
                emit(it.dest_key, dest_path, "skip", "对端不更旧")
        elif it.size != d_size:
            plan.append(it)
        else:
            compare.append(it)

    if compare:
        log("需比对内容：{} 个文件…".format(len(compare)))
        same = 0

        def _cmp(it: _Item) -> bool:
            local_path = it.src if push else _join_local(dest_root, it.dest_key)
            local_hash = T.local_hash(local_path, hash_algo)
            remote_hash = remote_get_hash(it)
            return bool(local_hash and remote_hash and local_hash == remote_hash)

        if jobs > 1 and len(compare) > 1:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(jobs, len(compare))) as ex:
                flags = list(ex.map(_cmp, compare))
        else:
            flags = [_cmp(it) for it in compare]
        for it, is_same in zip(compare, flags):
            dest_path = (posixpath.join(dest_root, it.dest_key) if push
                         else _join_local(dest_root, it.dest_key))
            if is_same:
                same += 1
                skipped += 1
                emit(it.dest_key, dest_path, "skip", "哈希一致")
            else:
                plan.append(it)
        log("比对完成：一致 {}｜需传输 {}".format(same, len(compare) - same))

    stats = T.SyncStats(total=len(plan), skipped=skipped)
    stats.bytes_total = sum(it.size for it in plan)
    log("共 {} 个文件，需传输 {} 个，合计 {}".format(
        len(items), len(plan), U._human(stats.bytes_total)))
    if not plan:
        log("对端已是最新，无需传输。")
        log("——— " + stats.summary() + " ———")
        return stats

    # ---------- 4) 传输 ----------
    lock = threading.Lock()
    meter = T.SpeedMeter()
    state = {"done_files": 0, "done": 0, "cur": 0, "last": 0.0}

    def flush(force: bool = False) -> None:
        now = time.monotonic()
        with lock:
            if not force and now - state["last"] < 0.08:
                return
            state["last"] = now
            done = min(state["done"] + state["cur"], stats.bytes_total)
            stats.bytes_done = done
            files = state["done_files"]
        if on_progress:
            pct = int(done / stats.bytes_total * 100) if stats.bytes_total else 100
            on_progress(files, stats.total, done, stats.bytes_total, pct, meter.rate())

    def process(it: _Item) -> None:
        if cancel_flag and cancel_flag():
            return
        dest_rel = posixpath.join(dest_root, it.dest_key) if push else ""
        dest_path = (dest_rel if push else _join_local(dest_root, it.dest_key))

        if dry_run:
            log("[试运行] {} -> {}".format(it.src, dest_path))
            emit(it.dest_key, dest_path, "push", "dry-run")
            with lock:
                state["done"] += it.size
                state["done_files"] += 1
                stats.transferred += 1
            flush(True)
            return

        def on_bytes(n: int) -> None:
            meter.add(n)
            with lock:
                state["cur"] += n
            flush()

        try:
            if push:
                ok, err, resumed = client.upload(it.src, dest_rel, chunk_size,
                                                 resume, on_bytes, cancel_flag)
            else:
                ok, err, resumed = client.download(it.src, dest_path, on_bytes,
                                                   resume, cancel_flag)
        except LanError as exc:
            ok, err, resumed = False, str(exc), False
        except OSError as exc:
            ok, err, resumed = False, str(exc), False

        with lock:
            state["cur"] = 0
            state["done"] += it.size
            state["done_files"] += 1
            if ok:
                stats.transferred += 1
                if resumed:
                    stats.resumed += 1
            else:
                stats.failed += 1
                stats.failed_files.append((dest_path, err or "未知错误"))

        if not ok:
            log("[失败] {}：{}".format(it.dest_key, err))
            emit(it.dest_key, dest_path, "fail", err or "未知错误")
            flush(True)
            return

        log("[完成] {}{}".format(it.dest_key, "（续传）" if resumed else ""))
        emit(it.dest_key, dest_path, "push", "续传完成" if resumed else "成功")

        if verify != "none":
            _verify_lan(client, it, dest_path, push, verify, hash_algo,
                        dest_root, stats, lock, log, emit)
        flush(True)

    if jobs > 1 and len(plan) > 1 and not dry_run:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(jobs, len(plan))) as ex:
            list(ex.map(process, plan))
    else:
        for it in plan:
            if cancel_flag and cancel_flag():
                log("[已取消] 用户中止任务。")
                break
            process(it)

    log("——— " + stats.summary() + " ———")
    return stats


def _verify_lan(client: LanClient, it: _Item, dest_path: str, push: bool,
                verify: str, algo: str, dest_root: str,
                stats: T.SyncStats, lock: threading.Lock,
                log: Callable[[str], None],
                emit: Callable[[str, str, str, str], None]) -> None:
    """传输后校验：size 比大小；hash 比大小 + 哈希。"""
    if push:
        rel = posixpath.join(dest_root, it.dest_key)
        st = client.stat(rel)
        d_size = int(st.get("size") or 0) if st.get("exists") else None
    else:
        d_size = os.path.getsize(dest_path) if os.path.exists(dest_path) else None

    if d_size != it.size:
        detail = "大小不一致 源{} / 目标{}".format(it.size, d_size)
        with lock:
            stats.verify_failed += 1
            stats.verify_errors.append((dest_path, detail))
        log("[校验] 不一致 {}（{}）".format(it.dest_key, detail))
        emit(it.dest_key, dest_path, "verify", detail)
        return

    if verify == "hash":
        if push:
            src_hash = T.local_hash(it.src, algo)
            dst_hash = client.hash(posixpath.join(dest_root, it.dest_key), algo)
        else:
            src_hash = client.hash(it.src, algo)
            dst_hash = T.local_hash(dest_path, algo)
        if not src_hash or not dst_hash or src_hash != dst_hash:
            detail = "哈希不一致 {} / {}".format(src_hash, dst_hash)
            with lock:
                stats.verify_failed += 1
                stats.verify_errors.append((dest_path, detail))
            log("[校验] 不一致 {}（{}）".format(it.dest_key, detail))
            emit(it.dest_key, dest_path, "verify", detail)
            return

    with lock:
        stats.verified += 1
    log("[校验] 通过 {}{}".format(it.dest_key, "（大小+哈希）" if verify == "hash" else "（大小）"))
    emit(it.dest_key, dest_path, "verify", "通过")
