# ADR-004 — Move the bridge out of the cluster

**Status:** accepted · **Context:** third generation of a telemetry bridge

## Decision

Run the receiver as a managed function behind an API gateway rather than as a deployment in the
cluster with a collector alongside it.

## Why — and the reason is not the one you would guess

The in-cluster version was architecturally sound. It had a genuine advantage the replacement
gives up: the collector held the backend credentials, so the internet-facing receiver never had
them, and its only outbound dependency was a service inside the cluster.

It was replaced because of how it **failed**, which was operational:

> Its front door was gated behind a configuration flag that was never set in that environment.
> The pods ran, were scraped, and reported zero for about three and a half months. Nothing
> alerted, because no events is exactly what a quiet period looks like.

The lesson is not "use serverless". It is that an ingestion path assembled from several
independently-configured pieces — ingress, controller, gateway class, certificate, receiver,
collector — has many places to be *almost* configured, and the symptom of any of them is the
same silence. Fewer moving parts is a real reduction in that surface.

## What was given up, and how it is compensated

The receiver now holds a backend credential. Compensated by scoping it to write-only on one
signal, by an age alarm on the credential, and by the end-to-end liveness assertion the pattern
requires anyway — which was the thing missing in the failure above.

## Consequences

- Fewer components to be half-configured.
- No cluster dependency, so frontend telemetry keeps arriving during a cluster outage — which is
  when you most want to know whether the frontend is up.
- Scaling is per-request, so the in-memory dedupe is weaker (see ADR-003's accepted limitation).

## Reopen if

Volume makes per-request pricing worse than a running pod, or the dedupe needs to hold across
instances — at which point shared state is needed regardless and the cluster becomes attractive
again.
