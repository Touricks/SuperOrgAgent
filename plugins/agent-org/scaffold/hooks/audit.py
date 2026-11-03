#!/usr/bin/env python3
"""PostToolUse / PostToolUseFailure audit log for Feishu write commands.

Appends one JSON line per lark-cli or meegle write invocation to
feishu/write-log.jsonl: time, session, agent, command (redacted), result,
returned IDs and meegle trace IDs. Read-only commands are not logged.

Also:
- pool mode: completes a claim in two steps. `route.py claim` (whose Bash sees the
  desktop session ID) leaves a pending entry; when that command has run in the
  same session, outside any subagent, this hook turns it into the claim.
- CLI mode: releases the slot guard.py reserved for an Agent call that ended
  without starting a subagent.
The hook never blocks; problems are written to the log as audit_error records.
"""
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import feishu_cmd as fc  # noqa: E402
import instances  # noqa: E402
import pool  # noqa: E402

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


CLAIM_RUN = re.compile(r"route\.py['\"]?\s+claim(?![\w-])")


def finish_claim(data, policy, root, command):
    """The second step of a claim: the pending entry this session's route.py claim left."""
    if (policy.get("mode") != "pool" or data.get("agent_id") or not CLAIM_RUN.search(command)
            or data.get("hook_event_name") != "PostToolUse"):
        return
    response = data.get("tool_response") or {}
    stdout = response.get("stdout", "") if isinstance(response, dict) else str(response)
    if "待转正" not in stdout:
        return
    sid = data.get("session_id") or ""
    done = []
    with pool.locked(root) as pl:
        for pend in pool.pending_for(pl, sid):
            if pend.get("kind") in ("attach", "desk"):
                claim = pl["claims"].get(pend.get("name"))
                other = pool.claim_by_desk(pl, pend.get("desk"))
                if claim and other not in (None, claim):
                    done.append(("desk_conflict", claim["name"], "桌面会话 ID 已属于 %s" % other["name"]))
                elif claim and pend["kind"] == "desk" and claim.get("session_id") == sid:
                    claim["desk"] = pend.get("desk") or claim.get("desk")
                    done.append(("desk", claim["name"], None))
                elif claim and pend["kind"] == "attach" and pool.cleared(root, claim):   # only after /clear
                    claim["session_id"] = sid
                    if pend.get("desk"):
                        claim["desk"] = pend["desk"]
                    done.append(("attach", claim["name"], None))
                pl["pending"].remove(pend)
                continue
            permit = pool.permit_for(pl, None, pend.get("desk")) or pool.permit_for(pl, sid, None)
            if not permit or permit.get("role") != pend.get("role"):
                continue
            claim, note = pool.promote(pl, policy, permit, sid, pend.get("desk"))
            done.append(("claim" if claim else "claim_refused", (claim or permit).get("name"), note))
    for kind, name, note in done:
        pool.log_event(root, {"event": kind, "how": "two-step", "session_id": sid, "name": name, "note": note})
        if kind in ("claim", "attach"):
            pool.mark_claimed(root, sid, {"name": name})


def answered(root, sid, agent_id=None):
    """A tool call finished, so a permission prompt for it was answered (see pool.end_wait for
    whose wait a subagent's call ends)."""
    if not str(pool.load_session(root, sid).get("status", "")).startswith("waiting"):
        return
    pool.update_session(root, sid, lambda st: pool.end_wait(st, agent_id))


def release_slot(data, policy, root):
    """CLI mode: an Agent call that failed keeps no reserved slot. A call that succeeded started a
    subagent, whose SubagentStart takes the slot (for a background agent, possibly after this hook)."""
    if (policy.get("mode") == "pool" or data.get("tool_name") not in ("Agent", "Task")
            or data.get("hook_event_name") != "PostToolUseFailure"):
        return
    tid = data.get("tool_use_id")
    if not tid:
        return
    with instances.locked(root) as state:
        state["pending"] = [p for p in state["pending"] if p.get("tool_use_id") != tid]


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    root = os.path.normpath(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
    log_path = os.path.join(root, "feishu", "write-log.jsonl")
    try:
        policy = fc.load_json(os.path.join(HERE, "policy.json"), {}) or {}
        command = (data.get("tool_input") or {}).get("command") or ""
        if policy:
            release_slot(data, policy, root)
            if policy.get("mode") == "pool":
                answered(root, data.get("session_id") or "", data.get("agent_id"))
        tool = str(data.get("tool_name") or "")
        if tool not in ("Bash", "Monitor") and not tool.endswith("run_in_terminal"):
            return 0
        if policy:
            finish_claim(data, policy, root, command)
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
