# taosync-csenc 唯一 Dockerfile（群晖 / 本机共用，不依赖上游 dr34m/*）
# 构建前请先：npm run build --prefix web
#   docker build -t taosync-csenc:local .

FROM python:3.11-slim-bullseye AS builder
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends binutils \
    && rm -rf /var/lib/apt/lists/*

# 国内网络默认走清华源；可按环境改掉
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

COPY requirements.txt taoSync.spec main.py ./
COPY common ./common
COPY controller ./controller
COPY mapper ./mapper
COPY service ./service
COPY locales ./locales
COPY web/dist ./web/dist

RUN pip install --no-cache-dir -r requirements.txt pyinstaller \
    && pyinstaller taoSync.spec

FROM python:3.11-slim-bullseye
VOLUME /app/data
WORKDIR /app
COPY --from=builder /app/dist/taoSync /app/
ENV TAO_PORT=8023 \
    TAO_EXPIRES=2 \
    TAO_LOG_LEVEL=1 \
    TAO_CONSOLE_LEVEL=2 \
    TAO_LOG_SAVE=7 \
    TAO_TASK_SAVE=0 \
    TAO_TASK_TIMEOUT=72 \
    TZ=Asia/Shanghai
EXPOSE 8023
CMD ["./taoSync"]
