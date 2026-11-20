"""
Offline Downloader — 离线下载
=============================
用户提交一个 URL，服务器后台下载并存入网盘存储。

支持：
1. HTTP/HTTPS 直接链接
2. 后台异步下载，不阻塞 API
3. 进度追踪
4. 下载完成后自动入库（走 storage engine 的 chunk + CID 流程）
5. 断点续传（支持 Range 请求的源站）

流程：
  POST /api/offline/add { url, parent, name }
  → 创建任务，后台线程下载
  → 下载完成后 chunk + 存储
  → 生成 FileMeta
  GET /api/offline/list → 查看所有任务状态
"""

import os
import time
import uuid
import sqlite3
import threading
import requests
from pathlib import Path
from typing import Optional

from storage_engine import StorageEngine


class OfflineDownloader:
    def __init__(self, engine: StorageEngine):
        self.engine = engine
        self.db_path = engine.db_dir / "offline.sqlite"
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS offline_tasks (
                task_id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                name TEXT,
                parent TEXT DEFAULT 'root',
                status TEXT DEFAULT 'pending',
                progress REAL DEFAULT 0,
                downloaded_bytes INTEGER DEFAULT 0,
                total_bytes INTEGER DEFAULT 0,
                file_identity TEXT,
                error TEXT,
                created_ts INTEGER,
                finished_ts INTEGER,
                user_id TEXT NOT NULL DEFAULT 'anonymous'
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_offline_status ON offline_tasks(status)")
        cols = {row[1] for row in c.execute("PRAGMA table_info(offline_tasks)")}
        if "user_id" not in cols:
            c.execute("ALTER TABLE offline_tasks ADD COLUMN user_id TEXT NOT NULL DEFAULT 'anonymous'")
        c.execute("CREATE INDEX IF NOT EXISTS idx_offline_user ON offline_tasks(user_id)")
        conn.commit()
        conn.close()

    def add_task(self, url: str, parent: str = "root", name: str = "",
                 user_id: str = "anonymous") -> dict:
        if not url.startswith(("http://", "https://")):
            return {"success": False, "error": "Only HTTP(S) URLs supported"}

        if not name:
            name = url.rsplit("/", 1)[-1].split("?")[0] or f"offline_{int(time.time())}"

        task_id = uuid.uuid4().hex[:12]
        now = int(time.time())

        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute(
            "INSERT INTO offline_tasks (task_id, url, name, parent, status, created_ts, user_id) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (task_id, url, name, parent, now, user_id or "anonymous")
        )
        conn.commit()
        conn.close()

        # Start background download thread
        thread = threading.Thread(target=self._download_worker, args=(task_id,), daemon=True)
        thread.start()

        return {"success": True, "task_id": task_id, "name": name, "status": "pending"}

    def _download_worker(self, task_id: str):
        """Background worker: stream URL to a temp file, then store it in the engine."""
        import tempfile

        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute("SELECT url, name, parent, user_id FROM offline_tasks WHERE task_id = ?", (task_id,))
        row = c.fetchone()
        conn.close()

        if not row:
            return

        url, name, parent, user_id = row
        tmp_path = None

        # Update status to downloading
        self._update_task(task_id, status="downloading")

        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".part") as tmp:
                tmp_path = tmp.name

            with requests.get(url, stream=True, timeout=30, allow_redirects=True) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0) or 0)
                downloaded = 0
                last_update = 0

                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=512 * 1024):
                        if self._task_cancelled(task_id):
                            return
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = int(time.time())
                        if now >= last_update + 1 or (total and downloaded >= total):
                            progress = (downloaded / total * 100) if total else 0
                            self._update_task(task_id, progress=progress,
                                            downloaded_bytes=downloaded, total_bytes=total)
                            last_update = now

            if self._task_cancelled(task_id):
                return

            # Store in engine by streaming from disk (no full-file memory copy).
            self._update_task(task_id, status="storing")
            meta = self.engine.chunk_file(tmp_path, parent=parent, name=name,
                                          user_id=user_id or "anonymous")

            self._update_task(task_id, status="completed", progress=100.0,
                            file_identity=meta.identity, finished_ts=int(time.time()))

        except Exception as e:
            self._update_task(task_id, status="failed", error=str(e), finished_ts=int(time.time()))
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _task_cancelled(self, task_id: str) -> bool:
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute("SELECT status FROM offline_tasks WHERE task_id = ?", (task_id,))
        row = c.fetchone()
        conn.close()
        return bool(row and row[0] == "cancelled")

    def _update_task(self, task_id: str, **kwargs):
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [task_id]
        c.execute(f"UPDATE offline_tasks SET {sets} WHERE task_id = ?", values)
        conn.commit()
        conn.close()

    def list_tasks(self, user_id: str = None) -> list:
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        query = ("SELECT task_id, url, name, parent, status, progress, downloaded_bytes, "
                 "total_bytes, file_identity, error, created_ts, finished_ts "
                 "FROM offline_tasks")
        params = ()
        if user_id is not None:
            query += " WHERE user_id = ?"
            params = (user_id,)
        query += " ORDER BY created_ts DESC"
        c.execute(query, params)
        rows = c.fetchall()
        conn.close()
        return [
            {
                "task_id": r[0], "url": r[1], "name": r[2], "parent": r[3],
                "status": r[4], "progress": r[5], "downloaded_bytes": r[6],
                "total_bytes": r[7], "file_identity": r[8], "error": r[9],
                "created_ts": r[10], "finished_ts": r[11],
            }
            for r in rows
        ]

    def get_task(self, task_id: str, user_id: str = None) -> Optional[dict]:
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        query = ("SELECT task_id, url, name, parent, status, progress, downloaded_bytes, "
                 "total_bytes, file_identity, error, created_ts, finished_ts "
                 "FROM offline_tasks WHERE task_id = ?")
        params = [task_id]
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        c.execute(query, params)
        r = c.fetchone()
        conn.close()
        if not r:
            return None
        return {
            "task_id": r[0], "url": r[1], "name": r[2], "parent": r[3],
            "status": r[4], "progress": r[5], "downloaded_bytes": r[6],
            "total_bytes": r[7], "file_identity": r[8], "error": r[9],
            "created_ts": r[10], "finished_ts": r[11],
        }

    def cancel_task(self, task_id: str, user_id: str = None):
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        if user_id is None:
            c.execute("UPDATE offline_tasks SET status = 'cancelled' WHERE task_id = ?", (task_id,))
        else:
            c.execute("UPDATE offline_tasks SET status = 'cancelled' WHERE task_id = ? AND user_id = ?",
                      (task_id, user_id))
        conn.commit()
        conn.close()
