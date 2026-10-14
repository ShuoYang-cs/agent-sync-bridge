import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import REPO, TempHome

INSTALLER = str(REPO / "scripts" / "installer.py")


class Installer(TempHome):
    """Runs the real installer against a fake $HOME and checks it is reversible."""

    def run_installer(self, *args):
        env = dict(os.environ, HOME=str(self.home))
        r = subprocess.run([sys.executable, "-I", INSTALLER, *args], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_install_uninstall_roundtrip(self):
        claude = self.home / ".claude/settings.json"
        claude.parent.mkdir(parents=True)
        original = {"theme": "auto", "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}
        claude.write_text(json.dumps(original))

        self.run_installer("install", "--link", f"{self.proj_a}=anime")
        self.run_installer("install")  # idempotent
        s = json.loads(claude.read_text())
        cmds = [h["command"] for g in s["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        self.assertEqual(cmds.count(f"{self.home}/.local/bin/asb hook claude"), 1)
        self.assertIn("echo mine", cmds)
        codex = json.loads((self.home / ".codex/hooks.json").read_text())
        self.assertEqual(len(codex["hooks"]["UserPromptSubmit"]), 1)
        skill = (self.home / ".agents/skills/agent-sync/SKILL.md").read_text()
        self.assertIn("你是 **Codex**", skill)
        self.assertIn("--from codex --to claude", skill)
        self.assertNotIn("{{", skill)
        self.assertIn("你是 **Claude Code**", (self.home / ".claude/skills/agent-sync/SKILL.md").read_text())
        self.assertTrue((self.home / ".local/bin/asb").is_symlink())
        links = json.loads(Path(os.environ["ASB_HOME"], "projects.json").read_text())
        self.assertEqual(list(links["links"].values()), ["anime"])

        self.run_installer("uninstall")
        self.assertEqual(json.loads(claude.read_text()), original)
        self.assertFalse((self.home / ".codex/hooks.json").exists())
        self.assertFalse((self.home / ".agents").exists())
        self.assertFalse((self.home / ".claude/skills/agent-sync").exists())
        self.assertFalse((self.home / ".local/bin/asb").exists())

    def test_does_not_clobber_foreign_skill(self):
        d = self.home / ".claude/skills/agent-sync"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("user's own skill")
        self.run_installer("install")
        self.assertEqual((d / "SKILL.md").read_text(), "user's own skill")
        self.run_installer("uninstall")
        self.assertEqual((d / "SKILL.md").read_text(), "user's own skill")


if __name__ == "__main__":
    unittest.main()


class PluginPackage(unittest.TestCase):
    """The Claude Code plugin files stay valid and in sync with the template."""

    def test_plugin_skill_matches_template(self):
        sys.path.insert(0, str(REPO / "scripts"))
        import installer
        rendered = installer.render_skill("claude", installer.PLUGIN_ASB)
        self.assertEqual((REPO / "skills/agent-sync/SKILL.md").read_text(encoding="utf-8"), rendered,
                         "run: python3 scripts/installer.py render-plugin")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/bin/asb send --from claude --to codex", rendered)

    def test_manifests(self):
        plugin = json.loads((REPO / ".claude-plugin/plugin.json").read_text())
        market = json.loads((REPO / ".claude-plugin/marketplace.json").read_text())
        hooks = json.loads((REPO / "hooks/hooks.json").read_text())
        self.assertRegex(plugin["name"], r"^[a-z0-9-]+$")
        self.assertFalse(plugin["name"].startswith(("claude-", "anthropic-")))
        self.assertTrue(market["owner"]["name"])
        self.assertEqual(market["plugins"][0]["name"], plugin["name"])
        self.assertEqual(market["plugins"][0]["source"], ".")
        command = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertEqual(command, '"${CLAUDE_PLUGIN_ROOT}/bin/asb" hook claude')
        self.assertTrue(os.access(REPO / "bin/asb", os.X_OK))
