# 主动运维告警

管理后台的 `/api/admin/operational-alerts` 与命令行告警使用同一套模型失败率、模型延迟、OCR 失败和 OCR 卡住判定。主动通知默认关闭；未配置 `OPERATIONAL_ALERT_WEBHOOK_URL` 时不会向外部发送数据。

Webhook 负载只包含环境名、时间窗口、聚合指标、告警代码和阈值，不包含用户标识、题目、回答、图片、密钥或完整服务配置。接收端必须使用 HTTPS，并由运维负责人控制访问权限。

## 验证

先在容器内预览负载，不发送网络请求：

```bash
docker compose -p qingkui run --rm --no-deps api \
  python -m app.cli operational-alert-notify --dry-run
```

配置服务器秘密 `OPERATIONAL_ALERT_WEBHOOK_URL` 后，执行同一命令但移除 `--dry-run`。只有状态为 `warning` 或 `critical` 时发送；相同告警组合在 `OPERATIONAL_ALERT_COOLDOWN_MINUTES` 内只发送一次。Webhook 失败会返回非零退出码并释放去重锁，供下次任务重试。

## 调度

可由服务器 systemd timer 或现有受控调度器每 5 分钟运行一次。调度器应记录退出码，禁止把 Webhook URL 写进命令行、Git 或日志。部署后先制造一次受控阈值告警，确认接收、冷却去重和恢复为 `ok` 三种状态。
