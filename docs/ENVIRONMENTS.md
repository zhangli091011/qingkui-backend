# 环境与配置管理

青葵使用 `development`、`test`、`pilot`、`production` 四套隔离环境。模板只保存非秘密默认值；密码、JWT、模型密钥和 OSS 凭据必须来自 CI/服务器秘密存储。

| 环境 | 数据库 | AI/检索 | 网络 | 用途 |
|---|---|---|---|---|
| development | 本地 SQLite | stub/lexical 默认 | localhost 或模拟器 `10.0.2.2` | 日常开发 |
| test | 独立临时 SQLite/PostgreSQL | stub/lexical | CI 内部 | 单测、迁移、契约检查 |
| pilot | 独立 PostgreSQL 库 | 真实模型，独立额度 | 独立 HTTPS 域名 | 5-10 人受控试点 |
| production | PostgreSQL 主库 | 真实模型 | HTTPS only | 经试点批准后的正式环境 |

## 规则

1. `.env`、`.env.local`、签名文件和任何真实密钥不得提交。
2. 试点与生产不得共用数据库、JWT secret、管理员账号或模型额度预算。
3. Android Release 构建必须使用 HTTPS；明文 HTTP 只允许 Debug manifest。
4. 配置变化必须先在 test 通过 CI，再进入 pilot；production 只接受已验证的同一 Git commit。
5. 每次部署记录 Git SHA、数据库 revision、APK versionCode、向量索引 SHA-256 和回滚目标。

试点与生产默认启用内容发布范围门：首发只允许 `数学 / 高一 / 人教A版`。改变范围必须先生成内容治理报告，并完成来源授权、公式复核和关系完整性审核。
