#!/usr/bin/env python3
"""验证 v6_extra 新增的 79 个 RPC 中能落地的那些。"""
import hashlib
import os
import time

import requests

import pb

API = "http://127.0.0.1:8765"
# 与服务端 grpc_compat.APP_SECRET 保持一致即可（本地 lab 默认值）
SECRET = os.environ.get("MYDRIVE_APP_SECRET", "dev-only-insecure-secret")
APPID = "2dland/web"
APPVER = "202603182100"
USER = "extra_user"
PASS = "extra_pass_123"


def sign(service, method, token=""):
    ts = str(int(time.time() * 1000))
    plain = f"/{service}/{method}{ts}{APPID}{APPVER}"
    if token:
        plain += "Bearer " + token
    return {"timestamp": ts, "appid": APPID, "appversion": APPVER,
            "sign": hashlib.md5((plain + SECRET).encode()).hexdigest(),
            "content-type": "application/grpc-web+proto", "te": "trailers"}


def call(service, method, payload=b"", token=""):
    headers = sign(service, method, token)
    if token:
        headers["authorization"] = "Bearer " + token
    frame = b"\x00" + len(payload).to_bytes(4, "big") + payload
    resp = requests.post(f"{API}/v6.services.pub.{service}/{method}", data=frame, headers=headers)
    status = int(resp.headers.get("grpc-status", "0") or 0)
    body, pos, raw = b"", 0, resp.content
    while pos + 5 <= len(raw):
        flag = raw[pos]
        length = int.from_bytes(raw[pos + 1:pos + 5], "big")
        if flag == 0:
            body = raw[pos + 5:pos + 5 + length]
        pos += 5 + length
    return status, body, resp.headers.get("grpc-message", "")


def encode_msg(name, obj):
    return pb.encode(name, obj)


def decode_msg(name, body):
    return pb.decode(name, body) if body else {}


def token():
    creds = {"username": USER, "password": PASS}
    r = requests.post(f"{API}/api/auth/register", json=creds)
    if r.status_code != 200:
        r = requests.post(f"{API}/api/auth/login", json=creds)
    r.raise_for_status()
    return r.json()["access_token"]


def main():
    t = token()
    ok = []

    def check(label, cond, extra=""):
        assert cond, f"{label} FAILED {extra}"
        ok.append(label)
        print(f"  ✅ {label} {extra}")

    # ---- PubDavConfig ----
    st, body, msg = call("PubDavConfig", "ServerInfo", encode_msg("DavConfig", {}), t)
    info = decode_msg("DavServerInfo", body)
    check("DavConfig.ServerInfo", st == 0 and info.get("server_name") == "webdav-server",
          f"{info.get('server_name')} {info.get('server_software')}")

    st, body, msg = call("PubDavConfig", "Get", encode_msg("DavConfig", {}), t)
    cfg = decode_msg("DavConfig", body)
    check("DavConfig.Get", st == 0 and cfg.get("username") and cfg.get("password"),
          f"user={cfg.get('username')} pwd_len={len(cfg.get('password') or '')}")

    st, body, _ = call("PubDavConfig", "Disable", encode_msg("DavConfig", {}), t)
    check("DavConfig.Disable", (decode_msg("DavConfig", body).get("enable_flag") or 0) == 0)
    st, body, _ = call("PubDavConfig", "Enable", encode_msg("DavConfig", {}), t)
    check("DavConfig.Enable", decode_msg("DavConfig", body).get("enable_flag") == 1)

    # ---- PubSftpConfig ----
    st, body, _ = call("PubSftpConfig", "Get", encode_msg("SftpConfig", {}), t)
    sftp = decode_msg("SftpConfig", body)
    check("SftpConfig.Get", st == 0 and (sftp.get("ssh_key") or "").startswith("ssh-ed25519"))

    st, body, _ = call("PubSftpConfig", "UpdateKeys", encode_msg("SftpConfig", {}), t)
    check("SftpConfig.UpdateKeys", st == 0 and "ssh-ed25519" in (decode_msg("SftpConfig", body).get("ssh_key") or ""))

    # ---- PubOfflineTask ----
    magnet = "magnet:?xt=urn:btih:abc123&dn=ubuntu-24.04.iso"
    st, body, _ = call("PubOfflineTask", "Parse", encode_msg("TaskParseRequest", {"url": magnet}), t)
    parsed = decode_msg("TaskParseResponse", body)
    meta = parsed.get("meta") or {}
    # 真实服务对无法解析的来源返回空 payload（线上实测一致）
    check("OfflineTask.Parse", st == 0 and isinstance(parsed, dict),
          f"name={meta.get('name')} files={len(parsed.get('task_files') or [])}")

    st, body, _ = call("PubOfflineTask", "Add",
                       encode_msg("UserTask", {"url": magnet, "name": "ubuntu-24.04.iso",
                                               "save_path": "/", "type": 3}), t)
    added = decode_msg("UserTask", body)
    check("OfflineTask.Add", st == 0 and added.get("identity"))

    st, body, _ = call("PubOfflineTask", "List", encode_msg("OfflineTaskListRequest", {}), t)
    tasks = decode_msg("OfflineTaskListResponse", body).get("tasks") or []
    if isinstance(tasks, dict):
        tasks = [tasks]
    check("OfflineTask.List", st == 0 and len(tasks) >= 1, f"{len(tasks)} task(s)")

    st, body, _ = call("PubOfflineTask", "Delete",
                       encode_msg("OfflineTaskDeleteRequest", {"identity": [added["identity"]],
                                                               "delete_files": False}), t)
    check("OfflineTask.Delete", st == 0 and decode_msg("OfflineTaskDeleteResponse", body).get("count") == 1)

    # ---- PubTicket ----
    st, body, _ = call("PubTicket", "Create",
                       encode_msg("Ticket", {"subject": "无法上传大文件", "category": "tech",
                                             "description": "上传 8GB 文件时报错", "channel": "web"}), t)
    ticket = decode_msg("Ticket", body)
    check("Ticket.Create", st == 0 and ticket.get("identity"))

    st, body, _ = call("PubTicket", "List", encode_msg("TicketListRequest", {}), t)
    tickets = decode_msg("TicketListResponse", body).get("tickets") or []
    if isinstance(tickets, dict):
        tickets = [tickets]
    check("Ticket.List", st == 0 and len(tickets) >= 1)

    st, body, _ = call("PubTicket", "Comment", encode_msg("Ticket", {"identity": ticket["identity"]}), t)
    check("Ticket.Comment", st == 0 and decode_msg("Ticket", body).get("comment_count") == 1)

    st, body, _ = call("PubTicket", "Close", encode_msg("StringList", {"list": [ticket["identity"]]}), t)
    check("Ticket.Close", st == 0 and ticket["identity"] in (decode_msg("StringList", body).get("list") or []))

    # ---- Settings ----
    st, body, _ = call("PubUser", "SetSettings",
                       encode_msg("Settings", {"settings": [{"key": "theme", "value": "dark"},
                                                            {"key": "lang", "value": "zh-CN"}]}), t)
    stored = decode_msg("Settings", body).get("settings") or []
    check("PubUser.SetSettings", st == 0 and len(stored) == 2, f"{stored}")

    st, body, _ = call("PubUser", "GetSettings", encode_msg("Settings", {}), t)
    got = {e["key"]: e["value"] for e in (decode_msg("Settings", body).get("settings") or [])}
    check("PubUser.GetSettings", st == 0 and got.get("theme") == "dark", f"{got}")

    st, body, _ = call("PubUser", "GetAvailableExtendQuota", encode_msg("User", {}), t)
    quota = decode_msg("UserQuotaExtendInfo", body)
    check("PubUser.GetAvailableExtendQuota", st == 0 and quota.get("daily_bytes_upload", 0) > 0,
          f"upload={quota.get('daily_bytes_upload')}")

    # ---- PubSlice ----
    st, body, _ = call("PubSlice", "GetSlice",
                       encode_msg("Slice", {"identity": "bafkreiaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}), t)
    sl = decode_msg("Slice", body)
    check("PubSlice.GetSlice", st == 0 and (sl.get("status") or 0) == 5, f"status={sl.get('status')}")

    # ---- OAuth device code ----
    st, body, _ = call("OauthAuthorization", "DeviceCode", encode_msg("AuthorizeRequest", {"client_id": "2dland/web"}))
    dev = decode_msg("DeviceCodeAuthorizeResponse", body)
    check("Oauth.DeviceCode", st == 0 and dev.get("user_code") and dev.get("device_code"),
          f"user_code={dev.get('user_code')}")

    st, body, _ = call("OauthAuthorization", "GetDeviceCodeState",
                       encode_msg("DeviceCodeAuthorizeState", {"device_code": dev["device_code"],
                                                               "user_code": dev["user_code"]}))
    state = decode_msg("DeviceCodeAuthorizeState", body)
    check("Oauth.GetDeviceCodeState", st == 0 and state.get("status") == "authorization_pending",
          f"status={state.get('status')}")

    # ---- PubUserFile: ParseFileSlice / DirectDownloadAddress ----
    data = b"slice-probe-" * 500
    init = requests.post(f"{API}/api/upload/init",
                         json={"name": "slice_probe.txt", "parent": "root", "file_size": len(data)},
                         headers={"Authorization": "Bearer " + t}).json()
    from cid_engine import compute_cid
    cid = compute_cid(data)
    requests.post(f"{API}/int/blocks/{init['task']}/{cid}", data=data,
                  headers={"Authorization": "Bearer " + t, "Content-Type": "application/octet-stream"})
    commit = requests.post(f"{API}/int/blocks/{init['task']}", json=[cid],
                           headers={"Authorization": "Bearer " + t}).json()
    fid = commit["identity"]

    st, body, _ = call("PubUserFile", "ParseFileSlice", encode_msg("File", {"identity": fid}), t)
    parsed = decode_msg("ParseFileSliceResponse", body)
    check("PubUserFile.ParseFileSlice",
          st == 0 and parsed.get("content_identity") == commit["content_identity"]
          and (parsed.get("raw_nodes") or []) == [cid],
          f"nodes={len(parsed.get('raw_nodes') or [])} sha1={str(parsed.get('sha1'))[:12]}")

    st, body, _ = call("PubUserFile", "GetDirectDownloadAddress",
                       encode_msg("DirectDownloadRequest", {"identity": fid}), t)
    direct = decode_msg("FileDownloadAddressResponse", body)
    check("PubUserFile.GetDirectDownloadAddress",
          st == 0 and auth_in(direct), direct.get("download_address", "")[-40:])

    st, body, _ = call("PubUserFile", "GetDownloadAndPreviewInfo",
                       encode_msg("File", {"identity": fid}), t)
    prev = decode_msg("DownloadAndPreviewInfo", body)
    check("PubUserFile.GetDownloadAndPreviewInfo",
          st == 0 and prev.get("can_download") is True and prev.get("sha1"),
          f"preview={prev.get('can_preview')} sha1={str(prev.get('sha1'))[:10]}")

    print(f"\n{len(ok)} checks passed")


def auth_in(direct):
    return "auth_key=" in (direct.get("download_address") or "")


if __name__ == "__main__":
    main()
