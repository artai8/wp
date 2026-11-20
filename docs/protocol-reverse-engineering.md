# 清真云（HalalCloud）客户端协议逆向报告

> 生成时间：2026-11-20
> 目标包：`com.halalcloud.diskmanager.halalcloud` v1.0.24（Flutter / targetSdk 36）
> 手段：Android 抓包（PCAPdroid）+ APK 静态提取 + Web 端 bundle 分析 + 线上签名验证

---

## 一、抓包结论：先前的 8080 明文判断是错的

| 项 | 实测结论 |
|---|---|
| 传输层 | 全量 **TLS 1.3 / 443**，无明文 h2c |
| API 主机 | `grpcuserapi.2dland.cn`（Web/移动共用），CDN 边缘 `drcdn2.2dland.cn`（腾讯 EdgeOne，CNAME `eo.dnse2.com`） |
| 错误上报 | `sentry.qiecdn.com` |
| 系统代理 | 被绕过（Dart `HttpClient` 直连），必须走 VPN 层才能看到流量 |
| 证书 | APK 无 `network_security_config`，targetSdk 36 → **不信任用户 CA**，常规 MITM 解不开 |

抓包样本（`downloads/pcap/cap1.pcap`，140 包）里清真云只有 3 条出站连接，全是 443，SNI 分别为
`sentry.qiecdn.com`、`drcdn2.2dland.cn`。DNS 查询记录同样只有这两个域名。

> 也就是说：**靠 TLS 解密这条路走不通（无 root + 无 pinning 绕过），但协议本身在 Web 端是公开的**。

---

## 二、突破口：Web 版 bundle 暴露了完整协议栈

`https://drive.2dland.cn` 是 Vue/Vite SPA，入口 `/assets/index-C6KzmNsb.js`（4.5 MB）内直接硬编码了：

```js
appid      = "2dland/web"
appversion = "202603182100"
appsecret  = "<APP_SECRET_REDACTED>"
host       = "https://grpcuserapi.2dland.cn"
```

以及请求签名函数（bundle 内 `au()`）：

```js
function au(call, token) {
  const ts = String(Date.now());                       // 毫秒时间戳
  call.header.set("timestamp",  ts);
  call.header.set("appid",      "2dland/web");
  call.header.set("appversion", "202603182100");

  let plain = "/" + call.service.typeName + "/" + call.method.name
            + String(ts + "2dland/web" + "202603182100");

  if (token?.length > 0) {
    const auth = "Bearer " + token;
    call.header.set("authorization", auth);
    plain += auth;
  }
  call.header.set("sign", MD5(plain + "<APP_SECRET_REDACTED>"));   // 小写 hex
}
```

### 可运行的等价实现（已验证）

```python
import time, hashlib

APPID, VER, SECRET = "2dland/web", "202603182100", "<APP_SECRET_REDACTED>"

def signed_headers(service: str, method: str, token: str | None = None):
    ts = str(int(time.time() * 1000))
    plain = f"/{service}/{method}{ts}{APPID}{VER}"
    h = {"timestamp": ts, "appid": APPID, "appversion": VER,
         "content-type": "application/grpc-web+proto", "x-grpc-web": "1"}
    if token:
        h["authorization"] = f"Bearer {token}"
        plain += f"Bearer {token}"
    h["sign"] = hashlib.md5((plain + SECRET).encode()).hexdigest()
    return h
```

### 线上验证结果

```
未带签名  -> grpc-status: 3  "missing timestamp"
带签名无 token -> grpc-status: 16 "invalid accesstoken; missing authorization"
```

签名被服务端接受，只差用户 token。**协议栈已完全复刻。**

---

## 三、服务清单（12 个 service / 108 个 RPC）

所有 RPC 同时暴露 **gRPC-Web** 与 **REST/JSON（grpc-gateway）** 两种入口，共 62 条 REST 路径。

| Service | RPC 数 | 职责 |
|---|---|---|
| `v6.services.pub.PubUserFile` | 29 | 文件增删改查、列表、搜索、分片下载地址、上传任务、文档预览、WebRTC 拉流 |
| `v6.services.pub.PubUser` | 23 | 登录/注册/改密/短信验证码/配额/用户设置/设备授权 |
| `v6.services.pub.PubFileShare` | 10 | 分享创建、点赞、点踩、投诉、转存 |
| `v6.services.pub.PubDavConfig` | 10 | WebDAV 配置（启用/禁用/服务器信息） |
| `v6.services.pub.PubTicket` | 9 | 工单与会话 |
| `v6.services.pub.PubSlice` | 6 | 分片元数据、秒传 FastLookup |
| `v6.services.pub.PubSftpConfig` | 6 | SFTP 配置与密钥 |
| `v6.services.oauth.OauthAuthorization` | 6 | OAuth2 授权码 / 设备码 |
| `v6.services.p2p.WebRTCManageService` | 5 | P2P 下载信令 |
| `v6.services.pub.PubOfflineTask` | 4 | 离线下载（BT/磁力解析、任务） |

### REST 路径样例（全量 62 条见 `protos/*.proto` 的 `google.api.http` 注解）

```
POST /v6/userfile/list              /v6/userfile/create        /v6/userfile/trash
POST /v6/userfile/move              /v6/userfile/copy          /v6/userfile/delete
POST /v6/userfile/get_slice_download_address
POST /v6/userfile/get_file_slices_download_info
POST /v6/userfile/create_upload_task        /v6/userfile/create_upload_token
POST /v6/userfile/get_direct_download_address
POST /v6/user/login                 /v6/user/register          /v6/user/get_statistics_and_quota
POST /v6/userfile/... (共 29 条)
```

---

## 四、核心数据模型

### File（文件/目录统一结构）

```protobuf
message File {
  string identity         = 1;   // 文件 ID（UUID 无连字符）
  string parent           = 3;   // 父目录 identity
  string name             = 4;
  string path             = 5;
  string mime_type        = 6;
  int64  size             = 7;
  int64  type             = 8;
  int64  create_ts        = 9;
  int64  update_ts        = 10;
  int64  delete_ts        = 11;
  bool   deleted          = 12;
  bool   dir              = 13;
  bool   hidden           = 14;
  bool   locked           = 15;
  bool   shared           = 16;
  bool   starred          = 17;
  bool   trashed          = 18;
  string unique           = 23;
  string content_identity = 24;  // 内容 ID ≈ MyDrive 的 CID
  int64  label            = 25;
  int64  store_type       = 26;
  int64  version          = 27;
  int64  files            = 28;
  int64  direcotries      = 29;  // 注意：服务端字段名拼写错误，保留原样
  int64  nodes            = 30;
  string sort_name        = 31;
}
```

### 列表请求 / 分页

```protobuf
message FileListRequest  { File parent = 1; File filter = 2; ScanListRequest list_info = 3; }
message FileListResponse { repeated File files = 1; ScanListRequest list_info = 2; }

message ScanListRequest {           // 游标分页
  string token  = 1;                // 下一页游标
  int64  limit  = 2;
  repeated OrderByInfo order_by = 3;
  int32  version = 4;
}
```

### 上传（分片参数由服务端下发）

```protobuf
message UploadTask {
  bool   created        = 1;
  string upload_address = 2;
  string task           = 3;
  string upload_token   = 4;
  int64  block_size     = 16;   // 分片大小
  int64  block_hash_type= 17;   // 分片哈希类型
  int32  block_version  = 18;
  int64  block_codec    = 19;
  // 同时兼容 S3/OSS 直传
  string access_key = 10; string secret_key = 11;
  string bucket = 12; string region = 13; string endpoint = 14;
}
```

### 秒传（FastLookup）

```protobuf
message FastLookup {
  string identity    = 1;
  string wcs_etag    = 2;
  string sha1        = 3;
  string head_sha1   = 4;   // 文件头部哈希
  int64  file_size   = 5;
  string special_sha1= 6;
  string name        = 7;
  string lh_hash     = 8;
}
```

### 下载地址

```protobuf
message SliceDownloadInfo {
  string identity         = 1;
  string download_address = 2;   // 预签名 URL
  string download_token   = 3;
  int32  encrypt          = 4;
  int64  store_type       = 5;
}
```

---

## 五、与 MyDrive 的映射对照

| 清真云 | MyDrive 现状 | 差异 / 待办 |
|---|---|---|
| `File.identity`（UUID） | `files.id` 自增 + `cid` | 需补 UUID 主键 |
| `File.content_identity` | `chunks.cid`（SHA256 base32 multihash） | 语义一致，编码不同 |
| `FastLookup.sha1 + file_size + head_sha1` | `fast_lookup(sha1, file_size, user_id)` | 已对齐，可加 `head_sha1` 提升命中 |
| `ScanListRequest.token` 游标分页 | 现有 `offset/limit` | 改为游标更贴近 |
| `UploadTask.block_size` 服务端下发 | 固定 4 MiB | 改为服务端可配 |
| `PubDavConfig` | `webdav_gateway.py` | 已实现，字段名待对齐 |
| `PubSftpConfig` | 未实现 | 待补 |
| `PubFileShare` 点赞/投诉 | 分享有，无点赞/投诉 | 待补 |
| `PubOfflineTask` BT/磁力 | 仅 HTTP 离线 | 待补解析器 |
| `WebRTCManageService` P2P 信令 | `/ws/p2p` DataChannel | 已实现，信令格式不同 |
| MD5 签名 + Bearer | JWT HS256 | 可选增加签名中间件 |

---

## 六、复现命令速查

```bash
# 1. 抓包（PCAPdroid 控制接口）
adb shell am start -n com.emanuelef.remote_capture/.activities.CaptureCtrl \
  -e action start -e pcap_dump_mode pcap_file -e pcap_uri file:///sdcard/Download/cap1.pcap
# 首次会弹 VPN 授权 + SAF 保存框，需手动确认

# 2. 拉取与分析
adb pull /sdcard/Download/cap1 ./cap1.pcap
python downloads/pcap/parse_pcap.py cap1.pcap      # 流统计
python downloads/pcap/tls_sni.py    cap1.pcap      # SNI / 证书
python downloads/pcap/dns_dump.py   cap1.pcap      # DNS 记录

# 3. 协议验证（签名 + 匿名调用）
python downloads/pcap/halal_probe.py
```

---

## 七、结论

1. 移动端与 Web 端共用同一套 **gRPC-Web / gRPC** 服务，主机 `grpcuserapi.2dland.cn`。
2. 鉴权 = **`timestamp + appid + appversion + sign(MD5)` + `Authorization: Bearer <token>`**，签名串含 HTTP 路径，防篡改。
3. 协议定义（12 service / 108 RPC / 62 REST 路径 / 全部 message）已完整提取，位于 `downloads/halalcloud_apk/protos/`。
4. 客户端唯一无法离线复现的是**用户 token**（存于应用私有目录，需 root 或重新登录获取）。
5. 复刻工作不再需要抓包解密——按本报告的签名算法 + proto 定义即可 1:1 实现服务端与客户端。

---

## 八、真实接口样本（账号实测，2026-11-20）

用账号 `loli001` 走完 Web 端 OAuth2 授权码流程拿到 access_token（JWT，552 字符），
随后以 `Bearer` 调用各接口，样本落盘在 `downloads/pcap/samples/`。

### 8.1 登录流程（无验证码，风控低时直接过）

```
1. GET https://drive.2dland.cn
   -> 302 https://ucenter.2dland.cn/oauth?login_type=authorization_code
        &code=<hex>&client_id=2dland/web&state=<uuid>&device=Web
2. 表单 POST（账号+密码），成功后进 /loginConfirm
3. 点「确认登录」-> 跳回 https://drive.2dland.cn/，localStorage 写入：
   USER_INFO = {"identity":"f42c4f7a...","name":"loli001","accessToken":"eyJ..."}
```

> 密码登录接口要求 `captcha` 字段为 JSON（含腾讯防水墙 `ticket` / `randstr`），
> 但 Web 端风控低时会直接放行，不需要过验证码。

### 8.2 File 结构（真实返回，字段号对齐 proto）

```json
{
  "1":  "fc6d2b42a4ce62129b32dcdbe47b5368",       // identity (32 hex, UUID 去连字符)
  "3":  "0124906f5d0cc96a8cf3371734c9328e",       // parent identity
  "4":  "스웨디시 1.mp4",                          // name
  "5":  "/Seo ahn/스웨디시 1.mp4",                 // path（绝对路径，含父目录名）
  "6":  "video/mp4",                              // mime_type
  "7":  938184848,                                // size
  "8":  40,                                       // type: 40=文件, 20=目录
  "9":  1795032182096,                            // create_ts (ms)
  "10": 1795032182096,                            // update_ts (ms)
  "13": 1,                                        // dir
  "24": "bafybeic7ywqgb6jyspavavz37tfo5os7aiws3ouwdznqsopnva2h7hnkyy",  // content_identity
  "31": "스웨디시 1 mp4"                            // sort_name（去扩展名）
}
```

### 8.3 分页游标（关键）

`list_info.token` 是 **base64(JSON)**，不是简单 offset：

```json
{"user_identity":"f42c4f7a52454c3ea5fe8bf60b94fb44","skip":10,"limit":10,
 "create_time":"2026-11-20T02:36:36.99488338+08:00","version":1,
 "order_by":[{"field":"type","asc":true},{"field":"create_ts"}]}
```

默认排序：先 `type` 升序，再 `create_ts`（新版在前）。

### 8.4 内容寻址：CIDv1

| 前缀 | 含义 | 用途 |
|---|---|---|
| `bafybei...` | CIDv1 / dag-pb / sha2-256 | 文件根（`content_identity`） |
| `bafkrei...` | CIDv1 / raw / sha2-256 | 分片（slice identity） |

**与 MyDrive 完全一致**（同为 sha256 multihash + base32 CIDv1）。

### 8.5 分片下载（4 MiB / 片）

实测文件 938,184,848 B → **224 片**，即 4 MiB/片：

```
https://slices.qzy.xyz/ipfs/blocks/2/75/bafkreick4nyxtwxd2jyqtpkhcuj347w6wa6mbu5mqfiuq3h7eznumbgu2a
  ?auth_key=<expire_ts>-<key_id>-<user_identity>-<sig>
  &session=<session_id>
```

- 路径 `/ipfs/blocks/<h2>/<h2>/<cid>`，两级目录取 CID 前 4 位十六进制
- `auth_key` 四段：过期时间戳、密钥 ID、用户 identity、签名
- 下载走独立域名 `slices.qzy.xyz`，与 API 域名分离

### 8.6 配额字段

```json
{"1": {"1": 13194139533312, "2": 5385562839164, "4": 655360, "5": 6744, "6": 1024},
 "2": {"1": 1099511627776, "2": 10737418240, "3": 1024},
 "3": {"1": 1024, "2": 1, "3": 10995116277760, "4": 1}}
```

| 组 | 字段 | 含义 |
|---|---|---|
| 1 | 1 / 2 | 总空间 12 TiB / 已用 5.01 TiB |
| 1 | 5 | 文件数 6744 |
| 2 | 1 / 2 | 日下行配额 1 TiB / 日上行配额 10 GiB |

### 8.7 离线任务

```json
{"1":"6afd954418be1cc1aa550a65", "2":5050, "3":1000,
 "5":1795019030822, "7":1795003716739,
 "8":"magnet:?xt=urn:btih:B3DF...", "10":"任务名",
 "11":"b3df..._otp_btv1", "16":100, "17":1383935599, "18":1383935599, "20":"/"}
```

- `2`/`3` 为状态码（5050 = 完成，1000 = 排队）
- `11` 是 BTIH + `_otp_btv1` 的存储键
- `16` 进度百分比

### 8.8 与 MyDrive 的实测一致性

| 项 | 清真云 | MyDrive | 结论 |
|---|---|---|---|
| 分片大小 | 4 MiB | 4 MiB | ✅ 一致 |
| 内容寻址 | CIDv1 base32 sha256 | CIDv1 base32 sha256 | ✅ 一致 |
| 秒传键 | sha1 + file_size (+head_sha1) | sha1 + file_size + user_id | ⚠️ 待补 head_sha1 |
| 下载 | 预签名 URL（独立域名） | `/api/chunk/{cid}` + token | ⚠️ 可加签名直链 |
| 分页 | 游标 base64(JSON) | offset/limit | ⚠️ 待改游标 |
| 主键 | 32 hex identity | 自增 id | ⚠️ 待改 |

### 8.9 上传流程（实测 CreateUploadTask）

```json
{
  "2":  "https://simple-file-proxy.2dland.cn/int/blocks",   // upload_address
  "3":  "upload_session::1b096482d65743309d5ce65421a939e2_hobbs2asja:ts::1795113548468",
  "16": 4194304,     // block_size = 4 MiB
  "17": 18,          // block_hash_type = 0x12 = sha2-256
  "18": 1,           // block_version = 1
  "19": 85           // block_codec = 0x55 = raw（对应 CIDv1 前缀 bafkrei）
}
```

`block_hash_type=18` / `block_codec=85` 正是 IPFS multicodec 编号：
`0x12 = sha2-256`、`0x55 = raw`、`0x70 = dag-pb`。**分片标识 = CIDv1(raw, sha256)，
文件根 = CIDv1(dag-pb, sha256)，与实测 `bafkrei…` / `bafybei…` 完全吻合。**

临时上传（不落盘，直传后可转正式）：

```json
{"2": "https://simple-file-proxy.2dland.cn/int/tempfile/upload",
 "3": "upload_session::7685d01861584a539d63a562c2f550f3_hobbs2frmr:ts::1795113548700",
 "16": 18446744073709551615, "18": 18446744073709551615}
```

> 上传走独立域名 `simple-file-proxy.2dland.cn`，路径 `/int/blocks`，每片一次 POST。
> 会话串格式：`upload_session::<32hex>_<8位随机>:ts::<毫秒时间戳>`。

### 8.9.1 上传链路已完整打通（2026-11-20 实测）

路径不是 `/int/blocks`，而是 **三段式** `/int/blocks/<task>/<cid>`。只 POST 根路径会拿到
Go 默认 mux 的 `404 page not found`（此前误判为「不可上传」）：

```text
1) CreateUploadTask(File{parent, path, size})   # 注意：name 通过 path 传，size 必填
   -> upload_address = https://simple-file-proxy.2dland.cn/int/blocks
      task           = upload_session::<32hex>_<8>:ts::<ms>

2) GET  {upload_address}/{task}/{cid}   -> 200 且 JSON 非空 = 已存在（秒传，跳过）
   POST {upload_address}/{task}/{cid}   -> 201
        Content-Type: application/octet-stream
        body = 该分片原始字节

3) POST {upload_address}/{task}         -> 200
        Content-Type: application/json
        body = ["<cid1>","<cid2>", ...]      # 按分片顺序
   -> {"user_identity","parent","path","size","content_identity", ...}
```

实测返回（1 KiB 与 8 MiB 两次真实上传，测试文件已 Trash + DeleteTrash 清理）：

| 文件 | 分片数 | content_identity |
|---|---|---|
| 1 KiB 随机 | 1 | `bafkreid3ozn…`（**等于分片 CID 本身**） |
| 8 MiB 确定性 | 2 | `bafybeifelct3…`（dag-pb UnixFS 根） |

**根 CID 算法（逐字节验证）**：与 js-ipfs 的
`{cidVersion:1, rawLeaves:true, chunker:fixed(4194304)}` 完全一致。

```text
PBNode = field2 Links  (先编码)
           └ PBLink{ field1 Hash = 子 CID 二进制, field2 Name = "" (显式 12 00), field3 Tsize = 子块字节数 }
       + field1 Data   = UnixFS{ field1 Type=2(File), field3 filesize, field4 blocksizes[] }
```

- 单分片：`reduceSingleLeafToSelf` → 根即 raw 分片 CID，不做 dag-pb 包裹；
- 多分片：174 links/节点，超出则 balanced 分层，父节点 `filesize` 与 `blocksizes` 取子树字节数；
- `Name=""` 必须写入（`12 00`）。省略会得到另一个 CID —— 这是最初复算不匹配的原因；
- 校验样本：8 MiB 双块 → `bafybeifelct3xojbueu5huhybhjm2ua4ehxxulfiqpyh4hglxfsj4bu5ka`；
  196 块（174+22）分层 → `bafybeiddkfzdyc3gibpebyq2idl2qlmcj45hrylkktn622kpkpxgqenvzm`。
  两者与本地实现、与线上服务返回值三方一致。

### 8.9.2 上传实现来自 Web 端 bundle（未混淆）

`drive.2dland.cn` 是 Angular（`assets/index-*.js` + 懒加载 chunk），上传逻辑在
`assets/taskUpload-*.js` 的 worker 里，可直读：

```js
const n = await Th({parent: task.parent, path: task.path, contentIdentity: "", size: BigInt(file.size)});
n.created ? postMessage({command: "IsDone"})
          : Mh(file.stream(), n).then(() => postMessage({command: "IsDone"}));
// Mh(): 按 blockSize 切流 -> cid = CID.create(blockVersion, blockCodec, sha256(chunk))
//       GET `${uploadAddress}/${task}/${cid}` 命中即跳过
//       POST `${uploadAddress}/${task}/${cid}` (octet-stream)
//       最后 POST `${uploadAddress}/${task}` body=JSON.stringify(cids)
```

`Dh = {name:"sha2-256", code:18}` 印证 `block_hash_type=18` 就是 multihash 的 sha2-256。

### 8.9.3 JS 里内嵌了完整 FileDescriptorProto

Web bundle 中的 `Ch91c2VyZmlsZS9wdWJsaWNfdXNlcl9maWxlLnByb3Rv…` 是 base64 的
`FileDescriptorProto`（解码后首行即 `userfile/public_user_file.proto`），与 APK 的
`protos/*.proto` 同源。字段号无需猜测，两边可互相校验。

### 8.10 WebDAV

```json
{"1":"webdav-server", "2":"0.0.3-build202402220030", "4":"Microsoft-IIS/8.0",
 "5":"https://dav.2dland.cn", "6":"443", "7":"HTTP/1.1", "8":"/", "9":1, "11":5}
```

- 独立域名 `dav.2dland.cn:443`，`Server` 头伪装成 `Microsoft-IIS/8.0`
- `ListAll` 返回配置记录，**含明文 WebDAV 用户名与密码**（见 `samples/16_dav_list.json`，此处不转录）
- 这组凭据与登录账号**相互独立**（可自定义用户名、6 位密码），是 WebDAV 客户端唯一入口
- MyDrive 已按此实现：`PubDavConfig` 下发独立凭据，`webdav_gateway.py` 用它做 Basic 认证，
  账户密码仍可回退登录

### 8.11 服务端未实现 / 不可用

| 接口 | 返回 |
|---|---|
| `PubUser.GetAvailableExtendQuota` | `grpc-status 12` method not implemented |
| `PubTicket.List` / `ListConversation` | `grpc-status 14` no healthy upstream（工单服务当前不可用） |
| `PubSftpConfig.Get` | 无响应（超时） |
| `PubSlice.GetMeta` | 无响应（超时） |

### 8.12 域名分工总表

| 域名 | 用途 | 备注 |
|---|---|---|
| `grpcuserapi.2dland.cn` | gRPC-Web / gRPC API | 主入口，需 MD5 签名 + Bearer |
| `drcdn2.2dland.cn` | 同上，CDN 加速入口 | 腾讯 EdgeOne 回源 |
| `slices.qzy.xyz` | 分片下载 | `/ipfs/blocks/<h2>/<h2>/<cid>` + auth_key |
| `simple-file-proxy.2dland.cn` | 分片上传 | `/int/blocks`、`/int/tempfile/upload` |
| `dav.2dland.cn` | WebDAV | 443，伪装 IIS |
| `ucenter.2dland.cn` | 登录 / OAuth2 授权 | 授权码 + PKCE |
| `user-center.2dland.cn` | 用户中心（改密、头像） | `UserCenterUri` 返回 |
| `drive.2dland.cn` | Web 前端 | Vue3 + gRPC-Web |
| `sentry.qiecdn.com` | 错误上报 | Sentry |

---

## 九、复刻落地：从「协议文档」到「可运行同构服务」（2026-11-20）

第 8 节的实测结论已全部落到 `mydrive/` 代码里，本节记录对应关系。

### 9.1 实测 → 实现对照

| 实测项 | 实测值 | MyDrive 实现 |
|---|---|---|
| identity | 32 hex（`fc6d2b42…`） | `protocol.new_identity()`，文件/用户/分享统一 |
| content_identity | `bafybei…` = CIDv1 **dag-pb** sha2-256 | `cid_engine.compute_file_cid()`，codec 0x70 |
| slice identity | `bafkrei…` = CIDv1 **raw** sha2-256 | `compute_cid()`，codec 0x55 |
| block_size | 4194304 | `protocol.BLOCK_SIZE` |
| block_hash_type | 18（0x12） | `protocol.BLOCK_HASH_TYPE` |
| block_codec | 85（0x55） | `protocol.BLOCK_CODEC` |
| task 串 | `upload_session::<32hex>_<8>:ts::<ms>` | `protocol.new_upload_session()` |
| 分页游标 | base64(JSON) skip/limit/order_by | `protocol.encode_cursor()` / `decode_cursor()` |
| 默认排序 | type ASC, create_ts DESC | `scan_list()` 用 `is_dir DESC, type ASC, create_ts DESC`（目录恒定置顶） |
| 下载 | `/ipfs/blocks/<h2>/<h2>/<cid>?auth_key=4段` | `/slices/blocks/<h2>/<h2>/<cid>?auth_key=4段` |
| 签名 | MD5(service+method+ts+appid+ver+[Bearer]+secret) | `grpc_compat.compute_sign()`，实测密钥同款 |
| 配额结构 | Disk/Traffic/Offline 三组 | `api_server./api/quota` + `PubUser.GetStatisticsAndQuota` |
| File 字段 | 31 个（identity…sort_name） | `pb.py` File schema 全字段 |

### 9.2 时间戳与类型码（本轮新确认）

- `create_ts = 1795032182096` → **毫秒**，不是秒。早期实现用秒，会导致与真实客户端
  混排时顺序错乱，已改为 `now_ms()`。
- `type` 实测：目录 = **20**，`video/mp4` = **40**；据此补齐 10 文档 / 30 音频 /
  50 图片 / 60 压缩包与其他（未知一律归 60，保证目录 20 仍排最前），并按 mime 前缀 + 扩展名双向映射。
- `sort_name` 实测 `스웨디시 1.mp4` → `스웨디시 1 mp4`，即**分隔符替换为空格**
  （不是去扩展名），拉丁字符转小写。

### 9.3 请求签名线上验证矩阵

| 请求 | grpc-status | 含义 |
|---|---|---|
| 无 timestamp/appid/sign | 3 | `missing timestamp` |
| 签名正确 + 无 token | 16 | `invalid accesstoken` |
| 签名错误 + 有 token | 3 | `invalid sign`（本地复刻层同码） |
| 签名正确 + 有效 token | 0 | 正常返回 protobuf |

复刻层的错误码与上游一致，客户端无需改动判别逻辑。

### 9.4 已知差异（诚实清单）

1. **文件级 CID 算法 —— 已消除（2026-11-20 深夜）**：原先用「有序分片 CID 串接后再哈希」，
   与真实值不同。现改为标准 UnixFS dag-pb（Links→Data，PBLink 含 `Name=""`，
   174 links/节点，单块折叠），并用 8 MiB 双块与 196 块分层两个样本和线上服务逐字节对齐，
   详见 §8.9.1。**同一内容现在得到同一个 `bafybei…`。**
2. **原生 gRPC**：本层是 gRPC-Web（HTTP/1.1 + 0x80 trailer 帧）。手机客户端走
   HTTP/2 原生 gRPC，需 hypercorn 或 Envoy 转换。
3. **slices 独立域名**：真实服务用 `slices.qzy.xyz`，本实现同路径但同域名，
   若要完全对齐可加一个反代 vhost。
4. **WebDAV 凭据 —— 已对齐**：真实服务 `ListAll` 明文返回独立账号密码，本实现同样下发，
   并让 `webdav_gateway` 用这组凭据做 Basic 认证（账号密码仍可回退）。

### 9.5 复现命令

```bash
cd mydrive
PORT=8765 STORAGE_ROOT=./storage_data \
  "C:/Users/Administrator/AppData/Local/Programs/Python/Python315/python.exe" api_server.py

# 带签名的 gRPC-Web 调用（curl 版）
TS=$(date +%s%3N)
SIGN=$(printf "/v6.services.pub.PubUserFile/List%s2dland/web202603182100<APP_SECRET_REDACTED>" "$TS" | md5sum | cut -d' ' -f1)
printf '\x00\x00\x00\x00\x05\x1a\x03\x08\x0a' | curl -sS --data-binary @- \
  -H "content-type: application/grpc-web+proto" -H "timestamp: $TS" \
  -H "appid: 2dland/web" -H "appversion: 202603182100" -H "sign: $SIGN" \
  -H "authorization: Bearer <token>" \
  http://127.0.0.1:8765/v6.services.pub.PubUserFile/List | xxd | head
```
