# ADR-001 — A curated driver bundle, not a paid client, a proxy or a bastion

**Status:** accepted · **Context:** replacing shared-password database access for engineers

## Decision

Ship a pinned dependency set plus a client driver registration. No bespoke code, no additional
running infrastructure, no per-seat licence.

## The options, and why the obvious ones lost

**A paid SQL client with the authentication built in.** Several have this as a dropdown, and it
would have taken minutes rather than an afternoon. Rejected on two grounds. It is a per-seat cost
that scales with the team for a capability the free tier can reach with a configuration change —
but more importantly, **it moves the mechanism inside a vendor's UI**. When it breaks you are
reading release notes rather than a classpath, and you cannot hand a colleague a file that
reproduces your setup. The bundle is four lines of XML anyone can inspect and pin.

**A connection proxy that terminates authentication.** Correct at scale and genuinely better for
applications. Rejected for *humans* because it is a new tier-0 component on the access path for a
one-person platform team: something else to run, patch, monitor and be paged for. And it would be
carrying the credential the pattern exists to eliminate. The reason to reconsider is application
connection pooling, not human access — a different problem with a different answer.

**A bastion host with a client installed.** Rejected as a step backwards: it reintroduces a
shared machine, gives everyone the same source address at the database, and puts the audit trail
back where the pattern is trying to move it away from.

**Do nothing — the path already existed.** This was effectively the status quo, and it is the one
worth recording, because it was the *measured* state rather than a hypothetical. The IAM path was
provisioned and had zero sessions. Rejecting "do nothing" required the audit to prove that
availability is not adoption.

## What made the chosen option work

The wrapper's own dependency graph assumes a workload identity. Making it resolve an SSO profile
needs two SDK modules the wrapper never declares and nothing else pulls in. That is the entire
delta, and it is invisible until you hit it — the profile-reading module *does* arrive
transitively, so the SDK reads your config, recognises the profile, and fails to resolve it.

## Consequences

- Every engineer runs one Maven command once, then configures a driver once.
- Versions are pinned in a file under review; the bundle cannot drift underneath anyone.
- Upgrading the wrapper is a deliberate change with a diff, not a client auto-update.
- The client is free, so this does not gate on procurement — which mattered, because friction was
  the reason the previous attempt went unused.

## Reopen if

Application connection pooling forces a proxy onto the path anyway. At that point human access
should ride the proxy too rather than maintaining two mechanisms — and this ADR loses to the
option it rejected, for a reason that did not exist when it was written.
