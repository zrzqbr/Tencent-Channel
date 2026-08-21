# 可视化管理后台设计基线

当前状态：已实现并部署。以下模块均需要管理员登录；编辑、移动和删除提交后直接作用到腾讯频道，并自动记录结果。

## 页面模块

1. **总览**：今日检测数、待审核数、重复数、删除成功/失败数、各风险等级与栏目分布。
2. **审核队列**：按频道、版块、风险、规则和时间筛选；展示原内容、分类、证据和建议动作。
3. **频道与版块**：维护频道 ID、版块 ID、固定分类或自动分类模式、最小长度、话题和外链规则。
4. **敏感词规则**：维护中文/英文词、匹配方式、分类、严重度、建议动作和策略版本。
5. **去重记录**：同时展示两条内容及指纹、作者、所在栏目、相邻关系和删除状态。
6. **操作审计**：记录审核人、批准/拒绝、删帖结果、失败原因和时间，禁止覆盖历史记录。
7. **内容测试**：管理员可输入标题、正文、媒体和版块 ID，只运行分类与审核，不连接 QQ、不执行删除。

## 建议接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/dashboard` | 获取汇总指标 |
| `GET` | `/api/reviews` | 获取待审核队列 |
| `GET` | `/api/events/{id}` | 查看内容、分类和完整理由 |
| `POST` | `/api/reviews/{id}/resolve` | 批准、拒绝、忽略或请求删除 |
| `GET/PUT` | `/api/policies` | 查询或更新版本化审核策略 |
| `GET/POST/PUT` | `/api/boards` | 管理频道版块规则 |
| `GET` | `/api/audit-actions` | 查询不可变操作日志 |

删除接口与审核状态保持分离。管理员点击删除时，服务端重新读取当前内容 ID、频道 ID 和最新审核记录，再调用腾讯官方接口执行并写入审计。

## 核心返回字段

```json
{
  "content_id": "平台内容ID",
  "guild_id": "频道ID",
  "channel_id": "版块ID",
  "classification": "practical_article",
  "classification_confidence": 0.78,
  "risk_level": "medium",
  "risk_score": 45,
  "policy_version": "2026-08-19.1",
  "recommended_action": "review",
  "reasons": [
    {
      "code": "sensitive_term_en",
      "message": "命中英文敏感词规则",
      "evidence": "命中的词",
      "auto_delete_eligible": false
    }
  ],
  "review_status": "pending",
  "delete_status": "not_needed"
}
```

## 权限与安全

- 登录后才能访问，管理员与审核员分权。
- 腾讯凭据只保存在服务端密钥管理中，绝不返回浏览器。
- 修改策略、批量审核和真实删帖必须要求管理员登录、CSRF 校验和不可变审计。
- 所有写操作保存操作者、请求 ID、策略版本、旧值和新值。
- Web 服务默认使用 HTTPS、CSRF 防护、速率限制和会话过期。
- 管理密码只以 PBKDF2 哈希保存在服务器环境文件中，仓库不保存明文或哈希。
- 登录连续失败会限速；Nginx 与应用层分别限制登录请求。
- 页面显示敏感内容时应局部遮挡，点击后才展开证据。

## 当前可直接复用的数据

- `content_events`：机器人实时事件、分类、风险、建议动作、重复和删除结果。
- `moderation_review_actions`：人工审核结论和备注。
- `tencent_moderation_findings`：腾讯官方 CLI 巡检发现的问题。
- `tencent_duplicate_actions`：频道帖子连续重复记录。
- `tencent_scan_runs`：每次巡检的数量和分类汇总。

在接入正式网页前，可使用 `qq-guard dashboard` 输出汇总 JSON，使用 `qq-guard audit --review-only` 查看待审核记录。
