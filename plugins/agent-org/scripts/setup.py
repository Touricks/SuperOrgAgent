#!/usr/bin/env python3
"""Initialise a project directory for the agent organisation (the plugin's init step).

    python3 -I scripts/setup.py precheck
    python3 -I scripts/setup.py init --target DIR --task task.json [--sequences product,alg,...] [--apply]
    python3 -I scripts/setup.py feishu --target DIR [--map ID=NAME ...] [--live] [--apply]
    python3 -I scripts/setup.py template [--space NAME] [--target DIR] [--live]
    python3 -I scripts/setup.py check --target DIR

init renders agents, project settings (permissions and hooks), CLAUDE.md, org/,
packages/, inbox/, spec/ and a copy of the plugin's scripts and rules into
.claude/agent-org/. Without --apply it only previews. Existing files are never
overwritten: a different version goes next to them as <file>.new. Running it
twice with the same inputs writes nothing.

feishu binds the rules' types and fields to the space spec/task.json names: by
--map, else the key bound last time, else the same name. It writes the keys to
feishu/ids.json and a build-and-check list to feishu/migration.md. It only reads
Feishu; the ledger refuses to touch Feishu until a binding has been applied. When
no type matches, it points to copying the space from the template, and --apply
records the space without a binding time.

template checks the space that project spaces are copied from (feishu.template_space
in the task file, else std_template): its types, states, roles, fields and options
must carry the rules' own names, so that a copy binds without --map, and it must
hold no work items, since it is only a copy source. It always reads Feishu (--live
is accepted, as for feishu) and writes nothing.
Python 3.9 standard library only.
"""
import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import ledger  # noqa: E402

PLUGIN_ROOT = orgspec.PLUGIN_ROOT
SCAFFOLD = os.path.join(PLUGIN_ROOT, "scaffold")
TEMPLATES = os.path.join(PLUGIN_ROOT, "skills", "packages", "assets")
MIN_VERSIONS = {"lark-cli": "1.0.96", "meegle": "1.0.23", "claude": "2.1.217"}
DEFAULT_TEMPLATE_SPACE = "std_template"
DEFAULT_MODELS = {"p2": "opus", "p1": "sonnet", "p0": "sonnet", "pmo-p2": "opus",
                  "pmo-router": "sonnet", "pmo-scribe": "haiku", "quality-p1": "opus"}
REQUIRED_SEQUENCES = ("pmo", "quality")
BIN_SCRIPTS = ("orgspec.py", "ledger.py", "route.py", "export_field_dictionary.py")
HOOK_SCRIPTS = ("guard.py", "audit.py", "instances.py", "inbox.py", "feishu_cmd.py")

SCRIBE_LARK = ["docs +create", "docs +update", "wiki +node-create", "wiki +move",
               "drive +add-comment", "drive +add-reply", "drive +resolve-comment", "drive +upload"]
SCRIBE_MEEGLE = ["workitem create", "workitem update", "workflow transition-state", "comment add",
                 "view create-fixed", "view update-fixed", "attachment +upload", "attachment prepare-upload"]
READ_ALLOW = [
    "Bash(lark-cli docs +fetch *)", "Bash(lark-cli docs +search *)", "Bash(lark-cli wiki +node-list *)",
    "Bash(lark-cli wiki +node-get *)", "Bash(lark-cli wiki +space-list *)",
    "Bash(lark-cli drive +list-comments *)", "Bash(lark-cli im +chat-messages-list *)",
    "Bash(lark-cli doctor)", "Bash(lark-cli whoami)", "Bash(lark-cli --version)",
    "Bash(lark-cli schema *)", "Bash(lark-cli * --help)",
    "Bash(meegle workitem get *)", "Bash(meegle workitem query *)", "Bash(meegle workitem meta-types *)",
    "Bash(meegle workitem meta-fields *)", "Bash(meegle workitem meta-roles *)",
    "Bash(meegle workitem meta-create-fields *)", "Bash(meegle workitem list-op-records *)",
    "Bash(meegle comment list *)", "Bash(meegle workflow list-state-transitions *)",
    "Bash(meegle workflow list-state-required *)", "Bash(meegle project search *)",
    "Bash(meegle view search *)", "Bash(meegle view get *)", "Bash(meegle user me *)",
    "Bash(meegle auth status)", "Bash(meegle telemetry status)", "Bash(meegle version)",
    "Bash(python3 -I .claude/agent-org/bin/ledger.py get *)",
    "Bash(python3 -I .claude/agent-org/bin/ledger.py query *)",
    "Bash(python3 -I .claude/agent-org/bin/ledger.py trace *)",
    "Bash(python3 -I .claude/agent-org/bin/ledger.py status *)",
    "Bash(python3 -I .claude/agent-org/bin/ledger.py mql *)",
    "Bash(python3 -I .claude/agent-org/bin/route.py classify *)",
    "Bash(python3 -I .claude/agent-org/bin/orgspec.py lint *)",
    "Bash(python3 -I .claude/agent-org/bin/orgspec.py show *)",
]
ADMIN_DENY = [
    "Bash(lark-cli * --yes)", "Bash(lark-cli * --yes *)", "Bash(lark-cli auth login *)",
    "Bash(lark-cli auth logout *)", "Bash(lark-cli config *)", "Bash(lark-cli profile *)",
    "Bash(lark-cli update *)", "Bash(meegle auth login *)", "Bash(meegle auth logout *)",
    "Bash(meegle telemetry enable *)", "Bash(meegle telemetry disable *)", "Bash(meegle config set *)",
    "Bash(meegle config init *)", "Bash(meegle preference *)", "Bash(meegle ai-handoff *)",
]


class SetupError(Exception):
    pass


# ------------------------------------------------------------- helpers
def sha256(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def scaffold(rel):
    return read_text(os.path.join(SCAFFOLD, rel))


def render(template, values):
    def rep(m):
        if m.group(1) not in values:
            raise SetupError("模板缺少取值：%s" % m.group(1))
        return values[m.group(1)]
    return re.sub(r"\{\{([a-z_]+)\}\}", rep, template)


def dump_json(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def yaml_str(value):
    return json.dumps(value, ensure_ascii=False)


def bullets(items, indent=""):
    return "\n".join("%s- %s" % (indent, i) for i in items)


def code_list(items):
    return "、".join("`%s`" % p for p in items) if items else "无"


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "", str(e)


def plugin_version():
    try:
        return json.loads(read_text(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"))).get("version")
    except (OSError, ValueError):
        return None


# -------------------------------------------------------------- inputs
TASK_KEYS = ("name", "description", "editable_paths", "protected_paths", "feishu", "models")
# Earlier task files described the experiment in one line each (x, f, g, h, search). Their
# paths are taken over; the lines only seed the documents in spec/ the first time.
LEGACY_SEEDS = (("x", "可改范围"), ("f", "目标指标"), ("g", "实验资源"), ("h", "评测协议"), ("search", "搜索方法"))


def legacy_text(raw, key):
    v = raw.get(key)
    if isinstance(v, dict):
        v = v.get("definition")
    return v.strip() if isinstance(v, str) else ""


def load_task_and_seeds(path):
    """The task file in its current form, plus the one-line descriptions an earlier file gave.

    The task file holds only what scripts and hooks use: names, editable_paths (可改范围),
    protected_paths (评测协议), the Feishu space this project is bound to and the models.
    What the experiment is goes into spec/spec.md and spec/eval-protocol.md.
    """
    try:
        raw = json.loads(read_text(path))
    except (OSError, ValueError) as e:
        raise SetupError("读不到研究任务 %s：%s" % (path, e))
    if not isinstance(raw, dict):
        raise SetupError("研究任务 %s 要写成一个 JSON 对象" % path)
    for key in ("name", "description"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise SetupError("研究任务的 %s 要写成非空的文字" % key)
    for key in ("x", "f", "g", "h"):
        v = raw.get(key)
        if v is not None and not (isinstance(v, dict) and isinstance(v.get("definition", ""), str)):
            raise SetupError("研究任务里的 %s 是旧写法，只认 {\"definition\": \"...\", ...}；新写法见插件的 scaffold/task.example.json" % key)
    if raw.get("search") is not None and not isinstance(raw["search"], str):
        raise SetupError("研究任务的 search 要写成文字")
    unknown = sorted(k for k in raw if k not in TASK_KEYS + ("x", "f", "g", "h", "search") and not k.startswith("_"))
    if unknown:
        raise SetupError("研究任务里有不认识的字段：%s；能写的是 %s" % ("、".join(unknown), "、".join(TASK_KEYS)))
    task = {"name": raw["name"], "description": raw["description"]}
    for key, old in (("editable_paths", "x"), ("protected_paths", "h")):
        legacy = (raw.get(old) or {}).get("paths")
        if key in raw and legacy is not None:
            raise SetupError("研究任务里同时有 %s 和旧写法的 %s.paths；只留 %s" % (key, old, key))
        paths = raw.get(key, legacy)
        if paths is None:
            paths = []
        if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
            raise SetupError("研究任务的 %s 要写成路径列表" % key)
        clean = []
        for written in paths:
            p = written
            while p.startswith("./"):         # the hook compares paths without ./ and without a trailing /
                p = p[2:]
            if p.endswith("/"):
                p += "**"
            if not p or p.startswith("/") or ".." in p.split("/"):
                raise SetupError("%s 要写成项目目录内的相对路径：%s" % (key, written))
            clean.append(p)
        task[key] = clean
    feishu = raw.get("feishu")
    if feishu is None:
        feishu = {}
    if not isinstance(feishu, dict) or not all(isinstance(v, str) for v in feishu.values() if v is not None):
        raise SetupError("研究任务的 feishu 要写成 {\"meegle_space\": \"...\", ...}，值都是文字")
    if not (feishu.get("meegle_space") or "").strip():
        raise SetupError("研究任务要写明这个项目用哪个飞书项目空间：feishu.meegle_space 填空间名、simple_name 或 project_key。"
                         "初始化以后用 setup.py feishu --live 绑定这个空间")
    task["feishu"] = feishu
    if raw.get("models") is not None and not (
            isinstance(raw["models"], dict) and all(isinstance(v, str) for v in raw["models"].values())):
        raise SetupError("研究任务的 models 要写成 {\"角色或职级\": \"模型名\"}")
    models = dict(DEFAULT_MODELS)
    models.update(raw.get("models") or {})
    task["models"] = models
    seeds = {k: legacy_text(raw, k) for k, _ in LEGACY_SEEDS if legacy_text(raw, k)}
    legacy = [k for k, _ in LEGACY_SEEDS if k in raw]
    return task, seeds, legacy


def load_task(path):
    return load_task_and_seeds(path)[0]


def pick_sequences(spec, wanted):
    all_ids = [s for s, _, _ in spec.sequences()]
    if not wanted:
        return all_ids
    chosen = [s.strip() for s in wanted.split(",") if s.strip()]
    unknown = [s for s in chosen if s not in all_ids]
    if unknown:
        raise SetupError("没有这些序列：%s；可选：%s" % ("、".join(unknown), "、".join(all_ids)))
    for s in REQUIRED_SEQUENCES:
        if s not in chosen:
            raise SetupError("序列 %s 必须启用：项目管理是入口，质量负责验收" % s)
    return [s for s in all_ids if s in chosen]


def load_ids(target):
    path = os.path.join(target, "feishu", "ids.json")
    if not os.path.isfile(path):
        return {}
    try:
        ids = json.loads(read_text(path))
    except ValueError:
        raise SetupError("%s 不是合法 JSON" % path)
    if not isinstance(ids, dict) or any(not isinstance(ids.get(k), (dict, type(None))) for k in ("meegle", "wiki", "lark")):
        raise SetupError("%s 的结构不对：要是一个对象，meegle、wiki、lark 各是一个对象" % path)
    return ids


# --------------------------------------------------------------- roles
def agent_name(seq, rank):
    if seq == "pmo" and rank == 1:
        return "pmo-router"
    if seq == "pmo" and rank == 0:
        return "pmo-scribe"
    return "%s-p%d" % (seq, rank)


def tag_of(abbrev, rank):
    return "[%s+%d]" % (abbrev, rank)


def write_paths(seq, rank, task):
    if seq == "pmo":
        return {2: ["packages/**", "inbox/**", "org/**"], 1: ["packages/drafts/pmo/**"],
                0: ["packages/**", "feishu/**"]}[rank]
    paths = ["packages/drafts/%s/**" % seq]
    if rank == 2:
        paths.append(".claude/agent-memory/%s/**" % agent_name(seq, rank))
    if seq == "rnd":
        paths += list(task["editable_paths"]) + ["runs/**"]
    elif seq in ("alg", "quality"):
        paths.append("runs/%s/**" % seq)
    elif seq == "infra":
        paths += ["infra/**", "runs/infra/**"]
    return paths


def spawn_list(seq, rank, seqs):
    if seq == "pmo" and rank == 2:
        return [agent_name(s, 2) for s in seqs if s != "pmo"] + ["pmo-router", "pmo-scribe", "Explore", "Plan"]
    if seq == "pmo" and rank == 1:
        return ["pmo-scribe"]
    if seq == "pmo" or rank == 0:
        return []
    return [agent_name(seq, rank - 1), "pmo-scribe"]


def build_policy(spec, task, seqs):
    rules = spec.rules
    limits, fixed = rules["limits"], rules["naming"]["fixed_sessions"]
    agents = {}
    for seq in seqs:
        abbrev = spec.term("abbrev", seq)
        for rank in (2, 1, 0):
            name = agent_name(seq, rank)
            pattern = fixed.get(name) or ("%s-p0-<工作项 ID>" % abbrev if rank == 0 else "%s-p%d-<序号>" % (abbrev, rank))
            pol = {"tag": tag_of(abbrev, rank), "abbrev": abbrev, "rank": rank, "sequence": seq,
                   "limit": limits.get(name, limits["p%d" % rank]), "fixed_session": fixed.get(name),
                   "session_pattern": pattern, "spawn": spawn_list(seq, rank, seqs),
                   "write_paths": write_paths(seq, rank, task), "lark_write": [], "lark_ask": [],
                   "meegle_write": ["comment add"], "meegle_ask": [], "docs_create_parents": None,
                   "ledger_write": False}
            if name == "pmo-scribe":
                pol.update(lark_write=SCRIBE_LARK, meegle_write=SCRIBE_MEEGLE, ledger_write=True)
            elif name == "pmo-p2":
                pol["lark_write"] = ["im +messages-send", "im +messages-reply"]
            agents[name] = pol
    p2s = [agent_name(s, 2) for s in seqs]
    owned = {".claude/agent-memory/**": p2s,
             ".claude/**": [agent_name("infra", r) for r in (2, 1, 0)] if "infra" in seqs else [],
             "org/**": ["pmo-p2"], "spec/spec.md": ["alg-p2"] if "alg" in seqs else [],
             "spec/requirements.md": ["product-p2"] if "product" in seqs else [],
             "spec/eval-protocol.md": ["quality-p2"], "spec/task.json": []}
    return {
        "version": 3,
        "main_agent": rules["entry"]["main_agent"],
        "agents": agents,
        "default_agent": {"tag": "[unknown]", "abbrev": "", "rank": 0, "sequence": "", "limit": 0,
                          "fixed_session": None, "session_pattern": "", "spawn": [], "write_paths": [],
                          "lark_write": [], "lark_ask": [], "meegle_write": [], "meegle_ask": [],
                          "docs_create_parents": None, "ledger_write": False},
        "h_paths": list(task["protected_paths"]),         # 评测协议的只读路径
        "x_paths": list(task["editable_paths"]),          # 可改范围的可写路径
        "h_writers": [agent_name("quality", r) for r in (2, 1, 0)],
        "x_writers": [agent_name("rnd", r) for r in (2, 1, 0)] if "rnd" in seqs else [],
        "owned_paths": owned,
        "identity": {"user_domains": ["docs", "wiki", "drive", "markdown", "base", "sheets"],
                     "bot_commands": ["im +messages-send", "im +messages-reply", "event consume"]},
        "sequences": seqs,
    }


def build_settings(task, risk, main_agent):
    high = sorted(k for k, v in risk.get("commands", {}).items() if v == "high-risk-write")
    deny = ADMIN_DENY + ["Bash(lark-cli %s *)" % k for k in high if k != "update"]
    ask = ["Edit(/.claude/settings.json)", "Edit(/.claude/settings.local.json)", "Edit(/.claude/hooks/**)",
           "Edit(/.claude/agents/**)", "Edit(/.claude/agent-org/**)", "Edit(/org/**)", "Edit(/spec/**)"]
    ask += ["Edit(/%s)" % p for p in task["protected_paths"]]

    def hook(script):
        return [{"type": "command", "timeout": 30,
                 "command": "python3 -I -B \"$CLAUDE_PROJECT_DIR/.claude/hooks/%s\"" % script}]
    return {
        "agent": main_agent,
        "env": {"MEEGLE_NO_UPDATE_CHECK": "1", "MEEGLE_AI_HANDOFF": "disabled"},
        "permissions": {"allow": READ_ALLOW, "ask": ask, "deny": deny},
        "hooks": {
            "PreToolUse": [{"matcher": "Bash|Edit|Write|NotebookEdit|Agent|Task", "hooks": hook("guard.py")}],
            "PostToolUse": [{"matcher": "Bash", "hooks": hook("audit.py")}],
            "PostToolUseFailure": [{"matcher": "Bash", "hooks": hook("audit.py")}],
            "SubagentStart": [{"hooks": hook("instances.py")}],
            "SubagentStop": [{"hooks": hook("instances.py")}],
            "SessionStart": [{"hooks": hook("instances.py")}],
            "SessionEnd": [{"hooks": hook("instances.py")}],
            "UserPromptSubmit": [{"hooks": hook("inbox.py")}],
        },
    }


# ----------------------------------------------------------- lark risk
def installed_lark_version():
    code, out, _ = run(["lark-cli", "--version"])
    found = re.findall(r"\d+\.\d+\.\d+", out or "") if code == 0 else []
    return found[0] if found else None


def resolve_risk(target):
    """Prefer an existing table for the installed lark-cli; else the plugin's static table."""
    static = json.loads(scaffold("hooks/lark-risk.static.json"))
    installed = installed_lark_version()
    existing = os.path.join(target, ".claude", "hooks", "lark-risk.json")
    if installed and os.path.isfile(existing):
        try:
            table = json.loads(read_text(existing))
            if table.get("lark_cli_version") == installed:
                return table, "风险表：沿用项目里 lark-cli %s 的风险表" % installed
        except ValueError:
            pass
    note = "风险表：用插件自带的静态表（lark-cli %s）" % static.get("lark_cli_version")
    if installed and installed != static.get("lark_cli_version"):
        note += "；本机是 %s，新命令的风险等级可能缺失，未知的写命令一律按写处理" % installed
    return static, note


# -------------------------------------------------------------- render
def naming_lines(spec):
    n = spec.rules["naming"]
    fixed = "、".join("%s 是 `%s`" % (k, v) for k, v in n["fixed_sessions"].items())
    return [
        "会话名写作 `%s`；+0 写作 `%s`；%s。" % (n["session"], n["session_p0"], fixed),
        "会话标签写作 `%s`，飞书评论和交接记录的开头都带上它。" % n["tag"],
        "工作项名称写作 `%s`；主文档标题写作 `%s`；挂在下面的答复写作 `%s`。" % (n["work_item"], n["main_doc"], n["reply_doc"]),
        "草稿路径写作 `%s`，序列用 product、alg、rnd、infra、quality、pmo、liaison。" % n["draft"],
        "日期字段写目标日期的 UTC 中午。meegle 的 workitem 命令里工作项类型写 api_name；MQL 的表名认 type_key 或类型现名，不认 api_name；view search 的 --view-scope 只认 type_key。ledger.py 已经处理好，手写时注意。",
    ]


def describe_writes(pol):
    parts = []
    if pol["ledger_write"]:
        parts.append("`ledger.py` 的全部写命令")
    parts += ["`lark-cli %s`" % k for k in pol["lark_write"]]
    parts += ["`meegle %s`" % k for k in pol["meegle_write"]]
    return "、".join(parts) or "无"


def tools_for(seq, pol):
    tools = ["Read", "Grep", "Glob", "Bash", "Skill", "Write", "Edit"]
    if seq in ("product", "alg"):
        tools += ["WebSearch", "WebFetch"]
    if pol["spawn"]:
        tools.append("Agent")
    return tools


def general_rank_text(spec, rank):
    row = spec.rank_rows()[rank]
    return "\n".join([
        "- 给定的上下文：%s" % row["given"], "- 需要产出：%s" % row["outputs"],
        "- 上下文维护标准：%s" % row["context"], "- 什么时候上报：%s" % row["escalate"],
        "- 通用规则：%s" % "；".join(spec.general_rules_heads()),
    ])


SPEC_DOC_NAMES = {"spec/spec.md": "实验规格", "spec/eval-protocol.md": "评测协议", "spec/requirements.md": "需求文档"}


def agent_mechanics(spec, seq, rank, name, pol, task, policy):
    n = spec.rules["naming"]
    if rank == 0:
        session = "派发方会在提示里写明你的会话名，写作 `%s`；启动时 hook 也会告诉你。" % pol["session_pattern"]
    else:
        session = "启动时 hook 会告诉你这一次的会话名，写作 `%s`，序号取最小的空闲号。" % pol["session_pattern"]
    lines = [
        "- 会话名：%s会话标签是 `%s`。" % (session, pol["tag"]),
        "- 开工或接手：先读交给你的包；再用 `python3 -I .claude/agent-org/bin/ledger.py get <工作项 ID>` 读工作项，读主文档的“状态包”接上进度。不靠对话记状态。",
        "- 写包用技能 `agent-org:packages`（模板副本在 `.claude/agent-org/templates/`）。草稿写到 `packages/drafts/%s/`，文件名照 `%s`；写完跑 `python3 -I .claude/agent-org/bin/orgspec.py lint <路径>`。" % (seq, n["draft"]),
        "- 交回：最后一条消息只写包的路径和三行以内的摘要，正文都在文件里。要送审的，摘要里写“待送审：<送审包路径>”。",
    ]
    if rank == 2:
        lines.append("- 派 +1：先写下发关系的需求包，派 `pmo-scribe` 登记（提示写“登记请求”和草稿路径，马上要派的写“登记并开工”，书记员会接着流转到“执行中”），拿到工作项 ID；再用 Agent 工具派 `%s`，不要带 `name`，提示里给需求包路径和工作项 ID。会话名由 hook 分配。排队没派出去的留在“待开工”，派之前再请书记员流转。" % pol["spawn"][0])
    elif rank == 1:
        lines.append("- 派 +0：执行关系的需求包按验收标准划分，不按运行次数划分。先派 `pmo-scribe` 登记并开工（提示写“登记请求”、草稿路径、“来源工作项 #<所属阶段的下发需求包 ID>”和“开工”，书记员登记后接着流转到“执行中”），拿到工作项 ID；再用 Agent 工具派 `%s`，不要带 `name`，提示里单独写一行“会话名：%s-p0-<工作项 ID>”，并给需求包路径。排队没派出去的留在“待开工”，派之前再请书记员流转。" % (pol["spawn"][0], pol["abbrev"]))
    else:
        lines.append("- 你不能再派子智能体。")
    if pol["spawn"]:
        lines.append("- 并行上限：每个角色 +2 最多 1 个、+1 最多 3 个、+0 最多 5 个，书记员 1 个。到了上限 hook 会拒绝派发，这时等在跑的交回，或者把任务写进交回的包，交回上一级排队。")
    lines += [
        "- 跨序列的需求包、送审包、决策包写成草稿交回，由入口转交给对方序列的 +2、质量 +2 或研究员；你不直接派别的序列。执行结果先送审，拿到验收包再交上一级。",
        "- 工作项、字段、状态和文档只由书记员写；你能写的飞书内容只有带会话标签的评论（`meegle comment add`，hook 会补标签）。",
        "- 读台账和文档：`ledger.py get`、`ledger.py query`、`ledger.py trace`、`ledger.py status`，以及 `lark-cli docs +fetch --as user`。",
    ]
    docs = [p for p, owners in sorted(policy["owned_paths"].items()) if name in owners and p in SPEC_DOC_NAMES]
    writable = code_list(pol["write_paths"]) + ("，以及你起草的 %s（写之前会请研究员确认）" % code_list(docs) if docs else "")
    lines.append("- 本地文件只能写：%s；评测协议的只读路径（%s）%s。" % (
        writable, code_list(task["protected_paths"]), "改动要研究员确认" if name in policy["h_writers"] else "不能改"))
    for doc in docs:
        how = "" if doc == "spec/requirements.md" else "用技能 `agent-org:spec` 访谈研究员后写，每条标来源；"
        lines.append("- 你负责起草%s `%s`：%s研究员签字后生效。" % (SPEC_DOC_NAMES[doc], doc, how))
    if rank == 2:
        lines += [
            "- 研究员可能直接用 `claude --agent %s` 和你开聚焦会话。这时你在主会话里，可以直接问研究员；谈定的事写成决策包（渠道：研究员主动发起），交回入口登记。没写进台账的决定不算数。" % name,
            "- 你不在聚焦会话里时，不能直接问研究员：要研究员拍板的事写决策包；+1 和 +0 要拍板的事写进交付包。",
            "- 你有项目级记忆（`.claude/agent-memory/%s/`）。只记跨阶段要沿用的标准和判断；任务的当前状态以台账和状态包为准。" % name,
        ]
    else:
        lines.append("- 你不能直接问研究员。要拍板的事写进交给上一级的交付包。")
    if seq == "quality":
        lines.append("- 质量序列自己的产出在序列内验收，不送审；同序列需求的“送审包”一栏填序列内验收用的交付包链接。")
    if seq == "liaison":
        lines.append("- 汇报材料写成草稿，由书记员上传到知识库的“汇报文档”节点，研究员确认后自己转交导师。导师不用飞书：不给导师开任何飞书权限，也不为导师建视图。")
    return "\n".join(lines)


def agent_file(spec, seq, rank, task, policy):
    name = agent_name(seq, rank)
    pol = policy["agents"][name]
    s = spec.sequence(seq)
    row = s["ranks"][rank]
    desc = "%s +%d。接手：%s。何时上报：%s。" % (s["cn"], rank, row["given"].replace("<br>", "；"),
                                         row["escalate"].replace("<br>", "；"))
    values = {
        "name": name, "description_json": yaml_str(desc), "tools": ", ".join(tools_for(seq, pol)),
        "model": task["models"].get(name) or task["models"].get("p%d" % rank) or "inherit",
        "memory_line": "memory: project\n" if rank == 2 else "",
        "project": task["name"], "seq_cn": s["cn"], "rank": str(rank), "tag": pol["tag"],
        "seq_intro": s["intro"], "rank_given": row["given"], "rank_outputs": bullets(row["outputs"], "  "),
        "rank_escalate": row["escalate"], "seq_notes": ("\n" + s["notes"] + "\n") if s["notes"] else "",
        "general_rank": general_rank_text(spec, rank),
        "mechanics": agent_mechanics(spec, seq, rank, name, pol, task, policy),
    }
    return render(scaffold("agents/agent.md.tmpl"), values)


def lead_by_phase_text(spec):
    rows = []
    for item in spec.rules["entry"]["lead_by_phase"]:
        rows.append("%s：%s" % (item["phase"], "、".join("%s +2（`%s`）" % (spec.term("seq", s), agent_name(s, 2))
                                                  for s in item["sequences"])))
    return bullets(rows + [spec.render(spec.rules["entry"]["focus_rule"])])


def pmo_file(spec, rank, task, policy):
    name = agent_name("pmo", rank)
    pol = policy["agents"][name]
    s = spec.sequence("pmo")
    row = s["ranks"][rank]
    model = task["models"].get(name) or task["models"].get("p%d" % rank) or "inherit"
    common = {"project": task["name"], "session": pol["fixed_session"], "tag": pol["tag"], "model": model,
              "rank_given": row["given"], "rank_outputs": bullets(row["outputs"], "  "),
              "rank_escalate": row["escalate"], "naming": bullets(naming_lines(spec))}
    if rank == 2:
        common.update({
            "description_json": yaml_str("项目管理 +2（入口）。主会话：研究员的信息和指示都先到这里，按路由决策表分发，管理决策队列和聚焦会话。"),
            "seq_intro": s["intro"], "handoff_rules": spec.handoff_rules(), "signoff": spec.signoff_items(),
            "routing_table": spec.routing_table(), "package_routes": spec.package_routes(),
            "dispatch_targets": code_list(pol["spawn"]), "lead_by_phase": lead_by_phase_text(spec),
            "writes": describe_writes(pol),
        })
        return render(scaffold("agents/entry.md.tmpl"), common)
    if rank == 1:
        common.update({"description_json": yaml_str("项目管理 +1（路由）。按路由决策表给出路由结论、汇总每日决策队列、安排书记员登记；不派发序列 +2。"),
                       "routing_table": spec.routing_table()})
        return render(scaffold("agents/router.md.tmpl"), common)
    common.update({"description_json": yaml_str("项目管理 +0（书记员）。台账和文档唯一的写入方：按登记请求和需求包建工作项、上传文档、写字段、流转状态。"),
                   "docs_tree": spec.docs_tree()})
    return render(scaffold("agents/scribe.md.tmpl"), common)


def task_summary(task):
    lines = ["- 名称：%s（%s）" % (task["name"], task["description"]),
             "- 研究问题、目标和判据：立项单（飞书项目里“项目”工作项的主文档）和 `spec/requirements.md`。",
             "- 实验规格：`spec/spec.md`，写可改范围、目标指标、实验资源和搜索方法。可写路径：%s，只有工程序列能写。" % (
                 code_list(task["editable_paths"])),
             "- 评测协议：`spec/eval-protocol.md`，写数据、划分、打分和证据标准。只读路径：%s。" % code_list(task["protected_paths"])]
    return "\n".join(lines)


def where_text(task, ids):
    m = ids.get("meegle") or {}
    w = ids.get("wiki") or {}
    fs = task.get("feishu") or {}
    return "\n".join([
        "- 台账：飞书项目空间「%s」，project_key %s；字段字典在 `feishu/field-dictionary.md`。" % (
            fs.get("meegle_space", "未填"), "`%s`" % m["project_key"] if m.get("project_key") else "待填（跑 `setup.py feishu`）"),
        "- 任务包：飞书知识库空间「%s」%s；发出前的草稿在 `packages/drafts/<序列>/`。" % (
            fs.get("wiki_space", "未填"), "，space_id `%s`" % w["space_id"] if w.get("space_id") else ""),
        "- 实验规格、评测协议和需求文档在 `spec/`，运行记录在 `runs/`，阶段和主责序列在 `org/phases.md`。",
        "- 在跑的实例、会话名和 agent ID 记在 `.claude/state/instances.json`，由 hook 维护；飞书写操作的日志在 `feishu/write-log.jsonl`；收件箱在 `inbox/`。",
    ])


def shared_mechanics():
    return bullets([
        "主会话是入口 `pmo-p2`。研究员也可以用 `claude --agent <角色>` 和某个序列的 +2 开聚焦会话；开之前先在入口登记。",
        "派发层级：入口派各序列 +2、路由和书记员；+2 派本序列 +1，+1 派本序列 +0；+2 和 +1 都可以派书记员登记自己的草稿。嵌套最多三层，+0 不能再派。",
        "工作项、字段、状态和文档只由书记员写，写操作都走 `ledger.py`；其他会话只能加带会话标签的评论。",
        "high-risk-write 和带 `--yes` 的飞书命令一律不跑：把完整命令写给研究员，由研究员本人执行。",
        "知识库、文档和云空间的命令显式带 `--as user`；收发消息和订阅事件带 `--as bot`。",
    ])


def claude_md(spec, task, ids):
    values = {
        "project": task["name"], "task_summary": task_summary(task), "rank_table": spec.rank_table(),
        "general_rules": bullets(spec.general_rules_heads()), "handoff_rules": spec.handoff_rules(),
        "signoff": spec.signoff_items(), "naming": bullets(naming_lines(spec)), "where": where_text(task, ids),
        "mechanics": shared_mechanics(),
    }
    return render(scaffold("CLAUDE.md.tmpl"), values)


def org_md(spec, seqs):
    parts = ["# 组织规则", "",
             "> 由插件 agent-org 的规则文件（`.claude/agent-org/orgspec/rules.json`）生成，不要在这里改。组织规则的变更走决策包，研究员签字后改规则文件，再重跑初始化。研究员的补充决定在 `org/decisions.md`。", "",
             "## 职级", "", spec.rank_table(), ""]
    for head, body in spec.general_rules():
        parts += ["**%s**%s" % (head, body), ""]
    parts += ["## 只能由人来做的事", "", spec.signoff_items(), "", "## 序列", "", spec.sequence_overview(), ""]
    for sid in seqs:
        s = spec.sequence(sid)
        parts += ["### %s" % s["heading"], "", s["intro"], "",
                  orgspec.md_table(["职级", "已给定", "产出", "何时上报"],
                                   [[r["label"], r["given"], "<br>".join(r["outputs"]), r["escalate"]]
                                    for r in (s["ranks"][2], s["ranks"][1], s["ranks"][0])]), ""]
        if s["notes"]:
            parts += [s["notes"], ""]
    parts += ["## 任务包", "", spec.package_table(), "", "### 三种关系", "", spec.relation_matrix(), "",
              "### 包的流向", "", spec.package_routes(), "", "### 交接规则", "", spec.handoff_rules(), "",
              "## 入口和路由", "", spec.routing_table(), "",
              "## 台账：飞书项目", "", spec.work_items_table(), "", "### 状态和流转表单", "", spec.flows_table(), "",
              "### 字段", "", spec.fields_table(), "", "## 任务包文档：飞书文档", "", spec.docs_tree(), ""]
    return "\n".join(parts)


def grants_md(spec, task, policy):
    rows = []
    for name, pol in policy["agents"].items():
        s = spec.sequence(pol["sequence"])
        rows.append([name, s["cn"], "+%d" % pol["rank"],
                     task["models"].get(name) or task["models"].get("p%d" % pol["rank"]) or "inherit",
                     pol["limit"], s["ranks"][pol["rank"]]["given"], "初始授权，待评测"])
    head = spec.general_rules_heads()[2]
    return "\n".join(["# 授权表：配置 × 任务类型", "",
                      "> 组织规则：**%s**一份配置就是 `.claude/agents/` 里的一份定义，包括模型、提示词和工具；授权的任务就是这一级“已给定”的那类包。按评测结果重新核定时，改这张表和对应的 agent 定义，走组织规则变更，由研究员签字。" % head, "",
                      orgspec.md_table(["配置", "序列", "职级", "模型", "并行上限", "授权的任务（已给定）", "状态"], rows), ""])


def phases_md(spec):
    rows = []
    for item in spec.rules["entry"]["lead_by_phase"]:
        rows.append("| %s | 待立项时填写 | 待立项时填写 | %s | | 未开始 |" % (
            item["phase"], "、".join(spec.term("seq", s) for s in item["sequences"])))
    return render(scaffold("org/phases.md.tmpl"), {"rows": "\n".join(rows), "current": "未开始"})


SEED_PLACEHOLDERS = {
    "x": "<能改哪些模块、配置、超参或提示词；即使在可写路径里也不能碰的文件；一个候选是什么，比如一个 git 快照；从哪个版本起步，它的分数是多少；能用哪些依赖和外部服务>",
    "f": "<指标名称和方向，在哪个划分上算；多个种子怎么合成一个分数；提高多少才算数，用什么检验或置信区间判断；哪些护栏指标不能变差；优化时看哪个分数，最终报告用哪个>",
    "g": "<用什么计成本：GPU 小时、API 费用、token、墙钟时间，评测本身的花费也算；单个候选的上限和总预算；谁计量、记在哪；超了谁叫停>",
    "h": "<一句话说清：用什么数据、怎么打分、优化过程中谁能看到什么>",
    "search": "<外层怎样生成候选、怎样挑下一个要扩展的候选、什么时候停>",
}


def seeded(seeds, key):
    text = seeds.get(key)
    return "%s（取自旧版任务文件）〔推测，待确认〕" % text if text else SEED_PLACEHOLDERS[key]


def spec_files(spec, task, seeds, seqs):
    sign = "需求文档、规格、权限策略……由研究员签字"
    for item in spec.rules.get("only_humans", []):
        if item.startswith("签字"):
            sign = item
    req = "\n\n".join("## %s\n\n无" % h for h in ("要解决的问题和目标用户", "现状工作流和基线", "成功判据（事先写下）",
                                                 "约束", "停止条件", "试点方案"))
    common = {"project": task["name"], "sign_rule": sign, "req_sections": req,
              "spec_drafting": "由%s +2 访谈研究员后起草" % spec.term("seq", "alg") if "alg" in seqs
                               else "这个项目没有启用算法序列，由研究员本人写",
              "eval_drafting": "由%s +2 访谈研究员后起草" % spec.term("seq", "quality"),
              "scope_seed": seeded(seeds, "x"), "metric_seed": seeded(seeds, "f"),
              "resources_seed": seeded(seeds, "g"), "search_seed": seeded(seeds, "search"),
              "eval_seed": seeded(seeds, "h")}
    return {"spec/spec.md": render(scaffold("spec/spec.md.tmpl"), common),
            "spec/requirements.md": render(scaffold("spec/requirements.md.tmpl"), common),
            "spec/eval-protocol.md": render(scaffold("spec/eval-protocol.md.tmpl"), common),
            "spec/task.json": dump_json(task)}


# Headings only the templates before the rename used; such a document is still the old draft.
OLD_SPEC_HEADINGS = {"spec/spec.md": "## x：可以改的代码", "spec/eval-protocol.md": "## h 的定义"}


def deviations_text(spec):
    items = list(spec.rules.get("plugin_decisions", []))
    return bullets(items) if items else "- 无"


def feishu_readme(task, ids):
    m = ids.get("meegle") or {}
    line = "飞书项目「%s」（project_key %s）；知识库「%s」。" % (
        (task.get("feishu") or {}).get("meegle_space", "未填"), m.get("project_key") or "待填",
        (task.get("feishu") or {}).get("wiki_space", "未填"))
    return render(scaffold("feishu/README.md.tmpl"), {"space_line": line})


# ---------------------------------------------------------------- plan
class Plan:
    def __init__(self):
        self.files = {}      # rel -> (content, executable, keep_existing)
        self.notes = []
        self.manifest = {}


def build_plan(target, task_path, seq_arg):
    spec = orgspec.OrgSpec()
    problems = spec.validate()
    if problems:
        raise SetupError("规则文件有问题，先修好：\n" + "\n".join(problems))
    task, seeds, legacy = load_task_and_seeds(task_path)
    seqs = pick_sequences(spec, seq_arg)
    ids = load_ids(target)
    risk, note = resolve_risk(target)
    plan = Plan()
    plan.notes.append(note)
    if not (ids.get("meegle") or {}).get("project_key"):
        plan.notes.append("提醒：项目还没绑定飞书空间。初始化以后跑 `setup.py feishu --target <项目目录> --live`，看任务文件里那个空间的匹配结果，核对后加 --apply；绑定以前台账脚本不能用。")
    policy = build_policy(spec, task, seqs)
    f = plan.files

    def put(rel, content, executable=False, keep=False):
        f[rel] = (content, executable, keep)

    put("CLAUDE.md", claude_md(spec, task, ids))
    put(".claude/settings.json", dump_json(build_settings(task, risk, policy["main_agent"])))
    put(".claude/hooks/policy.json", dump_json(policy))
    put(".claude/hooks/lark-risk.json", dump_json(risk))
    for script in HOOK_SCRIPTS:
        put(".claude/hooks/" + script, scaffold("hooks/" + script), script != "feishu_cmd.py")
    for seq in seqs:
        for rank in (2, 1, 0):
            name = agent_name(seq, rank)
            content = pmo_file(spec, rank, task, policy) if seq == "pmo" else agent_file(spec, seq, rank, task, policy)
            put(".claude/agents/%s.md" % name, content)
    for script in BIN_SCRIPTS:
        put(".claude/agent-org/bin/" + script, read_text(os.path.join(HERE, script)), True)
    for name in ("rules.json", "terms.zh.json"):
        put(".claude/agent-org/orgspec/" + name, read_text(os.path.join(orgspec.ORGSPEC_DIR, name)))
    for name in sorted(os.listdir(TEMPLATES)):
        if name.endswith(".md"):
            put(".claude/agent-org/templates/" + name, read_text(os.path.join(TEMPLATES, name)))
    put("org/org.md", org_md(spec, seqs))
    put("org/grants.md", grants_md(spec, task, policy))
    put("org/decisions.md", render(scaffold("org/decisions.md.tmpl"), {"deviations": deviations_text(spec)}), keep=True)
    put("org/phases.md", phases_md(spec), keep=True)
    for rel, content in spec_files(spec, task, seeds, seqs).items():
        put(rel, content, keep=rel != "spec/task.json")
    if legacy:
        plan.notes.append("提醒：任务文件还是旧写法（%s）。路径已经当作 editable_paths、protected_paths 读进来；旧的一句话说明只在"
                          "第一次生成 spec/ 下的文档时填进对应小节，标成〔推测，待确认〕。换成新写法时，先把 x.paths、h.paths 改写成 "
                          "editable_paths、protected_paths，再删掉这几项；格式见插件的 scaffold/task.example.json。" % "、".join(legacy))
    if not task["editable_paths"]:
        plan.notes.append("提醒：任务文件没有写 editable_paths，工程序列改不了任何代码。")
    if not task["protected_paths"]:
        plan.notes.append("提醒：任务文件没有写 protected_paths，评测脚本和评测集没有只读保护。")
    for key in ("editable_paths", "protected_paths"):
        for p in task[key]:
            if not re.search(r"[*?\[]", p) and os.path.isdir(os.path.join(target, p)):
                plan.notes.append("提醒：%s 里的 %s 是目录，写成 %s/** 才能管到里面的文件。" % (key, p, p))
    for rel, heading in sorted(OLD_SPEC_HEADINGS.items()):
        path = os.path.join(target, rel)
        if not os.path.isfile(path):
            continue
        try:
            old = heading in read_text(path)
        except (UnicodeDecodeError, OSError):
            plan.notes.append("提醒：%s 读不了（不是 UTF-8 编码？），没检查它是不是旧模板；请另存成 UTF-8。" % rel)
            continue
        if old:
            plan.notes.append("提醒：%s 还是改名前的旧模板。新模板按小节写清每一项，用技能 `agent-org:spec` 和研究员访谈后重写；"
                              "新模板在插件的 scaffold/%s.tmpl。" % (rel, rel))
    put("feishu/README.md", feishu_readme(task, ids))
    put("packages/README.md", scaffold("packages/README.md.tmpl"))
    for seq in seqs:
        put("packages/drafts/%s/.gitkeep" % seq, "")
    put("packages/sent/.gitkeep", "")
    put("inbox/README.md", scaffold("inbox/README.md.tmpl"))
    put("runs/README.md", scaffold("runs/README.md.tmpl"))
    plan.manifest = {
        "plugin": "agent-org", "plugin_version": plugin_version(),
        "sequences": seqs, "main_agent": policy["main_agent"],
        "task_sha256": sha256(dump_json(task)),
        "rules_sha256": sha256(read_text(spec.rules_path)), "terms_sha256": sha256(read_text(spec.terms_path)),
        "lark_risk": {"lark_cli_version": risk.get("lark_cli_version"), "commands": len(risk.get("commands", {}))},
        "files": {p: sha256(c) for p, (c, _, _) in sorted(f.items())},
    }
    return plan


def previous_hashes(target):
    m = os.path.join(target, ".claude", "agent-org", "manifest.json")
    try:
        return json.loads(read_text(m)).get("files", {})
    except (OSError, ValueError):
        return {}


def actions_for(target, plan):
    acts = []
    before = previous_hashes(target)
    for rel, (content, executable, keep) in sorted(plan.files.items()):
        path = os.path.join(target, rel)
        if not os.path.lexists(path):
            acts.append(("create", rel, None))
            continue
        try:
            current = read_text(path) if os.path.isfile(path) else None
        except UnicodeDecodeError:
            current = None
        if current == content:
            ok = not executable or os.access(path, os.X_OK)
            acts.append(("same" if ok else "chmod", rel, None))
        elif keep:
            acts.append(("kept", rel, None))
        elif current is not None and before.get(rel) == sha256(current):
            acts.append(("update", rel, current))
        elif os.path.isfile(path + ".new") and read_text(path + ".new") == content:
            acts.append(("pending", rel, current))
        else:
            acts.append(("new", rel, current))
    return acts


LABELS = {"create": "新建", "same": "相同，跳过", "chmod": "补可执行权限", "update": "更新（上次生成后没人改过）",
          "new": "本地改过，新版本写 .new",
          "pending": "内容不同，.new 已存在", "kept": "已有，研究员维护，不动"}


def manifest_text(plan, acts):
    m = dict(plan.manifest)
    m["pending_new"] = sorted(rel for kind, rel, _ in acts if kind in ("new", "pending"))
    return dump_json(m)


def report(target, plan, acts, apply):
    print("目标目录：%s" % target)
    for n in plan.notes:
        print(n)
    counts = {}
    for kind, rel, current in acts:
        counts[kind] = counts.get(kind, 0) + 1
        if kind in ("same", "kept"):
            continue
        print("  [%s] %s" % (LABELS[kind], rel))
        if kind in ("new", "update"):
            diff = difflib.unified_diff((current or "").splitlines(True), plan.files[rel][0].splitlines(True),
                                        rel, rel + ".new", n=1)
            sys.stdout.writelines("      " + line for line in list(diff)[:60])
    print("合计：" + "，".join("%s %d" % (LABELS[k], v) for k, v in sorted(counts.items())))
    if not apply:
        print("这是预览，没有写任何文件。加 --apply 才会写。")


def apply_plan(target, plan, acts):
    written = 0
    for kind, rel, _ in acts:
        path = os.path.join(target, rel)
        if kind in ("create", "new", "update"):
            content, executable, _ = plan.files[rel]
            dest = path + ".new" if kind == "new" else path
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(content)
            if executable:
                os.chmod(dest, os.stat(dest).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            written += 1
        elif kind == "chmod":
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            written += 1
    mpath = os.path.join(target, ".claude", "agent-org", "manifest.json")
    text = manifest_text(plan, acts)
    if not (os.path.isfile(mpath) and read_text(mpath) == text):
        os.makedirs(os.path.dirname(mpath), exist_ok=True)
        with open(mpath, "w", encoding="utf-8") as fh:
            fh.write(text)
        written += 1
    print("已写入 %d 处。" % written)
    return written


# ------------------------------------------------------------ precheck
def precheck():
    rows = []

    def add(item, status, detail):
        rows.append((item, status, detail))

    if sys.platform == "darwin":
        apps = [p for p in ("/Applications/Lark.app", "/Applications/Feishu.app",
                            os.path.expanduser("~/Applications/Lark.app"),
                            os.path.expanduser("~/Applications/Feishu.app")) if os.path.isdir(p)]
        add("飞书客户端", "通过" if apps else "提醒", apps[0] if apps else "没找到 Lark.app 或 Feishu.app；网页版也能用")
    else:
        add("飞书客户端", "跳过", "不是 macOS，请人工确认")
    for cli, cmd in (("lark-cli", ["lark-cli", "--version"]), ("meegle", ["meegle", "version"]),
                     ("claude", ["claude", "--version"])):
        code, out, err = run(cmd)
        found = re.findall(r"\d+\.\d+\.\d+", out or "")
        if code != 0 or not found:
            add("%s 版本" % cli, "失败", (err or out or "未安装").strip()[:120])
        else:
            ok = version_tuple(found[0]) >= version_tuple(MIN_VERSIONS[cli])
            add("%s 版本" % cli, "通过" if ok else "失败", "%s（最低 %s）" % (found[0], MIN_VERSIONS[cli]))
    code, out, _ = run(["lark-cli", "doctor"], 60)
    try:
        doctor = json.loads(out)
        bad = [c for c in doctor.get("checks", []) if c.get("status") != "pass"]
        add("lark-cli doctor", "通过" if doctor.get("ok") else "失败",
            "；".join("%s=%s" % (c.get("name"), c.get("status")) for c in bad) or "全部通过")
    except ValueError:
        add("lark-cli doctor", "失败", "输出不是 JSON（退出码 %s）" % code)
    code, out, _ = run(["lark-cli", "whoami"])
    try:
        who = json.loads(out)
        ok = who.get("identity") == "user" and who.get("tokenStatus") == "ready"
        add("lark-cli 用户身份", "通过" if ok else "失败", "identity=%s tokenStatus=%s" % (who.get("identity"), who.get("tokenStatus")))
    except ValueError:
        add("lark-cli 用户身份", "失败", "whoami 输出不是 JSON（退出码 %s）" % code)
    code, _, _ = run(["meegle", "auth", "status"])
    add("meegle 登录", "通过" if code == 0 else "失败", "退出码 %s" % code)
    code, out, _ = run(["meegle", "project", "search", "--page-num", "1", "--format", "json"], 60)
    try:
        projects = (json.loads(out) or {}).get("projects") or []
        add("飞书项目权限", "通过" if projects else "失败", "能访问 %d 个空间" % len(projects))
    except ValueError:
        add("飞书项目权限", "失败", "project search 输出不是 JSON（退出码 %s）" % code)
    code, out, _ = run(["meegle", "telemetry", "status"])
    try:
        enabled = json.loads(out).get("enabled")
        add("meegle 诊断上报", "提醒" if enabled else "通过",
            "开着，请研究员决定是否关闭（meegle telemetry disable，本人执行）" if enabled else "已关闭")
    except ValueError:
        add("meegle 诊断上报", "跳过", "telemetry 子命令不可用（退出码 %s）" % code)
    code, out, _ = run(["lark-cli", "event", "consume", "im.message.receive_v1", "--as", "bot", "--dry-run"], 60)
    try:
        decision = (json.loads(out).get("data") or {}).get("decision") or {}
        bad = [c.get("name") for c in decision.get("preconditions", []) if c.get("status") != "ok"]
        ok = decision.get("status") == "ready" and not bad
        add("飞书私聊事件订阅", "通过" if ok else "提醒",
            "凭证、开发者后台的事件订阅、权限 im:message.p2p_msg:readonly 都就绪" if ok else
            "没就绪：%s；入口收不到飞书私聊，研究员转述也行" % ("、".join(bad) or decision.get("status") or "未知"))
    except (ValueError, AttributeError):
        add("飞书私聊事件订阅", "提醒", "event consume 的预览输出读不懂（退出码 %s）" % code)
    add("Python", "通过" if sys.version_info[:2] >= (3, 9) else "失败", sys.version.split()[0])
    for name in ("lark-doc", "lark-wiki", "meegle"):
        here = any(os.path.isdir(os.path.expanduser(os.path.join(b, name))) for b in ("~/.claude/skills", "~/.agents/skills"))
        add("技能 %s" % name, "通过" if here else "提醒", "已安装" if here else "没找到；书记员用 ledger.py，不依赖它，但人工排查时有用")
    width = max(len(r[0]) for r in rows)
    for item, status, detail in rows:
        print("%s  %s  %s" % (item.ljust(width), status, detail))
    failed = [r for r in rows if r[1] == "失败"]
    print("预检：%d 项失败。" % len(failed) if failed else "预检：没有失败项。")
    return 1 if failed else 0


# --------------------------------------------------------------- check
def check(target):
    mpath = os.path.join(target, ".claude", "agent-org", "manifest.json")
    if not os.path.isfile(mpath):
        print("没有 %s：这个目录还没初始化。" % mpath)
        return 2
    m = json.loads(read_text(mpath))
    problems = []
    if m.get("plugin_version") != plugin_version():
        problems.append("插件版本：项目是 %s，插件是 %s；重跑 init 预览差异" % (m.get("plugin_version"), plugin_version()))
    rules = read_text(os.path.join(orgspec.ORGSPEC_DIR, "rules.json"))
    if m.get("rules_sha256") != sha256(rules):
        problems.append("规则文件和项目里的副本不一致；重跑 init 预览差异")
    if m.get("terms_sha256") != sha256(read_text(os.path.join(orgspec.ORGSPEC_DIR, "terms.zh.json"))):
        problems.append("术语表和项目里的副本不一致；重跑 init 预览差异")
    task_path = os.path.join(target, "spec", "task.json")
    fresh = None
    if os.path.isfile(task_path) and m.get("files", {}).get("spec/task.json") not in (None, sha256(read_text(task_path))):
        problems.append("改过：spec/task.json（和 init 用的任务文件不同）。它跟着 init --task 的任务文件走：要改设置，改任务文件再重跑 init --apply；"
                        "有 spec/task.json.new 的，对照合并。改回来之前，先不比对插件更新")
    else:
        try:      # regenerate from the project's own task and sequences: catches script, template and ID changes
            fresh = build_plan(target, task_path, ",".join(m.get("sequences") or []) or None)
        except SetupError as e:
            problems.append("没法按现在的插件重新生成来比对：%s" % e)
    if fresh:
        stale = [rel for rel, (content, _, keep) in sorted(fresh.files.items())
                 if not keep and m.get("files", {}).get(rel) != sha256(content)]
        risk_reported = ".claude/hooks/lark-risk.json" in stale
        if risk_reported:                                   # follows the lark-cli on this machine, not the plugin
            stale.remove(".claude/hooks/lark-risk.json")
            table = (load_json_file(os.path.join(target, ".claude", "hooks", "lark-risk.json")) or {}).get("lark_cli_version")
            problems.append("lark-cli 风险表和本机的 lark-cli 对不上：项目里的表按 %s 生成，本机是 %s；重跑 init 会换成对应的表" % (
                table or "未知版本", installed_lark_version() or "没装 lark-cli"))
        table = (load_json_file(os.path.join(target, ".claude", "hooks", "lark-risk.json")) or {}).get("lark_cli_version")
        installed = installed_lark_version()
        if table and installed and installed != table and not risk_reported:
            print("提醒：本机 lark-cli 是 %s，项目的风险表按 %s 生成（插件自带的表）；新命令的风险等级可能缺失，未知的写命令一律按写处理" % (installed, table))
        if stale:
            problems.append("现在的插件、规则或 feishu/ids.json 生成的内容和初始化时不同（%d 个文件：%s）；重跑 init 预览差异" % (
                len(stale), "、".join(stale[:12]) + ("等" if len(stale) > 12 else "")))
    for rel, digest in sorted(m.get("files", {}).items()):
        path = os.path.join(target, rel)
        if not os.path.isfile(path):
            problems.append("缺文件：%s" % rel)
        elif rel in ("org/decisions.md", "org/phases.md") or rel.startswith("spec/"):
            continue                                        # the researcher's documents
        else:
            try:
                changed = sha256(read_text(path)) != digest
            except (UnicodeDecodeError, OSError):
                problems.append("读不了：%s（不是 UTF-8 编码？）" % rel)
                continue
            if changed:
                problems.append("改过：%s（和初始化时不同）" % rel)
    for rel in m.get("pending_new", []):
        if os.path.isfile(os.path.join(target, rel + ".new")):
            problems.append("待合并：%s.new" % rel)
    for p in problems:
        print("问题：" + p)
    print("检查：%s" % ("%d 个问题" % len(problems) if problems else "没有问题"))
    return 1 if problems else 0


# -------------------------------------------------------------- feishu
def normalise_ids(raw):
    """ids.json as this version writes it. Any other shape starts empty: bind again with --live."""
    if raw.get("schema") == "agent-org/ids-v1":
        return raw, []
    notes = ["feishu/ids.json 不是这一版的格式，按空间重新绑定；--apply 时原文件备份成 ids.legacy.json"] if raw else []
    return {"schema": "agent-org/ids-v1", "lark": {}, "meegle": {}, "wiki": {}}, notes


FIELD_TYPES = {"select": "单选", "multi_select": "多选", "text": "单行文本", "multi_text": "多行文本", "date": "日期",
               "link": "链接", "number": "数字", "bool": "开关", "user": "人员", "workitem_related_select": "关联工作项"}
# Field type names a space may report for each rule type. A name outside all of them is not judged.
# meta-fields reports 多行文本 as text, the same as 单行文本, so text passes for multi_text too.
FIELD_TYPE_ALIASES = {"select": {"select", "radio"}, "multi_select": {"multi_select", "multi-select", "checkbox"},
                      "text": {"text"}, "multi_text": {"multi_text", "multi-text", "multi_pure_text", "multi-pure-text", "text"},
                      "date": {"date"}, "link": {"link"}, "number": {"number"}, "bool": {"bool", "switch"},
                      "user": {"user"}, "workitem_related_select": {"workitem_related_select", "work_item_related_select"}}
KNOWN_FIELD_TYPES = set().union(*FIELD_TYPE_ALIASES.values())


def parse_maps(spec, items):
    """--map 类型标识=类型名 / 类型标识.字段标识=字段名: what the researcher pointed at by hand."""
    maps = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        key, value = key.strip(), value.strip()
        wid, _, fid = key.partition(".")
        ok = sep and value and any(w["id"] == wid for w in spec.work_items())
        if ok and fid:
            ok = any(f["id"] == fid for f in spec.fields_of(wid))
        if not ok:
            raise SetupError("--map 要写成 <类型标识>=<空间里的类型名> 或 <类型标识>.<字段标识>=<空间里的字段名>：%s" % item)
        maps[key] = value
    return maps


def template_space(task_feishu):
    """The space project spaces are copied from: feishu.template_space, else std_template."""
    return str((task_feishu or {}).get("template_space") or "").strip() or DEFAULT_TEMPLATE_SPACE


def bind(spec, ids, fd, maps, template=False):
    """Bind the rules' types and fields to this space: what --map names, else the key bound
    last time while it still exists, else the same name. Updates ids["meegle"] and returns the
    problems as [对象, 问题, 怎么办] rows. With template=True the space is checked as the
    template projects are copied from: its names must be the rules' own, so the advice is to
    rename rather than to point with --map."""
    m = ids.setdefault("meegle", {})
    space = (fd or {}).get("types") or {}
    types = m.setdefault("types", {})
    role = spec.term("role", spec.rules["feishu"]["owner_role"])
    problems = []
    for w in spec.work_items():
        wid, want = w["id"], spec.term("wi", w["id"])
        asked, kept = maps.get(wid), (types.get(wid) or {}).get("type_key")
        if asked:
            hits = [k for k, t in space.items() if asked in (k, t.get("name"), t.get("api_name"))]
        elif kept in space:
            hits = [kept]
        else:
            hits = [k for k, t in space.items() if t.get("name") == want]
        if len(hits) != 1:
            types.pop(wid, None)
            if hits:
                problems.append([want, "空间里有 %d 个“%s”类型" % (len(hits), asked or want),
                                 "同名的类型只留一个" if template else "用 `--map %s=<type_key>` 指明用哪一个" % wid])
            else:
                problems.append([want, "按“%s”找不到类型" % asked if asked else "空间里没有这个类型",
                                 "照搭建手册建这个类型；已经有、只是名字不同的，改名为“%s”" % want if template else
                                 "照下面“%s”一节建好；已经有、只是名字不同的，用 `--map %s=<空间里的类型名>` 指认" % (want, wid)])
            continue
        t = space[hits[0]]
        slot = {"name": t.get("name"), "type_key": hits[0], "api_name": t.get("api_name")}
        tpl = next((f for f in t.get("fields", []) if f.get("field_key") == "template"), None)
        if tpl and tpl.get("options"):
            slot["template_id"] = tpl["options"][0].get("id")
        types[wid] = slot
        if not any(r.get("role_name") == role for r in t.get("roles", [])):
            problems.append([want, "没有“%s”角色" % role, "在这个类型的角色里加上“%s”，每个状态的授权角色都设成它" % role])
        status = next((f for f in t.get("fields", []) if f.get("field_key") == "work_item_status"), None)
        have = [o.get("name") for o in (status or {}).get("options") or []]
        missing = [s for s in w["states"] if s not in have]
        if have and missing:                          # the status field lists the states of the flow
            problems.append([want, "缺状态：%s" % "、".join(missing),
                             "在这个类型的状态流里加上，或者把现有状态改成这些名字；现有：%s" % "、".join(have)])
    old, keys = m.get("field_keys") or {}, {}
    for wid, slot in types.items():
        custom = [f for f in (space.get(slot["type_key"]) or {}).get("fields", [])
                  if str(f.get("field_key") or "").startswith("field_")]
        by_key = {f["field_key"]: f for f in custom}
        for f in spec.fields_of(wid):
            fid, name = f["id"], spec.field_name(f["id"])
            label = "%s的“%s”" % (spec.term("wi", wid), name)
            asked, kept = maps.get("%s.%s" % (wid, fid)), (old.get(wid) or {}).get(fid)
            if asked:
                hits = [x for x in custom if asked in (x["field_key"], x.get("name"))]
            elif kept in by_key:
                hits = [by_key[kept]]
            else:
                hits = [x for x in custom if x.get("name") == name]
            if not hits:
                need = "建一个%s字段，名字写“%s”%s" % (FIELD_TYPES.get(f.get("type"), f.get("type") or ""), name,
                                                    "，选项：%s" % "、".join(f["options"]) if f.get("options") else "")
                problems.append([label, "按“%s”找不到字段" % asked if asked else "空间里没有",
                                 need + ("；已经有、只是名字不同的，改成这个名字" if template else
                                         "；已经有、只是名字不同的，用 `--map %s.%s=<空间里的字段名>` 指认" % (wid, fid))])
                continue
            if len(hits) > 1:
                problems.append([label, "有 %d 个同名字段（%s）" % (len(hits), "、".join(x["field_key"] for x in hits)),
                                 "同名的字段只留一个" if template else "用 `--map %s.%s=<field_key>` 指明用哪一个" % (wid, fid)])
                continue
            x = hits[0]
            got = x.get("field_type")
            if got in KNOWN_FIELD_TYPES and got not in FIELD_TYPE_ALIASES.get(f.get("type"), {got}):
                problems.append([label, "空间里的类型是 %s，插件要的是%s" % (got, FIELD_TYPES.get(f.get("type"), f.get("type"))),
                                 "改成对的类型，或者另建一个字段再指认"])
                continue
            keys.setdefault(wid, {})[fid] = x["field_key"]
            have = [o.get("name") for o in x.get("options") or []]
            missing = [o for o in f.get("options") or [] if o not in have]
            if have and missing:
                problems.append([label, "缺选项：%s" % "、".join(missing), "在空间里加上，或者把现有选项改成这些名字；现有：%s" % "、".join(have)])
    m["field_keys"] = keys
    return problems


def binding_table(spec, ids, label="已绑定的字段"):
    m = ids.get("meegle") or {}
    types, keys = m.get("types") or {}, m.get("field_keys") or {}
    rows = []
    for w in spec.work_items():
        t = types.get(w["id"])
        rows.append([spec.term("wi", w["id"]), "%s（`%s`）" % (t.get("name"), t.get("type_key")) if t else "没绑定",
                     "%d / %d" % (len(keys.get(w["id"]) or {}), len(spec.fields_of(w["id"]))) if t else "—"])
    return orgspec.md_table(["类型", "空间里的类型", label], rows)


def structure_md(spec, ids):
    """每种类型要有的东西: the states, transitions, forms, role and fields each type needs."""
    keys = (ids.get("meegle") or {}).get("field_keys") or {}
    role = spec.term("role", spec.rules["feishu"]["owner_role"])
    lines = ["## 每种类型要有的东西", "",
             "照下面逐项建或核对。每种类型都用状态流，列在最前面的状态是初始状态，每个状态的授权角色都设成“%s”。序列、发出方这些几种类型都有的字段，可以在一个类型上建好，再用“复制至其他工作项”加到别的类型上。" % role, ""]
    for w in spec.work_items():
        wid = w["id"]
        forms = "；".join("%s→%s：%s" % (f["from"], f["to"], "、".join(spec.field_name(x) for x in f["required"]))
                         for f in w.get("forms", [])) or "无"
        lines += ["### %s（标识 `%s`）" % (spec.term("wi", wid), wid), "",
                  bullets(["状态：%s（初始状态：%s）" % ("、".join(w["states"]), w["states"][0]),
                           "流转：%s" % "；".join("%s→%s" % (a, b) for a, b in w["transitions"]),
                           "流转表单必填：%s" % forms,
                           "角色：%s" % role]), ""]
        rows = []
        for f in spec.fields_of(wid):
            options = "、".join(f.get("options") or [])
            if f.get("target"):
                options = "指向“%s”" % spec.term("wi", f["target"])
            rows.append([spec.field_name(f["id"]), "`%s`" % f["id"], FIELD_TYPES.get(f.get("type"), f.get("type") or ""),
                         options, (keys.get(wid) or {}).get(f["id"]) or "没绑定", f.get("note", "")])
        lines += [orgspec.md_table(["字段", "标识", "类型", "选项", "空间里的 field_key", "说明"], rows), ""]
    return "\n".join(lines)


def migration_md(spec, ids, fd, found_views=None, problems=None):
    m = ids.get("meegle") or {}
    lines = ["# 建空间与核对清单", "",
             "> 由 `setup.py feishu` 生成。项目空间从模板空间（默认叫 %s）用“分享空间配置”复制：类型、字段、流程、表单、角色和详情页跟着复制过来，条件视图和空间置顶不复制，每个项目空间各建一次。这里缺的结构先在模板里补上，以后复制的空间就都有；本空间也照样补一遍，已经复制出来的空间不会跟着模板变。补好以后重跑 `setup.py feishu --live --apply`。知识库节点由书记员用 `ledger.py ensure-node` 建。" % DEFAULT_TEMPLATE_SPACE,
             "> 空间里已经有、只是名字和这里不同的，不必改名：用 `--map <类型标识>=<空间里的类型名>` 或 `--map <类型标识>.<字段标识>=<空间里的字段名>` 指认。绑定以后脚本按键找，空间里改名也不会断。", ""]
    lines += ["## 绑定结果", "", binding_table(spec, ids), ""]
    lines += ["## 要处理的问题", ""]
    if problems is None:
        lines += ["还没有字段字典，没法核对：先跑 `setup.py feishu --live`。", ""]
    else:
        lines += [orgspec.md_table(["对象", "问题", "怎么办"], problems) if problems else "无。", ""]
    lines += [structure_md(spec, ids)]
    rows = []
    for v in spec.rules["feishu"]["views"]:
        found = None if found_views is None else found_views.get(v["name"])
        pinned = bool(v.get("pinned"))
        if found is True and not pinned:
            continue
        how = "条件：%s" % v["where"]
        if v.get("order"):
            how += "；排序：%s" % v["order"]
        if v.get("columns"):
            how += "；显示列：%s" % v["columns"]
        if v.get("mode"):
            how += "；%s" % v["mode"]
        if found is True:
            how = "确认已放进空间置顶、显示列已保存"
        rows.append([v["name"], spec.term("wi", v["on"]), {True: "已有", False: "没有", None: "待核对"}[found], how,
                     "放进空间置顶" if pinned else ""])
    lines += ["## 条件视图", "", orgspec.md_table(["视图", "类型", "现状", "怎么建", "置顶"], rows), "",
              "条件视图只能在网页上建。新建视图的对话框只能放进“个人”，要放进空间置顶，在左侧“空间置顶”的“+”里选“添加现有”。显示列在视图右上角“…”→“设置视图展示”里改，改完点“存在本地变更”里的“保存”，才对所有人生效。脚本只能查到视图在不在，查不到它是否置顶。", ""]
    rows = []
    nodes = (ids.get("wiki") or {}).get("nodes") or {}
    tree = spec.rules["docs_tree"]
    seqs = [s for s, _, _ in spec.sequences()]
    renames = False
    for key in list(tree["nodes"]) + ["%s/%s" % (n, s) for n in tree["per_sequence"] for s in seqs]:
        head, _, seq = key.partition("/")
        title = spec.term("node", head) + ("/" + spec.term("seq", seq) if seq else "")
        n = nodes.get(key)
        if not n:
            rows.append([title, "没有", "书记员建节点：`ledger.py ensure-node %s`" % key])
        elif seq and n.get("title", "").split("/")[-1] != spec.term("seq", seq):
            renames = True
            rows.append([title, "现名“%s”" % n.get("title"), "改名为“%s”" % spec.term("seq", seq)])
        elif not seq and n.get("title", "").split("/")[0] != spec.term("node", head):
            renames = True
            rows.append([title, "现名“%s”" % n.get("title"), "改名为“%s”" % spec.term("node", head)])
    lines += ["## 知识库节点", "", orgspec.md_table(["节点", "现状", "要做的"], rows) if rows else "无差异。", ""]
    if renames:
        lines += ["命令行没有给知识库节点改名的子命令；节点标题跟着节点文档的一级标题走。先读一遍节点文档：是空的，书记员可以写入只有新标题的正文：`lark-cli docs +update --doc <节点链接> --command overwrite --doc-format markdown --content - --as user`，正文用 heredoc 传入一行 `# <新名>`；有内容的，在网页上改名，不要覆盖。", ""]
    lark = ids.get("lark") or {}
    wiki = ids.get("wiki") or {}
    readback = [
        ["project_key", "有" if m.get("project_key") else "缺", "跑 `setup.py feishu --live --apply`：按 `spec/task.json` 里的空间名精确查找"],
        ["simple_name", "有" if m.get("simple_name") else "缺", "同上；工作项链接要用它"],
        ["研究员的 open_id", "有" if lark.get("researcher_open_id") else "缺", "同上：从 `lark-cli whoami` 读；没有它，飞书私聊监听不启动"],
        ["知识空间 space_id", "有" if wiki.get("space_id") else "缺", "先在已有的知识空间里找（名字可能不一样），确认没有再建：研究员确认后由 setup 技能执行，或者研究员自己执行 `lark-cli wiki +space-create --name \"<知识空间名>\" --description \"<一句话>\" --as user`，再重跑 `setup.py feishu --live --apply`"],
    ]
    lines += ["## 读回", "", orgspec.md_table(["项", "现状", "怎么补"], readback), ""]
    lines += ["## 网页上还要做的事", "",
              "下面几项脚本核对不了，在网页上做（可以由会话代点），做完自己勾掉：", "",
              bullets(["六种类型的详情页布局：默认“基本信息”里只有“名称”，把主文档、来源工作项、发出方、负责会话、截止时间等字段放进去，人在页面上才看得到。在模板空间里做，复制时一起带过来。",
                       "项目详情页加一个标签页，名为“%s”，放一个“工作项视图”控件，列出这个项目下的%s。也在模板空间里做。" % (spec.term("wi", "same_seq"), spec.term("wi", "same_seq")),
                       "知识库里给人看的参考（可选）：五份包模板（源文件在 `.claude/agent-org/templates/`）和字段字典（`feishu/field-dictionary.md`）。插件只读本地副本，放进知识库只是方便人翻看。"]), ""]
    return "\n".join(lines)


def cli_json(cmd, timeout=60):
    code, out, err = run(cmd, timeout)
    if code != 0:
        raise SetupError("%s 失败（退出码 %s）：%s" % (" ".join(cmd[:4]), code, (err or out or "")[-300:]))
    try:
        return json.loads(out)
    except ValueError:
        raise SetupError("%s 的输出不是 JSON" % " ".join(cmd[:4]))


def find_project(data, wanted, fix=None):
    """The one project in `meegle project search` whose name, simple_name or project_key equals wanted."""
    projects = data.get("projects") if isinstance(data, dict) else data
    if not isinstance(projects, list):
        projects = [d for d in ledger._walk(data) if isinstance(d, dict) and d.get("project_key")]
    projects = [p for p in projects if isinstance(p, dict)]
    hits = [p for p in projects or [] if wanted in (p.get("name"), p.get("simple_name"), p.get("project_key"))]
    if len(hits) != 1:
        cands = "；".join("%s（simple_name %s，project_key %s）" % (p.get("name"), p.get("simple_name"), p.get("project_key"))
                          for p in projects or []) or "无"
        raise SetupError("按“%s”找到 %d 个飞书项目空间，要正好一个。候选：%s。%s" % (wanted, len(hits), cands, fix or (
            "把准确的 simple_name 或 project_key 写进 init --task 用的任务文件的 feishu.meegle_space，重跑 init --apply"
            "（spec/task.json 跟着更新；手改过它的，init 会写成 spec/task.json.new，对照合并），再重跑这条命令")))
    return hits[0]


def export_dictionary(project_key, space_name, export_dir):
    """Export a space's field dictionary into export_dir and load it."""
    code, out, err = run([sys.executable, "-I", os.path.join(HERE, "export_field_dictionary.py"), "--project-key",
                          project_key, "--out-dir", export_dir, "--space-name", space_name or ""], 300)
    if code != 0:
        raise SetupError("导出字段字典失败（退出码 %s）：%s" % (code, (err or out or "")[-500:]))
    try:
        return json.loads(read_text(os.path.join(export_dir, "field-dictionary.json")))
    except (OSError, ValueError) as e:
        raise SetupError("读不了刚导出的字段字典：%s" % e)


def optional_read(what, fn):
    """Run one optional read-back step; a failure becomes a reminder, not an abort."""
    try:
        return fn()
    except SetupError as e:
        print("提醒：没能读回%s：%s" % (what, e))
        return None


def read_back(spec, ids, task_feishu, export_dir, maps):
    """Find the project's space, export its field dictionary, bind the rules to it, and read
    open_id, space_id, node titles and views. Returns (field dictionary, {view: True/False/None},
    binding problems)."""
    m = ids["meegle"]
    named = str(task_feishu.get("meegle_space") or "").strip()
    moved = named and m.get("project_key") and named not in [m.get(k) for k in ("project_key", "simple_name", "space_name")]
    if not m.get("project_key") or not m.get("simple_name") or moved:
        wanted = named if moved or not m.get("project_key") else m["project_key"]
        if not wanted:
            raise SetupError("ids.json 里没有 project_key，研究任务里也没写飞书项目空间：把空间名写进 init --task 用的任务文件的 feishu.meegle_space，重跑 init --apply，再重跑这条命令")
        hit = find_project(cli_json(["meegle", "project", "search", "--project-key", wanted, "--format", "json"]), wanted)
        if m.get("project_key") and hit.get("project_key") != m["project_key"]:
            print("提醒：任务文件里的空间“%s”不是上次绑定的“%s”，按新空间重新绑定，原来的绑定作废" % (
                wanted, m.get("space_name") or m["project_key"]))
            for k in ("field_keys", "views_found", "bound_at"):
                m.pop(k, None)
            m["types"] = {}
        m.update({"project_key": hit.get("project_key"), "simple_name": hit.get("simple_name"),
                  "space_name": hit.get("name") or m.get("space_name")})
    fd = export_dictionary(m["project_key"], m.get("space_name"), export_dir)
    problems = bind(spec, ids, fd, maps)
    lark = ids.setdefault("lark", {})
    if not lark.get("researcher_open_id"):
        who = optional_read("研究员的 open_id", lambda: cli_json(["lark-cli", "whoami"]))
        who = who if isinstance(who, dict) else {}
        on_behalf = who.get("onBehalfOf") if isinstance(who.get("onBehalfOf"), dict) else {}
        oid = on_behalf.get("openId") if who.get("identity") == "user" else None
        if oid:
            lark["researcher_open_id"] = oid
        else:
            print("提醒：lark-cli whoami 里没有用户身份的 open_id；飞书私聊监听不会启动")
    wiki = ids.setdefault("wiki", {})
    want_wiki = task_feishu.get("wiki_space")
    if want_wiki and (not wiki.get("space_id") or wiki.get("space_name", want_wiki) != want_wiki):
        data = optional_read("知识空间", lambda: cli_json(["lark-cli", "wiki", "+space-list", "--as", "user"]))
        hits = [d for d in ledger._walk(data or {}) if d.get("name") == want_wiki and d.get("space_id")]
        if len(hits) == 1:
            if wiki.get("space_id") and hits[0]["space_id"] != wiki["space_id"]:
                print("提醒：知识空间换成了“%s”，原来记下的节点作废，书记员在新空间里重建" % want_wiki)
                wiki.pop("nodes", None)
            wiki.update({"space_id": hits[0]["space_id"], "space_name": hits[0]["name"]})
        elif data is not None and not hits:
            print("提醒：知识空间列表里没有叫“%s”的空间（只读了第一页）。先在网页上找一找，确认没有再建。" % task_feishu["wiki_space"])
        elif len(hits) > 1:
            print("提醒：有 %d 个知识空间叫“%s”，没有自动选；把 space_id 手工写进 feishu/ids.json" % (len(hits), task_feishu["wiki_space"]))
    for key, n in (wiki.get("nodes") or {}).items():
        if n.get("node_token"):
            got = optional_read("节点 %s 的标题" % key,
                                lambda tok=n["node_token"]: cli_json(["lark-cli", "wiki", "+node-get", "--node-token", tok, "--as", "user"]))
            title = ledger._find(got or {}, ("title",))
            if title:
                n["title"] = title
    found = {}
    for v in spec.rules["feishu"]["views"]:
        tkey = (m["types"].get(v["on"]) or {}).get("type_key")
        if not tkey:
            found[v["name"]] = None
            continue
        ok, data = True, None
        try:
            data = cli_json(["meegle", "view", "search", "--project-key", m["project_key"], "--view-scope", tkey,
                             "--key-word", v["name"], "--format", "json"])
        except SetupError as e:
            ok = False
            print("提醒：没能读回视图“%s”：%s" % (v["name"], e))
        views = [d for d in ledger._walk(data) if d.get("view_id") or d.get("id")] if ok and data else []
        if not ok:
            found[v["name"]] = None
        elif data in (None, [], {}) or views:
            found[v["name"]] = any((d.get("view_name") or d.get("name")) == v["name"] for d in views)
        else:
            found[v["name"]] = None
            print("提醒：视图“%s”的查询结果读不懂，标成待核对" % v["name"])
    return fd, found, problems


def feishu(target, maps, live, apply):
    spec = orgspec.OrgSpec()
    task = (load_json_file(os.path.join(target, "spec", "task.json")) or {}).get("feishu") or {}
    maps = parse_maps(spec, maps)
    fd_dir = os.path.join(target, "feishu")
    ids_path = os.path.join(fd_dir, "ids.json")
    raw = load_json_file(ids_path) or {}
    ids, notes = normalise_ids(raw)
    fd = load_json_file(os.path.join(fd_dir, "field-dictionary.json"))
    found_views, export_dir, problems = None, None, None
    if live:
        if apply:
            os.makedirs(fd_dir, exist_ok=True)
        export_dir = tempfile.mkdtemp(prefix=".export-", dir=fd_dir if apply else None)
        try:
            fd, found_views, problems = read_back(spec, ids, task, export_dir, maps)
        except SetupError:
            shutil.rmtree(export_dir, ignore_errors=True)
            raise
        except Exception as e:                       # an output shape nobody expected
            shutil.rmtree(export_dir, ignore_errors=True)
            raise SetupError("读回时遇到没见过的输出（%s: %s）；什么都没写" % (type(e).__name__, e))
        ids["meegle"]["views_found"] = found_views
    else:
        moved = ledger.other_space({"feishu": task}, ids.get("meegle") or {})
        if moved:
            raise SetupError("任务文件里的飞书项目空间是“%s”，上次绑定的是“%s”：加 --live 从空间读一遍再绑定" % (
                moved, ids["meegle"]["space_name"]))
        if fd is not None:
            problems = bind(spec, ids, fd, maps)
        elif maps:
            raise SetupError("还没有字段字典，--map 没法核对：先跑 setup.py feishu --live")
        found_views = (ids.get("meegle") or {}).get("views_found")
    blank = fd is not None and not (ids.get("meegle") or {}).get("types")
    if apply and fd is not None:
        if blank:                                    # nothing bound: the ledger keeps refusing
            ids["meegle"].pop("bound_at", None)
        else:
            ids["meegle"]["bound_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    for n in notes:
        print("提醒：" + n)
    files = {ids_path: dump_json(ids), os.path.join(fd_dir, "migration.md"): migration_md(spec, ids, fd, found_views, problems)}
    for p, content in files.items():
        current = read_text(p) if os.path.isfile(p) else None
        state = "相同" if current == content else ("新建" if current is None else "更新")
        print("[%s] %s" % (state, os.path.relpath(p, target)))
    if export_dir:
        print("[%s] feishu/field-dictionary.json、field-dictionary.md（刚从空间导出）" % ("写入" if apply else "不写"))
    if problems is None:
        print("还没有字段字典，绑定不了：加 --live 从空间读。")
    else:
        print("绑定：%s" % ("没有问题" if not problems else "%d 个问题，见 feishu/migration.md 的“要处理的问题”" % len(problems)))
    if blank:
        print("空间“%s”里没有一种类型对得上插件的规则。项目空间要从模板空间“%s”复制：先用 `setup.py template --live` 核对模板，"
              "再用模板的“分享空间配置”复制出项目空间，步骤见 setup 技能的搭建手册（references/build-space.md）；"
              "企业里还没有模板的，先照手册搭模板。空间里其实有这些类型、只是名字不同的，用 --map 指认。" % (
                  (ids.get("meegle") or {}).get("space_name") or task.get("meegle_space") or "", template_space(task)))
    if not apply:
        if export_dir:
            shutil.rmtree(export_dir, ignore_errors=True)
        print("这是预览。核对绑定结果后加 --apply 写入；台账脚本在绑定写入以后才能用。")
        if not blank:                                # a blank space gets the hint, not the whole list
            print()
            print(files[os.path.join(fd_dir, "migration.md")])
        return 0
    os.makedirs(fd_dir, exist_ok=True)
    if os.path.isfile(ids_path) and raw.get("schema") != "agent-org/ids-v1":
        backup = os.path.join(fd_dir, "ids.legacy.json")
        if not os.path.exists(backup):
            shutil.copyfile(ids_path, backup)
    if export_dir:
        for name in ("field-dictionary.json", "field-dictionary.md"):
            if os.path.isfile(os.path.join(export_dir, name)):
                os.replace(os.path.join(export_dir, name), os.path.join(fd_dir, name))
        shutil.rmtree(export_dir, ignore_errors=True)
    for p, content in files.items():
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
    print("已写入 feishu/ids.json 和 feishu/migration.md。")
    if blank:
        print("一种类型都没绑上：这次只记下空间和 open_id，没写绑定时间，台账脚本仍然不读写飞书。")
    return 0


def check_template(target, space):
    """Check a space as the template project spaces are copied from: the rules' names, and no
    work items, since the template is only a copy source. Reads Feishu, writes nothing."""
    spec = orgspec.OrgSpec()
    if not space:
        task = (load_json_file(os.path.join(target, "spec", "task.json")) or {}).get("feishu") or {} if target else {}
        space = template_space(task)
    hit = find_project(cli_json(["meegle", "project", "search", "--project-key", space, "--format", "json"]), space,
                       "把模板空间的准确名字、simple_name 或 project_key 用 --space 传进来，或者写进任务文件的 feishu.template_space")
    export_dir = tempfile.mkdtemp(prefix="agent-org-template-")
    try:
        fd = export_dictionary(hit.get("project_key"), hit.get("name"), export_dir)
    finally:
        shutil.rmtree(export_dir, ignore_errors=True)
    ids = {"meegle": {}}
    problems = bind(spec, ids, fd, {}, template=True)
    held = []
    for wid, slot in ids["meegle"]["types"].items():
        mql = "SELECT `work_item_id` FROM `%s`.`%s` LIMIT 1" % (hit.get("project_key"), slot["type_key"])
        try:
            rows = ledger.query_rows(cli_json(["meegle", "workitem", "query", "--project-key", hit.get("project_key"),
                                               "--mql", mql, "--format", "json"]))
        except (SetupError, ledger.LedgerError) as e:
            print("提醒：没能查%s里有没有工作项：%s" % (spec.term("wi", wid), e))
            continue
        if rows:
            held.append(spec.term("wi", wid))
    if held:
        problems.append(["模板空间", "有工作项：%s" % "、".join(held),
                         "模板只当复制源，不放工作项、不跑项目：工作项放到项目空间里，模板里的请研究员在网页上删掉"])
    print("模板空间“%s”（simple_name %s，project_key %s）。只读了飞书，没写任何文件。" % (
        hit.get("name"), hit.get("simple_name"), hit.get("project_key")))
    print()
    print(binding_table(spec, ids, "对上的字段"))
    print()
    if problems:
        print("## 要处理的问题")
        print()
        print(orgspec.md_table(["对象", "问题", "怎么办"], problems))
        print()
        print(structure_md(spec, ids))
    print("流转、流转表单和详情页脚本读不到，照 setup 技能的搭建手册（references/build-space.md）在网页上核对。")
    if problems:
        print("还有 %d 个问题，补好以后再跑一次 setup.py template --live。" % len(problems))
        return 1
    print("可以当模板：项目空间从这里复制以后，绑定不用 --map。")
    return 0


def load_json_file(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        return json.loads(read_text(path))
    except ValueError:
        raise SetupError("%s 不是合法 JSON" % path)


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="初始化智能体组织的项目目录")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("precheck")
    p = sub.add_parser("init")
    p.add_argument("--target", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--sequences")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("feishu")
    p.add_argument("--target", required=True)
    p.add_argument("--map", action="append", default=[],
                   help="指认空间里名字不同的类型或字段：<类型标识>=<类型名> 或 <类型标识>.<字段标识>=<字段名或 field_key>，可重复")
    p.add_argument("--live", action="store_true")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("template")
    p.add_argument("--space", help="要核对的模板空间：名字、simple_name 或 project_key；不写就用任务文件的 feishu.template_space，再没有就用 std_template")
    p.add_argument("--target", help="项目目录：从它的 spec/task.json 读 feishu.template_space")
    p.add_argument("--live", action="store_true", help="这条命令总是只读飞书，写不写都一样；和 feishu --live 的写法一致")
    p = sub.add_parser("check")
    p.add_argument("--target", required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "precheck":
            return precheck()
        if args.cmd == "init":
            target = os.path.abspath(args.target)
            plan = build_plan(target, args.task, args.sequences)
            acts = actions_for(target, plan)
            report(target, plan, acts, args.apply)
            if args.apply:
                apply_plan(target, plan, acts)
            return 0
        if args.cmd == "feishu":
            return feishu(os.path.abspath(args.target), args.map, args.live, args.apply)
        if args.cmd == "template":
            return check_template(os.path.abspath(args.target) if args.target else None, args.space)
        if args.cmd == "check":
            return check(os.path.abspath(args.target))
    except SetupError as e:
        print("出错：%s" % e)
        return 2
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
