#!/usr/bin/env python3
"""Read the organisation rules of the agent-org plugin.

    python3 -I scripts/orgspec.py check            # validate rules.json against terms.zh.json
    python3 -I scripts/orgspec.py show packages    # print one rendered section
    python3 -I scripts/orgspec.py lint <file>...   # check package drafts and docs for stale names

orgspec/rules.json holds the rules with stable English identifiers; Chinese names
live only in orgspec/terms.zh.json. Prose in rules.json may contain tokens such as
{pkg:request} or {seq:alg}; render() replaces them with the current names.
Python 3.9 standard library only.
"""
import json
import os
import re
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORGSPEC_DIR = os.path.join(PLUGIN_ROOT, "orgspec")
TOKEN = re.compile(r"\{(pkg|rel|wi|seq|rank|node|field|role|channel|abbrev):([a-z0-9_]+)\}")
RANK_NUM = {"p0": 0, "p1": 1, "p2": 2}


class OrgSpecError(Exception):
    pass


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise OrgSpecError("读不到 %s：%s" % (path, e))


def md_table(headers, rows):
    def cell(v):
        return str(v).replace("|", "｜").replace("\n", "<br>")
    lines = ["| %s |" % " | ".join(headers), "| %s |" % " | ".join("---" for _ in headers)]
    lines += ["| %s |" % " | ".join(cell(c) for c in r) for r in rows]
    return "\n".join(lines)


class OrgSpec:
    def __init__(self, rules_path=None, terms_path=None):
        self.rules_path = rules_path or os.path.join(ORGSPEC_DIR, "rules.json")
        self.terms_path = terms_path or os.path.join(ORGSPEC_DIR, "terms.zh.json")
        self.rules = _load(self.rules_path)
        self.terms = _load(self.terms_path)

    # ---------------------------------------------------------------- names
    def term(self, group, key):
        try:
            return self.terms[group][key]
        except KeyError:
            raise OrgSpecError("术语表里没有 %s:%s" % (group, key))

    def render(self, value):
        if isinstance(value, list):
            return [self.render(v) for v in value]
        if not isinstance(value, str):
            return value
        return TOKEN.sub(lambda m: self.term(m.group(1), m.group(2)), value)

    def pkg(self, pid):
        return self.term("pkg", pid)

    # ------------------------------------------------------------ sequences
    def sequences(self):
        """[(id, Chinese name, abbreviation)] in rule order."""
        return [(s["id"], self.term("seq", s["id"]), s["abbrev"]) for s in self.rules["sequences"]]

    def node_keys(self, seqs=None):
        """The knowledge-base node skeleton as keys: the top-level nodes, then a node per sequence
        under each node that has one (every sequence unless seqs names the enabled ones)."""
        tree = self.rules["docs_tree"]
        seqs = [s for s, _, _ in self.sequences()] if seqs is None else list(seqs)
        return list(tree["nodes"]) + ["%s/%s" % (n, s) for n in tree["per_sequence"] for s in seqs]

    def sequence(self, sid):
        for s in self.rules["sequences"]:
            if s["id"] == sid:
                break
        else:
            raise OrgSpecError("未知序列：%s" % sid)
        cn = self.term("seq", sid)
        ranks = {}
        for rid, row in s["ranks"].items():
            ranks[RANK_NUM[rid]] = {"label": row["label"], "given": self.render(row["given"]),
                                   "outputs": self.render(row["outputs"]), "escalate": self.render(row["escalate"])}
        if sorted(ranks) != [0, 1, 2]:
            raise OrgSpecError("%s 序列的职级分工不是 +0、+1、+2 三行" % cn)
        return {"id": sid, "cn": cn, "abbrev": s["abbrev"], "title": s["title"],
                "heading": "序列设置 - %s：%s" % (cn, s["title"]), "focus": self.render(s["focus"]),
                "intro": self.render(s["intro"]), "ranks": ranks, "notes": self.render(s.get("notes") or "")}

    # ------------------------------------------------------- whole-org text
    def rank_rows(self):
        return {RANK_NUM[r["id"]]: r for r in self.rules["ranks"]}

    def rank_table(self):
        rows = [["+%d" % RANK_NUM[r["id"]], r["given"], r["outputs"], r["context"], r["escalate"]]
                for r in self.rules["ranks"]]
        return md_table(["级别", "给定的上下文", "需要产出", "上下文维护标准", "什么时候上报"], rows)

    def general_rules(self):
        return [(g["head"], self.render(g["body"])) for g in self.rules["general_rules"]]

    def general_rules_heads(self):
        return [h for h, _ in self.general_rules()]

    def handoff_rules(self):
        return "\n".join("- " + self.render(r) for r in self.rules["handoff_rules"])

    def signoff_items(self):
        return "\n".join("- " + i for i in self.rules["only_humans"])

    def sequence_overview(self):
        return md_table(["序列", "重心"], [[self.term("seq", s["id"]), self.render(s["focus"])]
                                          for s in self.rules["sequences"]])

    def package_table(self):
        rows = [[self.pkg(p["id"]), self.render(p["direction"]), self.render(p["receiver_action"]),
                 self.render(p["focus"]), self.render(p["not_write"])] for p in self.rules["packages"]]
        return md_table(["任务包", "方向", "接收方要做的事", "内容重点", "不写"], rows)

    def relation_matrix(self):
        rels = [r["id"] for r in self.rules["relations"]]
        heads = ["发出方定下的内容"] + ["%s（%s）" % (self.term("rel", r), self._rel_dir(r)) for r in rels]
        rows = [[m["item"]] + [m[r] for r in rels] for m in self.rules["relation_matrix"]]
        return md_table(heads, rows)

    def _rel_dir(self, rid):
        r = next(x for x in self.rules["relations"] if x["id"] == rid)
        a, b = "+%d" % RANK_NUM[r["from_rank"]], "+%d" % RANK_NUM[r["to_rank"]]
        return "%s → %s" % (a, b) if r["same_sequence"] else "%s → 其他序列 %s" % (a, b)

    def package_routes(self):
        rows = []
        for r in self.rules["package_routes"]:
            name = self.pkg(r["pkg"]) + ("（%s）" % self.term("rel", r["rel"]) if r.get("rel") else "")
            rows.append([name, r["from"], r["to"], r["via"]])
        return md_table(["包", "发出方", "接收方", "怎么送到"], rows)

    def routing_table(self):
        rows = [[r["id"], r["when"], r["test"], self.render(r["detail"])] for r in self.rules["routing"]]
        return md_table(["编号", "收到什么", "判据", "动作"], rows)

    # --------------------------------------------------------------- feishu
    def work_items(self):
        return self.rules["feishu"]["work_items"]

    def work_item(self, wid):
        for w in self.work_items():
            if w["id"] == wid:
                return w
        raise OrgSpecError("未知工作项类型：%s" % wid)

    def fields_of(self, wid):
        """The shared fields, the source field, then the type's own; a type's own entry
        with a shared id overrides that entry (feedback's main_doc carries its own note)."""
        f = self.rules["feishu"]["fields"]
        out = list(f["common"])
        if wid in f["source_item"]["on"]:
            out.append(f["source_item"])
        merged = {}
        for x in out + f.get(wid, []):
            merged[x["id"]] = dict(merged.get(x["id"], {}), **x)
        return list(merged.values())

    def field(self, wid, fid):
        for f in self.fields_of(wid):
            if f["id"] == fid:
                return f
        raise OrgSpecError("%s 上没有字段 %s" % (wid, fid))

    def field_name(self, fid):
        return self.term("field", fid)

    def work_items_table(self):
        rows = []
        for w in self.work_items():
            replies = "、".join(self.pkg(p) for p in w.get("replies", [])) or "无"
            who = {"researcher": self.term("role", "researcher"), "pmo-scribe": self.term("role", "scribe")}.get(
                w.get("created_by"), w.get("created_by"))
            rows.append([self.term("wi", w["id"]), self.render(w["main_doc"]), replies, who])
        return md_table(["工作项类型", "主文档", "挂在下面的答复", "由谁创建"], rows)

    def flows_table(self):
        rows = []
        for w in self.work_items():
            flow = "；".join("%s→%s" % (a, b) for a, b in w["transitions"])
            forms = "；".join("%s→%s：%s" % (f["from"], f["to"], "、".join(self.field_name(x) for x in f["required"]))
                              for f in w.get("forms", []))
            rows.append([self.term("wi", w["id"]), "、".join(w["states"]), flow, forms or "无"])
        return md_table(["工作项类型", "状态", "允许的流转", "流转表单必填"], rows)

    def fields_table(self):
        rows = []
        seen = set()
        for w in self.work_items():
            for f in self.fields_of(w["id"]):
                key = (w["id"], f["id"])
                if key in seen:
                    continue
                seen.add(key)
                rows.append([self.term("wi", w["id"]), self.field_name(f["id"]), f["id"], f.get("type") or "",
                             "、".join(f.get("options") or []), f.get("note", "")])
        return md_table(["工作项类型", "字段", "标识", "类型", "选项", "说明"], rows)

    def docs_tree(self):
        t = self.rules["docs_tree"]
        lines = ["- 空间：%s。" % t["space"],
                 "- 一级节点：%s。" % "、".join(self.term("node", n) for n in t["nodes"]),
                 "- 按序列再分一层的节点：%s。" % "、".join(self.term("node", n) for n in t["per_sequence"])]
        for p in t["placement"]:
            pid, _, rel = p["what"].partition(":")
            name = self.pkg(pid) if pid in self.terms["pkg"] else self.term("wi", pid)
            if rel:
                name = "%s关系的%s" % (self.term("rel", rel), name)
            lines.append("- %s放在%s。" % (name, p["parent"]))
        lines += ["- 上传和归位统一由%s做。" % self.term("role", "scribe"),
                  "- " + t["archive"] + "。", "- " + self.render(t["granularity"]) + "。"]
        return "\n".join(lines)

    # ----------------------------------------------------------- validation
    def validate(self):
        problems = []

        def walk(v, where):
            if isinstance(v, dict):
                for k, x in v.items():
                    walk(x, "%s.%s" % (where, k))
            elif isinstance(v, list):
                for i, x in enumerate(v):
                    walk(x, "%s[%d]" % (where, i))
            elif isinstance(v, str):
                for m in TOKEN.finditer(v):
                    if m.group(2) not in self.terms.get(m.group(1), {}):
                        problems.append("%s：术语表里没有 %s" % (where, m.group(0)))
        walk(self.rules, "rules")
        if [r["id"] for r in self.rules["ranks"]] != ["p0", "p1", "p2"]:
            problems.append("ranks 要依次是 p0、p1、p2")
        for s in self.rules["sequences"]:
            if s["id"] not in self.terms["seq"] or s["id"] not in self.terms["abbrev"]:
                problems.append("序列 %s 缺中文名或简写" % s["id"])
            if self.terms["abbrev"].get(s["id"]) != s["abbrev"]:
                problems.append("序列 %s 的简写和术语表不一致" % s["id"])
            if sorted(s["ranks"]) != ["p0", "p1", "p2"]:
                problems.append("序列 %s 的职级分工不全" % s["id"])
        pids = [p["id"] for p in self.rules["packages"]]
        if sorted(pids) != sorted(self.terms["pkg"]):
            problems.append("包的标识和术语表对不上：%s" % pids)
        for p in self.rules["packages"]:
            if p.get("answers") and p["answers"] not in pids:
                problems.append("%s 答复的包 %s 不存在" % (p["id"], p["answers"]))
            if not template_path(p["template"]):
                problems.append("找不到 %s 的模板 %s" % (p["id"], p["template"]))
        ids = [r["id"] for r in self.rules["routing"]]
        if len(ids) != len(set(ids)):
            problems.append("路由决策表的编号有重复")
        for w in self.work_items():
            if w["id"] not in self.terms["wi"]:
                problems.append("工作项类型 %s 缺中文名" % w["id"])
            have = {f["id"] for f in self.fields_of(w["id"])}
            for form in w.get("forms", []):
                for fid in form["required"]:
                    if fid not in have:
                        problems.append("%s 的流转 %s→%s 要求的字段 %s 不在这个类型上" % (
                            w["id"], form["from"], form["to"], fid))
                if [form["from"], form["to"]] not in w["transitions"]:
                    problems.append("%s 的表单 %s→%s 不在允许的流转里" % (w["id"], form["from"], form["to"]))
            for a, b in w["transitions"]:
                if a not in w["states"] or b not in w["states"]:
                    problems.append("%s 的流转 %s→%s 用了不存在的状态" % (w["id"], a, b))
        for group in ("common", "project", "same_seq", "cross_request", "review", "decision", "feedback"):
            for f in self.rules["feishu"]["fields"].get(group, []):
                if f["id"] not in self.terms["field"]:
                    problems.append("字段 %s 缺中文名" % f["id"])
        return problems


# ------------------------------------------------------------------ lint
HEADER = re.compile(r"^- ([^：:]+)[：:]\s*(.*)$")
HEADER_KEYS = ["包类型", "发出方", "接收方", "关联工作项", "需要接收方做的事", "回复期限", "上下文范围"]
ADDED_ON_UPLOAD = {"状态包", "交接记录"}


def template_path(name):
    """Templates live in the plugin's packages skill, or next to a deployed copy."""
    for base in (os.path.join(PLUGIN_ROOT, "skills", "packages", "assets"), os.path.join(PLUGIN_ROOT, "templates")):
        path = os.path.join(base, name)
        if os.path.isfile(path):
            return path
    return None


def template_block(text):
    """Return the first ```markdown block of a template file."""
    m = re.search(r"```markdown\n(.*?)\n```", text, re.S)
    return m.group(1) if m else ""


def headings(text, level=2):
    mark = "#" * level + " "
    return [line[len(mark):].strip() for line in text.splitlines() if line.startswith(mark)]


def dropped_sections(template_text, relation):
    """Sections marked 不写 for this relation in the template's 栏目随关系变化 table."""
    m = re.search(r"## 栏目随关系变化\n(.*?)(?:\n## |\Z)", template_text, re.S)
    if not m or not relation:
        return set()
    rows = [r for r in m.group(1).splitlines() if r.startswith("|")]
    if len(rows) < 3:
        return set()
    head = [c.strip() for c in rows[0].strip("|").split("|")]
    if relation not in head:
        return set()
    col = head.index(relation)
    out = set()
    for r in rows[2:]:
        cells = [c.strip() for c in r.strip("|").split("|")]
        if len(cells) > col and cells[col].startswith("不写"):
            out.add(cells[0])
    return out


def lint_text(spec, text):
    """Return (errors, warnings) for one package draft or document."""
    errors, warnings = [], []
    for old in spec.terms.get("retired", {}):
        if re.search(re.escape(old) + "(?![含括装办])", text):
            errors.append("用了旧包名“%s”，现在叫“%s”" % (old, spec.terms["retired"][old]))
    header = {}
    for line in text.splitlines():
        m = HEADER.match(line)
        if m and m.group(1) not in header:
            header[m.group(1)] = m.group(2).strip()
        if line.startswith("## "):
            break
    if "包类型" not in header:
        return errors, warnings
    names = {v: k for k, v in spec.terms["pkg"].items()}
    pid = names.get(header["包类型"].split("（")[0].strip())
    if not pid:
        errors.append("包类型“%s”不是现行的五类包之一" % header["包类型"])
        return errors, warnings
    keys = list(HEADER_KEYS)
    if pid in ("request", "delivery"):
        keys.insert(1, "关系")
    if pid == "decision":
        keys.insert(1, "渠道")
    for k in keys:
        if k not in header:
            errors.append("包头缺“%s”" % k)
    relation = header.get("关系", "").strip()
    rel_names = set(spec.terms["rel"].values())
    if "关系" in keys and relation and relation not in rel_names:
        errors.append("关系“%s”要写%s之一" % (relation, "、".join(sorted(rel_names))))
    if pid == "decision" and header.get("渠道") not in (None, *spec.terms["channel"].values()):
        errors.append("渠道要写%s之一" % "、".join(spec.terms["channel"].values()))
    tpl = template_path(next(p["template"] for p in spec.rules["packages"] if p["id"] == pid))
    if not tpl:
        warnings.append("找不到%s的模板，跳过栏目检查" % spec.pkg(pid))
        return errors, warnings
    with open(tpl, encoding="utf-8") as f:
        tpl_text = f.read()
    want = [h for h in headings(template_block(tpl_text)) if h not in dropped_sections(tpl_text, relation)]
    have = headings(text)
    for h in want:
        if h not in have:
            errors.append("缺栏目“%s”；没有内容写“无”" % h)
    for h in have:
        if h not in want and h not in ADDED_ON_UPLOAD:
            if h in headings(template_block(tpl_text)):
                errors.append("栏目“%s”在%s关系下不写，整节删掉" % (h, relation))
            else:
                errors.append("模板里没有栏目“%s”，不临时加栏目" % h)
    if pid == "request" and relation in (spec.term("rel", "assign"), spec.term("rel", "cross")):
        m = re.search(r"\n## 方法\n(.*?)(?:\n## |\Z)", text, re.S)
        if m and m.group(1).strip() not in ("由接收方决定。", "由接收方决定"):
            errors.append("%s关系的方法只写“由接收方决定”" % relation)
    return errors, warnings


def lint_files(spec, paths):
    bad = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            print("%s：读不到（%s）" % (path, e))
            bad += 1
            continue
        errors, warnings = lint_text(spec, text)
        for w in warnings:
            print("%s：提醒：%s" % (path, w))
        for e in errors:
            print("%s：%s" % (path, e))
        bad += bool(errors)
    print("检查了 %d 个文件，%s" % (len(paths), "%d 个有问题" % bad if bad else "没有问题"))
    return 1 if bad else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    spec = OrgSpec()
    if not argv or argv[0] == "check":
        problems = spec.validate()
        for p in problems:
            print("问题：" + p)
        print("规则文件检查：%s" % ("%d 个问题" % len(problems) if problems else "没有问题"))
        return 1 if problems else 0
    if argv[0] == "lint" and len(argv) > 1:
        return lint_files(spec, argv[1:])
    if argv[0] == "show" and len(argv) > 1:
        fn = {"ranks": spec.rank_table, "packages": spec.package_table, "relations": spec.relation_matrix,
              "routes": spec.package_routes, "routing": spec.routing_table, "work-items": spec.work_items_table,
              "flows": spec.flows_table, "fields": spec.fields_table, "docs-tree": spec.docs_tree,
              "handoff": spec.handoff_rules, "overview": spec.sequence_overview}.get(argv[1])
        if not fn:
            print("可以显示：ranks packages relations routes routing work-items flows fields docs-tree handoff overview")
            return 2
        print(fn())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
