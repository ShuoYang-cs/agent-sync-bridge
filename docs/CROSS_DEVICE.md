# 跨电脑同步：信箱格式与规则（0.2）

两台电脑各自运行完整的 Agent Sync Bridge（本地 SQLite 队列、hook、skill）。跨电脑的部分只做一件事：把“发往另一台电脑的消息”和“关于这些消息的回执”通过一个 Git 仓库交换。实现在 `asb/remote.py`，本地状态在 `asb/store.py`。

## 信箱仓库

```
asb-mailbox.json                      {"kind": "asb-mailbox", "format": 1}
.gitattributes                        * -text（Windows 上不转换换行）
devices/<设备ID>/device.json          设备卡片：id、label、platform、created_at、sig
devices/<设备ID>/messages/<消息ID>.json   这台电脑发往其他电脑的消息
devices/<设备ID>/events/<消息ID>.<类型>.<8位hex>.json   这台电脑写的回执
```

- 每台电脑只写自己的 `devices/<ID>/`，文件创建后不再修改。所以 `git pull --rebase` 不会冲突，同一个文件读几次结果都一样。
- 同步顺序：导出本机待发记录 → commit → `pull --rebase` → 导入其他电脑的新记录 → 导出导入时产生的 `RECEIVED` 回执 → push。push 被拒（对方刚推过）就重新 pull 再推，最多 4 次。push 成功后才把记录标成已上传。
- Git 命令都带 `GIT_TERMINAL_PROMPT=0`（Windows 上另加 `GCM_INTERACTIVE=never`），不会在 hook 里卡在输入密码上。只有 `asb remote setup` 的 clone 允许交互式登录。

## 签名

- 配对码：20 位 Crockford base32（100 位随机），第一台电脑 `setup` 时生成。`key = SHA-256("asb-mailbox-v1:" + 配对码)`。
- 每条记录的 `sig = HMAC-SHA256(key, 去掉 sig 后按键排序的紧凑 JSON)`。
- 导入时，签名不对、字段和所在目录或文件名不一致、文件超过 256 KB 的记录都会被拒收，并记在 `asb remote status` 的“上次同步拒收”里。签名不对的记录不会被标记为已处理，修好配对码后会重新校验。
- 接收方不使用发送方渲染的 Markdown：先用 `render.validate` 重新校验 `context`，再在本机渲染。
- 这是完整性和身份校验，**不加密**。能读仓库的人能读到摘要。

## 消息与回执

消息记录包含：`id`、`source_device`/`target_device`、`source_agent`/`target_agent`、`mode`、`title`、`user_command`、`context`（和本机消息同一套结构）、`project_id`、`project_name`（文件夹名，只用来提示）、`git_remote`、`git_branch`、`git_head`、`created_at`、`expires_at`。不包含本机绝对路径。

回执类型：

| 类型 | 谁能写 | 作用 |
|---|---|---|
| `RECEIVED` | 接收方 | 已导入接收方的本地队列（发送方显示“对方电脑已收到”） |
| `DELIVERED` | 接收方 | 第一次注入接收方 Agent 的上下文 |
| `ACKNOWLEDGED` | 接收方 | 接收方 Agent 带复述确认（`summary`） |
| `FAILED` | 双方 | 接收方拒收或过期；发送方取消 |
| `RETRY` | 双方 | 重新打开 FAILED 的消息（带新的 `expires_at`） |

合并规则（`Store.apply_remote_event`，与到达顺序无关）：

- `DELIVERED` 只把 `PENDING` 变成 `DELIVERED`。
- `ACKNOWLEDGED` 总会生效，包括覆盖同时发生的 `FAILED`（“对方已读”是事实，优先于“取消”的意图）。
- `FAILED` 只作用于 `PENDING`/`DELIVERED`。
- `RETRY` 只作用于 `FAILED`，并清空按会话的投递记录，让对方重新注入。
- 不是这条消息双方之一写的回执、发送方冒充接收方写的 `DELIVERED`/`ACKNOWLEDGED`，都会被忽略。

过期只由接收方判断：发送方不会让发往另一台电脑的消息自己过期，而是一直显示对方的真实状态。

## 本机状态

`messages` 表新增 `source_device`、`target_device`、`transport`、`project_name`、`git_remote`。同一台电脑内的消息这些列都是空的，行为和 0.1 完全一样。

`transport`：发送方 `OUTBOX`（未上传）→ `SENT`（已上传）→ `RECEIVED`（对方已导入）；接收方 `INBOUND`。

`outbox` 表存待上传的回执，和状态变更写在同一个事务里，所以离线、进程被杀都不会丢回执。`imported` 表记录处理过的信箱记录，保证导入幂等；本机自己写的记录也会登记，所以本地数据库被删后重新同步，能从信箱里恢复本机发过的消息和本机写过的回执（已确认的消息不会再注入一次）。

## hook 的时间预算

- 每次 `UserPromptSubmit`：距上次同步超过 5 秒、没有处于失败退避、没有别的同步在跑时，启动一个独立进程跑 `asb remote sync`，最多等 `hook_wait` 秒（默认 2.5），然后用本机已有的数据注入。没等到的话同步在后台继续，下一句话会加载。
- 失败后按 15、30、60……秒退避，最长 5 分钟。
- 注入产生了 `DELIVERED` 回执时，再启动一个后台同步把回执推回去（不等待）。
- `remote.json` 里 `"hook_sync": false` 可以关掉对话时的自动同步，只用 `asb remote sync`。

## 项目对应

稳定的项目 ID 才能跨电脑：`asb project link` 的显式关联，或 git origin（规范化后的 URL）。接收方按项目 ID 或 `git_remote` 匹配当前项目。发送时项目 ID 是本机路径会被拒绝，并给出 `asb project link` 命令；`project link` 会把原来按路径记录的消息一起改到新 ID 下。
