# Notes — passwordless database access

What this replaced, how the "before" was measured, and the things that only show up once it is
running.

---

## What it replaced

**Model A — a shared master password in a distributed project file.** Engineers downloaded a
prepared SQL-client project from a password manager, pasted the master user's password, and
ticked *save password*. Read-only versus read-write was enforced by pointing the connection at
the **reader endpoint**, which sets the session read-only at the engine level — a topology
control standing in for an access control. It works right up until someone changes the host.

**Model B — an ephemeral database pod.** Run a container in the cluster with the password in an
environment variable, exec in, run the query. Its own runbook carried the warning that made the
case against it: mask the password, because it lands in the pod spec and the logs.

**Model C — a paid SQL client.** Several have this authentication built in as a dropdown. See
[ADR-001](../../docs/decisions/001-curated-driver-bundle.md).

## How the "before" was measured

The audit is worth describing because it needed no database client and no network path to the
database, which is what made it possible at all.

Log export to the cloud log service was off and the data API was off, so the engine logs were
pulled directly with the provider's log-download API using an administrative SSO session. That
only yields anything because a cluster parameter group had previously set connection and
disconnection logging with a line prefix carrying host, user and database — a change made for an
entirely unrelated ticket, and the reason the audit was possible.

A second, independent pass enumerated the IAM side: every principal holding the database-connect
action, resolved from the account authorisation dump and re-verified per policy.

Two methods, two directions, one conclusion:

- Interactive access was **~100% shared master password**.
- The per-engineer IAM path was provisioned in production and had **zero sessions** in the window.
- The single genuine IAM session that did exist rode an administrator wildcard and a hand-made
  personal database role — not the designed tier.

That last line is why the pattern is framed as *make it easier than the password* rather than
*enable IAM auth*. The path already existed. Nobody walked it.

## The best thing anyone said about the old credential

When the master moved to short automatic rotation, the copies in the password manager and the
secret store stopped working — and nobody noticed for a while, because nobody was using them.
The reading of that at the time:

> The stale password is the proof the value is unused. A credential that can go invalid
> unnoticed is not a rollback net; it is a trap for whoever reaches for it mid-incident.

That generalises well past databases. A fallback nobody exercises is not a fallback, and its
staleness is evidence — you just have to be willing to read it that way.

## Things that only appear once it is running

- **Token lifetime is not session lifetime.** The token authenticates at connect time. An open
  connection outlives it fine; a *reconnect* after expiry needs a fresh mint. The wrapper handles
  this. A hand-rolled `PGPASSWORD=$(... generate-db-auth-token ...)` shell alias does not, which
  is why the alias is a trap dressed as a simplification.
- **The token is signed for one hostname.** Anything that changes the apparent host — an alias, a
  tunnel, a proxy — invalidates the signature. The error looks like a rejected password, which
  sends people to the wrong place for twenty minutes.
- **Authentication is not authorisation.** The engine's IAM-auth role grants the right to
  authenticate and nothing else. A tier role with membership and no grants connects successfully
  and can read nothing, which is a confusing first-run experience worth pre-empting in the
  onboarding note.
- **Grant on the resource id, not the name.** The connect action names the cluster's immutable
  resource identifier. Grant on the name and the permission follows a rename, or worse, quietly
  fails to follow a restore.
- **Offboarding is only clean if nothing is per-person.** Tier roles keep it a group change. The
  moment somebody creates a personal database role to solve an edge case — as happened here —
  offboarding has a manual step nobody will remember.

## What it cost

An afternoon of dependency archaeology, a parameter-group change that already existed for other
reasons, one onboarding document, and a per-service cutover to move applications off static
credentials separately. The client side is a `pom.xml`, one driver registration and three
connection properties.

The expensive part was not building it. It was discovering that building it the first time had
not been enough, because nothing had made it the path of least resistance.
