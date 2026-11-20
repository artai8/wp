"""
Minimal protobuf codec
======================
A schema-driven encoder/decoder for the wire format subset used by the v6
gRPC API: varints, length-delimited scalars and nested messages.

It is intentionally tiny — no .proto parsing, no code generation. Schemas are
declared as plain dicts so the gRPC compatibility layer can speak the exact
field numbers observed on the live service.
"""

import struct
from typing import Any, Dict, Tuple

# wire types
VARINT, FIXED64, LEN, FIXED32 = 0, 1, 2, 5

_SCALARS = {
    "int32": VARINT, "int64": VARINT, "uint32": VARINT, "uint64": VARINT,
    "bool": VARINT, "enum": VARINT,
    "string": LEN, "bytes": LEN,
    "fixed64": FIXED64, "double": FIXED64,
    "fixed32": FIXED32, "float": FIXED32,
}

# name -> {field_no: (type, field_name)}
# type is a scalar name, a message name, or "repeated <type>"
MESSAGES: Dict[str, Dict[int, Tuple[str, str]]] = {}


def message(name: str, fields: Dict[int, Tuple[str, str]]):
    """Register a schema: ``message("File", {1: ("string", "identity"), ...})``"""
    MESSAGES[name] = fields
    return name


def _wire_type(type_name: str) -> int:
    base = type_name.split(" ", 1)[1] if type_name.startswith("repeated ") else type_name
    if base in _SCALARS:
        return _SCALARS[base]
    if base in MESSAGES:
        return LEN
    raise KeyError(f"unknown field type: {type_name}")


# ==================== VARINT ====================

def write_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


# ==================== ENCODE ====================

def _encode_scalar(type_name: str, value: Any) -> bytes:
    if type_name in ("int32", "int64", "uint32", "uint64", "enum"):
        return write_varint(int(value))
    if type_name == "bool":
        return write_varint(1 if value else 0)
    if type_name == "string":
        raw = str(value).encode("utf-8")
        return write_varint(len(raw)) + raw
    if type_name == "bytes":
        raw = bytes(value)
        return write_varint(len(raw)) + raw
    if type_name == "double":
        return struct.pack("<d", float(value))
    if type_name == "float":
        return struct.pack("<f", float(value))
    if type_name == "fixed64":
        return struct.pack("<Q", int(value) & 0xFFFFFFFFFFFFFFFF)
    if type_name == "fixed32":
        return struct.pack("<I", int(value) & 0xFFFFFFFF)
    raise KeyError(f"cannot encode scalar {type_name}")


def _encode_value(type_name: str, value: Any) -> bytes:
    if type_name in _SCALARS:
        return _encode_scalar(type_name, value)
    # nested message (dict) — LEN fields carry their own length prefix
    raw = encode(type_name, value or {})
    return write_varint(len(raw)) + raw


def encode(schema: str, obj: Dict[str, Any]) -> bytes:
    """Encode a dict into protobuf bytes using the registered schema."""
    fields = MESSAGES[schema]
    by_name = {name: (no, t) for no, (t, name) in fields.items()}
    out = bytearray()

    for key, value in (obj or {}).items():
        if value is None:
            continue
        if isinstance(key, int):
            if key not in fields:
                continue
            type_name, field_name = fields[key]
            field_no = key
        else:
            if key not in by_name:
                continue
            field_no, type_name = by_name[key]

        repeated = type_name.startswith("repeated ")
        base_type = type_name.split(" ", 1)[1] if repeated else type_name
        values = value if (repeated and isinstance(value, (list, tuple))) else [value]

        for item in values:
            if item is None:
                continue
            # proto3 omits default scalars
            if not repeated and base_type in ("int32", "int64", "uint32", "uint64", "enum") and int(item) == 0:
                continue
            if not repeated and base_type == "bool" and not item:
                continue
            if not repeated and base_type in ("string", "bytes") and len(item) == 0:
                continue
            wt = _wire_type(type_name)
            out += write_varint((field_no << 3) | wt)
            out += _encode_value(base_type, item)

    return bytes(out)


# ==================== DECODE ====================

def _decode_scalar(type_name: str, buf: bytes, pos: int, wt: int) -> Tuple[Any, int]:
    if wt == VARINT:
        raw, pos = read_varint(buf, pos)
        if type_name == "bool":
            return bool(raw), pos
        # int32 / int64 负数在 wire 上是 64 位补码
        if type_name in ("int32", "int64", "enum") and raw >= 1 << 63:
            raw -= 1 << 64
        return raw, pos
    if wt == LEN:
        length, pos = read_varint(buf, pos)
        chunk = buf[pos:pos + length]
        pos += length
        if type_name == "bytes":
            return chunk, pos
        try:
            return chunk.decode("utf-8"), pos
        except UnicodeDecodeError:
            return chunk, pos
    if wt == FIXED64:
        return struct.unpack("<d", buf[pos:pos + 8])[0], pos + 8
    if wt == FIXED32:
        return struct.unpack("<f", buf[pos:pos + 4])[0], pos + 4
    raise ValueError(f"unsupported wire type {wt}")


def _skip(buf: bytes, pos: int, wt: int) -> int:
    if wt == VARINT:
        _, pos = read_varint(buf, pos)
        return pos
    if wt == LEN:
        length, pos = read_varint(buf, pos)
        return pos + length
    if wt == FIXED64:
        return pos + 8
    if wt == FIXED32:
        return pos + 4
    raise ValueError(f"cannot skip wire type {wt}")


def decode(schema: str, data: bytes) -> Dict[str, Any]:
    """Decode protobuf bytes into a dict keyed by field name."""
    fields = MESSAGES.get(schema, {})
    result: Dict[str, Any] = {}
    pos = 0
    while pos < len(data):
        key, pos = read_varint(data, pos)
        field_no, wt = key >> 3, key & 0x07
        spec = fields.get(field_no)
        if spec is None:
            pos = _skip(data, pos, wt)
            continue
        type_name, field_name = spec
        repeated = type_name.startswith("repeated ")
        base_type = type_name.split(" ", 1)[1] if repeated else type_name
        if base_type in _SCALARS:
            value, pos = _decode_scalar(base_type, data, pos, wt)
        else:
            length, pos = read_varint(data, pos)
            value = decode(base_type, data[pos:pos + length])
            pos += length

        if repeated:
            result.setdefault(field_name, []).append(value)
        else:
            result[field_name] = value
    return result


def decode_partial(schema: str, data: bytes) -> Dict[str, Any]:
    """Best-effort decode that never raises — used for logging unknown RPCs."""
    try:
        return decode(schema, data)
    except Exception:
        return {}


# ==================== v6 SCHEMAS ====================
# Field numbers transcribed from downloads/halalcloud_apk/protos/

message("OrderByInfo", {
    1: ("string", "field"),
    2: ("bool", "asc"),
})

message("ScanListRequest", {
    1: ("string", "token"),
    2: ("int64", "limit"),
    3: ("repeated OrderByInfo", "order_by"),
    4: ("int32", "version"),
})

message("File", {
    1: ("string", "identity"),
    3: ("string", "parent"),
    4: ("string", "name"),
    5: ("string", "path"),
    6: ("string", "mime_type"),
    7: ("int64", "size"),
    8: ("int64", "type"),
    9: ("int64", "create_ts"),
    10: ("int64", "update_ts"),
    11: ("int64", "delete_ts"),
    12: ("bool", "deleted"),
    13: ("bool", "dir"),
    14: ("bool", "hidden"),
    15: ("bool", "locked"),
    16: ("bool", "shared"),
    17: ("bool", "starred"),
    18: ("bool", "trashed"),
    19: ("int64", "locked_at"),
    20: ("string", "locked_by"),
    21: ("int64", "shared_at"),
    22: ("int64", "flag"),
    23: ("string", "unique"),
    24: ("string", "content_identity"),
    25: ("int64", "label"),
    26: ("int64", "store_type"),
    27: ("int64", "version"),
    28: ("int64", "files"),
    29: ("int64", "directories"),
    30: ("int64", "nodes"),
    31: ("string", "sort_name"),
})

message("FileListRequest", {
    1: ("File", "parent"),
    2: ("File", "filter"),
    3: ("ScanListRequest", "list_info"),
})

message("FileListResponse", {
    1: ("repeated File", "files"),
    2: ("ScanListRequest", "list_info"),
})

message("BatchOperationRequest", {
    1: ("repeated File", "source"),
    2: ("File", "dest"),
    3: ("int32", "operation"),
    4: ("int32", "flag"),
})

message("BatchOperationResponse", {
    1: ("string", "task"),
    2: ("int32", "status"),
    3: ("int64", "affected"),
    4: ("int64", "create_ts"),
    5: ("int64", "progress"),
    6: ("int64", "total"),
})

message("SearchRequest", {
    1: ("string", "name"),
    3: ("ScanListRequest", "list_info"),
})

message("User", {
    1: ("string", "identity"),
    2: ("int32", "type"),
    3: ("int32", "status"),
    5: ("int64", "update_ts"),
    6: ("string", "password"),
    7: ("string", "name"),
    8: ("string", "addon"),
    9: ("int64", "create_ts"),
    10: ("string", "hash"),
    11: ("string", "icon"),
})

message("Token", {
    1: ("string", "identity"),
    2: ("string", "user_identity"),
    3: ("string", "device"),
    4: ("int32", "version"),
    5: ("string", "access_token"),
    6: ("string", "refresh_token"),
    7: ("int64", "update_ts"),
    8: ("int64", "access_token_expire_ts"),
    9: ("int64", "refresh_token_expire_ts"),
})

message("LoginRequest", {
    1: ("string", "identity"),
    2: ("string", "password"),
    3: ("string", "addon"),
    4: ("string", "input"),
    5: ("string", "type"),
    6: ("string", "callback"),
    7: ("string", "return_url"),
    8: ("int32", "return_type"),
    9: ("string", "captcha"),
    10: ("string", "state"),
    11: ("string", "country_code"),
    12: ("string", "code_challenge"),
    13: ("string", "code_challenge_method"),
    14: ("string", "device"),
    15: ("string", "nonce"),
    16: ("string", "scope"),
    17: ("int32", "version"),
})

message("LoginResponse", {
    1: ("Token", "token"),
    2: ("User", "user"),
    3: ("LastLoginResponse", "last_login"),
    4: ("string", "state"),
    5: ("string", "code"),
})

message("LastLoginResponse", {
    1: ("string", "identity"),
    2: ("int64", "last_login_ts"),
    3: ("string", "last_login_ip"),
    4: ("string", "last_login_device"),
})

message("DiskStatisticsAndQuota", {
    1: ("int64", "bytes_quota"),
    2: ("int64", "bytes_used"),
    3: ("int64", "bytes_free"),
    4: ("int64", "files_quota"),
    5: ("int64", "files_used"),
    6: ("int64", "bytes_trash_used"),
})

message("TrafficStatisticsAndQuota", {
    1: ("int64", "daily_bytes_download_quota"),
    2: ("int64", "daily_bytes_upload_quota"),
    3: ("int64", "bytes_downloaded_today"),
    4: ("int64", "bytes_uploaded_today"),
})

message("OfflineTaskStatisticsAndQuota", {
    1: ("int64", "daily_tasks_quota"),
    2: ("int64", "tasks_commited_today"),
    3: ("int64", "daily_bytes_quota"),
    4: ("int64", "bytes_commited_today"),
})

message("UserStatisticsAndQuota", {
    1: ("DiskStatisticsAndQuota", "disk_statistics_quota"),
    2: ("TrafficStatisticsAndQuota", "traffic_statistics_quota"),
    3: ("OfflineTaskStatisticsAndQuota", "offline_task_statistics_quota"),
})

message("SettingEntry", {
    1: ("string", "key"),
    2: ("string", "value"),
})

# proto: map<string, string> settings = 1  ->  repeated entry on the wire
message("Settings", {
    1: ("repeated SettingEntry", "settings"),
})

message("UploadTask", {
    1: ("bool", "created"),
    2: ("string", "upload_address"),
    3: ("string", "task"),
    4: ("string", "upload_token"),
    5: ("string", "upload_key"),
    10: ("string", "access_key"),
    11: ("string", "secret_key"),
    12: ("string", "bucket"),
    13: ("string", "region"),
    14: ("string", "endpoint"),
    15: ("string", "key"),
    16: ("int64", "block_size"),
    17: ("int64", "block_hash_type"),
    18: ("int32", "block_version"),
    19: ("int64", "block_codec"),
})

message("SliceDownloadInfo", {
    1: ("string", "identity"),
    2: ("string", "download_address"),
    3: ("string", "download_token"),
    4: ("int32", "encrypt"),
    5: ("int64", "store_type"),
})

message("SlicesDownloadInfoRequest", {
    1: ("File", "file"),
    2: ("ScanListRequest", "list_info"),
})

message("SlicesDownloadInfoResponse", {
    1: ("repeated SliceDownloadInfo", "slices"),
    2: ("ScanListRequest", "list_info"),
    3: ("File", "file"),
})

message("FastLookup", {
    1: ("string", "identity"),
    2: ("string", "wcs_etag"),
    3: ("string", "sha1"),
    4: ("string", "head_sha1"),
    5: ("int64", "file_size"),
    6: ("string", "special_sha1"),
    7: ("string", "name"),
    8: ("string", "lh_hash"),
})

message("FastLookupRequest", {
    1: ("int32", "type"),
    2: ("FastLookup", "info"),
})

# ---- PubFileShare ----

message("FileShare", {
    1: ("string", "identity"),
    2: ("string", "user_identity"),
    3: ("int32", "type"),
    4: ("string", "mime_type"),
    5: ("string", "name"),
    6: ("repeated string", "path_list"),
    7: ("bool", "has_password"),
    8: ("string", "password"),
    9: ("int64", "lifetime"),
    10: ("int64", "save_limit"),
    11: ("int64", "save_count"),
    12: ("int64", "like"),
    13: ("int64", "dislike"),
    14: ("int64", "create_ts"),
    15: ("int64", "update_ts"),
    16: ("int64", "expire_ts"),
    17: ("int64", "file_size"),
    18: ("string", "save_path"),
})

message("FileShareListRequest", {
    1: ("string", "user_identity"),
    3: ("ScanListRequest", "list_info"),
})

message("FileShareListResponse", {
    1: ("repeated FileShare", "shares"),
    2: ("ScanListRequest", "list_info"),
})

message("FileShareDeleteRequest", {
    1: ("repeated string", "identity"),
})

message("FileShareDeleteResponse", {
    1: ("int64", "count"),
})

message("Complaint", {
    1: ("string", "identity"),
    2: ("string", "user_identity"),
    3: ("string", "file_share_identity"),
    4: ("string", "file_share_user_identity"),
    5: ("string", "content"),
    6: ("int64", "create_ts"),
    7: ("int64", "update_ts"),
    8: ("int32", "status"),
    9: ("int32", "flag"),
})

message("ComplaintListRequest", {
    3: ("ScanListRequest", "list_info"),
})

message("ComplaintListResponse", {
    1: ("repeated Complaint", "complaints"),
    2: ("ScanListRequest", "list_info"),
})

# ---- PubUserFile extras (upload / preview / slice addresses) ----

message("UploadToken", {
    1: ("string", "token"),
    10: ("string", "access_key"),
    11: ("string", "secret_key"),
    12: ("string", "bucket"),
    13: ("string", "region"),
    14: ("string", "endpoint"),
    15: ("string", "key"),
})

message("DocFilePreview", {
    1: ("string", "identity"),
    3: ("string", "parent"),
    4: ("string", "name"),
    5: ("string", "path"),
    6: ("string", "mime_type"),
    7: ("int64", "size"),
    8: ("int64", "type"),
    9: ("bool", "can_preview"),
    10: ("string", "preview_address"),
    11: ("string", "preview_token"),
    12: ("bool", "can_edit"),
    13: ("string", "edit_address"),
    14: ("string", "edit_token"),
})

message("DownloadAndPreviewInfo", {
    1: ("string", "identity"),
    3: ("string", "parent"),
    4: ("string", "name"),
    5: ("string", "path"),
    6: ("string", "mime_type"),
    7: ("int64", "size"),
    8: ("int64", "type"),
    9: ("bool", "can_preview"),
    10: ("bool", "can_edit"),
    11: ("int32", "preview_type"),
    12: ("int32", "download_type"),
    13: ("string", "sha1"),
    14: ("bool", "can_download"),
    15: ("string", "cannot_download_message"),
    16: ("string", "content_identity"),
})

message("PreviewStatus", {
    1: ("string", "identity"),
    2: ("string", "code"),
    3: ("string", "message"),
    4: ("string", "path"),
    5: ("string", "status"),
    6: ("int32", "type"),
    7: ("int32", "num"),
    8: ("int64", "report_ts"),
    9: ("string", "video_codec"),
    10: ("string", "audio_codec"),
    11: ("int32", "width"),
    12: ("int32", "height"),
    13: ("float", "duration"),
    14: ("int64", "size"),
    15: ("string", "extra"),
    16: ("string", "user_agent"),
    17: ("string", "client_id"),
    18: ("string", "content_identity"),
    19: ("string", "extra_code"),
    20: ("string", "extra_message"),
    21: ("string", "name"),
    22: ("int32", "playback_type"),
    23: ("int32", "play_mode"),
    24: ("float", "current_pos"),
})

message("SliceSize", {
    1: ("int64", "start_index"),
    2: ("int64", "end_index"),
    3: ("int64", "size"),
})

message("ParseFileSliceResponse", {
    1: ("string", "content_identity"),
    2: ("repeated string", "meta_nodes"),
    3: ("repeated string", "raw_nodes"),
    4: ("int64", "file_size"),
    5: ("repeated SliceSize", "sizes"),
    6: ("string", "sha1"),
    7: ("string", "wcs_etag"),
    8: ("string", "name"),
    9: ("string", "path"),
    10: ("int64", "store_type"),
    11: ("repeated SliceDownloadInfo", "raw_download_addresses"),
})

message("SliceDownloadAddressRequest", {
    1: ("repeated string", "identity"),
    2: ("int32", "version"),
    3: ("string", "filename"),
    4: ("int64", "flag"),
    5: ("bool", "parse"),
})

message("SliceDownloadAddressResponse", {
    1: ("repeated SliceDownloadInfo", "addresses"),
    2: ("int64", "expire_at"),
    3: ("string", "request_address"),
    4: ("int32", "version"),
})

message("FileDownloadAddressResponse", {
    1: ("string", "download_address"),
    2: ("int64", "expire_at"),
    3: ("string", "request_address"),
    4: ("int32", "version"),
    5: ("int32", "encrypt"),
    6: ("int64", "store_type"),
    7: ("int64", "file_size"),
    8: ("string", "path"),
    9: ("string", "name"),
    10: ("string", "content_identity"),
})

message("DirectDownloadRequest", {
    1: ("string", "identity"),
    2: ("int32", "version"),
    3: ("string", "path"),
    4: ("int64", "flag"),
    5: ("string", "client_ip"),
    6: ("string", "client_ua"),
    7: ("repeated string", "video_codecs"),
    8: ("repeated string", "audio_codecs"),
    9: ("int64", "expire"),
    10: ("int32", "encrypt"),
    11: ("string", "addon"),
})

message("ListRecentUpdatedFilesRequest", {
    1: ("File", "parent"),
    2: ("int64", "start_ts"),
    3: ("ScanListRequest", "list_info"),
    4: ("bool", "contain_subdir"),
    5: ("bool", "contain_self"),
})

# ---- PubSlice ----

message("Slice", {
    1: ("string", "identity"),
    2: ("int32", "type"),
    3: ("int32", "status"),
    5: ("int64", "update_ts"),
    6: ("string", "peer"),
    7: ("int64", "create_ts"),
    8: ("string", "server"),
    9: ("bool", "need_data"),
    10: ("bytes", "data"),
})

message("Meta", {
    1: ("string", "identity"),
    2: ("bytes", "data"),
})

# ---- PubDavConfig ----

message("DavConfig", {
    1: ("string", "user_identity"),
    2: ("int32", "enable_flag"),
    3: ("string", "root"),
    4: ("int32", "write_flag"),
    5: ("int32", "lock_flag"),
    6: ("int32", "chunked_flag"),
    7: ("int32", "path_style"),
    8: ("int32", "status"),
    9: ("string", "prefix"),
    10: ("int32", "code"),
    11: ("string", "message"),
    12: ("string", "username"),
    13: ("string", "password"),
    14: ("int64", "update_ts"),
    15: ("string", "remark"),
    16: ("int64", "last_access_ts"),
    17: ("string", "identity"),
})

message("DavConfigListRequest", {
    1: ("ScanListRequest", "list_info"),
    2: ("string", "user_identity"),
})

message("DavConfigListResponse", {
    1: ("ScanListRequest", "list_info"),
    2: ("repeated DavConfig", "configs"),
})

message("DavServerInfo", {
    1: ("string", "server_name"),
    2: ("string", "server_version"),
    4: ("string", "server_software"),
    5: ("string", "server_address"),
    6: ("string", "server_port"),
    7: ("string", "server_protocol"),
    8: ("string", "server_root"),
    9: ("bool", "server_ssl"),
    10: ("bool", "server_allow_write"),
    11: ("int64", "max_users"),
})

# ---- PubSftpConfig ----

message("SftpConfig", {
    1: ("string", "user_identity"),
    2: ("int32", "enable_flag"),
    3: ("string", "root"),
    4: ("int32", "write_flag"),
    5: ("int32", "lock_flag"),
    6: ("int32", "chunked_flag"),
    7: ("int32", "path_style"),
    8: ("int32", "status"),
    9: ("string", "prefix"),
    10: ("int32", "code"),
    11: ("string", "message"),
    12: ("string", "username"),
    13: ("string", "password"),
    14: ("int64", "update_ts"),
    15: ("string", "ssh_key"),
})

# ---- PubOfflineTask ----

message("TaskParseRequest", {
    1: ("string", "url"),
    2: ("string", "file"),
    3: ("string", "identity"),
    4: ("string", "addon"),
    5: ("int32", "flag"),
    6: ("string", "from"),
})

message("TaskMeta", {
    1: ("string", "identity"),
    2: ("int32", "type"),
    3: ("int32", "status"),
    4: ("bool", "locked"),
    5: ("int64", "update_ts"),
    6: ("string", "file"),
    7: ("int64", "create_ts"),
    8: ("string", "url"),
    9: ("int64", "size"),
    10: ("string", "name"),
    12: ("int32", "code"),
    13: ("string", "message"),
    14: ("string", "addon"),
    15: ("int64", "retries"),
    16: ("int64", "progress"),
    17: ("int64", "bytes_total"),
    18: ("int64", "bytes_processed"),
    19: ("int32", "flag"),
})

message("TaskFile", {
    1: ("string", "identity"),
    4: ("string", "path"),
    5: ("int32", "status"),
    6: ("int64", "update_ts"),
    7: ("int64", "create_ts"),
    8: ("string", "name"),
    9: ("int64", "size"),
    13: ("int64", "bytes_total"),
    15: ("int32", "index"),
    16: ("bool", "directory"),
})

message("TaskParseResponse", {
    1: ("TaskMeta", "meta"),
    2: ("repeated TaskFile", "task_files"),
})

message("UserTask", {
    1: ("string", "identity"),
    2: ("int32", "type"),
    3: ("int32", "status"),
    5: ("int64", "update_ts"),
    6: ("string", "file"),
    7: ("int64", "create_ts"),
    8: ("string", "url"),
    9: ("int64", "size"),
    10: ("string", "name"),
    11: ("string", "task_identity"),
    12: ("int32", "code"),
    13: ("string", "message"),
    14: ("string", "addon"),
    16: ("int64", "progress"),
    17: ("int64", "bytes_total"),
    18: ("int64", "bytes_processed"),
    19: ("int32", "flag"),
    20: ("string", "save_path"),
    21: ("repeated string", "callbacks"),
    22: ("repeated string", "ignore_files"),
    23: ("string", "save_identity"),
})

message("OfflineTaskListRequest", {
    3: ("ScanListRequest", "list_info"),
})

message("OfflineTaskListResponse", {
    1: ("repeated UserTask", "tasks"),
    2: ("ScanListRequest", "list_info"),
})

message("OfflineTaskDeleteRequest", {
    1: ("repeated string", "identity"),
    2: ("bool", "delete_files"),
})

message("OfflineTaskDeleteResponse", {
    1: ("int64", "count"),
})

# ---- PubTicket ----

message("Ticket", {
    1: ("string", "identity"),
    2: ("string", "email"),
    3: ("string", "phone"),
    4: ("string", "subject"),
    5: ("string", "category"),
    6: ("string", "sub_category"),
    7: ("string", "channel"),
    8: ("string", "description"),
    9: ("int64", "create_ts"),
    10: ("repeated string", "attachments"),
    11: ("string", "entity_type"),
    12: ("string", "content_type"),
    13: ("string", "status"),
    14: ("bool", "read"),
    15: ("int64", "comment_count"),
    16: ("int64", "thread_count"),
})

message("TicketListRequest", {
    3: ("ScanListRequest", "list_info"),
})

message("TicketListResponse", {
    1: ("repeated Ticket", "tickets"),
    2: ("ScanListRequest", "list_info"),
})

message("ContactInfo", {
    1: ("string", "first_name"),
    2: ("string", "last_name"),
    3: ("string", "email"),
    4: ("string", "phone"),
})

message("Conversation", {
    1: ("bool", "is_description_thread"),
    2: ("bool", "can_reply"),
    3: ("string", "bcc"),
    4: ("string", "channel"),
    5: ("string", "type"),
    6: ("string", "last_rating_icon_url"),
    7: ("string", "impersonated_user"),
    8: ("string", "identity"),
    9: ("string", "content_type"),
    10: ("string", "direction"),
    11: ("string", "summary"),
    12: ("string", "cc"),
    13: ("string", "key_words"),
    14: ("string", "visibility"),
    15: ("string", "content"),
    16: ("string", "encoded_content"),
    17: ("bool", "is_forward"),
    18: ("bool", "has_attach"),
    19: ("string", "attachment_count"),
    20: ("string", "to"),
    21: ("string", "from_email_address"),
    22: ("string", "status"),
    23: ("int64", "create_ts"),
    24: ("bool", "user_thread"),
    25: ("string", "clean_content"),
})

message("ConversationListRequest", {
    1: ("string", "ticket_identity"),
    2: ("ScanListRequest", "list_info"),
})

message("ConversationListResponse", {
    1: ("repeated Conversation", "conversations"),
    2: ("ScanListRequest", "list_info"),
})

# ---- OAuth ----

message("AuthorizeRequest", {
    1: ("string", "client_id"),
    2: ("string", "response_type"),
    3: ("string", "redirect_uri"),
    4: ("string", "scope"),
    5: ("string", "state"),
    6: ("string", "code_challenge"),
    7: ("string", "code_challenge_method"),
    8: ("string", "version"),
    9: ("string", "nonce"),
    10: ("string", "device"),
    11: ("string", "mode"),
    12: ("string", "debug_host"),
    13: ("string", "grant_type"),
    14: ("bool", "legacy"),
})

message("AuthorizeResponse", {
    1: ("string", "code"),
    2: ("string", "redirect_uri"),
    3: ("string", "state"),
    4: ("string", "status"),
    5: ("string", "user_code"),
    6: ("string", "device_code"),
    7: ("int32", "expires_in"),
    8: ("string", "verification_uri"),
    10: ("string", "interval"),
    11: ("int64", "expires_in_ts"),
})

message("DeviceCodeAuthorizeResponse", {
    1: ("string", "user_code"),
    2: ("string", "device_code"),
    3: ("int32", "expires_in"),
    4: ("string", "verification_uri"),
    6: ("int32", "interval"),
    7: ("int64", "expires_in_ts"),
})

message("AuthorizeState", {
    1: ("string", "code"),
    2: ("string", "state"),
    3: ("string", "status"),
    4: ("string", "redirect_uri"),
    5: ("string", "scope"),
    6: ("string", "response_type"),
    7: ("string", "version"),
    8: ("string", "code_challenge"),
    9: ("string", "code_challenge_method"),
    10: ("string", "client_id"),
    11: ("string", "device"),
})

message("DeviceCodeAuthorizeState", {
    1: ("string", "user_code"),
    2: ("string", "device_code"),
    3: ("int32", "expires_in"),
    4: ("string", "scope"),
    5: ("bool", "login"),
    6: ("int32", "interval"),
    7: ("string", "status"),
    8: ("string", "state"),
    9: ("int64", "interval_ts"),
    10: ("int64", "expires_in_ts"),
    11: ("string", "access_token"),
    12: ("string", "refresh_token"),
})

message("TokenRequest", {
    3: ("string", "code"),
    4: ("string", "redirect_uri"),
    5: ("string", "grant_type"),
    6: ("string", "code_verifier"),
})

message("TokenResponse", {
    1: ("string", "access_token"),
    2: ("string", "refresh_token"),
    3: ("int32", "expires_in"),
    4: ("string", "token_type"),
    5: ("string", "scope"),
    6: ("int64", "expires_in_ts"),
})

message("RefreshTokenRequest", {
    1: ("string", "client_id"),
    3: ("string", "refresh_token"),
    4: ("string", "grant_type"),
})

# ---- PubUser extras ----

message("UserCenterUriRequest", {
    1: ("bool", "disable_ssl"),
    2: ("string", "route"),
})

message("UserCenterUriResponse", {
    1: ("string", "uri"),
})

message("UserQuotaExtendInfo", {
    1: ("int64", "daily_bytes_download"),
    2: ("int64", "daily_bytes_upload"),
    3: ("int64", "daily_offline_tasks"),
    4: ("int64", "daily_offline_tasks_bytes"),
    5: ("int64", "disk_bytes"),
    6: ("int64", "disk_files"),
})

message("UserNameValidateResponse", {
    1: ("int32", "code"),
    2: ("string", "message"),
    3: ("bool", "valid"),
})

message("ChangePasswordRequest", {
    1: ("string", "identity"),
    2: ("string", "old_password"),
    3: ("string", "new_password"),
    4: ("string", "addon"),
})

message("RegisterRequest", {
    1: ("string", "name"),
    2: ("string", "password"),
    5: ("string", "email"),
    6: ("string", "sms_captcha"),
    3: ("string", "captcha"),
})

message("DeviceAuthorizationRequest", {
    1: ("string", "identity"),
    2: ("string", "device"),
    3: ("int32", "type"),
})

message("OauthTokenResponse", {
    1: ("string", "url"),
    2: ("string", "addon"),
    3: ("string", "input"),
    4: ("string", "type"),
    5: ("string", "callback"),
    6: ("string", "return_url"),
    7: ("int32", "return_type"),
    8: ("string", "captcha"),
    9: ("string", "state"),
})

message("OauthTokenCheckResponse", {
    1: ("LoginResponse", "login"),
    2: ("OauthTokenResponse", "oauth"),
    3: ("int32", "status"),
    4: ("string", "message"),
    5: ("string", "code"),
    6: ("string", "state"),
})

message("AuthorizationCode", {
    1: ("string", "identity"),
    2: ("string", "code"),
    3: ("string", "addon"),
    4: ("string", "state"),
    5: ("string", "code_verifier"),
    6: ("int32", "version"),
})

message("UserValidateInfo", {
    1: ("string", "identity"),
    2: ("string", "name"),
    3: ("string", "email"),
    4: ("string", "phone"),
    5: ("string", "country_code"),
    6: ("string", "addon"),
})

message("SmsVeifyCodeSendRequest", {
    1: ("string", "captcha"),
    2: ("int32", "type"),
    3: ("string", "addon"),
})

message("SmsVeifyCodeSendRequestNotUser", {
    1: ("string", "captcha"),
    2: ("int32", "type"),
    3: ("string", "addon"),
    4: ("string", "country_code"),
    5: ("string", "phone"),
})

message("SmsVeifyCodeSendResponse", {
    1: ("string", "captcha"),
    2: ("int32", "type"),
    3: ("string", "addon"),
    4: ("int64", "expire_ts"),
})

message("StringList", {
    1: ("repeated string", "list"),
})

# ---- PubFileShare: complaint management (share_file_share.proto) ----

message("ComplaintDeleteRequest", {
    1: ("repeated string", "identity"),
})

message("ComplaintDeleteResponse", {
    1: ("int64", "count"),
})

# ---- PubUserFile: WebRTC data-channel signalling (userfile_public_user_file.proto) ----

message("ManageRTCRequest", {
    1: ("string", "sdp"),
    2: ("string", "offer"),
    3: ("string", "client_identity"),
})

message("ManageRTCResponse", {
    1: ("int64", "file_size"),
    2: ("string", "encode_key"),
    3: ("string", "sdp"),
    4: ("string", "name"),
    5: ("string", "connection"),
})

message("SendIceCandidateRequest", {
    1: ("string", "client_identity"),
    2: ("string", "candidate"),
})

message("SendIceCandidateResponse", {
    1: ("string", "client_identity"),
    2: ("string", "candidate"),
})

message("GetIceCandidateRequest", {
    1: ("string", "client_identity"),
    2: ("string", "candidate"),
})

message("GetIceCandidateResponse", {
    1: ("string", "client_identity"),
    2: ("repeated string", "candidate"),
})

# ---- v6.services.p2p.WebRTCManageService (wrtc_webrtc_manager.proto) ----

message("StartDownloadRequest", {
    1: ("string", "client_identity"),
    2: ("string", "content_identity"),
    3: ("int64", "peer_limit"),
})

message("RtcPeerInfo", {
    1: ("string", "client_identity"),
    2: ("string", "content_identity"),
    3: ("string", "sdp"),
})

message("StartDownloadResponse", {
    1: ("string", "content_identity"),
    2: ("repeated RtcPeerInfo", "peers"),
    3: ("repeated RtcPeerInfo", "backend_peers"),
})

message("StopDownloadRequest", {
    1: ("string", "client_identity"),
    2: ("string", "content_identity"),
})

message("StopDownloadResponse", {
    1: ("string", "content_identity"),
})

message("DisconnectRequest", {
    1: ("string", "client_identity"),
})

message("DisconnectResponse", {
    1: ("string", "client_identity"),
})

message("SendPeerIceCandidateRequest", {
    1: ("string", "client_identity"),
    3: ("string", "candidate"),
})

message("SendPeerIceCandidateResponse", {
    1: ("string", "client_identity"),
})

message("ReceiveRequest", {
    1: ("string", "id"),
})

message("ReceiveResponse", {
    1: ("string", "data"),
})

# gRPC status codes
GRPC_OK = 0
GRPC_INVALID_ARGUMENT = 3
GRPC_NOT_FOUND = 5
GRPC_PERMISSION_DENIED = 7
GRPC_UNAUTHENTICATED = 16
GRPC_UNIMPLEMENTED = 12
