# QQ频道栏目分类与连续重复治理工具

统一代码仓库：[zrzqbr/Tencent-Channel](https://github.com/zrzqbr/Tencent-Channel)

下一位开发者或 AI 请先阅读 [开发与生产交接文档](HANDOFF.md)，其中包含当前进度、GitHub 提交方式、服务器发布与回滚、授权注意事项和待优化清单。

这是一个可运行的 MVP，面向一个或多个 QQ 内置频道的论坛帖子和文字子频道消息。它完成两件事：

1. 将内容检测为“精华、每周一问、实用文章、问答与交流、官方资讯”之一。
2. 如果同一用户在同一栏目里连续发布两条相同内容，自动删除后一条，并写入 SQLite 审计记录。

系统还包含可解释的内容审核层：中英文敏感词、联系方式、外链、低信息灌水、乱码、版块错投和人工复核队列。完整动作边界见 [审核与治理规则](docs/MODERATION_POLICY.md)，未来网页化可按 [管理后台设计基线](docs/ADMIN_BACKEND.md) 接入。

后台现已接入腾讯官方社区 Skill `1.1.5` 的动态能力清单，统一展示频道、栏目、帖子、评论、回复、成员、权限、通知、私信和运营快捷工具。官方写操作需开启服务器开关，登录管理员提交后直接执行并自动审计。巡检优先使用全频道时间线一次读取各栏目内容，跟随官方分页游标同步，并分别统计本轮新增、更新、缓存和累计收录。

AI 审核使用腾讯云境内 TokenHub 的双模型链路：`youtu-vita` 先提取图片文字、主体和风险线索，`hy3` 再结合全文、相邻内容、版块要求和规则信号给出结构化分类、风险、证据和建议。普通内容只能进入人工审批，AI 无权直接删帖。

频道管理后台的正式域名是 [tencent.ruitcarch.cloud](https://tencent.ruitcarch.cloud)。DNS、独立 Nginx 站点、自动续期 HTTPS 和密码登录后台均已启用。部署状态与上线检查项见 [部署与域名](docs/DEPLOYMENT.md)。

“全部”和“热门”是展示视图，不作为内容分类。

## 已实现的栏目规则

| 栏目 | 默认判断规则 |
| --- | --- |
| 每周一问 | `#每周一问`；或正文出现“每周一问/本周问题”等语义并至少包含一个 `#话题`。缺少井号话题时进入待确认，不算每周一问。 |
| 实用文章 | 图文结合，并包含案例、教程、步骤、实战、指南、经验、复盘、解决方案等特征，或达到配置的文章长度。 |
| 问答与交流 | 包含问号，或出现“请问、如何、怎么、求助、讨论、交流”等互动语气。 |
| 精华 | 默认要求显式 `#精华`，避免机器直接把普通长文授予精华；长篇图文会额外标记为 `featured_candidate`。 |
| 官方资讯 | 使用 `#官方资讯/#官方公告`，或作者 ID 在 `official_author_ids` 白名单中。 |

显式栏目话题优先于内容特征。所有标签、关键词、长度和子频道映射都可以在 `config.json` 中调整。

## 去重的准确含义

只有同时满足以下条件，后一条才会被删除：

- 位于同一个 QQ 频道（不同频道的数据完全隔离）；
- 检测为同一个栏目；
- 两条内容在该栏目内相邻，中间没有其他内容；
- 作者相同；
- 标题、正文、图片/视频地址在统一空白和全半角后完全相同。

不同作者、不同栏目、非连续重复、仅仅语义相似的内容都不会自动删除。这样可以尽量避免误删。

论坛帖子调用 `delete_thread`；普通文字子频道消息调用 `recall_message`。删除失败会记录为 `failed`，并保存权限错误等原因。

## 本地安装

需要 Python 3.9 或更高版本。

```bash
cd /path/to/qq-channel-content-guard
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e '.[qq]'
cp config.example.json config.json
```

先编辑 `config.json`：

- `delete_mode`：首次测试保持 `dry_run`。该模式不会调用腾讯删除接口。
- `auto_delete_duplicates`：默认 `false`。只有它为 `true` 且 `delete_mode=live` 时才允许删除。
- `auto_delete_policy_violations`：默认 `false`。只有明确具备自动删除资格的敏感词规则才会使用该开关；低质量和分类问题不能自动删除。
- `channel_sections`：如果某个子频道固定属于一个栏目，可填写 `"子频道ID": "qa_discussion"`。固定为 `weekly_question` 的子频道仍要求帖子带井号话题。
- `official_author_ids`：填写官方运营账号的用户 ID，防止普通成员伪装成官方资讯。
- `section_hashtags`：配置栏目允许使用的井号话题别名。

## 接入 QQ 频道

在 QQ 开放平台创建机器人并取得 AppID、AppSecret。这个工具需要：

- 私域频道的完整消息事件权限，才能看到未 @ 机器人的普通频道消息；
- 论坛事件权限，才能检测信息流帖子；
- 对相关子频道的管理权限，才能撤回成员消息或删除帖子。

启动：

```bash
export QQBOT_APP_ID='你的AppID'
export QQBOT_APP_SECRET='你的AppSecret'
export QQ_GUARD_CONFIG='/绝对路径/config.json'
qq-guard-bot
```

如果使用腾讯官方频道 Skill/CLI，可在 `config.json` 中配置 `tencent_channels`。每个频道可以使用固定栏目版块，也可以把一个版块配置为自动分类入口：

```json
{
  "delete_mode": "dry_run",
  "auto_delete_duplicates": false,
  "tencent_channels": [
    {
      "name": "频道一",
      "guild_id": "频道ID",
      "channels": {
        "practical_article": "实用文章版块ID",
        "qa_discussion": "问答版块ID"
      }
    },
    {
      "name": "频道二",
      "guild_id": "频道ID",
      "auto_classify_channels": {
        "文章": "统一文章版块ID"
      }
    }
  ]
}
```

`auto_classify_channels` 会先按井号话题与内容特征分类，再在同一分类内判断连续重复。配置完成后可以执行：

```bash
# 单次巡检
qq-guard --config config.json tencent-scan

# 只同步帖子、文章、正文和图片，不执行 AI
qq-guard --config config.json tencent-sync

# 默认不持续轮询。只有明确需要恢复自动巡检时才设置：
QQ_GUARD_AUTO_SCAN_ENABLED=true qq-guard --config config.json tencent-monitor
```

生产后台将内容同步与 AI 巡检分开：内容每 30 分钟自动同步，也可以在“全部内容”手动同步；每轮同步会限量补齐历史正文，遇到官方频率限制则留到下轮继续。AI 只在管理员点击“立即巡检”后分析已经同步的文字和图片。

官方 CLI 模式不需要把 Token 写入本项目；登录凭据由 `tencent-channel-cli login` 单独保存在本机。测试模式只输出 `detected_only`，不会调用删帖接口。只有同时设置 `delete_mode=live` 和 `auto_delete_duplicates=true`，才会对命中的后一条重复帖子调用官方删帖接口。

## 接入腾讯云 Hy3 与 VITA

在腾讯云 TokenHub 控制台开通 `hy3` 和 `youtu-vita`，创建广州站 API Key，并仅在服务器环境文件中配置：

```bash
TENCENT_TOKENHUB_API_KEY='腾讯云 TokenHub API Key'
TENCENT_TOKENHUB_BASE_URL='https://tokenhub.tencentmaas.com/v1'
```

同一 TokenHub Key 默认同时供 Hy3 和 Youtu-VITA 使用。如视觉服务使用单独密钥，可以额外设置 `TENCENT_VITA_API_KEY`。不要把任何密钥写进 `config.json`、网页表单或 GitHub。

当 VITA 调用失败时，含图片内容会被强制转为人工复核；当 Hy3 调用失败时，系统降级为规则审核。两种降级都不会触发普通内容自动删除。

程序输出一行一条 JSON 日志。每次检测都会包含栏目、置信度、井号话题、是否为精华候选、是否重复及删除结果。

## 在上线前测试分类

```bash
qq-guard --config config.json classify \
  --body '#效率工具 每周一问：你最常用什么方法？'

qq-guard --config config.json classify \
  --title '一次部署故障复盘' \
  --body '这是完整案例、操作步骤和解决方案……' \
  --media 'https://example.com/case.png'
```

查看最近审计记录：

```bash
qq-guard --config config.json audit --limit 30
qq-guard --config config.json audit --duplicates-only

# 测试完整审核机制
qq-guard --config config.json moderate --channel 版块ID --body '待检测内容'

# 查看待人工复核内容和后台汇总
qq-guard --config config.json audit --review-only
qq-guard --config config.json dashboard
```

## 可视化管理后台

后台包含今日处理、全部内容、内容审核、频道管理、规则与栏目和操作记录。“全部内容”显示已同步的正常帖子与待审核内容，并可直接编辑、移动、打开原帖或删除。“立即同步”只更新频道数据；“立即巡检”只执行文字与图片 AI 分析。今日处理按“需要删帖、调整栏目、需要核对、可以保留”分开显示。同一时间只允许同步或巡检中的一项任务运行。

“AI 分析记录”会逐条展示规则初审、Youtu-VITA 图片证据、Hy3 综合摘要、置信度、建议动作和管理员状态。如果服务器未配置 API Key 或模型执行失败，页面会明确显示“规则判定/安全降级”及原因，不会把规则结果伪装成 AI 结论。

安装网页依赖：

```bash
pip install -e '.[web]'
```

生产环境必须通过环境变量提供随机会话密钥和经过 PBKDF2 处理的密码哈希，不能提交明文密码：

```bash
export QQ_GUARD_CONFIG='/绝对路径/config.json'
export QQ_GUARD_SECRET_KEY='随机会话密钥'
export QQ_GUARD_ADMIN_PASSWORD_HASH='Werkzeug 密码哈希'
export QQ_GUARD_MANUAL_DELETE_ENABLED='false'
qq-guard-web
```

真实人工删除支持单条和批量两种入口。管理员登录后点击删除即直接执行；批量操作必须先逐条勾选，每次最多 20 条。系统逐条记录平台结果，遇到频率限制会停止尚未执行的内容。腾讯官方 CLI 必须已登录，服务器也必须显式启用人工删除。自动删除开关与人工删除相互独立，默认保持关闭。

同一个腾讯帖子在不同策略版本下只显示最新审核记录；历史记录不能再次进入删除队列。删除成功后，该帖所有历史策略记录都会同步为已删除，避免对已经不存在的帖子重复调用平台接口。

审核列表和“全部内容”都支持调整栏目：选择同一腾讯频道内的帖子和目标栏目后，后台直接调用官方 `feed move-feed` 接口移动原帖。移动不会删帖重发，成功或失败结果、原栏目和目标栏目都会写入审计记录。

## 运行测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖：每周一问缺少井号话题、五类栏目判断、同作者连续重复、不同作者、不同栏目、非连续重复、网关重复投递、删除权限失败审计、后台登录、CSRF、规则修改、审核操作、完整内容目录、AI 记录页、全频道增量同步、巡检进度与并发互斥，以及单条和批量直接操作。

## 上线建议

先在 QQ 机器人沙箱中保持 `dry_run` 运行 1～3 天，查看审计记录确认规则命中正确，再将 `delete_mode` 改为 `live`。这不是功能限制，而是防止首次配置的频道 ID、标签别名或官方账号白名单错误造成误删。
