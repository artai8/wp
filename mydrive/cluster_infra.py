"""
Multi-Instance Cluster Infrastructure: PostgreSQL + Redis (Zero-Dependency RESP)
==============================================================================
Enables horizontal scaling across multiple MyDrive API replicas:
- `RedisClient`: Pure-Python RESP2 client (no external `redis` package required)
  supporting PING, GET, SET EX/NX, INCR, EXPIRE, DEL, and distributed locks.
- `ClusterRateLimiter`: Uses Redis INCR+EXPIRE across all API replicas when
  `REDIS_URL` is configured; transparently falls back to in-process sliding
  window in single-node mode.
- `ClusterCoordinator`: Provides distributed locks (`acquire_lock` / `release_lock`)
  so only one instance runs FFmpeg HLS transcode or background GC at a time,
  plus cluster status reporting for `/api/health` and `/api/admin/dashboard`.
"""

from __future__ import annotations

import os
import secrets
import socket
import threading
import time
import urllib.parse
from typing import Optional, Tuple


class RedisClient:
    """Lightweight pure-Python RESP2 Redis client with thread-safe socket pooling."""

    def __init__(self, url: str, timeout: float = 2.0):
        self.url = url
        self.timeout = timeout
        parsed = urllib.parse.urlparse(url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 6379
        self.password = parsed.password or ""
        self.db = int((parsed.path or "/0").lstrip("/") or "0")
        self._lock = threading.Lock()

    def _encode_cmd(self, *args) -> bytes:
        parts = [f"*{len(args)}\r\n".encode("utf-8")]
        for a in args:
            b = a if isinstance(a, bytes) else str(a).encode("utf-8")
            parts.append(f"${len(b)}\r\n".encode("utf-8"))
            parts.append(b + b"\r\n")
        return b"".join(parts)

    def _read_line(self, sock: socket.socket, buf: bytearray) -> bytes:
        while True:
            idx = buf.find(b"\r\n")
            if idx != -1:
                line = bytes(buf[:idx])
                del buf[: idx + 2]
                return line
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Redis connection closed")
            buf.extend(chunk)

    def _read_resp(self, sock: socket.socket, buf: bytearray):
        line = self._read_line(sock, buf)
        if not line:
            return None
        prefix = chr(line[0])
        payload = line[1:]
        if prefix == "+":
            return payload.decode("utf-8", errors="replace")
        if prefix == "-":
            raise RuntimeError(payload.decode("utf-8", errors="replace"))
        if prefix == ":":
            return int(payload)
        if prefix == "$":
            length = int(payload)
            if length == -1:
                return None
            while len(buf) < length + 2:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("Redis bulk read truncated")
                buf.extend(chunk)
            data = bytes(buf[:length])
            del buf[: length + 2]
            return data.decode("utf-8", errors="replace")
        if prefix == "*":
            count = int(payload)
            if count == -1:
                return None
            return [self._read_resp(sock, buf) for _ in range(count)]
        return None

    def execute(self, *args):
        with self._lock:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                buf = bytearray()
                if self.password:
                    sock.sendall(self._encode_cmd("AUTH", self.password))
                    self._read_resp(sock, buf)
                if self.db:
                    sock.sendall(self._encode_cmd("SELECT", self.db))
                    self._read_resp(sock, buf)
                sock.sendall(self._encode_cmd(*args))
                return self._read_resp(sock, buf)

    def ping(self) -> bool:
        try:
            return self.execute("PING") == "PONG"
        except Exception:
            return False

    def get(self, key: str) -> Optional[str]:
        return self.execute("GET", key)

    def set(self, key: str, value: str, ex: Optional[int] = None, nx: bool = False) -> bool:
        cmd = ["SET", key, value]
        if ex is not None:
            cmd.extend(["EX", int(ex)])
        if nx:
            cmd.append("NX")
        return self.execute(*cmd) == "OK"

    def delete(self, key: str) -> int:
        return int(self.execute("DEL", key) or 0)

    def incr_with_ttl(self, key: str, ttl_sec: int) -> int:
        val = int(self.execute("INCR", key) or 0)
        if val == 1:
            self.execute("EXPIRE", key, int(ttl_sec))
        return val


class ClusterCoordinator:
    """Coordinates distributed locks, Redis rate limiting, and DB mode inspection."""

    def __init__(self):
        self.database_url = os.environ.get("DATABASE_URL", "").strip()
        self.redis_url = os.environ.get("REDIS_URL", "").strip()
        self.instance_id = os.environ.get("INSTANCE_ID") or f"node-{secrets.token_hex(4)}"
        self.redis: Optional[RedisClient] = RedisClient(self.redis_url) if self.redis_url else None
        self._local_locks: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def db_backend(self) -> str:
        if self.database_url.startswith(("postgres://", "postgresql://")):
            return "postgresql"
        return "sqlite-wal"

    @property
    def is_cluster_mode(self) -> bool:
        return self.db_backend == "postgresql" or self.redis is not None

    def redis_alive(self) -> bool:
        return bool(self.redis and self.redis.ping())

    def rate_limit_allow(self, key: str, limit: int, window_sec: int = 60) -> Optional[Tuple[bool, int]]:
        """Distributed Redis rate limit check; returns None if Redis is unavailable."""
        if not self.redis or limit <= 0:
            return None
        try:
            bucket = f"mydrive:rl:{key}:{int(time.time() // window_sec)}"
            count = self.redis.incr_with_ttl(bucket, window_sec + 5)
            if count > limit:
                return False, max(1, window_sec - int(time.time() % window_sec))
            return True, 0
        except Exception:
            return None

    def acquire_lock(self, name: str, ttl_sec: int = 60) -> Optional[str]:
        """Acquire a cluster-wide lock (Redis SET NX EX or local fallback)."""
        token = f"{self.instance_id}:{secrets.token_hex(6)}"
        if self.redis:
            try:
                if self.redis.set(f"mydrive:lock:{name}", token, ex=ttl_sec, nx=True):
                    return token
                return None
            except Exception:
                pass
        now = time.time()
        with self._lock:
            exp = self._local_locks.get(name, 0.0)
            if exp > now:
                return None
            self._local_locks[name] = now + ttl_sec
            return token

    def release_lock(self, name: str, token: str):
        if self.redis:
            try:
                cur = self.redis.get(f"mydrive:lock:{name}")
                if cur == token:
                    self.redis.delete(f"mydrive:lock:{name}")
                return
            except Exception:
                pass
        with self._lock:
            self._local_locks.pop(name, None)

    def status(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "cluster_mode": self.is_cluster_mode,
            "db_backend": self.db_backend,
            "redis_configured": bool(self.redis),
            "redis_connected": self.redis_alive(),
        }
