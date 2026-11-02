"""Parse Bash command text and classify lark-cli / meegle invocations.

Shared by guard.py (PreToolUse) and audit.py (PostToolUse). Python 3.9
standard library only. This is a guardrail that reads command text; it cannot
see what a script does internally, so it is not a security boundary.
"""
import fnmatch
import itertools
import json
import os
import re
import shlex

CLI_BASENAMES = {"lark-cli": "lark", "meegle": "meegle"}
NPX_PACKAGES = (("@larksuite/cli", "lark"), ("@lark-project/meegle", "meegle"))
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
INTERPRETERS = {"python", "python3", "node", "ruby", "perl", "deno", "bun", "php"}
PASS_WRAPPERS = {"command", "builtin", "exec", "nohup", "noglob", "time"}
RUNNERS = {"uv", "poetry", "pipenv", "pdm", "hatch", "rye", "pixi", "conda", "mamba", "micromamba"}
RUNNER_GLOBAL_VALUES = {"--directory", "--project", "--config-file", "--cache-dir", "-C", "-P", "--manifest-path",
                        "--data-dir", "--env", "-e"}
VALUE_OPTIONS = {
    "sudo": ("-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T"),
    "caffeinate": ("-w", "-t"), "arch": ("-arch",), "ionice": ("-c", "-n", "-p", "-P", "-u"),
    "chrt": (), "taskset": ("-c", "--cpu-list"), "stdbuf": (), "doas": ("-u", "-C"),
    "run": ("-n", "--name", "-p", "--prefix", "--python", "--with", "--project", "--directory", "--env-file",
            "-e", "--environment", "--group", "--extra", "--from", "--cwd"),
}
SHELL_KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "!", "{", "}", "done", "fi"}
SEPARATORS = {";", ";;", "&&", "||", "|", "|&", "&", "\n", "(", ")"}
REDIRECTS = {"<", ">", ">>", "&>", ">&", "<&", "<<", "<<<", "<>", ">|"}
OPERATORS = sorted(SEPARATORS | REDIRECTS, key=len, reverse=True)
PUNCT = set(";&|()<>\n")
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# A CLI name as a command, not as data: ids['meegle'], .get("meegle"), {"meegle": ...} and
# "meegle" in ids are dictionary keys, meegle.json is a file name.
_NOT_KEY_BEFORE = r"(?<![\w)\]]\[['\"])(?<!get\(['\"])(?<!default\(['\"])"
_NOT_KEY_AFTER = r"(?![\w.-])(?!['\"]\s*:)(?!['\"]\s+(?:not\s+)?in\b)"
MENTION_RE = re.compile(r"(?<![\w.-])%s(?:lark-cli|meegle)%s|@larksuite/cli|@lark-project/meegle" % (_NOT_KEY_BEFORE, _NOT_KEY_AFTER))
HEREDOC_OP_RE = re.compile(r"<<(-?)[ \t]*(\\?)(['\"]?)([A-Za-z_][\w.-]*)\3")
HEREDOC_MARK_RE = re.compile(r"__AOHD(\d+)__")
_heredoc_ids = itertools.count(1)
MAX_DEPTH = 4
MAX_SCAN_BYTES = 2 * 1024 * 1024


class Invocation:
    def __init__(self, cli, args, cwd, prefix, stdin=None):
        self.cli = cli          # "lark" or "meegle"
        self.args = args        # arguments after the CLI name
        self.cwd = cwd          # best-effort working directory
        self.prefix = prefix    # tokens up to and including the CLI name
        self.stdin = stdin      # text on its standard input when visible: its here-document, here-string or pipe


class Parsed:
    def __init__(self):
        self.invocations = []
        self.mentions = False
        self.opaque = []        # reasons we could not see into something
        self.trusted = []       # plugin scripts that call Feishu themselves and log their writes
        self.error = None
        self.simple = True      # one plain command: safe to rebuild with shlex.join
        self.bodies = {}        # here-document bodies by marker number: (body, quoted)

    @property
    def heredocs(self):
        return [b for b, _ in self.bodies.values()]


def split_heredocs(command):
    """Find here-document operators outside quotes and comments, and take out their bodies.

    Returns (text, bodies). In text each operator's delimiter is replaced by a marker
    word __AOHD<n>__, so later steps (comment stripping, joining lines, replacing
    $(...), nested parsing) still know which body belongs to which operator; bodies
    maps n to (body, quoted). quoted is True when the delimiter was quoted or escaped
    (<<'EOF', <<"EOF", <<\\EOF): the shell then does not expand $(...) in the body.
    Operators inside $(...) and backticks count, also within double quotes; a body
    starts after the next newline that is not inside quotes.
    """
    out, bodies, pending, stack = [], {}, [], []
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        top = stack[-1] if stack else None
        if top == "'":
            out.append(c)
            if c == "'":
                stack.pop()
            i += 1
            continue
        if top == '"':
            if c == "\\" and i + 1 < n:
                out.append(command[i:i + 2])
                i += 2
                continue
            if c == '"':
                stack.pop()
            elif command.startswith("$(", i):
                stack.append(["$(", 0, command.startswith("$((", i)])
                out.append("$(")
                i += 2
                continue
            elif c == "`":
                stack.append(["`"])
            out.append(c)
            i += 1
            continue
        # unquoted: top level, inside $(...) or inside backticks
        if c == "\\" and i + 1 < n:
            out.append(command[i:i + 2])
            i += 2
            continue
        if c in "'\"":
            stack.append(c)
        elif c == "`":
            if top and top[0] == "`":
                stack.pop()
            else:
                stack.append(["`"])
        elif command.startswith("$(", i):
            stack.append(["$(", 0, command.startswith("$((", i)])
            out.append("$(")
            i += 2
            continue
        elif command.startswith("((", i) and (i == 0 or command[i - 1] in " \t\n;&|("):
            stack.append(["$(", 1, True])              # (( x << n )): arithmetic, closed by ))
            out.append("((")
            i += 2
            continue
        elif c == "(" and top and top[0] == "$(":
            top[1] += 1
        elif c == ")" and top and top[0] == "$(":
            if top[1]:
                top[1] -= 1
            else:
                stack.pop()
        elif c == "#" and (i == 0 or command[i - 1] in " \t\n;&|()<>"):
            j = command.find("\n", i)
            j = n if j < 0 else j
            out.append(command[i:j])
            i = j
            continue
        elif command.startswith("<<<", i):
            out.append("<<<")
            i += 3
            continue
        elif command.startswith("<<", i) and not (top and top[0] == "$(" and top[2]):
            m = HEREDOC_OP_RE.match(command, i)
            if m and not HEREDOC_MARK_RE.fullmatch(m.group(4)):
                k = next(_heredoc_ids)
                pending.append((k, m.group(4), m.group(1) == "-", bool(m.group(2) or m.group(3))))
                out.append("<<__AOHD%d__" % k)
                i = m.end()
                continue
        elif c == "\n" and pending:
            out.append(c)
            i = _read_bodies(command, i + 1, pending, bodies)
            continue
        out.append(c)
        i += 1
    for k, _, _, quoted in pending:            # no newline after the operator: an empty body
        bodies[k] = ("", quoted)
    return "".join(out), bodies


def _read_bodies(command, pos, pending, bodies):
    """pos is just after the newline that ends the operators' line; returns where the command goes on."""
    n = len(command)
    for k, delim, dash, quoted in pending:
        lines = []
        while pos < n:
            end = command.find("\n", pos)
            line = command[pos:] if end < 0 else command[pos:end]
            pos = n if end < 0 else end + 1
            if (line.lstrip("\t") if dash else line).rstrip("\r") == delim:
                break
            lines.append(line)
        bodies[k] = ("\n".join(lines), quoted)
    pending.clear()
    return pos


def heredoc_of(tokens, i, bodies):
    """The body for the << at tokens[i], or None."""
    if i + 1 < len(tokens):
        m = HEREDOC_MARK_RE.fullmatch(tokens[i + 1])
        if m and int(m.group(1)) in bodies:
            return bodies[int(m.group(1))][0]
    return None


def strip_heredocs(command):
    """Remove here-document bodies (kept separately) so they are not parsed."""
    text, bodies = split_heredocs(command)
    return text, [b for b, _ in bodies.values()]


def strip_comments(text):
    """Drop shell comments: an unquoted # at the start of a word, up to (not including) the newline.

    shlex's own comment handling would also eat the newline, joining the next
    command to this one, and would start a comment in the middle of a word (issue#12).
    """
    out, i, n, q = [], 0, len(text), None
    while i < n:
        c = text[i]
        if q:
            out.append(c)
            if c == q:
                q = None
            elif c == "\\" and q == '"' and i + 1 < n:
                out.append(text[i + 1])
                i += 1
        elif c in "'\"":
            q = c
            out.append(c)
        elif c == "\\" and i + 1 < n:
            out.append(c + text[i + 1])
            i += 1
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n;&|()<>"):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


# Escaped or quoted punctuation (find ... \; or ';', find \( ... \)) is a word, not an operator.
_SENTINEL = {ch: chr(0xE001 + k) for k, ch in enumerate(";&|()<>")}
_UNSENTINEL = {v: k for k, v in _SENTINEL.items()}


def _protect_punct(text):
    out, i, n, q = [], 0, len(text), None
    while i < n:
        c = text[i]
        if q:
            out.append(c)
            if c == q:
                q = None
            elif c == "\\" and q == '"' and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            i += 1
            continue
        if c == "\\" and i + 1 < n and text[i + 1] in _SENTINEL:
            out.append(_SENTINEL[text[i + 1]])
            i += 2
            continue
        if c in "'\"":
            j = text.find(c, i + 1)
            inner = text[i + 1:j] if j > 0 else ""
            before_ok = i == 0 or text[i - 1] in " \t\n"
            after_ok = j > 0 and (j + 1 == n or text[j + 1] in " \t\n")
            if inner and all(ch in _SENTINEL for ch in inner) and before_ok and after_ok:
                out.append("".join(_SENTINEL[ch] for ch in inner))
                i = j + 1
                continue
            q = c
        out.append(c)
        i += 1
    return "".join(out)


def _split_punct(tok):
    parts, i = [], 0
    while i < len(tok):
        for op in OPERATORS:
            if tok.startswith(op, i):
                parts.append(op)
                i += len(op)
                break
        else:
            parts.append(tok[i])
            i += 1
    return parts


def tokenize(command):
    lex = shlex.shlex(_protect_punct(strip_comments(command)), posix=True, punctuation_chars=";&|()<>\n")
    lex.commenters = ""
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    tokens = []
    for tok in lex:
        if tok and all(c in PUNCT for c in tok):
            tokens.extend(_split_punct(tok))
        elif tok and all(c in _UNSENTINEL for c in tok):
            tokens.append("".join("\\" + _UNSENTINEL[c] for c in tok))      # a literal \; \( ...
        else:
            tokens.append("".join(_UNSENTINEL.get(c, c) for c in tok))
    return tokens


def substitutions(text):
    """Return the inner text of $(...) and `...` found in a token."""
    found, i = [], 0
    while i < len(text):
        if text.startswith("$(", i):
            depth, j = 1, i + 2
            while j < len(text) and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            found.append(text[i + 2:j - 1] if depth == 0 else text[i + 2:])
            i = j
        elif text[i] == "`":
            j = text.find("`", i + 1)
            found.append(text[i + 1:] if j < 0 else text[i + 1:j])
            i = len(text) if j < 0 else j + 1
        else:
            i += 1
    return found


def _segments(tokens):
    segs, cur, piped, nxt_piped = [], [], [], False
    for t in tokens:
        if t in SEPARATORS:
            if cur:
                segs.append(cur)
                piped.append(nxt_piped)
                cur = []
            nxt_piped = t in ("|", "|&")
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
        piped.append(nxt_piped)
    return segs, piped


def _drop_redirects(seg):
    out, i, had = [], 0, False
    while i < len(seg):
        t = seg[i]
        if t in REDIRECTS:
            had = True
            if out and out[-1].isdigit():
                out.pop()
            i += 2
            continue
        out.append(t)
        i += 1
    return out, had


def seg_stdin(seg, bodies, cwd):
    """(here-document bodies, here-strings, file after <) attached to one command's tokens."""
    docs = [b for b in (heredoc_of(seg, i, bodies) for i, t in enumerate(seg) if t == "<<") if b is not None]
    strings = [seg[i + 1] for i, t in enumerate(seg[:-1]) if t == "<<<"]
    infile = next((seg[i + 1] for i, t in enumerate(seg[:-1]) if t == "<"), None)
    return docs, strings, infile


def pipe_output(argv, stdin_text, cwd):
    """What a command writes to a pipe, when it is plain to see: cat/head/tail of files or of
    their own standard input, echo/printf text. None otherwise."""
    if not argv:
        return None
    base = os.path.basename(argv[0])
    if base in ("cat", "head", "tail"):
        files = [a for a in argv[1:] if not a.startswith("-")]
        if not files:
            return stdin_text
        texts = [read_script(f, cwd) for f in files]
        return "\n".join(t for t in texts if t) or None
    if base in ("echo", "printf"):
        return " ".join(a for a in argv[1:] if not a.startswith("-"))
    return None


SCRIPT_MENTION_RE = re.compile(r"(?:^|[\s;&|(`'\"\[,/=])%s(?:lark-cli|meegle)%s|@larksuite/cli|@lark-project/meegle"
                               % (_NOT_KEY_BEFORE, _NOT_KEY_AFTER), re.M)


def read_script(path, cwd):
    """Text of a regular file of at most 2 MB, or None."""
    full = path if os.path.isabs(path) else os.path.join(cwd or ".", path)
    try:
        if os.path.isfile(full) and os.path.getsize(full) <= MAX_SCAN_BYTES:
            with open(full, encoding="utf-8", errors="ignore") as f:
                return f.read()
    except OSError:
        return None
    return None


def code_lines(text):
    """Source text without full-line # comments."""
    return "\n".join(l for l in (text or "").splitlines() if not l.lstrip().startswith("#"))


def mentions_cli(text):
    return bool(SCRIPT_MENTION_RE.search(code_lines(text)))


def _scan_file(path, cwd):
    """Does the script call lark-cli or meegle? Comment lines and file names like meegle.json do not count."""
    text = read_script(path, cwd)
    return text is not None and "\0" not in text and mentions_cli(text)


def _unwrap(seg):
    i = 0
    while i < len(seg):
        t = seg[i]
        base = os.path.basename(t)
        if ASSIGN_RE.match(t) or base in PASS_WRAPPERS or t in SHELL_KEYWORDS:
            i += 1
        elif base == "env":
            i += 1
            while i < len(seg) and (seg[i].startswith("-") or ASSIGN_RE.match(seg[i])):
                i += 2 if seg[i] in ("-u", "--unset", "-C", "--chdir") else 1
        elif base == "timeout":
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 2 if seg[i] in ("-k", "--kill-after", "-s", "--signal") else 1
            i += 1
        elif base == "nice":
            i += 1
            if i < len(seg) and seg[i] == "-n":
                i += 2
            elif i < len(seg) and re.match(r"^-\d+$", seg[i]):
                i += 1
        elif base in ("stdbuf", "sudo", "doas", "caffeinate", "arch", "ionice", "chrt", "taskset"):
            takes = VALUE_OPTIONS.get(base, ())
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 2 if seg[i] in takes else 1
            if base in ("chrt", "taskset") and i < len(seg) and re.match(r"^[0-9a-fx,.-]+$", seg[i]):
                i += 1                                   # priority or CPU mask
        elif base in RUNNERS or base == "uvx":
            j = i + 1
            if base != "uvx":                            # uv --directory X run, poetry -C X run
                while j < len(seg) and seg[j].startswith("-"):
                    j += 2 if seg[j] in RUNNER_GLOBAL_VALUES else 1
                if j >= len(seg) or seg[j] != "run":
                    break
                j += 1
            takes = VALUE_OPTIONS.get("run", ())
            while j < len(seg) and seg[j].startswith("-"):
                if seg[j] == "--":
                    j += 1
                    break
                j += 2 if seg[j] in takes else 1
            i = j
        elif base == "xargs":
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                takes = seg[i] in ("-I", "-i", "-n", "-P", "-L", "-d", "-E", "-s", "-a")
                i += 2 if takes else 1
        else:
            break
    return seg[i:], seg[:i]


FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
FIND_END = {";", "+", "\\;"}
FIND_NAMES = {"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename"}
PYTHON_RE = re.compile(r"i?python[\d.]*$")


def python_target(args):
    """What `python [options] ...` runs, from the arguments after the interpreter:
    ("code", CODE, rest), ("module", NAME, rest), ("script", PATH, rest), or
    ("stdin", "-" or None, rest). Option clusters count: -IBc CODE, -Bm mod, -BW ignore."""
    k = 0
    while k < len(args):
        a = args[k]
        if a == "-" or not a.startswith("-"):
            break
        if a == "--":
            k += 1
            break
        if a.startswith("--"):
            k += 2 if a == "--check-hash-based-pycs" else 1
            continue
        letters, skip = a[1:], 1
        for j, ch in enumerate(letters):
            if ch in "cmWX":
                value = letters[j + 1:] or (args[k + 1] if k + 1 < len(args) else "")
                skip = 1 if letters[j + 1:] else 2
                if ch == "c":
                    return "code", value, args[k + skip:]
                if ch == "m":
                    return "module", value, args[k + skip:]
                break
        k += skip
    rest = args[k:]
    if not rest or rest[0] == "-":
        return "stdin", rest[0] if rest else None, rest[1:]
    return "script", rest[0], rest[1:]


def shell_target(args):
    """What `sh/bash/zsh [options] ...` runs: ("command", STRING, rest) for -c, ("script", PATH, rest),
    or ("stdin", None, rest). Options are read left to right up to the first other word; each
    o or O in a cluster (-o, -euo, +O) takes the next word, --rcfile and --init-file take a value."""
    k, command, stdin = 0, False, False
    while k < len(args):
        a = args[k]
        if a == "--" or a == "-":
            k += 1
            stdin = stdin or a == "-"
            break
        if a in ("--rcfile", "--init-file"):
            k += 2
            continue
        if a.startswith("--"):
            k += 1
            continue
        if a[:1] in "-+" and len(a) > 1:
            letters = a[1:]
            command = command or (a[0] == "-" and "c" in letters)
            stdin = stdin or (a[0] == "-" and "s" in letters)
            k += 1 + letters.count("o") + letters.count("O")
            continue
        break
    rest = args[k:]
    if command:
        return "command", rest[0] if rest else "", rest[1:]
    if stdin or not rest:
        return "stdin", None, rest
    return "script", rest[0], rest[1:]


INLINE_FLAGS = {"node": ("-e", "--eval", "-p", "--print"), "bun": ("-e", "--eval", "-p", "--print"),
                "php": ("-r",), "perl": ("-e", "-E"), "ruby": ("-e",)}
INLINE_VALUES = {"node": ("-r", "--require", "--import", "--loader", "-C", "--conditions", "--env-file"),
                 "bun": ("-r", "--preload", "--cwd", "--env-file"), "php": ("-d", "-c", "-z"),
                 "perl": ("-I", "-M", "-m", "-x", "-C", "-F", "-i", "-l", "-0"), "ruby": ("-I", "-r", "-C", "-E", "-F", "-x")}


def inline_code(base, args):
    """The inline program of perl, ruby, node, bun, php or deno (perl -we CODE, node -pe CODE,
    node --eval=CODE, deno eval CODE), or None when the command runs a file."""
    if base == "deno":
        return args[args.index("eval") + 1] if "eval" in args and args.index("eval") + 1 < len(args) else None
    flags, values = INLINE_FLAGS.get(base, ()), INLINE_VALUES.get(base, ())
    k = 0
    while k < len(args):
        a = args[k]
        if not a.startswith("-") or a == "-":
            return None
        if a == "--":
            return None
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name in flags:
                return val if eq else (args[k + 1] if k + 1 < len(args) else "")
            k += 2 if name in values and not eq else 1
            continue
        if a in flags:
            return args[k + 1] if k + 1 < len(args) else ""
        letters = a[1:]
        for j, ch in enumerate(letters):              # perl -we, node -pe, ruby -rtime -e
            if "-" + ch in values:
                if not letters[j + 1:]:
                    k += 1                           # the value is the next word
                break
            if "-" + ch in flags:
                if base in ("node", "bun"):                # node -pe CODE: the code is the next word
                    return args[k + 1] if k + 1 < len(args) else ""
                return letters[j + 1:] or (args[k + 1] if k + 1 < len(args) else "")
        k += 1
    return None


def find_clauses(argv):
    """The commands that find's -exec / -execdir / -ok / -okdir clauses in argv run."""
    out, k = [], 0
    while k < len(argv):
        if argv[k] in FIND_EXEC:
            end = next((j for j in range(k + 1, len(argv)) if argv[j] in FIND_END), len(argv))
            out.append(argv[k + 1:end])
            k = end
        k += 1
    return out


def glob_names_ledger(pattern):
    """Does a shell glob that mentions ledger (ledger.p?, ledger*, */ledger.py) match ledger.py?"""
    name = os.path.basename(pattern)
    if "ledger" not in name:
        return False
    try:
        return fnmatch.fnmatch("ledger.py", name)
    except re.error:                                  # [2024-11-01]: a bad range is not a pattern
        return False


def find_names_ledger(argv):
    """Does a find select files named ledger.py (-name ledger.py, -name 'ledger*', -path '*/ledger.py')?"""
    return any(a in FIND_NAMES and glob_names_ledger(argv[i + 1]) for i, a in enumerate(argv[:-1]))


# Absolute paths of the plugin's own scripts whose content matches the project manifest.
# guard.py fills this in; those scripts validate and log their Feishu writes themselves.
TRUSTED = set()


def _full(path, cwd):
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd or ".", path))


def _cd(cur, argv):
    target = argv[1] if len(argv) > 1 else os.path.expanduser("~")
    if target == "-" or cur is None:
        return cur
    return os.path.normpath(os.path.join(cur, os.path.expanduser(target)))


def parse(command, cwd=None, _depth=0, _result=None):
    res = _result or Parsed()
    if _depth > MAX_DEPTH:
        res.opaque.append("嵌套太深")
        return res
    if MENTION_RE.search(command):
        res.mentions = True
    text, bodies = split_heredocs(command)
    if bodies:
        res.bodies.update(bodies)
        res.simple = False
        for body, quoted in bodies.values():          # $(...) in an unquoted here-document runs
            if not quoted:
                for inner in substitutions(body):
                    parse(inner, cwd, _depth + 1, res)
    try:
        tokens = tokenize(strip_comments(text).replace("\\\n", " "))     # a trailing \ continues the line
    except ValueError as e:
        res.error = "命令解析失败：%s" % e
        return res
    segs, piped = _segments(tokens)
    if len(segs) > 1 or any(t in SEPARATORS or t in REDIRECTS for t in tokens):
        res.simple = False
    cur = cwd
    feed = None                                       # what the previous command writes to the pipe
    for seg, is_piped in zip(segs, piped):
        docs, strings, infile = seg_stdin(seg, res.bodies, cur)
        stdin_text = (docs + strings)[0] if docs or strings else (
            read_script(infile, cur) if infile else (feed if is_piped else None))
        stdin_known = bool(docs or strings) or (infile is not None and stdin_text is not None) or (is_piped and feed is not None)
        seg, had_redirect = _drop_redirects(seg)
        if had_redirect:
            res.simple = False
        for tok in seg:
            if "$(" in tok or "`" in tok:
                res.simple = False
                for inner in substitutions(tok):
                    parse(inner, cur, _depth + 1, res)
        argv, wrappers = _unwrap(seg)
        feed = pipe_output(argv, stdin_text, cur)
        if not argv:
            continue
        head = argv[0]
        base = os.path.basename(head)
        if base == "cd":
            cur = _cd(cur, argv)
            continue
        if base in CLI_BASENAMES:
            res.invocations.append(Invocation(CLI_BASENAMES[base], argv[1:], cur, wrappers + [head], stdin_text))
            continue
        if base in ("npx", "bunx", "pnpx"):
            j = 1
            while j < len(argv) and argv[j].startswith("-"):
                j += 2 if argv[j] in ("-p", "--package") else 1
            if j < len(argv):
                for pkg, cli in NPX_PACKAGES:
                    if argv[j].startswith(pkg):
                        res.invocations.append(Invocation(cli, argv[j + 1:], cur, wrappers + argv[:j + 1], stdin_text))
                        break
            continue
        if base == "npm" and len(argv) > 1 and argv[1] in ("exec", "x"):
            if "--" in argv:
                k = argv.index("--")
                parse(shlex.join(argv[k + 1:]), cur, _depth + 1, res)
            elif res.mentions:
                res.opaque.append("npm exec")
            continue
        if base in SHELLS or base in ("csh", "tcsh"):
            kind, value, _ = shell_target(argv[1:])
            res.simple = False
            if kind == "command":
                parse(value, cur, _depth + 1, res)
            elif kind == "script":
                if _scan_file(value, cur):
                    res.opaque.append("脚本 %s 里调用了飞书命令行" % value)
            elif stdin_known:
                parse(stdin_text or "", cur, _depth + 1, res)
            else:
                res.opaque.append("%s 从标准输入读命令" % base)
            continue
        if base == "eval":
            res.simple = False
            parse(" ".join(argv[1:]), cur, _depth + 1, res)
            continue
        if base == "find" or head in FIND_EXEC:         # an escaped \; splits find ... -exec a \; -exec b \; in two
            for clause in find_clauses(argv):
                res.simple = False
                parse(shlex.join(clause), cur, _depth + 1, res)
            continue
        if base in ("source", "."):
            if len(argv) > 1 and _scan_file(argv[1], cur):
                res.opaque.append("脚本 %s 里调用了飞书命令行" % argv[1])
            continue
        if base == "node" and len(argv) > 1:
            hit = [cli for pkg, cli in NPX_PACKAGES if pkg in argv[1]]
            if hit:
                res.invocations.append(Invocation(hit[0], argv[2:], cur, wrappers + argv[:2], stdin_text))
                continue
        if base in INTERPRETERS or PYTHON_RE.match(base):
            if PYTHON_RE.match(base):
                kind, value, _ = python_target(argv[1:])
                code = code_lines(value) if kind == "code" else None
                files = [value] if kind == "script" else []
                reads_stdin = kind == "stdin"            # python -m mod <<EOF: the body is the module's data
            else:
                code = inline_code(base, argv[1:])
                files = [] if code is not None else [a for a in argv[1:] if not a.startswith("-")][:1]
                reads_stdin = code is None and not files
            if code is not None:
                if MENTION_RE.search(code):
                    res.opaque.append("%s 内联代码里调用了飞书命令行" % base)
                continue
            if reads_stdin:
                if stdin_text and mentions_cli(stdin_text):
                    res.opaque.append("%s 从标准输入读的代码里调用了飞书命令行" % base)
                continue
            if files and _full(files[0], cur) in TRUSTED:
                res.trusted.append(files[0])
                continue
            if files and _scan_file(files[0], cur):
                res.opaque.append("脚本 %s 里调用了飞书命令行" % files[0])
            continue
        if _full(head, cur) in TRUSTED:
            res.trusted.append(head)
            continue
        if ("/" in head or os.path.isfile(os.path.join(cur or ".", head))) and _scan_file(head, cur):
            res.opaque.append("脚本 %s 里调用了飞书命令行" % head)
    return res


# ---------------------------------------------------------------- lark-cli
LARK_BOOL = {"--yes", "--dry-run", "-h", "--help", "--json", "--quiet", "-v", "--version",
             "--check", "--force", "--set-bot-manager", "--fail-on-orphan", "--all",
             "--auto-paginate", "--no-color"}


def options(args, booleans):
    flags, pos, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos.extend(args[i + 1:])
            break
        if a.startswith("--") and "=" in a:
            k, v = a.split("=", 1)
            flags.setdefault(k, []).append(v)
            i += 1
        elif a.startswith("-") and len(a) > 1 and not re.match(r"^-\d", a):
            if a in booleans:
                flags.setdefault(a, []).append(True)
                i += 1
            else:
                flags.setdefault(a, []).append(args[i + 1] if i + 1 < len(args) else None)
                i += 2
        else:
            pos.append(a)
            i += 1
    return pos, flags


def last(flags, *names):
    for n in names:
        if n in flags and flags[n]:
            return flags[n][-1]
    return None


def classify_lark(args, risk):
    pos, flags = options(args, LARK_BOOL)
    cmds = (risk or {}).get("commands", {})
    key = tier = None
    for n in (3, 2, 1):
        if len(pos) >= n and " ".join(pos[:n]) in cmds:
            key = " ".join(pos[:n])
            tier = cmds[key]
            break
    if key is None:
        if not pos and ("--version" in flags or "-v" in flags):
            key, tier = "--version", "read"
        elif pos and pos[0] == "api":
            method = pos[1].upper() if len(pos) > 1 else ""
            key, tier = "api " + method, ("read" if method in ("GET", "HEAD") else "write")
        else:
            key, tier = " ".join(pos[:2]) or "(none)", "unknown"
    return {
        "cli": "lark", "key": key, "tier": tier, "pos": pos, "flags": flags,
        "help": "-h" in flags or "--help" in flags or (bool(pos) and pos[0] == "help"),
        "dry_run": "--dry-run" in flags, "yes": "--yes" in flags,
        "as": last(flags, "--as"),
    }


# ------------------------------------------------------------------ meegle
MEEGLE_BOOL = {"--dry-run", "--envelope", "--auto-paginate", "-h", "--help", "--json"}
MEEGLE_TOP_READ = {"version", "help", "inspect", "url", "completion", "extension", "team", "user",
                   "mywork", "chart", "deliverable", "workhour", "relation", "project"}
MEEGLE_READ = {
    "workitem": {"+batch-get", "get", "list-op-records", "meta-create-fields", "meta-fields",
                 "meta-roles", "meta-types", "query"},
    "workflow": {"get-node", "list-state-required", "list-state-transitions", "meta-node-fields"},
    "comment": {"list"},
    "view": {"get", "list-multi-project-workitems", "search"},
    "resource": {"meta-fields"},
    "wbs": {"get-draft-progress", "list-draft-rows", "list-element-templates", "list-instance-rows"},
    "attachment": {"+download", "prepare-download"},
    "auth": {"status"},
    "telemetry": {"status"},
    "config": {"get", "show"},
}
MEEGLE_WRITE = {
    "workitem": {"create", "update"},
    "workflow": {"transition", "transition-state", "update-node"},
    "comment": {"add"},
    "view": {"create-fixed", "update-fixed"},
    "subtask": {"update"},
    "attachment": {"+upload", "prepare-upload"},
    "resource": {"create"},
    "wbs": {"create-draft", "edit-draft", "publish-draft", "reset-draft"},
}
MEEGLE_ADMIN = {"auth": {"login", "logout"}, "telemetry": {"enable", "disable"},
                "config": {"init", "profile", "set"}, "preference": None, "ai-handoff": None}


def classify_meegle(args):
    pos, flags = options(args, MEEGLE_BOOL)
    dom = pos[0] if pos else ""
    verb = pos[1] if len(pos) > 1 else ""
    key = (dom + " " + verb).strip() or "(none)"
    if dom in MEEGLE_ADMIN and (MEEGLE_ADMIN[dom] is None or verb in MEEGLE_ADMIN[dom]):
        tier = "admin"
    elif dom in MEEGLE_TOP_READ or verb in MEEGLE_READ.get(dom, ()):
        tier = "read"
    elif verb in MEEGLE_WRITE.get(dom, ()):
        tier = "write"
    elif not pos:
        tier = "read"
    else:
        tier = "unknown"
    return {
        "cli": "meegle", "key": key, "tier": tier, "pos": pos, "flags": flags,
        "help": "-h" in flags or "--help" in flags or dom == "help",
        "dry_run": "--dry-run" in flags, "yes": False, "as": None,
    }


def classify(inv, risk):
    return classify_lark(inv.args, risk) if inv.cli == "lark" else classify_meegle(inv.args)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
