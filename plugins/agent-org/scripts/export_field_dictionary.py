#!/usr/bin/env python3
"""Export the Feishu Project field dictionary for the agent organisation.

Reads work item types, fields and roles with meegle (read-only commands) and
writes field-dictionary.json plus a Markdown table: every type in use in the space,
or the ones named with --types. Types switched off in the space (is_disable 1) are
skipped: meta-fields answers "type not found" for them. The JSON holds project_key
plus types keyed by type_key; setup.py binds the rules to it by name.

Exit codes: 0 done; 3 done but some types are missing from the space; 2 failed.

Usage:
    python3 -I export_field_dictionary.py --project-key KEY --out-dir feishu
    python3 -I export_field_dictionary.py --project-key KEY --types 项目,决策 --print
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def meegle(*args):
    cmd = ["meegle", *args, "--format", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} 退出码 {proc.returncode}：{proc.stdout[-300:]}{proc.stderr[-300:]}")
    return json.loads(proc.stdout)


def paged(command, key, type_key):
    items, page = [], 1
    while True:
        data = meegle("workitem", command, "--project-key", key, "--work-item-type", type_key,
                      "--page-num", str(page))
        batch = data.get("list") or []
        items.extend(batch)
        pagination = data.get("pagination") or {}
        if not batch or not pagination.get("has_more", len(batch) == 50):
            return items
        page += 1


def field_record(f):
    rec = {"name": f.get("field_name"), "field_key": f.get("field_key"), "field_type": f.get("field_type")}
    if f.get("option"):
        rec["options"] = [{"id": o.get("option_id"), "name": o.get("option_name")} for o in f["option"]]
    if f.get("related_work_item_info"):
        rec["related"] = f["related_work_item_info"]
    return rec


def export_types(types, key, workers=4):
    """Independent read-only type requests; preserve source ordering and propagate errors."""
    def export(t):
        fields = paged("meta-fields", key, t["type_key"])
        roles = paged("meta-roles", key, t["type_key"])
        return t["type_key"], {"name": t.get("name"), "api_name": t.get("api_name"),
            "fields": [field_record(f) for f in fields],
            "roles": [{"role_id": r.get("role_id"), "role_name": r.get("role_name")} for r in roles]}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(export, types))


def to_markdown(doc):
    lines = [f"# 字段字典：{doc.get('space_name') or doc['project_key']}", "",
             f"> 导出时间：{doc['exported_at']}。来源：`meegle workitem meta-types`、`meta-fields`、`meta-roles`。"
             "写入工作项时以 field_key 为准，不凭中文名猜；空间配置一改就重新导出。"
             "字段的对接标识（如 `sequence`）只在网页上可见，meta-fields 不返回，所以这里没有这一列。", ""]
    if doc["missing_types"]:
        lines += [f"> 空间里还没有这些类型：{'、'.join(doc['missing_types'])}。", ""]
    if doc["disabled_types"]:
        lines += [f"> 空间里停用的类型没有导出：{'、'.join(doc['disabled_types'])}。", ""]
    for type_key, t in doc["types"].items():
        lines += [f"## {t['name']}（`{type_key}`）", ""]
        if t.get("roles"):
            lines += ["角色：" + "、".join(f"{r['role_name']}（`{r['role_id']}`）" for r in t["roles"]), ""]
        lines += ["| 中文名 | field_key | 类型 | 可选值 |", "| --- | --- | --- | --- |"]
        for f in t["fields"]:
            opts = "、".join(o["name"] for o in f.get("options", []))
            if f.get("related"):
                opts = "关联：" + "、".join(r.get("work_item_type", "") for r in f["related"])
            lines.append(f"| {f['name']} | `{f['field_key']}` | {f['field_type']} | {opts} |")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="导出飞书项目的字段字典")
    ap.add_argument("--project-key", required=True)
    ap.add_argument("--types", help="只导出这些类型（中文名、api_name 或 type_key，逗号分隔）；默认导出全部类型")
    ap.add_argument("--out-dir", help="输出目录")
    ap.add_argument("--print", action="store_true", help="只把 JSON 打到标准输出，不写文件")
    ap.add_argument("--space-name")
    args = ap.parse_args()
    if not args.print and not args.out_dir:
        sys.stderr.write("要么给 --out-dir，要么加 --print\n")
        return 2
    ids = {"meegle": {"space_name": args.space_name}}
    key = args.project_key
    wanted = [w.strip() for w in args.types.split(",") if w.strip()] if args.types else None

    types = meegle("workitem", "meta-types", "--project-key", key).get("list") or []

    def names(t):
        return {t.get("api_name"), t.get("type_key"), t.get("name")}
    doc = {
        "project_key": key,
        "simple_name": ids.get("meegle", {}).get("simple_name"),
        "space_name": ids.get("meegle", {}).get("space_name"),
        "exported_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "types": {},
        "missing_types": [n for n in wanted or [] if not any(n in names(t) for t in types)],
        "disabled_types": [],
    }
    enabled = []
    for t in types:
        if wanted is not None and not set(wanted) & names(t):
            continue
        if t.get("is_disable") == 1:
            doc["disabled_types"].append(t.get("name") or t["type_key"])
            continue
        enabled.append(t)
    doc["types"] = export_types(enabled, key)

    if args.print:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 3 if doc["missing_types"] else 0
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "field-dictionary.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    with open(os.path.join(args.out_dir, "field-dictionary.md"), "w", encoding="utf-8") as fh:
        fh.write(to_markdown(doc) + "\n")
    print(json.dumps({"types": {k: len(v["fields"]) for k, v in doc["types"].items()},
                      "missing_types": doc["missing_types"]}, ensure_ascii=False))
    return 3 if doc["missing_types"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # any failure is exit 2, never a bare traceback with exit 1
        sys.stderr.write("导出失败：%s: %s\n" % (type(e).__name__, e))
        sys.exit(2)
