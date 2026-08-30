# 灾难恢复与演练

## 恢复边界

- PostgreSQL 是账户、知识元数据、分块、向量数据、图谱和学习记录的事实来源。
- 私有 OSS 保存原始文档、不可变向量索引版本和迁移清单。
- API 只读取经过 SHA-256 校验的本地索引缓存；生产请求不会在查询路径下载 OSS。
- 恢复演练必须使用隔离 PostgreSQL 容器和临时索引目录，禁止覆盖生产卷或 `/data/qingkui-vectors.npz`。

## 目标

| 数据 | RPO | RTO | 验证证据 |
|---|---:|---:|---|
| PostgreSQL | 24 小时，试点发布前额外备份 | 2 小时 | gzip 校验、隔离恢复、迁移版本及核心表计数 |
| OSS 原始文档 | 对象上传成功即持久化 | 4 小时 | 对象 HEAD、大小与 SHA-256 全量审计 |
| 向量索引 | 每次发布一个不可变版本 | 30 分钟 | 当前和上一版本临时下载与 SHA-256 校验 |

## 创建备份

```bash
stamp=$(date -u +%Y%m%dT%H%M%SZ)
docker compose -p qingkui exec -T postgres \
  pg_dump -U qingkui -d qingkui | gzip -1 \
  > "/opt/qingkui/backups/qingkui-${stamp}.sql.gz"
gzip -t "/opt/qingkui/backups/qingkui-${stamp}.sql.gz"
sha256sum "/opt/qingkui/backups/qingkui-${stamp}.sql.gz" \
  > "/opt/qingkui/backups/qingkui-${stamp}.sql.gz.sha256"
```

备份文件和校验文件应复制到与服务器故障域隔离的私有存储。只放在同一系统盘不构成灾难恢复备份。

## 隔离恢复演练

在 `backend` 目录执行：

```bash
chmod 750 deploy/recovery-drill.sh
./deploy/recovery-drill.sh --full-oss \
  /opt/qingkui/backups/qingkui-20260830T022319Z.sql.gz \
  /opt/qingkui/recovery-reports/20260830.json
```

脚本会：

1. 校验 gzip 和备份 SHA-256。
2. 创建一次性 PostgreSQL 17 容器及临时卷并完整恢复。
3. 查询 Alembic 版本与用户、文档、分块、节点、消息数量。
4. 把 OSS 当前和上一版索引下载到 `/tmp`，分别校验 SHA-256。
5. 可选执行 4,000+ 原始文档的 OSS 全量对象审计。
6. 删除一次性容器、卷和索引，只保留 JSON 报告。

通过条件：脚本退出码为 0，报告 `status=passed`、`production_data_modified=false`，数据库计数符合预期，两个索引摘要非空，全量 OSS 审计无缺失或校验错误。

## 真正恢复

真正恢复属于事故操作，必须由负责人明确批准恢复点：

1. 冻结写请求，记录当前数据库和索引摘要。
2. 新建 PostgreSQL 卷，不直接覆盖故障卷；先恢复并执行完整验证。
3. 将恢复库升级到目标 Alembic 版本并运行后端测试/健康检查。
4. 严格同步指定 OSS 索引；若当前索引异常，使用 `--version previous`。
5. 切换 API 数据库连接和只读索引挂载，验证登录、检索、SSE、额度和管理后台。
6. 保留原故障卷，直到事件复盘和数据差异核对完成。

## 周期与保留

- 每日数据库备份，至少保留 7 个日备份和 4 个周备份。
- 每次数据库迁移、试点包发布和内容批量发布前创建额外恢复点。
- 每月至少一次隔离恢复，每季度一次包含 `--full-oss` 的完整演练。
- OSS 当前和上一版索引必须保留；更老版本按发布记录和成本策略归档。
- 恢复报告不得包含密码、AccessKey、题目正文或用户身份信息。
