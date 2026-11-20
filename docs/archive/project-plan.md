# 复刻清真云（HalalCloud / 2dland）网盘 — 完整技术方案

> 基于对 `com.halalcloud.diskmanager.halalcloud` v1.0.24 APK 的逆向分析 + 官方开源 proto 定义（`city404/v6-public-rpc-proto`）交叉验证

---

## 一、清真云是什么

一个基于 **IPFS 内容寻址**的去中心化网盘。核心特征：

- **服务端不存文件数据**，只存"文件树 + CID 索引"
- 文件按内容哈希切块，分散存储在用户硬盘和 P2P 节点上
- 相同内容自动去重（秒传）
- 支持 WebDAV / SFTP 挂载
- 支持 WebRTC P2P 直连下载
- Flutter 客户端，gRPC + Protobuf 通信

域名：`drive.2dland.cn`（API）、`drcdn2.2dland.cn`（CDN）、账号体系 `2dland.cn`

---

## 二、系统架构总览

```
┌─────────────────────────────────────────────────────────┐
│                    客户端 (Flutter)                       │
│  ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌─────────────┐ │
│  │文件管理  │ │下载管理   │ │同步管理   │ │WebDAV/SFTP  │ │
│  │(列表/预览)│ │(分片/断点)│ │(本地↔远程)│ │服务器       │ │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └──────┬──────┘ │
│       │             │            │              │        │
│  ┌────┴─────────────┴────────────┴──────────────┴──────┐ │
│  │              gRPC Client (Protobuf)                  │ │
│  │         + OAuth2 PKCE 认证拦截器                      │ │
│  └──────────────────────┬──────────────────────────────┘ │
│  ┌──────────────────────┴──────────────────────────────┐ │
│  │        本地 SQLite (Drift ORM)                       │ │
│  │  sync_tasks / cacheObject / open_connections        │ │
│  └─────────────────────────────────────────────────────┘ │
└─────────────────────────┬───────────────────────────────┘
                          │ gRPC (HTTP/2 + TLS)
┌─────────────────────────┴───────────────────────────────┐
│                    服务端                                │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────┐  │
│  │OAuth认证  │ │文件元数据 │ │分片索引   │ │配额/统计   │  │
│  │服务      │ │服务      │ │服务      │ │服务       │  │
│  └──────────┘ └──────────┘ └──────────┘ └───────────┘  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────┐  │
│  │WebDAV    │ │离线下载   │ │WebRTC    │ │分享/工单   │  │
│  │网关      │ │服务      │ │信令服务   │ │服务       │  │
│  └──────────┘ └──────────┘ └──────────┘ └───────────┘  │
│  ┌──────────────────────────────────────────────────┐   │
│  │              对象存储 (S3兼容)                     │   │
│  │   临时凭证下发 → 客户端直传                         │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
                          │
┌─────────────────────────┴───────────────────────────────┐
│              P2P 节点网络 (IPFS)                         │
│  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐        │
│  │用户节点A│  │用户节点B│  │缓存节点 │  │种子节点 │        │
│  │CID→块  │  │CID→块  │  │CID→块  │  │CID→块  │        │
│  └────────┘  └────────┘  └────────┘  └────────┘        │
└─────────────────────────────────────────────────────────┘
```

---

## 三、技术栈选型

### 客户端

| 层 | 清真云用的 | 你的选择 | 推荐理由 |
|---|---|---|---|
| 框架 | Flutter 3.35.7 | **Flutter** | 一套代码覆盖 Android/iOS/Desktop |
| 状态管理 | flutter_riverpod | **flutter_riverpod** | 清真云同款，社区活跃 |
| 路由 | go_router | **go_router** | 声明式路由，支持嵌套鉴权 |
| 数据库 | Drift (SQLite) | **Drift** | 类型安全 ORM，支持 isolate |
| gRPC | grpc + http2 | **grpc** | Protobuf 原生支持 |
| 认证 | OAuth2 + PKCE | **OAuth2 + PKCE** | 标准、安全 |
| CID 编解码 | 自实现 CidParser | **multiformats/dart-cid** | IPFS 标准 CID 库 |
| 图片缓存 | flutter_cache_manager | **同左** | 成熟方案 |
| 媒体播放 | media_kit (libmpv) | **media_kit** | 全格式支持 |
| 文件选择 | file_picker | **file_picker** | 跨平台 |
| WebDAV | 服务端实现 | 见服务端 | — |

### 服务端

| 层 | 清真云用的 | 你的选择 | 推荐理由 |
|---|---|---|---|
| 语言 | Go (推断) | **Go** | gRPC 原生、高并发、部署简单 |
| gRPC | go-grpc | **go-grpc** | 标准选择 |
| 元数据库 | (推断 PostgreSQL/MySQL) | **PostgreSQL** | 可靠、事务、JSONB |
| 对象存储 | S3 兼容 (UploadTask 含 access_key/secret_key/bucket) | **MinIO** | 自建 S3 兼容，临时凭证支持好 |
| IPFS 节点 | IPFS (go-ipfs/kubo) | **kubo (go-ipFS)** | 官方实现 |
| WebRTC | (服务端信令) | **pion/webrtc** | Go 原生 WebRTC |
| WebDAV | (自实现网关) | **golang.org/x/net/webdav** | 标准库 |
| 缓存 | (推断 Redis) | **Redis** | 临时凭证、会话、限流 |

---

## 四、核心协议定义（从官方 proto 还原）

### 4.1 文件元数据 — File 消息

```protobuf
message File {
    string identity = 1;           // 文件唯一 ID
    string parent = 3;             // 父目录 ID
    string name = 4;               // 文件名
    string path = 5;               // 完整路径
    string mime_type = 6;          // MIME 类型
    int64 size = 7;                // 文件大小
    int64 type = 8;                // 类型(文件/文件夹)
    int64 create_ts = 9;           // 创建时间戳
    int64 update_ts = 10;          // 更新时间戳
    int64 delete_ts = 11;          // 删除时间戳
    bool deleted = 12;
    bool dir = 13;
    bool hidden = 14;
    bool locked = 15;
    bool shared = 16;
    bool starred = 17;
    bool trashed = 18;
    int64 locked_at = 19;
    string locked_by = 20;
    int64 shared_at = 21;
    int64 flag = 22;
    string unique = 23;            // 唯一标识（用于去重判断）
    string content_identity = 24;  // ★ CID — 内容标识（IPFS）
    int64 label = 25;
    int64 store_type = 26;         // ★ 存储类型
    int64 version = 27;
    int64 files = 28;              // 子文件数
    int64 direcotries = 29;        // 子目录数
    int64 nodes = 30;              // 分片节点数
    string sort_name = 31;
}
```

### 4.2 分片存储 — Slice 消息

```protobuf
message Slice {
    string identity = 1;  // ★ 分片内容标识 (CID)
    int32 type = 2;
    int32 status = 3;
    int64 update_ts = 5;
    string peer = 6;       // ★ P2P 节点标识
    int64 create_ts = 7;
    string server = 8;     // ★ 后端服务器地址
    bool need_data = 9;    // 是否需要数据
    bytes data = 10;       // 内联数据（小块）
}

message Meta {
    string identity = 1;  // 元数据标识
    bytes data = 2;       // 元数据内容
}

// 秒传/去重索引
message FastLookup {
    string identity = 1;
    string wcs_etag = 2;       // ETag
    string sha1 = 3;           // SHA1 哈希
    string head_sha1 = 4;      // 文件头 SHA1（快速判断）
    int64 file_size = 5;
    string special_sha1 = 6;
    string name = 7;
    string lh_hash = 8;        // LH 哈希（另一种去重指纹）
}
```

### 4.3 文件解析 — ParseFileSliceResponse

```protobuf
message ParseFileSliceResponse {
    string content_identity = 1;     // 文件整体 CID
    repeated string meta_nodes = 2;  // 元数据节点 CID 列表
    repeated string raw_nodes = 3;   // 原始数据块 CID 列表
    int64 file_size = 4;
    repeated SliceSize sizes = 5;    // 各分片大小范围
    string sha1 = 6;
    string wcs_etag = 7;
    string name = 8;
    string path = 9;
    int64 store_type = 10;
    repeated SliceDownloadInfo raw_download_addresses = 11;
}

message SliceSize {
    int64 start_index = 1;
    int64 end_index = 2;
    int64 size = 3;
}

message SliceDownloadInfo {
    string identity = 1;          // 分片 CID
    string download_address = 2;  // 下载地址 (CDN/节点)
    string download_token = 3;    // 时效令牌
    int32 encrypt = 4;            // 加密标志
    int64 store_type = 5;
}
```

### 4.4 上传 — UploadTask（S3 兼容临时凭证）

```protobuf
message UploadToken {
    string token = 1;
    string access_key = 10;   // ★ S3 临时 AccessKey
    string secret_key = 11;   // ★ S3 临时 SecretKey
    string bucket = 12;       // ★ S3 Bucket
    string region = 13;
    string endpoint = 14;     // ★ S3 Endpoint
    string key = 15;          // ★ 对象 Key
}

message UploadTask {
    bool created = 1;
    string upload_address = 2;
    string task = 3;
    string upload_token = 4;
    string upload_key = 5;
    string access_key = 10;
    string secret_key = 11;
    string bucket = 12;
    string region = 13;
    string endpoint = 14;
    string key = 15;
    int64 block_size = 16;       // ★ 分块大小
    int64 block_hash_type = 17;  // ★ 分块哈希类型
    int32 block_version = 18;
    int64 block_codec = 19;      // 编码方式
}
```

### 4.5 gRPC 服务接口总览

```protobuf
// 文件操作服务
service PubUserFile {
    rpc Create(File) returns (File);                    // 创建文件/文件夹
    rpc Get(File) returns (File);                       // 获取文件信息
    rpc Rename(File) returns (BatchOperationResponse);  // 重命名
    rpc Trash(BatchOperationRequest) returns (BatchOperationResponse);   // 移入回收站
    rpc Move(BatchOperationRequest) returns (BatchOperationResponse);    // 移动
    rpc Copy(BatchOperationRequest) returns (BatchOperationResponse);    // 复制
    rpc Delete(BatchOperationRequest) returns (BatchOperationResponse);  // 永久删除
    rpc DeleteTrash(BatchOperationRequest) returns (BatchOperationResponse);
    rpc Recover(BatchOperationRequest) returns (BatchOperationResponse); // 从回收站恢复
    rpc List(FileListRequest) returns (FileListResponse);               // 列表
    rpc ListTrash(FileListRequest) returns (FileListResponse);
    rpc Search(SearchRequest) returns (FileListResponse);
    
    // ★ 核心：文件解析为分片
    rpc ParseFileSlice(File) returns (ParseFileSliceResponse);
    // ★ 分片下载地址
    rpc GetSliceDownloadAddress(SliceDownloadAddressRequest) returns (SliceDownloadAddressResponse);
    // ★ 直链下载
    rpc GetDirectDownloadAddress(DirectDownloadRequest) returns (FileDownloadAddressResponse);
    // 预览
    rpc GetDownloadAndPreviewInfo(File) returns (DownloadAndPreviewInfo);
    rpc PreviewDoc(File) returns (DocFilePreview);
    // ★ 上传
    rpc CreateUploadToken(File) returns (UploadToken);
    rpc CreateUploadTask(File) returns (UploadTask);
    rpc CreateTemporaryUpload(File) returns (UploadTask);
    // WebRTC P2P
    rpc CreateManageRTCOffer(ManageRTCRequest) returns (ManageRTCResponse);
    rpc SendClientIceCandidate(SendIceCandidateRequest) returns (SendIceCandidateResponse);
    rpc GetServerIceCandidate(GetIceCandidateRequest) returns (GetIceCandidateResponse);
}

// 分片存储服务
service PubSlice {
    rpc GetSlice(Slice) returns (Slice);
    rpc CreateSlice(Slice) returns (Slice);
    rpc GetMeta(Meta) returns (Meta);
    rpc CreateMeta(Meta) returns (Meta);
    rpc AddFastLookup(FastLookup) returns (FastLookup);    // 添加秒传索引
    rpc GetFastLookup(FastLookupRequest) returns (FastLookup); // 查询秒传
}

// OAuth 认证
service OauthAuthorization {
    rpc Authorize(AuthorizeRequest) returns (AuthorizeResponse);   // PKCE 授权
    rpc GetToken(TokenRequest) returns (TokenResponse);            // 获取令牌
    rpc RefreshToken(RefreshTokenRequest) returns (TokenResponse); // 刷新令牌
    rpc DeviceCode(AuthorizeRequest) returns (DeviceCodeAuthorizeResponse); // 设备码授权
}

// 用户/配额
service PubUser {
    rpc GetStatisticsAndQuota(...) returns (UserStatisticsAndQuota);
    // DiskStatisticsAndQuota: bytes_quota, bytes_used, files_quota, files_used, bytes_trash_used
    // TrafficStatisticsAndQuota: daily_bytes_download_quota, daily_bytes_upload_quota
    // OfflineTaskStatisticsAndQuota: daily_tasks_quota
}

// WebDAV 网关
service PubDavConfig { Get/Update/Enable/Disable/List/Create/Delete }

// SFTP 网关
service PubSftpConfig { Get/Update/Enable/Disable/UpdateKeys }

// 离线下载
service PubOfflineTask { Parse/Add/List/Delete }

// 文件分享
service PubFileShare { Create/Get/List/Delete/Save/Like/Dislike }

// WebRTC P2P 下载信令
service WebRTCManageService {
    rpc StartDownload(StartDownloadRequest) returns (StartDownloadResponse);
    rpc StopDownload(StopDownloadRequest) returns (StopDownloadResponse);
    rpc SendPeerIceCandidate(...) returns (...);
    rpc Receive(ReceiveRequest) returns (stream ReceiveResponse);
}
```

---

## 五、核心流程实现

### 5.1 上传流程（秒传 + 分片上传）

```
用户选择文件
     │
     ▼
计算文件指纹（SHA1 + head_sha1 + 文件大小）
     │
     ▼
调用 GetFastLookup 查询是否已存在
     │
     ├─ 命中 → 秒传：直接创建 File 记录，content_identity 指向已有 CID
     │         流程结束，无需上传任何数据
     │
     └─ 未命中 → 正常上传：
                    │
                    ▼
              调用 CreateUploadToken → 获取 S3 临时凭证 (access_key/secret_key/bucket/endpoint)
                    │
                    ▼
              调用 CreateUploadTask → 获取 block_size、key、upload_address
                    │
                    ▼
              本地按 block_size 切分文件
                    │
                    ▼
              每块计算 SHA256 → 生成 CID (multibase + multihash)
                    │
                    ▼
              用 S3 Multipart Upload 上传各分块到对象存储
                    │
                    ▼
              上传完成后服务端生成:
                - ParseFileSliceResponse (content_identity + meta_nodes + raw_nodes)
                - AddFastLookup (写入秒传索引)
                    │
                    ▼
              创建 File 记录 (content_identity = 文件整体 CID)
```

### 5.2 下载流程（分片 + CDN + P2P）

```
用户点击下载
     │
     ▼
调用 ParseFileSlice(file) → 获取:
  - content_identity (文件 CID)
  - meta_nodes (元数据块 CID 列表)
  - raw_nodes (数据块 CID 列表)
  - sizes (各块大小范围)
     │
     ▼
调用 GetSliceDownloadAddress(raw_nodes) → 获取各块下载地址:
  - download_address (CDN URL, 时效链接)
  - download_token
  - store_type
     │
     ├─ CDN 下载: HTTP GET 各分块 → 本地拼接
     │
     └─ P2P 下载 (可选):
        调用 WebRTCManageService.StartDownload(content_identity)
        → 返回 peers (RtcPeerInfo[]，含 SDP)
        → 建立 WebRTC 连接
        → 从其他用户节点拉取分块
        → 优先用 P2P，CDN 兜底
     │
     ▼
所有分块下载完成 → 按 sizes 顺序拼接 → 校验 SHA1
     │
     ▼
保存到本地: /sdcard/Android/data/<包名>/files/downloads/
```

### 5.3 CID 生成与编解码

```
CID 生成:
  原始数据 → SHA256 → multihash(0x12=sha256, 0x20=32字节长度, <hash>)
           → multibase编码:
             - base32 (常用, CIDv1)
             - base58btc (CIDv0, 以 "Qm" 开头)
             - base32Upper
             - base32Hex
             - zbase32

CID 解析:
  字符串 → 识别 multibase 前缀 → 解码 → 解析 multihash → 提取哈希算法+哈希值

清真云用的是标准 IPFS CID (CIDv1 base32 或 CIDv0 base58btc)
依赖: multiformats/dart-cid 或 Go: github.com/ipfs/go-cid
```

### 5.4 OAuth2 + PKCE 认证

```
1. 客户端生成 code_verifier (随机字符串)
   code_challenge = SHA256(code_verifier) → Base64URL → code_challenge

2. 打开浏览器: Authorize(client_id, response_type=code, 
   code_challenge, code_challenge_method=S256, redirect_uri, state)

3. 用户登录授权 → 回调返回 code

4. GetToken(code, code_verifier, grant_type=authorization_code)
   → 返回 access_token + refresh_token + expires_in

5. 后续请求: Authorization: Bearer <access_token>
   过期后: RefreshToken(refresh_token) → 新令牌
```

### 5.5 本地数据库 (SQLite / Drift)

```sql
-- 同步任务表
CREATE TABLE sync_tasks (
    id INTEGER PRIMARY KEY,
    local_path TEXT,        -- 本地路径
    remote_path TEXT,       -- 远程路径
    task_type INTEGER,      -- 任务类型(上传/下载/双向同步)
    status INTEGER,
    progress INTEGER,
    ...
);

-- 缓存表 (flutter_cache_manager)
CREATE TABLE cacheObject (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE,
    sub_id TEXT,
    eTag TEXT,
    validTill INTEGER,
    length INTEGER,
    ...
);

-- 连接追踪
CREATE TABLE IF NOT EXISTS open_connections(
    id INTEGER PRIMARY KEY,
    database_pointer INTEGER,
    ...
);
```

---

## 六、关键难点与解决方案

### 难点 1：IPFS 内容寻址与分片引擎

**问题**：需要实现文件 → CID → 分块存储 → P2P 分发的完整链路

**解决方案**：
- 集成 kubo (go-ipfs) 作为 IPFS 节点
- 分块策略：固定大小分块 (如 1MB/4MB/8MB，根据 `block_size` 配置)
- CID 用标准 `go-cid` 库生成
- 小文件内联存储 (`Slice.data` 字段)
- 大文件分块后每块独立 CID，用 `Meta` 消息记录文件→块的映射

**预估工作量**：4-6 周

### 难点 2：秒传/去重系统

**问题**：需要在服务端维护多重哈希索引，支持快速判断文件是否已存在

**解决方案**：
```
FastLookup 表 (PostgreSQL):
  identity (UUID)
  sha1 (全文文件)
  head_sha1 (前 256KB 的 SHA1)  
  file_size
  wcs_etag
  lh_hash
  content_identity (CID)
  
查询逻辑:
  1. 先按 head_sha1 + file_size 精确匹配（快速，99% 覆盖）
  2. 再按 sha1 完整校验（确保正确性）
  3. 命中则返回 CID，客户端直接创建 File 记录
```

**预估工作量**：2 周

### 难点 3：WebRTC P2P 下载

**问题**：需要实现 WebRTC 信令服务器 + 客户端 P2P 连接 + 分块路由

**解决方案**：
- 服务端用 pion/webrtc 实现信令
- 客户端用 Flutter WebRTC 插件
- 调度策略：优先 P2P（查询哪些节点有该 CID），CDN 兜底
- `StartDownloadRequest` 包含 `peer_limit`（最大 P2P 节点数）

**预估工作量**：3-4 周（可先跳过，用纯 CDN 启动）

### 难点 4：S3 兼容临时凭证上传

**问题**：客户端不直接持有对象存储密钥，需要服务端下发临时凭证

**解决方案**：
- 用 MinIO 作为 S3 兼容后端
- 服务端用 STS (Security Token Service) 生成临时凭证
- 凭证有效期 15-30 分钟，按 UploadTask 下发
- 客户端用标准 S3 Multipart Upload API 上传

**预估工作量**：1 周

### 难点 5：WebDAV / SFTP 网关

**问题**：需要把 IPFS 后端适配为标准 WebDAV/SFTP 协议

**解决方案**：
- WebDAV：用 `golang.org/x/net/webdav` 实现 Handler，底层映射到 gRPC 文件操作
- SFTP：用 `github.com/pkg/sftp` 实现，同理映射
- 每用户独立账号 (`DavConfig.username` / `SftpConfig.username`)
- 支持读写控制 (`write_flag`)、锁 (`lock_flag`)、分块 (`chunked_flag`)

**预估工作量**：2 周

### 难点 6：多端同步

**问题**：本地文件夹与云端双向同步，需要冲突解决

**解决方案**：
- `sync_tasks` 表记录同步规则 (local_path ↔ remote_path)
- 增量同步：按文件修改时间 + CID 判断变更
- 冲突策略：最后写入胜出 + 版本号 (`File.version`)
- 用 isolate 隔离同步逻辑（清真云的 `download_isolate_manager.dart`）

**预估工作量**：3 周

### 难点 7：配额与流量控制

**问题**：需要限制每用户的存储空间、日下载流量、离线任务数

**解决方案**：
```sql
-- 配额表
disk_quota: bytes_quota, bytes_used, files_quota, files_used, bytes_trash_used
traffic_quota: daily_bytes_download_quota, daily_bytes_upload_quota
offline_quota: daily_tasks_quota

-- 每次操作前检查配额
-- 每日重置流量计数 (定时任务)
-- 支持扩展配额 (ExtendQuota)
```

**预估工作量**：1 周

---

## 七、MVP 开发路线

### Phase 0：基础设施（1-2 周）

```
□ 搭建 Go 项目骨架 (gRPC server + Protobuf codegen)
□ 部署 PostgreSQL + MinIO + Redis
□ 部署 kubo (IPFS 节点)
□ 定义 proto 文件（直接复用 city404/v6-public-rpc-proto）
□ 生成 Go + Dart 代码
```

### Phase 1：认证 + 文件元数据（2-3 周）

```
□ OAuth2 + PKCE 登录流程
□ 文件 CRUD (Create/Get/List/Rename/Move/Copy/Delete/Trash/Recover)
□ 文件树结构 (parent → children)
□ 客户端基本 UI（登录页 + 文件列表页）
□ 本地 SQLite 同步 (Drift)
```

### Phase 2：上传 + 下载（3-4 周）

```
□ S3 临时凭证上传 (CreateUploadToken + CreateUploadTask)
□ 分块上传 + SHA256 CID 生成
□ 秒传索引 (FastLookup)
□ ParseFileSlice 文件解析
□ 分块下载 + CDN 地址获取
□ 断点续传
□ 客户端上传/下载 UI + 进度显示
```

### Phase 3：完善体验（2-3 周）

```
□ 文件预览（图片/视频/文档）
□ 搜索功能
□ 分享链接
□ 配额统计
□ 缓存管理 (flutter_cache_manager)
□ 多端同步 (sync_tasks)
```

### Phase 4：高级功能（按需）

```
□ WebDAV 网关
□ SFTP 网关
□ 离线下载
□ WebRTC P2P 下载
□ 加密存储
□ 客服工单系统
```

---

## 八、项目结构建议

```
my-cloud-drive/
├── proto/                     # Proto 定义（复用 v6-public-rpc-proto）
│   ├── userfile/
│   ├── slice/
│   ├── oauth/
│   └── ...
│
├── server/                    # Go 服务端
│   ├── cmd/
│   │   └── main.go
│   ├── internal/
│   │   ├── auth/              # OAuth2 + PKCE
│   │   ├── file/              # 文件元数据 CRUD
│   │   ├── slice/             # 分片解析 + IPFS 交互
│   │   ├── upload/            # S3 临时凭证 + 上传任务
│   │   ├── download/          # 下载地址生成
│   │   ├── quota/             # 配额管理
│   │   ├── webdav/            # WebDAV 网关
│   │   ├── sftp/              # SFTP 网关
│   │   ├── offline/           # 离线下载
│   │   ├── share/             # 文件分享
│   │   ├── wrtc/              # WebRTC 信令
│   │   └── store/             # 数据库 + MinIO + Redis
│   ├── go.mod
│   └── Dockerfile
│
├── client/                    # Flutter 客户端
│   ├── lib/
│   │   ├── main.dart
│   │   ├── application/
│   │   ├── core/
│   │   │   └── cid_parser.dart      # CID 编解码
│   │   ├── database/
│   │   │   └── database.dart        # Drift ORM
│   │   ├── managers/
│   │   │   ├── download_manager/    # 下载管理
│   │   │   └── http_manager/        # HTTP 流 + 缓存
│   │   ├── models/
│   │   ├── pages/
│   │   │   ├── files/               # 文件管理
│   │   │   ├── settings/            # 设置
│   │   │   ├── syncing/             # 同步
│   │   │   └── home/                # 首页
│   │   ├── providers/
│   │   │   ├── grpc_provider.dart   # gRPC 连接
│   │   │   ├── database_provider.dart
│   │   │   └── settings/
│   │   ├── protocol/                # 生成的 pb 代码
│   │   ├── routers/
│   │   ├── rpc/
│   │   │   └── client_interceptor.dart  # OAuth 拦截器
│   │   └── utils/
│   ├── pubspec.yaml
│   └── android/
│
├── deploy/                    # 部署配置
│   ├── docker-compose.yml     # PG + MinIO + Redis + kubo
│   ├── nginx/                 # 反向代理 + TLS
│   └── k8s/                   # K8s 清单（可选）
│
└── docs/
```

---

## 九、关键技术决策清单

| 决策点 | 清真云的做法 | 你的最优选择 | 理由 |
|---|---|---|---|
| 文件存储后端 | IPFS (P2P) + S3 兼容 (兜底) | **MinIO (S3) 优先**，IPFS 作为加速层 | 先保证可用性，P2P 后加 |
| 分块大小 | 服务端动态下发 (block_size) | **默认 4MB**，可配置 | 平衡内存/IO/网络 |
| CID 版本 | CIDv1 (base32) 为主 | **CIDv1** | IPFS 推荐 |
| 哈希算法 | SHA256 (CID) + SHA1 (秒传) | **同左** | 双层：CID 做内容寻址，SHA1 做快速查重 |
| 认证 | OAuth2 + PKCE | **同左** | 标准、安全、支持第三方接入 |
| 客户端框架 | Flutter + Riverpod | **同左** | 已验证可行 |
| 服务端语言 | Go | **同左** | gRPC 原生、IPFS 生态 |
| 数据库 | Drift (客户端) + SQL (服务端) | **同左** | 类型安全 |
| P2P | WebRTC | **先跳过** | MVP 阶段不需要 |

---

## 十、风险与注意事项

1. **去中心化删除问题**：P2P 存储下"删除"只删索引，内容块可能在其他节点存在。清真云对此无明确方案，你也需要决定策略（TTL 过期清理 / 节点共识删除 / 接受不可删除）。

2. **冷启动 P2P 网络**：没有足够的用户节点时，P2P 下载形同虚设。MVP 阶段建议用纯 CDN，用户量上来后再开 P2P。

3. **法律合规**：去中心化存储的内容不可控，可能被用于分享侵权/违规内容。需要有分享审核机制（清真云有 `ComplaintShare` 投诉接口）。

4. **性能**：IPFS 节点发现和路由有延迟，首次下载可能较慢。需要预热缓存节点。

5. **数据一致性**：服务端只存索引，如果索引库出问题，文件就"丢失"了。需要多副本 + 定期备份。

6. **成本**：纯 CDN 方案流量成本高，P2P 是省钱的，但初期必须承担 CDN 成本。

---

## 附录：关键依赖库清单

### Go 服务端
```
google.golang.org/grpc          # gRPC
google.golang.org/protobuf      # Protobuf
github.com/ipfs/go-cid          # CID 编解码
github.com/ipfs/kubo            # IPFS 节点 (或作为独立进程)
github.com/minio/minio-go/v7    # S3 兼容存储
github.com/redis/go-redis/v9    # Redis
github.com/jackc/pgx/v5         # PostgreSQL
github.com/pion/webrtc          # WebRTC (Phase 4)
golang.org/x/net/webdav         # WebDAV (Phase 4)
github.com/pkg/sftp             # SFTP (Phase 4)
```

### Flutter 客户端
```yaml
flutter_riverpod: ^2.5.1        # 状态管理
go_router: ^14.0.0              # 路由
grpc: ^4.0.0                    # gRPC
protobuf: ^3.1.0                # Protobuf
drift: ^2.20.0                  # SQLite ORM
drift_flutter: ^0.2.0           # Drift Flutter 集成
crypto: ^3.0.0                  # SHA256/SHA1
base32: ^2.1.3                  # Base32 编解码
flutter_cache_manager: ^3.4.0   # 图片缓存
file_picker: ^8.0.0             # 文件选择
media_kit: ^1.1.0               # 媒体播放
cached_network_image: ^3.4.0    # 图片加载
flutter_web_auth_2: ^4.0.0      # OAuth 浏览器回调
media_kit_video: ^1.1.0         # 视频播放器
```
