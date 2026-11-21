#!/usr/bin/env python3
"""agent_log_census.py — DSH 会话/日志普查：工具故障聚类 + compaction 遥测。

用途（对应 Performance Factory 的 self-repair 前置条件与 CompAct-Bench 的 IRC 口径）：
  1) 扫 ~/.dsh/sessions/*/*/session.v4.jsonl.zstd，抽 tool/result 的失败信号，
     归一化成 signature，按「跨多少个 session 出现」排序 —— 回答「同一工具错误
     是否阻塞了多个任务」，即 self-repair 是否值得自动开工单。
  2) 抽 compaction/start|summary|end 事件，读 shadowedTokenCount 与摘要长度，
     算真实实现比率（realized retention ratio）与落桶分布 —— 本机版 IRC。

用法:
  python3 scripts/agent_log_census.py --census            # 两份报告都出
  python3 scripts/agent_log_census.py --census --json out.json
  python3 scripts/agent_log_census.py --selftest          # 注入已知故障，门必须报红

设计约束:
  - 只读；不改 ~/.dsh。
  - zstd 路径必须以 './' 前缀传给 CLI，因为项目目录名以 '--' 开头会被当成 option。
  - 无第三方依赖（zstandard 模块本机未装）。
"""

from __future__ import annotations

import argparse

# --- adapter bootstrap (repo-root on sys.path) -------------------------------
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from adapters.dsh import (  # noqa: E402
    SESSIONS_ROOT as _DSH_ROOT, iter_session_files as _dsh_files,
    read_events as _dsh_read_events, read_lines as _dsh_lines,
)
import collections
import glob
import json
import os
import re
import subprocess
import sys

SESSIONS_ROOT = _DSH_ROOT
PATTERN = "*/*/session.v4.jsonl.zstd"

# ---------- 失败信号 ----------

EXIT_RE = re.compile(r"\[exit code: (-?\d+)\]")
SANDBOX_RE = re.compile(r"\[sandbox: [^\]]+\]")
SIG_RULES = [
    ("sandbox_denied", re.compile(r"\[sandbox:")),
    ("module_missing", re.compile(r"ModuleNotFoundError|No module named")),
    ("cmd_not_found", re.compile(r"command not found|not recognized as an internal")),
    ("no_such_file", re.compile(r"No such file or directory|ENOENT")),
    ("permission", re.compile(r"Permission denied|EACCES")),
    ("timeout", re.compile(r"timed out|Timeout|ETIMEDOUT")),
    ("syntax_error", re.compile(r"SyntaxError|IndentationError")),
    ("traceback", re.compile(r"Traceback \(most recent call last\)")),
    ("http_error", re.compile(r"HTTP (?:4\d\d|5\d\d)|HTTPError")),
    ("not_found_tool", re.compile(r"Tool .* not found|Unknown tool")),
]

# 归一化：抹掉会漂移的部分，让同一种故障落进同一个 signature
NORM_RULES = [
    (re.compile(r"/[\w.\-]+(?:/[\w.\-]+)+"), "<PATH>"),
    (re.compile(r"\b[0-9a-f]{8,}\b"), "<HEX>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<N>"),
    (re.compile(r"\s+"), " "),
]


def normalize(text: str, limit: int = 160) -> str:
    s = text
    for pat, rep in NORM_RULES:
        s = pat.sub(rep, s)
    return s.strip()[:limit]


def classify(text: str) -> list[str]:
    hits = [name for name, pat in SIG_RULES if pat.search(text)]
    m = EXIT_RE.search(text)
    if m and int(m.group(1)) != 0:
        hits.append("nonzero_exit")
    return hits


def text_of(event: dict) -> str:
    d = event.get("data") or {}
    msg = d.get("message")
    if msg is None:
        return ""
    parts = []
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and isinstance(c.get("text"), str):
                parts.append(c["text"])
    elif isinstance(content, str):
        parts.append(content)
    if not parts:
        parts.append(json.dumps(msg, ensure_ascii=False))
    return "\n".join(parts)


def summary_text_of(summary) -> str:
    """把 compaction/summary 归一化为纯文本后再估 token。

    真实日志里 summary 是 [{"type":"text","text":"..."}] 的 list（本机 62/62
    全是这个形态）。直接 len(list)=1 会把摘要估成 0.25 token，比率恒 0.0000。
    这里抽出每个元素的 text 字段拼接；dict/其它形态 json.dumps 兜底。
    """
    if summary is None:
        return ""
    if isinstance(summary, str):
        return summary
    if isinstance(summary, list):
        parts = []
        for c in summary:
            if isinstance(c, dict):
                t = c.get("text")
                parts.append(t if isinstance(t, str) else json.dumps(c, ensure_ascii=False))
            else:
                parts.append(str(c))
        return "\n".join(parts)
    return json.dumps(summary, ensure_ascii=False)


def iter_events(path: str):
    for line in _dsh_lines("", path):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


# ---------- 普查 ----------


def census(root: str = SESSIONS_ROOT) -> dict:
    cwd = os.getcwd()
    os.chdir(root)
    try:
        files = _dsh_files(root)
        sessions = 0
        tool_calls = 0
        failures = 0
        sig_sessions: dict[str, set] = collections.defaultdict(set)
        sig_kinds: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        by_tool = collections.Counter()
        comp = collections.Counter()
        ratios: list[dict] = []
        for f in files:
            sid = os.path.relpath(os.path.dirname(f), root)
            saw_any = False
            pending: dict[str, str] = {}
            for e in iter_events(f):
                saw_any = True
                t = e.get("type")
                if t == "tool/call":
                    tool_calls += 1
                    d = e.get("data") or {}
                    pending[d.get("callId", "")] = d.get("name") or "?"
                elif t == "tool/result":
                    txt = text_of(e)
                    kinds = classify(txt)
                    if kinds:
                        failures += 1
                        d = e.get("data") or {}
                        tool = pending.get(d.get("toolCallId", ""), "?")
                        by_tool[tool] += 1
                        sig = f"{tool} :: {normalize(txt)}"
                        sig_sessions[sig].add(sid)
                        for k in kinds:
                            sig_kinds[sig][k] += 1
                elif t == "compaction/start":
                    comp["start"] += 1
                elif t == "compaction/summary":
                    comp["summary"] += 1
                    d = e.get("data") or {}
                    shadow = d.get("shadowedTokenCount")
                    rng = d.get("shadowedRange") or {}
                    summary_text = d.get("summary")
                    summary_est = summary_text_of(summary_text)
                    # 粗估：4 字符/token，只用于量级判断，标注为估算
                    est_summary_tokens = len(summary_est) / 4.0
                    if shadow:
                        ratios.append(
                            {
                                "session": sid,
                                "shadowed": shadow,
                                "summary_tokens_est": round(est_summary_tokens, 1),
                                "ratio_est": round(est_summary_tokens / shadow, 4),
                                "range": [rng.get("start"), rng.get("end")],
                            }
                        )
                elif t == "compaction/end":
                    comp["end"] += 1
                    d = e.get("data") or {}
                    if d.get("error"):
                        comp["end_error"] += 1
            if saw_any:
                sessions += 1
        ranked = sorted(sig_sessions.items(), key=lambda kv: (-len(kv[1]), -sum(sig_kinds[kv[0]].values())))
        return {
            "files": len(files),
            "sessions_read": sessions,
            "tool_calls": tool_calls,
            "failed_tool_results": failures,
            "fail_rate": round(failures / tool_calls, 4) if tool_calls else None,
            "distinct_signatures": len(sig_sessions),
            "by_tool": by_tool.most_common(15),
            "top_signatures": [
                {
                    "signature": sig,
                    "sessions": len(sids),
                    "kinds": dict(sig_kinds[sig]),
                }
                for sig, sids in ranked[:25]
              ],
            "multi_session_signatures": sum(1 for _, s in sig_sessions.items() if len(s) >= 2),
            "compaction": dict(comp),
            "compaction_ratios": ratios[:50],
        }
    finally:
        os.chdir(cwd)


def report(res: dict) -> None:
    print("== 会话普查 ==")
    print(f"文件 {res['files']} / 读到 {res['sessions_read']} / tool/call {res['tool_calls']} / 失败结果 {res['failed_tool_results']} / 失败率 {res['fail_rate']}")
    print(f"distinct signatures {res['distinct_signatures']} / 跨≥2 session 的 signature {res['multi_session_signatures']}")
    print("\n-- 按工具 --")
    for name, n in res["by_tool"]:
        print(f"  {n:5d}  {name}")
    print("\n-- 最广的故障 signature（按涉及 session 数）--")
    for row in res["top_signatures"][:12]:
        print(f"  [{row['sessions']:3d} sess] {row['signature'][:130]}")
    print("\n== compaction 遥测 ==")
    print(f"  {res['compaction']}")
    if res["compaction_ratios"]:
        rs = [r["ratio_est"] for r in res["compaction_ratios"]]
        print(f"  样本 {len(rs)} / 中位 {sorted(rs)[len(rs)//2]:.4f} / min {min(rs):.4f} / max {max(rs):.4f}（摘要 token 为 4 字符/token 估算）")
    else:
        print("  本机无 compaction/summary 记录 → 该路径从未在本机触发")


# ---------- 自测：注入已知故障，门必须报红 ----------

FIXTURE = [
    {"type": "tool/call", "data": {"callId": "c1", "name": "bash"}},
    {"type": "tool/result", "data": {"toolCallId": "c1", "message": {"content": [{"type": "text", "text": "python3 x.py\nModuleNotFoundError: No module named 'zstandard'"}]}}},
    {"type": "tool/call", "data": {"callId": "c2", "name": "read"}},
    {"type": "tool/result", "data": {"toolCallId": "c2", "message": {"content": [{"type": "text", "text": "[sandbox: file access limited under workspace-write mode]"}]}}},
    {"type": "tool/call", "data": {"callId": "c3", "name": "bash"}},
    {"type": "tool/result", "data": {"toolCallId": "c3", "message": {"content": [{"type": "text", "text": "ok, done"}]}}},
    {"type": "compaction/summary", "data": {"compactionId": "k1", "shadowedTokenCount": 40000, "shadowedRange": {"start": 1, "end": 90}, "summary": "x" * 16000}},
    # 真实形态：summary 是 list[{type,text}]（本机 62/62 全是这个），旧代码 len(list)=1 → 比率恒 0
    {"type": "compaction/summary", "data": {"compactionId": "k2", "shadowedTokenCount": 4000, "shadowedRange": {"start": 1, "end": 90}, "summary": [{"type": "text", "text": "y" * 8000}]}},
]


def selftest() -> int:
    """三道断言：失败必被抓、干净结果不误报、压缩比率能算出来。"""
    failures = 0
    kinds1 = classify(text_of(FIXTURE[1]))
    if "module_missing" not in kinds1:
        print("FAIL: ModuleNotFoundError 未被识别"); failures += 1
    kinds2 = classify(text_of(FIXTURE[3]))
    if "sandbox_denied" not in kinds2:
        print("FAIL: sandbox 拒绝未被识别"); failures += 1
    kinds3 = classify(text_of(FIXTURE[5]))
    if kinds3:
        print(f"FAIL: 干净结果被误报为 {kinds3}"); failures += 1
    # 归一化必须把不同路径/数字折叠成同一 signature
    a = normalize("No such file or directory: /a/b/c.py")
    b = normalize("No such file or directory: /x/y/z.py")
    if a != b:
        print(f"FAIL: 归一化未折叠路径: {a!r} != {b!r}"); failures += 1
    # 非零退出码
    if "nonzero_exit" not in classify("boom\n[exit code: 2]"):
        print("FAIL: 非零退出码未被识别"); failures += 1
    # 真实 list 形态的 summary 必须抽出 text：list 型不得被 len(list)=1 估成 0
    lst = [{"type": "text", "text": "y" * 8000}, {"type": "text", "text": "z" * 2000}]
    got = summary_text_of(lst)
    if len(got) != 10001:  # 8000 + 换行 + 2000
        print(f"FAIL: list 型 summary 未展开为 text（len={len(got)}，期望 10001）"); failures += 1
    if summary_text_of("x" * 100) != "x" * 100:
        print("FAIL: str 型 summary 被误改"); failures += 1
    if summary_text_of(None) != "":
        print("FAIL: None 型 summary 未归零"); failures += 1
    if summary_text_of({"type": "text", "text": "abc"}) == "abc":
        print("FAIL: dict 型 summary 应 json.dumps 兜底"); failures += 1
    print("selftest:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--census", action="store_true")
    ap.add_argument("--json")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--root", default=SESSIONS_ROOT)
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.census:
        ap.print_help()
        return 2
    res = census(args.root)
    report(res)
    if args.json:
        with open(args.json, "w", encoding="utf8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
