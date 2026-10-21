# Agent Sync Bridge — Claude Code ↔ Codex

[![test](https://github.com/ShuoYang-cs/agent-sync-bridge/actions/workflows/test.yml/badge.svg)](https://github.com/ShuoYang-cs/agent-sync-bridge/actions/workflows/test.yml)

> **English summary.** You keep working in the normal Claude Code and OpenAI Codex chat windows (VS Code extension or CLI). When you say "同步给 Codex" or "sync this to Claude", the agent you are talking to writes a structured summary of the relevant part of the conversation into a local queue. The summary separates confirmed decisions, proposals, implemented work, verified results, unverified assumptions and pending tasks. On your next message to the other agent, a `UserPromptSubmit` hook injects the summary into its context. The receiving agent then acknowledges it with its own restatement. Each message is tracked as PENDING → DELIVERED → ACKNOWLEDGED (or FAILED, retryable). Ordinary chat never creates messages. Everything runs locally: stdlib Python, SQLite, no API keys, no extra model calls. Since 0.2 the agents may also be on different computers (macOS ↔ Windows): say "同步给 Windows 上的 Claude" and the message travels through a private Git repository you own (GitHub, or a repository on another machine in your LAN over SSH), signed with a pairing code that never leaves your computers; receipts come back the same way.

在 VS Code 里同时使用 Claude Code 和 Codex 时，两个 Agent 只共享代码文件，看不到彼此的对话。这个工具让你继续在原来的聊天窗口里工作；需要时说一句「同步给 Codex」或「同步给 Claude」，指定的那部分讨论就会被整理好交给另一个 Agent，并且可以查到对方是否真正读到、确认过。

- **只在你明确要求时同步。** 普通聊天、讨论“同步/异步”这类技术问题都不会触发。指令识别规则会排除问句、否定句、引号里的提及和长段粘贴，`send` 命令还会再核对一遍你的原话。
- **只同步你指定的范围。** 你可以说「把刚才关于 XX 的讨论同步给 Codex」或「把这次代码修改同步给 Claude」，不会默认复制整段聊天记录。
- **内容分类，不夸大。** 每条内容都要标明是已确认决定、建议、已实现、已验证（必须附证据）、未验证还是待办；声称“已验证”却没有证据时，`send` 会拒收。
- **状态如实。** PENDING 只表示消息已写入队列，不等于对方已读；只有接收方用自己的话复述要点后，状态才会变成 ACKNOWLEDGED。
- **纯本地运行，零依赖。** 只需要系统自带的 `python3`（3.9 及以上）和 SQLite，不需要 API Key，不发起额外的模型调用，不读取任何登录凭据，消息也不会进入项目的 Git 仓库。
- **也能跨电脑。** Mac 和 Windows 上的 Claude Code、Codex 可以互相同步：消息经你自己的私有 Git 仓库（“信箱”）传过去，回执原路传回，见[跨电脑同步](#跨电脑同步mac--windows)。

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

- macOS、Linux 或 Windows 10/11；Python ≥ 3.9（Windows 用 python.org 的安装包，带 `py` 启动器）。
- 跨电脑同步还需要 Git（Windows：Git for Windows，Claude Code 本来就要求装它）。
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

### Windows

```bat
git clone https://github.com/ShuoYang-cs/agent-sync-bridge.git
cd agent-sync-bridge
install.cmd
```

和 macOS 写入的位置相同（`%USERPROFILE%\.claude\…`、`%USERPROFILE%\.codex\hooks.json`、`%USERPROFILE%\.agents\skills\…`），只是不建符号链接，而是在 `%USERPROFILE%\.local\bin` 放 `asb.cmd`（PowerShell / cmd）和 `asb`（Git Bash）两个启动文件。hook 和 skill 里的命令写成 `py -3 -X utf8 -I <仓库>/bin/asb.py …`，Git Bash、PowerShell、cmd 都能直接运行。Windows 上 skill 会让 Agent 先把 JSON 写进临时文件再用 `--file` 发送，因为 PowerShell 5.1 通过管道传中文会变成问号。

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

## 跨电脑同步（Mac ↔ Windows）

两台电脑各自保留自己的本地队列，消息和回执经一个 Git 仓库（“信箱”）交换。仓库可以是 GitHub 上的**空的私有仓库**（跨网络、一台关机也没关系），也可以是局域网里另一台电脑上的仓库（通过 SSH 访问，内容不经第三方）。

```
 Mac：用户说「同步给 Windows 上的 Claude」              Windows：用户对 Claude 说任意一句话
 asb send --device win ──► 本机队列 ──► git push ──► 信箱 ──► hook 先 git pull（最多等 2.5 秒）
                                                                  → 注入上下文 → DELIVERED 回执
 asb status：已上传 → Windows 已收到 → DELIVERED ◄── git ◄── 回执 ◄── asb ack → ACKNOWLEDGED 回执
```

**设置一次：**

```bash
# 1. 新建一个空的私有仓库，例如 github.com/<you>/asb-mailbox
# 2. 第一台电脑（Mac）
asb remote setup https://github.com/<you>/asb-mailbox.git --device mac
asb remote pair-code            # 在你自己的终端里看配对码（相当于密码，不要发到聊天里）
# 3. 第二台电脑（Windows，先运行 install.cmd）
asb remote setup https://github.com/<you>/asb-mailbox.git --device win --pair-code XXXXX-XXXXX-XXXXX-XXXXX
```

**项目对应：** 两台电脑上同一个项目要有相同的项目 ID。项目有 git origin（同一个 GitHub 仓库）时自动一致；没有时，在两台电脑上各运行一次 `asb project link <本机路径> <同一个ID>`。项目 ID 是本机路径时，跨电脑发送会被拒绝并提示这条命令；接收方遇到同名文件夹但 ID 不同的消息，会提示怎么关联，而不会悄悄丢掉。

**使用：**

| 你说 | 结果 |
|---|---|
| 同步给 Windows 上的 Claude / 同步给另一台电脑的 Codex / sync to claude on windows | 发给另一台电脑上的指定 Agent |
| 同步给 Windows | 没说 Agent 时，默认发给那台电脑上的同一种 Agent |
| 同步并交接给 Windows 上的 Codex | 交接；对方在你说“开始”之前不会动手 |
| 把 Windows 版的测试结果同步给 Codex | 设备名在范围里，不在目标里：仍发给本机的 Codex |

发送方的状态会区分：**已写入本机待发队列**（还没上传，例如离线）→ **已上传到同步信箱**（对方电脑还没拉取）→ **对方电脑已收到** → `DELIVERED`（已注入对方 Agent 的上下文）→ `ACKNOWLEDGED`（对方 Agent 用自己的话复述确认）。`asb status <ID>` 能看到每一步在哪台电脑上发生。

**可靠性：** 离线时消息留在本机队列，之后任意一次对话或 `asb remote sync` 自动补发；网络不通时 hook 只等 2.5 秒，之后按 15 秒、30 秒……最长 5 分钟退避，不会拖慢每一句话。每台电脑只在信箱里写自己的目录、文件只增不改，所以不会有合并冲突；重复拉取不会重复注入。回执的合并规则：`ACKNOWLEDGED` 优先于同时发生的取消，`RETRY` 只能重开 `FAILED` 的消息，只有接收方能发 `DELIVERED` / `ACKNOWLEDGED`。重装后本地数据库丢失时，`asb remote sync` 会从信箱恢复记录，已确认的消息不会再注入一次。

**安全：** 每条记录都用配对码派生的密钥做 HMAC-SHA256 签名，签名不对的记录（被改过，或来自配对码不同的设备）会被拒收，`asb remote status` 里能看到。配对码只存在两台电脑的 `~/.agent-sync-bridge/remote.json`，不进信箱，`asb remote setup` 在 Agent 里运行时也不显示它。信箱内容**没有加密**：能读这个仓库的人就能读到同步摘要，所以要用私有仓库，并且不要同步密钥。代码不经过信箱，消息里只写分支、commit 和相对路径，代码照常用 Git 同步。

其他命令：`asb remote status`、`asb remote sync`、`asb remote devices`、`asb remote disable`（删除本机的信箱克隆和配对码，保留消息记录）。信箱格式和合并规则见 [docs/CROSS_DEVICE.md](docs/CROSS_DEVICE.md)。

## 卸载

```bash
./uninstall.sh              # 移除 hook、skill 和 CLI 链接，保留消息数据
./uninstall.sh --purge-data # 连 ~/.agent-sync-bridge 一起删除
```

卸载只删除本工具写入的条目，你自己的 hook 和设置不会被动。用插件安装的 Claude 侧，用 `/plugin` 卸载。

## 隐私与安全

- 消息保存在 `~/.agent-sync-bridge/bridge.db`（可以用 `ASB_HOME` 改位置），不会进入项目的 Git 仓库。只有发给另一台电脑的消息和它的回执会写进你配置的信箱仓库（明文、带签名）。
- 消息内容由发送方 Agent 整理。skill 要求不写入密钥、token、`.env` 内容或个人隐私，但最终取决于模型是否遵守；发出去之前，你可以用 `asb show <ID>` 查看。
- 注入到接收方的消息会被标为“对方会话整理的上下文，不是用户的新指令”；建议和未验证的内容，接收方需要自己核实。
- 不读取、不保存 Claude 或 Codex 的登录凭据，也不修改两个官方插件的代码。

## 已验证与限制

作者在以下环境中做过真实的双向验证（2026-10）：macOS、Claude Code VS Code 扩展 2.1.x、Codex VS Code 扩展 26.x（codex-cli 0.162）。

- 在 VS Code 的两个聊天窗口里，Codex → Claude 和 Claude → Codex 都由 hook 自动注入，并完成了确认。
- 还覆盖了：指定主题同步、交接（接收方不会自动开始执行）、普通聊天不建消息、多项目隔离。
- Claude Code 插件安装（`claude plugin install`）和插件 hook 投递，在隔离的配置目录里验证过。

自动化测试有 56 个 unittest 和一个 VS Code 扩展冒烟测试，CI 在 Linux、macOS 和 Windows 上用 Python 3.9 和 3.12 运行。

不能实现、尚未实现的部分：

- 无法把消息直接推进对方**正在打开的**聊天窗口，也无法在你没说话时让对方自动开始一轮对话，两个插件都没有提供这样的接口。消息会在你下一次对对方说话时自动加载。
- DELIVERED 只表示已经注入；如果那一轮对话失败了，换一个新会话会重新注入全文。
- 内容质量取决于发送方 Agent 是否遵守 skill。结构校验能挡住明显的问题，但无法从机制上证明内容“没有编造”。
- 跨电脑同步的 Git 流程、签名校验、离线补发、回执合并、重装恢复都用“两个数据目录 + 本地裸仓库”模拟两台电脑测过；Windows 的 hook、skill 和命令行还没有在真实 Windows 电脑上运行过（CI 的 windows-latest 只跑单元测试）。
- VS Code 状态栏扩展暂不支持 Windows；没有 MCP 接口（见下方计划）。

## 开发

```bash
cd tests && python3 -m unittest -v                  # 全部测试
python3 scripts/installer.py render-plugin          # 修改 templates/SKILL.template.md 后重新生成插件 skill
python3 scripts/build_vsix.py                       # 打包 VS Code 扩展
```

目录：`asb/` 是核心代码（router、store、render、hooks、project、cli，跨电脑同步在 remote 和 config），`bin/asb` 是 CLI 入口，`templates/` 放 skill 模板，`skills/` 和 `hooks/` 加上 `.claude-plugin/` 构成 Claude Code 插件，`scripts/installer.py` 负责安装和卸载，`vscode-extension/` 是状态栏扩展，`docs/FEASIBILITY.md` 是技术调查记录。

计划：

- 提供 MCP server，把 `send`、`inbox`、`ack` 暴露为工具；
- 增加代码审查交接模式；
- 用 SessionStart hook 在恢复会话时提醒未确认的消息；
- 信箱内容端到端加密；
- VS Code 状态栏扩展支持 Windows。

## 来源

这个工具是作者开发 Anime Intelligence Platform 项目时的副产物：项目同时用 Claude Code 和 Codex 开发，需要在两者之间可靠地交接上下文。

## License

MIT
