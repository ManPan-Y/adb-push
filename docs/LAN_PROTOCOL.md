# YHGP/1 局域网协议

用于 Windows 版与 Android 版之间「不装 adb、只用网卡」直接传输。
传输层是 HTTP/1.1，发现是 UDP 广播，载荷全部 UTF-8 JSON。

## 1. 发现

- UDP 广播到端口 **47821**
- 请求体：`YHGP/1 DISCOVER`
- 应答：单播回发送方，JSON

```json
{"magic":"YHGP/1","name":"卧室电脑","platform":"windows","version":"1.0","port":47820}
```

Windows 端会同时向 `255.255.255.255` 与本机各网卡的 /24 广播地址各发一次，等 1.5 秒收应答。

## 2. HTTP 接口

默认端口 **47820**。若配置了令牌，所有请求需带请求头 `X-YHGP-Token: <token>`，
否则返回 401。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/info` | 服务信息 |
| GET | `/api/stat?path=<rel>` | 路径状态 |
| GET | `/api/list?path=<rel>` | 目录直接子项 |
| GET | `/api/hash?path=<rel>&algo=md5` | 文件哈希 |
| GET | `/api/file?path=<rel>` | 下载，支持 `Range: bytes=N-` |
| PUT | `/api/file?path=<rel>&offset=N` | 从偏移 N 追加写入（续传） |
| POST | `/api/mkdir?path=<rel>` | 创建目录（含父级） |
| POST | `/api/move?from=<rel>&to=<rel>` | 改名 / 覆盖目标 |
| DELETE | `/api/file?path=<rel>` | 删除文件或目录 |

`path` 一律是相对于服务端共享根目录的**相对路径**，用 `/` 分隔，禁止 `..` 越界。
所有响应都是 JSON，除 `/api/file` 的下载流。

### GET /api/info

```json
{"magic":"YHGP/1","name":"卧室电脑","platform":"windows","version":"1.0",
 "root":"C:/Users/Admin/Desktop/share","writable":true,
 "sep":"\\","free":123456789}
```

### GET /api/stat

```json
{"ok":true,"exists":true,"type":"f","size":1048576,"mtime":1729000000}
```

### GET /api/list

```json
{"ok":true,"path":"Download","entries":[
  {"name":"a.txt","type":"f","size":12,"mtime":1729000000},
  {"name":"GAMES","type":"d","size":0,"mtime":1729000000}]}
```

### GET /api/file

- 无 Range：`200`，`Content-Length` 为完整大小，`X-YHGP-Size` 同值
- `Range: bytes=N-`：`206`，`Content-Range: bytes N-(size-1)/size`
- 断点续传即靠 Range 取剩余字节

### PUT /api/file

请求体是原始字节。`offset` 缺省为 0。语义：

- 文件不存在 → 创建
- 文件长度 == offset → 末尾追加
- 文件长度 > offset → 截断到 offset 后写入
- 文件长度 < offset → 409（调用方应回退到 0 重传）

响应：`{"ok":true,"size":<写入后的长度>}`

### 续传落地方式

上传先写 `<目标>.adbp.part`，完成后 `POST /api/move` 改名成正式文件。
下载先写本地 `<目标>.adbp.part`，完成后本地改名。
