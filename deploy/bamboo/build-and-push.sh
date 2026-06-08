#!/usr/bin/env bash
# Bamboo 构建任务：编译镜像并推送到私有仓库
# 必需变量: DOCKER_REGISTRY, IMAGE_NAME, IMAGE_TAG
# 可选: DOCKER_USERNAME, DOCKER_PASSWORD（仓库需登录时）

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"

REGISTRY="${DOCKER_REGISTRY:-10.8.0.1:90}"
IMAGE_NAME="${IMAGE_NAME:-stock-service-api/stock-algorithm}"
IMAGE_TAG="${IMAGE_TAG:-v${bamboo_buildNumber:-local}}"
FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"

echo ">>> build ${FULL_IMAGE}"
docker build \
  -f deploy/docker/Dockerfile \
  -t "${FULL_IMAGE}" \
  -t "${REGISTRY}/${IMAGE_NAME}:latest" \
  .

if [[ -n "${DOCKER_USERNAME:-}" && -n "${DOCKER_PASSWORD:-}" ]]; then
  echo ">>> docker login ${REGISTRY}"
  echo "${DOCKER_PASSWORD}" | docker login "${REGISTRY}" -u "${DOCKER_USERNAME}" --password-stdin
fi

echo ">>> push ${FULL_IMAGE}"
docker push "${FULL_IMAGE}"
docker push "${REGISTRY}/${IMAGE_NAME}:latest"

echo "BAMBOO_IMAGE=${FULL_IMAGE}" > "${bamboo_build_working_directory:-.}/image.properties"
echo ">>> done: ${FULL_IMAGE}"
