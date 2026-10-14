---
name: agent-sync
description: Agent Sync Bridge between Claude Code and Codex. Use ONLY when the user explicitly asks to sync or hand off context to the other agent ("同步给 {{OTHER_LABEL}}", "把…同步给…", "把结论交给/发给 {{OTHER_LABEL}}", "同步并交接给…", "只同步…的结论", "sync/hand off to {{OTHER}}"), asks to view sync inbox/history ("查看待接收消息", "查看同步历史", "同步状态"), or when an [Agent Sync Bridge] context block asks you to acknowledge a message. Never use for ordinary chat or for sync-vs-async programming questions.
---

# Agent Sync Bridge

你是 **{{AGENT_LABEL}}**（agent id `{{AGENT}}`），对端是 **{{OTHER_LABEL}}**（`{{OTHER}}`）。
CLI：`{{ASB}}`（本机 SQLite 队列，数据只在本机，不进 Git）。

## 硬规则

1. **只有用户明确发出同步指令才创建消息。** 普通聊天、讨论“同步/异步”技术问题、用户只是提到对方 Agent，都不发送。
2. **只写你在本会话中真实看到的内容。** 不编造结论、文件、测试结果。不确定就标 `UNVERIFIED`，不要升级成 `VERIFIED`。
3. **不要复制整段聊天记录。** 写摘要和分类条目，每条一两句，足以让对方独立理解。
4. **只同步用户指定的范围。** 其他主题不要混进来，只在 `scope.excluded` 写出被排除的主题名（不写其内容）。
5. **范围不清就先问。** 用户没指定范围、而本会话有多个不相关主题时，先问用户要同步哪部分，暂不发送。只有一个明显主题时直接同步，并在 `scope.included` 写明。
6. **如实报告状态。** `PENDING` 只是“已写入队列”，不能说成“对方已收到/已理解”。
7. 不要放入密钥、token、`.env` 内容或个人隐私。

## A. 发送（用户说「同步给 {{OTHER_LABEL}}」「同步并交接给 {{OTHER_LABEL}}」等）

1. **确定参数。** 如果上下文里有 `[Agent Sync Bridge] 用户发出了显式同步指令` 的提示，就用它给出的目标、模式和范围。
   - 「同步」→ `--mode sync`：只传上下文，**不**分派任务，不得带 `handoff` 字段。
   - 「同步并交接」「交接给」→ `--mode handoff`：必须带 `handoff`（owner = `{{OTHER}}`、goal、acceptance_criteria）。
2. **回顾本会话，挑出与范围相关的内容并分类：**

   | type | 何时使用 |
   |---|---|
   | `CONFIRMED_DECISION` | 用户明确同意或拍板的决定 |
   | `PROPOSAL` | 提出过、但用户没有确认的方案或建议 |
   | `IMPLEMENTED` | 本会话里确实修改了代码或文件（同时写进 `files_changed`） |
   | `VERIFIED` | 实际运行过并看到通过输出的测试或命令。**`evidence` 必填**，写命令和结果 |
   | `UNVERIFIED` | 推测、假设、未运行的结论、只来自文档而未核实代码的说法 |
   | `PENDING_TASK` | 尚未完成的待办 |

   同步代码修改时，先运行 `git status --short` 和 `git diff --stat`，用真实输出核对 `files_changed`。`tests` 只写本会话实际运行过的命令。
3. **发送。** 用 heredoc 把 JSON 从 stdin 传入，`--user-command` 原样填用户的同步指令：

```bash
{{ASB}} send --from {{AGENT}} --to {{OTHER}} --mode sync --user-command '把刚才关于 Comment Collector 的讨论同步给 {{OTHER_LABEL}}' <<'JSON'
{
  "title": "Comment Collector 架构定稿",
  "summary": "两三句话：讨论了什么、结论是什么、对方需要据此做什么。",
  "scope": {"topic": "Bilibili Comment Collector", "included": "本会话中关于分页和重试的讨论", "excluded": ["弹幕采集", "标注工具"]},
  "items": [
    {"type": "CONFIRMED_DECISION", "text": "用户确认：评论按 BV 号分目录存储"},
    {"type": "PROPOSAL", "text": "建议用 cursor 分页替代页码分页（用户未确认）"},
    {"type": "VERIFIED", "text": "WBI 签名单元测试通过", "evidence": "python -m unittest tests.test_bilibili_wbi → OK (12 tests)"},
    {"type": "UNVERIFIED", "text": "推测 412 错误来自频率限制，未复现"},
    {"type": "PENDING_TASK", "text": "实现 checkpoint 断点续采"}
  ],
  "files_changed": [{"path": "backend/app/adapters/bilibili.py", "change": "新增 reply 分页"}],
  "tests": [{"command": "python -m unittest tests.test_bilibili_wbi", "result": "OK (12 tests)", "passed": true}],
  "open_questions": ["是否需要采集楼中楼的全部回复？"],
  "next_steps_for_receiver": ["改动前先读 docs/DECISIONS.md 的 ADR-xx"]
}
JSON
```

   交接模式另加（`--mode handoff`）：

```json
"handoff": {"owner": "{{OTHER}}", "goal": "实现 checkpoint 断点续采", "acceptance_criteria": ["中断后重跑不重复写入", "新增单元测试通过"], "constraints": ["不修改 Golden Dataset"]}
```

   空字段可以省略。`send` 会校验 JSON 结构，并核对 `--user-command` 是否真的是同步指令。校验失败就修正后重发，**不要**改说“已发送”。
4. **回复用户**（根据 send 的真实输出）：
   > 已整理当前讨论，并将消息写入 {{OTHER_LABEL}} 的待接收队列：#ID「标题」（PENDING）。包含：N 条已确认决定、M 条建议……；已排除：……。{{OTHER_LABEL}} 下一次对话时会自动加载，确认后状态变为 ACKNOWLEDGED，可以说「查看同步历史」查询。

   如果输出是“相同内容的消息已存在”，就告诉用户已有消息的 ID 和状态，不要重复发送。

## B. 接收（上下文出现 `[Agent Sync Bridge]` 消息，或用户说「查看待接收消息」）

1. 如果消息没有被注入上下文：先运行 `{{ASB}} inbox --agent {{AGENT}}`，再逐条运行 `{{ASB}} show <ID> --agent {{AGENT}}` 阅读全文。
2. 读完后确认（一条一次，摘要用你自己的话写）：
   `{{ASB}} ack <ID> --agent {{AGENT}} --summary "<一句话复述你理解的要点>"`
3. 在回复开头用一行告诉用户：已接收 #ID「标题」（来自 {{OTHER_LABEL}}），已确认。
4. **同步**消息：只吸收上下文，不要自动开始新开发任务。**交接**消息：列出目标和验收条件。用户本轮明确说开始或接手才执行，否则询问是否开始。
5. `PROPOSAL` / `UNVERIFIED` 不是事实。依赖它们之前，先查看代码和测试。
6. 无法处理（项目不符、内容损坏、明显发错对象）时，运行：`{{ASB}} fail <ID> --agent {{AGENT}} --reason "<原因>"`。

## C. 历史与状态

- 「查看同步历史」：`{{ASB}} history`（加 `--all-projects` 看全部项目）
- 单条状态与事件：`{{ASB}} status <ID>`
- 重试失败的消息：`{{ASB}} retry <ID>`

| 状态 | 含义 |
|---|---|
| PENDING | 已写入本机队列，对方还没读取 |
| DELIVERED | 已注入对方会话上下文（或对方运行了 `show`），等待对方确认 |
| ACKNOWLEDGED | 对方读完并用复述完成确认 |
| FAILED | 被拒收或超过保留期（30 天）仍未确认，可以 `retry` |
