#!/usr/bin/env python3
"""BT 全链路：.torrent -> aria2 下载（webseed）-> 目录入库 -> 校验结构。"""
import json
import sys
import time

import requests

from v6_extra_test import API, call, encode_msg, decode_msg

USER = "extra_user"
PASS = "extra_pass_123"
TORRENT = "http://127.0.0.1:8899/data/mydrive-bt-demo.torrent"
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


def list_files(tok):
    st, b, _ = call("PubUserFile", "List",
                    encode_msg("FileListRequest", {"list_info": {"limit": 200}}), tok)
    files = decode_msg("FileListResponse", b).get("files") or []
    if isinstance(files, dict):
        files = [files]
    return files


def main():
    tok = login()

    print("== 1) Parse .torrent ==")
    st, b, msg = call("PubOfflineTask", "Parse",
                      encode_msg("TaskParseRequest", {"url": TORRENT}), tok)
    r = decode_msg("TaskParseResponse", b)
    meta, tfs = r.get("meta") or {}, r.get("task_files") or []
    if isinstance(tfs, dict):
        tfs = [tfs]
    check("Parse 成功", st == 0 and bool(meta), f"{st} {msg[:60]}")
    check("名字 = mydrive-bt-demo", meta.get("name") == "mydrive-bt-demo", str(meta.get("name")))
    check("任务文件数 = 3", len(tfs) == 3, f"{len(tfs)}: {[t.get('path') for t in tfs]}")

    print("== 2) Add（BT） ==")
    st2, b2, _ = call("PubOfflineTask", "Add",
                      encode_msg("UserTask", {"url": TORRENT, "save_path": "/"}), tok)
    task = decode_msg("UserTask", b2)
    check("Add 成功", st2 == 0 and bool(task.get("identity")), f"{st2}")
    check("type = 5050", task.get("type") == 5050, str(task.get("type")))
    ident = task.get("identity")

    print("== 3) 等待完成 ==")
    final = None
    for i in range(50):
        time.sleep(1.5)
        st3, b3, _ = call("PubOfflineTask", "List",
                          encode_msg("OfflineTaskListRequest", {"list_info": {"limit": 50}}), tok)
        tasks = decode_msg("OfflineTaskListResponse", b3).get("tasks") or []
        if isinstance(tasks, dict):
            tasks = [tasks]
        cur = next((t for t in tasks if t.get("identity") == ident), None)
        if cur:
            print(f"     [{i}] status={cur.get('status')} progress={cur.get('progress')} "
                  f"{cur.get('bytes_processed')}/{cur.get('bytes_total')}")
            if (cur.get("status") or 0) >= 1000 or (cur.get("status") or 0) < 0:
                final = cur
                break
    check("完成 status=1000", bool(final) and final.get("status") == 1000,
          f"status={final and final.get('status')} msg={final and final.get('message')}")

    print("== 4) 目录结构入库 ==")
    files = list_files(tok)
    names = [f.get("name") for f in files]
    check("出现 mydrive-bt-demo 目录", "mydrive-bt-demo" in names, str(names))
    root = next((f for f in files if f.get("name") == "mydrive-bt-demo"), None)
    if root and root.get("identity"):
        st5, b5, _ = call("PubUserFile", "List",
                          encode_msg("FileListRequest",
                                     {"parent": {"identity": root["identity"]},
                                      "list_info": {"limit": 50}}), tok)
        sub = decode_msg("FileListResponse", b5).get("files") or []
        if isinstance(sub, dict):
            sub = [sub]
        sub_names = [f.get("name") for f in sub]
        check("目录内有 hello.txt / sub", "hello.txt" in sub_names and "sub" in sub_names, str(sub_names))
        sizes = {f.get("name"): f.get("size") for f in sub}
        check("hello.txt 大小 9600", sizes.get("hello.txt") == 9600, str(sizes.get("hello.txt")))
        subdir = next((f for f in sub if f.get("name") == "sub"), None)
        if subdir:
            st6, b6, _ = call("PubUserFile", "List",
                              encode_msg("FileListRequest",
                                         {"parent": {"identity": subdir["identity"]},
                                          "list_info": {"limit": 50}}), tok)
            deep = decode_msg("FileListResponse", b6).get("files") or []
            if isinstance(deep, dict):
                deep = [deep]
            deep_names = [f.get("name") for f in deep]
            check("子目录有 clip.bin / notes.md",
                  "clip.bin" in deep_names and "notes.md" in deep_names, str(deep_names))
            sizes2 = {f.get("name"): f.get("size") for f in deep}
            check("clip.bin 大小 900000", sizes2.get("clip.bin") == 900000, str(sizes2.get("clip.bin")))

    print("== 5) 清理 ==")
    st7, b7, _ = call("PubOfflineTask", "Delete",
                      encode_msg("OfflineTaskDeleteRequest",
                                 {"identity": [final.get("task_identity") or ident],
                                  "delete_files": True}), tok)
    check("Delete 成功", st7 == 0, str(st7))
    files2 = list_files(tok)
    check("目录已随任务删除", "mydrive-bt-demo" not in [f.get("name") for f in files2],
          str([f.get("name") for f in files2]))

    print(f"\n{OK} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
