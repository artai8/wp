#!/usr/bin/env python3
"""块编码层（XOR 0x75）与全局秒传的单元验证。

线上真实证据：下载 ``/ipfs/blocks/2/75/bafkreick4...`` 得到的响应体，
逐字节 XOR 0x75 之后 sha256 恰好等于该 CID（即 HTTP Etag）。
本测试固化这一行为，并验证 CID 语义不因编码而改变。
"""
import base64
import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cid_engine
import storage_engine as se

OK = FAIL = 0
SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                       "downloads", "pcap")


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def cid_of(data: bytes) -> str:
    mh = bytes([1, 0x55, 0x12, 0x20]) + hashlib.sha256(data).digest()
    return "b" + base64.b32encode(mh).decode().lower().rstrip("=")


def main():
    root = tempfile.mkdtemp(prefix="mydrive-codec-")
    try:
        engine = se.StorageEngine(root, chunk_size=4 * 1024 * 1024)

        print("== 1) 编码/解码自反 ==")
        payload = b"ftyp" + b"\x00" * 60
        enc = se.encode_chunk(payload, se.CHUNK_FMT_XOR75)
        check("XOR 与原文不同", enc != payload)
        check("XOR 自反", se.decode_chunk(enc, se.CHUNK_FMT_XOR75) == payload)
        check("明文格式不变", se.encode_chunk(payload, se.CHUNK_FMT_PLAIN) == payload)

        print("== 2) 线上真实块复现（b.bin / Etag）==")
        sample = os.path.join(SAMPLES, "b.bin")
        if os.path.exists(sample):
            raw = open(sample, "rb").read()
            plain = se.decode_chunk(raw, se.CHUNK_FMT_XOR75)
            check("4MiB 长度不变", len(plain) == len(raw), f"{len(plain)} vs {len(raw)}")
            check("解出 MP4 ftyp 头", plain[:8] == b"\x00\x00\x00\x18ftyp", plain[:8])
            check("CID == 线上 Etag",
                  cid_of(plain) == "bafkreick4nyxtwxd2jyqtpkhcuj347w6wa6mbu5mqfiuq3h7eznumbgu2a",
                  cid_of(plain))
        else:
            print("  skip 缺少样本 b.bin")

        print("== 3) 落盘编码：离线块 XOR，上传块明文 ==")
        src = os.path.join(root, "offline.bin")
        body = bytes((i * 37 + 11) & 0xFF for i in range(9000))
        open(src, "wb").write(body)

        meta = engine.chunk_file(src, parent="root", name="offline.bin",
                                 user_id="u1", chunk_format=se.CHUNK_FMT_XOR75)
        cid = meta.chunks[0].cid
        check("CID 覆盖明文", cid == cid_engine.compute_cid(body), cid)
        raw = engine.get_chunk_raw(cid)
        check("磁盘上是密文", raw == se.encode_chunk(body, 1))
        check("读取自动解回明文", engine.get_chunk(cid) == body)
        check("format 记录为 1", engine.chunk_format(cid) == 1)

        src2 = os.path.join(root, "upload.bin")
        open(src2, "wb").write(body)   # 同内容 -> 已存在，不会重复落盘
        meta2 = engine.chunk_file(src2, parent="root", name="upload.bin",
                                  user_id="u2", chunk_format=se.CHUNK_FMT_PLAIN)
        check("同内容复用同一 CID", meta2.chunks[0].cid == cid)
        check("已存在块不会被重写", engine.chunk_format(cid) == 1)

        print("== 4) 格式转换（plain <-> xor75）==")
        other = os.path.join(root, "other.bin")
        body2 = b"plain-chunk-" * 300
        open(other, "wb").write(body2)
        m3 = engine.chunk_file(other, parent="root", name="other.bin",
                               user_id="u1", chunk_format=se.CHUNK_FMT_PLAIN)
        cid3 = m3.chunks[0].cid
        check("初始为明文", engine.chunk_format(cid3) == 0)
        check("转 XOR 成功", engine.set_chunk_format(cid3, se.CHUNK_FMT_XOR75))
        check("转换后仍是密文", engine.get_chunk_raw(cid3) == se.encode_chunk(body2, 1))
        check("转换后读回明文", engine.get_chunk(cid3) == body2)
        check("CID 未变", cid3 == cid_engine.compute_cid(body2))

        print("== 5) 全局秒传（跨用户）==")
        big = os.path.join(root, "shared.bin")
        shared_body = b"cross-user-dedup-" * 5000
        open(big, "wb").write(shared_body)
        m4 = engine.chunk_file(big, parent="root", name="shared.bin", user_id="alice")
        sha1 = hashlib.sha1(shared_body).hexdigest()
        head = hashlib.sha1(shared_body[:262144]).hexdigest()

        hit_alice = engine.check_instant_transfer(sha1, head, len(shared_body), user_id="alice")
        hit_bob = engine.check_instant_transfer(sha1, head, len(shared_body), user_id="bob")
        check("本人秒传命中", hit_alice == m4.content_identity, hit_alice)
        check("他人也能秒传（全局去重）", hit_bob == m4.content_identity, hit_bob)

        miss = engine.check_instant_transfer("0" * 40, "0" * 40, len(shared_body), user_id="bob")
        check("错误哈希不命中", miss is None, miss)

        # 落库幂等：同一内容只保留一条索引
        engine._add_fast_lookup(sha1, head, len(shared_body), m4.content_identity,
                                "again.bin", user_id="bob")
        import sqlite3
        conn = sqlite3.connect(str(engine.db_path))
        n = conn.execute("SELECT COUNT(*) FROM fast_lookup WHERE sha1=?", (sha1,)).fetchone()[0]
        conn.close()
        check("全局索引唯一", n == 1, f"rows={n}")

        print(f"\n结果: {OK} passed, {FAIL} failed")
        return 0 if FAIL == 0 else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
