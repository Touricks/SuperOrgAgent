"""Locate project state without depending on the conversation host."""
import os


def runtime_dir(root):
    codex = os.path.join(root, ".agent-org")
    if os.path.isfile(os.path.join(codex, "manifest.json")):
        return codex
    return os.path.join(root, ".claude", "agent-org")


def project_root():
    # An explicit Claude project belongs to Claude even when Codex is also installed.
    if os.environ.get("CLAUDE_PROJECT_DIR"):
        return os.path.abspath(os.environ["CLAUDE_PROJECT_DIR"])
    here = os.path.abspath(os.getcwd())
    while True:
        if any(os.path.isfile(os.path.join(here, p, "manifest.json"))
               for p in (".agent-org", ".claude/agent-org")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return os.getcwd()
        here = parent
