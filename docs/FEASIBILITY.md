# 技术可行性调查（2026-10-14，作者环境实测）

环境：macOS (Apple Silicon)、VS Code、Claude Code 插件 2.1.296、OpenAI Codex 插件 26.1007（内置 codex-cli 0.162.0-alpha）。

## Claude Code

| 能力 | 结论 |
|---|---|
| Skills | `~/.claude/skills/<name>/SKILL.md`，由模型根据 description 自动选用。可用于“同步指令 → 打包 → 发送”的流程 |
| Hooks | `UserPromptSubmit` 的 stdout / `hookSpecificOutput.additionalContext` 会注入上下文。实测 VS Code 插件自带的二进制能正确解析（debug 日志：`provided additionalContext`） |
| MCP | 支持，但 MVP 不需要（skill 加 shell 已经够用） |
| 会话上下文 | 只有 Agent 自己能“看到”本会话；没有公开 API 让外部读取或写入正在运行的 VS Code 会话。所以上下文必须由 Agent 自己整理 |
| VS Code 集成 | 公开命令：`claude-vscode.focus`、`claude-vscode.insertAtMention` 等；没有 URI 或 API 可以注入一条聊天消息 |

## Codex

| 能力 | 结论 |
|---|---|
| Skills | `$HOME/.agents/skills/<name>/SKILL.md`。实测 `codex exec` 能发现 `agent-sync` |
| AGENTS.md | 支持，但会改项目文件，所以不使用 |
| Hooks | `~/.codex/hooks.json`；支持 `UserPromptSubmit` 和 `SessionStart`；additionalContext 注入，默认约 2500 token 上限（已设为 8000）。**每个 hook 必须由用户信任一次**（`/hooks` 或插件设置），状态记录在 `hooks.state.<key>.trusted_hash` |
| MCP / CLI | `codex mcp`、`codex exec`、`codex exec resume` 都可用；`codex exec` 和 VS Code 共享同一份配置、skills 和登录态 |
| 会话投递 | `codex queue --thread <id>` 可以给已有会话排队消息，但会触发 Codex 立即执行一轮，不符合“同步不自动执行”，所以不采用 |
| VS Code 集成 | 公开命令：`chatgpt.openSidebar`、`chatgpt.addToThread`（把选区加入线程）等 |

## 跨 Agent 通信结论

| 需求 | 可行性 | 方案 |
|---|---|---|
| 1. 获取指定讨论的真实上下文 | ✅（只能由发起方 Agent 自己整理） | skill 指导 Agent 分类，`render.validate` 校验结构 |
| 2. 写入本地共享队列 | ✅ | SQLite `~/.agent-sync-bridge/bridge.db` |
| 3. 主动通知另一个 Agent | ⚠️ 只能通知用户 | VS Code 扩展弹通知；Agent 本身无法被外部唤醒 |
| 4. 接收方下一次交互时自动加载 | ✅ | `UserPromptSubmit` hook（Codex 需要先信任） |
| 5. 直接投递进现有聊天窗口 | ❌ 没有公开接口 | 替代：hook 自动加载 + 一键导入（使用官方公开命令） |

四种状态的区分：已保存 = PENDING；已投递 = DELIVERED（注入上下文或 `show`）；已读取并确认 = ACKNOWLEDGED（接收方附带复述的 ack）。“已理解”无法从机制上证明，复述摘要是最接近的可审计证据。
