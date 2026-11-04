#!/usr/bin/env python3
"""Read and write the task ledger (Feishu Project) and package documents (Feishu Docs).

Read commands, for every session:
    ledger.py get <ID> [--raw]              type, state, name and the rule fields (--raw: meegle's JSON)
    ledger.py query <name> [--sequence s]   queue | blocked | verifying | reviewing | open | cross | recent | source:<ID>
    ledger.py mql <name> [--sequence s]     print the MQL only
    ledger.py trace <ID>                    follow 来源工作项 upwards
    ledger.py status <ID>                   print the 状态包 section of the main document

Write commands, for the scribe only (the guard hook enforces this). They print the
commands first; nothing is written without --apply:
    ledger.py register <draft> [--resume] [--existing]   request / review / decision / 立项单: work item + main document
    ledger.py reply <draft>                 delivery / verdict: document under the parent, link field
    ledger.py transition <ID> <state> [--set field=value ...]
    ledger.py update <ID> --set field=value [...]
    ledger.py comment <ID> --text T
    ledger.py status-block <ID> --file F    replace the 状态包 section (block_replace)
    ledger.py append <ID> --file F          append to 交接记录 (first line carries the session tag)
    ledger.py archive <ID>                  move the main document subtree to 归档文档/<序列>
    ledger.py ensure-node <key>             create a knowledge-base node such as feedback or review/alg
    ledger.py ensure-node all               create the whole node skeleton, parents first

Fields are named by rule identifiers (sequence, sender, source_item, ...); values
are converted with feishu/field-dictionary.json. Dates are written as UTC noon.
Document bodies go to lark-cli on stdin (`--content -`): lark-cli only reads @files
under the working directory, /tmp or ~/files. The shapes parsed here are the ones
recorded from a real space (tests/fixtures/feishu/). Every type and field is found
through feishu/ids.json, which `setup.py feishu --live --apply` binds to the project's
own space; the rules hold names and identifiers only, never a space's keys.
--offline prints every command without calling Feishu (used by tests).
Every --apply holds an exclusive lock on feishu/.ledger.lock (waiting up to 60 seconds),
so two writers never interleave; ids.json and receipts are replaced atomically.
Python 3.9 standard library only.
"""
import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import project_paths  # noqa: E402

# name: (work item type, condition, states to leave out besides the system ones or None, description)
QUERIES = {
    "queue": ("decision", "`work_item_status` = '待决定'", None, "决策队列：待决定的决策"),
    "blocked": ("same_seq", "`work_item_status` = '阻塞'", None, "阻塞的同序列需求"),
    "verifying": ("same_seq", "`work_item_status` = '待验收'", None, "待验收的同序列需求"),
    "reviewing": ("review", "`work_item_status` = '待核查'", None, "待核查的送审"),
    "open": ("same_seq", None, ["已验收", "归档"], "进行中的同序列需求"),
    "cross": ("cross_request", None, ["已拒绝", "已关闭"], "进行中的跨序列需求"),
    "recent": ("same_seq", "RELATIVE_DATETIME_BETWEEN(`updated_at`, 'past', '7d')", [], "近 7 天更新过的同序列需求"),
}
READ_COMMANDS = ("get", "query", "mql", "trace", "status")
TRUE_WORDS = {"是", "true", "True", "1", "yes", "要"}
FALSE_WORDS = {"否", "false", "False", "0", "no", "不要"}


class LedgerError(Exception):
    pass


class WriteMaybeDone(LedgerError):
    """A write whose outcome is unknown (a timeout, an answer without the expected ID): it may be in Feishu."""


def project_root():
    return project_paths.project_root()


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    """Write JSON through a temporary file and os.replace, so a reader never sees half a file."""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


LOCK_WAIT = 60


@contextlib.contextmanager
def write_lock(root, wait=None):
    """Hold feishu/.ledger.lock for one --apply run. The file names the holder (session and pid)."""
    wait = LOCK_WAIT if wait is None else wait
    path = os.path.join(root, "feishu", ".ledger.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    f.seek(0)
                    holder = f.read().strip() or "未知"
                    raise LedgerError("台账正在被别的写入占用（%s），等了 %d 秒还没放开；等它写完再试，不要删锁文件" % (holder, wait))
                time.sleep(0.5)
        try:
            f.seek(0)
            f.truncate()
            f.write("会话 %s，pid %d，%s 开始" % (os.environ.get("CLAUDE_CODE_SESSION_ID") or "终端", os.getpid(), now()))
            f.flush()
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def other_space(task, meegle):
    """The space spec/task.json names when it is not the bound one; None when they agree or
    the binding does not record a space name to compare with."""
    fs = task.get("feishu") if isinstance(task, dict) else None
    wanted = str(fs.get("meegle_space") or "").strip() if isinstance(fs, dict) else ""
    known = [meegle.get(k) for k in ("project_key", "simple_name", "space_name") if meegle.get(k)]
    return wanted if wanted and meegle.get("space_name") and wanted not in known else None


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def utc_noon_ms(text):
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text or "")
    if not m:
        return None
    try:
        d = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return str(int(d.timestamp() * 1000))


def item_id_of(value):
    """A work item id given as 2003 or #2003; LedgerError for anything else."""
    text = str(value or "").strip().lstrip("#")
    if not text.isdigit():
        raise LedgerError("工作项 ID 要写成数字，比如 2003 或 #2003：%r" % value)
    return text


def system_states(spec):
    return list(spec.rules["feishu"].get("system_states", []))


def closed_states(spec):
    """States in which new information no longer resumes a work item.

    End states of every type, the system state 已终止, and the states listed in
    feishu.closed_for_routing (已验收, 已过期): done, though not yet an end state.
    """
    out = set(system_states(spec)) | set(spec.rules["feishu"].get("closed_for_routing", []))
    for w in spec.work_items():
        out.update(w.get("end_states", []))
    return out


def plain(value):
    """A field value read back by `workitem get`, as plain text (None when empty).

    select {label, value} -> label; date {iso_time, timestamp} -> iso_time;
    related work item {id, name} -> id; roles [{label, value}] -> labels; text, link,
    number and bool come back as strings already.
    """
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, list):
        parts = [plain(v) for v in value]
        return "、".join(p for p in parts if p) or None
    if isinstance(value, dict):
        if "label" in value:
            return value.get("label")
        if "iso_time" in value:
            return value.get("iso_time")
        if "id" in value:
            return str(value["id"])
        if "name" in value:
            return value.get("name")
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def moql_value(v):
    """One cell of `workitem query` output: data.<group>[].moql_field_list[].value.

    A person comes back as user_value {name_cn, name_en, user_key, email}; only the
    name is kept, so a query never prints e-mail addresses.
    """
    if not isinstance(v, dict):
        return v
    for k in ("long_value", "string_value", "double_value", "bool_value"):
        if k in v:
            return v[k]
    if "user_value" in v:
        users = v["user_value"] if isinstance(v["user_value"], list) else [v["user_value"]]
        names = [u.get("name_cn") or u.get("name_en") or u.get("user_key") for u in users if isinstance(u, dict)]
        return "、".join(n for n in names if n) or None
    labels = v.get("key_label_value_list")
    if labels:
        return "、".join(str(x.get("label") or x.get("key")) for x in labels)
    return v.get("value", v)


# ------------------------------------------------------------- context
class Ledger:
    def __init__(self, root, mode):
        self.root, self.mode = root, mode          # mode: apply | preview | offline
        self.spec = orgspec.OrgSpec()
        self.ids = load_json(os.path.join(root, "feishu", "ids.json"), {}) or {}
        self.fd = load_json(os.path.join(root, "feishu", "field-dictionary.json"), {}) or {}
        m = self.ids.get("meegle") or {}
        self.key = m.get("project_key") or "<project_key>"
        self.simple_name = m.get("simple_name")
        self.types = m.get("types") or {}
        self.field_keys = m.get("field_keys") or {}
        wiki = self.ids.get("wiki") or {}
        self.nodes = wiki.get("nodes") or {}
        self.tenant = wiki.get("tenant_domain")
        self.tag = os.environ.get("AGENT_ORG_TAG", "[PMO+0]")
        if os.path.isfile(os.path.join(root, ".agent-org", "manifest.json")):
            import codex_runtime
            context = codex_runtime.Pool(root).context()
            policy = load_json(os.path.join(root, ".agent-org", "policy.json"), {})
            self.tag = (policy.get("agents", {}).get(context.get("role")) or {}).get("tag", "[unknown]")
        self.planned = []
        self._me = None

    # names and keys
    def bound_type(self, wid):
        t = self.types.get(wid) or {}
        if not t.get("type_key"):
            raise LedgerError("%s还没绑定到空间里的类型：先跑 setup.py feishu --live，核对后加 --apply" % self.spec.term("wi", wid))
        return t

    def api(self, wid):
        """The type's system identifier in this space (work item links use it)."""
        t = self.bound_type(wid)
        return t.get("api_name") or t["type_key"]

    def type_ref(self, wid):
        """What MQL accepts as a table name: the type_key bound for this space."""
        return self.bound_type(wid)["type_key"]

    def wid_of(self, key):
        """Rule id of a work item type, from the type_key, api_name or name bound for this
        space, or from the rule id itself (what --type takes)."""
        if not key:
            return None
        for wid, t in self.types.items():
            if key in (t.get("type_key"), t.get("api_name"), t.get("name")):
                return wid
        return next((w["id"] for w in self.spec.work_items() if w["id"] == key), None)

    def field(self, wid, fid):
        """Rule field with the field_key bound for this space."""
        f = self.spec.field(wid, fid)
        key = (self.field_keys.get(wid) or {}).get(fid)
        if not key:
            raise LedgerError("%s的字段 %s（%s）还没绑定：空间里没有的照 feishu/migration.md 建好，名字不同的用 --map 指认，再跑 setup.py feishu --live --apply" % (
                self.spec.term("wi", wid), fid, self.spec.field_name(fid)))
        return dict(f, field_key=key)

    def key_of(self, wid, fid):
        try:
            return self.field(wid, fid)["field_key"]
        except (LedgerError, orgspec.OrgSpecError, KeyError):
            return None

    def fid_by_key(self, wid):
        """{field_key: rule id} for every rule field of a type that has a key."""
        out = {}
        for f in self.spec.fields_of(wid):
            key = self.key_of(wid, f["id"])
            if key:
                out[key] = f["id"]
        return out

    def fd_type(self, wid):
        tkey = (self.types.get(wid) or {}).get("type_key")
        return (self.fd.get("types") or {}).get(tkey) if tkey else None

    def fd_field(self, wid, field_key):
        t = self.fd_type(wid) or {}
        return next((x for x in t.get("fields", []) if x.get("field_key") == field_key), None)

    def me(self):
        """user_key of the account every agent runs as (the researcher's)."""
        if self._me is None:
            if self.mode == "offline":
                self._me = "<研究员的 user_key>"
            else:
                data = self.run(["meegle", "user", "me", "--format", "json"], False)
                key = _find(data, ("user_key", "userkey", "userKey")) if data else None
                if not key:
                    raise LedgerError("meegle user me 的输出里找不到 user_key：%s" % json.dumps(data, ensure_ascii=False)[:300])
                self._me = str(key)
        return self._me

    def value(self, wid, fid, raw):
        """Convert a human value to the STRING protocol meegle expects."""
        f = self.field(wid, fid)
        kind = f.get("type")
        raw = str(raw).strip()
        if kind == "date":
            ms = utc_noon_ms(raw)
            if not ms:
                raise LedgerError("%s 要写成存在的日期 YYYY-MM-DD：%s" % (self.spec.field_name(fid), raw))
            return ms
        if kind == "bool":
            if raw in TRUE_WORDS:
                return "true"
            if raw in FALSE_WORDS:
                return "false"
            raise LedgerError("%s 要写“是”或“否”：%s" % (self.spec.field_name(fid), raw))
        if kind == "user":
            # every agent runs as the researcher's account, and only the researcher decides
            if raw in ("研究员", "我", "me") or (self.mode != "offline" and raw == self.me()):
                return self.me()
            if self.mode == "offline":
                return raw
            raise LedgerError("%s 只能填“研究员”（或研究员的 user_key）：%s" % (self.spec.field_name(fid), raw))
        if kind in ("select", "multi_select"):
            meta = self.fd_field(wid, f["field_key"]) or {}
            options = meta.get("options") or []
            hit = next((o.get("id") for o in options if o.get("name") == raw), None)
            if hit is None:
                if self.mode == "offline":
                    return "<%s 的选项 ID>" % raw
                raise LedgerError("%s 没有选项“%s”；字段字典里的选项：%s" % (
                    self.spec.field_name(fid), raw, "、".join(o.get("name", "") for o in options) or "无（先导出字段字典）"))
            return json.dumps([{"option_id": hit}]) if kind == "multi_select" else hit
        return raw

    def entry(self, wid, fid, raw):
        return {"field_key": self.field(wid, fid)["field_key"], "field_value": self.value(wid, fid, raw)}

    def researcher_role(self, wid):
        who = self.spec.term("role", "researcher")
        t = self.fd_type(wid) or {}
        role = next((r.get("role_id") for r in t.get("roles", []) if r.get("role_name") == who), None)
        if not role and self.mode == "offline":
            role = "<研究员角色 ID>"
        return role

    def role_owners(self, wid):
        """role_owners entry naming the researcher, for workitem create; None if the role is unknown."""
        role = self.researcher_role(wid)
        if not role:
            print("提醒：字段字典里%s没有“%s”角色，工作项的角色人员会空着" % (
                self.spec.term("wi", wid), self.spec.term("role", "researcher")))
            return None
        return {"field_key": "role_owners", "field_value": json.dumps([{"role": role, "owners": [self.me()]}])}

    # running commands
    def run(self, argv, write, stdin=None):
        shown = " ".join(_quote(a) for a in argv)
        if stdin is not None:
            shown += "  # 正文从标准输入传入，%d 字" % len(stdin)
        if write and self.mode != "apply" or self.mode == "offline":
            self.planned.append(shown)
            print(("将执行：" if write else "将读取：") + shown)
            return None
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=120, input=stdin)
        except subprocess.TimeoutExpired:
            if write:
                self.log(argv, "timeout", "")
                raise WriteMaybeDone("写命令超时（120 秒）：%s\n这一步可能已经写进飞书。先在网页上核对，再决定要不要重跑。" % shown)
            raise LedgerError("命令超时（120 秒）：%s" % shown)
        except OSError as e:
            raise LedgerError("运行不了 %s（%s）；先装好并登录，见 setup.py precheck" % (argv[0], e))
        out = p.stdout.strip()
        if write:
            self.log(argv, p.returncode, out[-2000:])
        if p.returncode != 0:
            raise LedgerError("命令失败（退出码 %s）：%s\n%s" % (p.returncode, shown, (p.stderr or out)[-800:]))
        try:
            return json.loads(out) if out else {}
        except ValueError:
            return {"_text": out}

    def meegle(self, *args, write=False):
        return self.run(["meegle", *args, "--project-key", self.key, "--format", "json"], write)

    def lark(self, *args, write=False, stdin=None):
        return self.run(["lark-cli", *args, "--as", "user"], write, stdin)

    def log(self, argv, code, out):
        path = os.path.join(self.root, "feishu", "write-log.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ids = sorted(set(re.findall(r'"(?:work_item_id|node_token|obj_token|document_id|url)"\s*:\s*"?([^",}\s]+)', out)))
        rec = {"ts": now(), "tool": "ledger.py", "session": self.tag,
               "argv": [a if len(a) < 300 else a[:300] + "…" for a in argv], "exit": code, "ids": ids[:10]}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # reads
    def get(self, item_id, field_keys=None):
        args = ["workitem", "get", "--work-item-id", str(item_id)]
        if field_keys:
            args += ["--fields", json.dumps(field_keys)]
        return self.meegle(*args)

    def item(self, item_id):
        """{type, type_key, state, name, fields{field_key: raw value}}; None offline.

        Shape of `workitem get --fields '["_all"]'`: work_item_attribute holds the
        type {key: type_key, name}, the state {key, name} and work_item_name;
        work_item_fields lists {key, name, value}. Fields without a value are left
        out. One page holds 100 fields; the types here have under 50.
        """
        data = self.get(item_id, ["_all"])
        if data is None:
            return None
        attr = data.get("work_item_attribute")
        if not isinstance(attr, dict):
            raise LedgerError("读不出工作项 %s：输出里没有 work_item_attribute：%s" % (
                item_id, json.dumps(data, ensure_ascii=False)[:300]))
        tkey = (attr.get("work_item_type") or {}).get("key")
        fields = {}
        for f in data.get("work_item_fields") or []:
            if isinstance(f, dict) and f.get("key"):
                fields[f["key"]] = f.get("value")
        return {"type": self.wid_of(tkey), "type_key": tkey,
                "state": (attr.get("work_item_status") or {}).get("name"),
                "name": attr.get("work_item_name"), "fields": fields}

    def people(self, it, item_id, keys=None):
        """Fill in the person fields `workitem get` leaves empty (决定人 reads back as null).

        MQL has them as user_value; one query reads the empty ones and puts the names
        into it["fields"]. keys: the field_keys to read, by default every person field
        of the item's type. Only a non-empty name counts as filled in: an empty cell
        ({}, [], null) stays empty, and so does a shape moql_value cannot read, with a
        reminder that leaves the value out (it may hold e-mail addresses). When the
        query fails they stay empty, with a reminder.
        """
        wid = it.get("type") if it else None
        if not wid:
            return
        if keys is None:
            keys = [self.key_of(wid, f["id"]) for f in self.spec.fields_of(wid) if f.get("type") == "user"]
        keys = [k for k in dict.fromkeys(keys) if k and plain(it["fields"].get(k)) is None]
        num = str(item_id).strip().lstrip("#")
        if not keys or not num.isdigit():
            return
        try:
            mql = "SELECT `work_item_id`, %s FROM `%s`.`%s` WHERE `work_item_id` = %s" % (
                ", ".join("`%s`" % k for k in keys), self.key, self.type_ref(wid), num)
            data = self.run(["meegle", "workitem", "query", "--project-key", self.key, "--mql", mql, "--format", "json"], False)
            rows = query_rows(data) if data is not None else []
        except LedgerError as e:
            print("提醒：人员字段读不出来，按空的处理：%s" % e)
            return
        row = rows[0] if rows else {}
        for k in keys:
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                it["fields"][k] = v
            elif v not in (None, "", {}, []):
                print("提醒：人员字段 %s 的 MQL 输出读不懂，按空的处理" % k)

    def text_of(self, it, fid):
        """Plain text of a rule field on a work item read with item(); None if empty or absent.

        When the item's type cannot be read, shared fields (main_doc, sequence,
        sender ...) are still found through any type that has them.
        """
        if not it:
            return None
        wids = [it["type"]] if it.get("type") else [w["id"] for w in self.spec.work_items()]
        for wid in wids:
            key = self.key_of(wid, fid)
            if key and key in it["fields"]:
                return plain(it["fields"].get(key))
        return None

    def doc_url(self, item_id, it=None):
        it = it if it is not None else self.item(item_id)
        if it is None:
            return "<#%s 的主文档链接>" % item_id
        url = self.text_of(it, "main_doc")
        if not url:
            raise LedgerError("工作项 %s 的“主文档”是空的" % item_id)
        return url

    def node_of(self, url):
        """Wiki node token of a document link: /wiki/<node> directly, /docx/<id> via wiki +node-get."""
        if self.mode == "offline" and (url or "").startswith("<"):
            return "<%s对应的节点>" % url.strip("<>")
        m = re.search(r"/wiki/([A-Za-z0-9]+)", url or "")
        if m:
            return m.group(1)
        m = re.search(r"/docx/([A-Za-z0-9]+)", url or "")
        if not m:
            return None
        data = self.lark("wiki", "+node-get", "--node-token", m.group(1))
        if data is None:
            return "<%s 对应的节点>" % m.group(1)
        return _find(data, ("node_token",))

    def remember_tenant(self, link):
        """Keep the tenant domain of the first document link in ids.json (wiki.tenant_domain):
        a later --resume that only has a document_id needs it to build the /wiki/ link."""
        m = re.match(r"https?://([^/\s]+)/", link or "")
        if not m or self.tenant:
            return
        self.tenant = m.group(1)
        if self.mode == "apply":
            path = os.path.join(self.root, "feishu", "ids.json")
            ids = load_json(path, {})
            if isinstance(ids, dict) and not (ids.get("wiki") or {}).get("tenant_domain"):
                ids.setdefault("wiki", {})["tenant_domain"] = self.tenant
                save_json(path, ids)

    def wiki_url(self, node, near=None):
        m = re.match(r"https?://([^/]+)/", near or "")
        host = m.group(1) if m else self.tenant
        if not host:
            raise LedgerError("不知道租户域名：ids.json 的 wiki.tenant_domain 是空的")
        return "https://%s/wiki/%s" % (host, node)

    def item_url(self, wid, item_id):
        """Canonical work item link /{simple_name}/{api_name}/detail/{id}; None without simple_name."""
        if not self.simple_name:
            return None
        host = (self.ids.get("meegle") or {}).get("host") or "project.feishu.cn"
        return "https://%s/%s/%s/detail/%s" % (host, self.simple_name, self.api(wid), item_id)

    # writes on documents
    def create_doc(self, parent, title, body, created=None):
        """Create a document under a wiki node; return its /wiki/ link.

        docs +create answers with a /docx/ link and document_id only, so the node
        token is looked up with wiki +node-get before the link is stored anywhere.
        created(docx_link_or_id) is called as soon as the document exists, so a
        failed lookup can be resumed without creating a second document.
        """
        out = self.lark("docs", "+create", "--parent-token", parent, "--title", title, "--doc-format", "markdown",
                        "--content", "-", write=True, stdin=body)
        if out is None:
            return "<新文档的知识库链接>"
        docx = _find(out, ("url",))
        doc_id = _find(out, ("document_id", "obj_token"))
        self.remember_tenant(docx)
        if not (docx or doc_id):
            raise WriteMaybeDone("docs +create 的返回里没有文档链接和 document_id：%s\n文档可能已经建好了" % (
                json.dumps(out, ensure_ascii=False)[:300]))
        if created:
            created(docx or doc_id)
        return self.doc_link(docx, doc_id)

    def doc_link(self, docx=None, doc_id=None):
        """/wiki/ link of a document known by its /docx/ link or document_id."""
        node = self.node_of(docx) if docx else None
        if not node and doc_id:
            got = self.lark("wiki", "+node-get", "--node-token", doc_id)
            node = _find(got, ("node_token",)) if got else None
        if not node:
            raise LedgerError("文档建好了（%s），但还没查到它的知识库节点" % (docx or doc_id))
        return self.wiki_url(node, docx)


def _quote(a):
    return a if re.fullmatch(r"[\w@%+=:,./#-]+", a) else "'" + a.replace("'", "'\\''") + "'"


def _walk(v):
    if isinstance(v, dict):
        yield v
        for x in v.values():
            yield from _walk(x)
    elif isinstance(v, list):
        for x in v:
            yield from _walk(x)


def _find(data, keys):
    for d in _walk(data):
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
    return None


def doc_content(data):
    """The XML string of a docs +fetch answer: data.document.content."""
    if not isinstance(data, dict):
        return ""
    doc = (data.get("data") or {}).get("document") or data.get("document") or {}
    return doc.get("content") or data.get("_text") or ""


def created_id(out, what):
    """The work item id in a `workitem create` answer; stop if there is none.

    Only work_item_id (anywhere), or an id at the top level or directly under data:
    a nested id may belong to the template or the project.
    """
    item_id = _find(out, ("work_item_id",))
    if item_id in (None, "") and isinstance(out, dict):
        data = out.get("data") if isinstance(out.get("data"), dict) else {}
        item_id = out.get("id") or data.get("id")
    if item_id in (None, ""):
        raise LedgerError("%s建了，但输出里没有工作项 ID：%s；去网页上核对，不要重跑" % (
            what, json.dumps(out, ensure_ascii=False)[:300]))
    return str(item_id)


# --------------------------------------------------------------- drafts
HEADER = re.compile(r"^- ([^：:]+)[：:]\s*(.*)$")


class Draft:
    def __init__(self, spec, path):
        self.path = path
        with open(path, encoding="utf-8") as f:
            self.text = f.read()
        errors, _ = orgspec.lint_text(spec, self.text)
        if errors:
            raise LedgerError("草稿没通过检查，先改好：\n" + "\n".join(errors))
        self.header = {}
        for line in self.text.splitlines():
            m = HEADER.match(line)
            if m and m.group(1) not in self.header:
                self.header[m.group(1)] = m.group(2).strip()
            if line.startswith("## "):
                break
        if "包类型" not in self.header:
            raise LedgerError("草稿的包头没有“包类型”：%s" % path)
        names = {v: k for k, v in spec.terms["pkg"].items()}
        self.pid = names[self.header["包类型"].split("（")[0].strip()]
        rels = {v: k for k, v in spec.terms["rel"].items()}
        self.rel = rels.get(self.header.get("关系", "").strip())
        title = next((l[2:].strip() for l in self.text.splitlines() if l.startswith("# ")), "")
        self.task = re.split(r"[：:]", title, 1)[-1].strip() if title else os.path.basename(path)
        m = re.search(r"（([A-Za-z]+-p[0-2]-[\w]+)）", self.header.get("发出方", ""))
        self.author = m.group(1) if m else None
        seq_by_cn = {spec.term("seq", s): s for s, _, _ in spec.sequences()}
        self.sender_seq = next((s for cn, s in seq_by_cn.items() if self.header.get("发出方", "").startswith(cn)), None)
        self.receiver_seq = next((s for cn, s in seq_by_cn.items() if self.header.get("接收方", "").startswith(cn)), None)
        link = self.header.get("关联工作项", "")
        self.linked = re.findall(r"#(\d+)", link)
        src = re.search(r"来源工作项[：:\s]*#?(\d+)", self.text)
        self.source = src.group(1) if src else None

    def section(self, name):
        m = re.search(r"\n## %s\n(.*?)(?:\n## |\Z)" % re.escape(name), self.text, re.S)
        return m.group(1).strip() if m else ""

    def section_choice(self, name):
        """The first line of a section, without trailing punctuation or explanation, to match an option."""
        first = (self.section(name).splitlines() or [""])[0].strip()
        return re.split(r"[：:。；;，,（(]", first, 1)[0].strip() or None

    def body_without_title(self):
        return "\n".join(l for l in self.text.splitlines() if not l.startswith("# ")).strip() + "\n"


STATUS_SECTION = "## 状态包\n\n### 简介\n\n%s### 当前状态\n\n### 近期变化\n\n### 风险\n"


def cognitive_layer(body):
    """The 认知层 paragraph of a draft; a 送审包 has none and uses its 待审结论. '' when neither."""
    for heading in ("认知层", "待审结论"):
        m = re.search(r"^## %s[ \t]*\n(.*?)(?=^## |\Z)" % heading, body, re.M | re.S)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def with_status_sections(body):
    """Main documents end with 状态包 and then 交接记录; add whichever the draft lacks.
    A new 状态包 starts with the 认知层 as its 简介: the router matches against it."""
    has_status = re.search(r"^## 状态包\s*$", body, re.M)
    has_log = re.search(r"^## 交接记录\s*$", body, re.M)
    if has_status and has_log:
        return body
    intro = cognitive_layer(body)
    status = STATUS_SECTION % (intro + "\n\n" if intro else "")
    if has_log:
        return body[:has_log.start()] + status + "\n" + body[has_log.start():]
    if has_status:
        return body.rstrip("\n") + "\n\n## 交接记录\n"
    return body + "\n" + status + "\n## 交接记录\n"


# ------------------------------------------------------------- receipts
def sent_dir(lg):
    return os.path.join(lg.root, "packages", "sent")


def find_receipt(lg, path):
    rel = os.path.relpath(path, lg.root)
    d = sent_dir(lg)
    if not os.path.isdir(d):
        return None
    for name in sorted(os.listdir(d)):
        if name.endswith(".json") and not name.startswith("reply-"):
            rec = load_json(os.path.join(d, name))
            if isinstance(rec, dict) and rec.get("draft") == rel:
                rec["_file"] = "packages/sent/%s" % name
                return rec
    return None


def receipt_file(lg, stems, path):
    """packages/sent/<stem>.json for the first stem (then <stem>-2.json, -3 ...) that is free
    or already belongs to this draft: a receipt is never taken over by another draft."""
    rel = os.path.relpath(path, lg.root)
    for stem in stems:
        for k in range(1, 100):
            full = os.path.join(sent_dir(lg), "%s.json" % stem if k == 1 else "%s-%d.json" % (stem, k))
            rec = load_json(full, {}) if os.path.exists(full) else {}
            if isinstance(rec, dict) and (not rec or rec.get("draft") == rel):
                return full
            if stem == stems[0] and len(stems) > 1:
                break
    raise LedgerError("packages/sent/ 里同名的回执太多了：%s" % stems[-1])


def safe_name(path):
    return re.sub(r"[^\w.-]", "_", os.path.basename(path))


def receipt_path(lg, path, item_id):
    """packages/sent/<ID>.json, or <ID>-<draft file>.json when another draft already holds <ID>.json
    (a second 立项单 attached to the same project with --existing)."""
    return receipt_file(lg, [str(item_id), "%s-%s" % (item_id, safe_name(path))], path)


def save_receipt(lg, path, item_id, status, **extra):
    """packages/sent/<ID>.json: written right after the work item exists, updated as steps finish.

    Keeps what a --resume needs: the source, the --set pairs, and the document as
    soon as it exists (docx), then its /wiki/ link (doc).
    """
    if lg.mode != "apply":
        return
    os.makedirs(sent_dir(lg), exist_ok=True)
    target = receipt_path(lg, path, item_id)
    rec = load_json(target, {}) or {}
    rel = os.path.relpath(path, lg.root)
    if not isinstance(rec, dict) or rec.get("draft") != rel:
        rec = {}
    rec.update({"draft": rel, "work_item_id": item_id, "status": status, "updated_at": now()})
    rec.update({k: v for k, v in extra.items() if v is not None})
    save_json(target, rec)


def check_receipt(lg, path, resume, again=""):
    """The receipt to continue from with --resume (or --doc); None for a fresh registration."""
    rec = find_receipt(lg, path)
    if not rec:
        if resume:
            raise LedgerError("没有这份草稿的登记回执，不用 --resume")
        return None
    item_id = rec.get("work_item_id")
    if rec.get("status") == "registered":
        raise LedgerError("这份草稿已经登记成 #%s（回执 %s），不要重复登记%s" % (item_id, rec["_file"], again))
    if not resume:
        step = "写回主文档链接" if rec.get("doc") else ("查主文档的知识库节点" if rec.get("docx") else "建主文档")
        raise LedgerError("上次已经建了工作项 #%s，但没做完（停在：%s）。修好原因后加 --resume 接着做，不会再建一个" % (item_id, step))
    try:
        lg.item(item_id)
    except LedgerError as e:
        raise LedgerError("回执里的工作项 #%s 读不到了（%s）。如果它已经被删掉，删掉回执 %s 再重新登记" % (
            item_id, str(e)[:120], rec["_file"]))
    return rec


def doc_from_link(lg, link, given=False):
    """/wiki/ link of a document known by a /wiki/ or /docx/ link or a document_id.
    given=True checks a link typed with --doc before using it."""
    link = (link or "").strip()
    if given and not re.match(r"https?://[^/\s]+/(?:wiki|docx)/[A-Za-z0-9]{6,}$", link):
        raise LedgerError("--doc 要给文档的 /wiki/ 或 /docx/ 链接，或者 new：%s" % link)
    if "/wiki/" in link:
        return link
    return lg.doc_link(link if link.startswith("http") else None, None if link.startswith("http") else link)


def main_document(lg, rec, doc, where, create, remember):
    """The /wiki/ link of the draft's document: given with --doc, kept in the receipt, or created now.

    create() makes the document and calls remember(docx=...) as soon as it exists;
    remember(**kw) updates the receipt. When docs +create timed out or answered
    without a link, the receipt keeps doc_unknown and the next run stops until --doc
    says what to do: --doc <链接> takes over the document found on the web, --doc new
    creates another one.
    """
    if doc and doc.strip().lower() != "new":
        url = doc_from_link(lg, doc, given=True)
        remember(doc=url, doc_unknown=False)
        return url
    if rec.get("doc"):
        return rec["doc"]
    if rec.get("docx"):
        url = doc_from_link(lg, rec["docx"])
        remember(doc=url)
        return url
    if rec.get("doc_unknown") and (doc or "").strip().lower() != "new":
        raise LedgerError("上次建文档没有拿到确定的结果，文档可能已经建在%s下了。先在网页上核对：建好了的，重跑时加 --doc <文档链接> 接着用它；确认没建成的，加 --doc new 重新建" % where)
    try:
        url = create()
    except WriteMaybeDone:
        remember(doc_unknown=True)
        raise
    remember(doc=url, doc_unknown=False)
    return url


def resolved_pairs(lg, pairs):
    """--set pairs with @file replaced by the file's text, as kept in a receipt: a --resume
    writes the same values even if the file has moved or changed."""
    out = []
    for pair in pairs or []:
        fid, eq, raw = pair.partition("=")
        if not eq:
            raise LedgerError("--set 要写成 字段标识=值：%s" % pair)
        value = set_value(lg, raw)
        out.append("%s=%s" % (fid, "@" + value if value.startswith("@") else value))
    return out


def merge_pairs(*lists):
    """Later --set pairs win over earlier ones for the same field."""
    merged = {}
    for pairs in lists:
        for pair in pairs or []:
            merged[pair.partition("=")[0]] = pair
    return list(merged.values())


def write_project_id(lg, item_id):
    if lg.mode != "apply":
        return
    m = os.path.join(lg.root, "feishu", "ids.json")
    ids = load_json(m, {})
    if (ids.get("meegle") or {}).get("project_item_id") == item_id:
        return
    ids.setdefault("meegle", {})["project_item_id"] = item_id
    save_json(m, ids)


def work_item_type(draft):
    if draft.pid == "request":
        return "cross_request" if draft.rel == "cross" else "same_seq"
    if draft.pid in ("review", "decision"):
        return draft.pid
    raise LedgerError("%s是答复，不新建工作项；用 reply" % draft.header["包类型"])


def parent_node(lg, draft, wid, source):
    seq = draft.sender_seq
    if draft.pid == "request" and draft.rel == "execute":
        if not source:
            raise LedgerError("执行关系的需求包要写来源工作项（所属阶段的下发需求包）：--source <ID>")
        node = lg.node_of(lg.doc_url(source))
        if not node:
            raise LedgerError("来源工作项 #%s 的主文档不是知识库文档，没法把执行需求包挂在它下面" % source)
        return node
    key = {"same_seq": "same_seq/%s" % seq, "cross_request": "cross_request",
           "review": "review/%s" % seq, "decision": "decision"}[wid]
    n = lg.nodes.get(key)
    if not n or not n.get("node_token"):
        raise LedgerError("ids.json 里没有知识库节点 %s；先跑 ensure-node %s" % (key, key))
    return n["node_token"]


def ensure_role(lg, wid, item_id):
    """After create, check that the researcher is the current operator (the state's role is 研究员).

    Returns "ok", "added", "unknown" (could not tell) or None (not checked: not
    apply mode, or the dictionary has no 研究员 role).
    """
    if lg.mode != "apply":
        return None
    role = lg.researcher_role(wid)
    if not role:
        return None
    try:
        data = lg.get(item_id)            # the default answer carries current_status_operator
        entry = next((f for f in (data or {}).get("work_item_fields") or []
                      if isinstance(f, dict) and f.get("key") == "current_status_operator"), None)
        if entry is None:
            print("提醒：读不到 #%s 的“当前负责人”，没法确认研究员角色有没有人；请在网页上核对。" % item_id)
            return "unknown"
        if entry.get("value") and lg.me() in json.dumps(entry.get("value"), ensure_ascii=False):
            return "ok"
        lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--role-operate",
                  json.dumps([{"op": "add", "role_key": role, "user_keys": [lg.me()]}]), write=True)
        print("建工作项时带的角色人员没生效，已用 --role-operate 把研究员补进“%s”角色。" % lg.spec.term("role", "researcher"))
        return "added"
    except LedgerError as e:
        print("提醒：没能核对 #%s 的角色人员：%s" % (item_id, str(e)[:200]))
        return "unknown"


def template_entry(lg, wid):
    tpl = (lg.types.get(wid) or {}).get("template_id")
    if not tpl:
        if lg.mode != "offline":
            raise LedgerError("ids.json 里没有%s的模板 ID；先跑 setup.py feishu --live --apply" % lg.spec.term("wi", wid))
        tpl = "<模板 ID>"
    return {"field_key": "template", "field_value": str(tpl)}


def rewrite_link(lg, path, text, item_id):
    if lg.mode == "apply":
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace("关联工作项：待建", "关联工作项：#%s" % item_id))


def register_project(lg, path, pairs, resume=False, existing=False, doc=None):
    """立项单: create the project work item with the charter as its main document.

    With existing=True, a project already recorded in ids.json gets the new
    charter as its main document instead (no second project is created).
    """
    project = (lg.ids.get("meegle") or {}).get("project_item_id")
    again = "。立项单改过、要换主文档的，把修订版另存成新的草稿文件，再用 register <新文件> --existing 登记"
    rec = check_receipt(lg, path, resume or bool(doc) and bool(find_receipt(lg, path)), again) if lg.mode != "offline" else None
    pairs = merge_pairs((rec or {}).get("pairs"), resolved_pairs(lg, pairs))
    sets = parse_sets(lg, "project", pairs)               # a bad --set value stops before anything is written
    if project and not rec and not existing:
        raise LedgerError("已经有项目 #%s。要把这份立项单换成它的主文档，加 --existing：ledger.py register %s --existing" % (
            project, path))
    if existing and not project and not rec:
        raise LedgerError("ids.json 里没有已有的项目（meegle.project_item_id）。去掉 --existing 新建项目，或者先把项目 ID 写进 feishu/ids.json")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), "")
    name = re.split(r"[：:]", title, 1)[-1].strip()
    parent = (lg.nodes.get("overview") or {}).get("node_token")
    if not parent:
        raise LedgerError("ids.json 里没有“项目总览”节点；先跑 ensure-node overview")
    owners = None
    if rec:
        item_id = rec["work_item_id"]
    elif project and existing:
        item_id = str(project)
        save_receipt(lg, path, item_id, "created", pairs=pairs)
        print("把立项单挂到已有的项目 #%s 上。" % item_id)
    else:
        owners = lg.role_owners("project")
        fields = [{"field_key": "name", "field_value": name}, template_entry(lg, "project")]
        fields += [v for _, v in sets]
        if owners:
            fields.append(owners)
        created = lg.meegle("workitem", "create", "--work-item-type", lg.api("project"), "--fields",
                            json.dumps(fields, ensure_ascii=False), write=True)
        item_id = created_id(created, "项目") if created is not None else "<项目工作项 ID>"
        save_receipt(lg, path, item_id, "created", pairs=pairs)
        write_project_id(lg, item_id)                     # a revised charter with --existing finds it at once
    role = None
    state = {}

    def remember(**kw):
        state.update(kw)
        save_receipt(lg, path, item_id, "created", **kw)

    def create():
        body = "\n".join(l for l in text.splitlines() if not l.startswith("# ")).replace(
            "关联工作项：待建", "关联工作项：#%s" % item_id)
        return lg.create_doc(parent, title, body + "\n", created=lambda d: remember(docx=d, doc_unknown=False))

    try:
        if owners:
            role = ensure_role(lg, "project", item_id)
        url = main_document(lg, rec or {}, doc, "“项目总览”节点（%s）" % parent, create, remember)
        updates = [{"field_key": lg.field("project", "main_doc")["field_key"], "field_value": url}]
        updates += [v for _, v in sets]                   # --existing: the --set values go in with the main document
        lg.meegle("workitem", "update", "--work-item-id", item_id, "--fields",
                  json.dumps(updates, ensure_ascii=False), write=True)
    except LedgerError as e:
        if state.get("doc_unknown", (rec or {}).get("doc_unknown")) and not (state.get("docx") or state.get("doc")):
            raise WriteMaybeDone("%s\n项目 #%s 已经在了，建立项单文档没有拿到确定的结果：先在网页上“项目总览”节点下核对。建好了的，运行 ledger.py register %s --resume --doc <文档链接> --apply；确认没建成的，用 --doc new" % (
                e, item_id, path))
        raise LedgerError("%s\n项目 #%s 已经在了；修好后运行 ledger.py register %s --resume --apply 接着做（--set 的值已记在回执里）" % (
            e, item_id, path))
    if lg.mode == "apply":
        write_project_id(lg, item_id)
        rewrite_link(lg, path, text, item_id)
        save_receipt(lg, path, item_id, "registered", doc=url)
    if rec:
        who = "（接着上次做完；角色人员请在网页上核对。）"
    elif owners and role in ("ok", "added"):
        who = "负责人角色填的是研究员。"
    elif owners and role == "unknown":
        who = "负责人角色没能核对，请研究员在网页上看一眼。"
    elif owners:
        who = "负责人角色随创建写入了研究员（预览时不核对）。"
    elif project and existing:
        who = ""
    else:
        who = "负责人角色没填上，请研究员在网页上补。"
    print("登记：项目 #%s，立项单放在项目总览下。%s" % (item_id, who))
    return item_id


def register(lg, path, source=None, extra=None, pairs=None, resume=False, existing=False, doc=None):
    spec = lg.spec
    with open(path, encoding="utf-8") as f:
        first = next((l for l in f.read().splitlines() if l.startswith("# ")), "")
    if first.startswith("# 立项单"):
        return register_project(lg, path, pairs, resume, existing, doc)
    d = Draft(spec, path)
    wid = work_item_type(d)
    if not d.author:
        raise LedgerError("包头的发出方要带会话名，比如“算法 +2（Alg-p2-01）”")
    rec = check_receipt(lg, path, resume or bool(doc) and bool(find_receipt(lg, path))) if lg.mode != "offline" else None
    if rec:
        source = source or rec.get("source")
        pairs = merge_pairs(rec.get("pairs"), resolved_pairs(lg, pairs))   # rewritten with the main document
    source = item_id_of(source or d.source) if (source or d.source) else None
    pkg = spec.pkg(d.pid)
    name = spec.rules["naming"]["work_item"].replace("<包名>", pkg).replace("<作者会话名>", d.author).replace("<任务名>", d.task)

    # Everything that can fail is checked before the work item exists.
    fields = [{"field_key": "name", "field_value": name}, template_entry(lg, wid)]
    required = [("sender", d.author)]
    seq = d.receiver_seq if wid == "cross_request" else d.sender_seq
    if not seq:
        raise LedgerError("从包头的发出方、接收方认不出序列")
    required.append(("sequence", spec.term("seq", seq)))
    if wid == "same_seq":
        if not d.rel:
            raise LedgerError("同序列需求要写关系：下发或执行")
        rank = re.search(r"-p([0-2])-", d.author or "")
        expected = {"2": "assign", "1": "execute"}.get(rank.group(1)) if rank else None
        if expected != d.rel:                       # +2 发下发关系的包，+1 发执行关系的包
            raise LedgerError("关系“%s”和发出方 %s 的职级对不上：+2 发的同序列需求是下发，+1 发的是执行；退回作者改包头" % (
                spec.term("rel", d.rel), d.author))
        required.append(("relation", spec.term("rel", d.rel)))
    if wid == "cross_request":
        if not (d.sender_seq and d.receiver_seq):
            raise LedgerError("跨序列需求要认得出发起序列和承接序列")
        required += [("from_sequence", spec.term("seq", d.sender_seq)), ("to_sequence", spec.term("seq", d.receiver_seq))]
    if source:
        required.append(("source_item", source))
    for fid, raw in required:
        fields.append(lg.entry(wid, fid, raw))
    optional = []
    if utc_noon_ms(d.header.get("回复期限", "")):
        optional.append(("due", d.header["回复期限"]))
    elif re.search(r"\d{4}-\d{1,2}-\d{1,2}", d.header.get("回复期限", "")):
        print("提醒：回复期限“%s”不是存在的日期，截止时间没写；改好后用 update 补" % d.header["回复期限"])
    if wid == "decision":
        optional += [("urgency", d.header.get("紧急程度")), ("recommendation", d.section("建议")[:500]),
                     ("default_action", d.header.get("默认处理")), ("channel", d.header.get("渠道"))]
    if wid == "review":
        optional += [("claim_type", d.section_choice("结论类型")), ("usage", d.section_choice("用途"))]
    project = (lg.ids.get("meegle") or {}).get("project_item_id")
    if project:
        optional.append(("project_ref", str(project)))
    optional += list((extra or {}).items())
    for fid, raw in optional:
        if not raw or "/" in str(raw) and fid in ("urgency", "channel"):
            continue
        try:
            fields.append(lg.entry(wid, fid, raw))
        except (LedgerError, KeyError, orgspec.OrgSpecError) as e:
            print("提醒：跳过字段 %s（不影响登记，之后可以用 update 补）：%s" % (fid, e))
    sets = parse_sets(lg, wid, pairs)                 # on --resume, all values are written again with the main document
    if not rec:
        fields += [v for _, v in sets]
    owners = lg.role_owners(wid)
    if owners:
        fields.append(owners)
    parent = parent_node(lg, d, wid, source)

    if rec:
        item_id = rec["work_item_id"]
        print("接着上次的 #%s 做。" % item_id)
        save_receipt(lg, path, item_id, "created", pairs=pairs)                 # values given on --resume too
    else:
        created = lg.meegle("workitem", "create", "--work-item-type", lg.api(wid), "--fields",
                            json.dumps(fields, ensure_ascii=False), write=True)
        item_id = created_id(created, spec.term("wi", wid)) if created is not None else "<新工作项 ID>"
        save_receipt(lg, path, item_id, "created", source=source, pairs=resolved_pairs(lg, pairs))
    title = spec.rules["naming"]["main_doc"].replace("<工作项 ID>", item_id).replace("<包名>", pkg) \
        .replace("<作者会话名>", d.author).replace("<任务名>", d.task)

    state = {}

    def remember(**kw):
        state.update(kw)
        save_receipt(lg, path, item_id, "created", **kw)

    def create():
        link = lg.item_url(wid, item_id)
        ref = "[#%s](%s)" % (item_id, link) if link else "#%s" % item_id
        body = with_status_sections(d.body_without_title().replace("关联工作项：待建", "关联工作项：%s" % ref))
        return lg.create_doc(parent, title, body, created=lambda x: remember(docx=x, doc_unknown=False))

    try:
        if owners and not rec:
            ensure_role(lg, wid, item_id)
        url = main_document(lg, rec or {}, doc, "节点 %s " % parent, create, remember)
        updates = [{"field_key": lg.field(wid, "main_doc")["field_key"], "field_value": url}]
        if rec:
            updates += [v for _, v in sets]
        if wid == "same_seq" and d.rel == "execute":
            key = lg.key_of(wid, "owner_session")
            if key:
                updates.append({"field_key": key, "field_value": "%s-p0-%s" % (spec.term("abbrev", d.sender_seq), item_id)})
        lg.meegle("workitem", "update", "--work-item-id", item_id, "--fields",
                  json.dumps(updates, ensure_ascii=False), write=True)
        lg.meegle("comment", "add", "--work-item-id", item_id, "--content",
                  "%s 已登记：%s。主文档：%s" % (lg.tag, title, url), write=True)
    except LedgerError as e:
        if state.get("doc_unknown", (rec or {}).get("doc_unknown")) and not (state.get("docx") or state.get("doc")):
            raise WriteMaybeDone("%s\n工作项 #%s 已经建了，建主文档没有拿到确定的结果：先在网页上节点 %s 下核对。建好了的，运行 ledger.py register %s --resume --doc <文档链接> --apply；确认没建成的，用 --doc new" % (
                e, item_id, parent, path))
        raise LedgerError("%s\n工作项 #%s 已经建了；修好后运行 ledger.py register %s --resume --apply 接着做，不会再建一个工作项（来源工作项和 --set 的值已记在回执里）" % (
            e, item_id, path))
    if lg.mode == "apply":
        rewrite_link(lg, path, d.text, item_id)
        save_receipt(lg, path, item_id, "registered", doc=url)
    if wid == "decision" and d.header.get("渠道") == spec.term("channel", "researcher"):
        sec = d.section("决定")
        content = re.search(r"决定内容[：:]\s*(.+)", sec)
        decider = re.search(r"决定人[：:]\s*(.+)", sec)
        if content and decider:
            print("研究员主动发起的决策，接着流转：ledger.py transition %s 已决定 --set decision_content='%s' --set decider=研究员"
                  % (item_id, content.group(1).strip()))
        else:
            print("研究员主动发起的决策，但“决定”一节没写全决定内容和决定人：退回作者补上，再流转到“已决定”。")
    print("登记：%s #%s，主文档放在节点 %s 下。" % (spec.term("wi", wid), item_id, parent))
    if wid == "same_seq":
        print("请求方说马上派下一级的（登记请求写了“开工”），接着流转：ledger.py transition %s 执行中；排队没派的留在“待开工”。" % item_id)
    return item_id


def reply(lg, path, doc=None):
    """Hang a 交付包 or 验收包 under the main document it answers.

    The target field and the parent node are worked out before anything is
    written; a receipt packages/sent/reply-<ID>-<file>.json lets a rerun reuse the
    document instead of creating a second one.
    """
    spec = lg.spec
    d = Draft(spec, path)
    if d.pid not in ("delivery", "verdict"):
        raise LedgerError("%s要用 register 登记" % d.header["包类型"])
    if not d.linked:
        raise LedgerError("包头的“关联工作项”要写它答复的工作项 ID，比如 #2003")
    parent_id = item_id_of(d.linked[0])
    rpath = receipt_file(lg, ["reply-%s-%s" % (parent_id, safe_name(path))], path)
    rec = (load_json(rpath, {}) or {}) if lg.mode != "offline" else {}
    rec = rec if isinstance(rec, dict) else {}
    if rec.get("status") == "registered":
        raise LedgerError("这份%s已经挂到 #%s 下了（回执 %s），不要重复登记" % (spec.pkg(d.pid), parent_id, os.path.relpath(rpath, lg.root)))
    it = lg.item(parent_id)
    if it is not None and not it.get("type"):
        raise LedgerError("认不出 #%s 的类型（type_key %s）；确认工作项 ID 没写错，或者先跑 setup.py feishu --live --apply 重读类型" % (
            parent_id, it.get("type_key")))
    wid = (it and it.get("type")) or ("review" if d.pid == "verdict" else "same_seq")
    if d.pid == "delivery" and wid not in ("same_seq", "cross_request"):
        raise LedgerError("交付包答复的是同序列需求或跨序列需求，#%s 是%s" % (parent_id, spec.term("wi", wid)))
    if d.pid == "verdict" and wid != "review":
        raise LedgerError("验收包答复的是送审，#%s 是%s" % (parent_id, spec.term("wi", wid)))
    link_key = lg.field(wid, "delivery_link")["field_key"] if d.pid == "delivery" else None
    node = lg.node_of(lg.doc_url(parent_id, it))
    if not node:
        raise LedgerError("#%s 的主文档不是知识库文档，答复没处挂" % parent_id)
    title = spec.rules["naming"]["reply_doc"].replace("<答复包名>", spec.pkg(d.pid)) \
        .replace("<作者会话名>", d.author or "?").replace("<同一个任务名>", d.task)

    def remember(**kw):
        rec.update({k: v for k, v in kw.items() if v is not None})
        if lg.mode == "apply":
            rec.update(draft=os.path.relpath(path, lg.root), parent=parent_id, updated_at=now())
            rec.setdefault("status", "created")
            os.makedirs(sent_dir(lg), exist_ok=True)
            save_json(rpath, rec)

    def create():
        return lg.create_doc(node, title, d.body_without_title(), created=lambda x: remember(docx=x, doc_unknown=False))

    try:
        link = main_document(lg, dict(rec), doc, "#%s 的主文档下" % parent_id, create, remember)
        if d.pid == "delivery":
            lg.meegle("workitem", "update", "--work-item-id", parent_id, "--fields", json.dumps(
                [{"field_key": link_key, "field_value": link}]), write=True)
            lg.meegle("comment", "add", "--work-item-id", parent_id, "--content",
                      "%s 收到%s：%s" % (lg.tag, spec.pkg("delivery"), link), write=True)
        else:
            lg.meegle("comment", "add", "--work-item-id", parent_id, "--content",
                      "%s 验收包：%s" % (lg.tag, link), write=True)
    except LedgerError as e:
        receipt = os.path.relpath(rpath, lg.root)
        if rec.get("doc_unknown") and not (rec.get("doc") or rec.get("docx")):
            raise LedgerError("%s\n建文档没有拿到确定的结果（回执 %s）：先在网页上核对 #%s 的主文档下有没有这份%s；建好了的，重跑时加 --doc <文档链接>；确认没建成的，加 --doc new" % (
                e, receipt, parent_id, spec.pkg(d.pid)))
        if rec.get("doc") or rec.get("docx"):
            raise LedgerError("%s\n文档已经建好，回执记在 %s；修好后重跑同一条命令，会接着用这份文档，不会再建一个" % (e, receipt))
        raise LedgerError("%s\n文档还没建，飞书里没有写入；修好后重跑同一条命令即可" % e)
    remember(status="registered")
    if d.pid == "delivery":
        if wid == "cross_request":
            print("跨序列交付：确认结论已有验收包后，ledger.py transition %s 已交付 --set delivery_link=%s" % (parent_id, link))
        else:
            print("交付包已挂到 #%s 下。执行结果要先有验收包，才流转到下一步。" % parent_id)
    else:
        reviewed = lg.text_of(it, "source_item") or "<被送审的工作项>"
        conclusion = d.section_choice("结论") or "<接受/缩小/否决>"
        print("接着流转（把值核对后执行）：")
        if not it or it.get("state") == "待核查":
            print("  ledger.py transition %s 核查中" % parent_id)
        print("  ledger.py transition %s 已出结论 --set conclusion='%s' --set usable_statement=@<可使用的结论原文文件> --set invalidation=@<失效条件文件>" % (parent_id, conclusion))
        print("  ledger.py transition %s 已验收 --set verdict='%s' --set verdict_link=%s   （否决时改流转到“执行中”，只填验收结论）" % (reviewed, conclusion, link))
    return link


def set_value(lg, raw):
    """--set key=@file reads the file (relative to the current directory, then the project);
    @@text keeps a literal @text.

    When no such file exists, the value stops the command if it could be a file name:
    one word, or anything with a /, a .ext ending or a <placeholder>. Only text with a
    space and none of those (@研究员 还没批预算) is kept as it is.
    """
    if raw.startswith("@@"):
        return raw[1:]
    if not raw.startswith("@") or len(raw) == 1:
        return raw
    name = raw[1:]
    for base in dict.fromkeys((os.getcwd(), lg.root)):
        full = name if os.path.isabs(name) else os.path.join(base, name)
        if os.path.isfile(full):
            try:
                with open(full, encoding="utf-8") as f:
                    return f.read().strip()
            except UnicodeDecodeError:
                raise LedgerError("文件 %s 不是 UTF-8 文本；转成 UTF-8 再传" % name)
    if not re.search(r"\s", name) or re.search(r"/|<|>|\.[A-Za-z0-9]{1,5}$", name):
        raise LedgerError("找不到文件 %s（--set 的 @文件 先按当前目录、再按项目目录找）；要写以 @ 开头的文字，用 @@" % name)
    return raw


def parse_sets(lg, wid, pairs):
    out = []
    for pair in pairs or []:
        if "=" not in pair:
            raise LedgerError("--set 要写成 字段标识=值：%s" % pair)
        fid, raw = pair.split("=", 1)
        try:
            out.append((fid, lg.entry(wid, fid, set_value(lg, raw))))
        except (orgspec.OrgSpecError, KeyError) as e:
            raise LedgerError("--set %s：%s（%s的字段标识见 org/org.md 的“字段”表）" % (fid, e, lg.spec.term("wi", wid)))
    return out


def transition(lg, item_id, target, pairs, wid_hint=None):
    """Move a work item to target. When the space answers, its form is the requirement;
    the rules form is compared with it and differences are reported, not enforced."""
    spec = lg.spec
    it = lg.item(item_id)
    wid = (it and it.get("type")) or wid_hint
    if not wid:
        raise LedgerError("读不出 #%s 的类型；用 --type 指明" % item_id)
    w = spec.work_item(wid)
    if target in system_states(spec):
        raise LedgerError("“%s”不是一次流转，要研究员在网页上操作" % target)
    if target not in w["states"]:
        raise LedgerError("%s 没有状态“%s”；可选：%s" % (spec.term("wi", wid), target, "、".join(w["states"])))
    current = it["state"] if it else None
    if current and [current, target] not in w["transitions"]:
        allowed = "、".join(b for a, b in w["transitions"] if a == current) or "无"
        raise LedgerError("%s 不能从“%s”流转到“%s”；能去：%s" % (spec.term("wi", wid), current, target, allowed))
    sets = parse_sets(lg, wid, pairs)
    given = {fid for fid, _ in sets}
    if target == "阻塞" and "blocked_since" not in given:
        sets += parse_sets(lg, wid, ["blocked_since=%s" % datetime.date.today().isoformat()])   # local date

    # What the space offers: the transition id and its form.
    options = lg.meegle("workflow", "list-state-transitions", "--work-item-id", str(item_id))
    tid, space_form = "<transition_id>", None
    if options is not None:
        choices = options.get("transition") or []
        t = next((c for c in choices if c.get("state_name") == target), None)
        if not t:
            raise LedgerError("空间里 #%s 从“%s”去不了“%s”；能去：%s" % (
                item_id, options.get("state_name") or current, target,
                "、".join(c.get("state_name", "") for c in choices) or "无"))
        tid = t.get("id")
        if tid in (None, ""):
            raise LedgerError("空间给的流转没有 id：%s" % json.dumps(t, ensure_ascii=False)[:300])
        space_form = {f.get("key"): f.get("name") for f in t.get("confirm_form") or [] if f.get("key")}

    # What the rules say the form should be. Without the current state (offline), only
    # what every transition into the target requires: a transition without a form requires nothing.
    notes = []
    if current is None:
        incoming = [a for a, b in w["transitions"] if b == target]
        per_source = []
        for a in incoming:
            f = next((f for f in w.get("forms", []) if f["from"] == a and f["to"] == target), None)
            per_source.append((f or {}).get("required", []))
        common = set(per_source[0]).intersection(*per_source[1:]) if per_source else set()
        required = [fid for req in per_source for fid in req if fid in common]
        required = list(dict.fromkeys(required))
        if len(incoming) > 1:
            notes.append("不知道当前状态，只按所有能进入“%s”的流转都要求的字段检查" % target)
    else:
        form = next((f for f in w.get("forms", []) if f["from"] == current and f["to"] == target), None)
        required = list((form or {}).get("required", []))
    rule_keyed, rule_unbuilt = {}, []
    for fid in required:
        key = lg.key_of(wid, fid)
        if key:
            rule_keyed[key] = fid
        else:
            rule_unbuilt.append(fid)
    by_key = lg.fid_by_key(wid)
    unbuilt_by_name = {spec.field_name(f["id"]): f["id"] for f in spec.fields_of(wid) if not lg.key_of(wid, f["id"])}

    def label(key, name=None):
        fid = by_key.get(key)
        if fid:
            return "%s（%s，用 --set 传）" % (fid, spec.field_name(fid))
        if name in unbuilt_by_name:
            return "%s（%s：空间里已经建了，还没读回；先跑 setup.py feishu --live --apply，再用 --set %s=… 传）" % (
                unbuilt_by_name[name], name, unbuilt_by_name[name])
        return "%s（%s：规则文件里没有这个字段；请研究员在网页上流转时填，或者更新规则文件）" % (key, name or key)

    given_keys = {v["field_key"] for _, v in sets}
    wanted = set(space_form) if space_form is not None else set(rule_keyed)
    people = [k for k in wanted - given_keys if k in by_key and spec.field(wid, by_key[k]).get("type") == "user"]
    if it and people:
        lg.people(it, item_id, people)                 # 决定人 already filled in counts
    have_keys = {k for k, v in (it["fields"] if it else {}).items() if plain(v)}
    if space_form is not None:
        need = space_form
        missing = [label(k, n) for k, n in need.items() if k not in given_keys and k not in have_keys]
    else:
        need = {k: spec.field_name(fid) for k, fid in rule_keyed.items()}
        missing = [label(k) for k in need if k not in given_keys and k not in have_keys]
        missing += ["%s（%s：还没绑定；空间里没有的建好，名字不同的用 --map 指认，再跑 setup.py feishu --live --apply）" % (fid, spec.field_name(fid))
                    for fid in rule_unbuilt]
    if space_form is not None:
        rule_only = [rule_keyed[k] for k in rule_keyed if k not in space_form] + rule_unbuilt
        space_only = [k for k in space_form if k not in rule_keyed]
        if rule_only or space_only:
            notes.append("空间里这一步的流转表单是 %s，规则文件写的是 %s；以空间为准，差异报给研究员核对" % (
                "、".join(space_form.values()) or "无",
                "、".join(spec.field_name(f) for f in list(rule_keyed.values()) + rule_unbuilt) or "无"))
    if missing:
        raise LedgerError("流转到“%s”要填：%s。%s" % (
            target, "、".join(missing), ("（%s）" % "；".join(notes)) if notes else ""))
    for n in notes:
        print("提醒：%s。" % n)
    if sets:
        lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--fields",
                  json.dumps([v for _, v in sets], ensure_ascii=False), write=True)
    lg.meegle("workflow", "transition-state", "--work-item-id", str(item_id), "--transition-id", str(tid), write=True)
    if lg.mode == "apply":
        after = lg.item(item_id)
        if after and after["state"] != target:
            raise LedgerError("流转后回读状态是“%s”，不是“%s”" % (after["state"], target))
    print("流转：#%s %s→%s" % (item_id, current or "?", target))


def update(lg, item_id, pairs, wid_hint=None):
    it = lg.item(item_id)
    wid = (it and it.get("type")) or wid_hint
    if not wid:
        raise LedgerError("读不出 #%s 的类型；用 --type 指明" % item_id)
    sets = parse_sets(lg, wid, pairs)
    lg.meegle("workitem", "update", "--work-item-id", str(item_id), "--fields",
              json.dumps([v for _, v in sets], ensure_ascii=False), write=True)


SESSION_TAG = re.compile(r"\s*\[[A-Za-z]+\+[0-2]\]")


def comment(lg, item_id, text):
    if not SESSION_TAG.match(text):
        text = "%s %s" % (lg.tag, text)
    lg.meegle("comment", "add", "--work-item-id", str(item_id), "--content", text, write=True)


HEADING = re.compile(r'<h([1-9])\b[^>]*?\bid="([^"]+)"[^>]*>(.*?)</h\1>', re.S)


def heading_ids(lg, url):
    """[(block_id, heading text)] from `docs +fetch --scope outline --detail with-ids`."""
    data = lg.lark("docs", "+fetch", "--doc", url, "--scope", "outline", "--max-depth", "2", "--detail", "with-ids")
    if data is None:
        return []
    return [(m.group(2), re.sub(r"<[^>]+>", "", m.group(3)).strip()) for m in HEADING.finditer(doc_content(data))]


def last_block_before(xml, end_id):
    """Id of the last top-level block before end_id in a range fragment.

    --start-block-id and --end-block-id must be siblings. Lists come back as a
    <ul>/<ol> without an id around <li id=...>, so a list ends at its last <li>;
    an <excerpt top-block-id=...> wrapper stands for its top-level block.
    """
    root = ET.fromstring(xml)
    children = list(root)
    idx = next((i for i, c in enumerate(children) if c.get("id") == end_id or c.get("top-block-id") == end_id), None)
    if not idx:
        raise LedgerError("在“状态包”和“交接记录”之间找不到内容块")
    prev = children[idx - 1]
    if prev.get("top-block-id"):
        return prev.get("top-block-id")
    if prev.get("id"):
        return prev.get("id")
    items = [c for c in prev if c.get("id")]
    if prev.tag in ("ul", "ol") and items:
        return items[-1].get("id")
    raise LedgerError("“交接记录”前面那一块（<%s>）没有 block id；请在网页上替换状态包" % prev.tag)


def status_block(lg, item_id, path):
    url = lg.doc_url(item_id)
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if not content.lstrip().startswith("## 状态包"):
        content = "## 状态包\n\n" + content
    heads = heading_ids(lg, url)
    start = next((b for b, t in heads if t == "状态包"), None)
    end_head = next((b for b, t in heads if t == "交接记录"), None)
    if lg.mode == "offline":
        start, end = start or "<状态包标题的 block_id>", "<交接记录前一块的 block_id>"
    else:
        if not start or not end_head:
            raise LedgerError("主文档里找不到“状态包”或“交接记录”标题")
        rng = lg.lark("docs", "+fetch", "--doc", url, "--scope", "range", "--start-block-id", start,
                      "--end-block-id", end_head, "--detail", "with-ids")
        end = last_block_before(doc_content(rng), end_head) if rng is not None else "<交接记录前一块的 block_id>"
    lg.lark("docs", "+update", "--doc", url, "--command", "block_replace", "--start-block-id", start,
            "--end-block-id", end, "--doc-format", "markdown", "--content", "-", write=True, stdin=content)


def append(lg, item_id, path):
    with open(path, encoding="utf-8") as f:
        content = f.read()
    first = next((l for l in content.splitlines() if l.strip()), "")
    if not re.search(r"\[[A-Za-z]+\+[0-2]\]", first):
        raise LedgerError("交接记录第一行要带会话标签，比如 [Alg+1]")
    url = lg.doc_url(item_id)
    lg.lark("docs", "+update", "--doc", url, "--command", "append", "--doc-format", "markdown",
            "--content", "-", write=True, stdin=content)


def archive(lg, item_id):
    it = lg.item(item_id)
    url = lg.doc_url(item_id, it)
    seq_cn = lg.text_of(it, "sequence")
    seq = next((s for s, cn, _ in lg.spec.sequences() if cn == seq_cn), None)
    if it is not None and not seq:
        raise LedgerError("认不出 #%s 属于哪个序列（“序列”字段是 %s），没法决定归档到哪" % (item_id, seq_cn or "空的"))
    key = "archive/%s" % (seq or "<序列>")
    n = lg.nodes.get(key) if (lg.nodes.get(key) or {}).get("node_token") else None
    if not n and lg.mode != "offline":
        raise LedgerError("ids.json 里没有归档节点 %s；先跑 ensure-node %s" % (key, key))
    node = lg.node_of(url)
    if not node:
        raise LedgerError("#%s 的主文档不是知识库文档，没法移动" % item_id)
    lg.lark("wiki", "+move", "--node-token", node, "--target-parent-token", (n or {}).get("node_token") or "<%s 的节点>" % key,
            write=True)
    print("整棵子树移到 %s 下；移动的是原节点，文档链接不变。接着把工作项流转到“归档”。" % key)


def existing_node(lg, space, parent, key, title):
    """(node_token, title) of a node already in the space for this key: directly under parent
    (the space root when parent is None), titled with the node's name. None when there is
    none; offline it is not looked up."""
    args = ["wiki", "+node-list", "--space-id", space, "--page-all"]
    if parent:
        args += ["--parent-node-token", parent]
    data = lg.lark(*args)
    if data is None:
        return None
    nodes = (data.get("data") or {}).get("nodes") if isinstance(data, dict) and isinstance(data.get("data"), dict) else None
    if not isinstance(nodes, list):
        raise LedgerError("wiki +node-list 的输出读不懂，没法确认已有没有这个节点：%s" % json.dumps(data, ensure_ascii=False)[:300])

    def same(t):
        return (t or "").split("：")[0].strip() == title

    hits = [n for n in nodes if isinstance(n, dict) and n.get("node_token") and same(n.get("title"))]
    if len(hits) > 1:
        raise LedgerError("知识空间里已经有 %d 个“%s”节点（%s）；在 feishu/ids.json 的 wiki.nodes.%s 里写明用哪一个" % (
            len(hits), title, "、".join(n["node_token"] for n in hits), key))
    return (hits[0]["node_token"], hits[0].get("title")) if hits else None


def ensure_node(lg, key):
    if (lg.nodes.get(key) or {}).get("node_token"):
        print("节点 %s 已有：%s" % (key, lg.nodes[key].get("node_token")))
        return
    head, _, seq = key.partition("/")
    title = lg.spec.term("node", head) if not seq else lg.spec.term("seq", seq)
    space = (lg.ids.get("wiki") or {}).get("space_id")
    if not space and lg.mode != "offline":
        raise LedgerError("ids.json 里没有知识空间 space_id；先建知识空间，再跑 setup.py feishu --live --apply")
    parent = None
    if seq:
        parent = (lg.nodes.get(head) or {}).get("node_token")
        if not parent:
            raise LedgerError("ids.json 里的上一级节点 %s 没有 node_token；先跑 ensure-node %s" % (head, head))
    found = existing_node(lg, space, parent, key, title) if space else None
    if found:                                          # the space already has it: record it, do not create a second
        token, current = found
        record = {"title": current or title, "node_token": token, "obj_token": None}
        out = None
        print("知识空间里已经有节点 %s（%s），直接用，不再建。" % (key, token))
    else:
        args = ["wiki", "+node-create", "--space-id", space or "<space_id>", "--obj-type", "docx",
                "--node-type", "origin", "--title", title]
        if parent:
            args += ["--parent-node-token", parent]
        out = lg.lark(*args, write=True)
        record = None
    if lg.mode == "apply":
        if record is None:
            token = _find(out, ("node_token",))
            if not token:
                raise LedgerError("节点建了，但输出里没有 node_token：%s；去网页上核对，不要重跑" % json.dumps(out, ensure_ascii=False)[:300])
            record = {"title": title, "node_token": token, "obj_token": _find(out, ("obj_token",))}
        path = os.path.join(lg.root, "feishu", "ids.json")
        ids = load_json(path, {})
        ids.setdefault("wiki", {}).setdefault("nodes", {})[key] = record
        save_json(path, ids)
        lg.nodes[key] = record                         # a child created next in the same run finds it
        if out is not None:
            print("已建节点 %s：%s，写进 feishu/ids.json。" % (key, record["node_token"]))
        else:
            print("写进 feishu/ids.json。")


def ensure_all_nodes(lg):
    """The whole node skeleton: the top-level nodes first, then the enabled sequences' nodes
    under them. Nodes already recorded, or already in the space under the same title, are kept."""
    manifest = load_json(os.path.join(project_paths.runtime_dir(lg.root), "manifest.json"), {})
    for key in lg.spec.node_keys(manifest.get("sequences") or None):
        head, _, seq = key.partition("/")
        if seq and not (lg.nodes.get(head) or {}).get("node_token"):
            print("节点 %s 要等上一级 %s 建好：加 --apply 时先建上一级，再建它。" % (key, head))
            continue
        ensure_node(lg, key)


# ---------------------------------------------------------------- reads
def _not_in(states):
    return "`work_item_status` NOT IN (%s)" % ", ".join("'%s'" % s for s in states)


def mql_text(lg, name, sequence=None):
    """MQL for a named query. Every query leaves out the system state 已终止:
    views hide terminated items by default, MQL does not."""
    terminated = system_states(lg.spec)
    if name.startswith("source:"):
        src = item_id_of(name.split(":", 1)[1])
        if sequence:
            raise LedgerError("source:<ID> 查的是由某个工作项引出的所有工作项，不按序列筛；去掉 --sequence")
        return [_select(lg, w, "`%s` = '%s' AND %s" % (lg.field(w, "source_item")["field_key"], src, _not_in(terminated)))
                for w in ("same_seq", "cross_request", "review", "decision")]
    if name not in QUERIES:
        raise LedgerError("没有查询 %s；可选：%s、source:<ID>" % (name, "、".join(QUERIES)))
    wid, cond, not_in, _ = QUERIES[name]
    parts = [cond] if cond else []
    if not_in is not None:
        parts.append(_not_in(not_in + terminated))
    if sequence:
        parts.append("`%s` = '%s'" % (lg.field(wid, "sequence")["field_key"], lg.spec.term("seq", sequence)))
    return [_select(lg, wid, " AND ".join(parts))]


def _select(lg, wid, cond):
    return "SELECT `work_item_id`, `name`, `work_item_status` FROM `%s`.`%s` WHERE %s" % (lg.key, lg.type_ref(wid), cond)


def query_rows(data):
    """Rows of `workitem query --format json`: data.<group>[].moql_field_list[{key, value}].

    Anything else (an error text, a JSON object without data, other shapes)
    stops with LedgerError: an empty list would read as "nothing is waiting".
    """
    bad = LedgerError("workitem query 的输出读不懂，没法确定有没有结果：%s" % json.dumps(data, ensure_ascii=False)[:300])
    groups = data.get("data") if isinstance(data, dict) else None
    if not isinstance(groups, dict):
        raise bad
    rows = []
    for group in groups.values():
        if group is None:
            continue
        if not isinstance(group, list):
            raise bad
        for rec in group:
            fl = rec.get("moql_field_list") if isinstance(rec, dict) else None
            if not isinstance(fl, list) or not all(isinstance(f, dict) for f in fl):
                raise bad
            for f in fl:
                labels = f.get("value", {}).get("key_label_value_list") if isinstance(f.get("value"), dict) else None
                if labels is not None and not (isinstance(labels, list) and all(isinstance(x, dict) for x in labels)):
                    raise bad
            rows.append({f.get("key"): moql_value(f.get("value")) for f in fl})
    return rows


def past_the_end(data):
    """The recorded answer for a page after the last one: every item null, no error."""
    return isinstance(data, dict) and bool(data) and all(v is None for v in data.values())


def query_count(data):
    info = data.get("list")[0] if isinstance(data.get("list"), list) and data.get("list") else None
    count = info.get("count") if isinstance(info, dict) else None
    if isinstance(count, bool):
        return None
    if isinstance(count, int):
        return count
    if isinstance(count, float) and count.is_integer():
        return int(count)
    if isinstance(count, str) and count.isdigit():
        return int(count)
    return None


QUERY_PAGE, QUERY_MAX_PAGES = 50, 20


def query_all(lg, mql):
    """Every row of one MQL query. The first answer holds 50 rows at most, with the
    total in list[0].count; the rest are read page by page with its session_id."""
    data = lg.run(["meegle", "workitem", "query", "--project-key", lg.key, "--mql", mql, "--format", "json"], False)
    if data is None:
        return []
    rows = query_rows(data)
    count = query_count(data)
    if count is None and len(rows) < QUERY_PAGE or count is not None and count <= len(rows):
        return rows                                    # a short first page is complete
    info = data["list"][0] if isinstance(data.get("list"), list) and data["list"] else {}
    groups = info.get("group_infos") if isinstance(info, dict) else None
    group = next((g.get("group_id") for g in groups or [] if isinstance(g, dict) and g.get("group_id")), "1")
    session = data.get("session_id")
    last = min(-(-count // QUERY_PAGE), QUERY_MAX_PAGES) if count is not None else QUERY_MAX_PAGES
    page = 1
    while session and page < last:
        page += 1
        more = lg.run(["meegle", "workitem", "query", "--project-key", lg.key, "--session-id", str(session),
                       "--group-pagination-list", json.dumps([{"group_id": group, "page_num": page}]), "--format", "json"], False)
        if past_the_end(more):
            break
        try:
            got = query_rows(more)
        except LedgerError as e:
            raise LedgerError("第 %d 页（前面已读出 %d 项）：%s" % (page, len(rows), e))
        rows += got
        if count is None and len(got) < QUERY_PAGE:
            break
    if count is not None and len(rows) < count or count is None and (page >= QUERY_MAX_PAGES or not session):
        print("提醒：%s只读出 %d 项（每页 50 项，没翻完）；完整结果看网页上的视图。" % (
            "共 %d 项，" % count if count is not None else "", len(rows)))
    return rows


def query(lg, name, sequence=None, as_json=False):
    rows = []
    for mql in mql_text(lg, name, sequence):
        rows += query_all(lg, mql)
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            print("#%s  %s  %s" % (r.get("work_item_id"), r.get("work_item_status", ""), r.get("name", "")))
        if lg.mode != "offline":
            print("共 %d 项。" % len(rows))
    return rows


def digest(lg, item_id):
    """`ledger.py get`: type, state, name and the rule fields that have a value."""
    it = lg.item(item_id)
    if it is None:
        return None
    lg.people(it, item_id)
    lines = ["#%s  %s  %s  %s" % (item_id, lg.spec.term("wi", it["type"]) if it["type"] else "?", it["state"], it["name"])]
    if it["type"]:
        for fid in dict.fromkeys(f["id"] for f in lg.spec.fields_of(it["type"])):
            text = lg.text_of(it, fid)
            if text:
                lines.append("  %s（%s）：%s" % (fid, lg.spec.field_name(fid), text))
    return "\n".join(lines)


def trace(lg, item_id):
    seen = []
    cur = str(item_id)
    while cur and cur not in seen and len(seen) < 20:
        seen.append(cur)
        it = lg.item(cur)
        if it is None:
            print("#%s（离线，无法回溯）" % cur)
            return
        kind = lg.spec.term("wi", it["type"]) if it["type"] else "?"
        print("#%s  %s  %s  %s" % (cur, kind, it["state"], it["name"]))
        nxt = lg.text_of(it, "source_item")
        cur = nxt.strip() if nxt else None


def status(lg, item_id):
    """The 状态包 section, found by its heading in the outline. The router matches
    messages without a work item ID against it, the item's name and its fields."""
    url = lg.doc_url(item_id)
    heads = heading_ids(lg, url)
    start = next((b for b, t in heads if t == "状态包"), None)
    if lg.mode == "offline":
        start = start or "<状态包标题的 block_id>"
    if not start:
        raise LedgerError("主文档里找不到“状态包”标题")
    data = lg.lark("docs", "+fetch", "--doc", url, "--doc-format", "markdown", "--scope", "section",
                   "--start-block-id", start)
    if data is not None:
        print(doc_content(data) or json.dumps(data, ensure_ascii=False, indent=2))


# ----------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="读写智能体组织的台账和任务包文档")
    ap.add_argument("--offline", action="store_true", help="不调用飞书，只打印命令")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("get"); p.add_argument("id"); p.add_argument("--raw", action="store_true")
    p = sub.add_parser("query"); p.add_argument("name"); p.add_argument("--sequence"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("mql"); p.add_argument("name"); p.add_argument("--sequence")
    p = sub.add_parser("trace"); p.add_argument("id")
    p = sub.add_parser("status"); p.add_argument("id")
    for name in ("register", "reply"):
        p = sub.add_parser(name); p.add_argument("draft"); p.add_argument("--apply", action="store_true")
        p.add_argument("--doc", help="上次建文档没有确定结果时：已建好的文档链接，或 new 重新建")
        if name == "register":
            p.add_argument("--source")
            p.add_argument("--set", action="append")
            p.add_argument("--resume", action="store_true")
            p.add_argument("--existing", action="store_true", help="立项单：挂到 ids.json 里已有的项目上")
    p = sub.add_parser("transition"); p.add_argument("id"); p.add_argument("state")
    p.add_argument("--set", action="append"); p.add_argument("--type"); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("update"); p.add_argument("id"); p.add_argument("--set", action="append", required=True)
    p.add_argument("--type"); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("comment"); p.add_argument("id"); p.add_argument("--text", required=True); p.add_argument("--apply", action="store_true")
    for name in ("status-block", "append"):
        p = sub.add_parser(name); p.add_argument("id"); p.add_argument("--file", required=True); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("archive"); p.add_argument("id"); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("ensure-node"); p.add_argument("key"); p.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    mode = "offline" if args.offline else ("apply" if getattr(args, "apply", False) else "preview")
    lg = Ledger(project_root(), mode)
    m = lg.ids.get("meegle") or {}
    if mode != "offline" and not (m.get("bound_at") and m.get("types")):  # a binding time with no type is no binding
        print("出错：这个项目还没绑定飞书空间。先跑 setup.py feishu --target <项目目录> --live 看匹配结果，核对后加 --apply；"
              "绑定以前台账脚本不读也不写飞书。")
        return 1
    moved = other_space(load_json(os.path.join(lg.root, "spec", "task.json"), {}), m)
    if mode != "offline" and moved:
        print("出错：spec/task.json 里的飞书项目空间是“%s”，绑定的是“%s”。重跑 setup.py feishu --target <项目目录> --live 核对，"
              "再加 --apply；在这以前台账脚本不读也不写飞书。" % (moved, m["space_name"]))
        return 1
    lock = write_lock(lg.root) if mode == "apply" else contextlib.nullcontext()
    try:
        with lock:
            if mode == "apply" and os.path.isfile(os.path.join(lg.root, ".agent-org", "manifest.json")):
                import codex_runtime
                try:
                    codex_runtime.assert_writer(lg.root, args.cmd)
                except codex_runtime.RuntimeError as e:
                    raise LedgerError(str(e))
            run_command(lg, args)
    except (LedgerError, orgspec.OrgSpecError, ET.ParseError, OSError) as e:
        print("出错：%s" % e)
        return 1
    if mode == "preview" and lg.planned:
        print("这是预览，没有写飞书。核对后加 --apply 执行。")
    return 0


def run_command(lg, args):
    if args.cmd == "get":
        if args.raw:
            print(json.dumps(lg.get(args.id, ["_all"]), ensure_ascii=False, indent=2))
        else:
            print(digest(lg, args.id) or "")
    elif args.cmd == "query":
        query(lg, args.name, args.sequence, args.json)
    elif args.cmd == "mql":
        print("\n".join(mql_text(lg, args.name, args.sequence)))
    elif args.cmd == "trace":
        trace(lg, args.id)
    elif args.cmd == "status":
        status(lg, args.id)
    elif args.cmd == "register":
        register(lg, args.draft, args.source, pairs=args.set, resume=args.resume, existing=args.existing, doc=args.doc)
    elif args.cmd == "reply":
        reply(lg, args.draft, args.doc)
    elif args.cmd == "transition":
        transition(lg, args.id, args.state, args.set, args.type)
    elif args.cmd == "update":
        update(lg, args.id, args.set, args.type)
    elif args.cmd == "comment":
        comment(lg, args.id, args.text)
    elif args.cmd == "status-block":
        status_block(lg, args.id, args.file)
    elif args.cmd == "append":
        append(lg, args.id, args.file)
    elif args.cmd == "archive":
        archive(lg, args.id)
    elif args.cmd == "ensure-node":
        if args.key == "all":
            ensure_all_nodes(lg)
        else:
            ensure_node(lg, args.key)


if __name__ == "__main__":
    sys.exit(main())
