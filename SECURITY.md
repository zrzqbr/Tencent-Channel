# Security

请勿把以下内容提交到仓库、Issue、Pull Request 或聊天记录：

- QQ 机器人 AppSecret、Token 或登录凭据；
- 腾讯频道 CLI 的本机认证文件；
- 真实 `config.json`；
- `data/` 下的审计数据库；
- 用户原始内容、手机号、邮箱等个人信息；
- 服务器私钥或其他生产环境密钥。

仓库只提交 `config.example.json`。真实配置应保存在部署环境中，并通过最小权限控制读取。

发现安全问题时请私下联系仓库管理员，不要创建公开 Issue。
