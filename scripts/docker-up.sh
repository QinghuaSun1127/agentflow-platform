#!/usr/bin/env bash
# 在仓库根目录拉起 Postgres+Redis；需 Docker Desktop 对本 WSL 发行版开启 Integration。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! docker info >/dev/null 2>&1; then
  echo "错误: 当前 WSL 里无法访问 Docker 引擎。" >&2
  if groups | grep -qw docker; then
    echo "  你已在 docker 组：尝试 newgrp docker 或 wsl --shutdown 后重开终端。" >&2
  else
    echo "  常见原因：用户不在 docker 组（permission denied 连 docker.sock）。" >&2
    echo "  在本仓库执行一次: bash scripts/fix-docker-permissions.sh" >&2
  fi
  echo "  其它: 启动 Docker Desktop；Settings → WSL integration → 勾选本发行版。" >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  exec docker compose -f "$ROOT/compose.yaml" up -d "$@"
fi
if command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose -f "$ROOT/compose.yaml" up -d "$@"
fi

echo "错误: 未找到「docker compose」或 docker-compose，请更新 Docker Desktop。" >&2
exit 1
