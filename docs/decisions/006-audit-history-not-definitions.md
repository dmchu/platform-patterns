# ADR-006 — Audit alert rules by their history and the evaluator, not by their definitions

**Status:** accepted · **Context:** about 260 alert rules; in one quarter nine were found unable
to fire and a tenth fired into an unread inbox, each by accident

## Decision

The liveness audit reads what each rule *did* — its state history over a window, and what the
rule engine returns when the rule's own query chain is run against a window with a known event —
and checks the definition only for the few dead shapes that are visible statically.

## Why the obvious option lost

**Review the definitions.** Every one of the ten had a definition that read correctly. A selector for a stream that no longer exists looks like a selector. A threshold of
15,000 looks like a threshold; nothing in the JSON says the signal's ceiling is 12,212. A pending
period of five minutes looks reasonable until you know the sample lands four minutes late. An
evaluator on a reduce node is the one shape a reviewer *could* catch, and it passed review.
The failures live in the relationship between the rule and the signal, and the definition holds
only one side of it.

**Fire every rule synthetically.** The strongest possible test: inject the condition and watch the
page arrive. Rejected at this size. Most signals cannot be injected without harming the thing
they watch (a stuck payment, a reclaimed node), the platform's "test" button bypasses the routing
tree and so proves the wrong half, and at 260 rules the exercise is a project rather than an
audit. Kept for the handful of rules whose page tier justifies it.

**Flip the no-data state to alerting everywhere.** Measured: across twelve absence-shaped rules
it paged continuously on eight, because for those rules no data *is* health. A blanket setting
cannot tell a dead selector from a quiet week.

## What made the chosen option work

State history is a record the platform already keeps, keyed by rule, and the three dead shapes it
exposes — pending-never-alerting, only-no-data, never-asked — cover the failures that a
definition cannot show. The evaluator endpoint completes it: a rule run as written and again with
its threshold at zero, over a window containing a real event, separates "the arithmetic is dead"
from "the signal is dead" in one call each, with nothing changed and nothing paged.

## Consequences

- The audit is read-only and takes minutes, so it can run on a schedule rather than after an
  incident.
- It must refuse a green result over an empty estate or an empty history. The first version of
  the underlying tooling in this repository's estate did not, and a query returning zero over
  zero lines scanned was read as "no failures" more than once.
- History has a horizon (31 days here). Evidence about what a rule reported during an incident
  must be captured before the rule is changed, or it is gone.
- It ends at the rule. Delivery — that a firing rule reaches a human — is a separate audit against
  the notification record, and belongs to the relay pattern.

## Reopen if

The platform exposes a rule's *signal cardinality* alongside its state — the series or stream
count its selector resolves to at each evaluation. That would move the first of the three checks
from an audit into the rule's own health, and most of this document into a dashboard.
