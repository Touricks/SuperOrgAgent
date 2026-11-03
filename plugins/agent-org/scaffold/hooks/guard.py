#!/usr/bin/env python3
"""PreToolUse guard for a project set up by the agent-org plugin.

Reads the hook input JSON on stdin and answers Bash, Monitor, Edit, Write,
NotebookEdit, Agent and AskUserQuestion calls, based on policy.json and lark-risk.json in this
directory. Two kinds of checks:
- hard rules guard data: only the scribe writes Feishu (in pool mode only the one
  claimed scribe session, and nobody while two scribes are claimed; in CLI mode one
  scribe at a time), the evaluation protocol's files stay read-only, high-risk and
  --yes Feishu commands are the researcher's, the hooks, settings, role definitions
  and claims under .claude/ are not edited, Agent calls carry no name, pool sessions
  do not dispatch the scribe with the Agent tool, do not switch branches, stash or
  pull in the shared directory, and claim, pre-register and release only as allowed.
  Writes to the evaluation protocol's files and to .claude/ are denied in every
  session; the other hard rules ask in the researcher's own session (deny there too
  under bypassPermissions or dontAsk, where no prompt can appear).
- soft rules are the dispatch norms: they never block, they add a reminder to the
  model's context and a line to .claude/logs/guard.jsonl.
Anything unexpected in a hard check exits 2, which blocks the call: Claude Code
treats other non-zero exits as non-blocking, so failing open must be avoided
explicitly. Settings deny and ask rules still apply after this hook answers allow.
"""
import datetime
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import feishu_cmd as fc  # noqa: E402
import instances  # noqa: E402
import pool  # noqa: E402

TMP_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")
POLICY_VERSION = 4


def glob_regex(pattern):
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


def matches(rel, patterns):
    return any(re.fullmatch(glob_regex(p), rel) for p in patterns)


class Verdict:
    ORDER = {None: 0, "allow": 1, "ask": 2, "deny": 3}

    def __init__(self, decision=None, reason=""):
        self.decision, self.reasons, self.updated = decision, [reason] if reason else [], None

    def merge(self, other):
        if self.ORDER[other.decision] > self.ORDER[self.decision]:
            self.decision = other.decision
        self.reasons.extend(r for r in other.reasons if r not in self.reasons)
        return self


def refuse(who, data, reason):
    """Ask in the researcher's own session; deny everywhere else, and wherever no prompt can appear."""
    if not who.researcher:
        return Verdict("deny", reason)
    mode = data.get("permission_mode")
    if mode in pool.BYPASS_MODES:
        return Verdict("deny", "%s 当前是 %s 模式，确认框弹不出来：请研究员在自己的终端里运行。" % (reason, mode))
    return Verdict("ask", reason)


def project_root(data):
    return os.path.normpath(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())


def resolve_role(data, policy):
    """(role, policy entry, researcher's session?) for the caller; see pool.identify."""
    who = pool.identify(data, policy, project_root(data))
    return who.role, who.pol, who.researcher


# --------------------------------------------------------------- Bash
def content_of(inv, flags):
    """Return the text passed to --content (inline, @file or stdin), or None."""
    value = fc.last(flags, "--content")
    if value is None:
        return None
    if value == "-":
        return inv.stdin            # its own here-document or here-string, or what a pipe feeds it
    if isinstance(value, str) and value.startswith("@"):
        return fc.read_script(value[1:], inv.cwd)      # regular files up to 2 MB only: a FIFO would hang the hook
    return value


def first_line(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line
    return ""


def tag_comment(args, tag):
    """Return new args with the session tag prefixed to the comment, or None."""
    new = list(args)
    for i, a in enumerate(new):
        if a == "--content" and i + 1 < len(new):
            if not new[i + 1].lstrip().startswith(tag):
                new[i + 1] = "%s %s" % (tag, new[i + 1])
            return new
        if a.startswith("--content="):
            value = a.split("=", 1)[1]
            if not value.lstrip().startswith(tag):
                new[i] = "--content=%s %s" % (tag, value)
            return new
    for i, a in enumerate(new):
        if a == "--params" and i + 1 < len(new):
            try:
                obj = json.loads(new[i + 1])
            except ValueError:
                return None
            if isinstance(obj, dict) and isinstance(obj.get("content"), str):
                if not obj["content"].lstrip().startswith(tag):
                    obj["content"] = "%s %s" % (tag, obj["content"])
                    new[i + 1] = json.dumps(obj, ensure_ascii=False)
            return new
    return new


def as_needed(policy, c):
    ident = policy["identity"]
    domain = c["pos"][0] if c["pos"] else ""
    if domain in ident["user_domains"]:
        return "user"
    if c["key"] in ident["bot_commands"]:
        return "bot"
    return None


AS_HINT = {"user": "知识库、文档和云空间的命令用研究员身份，带 --as user",
           "bot": "收发消息和订阅事件用机器人身份，带 --as bot"}


def check_lark(inv, c, who, policy, data, soft, rewritable):
    key, pol, name = c["key"], who.pol, who.label
    if c["help"]:
        return Verdict("allow", "查看帮助"), None
    if c["yes"]:
        return Verdict("deny", "带 --yes 的命令只能由研究员本人执行。不要自己加 --yes；把完整命令写进交回的内容。"), None
    if c["tier"] == "high-risk-write":
        return Verdict("deny", "%s 是 high-risk-write，只能由研究员本人执行。把完整命令写进交回的内容。" % key), None
    if c["dry_run"]:
        return Verdict("allow", "--dry-run 只预览"), None
    new_args = None
    need = as_needed(policy, c)
    if need and c["as"] != need:
        if c["tier"] == "read":
            soft.append("%s：%s。" % (key, AS_HINT[need]))
        elif c["as"] is None and rewritable:
            new_args = list(inv.args) + ["--as", need]
        else:
            return Verdict("deny", "%s 是写命令，%s；身份不对，写出来的东西会落在别人名下。" % (key, AS_HINT[need])), None
    if c["tier"] == "read":
        return Verdict("allow", "只读命令"), new_args
    if key == "docs +update" and fc.last(c["flags"], "--command") == "append":
        text = content_of(inv, c["flags"])
        if text is None:
            soft.append("看不到要追加的正文（比如从文件重定向传入），没法核对第一行的会话标签 %s；书记员用 ledger.py append。" % pol["tag"])
        elif pol["tag"] not in first_line(text):
            soft.append("追加的交接记录第一行要带会话标签 %s；书记员用 ledger.py append。" % pol["tag"])
    if key in pol.get("lark_write", ()):
        return Verdict("allow", "%s 在 %s 的写入白名单里" % (key, name)), new_args
    return refuse(who, data, "（%s）不能执行写命令 %s。要写进飞书的内容先写成草稿，请书记员登记。" % (name, key)), new_args


def check_meegle(inv, c, who, data, soft, rewritable):
    key, pol, name = c["key"], who.pol, who.label
    if c["help"]:
        return Verdict("allow", "查看帮助"), None
    if c["tier"] == "admin":
        return Verdict("deny", "%s 属于登录、诊断上报或配置类命令，只能由研究员本人执行。" % key), None
    if c["dry_run"]:
        return Verdict("allow", "--dry-run 只预览"), None
    if c["tier"] == "read":
        return Verdict("allow", "只读命令"), None
    new_args = list(inv.args)
    if key == "comment add":
        tagged = tag_comment(new_args, pol["tag"])
        if tagged is None:
            soft.append("评论的 --params 不是合法 JSON，没法补会话标签 %s；请写在内容开头。" % pol["tag"])
        elif tagged != new_args and not rewritable:
            soft.append("评论开头缺会话标签 %s；命令不是单条简单命令，没法自动补，请在内容开头加上。" % pol["tag"])
        else:
            new_args = tagged
    if rewritable and "--envelope" not in c["flags"]:
        new_args.append("--envelope")
    updated = new_args if new_args != inv.args else None
    if key in pol.get("meegle_write", ()):
        return Verdict("allow", "%s 在 %s 的写入白名单里" % (key, name)), updated
    return refuse(who, data, "（%s）不能执行写命令 %s。工作项、字段、状态和文档只由书记员写；先写成草稿，请书记员登记。" % (name, key)), None


LEDGER_READS = {"", "get", "query", "mql", "trace", "status", "-h", "--help"}
LEDGER_WRITE_WORDS = {"register", "reply", "transition", "update", "comment", "status-block", "append",
                      "archive", "ensure-node"}
_WORDS = "|".join(re.escape(w) for w in sorted(LEDGER_WRITE_WORDS, key=len, reverse=True))
CODE_WRITE = re.compile(r"(?<![\w-])['\"](?:%s)['\"](?!\s*:)|ledger\.py['\"]?\s+(?:-\S+\s+)*(?:%s)\b" % (_WORDS, _WORDS))
CODE_RUNS_LEDGER = re.compile(r"run_(?:path|module)\([^)]*ledger")
CODE_IMPORTS_LEDGER = re.compile(r"\bimport\s+ledger\b|\bfrom\s+ledger\s+import\b|import_module\(\s*['\"]ledger['\"]")
STRING_LITERAL = re.compile(r"""(['"])((?:\\.|(?!\1)[^\\\n])*)\1""")
LITERAL_AFTER = re.compile(r"(?:\}|ledger\.py)\s+(?:-\S+\s+)*(?:%s)\s+(?:\{|#?\d|--apply)" % _WORDS)   # f"{L} archive {i}"
LITERAL_LEAD = re.compile(r"\s*(?:%s)\s+(?:#?\d{3,}|\{[^}]*\}|.*\s--apply\b)" % _WORDS)               # "archive 2106 --apply".split()
SHELL_SCRIPT = re.compile(r"^#!.*\b(?:sh|bash|zsh|dash|ksh)\b")
# Code can only start ledger.py through one of these (imports and runpy are checked separately).
CODE_EXECUTES = re.compile(r"subprocess|os\.(?:system|popen|exec|spawn)|\bpty\.spawn|Popen|check_output|check_call|getoutput"
                           r"|child_process|execSync|execFileSync|spawnSync|\bsystem\s*\(|\bexec\s*\(|\bqx\b|%x|IO\.popen|Open3|`")
PYTHON = fc.PYTHON_RE
SHELLS_RUN = set(fc.SHELLS) | {"csh", "tcsh"}
SOURCE = {"source", "."}
INLINE_CODE = {"perl", "ruby", "node", "php", "bun", "deno"}
SCRIPT_MODULES = {"cProfile", "profile", "pdb", "trace", "runpy"}
RUN_WRAPPERS = {"sudo", "doas", "chrt", "ionice", "taskset", "nohup", "time", "command", "exec", "caffeinate",
                "arch", "nice", "timeout", "env", "stdbuf", "xargs"} | fc.RUNNERS
GLOB_QUALIFIER = re.compile(r"(ledger[^\s;&|()<>'\"]*)\([^()\s]*\)")    # zsh: ledger.py(.)


def _is_ledger_script(tok):
    name = os.path.basename(tok)
    if name == "ledger.py":
        return True
    return any(ch in name for ch in "*?[") and fc.glob_names_ledger(name)


def _ledger_named(tok, rest):
    """tok runs ledger.py: the script itself, or a renamed copy (my_ledger.py) given a write subcommand."""
    if _is_ledger_script(tok):
        return True
    return os.path.basename(tok).endswith("ledger.py") and _subcommand(rest) in LEDGER_WRITE_WORDS


def _subcommand(args):
    """The ledger.py subcommand in the arguments after the script; --help before any -- makes it a read."""
    head = args[:args.index("--")] if "--" in args else args
    if "-h" in head or "--help" in head:
        return "--help"
    return next((a for a in args if not a.startswith("-")), "")


def _verdict(sub):
    return None if sub in LEDGER_READS else sub


def _writes_in_code(code):
    """Does Python (or other) source text run a ledger.py write? Imports of ledger count as writes.

    Only code that names ledger.py and can start a process counts (a data file such as
    ledger-open.json does not, nor a draft whose text mentions the command).
    A write word counts when it is quoted on its own as a list item ('register', not a
    'register': key), follows ledger.py, follows a {placeholder} inside a string and goes on
    with an argument (f"{L} archive {i}"), or starts a string that goes on with an ID or
    --apply ("archive 2106 --apply".split()). Full-line comments are skipped.
    """
    code = fc.code_lines(code)
    if CODE_IMPORTS_LEDGER.search(code):
        return True
    if "ledger.py" not in code or not CODE_EXECUTES.search(code):  # text about ledger.py written to a file
        return False
    if CODE_WRITE.search(code):
        return True
    return any(LITERAL_AFTER.search(m.group(2)) or LITERAL_LEAD.match(m.group(2))
               for m in STRING_LITERAL.finditer(code))


def _code_verdict(code, rest):
    """python -c CODE [args], or a script on stdin: CODE that runs ledger.py takes args as its subcommand."""
    if CODE_RUNS_LEDGER.search(code):
        return _verdict(_subcommand(rest)) if rest else "（代码里运行了 ledger.py）"
    return "（代码里调用了 ledger.py 的写命令）" if _writes_in_code(code) else None


class _Ctx:
    def __init__(self, bodies, cwd, depth):
        self.text, self.bodies, self.cwd, self.depth = "", bodies, cwd, depth
        self.assign = {}
        self.find_named = None      # inside a find: True when it selects ledger.py, so {} is the script


class _Stdin:
    """What a command reads on standard input, as far as the text shows."""

    def __init__(self, texts=(), ledger=False):
        self.texts = [t for t in texts if t is not None]
        self.ledger = ledger        # ledger.py itself: python3 - archive 1 < ledger.py, cat ledger.py | python3 -


def _substitute(payload, ctx):
    for name, value in ctx.assign.items():
        payload = payload.replace("${%s}" % name, value).replace("$%s" % name, value)
    return payload


def _script_text(path, ctx):
    full = fc._full(path, ctx.cwd)
    if full in fc.TRUSTED:
        return None
    return fc.read_script(path, ctx.cwd)


def _python_run(argv, ctx, stdin, via_xargs=False):
    kind, script, rest = fc.python_target(argv[1:])     # option clusters: -BW ignore, -IBc CODE, -Bm mod
    if kind == "code":
        return _code_verdict(script, rest)
    if kind == "module":
        return _module_verdict(script, rest)
    if kind == "stdin":
        if stdin.ledger:
            return _verdict(_subcommand(rest))
        for text in stdin.texts:
            found = _code_verdict(text, rest)
            if found:
                return found
        known = stdin.texts and not any(m in t for t in stdin.texts for m in ("$(", "`", "ledger", "$__SUB"))
        if not known and "ledger.py" in ctx.text and _subcommand(rest) in LEDGER_WRITE_WORDS:
            return _subcommand(rest)                   # python3 - archive 1 <<< "$(cat ledger.py)"
        return None
    if _ledger_named(script, rest):
        sub = _subcommand(rest)
        return "（xargs 传入的子命令）" if not sub and via_xargs else _verdict(sub)
    name = script.rsplit("/", 1)[-1]
    if ("$" in name or "`" in name) and "ledger.py" in ctx.text and _subcommand(rest) in LEDGER_WRITE_WORDS:
        return _subcommand(rest)                       # for f in .../ledger.py; do python3 $f archive ...
    text = _script_text(script, ctx)
    if text and _writes_in_code(text):
        return "（脚本 %s 里调用了 ledger.py 的写命令）" % script
    return None


def _module_verdict(module, rest):
    if module == "ledger" or module.endswith(".ledger"):
        return _verdict(_subcommand(rest))
    if module in SCRIPT_MODULES:
        for j, a in enumerate(rest):
            if _is_ledger_script(a):
                return _verdict(_subcommand(rest[j + 1:]))
            if a == "-m" and j + 1 < len(rest) and (rest[j + 1] == "ledger" or rest[j + 1].endswith(".ledger")):
                return _verdict(_subcommand(rest[j + 2:]))
            if module == "runpy" and (a == "ledger" or a.endswith(".ledger")):
                return _verdict(_subcommand(rest[j + 1:]))
    return None


def _pipe_out(argv, stdin, ctx):
    """(text, is ledger.py) that a command writes to a pipe: cat/head/tail of files or of their own
    standard input, echo/printf text."""
    if not argv:
        return None, False
    head = os.path.basename(argv[0])
    if head in ("cat", "head", "tail"):
        files = [a for a in argv[1:] if not a.startswith("-")]
        if not files:
            return "\n".join(stdin.texts) or None, stdin.ledger
        if any(_is_ledger_script(f) for f in files):
            return None, True
        return "\n".join(t for t in (_script_text(f, ctx) for f in files) if t) or None, False
    if head in ("echo", "printf"):
        return " ".join(a for a in argv[1:] if not a.startswith("-")), False
    return None, False


def _strip_substitutions(text, ctx):
    """Replace $(...) and `...` outside single quotes with placeholders; check what they run."""
    out, i, n, q, k = [], 0, len(text), None, 0
    while i < n:
        c = text[i]
        if q == "'":
            out.append(c)
            if c == "'":
                q = None
            i += 1
            continue
        if c == "'" and q is None:
            q = "'"
            out.append(c)
            i += 1
            continue
        if c == '"':
            q = None if q == '"' else '"'
            out.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        inner = None
        if text.startswith("$(", i) and not text.startswith("$((", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            inner, i = text[i + 2:j - 1], j
        elif c == "`":
            j = text.find("`", i + 1)
            j = n if j < 0 else j
            inner, i = text[i + 1:j], j + 1
        if inner is not None:
            found = _ledger_write(inner, ctx.cwd, ctx.depth + 1, ctx.bodies)
            if found:
                return None, found
            k += 1
            out.append("$__SUB%d" % k)
            continue
        out.append(c)
        i += 1
    return "".join(out), None


def ledger_write(command, cwd=None):
    """The ledger.py subcommand a shell command would run, unless it is a read.

    A guardrail against accidental writes by well-behaved agents, not a security
    boundary: enough obfuscation can always hide a command from a shell parser.
    Returns None when ledger.py is not run, or only with get/query/mql/trace/status,
    --help or no subcommand. Handled: comments, new lines, ; && || |, cd, shell
    keywords and functions, wrappers and project runners (env, timeout, nice,
    sudo, xargs, uv run ...), python option clusters, -c / -m (ledger, runpy,
    cProfile ...), scripts on stdin (here-documents, here-strings, < file, pipes),
    script files run by a shell, source, python or their own path, $(...) and
    backticks, eval and sh -c (with simple $VAR substitution), find -exec, other
    interpreters' inline code, and glob script names.
    """
    return _ledger_write(command, cwd, 0)


def _ledger_write(command, cwd, depth, bodies=None):
    if depth > 4:
        return "（嵌套太深）" if "ledger" in command else None
    text, new = fc.split_heredocs(command)
    ctx = _Ctx(dict(bodies or {}), cwd, depth)
    ctx.bodies.update(new)
    for body, quoted in new.values():                  # $(...) in an unquoted here-document runs
        if not quoted:
            for inner in fc.substitutions(body):
                found = _ledger_write(inner, cwd, depth + 1, ctx.bodies)
                if found:
                    return found
    text = GLOB_QUALIFIER.sub(r"\1", fc.strip_comments(text).replace("\\\n", " "))   # a comment ending in \ does not continue
    ctx.text = text                                    # still with the text of $(...): "ledger.py" in ctx.text
    text, found = _strip_substitutions(text, ctx)
    if found:
        return found
    try:
        tokens = fc.tokenize(text)
    except ValueError:
        return "（看不清的命令）" if "ledger" in text else None
    segs, piped = fc._segments(tokens)
    prev_out, prev_ledger = None, False
    for raw, is_piped in zip(segs, piped):
        docs, strings, infile = fc.seg_stdin(raw, ctx.bodies, ctx.cwd)
        texts = docs + strings
        from_ledger = bool(infile and _is_ledger_script(infile))
        if infile and not from_ledger:
            texts.append(_script_text(infile, ctx))
        if is_piped:
            texts.append(prev_out)
            from_ledger = from_ledger or prev_ledger
        stdin = _Stdin(texts, from_ledger)
        for tok in raw:
            if fc.ASSIGN_RE.match(tok):
                name, _, value = tok.partition("=")
                ctx.assign[name] = value
        seg, _ = fc._drop_redirects(raw)
        argv, wrappers = fc._unwrap(seg)
        if argv and argv[0] == "function":
            argv, more = fc._unwrap(argv[2:])
            wrappers = wrappers + more
        if argv and (" " in argv[0] or "\t" in argv[0]):        # env -S 'python3 -I' ...
            try:
                argv = shlex.split(argv[0]) + argv[1:]
            except ValueError:
                pass
        if argv and os.path.basename(argv[0]) == "cd":
            ctx.cwd = fc._cd(ctx.cwd, argv)
            prev_out, prev_ledger = None, False
            continue
        if argv and not (os.path.basename(argv[0]) == "find" or argv[0].startswith("-")):
            ctx.find_named = None
        found = _segment_write(argv, wrappers, ctx, stdin)
        if found:
            return found
        prev_out, prev_ledger = _pipe_out(argv, stdin, ctx)
    return None


def _direct_script(argv, ctx):
    """./x.sh, ./x.py, uv run x.py: read the file and check it as bash FILE or python FILE would."""
    path = argv[0]
    if "/" not in path and not os.path.isfile(os.path.join(ctx.cwd or ".", path)):
        return None
    text = _script_text(path, ctx)
    if not text or "\0" in text:
        return None
    first = text.split("\n", 1)[0]
    shell = path.endswith((".sh", ".bash", ".zsh")) or bool(SHELL_SCRIPT.match(first)) or (
        not first.startswith("#!") and "." not in os.path.basename(path))
    if shell:
        return _ledger_write(text, ctx.cwd, ctx.depth + 1, ctx.bodies)
    return "（脚本 %s 里调用了 ledger.py 的写命令）" % path if _writes_in_code(text) else None


def _segment_write(argv, wrappers, ctx, stdin):
    if not argv:
        return None
    head = os.path.basename(argv[0])
    via_xargs = any(os.path.basename(w) == "xargs" for w in wrappers)
    if via_xargs and stdin.texts:                         # echo archive 2106 | xargs python3 ledger.py
        argv = argv + " ".join(stdin.texts).split()

    def run_verdict(args):
        sub = _subcommand(args)
        if not sub and via_xargs:
            return "（xargs 传入的子命令）"
        return _verdict(sub)

    def check_texts(texts):
        for src in texts:
            found = _ledger_write(src, ctx.cwd, ctx.depth + 1, ctx.bodies)
            if found:
                return found
        return None

    if head == "eval":
        return _ledger_write(_substitute(" ".join(argv[1:]), ctx), ctx.cwd, ctx.depth + 1, ctx.bodies)
    if head in SHELLS_RUN:
        kind, value, _ = fc.shell_target(argv[1:])
        if kind == "command":
            return _ledger_write(_substitute(value, ctx), ctx.cwd, ctx.depth + 1, ctx.bodies)
        if kind == "script":
            text = _script_text(value, ctx)
            if text is not None:
                return _ledger_write(text, ctx.cwd, ctx.depth + 1, ctx.bodies)
            if not os.path.exists(fc._full(value, ctx.cwd)):  # written by a here-document earlier in this command
                return check_texts(b for b, _ in ctx.bodies.values())
            return None
        return check_texts(stdin.texts)
    if head in SOURCE and len(argv) > 1:
        text = _script_text(argv[1], ctx)
        if text is not None:
            return _ledger_write(text, ctx.cwd, ctx.depth + 1, ctx.bodies)
        if not os.path.exists(fc._full(argv[1], ctx.cwd)):
            return check_texts(b for b, _ in ctx.bodies.values())
        return None
    if head == "find" or (argv[0].startswith("-") and ctx.find_named is not None):
        if head == "find":                                # find bin -name ledger.py -execdir python3 {} <sub>
            ctx.find_named = fc.find_names_ledger(argv)
        else:                                             # the rest of a find split at ( or )
            ctx.find_named = ctx.find_named or fc.find_names_ledger(argv)
        for cmd in fc.find_clauses(argv):
            cmd = ["ledger.py" if a == "{}" and ctx.find_named else a for a in cmd]
            found = _ledger_write(shlex.join(cmd), ctx.cwd, ctx.depth + 1, ctx.bodies)
            if found:
                return found
        return None
    if head in INLINE_CODE:
        code = fc.inline_code(head, argv[1:])
        if code is None:
            files = [a for a in argv[1:] if not a.startswith("-")][:1]
            code = _script_text(files[0], ctx) if files else ("\n".join(stdin.texts) or None)
        if code and _writes_in_code(code):
            return "（%s 的代码里调用了 ledger.py 的写命令）" % head
        return None
    if PYTHON.search(head):
        return _python_run(argv, ctx, stdin, via_xargs)
    if _ledger_named(argv[0], argv[1:]):                  # ./ledger.py <sub>, uv run ledger.py <sub>
        return run_verdict(argv[1:])
    j = next((k for k, w in enumerate(argv) if PYTHON.search(os.path.basename(w))), None)
    wrapped = head in RUN_WRAPPERS or any(os.path.basename(w) in RUN_WRAPPERS for w in wrappers)
    if j is not None and wrapped:                         # a wrapper with option values we do not know
        return _python_run(argv[j:], ctx, stdin, via_xargs)
    if argv[0].startswith("$"):                           # $PY .../ledger.py <sub>
        s = next((k for k, w in enumerate(argv) if _is_ledger_script(w)), None)
        if s is not None:
            return run_verdict(argv[s + 1:])
        if "ledger" in ctx.text:
            return next((w for w in argv[1:] if w in LEDGER_WRITE_WORDS), None)
        return None
    return _direct_script(argv, ctx)


POOL_SUBS = ("claim", "expect", "release")
GIT_SWITCHES = ("checkout", "switch", "stash", "rebase", "merge", "reset", "clean", "restore", "pull")
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env",
                     "--super-prefix", "--list-cmds"}
SHELL_NAMES = set(fc.SHELLS) | {"csh", "tcsh"}
IDENTITY = r"CLAUDE_CODE_(?:HOST_)?SESSION_ID"
IDENTITY_VARS = re.compile(
    r"\bunset\b[^;&|\n]*CLAUDE_CODE_"                                        # unset [-v] NAME, "NAME", NAME{,…}
    r"|\b(?:export|declare|typeset|readonly|local)\b[^;&|\n]*" + IDENTITY +  # export -n NAME, declare +x NAME
    r"|(?:^|\s)-u\s*['\"]?CLAUDE_CODE_|--unset[= ]\s*['\"]?CLAUDE_CODE_"     # env -u NAME
    r"|(?:^|[;&|({\n])\s*(?:[A-Za-z_]\w*=\S*\s+)*" + IDENTITY + "="           # NAME=value where a command starts
    r"|\benv\b(?:\s+(?:-[uCS]\s+\S+|-\S+|[A-Za-z_]\w*=\S*))*\s+" + IDENTITY + "=", re.M)
CLEAR_ENV = re.compile(r"\benv\b[^;&|\n]*\s(?:-[a-zA-Z]*i[a-zA-Z]*|--ignore-environment|-)(?=\s|$)", re.M)
SUB_MARK = re.compile(r"\$__SUB(\d+)")
VAR_REF = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def split_substitutions(text, heredoc=False):
    """(text with each $(...) and `...` the shell would run replaced by $__SUB<n>, their inner texts).
    Inside single quotes and after a backslash they are plain text; a here-document body has no quotes."""
    out, subs, i, n, q = [], [], 0, len(text), None
    while i < n:
        c = text[i]
        if q == "'":
            out.append(c)
            q = None if c == "'" else q
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if not heredoc and c == "'" and q is None:
            q = "'"
        elif not heredoc and c == '"':
            q = None if q == '"' else '"'
        elif text.startswith("$(", i) and not text.startswith("$((", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            subs.append(text[i + 2:j - 1] if depth == 0 else text[i + 2:])
            out.append("$__SUB%d" % len(subs))
            i = j
            continue
        elif c == "`":
            j = text.find("`", i + 1)
            subs.append(text[i + 1:] if j < 0 else text[i + 1:j])
            out.append("$__SUB%d" % len(subs))
            i = n if j < 0 else j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out), subs


def expand(word, assign):
    """$NAME and ${NAME} replaced by values assigned earlier in the same command."""
    if "$" not in word or not assign:
        return word
    return VAR_REF.sub(lambda m: assign.get(m.group(1) or m.group(2), m.group(0)), word)


def target_dir(cur, path, root):
    """Where cd PATH (or git -C PATH) leads from cur, ~ and $VAR expanded; the project root when it
    cannot be told (cd -, a variable the hook does not have)."""
    if path == "-" or cur is None:
        return root
    path = os.path.expandvars(os.path.expanduser(path or "~"))
    if "$" in path or "`" in path:
        return root
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(cur, path))


def full_path(path, where):
    return os.path.normpath(os.path.join(where or ".", os.path.expanduser(path)))


def fed_script(path, where, written, bodies):
    """Texts a shell or source would read from path: what this command wrote there earlier
    (echo, printf, cat or tee into it), the file itself, or, for a file that does not exist yet,
    the here-documents of the command."""
    full = full_path(path, where)
    texts = [written[full]] if full in written else []
    text = fc.read_script(full, None)
    if text is not None:
        texts.append(text)
    elif not texts and not os.path.exists(full):
        texts += [b for b, _ in bodies.values()]
    return texts


def out_file(raw, where):
    """The file a command's > or >> writes to, or None."""
    for i, t in enumerate(raw[:-1]):
        if t in (">", ">>", ">|", "&>"):
            return full_path(raw[i + 1], where)
    return None


def find_pick(argv):
    """What {} stands for in a find's -exec: git or route.py when the find selects that file."""
    for i, a in enumerate(argv[1:], 1):
        if a in fc.FIND_NAMES and i + 1 < len(argv):
            names = [argv[i + 1]]
        elif not a.startswith("-") and (i == 1 or argv[i - 1] not in fc.FIND_NAMES):
            names = [a]
        else:
            continue
        for v in names:
            base = os.path.basename(v)
            for pick in ("git", "route.py"):
                try:
                    if base == pick or fnmatch.fnmatchcase(pick, base):
                        return pick
                except re.error:
                    pass
    return None


def simple_output(tokens, where):
    """What one plain echo, printf or cat prints, from its tokens; None for anything else."""
    segs, _ = fc._segments(tokens)
    if len(segs) != 1:
        return None
    argv, _ = fc._unwrap(fc._drop_redirects(segs[0])[0])
    text = fc.pipe_output(argv, "", where)
    if text and argv and os.path.basename(argv[0]) == "printf":
        text = text.replace("\\n", "\n").replace("\\t", "\t")
    return text


def command_runs(command, cwd, root, depth=0, bodies=None, assign=None, written=None):
    """(argv, directory) of each simple command a shell command runs, as far as the text shows.

    Split at ; && || | and ( ); wrappers (env, timeout, nohup ...) dropped; cd followed, but a cd
    inside ( ... ), $(...) or a pipeline stays there. Followed: $(...) and backticks the shell runs
    (not those inside single quotes, after a backslash or in a comment), sh -c and eval, simple
    NAME=value variables, here-documents, here-strings, pipes and files fed to a shell or to
    source (a file written earlier in the same command counts), and find -exec. Here-document
    bodies not fed to a shell, quoted arguments and comments are not commands. (None, text)
    stands for text the parser cannot read."""
    if depth > 4:
        return []
    text, new = fc.split_heredocs(command)
    bodies = dict(bodies or {})
    bodies.update(new)
    assign = dict(assign or {})
    written = {} if written is None else written
    out = []

    def deeper(inner, where):
        return command_runs(inner, where, root, depth + 1, bodies, assign, written)

    for body, quoted in new.values():                   # an unquoted here-document expands $(...)
        if not quoted:
            for inner in split_substitutions(body, heredoc=True)[1]:
                out.extend(deeper(inner, cwd))
    text, subs = split_substitutions(fc.strip_comments(text).replace("\\\n", " "))
    try:
        tokens = fc.tokenize(text)
    except ValueError:
        for inner in subs:
            out.extend(deeper(inner, cwd))
        return out + [(None, text)]
    st, stack, done = {"where": cwd, "prev_out": None}, [], set()

    def segment(raw, piped_in, piped_out):
        where = st["where"]
        for tok in raw:                                 # $(...) in this command runs here, before it
            for m in SUB_MARK.finditer(tok):
                k = int(m.group(1)) - 1
                if 0 <= k < len(subs) and k not in done:
                    done.add(k)
                    out.extend(deeper(subs[k], where))
        for tok in raw:
            if fc.ASSIGN_RE.match(tok):
                name, _, value = tok.partition("=")
                assign[name] = expand(value, assign)
        docs, strings, infile = fc.seg_stdin(raw, bodies, where)
        fed = docs + strings + (fed_script(infile, where, written, {}) if infile else [])
        if piped_in and st["prev_out"]:
            fed.append(st["prev_out"])
        stdin_text = "\n".join(t for t in fed if t)
        st["prev_out"] = None
        seg, _ = fc._drop_redirects(raw)
        argv, _ = fc._unwrap(seg)
        argv = [expand(a, assign) for a in argv]
        if argv and (" " in argv[0] or "\t" in argv[0]):   # env -S 'python3 -I' ..., $C holding a command
            try:
                argv = shlex.split(argv[0]) + argv[1:]
            except ValueError:
                pass
        if not argv:
            return
        head = os.path.basename(argv[0]).lstrip("\\")
        if head == "cd":
            if not (piped_in or piped_out):
                st["where"] = target_dir(where, argv[1] if len(argv) > 1 else "", root)
            return
        if head in SHELL_NAMES:
            kind, value, _ = fc.shell_target(argv[1:])
            if kind == "command":
                out.extend(deeper(value, where))
                return
            for t in ([stdin_text] if kind == "stdin" else fed_script(value, where, written, bodies)):
                if t:
                    out.extend(deeper(t, where))
            out.append((argv, where))
            return
        if head in SOURCE and len(argv) > 1:
            for t in fed_script(argv[1], where, written, bodies):
                if t:
                    out.extend(deeper(t, where))
            return
        if head == "eval":
            out.extend(deeper(" ".join(argv[1:]), where))
            return
        if head == "find":
            pick = find_pick(argv)
            for clause in fc.find_clauses(argv):
                clause = [pick if x == "{}" and pick else x for x in clause]
                if clause:
                    out.extend(deeper(shlex.join(clause), where))
        if head == "xcrun" and len(argv) > 1:
            argv = argv[1:]
        out.append((argv, where))
        text_out = fc.pipe_output(argv, stdin_text, where)
        if text_out and (head == "printf" or (head == "echo" and "-e" in argv[1:2])):
            text_out = text_out.replace("\\n", "\n").replace("\\t", "\t")      # printf 'git stash\n'
        if head == "tee":
            text_out = stdin_text
            for f in argv[1:]:
                if not f.startswith("-"):
                    written[full_path(f, where)] = stdin_text
        target = out_file(raw, where)
        if target and text_out is not None:
            written[target] = text_out
        st["prev_out"] = text_out

    cur, piped_in, i = [], False, 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if tok in ("<", ">") and i < len(tokens) and tokens[i] == "(":        # <(cmd) >(cmd): runs cmd
            depth, j = 1, i + 1
            while j < len(tokens) and depth:
                depth += {"(": 1, ")": -1}.get(tokens[j], 0)
                j += 1
            inner = tokens[i + 1:j - 1]
            out.extend(deeper(" ".join(t if t in fc.SEPARATORS or t in fc.REDIRECTS else shlex.quote(t) for t in inner),
                              st["where"]))
            produced = simple_output(inner, st["where"]) if tok == "<" else None
            if produced:                                # bash <(echo git stash) reads what cmd prints
                cur += ["<<<", produced]
            i = j
            continue
        if tok in fc.SEPARATORS:
            piped_out = tok in ("|", "|&")
            if cur:
                segment(cur, piped_in, piped_out)
                cur = []
            piped_in = piped_out
            if tok == "(":
                stack.append(st["where"])
            elif tok == ")" and stack:
                st["where"] = stack.pop()
            continue
        cur.append(tok)
    if cur:
        segment(cur, piped_in, False)
    for k, inner in enumerate(subs):
        if k not in done:
            out.extend(deeper(inner, st["where"]))
    return out


def route_sub(argv):
    """The route.py subcommand this argv runs (interpreter options, global options such as
    --offline and runner modules such as cProfile skipped), or None."""
    if not argv:
        return None
    if PYTHON.search(os.path.basename(argv[0])):
        kind, target, rest = fc.python_target(argv[1:])
        if kind == "module" and (target == "route" or str(target).endswith(".route")):
            script = "route.py"
        elif kind == "module" and target in SCRIPT_MODULES:     # python3 -m cProfile -o x route.py <sub>
            j = next((k for k, a in enumerate(rest) if os.path.basename(a) == "route.py"
                      or ((target == "runpy" or (k and rest[k - 1] == "-m")) and (a == "route" or a.endswith(".route")))),
                     None)
            if j is None:
                return None
            script, rest = "route.py", rest[j + 1:]
        elif kind == "script":
            script = target
        else:
            return None
    else:
        script, rest = argv[0], argv[1:]
    if os.path.basename(script or "").lstrip("\\") != "route.py":
        return None
    return next((x for x in rest if not x.startswith("-")), "")


def flag_value(args, flag):
    for i, x in enumerate(args):
        if x == flag and i + 1 < len(args):
            return args[i + 1]
        if x.startswith(flag + "="):
            return x.split("=", 1)[1]
    return None


def check_pool_command(data, who, policy, root, command):
    """route.py claim / expect / release: one plain command, run by a session allowed to."""
    runs = command_runs(command, data.get("cwd") or root, root)
    subs = [route_sub(argv) for argv, _ in runs if argv]
    sub = next((x for x in subs if x in POOL_SUBS), None)
    if sub is None:
        unread = [t for argv, t in runs if argv is None]
        if any("route.py" in t and re.search(r"\b(?:%s)\b" % "|".join(POOL_SUBS), t) for t in unread):
            return refuse(who, data, "看不清的 route.py 命令；认领、预登记、释放要写成单条简单命令。")
        return None
    if data.get("agent_id"):
        return Verdict("deny", "子智能体不能运行 route.py %s：认领属于会话本身，不属于会话里开出的子智能体。" % sub)
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = []
    plain = (len(argv) >= 4 and PYTHON.search(os.path.basename(argv[0])) and argv[1] == "-I"
             and argv[2].endswith(".claude/agent-org/bin/route.py") and argv[3] == sub
             and not re.search(r"[;&|`$<>\n]", command))
    if not plain:
        return Verdict("deny", "route.py %s 要写成单条简单命令：python3 -I .claude/agent-org/bin/route.py %s …；"
                               "前面不带环境变量赋值、包装命令或 --offline 这类全局选项，后面不接管道。" % (sub, sub))
    args = argv[4:]
    names = [a.split("=", 1)[0] for a in args if a.startswith("--")]
    twice = sorted({n for n in names if names.count(n) > 1})
    if twice:
        return Verdict("deny", "route.py %s 的选项不能重复写（%s）：hook 和脚本会各读到不同的值。" % (sub, "、".join(twice)))
    desk = os.environ.get(pool.DESK_ENV) or ""
    given = flag_value(args, "--desk")
    if sub == "claim":
        if given is not None and given != desk:
            if desk:
                return Verdict("deny", "--desk %s 和本会话的桌面会话 ID 不一致。" % (given or "''"))
            return refuse(who, data, "hook 看不到本会话的桌面会话 ID，没法核对 --desk %s；会话自己的 Bash 里有桌面会话 ID 时，"
                                     "去掉 --desk 重试。" % (given or "''"))
        if who.origin in ("claim", "permit", "pending", "researcher"):
            return None                       # route.py checks the permit against this session's own IDs
        if who.origin == "tombstone":
            return Verdict("deny", "本会话的认领已经释放；要重新认领，请研究员或开它的会话重新预登记。")
        if who.origin == "desk_conflict":
            return Verdict("deny", "本会话和已认领的 %s 共用一个桌面会话 ID（可能是从那个会话里起的进程），不能认领。" % who.conflict)
        return Verdict("deny", "（%s）不能认领。" % who.label)
    if sub == "expect":
        role = flag_value(args, "--role") or ""
        if who.researcher:
            return None
        if who.origin != "claim":
            return Verdict("deny", "只有已认领的 +2（预登记本序列的 +1、+0）、入口（预登记路由和书记员）和研究员自己的会话能预登记。")
        mine = policy["agents"].get(who.role) or {}
        target = policy["agents"].get(role) or {}
        if who.role == policy.get("entry_role") and role in (policy.get("router_role"), policy.get("scribe_role")):
            return None
        if mine.get("rank") == 2 and who.role != policy.get("entry_role") and target.get("sequence") == mine.get("sequence") \
                and target.get("rank") in (0, 1):
            return None
        return Verdict("deny", "（%s）不能预登记 %s：+2 只预登记本序列的 +1、+0，入口只预登记路由和书记员。" % (who.label, role or "这个角色"))
    if who.researcher:
        return refuse(who, data, "释放认领会让这个会话不再是 %s；请研究员确认。" % (args[0] if args else "该角色"))
    return Verdict("deny", "只有研究员能释放认领（route.py release）。")


def check_identity_vars(data, who, command):
    """Pool mode: a command that unsets, clears or sets the session's identity variables would speak
    for another session to route.py and ledger.py."""
    if IDENTITY_VARS.search(command) or (CLEAR_ENV.search(command) and re.search(r"route|ledger", command)):
        return Verdict("deny", "不要改或清掉 CLAUDE_CODE_SESSION_ID、CLAUDE_CODE_HOST_SESSION_ID（unset、env -u、env -i、export -n 都算）："
                               "认领和台账写权按它们认会话。")
    return None


def linked_worktree(path, root):
    """Is path inside a linked git worktree below the project root (a .git file pointing into
    .../worktrees/, such as Claude Code's own .claude/worktrees/<name>)? Such a tree is not shared.
    A nested clone or a submodule is still part of the shared directory."""
    real_root, cur = os.path.realpath(root), resolved(path)
    while inside(cur, real_root) and cur != real_root:
        dot = os.path.join(cur, ".git")
        if os.path.isdir(dot):
            return False
        if os.path.isfile(dot):
            try:
                with open(dot, encoding="utf-8", errors="ignore") as f:
                    head = f.read(1024)
            except OSError:
                return False
            return head.startswith("gitdir:") and "/worktrees/" in head
        cur = os.path.dirname(cur)
    return False


def outside(path, cwd, root):
    full = os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd or root, path))
    if not inside(resolved(full), os.path.realpath(root)) and not inside(full, root):
        return True
    return linked_worktree(full, root)


def git_switch(command, root, cwd=None):
    """The git subcommand that switches, stashes, merges or pulls in this project's working tree,
    if the command runs one. Read-only forms (stash list/show, --help, clean -n) and git run in
    another tree (-C, --work-tree, a cd before it, a linked worktree) do not count; with --git-dir
    alone the working tree is still the current directory."""
    for argv, where in command_runs(command, cwd or root, root):
        if not argv:
            continue
        head = os.path.basename(argv[0]).lstrip("\\")
        if head == "gh" and argv[1:3] == ["pr", "checkout"]:
            if not outside(where, where, root):
                return "pr checkout"
            continue
        if head != "git":
            continue
        i, target, work = 1, where, None
        while i < len(argv) and argv[i].startswith("-"):
            opt = argv[i].split("=", 1)[0]
            value = argv[i].split("=", 1)[1] if "=" in argv[i] else (argv[i + 1] if i + 1 < len(argv) else "")
            if opt == "-C" and value:
                target = target_dir(target, value, root)
            elif opt == "--work-tree" and value:
                work = value
            i += 1 if "=" in argv[i] else (2 if opt in GIT_VALUE_OPTIONS else 1)
        if work:
            target = target_dir(target, work, root)
        if i >= len(argv) or argv[i] not in GIT_SWITCHES or outside(target, target, root):
            continue
        sub, rest = argv[i], argv[i + 1:]
        if "-h" in rest or "--help" in rest:
            continue
        if sub == "stash" and rest[:1] in (["list"], ["show"]):
            continue
        if sub == "clean" and any(x in ("-n", "--dry-run") or (x.startswith("-") and not x.startswith("--") and "n" in x)
                                  for x in rest):
            continue
        return sub
    return None


def check_git(data, who, command, root):
    sub = git_switch(command, root, data.get("cwd"))
    if not sub:
        return None
    reason = ("（%s）不能在共享工作区里运行 git %s：会话池的会话共用这个目录，切分支、stash、reset、pull 这类命令会丢掉"
              "或混进别的会话还没提交的改动。把要做的事写进交回，由研究员执行。" % (who.label, sub))
    return refuse(who, data, reason)


def check_ledger(data, who, policy, root, sub):
    scribe = policy.get("scribe_role", "pmo-scribe")
    if policy.get("mode") == "pool":
        fixed = (policy["agents"].get(scribe) or {}).get("fixed_session") or scribe
        pl, ok = pool.load(root)
        if not ok or len(pool.claims_of(pl, scribe)) > 1:
            return Verdict("deny", "认领表读不出来，或者书记员的认领不止一条：为免两处同时写台账，所有会话都先停写。"
                                   "请研究员用 route.py pool 核对，release 多余的认领。")
        if who.origin == "claim" and who.role == scribe:
            if pool.scribe_ok(pl, policy, who.claim):
                return Verdict("allow", "唯一认领的书记员 %s 写台账" % who.name)
            return Verdict("deny", "这条书记员认领已经失效；请研究员用 route.py pool 核对。")
        return refuse(who, data, "（%s）不能运行 ledger.py %s：工作项、字段、状态和文档只由书记员 %s 写。写成草稿，给它发【%s】。" % (
            who.label, sub, fixed, (policy.get("msg") or {}).get("register_request", "登记请求")))
    if who.pol.get("ledger_write"):
        return Verdict()
    return refuse(who, data, "（%s）不能运行 ledger.py %s：工作项、字段、状态和文档只由书记员写。写成草稿，请书记员登记。" % (who.label, sub))


def check_bash(data, who, policy, risk, soft, command=None):
    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command") or "" if command is None else command
    root = project_root(data)
    verdict = Verdict()
    if policy.get("mode") == "pool":
        for found in (check_identity_vars(data, who, command), check_pool_command(data, who, policy, root, command),
                      check_git(data, who, command, root)):
            if found:
                verdict.merge(found)
    if verdict.decision == "deny":
        return verdict
    ledger = ledger_write(command, data.get("cwd") or root)
    if ledger:
        verdict.merge(check_ledger(data, who, policy, root, ledger))
        if verdict.decision == "deny":
            return verdict
    parsed = fc.parse(command, data.get("cwd") or root)
    if parsed.error and parsed.mentions:
        return verdict.merge(refuse(who, data, "命令里用到飞书命令行，但%s；请改成单条简单命令。" % parsed.error))
    hidden = [o for o in parsed.opaque if "飞书" in o or parsed.mentions]
    if hidden:
        return verdict.merge(refuse(who, data, "看不到实际执行的飞书命令（%s）；请直接写出单条命令。" % "；".join(hidden)))
    if not parsed.invocations:
        return verdict
    rewritable = (parsed.simple and len(parsed.invocations) == 1 and data.get("tool_name") == "Bash"
                  and "$" not in command and "`" not in command)
    for inv in parsed.invocations:
        c = fc.classify(inv, risk)
        if inv.cli == "lark":
            v, new_args = check_lark(inv, c, who, policy, data, soft, rewritable)
        else:
            v, new_args = check_meegle(inv, c, who, data, soft, rewritable)
        verdict.merge(v)
        if new_args is not None and v.decision in ("allow", "ask"):
            updated = dict(tool_input)
            updated["command"] = shlex.join(inv.prefix + new_args)
            verdict.updated = updated
    if verdict.decision == "deny":
        verdict.updated = None
    return verdict


# --------------------------------------------------------------- files
EDIT_WINDOW = datetime.timedelta(minutes=30)


def note_edit(root, rel, who, data, soft):
    """Pool mode: remind both sides when two sessions change the same file within 30 minutes."""
    me = who.name or "%s:%s" % (who.role, (data.get("session_id") or "")[:8])
    path = os.path.join(pool.state_dir(root), "edits.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            edits = fc.load_json(path, {}) or {}
            at = pool.now()
            fresh = {}
            for k, rows in edits.items():
                keep = [r for r in rows if (pool.parse_time(r.get("at")) or at) > at - EDIT_WINDOW]
                if keep:
                    fresh[k] = keep
            others = sorted({r["who"] for r in fresh.get(rel, []) if r.get("who") != me})
            if others:
                soft.append("%s 在 30 分钟内也被 %s 改过；先和对方对一下，别互相覆盖。" % (rel, "、".join(others)))
            fresh.setdefault(rel, [])
            fresh[rel] = [r for r in fresh[rel] if r.get("who") != me] + [{"who": me, "at": pool.stamp(at)}]
            pool.save_json(path, fresh)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


FOLD_CASE = sys.platform == "darwin"             # macOS volumes ignore case unless formatted otherwise


def resolved(path):
    """The real path, symlinks followed; a path that does not exist yet keeps its missing tail."""
    head, tail = os.path.normpath(path), []
    while head and not os.path.exists(head) and os.path.dirname(head) != head:
        head, part = os.path.dirname(head), os.path.basename(head)
        tail.insert(0, part)
    return os.path.join(os.path.realpath(head), *tail) if tail else os.path.realpath(head)


def inside(full, root):
    if FOLD_CASE:
        full, root = full.lower(), root.lower()
    return full == root or full.startswith(root + os.sep)


def path_matches(rel, patterns):
    if FOLD_CASE:
        return matches(rel.lower(), [p.lower() for p in patterns])
    return matches(rel, patterns)


def relative(full, root):
    if not inside(full, root):
        return None
    return full[len(root) + 1:].replace(os.sep, "/") if len(full) > len(root) else ""


def check_path(data, who, policy, soft):
    """Rules hold for the path as written and for where it leads after symlinks: a protected path
    that is a symlink stays protected, and a link into a protected path is protected too."""
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        return Verdict()
    lex_root, real_root = project_root(data), os.path.realpath(project_root(data))
    lexical = os.path.normpath(path if os.path.isabs(path) else os.path.join(lex_root, path))
    full = resolved(lexical)
    rels = []
    for r in (relative(lexical, lex_root), relative(full, real_root), relative(lexical, real_root)):
        if r is not None and r not in rels:
            rels.append(r)
    if not rels:
        tmpdir = os.environ.get("TMPDIR", "")
        if full.startswith(TMP_PREFIXES) or (tmpdir and full.startswith(os.path.realpath(tmpdir))):
            return Verdict()
        if not who.researcher:
            soft.append("（%s）写到了项目目录和临时目录以外：%s；确认这是该写的地方。" % (who.label, path))
        return Verdict()
    rel = rels[0]
    if any(path_matches(r, policy["h_paths"]) for r in rels):
        if who.role in policy["h_writers"]:
            if data.get("permission_mode") in pool.BYPASS_MODES:
                return Verdict("deny", "评测协议用到的文件改动要研究员确认；当前是 %s 模式，确认框弹不出来，请研究员自己改。" % data["permission_mode"])
            return Verdict("ask", "评测协议用到的文件（评测脚本、评测集和接口），改动要研究员确认。")
        return Verdict("deny", "评测协议用到的文件（评测脚本、评测集和接口）在优化过程中保持不变；只有质量序列能提出修改，并且要研究员签字。")
    if policy.get("mode") == "pool":
        try:
            note_edit(project_root(data), rel, who, data, soft)
        except Exception:                          # a soft check never blocks
            pass
    soft_owned = policy.get("soft_owned") or {}
    owned = False
    for r in rels:                              # each path: a soft-owned pattern first, then the first owned one
        if any(path_matches(r, [pattern]) for pattern in soft_owned):
            continue
        for pattern, owners in policy["owned_paths"].items():
            if path_matches(r, [pattern]):
                if who.role not in owners:
                    owner = "、".join(owners) if owners else "研究员本人（改插件或任务文件后重跑 init）"
                    return Verdict("deny", "%s 只能由 %s 修改。" % (pattern, owner))
                owned = True
                break
    for pattern, owners in soft_owned.items():
        if any(path_matches(r, [pattern]) for r in rels):
            if who.role not in owners and not who.researcher:
                soft.append("%s 照规范由 %s 提出修改；改了要在交回里写明原因。" % (pattern, "、".join(owners) or "研究员本人"))
            return Verdict()
    if owned:
        return Verdict()
    if path_matches(rel, policy["x_paths"]) and who.role not in policy["x_writers"]:
        if not who.researcher:
            soft.append("可改范围里的代码照规范只由工程序列修改；改了要在交回里写明。")
        return Verdict()
    if path_matches(rel, who.pol.get("write_paths", [])):
        return Verdict("allow", "在 %s 的写入范围内" % who.label)
    if not who.researcher:
        soft.append("（%s）的写入范围是：%s；%s 不在里面，确认是你该改的。" % (
            who.label, "、".join(who.pol.get("write_paths", [])) or "无", rel))
    return Verdict()


def check_agent(data, who, policy, soft):
    tool_input = data.get("tool_input") or {}
    if tool_input.get("name"):
        return Verdict("deny", "派子智能体时不要带 name：开着 agent teams 时，带 name 的派发会变成队员。去掉 name 重试。")
    target = tool_input.get("subagent_type") or "general-purpose"
    if policy.get("mode") == "pool":
        if target == policy.get("scribe_role"):
            fixed = (policy["agents"].get(target) or {}).get("fixed_session") or target
            return Verdict("deny", "会话池模式下不用 Agent 工具派书记员：登记请求发消息给 %s。" % fixed)
        if target in policy["agents"]:
            soft.append("会话池模式下派活给 %s 的会话发消息（先跑 route.py pool --role %s），不要用 Agent 工具另派一个池外的实例。" % (target, target))
        return Verdict()
    spawn = who.pol.get("spawn", ())
    if target not in spawn:
        soft.append("（%s）照派发图只派：%s。跨序列的包、送审包和决策包写成草稿交回，由入口转交。" % (who.label, "、".join(spawn) or "无"))
    tpol = policy["agents"].get(target)
    if not tpol:
        return Verdict()
    session = None
    if tpol["rank"] == 0 and not tpol.get("fixed_session"):
        m = re.search(r"会话名[：:]\s*(%s-p0-\d+)(?![\w-])" % re.escape(tpol["abbrev"]), tool_input.get("prompt") or "")
        if m:
            session = m.group(1)
        else:
            soft.append("派 +0 时，在提示里单独写一行“会话名：%s-p0-<需求包的工作项 ID>”；还没有工作项的，先请书记员登记。" % tpol["abbrev"])
    with instances.locked(project_root(data)) as state:
        instances.prune(state)
        running = instances.count(state, target)
        if running >= tpol["limit"]:
            focus = instances.focus_session(state, target)
            if target in policy.get("hard_limits", ()):
                return Verdict("deny", "%s 已有 %d 个在跑，上限 %d：书记员同时只能有一个，免得两处同时写台账。等它交回再派。" % (
                    target, running, tpol["limit"]))
            if focus:
                soft.append("研究员正在和 %s 开聚焦会话（%s）；给它的新信息用 route.py notify %s 留在收件箱。" % (target, focus, target))
            else:
                soft.append("%s 已有 %d 个在跑，上限 %d；照规范把任务写进交回的内容，交回上一级排队。" % (target, running, tpol["limit"]))
        if session and instances.name_in_use(state, session):
            soft.append("会话名 %s 已经在跑；同一个工作项只派一个 +0。" % session)
        instances.reserve(state, target, session, data.get("tool_use_id"))
    return Verdict()


def check_ask(data, who, policy, soft):
    """Pool mode: a turn another session's message started may have nobody to answer a question."""
    if policy.get("mode") != "pool" or who.origin != "claim":
        return Verdict()
    st = pool.load_session(project_root(data), data.get("session_id") or "")
    if st.get("turn_origin") == "message":
        if (policy["agents"].get(who.role) or {}).get("rank") == 2:
            soft.append("这一轮是别的会话的消息叫起来的，研究员可能不在：要研究员拍板的写【%s】发给入口，或者写成决策包，不要停在这里等回答。" % (
                (policy.get("msg") or {}).get("decide", "需拍板")))
        else:
            soft.append("这一轮是别的会话的消息叫起来的，研究员可能不在：要拍板的事写进交给上一级的交付包，不要停在这里等回答。")
    return Verdict()


def log(root, data, who, verdict, soft):
    try:
        path = os.path.join(root, ".claude", "logs", "guard.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rec = {
            "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "session_id": data.get("session_id"), "tool": data.get("tool_name"),
            "agent_type": data.get("agent_type"), "has_agent_id": bool(data.get("agent_id")),
            "role": who.role, "origin": who.origin, "session_name": who.name,
            "desk": os.environ.get(pool.DESK_ENV) or None, "permission_mode": data.get("permission_mode"),
            "decision": verdict.decision, "reason": "；".join(verdict.reasons)[:200],
            "soft_hits": [h[:160] for h in soft],
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def touch(root, data, who):
    """Pool mode: a tool call means the session is working; remember when. A subagent's call
    (a background one, say) also ends a wait that was its own or nobody's (pool.end_wait); the
    session's own call, other than the one a permission prompt was raised for, means the prompt
    was answered."""
    sid = data.get("session_id") or ""
    stamp = pool.stamp()

    def change(st):
        st["last_tool_at"] = stamp
        if data.get("agent_id"):
            pool.end_wait(st, data["agent_id"])
            return
        if not str(st.get("status", "")).startswith("waiting"):
            st["status"] = "busy"
            st["status_at"] = stamp
        elif st.get("waiting_tool_use_id") != data.get("tool_use_id"):
            pool.end_wait(st)
    try:
        pool.update_session(root, sid, change)
    except OSError:
        pass


def trusted_scripts(root):
    """Plugin scripts in .claude/agent-org/bin/ that still match the manifest."""
    m = fc.load_json(os.path.join(root, ".claude", "agent-org", "manifest.json"), {}) or {}
    out = set()
    for rel, digest in (m.get("files") or {}).items():
        if rel.startswith(".claude/agent-org/bin/") and rel.endswith(".py"):
            full = os.path.join(root, rel)
            try:
                with open(full, "rb") as f:
                    if hashlib.sha256(f.read()).hexdigest() == digest:
                        out.add(os.path.normpath(full))
            except OSError:
                pass
    return out


def evaluate(data):
    """(verdict, soft reminders) for one PreToolUse call."""
    root = project_root(data)
    fc.TRUSTED = trusted_scripts(root)
    policy = fc.load_json(os.path.join(HERE, "policy.json"))
    if not policy:
        raise RuntimeError("读不到 policy.json")
    if policy.get("version") != POLICY_VERSION:
        raise RuntimeError("policy.json 是第 %s 版，这个 guard.py 要第 %d 版：两者不配套，硬规则会悄悄变样，所以先全部拒绝。"
                           "请研究员在 Claude 以外的终端里把 .claude/hooks/ 下的 guard.py.new 和 policy.json.new 一起合并，"
                           "再运行 setup.py init --apply" % (policy.get("version"), POLICY_VERSION))
    risk = fc.load_json(os.path.join(HERE, "lark-risk.json"), {"commands": {}})
    who = pool.identify(data, policy, root)
    soft = []
    tool = data.get("tool_name")
    if tool == "Bash":
        verdict = check_bash(data, who, policy, risk, soft)
    elif tool == "Monitor" or str(tool).endswith("run_in_terminal"):   # shell commands outside the Bash tool
        command = (data.get("tool_input") or {}).get("command")
        verdict = check_bash(data, who, policy, risk, soft, command) if command else Verdict()
    elif tool in ("Edit", "Write", "NotebookEdit"):
        verdict = check_path(data, who, policy, soft)
    elif tool in ("Agent", "Task"):
        verdict = check_agent(data, who, policy, soft)
    elif tool == "AskUserQuestion":
        verdict = check_ask(data, who, policy, soft)
    else:
        verdict = Verdict()
    if policy.get("mode") == "pool":
        touch(root, data, who)
    log(root, data, who, verdict, soft)
    return verdict, soft


def main():
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise ValueError("hook 输入不是 JSON 对象")
        verdict, soft = evaluate(data)
        out = {"hookEventName": "PreToolUse"}
        if verdict.decision:
            out["permissionDecision"] = verdict.decision
            out["permissionDecisionReason"] = "；".join(verdict.reasons) or verdict.decision
            if verdict.updated is not None:
                out["updatedInput"] = verdict.updated
        if soft:
            out["additionalContext"] = "\n".join("提醒：" + s for s in soft)
        if len(out) > 1:
            sys.stdout.write(json.dumps({"hookSpecificOutput": out}, ensure_ascii=True) + "\n")
            sys.stdout.flush()
    except Exception as e:  # fail closed: exit 2 blocks; any other exit code would let the call through
        try:
            sys.stderr.write("[guard] 检查出错，按拒绝处理：%r\n" % e)
        finally:
            sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
