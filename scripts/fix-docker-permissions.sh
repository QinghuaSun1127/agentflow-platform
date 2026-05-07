#!/usr/bin/env bash
# 将当前用户加入 docker 组，解决 unix:///var/run/docker.sock permission denied
# 在仓库根或任意目录执行: bash scripts/fix-docker-permissions.sh
set -euo pipefail

echo "=== Docker 权限自检 ==="
echo "用户: $(whoami) (uid=$(id -u))"
echo "当前组: $(groups)"
if ls -la /var/run/docker.sock 2>/dev/null; then
  :
else
  echo "未找到 /var/run/docker.sock — 请先启动 Docker Desktop 或 Docker 服务。" >&2
  exit 1
fi

if groups | grep -qw docker; then
  echo "你已在 docker 组中。若仍报 permission denied，请执行: newgrp docker"
  echo "或关闭 WSL 后在 Windows 中运行: wsl --shutdown"
  exit 0
fi

echo ""
echo "将把用户「$(whoami)」加入系统组「docker」（需输入 sudo 密码）..."
if ! getent group docker >/dev/null 2>&1; then
  sudo groupadd docker
fi
sudo usermod -aG docker "$(whoami)"

echo ""
echo "=== 已完成 ==="
echo "组变更需新登录会话才生效，请任选其一："
echo "  1) 关闭本终端，新开一个 WSL 终端；或"
echo "  2) 在 Windows PowerShell 执行: wsl --shutdown  然后重新打开 Ubuntu；或"
echo "  3) 当前终端临时生效: newgrp docker"
echo ""
echo "验证: groups | grep docker  &&  docker info"
