#!/usr/bin/env python3
"""离线下载（BT/HTTP）真实链路验证：Add -> 下载 -> 入库 -> List -> Delete。"""
import json
import sys
import time

import requests

from v6_extra_test import API, call, encode_msg, decode_msg

USER = "extra_user"
PASS = "extra_pass_123"
OK = 0
FAIL = 0


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
    print("== 1) Add HTTP 直链 ==")
    url = "https://speed.cloudflare.com/__down?bytes=524288"
    st, body, msg = call("PubOfflineTask", "Add",
                         encode_msg("UserTask", {"url": url, "name": "offline-http-test.bin",
                                                 "save_path": "/"}), tok)
    task = decode_msg("UserTask", body)
    check("Add 返回 0", st == 0, f"status={st} {msg[:60]}")
    check("有 identity", bool(task.get("identity")), task.get("identity", ""))
    check("task_identity 有值", bool(task.get("task_identity")), str(task.get("task_identity"))[:30])
    print("     task:", {k: task.get(k) for k in ("identity", "type", "status", "name", "size", "save_path")})
    ident = task.get("identity")

    print("== 2) 轮询到完成 ==")
    final = None
    for i in range(40):
        time.sleep(1.5)
        st2, b2, _ = call("PubOfflineTask", "List",
                          encode_msg("OfflineTaskListRequest", {"list_info": {"limit": 50}}), tok)
        tasks = decode_msg("OfflineTaskListResponse", b2).get("tasks") or []
        if isinstance(tasks, dict):
            tasks = [tasks]
        cur = next((t for t in tasks if t.get("identity") == ident), None)
        if cur:
            print(f"     [{i}] status={cur.get('status')} progress={cur.get('progress')} "
                  f"bytes={cur.get('bytes_processed')}/{cur.get('bytes_total')}")
            if (cur.get("status") or 0) >= 1000 or (cur.get("status") or 0) < 0:
                final = cur
                break
    check("任务到达终态", final is not None, f"status={final and final.get('status')}")
    check("完成状态 = 1000", bool(final) and final.get("status") == 1000,
          f"status={final and final.get('status')} msg={final and final.get('message')}")
    check("progress = 100", bool(final) and final.get("progress") == 100)
    check("bytes_total > 0", bool(final) and (final.get("bytes_total") or 0) > 0,
          str(final and final.get("bytes_total")))

    print("== 3) 文件已入库 ==")
    st3, b3, _ = call("PubUserFile", "List", encode_msg("FileListRequest", {"list_info": {"limit": 100}}), tok)
    files = decode_msg("FileListResponse", b3).get("files") or []
    if isinstance(files, dict):
        files = [files]
    names = [f.get("name") for f in files]
    hit = [f for f in files if f.get("name") == "offline-http-test.bin"]
    check("出现 offline-http-test.bin", bool(hit), f"共 {len(names)} 项")
    if hit:
        f = hit[0]
        check("大小 524288", f.get("size") == 524288, str(f.get("size")))
        check("有 content_identity", str(f.get("content_identity") or "").startswith("b"),
              str(f.get("content_identity"))[:24])
        print("     file:", {k: f.get(k) for k in ("identity", "name", "size", "type", "path")})

    print("== 4) Delete（含文件） ==")
    st4, b4, _ = call("PubOfflineTask", "Delete",
                      encode_msg("OfflineTaskDeleteRequest",
                                 {"identity": [final.get("task_identity") if final else ident],
                                  "delete_files": True}), tok)
    d4 = decode_msg("OfflineTaskDeleteResponse", b4)
    check("Delete 返回 0", st4 == 0, f"{st4}")
    check("count >= 1", (d4.get("count") or 0) >= 1, str(d4.get("count")))

    print("== 5) Parse magnet ==")
    mag = "magnet:?xt=urn:btih:6aa271c5bcdf8815baff8b4bcaf7b88c5bb8ed5d"
    st5, b5, msg5 = call("PubOfflineTask", "Parse",
                         encode_msg("TaskParseRequest", {"url": mag}), tok)
    r5 = decode_msg("TaskParseResponse", b5)
    meta = r5.get("meta") or {}
    files5 = r5.get("task_files") or []
    if isinstance(files5, dict):
        files5 = [files5]
    check("Parse 返回 0", st5 == 0, f"{st5} {msg5[:60]}")
    check("identity = <infohash>_otp_btv1",
          str(meta.get("identity") or "").endswith("_otp_btv1"),
          str(meta.get("identity")))
    print(f"     meta: name={meta.get('name')} size={meta.get('size')} files={len(files5)}")
    for f in files5[:3]:
        print("       -", f.get("path"), f.get("size"))

    print(f"\n{OK} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
