# ADR-005 — Fan out at the event bus, never chain behind the queue

**Status:** accepted · **Context:** several consumers want the same capacity-reclaim events

## Decision

One queue per cluster, consumed by exactly one consumer: the autoscaler's interruption
controller. Every additional consumer is added as an **additional target on the event-bus rule**,
with its own delivery path.

## Why

A queue delivers each message once. Add a second consumer and the notifier and the drain
**compete for the same messages** — so some reclaims are announced to a human and never drained,
and some are drained and never announced. Both halves are intermittently broken, and the symptom
is non-deterministic, which is the worst kind to debug.

The event bus already fans out natively. Using it costs one extra target and nothing else.

## The rejected option, and why it is tempting

Chaining — queue → controller → and have something forward from there — looks like less
infrastructure and keeps one ingestion point. It fails for a second reason beyond message
stealing: **it makes the notifier's liveness depend on the controller's**, so the component you
would use to discover that the controller is broken shares its fate.

## How it is made hard to get wrong

The rule set is a map that consumers extend, so adding a notifier creates a new target rather
than editing an existing one. Making the safe change the easy change does more than a comment
warning against the unsafe one.

## Consequences

- Each consumer has independent delivery, retry and failure. One being broken is visible.
- The queue's configuration is tuned for exactly one consumer, so retention can be sized against
  that consumer's worst-case downtime rather than against the slowest of several.
- Every new consumer is a permission and a cost, which is the right amount of friction for
  something subscribing to a production event stream.

## Reopen if

A consumer needs at-least-once delivery with its own retry semantics. Then it gets its **own
queue** as a second bus target — still not a second consumer on the first queue.
