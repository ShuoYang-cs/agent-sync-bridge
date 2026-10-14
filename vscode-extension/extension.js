// Agent Sync Bridge — VS Code companion. Read-mostly UI over the `asb` CLI;
// it never marks messages DELIVERED/ACKNOWLEDGED (only the agents do that).
const vscode = require("vscode");
const { execFile } = require("child_process");
const os = require("os");
const path = require("path");

const LABEL = { claude: "Claude", codex: "Codex" };
// Public commands contributed by the official extensions.
const OPEN_CMD = { claude: "claude-vscode.focus", codex: "chatgpt.openSidebar" };
const IMPORT_CMD = { claude: "claude-vscode.insertAtMention", codex: "chatgpt.addToThread" };
const RECEIVE_PROMPT = "查看待接收消息";

let statusItem;
let timer;
const seen = new Set();
let firstPoll = true;

function asbPath() {
  const p = vscode.workspace.getConfiguration("agentSync").get("asbPath") || "~/.local/bin/asb";
  return p.startsWith("~") ? path.join(os.homedir(), p.slice(1)) : p;
}

function cwd() {
  const f = vscode.workspace.workspaceFolders;
  return f && f.length ? f[0].uri.fsPath : os.homedir();
}

function asb(args) {
  return new Promise((resolve, reject) => {
    execFile(asbPath(), [...args, "--json"], { cwd: cwd(), timeout: 15000 }, (err, stdout, stderr) => {
      let data = null;
      try { data = JSON.parse(stdout || "null"); } catch (_) { /* fallthrough */ }
      if (data && data.error) return reject(new Error(data.error));
      if (err && data === null) return reject(new Error(stderr || err.message));
      resolve(data);
    });
  });
}

function describe(m) {
  const mode = m.mode === "handoff" ? "交接" : "同步";
  return {
    label: `${LABEL[m.from]} → ${LABEL[m.to]} · ${m.title}`,
    description: `${m.status} · ${mode}`,
    detail: `#${m.id} · ${m.created_at}${m.ack_summary ? " · 复述：" + m.ack_summary : ""}${m.error ? " · " + m.error : ""}`,
    msg: m,
  };
}

async function poll() {
  let inbox;
  try {
    inbox = await asb(["inbox", "--agent", "all", "--cwd", cwd()]);
  } catch (e) {
    statusItem.text = "$(sync-ignored) Agent Sync";
    statusItem.tooltip = "asb 不可用：" + e.message;
    statusItem.show();
    return;
  }
  const count = { claude: 0, codex: 0 };
  for (const m of inbox) count[m.to] = (count[m.to] || 0) + 1;
  const total = count.claude + count.codex;
  statusItem.text = total ? `$(mail) Claude ${count.claude} · Codex ${count.codex}` : "$(sync) Agent Sync";
  statusItem.tooltip = total ? "待接收的同步消息（点击查看）" : "没有待接收的同步消息";
  statusItem.backgroundColor = total ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
  statusItem.show();

  const fresh = inbox.filter((m) => !seen.has(m.id));
  fresh.forEach((m) => seen.add(m.id));
  if (firstPoll) { firstPoll = false; if (!fresh.length) return; }
  if (!vscode.workspace.getConfiguration("agentSync").get("notify")) return;
  for (const m of fresh) notify(m);
}

async function notify(m) {
  const kind = m.mode === "handoff" ? "交接" : "同步";
  const choice = await vscode.window.showInformationMessage(
    `📨 ${LABEL[m.from]} → ${LABEL[m.to]} ${kind}：${m.title}（#${m.id}）。在 ${LABEL[m.to]} 中说「${RECEIVE_PROMPT}」即可接收。`,
    `打开 ${LABEL[m.to]}`, "复制接收指令", "预览");
  if (!choice) return;
  if (choice.startsWith("打开")) return openTarget(m.to, true);
  if (choice === "复制接收指令") {
    await vscode.env.clipboard.writeText(RECEIVE_PROMPT);
    return vscode.window.showInformationMessage(`已复制「${RECEIVE_PROMPT}」，粘贴到 ${LABEL[m.to]} 输入框发送即可。`);
  }
  return preview(m);
}

async function openTarget(agent, copy) {
  if (copy) await vscode.env.clipboard.writeText(RECEIVE_PROMPT);
  try {
    await vscode.commands.executeCommand(OPEN_CMD[agent]);
  } catch (e) {
    vscode.window.showWarningMessage(`无法打开 ${LABEL[agent]}：${e.message}`);
  }
}

async function preview(m) {
  // `show` without --agent is a peek: it does not change the message status.
  const full = await asb(["show", m.id]);
  const doc = await vscode.workspace.openTextDocument({ content: full.body_md, language: "markdown" });
  return vscode.window.showTextDocument(doc, { preview: true });
}

async function pick(list, placeHolder) {
  if (!list.length) {
    vscode.window.showInformationMessage("（无）");
    return null;
  }
  const it = await vscode.window.showQuickPick(list.map(describe), { placeHolder, matchOnDetail: true });
  return it ? it.msg : null;
}

async function showInbox() {
  const m = await pick(await asb(["inbox", "--agent", "all", "--cwd", cwd()]), "待接收的同步消息（当前项目）");
  if (!m) return;
  const a = await vscode.window.showQuickPick([`一键导入到 ${LABEL[m.to]}`, "预览", `打开 ${LABEL[m.to]} 并复制接收指令`]);
  if (!a) return;
  if (a.startsWith("一键导入")) return importMessage(m);
  if (a === "预览") return preview(m);
  return openTarget(m.to, true);
}

async function showHistory() {
  const m = await pick(await asb(["history", "--cwd", cwd(), "--limit", "50"]), "同步历史（当前项目）");
  if (m) preview(m);
}

// Opens the message, selects it and hands the selection to the target agent's
// own public "add selection to chat" command. The agent still has to read it
// and run `asb ack` for the status to become ACKNOWLEDGED.
async function importMessage(m) {
  if (!m) {
    m = await pick(await asb(["inbox", "--agent", "all", "--cwd", cwd()]), "选择要导入的消息");
    if (!m) return;
  }
  const editor = await preview(m);
  const doc = editor.document;
  editor.selection = new vscode.Selection(0, 0, doc.lineCount, 0);
  try {
    await vscode.commands.executeCommand(IMPORT_CMD[m.to]);
    vscode.window.showInformationMessage(
      `已把 #${m.id} 加入 ${LABEL[m.to]} 输入框。发送时可附一句「这是同步消息，请阅读并 ack」。`);
  } catch (e) {
    await openTarget(m.to, true);
    vscode.window.showWarningMessage(`${LABEL[m.to]} 不支持直接导入（${e.message}），已打开并复制「${RECEIVE_PROMPT}」。`);
  }
}

async function retryFailed() {
  const hist = await asb(["history", "--cwd", cwd(), "--limit", "50"]);
  const m = await pick(hist.filter((x) => x.status === "FAILED"), "选择要重试的 FAILED 消息");
  if (!m) return;
  await asb(["retry", m.id]);
  seen.delete(m.id);
  vscode.window.showInformationMessage(`#${m.id} 已重新进入 ${LABEL[m.to]} 的待接收队列`);
  poll();
}

function wrap(fn) {
  return async (...args) => {
    try { await fn(...args); } catch (e) { vscode.window.showErrorMessage("Agent Sync：" + e.message); }
  };
}

function activate(context) {
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusItem.command = "agentSync.showInbox";
  context.subscriptions.push(
    statusItem,
    vscode.commands.registerCommand("agentSync.showInbox", wrap(showInbox)),
    vscode.commands.registerCommand("agentSync.showHistory", wrap(showHistory)),
    vscode.commands.registerCommand("agentSync.importMessage", wrap(() => importMessage())),
    vscode.commands.registerCommand("agentSync.retryFailed", wrap(retryFailed)),
  );
  const secs = Math.max(2, vscode.workspace.getConfiguration("agentSync").get("pollSeconds") || 5);
  poll();
  timer = setInterval(poll, secs * 1000);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
}

function deactivate() {
  if (timer) clearInterval(timer);
}

module.exports = { activate, deactivate };
