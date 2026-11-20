"""
P2P Signaling Server — WebRTC P2P 分块下载信令
================================================
清真云用 gRPC + WebRTC 做 P2P 文件分块传输。我们用 WebSocket 做信令服务器。

核心思路：
1. 每个 peer（客户端）连接到信令服务器（WebSocket）
2. 客户端声明自己有哪些 chunk CID（可以提供给他人下载）
3. 当客户端 A 需要某个 chunk 时，向信令服务器查询谁有
4. 信令服务器返回有该 chunk 的 peer 列表
5. A 通过 WebRTC DataChannel 直接从 B 拉取 chunk（不经服务器）
6. 信令服务器只做中介：交换 SDP/ICE，不传输实际数据

好处：
- 减轻服务器带宽压力
- 多源并行下载（从多个 peer 同时拉不同 chunk）
- 去中心化，类似 IPFS 的内容路由

API 端点：
- GET  /api/p2p/peers          — 列出在线 peer
- GET  /api/p2p/peers/{cid}    — 查询哪些 peer 有某个 chunk
- POST /api/p2p/announce       — peer 声明自己拥有的 chunk
- WS   /ws/p2p                 — WebSocket 信令通道（SDP/ICE 交换）
"""

import os
import json
import time
import asyncio
from typing import Dict, Set, Optional
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from storage_engine import StorageEngine


class PeerInfo:
    """Track a connected peer."""
    def __init__(self, peer_id: str, ws: WebSocket):
        self.peer_id = peer_id
        self.ws = ws
        self.connected_ts = int(time.time())
        self.last_seen = self.connected_ts
        self.chunks: Set[str] = set()  # CIDs this peer has
        self.name = ""
        self.user_agent = ""

    def to_dict(self) -> dict:
        return {
            "peer_id": self.peer_id,
            "connected_ts": self.connected_ts,
            "last_seen": self.last_seen,
            "chunk_count": len(self.chunks),
            "name": self.name,
        }


class P2PSignalingServer:
    """Manages peer registry and WebRTC signaling."""

    def __init__(self, engine: StorageEngine):
        self.engine = engine
        self.peers: Dict[str, PeerInfo] = {}
        # Reverse index: cid -> set of peer_ids
        self.cid_to_peers: Dict[str, Set[str]] = {}
        self.lock = asyncio.Lock()

    async def register_peer(self, peer_id: str, ws: WebSocket, name: str = ""):
        async with self.lock:
            peer = PeerInfo(peer_id, ws)
            peer.name = name
            self.peers[peer_id] = peer

    async def unregister_peer(self, peer_id: str):
        async with self.lock:
            peer = self.peers.pop(peer_id, None)
            if peer:
                # Remove from CID index
                for cid in peer.chunks:
                    if cid in self.cid_to_peers:
                        self.cid_to_peers[cid].discard(peer_id)
                        if not self.cid_to_peers[cid]:
                            del self.cid_to_peers[cid]

    async def announce_chunks(self, peer_id: str, cids: list):
        """Peer declares which chunks it has available for P2P upload."""
        async with self.lock:
            peer = self.peers.get(peer_id)
            if not peer:
                return
            for cid in cids:
                peer.chunks.add(cid)
                if cid not in self.cid_to_peers:
                    self.cid_to_peers[cid] = set()
                self.cid_to_peers[cid].add(peer_id)
            peer.last_seen = int(time.time())

    def get_peers_for_cid(self, cid: str) -> list:
        """Find which peers have a specific chunk."""
        peer_ids = self.cid_to_peers.get(cid, set())
        return [self.peers[pid].to_dict() for pid in peer_ids if pid in self.peers]

    def list_peers(self) -> list:
        return [p.to_dict() for p in self.peers.values()]

    async def relay_signal(self, from_id: str, to_id: str, signal_type: str, data: dict):
        """Relay WebRTC signaling (SDP offer/answer, ICE candidates) between peers."""
        target = self.peers.get(to_id)
        if target and target.ws:
            await target.ws.send_text(json.dumps({
                "type": "signal",
                "signal_type": signal_type,
                "from": from_id,
                "data": data,
            }))

    async def handle_websocket(self, ws: WebSocket):
        """Main WebSocket handler for P2P signaling."""
        await ws.accept()
        peer_id = f"peer_{id(ws)}_{int(time.time())}"

        await self.register_peer(peer_id, ws)
        await ws.send_text(json.dumps({"type": "registered", "peer_id": peer_id}))

        try:
            while True:
                raw = await ws.receive_text()
                msg = json.loads(raw)
                msg_type = msg.get("type", "")

                if msg_type == "announce":
                    # Peer declares its chunks
                    cids = msg.get("cids", [])
                    await self.announce_chunks(peer_id, cids)
                    await ws.send_text(json.dumps({
                        "type": "announce_ok",
                        "chunk_count": len(self.peers[peer_id].chunks),
                    }))

                elif msg_type == "query_chunk":
                    # Peer asks who has a chunk
                    cid = msg.get("cid", "")
                    peers = self.get_peers_for_cid(cid)
                    await ws.send_text(json.dumps({
                        "type": "chunk_peers",
                        "cid": cid,
                        "peers": peers,
                    }))

                elif msg_type == "signal":
                    # Relay WebRTC signaling to target peer
                    to_id = msg.get("to", "")
                    signal_type = msg.get("signal_type", "")
                    data = msg.get("data", {})
                    await self.relay_signal(peer_id, to_id, signal_type, data)

                elif msg_type == "heartbeat":
                    # Keep-alive
                    if peer_id in self.peers:
                        self.peers[peer_id].last_seen = int(time.time())
                    await ws.send_text(json.dumps({"type": "heartbeat_ok"}))

                elif msg_type == "list_peers":
                    await ws.send_text(json.dumps({
                        "type": "peer_list",
                        "peers": self.list_peers(),
                    }))

        except WebSocketDisconnect:
            await self.unregister_peer(peer_id)
        except Exception:
            await self.unregister_peer(peer_id)
