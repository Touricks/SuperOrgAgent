#!/usr/bin/env python3
"""Session pool state for a project initialised in pool mode.

In pool mode every org role runs as a desktop session the researcher can see and talk
to. A session becomes a role by claiming it, and a claim needs a permit:
- a pre-registration (route.py expect) by the session that opened it, keyed by the new
  session's desktop ID (or, in a terminal, its session ID); or
- the opening line the researcher pastes into a new top-level session,
  "认领 <角色>（会话名 <名字>）…", for +2 roles only.
Hooks turn a permit into a claim; route.py only writes permits and pending entries.

A claim belongs to one session ID. Its desktop ID moves it to a new session ID only after
/clear: the old session's SessionEnd(clear) and the new session's SessionStart(clear) with
the same desktop ID (or, without it, a pairing by time). A resume keeps the session ID, so
it needs no move. Any other process that shows the same desktop ID, such as a claude
started (or resumed) from that session's shell, is a desk conflict: it holds no role and
no write right.

setup.py copies this file to .claude/hooks/ (for the hooks) and .claude/agent-org/bin/
(for route.py and ledger.py).

    .claude/state/pool.json            claims, permits, pending, tombstones, work item
                                       owners, refill requests, focus flags, re-attach notes
    .claude/state/sessions/<id>.json   one session's status, written by its own hooks
    .claude/logs/dispatch.jsonl        messages seen, claims, releases, refills

Writers lock .claude/state/pool.lock and replace pool.json atomically; readers do not
lock. None of this is a security boundary: a shell can still write the files. The guard
hook denies edits to .claude/state/ and checks the claims again before each ledger write.
Python 3.9 standard library only.
"""
import contextlib
import datetime
import fcntl
import glob
import json
import os
import re

OPENING_RE = re.compile(r"^\s*认领\s*([a-z]+-p2)\s*[（(]\s*会话名\s*[：:]?\s*([A-Za-z]+-p2-\d+)\s*[)）]")
NAME_RE = re.compile(r"^([A-Za-z]+)-p([0-2])-(\d+)$")
NAME_IN_TEXT = r"[A-Za-z]+-[pP][0-2]-\d+"
HEADER_LINES = 5                      # a message header starts one of the first non-empty lines
REATTACH_WINDOW = datetime.timedelta(seconds=60)
STALE_BUSY = datetime.timedelta(minutes=15)
FOCUS_IDLE = datetime.timedelta(minutes=30)
OFFLINE_LONG = datetime.timedelta(hours=24)
INJECT_LIMIT = 9500
CONTEXT_LIMIT = 9500
ITEMS_SHOWN = 20
BYPASS_MODES = ("bypassPermissions", "dontAsk")
DESK_ENV = "CLAUDE_CODE_HOST_SESSION_ID"
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
LIST_KEYS = ("permits", "pending", "tombstones", "refills", "reattach")
DICT_KEYS = ("owners", "focus")


class PoolError(Exception):
    pass


# ----------------------------------------------------------------- time
def now():
    return datetime.datetime.now().astimezone()


def stamp(at=None):
    return (at or now()).isoformat(timespec="seconds")


def parse_time(text):
    try:
        return datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- files
def state_dir(root):
    return os.path.join(root, ".claude", "state")


def pool_path(root):
    return os.path.join(state_dir(root), "pool.json")


def empty():
    return {"version": 1, "claims": {}, "permits": [], "pending": [], "tombstones": [], "owners": {},
            "refills": [], "focus": {}, "reattach": []}


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


ENTRY_FIELDS = ("name", "role", "session_id", "desk")


def _entry_ok(x):
    return isinstance(x, dict) and all(isinstance(x.get(k), (str, type(None))) for k in ENTRY_FIELDS)


def well_formed(data):
    """pool.json has the shape this module writes: anything else counts as unreadable."""
    if not isinstance(data, dict):
        return False
    claims = data.get("claims", {})
    if not isinstance(claims, dict) or not all(_entry_ok(c) for c in claims.values()):
        return False
    for key in LIST_KEYS:
        v = data.get(key, [])
        if not isinstance(v, list) or not all(_entry_ok(x) for x in v):
            return False
    if not all(isinstance(r.get("count", 1), int) for r in data.get("refills", [])):
        return False
    for key in DICT_KEYS:
        if not isinstance(data.get(key, {}), dict):
            return False
    return all(isinstance(v, str) for v in data.get("owners", {}).values()) and \
        all(isinstance(v, dict) for v in data.get("focus", {}).values())


def load(root):
    """(pool, ok). A missing file is an empty pool; an unreadable or misshapen one gives ok=False."""
    path = pool_path(root)
    if not os.path.isfile(path):
        return empty(), True
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return empty(), False
    if not well_formed(data):
        return empty(), False
    for key, value in empty().items():
        data.setdefault(key, value)
    return data, True


@contextlib.contextmanager
def locked(root):
    """Load pool.json under an exclusive lock; save it if the block succeeds."""
    os.makedirs(state_dir(root), exist_ok=True)
    with open(os.path.join(state_dir(root), "pool.lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data, ok = load(root)
            if not ok:
                raise PoolError("读不出 .claude/state/pool.json；请研究员先看这个文件，不要让会话去改它")
            yield data
            save_json(pool_path(root), data)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def log_event(root, rec):
    try:
        path = os.path.join(root, ".claude", "logs", "dispatch.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rec = dict(rec)
        rec.setdefault("ts", stamp())
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_events(root):
    out = []
    path = os.path.join(root, ".claude", "logs", "dispatch.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out


def manifest_version(root):
    try:
        with open(os.path.join(root, ".claude", "agent-org", "manifest.json"), encoding="utf-8") as f:
            return json.load(f).get("plugin_version")
    except (OSError, ValueError, AttributeError):
        return None


# ------------------------------------------------------- session status
def session_file(root, sid):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", sid or "")[:100] or "_"
    return os.path.join(state_dir(root), "sessions", safe + ".json")


def load_session(root, sid):
    try:
        with open(session_file(root, sid), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update_session(root, sid, change):
    """Apply change(state) to this session's own status file under a lock; return the new state."""
    if not sid:
        return {}
    path = session_file(root, sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = load_session(root, sid)
            state.setdefault("session_id", sid)
            change(state)
            state["updated_at"] = stamp()
            save_json(path, state)
            return state
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


WAIT_KEYS = ("waiting_tool_use_id", "waiting_self", "waiting_agent_id", "status_before_wait")


def set_status(root, sid, status, **fields):
    def change(state):
        if not status.startswith("waiting"):
            for k in WAIT_KEYS:
                state.pop(k, None)
        state["status"] = status
        state["status_at"] = stamp()
        state.update({k: v for k, v in fields.items() if v is not None})
    return update_session(root, sid, change)


def end_wait(st, agent_id=None):
    """A tool call went on, so a prompt it was waiting on was answered. A subagent's call ends only a
    wait that is not the session's own prompt (waiting_self) and not another subagent's
    (waiting_agent_id); the session's own call ends any wait but a subagent's. Returns True when
    the wait ended."""
    if not str(st.get("status", "")).startswith("waiting"):
        return False
    owner = st.get("waiting_agent_id")
    if agent_id:
        if st.get("waiting_self") or (owner and owner != agent_id):
            return False
        st["status"] = st.get("status_before_wait") or "busy"
    else:
        if owner and not st.get("waiting_self"):
            return False
        st["status"] = "busy"
    st["status_at"] = stamp()
    for k in WAIT_KEYS:
        st.pop(k, None)
    return True


def mark_claimed(root, sid, claim):
    """Record in the session's own file which claim it holds and under which plugin version."""
    version = manifest_version(root)
    try:
        update_session(root, sid, lambda st: st.update({"name": claim.get("name"), "plugin_version": version}))
    except OSError:
        pass


def all_sessions(root):
    out = {}
    for path in glob.glob(os.path.join(state_dir(root), "sessions", "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("session_id"):
            out[data["session_id"]] = data
    return out


def mode_class(mode):
    """The desktop holds a message back when sender and receiver are in different classes."""
    if not mode:
        return None
    return "bypass" if mode == "bypassPermissions" else "other"


# ---------------------------------------------------------------- names
def name_map(policy):
    return {(pol["abbrev"].lower(), pol["rank"]): role for role, pol in policy["agents"].items()}


def canonical(policy, name):
    """Alg-p1-01 for alg-p1-01 or ALG-P1-01; None when the name names no role."""
    m = re.match(r"^([A-Za-z]+)-[pP]([0-2])-(\d+)$", (name or "").strip())
    if not m:
        return None
    role = name_map(policy).get((m.group(1).lower(), int(m.group(2))))
    if not role:
        return None
    return "%s-p%s-%s" % (policy["agents"][role]["abbrev"], m.group(2), m.group(3))


def role_of(policy, name):
    c = canonical(policy, name)
    if not c:
        return None
    m = NAME_RE.match(c)
    return name_map(policy).get((m.group(1).lower(), int(m.group(2))))


def is_slot(name):
    """+1 and +2 names are pool slots; a +0 name is a slot with two digits (Alg-p0-01)
    and a work name with three or more (Alg-p0-2101, after the work item ID)."""
    m = NAME_RE.match(name or "")
    return bool(m) and (m.group(2) != "0" or len(m.group(3)) == 2)


def work_item_of(name):
    """The work item ID in a +0 work name (Alg-p0-2101 → 2101); None for any other name."""
    m = NAME_RE.match(name or "")
    if m and m.group(2) == "0" and len(m.group(3)) >= 3:
        return m.group(3)
    return None


def slot_name(pol, n):
    return "%s-p%d-%02d" % (pol["abbrev"], pol["rank"], n)


def short_name(name):
    return (name or "").lower()


def resolve(policy, pool, name, item=None):
    """The claim a name in a header or a ledger field stands for: the claim itself, or for a +0
    work name (Alg-p0-2101) the claim that owns that work item. Other names come back canonical."""
    c = canonical(policy, name)
    if not c:
        return name or None
    if c in pool["claims"]:
        return c
    wid = work_item_of(c)
    owner = (pool.get("owners") or {}).get(wid) if wid else None
    if owner and owner in pool["claims"]:
        return owner
    return c


# --------------------------------------------------------------- claims
def claims_of(pool, role=None):
    return [c for c in pool["claims"].values() if role is None or c.get("role") == role]


def claim_by_session(pool, sid):
    if not sid:
        return None
    return next((c for c in pool["claims"].values() if c.get("session_id") == sid), None)


def claim_by_desk(pool, desk):
    if not desk:
        return None
    return next((c for c in pool["claims"].values() if c.get("desk") == desk), None)


def permit_for(pool, sid, desk):
    return next((p for p in pool["permits"]
                 if (sid and p.get("session_id") == sid) or (desk and p.get("desk") == desk)), None)


def pending_for(pool, sid):
    return [p for p in pool["pending"] if sid and p.get("session_id") == sid]


def tombstoned(pool, sid, desk):
    return any((sid and t.get("session_id") == sid) or (desk and t.get("desk") == desk)
               for t in pool["tombstones"])


def single_roles(policy):
    return policy.get("single_claim_roles") or [policy.get("entry_role", "pmo-p2"), policy.get("scribe_role", "pmo-scribe")]


def role_limit(policy, role):
    pol = policy["agents"].get(role) or {}
    return 1 if role in single_roles(policy) else int(pol.get("limit") or 1)


def fixed_name(policy, role):
    return (policy["agents"].get(role) or {}).get("fixed_session")


def cleared(root, claim):
    """The claim's recorded session ended with /clear (SessionEnd reason clear, not resumed since)."""
    st = load_session(root, claim.get("session_id") or "")
    return st.get("status") == "offline" and st.get("ended_reason") == "clear"


def consume_refill(pool, role):
    for r in pool["refills"]:
        if r.get("role") == role:
            r["count"] = int(r.get("count") or 1) - 1
            if r["count"] <= 0:
                pool["refills"].remove(r)
            return


def promote(pool, policy, permit, sid, desk, at=None):
    """Turn permit into a claim for session sid. Returns (claim, note); claim is None when refused."""
    role = permit.get("role")
    pol = policy["agents"].get(role)
    if not pol:
        return None, "没有角色 %s" % role
    held = claims_of(pool, role)
    if len(held) >= role_limit(policy, role):
        return None, "%s 已有 %d 条认领（%s），上限 %d；要换会话，研究员先 release 旧的" % (
            role, len(held), "、".join(c["name"] for c in held), role_limit(policy, role))
    name, note = permit.get("name"), None
    fixed = pol.get("fixed_session")
    if fixed:
        if name != fixed:
            note = "%s 的会话名固定是 %s" % (role, fixed)
        name = fixed
        if name in pool["claims"]:
            return None, "会话名 %s 已被占用" % name
    elif not name or name in pool["claims"]:
        n = 1
        while slot_name(pol, n) in pool["claims"]:
            n += 1
        note = "会话名 %s 已被占用，改用 %s" % (name, slot_name(pol, n))
        name = slot_name(pol, n)
    claim = {"name": name, "short": short_name(name), "role": role, "session_id": sid,
             "desk": desk or permit.get("desk"), "claimed_at": stamp(at), "via": permit.get("via"),
             "by": permit.get("by")}
    pool["claims"][name] = claim
    if permit in pool["permits"]:
        pool["permits"].remove(permit)
    pool["pending"] = [p for p in pool["pending"] if p.get("session_id") != sid]
    pool["tombstones"] = [t for t in pool["tombstones"]
                          if not ((sid and t.get("session_id") == sid) or (claim["desk"] and t.get("desk") == claim["desk"]))]
    consume_refill(pool, role)
    return claim, note


def promote_session(root, policy, sid, desk, how):
    """Promote a permit waiting for this session, under the lock. A permit that cannot be
    promoted is dropped, so it is not retried on every hook call. Returns the claim or None."""
    permit = None
    try:
        with locked(root) as pool:
            claim = claim_by_session(pool, sid)
            if claim:
                return claim
            permit = permit_for(pool, sid, desk)
            if not permit:
                return None
            claim, note = promote(pool, policy, permit, sid, desk)
            if claim is None and permit in pool["permits"]:
                pool["permits"].remove(permit)
    except (PoolError, OSError):
        return None
    log_event(root, {"event": "claim" if claim else "claim_refused", "how": how, "session_id": sid,
                     "desk": desk or None, "name": (claim or permit).get("name"),
                     "role": (claim or permit).get("role"), "note": note})
    if claim:
        mark_claimed(root, sid, claim)
    return claim


def reattach(root, name, sid, desk=None, how="desk"):
    """A claim moves to a new session ID: its desktop ID after /clear or a resume, or a /clear pairing."""
    old = None
    try:
        with locked(root) as pool:
            claim = pool["claims"].get(name)
            if not claim:
                return None
            old = claim.get("session_id")
            claim["session_id"] = sid
            if desk:
                claim["desk"] = desk
            claim["reattached_at"] = stamp()
            pool["reattach"] = [r for r in pool["reattach"] if r.get("name") != name]
    except (PoolError, OSError):
        return None
    log_event(root, {"event": "reattach", "how": how, "name": name, "old_session_id": old, "session_id": sid})
    mark_claimed(root, sid, claim)
    return claim


def no_write(pol):
    p = dict(pol)
    p.update(lark_write=[], meegle_write=[], ledger_write=False)
    return p


class Identity:
    """Who a hook call comes from.

    origin: subagent | entry | agent_flag | claim | permit | pending | tombstone | desk_conflict |
            pool_unreadable | researcher.
    researcher: the researcher drives this session (CLI main or focus session, or a pool
    session that holds no claim, permit or tombstone): hard rules ask instead of deny.
    """

    def __init__(self, role, pol, origin, name=None, researcher=False, claim=None, conflict=None):
        self.role, self.pol, self.origin, self.name = role, pol, origin, name
        self.researcher, self.claim, self.conflict = researcher, claim, conflict

    @property
    def label(self):
        return self.name or self.role


def scribe_ok(pool, policy, claim):
    """The claim is the one unreleased scribe claim: the only session that writes the ledger."""
    scribe = policy.get("scribe_role", "pmo-scribe")
    held = claims_of(pool, scribe)
    return claim.get("role") == scribe and len(held) == 1 and held[0].get("name") == claim.get("name")


def claim_identity(policy, pool, claim):
    role = claim.get("role")
    pol = policy["agents"].get(role)
    if not pol:
        return Identity(role or "unknown", policy["default_agent"], "claim", claim.get("name"), False, claim)
    if role == policy.get("scribe_role", "pmo-scribe") and not scribe_ok(pool, policy, claim):
        pol = no_write(pol)
    return Identity(role, pol, "claim", claim.get("name"), False, claim)


def may_reattach(root, claim, data):
    """A claim found only by desktop ID moves to this session only at this session's
    SessionStart after /clear, when the claim's own session ended with /clear."""
    return (data.get("hook_event_name") == "SessionStart" and data.get("source") == "clear"
            and cleared(root, claim))


def identify(data, policy, root, env=None, promote_permits=True):
    env = os.environ if env is None else env
    agents = policy["agents"]
    agent = data.get("agent_type") or ""
    sid = data.get("session_id") or ""
    desk = env.get(DESK_ENV) or ""
    pool_mode = policy.get("mode") == "pool"
    if data.get("agent_id"):
        pol = agents.get(agent, policy["default_agent"])
        return Identity(agent if agent in agents else (agent or "unknown"),
                        no_write(pol) if pool_mode else pol, "subagent")
    if not pool_mode:
        if agent in agents:
            return Identity(agent, agents[agent], "entry" if agent == policy["main_agent"] else "agent_flag",
                            researcher=True)
        main = policy["main_agent"]
        return Identity(main, agents[main], "entry", researcher=True)
    if agent in agents:                                  # claude --agent <role> in a terminal
        return Identity(agent, no_write(agents[agent]), "agent_flag")
    pool, ok = load(root)
    if not ok:
        return Identity("unknown", policy["default_agent"], "pool_unreadable")
    claim = claim_by_session(pool, sid)
    if claim is None:
        other = claim_by_desk(pool, desk)
        if other is not None:
            if not (promote_permits and may_reattach(root, other, data)):
                return Identity("desk_conflict", policy["default_agent"], "desk_conflict", conflict=other.get("name"))
            claim = reattach(root, other["name"], sid, desk) or other
            pool, _ = load(root)
    if claim is None:
        permit = permit_for(pool, sid, desk)
        if permit and promote_permits:
            claim = promote_session(root, policy, sid, desk, "hook")
            pool, _ = load(root)
        if claim is None and permit and permit in pool["permits"]:
            return Identity(permit.get("role"), no_write(agents.get(permit.get("role"), policy["default_agent"])),
                            "permit", permit.get("name"))
    if claim:
        return claim_identity(policy, pool, claim)
    pend = pending_for(pool, sid)
    if pend:
        return Identity(pend[0].get("role"), no_write(agents.get(pend[0].get("role"), policy["default_agent"])),
                        "pending", pend[0].get("name"))
    if tombstoned(pool, sid, desk):
        return Identity("released", policy["default_agent"], "tombstone")
    return Identity("researcher", policy["default_agent"], "researcher", researcher=True)


# --------------------------------------------------------------- opening
def opening_permit(root, policy, sid, desk, prompt):
    """The researcher's opening line in a new top-level session: a permit for a +2 role."""
    m = OPENING_RE.match(prompt or "")
    if not m:
        return None, None
    role, name = m.group(1), canonical(policy, m.group(2))
    pol = policy["agents"].get(role)
    if not pol or pol["rank"] != 2 or role_of(policy, name) != role:
        return None, "开局句里的角色 %s 和会话名 %s 对不上，或者不是 +2；只有 +2 能用开局句认领" % (role, m.group(2))
    if pol.get("fixed_session") and name != pol["fixed_session"]:
        return None, "%s 的会话名固定是 %s，开局句写错了；改成“认领 %s（会话名 %s）”再开一个会话" % (
            role, pol["fixed_session"], role, pol["fixed_session"])
    try:
        with locked(root) as pool:
            if claim_by_session(pool, sid):
                return None, None
            permit = {"name": name, "role": role, "session_id": sid, "desk": desk or None,
                      "by": "researcher", "via": "opening", "at": stamp()}
            pool["permits"] = [p for p in pool["permits"] if p.get("session_id") != sid]
            pool["permits"].append(permit)
            claim, note = promote(pool, policy, permit, sid, desk)
            if claim is None:
                pool["permits"].remove(permit)
    except (PoolError, OSError) as e:
        return None, str(e)
    log_event(root, {"event": "claim" if claim else "claim_refused", "how": "opening", "session_id": sid,
                     "desk": desk or None, "name": name, "role": role, "note": note})
    if claim:
        mark_claimed(root, sid, claim)
    return claim, note


# -------------------------------------------------------------- messages
def header_re(policy):
    names = sorted((policy.get("msg") or {}).values(), key=len, reverse=True)
    return re.compile(r"^\s*(?:\*\*)?(?:[（(【]重发[^）)】]*[）)】]\s*)?【(%s)(?:\s*#(\d{3,}))?[^】]*】\s*(?:(%s)\s*(?:→|->)\s*(%s))?" % (
        "|".join(re.escape(n) for n in names), NAME_IN_TEXT, NAME_IN_TEXT))


def parse_message(policy, text):
    """The message header that starts one of the first non-empty lines: kind, work item ID, sender,
    receiver. A header quoted inside a sentence is not a message. None if there is none."""
    if not text or not policy.get("msg"):
        return None
    kinds = {v: k for k, v in policy["msg"].items()}
    rx = header_re(policy)
    lines = [ln for ln in text.splitlines() if ln.strip()][:HEADER_LINES]
    for line in lines:
        m = rx.match(line)
        if not m:
            continue
        p0 = re.search(r"会话名[：:]\s*(%s)" % NAME_IN_TEXT, text)
        return {"kind": kinds[m.group(1)], "id": m.group(2),
                "from": canonical(policy, m.group(3)) or m.group(3), "to": canonical(policy, m.group(4)) or m.group(4),
                "p0_name": canonical(policy, p0.group(1)) if p0 else None, "line": line.strip()[:200]}
    return None


def addressed_to(policy, pool, msg, me):
    """Is this message addressed to the claim me? A +0 work name of my own role counts as me."""
    to = msg.get("to")
    if not to:
        return True
    if to == me["name"]:
        return True
    return bool(work_item_of(to)) and role_of(policy, to) == me.get("role")


def _matches(policy, spec, role):
    pol = policy["agents"].get(role)
    return bool(pol) and (spec == role or spec == "p%d" % pol["rank"])


def edge_ok(policy, edge, a, b):
    if not (_matches(policy, edge["from"], a) and _matches(policy, edge["to"], b)):
        return False
    sa, sb = policy["agents"][a]["sequence"], policy["agents"][b]["sequence"]
    scope = edge.get("scope")
    if scope == "same_sequence":
        return sa == sb
    if scope == "other_sequences":
        return sa != sb
    return True


def kind_allowed(policy, kind, sender, receiver):
    """Does the dispatch graph let sender send this kind of message to receiver (role names)?"""
    d = policy.get("dispatch") or {}
    rule = (d.get("kinds") or {}).get(kind)
    if rule is None or sender not in policy["agents"] or receiver not in policy["agents"]:
        return False
    if rule.get("along"):
        a, b = (receiver, sender) if rule.get("reverse") else (sender, receiver)
        return any(edge_ok(policy, e, a, b) for e in d.get(rule["along"], []))
    if rule.get("to") and receiver != rule["to"]:
        return False
    if rule.get("from") and sender != rule["from"]:
        return False
    return True


def receipt_exists(root, item_id):
    return bool(item_id) and bool(glob.glob(os.path.join(root, "packages", "sent", "%s.json" % item_id))
                                  or glob.glob(os.path.join(root, "packages", "sent", "%s-*.json" % item_id)))


def receive_checks(policy, root, pool, msg, me):
    """Reminders for a message this claim received; also the norms it breaks, for the log.
    msg["from"] is already resolved to a claim name where possible."""
    out = []
    names = policy["msg"]
    kind, item, sender = msg["kind"], msg.get("id"), msg.get("from")
    sender_claim = pool["claims"].get(sender) if sender else None
    if sender and not sender_claim:
        out.append("发出方 %s 不在会话池的认领表里；先用 route.py pool 核对是谁发的" % sender)
    if not addressed_to(policy, pool, msg, me):
        out.append("抬头写的接收方是 %s，不是本会话 %s；确认是不是发错了" % (msg["to"], me["name"]))
    if sender_claim and not kind_allowed(policy, kind, sender_claim["role"], me["role"]):
        out.append("派发图里 %s（%s）不给 %s（%s）发【%s】；照规范该走入口或本序列上一级，接不接先和发出方确认" % (
            sender, sender_claim["role"], me["name"], me["role"], names.get(kind, kind)))
    routing = sender_claim and sender_claim["role"] == policy.get("entry_role") and me["role"] == policy.get("router_role")
    if kind == "assign" and item and not receipt_exists(root, item):
        out.append("#%s 在 packages/sent/ 里没有登记回执；派活要先登记，先跟派活方确认" % item)
    if kind == "assign" and not item and not routing:
        out.append("派活消息没写工作项 ID；先跟派活方确认是否已登记")
    if kind == "assign" and (policy["agents"].get(me["role"]) or {}).get("rank") == 0 and not msg.get("p0_name"):
        out.append("派 +0 的消息要单独写一行“会话名：<简写>-p0-<工作项 ID>”；先按需求包里的工作项 ID 定会话名")
    if kind == "report" and item:
        owner = (pool.get("owners") or {}).get(item)
        if owner and sender and owner != sender:
            out.append("#%s 的负责会话是 %s，这条交回是 %s 发的；可能是重派后旧会话迟到的交回，先核对再用" % (item, owner, sender))
    return out


# ------------------------------------------------------------ open items
def all_open_items(root, owners=None):
    """{name: ({#ID: receiver} assigned and not reported back, {#ID: sender} still owed)} from the
    messages the receivers' hooks saw. One pass over dispatch.jsonl."""
    owners = owners or {}
    sent, owed = {}, {}
    for rec in read_events(root):
        if rec.get("event") != "message" or not rec.get("id"):
            continue
        kind, item = rec.get("kind"), rec["id"]
        frm, to = rec.get("from"), rec.get("seen_by") or rec.get("to")
        if not frm or not to:
            continue
        if kind == "assign":
            prev = sent.get(frm, {}).get(item)
            if prev and prev != to and owed.get(prev, {}).get(item) == frm:
                owed[prev].pop(item)                 # re-dispatched: the old receiver owes nothing
            sent.setdefault(frm, {})[item] = to
            owed.setdefault(to, {})[item] = frm
        elif kind == "report":
            mine = sent.get(to, {})
            if item in mine and (mine[item] == frm or owners.get(item) == frm):
                mine.pop(item)
            theirs = owed.get(frm, {})
            if theirs.get(item) == to:
                theirs.pop(item)
    names = set(sent) | set(owed)
    return {n: (sent.get(n, {}), owed.get(n, {})) for n in names}


def open_items(root, name, owners=None):
    """([(#ID, receiver)] assigned by name and not reported back, [(#ID, sender)] name still owes)."""
    sent, owed = all_open_items(root, owners).get(name, ({}, {}))
    return sorted(sent.items()), sorted(owed.items())


def role_body(root, role):
    try:
        with open(os.path.join(root, ".claude", "agents", role + ".md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            text = text[end + 5:]
    return text.strip()


def describe(policy, claim):
    pol = policy["agents"].get(claim.get("role")) or {}
    return "%s（%s +%s，角色 %s，标签 %s）" % (claim.get("name"), pol.get("sequence_cn") or pol.get("sequence", ""),
                                       pol.get("rank", "?"), claim.get("role"), pol.get("tag", ""))


def items_text(pairs, fmt, more):
    shown = [fmt % p for p in pairs[:ITEMS_SHOWN]]
    if len(pairs) > ITEMS_SHOWN:
        shown.append(more % (len(pairs) - ITEMS_SHOWN))
    return "；".join(shown)


def injection(root, policy, claim, reason="", limit=INJECT_LIMIT, owners=None):
    """Role text injected after a claim, a resume, a compaction or a /clear: the role definition
    without frontmatter, the open items and a pointer, at most limit characters."""
    name, role = claim.get("name"), claim.get("role")
    sent, owed = open_items(root, name, owners)
    items = []
    if sent:
        items.append("派出去还没交回：" + items_text(sent, "#%s→%s", "另有 %d 条，用 route.py pool --recover 看全"))
    if owed:
        items.append("还欠交回：" + items_text(owed, "#%s（交回给 %s）", "另有 %d 条"))
    head = "%s本会话是 %s。下面的角色说明就是本会话的工作说明。" % (reason, describe(policy, claim))
    tail = "未了事项：%s。台账和状态包是正本：开工前用 ledger.py get 读工作项，找人用 route.py pool。完整说明在 .claude/agents/%s.md。" % (
        "；".join(items) or "无", role)
    body = role_body(root, role)
    room = limit - len(head) - len(tail) - 4
    if len(body) > room:
        cut = "……（下文略，见 .claude/agents/%s.md）" % role
        body = body[:max(room - len(cut), 0)] + cut
    text = "\n\n".join(x for x in (head, body, tail) if x)
    return text[:limit]


def reminder_line(policy, claim):
    pol = policy["agents"].get(claim.get("role")) or {}
    return "你是 %s（%s +%s），标签 %s。派活、交回、登记都发消息，抬头照 CLAUDE.md 的格式；找人先跑 route.py pool。" % (
        claim.get("name"), pol.get("sequence_cn") or pol.get("sequence", ""), pol.get("rank", "?"), pol.get("tag", ""))


# ------------------------------------------------------------------ view
def last_activity(st):
    times = [t for t in (parse_time(st.get("last_tool_at")), parse_time(st.get("status_at"))) if t]
    return max(times) if times else None


def focus_expired(st, at=None):
    """A focus flag lapses once its session has been idle for 30 minutes."""
    at = at or now()
    since = parse_time(st.get("status_at"))
    return st.get("status") in ("idle", "idle_bg", "offline") and bool(since) and at - since > FOCUS_IDLE


def session_view(root, policy, pool, sessions=None, at=None):
    """One row per claim: name, role, status (with uncertain for a busy session gone quiet), work
    items, last activity, desktop ID, permission mode, focus flag, quota pause, status before the end."""
    at = at or now()
    sessions = all_sessions(root) if sessions is None else sessions
    owners = {}
    for item, owner in (pool.get("owners") or {}).items():
        owners.setdefault(owner, []).append(item)
    rows = []
    for name, claim in sorted(pool["claims"].items()):
        st = sessions.get(claim.get("session_id")) or {}
        status = st.get("status") or "unknown"
        last = last_activity(st)
        if status == "busy" and last and at - last > STALE_BUSY:
            status = "uncertain"
        focus = (pool.get("focus") or {}).get(name)
        focus_left = None
        if focus:
            if focus_expired(st, at):
                focus = None
            elif st.get("status") in ("idle", "idle_bg", "offline") and parse_time(st.get("status_at")):
                focus_left = int((FOCUS_IDLE - (at - parse_time(st["status_at"]))).total_seconds() // 60)
        rows.append({"name": name, "short": claim.get("short"), "role": claim.get("role"), "status": status,
                     "work_items": sorted(owners.get(name, [])), "last": stamp(last) if last else None,
                     "desk": claim.get("desk"), "permission_mode": st.get("permission_mode"),
                     "version": st.get("version"), "plugin_version": st.get("plugin_version"),
                     "denied": st.get("denied", 0), "focus": focus, "focus_left_min": focus_left,
                     "quota": st.get("quota"), "quota_at": st.get("quota_at"),
                     "prev_status": st.get("prev_status"), "last_message": st.get("last_message")})
    return rows


def mixed_modes(rows):
    """Names whose permission-mode class differs from most of the pool."""
    classes = [mode_class(r.get("permission_mode")) for r in rows if r.get("permission_mode")]
    if len(set(classes)) < 2:
        return []
    major = max(set(classes), key=classes.count)
    return [r["name"] for r in rows if r.get("permission_mode") and mode_class(r["permission_mode"]) != major]
