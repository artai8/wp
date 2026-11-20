"""
gRPC-Web compatibility layer
============================
Speaks the HalalCloud v6 protocol on the wire so an unmodified client can be
pointed at MyDrive.

Two transports are served from the same handler table:

1. **gRPC-Web** — ``POST /v6.services.pub.<Service>/<Method>``
   body is ``[flag:1][length:4][protobuf]``, trailers carry ``grpc-status``.
   Both ``application/grpc-web+proto`` and the base64 ``-text`` variant work.

2. **REST gateway** — ``POST /v6/<service>/<method>`` with a JSON body.
   This mirrors the ``google.api.http`` annotations in the official protos:
   camelCase in, camelCase out, every endpoint a POST.

Authentication reproduces the observed scheme:

    timestamp / appid / appversion / sign   (+ authorization: Bearer <jwt>)
    sign = MD5("/" + service + "/" + method + timestamp + appid + appversion
               [+ "Bearer " + token] + appsecret)

Set ``MYDRIVE_REQUIRE_SIGN=1`` to reject unsigned calls; by default a request
without a ``sign`` header is accepted (convenient for local tooling), but any
signature that *is* present must be valid.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import time
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import JSONResponse

import pb
import protocol as proto
import v6_extra
from v6_extra import ExtraError

APP_ID = os.environ.get("MYDRIVE_APP_ID", "2dland/web")
APP_VERSION = os.environ.get("MYDRIVE_APP_VERSION", "202603182100")
# 本地 lab 的默认值；真实部署请通过环境变量 MYDRIVE_APP_SECRET 注入，
# 切勿把生产环境的密钥提交进仓库。
APP_SECRET = os.environ.get("MYDRIVE_APP_SECRET", "dev-only-insecure-secret")
REQUIRE_SIGN = os.environ.get("MYDRIVE_REQUIRE_SIGN", "0").lower() in ("1", "true", "yes")
SIGN_SKEW_MS = int(os.environ.get("MYDRIVE_SIGN_SKEW_MS", str(5 * 60 * 1000)))

GRPC_INTERNAL = 13


class GrpcError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


# ==================== CONTEXT ====================

class Ctx:
    """Per-request context handed to every RPC handler."""

    def __init__(self, engine, auth, presign_secret: str, base_url: str = "",
                 user: Optional[dict] = None, share=None, offline=None, sftp=None):
        self.engine = engine
        self.auth = auth
        self.share = share
        self.offline = offline
        self.sftp = sftp
        self.presign_secret = presign_secret
        self.base_url = base_url.rstrip("/")
        self.user = user

    @property
    def uid(self) -> Optional[str]:
        return (self.user or {}).get("user_id")

    @property
    def owner(self) -> str:
        return self.uid or "anonymous"


# ==================== SIGNATURE ====================

def compute_sign(service: str, method: str, timestamp: str, appid: str,
                 appversion: str, token: str = "", secret: str = APP_SECRET) -> str:
    plain = f"/{service}/{method}{timestamp}{appid}{appversion}"
    if token:
        plain += "Bearer " + token
    return hashlib.md5((plain + secret).encode("utf-8")).hexdigest()


def verify_sign(service: str, method: str, headers: Dict[str, str],
                token: str = "") -> Tuple[bool, str]:
    """Validate the v6 request signature. Returns ``(ok, reason)``."""
    sign = (headers.get("sign") or "").strip().lower()
    timestamp = (headers.get("timestamp") or "").strip()

    if not sign:
        return (True, "unsigned") if not REQUIRE_SIGN else (False, "missing sign")

    if not timestamp:
        return False, "missing timestamp"
    try:
        skew = abs(int(timestamp) - int(time.time() * 1000))
    except ValueError:
        return False, "invalid timestamp"
    if skew > SIGN_SKEW_MS:
        return False, "timestamp expired"

    expected = compute_sign(
        service, method, timestamp,
        headers.get("appid", ""), headers.get("appversion", ""), token,
    )
    if not hmac.compare_digest(expected, sign):
        return False, "invalid sign"
    return True, "ok"


# ==================== WIRE HELPERS ====================

def unwrap_frame(raw: bytes) -> bytes:
    """Strip the 5-byte gRPC-Web frame header from the first data frame."""
    if len(raw) < 5:
        return b""
    flag = raw[0]
    length = int.from_bytes(raw[1:5], "big")
    if flag & 0x80:
        return b""
    return raw[5:5 + length]


def wrap_frame(payload: bytes) -> bytes:
    return bytes([0]) + len(payload).to_bytes(4, "big") + payload


def grpc_response(code: int, message: str, payload: bytes = b"",
                  is_text: bool = False) -> Response:
    body = b""
    if payload:
        body += wrap_frame(payload)
    trailer = f"grpc-status: {code}\r\ngrpc-message: {quote(message or '')}\r\n".encode()
    body += bytes([0x80]) + len(trailer).to_bytes(4, "big") + trailer

    headers = {
        "grpc-status": str(code),
        "grpc-message": quote(message or ""),
    }
    if is_text:
        return Response(content=base64.b64encode(body),
                        media_type="application/grpc-web-text+proto",
                        headers=headers)
    return Response(content=body, media_type="application/grpc-web+proto",
                    headers=headers)


_CAMEL_TO_SNAKE = re.compile(r"(?<!^)(?=[A-Z])")


def to_snake(obj: Any) -> Any:
    """camelCase JSON (REST gateway) -> snake_case field names."""
    if isinstance(obj, dict):
        return {_CAMEL_TO_SNAKE.sub("_", k).lower(): to_snake(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_snake(v) for v in obj]
    return obj


def to_camel(obj: Any) -> Any:
    """snake_case -> camelCase, matching proto3 JSON mapping."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            parts = key.split("_")
            name = parts[0] + "".join(p.title() for p in parts[1:])
            out[name] = to_camel(value)
        return out
    if isinstance(obj, list):
        return [to_camel(v) for v in obj]
    return obj


def _bearer(headers: Dict[str, str]) -> str:
    raw = headers.get("authorization", "")
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return ""


# ==================== MESSAGE MAPPING ====================

def file_pb(meta) -> dict:
    """FileMeta -> v6 ``File`` message dict."""
    return {
        "identity": meta.identity,
        "parent": meta.parent,
        "name": meta.name,
        "path": meta.path,
        "mime_type": meta.mime_type,
        "size": meta.size,
        "type": meta.type,
        "create_ts": meta.create_ts,
        "update_ts": meta.update_ts,
        "delete_ts": meta.delete_ts,
        "deleted": meta.deleted,
        "dir": meta.is_dir,
        "hidden": meta.hidden,
        "locked": meta.locked,
        "shared": meta.shared,
        "starred": meta.starred,
        "trashed": meta.trashed,
        "content_identity": meta.content_identity,
        "store_type": meta.store_type,
        "version": meta.version,
        "files": meta.files,
        "directories": meta.directories,
        "nodes": meta.nodes,
        "sort_name": meta.sort_name,
    }


def user_pb(record: dict, name: str = "") -> dict:
    return {
        "identity": record.get("user_id", ""),
        "type": 1,
        "status": 1,
        "name": name or record.get("username", ""),
        "create_ts": int(record.get("created_ts") or 0),
    }


def _offline_quota(ctx: Ctx) -> dict:
    engine = getattr(ctx, "offline", None)
    if engine is not None:
        st = engine.stats(ctx.owner)
        return {
            "daily_tasks_quota": st.get("daily_tasks_quota", 1024),
            "tasks_commited_today": st.get("tasks_commited_today", 0),
            "daily_bytes_quota": st.get("daily_bytes_quota", 10 ** 13),
            "bytes_commited_today": st.get("bytes_commited_today", 0),
        }
    return {"daily_tasks_quota": int(os.environ.get("MYDRIVE_OFFLINE_QUOTA", 100)),
            "tasks_commited_today": 0, "daily_bytes_quota": 10 ** 13,
            "bytes_commited_today": 0}


def _quota_pb(ctx: Ctx) -> dict:
    quota = ctx.auth.get_quota(ctx.owner)
    stats = ctx.engine.get_storage_stats(user_id=ctx.owner)
    total = int(quota.get("quota") or 0)
    used = int(quota.get("used") or 0)
    return {
        "disk_statistics_quota": {
            "bytes_quota": total,
            "bytes_used": used,
            "bytes_free": max(0, total - used),
            "files_quota": 100000,
            "files_used": int(stats.get("total_files", 0)),
        },
        "traffic_statistics_quota": {
            "daily_bytes_download_quota": int(os.environ.get("MYDRIVE_DL_QUOTA", 1024 ** 4)),
            "daily_bytes_upload_quota": int(os.environ.get("MYDRIVE_UL_QUOTA", 10 * 1024 ** 3)),
        },
        "offline_task_statistics_quota": _offline_quota(ctx),
    }


def _batch_ok(affected: int) -> dict:
    now = int(time.time() * 1000)
    return {"task": proto.new_identity(), "status": 0, "affected": affected,
            "create_ts": now, "progress": affected, "total": affected}


def _require_identity(req: dict, field: str = "identity") -> str:
    value = req.get(field) or ""
    if not value:
        raise GrpcError(pb.GRPC_INVALID_ARGUMENT, f"{field} required")
    return value


# ==================== HANDLERS: PubUser ====================

def h_login(ctx: Ctx, req: dict) -> dict:
    identity = req.get("identity") or req.get("input") or ""
    password = req.get("password") or ""
    if not identity or not password:
        raise GrpcError(pb.GRPC_INVALID_ARGUMENT, "identity and password required")

    result = ctx.auth.login(identity, password)
    if not result.get("success"):
        raise GrpcError(pb.GRPC_UNAUTHENTICATED, result.get("error", "login failed"))

    now = int(time.time() * 1000)
    record = ctx.auth.get_user(result["user_id"]) or {}
    return {
        "token": {
            "identity": proto.new_identity(),
            "user_identity": result["user_id"],
            "device": req.get("device", ""),
            "version": 1,
            "access_token": result["access_token"],
            "refresh_token": result["refresh_token"],
            "update_ts": now,
            "access_token_expire_ts": now + 3600 * 1000,
            "refresh_token_expire_ts": now + 30 * 86400 * 1000,
        },
        "user": user_pb(record, result.get("username", "")),
        "state": req.get("state", ""),
        "code": "",
    }


def h_get_user(ctx: Ctx, req: dict) -> dict:
    target = req.get("identity") or ctx.uid
    record = ctx.auth.get_user(target) if target else None
    if not record:
        raise GrpcError(pb.GRPC_NOT_FOUND, "user not found")
    return user_pb(record)


def h_quota(ctx: Ctx, req: dict) -> dict:
    return _quota_pb(ctx)


def h_settings(ctx: Ctx, req: dict) -> dict:
    return {"identity": ctx.owner, "settings": b"{}"}


def h_refresh(ctx: Ctx, req: dict) -> dict:
    token = req.get("refresh_token") or ""
    result = ctx.auth.refresh(token)
    if not result.get("success"):
        raise GrpcError(pb.GRPC_UNAUTHENTICATED, result.get("error", "refresh failed"))
    now = int(time.time() * 1000)
    return {
        "identity": proto.new_identity(),
        "access_token": result["access_token"],
        "refresh_token": result["refresh_token"],
        "update_ts": now,
        "access_token_expire_ts": now + 3600 * 1000,
        "refresh_token_expire_ts": now + 30 * 86400 * 1000,
    }


# ==================== HANDLERS: PubUserFile ====================

def h_list(ctx: Ctx, req: dict) -> dict:
    parent = (req.get("parent") or {}).get("identity") or "root"
    info = req.get("list_info") or {}
    result = ctx.engine.scan_list(
        parent, user_id=ctx.uid,
        limit=int(info.get("limit") or 100),
        token=info.get("token") or None,
    )
    return {
        "files": [file_pb(m) for m in result["files"]],
        "list_info": {"token": result["list_info"]["token"],
                      "limit": result["list_info"]["limit"]},
    }


def h_list_trash(ctx: Ctx, req: dict) -> dict:
    info = req.get("list_info") or {}
    result = ctx.engine.scan_list(
        "root", user_id=ctx.uid, limit=int(info.get("limit") or 100),
        token=info.get("token") or None, only_trashed=True,
    )
    return {
        "files": [file_pb(m) for m in result["files"]],
        "list_info": {"token": result["list_info"]["token"],
                      "limit": result["list_info"]["limit"]},
    }


def h_search(ctx: Ctx, req: dict) -> dict:
    info = req.get("list_info") or {}
    result = ctx.engine.search_files(
        req.get("name") or "", user_id=ctx.uid,
        limit=int(info.get("limit") or 100), token=info.get("token") or None,
    )
    return {
        "files": [file_pb(m) for m in result["files"]],
        "list_info": {"token": result["list_info"]["token"],
                      "limit": result["list_info"]["limit"]},
    }


def h_get_file(ctx: Ctx, req: dict) -> dict:
    identity = _require_identity(req)
    meta = ctx.engine.get_file_meta(identity, user_id=ctx.uid)
    if not meta:
        raise GrpcError(pb.GRPC_NOT_FOUND, "file not found")
    return file_pb(meta)


def h_create(ctx: Ctx, req: dict) -> dict:
    name = req.get("name") or ""
    if not name:
        raise GrpcError(pb.GRPC_INVALID_ARGUMENT, "name required")
    parent = req.get("parent") or "root"
    if req.get("dir"):
        meta = ctx.engine.create_dir(name, parent, user_id=ctx.owner)
    else:
        meta = ctx.engine.chunk_data(b"", name=name, parent=parent, user_id=ctx.owner)
    return file_pb(meta)


def h_rename(ctx: Ctx, req: dict) -> dict:
    identity = _require_identity(req)
    name = req.get("name") or ""
    if not ctx.engine.rename(identity, name, user_id=ctx.uid):
        raise GrpcError(pb.GRPC_NOT_FOUND, "file not found")
    return _batch_ok(1)


def _batch_resolve(ctx: Ctx, req: dict) -> list:
    sources = req.get("source") or []
    if isinstance(sources, dict):
        sources = [sources]
    return [s.get("identity") for s in sources if s.get("identity")]


def h_move(ctx: Ctx, req: dict) -> dict:
    dest = (req.get("dest") or {}).get("identity") or "root"
    affected = 0
    for identity in _batch_resolve(ctx, req):
        if ctx.engine.move(identity, dest, user_id=ctx.uid):
            affected += 1
    return _batch_ok(affected)


def h_trash(ctx: Ctx, req: dict) -> dict:
    affected = 0
    for identity in _batch_resolve(ctx, req):
        if ctx.engine.set_trashed(identity, trashed=True, user_id=ctx.uid):
            affected += 1
    return _batch_ok(affected)


def h_recover(ctx: Ctx, req: dict) -> dict:
    affected = 0
    for identity in _batch_resolve(ctx, req):
        if ctx.engine.set_trashed(identity, trashed=False, user_id=ctx.uid):
            affected += 1
    return _batch_ok(affected)


def h_delete(ctx: Ctx, req: dict) -> dict:
    affected = 0
    for identity in _batch_resolve(ctx, req):
        if ctx.engine.set_trashed(identity, permanent=True, user_id=ctx.uid):
            affected += 1
    return _batch_ok(affected)


def h_upload_task(ctx: Ctx, req: dict) -> dict:
    """CreateUploadTask — returns the exact block parameters the service uses."""
    session = proto.new_upload_session()
    session_id = proto.parse_upload_session(session)
    proto.UPLOAD_SESSIONS[session_id] = {
        "user_id": ctx.owner,
        "name": req.get("name") or "unnamed",
        "parent": req.get("parent") or "root",
        "file_size": int(req.get("size") or 0),
        "sha1": "",
        "head_sha1": "",
        "blocks": {},
        "created_ts": int(time.time()),
    }
    return {
        "created": False,
        "upload_address": f"{ctx.base_url}/api/upload/block",
        "task": session,
        "upload_token": session_id,
        "block_size": proto.BLOCK_SIZE,
        "block_hash_type": proto.BLOCK_HASH_TYPE,
        "block_version": proto.BLOCK_VERSION,
        "block_codec": proto.BLOCK_CODEC,
    }


def h_slices_download(ctx: Ctx, req: dict) -> dict:
    file_req = req.get("file") or {}
    identity = file_req.get("identity") or ""
    meta = ctx.engine.get_file_meta(identity, user_id=ctx.uid)
    if not meta:
        raise GrpcError(pb.GRPC_NOT_FOUND, "file not found")

    info = req.get("list_info") or {}
    cursor = proto.decode_cursor(info.get("token") or "") or {}
    skip = max(0, int(cursor.get("skip", 0) or 0))
    take = max(1, min(int(info.get("limit") or cursor.get("limit") or 100), 1000))
    window = meta.chunks[skip:skip + take]

    slices = []
    for chunk in window:
        cred = proto.build_slice_query(chunk.cid, meta.user_id, ctx.presign_secret,
                                      ttl=proto.PRESIGN_TTL)
        slices.append({
            "identity": chunk.cid,
            "download_address": proto.slice_url(chunk.cid, cred["auth_key"], "",
                                                cred["session"]),
            "download_token": cred["auth_key"],
            "encrypt": 0,
            "store_type": 0,
        })

    next_skip = skip + len(slices)
    next_token = proto.encode_cursor(meta.user_id, next_skip, take) if next_skip < len(meta.chunks) else ""
    return {
        "slices": slices,
        "list_info": {"token": next_token, "limit": take},
        "file": file_pb(meta),
    }


def h_add_fastlookup(ctx: Ctx, req: dict) -> dict:
    """Register or look up an instant-transfer fingerprint."""
    info = req.get("info") or {}
    sha1 = info.get("sha1") or ""
    head = info.get("head_sha1") or ""
    size = int(info.get("file_size") or 0)
    if not sha1:
        raise GrpcError(pb.GRPC_INVALID_ARGUMENT, "sha1 required")

    entry = ctx.engine._fast_lookup(sha1, head, size, user_id=ctx.uid)
    if not entry:
        ctx.engine._add_fast_lookup(sha1, head, size, "", info.get("name") or "",
                                    user_id=ctx.owner)
        entry = ctx.engine._fast_lookup(sha1, head, size, user_id=ctx.uid)
    if not entry:
        return {}
    return {
        "identity": entry.identity,
        "sha1": entry.sha1,
        "head_sha1": entry.head_sha1,
        "file_size": entry.file_size,
        "name": entry.name,
    }


def h_get_fastlookup(ctx: Ctx, req: dict) -> dict:
    info = req.get("info") or {}
    entry = ctx.engine._fast_lookup(
        info.get("sha1") or "", info.get("head_sha1") or "",
        int(info.get("file_size") or 0), user_id=ctx.uid,
    )
    if not entry:
        return {}
    return {
        "identity": entry.identity,
        "sha1": entry.sha1,
        "head_sha1": entry.head_sha1,
        "file_size": entry.file_size,
        "name": entry.name,
    }


# ==================== HANDLERS: PubFileShare ====================

def _share_pb(ctx: Ctx, share_id: str) -> dict:
    record = ctx.share.get_record(share_id)
    if not record:
        raise GrpcError(pb.GRPC_NOT_FOUND, "share not found")
    meta = ctx.engine.get_file_meta(record["file_identity"])
    return {
        "identity": record["share_id"],
        "user_identity": record["user_identity"],
        "type": meta.type if meta else 0,
        "mime_type": meta.mime_type if meta else "",
        "name": meta.name if meta else "",
        "has_password": record["has_password"],
        "lifetime": 0,
        "save_count": record["save_count"],
        "like": record["like"],
        "dislike": record["dislike"],
        "create_ts": record["create_ts"],
        "expire_ts": record["expire_ts"],
        "file_size": meta.size if meta else 0,
    }


def h_share_create(ctx: Ctx, req: dict) -> dict:
    paths = req.get("path_list") or []
    if isinstance(paths, str):
        paths = [paths]
    file_identity = req.get("identity") or (paths[0] if paths else "")
    if not file_identity:
        raise GrpcError(pb.GRPC_INVALID_ARGUMENT, "path_list required")

    lifetime = int(req.get("lifetime") or 0)
    result = ctx.share.create_share(
        file_identity, req.get("password") or "",
        expire_hours=max(1, lifetime // 3600) if lifetime else 0,
        download_limit=int(req.get("save_limit") or 0),
        user_id=ctx.owner,
    )
    if not result.get("success"):
        raise GrpcError(pb.GRPC_NOT_FOUND, result.get("error", "create failed"))
    return _share_pb(ctx, result["share_id"])


def h_share_get(ctx: Ctx, req: dict) -> dict:
    return _share_pb(ctx, _require_identity(req))


def h_share_like(ctx: Ctx, req: dict) -> dict:
    result = ctx.share.react(_require_identity(req), "like")
    if not result.get("success"):
        raise GrpcError(pb.GRPC_NOT_FOUND, result.get("error", "share not found"))
    return _share_pb(ctx, req["identity"])


def h_share_dislike(ctx: Ctx, req: dict) -> dict:
    result = ctx.share.react(_require_identity(req), "dislike")
    if not result.get("success"):
        raise GrpcError(pb.GRPC_NOT_FOUND, result.get("error", "share not found"))
    return _share_pb(ctx, req["identity"])


def h_share_complaint(ctx: Ctx, req: dict) -> dict:
    share_id = req.get("file_share_identity") or ""
    if not share_id:
        raise GrpcError(pb.GRPC_INVALID_ARGUMENT, "file_share_identity required")
    result = ctx.share.add_complaint(share_id, ctx.owner, req.get("content") or "")
    if not result.get("success"):
        raise GrpcError(pb.GRPC_NOT_FOUND, result.get("error", "share not found"))
    return {
        "identity": result["identity"],
        "user_identity": ctx.owner,
        "file_share_identity": share_id,
        "content": result["content"],
        "create_ts": result["create_ts"],
        "status": 0,
    }


def h_share_list(ctx: Ctx, req: dict) -> dict:
    rows = ctx.share.list_shares(user_id=ctx.uid)
    limit = int((req.get("list_info") or {}).get("limit") or 100)
    shares = []
    for row in rows[:limit]:
        try:
            shares.append(_share_pb(ctx, row["share_id"]))
        except GrpcError:
            continue
    return {"shares": shares, "list_info": {"limit": limit}}


def h_share_delete(ctx: Ctx, req: dict) -> dict:
    identities = req.get("identity") or []
    if isinstance(identities, str):
        identities = [identities]
    count = 0
    for share_id in identities:
        ctx.share.delete_share(share_id, user_id=ctx.uid)
        count += 1
    return {"count": count}


def h_share_save(ctx: Ctx, req: dict) -> dict:
    share_id = _require_identity(req)
    result = ctx.share.save_share(
        share_id, ctx.owner,
        password=req.get("password") or "",
        parent=req.get("save_path") or "root",
        name=req.get("name") or "",
    )
    if not result.get("success"):
        raise GrpcError(pb.GRPC_NOT_FOUND, result.get("error", "save failed"))
    return _share_pb(ctx, share_id)


# ==================== RPC TABLE ====================
# (service, method) -> {in, out, fn, auth}

RPCS: Dict[Tuple[str, str], dict] = {
    ("PubUser", "Login"): {"in": "LoginRequest", "out": "LoginResponse", "fn": h_login, "auth": False},
    ("PubUser", "Get"): {"in": "User", "out": "User", "fn": h_get_user},
    ("PubUser", "GetStatisticsAndQuota"): {"in": "User", "out": "UserStatisticsAndQuota", "fn": h_quota},
    ("PubUser", "GetSettings"): {"in": "Settings", "out": "Settings", "fn": h_settings},
    ("PubUser", "Refresh"): {"in": "Token", "out": "Token", "fn": h_refresh, "auth": False},

    ("PubUserFile", "List"): {"in": "FileListRequest", "out": "FileListResponse", "fn": h_list},
    ("PubUserFile", "ListTrash"): {"in": "FileListRequest", "out": "FileListResponse", "fn": h_list_trash},
    ("PubUserFile", "Search"): {"in": "SearchRequest", "out": "FileListResponse", "fn": h_search},
    ("PubUserFile", "Get"): {"in": "File", "out": "File", "fn": h_get_file},
    ("PubUserFile", "Create"): {"in": "File", "out": "File", "fn": h_create},
    ("PubUserFile", "Rename"): {"in": "File", "out": "BatchOperationResponse", "fn": h_rename},
    ("PubUserFile", "Move"): {"in": "BatchOperationRequest", "out": "BatchOperationResponse", "fn": h_move},
    ("PubUserFile", "Trash"): {"in": "BatchOperationRequest", "out": "BatchOperationResponse", "fn": h_trash},
    ("PubUserFile", "Recover"): {"in": "BatchOperationRequest", "out": "BatchOperationResponse", "fn": h_recover},
    ("PubUserFile", "Delete"): {"in": "BatchOperationRequest", "out": "BatchOperationResponse", "fn": h_delete},
    ("PubUserFile", "DeleteTrash"): {"in": "BatchOperationRequest", "out": "BatchOperationResponse", "fn": h_delete},
    ("PubUserFile", "CreateUploadTask"): {"in": "File", "out": "UploadTask", "fn": h_upload_task},
    ("PubUserFile", "CreateTemporaryUpload"): {"in": "File", "out": "UploadTask", "fn": h_upload_task},
    ("PubUserFile", "GetFileSlicesDownloadInfo"): {
        "in": "SlicesDownloadInfoRequest", "out": "SlicesDownloadInfoResponse",
        "fn": h_slices_download,
    },

    ("PubSlice", "AddFastLookup"): {"in": "FastLookup", "out": "FastLookup",
                                    "fn": lambda ctx, req: h_add_fastlookup(ctx, {"info": req})},
    ("PubSlice", "GetFastLookup"): {"in": "FastLookupRequest", "out": "FastLookup", "fn": h_get_fastlookup},

    ("PubFileShare", "Create"): {"in": "FileShare", "out": "FileShare", "fn": h_share_create},
    ("PubFileShare", "Get"): {"in": "FileShare", "out": "FileShare", "fn": h_share_get},
    ("PubFileShare", "Like"): {"in": "FileShare", "out": "FileShare", "fn": h_share_like},
    ("PubFileShare", "Dislike"): {"in": "FileShare", "out": "FileShare", "fn": h_share_dislike},
    ("PubFileShare", "ComplaintShare"): {"in": "Complaint", "out": "Complaint", "fn": h_share_complaint},
    ("PubFileShare", "List"): {"in": "FileShareListRequest", "out": "FileShareListResponse", "fn": h_share_list},
    ("PubFileShare", "Delete"): {"in": "FileShareDeleteRequest", "out": "FileShareDeleteResponse",
                                 "fn": h_share_delete},
    ("PubFileShare", "Save"): {"in": "FileShare", "out": "FileShare", "fn": h_share_save},
}

# 扩展服务（DAV/SFTP、离线任务、工单、OAuth、切片）由 v6_extra 提供
RPCS.update(v6_extra.EXTRA_RPCS)

# REST gateway paths (google.api.http annotations)
REST_ROUTES: Dict[str, Tuple[str, str]] = {
    "/v6/userfile/list": ("PubUserFile", "List"),
    "/v6/userfile/list_trash": ("PubUserFile", "ListTrash"),
    "/v6/userfile/search": ("PubUserFile", "Search"),
    "/v6/userfile/get": ("PubUserFile", "Get"),
    "/v6/userfile/create": ("PubUserFile", "Create"),
    "/v6/userfile/rename": ("PubUserFile", "Rename"),
    "/v6/userfile/move": ("PubUserFile", "Move"),
    "/v6/userfile/trash": ("PubUserFile", "Trash"),
    "/v6/userfile/recover": ("PubUserFile", "Recover"),
    "/v6/userfile/delete": ("PubUserFile", "Delete"),
    "/v6/userfile/delete_trash": ("PubUserFile", "DeleteTrash"),
    "/v6/userfile/create_upload_task": ("PubUserFile", "CreateUploadTask"),
    "/v6/userfile/create_temporary_upload": ("PubUserFile", "CreateTemporaryUpload"),
    "/v6/userfile/get_file_slices_download_info": ("PubUserFile", "GetFileSlicesDownloadInfo"),
    "/v6/userfile/create_upload_token": ("PubUserFile", "CreateUploadToken"),
    "/v6/userfile/get_download_and_preview_info": ("PubUserFile", "GetDownloadAndPreviewInfo"),
    "/v6/userfile/parse_file_slice": ("PubUserFile", "ParseFileSlice"),
    "/v6/userfile/get_slice_download_address": ("PubUserFile", "GetSliceDownloadAddress"),
    "/v6/userfile/get_direct_download_address": ("PubUserFile", "GetDirectDownloadAddress"),
    "/v6/userfile/preview_doc": ("PubUserFile", "PreviewDoc"),
    "/v6/userfile/list_recent_updated_files": ("PubUserFile", "ListRecentUpdatedFiles"),
    "/v6/user/get_statistics_and_quota": ("PubUser", "GetStatisticsAndQuota"),
    "/v6/user/get_settings": ("PubUser", "GetSettings"),
    "/v6/user/set_settings": ("PubUser", "SetSettings"),
    "/v6/user/get_available_extend_quota": ("PubUser", "GetAvailableExtendQuota"),
    "/v6/oauth/get_token": ("OauthAuthorization", "GetToken"),
    "/v6/offline_task/parse": ("PubOfflineTask", "Parse"),
    "/v6/offline_task/add": ("PubOfflineTask", "Add"),
    "/v6/offline_task/list": ("PubOfflineTask", "List"),
    "/v6/offline_task/delete": ("PubOfflineTask", "Delete"),
    # PubUser account lifecycle
    "/v6/user/get": ("PubUser", "Get"),
    "/v6/user/login": ("PubUser", "Login"),
    "/v6/user/refresh": ("PubUser", "Refresh"),
    "/v6/user/register": ("PubUser", "Register"),
    "/v6/user/logoff": ("PubUser", "Logoff"),
    "/v6/user/delete": ("PubUser", "Delete"),
    "/v6/user/update": ("PubUser", "Update"),
    "/v6/user/change_password": ("PubUser", "ChangePassword"),
    "/v6/user/reset_password": ("PubUser", "ResetPassword"),
    "/v6/user/device_authorization": ("PubUser", "DeviceAuthorization"),
    "/v6/user/sms_verify_code": ("PubUser", "SendSmsVerifyCode"),
    "/v6/user/sms_verify_code_not_user": ("PubUser", "SendSmsVerifyCodeNotUser"),
    "/v6/user/verify_auth_token": ("PubUser", "VerifyAuthToken"),
    "/v6/user/verify_authorization_code": ("PubUser", "VerifyAuthorizationCode"),
    "/v6/user/check_auth_status": ("PubUser", "CheckAuthStatus"),
    "/v6/user/create_auth_token": ("PubUser", "CreateAuthToken"),
    "/v6/user/validate_user_info": ("PubUser", "ValidateUserInfo"),
    "/v6/user/extend_quota": ("PubUser", "ExtendQuota"),
    "/v6/user/user_center_uri": ("PubUser", "UserCenterUri"),
    # PubUserFile batch + RTC
    "/v6/userfile/copy": ("PubUserFile", "Copy"),
    "/v6/userfile/batch_rename": ("PubUserFile", "BatchRename"),
    "/v6/userfile/batch_operation": ("PubUserFile", "BatchOperation"),
    "/v6/userfile/update": ("PubUserFile", "Update"),
    "/v6/userfile/create_doc": ("PubUserFile", "CreateDoc"),
    "/v6/userfile/report_preview_status": ("PubUserFile", "ReportPreviewStatus"),
    "/v6/userfile/create_manage_rtc_offer": ("PubUserFile", "CreateManageRTCOffer"),
    "/v6/userfile/send_client_ice_candidate": ("PubUserFile", "SendClientIceCandidate"),
    "/v6/userfile/get_server_ice_candidate": ("PubUserFile", "GetServerIceCandidate"),
    # OauthAuthorization remaining
    "/v6/oauth/authorize": ("OauthAuthorization", "Authorize"),
    "/v6/oauth/device_code": ("OauthAuthorization", "DeviceCode"),
    "/v6/oauth/get_authorize_state": ("OauthAuthorization", "GetAuthorizeState"),
    "/v6/oauth/get_device_code_state": ("OauthAuthorization", "GetDeviceCodeState"),
    "/v6/oauth/refresh_token": ("OauthAuthorization", "RefreshToken"),
}


# ==================== ROUTER ====================

class GrpcCompatRouter:
    """Mounts the gRPC-Web and REST endpoints on a FastAPI app."""

    def __init__(self, app, engine, auth, presign_secret: str, share=None,
                 offline=None, sftp=None):
        self.engine = engine
        self.auth = auth
        self.share = share
        self.offline = offline
        self.sftp = sftp
        self.presign_secret = presign_secret
        app.api_route("/v6.services.pub.{service}/{method}", methods=["POST"],
                      include_in_schema=False)(self.grpc_endpoint)
        app.api_route("/v6.services.p2p.{service}/{method}", methods=["POST"],
                      include_in_schema=False)(self.grpc_endpoint)
        app.api_route("/v6/{rest:path}", methods=["POST"],
                      include_in_schema=False)(self.rest_endpoint)

    # ---------- context ----------
    def _ctx(self, request: Request, headers: Dict[str, str]) -> Ctx:
        token = _bearer(headers)
        user = self.auth.verify_token(token) if token else None
        base = str(request.base_url).rstrip("/")
        return Ctx(self.engine, self.auth, self.presign_secret, base, user, self.share,
                   offline=self.offline, sftp=self.sftp)

    def _dispatch(self, ctx: Ctx, service: str, method: str, payload: bytes) -> bytes:
        spec = RPCS.get((service, method))
        if not spec:
            raise GrpcError(pb.GRPC_UNIMPLEMENTED, f"method not implemented: {service}/{method}")
        if spec.get("auth", True) and not ctx.user:
            raise GrpcError(pb.GRPC_UNAUTHENTICATED, "invalid accesstoken")
        req = pb.decode(spec["in"], payload) if payload else {}
        try:
            result = spec["fn"](ctx, req)
        except ExtraError as exc:
            raise GrpcError(exc.code, exc.message)
        return pb.encode(spec["out"], result or {})

    # ---------- gRPC-Web ----------
    async def grpc_endpoint(self, request: Request, service: str, method: str):
        ctype = (request.headers.get("content-type") or "").lower()
        is_text = "grpc-web-text" in ctype
        raw = await request.body()
        if is_text:
            try:
                raw = base64.b64decode(raw)
            except Exception:
                return grpc_response(pb.GRPC_INVALID_ARGUMENT, "invalid base64 body", is_text=True)
        payload = unwrap_frame(raw)

        headers = {k.lower(): v for k, v in request.headers.items()}
        token = _bearer(headers)

        ok, reason = verify_sign(service, method, headers, token)
        if not ok:
            return grpc_response(pb.GRPC_INVALID_ARGUMENT, reason, is_text=is_text)

        ctx = self._ctx(request, headers)
        try:
            out = self._dispatch(ctx, service, method, payload)
        except GrpcError as exc:
            return grpc_response(exc.code, exc.message, is_text=is_text)
        except Exception as exc:  # noqa: BLE001 — surface as a gRPC internal error
            return grpc_response(GRPC_INTERNAL, f"{type(exc).__name__}: {exc}", is_text=is_text)
        return grpc_response(pb.GRPC_OK, "", out, is_text=is_text)

    # ---------- REST gateway ----------
    async def rest_endpoint(self, request: Request, rest: str):
        path = "/v6/" + rest
        target = REST_ROUTES.get(path)
        if not target:
            return JSONResponse(status_code=404,
                                content={"code": 5, "message": f"unknown route {path}"})
        service, method = target

        headers = {k.lower(): v for k, v in request.headers.items()}
        token = _bearer(headers)
        ok, reason = verify_sign(service, method, headers, token)
        if not ok:
            return JSONResponse(status_code=400, content={"code": 3, "message": reason})

        try:
            body = await request.json()
        except Exception:
            body = {}
        req = to_snake(body if isinstance(body, dict) else {})

        ctx = self._ctx(request, headers)
        spec = RPCS[(service, method)]
        if spec.get("auth", True) and not ctx.user:
            return JSONResponse(status_code=401, content={"code": 16, "message": "invalid accesstoken"})
        try:
            result = spec["fn"](ctx, req)
        except GrpcError as exc:
            return JSONResponse(status_code=400, content={"code": exc.code, "message": exc.message})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=500,
                                content={"code": GRPC_INTERNAL, "message": str(exc)})
        return JSONResponse(content=to_camel(result or {}))
