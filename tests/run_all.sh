#!/usr/bin/env bash
# Every probe must prove it can fail before it is allowed to prove anything else.
# 0 = all green.
set -u
cd "$(dirname "$0")/.."
fail=0
for p in probes/compaction_probe.py probes/compact_summary_audit.py probes/agent_log_census.py; do
  printf '\n== %s ==\n' "$p"
  python3 "$p" --selftest || fail=1
done
printf '\n== examples (offline, no session logs needed) ==\n'
python3 probes/compaction_probe.py --fixture examples/healthy.jsonl; echo "healthy exit=$?  (expect 0)"
python3 probes/compaction_probe.py --fixture examples/broken.jsonl;  echo "broken  exit=$?  (expect 3)"
echo
if [ "$fail" -eq 0 ]; then echo "ALL PROBES PASS"; else echo "SOMETHING FAILED"; fi
exit "$fail"
