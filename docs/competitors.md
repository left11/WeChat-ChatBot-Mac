# 微信与 WorkBuddy：竞品记录

资料获取：2026-11-15。以下为官方资料、公开项目和社区方案；除本项目的 WorkBuddy MCP 测试外，未安装或独立验证这些产品的当前兼容性。公开交付、厂商宣称和真实持续使用分别记录。

## 直接替代

| 产品或方案 | 公开能力 | 与工作桥的关系 |
| --- | --- | --- |
| [Agent for WeChat / ChatGPT for WeChat](https://chatgpt4wechat.aow.me/) | 浏览器微信 AI 机器人，宣传群聊、私聊、文件传输助手、AI 网页/API 和触发规则 | 已覆盖聊天问答与回复。官网直取为 JS 页面，功能与价格线索来自同站搜索索引；安装成功率、近期维护和当前收费未独立核验 |
| [Gusion1017 WeChat AI](https://github.com/Gusion1017/WeChat-ChatBot-Mac) | 公开 Mac 应用与源码，包括回复触发、冷却/工作时段、长期记忆及事实、决策、待办提取 | 本 fork 的技术参照。存在公开发布，不等于兼容本机微信或已验证长期稳定 |
| [WorkBuddy 聊天分享](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Task-Bar) | 微信/企业微信选中聊天，经系统分享进入记录卡片；一次最多 3 段，加入输入框后不会自动提交 | 官方已经提供“聊天 → AI 上下文”的手动路径，是必须比较的基线 |
| [知更 AI 接入 WorkBuddy 的社区教程](https://developer.cloud.tencent.com/article/2737844) | 2026-09-05 的作者文章提出微信 HTTP 接口接入技能/MCP，用于查询和发送 | 与拟议路线直接重合。本次仅取得摘要和目录，未核实安装、接口、费用或成功率；社区文章不代表腾讯官方保证 |

已有可下载项目和同路线方案，因此不能把“接入微信”称为市场空白。候选差异是更少配置、更少搬运、结果与来源对应、失败时保留明确状态；这些仍需在相同任务上比较。

## WorkBuddy 的接口边界

WorkBuddy 的 [OpenAPI](https://open.workbuddy.cn/docs/openapi) 已包含本地助理状态、发消息与历史、云任务、ACP 实时通道和产物，依赖应用凭据、OAuth 授权和相应权限。向 WorkBuddy 助理发消息与向微信联系人发消息是两个接口边界。

[第三方应用文档](https://open.workbuddy.cn/docs/third-party-app) 提供权限申请与审核说明，分享接入要求企业认证开发者、审核及 WorkBuddy 5.8.0 以上。普通桌面应用能否取得本项目需要的执行权限尚未验证；分享接入本身也没有证明微信原会话回发。

本项目已用 WorkBuddy 5.5.6 / Hy4 通过本地 MCP 完成合成会话读取、摘要/3 项待办写回和草稿创建。这验证了 MCP 路线，未验证上述 OpenAPI 授权或自动触发路线。WorkBuddy 的订阅和额度应以[官方定价页](https://www.workbuddy.cn/docs/workbuddy/Pricing)为准；工作桥未来的收费需要对应订阅之外的具体便利。

## 可借鉴的个人工作助手

| 产品 | 官方场景 | 可借鉴的产品选择 |
| --- | --- | --- |
| [Fyxer](https://www.fyxer.com/pricing) | Gmail/Outlook 整理、语气草稿、会议与跟进；由用户决定发送 | 将草稿与外发分开，把承诺和后续跟进作为持续价值 |
| [Lindy](https://www.lindy.ai/pricing) | 邮件、日程、会议跟进、每日简报及工具集成；对外动作等待批准 | 在任务结果旁交代下一步动作与批准边界 |
| [Granola](https://www.granola.ai/pricing) | 会议记录、跨会议上下文、会前简报、跟进材料及 MCP | 原始记录到后续工作的衔接，比单次摘要更值得比较 |

三者有公开付费入口，但本轮没有核实个人微信接入或真实付费人数。官网价格、客户证言和品牌列表不能替代微信用户的支付意愿。

下一轮比较应使用同一段获授权的工作聊天：记录安装步骤、取得上下文的操作数、返回结果的位置、人工核对和错误恢复。公开需求及首轮试用安排见[产品方向](product.md)。
