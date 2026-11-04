#!/usr/bin/env python3
"""Codex hook adapter. Reuses upstream command parsing, not Claude identity.

Best-effort command guardrails are not an OS isolation boundary. A trusted,
working PreToolUse hook and role binding are prerequisites for ledger writes.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import codex_runtime as runtime
import project_paths


def modules():
    # Source checkout and the self-contained generated .agent-org/bin layout.
    source = Path(HERE).parent / "scaffold/hooks"
    if source.is_dir():
        sys.path.insert(0, str(source))
    import guard
    import feishu_cmd
    return guard, feishu_cmd


def patch_paths(value):
    if isinstance(value, dict):
        value = value.get("patch") or value.get("input") or ""
    if not isinstance(value, str) or "*** Begin Patch" not in value:
        raise runtime.RuntimeError("无法识别 apply_patch 的路径，停止这次编辑")
    paths = re.findall(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", value, re.M)
    if not paths:
        raise runtime.RuntimeError("apply_patch 没有可核对的文件路径")
    return paths


def protect_path(pool, policy, path, cwd):
    guard, _ = modules()
    lexical = Path(os.path.abspath(os.path.join(cwd, path)))
    for full in (lexical, lexical.resolve()):
        try:
            rel = full.relative_to(pool.root).as_posix()
        except ValueError:
            continue
        if guard.matches(rel, policy.get("h_paths", [])):
            return "评测保护路径保持只读：" + rel
        if rel in ("AGENTS.md", "spec/task.json") or rel.startswith((".agent-org/", ".codex/")):
            return "角色、运行状态和配置由初始化/会话命令维护：" + rel
    return None


def evaluate(pool, state, event, role):
    policy = runtime.read(pool.base / "policy.json")
    risk = runtime.read(pool.base / "lark-risk.json")
    guard, fc = modules()
    tool = (event.get("tool_name") or "").split(".")[-1]
    data = event.get("tool_input") or {}
    if isinstance(data, str) and tool != "apply_patch":
        try:
            data = json.loads(data)
        except ValueError:
            raise runtime.RuntimeError("无法解析工具输入")
    cwd = event.get("cwd") or str(pool.root)
    if tool in ("apply_patch", "Edit", "Write", "NotebookEdit"):
        if tool == "apply_patch":
            paths = patch_paths(data)
        else:
            paths = [data.get("file_path") or data.get("notebook_path")]
        for path in paths:
            if not path:
                raise runtime.RuntimeError("编辑工具没有路径")
            reason = protect_path(pool, policy, path, cwd)
            if reason:
                return reason
    if tool in ("Bash", "exec_command", "shell_command", "shell"):
        command = data.get("command") or data.get("cmd") or ""
        if isinstance(command, list):
            command = " ".join(command)
        cwd = data.get("workdir") or cwd
        if not isinstance(command, str):
            raise runtime.RuntimeError("无法解析 shell 命令")
        if guard.git_switch(command, str(pool.root), cwd):
            return "共享会话池不切换、合并或清理当前工作树；先协调所有使用者"
        manifest = runtime.read(pool.base / "manifest.json", {})
        fc.TRUSTED = {str(pool.root / rel) for rel, expected in manifest.get("files", {}).items()
                      if rel.startswith(".agent-org/bin/") and rel.endswith(".py")
                      and (pool.root / rel).is_file()
                      and hashlib.sha256((pool.root / rel).read_bytes()).hexdigest() == expected}
        source = manifest.get("setup_source", {})
        setup_path = Path(source.get("path", ""))
        if setup_path.is_file() and hashlib.sha256(setup_path.read_bytes()).hexdigest() == source.get("sha256"):
            fc.TRUSTED.add(str(setup_path))
            for argv, where in guard.command_runs(command, cwd, str(pool.root)):
                if argv and re.search(r"python[0-9.]*$", os.path.basename(argv[0])):
                    kind, script, rest = fc.python_target(argv[1:])
                    if kind == "script" and script and Path(os.path.join(where or cwd, script)).resolve() == setup_path.resolve():
                        if role and "--apply" in rest:
                            return "已认领的角色不修改项目初始化与空间绑定；在研究员的初始化会话核对后执行"
        write = guard.ledger_write(command, cwd)
        if write and role != "pmo-scribe" and not (write == "comment" and role):
            return "正式登记和状态修改只由已绑定书记员执行；其他会话交任务包"
        parsed = fc.parse(command, cwd)
        if parsed.mentions and (parsed.error or parsed.opaque):
            return "无法完整检查飞书命令，请使用已校验的 ledger.py 入口"
        for inv in parsed.invocations:
            item = fc.classify(inv, risk)
            if item["help"] or item["dry_run"]:
                continue
            if item["tier"] != "read" or item["yes"]:
                return "飞书写入通过 ledger.py 执行，保留绑定校验、回读和回执；不直接调用 CLI 写命令"
    return None


def handle(event, root=None):
    if not isinstance(event, dict):
        raise runtime.RuntimeError("hook 输入不是对象")
    name = event.get("hook_event_name")
    sid = event.get("session_id")
    if not isinstance(sid, str) or not sid:
        raise runtime.RuntimeError("hook 缺少 session_id")
    if root is None:
        installed = Path(HERE).parent
        if installed.name == ".agent-org" and (installed / "manifest.json").is_file():
            root = installed.parent
    if root is None:
        cwd = Path(event.get("cwd") or os.getcwd()).resolve()
        root = next((p for p in (cwd, *cwd.parents) if (p / ".agent-org/manifest.json").is_file()), None)
    if root is None:
        return {}
    pool = runtime.Pool(root)
    state = pool.load()
    slot, binding = pool.binding_for(state, sid)
    subagent = bool(event.get("agent_id") or event.get("agent_type"))
    role = binding["role"] if binding and not subagent else None
    reason = evaluate(pool, state, event, role) if name == "PreToolUse" else None
    # Subagent hook session_id can be the parent's. Do not overwrite its role/status/receipt.
    if not subagent:
        with pool.locked() as current:
            current["hooks_seen"][name] = runtime.now()
            session = current["sessions"].setdefault(sid, {})
            session.update(last_event=name, last_seen_at=runtime.now(), subagent=False)
            if name == "PreToolUse":
                session["pretool_seen"] = runtime.now()
                session["status"] = "working"
            elif name == "Stop": session["status"] = "turn_ended"
            elif name == "Interrupt": session["status"] = "interrupted"
            elif name == "SessionEnd": session["status"] = "session_ended"
            elif name == "UserPromptSubmit":
                session["status"] = "working"
                prompt = event.get("prompt") or ""
                match = re.match(r"^\[agent-org:([a-f0-9-]+)\]", prompt)
                m = current["messages"].get(match[1]) if match else None
                if m and m["to"] == slot and m["prompt"] == prompt:
                    m.setdefault("received_at", runtime.now())
                    if not m.get("handled_at"):
                        m["status"] = "received"
                    session["origin"] = "pool_message"
                else:
                    session["origin"] = "unclassified"  # Never infer human approval from a missing header.
    if reason:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": reason}}
    if name in ("SessionStart", "UserPromptSubmit"):
        context = pool.context(sid) if not subagent else {"role": None, "note": "子智能体不继承父会话的池位和飞书写入身份"}
        return {"hookSpecificOutput": {"hookEventName": name,
            "additionalContext": "agent-org 身份与待办：" + json.dumps(context, ensure_ascii=False)}}
    return {}


def main():
    try:
        print(json.dumps(handle(json.load(sys.stdin)), ensure_ascii=False))
        return 0
    except Exception as e:
        # The documented blocking exit code; never silently turn a corrupt state into no role.
        print("agent-org hook 无法完成检查：%s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
