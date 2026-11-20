"""
Download Manager — 断点续传 + 并行分块下载
==========================================
这是下载侧的核心难点：

1. 断点续传：大文件下载中断后，从上次断掉的分块继续，不重头来
2. 并行下载：多个分块同时拉取，提高吞吐
3. 校验拼装：每块下载后校验 CID，确认没被篡改
4. 进度追踪：实时上报下载进度

存储格式：
  downloads/
    <file_identity>.progress   — JSON 格式的进度文件，记录已完成的分块
    <file_identity>.part       — 临时文件，已下载的分块按序写入
    <file_identity>.final      — 拼装完成的文件（校验通过后重命名为最终文件名）
"""

import os
import json
import time
import hashlib
import asyncio
import aiohttp
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from cid_engine import compute_cid, parse_cid


@dataclass
class DownloadProgress:
    """Tracks which chunks of a file have been downloaded."""
    file_identity: str
    file_name: str
    file_size: int
    total_chunks: int
    completed_chunks: Dict[int, bool] = field(default_factory=dict)  # index -> done
    chunk_cids: List[str] = field(default_factory=list)
    chunk_sizes: List[int] = field(default_factory=list)
    download_url: str = ""          # base URL for chunk downloads
    started_ts: int = 0
    last_active_ts: int = 0

    @property
    def completed_count(self) -> int:
        return sum(1 for v in self.completed_chunks.values() if v)

    @property
    def completed_bytes(self) -> int:
        return sum(self.chunk_sizes[i] for i, done in self.completed_chunks.items() if done)

    @property
    def progress_percent(self) -> float:
        if self.total_chunks == 0:
            return 0.0
        return (self.completed_count / self.total_chunks) * 100.0

    @property
    def is_complete(self) -> bool:
        return self.completed_count == self.total_chunks

    def to_dict(self) -> dict:
        return {
            "file_identity": self.file_identity,
            "file_name": self.file_name,
            "file_size": self.file_size,
            "total_chunks": self.total_chunks,
            "completed_chunks": {str(k): v for k, v in self.completed_chunks.items()},
            "chunk_cids": self.chunk_cids,
            "chunk_sizes": self.chunk_sizes,
            "download_url": self.download_url,
            "started_ts": self.started_ts,
            "last_active_ts": self.last_active_ts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DownloadProgress":
        completed = {int(k): v for k, v in d.pop("completed_chunks", {}).items()}
        return cls(completed_chunks=completed, **d)


class DownloadManager:
    """
    Handles resumable, parallel chunk downloads.

    Usage:
        dm = DownloadManager("./downloads", "http://localhost:8765")

        # Start or resume a download
        progress = dm.start_download(file_identity, chunk_info)

        # The manager downloads chunks in parallel, writes to .part file,
        # and saves progress to .progress file. If interrupted, call
        # start_download again with same file_identity — it resumes.

        # When all chunks done, manager assembles final file and verifies.
    """

    def __init__(self, download_dir: str, api_base: str = "http://127.0.0.1:8765",
                 max_parallel: int = 4):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.api_base = api_base
        self.max_parallel = max_parallel

    def _progress_path(self, file_identity: str) -> Path:
        return self.download_dir / f"{file_identity}.progress"

    def _part_path(self, file_identity: str) -> Path:
        return self.download_dir / f"{file_identity}.part"

    def _final_path(self, file_identity: str, name: str) -> Path:
        return self.download_dir / name

    def load_progress(self, file_identity: str) -> Optional[DownloadProgress]:
        """Load existing download progress (for resume)."""
        p = self._progress_path(file_identity)
        if p.exists():
            data = json.loads(p.read_text())
            return DownloadProgress.from_dict(data)
        return None

    def save_progress(self, progress: DownloadProgress):
        """Persist download progress to disk."""
        progress.last_active_ts = int(time.time())
        p = self._progress_path(progress.file_identity)
        p.write_text(json.dumps(progress.to_dict(), indent=2))

    def start_download(
        self,
        file_identity: str,
        file_name: str,
        file_size: int,
        chunk_cids: List[str],
        chunk_sizes: List[int],
        on_progress: Optional[Callable[[DownloadProgress], None]] = None,
    ) -> DownloadProgress:
        """
        Start or resume a download.

        If a .progress file exists, resumes from where it left off.
        Downloads chunks in parallel, writes to .part file,
        assembles final file when complete.

        Args:
            file_identity: Server file ID
            file_name: Output filename
            file_size: Total file size
            chunk_cids: List of chunk CIDs
            chunk_sizes: List of chunk sizes (parallel to chunk_cids)
            on_progress: Callback called after each chunk completes

        Returns:
            DownloadProgress (final state)
        """
        # Try to resume
        progress = self.load_progress(file_identity)

        if progress and progress.is_complete:
            # Already done — just assemble
            print(f"  [resume] All chunks already downloaded, assembling...")
            self._assemble(progress)
            return progress

        if not progress:
            progress = DownloadProgress(
                file_identity=file_identity,
                file_name=file_name,
                file_size=file_size,
                total_chunks=len(chunk_cids),
                chunk_cids=chunk_cids,
                chunk_sizes=chunk_sizes,
                download_url=self.api_base,
                started_ts=int(time.time()),
            )
            # Mark all as not done
            for i in range(len(chunk_cids)):
                progress.completed_chunks[i] = False

        # Create part file if not exists (pre-allocate)
        part_path = self._part_path(file_identity)
        if not part_path.exists():
            # Pre-allocate the file to the full size
            with open(part_path, "wb") as f:
                f.truncate(file_size)

        # Find incomplete chunks
        pending = [
            i for i in range(progress.total_chunks)
            if not progress.completed_chunks.get(i, False)
        ]

        print(f"  [download] {file_name}: {len(pending)}/{progress.total_chunks} chunks remaining")
        print(f"  [download] Already have: {progress.completed_count} chunks ({progress.completed_bytes:,} bytes)")

        # Download in parallel
        with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
            futures = {}
            for idx in pending:
                future = pool.submit(
                    self._download_chunk_to_file,
                    progress, idx
                )
                futures[future] = idx

            for future in as_completed(futures):
                idx = futures[future]
                try:
                    success = future.result()
                    if success:
                        progress.completed_chunks[idx] = True
                        self.save_progress(progress)
                        if on_progress:
                            on_progress(progress)
                        pct = progress.progress_percent
                        done_bytes = progress.completed_bytes
                        print(f"  [chunk {idx}/{progress.total_chunks-1}] ✅ "
                              f"{progress.chunk_sizes[idx]:,} bytes | "
                              f"total: {pct:.0f}% ({done_bytes:,}/{file_size:,})")
                except Exception as e:
                    print(f"  [chunk {idx}] FAILED: {e}")

        # Assemble if complete
        if progress.is_complete:
            print(f"  [download] All chunks complete, assembling final file...")
            self._assemble(progress)
            print(f"  [download] ✅ {file_name} downloaded successfully")
        else:
            print(f"  [download] ⏸ Interrupted at {progress.progress_percent:.0f}%, "
                  f"can resume with same file_identity")

        return progress

    def _download_chunk_to_file(self, progress: DownloadProgress, chunk_index: int) -> bool:
        """
        Download a single chunk and write it at the correct offset in the .part file.
        Uses HTTP to fetch the chunk by CID, then verifies the CID.
        """
        cid = progress.chunk_cids[chunk_index]
        expected_size = progress.chunk_sizes[chunk_index]

        # Calculate offset in file
        offset = sum(progress.chunk_sizes[:chunk_index])

        # Fetch chunk from server
        url = f"{self.api_base}/api/chunk/{cid}"
        import requests
        resp = requests.get(url, timeout=120)
        if resp.status_code != 200:
            return False

        data = resp.content

        # Verify size
        if len(data) != expected_size:
            print(f"  [chunk {chunk_index}] size mismatch: got {len(data)}, expected {expected_size}")
            return False

        # Verify CID — this ensures the chunk hasn't been tampered with
        actual_cid = compute_cid(data)
        if actual_cid != cid:
            print(f"  [chunk {chunk_index}] CID mismatch! Expected {cid[:20]}..., got {actual_cid[:20]}...")
            return False

        # Write to correct offset in .part file
        part_path = self._part_path(progress.file_identity)
        with open(part_path, "r+b") as f:
            f.seek(offset)
            f.write(data)

        return True

    def _assemble(self, progress: DownloadProgress):
        """Rename .part to final filename and clean up progress file."""
        part_path = self._part_path(progress.file_identity)
        final_path = self._final_path(progress.file_identity, progress.file_name)

        # The .part file IS the complete file (we wrote chunks at correct offsets)
        os.rename(str(part_path), str(final_path))

        # Verify final file size
        actual_size = final_path.stat().st_size
        if actual_size != progress.file_size:
            print(f"  [assemble] WARNING: size mismatch {actual_size} vs {progress.file_size}")

        # Clean up progress file
        prog_path = self._progress_path(progress.file_identity)
        if prog_path.exists():
            prog_path.unlink()

    def cancel_download(self, file_identity: str):
        """Cancel a download and clean up partial files."""
        prog = self._progress_path(file_identity)
        part = self._part_path(file_identity)
        if prog.exists():
            prog.unlink()
        if part.exists():
            part.unlink()
        print(f"  [cancel] Cleaned up {file_identity}")

    def list_downloads(self) -> List[dict]:
        """List all download progress files."""
        results = []
        for p in self.download_dir.glob("*.progress"):
            data = json.loads(p.read_text())
            prog = DownloadProgress.from_dict(data)
            results.append({
                "file_identity": prog.file_identity,
                "file_name": prog.file_name,
                "progress": f"{prog.progress_percent:.1f}%",
                "completed": prog.completed_count,
                "total": prog.total_chunks,
                "bytes_done": prog.completed_bytes,
                "bytes_total": prog.file_size,
                "is_complete": prog.is_complete,
            })
        return results


if __name__ == "__main__":
    """
    Test: upload a large file to the API, then download it with
    interruption + resume to verify breakpoint continuation works.
    """
    import requests as req
    import os
    import tempfile

    API = "http://127.0.0.1:8765"
    DL_DIR = "./test_downloads"

    # First, make sure server is up
    try:
        r = req.get(f"{API}/api/stats", timeout=3)
        print(f"Server status: {r.json()}")
    except:
        print("Server not running! Start it with: python api_server.py")
        exit(1)

    # Create and upload a 10MB file
    print("\n=== Upload 10MB file ===")
    content = os.urandom(10 * 1024 * 1024)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        f.write(content)
        upload_file = f.name

    with open(upload_file, "rb") as f:
        resp = req.post(f"{API}/api/upload", files={"file": ("test_download.bin", f)})
    meta = resp.json()
    print(f"Uploaded: {meta['name']} id={meta['identity']} chunks={meta['chunks']}")

    # Get chunk list
    resp = req.get(f"{API}/api/files/{meta['identity']}/chunks")
    chunk_data = resp.json()
    cids = [c["cid"] for c in chunk_data["chunks"]]
    sizes = [c["size"] for c in chunk_data["chunks"]]

    # Test 1: Normal download
    print("\n=== Test 1: Normal parallel download ===")
    dm = DownloadManager(DL_DIR, API, max_parallel=4)

    progress = dm.start_download(
        file_identity=meta["identity"],
        file_name=meta["name"],
        file_size=meta["size"],
        chunk_cids=cids,
        chunk_sizes=sizes,
    )

    assert progress.is_complete, "Download should be complete!"
    final_path = Path(DL_DIR) / meta["name"]
    assert final_path.exists(), "Final file should exist!"

    # Verify content
    downloaded = final_path.read_bytes()
    assert downloaded == content, "Downloaded content should match!"
    print(f"\n✅ Downloaded file matches original ({len(downloaded):,} bytes)")

    # Test 2: Simulate interruption + resume
    print("\n=== Test 2: Simulate interruption + resume ===")

    # Upload another file
    content2 = os.urandom(8 * 1024 * 1024)  # 8MB
    with open(upload_file, "wb") as f:
        f.write(content2)
    with open(upload_file, "rb") as f:
        resp = req.post(f"{API}/api/upload", files={"file": ("resume_test.bin", f)})
    meta2 = resp.json()

    resp = req.get(f"{API}/api/files/{meta2['identity']}/chunks")
    chunk_data2 = resp.json()
    cids2 = [c["cid"] for c in chunk_data2["chunks"]]
    sizes2 = [c["size"] for c in chunk_data2["chunks"]]

    # Create a download manager that only does 1 chunk then "crashes"
    dm2 = DownloadManager(DL_DIR, API, max_parallel=1)

    # Manually download only the first chunk
    progress2 = DownloadProgress(
        file_identity=meta2["identity"],
        file_name=meta2["name"],
        file_size=meta2["size"],
        total_chunks=len(cids2),
        chunk_cids=cids2,
        chunk_sizes=sizes2,
        download_url=API,
        started_ts=int(time.time()),
    )
    for i in range(len(cids2)):
        progress2.completed_chunks[i] = False

    # Download only first chunk, then "interrupt"
    dm2.save_progress(progress2)
    part_path = dm2._part_path(meta2["identity"])
    with open(part_path, "wb") as f:
        f.truncate(meta2["size"])

    # Download chunk 0 only
    dm2._download_chunk_to_file(progress2, 0)
    progress2.completed_chunks[0] = True
    dm2.save_progress(progress2)
    print(f"  Simulated interruption after chunk 0/{len(cids2)-1}")

    # Now resume with a new manager
    print("\n  Resuming download...")
    dm3 = DownloadManager(DL_DIR, API, max_parallel=4)
    final_progress = dm3.start_download(
        file_identity=meta2["identity"],
        file_name=meta2["name"],
        file_size=meta2["size"],
        chunk_cids=cids2,
        chunk_sizes=sizes2,
    )

    assert final_progress.is_complete, "Resumed download should complete!"
    final_path2 = Path(DL_DIR) / meta2["name"]
    downloaded2 = final_path2.read_bytes()
    assert downloaded2 == content2, "Resumed download content should match!"
    print(f"\n✅ Resumed download matches original ({len(downloaded2):,} bytes)")

    # Test 3: List downloads
    print("\n=== Test 3: List downloads ===")
    for d in dm.list_downloads():
        print(f"  {d['file_name']}: {d['progress']} ({d['completed']}/{d['total']} chunks)")

    # Cleanup
    os.unlink(upload_file)
    print("\n✅ All download tests passed — breakpoint resume works!")
