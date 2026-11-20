#!/usr/bin/env python3
"""CDN 边缘层验证：缓存语义、响应头、预热 / 失效 / 统计。

对齐线上实测响应头（h.txt）：
  Server: Byte-nginx / Etag: "<cid>" / Age / X-Bdcdn-Cache-Status /
  X-Response-Cache / X-Response-Cinfo / X-Tt-Trace-Tag / via / Vary
"""
import hashlib
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from v6_extra_test import API, call, encode_msg, decode_msg, token as _token  # noqa: E402

SLICES = os.environ.get("SLICES_API", "http://127.0.0.1:8766")
OK = FAIL = 0


def hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


def login():
    return _token()


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  PASS  {name} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def upload(token, name, content):
    r = requests.post(f"{API}/api/upload", files={"file": (name, content)},
                      headers=hdr(token), timeout=30)
    r.raise_for_status()
    return r.json()


def slices_of(token, identity):
    st, body, msg = call("PubUserFile", "GetFileSlicesDownloadInfo",
                         encode_msg("SlicesDownloadInfoRequest",
                                    {"file": {"identity": identity},
                                     "list_info": {"limit": 10}}), token)
    if st != 0:
        raise SystemExit(f"GetFileSlicesDownloadInfo failed: {st} {msg}")
    data = decode_msg("SlicesDownloadInfoResponse", body)
    slices = data.get("slices") or []
    if isinstance(slices, dict):
        slices = [slices]
    return data, slices


def main():
    tok = login()

    print("== 1) 上传并取带 session 的切片地址 ==")
    content = os.urandom(600000)
    meta = upload(tok, "cdn_probe.bin", content)
    data, slices = slices_of(tok, meta["identity"])
    check("返回切片", bool(slices), len(slices))
    s0 = slices[0]
    url = s0.get("download_address", "")
    check("地址是 /ipfs/blocks/2/<x>/<cid>",
          "/ipfs/blocks/2/" in url, url[:90])
    check("地址带 auth_key 与 session", "auth_key=" in url and "session=-" in url, url[-60:])
    parts = url.split("/ipfs/blocks/")[1].split("/")
    check("分片第二级是单个 base32 字符",
          len(parts) == 3 and len(parts[1]) == 1 and parts[1] in "abcdefghijklmnopqrstuvwxyz234567",
          parts[:2])

    print("== 2) 首次回源 -> 二次命中 ==")
    r1 = requests.get(url, timeout=30)
    check("首次 200", r1.status_code == 200, r1.status_code)
    check("首次 cache-status = TCP_MISS,TCP_MISS",
          r1.headers.get("x-bdcdn-cache-status") == "TCP_MISS,TCP_MISS",
          r1.headers.get("x-bdcdn-cache-status"))
    check("首次 edge = edge_miss", r1.headers.get("x-response-cache") == "edge_miss",
          r1.headers.get("x-response-cache"))
    check("Server = Byte-nginx", r1.headers.get("server") == "Byte-nginx",
          r1.headers.get("server"))
    check("Etag 等于 cid", r1.headers.get("etag", "").strip('"') == s0.get("identity"),
          r1.headers.get("etag"))
    check("内容与上传一致", r1.content == content, len(r1.content))

    time.sleep(1.2)
    r2 = requests.get(url, timeout=30)
    check("二次 cache-status = TCP_HIT",
          r2.headers.get("x-bdcdn-cache-status") == "TCP_HIT",
          r2.headers.get("x-bdcdn-cache-status"))
    check("二次 edge = edge_hit", r2.headers.get("x-response-cache") == "edge_hit",
          r2.headers.get("x-response-cache"))
    check("Age 递增", int(r2.headers.get("age", "0")) >= 1, r2.headers.get("age"))
    check("命中后内容仍一致", r2.content == content)

    print("== 3) CDN 响应头齐备 ==")
    for h in ("x-request-id", "x-tt-trace-tag", "via", "vary",
              "x-content-type-options", "x-response-cinfo"):
        check(f"头 {h}", h in {k.lower() for k in r2.headers}, r2.headers.get(h))

    print("== 4) Range / HEAD ==")
    rr = requests.get(url, headers={"Range": "bytes=100-199"}, timeout=20)
    check("Range 206", rr.status_code == 206, rr.status_code)
    check("Range 内容正确", rr.content == content[100:200], len(rr.content))
    check("Content-Range 正确",
          rr.headers.get("content-range") == f"bytes 100-199/{len(content)}",
          rr.headers.get("content-range"))
    rh = requests.head(url, timeout=20)
    check("HEAD 200 且无 body", rh.status_code == 200 and rh.content == b"", rh.status_code)

    print("== 5) 统计 / 预热 / 失效 ==")
    st = requests.get(f"{API}/api/cdn/stats", headers=hdr(tok)).json()
    check("stats 有命中记录", st.get("hits", 0) >= 1, st)
    check("stats 命中率 > 0", st.get("hit_ratio", 0) > 0, st.get("hit_ratio"))

    pw = requests.post(f"{API}/api/cdn/prewarm",
                       json={"identity": meta["identity"]}, headers=hdr(tok)).json()
    check("预热成功", pw.get("prewarmed", 0) >= 1, pw)

    cid = s0.get("identity")
    pg = requests.post(f"{API}/api/cdn/purge", params={"cid": cid},
                       headers=hdr(tok)).json()
    check("按 CID 失效", pg.get("purged") == 1, pg)

    r3 = requests.get(url, timeout=20)
    check("失效后重新回源",
          r3.headers.get("x-bdcdn-cache-status") == "TCP_MISS,TCP_MISS",
          r3.headers.get("x-bdcdn-cache-status"))

    print("== 6) 鉴权仍生效 ==")
    bad = url.split("?")[0] + "?auth_key=1-1-1-1&session=-" + "0" * 32
    check("伪造凭据 403", requests.get(bad, timeout=10).status_code == 403)
    nosess = url.split("&session=")[0]
    check("去掉 session 后验签失败", requests.get(nosess, timeout=10).status_code == 403)

    print("== 7) 离线任务块在出口是密文（与线上 b.bin 一致）==")
    seed_dir = os.environ.get("SEED_DIR",
                              os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "..", "tools", "seed", "data"))
    probe_name = f"codec_probe_{int(time.time())}.bin"
    probe_path = os.path.join(seed_dir, probe_name)
    payload = os.urandom(8192)
    offline_url = f"http://127.0.0.1:8899/data/{probe_name}"
    try:
        os.makedirs(seed_dir, exist_ok=True)
        open(probe_path, "wb").write(payload)
        clear = requests.get(offline_url, timeout=10)
        clear.raise_for_status()
        payload = clear.content
        add = requests.post(f"{API}/api/offline/add",
                            json={"url": offline_url, "save_path": "/"}, headers=hdr(tok))
        if add.status_code != 200:
            check("离线任务登记", False, add.text[:120])
        else:
            task = add.json()
            task_id = task.get("identity")
            stored = None
            for _ in range(40):
                time.sleep(1.5)
                st_l, b_l, _ = call("PubOfflineTask", "List",
                                    encode_msg("OfflineTaskListRequest",
                                               {"list_info": {"limit": 50}}), tok)
                tasks = decode_msg("OfflineTaskListResponse", b_l).get("tasks") or []
                if isinstance(tasks, dict):
                    tasks = [tasks]
                cur = next((t for t in tasks if t.get("identity") == task_id), None)
                if cur and (int(cur.get("status") or 0) >= 1000
                            or int(cur.get("status") or 0) < 0):
                    stored = cur
                    break
            if not stored or int(stored.get("status") or 0) < 1000:
                check("离线任务完成", False, str(stored)[:120])
            else:
                files = requests.get(f"{API}/api/files", params={"parent": "root"},
                                     headers=hdr(tok)).json()
                hit = next((f for f in files if f["name"] == probe_name
                            and not f.get("dir")), None)
                if not hit:
                    check("离线文件入库", False, [f["name"] for f in files][:8])
                else:
                    _, sl = slices_of(tok, hit["identity"])
                    u = sl[0]["download_address"]
                    raw = requests.get(u, timeout=20).content
                    check("出口字节 != 明文（XOR 混淆）", raw != payload, f"{len(raw)}B")
                    check("密文 XOR 0x75 还原明文",
                          bytes(b ^ 0x75 for b in raw) == payload, len(raw))
                    dec = requests.get(u + "&decrypt=1", timeout=20).content
                    check("decrypt=1 直接给明文", dec == payload, len(dec))
                    cdn_off = requests.get(f"{API}/api/download/{hit['identity']}",
                                           headers=hdr(tok)).content
                    check("内部下载链路自动解密", cdn_off == payload, len(cdn_off))
                    call("PubOfflineTask", "Delete",
                         encode_msg("OfflineTaskDeleteRequest",
                                    {"identity": [task_id], "delete_files": True}), tok)

    except Exception as exc:  # noqa: BLE001
        check("离线块密文验证", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            os.remove(probe_path)
        except OSError:
            pass

    print(f"\n{OK} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
