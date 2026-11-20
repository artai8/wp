"""独立 SFTP 服务：把网盘挂成 sftp://<host>:2022。

对齐线上 ``sftp://ftp.2dland.cn:2022``：
  * 独立端口（默认 2022），不占用 HTTP 端口
  * 账号密码来自 ``PubSftpConfig``（``storage_data/json/sftp.json``）下发的独立凭据
  * 也可用网盘账号密码直接登录（与 WebDAV 网关行为一致）
  * 支持 ls / get / put / rm / mv / mkdir / rmdir，读写走同一套分块 + CID 入库
"""
from __future__ import annotations

import io
import json
import os
import socket
import stat as stat_mod
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import paramiko
from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface

DEFAULT_PORT = int(os.environ.get("SFTP_PORT", "2022"))

# SFTP 协议的 open pflags（与 os.O_* 数值不同，必须按协议定义判断）
F_READ, F_WRITE, F_APPEND, F_CREAT, F_TRUNC, F_EXCL = 0x1, 0x2, 0x4, 0x8, 0x10, 0x20
HOST_KEY_FILE = os.environ.get("SFTP_HOST_KEY", "")


# ------------------------------------------------------------------ auth
class SftpAuth:
    """账号来源：PubSftpConfig 独立凭据优先，其次网盘账号。"""

    def __init__(self, auth, cfg_path: str):
        self.auth = auth
        self.cfg_path = Path(cfg_path)

    def _configs(self) -> Dict[str, dict]:
        try:
            return json.loads(self.cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def check_password(self, username: str, password: str) -> Optional[str]:
        # 1) SFTP 专属凭据
        for user_id, cfg in self._configs().items():
            if cfg.get("username") == username and cfg.get("password") == password:
                if int(cfg.get("enable_flag", 1)) == 0:
                    return None
                return user_id
        # 2) 网盘账号
        result = self.auth.login(username, password)
        return result.get("user_id") if result.get("success") else None

    def config_of(self, user_id: str) -> dict:
        cfg = self._configs().get(user_id) or {}
        return {
            "root": cfg.get("root") or "/",
            "write_flag": int(cfg.get("write_flag", 1)),
            "enable_flag": int(cfg.get("enable_flag", 1)),
        }


class _Server(paramiko.ServerInterface):
    def __init__(self, auth: SftpAuth, engine, storage_root: str):
        self.auth = auth
        self.engine = engine
        self.storage_root = storage_root
        self.user_id: Optional[str] = None
        self.cfg: dict = {}
        self.event = threading.Event()

    def check_auth_password(self, username, password):
        uid = self.auth.check_password(username, password)
        if not uid:
            return paramiko.AUTH_FAILED
        self.user_id = uid
        self.cfg = self.auth.config_of(uid)
        if self.cfg.get("enable_flag", 1) == 0:
            return paramiko.AUTH_FAILED
        return paramiko.AUTH_SUCCESSFUL

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_subsystem_request(self, channel, name):
        if name == "sftp":
            self.event.set()
        return super().check_channel_subsystem_request(channel, name)


# ------------------------------------------------------------------ fs
class _VFS:
    """把 SFTP 路径映射到 storage_engine 的虚拟目录树。"""

    def __init__(self, engine, user_id: str, root: str = "/", writable: bool = True):
        self.engine = engine
        self.user_id = user_id
        self.root = (root or "/").rstrip("/") or "/"
        self.writable = writable

    # -- path helpers --
    def _parts(self, path: str):
        path = "/" + (path or "/").replace("\\", "/").strip("/")
        if self.root != "/":
            if not path.startswith(self.root):
                raise PermissionError("outside of root")
            path = path[len(self.root):] or "/"
        return [p for p in path.split("/") if p and p != "."]

    def resolve(self, path: str):
        """-> (FileMeta|None, parent_meta_id, name)"""
        parts = self._parts(path)
        if not parts:
            return None, "root", ""
        parent = "root"
        node = None
        for idx, part in enumerate(parts):
            node = None
            for meta in self.engine.list_files(parent=parent, user_id=self.user_id):
                if meta.name == part:
                    node = meta
                    break
            if node is None:
                return None, parent, parts[-1]
            parent = node.identity
        return node, parent, node.name

    def listdir(self, path: str):
        meta, parent, name = self.resolve(path)
        if meta is None:
            if path.strip("/") in ("", ".") or not self._parts(path):
                return self.engine.list_files(parent="root", user_id=self.user_id)
            raise FileNotFoundError(path)
        if not meta.is_dir:
            raise NotADirectoryError(path)
        return self.engine.list_files(parent=meta.identity, user_id=self.user_id)

    def mkdir(self, path: str):
        meta, parent, name = self.resolve(path)
        if meta is not None:
            raise FileExistsError(path)
        return self.engine.create_dir(name, parent=parent, user_id=self.user_id)

    def remove(self, path: str):
        meta, _, _ = self.resolve(path)
        if meta is None:
            raise FileNotFoundError(path)
        if meta.is_dir:
            raise IsADirectoryError(path)
        self.engine.delete_file(meta.identity, permanent=True, user_id=self.user_id)

    def rmdir(self, path: str):
        meta, _, _ = self.resolve(path)
        if meta is None:
            raise FileNotFoundError(path)
        if not meta.is_dir:
            raise NotADirectoryError(path)
        if self.engine.list_files(parent=meta.identity, user_id=self.user_id):
            raise OSError("directory not empty")
        self.engine.delete_file(meta.identity, permanent=True, user_id=self.user_id)

    def rename(self, old: str, new: str):
        meta, _, _ = self.resolve(old)
        if meta is None:
            raise FileNotFoundError(old)
        target, parent, name = self.resolve(new)
        if target is not None:
            raise FileExistsError(new)
        if parent != meta.parent:
            self.engine.move(meta.identity, new_parent=parent, user_id=self.user_id)
            meta = self.engine.get_file_meta(meta.identity, user_id=self.user_id)
        self.engine.rename(meta.identity, new_name=name, user_id=self.user_id)

    def read_bytes(self, path: str) -> bytes:
        meta, _, _ = self.resolve(path)
        if meta is None:
            raise FileNotFoundError(path)
        if meta.is_dir:
            raise IsADirectoryError(path)
        buf = io.BytesIO()
        for _cid, _idx, data in self.engine.stream_chunks(meta.identity, user_id=self.user_id):
            buf.write(data)
        return buf.getvalue()

    def write_bytes(self, path: str, data: bytes):
        meta, parent, name = self.resolve(path)
        if meta is not None:
            if meta.is_dir:
                raise IsADirectoryError(path)
            self.engine.delete_file(meta.identity, permanent=True, user_id=self.user_id)
        self.engine.chunk_data(data, name=name, parent=parent, user_id=self.user_id)


def _attrs(meta, name: str) -> SFTPAttributes:
    a = SFTPAttributes()
    a.filename = name
    a.st_size = 0 if meta.is_dir else meta.size
    a.st_uid = 0
    a.st_gid = 0
    a.st_mode = (stat_mod.S_IFDIR | 0o755) if meta.is_dir else (stat_mod.S_IFREG | 0o644)
    ts = int((meta.update_ts or meta.create_ts or 0) / 1000)
    a.st_atime = ts
    a.st_mtime = ts
    return a


class _RootAttrs(SFTPAttributes):
    def __init__(self):
        super().__init__()
        self.filename = "/"
        self.st_size = 0
        self.st_mode = stat_mod.S_IFDIR | 0o755
        ts = int(time.time())
        self.st_atime = ts
        self.st_mtime = ts
        self.st_uid = 0
        self.st_gid = 0


def _is_write(flags: int) -> bool:
    return bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
                         | F_WRITE | F_CREAT | F_TRUNC | F_APPEND))


class _Handle(SFTPHandle):
    """读写都走内存缓冲，close 时一次性入库（保持 CID 计算一致）。"""

    def __init__(self, vfs: _VFS, path: str, flags: int, writable: bool):
        super().__init__(flags)
        self.vfs = vfs
        self.path = path
        self.writing = _is_write(flags) and writable
        self.buf = bytearray()
        if self.writing:
            if not (flags & (os.O_TRUNC | F_TRUNC)) and not (flags & os.O_WRONLY):
                try:
                    self.buf += vfs.read_bytes(path)
                except Exception:
                    pass
        else:
            self.buf += vfs.read_bytes(path)

    def read(self, offset, length):
        return bytes(self.buf[offset:offset + length])

    def write(self, offset, data):
        # paramiko 5.x: 返回值就是 SFTP status code（不是写入字节数）
        if not self.writing:
            return paramiko.SFTP_PERMISSION_DENIED
        if offset > len(self.buf):
            self.buf += b"\x00" * (offset - len(self.buf))
        self.buf[offset:offset + len(data)] = data
        return paramiko.SFTP_OK

    def stat(self):
        try:
            meta, _, name = self.vfs.resolve(self.path)
            if meta is not None and not self.writing:
                return _attrs(meta, name)
        except Exception:
            pass
        a = SFTPAttributes()
        a.filename = Path(self.path).name
        a.st_size = len(self.buf)
        a.st_mode = stat_mod.S_IFREG | 0o644
        ts = int(time.time())
        a.st_atime = a.st_mtime = ts
        a.st_uid = a.st_gid = 0
        return a

    def chattr(self, attr):
        return paramiko.SFTP_OK

    def close(self):
        if self.writing:
            try:
                self.vfs.write_bytes(self.path, bytes(self.buf))
            except Exception:
                pass
        super().close()


class _Interface(SFTPServerInterface):
    def __init__(self, server: _Server, *args, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.server = server

    def _vfs(self) -> _VFS:
        cfg = self.server.cfg or {}
        return _VFS(self.server.engine, self.server.user_id or "anonymous",
                    cfg.get("root") or "/", bool(cfg.get("write_flag", 1)))

    def _check_write(self):
        cfg = self.server.cfg or {}
        if int(cfg.get("write_flag", 1)) == 0:
            raise PermissionError("read-only")

    def list_folder(self, path):
        vfs = self._vfs()
        try:
            items = vfs.listdir(path)
        except Exception:
            return paramiko.SFTP_NO_SUCH_FILE
        return [_attrs(m, m.name) for m in items]

    def stat(self, path):
        vfs = self._vfs()
        try:
            meta, _, name = vfs.resolve(path)
        except Exception:
            return paramiko.SFTP_NO_SUCH_FILE
        if meta is None:
            if not vfs._parts(path):
                return _RootAttrs()
            return paramiko.SFTP_NO_SUCH_FILE
        return _attrs(meta, name)

    def lstat(self, path):
        return self.stat(path)

    def open(self, path, flags, attr):
        vfs = self._vfs()
        writing = _is_write(flags)
        if writing:
            try:
                self._check_write()
            except PermissionError:
                return paramiko.SFTP_PERMISSION_DENIED
            parent = Path(path).parent.as_posix()
            if (flags & (os.O_CREAT | F_CREAT)) and vfs.resolve(path)[0] is None:
                if vfs.resolve(parent)[0] is None and parent not in ("/", "."):
                    return paramiko.SFTP_NO_SUCH_FILE
        elif vfs.resolve(path)[0] is None:
            return paramiko.SFTP_NO_SUCH_FILE
        try:
            return _Handle(vfs, path, flags, bool(vfs.writable))
        except Exception:
            return paramiko.SFTP_FAILURE

    def remove(self, path):
        try:
            self._check_write()
            self._vfs().remove(path)
            return paramiko.SFTP_OK
        except PermissionError:
            return paramiko.SFTP_PERMISSION_DENIED
        except FileNotFoundError:
            return paramiko.SFTP_NO_SUCH_FILE
        except Exception:
            return paramiko.SFTP_FAILURE

    def rename(self, oldpath, newpath):
        try:
            self._check_write()
            self._vfs().rename(oldpath, newpath)
            return paramiko.SFTP_OK
        except PermissionError:
            return paramiko.SFTP_PERMISSION_DENIED
        except FileNotFoundError:
            return paramiko.SFTP_NO_SUCH_FILE
        except Exception:
            return paramiko.SFTP_FAILURE

    def mkdir(self, path, attr):
        try:
            self._check_write()
            self._vfs().mkdir(path)
            return paramiko.SFTP_OK
        except PermissionError:
            return paramiko.SFTP_PERMISSION_DENIED
        except FileExistsError:
            return paramiko.SFTP_FAILURE
        except Exception:
            return paramiko.SFTP_FAILURE

    def rmdir(self, path):
        try:
            self._check_write()
            self._vfs().rmdir(path)
            return paramiko.SFTP_OK
        except PermissionError:
            return paramiko.SFTP_PERMISSION_DENIED
        except FileNotFoundError:
            return paramiko.SFTP_NO_SUCH_FILE
        except Exception:
            return paramiko.SFTP_FAILURE

    def chattr(self, path, attr):
        return paramiko.SFTP_OK

    def canonicalize(self, path):
        parts = [p for p in (path or "/").replace("\\", "/").split("/") if p and p != "."]
        out = []
        for p in parts:
            if p == "..":
                if out:
                    out.pop()
            else:
                out.append(p)
        return "/" + "/".join(out)


# ------------------------------------------------------------------ server
class SftpService:
    def __init__(self, engine, auth, cfg_path: str, port: int = DEFAULT_PORT,
                 host_key: str = "", bind: str = "0.0.0.0"):
        self.engine = engine
        self.auth = SftpAuth(auth, cfg_path)
        self.port = port
        self.bind = bind
        self.host_key = paramiko.RSAKey.generate(2048) if not host_key else \
            paramiko.RSAKey(filename=host_key)
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.ready = False

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.bind, self.port))
        except OSError as exc:
            print(f"  [sftp] bind {self.bind}:{self.port} failed: {exc}")
            return False
        sock.listen(64)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="sftp-server", daemon=True)
        self._thread.start()
        self.ready = True
        return True

    def stop(self):
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        self.ready = False

    def _serve(self):
        while not self._stop.is_set():
            try:
                client, addr = self._sock.accept()
            except Exception:
                break
            threading.Thread(target=self._handle, args=(client, addr), daemon=True).start()

    def _handle(self, client: socket.socket, addr):
        import traceback
        try:
            transport = paramiko.Transport(client)
            transport.add_server_key(self.host_key)
            transport.set_subsystem_handler("sftp", SFTPServer, _Interface)
            server = _Server(self.auth, self.engine, "")
            transport.start_server(server=server)
            server.event.wait(30)
            while transport.is_active():
                time.sleep(0.5)
        except Exception:
            traceback.print_exc()
        finally:
            try:
                client.close()
            except Exception:
                pass
