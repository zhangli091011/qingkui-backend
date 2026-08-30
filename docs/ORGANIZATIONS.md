# 学校与班级数据边界

学校/班级能力属于 V2，代码与迁移可以提前部署，但默认由 `ORGANIZATIONS_ENABLED=false` 关闭。只有试点结论、学校授权、未成年人数据说明和管理员责任人明确后才能开启。

## 模型

- `schools`：学校租户，不存校方系统密钥。
- `school_memberships`：学校管理员、教师、学生角色；业务权限不依赖客户端 Token 中的角色声明。
- `school_classes`、`class_memberships`：班级及教师/学生归属。
- `organization_invites`：只保存邀请码 HMAC 摘要、用途、有效期和使用次数，明文只在创建响应中出现一次。
- `users.tenant_id`：学生/教师首次加入学校后绑定，禁止同一账户跨学校加入；全局系统管理员不绑定单一租户。

## 权限

| 操作 | 系统管理员 | 学校管理员 | 教师 | 学生 |
|---|---|---|---|---|
| 创建学校 | 是 | 否 | 否 | 否 |
| 创建班级 | 是 | 是 | 否 | 否 |
| 邀请教师 | 是 | 是 | 否 | 否 |
| 邀请学生 | 是 | 是 | 仅本人任教班级 | 否 |
| 查看班级概览 | 是 | 本校 | 仅本人任教班级 | 否 |
| 查看班级列表 | 全部 | 本校全部 | 本人班级 | 本人班级 |

所有权限从数据库当前成员关系读取。伪造 JWT 角色、学校 ID 或班级 ID 不会扩大权限。

## 教师概览

教师端只返回班级聚合和稳定匿名学生 ID：最近活动、提问数、错题数、已验证节点数。不返回用户名、昵称、邮箱、题目、回答、笔记、反馈正文或图片。未来若增加实名视图，必须单独记录授权范围、授权人、目的和有效期，不能复用当前匿名接口。

## 接口

- `POST /api/organizations/schools`
- `GET /api/organizations/me`
- `GET/POST /api/organizations/schools/{school_id}/classes`
- `POST /api/organizations/schools/{school_id}/invites`
- `POST /api/organizations/invites/redeem`
- `GET /api/organizations/classes/{class_id}/overview`

注销和试点清理会删除学校/班级成员关系及幂等响应。审计日志保留匿名操作事实，不保留邀请码明文。
