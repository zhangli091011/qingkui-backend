# 学校与班级数据边界

学校/班级能力属于 V2，代码与迁移可以提前部署，但默认由 `ORGANIZATIONS_ENABLED=false` 关闭。试点或生产启用时必须同时设置 `PILOT_AUTHORIZATION_ENFORCED=true`，否则服务拒绝启动。学校管理员需先登记准入确认，学生邀请码才可使用。

## 模型

- `schools`：学校租户，不存校方系统密钥。
- `school_memberships`：学校管理员、教师、学生角色；业务权限不依赖客户端 Token 中的角色声明。
- `school_classes`、`class_memberships`：班级及教师/学生归属。
- `organization_invites`：只保存邀请码 HMAC 摘要、用途、有效期和使用次数，明文只在创建响应中出现一次。
- `pilot_enrollment_approvals`：按学校和参与者保存学校授权、学生自愿参与、必要的监护人授权及依据；不保存监护人姓名、电话、证件号或住址。
- `users.tenant_id`：学生/教师首次加入学校后绑定，禁止同一账户跨学校加入；全局系统管理员不绑定单一租户。

## 权限

| 操作 | 系统管理员 | 学校管理员 | 教师 | 学生 |
|---|---|---|---|---|
| 创建学校 | 是 | 否 | 否 | 否 |
| 创建班级 | 是 | 是 | 否 | 否 |
| 邀请教师 | 是 | 是 | 否 | 否 |
| 邀请学生 | 是 | 是 | 仅本人任教班级 | 否 |
| 管理试点准入 | 是 | 本校 | 否 | 否 |
| 查看班级概览 | 是 | 本校 | 仅本人任教班级 | 否 |
| 查看班级列表 | 全部 | 本校全部 | 本人班级 | 本人班级 |

所有权限从数据库当前成员关系读取。伪造 JWT 角色、学校 ID 或班级 ID 不会扩大权限。

## 教师概览

教师端只返回班级聚合和稳定匿名学生 ID：最近活动、提问数、错题数、已验证节点数，以及错因分布、薄弱知识点名称、近 7 日练习完成率、二次作答正确率和到期复习数。不返回用户名、昵称、邮箱、节点 ID、题目、回答、笔记、反馈正文或图片。未来若增加实名视图，必须单独记录授权范围、授权人、目的和有效期，不能复用当前匿名接口。

## 接口

- `POST /api/organizations/schools`
- `GET /api/organizations/me`
- `GET/POST /api/organizations/schools/{school_id}/classes`
- `POST /api/organizations/schools/{school_id}/invites`
- `GET/POST /api/organizations/schools/{school_id}/pilot-approvals`
- `DELETE /api/organizations/schools/{school_id}/pilot-approvals/{participant_id}`
- `POST /api/organizations/invites/redeem`
- `GET /api/organizations/classes/{class_id}/overview`

注销和试点清理会删除学校/班级成员关系及幂等响应。审计日志保留匿名操作事实，不保留邀请码明文。

撤销准入会立即停用该参与者在对应学校和班级的成员关系，并解除账户的学校绑定。重新加入前必须重新批准；无有效批准的请求不会消耗邀请码次数。

## 活动额度

活动额度由 `CREDIT_CAMPAIGNS_ENABLED=false` 默认关闭。系统管理员可以创建全局或学校范围活动，设置有效期、活动总次数、个人次数和单码次数。兑换码与邀请码一样只保存 HMAC 摘要，明文只在创建响应中返回一次。兑换会在同一事务内锁定兑换码、活动和额度账户，并写入 `credit_ledger`；学校活动同时检查 `tenant_id` 和有效学校成员关系。
