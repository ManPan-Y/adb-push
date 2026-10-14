# -*- coding: utf-8 -*-
"""
adb_push.py
===========
命令行入口：通过 ADB 无线调试，把本地任意文件 / 文件夹推送到安卓设备指定目录。

典型用法
--------
    # 1) 传统方式：先用 USB 打开 TCP 模式，然后无线连接
    python adb_push.py --to-wireless --port 5555

    # 2) Android 11+ 配对（交互输入配对码）
    python adb_push.py --pair

    # 3) 仅连接设备
    python adb_push.py --connect --address 192.168.1.5:37000

    # 4) 推送文件夹（存在则跳过，并做大小校验）
    python adb_push.py --source "D:\\我的文件" --target "/sdcard/Download/目标目录" \
        --mode skip-existing --verify

    # 5) 只演练不传输
    python adb_push.py --source "D:\\我的文件" --target "/sdcard/Download" --dry-run

    # 6) 使用配置文件批量推送
    python adb_push.py --config config.json --batch

    # 7) 自动发现局域网内开启无线调试的设备
    python adb_push.py --discover

    # 8) 自动发现并连接设备（推荐，无需手动填 IP）
    python adb_push.py --auto
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from typing import List, Optional

# 允许从任意目录运行本脚本
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adb_utils as U  # noqa: E402
import transfer as T  # noqa: E402

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


# --------------------------------------------------------------------------- #
# 日志与进度显示
# --------------------------------------------------------------------------- #

class ConsoleReporter:
    """把传输事件渲染到控制台：带进度条 + 实时速率 + 彩色前缀。"""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self._last_line_len = 0
        self._lock = threading.Lock()      # 多线程并发传输时保证输出不串行错乱

    def log(self, msg: str) -> None:
        if self.quiet:
            return
        with self._lock:
            self._clear_line()
            print(msg, flush=True)

    def _clear_line(self) -> None:
        if self._last_line_len:
            print("\r" + " " * self._last_line_len + "\r", end="", flush=True)
            self._last_line_len = 0

    def on_progress(self, done_files: int, total_files: int,
                    done_bytes: int, total_bytes: int, cur_pct: int,
                    speed: float = 0.0) -> None:
        if self.quiet:
            return
        pct = int(done_bytes / total_bytes * 100) if total_bytes else cur_pct
        pct = max(0, min(100, pct))
        bar_len = 30
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "─" * (bar_len - filled)
        # 预计剩余时长：剩余字节 / 当前速率（速率已是滑动窗口平滑值）
        remain = max(0, total_bytes - done_bytes)
        eta = (remain / speed) if (speed > 0 and remain > 0) else None
        line = (f"\r[{bar}] {pct:3d}%  文件 {done_files}/{total_files}  "
                f"{U._human(done_bytes)}/{U._human(total_bytes)}  "
                f"{U._human(int(speed))}/s  剩余 {U.human_duration(eta)}")
        with self._lock:
            pad = max(0, self._last_line_len - len(line))
            print(line + " " * pad, end="", flush=True)
            self._last_line_len = len(line)

    def finish_line(self) -> None:
        with self._lock:
            self._clear_line()


# --------------------------------------------------------------------------- #
# 各子流程
# --------------------------------------------------------------------------- #

def print_devices(adb_path: Optional[str]) -> None:
    """打印当前 ADB 设备列表（含型号与状态）。"""
    try:
        devices = U.list_devices(adb_path=adb_path)
    except U.AdbError as exc:
        print(f"[错误] {exc}")
        return
    if not devices:
        print("未检测到任何设备。")
        return
    print("当前设备：")
    for d in devices:
        print("  " + str(d))


def do_discover(adb_path: Optional[str]) -> None:
    """通过 mDNS 发现同网段开启无线调试的设备。"""
    print("正在通过 mDNS 发现局域网内的无线调试设备…")
    found = U.discover_mdns(adb_path=adb_path)
    if not found:
        print("未发现设备。请确认手机「无线调试」已开启，且与电脑处于同一局域网。")
        return
    print("发现以下地址（连接端口）：")
    for addr in found:
        print("  " + addr)
    print("\n提示：使用 `--connect --address <地址>` 连接。")


def do_pair(args, adb_path: Optional[str]) -> Optional[str]:
    """Android 11+ 无线调试配对流程。返回连接地址（IP:连接端口）。"""
    print("=== Android 11+ 无线调试配对 ===")
    print("请在手机上：设置 -> 开发者选项 -> 无线调试 -> 使用配对码配对设备")
    print("（手机上会显示「IP 地址与端口」和 6 位配对码）")
    address = args.address or input("配对 IP:端口（例如 192.168.1.5:37000）：").strip()
    code = args.pair_code or input("配对码（6 位数字）：").strip()
    ok, text = U.pair(address, code, adb_path=adb_path)
    print(("[配对成功] " if ok else "[配对失败] ") + text)
    if not ok:
        print("常见原因：配对码已过期（重新打开配对弹窗）、端口填错、手机与电脑不在同一局域网。")
        return None

    # 配对成功后通常可用同一 IP 的「连接端口」连接（无线调试页面显示的端口）
    conn_addr = args.connect_address or address
    if not args.connect_address:
        print(f"提示：配对端口与连接端口通常不同，请在「无线调试」主页查看端口。")
        user = input(f"连接 IP:端口（回车默认 {conn_addr}）：").strip()
        if user:
            conn_addr = user
    ok2, text2 = U.connect(conn_addr, adb_path=adb_path)
    print(("[已连接] " if ok2 else "[连接失败] ") + text2)
    return conn_addr if ok2 else None


def do_auto(args, adb_path: Optional[str]) -> Optional[str]:
    """自动发现并连接设备（无需手动输入 IP）。返回可用序列号。"""
    print("正在自动搜索并连接设备…")
    addr = U.auto_connect(args.serial, adb_path=adb_path, on_log=lambda m: print("  " + m))
    if not addr:
        print("未找到可用设备。请确认手机已开启「无线调试」并与电脑处于同一 WiFi；"
              "或先用 USB 连接执行 `--to-wireless`。")
        return None
    info = U.get_device_info(addr, adb_path=adb_path)
    print(f"设备就绪：{info.get('brand')} {info.get('model')} "
          f"Android {info.get('android')} (SDK {info.get('sdk')})｜{addr}")
    return addr


def do_connect(args, adb_path: Optional[str]) -> Optional[str]:
    """连接无线设备。--address 未给时先取 USB 设备 IP，再退化为 mDNS 自动连接。"""
    address = args.address
    if not address:
        # 若已有 USB 设备，自动获取其 IP 并用默认端口
        try:
            dev = U.pick_device(args.serial, adb_path=adb_path)
            ip = U.get_device_ip(dev.serial, adb_path=adb_path)
            if ip:
                address = f"{ip}:{args.port}"
                print(f"自动获取到设备 IP：{ip}")
        except U.AdbError:
            pass
    if not address:
        # 没有 USB 设备：尝试 mDNS 自动发现并连接
        print("未发现 USB 设备，尝试通过 mDNS 自动发现无线调试设备…")
        auto_addr = U.auto_connect(args.serial, adb_path=adb_path,
                                   on_log=lambda m: print("  " + m))
        if auto_addr:
            info = U.get_device_info(auto_addr, adb_path=adb_path)
            print(f"设备就绪：{info.get('brand')} {info.get('model')} "
                  f"Android {info.get('android')} (SDK {info.get('sdk')})｜{auto_addr}")
            return auto_addr
        address = input("自动发现失败，请手动输入设备 IP:端口（例如 192.168.1.5:5555）：").strip()
    if not address:
        return None

    ok, text = U.connect(address, adb_path=adb_path)
    print(("[已连接] " if ok else "[连接失败] ") + text)
    if not ok:
        return None
    if U.wait_for_device(address, timeout=20, adb_path=adb_path):
        info = U.get_device_info(address, adb_path=adb_path)
        print(f"设备就绪：{info.get('brand')} {info.get('model')} "
              f"Android {info.get('android')} (SDK {info.get('sdk')})")
        return address
    print("[警告] 设备已连接但未进入 device 状态，请检查手机上的授权弹窗。")
    return None


def do_to_wireless(args, adb_path: Optional[str]) -> Optional[str]:
    """
    传统无线调试流程：
        1) USB 连接设备（需已授权）
        2) adb tcpip <port>
        3) 自动获取 IP 并 adb connect
    """
    print("=== 切换到无线调试（传统方式）===")
    try:
        dev = U.pick_device(args.serial, adb_path=adb_path, prefer_wireless=False)
    except U.AdbError as exc:
        print(f"[错误] {exc}")
        return None
    if dev.is_wireless:
        print(f"当前已是无线连接：{dev.serial}")
        return dev.serial

    print(f"USB 设备：{dev.serial}（{dev.model}）")
    ip = args.device_ip or U.get_device_ip(dev.serial, adb_path=adb_path)
    if not ip:
        ip = input("无法自动获取设备 IP，请手动输入（例如 192.168.1.5）：").strip()

    try:
        U.enable_tcpip(dev.serial, port=args.port, adb_path=adb_path)
    except U.AdbError as exc:
        print(f"[错误] 开启 TCP 模式失败：{exc}")
        return None
    print(f"已开启 TCP 模式，端口 {args.port}。")

    address = f"{ip}:{args.port}"
    ok, text = U.connect(address, adb_path=adb_path)
    print(("[已连接] " if ok else "[连接失败] ") + text)
    if ok and U.wait_for_device(address, timeout=20, adb_path=adb_path):
        print(f"现在可以拔掉 USB 线，后续将完全无线推送（目标：{address}）。")
        return address
    print("[警告] 无线连接未就绪，请确认手机与电脑在同一 WiFi 且未被防火墙拦截。")
    return None


# --------------------------------------------------------------------------- #
# 推送
# --------------------------------------------------------------------------- #

def build_reconnect(serial: Optional[str], address: Optional[str],
                    adb_path: Optional[str], reporter: ConsoleReporter):
    """构造断线自动重连函数：优先重连已知无线地址，其次重新探测。"""
    def _reconnect() -> Optional[str]:
        addr = address
        if not addr and serial and ":" in serial:
            addr = serial
        if addr:
            ok, text = U.connect(addr, adb_path=adb_path)
            reporter.log(f"[重连] {text}")
            if ok and U.wait_for_device(addr, timeout=15, adb_path=adb_path):
                return addr
        # 再尝试同网段 mDNS 发现
        for candidate in U.discover_mdns(adb_path=adb_path):
            ok, text = U.connect(candidate, adb_path=adb_path)
            reporter.log(f"[重连] {candidate} -> {text}")
            if ok and U.wait_for_device(candidate, timeout=10, adb_path=adb_path):
                return candidate
        return None
    return _reconnect


def run_sync(args, adb_path: Optional[str], reporter: ConsoleReporter) -> int:
    """执行一次同步任务（推送或拉取），返回退出码（0 成功 / 1 有失败 / 2 致命错误）。"""
    direction = getattr(args, "direction", "push") or "push"
    if getattr(args, "pull", False):
        direction = "pull"
    push = (direction == "push")

    try:
        dev = U.pick_device(args.serial, adb_path=adb_path)
    except U.AdbError as exc:
        print(f"[错误] {exc}")
        print("提示：可先执行 `--auto`（自动连接）或 `--to-wireless`（USB 转无线）。")
        return 2

    info = U.get_device_info(dev.serial, adb_path=adb_path)
    reporter.log(f"目标设备：{info.get('brand')} {info.get('model')} "
                 f"Android {info.get('android')} (SDK {info.get('sdk')})｜{dev.serial}")

    mode = getattr(args, "newer_only", False) and "newer" or args.mode

    # 推送方向做一次容量检查（仅提示，不阻断）
    if push:
        try:
            need = sum(os.path.getsize(p) for p, _ in U.iter_local_files(args.source))
            free = U.remote_free_bytes(dev.serial, args.target, adb_path=adb_path)
            if free is not None and need > free:
                reporter.log(f"[警告] 目标空间可能不足：需要 {U._human(need)}，可用 {U._human(free)}")
        except (OSError, U.AdbError):
            pass

    try:
        stats = T.sync_path(
            source=args.source,
            target=args.target,
            serial=dev.serial,
            adb_path=adb_path,
            direction=direction,
            mode=mode,
            verify=args.verify,
            hash_algo=getattr(args, "hash_algo", None),
            resume=not getattr(args, "no_resume", False),
            chunk_size=int(getattr(args, "chunk_size", 8) or 8) * 1024 * 1024,
            jobs=int(getattr(args, "jobs", T.DEFAULT_JOBS) or 1),
            parallel_threshold=int(getattr(args, "parallel_threshold", 64) or 64) * 1024 * 1024,
            dry_run=args.dry_run,
            use_cache=not getattr(args, "no_cache", False),
            on_log=reporter.log,
            on_progress=reporter.on_progress,
            reconnect=build_reconnect(dev.serial, args.address, adb_path, reporter),
        )
    except U.AdbError as exc:
        reporter.finish_line()
        print(f"[错误] {exc}")
        return 2
    except (OSError, ValueError) as exc:
        # 文件/路径层面的意外错误（磁盘满、路径非法等）也要给出退出码，
        # 而不是抛出栈回溯把批处理打断。
        reporter.finish_line()
        print(f"[错误] {exc}")
        return 2
    finally:
        reporter.finish_line()

    if stats.failed:
        print(f"\n以下 {stats.failed} 个文件传输失败：")
        for dest, reason in stats.failed_files[:20]:
            print(f"  - {dest}：{reason}")
        if stats.failed > 20:
            print(f"  ... 其余 {stats.failed - 20} 个略")
        return 1
    if stats.verify_failed:
        print(f"\n[警告] 有 {stats.verify_failed} 个文件校验未通过：")
        for dest, reason in stats.verify_errors[:20]:
            print(f"  - {dest}：{reason}")
        return 1
    return 0


def run_batch(args, adb_path: Optional[str], reporter: ConsoleReporter) -> int:
    """按配置文件中的 jobs 依次批量推送。"""
    cfg = load_config(args.config)
    jobs = cfg.get("jobs") or []
    if not jobs:
        print("[错误] 配置文件中没有 jobs（批量任务列表）。")
        return 2

    rc = 0
    for i, job in enumerate(jobs, 1):
        print(f"\n===== 批量任务 {i}/{len(jobs)}：{job.get('source')} -> {job.get('target')} =====")
        sub = argparse.Namespace(**vars(args))
        sub.source = job.get("source")
        sub.target = job.get("target")
        sub.direction = job.get("direction", getattr(args, "direction", "push"))
        sub.pull = job.get("direction", getattr(args, "direction", "push")) == "pull"
        sub.mode = job.get("mode", args.mode)
        sub.verify = job.get("verify", args.verify)
        sub.newer_only = job.get("newer_only", args.newer_only)
        sub.dry_run = job.get("dry_run", args.dry_run)
        sub.jobs = job.get("jobs", getattr(args, "jobs", T.DEFAULT_JOBS))
        sub.parallel_threshold = job.get(
            "parallel_threshold_mb", getattr(args, "parallel_threshold", 64))
        if not sub.source or not sub.target:
            print("[跳过] 该任务缺少 source 或 target。")
            continue
        rc = max(rc, run_sync(sub, adb_path, reporter))
    return rc


# --------------------------------------------------------------------------- #
# 配置文件
# --------------------------------------------------------------------------- #

def load_config(path: str) -> dict:
    """
    读取 JSON 配置；不存在时返回空配置。

    用 utf-8-sig 读取：Windows 记事本等编辑器保存 JSON 时可能带 UTF-8 BOM，
    用普通 utf-8 打开会直接抛 JSONDecodeError。
    """
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as fp:
            data = json.load(fp)
    except (OSError, ValueError) as exc:
        print("[错误] 配置文件无法解析：{}（{}）".format(path, exc))
        return {}
    return data if isinstance(data, dict) else {}


def save_config(path: str, data: dict) -> None:
    """写出 JSON 配置（UTF-8 无 BOM，缩进 2）。"""
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


def remember(args, adb_path: Optional[str]) -> None:
    """把常用设备 / 目标目录保存到 config.json。"""
    cfg = load_config(args.config)
    cfg.setdefault("devices", [])
    if args.address:
        entry = {"address": args.address}
        if entry not in cfg["devices"]:
            cfg["devices"].append(entry)
    if args.target:
        cfg.setdefault("targets", [])
        if args.target not in cfg["targets"]:
            cfg["targets"].append(args.target)
    save_config(args.config, cfg)
    print(f"已写入配置：{args.config}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    p = argparse.ArgumentParser(
        prog="adb_push",
        description="通过 ADB 无线调试把任意文件/文件夹推送到安卓设备指定目录。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  # 推送（差异同步 + 双校验 + 断点续传，均为默认）\n"
               "  python adb_push.py --source \"D:\\我的文件\" --target /sdcard/Download/我的文件\n"
               "  # 拉取（设备 -> 本地）\n"
               "  python adb_push.py --pull --source /sdcard/DCIM/Camera --target \"D:\\照片备份\"\n"
               "  # 仅比对不传输\n"
               "  python adb_push.py --source \"D:\\我的文件\" --target /sdcard/Download --dry-run\n"
               "  python adb_push.py --auto\n",
    )

    g_src = p.add_argument_group("传输参数")
    g_src.add_argument("--source", "-s",
                       help="源路径：push 时为本地路径，pull 时为设备路径（传输时必填）")
    g_src.add_argument("--target", "-t",
                       help="目标路径：push 时为设备目录，pull 时为本地目录")
    g_src.add_argument("--direction", choices=["push", "pull"], default="push",
                       help="方向：push 本地→设备（默认）｜pull 设备→本地")
    g_src.add_argument("--pull", action="store_true",
                       help="等价于 --direction pull（从设备取回）")
    g_src.add_argument("--mode", choices=["overwrite", "skip-existing", "diff", "newer"],
                       default="diff",
                       help="overwrite 总是传｜skip-existing 已存在则跳过｜"
                            "diff 缺失或哈希不一致才传（默认）｜newer 源较新才传")
    g_src.add_argument("--verify", nargs="?", const="hash", default="hash",
                       choices=["none", "size", "hash"],
                       help="传输后校验：none｜size 仅大小｜hash 大小+哈希（默认，双校验）")
    g_src.add_argument("--hash-algo", choices=["md5", "sha256", "sha1"], default=None,
                       help="哈希算法，默认自动探测设备支持（通常 md5）")
    g_src.add_argument("--no-resume", action="store_true", help="关闭断点续传")
    g_src.add_argument("--no-cache", action="store_true", help="不使用远端哈希清单缓存")
    g_src.add_argument("--chunk-size", type=int, default=8,
                       help="分片大小（MB），默认 8，决定续传粒度")
    g_src.add_argument("--jobs", "-j", type=int, default=T.DEFAULT_JOBS,
                       help="并发上限，默认 {}（多文件按文件并行，单大文件按分片并行；1 = 关闭并行）".format(
                           T.DEFAULT_JOBS))
    g_src.add_argument("--parallel-threshold", type=int, default=64,
                       help="单文件达到该大小（MB）才启用分块级并行，默认 64")
    g_src.add_argument("--dry-run", action="store_true", help="只打印将执行的操作，不实际传输")
    g_src.add_argument("--newer-only", action="store_true",
                       help="等价于 --mode newer（仅传源修改时间较新的文件）")

    g_dev = p.add_argument_group("设备与网络")
    g_dev.add_argument("--serial", help="指定设备序列号（多设备时使用）")
    g_dev.add_argument("--address", "-a", help="无线设备地址 IP:端口（如 192.168.1.5:5555）")
    g_dev.add_argument("--connect-address", help="配对后用于连接（adb connect）的 IP:端口")
    g_dev.add_argument("--pair-code", help="配对码（不填则交互输入）")
    g_dev.add_argument("--port", type=int, default=5555, help="tcpip / connect 使用的端口，默认 5555")
    g_dev.add_argument("--device-ip", help="手动指定设备 IP（自动获取失败时使用）")
    g_dev.add_argument("--adb", help="adb 可执行文件路径（默认自动定位）")

    g_act = p.add_argument_group("动作")
    g_act.add_argument("--devices", action="store_true", help="列出当前设备")
    g_act.add_argument("--auto", action="store_true",
                       help="自动发现并连接局域网设备（USB 或无线均可）")
    g_act.add_argument("--discover", action="store_true", help="mDNS 发现局域网内无线调试设备")
    g_act.add_argument("--pair", action="store_true", help="进入 Android 11+ 无线配对流程")
    g_act.add_argument("--connect", action="store_true", help="仅连接设备，不推送")
    g_act.add_argument("--to-wireless", action="store_true",
                       help="由 USB 切换为无线调试（adb tcpip + adb connect）")

    g_cfg = p.add_argument_group("配置与批量")
    g_cfg.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径，默认同目录 config.json")
    g_cfg.add_argument("--batch", action="store_true", help="按配置文件的任务列表批量传输")
    g_cfg.add_argument("--save-config", action="store_true", help="把 --address / --target 写入配置")
    g_cfg.add_argument("--quiet", action="store_true", help="精简输出（不显示进度条）")

    return p


def main(argv: Optional[List[str]] = None) -> int:
    """程序入口。"""
    args = build_parser().parse_args(argv)
    reporter = ConsoleReporter(quiet=args.quiet)

    # 定位 adb
    try:
        adb_path = U.resolve_adb(args.adb)
    except U.AdbNotFoundError as exc:
        print(f"[错误] {exc}")
        return 2
    if not args.quiet:
        print(f"adb：{adb_path}")

    U.start_server(adb_path=adb_path)

    # 一次性动作
    if args.devices:
        print_devices(adb_path)
        return 0

    if args.discover:
        do_discover(adb_path)
        return 0

    if args.auto:
        addr = do_auto(args, adb_path)
        if args.save_config and addr:
            args.address = addr
            remember(args, adb_path)
        return 0 if addr else 1

    if args.pair:
        addr = do_pair(args, adb_path)
        if args.save_config and addr:
            args.address = addr
            remember(args, adb_path)
        return 0 if addr else 1

    if args.to_wireless:
        addr = do_to_wireless(args, adb_path)
        if args.save_config and addr:
            args.address = addr
            remember(args, adb_path)
        return 0 if addr else 1

    if args.connect:
        addr = do_connect(args, adb_path)
        if args.save_config and addr:
            args.address = addr
            remember(args, adb_path)
        return 0 if addr else 1

    if args.save_config:
        remember(args, adb_path)
        return 0

    if args.batch:
        return run_batch(args, adb_path, reporter)

    # 默认：传输（推送或拉取）
    if not args.source or not args.target:
        print("[错误] 传输需要同时指定 --source 和 --target。")
        build_parser().print_help()
        print("\n提示：配置文件中保存了常用目标目录，可用 --config 复用。")
        return 2
    if (args.direction == "push") and not args.pull and not os.path.exists(args.source):
        print(f"[错误] 本地源路径不存在：{args.source}")
        return 2

    return run_sync(args, adb_path, reporter)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[已取消] 用户中断。")
        sys.exit(130)
