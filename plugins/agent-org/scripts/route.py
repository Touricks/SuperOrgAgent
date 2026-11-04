#!/usr/bin/env python3
"""Mechanical part of the entry's routing, plus inboxes, focus sessions and the session pool.

    route.py classify --text '<message>'      rows R1/R3 by work item ID or session name; else needs_router
    route.py classify --draft <path>          rows R5/R6/R7 for a package handed back to the entry
    route.py notify <role or session name> --text T [--work-item ID]
    route.py focus open [<role or session>] --topic T | focus close [<role or session>] | focus list
    route.py listen                           Feishu DM listener: appends to inbox/feishu.jsonl

Pool mode (the task file says "mode": "pool"; see pool.py):
    route.py claim <role> [--as NAME] [--desk ID]    claim the role a permit names (the hooks confirm it)
    route.py expect NAME --role R (--desk ID | --session ID)   pre-register a session you opened
    route.py release NAME                     the researcher frees a claim (it becomes a tombstone)
    route.py pool [--role R] [--item ID] [--queue NAME] [--recover] [--watch [--once]]
    route.py refill <role> [--count N]        ask for more sessions of a role; the entry's daily queue
    route.py audit                            the soft-rule hits, stale work and claim anomalies

classify walks the routing table in orgspec/rules.json and stops at the first row
it can decide without judgement. Matching a message to work by the work item's
name, fields and 状态包 (R2) is left to the router agent: the script answers
needs_router instead of guessing. --offline skips the ledger lookups.
Python 3.9 standard library only.
"""
import argparse
import contextlib
import datetime
import fcntl
import io
import json
import os
import re
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import ledger  # noqa: E402
import pool  # noqa: E402

ID_RE = re.compile(r"#(\d{3,})|(?<![\d-])(\d{9,12})(?!\d)")
SESSION_RE = re.compile(r"(?<![\w-])([A-Za-z]+-[pP][0-2]-\d+)(?![\w-])")
CLAIM_WAIT = 60


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def role_of_session(spec, session):
    m = re.match(r"([A-Za-z]+)-[pP]([0-2])-", session)
    if not m:
        return None
    seq = next((s for s, _, a in spec.sequences() if a.lower() == m.group(1).lower()), None)
    if not seq:
        return None
    rank = int(m.group(2))
    if seq == "pmo" and rank < 2:
        return "pmo-router" if rank == 1 else "pmo-scribe"
    return "%s-p%d" % (seq, rank)


def focus_state(root):
    if os.path.isfile(os.path.join(root, ".agent-org", "manifest.json")):
        import codex_runtime
        return {b["role"]: b["focus"] for b in codex_runtime.Pool(root).load()["bindings"].values() if b.get("focus")}
    return load_json(os.path.join(root, "inbox", "focus.json"), {})


def focus_for_sequence(root, seq):
    for role, info in focus_state(root).items():
        if role.startswith(seq + "-p2"):
            return role
    return None


# ------------------------------------------------------------- classify
def classify_text(spec, root, text, offline):
    rows = {r["id"]: r for r in spec.rules["routing"]}
    ids = [a or b for a, b in ID_RE.findall(text)]
    sessions = SESSION_RE.findall(text)
    for s in sessions:
        m = re.match(r"[A-Za-z]+-[pP]0-(\d{3,})$", s)        # Alg-p0-2101 names work item 2101; Alg-p0-01 is a pool slot
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    if ids or sessions:
        lg = None if offline else ledger.Ledger(root, "preview")
        for item_id in ids:
            if lg is None:
                return result(spec, "R1", "带工作项 ID #%s（离线，没核对是否存在）" % item_id, work_item=item_id)
            try:
                it = lg.item(item_id)
            except ledger.LedgerError as e:
                return result(spec, "R3", "#%s 查不到：%s" % (item_id, str(e)[:120]), work_item=item_id)
            if it["state"] in ledger.closed_states(spec):
                return result(spec, "R3", "#%s 已是“%s”，不能直接续上" % (item_id, it["state"]), work_item=item_id)
            seq_cn = lg.text_of(it, "sequence")
            seq = next((s for s, cn, _ in spec.sequences() if cn == seq_cn), None)
            owner = lg.text_of(it, "owner_session")
            focus = focus_for_sequence(root, seq) if seq else None
            return result(spec, "R1", "#%s 存在，状态“%s”" % (item_id, it["state"]), work_item=item_id,
                          session=owner, focus=focus, pool=pool_info(root, owner, item_id))
        for s in sessions:
            role = role_of_session(spec, s)
            if role:
                return result(spec, "R1", "带会话名 %s" % s, session=s, role=role,
                              focus=role if role in focus_state(root) else None, pool=pool_info(root, s))
    out = result(spec, None, "没带工作项 ID 或会话名；要按工作项名称、字段和状态包匹配（R2/R3），或判断是不是新任务（R4）、要人拍板（R5）")
    out["needs_router"] = True
    out["candidates"] = [rows[k]["id"] for k in ("R2", "R3", "R4", "R5") if k in rows]
    return out


def classify_draft(spec, root, path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    errors, _ = orgspec.lint_text(spec, text)
    if errors:
        return {"row": None, "reason": "草稿没通过检查", "errors": errors, "action": "退回作者修改"}
    d = ledger.Draft(spec, path)
    to_seq = d.receiver_seq
    pooled = (load_policy(root) or {}).get("mode") == "pool"
    notice = spec.term("msg", "notice")
    if d.pid == "decision":
        if d.header.get("渠道") == spec.term("channel", "researcher"):
            tell = ("给“受影响的工作项”的负责会话发消息【%s #ID】；显示“研究员在用”的改用 route.py notify 留言" % notice
                    if pooled else "通知“受影响的工作项”的负责会话（route.py notify）")
            return result(spec, "R6", "研究员主动发起的决策包", next_hop="pmo-scribe",
                          steps=["书记员 register 后流转到“已决定”，表单填决定内容和决定人", tell])
        return result(spec, "R5", "决策队列来的决策包", next_hop="pmo-scribe",
                      steps=["书记员 register，建成“待决定”", "紧急程度为“插队”的马上交研究员，其余进每日队列"])
    route = next((r for r in spec.rules["package_routes"] if r["pkg"] == d.pid and (r["rel"] or None) == d.rel), None)
    steps, hop = [], None
    if d.pid == "request" and d.rel == "cross":
        hop = "%s-p2" % to_seq if to_seq else None
        steps = ["书记员 register，建跨序列需求（待接单）", "派 %s 接单：复述需求、承接范围、预计交付时间" % hop]
    elif d.pid == "delivery" and d.rel == "cross":
        hop = "%s-p2" % to_seq if to_seq else None
        steps = ["确认交付物涉及的结论已有验收包", "书记员 reply，并流转跨序列需求到“已交付”",
                 ("给 %s 的会话发消息【%s #ID】，写交付包路径" % (hop, notice)) if pooled else "恢复 %s 接收交付" % hop]
    elif d.pid == "review":
        hop = "quality-p2"
        steps = ["书记员 register，建送审（待核查）；被送审的工作项流转到“待验收”", "派 quality-p2 核查"]
    elif d.pid == "verdict":
        hop = "pmo-scribe"
        steps = ["书记员 reply，按它打印的两条流转命令改状态",
                 ("给送审方（发【%s】给你的那个会话）发消息【%s #被送审工作项 ID】，写验收包路径；送审方是 +1、+0 的，"
                  "再用 route.py notify 给它所在序列的 +2 留一句" % (spec.term("msg", "review"), notice))
                 if pooled else "恢复送审方所在序列的 +2，交验收包路径"]
    else:
        return result(spec, None, "序列内的%s不经过入口，交回本序列上一级" % spec.pkg(d.pid), next_hop=None)
    if hop and hop.endswith("-p2") and to_seq and focus_for_sequence(root, to_seq):
        steps.append("%s 正开着聚焦会话：用 route.py notify 留言，不另派" % hop)
    return result(spec, "R7", "%s（%s → %s）" % (spec.pkg(d.pid) + ("（%s）" % spec.term("rel", d.rel) if d.rel else ""),
                                              route["from"] if route else "?", route["to"] if route else "?"),
                  next_hop=hop, steps=steps)


def result(spec, row, reason, **extra):
    out = {"row": row, "reason": reason}
    if row:
        r = next(x for x in spec.rules["routing"] if x["id"] == row)
        out.update({"when": r["when"], "action": r["action"], "detail": spec.render(r["detail"])})
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def pool_info(root, session=None, item_id=None):
    """Pool mode: where the owner session stands (desktop ID, status, the researcher using it). A +0
    work name such as the ledger's 负责会话 (Alg-p0-2101) stands for the claim that owns the item."""
    if os.path.isfile(os.path.join(root, ".agent-org", "manifest.json")):
        import codex_runtime
        state = codex_runtime.Pool(root).load()
        b = state["bindings"].get(session)
        if not b:
            return {"session": session, "claimed": False}
        return {"session": session, "claimed": True, "thread_id": b["thread_id"], "host_id": b["host_id"],
                "status": state["sessions"].get(b["thread_id"], {}).get("status", "unknown"),
                "researcher_using": bool(b.get("focus"))}
    policy = load_policy(root)
    if not policy or policy.get("mode") != "pool":
        return None
    pl, ok = pool.load(root)
    if not ok:
        return None
    name = pool.resolve(policy, pl, session) if session else None
    if (not name or name not in pl["claims"]) and item_id:
        name = (pl.get("owners") or {}).get(str(item_id)) or name
    row = next((r for r in pool.session_view(root, policy, pl) if r["name"] == name), None)
    if not row:
        return {"session": name, "claimed": False}
    return {"session": name, "claimed": True, "desk": row["desk"], "status": row["status"],
            "researcher_using": bool(row["focus"])}


# ------------------------------------------------------- inbox & focus
def append_inbox(root, role, msg):
    path = os.path.join(root, "inbox", role + ".jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def notify_target(spec, root, policy, role):
    """The inbox a notify goes to. CLI: a role (a session name counts as its role). Pool: a session;
    a role only when it can have one session, or has exactly one claim; a +0 work name goes to the
    claim that owns the work item."""
    if role == "feishu":
        raise SystemExit("inbox/feishu.jsonl 只由飞书私聊监听（route.py listen）写，notify 不写它")
    roles = agent_roles(spec)
    if policy.get("mode") != "pool":
        found = role if role in roles else (pool.role_of(policy, role) if policy.get("agents") else None)
        if not found:
            raise SystemExit("没有角色 %s" % role)
        if policy.get("agents") and (policy["agents"].get(found) or {}).get("rank") != 2:
            raise SystemExit("CLI 模式下 %s 是子智能体，不读收件箱：新信息交给派它的上一级，或写进包里" % found)
        return found
    pl, ok = pool.load(root)
    if role in roles:
        if pool.role_limit(policy, role) == 1:
            return role
        held = sorted(c["name"] for c in pool.claims_of(pl, role)) if ok else []
        if len(held) == 1:
            return held[0]
        raise SystemExit("会话池里 %s 可能有几个会话（现在认领了：%s），写会话名，比如 %s" % (
            role, "、".join(held) or "无", pool.slot_name(policy["agents"][role], 1)))
    name = pool.resolve(policy, pl, role) if ok else pool.canonical(policy, role)
    if not name or not pool.canonical(policy, name):
        raise SystemExit("没有角色或会话 %s" % role)
    if pool.work_item_of(name) and name not in pl["claims"]:
        r = pool.role_of(policy, name)
        held = sorted(c["name"] for c in pool.claims_of(pl, r)) if ok else []
        raise SystemExit("%s 是 +0 的工作名，#%s 现在没有记下负责会话；改写池位名（%s 现有：%s）" % (
            name, pool.work_item_of(name), r, "、".join(held) or "无"))
    return name


def notify(spec, root, role, text, work_item=None, sender=None):
    """Leave a message in an inbox: a role's (inbox/<role>.jsonl) or, in pool mode, a session's."""
    policy = load_policy(root) or {}
    target = notify_target(spec, root, policy, role)
    if policy.get("mode") == "pool":                    # the sender is whoever runs this, not --from
        me, origin = caller(root, policy)
        sender = sender_label(me, origin)
    elif not sender:
        sender = spec.rules["naming"]["fixed_sessions"]["pmo-p2"]
    msg = {"id": uuid.uuid4().hex[:12], "ts": now(), "from": sender, "text": text, "read": False}
    if work_item:
        msg["work_item"] = str(work_item).lstrip("#")
    append_inbox(root, target, msg)
    print("已留言给 %s：%s" % (target, text[:60]))


ORIGIN_LABEL = {"researcher": "研究员的会话", "desk_conflict": "和已认领会话共用桌面会话 ID 的进程",
                "tombstone": "已释放认领的会话", "permit": "还没转正的会话", "pending": "还没转正的会话",
                "pool_unreadable": "认领表读不出时的会话"}


def sender_label(me, origin):
    """Who a message or request is from: the claim's name, or what kind of caller ran the script.
    A process with neither a session ID nor a desktop ID is a plain terminal, not a session."""
    if me:
        return me["name"]
    if origin == "researcher" and not os.environ.get(pool.SESSION_ENV) and not os.environ.get(pool.DESK_ENV):
        return "终端里的进程（没有会话 ID）"
    return ORIGIN_LABEL.get(origin, "未认领的会话")


def agent_roles(spec):
    out = []
    for s, _, _ in spec.sequences():
        for r in (2, 1, 0):
            out.append("pmo-router" if (s, r) == ("pmo", 1) else "pmo-scribe" if (s, r) == ("pmo", 0) else "%s-p%d" % (s, r))
    return out


def focus(spec, root, action, role=None, topic=None, offline=False):
    policy = load_policy(root) or {}
    if policy.get("mode") == "pool":
        return pool_focus(spec, root, policy, action, role, topic)
    path = os.path.join(root, "inbox", "focus.json")
    state = focus_state(root)
    if action == "list":
        for r, info in state.items():
            print("%s：%s（%s 开始）" % (r, info.get("topic"), info.get("opened_at")))
        if not state:
            print("没有开着的聚焦会话。")
        return
    if role and role not in agent_roles(spec) and policy.get("agents"):
        role = pool.role_of(policy, role) or role       # Alg-p2-01 means alg-p2 here
    if not role or not role.endswith("-p2") or role not in agent_roles(spec):
        raise SystemExit("聚焦会话只和某个序列的 +2 开，比如 alg-p2")
    if action == "close":
        state.pop(role, None)
        print("已收尾：%s。确认它交回的决策包已经登记成“已决定”。" % role)
    else:
        state[role] = {"topic": topic or "", "opened_at": now()}
        seq = role[:-3]
        lines = []
        if not offline:
            lg = ledger.Ledger(root, "preview")
            try:
                for row in ledger.query(lg, "open", seq, as_json=False):
                    lines.append("#%s %s %s" % (row.get("work_item_id"), row.get("work_item_status", ""), row.get("name", "")))
            except ledger.LedgerError as e:
                lines.append("（台账没读到：%s）" % str(e)[:80])
        text = "入口登记：研究员要和你谈“%s”。本序列进行中的工作项：%s" % (topic or "未写", "；".join(lines) or "无")
        notify(spec, root, role, text)
        print("请研究员在本目录另开一个终端运行：claude --agent %s" % role)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def pool_focus(spec, root, policy, action, target, topic):
    """Pool mode: mark a session as the researcher's for now; route.py pool will not suggest it."""
    if action == "list":
        pl, _ = pool.load(root)
        rows = [r for r in pool.session_view(root, policy, pl) if r["focus"]]
        for r in rows:
            left = "，空闲 %d 分钟后失效" % r["focus_left_min"] if r["focus_left_min"] is not None else ""
            print("%s：研究员在用（%s，%s 开始%s）" % (r["name"], r["focus"].get("topic"), r["focus"].get("opened_at"), left))
        if not rows:
            print("没有研究员在用的会话。")
        return
    me = caller(root, policy)[0]
    pl, _ = pool.load(root)
    name = None
    if target:
        if target in policy["agents"]:
            held = [c for c in pl["claims"].values() if c.get("role") == target]
            name = held[0]["name"] if len(held) == 1 else None
        else:
            name = pool.resolve(policy, pl, target)
    name = name or (me or {}).get("name")
    if not name:
        raise SystemExit("不知道是哪个会话：在池会话里运行时不用写名字；在别处运行写会话名，比如 Alg-p2-01")
    with pool.locked(root) as pl:
        if name not in pl["claims"]:
            raise SystemExit("%s 不在认领表里" % name)
        if action == "close":
            pl["focus"].pop(name, None)
        else:
            pl["focus"][name] = {"topic": topic or "", "opened_at": now()}
        sid = pl["claims"][name].get("session_id") or ""
    if action != "close":                               # an old lapse note no longer applies
        pool.update_session(root, sid, lambda st: st.pop("focus_lapsed", None))
    pool.log_event(root, {"event": "focus_" + action, "name": name, "topic": topic})
    if action == "close":
        print("已收尾：%s。谈定的事要写成决策包交给入口登记，没写进台账的不算数。" % name)
    else:
        print("已标记：研究员在用 %s（%s）。会话空闲满 30 分钟自动失效；这期间 route.py pool 不推荐它。去侧栏找 %s。" % (
            name, topic or "未写", name))


# ------------------------------------------------------------------ pool
def load_policy(root):
    if os.path.isfile(os.path.join(root, ".agent-org", "manifest.json")):
        return load_json(os.path.join(root, ".agent-org", "policy.json"), None)
    return load_json(os.path.join(root, ".claude", "hooks", "policy.json"), None)


def caller(root, policy):
    """(claim, origin) of the session running this script, from its Bash environment. A desktop ID
    whose claim belongs to another running session counts as a desk conflict, not as that claim."""
    sid = os.environ.get(pool.SESSION_ENV) or ""
    desk = os.environ.get(pool.DESK_ENV) or ""
    pl, ok = pool.load(root)
    if not ok:
        return None, "pool_unreadable"
    claim = pool.claim_by_session(pl, sid)
    if claim:
        return claim, "claim"
    other = pool.claim_by_desk(pl, desk)
    if other:
        return (other, "claim") if pool.cleared(root, other) else (None, "desk_conflict")
    if pool.permit_for(pl, sid, desk):
        return None, "permit"
    if pool.pending_for(pl, sid):
        return None, "pending"
    if pool.tombstoned(pl, sid, desk):
        return None, "tombstone"
    return None, "researcher"


def need_pool(root):
    policy = load_policy(root)
    if not policy:
        raise SystemExit("读不到 .claude/hooks/policy.json：这个目录还没初始化")
    if policy.get("mode") != "pool":
        print("本项目是 CLI 模式，不用认领，也没有会话池。")
        return None
    return policy


def plan_of(root):
    return load_json(os.path.join(root, ".claude", "agent-org", "pool-plan.json"), {}) or {}


def next_slot(policy, pl, role):
    """The name a new session of role gets: its fixed name, or the smallest slot not claimed or permitted."""
    pol = policy["agents"][role]
    if pol.get("fixed_session"):
        return pol["fixed_session"]
    taken = set(pl["claims"]) | {p.get("name") for p in pl["permits"]}
    n = 1
    while pool.slot_name(pol, n) in taken:
        n += 1
    return pool.slot_name(pol, n)


def child_row(tpl, name):
    return {k: (v.replace("{name}", name).replace("{short}", name.lower()) if isinstance(v, str) else v)
            for k, v in tpl.items()}


def children_text(root, policy, claim):
    """For a +2: the sessions it opens, from pool-plan.json, with the ones already claimed marked,
    and a ready row for the next free slot of each child role (for a refill)."""
    pl, _ = pool.load(root)
    top = next((t for t in plan_of(root).get("top", []) if t.get("name") == claim["name"] or t.get("role") == claim["role"]), None)
    if not top:
        return ""
    rows = []
    for ch in top.get("children", []):
        rows.append((ch, ch["name"] in pl["claims"]))
    cap = (policy.get("pool") or {}).get("max_sessions")
    full = bool(cap) and len(pl["claims"]) + len(pl["permits"]) >= cap
    for tpl in ([] if full else top.get("child_roles", [])):
        if pool.role_limit(policy, tpl["role"]) > len(pool.claims_of(pl, tpl["role"])):
            name = next_slot(policy, pl, tpl["role"])
            if not any(ch["name"] == name for ch, _ in rows):
                rows.append((child_row(tpl, name), False))
    lines = ["开局句要你开子会话时，照下表开还没认领的会话（开局句写着“不开子会话”的，不开，用 route.py pool 找回已有的会话）；"
             "研究员说“补位”时，开表里“补位用”的那一行。每个都用会话管理工具 start_session：initiation 写 user_asked，context 写 fresh，"
             "不开 worktree，权限模式不填（继承本会话）；name、title、model、prompt、background 照下表。开好马上用返回的桌面会话 ID 运行表里的 expect。"]
    if not top.get("children"):
        lines.append("本序列开池时只开 +2（任务文件的 pool.later），下表只有补位用的行。")
    if full:
        lines.append("会话池已到任务文件的总上限 max_sessions=%d，不能再补位：先请研究员归档不用的会话并 release。" % cap)
    for ch, done in rows:
        mark = "（已认领，不用再开）" if done else ("" if ch in top.get("children", []) else "（补位用）")
        lines.append("- %s：name=%s，title=“%s”，model=%s%s\n  prompt：%s\n  background：%s\n  然后：%s" % (
            ch["name"], ch["short"], ch["title"], ch["model"], mark, ch["brief"], ch["background"], ch["expect"]))
    lines.append("开完以后：用 set_session_title 把本会话的标题改成“%s”，再把本会话置顶；回研究员一句“已认领 %s，已开 …”。"
                 "model 填不进去就不填，开好以后再改模型。" % (top["title"], claim["name"]))
    return "\n".join(lines)


def claim_cmd(spec, root, role, name=None, desk_arg=None, wait=CLAIM_WAIT):
    policy = need_pool(root)
    if not policy:
        return 0
    sid = os.environ.get(pool.SESSION_ENV) or ""
    desk = os.environ.get(pool.DESK_ENV) or desk_arg or ""
    if not sid:
        print("出错：拿不到本会话的 ID（CLAUDE_CODE_SESSION_ID）；这条命令要在会话自己的 Bash 里运行。")
        return 1
    if role not in policy["agents"]:
        print("出错：没有角色 %s" % role)
        return 1
    deadline = time.monotonic() + wait
    while True:
        pl, ok = pool.load(root)
        if not ok:
            print("出错：读不出 .claude/state/pool.json；请研究员先处理。")
            return 1
        claim = pool.claim_by_session(pl, sid) or pool.claim_by_desk(pl, desk)
        if claim:
            if claim["role"] != role:
                print("出错：本会话已经认领了 %s（%s），不能再认领 %s。" % (claim["role"], claim["name"], role))
                return 1
            if claim.get("session_id") != sid and not pool.cleared(root, claim):
                print("出错：认领 %s 属于另一个会话（它没有因为清空上下文而结束），不能接过来。要换会话，请研究员先 release。" % claim["name"])
                return 1
            if claim.get("session_id") != sid or (desk and claim.get("desk") != desk):
                with pool.locked(root) as pl2:
                    pl2["pending"] = [p for p in pl2["pending"] if p.get("session_id") != sid]
                    pl2["pending"].append({"kind": "attach" if claim.get("session_id") != sid else "desk",
                                           "name": claim["name"], "role": role, "session_id": sid,
                                           "desk": desk or None, "at": now()})
                print("待转正：本会话的 ID 有变化，这条命令结束时 hook 会把认领 %s 接到本会话上。" % claim["name"])
            print("已认领：%s。" % pool.describe(policy, claim))
            print(pool.injection(root, policy, claim, owners=pl.get("owners")))
            more = children_text(root, policy, claim) if policy["agents"][role]["rank"] == 2 else ""
            if more:
                print("\n" + more)
            return 0
        permit = pool.permit_for(pl, sid, desk)
        if permit:
            if permit.get("role") != role:
                print("出错：本会话的许可是 %s（%s），不是 %s。" % (permit["role"], permit["name"], role))
                return 1
            with pool.locked(root) as pl2:
                pl2["pending"] = [p for p in pl2["pending"] if p.get("session_id") != sid]
                pl2["pending"].append({"kind": "claim", "name": permit["name"], "role": role, "session_id": sid,
                                       "desk": desk or permit.get("desk"), "at": now()})
            pool.log_event(root, {"event": "pending", "session_id": sid, "desk": desk or None,
                                  "name": permit["name"], "role": role})
            print("待转正：%s（%s）。这条命令结束时 hook 会转正，下一条命令起本会话就是 %s。" % (permit["name"], role, permit["name"]))
            print(pool.injection(root, policy, dict(permit, name=permit["name"]), owners=pl.get("owners")))
            return 0
        if pool.tombstoned(pl, sid, desk):
            print("出错：本会话原来的认领已经释放；要重新认领，请研究员或开它的会话重新预登记。")
            return 1
        if time.monotonic() >= deadline:
            print("出错：没有找到本会话认领 %s 的许可（等了 %d 秒）。开局第一句写着“认领 <+2 角色>（会话名 …）”的顶层会话由 hook 直接认领；"
                  "子会话要等开它的会话运行 route.py expect 预登记。研究员自己的会话不认领。" % (role, wait))
            return 1
        time.sleep(2)


def expect_cmd(spec, root, name, role, desk=None, session=None):
    policy = need_pool(root)
    if not policy:
        return 0
    me, origin = caller(root, policy)
    if role not in policy["agents"]:
        print("出错：没有角色 %s" % role)
        return 1
    full = pool.canonical(policy, name)
    fixed = pool.fixed_name(policy, role)
    if not full or pool.role_of(policy, full) != role or not pool.is_slot(full):
        print("出错：会话名 %s 和角色 %s 对不上；写成 %s 这样的名字。" % (name, role, fixed or pool.slot_name(policy["agents"][role], 1)))
        return 1
    if fixed and full != fixed:
        print("出错：%s 的会话名固定是 %s。" % (role, fixed))
        return 1
    if not desk and not session:
        print("出错：要给 --desk <桌面会话 ID>（开会话时返回的 ID），终端里测试时给 --session <会话 ID>。")
        return 1
    if origin == "claim":
        mine, target = policy["agents"][me["role"]], policy["agents"][role]
        ok = ((me["role"] == policy.get("entry_role") and role in (policy.get("router_role"), policy.get("scribe_role")))
              or (mine["rank"] == 2 and me["role"] != policy.get("entry_role")
                  and target["sequence"] == mine["sequence"] and target["rank"] in (0, 1)))
        if not ok:
            print("出错：%s 不能预登记 %s：+2 只预登记本序列的 +1、+0，入口只预登记路由和书记员。" % (me["name"], role))
            return 1
    elif origin != "researcher":
        print("出错：本会话（%s）不能预登记。" % origin)
        return 1
    by = me["name"] if me else "researcher"
    cap = (policy.get("pool") or {}).get("max_sessions")
    with pool.locked(root) as pl:
        other = pool.claim_by_desk(pl, desk) or pool.claim_by_session(pl, session)
        if other:
            print("出错：这个会话已经认领了 %s。" % other["name"])
            return 1
        if full in pl["claims"]:
            print("出错：%s 已经被认领；换一个名字，或请研究员先 release。" % full)
            return 1
        def same_target(p):
            return (desk and p.get("desk") == desk) or (session and p.get("session_id") == session) or p.get("name") == full
        pl["permits"] = [p for p in pl["permits"] if not same_target(p)]   # a new pre-registration replaces the old one
        held = pool.claims_of(pl, role)
        waiting = [p for p in pl["permits"] if p.get("role") == role]
        if len(held) + len(waiting) >= pool.role_limit(policy, role):
            print("出错：%s 已有 %d 条认领、%d 条许可，上限 %d。" % (role, len(held), len(waiting), pool.role_limit(policy, role)))
            return 1
        if cap and len(pl["claims"]) + len(pl["permits"]) >= cap:
            print("出错：会话池已有 %d 条认领和许可，到了任务文件的总上限 max_sessions=%d；先让研究员归档不用的会话并 release。" % (
                len(pl["claims"]) + len(pl["permits"]), cap))
            return 1
        pl["permits"].append({"name": full, "role": role, "desk": desk or None, "session_id": session or None,
                              "by": by, "via": "expect", "at": now()})
    pool.log_event(root, {"event": "expect", "name": full, "role": role, "desk": desk, "session_id": session, "by": by})
    print("已预登记 %s（%s），%s。对方运行 route.py claim 时转正。" % (full, role, "桌面会话 %s" % desk if desk else "会话 %s" % session))
    return 0


def release_cmd(spec, root, name):
    policy = need_pool(root)
    if not policy:
        return 0
    me, origin = caller(root, policy)
    if origin != "researcher":
        print("出错：只有研究员能释放认领，在研究员自己的会话或终端里运行。")
        return 1
    full = pool.canonical(policy, name) or name
    with pool.locked(root) as pl:
        claim = pl["claims"].pop(full, None)
        if claim:
            pl["tombstones"].append(dict(claim, released_at=now(), released_by="researcher"))
            pl["focus"].pop(full, None)
        permits = [p for p in pl["permits"] if p.get("name") == full]
        pending = [p for p in pl["pending"] if p.get("name") == full]
        pl["permits"] = [p for p in pl["permits"] if p.get("name") != full]
        pl["pending"] = [p for p in pl["pending"] if p.get("name") != full]
    if not claim and not permits and not pending:
        print("出错：认领表里没有 %s，也没有它的许可。" % full)
        return 1
    pool.log_event(root, {"event": "release", "name": full, "role": (claim or {}).get("role"),
                          "session_id": (claim or {}).get("session_id"), "permits": len(permits), "pending": len(pending)})
    if claim:
        print("已释放 %s（%s）。原来的会话不再代表这个角色；+2 照 pool-plan.json 的重建句新建顶层会话，其余角色由开它的会话重新预登记。" % (
            full, claim["role"]))
    if permits or pending:
        print("已撤掉 %s 的 %d 条许可、%d 条待转正。" % (full, len(permits), len(pending)))
    return 0


STATUS_CN = {"busy": "在忙", "idle": "空闲", "idle_bg": "空闲（后台任务在跑）", "waiting_researcher": "等研究员批准",
             "offline": "离线", "uncertain": "不确定（标着在忙，15 分钟没有工具调用）", "unknown": "未知"}


def status_cn(status):
    status = status or "unknown"
    if status.startswith("failed"):
        return "失败（%s）" % status.partition(":")[2]
    return STATUS_CN.get(status, status)


def suggest(policy, rows, role, item, pl):
    """Who to send this role's work to: an idle session (the work item's owner first), else refill or queue."""
    mine = [r for r in rows if r["role"] == role]
    owner = (pl.get("owners") or {}).get(str(item)) if item else None
    free = [r for r in mine if r["status"] in ("idle", "idle_bg") and not r["focus"]]
    if owner and any(r["name"] == owner for r in free):
        return "发给 %s：它是 #%s 的负责会话，现在空闲。" % (owner, item)
    if free:
        return "发给 %s（空闲）。" % free[0]["name"]
    cap = (policy.get("pool") or {}).get("max_sessions")
    if cap and len(pl["claims"]) + len(pl["permits"]) >= cap:
        return "%s 没有空闲的会话，会话池也到了总上限 %d：留在“待开工”排队，等有会话交回。" % (role, cap)
    if len(mine) < pool.role_limit(policy, role):
        return "%s 没有空闲的会话，还没到上限 %d：排队，或请研究员在本序列 +2 的会话里说“补位”（别处运行 route.py refill %s）。" % (
            role, pool.role_limit(policy, role), role)
    return "%s 没有空闲的会话，已到上限 %d：留在“待开工”排队，等有会话交回。" % (role, pool.role_limit(policy, role))


def drop_expired_focus(root, policy, pl):
    """Remove focus flags whose sessions have been idle for 30 minutes, so they do not come back."""
    sessions = pool.all_sessions(root)
    stale = [n for n in (pl.get("focus") or {}) if n in pl["claims"]
             and pool.focus_expired(sessions.get(pl["claims"][n].get("session_id")) or {})]
    if not stale:
        return pl
    with pool.locked(root) as locked_pool:
        for n in stale:
            locked_pool["focus"].pop(n, None)
    for n in stale:
        pool.log_event(root, {"event": "focus_expired", "name": n})
        pool.update_session(root, pl["claims"][n].get("session_id") or "", lambda st: st.update({"focus_lapsed": True}))
    return pool.load(root)[0]


def open_refills(pl):
    return [r for r in pl.get("refills", []) if int(r.get("count") or 1) > 0]


def print_pool(root, policy, role=None, item=None):
    pl, ok = pool.load(root)
    if not ok:
        print("出错：读不出 .claude/state/pool.json；请研究员先处理。")
        return 1
    pl = drop_expired_focus(root, policy, pl)
    rows = pool.session_view(root, policy, pl)
    shown = [r for r in rows if not role or r["role"] == role]
    total = len(pl["claims"]) + len(pl["permits"])
    print("会话池：%d 条认领，%d 条待转正的许可，上限 %s 个会话。" % (len(pl["claims"]), len(pl["permits"]),
                                                       (policy.get("pool") or {}).get("max_sessions", "?")))
    for r in shown:
        extra = []
        if r["work_items"]:
            extra.append("负责 " + "、".join("#" + i for i in r["work_items"]))
        if r["focus"]:
            left = "，还剩 %d 分钟" % r["focus_left_min"] if r["focus_left_min"] is not None else ""
            extra.append("研究员在用（%s%s）" % (r["focus"].get("topic") or "未写", left))
        if r["quota"]:
            extra.append("额度暂停（%s）" % r["quota"])
        if r["denied"]:
            extra.append("被拦 %d 次" % r["denied"])
        print("- %s（%s）%s；最后活动 %s；桌面会话 %s；短名 %s%s" % (
            r["name"], r["role"], status_cn(r["status"]), r["last"] or "无记录", r["desk"] or "未知", r["short"],
            "；" + "；".join(extra) if extra else ""))
    for p in pl["permits"]:
        if not role or p.get("role") == role:
            print("- 待转正：%s（%s），预登记方 %s" % (p.get("name"), p.get("role"), p.get("by")))
    for r in open_refills(pl):
        if not role or r.get("role") == role:
            print("- 补位请求：%s ×%s（%s，%s 提）" % (r.get("role"), r.get("count", 1), r.get("by"), str(r.get("at"))[:16]))
    mixed = pool.mixed_modes(rows)
    if mixed:
        print("提醒：%s 的权限模式和池里多数会话不是一类（bypass 和其他模式之间的消息会被扣下）。" % "、".join(mixed))
    near = [r["name"] for r in rows if (r["denied"] or 0) >= 15]
    if near:
        print("提醒：%s 被自动模式拦了 15 次以上，到 20 次会退回逐条询问。" % "、".join(near))
    cap = (policy.get("pool") or {}).get("max_sessions")
    if cap and total >= cap - 2:
        print("提醒：会话数 %d 接近总上限 %d。" % (total, cap))
    if role:
        print("建议：" + suggest(policy, rows, role, item, pl))
        target = next((r for r in shown if r["status"] in ("idle", "idle_bg") and not r["focus"]), None)
        if target:
            print("发法：用会话管理工具发消息（%s），对方的桌面会话 ID 是 %s；同一家族里也可以写短名 %s。"
                  "抬头里写对方的池位名 %s。回执是 delivered 或 queued 才算发出。" % (
                      policy.get("channel", "send_message"), target["desk"] or "未知", target["short"], target["name"]))
    return 0


def queue_rows(root, policy, me, seq):
    """The 待开工 work items this claim dispatches: for the entry, cross-sequence requests waiting
    for a +2 and 下发 packages it wrote; for a +2 or +1, the packages of its sequence it wrote."""
    lg = ledger.Ledger(root, "preview")
    tag = "（%s）" % me["name"]
    rows = []
    with contextlib.redirect_stdout(io.StringIO()):
        if me["role"] == policy.get("entry_role"):
            leased = set((pool.load(root)[0].get("owners") or {}))
            rows += [r for r in ledger.query(lg, "cross", None, as_json=False)
                     if r.get("work_item_status") == "待接单" and str(r.get("work_item_id")) not in leased]
            rows += [r for r in ledger.query(lg, "open", None, as_json=False)
                     if r.get("work_item_status") == "待开工" and tag in str(r.get("name", ""))]
        else:
            rows += [r for r in ledger.query(lg, "open", seq, as_json=False)
                     if r.get("work_item_status") == "待开工" and tag in str(r.get("name", ""))]
    return rows


def queue_cmd(root, policy, name, offline=False):
    pl, _ = pool.load(root)
    me = pl["claims"].get(pool.canonical(policy, name) or name)
    if not me:
        print("出错：%s 不在认领表里。" % name)
        return 1
    seq = policy["agents"][me["role"]]["sequence"]
    rows = pool.session_view(root, policy, pl)
    free = [r["name"] for r in rows if r["status"] in ("idle", "idle_bg") and not r["focus"] and r["name"] != me["name"]
            and pool.kind_allowed(policy, "assign", me["role"], r["role"])]
    print("能派活的空闲会话：%s" % ("、".join(free) or "无"))
    if offline:
        print("（离线，没查台账里待开工的工作项）")
        return 0
    try:
        items = queue_rows(root, policy, me, seq)
    except ledger.LedgerError as e:
        print("台账没读到：%s" % str(e)[:120])
        return 1
    for row in items:
        print("- %s #%s %s" % (row.get("work_item_status"), row.get("work_item_id"), row.get("name", "")))
    if not items:
        print("没有等你派的工作项。没事就一句话结束本轮。")
    elif free:
        print("有等你派的活、又有空闲会话：先请书记员把它流转到“执行中”（跨序列需求是派对方 +2 接单），再派出去。")
    return 0


RECOVER_BEFORE = ("busy", "uncertain", "waiting_researcher")


def recover_cmd(root, policy):
    pl, ok = pool.load(root)
    if not ok:
        print("出错：读不出 .claude/state/pool.json")
        return 1
    rows = pool.session_view(root, policy, pl)
    entry = policy["agents"][policy["entry_role"]].get("fixed_session") or "PMO-p2-01"
    word = (policy.get("msg") or {}).get("resume", "继续")
    print("恢复清单：")
    unfinished, replies = [], []
    for path in pool.files_in(os.path.join(root, "packages", "sent"), "*.json"):
        rec = load_json(path, {}) or {}
        if isinstance(rec, dict) and rec.get("status") not in (None, "registered", "replied"):
            if os.path.basename(path).startswith("reply-"):
                replies.append("%s（答复 #%s，%s）" % (rec.get("draft"), rec.get("parent"), rec.get("status")))
            else:
                unfinished.append("%s（#%s，%s）" % (rec.get("draft"), rec.get("work_item_id"), rec.get("status")))
    if unfinished:
        print("- 书记员先补完写到一半的登记，逐条运行 ledger.py register <草稿> --resume：%s" % "；".join(unfinished))
    if replies:
        print("- 书记员补完写到一半的答复，逐条重跑 ledger.py reply <草稿>（会接着上次的回执做）：%s" % "；".join(replies))
    for r in rows:
        st, before = r["status"], r.get("prev_status") or ""
        if before == "idle_bg":
            print("- %s：应用退出前有后台任务在跑，后台任务随应用退出停了。到侧栏点开它看一眼，需要的话重新启动。" % r["name"])
        elif before in RECOVER_BEFORE or before.startswith("failed"):
            print("- %s：应用退出前%s。到侧栏点开它看一眼，需要的话重发上一条消息。" % (r["name"], status_cn(before)))
        elif st in RECOVER_BEFORE or st.startswith("failed"):
            print("- %s：%s。到侧栏点开它看一眼，需要的话按回车或重发上一条消息。" % (r["name"], status_cn(st)))
        if r.get("quota"):
            print("- %s：额度暂停过（%s），要按回车才继续的，到侧栏点开它。" % (r["name"], r["quota"]))
    found = False
    items = pool.all_open_items(root, pl.get("owners"))
    for name in sorted(pl["claims"]):
        sent, _ = items.get(name, ({}, {}))
        for item, to in sorted(sent.items()):
            found = True
            if name == entry:
                print("- #%s：你派给 %s，还没交回。发：【%s #%s】%s → %s：重启后接着做；做完了的，重发交回。" % (
                    item, to, word, item, entry, to))
            else:
                print("- #%s：%s 派给 %s，还没交回。给 %s 发消息【%s #%s】%s → %s：重启后请给 %s 发【%s #%s】。" % (
                    item, name, to, name, (policy.get("msg") or {}).get("notice", "通知"), item, entry, name, to, word, item))
    if not found:
        print("- 没有派出去还没交回的工作项。")
    print("- 长期任务（比如 route.py listen）随应用退出就停了，要研究员在终端里重新启动。")
    return 0


def watch_cmd(root, policy, once=False, every=30):
    while True:
        pl, ok = pool.load(root)
        if ok:
            pl = drop_expired_focus(root, policy, pl)
        rows = pool.session_view(root, policy, pl) if ok else []
        order = {"waiting_researcher": 0, "uncertain": 2}
        rows.sort(key=lambda r: (0 if r["status"] == "waiting_researcher" else 1 if r["status"].startswith("failed")
                                 else order.get(r["status"], 3), r["name"]))
        if not once:
            sys.stdout.write("\033[2J\033[H")
        print("会话池 %s（每 %d 秒刷新，Ctrl-C 退出）" % (now(), every))
        for r in rows:
            print("%-14s %-22s %s %s" % (r["name"], status_cn(r["status"]), " ".join("#" + i for i in r["work_items"]),
                                         "研究员在用" if r["focus"] else ""))
        pending = open_refills(pl)
        if pending:
            print("待补位：" + "；".join("%s×%s（%s）" % (p.get("role"), p.get("count", 1), str(p.get("at"))[5:16]) for p in pending))
        if once:
            return 0
        sys.stdout.flush()
        time.sleep(every)


def refill_cmd(spec, root, role, count=1):
    policy = need_pool(root)
    if not policy:
        return 0
    if role not in policy["agents"]:
        print("出错：没有角色 %s" % role)
        return 1
    by = sender_label(*caller(root, policy))
    with pool.locked(root) as pl:
        pl["refills"].append({"role": role, "count": count, "by": by, "at": now()})
    pool.log_event(root, {"event": "refill", "role": role, "count": count, "by": by})
    entry = policy["agents"][policy["entry_role"]].get("fixed_session") or "PMO-p2-01"
    append_inbox(root, entry, {"id": uuid.uuid4().hex[:12], "ts": now(), "from": by, "read": False,
                               "text": "补位请求：%s 再开 %d 个会话。放进每日决策队列，研究员在对应 +2 的会话里说“补位”时开。" % (role, count)})
    print("已记补位请求：%s ×%d，进入口的每日队列。现在不开会话，也不提卡片。新会话认领后这条请求自动销掉。" % (role, count))
    return 0


def audit_cmd(spec, root, hours=24):
    policy = need_pool(root)
    if not policy:
        return 0
    pl, ok = pool.load(root)
    if not ok:
        print("出错：读不出 .claude/state/pool.json")
        return 1
    hits = {}
    try:
        with open(os.path.join(root, ".claude", "logs", "guard.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    for h in rec.get("soft_hits") or []:
                        hits.setdefault(rec.get("session_name") or rec.get("role") or "?", []).append(h)
    except OSError:
        pass
    events = pool.read_events(root)
    for rec in events:
        for h in rec.get("soft_hits") or []:
            hits.setdefault(rec.get("seen_by") or "?", []).append(h)
    print("软规则命中（只提醒，没拦）：")
    for who, hs in sorted(hits.items()):
        print("- %s：%d 次；最近一次：%s" % (who, len(hs), hs[-1][:100]))
    if not hits:
        print("- 无")
    claimed = {r.get("name") for r in events if r.get("event") in ("claim", "reattach")}
    odd = [n for n in pl["claims"] if n not in claimed]
    if odd:
        print("认领表里有、日志里没有认领记录的：%s（可能被直接改过，请研究员核对）" % "、".join(odd))
    scribes = pool.claims_of(pl, policy.get("scribe_role"))
    if len(scribes) > 1:
        print("问题：书记员有 %d 条认领，台账写入已全部停下。" % len(scribes))
    conflicts = sorted({r.get("name") for r in events if r.get("event") == "desk_conflict"})
    if conflicts:
        print("和别的进程共用桌面会话 ID 的认领：%s（多半是从这些会话里起的 claude 进程）" % "、".join(conflicts))
    rows = pool.session_view(root, policy, pl)
    stale = [r["name"] for r in rows if r["status"] == "uncertain"]
    if stale:
        print("标着在忙、15 分钟没有工具调用的：%s" % "、".join(stale))
    at = pool.now()
    sent_times = {}
    for rec in events:
        if rec.get("event") == "message" and rec.get("kind") == "assign" and rec.get("id"):
            sent_times[(rec.get("from"), rec["id"])] = pool.parse_time(rec.get("ts"))
    late = []
    for name, (sent, _) in pool.all_open_items(root, pl.get("owners")).items():
        for item, to in sent.items():
            t = sent_times.get((name, item))
            if t and at - t > datetime.timedelta(hours=hours):
                late.append("#%s（%s → %s）" % (item, name, to))
    if late:
        print("派出 %d 小时还没交回的：%s" % (hours, "、".join(sorted(late))))
    mixed = pool.mixed_modes(rows)
    if mixed:
        print("权限模式和多数会话不同类的：%s" % "、".join(mixed))
    return 0


def pool_cmd(spec, root, args):
    policy = need_pool(root)
    if not policy:
        return 0
    if args.recover:
        return recover_cmd(root, policy)
    if args.watch:
        return watch_cmd(root, policy, args.once)
    if args.queue:
        return queue_cmd(root, policy, args.queue, args.offline)
    return print_pool(root, policy, args.role, args.item)


# --------------------------------------------------------------- listen
def listen(root, max_events=None):
    """Append the researcher's Feishu DMs to inbox/feishu.jsonl. Run by the researcher.

    `event consume im.message.receive_v1` prints one flat JSON object per message:
    type, sender_id (open_id), sender_type, chat_type, content (already readable
    text) and message_id, which is the key to deduplicate on.
    """
    ids = load_json(os.path.join(root, "feishu", "ids.json"), {})
    researcher = (ids.get("lark") or {}).get("researcher_open_id")
    if not researcher:
        print("feishu/ids.json 里没有研究员的 open_id（lark.researcher_open_id），不启动监听：否则谁私聊机器人都会进入口。"
              "先跑 setup.py feishu --live --apply 读回。")
        return 2
    path = os.path.join(root, "inbox", "feishu.jsonl")
    seen = set()
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    seen.add(json.loads(line).get("id"))
                except ValueError:
                    pass
    cmd = ["lark-cli", "event", "consume", "im.message.receive_v1", "--as", "bot"]
    if max_events:
        cmd += ["--max-events", str(max_events)]
    print("监听飞书私聊：%s（Ctrl-C 停止）" % " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    for line in proc.stdout:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type", "im.message.receive_v1") != "im.message.receive_v1":
            continue
        if ev.get("sender_type") != "user" or ev.get("chat_type") != "p2p" or ev.get("sender_id") != researcher:
            continue
        key = ev.get("message_id") or ev.get("id")
        text = ev.get("content")
        if not key or not text or key in seen:
            continue
        seen.add(key)
        append_inbox(root, "feishu", {"id": key, "ts": now(), "from": "研究员（飞书私聊）", "text": text, "read": False})
        print("收到：%s" % text[:60])
    return proc.wait()


def main(argv=None):
    ap = argparse.ArgumentParser(description="入口的机械路由、收件箱和聚焦会话", allow_abbrev=False)
    ap.add_argument("--offline", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("classify")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--draft")
    p = sub.add_parser("notify")
    p.add_argument("role", help="角色（alg-p2）或会话池里的会话名（Alg-p2-01）")
    p.add_argument("--text", required=True)
    p.add_argument("--work-item")
    p.add_argument("--from", dest="sender")
    p = sub.add_parser("focus")
    p.add_argument("action", choices=["open", "close", "list"])
    p.add_argument("role", nargs="?")
    p.add_argument("--topic")
    p = sub.add_parser("claim", allow_abbrev=False)
    p.add_argument("role")
    p.add_argument("--as", dest="name")
    p.add_argument("--desk", help="环境里拿不到桌面会话 ID 时，从会话管理工具读出自己的 ID 传进来")
    p.add_argument("--wait", type=int, default=CLAIM_WAIT)
    p = sub.add_parser("expect", allow_abbrev=False)
    p.add_argument("name")
    p.add_argument("--role", required=True)
    p.add_argument("--desk")
    p.add_argument("--session")
    p = sub.add_parser("release", allow_abbrev=False)
    p.add_argument("name")
    p = sub.add_parser("pool")
    p.add_argument("--role")
    p.add_argument("--item")
    p.add_argument("--queue")
    p.add_argument("--recover", action="store_true")
    p.add_argument("--watch", action="store_true")
    p.add_argument("--once", action="store_true")
    p = sub.add_parser("refill")
    p.add_argument("role")
    p.add_argument("--count", type=int, default=1)
    p = sub.add_parser("audit")
    p.add_argument("--hours", type=int, default=24)
    p = sub.add_parser("listen")
    p.add_argument("--max-events", type=int,
                   help="lark-cli 收到这么多条事件就退出；别人发给机器人的私聊、群消息也算数，不只算收进收件箱的")
    args = ap.parse_args(argv)
    spec = orgspec.OrgSpec()
    root = ledger.project_root()
    codex = os.path.isfile(os.path.join(root, ".agent-org", "manifest.json"))
    if codex and args.cmd != "classify":
        print("Codex 的会话操作使用 codex_runtime.py；本入口只复用 classify 的业务路由")
        return 2
    if args.cmd == "classify":
        out = classify_text(spec, root, args.text, args.offline) if args.text else classify_draft(spec, root, args.draft)
        if codex:
            out = json.loads(json.dumps(out, ensure_ascii=False).replace("route.py notify", "entry 技能的通知流程"))
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "notify":
        notify(spec, root, args.role, args.text, args.work_item, args.sender)
        return 0
    if args.cmd == "focus":
        focus(spec, root, args.action, args.role, args.topic, args.offline)
        return 0
    if args.cmd == "listen":
        return listen(root, args.max_events)
    if args.cmd == "claim":
        return claim_cmd(spec, root, args.role, args.name, args.desk, args.wait)
    if args.cmd == "expect":
        return expect_cmd(spec, root, args.name, args.role, args.desk, args.session)
    if args.cmd == "release":
        return release_cmd(spec, root, args.name)
    if args.cmd == "pool":
        return pool_cmd(spec, root, args)
    if args.cmd == "refill":
        return refill_cmd(spec, root, args.role, args.count)
    if args.cmd == "audit":
        return audit_cmd(spec, root, args.hours)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
