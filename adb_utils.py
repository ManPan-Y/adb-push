# -*- coding: utf-8 -*-
"""
adb_utils.py
============
ADB 封装层。

职责：
    * 定位 adb 可执行文件（环境变量 / PATH / 常见安装目录 / 脚本同级 platform-tools）
    * 统一执行 adb 命令（subprocess 参数列表方式，天然支持中文与空格）
    * 设备发现（adb devices / mdns）、设备信息获取、自动获取设备 IP
    * 无线调试：adb tcpip / adb pair / adb connect / 断线重连
    * 远端文件系统操作：mkdir -p、存在性判断、文件大小读取
    * 带实时进度的文件推送（解析 adb push 的百分比输出）

设计原则：
    1. 只依赖 Python 标准库，直接调用系统 adb，不引入任何第三方 adb 封装库。
    2. 所有涉及中文 / 空格的路径，均以「参数列表」传给 subprocess，绝不做字符串拼接。
    3. 远端 shell 命令中的路径，统一用 _sh_quote() 做 POSIX 单引号转义。
"""

from __future__ import annotations

import os
import posixpath
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# 常量与全局配置
# --------------------------------------------------------------------------- #

# 允许通过环境变量指定 adb 路径，优先级最高（显式参数除外）
ADB_ENV_VAR = "ADB_PATH"

# Windows 下隐藏子进程控制台窗口（避免打包成 GUI 时弹黑框）
_CREATION_FLAGS = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

# 连接类错误的特征字符串（用于触发自动重连）
_CONN_ERROR_PATTERNS = (
    "device offline",
    "device not found",
    "no devices/emulators found",
    "error: device",
    "connection reset",
    "connection refused",
    "closed",
    "protocol fault",
    "adb server",
)


class AdbError(RuntimeError):
    """adb 相关操作的通用异常。"""


class AdbNotFoundError(AdbError):
    """未找到 adb 可执行文件。"""


# --------------------------------------------------------------------------- #
# adb 可执行文件定位
# --------------------------------------------------------------------------- #

def _candidate_adb_paths(explicit: Optional[str] = None) -> List[str]:
    """返回所有可能的 adb 路径候选（按优先级排序）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cands: List[str] = []

    if explicit:
        cands.append(explicit)

    env = os.environ.get(ADB_ENV_VAR)
    if env:
        cands.append(env)

    which = shutil.which("adb")
    if which:
        cands.append(which)

    # 脚本目录附近的 platform-tools（随工具一起分发时最方便）
    for rel in ("platform-tools",
                os.path.join("..", "platform-tools"),
                os.path.join("..", "..", "platform-tools"),
                os.path.join("..", "..", "..", "platform-tools")):
        for exe in ("adb.exe", "adb"):
            cands.append(os.path.abspath(os.path.join(here, rel, exe)))

    # Windows 常见手工安装位置
    for d in (r"C:\platform-tools",
              r"C:\adb",
              r"D:\platform-tools",
              r"D:\GAME\platform-tools",
              r"C:\Users\Admin\Desktop\YHGame\platform-tools",
              os.path.expanduser(r"~\platform-tools"),
              os.path.expanduser(r"~\AppData\Local\Android\Sdk\platform-tools")):
        for exe in ("adb.exe", "adb"):
            cands.append(os.path.join(d, exe))

    return cands


def resolve_adb(explicit: Optional[str] = None) -> str:
    """定位可用的 adb 可执行文件，找不到时抛出 AdbNotFoundError。"""
    for p in _candidate_adb_paths(explicit):
        if p and os.path.isfile(p):
            return os.path.abspath(p)
        # 允许传入目录
        if p and os.path.isdir(p):
            for exe in ("adb.exe", "adb"):
                cand = os.path.join(p, exe)
                if os.path.isfile(cand):
                    return os.path.abspath(cand)
    raise AdbNotFoundError(
        "未找到 adb 可执行文件。请下载 Android Platform Tools 并任选一种方式配置：\n"
        "  1) 将 adb 所在目录加入系统 PATH 环境变量；\n"
        "  2) 设置环境变量 ADB_PATH 指向 adb.exe；\n"
        "  3) 把 platform-tools 文件夹放到本工具同级的父目录下。\n"
        "下载地址：https://developer.android.com/tools/releases/platform-tools"
    )


# --------------------------------------------------------------------------- #
# 命令执行
# --------------------------------------------------------------------------- #

def _decode(raw: bytes) -> str:
    """把 adb 输出解码为文本（adb 默认 UTF-8，容错处理）。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", errors="replace")


def _build_cmd(args: Sequence[object], serial: Optional[str], adb_path: Optional[str]) -> List[str]:
    """组装完整命令列表：adb [-s serial] args..."""
    cmd = [resolve_adb(adb_path)]
    if serial:
        cmd += ["-s", str(serial)]
    cmd += [str(a) for a in args]
    return cmd


def run_adb(args: Sequence[object],
            serial: Optional[str] = None,
            adb_path: Optional[str] = None,
            timeout: Optional[float] = None,
            check: bool = False) -> Tuple[int, str, str]:
    """
    同步执行一条 adb 命令。

    参数：
        args      : adb 子命令及其参数，例如 ["shell", "ls", "/sdcard"]
        serial    : 目标设备序列号（多设备时必填）
        adb_path  : 指定 adb 可执行文件（默认自动定位）
        timeout   : 超时秒数
        check     : 为 True 时非 0 返回码抛 AdbError

    返回：
        (returncode, stdout, stderr)  —— stdout/stderr 已解码为字符串
    """
    cmd = _build_cmd(args, serial, adb_path)
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=_CREATION_FLAGS,
        )
    except FileNotFoundError as exc:  # adb 丢失
        raise AdbNotFoundError(f"无法执行 adb：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"adb 命令超时（{timeout}s）：{' '.join(cmd)}") from exc

    out = _decode(proc.stdout)
    err = _decode(proc.stderr)
    if check and proc.returncode != 0:
        raise AdbError(f"adb 执行失败({proc.returncode})：{' '.join(args)}\n{err.strip()}")
    return proc.returncode, out, err


def start_server(adb_path: Optional[str] = None) -> None:
    """确保 adb server 已启动。"""
    run_adb(["start-server"], adb_path=adb_path, timeout=30)


def kill_server(adb_path: Optional[str] = None) -> None:
    """关闭 adb server（用于彻底重置连接状态）。"""
    run_adb(["kill-server"], adb_path=adb_path, timeout=30)


def _sh_quote(path: str) -> str:
    """
    对远端 shell 路径做 POSIX 单引号转义，保证中文 / 空格 / 特殊字符安全。
    例如：/sdcard/我 的'文件 -> '/sdcard/我 的'\''文件'
    """
    return "'" + str(path).replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- #
# 设备发现
# --------------------------------------------------------------------------- #

@dataclass
class Device:
    """adb devices -l 的解析结果。"""
    serial: str
    state: str                       # device / offline / unauthorized
    model: str = ""
    product: str = ""
    transport_id: str = ""

    @property
    def is_ready(self) -> bool:
        return self.state == "device"

    @property
    def is_wireless(self) -> bool:
        """序列号形如 ip:port 即视为无线连接。"""
        return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d+$", self.serial))

    def __str__(self) -> str:
        tag = "无线" if self.is_wireless else "USB "
        name = self.model or self.product or "未知型号"
        return f"[{tag}] {self.serial}  ({name}, {self.state})"


def list_devices(adb_path: Optional[str] = None) -> List[Device]:
    """执行 adb devices -l 并解析设备列表。"""
    code, out, err = run_adb(["devices", "-l"], adb_path=adb_path, timeout=30)
    devices: List[Device] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        dev = Device(serial=parts[0], state=parts[1])
        for item in parts[2:]:
            if ":" in item:
                k, _, v = item.partition(":")
                if k == "model":
                    dev.model = v
                elif k == "product":
                    dev.product = v
                elif k == "transport_id":
                    dev.transport_id = v
        devices.append(dev)
    return devices


def pick_device(serial: Optional[str] = None,
                adb_path: Optional[str] = None,
                prefer_wireless: bool = True) -> Device:
    """
    选择一台可用设备。

    serial 指定时精确匹配；未指定时：
        * 只有一台可用设备 -> 直接返回
        * 多台设备 -> 优先返回无线设备，否则抛错要求用 --serial 指定
    """
    devices = list_devices(adb_path=adb_path)
    ready = [d for d in devices if d.is_ready]

    if serial:
        for d in devices:
            if d.serial == serial:
                if not d.is_ready:
                    # 尝试自动重连一次
                    if d.state == "offline" and d.is_wireless:
                        connect(d.serial, adb_path=adb_path)
                        wait_for_device(d.serial, adb_path=adb_path, timeout=15)
                        return pick_device(serial, adb_path, prefer_wireless)
                    raise AdbError(f"设备 {serial} 当前状态为 {d.state}，无法使用（请检查授权/网络）。")
                return d
        raise AdbError(f"未找到序列号为 {serial} 的设备，请先连接。")

    if not ready:
        raise AdbError(
            "未检测到可用设备。请确认：\n"
            "  * USB 已连接且已在手机上允许「USB 调试」授权；或\n"
            "  * 已执行 adb connect <IP>:<端口> 建立无线连接。"
        )
    if len(ready) == 1:
        return ready[0]

    if prefer_wireless:
        wireless = [d for d in ready if d.is_wireless]
        if len(wireless) == 1:
            return wireless[0]
    raise AdbError("检测到多台设备，请使用 --serial 指定目标：\n  " +
                   "\n  ".join(str(d) for d in ready))


def get_device_info(serial: str, adb_path: Optional[str] = None) -> dict:
    """获取设备型号与 Android 版本等基础信息。"""
    props = {
        "model": "ro.product.model",
        "brand": "ro.product.brand",
        "android": "ro.build.version.release",
        "sdk": "ro.build.version.sdk",
        "abi": "ro.product.cpu.abi",
    }
    info = {"serial": serial}
    for key, prop in props.items():
        try:
            _, out, _ = run_adb(["shell", "getprop", prop], serial=serial,
                                adb_path=adb_path, timeout=20)
            info[key] = out.strip()
        except AdbError:
            info[key] = ""
    return info


def get_device_ip(serial: str,
                  adb_path: Optional[str] = None,
                  interface: str = "wlan0") -> Optional[str]:
    """
    自动获取设备局域网 IP。
    依次尝试：ip route（默认路由 src）/ ip -f inet addr show wlan0 / ifconfig wlan0。
    失败返回 None，由调用方提示用户手动输入。
    """
    # 方案 1：默认路由里的 src 地址
    try:
        _, out, _ = run_adb(["shell", "ip", "route"], serial=serial,
                            adb_path=adb_path, timeout=20)
        for line in out.splitlines():
            if "default" in line:
                m = re.search(r"src\s+(\d{1,3}(?:\.\d{1,3}){3})", line)
                if m and not m.group(1).startswith("127."):
                    return m.group(1)
    except AdbError:
        pass

    # 方案 2：ip addr
    try:
        _, out, _ = run_adb(["shell", "ip", "-f", "inet", "addr", "show", interface],
                            serial=serial, adb_path=adb_path, timeout=20)
        m = re.search(r"inet\s+(\d{1,3}(?:\.\d{1,3}){3})", out)
        if m:
            return m.group(1)
    except AdbError:
        pass

    # 方案 3：ifconfig（部分老设备 / toybox 差异）
    try:
        _, out, _ = run_adb(["shell", "ifconfig", interface], serial=serial,
                            adb_path=adb_path, timeout=20)
        m = re.search(r"inet addr:(\d{1,3}(?:\.\d{1,3}){3})", out) or \
            re.search(r"inet\s+(\d{1,3}(?:\.\d{1,3}){3})", out)
        if m:
            return m.group(1)
    except AdbError:
        pass

    return None


def discover_mdns(adb_path: Optional[str] = None) -> List[str]:
    """
    通过 adb mdns services 发现同一局域网内开启「无线调试」的设备。
    返回形如 ["192.168.1.5:37000", ...] 的地址列表。
    """
    results: List[str] = []
    try:
        _, out, _ = run_adb(["mdns", "services"], adb_path=adb_path, timeout=15)
    except AdbError:
        return results
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("List of discovered"):
            continue
        # 典型输出： adb-XXXX-YYYY._adb-tls-connect._tcp.   192.168.1.5:37000
        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}:\d+)", line)
        if m:
            results.append(m.group(1))
    return results


# --------------------------------------------------------------------------- #
# 无线调试：tcpip / pair / connect / 重连
# --------------------------------------------------------------------------- #

def enable_tcpip(serial: str, port: int = 5555, adb_path: Optional[str] = None) -> None:
    """USB 已连接时，把设备 adbd 切换到 TCP 模式（传统无线方式）。"""
    code, out, err = run_adb(["tcpip", str(port)], serial=serial, adb_path=adb_path, timeout=30)
    if code != 0:
        raise AdbError(f"adb tcpip 失败：{err.strip() or out.strip()}")
    time.sleep(2)  # 给 adbd 重启留时间


def pair(address: str, code: str, adb_path: Optional[str] = None) -> Tuple[bool, str]:
    """
    Android 11+ 无线调试配对：adb pair <IP>:<端口> <配对码>。
    返回 (是否成功, 输出文本)。配对端口与连接端口不同，需从手机界面读取。
    """
    code_out, out, err = run_adb(["pair", address, str(code)], adb_path=adb_path, timeout=60)
    text = (out + err).strip()
    ok = code_out == 0 and "successfully" in text.lower()
    return ok, text or "配对无输出"


def connect(address: str, adb_path: Optional[str] = None) -> Tuple[bool, str]:
    """adb connect <IP>:<端口>，返回 (是否成功, 输出文本)。"""
    code_out, out, err = run_adb(["connect", address], adb_path=adb_path, timeout=30)
    text = (out + err).strip()
    ok = code_out == 0 and ("connected" in text.lower()) and ("cannot" not in text.lower())
    return ok, text


def disconnect(address: Optional[str] = None, adb_path: Optional[str] = None) -> None:
    """断开无线连接（address 为空则断开全部）。"""
    args = ["disconnect"] + ([address] if address else [])
    run_adb(args, adb_path=adb_path, timeout=20)


def wait_for_device(serial: str,
                    timeout: float = 30,
                    interval: float = 1.0,
                    adb_path: Optional[str] = None) -> bool:
    """轮询等待设备进入 device 状态（连接后必须等它 ready 再推送）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for d in list_devices(adb_path=adb_path):
            if d.serial == serial and d.is_ready:
                return True
        time.sleep(interval)
    return False


def is_connection_error(text: str) -> bool:
    """判断输出文本是否属于「连接类错误」，用于触发自动重连。"""
    low = (text or "").lower()
    return any(p in low for p in _CONN_ERROR_PATTERNS)


def auto_connect(serial: Optional[str] = None,
                 adb_path: Optional[str] = None,
                 prefer_wireless: bool = True,
                 on_log: Optional[Callable[[str], None]] = None,
                 timeout: float = 20.0) -> Optional[str]:
    """
    自动发现并连接设备，返回可用设备的序列号；失败返回 None。

    流程：
        1) 确保 adb server 已启动；
        2) 若已有 ready 设备（或指定 serial 已 ready），直接返回，不重复连接；
        3) 否则用 mDNS 发现局域网内的无线调试设备，逐个 adb connect；
        4) 第一个成功进入 device 状态的地址即作为结果返回。

    说明：
        * 优先复用 `_adb._tcp`（传统 tcpip 5555）与
          `_adb-tls-connect._tcp`（Android 11+ 无线调试）两类服务；
        * 全程只做「发现 + 连接」，不会开启 tcpip（那需要 USB 在线）。
    """
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    try:
        start_server(adb_path=adb_path)
    except AdbError as exc:
        log(f"启动 adb server 失败：{exc}")

    # 1) 已有可用设备则直接复用
    try:
        devices = list_devices(adb_path=adb_path)
    except AdbError as exc:
        log(f"读取设备列表失败：{exc}")
        devices = []

    ready = [d for d in devices if d.is_ready]
    if serial:
        for d in ready:
            if d.serial == serial:
                log(f"设备已在线：{serial}")
                return d.serial
    elif ready:
        try:
            dev = pick_device(None, adb_path=adb_path, prefer_wireless=prefer_wireless)
            log(f"检测到已在线设备：{dev.serial}")
            return dev.serial
        except AdbError:
            pass

    # 2) mDNS 发现 + 逐个连接
    candidates = discover_mdns(adb_path=adb_path)
    if not candidates:
        log("mDNS 未发现设备：请确认手机「无线调试」已开启，且与电脑处于同一 WiFi。")
        return None

    log("mDNS 发现候选地址：" + "、".join(candidates))
    for addr in candidates:
        ok, text = connect(addr, adb_path=adb_path)
        log(f"adb connect {addr} -> {text}")
        if ok and wait_for_device(addr, timeout=timeout, adb_path=adb_path):
            log(f"已连接：{addr}")
            return addr
    log("已发现设备但均连接失败（请检查手机授权弹窗与防火墙）。")
    return None


# --------------------------------------------------------------------------- #
# 远端文件系统操作
# --------------------------------------------------------------------------- #

def remote_mkdirs(serial: str, path: str, adb_path: Optional[str] = None) -> None:
    """
    在设备上创建目录（等价 mkdir -p）。
    优先使用 mkdir -p；老设备 toybox 不支持时逐级创建兜底。
    """
    path = path.rstrip("/") or "/"
    if path in ("/", "/sdcard", "/storage/emulated/0"):
        return
    code, out, err = run_adb(["shell", "mkdir", "-p", _sh_quote(path)],
                             serial=serial, adb_path=adb_path, timeout=20)
    if code == 0 and not err.strip():
        return

    # 兜底：逐级创建
    parts = [p for p in path.split("/") if p]
    cur = ""
    for p in parts:
        cur += "/" + p
        run_adb(["shell", "mkdir", _sh_quote(cur)], serial=serial,
                adb_path=adb_path, timeout=20)


def remote_exists(serial: str, path: str, adb_path: Optional[str] = None) -> bool:
    """判断远端路径是否存在（文件或目录）。"""
    cmd = "[ -e {p} ] && echo __YES__ || echo __NO__".format(p=_sh_quote(path))
    _, out, _ = run_adb(["shell", cmd], serial=serial, adb_path=adb_path, timeout=20)
    return "__YES__" in out


def remote_size(serial: str, path: str, adb_path: Optional[str] = None) -> Optional[int]:
    """
    读取远端文件字节数。优先 stat -c %s，失败则用 wc -c 兜底。
    读取失败返回 None。
    """
    # 注意：此处使用 str.format，%s 是 stat 的格式串，不能再写成 %%
    cmd = "stat -c %s {p} 2>/dev/null || wc -c < {p}".format(p=_sh_quote(path))
    _, out, _ = run_adb(["shell", cmd], serial=serial, adb_path=adb_path, timeout=20)
    m = re.search(r"(\d+)", out)
    return int(m.group(1)) if m else None


def remote_free_bytes(serial: str, path: str = "/sdcard", adb_path: Optional[str] = None) -> Optional[int]:
    """读取远端可用空间（KB 单位换算为字节），用于推送前容量检查。"""
    _, out, _ = run_adb(["shell", "df", "-k", _sh_quote(path)],
                        serial=serial, adb_path=adb_path, timeout=20)
    for line in out.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 4:
            try:
                return int(cols[3]) * 1024
            except ValueError:
                continue
    return None


def remote_is_dir(serial: str, path: str, adb_path: Optional[str] = None) -> bool:
    """判断远端路径是否为目录。"""
    cmd = "[ -d {p} ] && echo __YES__ || echo __NO__".format(p=_sh_quote(path))
    _, out, _ = run_adb(["shell", cmd], serial=serial, adb_path=adb_path, timeout=20)
    return "__YES__" in out


def remote_remove(serial: str, path: str, adb_path: Optional[str] = None) -> None:
    """强制删除远端文件或目录（静默，失败不抛错）。"""
    run_adb(["shell", "rm", "-rf", _sh_quote(path)],
            serial=serial, adb_path=adb_path, timeout=60)


def remote_list_dir(serial: str,
                    path: str,
                    adb_path: Optional[str] = None) -> List[Tuple[str, str]]:
    """
    列出远端目录的直接子项，返回 [(名称, "d"|"f"), ...]，目录排在前面。

    用于设备的图形化目录浏览器。toybox 的 find 不支持 `-printf %y`，
    所以拆成两次调用分别取目录与文件。

    `-H` 用于跟随命令行上的符号链接：`/sdcard` 本身是符号链接，
    不加 -H 时 find 不会进入它，会被误判成空目录。
    """
    path = path.rstrip("/") or "/"
    cmd = ("find -H {p} -mindepth 1 -maxdepth 1 -type d -printf 'd\\t%f\\n' 2>/dev/null; "
           "find -H {p} -mindepth 1 -maxdepth 1 -type f -printf 'f\\t%f\\n' 2>/dev/null"
           ).format(p=_sh_quote(path))
    _code, out, _err = run_adb(["shell", cmd], serial=serial, adb_path=adb_path, timeout=120)
    entries: List[Tuple[str, str]] = []
    for line in out.splitlines():
        kind, sep, name = line.partition("\t")
        if sep and kind in ("d", "f") and name:
            entries.append((name, kind))
    return entries


def push_file_fast(local: str,
                   remote: str,
                   serial: Optional[str] = None,
                   adb_path: Optional[str] = None,
                   timeout: Optional[float] = None) -> Tuple[int, str]:
    """
    推送单个文件，但**不解析进度输出**（一次性读完 stdout）。

    供分片/批量内部调用：相比带进度解析的 push_file()，省掉逐字节读的开销，
    在「大量小文件」场景下差别显著。
    """
    cmd = _build_cmd(["push", local, remote], serial, adb_path)
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            creationflags=_CREATION_FLAGS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdbError("推送超时：{}".format(local)) from exc
    return proc.returncode, _decode(proc.stdout)


def run_shell_stream(cmd: str,
                     on_line: Callable[[str], None],
                     serial: Optional[str] = None,
                     adb_path: Optional[str] = None) -> int:
    """
    执行一条远端 shell 命令，每读到一行就回调 on_line(line)，返回退出码。

    用于「一条命令要跑很久但会产生大量可解析输出」的场景（例如整目录
    批量 md5sum），让调用方能把进度实时反馈给用户，而不是干等。
    """
    full = _build_cmd(["shell", cmd], serial, adb_path)
    proc = subprocess.Popen(full, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            creationflags=_CREATION_FLAGS)
    try:
        if proc.stdout:
            for raw in proc.stdout:
                on_line(_decode(raw).rstrip("\r\n"))
    finally:
        if proc.stdout:
            proc.stdout.close()
    return proc.wait()


def open_exec_out(args: Sequence[object],
                  serial: Optional[str] = None,
                  adb_path: Optional[str] = None) -> subprocess.Popen:
    """
    以流式 Popen 执行 `adb exec-out ...`，stdout 为原始二进制（无 CRLF 转换）。

    这是唯一可靠的「设备 → 电脑」二进制通道，用于带精确偏移的断点续传下载。
    调用方负责读取 proc.stdout 并 wait()/kill()。
    """
    cmd = [resolve_adb(adb_path)]
    if serial:
        cmd += ["-s", str(serial)]
    cmd += ["exec-out"] + [str(a) for a in args]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=_CREATION_FLAGS)


def exec_out_bytes(args: Sequence[object],
                   serial: Optional[str] = None,
                   adb_path: Optional[str] = None,
                   timeout: Optional[float] = None) -> bytes:
    """一次性执行 exec-out 并返回原始字节（小数据量场景，如取哈希）。"""
    proc = open_exec_out(args, serial=serial, adb_path=adb_path)
    try:
        out, _err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise AdbError("exec-out 超时")
    return out or b""


# --------------------------------------------------------------------------- #
# 文件推送（带实时进度）
# --------------------------------------------------------------------------- #

@dataclass
class PushStats:
    """一次推送任务的统计结果。"""
    total: int = 0                                   # 待处理文件总数
    pushed: int = 0                                  # 成功推送数
    skipped: int = 0                                 # 跳过数（skip-existing / 未更新）
    failed: int = 0                                  # 失败数
    bytes_total: int = 0                             # 待推送总字节
    bytes_done: int = 0                              # 已推送字节
    failed_files: List[Tuple[str, str]] = field(default_factory=list)  # (远端路径, 原因)

    def summary(self) -> str:
        return (f"总计 {self.total} 个文件｜成功 {self.pushed}｜跳过 {self.skipped}｜"
                f"失败 {self.failed}｜传输 {_human(self.bytes_done)} / {_human(self.bytes_total)}")


def _human(n: int) -> str:
    """字节数转可读文本。"""
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f}{u}" if u != "B" else f"{int(f)}B"
        f /= 1024
    return f"{n}B"


def human_duration(seconds: Optional[float]) -> str:
    """秒数转可读时长，用于「预计剩余时间」。未知时返回 "--"。"""
    if not seconds or seconds <= 0:
        return "--"
    s = int(seconds)
    if s < 60:
        return "{}s".format(s)
    if s < 3600:
        return "{}m{:02d}s".format(s // 60, s % 60)
    return "{}h{:02d}m".format(s // 3600, (s % 3600) // 60)


def iter_local_files(source: str) -> Iterator[Tuple[str, str]]:
    """
    遍历本地 source，产出 (本地绝对路径, 相对路径)。

    * source 为文件   -> 相对路径 = 文件名
    * source 为文件夹 -> 相对路径 = 顶层文件夹名/子路径（与 adb push 语义一致）

    相对路径统一使用正斜杠，便于拼接到安卓目标目录。
    """
    source = os.path.abspath(source)
    if os.path.isfile(source):
        yield source, os.path.basename(source)
    elif os.path.isdir(source):
        base = os.path.dirname(source)
        for root, _dirs, files in os.walk(source):
            for name in files:
                local = os.path.join(root, name)
                rel = os.path.relpath(local, base).replace(os.sep, "/")
                yield local, rel
    else:
        raise AdbError(f"本地路径不存在：{source}")


def push_file(local: str,
              remote: str,
              serial: Optional[str] = None,
              adb_path: Optional[str] = None,
              on_progress: Optional[Callable[[int, str], None]] = None,
              timeout: Optional[float] = None) -> Tuple[int, str]:
    """
    推送单个文件，并实时解析 adb push 的进度百分比。

    参数：
        local       : 本地文件绝对路径
        remote      : 远端完整文件路径
        on_progress : 回调 (百分比 0-100, 原始行文本)
        timeout     : 整体超时（大文件建议给足）

    返回：
        (returncode, 合并输出文本)
    """
    cmd = _build_cmd(["push", local, remote], serial, adb_path)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=_CREATION_FLAGS,
    )
    buf = bytearray()
    output_lines: List[str] = []
    deadline = (time.time() + timeout) if timeout else None

    # 读线程负责阻塞式读取；主线程只做「带超时的取件」。
    # 若直接在 proc.stdout 上阻塞读（read(1) / for line），adb 一旦静默卡死
    # 就永远读不到数据，超时判定本身也执行不到，timeout 形同虚设。
    data_q: "queue.Queue[Optional[bytes]]" = queue.Queue()

    def _reader() -> None:
        try:
            while True:
                data = proc.stdout.read(4096) if proc.stdout else b""
                if not data:
                    break
                data_q.put(data)
        except (OSError, ValueError):
            pass
        finally:
            data_q.put(None)          # 结束哨兵

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    def _emit_line(line: str) -> None:
        if line.strip():
            output_lines.append(line)
        m = re.search(r"\[\s*(\d+)%\]", line)
        if m and on_progress:
            on_progress(int(m.group(1)), line)

    try:
        while True:
            try:
                data = data_q.get(timeout=0.5)
            except queue.Empty:
                if deadline and time.time() > deadline:
                    proc.kill()
                    raise AdbError(f"推送超时：{local}")
                continue
            if data is None:
                break
            for byte in data:
                if byte in (0x0D, 0x0A):      # \r 或 \n 都算一行结束
                    _emit_line(buf.decode("utf-8", errors="replace"))
                    buf.clear()
                else:
                    buf.append(byte)
        if buf:
            _emit_line(buf.decode("utf-8", errors="replace"))
        proc.wait()
    finally:
        if proc.stdout:
            proc.stdout.close()
    return proc.returncode, "\n".join(output_lines)


# --------------------------------------------------------------------------- #
# 高层推送编排
# --------------------------------------------------------------------------- #

# 事件回调类型说明（供 GUI / CLI 复用）：
#   on_log(text)                          —— 普通日志
#   on_file(rel, remote, action, detail)  —— 单文件状态：action in {push, skip, fail, verify}
#   on_progress(done_files, total_files, done_bytes, total_bytes, cur_pct)
LogFn = Callable[[str], None]
FileFn = Callable[[str, str, str, str], None]
ProgressFn = Callable[[int, int, int, int, int], None]


def push_path(source: str,
              target: str,
              serial: Optional[str] = None,
              adb_path: Optional[str] = None,
              mode: str = "overwrite",
              verify: bool = False,
              dry_run: bool = False,
              newer_only: bool = False,
              on_log: Optional[LogFn] = None,
              on_file: Optional[FileFn] = None,
              on_progress: Optional[ProgressFn] = None,
              reconnect: Optional[Callable[[], Optional[str]]] = None,
              retries: int = 2,
              cancel_flag: Optional[Callable[[], bool]] = None) -> PushStats:
    """
    把一个本地文件 / 文件夹推送到设备目标目录。

    参数：
        source      : 本地文件或文件夹
        target      : 设备目标目录（如 /sdcard/Download/目标目录）
        mode        : "overwrite" 直接覆盖；"skip-existing" 远端已存在则跳过
        verify      : 推送后比对本地/远端文件大小
        dry_run     : 仅打印计划，不实际传输
        newer_only  : 只推送「本地修改时间比远端新」的文件（以远端存在为准）
        on_log      : 日志回调
        on_file     : 单文件回调
        on_progress : 汇总进度回调
        reconnect   : 断线时调用的重连函数，返回新的 serial（或 None）
        retries     : 单文件失败重试次数（针对连接类错误）
        cancel_flag : 返回 True 时中止任务

    返回：
        PushStats 统计对象

    .. deprecated::
        本函数为旧版实现（仅单向推送、无续传、校验只比大小）。
        新代码请使用 ``transfer.sync_path()``：支持双向、断点续传、
        大小+哈希双校验与哈希差异同步。保留此函数仅为向后兼容。
    """
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    # 1) 展开文件清单
    items = list(iter_local_files(source))
    if not items:
        raise AdbError(f"源目录为空：{source}")

    stats = PushStats(total=len(items))
    for local, _rel in items:
        try:
            stats.bytes_total += os.path.getsize(local)
        except OSError:
            pass

    log(f"源：{os.path.abspath(source)}")
    log(f"目标：{target}")
    log(f"模式：{mode}｜校验：{'开' if verify else '关'}｜"
        f"仅更新较新：{'开' if newer_only else '关'}｜试运行：{'开' if dry_run else '关'}")
    log(f"共 {stats.total} 个文件，合计 {_human(stats.bytes_total)}")

    # 2) 创建目标根目录（dry-run 跳过）
    if not dry_run:
        try:
            remote_mkdirs(serial, target, adb_path=adb_path)
        except AdbError as exc:
            log(f"[警告] 创建目标目录失败：{exc}")

    for idx, (local, rel) in enumerate(items, start=1):
        if cancel_flag and cancel_flag():
            log("[已取消] 用户中止任务。")
            break

        remote = posixpath.join(target.rstrip("/"), rel)
        try:
            size = os.path.getsize(local)
        except OSError:
            size = 0

        # 3) skip-existing / newer-only 判断
        if not dry_run and (mode == "skip-existing" or newer_only or verify):
            try:
                if mode == "skip-existing" and remote_exists(serial, remote, adb_path=adb_path):
                    stats.skipped += 1
                    stats.bytes_done += size
                    log(f"[跳过] 已存在：{remote}")
                    if on_file:
                        on_file(rel, remote, "skip", "远端已存在")
                    if on_progress:
                        on_progress(idx, stats.total, stats.bytes_done, stats.bytes_total, 100)
                    continue
                if newer_only and remote_exists(serial, remote, adb_path=adb_path):
                    r_size = remote_size(serial, remote, adb_path=adb_path)
                    if r_size is not None and r_size == size:
                        stats.skipped += 1
                        stats.bytes_done += size
                        log(f"[跳过] 大小一致：{remote}")
                        if on_file:
                            on_file(rel, remote, "skip", "大小一致")
                        if on_progress:
                            on_progress(idx, stats.total, stats.bytes_done, stats.bytes_total, 100)
                        continue
            except AdbError as exc:
                log(f"[警告] 远端检查失败（继续推送）：{exc}")

        # 4) dry-run：只打印
        if dry_run:
            stats.pushed += 1
            stats.bytes_done += size
            log(f"[试运行] 将推送 {local}  ->  {remote}  ({_human(size)})")
            if on_file:
                on_file(rel, remote, "push", "dry-run")
            if on_progress:
                on_progress(idx, stats.total, stats.bytes_done, stats.bytes_total, 100)
            continue

        # 5) 确保远端子目录存在
        sub_dir = posixpath.dirname(remote)
        try:
            remote_mkdirs(serial, sub_dir, adb_path=adb_path)
        except AdbError as exc:
            stats.failed += 1
            stats.failed_files.append((remote, f"创建目录失败：{exc}"))
            log(f"[失败] {remote}：{exc}")
            if on_file:
                on_file(rel, remote, "fail", str(exc))
            continue

        # 6) 执行推送（含连接类错误自动重连重试）
        base_done = stats.bytes_done
        attempt = 0
        ok = False
        last_err = ""
        cur_serial = serial
        while attempt <= retries:
            attempt += 1

            def _pct(pct: int, _line: str, _base=base_done, _size=size) -> None:
                if on_progress:
                    on_progress(idx, stats.total,
                                _base + int(_size * pct / 100),
                                stats.bytes_total, pct)

            try:
                code, text = push_file(local, remote, serial=cur_serial,
                                       adb_path=adb_path, on_progress=_pct)
            except AdbError as exc:
                code, text = 1, str(exc)

            if code == 0 and "error" not in text.lower():
                ok = True
                break

            last_err = text.strip()
            log(f"[重试 {attempt}/{retries + 1}] {rel} 失败：{last_err[:200]}")

            # 连接类错误 -> 尝试重连
            if is_connection_error(text) and reconnect and attempt <= retries:
                log("检测到连接异常，尝试自动重连…")
                new_serial = reconnect()
                if new_serial:
                    cur_serial = new_serial
                    log(f"重连成功：{new_serial}")
                else:
                    log("自动重连失败，继续重试…")
            elif attempt > retries:
                break
            time.sleep(1.5)

        if not ok:
            stats.failed += 1
            stats.failed_files.append((remote, last_err[:300] or "未知错误"))
            log(f"[失败] {remote}：{last_err[:200] or '未知错误'}")
            if on_file:
                on_file(rel, remote, "fail", last_err[:200] or "未知错误")
            # 失败也要推进字节计数，避免进度倒退
            stats.bytes_done = base_done + size
            if on_progress:
                on_progress(idx, stats.total, stats.bytes_done, stats.bytes_total, 100)
            continue

        stats.pushed += 1
        stats.bytes_done = base_done + size
        log(f"[完成] {rel}  ({_human(size)})")
        if on_file:
            on_file(rel, remote, "push", "成功")

        # 7) 校验
        if verify:
            r_size = remote_size(cur_serial, remote, adb_path=adb_path)
            if r_size is None:
                log(f"[校验] 无法读取远端大小：{remote}")
                if on_file:
                    on_file(rel, remote, "verify", "无法读取远端大小")
            elif r_size == size:
                log(f"[校验] 通过 {rel} ({size}B)")
                if on_file:
                    on_file(rel, remote, "verify", "通过")
            else:
                log(f"[校验] 不一致 {rel} 本地 {size}B / 远端 {r_size}B")
                if on_file:
                    on_file(rel, remote, "verify", f"不一致 本地{size}/远端{r_size}")

        if on_progress:
            on_progress(idx, stats.total, stats.bytes_done, stats.bytes_total, 100)

    log("——— " + stats.summary() + " ———")
    return stats

