# APK、MDM、升级与紧急回滚

## 签名

- 正式包使用独立 PKCS12/JKS 上传密钥，至少 RSA 3072，密码存入离线密码库和 CI secret。
- keystore 不进入 Git、镜像、服务器日志或 MDM 公共目录；保留两份加密离线备份。
- `versionCode` 每次发布严格递增；同一 applicationId 不得更换签名。
- Debug 包不得进入学生 MDM 正式应用目录。

所有代码、内容和外部验收完成后，必须执行 `docs/RELEASE_READINESS.md` 的严格报告。`release-readiness-report --strict` 返回非零时不得扩大 MDM 投放。

## 发布通道

1. CI 生成未签名/测试 APK并运行测试。
2. 负责人从批准 commit 构建签名 APK，记录 SHA-256、versionCode、变更记录。
3. 先投放内部设备组，再投放 5-10 人试点组；禁止默认全校推送。
4. MDM 使用分阶段比例、安装窗口和强制升级开关；重大升级先允许回退。

进入未成年人试点前还必须完成 `docs/PILOT_PRIVACY.md` 中的告知/授权、负责人、退出联系人和数据清理安排；应用内登录不能替代这些手续。

## 兼容与升级

- 数据库先做可恢复备份，再执行向后兼容迁移；旧 APK 至少保留一个发布周期的 API 兼容。
- 新隐私版本发布时先保持 `PRIVACY_CONSENT_ENFORCED=false`，由新版客户端主动引导同意；确认旧 APK 已完成升级后，再设置为 `true` 并重建 API 容器。
- 服务端部署采用“新镜像健康检查通过后切流”，不得在原容器内手工改代码。
- Compose 的 `api` 健康检查必须进入 `healthy` 后才能切流；检查覆盖迁移完成后的 `/health`，停止容器保留 30 秒处理在途请求。
- 向量索引按 SHA-256 版本化，本地只读挂载；OSS 保存当前和上一个可用版本。
- 发布前必须执行 `verify-oss-storage` 和严格模式 `sync-vector-index-from-oss`；具体流程见 `docs/OSS_STORAGE.md`。

## 紧急回滚

1. 停止扩大 MDM 发布并冻结新版本。
2. API 回滚到上一批准镜像；若迁移不兼容，先进入只读维护页，再按演练过的恢复点还原 PostgreSQL。
3. 恢复上一向量索引软链接/挂载并执行健康检查和一条受控问答。
   OSS 架构下执行 `sync-vector-index-from-oss --version previous`，校验成功后重建 API 容器。
4. MDM 重新发布上一签名 APK；由于 Android 不允许降 versionCode，回滚包使用相同代码但更高 versionCode。
5. 记录影响范围、时间线、数据修复和防复发项。

完整的隔离恢复演练、RPO/RTO 和通过条件见 `docs/DISASTER_RECOVERY.md`。每次数据库迁移进入生产前，必须存在最近 30 天内成功的隔离恢复报告。

## HTTPS 证书

- 生产域名为 `qingkui-api.82-158-229-157.sslip.io`，证书由 Certbot webroot 模式签发。
- ACME 目录固定为 `/www/wwwroot/qingkui-api-acme`，续期钩子安装在 `/etc/letsencrypt/renewal-hooks/deploy/`。
- 每次 Nginx 配置变更先执行 `nginx -t`；续期钩子也会先校验配置再重载。
- 发布验收必须覆盖 HTTP 301、TLS 证书、`/health`、`/docs`、OpenAPI 和一条真实 SSE 问答。
- `/health` 必须满足 `status=ok`；试点/生产同时报告 `release_config_ready`，检查隐私同意、首发内容范围、限流、模型、检索和私有 OSS 配置。仅隐私版本灰度升级窗口可暂时保留 `privacy_consent_not_enforced`，且必须按本手册“兼容与升级”完成旧 APK 升级后关闭该例外；其余配置问题会使健康状态降级并阻止切流。

## 浏览器跨域

- 原生 Android 不依赖 CORS，生产 `CORS_ORIGINS` 默认留空；同源 `/admin` 仍可使用 API。
- 仅在部署独立浏览器前端时填写其完整 HTTPS Origin，多个值使用逗号分隔；生产禁止使用 `*`。

## 限流与模型可观测性

- 试点期间需要紧急暂停生成式 AI 时，将生产 `.env` 的 `AI_ENABLED=false`，然后仅重建 `api`；同步问答、流式问答、错题分析和同类题生成会在扣费前返回维护提示，知识图谱、学习记录和既有错题仍可使用。恢复前先完成受控问答回归，再设回 `true`。
- 生产 API 使用 Redis 固定窗口限流；登录/注册、AI 问答、错题分析和图片上传采用独立阈值。
- 仅在 Nginx 明确覆盖 `X-Real-IP` 时启用 `RATE_LIMIT_TRUST_PROXY_HEADERS=true`；应用优先使用该头，不信任客户端可伪造的转发链首项。
- Redis 暂时不可用时请求降级放行并写入结构化错误日志，避免存储故障扩大为全站不可用。
- `/api/admin/model-costs` 支持 `start_at`、`end_at`、`feature`、`provider` 筛选，展示成功/失败调用、失败率、平均延迟、Token 和额度消耗。
- 主动告警使用 `operational-alert-notify`，默认关闭；配置、负载边界和调度方式见 `docs/OPERATIONAL_ALERTS.md`。
- 发布回归至少制造一次受控成功调用和一次模拟失败，确认失败调用不扣额度且后台统计可见。
- 开启 `ORGANIZATIONS_ENABLED=true` 前必须先设置 `PILOT_AUTHORIZATION_ENFORCED=true`，并验证未批准学生不能消耗邀请码、撤销后立即退出学校和班级。
- “已验证”必须符合 `docs/LEARNING_EVIDENCE.md`；发布回归应确认客户端伪造 `completed_check` 被拒绝，检查重复提交被拒绝。
