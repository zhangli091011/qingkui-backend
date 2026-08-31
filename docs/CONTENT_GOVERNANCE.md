# 首发内容治理规范

首发正式内容固定为 `数学 / 高一 / 人教A版`。其他学科和版本可以继续导入、向量化和生成草稿，但不得绕过发布门进入学生图谱。

## 发布门

每个知识节点必须同时满足：

1. 来源状态为 `authorized`、`self_owned` 或 `public_domain`；`self_owned_demo` 只能用于开发演示。
2. 学科、年级、教材版本与当前首发范围完全一致。
3. 定义、解释、常见错误、典型题型、来源定位完整，不含“待审核”“待补充”等占位语。
4. 至少存在一条有效知识关系；关系方向与类型必须由内容审核员确认。
5. 发布前运行内容治理报告，确认公式待复核数、文档元数据缺失数和未授权文档数。

## 审核 SLA

- 新节点初审：2 个工作日。
- 公式低置信度或 OCR 异常：1 个工作日内进入人工队列，未经确认不进入正式内容。
- 学生高风险反馈：4 小时内下线或加警示，2 个工作日内完成修复版本。
- 普通内容反馈：3 个工作日内给出受理状态，5 个工作日内完成处理。

## 发布、下线与恢复

- 发布只通过 `/api/admin/knowledge/nodes/{id}/publish`，禁止直接修改数据库状态。
- 发现公式、定义、来源或关系错误时先下线，再创建修订版本；不得覆盖历史版本。
- 恢复历史版本仍需重新通过当前发布门，旧版本不会因为曾发布而自动获得资格。
- 每次操作写入审计日志，记录操作者、版本和变更说明。

治理报告接口：`GET /api/admin/knowledge/governance`。它返回范围内候选节点、逐项阻断原因、文档元数据、公式审核和关系类型统计。

元数据回填应先运行只读演练，检查输出中的 `fields_changed`、`incomplete_documents` 和各 `missing_*` 计数，再正式提交：

```powershell
python -m app.cli backfill-document-metadata --dry-run
python -m app.cli backfill-document-metadata
```

可以运行 `python -m app.cli materialize-launch-candidates --limit 600` 从首发范围内的授权文档生成审核候选。命令按文档族去重，抽取章节、知识点和题型层级并建立关系；新节点一律为未激活草稿，且包含“待审核”标记，不能自动发布。

## 批量审核包

为减少审核员逐个查找原文的时间，可以生成只读审核包：

```powershell
python -m app.cli content-review-packet `
  --subject 数学 `
  --grade 高一 `
  --textbook-version 人教A版 `
  --limit 600 `
  --output .local/math-review-packet.json `
  --markdown .local/math-review-packet.md
```

审核人员必须逐项核对原文证据，修订 `node.definition / explanation / common_errors / question_types`，并把 `review` 填为：

```json
{
  "status": "completed",
  "reviewer": "content_admin_username",
  "reviewed_at": "2026-08-31T15:00:00+00:00",
  "decision": "approved",
  "notes": "已按人教 A 版教材逐条核对"
}
```

先只读演练回写；该命令会验证审核人权限、节点版本、范围、来源授权和发布阻断项：

```powershell
python -m app.cli content-review-apply `
  --input .local/math-review-packet.json `
  --reviewer content_admin_username `
  --publish `
  --dry-run
```

确认演练结果后才允许正式回写。发布模式不会绕过发布门；有阻断项的节点保留为未激活草稿：

```powershell
python -m app.cli content-review-apply `
  --input .local/math-review-packet.json `
  --reviewer content_admin_username `
  --publish `
  --confirmation PUBLISH_REVIEWED_CONTENT
```

审核包为每个草稿节点提供来源、原文分块、当前阻断原因、同文档待审公式，以及从带“易错、注意、题型、例题”等标记的原文行中提取的建议。每条建议都保留 `chunk_id` 和原文证据。

`content-review-packet` 只生成审核材料，不修改数据库。建议可能不完整或与节点边界不完全一致；审核员必须对照原文确认后填写节点字段和 `review` 结论。只有 `content-review-apply` 会在权限、版本、范围和发布门全部校验后写回；禁止使用其他脚本绕过该命令，把启发式建议直接批量写入或发布。

## 自动预审与草稿补全

对大量结构候选可以先运行两轮模型预审。第一轮只依据审核包中的原文分块补全定义、解释、易错点和题型；第二轮独立检查证据支持、边界条件和公式风险。规则检查、两轮结果、模型、Prompt 版本、证据分块和证据 SHA-256 会写入 `content_auto_reviews`：

```powershell
python -m app.cli auto-review-launch-content `
  --subject 数学 `
  --grade 高一 `
  --textbook-version 人教A版 `
  --limit 600 `
  --workers 4 `
  --output .local/math-auto-review.json
```

默认命令只生成预审记录。确认报告后，可以把高置信且证据完整的建议写成新的未激活草稿版本：

```powershell
python -m app.cli auto-review-launch-content `
  --apply-drafts `
  --confirmation APPLY_AI_DRAFT_IMPROVEMENTS `
  --output .local/math-auto-review-applied.json
```

该命令在数据库层固定保留 `review_status=draft` 和 `is_active=false`，版本记录状态也是 `draft`，并写入 `knowledge_node.auto_review_applied` 审计事件。模型无权填写审核员、批准公式或发布节点。自动预审结束后必须重新生成 `content-review-packet`，由真实学科审核员逐项确认，再通过 `content-review-apply` 发布。
