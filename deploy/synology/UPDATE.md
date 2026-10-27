# 若你之前把 data 放在 deploy/synology/data

旧路径常见为：

```text
/volume1/docker/taosync/deploy/synology/data
```

迁移到推荐布局（只做一次）：

```sh
mkdir -p /volume1/docker/taosync/data
# 把旧数据挪过去（不要复制错成空目录覆盖）
mv /volume1/docker/taosync/deploy/synology/data/* /volume1/docker/taosync/data/
# 再按 README 放好 docker-compose.yml 与 app/
```

确认 `data` 里有数据库/日志后再删旧目录。

