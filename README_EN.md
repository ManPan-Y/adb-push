# ADB Wireless Push Tool (Windows)

[中文](README.md) | **English**

Push local files or folders to an Android device over ADB wireless debugging, and pull them back.
Pure Python standard library + a Tkinter GUI. No `pip install` required.

**No command line, no adb commands to remember.** Unzip and double-click `adb-push-gui.exe` (portable build)
or `启动GUI.bat` (source), then pick your files and hit start — the device folder is picked in the GUI too.
The CLI options are only there for people who want them.

> **Free for personal use — no permission needed.**
> Personal study, research, hobby projects and non-profit organizations may use and modify it freely.
> Only **commercial use** (reselling, paid services, integration into a commercial product, etc.)
> requires asking the author for a license — see "License" at the end.

## Download

| Package | For | Notes |
| --- | --- | --- |
| **`adb-push-v1.0.0-win64.zip`** (portable) | Most users | Bundled `.exe`, **no Python needed**, just unzip and run |
| **`adb-push-v1.0.0.zip`** / clone | Developers | Requires Python 3.10+ |

Both are on the [Releases](https://github.com/0701QYhong/adb-push/releases/latest) page. Either way you still need **ADB** (below).

## Features

- **Resumable transfer**: works for both push and pull; interrupted transfers continue from where they stopped
- **Dual verification**: compares both size and hash after transfer, and reports a clear error on mismatch
- **Diff sync**: only transfers files that are missing or whose content differs on the target
- **Bidirectional**: `push` (local → device) and `pull` (device → local)
- **Parallel**: file-level and chunk-level; up to 7.2× measured speedup
- **Wireless**: mDNS auto-discovery, pairing-code pairing, automatic reconnect
- **GUI**: devices, direction, progress and logs all visible in one window

<!-- Screenshot: save it as docs/screenshot-gui.png, then uncomment the line below -->
<!-- ![GUI](docs/screenshot-gui.png) -->

## Requirements

1. **ADB**: <https://developer.android.com/tools/releases/platform-tools>
2. **Python 3.10+** (source package only — skip this if you use the portable build). No third-party packages.

`adb` is located automatically in this order: the `--adb` argument → the `ADB_PATH` environment variable → the system `PATH` → a `platform-tools/` folder next to the program or in a parent directory.

> Easiest setup: drop the extracted `platform-tools` folder right next to the `.exe` — it will be found automatically.

## Quick start

Unzip the portable build and double-click `adb-push-gui.exe`; from source, double-click `启动GUI.bat`. Or use the command line:

```bash
# Discover and connect to a device (USB or wireless debugging on the same Wi-Fi)
python adb_push.py --auto

# Push (diff sync + dual verification + resume are all on by default)
python adb_push.py --source "D:\MyFiles" --target "/sdcard/Download/MyFiles"

# Pull
python adb_push.py --pull --source "/sdcard/DCIM/Camera" --target "D:\Photos"
```

For Android 11+ wireless debugging, pair once: open *Developer options → Wireless debugging → Pair device with pairing code* on the phone, then run `python adb_push.py --pair`.
On Android 10 and below, connect over USB and run `python adb_push.py --to-wireless`, then unplug the cable.

## Options

| Option | Description |
| --- | --- |
| `--source, -s` | Source path (local for push, device for pull) |
| `--target, -t` | Target path (device directory for push, local directory for pull) |
| `--direction` | `push` (default) \| `pull` |
| `--pull` | Same as `--direction pull` |
| `--mode` | `diff` (default: only missing or hash-mismatched files) \| `overwrite` \| `skip-existing` \| `newer` |
| `--verify` | `hash` (default: size + hash) \| `size` \| `none` |
| `--hash-algo` | `md5` \| `sha256` \| `sha1`; auto-detected by default |
| `--jobs, -j` | Max concurrency, default 4 (`1` disables parallelism) |
| `--chunk-size` | Chunk size in MB, default 8; determines resume granularity |
| `--parallel-threshold` | Only files larger than this (MB, default 64) use chunk-level parallelism |
| `--no-resume` | Disable resumable transfer |
| `--no-cache` | Do not use the hash manifest cache |
| `--dry-run` | Print the plan without transferring anything |
| `--serial` | Device serial (when several devices are connected) |
| `--address, -a` | Wireless device address `IP:port` |
| `--pair-code` / `--connect-address` | Pairing code / address for `adb connect` after pairing |
| `--port` | tcpip / connect port, default `5555` |
| `--adb` | Path to the adb executable |
| `--auto` | Discover and connect to a device automatically |
| `--devices` / `--discover` | List devices / discover devices over mDNS |
| `--pair` / `--connect` / `--to-wireless` | Pair wirelessly / connect only / switch USB to wireless |
| `--config` | Config file path, defaults to `config.json` |
| `--batch` / `--save-config` | Run batches / save settings to config |
| `--quiet` | Minimal output |

## Configuration file

Copy `config.example.json` to `config.json`, fill it in, then run a batch:

```bash
python adb_push.py --batch --config config.json
```

See the comments inside `config.example.json` for the field descriptions.

## Implementation notes

- **Push resume**: the local file is split into `--chunk-size` chunks, and each chunk is pushed separately as `<target>/.adbp/<key>/NNNNN.part`. Once all chunks are ready, the device concatenates them with a single `cat`. The merged file is renamed to its final path **only after the device confirms the correct size**, so a failed check never corrupts the target file.
- **Pull resume**: `adb exec-out tail -c +N` appends from an exact byte offset into a local `.part` file. Large files are downloaded in parallel via `dd` and assembled locally.
- **Diff comparison**: one `md5sum` over the whole remote directory, multi-threaded hashing locally, and a `size+mtime` cache on both sides. Re-syncing unchanged files costs almost nothing.
- **Concurrency bound**: `file_workers × chunk_workers ≤ jobs`, so the number of concurrent adb connections never exceeds `--jobs`.
- **Symlinks**: remote enumeration always uses `find -H`; without it, files under `/sdcard` are invisible (it is itself a symlink).
- **Staging directory**: `.adbp/` is cleaned up after a successful transfer. Changing `--chunk-size` makes that file restart from scratch (chunk offsets change — intentional).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Python was not found ... Microsoft Store` | That is the Store placeholder alias. Install real Python 3.10+, or turn off `python.exe` under *Settings → Apps → Advanced app settings → App execution aliases* |
| `未找到 adb 可执行文件` (adb not found) | ADB is missing or not configured; see Requirements above |
| `未检测到可用设备` (no device) | Run `--auto`, or accept the authorization prompt on the phone and use `--to-wireless` / `--pair` |
| `unauthorized` | Tap "Allow debugging" on the phone; if needed, `adb kill-server` and reconnect |
| `device offline` | Unstable wireless connection. The tool reconnects automatically; otherwise try `--connect` |
| `cannot connect to <IP>:5555` | Phone and PC must be on the same Wi-Fi, port 5555 allowed, VPN off |
| `connect failed: ECONNREFUSED` | Not in TCP mode yet; run `--to-wireless` over USB first |
| `Permission denied` | The target directory needs root; push to `/sdcard/...` instead |
| `合并后大小不符` (merged size mismatch) | Incomplete chunks; just run again (missing chunks are re-sent, the target file was not overwritten) |
| `.adbp/` or `*.part` appears on the device | Staging data from an interrupted transfer; harmless, the next run resumes and cleans up |

## License

[PolyForm Noncommercial License 1.0.0](LICENSE)

- **Free, no permission needed**: personal study, research, testing, hobby projects, and charitable / educational / public-research / government organizations may use, modify and distribute it freely.
- **License required**: any commercial use, including selling this tool or products based on it, using it as part of a paid service, or bundling it into commercial software.

For commercial licensing, just open an [issue](https://github.com/0701QYhong/adb-push/issues) — the author is generally flexible.
