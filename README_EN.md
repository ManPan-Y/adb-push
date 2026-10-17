# adb-push

File transfer between Windows and Android over **wireless adb** or **plain LAN**. No relay server.

Three editions, same transfer core and same options:

| Edition | Location | Start | Notes |
| --- | --- | --- | --- |
| Python | repo root | double-click `启动.bat` | reference build, configurable via `config.json` |
| WinUI3 | `winui/` | double-click `启动WinUI.bat` | native UI, builds once, then self-contained |
| Android (experimental) | `android/` | build in Android Studio | phone acts as LAN server or client |

## Features

- Discovery: mDNS for adb, UDP broadcast for LAN, manual `IP:port`
- Diff sync (size + hash), resumable chunked transfer, parallel jobs, post-transfer verification
- Push / pull, file / folder, batch jobs
- LAN sharing on either side, optional access token, path sandboxing
- Configurable port, share dir, chunk size, concurrency

Protocol: [`docs/LAN_PROTOCOL.md`](docs/LAN_PROTOCOL.md)

## Python edition

Python 3.10+, standard library only.

```bat
启动.bat
python adb_push.py --lan --lan-host 192.168.1.9 --source "D:\stuff" --target Download
python adb_push.py --lan-serve --lan-root "D:\share"
python adb_push.py --lan-discover
```

Copy `config.example.json` to `config.json` to customize devices, targets, defaults and LAN settings.

## WinUI3 edition

Requires .NET SDK 8+ and Windows 10 1809+.

```powershell
powershell -ExecutionPolicy Bypass -File winui\build.ps1
```

Output `winui\dist\YHGamePush\` ships with `platform-tools`; distribute the whole folder.

## Android edition

Pure Java, no third-party dependencies. Open `android/` in Android Studio, grant all-files access, tap "开启共享" (start sharing). More in [`android/README.md`](android/README.md).

## Notes

- adb channel needs wireless debugging enabled and authorized on the phone
- LAN channel needs both devices on the same subnet; allow TCP 47820 and UDP 47821
- Paths outside the shared directory are rejected by design

MIT license.
