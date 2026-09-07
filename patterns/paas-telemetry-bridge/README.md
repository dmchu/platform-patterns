# PaaS telemetry bridge

**A small owned HTTP shim that converts a managed platform's push-only telemetry into your
observability backend's ingest protocol — and uses the fact that it sits in the middle to do
three things neither end can.**

---

## When you need one

Your application platform emits telemetry only by POSTing to a URL you supply, in a format it
chose, signed with a shared secret, at whatever volume it produces. Your observability backend
accepts only its own wire protocol, authenticated its own way, and bills by volume or by series.

Any two of those three mismatches — **auth, shape, volume** — already means something has to sit
between them.

The fourth reason is the one people miss, and it is usually the most valuable: **the platform's
identifiers are not your application's identifiers.** The platform knows its own project name;
your traces are keyed on a service name. Only the thing in the middle can reconcile them, and
without that reconciliation the logs arrive and cannot be correlated with anything.

---

## The mechanism

```
platform  ──HMAC-signed POST──▶  bridge  ──▶  backend
 (drain / webhook)                 │
                                   ├─ verify signature over the RAW body
                                   ├─ normalise: platform ids → your service identity
                                   ├─ allowlist labels, drop the rest
                                   ├─ sample by class (keep errors, sample the rest)
                                   └─ dedupe by delivery id
```

Five jobs, and only the first is obvious. The middle three are why a bridge beats pointing the
drain straight at the backend even where the protocols happen to match.

---

## The two things worth stealing

### 1. The two ingest paths have deliberately opposite delivery semantics

This is the sharpest design decision in the pattern and it looks like an inconsistency until you
see why.

| Path | On a backend failure | Why |
|---|---|---|
| **Log drain** | swallow the error, return **204 anyway** | the platform retries at *batch* granularity and the records carry no stable per-record delivery id, so a retry **cannot** be deduplicated — it can only duplicate |
| **Event webhook** | let it propagate, return **5xx** | these carry a delivery id, so the dedupe makes a retry safe — and the events drive counters that alerts threshold on, where a gap matters more than a duplicate |

The rule underneath: **ask for a retry only when you can make it idempotent.** Otherwise
acknowledge and accept the loss, because the alternative is duplication you cannot detect.

Which means the dedupe and the 5xx are one decision, not two. Remove the dedupe and you must also
remove the 5xx.

### 2. The dedupe exists for counter correctness, not for storage

It runs *before* the counters increment, not before the write. A redelivered webhook would
otherwise double-count the very series your deploy-failure alerts threshold on — so the dedupe
protects the alert, not the bill.

Its limitation is worth stating plainly because it is inherent: **an in-memory dedupe is
per-instance, so it fails exactly when it is needed** — on a cold start, and on scale-out, which
is to say during a burst, which is when redelivery happens. If you need it to hold under those
conditions, it has to be shared state, and that is a real cost to weigh rather than a detail to
fix later.

---

## What fails without it — and the one that matters most

**The whole path can go dark and look perfectly healthy.** In the reference estate an earlier
generation of this bridge ran for roughly three and a half months with its pods `Running`, being
scraped, and every counter flat at zero. Nothing alerted, because "no events" is exactly what a
quiet period looks like.

The cause was banal: its front door was gated behind a configuration flag that was never set in
that environment. The lesson is not about the flag.

> A telemetry path needs a heartbeat that is **independent of the telemetry**. If the only
> evidence that ingestion works is the arrival of data, then no data is indistinguishable from
> nothing happening.

Three more:

**Metrics from a load-balanced set of in-memory counters are structurally untrustworthy.** Two
replicas, each holding its own counters, scraped through one address: every scrape lands on an
arbitrary one, and both carry the same target label. The resulting series is a sawtooth of two
unrelated counters, and no amount of querying fixes it. Either make the state shared, or make the
metric a single-writer.

**A stale backend credential drops every line silently.** The bridge returns 204, the platform's
delivery UI shows success, and nothing reaches the backend. Nothing in either vendor's console is
wrong. Only an end-to-end assertion catches it.

**Cardinality arrives from outside your control.** The platform decides what fields it sends. An
allowlist at the bridge — not a denylist — is what stops a new field the platform adds next month
becoming a new label and a bill.

---

## Decisions

See [ADR-003](../../docs/decisions/003-bridge-delivery-semantics.md) for the 204-versus-5xx
split, and [ADR-004](../../docs/decisions/004-serverless-over-in-cluster.md) for why the third
generation of this moved out of the cluster — the in-cluster version's failure was operational
rather than architectural, which is the more interesting reason.
