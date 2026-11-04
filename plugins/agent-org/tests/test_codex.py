#!/usr/bin/env python3
"""Offline Codex adapter contracts, real subprocess hooks, no model/API calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import setup
import codex_setup
import codex_runtime as runtime
import codex_hook
import ledger
import route
import orgspec


class CodexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-org-codex-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "Phase1[Current] with spaces"
        self.root.mkdir()
        self.task = Path(self.tmp.name) / "task.json"
        self.task.write_text((ROOT / "scaffold/task.codex.example.json").read_text())
        self.patch = mock.patch.object(setup, "run", return_value=(None, "", "offline test"))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.plan = self.build()
        self.apply()
        self.pool = runtime.Pool(self.root)
        self.pool.configure("local", "project-test")

    def build(self):
        return setup.build_plan(str(self.root), str(self.task), None)

    def apply(self, plan=None):
        plan = plan or self.plan
        return setup.apply_plan(str(self.root), plan, setup.actions_for(str(self.root), plan))

    def bind(self, name, thread=None):
        token = self.pool.reserve(name)["token"]
        return self.pool.bind(token, thread or "thread-" + name, "local")

    def event(self, kind, thread, **extra):
        return codex_hook.handle(dict(hook_event_name=kind, session_id=thread, cwd=str(self.root), **extra), str(self.root))

    def tool(self, thread, command, **extra):
        return self.event("PreToolUse", thread, tool_name="Bash", tool_input={"command": command}, **extra)

    def denied(self, response):
        return response.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def test_layout_rules_and_eight_pins(self):
        self.assertFalse((self.root / ".claude").exists())
        self.assertFalse((self.root / "CLAUDE.md").exists())
        self.assertEqual(self.plan.manifest["upstream_version"], "0.5.1")
        self.assertEqual(len(self.pool.slots), 21)
        pins = [s["role"] for s in self.pool.config["slots"] if s["pin"]]
        self.assertEqual(pins, list(codex_setup.PIN_ROLES))
        self.assertTrue(all(s["model"] == "inherit" for s in self.pool.slots.values()))
        self.assertEqual((self.root / ".agent-org/orgspec/rules.json").read_bytes(), (ROOT / "orgspec/rules.json").read_bytes())

    def test_rerun_preserves_binding_and_handwritten_spec(self):
        binding = self.bind("Alg-p2-01")
        (self.root / "spec/spec.md").write_text("研究员的原文\n")
        self.assertTrue(self.apply(self.build()))
        self.assertEqual(self.pool.context(binding["thread_id"])["role"], "alg-p2")
        self.assertEqual((self.root / "spec/spec.md").read_text(), "研究员的原文\n")
        self.assertTrue(all(k in ("same", "kept") for k, _, _ in setup.actions_for(str(self.root), self.build())))

    def test_merge_existing_instructions_and_hooks(self):
        agents = self.root / "AGENTS.md"
        agents.write_text("我的全局约束\n\n" + agents.read_text() + "\n其他说明\n")
        hooks = self.root / ".codex/hooks.json"
        data = json.loads(hooks.read_text())
        custom = {"hooks": [{"type": "command", "command": "echo user-hook"}]}
        data["hooks"]["SessionStart"].insert(0, custom)
        hooks.write_text(json.dumps(data))
        self.assertTrue(self.apply(self.build()))
        self.assertIn("我的全局约束", agents.read_text())
        self.assertIn("其他说明", agents.read_text())
        self.assertEqual(agents.read_text().count("<!-- agent-org:begin -->"), 1)
        self.assertEqual(json.loads(hooks.read_text())["hooks"]["SessionStart"][0], custom)
        self.assertEqual(len(json.loads(hooks.read_text())["hooks"]["SessionStart"]), 2)

    def test_local_policy_conflict_blocks_whole_upgrade(self):
        policy = self.root / ".agent-org/policy.json"
        policy.write_text(policy.read_text() + " ")
        before = (self.root / ".agent-org/manifest.json").read_bytes()
        changed = self.build()
        changed.files[".agent-org/bin/orgspec.py"] = ("print('new')", True, False)
        old_code = (self.root / ".agent-org/bin/orgspec.py").read_bytes()
        self.assertFalse(self.apply(changed))
        self.assertEqual(before, (self.root / ".agent-org/manifest.json").read_bytes())
        self.assertEqual(old_code, (self.root / ".agent-org/bin/orgspec.py").read_bytes())
        self.assertTrue(Path(str(policy) + ".new").exists())

    def test_claude_defaults_unchanged_and_models_rejected(self):
        task, _, _ = setup.load_task_and_seeds(str(ROOT / "scaffold/task.example.json"))
        self.assertEqual(task["mode"], "cli")
        self.assertEqual(task["models"]["p2"], "opus")
        raw = json.loads(self.task.read_text()); raw["models"] = {"p2": "opus"}
        self.task.write_text(json.dumps(raw))
        with self.assertRaises(setup.SetupError): self.build()

    def test_codex_cli_mode_rejected(self):
        raw = json.loads(self.task.read_text()); raw["mode"] = "cli"
        self.task.write_text(json.dumps(raw))
        with self.assertRaises(setup.SetupError): self.build()

    def test_reserve_reconcile_and_claim_race(self):
        first = self.pool.reserve("PMO-p2-01")
        again = self.pool.reserve("PMO-p2-01")
        self.assertEqual(again["action"], "reconcile_creation")
        self.assertEqual(again["token"], first["token"])
        self.pool.bind(first["token"], "native-id", "local")
        self.assertEqual(self.pool.bind(first["token"], "native-id")["thread_id"], "native-id")
        self.assertEqual(self.pool.reserve("PMO-p2-01")["action"], "reuse")
        with self.assertRaises(runtime.RuntimeError): self.pool.bind(first["token"], "other")

    def test_queued_client_id_is_not_thread_id(self):
        token = self.pool.reserve("PMO-p1-01")["token"]
        self.pool.creation_result(token, "queued", "client-only")
        with self.assertRaises(runtime.RuntimeError): self.pool.bind(token, "client-only")
        self.assertEqual(self.pool.plan()["actions"][1]["action"], "reconcile_creation")
        self.pool.bind(token, "real-thread")

    def test_cancel_records_evidence_before_new_reservation(self):
        token = self.pool.reserve("PMO-p1-01")["token"]
        self.pool.creation_result(token, "unknown")
        with self.assertRaises(runtime.RuntimeError): self.pool.cancel_reservation(token, "")
        self.pool.cancel_reservation(token, "原工具明确返回创建失败，列表核对无会话")
        self.assertNotEqual(token, self.pool.reserve("PMO-p1-01")["token"])
        self.assertEqual(self.pool.load()["history"][0]["token"], token)

    def test_duplicate_identity_and_changed_project_rejected(self):
        self.bind("Alg-p2-01", "same")
        token = self.pool.reserve("Product-p2-01")["token"]
        with self.assertRaises(runtime.RuntimeError): self.pool.bind(token, "same")
        with self.assertRaises(runtime.RuntimeError): self.pool.configure("remote", "other")

    def test_unknown_chat_does_not_become_pmo(self):
        self.assertIsNone(self.pool.context("unknown")["role"])
        with self.assertRaises(runtime.RuntimeError): self.pool.bind("invented-token", "unknown")

    def test_pin_readback_preserves_unrelated_and_user_order(self):
        self.bind("Alg-p2-01", "alg")
        self.bind("PMO-p1-01", "router")
        self.pool.observe({"pinnedThreads": [{"id": "router", "hostId": "local"},
            {"id": "other", "hostId": "local"}, {"id": "alg", "hostId": "local"}, {"id": "cloud", "kind": "chatgpt"}]})
        self.assertFalse(any(a["action"] == "pin" for a in self.pool.plan()["actions"]))
        self.pool.observe({"pinnedThreads": [{"id": "router", "hostId": "local"}]})
        self.assertEqual([a["slot"] for a in self.pool.plan()["actions"] if a["action"] == "pin"], ["Alg-p2-01"])

    def test_unavailable_host_does_not_unpin(self):
        self.bind("Alg-p2-01", "alg")
        self.pool.observe({"pinnedThreads": [{"id": "alg", "hostId": "local"}]})
        with self.assertRaises(runtime.RuntimeError):
            self.pool.observe({"pinnedThreads": [], "unavailableHosts": ["local"]})
        self.assertTrue(self.pool.context("alg")["pinned"])

    def test_replace_preserves_history_and_messages(self):
        self.bind("PMO-p2-01", "pmo"); self.bind("Alg-p2-01", "alg")
        m = self.pool.message("Alg-p2-01", "notify", "仍需处理", thread="pmo")
        with self.assertRaises(runtime.RuntimeError): self.pool.replace("Alg-p2-01", "stale", "重建")
        self.pool.replace("Alg-p2-01", "alg", "旧会话已失效")
        self.bind("Alg-p2-01", "alg-new")
        self.assertEqual(self.pool.context("alg-new")["pending_messages"][0]["id"], m["message_id"])
        self.assertEqual(self.pool.load()["history"][0]["thread_id"], "alg")
        self.assertFalse(self.pool.context("alg-new")["pinned"])

    def test_message_idempotency_and_no_false_receipt(self):
        self.bind("PMO-p2-01", "pmo"); self.bind("Alg-p2-01", "alg")
        m = self.pool.message("Alg-p2-01", "assign", "开工", "package.md", "101", "pmo")
        repeated = self.pool.message("Alg-p2-01", "assign", "开工", "package.md", "101", "pmo")
        self.assertEqual(repeated["action"], "reuse")
        self.assertEqual(repeated["message_id"], m["message_id"])
        self.pool.receipt(m["message_id"], "queued", "原生工具排队回执", "pmo")
        self.assertNotIn("received_at", self.pool.load()["messages"][m["message_id"]])
        self.event("UserPromptSubmit", "alg", prompt=m["prompt"])
        self.pool.receipt(m["message_id"], "delivered", "稍晚返回", "pmo")
        self.assertEqual(self.pool.load()["messages"][m["message_id"]]["status"], "received")
        with self.assertRaises(runtime.RuntimeError): self.pool.acknowledge(m["message_id"], thread="pmo")
        self.pool.acknowledge(m["message_id"], handled=True, thread="alg")
        self.event("UserPromptSubmit", "alg", prompt=m["prompt"])
        self.assertEqual(self.pool.load()["messages"][m["message_id"]]["status"], "handled")

    def test_work_item_required_for_dispatch(self):
        self.bind("PMO-p2-01", "pmo"); self.bind("Alg-p2-01", "alg")
        with self.assertRaises(runtime.RuntimeError): self.pool.message("Alg-p2-01", "assign", "开工", thread="pmo")

    def test_focus_defers_and_does_not_change_business_state(self):
        self.bind("PMO-p2-01", "pmo"); self.bind("Alg-p2-01", "alg")
        self.pool.focus("open", "研究员讨论", "alg")
        self.assertTrue(self.pool.message("Alg-p2-01", "notify", "补充", thread="pmo")["defer"])
        self.event("Stop", "alg")
        self.assertIsNotNone(self.pool.context("alg")["focus"])
        self.assertEqual(self.pool.load()["sessions"]["alg"]["status"], "turn_ended")

    def test_subagent_cannot_inherit_parent_writer(self):
        self.bind("PMO-p0-01", "scribe")
        self.tool("scribe", "pwd")
        before = self.pool.load()["sessions"]["scribe"]
        response = self.tool("scribe", "python3 -I .agent-org/bin/ledger.py register task.md --apply", agent_id="child")
        self.assertTrue(self.denied(response))
        self.assertEqual(before, self.pool.load()["sessions"]["scribe"])

    def test_only_scribe_writes_and_comment_uses_bound_tag(self):
        self.bind("PMO-p0-01", "scribe"); self.bind("Alg-p2-01", "alg")
        cmd = "python3 -I .agent-org/bin/ledger.py register task.md --apply"
        self.assertTrue(self.denied(self.tool("alg", cmd)))
        self.assertFalse(self.denied(self.tool("scribe", cmd)))
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "alg", "AGENT_ORG_TAG": "[PMO+0]"}):
            with self.assertRaises(runtime.RuntimeError): runtime.assert_writer(str(self.root), "register")
            runtime.assert_writer(str(self.root), "comment")
            self.assertEqual(ledger.Ledger(str(self.root), "offline").tag, "[Alg+2]")

    def test_unobserved_hook_blocks_direct_ledger_write(self):
        self.bind("PMO-p0-01", "scribe")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "scribe"}):
            with self.assertRaises(runtime.RuntimeError): runtime.assert_writer(str(self.root), "register")
            self.tool("scribe", "pwd")
            runtime.assert_writer(str(self.root), "register")

    def test_raw_feishu_writes_and_wrappers_denied(self):
        self.bind("PMO-p0-01", "scribe")
        for cmd in ("meegle workitem create --name a", "sh -c 'lark-cli docs +create --title a'",
                    "python3 -c \"import subprocess; subprocess.run(['meegle', 'workitem', 'create'])\""):
            with self.subTest(cmd=cmd): self.assertTrue(self.denied(self.tool("scribe", cmd)))
        self.assertFalse(self.denied(self.tool("scribe", "meegle auth status")))

    def test_apply_patch_protects_move_and_symlink_paths(self):
        self.bind("Alg-p2-01", "alg")
        (self.root / "repo/eval").mkdir(parents=True)
        (self.root / "alias").symlink_to(self.root / "repo/eval", target_is_directory=True)
        for path in ("repo/eval/a.py", ".agent-org/policy.json", "alias/a.py"):
            patch = "*** Begin Patch\n*** Update File: free.py\n*** Move to: %s\n@@\n-a\n+b\n*** End Patch" % path
            self.assertTrue(self.denied(self.event("PreToolUse", "alg", tool_name="apply_patch", tool_input={"input": patch})))

    def test_hook_command_runs_in_bracket_path(self):
        hooks = json.loads((self.root / ".codex/hooks.json").read_text())
        cmd = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.bind("Alg-p2-01", "alg")
        p = subprocess.run(shlex.split(cmd), input=json.dumps({"session_id": "alg", "hook_event_name": "SessionStart", "cwd": str(self.root)}), text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('"role": "alg-p2"', json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_corrupt_state_blocks_hook(self):
        self.pool.file.write_text("{broken")
        cmd = [sys.executable, "-I", "-B", str(self.root / ".agent-org/bin/codex_hook.py")]
        p = subprocess.run(cmd, input=json.dumps({"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "pwd"}, "cwd": str(self.root)}), text=True, capture_output=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("不按空池恢复", p.stderr)

    def test_all_chats_and_pins_still_need_real_hook_evidence(self):
        pinned = []
        for name, s in self.pool.slots.items():
            self.bind(name, name)
            if s["pin"]: pinned.append({"id": name, "hostId": "local"})
        self.pool.observe({"pinnedThreads": pinned})
        self.assertFalse(self.pool.check()["ready"])
        for name in self.pool.slots:
            self.event("SessionStart", name)
            self.event("UserPromptSubmit", name, prompt="开局")
            self.tool(name, "pwd")
            self.event("Stop", name)
        self.assertTrue(self.pool.check()["ready"])

    def test_shared_classifier_reads_codex_focus_and_identity(self):
        self.bind("Alg-p2-01", "alg")
        self.pool.focus("open", "规格", "alg")
        result = route.classify_text(orgspec.OrgSpec(), str(self.root), "请转给 Alg-p2-01", True)
        self.assertEqual(result["row"], "R1")
        self.assertEqual(result["pool"]["thread_id"], "alg")
        self.assertTrue(result["pool"]["researcher_using"])
        self.assertEqual(result["focus"], "alg-p2")

    def test_setup_check_includes_feishu_gaps(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(codex_setup.check(str(self.root), setup), 1)
        self.assertIn("飞书项目空间还没绑定", out.getvalue())

    def test_no_cross_host_in_place_conversion(self):
        other = self.root / ".claude/agent-org/manifest.json"
        other.parent.mkdir(parents=True)
        other.write_text("{}")
        with self.assertRaises(setup.SetupError): self.build()

    def test_reducing_pool_does_not_orphan_bound_slots(self):
        self.bind("Alg-p0-01", "worker")
        raw = json.loads(self.task.read_text()); raw["pool"]["later"] = ["alg"]
        self.task.write_text(json.dumps(raw))
        with self.assertRaises(setup.SetupError): self.build()

    def test_local_modified_role_not_silently_accepted_as_upgrade(self):
        role = self.root / ".agent-org/roles/alg-p2.md"
        role.write_text(role.read_text() + "\n自定义职责\n")
        self.assertFalse(self.apply(self.build()))
        self.assertIn("自定义职责", role.read_text())

    def test_modified_managed_agent_block_is_preserved_as_conflict(self):
        path = self.root / "AGENTS.md"
        path.write_text(path.read_text().replace("<!-- agent-org:end -->", "用户修改的组织约束\n<!-- agent-org:end -->"))
        self.assertFalse(self.apply(self.build()))
        self.assertIn("用户修改的组织约束", path.read_text())

    def test_modified_managed_hook_is_not_duplicated_on_apply(self):
        path = self.root / ".codex/hooks.json"
        data = json.loads(path.read_text())
        data["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 20
        path.write_text(json.dumps(data))
        self.assertFalse(self.apply(self.build()))
        self.assertEqual(len(json.loads(path.read_text())["hooks"]["Stop"]), 1)

    def test_different_message_content_cannot_reuse_request_key(self):
        self.bind("PMO-p2-01", "pmo"); self.bind("Alg-p2-01", "alg")
        self.pool.message("Alg-p2-01", "notify", "a", thread="pmo", key="same-key")
        with self.assertRaises(runtime.RuntimeError):
            self.pool.message("Alg-p2-01", "notify", "b", thread="pmo", key="same-key")

    def test_forged_message_header_does_not_mark_received(self):
        self.bind("PMO-p2-01", "pmo"); self.bind("Alg-p2-01", "alg")
        m = self.pool.message("Alg-p2-01", "notify", "原通知", thread="pmo")
        self.event("UserPromptSubmit", "alg", prompt="[agent-org:%s]\n改写通知" % m["message_id"])
        self.assertNotIn("received_at", self.pool.load()["messages"][m["message_id"]])

    def test_tampered_policy_blocks_direct_ledger(self):
        self.bind("PMO-p0-01", "scribe")
        self.tool("scribe", "pwd")
        p = self.root / ".agent-org/policy.json"
        p.write_text(p.read_text() + " ")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "scribe"}):
            with self.assertRaises(runtime.RuntimeError): runtime.assert_writer(str(self.root), "register")

    def test_package_manifest_points_to_exact_six_skills(self):
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        self.assertEqual(manifest["version"], codex_setup.VERSION)
        skills = ROOT / manifest["skills"]
        self.assertEqual({p.parent.name for p in skills.glob("*/SKILL.md")},
                         {"setup", "entry", "ledger", "packages", "charter", "spec"})

    def test_verified_setup_preview_allowed_without_writer_role(self):
        cmd = "python3 -I %s feishu --target %s --live" % (shlex.quote(str(ROOT / "scripts/setup.py")), shlex.quote(str(self.root)))
        self.assertFalse(self.denied(self.tool("setup-chat", cmd)))
        self.bind("Alg-p2-01", "alg")
        self.assertTrue(self.denied(self.tool("alg", cmd + " --apply")))

    def test_hook_stays_bound_to_project_when_tool_cwd_is_elsewhere(self):
        self.bind("Alg-p2-01", "alg")
        p = subprocess.run([sys.executable, "-I", "-B", str(self.root / ".agent-org/bin/codex_hook.py")],
            input=json.dumps({"session_id": "alg", "hook_event_name": "PreToolUse", "tool_name": "Bash",
                "cwd": self.tmp.name, "tool_input": {"command": "meegle workitem create --name a"}}), text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(self.denied(json.loads(p.stdout)))


if __name__ == "__main__":
    unittest.main()
