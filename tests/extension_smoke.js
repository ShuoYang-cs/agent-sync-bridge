// Smoke test for vscode-extension/extension.js with a mocked `vscode` module.
// Usage: ASB_HOME=<tmp> node tests/extension_smoke.js <project-dir>
const Module = require("module");
const path = require("path");
const assert = require("assert");

const cwd = process.argv[2];
const shown = [];
const registered = {};
const status = { show() { this.visible = true; } };
const vscode = {
  StatusBarAlignment: { Right: 2 },
  ThemeColor: function (id) { this.id = id; },
  Selection: function () {},
  window: {
    createStatusBarItem: () => status,
    showInformationMessage: async (msg) => { shown.push(msg); return undefined; },
    showWarningMessage: async () => undefined,
    showErrorMessage: async (msg) => { shown.push("ERR " + msg); },
    showQuickPick: async () => undefined,
  },
  workspace: {
    workspaceFolders: [{ uri: { fsPath: cwd } }],
    getConfiguration: () => ({ get: (k) => ({ asbPath: path.join(__dirname, "..", "bin", "asb"), pollSeconds: 60, notify: true })[k] }),
  },
  commands: { registerCommand: (id, fn) => { registered[id] = fn; return { dispose() {} }; }, executeCommand: async () => {} },
  env: { clipboard: { writeText: async () => {} } },
};
const orig = Module._load;
Module._load = function (req, ...rest) { return req === "vscode" ? vscode : orig.call(this, req, ...rest); };

const ext = require("../vscode-extension/extension.js");
const ctx = { subscriptions: [] };
ext.activate(ctx);
setTimeout(() => {
  try {
    assert.ok(status.visible, "status bar shown");
    assert.match(status.text, /Codex 1/, "status shows 1 pending for Codex: " + status.text);
    assert.ok(shown.some((m) => m.includes("Claude → Codex")), "notification shown: " + JSON.stringify(shown));
    for (const c of ["agentSync.showInbox", "agentSync.showHistory", "agentSync.importMessage", "agentSync.retryFailed"]) {
      assert.ok(registered[c], "registered " + c);
    }
    console.log("extension smoke OK:", status.text, "|", shown[0]);
    ext.deactivate();
    process.exit(0);
  } catch (e) {
    console.error(e.message);
    process.exit(1);
  }
}, 2500);
