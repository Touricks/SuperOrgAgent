#!/usr/bin/env python3
"""Record the complete setup clock, including human steps; never claim a live test from a timer."""
import argparse
import contextlib
import datetime
import io
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_runtime as runtime
import codex_setup

BUDGET_SECONDS = 300


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def clock(target, action, at=None):
    path = Path(target).resolve() / ".agent-org/state/onboarding.json"
    fixed_time = at is not None
    at = at or now()
    record = runtime.read(path)
    if action == "begin" and record is None:
        record = {"started_at": at.isoformat(), "budget_seconds": BUDGET_SECONDS,
                  "scope": "downloaded_plugin_to_configured", "attempts": []}
        runtime.save(path, record)
    if not record:
        raise runtime.RuntimeError("没有初始化起点；先 begin，不能在配置结束后补算耗时")
    check, code = None, None
    if action == "finish":
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = codex_setup.check(str(Path(target).resolve()))
        check = json.loads(out.getvalue())
        if not fixed_time:
            at = now()  # Verification time is part of configuration time.
    elapsed = (at - datetime.datetime.fromisoformat(record["started_at"])).total_seconds()
    if elapsed < 0:
        raise runtime.RuntimeError("系统时间早于初始化起点，计时无效")
    result = dict(record, elapsed_seconds=round(elapsed, 2), within_budget=elapsed <= BUDGET_SECONDS)
    if action == "finish":
        result.update(local_check=check, local_ready=(code == 0),
                      timing_pass=(code == 0 and elapsed <= BUDGET_SECONDS),
                      live_acceptance="requires_fresh_remote_readbacks",
                      note="计时包含认证、复制和信任等待；完整三处回读另见测试证据。超时不能重置起点后称为首次通过。")
        record["attempts"].append({"finished_at": at.isoformat(), "elapsed_seconds": result["elapsed_seconds"],
                                   "local_ready": code == 0, "timing_pass": result["timing_pass"]})
        runtime.save(path, record)
    return result


def main():
    parser = argparse.ArgumentParser(description="Codex 五分钟配置计时；begin 幂等，不跳过人工等待")
    parser.add_argument("action", choices=("begin", "status", "finish"))
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    try:
        result = clock(args.target, args.action)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.action == "finish" and not result["timing_pass"] else 0
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
