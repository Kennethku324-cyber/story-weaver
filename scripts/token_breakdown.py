#!/usr/bin/env python3
"""token_breakdown.py — 分析 timing.log 入面嘅 token 使用統計。

用法：
  python scripts/token_breakdown.py [--top N] [--no-caller]

輸出：每 caller 嘅 call 次數、成功/失敗、token 分佈、成本估算。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "generative_agents" / "results" / "timing.log"

# DeepSeek 定價（CNY per 1M tokens）
PRICING = {
    "deepseek-chat":    {"prompt": 1.0, "completion": 2.0},   # 標準
    "deepseek-reasoner": {"prompt": 4.0, "completion": 16.0},  # R1
    "deepseek-v3":       {"prompt": 2.0, "completion": 8.0},
}


def _parse_line(line: str) -> dict | None:
    """Parse 新版 timing.log line（含 token data）。兼容舊版（無 token field）。"""
    m = re.search(r"caller=(\S+)", line)
    if not m:
        return None
    d = {"caller": m.group(1)}
    for field in ["model", "attempts", "duration"]:
        m = re.search(rf"{field}=(\S+)", line)
        if m:
            d[field] = m.group(1)
    m = re.search(r"ok=(\S+)", line)
    d["ok"] = m.group(1) == "True" if m else False
    for field in ["prompt_tok", "completion_tok", "total_tok", "cumul_tok"]:
        m = re.search(rf"{field}=(\d+)", line)
        d[field] = int(m.group(1)) if m else 0
    return d


def main():
    parser = argparse.ArgumentParser(description="Story Weaver token 統計")
    parser.add_argument("--log", type=str, default=str(LOG_PATH), help="timing.log 路徑")
    parser.add_argument("--top", type=int, default=30, help="顯示 top N caller")
    parser.add_argument("--no-caller", action="store_true", help="只顯示總計")
    args = parser.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        print(f"❌ 搵唔到 log：{log_path}")
        sys.exit(1)

    records = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            rec = _parse_line(line)
            if rec:
                records.append(rec)

    if not records:
        print("❌ 冇 parse 到任何 llm_call log line")
        sys.exit(1)

    # ── 統計 ──
    caller_stats: dict[str, dict] = defaultdict(lambda: {
        "calls": 0, "ok": 0, "fail": 0,
        "prompt_tok": 0, "completion_tok": 0, "total_tok": 0,
        "total_duration": 0.0,
    })
    total_prompt = 0
    total_completion = 0
    first_ts = last_ts = None
    model_set = set()
    has_token_data = False

    for r in records:
        c = r["caller"]
        s = caller_stats[c]
        s["calls"] += 1
        if r["ok"]:
            s["ok"] += 1
        else:
            s["fail"] += 1
        pt = r.get("prompt_tok", 0)
        ct = r.get("completion_tok", 0)
        if pt or ct:
            has_token_data = True
        s["prompt_tok"] += pt
        s["completion_tok"] += ct
        s["total_tok"] += pt + ct
        total_prompt += pt
        total_completion += ct
        dur = r.get("duration", "0s")
        try:
            s["total_duration"] += float(dur.rstrip("s"))
        except ValueError:
            pass
        model_set.add(r.get("model", "?"))

    total_tokens = total_prompt + total_completion
    model = sorted(model_set)[0] if len(model_set) == 1 else "multiple"
    pricing = PRICING.get(model, PRICING["deepseek-chat"])
    cost_cny = (total_prompt / 1_000_000) * pricing["prompt"] + (total_completion / 1_000_000) * pricing["completion"]
    cost_hkd = cost_cny * 1.08  # CNY → HKD rough rate

    # ── 輸出 ──
    print(f"📊 Token 統計 — {log_path.name}")
    print(f"   Model: {model}  |  定價: ¥{pricing['prompt']}/¥{pricing['completion']} per 1M tokens")
    print(f"   Total calls: {len(records):,}  |  Success: {sum(1 for r in records if r['ok']):,}")
    if not has_token_data:
        print(f"   ⚠️  呢個 log 冇 token data（舊版 llm_model.py）。行一次新版先有數。")
        print(f"   📏 僅供 call count 參考 — token 數係零。")
    print()
    print(f"   {'Prompt tokens':>20s}: {total_prompt:>15,}")
    print(f"   {'Completion tokens':>20s}: {total_completion:>15,}")
    print(f"   {'Total tokens':>20s}: {total_tokens:>15,}")
    print(f"   {'Estimated cost (CNY)':>20s}: ¥{cost_cny:>14.2f}")
    print(f"   {'Estimated cost (HKD)':>20s}: HK${cost_hkd:>13.2f}")
    print()

    if args.no_caller:
        return

    # ── Per-caller breakdown ──
    sorted_callers = sorted(caller_stats.items(), key=lambda x: x[1]["total_tok"], reverse=True)
    print(f"{'Caller':<32s} {'Calls':>6s} {'OK':>5s} {'Fail':>5s} "
          f"{'PromptTok':>12s} {'CompTok':>10s} {'TotalTok':>12s} {'%Cost':>7s} {'AvgDur':>7s}")
    print("-" * 110)

    for i, (caller, s) in enumerate(sorted_callers[:args.top]):
        if i >= args.top:
            break
        pct = (s["total_tok"] / total_tokens * 100) if total_tokens > 0 else 0
        avg_dur = s["total_duration"] / s["calls"] if s["calls"] else 0
        print(f"{caller:<32s} {s['calls']:>6d} {s['ok']:>5d} {s['fail']:>5d} "
              f"{s['prompt_tok']:>12,} {s['completion_tok']:>10,} {s['total_tok']:>12,} "
              f"{pct:>6.1f}% {avg_dur:>6.2f}s")

    if len(sorted_callers) > args.top:
        remaining = sorted_callers[args.top:]
        rem_total = sum(s["total_tok"] for _, s in remaining)
        rem_calls = sum(s["calls"] for _, s in remaining)
        print(f"{'... (+' + str(len(remaining)) + ' more callers)':<32s} {rem_calls:>6d} "
              f"{rem_total:>43,}")


if __name__ == "__main__":
    main()
