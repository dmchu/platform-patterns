# Agentless log ingestion

**One ingestion shape for every source that cannot run a log agent — and a liveness signal for
the pusher that does not travel on the same channel as the payload.**

---

## The problem

A node agent collects logs from everything running next to it. Everything else — a SaaS platform,
a managed cloud log destination, a webhook publisher, a scheduled job outside the cluster — has
to get there another way.

Left alone, each becomes a bespoke integration with its own label names, its own credential
handling, and its own silent failure mode. And because these sources sit outside the cluster's
health surface, **nothing notices when one stops**.

---

## The shape

Six parts. The last two are the ones usually missing.

1. **A signature-verified receiver, sized to the source.** Expose exactly the routes the source
   calls. Verify an HMAC over the **raw** body with a constant-time compare, and fail closed when
   either the signature or the configured secret is absent. Cap the body before parsing and
   return an explicit `413` rather than dropping the connection — a status code tells the source
   something; a closed socket does not.
2. **Accept whatever body shape arrives.** Single object, array, newline-delimited, or an object
   with a records key. Sources change this without telling you.
3. **Normalise to your vocabulary.** The source's identifiers are not yours. This is where they
   are reconciled, and it is the reason the pattern exists rather than pointing the source at the
   backend directly.
4. **Allowlist labels, never denylist.** The source decides what fields it sends. An allowlist
   means a field it adds next month becomes a *value*, not a new label and a new bill.
5. **Sample by class, not uniformly.** Keep every error; sample the routine. A flat sample rate
   throws away the thing you are keeping logs for.
6. **A liveness signal on a different channel from the payload.** See below — this is the part
   that matters.

---

## What fails silently

**A pusher dies and every alert on its data turns green.**

Not red. Green. The stream vanishes, the query returns nothing, and nothing-is-not-greater-than-a
-threshold. Dashboards show flat zero, which is indistinguishable from a healthy quiet period.

And there is a specific idiom that decides whether you are protected or blindfolded, depending
entirely on **which direction the comparison runs**:

| Rule | With the empty-vector fallback | Verdict |
|---|---|---|
| `count(...) > N` — "too many errors" | absent stream → `0` → `0 > N` is false → **permanently green** | a blindfold |
| `count(...) < 1` — "nothing arrived" | absent stream → `0` → `0 < 1` is true → **fires** | load-bearing |

The same operator, on the same stream, doing opposite jobs. On a "too many" rule it converts a
dead pusher into silence; on a "nothing arrived" rule it is the only reason the rule works at all,
because without it an absent stream returns no-data and a permissive no-data policy swallows it.

So: **the liveness rule must exist per pusher, must use the fallback, and must never be the same
rule as the threshold.** If your threshold rule and your liveness rule are the same rule, you have
a threshold rule and no liveness rule.

---

## Three more, each found the expensive way

**Archived logs query successfully and return nothing.** `status: success`, empty result, no
error anywhere. An operator reasonably concludes the logs do not exist — when in fact the query
was outside the archive's addressable window, or against a tier that needs a restore first. The
absence of an error is not evidence of absence.

**A per-minute rate cap does not cap per minute.** A cap enforced per worker, with concurrency
above one, permits the ceiling multiplied by the concurrency factor during a burst — which is
precisely when the cap was supposed to apply. The cap looks set correctly and is not.

**The agent path fails the same way, just more quietly.** An in-cluster agent that stops
delivering to one destination leaves the others working, so the fleet looks healthy. It was found
here only when someone went looking for old data and it was not there. Whatever asserts liveness
for the agentless pushers should cover the agent's destinations too.

---

## The boundary with the agent path

Worth being explicit, because the two are often conflated:

| | Node agent | Agentless pusher |
|---|---|---|
| Discovers new sources | yes, automatically | no — each is wired deliberately |
| Enriches with local context | yes: node, pod, namespace | only what the source sends |
| Survives backend outage | buffers on disk | typically not — the source's retry is the only buffer |
| Works for sources you do not run | no | that is the entire point |

The pusher is not a worse agent. It is the answer to a different question, and treating it as an
agent substitute is how it ends up without a liveness signal.
