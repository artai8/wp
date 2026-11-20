#!/usr/bin/env python3
"""生成一个带 webseed 的真实 .torrent，用于本地验证 BT 链路。

产出：
  tools/seed/data/<name>/...        被分享的内容
  tools/seed/<name>.torrent         种子文件（含 url-list webseed）
"""
import hashlib
import os
import sys
from pathlib import Path

NAME = "mydrive-bt-demo"
PIECE = 262144  # 256 KiB


def bencode(obj) -> bytes:
    if isinstance(obj, int):
        return b"i%de" % obj
    if isinstance(obj, bytes):
        return b"%d:%s" % (len(obj), obj)
    if isinstance(obj, str):
        return bencode(obj.encode())
    if isinstance(obj, list):
        return b"l" + b"".join(bencode(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, v) for k, v in obj.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(type(obj))


def build(root: Path, seed_url: str) -> Path:
    files = []
    blob = bytearray()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        data = path.read_bytes()
        rel = path.relative_to(root).parts
        files.append({"length": len(data), "path": list(rel)})
        blob += data

    pieces = b"".join(hashlib.sha1(blob[i:i + PIECE]).digest()
                      for i in range(0, len(blob), PIECE))
    info = {
        "name": NAME,
        "piece length": PIECE,
        "pieces": pieces,
        "files": files,
    }
    torrent = {
        "announce": "udp://tracker.opentrackr.org:1337/announce",
        "url-list": seed_url,
        "info": info,
    }
    out = root.parent / f"{NAME}.torrent"
    out.write_bytes(bencode(torrent))
    return out


def main():
    base = Path(__file__).resolve().parent / "seed"
    data = base / "data" / NAME
    data.mkdir(parents=True, exist_ok=True)
    # 三个文件，跨多个 piece
    (data / "hello.txt").write_bytes(b"mydrive offline bt demo\n" * 400)
    (data / "sub").mkdir(exist_ok=True)
    (data / "sub" / "clip.bin").write_bytes(bytes((i * 37 + 11) & 0xFF for i in range(900_000)))
    (data / "sub" / "notes.md").write_text("# offline task\n" + "piece data\n" * 2000, encoding="utf-8")

    seed_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8899/data/"
    t = build(data, seed_url)
    print("torrent:", t)
    print("size:", t.stat().st_size)


if __name__ == "__main__":
    main()
