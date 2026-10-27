# taosync-csenc

> **本仓库是 [dr34m-cn/taosync](https://github.com/dr34m-cn/taosync) 的修改版（fork），不是官方项目。**  
> 上游许可证：**[AGPL-3.0](./LICENSE)**。修改日期：2026-10。  
> 维护者 GitHub：[@yudeyu9](https://github.com/yudeyu9)

[English](./README.md) | **简体中文**

## 这个 fork 做什么

面向群晖等 NAS 场景：在大文件经 OpenList/网盘同步时，作为对「超时后无限重试」类问题的替代方案之一；并增加与群晖 Cloud Sync 兼容的文件加密能力（`service/crypto/csenc.py`）。

相对上游，本仓库侧重：

- 群晖 Docker 部署（`deploy/synology/`）
- Cloud Sync 兼容加密同步（密码 + 可选 RSA）
- 本地构建镜像，不依赖官方 `dr34m/tao-sync` 镜像

程序核心仍基于上游 TaoSync：内置存储引擎，并兼容外部 OpenList/AList v3+。

仓库地址：https://github.com/yudeyu9/taosync-csenc

## 加密同步流程（OpenList）

作业开启加密后，**不再走** OpenList 远端 `fs/copy`，改为本机处理密文再交给 OpenList 异步上传，避免大文件在远端复制链路里超时死循环重试。

大致步骤：

```text
1. 下载   从源路径把明文拉到本机临时文件
2. 加密   用群晖 Cloud Sync 兼容格式加密为密文临时文件（密码写入 enc_key1，可选 RSA 公钥写入 enc_key2）
3. 上传   把密文 put 到 OpenList 目标路径（As-Task 异步上传）
4. 轮询   拿到 upload 任务 id 后，轮询 OpenList 任务状态，直到成功 / 失败 / 取消
```

进度大致划分（单文件）：

| 阶段 | 进度区间 | 说明 |
| --- | --- | --- |
| 下载明文 | 0% → 40% | 本机从源存储拉取 |
| 加密 | 40% → 70% | `csenc` 流式压缩 + AES 等，写出密文 |
| 提交上传 | 70% → 85% | 密文交给 OpenList |
| 轮询上传 | 85% → 100% | 跟踪 OpenList→网盘的 upload 任务 |

临时明文 / 密文文件在单文件结束后会清理。内置引擎路径的加密上传逻辑类似（本机加密后再写入目标挂载），但不经过 OpenList 的 upload 任务轮询。

相关代码：`service/crypto/csenc.py`、`service/alist/alistClient.py`（`_copyFileEncrypted`）、`service/syncJob/jobClient.py`。

## 合规与归属

- 保留完整 [LICENSE](./LICENSE)（GNU Affero General Public License v3.0）
- 使用、修改、再分发（含 Docker 镜像）须遵守 AGPL-3.0
- 公开镜像时请同时提供与本仓库一致的完整对应源码
- UI / 文档中的「TaoSync」名称来自上游；本仓库发布名建议使用 **taosync-csenc**，以免与官方混淆

上游项目：[GitHub](https://github.com/dr34m-cn/taosync) · [Gitee](https://gitee.com/dr34m/taosync)

## 须知

> [!WARNING]
> **不要将本服务直接暴露到公网。** 如必须外网访问，请使用强密码并配置 SSL/反向代理。

> [!IMPORTANT]
> 默认提供不可删除的内置引擎；也可另行添加 OpenList/AList 作为外部引擎。

## 群晖部署（推荐）

数据与程序分离，更新程序时不要删除 `data`。

详细步骤见：[deploy/synology/README.md](./deploy/synology/README.md)

简要流程：

1. 本机构建前端：`npm run build --prefix web`
2. 在群晖上按文档准备 `data/` 与程序目录
3. 构建并启动本地镜像（默认名 `taosync-csenc:local`）

```sh
# 在仓库根目录（需已有 web/dist）
docker build -f deploy/synology/Dockerfile -t taosync-csenc:local .
```

Compose 示例见 `deploy/synology/docker-compose.yml`。

- 端口：`8023`
- 数据卷：`/app/data`
- 默认用户：`admin`；初始密码见容器日志或 `data/log/`

## 本机快速运行（Docker）

```sh
docker build -f deploy/synology/Dockerfile -t taosync-csenc:local .
docker run -d --restart=unless-stopped -p 8023:8023 \
  -v /opt/taosync-data:/app/data \
  --name taoSync taosync-csenc:local
```

把 `/opt/taosync-data` 换成你的数据目录。访问 `http://127.0.0.1:8023`。

与 OpenList 同网部署时，在引擎里填写 Compose 服务名即可，例如 `http://openlist:5244`。

## 配置

优先级：`data/config.ini` > 环境变量 > 默认值。前一档存在时，后档会被忽略。修改后需重启。

| 配置项 | 环境变量 | 说明 | 默认 |
| --- | --- | --- | --- |
| password | TAO_PASSWORD | 仅首次建库生效；`RANDOM` 或空则随机 | RANDOM |
| port | TAO_PORT | 监听端口 | 8023 |
| expires | TAO_EXPIRES | 登录有效期（天） | 2 |
| log_level | TAO_LOG_LEVEL | 0–4，越大日志越少 | 1 |
| console_level | TAO_CONSOLE_LEVEL | 控制台日志等级 | 2 |
| log_save | TAO_LOG_SAVE | 系统日志保留天数；0 不清理 | 7 |
| task_save | TAO_TASK_SAVE | 任务记录保留天数；0 不清理 | 0 |
| task_timeout | TAO_TASK_TIMEOUT | 任务超时（小时），大文件请调大 | 72 |
| - | TZ | 时区 | Asia/Shanghai |

完整功能说明仍可参考上游 README；本 fork 以群晖部署与加密扩展为主。

## 源码与发行

- 本仓库：https://github.com/yudeyu9/taosync-csenc
- 上游：https://github.com/dr34m-cn/taosync
- Docker Hub：若后续发布公共镜像，将在此 README 补充镜像名，并与本仓库源码对应

## 免责声明

本修改版按「现状」提供，不保证适用于你的全部业务场景。数据安全与备份策略由使用者自行负责。
