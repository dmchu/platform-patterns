# Alert rules that cannot fire

**Every alert rule needs a second mechanism whose only job is to prove the rule can still reach
a human — because a rule that cannot fire renders exactly like a rule with nothing to report.**

---

## The problem

An alert rule that is working, on a system with nothing wrong, shows one state: `Normal`, green,
no history. A rule whose metric was never scraped shows the same state. So does one whose
threshold sits above the ceiling of its own signal, one whose pending period is longer than the
signal stays visible, one whose evaluator is attached to a node that ignores evaluators, and one
that fires perfectly and is delivered to an inbox nobody reads.

Nothing in the platform distinguishes these. Each evaluates on schedule, reports healthy, and is
counted as coverage. In one estate of about 260 rules, these are the ways it happened in a single
quarter. Every one was found by accident, while doing something else.

| Where the rule died | What it looked like | How it was found |
|---|---|---|
| **The signal never existed.** The bad-event selector matched no series, so three of four service-level indicators read a constant `1.000000` for 92 days. | Perfect health on a dashboard | Writing burn-rate rules against them, and checking first |
| **The stream was gone.** The log stream a rule selected no longer existed; a sibling's stream existed but its line filter matched a log format the service had stopped using. | `Normal` | An audit that tested the *selector*, then had to be corrected to test the *filter* too |
| **Wrong scope.** The right resource name in the wrong account. | Paged with a static summary claiming a value it never measured | The page |
| **A regex that never extracts.** One rule filtered on a label its own parser never produced; four selected a pod family that never logs the event, 0 lines in 7 days against half a million scanned. | `Normal` | Pulling on one odd-looking contact point |
| **A threshold above the ceiling.** `15,000` blocked requests per window, chosen while a six-night scan inflated the baseline. The scan stopped nine days before the rule reached production; the maximum since was `12,212`. | Live, armed, 17 days | Going to write the same rule and finding it already there |
| **A threshold sixty times the signal.** A per-failure metric that reads `0.017`, thresholded at `> 1`. | `Normal` through a real incident | A payout timeout that should have paged |
| **A window shorter than the emission.** A 5-minute count against a log each item emits every ~22 minutes: 232 no-data gaps across one incident, a maximum of 5 against a true stuck set of 8. | Flapping, so people learned to ignore it | Replaying the expression over the incident |
| **A pending period longer than the signal is visible.** A sample that lands four minutes late is visible to a `[5m]` window for about one evaluation; `for: 5m` can never complete. | 25 `Pending → Normal` transitions in a week, 0 `Alerting` | Reading state history |
| **An evaluator on the wrong node.** A `> 2` threshold attached to a *reduce* node, which ignores evaluators; the effective condition was "any non-zero". | Fired on every single line | A colleague asking why the channel was full |
| **It fired, and reached nobody.** Rules matching no route in the policy tree fell through to one person's inbox: 136 deliveries in 30 days. | Coverage, on paper | Tracing one delivery's route path |

Ten mechanisms, one symptom. The definition looks right in every case; several passed review.

---

## The mechanism

Treat every rule as a claim with three parts, and audit each part separately — because each dies
in a different place and a check on one says nothing about the others:

1. **The signal exists.** The metric is scraped or the stream is written, *in the scope the rule
   names*, and the rule's own filter still matches inside a window where the event genuinely
   occurred.
2. **The arithmetic can cross the line.** The threshold is below the signal's ceiling and above
   its floor, the window is longer than the emission cadence, and the value the expression
   produces is the unit the threshold was written in.
3. **The condition is wired.** The condition points at a threshold node; the pending period is
   shorter than the signal stays visible; missing data and errors resolve to the state you
   intended; and the receiver is a place a human looks.

Then two tests that do not require reading a single rule definition:

**State history is the audit trail.** A rule with `Pending` entries and no `Alerting` entry is
dead at part 3. A rule whose history is only `NoData` is dead at part 1. A rule with no history at
all over a window longer than its threshold event's rate has never been asked. All three are
visible without opening the JSON, and the JSON would not have shown them anyway.

**The evaluator is the positive control.** The platform exposes its rule engine for a one-off
run (Grafana: `POST /api/v1/eval`): send the rule's own query chain with a widened window and
read what the threshold node returns. Run it twice — once as written, once with the threshold set to zero — on a window
that contains a known real event. As-written returns nothing and the zero-threshold run returns
something: the arithmetic is dead. Both return nothing: the signal is dead. That is a
five-minute test, and it would have caught six of the nine rows above that die before delivery.

[`reference/audit.py`](reference/audit.py) walks a whole estate through the history test and the
definition checks that *can* be made statically, and prints one line per finding. It reads only.

---

## How to use it

```bash
export GRAFANA_URL=https://your-stack.grafana.net
export GRAFANA_TOKEN=...            # a read-only service account token
python3 reference/audit.py --days 14
```

```
EVALUATOR_ON_REDUCE      q3w8e1r7t5y2u9  'Fatal errors (production)'  condition B is reduce; evaluator [2] is ignored
FOR_EXCEEDS_WINDOW       h4j6k8l1z3x5c7  'Workflow failure (staging)'  for=5m, window=5m: check the signal's cadence
NODATA_ONLY              v9b2n4m6a8s1d3  'Successful transactions'     41 transitions, all NoData
PENDING_NEVER_ALERTING   f5g7h9j1k3l5p7  'Onboarding workflow failed'  25 pending, 0 alerting in the window
audited 262 rules, 3,118 history entries: 4 findings
```

The identifiers and titles above are invented; the findings are the real ones.

Then the part the script cannot do: for each finding, run the evaluator control on a window with
a known event, and read the receiver end to end. A rule that passes every static check and fires
into an unread inbox has passed nothing.

---

## What fails silently

**The audit that audited nothing.** An API token scoped to the wrong organisation, a datasource
renamed, a history endpoint that quietly returns an empty array on a stack without history
enabled — each produces a clean report. The reference refuses to exit green unless it audited at
least one rule and read at least one history entry, for the same reason this repository's CI
plants a secret before trusting its own scan: **a check that cannot be seen to fail has not been
seen to work.**

**History has a horizon.** State history here is capped at 31 days. During one investigation the
first half of the incident had already aged out by the time anyone looked, and the evidence of
what the old rule had reported was gone with it. Capture the history *before* changing the rule,
not after.

**The blanket fix that pages continuously.** The obvious repair for "no data looks like health"
is to make no-data alert. Applied across a filtered set of twelve absence-shaped rules, it paged
continuously on eight of them, because for those rules absence *is* health. Change the no-data
state only where the selector is proven dead.

**The repair that converts silence into a siren.** `or vector(0)` turns "no series" into a real
zero so that a `< N` rule can fire on absence — it is the correct idiom for a liveness rule.
Applied to a rule whose filter is dead, it converts "never fires" into "fires permanently". This
was one edit away from shipping as a one-line fix. Establish that the filter is alive first, then
add the guard.

**The two individually-correct settings that together hide an outage.** A rule with no-data
treated as OK *and* `or vector(0)` in its expression renders a total telemetry outage as 0% bad,
which is indistinguishable from perfect health. Each choice was right on its own; the combination
needs an independent heartbeat, which is the [agentless ingestion](../agentless-log-ingestion/)
pattern's whole point.

**It fires, and still reaches nobody.** The audit has to end at the receiver. The rules that fell
through to one inbox would have passed every check on this page. What caught them was the
notification record, which encodes the route path traversed, not just the destination. See the
[alert relay](../alert-relay/) for the delivery half.

---

## Decisions

See [ADR-006](../../docs/decisions/006-audit-history-not-definitions.md) for why the audit reads
history and the evaluator rather than the rule definitions, and why per-rule synthetic firing was
rejected at this size.
