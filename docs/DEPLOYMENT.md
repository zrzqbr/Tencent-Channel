# 部署与域名

## 正式入口

- 频道管理后台：`https://tencent.ruitcarch.cloud`
- 服务器公网地址：`150.158.77.134`
- 峰会官网：`https://ruitcarch.cloud`，与频道管理后台相互独立

## 当前状态（2026-08-19）

- DNSPod 已添加 `tencent` 的 A 记录，指向 `150.158.77.134`，TTL 为 600 秒。
- 现有服务器仍把该主机名的 HTTP 请求转回峰会官网。
- 现有 HTTPS 证书只覆盖 `ruitcarch.cloud` 和 `www.ruitcarch.cloud`，尚未覆盖 `tencent.ruitcarch.cloud`。
- 内容审核与删除继续保持 `dry_run`，域名配置不会开启真实删帖。

## 上线前必须完成

1. 为 `tencent.ruitcarch.cloud` 创建独立 Nginx 站点，不复用峰会官网的站点根目录。
2. 签发并安装包含 `tencent.ruitcarch.cloud` 的 HTTPS 证书。
3. 将管理后台服务只监听本机端口，由 Nginx 反向代理；腾讯凭据仅保存在服务端。
4. 上线登录、管理员与审核员权限、CSRF 防护、限流、会话过期和操作审计。
5. 验证域名、证书、健康检查和回滚流程后，再开放后台入口。
6. 真实删帖仍需单独修改 `delete_mode` 和对应自动删除开关，不与网页上线联动。

## 验收标准

- `https://tencent.ruitcarch.cloud` 的证书域名校验通过。
- HTTP 自动跳转到同一子域名的 HTTPS，不跳转到峰会官网。
- 未登录用户不能读取频道内容、审核结果或策略配置。
- 所有分类、审核、去重和删除动作均保留理由、策略版本、操作者和时间。
- 删除开关默认关闭，测试环境只能产生 `detected_only` 或 `dry_run` 记录。
