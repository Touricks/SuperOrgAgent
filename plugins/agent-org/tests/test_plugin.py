#!/usr/bin/env python3
"""Tests for the agent-org plugin. Standard library only, no Feishu access.

    python3 -I -B tests/test_plugin.py
"""
import contextlib
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(ROOT))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import orgspec  # noqa: E402
import setup  # noqa: E402
import ledger  # noqa: E402
import route  # noqa: E402

TASK = os.path.join(ROOT, "scaffold", "task.example.json")


def no_cli(cmd, timeout=30):
    """setup.run during tests: no lark-cli, meegle or claude is ever called."""
    return None, "", "tests do not call %s" % cmd[0]


setup.run = no_cli
FIX = os.path.join(ROOT, "tests", "fixtures", "feishu")
EXAMPLES = os.path.join(ROOT, "skills", "packages", "references", "examples.md")


def example_blocks():
    with open(EXAMPLES, encoding="utf-8") as f:
        return re.findall(r"```markdown\n(.*?)\n```", f.read(), re.S)


SPACE_KEYS = json.load(open(os.path.join(ROOT, "tests", "fixtures", "feishu", "space-keys.json"), encoding="utf-8"))


def fake_feishu(spec):
    """ids.json and field-dictionary.json of a space already bound to the rules. Types and
    fields carry the keys of the space the recorded outputs come from, so fixtures match."""
    types, fd_types, field_keys = {}, {}, {}
    for i, w in enumerate(spec.work_items()):
        real = SPACE_KEYS["types"][w["id"]]
        tkey = real["type_key"]
        types[w["id"]] = {"name": spec.term("wi", w["id"]), "type_key": tkey, "api_name": real["api_name"],
                          "template_id": str(1000 + i)}
        fields = [{"name": "名称", "field_key": "name", "field_type": "_name"},
                  {"name": "模板", "field_key": "template", "field_type": "_work_item_template",
                   "options": [{"id": str(1000 + i), "name": "默认"}]}]
        for f in spec.fields_of(w["id"]):
            key = (SPACE_KEYS["fields"].get(w["id"]) or {}).get(f["id"], {}).get("field_key") \
                or "field_new_%s_%s" % (w["id"], f["id"])
            field_keys.setdefault(w["id"], {})[f["id"]] = key
            rec = {"name": spec.field_name(f["id"]), "field_key": key, "field_type": f.get("type")}
            if f.get("options"):
                rec["options"] = [{"id": "opt_%s_%d" % (f["id"], j), "name": o} for j, o in enumerate(f["options"])]
            fields.append(rec)
        fd_types[tkey] = {"name": spec.term("wi", w["id"]), "api_name": real["api_name"], "fields": fields,
                          "roles": [{"role_id": "role_fake_%d" % i, "role_name": "研究员"}]}
    nodes = {k: {"title": k, "node_token": "node_%s" % k.replace("/", "_")}
             for k in ["overview", "same_seq", "cross_request", "review", "decision", "feedback", "report", "archive"]}
    for s, _, _ in spec.sequences():
        for n in ("same_seq", "review", "archive"):
            nodes["%s/%s" % (n, s)] = {"title": "%s/%s" % (n, s), "node_token": "node_%s_%s" % (n, s)}
    ids = {"schema": "agent-org/ids-v1", "lark": {}, "meegle": {"project_key": "pk_fake", "types": types,
           "field_keys": field_keys, "bound_at": "2026-11-01T00:00:00-05:00", "project_item_id": "9000000001"},
           "wiki": {"space_id": "space_fake", "nodes": nodes}}
    return ids, {"project_key": "pk_fake", "types": fd_types}


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def with_operator(value, **attr):
    """The recorded workitem get answer, with current_status_operator set (and attributes changed)."""
    d = fixture("workitem-get-all.json")
    for f in d["work_item_fields"]:
        if f["key"] == "current_status_operator":
            f["value"] = value
    d["work_item_attribute"].update(attr)
    return d


class StubLedger(ledger.Ledger):
    """A Ledger whose Feishu calls are answered from recorded outputs."""

    def __init__(self, root, mode, replies):
        super().__init__(root, mode)
        self.replies = replies
        self.calls = []

    def run(self, argv, write, stdin=None):
        self.calls.append((list(argv), write, stdin))
        if write and self.mode != "apply":
            self.planned.append(" ".join(argv))
            return None
        for tokens, value in self.replies:
            if all(tok in argv for tok in tokens):
                if isinstance(value, Exception):
                    raise value
                return value
        if write:
            return {}
        raise AssertionError("没有录下的输出：%s" % argv[:5])


class ProjectCase(unittest.TestCase):
    """A fresh initialised project in a temp dir, with fake Feishu IDs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent-org-test-")
        self.spec = orgspec.OrgSpec()
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(self.tmp, TASK, None)
            setup.apply_plan(self.tmp, plan, setup.actions_for(self.tmp, plan))
        ids, fd = fake_feishu(self.spec)
        for name, obj in (("ids.json", ids), ("field-dictionary.json", fd)):
            with open(os.path.join(self.tmp, "feishu", name), "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
        self.hooks = os.path.join(self.tmp, ".claude", "hooks")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hook(self, script, data):
        env = dict(os.environ, CLAUDE_PROJECT_DIR=self.tmp)
        p = subprocess.run([sys.executable, "-I", "-B", os.path.join(self.hooks, script)],
                           input=json.dumps(data), capture_output=True, text=True, env=env)
        out = json.loads(p.stdout) if p.stdout.strip() else {}
        return p.returncode, out.get("hookSpecificOutput", {})

    def guard(self, agent_type, tool, tool_input, agent_id="a1"):
        data = {"tool_name": tool, "tool_input": tool_input, "cwd": self.tmp, "session_id": "s1",
                "agent_type": agent_type}
        if agent_id:
            data["agent_id"] = agent_id
        return self.hook("guard.py", data)

    def draft(self, block_no, name="d.md", edit=None):
        text = example_blocks()[block_no]
        if edit:
            text = edit(text)
        path = os.path.join(self.tmp, "packages", "drafts", "alg", name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def run_quiet(self, fn, *args, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = fn(*args, **kw)
        return res, buf.getvalue()


# ---------------------------------------------------------------- rules
class RulesTest(unittest.TestCase):
    def test_rules_valid(self):
        self.assertEqual(orgspec.OrgSpec().validate(), [])

    def test_examples_pass_lint(self):
        spec = orgspec.OrgSpec()
        for i, block in enumerate(example_blocks()):
            errors, _ = orgspec.lint_text(spec, block)
            self.assertEqual(errors, [], "示例 %d：%s" % (i, errors))

    def test_lint_catches_problems(self):
        spec = orgspec.OrgSpec()
        bad = example_blocks()[0].replace("## 授权与边界", "## 自己加的").replace("关联工作项：#2001", "关联工作项：委托包 #2001")
        errors, _ = orgspec.lint_text(spec, bad)
        joined = "\n".join(errors)
        self.assertIn("旧包名", joined)
        self.assertIn("缺栏目“授权与边界”", joined)
        self.assertIn("不临时加栏目", joined)

    def test_no_retired_names_in_plugin_docs(self):
        spec = orgspec.OrgSpec()
        # These files describe what the live space is called today, so old names are expected there.
        allowed = {"terms.zh.json", "rules.json", "structure.md", "test_plugin.py"}
        hits = []
        for base, _, files in os.walk(REPO):
            # .git and the recorded Feishu outputs keep the names the live space uses
            if "/.git" in base or "/fixtures" in base:
                continue
            for name in files:
                if name in allowed or not name.endswith((".md", ".json", ".tmpl", ".py")):
                    continue
                with open(os.path.join(base, name), encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                for old in spec.terms["retired"]:
                    if re.search(re.escape(old) + "(?![含括装办])", text):
                        hits.append("%s：%s" % (os.path.relpath(os.path.join(base, name), REPO), old))
        self.assertEqual(hits, [])

    def test_principles_tables_match_rules(self):
        spec = orgspec.OrgSpec()
        with open(os.path.join(ROOT, "skills", "packages", "references", "principles.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn(spec.package_table(), text)
        self.assertIn(spec.relation_matrix(), text)


    def test_ledger_structure_matches_rules(self):
        spec = orgspec.OrgSpec()
        with open(os.path.join(ROOT, "skills", "ledger", "references", "structure.md"), encoding="utf-8") as f:
            text = f.read()
        for part in (spec.work_items_table(), spec.flows_table(), spec.fields_table(), spec.docs_tree()):
            self.assertIn(part, text)

    def test_skills_have_frontmatter(self):
        for name in ("setup", "charter", "packages", "ledger", "entry"):
            with open(os.path.join(ROOT, "skills", name, "SKILL.md"), encoding="utf-8") as f:
                head = f.read().split("---")[1]
            self.assertIn("name: %s" % name, head)
            self.assertIn("description:", head)


# ---------------------------------------------------------------- setup
class SetupTest(ProjectCase):
    def test_idempotent(self):
        plan = setup.build_plan(self.tmp, TASK, None)
        acts = setup.actions_for(self.tmp, plan)
        changed = {rel for k, rel, _ in acts if k not in ("same", "kept")}
        self.assertEqual(changed, {"CLAUDE.md", "feishu/README.md"})   # they quote the new project_key
        self.assertEqual({k for k, rel, _ in acts if rel in changed}, {"update"})
        self.run_quiet(setup.apply_plan, self.tmp, plan, acts)
        plan = setup.build_plan(self.tmp, TASK, None)
        kinds = {k for k, _, _ in setup.actions_for(self.tmp, plan)}
        self.assertTrue(kinds <= {"same", "kept"}, kinds)

    def test_local_edit_goes_to_new(self):
        path = os.path.join(self.tmp, "CLAUDE.md")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n本地补充\n")
        plan = setup.build_plan(self.tmp, TASK, None)
        acts = dict((rel, k) for k, rel, _ in setup.actions_for(self.tmp, plan))
        self.assertEqual(acts["CLAUDE.md"], "new")
        self.assertEqual(acts["org/decisions.md"], "same")

    def test_spawn_graph_and_entry(self):
        with open(os.path.join(self.hooks, "policy.json"), encoding="utf-8") as f:
            pol = json.load(f)
        self.assertEqual(pol["main_agent"], "pmo-p2")
        agents = pol["agents"]
        self.assertIn("alg-p2", agents["pmo-p2"]["spawn"])
        self.assertNotIn("alg-p2", agents["pmo-router"]["spawn"])
        self.assertEqual(agents["alg-p2"]["spawn"], ["alg-p1", "pmo-scribe"])
        self.assertEqual(agents["alg-p0"]["spawn"], [])
        self.assertTrue(agents["pmo-scribe"]["ledger_write"])
        self.assertFalse(agents["pmo-p2"]["ledger_write"])
        with open(os.path.join(self.tmp, ".claude", "settings.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["agent"], "pmo-p2")

    def test_subset_of_sequences(self):
        tmp = tempfile.mkdtemp()
        try:
            plan = setup.build_plan(tmp, TASK, "product,alg,quality,pmo")
            agents = [p for p in plan.files if p.startswith(".claude/agents/")]
            self.assertEqual(len(agents), 12)
            with self.assertRaises(setup.SetupError):
                setup.build_plan(tmp, TASK, "product,alg")
        finally:
            shutil.rmtree(tmp)

    def test_ids_of_another_shape_start_empty(self):
        ids, notes = setup.normalise_ids({"meegle": {"project_key": "pk"}})
        self.assertEqual(ids, {"schema": "agent-org/ids-v1", "lark": {}, "meegle": {}, "wiki": {}})
        self.assertTrue(notes)
        same, notes = setup.normalise_ids({"schema": "agent-org/ids-v1", "meegle": {"project_key": "pk"}})
        self.assertEqual((same["meegle"]["project_key"], notes), ("pk", []))


# ---------------------------------------------------------------- hooks
class HookTest(ProjectCase):
    def test_p0_needs_session_name(self):
        _, out = self.guard("alg-p1", "Agent", {"subagent_type": "alg-p0", "prompt": "去跑消融"})
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("alg-p1", "Agent", {"subagent_type": "alg-p0", "prompt": "会话名：Alg-p0-2003\n去跑消融"})
        self.assertNotEqual(out.get("permissionDecision"), "deny")

    def test_cross_sequence_spawn_denied(self):
        _, out = self.guard("alg-p1", "Agent", {"subagent_type": "quality-p2", "prompt": "核查"})
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("alg-p2", "Agent", {"subagent_type": "pmo-scribe", "prompt": "登记请求"})
        self.assertNotEqual(out.get("permissionDecision"), "deny")

    def test_only_scribe_writes_ledger(self):
        cmd = {"command": "python3 -I .claude/agent-org/bin/ledger.py register packages/drafts/alg/x.md --apply"}
        _, out = self.guard("alg-p2", "Bash", cmd)
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("pmo-scribe", "Bash", cmd)
        self.assertNotEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("alg-p2", "Bash", {"command": "python3 -I .claude/agent-org/bin/ledger.py query open"})
        self.assertNotEqual(out.get("permissionDecision"), "deny")

    def test_tampered_script_not_trusted(self):
        cmd = {"command": "python3 -I .claude/agent-org/bin/ledger.py query open"}
        _, out = self.guard("alg-p2", "Bash", cmd)
        self.assertNotEqual(out.get("permissionDecision"), "deny")
        with open(os.path.join(self.tmp, ".claude", "agent-org", "bin", "ledger.py"), "a", encoding="utf-8") as f:
            f.write("\n# changed\n")
        _, out = self.guard("alg-p2", "Bash", cmd)
        self.assertEqual(out.get("permissionDecision"), "deny")

    def test_meegle_write_only_scribe(self):
        cmd = {"command": "meegle workitem update --work-item-id 1 --project-key pk --fields '[]'"}
        _, out = self.guard("alg-p1", "Bash", cmd)
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("alg-p1", "Bash", {"command": "meegle comment add --work-item-id 1 --content hi"})
        self.assertNotEqual(out.get("permissionDecision"), "deny")

    def test_drafts_only_own_sequence(self):
        own = os.path.join(self.tmp, "packages", "drafts", "alg", "a.md")
        other = os.path.join(self.tmp, "packages", "drafts", "rnd", "a.md")
        _, out = self.guard("alg-p1", "Write", {"file_path": own})
        self.assertEqual(out.get("permissionDecision"), "allow")
        _, out = self.guard("alg-p1", "Write", {"file_path": other})
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("alg-p1", "Write", {"file_path": os.path.join(self.tmp, "repo", "eval", "x.py")})
        self.assertEqual(out.get("permissionDecision"), "deny")

    def test_focus_session_blocks_dispatch(self):
        code, out = self.hook("instances.py", {"hook_event_name": "SessionStart", "session_id": "focus1",
                                               "agent_type": "alg-p2", "cwd": self.tmp, "source": "startup"})
        self.assertEqual(code, 0)
        self.assertIn("聚焦会话", out.get("additionalContext", ""))
        _, out = self.guard("pmo-p2", "Agent", {"subagent_type": "alg-p2", "prompt": "继续"}, agent_id=None)
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertIn("聚焦会话", out.get("permissionDecisionReason", ""))
        self.hook("instances.py", {"hook_event_name": "SessionEnd", "session_id": "focus1", "agent_type": "alg-p2",
                                   "cwd": self.tmp})
        _, out = self.guard("pmo-p2", "Agent", {"subagent_type": "alg-p2", "prompt": "继续"}, agent_id=None)
        self.assertNotEqual(out.get("permissionDecision"), "deny")

    def test_inbox_shown_once(self):
        with contextlib.redirect_stdout(io.StringIO()):
            route.notify(self.spec, self.tmp, "alg-p2", "规格 v3 已签字", work_item="2120")
        data = {"hook_event_name": "UserPromptSubmit", "session_id": "f", "agent_type": "alg-p2", "cwd": self.tmp,
                "prompt": "继续"}
        _, out = self.hook("inbox.py", data)
        self.assertIn("规格 v3 已签字", out.get("additionalContext", ""))
        _, out = self.hook("inbox.py", data)
        self.assertEqual(out, {})


# --------------------------------------------------------------- ledger
class LedgerTest(ProjectCase):
    def lg(self):
        return ledger.Ledger(self.tmp, "offline")

    def test_register_execute_request(self):
        path = self.draft(6, edit=lambda t: t.replace("关联工作项：#2106", "关联工作项：待建"))
        lg = self.lg()
        _, out = self.run_quiet(ledger.register, lg, path, "2200")
        self.assertIn("workitem create --work-item-type package", out)
        self.assertIn("docs +create --parent-token", out)
        cmds = "\n".join(lg.planned)
        self.assertIn("opt_relation_1", cmds)          # 执行 matches the legacy option 执行包
        self.assertIn("field_43cb8a", cmds)            # source_item
        self.assertIn("Quality-p0-", cmds)             # owner session of the +0

    def test_register_rejects_reply(self):
        path = self.draft(7)
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.register, self.lg(), path)

    def test_register_review_goes_to_sequence_node(self):
        path = self.draft(4)
        lg = self.lg()
        self.run_quiet(ledger.register, lg, path)
        self.assertIn("node_review_alg", "\n".join(lg.planned))

    def test_researcher_decision_prints_transition(self):
        lg = self.lg()
        _, out = self.run_quiet(ledger.register, lg, self.draft(9))
        self.assertIn("transition", out)
        self.assertIn("decision_content=", out)
        self.assertIn("decider=研究员", out)

    def test_transition_checks_form(self):
        lg = self.lg()
        with self.assertRaises(ledger.LedgerError) as e:
            self.run_quiet(ledger.transition, lg, "2003", "已拒绝", [], "cross_request")
        self.assertIn("accepted_scope", str(e.exception))
        with self.assertRaises(ledger.LedgerError) as e:
            self.run_quiet(ledger.transition, self.lg(), "2003", "已接单", [], "cross_request")
        self.assertIn("不知道当前状态", str(e.exception))
        lg = self.lg()
        _, out = self.run_quiet(ledger.transition, lg, "7151870875", "执行中", [], "same_seq")
        self.assertIn("不知道当前状态", out)
        self.assertFalse([c for c in lg.planned if "workitem update" in c])

    def test_transition_unknown_state(self):
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.transition, self.lg(), "2003", "完成了", [], "same_seq")

    def test_blocked_sets_start_date(self):
        lg = self.lg()
        self.run_quiet(ledger.transition, lg, "2003", "阻塞", ["blocked_reason=等配额"], "same_seq")
        cmds = "\n".join(lg.planned)
        self.assertIn("field_414a78", cmds)
        self.assertRegex(cmds, r"\d{13}")

    def test_dates_are_utc_noon(self):
        self.assertEqual(ledger.utc_noon_ms("2026-11-14"), "1794657600000")

    def test_append_needs_tag(self):
        path = os.path.join(self.tmp, "rec.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("没有标签的记录\n")
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.append, self.lg(), "2003", path)

    def test_queue_mql(self):
        mql = ledger.mql_text(self.lg(), "queue")[0]
        self.assertIn("待决定", mql)
        self.assertIn("pk_fake", mql)


# ---------------------------------------------------------------- route
class RouteTest(ProjectCase):
    def test_id_routes_to_r1(self):
        out = route.classify_text(self.spec, self.tmp, "#7151870875 的访谈记录补上了", offline=True)
        self.assertEqual(out["row"], "R1")

    def test_session_name_routes_to_r1(self):
        out = route.classify_text(self.spec, self.tmp, "Alg-p1-02 那边的消融结果怎样", offline=True)
        self.assertEqual(out["row"], "R1")
        self.assertEqual(out["role"], "alg-p1")

    def test_plain_text_needs_router(self):
        out = route.classify_text(self.spec, self.tmp, "导师说下周想看和人工基线的对比", offline=True)
        self.assertIsNone(out["row"])
        self.assertTrue(out["needs_router"])

    def test_drafts(self):
        cases = {2: ("R7", "alg-p2"), 4: ("R7", "quality-p2"), 8: ("R5", "pmo-scribe"), 9: ("R6", "pmo-scribe")}
        for block, (row, hop) in cases.items():
            out = route.classify_draft(self.spec, self.tmp, self.draft(block, "b%d.md" % block))
            self.assertEqual((out["row"], out.get("next_hop")), (row, hop), block)

    def test_in_sequence_package_not_routed(self):
        out = route.classify_draft(self.spec, self.tmp, self.draft(0))
        self.assertIsNone(out["row"])

    def test_focus_open_writes_inbox(self):
        self.run_quiet(route.focus, self.spec, self.tmp, "open", "alg-p2", "规格 v3", offline=True)
        with open(os.path.join(self.tmp, "inbox", "alg-p2.jsonl"), encoding="utf-8") as f:
            self.assertIn("规格 v3", f.read())
        out = route.classify_draft(self.spec, self.tmp, self.draft(2))
        self.assertTrue(any("聚焦会话" in s for s in out["steps"]))


# ------------------------------------------------- recorded Feishu shapes
class FeishuShapeTest(ProjectCase):
    def stub(self, mode, replies):
        return StubLedger(self.tmp, mode, replies)

    def decision_item(self):
        d = fixture("workitem-get-all.json")
        d["work_item_attribute"].update({"work_item_type": {"key": "6ae688662a2427d9b87b6850", "name": "决策"},
                                         "work_item_status": {"key": "Not started", "name": "待决定"},
                                         "work_item_id": "7152108550"})
        d["work_item_fields"] = [f for f in d["work_item_fields"] if not f["key"].startswith("field_")]
        return d

    def test_item_reads_type_state_name_and_fields(self):
        lg = self.stub("preview", [(("workitem", "get"), fixture("workitem-get-all.json"))])
        it = lg.item("7151870875")
        self.assertEqual((it["type"], it["state"]), ("same_seq", "待开工"))
        self.assertTrue(it["name"].startswith("下发包（Product-p2-01）"))
        self.assertEqual(lg.text_of(it, "sequence"), "产品")
        self.assertEqual(lg.text_of(it, "project_ref"), "7152274970")
        self.assertIsNone(lg.text_of(it, "source_item"))
        self.assertEqual(lg.doc_url("7151870875", it), "https://example.feishu.cn/wiki/wikNodeMainDoc0001")

    def test_transition_takes_id_and_form_from_space(self):
        replies = [(("workitem", "get"), self.decision_item()),
                   (("list-state-transitions",), fixture("transitions-decision.json")),
                   (("workitem", "query"), fixture("workitem-query-empty.json")),
                   (("user", "me"), {"user_key": "u_researcher"})]
        with self.assertRaises(ledger.LedgerError) as e:
            self.run_quiet(ledger.transition, self.stub("preview", replies), "7152108550", "已决定", [])
        self.assertIn("决定内容", str(e.exception))
        self.assertIn("决定人", str(e.exception))
        lg = self.stub("preview", replies)
        self.run_quiet(ledger.transition, lg, "7152108550", "已决定",
                       ["decision_content=从搜索开始计时", "decider=研究员"])
        cmds = "\n".join(lg.planned)
        self.assertIn("--transition-id 40176773", cmds)
        self.assertIn("field_899dbd", cmds)
        self.assertIn("u_researcher", cmds)
        self.assertFalse(any("--user-key" in c[0] for c in lg.calls))

    def test_transition_without_form(self):
        lg = self.stub("preview", [(("workitem", "get"), fixture("workitem-get-all.json")),
                                   (("list-state-transitions",), fixture("transitions-minimal.json"))])
        self.run_quiet(ledger.transition, lg, "7151870875", "执行中", [])
        self.assertIn("--transition-id 40176602", "\n".join(lg.planned))

    def test_terminated_is_not_a_transition(self):
        lg = self.stub("preview", [(("workitem", "get"), fixture("workitem-get-all.json"))])
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.transition, lg, "7151870875", "已终止", [])
        self.assertIn("已终止", ledger.closed_states(self.spec))

    def test_outline_and_status_block_range(self):
        lg = self.stub("preview", [(("workitem", "get"), fixture("workitem-get-all.json")),
                                   (("+fetch", "outline"), fixture("docs-fetch-outline.json")),
                                   (("+fetch", "range"), fixture("docs-fetch-range-status.json"))])
        heads = ledger.heading_ids(lg, "https://example.feishu.cn/wiki/wikNodeMainDoc0001")
        self.assertEqual(len(heads), 12)
        self.assertEqual(heads[-1], ("doxcnakjeLdey6R5sMps4TyITGg", "交接记录"))
        path = os.path.join(self.tmp, "status.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("### 当前状态\n执行中。\n")
        self.run_quiet(ledger.status_block, lg, "7151870875", path)
        argv, write, stdin = lg.calls[-1]
        self.assertTrue(write)
        self.assertEqual(argv[argv.index("--start-block-id") + 1], "doxcnnyQfTdXSmSuMyN3ajvOmgh")
        self.assertEqual(argv[argv.index("--end-block-id") + 1], "doxcnm3kiNhVu6Y7yOgNWnLzddd")
        self.assertEqual(argv[argv.index("--content") + 1], "-")
        self.assertTrue(stdin.startswith("## 状态包"))

    def test_last_block_of_a_list_is_its_last_item(self):
        xml = fixture("docs-fetch-range-status.json")["data"]["document"]["content"]
        self.assertEqual(ledger.last_block_before(xml, "doxcnakjeLdey6R5sMps4TyITGg"), "doxcnm3kiNhVu6Y7yOgNWnLzddd")
        nested = '<fragment><h2 id="a">状态包</h2><p id="b">x</p><table id="t"><tr><td id="c">y</td></tr></table><h2 id="z">交接记录</h2></fragment>'
        self.assertEqual(ledger.last_block_before(nested, "z"), "t")

    def test_register_stores_wiki_link_and_sends_body_on_stdin(self):
        path = self.draft(0, edit=lambda t: t.replace("关联工作项：#2001", "关联工作项：待建"))
        lg = self.stub("apply", [(("user", "me"), {"user_key": "u_researcher"}),
                                 (("workitem", "get"), with_operator(["u_researcher"])),
                                 (("workitem", "create"), {"work_item_id": 7160000001}),
                                 (("docs", "+create"), fixture("docs-create.json")),
                                 (("wiki", "+node-get"), fixture("wiki-node-get.json"))])
        self.run_quiet(ledger.register, lg, path)
        argv, _, stdin = next(c for c in lg.calls if "+create" in c[0])
        self.assertEqual(argv[argv.index("--content") + 1], "-")
        self.assertIn("## 认知层", stdin)
        self.assertIn("## 交接记录", stdin)
        created = next(c for c in lg.calls if c[0][:3] == ["meegle", "workitem", "create"])
        keys = {f["field_key"] for f in json.loads(created[0][created[0].index("--fields") + 1])}
        self.assertIn("role_owners", keys)
        self.assertIn("template", keys)
        update = next(c for c in lg.calls if c[0][:3] == ["meegle", "workitem", "update"])
        self.assertIn("https://example.feishu.cn/wiki/wikNodeMainDoc0001", " ".join(update[0]))
        with open(path, encoding="utf-8") as f:
            self.assertIn("关联工作项：#7160000001", f.read())
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "packages", "sent", "7160000001.json")))

    def test_register_checks_everything_before_creating(self):
        path = self.draft(0, edit=lambda t: t.replace("关联工作项：#2001", "关联工作项：待建"))
        lg = self.stub("apply", [(("user", "me"), {"user_key": "u_researcher"})])
        lg.nodes = {}
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.register, lg, path)
        self.assertFalse([c for c in lg.calls if c[1]])

    def test_register_cross_request_fills_both_sequences(self):
        lg = self.stub("preview", [(("user", "me"), {"user_key": "u_researcher"})])
        self.run_quiet(ledger.register, lg, self.draft(2))
        cmds = "\n".join(lg.planned)
        self.assertIn("field_cd125c", cmds)
        self.assertIn("field_eebd40", cmds)

    def test_query_reads_status_label(self):
        lg = self.stub("preview", [(("workitem", "query"), fixture("workitem-query-queue.json"))])
        rows, _ = self.run_quiet(ledger.query, lg, "queue")
        self.assertEqual(rows[0]["work_item_status"], "待决定")
        self.assertEqual(rows[0]["work_item_id"], 7152108550)

    def test_mql_table_and_conditions(self):
        lg = ledger.Ledger(self.tmp, "offline")
        self.assertIn("RELATIVE_DATETIME_BETWEEN(`updated_at`, 'past', '7d')", ledger.mql_text(lg, "recent")[0])
        self.assertIn("'已终止'", ledger.mql_text(lg, "open")[0])
        queue = ledger.mql_text(lg, "queue")[0]
        self.assertIn("`%s`" % lg.types["decision"]["type_key"], queue)
        lg.types = {}
        with self.assertRaises(ledger.LedgerError):
            ledger.mql_text(lg, "queue")

    def test_bool_and_user_values(self):
        lg = self.stub("preview", [(("user", "me"), {"user_key": "u_researcher"})])
        self.assertEqual(lg.value("same_seq", "needs_human", "是"), "true")
        self.assertEqual(lg.value("same_seq", "needs_human", "否"), "false")
        with self.assertRaises(ledger.LedgerError):
            lg.value("same_seq", "needs_human", "也许")
        self.assertEqual(lg.value("decision", "decider", "研究员"), "u_researcher")


class ListenTest(ProjectCase):
    def listen_with(self, events, open_id="ou_researcher"):
        path = os.path.join(self.tmp, "feishu", "ids.json")
        with open(path, encoding="utf-8") as f:
            ids = json.load(f)
        ids["lark"] = {"researcher_open_id": open_id} if open_id else {}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ids, f, ensure_ascii=False)
        started = []

        class FakeProc:
            def __init__(self, cmd, **kw):
                started.append(cmd)
                self.stdout = io.StringIO("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))

            def wait(self):
                return 0

        real = route.subprocess.Popen
        route.subprocess.Popen = FakeProc
        try:
            code, _ = self.run_quiet(route.listen, self.tmp)
        finally:
            route.subprocess.Popen = real
        inbox = os.path.join(self.tmp, "inbox", "feishu.jsonl")
        lines = []
        if os.path.isfile(inbox):
            with open(inbox, encoding="utf-8") as f:
                lines = [json.loads(l) for l in f if l.strip()]
        return code, lines, started

    def test_researcher_dm_is_taken_once(self):
        ev = fixture("event-message.json")
        code, lines, _ = self.listen_with([ev, ev])
        self.assertEqual(code, 0)
        self.assertEqual([m["text"] for m in lines], ["#7151870875 访谈记录补上了"])
        self.assertEqual(lines[0]["id"], "om_1")

    def test_others_are_ignored(self):
        ev = fixture("event-message.json")
        others = [dict(ev, sender_id="ou_other", message_id="om_2"), dict(ev, chat_type="group", message_id="om_3"),
                  dict(ev, sender_type="bot", message_id="om_4")]
        _, lines, _ = self.listen_with(others)
        self.assertEqual(lines, [])

    def test_refuses_without_open_id(self):
        code, lines, started = self.listen_with([fixture("event-message.json")], open_id=None)
        self.assertEqual(code, 2)
        self.assertEqual(started, [])


class ReadBackTest(ProjectCase):
    def fake_feishu_cli(self, fd, projects=None, export_code=0):
        calls = []

        def fake_run(cmd, timeout=30):
            calls.append(cmd)
            s = " ".join(cmd)
            if "export_field_dictionary.py" in s:
                out_dir = cmd[cmd.index("--out-dir") + 1]
                with open(os.path.join(out_dir, "field-dictionary.json"), "w", encoding="utf-8") as f:
                    json.dump(fd, f, ensure_ascii=False)
                return export_code, "{}", "" if export_code in (0, 3) else "Traceback: boom"
            if "project search" in s:
                return 0, json.dumps({"projects": projects if projects is not None else [
                    {"name": "AutoResearch_New", "simple_name": "sn", "project_key": "pk_new"}]}), ""
            if "whoami" in s:
                return 0, json.dumps({"identity": "user", "onBehalfOf": {"openId": "ou_new"}}), ""
            if "+space-list" in s:
                return 0, json.dumps({"data": {"items": [{"name": "AutoResearch", "space_id": "sp_new"}]}}), ""
            if "view search" in s:
                if "--key-word" not in cmd:
                    return 1, "", "missing required parameter: --key-word"
                word = cmd[cmd.index("--key-word") + 1]
                return 0, json.dumps([{"view_id": "v1", "view_name": "决策队列"}] if word in "决策队列" else []), ""
            return 1, "", "unexpected: %s" % s
        return fake_run, calls

    def read_back(self, fake_run, export_dir, maps=None):
        real = setup.run
        setup.run = fake_run
        try:
            ids = {"schema": "agent-org/ids-v1", "lark": {}, "meegle": {"types": {}}, "wiki": {}}
            fd, found, problems = setup.read_back(self.spec, ids, {"meegle_space": "AutoResearch_New", "wiki_space": "AutoResearch"},
                                                  export_dir, maps or {})
            return ids, fd, found
        finally:
            setup.run = real

    def test_read_back_fills_what_a_new_space_lacks(self):
        fd = {"types": {
            "tk_pkg": {"name": "任务包", "api_name": "package",
                       "fields": [{"field_key": "template", "options": [{"id": "222", "name": "默认"}]}]},
            "tk_cross": {"name": "跨序列需求", "api_name": "cross_request",
                         "fields": [{"field_key": "template", "options": [{"id": "444", "name": "默认"}]},
                                    {"field_key": "field_new001", "name": "需求复述"}]},
            "tk_dec": {"name": "决策", "api_name": "decision",
                       "fields": [{"field_key": "template", "options": [{"id": "333", "name": "默认"}]}]}}}
        fake_run, calls = self.fake_feishu_cli(fd)
        ids, _, found = self.read_back(fake_run, tempfile.mkdtemp(dir=self.tmp))
        self.assertNotIn("same_seq", ids["meegle"]["types"])          # 任务包 is not the type's name
        ids, _, found = self.read_back(fake_run, tempfile.mkdtemp(dir=self.tmp), {"same_seq": "任务包"})
        m = ids["meegle"]
        self.assertEqual((m["project_key"], m["simple_name"]), ("pk_new", "sn"))
        self.assertEqual((m["types"]["same_seq"]["type_key"], m["types"]["same_seq"]["template_id"]), ("tk_pkg", "222"))
        self.assertEqual(m["field_keys"]["cross_request"]["restatement"], "field_new001")
        self.assertEqual(ids["lark"]["researcher_open_id"], "ou_new")
        self.assertEqual(ids["wiki"]["space_id"], "sp_new")
        self.assertTrue(found["决策队列"])
        self.assertFalse(found["阻塞"])
        self.assertTrue(all("--key-word" in c for c in calls if "view" in c))
        lg = ledger.Ledger(self.tmp, "offline")
        lg.field_keys = m["field_keys"]
        self.assertEqual(lg.field("cross_request", "restatement")["field_key"], "field_new001")

    def test_read_back_needs_one_exact_project(self):
        fd = {"types": {}}
        two = [{"name": "AutoResearch_New", "simple_name": "a", "project_key": "p1"},
               {"name": "AutoResearch_New", "simple_name": "b", "project_key": "p2"}]
        fake_run, _ = self.fake_feishu_cli(fd, projects=two)
        with self.assertRaises(setup.SetupError):
            self.read_back(fake_run, tempfile.mkdtemp(dir=self.tmp))
        fake_run, _ = self.fake_feishu_cli(fd, projects=[{"name": "AutoResearch_Newer", "simple_name": "c", "project_key": "p3"}])
        with self.assertRaises(setup.SetupError):
            self.read_back(fake_run, tempfile.mkdtemp(dir=self.tmp))

    def test_export_failure_stops_read_back(self):
        fake_run, _ = self.fake_feishu_cli({"types": {}}, export_code=1)
        with self.assertRaises(setup.SetupError):
            self.read_back(fake_run, tempfile.mkdtemp(dir=self.tmp))
        fake_run, _ = self.fake_feishu_cli({"types": {}, "missing_types": ["feedback"]}, export_code=3)
        with self.assertRaises(setup.SetupError):
            self.read_back(fake_run, tempfile.mkdtemp(dir=self.tmp))

    def test_view_search_failure_is_not_fatal(self):
        fd = {"types": {"tk_dec": {"name": "决策", "api_name": "decision", "fields": []}}}
        fake_run, _ = self.fake_feishu_cli(fd)

        def flaky(cmd, timeout=30):
            if "view" in cmd and "search" in cmd:
                return 1, "", "server busy"
            return fake_run(cmd, timeout)
        (ids, _, found), out = self.run_quiet(self.read_back, flaky, tempfile.mkdtemp(dir=self.tmp))
        self.assertIsNone(found["决策队列"])
        self.assertEqual(ids["meegle"]["project_key"], "pk_new")
        self.assertIn("没能读回视图", out)

    def test_migration_lists_what_was_missing(self):
        ids, fd = fake_feishu(self.spec)
        for t in fd["types"].values():
            if t["name"] == "反馈":
                t["fields"] = [f for f in t["fields"] if f["name"] != "主文档"]
            for f in t["fields"]:
                if f["name"] == "关系":
                    f["options"] = [{"id": "o1", "name": "下发包"}, {"id": "o2", "name": "执行包"}]
        problems = setup.bind(self.spec, ids, fd, {})
        md = setup.migration_md(self.spec, ids, fd, {"决策队列": True}, problems)
        self.assertIn("| 反馈的“主文档” | 空间里没有 |", md)
        self.assertIn("缺选项：下发、执行", md)
        self.assertIn("## 网页上还要做的事", md)
        self.assertNotIn("模板空间", md)
        self.assertIn("研究员的 open_id", md)
        self.assertIn("| 阻塞 | 同序列需求 | 待核对 |", md)
        self.assertIn("| 决策队列 | 决策 | 已有 | 确认已放进空间置顶", md)


class SecondRoundTest(ProjectCase):
    """Fixes from the independent check of the first round."""

    def stub(self, mode, replies):
        return StubLedger(self.tmp, mode, replies)

    def item_json(self, type_key, state, fields=()):
        d = fixture("workitem-get-all.json")
        d["work_item_attribute"].update({"work_item_type": {"key": type_key, "name": "x"},
                                         "work_item_status": {"key": "k", "name": state}})
        d["work_item_fields"] = [f for f in d["work_item_fields"] if not f["key"].startswith("field_")] + list(fields)
        return d

    def test_space_form_is_the_requirement(self):
        item = self.item_json("6ae6881809318d94fd50e32a", "待接单")
        offered = {"state_name": "待接单", "transition": [
            {"id": 41, "state_name": "已接单", "confirm_form": [{"key": "field_39dc54", "name": "回执包"}]}]}
        lg = self.stub("preview", [(("workitem", "get"), item), (("list-state-transitions",), offered)])
        with self.assertRaises(ledger.LedgerError) as e:
            self.run_quiet(ledger.transition, lg, "2030", "已接单", [])
        self.assertIn("delivery_link（交付包", str(e.exception))
        lg = self.stub("preview", [(("workitem", "get"), item), (("list-state-transitions",), offered)])
        _, out = self.run_quiet(ledger.transition, lg, "2030", "已接单", ["delivery_link=https://example.feishu.cn/wiki/x"])
        self.assertIn("--transition-id 41", "\n".join(lg.planned))
        self.assertIn("以空间为准", out)

    def test_space_only_field_is_named_by_rule_id(self):
        item = self.item_json("6ae688662a2427d9b87b6850", "待决定")
        offered = {"state_name": "待决定", "transition": [
            {"id": 7, "state_name": "已过期", "confirm_form": [{"key": "field_9ab577", "name": "建议"}]}]}
        lg = self.stub("preview", [(("workitem", "get"), item), (("list-state-transitions",), offered)])
        with self.assertRaises(ledger.LedgerError) as e:
            self.run_quiet(ledger.transition, lg, "2120", "已过期", [])
        self.assertIn("recommendation（建议", str(e.exception))

    def test_closed_states_for_routing(self):
        for state in ("已验收", "已终止", "归档"):
            self.assertIn(state, ledger.closed_states(self.spec))
        self.assertIn("已过期", ledger.closed_states(self.spec))
        real = ledger.Ledger.run
        try:
            for state in ("已验收", "已终止"):
                item = self.item_json("6ae687b383e512df18483426", state)
                ledger.Ledger.run = lambda self, argv, write, stdin=None, item=item: item
                out = route.classify_text(self.spec, self.tmp, "#7151870875 补一句", offline=False)
                self.assertEqual(out["row"], "R3", state)
        finally:
            ledger.Ledger.run = real

    def test_archive_needs_the_sequence_node(self):
        lg = self.stub("preview", [(("workitem", "get"), fixture("workitem-get-all.json"))])
        lg.nodes.pop("archive/product")
        with self.assertRaises(ledger.LedgerError) as e:
            self.run_quiet(ledger.archive, lg, "7151870875")
        self.assertIn("ensure-node archive/product", str(e.exception))

    def test_project_is_not_registered_twice(self):
        path = os.path.join(self.tmp, "packages", "drafts", "pmo", "charter.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 立项单：AutoResearch\n\n- 关联工作项：待建\n")
        with self.assertRaises(ledger.LedgerError) as e:
            self.run_quiet(ledger.register, self.stub("preview", []), path)
        self.assertIn("已经有项目", str(e.exception))

    def test_user_key_must_be_real(self):
        lg = self.stub("preview", [(("user", "me"), {"data": {"name": "x"}})])
        with self.assertRaises(ledger.LedgerError):
            lg.me()
        lg = self.stub("preview", [(("user", "me"), {"data": {"userKey": "u9"}})])
        self.assertEqual(lg.me(), "u9")

    def test_failed_register_resumes_without_a_second_work_item(self):
        path = self.draft(0, edit=lambda t: t.replace("关联工作项：#2001", "关联工作项：待建"))
        base = [(("user", "me"), {"user_key": "u_researcher"}),
                (("workitem", "get"), with_operator(["u_researcher"])),
                (("workitem", "create"), {"work_item_id": 7160000002})]
        lg = self.stub("apply", base + [(("docs", "+create"), ledger.LedgerError("network down"))])
        with self.assertRaises(ledger.LedgerError) as e:
            self.run_quiet(ledger.register, lg, path)
        self.assertIn("--resume", str(e.exception))
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.register, self.stub("apply", base), path)
        lg = self.stub("apply", base + [(("docs", "+create"), fixture("docs-create.json")),
                                        (("wiki", "+node-get"), fixture("wiki-node-get.json"))])
        self.run_quiet(ledger.register, lg, path, resume=True)
        self.assertFalse([c for c in lg.calls if c[0][:3] == ["meegle", "workitem", "create"]])
        with open(os.path.join(self.tmp, "packages", "sent", "7160000002.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["status"], "registered")
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.register, self.stub("apply", base), path)

    def test_create_without_an_id_stops(self):
        path = self.draft(0, edit=lambda t: t.replace("关联工作项：#2001", "关联工作项：待建"))
        lg = self.stub("apply", [(("user", "me"), {"user_key": "u_researcher"}), (("workitem", "create"), {"ok": True})])
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.register, lg, path)

    def test_heading_and_excerpt_shapes(self):
        html = '<fragment><h2 align="left" id="s1">状态包</h2><p id="p">x</p><h2 id="z">交接记录</h2></fragment>'
        lg = self.stub("preview", [(("+fetch", "outline"), {"data": {"document": {"content": html}}})])
        self.assertEqual(ledger.heading_ids(lg, "u")[0], ("s1", "状态包"))
        xml = '<fragment><h2 id="a">状态包</h2><excerpt top-block-id="tb"><p id="inner">x</p></excerpt><h2 id="z">交接记录</h2></fragment>'
        self.assertEqual(ledger.last_block_before(xml, "z"), "tb")

    def test_work_item_link_uses_simple_name(self):
        lg = ledger.Ledger(self.tmp, "offline")
        self.assertIsNone(lg.item_url("same_seq", 1))
        lg.simple_name = "cn09lu"
        self.assertEqual(lg.item_url("same_seq", 123), "https://project.feishu.cn/cn09lu/package/detail/123")

    def test_every_query_leaves_out_terminated(self):
        lg = ledger.Ledger(self.tmp, "offline")
        for name in ("open", "cross", "recent", "source:7151870875"):
            for mql in ledger.mql_text(lg, name):
                self.assertIn("'已终止'", mql, name)

    def test_guard_sees_every_ledger_write(self):
        for cmd in ("python3 -I .claude/agent-org/bin/ledger.py \\\n  transition 1 执行中 --apply",
                    "python3 -I .claude/agent-org/bin/ledger.py 'register' x.md",
                    "bash -c 'python3 .claude/agent-org/bin/ledger.py update 1 --set a=b'"):
            _, out = self.guard("alg-p2", "Bash", {"command": cmd})
            self.assertEqual(out.get("permissionDecision"), "deny", cmd)
        _, out = self.guard("alg-p2", "Bash", {"command": "python3 -I .claude/agent-org/bin/ledger.py mql queue"})
        self.assertNotEqual(out.get("permissionDecision"), "deny")


class ThirdRoundTest(ProjectCase):
    """Fixes from the second independent check."""

    def stub(self, mode, replies):
        return StubLedger(self.tmp, mode, replies)

    def test_guard_table(self):
        sys.path.insert(0, self.hooks)
        import importlib
        guard = importlib.import_module("guard")
        reads = [
            "python3 -I .claude/agent-org/bin/ledger.py query open",
            "python3 -I .claude/agent-org/bin/ledger.py --offline get 1",
            "python3 -I .claude/agent-org/bin/ledger.py transition --help",
            "cat .claude/agent-org/bin/ledger.py",
            'grep -rn "ledger.py:" org',
            "ls -la .claude/agent-org/bin/ledger.py*",
            "diff .claude/agent-org/bin/ledger.py .claude/agent-org/bin/ledger.py.new",
            'git commit -m "ledger.py: fix transition"',
            'echo "ledger.py register 由书记员跑"',
            "python3 -I -c \"import ast; ast.parse(open('.claude/agent-org/bin/ledger.py').read())\"",
            "python3 -I .claude/agent-org/bin/route.py classify --text 'ledger.py transition 2030 要改'",
            "cat > packages/drafts/alg/x.md <<'EOF'\nrun python3 .claude/agent-org/bin/ledger.py register x.md\nit's fine\nEOF",
        ]
        writes = [
            "python3 -I .claude/agent-org/bin/ledger.py \\\n  transition 7151870875 执行中 --apply",
            "python3 -X utf8 .claude/agent-org/bin/ledger.py transition 1 执行中 --apply",
            "python3 -I -W ignore .claude/agent-org/bin/ledger.py update 1 --set a=b --apply",
            "python3 -m cProfile -o /dev/null .claude/agent-org/bin/ledger.py transition 1 x",
            "cd .claude/agent-org/bin && python3 - transition 1 x < ledger.py",
            "cat .claude/agent-org/bin/ledger.py | python3 - transition 1 x",
            "PYTHONPATH=.claude/agent-org/bin python3 -m ledger transition 1 x",
            'python3 -I .claude/agent-org/bin/"ledger".py archive 1',
            "python3 $(echo .claude/agent-org/bin/ledger.py) transition 1 x",
            "sub=transition; python3 .claude/agent-org/bin/ledger.py $sub 1 x",
            "bash -c 'python3 .claude/agent-org/bin/ledger.py update 1 --set a=b'",
            "timeout 60 python3 .claude/agent-org/bin/ledger.py append 1 --file r.md",
            "python3 -I .claude/agent-org/bin/ledger.py query open | head -5 && python3 .claude/agent-org/bin/ledger.py archive 3",
        ]
        for cmd in reads:
            self.assertIsNone(guard.ledger_write(cmd), cmd)
        for cmd in writes:
            self.assertTrue(guard.ledger_write(cmd), cmd)

    def test_role_check_reads_the_current_operator(self):
        added = self.stub("apply", [(("user", "me"), {"user_key": "u_researcher"}), (("workitem", "get"), with_operator(None))])
        self.assertEqual(self.run_quiet(ledger.ensure_role, added, "same_seq", "1")[0], "added")
        self.assertTrue(any("--role-operate" in c[0] for c in added.calls if c[1]))
        ok = self.stub("apply", [(("user", "me"), {"user_key": "u_researcher"}),
                                 (("workitem", "get"), with_operator([{"key": "u_researcher", "name": "研究员"}]))])
        self.assertEqual(self.run_quiet(ledger.ensure_role, ok, "same_seq", "1")[0], "ok")
        self.assertFalse([c for c in ok.calls if c[1]])
        d = fixture("workitem-get-all.json")
        d["work_item_fields"] = [f for f in d["work_item_fields"] if f["key"] != "current_status_operator"]
        unknown = self.stub("apply", [(("user", "me"), {"user_key": "u_researcher"}), (("workitem", "get"), d)])
        status, out = self.run_quiet(ledger.ensure_role, unknown, "same_seq", "1")
        self.assertEqual(status, "unknown")
        self.assertIn("网页上核对", out)

    def test_decider_must_be_the_researcher(self):
        lg = self.stub("preview", [(("user", "me"), {"user_key": "u_researcher"})])
        self.assertEqual(lg.value("decision", "decider", "研究员"), "u_researcher")
        self.assertEqual(lg.value("decision", "decider", "u_researcher"), "u_researcher")
        for bad in ("研究员本人", "张三", "<研究员的 user_key>"):
            with self.assertRaises(ledger.LedgerError):
                lg.value("decision", "decider", bad)

    def test_created_id_ignores_nested_ids(self):
        with self.assertRaises(ledger.LedgerError):
            ledger.created_id({"data": {"template": {"id": 11093046}}}, "x")
        self.assertEqual(ledger.created_id({"data": {"work_item_id": 5}}, "x"), "5")
        self.assertEqual(ledger.created_id({"data": {"id": 6}}, "x"), "6")

    def test_resume_after_lookup_failure_does_not_create_a_second_document(self):
        path = self.draft(0, edit=lambda t: t.replace("关联工作项：#2001", "关联工作项：待建"))
        base = [(("user", "me"), {"user_key": "u_researcher"}), (("workitem", "get"), with_operator(["u_researcher"])),
                (("workitem", "create"), {"work_item_id": 7160000003}), (("docs", "+create"), fixture("docs-create.json"))]
        lg = self.stub("apply", base + [(("wiki", "+node-get"), ledger.LedgerError("node-get down"))])
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.register, lg, path)
        with open(os.path.join(self.tmp, "packages", "sent", "7160000003.json"), encoding="utf-8") as f:
            self.assertIn("docx", json.load(f))
        lg = self.stub("apply", base + [(("wiki", "+node-get"), fixture("wiki-node-get.json"))])
        self.run_quiet(ledger.register, lg, path, resume=True)
        self.assertFalse([c for c in lg.calls if "+create" in c[0]])
        self.assertTrue(any("wikNodeMainDoc0001" in " ".join(c[0]) for c in lg.calls if c[0][:3] == ["meegle", "workitem", "update"]))

    def test_resume_reuses_the_source(self):
        path = self.draft(6, edit=lambda t: t.replace("关联工作项：#2106", "关联工作项：待建"))
        base = [(("user", "me"), {"user_key": "u_researcher"}), (("workitem", "get"), with_operator(["u_researcher"])),
                (("workitem", "create"), {"work_item_id": 7160000004})]
        lg = self.stub("apply", base + [(("docs", "+create"), ledger.LedgerError("down"))])
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.register, lg, path, "2200")
        lg = self.stub("apply", base + [(("docs", "+create"), fixture("docs-create.json")),
                                        (("wiki", "+node-get"), fixture("wiki-node-get.json"))])
        self.run_quiet(ledger.register, lg, path, resume=True)
        self.assertFalse([c for c in lg.calls if c[0][:3] == ["meegle", "workitem", "create"]])

    def test_stale_receipt_is_explained(self):
        path = self.draft(0, edit=lambda t: t.replace("关联工作项：#2001", "关联工作项：待建"))
        os.makedirs(os.path.join(self.tmp, "packages", "sent"), exist_ok=True)
        with open(os.path.join(self.tmp, "packages", "sent", "7160000005.json"), "w", encoding="utf-8") as f:
            json.dump({"draft": os.path.relpath(path, self.tmp), "work_item_id": "7160000005", "status": "created"}, f)
        lg = self.stub("apply", [(("workitem", "get"), ledger.LedgerError("not found"))])
        with self.assertRaises(ledger.LedgerError) as e:
            self.run_quiet(ledger.register, lg, path, resume=True)
        self.assertIn("删掉回执", str(e.exception))

    def charter(self):
        path = os.path.join(self.tmp, "packages", "drafts", "pmo", "charter.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 立项单：AutoResearch\n\n- 关联工作项：待建\n\n## 背景\n无\n")
        return path

    def test_charter_for_an_existing_project(self):
        path = self.charter()
        lg = self.stub("apply", [(("docs", "+create"), fixture("docs-create.json")),
                                 (("wiki", "+node-get"), fixture("wiki-node-get.json"))])
        self.run_quiet(ledger.register, lg, path, existing=True)
        self.assertFalse([c for c in lg.calls if c[0][:3] == ["meegle", "workitem", "create"]])
        update = next(c for c in lg.calls if c[0][:3] == ["meegle", "workitem", "update"])
        self.assertIn("9000000001", update[0])
        with open(path, encoding="utf-8") as f:
            self.assertIn("关联工作项：#9000000001", f.read())

    def test_new_project_rewrites_the_charter(self):
        with open(os.path.join(self.tmp, "feishu", "ids.json"), encoding="utf-8") as f:
            ids = json.load(f)
        ids["meegle"].pop("project_item_id")
        with open(os.path.join(self.tmp, "feishu", "ids.json"), "w", encoding="utf-8") as f:
            json.dump(ids, f)
        path = self.charter()
        lg = self.stub("apply", [(("user", "me"), {"user_key": "u_researcher"}), (("workitem", "get"), with_operator(["u_researcher"])),
                                 (("workitem", "create"), {"work_item_id": 7160000006}),
                                 (("docs", "+create"), fixture("docs-create.json")),
                                 (("wiki", "+node-get"), fixture("wiki-node-get.json"))])
        _, out = self.run_quiet(ledger.register, lg, path)
        self.assertIn("负责人角色填的是研究员", out)
        with open(path, encoding="utf-8") as f:
            self.assertIn("关联工作项：#7160000006", f.read())

    def test_migration_without_a_dictionary(self):
        ids, _ = fake_feishu(self.spec)
        md = setup.migration_md(self.spec, ids, None, None)
        self.assertIn("还没有字段字典，没法核对", md)
        self.assertIn("复制至其他工作项", md)
        self.assertIn("| 渠道 | `channel` | 单选 | 决策队列、研究员主动发起 |", md)

    def test_option_lists_of_different_length(self):
        ids, fd = fake_feishu(self.spec)
        for t in fd["types"].values():
            for f in t["fields"]:
                if f["name"] == "关系":
                    f["options"] = [{"id": "o1", "name": "下发"}]
        md = setup.migration_md(self.spec, ids, fd, None, setup.bind(self.spec, ids, fd, {}))
        self.assertIn("缺选项：执行", md)

    def test_map_binds_what_has_another_name(self):
        ids, fd = fake_feishu(self.spec)
        for t in fd["types"].values():
            for f in t["fields"]:
                if f["name"] == "关系":
                    f["name"] = "包类型"
        ids["meegle"]["field_keys"]["same_seq"].pop("relation")
        for name, obj in (("ids.json", ids), ("field-dictionary.json", fd)):
            with open(os.path.join(self.tmp, "feishu", name), "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
        _, out = self.run_quiet(setup.feishu, self.tmp, [], False, False)
        self.assertIn("| 同序列需求的“关系” | 空间里没有 |", out)
        self.run_quiet(setup.feishu, self.tmp, ["same_seq.relation=包类型"], False, True)
        with open(os.path.join(self.tmp, "feishu", "ids.json"), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["meegle"]["field_keys"]["same_seq"]["relation"], "field_d96adf")
        self.assertTrue(saved["meegle"]["bound_at"])
        for t in fd["types"].values():                      # renamed again in the space: the key still binds
            for f in t["fields"]:
                if f["name"] == "包类型":
                    f["name"] = "关系类型"
        problems = setup.bind(self.spec, saved, fd, {})
        self.assertEqual(saved["meegle"]["field_keys"]["same_seq"]["relation"], "field_d96adf")
        self.assertFalse([p for p in problems if "关系" in p[0]])
        with self.assertRaises(setup.SetupError):
            setup.parse_maps(self.spec, ["same_seq.nothing=x"])

    def fake_cli(self, whoami=None, view_answer=None, node_titles=None, projects=None):
        fd = {"types": {"tk_dec": {"name": "决策", "api_name": "decision", "fields": []}}}

        def fake_run(cmd, timeout=30):
            s = " ".join(cmd)
            if "export_field_dictionary.py" in s:
                with open(os.path.join(cmd[cmd.index("--out-dir") + 1], "field-dictionary.json"), "w", encoding="utf-8") as f:
                    json.dump(fd, f)
                return 0, "{}", ""
            if "project search" in s:
                return 0, json.dumps(projects if projects is not None else
                                     {"projects": [{"name": "AutoResearch_New", "simple_name": "sn", "project_key": "pk"}]}), ""
            if "whoami" in s:
                return 0, json.dumps(whoami or {"identity": "user", "onBehalfOf": {"openId": "ou_x"}}), ""
            if "+space-list" in s:
                return 0, json.dumps({"data": {"items": []}}), ""
            if "+node-get" in s:
                tok = cmd[cmd.index("--node-token") + 1]
                return 0, json.dumps({"data": {"title": (node_titles or {}).get(tok, "x")}}), ""
            if "view search" in s:
                return 0, json.dumps(view_answer if view_answer is not None else []), ""
            return 1, "", "unexpected"
        return fake_run

    def read_back(self, fake_run, ids=None):
        real = setup.run
        setup.run = fake_run
        try:
            ids = ids or {"schema": "agent-org/ids-v1", "lark": {}, "meegle": {"types": {}}, "wiki": {}}
            return (ids,) + self.run_quiet(setup.read_back, self.spec, ids,
                                           {"meegle_space": "AutoResearch_New", "wiki_space": "AutoResearch"},
                                           tempfile.mkdtemp(dir=self.tmp), {})
        finally:
            setup.run = real

    def test_bot_identity_gives_no_open_id(self):
        ids, _, out = self.read_back(self.fake_cli(whoami={"identity": "bot", "onBehalfOf": {"openId": "ou_bot"}}))
        self.assertNotIn("researcher_open_id", ids["lark"])
        self.assertIn("飞书私聊监听不会启动", out)

    def test_node_titles_are_refreshed(self):
        ids = {"schema": "agent-org/ids-v1", "lark": {}, "meegle": {"types": {}},
               "wiki": {"nodes": {"same_seq": {"title": "任务包", "node_token": "n1"}}}}
        ids, _, _ = self.read_back(self.fake_cli(node_titles={"n1": "同序列需求包"}), ids)
        self.assertEqual(ids["wiki"]["nodes"]["same_seq"]["title"], "同序列需求包")

    def test_view_answers(self):
        _, (fd, found, _), _ = self.read_back(self.fake_cli(view_answer=[]))
        self.assertIs(found["决策队列"], False)
        _, (fd, found, _), out = self.read_back(self.fake_cli(view_answer={"weird": {"shape": 1}}))
        self.assertIsNone(found["决策队列"])
        self.assertIn("读不懂", out)

    def test_export_skips_types_switched_off(self):
        import export_field_dictionary as efd
        with open(os.path.join(FIX, "meta-types.json"), encoding="utf-8") as f:
            listed = json.load(f)
        off = {t["type_key"] for t in listed["list"] if t["is_disable"] == 1}

        def fake(*args):
            if args[1] == "meta-types":
                return listed
            tkey = args[args.index("--work-item-type") + 1]
            if tkey in off:                                  # what the space answers for them
                raise RuntimeError("work item type %s not found" % tkey)
            return {"list": [], "pagination": {"has_more": False}}
        real, argv = efd.meegle, sys.argv
        out = tempfile.mkdtemp(dir=self.tmp)
        efd.meegle, sys.argv = fake, ["export_field_dictionary.py", "--project-key", "pk", "--out-dir", out]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = efd.main()
        finally:
            efd.meegle, sys.argv = real, argv
        self.assertEqual(code, 0)
        with open(os.path.join(out, "field-dictionary.json"), encoding="utf-8") as f:
            doc = json.load(f)
        self.assertFalse(off & set(doc["types"]))
        self.assertEqual(doc["disabled_types"], ["需求", "缺陷", "版本", "迭代"])
        self.assertEqual(len(doc["types"]), 8)

    def test_a_new_space_in_the_task_starts_a_new_binding(self):
        old = {"schema": "agent-org/ids-v1", "lark": {}, "wiki": {},
               "meegle": {"project_key": "pk_old", "simple_name": "so", "space_name": "旧空间", "bound_at": "x",
                          "types": {"decision": {"type_key": "tk_old"}}, "field_keys": {"decision": {"channel": "field_old"}}}}
        ids, _, out = self.read_back(self.fake_cli(), old)
        m = ids["meegle"]
        self.assertIn("按新空间重新绑定", out)
        self.assertEqual((m["project_key"], m["types"]["decision"]["type_key"]), ("pk", "tk_dec"))
        self.assertNotIn("bound_at", m)
        self.assertNotIn("field_old", json.dumps(m))
        renamed = {"schema": "agent-org/ids-v1", "lark": {}, "wiki": {},
                   "meegle": {"project_key": "pk", "simple_name": "sn", "space_name": "改名前", "types": {}}}
        ids, _, out = self.read_back(self.fake_cli(), renamed)          # same space under a new name: binding kept
        self.assertNotIn("重新绑定", out)
        self.assertEqual(ids["meegle"]["space_name"], "AutoResearch_New")

    def test_a_new_wiki_space_drops_the_old_nodes(self):
        base = self.fake_cli()

        def fake_run(cmd, timeout=30):
            if "+space-list" in " ".join(cmd):
                return 0, json.dumps({"data": {"items": [{"name": "AutoResearch", "space_id": "sp_new"}]}}), ""
            return base(cmd, timeout)
        ids = {"schema": "agent-org/ids-v1", "lark": {}, "meegle": {"types": {}},
               "wiki": {"space_id": "sp_old", "space_name": "旧知识库", "nodes": {"same_seq": {"node_token": "n1"}}}}
        ids, _, out = self.read_back(fake_run, ids)
        self.assertEqual(ids["wiki"]["space_id"], "sp_new")
        self.assertNotIn("nodes", ids["wiki"])
        self.assertIn("原来记下的节点作废", out)

    def test_unexpected_shapes_stop_cleanly(self):
        real = setup.run
        setup.run = self.fake_cli(projects=["AutoResearch_New"])
        try:
            with self.assertRaises(setup.SetupError):
                self.run_quiet(setup.feishu, self.tmp, None, True, True)
        finally:
            setup.run = real
        self.assertFalse([n for n in os.listdir(os.path.join(self.tmp, "feishu")) if n.startswith(".export-")])

    def test_blocked_since_is_the_local_date(self):
        lg = ledger.Ledger(self.tmp, "offline")
        self.run_quiet(ledger.transition, lg, "2003", "阻塞", ["blocked_reason=等配额"], "same_seq")
        self.assertIn(ledger.utc_noon_ms(datetime.date.today().isoformat()), "\n".join(lg.planned))


class FourthRoundTest(ProjectCase):
    """Fixes from the bug hunt and the completeness check."""

    def stub(self, mode, replies):
        return StubLedger(self.tmp, mode, replies)

    def guard_module(self):
        sys.path.insert(0, self.hooks)
        import importlib
        return importlib.import_module("guard")

    def test_guard_shell_forms(self):
        guard = self.guard_module()
        L = ".claude/agent-org/bin/ledger.py"
        reads = [f"python3 -I {L}", f"python3 -m py_compile {L}", f"python3 -m pyflakes {L}",
                 "python3 -I .claude/agent-org/bin/route.py classify --text '问题出在 ledger.py'",
                 "python3 - <<'EOF'\nimport json\nprint(json.load(open('/tmp/ledger-open.json')))\nEOF",
                 f"for q in queue blocked; do python3 -I {L} query $q; done",
                 f"echo python3 {L} transition 1", f"git log -- {L}"]
        writes = [f"python3 -I {L} query open\npython3 -I {L} transition 1 执行中 --apply",
                  f"for id in 1 2; do python3 -I {L} transition $id 归档 --apply; done",
                  f"while read id; do python3 {L} archive $id --apply; done < ids.txt",
                  f"if true; then python3 {L} update 1 --set a=b; fi", f"{{ python3 {L} comment 1 --text x; }}",
                  f"! python3 {L} register x.md", f"echo 1 | xargs -I{{}} python3 {L} transition {{}} 执行中",
                  f"find . -name '*.md' -exec python3 {L} register {{}} \;",
                  f"bash <<'EOF'\npython3 {L} transition 1 x --apply\nEOF",
                  f"python3 - <<'EOF'\nimport subprocess\nsubprocess.run(['python3', '{L}', 'transition', '1', 'x'])\nEOF",
                  f"sudo -u me python3 {L} archive 1", f"chrt -r 1 python3 {L} update 1"]
        for cmd in reads:
            self.assertIsNone(guard.ledger_write(cmd), cmd)
        for cmd in writes:
            self.assertTrue(guard.ledger_write(cmd), cmd)

    def test_append_from_a_file_redirect_is_explained(self):
        cmd = "lark-cli docs +update --doc https://x.feishu.cn/wiki/A --command append --doc-format markdown --content - --as user < rec.md"
        _, out = self.guard("pmo-scribe", "Bash", {"command": cmd})
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertIn("看不到要追加的正文", out.get("permissionDecisionReason", ""))

    def charter(self, name="charter.md"):
        path = os.path.join(self.tmp, "packages", "drafts", "pmo", name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 立项单：AutoResearch\n\n- 关联工作项：待建\n\n## 背景\n%s\n" % name)
        return path

    def test_existing_project_receipts_stay_with_their_draft(self):
        ok = [(("docs", "+create"), fixture("docs-create.json")), (("wiki", "+node-get"), fixture("wiki-node-get.json"))]
        self.run_quiet(ledger.register, self.stub("apply", ok), self.charter("v1.md"), existing=True)
        v2 = self.charter("v2.md")
        other = dict(fixture("docs-create.json"))
        other = {"data": {"document": {"document_id": "docxV2", "url": "https://example.feishu.cn/docx/docxV2"}}}
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.register, self.stub("apply", [(("docs", "+create"), other),
                                                                (("wiki", "+node-get"), ledger.LedgerError("down"))]), v2, existing=True)
        node = {"data": {"node_token": "wikNodeV2", "obj_token": "docxV2"}}
        lg = self.stub("apply", [(("workitem", "get"), with_operator(["u_researcher"])), (("wiki", "+node-get"), node)])
        self.run_quiet(ledger.register, lg, v2, resume=True)
        update = next(c for c in lg.calls if c[0][:3] == ["meegle", "workitem", "update"])
        self.assertIn("wikNodeV2", " ".join(update[0]))
        self.assertFalse([c for c in lg.calls if "+create" in c[0]])

    def test_existing_needs_a_project(self):
        with open(os.path.join(self.tmp, "feishu", "ids.json"), encoding="utf-8") as f:
            ids = json.load(f)
        ids["meegle"].pop("project_item_id")
        with open(os.path.join(self.tmp, "feishu", "ids.json"), "w", encoding="utf-8") as f:
            json.dump(ids, f)
        lg = self.stub("apply", [])
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.register, lg, self.charter(), existing=True)
        self.assertFalse([c for c in lg.calls if c[1]])

    def test_existing_can_resume_after_a_document_failure(self):
        path = self.charter()
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.register, self.stub("apply", [(("docs", "+create"), ledger.LedgerError("down"))]), path, existing=True)
        lg = self.stub("apply", [(("workitem", "get"), with_operator(["u_researcher"])),
                                 (("docs", "+create"), fixture("docs-create.json")), (("wiki", "+node-get"), fixture("wiki-node-get.json"))])
        self.run_quiet(ledger.register, lg, path, resume=True)
        self.assertFalse([c for c in lg.calls if c[0][:3] == ["meegle", "workitem", "create"]])

    def test_reply_rerun_reuses_the_document(self):
        path = self.draft(7, "delivery.md")
        get = with_operator(["u_researcher"], work_item_id="2106")
        base = [(("workitem", "get"), get), (("docs", "+create"), fixture("docs-create.json"))]
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.reply, self.stub("apply", base + [(("wiki", "+node-get"), ledger.LedgerError("down"))]), path)
        lg = self.stub("apply", base + [(("wiki", "+node-get"), fixture("wiki-node-get.json"))])
        self.run_quiet(ledger.reply, lg, path)
        self.assertFalse([c for c in lg.calls if "+create" in c[0]])
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.reply, self.stub("apply", base), path)

    def test_reply_checks_the_target_before_writing(self):
        path = self.draft(7, "delivery.md")
        decision = with_operator(None, work_item_type={"key": "6ae688662a2427d9b87b6850", "name": "决策"})
        lg = self.stub("apply", [(("workitem", "get"), decision)])
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.reply, lg, path)
        self.assertFalse([c for c in lg.calls if c[1]])

    def test_inputs_that_used_to_crash(self):
        lg = ledger.Ledger(self.tmp, "offline")
        _, out = self.run_quiet(ledger.transition, lg, "2003", "阻塞", ["blocked_reason=@研究员 还没批预算"], "same_seq")
        self.assertIn("@研究员 还没批预算", "\n".join(lg.planned))
        self.assertEqual(self.run_quiet(ledger.main, ["--offline", "register", os.path.join(self.tmp, "missing.md")])[0], 1)
        self.assertEqual(ledger.item_id_of("#2110"), "2110")
        with self.assertRaises(ledger.LedgerError):
            ledger.item_id_of("abc")
        self.assertIn("'2110'", ledger.mql_text(lg, "source:#2110")[0])
        with self.assertRaises(ledger.LedgerError):
            ledger.mql_text(lg, "source:2110", "alg")
        self.assertIsNone(ledger.utc_noon_ms("2026-11-31"))

    def test_nodes_need_a_token(self):
        lg = self.stub("apply", [(("wiki", "+node-list"), {"ok": True, "data": {"nodes": [], "has_more": False}}),
                                 (("wiki", "+node-create"), {"data": {}})])
        lg.nodes.pop("archive/alg")
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.ensure_node, lg, "archive/alg")
        with open(os.path.join(self.tmp, "feishu", "ids.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["wiki"]["nodes"]["archive/alg"]["node_token"], "node_archive_alg")
        lg = self.stub("preview", [(("workitem", "get"), fixture("workitem-get-all.json"))])
        lg.nodes["archive/product"] = {"title": "产品", "node_token": None}
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.archive, lg, "7151870875")

    def test_status_section_is_added_before_the_log(self):
        body = ledger.with_status_sections("## 认知层\nx\n\n## 交接记录\n[Product+2] 起草。\n")
        self.assertLess(body.index("## 状态包"), body.index("## 交接记录"))
        self.assertEqual(ledger.with_status_sections(body), body)
        self.assertTrue(ledger.with_status_sections("## 认知层\nx\n").rstrip().endswith("## 交接记录"))

    def test_unreadable_query_output_stops(self):
        lg = self.stub("preview", [(("workitem", "query"), {"data": ["unexpected"]})])
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.query, lg, "queue")

    def test_bound_fields_show_their_keys(self):
        ids, _ = fake_feishu(self.spec)
        ids["meegle"]["field_keys"] = {"cross_request": {"restatement": "field_new001"}, "decision": {"channel": "field_new004"}}
        md = setup.migration_md(self.spec, ids, None, None)
        rows = {line.split(" | ")[1]: line for line in md.splitlines() if line.startswith("| ") and "`" in line}
        self.assertIn("field_new001", rows["`restatement`"])
        self.assertIn("没绑定", rows["`accepted_scope`"])



class FifthRoundTest(ProjectCase):
    """The fourth adversarial check: comments, runners, script files, option clusters, find, eval."""

    L = ".claude/agent-org/bin/ledger.py"

    def setUp(self):
        super().setUp()
        sys.path.insert(0, self.hooks)
        import importlib
        self.g = importlib.import_module("guard")
        self.g.fc.TRUSTED = self.g.trusted_scripts(self.tmp)       # as evaluate() does
        runs = os.path.join(self.tmp, "runs", "alg")
        os.makedirs(runs, exist_ok=True)
        files = {"r.sh": "python3 -I %s register packages/drafts/alg/x.md --apply\n" % self.L,
                 "r.py": "import subprocess\nsubprocess.run(['python3', '-I', '%s', 'register', 'x.md', '--apply'])\n" % self.L,
                 "stats.py": "import json\nprint(len(json.load(open('runs/alg/meegle.json'))))\n",
                 "q.sh": "# list what is open\npython3 -I %s query open\n" % self.L}
        for name, text in files.items():
            with open(os.path.join(runs, name), "w", encoding="utf-8") as f:
                f.write(text)

    def tearDown(self):
        sys.path.remove(self.hooks)
        sys.modules.pop("guard", None)
        sys.modules.pop("feishu_cmd", None)
        sys.modules.pop("instances", None)
        super().tearDown()

    def check(self, reads, writes):
        for cmd in reads:
            self.assertIsNone(self.g.ledger_write(cmd, self.tmp), cmd)
        for cmd in writes:
            self.assertTrue(self.g.ledger_write(cmd, self.tmp), cmd)

    def test_comments_do_not_hide_the_next_line(self):
        L = self.L
        self.check([f"python3 -I {L} get 2106   # current state", "echo see issue#12", "bash runs/alg/q.sh",
                    f"cat > packages/drafts/alg/x.md <<'END-DRAFT'\nWe don't run ledger.py here\nEND-DRAFT"],
                   [f"python3 -I {L} get 2106   # current state\npython3 -I {L} transition 2106 执行中 --apply",
                    f"ls packages/drafts/alg  # check\npython3 -I {L} register packages/drafts/alg/x.md --apply",
                    f"echo see issue#12; python3 -I {L} archive 2106 --apply"])

    def test_runners_scripts_and_option_clusters(self):
        L = self.L
        self.check(["python3 runs/alg/stats.py", f"python3 -m py_compile {L}",
                    "python3 -c \"import json; rows=[]; [rows.append(r['id']) for r in json.load(open('runs/alg/x.json'))]\""],
                   [f"uv run python {L} register x.md --apply", f"uv run {L} update 2106 --set a=b --apply",
                    f"uv run --no-project python3 -I {L} archive 2106 --apply", f"poetry run python {L} archive 1",
                    f"conda run -n base python {L} archive 1",
                    "bash runs/alg/r.sh", "sh runs/alg/r.sh", "source runs/alg/r.sh", ". runs/alg/r.sh",
                    "bash < runs/alg/r.sh", "cat runs/alg/r.sh | sh", "python3 -I runs/alg/r.py",
                    f"cat > runs/alg/r2.sh <<'EOF'\npython3 -I {L} register x.md --apply\nEOF\nbash runs/alg/r2.sh",
                    f"python3 -BW ignore {L} register x.md --apply",
                    "cd .claude/agent-org/bin && python3 -Bm ledger archive 2106 --apply",
                    f"python3 -m cProfile --sort cumtime {L} archive 2106 --apply",
                    f"python3 -m trace --count -C runs/alg {L} archive 1",
                    "cd .claude/agent-org/bin && python3 -m runpy ledger archive 2106 --apply",
                    "cd .claude/agent-org/bin && python3 -m cProfile -m ledger archive 1",
                    "python3 -I -c \"import runpy; runpy.run_path('%s', run_name='__main__')\" register x.md --apply" % L,
                    "python3 -c \"import sys; sys.path.insert(0, '.claude/agent-org/bin'); import ledger as m; m.archive(m.Ledger('.', 'apply'), '1')\"",
                    f"python3 -I {L} register --apply -- -h"])

    def test_find_eval_shells_and_other_interpreters(self):
        L = self.L
        self.check([f"find .claude -name ledger.py -exec cat {{}} \;",
                    f"find .claude/agent-org/bin -name ledger.py -execdir python3 -I {{}} query open \;",
                    f"ls {L}(.)", "reg() { ls; }; reg", f"grep -n update $(ls {L})"],
                   [f"find .claude/agent-org/bin -name ledger.py -execdir python3 -I {{}} archive 2106 --apply \;",
                    f"find . -name '*.md' -exec echo {{}} \; -exec python3 {L} register {{}} \;",
                    f"eval eval eval eval eval python3 -I {L} archive 2106 --apply",
                    f"echo archive 2106 --apply | xargs python3 -I {L}",
                    f"function reg {{ python3 -I {L} register x.md --apply; }}; reg",
                    f"env -S 'python3 -I' {L} archive 2106 --apply",
                    f"CMD='python3 -I {L} archive 2106 --apply'; eval \"$CMD\"",
                    f"CMD='python3 -I {L} archive 2106 --apply'; bash -c \"$CMD\"",
                    f"csh -c 'python3 {L} archive 1'", f"bash -c -- 'python3 {L} archive 1'",
                    "perl -e 'system(\"python3\", \"%s\", \"archive\", \"2106\")'" % L,
                    "node -e \"require('child_process').execFileSync('python3', ['%s', 'archive', '2106'])\"" % L,
                    f"cat > runs/alg/out.txt <<EOF\n$(python3 -I {L} archive 2106 --apply)\nEOF",
                    "python3 -I .claude/agent-org/bin/ledger.p? archive 2106 --apply",
                    "python3 -I .claude/agent-org/bin/ledger.[p]y archive 1", f"python3 -I {L}(.) archive 1"])

    def test_words_in_code_and_text_are_not_commands(self):
        L = self.L
        self.check([f"python3 - <<'PY'\nimport subprocess\nout=subprocess.run(['python3','-I','{L}','query','open'],capture_output=True)\nids=[1]\nids.append(2)\nPY",
                    f"cat > packages/drafts/alg/x.md <<'EOF'\n- 请书记员 ledger.py register 后 ledger.py update 2106\nEOF\npython3 - <<'PY'\nprint(1)\nPY",
                    'git commit -m "ledger.py: fix transition"', f"python3 -I {L} query open 2>/dev/null || echo 'ledger.py: 查询失败'",
                    "python3 -I .claude/agent-org/bin/route.py classify --text 'ledger.py transition 2030 要改'"], [])
        cmd = "python3 -I .claude/agent-org/bin/route.py classify --text 'ledger.py transition 2030 要改'"
        _, out = self.guard("alg-p2", "Bash", {"command": cmd})
        self.assertNotEqual(out.get("permissionDecision"), "deny")

    def test_feishu_commands_after_a_comment_or_in_find(self):
        cmd = "ls  # 先看看\nlark-cli docs +update --doc https://x.feishu.cn/wiki/A --command append --content 'no tag' --as user"
        _, out = self.guard("alg-p2", "Bash", {"command": cmd})
        self.assertEqual(out.get("permissionDecision"), "deny")
        cmd = "# 改字段\nmeegle workitem update --work-item-id 1 --project-key pk --fields '[]'"
        _, out = self.guard("alg-p1", "Bash", {"command": cmd})
        self.assertEqual(out.get("permissionDecision"), "deny")
        cmd = "find . -name '*.md' -exec lark-cli docs +update --doc {} --command overwrite --content x --as user \;"
        _, out = self.guard("alg-p1", "Bash", {"command": cmd})
        self.assertEqual(out.get("permissionDecision"), "deny")
        _, out = self.guard("alg-p1", "Bash", {"command": "grep -rn meegle org/"})
        self.assertNotEqual(out.get("permissionDecision"), "deny")

    def test_odd_input_never_fails_open(self):
        fifo = os.path.join(self.tmp, "pipe")
        os.mkfifo(fifo)
        cmd = "lark-cli docs +update --doc https://x.feishu.cn/wiki/A --command append --content @pipe --as user"
        _, out = self.guard("pmo-scribe", "Bash", {"command": cmd})       # must not hang on the FIFO
        self.assertEqual(out.get("permissionDecision"), "deny")
        code, out = self.guard("alg-p2", "Bash", {"command": "python3 -I %s register \ud800 --apply" % self.L})
        self.assertIn(code, (0, 2))
        if code == 0:
            self.assertEqual(out.get("permissionDecision"), "deny")


class FifthRoundLedgerTest(ProjectCase):
    """Ledger, setup and parser fixes from the fourth check."""

    def stub(self, mode, replies):
        return StubLedger(self.tmp, mode, replies)

    def charter(self, name="charter.md"):
        path = os.path.join(self.tmp, "packages", "drafts", "pmo", name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 立项单：AutoResearch\n\n- 关联工作项：待建\n\n## 背景\n%s\n" % name)
        return path

    DOC = [(("docs", "+create"), fixture("docs-create.json")), (("wiki", "+node-get"), fixture("wiki-node-get.json"))]

    def test_existing_project_writes_the_set_values(self):
        lg = self.stub("apply", self.DOC)
        self.run_quiet(ledger.register, lg, self.charter(), pairs=["budget_total=300"], existing=True)
        update = next(c for c in lg.calls if c[0][:3] == ["meegle", "workitem", "update"])
        self.assertIn(lg.field("project", "budget_total")["field_key"], " ".join(update[0]))
        lg = self.stub("apply", self.DOC)
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.register, lg, self.charter("bad.md"), pairs=["no_such_field=1"], existing=True)
        self.assertFalse([c for c in lg.calls if c[1]])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "packages", "sent", "9000000001-bad.md.json")))

    def test_each_charter_keeps_its_own_receipt(self):
        self.run_quiet(ledger.register, self.stub("apply", self.DOC), self.charter("v1.md"), existing=True)
        v2 = self.charter("v2.md")
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.register, self.stub("apply", [self.DOC[0], (("wiki", "+node-get"), ledger.LedgerError("down"))]),
                           v2, existing=True)
        sent = sorted(os.listdir(os.path.join(self.tmp, "packages", "sent")))
        self.assertIn("9000000001.json", sent)
        self.assertIn("9000000001-v2.md.json", sent)
        with self.assertRaises(ledger.LedgerError) as e:           # v1 is still registered: no second document
            self.run_quiet(ledger.register, self.stub("apply", self.DOC), os.path.join(self.tmp, "packages", "drafts", "pmo", "v1.md"),
                           existing=True)
        self.assertIn("另存成新的草稿文件", str(e.exception))
        lg = self.stub("apply", [(("workitem", "get"), with_operator(["u_researcher"])), self.DOC[1]])
        self.run_quiet(ledger.register, lg, v2, resume=True)
        self.assertFalse([c for c in lg.calls if "+create" in c[0]])

    def test_set_values_from_files(self):
        lg = ledger.Ledger(self.tmp, "offline")
        with open(os.path.join(self.tmp, "usable.md"), "w", encoding="utf-8") as f:
            f.write("结论原文\n")
        self.assertEqual(ledger.set_value(lg, "@usable.md"), "结论原文")
        self.assertEqual(ledger.set_value(lg, "@研究员 还没批预算"), "@研究员 还没批预算")
        self.assertEqual(ledger.set_value(lg, "@@usable.md"), "@usable.md")
        for missing in ("@usabel.md", "@runs/alg/x"):
            with self.assertRaises(ledger.LedgerError):
                ledger.set_value(lg, missing)
        cwd = os.getcwd()
        try:
            os.chdir(os.path.join(self.tmp, "packages"))               # falls back to the project directory
            self.assertEqual(ledger.set_value(lg, "@usable.md"), "结论原文")
        finally:
            os.chdir(cwd)

    def test_query_reads_recorded_shapes_and_pages(self):
        rows = ledger.query_rows(fixture("workitem-query-queue.json"))
        self.assertEqual([r["work_item_id"] for r in rows], [7152108550])
        self.assertEqual(ledger.query_rows(fixture("workitem-query-empty.json")), [])
        for bad in ({"_text": "Error: token expired"}, {"code": 10001, "data": None}, {"data": {"1": "x"}},
                    {"data": {"1": [{"moql_field_list": "x"}]}}, {"data": {"1": ["abc"]}}):
            with self.assertRaises(ledger.LedgerError):
                ledger.query_rows(bad)

        def page(start, n, count=None):
            group = [{"moql_field_list": [{"key": "work_item_id", "value": {"long_value": start + i}}]} for i in range(n)]
            out = {"data": {"1": group}, "session_id": "S1" if count else None,
                   "list": [{"count": count, "group_infos": [{"group_id": "1"}]}] if count else None}
            return out
        pages = [(("--session-id", json.dumps([{"group_id": "1", "page_num": k}])), page(50 * (k - 1), n))
                 for k, n in ((2, 50), (3, 20))]
        lg = self.stub("preview", pages + [(("--mql",), page(0, 50, 120))])
        rows, _ = self.run_quiet(ledger.query, lg, "open")
        self.assertEqual(len(rows), 120)
        lg = self.stub("preview", [(("--session-id",), fixture("workitem-query-page-past-end.json")),
                                   (("--mql",), page(0, 50, 120))])
        rows, out = self.run_quiet(ledger.query, lg, "open")
        self.assertEqual(len(rows), 50)
        self.assertIn("只读出 50 项", out)

    def test_reply_stops_on_an_unknown_type(self):
        path = self.draft(7, "delivery.md")
        story = with_operator(None, work_item_type={"key": "story", "name": "需求"})
        lg = self.stub("apply", [(("workitem", "get"), story)])
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.reply, lg, path)
        self.assertFalse([c for c in lg.calls if c[1]])
        get = with_operator(["u_researcher"], work_item_id="2106")
        with self.assertRaises(ledger.LedgerError) as e:
            self.run_quiet(ledger.reply, self.stub("apply", [(("workitem", "get"), get),
                                                             (("docs", "+create"), ledger.LedgerError("403"))]), path)
        self.assertIn("文档还没建", str(e.exception))

    def test_node_parent_needs_a_token(self):
        lg = self.stub("preview", [])
        lg.nodes["review"] = {"title": "送审", "node_token": None}
        lg.nodes.pop("review/alg", None)
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.ensure_node, lg, "review/alg")

    def test_comment_tag_is_a_session_tag(self):
        lg = ledger.Ledger(self.tmp, "offline")
        self.run_quiet(ledger.comment, lg, "7151870875", "[紧急] 预算超了")
        self.assertIn("'%s [紧急] 预算超了'" % lg.tag, lg.planned[-1])
        self.run_quiet(ledger.comment, lg, "7151870875", "[Alg+1] 收到")
        self.assertIn("'[Alg+1] 收到'", lg.planned[-1])

    def test_type_entries_override_shared_fields(self):
        self.assertIn("导师原话", self.spec.field("feedback", "main_doc").get("note"))
        self.assertIsNone(self.spec.field("same_seq", "main_doc").get("note"))

    def test_check_regenerates_and_compares(self):
        code, out = self.run_quiet(setup.check, self.tmp)       # setUp wrote ids.json after init
        self.assertEqual(code, 1)
        self.assertIn("CLAUDE.md", out)
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(self.tmp, os.path.join(self.tmp, "spec", "task.json"), None)
            setup.apply_plan(self.tmp, plan, setup.actions_for(self.tmp, plan))
        code, out = self.run_quiet(setup.check, self.tmp)
        self.assertEqual(code, 0, out)

    def test_same_seq_registration_says_how_to_start(self):
        lg = ledger.Ledger(self.tmp, "offline")
        path = self.draft(6, "exec.md")
        _, out = self.run_quiet(ledger.register, lg, path, source="7151870875")
        self.assertIn("执行中", out)

    def test_python_clusters_and_heredocs_hiding_feishu_calls(self):
        with open(os.path.join(self.tmp, "w.py"), "w", encoding="utf-8") as f:
            f.write("import subprocess\nsubprocess.run(['lark-cli', 'docs', '+update'])\n")
        for cmd in ("python3 -IW ignore w.py",
                    "python3 -IBc \"import subprocess; subprocess.run(['lark-cli','docs','+update'])\"",
                    "python3 - <<'EOF'\nimport subprocess\nsubprocess.run(['lark-cli', 'docs', '+update'])\nEOF"):
            _, out = self.guard("alg-p2", "Bash", {"command": cmd})
            self.assertEqual(out.get("permissionDecision"), "deny", cmd)
        cmd = "cat > packages/drafts/alg/n.md <<'EOF'\n用 lark-cli docs +fetch 读\nEOF\npython3 - <<'PY'\nprint(1)\nPY"
        _, out = self.guard("alg-p2", "Bash", {"command": cmd})
        self.assertNotEqual(out.get("permissionDecision"), "deny")


class SixthRoundHookTest(ProjectCase):
    """The fifth adversarial check of the hooks, replayed through the real guard.py process."""

    L = ".claude/agent-org/bin/ledger.py"
    LARK_W = "lark-cli docs +update --doc https://x.feishu.cn/wiki/A --command overwrite --content x --as user"

    def setUp(self):
        super().setUp()
        L, W = self.L, self.LARK_W
        files = {
            "runs/alg/r.py": "import subprocess\nsubprocess.run(['python3', '-I', '%s', 'register', 'x.md', '--apply'])\n" % L,
            "runs/alg/r.sh": "python3 -I %s register packages/drafts/alg/x.md --apply\n" % L,
            "runs/alg/r2.py": "import subprocess\nsubprocess.run(['python3', '-I', '../../%s', 'archive', '2106', '--apply'])\n" % L,
            "runs/alg/push.py": "import subprocess\nsubprocess.run(%r.split())\n" % W,
            "runs/alg/push.sh": W + "\n",
            "runs/alg/rx.sh": "#!/bin/sh\npython3 -I %s archive 2106 --apply\n" % L,
            "runs/alg/rx.py": "#!/usr/bin/env python3\nimport subprocess\nsubprocess.run(['python3', '%s', 'archive', '2106', '--apply'])\n" % L,
            "runs/alg/pushx.sh": "#!/bin/sh\n" + W + "\n",
            "runs/alg/batch.py": "import glob, subprocess\nLEDGER = '%s'\nfor path in sorted(glob.glob('packages/drafts/alg/*.md')):\n"
                                 "    subprocess.run(f'python3 -I {LEDGER} register {path} --apply', shell=True, check=True)\n" % L,
            "runs/alg/batch2.py": "import shlex, subprocess\nL = '%s'\nsubprocess.run(shlex.split(f'python3 -I {L} transition 2106 执行中 --apply'))\n" % L,
            "runs/alg/batch3.py": "import subprocess, sys\nsubprocess.run([sys.executable, '-I', '%s'] + 'archive 2106 --apply'.split())\n" % L,
            "runs/alg/report.py": "import json, subprocess\nout = subprocess.run(['python3', '-I', '%s', 'query', 'open', '--json'], capture_output=True, text=True).stdout\n"
                                  "rows = json.loads(out or '[]')\nnames = []\nfor r in rows: print(r['id']); names.append(r['name'])\n"
                                  "n = len(rows)\nprint(f'{n} items need an update')\n" % L,
            "runs/alg/pk.py": "import json\nids = json.load(open('feishu/ids.json'))\nprint(ids['meegle']['project_key'])\n",
            "runs/alg/summarize_ledger.py": "import json, sys\nprint(len(json.load(open(sys.argv[-1]))))\n",
            "runs/alg/stats.py": "import json, sys\nprint(len(json.load(open(sys.argv[1]))))\n",
            "runs/alg/readdoc.py": "import json\n# doc.json was saved earlier by lark-cli docs +fetch\nprint(json.load(open('runs/alg/doc.json'))['title'])\n",
            "runs/alg/analyze_x.py": "print(1)\n",
        }
        for rel, text in files.items():
            path = os.path.join(self.tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            if os.path.basename(rel) in ("rx.sh", "rx.py", "pushx.sh"):
                os.chmod(path, 0o755)

    def check(self, cases):
        for role, cmd, deny in cases:
            code, out = self.guard(role, "Bash", {"command": cmd})
            denied = out.get("permissionDecision") == "deny" or code == 2
            self.assertEqual(denied, deny, "%s: %s → %s" % (role, cmd, out.get("permissionDecisionReason")))

    def test_interpreters_by_path_cd_and_direct_scripts(self):
        self.check([("alg-p2", c, True) for c in (
            ".venv/bin/python runs/alg/r.py", "/usr/bin/python3 runs/alg/r.py", "/bin/bash runs/alg/r.sh",
            "\"python3\" runs/alg/r.py", "ipython runs/alg/r.py", "cd runs/alg && python3 r.py", "cd runs/alg && bash r.sh",
            "cd runs/alg && python3 r2.py", "./runs/alg/rx.sh", "./runs/alg/rx.py", "uv run runs/alg/r.py",
            "nohup ./runs/alg/rx.sh", "bash -lc ./runs/alg/rx.sh", "cd runs/alg && python3 push.py",
            "uv run runs/alg/push.py", "./runs/alg/pushx.sh")])

    def test_heredocs_inside_substitutions_and_comments(self):
        L, W = self.L, self.LARK_W
        g = "git commit -q -m \"$(cat <<'EOF'\nAdd eval script\nEOF\n)\" && "
        pyl = "python3 - <<'PY'\nimport subprocess\nsubprocess.run(['python3', '-I', '%s', 'archive', '2106', '--apply'])\nPY" % L
        pyf = "python3 - <<'PY'\nimport subprocess\nsubprocess.run(%r.split())\nPY" % W
        self.check([
            ("alg-p2", g + pyl, True), ("alg-p2", g + pyf, True),
            ("alg-p2", "TITLE=\"$(cat <<'EOF'\n周报\nEOF\n)\"\ncat > packages/drafts/alg/n.md <<'EOF'\n用 lark-cli docs +fetch 读原文\nEOF\n"
                       "python3 - <<'PY'\nprint(1)\nPY", False),
            ("alg-p2", "TITLE=\"$(cat <<'EOF'\n周报\nEOF\n)\"\ncat > packages/drafts/alg/n.md <<'EOF'\npython3 -I %s register n.md --apply\nEOF\n"
                       "python3 - <<'PY'\nprint(1)\nPY" % L, False),
            ("alg-p2", "X=\"$(python3 - <<'PY'\nimport subprocess\nsubprocess.run(['python3', '%s', 'archive', '2106', '--apply'])\nPY\n)\"" % L, True),
            ("alg-p2", "X=\"$(python3 - <<'PY'\nimport subprocess\nsubprocess.run(%r.split())\nPY\n)\"" % W, True),
            ("alg-p2", "MSG=$(cat <<'EOF'\nfix\nEOF\n) && bash <<'SH'\npython3 -I %s archive 2106 --apply\nSH" % L, True),
            ("alg-p2", "# drafts are written with cat <<EOF, not here\npython3 -I %s archive 2106 --apply" % L, True),
            ("alg-p1", "ls runs/alg  # then cat <<'EOF' > notes\nmeegle workitem update --work-item-id 1 --project-key pk --fields '[]'", True),
            ("alg-p2", "grep -rn \"<<EOF\" runs/alg || true\n" + W, True),
            ("alg-p2", "echo 'use <<EOF for drafts'\npython3 -I %s archive 2106 --apply" % L, True),
            ("alg-p2", "git commit -q -m \"$(cat <<'EOF'\nSay \"use lark-cli docs +fetch first\nEOF\n)\"", False),
            ("alg-p2", "ls runs/alg # list \\\npython3 -I %s archive 2106 --apply" % L, True),
            ("alg-p2", "python3 - <<'PY'\nimport json\n# doc.json was saved earlier by lark-cli docs +fetch\n"
                       "print(json.load(open('runs/alg/doc.json'))['title'])\nPY", False),
            ("alg-p2", "python3 -m json.tool <<'EOF'\n{\"c\": \"lark-cli docs +update\"}\nEOF", False),
        ])

    def test_code_that_builds_the_command(self):
        L = self.L
        self.check([
            ("alg-p2", "python3 runs/alg/batch.py", True), ("alg-p2", "python3 runs/alg/batch2.py", True),
            ("alg-p2", "python3 runs/alg/batch3.py", True),
            ("alg-p2", "python3 - <<'PY'\nimport subprocess\nL = '%s'\nfor i in (2106, 2107):\n"
                       "    subprocess.run(f'python3 -I {L} archive {i} --apply', shell=True)\nPY" % L, True),
            ("alg-p2", "python3 runs/alg/report.py", False),
        ])

    def test_cli_names_used_as_data(self):
        self.check([
            ("alg-p2", "python3 -c \"import json; print(json.load(open('feishu/ids.json'))['meegle']['project_key'])\"", False),
            ("alg-p2", "python3 - <<'PY'\nimport json\nids = json.load(open('feishu/ids.json'))\nprint(ids['meegle']['project_key'])\nPY", False),
            ("alg-p2", "python3 runs/alg/pk.py", False),
            ("alg-p2", "meegle workitem get --work-item-id 2106 --project-key \"$(python3 runs/alg/pk.py)\"", False),
            ("alg-p2", "python3 -c \"import json; print(json.load(open('feishu/ids.json')).get('meegle', {}))\"", False),
            ("alg-p2", "python3 -c \"import subprocess,sys; subprocess.run(['meegle'] + sys.argv[1:])\" workitem update --work-item-id 1", True),
            ("alg-p2", "python3 -c \"import json; print(len(json.load(open('runs/alg/meegle.json'))))\"", False),
            ("alg-p2", "node -e \"const d=require('./runs/alg/meegle.json'); console.log(d.length)\"", False),
            ("alg-p2", "python3 runs/alg/readdoc.py", False),
        ])

    def test_arguments_are_not_subcommands(self):
        L = self.L
        self.check([
            ("alg-p2", "python3 -I %s query open --json > /tmp/open.json && python3 - /tmp/open.json <<'PY'\n"
                       "import json, sys\nprint(len(json.load(open(sys.argv[1]))))\nPY" % L, False),
            ("alg-p2", "python3 -I %s query open --json > runs/alg/open.json && python3 \"$CLAUDE_PROJECT_DIR/runs/alg/stats.py\" runs/alg/open.json" % L, False),
            ("alg-p2", "python3 \"$(git rev-parse --show-toplevel)/runs/alg/stats.py\" runs/alg/ledger-open.json", False),
            ("alg-p2", "for f in %s; do python3 $f archive 2106 --apply; done" % L, True),
            ("alg-p2", "python3 runs/alg/summarize_ledger.py runs/alg/open.json", False),
            ("alg-p2", "python3 runs/alg/summarize_ledger.py --top 5 runs/alg/open.json", False),
            ("alg-p2", "cp %s /tmp/my_ledger.py && python3 /tmp/my_ledger.py archive 2106 --apply" % L, True),
        ])

    def test_shell_options_pipes_and_here_strings(self):
        L, W = self.L, self.LARK_W
        self.check([
            ("alg-p2", "bash runs/alg/r.sh -c configs/a.yaml", True), ("alg-p2", "bash -eo pipefail runs/alg/r.sh", True),
            ("alg-p2", "bash runs/alg/push.sh -c configs/a.yaml", True), ("alg-p2", "bash -euo pipefail runs/alg/push.sh", True),
            ("alg-p2", "bash -o pipefail -c 'python3 %s archive 1'" % L, True),
            ("alg-p2", "cat <<'EOF' | python3 -\nimport subprocess\nsubprocess.run(['python3', '-I', '%s', 'archive', '2106', '--apply'])\nEOF" % L, True),
            ("alg-p2", "cat <<'EOF' | python3 -\nimport subprocess\nsubprocess.run(%r.split())\nEOF" % W, True),
            ("alg-p2", "cat <<'EOF' | bash\npython3 -I %s archive 2106 --apply\nEOF" % L, True),
            ("alg-p2", "bash <<< \"python3 -I %s archive 2106 --apply\"" % L, True),
            ("alg-p2", "python3 - <<< \"import subprocess; subprocess.run(['python3', '%s', 'archive', '2106'])\"" % L, True),
            ("alg-p2", "python3 - <<< \"import subprocess; subprocess.run(%r.split())\"" % W, True),
            ("alg-p2", "grep ';' runs/alg/r.sh", False),
            ("alg-p2", "source .venv/bin/activate && python3 -I %s query open" % L, False),
        ])

    def test_find_runners_and_inline_flags(self):
        L, W = self.L, self.LARK_W
        self.check([
            ("alg-p2", "find runs/alg -name 'analyze_*.py' -exec python3 {} runs/alg/ledger-open.json \;", False),
            ("alg-p2", "find .claude/agent-org/bin \\( -name ledger.py \\) -execdir python3 -I {} archive 2106 --apply \;", True),
            ("alg-p2", "find runs/alg -name '*.py' -exec python3 {} runs/alg/data.json \;", False),
            ("alg-p2", "find runs/alg -name '*.py' -exec python3 {} --limit 5 \;", False),
            ("alg-p2", "find .claude -name 'ledger*' -exec python3 {} archive 1 \;", True),
            ("alg-p2", "conda run -n base --cwd . python %s archive 2106 --apply" % L, True),
            ("alg-p2", "uv --directory . run python %s archive 2106 --apply" % L, True),
            ("alg-p2", "poetry -C . run python %s archive 2106 --apply" % L, True),
            ("alg-p2", "uv --directory . run " + W, True), ("alg-p2", "conda run -n base --cwd . " + W, True),
            ("alg-p2", "perl -we 'system(\"python3\", \"%s\", \"archive\", \"2106\", \"--apply\")'" % L, True),
            ("alg-p2", "node -pe \"require('child_process').execFileSync('python3', ['%s', 'archive', '2106', '--apply']).toString()\"" % L, True),
            ("alg-p2", "node --eval=\"require('child_process').execFileSync('python3', ['%s', 'archive', '2106'])\"" % L, True),
            ("alg-p2", "node -r dotenv/config -e \"require('child_process').execFileSync('lark-cli', ['docs', '+update', '--doc', 'X', "
                       "'--command', 'overwrite', '--content', 'x', '--as', 'user'])\"", True),
            ("alg-p2", "ipython -c \"import subprocess; subprocess.run(['lark-cli', 'docs', '+update', '--doc', 'X', '--command', "
                       "'overwrite', '--content', 'x', '--as', 'user'])\"", True),
            ("alg-p2", "ruby -rtime -e \"system('lark-cli docs +update --doc X --command overwrite --content x --as user')\"", True),
        ])

    def test_scribe_tag_check_reads_the_heredoc_fed_to_lark(self):
        append = "lark-cli docs +update --doc https://x.feishu.cn/wiki/A --command append --doc-format markdown --content - --as user"
        self.check([
            ("pmo-scribe", "cat > /tmp/prev.md <<'EOF'\n[PMO+0] 上一条记录\nEOF\n%s <<'EOF'\n没有会话标签的记录\nEOF" % append, True),
            ("pmo-scribe", "cat > /tmp/prev.md <<'EOF'\n没有标签\nEOF\n%s <<'EOF'\n[PMO+0] 记录\nEOF" % append, False),
            ("pmo-scribe", "cat <<'EOF' | %s\n[PMO+0] 记录\nEOF" % append, False),
            ("pmo-scribe", "python3 -I %s register packages/drafts/alg/x.md --apply" % self.L, False),
        ])


class SixthRoundLedgerTest(ProjectCase):
    """Ledger and setup fixes from the fifth adversarial check."""

    DOC = [(("docs", "+create"), fixture("docs-create.json")), (("wiki", "+node-get"), fixture("wiki-node-get.json"))]

    def stub(self, mode, replies):
        return StubLedger(self.tmp, mode, replies)

    def charter(self, name, folder="pmo"):
        path = os.path.join(self.tmp, "packages", "drafts", folder, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 立项单：AutoResearch\n\n- 关联工作项：待建\n\n## 背景\n%s\n" % name)
        return path

    def exec_draft(self, name="exec.md", edit=None):
        return self.draft(6, name, edit=lambda t: (edit or (lambda x: x))(t.replace("关联工作项：#2106；来源工作项 #2100", "关联工作项：待建")))

    def test_receipt_names_never_take_over_another_draft(self):
        self.run_quiet(ledger.register, self.stub("apply", self.DOC), self.charter("20261101-立项单-AR.md"), existing=True)
        self.run_quiet(ledger.register, self.stub("apply", self.DOC), self.charter("20261102-立项单 AR.md"), existing=True)
        b = self.charter("20261102-立项单_AR.md")                  # the same name once sanitised
        lg = self.stub("apply", self.DOC)
        self.run_quiet(ledger.register, lg, b, existing=True)
        self.assertTrue([c for c in lg.calls if "+create" in c[0]])
        sent = sorted(os.listdir(os.path.join(self.tmp, "packages", "sent")))
        self.assertEqual(len([n for n in sent if n.startswith("9000000001")]), 3, sent)
        get = with_operator(["u_researcher"], work_item_id="2106")
        first = self.draft(7, "delivery.md")
        self.run_quiet(ledger.reply, self.stub("apply", [(("workitem", "get"), get)] + self.DOC), first)
        other = os.path.join(self.tmp, "packages", "drafts", "alg", "v2", "delivery.md")
        os.makedirs(os.path.dirname(other))
        shutil.copy(first, other)
        lg = self.stub("apply", [(("workitem", "get"), get)] + self.DOC)
        self.run_quiet(ledger.reply, lg, other)                     # its own document, not the first one's
        self.assertTrue([c for c in lg.calls if "+create" in c[0]])

    def test_uncertain_document_needs_doc(self):
        path = self.exec_draft()
        base = [(("user", "me"), {"user_key": "u_researcher"}), (("workitem", "get"), with_operator(["u_researcher"])),
                (("workitem", "create"), {"work_item_id": 7160000004})]
        with self.assertRaises(ledger.WriteMaybeDone):
            self.run_quiet(ledger.register, self.stub("apply", base + [(("docs", "+create"), ledger.WriteMaybeDone("timeout"))]),
                           path, "2200")
        with self.assertRaises(ledger.LedgerError) as e:
            self.run_quiet(ledger.register, self.stub("apply", base + self.DOC), path, resume=True)
        self.assertIn("--doc", str(e.exception))
        lg = self.stub("apply", base + self.DOC)
        self.run_quiet(ledger.register, lg, path, resume=True, doc="https://example.feishu.cn/wiki/wikFoundOnWeb01")
        self.assertFalse([c for c in lg.calls if "+create" in c[0]])
        update = next(c for c in lg.calls if c[0][:3] == ["meegle", "workitem", "update"])
        self.assertIn("wikFoundOnWeb01", " ".join(update[0]))
        reply_path = self.draft(7, "delivery.md")
        get = with_operator(["u_researcher"], work_item_id="2106")
        with self.assertRaises(ledger.LedgerError) as e:
            self.run_quiet(ledger.reply, self.stub("apply", [(("workitem", "get"), get), (("docs", "+create"), {"data": {}})]), reply_path)
        self.assertIn("--doc", str(e.exception))
        lg = self.stub("apply", [(("workitem", "get"), get)] + self.DOC)
        self.run_quiet(ledger.reply, lg, reply_path, "new")
        self.assertTrue([c for c in lg.calls if "+create" in c[0]])

    def test_half_registered_project_is_found_by_the_next_charter(self):
        with open(os.path.join(self.tmp, "feishu", "ids.json"), encoding="utf-8") as f:
            ids = json.load(f)
        ids["meegle"].pop("project_item_id")
        with open(os.path.join(self.tmp, "feishu", "ids.json"), "w", encoding="utf-8") as f:
            json.dump(ids, f)
        base = [(("user", "me"), {"user_key": "u_researcher"}), (("workitem", "get"), with_operator(["u_researcher"])),
                (("workitem", "create"), {"work_item_id": 7160000300})]
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.register, self.stub("apply", base + [(("docs", "+create"), ledger.LedgerError("down"))]),
                           self.charter("a.md"))
        with open(os.path.join(self.tmp, "feishu", "ids.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["meegle"]["project_item_id"], "7160000300")
        with self.assertRaises(ledger.LedgerError) as e:
            self.run_quiet(ledger.register, self.stub("apply", base + self.DOC), self.charter("b.md"))
        self.assertIn("已经有项目", str(e.exception))
        lg = self.stub("apply", base + self.DOC)
        self.run_quiet(ledger.register, lg, os.path.join(self.tmp, "packages", "drafts", "pmo", "b.md"), existing=True)
        self.assertFalse([c for c in lg.calls if c[0][:3] == ["meegle", "workitem", "create"]])

    def test_resume_keeps_set_values_and_writes_new_ones(self):
        path = self.exec_draft()
        claim = os.path.join(self.tmp, "runs", "alg", "claim.txt")
        os.makedirs(os.path.dirname(claim), exist_ok=True)
        with open(claim, "w", encoding="utf-8") as f:
            f.write("2 GPU\n")
        base = [(("user", "me"), {"user_key": "u_researcher"}), (("workitem", "get"), with_operator(["u_researcher"])),
                (("workitem", "create"), {"work_item_id": 7160000004})]
        cwd = os.getcwd()
        try:
            os.chdir(self.tmp)
            with self.assertRaises(ledger.LedgerError):
                self.run_quiet(ledger.register, self.stub("apply", base + [(("docs", "+create"), ledger.LedgerError("down"))]),
                               path, "2200", pairs=["resource_claim=@runs/alg/claim.txt"])
        finally:
            os.chdir(cwd)
        os.remove(claim)
        with open(os.path.join(self.tmp, "packages", "sent", "7160000004.json"), encoding="utf-8") as f:
            rec = json.load(f)
        self.assertEqual(rec["pairs"], ["resource_claim=2 GPU"])
        lg = self.stub("apply", base + self.DOC)
        self.run_quiet(ledger.register, lg, path, resume=True, pairs=["run_ids=r-17"])
        update = " ".join(next(c for c in lg.calls if c[0][:3] == ["meegle", "workitem", "update"])[0])
        self.assertIn("r-17", update)

    def test_query_counts_pages_and_odd_cells(self):
        def page(start, n, count=None, session="S1"):
            group = [{"moql_field_list": [{"key": "work_item_id", "value": {"long_value": start + i}}]} for i in range(n)]
            return {"data": {"1": group}, "session_id": session if count != "none" else None,
                    "list": None if count in (None, "none") else [{"count": count, "group_infos": [{"group_id": "1"}]}]}
        pages = [(("--session-id", json.dumps([{"group_id": "1", "page_num": k}])), page(50 * (k - 1), n)) for k, n in ((2, 50), (3, 20))]
        for count in ("120", 120.0, None):
            lg = self.stub("preview", pages + [(("--mql",), page(0, 50, count))])
            rows, _ = self.run_quiet(ledger.query, lg, "open")
            self.assertEqual(len(rows), 120, count)
        bad_page = [(("--session-id",), {"_text": "Error: session expired"}), (("--mql",), page(0, 50, 120))]
        with self.assertRaises(ledger.LedgerError) as e:
            self.run_quiet(ledger.query, self.stub("preview", bad_page), "open")
        self.assertIn("第 2 页", str(e.exception))
        for cell in (["待决定"], "待决定", [None]):
            odd = {"data": {"1": [{"moql_field_list": [{"key": "work_item_status", "value": {"key_label_value_list": cell}}]}]}}
            with self.assertRaises(ledger.LedgerError):
                ledger.query_rows(odd)

    def test_set_value_stops_on_anything_that_could_be_a_file(self):
        lg = ledger.Ledger(self.tmp, "offline")
        for missing in ("@可使用的结论原文", "@<可使用的结论原文文件>", "@runs/alg/重评 结果.md", "@usable"):
            with self.assertRaises(ledger.LedgerError, msg=missing):
                ledger.set_value(lg, missing)
        self.assertEqual(ledger.set_value(lg, "@研究员 还没批预算"), "@研究员 还没批预算")
        gbk = os.path.join(self.tmp, "gbk.txt")
        with open(gbk, "wb") as f:
            f.write("结论原文".encode("gbk"))
        with self.assertRaises(ledger.LedgerError):
            ledger.set_value(lg, "@" + gbk)

    def test_relation_must_match_the_sender_rank(self):
        path = self.exec_draft(edit=lambda t: t.replace("质量 +1（Quality-p1-02）", "质量 +2（Quality-p2-01）"))
        with self.assertRaises(ledger.LedgerError) as e:
            self.run_quiet(ledger.register, ledger.Ledger(self.tmp, "offline"), path, "2200")
        self.assertIn("职级", str(e.exception))

    def test_source_line_with_a_full_width_colon(self):
        path = self.exec_draft(edit=lambda t: t.replace("关联工作项：待建", "关联工作项：待建\n- 来源工作项：#7151870875"))
        d = ledger.Draft(self.spec, path)
        self.assertEqual(d.source, "7151870875")

    def test_check_separates_task_edits_and_lark_versions(self):
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(self.tmp, os.path.join(self.tmp, "spec", "task.json"), None)
            setup.apply_plan(self.tmp, plan, setup.actions_for(self.tmp, plan))
        risk = os.path.join(self.tmp, ".claude", "hooks", "lark-risk.json")
        with open(risk, encoding="utf-8") as f:
            table = json.load(f)
        table["lark_cli_version"] = "1.0.96"
        with open(risk, "w", encoding="utf-8") as f:
            f.write(setup.dump_json(table))
        mpath = os.path.join(self.tmp, ".claude", "agent-org", "manifest.json")
        with open(mpath, encoding="utf-8") as f:
            m = json.load(f)
        m["files"][".claude/hooks/lark-risk.json"] = setup.sha256(setup.read_text(risk))
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(m, f)
        code, out = self.run_quiet(setup.check, self.tmp)
        self.assertIn("lark-cli 风险表", out)
        self.assertNotIn("现在的插件", out)
        task = os.path.join(self.tmp, "spec", "task.json")
        with open(task, encoding="utf-8") as f:
            t = json.load(f)
        t["feishu"]["meegle_space"] = "ar1"
        with open(task, "w", encoding="utf-8") as f:
            json.dump(t, f)
        code, out = self.run_quiet(setup.check, self.tmp)
        self.assertIn("改过：spec/task.json", out)
        self.assertNotIn("现在的插件", out)

    def test_malformed_task_and_ids_are_explained(self):
        with open(TASK, encoding="utf-8") as f:
            base = json.load(f)
        for bad in (None, 5, dict(base, name=2026), dict(base, models="opus"), dict(base, editable_paths="repo")):
            path = os.path.join(self.tmp, "bad-task.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(bad, f)
            with self.assertRaises(setup.SetupError, msg=repr(bad)[:60]):
                setup.load_task(path)
        with open(os.path.join(self.tmp, "feishu", "ids.json"), "w", encoding="utf-8") as f:
            json.dump([], f)
        with self.assertRaises(setup.SetupError):
            setup.load_ids(self.tmp)

    def test_knowledge_base_items_are_per_project(self):
        ids, _ = fake_feishu(self.spec)
        md = setup.migration_md(self.spec, ids, None, None)
        self.assertIn("五份包模板", md[md.index("## 网页上还要做的事"):])
        self.assertNotIn("分享链接", md)
        self.assertNotIn("模板空间", md)

class SeventhRoundTest(ProjectCase):
    """Cases from the sixth check, which stopped half way: its repro scripts were replayed by hand."""

    L = ".claude/agent-org/bin/ledger.py"

    def check(self, cases):
        for role, cmd, deny in cases:
            code, out = self.guard(role, "Bash", {"command": cmd})
            denied = out.get("permissionDecision") == "deny" or code == 2
            self.assertEqual(denied, deny, "%s: %r → %s %s" % (role, cmd, code, out.get("permissionDecisionReason")))

    def test_hook_reads_ordinary_commands(self):
        tools = os.path.join(self.tmp, "tools")
        os.makedirs(tools)
        with open(os.path.join(tools, "hello.js"), "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env node\n/*\n * Copyright (c) 2026\n * Redistributions of source code must retain this notice.\n */\nconsole.log('hi')\n")
        os.chmod(os.path.join(tools, "hello.js"), 0o755)
        draft_writer = ("python3 - <<'PY'\nfrom pathlib import Path\nPath('packages/drafts/alg/req.md').write_text("
                        "'# 执行请求\\n请书记员运行 ledger.py register packages/drafts/alg/req.md\\n')\nPY")
        self.check([("alg-p2", c, False) for c in (
            'cat "runs/alg/notes [2024-11-01].md"', 'tail -n 100 "runs/alg/train [lr=3e-4].log"',
            '"$PY" -m pytest "tests/test_math.py::test_sub[5-3-2]"', "./tools/hello.js world",
            "python3 - <<'PY'\nrows = [1,2]\nprint(f\"{len(rows)} update(s) pending in ledger\")\nPY",
            "python3 - <<'PY'\nimport json\nd = json.load(open('/tmp/ledger-open.json'))\nstates = {'register': 0, 'update': 0}\nPY",
            "python3 -c \"import json; d=json.load(open('/tmp/ledger-open.json')); print([r for r in d if r['type']=='reply'])\"",
            "python3 - <<'PY'\n# summarise; the scribe runs ledger.py update afterwards\nimport json\nprint(1)\nPY",
            draft_writer,
        )])

    def test_hook_still_sees_writes_after_arithmetic_and_crlf(self):
        L = self.L
        self.check([
            ("alg-p2", "x=1; (( y = x << n ))\npython3 -I %s archive 2106 --apply" % L, True),
            ("alg-p2", "cat > /tmp/a.md <<'EOF'\r\nhello\r\nEOF\r\npython3 -I %s archive 2106 --apply\r\n" % L, True),
            ("alg-p2", "x=1; (( y = x << n ))\nlark-cli docs +update --doc https://x.feishu.cn/wiki/A --command overwrite --content x --as user", True),
        ])

    def test_scribe_multi_line_lark_command(self):
        cmd = ("lark-cli docs +update \\\n  --doc https://x.feishu.cn/wiki/A \\\n  --command append --doc-format markdown \\\n"
               "  --content - --as user <<'EOF'\n[PMO+0] 记录\nEOF")
        self.check([("pmo-scribe", cmd, False)])

    def exec_draft(self):
        return self.draft(6, "exec.md", edit=lambda t: t.replace("关联工作项：#2106；来源工作项 #2100", "关联工作项：待建"))

    BASE = [(("user", "me"), {"user_key": "u_researcher"}), (("workitem", "get"), with_operator(["u_researcher"])),
            (("workitem", "create"), {"work_item_id": 7160000004})]
    DOC = [(("docs", "+create"), fixture("docs-create.json")), (("wiki", "+node-get"), fixture("wiki-node-get.json"))]

    def test_resume_records_new_set_values(self):
        path = self.exec_draft()
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.register, StubLedger(self.tmp, "apply", self.BASE + [(("docs", "+create"), ledger.LedgerError("down"))]),
                           path, "2200")
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.register, StubLedger(self.tmp, "apply", self.BASE + [(("workitem", "update"), ledger.LedgerError("down"))]
                                                       + self.DOC), path, resume=True, pairs=["run_ids=r-17"])
        lg = StubLedger(self.tmp, "apply", self.BASE + self.DOC)
        self.run_quiet(ledger.register, lg, path, resume=True)
        update = " ".join(next(c for c in lg.calls if c[0][:3] == ["meegle", "workitem", "update"])[0])
        self.assertIn("r-17", update)

    def test_doc_must_be_a_document_link_and_implies_resume(self):
        path = self.exec_draft()
        with self.assertRaises(ledger.WriteMaybeDone):
            self.run_quiet(ledger.register, StubLedger(self.tmp, "apply", self.BASE + [(("docs", "+create"), ledger.WriteMaybeDone("t"))]),
                           path, "2200")
        for bad in ("https://example.feishu.cn/wiki/", "https://example.feishu.cn/drive/folder/fldX", "docxMainDoc0001"):
            with self.assertRaises(ledger.LedgerError) as e:
                self.run_quiet(ledger.register, StubLedger(self.tmp, "apply", self.BASE), path, doc=bad)
            self.assertIn("--doc", str(e.exception))
        lg = StubLedger(self.tmp, "apply", self.BASE)
        self.run_quiet(ledger.register, lg, path, doc="https://example.feishu.cn/wiki/wikFoundOnWeb01")   # no --resume needed
        self.assertFalse([c for c in lg.calls if c[0][:3] == ["meegle", "workitem", "create"]])

    def test_odd_receipt_files_are_skipped(self):
        sent = os.path.join(self.tmp, "packages", "sent")
        with open(os.path.join(sent, "reply-2106-delivery.md.json"), "w", encoding="utf-8") as f:
            json.dump(["x"], f)
        get = with_operator(["u_researcher"], work_item_id="2106")
        self.run_quiet(ledger.reply, StubLedger(self.tmp, "apply", [(("workitem", "get"), get)] + self.DOC), self.draft(7, "delivery.md"))
        self.assertTrue(os.path.exists(os.path.join(sent, "reply-2106-delivery.md-2.json")))

    def test_full_first_page_without_count_or_session_warns(self):
        group = [{"moql_field_list": [{"key": "work_item_id", "value": {"long_value": i}}]} for i in range(50)]
        lg = StubLedger(self.tmp, "preview", [(("--mql",), {"data": {"1": group}, "list": None, "session_id": None})])
        rows, out = self.run_quiet(ledger.query, lg, "open")
        self.assertEqual(len(rows), 50)
        self.assertIn("没翻完", out)


class ReviewFollowUpTest(ProjectCase):
    """Minor issues found when the Feishu read-back and node lookup were re-checked."""

    def ids(self):
        with open(os.path.join(self.tmp, "feishu", "ids.json"), encoding="utf-8") as f:
            return json.load(f)

    def nodes(self, *pairs):
        return {"ok": True, "data": {"nodes": [{"node_token": t, "title": title, "has_child": False} for t, title in pairs],
                                     "has_more": False}}

    def test_ensure_node_reuses_a_node_with_the_same_name(self):
        lg = StubLedger(self.tmp, "apply", [(("wiki", "+node-list"), self.nodes(("wikSame001", "反馈包"), ("wikX", "汇报文档")))])
        lg.nodes.pop("feedback")
        self.run_quiet(ledger.ensure_node, lg, "feedback")
        self.assertFalse([c for c in lg.calls if "+node-create" in c[0]])
        self.assertEqual(self.ids()["wiki"]["nodes"]["feedback"]["node_token"], "wikSame001")
        lg = StubLedger(self.tmp, "apply", [(("wiki", "+node-list"), self.nodes(("wikA", "产品"), ("wikB", "产品")))])
        lg.nodes.pop("archive/product")
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.ensure_node, lg, "archive/product")
        lg = StubLedger(self.tmp, "apply", [(("wiki", "+node-list"), {"_text": "error"})])
        lg.nodes.pop("archive/product")
        with self.assertRaises(ledger.LedgerError):
            self.run_quiet(ledger.ensure_node, lg, "archive/product")
        lg = StubLedger(self.tmp, "apply", [(("wiki", "+node-list"), self.nodes(("wikZ", "算法"))),
                                            (("wiki", "+node-create"), {"data": {"node": {"node_token": "wikNew0001"}}})])
        lg.nodes.pop("archive/product")
        self.run_quiet(ledger.ensure_node, lg, "archive/product")
        create = next(c for c in lg.calls if "+node-create" in c[0])
        self.assertIn("node_archive", " ".join(create[0]))
        self.assertEqual(self.ids()["wiki"]["nodes"]["archive/product"]["node_token"], "wikNew0001")

    def test_ensure_node_on_the_recorded_space_root(self):
        root = fixture("wiki-node-list-root.json")
        lg = StubLedger(self.tmp, "preview", [(("wiki", "+node-list"), root)])
        lg.nodes.pop("overview")
        _, out = self.run_quiet(ledger.ensure_node, lg, "overview")
        self.assertIn("wikFakeNodeRoot0001", out)
        self.assertFalse(lg.planned)
        for key, title in (("same_seq", "同序列需求包"), ("archive", "归档文档")):     # other names are not taken over
            lg = StubLedger(self.tmp, "preview", [(("wiki", "+node-list"), root)])
            lg.nodes.pop(key)
            self.run_quiet(ledger.ensure_node, lg, key)
            self.assertTrue(any("+node-create" in p and title in p for p in lg.planned), key)
        lg = StubLedger(self.tmp, "preview", [(("wiki", "+node-list"), root)])
        lg.nodes.pop("feedback")
        self.run_quiet(ledger.ensure_node, lg, "feedback")
        self.assertTrue(any("+node-create" in p and "反馈包" in p for p in lg.planned))

    def test_tenant_domain_is_kept_from_the_first_document_link(self):
        ids = self.ids()
        ids["wiki"].pop("tenant_domain", None)
        with open(os.path.join(self.tmp, "feishu", "ids.json"), "w", encoding="utf-8") as f:
            json.dump(ids, f)
        path = self.draft(6, "exec.md", edit=lambda t: t.replace("关联工作项：#2106；来源工作项 #2100", "关联工作项：待建"))
        base = [(("user", "me"), {"user_key": "u_researcher"}), (("workitem", "get"), with_operator(["u_researcher"])),
                (("workitem", "create"), {"work_item_id": 7160000004}), (("docs", "+create"), fixture("docs-create.json")),
                (("wiki", "+node-get"), fixture("wiki-node-get.json"))]
        self.run_quiet(ledger.register, StubLedger(self.tmp, "apply", base), path, "2200")
        self.assertEqual(self.ids()["wiki"].get("tenant_domain"), "example.feishu.cn")


class WriteTestFollowUpTest(ProjectCase):
    """Findings of the write tests on the template space."""

    def decision(self):
        d = fixture("workitem-get-all.json")
        d["work_item_attribute"].update({"work_item_type": {"key": "6ae688662a2427d9b87b6850", "name": "决策"},
                                         "work_item_status": {"key": "Not started", "name": "待决定"},
                                         "work_item_id": "7152108550"})
        d["work_item_fields"] = [f for f in d["work_item_fields"] if not f["key"].startswith("field_")]
        return d

    def test_query_keeps_only_the_name_of_a_person(self):
        rows = ledger.query_rows(fixture("workitem-query-user.json"))
        self.assertEqual(rows[0]["field_c9def8"], "研究员甲")
        self.assertNotIn("example.com", json.dumps(rows, ensure_ascii=False))
        self.assertIsNone(ledger.moql_value({"user_value": None}))
        self.assertEqual(ledger.moql_value({"user_value": [{"name_en": "A"}, {"user_key": "u_b"}]}), "A、u_b")

    def test_get_reads_the_decider_with_mql(self):
        lg = StubLedger(self.tmp, "preview", [(("workitem", "get"), self.decision()),
                                              (("workitem", "query"), fixture("workitem-query-user.json"))])
        out, _ = self.run_quiet(ledger.digest, lg, "7152108550")
        self.assertIn("decider（决定人）：研究员甲", out)
        self.assertNotIn("example.com", out)
        argv = next(c[0] for c in lg.calls if "query" in c[0])
        mql = argv[argv.index("--mql") + 1]
        self.assertIn("`field_c9def8`", mql)
        self.assertIn("WHERE `work_item_id` = 7152108550", mql)
        lg = StubLedger(self.tmp, "preview", [(("workitem", "get"), fixture("workitem-get-all.json"))])
        self.run_quiet(ledger.digest, lg, "7151870875")
        self.assertFalse([c for c in lg.calls if "query" in c[0]])

    def person_cell(self, value):
        d = fixture("workitem-query-user.json")
        for f in d["data"]["1"][0]["moql_field_list"]:
            if f["key"] == "field_c9def8":
                f["value"] = value
        return d

    def replies_with(self, cell):
        return [(("workitem", "get"), self.decision()),
                (("list-state-transitions",), fixture("transitions-decision.json")),
                (("workitem", "query"), self.person_cell(cell)),
                (("user", "me"), {"user_key": "u_researcher"})]

    def test_an_empty_person_cell_leaves_the_decider_empty(self):
        for cell in ({}, [], None, {"user_value": None}, {"user_value": {}}):
            replies = self.replies_with(cell)
            out, printed = self.run_quiet(ledger.digest, StubLedger(self.tmp, "preview", replies), "7152108550")
            self.assertNotIn("decider", out, cell)
            self.assertNotIn("{}", out + printed, cell)
            self.assertNotIn("读不懂", printed, cell)
            with self.assertRaises(ledger.LedgerError) as e:
                self.run_quiet(ledger.transition, StubLedger(self.tmp, "preview", replies), "7152108550", "已决定",
                               ["decision_content=x"])
            self.assertIn("决定人", str(e.exception), cell)

    def test_an_unknown_person_cell_is_reported_without_its_value(self):
        replies = self.replies_with({"user_value_list": [{"email": "r@example.com", "name_cn": "甲"}]})
        out, printed = self.run_quiet(ledger.digest, StubLedger(self.tmp, "preview", replies), "7152108550")
        self.assertNotIn("decider", out)
        self.assertIn("field_c9def8", printed)
        self.assertIn("读不懂", printed)
        self.assertNotIn("example.com", out + printed)
        buf = io.StringIO()
        with self.assertRaises(ledger.LedgerError) as e, contextlib.redirect_stdout(buf):
            ledger.transition(StubLedger(self.tmp, "preview", replies), "7152108550", "已决定", ["decision_content=x"])
        self.assertIn("决定人", str(e.exception))
        self.assertIn("读不懂", buf.getvalue())
        self.assertNotIn("example.com", buf.getvalue() + str(e.exception))

    def test_get_still_answers_when_the_person_query_fails(self):
        lg = StubLedger(self.tmp, "preview", [(("workitem", "get"), self.decision()),
                                              (("workitem", "query"), {"_text": "error"})])
        out, printed = self.run_quiet(ledger.digest, lg, "7152108550")
        self.assertTrue(out.startswith("#7152108550"))
        self.assertNotIn("decider", out)
        self.assertIn("人员字段读不出来", printed)

    def test_transition_counts_a_decider_already_filled_in(self):
        replies = [(("workitem", "get"), self.decision()),
                   (("list-state-transitions",), fixture("transitions-decision.json")),
                   (("workitem", "query"), fixture("workitem-query-user.json")),
                   (("user", "me"), {"user_key": "u_researcher"})]
        lg = StubLedger(self.tmp, "preview", replies)
        self.run_quiet(ledger.transition, lg, "7152108550", "已决定", ["decision_content=从搜索开始计时"])
        cmds = "\n".join(lg.planned)
        self.assertIn("--transition-id 40176773", cmds)
        self.assertNotIn("field_c9def8", cmds)
        lg = StubLedger(self.tmp, "preview", replies)
        self.run_quiet(ledger.transition, lg, "7152108550", "已决定", ["decision_content=x", "decider=研究员"])
        self.assertFalse([c for c in lg.calls if "query" in c[0]])

    def test_write_log_ids_have_no_trailing_newline(self):
        lg = ledger.Ledger(self.tmp, "apply")
        lg.log(["meegle", "workitem", "create"], 0, '{\n  "work_item_id": 7152343534\n}\n')
        with open(os.path.join(self.tmp, "feishu", "write-log.jsonl"), encoding="utf-8") as f:
            rec = json.loads(f.read().splitlines()[-1])
        self.assertEqual(rec["ids"], ["7152343534"])


class SpecAndRoutingTest(ProjectCase):
    """Task file with named fields, two spec documents written by interview, routing by 状态包."""

    LEGACY = {"name": "AR", "description": "旧版任务", "x": {"definition": "只能改模型结构。", "paths": ["repo/**"]},
              "f": {"definition": "留出集 AUC，3 个种子取平均。"}, "g": {"definition": "每个快照 2 卡 1 小时。", "budget_gpu_hours": 300},
              "h": {"definition": "评测脚本和测试集只读。", "paths": ["repo/eval/**"]}, "search": "MCGS 按平均分扩展。",
              "feishu": {"meegle_space": "AR", "wiki_space": "AR"}}

    def write_task(self, obj, name="task.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        return path

    def plan_for(self, task_path):
        target = tempfile.mkdtemp(prefix="agent-org-spec-")
        self.addCleanup(shutil.rmtree, target, True)
        with contextlib.redirect_stdout(io.StringIO()):
            return target, setup.build_plan(target, task_path, None)

    def read(self, rel):
        with open(os.path.join(self.tmp, rel), encoding="utf-8") as f:
            return f.read()

    def test_task_file_holds_only_what_scripts_use(self):
        task = setup.load_task(TASK)
        self.assertEqual(tuple(task), setup.TASK_KEYS)
        self.assertEqual(json.loads(self.read("spec/task.json")), task)
        claude = self.read("CLAUDE.md")
        for part in ("`spec/spec.md`", "`spec/eval-protocol.md`", "可写路径：`repo/**`", "只读路径：`repo/eval/**`、`data/eval/**`"):
            self.assertIn(part, claude)
        for letter in ("- x：", "- f：", "- g：", "- h：", "h 的路径"):
            self.assertNotIn(letter, claude)
        for name in os.listdir(os.path.join(self.tmp, ".claude", "agents")):
            self.assertNotIn("h 的路径", self.read(os.path.join(".claude", "agents", name)))
        policy = json.loads(self.read(".claude/hooks/policy.json"))
        self.assertEqual((policy["x_paths"], policy["h_paths"]), (["repo/**"], ["repo/eval/**", "data/eval/**"]))

    def test_spec_documents_have_the_named_sections(self):
        spec_md, protocol = self.read("spec/spec.md"), self.read("spec/eval-protocol.md")
        for heading in ("## 可改范围", "## 目标指标", "## 实验资源", "## 搜索方法", "## 签字"):
            self.assertIn(heading, spec_md)
        for heading in ("## 存放和版本", "## 划分和可见性", "## 接入方式", "## 打分", "## 证据标准", "## 签字"):
            self.assertIn(heading, protocol)
        self.assertIn("〔推测，待确认〕", spec_md)
        self.assertIn("agent-org:spec", protocol)
        self.assertIn("约束优化问题", spec_md)
        self.assertNotIn("x、f、g、h", spec_md + protocol)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "skills", "spec", "references", "questions.md")))

    def test_old_task_file_still_works_and_seeds_the_documents(self):
        target, plan = self.plan_for(self.write_task(self.LEGACY))
        spec_md = plan.files["spec/spec.md"][0]
        self.assertIn("只能改模型结构。（取自旧版任务文件）〔推测，待确认〕", spec_md)
        self.assertIn("MCGS 按平均分扩展。（取自旧版任务文件）〔推测，待确认〕", spec_md)
        self.assertIn("评测脚本和测试集只读。（取自旧版任务文件）〔推测，待确认〕", plan.files["spec/eval-protocol.md"][0])
        task = json.loads(plan.files["spec/task.json"][0])
        self.assertEqual((task["editable_paths"], task["protected_paths"]), (["repo/**"], ["repo/eval/**"]))
        self.assertFalse({"x", "f", "g", "h", "search"} & set(task))
        self.assertTrue(any("旧写法（x、f、g、h、search）" in n and "先把 x.paths、h.paths 改写成" in n for n in plan.notes))
        both = dict(self.LEGACY, editable_paths=["src/**"])
        with self.assertRaises(setup.SetupError):
            setup.load_task(self.write_task(both, "both.json"))

    def test_task_file_names_the_space(self):
        base = setup.load_task(TASK)
        target, plan = self.plan_for(self.write_task(dict(base, _comment="ok")))
        self.assertNotIn("_comment", plan.files["spec/task.json"][0])
        self.assertTrue(any("还没绑定飞书空间" in n for n in plan.notes))
        for bad in ({"eval_resources": []}, {"feishu": {"wiki_space": "w"}}, {"feishu": {"meegle_space": " "}},
                    {"protected_paths": ["/abs/eval"]}, {"editable_paths": "repo"}, {"metric": "auc"}):
            with self.assertRaises(setup.SetupError, msg=repr(bad)):
                setup.load_task(self.write_task(dict(base, **bad), "bad.json"))

    def test_a_space_built_by_hand_binds_by_name(self):
        _, fd = fake_feishu(self.spec)
        hand = {"types": {}}
        for i, t in enumerate(fd["types"].values()):
            fields = [dict(f, field_key="field_hand%d%03d" % (i, j)) if f["field_key"].startswith("field_") else f
                      for j, f in enumerate(t["fields"])]
            hand["types"]["tk_hand_%d" % i] = dict(t, fields=fields, api_name="custom_%d" % i)
        fresh = {"schema": "agent-org/ids-v1", "lark": {}, "meegle": {}, "wiki": {}}
        self.assertEqual(setup.bind(self.spec, fresh, hand, {}), [])
        lg = ledger.Ledger(self.tmp, "offline")
        lg.types, lg.field_keys, lg.fd = fresh["meegle"]["types"], fresh["meegle"]["field_keys"], hand
        self.assertTrue(lg.field("decision", "decider")["field_key"].startswith("field_hand"))
        self.assertIn("tk_hand_", ledger.mql_text(lg, "queue")[0])
        lg.simple_name = "sn"
        self.assertTrue(lg.item_url("decision", "123").endswith("/sn/custom_4/detail/123"))

    def test_ledger_waits_for_the_binding(self):
        path = os.path.join(self.tmp, "feishu", "ids.json")
        with open(path, encoding="utf-8") as f:
            ids = json.load(f)
        ids["meegle"].pop("bound_at")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ids, f)
        old = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp
        try:
            code, out = self.run_quiet(ledger.main, ["update", "2003", "--set", "run_ids=r-1", "--apply"])
            self.assertEqual(code, 1)
            self.assertIn("还没绑定飞书空间", out)
            code, _ = self.run_quiet(ledger.main, ["--offline", "update", "2003", "--set", "run_ids=r-1", "--type", "same_seq"])
            self.assertEqual(code, 0)
        finally:
            if old is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = old

    def test_ledger_stops_when_the_task_names_another_space(self):
        path = os.path.join(self.tmp, "feishu", "ids.json")
        with open(path, encoding="utf-8") as f:
            ids = json.load(f)
        ids["meegle"]["space_name"] = "旧空间"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ids, f)
        old = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp
        try:
            code, out = self.run_quiet(ledger.main, ["update", "2003", "--set", "run_ids=r-1", "--apply"])
            self.assertEqual(code, 1)
            self.assertIn("绑定的是“旧空间”", out)
            with self.assertRaises(setup.SetupError):          # the local dictionary belongs to the old space
                self.run_quiet(setup.feishu, self.tmp, [], False, False)
        finally:
            if old is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = old

    def test_old_spec_documents_are_kept_and_flagged(self):
        old = "# AR 规格（草稿）\n\n## x：可以改的代码\n\n只能改模型结构。\n"
        with open(os.path.join(self.tmp, "spec", "spec.md"), "w", encoding="utf-8") as f:
            f.write(old)
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(self.tmp, os.path.join(self.tmp, "spec", "task.json"), None)
        self.assertTrue(any("spec/spec.md 还是改名前的旧模板" in n for n in plan.notes))
        acts = {rel: act for act, rel, _ in setup.actions_for(self.tmp, plan)}
        self.assertEqual(acts["spec/spec.md"], "kept")

    def test_hook_messages_use_the_names(self):
        _, out = self.guard("alg-p0", "Write", {"file_path": "repo/eval/score.py", "content": "x"})
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertIn("评测协议", out.get("permissionDecisionReason", ""))
        _, out = self.guard("alg-p0", "Write", {"file_path": "repo/model.py", "content": "x"})
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertIn("可改范围", out.get("permissionDecisionReason", ""))
        _, out = self.guard("rnd-p0", "Write", {"file_path": "repo/model.py", "content": "x"}, agent_id="a2")
        self.assertNotEqual(out.get("permissionDecision"), "deny")

    def test_router_reads_the_status_block(self):
        _, out = self.guard("pmo-router", "Bash", {"command": "python3 -I .claude/agent-org/bin/ledger.py status 7151870875"})
        self.assertNotEqual(out.get("permissionDecision"), "deny")
        section = {"data": {"document": {"content": "<h2>状态包</h2><h3>简介</h3><p>访谈提纲</p>"}}}
        lg = StubLedger(self.tmp, "preview", [(("workitem", "get"), fixture("workitem-get-all.json")),
                                              (("--scope", "outline"), fixture("docs-fetch-outline.json")),
                                              (("--scope", "section"), section)])
        _, printed = self.run_quiet(ledger.status, lg, "7151870875")
        self.assertIn("访谈提纲", printed)
        argv = next(c[0] for c in lg.calls if "section" in c[0])
        self.assertEqual(argv[argv.index("--start-block-id") + 1], "doxcnnyQfTdXSmSuMyN3ajvOmgh")
        self.assertIn("status", ledger.READ_COMMANDS)
        self.assertNotIn("index", ledger.READ_COMMANDS)

    def test_new_status_block_starts_with_the_cognitive_layer(self):
        body = "## 认知层\n\n这个阶段整理访谈，涉及 Product-p1-01 和需求文档 v1。\n\n## 目标\n\n写提纲。\n"
        out = ledger.with_status_sections(body)
        self.assertIn("### 简介\n\n这个阶段整理访谈，涉及 Product-p1-01 和需求文档 v1。\n\n### 当前状态", out)
        self.assertLess(out.index("## 状态包"), out.index("## 交接记录"))
        self.assertIn("### 简介\n\n### 当前状态", ledger.with_status_sections("## 目标\n\n写提纲。\n"))

    def test_plugin_cites_nothing_outside_itself(self):
        """Nothing in the repository, docs included, points to material outside the plugin."""
        forbidden = re.compile("|".join([
            "/Users/", "llm-?[Ww]iki", "WikiWorkspace", "ResearchWorkspace", "协作" + "记录", "协作" + "请求",
            "讨论" + "记录", "scratch" + "pad", "《", "文" + "章", "local_[0-9a-f]{8}", "Anthrop" + "ic", "Open" + "AI",
            "ME" + "TR ", "Cogni" + "tion", "Chro" + "ma ", "SWE-" + "bench", "Simple" + "QA", "Hua" + "ng 等人",
            "τ-" + "bench", "评审" + "会话"]))
        work_item = re.compile("#?715[0-9]{7}")
        hits = []
        for base, dirs, files in os.walk(REPO):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for name in files:
                path = os.path.join(base, name)
                if os.path.samefile(path, __file__) or not name.endswith((".md", ".json", ".tmpl", ".py", ".txt")):
                    continue
                docs = name.endswith((".md", ".tmpl")) and os.sep + "tests" + os.sep not in path
                with open(path, encoding="utf-8", errors="ignore") as f:
                    for no, line in enumerate(f, 1):
                        if forbidden.search(line) or docs and work_item.search(line):
                            hits.append("%s:%d" % (os.path.relpath(path, REPO), no))
        self.assertEqual(hits, [])

    def test_plugin_names_no_platform_or_test_space(self):
        """The plugin body names no platform outside its dependencies and no key or name of the space
        it was tested on; tests/ keeps the recorded outputs."""
        with open(os.path.join(ROOT, "tests", "fixtures", "feishu", "space-keys.json"), encoding="utf-8") as f:
            space = json.load(f)
        keys = [space["project_key"], space["simple_name"]] + [t["type_key"] for t in space["types"].values()]
        keys += [x["field_key"] for fields in space["fields"].values() for x in fields.values()]
        words = ["Coze" + "Loop", "coze" + "loop", "eval_" + "resources", "Auto" + "Research", "MC" + "GS", "贝索" + "斯",
                 "Claude in " + "Chrome", "前" + "文", "模板" + "空间", "分享" + "链接"]
        pattern = re.compile("|".join(re.escape(w) for w in words + sorted(set(keys))))
        hits = []
        for base, dirs, files in os.walk(REPO):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "tests")]
            for name in files:
                if not name.endswith((".md", ".json", ".tmpl", ".py", ".txt")):
                    continue
                path = os.path.join(base, name)
                with open(path, encoding="utf-8", errors="ignore") as f:
                    for no, line in enumerate(f, 1):
                        m = pattern.search(line)
                        if m:
                            hits.append("%s:%d %s" % (os.path.relpath(path, REPO), no, m.group(0)))
        self.assertEqual(hits, [])

    def test_packages_no_longer_carry_the_index(self):
        skills = os.path.join(ROOT, "skills")
        for rel in ("packages/assets/request.md", "packages/assets/delivery.md", "packages/assets/review.md",
                    "packages/assets/verdict.md", "packages/assets/decision.md", "packages/references/examples.md",
                    "packages/references/principles.md", "packages/SKILL.md", "entry/SKILL.md",
                    "entry/references/routing.md", "ledger/SKILL.md"):
            with open(os.path.join(skills, rel), encoding="utf-8") as f:
                self.assertNotIn("人、事、物索引", f.read(), rel)
        self.assertIn("状态包", orgspec.OrgSpec().routing_table())
        for rel in ("scaffold/agents/router.md.tmpl", "skills/entry/references/routing.md", "skills/packages/assets/request.md",
                    "skills/packages/assets/delivery.md", "skills/packages/assets/verdict.md",
                    "skills/packages/references/principles.md", "skills/packages/references/examples.md"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                self.assertNotIn("索引", f.read(), rel)

    def test_settings_allow_the_status_read(self):
        allow = json.loads(self.read(".claude/settings.json"))["permissions"]["allow"]
        self.assertIn("Bash(python3 -I .claude/agent-org/bin/ledger.py status *)", allow)
        self.assertFalse([r for r in allow if "ledger.py index" in r])

    def test_drafters_see_their_documents(self):
        alg, quality, rnd = (self.read(".claude/agents/%s.md" % n) for n in ("alg-p2", "quality-p2", "rnd-p0"))
        self.assertIn("以及你起草的 `spec/spec.md`", alg)
        self.assertIn("你负责起草实验规格 `spec/spec.md`：用技能 `agent-org:spec`", alg)
        self.assertIn("你负责起草评测协议 `spec/eval-protocol.md`", quality)
        self.assertIn("只读路径（`repo/eval/**`、`data/eval/**`）改动要研究员确认", quality)
        self.assertIn("只读路径（`repo/eval/**`、`data/eval/**`）不能改", rnd)
        self.assertNotIn("spec/spec.md", rnd)

    def test_spec_without_the_algorithm_sequence(self):
        target = tempfile.mkdtemp(prefix="agent-org-spec-")
        self.addCleanup(shutil.rmtree, target, True)
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(target, TASK, "pmo,quality,rnd,product")
        spec_md = plan.files["spec/spec.md"][0]
        self.assertIn("没有启用算法序列，由研究员本人写", spec_md)
        self.assertNotIn("研究员和研究员", spec_md)

    def test_review_package_summary_comes_from_the_conclusion(self):
        body = "- 包类型：送审包\n\n## 待审结论\n\n离线 AUC 提升 0.004（规格 v2，评测集 v1）。\n\n## 证据\n\n运行 r-801。\n"
        self.assertIn("### 简介\n\n离线 AUC 提升 0.004（规格 v2，评测集 v1）。\n\n### 当前状态", ledger.with_status_sections(body))

    def test_paths_are_normalised_and_gaps_reported(self):
        base = setup.load_task(TASK)
        task = setup.load_task(self.write_task(dict(base, editable_paths=["./repo/"], protected_paths=[]), "paths.json"))
        self.assertEqual((task["editable_paths"], task["protected_paths"]), (["repo/**"], []))
        target, plan = self.plan_for(self.write_task(dict(base, protected_paths=[]), "noprot.json"))
        self.assertTrue(any("没有写 protected_paths" in n for n in plan.notes))
        os.makedirs(os.path.join(target, "data", "eval"))
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(target, self.write_task(dict(base, protected_paths=["data/eval"]), "dir.json"), None)
        self.assertTrue(any("写成 data/eval/** 才能管到" in n for n in plan.notes))
        for bad in (["./"], ["./../x"]):
            with self.assertRaises(setup.SetupError, msg=repr(bad)):
                setup.load_task(self.write_task(dict(base, protected_paths=bad), "bad.json"))

    def test_spec_document_that_is_not_utf8(self):
        with open(os.path.join(self.tmp, "spec", "spec.md"), "wb") as f:
            f.write("# AR 实验规格\n\n## x：可以改的代码\n".encode("gbk"))
        with contextlib.redirect_stdout(io.StringIO()):
            plan = setup.build_plan(self.tmp, os.path.join(self.tmp, "spec", "task.json"), None)
        self.assertTrue(any("UTF-8" in n for n in plan.notes))
        code, out = self.run_quiet(setup.check, self.tmp)
        self.assertNotIn("Traceback", out)

    def test_path_errors_quote_the_entry_as_written(self):
        base = setup.load_task(TASK)
        for written in ("./", ".//repo", "/"):
            with self.assertRaises(setup.SetupError) as e:
                setup.load_task(self.write_task(dict(base, editable_paths=[written]), "bad.json"))
            self.assertTrue(str(e.exception).endswith("：" + written), str(e.exception))


if __name__ == "__main__":
    unittest.main(verbosity=1)
