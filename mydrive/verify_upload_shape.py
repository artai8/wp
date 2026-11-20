#!/usr/bin/env python3
"""验证 /int/blocks 三段式上传与真实服务同形，且根 CID = 标准 UnixFS。"""
import hashlib
import os
import sys

import requests

from cid_engine import compute_cid, compute_file_cid

API = "http://127.0.0.1:8765"


def login():
    creds = {"username": "shape_user", "password": "shape_pass_123"}
    r = requests.post(f"{API}/api/auth/register", json=creds)
    if r.status_code != 200:
        r = requests.post(f"{API}/api/auth/login", json=creds)
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def upload(h, name, data, block=4194304):
    init = requests.post(f"{API}/api/upload/init",
                         json={"name": name, "parent": "root", "file_size": len(data)}, headers=h)
    init.raise_for_status()
    job = init.json()
    assert job["upload_address"] == "/int/blocks", job
    task = job["task"]

    cids = []
    for off in range(0, len(data), block):
        chunk = data[off:off + block]
        cid = compute_cid(chunk)
        cids.append(cid)
        probe = requests.get(f"{API}/int/blocks/{task}/{cid}", headers=h)
        assert probe.status_code in (200, 404), probe.status_code
        if probe.status_code == 404:          # 未命中则上传；200 = 块已存在（秒传）
            r = requests.post(f"{API}/int/blocks/{task}/{cid}", data=chunk,
                              headers={**h, "Content-Type": "application/octet-stream"})
            assert r.status_code == 201, r.text
        assert requests.get(f"{API}/int/blocks/{task}/{cid}", headers=h).status_code == 200

    commit = requests.post(f"{API}/int/blocks/{task}", json=cids, headers=h)
    commit.raise_for_status()
    return commit.json(), cids


def main():
    h = login()
    small = os.urandom(1024)
    meta, cids = upload(h, "shape_small.bin", small)
    expect = compute_file_cid([(cids[0], len(small))])
    assert meta["content_identity"] == expect, (meta["content_identity"], expect)
    assert expect.startswith("bafkrei"), "single slice collapses to raw CID"
    print("✅ 1 KiB  :", meta["content_identity"])

    big = open("../downloads/nodeipfs/data2.bin", "rb").read()
    meta, cids = upload(h, "shape_big.bin", big)
    expect = "bafybeibkkab4ej42sijn6badh4s5tqozzjfqt547s3utp5xdxolqmotzx4"
    assert meta["content_identity"] == expect, (meta["content_identity"], expect)
    print("✅ 8 MiB  :", meta["content_identity"], "(与线上/js-ipfs 一致)")

    # 坏切片 id
    init = requests.post(f"{API}/api/upload/init",
                         json={"name": "bad.bin", "parent": "root", "file_size": 4}, headers=h)
    task = init.json()["task"]
    bogus = compute_cid(b"aaaa")
    r = requests.post(f"{API}/int/blocks/{task}/{bogus}", data=b"bbbb", headers=h)
    assert r.status_code == 400, r.text
    print("✅ 非法 slice id 拒绝:", r.json().get("detail"))
    print("\nALL SHAPE CHECKS PASSED")


if __name__ == "__main__":
    main()
