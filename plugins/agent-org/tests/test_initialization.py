#!/usr/bin/env python3
"""Setup timing, bounded metadata reads, and restart-safe knowledge node creation."""
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import codex_onboarding
import export_field_dictionary as exporter
import ledger
import setup


class InitializationTest(unittest.TestCase):
    def test_begin_before_initialization_keeps_original_clock(self):
        with tempfile.TemporaryDirectory(prefix="onboarding[scope]-") as root:
            start = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)
            codex_onboarding.clock(root, "begin", start)
            task = Path(__file__).resolve().parents[1] / "scaffold/task.codex.example.json"
            with mock.patch.object(setup, "run", return_value=(None, "", "offline")), contextlib.redirect_stdout(io.StringIO()):
                plan = setup.build_plan(root, str(task), None)
                setup.apply_plan(root, plan, setup.actions_for(root, plan))
            result = codex_onboarding.clock(root, "status", start + dt.timedelta(seconds=70))
            self.assertEqual(result["started_at"], start.isoformat())
            self.assertEqual(result["elapsed_seconds"], 70)
            self.assertTrue((Path(root) / ".agent-org/pool-plan.json").is_file())

    def test_timer_includes_waits_and_never_resets_on_resume(self):
        with tempfile.TemporaryDirectory() as root:
            start = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)
            first = codex_onboarding.clock(root, "begin", start)
            again = codex_onboarding.clock(root, "begin", start + dt.timedelta(seconds=301))
            self.assertEqual(first["started_at"], again["started_at"])
            self.assertFalse(again["within_budget"])
            def check(_):
                print(json.dumps({"ready": True, "issues": []})); return 0
            with mock.patch.object(codex_onboarding.codex_setup, "check", side_effect=check):
                result = codex_onboarding.clock(root, "finish", start + dt.timedelta(seconds=301))
                self.assertFalse(result["timing_pass"])
                self.assertEqual(result["live_acceptance"], "requires_fresh_remote_readbacks")
            self.assertEqual(len(codex_onboarding.clock(root, "status", start + dt.timedelta(seconds=302))["attempts"]), 1)

    def test_timer_cannot_begin_after_completion_or_ignore_missing_setup(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError): codex_onboarding.clock(root, "finish")
            start = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)
            codex_onboarding.clock(root, "begin", start)
            def check(_):
                print(json.dumps({"ready": False, "issues": ["缺少绑定"]})); return 1
            with mock.patch.object(codex_onboarding.codex_setup, "check", side_effect=check):
                result = codex_onboarding.clock(root, "finish", start + dt.timedelta(seconds=20))
                self.assertFalse(result["timing_pass"])

    def test_final_verification_time_is_included(self):
        with tempfile.TemporaryDirectory() as root:
            start = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)
            codex_onboarding.clock(root, "begin", start)
            def check(_):
                print(json.dumps({"ready": True})); return 0
            with mock.patch.object(codex_onboarding, "now", side_effect=[start + dt.timedelta(seconds=299),
                                                                         start + dt.timedelta(seconds=301)]), \
                 mock.patch.object(codex_onboarding.codex_setup, "check", side_effect=check):
                self.assertFalse(codex_onboarding.clock(root, "finish")["timing_pass"])

    def test_metadata_reads_overlap_and_keep_type_identity(self):
        barrier = threading.Barrier(4)
        def page(command, key, type_key):
            if command == "meta-fields":
                barrier.wait(timeout=2)
                return [{"field_name": type_key, "field_key": type_key + "-field", "field_type": "text"}]
            return [{"role_name": type_key, "role_id": type_key + "-role"}]
        types = [{"type_key": str(n), "name": str(n)} for n in range(4)]
        with mock.patch.object(exporter, "paged", side_effect=page):
            result = exporter.export_types(types, "space")
        self.assertEqual(list(result), ["0", "1", "2", "3"])
        for key, item in result.items():
            self.assertEqual(item["fields"][0]["field_key"], key + "-field")
            self.assertEqual(item["roles"][0]["role_id"], key + "-role")

    def test_metadata_failures_do_not_become_partial_success(self):
        with mock.patch.object(exporter, "paged", side_effect=RuntimeError("read failure")):
            with self.assertRaisesRegex(RuntimeError, "read failure"):
                exporter.export_types([{"type_key": "a"}], "space")

    def test_node_setup_lists_each_parent_once_and_rerun_reuses_ids(self):
        with tempfile.TemporaryDirectory(prefix="init[scope]-") as root:
            path = Path(root) / "feishu/ids.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"wiki": {"space_id": "wiki"}}))
            lg = ledger.Ledger(root, "apply")
            calls, created = [], []
            def lark(*args, **kw):
                calls.append(args)
                if args[1] == "+node-list": return {"data": {"nodes": []}}
                self.assertEqual(args[1], "+node-create")
                created.append(args)
                return {"data": {"node_token": "node-" + str(len(created)), "obj_token": "doc-" + str(len(created))}}
            with mock.patch.object(lg, "lark", side_effect=lark), contextlib.redirect_stdout(io.StringIO()):
                ledger.ensure_all_nodes(lg)
                ledger.ensure_all_nodes(lg)
            self.assertEqual(len(created), 29)
            self.assertEqual(sum(c[1] == "+node-list" for c in calls), 4)
            self.assertEqual(len(json.loads(path.read_text())["wiki"]["nodes"]), 29)
            self.assertFalse(hasattr(lg, "_node_listing_cache"))


if __name__ == "__main__":
    unittest.main()
