# Telegram 机器人资料

目标机器人：`@NexaAdminRobot`

## 名称

```text
Nexa 群管助手
```

## 简短介绍

```text
Nexa 官方群管理审核员：GPT-5.5 智能识别广告与刷屏，自动删除并警告；第 3 次直接封禁，并支持安全透明的群抽奖。
```

## 详细说明

```text
Nexa Guardian 是 @NexaStoreGroup 的官方群管理与安全审核员。

• GPT-5.5 审核文字、图片及视频封面
• 自动清理广告、诈骗、恶意推广与刷屏；第 3 次警告直接封禁
• 普通聊天静默处理；仅可疑消息显示审核状态
• 持续学习高置信广告特征，提升后续拦截效率
• 极简管理入口：状态、删除、封禁与群抽奖
• 私聊支持状态汇总、群历史清理及长期不活跃成员安全预览
• 抽奖支持关键词报名、自动置顶、定时开奖、取消及重抽
• 群内机器人回复约 30 秒后清理；管理员私聊回复永久保留
• 不保存群消息原文、图片或模型推理

发送 /help 查看管理命令。

官方服务：
Nexa Store 商城：https://nexastore.cn
Nexa · ModelPass：https://openai.nexastore.cn
Nexa · GiftPass：https://premium.nexastore.cn
```

服务启动时会把以上名称、简短介绍、详细说明和命令菜单同步为 Telegram 的默认资料与
`zh` 本地化资料。头像仍由 BotFather/Telegram 账号资料手工上传，不在启动时重复覆盖。

## 头像资产

- `assets/nexa-admin-robot-avatar.png`：生成的无损原图。
- `assets/nexa-admin-robot-avatar.jpg`：Telegram 静态头像上传版。

生成方式：OpenAI 内置图像生成。

生成提示词：

```text
Create an original premium Telegram moderation bot avatar for “Nexa Guardian”. Output a square 1024×1024 raster image designed for Telegram’s circular profile crop. Center a bold, distinctive geometric guardian emblem: a clean shield-like outer silhouette fused with a subtle speech-bubble notch and an abstract forward-moving N-shaped negative space. The mark should communicate group protection, anti-spam, calm authority, and fast moderation without using a generic robot face. Deep midnight navy background; luminous electric cyan and cobalt-blue emblem; one tiny warm amber status accent used sparingly. Crisp flat vector-inspired geometry rendered as polished high-resolution raster, restrained subtle depth, strong contrast, balanced symmetry with one purposeful asymmetric cut, generous safe margin around the emblem, legible at 32 px. No words, no letters drawn explicitly, no Telegram paper-plane logo, no watermark, no mockup, no border ring, no busy background, no photorealism, no mascot character.
```
