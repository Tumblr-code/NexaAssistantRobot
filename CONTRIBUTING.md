# Contributing / 贡献

Use Python 3.10+ and `uv sync --frozen --extra dev`. Before a PR run Ruff check
and format, Pyright, the complete pytest suite with coverage >=75%, dependency
audit and `uv build`. CI tests Python 3.10 and 3.12. Tests use mocks and temporary
state; do not connect a real Telegram bot, perform moderation, or run gateway
probes. Keep credentials and runtime data outside Git. Change bilingual core
docs together, preserve behavior unless explicitly scoped, and review schema
compatibility before a migration. Release only the exact checked commit; verify
wheel installation/import and all asset hashes. Publishing does not deploy.

使用 Python 3.10+，执行 `uv sync --frozen --extra dev`。PR 前完成 Ruff 检查与格式、Pyright、全套 pytest（覆盖率至少 75%）、依赖审计与 `uv build`。CI 覆盖 Python 3.10 / 3.12。测试仅使用模拟和临时状态，不连接真实机器人、不管理群、不跑外部网关探针。密钥与运行数据不得入库，核心双语文档同步修改。Schema 变更先审兼容性，仅对已通过检查的完整提交发布，并校验 wheel 安装/导入与资产摘要；发布不等于部署。

Existing unique branches and historical tags are retained unless separately
reviewed. Keep pull requests small, explain validation and residual limits.
保留已有独有分支与历史 tag，未经专项审阅不删除；PR 保持范围明确，说明验证与局限。
