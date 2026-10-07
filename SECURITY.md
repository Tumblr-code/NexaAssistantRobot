# Security / 安全

Do not disclose tokens, sessions, raw group messages, database copies or
production configuration in issues, pull requests or release assets. Report a
suspected vulnerability through GitHub private vulnerability reporting when
available, otherwise contact the repository owner through an existing private
channel. Include a redacted reproduction, affected commit and expected impact;
do not exploit a production group or perform destructive moderation.

Do not weaken group/admin allowlists, bot identity checks, filesystem protection,
confirmation gates or backup integrity checks. `DRY_RUN` is not a sandbox for
manual commands. Treat Telegram and model-provider credentials as independent
secrets. Rotate an exposed credential through its provider and preserve audit
evidence without republishing the value. MIT licensing is in `LICENSE`.

不得在 issue、PR 或发布资产中披露 Token、Session、群消息原文、数据库和生产配置。优先使用 GitHub 私密漏洞报告；未启用时通过已有私密渠道联系仓库所有者。提供脱敏复现、受影响提交与影响范围，不能在真实群利用问题或执行破坏性管理。

保留群/管理员白名单、身份校验、文件系统保护、确认门槛和备份完整性检查。`DRY_RUN` 不是手工命令沙箱，Telegram 与模型凭据独立保护。凭据泄露后通过上游轮换并保留脱敏审计，不再次传播值。许可证为既有 `LICENSE` 中的 MIT。
