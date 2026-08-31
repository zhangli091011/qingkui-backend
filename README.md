# 青葵计划后端

FastAPI 单体服务，面向 Android MVP。当前实现账户、知识图谱、分场景问答、学习记录、额度、反馈、错题 OCR、私有对象上传和审核队列闭环。

问答检索采用“知识图谱节点 + 授权文档片段”的混合上下文。文档可使用阿里百炼 `text-embedding-v4` 向量化，并由 `gte-rerank-v2` 重排；百炼暂时不可用时自动回退到本地关键词检索。

知识库支持语文、数学、英语、物理、化学、生物、政治、历史、地理九个学科。用户不需要选择学科：服务会先用本地规则识别问题，再只在对应学科的节点、文档和向量分区内检索；无法可靠识别时不注入知识库上下文，避免跨学科误召回。会话响应中的 `subject` 表示最近一次自动识别结果。

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

## 文档知识库与阿里百炼

仅导入已经取得授权、自有或公版的 `.txt`、`.md`、`.pdf`、`.docx`、`.pptx` 文件。导入按 SHA-256 去重，并记录来源、授权状态、解析状态和分块位置。PPTX 会直接提取每页可见文本，不依赖 Office；旧版 `.doc`、视频和其他二进制文件需先转换后再导入。

```powershell
# 未配置百炼时先完成文本入库，检索自动使用关键词模式
python -m app.cli import-documents D:\knowledge --authorization-status self_owned

# 也可以在导入时显式覆盖自动识别结果
python -m app.cli import-documents D:\knowledge --authorization-status self_owned --subject 物理

# 导入“高中数学必修第一册（人教A版）”精选可解析资料：知识清单 + 2025 同步讲义教师版
python -m app.cli import-curated-textbook "E:\BaiduNetdiskDownload\高中数学必修第一册（人教A版）"

# 采集中文维基教科书的高中数学核心主题（CC BY-SA 4.0，首次约 30 个页面）
python -m app.cli import-wikibooks --pages-per-topic 6

# 启用阿里百炼后，为已有文档补齐向量
$env:RETRIEVAL_PROVIDER="bailian"
$env:DASHSCOPE_API_KEY="在服务端配置，不要写入仓库"
python -m app.cli reindex-documents

# 已有向量只生成本地检索索引，不调用百炼
python -m app.cli build-vector-index

# 发布前审计首发范围、授权、公式审核和图谱关系
python -m app.cli content-governance-report --output content-governance-report.json

# 从首发范围授权文档生成最多 600 个未发布审核候选
python -m app.cli materialize-launch-candidates --limit 600

# 终端交互式流式问答测试（不扣额度、不写入会话）
python -m app.cli qa-console --mode knowledge --help-level approach
```

进入 `qa-console` 后，直接输入问题即可看到流式输出，并显示语义学科、置信度和检索数量；输入 `/help` 查看命令，`/mode problem` 切换场景，`/help full` 切换帮助级别，`/subject 数学` 固定学科，`/subject auto` 恢复自动分类，`/sources` 查看最近一轮检索片段，`/clear` 清屏，`/quit` 退出。也可以用 `--question "随机变量的期望怎么求"` 执行单次体验。

生产环境在 `.env` 中配置：

```text
RETRIEVAL_PROVIDER=bailian
DASHSCOPE_API_KEY=...
DASHSCOPE_EMBEDDING_MODEL=text-embedding-v4
DASHSCOPE_EMBEDDING_DIMENSION=1024
DASHSCOPE_RERANK_MODEL=gte-rerank-v2
```

如果使用带 `/compatible-mode/v1` 的 OpenAI 兼容端点，应改为：

```text
DASHSCOPE_BASE_URL=https://your-endpoint/compatible-mode/v1
DASHSCOPE_API_MODE=openai_compatible
DASHSCOPE_EMBEDDING_MODEL=qwen3.7-text-embedding
DASHSCOPE_RERANK_ENABLED=false
DEEPSEEK_BASE_URL=https://your-endpoint/compatible-mode/v1
DEEPSEEK_MODEL=deepseek-v4-flash
```

兼容端点未提供 `/rerank` 时关闭远程重排，系统会按向量相似度和关键词分数返回结果，不会中断问答。

同一文件重复导入会被跳过。向量服务失败不会阻断文本入库或问答，文档保持 `text_ready`，稍后可再次运行 `reindex-documents`。

### 文本与公式自动分段

导入时会将普通段落与显式公式分别写入 `knowledge_chunks`。支持 `$...$`、`$$...$$`、`\\(...\\)` 和 `\\[...\\]`：普通段为 `content_type=text`，公式段为 `content_type=formula`，并保存 `formula_latex` 与公式来源。两类片段都会参与检索，Android 可按 `formula_latex` 使用数学渲染组件显示。

扫描图片或 PDF 页面中的公式需要经过 OCR，结果应先保留为待复核公式，再由用户或内容管理员确认后进入正式知识库。

- 健康检查：`http://127.0.0.1:8000/health`
- OpenAPI：`http://127.0.0.1:8000/docs`
- Android 模拟器基地址：`http://10.0.2.2:8000/api/`
- 生产 API：`https://qingkui-api.82-158-229-157.sslip.io/api/`
- C9 真机使用开发机局域网 IP，并允许明文 HTTP 仅用于本地调试；测试与生产必须使用 HTTPS。

开发环境会自动创建 SQLite 表并导入 6 个高一数学演示节点。演示知识源标记为 `self_owned_demo`，不能当作正式教材内容发布。

## 关键接口

| 功能 | 接口 |
|---|---|
| 注册、登录、刷新 | `POST /api/auth/register`、`/login`、`/refresh` |
| 搜索、详情、邻接节点 | `GET /api/knowledge/search`、`/nodes/{id}`、`/neighbors` |
| 学科目录 | `GET /api/knowledge/subjects` |
| 创建会话、发送问题 | `POST /api/qa/sessions`、`/sessions/{id}/messages` |
| SSE 流式问答 | `POST /api/qa/sessions/{id}/messages/stream` |
| 学习事件与状态 | `POST /api/learning/events`、`PATCH /nodes/{id}/state` |
| 额度余额与流水 | `GET /api/credits`、`/ledger` |
| 错误反馈 | `POST /api/feedback` |

### 管理后台

浏览器打开 `http://127.0.0.1:8000/admin` 可使用最小管理页面。先在页面粘贴管理员登录得到的 access token，再执行节点发布、下线、版本恢复、章节和关系维护、反馈审核、额度调整、模型成本与审计日志查询。对应 API 均位于 `/api/admin/*`，并强制要求 `admin` 或 `content_admin` 角色。

| 管理能力 | 接口 |
|---|---|
| 节点/章节 | `GET /api/admin/knowledge/nodes`、`/chapters` |
| 内容治理报告 | `GET /api/admin/knowledge/governance` |
| 统一发布就绪报告 | `GET /api/admin/release-readiness` |
| 关系 CRUD | `GET/POST/PATCH/DELETE /api/admin/knowledge/edges` |
| 版本历史与状态 | `GET .../versions`、`POST .../publish`、`/withdraw`、`/restore` |
| 反馈审核 | `GET/PATCH /api/admin/feedback` |
| 额度调整 | `POST /api/admin/credits/{user_id}/adjust` |
| 成本与审计 | `GET /api/admin/model-costs`、`/audit-logs` |

V2 学校、班级、邀请码、逐人试点准入和匿名教师概览已具备隔离模型与接口，但默认由 `ORGANIZATIONS_ENABLED=false` 关闭。试点或生产启用时必须同时设置 `PILOT_AUTHORIZATION_ENFORCED=true`，详见 `docs/ORGANIZATIONS.md`。

所有业务接口使用 `Authorization: Bearer <access_token>`。DeepSeek 密钥只保存在服务端环境变量中。普通问答消耗 1 额度，完整解析消耗 2 额度；供应商失败时数据库事务回滚，不扣额度。

正式 AI 质量门使用人工复核评测，而不是关键词命中率。运行与评分命令、字段定义和首发数学阈值见 `docs/AI_EVALUATION.md`；种子集位于 `config/evaluation/math-v1.json`。

发布前使用 `python -m app.cli release-readiness-report --strict` 汇总配置、迁移、内容、人评、恢复演练、正式签名 APK 和 C9 真机证据。证据目录、模板和交叉校验规则见 `docs/RELEASE_READINESS.md`。

## 数据库与部署

本地默认 SQLite；生产使用 PostgreSQL：

```text
DATABASE_URL=postgresql+psycopg://user:password@host:5432/qingkui
```

`compose.yaml` 同时准备 PostgreSQL、Redis 和 MinIO。Redis/MinIO 当前不在同步问答路径中，后续用于 OCR 任务队列和对象上传。

生产部署前必须在服务器 `.env` 中设置随机的 `JWT_SECRET`、`POSTGRES_PASSWORD`、`MINIO_ROOT_PASSWORD`、服务端 `DEEPSEEK_API_KEY`；启用百炼时再设置 `DASHSCOPE_API_KEY`。`.dockerignore` 会阻止这些环境文件进入容器镜像。

创建首个管理员：

```powershell
python -m app.cli create-admin --username admin
```

运行测试：

```powershell
pytest
```
