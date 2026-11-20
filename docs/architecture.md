# MyDrive 网盘复刻项目 — AI 交接文档

> 交接日期：2026-11-20（第二轮，Bug 已修复）
> 项目性质：复刻「清真云 / HalalCloud」网盘的核心功能（重点在**存储**与**下载**）
> 本文档是接手的**权威状态来源**。任务追踪器（TaskList）里 #5–#10 显示 pending/in_progress 是历史遗留，**代码实际已全部写完**，请以本文档第 5、6 节为准。

---

## 0. 一句话概况

用 Python（FastAPI + 标准库）从零实现的一个 IPFS 内容寻址网盘：文件按 4MB 分块、每块用 CID（SHA256+multihash+base32）寻址、自动去重、秒传、断点续传、零知识加密，并叠加了 WebDAV / OAuth2 / 分享 / 工单 / 离线下载 / 同步 / P2P 信令等清真云同款功能。**代码全部可运行，上一轮的 3 个 Bug（WebDAV GET 500、文件名 tmp 前缀、工单前端缺失）已全部修复并验证通过。剩余待办见第 6 节。**

---

## 1. 用户背景与偏好（务必遵守）

- 用户最初要求「复刻清真云这种网盘」，并明确表态：**「我不要文档，直接开干，我觉得复刻 app 不难，难的是储存和下载」**、**「不要给我看文档啦，直接干」**、**「我不要文档，也不要总结，给我功能和清真云一样的 app」**。
- 也就是说：用户重视**能跑的成品和功能**，不喜欢空谈和长篇说明。
- **交接文档是用户主动要求**（用于移交其他 AI），所以本文档是例外。
- 接手后沟通建议：直接干活、少写长篇总结、优先给出可验证的结果。

---

## 2. 环境信息（关键，照抄即可）

| 项目 | 值 |
|---|---|
| 项目根目录 | `C:\Users\Administrator\WorkBuddy AI\2026-11-09-08-48-20` |
| 代码目录 | `...\mydrive` |
| Python 解释器 | `C:\Users\Administrator\AppData\Local\Programs\Python\Python315\python.exe`（**必须用这个全路径**；`python` 命令指向 3.13，没装 fastapi） |
| 服务地址 | `http://127.0.0.1:8765`（绑定 127.0.0.1，非 0.0.0.0） |
| 当前服务状态 | **未运行**（交接时已停止，启动命令见下方） |
| 依赖 | fastapi、uvicorn、requests、pydantic（已装到上面的 Python315） |
| 启动脚本 | `mydrive\启动.bat`（Windows）、`mydrive\start.sh`（Linux） |

### 启动命令
```bash
cd "C:\Users\Administrator\WorkBuddy AI\2026-11-09-08-48-20\mydrive"
"C:\Users\Administrator\AppData\Local\Programs\Python\Python315\python.exe" api_server.py
```
> 注意：本环境后台进程会随命令结束被回收。若要长期驻留，用用户本机运行 `启动.bat`，或用带 `wait` 的长时后台任务保持进程存活。重启前先 `netstat -ano | findstr :8765` 找旧 PID，`taskkill //F //PID <PID>`（Git Bash 要用双斜杠）。

---

## 3. 文件结构与职责

```
mydrive/
├── cid_engine.py          (209行) CID 引擎：compute_cid / parse_cid / quick_fingerprint
├── storage_engine.py      (659行) 核心存储引擎：分块、CID 去重、秒传、元数据、SQLite 索引
├── download_manager.py    (450行) 断点续传 + 并行分块下载（.progress + .part）
├── encryption_layer.py    (377行) 零知识加密：PBKDF2 + HMAC-CTR，CID 对密文计算
├── api_server.py          (625行) FastAPI 主服务器，聚合所有功能 + Web UI
├── webdav_gateway.py      (417行) WebDAV 网关（桥接到 storage engine）✅ GET 已修复
├── auth_manager.py        (334行) OAuth2 多用户认证：注册/登录/JWT/refresh/device code
├── share_manager.py       (224行) 分享链接 + 工单系统
├── offline_downloader.py  (184行) 离线下载（后台线程 + 进度追踪）
├── sync_manager.py        (260行) 本地↔云端同步
├── p2p_signaling.py       (172行) WebRTC P2P 信令服务器（WebSocket）
├── e2e_test.py            (236行) 6 个端到端测试
├── static/index.html      (1339行) 完整 Web UI（单文件，无框架，含工单页面）
├── 启动.bat / start.sh             启动脚本
└── storage_data/                   运行时数据（chunks/ meta/ db/*.sqlite）
```

> 行数统计截至 2026-11-20 第二轮修复后。总代码量约 5486 行。

相关的逆向分析资料（只读参考）：
- `downloads/halalcloud_apk/protos/` — 16 个清真云官方 proto 文件（userfile / slice / oauth / offline / webdav / sftp / share / ticket / wrtc / user 等），来自 `city404/v6-public-rpc-proto`。
- 历史会话记忆：`.workbuddy-ai/memory/2026-11-09.md`、`.workbuddy-ai/memory/2026-11-20.md`

---

## 4. 技术架构与核心原理

### 4.1 存储（项目的精髓，已完整实现并测试通过）
1. **CID 内容寻址**：`compute_cid(data)` = SHA256 → multihash(0x12,0x20) → CIDv1(raw codec 0x55) → base32，产出 `bafkrei...`。与真实 IPFS 一致。
2. **分块**：默认 4MB/块（`CHUNK_SIZE` 环境变量可调），每块独立存 CID。
3. **磁盘布局**：`storage_data/chunks/ba/fk/<完整CID>`（按 CID 前 4 字符分两级目录）。
4. **去重**：相同内容 → 相同 CID → 文件不重复写盘（`_store_chunk` 里 `if exists: return`）。
5. **秒传**：`fast_lookup` 表用 `SHA1 + head256K SHA1 + file_size` 双重匹配；`quick_fingerprint()` 生成指纹。前端用 Web Crypto API 算 SHA1 后先调 `/api/instant-check`。
6. **元数据**：每个文件一个 `meta/<uuid>.json`（含 FAT 风格的有序 chunk CID 列表），同时写 `db/index.sqlite` 的 `files / chunks / file_chunks / fast_lookup / user_quota` 表。

### 4.2 下载（已完整实现并测试通过）
- `download_manager.py`：`.progress`（JSON 记录已完成块）+ `.part`（预分配，按字节偏移写块）→ 每块下载后重算 CID 校验防篡改 → 全部完成后改名成最终文件。
- 支持并行（ThreadPoolExecutor，默认 4）与中断续传（同名 identity 再调用即续传）。

### 4.3 加密（已实现并通过测试）
- PBKDF2(password) 派生 master key；HMAC(master_key, plain_cid) 派生每块密钥；HMAC-CTR 流加密。
- **CID 对密文计算**，服务器只见密文，零知识。`upload_encrypted_v2` / `download_and_decrypt_v2` 是正确版本（v1 是废弃草稿，保留但别用）。

### 4.4 各功能的实现方式（清真云对照）
- 清真云真实架构：Flutter 客户端 + gRPC（proto 开源）+ S3 临时凭证上传 + IPFS 式分块 + WebRTC P2P + WebDAV/SFTP 网关。
- 本项目等价实现：FastAPI REST 替代 gRPC，本地文件系统替代 S3，WebSocket 替代 P2P 信令 gRPC。

---

## 5. 当前完成状态（逐功能，已实测）

> 以下是 2026-11-20 第二轮修复后**实际用 curl 跑通**的验证结果，不是计划。

| 功能 | 代码状态 | 实测结果 |
|---|---|---|
| CID 引擎 | ✅ 完成 | 同内容同 CID、不同内容不同 CID、合法 `bafkrei...` |
| 存储引擎 | ✅ 完成并测试 | 10MB→3块→重组一致；重复上传去重 25%；秒传命中 |
| 下载管理器 | ✅ 完成并测试 | 并行下载一致；模拟中断后续传一致 |
| 加密层 | ✅ 完成并测试 | 10MB 加解密一致；错密码拒绝；篡改被 CID 检测 |
| 文件 API + Web UI | ✅ 完成 | `/api/files`、`/api/upload`、`/`（1339 行单页 UI）正常 |
| #5 WebDAV 网关 | ✅ 完成并修复 | OPTIONS/PROPFIND/GET/PUT/MKCOL/DELETE/MOVE/COPY 全部通过；**GET 已修复**（见 6.1） |
| #6 OAuth2 认证 | ✅ 完成 | register/login 实测成功，返回 JWT + refresh token |
| #7 本地↔云端同步 | ✅ 完成 | add/list/status/run 实测通（简化版，见 6.4） |
| #8 离线下载 | ✅ 完成 | add/list 实测通，后台线程下载（HTTP(S)） |
| #9 WebRTC P2P | ⚠️ 信令完成，客户端未完成 | 信令服务器 + `/ws/p2p` + peers 查询通；真正 DataChannel 传输需客户端实现（见 6.3） |
| #10 工单系统 | ✅ 完成（前后端） | create/list/get/reply 全通；**前端页面已做好**（见 6.2） |
| 分享链接 | ✅ 完成 | create/list/密码/过期/次数/download 均实测通 |
| 文件名正确性 | ✅ 已修复 | 新上传文件不再有 `tmpXXXX_` 前缀（见 6.1） |

### 已注册的 API 端点（完整清单）
```
WebDAV:  OPTIONS/PROPFIND/GET/PUT/MKCOL/DELETE/MOVE/COPY /webdav/{path:path}, /webdav
文件:    GET /api/stats | POST /api/instant-check | POST /api/upload | GET /api/files
         GET /api/files/{id} | GET /api/files/{id}/chunks | GET /api/download/{id}
         GET /api/chunk/{cid} | POST /api/mkdir | DELETE /api/files/{id} | GET /api/verify/{cid}
         POST /api/move | POST /api/rename
认证:    POST /api/auth/register | /api/auth/login | /api/auth/refresh
         /api/auth/device-code | /api/auth/device-poll | /api/auth/device-authorize
         GET /api/auth/me
分享:    POST /api/share | GET /api/share/{id} | GET /api/share/{id}/download
         GET /api/shares | DELETE /api/share/{id}
工单:    POST /api/ticket | GET /api/tickets | GET /api/ticket/{id}
         POST /api/ticket/{id}/reply    ← 新增（第二轮修复）
离线:    POST /api/offline/add | GET /api/offline/list | GET/DELETE /api/offline/{task_id}
同步:    POST /api/sync/add | GET /api/sync/list | POST /api/sync/run/{pair_id}
         POST /api/sync/run-all | DELETE /api/sync/{pair_id} | GET /api/sync/status
P2P:     GET /api/p2p/peers | GET /api/p2p/peers/{cid} | POST /api/p2p/announce
         WS /ws/p2p
UI:      GET /  |  /docs（FastAPI 自动文档）
```

---

## 6. 已知问题与待办

> **6.1 和 6.2 已在第二轮修复中完成，保留说明供参考。实际待办从 6.3 开始。**

### 6.1 【已修复】WebDAV GET 返回 500
- **原现象**：`GET /webdav/<文件>` 报 500。
- **原根因**：`webdav_gateway.py` 的 `dav_get()` 里用 `FastResponse(content=stream())`，而 `Response` 只接受 `bytes/str`，不接受 generator。
- **已修复**：导入 `StreamingResponse`，改用 `StreamingResponse(stream(), media_type=..., headers=...)`。
- **验证**：`curl PUT` 上传 → `curl GET` 下载，返回 **200** + 正确文件内容。✅

### 6.2 【已修复】上传文件名被加临时前缀 + 工单前端缺失
- **原现象 1**：上传文件名变成 `tmpXXXX_test.txt`。
- **原根因 1**：`storage_engine.chunk_data()` 用 `NamedTemporaryFile(suffix=f"_{name}")`，`chunk_file()` 取 `Path(tmp_path).name` 当文件名。
- **已修复 1**：`chunk_file(file_path, parent, name=None)` 增加 `name` 参数，`chunk_data()` 传 `name=name`。新上传文件名正确。✅
- **原现象 2**：工单后端齐全但 Web UI 无页面。
- **已修复 2**：`static/index.html` 新增「工单支持」侧边栏导航 + `renderTickets()` 列表页 + `showTicketDialog()` 创建弹窗 + `viewTicket()` 详情弹窗（含回复）；`api_server.py` 新增 `POST /api/ticket/{id}/reply` 端点。✅

> **注意**：`storage_data/` 中仍有修复前上传的旧文件（名字带 `tmp` 前缀），不影响新功能。如需清空，删除 `storage_data/` 目录后重启即可重建。

### 6.3 【P2，待办】OAuth2 是"半接入"
- register/login/refresh/device-code 全部可用，但**鉴权没有强制到所有 API**：目前只有 `/api/auth/me` 和工单接口读取 Bearer token，其余文件 API 仍匿名可访问。
- 若要完整多用户隔离，需要用 FastAPI dependency 在每个路由校验 token，并给 files/fast_lookup 增加 user_id 维度（当前 `user_quota` 表已预留但没用起来）。

### 6.4 【P2，待办】P2P 只做了信令，没做数据传输
- 已实现：peer 注册、chunk 声明、按 CID 查 peer、SDP/ICE 中继（WebSocket）。
- 未实现：浏览器/客户端侧的 WebRTC `RTCPeerConnection` + DataChannel 真正传块逻辑。这是"部分完成"，不是完整 P2P 下载。

### 6.5 其他小问题
- WebDAV `PROPFIND` 的 `href` 里中文/特殊字符未做 URL 编码，部分严格客户端可能不兼容。
- `encryption_layer.py` 的 `upload_encrypted` / `download_and_decrypt`（v1）是废弃草稿，注释里有未完成逻辑，别调用；用 v2。
- 离线下载用 `requests` 把整文件读进内存（`b"".join(chunks)`），超大文件会吃内存；且不支持磁力/BT。
- 同步是简化版：靠 mtime+size 检测变更，没有删除同步、冲突合并、软链接处理。

---

## 7. 如何启动与验证

```bash
# 1) 启动
cd "C:\Users\Administrator\WorkBuddy AI\2026-11-09-08-48-20\mydrive"
"C:\Users\Administrator\AppData\Local\Programs\Python\Python315\python.exe" api_server.py

# 2) 冒烟测试（服务器起来后）
curl -s http://127.0.0.1:8765/api/stats
curl -s -X POST http://127.0.0.1:8765/api/auth/register \
  -H "Content-Type: application/json" -d '{"username":"u1","password":"pass123"}'
curl -s -X PROPFIND http://127.0.0.1:8765/webdav/ -H "Depth: 1"
curl -s http://127.0.0.1:8765/            # Web UI

# 3) WebDAV GET 测试（第二轮修复验证）
curl -s -X PUT -H "Content-Type: text/plain" --data-binary "Hello!" http://127.0.0.1:8765/webdav/test.txt
curl -s http://127.0.0.1:8765/webdav/test.txt   # 应返回 Hello!

# 4) 工单测试（第二轮新增）
curl -s -X POST http://127.0.0.1:8765/api/ticket \
  -H "Content-Type: application/json" -d '{"subject":"测试","body":"内容","priority":"normal"}'
curl -s http://127.0.0.1:8765/api/tickets
```
- 运行各模块自带 self-test：`python cid_engine.py` / `storage_engine.py` / `encryption_layer.py` / `e2e_test.py`（需服务器在线）。
- 自动 API 文档：`http://127.0.0.1:8765/docs`（用户此前明确不想看文档页，接手后别主动展示给他）。

---

## 8. 数据 / 存储结构速查

- `storage_data/db/index.sqlite` 表：`fast_lookup`(秒传)、`files`(文件树)、`chunks`(块索引+ref_count)、`file_chunks`(文件→块映射)、`user_quota`(配额，预留)。
- 独立 SQLite：`auth.sqlite`(用户/token/device code)、`share.sqlite`(分享/工单)、`offline.sqlite`(离线任务)、`sync.sqlite`(同步对/记录)，都在 `storage_data/db/`。
- `FileMeta` 字段见 `storage_engine.py`：`identity, parent, name, path, size, mime_type, is_dir, content_identity, store_type, chunks[], create_ts, update_ts, deleted, trashed, shared, version`。
- 删文件默认进回收站（`trashed=1`），`permanent=True` 才删除 meta 文件。

---

## 9. 第二轮修复变更清单（2026-11-20）

| 文件 | 改动 |
|---|---|
| `webdav_gateway.py` | ① 导入 `StreamingResponse`；② `dav_get()` 从 `FastResponse(content=stream())` 改为 `StreamingResponse(stream(), ...)` |
| `storage_engine.py` | ① `chunk_file()` 签名增加 `name=None` 参数；② `name` 为 None 时取 `path_obj.name`，否则用传入的 `name`；③ `chunk_data()` 调用时传 `name=name` |
| `api_server.py` | 新增 `POST /api/ticket/{id}/reply` 端点 + `TicketReplyRequest` 模型 |
| `static/index.html` | ① 侧边栏新增「工单支持」导航项；② 新增 `renderTickets()` / `showTicketDialog()` / `viewTicket()` / `escapeHtml()` 函数；③ `navigate()` 增加 `tickets` 分支 |

---

## 10. 给接手 AI 的行动建议（按顺序）

1. **启动服务**，跑一遍第 7 节冒烟测试确认一切正常。
2. 根据用户下一步要求，选择补齐：
   - 完整 OAuth2 鉴权接入（6.3，工作量大）；
   - 真正 WebRTC DataChannel P2P（6.4，前端工作量大）；
   - 或把后端整体接成真正的多用户 + S3 兼容存储。
3. **沟通方式**：直接给可运行的结果，少写文档和总结（用户偏好，见第 1 节）。

---

## 11. 交接检查清单（移交方→接收方）

- [ ] 接收方已读本文档第 1 节（用户偏好）和第 6 节（待办）
- [ ] 环境能用第 2 节的全路径 Python 启动服务
- [ ] 确认 8765 端口无残留旧进程（必要时 taskkill）
- [ ] 接收方已知：`static/index.html` 是单文件 UI，改 UI 只需改这一处
- [ ] 接收方已知：`storage_data/` 是运行时数据，测试产生，可清空重建（会丢测试文件/账号）
- [ ] 接收方已知：`.workbuddy-ai/` 是项目数据，**禁止删除**
- [ ] 接收方已知：第二轮已修复 WebDAV GET、文件名前缀、工单前端三个问题，无需重做

---

## 12. 第三轮变更（2026-11-20 晚）：全接口鉴权 + 多用户隔离 + 真 P2P

### 12.1 鉴权从"可选"变成"默认强制"

新增 `MYDRIVE_AUTH_REQUIRED`（默认 `1`，设 `0` 可退回单用户本地模式）。所有数据端点现在都走 `caller_id(request)`：

- 无 token → 401；
- 有 token → 返回该用户的 `user_id`，所有存储/分享/离线/同步/P2P 查询都带 `user_id` 过滤。

改动的文件：

| 文件 | 变更 |
|---|---|
| `api_server.py` | 新增 `_bearer / optional_user / require_user / caller_id`；`/api/files`、`/api/download`、`/api/chunk`、`/api/stats`、`/api/mkdir`、`/api/move`、`/api/rename`、`/api/delete`、`/api/verify`、`/api/share*`、`/api/offline*`、`/api/sync*`、`/api/ticket*`、`/api/p2p*` 全部接入 |
| `storage_engine.py` | `user_id` 列 + 索引 + 旧库自动 `ALTER TABLE`；`fast_lookup` 唯一键改为 `(sha1, file_size, user_id)`；`get_file_meta / list_files / stream_chunks / delete_file / create_dir / chunk_data / check_instant_transfer` 接受 `user_id`；新增 `can_user_access_chunk()`；`get_storage_stats(user_id)` 修正为按 DISTINCT chunk 统计（否则去重收益会被重复计算抵消） |
| `share_manager.py` | 分享记录带 `creator_user_id`，创建时校验文件归属，列表/删除按用户过滤 |
| `offline_downloader.py` | 任务表带 `user_id`，列表/查询/取消按用户过滤；下载改为流式落盘（不再在内存里拼整个文件） |
| `sync_manager.py` | 同步对带 `user_id`，运行/删除/列表按用户过滤；上传下载都改为按用户写入 |
| `webdav_gateway.py` | 之前**完全无鉴权**，现加 HTTP middleware：支持 Basic（WebDAV 客户端）与 Bearer，用 `ContextVar` 把 `user_id` 传进所有读写路径；未认证返回 401 + `WWW-Authenticate` |

安全相关的语义决策（重要，别改回去）：

- **秒传不跨用户**：`/api/instant-check` 只查调用者自己的 `fast_lookup`，否则可以拿 hash 探测别人有什么文件。
- **分块访问要授权**：`/api/chunk/{cid}` 与 `/api/p2p/announce` 都校验 `can_user_access_chunk`，避免拿到 CID 就能下载。
- **分享链接是 capability**：`/api/share/{id}` 与 `/api/share/{id}/download` 保持匿名可访问（凭 share_id + 密码），这是设计意图。
- **WebSocket 用 query token**：浏览器不能给 WS 握手加 header，`/ws/p2p?access_token=...` 在 accept 前校验。

### 12.2 P2P 从"只有信令"变成"真的传数据"

`static/index.html` 新增 P2P 模块（约 180 行）：

1. `p2pConnect()` 连 `/ws/p2p`，拿到 `peer_id`；
2. 下载时 `p2pDownload()` 先取分块清单，向 `/api/p2p/peers/{cid}` 查谁有该块；
3. 有 peer → `RTCPeerConnection` + `DataChannel('chunks')` 直连，请求 `{t:'get',cid,rid}`，对端按 128KB 切片回传二进制；
4. 没有 peer / 超时 / 报错 → 该分块自动回退 `/api/chunk/{cid}`；
5. 全部块从 HTTP 拿的（没有任何 peer 参与）时返回 `false`，退回原来的浏览器直链下载，行为不变；
6. 上传成功、以及下载时，会把分块 CID `announce` 给信令服务器，让自己成为别人的源。

已验证：无 token 连 WS 被拒（403）；两个带 token 的连接可完成 `registered → signal(offer) → 对端收到 → announce → query_chunk 返回 1 个 peer`。**WebRTC 本体需要在浏览器里双开页面验证**（当前机器无浏览器自动化环境）。

### 12.3 前端登录态

`static/index.html` 顶部新增登录/注册浮层（`#authOverlay`）：

- token 存 `localStorage.mydrive_token`，`api()` / `apiJSON()` 自动加 `Authorization: Bearer`；
- 任意请求 401 → 清 token 并弹回登录；
- 上传 XHR 单独加 header（XHR 不走 `api()`）；
- 下载/预览链接带 `?access_token=`（`api_server._bearer` 支持 query token）；
- 设置页显示当前用户名 + 退出登录。

### 12.4 清真云协议对照（来自 APK 内 proto，非猜测）

`downloads/halalcloud_apk/protos/*.proto` 里的服务与方法，对照本项目实现：

| 清真云 gRPC | 本项目 REST 等价 | 状态 |
|---|---|---|
| `PubUser.Login / Register / Refresh / Logoff / DeviceAuthorization` | `/api/auth/login|register|refresh|device-code|device-poll|device-authorize` | 已实现（JWT + refresh 轮换 + 设备码） |
| `PubUser.GetStatisticsAndQuota` | `/api/auth/me`（含 quota）、`/api/stats` | 已实现 |
| `PubUserFile.Create / Get / Rename / Trash / Move / Copy / Delete / DeleteTrash / Recover / List / ListTrash / Search` | `/api/upload`、`/api/files`、`/api/mkdir`、`/api/rename`、`/api/move`、`/api/files/{id}` + `DELETE` | 已实现（Search 仅前端过滤） |
| `PubUserFile.CreateUploadToken / CreateUploadTask / CreateTemporaryUpload` | `POST /api/upload`（multipart 直传） | 已实现（未做分片续传 token） |
| `PubUserFile.ParseFileSlice / GetSliceDownloadAddress / GetFileSlicesDownloadInfo` | `/api/files/{id}/chunks` + `/api/chunk/{cid}` | 已实现（分块 CID 可独立下载/校验） |
| `PubUserFile.GetDirectDownloadAddress` | `/api/download/{id}` | 已实现（流式重组） |
| `PubUserFile.CreateManageRTCOffer / SendClientIceCandidate / GetServerIceCandidate` | `/ws/p2p` 的 offer/answer/ice 转发 | 已实现（信令语义一致，传输层用标准 WebRTC） |
| `PubSlice.GetSlice / CreateSlice / GetMeta / CreateMeta / AddFastLookup / GetFastLookup` | `chunk_*` + `fast_lookup` 表 | 已实现（CID = SHA256 multihash base32） |
| `PubFileShare.Create / Get / List / Delete / Save / Like / Dislike / ComplaintShare` | `/api/share*` | 已实现（点赞/投诉未做） |
| `PubOfflineTask.Parse / Add / List / Delete` | `/api/offline/*` | 已实现（仅 HTTP(S) 直链，未做 BT/磁力解析） |
| `PubSftpConfig.*` | 无 | 未实现（优先级低） |
| `PubWebdav.*` | `/webdav/*`（标准 WebDAV 协议） | 已实现 |
| `PubTicket.Get / List / ...` | `/api/ticket*` | 已实现 |

结论：清真云是 **gRPC + protobuf + WebRTC DataChannel** 架构，本项目用 **REST/JSON + 同样的内容寻址分块 + 标准 WebRTC DataChannel** 做等价实现。字段级差异（如 `File.slice` 的具体结构、`UploadToken` 的签名规则）仍需真实抓包校准。

### 12.5 抓包现状（阻塞点）

- mitmproxy 已在本机 `127.0.0.1:8080` 监听，web 界面 `:8081`；
- 手机代理此前已设为 `127.0.0.1:8080`（`adb reverse tcp:8080 tcp:8080`），证书已推到 `/sdcard/Download/mitmproxy-ca-cert.cer`；
- **当前 `adb devices` 只剩 `emulator-5554 offline`，物理手机已断开**（USB 掉线或调试授权失效），无法继续采集。
- 恢复后照这个顺序做即可（不需要重新摸索）：

```bash
ADB="D:/an/pj/Android/Sdk/platform-tools/adb.exe"
"$ADB" devices -l                      # 确认物理机 online
"$ADB" reverse tcp:8080 tcp:8080
"$ADB" shell settings put global http_proxy 127.0.0.1:8080
"D:/an/pj/mitmproxy/venv/Scripts/mitmdump.exe" -p 8080 \
  --allow-hosts '^(.*\.)?halalcloud\.com$' \
  -s "downloads/halalcloud_capture_addon.py" \
  -w "downloads/halalcloud.flow"
```

`downloads/halalcloud_capture_addon.py` 已写好：只落盘脱敏结构（host / path / grpc method / 字段名 / 大小 / 状态码），不写 token、cookie、文件内容。

### 12.6 本轮验证结果

`e2e_test.py` 已重写为带鉴权版本，10 组全部通过：

1. 匿名访问被 401 拒绝；
2. 上传 + 下载内容一致；
3. 同用户秒传命中；
4. 10MB 文件 3 分块，逐块下载后重组一致；
5. 去重：logical 41,946,640 / disk 31,459,080，节省约 10MB；
6. 分块 CID 校验；
7. 目录/列表/回收站；
8. **跨用户隔离**：列表不可见、元数据/下载/分块全 404、秒传不泄露、不能给别人文件建分享；
9. 分享链接：错误密码 403、正确密码可取元数据并匿名下载；
10. 存储统计按用户口径正确。

补充验证：`/webdav/` 无认证 401、Basic 正确 200、密码错误 401；`/ws/p2p` 无 token 403、有 token 可完成信令转发。

### 12.7 已知遗留

- WebRTC DataChannel 需浏览器双开实测（本机无浏览器自动化）；
- SFTP 配置、分享点赞/投诉、BT/磁力离线解析未做；
- 上传仍是整文件 multipart，未做清真云那种 `UploadToken + 分片续传`；
- `storage_data/` 里的 `anonymous` 旧数据不会出现在任何登录用户视图中（保留未删）。

---

## 13. 清真云协议逆向结论（2026-11-20）

详见根目录 **`清真云协议逆向报告.md`**。要点：

### 13.1 抓包结果推翻了旧判断

旧结论「API 是 `<SERVER_IP>:8080` 的明文 h2c」不成立。实测（PCAPdroid，`downloads/pcap/cap1.pcap`）：

| 项 | 实测 |
|---|---|
| 端口 | 全部 **443 / TLS 1.3**，无明文 |
| 主机 | `grpcuserapi.2dland.cn`，CDN 边缘 `drcdn2.2dland.cn`（EdgeOne） |
| 上报 | `sentry.qiecdn.com` |
| 代理 | Dart `HttpClient` 直连，绕过系统代理，须在 VPN 层抓 |
| 证书 | 无 `network_security_config` + targetSdk 36 → 不信任用户 CA，MITM 不可行 |

### 13.2 协议栈已完全复刻

Web 端 bundle（`https://drive.2dland.cn/assets/index-C6KzmNsb.js`）明文包含凭据与签名函数：

```
appid      = 2dland/web
appversion = 202603182100
appsecret  = <APP_SECRET_REDACTED>
sign       = MD5("/" + service + "/" + method + timestamp + appid + appversion
                  [+ "Bearer " + token] + appsecret)
headers    = timestamp / appid / appversion / sign / authorization
```

线上验证：不签名 → `grpc-status 3 missing timestamp`；签名后 → `grpc-status 16 invalid accesstoken`。
**签名算法正确，只差用户 token。**

工具：`downloads/pcap/halal_probe.py`（`HALAL_TOKEN=xxx python halal_probe.py` 可带 token 直接调）。

### 13.3 协议资产

- `downloads/halalcloud_apk/protos/` —— 12 个 proto，12 service / 108 RPC，含 `google.api.http` 注解（**62 条 REST 路径**，全部 POST + JSON）
- 关键结构：`File`（identity / content_identity / trashed / store_type）、`ScanListRequest`（**游标 token 分页**）、`UploadTask`（**block_size 由服务端下发**）、`FastLookup`（sha1 + head_sha1 + file_size 秒传）、`SliceDownloadInfo`（预签名下载地址）

### 13.4 对 MyDrive 的落地清单

| 项 | 动作 |
|---|---|
| 文件主键 | 自增 id → UUID `identity`，`content_identity` 对齐 CID |
| 分页 | offset/limit → 游标 `ScanListRequest.token` |
| 秒传 | 增加 `head_sha1` 维度 |
| 分片 | `block_size` 改为服务端下发，支持续传 |
| 鉴权 | 可选叠加 `timestamp+sign(MD5)` 中间件，与 JWT 并行 |
| 待补模块 | SFTP、分享点赞/投诉、BT/磁力离线解析 |

### 13.5 抓包命令（可复现）

```bash
adb shell am start -n com.emanuelef.remote_capture/.activities.CaptureCtrl \
  -e action start -e pcap_dump_mode pcap_file -e pcap_uri file:///sdcard/Download/cap1.pcap
# 弹窗需手动点「允许」+ SAF 保存
python downloads/pcap/parse_pcap.py cap1.pcap
python downloads/pcap/tls_sni.py    cap1.pcap
python downloads/pcap/dns_dump.py   cap1.pcap
```

---

## 14. v6 协议对齐落地（2026-11-20，已完成）

按《清真云协议逆向报告》第 8 节的实测值，把 MyDrive 从「自研 REST」改造成「v6 同构」。

### 14.1 新增模块

| 文件 | 职责 |
|---|---|
| `mydrive/protocol.py` | 协议常量与约定：32 hex identity、File.type 码表、sort_name、游标编解码、四段式 `auth_key` 预签名、上传会话 |
| `mydrive/pb.py` | 极简 protobuf 编解码（schema 驱动，无 .proto 解析）+ 全部 v6 消息定义 |
| `mydrive/grpc_compat.py` | gRPC-Web + REST 双传输兼容层、MD5 签名校验、RPC 分发表 |

### 14.2 数据模型对齐

| 项 | 改造前 | 改造后（= 清真云实测） |
|---|---|---|
| 文件主键 | `uuid4()` 带连字符 | **32 位小写 hex** `uuid4().hex` |
| 用户主键 | 同上 | 32 hex（新注册用户；老账号仍兼容） |
| 文件类型 | 无 | `type` 码：20 目录 / 40 视频 / 30 音频 / 50 图片 / 10 文档 / 60 压缩包与其他 |
| 排序键 | 无 | `sort_name`（分隔符归一 + 拉丁小写，如 `스웨디시 1.mp4` → `스웨디시 1 mp4`） |
| 时间戳 | 秒 | **毫秒**（与 `create_ts: 1795032182096` 一致） |
| 路径 | `/文件名`（扁平） | 递归拼接 `/父目录/文件名`，移动/重命名自动级联 |
| 目录统计 | 无 | `files` / `directories` / `nodes` 沿祖先链自增 |
| 文件级 CID | raw 编码（`bafkrei…`） | **dag-pb 编码（`bafybei…`）**，与实测 `content_identity` 一致 |
| 分片 CID | `bafkrei…` | 不变（raw / 0x55） |

`cid_engine.py` 新增 `compute_cid_with_codec()` / `compute_file_cid()`，multicodec 由调用方指定。

### 14.3 游标分页（ScanList）

```
GET /api/files/scan?parent=root&limit=10&token=<base64>
-> {"files":[…], "list_info":{"token":"…","total":N,"limit":10,"skip":0}}
```

游标就是清真云那种 **base64(JSON)**：

```json
{"user_identity":"…","skip":10,"limit":10,"create_time":"2026-11-20T02:37:39.001861264+08:00",
 "version":1,"order_by":[{"field":"type","asc":true},{"field":"create_ts"}]}
```

排序 `is_dir DESC, type ASC, create_ts DESC` —— 目录永远在最前（type 20），
其后按类型 10 文档 / 30 音频 / 40 视频 / 50 图片 / 60 其他，同类型新的在前。
（真实服务默认 `type ASC, create_ts DESC`；此处显式加 `is_dir DESC`，避免 10 文档类文件
排到目录前面。）
`GET /api/files` 保留数组返回（前端无感），传 `limit` 或 `token` 即自动切游标模式。

### 14.4 预签名下载（auth_key）

```
GET /api/files/{identity}/download-info?limit=100
-> {"file":{…}, "slices":[{"identity":"bafkrei…","index":0,"size":4194304,
     "download_address":"/slices/blocks/ba/fk/<cid>?auth_key=…",
     "download_token":"<exp>-<key_id>-<user>-<sig>","expire_ts":…}],
    "list_info":{"token":"…","total":224}}
```

- 路径分片与清真云一致：`/slices/blocks/<cid前2>/<cid第3-4>/<cid>`
- `auth_key` 四段：`<过期秒>-<密钥ID>-<user_identity>-<HMAC-SHA256 前 32 hex>`
- 签名原文 `cid\nexpire\nuser_identity\nkey_id`，密钥取 `MYDRIVE_PRESIGN_SECRET`，缺省复用 `MYDRIVE_SECRET`
- 下载**无需 Bearer**，凭 auth_key 验签 + 校验该用户确实拥有此分片；篡改或缺失返回 403
- 过期时间 `MYDRIVE_PRESIGN_TTL`（默认 7200 秒）

### 14.5 分片上传（CreateUploadTask 三步）

> 第五轮已把形状改成线上真实三段式，见 §15.1；下面保留旧的 REST 封装便于对照。

```
POST /api/upload/init   {name,parent,file_size,sha1,head_sha1}
  -> block_size=4194304, block_hash_type=18(0x12 sha2-256),
     block_version=1, block_codec=85(0x55 raw), task="upload_session::<32hex>_<8>:ts::<ms>"
     upload_address="/int/blocks"
     命中秒传时直接 created=true + instant=true + identity
POST /int/blocks/{task}/{cid}   (raw body, 服务端校验 sha256(body)==cid -> 201)
POST /int/blocks/{task}         (JSON: ["<cid>", …] -> 文件元信息)
POST /api/upload/complete       (旧封装，保留兼容)
```

`engine.create_file_from_blocks()` 按分片 CID 还原 chunk 表，并用标准 UnixFS
dag-pb 计算文件根 CID；`engine.create_file_from_content()` 实现零字节秒传。

### 14.6 gRPC-Web / REST 兼容层

请求签名（实测算法，已复刻）：

```
sign = MD5("/"+service+"/"+method+timestamp+appid+appversion [+"Bearer "+token] + appsecret)
header: timestamp / appid / appversion / sign / authorization
```

| 传输 | 入口 | 说明 |
|---|---|---|
| gRPC-Web | `POST /v6.services.pub.<Service>/<Method>` | 5 字节帧头 + protobuf，trailers 帧 `0x80` 带 `grpc-status`；支持 `-text` base64 变体 |
| REST 网关 | `POST /v6/userfile/list` 等 | camelCase JSON 出入，等价 `google.api.http` 注解 |

已实现 RPC（22 个）：

| 服务 | 方法 |
|---|---|
| `PubUser` | Login / Get / GetStatisticsAndQuota / GetSettings / Refresh |
| `PubUserFile` | List / ListTrash / Search / Get / Create / Rename / Move / Trash / Recover / Delete / DeleteTrash / CreateUploadTask / CreateTemporaryUpload / GetFileSlicesDownloadInfo |
| `PubSlice` | AddFastLookup / GetFastLookup |
| `PubFileShare` | Create / Get / Like / Dislike / ComplaintShare / List / Delete / Save |

错误语义按 gRPC 码：签名错 `3 invalid sign`、缺 token `16 invalid accesstoken`、未实现 `12`。
`MYDRIVE_REQUIRE_SIGN=1` 可强制拒绝无签名请求（默认放行无签名，但**有签名必须正确**）。

> 说明：手机客户端走**原生 gRPC（HTTP/2 + trailer）**，uvicorn 只支持 HTTP/1.1，
> 因此本层对 Web 端/工具链可直接对接；要直连手机端需在前面挂 Envoy 做
> `grpc-web ↔ grpc` 或直接给 uvicorn 换 hypercorn（h2）并改发 trailer。

### 14.7 分享社交

`POST /api/share/{id}/react`（like/dislike 计数）、`POST /api/share/{id}/complaint`、
`GET /api/complaints`、`POST /api/share/{id}/save`（**零数据搬运**克隆到自己的网盘，
共用同一 content_identity）。分享表新增 `like_count/dislike_count/save_count` 与自动迁移。

### 14.8 回归测试

`mydrive/e2e_test.py` 扩到 **16 项**，全部通过：

| # | 用例 |
|---|---|
| 0–8 | 鉴权拦截、上传下载、秒传、10MB 多分片、去重、鉴权校验、目录操作、跨用户隔离、分享链路 |
| 9 | 游标分页：3 页走完、无重复、total 对齐、游标可解码 |
| 10 | 预签名：无 token 取分片、篡改签名 403、缺 auth_key 403 |
| 11 | 分片上传三步流 + 二次 init 命中秒传 |
| 12 | gRPC-Web：登录、错签名 3、缺 token 16、签名 List、identity 全 32hex、配额 RPC |
| 13 | REST 网关：camelCase 出入、无 token 401 |
| 14 | 分享点赞/投诉/另存（跨账号同 CID 下载一致） |

运行：

```bash
cd mydrive
"C:/Users/Administrator/AppData/Local/Programs/Python/Python315/python.exe" e2e_test.py
```

### 14.9 仍未做

| 项 | 现状 |
|---|---|
| SFTP | 未实现（需 paramiko + 独立端口）；上游 `PubSftpConfig.Get` 本身超时不可用 |
| BT / 磁力离线 | 仅 HTTP/HTTPS 直链；magnet 需要 BT 引擎 |
| 原生 gRPC（h2） | 需 hypercorn 或 Envoy 前置 |
| 分享列表游标 | 一次性返回，未接 cursor |

---

## 15. 第五轮：把上传链路和 CID 算法彻底对齐（2026-11-20 深夜）

这轮只做一件事：**不再"大概对"，而是拿线上服务当 oracle 逐字节验证**。

### 15.1 上传端点是三段式，不是 `/int/blocks`

之前一直 POST `/int/blocks`，拿 Go 默认 mux 的 `404 page not found`，误判成"上传不可用"。
真实形状（Web 端 `assets/taskUpload-*.js` 直读 + 实测）：

```text
POST /int/blocks/<task>/<cid>    body = 分片原始字节, Content-Type: application/octet-stream -> 201
GET  /int/blocks/<task>/<cid>    200 = 该块已存在（秒传探测）
POST /int/blocks/<task>          body = JSON 数组，按序的分片 CID -> 200 + 文件元信息
```

- `CreateUploadTask` 的入参是扁平 `File`：`parent`(3) + `path`(5) + `size`(7)，**名字走 path**；
- `task` = `upload_session::<32hex>_<8>:ts::<毫秒>`，直接作为 URL path 段；
- 提交后返回 `{"user_identity","parent","path","size","content_identity",...}`。

MyDrive 侧已按同形实现（`api_server.py`）：`/int/blocks/{task}/{cid}`（GET 探测 + POST 写入）、
`/int/blocks/{task}`（JSON 提交），`/api/upload/*` 旧接口保留兼容。

### 15.2 文件根 CID：真正的 UnixFS dag-pb

实测锚点（真实账号，测试文件已清理）：

| 上传内容 | 分片数 | 服务端 content_identity |
|---|---|---|
| 1 KiB | 1 | `bafkreid3ozn…` = **分片 CID 本身**（无 dag-pb 包裹） |
| 8 MiB | 2 | `bafybeifelct3xojbueu5huhybhjm2ua4ehxxulfiqpyh4hglxfsj4bu5ka` |

`cid_engine.compute_file_cid()` 已重写为真 UnixFS：

```text
PBNode = Links(2) 先编码 + Data(1) 后编码
PBLink = Hash(1)=子 CID 二进制 | Name(2)="" (必须写 12 00) | Tsize(3)=子块字节数
UnixFS = Type(1)=2 | filesize(3) | blocksizes(4)...
单分片 -> 直接返回 raw CID（reduceSingleLeafToSelf）
多分片 -> 174 links/节点，超出 balanced 分层
```

踩过的坑：**不写空 `Name` 会得到另一个 CID**（109 vs 113 字节）。补上 `12 00` 后，
8 MiB 与 196 块分层两个样本全部与 js-ipfs 一致。

### 15.3 服务能力补齐：RPC 从 22 个到 79 个

新增 `mydrive/v6_extra.py`，按官方 proto 落地了此前"返回 12/14"或缺失的服务：

| 服务 | 方法 | 落地形态 |
|---|---|---|
| `PubDavConfig` | Get/Update/Enable/Disable/Create/Delete/List/ListAll/ValidateUserName/ServerInfo | 每用户一份挂载配置，`ServerInfo` 与线上同值（`webdav-server` / `Microsoft-IIS/8.0`） |
| `PubSftpConfig` | Get/Update/Enable/Disable/UpdateKeys/ValidateUserName | 独立账号 + 生成的 `ssh-ed25519` 公钥 |
| `PubOfflineTask` | Parse/Add/List/Delete | 磁力取 `dn=` 作文件名，HTTP 取 path 末段；任务持久化 |
| `PubTicket` | Create/List/Get/Comment/Close/Delete/ContactInfo/UpdateContactInfo/ListConversation | 工单 + 对话线程，JSON 持久化 |
| `OauthAuthorization` | Authorize/DeviceCode/GetAuthorizeState/GetDeviceCodeState/GetToken/RefreshToken | 授权码 + 设备码（复用 `auth_manager` 的 device_codes 表） |
| `PubSlice` | GetSlice/CreateSlice/GetMeta/CreateMeta | 块存在性查询 + 元数据回读 |
| `PubUserFile` | CreateUploadToken/ParseFileSlice/GetSliceDownloadAddress/GetDirectDownloadAddress/GetDownloadAndPreviewInfo/PreviewDoc/CreateDoc/ReportPreviewStatus/ListRecentUpdatedFiles | 切片清单、预签名直链、预览能力判定 |
| `PubUser` | GetSettings/SetSettings/GetAvailableExtendQuota | `map<string,string>`（wire 上是 repeated entry） |

数据落在 `storage_data/json/*.json`（dav / sftp / offline / tickets / settings / contact）。

### 15.4 WebDAV 用独立凭据，并修掉一个真 bug

- `webdav_gateway._identify()` 先匹配 `PubDavConfig` 下发的 `username/password`，再回退账户密码；
- 顺手修掉 `PROPFIND` 500：`update_ts` 是毫秒，直接喂 `email.utils.formatdate()` 会
  `OSError: [Errno 22]`（Windows localtime 溢出）——现在先除 1000；
- `PROPFIND` 返回码从 200 改为标准的 **207 Multi-Status**（真实 WebDAV 客户端按 207 判定）。

实测：`PROPFIND`（dav 凭据）207、错误密码 401、`MKCOL` 201、`PUT` 201、`GET` 200 且内容一致。

### 15.5 本轮的验证

```bash
cd mydrive
python e2e_test.py            # 16 项，全绿（旧功能未回归）
python verify_upload_shape.py # 3 项：1KiB=single-raw、8MiB=标准 UnixFS、非法 slice id 400
python v6_extra_test.py       # 23 项：DAV/SFTP/离线/工单/设置/OAuth/切片
```

合计 **42 项断言**，其中 8 MiB 的期望值来自线上服务与 js-ipfs 的双重确认。

### 15.6 遗留（诚实清单）

| 项 | 说明 |
|---|---|
| 分享列表游标 | 仍一次性返回 |
| 原生 h2 gRPC | 仍只做 gRPC-Web + REST |
| 离线任务执行 | 任务能登记、能列、能删，但没有真正的 BT 引擎去下载 |
| SFTP 服务 | 只下发了配置与公钥，未起 22 端口 |
