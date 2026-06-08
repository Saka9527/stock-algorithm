#!/usr/bin/env bash
# Bamboo 部署任务：SSH 到目标服务器拉取镜像并重启 compose
# 必需变量: DEPLOY_HOST, DEPLOY_USER, IMAGE_TAG
# 可选: DEPLOY_SSH_KEY, DEPLOY_PATH, DOCKER_REGISTRY

set -euo pipefail

REGISTRY="${DOCKER_REGISTRY:-10.8.0.1:90}"
IMAGE_NAME="${IMAGE_NAME:-stock-service-api/stock-algorithm}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
DEPLOY_HOST="${DEPLOY_HOST:?set DEPLOY_HOST}"
DEPLOY_USER="${DEPLOY_USER:-root}"
DEPLOY_PATH="${DEPLOY_PATH:-/opt/stock-algorithm}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)

if [[ -n "${DEPLOY_SSH_KEY:-}" ]]; then
  SSH_OPTS+=(-i "${DEPLOY_SSH_KEY}")
fi

REMOTE="${DEPLOY_USER}@${DEPLOY_HOST}"
FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"

echo ">>> deploy ${FULL_IMAGE} -> ${REMOTE}:${DEPLOY_PATH}"

ssh "${SSH_OPTS[@]}" "${REMOTE}" bash -s <<EOF
set -euo pipefail
mkdir -p "${DEPLOY_PATH}/deploy/docker"
cd "${DEPLOY_PATH}/deploy/docker"

export DOCKER_REGISTRY="${REGISTRY}"
export IMAGE_TAG="${IMAGE_TAG}"
export BLADER_DB_PASSWORD="${BLADER_DB_PASSWORD:-}"
export REDIS_PASSWORD="${REDIS_PASSWORD:-}"
export MULTI_FACTOR_API_KEY="${MULTI_FACTOR_API_KEY:-}"

if [[ -n "\${DOCKER_USERNAME:-}" && -n "\${DOCKER_PASSWORD:-}" ]]; then
  echo "\${DOCKER_PASSWORD}" | docker login "${REGISTRY}" -u "\${DOCKER_USERNAME}" --password-stdin
fi

docker pull "${FULL_IMAGE}"
docker compose --env-file .env up -d --remove-orphans
docker compose ps
curl -fsS "http://127.0.0.1:\${HOST_PORT:-8032}/health/live" || true
EOF

echo ">>> deploy finished"
