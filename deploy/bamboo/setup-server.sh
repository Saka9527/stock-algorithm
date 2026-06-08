#!/usr/bin/env bash
# 首次在目标服务器初始化 Docker 网络与部署目录（仅需执行一次）
# 用法: DEPLOY_HOST=x.x.x.x DEPLOY_USER=root ./deploy/bamboo/setup-server.sh

set -euo pipefail

DEPLOY_HOST="${DEPLOY_HOST:?set DEPLOY_HOST}"
DEPLOY_USER="${DEPLOY_USER:-root}"
DEPLOY_PATH="${DEPLOY_PATH:-/opt/stock-algorithm}"
NETWORK="${DOCKER_NETWORK:-stock-net}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)

if [[ -n "${DEPLOY_SSH_KEY:-}" ]]; then
  SSH_OPTS+=(-i "${DEPLOY_SSH_KEY}")
fi

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

echo ">>> init server ${DEPLOY_USER}@${DEPLOY_HOST}"

ssh "${SSH_OPTS[@]}" "${DEPLOY_USER}@${DEPLOY_HOST}" bash -s <<EOF
set -euo pipefail
docker network inspect "${NETWORK}" >/dev/null 2>&1 || docker network create "${NETWORK}"
mkdir -p "${DEPLOY_PATH}/deploy/docker"
EOF

scp "${SSH_OPTS[@]}" -r \
  "${ROOT_DIR}/deploy/docker/docker-compose.yml" \
  "${ROOT_DIR}/deploy/docker/.env.example" \
  "${DEPLOY_USER}@${DEPLOY_HOST}:${DEPLOY_PATH}/deploy/docker/"

ssh "${SSH_OPTS[@]}" "${DEPLOY_USER}@${DEPLOY_HOST}" bash -s <<EOF
set -euo pipefail
cd "${DEPLOY_PATH}/deploy/docker"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "请编辑 ${DEPLOY_PATH}/deploy/docker/.env 填写数据库/Redis 密码后重新部署"
fi
EOF

echo ">>> 将现有容器接入同一网络（按需执行）:"
echo "    docker network connect ${NETWORK} stock-data-api"
echo "    docker network connect ${NETWORK} data-collect-backend"
echo "    docker network connect ${NETWORK} <blade-mcp-container>"
