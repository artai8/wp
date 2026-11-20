"""
WebDAV Gateway
==============
Maps the storage engine to standard WebDAV protocol.
Clients can mount it as a network drive (Windows: Map Network Drive, Mac: Finder > Connect to Server).

WebDAV methods implemented:
- OPTIONS: return DAV capabilities
- PROPFIND: list directory (depth: 0 or 1) or get file props
- GET: download file
- PUT: upload file
- MKCOL: create directory
- DELETE: delete file/folder
- MOVE: rename/move
- COPY: copy file

URL structure:
  http://host:port/webdav/          -> root directory
  http://host:port/webdav/folder/   -> a subdirectory
  http://host:port/webdav/file.txt  -> a file

Mount examples:
  Windows:  net use Z: http://127.0.0.1:8765/webdav /user:admin password
  macOS:    mount -t webdav http://127.0.0.1:8765/webdav /Volumes/MyDrive
  Linux:    mount -t davfs http://127.0.0.1:8765/webdav /mnt/mydrive
"""

import os
import json
import time
import uuid
import base64
import contextvars
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, List
from fastapi import Request, Response, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, Response as FastResponse, StreamingResponse

from storage_engine import StorageEngine, FileMeta

# Per-request caller id for WebDAV. ContextVars are task-local, so concurrent
# WebDAV requests never leak each other's identity into shared helpers.
_DAV_UID: contextvars.ContextVar[str] = contextvars.ContextVar("dav_uid", default="anonymous")


class WebDAVGateway:
    """Bridges WebDAV protocol to the storage engine."""

    def __init__(self, app: FastAPI, engine: StorageEngine, prefix: str = "/webdav",
                 auth=None, auth_required: bool = True):
        self.app = app
        self.engine = engine
        self.auth = auth
        self.auth_required = auth_required and auth is not None
        self.prefix = prefix.rstrip("/")
        self._register_routes()
        self._register_auth_middleware()

    # ==================== AUTH ====================

    def _identify(self, request: Request) -> Optional[str]:
        """Resolve a user_id from Basic (WebDAV clients) or Bearer auth."""
        if not self.auth:
            return None
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                raw = base64.b64decode(header[6:].strip()).decode()
                username, _, password = raw.partition(":")
            except Exception:
                return None
            # 1) 独立 WebDAV 凭据（PubDavConfig 下发的那一套）
            owner = self._dav_owner(username, password)
            if owner:
                return owner
            # 2) 回退到账户密码
            result = self.auth.login(username, password)
            return result.get("user_id") if result.get("success") else None
        if header.lower().startswith("bearer "):
            user = self.auth.verify_token(header[7:].strip())
            return user["user_id"] if user else None
        return None

    def _dav_owner(self, username: str, password: str) -> Optional[str]:
        """Match the mount credentials issued by ``PubDavConfig``.

        The live service hands out its own username/password pair (6 digits),
        independent of the login account — WebDAV clients use that pair only.
        """
        if not username or not password:
            return None
        path = Path(getattr(self.engine, "root", ".")) / "json" / "dav.json"
        if not path.exists():
            return None
        try:
            configs = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        for owner, cfg in (configs or {}).items():
            if not isinstance(cfg, dict):
                continue
            if cfg.get("enable_flag") == 0:
                continue
            if cfg.get("username") == username and cfg.get("password") == password:
                return owner
        return None

    def _register_auth_middleware(self):
        @self.app.middleware("http")
        async def dav_auth(request: Request, call_next):
            path = request.url.path
            if path != self.prefix and not path.startswith(self.prefix + "/"):
                return await call_next(request)

            uid = self._identify(request)
            if uid is None:
                if self.auth_required:
                    return FastResponse(
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="MyDrive"'},
                    )
                uid = "anonymous"
            token = _DAV_UID.set(uid)
            try:
                return await call_next(request)
            finally:
                _DAV_UID.reset(token)

    def _register_routes(self):
        """Register all WebDAV routes on the FastAPI app."""
        p = self.prefix

        @self.app.api_route(f"{p}/{{path:path}}", methods=["OPTIONS"])
        async def dav_options(path: str = ""):
            return FastResponse(
                content="",
                headers={
                    "DAV": "1, 2",
                    "MS-Author-Via": "DAV",
                    "Allow": "OPTIONS, PROPFIND, GET, PUT, MKCOL, DELETE, MOVE, COPY",
                    "Content-Length": "0",
                }
            )

        @self.app.api_route(f"{p}/{{path:path}}", methods=["PROPFIND"])
        async def dav_propfind(request: Request, path: str = ""):
            """List directory or get file properties."""
            depth = request.headers.get("depth", "1")

            # Find the file/folder by path
            target = self._resolve_path(path)
            if not target:
                raise HTTPException(status_code=404, detail="Not found")

            if depth == "0":
                # Just return properties of this item
                items = [target]
            else:
                # List children if directory
                if not target.is_dir:
                    items = [target]
                else:
                    children = self.engine.list_files(target.identity, user_id=_DAV_UID.get())
                    items = [target] + children

            # Build XML response
            xml = self._build_multistatus(items, path)
            return FastResponse(
                content=xml,
                status_code=207,  # Multi-Status: what real WebDAV servers answer
                media_type="application/xml; charset=utf-8",
                headers={"DAV": "1, 2"},
            )

        @self.app.api_route(f"{p}/{{path:path}}", methods=["GET"])
        async def dav_get(path: str = ""):
            """Download a file."""
            target = self._resolve_path(path)
            if not target:
                raise HTTPException(status_code=404, detail="Not found")
            if target.is_dir:
                # Return directory listing as HTML
                children = self.engine.list_files(target.identity, user_id=_DAV_UID.get())
                html = self._directory_listing_html(path, children)
                return FastResponse(content=html, media_type="text/html")

            # Stream file chunks
            def stream():
                for _, _, data in self.engine.stream_chunks(target.identity, user_id=_DAV_UID.get()):
                    yield data

            return StreamingResponse(
                stream(),
                media_type=target.mime_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{target.name}"',
                    "Content-Length": str(target.size),
                }
            )

        @self.app.api_route(f"{p}/{{path:path}}", methods=["PUT"])
        async def dav_put(request: Request, path: str = ""):
            """Upload a file."""
            data = await request.body()

            # Find or create parent directory
            parts = [p for p in path.split("/") if p]
            if not parts:
                raise HTTPException(status_code=403, detail="Cannot PUT to root")

            filename = parts[-1]
            parent_path = "/".join(parts[:-1])
            parent = self._resolve_path(parent_path) or self._ensure_dir_path(parent_path)

            if not parent:
                raise HTTPException(status_code=409, detail="Parent not found")

            # Check if file already exists (update vs create)
            existing = self._find_in_dir(parent.identity, filename)

            # Upload via storage engine
            meta = self.engine.chunk_data(data, name=filename, parent=parent.identity, user_id=_DAV_UID.get())

            # If existing, delete old one
            if existing and existing.identity != meta.identity:
                self.engine.delete_file(existing.identity, permanent=True, user_id=_DAV_UID.get())

            return FastResponse(
                content="",
                status_code=201,
                headers={"Content-Length": "0"}
            )

        @self.app.api_route(f"{p}/{{path:path}}", methods=["MKCOL"])
        async def dav_mkcol(path: str = ""):
            """Create a directory."""
            parts = [p for p in path.split("/") if p]
            if not parts:
                raise HTTPException(status_code=403, detail="Cannot MKCOL root")

            dirname = parts[-1]
            parent_path = "/".join(parts[:-1])
            parent = self._resolve_path(parent_path) or self._ensure_dir_path(parent_path)

            if not parent:
                raise HTTPException(status_code=409, detail="Parent not found")

            # Check if already exists
            existing = self._find_in_dir(parent.identity, dirname)
            if existing:
                raise HTTPException(status_code=405, detail="Already exists")

            self.engine.create_dir(dirname, parent.identity, user_id=_DAV_UID.get())
            return FastResponse(content="", status_code=201, headers={"Content-Length": "0"})

        @self.app.api_route(f"{p}/{{path:path}}", methods=["DELETE"])
        async def dav_delete(path: str = ""):
            """Delete a file or directory."""
            target = self._resolve_path(path)
            if not target:
                raise HTTPException(status_code=404, detail="Not found")
            self.engine.delete_file(target.identity, permanent=True, user_id=_DAV_UID.get())
            return FastResponse(content="", status_code=204, headers={"Content-Length": "0"})

        @self.app.api_route(f"{p}/{{path:path}}", methods=["MOVE"])
        async def dav_move(request: Request, path: str = ""):
            """Move/rename a file."""
            destination = request.headers.get("destination", "")
            # Extract path from full URL
            if "://" in destination:
                from urllib.parse import urlparse
                parsed = urlparse(destination)
                dest_path = parsed.path
                # Remove prefix
                prefix = self.prefix
                if dest_path.startswith(prefix):
                    dest_path = dest_path[len(prefix)+1:]
            else:
                dest_path = destination.lstrip("/")

            target = self._resolve_path(path)
            if not target:
                raise HTTPException(status_code=404, detail="Source not found")

            parts = [p for p in dest_path.split("/") if p]
            if not parts:
                raise HTTPException(status_code=403, detail="Invalid destination")

            new_name = parts[-1]
            new_parent_path = "/".join(parts[:-1])
            new_parent = self._resolve_path(new_parent_path) or self._ensure_dir_path(new_parent_path)

            if not new_parent:
                raise HTTPException(status_code=409, detail="Destination parent not found")

            # Update metadata
            target.name = new_name
            target.parent = new_parent.identity
            target.path = "/" + "/".join(parts)
            target.update_ts = int(time.time())
            self.engine._save_meta(target)
            self.engine._db_insert_file(target)

            return FastResponse(content="", status_code=201, headers={"Content-Length": "0"})

        @self.app.api_route(f"{p}/{{path:path}}", methods=["COPY"])
        async def dav_copy(request: Request, path: str = ""):
            """Copy a file."""
            destination = request.headers.get("destination", "")
            from urllib.parse import urlparse
            if "://" in destination:
                parsed = urlparse(destination)
                dest_path = parsed.path
                prefix = self.prefix
                if dest_path.startswith(prefix):
                    dest_path = dest_path[len(prefix)+1:]
            else:
                dest_path = destination.lstrip("/")

            target = self._resolve_path(path)
            if not target:
                raise HTTPException(status_code=404, detail="Source not found")

            # Download and re-upload
            data = b""
            for _, _, chunk_data in self.engine.stream_chunks(target.identity, user_id=_DAV_UID.get()):
                data += chunk_data

            parts = [p for p in dest_path.split("/") if p]
            new_name = parts[-1] if parts else target.name
            new_parent_path = "/".join(parts[:-1])
            new_parent = self._resolve_path(new_parent_path) or self._ensure_dir_path(new_parent_path)

            self.engine.chunk_data(data, name=new_name, parent=new_parent.identity if new_parent else "root", user_id=_DAV_UID.get())

            return FastResponse(content="", status_code=201, headers={"Content-Length": "0"})

        # Root path handler
        @self.app.api_route(f"{p}", methods=["OPTIONS", "PROPFIND", "GET"])
        async def dav_root(request: Request):
            if request.method == "OPTIONS":
                return FastResponse(content="", headers={
                    "DAV": "1, 2", "MS-Author-Via": "DAV",
                    "Allow": "OPTIONS, PROPFIND, GET, PUT, MKCOL, DELETE, MOVE, COPY",
                })
            elif request.method == "PROPFIND":
                children = self.engine.list_files("root", user_id=_DAV_UID.get())
                root_meta = FileMeta(
                    identity="root", parent="", name="root", path="/",
                    size=0, is_dir=True, create_ts=0, update_ts=0
                )
                items = [root_meta] + children
                xml = self._build_multistatus(items, "")
                return FastResponse(content=xml, status_code=207,
                                    media_type="application/xml; charset=utf-8", headers={"DAV": "1, 2"})
            else:
                children = self.engine.list_files("root", user_id=_DAV_UID.get())
                html = self._directory_listing_html("", children)
                return FastResponse(content=html, media_type="text/html")

    def _resolve_path(self, path: str) -> Optional[FileMeta]:
        """Resolve a WebDAV path to a FileMeta."""
        path = path.strip("/")
        if not path:
            # Root
            root = FileMeta(identity="root", parent="", name="root", path="/",
                          size=0, is_dir=True, create_ts=0, update_ts=0)
            return root

        parts = [p for p in path.split("/") if p]
        current_parent = "root"
        for i, part in enumerate(parts):
            found = self._find_in_dir(current_parent, part)
            if not found:
                return None
            if i == len(parts) - 1:
                return found
            if not found.is_dir:
                return None
            current_parent = found.identity
        return None

    def _find_in_dir(self, parent_id: str, name: str) -> Optional[FileMeta]:
        """Find a file by name in a directory."""
        children = self.engine.list_files(parent_id, user_id=_DAV_UID.get())
        for child in children:
            if child.name == name:
                return child
        return None

    def _ensure_dir_path(self, path: str) -> Optional[FileMeta]:
        """Ensure a directory path exists, creating intermediate dirs."""
        path = path.strip("/")
        if not path:
            return FileMeta(identity="root", parent="", name="root", path="/",
                          size=0, is_dir=True, create_ts=0, update_ts=0)
        parts = [p for p in path.split("/") if p]
        current_parent = "root"
        for part in parts:
            found = self._find_in_dir(current_parent, part)
            if found:
                if not found.is_dir:
                    return None
                current_parent = found.identity
            else:
                meta = self.engine.create_dir(part, current_parent, user_id=_DAV_UID.get())
                current_parent = meta.identity
        return self._find_in_dir("root", parts[0]) if parts else None

    def _build_multistatus(self, items: List[FileMeta], base_path: str) -> str:
        """Build a WebDAV multistatus XML response."""
        ns = "DAV:"
        root = ET.Element(f"{{{ns}}}multistatus")

        for item in items:
            response = ET.SubElement(root, f"{{{ns}}}response")

            # Build href
            if item.identity == "root":
                href = f"{self.prefix}/"
            else:
                # Find the path relative to base
                href_path = base_path.rstrip("/")
                if href_path:
                    href = f"{self.prefix}/{href_path}/{item.name}"
                else:
                    href = f"{self.prefix}/{item.name}"
                if item.is_dir:
                    href += "/"

            ET.SubElement(response, f"{{{ns}}}href").text = href

            propstat = ET.SubElement(response, f"{{{ns}}}propstat")
            prop = ET.SubElement(propstat, f"{{{ns}}}prop")

            # Displayname
            ET.SubElement(prop, f"{{{ns}}}displayname").text = item.name

            # Content length
            resourcetype = ET.SubElement(prop, f"{{{ns}}}resourcetype")
            if item.is_dir:
                ET.SubElement(resourcetype, f"{{{ns}}}collection")
            ET.SubElement(prop, f"{{{ns}}}getcontentlength").text = str(item.size)

            # Content type
            ET.SubElement(prop, f"{{{ns}}}getcontenttype").text = item.mime_type if not item.is_dir else "httpd/unix-directory"

            # Last modified (RFC 2822 format). v6 timestamps are milliseconds,
            # formatdate() needs seconds — feeding ms overflows localtime.
            ts = item.update_ts or int(time.time())
            if ts > 1e11:
                ts = ts / 1000
            from email.utils import formatdate
            ET.SubElement(prop, f"{{{ns}}}getlastmodified").text = formatdate(ts)

            # ETag (use content_identity if available)
            etag = item.content_identity or item.identity
            ET.SubElement(prop, f"{{{ns}}}getetag").text = f'"{etag}"'

            # Status
            ET.SubElement(propstat, f"{{{ns}}}status").text = "HTTP/1.1 200 OK"

        # Pretty print
        ET.indent(root, space="  ")
        xml_str = '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")
        return xml_str

    def _directory_listing_html(self, path: str, children: List[FileMeta]) -> str:
        """Build a simple HTML directory listing for browser access."""
        items_html = ""
        for child in children:
            icon = "📁" if child.is_dir else "📄"
            size = "-" if child.is_dir else f"{child.size:,} bytes"
            href = f"{self.prefix}/{path.rstrip('/')}/{child.name}".replace("//", "/")
            if child.is_dir:
                href += "/"
            items_html += f'<li>{icon} <a href="{href}">{child.name}</a> <span style="color:#888;font-size:12px">({size})</span></li>'

        return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>MyDrive WebDAV</title></head>
        <body style="font-family:monospace;padding:20px">
        <h2>MyDrive WebDAV — /{path}</h2>
        <ul>{items_html}</ul>
        <hr><p style="color:#888;font-size:12px">Mount: net use Z: http://127.0.0.1:8765{self.prefix}</p>
        </body></html>"""


if __name__ == "__main__":
    """Quick test: start a standalone WebDAV server."""
    from fastapi import FastAPI
    import uvicorn

    engine = StorageEngine("./storage_data")
    app = FastAPI()
    gateway = WebDAVGateway(app, engine)

    # Create a test file
    engine.chunk_data(b"Hello from WebDAV!", name="webdav_test.txt", parent="root")
    engine.create_dir("documents", "root")

    print(f"\nWebDAV server on http://127.0.0.1:8765/webdav/")
    print(f"Mount: net use Z: http://127.0.0.1:8765/webdav")
    uvicorn.run(app, host="127.0.0.1", port=8765)
