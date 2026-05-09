#!/usr/bin/env bash
# Run ON THE ECS (SSH root). Fixes docker.io / PyPI / Debian mirror timeouts for compose.prod build.
# Usage:
#   cd ~/agentflow-platform && bash scripts/apply-china-ecs-network-fixes.sh
#   PROJECT_ROOT=/opt/agentflow bash scripts/apply-china-ecs-network-fixes.sh   # if repo not under /root/agentflow-platform
set -euo pipefail

# Default project path on ECS (override: PROJECT_ROOT=/path bash this-script.sh)
ROOT="${PROJECT_ROOT:-$HOME/agentflow-platform}"
cd "$ROOT"

echo "== 1) Redis image: use AWS public ECR (not docker.io) =="
sed -i 's|image: redis:7-alpine|image: public.ecr.aws/docker/library/redis:7-alpine|g' compose.prod.yaml

echo "== 2) Rewrite Dockerfile.api / Dockerfile.frontend (ECR base + Aliyun pip + apt mirror) =="
cat > Dockerfile.api << 'EOF'
# Use AWS public mirror of Docker Official Image (avoids docker.io timeouts on some networks).
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com

WORKDIR /app

# Use Aliyun Debian mirror during build (avoid deb.debian.org timeouts on some ECS networks).
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's|deb.debian.org|mirrors.aliyun.com|g;s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
      sed -i 's|deb.debian.org|mirrors.aliyun.com|g;s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list; \
    fi; \
    apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY main.py .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
EOF

cat > Dockerfile.frontend << 'EOF'
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY frontend ./frontend

EXPOSE 8501
CMD ["streamlit", "run", "frontend/app.py", "--server.address=0.0.0.0", "--server.port=8501"]
EOF

echo "== 3) Pre-pull base images (optional but helps) =="
docker pull public.ecr.aws/docker/library/python:3.12-slim || true
docker pull public.ecr.aws/docker/library/redis:7-alpine || true

echo "== 4) Check pgvector image exists (from docker load or prior pull) =="
if ! docker image inspect pgvector/pgvector:pg16 >/dev/null 2>&1; then
  echo "[!!] 本机没有 pgvector/pgvector:pg16。请先: docker load -i /root/pgvector-pg16.tar"
  exit 1
fi

echo "== 5) Build and start =="
docker compose -f compose.prod.yaml build --no-cache
docker compose -f compose.prod.yaml up -d

echo "== 6) Quick checks =="
docker compose -f compose.prod.yaml ps
curl -sfS http://127.0.0.1:8000/healthz && echo "" || echo "[!!] healthz failed"

echo "OK (if healthz prints JSON above). Next: nginx + certbot."
