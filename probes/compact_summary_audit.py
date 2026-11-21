#!/usr/bin/env python3
r"""compact_summary_audit.py — 把 CompAct-Bench 的六类压缩错误搬成可机械审计的结构判据。

背景：CompAct-Bench（arXiv:2611.25673）把压缩失败归成 CO 关键遗漏 / FD 事实扭曲 /
PC 过早定论 / LR 丢失已定结论 / PSL 进度状态丢失 / CGD 约束目标扭曲 六类。
其中 FD 与 PC 需要 ground truth 才能判，机械审计只能给结构代理；CO/LR/PSL/CGD
则可以在「摘要是否保留了可重建任务状态所需的字段」这一层被计数。

本脚本对 ~/.dsh/sessions 里真实的 compaction/summary 文本逐条打分，输出每类的
命中率。它测的是「结构覆盖」，不是「事实正确」——这一点在输出里显式标注。

用法:
  python3 scripts/compact_summary_audit.py --audit
  python3 scripts/compact_summary_audit.py --audit --json /tmp/audit.json
  python3 scripts/compact_summary_audit.py --selftest   # 注入缺字段/全字段样本，门必须两种都判对

判据（每条都是「摘要里必须出现该类标记」）:
  state_progress  进度/已完成：/已完成|completed|settled|done/
  open_objectives 未完成目标：/open objectives|open objective|未完成|进行中|待办/
  constraints     约束：/约束|constraint|禁止|must|必须先|不要/
  evidence_pointer 可追溯指针：/notes\/|scripts\/|\.md|\.json|\.py|\/home\//
  numbers         数字：至少一个 >=2 位数字或百分数
  unverified_flag 未验证标注：/未验证|unverified|未取到|无法确认/
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

RUBRIC = {
    "state_progress": re.compile(r"已完成|completed|settled|done|状态"),
    "open_objectives": re.compile(r"open objective|未完成|进行中|待办|未结|remaining"),
    "constraints": re.compile(r"约束|constraint|禁止|must|必须先|不许|不要"),
    "evidence_pointer": re.compile(r"notes/|scripts/|\.md|\.json|\.py|/home/"),
    "numbers": re.compile(r"\d{2,}|\d+(?:\.\d+)?%"),
    "unverified_flag": re.compile(r"未验证|unverified|未取到|无法确认|未测量"),
}

# FD/PC 需要 ground truth；这里只标记「疑似过早定论」：既写完成又写未完成
PC_PROXY = re.compile(r"(已完成|resolved|finished)[^。\n]{0,40}(未完成|open|remaining|待办|未结)")


def summary_text(payload) -> str:
    if isinstance(payload, list):
        return "".join(x.get("text", "") for x in payload if isinstance(x, dict))
    return str(payload or "")


def audit(root: str = SESSIONS_ROOT) -> dict:
    cwd = os.getcwd()
    os.chdir(root)
    try:
        files = _dsh_files(root)
        rows = []
        hits = collections.Counter()
        total = 0
        for f in files:
            for line in _dsh_lines("", f):
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") != "compaction/summary":
                    continue
                d = e.get("data") or {}
                txt = summary_text(d.get("summary"))
                if not txt.strip():
                    continue
                total += 1
                got = {k: bool(p.search(txt)) for k, p in RUBRIC.items()}
                for k, v in got.items():
                    hits[k] += int(v)
                rows.append(
                    {
                        "session": f.split("/")[0] + "/" + f.split("/")[1],
                        "chars": len(txt),
                        "shadowed": d.get("shadowedTokenCount"),
                        "provider": d.get("provider"),
                        "score": sum(got.values()),
                        "missing": [k for k, v in got.items() if not v],
                        "pc_proxy": bool(PC_PROXY.search(txt)),
                    }
                )
        return {
            "summaries": total,
            "coverage": {k: [hits[k], total, round(hits[k] / total, 3) if total else None] for k in RUBRIC},
            "mean_score": round(sum(r["score"] for r in rows) / total, 2) if total else None,
            "pc_proxy_hits": sum(1 for r in rows if r["pc_proxy"]),
            "rows": rows,
        }
    finally:
        os.chdir(cwd)


GOOD = "已完成 X。open objectives: Y 未完成。约束：禁止改 Z。见 notes/a.md、scripts/b.py。数字 56.7%。未验证：W。"
BAD = "The task was handled successfully and everything is fine."


def selftest() -> int:
    """双向反证：全字段样本要满分，空壳样本要判缺——门必须两种都判对。"""
    fails = 0
    good = {k: bool(p.search(GOOD)) for k, p in RUBRIC.items()}
    if not all(good.values()):
        print("FAIL: 完整样本被判缺字段", {k: v for k, v in good.items() if not v})
        fails += 1
    bad = {k: bool(p.search(BAD)) for k, p in RUBRIC.items()}
    if bad["open_objectives"] or bad["constraints"] or bad["evidence_pointer"]:
        print("FAIL: 空壳样本被误判有字段", {k: v for k, v in bad.items() if v})
        fails += 1
    if sum(bad.values()) >= sum(good.values()):
        print("FAIL: 空壳与完整样本得分不可分")
        fails += 1
    if not PC_PROXY.search("已完成 A，未完成 B"):
        print("FAIL: PC 代理未识别「既完成又未完成」")
        fails += 1
    print("selftest:", "PASS" if fails == 0 else f"FAIL ({fails})")
    return 0 if fails == 0 else 1


def report(res: dict) -> None:
    n = res["summaries"]
    print(f"样本 {n} 条 compression summary（来自本机 806 个 session 轨迹）")
    if not n:
        print("无样本")
        return
    print(f"平均结构分 {res['mean_score']}/6（6=六类字段全覆盖）")
    print("\n-- 字段覆盖（命中/总数/比例）--")
    for k, (h, t, p) in res["coverage"].items():
        print(f"  {k:18s} {h:3d}/{t:3d}  {p}")
    print(f"\nPC 代理命中（同时写已完成与未完成）：{res['pc_proxy_hits']}/{n}")
    miss = collections.Counter()
    for r in res["rows"]:
        for m in r["missing"]:
            miss[m] += 1
    print("\n-- 缺失最多的字段 --")
    for k, v in miss.most_common():
        print(f"  {v:3d}x  {k}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json")
    ap.add_argument("--root", default=SESSIONS_ROOT)
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.audit:
        ap.print_help()
        return 2
    res = audit(a.root)
    report(res)
    if a.json:
        with open(a.json, "w", encoding="utf8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
        print(f"\nwrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
