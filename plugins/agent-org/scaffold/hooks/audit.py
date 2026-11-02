#!/usr/bin/env python3
"""PostToolUse / PostToolUseFailure audit log for Feishu write commands.

Appends one JSON line per lark-cli or meegle write invocation to
feishu/write-log.jsonl: time, session, agent, command (redacted), result,
returned IDs and meegle trace IDs. Read-only commands are not logged. The
hook never blocks; problems are written to the log as audit_error records.
"""
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import feishu_cmd as fc  # noqa: E402

SECRET_FLAGS = {"--app-secret", "--secret", "--password", "--access-token", "--user-access-token",
                "--tenant-access-token", "--client-secret"}
ID_KEYS = ("work_item_id", "node_token", "obj_token", "document_id", "space_id", "chat_id",
           "message_id", "comment_id", "view_id", "record_id", "url")
TRACE_KEYS = ("logid", "log_id", "trace_id", "x-tt-logid")
MAX_VALUE = 300


def redact(args):
    out, hide = [], False
    for a in args:
        if hide:
            out.append("***")
            hide = False
            continue
        if a in SECRET_FLAGS:
            hide = True
        elif "=" in a and a.split("=", 1)[0] in SECRET_FLAGS:
            a = a.split("=", 1)[0] + "=***"
        a = re.sub(r"(?i)bearer\s+\S+", "Bearer ***", a)
        if len(a) > MAX_VALUE:
            a = a[:MAX_VALUE] + "…（共 %d 字）" % len(a)
        out.append(a)
    return out


def harvest(text, keys):
    found = {}
    for k in keys:
        for m in re.finditer(r'"%s"\s*:\s*("([^"]*)"|-?\d+)' % re.escape(k), text or ""):
            value = m.group(2) if m.group(2) is not None else m.group(1)
            bucket = found.setdefault(k, [])
            if value not in bucket and len(bucket) < 5:
                bucket.append(value)
    return found


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    root = os.path.normpath(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
    log_path = os.path.join(root, "feishu", "write-log.jsonl")
    try:
        if data.get("tool_name") != "Bash":
            return 0
        command = (data.get("tool_input") or {}).get("command") or ""
        parsed = fc.parse(command, data.get("cwd") or root)
        risk = fc.load_json(os.path.join(HERE, "lark-risk.json"), {"commands": {}})
        event = data.get("hook_event_name") or "PostToolUse"
        response = data.get("tool_response") or {}
        stdout = response.get("stdout", "") if isinstance(response, dict) else str(response)
        stderr = response.get("stderr", "") if isinstance(response, dict) else ""
        records = []
        for inv in parsed.invocations:
            c = fc.classify(inv, risk)
            if c["help"] or c["tier"] == "read":
                continue
            failed = event == "PostToolUseFailure" or '"ok": false' in stdout or '"ok":false' in stdout
            records.append({
                "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "event": event,
                "session_id": data.get("session_id"),
                "agent_type": data.get("agent_type") or "main",
                "agent_id": data.get("agent_id"),
                "tool_use_id": data.get("tool_use_id"),
                "cli": "lark-cli" if inv.cli == "lark" else "meegle",
                "command_key": c["key"],
                "tier": c["tier"],
                "dry_run": c["dry_run"],
                "args": redact(inv.args),
                "ok": not failed,
                "error": (data.get("error") or stderr or "")[:500] if failed else None,
                "ids": harvest(stdout, ID_KEYS),
                "trace": harvest(stdout, TRACE_KEYS),
            })
        if records:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception as e:  # never block; leave a trace instead
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"audit_error": str(e)[:300]}, ensure_ascii=False) + "\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
