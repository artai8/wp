"""切片下载专用服务：独立域名 / 独立端口的对象块出口。

对齐线上 ``slices.qzy.xyz``（白山云 Byte-nginx 边缘）：

  * 路径 ``/ipfs/blocks/<a>/<b>/<cid>``，``a`` 恒为 ``2``，``b`` 是 base32 字符（32 桶）
  * 查询串 ``auth_key=<exp>-<kid>-<user>-<sig>&session=-<32hex>``
  * 响应头：``Server: Byte-nginx`` / ``Etag: "<cid>"`` / ``Age`` /
    ``X-Bdcdn-Cache-Status`` / ``X-Response-Cache`` / ``via`` / ``X-Tt-Trace-Tag``
  * 支持 Range（206）、HEAD，缺签名或签名失效一律 403

边缘缓存语义（与实测一致）：
  * 首次请求回源 → ``TCP_MISS,TCP_MISS`` + ``edge_miss``
  * 命中 → ``TCP_HIT`` + ``edge_hit``，``Age`` 为缓存存续秒数
  * 出口字节 = 该块的**存储形态**：离线任务入库的块是 XOR 0x75 后的密文，
    普通上传的块是明文（与线上实测样本的响应体完全一致）。
    ``?decrypt=1`` 可额外拿到明文，供自带播放器/校验使用。
"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from typing import Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import Response

import protocol as proto
import storage_engine as se

CDN_SERVER = os.environ.get("SLICES_SERVER_HEADER", "Byte-nginx")
EDGE_NODE = os.environ.get("SLICES_EDGE_NODE", "myedge-01")
EDGE_CACHE_MAX = int(os.environ.get("SLICES_CACHE_MAX", "512"))     # 块数
EDGE_CACHE_TTL = int(os.environ.get("SLICES_CACHE_TTL", "3600"))    # 秒


# ==================== EDGE CACHE ====================

class EdgeCache:
    """In-memory edge node cache — LRU with TTL, keyed by CID.

    Stores the *stored* bytes (XOR form for offline-ingested blocks) so both the
    obfuscated and the plaintext representation can be served without re-reading
    the source. Mirrors the live CDN: byte-for-byte passthrough of whatever the
    origin object store holds.
    """

    def __init__(self, capacity: int = EDGE_CACHE_MAX, ttl: int = EDGE_CACHE_TTL):
        self.capacity = max(8, capacity)
        self.ttl = max(1, ttl)
        self._data: "OrderedDict[str, Tuple[bytes, int, float]]" = OrderedDict()
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0
        self.purges = 0
        self.prewarms = 0

    def get(self, cid: str) -> Optional[Tuple[bytes, int, float]]:
        with self._lock:
            item = self._data.get(cid)
            if not item:
                self.misses += 1
                return None
            raw, fmt, stored_at = item
            if time.time() - stored_at > self.ttl:
                self._data.pop(cid, None)
                self.misses += 1
                return None
            self._data.move_to_end(cid)
            self.hits += 1
            return raw, fmt, stored_at

    def put(self, cid: str, raw: bytes, fmt: int, stored_at: Optional[float] = None):
        with self._lock:
            self._data[cid] = (raw, fmt, stored_at or time.time())
            self._data.move_to_end(cid)
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)

    def purge(self, cid: str = "") -> int:
        with self._lock:
            if cid:
                removed = 1 if self._data.pop(cid, None) is not None else 0
            else:
                removed = len(self._data)
                self._data.clear()
            self.purges += removed
            return removed

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "node": EDGE_NODE,
                "entries": len(self._data),
                "capacity": self.capacity,
                "ttl": self.ttl,
                "hits": self.hits,
                "misses": self.misses,
                "purges": self.purges,
                "prewarms": self.prewarms,
                "hit_ratio": round(self.hits / total, 4) if total else 0.0,
                "bytes_cached": sum(len(v[0]) for v in self._data.values()),
            }


def _origin_fetch(engine, cid: str) -> Optional[Tuple[bytes, int]]:
    """Read the object exactly as stored (source-of-truth passthrough)."""
    raw = engine.get_chunk_raw(cid)
    if raw is None:
        return None
    return raw, engine.chunk_format(cid)


def fetch_cached(cache: EdgeCache, engine, cid: str) -> Optional[dict]:
    """Return ``{raw, fmt, age, cache_status}`` for one CID, filling the cache."""
    hit = cache.get(cid)
    if hit:
        raw, fmt, stored_at = hit
        return {"raw": raw, "fmt": fmt, "age": int(time.time() - stored_at),
                "cache_status": "TCP_HIT", "edge": "edge_hit"}
    origin = _origin_fetch(engine, cid)
    if origin is None:
        return None
    raw, fmt = origin
    cache.put(cid, raw, fmt)
    return {"raw": raw, "fmt": fmt, "age": 0,
            "cache_status": "TCP_MISS,TCP_MISS", "edge": "edge_miss"}


def prewarm(cache: EdgeCache, engine, cids) -> dict:
    """Push blocks into the edge cache ahead of playback (``/api/cdn/prewarm``)."""
    filled, missing = 0, []
    for cid in cids or []:
        origin = _origin_fetch(engine, cid)
        if origin is None:
            missing.append(cid)
            continue
        raw, fmt = origin
        cache.put(cid, raw, fmt)
        filled += 1
    cache.prewarms += filled
    return {"prewarmed": filled, "missing": missing, "node": EDGE_NODE}


# ==================== HTTP PLUMBING ====================

def _parse_range(header: str, total: int):
    """-> (start, end)；不合法返回 None，越界返回 (-1, -1)。"""
    m = re.match(r"bytes=(\d*)-(\d*)$", (header or "").strip())
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    if a == "" and b == "":
        return None
    if a == "":
        length = int(b)
        if length <= 0:
            return None
        return max(0, total - length), total - 1
    start = int(a)
    if start >= total:
        return (-1, -1)
    end = int(b) if b else total - 1
    return start, min(end, total - 1)


def _base_headers(cid: str, hit: Optional[dict] = None, client_ip: str = "") -> dict:
    hit = hit or {}
    return {
        "server": CDN_SERVER,
        "etag": f'"{cid}"',
        "content-type": "application/octet-stream",
        "age": str(int(hit.get("age") or 0)),
        "x-bdcdn-cache-status": hit.get("cache_status") or "TCP_MISS,TCP_MISS",
        "x-response-cache": hit.get("edge") or "edge_miss",
        "x-response-cinfo": client_ip or EDGE_NODE,
        "x-tt-trace-tag": "id=5",
        "via": f"{EDGE_NODE}.edge",
        "x-content-type-options": "nosniff",
        "vary": "Origin",
        "accept-ranges": "bytes",
        "x-request-id": uuid.uuid4().hex,
    }


def _forbidden() -> Response:
    return Response(status_code=403, headers={"server": CDN_SERVER})


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


async def serve_block(engine, secret: str, cid: str, request: Request,
                      cache: Optional[EdgeCache] = None) -> Response:
    """核心出口逻辑：独立端口与主服务共用。"""
    auth_key = request.query_params.get("auth_key", "")
    if not auth_key:
        return _forbidden()
    session = proto.parse_session(request.query_params.get("session", ""))
    ok, _reason = proto.verify_auth_key(cid, auth_key, secret, session)
    if not ok:
        return _forbidden()

    cache = cache or _DEFAULT_CACHE
    hit = fetch_cached(cache, engine, cid)
    if hit is None:
        return Response(status_code=404, headers=_base_headers(cid, None, _client_ip(request)))

    body = hit["raw"]
    if request.query_params.get("decrypt") in ("1", "true", "yes"):
        body = se.decode_chunk(body, hit["fmt"])

    total = len(body)
    headers = _base_headers(cid, hit, _client_ip(request))
    rng = _parse_range(request.headers.get("range", ""), total)
    if rng and rng[0] >= 0:
        start, end = rng
        chunk = body[start:end + 1]
        headers["content-range"] = f"bytes {start}-{end}/{total}"
        headers["content-length"] = str(len(chunk))
        return Response(content=chunk, status_code=206, headers=headers)

    headers["content-length"] = str(total)
    if request.method == "HEAD":
        return Response(content=b"", status_code=200, headers=headers)
    return Response(content=body, status_code=200, headers=headers)


_DEFAULT_CACHE: EdgeCache = EdgeCache()


def default_cache() -> EdgeCache:
    """Process-wide edge cache shared by the standalone port and the API server."""
    return _DEFAULT_CACHE


def build_slices_app(engine, secret: str = "", cache: Optional[EdgeCache] = None) -> FastAPI:
    app = FastAPI(title="MyDrive Slices", docs_url=None, redoc_url=None, openapi_url=None)
    edge = cache or _DEFAULT_CACHE

    @app.get("/ipfs/blocks/{h1}/{h2}/{cid}")
    @app.head("/ipfs/blocks/{h1}/{h2}/{cid}")
    async def ipfs_block(h1: str, h2: str, cid: str, request: Request):
        return await serve_block(engine, secret, cid, request, edge)

    @app.get("/slices/blocks/{h1}/{h2}/{cid}")
    @app.head("/slices/blocks/{h1}/{h2}/{cid}")
    async def legacy_block(h1: str, h2: str, cid: str, request: Request):
        return await serve_block(engine, secret, cid, request, edge)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "server": CDN_SERVER, "node": EDGE_NODE,
                "cache": edge.stats()}

    return app


def start_slices_server(app: FastAPI, host: str = "0.0.0.0", port: int = 8766):
    """在后台线程里跑独立端口的 slices 服务。"""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            server_header=False)
    server = uvicorn.Server(config)

    def _run():
        server.run()

    thread = threading.Thread(target=_run, daemon=True, name=f"slices-{port}")
    thread.start()
    return server
