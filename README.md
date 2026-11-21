# compaction-probe

**Verification-first probes for LLM agent context compaction.** Three small, zero-dependency
Python scripts that answer three questions with a number and an exit code:

1. **Is compaction working at all?** → transaction success rate
2. **How much does it actually keep?** → realized retention ratio + what the summary preserved
3. **Which failures are worth fixing?** → cross-session failure signature clustering

Built while measuring a local coding-agent harness (806 session logs). The numbers below are real
readings from 2026-11-20, not synthetic. Every probe ships with `--selftest` that must **fail on a
broken baseline before it is allowed to pass on a healthy one**.

```
$ python3 probes/compaction_probe.py
2026-11-20 compaction RED tx=403 ok=62 rate=15.4% ratio_med=2.43%(n=62) \
  boundary[<50%:40 50-75:18 >=75:4] silent=0 prune=564/2434529tok \
  toperr=339× pi-ai detected context overflow for model "..." \
  ⟵ R1 成功率 15.4% < 60%; R2 保留率中位 2.43% < 5%（CompAct 目标 10%）
```

## Why this exists

Compaction is usually instrumented as "did it run?" — which is the one question that never has an
interesting answer. The interesting answers are quantitative, and they are all ratios whose
**denominator is easy to get wrong**:

| Reading | Value | What it means |
|---|---|---|
| transaction success rate | **62 / 403 = 15.4%** | compaction is attempted constantly and mostly does not produce a summary |
| realized retention ratio (median) | **2.43%** (range 0.39–6.31%) | against a 10% target, we keep about a quarter of what we meant to |
| where compaction happens (median session progress) | **43.4%** | mid-session, not at the tail: <50% 40, 50–75% 18, ≥75% 4 |
| summaries missing a "verified / unverified" flag | **13 / 62 = 21.0%** kept it, i.e. 49 missing | numbers survive, *provenance* does not |
| summaries missing open objectives | **16 / 62** | a quarter of summaries do not say what is still unfinished |
| tool calls counted as failures by text regex | 7.52% | vs **1.34%** by the authoritative `isError` bit — a **5.6× false-positive rate** |

The last row generalizes: **a log scanner that greps for "error" measures the corpus, not the
harness.** `agent_log_census.py` uses `tool/result.message.isError`; the regex version inflated
1,510 real failures into 8,444 phantom ones.

## The three probes

### `probes/compaction_probe.py` — daily one-liner, red = exit 3

Pre-registered criteria (changing a threshold means changing the docstring and saying why):

| id | criterion | red when | rationale |
|---|---|---|---|
| R1 | summary / start | `< 60%` | a failed compaction costs a whole turn of context; sub-60% is normal failure, not jitter |
| R2 | median summary-tokens / shadowed-tokens | `< 5%` | CompAct's default target is 10%; below half of it you are outside the regime the paper measured |
| R3 | share of successful compactions below 50% session progress | `= 0` with `n ≥ 20` | if you only ever sample the tail, you cannot say anything about early-boundary behaviour |

Exit codes: `0` green · `3` red · `4` **insufficient samples** · `2` usage error.

`UNKNOWN` is deliberately *not* green — the most common way a probe lies is by reporting "no data"
as "no problem".

### `probes/compact_summary_audit.py` — what did the summary actually preserve?

Scores each compaction summary against a six-field rubric:

| field | coverage here |
|---|---|
| state / progress | 61/62 (98.4%) |
| open objectives | 46/62 (74.2%) |
| constraints | 60/62 (96.8%) |
| evidence pointer | 62/62 |
| numbers | 62/62 |
| **unverified flag** | **13/62 (21.0%)** |

Mean score 4.9/6. The gap between "numbers: 100%" and "unverified: 21%" is the interesting finding:
a summary can carry every figure forward while dropping the fact that some of them were never
confirmed — which is exactly how a later turn confidently asserts something nobody verified.

### `probes/agent_log_census.py` — which failures recur across sessions?

Clusters tool failures by signature and reports how many distinct sessions each one blocked.
A failure that blocks ≥2 sessions is a repair candidate; a failure that appears once is noise.
Readings here: 112,843 tool results, 1,510 failures (1.34%), 374 signatures, **118 spanning ≥2
sessions**. Top three: `web_fetch` 30s timeout (84 sessions / 160 events), `bash` missing a required
`description` (55/199), `edit` with a stale `old_string` (28/62).

It also attributes each signature to one of four buckets — environment/channel, harness contract,
guard working as designed, model behaviour — because **a guard rejecting bad input is not a bug**,
and counting it as one is how "reliability" numbers get inflated.

## Quickstart

```bash
git clone https://github.com/aidless/compaction-probe && cd compaction-probe
bash tests/run_all.sh          # all three --selftest + offline fixture demo
```

Offline demo needs no session logs — the fixtures are synthetic:

```bash
python3 probes/compaction_probe.py --fixture examples/healthy.jsonl   # exit 0
python3 probes/compaction_probe.py --fixture examples/broken.jsonl    # exit 3
```

Against real logs (DSH layout is the bundled adapter):

```bash
python3 probes/compaction_probe.py                 # one line + exit code
python3 probes/compaction_probe.py --json          # machine-readable
python3 probes/compaction_probe.py --days 7        # recent events only
python3 probes/compact_summary_audit.py            # six-field coverage table
python3 probes/agent_log_census.py                 # failure signatures by session count
```

Daily cron, one line per run:

```cron
20 9 * * * cd /path/to/compaction-probe && python3 probes/compaction_probe.py >> ~/.dsh/logs/compaction-probe.log 2>&1; echo "exit=$?" >> ~/.dsh/logs/compaction-probe.log
```

## Porting to another harness

Everything harness-specific lives in `adapters/`. Five functions to reimplement:

```python
SESSIONS_ROOT                      # where logs live
iter_session_files(root) -> list   # session log paths
read_lines(root, path)             # decompressed lines (jsonl)
iter_events(root, path)            # tolerant JSONL iterator
read_events(root, path) -> (events, max_seq)   # compaction events + whole-session max seq
```

`read_events` returning `max_seq` is not a convenience — it is the fix for one of the two bugs
below. A session's length for "where did this happen" purposes is the max `seq` of the **whole
session**, and an adapter that only sees compaction events cannot know it.

## Two instrument bugs this repo exists to prevent

Both were found *after* the first version had already produced publishable-looking numbers, and both
changed a conclusion.

### 1. The denominator that truncates itself

Reading events in a stream and computing `position = shadowedRange.end / max(seq seen so far)`
silently truncates the denominator at the moment of the event. Result: **55 of 62 readings differed
by more than 20 percentage points**, median position inflated from 0.434 to 0.876.

- reported (wrong): "all 62 compactions happen after 50% of the session, 85.5% after 75%, none before 50%"
- actual: median **43.4%**; `<50%` 40, `50–75%` 18, `≥75%` 4

A conclusion ("we only ever sample the tail, so we cannot see early-boundary behaviour") was built on
the artifact and had to be withdrawn. The fix: `max_seq` over all lines, not over the lines you
happen to be scanning.

### 2. An event type that does not exist

The scanner counted `compaction/error` events. There are none — the four types that exist are
`prune` / `start` / `end` / `summary`. Failures live in **`compaction/end.data.error`**. Looking for
the wrong type yields a self-contradictory reading that is easy to miss: `errors=0` while
`tx=403, ok=62`.

**The general shape:** a ruler that cannot see a category reports 0 for that category, and 0 looks
like a clean result. Hence R1/R2/R3 are pre-registered, and hence `--selftest` feeds a broken
baseline through the *real* entry point and asserts `exit=3`.

## What this cannot tell you

- **Compaction quality.** Every probe here measures the compaction *event*, not whether the model
  resumed well afterwards. That requires replaying a task with and without the summary — see
  CompAct-Bench (`arXiv:2611.25673`) for the protocol; this repo measures only the cheap half.
- **Causality.** R2 < 5% says retention is low; it does not say whether the cause is the summarizer
  prompt, the model, or the budget it was given.
- **Anything cross-harness.** The thresholds are calibrated on one harness and ~800 sessions. Treat
  them as a starting prior, re-derive them on your own logs, and keep the `--selftest`.

## License

MIT.
