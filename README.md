# QQ频道栏目分类与连续重复治理工具

统一代码仓库：[zrzqbr/Tencent-Channel](https://github.com/zrzqbr/Tencent-Channel)

这是一个可运行的 MVP，面向一个或多个 QQ 内置频道的论坛帖子和文字子频道消息。它完成两件事：

1. 将内容检测为“精华、每周一问、实用文章、问答与交流、官方资讯”之一。
2. 如果同一用户在同一栏目里连续发布两条相同内容，自动删除后一条，并写入 SQLite 审计记录。

系统还包含可解释的内容审核层：中英文敏感词、联系方式、外链、低信息灌水、乱码、版块错投和人工复核队列。完整动作边界见 [审核与治理规则](docs/MODERATION_POLICY.md)，未来网页化可按 [管理后台设计基线](docs/ADMIN_BACKEND.md) 接入。

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
# 单次只读/预演巡检
qq-guard --config config.json tencent-scan

# 持续轮询
qq-guard --config config.json tencent-monitor
```

当前已配置的所有频道也可以直接运行：

```bash
./start-tencent-monitor.sh
```

官方 CLI 模式不需要把 Token 写入本项目；登录凭据由 `tencent-channel-cli login` 单独保存在本机。测试模式只输出 `detected_only`，不会调用删帖接口。只有同时设置 `delete_mode=live` 和 `auto_delete_duplicates=true`，才会对命中的后一条重复帖子调用官方删帖接口。

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

## 运行测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖：每周一问缺少井号话题、五类栏目判断、同作者连续重复、不同作者、不同栏目、非连续重复、网关重复投递，以及删除权限失败审计。

## 上线建议

先在 QQ 机器人沙箱中保持 `dry_run` 运行 1～3 天，查看审计记录确认规则命中正确，再将 `delete_mode` 改为 `live`。这不是功能限制，而是防止首次配置的频道 ID、标签别名或官方账号白名单错误造成误删。
