#!/usr/bin/env python3
"""Track running org instances and pool sessions.

CLI mode (.claude/state/instances.json; SubagentStart / SubagentStop / SessionStart / SessionEnd):
- SubagentStart records the instance. +1 and +2 get the smallest free sequence
  number; +0 takes the session name the dispatcher wrote in its prompt (reserved
  by guard.py at PreToolUse); pmo-scribe is always PMO-p0-01. The session name is
  passed to the subagent through additionalContext.
- SubagentStop removes the instance.
- SessionStart registers a top-level session that runs as an org role: the entry
  (main agent from settings) or a focus session started with `claude --agent <role>`.
  It counts against the same limits, so the entry cannot dispatch a +2 the
  researcher is talking to. SessionEnd removes it.
Entries older than 12 hours without a Stop are expired.

Pool mode (.claude/state/pool.json and sessions/<id>.json, through pool.py):
- SessionStart records the session's desktop ID and Claude Code version, turns a
  waiting permit into a claim, re-attaches a claim after /clear, and injects the
  role text with the open items on startup, resume, compaction and /clear.
- SessionEnd marks the session offline and keeps its claim.
- Stop, StopFailure, Notification, PermissionRequest and PermissionDenied keep the
  session's status (idle, idle_bg, failed, waiting_researcher) and counters;
  PostCompact keeps the compaction summary in .claude/logs/compact/<name>.jsonl.
Subagents keep the CLI bookkeeping in both modes.

This hook never blocks: SubagentStart cannot block, and exit 2 on SubagentStop
would keep the subagent running, so every error exits 0.
guard.py imports this module to count instances and reserve slots on Agent calls.
Python 3.9 standard library only.
"""
import contextlib
import datetime
import fcntl
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pool  # noqa: E402

EXPIRE = datetime.timedelta(hours=12)
PENDING_TTL = datetime.timedelta(minutes=10)
NAMES_KEEP = datetime.timedelta(days=30)
KEEP_EXPIRED = 20


def now():
    return datetime.datetime.now().astimezone()


def parse_time(text):
    try:
        return datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def empty():
    return {"version": 1, "running": [], "pending": [], "names": {}, "expired": []}


def state_dir(root):
    return os.path.join(root, ".claude", "state")


def load(root):
    path = os.path.join(state_dir(root), "instances.json")
    if not os.path.isfile(path):
        return empty()
    with open(path, encoding="utf-8") as f:
        state = json.load(f)
    if not isinstance(state, dict):
        raise ValueError("instances.json 不是 JSON 对象")
    for key, value in empty().items():
        state.setdefault(key, value)
    return state


def save(root, state):
    path = os.path.join(state_dir(root), "instances.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


@contextlib.contextmanager
def locked(root):
    """Load the state under an exclusive lock; save it if the block succeeds."""
    os.makedirs(state_dir(root), exist_ok=True)
    with open(os.path.join(state_dir(root), "instances.lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = load(root)
            yield state
            save(root, state)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def prune(state, at=None):
    at = at or now()
    keep = []
    for inst in state["running"]:
        started = parse_time(inst.get("started_at"))
        if started and at - started > EXPIRE:
            inst["expired_at"] = at.isoformat(timespec="seconds")
            state["expired"].append(inst)
        else:
            keep.append(inst)
    state["running"] = keep
    state["expired"] = state["expired"][-KEEP_EXPIRED:]
    state["pending"] = [p for p in state["pending"]
                        if (parse_time(p.get("reserved_at")) or at) > at - PENDING_TTL]
    state["names"] = {k: v for k, v in state["names"].items()
                      if (parse_time(v.get("last_seen")) or at) > at - NAMES_KEEP}


def count(state, agent_type):
    return (sum(1 for i in state["running"] if i.get("agent_type") == agent_type)
            + sum(1 for p in state["pending"] if p.get("agent_type") == agent_type))


def name_in_use(state, session_name):
    return (any(i.get("session_name") == session_name for i in state["running"])
            or any(p.get("session_name") == session_name for p in state["pending"]))


def reserve(state, agent_type, session_name=None, tool_use_id=None, at=None):
    state["pending"].append({"agent_type": agent_type, "session_name": session_name,
                             "tool_use_id": tool_use_id,
                             "reserved_at": (at or now()).isoformat(timespec="seconds")})


def numbered(pol, n):
    return "%s-p%d-%02d" % (pol["abbrev"], pol["rank"], n)


def start(state, agent_id, agent_type, pol, at=None):
    """Record a started (or resumed) instance and return its session name."""
    at = at or now()
    for inst in state["running"]:
        if inst.get("agent_id") == agent_id:
            return inst.get("session_name")
    pending = next((p for p in state["pending"] if p.get("agent_type") == agent_type), None)
    if pending:
        state["pending"].remove(pending)
    taken = {i.get("session_name") for i in state["running"]}
    known = (state["names"].get(agent_id) or {}).get("session_name")
    if pol.get("fixed_session"):
        name = pol["fixed_session"]
    elif known and known not in taken:
        name = known
    elif pol["rank"] == 0:
        name = (pending or {}).get("session_name")
    else:
        n = 1
        while numbered(pol, n) in taken:
            n += 1
        name = numbered(pol, n)
    state["running"].append({"agent_id": agent_id, "agent_type": agent_type, "session_name": name,
                             "started_at": at.isoformat(timespec="seconds")})
    if name:
        state["names"][agent_id] = {"session_name": name, "agent_type": agent_type,
                                    "last_seen": at.isoformat(timespec="seconds")}
    return name


def stop(state, agent_id, at=None):
    state["running"] = [i for i in state["running"] if i.get("agent_id") != agent_id]
    if agent_id in state["names"]:
        state["names"][agent_id]["last_seen"] = (at or now()).isoformat(timespec="seconds")


def context_for(name, pol):
    if name:
        return ("你这一次的会话名是 %s，会话标签是 %s。工作项名称、发出方和回复包标题都写这个会话名。"
                % (name, pol["tag"]))
    return ("派发方没有在提示里写会话名。你的会话名是 %s-p0-<需求包的工作项 ID>，会话标签是 %s；"
            "先从需求包里找到工作项 ID。" % (pol["abbrev"], pol["tag"]))


def focus_session(state, agent_type):
    """Session name of a focus session running as agent_type, if any."""
    for inst in state["running"]:
        if inst.get("agent_type") == agent_type and inst.get("kind") == "focus":
            return inst.get("session_name") or agent_type
    return None


def start_session(state, session_id, agent_type, pol, kind, at=None):
    """Register a top-level session; return (session name, warning or None)."""
    key = "session:" + session_id
    for inst in state["running"]:
        if inst.get("agent_id") == key:
            return inst.get("session_name"), None
    others = count(state, agent_type)
    warning = None
    if others >= pol["limit"]:
        warning = ("%s 已有 %d 个实例在跑（上限 %d），可能是入口派出的子智能体。先回入口确认，"
                   "别让两处同时改同一件事。" % (agent_type, others, pol["limit"]))
    name = start(state, key, agent_type, pol, at)
    state["running"][-1]["kind"] = kind
    return name, warning


def session_event(data, event, policy, root):
    session_id = data.get("session_id") or ""
    agent_type = data.get("agent_type") or policy.get("main_agent") or ""
    pol = policy["agents"].get(agent_type)
    if not session_id or not pol or data.get("agent_id"):
        return 0
    kind = "entry" if agent_type == policy.get("main_agent") else "focus"
    with locked(root) as state:
        prune(state)
        if event == "SessionEnd":
            stop(state, "session:" + session_id)
            return 0
        name, warning = start_session(state, session_id, agent_type, pol, kind)
    text = context_for(name, pol)
    if kind == "focus":
        text += ("这是研究员直接开的聚焦会话。谈定的事写成决策包（渠道：研究员主动发起）交回入口；"
                 "新任务写成需求包草稿。没写进台账的决定不算数。")
    if warning:
        text += warning
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}},
                     ensure_ascii=False))
    return 0


# ------------------------------------------------------------- pool mode
WAITING = ("permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input")


def emit(event, text):
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}},
                         ensure_ascii=False))


def claude_version():
    m = re.search(r"(\d+\.\d+\.\d+)", os.environ.get("CLAUDE_CODE_EXECPATH") or "")
    return m.group(1) if m else None


UNCLAIMED = ("只有开局第一句写着“认领 <角色>（会话名 …）”的会话才认领：照那句话运行 route.py claim，读输出里的角色说明。"
             "第一句没有这句话的，是研究员自己的会话，不要自己认领，也不替池里的角色干活；找人用 route.py pool。")
UNREADABLE = ("会话池的认领表 .claude/state/pool.json 读不出来。照你原来的角色接着干，不要重新认领，也不要改这个文件；"
              "告诉研究员认领表坏了。在修好以前，写台账这类硬规则管的操作都会被拒。")
AGENT_FLAG = ("这是研究员在终端里用 claude --agent %s 开的聚焦会话，项目是会话池模式：照这个角色的说明干活，但没有飞书和台账的写权；"
              "要派活给池里的会话发消息（找人用 route.py pool），谈定的事写成决策包交给入口 PMO-p2-01 登记。")
DESK_CONFLICT = ("本会话和已认领的 %s 共用一个桌面会话 ID，多半是从那个会话里起的进程。它不代表任何角色，没有写权，"
                 "也不读那个会话的收件箱；需要的话请研究员另开会话。")


def pool_start(data, policy, root, sid, desk):
    source = data.get("source") or "startup"
    prev = pool.load_session(root, sid)
    stamp = pool.stamp()

    def change(st):
        st.setdefault("started_at", stamp)
        st.update({"source": source, "version": claude_version(),
                   "desktop_app": os.environ.get("CLAUDE_CODE_DESKTOP_APP_VERSION")})
        if source != "compact":                    # an automatic compaction can happen in the middle of a turn
            st.update({"status": "idle", "status_at": stamp})
        if source == "clear":
            st.pop("prev_status", None)
        if desk:
            st["desk"] = desk
    pool.update_session(root, sid, change)
    if source == "clear":
        pl, ok = pool.load(root)
        if ok and not pool.claim_by_session(pl, sid) and not (desk and pool.claim_by_desk(pl, desk)):
            pair_after_clear(root, sid)            # no desktop ID, or one the claim never recorded
    who = pool.identify(data, policy, root)
    if who.origin == "claim" and desk and who.claim.get("desk") != desk:
        keep_desk(root, who.name, sid, desk)
    if who.origin != "claim":
        if who.origin == "tombstone":
            emit("SessionStart", "本会话原来的认领已经释放，现在不代表任何角色。" + UNCLAIMED)
        elif who.origin in ("permit", "pending"):
            emit("SessionStart", "本会话有 %s 的认领许可，还没转正：照开局的说明运行 route.py claim。" % who.role)
        elif who.origin == "pool_unreadable":
            emit("SessionStart", UNREADABLE)
        elif who.origin == "agent_flag":
            emit("SessionStart", AGENT_FLAG % who.role)
        elif who.origin == "desk_conflict":
            pool.log_event(root, {"event": "desk_conflict", "name": who.conflict, "session_id": sid, "desk": desk or None})
            emit("SessionStart", DESK_CONFLICT % who.conflict)
        else:
            emit("SessionStart", UNCLAIMED)
        return 0
    current = pool.manifest_version(root)
    seen = prev.get("plugin_version")
    reason = "规则已更新（插件 %s → %s），以下面的角色说明为准。" % (seen, current) if seen and current and seen != current else ""
    if source == "clear":
        reason = "上下文清空后接回认领。" + reason
    pool.update_session(root, sid, lambda st: st.update({"plugin_version": current, "name": who.name}))
    pl, _ = pool.load(root)
    emit("SessionStart", pool.injection(root, policy, who.claim, reason, owners=pl.get("owners")))
    return 0


def keep_desk(root, name, sid, desk):
    """A claim made in a terminal (no desktop ID) and continued in the desktop app, or one whose
    desktop ID changed: record the ID the session has now, unless another claim holds it."""
    with pool.locked(root) as pl:
        claim = pl["claims"].get(name)
        other = pool.claim_by_desk(pl, desk)
        if not claim or claim.get("session_id") != sid or (other and other.get("name") != name):
            return
        old, claim["desk"] = claim.get("desk"), desk
    pool.log_event(root, {"event": "desk_updated", "name": name, "session_id": sid, "old": old, "desk": desk})


def pair_after_clear(root, sid):
    """Without a desktop ID, a /clear is paired by time: one SessionEnd(clear) in the last minute."""
    pl, ok = pool.load(root)
    if not ok:
        return
    at = pool.now()
    recent = [r for r in pl["reattach"] if (pool.parse_time(r.get("at")) or at) > at - pool.REATTACH_WINDOW]
    if len(recent) == 1:
        pool.reattach(root, recent[0]["name"], sid, how="clear")
    elif len(recent) > 1:
        pool.log_event(root, {"event": "reattach_ambiguous", "session_id": sid,
                              "names": [r.get("name") for r in recent]})


def pool_event(data, event, policy, root):
    sid = data.get("session_id") or ""
    if not sid:
        return 0
    if data.get("agent_id"):
        if event == "PermissionRequest":           # a subagent's prompt: the Notification after it shows the wait
            pool.update_session(root, sid, lambda st: st.update({"waiting_agent_id": data["agent_id"]}))
        return 0
    desk = os.environ.get(pool.DESK_ENV) or ""
    if event == "SessionStart":
        return pool_start(data, policy, root, sid, desk)
    if event == "SessionEnd":
        reason = data.get("reason") or ""

        def ended(st):
            if st.get("status") != "offline":
                st["prev_status"], st["prev_status_at"] = st.get("status"), st.get("status_at")
            st.update({"status": "offline", "status_at": pool.stamp(), "ended_reason": reason})
        pool.update_session(root, sid, ended)
        if reason == "clear":
            pl, ok = pool.load(root)
            claim = pool.claim_by_session(pl, sid) if ok else None
            if claim:
                with pool.locked(root) as pl:
                    pl["reattach"].append({"name": claim["name"], "old_session_id": sid, "at": pool.stamp()})
        return 0
    if event == "Stop":
        msg = data.get("last_assistant_message") or ""
        pool.set_status(root, sid, "idle_bg" if data.get("background_tasks") else "idle", last_message=msg[:200])
        parsed = pool.parse_message(policy, msg)
        if parsed:
            pl, ok = pool.load(root)
            claim = pool.claim_by_session(pl, sid) if ok else None
            pool.log_event(root, {"event": "sent", "kind": parsed["kind"], "id": parsed["id"], "from": parsed["from"],
                                  "to": parsed["to"], "by": (claim or {}).get("name"), "session_id": sid,
                                  "line": parsed["line"]})
        return 0
    if event == "StopFailure":
        pool.set_status(root, sid, "failed:%s" % (data.get("error") or "unknown"),
                        last_message=(data.get("last_assistant_message") or "")[:200])
        return 0
    if event == "PermissionRequest":
        pool.set_status(root, sid, "waiting_researcher", waiting_for=data.get("tool_name"),
                        waiting_tool_use_id=data.get("tool_use_id"), waiting_self=True)
        return 0
    if event == "Notification":
        kind = data.get("notification_type") or ""
        if kind in WAITING:
            def wait(st):
                if not str(st.get("status", "")).startswith("waiting"):
                    st["status_before_wait"] = st.get("status")
                st.update({"status": "waiting_researcher", "status_at": pool.stamp(), "waiting_for": kind})
            pool.update_session(root, sid, wait)
        elif kind in ("elicitation_response", "elicitation_complete"):
            def answered(st):
                if str(st.get("status", "")).startswith("waiting"):
                    st["status"] = st.get("status_before_wait") or "busy"
                    st["status_at"] = pool.stamp()
                    for k in pool.WAIT_KEYS:
                        st.pop(k, None)
            pool.update_session(root, sid, answered)
        elif kind == "idle_prompt":
            if not str(pool.load_session(root, sid).get("status", "")).startswith("failed"):
                pool.set_status(root, sid, "idle")
        elif kind.startswith("quota_auto_resume"):
            pool.update_session(root, sid, lambda st: st.update({"quota": kind, "quota_at": pool.stamp()}))
        return 0
    if event == "PermissionDenied":
        pool.update_session(root, sid, lambda st: st.update({"denied": st.get("denied", 0) + 1,
                                                             "last_denied": (data.get("reason") or "")[:200]}))
        return 0
    if event == "PostCompact":
        pl, ok = pool.load(root)
        claim = pool.claim_by_session(pl, sid) if ok else None
        name = (claim or {}).get("name") or "session-" + sid[:8]
        path = os.path.join(root, ".claude", "logs", "compact", re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": pool.stamp(), "session_id": sid, "trigger": data.get("trigger"),
                                "summary": data.get("compact_summary")}, ensure_ascii=False) + "\n")
        return 0
    return 0


POOL_EVENTS = ("SessionStart", "SessionEnd", "Stop", "StopFailure", "Notification", "PermissionRequest",
               "PermissionDenied", "PostCompact")


def main():
    try:
        data = json.load(sys.stdin)
        event = data.get("hook_event_name")
        agent_type = data.get("agent_type") or ""
        agent_id = data.get("agent_id") or ""
        with open(os.path.join(HERE, "policy.json"), encoding="utf-8") as f:
            policy = json.load(f)
        root = os.path.normpath(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
        if policy.get("mode") == "pool" and event in POOL_EVENTS:
            return pool_event(data, event, policy, root)
        if event in ("SessionStart", "SessionEnd"):
            return session_event(data, event, policy, root)
        pol = policy["agents"].get(agent_type)
        if not pol or not agent_id or event not in ("SubagentStart", "SubagentStop"):
            return 0
        if policy.get("mode") == "pool":           # pool names belong to sessions; a subagent signs as its parent
            if event == "SubagentStart":
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": (
                    "你是开你的会话临时派出的 %s 子智能体，不是会话池里的会话，没有自己的会话名，也没有飞书写权。"
                    "不要用 %s 这样的池位名署名；包、评论和交回都写开你的会话的名字，结果交回给它。" % (
                        agent_type, pool.slot_name(pol, 1)))}}, ensure_ascii=False))
            return 0
        with locked(root) as state:
            prune(state)
            if event == "SubagentStop":
                stop(state, agent_id)
                return 0
            name = start(state, agent_id, agent_type, pol)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart",
                                                 "additionalContext": context_for(name, pol)}},
                         ensure_ascii=False))
    except Exception as e:  # never block
        sys.stderr.write("[instances] 记录实例出错：%s\n" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
