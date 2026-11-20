#!/usr/bin/env bash
# MyDrive — 一键启动（HTTP API + SFTP + Slices 边缘缓存 + aria2 离线引擎）
set -e
cd "$(dirname "$0")/mydrive"

export PORT="${PORT:-8765}"
export STORAGE_ROOT="${STORAGE_ROOT:-./storage_data}"
export SFTP_PORT="${SFTP_PORT:-2022}"
export SLICES_PORT="${SLICES_PORT:-8766}"
export ARIA2_PORT="${ARIA2_PORT:-6800}"
# 服务端签名密钥：本地 lab 有默认值，真实部署请通过环境变量覆盖
export MYDRIVE_APP_SECRET="${MYDRIVE_APP_SECRET:-dev-only-insecure-secret}"
PYTHON="${PYTHON:-python3}"

echo "============================================================"
echo "  MyDrive - IPFS 内容寻址网盘"
echo "  Web/API : http://127.0.0.1:${PORT}"
echo "  SFTP    : sftp://127.0.0.1:${SFTP_PORT}"
echo "  Slices  : http://127.0.0.1:${SLICES_PORT}/ipfs/blocks/..."
echo "  aria2   : 127.0.0.1:${ARIA2_PORT} (BT/HTTP 离线下载)"
echo "  CDN     : /api/cdn/{stats,prewarm,purge}"
echo "============================================================"

exec "$PYTHON" api_server.py
