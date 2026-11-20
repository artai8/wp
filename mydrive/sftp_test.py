#!/usr/bin/env python3
"""独立 SFTP 端口验证：真实 SSH 连接 + 全套文件操作 + 与网盘联动。"""
import hashlib
import io
import sys
import time

import paramiko
import requests

from v6_extra_test import API, call, encode_msg, decode_msg

HOST = "127.0.0.1"
PORT = 2022
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


def connect(user, pwd):
    t = paramiko.Transport((HOST, PORT))
    t.connect(username=user, password=pwd)
    return t, paramiko.SFTPClient.from_transport(t)


def main():
    tok = login()
    st, b, msg = call("PubSftpConfig", "Get", encode_msg("SftpConfig", {}), tok)
    cfg = decode_msg("SftpConfig", b)
    user, pwd = cfg.get("username"), cfg.get("password")
    check("SftpConfig 下发凭据", bool(user) and bool(pwd), f"{user}/{pwd}")
    check("enable_flag = 1", (cfg.get("enable_flag") or 0) >= 1, str(cfg.get("enable_flag")))

    print("== 1) 认证 ==")
    try:
        t, sftp = connect(user, pwd)
        check("SFTP 登录成功", True, f"{user}@ {HOST}:{PORT}")
    except Exception as exc:
        check("SFTP 登录成功", False, repr(exc))
        return 1
    try:
        connect(user, "wrong-pass").close()
        check("错误密码被拒", False)
    except Exception:
        check("错误密码被拒", True)

    print("== 2) 目录与上传 ==")
    base = sftp.listdir_attr("/")
    check("根目录可列出", isinstance(base, list), f"{len(base)} 项")

    d = "/sftp-test"
    try:
        for name in sftp.listdir(d):
            try:
                sftp.remove(d + "/" + name)
            except IOError:
                pass
        sftp.rmdir(d)
    except IOError:
        pass
    sftp.mkdir(d)
    check("mkdir", sftp.stat(d).st_mode & 0o040000 != 0)

    payload = b"sftp over mydrive\n" * 5000
    remote = d + "/payload.bin"
    with sftp.open(remote, "wb") as fh:
        fh.write(payload)
    got = sftp.open(remote, "rb").read()
    check("写入后读回一致", got == payload, f"{len(got)} bytes")
    check("sha256 一致", hashlib.sha256(got).hexdigest() == hashlib.sha256(payload).hexdigest())

    print("== 3) 网盘侧可见 ==")
    st2, b2, _ = call("PubUserFile", "List", encode_msg("FileListRequest", {"list_info": {"limit": 100}}), tok)
    files = decode_msg("FileListResponse", b2).get("files") or []
    if isinstance(files, dict):
        files = [files]
    node = next((f for f in files if f.get("name") == "sftp-test"), None)
    check("出现 sftp-test 目录", node is not None, str([f.get("name") for f in files])[:120])
    if node:
        st3, b3, _ = call("PubUserFile", "List",
                          encode_msg("FileListRequest",
                                     {"parent": {"identity": node["identity"]},
                                      "list_info": {"limit": 50}}), tok)
        sub = decode_msg("FileListResponse", b3).get("files") or []
        if isinstance(sub, dict):
            sub = [sub]
        hit = next((f for f in sub if f.get("name") == "payload.bin"), None)
        check("目录内有 payload.bin", hit is not None, str([f.get("name") for f in sub]))
        check("大小一致", bool(hit) and hit.get("size") == len(payload),
              str(hit and hit.get("size")))

    print("== 4) 重命名 / 删除 ==")
    sftp.rename(remote, d + "/renamed.bin")
    check("rename 生效", bool(sftp.stat(d + "/renamed.bin")))
    sftp.remove(d + "/renamed.bin")
    check("remove 生效", len([x for x in sftp.listdir(d)]) == 0)
    sftp.rmdir(d)
    check("rmdir 生效", not any(x.filename == "sftp-test" for x in sftp.listdir_attr("/")))

    sftp.close()
    t.close()

    print(f"\n{OK} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
