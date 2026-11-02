#!/usr/bin/env python3
"""PreToolUse guard for a project set up by the agent-org plugin.

Reads the hook input JSON on stdin and answers Bash, Edit, Write, NotebookEdit
and Agent calls with allow / ask / deny, based on policy.json and
lark-risk.json in this directory. Anything unexpected exits 2, which blocks the
call: Claude Code treats other non-zero exits as non-blocking, so failing open
must be avoided explicitly. Settings deny and ask rules still apply after this
hook answers allow.
"""
import datetime
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

TMP_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")


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


def refuse(is_main, reason):
    """Ask in the researcher's own session; deny inside subagents."""
    return Verdict("ask" if is_main else "deny", reason)


def project_root(data):
    return os.path.normpath(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())


def resolve_role(data, policy):
    agent = data.get("agent_type") or ""
    is_main = not data.get("agent_id")
    agents = policy["agents"]
    if agent in agents:
        return agent, agents[agent], is_main
    if is_main:
        main = policy["main_agent"]
        return main, agents[main], True
    return agent or "unknown", policy["default_agent"], False


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


def check_lark(inv, c, name, pol, is_main, policy, parsed):
    key = c["key"]
    if c["help"]:
        return Verdict("allow", "查看帮助")
    if c["yes"]:
        return Verdict("deny", "带 --yes 的命令只能由研究员本人执行。不要自己加 --yes；把完整命令写进交回的内容。")
    if c["tier"] == "high-risk-write":
        return Verdict("deny", "%s 是 high-risk-write，只能由研究员本人执行。把完整命令写进交回的内容。" % key)
    if c["dry_run"]:
        return Verdict("allow", "--dry-run 只预览")
    ident = policy["identity"]
    domain = c["pos"][0] if c["pos"] else ""
    if domain in ident["user_domains"] and c["as"] != "user":
        return Verdict("deny", "%s 命令要显式带 --as user：知识库、文档和云空间都用研究员身份。" % domain)
    if key in ident["bot_commands"] and c["as"] != "bot":
        return Verdict("deny", "%s 要显式带 --as bot：收发消息和订阅事件用机器人身份。" % key)
    if c["tier"] == "read":
        return Verdict("allow", "只读命令")
    if key == "docs +update" and fc.last(c["flags"], "--command") == "append":
        text = content_of(inv, c["flags"])
        if text is None:
            return Verdict("deny", "看不到要追加的正文（比如从文件重定向传入），没法核对会话标签。书记员用 ledger.py append；手工跑时用 heredoc 或内联传入 --content。")
        if pol["tag"] not in first_line(text):
            return Verdict("deny", "追加的交接记录第一行要带会话标签 %s；书记员用 ledger.py append。手工跑时用 heredoc 传 --content -，或内联传入（lark-cli 只读当前目录、/tmp、~/files 下的 @文件）。" % pol["tag"])
    if key in pol.get("lark_write", ()):
        return Verdict("allow", "%s 在 %s 的写入白名单里" % (key, name))
    parents = pol.get("docs_create_parents")
    if key == "docs +create" and parents and fc.last(c["flags"], "--parent-token") in parents:
        return Verdict("allow", "在允许的父节点下新建文档")
    if key in pol.get("lark_ask", ()):
        return Verdict("ask", "%s 要研究员确认（%s）" % (key, name))
    return refuse(is_main, "（%s）不能执行写命令 %s。要写进飞书的内容先写成草稿，请书记员登记。" % (name, key))


def check_meegle(inv, c, name, pol, is_main, rewritable):
    key = c["key"]
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
            return Verdict("deny", "评论的 --params 不是合法 JSON，没法补会话标签。"), None
        if tagged != new_args and not rewritable:
            return Verdict("deny", "评论开头缺会话标签 %s；命令不是单条简单命令，没法自动补，请在内容开头加上后重试。" % pol["tag"]), None
        new_args = tagged
    if rewritable and "--envelope" not in c["flags"]:
        new_args.append("--envelope")
    updated = new_args if new_args != inv.args else None
    if key in pol.get("meegle_write", ()):
        return Verdict("allow", "%s 在 %s 的写入白名单里" % (key, name)), updated
    if key in pol.get("meegle_ask", ()):
        return Verdict("ask", "%s 要研究员确认（%s）" % (key, name)), updated
    return refuse(is_main, "（%s）不能执行写命令 %s。工作项、字段、状态和文档只由书记员写；先写成草稿，请书记员登记。" % (name, key)), None


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


def check_bash(data, name, pol, is_main, policy, risk):
    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command") or ""
    root = project_root(data)
    ledger = ledger_write(command, data.get("cwd") or root)
    if ledger and not pol.get("ledger_write"):
        return refuse(is_main, "（%s）不能运行 ledger.py %s：工作项、字段、状态和文档只由书记员写。写成草稿，请书记员登记。" % (name, ledger))
    parsed = fc.parse(command, data.get("cwd") or root)
    if parsed.error and parsed.mentions:
        return refuse(is_main, "命令里用到飞书命令行，但%s；请改成单条简单命令。" % parsed.error)
    hidden = [o for o in parsed.opaque if "飞书" in o or parsed.mentions]
    if hidden:
        return refuse(is_main, "看不到实际执行的飞书命令（%s）；请直接写出单条命令。" % "；".join(hidden))
    if not parsed.invocations:
        return Verdict()
    rewritable = (parsed.simple and len(parsed.invocations) == 1
                  and "$" not in command and "`" not in command)
    verdict = Verdict()
    for inv in parsed.invocations:
        c = fc.classify(inv, risk)
        if inv.cli == "lark":
            verdict.merge(check_lark(inv, c, name, pol, is_main, policy, parsed))
        else:
            v, new_args = check_meegle(inv, c, name, pol, is_main, rewritable)
            verdict.merge(v)
            if new_args is not None and v.decision in ("allow", "ask"):
                updated = dict(tool_input)
                updated["command"] = shlex.join(inv.prefix + new_args)
                verdict.updated = updated
    if verdict.decision == "deny":
        verdict.updated = None
    return verdict


# --------------------------------------------------------------- files
def check_path(data, name, pol, is_main, policy):
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        return Verdict()
    root = project_root(data)
    full = os.path.normpath(path if os.path.isabs(path) else os.path.join(root, path))
    if not (full == root or full.startswith(root + os.sep)):
        tmpdir = os.environ.get("TMPDIR", "")
        if full.startswith(TMP_PREFIXES) or (tmpdir and full.startswith(tmpdir)):
            return Verdict()
        return Verdict() if is_main else Verdict("deny", "子智能体只能写本项目目录和临时目录。")
    rel = os.path.relpath(full, root).replace(os.sep, "/")
    if matches(rel, policy["h_paths"]):
        if name in policy["h_writers"]:
            return Verdict("ask", "评测协议用到的文件（评测脚本、评测集和接口），改动要研究员确认。")
        return Verdict("deny", "评测协议用到的文件（评测脚本、评测集和接口）在优化过程中保持不变；只有质量序列能提出修改，并且要研究员签字。")
    for pattern, owners in policy["owned_paths"].items():
        if matches(rel, [pattern]):
            if name in owners:
                return Verdict()
            who = "、".join(owners) if owners else "研究员本人"
            return Verdict("deny", "%s 只能由 %s 提出修改。" % (pattern, who))
    if matches(rel, policy["x_paths"]) and name not in policy["x_writers"]:
        return refuse(is_main, "可改范围里的代码只由工程序列修改。")
    if matches(rel, pol.get("write_paths", [])):
        return Verdict("allow", "在 %s 的写入范围内" % name)
    if is_main:
        return Verdict()
    return Verdict("deny", "（%s）只能写：%s。" % (name, "、".join(pol.get("write_paths", [])) or "无"))


def check_agent(data, name, pol, is_main, policy):
    tool_input = data.get("tool_input") or {}
    if tool_input.get("name"):
        return Verdict("deny", "派子智能体时不要带 name：开着 agent teams 时，带 name 的派发会变成队员。去掉 name 重试。")
    target = tool_input.get("subagent_type") or "general-purpose"
    if target not in pol.get("spawn", ()):
        allowed = "、".join(pol.get("spawn", ())) or "无"
        return refuse(is_main, "（%s）只能派：%s。跨序列的包、送审包和决策包写成草稿交回，由入口转交。" % (name, allowed))
    tpol = policy["agents"].get(target)
    if not tpol:
        return Verdict()
    session = None
    if tpol["rank"] == 0 and not tpol.get("fixed_session"):
        m = re.search(r"会话名[：:]\s*(%s-p0-\d+)(?![\w-])" % re.escape(tpol["abbrev"]),
                      tool_input.get("prompt") or "")
        if not m:
            return Verdict("deny", "派 +0 时，在提示里单独写一行“会话名：%s-p0-<需求包的工作项 ID>”；还没有工作项的，先请书记员登记。" % tpol["abbrev"])
        session = m.group(1)
    with instances.locked(project_root(data)) as state:
        instances.prune(state)
        running = instances.count(state, target)
        if running >= tpol["limit"]:
            focus = instances.focus_session(state, target)
            if focus:
                return Verdict("deny", "研究员正在和 %s 开聚焦会话（%s）。不要另派；用 route.py notify %s 把待办留给它，聚焦会话下一轮对话时会看到。" % (target, focus, target))
            return Verdict("deny", "%s 已有 %d 个在跑，上限 %d；把任务写进交回的内容，交回上一级排队。" % (target, running, tpol["limit"]))
        if session and instances.name_in_use(state, session):
            return Verdict("deny", "会话名 %s 已经在跑；同一个工作项只派一个 +0。" % session)
        instances.reserve(state, target, session, data.get("tool_use_id"))
    return Verdict()


def log(root, data, name, verdict):
    try:
        path = os.path.join(root, ".claude", "logs", "guard.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rec = {
            "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "session_id": data.get("session_id"), "tool": data.get("tool_name"),
            "agent_type": data.get("agent_type"), "has_agent_id": bool(data.get("agent_id")),
            "role": name, "decision": verdict.decision,
            "reason": "；".join(verdict.reasons)[:200],
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
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
    fc.TRUSTED = trusted_scripts(project_root(data))
    policy = fc.load_json(os.path.join(HERE, "policy.json"))
    if not policy:
        raise RuntimeError("读不到 policy.json")
    risk = fc.load_json(os.path.join(HERE, "lark-risk.json"), {"commands": {}})
    name, pol, is_main = resolve_role(data, policy)
    tool = data.get("tool_name")
    if tool == "Bash":
        verdict = check_bash(data, name, pol, is_main, policy, risk)
    elif tool in ("Edit", "Write", "NotebookEdit"):
        verdict = check_path(data, name, pol, is_main, policy)
    elif tool in ("Agent", "Task"):
        verdict = check_agent(data, name, pol, is_main, policy)
    else:
        verdict = Verdict()
    log(project_root(data), data, name, verdict)
    return verdict


def main():
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise ValueError("hook 输入不是 JSON 对象")
        verdict = evaluate(data)
        if verdict.decision:
            out = {"hookEventName": "PreToolUse", "permissionDecision": verdict.decision,
                   "permissionDecisionReason": "；".join(verdict.reasons) or verdict.decision}
            if verdict.updated is not None:
                out["updatedInput"] = verdict.updated
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
