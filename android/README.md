# Android 实验版

和 Windows 版走同一套局域网协议（[`docs/LAN_PROTOCOL.md`](../docs/LAN_PROTOCOL.md)），不需要 adb。

## 能做什么

- **手机当服务端**：电脑（Python 版或 WinUI3 版）填手机 `IP:47820`，直接读写手机的共享目录。
- **手机当客户端**：填电脑 IP，把手机文件传上去，或把电脑文件拉下来。
- **自动发现**：同一 WiFi 下点「搜索同网段」，双方互相可见。

## 构建

1. 用 Android Studio 打开 `android/` 目录。
2. 首次同步会下载 Gradle。
3. 直接 Run，或 Build → Generate Signed Bundle / APK。

命令行构建（需自备 JDK 17 + Android SDK）：

```bash
gradle :app:assembleDebug      # 或使用 Android Studio 自带的 Gradle
```

没有第三方依赖，`app/build.gradle.kts` 里的 `dependencies` 是空的。

## 使用

1. 首次启动会请求「所有文件访问权限」，允许后才能读写 `/sdcard`。
2. 共享目录默认 `Download/YHGPush`，可改。
3. 点「开启共享」，界面会显示 `192.168.x.x:47820`。
4. 电脑端把地址填进「网卡直连 → 对端地址」即可。

> 实验版限制：没有后台常驻服务，App 被系统杀掉后共享会停；只做单文件/目录级传输，不做相册/媒体库集成。
