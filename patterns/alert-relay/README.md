# Alert relay — N producers, one routing contract, one presentation layer

**Put a small owned service between every alert producer and the humans reading them, so that
routing, presentation and identity are decided in one place instead of four.**

---

## The problem

Several independent systems want to reach the same humans in a chat client: a hosted alerting
platform, an in-cluster observability agent, a cloud event bus, an on-call scheduler. Point each
at its own native integration and four things go wrong at once.

**Volume from lifecycle, not from incidents.** A typical stock integration posts when a rule
fires *and* again when it resolves. A channel of paired messages reads as duplicate alerts at a
glance even when one thing happened, and readers learn to discount the channel — which is the
worst possible outcome for an alerting system.

**No shared presentation.** Each producer's payload has a different shape and a different set of
omissions. A reader cannot compare two messages or learn one layout, so every alert is read from
scratch.

**Routing decided inside each producer.** The mapping from *what happened* to *who should see it*
is scattered across four vendor UIs, each with its own concept of a destination. Answering "where
does this alert go?" means opening four systems, and changing it means changing four.

**Nothing is testable.** A native integration is configuration in someone else's product. You
cannot exercise it, diff it, or assert on it.

---

## The mechanism

One HTTP endpoint. The routing key travels **in the URL path**, not in the payload.

```
POST /relay/<route_key>
        │
        ├── key → destination        (external config, not code)
        ├── key prefix → identity    (bot name, icon, presentation)
        └── key → parser             (which producer's shape to expect)
```

That single design choice carries the pattern. Producers are third-party systems: you usually
**cannot** make them emit an arbitrary discriminator field in the body, but nearly every one of
them lets you configure *a URL*. So onboarding a new producer is a URL on their side and one map
entry on yours, with no payload contract to negotiate.

One string then decides three things — destination, presentation identity, and which ingest
parser runs.

**Two maps, both external configuration, never code:**

| Map | Keyed on | Why |
|---|---|---|
| destination | the full route key | a routing change is a config change, reviewable and revertible on its own |
| identity | the key **prefix** | producers group naturally (`k8s-*`, `infra-*`), so a family of routes shares a bot identity without repeating it per route |

Keying identity on the prefix rather than the whole key is what stops the identity map growing at
the same rate as the routing map.

See [`reference/relay.py`](reference/relay.py) — the whole thing is about 120 lines.

---

## What fails without it, and what still fails with it

**The unmatched key is the interesting case, and it deserves a deliberate decision.** A route key
that matches nothing gets logged and rejected with a 4xx. Nothing is queued, retried or
dead-lettered.

That is a *choice*, and it is the right one for alerting: a misrouted alert delivered late to the
wrong place is worse than one that fails loudly at configuration time. But it means **the
ordering of a routing change is load-bearing**:

> Point a producer at a new route key *before* the relay knows that key, and every alert matching
> it is lost — with no error in the producer's UI, because from its side the POST succeeded.

Always: add the key to the map, deploy, then point the producer at it.

Three more, each found the expensive way:

**A declarative apply deletes routes added by hand.** If the map is infrastructure-as-code and
someone adds a route directly to the running function during an incident, the next apply removes
it. Routing stops with no error anywhere, and the alert then hits the unmatched case above.

**Your deployment tool may not be able to see the handler's source.** Packaging a function from a
directory without an explicit content hash means the deployment tool diffs only the metadata. A
preview showing just an environment-variable change can ship a **new route key with the old code
behind it** — exactly the split that careful ordering exists to prevent. The generalised rule is
worth more than the fix:

> A preview that shows no code diff is not proof the code is unchanged. It may be proof the
> provider cannot see it. Cross-check by downloading the deployed artefact.

**Returning 200 is not delivering.** A chat API can accept a call and not display anything —
wrong channel, revoked scope, archived conversation. Parse the destination's *logical* status,
not the transport status code, and fail on it. A relay that ran cleanly for a day while
delivering nothing is a real and unremarkable outcome.

---

## Why not the native integrations

See [ADR-002](../../docs/decisions/002-relay-over-native-integrations.md). Short version: the
stock integration's two-messages-per-episode behaviour is not configurable, the vendor's own
incident product would have solved threading but not the specific affordance wanted, and a
public function URL with no authorizer was built first and rejected after testing — every request
was refused at the platform edge before reaching the handler.

The migration itself is worth copying: six phases, the stock integration left in place as a
fallback until the relay had run clean, one pilot rule, then bulk. 53 rules moved as one-line
receiver changes.
