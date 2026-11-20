"""
Protocol Conventions — HalalCloud v6 alignment
==============================================
Everything in this module was reverse-engineered from the live API
(see 清真云协议逆向报告.md, sections 8.x) and is shared by the storage
engine, the HTTP API and the gRPC-Web compatibility layer.

Four conventions are reproduced here:

1. identity      — 32 lowercase hex chars (uuid4 with dashes removed)
2. File.type     — integer category code (20=dir, 40=video, ...)
3. cursor        — base64(JSON) paging token, not a plain offset
4. auth_key      — 4-segment presigned download credential:
                   ``<expire_ts>-<key_id>-<user_identity>-<signature>``
"""

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

# ==================== IDENTITY ====================

IDENTITY_HEX_LEN = 32


def new_identity() -> str:
    """32 lowercase hex chars — the identity format used across the v6 API."""
    return uuid.uuid4().hex


def is_identity(value: str) -> bool:
    """True when *value* looks like a v6 identity (32 hex chars)."""
    if not value or len(value) != IDENTITY_HEX_LEN:
        return False
    return all(c in "0123456789abcdef" for c in value.lower())


# ==================== FILE TYPE CODES ====================
# Observed on the live service:
#   directory         -> 20
#   video/mp4         -> 40
# The remaining codes follow the same tens-based scheme.

TYPE_UNKNOWN = 0     # proto default / legacy rows (sorted last)
TYPE_DOCUMENT = 10
TYPE_DIRECTORY = 20
TYPE_AUDIO = 30
TYPE_VIDEO = 40
TYPE_IMAGE = 50
TYPE_ARCHIVE = 60
TYPE_OTHER = 60      # unclassified files share the archive bucket,
                     # so ``type ASC`` keeps directories (20) first

_TYPE_BY_MIME_PREFIX = [
    ("video/", TYPE_VIDEO),
    ("audio/", TYPE_AUDIO),
    ("image/", TYPE_IMAGE),
    ("text/", TYPE_DOCUMENT),
    ("application/pdf", TYPE_DOCUMENT),
    ("application/msword", TYPE_DOCUMENT),
    ("application/vnd.openxmlformats", TYPE_DOCUMENT),
    ("application/vnd.ms-", TYPE_DOCUMENT),
    ("application/zip", TYPE_ARCHIVE),
    ("application/x-tar", TYPE_ARCHIVE),
    ("application/x-7z", TYPE_ARCHIVE),
    ("application/x-rar", TYPE_ARCHIVE),
    ("application/gzip", TYPE_ARCHIVE),
]

_ARCHIVE_EXT = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".iso",
}


def file_type_of(name: str = "", mime_type: str = "", is_dir: bool = False) -> int:
    """Map a name / mime pair onto the v6 integer category code."""
    if is_dir:
        return TYPE_DIRECTORY
    mime = (mime_type or "").lower()
    for prefix, code in _TYPE_BY_MIME_PREFIX:
        if mime.startswith(prefix):
            return code
    ext = os.path.splitext(name or "")[1].lower()
    if ext in _ARCHIVE_EXT:
        return TYPE_ARCHIVE
    if ext in (".txt", ".md", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf", ".csv"):
        return TYPE_DOCUMENT
    return TYPE_OTHER


def guess_mime(name: str) -> str:
    mime, _ = mimetypes.guess_type(name or "")
    return mime or "application/octet-stream"


_SORT_SEP = re.compile(r"[._\-\s]+")


def sort_name_of(name: str) -> str:
    """Search/sort key: separators collapsed to single spaces, latin lowered.

    Live sample: ``스웨디시 1.mp4`` -> ``스웨디시 1 mp4``
    """
    base = (name or "").strip()
    key = _SORT_SEP.sub(" ", base).strip()
    return key.lower()


# ==================== PAGING CURSOR ====================

DEFAULT_ORDER_BY = [{"field": "type", "asc": True}, {"field": "create_ts"}]


def _now_iso() -> str:
    """Local ISO8601 with nanosecond-style precision, matching the service."""
    now = datetime.now(timezone(timedelta(hours=8)))
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f") + "000+08:00"


def encode_cursor(user_identity: str, skip: int, limit: int,
                  create_time: Optional[str] = None,
                  order_by: Optional[list] = None) -> str:
    """Build the base64(JSON) paging token the v6 API returns."""
    payload = {
        "user_identity": user_identity or "",
        "skip": int(skip),
        "limit": int(limit),
        "create_time": create_time or _now_iso(),
        "version": 1,
        "order_by": order_by if order_by is not None else DEFAULT_ORDER_BY,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(token: str) -> Optional[dict]:
    """Parse a cursor token; returns None when it is absent or malformed."""
    if not token:
        return None
    try:
        padded = token + "=" * ((4 - len(token) % 4) % 4)
        payload = json.loads(base64.b64decode(padded).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        payload["skip"] = int(payload.get("skip", 0))
        payload["limit"] = int(payload.get("limit", 0))
    except (TypeError, ValueError):
        return None
    return payload


# ==================== PRESIGNED DOWNLOAD (auth_key) ====================

PRESIGN_KEY_ID = os.environ.get("MYDRIVE_PRESIGN_KEY_ID", "1")
PRESIGN_SECRET = os.environ.get("MYDRIVE_PRESIGN_SECRET", "")
PRESIGN_TTL = int(os.environ.get("MYDRIVE_PRESIGN_TTL", "7200"))
# 线上实测路径：/ipfs/blocks/<a>/<b>/<cid>，a 恒为 "2"
SLICE_PATH_PREFIX = os.environ.get("MYDRIVE_SLICE_PREFIX", "/ipfs/blocks")
SLICES_PUBLIC_URL = os.environ.get("SLICES_PUBLIC_URL", "").rstrip("/")


def _presign_secret(fallback: str) -> str:
    return PRESIGN_SECRET or fallback


# 线上 kid 是 8 字符、每条 URL 各不相同的随机串；session 是 32 hex、每条不同。
_B62 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_B32 = "abcdefghijklmnopqrstuvwxyz234567"


def _new_kid() -> str:
    """8-char key id, randomly generated per credential (matches live samples)."""
    return "".join(secrets.choice(_B62) for _ in range(8))


def new_session() -> str:
    """32-hex session id, prefixed with '-' on the wire (``session=-<32hex>``)."""
    return "-" + secrets.token_hex(16)


def parse_session(raw: str) -> str:
    """Normalise the ``?session=`` value (keep the leading dash, as sent)."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    return raw if raw.startswith("-") else "-" + raw.lstrip("-")


def _sign(cid: str, expire_ts: int, user_identity: str, key_id: str, secret: str,
          session: str = "") -> str:
    """Signature over path + expiry + kid + user (+ session when present)."""
    path = f"{SLICE_PATH_PREFIX}/{slice_shard(cid)}/{cid}"
    msg = f"{path}|{expire_ts}|{key_id}|{user_identity}"
    if session:
        msg += f"|{session}"
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def build_auth_key(cid: str, user_identity: str, secret: str,
                   ttl: int = PRESIGN_TTL, key_id: str = "",
                   session: str = "") -> str:
    """4-segment credential: ``<expire_ts>-<key_id>-<user_identity>-<sig>``.

    ``key_id`` defaults to a fresh 8-char random id per call, which is what the
    live service does (224 sampled URLs carried 224 distinct kids).
    """
    expire_ts = int(time.time()) + max(60, int(ttl))
    kid = key_id or _new_kid()
    sig = _sign(cid, expire_ts, user_identity or "", kid,
                _presign_secret(secret), session)
    return f"{expire_ts}-{kid}-{user_identity or ''}-{sig}"


def build_slice_query(cid: str, user_identity: str, secret: str,
                      ttl: int = PRESIGN_TTL) -> dict:
    """Return ``{"auth_key": ..., "session": "-<32hex>"}`` for one slice URL."""
    session = new_session()
    return {"auth_key": build_auth_key(cid, user_identity, secret, ttl, session=session),
            "session": session}


def split_auth_key(auth_key: str) -> Optional[dict]:
    """Parse ``<expire>-<key_id>-<user_identity>-<sig>``.

    Legacy accounts carry UUID-style user ids with dashes, so the middle
    segment is taken as everything between the second field and the last.
    """
    if not auth_key:
        return None
    parts = auth_key.split("-")
    if len(parts) < 4:
        return None
    expire, key_id, signature = parts[0], parts[1], parts[-1]
    user_identity = "-".join(parts[2:-1])
    try:
        expire_ts = int(expire)
    except ValueError:
        return None
    return {"expire_ts": expire_ts, "key_id": key_id,
            "user_identity": user_identity, "signature": signature}


def verify_auth_key(cid: str, auth_key: str, secret: str, session: str = "") -> tuple:
    """Return ``(ok, reason)`` for a presigned slice credential.

    ``session`` is the ``?session=`` query value; when present it is part of the
    signed material, so a credential cannot be replayed under another session.
    """
    parts = split_auth_key(auth_key)
    if not parts:
        return False, "malformed auth_key"
    if parts["expire_ts"] < int(time.time()):
        return False, "auth_key expired"
    expected = _sign(cid, parts["expire_ts"], parts["user_identity"],
                     parts["key_id"], _presign_secret(secret), session)
    if hmac.compare_digest(parts["signature"], expected):
        return True, "ok"
    # Legacy credentials were signed without the session component.
    legacy = _sign(cid, parts["expire_ts"], parts["user_identity"],
                   parts["key_id"], _presign_secret(secret), "")
    if hmac.compare_digest(parts["signature"], legacy):
        return True, "ok"
    return False, "bad signature"


def slice_url(cid: str, auth_key: str, base: str = "", session: str = "") -> str:
    """``/ipfs/blocks/2/<x>/<cid>?auth_key=...&session=-<32hex>`` — 与线上同形。"""
    b = (base or SLICES_PUBLIC_URL).rstrip("/")
    url = f"{b}{SLICE_PATH_PREFIX}/{slice_shard(cid)}/{cid}?auth_key={auth_key}"
    if session:
        url += f"&session={session}"
    return url


def slice_shard(cid: str) -> str:
    """两级分片：第一级恒为 ``2``，第二级是 base32 字母表里的一个字符。

    线上 224 条样本的第二段只有 32 种取值，且都是 base32 字符的 ASCII 十六进制
    （``75``→``u``、``69``→``i``、``61``→``a`` …），即 32 个桶。桶内选择用的是
    服务端私有哈希，无法从样本反推；这里用 sha256 的高 5 位稳定映射到同一空间。
    """
    digest = hashlib.sha256(cid.encode("utf-8")).digest()
    return "2/" + _B32[digest[0] >> 3]


# ==================== UPLOAD TASK PARAMETERS ====================

BLOCK_SIZE = 4 * 1024 * 1024
BLOCK_HASH_TYPE = 0x12   # sha2-256
BLOCK_VERSION = 1
BLOCK_CODEC = 0x55       # raw  -> CIDv1 prefix "bafkrei"

# session_id -> {user_id, name, parent, blocks, ...}; shared by the HTTP API
# and the gRPC compatibility layer.
UPLOAD_SESSIONS: dict = {}


def new_upload_session() -> str:
    """``upload_session::<32hex>_<8 chars>:ts::<ms>``"""
    return f"upload_session::{new_identity()}_{uuid.uuid4().hex[:8]}:ts::{int(time.time() * 1000)}"


def parse_upload_session(task: str) -> Optional[str]:
    """Extract the session id from a task string, or None when malformed."""
    if not task or not task.startswith("upload_session::"):
        return None
    body = task[len("upload_session::"):]
    session_id = body.split(":ts::")[0].split("_")[0]
    return session_id or None
