# taosync-csenc

> **This repository is a modified fork of [dr34m-cn/taosync](https://github.com/dr34m-cn/taosync), not the official project.**  
> Upstream license: **[AGPL-3.0](./LICENSE)**. Modified: 2026-10.  
> Maintainer: [@yudeyu9](https://github.com/yudeyu9)

**English** | [简体中文](./README_ZH.md)

## What this fork is for

Built for Synology / NAS workflows: an alternative path when large-file sync through OpenList keeps timing out and retrying endlessly. It also adds Synology Cloud Sync–compatible file encryption (`service/crypto/csenc.py`).

Compared with upstream, this repo focuses on:

- Synology Docker deploy (`deploy/synology/`)
- Cloud Sync–compatible encrypted sync (password + optional RSA)
- Building a local image instead of pulling official `dr34m/tao-sync`

Core behavior still comes from upstream TaoSync: a built-in storage engine plus optional external OpenList/AList v3+.

Repository: https://github.com/yudeyu9/taosync-csenc

## Encrypted sync flow (OpenList)

When a job enables encryption, TaoSync **does not** use OpenList remote `fs/copy`. It encrypts locally, then asks OpenList to upload asynchronously—so large files are less likely to get stuck in remote copy timeout/retry loops.

Pipeline:

```text
1. Download  Pull plaintext from the source into a local temp file
2. Encrypt   Write Synology Cloud Sync–compatible ciphertext (password → enc_key1; optional RSA public key → enc_key2)
3. Upload    PUT ciphertext to the OpenList destination (async As-Task upload)
4. Poll      Poll the OpenList upload task until success / failure / cancel
```

Approximate progress for one file:

| Stage | Range | Notes |
| --- | --- | --- |
| Download | 0% → 40% | Local pull from source |
| Encrypt | 40% → 70% | `csenc` stream compress + AES, etc. |
| Submit upload | 70% → 85% | Hand ciphertext to OpenList |
| Poll upload | 85% → 100% | Track OpenList → cloud upload task |

Temp plaintext/ciphertext files are removed after each file. Built-in engine encryption is similar (encrypt locally, then write to the target mount) but does not poll an OpenList upload task.

Code: `service/crypto/csenc.py`, `service/alist/alistClient.py` (`_copyFileEncrypted`), `service/syncJob/jobClient.py`.

## License and attribution

- Keep the full [LICENSE](./LICENSE) (GNU Affero General Public License v3.0)
- Use, modification, and redistribution (including Docker images) must follow AGPL-3.0
- If you publish an image, publish the matching complete Corresponding Source as well
- UI / docs may still say “TaoSync” (upstream name); prefer **taosync-csenc** for public naming so it is not confused with the official project

Upstream: [GitHub](https://github.com/dr34m-cn/taosync) · [Gitee](https://gitee.com/dr34m/taosync)

## Important

> [!WARNING]
> **Do not expose this service directly to the public internet.** If remote access is required, use a strong password and SSL / a reverse proxy.

> [!IMPORTANT]
> A built-in engine is created by default and cannot be removed. You can still add an external OpenList/AList engine when needed.

## Synology deploy (recommended)

Keep program files and `data` separate; never delete `data` when updating the app.

See: [deploy/synology/README.md](./deploy/synology/README.md)

Outline:

1. Build the frontend locally: `npm run build --prefix web`
2. Prepare `data/` and the app directory on the NAS as documented
3. Build and run the local image (default tag `taosync-csenc:local`)

```sh
# From the repo root (requires web/dist)
docker build -t taosync-csenc:local .
```

Compose sample: `deploy/synology/docker-compose.yml`.

- Port: `8023`
- Volume: `/app/data`
- Default user: `admin`; initial password is in container logs or `data/log/`

## Quick start (Docker)

```sh
docker build -t taosync-csenc:local .
docker run -d --restart=unless-stopped -p 8023:8023 \
  -v /opt/taosync-data:/app/data \
  --name taoSync taosync-csenc:local
```

Replace `/opt/taosync-data` with your data directory. Open `http://127.0.0.1:8023`.

When OpenList runs in the same Compose network, use the service hostname as the engine URL, e.g. `http://openlist:5244`.

## Configuration

Precedence: `data/config.ini` > environment variables > defaults. If a higher source exists, lower ones are ignored. Restart after changes.

| config.ini | Environment | Description | Default |
| --- | --- | --- | --- |
| password | TAO_PASSWORD | Only on first DB create; `RANDOM` or empty = random | RANDOM |
| port | TAO_PORT | Listen port | 8023 |
| expires | TAO_EXPIRES | Session lifetime (days) | 2 |
| log_level | TAO_LOG_LEVEL | 0–4; higher = fewer logs | 1 |
| console_level | TAO_CONSOLE_LEVEL | Console log level | 2 |
| log_save | TAO_LOG_SAVE | System log retention (days); 0 = keep all | 7 |
| task_save | TAO_TASK_SAVE | Task record retention (days); 0 = keep all | 0 |
| task_timeout | TAO_TASK_TIMEOUT | Task timeout (hours); raise for large files | 72 |
| - | TZ | Time zone | Asia/Shanghai |

For the full feature set, see the upstream README. This fork emphasizes Synology deploy and the encryption extension.

## Source and releases

- This repo: https://github.com/yudeyu9/taosync-csenc
- Upstream: https://github.com/dr34m-cn/taosync
- Docker Hub: https://hub.docker.com/r/yudy95/taosync-csenc (`yudy95/taosync-csenc:latest`, matches this source tree)

Pull and run:

```sh
docker pull yudy95/taosync-csenc:latest
docker run -d --restart=unless-stopped -p 8023:8023 \
  -v /opt/taosync-data:/app/data \
  --name taoSync yudy95/taosync-csenc:latest
```

## Disclaimer

Provided as-is. You are responsible for data safety and backup strategy in your environment.
