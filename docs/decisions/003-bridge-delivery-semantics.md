# ADR-003 — Acknowledge the log drain, fail the event webhook

**Status:** accepted · **Context:** a telemetry bridge with two ingest paths

## Decision

On a backend write failure: the **log drain** path swallows the error and returns success; the
**event webhook** path lets it propagate and returns a server error so the platform retries.

Two paths in one service, deliberately opposite.

## Why

**Ask for a retry only when you can make it idempotent.**

The drain delivers batches of records with no stable per-record delivery identifier. The
platform's retry is at batch granularity. So a retry cannot be deduplicated — it can only
duplicate, and duplicated log lines are both a cost and a correctness problem for anything
counting them. Acknowledging and accepting the loss is the lesser harm.

The webhook carries a delivery id. That makes the retry deduplicable, which makes asking for one
safe. And these events drive counters that alerts threshold on, where a gap is worse than a
duplicate — the opposite trade from the drain.

## The coupling this creates

The dedupe and the error response are **one decision, not two**. Remove the dedupe and the 5xx
becomes a duplication generator. Anyone touching either must know about the other, which is why
it is written here and not only in a comment.

## Known limitation, accepted

The dedupe is per-instance and in-memory, so it does not hold across a cold start or a
scale-out — which is to say, during a burst, which is when redelivery happens. Making it hold
means shared state and a real dependency. Accepted deliberately at this volume; the trigger to
revisit is any observed duplicate in the counters the events feed.

## Reopen if

The platform adds per-record delivery identifiers to the drain, at which point both paths can
have the same semantics and this ADR becomes unnecessary.
