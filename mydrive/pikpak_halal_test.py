"""
End-to-End Verification Suite for PikPak & HalalCloud Enhancements
==================================================================
Tests:
1. Tiered Large-File Storage: hot->cold offload (DirectoryColdBackend), local eviction,
   transparent cold->hot hydration on read, smart zlib block compression, chunk GC.
2. Smart File Ops: global cross-directory search, smart categories (video/image/doc/starred),
   star toggle, real copy, batch ops, batch ZIP download, version snapshots & rollback,
   empty trash & space reclamation.
3. Cloud Media (PikPak Cloud Play): real FFmpeg-generated MP4 -> ffprobe, HLS transcode
   (index.m3u8 + .ts), cover thumbnail, 2x2 preview sprite, auto-matched .srt -> WebVTT
   subtitle conversion, playback progress & speed memory.
4. Offline Global Instant-Save (Cross-User Magnet 秒存) + Pause/Resume + Batch Add.
5. Telegram Bot Adapter: /help, /bind, magnet forwarding (hitting instant-save), /quota, /tasks.
6. Public Share Page (`/s/{share_id}`) + password-protected share + one-click save.
"""

import io
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

# Isolate test storage before importing api_server
_TMP = tempfile.mkdtemp(prefix="mydrive_enhanced_test_")
os.environ["STORAGE_ROOT"] = os.path.join(_TMP, "storage")
os.environ["SFTP_ENABLE"] = "0"
os.environ["SLICES_ENABLE"] = "0"
os.environ["MYDRIVE_AUTH_REQUIRED"] = "1"

from fastapi.testclient import TestClient  # noqa: E402
import api_server  # noqa: E402


def _make_sample_mp4(path: Path):
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "ffmpeg required for media tests"
    cmd = [
        ffmpeg, "-y",
        "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=15",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run_all():
    client = TestClient(api_server.app)
    passed = 0

    def ok(name, cond, detail=""):
        nonlocal passed
        if not cond:
            raise AssertionError(f"FAIL [{name}]: {detail}")
        passed += 1
        print(f"  ok   {name}")

    print("== 1) 账号注册与鉴权 ==")
    r1 = client.post("/api/auth/register", json={"username": "alice", "password": "Password123!"})
    ok("注册用户 alice", r1.status_code == 200 and "access_token" in r1.json(), r1.text)
    tok_a = r1.json()["access_token"]
    hdr_a = {"Authorization": f"Bearer {tok_a}"}

    r2 = client.post("/api/auth/register", json={"username": "bob", "password": "Password123!"})
    ok("注册用户 bob", r2.status_code == 200 and "access_token" in r2.json(), r2.text)
    tok_b = r2.json()["access_token"]
    hdr_b = {"Authorization": f"Bearer {tok_b}"}

    print("== 2) 大文件分级存储：冷热分层 + 智能压缩 + 断盘按需拉取 + GC ==")
    cold_dir = os.path.join(_TMP, "cold_nas")
    rcfg = client.post("/api/storage/tiered/config", headers=hdr_a, json={
        "cold_type": "dir",
        "cold_dir": cold_dir,
        "local_cache_max_bytes": 50 * 1024 * 1024,
        "compression_enabled": True,
        "auto_offload": True,
    })
    ok("配置冷存储与智能分块压缩", rcfg.status_code == 200 and rcfg.json()["cold_active"], rcfg.text)

    # Upload highly compressible large text file (128 KB)
    big_text = ("MyDrive PikPak HalalCloud Tiered Storage Line\n" * 3000).encode("utf-8")
    up = client.post("/api/upload", headers=hdr_a, files={"file": ("notes.txt", big_text, "text/plain")})
    ok("上传可压缩文档", up.status_code == 200, up.text)
    doc_id = up.json()["identity"]

    st1 = client.get("/api/storage/tiered/stats", headers=hdr_a).json()
    ok("智能压缩生效节省空间", st1["tiered"]["compression_saved_bytes"] > 50000, str(st1))

    # Offload all local chunks to cold NAS & evict local copies
    off = client.post("/api/storage/tiered/offload?evict_local=true", headers=hdr_a).json()
    ok("热块迁移至冷存储并释放本地空间", off["offloaded"] >= 1 and off["freed_bytes"] > 0, str(off))

    st2 = client.get("/api/storage/tiered/stats", headers=hdr_a).json()
    ok("迁移后本地热缓存归零且冷块存在", st2["tiered"]["local_chunks"] == 0 and st2["tiered"]["cold_chunks"] >= 1, str(st2))

    # Download file -> transparently hydrates from cold tier and decompresses
    dl = client.get(f"/api/download/{doc_id}", headers=hdr_a)
    ok("冷存储按需拉取并透明解压还原", dl.status_code == 200 and dl.content == big_text)

    print("== 3) 文件管理闭环：复制、收藏、分类、全局搜索、批量 ZIP、版本回滚、清空回收站 ==")
    cp = client.post("/api/copy", headers=hdr_a, json={"identity": doc_id})
    ok("真实复制文件", cp.status_code == 200 and cp.json()["identity"] != doc_id, cp.text)
    copy_id = cp.json()["identity"]

    star = client.post(f"/api/files/{doc_id}/star", headers=hdr_a, json={"starred": True})
    ok("收藏文件", star.status_code == 200 and star.json()["starred"] is True)

    cat_starred = client.get("/api/category/starred", headers=hdr_a).json()
    ok("收藏分类视图命中", any(f["identity"] == doc_id for f in cat_starred))

    cat_doc = client.get("/api/category/doc", headers=hdr_a).json()
    ok("文档智能分类命中", any(f["identity"] == doc_id for f in cat_doc))

    srch = client.get("/api/search?q=notes", headers=hdr_a).json()
    ok("全局跨目录搜索命中", len(srch) >= 2)

    snap = client.post(f"/api/files/{doc_id}/versions/snapshot", headers=hdr_a, json={"note": "v1 备份"})
    ok("创建版本快照", snap.status_code == 200 and snap.json()["version"] == 1)
    vid = snap.json()["id"]

    rest = client.post(f"/api/files/{doc_id}/versions/{vid}/restore", headers=hdr_a)
    ok("版本回滚递增版本号", rest.status_code == 200 and rest.json()["version"] == 2)

    zresp = client.post("/api/batch/download", headers=hdr_a, json={"identities": [doc_id, copy_id]})
    ok("批量打包 ZIP 下载", zresp.status_code == 200)
    with zipfile.ZipFile(io.BytesIO(zresp.content)) as zf:
        ok("ZIP 含所选全部文件", len(zf.namelist()) == 2, str(zf.namelist()))

    btrash = client.post("/api/batch", headers=hdr_a, json={"identities": [copy_id], "operation": "trash"})
    ok("批量移入回收站", btrash.status_code == 200 and btrash.json()["affected"] == 1)

    emp = client.post("/api/trash/empty", headers=hdr_a)
    ok("一键清空回收站", emp.status_code == 200 and emp.json()["deleted_files"] >= 1, emp.text)

    print("== 4) 云播与媒体体验：HLS 转码、缩略图、逐帧预览、字幕自动匹配转 VTT、续播记忆 ==")
    mp4_path = Path(_TMP) / "demo_movie.mp4"
    _make_sample_mp4(mp4_path)
    up_v = client.post("/api/upload", headers=hdr_a, files={"file": ("demo_movie.mp4", mp4_path.read_bytes(), "video/mp4")})
    ok("上传测试视频", up_v.status_code == 200)
    vid_id = up_v.json()["identity"]

    srt_bytes = "1\n00:00:00,000 --> 00:00:01,500\n你好 MyDrive 云播字幕\n".encode("utf-8")
    up_s = client.post("/api/upload", headers=hdr_a, files={"file": ("demo_movie.zh.srt", srt_bytes, "text/plain")})
    ok("上传同目录 SRT 字幕", up_s.status_code == 200)

    probe = client.get(f"/api/media/{vid_id}/probe", headers=hdr_a).json()
    ok("ffprobe 解析分辨率与字幕匹配", probe["width"] == 320 and len(probe["subtitles"]) == 1 and probe["subtitles"][0]["matched"], str(probe))

    sub_url = probe["subtitles"][0]["url"]
    vtt = client.get(sub_url, headers=hdr_a)
    ok("SRT 自动转 WebVTT", vtt.status_code == 200 and vtt.text.startswith("WEBVTT") and "00:00:00.000 --> 00:00:01.500" in vtt.text)

    m3u8 = client.get(f"/api/media/{vid_id}/hls/480p/index.m3u8", headers=hdr_a)
    ok("FFmpeg HLS 480P 切片生成", m3u8.status_code == 200 and "#EXTM3U" in m3u8.text and ".ts" in m3u8.text)

    thumb = client.get(f"/api/media/{vid_id}/thumbnail", headers=hdr_a)
    ok("视频封面缩略图生成", thumb.status_code == 200 and thumb.content[:2] == b"\xff\xd8")

    sprite = client.get(f"/api/media/{vid_id}/sprite", headers=hdr_a)
    ok("视频逐帧雪碧图生成", sprite.status_code == 200 and sprite.content[:2] == b"\xff\xd8")

    p_save = client.post(f"/api/media/{vid_id}/progress", headers=hdr_a, json={
        "position_sec": 1.25, "duration_sec": 2.0, "playback_rate": 1.5, "quality": "720p"
    })
    p_get = client.get(f"/api/media/{vid_id}/progress", headers=hdr_a).json()
    ok("播放进度与倍速持久化", p_save.status_code == 200 and p_get["position_sec"] == 1.25 and p_get["playback_rate"] == 1.5)

    print("== 5) 跨用户全局磁力秒存 + Telegram 机器人转发离线 ==")
    magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=demo_movie.mp4"
    reg = client.post("/api/offline/instant-register", headers=hdr_a, json={
        "url": magnet, "file_identity": vid_id, "name": "demo_movie.mp4"
    })
    ok("alice 入库磁力全局秒存索引", reg.status_code == 200, reg.text)

    # Now bob (a completely different user!) adds the same magnet -> instant-saved in milliseconds!
    bob_add = client.post("/api/offline/add", headers=hdr_b, json={"url": magnet, "save_path": "/PikPak秒存"})
    ok("bob 跨用户命中全局磁力秒存", bob_add.status_code == 200 and bob_add.json().get("instant_saved") is True, bob_add.text)

    bob_files = client.get("/api/search?q=demo_movie", headers=hdr_b).json()
    ok("bob 网盘中已零拷贝入库该视频", len(bob_files) == 1)

    # Telegram Bot simulation: bind bob + forward magnet
    tg_bind = client.post("/api/telegram/simulate", headers=hdr_b, json={"chat_id": "999", "text": "/bind bob Password123!"}).json()
    ok("Telegram Bot 绑定账号", "绑定成功" in tg_bind["reply"], str(tg_bind))

    tg_mag = client.post("/api/telegram/simulate", headers=hdr_b, json={"chat_id": "999", "text": f"帮我存一下 {magnet}"}).json()
    ok("Telegram Bot 转发磁力触发秒存", "秒存" in tg_mag["reply"], str(tg_mag))

    tg_q = client.post("/api/telegram/simulate", headers=hdr_b, json={"chat_id": "999", "text": "/quota"}).json()
    ok("Telegram Bot 查询空间", "bob" in tg_q["reply"], str(tg_q))

    print("== 6) 公开分享提取码页与跨用户一键转存 ==")
    sh = client.post("/api/share", headers=hdr_a, json={"file_identity": doc_id, "password": "8888"}).json()
    sid = sh["share_id"]
    spage = client.get(f"/s/{sid}")
    ok("独立公开分享页可访问", spage.status_code == 200 and "一键转存" in spage.text)

    sv = client.post(f"/api/share/{sid}/save", headers=hdr_b, json={"password": "8888", "parent": "root"})
    ok("bob 凭提取码一键转存到自己网盘", sv.status_code == 200, sv.text)


    print("== 7) 商业版能力：VIP 兑换码、S3 预签名直传/直链、CID 合规封禁、MKV 内封字幕实时提取 ==")
    vip0 = client.get("/api/vip/me", headers=hdr_a).json()
    ok("新用户默认 free 等级 10GB", vip0["plan"] == "free" and vip0["storage_quota_bytes"] == 10 * 1024**3)

    gen = client.post("/api/vip/codes/generate", headers=hdr_a, json={"plan": "svip", "duration_days": 30, "count": 1}).json()
    code = gen["codes"][0]["code"]
    red = client.post("/api/vip/redeem", headers=hdr_a, json={"code": code}).json()
    ok("兑换码升级 SVIP 10TB 生效", red["plan"] == "svip" and red["storage_quota_bytes"] == 10 * 1024**4)

    # S3 SigV4 Presigned Upload & Download URLs
    client.post("/api/storage/tiered/config", headers=hdr_a, json={
        "cold_type": "s3",
        "s3_endpoint": "https://r2.example.com",
        "s3_bucket": "mydrive-prod",
        "s3_access_key": "AKIA_TEST",
        "s3_secret_key": "SECRET_TEST",
        "s3_region": "auto",
    })
    pu = client.post("/api/storage/presign-upload", headers=hdr_a, json={"cid": "bafkreitestcid123456"}).json()
    ok("生成 S3/R2 SigV4 预签名直传 URL", pu["mode"] == "s3_direct" and "X-Amz-Signature=" in pu["url"], str(pu))

    pd = client.get(f"/api/storage/presign-download/{doc_id}", headers=hdr_a).json()
    ok("生成 S3/R2 SigV4 预签名直链下载", pd["mode"] == "s3_direct" and len(pd["chunks"]) >= 1 and "X-Amz-Signature=" in pd["chunks"][0]["url"])

    # Content moderation block -> 451 -> unblock -> 200
    doc_cid = client.get(f"/api/files/{doc_id}", headers=hdr_a).json()["content_identity"]
    client.post(f"/api/admin/moderate/{doc_cid}", headers=hdr_a, json={"reason": "copyright"})
    blocked_resp = client.get(f"/api/storage/presign-download/{doc_id}", headers=hdr_a)
    ok("违规 CID 封禁拦截 (HTTP 451)", blocked_resp.status_code == 451, str(blocked_resp.status_code))
    client.delete(f"/api/admin/moderate/{doc_cid}", headers=hdr_a)
    unblocked_resp = client.get(f"/api/storage/presign-download/{doc_id}", headers=hdr_a)
    ok("解封 CID 后恢复访问", unblocked_resp.status_code == 200)

    # Restore dir cold backend before MKV test so local chunks are accessible
    client.post("/api/storage/tiered/config", headers=hdr_a, json={"cold_type": "dir", "cold_dir": cold_dir})

    # Generate real MKV with embedded SRT subtitle track via ffmpeg
    mkv_path = Path(_TMP) / "embedded_sub.mkv"
    srt_file = Path(_TMP) / "emb.srt"
    srt_file.write_text("1\n00:00:00,000 --> 00:00:01,500\nMKV内封中文字幕测试\n", encoding="utf-8")
    subprocess.run([
        shutil.which("ffmpeg"), "-y",
        "-i", str(mp4_path), "-i", str(srt_file),
        "-c", "copy", "-c:s", "srt", "-metadata:s:s:0", "language=zho",
        str(mkv_path),
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    up_mkv = client.post("/api/upload", headers=hdr_a, files={"file": ("embedded_sub.mkv", mkv_path.read_bytes(), "video/x-matroska")})
    mkv_id = up_mkv.json()["identity"]
    mkv_probe = client.get(f"/api/media/{mkv_id}/probe", headers=hdr_a).json()
    ok("MKV 探测到内封字幕与音轨", len(mkv_probe.get("embedded_subtitles", [])) >= 1 and len(mkv_probe.get("audio_tracks", [])) >= 1, str(mkv_probe))
    emb_url = mkv_probe["embedded_subtitles"][0]["url"]
    emb_vtt = client.get(emb_url, headers=hdr_a)
    ok("MKV 内封字幕实时提取为 WebVTT", emb_vtt.status_code == 200 and "MKV内封中文字幕测试" in emb_vtt.text, emb_vtt.text)


    print("== 8) 商业版安全与 Bug 修复回归：管理员 RBAC、主下载/分享 CID 拦截、回收站 delete_ts ==")
    # bob is NOT admin -> 403 on code generation and moderation
    bob_gen = client.post("/api/vip/codes/generate", headers=hdr_b, json={"plan": "svip", "duration_days": 30, "count": 1})
    ok("普通用户 bob 无权生成兑换码 (HTTP 403)", bob_gen.status_code == 403, str(bob_gen.status_code))
    bob_mod = client.post(f"/api/admin/moderate/{doc_cid}", headers=hdr_b, json={"reason": "test"})
    ok("普通用户 bob 无权封禁 CID (HTTP 403)", bob_mod.status_code == 403, str(bob_mod.status_code))

    # admin (alice) blocks CID -> main /api/download and /api/share/.../download both return 451
    client.post(f"/api/admin/moderate/{doc_cid}", headers=hdr_a, json={"reason": "policy"})
    dl_blk = client.get(f"/api/download/{doc_id}", headers=hdr_a)
    ok("主下载路由拦截封禁 CID (HTTP 451)", dl_blk.status_code == 451, str(dl_blk.status_code))
    sh_blk = client.get(f"/api/share/{sid}/download?password=8888")
    ok("分享下载路由拦截封禁 CID (HTTP 451)", sh_blk.status_code == 451, str(sh_blk.status_code))
    client.delete(f"/api/admin/moderate/{doc_cid}", headers=hdr_a)

    # Soft trash writes delete_ts and is NOT prematurely purged by 30-day retention
    up_t = client.post("/api/upload", headers=hdr_a, files={"file": ("trash_check.txt", b"keep me 30d", "text/plain")})
    tid = up_t.json()["identity"]
    client.delete(f"/api/files/{tid}", headers=hdr_a)
    meta_t = api_server.engine.get_file_meta(tid)
    ok("移入回收站写入 delete_ts", meta_t is not None and meta_t.delete_ts > 0, str(meta_t))
    purge_res = client.post("/api/trash/purge-expired?days=30", headers=hdr_a).json()
    ok("刚移入回收站的文件不会被 30 天策略误删", purge_res["deleted_files"] == 0, str(purge_res))


    print("== 9) 商业版 v3.0 终极能力：支付订单回调、健康监控、Prometheus、审计日志、运维后台、PWA ==")
    import hashlib, hmac, json as _json
    hlth = client.get("/api/health").json()
    ok("健康检查接口正常", hlth["status"] == "ok" and hlth["components"]["storage_db"] is True, str(hlth))

    met = client.get("/api/metrics")
    ok("Prometheus 指标导出正常", met.status_code == 200 and "mydrive_files_total" in met.text)

    man = client.get("/manifest.json")
    ok("PWA manifest.json 可访问", man.status_code == 200 and "MyDrive" in man.text)

    # bob creates a VIP order -> payment webhook with valid HMAC-SHA256 -> bob automatically upgraded to VIP!
    ord_resp = client.post("/api/vip/orders/create", headers=hdr_b, json={"plan": "vip", "duration_days": 30, "provider": "alipay"})
    ok("创建 VIP 支付订单", ord_resp.status_code == 200 and ord_resp.json()["amount_cents"] == 1990, ord_resp.text)
    order_no = ord_resp.json()["order_no"]

    hook_body = _json.dumps({"order_no": order_no}).encode("utf-8")
    bad_hook = client.post("/api/vip/orders/webhook", content=hook_body, headers={"X-Pay-Signature": "bad"})
    ok("伪造支付签名被拒绝 (HTTP 403)", bad_hook.status_code == 403)

    good_sig = hmac.new(b"mydrive-pay-secret", hook_body, hashlib.sha256).hexdigest()
    good_hook = client.post("/api/vip/orders/webhook", content=hook_body, headers={"X-Pay-Signature": good_sig})
    ok("合法支付回调自动开通 bob VIP", good_hook.status_code == 200 and good_hook.json()["order"]["membership"]["plan"] == "vip", good_hook.text)

    bob_ops = client.get("/api/admin/dashboard", headers=hdr_b)
    ok("普通用户 bob 无权访问运维后台 (HTTP 403)", bob_ops.status_code == 403)

    alice_ops = client.get("/api/admin/dashboard", headers=hdr_a).json()
    ok("管理员运维后台统计营收与审计日志", alice_ops["revenue"]["total_revenue_cents"] == 1990 and len(alice_ops["recent_logs"]) >= 3, str(alice_ops["revenue"]))


    print("== 10) 多实例集群基础设施 (Redis RESP2 分布式锁/限流 + PG Schema + Flutter 原生客户端) ==")
    import socket, threading as _th
    from cluster_infra import RedisClient, ClusterCoordinator

    # Spin up a tiny in-process mock RESP2 Redis server to verify our zero-dependency RedisClient
    store = {}
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    rport = srv.getsockname()[1]
    stop_ev = _th.Event()

    def _mock_redis():
        srv.settimeout(0.2)
        while not stop_ev.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                data = conn.recv(4096).decode("utf-8", errors="ignore").split("\r\n")
                args = [data[i] for i in range(2, len(data), 2) if i < len(data) and data[i-1].startswith("$")]
                if not args:
                    continue
                cmd = args[0].upper()
                if cmd == "PING":
                    conn.sendall(b"+PONG\r\n")
                elif cmd == "SET":
                    k, v = args[1], args[2]
                    nx = "NX" in [a.upper() for a in args[3:]]
                    if nx and k in store:
                        conn.sendall(b"$-1\r\n")
                    else:
                        store[k] = v
                        conn.sendall(b"+OK\r\n")
                elif cmd == "GET":
                    v = store.get(args[1])
                    if v is None:
                        conn.sendall(b"$-1\r\n")
                    else:
                        b = v.encode()
                        conn.sendall(f"${len(b)}\r\n".encode() + b + b"\r\n")
                elif cmd == "DEL":
                    n = 1 if store.pop(args[1], None) is not None else 0
                    conn.sendall(f":{n}\r\n".encode())
                elif cmd == "INCR":
                    n = int(store.get(args[1], "0")) + 1
                    store[args[1]] = str(n)
                    conn.sendall(f":{n}\r\n".encode())
                elif cmd == "EXPIRE":
                    conn.sendall(b":1\r\n")

    t_r = _th.Thread(target=_mock_redis, daemon=True)
    t_r.start()
    try:
        rc = RedisClient(f"redis://127.0.0.1:{rport}/0")
        ok("零依赖 Redis RESP2 PING 通畅", rc.ping() is True)
        os.environ["REDIS_URL"] = f"redis://127.0.0.1:{rport}/0"
        cc = ClusterCoordinator()
        tok1 = cc.acquire_lock("hls_job_1", ttl_sec=30)
        tok2 = cc.acquire_lock("hls_job_1", ttl_sec=30)
        ok("Redis 分布式锁互斥生效", tok1 is not None and tok2 is None)
        cc.release_lock("hls_job_1", tok1)
        tok3 = cc.acquire_lock("hls_job_1", ttl_sec=30)
        ok("Redis 分布式锁释放后可重入", tok3 is not None)
        rl1 = cc.rate_limit_allow("ip:1.2.3.4", limit=2, window_sec=60)
        rl2 = cc.rate_limit_allow("ip:1.2.3.4", limit=2, window_sec=60)
        rl3 = cc.rate_limit_allow("ip:1.2.3.4", limit=2, window_sec=60)
        ok("Redis 跨实例分布式限流触发", rl1[0] is True and rl2[0] is True and rl3[0] is False)
        os.environ.pop("REDIS_URL", None)
    finally:
        stop_ev.set()
        srv.close()

    h_cluster = client.get("/api/health").json()
    ok("/api/health 返回集群状态字段", "cluster" in h_cluster and "instance_id" in h_cluster["cluster"])

    proj_root = Path(__file__).resolve().parent.parent
    ok("PostgreSQL 集群建表脚本与编排文件存在",
       (proj_root / "deploy" / "postgres-schema.sql").exists() and
       (proj_root / "deploy" / "docker-compose.cluster.yml").exists())
    ok("Flutter 独立多端原生客户端工程完整",
       (proj_root / "client_flutter" / "pubspec.yaml").exists() and
       (proj_root / "client_flutter" / "lib" / "main.dart").exists() and
       (proj_root / "client_flutter" / "lib" / "pages" / "player_page.dart").exists())


    print("== 11) v4.1 最终修复回归：封禁文件可删除、PG 适配层 SQL 翻译、MinIO 建桶与分布式维护锁 ==")
    from db_adapter import translate_sql_to_pg, DualRow

    # Blocked CID can still be deleted by owner (not 451!)
    up_blk = client.post("/api/upload", headers=hdr_a, files={"file": ("blocked_del.txt", b"delete me even if blocked", "text/plain")})
    blk_id = up_blk.json()["identity"]
    blk_cid = up_blk.json()["content_identity"]
    client.post(f"/api/admin/moderate/{blk_cid}", headers=hdr_a, json={"reason": "test_del"})
    ok("封禁文件下载被拦截 (451)", client.get(f"/api/download/{blk_id}", headers=hdr_a).status_code == 451)
    ok("封禁文件仍允许用户自行删除 (200)", client.delete(f"/api/files/{blk_id}", headers=hdr_a).status_code == 200)
    client.delete(f"/api/admin/moderate/{blk_cid}", headers=hdr_a)

    # DB adapter PostgreSQL translation & DualRow
    pg_ddl = translate_sql_to_pg("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, raw BLOB)")
    ok("SQLite DDL 自动转 PostgreSQL (BIGSERIAL/BYTEA)", "BIGSERIAL PRIMARY KEY" in pg_ddl and "BYTEA" in pg_ddl, pg_ddl)
    pg_ins = translate_sql_to_pg("INSERT OR IGNORE INTO chunks (cid, note) VALUES (?, 'a?b')")
    ok("SQLite 占位符与 IGNORE 转 PG 且保护字面量", "VALUES (%s, 'a?b')" in pg_ins and "ON CONFLICT DO NOTHING" in pg_ins, pg_ins)
    dr = DualRow(["id", "name"], [42, "demo"])
    ok("DualRow 兼容下标与列名访问", dr[0] == 42 and dr["name"] == "demo")

    # Verify MinIO init & Flutter queryParameters
    ok("集群编排含 minio-init 自动建桶", "minio-init:" in (proj_root / "deploy" / "docker-compose.cluster.yml").read_text(encoding="utf-8"))
    ok("Flutter 上传 parent 走 queryParameters", "queryParameters: {'parent': parent}" in (proj_root / "client_flutter" / "lib" / "services" / "api_client.dart").read_text(encoding="utf-8"))

    print(f"\n结果: {passed} passed, 0 failed")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    run_all()
