# platform-patterns

Six patterns from running a small payments platform on AWS — the reusable shape of each, why it
is built that way, and what fails silently without it.

Each is a **boundary**: a place where two systems that do not speak the same language have to
meet. A managed database and a desktop SQL client. A frontend platform and an observability
backend. Four alert producers and one human reading a phone at 3am. A log store and everything
that cannot run an agent next to it.

The reason they belong in one repository is what they have in common:

> **At every one of these boundaries, the default failure is silence that looks like health.**

Not an error, not an alert — a green dashboard, a flat counter, a `status: success` with an empty
result, a pod that is `Running`. Each pattern below is therefore two things: a mechanism that does
the work, and a second mechanism whose only job is to prove the first one is still running. The
second is the part that is usually missing, and in every case here its absence was found the
expensive way.

| Pattern | The boundary | The silence it prevents |
|---|---|---|
| [Passwordless database access](patterns/iam-database-access/) | a desktop SQL client ↔ cloud IAM | the designed access path exists and **nobody uses it**, so the shared password stays |
| [PaaS telemetry bridge](patterns/paas-telemetry-bridge/) | a managed frontend platform ↔ a managed log backend | pods `Running`, every counter flat at zero, for **months** |
| [Alert relay and routing contract](patterns/alert-relay/) | N alert producers ↔ one chat destination | a routing key that matches nothing, dropped with no queue and no retry |
| [Agentless log ingestion](patterns/agentless-log-ingestion/) | sources with no agent ↔ a log store | the pusher dies and every alert on its data turns **green** |
| [Reclaimed-capacity early warning](patterns/reclaimed-capacity-early-warning/) | a cloud provider's capacity decisions ↔ your workloads | the subscription is dark for months while reclaims are handled ungracefully |
| [Alert rules that cannot fire](patterns/alert-liveness-audit/) | an alert rule ↔ the signal it claims to watch | `Normal`, green, counted as coverage, and structurally unable to fire — ten ways in one quarter |

## How to read these

Start with any pattern's `README.md`: the problem, the mechanism, how to use it, and *what fails
silently*. Then the decision records in [`docs/decisions/`](docs/decisions/) — each names the
option that lost and why, and several of the losers were the obvious choice.

**The treatment varies by pattern, deliberately.** One of these is forty lines of XML whose value
is knowing *which* forty; another is a service whose interesting part is its delivery semantics.
Forcing one template on both would produce a padded page and a cramped one:

| Pattern | Runnable reference | Long-form notes | Decision records |
|---|---|---|---|
| Passwordless database access | the dependency set — the whole pattern | how the "before" was measured | ADR-001 |
| PaaS telemetry bridge | — architecture and semantics, not code | in the README | ADR-003, ADR-004 |
| Alert relay | ~230 lines with tests; the delivery-state semantics are the point | in the README | ADR-002 |
| Agentless log ingestion | — the shape and the alerting trap | in the README | — |
| Reclaimed-capacity early warning | — the wiring and the honest budget | in the README | ADR-005 |
| Alert rules that cannot fire | a read-only audit script with tests | in the README | ADR-006 |

## Why the decision records matter more than the implementations

Every one of these had a plausible off-the-shelf alternative that a reasonable engineer would
reach for first: a paid SQL client with the auth built in, the platform's own marketplace
integration, the alerting vendor's stock chat contact point, the log backend's own agent, a
second consumer on an existing queue. In each case that alternative was tried or costed and lost
for a reason that is not obvious until you have hit it.

The implementations are small. The reasons are the artefact.

## Scope

These are generalised from systems I built and operate in production. The patterns transfer; the
numbers, names and internal identifiers do not, and are not here.

Every file is checked by [`.tools/redact-check.py`](.tools/redact-check.py) before commit —
sixteen rules covering internal identifiers, credentials, hostnames and local paths. It runs at
two strictness levels, because a portfolio may reasonably name the tools it used and public
writing about the same work should not.

It also runs in CI on every push, under the generic rules that need no private term list. The
job first plants a key-shaped string and refuses to trust its own clean run unless the scanner
trips on it — the same rule the patterns ask of everything else: a check that cannot be seen to
fail has not been seen to work.

It also checks what no content scan reads: every commit and tag must carry the public noreply
address, and CI proves that check can fail before trusting it.

The reference implementations are written fresh for this repository, and tested.

## Changelog

- **2026-09-28** — Commit and tag identities are checked in CI, with a planted foreign address
  as the positive control.
- **2026-09-27, later** — Liveness audit `v1.2.1`, after its first run on a real estate printed
  308 lines, most of them the tool's own: findings and review tiers, pending-episode lengths
  against the period, recording and quiet rules no longer counted as dead, and the
  `or vector(0)` check now respects the comparison direction.
- **2026-09-27** — Sixth pattern: *Alert rules that cannot fire* — ten dead-rule shapes from one
  quarter, the history and evaluator tests, a read-only audit script that refuses a green result
  over nothing, and ADR-006 on why definitions are not the thing to audit.
- **2026-09-27** — Alert relay: delivery state (post once, mark resolved, post again on a
  re-fire), the alarm that must not depend on the relay, and grouping by entity; a test suite for
  the reference; CI with a positive control for the redaction scan. The load-bearing section of
  every pattern is now titled *What fails silently*.
- **2026-09-07** — Five patterns, five decision records, one runnable reference.
