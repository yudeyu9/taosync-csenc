#!/bin/sh
# 在群晖上执行。支持两种布局：
#   A) 推荐：/volume1/docker/taosync/{data,docker-compose.yml,app/}
#      在 app 目录内执行本脚本，或在 taosync 根目录执行。
#   B) 旧布局：源码直接在 taosync/ 下，data 在 deploy/synology/data
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"

# 本脚本位于 .../app/deploy/synology 或 .../deploy/synology
APP_CANDIDATE="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"
PARENT="$(CDPATH= cd -- "$APP_CANDIDATE/.." && pwd)"

if [ -f "$PARENT/docker-compose.yml" ] && [ -d "$APP_CANDIDATE/web" ]; then
  # 推荐布局：compose 在父目录，代码在 app/
  HOST_ROOT="$PARENT"
  APP_ROOT="$APP_CANDIDATE"
  COMPOSE_FILE="$HOST_ROOT/docker-compose.yml"
elif [ -f "$APP_CANDIDATE/deploy/synology/docker-compose.yml" ]; then
  # 兼容：compose 仍在 deploy/synology，data 用父级 data 若存在
  APP_ROOT="$APP_CANDIDATE"
  HOST_ROOT="$APP_CANDIDATE"
  COMPOSE_FILE="$APP_CANDIDATE/deploy/synology/docker-compose.yml"
else
  echo "找不到 docker-compose.yml，请确认目录结构"
  exit 1
fi

cd "$APP_ROOT"
if [ ! -f web/dist/index.html ]; then
  echo "缺少 web/dist，请先在电脑上 npm run build 再上传到 app/"
  exit 1
fi

# 数据目录：优先用与 compose 同级的 data
DATA_DIR="$HOST_ROOT/data"
mkdir -p "$DATA_DIR"
echo "==> 数据目录（不会被本脚本删除）: $DATA_DIR"
echo "==> 程序目录: $APP_ROOT"

echo "==> 拉取基础镜像 python:3.11-slim-bullseye"
docker pull python:3.11-slim-bullseye || true

echo "==> 构建镜像 taosync-csenc:local"
# 使用仓库根目录 Dockerfile（已从 deploy/synology 迁出）
docker build -f Dockerfile -t taosync-csenc:local "$APP_ROOT"

echo "==> 重建容器（保留 $DATA_DIR）"
cd "$HOST_ROOT"
if [ -f "$COMPOSE_FILE" ]; then
  docker compose -f "$COMPOSE_FILE" up -d --force-recreate --build
else
  docker compose up -d --force-recreate --build
fi

echo "完成。访问 http://群晖IP:8023"
echo "  日志: docker logs taoSync"
echo "  数据: $DATA_DIR"
