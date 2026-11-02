#!/usr/bin/env python3
"""UserPromptSubmit hook: show a session the messages waiting in its inbox.

Messages live in inbox/<role>.jsonl, one JSON object per line, written by the
entry with `route.py notify`. The entry session also sees inbox/feishu.jsonl,
where the Feishu DM listener appends the researcher's messages. Each message is
shown once and then marked read. This hook never blocks: problems go to stderr
and the prompt goes through.
Python 3.9 standard library only.
"""
import fcntl
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_SHOWN = 20


def take_unread(path):
    """Return unread messages in path and mark them read, under a lock."""
    if not os.path.isfile(path):
        return []
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            out, unread = [], []
            for line in lines:
                try:
                    msg = json.loads(line)
                except ValueError:
                    out.append(line)
                    continue
                if not msg.get("read") and len(unread) < MAX_SHOWN:
                    unread.append(msg)
                    msg["read"] = True
                out.append(json.dumps(msg, ensure_ascii=False))
            if unread:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write("\n".join(out) + "\n")
                os.replace(tmp, path)
            return unread
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def describe(msg):
    head = msg.get("ts", "")[:16]
    if msg.get("from"):
        head += " 来自 %s" % msg["from"]
    if msg.get("work_item"):
        head += "，工作项 #%s" % msg["work_item"]
    return "- %s：%s" % (head, (msg.get("text") or "").strip())


def main():
    try:
        data = json.load(sys.stdin)
        with open(os.path.join(HERE, "policy.json"), encoding="utf-8") as f:
            policy = json.load(f)
        if data.get("agent_id"):
            return 0
        role = data.get("agent_type") or policy.get("main_agent")
        if role not in policy["agents"]:
            return 0
        root = os.path.normpath(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
        inbox = os.path.join(root, "inbox")
        msgs = take_unread(os.path.join(inbox, role + ".jsonl"))
        feishu = take_unread(os.path.join(inbox, "feishu.jsonl")) if role == policy.get("main_agent") else []
        if not msgs and not feishu:
            return 0
        parts = []
        if msgs:
            parts.append("收件箱里有 %d 条给你的消息，先处理再回答研究员：\n%s" % (len(msgs), "\n".join(describe(m) for m in msgs)))
        if feishu:
            parts.append("研究员在飞书私聊里发来 %d 条消息，照路由决策表处理：\n%s" % (len(feishu), "\n".join(describe(m) for m in feishu)))
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": "\n\n".join(parts)}}, ensure_ascii=False))
    except Exception as e:  # never block the prompt
        sys.stderr.write("[inbox] 读取收件箱出错：%s\n" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
