#!/usr/bin/env python3
"""Durable Codex chat bindings and receipts; no private app DB or network access.

The skill executes native app tools. This CLI records their identities/results and
produces the next actions. No recorded intention is reported as a completed tool call.
"""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project_paths


class RuntimeError(ValueError):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as e:
        raise RuntimeError("状态无法读取，不按空池恢复：%s：%s" % (path, e))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pool-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def empty():
    return {"version": 1, "workspace": None, "bindings": {}, "pending": {}, "history": [],
            "messages": {}, "sessions": {}, "hooks_seen": {}}


class Pool:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.base = self.root / ".agent-org"
        self.file = self.base / "state/pool.json"
        self.config = read(self.base / "pool-plan.json")
        if not isinstance(self.config, dict) or self.config.get("runtime") != "codex":
            raise RuntimeError("项目尚未初始化为 Codex 会话池")
        self.slots = {s["name"]: s for s in self.config["slots"]}

    def load(self):
        data = read(self.file, empty())
        if not isinstance(data, dict) or data.get("version") != 1 or any(
                not isinstance(data.get(k), dict) for k in ("bindings", "pending", "messages", "sessions", "hooks_seen")):
            raise RuntimeError("会话池格式不正确，请核对原记录，不要清空重建")
        identities = [(v.get("host_id"), v.get("thread_id")) for v in data["bindings"].values()]
        if len(set(identities)) != len(identities):
            raise RuntimeError("多个池位绑定到同一个会话，停止角色授权")
        for name, binding in data["bindings"].items():
            if name not in self.slots or binding.get("role") != self.slots[name]["role"]:
                raise RuntimeError("池位与角色计划不一致：" + name)
        return data

    @contextlib.contextmanager
    def locked(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.file.with_suffix(".lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            state = self.load()
            yield state
            save(self.file, state)

    def configure(self, host, project):
        if not host or not project:
            raise RuntimeError("必须从原生项目工具取得 hostId 和 projectId")
        with self.locked() as state:
            value = {"host_id": host, "project_id": project}
            if state["workspace"] and state["workspace"] != value:
                raise RuntimeError("池已绑定其他 Codex 项目；不能静默改绑")
            state["workspace"] = value
        return value

    def plan(self):
        state = self.load()
        actions = []
        for slot in self.config["slots"]:
            name = slot["name"]
            binding = state["bindings"].get(name)
            if binding:
                continue
            elif name in state["pending"]:
                actions.append({"action": "reconcile_creation", "slot": name, **state["pending"][name]})
            else:
                actions.append({"action": "reserve_then_create", **slot})
        bindings = {name: dict(b, activity=state["sessions"].get(b["thread_id"], {}))
                    for name, b in state["bindings"].items()}
        return {"workspace": state["workspace"], "actions": actions, "bindings": bindings,
                "messages": state["messages"], "history": state["history"],
                "priority_order": self.config.get("priority_order", []),
                "sidebar_note": "角色留在项目分组；关键入口顺序见 org/会话入口.md，原生工具暂不支持项目内固定排序"}

    def reserve(self, name):
        if name not in self.slots:
            raise RuntimeError("没有这个池位：" + name)
        with self.locked() as state:
            if not state["workspace"]:
                raise RuntimeError("先用 configure 绑定已核对的 Codex 项目")
            if name in state["bindings"]:
                return {"action": "reuse", **state["bindings"][name]}
            if name in state["pending"]:
                return {"action": "reconcile_creation", **state["pending"][name]}
            token = str(uuid.uuid4())
            slot = self.slots[name]
            state["pending"][name] = {"token": token, "status": "reserved", "created_at": now()}
            prompt = ("你是项目「%s」的 %s。先阅读项目 AGENTS.md，然后运行 "
                      "`python3 -I .agent-org/bin/codex_runtime.py claim %s` 完成身份绑定，"
                      "再运行 context 并阅读返回的角色文件。只完成开局，汇报已就绪，等待研究员或获授权的派活消息。"
                      "不要自行创建其他会话或写飞书。") % (self.config["project"], name, token)
            return {"action": "create_thread", "token": token, "slot": name, "title": slot["title"],
                    "prompt": prompt, "workspace": state["workspace"], "model": slot["model"]}

    def binding_for(self, state, thread_id):
        found = [(name, b) for name, b in state["bindings"].items() if b["thread_id"] == thread_id]
        if len(found) > 1:
            raise RuntimeError("会话身份不唯一")
        return found[0] if found else (None, None)

    def bind(self, token, thread, host=None):
        if not thread:
            raise RuntimeError("没有真实 threadId；排队返回的 clientThreadId 不能当成会话 ID")
        with self.locked() as state:
            # A child's claim may race the creator's receipt; the same binding is idempotent.
            matches = [(n, b) for n, b in state["bindings"].items() if b.get("token") == token]
            if matches:
                b = matches[0][1]
                if b["thread_id"] != thread or (host and b["host_id"] != host):
                    raise RuntimeError("创建回执与已有绑定不同，请核对原生会话")
                return b
            match = [n for n, p in state["pending"].items() if p["token"] == token]
            if len(match) != 1:
                raise RuntimeError("无有效创建预约，不能自行认领角色")
            if self.binding_for(state, thread)[1]:
                raise RuntimeError("这个会话已经占用另一个池位")
            host = host or state["workspace"]["host_id"]
            if host != state["workspace"]["host_id"]:
                raise RuntimeError("回执来自其他 host")
            name = match[0]
            if thread == state["pending"][name].get("client_thread_id"):
                raise RuntimeError("clientThreadId 不能用作真实 threadId")
            b = {"role": self.slots[name]["role"], "thread_id": thread, "host_id": host, "token": token,
                 "bound_at": now(), "pinned": False, "focus": None}
            state["bindings"][name] = b
            del state["pending"][name]
            return b

    def creation_result(self, token, status, client_id=None):
        with self.locked() as state:
            for p in state["pending"].values():
                if p["token"] == token:
                    p.update(status=status, client_thread_id=client_id, observed_at=now())
                    return p
            if any(b.get("token") == token for b in state["bindings"].values()):
                return {"status": "bound"}
            raise RuntimeError("找不到创建预约")

    def cancel_reservation(self, token, reason):
        if not reason.strip():
            raise RuntimeError("取消预约需要核对未创建的证据")
        with self.locked() as state:
            for name, p in list(state["pending"].items()):
                if p["token"] == token:
                    state["history"].append(dict(p, slot=name, cancelled_at=now(), reason=reason))
                    del state["pending"][name]
                    return {"cancelled": name}
            raise RuntimeError("没有该预约；已绑定的会话不能按未创建处理")

    def replace(self, name, expected, reason):
        if not reason.strip():
            raise RuntimeError("替换要记录原因")
        with self.locked() as state:
            b = state["bindings"].get(name)
            if not b or b["thread_id"] != expected:
                raise RuntimeError("旧会话已变化，停止替换")
            state["history"].append(dict(b, slot=name, retired_at=now(), reason=reason))
            del state["bindings"][name]
        return {"action": "reserve_replacement", "slot": name,
                "note": "工作项、文档和消息记录保留；新会话归入原项目，不改全局置顶"}

    def observe(self, snapshot):
        """Read project membership from native list_threads; the recent threads list may be partial."""
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("pinnedThreads"), list):
            raise RuntimeError("需要原生 list_threads 的完整 pinnedThreads 返回，不能用自行列的置顶名单")
        pinned = set()
        for item in snapshot["pinnedThreads"]:
            if item.get("kind") == "chatgpt":
                continue
            tid = item.get("threadId") or item.get("id")
            host = item.get("hostId")
            if not tid or not host:
                raise RuntimeError("置顶记录缺少 threadId/hostId，不能按标题猜测")
            pinned.add((host, tid))
        with self.locked() as state:
            unavailable = snapshot.get("unavailableHosts", [])
            if unavailable:
                raise RuntimeError("原生会话列表有不可用主机，保留上次置顶状态，先恢复查询")
            for b in state["bindings"].values():
                b.update(pinned=(b["host_id"], b["thread_id"]) in pinned, pin_observed_at=now())
                matches = [t for t in snapshot["pinnedThreads"] + snapshot.get("threads", [])
                           if (t.get("hostId"), t.get("threadId") or t.get("id")) == (b["host_id"], b["thread_id"])]
                projects = {t.get("projectId") for t in matches if t.get("projectId")}
                if len(projects) > 1:
                    raise RuntimeError("同一会话的项目归属回读冲突，请重新查询")
                if matches:
                    b.update(observed_project_id=next(iter(projects), None), project_observed_at=now())
        return {"pinned_count": len(pinned)}

    def context(self, thread=None):
        thread = thread or os.environ.get("CODEX_THREAD_ID", "")
        state = self.load()
        name, binding = self.binding_for(state, thread)
        if not binding:
            return {"role": None, "note": "本会话没有池位绑定，不自动充当 PMO 或书记员"}
        return {"slot": name, **binding, "role_file": str(self.base / "roles" / (binding["role"] + ".md")),
                "pending_messages": [dict(m, id=mid) for mid, m in state["messages"].items()
                                     if m["to"] == name and not m.get("handled_at")]}

    def focus(self, action, topic, thread=None):
        with self.locked() as state:
            name, binding = self.binding_for(state, thread or os.environ.get("CODEX_THREAD_ID", ""))
            if not binding or not binding["role"].endswith("-p2"):
                raise RuntimeError("只有已绑定的 +2 会话能登记自己的聚焦状态")
            if action == "open" and not topic:
                raise RuntimeError("聚焦需要 topic")
            binding["focus"] = {"topic": topic, "opened_at": now()} if action == "open" else None
            return {"slot": name, "focus": binding["focus"]}

    def message(self, to, kind, text, package=None, work_item=None, thread=None, key=None):
        state = self.load()
        sender, _ = self.binding_for(state, thread or os.environ.get("CODEX_THREAD_ID", ""))
        if not sender or not state["bindings"].get(to):
            raise RuntimeError("发出方和接收方必须已绑定")
        request = dict(sender=sender, to=to, kind=kind, text=text, package=package, work_item=work_item)
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        key = key or fingerprint
        duplicate = next((m for m in state["messages"].values() if m.get("request_key") == key), None)
        if duplicate and duplicate.get("fingerprint") != fingerprint:
            raise RuntimeError("相同消息 key 对应不同内容")
        # Network reads must not hold the pool lock used by every role's hooks.
        gate = self.dispatch_gate(to, kind, package, work_item) if kind in ("assign", "review") and not duplicate else None
        with self.locked() as state:
            sender, b = self.binding_for(state, thread or os.environ.get("CODEX_THREAD_ID", ""))
            recipient = state["bindings"].get(to)
            if not b or not recipient:
                raise RuntimeError("发出方和接收方必须已绑定")
            if kind in ("assign", "review") and not (package and work_item):
                raise RuntimeError("派活/送审必须给任务包路径和已登记的工作项 ID")
            request = dict(sender=sender, to=to, kind=kind, text=text, package=package, work_item=work_item)
            fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            key = key or fingerprint
            for old_id, old in state["messages"].items():
                if old.get("request_key") == key:
                    if old.get("fingerprint") != fingerprint:
                        raise RuntimeError("相同消息 key 对应不同内容")
                    return {"action": "reuse", "message_id": old_id, **old,
                            "note": "已经登记过；先核对现有发送/接收状态，不要再次发送"}
            mid = str(uuid.uuid4())
            if gate and recipient["thread_id"] != gate["thread_id"]:
                raise RuntimeError("检查后接收会话已替换，请重新派活")
            m = {"from": sender, "to": to, "kind": kind, "text": text, "package": package,
                 "work_item": work_item, "status": "prepared", "prepared_at": now(),
                 "thread_id": recipient["thread_id"], "host_id": recipient["host_id"],
                 "request_key": key, "fingerprint": fingerprint, "dispatch_check": gate}
            state["messages"][mid] = m
            prompt = "[agent-org:%s]\n%s → %s · %s\n工作项：%s\n任务包：%s\n%s" % (
                mid, sender, to, kind, work_item or "无", package or "无", text)
            prompt += ("\n接收后先执行：python3 -I .agent-org/bin/codex_runtime.py ack %s"
                       "\n实际处理完再执行同一命令并加 --handled。收到/处理回执不替代业务验收。") % mid
            if gate:
                prompt += "\n写入业务产物或过程材料前执行：python3 -I .agent-org/bin/codex_runtime.py start %s；检查通过才开工。" % mid
            m["prompt"] = prompt
            return {"action": "send", "message_id": mid, "tool": "send_message_to_thread", "thread_id": recipient["thread_id"],
                    "host_id": recipient["host_id"], "prompt": prompt, "defer": bool(recipient.get("focus")),
                    "note": "仅在用户已授权本次会话通信时调用；失败或结果未知先核对，不盲目重发"}

    def dispatch_gate(self, to, kind, package, work_item, starting=False):
        import ledger
        if not package or not work_item or not str(work_item).isdigit():
            raise RuntimeError("派活/送审必须给任务包路径和已登记的工作项 ID")
        binding = self.load()["bindings"].get(to)
        if not binding:
            raise RuntimeError("接收方必须已绑定")
        path = (self.root / package).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise RuntimeError("任务包必须是当前项目内已有的文件")
        content = path.read_bytes()
        body = content.decode("utf-8")
        scopes = {}
        for key, label in (("product_scope", "业务产物写入范围"), ("process_scope", "过程材料写入范围")):
            match = re.search(r"^- " + label + r"[：:][ \t]*([^\r\n]*)", body, re.M)
            value = match.group(1).strip() if match else ""
            if not value or re.search(r"[<>]|待定|待补|TODO", value, re.I):
                raise RuntimeError("任务包必须明确“%s”，先由发出方澄清，不扩大授权范围" % label)
            scopes[key] = value
        expected_package = "送审包" if kind == "review" else "需求包"
        if not re.search(r"^- 包类型：" + expected_package + r"\s*$", body, re.M):
            raise RuntimeError("消息类型与任务包不匹配：需要" + expected_package)
        lg = ledger.Ledger(str(self.root), "preview")
        try:
            binding_meta = lg.ids.get("meegle") or {}
            if not all(binding_meta.get(k) for k in ("project_key", "bound_at", "types")) or ledger.other_space(
                    read(self.root / "spec/task.json", {}), binding_meta):
                raise RuntimeError("飞书目标空间没有完成有效绑定")
            receipt = ledger.find_receipt(lg, str(path))
            if not receipt or receipt.get("status") != "registered" or str(receipt.get("work_item_id")) != str(work_item):
                raise RuntimeError("任务包没有该工作项的完整登记回执，请先交书记员登记")
            item = lg.item(work_item)
            wid = item.get("type") if item else None
            allowed = {"same_seq": ("执行中",), "review": ("待核查",),
                       "cross_request": ("已接单",) if starting else ("待接单", "已接单")}
            if (kind == "review" and wid != "review") or (kind == "assign" and wid not in ("same_seq", "cross_request")):
                raise RuntimeError("工作项类型与消息不匹配")
            if item.get("state") not in allowed[wid]:
                raise RuntimeError("工作项 #%s 当前为%s；须由书记员先流转至%s并回读，不能先写产物后补开工" %
                                   (work_item, item.get("state"), "/".join(allowed[wid])))
            if lg.text_of(item, "main_doc") != receipt.get("doc") or not receipt.get("doc"):
                raise RuntimeError("工作项主文档与登记回执不一致")
            if wid == "same_seq" and lg.text_of(item, "owner_session") != to:
                raise RuntimeError("工作项负责会话与接收池位不一致，先交书记员更新")
            if wid == "review":
                source = lg.text_of(item, "source_item")
                source_item = lg.item(ledger.item_id_of(source)) if source else None
                if not source_item or source_item.get("state") != "待验收":
                    raise RuntimeError("送审前来源工作项必须回读为待验收")
        except ledger.LedgerError as exc:
            raise RuntimeError("派活检查未通过：" + str(exc)) from exc
        return dict(scopes, state=item["state"], checked_at=now(), work_item=str(work_item),
                    project_key=lg.key, thread_id=binding["thread_id"],
                    package_sha256=hashlib.sha256(content).hexdigest())

    def start(self, mid, thread=None):
        state = self.load()
        thread = thread or os.environ.get("CODEX_THREAD_ID", "")
        name, _ = self.binding_for(state, thread)
        message = state["messages"].get(mid)
        if not message or message["to"] != name or message["kind"] not in ("assign", "review"):
            raise RuntimeError("只有派活/送审的接收方能记录开工检查")
        if not message.get("received_at"):
            raise RuntimeError("先 ack 记录收到，再检查开工")
        gate = self.dispatch_gate(name, message["kind"], message["package"], message["work_item"], starting=True)
        original = message.get("dispatch_check") or {}
        if original.get("package_sha256") != gate["package_sha256"]:
            raise RuntimeError("派活后任务包已改变；由发出方核对变更并登记新消息，不沿用旧授权")
        with self.locked() as current:
            if current["bindings"].get(name, {}).get("thread_id") != thread:
                raise RuntimeError("检查期间会话绑定已变化")
            current["messages"][mid]["start_check"] = gate
        return gate

    def receipt(self, mid, status, evidence, thread=None):
        with self.locked() as state:
            m = state["messages"].get(mid)
            if not m:
                raise RuntimeError("未知消息 ID")
            name, _ = self.binding_for(state, thread or os.environ.get("CODEX_THREAD_ID", ""))
            if name != m["from"]:
                raise RuntimeError("只有发出方可记录发送结果")
            if not evidence.strip():
                raise RuntimeError("要保留原生工具的回执摘要或错误原文")
            m.setdefault("attempts", []).append({"status": status, "evidence": evidence, "at": now()})
            # Receiving can happen before the sending tool returns.
            if not m.get("received_at"):
                m["status"] = status
            return m

    def acknowledge(self, mid, handled=False, thread=None):
        with self.locked() as state:
            m = state["messages"].get(mid)
            name, _ = self.binding_for(state, thread or os.environ.get("CODEX_THREAD_ID", ""))
            if not m or m["to"] != name:
                raise RuntimeError("只有接收方能记录收到/处理回执")
            m.setdefault("received_at", now())
            if handled:
                m["handled_at"] = now()
            m["status"] = "handled" if m.get("handled_at") else "received"
            return m

    def check(self):
        state = self.load()
        issues = []
        manifest = read(self.base / "manifest.json", {})
        for rel, expected in manifest.get("files", {}).items():
            if rel.startswith((".agent-org/bin/", ".agent-org/roles/")) or rel in (
                    ".agent-org/policy.json", ".agent-org/pool-plan.json", ".codex/hooks.json"):
                p = self.root / rel
                if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != expected:
                    issues.append("生成文件变化：" + rel)
        for name, slot in self.slots.items():
            b = state["bindings"].get(name)
            if not b:
                issues.append("未绑定：" + name)
                continue
            if b.get("observed_project_id") != (state.get("workspace") or {}).get("project_id"):
                issues.append("未回读确认项目归属：" + name)
            session = state["sessions"].get(b["thread_id"], {})
            if not session.get("pretool_seen"):
                issues.append("未观察到角色会话 PreToolUse：" + name)
        if not state["workspace"]:
            issues.append("尚未绑定 Codex 项目")
        for event in ("SessionStart", "PreToolUse", "Stop"):
            if not state["hooks_seen"].get(event):
                issues.append("未观察到 hook：" + event)
        return {"ready": not issues, "issues": issues, "bound": len(state["bindings"]),
                "expected": len(self.slots), "message_receipts": "receiver_ack",
                "note": "本地身份/项目归属/hook 检查；原生消息显式 ack，业务登记与独立验收另行验证"}


def assert_writer(root, command):
    pool = Pool(root)
    state = pool.load()
    thread = os.environ.get("CODEX_THREAD_ID", "")
    _, b = pool.binding_for(state, thread)
    session = state["sessions"].get(thread, {})
    manifest = read(pool.base / "manifest.json", {})
    for rel in (".agent-org/policy.json", ".agent-org/pool-plan.json"):
        path = pool.root / rel
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get("files", {}).get(rel):
            raise RuntimeError("身份配置与初始化版本不一致，停止写入：" + rel)
    if not b or not session.get("pretool_seen") or session.get("subagent"):
        raise RuntimeError("未验证的 Codex 角色会话不能写飞书；先核对绑定和 hook")
    if command != "comment" and b["role"] != "pmo-scribe":
        raise RuntimeError("正式文档、工作项和状态只由已绑定的书记员写入")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Codex 会话池：计划、原生回执、身份与恢复")
    ap.add_argument("--target", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for command in ("plan", "context", "check"):
        sub.add_parser(command)
    p = sub.add_parser("configure"); p.add_argument("--host", required=True); p.add_argument("--project", required=True)
    p = sub.add_parser("reserve"); p.add_argument("slot")
    p = sub.add_parser("claim"); p.add_argument("token")
    p = sub.add_parser("bind"); p.add_argument("token"); p.add_argument("--thread", required=True); p.add_argument("--host", required=True)
    p = sub.add_parser("creation-result"); p.add_argument("token"); p.add_argument("status", choices=("queued", "unknown", "failed")); p.add_argument("--client-id")
    p = sub.add_parser("cancel-reservation"); p.add_argument("token"); p.add_argument("--reason", required=True)
    p = sub.add_parser("replace"); p.add_argument("slot"); p.add_argument("--expected-thread", required=True); p.add_argument("--reason", required=True)
    p = sub.add_parser("observe"); p.add_argument("--file", required=True)
    p = sub.add_parser("focus"); p.add_argument("action", choices=("open", "close")); p.add_argument("--topic")
    p = sub.add_parser("message"); p.add_argument("to"); p.add_argument("--kind", required=True, choices=("assign", "report", "register", "review", "decide", "notify")); p.add_argument("--text", required=True); p.add_argument("--package"); p.add_argument("--work-item"); p.add_argument("--key")
    p = sub.add_parser("receipt"); p.add_argument("id"); p.add_argument("status", choices=("queued", "delivered", "failed", "unknown")); p.add_argument("--evidence", required=True)
    p = sub.add_parser("ack"); p.add_argument("id"); p.add_argument("--handled", action="store_true")
    p = sub.add_parser("start"); p.add_argument("id")
    args = ap.parse_args(argv)
    try:
        pool = Pool(args.target or project_paths.project_root())
        c = args.cmd
        if c in ("plan", "context", "check"):
            result = getattr(pool, c)()
        elif c == "configure": result = pool.configure(args.host, args.project)
        elif c == "reserve": result = pool.reserve(args.slot)
        elif c == "claim": result = pool.bind(args.token, os.environ.get("CODEX_THREAD_ID", ""))
        elif c == "bind": result = pool.bind(args.token, args.thread, args.host)
        elif c == "creation-result": result = pool.creation_result(args.token, args.status, args.client_id)
        elif c == "cancel-reservation": result = pool.cancel_reservation(args.token, args.reason)
        elif c == "replace": result = pool.replace(args.slot, args.expected_thread, args.reason)
        elif c == "observe": result = pool.observe(read(args.file))
        elif c == "focus": result = pool.focus(args.action, args.topic)
        elif c == "message": result = pool.message(args.to, args.kind, args.text, args.package, args.work_item, key=args.key)
        elif c == "receipt": result = pool.receipt(args.id, args.status, args.evidence)
        elif c == "ack": result = pool.acknowledge(args.id, args.handled)
        elif c == "start": result = pool.start(args.id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if c == "check" and not result["ready"] else 0
    except (RuntimeError, OSError, ValueError) as e:
        print("出错：%s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
