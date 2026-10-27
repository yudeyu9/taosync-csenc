# 群晖部署（数据与程序分离）

本目录属于 [taosync-csenc](https://github.com/yudeyu9/taosync-csenc)（[dr34m-cn/taosync](https://github.com/dr34m-cn/taosync) 的 AGPL-3.0 修改版），用于本地构建镜像，**不要**拉取官方 `dr34m/tao-sync`。

每次改代码都可以 `docker build`，只要**不要动 data**。

## 推荐目录（固定）

```text
/volume1/docker/taosync/
  data/                  # 持久数据：库、密钥、日志 —— 永不删除、不覆盖
  docker-compose.yml     # 本仓库 deploy/synology/docker-compose.yml —— 建议只拷一次
  app/                   # 程序源码 —— 更新时可整目录删掉再复制新包
    main.py
    requirements.txt
    taoSync.spec
    common/
    controller/
    mapper/
    service/
    locales/
    web/dist/            # 电脑上先 npm run build
    Dockerfile           # 仓库根目录 Dockerfile
    deploy/synology/build-on-nas.sh
```

## 首次安装

1. 建目录：`mkdir -p /volume1/docker/taosync/data /volume1/docker/taosync/app`
2. 把 `deploy/synology/docker-compose.yml` 拷到 `/volume1/docker/taosync/docker-compose.yml`
3. 把源码（含 `web/dist`）解压进 `/volume1/docker/taosync/app/`
4. 构建启动：

```sh
cd /volume1/docker/taosync/app
sh deploy/synology/build-on-nas.sh
```

## 以后更新（可删程序目录）

```sh
# 1. 停容器（可选）
cd /volume1/docker/taosync
docker compose stop

# 2. 只删程序，不动 data、不动 compose
rm -rf app
mkdir app
# 把新包解压到 app/

# 3. 重新构建并启动（data 仍挂载 ./data）
cd /volume1/docker/taosync/app
sh deploy/synology/build-on-nas.sh
# 或：
# cd /volume1/docker/taosync && docker compose up -d --force-recreate --build
```

**禁止：** `rm -rf /volume1/docker/taosync`（会连 data 一起删）  
**可以：** `rm -rf /volume1/docker/taosync/app`

## 电脑打包清单

先 `npm run build --prefix web`，再打包进 `app/`：

- `main.py`、`requirements.txt`、`taoSync.spec`
- `common/`、`controller/`、`mapper/`、`service/`、`locales/`
- `web/dist/`
- 根目录 `Dockerfile`（及可选 `deploy/synology/build-on-nas.sh`）

不要打包：`.venv`、`web/node_modules`、`data/`

## 说明

- 镜像名：`taosync-csenc:local`（不要用上游 `dr34m/tao-sync`）
- 端口：`8023`
- 默认账号：`admin`；首次密码见 `docker logs taoSync` 或 `data/log/`
