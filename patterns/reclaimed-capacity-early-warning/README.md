# Early warning for reclaimed capacity

**Consume the provider's "this node is going away" signals so a reclaim becomes a graceful drain
with a replacement already launching — and be honest about the fact that this does not keep your
service up.**

That second half is the reason this pattern is written down. The first half is well documented
and largely automatic. The second half is what a storm teaches you.

---

## The problem

Interruptible capacity is the largest cost lever available to a small team, and it is usually
adopted as a default and later discovered to have been an availability decision.

The provider does tell you before it takes a node back. Consume that and you get a drain and a
replacement. Ignore it and the node simply stops: pods killed rather than evicted, daemonset
containers taking a hard signal instead of a shutdown, and — measured here before the
subscription existed — node lifetimes of around six minutes with pods stuck failing to drain.

---

## The mechanism

One queue per cluster, consumed by the autoscaler's interruption controller. Everything else
that wants these events is added as **another target on the event rule**, never as another
consumer on the queue.

```
provider event bus
   │
   ├──▶ rule: interruption warning ──┬──▶ queue ──▶ autoscaler: cordon, drain, pre-provision
   │                                 └──▶ notifier (a human sees it)
   ├──▶ rule: rebalance advisory ────┬──▶ queue
   ├──▶ rule: instance state change ─┤
   └──▶ rule: provider health  ──────┘
```

> **Fan out at the bus. Never chain behind the mechanism that does the work.**
>
> A second consumer on the same queue **steals messages from the controller and breaks the
> drain** — queue semantics deliver each message once, so the notifier and the drain would
> compete for them. This was recorded as an explicit architectural rule at design time, and it is
> the single most important line in the pattern.

Express the rule set as a map that consumers extend, so adding a notifier creates a *new target*
rather than editing an existing one. Making the safe thing the easy thing is most of the value.

See [ADR-005](../../docs/decisions/005-fan-out-not-chain.md).

---

## Know what each signal actually buys you

| Signal | Real advance notice | What to do with it |
|---|---|---|
| Interruption warning | **≈2 minutes** before shutdown | cordon, drain, pre-provision. The only one with usable lead time |
| Rebalance advisory | none guaranteed — it is a hint | feed a capacity signal at most. **Not a page** |
| Instance state change | none — it has already happened | inventory reconciliation |
| Provider health event | varies, often days | a ticket, on a filtered subset |

**A signal with no action must not be routed to a human.** All four were forwarded to one channel
here; the filter that would have dropped the three low-value types was written and then removed,
and the channel became noise — which costs you the one signal that mattered.

And **filter on the discriminator, not just on source and event type.** A state-change rule with
no state filter and a health rule with no service filter match *every* instance and *every* health
event in the account, including hosts nothing in this system manages.

---

## What the early warning does not buy

This is the part worth the whole page.

A storm here produced **31 interruptions in 24 hours, 15 of them inside a five-hour window.** The
subscription worked perfectly throughout:

- every zero-available blip was preceded by an interruption event **two to four minutes earlier**
- the drain happened
- the replacement node launched in **two to four minutes**
- zero out-of-memory kills, no restart anomalies

**And six services still went dark for two to four minutes each** — in a partner-facing integration environment, blocking a partner's own testing for hours while three plausible and entirely wrong causes were investigated first.

Because the notice period was never the limiting factor, and neither was replacement capacity. It
was **pod topology**. The services that went dark were single-replica, or had two replicas that
had both been placed on the same node. The control case in the same storm — identical
configuration, two replicas on two distinct nodes — never lost availability.

So state the budget honestly:

> Consuming interruption signals converts an ungraceful kill into a graceful drain and buys a
> pre-provisioned replacement. **That is all it does. It does not create an endpoint.**

The mechanism that keeps a service up through a reclaim is replica count and *hard* anti-affinity,
and it is a separate piece of work that has to be costed separately.

> **A disruption budget does not protect against a reclaim at all.** A budget governs *voluntary*
> eviction. Reclamation is involuntary — the capacity is being taken. A budget must never appear
> in a resilience checklist next to interruption handling; it belongs to the drain and
> consolidation story only. A *soft* topology spread has the same problem: it yields exactly when
> capacity is under pressure, which is precisely when you needed it.

---

## The silences

True to the rest of this repository, every layer here failed quietly at least once.

**The controller ran for months without consuming the queue.** Eighteen real reclaims handled with
**zero** interruption activity. Nothing reported it. Code review did not catch it, because the
queue existed and the code declaring it was correct. Subscription is the archetypal
wired-or-dark-with-no-middle-state, so the assertion has to be against the **running controller** —
its reconciler is active, and its interruption counter has advanced since the last reclaim the
account actually saw — never against the declaration.

**The notifier delivered nothing for a day.** 107 invocations, zero errors, zero messages. It
never parsed the destination's logical status. Same failure as in the
[alert relay](../alert-relay/) pattern, found independently — which is why *proving delivery
rather than invocation* is a rule in this repository and not an anecdote.

**Short queue retention with no dead-letter queue silently discards notices.** Any controller
outage longer than the retention window loses the events, with nothing to show for it. Size
retention against the consumer's worst-case downtime, add a redrive, and alarm on message age at
a fraction of retention — so it fires while the message is **still deliverable**.

**A cost pass blinded the mechanism.** The metric label that answered "how many interruptions did
production take last night" was aggregated away by an entirely legitimate cardinality reduction,
and the next incident happened inside that blind spot.

> Pin a mechanism's diagnostic labels **in the same change that creates the mechanism**. Reducing
> telemetry cost is a change to the evidence base of every mechanism that telemetry serves.
