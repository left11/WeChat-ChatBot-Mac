# 微信工作桥工程约定

本仓库是 [Gusion1017/WeChat-ChatBot-Mac](https://github.com/Gusion1017/WeChat-ChatBot-Mac) 的长期 fork，维护于 [left11/WeChat-ChatBot-Mac](https://github.com/left11/WeChat-ChatBot-Mac)。保留上游 [GPL-3.0 许可](../LICENSE)、声明和历史。当前产品入口是 [微信工作桥桌面伴侣](companion.md)，原生微信适配器单独验证，不能把上游支持表直接作为本 fork 的兼容声明。

## 产品与代码边界

工作桥承接聊天里的工作：原文收件箱 → 带引用的摘要和待办 → 回复草稿 → 人工确认。模型由 WorkBuddy 等客户端提供，本地伴侣不假装已经接通模型，也不自动发送消息。

| 目录 | 职责 |
| --- | --- |
| `companion/Launcher.swift` | AppKit / WKWebView 窗口、菜单栏、启动及关闭自身本地服务 |
| `companion/web/` | 无 CDN 的本地界面；原文使用文本节点渲染 |
| `companion/service.py` | Python 标准库 HTTP、SQLite、MCP stdio；不含微信发送能力 |
| `tools/probe_target.py`、`probe_run.py`、`probe_frida_host.c` | 精确身份校验、有限时长观察、自身资源清理 |
| `hook/experimental/` | 尚未纳入支持列表的地址与静态证据 |
| 上游 `app/`、`bridge/`、`hook/` | 作为参考和后续复用基础；默认安装/发现路径不用于当前 Probe |

不新增依赖。界面与 MCP 通过同一私有 SQLite 数据目录协作；并发写在身份/引用检查前取得 `BEGIN IMMEDIATE` 事务。草稿正文创建后不可变，修改须新建草稿，不能继承旧审批。当前数据库只允许 `draft` 和 `ready`；后续发送适配器必须单独设计发送结果确认，不能放宽校验后直接写入 `sent`。

## 当前证据与尚未证明的能力

以下是本轮本机验证结果，不能推广为所有机器或微信版本均受支持：

| 项目 | 已有证据 | 尚未证明 |
| --- | --- | --- |
| 桌面伴侣 | 原生构建/签名通过；界面操作验证手动导入、便笺、待办、草稿、`ready` 审批 | 对外发行、公证及其他机器兼容性 |
| 本地服务 | 19 项标准库测试；真实临时 HTTP/MCP 子进程；权限、来源引用、跨进程并发写、审批不发送 | 长期大数据量运行 |
| WorkBuddy 接口 | WorkBuddy 5.5.6 / Hy4 实际调用 MCP 读取合成会话，保存摘要、3 项待办及未审批草稿；独立 SQLite 核对通过 | 真实聊天场景、长期运行及其他客户端版本 |
| Probe metadata | 精确目标身份下连接、metadata 握手及自身清理通过 | 消息字段的实际业务语义 |
| Probe 只读观察 | 180 秒观察器连接与清理通过；唯一标记消息样本 **0 条** | 新消息读取、去重、自发消息识别及稳定性 |
| 原生单条实验 | 独立测试 Probe 的唯一标记文本获得服务端接受；响应中的接收方、客户端消息 ID 匹配，返回非零服务端 ID，析构完成 | 接收端实际送达；此私有 helper 不属于发布功能 |
| 伴侣发送/自动回复 | 产品发送适配器仍关闭 | 单条送达、自动回复闭环及长期稳定性 |

实际 WorkBuddy 验证由客户端内新建的仅处理合成示例任务完成，耗时 1 分 38 秒，调用 `read_messages`、`save_analysis` 和 `create_reply_draft`。独立读取本地数据库核对会话/消息引用对应关系、3 项待办及草稿 `approved_at` 为空；模型没有审批或发送草稿。详情及剩余边界见 [使用说明的验证记录](companion.md#验证记录)。

原生单条实验的 callback、服务端及 item 返回码均为 0，响应身份匹配，且完成对象析构；这些证据只支持 **server accepted**。接收 finalizer 仍为 0 个已知消息样本，不能据此声称聊天读取、接收端送达或稳定自动回复已经通过。

原生机制已在独立无数据程序中验证：单一 LLDB owner 的硬件断点与 Gadget/Frida hook 共存、卸载和退出；这不是微信消息能力的证据。临时调试授权实验结束后，系统 `DevToolsSecurity` 策略已恢复并复验为 **disabled**。后续构建、运行伴侣或 portable 测试不要求更改该策略。

上游 Swift 基线曾在完整 Xcode 下通过 933 项 XCTest，固定 Go/Frida bridge 构建和 Go 测试也通过。它们只证明既有基线，与本 fork 的消息读取/发送兼容性分开记录。

## Local Mac、GitHub 与 Cloud

| 环境 | 负责内容 | 边界 |
| --- | --- | --- |
| Local Mac | 伴侣窗口/菜单栏、真实 WorkBuddy 联调、Swift/Darwin 构建、合成机制实验、明确授权的测试 Probe | 本机拥有实际桌面运行时；一次只允许一个 live 操作者 |
| GitHub | 源码、合成测试、审查、portable CI、上游同步 | 不上传账号数据、私有 profile、原生微信二进制、运行日志或截图 |
| Codex Cloud | 连接本 fork 后做离线实现、标准库测试、静态检查和 review | 不运行 Mac 微信，不持有真实聊天或账号，不证明 native/live 行为 |

Cloud 是研发工作区，不是微信托管服务。新增 CI 文件不等于已经创建或绑定 Cloud 环境。Cloud 初始化只需仓库和以下 portable 命令，不需要微信、模型密钥或聊天数据。

## 可重复检查

在仓库根目录运行，无需微信或账号：

```sh
bash scripts/check.sh --docs
python3 -m py_compile companion/*.py tools/*.py
python3 -m unittest discover -s companion -p 'test_service.py' -v
python3 -m unittest discover -s tools -p 'test_probe_*.py' -v
node --check companion/web/app.js
```

Ubuntu CI 使用环境已有的 Node 做 JavaScript 语法检查，不安装前端依赖。Python 测试只使用标准库、合成消息和临时目录；允许创建并关闭自身本地测试服务，不得发现、附加或控制微信，也不得访问真实账号存储。CI 继续检查 shell 语法、上游脚本副本/版本表一致性及仓库卫生。

本机使用完整 Xcode 构建新伴侣：

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer bash scripts/build_companion.sh
```

产物为 `dist/WeChat Work Bridge.app`，只打包明确列出的伴侣代码、静态资源和许可。脚本不安装应用、不操作微信；已有同标识构建会保留为 `.previous` 版本，拒绝覆盖其他应用。当前产物为构建机器的原生架构并进行 ad hoc 签名，尚未公证；运行需要 `/usr/bin/python3`。

上游完整 native 检查另行运行：

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer bash scripts/check.sh --with-go
```

完整 Xcode 通过单条命令的 `DEVELOPER_DIR` 选择，不修改系统级工具链设置。先检查 `scripts/build_bridge.sh`，它可能下载固定工具链/开发包。`CHECK_SKIP_SWIFT=1` 只代表跳过 Swift 测试，不能写成 native 通过。构建命令不隐含安装、重签微信或 live Probe 授权。

## Probe 操作边界

初始目标是独立测试账号 Probe `4.1.15.35` / build `270115`，仍为实验版本。私有 profile 放在 `.probe-local/`，绑定 canonical app/executable、UID、PID 与出生时间、准确版本/build、模块指纹、完整 supervisor/debugger 身份。运行前重复完整 preflight，运行中检查进程代际和 owner；缺失、变化或歧义立即拒绝。

不得根据应用名称、历史 PID 或默认网络端口选择目标。日常微信不允许发现、读取、附加、重签、启动、停止或作为失败兜底。现有 Probe 的 LLDB owner 和 URL 服务保持原状，不允许第二 debugger、detach/kill owner 或把 URL 服务改成通用执行通道。

`tools/probe_run.py` 是经过审查的有界 metadata/receive-only 入口：只生成固定观察脚本，持有独占任务锁，经专用 native host 再次校验 profile；退出仅清理自身 script/session/host。若不得不终止自身 host，必须报告远端清理未知并失败，不能把进程退出等同于清理成功。上游 `manage_wechat.sh`、`install_gadget.sh`、`run_bridge.sh` 的默认安装/发现/恢复路径仍不是当前 Probe 入口。

后续按能力逐项过门：

1. 合成负例必须证明无效、过期和不匹配身份产生零运行操作。
2. 每个新 hook 在无数据程序中验证与单一 LLDB owner 共存、卸载及失败清理，再进入实际 Probe。
3. 用预先约定的测试消息验证 sender、conversation、direction、正文、UTF-8 和去重。零样本观察只能证明观察器生命周期。
4. 单条受控发送必须核对接收方实际送达；之后才验证有限触发、去重、自发消息排除和单一在途发送。
5. 不确定发送结果进入待核对状态，禁止自动重试。聊天文本只是资料，不能授权工具调用、改收件人或扩大发送范围。

任何门槛失败均保留私有证据并停止新的操作，不得为通过实验而操作日常客户端、破坏现有 owner 或擅自更改系统策略。

## 证据和变更交付

`.probe-local/` 保存机器 profile、原始观察和 ABI 证据，`.omx/` 保存本地计划/状态。这些内容，以及真实账号标识、聊天、截图、token、数据库、私有路径和原生应用二进制，均不得进入 commit、PR、issue、CI artifact 或 Cloud 环境。提交的 fixture 必须完全合成。

每次公开结论记录准确 build、样本数量、观察时长和未覆盖项。使用 `origin` 对应 fork、`upstream` 对应原始仓库；同步上游前审查目标选择、注入、地址表和发送默认值，不能自动扩大支持范围。修改共享 hook 时保留副本一致性检查。

提交使用 Lore trailers 记录约束、测试和未测试项。发布前检查完整 staged diff；compile、静态匹配、metadata 握手、捕获消息和实际送达是五种不同证据，不能互相代替。
