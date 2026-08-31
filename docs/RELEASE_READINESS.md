# 统一发布就绪报告

`/health` 只检查服务依赖和生产配置，不能替代内容审核、人工评测、恢复演练、正式签名包或 C9 真机验收。正式发布使用独立的严格报告：

```powershell
python -m app.cli release-evidence-templates `
  --output-dir release-evidence

python -m app.cli release-readiness-report `
  --evidence-dir release-evidence `
  --output release-readiness-report.json `
  --strict
```

`--strict` 在任何必需门禁未通过时返回退出码 1，可直接用于发布流水线。管理员也可以通过 `GET /api/admin/release-readiness` 或管理后台“发布就绪门禁”查看同一报告。

## 证据目录

生产 Compose 将主机的 `QINGKUI_RELEASE_EVIDENCE_HOST_PATH` 只读挂载到 `/release-evidence`。目录固定包含：

| 文件 | 产生方式 | 通过条件 |
|---|---|---|
| `human-eval-dataset.json` | 已批准评测集的不可变副本 | 数据集状态、版本和首发范围匹配，SHA-256 与运行文件一致 |
| `human-eval-reviewed-run.json` | `qa-eval-run` 结果完成人工字段后保存 | SHA-256 与评分报告一致 |
| `human-eval-score.json` | `qa-eval-score` | 审核员标识存在且全部人工指标通过 |
| `recovery-drill.json` | `deploy/recovery-drill.sh --full-oss` | 30 天内隔离恢复成功，迁移一致，当前/上一索引均校验，全量 OSS 审计通过 |
| `android-release-manifest.json` | Android `scripts/New-ReleaseManifest.ps1` | 签名与 APK 哈希有效，版本明确，发布负责人独立复核并批准 |
| `c9-evidence.json` | 目标 C9/MDM 验收人员填写 | 十项真机检查全部通过，APK 哈希与正式发布清单一致 |

模板命令只创建 `pending`/`draft` 文件，绝不会自动标记通过，也不会覆盖已有文件，除非显式使用 `--overwrite`。人工证据不得由模型、模拟器结果或脚本默认值代填。

## 自动交叉校验

报告同时验证：

1. 当前数据库 Alembic 版本等于代码唯一 head。
2. 首发范围达到 `RELEASE_MIN_APPROVED_NODES`，已批准节点无发布阻断，文档元数据、授权和公式复核均清零。
3. 人工评测的数据集、人工复核运行和评分报告形成完整哈希链，且学科、年级和教材版本与生产首发配置一致。
4. C9 使用的 APK SHA-256 与正式发布清单一致。
5. 发布清单的向量索引 SHA-256 与恢复演练验证的当前索引一致。
6. `/health` 所列全部生产配置问题已经关闭。

每个 C9 检查项的 `evidence` 至少引用一个证据目录内的相对路径和 SHA-256；报告会读取文件重新计算哈希，绝对路径、`..` 路径穿越、缺失文件或摘要不一致都会阻断发布。

证据文件应保存在受控发布目录并限制写权限。报告只记录稳定审核员标识，不写姓名、电话、学生信息、密钥、题目正文或 keystore 密码。
