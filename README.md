# adb-push

Windows ↔ 安卓 文件传输。两条通道：**无线 adb** 和 **纯局域网（网卡直连）**，不经过任何中转服务器。

仓库里有三个版本，传输内核和参数一致：

| 版本 | 位置 | 启动 | 说明 |
| --- | --- | --- | --- |
| Python 版 | 仓库根目录 | 双击 `启动.bat` | 功能基准版，改 `config.json` 即可定制 |
| WinUI3 版 | `winui/` | 双击 `启动WinUI.bat` | 原生 Windows 界面，首次自动编译，之后自包含运行 |
| Android 实验版 | `android/` | Android Studio 构建安装 | 手机可当服务端或客户端，和 Windows 走网卡互传 |

## 功能

**连接**
- mDNS 自动发现、配对码配对、USB 转无线、自动取设备 IP
- 网卡通道：UDP 广播发现对端，或直接填 `IP:端口`

**传输**
- 差异同步：先比大小，再比哈希，只传变化的文件
- 断点续传：分片落到 `.adbp` 暂存，中断后从断点继续
- 并行传输、传输后双校验、失败清单
- 文件 / 文件夹、推送 / 拉取、批量任务

**局域网**
- Windows 或安卓都能开共享目录，另一端直接读写
- 可选访问令牌，路径限制在共享目录内（拒绝 `..`）
- 协议：HTTP + UDP 发现，见 [`docs/LAN_PROTOCOL.md`](docs/LAN_PROTOCOL.md)

## Python 版

要求 Python 3.10+，只用标准库。

1. 把 `platform-tools` 放到本目录或上一级目录（`adb.exe` 会自动查找）
2. 双击 `启动.bat`

配置文件：复制 `config.example.json` 为 `config.json`。可改设备列表、常用目标路径、默认模式、并发数、分片大小、网卡端口/共享目录/令牌。

网卡直连：

```
启动.bat → 网卡直连 → 开启共享      # 本机共享给手机/另一台电脑
启动.bat → 对端地址 → 搜索/直连测试  # 连到对方共享
```

命令行：

```bat
:: 推送（adb）
python adb_push.py -s 192.168.1.8:5555 --source "D:\我的文件" --target /sdcard/Download

:: 拉取（adb）
python adb_push.py --pull --source /sdcard/DCIM/Camera --target "D:\照片"

:: 网卡推送 / 拉取
python adb_push.py --lan --lan-host 192.168.1.9 --source "D:\我的文件" --target Download
python adb_push.py --lan --lan-host 192.168.1.9 --pull --source Download/a.zip --target "D:\下载"

:: 本机开共享（供安卓版或另一台电脑读写）
python adb_push.py --lan-serve --lan-root "D:\share"

:: 搜索局域网内开了共享的设备
python adb_push.py --lan-discover
```

常用参数：`--mode diff|overwrite|skip-existing|newer`、`--verify hash|size|none`、`--jobs 4`、`--chunk-size 8`、`--no-resume`、`--dry-run`。

## WinUI3 版

要求 .NET SDK 8+ 与 Windows 10 1809+。

- 双击 `winui\启动WinUI.bat`：没有 `dist\YHGamePush` 时先编译，之后直接启动
- 手动构建：`powershell -ExecutionPolicy Bypass -File winui\build.ps1`
- 产物：`winui\dist\YHGamePush\`，自带 `platform-tools`，整个文件夹可直接分发（`exe` 不能单独拿走）

界面分两个标签页：`ADB 无线调试`（设备、配对、USB 转无线、远端路径浏览）和 `网卡直连`（本机共享、发现对端、连接测试）。传输参数、进度、日志在同屏。

WinUI3 版从自己的 `config.json` 读配置（首次运行后生成）。

## Android 实验版

零第三方依赖，纯 Java。

1. Android Studio 打开 `android/`，同步后安装
2. 允许「所有文件访问权限」
3. 点「开启共享」→ 手机变成 `IP:47820` 的可读写共享目录
4. 或在「电脑（对端）」填 Windows 的 IP，连接后上传/下载文件

App 被系统杀掉后共享会停止；不做相册/媒体库集成。详见 [`android/README.md`](android/README.md)。

## 目录结构

```
启动.bat                  Python 版入口
gui.py / adb_push.py      图形界面 / 命令行
adb_utils.py              adb 封装
transfer.py               adb 通道同步引擎
lan.py                    网卡通道引擎（服务端 + 客户端）
docs/LAN_PROTOCOL.md      YHGP/1 协议
winui/                    WinUI3 版（C#）
android/                  Android 实验版（Java）
```

## 注意

- ADB 通道需要手机开启「开发者选项 → 无线调试」并授权
- 首次无线连接失败时，先用 USB 连一次点「USB 转无线」
- 网卡通道必须同一局域网；防火墙放行 TCP 47820、UDP 47821
- 共享目录之外的路径会被拒绝，这是有意的

## 许可

MIT，见 [`LICENSE`](LICENSE)。
