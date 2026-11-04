#!/usr/bin/env python3
"""Exercise actual codex.2/.3 generated installations, old hooks and the new local CLI.

Requires the source repository's historical commits. No remote service or native
chat tools are called; all identities and business records are fixture data.
"""
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
REFS = {"0.5.1-codex.2": "2bb30df", "0.5.1-codex.3": "2f46db0"}

GENERATE = '''
import json,sys
from pathlib import Path
source,target=map(Path,sys.argv[1:])
sys.path.insert(0,str(source/'scripts'))
import setup,codex_runtime
setup.run=lambda *a,**kw:(None,'','offline fixture')
plan=setup.build_plan(str(target),str(source/'scaffold/task.codex.example.json'),None)
assert setup.apply_plan(str(target),plan,setup.actions_for(str(target),plan))
pool=codex_runtime.Pool(target)
pool.configure('fixture-host','fixture-project')
for name in pool.slots:
    pool.bind(pool.reserve(name)['token'],'fixture-'+name,'fixture-host')
with pool.locked() as state:
    state['messages']['retained']={'status':'handled','from':'PMO-p2-01','to':'PMO-p0-01','handled_at':'fixture'}
(target/'feishu/ids.json').write_text(json.dumps({'wiki':{'space_id':'fixture-space','nodes':{'overview':{'node_token':'fixture-node'}}},'project':{'id':'fixture-project-item'}}))
(target/'packages/sent/fixture.json').write_text('{"item_id":"fixture-item"}')
p=target/'AGENTS.md';p.write_text('@~/AGENTS.md\\n用户自定义规则。\\n'+p.read_text())
'''


class LegacyUpgradeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="codex-legacy[")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.sources = {}
        for version, ref in REFS.items():
            result = subprocess.run(["git", "archive", ref, "plugins/agent-org"], cwd=REPO, capture_output=True)
            if result.returncode:
                raise unittest.SkipTest("历史迁移验收需要源码仓库提交 " + ref)
            folder = Path(cls.tmp.name) / version
            folder.mkdir()
            with tarfile.open(fileobj=io.BytesIO(result.stdout)) as bundle:
                for member in bundle.getmembers():
                    if Path(member.name).is_absolute() or ".." in Path(member.name).parts or member.issym() or member.islnk():
                        raise ValueError("unexpected archive member")
                bundle.extractall(folder)
            cls.sources[version] = folder / "plugins/agent-org"

    def install_old(self, version):
        tmp = tempfile.TemporaryDirectory(prefix="existing-project[scope]-")
        self.addCleanup(tmp.cleanup)
        target = Path(tmp.name) / "Phase1[Current] with spaces"
        target.mkdir()
        result = subprocess.run([sys.executable, "-I", "-B", "-c", GENERATE, str(self.sources[version]), str(target)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return target

    def hook(self, target, command, session="fixture-PMO-p2-01"):
        event = {"hook_event_name": "PreToolUse", "session_id": session, "cwd": str(target),
                 "tool_name": "exec_command", "tool_input": {"cmd": command}}
        result = subprocess.run([sys.executable, "-I", "-B", str(target / ".agent-org/bin/codex_hook.py")],
                                input=json.dumps(event), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.last_hook = json.loads(result.stdout)
        return self.last_hook.get("hookSpecificOutput", {}).get("permissionDecision")

    def upgrade(self, target, apply=False):
        args = [sys.executable, "-I", "-B", str(ROOT / "scripts/codex_upgrade.py"), "upgrade", "--target", str(target)]
        if apply:
            args.append("--apply")
        # Keep external CLIs unavailable. This must be a local-only upgrade.
        result = subprocess.run(args, text=True, capture_output=True, env=dict(os.environ, PATH="/usr/bin:/bin"))
        return result, json.loads(result.stdout)

    def test_old_hooks_upgrade_preserves_objects_and_enables_document_commands(self):
        for version in REFS:
            with self.subTest(version=version):
                target = self.install_old(version)
                before = json.loads((target / ".agent-org/state/pool.json").read_text())
                records = {rel: (target / rel).read_bytes() for rel in ("feishu/ids.json", "packages/sent/fixture.json")}
                document = "lark-cli docs +create --title fixture --as user"
                self.assertEqual(self.hook(target, document, "fixture-PMO-p0-01"), "deny")
                command = shlex.join([sys.executable, "-I", str(ROOT / "scripts/codex_upgrade.py"),
                                      "upgrade", "--target", str(target), "--apply"])
                for session in ("fixture-PMO-p2-01", "fixture-initializer"):
                    self.assertIsNone(self.hook(target, command, session), self.last_hook)
                preview, data = self.upgrade(target)
                self.assertEqual(preview.returncode, 0, preview.stderr)
                self.assertEqual(data["status"], "preview")
                self.assertEqual(json.loads((target / ".agent-org/manifest.json").read_text())["plugin_version"], version)
                applied, data = self.upgrade(target, True)
                self.assertEqual(applied.returncode, 0, applied.stderr)
                self.assertEqual(data["status"], "applied")
                self.assertEqual((data["remote_actions"], data["chat_actions"]), (0, 0))
                after = json.loads((target / ".agent-org/state/pool.json").read_text())
                for key in ("bindings", "messages", "history", "workspace"):
                    self.assertEqual(before[key], after[key], key)
                self.assertEqual(len(after["bindings"]), 21)
                for rel, original in records.items():
                    self.assertEqual((target / rel).read_bytes(), original, rel)
                self.assertIn("用户自定义规则", (target / "AGENTS.md").read_text())
                self.assertIsNone(self.hook(target, document, "fixture-PMO-p0-01"))
                self.assertIsNone(self.hook(target, document, "fixture-PMO-p2-01"))
                context = subprocess.run([sys.executable, "-I", str(target / ".agent-org/bin/codex_runtime.py"), "context"],
                    capture_output=True, text=True, cwd=target, env=dict(os.environ, CODEX_THREAD_ID="fixture-PMO-p2-01"))
                self.assertEqual(context.returncode, 0, context.stderr)
                self.assertEqual(Path(json.loads(context.stdout)["instructions"]["path"]), (target / "AGENTS.md").resolve())
                manifest = (target / ".agent-org/manifest.json").read_bytes()
                again, _ = self.upgrade(target, True)
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertEqual((target / ".agent-org/manifest.json").read_bytes(), manifest)

    def test_old_local_edit_blocks_entire_upgrade(self):
        for version in REFS:
            with self.subTest(version=version):
                target = self.install_old(version)
                role = target / ".agent-org/roles/pmo-scribe.md"
                role.write_text(role.read_text() + "\n用户修改职责。\n")
                old_manifest = (target / ".agent-org/manifest.json").read_bytes()
                old_hook = (target / ".agent-org/bin/codex_hook.py").read_bytes()
                result, data = self.upgrade(target, True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(data["status"], "conflict")
                self.assertEqual((target / ".agent-org/manifest.json").read_bytes(), old_manifest)
                self.assertEqual((target / ".agent-org/bin/codex_hook.py").read_bytes(), old_hook)
                self.assertTrue(role.with_suffix(".md.new").is_file())


if __name__ == "__main__":
    unittest.main()
