# ADB 无线推送工具（Windows）

**中文** | [English](README_EN.md)

用 ADB 无线调试把本地文件或文件夹推送到安卓设备，也支持反向拉取。
纯 Python 标准库 + Tkinter 界面，不需要 `pip install`。

**不用敲任何命令，不输 adb 命令也能用。** 免安装包（**Python 和 ADB 都已内置**）解压后双击 `adb-push-gui.exe` 即可；
源码方式则双击 `启动GUI.bat`。打开图形界面后选好文件、点「开始」就行 —— 手机目录也是在界面上点着选。
命令行参数只是给愿意用的人准备的。

> **个人使用完全免费，无需申请授权。**
> 个人学习、研究、业余项目、非营利组织都可以自由使用和修改，不受任何限制。
> 只有**商业用途**（出售、付费服务、集成进商业产品等）需要先联系作者取得授权 —— 详见文末「许可证」。

## 下载

| 方式 | 适合 | 说明 |
| --- | --- | --- |
| **免安装包** `adb-push-v1.0.0-win64.zip` | 普通用户 | 已内置 exe **和 ADB**，不需要 Python、不需要单独装 adb，**解压即用** |
| **源码包** `adb-push-v1.0.0.zip` / 直接 clone | 想改代码 | 需要 Python 3.10+ 和自备 ADB |

都在 [Releases](https://github.com/0701QYhong/adb-push/releases/latest) 页面。

## 特性

- **断点续传**：push / pull 都支持，中断后从断点继续
- **双校验**：传完比对大小 + 哈希，不一致直接报错
- **差异同步**：只传目标端缺失或内容不一致的文件
- **双向**：`push` 本地 → 设备，`pull` 设备 → 本地
- **并行**：文件级 + 分块级，实测最高 7.2×
- **无线**：mDNS 自动发现、配对码配对、断线自动重连
- **图形界面**：设备、方向、进度、日志全程可视化

<!-- 界面截图：把图存为 docs/screenshot-gui.png 后取消下面一行的注释 -->
<!-- ![界面](docs/screenshot-gui.png) -->

## 依赖

- **免安装包：什么都不用装。** ADB 已随包内置，解压到任意目录，双击 `adb-push-gui.exe` 即可。
- **源码方式**：需要 **Python 3.10+**（不需要任何第三方包），以及自备 **ADB**：<https://developer.android.com/tools/releases/platform-tools>

源码方式下，adb 按以下顺序自动查找：`--adb` 参数 → 环境变量 `ADB_PATH` → 系统 `PATH` → 程序所在目录及其上级目录下的 `platform-tools/`。

> 最省事的做法：把解压出来的 `platform-tools` 文件夹直接放到 exe 旁边，程序会自动找到它。
>
> 免安装包内的 `platform-tools` 为 Google 官方原样分发，版权与许可见包内 `platform-tools/NOTICE.txt`。

## 快速开始

解压免安装包后双击 `adb-push-gui.exe`；源码方式则双击 `启动GUI.bat`。也可以直接用命令行：

```bash
# 自动发现并连接设备（USB 或同 WiFi 的无线调试）
python adb_push.py --auto

# 推送（差异同步 + 双校验 + 断点续传，都是默认行为）
python adb_push.py --source "D:\我的文件" --target "/sdcard/Download/我的文件"

# 拉取
python adb_push.py --pull --source "/sdcard/DCIM/Camera" --target "D:\照片备份"
```

Android 11+ 无线调试首次使用需配对：手机打开「开发者选项 → 无线调试 → 使用配对码配对设备」，然后执行 `python adb_push.py --pair`。
Android 10 及以下先用 USB 执行 `python adb_push.py --to-wireless` 切换，之后即可拔线。

## 参数

| 参数 | 说明 |
| --- | --- |
| `--source, -s` | 源路径（push 为本地，pull 为设备） |
| `--target, -t` | 目标路径（push 为设备目录，pull 为本地目录） |
| `--direction` | `push`（默认）\| `pull` |
| `--pull` | 等价于 `--direction pull` |
| `--mode` | `diff`（默认，缺失或哈希不一致才传）\| `overwrite` \| `skip-existing` \| `newer` |
| `--verify` | `hash`（默认，大小+哈希）\| `size` \| `none` |
| `--hash-algo` | `md5` \| `sha256` \| `sha1`，默认自动探测 |
| `--jobs, -j` | 并发上限，默认 4（`1` 关闭并行） |
| `--chunk-size` | 分片大小 MB，默认 8，决定续传粒度 |
| `--parallel-threshold` | 单文件超过该大小（MB，默认 64）才分块并行 |
| `--no-resume` | 关闭断点续传 |
| `--no-cache` | 不使用哈希清单缓存 |
| `--dry-run` | 只打印计划，不传输 |
| `--serial` | 指定设备序列号（多设备时用） |
| `--address, -a` | 无线设备地址 `IP:端口` |
| `--pair-code` / `--connect-address` | 配对码 / 配对后 `adb connect` 的地址 |
| `--port` | tcpip / connect 端口，默认 `5555` |
| `--adb` | 指定 adb 可执行文件路径 |
| `--auto` | 自动发现并连接设备 |
| `--devices` / `--discover` | 列出设备 / mDNS 发现设备 |
| `--pair` / `--connect` / `--to-wireless` | 无线配对 / 仅连接 / USB 转无线 |
| `--config` | 配置文件路径，默认 `config.json` |
| `--batch` / `--save-config` | 批量执行 / 保存配置 |
| `--quiet` | 精简输出 |

## 配置文件

复制 `config.example.json` 为 `config.json`，填好后批量执行：

```bash
python adb_push.py --batch --config config.json
```

字段说明见 `config.example.json` 内的注释。

## 实现要点

- **push 续传**：本地按 `--chunk-size` 分片，每片独立 `adb push` 成 `<目标目录>/.adbp/<key>/NNNNN.part`，全部就绪后设备端一次 `cat` 合并。合并**先在设备端确认大小正确才改名成最终文件**，校验失败不会破坏目标文件。
- **pull 续传**：`adb exec-out tail -c +N` 按精确字节偏移追加到本地 `.part`；大文件用 `dd` 分块并行下载后本地拼接。
- **差异比对**：远端整目录一次 `md5sum`，本地多线程计算，两侧都按 `size+mtime` 缓存，文件没变时重复同步几乎零开销。
- **并发上限**：`file_workers × chunk_workers ≤ jobs`，任意时刻并发 adb 连接数不超过 `--jobs`。
- **符号链接**：远端枚举一律 `find -H`，否则看不到 `/sdcard` 里的文件（它本身是符号链接）。
- **暂存目录**：`.adbp/` 传输完成后自动清理。改动 `--chunk-size` 会让该文件从头重传（分片偏移变了，刻意如此）。

## 常见问题

| 现象 | 解决 |
| --- | --- |
| `Python was not found ... Microsoft Store` | 装的是应用商店占位别名。安装真正的 Python 3.10+，或到「设置 → 应用 → 高级应用设置 → 应用执行别名」关闭 `python.exe` |
| `未找到 adb 可执行文件` | 未安装或未配置，见上方「依赖」 |
| `未检测到可用设备` | 运行 `--auto`；或手机确认授权弹窗后执行 `--to-wireless` / `--pair` |
| `unauthorized` | 手机上点「允许调试」，必要时 `adb kill-server` 后重连 |
| `device offline` | 无线不稳。工具会自动重连，仍失败则 `--connect` |
| `cannot connect to <IP>:5555` | 手机与电脑需同一 WiFi，放行 5555，关闭 VPN |
| `connect failed: ECONNREFUSED` | 未先切到 TCP 模式，先用 USB 执行 `--to-wireless` |
| `Permission denied` | 目标目录需要 root，改推 `/sdcard/...` |
| `合并后大小不符` | 分片不全，重跑即可（会重传缺失分片，目标文件未被覆盖） |
| 设备端出现 `.adbp/` 或 `*.part` | 传输中断的暂存，正常现象，重跑自动续传并清理 |

## 许可证

[PolyForm Noncommercial License 1.0.0](LICENSE)

- **免费、无需授权**：个人学习、研究、测试、业余项目、慈善/教育/公共研究/政府等非营利机构，都可以自由使用、修改和分发。
- **需要授权**：任何商业用途，包括出售本工具或基于它的产品、作为付费服务的一部分、集成进商业软件等。

要商业授权请开一个 [Issue](https://github.com/0701QYhong/adb-push/issues) 联系作者，一般不会为难。
