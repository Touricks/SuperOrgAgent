#!/usr/bin/env python3
"""Mechanical part of the entry's routing, plus inboxes and focus sessions.

    route.py classify --text '<message>'      rows R1/R3 by work item ID or session name; else needs_router
    route.py classify --draft <path>          rows R5/R6/R7 for a package handed back to the entry
    route.py notify <role> --text T [--work-item ID]
    route.py focus open <role> --topic T | focus close <role> | focus list
    route.py listen                           Feishu DM listener: appends to inbox/feishu.jsonl

classify walks the routing table in orgspec/rules.json and stops at the first row
it can decide without judgement. Matching a message to work by the work item's
name, fields and 状态包 (R2) is left to the router agent: the script answers
needs_router instead of guessing. --offline skips the ledger lookups.
Python 3.9 standard library only.
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import ledger  # noqa: E402

ID_RE = re.compile(r"#(\d{3,})|(?<![\d-])(\d{9,12})(?!\d)")
SESSION_RE = re.compile(r"\b([A-Z][A-Za-z]*-p[0-2]-[0-9A-Za-z]+)\b")


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def role_of_session(spec, session):
    m = re.match(r"([A-Za-z]+)-p([0-2])-", session)
    if not m:
        return None
    seq = next((s for s, _, a in spec.sequences() if a == m.group(1)), None)
    if not seq:
        return None
    rank = int(m.group(2))
    if seq == "pmo" and rank < 2:
        return "pmo-router" if rank == 1 else "pmo-scribe"
    return "%s-p%d" % (seq, rank)


def focus_state(root):
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
        m = re.match(r"[A-Za-z]+-p0-(\d+)$", s)
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
                          session=owner, focus=focus)
        for s in sessions:
            role = role_of_session(spec, s)
            if role:
                return result(spec, "R1", "带会话名 %s" % s, session=s, role=role,
                              focus=role if role in focus_state(root) else None)
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
    if d.pid == "decision":
        if d.header.get("渠道") == spec.term("channel", "researcher"):
            return result(spec, "R6", "研究员主动发起的决策包", next_hop="pmo-scribe",
                          steps=["书记员 register 后流转到“已决定”，表单填决定内容和决定人",
                                 "通知“受影响的工作项”的负责会话（route.py notify）"])
        return result(spec, "R5", "决策队列来的决策包", next_hop="pmo-scribe",
                      steps=["书记员 register，建成“待决定”", "紧急程度为“插队”的马上交研究员，其余进每日队列"])
    route = next((r for r in spec.rules["package_routes"] if r["pkg"] == d.pid and (r["rel"] or None) == d.rel), None)
    steps, hop = [], None
    if d.pid == "request" and d.rel == "cross":
        hop = "%s-p2" % to_seq if to_seq else None
        steps = ["书记员 register，建跨序列需求（待接单）", "派 %s 接单：复述需求、承接范围、预计交付时间" % hop]
    elif d.pid == "delivery" and d.rel == "cross":
        hop = "%s-p2" % to_seq if to_seq else None
        steps = ["确认交付物涉及的结论已有验收包", "书记员 reply，并流转跨序列需求到“已交付”", "恢复 %s 接收交付" % hop]
    elif d.pid == "review":
        hop = "quality-p2"
        steps = ["书记员 register，建送审（待核查）；被送审的工作项流转到“待验收”", "派 quality-p2 核查"]
    elif d.pid == "verdict":
        hop = "pmo-scribe"
        steps = ["书记员 reply，按它打印的两条流转命令改状态", "恢复送审方所在序列的 +2，交验收包路径"]
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


def notify(spec, root, role, text, work_item=None, sender=None):
    if role != "feishu" and role not in agent_roles(spec):
        raise SystemExit("没有角色 %s" % role)
    msg = {"id": uuid.uuid4().hex[:12], "ts": now(), "from": sender or spec.rules["naming"]["fixed_sessions"]["pmo-p2"],
           "text": text, "read": False}
    if work_item:
        msg["work_item"] = str(work_item).lstrip("#")
    append_inbox(root, role, msg)
    print("已留言给 %s：%s" % (role, text[:60]))


def agent_roles(spec):
    out = []
    for s, _, _ in spec.sequences():
        for r in (2, 1, 0):
            out.append("pmo-router" if (s, r) == ("pmo", 1) else "pmo-scribe" if (s, r) == ("pmo", 0) else "%s-p%d" % (s, r))
    return out


def focus(spec, root, action, role=None, topic=None, offline=False):
    path = os.path.join(root, "inbox", "focus.json")
    state = focus_state(root)
    if action == "list":
        for r, info in state.items():
            print("%s：%s（%s 开始）" % (r, info.get("topic"), info.get("opened_at")))
        if not state:
            print("没有开着的聚焦会话。")
        return
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
    ap = argparse.ArgumentParser(description="入口的机械路由、收件箱和聚焦会话")
    ap.add_argument("--offline", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("classify")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--draft")
    p = sub.add_parser("notify")
    p.add_argument("role")
    p.add_argument("--text", required=True)
    p.add_argument("--work-item")
    p.add_argument("--from", dest="sender")
    p = sub.add_parser("focus")
    p.add_argument("action", choices=["open", "close", "list"])
    p.add_argument("role", nargs="?")
    p.add_argument("--topic")
    p = sub.add_parser("listen")
    p.add_argument("--max-events", type=int,
                   help="lark-cli 收到这么多条事件就退出；别人发给机器人的私聊、群消息也算数，不只算收进收件箱的")
    args = ap.parse_args(argv)
    spec = orgspec.OrgSpec()
    root = ledger.project_root()
    if args.cmd == "classify":
        out = classify_text(spec, root, args.text, args.offline) if args.text else classify_draft(spec, root, args.draft)
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
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
