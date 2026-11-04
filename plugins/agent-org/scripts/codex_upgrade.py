#!/usr/bin/env python3
"""Local-only plugin upgrade, recovery and rollback; never creates remote objects or chats."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def write(path, text, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".upgrade-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def path_in(root, rel):
    p = root / rel
    if Path(rel).is_absolute() or ".." in Path(rel).parts or p == root:
        raise ValueError("升级记录含非法路径：" + rel)
    if p.is_symlink() or any(a.is_symlink() for a in p.parents if a != root and root in a.parents):
        raise ValueError("升级路径含符号链接：" + rel)
    return p


def marker(root):
    return path_in(root, ".agent-org/state/upgrade.json")


@contextlib.contextmanager
def locked(root):
    lock = path_in(root, ".agent-org/state/upgrade.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("另一个插件维护操作正在执行，请等待它完成")
        try:
            # Share the existing flock protocol without importing the remote
            # transaction module. Maintenance performs local file operations only.
            business_lock = path_in(root, "feishu/.ledger.lock")
            business_lock.parent.mkdir(parents=True, exist_ok=True)
            with business_lock.open("a+") as business:
                try:
                    fcntl.flock(business, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("台账写入仍在执行，请等它结束后再升级")
                try:
                    yield
                finally:
                    fcntl.flock(business, fcntl.LOCK_UN)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def validate_record(root, record):
    if record.get("version") != 1 or record.get("target") != str(root):
        raise ValueError("升级记录格式或目标目录不一致")
    if not isinstance(record.get("files"), dict) or not record["files"]:
        raise ValueError("升级记录没有可恢复文件")
    for rel, item in record["files"].items():
        p = path_in(root, rel)
        old = item.get("old")
        if old is not None and (not isinstance(old, str) or digest(old) != item.get("old_sha256")):
            raise ValueError("升级备份内容校验失败：" + rel)
        current = p.read_text() if p.is_file() else None
        current_hash = digest(current) if current is not None else None
        if current_hash not in (item.get("old_sha256"), item.get("new_sha256")):
            raise ValueError("升级后文件被修改，保留现场并停止恢复：" + rel)


def restore_files(root, record, writer=write):
    validate_record(root, record)
    # Restore the manifest last, after the complete old installation is back.
    ordered = sorted(record["files"], key=lambda r: (r == ".agent-org/manifest.json", r))
    for rel in ordered:
        item = record["files"][rel]
        p = path_in(root, rel)
        if item["old"] is None:
            if p.exists():
                p.unlink()
        else:
            writer(p, item["old"], item["old_mode"])


def archive(root, record, status):
    record = dict(record, status=status)
    path = path_in(root, ".agent-org/upgrades/%s.json" % record["id"])
    write(path, dumps(record), 0o600)
    marker(root).unlink(missing_ok=True)
    return record


def recover(root):
    root = Path(root).resolve()
    if not marker(root).exists():
        return {"status": "no_pending_upgrade"}
    with locked(root):
        pending = marker(root)
        if not pending.exists():
            return {"status": "no_pending_upgrade"}
        record = json.loads(pending.read_text())
        restore_files(root, record)
        return {"status": "recovered", "backup": archive(root, record, "recovered")["id"]}


def rollback(root, backup=None):
    root = Path(root).resolve()
    directory = path_in(root, ".agent-org/upgrades")
    if not directory.is_dir():
        raise ValueError("没有已完成升级备份")
    with locked(root):
        if marker(root).exists():
            raise ValueError("有未完成升级，先执行 recover")
        paths = list(directory.glob("*.json")) if directory.is_dir() else []
        if backup:
            paths = [p for p in paths if p.stem == backup]
        records = [(p.stat().st_mtime_ns, json.loads(p.read_text())) for p in paths]
        records = [(t, r) for t, r in records if r.get("status") == "committed"]
        if not records:
            raise ValueError("没有匹配的已完成升级备份")
        record = max(records, key=lambda pair: pair[0])[1]
        # A later upgrade or manual edit must never be silently overwritten.
        for rel, item in record["files"].items():
            p = path_in(root, rel)
            if not p.is_file() or digest(p.read_text()) != item["new_sha256"]:
                raise ValueError("当前文件不匹配该升级结果，停止回退：" + rel)
        validate_record(root, record)
        write(marker(root), dumps(record), 0o600)
        restore_files(root, record)
        return {"status": "rolled_back", "backup": archive(root, record, "rolled_back")["id"]}


def install(target, plan, acts, actions_for):
    """Journal all changed generated files. Dynamic state and remote IDs are excluded."""
    root = Path(target).resolve()
    with locked(root):
        if marker(root).exists():
            raise ValueError("发现未完成升级；先运行 codex_upgrade.py recover --target <项目目录>，再重新预览")
        if actions_for(target, plan) != acts:
            raise ValueError("预览之后文件发生变化，请重新预览")
        changes = {}
        for kind, rel, old in acts:
            text, executable, _ = plan.files[rel]
            p = path_in(root, rel)
            old_mode = p.stat().st_mode & 0o777 if p.is_file() else None
            mode = (old_mode if old_mode is not None else 0o644) | (0o111 if executable else 0)
            if kind in ("create", "update") or (kind != "kept" and old_mode != mode):
                changes[rel] = {"old": old, "old_mode": old_mode,
                    "old_sha256": digest(old) if old is not None else None,
                    "new_sha256": digest(text), "new_mode": mode}
        mpath = root / ".agent-org/manifest.json"
        old_manifest = mpath.read_text() if mpath.is_file() else None
        manifest = dict(plan.manifest)
        manifest["files"] = {rel: digest(plan.files[rel][0] if rel in changes else (root / rel).read_text())
                             for rel in plan.files}
        new_manifest = dumps(manifest)
        if old_manifest != new_manifest:
            changes[".agent-org/manifest.json"] = {"old": old_manifest,
                "old_mode": mpath.stat().st_mode & 0o777 if mpath.is_file() else None,
                "old_sha256": digest(old_manifest) if old_manifest is not None else None,
                "new_sha256": digest(new_manifest), "new_mode": 0o644}
        if not changes:
            return True
        record = {"version": 1, "id": uuid.uuid4().hex, "target": str(root), "status": "pending", "files": changes}
        write(marker(root), dumps(record), 0o600)
        try:
            for rel, item in changes.items():
                if rel != ".agent-org/manifest.json":
                    write(root / rel, plan.files[rel][0], item["new_mode"])
            if ".agent-org/manifest.json" in changes:
                write(mpath, new_manifest, 0o644)
            archive(root, record, "committed")
        except Exception:
            # Retain the pending record when recovery itself fails. SIGKILL recovery
            # uses the same durable record on the next explicit recover invocation.
            restore_files(root, record)
            archive(root, record, "recovered")
            raise
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="只更新已有 Codex 项目的本地插件文件；不创建会话或远端对象")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("upgrade", "recover", "rollback"):
        p = sub.add_parser(command)
        p.add_argument("--target", required=True)
        if command == "upgrade":
            p.add_argument("--source", default=str(HERE.parent))
            p.add_argument("--apply", action="store_true")
        elif command == "rollback":
            p.add_argument("--backup")
    args = parser.parse_args(argv)
    root = Path(args.target).resolve()
    try:
        if args.command == "recover":
            result = recover(root)
        elif args.command == "rollback":
            result = rollback(root, args.backup)
        else:
            source = Path(args.source).resolve()
            if not (source / "scripts/setup.py").is_file():
                raise ValueError("--source 必须指向新版插件源目录 plugins/agent-org")
            mpath = root / ".agent-org/manifest.json"
            if not mpath.is_file() or json.loads(mpath.read_text()).get("runtime") != "codex":
                raise ValueError("目标必须是已有 Codex 项目；新项目请使用初始化流程")
            if marker(root).exists():
                raise ValueError("发现未完成升级，请先执行 recover 再预览")
            sys.path.insert(0, str(source / "scripts"))
            import setup
            import codex_setup
            plan = setup.build_plan(str(root), str(root / "spec/task.json"), None, "codex")
            acts = codex_setup.actions_for(str(root), plan)
            result = {"status": "preview", "target": str(root), "version": plan.manifest["plugin_version"],
                      "changes": [{"action": kind, "path": rel} for kind, rel, _ in acts if kind != "same"],
                      "remote_actions": 0, "chat_actions": 0}
            if args.apply:
                result["status"] = "applied" if codex_setup.apply_plan(str(root), plan, acts) else "conflict"
        print(dumps(result))
        return 2 if result.get("status") == "conflict" else 0
    except (ValueError, OSError) as e:
        print("插件维护失败：" + str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
