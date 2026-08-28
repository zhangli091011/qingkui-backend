# 青葵计划后端

FastAPI 单体服务，面向 Android MVP。当前实现账户、知识图谱、分场景问答、学习记录、额度和反馈闭环；OCR、对象上传和审核队列留到 V1.1。

## 本地启动

```powershell
uv venv
.\.venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"
Copy-Item .env.example .env.local
# 在 .env.local 中设置 DEEPSEEK_API_KEY 和一个随机 JWT_SECRET
alembic upgrade head
python -m app.seed_cli
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- 健康检查：`http://127.0.0.1:8000/health`
- OpenAPI：`http://127.0.0.1:8000/docs`
- Android 模拟器基地址：`http://10.0.2.2:8000/api/`
- C9 真机使用开发机局域网 IP，并允许明文 HTTP 仅用于本地调试；测试与生产必须使用 HTTPS。

开发环境会自动创建 SQLite 表并导入 6 个高一数学演示节点。演示知识源标记为 `self_owned_demo`，不能当作正式教材内容发布。

## 关键接口

| 功能 | 接口 |
|---|---|
| 注册、登录、刷新 | `POST /api/auth/register`、`/login`、`/refresh` |
| 搜索、详情、邻接节点 | `GET /api/knowledge/search`、`/nodes/{id}`、`/neighbors` |
| 创建会话、发送问题 | `POST /api/qa/sessions`、`/sessions/{id}/messages` |
| 学习事件与状态 | `POST /api/learning/events`、`PATCH /nodes/{id}/state` |
| 额度余额与流水 | `GET /api/credits`、`/ledger` |
| 错误反馈 | `POST /api/feedback` |

所有业务接口使用 `Authorization: Bearer <access_token>`。DeepSeek 密钥只保存在服务端环境变量中。普通问答消耗 1 额度，完整解析消耗 2 额度；供应商失败时数据库事务回滚，不扣额度。

## 数据库与部署

本地默认 SQLite；生产使用 PostgreSQL：

```text
DATABASE_URL=postgresql+psycopg://user:password@host:5432/qingkui
```

`compose.yaml` 同时准备 PostgreSQL、Redis 和 MinIO。Redis/MinIO 当前不在同步问答路径中，后续用于 OCR 任务队列和对象上传。

生产部署前必须在服务器 `.env` 中设置随机的 `JWT_SECRET`、`POSTGRES_PASSWORD`、`MINIO_ROOT_PASSWORD` 和服务端 `DEEPSEEK_API_KEY`。`.dockerignore` 会阻止这些环境文件进入容器镜像。

创建首个管理员：

```powershell
python -m app.cli create-admin --username admin
```

运行测试：

```powershell
pytest
```
