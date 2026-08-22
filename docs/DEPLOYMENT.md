# 部署与域名

## 正式入口

- 频道管理后台：`https://tencent.ruitcarch.cloud`
- 服务器公网地址：`150.158.77.134`
- 峰会官网：`https://ruitcarch.cloud`，与频道管理后台相互独立

## 当前状态（2026-08-23）

- DNSPod 已添加 `tencent` 的 A 记录，指向 `150.158.77.134`，TTL 为 600 秒。
- 独立 Nginx 站点已启用；HTTP 会跳转到同一子域名的 HTTPS，不会跳转到峰会官网。
- Let's Encrypt HTTPS 证书已签发，有效期至 2026-11-17；Certbot 自动续期定时器和模拟续期均验证成功。
- 密码登录管理后台已上线，由 Gunicorn 监听 `127.0.0.1:8787`，Nginx 负责 HTTPS 和反向代理。
- 后台服务单元为 `qq-channel-admin.service`；`qq-channel-sync.timer` 每 30 分钟只同步频道内容；`qq-channel-monitor.service` 保持禁用，AI 巡检由管理员在网页点击“立即巡检”后执行。
- WorkBuddy 官方知识库 2.0.1 位于 `/srv/tencent-channel/knowledge/current`；`workbuddy-kb-update.timer` 每天 09:00 更新公开官方文档和检索索引，不执行频道同步、AI 巡检或自动回复。当前生产索引为 140 份官方文档、106 篇公众号资料，共 246 份。
- 可使用 `https://tencent.ruitcarch.cloud/healthz` 检查应用状态，正常返回 JSON `{"status":"ok"}`。
- 普通审核内容只进入管理员审批，不自动删除；严格连续重复去重可独立开启。
- AI 使用广州站 TokenHub：Hy3 负责语义终审建议，Youtu-VITA 负责图片理解。

## 上线前必须完成

1. 腾讯官方频道 CLI 需要在服务器服务账号下单独扫码授权，凭据只保存在服务器。
2. 后续如增加审核员账号，应拆分管理员与审核员权限；当前仅提供单一管理员入口。
3. 真实自动删帖仍需单独修改 `delete_mode` 和对应自动删除开关，不与网页上线联动。
4. 在 `/etc/tencent-channel.env` 中配置 `TENCENT_TOKENHUB_API_KEY`，并在 TokenHub 控制台开通 `hy3` 与 `youtu-vita`；密钥不得进入仓库或网页配置。
5. 官方能力中心的真实写操作由 `QQ_GUARD_OFFICIAL_WRITES_ENABLED=true` 单独开启；未设置时写入按钮不可用。开启后，登录管理员提交即直接执行并自动写入操作记录。
6. 知识库检索不需要第三方依赖；每日抓取需要在独立虚拟环境中安装 `requirements-update.txt`。公众号抓取来源和凭据只能放在 `/srv/tencent-channel/knowledge/current/config.json` 与 `/etc/workbuddy-kb.env`，不得进入应用仓库。

## 验收标准

- `https://tencent.ruitcarch.cloud` 的证书域名校验通过。
- HTTP 自动跳转到同一子域名的 HTTPS，不跳转到峰会官网。
- 未登录用户不能读取频道内容、审核结果或策略配置。
- 所有分类、审核、去重和删除动作均保留理由、策略版本、操作者和时间。
- 普通违规自动删除保持关闭；严格连续重复只有在独立开关启用后才可删除，测试环境仍只能产生 `detected_only` 或 `dry_run` 记录。
- “知识问答”只为有充分官方证据的内容生成草稿；管理员点击发布后才调用官方 `feed.do-comment`，成功和失败都进入操作记录。
