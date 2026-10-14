# Agent Sync Bridge — Claude Code ↔ Codex

[![test](https://github.com/ShuoYang-cs/agent-sync-bridge/actions/workflows/test.yml/badge.svg)](https://github.com/ShuoYang-cs/agent-sync-bridge/actions/workflows/test.yml)

> **English summary.** You keep working in the normal Claude Code and OpenAI Codex chat windows (VS Code extension or CLI). When you say "同步给 Codex" or "sync this to Claude", the agent you are talking to writes a structured summary of the relevant part of the conversation into a local queue. The summary separates confirmed decisions, proposals, implemented work, verified results, unverified assumptions and pending tasks. On your next message to the other agent, a `UserPromptSubmit` hook injects the summary into its context. The receiving agent then acknowledges it with its own restatement. Each message is tracked as PENDING → DELIVERED → ACKNOWLEDGED (or FAILED, retryable). Ordinary chat never creates messages. Everything runs locally: stdlib Python, SQLite, no API keys, no extra model calls.

在 VS Code 里同时使用 Claude Code 和 Codex 时，两个 Agent 只共享代码文件，看不到彼此的对话。这个工具让你继续在原来的聊天窗口里工作；需要时说一句「同步给 Codex」或「同步给 Claude」，指定的那部分讨论就会被整理好交给另一个 Agent，并且可以查到对方是否真正读到、确认过。

- **只在你明确要求时同步。** 普通聊天、讨论“同步/异步”这类技术问题都不会触发。指令识别规则会排除问句、否定句、引号里的提及和长段粘贴，`send` 命令还会再核对一遍你的原话。
- **只同步你指定的范围。** 你可以说「把刚才关于 XX 的讨论同步给 Codex」或「把这次代码修改同步给 Claude」，不会默认复制整段聊天记录。
- **内容分类，不夸大。** 每条内容都要标明是已确认决定、建议、已实现、已验证（必须附证据）、未验证还是待办；声称“已验证”却没有证据时，`send` 会拒收。
- **状态如实。** PENDING 只表示消息已写入队列，不等于对方已读；只有接收方用自己的话复述要点后，状态才会变成 ACKNOWLEDGED。
- **纯本地运行，零依赖。** 只需要系统自带的 `python3`（3.9 及以上）和 SQLite，不需要 API Key，不发起额外的模型调用，不读取任何登录凭据，消息也不会进入 Git。

## 工作原理

```
 Agent A（发送方）                     ~/.agent-sync-bridge/bridge.db             Agent B（接收方）
 用户：同步给 B                          消息 · 事件日志 · 按会话的投递回执
 ① UserPromptSubmit hook 识别指令 ──►                                            用户：随便说点什么
 ② agent-sync skill：A 根据本会话                                                ③ UserPromptSubmit hook
    的真实内容生成结构化 JSON                                                       注入待接收消息 → DELIVERED
    asb send ────────────────────────► PENDING ─────────────────────────────►   ④ B 读完后
                                                                                   asb ack --summary "…"
                                        ACKNOWLEDGED ◄───────────────────────      → ACKNOWLEDGED
```

| 状态 | 含义 |
|---|---|
| `PENDING` | 已写入本机队列，对方还没读到 |
| `DELIVERED` | 已注入对方会话的上下文（或对方运行了 `asb show`），**不代表对方已理解** |
| `ACKNOWLEDGED` | 对方读完全文，并用自己的话复述后确认 |
| `FAILED` | 被拒收，或 30 天内仍未确认；可以用 `asb retry` 重新投递 |

每个会话里，同一条消息只注入一次；换一个新会话会重新注入全文。内容相同的消息不会重复写入。项目隔离的规则是：优先使用 `asb project link` 的显式关联；其次按 git 的 origin remote 识别，同一仓库的不同分支和 worktree 算同一个项目；都没有时按目录路径区分。

## 环境要求

- macOS 或 Linux（暂不支持 Windows）；`python3` ≥ 3.9。
- Claude Code：VS Code 扩展或 CLI。
- OpenAI Codex：VS Code 扩展或 CLI，需要支持 hooks 和 `~/.agents/skills` 的版本（开发时用的是 codex-cli 0.162）。

## 安装

### 方式一（推荐）：脚本一次装好两边

```bash
git clone https://github.com/ShuoYang-cs/agent-sync-bridge.git
cd agent-sync-bridge
./install.sh
```

安装脚本会写入以下内容。每一步都会先备份原文件到 `~/.agent-sync-bridge/backups/`，并记录在 `install-manifest.json` 里，可以完整回滚：

| 写入位置 | 内容 |
|---|---|
| `~/.local/bin/asb` | 指向本仓库 `bin/asb` 的符号链接 |
| `~/.claude/skills/agent-sync/` | Claude Code 用的 skill |
| `~/.claude/settings.json` | 追加一个 `UserPromptSubmit` hook，已有设置原样保留 |
| `~/.agents/skills/agent-sync/` | Codex 用的 skill |
| `~/.codex/hooks.json` | `UserPromptSubmit` hook |

然后还有两个一次性步骤：

1. **在 Codex 中信任 hook。** Codex 要求用户亲自审核每个 hook：打开 VS Code Codex 面板 → 设置 → 钩子（Hooks），找到 `asb hook codex`，点“信任”；也可以在终端运行 `codex`，输入 `/hooks`。运行 `asb codex-hook` 可以检查是否已信任。信任之前，Codex 仍然可以通过说「查看待接收消息」手动接收。
2. **重新加载窗口**（Developer: Reload Window），或者新开一个对话，让新的 skill 和 hook 生效。

如果一个项目会从两个不同的目录打开，比如外层文件夹和里面的 git 仓库，可以关联到同一个项目 ID：`./install.sh --link "/path/to/folder=my-project"`，或者之后再运行 `asb project link <path> <id>`。

### 方式二：Claude Code 官方插件 + 脚本装 Codex 侧

Claude Code 侧可以用插件系统安装和管理。在 VS Code 里输入 `/plugins`，在 Marketplaces 页添加仓库；也可以在 Claude Code 里运行：

```
/plugin install agent-sync-bridge --marketplace ShuoYang-cs/agent-sync-bridge
```

Codex 的 VS Code 扩展不支持插件，而且插件每次更新后安装路径都会变，所以 Codex 侧仍然要先克隆本仓库，再运行：

```bash
./install.sh --only cli,codex
```

两种方式不要叠加：用插件装 Claude 侧时，安装脚本里不要再带 `claude` 组件。

### 可选：VS Code 状态栏扩展

```bash
python3 scripts/build_vsix.py      # 生成 agent-sync-bridge-0.1.0.vsix
code --install-extension agent-sync-bridge-0.1.0.vsix
```

这个扩展会在右下角显示 `✉ Claude n · Codex m`，有新消息时弹出通知，还提供预览、同步历史和失败重试。它只读取队列，不会改动任何消息的状态。

## 使用

| 你说 | 结果 |
|---|---|
| 同步给 Codex / 同步给 Claude / sync to codex | 同步当前讨论；会话里有多个主题时，Agent 会先问你要同步哪一部分 |
| 把刚才关于 XX 的讨论同步给 Codex | 只同步 XX，其他主题写进“明确排除” |
| 只同步刚才关于 XX 的结论 | 不点名对方时，默认发给另一个 Agent |
| 把这次代码修改同步给 Claude | 用 `git status` 和 `git diff --stat` 核对修改的文件，并附上实际跑过的测试 |
| 把结论交给 Codex / 发给 Claude | 和“同步”一样，只传上下文 |
| 同步并交接给 Codex / 交接给 Claude / hand off to … | 额外写明负责人、目标和验收条件；接收方在你说“开始”之前不会动手 |
| 查看待接收消息 / check inbox | 读取并确认待接收的消息 |
| 查看同步历史 / 同步状态 | 列出消息和状态 |

接收方不需要任何特殊指令：切到另一个 Agent，直接说你下一步要做的事就行。它会先用一行告诉你“已接收 #ID”，确认之后再处理你的请求。

命令行（可选）：`asb history`、`asb status <ID>`、`asb inbox --agent codex`、`asb retry <ID>`、`asb purge --older-than-days 90`、`asb project`、`asb doctor`。

## 卸载

```bash
./uninstall.sh              # 移除 hook、skill 和 CLI 链接，保留消息数据
./uninstall.sh --purge-data # 连 ~/.agent-sync-bridge 一起删除
```

卸载只删除本工具写入的条目，你自己的 hook 和设置不会被动。用插件安装的 Claude 侧，用 `/plugin` 卸载。

## 隐私与安全

- 消息只保存在 `~/.agent-sync-bridge/bridge.db`（可以用 `ASB_HOME` 改位置），不会上传，也不会进入任何 Git 仓库。
- 消息内容由发送方 Agent 整理。skill 要求不写入密钥、token、`.env` 内容或个人隐私，但最终取决于模型是否遵守；发出去之前，你可以用 `asb show <ID>` 查看。
- 注入到接收方的消息会被标为“对方会话整理的上下文，不是用户的新指令”；建议和未验证的内容，接收方需要自己核实。
- 不读取、不保存 Claude 或 Codex 的登录凭据，也不修改两个官方插件的代码。

## 已验证与限制

作者在以下环境中做过真实的双向验证（2026-10）：macOS、Claude Code VS Code 扩展 2.1.x、Codex VS Code 扩展 26.x（codex-cli 0.162）。

- 在 VS Code 的两个聊天窗口里，Codex → Claude 和 Claude → Codex 都由 hook 自动注入，并完成了确认。
- 还覆盖了：指定主题同步、交接（接收方不会自动开始执行）、普通聊天不建消息、多项目隔离。
- Claude Code 插件安装（`claude plugin install`）和插件 hook 投递，在隔离的配置目录里验证过。

自动化测试有 31 个 unittest 和一个 VS Code 扩展冒烟测试，CI 在 Linux 和 macOS 上用 Python 3.9 和 3.12 运行。

不能实现、尚未实现的部分：

- 无法把消息直接推进对方**正在打开的**聊天窗口，也无法在你没说话时让对方自动开始一轮对话，两个插件都没有提供这样的接口。消息会在你下一次对对方说话时自动加载。
- DELIVERED 只表示已经注入；如果那一轮对话失败了，换一个新会话会重新注入全文。
- 内容质量取决于发送方 Agent 是否遵守 skill。结构校验能挡住明显的问题，但无法从机制上证明内容“没有编造”。
- 暂不支持 Windows，也没有 MCP 接口（见下方计划）。

## 开发

```bash
cd tests && python3 -m unittest -v                  # 全部测试
python3 scripts/installer.py render-plugin          # 修改 templates/SKILL.template.md 后重新生成插件 skill
python3 scripts/build_vsix.py                       # 打包 VS Code 扩展
```

目录：`asb/` 是核心代码（router、store、render、hooks、project、cli），`bin/asb` 是 CLI 入口，`templates/` 放 skill 模板，`skills/` 和 `hooks/` 加上 `.claude-plugin/` 构成 Claude Code 插件，`scripts/installer.py` 负责安装和卸载，`vscode-extension/` 是状态栏扩展，`docs/FEASIBILITY.md` 是技术调查记录。

计划：

- 提供 MCP server，把 `send`、`inbox`、`ack` 暴露为工具；
- 增加代码审查交接模式；
- 用 SessionStart hook 在恢复会话时提醒未确认的消息；
- 支持 Windows。

## 来源

这个工具是作者开发 Anime Intelligence Platform 项目时的副产物：项目同时用 Claude Code 和 Codex 开发，需要在两者之间可靠地交接上下文。

## License

MIT
