#!/usr/bin/env python3
"""UserPromptSubmit hook: show a session the messages waiting in its inbox.

Messages live in inbox/<role>.jsonl, one JSON object per line, written with
`route.py notify`. The entry session also sees inbox/feishu.jsonl, where the
Feishu DM listener appends the researcher's messages. Each message is shown once
and marked read only after the text is built; what does not fit in the context
budget stays unread for the next prompt.

In pool mode the hook also:
- turns the researcher's opening line in a new top-level session
  ("认领 <+2 角色>（会话名 …）") into a claim and injects the role text;
- marks the session busy and records its permission mode;
- recognises a message header at the start of one of the first lines
  (【派活 #ID】A → B …), records it in .claude/logs/dispatch.jsonl (its time is
  the receipt), makes the receiver the work item's owner on 【派活】 addressed to
  it, and adds reminders where the message breaks the dispatch norms; a +0 work
  name (Alg-p0-2101) stands for the claim that owns that work item;
- reads inbox/<session name>.jsonl (and inbox/<role>.jsonl for one-session roles)
  and adds one line saying who this session is.
The researcher's own session reads no inbox. This hook never blocks: problems go
to stderr and the prompt goes through.
Python 3.9 standard library only.
"""
import fcntl
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pool  # noqa: E402

MAX_SHOWN = 20


def key_of(msg):
    return msg.get("id") or "%s|%s" % (msg.get("ts"), msg.get("text"))


def peek_unread(path):
    """Unread messages in path, oldest first; lines that are not JSON objects are skipped."""
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and not msg.get("read"):
                out.append(msg)
    return out


def mark_read(path, keys):
    """Mark the messages with these keys read, under a lock; other lines are kept as they are."""
    if not keys or not os.path.isfile(path):
        return
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            out = []
            for line in lines:
                try:
                    msg = json.loads(line)
                except ValueError:
                    out.append(line)
                    continue
                if isinstance(msg, dict) and not msg.get("read") and key_of(msg) in keys:
                    msg["read"] = True
                    line = json.dumps(msg, ensure_ascii=False)
                out.append(line)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(out) + "\n")
            os.replace(tmp, path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def take_unread(path):
    """Return unread messages in path and mark them read (kept for callers that show them all)."""
    msgs = peek_unread(path)[:MAX_SHOWN]
    mark_read(path, {key_of(m) for m in msgs})
    return msgs


def describe(msg):
    head = str(msg.get("ts", ""))[:16]
    if msg.get("from"):
        head += " 来自 %s" % msg["from"]
    if msg.get("work_item"):
        head += "，工作项 #%s" % msg["work_item"]
    return "- %s：%s" % (head, str(msg.get("text") or "").strip())


def inbox_text(sources, used, budget=pool.CONTEXT_LIMIT, counts_only=False):
    """(text parts, {path: keys to mark read}) for the inboxes in sources [(path, title, action)],
    fitting what is left of the budget; with counts_only, just say how many wait and leave them unread."""
    parts, marks = [], {}
    for path, title, action in sources:
        msgs = peek_unread(path)
        if not msgs:
            continue
        if counts_only:
            parts.append("%s还有 %d 条消息没看，下一轮对话时显示。" % (title, len(msgs)))
            continue
        lines, keys = [], set()
        for m in msgs[:MAX_SHOWN]:
            line = describe(m)
            room = budget - used - 160
            if len(line) > room:
                if lines or room < 400:
                    break
                line = line[:room - 60] + "……（全文在 inbox/%s，id %s）" % (os.path.basename(path), m.get("id", "?"))
            lines.append(line)
            keys.add(key_of(m))
            used += len(line) + 1
        if lines:
            more = len(msgs) - len(lines)
            parts.append("%s有 %d 条消息，%s：\n%s%s" % (
                title, len(lines), action, "\n".join(lines), "\n（另有 %d 条，下一轮显示）" % more if more else ""))
            marks[path] = keys
        else:
            parts.append("%s还有 %d 条消息没看，下一轮对话时显示。" % (title, len(msgs)))
    return parts, marks


INBOX = "先处理再回答研究员"
GENERATED = re.compile(r"\s*<[a-z]+(?:-[a-z]+)+[\s>]")     # <task-notification> and other text Claude Code writes
FEISHU = "照路由决策表处理"


def pool_prompt(data, policy, root):
    sid = data.get("session_id") or ""
    if not sid:
        return [], {}
    desk = os.environ.get(pool.DESK_ENV) or ""
    prompt = data.get("prompt") or ""
    parts = []
    prev = pool.load_session(root, sid)
    opened = False
    if not prev.get("prompts"):
        claim, note = pool.opening_permit(root, policy, sid, desk, prompt)
        if claim:
            pl, _ = pool.load(root)
            parts.append(pool.injection(root, policy, claim, "已按开局句认领。", owners=pl.get("owners")))
            opened = True
        if note:
            parts.append("认领：" + note)
    who = pool.identify(data, policy, root)
    msg = pool.parse_message(policy, prompt) if who.origin == "claim" else None
    generated = bool(GENERATED.match(prompt))           # a background task's report, not the researcher
    stamp = pool.stamp()

    def change(st):
        st.update({"status": "busy", "status_at": stamp, "prompts": st.get("prompts", 0) + 1,
                   "turn_origin": "message" if msg else "other"})
        for k in ("quota", "quota_at", "prev_status", "prev_status_at") + pool.WAIT_KEYS:
            st.pop(k, None)
        if data.get("permission_mode"):
            st["permission_mode"] = data["permission_mode"]
        if who.name and who.origin == "claim":
            st["name"] = who.name
        if msg and msg.get("id") and msg["kind"] in ("assign", "resume"):
            st["work_item"] = msg["id"]
    pool.update_session(root, sid, change)
    if who.origin in ("permit", "pending"):
        return parts + ["本会话有 %s 的认领许可，还没转正：照开局的说明运行 route.py claim。" % who.role], {}
    if who.origin != "claim":
        return parts, {}
    me = who.claim
    pl, ok = pool.load(root)
    back = not msg and not generated                # the researcher typed this prompt
    if ok and me["name"] in (pl.get("focus") or {}) and pool.focus_expired(prev):
        with pool.locked(root) as locked_pool:
            if back:                                # the researcher came back to this session
                locked_pool["focus"][me["name"]]["opened_at"] = pool.stamp()
            else:
                locked_pool["focus"].pop(me["name"], None)
        pool.log_event(root, {"event": "focus_renewed" if back else "focus_expired", "name": me["name"]})
        if not back:                                # say so when the researcher comes back
            pool.update_session(root, sid, lambda st: st.update({"focus_lapsed": True}))
    elif prev.get("focus_lapsed") and back and me["name"] not in (pl.get("focus") or {}):
        parts.append("“研究员在用”的标记已因空闲 30 分钟失效。研究员接着和你谈的话，先重新运行 route.py focus open --topic '…'。")
        pool.update_session(root, sid, lambda st: st.pop("focus_lapsed", None))
    if msg and ok:
        msg["from"] = pool.resolve(policy, pl, msg["from"]) if msg.get("from") else None
        mine = pool.addressed_to(policy, pl, msg, me)
        hits = pool.receive_checks(policy, root, pl, msg, me)
        pool.log_event(root, {"event": "message" if mine else "message_misaddressed", "kind": msg["kind"],
                              "id": msg["id"], "from": msg["from"], "to": me["name"] if mine else msg.get("to"),
                              "header_to": msg.get("to"), "seen_by": me["name"], "session_id": sid,
                              "line": msg["line"], "soft_hits": hits})
        if msg["kind"] == "assign" and msg.get("id") and mine:
            with pool.locked(root) as locked_pool:
                locked_pool["owners"][msg["id"]] = me["name"]
        if hits:
            parts.append("这条消息有几处和规范对不上（只提醒，不拦）：\n" + "\n".join("- " + h for h in hits))
    inbox = os.path.join(root, "inbox")
    sources = [(os.path.join(inbox, me["name"] + ".jsonl"), "收件箱里", INBOX)]
    if pool.role_limit(policy, me["role"]) == 1:
        sources.append((os.path.join(inbox, me["role"] + ".jsonl"), "按角色的收件箱里", INBOX))
    if me["role"] == policy.get("entry_role"):
        sources.append((os.path.join(inbox, "feishu.jsonl"), "研究员的飞书私聊里", FEISHU))
    reminder = pool.reminder_line(policy, me)
    used = sum(len(p) for p in parts) + len(reminder)
    more, marks = inbox_text(sources, used, counts_only=opened)
    return parts + more + [reminder], marks


def main():
    try:
        data = json.load(sys.stdin)
        with open(os.path.join(HERE, "policy.json"), encoding="utf-8") as f:
            policy = json.load(f)
        if data.get("agent_id"):
            return 0
        root = os.path.normpath(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
        if policy.get("mode") == "pool":
            parts, marks = pool_prompt(data, policy, root)
        else:
            role = data.get("agent_type") or policy.get("main_agent")
            if role not in policy["agents"]:
                return 0
            inbox = os.path.join(root, "inbox")
            sources = [(os.path.join(inbox, role + ".jsonl"), "收件箱里", INBOX)]
            if role == policy.get("main_agent"):
                sources.append((os.path.join(inbox, "feishu.jsonl"), "研究员的飞书私聊里", FEISHU))
            parts, marks = inbox_text(sources, 0)
        if parts:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                     "additionalContext": "\n\n".join(parts)}}, ensure_ascii=False))
            sys.stdout.flush()
        for path, keys in marks.items():          # only what was shown is marked read
            mark_read(path, keys)
    except Exception as e:  # never block the prompt
        sys.stderr.write("[inbox] 读取收件箱出错：%s\n" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
