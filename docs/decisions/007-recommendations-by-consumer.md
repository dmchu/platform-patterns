# ADR-007 — Judge aggregation recommendations by their consumers, not by the engine's usage counts

**Status:** accepted · **Context:** a 467-row recommendation queue that netted +18,800 series
when read as the savings it was labelled

## Decision

Every recommendation is judged by the dashboards and alert rules that read the metric — files we
can open — and by shape checks on the proposed rule; the engine's usage counts are evidence that
a query happened, never that anyone needs the labels. The queue's net direction is printed before
any row. Verdicts travel as pull requests against the rules file, never as console clicks.

## Why the obvious option lost

**Apply the queue, or leave auto-apply on.** The page is called recommendations and the column
savings; the queue's own sum was +18,800 series, about 60% on top of the billable count, carried
by 55 `remove` rows, 45 of them backed by one or two ad-hoc queries and nothing else. Auto-apply
had been switched off in August; had it stayed on, the removes that arrived in mid-September
would have applied themselves, and nothing would have shown the direction.

**Trust `usages_in_queries` as a consumer.** It is the engine's whole basis for a `remove`, and
it is a count with no name attached: the platform's own query-insight log held no query touching
the metrics behind the largest removes, because the engine counts at the storage layer. A person
in Explore or an agent answering a question is a query, so the count rewards curiosity, ages out
when curiosity stops, and the recommendation reverses with it.

**Review by hand in the console.** The console shows one estimate per row and never the sum,
cannot render a protected label or a split pair as anything but a row, and its Apply button
writes a rule the repository does not have.

## What made the chosen option work

The export carries enough to compute direction (`recommended − current` per row; the raw count
stands in for recommended on a `remove`), and beside the rules file every shape defect is visible
statically. Consumers are a finite set of JSON files whose `expr` fields name their metrics;
matching by name, with histogram families expanded, turns "was it queried" into "is it read".
The pair check reads across rows, which the per-metric engine never does; the two largest
savings, about 1.3k series, came from it.

## Consequences

- The review is read-only and takes seconds; it refuses a green result over an empty queue or
  over a queue reviewed without consumers.
- It ends at the name: a dashboard that reads a metric may still not need a restored label, so
  those rows are review items, not verdicts.
- A lone pair side's sibling is not in the export; its series count comes from the cardinality
  API by hand. The queue itself is worth about 1% of the overage; the overage lives in an
  exempted family and a different decision.

## Reopen if

The engine distinguishes dashboard and rule consumers from ad-hoc queriers in the recommendation
itself; supports protected labels natively; or recommends across a histogram's families at once,
so that applying one row cannot split a pair.
