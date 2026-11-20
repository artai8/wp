#!/usr/bin/env python3
"""切片出口验证：独立端口 + 线上同形路径 + Range/HEAD/403 + CDN 头。"""
import hashlib
import sys
import time

import requests

from v6_extra_test import API, call, encode_msg, decode_msg

SLICES = "http://127.0.0.1:8766"
USER = "extra_user"
PASS = "extra_pass_123"
OK = FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  PASS  {name} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def login():
    r = requests.post(f"{API}/api/auth/login", json={"username": USER, "password": PASS})
    if r.status_code == 200:
        return r.json()["access_token"]
    requests.post(f"{API}/api/auth/register", json={"username": USER, "password": PASS})
    return requests.post(f"{API}/api/auth/login",
                         json={"username": USER, "password": PASS}).json()["access_token"]


def main():
    tok = login()
    h = {"Authorization": f"Bearer {tok}"}
    payload = bytes((i * 7 + 3) & 0xFF for i in range(600_000))
    r = requests.post(f"{API}/api/upload", headers=h,
                      files={"file": ("slices-demo.bin", payload, "application/octet-stream")},
                      data={"parent": "root"})
    check("上传成功", r.status_code == 200, str(r.status_code))
    info = r.json()
    fid = info.get("identity") or (info.get("file") or {}).get("identity")
    check("拿到 file identity", bool(fid), str(fid))

    print("== 1) 切片地址形状 ==")
    r2 = requests.get(f"{API}/api/files/{fid}/download-info", headers=h,
                      params={"limit": 10})
    check("download-info 200", r2.status_code == 200, str(r2.status_code))
    page = r2.json()
    slices = page.get("slices") or []
    check("有切片", len(slices) >= 1, f"{len(slices)} 片")
    addr = slices[0]["download_address"] if slices else ""
    cid = slices[0]["identity"] if slices else ""
    check("路径为 /ipfs/blocks/<a>/<b>/<cid>", "/ipfs/blocks/" in addr, addr[:90])
    check("首段恒为 2", addr.split("/ipfs/blocks/")[-1].split("/")[0] == "2", addr[:80])
    check("带 auth_key", "auth_key=" in addr, "")

    print("== 2) 独立端口下载 ==")
    path = addr.split("://", 1)[-1]
    path = "/" + path.split("/", 1)[1] if "/" in path else path
    url = SLICES + path
    r3 = requests.get(url, timeout=20)
    check("GET 200", r3.status_code == 200, str(r3.status_code))
    check("Server: Byte-nginx", r3.headers.get("server") == "Byte-nginx", str(r3.headers.get("server")))
    check("Etag = cid", r3.headers.get("etag", "").strip('"') == cid, str(r3.headers.get("etag")))
    check("X-Bdcdn-Cache-Status", bool(r3.headers.get("x-bdcdn-cache-status")),
          str(r3.headers.get("x-bdcdn-cache-status")))
    check("Content-Type octet-stream",
          r3.headers.get("content-type", "").startswith("application/octet-stream"),
          str(r3.headers.get("content-type")))
    body = r3.content
    recomputed = hashlib.sha256(body).digest()
    import base64
    alpha = "abcdefghijklmnopqrstuvwxyz234567"
    bits = "".join(f"{b:08b}" for b in bytes([1, 0x55, 0x12, 0x20]) + recomputed)
    bits += "0" * (-len(bits) % 5)
    got_cid = "b" + "".join(alpha[int(bits[i:i + 5], 2)] for i in range(0, len(bits), 5))
    check("内容按 CID 校验通过", got_cid == cid, f"{got_cid[:20]} vs {cid[:20]}")

    print("== 3) Range / HEAD ==")
    r4 = requests.get(url, headers={"Range": "bytes=0-1023"}, timeout=20)
    check("Range 206", r4.status_code == 206, str(r4.status_code))
    check("Content-Range 正确", r4.headers.get("content-range", "").startswith("bytes 0-1023/"),
          str(r4.headers.get("content-range")))
    check("Range 内容一致", r4.content == body[:1024], f"{len(r4.content)}")
    r5 = requests.head(url, timeout=20)
    check("HEAD 200", r5.status_code == 200, str(r5.status_code))
    check("HEAD Content-Length", r5.headers.get("content-length") == str(len(body)),
          str(r5.headers.get("content-length")))

    print("== 4) 鉴权 ==")
    r6 = requests.get(url.split("?")[0], timeout=15)
    check("无 auth_key -> 403", r6.status_code == 403, str(r6.status_code))
    bad = url.replace("auth_key=", "auth_key=deadbeef").split("&")[0]
    r7 = requests.get(bad, timeout=15)
    check("伪造 auth_key -> 403", r7.status_code == 403, str(r7.status_code))
    r8 = requests.get(url.replace("auth_key=", "auth_key=1-1-"), timeout=15)
    check("残缺 auth_key -> 403", r8.status_code == 403, str(r8.status_code))

    print("== 5) 主端口同路径（域名反代等价） ==")
    r9 = requests.get(API + path, timeout=20)
    check("主端口 GET 200", r9.status_code == 200, str(r9.status_code))
    check("主端口内容一致", r9.content == body, f"{len(r9.content)}")

    print("== 6) gRPC GetFileSlicesDownloadInfo ==")
    st, b, msg = call("PubUserFile", "GetFileSlicesDownloadInfo",
                      encode_msg("SlicesDownloadInfoRequest",
                                 {"file": {"identity": fid},
                                  "list_info": {"limit": 10}}), tok)
    resp = decode_msg("SlicesDownloadInfoResponse", b)
    sl = resp.get("slices") or []
    if isinstance(sl, dict):
        sl = [sl]
    check("gRPC 返回切片", st == 0 and len(sl) >= 1, f"{st} n={len(sl)}")
    if sl:
        a2 = sl[0].get("download_address") or ""
        check("gRPC 地址也是 /ipfs/blocks", "/ipfs/blocks/" in a2, a2[:80])

    print(f"\n{OK} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
