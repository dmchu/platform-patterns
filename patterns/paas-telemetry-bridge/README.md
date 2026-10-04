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
                                   └─ dedupe by id (record or delivery)
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
| **Log drain** | swallow the error, acknowledge with **200** (the default; a switch, see below) | if the platform redelivers, it is at *batch* granularity, and the per-entry id it now documents lets only the instance that saw the first delivery deduplicate, which in a burst is rarely the one retried |
| **Event webhook** | let it propagate, return **5xx** | these carry a delivery id, so the dedupe makes a retry safe — and the events drive counters that alerts threshold on, where a gap matters more than a duplicate |

The rule underneath: **ask for a retry only when you can make it idempotent.** Otherwise
acknowledge and accept the loss, because the alternative is duplication you cannot detect.

Which means the dedupe and the 5xx are one decision, not two. Remove the dedupe and you must also
remove the 5xx. When this was first written the drain carried no per-entry id and acknowledging
was the only possible answer; the platform now documents one, so the reference dedupes drain
records by id too and makes the drain's half of the decision a configuration switch
(`BRIDGE_DRAIN_FAIL_ON_BACKEND_ERROR`) rather than code — because the thing that makes flipping it
safe, a dedupe that holds across instances, is a property of your estate and not of the bridge.
See the [addendum to ADR-003](../../docs/decisions/003-bridge-delivery-semantics.md).

### 2. The webhook dedupe exists for counter correctness, not for storage

It runs *before* the counters increment, not before the write. A redelivered webhook would
otherwise double-count the very series your deploy-failure alerts threshold on — so the dedupe
protects the alert, not the bill.

The reference keeps the two states apart: a delivery id is *claimed* before the counters
increment and marked *written* only after the backend accepts the write. A retry after a failed
write therefore writes once without counting twice, and a retry after a successful write is
answered 200 and counted as a duplicate without touching either.

The drain's dedupe is the ordinary kind: it stops a redelivered record becoming a second line, and
it is what makes the configuration switch above honest.

Its limitation is worth stating plainly because it is inherent: **an in-memory dedupe is
per-instance, so it fails exactly when it is needed** — on a cold start, and on scale-out, which
is to say during a burst, which is when redelivery happens. If you need it to hold under those
conditions, it has to be shared state, and that is a real cost to weigh rather than a detail to
fix later.

---

## How to use it

[`reference/bridge.py`](reference/bridge.py) is the whole receiver in one standard-library
module, with [its tests](reference/test_bridge.py) alongside. One core, `Bridge.handle`, serves
two entry points: a threaded HTTP server for local use and an AWS Lambda `handler` for the
function behind an API gateway that ADR-004 describes. The probe lives in the same file but on the
other side of the front door; it is a client of the bridge, which is the point.

Configuration is environment only, and the process refuses to start without the secrets. There
is no default secret and no flag that skips verification, because a bridge with verification off
is a public endpoint that writes whatever it is sent into your log store under your credential.

| Variable | What it is |
|---|---|
| `BRIDGE_DRAIN_SECRET`, `BRIDGE_WEBHOOK_SECRET` | the signing secrets: the webhook's is shown once, when it is created; the drain's is generated for you and can be read or replaced from the drain's *Edit* dialog |
| `BRIDGE_BACKEND`, `BRIDGE_BACKEND_URL` | `loki` with its push API URL, or `otlp` with the `/v1/logs` URL; a redirect from either is a failed write, never followed |
| `BRIDGE_BACKEND_USER`, `BRIDGE_BACKEND_TOKEN` | the **write-only** backend credential; basic auth with both, bearer with the token alone; never logged |
| `BRIDGE_BACKEND_TIMEOUT_SECONDS` | seconds per backend call; the probe reuses it |
| `BRIDGE_CONFIG` | a JSON file with the service map, the label allowlist and the sampling rates — [`reference/config.example.json`](reference/config.example.json) |
| `BRIDGE_SERVICE_MAP`, `BRIDGE_LABEL_ALLOWLIST`, `BRIDGE_SAMPLE_RATE_STATIC`, `BRIDGE_SAMPLE_RATE_DEFAULT` | the same three things as environment overrides |
| `BRIDGE_DEDUPE_TTL_SECONDS`, `BRIDGE_DEDUPE_MAX_ENTRIES` | the dedupe is bounded in time and in memory |
| `BRIDGE_DRAIN_FAIL_ON_BACKEND_ERROR` | `true` turns the drain's 200 acknowledgement into a 500; read the section above first |
| `BRIDGE_QUERY_URL`, `BRIDGE_QUERY_USER`, `BRIDGE_QUERY_TOKEN` | probe only: how to *read* the backend, with a credential that is not the bridge's |

**Run it locally.**

```bash
export BRIDGE_DRAIN_SECRET=... BRIDGE_WEBHOOK_SECRET=...
export BRIDGE_BACKEND_URL=https://<your-log-store>/loki/api/v1/push
export BRIDGE_BACKEND_USER=<instance id> BRIDGE_BACKEND_TOKEN=<write-only token>
export BRIDGE_CONFIG=reference/config.example.json
python3 reference/bridge.py serve --port 8080
```

`POST /drain` and `POST /webhook` are the platform's two front doors; `GET /metrics` is the
instance's counters in Prometheus text format; `GET /healthz` is process liveness only. A body
over the platform's 5 MB batch maximum, plus a small framing margin (`MAX_BODY_BYTES` in the
module), is refused with a 413 by its declared length before any of it is parsed; the server
drains what it can first so the status reaches the sender rather than a connection reset.

Every record is normalised the same way: the signature is checked over the raw bytes before any
parsing; the platform's project id or name is mapped to your service name, and an unmapped
project keeps its platform name under `service_identity="fallback"` so the gaps in the map are
one query away; only the fields on the allowlist become labels, and every other field — including
whatever the platform adds next — rides inside the line as a structured field; errors, fatals and
crashed invocations are always kept, static-asset requests are sampled at their own rate, and the
sampling decision is a hash of the record id, so a redelivered record decides the same way twice.

**Deploy it as a function.** The module exposes `handler(event, context)` for a Lambda behind a
Function URL or an API Gateway HTTP API; it reads the same environment. Package the one file, set
the variables, and give the function nothing but the backend credential — scoped to writing one
signal, as ADR-004 asks, with an age alarm on it. The counters and the dedupe are then per
execution environment, which is the limitation both ADRs accept; `/metrics` repeats it in every
`HELP` line so nobody graphs a sawtooth and calls it a counter.

**Configure the platform.** In the platform's team settings, add a *Logs* drain with a custom
endpoint at `https://<your-bridge>/drain`, JSON or NDJSON format, and set its signature secret to
`BRIDGE_DRAIN_SECRET`; the platform tests the endpoint when the drain is created and starts
forwarding at once. Then add a webhook at `https://<your-bridge>/webhook` for the deployment
events you want counted; its secret is shown once and goes in `BRIDGE_WEBHOOK_SECRET`. The
platform marks a drain *errored* after sustained delivery failures — which a bridge that
acknowledges on backend failure will never trigger, and that is why the probe exists.

The platform's *trace* drain speaks OTLP/HTTP and can point straight at any OTLP endpoint; it
needs no bridge, and this one does not handle it. Its Speed Insights and Web Analytics drains use
the same signed JSON/NDJSON contract as the log drain, so the same receiver shape applies with a
different normaliser.

**Run the probe on a schedule.**

```bash
python3 reference/bridge.py probe --url https://<your-bridge> --timeout 60
```

It builds one synthetic record with a random marker, signs it with the drain secret, posts it
through the real front door, and then queries the backend for the marker until it appears or the
timeout passes. Exit `0` means the whole path works; `2` means the backend never showed the
record — a stale credential, a wrong tenant, an acknowledged failure (with
`BRIDGE_DRAIN_FAIL_ON_BACKEND_ERROR=true` the same failure is a 500 from the bridge, which the
probe reports as `2` as well); `1` means the bridge rejected it or the probe could not run. The
probe reads the marker only out of stored lines in the query answer, never out of the page as a
whole, because the marker is also in the query URL and a login page that quotes the request must
not pass. The probe's records land in a stream of their own, `{source="probe"}`, under
`service_identity="probe"`, which is never sampled, never counts as an unmapped project, and is
the thing to alert on being absent. For a backend that is not Loki, `--query-url` takes a query
with `{marker}` in it and expects an answer in the Loki `query_range` shape. Run it from a
scheduler that is not the bridge, with a read credential that is not the bridge's write-only one,
and alert on a non-zero exit: the bridge cannot be the thing that tells you the bridge is broken.

---

## What fails silently

**The whole path can go dark and look perfectly healthy.** In the reference estate an earlier
generation of this bridge ran for roughly three and a half months with its pods `Running`, being
scraped, and every counter flat at zero. Nothing alerted, because "no events" is exactly what a
quiet period looks like.

The cause was banal: its front door was gated behind a configuration flag that was never set in
that environment. The lesson is not about the flag.

> A telemetry path needs a heartbeat that is **independent of the telemetry**. If the only
> evidence that ingestion works is the arrival of data, then no data is indistinguishable from
> nothing happening.

In the reference that heartbeat is the `probe` command, and `/healthz` says in its own body that
it proves nothing about ingestion.

Three more:

**Metrics from a load-balanced set of in-memory counters are structurally untrustworthy.** Two
replicas, each holding its own counters, scraped through one address: every scrape lands on an
arbitrary one, and both carry the same target label. The resulting series is a sawtooth of two
unrelated counters, and no amount of querying fixes it. Either make the state shared, or make the
metric a single-writer.

**A stale backend credential drops every line silently.** The bridge acknowledges, the platform's
delivery UI shows success, and nothing reaches the backend. Nothing in either vendor's console is
wrong. Only an end-to-end assertion catches it — `bridge.py probe` exits `2` here, while
`/healthz` and the platform's delivery log both say fine.

**Cardinality arrives from outside your control.** The platform decides what fields it sends. An
allowlist at the bridge — not a denylist — is what stops a new field the platform adds next month
becoming a new label and a bill. In the reference the allowlist is the only route to a label, an
object never becomes one even if listed, and the test that pins this feeds the bridge a field
that does not exist yet.

---

## Decisions

See [ADR-003](../../docs/decisions/003-bridge-delivery-semantics.md) for the
acknowledge-versus-5xx split, and its addendum on why the acknowledgement stayed the default once
the platform documented a per-entry id and a drain retry became deduplicable. See
[ADR-004](../../docs/decisions/004-serverless-over-in-cluster.md) for why the third generation of
this moved out of the cluster: the in-cluster version's failure was operational rather than
architectural, which is the more interesting reason.

**What the bridge avoids.** The log backend's own data source plugin for this platform is an
Enterprise plugin: on Grafana Cloud Pro that is $55 per active user per month, charged for every
active user on the stack and not only those who open the plugin
([pricing](https://grafana.com/pricing/),
[how users are counted](https://grafana.com/docs/grafana-cloud/platform/pricing-and-usage/users/),
as of October 2026). It carries management data — deployments, projects, domains, drain
configurations — not runtime logs. The runtime logs still need a drain endpoint, which is what
this bridge is.
