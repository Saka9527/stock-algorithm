# stock-algorithm Docker 部署与 Bamboo 构建

## 架构

```
Bamboo Agent                    目标服务器 (Docker)
    | build-and-push.sh  -->  10.8.0.1:90/stock-service-api/stock-algorithm:tag
    | deploy-remote.sh   -->  docker compose up (8032:8000)
                                    |
                    stock-net (external network)
                    /      |         \
    stock-algorithm-api  stock-data-api  blade-mcp ...
```

| 服务 | 容器名 | 宿主机端口 | 容器内端口 | 说明 |
|------|--------|-----------|-----------|------|
| 本 API | `stock-algorithm-api` | **8032** | 8000 | 多因子引擎 FastAPI |
| 已有 | `data-collect-backend` | 8031 | 8000 | 参考 |
| 已有 | `stock-data-api` | 8001 | 8001 | 参考 |

## 1. 服务器首次初始化

```bash
export DEPLOY_HOST=<服务器IP>
export DEPLOY_USER=root
./deploy/bamboo/setup-server.sh
```

编辑服务器 `/opt/stock-algorithm/deploy/docker/.env`，填写 `BLADER_DB_PASSWORD`、`REDIS_PASSWORD` 等。

### 容器互通（同一 Docker 网络）

```bash
# 创建共享网络（setup-server 已执行可跳过）
docker network create stock-net

# 将已有容器接入（容器名以 docker ps 为准）
docker network connect stock-net stock-data-api
docker network connect stock-net data-collect-backend
```

**容器间调用本 API**（同网段）：

```
http://stock-algorithm-api:8000/api/v1/...
```

**宿主机 / 外部调用**：

```
http://<服务器IP>:8032/api/v1/...
http://<服务器IP>:8032/docs
```

可选请求头：`X-API-Key: <MULTI_FACTOR_API_KEY>`

## 2. 本地手动构建验证

```bash
docker build -f deploy/docker/Dockerfile -t stock-algorithm:local .

docker run --rm -p 8032:8000 \
  -e BLADER_DB_PASSWORD=*** \
  -e REDIS_PASSWORD=*** \
  stock-algorithm:local

curl http://127.0.0.1:8032/health/live
```

## 3. Bamboo Plan 配置

### 构建阶段（Agent 需安装 Docker）

| 变量 | 示例 | 说明 |
|------|------|------|
| `DOCKER_REGISTRY` | `10.8.0.1:90` | 私有仓库 |
| `IMAGE_NAME` | `stock-service-api/stock-algorithm` | 镜像路径 |
| `IMAGE_TAG` | `v${bamboo.buildNumber}` | 版本标签 |
| `DOCKER_USERNAME` / `DOCKER_PASSWORD` | Secret | 仓库登录 |

**Script Task:**

```bash
chmod +x deploy/bamboo/build-and-push.sh
export IMAGE_TAG="v${bamboo.buildNumber}"
./deploy/bamboo/build-and-push.sh
```

### 部署阶段

| 变量 | 说明 |
|------|------|
| `DEPLOY_HOST` | 服务器 IP |
| `DEPLOY_USER` | SSH 用户 |
| `DEPLOY_PATH` | `/opt/stock-algorithm` |
| `BLADER_DB_PASSWORD` | Secret |
| `REDIS_PASSWORD` | Secret |
| `MULTI_FACTOR_API_KEY` | Secret（可选） |

**Script Task:**

```bash
chmod +x deploy/bamboo/deploy-remote.sh
export IMAGE_TAG="v${bamboo.buildNumber}"
./deploy/bamboo/deploy-remote.sh
```

也可直接导入 `deploy/bamboo/bamboo-specs.yaml`（按贵司 Bamboo 版本调整）。

## 4. 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BLADER_DB_HOST` | `117.25.133.3` | MySQL |
| `BLADER_DB_USER` | `stock-dev` | |
| `BLADER_DB_PASSWORD` | 必填 | |
| `REDIS_HOST` | `117.25.133.3` | |
| `REDIS_PASSWORD` | | |
| `HOST_PORT` | `8032` | 宿主机映射端口 |
| `DOCKER_NETWORK` | `stock-net` | 互通网络名 |

Parquet 归档与输出目录通过 Docker Volume 持久化：`stock-algorithm-data`、`stock-algorithm-output`。

## 5. 凌晨流水线

部署后可通过 API 触发（无需单独脚本）：

```bash
curl -X POST "http://stock-algorithm-api:8000/api/v1/pipeline/nightly/run" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MULTI_FACTOR_API_KEY" \
  -d '{"skip_backtest_warmup": true}'
```
