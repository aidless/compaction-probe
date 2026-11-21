#!/usr/bin/env python3
"""compaction_probe.py — 常驻探针：每天一行，报本机压缩事务的健康度。

读的是 DSH 自己写的事件（`~/.dsh/sessions/*/*/session.v4.jsonl.zstd` 里的
`compaction/start|summary|end|error|prune`），不重算任何模型行为，只做聚合与判定。

判据（预注册，改阈值必须改这里并说明理由）：
  R1 事务成功率  = summary 数 / start 数            < 0.60  → 红
  R2 保留率中位  = 摘要 token / 被替换 token        < 0.05  → 红
     （角色：CompAct 2611.25673 的默认目标是 0.10；低于一半即视为退化）
  R3 边界覆盖    = 进度 <50% 的成功压缩占比         = 0 且样本 ≥20 → 红（抽样退化）
     （角色：全在 ≥75% 时，探针看不见早边界行为，结论不能外推）

退出码：0 绿 / 3 红 / 4 样本不足（不下结论）/ 2 用法错。
样本不足不是绿：samples<MIN_OK 时明确输出 UNKNOWN，避免「没数据=没问题」。

用法：
  python3 scripts/compaction_probe.py                 # 一行 + 退出码
  python3 scripts/compaction_probe.py --json          # 机器读
  python3 scripts/compaction_probe.py --days 7        # 只看近 7 天事件
  python3 scripts/compaction_probe.py --fixture f.jsonl  # 用指定事件文件代替会话树
  python3 scripts/compaction_probe.py --selftest      # 双向反证：必须一红一绿
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
import datetime as dt
import glob
import json
import os
import statistics
import subprocess
import sys
import tempfile

SESSIONS = _DSH_ROOT
CHARS_PER_TOKEN = 4.0  # 与 compact_summary_audit.py 同口径，粗估即可（只用于阈值）

RATE_MIN = 0.60
RATIO_MED_MIN = 0.05
MIN_OK = 10          # 成功样本少于这个数不下红绿结论
BOUNDARY_MIN_N = 20  # 样本够这个数才判「抽样退化」

RED, GREEN, UNKNOWN = "RED", "GREEN", "UNKNOWN"
STAGE_EVENTS = ("compaction/start", "compaction/summary", "compaction/end",
                "compaction/prune")


# ---------------------------------------------------------------- 读事件
def iter_session_files(root: str):
    """Session files under root (absolute paths). Format knowledge lives in adapters/dsh.py."""
    return _dsh_files(root)


def read_events(path: str):
    """解压一个会话文件 → (压缩事件列表, 整会话最大 seq)。zstd 失败返回 (None, 0)。

    会话最大 seq 必须遍历**全部**行取，不能只在压缩事件里取：压缩事件的 seq
    只覆盖压缩发生点，用它做分母会把边界位置系统性放大。
    """
    events, max_seq = _dsh_read_events("", path)
    if not events and max_seq == 0:
        return None, 0
    return [e for e in events if str(e.get("type", "")) in STAGE_EVENTS], max_seq


def load_events(fixture: str | None, days: int | None):
    """返回 [(session_key, events)]。fixture 存在时只读它。"""
    if fixture:
        evs, max_seq = [], 0
        with open(fixture, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                e = json.loads(line)
                max_seq = max(max_seq, e.get("seq") or 0)
                if str(e.get("type", "")) in STAGE_EVENTS:
                    evs.append(e)
        return [("fixture", sorted(evs, key=lambda e: (e.get("seq") or 0)), max_seq)]

    cutoff = None
    if days:
        cutoff = (dt.datetime.now() - dt.timedelta(days=days)).timestamp() * 1000
    out, skipped = [], 0
    for p in iter_session_files(SESSIONS):
        evs, max_seq = read_events(p)
        if evs is None:
            skipped += 1
            continue
        if cutoff:
            evs = [e for e in evs if (e.get("time") or 0) >= cutoff]
        if evs:
            out.append((os.path.relpath(p, SESSIONS), evs, max_seq))
    if skipped:
        print(f"warn: {skipped} 个会话文件解压失败，已跳过", file=sys.stderr)
    return out


# ---------------------------------------------------------------- 度量
def summarize(sessions) -> dict:
    """纯函数：给 [(key, events)]，出指标。selftest 直接喂合成事件。"""
    tx = ok = err = prune_n = prune_tok = 0
    ratios, positions, providers, models = [], [], {}, {}
    err_reasons = {}
    for item in sessions:
        evs = item[1]
        max_seq = item[2] if len(item) > 2 else (max((e.get("seq") or 0) for e in evs) if evs else 0)
        for e in evs:
            d = e.get("data") or {}
            t = e.get("type")
            if t == "compaction/start":
                tx += 1
            elif t == "compaction/prune":
                prune_n += 1
                prune_tok += int(d.get("shadowedTokenCount") or 0)
            elif t == "compaction/end":
                msg = str(d.get("error") or "")
                if msg:
                    err += 1
                    key = msg.split("\n")[0][:120] or "(empty)"
                    err_reasons[key] = err_reasons.get(key, 0) + 1
            elif t == "compaction/summary":
                ok += 1
                shadowed = int(d.get("shadowedTokenCount") or 0)
                text = str(d.get("summary") or "")
                if shadowed > 0 and text:
                    ratios.append((len(text) / CHARS_PER_TOKEN) / shadowed)
                rng = d.get("shadowedRange") or {}
                end = rng.get("end")
                if isinstance(end, (int, float)) and max_seq > 0:
                    positions.append(end / max_seq)
                p = str(d.get("provider") or "?")
                providers[p] = providers.get(p, 0) + 1
                m = str(d.get("model") or "?")
                models[m] = models.get(m, 0) + 1

    rate = (ok / tx) if tx else 0.0
    silent = max(0, tx - ok - err)   # 起了但既无摘要、end 也没报错：静默空转
    buckets = {"<25%": 0, "25-50%": 0, "50-75%": 0, ">=75%": 0}
    for pos in positions:
        buckets["<25%" if pos < .25 else "25-50%" if pos < .5
                else "50-75%" if pos < .75 else ">=75%"] += 1
    early = buckets["<25%"] + buckets["25-50%"]

    return {
        "transactions": tx, "successes": ok, "errors": err, "silent_no_summary": silent,
        "success_rate": round(rate, 4),
        "ratio_median": round(statistics.median(ratios), 4) if ratios else None,
        "ratio_n": len(ratios),
        "ratio_min": round(min(ratios), 4) if ratios else None,
        "ratio_max": round(max(ratios), 4) if ratios else None,
        "boundary": buckets, "boundary_early": early, "boundary_n": len(positions),
        "prune_count": prune_n, "prune_tokens": prune_tok,
        "providers": providers, "models": models,
        "error_reasons": dict(sorted(err_reasons.items(), key=lambda kv: -kv[1])[:5]),
    }


def verdict(m: dict):
    """返回 (状态, 原因列表)。原因列表空 = 绿。"""
    reasons = []
    if m["successes"] < MIN_OK:
        return UNKNOWN, [f"samples={m['successes']} < {MIN_OK}，不下结论"]
    if m["success_rate"] < RATE_MIN:
        reasons.append(f"R1 成功率 {m['success_rate']:.1%} < {RATE_MIN:.0%}")
    if m["ratio_median"] is not None and m["ratio_median"] < RATIO_MED_MIN:
        reasons.append(f"R2 保留率中位 {m['ratio_median']:.2%} < {RATIO_MED_MIN:.0%}"
                       "（CompAct 目标 10%）")
    if m["boundary_n"] >= BOUNDARY_MIN_N and m["boundary_early"] == 0:
        reasons.append(f"R3 边界退化：{m['boundary_n']} 次成功无一在进度 <50%")
    return (RED if reasons else GREEN), reasons


def top_error(m: dict) -> str:
    if not m["error_reasons"]:
        return "-"
    k, v = next(iter(m["error_reasons"].items()))
    return f"{v}× {k[:60]}"


def one_line(m: dict, status: str, reasons, day: str) -> str:
    ratio = "n/a" if m["ratio_median"] is None else f"{m['ratio_median']:.2%}"
    b = m["boundary"]
    return (f"{day} compaction {status} tx={m['transactions']} ok={m['successes']} "
            f"rate={m['success_rate']:.1%} ratio_med={ratio}(n={m['ratio_n']}) "
            f"boundary[<50%:{m['boundary_early']} 50-75:{b['50-75%']} >=75:{b['>=75%']}] "
            f"silent={m['silent_no_summary']} prune={m['prune_count']}/{m['prune_tokens']}tok "
            f"toperr={top_error(m)}"
            + ("  ⟵ " + "; ".join(reasons) if reasons else ""))


# ---------------------------------------------------------------- 自测
def _ev(t, seq, time_ms, **data):
    return {"type": t, "seq": seq, "time": time_ms, "data": data}


def _events(n_ok, n_err, ratio, end_lo, end_hi, session_max=200, shadowed=40000):
    """造一段会话事件：n_ok 次成功 + n_err 次失败，边界位置落在 [end_lo,end_hi]。"""
    evs, seq, t0 = [], 1, 1795000000000
    total = n_ok + n_err
    for i in range(total):
        evs.append(_ev("compaction/start", seq, t0 + i, compactionId=f"c{i}"))
        seq += 1
        if i < n_ok:
            end = end_lo + (end_hi - end_lo) * i / max(1, n_ok - 1)
            evs.append(_ev("compaction/summary", seq, t0 + i, compactionId=f"c{i}",
                           summary="x" * int(ratio * shadowed * CHARS_PER_TOKEN),
                           shadowedRange={"start": 1, "end": int(end)},
                           shadowedTokenCount=shadowed, provider="p", model="m"))
        else:
            evs.append(_ev("compaction/end", seq, t0 + i, compactionId=f"b{i}",
                           error="pi-ai detected context overflow for model x"))
        seq += 1
        evs.append(_ev("compaction/end", seq, t0 + i, compactionId=f"c{i}"))
        seq += 1
    # 撑开会话长度：真实会话在压缩点之后还继续写事件
    evs.append(_ev("compaction/prune", session_max, t0,
                   shadowedRange={"start": 1, "end": 1}, shadowedSeqs=[1],
                   shadowedTokenCount=1000))
    return evs


def _healthy():
    return _events(12, 0, ratio=0.12, end_lo=30, end_hi=190)


def _broken():
    """12 次成功（R1 绿）但保留率 2% 且边界全在 ≥75% → 犯 R2；边界样本 12 < 20，R3 不参与。"""
    return _events(12, 0, ratio=0.02, end_lo=150, end_hi=196)


def _broken_r1():
    """成功率崩：2 成 10 败 → 样本不足，必须 UNKNOWN 而非绿。"""
    return _events(2, 10, ratio=0.12, end_lo=30, end_hi=190)


def selftest() -> int:
    fails = []

    def check(name, got, want, extra=""):
        tag = "ok " if got == want else "FAIL"
        print(f"[{tag}] {name}: {got}{(' ' + extra) if extra else ''}")
        if got != want:
            fails.append(f"{name}: 期望 {want} 实得 {got} {extra}")

    m_h = summarize([("healthy", _healthy(), 200)])
    st, why = verdict(m_h)
    check("健康基线判绿", st, GREEN, str(why))
    check("健康基线保留率≈12%", round(m_h["ratio_median"], 2), 0.12)
    check("健康基线边界早期样本>0", m_h["boundary_early"] > 0, True)

    m_b = summarize([("broken", _broken(), 200)])
    st, why = verdict(m_b)
    check("坏基线判红", st, RED, str(why))
    check("坏基线 R1 仍绿（独立性）", any(w.startswith("R1") for w in why), False)

    # 三条判据各自单独能红：从健康指标出发只动一根轴
    for axis, patch, want_prefix in [
        ("R1", {"success_rate": 0.20}, "R1"),
        ("R2", {"ratio_median": 0.02}, "R2"),
        ("R3", {"boundary_early": 0, "boundary_n": 25}, "R3"),
    ]:
        m = dict(m_h); m.update(patch)
        st, why = verdict(m)
        hit = [w for w in why if w.startswith(want_prefix)]
        check(f"仅 {axis} 违规须判红且点名 {axis}", (st, bool(hit)), (RED, True), str(why))
        others = [w for w in why if not w.startswith(want_prefix)]
        check(f"仅 {axis} 违规不得牵连其他判据", others, [])

    m_s = summarize([("few", _broken_r1(), 200)])
    st, why = verdict(m_s)
    check("样本不足判 UNKNOWN（不许当绿）", st, UNKNOWN, str(why))

    # 端到端：走真实入口，退出码必须跟着状态走
    for name, evs, want_rc in [("坏基线", _broken(), 3), ("健康基线", _healthy(), 0),
                               ("样本不足", _broken_r1(), 4)]:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            for e in evs:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            path = fh.name
        try:
            rc = main(["--fixture", path, "--json"])
        finally:
            os.unlink(path)
        check(f"e2e {name} 退出码", rc, want_rc)

    if fails:
        print("\nSELFTEST FAIL")
        for f in fails:
            print(" -", f)
        return 1
    print("\nSELFTEST PASS（坏基线红 / 健康绿 / 样本不足 UNKNOWN；三判据各自能单独红且互不牵连）")
    return 0


# ---------------------------------------------------------------- 入口
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本机压缩事务健康度探针（一行一日）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--days", type=int, default=None, help="只看近 N 天事件")
    ap.add_argument("--fixture", default=None, help="用 jsonl 事件文件代替会话树")
    ap.add_argument("--selftest", action="store_true", help="双向反证自测")
    ap.add_argument("--sessions", default=None, help="会话根目录（默认 ~/.dsh/sessions）")
    a = ap.parse_args(argv)

    if a.sessions:
        global SESSIONS
        SESSIONS = os.path.expanduser(a.sessions)
    if a.selftest:
        return selftest()

    sessions = load_events(a.fixture, a.days)
    m = summarize(sessions)
    status, reasons = verdict(m)
    day = dt.datetime.now().strftime("%Y-%m-%d")
    if a.json:
        print(json.dumps({"day": day, "status": status, "reasons": reasons,
                          "sessions": len(sessions), **m}, ensure_ascii=False))
    else:
        print(one_line(m, status, reasons, day))
    return {GREEN: 0, RED: 3, UNKNOWN: 4}[status]


if __name__ == "__main__":
    sys.exit(main())
