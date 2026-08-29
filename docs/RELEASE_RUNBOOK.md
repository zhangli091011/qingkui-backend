# APK、MDM、升级与紧急回滚

## 签名

- 正式包使用独立 PKCS12/JKS 上传密钥，至少 RSA 3072，密码存入离线密码库和 CI secret。
- keystore 不进入 Git、镜像、服务器日志或 MDM 公共目录；保留两份加密离线备份。
- `versionCode` 每次发布严格递增；同一 applicationId 不得更换签名。
- Debug 包不得进入学生 MDM 正式应用目录。

## 发布通道

1. CI 生成未签名/测试 APK并运行测试。
2. 负责人从批准 commit 构建签名 APK，记录 SHA-256、versionCode、变更记录。
3. 先投放内部设备组，再投放 5-10 人试点组；禁止默认全校推送。
4. MDM 使用分阶段比例、安装窗口和强制升级开关；重大升级先允许回退。

## 兼容与升级

- 数据库先做可恢复备份，再执行向后兼容迁移；旧 APK 至少保留一个发布周期的 API 兼容。
- 服务端部署采用“新镜像健康检查通过后切流”，不得在原容器内手工改代码。
- 向量索引按 SHA-256 版本化，本地只读挂载；OSS 保存当前和上一个可用版本。

## 紧急回滚

1. 停止扩大 MDM 发布并冻结新版本。
2. API 回滚到上一批准镜像；若迁移不兼容，先进入只读维护页，再按演练过的恢复点还原 PostgreSQL。
3. 恢复上一向量索引软链接/挂载并执行健康检查和一条受控问答。
4. MDM 重新发布上一签名 APK；由于 Android 不允许降 versionCode，回滚包使用相同代码但更高 versionCode。
5. 记录影响范围、时间线、数据修复和防复发项。

## HTTPS 证书

- 生产域名为 `qingkui-api.82-158-229-157.sslip.io`，证书由 Certbot webroot 模式签发。
- ACME 目录固定为 `/www/wwwroot/qingkui-api-acme`，续期钩子安装在 `/etc/letsencrypt/renewal-hooks/deploy/`。
- 每次 Nginx 配置变更先执行 `nginx -t`；续期钩子也会先校验配置再重载。
- 发布验收必须覆盖 HTTP 301、TLS 证书、`/health`、`/docs`、OpenAPI 和一条真实 SSE 问答。
