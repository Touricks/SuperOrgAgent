"""Codex project renderer. Business documents and rules come from setup/orgspec.

This module never creates chats, changes hook trust, or writes remote services.
"""
import hashlib
import copy
import json
import os
from pathlib import Path
import shlex

VERSION = "0.5.1-codex.4"
PRIORITY_ROLES = ("pmo-p2", "pmo-router", "product-p2", "alg-p2", "rnd-p2",
             "infra-p2", "quality-p2", "liaison-p2")
EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop", "Interrupt", "SessionEnd")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def neutral(text):
    return (text.replace(".claude/agent-org/", ".agent-org/")
            .replace(".claude/agents/", ".agent-org/roles/")
            .replace(".claude/agent-memory/", ".agent-org/memory/")
            .replace("CLAUDE.md", "AGENTS.md"))


def codex_spec(original):
    """Adapt explicit policy fields; business schemas remain shared with Claude."""
    spec = copy.deepcopy(original)
    pmo = next(s for s in spec.rules["sequences"] if s["id"] == "pmo")
    pmo["notes"] = ("项目管理 +2 协调研究员的决定，+1 安排登记与分发，+0 默认承接台账维护。"
                    "明确委派其他会话代办时沿用原任务和回执。当前写入规则见项目 AGENTS.md；会话绑定用于路由与追溯。")
    spec.rules["limits_note"] = "会话池上限约束角色实例数量；写入任务的职责和委派按项目 AGENTS.md 执行。"
    decisions = spec.rules["plugin_decisions"]
    for i, item in enumerate(decisions):
        if isinstance(item, str) and "工作项、字段、状态和文档仍只由书记员写" in item:
            decisions[i] = "PMO 默认承接登记，明确委派可代办；工作项通过台账维护，独立正文按项目规范发布。"
        elif isinstance(item, dict) and "书记员的写权只认" in item.get("text", ""):
            item["text"] = ("Codex 会话池按原生会话 ID 路由和追溯。职责与委派见项目 AGENTS.md；"
                            "同一登记请求只由一名执行者处理，台账写锁与回执保持有效。")
    return spec


def adapt(base, original, target, spec, task, policy, seqs):
    spec = codex_spec(spec)
    plan = base.Plan()
    previous = read_manifest(target)
    conflicts = []
    plan.notes = [n for n in original.notes if "会话池模式" not in n]
    plan.notes.append("Codex 会话由原生会话工具创建并保留在项目分组；创建、绑定、项目归属和 hook 运行分别验收。")
    plan.notes.append("在 Codex 的 Settings > Hooks 审阅项目 hooks；脚本不会代替用户设置信任。")

    def put(rel, text, executable=False, keep=False):
        plan.files[rel] = (text, executable, keep)

    for rel, (content, executable, keep) in original.files.items():
        if not rel.startswith(".claude/") and rel not in ("CLAUDE.md", "org/grants.md", "inbox/README.md"):
            put(rel, neutral(content), executable, keep)
        elif rel.startswith((".claude/agent-org/orgspec/", ".claude/agent-org/templates/")):
            put(neutral(rel), neutral(content), executable, keep)
    policy = json.loads(neutral(dumps(policy)))
    policy.update(runtime="codex", channel="codex-desktop", owned_paths={
        ".agent-org/**": [], ".codex/**": [], "spec/task.json": []})
    for role in [*policy["agents"].values(), policy["default_agent"]]:
        for field in ("ledger_write", "lark_write", "meegle_write"):
            role.pop(field, None)
    put(".agent-org/policy.json", dumps(policy))
    put(".agent-org/orgspec/rules.json", dumps(spec.rules))
    put(".agent-org/operations/publishing.md", base.read_text(os.path.join(base.HERE,
        "../skills-codex/ledger/references/publishing.md")))
    put("org/org.md", neutral(base.org_md(spec, seqs)))
    put("org/decisions.md", neutral(base.render(base.scaffold("org/decisions.md.tmpl"),
        {"deviations": base.deviations_text(spec, "pool")})), keep=True)
    put(".agent-org/lark-risk.json", original.files[".claude/hooks/lark-risk.json"][0])
    for name in ("orgspec.py", "ledger.py", "route.py", "project_paths.py", "codex_runtime.py", "codex_hook.py", "pool.py", "codex_upgrade.py"):
        put(".agent-org/bin/" + name, base.read_text(os.path.join(base.HERE, name)), True)
    # Reuse the mature shell parser and pure path/ledger checks; never run the Claude hook entrypoint.
    for name in ("guard.py", "feishu_cmd.py", "instances.py"):
        put(".agent-org/bin/" + name, base.scaffold("hooks/" + name))

    legacy = base.pool_plan(spec, task, policy, seqs)
    slots = []
    for top in legacy["top"]:
        for item in [top] + top["children"]:
            role = item["role"]
            slots.append({"name": item["name"], "role": role,
                          "title": "[%s] %s" % (task["name"], item["name"]),
                          "model": item["model"], "pin": False,
                          "priority": PRIORITY_ROLES.index(role) + 1 if role in PRIORITY_ROLES else None})
    # PMO routing and the default registration slot remain available.
    if not {"pmo-router", "pmo-scribe"}.issubset({s["role"] for s in slots}):
        raise base.SetupError("Codex 会话池必须包含 PMO +1 和书记员，pool.later 不能包含 pmo")
    slots.sort(key=lambda s: s["priority"] or len(PRIORITY_ROLES) + 1)
    state_path = Path(target) / ".agent-org/state/pool.json"
    if state_path.is_file():
        state = json.loads(state_path.read_text())
        removed = (set(state.get("bindings", {})) | set(state.get("pending", {}))) - {s["name"] for s in slots}
        if removed:
            raise base.SetupError("这些池位仍有绑定或创建预约，不能直接从计划移除：" + "、".join(sorted(removed)))
    put(".agent-org/pool-plan.json", dumps({"version": 1, "runtime": "codex", "project": task["name"],
        "total": len(slots), "slots": slots, "sidebar_mode": "project",
        "priority_order": [s["name"] for s in slots if s["priority"]]}))
    for role, pol in policy["agents"].items():
        seq = spec.sequence(pol["sequence"])
        row = seq["ranks"][pol["rank"]]
        text = "# %s · %s +%d\n\n" % (task["name"], pol["sequence_cn"], pol["rank"])
        text += "角色：`%s`。实际身份由 `.agent-org/state/pool.json` 的会话 ID 绑定决定；标题不授予角色。\n\n" % role
        text += "已给定：%s\n\n产出：\n%s\n\n何时上报：%s\n\n" % (
            spec.render(row["given"]), base.bullets(spec.render(row["outputs"])), spec.render(row["escalate"]))
        text += "组织分工和五类包见 `org/org.md`。派发范围：%s。跨序列交接经过 PMO 入口。\n\n" % base.code_list(pol["spawn"])
        text += "默认职责与实际任务委派分开：正式登记默认交书记员，明确委派的代办按项目 AGENTS.md 执行，不配置逐会话写权。\n\n"
        text += "先读任务包、飞书工作项和主文档状态包；草稿放在 `packages/drafts/%s/`。\n\n" % pol["sequence"]
        text += "会话工具和恢复步骤见技能 `agent-org:entry`。先查池，再登记消息，再调用原生发送工具；只有用户授权的会话通信才能发送。\n\n"
        if pol["rank"] == 2:
            text += "研究员直接与你讨论时，用 codex_runtime.py focus open 登记占用，讨论结束后 focus close。谈定的变化写决策包，经书记员登记后生效。\n\n"
        if role == "pmo-p2":
            text += spec.routing_table() + "\n\n" + spec.handoff_rules() + "\n\n" + spec.signoff_items() + "\n"
        elif role == "pmo-router":
            text += spec.routing_table() + "\n\n汇总决策队列，安排登记与分发，交回入口。\n"
        elif role == "pmo-scribe":
            text += base.scaffold("codex/scribe.md.tmpl") + "\n"
        else:
            text += "交付先经过独立质量验收，再向上级交回；需签字的决定提交入口。交付包和验收包挂到原工作项，不另建同类任务。\n"
        put(".agent-org/roles/%s.md" % role, text)
    put("org/grants.md", "# 角色与写入职责\n\n")
    put("org/grants.md", plan.files["org/grants.md"][0] +
        "角色定义在 `.agent-org/roles/`，当前职责与委派规则在项目 `AGENTS.md`。默认书记员承接登记，明确委派可由其他主会话代办；绑定用于路由和审计。`policy.json` 保存角色元数据和受保护路径，不是逐会话写入白名单。工作项用 ledger，独立正文按文档发布流程执行。历史决定保留追溯，旧写权条款由当前 AGENTS 规则替代。模型默认继承 Codex 用户设置。\n")
    put("AGENTS.md", base.scaffold("codex/AGENTS.md.tmpl"))
    put("org/会话入口.md", "# 项目会话入口\n\n所有角色保留在本项目分组。以下是查找顺序，客户端暂不提供项目内固定排序接口；不移动到全局置顶。\n\n" +
        "\n".join("%d. %s（%s）" % (s["priority"], s["name"], s["title"]) for s in slots if s["priority"]) +
        "\n\n运行 `python3 -I .agent-org/bin/codex_runtime.py plan` 查看实际 thread_id、host_id 和未完成创建。\n")
    put("inbox/README.md", "# 消息与恢复\n\nCodex 的消息记录在 `.agent-org/state/pool.json`。正文放任务包，消息只存包路径、工作项和摘要。调用成功与接收回执分开记录；结果未知时先查会话，不能盲目重发。\n")
    command = "python3 -I -B %s" % shlex.quote(os.path.join(os.path.abspath(target), ".agent-org/bin/codex_hook.py"))
    hooks = {"description": "agent-org Codex 角色、消息回执和写入检查", "hooks": {}}
    for event in EVENTS:
        hooks["hooks"][event] = [{"hooks": [{"type": "command", "command": command,
                                          "timeout": 3 if event in ("Interrupt", "SessionEnd") else 10}]}]
    generated_hooks = hooks["hooks"]
    existing_hooks = Path(target) / ".codex/hooks.json"
    if existing_hooks.is_file():
        current = json.loads(existing_hooks.read_text())
        old_groups = previous.get("managed_hooks", {})
        for event, groups in generated_hooks.items():
            existing = current.setdefault("hooks", {}).get(event, [])
            for group in existing:
                ours = any("/.agent-org/bin/codex_hook.py" in h.get("command", "") for h in group.get("hooks", []))
                if ours and group not in old_groups.get(event, []) and group not in groups:
                    conflicts.append(".codex/hooks.json")
            current["hooks"][event] = [g for g in existing if g not in old_groups.get(event, []) and g not in groups] + groups
        hooks = current
    put(".codex/hooks.json", dumps(hooks))
    agents_path = Path(target) / "AGENTS.md"
    begin, end = "<!-- agent-org:begin -->", "<!-- agent-org:end -->"
    block = begin + "\n" + plan.files["AGENTS.md"][0] + end + "\n"
    existing = agents_path.read_text() if agents_path.is_file() else ""
    if begin in existing or end in existing:
        if existing.count(begin) != 1 or existing.count(end) != 1 or existing.index(begin) > existing.index(end):
            raise base.SetupError("AGENTS.md 的 agent-org 标记不完整，请先核对")
        before, rest = existing.split(begin, 1)
        body, after = rest.split(end, 1)
        current_block = begin + body + end
        if current_block not in (previous.get("managed_agents", "").rstrip("\n"), block.rstrip("\n")):
            conflicts.append("AGENTS.md")
        put("AGENTS.md", before + block.rstrip("\n") + after)
    else:
        put("AGENTS.md", existing.rstrip() + ("\n\n" if existing else "") + block)
    plan.manifest = dict(original.manifest, runtime="codex", plugin_version=VERSION, upstream_version=base.plugin_version())
    plan.manifest["managed_hooks"] = generated_hooks
    plan.manifest["managed_agents"] = block
    plan.manifest["setup_source"] = {"path": os.path.join(base.HERE, "setup.py"),
                                     "sha256": digest(base.read_text(os.path.join(base.HERE, "setup.py")))}
    plan.manifest["conflicts"] = sorted(set(conflicts))
    plan.manifest["files"] = {p: digest(c) for p, (c, _, _) in plan.files.items()}
    return plan


def read_manifest(target):
    p = Path(target) / ".agent-org/manifest.json"
    return json.loads(p.read_text()) if p.exists() else {}


def actions_for(target, plan):
    before = read_manifest(target).get("files", {})
    acts = []
    for rel, (content, _, keep) in sorted(plan.files.items()):
        root = Path(target).resolve()
        path = root / rel
        # Never follow a user-created symlink when installing generated code/config.
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root and root in p.parents):
            raise ValueError("初始化路径含符号链接，请先核对：" + str(path))
        old = path.read_text() if path.is_file() else None
        if path.exists() and not path.is_file():
            raise ValueError("目标不是普通文件：" + str(path))
        merged = rel in ("AGENTS.md", ".codex/hooks.json")
        kind = "new" if rel in plan.manifest.get("conflicts", []) else "create" if old is None else "same" if old == content else "kept" if keep else (
            "update" if merged else
            "update" if before.get(rel) == digest(old) else "new")
        acts.append((kind, rel, old))
    return acts


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def apply_plan(target, plan, acts):
    # A policy/script version is installed only as a complete set. Local edits stay intact.
    conflicts = [rel for kind, rel, _ in acts if kind == "new"]
    if conflicts:
        for rel in conflicts:
            atomic_text(Path(target) / (rel + ".new"), plan.files[rel][0])
        return False
    import codex_upgrade
    return codex_upgrade.install(target, plan, acts, actions_for)


def execute(target, plan, acts, apply):
    print("Codex %s（上游 %s），%s" % (VERSION, plan.manifest["upstream_version"], target))
    for kind, rel, _ in acts:
        if kind != "same":
            print("%s  %s" % (kind, rel))
    for note in plan.notes:
        print(note)
    if apply:
        if not apply_plan(target, plan, acts):
            print("有本地修改：只写了冲突文件的 .new；其余文件及版本记录未变。合并后重跑。")
            return 2
        print("本地初始化完成；运行 setup.py pool 查看会话清单。")
    return 0


def check(target, base=None):
    import codex_runtime
    if base is None:
        import setup as base
    result = codex_runtime.Pool(target).check()
    manifest = read_manifest(target)
    result["issues"] += base.feishu_gaps(target, manifest.get("sequences"))
    ids = base.load_ids(target)
    changed = base.ledger.other_space(base.load_json_file(os.path.join(target, "spec/task.json")) or {}, ids.get("meegle") or {})
    if changed:
        result["issues"].append("任务空间与绑定空间不一致，请重跑 feishu 预览")
    if manifest.get("plugin_version") != VERSION:
        result["issues"].append("项目初始化版本不同于当前插件，请重跑 init")
    result["ready"] = not result["issues"]
    print(dumps(result))
    return 0 if result["ready"] else 1
