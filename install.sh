#!/bin/sh
# Install Agent Sync Bridge for the current user (reversible; see uninstall.sh).
#   ./install.sh                                   # CLI + skills + hooks
#   ./install.sh --link "/path/to/project=my-id"   # also map a folder to a project id
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
/usr/bin/python3 -I "$ROOT/scripts/installer.py" install "$@"
echo
"$ROOT/bin/asb" doctor
echo
echo "下一步：在 Codex 中信任 hook 一次（VS Code Codex 设置 → Hooks，或 codex 终端里 /hooks）。"
echo "检查：asb codex-hook"
