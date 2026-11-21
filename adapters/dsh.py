"""DeepSeek Harness (DSH) session-log adapter.

This is the ONLY file in the repo that knows DSH's on-disk layout and event
names. Porting the probes to another harness = writing another adapter with
the same five functions.

Layout it reads:  <root>/<workspace>/<session>/session.v4.jsonl.zstd
Event names used: compaction/start | compaction/summary | compaction/end | compaction/prune,
                  tool/call | tool/result

Two traps this adapter exists to absorb:

1. Workspace directory names may start with ``--`` (this machine's is
   ``--home-<user>-<project>--``). Passed to ``zstd`` as a bare argument they
   are parsed as options; hence the ``./`` prefix on relative paths.
2. A session's length for "where did X happen" purposes is the max ``seq`` of
   the WHOLE session, not of the events you happen to be scanning. Using the
   events scanned so far truncates the denominator and inflates positions.
"""
from __future__ import annotations

import json
import os
import subprocess

SESSIONS_ROOT = os.path.expanduser("~/.dsh/sessions")
PATTERN = "*/*/session.v4.jsonl.zstd"

COMPACTION_TYPES = ("compaction/start", "compaction/summary",
                    "compaction/end", "compaction/prune")


def _resolve(root: str, relpath: str) -> str:
    """Return a path safe to hand to zstd (never starts with '-')."""
    p = relpath if os.path.isabs(relpath) else os.path.join(root, relpath)
    return p if os.path.isabs(p) else "./" + p


def iter_session_files(root: str = SESSIONS_ROOT) -> list[str]:
    """Session files under root, as paths relative to root, sorted."""
    import glob
    return sorted(glob.glob(os.path.join(root, PATTERN)))


def read_lines(root: str, relpath: str):
    """Decompressed lines of one session file. Unreadable file yields nothing."""
    r = subprocess.run(["zstd", "-dc", _resolve(root, relpath)], capture_output=True)
    if r.returncode != 0:
        return
    yield from r.stdout.decode("utf8", "replace").splitlines()


def iter_events(root: str, relpath: str):
    """Tolerant JSONL iterator: bad lines are skipped, not fatal."""
    for line in read_lines(root, relpath):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def read_events(root: str, relpath: str):
    """(compaction events, whole-session max seq) for one session file."""
    events, max_seq = [], 0
    for line in read_lines(root, relpath):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        max_seq = max(max_seq, e.get("seq") or 0)
        if str(e.get("type", "")) in COMPACTION_TYPES:
            events.append(e)
    return events, max_seq
