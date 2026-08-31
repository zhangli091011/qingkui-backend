# Android 联调契约

后端基地址：

- Android 模拟器：`http://10.0.2.2:8000/api/`
- C9 真机：`http://<开发机局域网IP>:8000/api/`
- 测试/生产：`https://qingkui-api.82-158-229-157.sslip.io/api/`

## 首次进入

1. `POST auth/register` 或 `POST auth/login`。
2. 将 `access_token` 放入 `Authorization: Bearer ...`。
3. 收到 401 时只调用一次 `POST auth/refresh`；刷新凭证会轮换，旧值立即失效。
4. `GET credits` 显示账户页额度。

## 现有 Kotlin 模型映射

| API 值 | Android 当前枚举 |
|---|---|
| `student` / `assistant` | `MessageAuthor.Student` / `Assistant` |
| `unexplored` | `KnowledgeStatus.Unexplored` |
| `explored` | `KnowledgeStatus.Explored` |
| `understood` | `KnowledgeStatus.Understood` |
| `verified` | `KnowledgeStatus.Verified` |
| `unstable` | `KnowledgeStatus.Unstable` |
| `error_prone` | `KnowledgeStatus.ErrorProne` |
| `to_explore` | Android 尚缺该枚举；联调期可映射为 `Unexplored` |

后端节点字段 `id/name/chapter/status` 对应现有 `KnowledgeNode.id/title/subtitle/status`。坐标由客户端布局算法生成，不由 API 固定返回。

## 问答闭环

1. `POST qa/sessions`，传 `mode` 和可选 `knowledge_node_id`。
2. `POST qa/sessions/{id}/messages`，传 `content`、`help_level`。
3. 显示 `assistant_message.content`；来源面板读取 `citations`。
4. 用响应里的 `balance` 原子更新额度，不在客户端自行减一。

推荐使用 `POST qa/sessions/{id}/messages/stream`。请求体相同，响应为 `text/event-stream`：

- `meta`：返回会话 ID 和预计额度。
- `delta`：`content` 是新增文本，追加到同一条助手消息。
- `done`：返回完整 `QaResult`，此时更新余额、消息 ID 和引用。
- `error`：返回 `status/detail`；失败不会落库或扣额度。

当模型达到 `max_tokens` 时，服务端会在 `delta` 末尾追加“回答达到长度上限”提示，并在 `assistant_message.structured_content.truncated=true` 中标记；客户端应保留回答并提供重新回答入口。

服务端只在 AI 流完整结束后写入消息并扣费。客户端断线时应移除半截回答并允许重试。

`mode`：`knowledge`、`problem`、`error`、`review`、`explore`、`verify`。

`help_level`：`keyword`、`next_step`、`approach`、`full`、`conclusion`。

完整解析消耗 2 额度，其余当前消耗 1 额度。HTTP 502 表示模型失败且未扣费；402 表示额度不足；503 表示服务端未配置模型。

回答卡片的“内容有误”应先让用户填写至少 2 个字的错误说明，再调用 `POST feedback`：`category=answer_error`、`message_id=<assistant_message.id>`，说明放入 `content`。其余“有帮助”“没帮助”“标记复习”可直接提交；所有反馈都只允许访问当前用户自己的回答。

## 图谱与学习状态

- 搜索：`GET knowledge/search?q=二次函数`
- 节点详情：`GET knowledge/nodes/{id}`
- 当前节点邻接：`GET knowledge/nodes/{id}/neighbors`
- 浏览后记录：`POST learning/events`，`event_type=viewed_node`
- 主动状态：`PATCH learning/nodes/{id}/state`
- 学习页：`GET learning/summary`

客户端不能直接把状态改成 `verified`。只有 `completed_check` 且 `event_data.passed=true` 的学习事件可以产生“已验证”。

## 学校与匿名班级趋势

- 当前用户学校：`GET organizations/me`
- 学校班级：`GET organizations/schools/{school_id}/classes`
- 使用邀请码：`POST organizations/invites/redeem`
- 退出学校：`DELETE organizations/schools/{school_id}/membership`
- 教师匿名概览：`GET organizations/classes/{class_id}/overview`

学生邀请码可能返回 403“尚未完成学校试点准入授权”，客户端应原样提示联系学校管理员，不应重试或自行绕过。教师概览新增 `top_error_categories`、`weak_knowledge_points`、`practice_completion_rate`、`second_attempt_accuracy` 和 `due_review_count`；前两项只包含显示名称与计数，不包含知识节点 ID 或学生身份。
