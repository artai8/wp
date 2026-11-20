"""
Commercial Security, Rate Limiting, Audit Logging & Health/Metrics
===================================================================
Production SaaS hardening layer for MyDrive Commercial Edition:
- Per-IP sliding-window rate limiter (protects /api/auth/* from brute-force
  and general endpoints from abuse, returning HTTP 429 + Retry-After).
- Persistent SQLite audit log (login, VIP redeem, order paid, CID moderation).
- Real download bandwidth throttler (enforces Free vs VIP download_speed_bps).
- Health check & Prometheus text metrics exporter (/api/health, /api/metrics).
"""

from __future__ import annotations

import collections
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Deque, Dict, List, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from commercial_manager import enable_sqlite_wal


class RateLimiter:
    """Thread-safe in-memory sliding-window rate limiter."""

    def __init__(self, default_rpm: int = 240, auth_rpm: int = 30):
        self.default_rpm = int(os.environ.get("RATE_LIMIT_PER_MIN", str(default_rpm)))
        self.auth_rpm = int(os.environ.get("AUTH_RATE_LIMIT_PER_MIN", str(auth_rpm)))
        self._buckets: Dict[str, Deque[float]] = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_sec: float = 60.0) -> tuple[bool, int]:
        if limit <= 0:
            return True, 0
        now = time.time()
        cutoff = now - window_sec
        with self._lock:
            q = self._buckets[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= limit:
                retry = max(1, int(window_sec - (now - q[0])))
                return False, retry
            q.append(now)
            return True, 0


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Applies per-IP rate limiting (stricter on /api/auth/*)."""

    def __init__(self, app, limiter: RateLimiter, cluster=None):
        super().__init__(app)
        self.limiter = limiter
        self.cluster = cluster

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/static/") or path in ("/", "/api/health", "/manifest.json", "/sw.js"):
            return await call_next(request)
        ip = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or (
            request.client.host if request.client else "unknown"
        )
        is_auth = path.startswith("/api/auth/")
        limit = self.limiter.auth_rpm if is_auth else self.limiter.default_rpm
        bucket_key = f"{'auth' if is_auth else 'api'}:{ip}"
        dist = self.cluster.rate_limit_allow(bucket_key, limit, 60) if self.cluster else None
        ok, retry = dist if dist is not None else self.limiter.allow(bucket_key, limit)
        if not ok:
            return JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁，请稍后再试", "retry_after": retry},
                headers={"Retry-After": str(retry)},
            )
        return await call_next(request)


class AuditLogger:
    """SQLite-backed security & commercial audit trail."""

    def __init__(self, storage_root: str):
        self.db_path = Path(storage_root) / "db" / "audit.sqlite"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    user_id TEXT DEFAULT '',
                    action TEXT NOT NULL,
                    target TEXT DEFAULT '',
                    ip TEXT DEFAULT '',
                    detail TEXT DEFAULT ''
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(ts DESC)")
        enable_sqlite_wal(self.db_path)

    def log(self, action: str, user_id: str = "", target: str = "", ip: str = "", detail: str = ""):
        try:
            with sqlite3.connect(str(self.db_path), timeout=3.0) as c:
                c.execute(
                    "INSERT INTO audit_logs (ts, user_id, action, target, ip, detail) VALUES (?, ?, ?, ?, ?, ?)",
                    (int(time.time()), str(user_id or ""), action, str(target or ""), str(ip or ""), str(detail or "")[:500]),
                )
        except Exception:
            pass

    def recent(self, limit: int = 100) -> List[dict]:
        with sqlite3.connect(str(self.db_path), timeout=3.0) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT id, ts, user_id, action, target, ip, detail FROM audit_logs ORDER BY id DESC LIMIT ?",
                (max(1, min(500, int(limit))),),
            ).fetchall()
        return [dict(r) for r in rows]


def throttled_chunk_iter(chunks_iter, speed_bps: int):
    """Yield bytes from an iterator while respecting a byte/sec bandwidth cap (0 = unlimited)."""
    if not speed_bps or speed_bps <= 0:
        yield from chunks_iter
        return
    slice_size = max(16384, min(262144, speed_bps // 4))
    start = time.monotonic()
    sent = 0
    for data in chunks_iter:
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            part = bytes(view[offset:offset + slice_size])
            offset += len(part)
            sent += len(part)
            yield part
            expected = sent / float(speed_bps)
            elapsed = time.monotonic() - start
            if expected > elapsed:
                time.sleep(min(1.0, expected - elapsed))


def collect_health(engine, offline_engine, media, storage_root: str) -> dict:
    """Return structured health diagnostics for Docker/K8s/Uptime probes."""
    disk = shutil.disk_usage(storage_root)
    return {
        "status": "ok",
        "timestamp": int(time.time()),
        "components": {
            "storage_db": engine.db_path.exists(),
            "cold_tier_active": bool(engine.tiered.backend is not None),
            "cold_tier_type": engine.tiered.config.get("cold_type", "none"),
            "ffmpeg": bool(media.ffmpeg),
            "aria2": bool(offline_engine.aria2.alive()),
        },
        "disk": {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
            "free_percent": round(disk.free / disk.total * 100, 2) if disk.total else 0,
        },
    }


def export_prometheus_metrics(engine, commercial, storage_root: str) -> str:
    """Render Prometheus text exposition format metrics."""
    st = engine.get_storage_stats()
    ts = engine.tiered.get_stats()
    disk = shutil.disk_usage(storage_root)
    blocked_cnt = len(commercial.list_blocked_cids())
    lines = [
        "# HELP mydrive_files_total Total stored logical files",
        "# TYPE mydrive_files_total gauge",
        f"mydrive_files_total {st['total_files']}",
        "# HELP mydrive_chunks_total Total physical content-addressed chunks",
        "# TYPE mydrive_chunks_total gauge",
        f"mydrive_chunks_total {st['total_chunks']}",
        "# HELP mydrive_logical_bytes Total logical file bytes",
        "# TYPE mydrive_logical_bytes gauge",
        f"mydrive_logical_bytes {st['logical_bytes']}",
        "# HELP mydrive_local_cache_bytes Local hot chunk cache bytes",
        "# TYPE mydrive_local_cache_bytes gauge",
        f"mydrive_local_cache_bytes {ts['local_cache_bytes']}",
        "# HELP mydrive_cold_chunks_total Chunks offloaded to cold storage",
        "# TYPE mydrive_cold_chunks_total gauge",
        f"mydrive_cold_chunks_total {ts['cold_chunks']}",
        "# HELP mydrive_dedup_ratio Storage deduplication ratio",
        "# TYPE mydrive_dedup_ratio gauge",
        f"mydrive_dedup_ratio {st['dedup_ratio']:.6f}",
        "# HELP mydrive_blocked_cids_total Moderated blocked CID count",
        "# TYPE mydrive_blocked_cids_total gauge",
        f"mydrive_blocked_cids_total {blocked_cnt}",
        "# HELP mydrive_disk_free_bytes Free bytes on storage volume",
        "# TYPE mydrive_disk_free_bytes gauge",
        f"mydrive_disk_free_bytes {disk.free}",
    ]
    return "\n".join(lines) + "\n"
