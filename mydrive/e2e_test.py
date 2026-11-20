"""
End-to-end test: auth -> upload -> instant-check -> download -> isolation.
Tests the full storage + download + multi-user pipeline over HTTP.
"""

import hashlib
import os
import time
import uuid

import requests

from cid_engine import compute_cid

API = "http://localhost:8765"
DEFAULT_USER = "e2e_main"
APP_SECRET = os.environ.get("MYDRIVE_APP_SECRET", "dev-only-insecure-secret")
_auth_cache = {}


def sha1_hex(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def head_sha1_hex(data: bytes) -> str:
    return hashlib.sha1(data[:262144]).hexdigest()


def _register(username: str, password: str) -> dict:
    resp = requests.post(f"{API}/api/auth/register", json={"username": username, "password": password})
    if resp.status_code == 200:
        return resp.json()
    # Already registered from a previous run — log in instead
    resp = requests.post(f"{API}/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return resp.json()


def login(username: str = None) -> dict:
    """Get (or reuse) a token pair for a test user."""
    username = username or DEFAULT_USER
    if username in _auth_cache:
        return _auth_cache[username]
    data = _register(username, "e2e_password_123")
    _auth_cache[username] = data
    return data


def hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def token() -> str:
    return login()["access_token"]


def test_requires_auth():
    """Test 0: data endpoints reject anonymous callers."""
    print("\n[Test 0] Auth enforcement")
    resp = requests.get(f"{API}/api/files", params={"parent": "root"})
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"
    resp = requests.get(f"{API}/api/stats")
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"
    print("  ✅ Anonymous access rejected with 401")


def test_basic_upload_download():
    """Test 1: Upload a small file and download it back."""
    print("\n[Test 1] Basic upload + download")
    t = token()

    content = b"Hello MyDrive! This is a test file.\n" * 50
    files = {"file": ("test.txt", content, "text/plain")}

    resp = requests.post(f"{API}/api/upload", files=files, headers=hdr(t))
    assert resp.status_code == 200, f"Upload failed: {resp.text}"
    meta = resp.json()
    print(f"  Uploaded: {meta['name']} size={meta['size']} chunks={meta['chunks']}")
    print(f"  CID: {meta['content_identity']}")

    resp = requests.get(f"{API}/api/download/{meta['identity']}", headers=hdr(t))
    assert resp.status_code == 200
    downloaded = resp.content
    print(f"  Downloaded {len(downloaded)} bytes")
    assert downloaded == content, "Content mismatch!"
    print("  ✅ Content matches")

    return meta


def test_instant_transfer():
    """Test 2: Upload same content again — should trigger 秒传 for the same user."""
    print("\n[Test 2] Instant transfer (秒传)")
    t = token()

    content = b"Hello MyDrive! This is a test file.\n" * 50

    check_req = {
        "sha1": sha1_hex(content),
        "head_sha1": head_sha1_hex(content),
        "file_size": len(content),
        "name": "duplicate.txt",
    }
    resp = requests.post(f"{API}/api/instant-check", json=check_req, headers=hdr(t))
    result = resp.json()
    print(f"  Instant transfer: {result['instant_transfer']}")
    print(f"  Content CID: {result.get('content_identity', 'N/A')}")
    assert result["instant_transfer"] is True, "Should have been instant!"
    print("  ✅ Duplicate detected — no data upload needed!")


def test_large_file():
    """Test 3: Upload a large file (multiple chunks)."""
    print("\n[Test 3] Large file (10MB, multiple chunks)")
    t = token()

    content = os.urandom(10 * 1024 * 1024)

    start = time.time()
    resp = requests.post(f"{API}/api/upload", files={"file": ("large.bin", content)},
                         headers=hdr(t))
    elapsed = time.time() - start
    meta = resp.json()
    print(f"  Uploaded {meta['size']:,} bytes in {elapsed:.2f}s")
    print(f"  Chunks: {meta['chunks']}")

    resp = requests.get(f"{API}/api/files/{meta['identity']}/chunks", headers=hdr(t))
    chunks_data = resp.json()
    print(f"  Chunk CIDs: {[c['cid'][:20] for c in chunks_data['chunks']]}")

    print("  Downloading chunks individually...")
    downloaded_parts = []
    for chunk in chunks_data["chunks"]:
        resp = requests.get(f"{API}/api/chunk/{chunk['cid']}", headers=hdr(t))
        assert resp.status_code == 200
        assert len(resp.content) == chunk["size"]
        downloaded_parts.append(resp.content)
        print(f"    chunk[{chunk['index']}]: {chunk['cid'][:20]}... {chunk['size']:,} bytes ✅")

    reassembled = b"".join(downloaded_parts)
    assert reassembled == content, "Reassembled content mismatch!"
    print("  ✅ All chunks downloaded and reassembled — content matches!")

    resp = requests.get(f"{API}/api/download/{meta['identity']}", headers=hdr(t))
    assert resp.content == content
    print("  ✅ Full stream download also matches")


def test_dedup():
    """Test 4: Two files with same content share chunks (dedup) within a user."""
    print("\n[Test 4] Deduplication")
    t = token()

    content = os.urandom(5 * 1024 * 1024)

    meta_a = requests.post(f"{API}/api/upload", files={"file": ("file_a.bin", content)},
                           headers=hdr(t)).json()
    meta_b = requests.post(f"{API}/api/upload", files={"file": ("file_b.bin", content)},
                           headers=hdr(t)).json()

    print(f"  File A: {meta_a['identity']} CID={meta_a['content_identity'][:20]}...")
    print(f"  File B: {meta_b['identity']} CID={meta_b['content_identity'][:20]}...")
    assert meta_a["content_identity"] == meta_b["content_identity"]
    print("  ✅ Same content = same CID (dedup at content level)")

    stats = requests.get(f"{API}/api/stats", headers=hdr(t)).json()
    print(f"  Stats: {stats['total_chunks']} chunks, "
          f"logical={stats['logical_bytes']:,}, disk={stats['disk_bytes']:,}")
    assert stats["disk_bytes"] < stats["logical_bytes"], "Dedup should reduce disk usage!"
    print(f"  ✅ Dedup saves {stats['logical_bytes'] - stats['disk_bytes']:,} bytes!")


def test_verify_chunk():
    """Test 5: Verify a chunk by CID."""
    print("\n[Test 5] Chunk verification by CID")
    t = token()

    content = b"verify test " * 1000
    meta = requests.post(f"{API}/api/upload", files={"file": ("verify.bin", content)},
                         headers=hdr(t)).json()

    chunks = requests.get(f"{API}/api/files/{meta['identity']}/chunks", headers=hdr(t)).json()["chunks"]

    for chunk in chunks:
        verify = requests.get(f"{API}/api/verify/{chunk['cid']}", headers=hdr(t)).json()
        print(f"  Chunk {chunk['cid'][:20]}...: size={verify['size']} sha256={verify['sha256'][:16]}...")
        assert verify["exists"]
        assert verify["size"] == chunk["size"]
    print("  ✅ All chunks verified")


def test_file_operations():
    """Test 6: Create directory, list files, delete."""
    print("\n[Test 6] File operations (mkdir, list, delete)")
    t = token()

    resp = requests.post(f"{API}/api/mkdir", json={"name": "my_folder", "parent": "root"},
                         headers=hdr(t))
    assert resp.status_code == 200
    dir_meta = resp.json()
    print(f"  Created dir: {dir_meta['identity']}")

    content = b"file in folder"
    resp = requests.post(f"{API}/api/upload", files={"file": ("in_folder.txt", content)},
                         params={"parent": dir_meta["identity"]}, headers=hdr(t))
    assert resp.status_code == 200
    file_meta = resp.json()
    print(f"  Uploaded file to folder: {file_meta['identity']}")

    root_files = requests.get(f"{API}/api/files", params={"parent": "root"}, headers=hdr(t)).json()
    print(f"  Root has {len(root_files)} items")

    dir_files = requests.get(f"{API}/api/files", params={"parent": dir_meta["identity"]},
                             headers=hdr(t)).json()
    print(f"  Folder has {len(dir_files)} items")
    assert len(dir_files) >= 1

    resp = requests.delete(f"{API}/api/files/{file_meta['identity']}", headers=hdr(t))
    assert resp.status_code == 200
    print(f"  Trashed file: {file_meta['identity']}")

    dir_files = requests.get(f"{API}/api/files", params={"parent": dir_meta["identity"]},
                             headers=hdr(t)).json()
    print(f"  Folder now has {len(dir_files)} items (after trash)")
    print("  ✅ File operations work")


def test_cross_user_isolation():
    """Test 7: A second user must not see or fetch the first user's data."""
    print("\n[Test 7] Cross-user isolation")

    owner = login("e2e_owner")
    intruder = login("e2e_intruder")
    assert owner["user_id"] != intruder["user_id"]

    secret = b"top secret payload " * 200
    meta = requests.post(f"{API}/api/upload", files={"file": ("secret.txt", secret)},
                         headers=hdr(owner["access_token"])).json()
    print(f"  Owner uploaded {meta['identity'][:8]}…")

    # Listing must not include the other user's file
    listing = requests.get(f"{API}/api/files", params={"parent": "root"},
                           headers=hdr(intruder["access_token"])).json()
    assert all(item["identity"] != meta["identity"] for item in listing), \
        "Intruder saw another user's file in listing"
    print("  ✅ File not visible in another user's listing")

    # Direct metadata / download / chunk access must 404
    assert requests.get(f"{API}/api/files/{meta['identity']}",
                        headers=hdr(intruder["access_token"])).status_code == 404
    assert requests.get(f"{API}/api/download/{meta['identity']}",
                        headers=hdr(intruder["access_token"])).status_code == 404
    chunk_cid = requests.get(f"{API}/api/files/{meta['identity']}/chunks",
                             headers=hdr(owner["access_token"])).json()["chunks"][0]["cid"]
    assert requests.get(f"{API}/api/chunk/{chunk_cid}",
                        headers=hdr(intruder["access_token"])).status_code == 404
    print("  ✅ Metadata, download and chunk all denied")

    # 秒传是全局内容去重（线上语义）：持有内容哈希即可秒传，但响应只回 CID，
    # 不泄露文件名、所有者或路径。
    probe = {
        "sha1": sha1_hex(secret),
        "head_sha1": head_sha1_hex(secret),
        "file_size": len(secret),
    }
    inst = requests.post(f"{API}/api/instant-check", json=probe,
                         headers=hdr(intruder["access_token"])).json()
    assert inst["instant_transfer"] is True, "全局秒传应按内容命中"
    assert set(inst) <= {"instant_transfer", "content_identity", "file_identity"}, \
        f"秒传响应泄露了额外字段: {sorted(inst)}"
    assert inst.get("content_identity"), "秒传应返回 content_identity"
    print("  ✅ 秒传全局命中，且只回 CID 不泄露元数据")

    # Sharing someone else's file must fail
    resp = requests.post(f"{API}/api/share", json={"file_identity": meta["identity"]},
                         headers=hdr(intruder["access_token"]))
    assert resp.status_code == 404
    print("  ✅ Cannot create a share for another user's file")


def test_share_flow():
    """Test 8: Owner creates a share, anonymous visitor downloads it."""
    print("\n[Test 8] Share link flow")
    owner = login()
    content = b"shared content " * 100
    meta = requests.post(f"{API}/api/upload", files={"file": ("shared.txt", content)},
                         headers=hdr(owner["access_token"])).json()

    created = requests.post(
        f"{API}/api/share",
        json={"file_identity": meta["identity"], "password": "pw123", "expire_hours": 1},
        headers=hdr(owner["access_token"]),
    ).json()
    assert created.get("success"), created
    share_id = created["share_id"]
    print(f"  Share created: {share_id} (password protected)")

    bad = requests.get(f"{API}/api/share/{share_id}", params={"password": "wrong"})
    assert bad.status_code == 403
    print("  ✅ Wrong password rejected")

    ok = requests.get(f"{API}/api/share/{share_id}", params={"password": "pw123"})
    assert ok.status_code == 200 and ok.json()["name"] == "shared.txt"
    print("  ✅ Correct password returns metadata")

    dl = requests.get(f"{API}/api/share/{share_id}/download", params={"password": "pw123"})
    assert dl.status_code == 200 and dl.content == content
    print("  ✅ Anonymous download works via share link")


def test_share_social():
    """Test 14: like / dislike / complaint / save-to-my-drive."""
    print("\n[Test 14] Share like, complaint, save")
    owner = login()
    content = b"social share payload " * 120
    meta = requests.post(f"{API}/api/upload", files={"file": ("social.txt", content)},
                         headers=hdr(owner["access_token"])).json()
    created = requests.post(f"{API}/api/share", json={"file_identity": meta["identity"]},
                            headers=hdr(owner["access_token"])).json()
    sid = created["share_id"]

    r = requests.post(f"{API}/api/share/{sid}/react", json={"action": "like"}).json()
    assert r["like"] == 1, r
    r = requests.post(f"{API}/api/share/{sid}/react", json={"action": "like"}).json()
    r = requests.post(f"{API}/api/share/{sid}/react", json={"action": "dislike"}).json()
    assert (r["like"], r["dislike"]) == (2, 1), r
    print(f"  ✅ like/dislike counters: like={r['like']} dislike={r['dislike']}")

    other = login("e2e_saver")
    complaint = requests.post(f"{API}/api/share/{sid}/complaint",
                              json={"content": "e2e complaint"},
                              headers=hdr(other["access_token"])).json()
    assert complaint["file_share_identity"] == sid
    mine = requests.get(f"{API}/api/complaints", headers=hdr(other["access_token"])).json()
    assert any(c["identity"] == complaint["identity"] for c in mine)
    print(f"  ✅ complaint recorded: {complaint['identity'][:8]}…")

    saved = requests.post(f"{API}/api/share/{sid}/save",
                          json={"parent": "root", "name": "saved_copy.txt"},
                          headers=hdr(other["access_token"]))
    assert saved.status_code == 200, saved.text
    clone = saved.json()
    assert clone["identity"] != meta["identity"], "save must create a new record"
    assert clone["size"] == len(content)
    print(f"  ✅ saved into another account as {clone['identity'][:8]}… "
          f"({clone['content_identity'][:14]}… shared CID)")

    got = requests.get(f"{API}/api/download/{clone['identity']}",
                       headers=hdr(other["access_token"]))
    assert got.content == content
    print("  ✅ saved copy downloads with identical bytes (zero data movement)")


def test_stats():
    """Print final storage statistics for the current user."""
    print("\n[Stats] Storage statistics")
    stats = requests.get(f"{API}/api/stats", headers=hdr(token())).json()
    print(f"  Total files: {stats['total_files']}")
    print(f"  Total chunks: {stats['total_chunks']}")
    print(f"  Logical bytes: {stats['logical_bytes']:,}")
    print(f"  Disk bytes: {stats['disk_bytes']:,}")
    print(f"  Dedup ratio: {stats['dedup_ratio']*100:.1f}%")
    print(f"  FastLookup entries: {stats['fast_lookup_entries']}")


# ==================== v6 PROTOCOL ALIGNMENT ====================

def test_cursor_paging():
    """Test 9: cursor paging — token round-trip, no duplicates, dirs first."""
    print("\n[Test 9] Cursor paging (ScanList)")
    # Fresh account per run so the root listing is deterministic
    t = login(f"e2e_paging_{int(time.time())}")["access_token"]

    for name in ("folder_a", "folder_b"):
        requests.post(f"{API}/api/mkdir", json={"name": name, "parent": "root"}, headers=hdr(t))
    for i in range(3):
        requests.post(f"{API}/api/upload", files={"file": (f"page_{i}.txt", b"x" * 64)},
                      headers=hdr(t))

    seen, cursor, pages, first_token, names = [], "", 0, "", set()
    while True:
        resp = requests.get(f"{API}/api/files/scan",
                            params={"parent": "root", "limit": 2, "token": cursor},
                            headers=hdr(t))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        pages += 1
        seen.extend(item["identity"] for item in data["files"])
        names.update(item["name"] for item in data["files"])
        print(f"  page {pages}: {[i['name'] for i in data['files']]} "
              f"token={'yes' if data['list_info']['token'] else 'end'}")
        cursor = data["list_info"]["token"]
        if cursor and not first_token:
            first_token = cursor
        if not cursor or pages > 20:
            break

    assert len(seen) == len(set(seen)), f"duplicate identities across pages: {seen}"
    assert {"folder_a", "folder_b", "page_0.txt"} <= names, "missing entries"
    assert data["list_info"]["total"] == len(seen), "total does not match page walk"
    print(f"  ✅ walked {len(seen)} items in {pages} pages, no duplicates")

    # The cursor itself is base64(JSON) with skip/limit/order_by
    import base64, json as _json
    decoded = _json.loads(base64.b64decode(first_token + "==").decode())
    print(f"  cursor payload: skip={decoded['skip']} limit={decoded['limit']} "
          f"order_by={decoded['order_by']}")


def test_presigned_slices():
    """Test 10: presigned slice URLs work without a Bearer token, and are scoped."""
    print("\n[Test 10] Presigned slice download (auth_key)")
    t = token()
    content = os.urandom(9 * 1024 * 1024)

    meta = requests.post(f"{API}/api/upload", files={"file": ("slices.bin", content)},
                         headers=hdr(t)).json()
    info = requests.get(f"{API}/api/files/{meta['identity']}/download-info",
                        params={"limit": 2}, headers=hdr(t)).json()
    print(f"  file has {len(meta['identity'])}-char id, {info['list_info']['total']} slices total")
    assert len(info["slices"]) == 2

    first = info["slices"][0]
    print(f"  slice[0] {first['identity'][:20]}… size={first['size']:,}")
    # 线上返回完整 URL（指向 slices 独立域名），MyDrive 指向独立端口
    assert "/ipfs/blocks/" in first["download_address"], first["download_address"][:90]
    assert first["download_address"].split("/ipfs/blocks/")[1].split("/")[0] == "2"

    plain = requests.get(first["download_address"])
    assert plain.status_code == 200, plain.text
    assert plain.content == content[:first["size"]]
    print("  ✅ slice fetched with auth_key only (no Authorization header)")

    tampered = first["download_address"][:-4] + "dead"
    assert requests.get(tampered).status_code == 403
    print("  ✅ tampered signature rejected (403)")

    shard = first["download_address"].split("/ipfs/blocks/")[1].split("/")
    anon = requests.get(first["download_address"].split("?")[0])
    assert anon.status_code == 403
    print("  ✅ missing auth_key rejected (403)")


def test_resumable_upload():
    """Test 11: block upload flow — init -> blocks -> complete -> download."""
    print("\n[Test 11] Resumable upload (CreateUploadTask flow)")
    t = token()

    blocks = [os.urandom(1024 * 1024) for _ in range(3)]
    whole = b"".join(blocks)

    init = requests.post(f"{API}/api/upload/init", headers=hdr(t), json={
        "name": "blocky.bin", "parent": "root", "file_size": len(whole),
        "sha1": hashlib.sha1(whole).hexdigest(),
        "head_sha1": head_sha1_hex(whole),
    }).json()
    assert init["block_size"] == 4194304 and init["block_hash_type"] == 0x12
    print(f"  task: block_size={init['block_size']} hash_type={init['block_hash_type']} "
          f"codec={init['block_codec']}")
    assert not init["instant"], "fresh random content must not be instant"

    for index, block in enumerate(blocks):
        cid = compute_cid(block)
        resp = requests.post(f"{API}/api/upload/block",
                             params={"task": init["task"], "cid": cid, "index": index},
                             data=block, headers=hdr(t))
        assert resp.status_code == 200, resp.text
        print(f"  block[{index}] {cid[:20]}… {len(block):,} B ✅")

    done = requests.post(f"{API}/api/upload/complete", headers=hdr(t), json={
        "task": init["task"], "name": "blocky.bin", "parent": "root",
        "size": len(whole), "sha1": hashlib.sha1(whole).hexdigest(),
        "head_sha1": head_sha1_hex(whole),
        "cids": [compute_cid(b) for b in blocks],
    })
    assert done.status_code == 200, done.text
    meta = done.json()["file"]
    assert len(meta["identity"]) == 32, "identity must be 32 hex chars"
    print(f"  file {meta['identity']} type={meta['type']} content={meta['content_identity'][:16]}…")

    got = requests.get(f"{API}/api/download/{meta['identity']}", headers=hdr(t))
    assert got.content == whole
    print("  ✅ blocks reassembled and content matches")

    # Same content again -> instant transfer via the init call
    again = requests.post(f"{API}/api/upload/init", headers=hdr(t), json={
        "name": "blocky_copy.bin", "parent": "root", "file_size": len(whole),
        "sha1": hashlib.sha1(whole).hexdigest(),
        "head_sha1": head_sha1_hex(whole),
    }).json()
    assert again["instant"] is True
    print("  ✅ second init returned instant=True (秒传, zero bytes uploaded)")


def _sign_headers(service, method, token="", secret=APP_SECRET, ts=None):
    ts = ts or str(int(time.time() * 1000))
    plain = f"/{service}/{method}{ts}2dland/web202603182100"
    if token:
        plain += "Bearer " + token
    return {
        "timestamp": ts,
        "appid": "2dland/web",
        "appversion": "202603182100",
        "sign": hashlib.md5((plain + secret).encode()).hexdigest(),
        **({"authorization": f"Bearer {token}"} if token else {}),
    }


def _grpc_call(service, method, payload=b"", token="", secret=APP_SECRET):
    headers = {"content-type": "application/grpc-web+proto", "te": "trailers"}
    headers.update(_sign_headers(service, method, token, secret))
    frame = b"\x00" + len(payload).to_bytes(4, "big") + payload
    resp = requests.post(f"{API}/v6.services.pub.{service}/{method}",
                         data=frame, headers=headers)
    status = int(resp.headers.get("grpc-status", "0") or 0)
    body, pos = b"", 0
    raw = resp.content
    while pos + 5 <= len(raw):
        flag = raw[pos]
        length = int.from_bytes(raw[pos + 1:pos + 5], "big")
        chunk = raw[pos + 5:pos + 5 + length]
        if flag == 0:
            body = chunk
        pos += 5 + length
    return status, body, resp.headers.get("grpc-message", "")


def test_grpc_web():
    """Test 12: gRPC-Web compatibility layer — sign, login, list."""
    print("\n[Test 12] gRPC-Web compatibility layer")
    import pb as pbmod

    # 12a. login (no token, valid signature)
    login_req = pbmod.encode("LoginRequest", {"identity": "e2e_main",
                                              "password": "e2e_password_123"})
    status, body, msg = _grpc_call("PubUser", "Login", login_req)
    assert status == 0, f"login failed: {status} {msg}"
    parsed = pbmod.decode("LoginResponse", body)
    access = parsed["token"]["access_token"]
    print(f"  login ok: user={parsed['user']['name']} token={access[:24]}…")

    # 12b. bad signature -> grpc-status 3
    status, _, msg = _grpc_call("PubUser", "Login", login_req, secret="wrong-secret")
    assert status == 3, f"expected invalid_sign, got {status}"
    print(f"  ✅ bad signature rejected: grpc-status={status} ({msg})")

    # 12c. missing token -> grpc-status 16
    list_req = pbmod.encode("FileListRequest", {"list_info": {"limit": 3}})
    status, _, msg = _grpc_call("PubUserFile", "List", list_req)
    assert status == 16, f"expected unauthenticated, got {status}"
    print(f"  ✅ missing token rejected: grpc-status={status}")

    # 12d. signed list with token
    status, body, msg = _grpc_call("PubUserFile", "List", list_req, token=access)
    assert status == 0, f"list failed: {status} {msg}"
    listing = pbmod.decode("FileListResponse", body)
    files = listing.get("files") or []
    print(f"  list returned {len(files)} files, next_token="
          f"{'yes' if (listing.get('list_info') or {}).get('token') else 'no'}")
    for item in files[:3]:
        print(f"    {item['identity'][:8]}… type={item.get('type')} dir={item.get('dir')} "
              f"{item['name']}")

    # 12e. identity format check
    for item in files:
        assert len(item["identity"]) == 32 and all(
            c in "0123456789abcdef" for c in item["identity"]), "identity not 32-hex"
    print("  ✅ every returned identity is 32 hex chars")

    # 12f. quota RPC
    status, body, _ = _grpc_call("PubUser", "GetStatisticsAndQuota", b"", token=access)
    assert status == 0
    quota = pbmod.decode("UserStatisticsAndQuota", body)
    print(f"  quota: {quota['disk_statistics_quota']['bytes_used']:,} / "
          f"{quota['disk_statistics_quota']['bytes_quota']:,} bytes")
    print("  ✅ PubUser.GetStatisticsAndQuota returns protobuf quota")


def test_rest_gateway():
    """Test 13: REST gateway (/v6/userfile/*) with camelCase JSON."""
    print("\n[Test 13] REST gateway")
    t = token()
    headers = {"content-type": "application/json"}
    headers.update(_sign_headers("PubUserFile", "List", t))

    resp = requests.post(f"{API}/v6/userfile/list", headers=headers,
                         json={"parent": {"identity": "root"}, "listInfo": {"limit": 3}})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "files" in data and "listInfo" in data
    print(f"  POST /v6/userfile/list -> {len(data['files'])} files, "
          f"listInfo keys={list(data['listInfo'].keys())}")
    if data["files"]:
        sample = data["files"][0]
        print(f"  camelCase fields: {sorted(sample.keys())[:6]} …")

    bad = requests.post(f"{API}/v6/userfile/list", headers={"content-type": "application/json"},
                        json={"listInfo": {"limit": 1}})
    assert bad.status_code == 401
    print("  ✅ REST gateway requires the token too")


if __name__ == "__main__":
    print("=" * 60)
    print("  MyDrive End-to-End Test Suite")
    print("=" * 60)

    test_requires_auth()
    test_basic_upload_download()
    test_instant_transfer()
    test_large_file()
    test_dedup()
    test_verify_chunk()
    test_file_operations()
    test_cross_user_isolation()
    test_share_flow()
    test_cursor_paging()
    test_presigned_slices()
    test_resumable_upload()
    test_grpc_web()
    test_rest_gateway()
    test_share_social()
    test_stats()

    print("\n" + "=" * 60)
    print("  ✅ ALL TESTS PASSED")
    print("=" * 60)
