#!/usr/bin/env python3
"""Track running org instances in .claude/state/instances.json.

As a hook (SubagentStart / SubagentStop / SessionStart / SessionEnd):
- SubagentStart records the instance. +1 and +2 get the smallest free sequence
  number; +0 takes the session name the dispatcher wrote in its prompt (reserved
  by guard.py at PreToolUse); pmo-scribe is always PMO-p0-01. The session name is
  passed to the subagent through additionalContext.
- SubagentStop removes the instance.
- SessionStart registers a top-level session that runs as an org role: the entry
  (main agent from settings) or a focus session started with `claude --agent <role>`.
  It counts against the same limits, so the entry cannot dispatch a +2 the
  researcher is talking to. SessionEnd removes it.
Entries older than 12 hours without a Stop are expired. This hook never blocks:
SubagentStart cannot block, and exit 2 on SubagentStop would keep the subagent
running, so every error exits 0.

guard.py imports this module to count instances and reserve slots on Agent calls.
Python 3.9 standard library only.
"""
import contextlib
import datetime
import fcntl
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
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


def main():
    try:
        data = json.load(sys.stdin)
        event = data.get("hook_event_name")
        agent_type = data.get("agent_type") or ""
        agent_id = data.get("agent_id") or ""
        with open(os.path.join(HERE, "policy.json"), encoding="utf-8") as f:
            policy = json.load(f)
        root = os.path.normpath(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
        if event in ("SessionStart", "SessionEnd"):
            return session_event(data, event, policy, root)
        pol = policy["agents"].get(agent_type)
        if not pol or not agent_id or event not in ("SubagentStart", "SubagentStop"):
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
