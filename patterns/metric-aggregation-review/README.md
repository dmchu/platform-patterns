# Aggregation recommendations judged by their consumers

**A recommendation queue labelled *savings* renders the same whether it shrinks the bill or
grows it, because each row is judged alone — so print the queue's net direction before any row,
and let a row be justified only by a dashboard or alert rule that reads the metric, never by a
count of who queried it.**

---

## The problem

Grafana Cloud Adaptive Metrics watches what is queried and recommends, per metric, an
aggregation rule that drops the labels nobody reads: `add` a rule, `update` one, `keep` one, or
`remove` one because the metric was queried with labels the rule drops. Each row carries the
engine's usage counts (`usages_in_rules`, `usages_in_dashboards`, `usages_in_queries`), a
current and a recommended series count, and the drop set. With auto-apply on, the queue applies
itself. With it off, the console offers each row with an **Apply** button, and an **Apply all**.

The queue reviewed here had 467 rows. Read as the page labels it — a list of savings — it was this:

| action | rows | series if applied | behind the rows: rules / dashboards / ad-hoc queries |
|---|---|---|---|
| remove | 55 | **+19,785** | 0 / 6 / 51 |
| update | 36 | −164 | 0 / 3 / 3 |
| add | 28 | −631 | 0 / 7 / 5 |
| keep | 348 | −190 (stale statistics) | 0 / 31 / 17 |
| **total** | 467 | **+18,800** | the engine's own `estimated_savings_series`: −18,803 |

The queue was an expansion. The engine's own savings estimate had read about +300 in early
August, slightly negative once auto-apply was turned off, and −16k to −19k from mid-September:
a saving that had become an expansion of the same size. The step was the 55 `remove` rows, each
saying "drop this aggregation rule because the metric was queried". 45 of the 55 had no
dashboard and no alert rule behind them — one or two ad-hoc queries each. About 17k of the
+19.8k came from one client library's runtime metrics, today collapsed to 1–3 series each from
650–1,392 raw. **Apply all** would have added about 60% to a billable count already at 30,418
series against 10,000 included.

None of this is visible in the console, which shows one row at a time, each with its own
estimate, and never the sum. Every row is plausible on its own: the metric *was* queried, the
labels *were* read.

Done carefully — every row judged by who reads the metric — the same queue nets about −240
series, about 1% of the overage.

---

## The mechanism

Treat the queue as three questions, in this order, and let a script answer the ones it can.

**1. Which way does it point?** Sum the per-row deltas by action before reading a single row.
The delta is in the export: `recommended_series_count − current_series_count`; for a `remove`
the recommended count is the raw count, which is why removes are large and positive. The sum
reproduces the engine's own total to within 3 series. A queue whose first line is `+18,800` is
reviewed differently from one whose first line is `−18,803`, and the console shows neither.

**2. Who reads the metric?** A usage count is evidence of a query, not of a consumer. The export
does not say who ran it; investigations count, including an agent's; and the count ages out of
the engine's window, at which point the recommendation reverses. The consumers that matter are
files you can open: dashboards and alert rules. [`reference/review.py`](reference/review.py)
extracts every metric name from their `expr` fields (or from a flat list of expressions),
expands `_bucket`/`_sum`/`_count` to the whole family when a histogram function is used, and
matches rows by exact name. A `remove` with no dashboard and no rule behind it is rejected
whatever its query count. One with a dashboard is a review item, because the current rule's kept
labels usually already serve the panel — they did in every such case here.

**3. Is the proposed rule the right shape?** Four shapes that are wrong on their face, each a
named check. The `count` and `_max` checks also run over the rules the engine says to `keep`; a
hit there is reported as a defect of the live rule (`LIVE_RULE_SHAPE`), not of a proposal:

- a drop of a protected label (default `cluster`, configurable) → modify: keep it, drop the rest;
- `count` as the only aggregation on anything but an `_info` gauge → reject: `count` stores the
  number of contributing series, not the value;
- a `_max` metric aggregated with `sum` and `count` → only `max` is valid;
- an `update` that only restores labels no consumer asked for → reject; one that restores some
  and drops others → take the drops.

And one check that reads across rows. `_sum`, `_count` and `_bucket` are one object stored as
three series families, and they must end with the same drop set or every average and quantile
join breaks. A `remove` of one side while the other stays ruled is a split. A ruled side whose
sibling appears in no input is listed separately, under *not recommended by the engine*, because
that is where the savings were: two `_count` families — 976 and 345 raw series — sat
unaggregated beside their collapsed `_sum`. Mirroring the `_sum` drop set onto them is about
−1.3k series, five times the whole queue's adds, and it repairs both pair defects. The engine
recommends per metric; it splits pairs and never mends them.

Every count field in the export is optional per row — the four removes of metrics that no longer
exist carried no counts at all — so each check tolerates a missing field, and a missing count is
printed as `?`, not as 0.

Then the adoption path, the same for every verdict: the rules file lives in a repository; a
verdict becomes a pull request against it; a repository-to-platform sync job's dry run shows the
live diff; merging applies with an etag guard against a concurrent console change. Never the
console buttons — they bypass all of it.

---

## How to use it

```bash
# the export: GET /aggregations/recommendations?verbose=true   the rules: GET /aggregations/rules
python3 reference/review.py --recommendations recommendations.json --rules rules.json \
    --consumers dashboards/ alert-rules.json --protect-label cluster
```

```
queue 467 rows: remove 55 +19785 | update 36 -164 | add 28 -631 | keep 348 -190 | net +18800

REMOVE_QUERY_ONLY   worker_poll_latency_seconds_sum   +341  reject   2 queries, 0 dashboards, 0 rules: a query is not a consumer; who ran it is not in the export and the count ages out
RESTORES_LABELS     runtime_threads                      +3  reject   only restores workload; 0 queries, 0 dashboards, 0 rules: no consumer asked for them
DROPS_PROTECTED     db_pool_wait_seconds_count         -109  modify   drops cluster: keep it, drop the rest; the saving shrinks a little
MAX_AS_SUM          queue_depth_max                       ?  modify   aggregations count,sum,sum:counter on a _max metric: only max is valid
REMOVE_GONE         legacy_client_duration_ms_sum         ?  apply    no counts in the export: the metric is gone, the rule is housekeeping
REMOVE_HAS_CONSUMER orders_total                        +78  review   3 queries, 1 dashboards, 0 rules; the engine counts a dashboard reads it: check whether the current rule's kept labels already serve it
…
not recommended by the engine (pair consistency; the sibling's series are not in the export):
PAIR_LONE           http_request_duration_seconds_sum     ?  review   no _count/_bucket sibling in any input: if one exists it is unaggregated; mirror this drop set onto it
…

N add/update rows pass every check: apply as recommended (-N series)
verdicts: apply N, modify N, reject N, review N; net if followed -N series (modify rows at the engine's estimate); N consumer metric names read
```

Rejects first, then by check. The identifiers are invented; the shapes and the first line are
from the real review, the per-row figures are its order of magnitude. `--format json` gives the
same with the by-action totals as numbers. Exit 1 means a verdict was printed — `review` and
`apply` rows included, since both still need a hand — 2 means an input could not be read, and 3
means the script refused to call an empty result clean.

Then the part the script cannot do: for each `review` row, open the panel and read what it
groups by; for each `PAIR_LONE`, ask the cardinality API whether the sibling exists and how
large it is. The script's "net if followed" is an upper bound. On this queue the human
rejections it cannot make — a per-target scrape-health metric, a per-pod heap diagnostic, a
label a dashboard that does not exist yet will want — were the difference between its number and
−240.

---

## What fails silently

**A savings page that is an expansion.** Nothing in the product shows the queue's sum. Each row
is plausible, each estimate is per row, and the page is titled as if the direction were known.
With auto-apply on, no one is even asked. The first line the script prints is the only defence:
the net, before any row.

**Usage counts that name no one.** Thirty days of the platform's own query-insight log — 6,309
lines — held no query touching the metrics behind the largest removes. The engine counts at the
storage layer, below what the insight log records. So the mechanism was recoverable — un-aggregation
driven by queries — and the querier was not. A person in Explore, a scheduled report, an agent
answering a question: each is a query, each will produce a `remove` a window later, and each
`remove` will age out when the window passes unless the metric is queried again. A queue driven
by that tracks who was curious last month, not who depends on the metric. All 55 removes here
were of that kind.

**The pair the engine splits and never mends.** Each `_count` was unruled and unqueried and
therefore invisible to a recommender that only looks at rows; the check that reads across rows
is the only one that found the two largest savings.

**A `count` that is not a count.** `count` in an aggregation rule stores the number of
contributing series. On an `_info` gauge that equals the value. On a request-count metric it
replaces the request count with the number of series reporting one, and the panel keeps
rendering a line.

**`keep` rows with stale statistics.** 348 rows that change nothing still summed to −190 series,
because the engine's current counts lag the live state. The direction number carries that error:
here 190 against 18,800, and it will not always be that small.

**A green review over nothing.** An export from the wrong stack, a token without the scope, a
request without `?verbose=true`, a consumers directory that holds no dashboards: each produces a
report with nothing to reject. The script refuses an empty queue with a non-zero exit, exits on a
consumers path that does not exist, and refuses to call a queue clean when no consumer input —
or none that named a metric — was given to judge it against; the same rule this repository's CI
applies to its own redaction scan.

**The console button that forks the truth.** Applying one row in the console writes a rule the
repository does not have. From then on the sync job's dry run shows a diff nobody made, and the
etag guard — which exists for exactly this — turns every later merge into a conflict. The review
started by checking live against repository, 439 to 439, because a review of rules that are not
the rules is a review of nothing.

**The queue is 1% of the problem.** The series were where no recommendation pointed: one
request-duration family was 31% of everything and exempted by a deliberate decision about
production visibility. The queue's job was never to fix the bill. It was to not make the bill
worse, and left to itself it would have made it 60% worse.

---

## Decisions

See [ADR-007](../../docs/decisions/007-recommendations-by-consumer.md) for why recommendations
are judged by the dashboards and alert rules that read the metric rather than by the engine's
usage counts, and why applying the queue as labelled lost.
