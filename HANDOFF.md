# 腾讯频道内容治理平台交接文档

> 最后核对：2026-08-23（Asia/Shanghai）
> 下一位开发者或 AI 应先完整阅读本文，再阅读 README.md、docs/MODERATION_POLICY.md 与 docs/DEPLOYMENT.md。  
> 本文不保存管理密码、QQ Token、腾讯云 API Key、会话密钥或 SSH 私钥。

## 1. 项目说明

这是一个面向腾讯 QQ 内置频道的内容治理后台。系统通过腾讯官方频道 CLI 读取帖子，进行确定性连续去重、规则审核、Youtu-VITA 图片理解和 Hy3 语义审核，然后向管理员给出“保留、调整栏目、人工核对、删除候选”之一的明确建议。普通内容最终由管理员审批，AI 不直接删除。

- 正式地址：https://tencent.ruitcarch.cloud/
- GitHub：https://github.com/zrzqbr/Tencent-Channel
- 主分支：main
- 本地 remote：origin = git@github-architect-summit:zrzqbr/Tencent-Channel.git
- Python 包版本：0.7.5（内容同步与 AI 巡检拆分、时间修正、待办分类明确化）

## 2. 当前生产快照

### 2.1 版本与服务

生产提交不在文档中固化，以服务器软链接为准：

~~~bash
ssh root@150.158.77.134 'readlink /srv/tencent-channel/app/current'
~~~

当前生产组件：

| 项目 | 状态 | 位置 |
| --- | --- | --- |
| 管理后台 | active | qq-channel-admin.service |
| 内容自动同步 | active / enabled | qq-channel-sync.timer |
| 自动巡检 | inactive / disabled | qq-channel-monitor.service |
| Nginx | active | /etc/nginx/conf.d/tencent-channel.conf |
| 健康检查 | 正常 | https://tencent.ruitcarch.cloud/healthz |
| QQ 官方 CLI | 已登录 | HOME=/srv/tencent-channel/home |
| HTTPS | 已启用 | Let's Encrypt，Certbot timer active |
| 应用监听 | 正常 | 127.0.0.1:8787 |

网页与巡检都以 txa_deployer:txa_deployer 运行，并且必须共用：

~~~text
HOME=/srv/tencent-channel/home
~~~

### 2.2 当前生产安全模式

~~~text
delete_mode = dry_run
auto_delete_duplicates = false
auto_delete_policy_violations = false
QQ_GUARD_MANUAL_DELETE_ENABLED = true
~~~

含义：

- 自动删除全部关闭，巡检不会自动删帖。
- 网页人工编辑、移动和删除入口已启用，登录管理员提交后直接执行并自动留痕。
- 没有用户明确授权时，不得切换 live 或打开自动删除。

### 2.3 最近巡检快照

~~~text
finished_at UTC = 2026-08-20T13:27:04.347880+00:00
北京时间 = 2026-08-20 21:27:04
scanned_feeds = 44
duplicates = 0
ai_reviewed = 44
ai_fallbacks = 0
ai_model = hy3
delete_mode = dry_run
~~~

频道内容每 30 分钟自动同步一次，但不执行 AI。AI 巡检只在管理员点击网页“立即巡检”后运行。

## 3. 生产路径

~~~text
SSH: root@150.158.77.134
应用根目录: /srv/tencent-channel/app
当前 release: /srv/tencent-channel/app/current
release 历史: /srv/tencent-channel/app/releases
共享虚拟环境: /srv/tencent-channel/app/venv
生产配置: /srv/tencent-channel/shared/config.json
生产数据库: /srv/tencent-channel/shared/data/guard.sqlite3
QQ CLI HOME: /srv/tencent-channel/home
QQ CLI 凭据: /srv/tencent-channel/home/.qqcli/.env
环境变量文件: /etc/tencent-channel.env
Systemd: /etc/systemd/system/qq-channel-*.service
Nginx: /etc/nginx/conf.d/tencent-channel.conf
~~~

配置、数据库、QQ 凭据和环境文件都在 release 目录之外，发布代码时不得覆盖。

## 4. 当前频道与栏目

### WorkBuddy

~~~text
guild_id: 94340771786936363
实用文章: 739414550
问答与交流: 739414561
官方资讯: 739418183
每周一问: 739652699
~~~

### 腾讯云架构师峰会

~~~text
guild_id: 25087321787111415
问答与交流: 739825330
文章（自动分类入口）: 739789994
~~~

峰会“文章”配置在 auto_classify_channels。系统会先判断实用文章、精华、官方资讯、每周一问等类型，再产生栏目调整建议。

每周一问必须有井号话题。只有提问语义但没有 #话题，不能归为每周一问。

## 5. 业务流程

~~~text
QQ 频道新内容
  → 每 30 分钟通过官方 CLI 同步标题、正文、作者、栏目、话题和图片
  → 管理员点击“立即巡检”后读取本地已同步内容
  → 同作者、同频道、同栏目、连续且完全一致的确定性去重
  → 敏感词、联系方式、外链、灌水和栏目规则初审
  → Youtu-VITA 提取图片事实、OCR、二维码和视觉风险
  → Hy3 综合文字、图片、规则和栏目上下文
  → 生成分类、风险、建议动作、理由和证据
  → 按“保留 / 调整栏目 / 人工核对 / 删除”进入管理后台
  → 管理员终审
  → SQLite 审计记录
~~~

## 6. 最近完成的关键修改

### 0.7.5：同步与 AI 巡检彻底拆分

- “全部内容”的“立即同步”只更新腾讯频道帖子、文章、正文和图片，不执行规则审核、Hy3 或 Youtu-VITA。
- 新增 `qq-channel-sync.timer`，每 30 分钟执行一次增量内容同步；`qq-channel-monitor.service` 继续禁用。
- 每轮增量同步会限量补齐旧记录的正文和图片；遇到腾讯频率限制即停止补齐并留到下轮，不阻塞正常增量同步。
- “立即巡检”只分析后台已经同步的内容，不再同时向腾讯频道拉取数据，也不在巡检阶段执行治理操作。
- “今日处理”明确拆为“需要删帖、调整栏目、需要核对、可以保留”，删除“先处理这些”这一混合分类。
- 全部内容优先使用腾讯 Unix 时间戳，修复北京时间字符串被重复加 8 小时的问题。

### 0.7.2：今日处理改为内容卡片

- 保留四类管理员任务，但移除重复标签、密集表格和右侧审核抽屉。
- 每条待办直接显示正文摘要、处理原因、建议下一步和可展开的判断依据。
- 保留、移动、查看完整内容和删除均可在同一张卡片直接操作，处理后返回当前任务。
- 腾讯秒级或毫秒级时间戳统一显示为北京时间。

### 0.7.3：原帖直达与 AI 巡检结果可见

- 左侧导航将“全部内容”置于“今日处理”上方，内容总览和待办入口顺序与管理员工作流一致。
- “今日处理”的审核卡片从已同步内容缓存中自动带出原帖地址，管理员可直接打开原帖核对，不需要查找帖子 ID。
- 手动点击“立即巡检”后，进度区明确展示文字 AI 判断、图片检查和需要人工补充的数量。
- 巡检报告新增图片检查完成与图片检查失败统计；旧数据库启动时自动补齐字段，保持现有生产数据库兼容。

### 0.7.4：管理员能直接看懂 AI 结论

- 工作台卡片统一只显示“AI分析出了什么”和“建议下一步”，不再显示“风险提示与处理建议不同”等技术解释。
- 下一步统一使用“删除帖子、调整栏目、确认保留、人工核对”等明确动作。
- “查看并处理”页面按 AI 结果、分析理由、图片检查、原帖正文、下一步操作排列，删除重复的系统检查步骤和风险分数说明。
- AI 返回的 Markdown 星号、短横线和数字列表会整理为普通中文短句，图片检查说明可直接阅读。

### 0.6.0：腾讯官方 Skill 全能力入口

- 官方 Skill 基线为 `tencent-channel-community 1.1.5`，后台“官方能力”页面从服务器 CLI 实时读取 Schema，因此腾讯后续新增命令可自动出现在能力目录。
- 覆盖频道与栏目、帖子与内容、评论与互动、成员与权限、通知与私信、运营快捷工具。
- 查询操作直接运行；真实写操作需 `QQ_GUARD_OFFICIAL_WRITES_ENABLED=true`，开启后登录管理员提交即执行并自动留痕。
- Token、Cookie、会话密钥、分页游标和 raw 字段不会显示在网页或审计详情。
- 涉及服务器文件路径的命令暂不接受路径输入，必须以后接入受控上传区，避免任意文件读取。
- 帖子列表现已跟随官方 `feed_attach_info` 分页，不再把第一页误认为全部结果。
- 网页手动巡检使用全频道时间线，完整翻页读取各栏目内容，并使用数据库帖子 ID 作为增量水位线。
- 全频道接口只返回栏目名称时，系统会自动映射到已配置的真实栏目 ID，兼容带表情和“频道名·栏目名”等名称。
- 接口遇到频率限制时只短暂重试，仍失败则结束本轮，避免反复消耗额度。
- 腾讯接口直接超时时同一命令最多重试一次，不再按栏目长时间重复等待；失败结果会在巡检状态中明确显示。
- 巡检新增本轮读取、新增、更新、缓存和累计收录五类统计。
- 新增“全部内容”页面，正常帖子和待审核内容统一显示，可直接编辑、移动、打开原帖或删除。
- 官方通知自动推送仍仅支持 OpenClaw；网站使用分页增量轮询，不伪装为实时推送。

### b91e587：后台工作台重构

- 导航改成今日待办、内容审核、栏目调整、处理记录、设置。
- 首页改成管理员任务视角。
- 增加巡检进度、AI 分析记录、栏目调整和审核抽屉。
- 引入本地 Remix Icon，避免外部 CDN。

### 2477116：建议明确化与 AI 一致性保护

- 每条内容展示问题类型、处理建议、原因、证据和分数构成。
- 新增“需人工核对”队列。
- 约束 AI 输出：
  - allow：0–24 分；
  - review：25–79 分；
  - delete_candidate：80–100 分且必须有高风险证据。
- “严重风险 95 分但建议放行”等矛盾结论由服务端校正。
- 删除建议没有高风险证据时降级为人工复核。
- 提示词版本升级为 2026-08-20.ai3。

### 3968b82：首页再次简化

- 首页首先显示“有没有问题”和“你要做什么”。
- 主按钮直接对应保留、移动、删除或查看后决定。
- 原帖摘要、证据、评分和其他处理方式默认收起。
- 核心字体、按钮和右侧审核卡片整体放大。
- 主要时间统一显示北京时间。
- 立即巡检完成后，最近巡检时间无需刷新即可更新。

## 7. 代码结构

| 文件 | 责任 |
| --- | --- |
| qq_guard/web.py | Flask 路由、审核队列、建议文案、北京时间过滤器 |
| qq_guard/tencent_monitor.py | 频道轮询、内容读取、审核流水线和落库 |
| qq_guard/tencent_cli.py | 官方 CLI 封装、频控重试、删除和移动 |
| qq_guard/ai_review.py | Hy3/VITA、缓存、结构化输出和一致性校正 |
| qq_guard/classifier.py | 栏目分类 |
| qq_guard/moderation.py | 敏感词、联系方式、外链、质量和栏目规则 |
| qq_guard/placement.py | 可解释栏目调整建议 |
| qq_guard/admin_store.py | SQLite 查询、审核、删除、移动和审计 |
| qq_guard/scan_control.py | 跨进程巡检锁与进度 |
| qq_guard/templates/dashboard.html | 今日处理工作台 |
| qq_guard/templates/review_detail.html | 完整审核详情 |
| qq_guard/static/admin.css | 后台主样式 |
| qq_guard/static/admin.js | 巡检进度、时间更新和批量操作 |
| config.example.json | 无秘密的配置模板 |
| tests/ | 分类、规则、AI、巡检、删除、移动和网页测试 |

## 8. 本地开发

仓库目录：

~~~text
/Users/raelzhang/Documents/Codex/2026-08-19/qq/outputs/qq-channel-content-guard
~~~

开始前：

~~~bash
cd /Users/raelzhang/Documents/Codex/2026-08-19/qq/outputs/qq-channel-content-guard
git status --short
git branch --show-current
git pull --ff-only origin main
~~~

安装：

~~~bash
python3 -m venv .venv
.venv/bin/pip install -e '.[web,qq]'
cp config.example.json config.json
~~~

测试：

~~~bash
.venv/bin/python -m unittest discover -s tests -v
~~~

交接前最后一次结果：87 tests OK。

本地 config.json 已被 .gitignore 忽略。不要把生产凭据写入测试文件。本地网页只用于界面和单元测试，不要在本机再次执行 tencent-channel-cli login，原因见第 11 节。

## 9. GitHub 提交方式

### 9.1 正常流程

先保护用户工作区：

~~~bash
git status --short
git pull --ff-only origin main
~~~

修改后：

~~~bash
git diff --check
.venv/bin/python -m unittest discover -s tests -v
git status --short
git diff --stat
~~~

只添加本次明确文件：

~~~bash
git add path/to/changed-file.py path/to/template.html
git commit -m "Concise English description"
git push origin main
git rev-parse HEAD
~~~

当前 remote 使用本机 SSH alias github-architect-summit。直接从现有仓库 push，不要修改 remote。

### 9.2 禁止事项

- 不要 force push。
- 不要 git reset --hard。
- 不要覆盖用户未提交改动。
- 不要提交 config.json、.env、SQLite、日志、QQ 凭据或 API Key。
- 不要把服务器秘密复制到 GitHub。

## 10. 服务器发布

### 10.1 发布前

~~~bash
git status --short
.venv/bin/python -m unittest discover -s tests -v
git rev-parse HEAD
ssh -o BatchMode=yes root@150.158.77.134 'systemctl is-active qq-channel-admin.service nginx; systemctl is-enabled qq-channel-sync.timer qq-channel-monitor.service || true'
~~~

### 10.2 创建不可变 release

在本地仓库执行：

~~~bash
set -o pipefail
release_sha=$(git rev-parse HEAD)

git archive --format=tar "$release_sha" | \
ssh -o BatchMode=yes root@150.158.77.134 "set -e
release_dir=/srv/tencent-channel/app/releases/$release_sha
install -d -o txa_deployer -g txa_deployer \"\$release_dir\"
tar -xf - -C \"\$release_dir\"
chown -R txa_deployer:txa_deployer \"\$release_dir\"
/srv/tencent-channel/app/venv/bin/pip install --no-deps --quiet \"\$release_dir\"
ln -sfn \"\$release_dir\" /srv/tencent-channel/app/current.new
mv -Tf /srv/tencent-channel/app/current.new /srv/tencent-channel/app/current
install -m 0644 "\$release_dir/deploy/systemd/qq-channel-sync.service" /etc/systemd/system/qq-channel-sync.service
install -m 0644 "\$release_dir/deploy/systemd/qq-channel-sync.timer" /etc/systemd/system/qq-channel-sync.timer
systemctl daemon-reload
systemctl enable --now qq-channel-sync.timer
systemctl restart qq-channel-admin.service
systemctl disable --now qq-channel-monitor.service
sleep 3
readlink /srv/tencent-channel/app/current
systemctl is-active qq-channel-admin.service
systemctl is-active qq-channel-sync.timer
systemctl is-enabled qq-channel-monitor.service || true
curl -fsS http://127.0.0.1:8787/healthz
"
~~~

关键点：

- set -o pipefail 必须保留，防止 SSH 端失败而本地管道显示成功。
- 不覆盖持久配置和数据库。
- release 目录使用完整 Git SHA。
- 管理后台从 current 加载并使用共享 venv，所以发布时必须安装 release。

### 10.3 发布后验证

~~~bash
release_sha=$(git rev-parse HEAD)

ssh -o BatchMode=yes root@150.158.77.134 "
set -e
test \"\$(readlink /srv/tencent-channel/app/current)\" = \"/srv/tencent-channel/app/releases/$release_sha\"
systemctl is-active qq-channel-admin.service nginx
systemctl is-active qq-channel-sync.timer
systemctl is-enabled qq-channel-sync.timer
systemctl is-enabled qq-channel-monitor.service || true
curl -fsS http://127.0.0.1:8787/healthz
sudo -u txa_deployer env HOME=/srv/tencent-channel/home tencent-channel-cli login status
journalctl -u qq-channel-admin.service --since '10 minutes ago' --no-pager | tail -120
"
~~~

再用已登录网页做无破坏性验证：

1. 全部内容的帖子时间与腾讯频道一致，不被重复加 8 小时；
2. 全部内容显示每 30 分钟自动同步，立即同步完成后明确显示“AI 分析 0 条”；
3. 立即巡检只分析已同步内容，并显示文字与图片 AI 数量；
4. 今日处理显示“需要删帖 / 调整栏目 / 需要核对 / 可以保留”；
5. 栏目错投显示目标栏目和确认移动按钮；
6. 生产验收只执行同步与巡检，不点击任何真实编辑、移动或删除按钮。

### 10.4 回滚

只改 current 软链接不够，因为管理后台使用共享 venv：

~~~bash
old_sha='<已验证的旧完整SHA>'

ssh -o BatchMode=yes root@150.158.77.134 "set -e
old_dir=/srv/tencent-channel/app/releases/$old_sha
test -d \"\$old_dir\"
/srv/tencent-channel/app/venv/bin/pip install --no-deps --quiet \"\$old_dir\"
ln -sfn \"\$old_dir\" /srv/tencent-channel/app/current.new
mv -Tf /srv/tencent-channel/app/current.new /srv/tencent-channel/app/current
systemctl restart qq-channel-admin.service
systemctl disable --now qq-channel-sync.timer
systemctl disable --now qq-channel-monitor.service
sleep 3
curl -fsS http://127.0.0.1:8787/healthz
"
~~~

回滚代码不会自动回滚数据库或生产配置。数据库变更前必须备份并保持向后兼容。

## 11. QQ CLI 授权：高风险踩坑点

生产凭据：

~~~text
/srv/tencent-channel/home/.qqcli/.env
owner: txa_deployer:txa_deployer
mode: 600
~~~

同一个 QQ AI Connect 账号重新扫码可能使上一枚 Token 失效。因此：

- 服务器是唯一正式授权源。
- 不要为了测试在本机重新 login。
- 本机与服务器分别扫码会互相踢下线，并出现：

~~~text
业务错误 (retCode=8011): {"message":"invalid ai token","retcode":100051}
~~~

检查：

~~~bash
ssh root@150.158.77.134 \
  'sudo -u txa_deployer env HOME=/srv/tencent-channel/home tencent-channel-cli login status'
~~~

确实失效时，只在服务器重新授权：

~~~bash
ssh -t root@150.158.77.134 \
  'sudo -u txa_deployer env HOME=/srv/tencent-channel/home tencent-channel-cli login'
~~~

用户必须在腾讯官方页面亲自确认。完成后：

~~~bash
ssh root@150.158.77.134 '
sudo -u txa_deployer env HOME=/srv/tencent-channel/home tencent-channel-cli login status
systemctl restart qq-channel-admin.service
systemctl disable --now qq-channel-monitor.service
'
~~~

授权不能保证永久有效，不要绕过腾讯安全机制。

## 12. SSH 与秘密

SSH 凭据不在仓库。如果环境没有可用 SSH key，向用户索取或查看用户单独保存的资料：

~~~text
/Users/raelzhang/Documents/Codex/峰会官网服务器接入资料-2026-08-19.md
~~~

该文件可能包含敏感信息，只能按用户授权用于连接，不能复制到仓库、日志或回复。

/etc/tencent-channel.env 当前变量名：

~~~text
PORT
QQ_GUARD_ADMIN_PASSWORD_HASH
QQ_GUARD_CONFIG
QQ_GUARD_MANUAL_DELETE_ENABLED
QQ_GUARD_SECRET_KEY
TENCENT_TOKENHUB_API_KEY
~~~

规则：

- TokenHub Key 只放服务器环境文件。
- QQ 凭据只放共享 QQ CLI HOME。
- 管理密码只保存 Werkzeug 哈希。
- 诊断只输出变量名、权限和状态，不输出值。

## 13. 常见故障

### invalid ai token / retCode=8011 / 100051

QQ 官方 CLI 授权失效，不是 Hy3 的 TokenHub Key。按第 11 节处理。

### retCode=153 / 频率上限

接口调用过快。网页巡检使用 tencent-scan.lock 保证同一时间只有一轮任务。等待一段时间后再手动点击巡检。

### 最近巡检看似没更新

曾有两个原因，均已修复：

1. 数据库存 UTC，页面曾直接显示；
2. AJAX 完成后顶部时间曾不重绘。

现在使用 cn_time 和浏览器 Asia/Shanghai 格式化。再次出现时检查浏览器缓存与 /scan/status/<job_id> 的 finished_at。

### VITA 502

图片内容转人工复核，Hy3 可继续文字审核。不要因 VITA 暂时不可用自动处置。

### 页面仍是旧版

~~~bash
git rev-parse HEAD
ssh root@150.158.77.134 'readlink /srv/tencent-channel/app/current'
ssh root@150.158.77.134 'systemctl status qq-channel-admin.service --no-pager'
~~~

再硬刷新浏览器。静态资源没有内容哈希，缓存旧 CSS/JS 是待优化项。

### 删除或移动失败

依次检查：

1. CLI login status；
2. 管理权限；
3. 内容是否已删除或移动；
4. 腾讯频率限制；
5. tencent_moderation_findings 的 delete_status/delete_error；
6. admin_audit_actions 的审计记录。

## 14. 数据库与审计

主要表：

~~~text
admin_audit_actions
ai_review_cache
ai_vision_cache
content_events
manual_delete_requests
moderation_review_actions
tencent_duplicate_actions
tencent_feed_cache
tencent_moderation_findings
tencent_scan_runs
~~~

交接快照：

~~~text
tencent_moderation_findings = 73
tencent_scan_runs = 429
admin_audit_actions = 105
~~~

数字只用于确认不是空库。不要手工删除生产数据来修页面。历史状态修复应使用可测试、幂等、可审计的迁移或命令，并先备份数据库。

## 15. 待优化事项

### P0：安全与准确性

1. 继续观察 2026-08-20.ai3 的误判率，重点看“栏目错投 + 外链”是否被放大成内容违规。
2. 删除候选必须有明确问题类型、证据和高风险理由；否则降级复核。
3. 生产保持 dry_run，除非用户明确批准自动删除。
4. 为 QQ 授权失效增加后台健康提示与授权引导。

### P1：后台易用性

1. 已完成桌面与 390px 手机视口验证；后续新增页面仍需保持无横向溢出和操作文字不换行。
2. AI 原始理由仍可能出现英文、内部分类名（official_news）或低质量词（test），需中文标准化或强化校验。
3. 状态色需更明显，但不能只依赖颜色。
4. 静态资源已带版本参数；后续发布修改 CSS/JS 时同步更新版本号。
5. 用更日常的语言解释“需人工核对”和“AI 未完整分析”。

### P1：巡检调度

1. 自动 AI 巡检已关闭；管理员点击“立即巡检”后只分析已同步内容。只有明确恢复自动 AI 模式时才设置 `QQ_GUARD_AUTO_SCAN_ENABLED=true` 并启用 monitor 服务。
2. `qq-channel-sync.timer` 每 30 分钟只做增量内容同步；继续观察实际额度消耗和分页完整性。
3. 增加巡检历史页：开始、结束、耗时、内容数、AI 数、降级数和失败阶段。

### P1：数据与队列

1. 检查待办数量是否因策略版本或重复巡检增长；同一帖子最新策略应替代旧待办。
2. 增加自动数据库备份、保留周期和恢复演练。
3. 引入正式 schema 迁移机制。

### P2：权限与产品化

1. 当前单管理员，未来增加审核员、管理员、只读角色。
2. 建议用户后续改用更强管理密码并建立轮换流程。
3. 增加审计导出与按频道、处理人筛选。
4. 增加服务退出、连续 AI 降级、QQ 授权失效和删除失败告警。

## 16. 下一位 AI 的起步顺序

1. git status，确认没有未知改动。
2. 阅读本文、README、审核策略和部署文档。
3. 跑完整测试。
4. 只读检查生产 release、服务、QQ 登录和最近巡检。
5. 抽查保留、栏目调整、人工核对、删除候选四类队列。
6. 选择一个明确 P0/P1 问题，在本地实现并补测试。
7. 提交 GitHub 后按完整 SHA 发布。
8. 发布后只做无破坏验证；删除、移动和 live 模式需用户明确授权。

## 17. 可开始开发的判断标准

下一位 AI 应能回答：

- 当前生产 SHA 和服务状态是什么？
- 生产配置、数据库和 QQ 凭据分别在哪里？
- 为什么不能在本机重新扫码？
- 普通内容是否自动删除？
- 如何测试、提交、发布、验证与回滚？
- Hy3 与 VITA 分别负责什么？
- 每周一问为什么必须有井号话题？
- 当前最高优先级优化是什么？

任何答案不确定时，先重新阅读对应章节，不要猜测生产配置。
