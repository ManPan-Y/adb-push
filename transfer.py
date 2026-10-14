# -*- coding: utf-8 -*-
"""
transfer.py
===========
可续传 / 差异同步 / 并行加速的传输引擎（构建在 adb_utils 的底层原语之上）。

能力
----
* 双向传输
    - push：本地 → 设备
    - pull：设备 → 本地
* 双校验
    - 大小 + 哈希（设备端 md5sum / sha256sum / sha1sum，自动探测）
* 断点续传（双向）
    - push：分片推送 + 设备端追加/合并，part 始终是合法前缀
    - pull：`tail -c +N` / `dd skip count` 精确偏移，本地 .part 续传
* 并行加速（两级）
    - 文件级：多个文件同时传（线程池），主要吃掉 adb 进程启动开销
    - 分块级：单个大文件内部按分片并行（push 走设备端按序合并，
      pull 走 dd 范围读取后本地拼接）
* 差异同步
    - 只传「目标端缺失」或「哈希不一致」的文件；远端哈希清单本地缓存
* 实时速率
    - SpeedMeter 滑动窗口速率，随进度回调给调用方显示

设计约束（均经真机实测确认）
----------------------------
1. Windows 上 `adb shell` 的 stdin 管道不可靠 → 绝不通过 stdin 上传数据。
2. `adb push` 中断后设备端不留残留 → 续传必须靠「本地分片 + 设备端追加」。
3. `adb exec-out` 的外层参数**不经过 shell**（引号会变成文件名一部分）；
   需要 shell 时必须显式 `exec-out sh -c "<cmd>"`。
4. 并发上限由 `jobs` 统一约束：file_workers × chunk_workers ≤ jobs，
   避免二级并行相乘导致 adb server 过载。
"""

from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import json
import os
import posixpath
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import adb_utils as U

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

# 分片大小：太大则续传粒度粗，太小则 adb 调用过多。8MB 为均衡值。
DEFAULT_CHUNK = 8 * 1024 * 1024

# 超过该大小的单文件才启用「分块级并行」（小文件并行分片得不偿失）
DEFAULT_PARALLEL_THRESHOLD = 64 * 1024 * 1024

# 默认并发数
DEFAULT_JOBS = 4

# 本地哈希计算块大小
_HASH_BLOCK = 1024 * 1024

# 算法 -> 设备端命令
_HASH_TOOLS = {"md5": "md5sum", "sha1": "sha1sum", "sha256": "sha256sum"}

# 哈希清单缓存目录（与脚本同级，dot 目录，不污染用户目录）
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".adbp_cache")

# 需要比对哈希的文件数达到该值，就改用「整目录一次批量计算」。
# 逐个文件查询远端哈希是 O(文件数) 次 adb 往返，上千个文件会卡住十几分钟。
_BATCH_HASH_THRESHOLD = 8

# 设备端暂存目录名（放在目标目录下，隐藏且传输完自动清理）
STAGE_DIRNAME = ".adbp"

# 进度回调节流间隔（秒），避免高频回调淹没 GUI 事件队列
_PROGRESS_INTERVAL = 0.08

LogFn = Callable[[str], None]
FileFn = Callable[[str, str, str, str], None]
# (已完成文件数, 总文件数, 已完成字节, 总字节, 当前百分比, 当前速率 B/s)
ProgressFn = Callable[[int, int, int, int, int, float], None]


# --------------------------------------------------------------------------- #
# 速率计
# --------------------------------------------------------------------------- #

class SpeedMeter:
    """滑动窗口速率计（线程安全），用于显示「当前速度」。"""

    def __init__(self, window: float = 4.0):
        self.window = window
        self._lock = threading.Lock()
        self._samples: "collections.deque[Tuple[float, int]]" = collections.deque()

    def add(self, nbytes: int) -> float:
        """累计 nbytes 并返回最新速率（B/s）。"""
        now = time.monotonic()
        with self._lock:
            self._samples.append((now, nbytes))
            cutoff = now - self.window
            # 至少保留两个样本，便于计算瞬时速率
            while len(self._samples) > 2 and self._samples[0][0] < cutoff:
                self._samples.popleft()
            return self._rate_locked()

    def rate(self) -> float:
        with self._lock:
            return self._rate_locked()

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()

    def _rate_locked(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        t0 = self._samples[0][0]
        t1 = self._samples[-1][0]
        dt = t1 - t0
        if dt <= 0.001:
            return 0.0
        # 不计首样本：它的字节发生在窗口左侧之外，会低估速率
        total = sum(n for _, n in list(self._samples)[1:])
        return total / dt


class _DirRegistry:
    """
    并发安全的「远端目录只创建一次」注册表。

    大量文件分布在同一目录时，逐个文件调用 mkdir 会浪费掉大量 adb 往返
    （实测这会让 120 个小文件的串行耗时从 4s 涨到 26s）。这里按目录缓存，
    并用「每目录一把锁」保证同目录只建一次、不同目录仍可并行。
    """

    def __init__(self):
        self._guard = threading.Lock()
        self._locks: Dict[str, threading.Lock] = {}
        self._ready = set()

    def ensure(self, path: str, make: Callable[[str], None]) -> None:
        with self._guard:
            if path in self._ready:
                return
            lk = self._locks.setdefault(path, threading.Lock())
        with lk:
            with self._guard:
                if path in self._ready:
                    return
            make(path)
            with self._guard:
                self._ready.add(path)


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #

@dataclass
class SyncStats:
    """一次同步任务的统计结果。"""
    total: int = 0                # 需要传输的文件数
    transferred: int = 0          # 成功传输数
    resumed: int = 0              # 其中属于断点续传完成的数量
    skipped: int = 0              # 跳过数（一致 / 已存在）
    failed: int = 0               # 失败数
    verified: int = 0             # 校验通过数
    verify_failed: int = 0        # 校验失败数
    bytes_total: int = 0          # 待传输总字节
    bytes_done: int = 0           # 已传输字节
    failed_files: List[Tuple[str, str]] = field(default_factory=list)
    verify_errors: List[Tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        parts = ["总计 {}".format(self.total),
                 "传输 {}".format(self.transferred),
                 "跳过 {}".format(self.skipped)]
        if self.resumed:
            parts.append("续传 {}".format(self.resumed))
        parts.append("失败 {}".format(self.failed))
        if self.verified or self.verify_failed:
            parts.append("校验 {}/{}".format(self.verified, self.verify_failed))
        parts.append("传输量 {}/{}".format(U._human(self.bytes_done), U._human(self.bytes_total)))
        return "｜".join(parts)


# --------------------------------------------------------------------------- #
# 哈希
# --------------------------------------------------------------------------- #

def local_hash(path: str, algo: str = "md5", block: int = _HASH_BLOCK) -> Optional[str]:
    """计算本地文件哈希；读取失败返回 None。"""
    try:
        h = hashlib.new(algo)
    except ValueError:
        h = hashlib.md5()
    try:
        with open(path, "rb") as fp:
            for chunk in iter(lambda: fp.read(block), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def device_hash_algo(serial: str, adb_path: Optional[str] = None) -> str:
    """探测设备支持的哈希算法，优先 md5（最快且最普遍）。"""
    for algo in ("md5", "sha256", "sha1"):
        tool = _HASH_TOOLS[algo]
        code, out, _ = U.run_adb(["shell", "which", tool],
                                 serial=serial, adb_path=adb_path, timeout=15)
        if code == 0 and tool in out:
            return algo
    return "md5"


def remote_hash(serial: str, path: str, algo: str = "md5",
                adb_path: Optional[str] = None) -> Optional[str]:
    """读取远端单个文件的哈希；失败返回 None。"""
    tool = _HASH_TOOLS.get(algo, "md5sum")
    cmd = "{t} {p} 2>/dev/null".format(t=tool, p=U._sh_quote(path))
    _code, out, _err = U.run_adb(["shell", cmd], serial=serial, adb_path=adb_path, timeout=300)
    m = re.search(r"([0-9a-fA-F]{32,64})", out)
    return m.group(1).lower() if m else None


def remote_hash_map(serial: str, root: str, algo: str = "md5",
                    adb_path: Optional[str] = None,
                    on_count: Optional[Callable[[int], None]] = None
                    ) -> Dict[str, str]:
    """
    整目录批量读取哈希：只发一次 adb 调用（`find ... -exec md5sum +`）。

    这是「差异比对」避免 O(文件数) 次 adb 往返的关键。因为一次调用可能
    要读几十 GB，这里改为**流式**解析输出，并通过 on_count 回调已完成的
    文件数，便于调用方展示进度。
    """
    tool = _HASH_TOOLS.get(algo, "md5sum")
    cmd = ("find -H {r} -type f -not -path '*/.adbp/*' -exec {t} {{}} + 2>/dev/null"
           .format(r=U._sh_quote(root), t=tool))
    rootn = root.rstrip("/")
    hexlen = {"md5": 32, "sha1": 40, "sha256": 64}.get(algo, 32)
    pat = re.compile(r"\\?([0-9a-fA-F]{%d})[ \t]+(.+)$" % hexlen)
    result: Dict[str, str] = {}

    def handle(line: str) -> None:
        m = pat.match(line)
        if not m:
            return
        digest, p = m.group(1).lower(), m.group(2).strip()
        if p == rootn:
            rel = ""
        elif p.startswith(rootn + "/"):
            rel = p[len(rootn) + 1:]
        else:
            return
        result[rel] = digest
        if on_count:
            on_count(len(result))

    U.run_shell_stream(cmd, handle, serial=serial, adb_path=adb_path)
    return result


# --------------------------------------------------------------------------- #
# 清单枚举
# --------------------------------------------------------------------------- #

def list_local_files(root: str) -> Dict[str, Tuple[int, int]]:
    """列出 root 下所有文件：{相对 root 的路径: (size, mtime)}；root 为文件时键为 ""。"""
    result: Dict[str, Tuple[int, int]] = {}
    if os.path.isfile(root):
        st = os.stat(root)
        return {"": (st.st_size, int(st.st_mtime))}
    for cur, _dirs, files in os.walk(root):
        for name in files:
            fp = os.path.join(cur, name)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            rel = os.path.relpath(fp, root).replace(os.sep, "/")
            result[rel] = (st.st_size, int(st.st_mtime))
    return result


def list_remote_files(serial: str, root: str,
                      adb_path: Optional[str] = None) -> Dict[str, Tuple[int, int]]:
    """列出远端 root 下所有文件：{相对 root 的路径: (size, mtime)}；root 为文件时键为 ""。"""
    # -H：跟随命令行参数上的符号链接。/sdcard 本身是符号链接，
    # 不加 -H 时 toybox find 不会进入它，会被误判成空目录。
    cmd = ("find -H {r} -type f -not -path '*/.adbp/*' "
           "-printf '%s\\t%T@\\t%p\\n' 2>/dev/null").format(r=U._sh_quote(root))
    _code, out, _err = U.run_adb(["shell", cmd], serial=serial, adb_path=adb_path, timeout=600)
    rootn = root.rstrip("/")
    result: Dict[str, Tuple[int, int]] = {}
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        try:
            size = int(parts[0])
            mtime = int(float(parts[1]))
        except ValueError:
            continue
        p = parts[2]
        if p == rootn:
            rel = ""
        elif p.startswith(rootn + "/"):
            rel = p[len(rootn) + 1:]
        else:
            continue
        result[rel] = (size, mtime)
    return result


# --------------------------------------------------------------------------- #
# 哈希清单缓存
# --------------------------------------------------------------------------- #

class HashCache:
    """
    按「缓存键」持久化文件哈希，键为逻辑相对路径。

    以 size + mtime 作为有效性判据：二者未变即复用旧哈希，避免每次同步
    重算哈希（无论远端 md5sum 还是本地读盘都是纯开销）。

    远端一侧与本地一侧各用一份缓存，缓存键分别形如：
        remote__<设备>__<远端根>      local__<本地根>
    """

    def __init__(self, cache_key: str):
        safe = re.sub(r"[^0-9A-Za-z._-]", "_", cache_key)
        self.path = os.path.join(CACHE_DIR, safe[:150] + ".json")
        self.data: Dict[str, dict] = self._load()
        self.dirty = False
        # 本地哈希由线程池并行计算（见 _local_hash_cached），必须加锁
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key: str, size: int, mtime: int) -> Optional[str]:
        with self._lock:
            entry = self.data.get(key)
            if (entry and entry.get("size") == size
                    and entry.get("mtime") == mtime and entry.get("hash")):
                return entry["hash"]
        return None

    def put(self, key: str, size: int, mtime: int, digest: str) -> None:
        with self._lock:
            self.data[key] = {"size": size, "mtime": mtime, "hash": digest}
            self.dirty = True

    def save(self) -> None:
        with self._lock:
            if not self.dirty:
                return
            snapshot = dict(self.data)
            self.dirty = False
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(snapshot, fp, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            self.dirty = True      # 写盘失败，下次 save 再试


# --------------------------------------------------------------------------- #
# 分片工具
# --------------------------------------------------------------------------- #

def _n_chunks(size: int, chunk_size: int) -> int:
    return max(1, (size + chunk_size - 1) // chunk_size)


def _chunk_len(size: int, chunk_size: int, idx: int) -> int:
    """第 idx 个分片的期望字节数。"""
    return max(0, min(chunk_size, size - idx * chunk_size))


def _chunk_digits(n: int) -> int:
    """分片文件名的零填充宽度，保证字典序 == 数字序。"""
    return max(5, len(str(max(0, n - 1))))


def _push_chunk_once(local_slice: str, remote_chunk: str, serial: Optional[str],
                     adb_path: Optional[str], retries: int,
                     cancel_flag: Optional[Callable[[], bool]]) -> Tuple[bool, str]:
    """把本地切片推送到远端分片文件，带重试。"""
    last = ""
    for _ in range(retries + 1):
        code, text = U.push_file_fast(local_slice, remote_chunk,
                                      serial=serial, adb_path=adb_path)
        if code == 0 and "error" not in text.lower():
            return True, ""
        last = text.strip()[:200]
        if cancel_flag and cancel_flag():
            return False, "已取消"
        time.sleep(1.0)
    return False, last or "未知错误"


def _remote_chunk_sizes(serial: Optional[str],
                        cdir: str,
                        adb_path: Optional[str] = None) -> Optional[Dict[int, int]]:
    """
    一次 adb 调用取回暂存目录内所有分片的大小：`{分片索引: 字节数}`。

    逐片调用 `remote_size` 在超大文件（上万分片）下会产生同等数量的 adb
    往返，仅"盘点"就要数分钟；`wc -c <目录>/*.part` 一条命令即可拿全。
    解析失败一律返回 None，调用方自动回退到逐片查询，保证行为不退化。
    """
    try:
        code, out, _err = U.run_adb(
            ["shell", "wc -c {d}/*.part 2>/dev/null".format(d=U._sh_quote(cdir))],
            serial=serial, adb_path=adb_path, timeout=300)
    except Exception:                      # noqa: BLE001 - 任何异常都走回退路径
        return None
    if code != 0 or not out:
        return None
    sizes: Dict[int, int] = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(.+?)\s*$", line)
        if not m:
            continue                       # 例如 `wc` 的 `total` 汇总行
        name = posixpath.basename(m.group(2))
        if not name.endswith(".part"):
            continue
        stem = name[:-len(".part")]
        if stem.isdigit():
            sizes[int(stem)] = int(m.group(1))
    return sizes or None


# --------------------------------------------------------------------------- #
# 单文件：分片续传推送（本地 → 设备）
# --------------------------------------------------------------------------- #

def push_file_resumable(local: str,
                        remote: str,
                        serial: Optional[str] = None,
                        adb_path: Optional[str] = None,
                        chunk_size: int = DEFAULT_CHUNK,
                        resume: bool = True,
                        retries: int = 2,
                        on_bytes: Optional[Callable[[int], None]] = None,
                        cancel_flag: Optional[Callable[[], bool]] = None,
                        parallel_workers: int = 1,
                        parallel_threshold: int = DEFAULT_PARALLEL_THRESHOLD
                        ) -> Tuple[bool, str, bool]:
    """
    推送单个文件，支持断点续传与（大文件）分块并行。
    返回 (是否成功, 错误说明, 是否为续传完成)。

    统一采用「每分片独立文件 + 最后一次性合并」：

        暂存目录  <目标目录>/.adbp/<key>/NNNNN.part     （每片一个文件）
        1) 盘点：文件大小与期望一致的分片即为「已完成」，可跳过（分片级续传）
        2) 传片：逐片从本地按偏移切片 → `adb push` 到对应分片文件
                （size >= parallel_threshold 且 workers > 1 时多线程并行）
        3) 合并：`cat <key>/[0-9]*.part > <merge> && mv -f <merge> <remote>`
                （零填充命名保证字典序 == 数字序，一次 cat 完成，最后改名）

    为什么不再用「逐片追加到同一个 part」：
        * 追加需要每个分片额外一次 `adb shell cat`，分片数一多就是几百次
          adb 往返；合并只需 1 次。
        * 串行与并行共用同一套暂存布局，中途改 `--jobs` 也能继续续传。

    单分片快路径：文件 <= 一个分片时直接推最终路径 —— 此时续传粒度本就是
    整个文件，且 adb push 中断不留残留，走暂存毫无收益。
    """
    size = os.path.getsize(local)

    # 0 字节文件无法分片，直接普通推送
    if size == 0:
        code, text = U.push_file(local, remote, serial=serial, adb_path=adb_path)
        ok = code == 0 and "error" not in text.lower()
        return ok, ("" if ok else (text.strip()[:200] or "推送失败")), False

    # 单分片：直接推最终路径
    if size <= chunk_size:
        ok, err = _push_chunk_once(local, remote, serial, adb_path, retries, cancel_flag)
        if not ok:
            return False, err, False
        if on_bytes:
            on_bytes(size)
        return True, "", False

    # 多分片：统一走「每分片独立文件 + 一次合并」；未达阈值时不并行
    workers = parallel_workers if size >= parallel_threshold else 1
    return _push_file_by_chunks(
        local, remote, size, serial, adb_path, chunk_size, resume,
        retries, workers, on_bytes, cancel_flag)


def _push_file_by_chunks(local: str, remote: str, size: int,
                         serial: Optional[str], adb_path: Optional[str],
                         chunk_size: int, resume: bool, retries: int,
                         workers: int,
                         on_bytes: Optional[Callable[[int], None]],
                         cancel_flag: Optional[Callable[[], bool]]
                         ) -> Tuple[bool, str, bool]:
    """
    多分片推送：每分片一个独立文件，最后设备端一次 `cat` 合并。

    workers = 1 时退化为串行逐片推送，与并行共用同一套暂存布局与续传判定。
    """
    work = posixpath.join(posixpath.dirname(remote), STAGE_DIRNAME)
    # key 必须包含 chunk_size：否则用户中途改动分片大小后，「索引相同但字节
    # 偏移已不同」的旧分片会被误判为已完成，最终拼出内容错误的文件。
    key = hashlib.md5("{}|{}|{}".format(remote, size, chunk_size)
                      .encode("utf-8")).hexdigest()[:16]
    legacy = hashlib.md5("{}|{}".format(remote, size)
                         .encode("utf-8")).hexdigest()[:16]
    cdir = posixpath.join(work, key)
    U.remote_mkdirs(serial, cdir, adb_path=adb_path)

    n = _n_chunks(size, chunk_size)
    digits = _chunk_digits(n)

    def chunk_path(i: int) -> str:
        return posixpath.join(cdir, ("{:0%dd}.part" % digits).format(i))

    # 1) 盘点：文件大小与期望一致的分片即为「已完成」，可跳过（分片级续传）。
    #    关闭续传时一律重传，因此不把已存在的分片算作完成。
    known = _remote_chunk_sizes(serial, cdir, adb_path=adb_path)

    def current_size(i: int) -> Optional[int]:
        if known is not None:
            return known.get(i)
        return U.remote_size(serial, chunk_path(i), adb_path=adb_path)

    todo: List[int] = []
    done_bytes = 0
    for i in range(n):
        want = _chunk_len(size, chunk_size, i)
        cur = current_size(i)
        if resume and cur == want:
            done_bytes += want
            continue
        if cur is not None:
            U.remote_remove(serial, chunk_path(i), adb_path=adb_path)
        todo.append(i)

    resumed = done_bytes > 0
    if on_bytes and done_bytes:
        on_bytes(done_bytes)          # 已就绪的分片计入进度

    if todo:
        tmpdir = tempfile.mkdtemp(prefix="adbp_")
        try:
            def work_one(i: int) -> Tuple[bool, str]:
                if cancel_flag and cancel_flag():
                    return False, "已取消"
                want = _chunk_len(size, chunk_size, i)
                slice_path = os.path.join(tmpdir, "s{:0{w}d}.bin".format(i, w=digits))
                with open(local, "rb") as fp:
                    fp.seek(i * chunk_size)
                    data = fp.read(want)
                if len(data) != want:
                    return False, "读取本地分片失败"
                with open(slice_path, "wb") as out:
                    out.write(data)
                ok, err = _push_chunk_once(slice_path, chunk_path(i), serial,
                                           adb_path, retries, cancel_flag)
                if not ok:
                    return False, err
                if on_bytes:
                    on_bytes(want)
                return True, ""

            if workers > 1 and len(todo) > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                    results = list(ex.map(work_one, todo))
            else:
                results = [work_one(i) for i in todo]

            for ok, err in results:
                if not ok:
                    return False, ("已取消" if err == "已取消"
                                   else "分片传输失败：{}".format(err)), resumed
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # 2) 合并：按字典序（即数字序）cat 全部分片到临时文件，
    #    **先在设备端校验临时文件大小，通过后才改名成最终文件**。
    #    这样分片不完整时只会留下暂存文件，绝不会把目标文件写成坏内容。
    part = posixpath.join(work, key + ".merge")
    merge_cmd = (
        "rm -f {p}; cat {d}/[0-9]*.part > {p}; "
        'sz=$(wc -c < {p} | tr -d " "); '
        'if [ "$sz" != "{size}" ]; then echo "SIZE_MISMATCH:$sz"; exit 3; fi; '
        "mv -f {p} {r}; rm -rf {d} {old}"
    ).format(d=U._sh_quote(cdir), p=U._sh_quote(part), r=U._sh_quote(remote),
             size=size, old=U._sh_quote(posixpath.join(work, legacy)))
    code, out, err = U.run_adb(["shell", merge_cmd], serial=serial,
                               adb_path=adb_path, timeout=1800)
    if code != 0:
        detail = (out or err).strip()[:200]
        if "SIZE_MISMATCH:" in detail:
            got_sz = detail.split("SIZE_MISMATCH:", 1)[1].strip()
            return (False, "合并后大小不符（{} / {}），目标文件未被覆盖，重跑将重传缺失分片"
                    .format(got_sz, size), resumed)
        return (False, ("合并分片失败：{}".format(detail) if detail
                        else "合并分片失败（分片不完整）"), resumed)

    got = U.remote_size(serial, remote, adb_path=adb_path)
    if got != size:
        return False, "合并后大小不符（{} / {}）".format(got, size), resumed
    return True, "", resumed


# --------------------------------------------------------------------------- #
# 单文件：续传拉取（设备 → 本地）
# --------------------------------------------------------------------------- #

def pull_file_resumable(remote: str,
                        local: str,
                        serial: Optional[str] = None,
                        adb_path: Optional[str] = None,
                        chunk_size: int = DEFAULT_CHUNK,
                        resume: bool = True,
                        on_bytes: Optional[Callable[[int], None]] = None,
                        cancel_flag: Optional[Callable[[], bool]] = None,
                        parallel_workers: int = 1,
                        parallel_threshold: int = DEFAULT_PARALLEL_THRESHOLD
                        ) -> Tuple[bool, str, bool]:
    """
    从设备拉取单个文件到本地，支持精确偏移续传与（大文件）分块并行。
    返回 (是否成功, 错误说明, 是否为续传完成)。

    串行路径：`adb exec-out tail -c +N` 从 offset 流式读入本地 .part。
    并行路径：`exec-out sh -c "dd if=... bs=.. skip=i count=1"` 并行取各分片，
              本地按序拼接。以「分片」为单位续传。
    """
    rsize = U.remote_size(serial, remote, adb_path=adb_path)
    if rsize is None:
        return False, "无法读取远端文件大小（文件不存在或无权限）", False

    if parallel_workers > 1 and rsize >= parallel_threshold:
        return _pull_file_parallel_chunks(remote, local, rsize, serial, adb_path,
                                          chunk_size, resume, parallel_workers,
                                          on_bytes, cancel_flag)

    os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
    part = local + ".adbp.part"

    offset = os.path.getsize(part) if os.path.exists(part) else 0
    if not resume or offset > rsize:
        if os.path.exists(part):
            os.remove(part)
        offset = 0
    resumed = offset > 0

    if offset < rsize:
        # 注意：exec-out 外层不走设备 shell，参数按 argv 直接传递，
        # 因此这里必须传「原始路径」，绝不能加 POSIX 引号。
        proc = U.open_exec_out(["tail", "-c", "+{}".format(offset + 1), remote],
                               serial=serial, adb_path=adb_path)
        head = b""              # stdout 开头若干字节（极少数实现会把错误写这里）
        err_buf = bytearray()   # 远端报错实际走 stderr

        # 远端 tail 的报错只会出现在 stderr。若不持续读走 stderr，
        # 一旦写满管道缓冲（约 64KB）子进程会阻塞，导致拉取永久卡死。
        def _drain_stderr() -> None:
            try:
                while proc.stderr:
                    data = proc.stderr.read(4096)
                    if not data:
                        break
                    if len(err_buf) < 4096:
                        err_buf.extend(data[:4096 - len(err_buf)])
            except (OSError, ValueError):
                pass

        stderr_reader = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_reader.start()
        try:
            with open(part, "ab") as out:
                while True:
                    if cancel_flag and cancel_flag():
                        proc.kill()
                        proc.communicate()
                        return False, "已取消", resumed
                    buf = proc.stdout.read(256 * 1024)
                    if not buf:
                        break
                    if len(head) < 256:
                        head += buf[:256 - len(head)]
                    out.write(buf)
                    offset += len(buf)
                    if on_bytes:
                        on_bytes(len(buf))
            proc.wait()
        finally:
            stderr_reader.join(timeout=2.0)
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass

        got = os.path.getsize(part)

        def _discard(reason: str) -> Tuple[bool, str, bool]:
            """丢弃被错误文本污染或长度异常的 .part，避免续传拼出坏文件。"""
            try:
                if os.path.exists(part):
                    os.remove(part)
            except OSError:
                pass
            return False, reason, resumed

        err_text = bytes(err_buf)
        if err_text and any(m in err_text for m in (
                b"tail:", b"No such file", b"Permission denied",
                b"not found", b"Inaccessible", b"Invalid argument")):
            return _discard("远端读取失败：{}".format(
                err_text.split(b"\n")[0].decode("utf-8", "replace")))
        if b"tail:" in head or b"No such file" in head or b"Permission denied" in head:
            return _discard("远端读取失败：{}".format(
                head.split(b"\n")[0].decode("utf-8", "replace")))
        if got > rsize:
            return _discard("拉取数据超出预期长度（{} / {}）".format(got, rsize))
        if got != rsize:
            return False, "拉取不完整（{} / {} 字节）".format(got, rsize), resumed

    try:
        os.replace(part, local)
    except OSError as exc:
        return False, "无法写入本地文件：{}".format(exc), resumed
    return True, "", resumed


def _pull_file_parallel_chunks(remote: str, local: str, rsize: int,
                               serial: Optional[str], adb_path: Optional[str],
                               chunk_size: int, resume: bool, workers: int,
                               on_bytes: Optional[Callable[[int], None]],
                               cancel_flag: Optional[Callable[[], bool]]
                               ) -> Tuple[bool, str, bool]:
    """大文件分块并行拉取：各分片独立下载到本地目录，最后按序拼接。"""
    os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
    partdir = local + ".adbp.parts"
    os.makedirs(partdir, exist_ok=True)

    n = _n_chunks(rsize, chunk_size)
    digits = _chunk_digits(n)

    def part_path(i: int) -> str:
        return os.path.join(partdir, ("{:0%dd}.part" % digits).format(i))

    # 1) 盘点已完成分片
    todo: List[int] = []
    done_bytes = 0
    for i in range(n):
        want = _chunk_len(rsize, chunk_size, i)
        p = part_path(i)
        if resume and os.path.exists(p) and os.path.getsize(p) == want:
            done_bytes += want
            continue
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
        todo.append(i)

    resumed = done_bytes > 0
    if on_bytes and done_bytes:
        on_bytes(done_bytes)

    # 2) 并行下载各分片
    if todo:
        def work_one(i: int) -> Tuple[bool, str]:
            if cancel_flag and cancel_flag():
                return False, "已取消"
            want = _chunk_len(rsize, chunk_size, i)
            # dd 需要 shell；用 sh -c 显式起 shell，路径在内层做单引号转义
            cmd = "dd if={src} bs={bs} skip={skip} count=1 2>/dev/null".format(
                src=U._sh_quote(remote), bs=chunk_size, skip=i)
            try:
                data = U.exec_out_bytes(["sh", "-c", cmd], serial=serial,
                                        adb_path=adb_path, timeout=1800)
            except U.AdbError as exc:
                return False, str(exc)
            if len(data) != want:
                return False, "分片 {} 长度不符（{} / {}）".format(i, len(data), want)
            tmp = part_path(i) + ".tmp"
            with open(tmp, "wb") as out:
                out.write(data)
            os.replace(tmp, part_path(i))
            if on_bytes:
                on_bytes(want)
            return True, ""

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(work_one, todo))
        for ok, err in results:
            if not ok:
                return False, ("已取消" if err == "已取消" else "分片下载失败：{}".format(err)), resumed

    # 3) 按序拼接
    merge_tmp = local + ".adbp.part"
    try:
        with open(merge_tmp, "wb") as out:
            for i in range(n):
                with open(part_path(i), "rb") as fp:
                    shutil.copyfileobj(fp, out, 1024 * 1024)
        if os.path.getsize(merge_tmp) != rsize:
            raise OSError("拼接后大小不符")
        os.replace(merge_tmp, local)
    except OSError as exc:
        return False, "本地拼接失败：{}".format(exc), resumed

    shutil.rmtree(partdir, ignore_errors=True)
    return True, "", resumed


# --------------------------------------------------------------------------- #
# 高层编排：双向 / 差异 / 双校验 / 并行
# --------------------------------------------------------------------------- #

@dataclass
class _Item:
    """一个待处理文件：dest_key 决定目标端相对路径，within 决定源端相对路径。"""
    dest_key: str
    within: str
    src_path: str
    size: int
    mtime: int


def _join_local(root: str, within: str) -> str:
    return os.path.join(root, *within.split("/")) if within else root


def _dest_of(dest_root: str, dest_key: str, push: bool) -> str:
    """按方向拼接目标端完整路径（push→远端，pull→本地）。"""
    return posixpath.join(dest_root, dest_key) if push else _join_local(dest_root, dest_key)


def sync_path(source: str,
              target: str,
              serial: Optional[str] = None,
              adb_path: Optional[str] = None,
              direction: str = "push",
              mode: str = "diff",
              verify: str = "hash",
              hash_algo: Optional[str] = None,
              resume: bool = True,
              chunk_size: int = DEFAULT_CHUNK,
              jobs: int = DEFAULT_JOBS,
              parallel_threshold: int = DEFAULT_PARALLEL_THRESHOLD,
              dry_run: bool = False,
              use_cache: bool = True,
              on_log: Optional[LogFn] = None,
              on_file: Optional[FileFn] = None,
              on_progress: Optional[ProgressFn] = None,
              reconnect: Optional[Callable[[], Optional[str]]] = None,
              retries: int = 2,
              cancel_flag: Optional[Callable[[], bool]] = None) -> SyncStats:
    """
    双向 / 差异同步的编排入口，支持文件级并行与分块级并行。

    source    : direction="push" 时为本地路径；"pull" 时为设备路径
    target    : direction="push" 时为设备目录（如 /sdcard/Download）；
                "pull" 时为本地目录
    direction : "push"（本地→设备）| "pull"（设备→本地）
    mode      : "overwrite" / "skip-existing" / "diff"（默认）/ "newer"
    verify    : "none" | "size"（仅大小）| "hash"（大小 + 哈希，即双校验）
    hash_algo : 哈希算法；None 时自动探测设备能力
    resume    : 是否启用断点续传
    chunk_size: 分片大小（字节）
    jobs      : 并发上限。多文件时按文件并行；单文件（或文件数少于 jobs）时
                自动把剩余并发用于分块并行，保证 file_workers × chunk_workers ≤ jobs
    parallel_threshold : 单文件达到该大小才启用分块并行
    dry_run   : 只打印计划，不做任何实际传输
    use_cache : 是否复用远端哈希清单缓存
    """
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    def emit(rel: str, dest: str, action: str, detail: str) -> None:
        if on_file:
            on_file(rel, dest, action, detail)

    push = (direction == "push")
    algo = hash_algo or device_hash_algo(serial, adb_path=adb_path)
    jobs = max(1, int(jobs))

    # 1) 源：绝对路径 / 类型 / 清单
    if push:
        src_root = os.path.abspath(source)
        if not os.path.exists(src_root):
            raise U.AdbError("本地源路径不存在：{}".format(src_root))
        src_is_dir = os.path.isdir(src_root)
        base_name = os.path.basename(src_root.rstrip("\\/")) or src_root
        raw = list_local_files(src_root)
    else:
        src_root = source.rstrip("/") or "/"
        src_is_dir = U.remote_is_dir(serial, src_root, adb_path=adb_path)
        base_name = posixpath.basename(src_root) or "root"
        raw = list_remote_files(serial, src_root, adb_path=adb_path)

    if not raw:
        raise U.AdbError("源为空或不可读：{}".format(src_root))

    items: List[_Item] = []
    for within, (size, mtime) in raw.items():
        dest_key = "{}/{}".format(base_name, within) if (src_is_dir and within) else base_name
        if push:
            src_path = _join_local(src_root, within)
        else:
            src_path = posixpath.join(src_root, within) if within else src_root
        items.append(_Item(dest_key, within, src_path, size, mtime))

    log("方向：{}｜模式：{}｜校验：{}｜续传：{}｜并发：{}｜试运行：{}".format(
        "推送 本地→设备" if push else "拉取 设备→本地",
        mode,
        verify + ("（{}）".format(algo) if verify == "hash" else ""),
        "开" if resume else "关", jobs, "开" if dry_run else "关"))
    log("源：{}".format(src_root))
    log("目标：{}".format(target if push else os.path.abspath(target)))

    # 2) 目标清单（dest_key -> (size, mtime)）
    if push:
        # 目标为根目录 "/" 时 rstrip 会得到空串，会让 find / 路径拼接失效，
        # 因此显式回退为 "/"。
        dest_root = target.rstrip("/") or "/"
        dest_manifest = list_remote_files(serial, dest_root, adb_path=adb_path)
        hash_root = dest_root                     # 远端一侧 = 目标
    else:
        dest_root = os.path.abspath(target)
        dest_manifest = list_local_files(dest_root) if os.path.isdir(dest_root) else {}
        hash_root = src_root                      # 远端一侧 = 源

    # 3) 决策（分类单线程；哈希比对走批量/并行，见 _resolve_diff）
    remote_cache = (HashCache("remote__{}__{}".format(serial, hash_root))
                    if (use_cache and serial) else None)
    local_root = src_root if push else dest_root
    local_cache = HashCache("local__{}".format(local_root)) if use_cache else None

    plan: List[_Item] = []
    skipped = 0
    compare: List[Tuple[_Item, str, int, int]] = []   # 同大小 → 需比对内容

    for it in items:
        dest_path = _dest_of(dest_root, it.dest_key, push)
        dentry = dest_manifest.get(it.dest_key)
        if dentry is None:
            plan.append(it)
            continue
        d_size, d_mtime = dentry

        if mode == "overwrite":
            plan.append(it)
        elif mode == "skip-existing":
            skipped += 1
            emit(it.dest_key, dest_path, "skip", "目标端已存在")
        elif mode == "newer":
            if it.mtime > d_mtime:
                plan.append(it)
            else:
                skipped += 1
                emit(it.dest_key, dest_path, "skip", "目标端不更旧")
        elif it.size != d_size:
            plan.append(it)                       # 大小不同，必然不一致
        else:
            compare.append((it, dest_path, d_size, d_mtime))

    if compare:
        log("需比对内容：{} 个文件（与目标端同大小，逐一核对哈希）…".format(len(compare)))
        t0 = time.monotonic()
        same_flags = _resolve_diff(compare, push, algo, serial, adb_path,
                                   src_root, dest_root, remote_cache,
                                   local_cache, jobs, log)
        n_same = 0
        for (it, dest_path, _s, _m), is_same in zip(compare, same_flags):
            if is_same:
                n_same += 1
                skipped += 1
                emit(it.dest_key, dest_path, "skip", "哈希一致")
            else:
                plan.append(it)
        log("比对完成：一致 {}｜需传输 {}｜用时 {:.1f}s".format(
            n_same, len(compare) - n_same, time.monotonic() - t0))

    stats = SyncStats(total=len(plan), skipped=skipped)
    stats.bytes_total = sum(it.size for it in plan)
    log("共 {} 个文件，需传输 {} 个，合计 {}".format(
        len(items), len(plan), U._human(stats.bytes_total)))
    if remote_cache:
        remote_cache.save()
    if local_cache:
        local_cache.save()

    if not plan:
        log("目标端已是最新，无需传输。")
        log("——— " + stats.summary() + " ———")
        return stats

    # 4) 并发度分配：file_workers × chunk_workers ≤ jobs
    file_workers = min(jobs, len(plan))
    chunk_workers = max(1, jobs // file_workers)
    if chunk_workers > 1:
        log("并发分配：文件级 {} × 分块级 {}（单文件 ≥ {} 才分块并行）".format(
            file_workers, chunk_workers, U._human(parallel_threshold)))

    # 5) 逐文件传输（多线程）
    lock = threading.Lock()
    meter = SpeedMeter()
    state = {"done_files": 0, "done": 0, "cur": 0, "last_emit": 0.0}
    staged_dirs = set()
    dirs = _DirRegistry()

    def flush(cur_pct: Optional[int] = None, force: bool = False) -> None:
        now = time.monotonic()
        with lock:
            if not force and (now - state["last_emit"]) < _PROGRESS_INTERVAL:
                return
            state["last_emit"] = now
            done_bytes = state["done"] + state["cur"]
            if stats.bytes_total:
                done_bytes = min(done_bytes, stats.bytes_total)
            stats.bytes_done = done_bytes
            n_files = state["done_files"]
        _report(on_progress, n_files, stats.total, done_bytes, stats.bytes_total,
                cur_pct, meter.rate())

    reconnect_lock = threading.Lock()

    def safe_reconnect() -> Optional[str]:
        """并发下只允许一个线程真正重连，其余复用结果。"""
        if not reconnect:
            return None
        with reconnect_lock:
            return reconnect()

    def prepare_remote_dir(path: str) -> None:
        """
        每个远端目录只执行一次：建目录 + 清理旧版布局残留。

        旧版把分片暂存为 `<目标目录>/.adbp/<key>.part`（直接放在 .adbp 下），
        新版改放进 `<key>/NNNNN.part` 子目录，因此这里的 `*.part` 通配符
        （shell 的 `*` 不跨 `/`）只会命中旧版残留，不会误删新版分片。
        """
        U.remote_mkdirs(serial, path, adb_path=adb_path)
        stage = posixpath.join(path, STAGE_DIRNAME)
        U.run_adb(["shell", "rm -rf {s}/*.part {s}/*.chunk".format(s=U._sh_quote(stage))],
                  serial=serial, adb_path=adb_path, timeout=120)

    def process_one(it: _Item) -> None:
        if cancel_flag and cancel_flag():
            return
        with lock:
            state["done_files"] += 1
        dest_path = _dest_of(dest_root, it.dest_key, push)
        file_bytes = [0]

        def on_bytes(n: int) -> None:
            file_bytes[0] += n
            meter.add(n)
            with lock:
                state["cur"] += n
            flush()

        if dry_run:
            with lock:
                state["done"] += it.size
                stats.transferred += 1
            log("[试运行] {} -> {}".format(it.src_path, dest_path))
            emit(it.dest_key, dest_path, "push", "dry-run")
            flush(force=True)
            return

        # 确保目标父目录存在（按目录缓存，避免逐文件重复 mkdir）
        try:
            if push:
                parent = posixpath.dirname(dest_path)
                dirs.ensure(parent, prepare_remote_dir)
                if it.size > chunk_size:      # 只有走分片传输才会产生暂存目录
                    with lock:
                        staged_dirs.add(posixpath.join(parent, STAGE_DIRNAME))
            else:
                os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        except (U.AdbError, OSError) as exc:
            with lock:
                stats.failed += 1
                stats.failed_files.append((dest_path, "创建目录失败：{}".format(exc)))
            log("[失败] {}：{}".format(it.dest_key, exc))
            emit(it.dest_key, dest_path, "fail", str(exc))
            flush(force=True)
            return

        ok, err, was_resumed = _transfer_one(
            push, it.src_path, dest_path, serial, adb_path, chunk_size, resume,
            retries, on_bytes, cancel_flag, safe_reconnect, log,
            chunk_workers, parallel_threshold)

        # 无论成功失败，本文件都按完整大小结算，保证进度单调且不超 100%
        with lock:
            state["cur"] -= file_bytes[0]
            state["done"] += it.size
            if not ok:
                stats.failed += 1
                stats.failed_files.append((dest_path, err or "未知错误"))
            else:
                stats.transferred += 1
                if was_resumed:
                    stats.resumed += 1

        if not ok:
            log("[失败] {}：{}".format(it.dest_key, err or "未知错误"))
            emit(it.dest_key, dest_path, "fail", err or "未知错误")
            flush(force=True)
            return

        log("[完成] {}{}".format(it.dest_key, "（续传）" if was_resumed else ""))
        emit(it.dest_key, dest_path, "push", "续传完成" if was_resumed else "成功")

        if verify != "none":
            _verify(it, dest_path, push, verify, algo, serial, adb_path,
                    stats, lock, log, emit)
        flush(force=True)

    if file_workers > 1 and not dry_run:
        with concurrent.futures.ThreadPoolExecutor(max_workers=file_workers) as ex:
            list(ex.map(process_one, plan))
    else:
        for it in plan:
            if cancel_flag and cancel_flag():
                log("[已取消] 用户中止任务。")
                break
            process_one(it)

    # 6) 清理设备端空暂存目录
    if push and not dry_run:
        for d in staged_dirs:
            U.run_adb(["shell", "rmdir", U._sh_quote(d)],
                      serial=serial, adb_path=adb_path, timeout=20)

    log("——— " + stats.summary() + " ———")
    return stats


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #

def _report(on_progress: Optional[ProgressFn], done_files: int, total_files: int,
            done_bytes: int, total_bytes: int,
            cur_pct: Optional[int] = None, speed: float = 0.0) -> None:
    if on_progress:
        if cur_pct is None:
            cur_pct = int(done_bytes / total_bytes * 100) if total_bytes else 100
        pct = min(100, cur_pct) if total_bytes else 100
        on_progress(done_files, total_files, min(done_bytes, total_bytes),
                    total_bytes, pct, speed)


def _local_hash_cached(path: str, key: str, algo: str,
                       cache: Optional[HashCache]) -> Optional[str]:
    """本地文件哈希，带 size+mtime 缓存（未变则不重复读盘）。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    size, mtime = st.st_size, int(st.st_mtime)
    if cache:
        hit = cache.get(key, size, mtime)
        if hit:
            return hit
    digest = local_hash(path, algo)
    if digest and cache:
        cache.put(key, size, mtime, digest)
    return digest


def _resolve_diff(candidates: List[Tuple[_Item, str, int, int]],
                  push: bool, algo: str, serial: Optional[str],
                  adb_path: Optional[str], src_root: str, dest_root: str,
                  remote_cache: Optional[HashCache],
                  local_cache: Optional[HashCache],
                  jobs: int, log: LogFn) -> List[bool]:
    """
    对「源与目标同大小」的候选文件判断内容是否一致，返回等长的布尔列表。

    代价控制（这是本工具最容易被文件数量拖垮的一步）：
        * 远端一侧：先用 size+mtime 缓存；未命中数量达阈值时改为
          **整目录一次批量 `find -exec md5sum +`（仅 1 次 adb 调用）**，
          否则才逐文件查询。
        * 本地一侧：**多线程并行**计算，同样带 size+mtime 缓存。
    """
    n = len(candidates)
    if not n:
        return []

    remote_hash_by_key: Dict[str, Optional[str]] = {}
    misses: List[Tuple[_Item, str, int, int]] = []

    for it, _dest_path, d_size, d_mtime in candidates:
        # 远端一侧的有效性判据：push 时远端是目标，pull 时远端是源
        if push:
            key, size, mtime = it.dest_key, d_size, d_mtime
        else:
            key, size, mtime = it.dest_key, it.size, it.mtime
        hit = remote_cache.get(key, size, mtime) if remote_cache else None
        if hit:
            remote_hash_by_key[key] = hit
        else:
            misses.append((it, key, size, mtime))

    if misses:
        remote_root = dest_root if push else src_root
        rel_of = (lambda i: i.dest_key) if push else (lambda i: i.within)
        if len(misses) >= _BATCH_HASH_THRESHOLD:
            total = len(misses)
            log("  远端哈希：{} 个未命中缓存 → 整目录批量计算（仅 1 次 adb 调用，"
                "但需在设备上读完这些文件，请稍候）…".format(total))
            step = max(256, total // 10)
            tick = {"next": step}

            def _on_count(done: int) -> None:
                if done >= tick["next"]:
                    tick["next"] = done + step
                    log("    远端哈希进度：约 {}/{}…".format(min(done, total), total))

            table = remote_hash_map(serial, remote_root, algo, adb_path, on_count=_on_count)
            for it, key, size, mtime in misses:
                digest = table.get(rel_of(it))
                remote_hash_by_key[key] = digest
                if digest and remote_cache:
                    remote_cache.put(key, size, mtime, digest)
        else:
            for it, key, size, mtime in misses:
                path = posixpath.join(dest_root, it.dest_key) if push else it.src_path
                digest = remote_hash(serial, path, algo, adb_path)
                remote_hash_by_key[key] = digest
                if digest and remote_cache:
                    remote_cache.put(key, size, mtime, digest)
        if remote_cache:
            remote_cache.save()

    # 本地一侧：并行计算（带缓存）
    local_paths = [(it.src_path if push else _dest_of(dest_root, it.dest_key, False))
                   for it, _p, _s, _m in candidates]
    keys = [it.dest_key for it, _p, _s, _m in candidates]

    def worker(i: int) -> Optional[str]:
        return _local_hash_cached(local_paths[i], keys[i], algo, local_cache)

    log("  本地哈希：{} 个文件，{} 线程并行…".format(n, min(jobs, n)))
    if jobs > 1 and n > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(jobs, n)) as ex:
            local_hashes = list(ex.map(worker, range(n)))
    else:
        local_hashes = [worker(i) for i in range(n)]
    if local_cache:
        local_cache.save()

    return [remote_hash_by_key.get(keys[i]) is not None
            and remote_hash_by_key.get(keys[i]) == local_hashes[i]
            for i in range(n)]


def _transfer_one(push: bool, src_path: str, dest_path: str,
                  serial: Optional[str], adb_path: Optional[str], chunk_size: int,
                  resume: bool, retries: int, on_bytes: Callable[[int], None],
                  cancel_flag: Optional[Callable[[], bool]],
                  reconnect: Optional[Callable[[], Optional[str]]],
                  log: LogFn, chunk_workers: int,
                  parallel_threshold: int) -> Tuple[bool, str, bool]:
    """执行单文件传输，带连接类错误自动重连。"""
    attempt = 0
    last_err = ""
    while attempt <= retries:
        attempt += 1
        if push:
            ok, err, resumed = push_file_resumable(
                src_path, dest_path, serial=serial, adb_path=adb_path,
                chunk_size=chunk_size, resume=resume, retries=retries,
                on_bytes=on_bytes, cancel_flag=cancel_flag,
                parallel_workers=chunk_workers, parallel_threshold=parallel_threshold)
        else:
            ok, err, resumed = pull_file_resumable(
                src_path, dest_path, serial=serial, adb_path=adb_path,
                chunk_size=chunk_size, resume=resume,
                on_bytes=on_bytes, cancel_flag=cancel_flag,
                parallel_workers=chunk_workers, parallel_threshold=parallel_threshold)
        if ok:
            return True, "", resumed
        if err == "已取消":
            return False, err, resumed
        last_err = err
        if reconnect and U.is_connection_error(err) and attempt <= retries:
            log("检测到连接异常，尝试自动重连…")
            new_serial = reconnect()
            if new_serial:
                serial = new_serial
        time.sleep(1.0)
    return False, last_err, False


def _verify(it: _Item, dest_path: str, push: bool, verify: str, algo: str,
            serial: Optional[str], adb_path: Optional[str],
            stats: SyncStats, lock: threading.Lock, log: LogFn, emit: FileFn) -> None:
    """传输后校验：size 比大小；hash 比 大小 + 哈希（双校验）。"""
    d_size = (U.remote_size(serial, dest_path, adb_path=adb_path) if push
              else (os.path.getsize(dest_path) if os.path.exists(dest_path) else None))

    if d_size != it.size:
        with lock:
            stats.verify_failed += 1
            stats.verify_errors.append((dest_path, "大小不一致 源{} / 目标{}".format(it.size, d_size)))
        detail = "大小不一致 源{} / 目标{}".format(it.size, d_size)
        log("[校验] 不一致 {}（{}）".format(it.dest_key, detail))
        emit(it.dest_key, dest_path, "verify", detail)
        return

    if verify == "hash":
        if push:
            s_hash = local_hash(it.src_path, algo)
            d_hash = remote_hash(serial, dest_path, algo, adb_path)
        else:
            d_hash = local_hash(dest_path, algo)
            s_hash = remote_hash(serial, it.src_path, algo, adb_path)
        if not s_hash or not d_hash or s_hash != d_hash:
            detail = "哈希不一致 {} / {}".format(s_hash, d_hash)
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
