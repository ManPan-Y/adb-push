# -*- coding: utf-8 -*-
"""
gui.py
======
图形化界面（Tkinter，Python 标准库自带，无需额外安装）。

把「设备连接 -> 无线配对 -> 选择源/目标 -> 推送」全过程可视化：
    * 左侧：设备列表、无线连接工具按钮、adb 路径
    * 右侧：推送配置与实时进度、彩色日志

运行：python gui.py    （或双击 启动GUI.bat）
"""

from __future__ import annotations

import os
import posixpath
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adb_utils as U  # noqa: E402
import transfer as T  # noqa: E402

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


class DevicePathDialog(tk.Toplevel):
    """
    设备端路径选择对话框：浏览手机上的目录与文件。

    * 双击目录进入；「上级」返回；「转到」按输入框路径读取
    * 选中某项后「确定」返回该项完整路径
    * 未选中任何项时「确定」= 选用当前目录
    """

    # 常用位置快捷入口
    QUICK_DIRS = (
        ("/sdcard", "/sdcard"),
        ("Download", "/sdcard/Download"),
        ("DCIM", "/sdcard/DCIM"),
        ("Documents", "/sdcard/Documents"),
        ("Pictures", "/sdcard/Pictures"),
    )

    def __init__(self, parent, adb_path: str, serial: str,
                 title: str = "选择设备路径", initial: str = "/sdcard",
                 want_files: bool = True):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(True, True)
        self.geometry("640x480")
        self.minsize(520, 380)

        self.adb_path = adb_path
        self.serial = serial
        self.want_files = want_files
        self.result: Optional[str] = None
        self._entries: List[Tuple[str, str]] = []

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="路径：").grid(row=0, column=0, sticky="w")
        self.path_var = tk.StringVar(value=initial or "/sdcard")
        entry = ttk.Entry(body, textvariable=self.path_var)
        entry.grid(row=0, column=1, sticky="ew", padx=4)
        entry.bind("<Return>", lambda _e: self._refresh())

        bar = ttk.Frame(body)
        bar.grid(row=0, column=2, sticky="e")
        ttk.Button(bar, text="上级", width=6, command=self._go_up).pack(side="left", padx=2)
        ttk.Button(bar, text="刷新", width=6, command=self._refresh).pack(side="left")

        quick = ttk.Frame(body)
        quick.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(quick, text="快捷：").pack(side="left")
        for label, target in self.QUICK_DIRS:
            ttk.Button(quick, text=label, width=10,
                       command=lambda t=target: self._goto(t)).pack(side="left", padx=2)

        wrap = ttk.Frame(body)
        wrap.grid(row=2, column=0, columnspan=3, sticky="nsew", pady=6)
        body.rowconfigure(2, weight=1)
        self.listbox = tk.Listbox(wrap, exportselection=False)
        sb = ttk.Scrollbar(wrap, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.listbox.bind("<Double-Button-1>", self._on_double)

        hint = "双击进入目录；选中后「确定」选用该项，不选则选用当前目录"
        if not want_files:
            hint += "（本处只列目录）"
        self.status = ttk.Label(body, text=hint, foreground="#666")
        self.status.grid(row=3, column=0, columnspan=3, sticky="w")

        foot = ttk.Frame(body)
        foot.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Button(foot, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(foot, text="确定", command=self._ok).pack(side="right", padx=6)

        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self._refresh()
        self.listbox.focus_set()
        # 窗口尚未映射时 grab_set 会失败，延迟到可视后再抓取
        self.after(120, self._grab_soft)

    def _grab_soft(self) -> None:
        try:
            self.grab_set()
        except tk.TclError:
            pass

    # ------------------------------------------------------------ 内部 --
    def _refresh(self) -> None:
        path = self.path_var.get().strip() or "/"
        self.path_var.set(path)
        self.listbox.delete(0, "end")
        self._entries = []
        try:
            entries = U.remote_list_dir(self.serial, path, adb_path=self.adb_path)
        except U.AdbError as exc:
            self.status.configure(text="读取失败：{}".format(exc))
            return
        # 目录在前，其次按名称
        entries.sort(key=lambda e: (e[1] != "d", e[0].lower()))
        for name, kind in entries:
            if kind == "f" and not self.want_files:
                continue
            self._entries.append((name, kind))
            self.listbox.insert("end", ("[目录] " if kind == "d" else "[文件] ") + name)
        if not self._entries:
            self.status.configure(text="{}　（无可见项）".format(path))
        else:
            self.status.configure(
                text="{}　共 {} 项（双击进入目录）".format(path, len(self._entries)))

    def _goto(self, path: str) -> None:
        self.path_var.set(path)
        self._refresh()

    def _go_up(self) -> None:
        cur = self.path_var.get().strip().rstrip("/") or "/"
        if cur == "/":
            return
        self._goto(posixpath.dirname(cur) or "/")

    def _selected_full(self) -> Optional[str]:
        sel = self.listbox.curselection()
        if not sel:
            return None
        name = self._entries[sel[0]][0]
        base = self.path_var.get().strip().rstrip("/")
        return "{}/{}".format(base, name) if base else "/" + name

    def _on_double(self, _event=None) -> None:
        sel = self.listbox.curselection()
        if sel and self._entries[sel[0]][1] == "d":
            self._goto(self._selected_full())

    def _ok(self) -> None:
        self.result = self._selected_full() or (self.path_var.get().strip() or "/")
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class AdbPushGUI:
    """主窗口。所有耗时操作放到后台线程，通过队列回传事件给 UI 线程。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ADB 无线推送工具")
        self.root.geometry("980x680")
        self.root.minsize(900, 600)

        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.busy = False
        self.cancel_requested = False
        # 由主线程维护的「选中设备」纯文本镜像：后台线程只能读这个，
        # 不能直接读 Tk 变量（Tkinter 不是线程安全的）。
        self.serial_live: str = ""
        self.adb_path: Optional[str] = None

        self._build_ui()
        self._poll_events()
        self.log("欢迎使用 ADB 无线推送工具。")
        self.log("提示：首次使用请先用 USB 连接并允许调试授权，然后点击「USB 转无线」。")

    # ------------------------------------------------------------------ UI --
    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # ---------- 顶部：adb 路径 ----------
        top = ttk.LabelFrame(outer, text="1. 环境", padding=8)
        top.pack(fill="x")
        self.adb_var = tk.StringVar(value="（未检测）")
        ttk.Label(top, text="adb 路径：").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.adb_var, state="readonly").grid(
            row=0, column=1, sticky="ew", padx=4)
        ttk.Button(top, text="浏览…", command=self.pick_adb).grid(row=0, column=2)
        top.columnconfigure(1, weight=1)

        # ---------- 中部左：设备 ----------
        mid = ttk.Frame(outer)
        mid.pack(fill="both", expand=True, pady=8)

        left = ttk.LabelFrame(mid, text="2. 设备连接", padding=8)
        left.pack(side="left", fill="both", expand=False)

        self.device_box = tk.Listbox(left, height=8, width=46, exportselection=False)
        self.device_box.grid(row=0, column=0, columnspan=3, sticky="nsew")
        self.device_box.bind("<<ListboxSelect>>", self.on_device_select)

        btns = [
            ("自动连接", self.do_auto_connect),
            ("刷新设备", self.do_refresh),
            ("USB 转无线", self.do_to_wireless),
            ("无线配对", self.do_pair),
            ("连接地址", self.do_connect),
            ("mDNS 发现", self.do_discover),
            ("断开", self.do_disconnect),
        ]
        for i, (text, cmd) in enumerate(btns):
            ttk.Button(left, text=text, command=cmd, width=14).grid(
                row=1 + i // 3, column=i % 3, padx=3, pady=3, sticky="ew")

        ttk.Label(left, text="选中设备：").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.serial_var = tk.StringVar()
        ttk.Entry(left, textvariable=self.serial_var).grid(
            row=4, column=1, columnspan=2, sticky="ew", padx=4, pady=(6, 0))

        ttk.Label(left, text="设备 IP：").grid(row=5, column=0, sticky="w", pady=(4, 0))
        self.ip_var = tk.StringVar()
        ttk.Entry(left, textvariable=self.ip_var).grid(
            row=5, column=1, sticky="ew", padx=4, pady=(4, 0))
        ttk.Button(left, text="自动获取", command=self.do_get_ip, width=8).grid(
            row=5, column=2, sticky="ew", pady=(4, 0))

        ttk.Label(left, text="端口：").grid(row=6, column=0, sticky="w", pady=(4, 0))
        self.port_var = tk.StringVar(value="5555")
        ttk.Entry(left, textvariable=self.port_var, width=8).grid(
            row=6, column=1, sticky="w", padx=4, pady=(4, 0))

        # ---------- 中部右：传输配置 ----------
        right = ttk.LabelFrame(mid, text="3. 传输配置", padding=8)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))

        drow = ttk.Frame(right)
        drow.grid(row=0, column=0, columnspan=4, sticky="ew")
        ttk.Label(drow, text="方向：").pack(side="left")
        self.dir_var = tk.StringVar(value="push")
        ttk.Radiobutton(drow, text="推送 本地→设备", variable=self.dir_var,
                        value="push", command=self._on_direction).pack(side="left", padx=4)
        ttk.Radiobutton(drow, text="拉取 设备→本地", variable=self.dir_var,
                        value="pull", command=self._on_direction).pack(side="left", padx=4)

        ttk.Label(right, text="源路径：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.src_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.src_var).grid(
            row=1, column=1, sticky="ew", padx=4, pady=(6, 0))
        # 按钮文案随方向变化：push 选本地，pull 选设备
        self.src_file_btn = ttk.Button(right, text="本地文件", width=10,
                                       command=self.pick_src_file)
        self.src_file_btn.grid(row=1, column=2, pady=(6, 0))
        self.src_dir_btn = ttk.Button(right, text="本地文件夹", width=11,
                                      command=self.pick_src_dir)
        self.src_dir_btn.grid(row=1, column=3, padx=(4, 0), pady=(6, 0))

        ttk.Label(right, text="目标路径：").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.dst_var = tk.StringVar(value="/sdcard/Download/")
        ttk.Entry(right, textvariable=self.dst_var).grid(
            row=2, column=1, sticky="ew", padx=4, pady=(6, 0))
        tgt_bar = ttk.Frame(right)
        tgt_bar.grid(row=2, column=2, columnspan=2, sticky="e", pady=(6, 0))
        self.dst_browse_btn = ttk.Button(tgt_bar, text="设备目录…", width=11,
                                         command=self.pick_dst)
        self.dst_browse_btn.pack(side="left", padx=2)
        ttk.Button(tgt_bar, text="常用", width=6,
                   command=self.pick_saved_target).pack(side="left", padx=2)
        ttk.Button(tgt_bar, text="保存", width=6,
                   command=self.save_target).pack(side="left", padx=2)

        opt = ttk.Frame(right)
        opt.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Label(opt, text="已存在：").pack(side="left")
        self.mode_var = tk.StringVar(value="diff")
        for text, val in (("差异同步", "diff"), ("覆盖", "overwrite"),
                          ("跳过", "skip-existing"), ("仅较新", "newer")):
            ttk.Radiobutton(opt, text=text, variable=self.mode_var,
                            value=val).pack(side="left", padx=3)

        opt2 = ttk.Frame(right)
        opt2.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Label(opt2, text="双校验：").pack(side="left")
        self.verify_var = tk.StringVar(value="hash")
        ttk.Radiobutton(opt2, text="大小+哈希", variable=self.verify_var,
                        value="hash").pack(side="left", padx=3)
        ttk.Radiobutton(opt2, text="仅大小", variable=self.verify_var,
                        value="size").pack(side="left", padx=3)
        ttk.Radiobutton(opt2, text="不校验", variable=self.verify_var,
                        value="none").pack(side="left", padx=3)
        self.resume_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt2, text="断点续传", variable=self.resume_var).pack(side="left", padx=10)
        self.dry_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt2, text="试运行", variable=self.dry_var).pack(side="left", padx=10)

        opt3 = ttk.Frame(right)
        opt3.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Label(opt3, text="并发数：").pack(side="left")
        self.jobs_var = tk.StringVar(value=str(T.DEFAULT_JOBS))
        ttk.Spinbox(opt3, from_=1, to=32, width=5,
                    textvariable=self.jobs_var).pack(side="left", padx=3)
        ttk.Label(opt3, text="（多文件按文件并行；单个大文件按分片并行；1 = 关闭并行）",
                  foreground="#666").pack(side="left", padx=6)

        # 进度条 + 三个独立字段：进度 / 速度 / 状态
        prog = ttk.Frame(right)
        prog.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        prog.columnconfigure(1, weight=1)

        self.pb_var = tk.DoubleVar(value=0)
        ttk.Progressbar(prog, variable=self.pb_var, maximum=100).grid(
            row=0, column=0, columnspan=2, sticky="ew")

        ttk.Label(prog, text="进度：").grid(row=1, column=0, sticky="w", pady=(5, 0))
        self.prog_var = tk.StringVar(value="—")
        ttk.Label(prog, textvariable=self.prog_var, foreground="#1f4e79").grid(
            row=1, column=1, sticky="w", padx=(2, 0), pady=(5, 0))

        ttk.Label(prog, text="速度：").grid(row=2, column=0, sticky="w", pady=(3, 0))
        self.speed_var = tk.StringVar(value="—")
        ttk.Label(prog, textvariable=self.speed_var, foreground="#1f4e79").grid(
            row=2, column=1, sticky="w", padx=(2, 0), pady=(3, 0))

        ttk.Label(prog, text="状态：").grid(row=3, column=0, sticky="nw", pady=(3, 0))
        self.cur_var = tk.StringVar(value="就绪")
        ttk.Label(prog, textvariable=self.cur_var, foreground="#0b6623",
                  wraplength=560, justify="left").grid(
            row=3, column=1, sticky="w", padx=(2, 0), pady=(3, 0))

        # 操作按钮
        act = ttk.Frame(right)
        act.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        self.start_btn = ttk.Button(act, text="开始推送", command=self.start_push)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(act, text="取消", command=self.cancel_push, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        ttk.Button(act, text="查看设备列表", command=self.do_refresh).pack(side="left", padx=6)
        ttk.Button(act, text="清空日志", command=self.clear_log).pack(side="right")

        right.columnconfigure(1, weight=1)

        # ---------- 底部：日志 ----------
        bottom = ttk.LabelFrame(outer, text="4. 实时日志", padding=6)
        bottom.pack(fill="both", expand=True)
        self.log_text = tk.Text(bottom, height=12, wrap="word", state="disabled",
                                background="#1e1e1e", foreground="#d4d4d4",
                                insertbackground="#d4d4d4")
        scroll = ttk.Scrollbar(bottom, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        # 日志着色标签
        self.log_text.tag_config("ok", foreground="#4ec9b0")
        self.log_text.tag_config("err", foreground="#f48771")
        self.log_text.tag_config("warn", foreground="#dcdcaa")
        self.log_text.tag_config("info", foreground="#9cdcfe")

        # 尝试定位 adb
        self.detect_adb_async()

    # ------------------------------------------------------------- 日志 --
    def log(self, msg: str, tag: str = "") -> None:
        """
        线程安全地追加日志（实际写入在 UI 线程的 _poll_events 中完成），
        并同步刷新「状态」字段 —— 任何一条日志都能反映当前正在做什么。
        """
        self.events.put(("log", msg, tag))
        self.events.put(("status", msg))

    def _write_log(self, msg: str, tag: str = "") -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", time.strftime("[%H:%M:%S] ") + str(msg) + "\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------- 事件轮询 --
    def _poll_events(self) -> None:
        """主线程定时消费后台事件队列。"""
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            # 单个事件处理出错绝不能打断 after 轮询，否则界面会永久停止刷新
            try:
                self._handle_event(ev)
            except Exception as exc:                      # noqa: BLE001
                try:
                    self._write_log("事件处理异常：{}".format(exc), "err")
                except Exception:
                    pass
        self.root.after(120, self._poll_events)

    def _handle_event(self, ev) -> None:
        """处理单条后台事件（保证在主线程执行）。"""
        kind = ev[0]
        if kind == "log":
            self._write_log(ev[1], ev[2] if len(ev) > 2 else "")
        elif kind == "devices":
            self._render_devices(ev[1])
        elif kind == "progress":
            _, pct, prog_text, speed_text = ev
            self.pb_var.set(max(0, min(100, pct)))
            self.prog_var.set(prog_text)
            if speed_text is not None:
                self.speed_var.set(speed_text)
        elif kind == "status":
            # 阶段提示（准备/比对/单文件完成等），只更新状态行
            self.cur_var.set(ev[1])
        elif kind == "busy":
            self._set_busy(ev[1])
        elif kind == "adb":
            self.adb_var.set(ev[1])
        elif kind == "serial":
            self.serial_live = ev[1] or ""
            self.serial_var.set(self.serial_live)
        elif kind == "fields":
            # 后台线程要写控件，一律通过事件回到主线程来做
            for name, value in ev[1].items():
                getattr(self, name).set(value)
        elif kind == "info":
            messagebox.showinfo("提示", ev[1])
        elif kind == "error":
            messagebox.showerror("错误", ev[1])

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.start_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if busy else "disabled")

    # --------------------------------------------------------- 线程工具 --
    def run_bg(self, fn, *a, **kw) -> None:
        """在后台线程执行 fn(*a, **kw)。"""
        if self.busy:
            messagebox.showwarning("请稍候", "当前有任务正在执行。")
            return
        self._set_busy(True)

        def _wrap():
            try:
                fn(*a, **kw)
            except U.AdbError as exc:
                self.log(f"[错误] {exc}", "err")
                self.events.put(("error", str(exc)))
            except Exception as exc:  # noqa: BLE001
                self.log(f"[异常] {exc}", "err")
                self.events.put(("error", repr(exc)))
            finally:
                self.events.put(("busy", False))
        self.worker = threading.Thread(target=_wrap, daemon=True)
        self.worker.start()

    # --------------------------------------------------- 主/后台线程边界 --
    def _snapshot(self) -> dict:
        """
        在主线程读取界面状态，供后台线程使用。

        Tkinter 变量不是线程安全的：后台线程一律只用这里的**普通值副本**，
        或读取 self.serial_live（主线程维护的镜像）。
        """
        return {
            "serial": self.serial_var.get().strip(),
            "ip": self.ip_var.get().strip(),
            "port": self.port_var.get().strip(),
            "src": self.src_var.get().strip(),
            "dst": self.dst_var.get().strip(),
            "direction": self.dir_var.get(),
            "mode": self.mode_var.get(),
            "verify": self.verify_var.get(),
            "resume": self.resume_var.get(),
            "jobs": self.jobs_var.get(),
            "dry_run": self.dry_var.get(),
        }

    def _ask_main(self, fn, *args, **kwargs):
        """
        在 UI 线程执行 fn 并等待结果。

        后台线程需要弹窗（simpledialog 等）时必须走这里：直接在后台线程
        创建 Tk 对话框属于未定义行为。
        """
        box: dict = {}
        done = threading.Event()

        def _run() -> None:
            try:
                box["result"] = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        self.root.after(0, _run)
        done.wait()
        if "error" in box:
            raise box["error"]
        return box.get("result")

    # ------------------------------------------------------------ adb --
    def detect_adb_async(self) -> None:
        initial_serial = self.serial_var.get().strip()   # 主线程读取一次

        def _job():
            try:
                path = U.resolve_adb()
                self.adb_path = path
                self.events.put(("adb", path))
                self.log(f"已找到 adb：{path}", "ok")
                U.start_server(adb_path=path)
                self._refresh_devices()
                self._auto_connect(initial_serial)
            except U.AdbNotFoundError as exc:
                self.events.put(("adb", "未找到 adb"))
                self.log(str(exc), "err")
        threading.Thread(target=_job, daemon=True).start()

    def pick_adb(self) -> None:
        path = filedialog.askopenfilename(title="选择 adb.exe",
                                          filetypes=[("adb", "adb.exe"), ("所有文件", "*.*")])
        if path:
            self.adb_path = path
            self.adb_var.set(path)
            self.log(f"手动指定 adb：{path}", "info")
            self.do_refresh()

    # --------------------------------------------------------- 设备操作 --
    def _refresh_devices(self) -> None:
        """（需在后台线程调用）读取设备列表并推送到 UI。"""
        try:
            devices = U.list_devices(adb_path=self.adb_path)
        except U.AdbError as exc:
            self.log(f"读取设备列表失败：{exc}", "err")
            return
        self.events.put(("devices", devices))
        if devices:
            self.log(f"检测到 {len(devices)} 台设备。", "ok")
        else:
            self.log("未检测到设备。可用 USB 连接后点击「USB 转无线」，或使用「无线配对」。", "warn")

    def do_refresh(self) -> None:
        self.run_bg(self._refresh_devices)

    def _auto_connect(self, serial: str = "") -> None:
        """（需在后台线程调用）自动发现并连接设备，成功后回填序列号。"""
        self.log("正在自动搜索并连接设备…")
        try:
            addr = U.auto_connect(
                serial or None,
                adb_path=self.adb_path,
                on_log=lambda m: self.log(m, "info"),
            )
        except U.AdbError as exc:
            self.log(f"自动连接出错：{exc}", "err")
            addr = None
        if addr:
            self.events.put(("serial", addr))
            self.log(f"已自动连接：{addr}", "ok")
        else:
            self.log("未能自动连接。可用 USB 连接后点「USB 转无线」，或点「无线配对」。", "warn")
        self._refresh_devices()

    def do_auto_connect(self) -> None:
        self.run_bg(self._auto_connect, self.serial_var.get().strip())

    def _render_devices(self, devices) -> None:
        self.device_box.delete(0, "end")
        for d in devices:
            self.device_box.insert("end", str(d))
            if d.is_ready and not self.serial_live:
                self.serial_live = d.serial
                self.serial_var.set(d.serial)

    def on_device_select(self, _event=None) -> None:
        sel = self.device_box.curselection()
        if sel:
            text = self.device_box.get(sel[0])
            serial = text.split("]")[-1].strip().split()[0]
            self.serial_live = serial
            self.serial_var.set(serial)

    def do_to_wireless(self) -> None:
        """USB -> 无线：tcpip + connect。"""
        self.run_bg(self._to_wireless_job, self._snapshot())

    def _to_wireless_job(self, st: dict) -> None:
        dev = U.pick_device(st["serial"] or None, adb_path=self.adb_path,
                            prefer_wireless=False)
        if dev.is_wireless:
            self.log(f"当前已是无线连接：{dev.serial}", "info")
            self.events.put(("serial", dev.serial))
            return
        ip = st["ip"] or U.get_device_ip(dev.serial, adb_path=self.adb_path)
        if not ip:
            # 弹窗必须回到 UI 线程执行
            ip = (self._ask_main(simpledialog.askstring, "需要设备 IP",
                                 "无法自动获取，请手动输入设备 IP：") or "").strip()
        if not ip:
            self.log("已取消：缺少设备 IP。", "warn")
            return
        try:
            port = int(st["port"] or 5555)
        except ValueError:
            port = 5555
        self.log(f"正在开启 TCP 模式（端口 {port}）…")
        U.enable_tcpip(dev.serial, port=port, adb_path=self.adb_path)
        addr = f"{ip}:{port}"
        ok, text = U.connect(addr, adb_path=self.adb_path)
        self.log(f"adb connect {addr} -> {text}", "ok" if ok else "err")
        if ok and U.wait_for_device(addr, timeout=20, adb_path=self.adb_path):
            self.events.put(("serial", addr))
            info = U.get_device_info(addr, adb_path=self.adb_path)
            self.log(f"无线就绪：{info.get('brand')} {info.get('model')} "
                     f"Android {info.get('android')}。现在可拔掉 USB。", "ok")
            self._refresh_devices()
        else:
            self.log("无线连接未就绪，请检查 WiFi 与授权弹窗。", "err")

    def do_pair(self) -> None:
        """Android 11+ 配对。"""
        address = simpledialog.askstring(
            "无线配对", "请输入手机「无线调试-使用配对码配对设备」中显示的\nIP:端口（配对端口）：")
        if not address:
            return
        code = simpledialog.askstring("无线配对", "请输入 6 位配对码：")
        if not code:
            return
        self.run_bg(self._pair_job, address.strip(), code.strip())

    def _pair_job(self, address: str, code: str) -> None:
        ok, text = U.pair(address, code, adb_path=self.adb_path)
        self.log(f"adb pair {address} -> {text}", "ok" if ok else "err")
        if not ok:
            self.log("配对失败：请确认配对码未过期、端口正确、手机与电脑同一局域网。", "err")
            return
        conn = (self._ask_main(
            simpledialog.askstring, "连接端口",
            f"配对成功。请输入「无线调试」主页显示的连接端口地址\n"
            f"（回车默认使用 {address}）：") or address).strip()
        ok2, text2 = U.connect(conn, adb_path=self.adb_path)
        self.log(f"adb connect {conn} -> {text2}", "ok" if ok2 else "err")
        if ok2 and U.wait_for_device(conn, timeout=20, adb_path=self.adb_path):
            self.events.put(("serial", conn))
            self._refresh_devices()

    def do_connect(self) -> None:
        """仅连接。"""
        address = self.ip_var.get().strip()
        if address and ":" not in address:
            address = f"{address}:{self.port_var.get() or 5555}"
        if not address:
            address = simpledialog.askstring("连接设备", "请输入设备 IP:端口：")
        if not address:
            return
        self.run_bg(self._connect_job, address.strip())

    def _connect_job(self, address: str) -> None:
        ok, text = U.connect(address, adb_path=self.adb_path)
        self.log(f"adb connect {address} -> {text}", "ok" if ok else "err")
        if ok and U.wait_for_device(address, timeout=20, adb_path=self.adb_path):
            self.events.put(("serial", address))
            info = U.get_device_info(address, adb_path=self.adb_path)
            self.log(f"已连接：{info.get('brand')} {info.get('model')} "
                     f"Android {info.get('android')}", "ok")
            self._refresh_devices()

    def do_disconnect(self) -> None:
        self.run_bg(self._disconnect_job, self.serial_var.get().strip())

    def _disconnect_job(self, serial: str) -> None:
        U.disconnect(serial or None, adb_path=self.adb_path)
        self.log("已断开无线连接。", "info")
        self._refresh_devices()

    def do_discover(self) -> None:
        self.run_bg(self._discover_job)

    def _discover_job(self) -> None:
        self.log("正在通过 mDNS 发现同网段无线调试设备…")
        found = U.discover_mdns(adb_path=self.adb_path)
        if not found:
            self.log("未发现设备，请确认「无线调试」已开启且在同一 WiFi。", "warn")
            return
        for addr in found:
            self.log(f"发现：{addr}", "ok")
        self.events.put(("serial", found[0]))
        # 控件回写一律交给主线程
        self.events.put(("fields", {"ip_var": found[0].split(":")[0],
                                    "port_var": found[0].split(":")[1]}))

    def do_get_ip(self) -> None:
        self.run_bg(self._get_ip_job, self.serial_var.get().strip())

    def _get_ip_job(self, serial: str) -> None:
        dev = U.pick_device(serial or None, adb_path=self.adb_path)
        ip = U.get_device_ip(dev.serial, adb_path=self.adb_path)
        if ip:
            self.events.put(("fields", {"ip_var": ip}))
            self.log(f"设备 IP：{ip}", "ok")
        else:
            self.log("无法自动获取 IP，请手动填写。", "warn")

    # ----------------------------------------------------------- 选择路径 --
    def ask_device_path(self, title: str, initial: str = "/sdcard",
                        want_files: bool = True) -> Optional[str]:
        """弹出设备目录浏览器（只能在主线程调用）。返回选中的设备路径或 None。"""
        if not self.serial_live:
            messagebox.showwarning("未连接设备", "请先连接设备，然后再选择设备路径。")
            return None
        if not self.adb_path:
            messagebox.showwarning("未就绪", "尚未定位到 adb，请稍候再试。")
            return None
        dlg = DevicePathDialog(self.root, self.adb_path, self.serial_live,
                               title=title, initial=initial or "/sdcard",
                               want_files=want_files)
        self.root.wait_window(dlg)
        return dlg.result

    def pick_src_file(self) -> None:
        """源路径：push 选本地文件，pull 选设备上的文件。"""
        if self.dir_var.get() == "pull":
            path = self.ask_device_path("选择设备上的文件",
                                        initial=self.src_var.get().strip() or "/sdcard",
                                        want_files=True)
        else:
            path = filedialog.askopenfilename(title="选择要推送的文件")
        if path:
            self.src_var.set(path)

    def pick_src_dir(self) -> None:
        """源路径：push 选本地文件夹，pull 选设备上的文件夹。"""
        if self.dir_var.get() == "pull":
            path = self.ask_device_path("选择设备上的文件夹",
                                        initial=self.src_var.get().strip() or "/sdcard",
                                        want_files=False)
        else:
            path = filedialog.askdirectory(title="选择要推送的文件夹")
        if path:
            self.src_var.set(path)

    def pick_dst(self) -> None:
        """目标路径：push 选设备目录，pull 选本地目录。"""
        if self.dir_var.get() == "pull":
            path = filedialog.askdirectory(title="选择本地目标文件夹")
        else:
            path = self.ask_device_path("选择设备目标目录",
                                        initial=self.dst_var.get().strip() or "/sdcard",
                                        want_files=False)
        if path:
            self.dst_var.set(path)

    def pick_saved_target(self) -> None:
        """从配置中的常用路径里挑一个（按方向给不同的默认候选）。"""
        try:
            import json
            if os.path.isfile(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fp:
                    targets = json.load(fp).get("targets", [])
            else:
                targets = []
        except Exception:  # noqa: BLE001
            targets = []
        defaults = (["/sdcard/Download/", "/sdcard/PC_GAME/", "/sdcard/Documents/", "/sdcard/"]
                    if self.dir_var.get() == "push"
                    else [os.path.expanduser("~\\Desktop"),
                          os.path.expanduser("~\\Downloads"),
                          "D:\\"])
        targets = targets or defaults
        choice = simpledialog.askstring(
            "选择路径", "常用路径（输入编号）：\n" +
            "\n".join(f"{i + 1}. {t}" for i, t in enumerate(targets)))
        if not choice:
            return
        try:
            self.dst_var.set(targets[int(choice) - 1])
        except (ValueError, IndexError):
            if os.path.isabs(choice) or choice.startswith("/"):
                self.dst_var.set(choice)

    def save_target(self) -> None:
        import json
        cfg = {}
        if os.path.isfile(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fp:
                    cfg = json.load(fp)
            except Exception:  # noqa: BLE001
                cfg = {}
        cfg.setdefault("targets", [])
        tgt = self.dst_var.get().strip()
        if tgt and tgt not in cfg["targets"]:
            cfg["targets"].append(tgt)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
            json.dump(cfg, fp, ensure_ascii=False, indent=2)
        self.log(f"已保存目标目录：{tgt}", "ok")

    # -------------------------------------------------------------- 传输 --
    def _on_direction(self) -> None:
        """方向切换时更新按钮文案，并给出合理的默认目标路径。"""
        pull = (self.dir_var.get() == "pull")
        dst = self.dst_var.get().strip()
        if pull:
            self.start_btn.configure(text="开始拉取")
            self.src_file_btn.configure(text="设备文件")
            self.src_dir_btn.configure(text="设备目录")
            self.dst_browse_btn.configure(text="本地目录…")
            # 目标原本是设备路径（以 / 开头）或为空时，换成桌面
            if not dst or dst.startswith("/"):
                self.dst_var.set(os.path.expanduser("~\\Desktop"))
        else:
            self.start_btn.configure(text="开始推送")
            self.src_file_btn.configure(text="本地文件")
            self.src_dir_btn.configure(text="本地文件夹")
            self.dst_browse_btn.configure(text="设备目录…")
            if not dst or not dst.startswith("/"):
                self.dst_var.set("/sdcard/Download/")

    def start_push(self) -> None:
        st = self._snapshot()            # 在主线程一次性读取界面状态
        if not st["src"]:
            messagebox.showerror("错误", "请填写源路径。")
            return
        if not st["dst"]:
            messagebox.showerror("错误", "请填写目标路径。")
            return
        # 方向与路径类型必须匹配，否则会得到「源为空或不可读」这类难懂的报错
        if st["direction"] == "pull":
            if not st["src"].startswith("/"):
                messagebox.showerror(
                    "路径不对",
                    "「拉取 设备→本地」时，源路径必须是**设备上**的路径（以 / 开头），\n"
                    "例如 /sdcard/Download。\n\n"
                    "请用「设备文件 / 设备目录」按钮选择，而不是本地文件对话框。")
                return
        else:
            if not os.path.exists(st["src"]):
                messagebox.showerror("错误", "本地源路径不存在。")
                return
            if not st["dst"].startswith("/"):
                messagebox.showerror(
                    "路径不对",
                    "「推送 本地→设备」时，目标路径必须是**设备上**的路径（以 / 开头），\n"
                    "例如 /sdcard/Download。\n\n"
                    "请用「设备目录…」按钮选择。")
                return
        self.cancel_requested = False
        self.pb_var.set(0)
        self.prog_var.set("—")
        self.speed_var.set("—")
        self.cur_var.set("准备中…")
        self.run_bg(self._push_job, st)

    def cancel_push(self) -> None:
        self.cancel_requested = True
        self.log("已请求取消，当前文件传输会立即中断，下次运行自动续传…", "warn")

    def _push_job(self, st: dict) -> None:
        """后台传输任务。界面状态一律取自 st（主线程快照），不直接读 Tk 变量。"""
        serial = st["serial"] or None
        dev = U.pick_device(serial, adb_path=self.adb_path)
        info = U.get_device_info(dev.serial, adb_path=self.adb_path)
        self.events.put(("serial", dev.serial))
        self.log(f"目标设备：{info.get('brand')} {info.get('model')} "
                 f"Android {info.get('android')}｜{dev.serial}", "info")

        def on_log(msg: str) -> None:
            tag = ""
            if msg.startswith("[失败") or msg.startswith("[错误"):
                tag = "err"
            elif msg.startswith("[跳过") or msg.startswith("[警告") or msg.startswith("[校验"):
                tag = "warn"
            elif msg.startswith("[完成"):
                tag = "ok"
            self.log(msg, tag)          # log() 内部会同步刷新状态字段

        def on_progress(done, total, dbytes, tbytes, pct, speed=0.0):
            pct_total = int(dbytes / tbytes * 100) if tbytes else pct
            remain = max(0, tbytes - dbytes)
            eta = (remain / speed) if (speed > 0 and remain > 0) else None
            self.events.put((
                "progress", pct_total,
                f"{done}/{total} 个文件｜{U._human(dbytes)}/{U._human(tbytes)}",
                f"{U._human(int(speed))}/s｜剩余 {U.human_duration(eta)}"))

        def reconnect() -> Optional[str]:
            addr = self.serial_live          # 主线程维护的镜像，可安全读取
            if ":" in addr:
                ok, text = U.connect(addr, adb_path=self.adb_path)
                self.log(f"[重连] {text}", "info")
                if ok and U.wait_for_device(addr, timeout=15, adb_path=self.adb_path):
                    return addr
            for cand in U.discover_mdns(adb_path=self.adb_path):
                ok, text = U.connect(cand, adb_path=self.adb_path)
                self.log(f"[重连] {cand} -> {text}", "info")
                if ok and U.wait_for_device(cand, timeout=10, adb_path=self.adb_path):
                    return cand
            return None

        try:
            jobs = max(1, int(st["jobs"]))
        except (TypeError, ValueError):
            jobs = T.DEFAULT_JOBS

        stats = T.sync_path(
            source=st["src"], target=st["dst"],
            serial=dev.serial, adb_path=self.adb_path,
            direction=st["direction"],
            mode=st["mode"], verify=st["verify"],
            resume=st["resume"],
            jobs=jobs,
            dry_run=st["dry_run"],
            on_log=on_log, on_progress=on_progress, reconnect=reconnect,
            cancel_flag=lambda: self.cancel_requested,
        )
        self.events.put(("progress", 100, stats.summary(), None))
        self.events.put(("status", "传输结束：" + stats.summary()))
        level = "ok" if not (stats.failed or stats.verify_failed) else "err"
        self.log("传输结束：" + stats.summary(), level)
        self.events.put(("info", "传输结束\n\n" + stats.summary()))


def main() -> None:
    root = tk.Tk()
    AdbPushGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
