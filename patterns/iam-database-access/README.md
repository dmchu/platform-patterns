# Passwordless database access from a desktop SQL client

**Replace a shared database password with a short-lived, per-person token derived from the
engineer's existing single-sign-on session — in a free SQL client, with no bespoke code.**

The database sees the identity the identity provider issued. There is no secret to store,
distribute, rotate or leak.

---

## The problem

Interactive human access to production databases is almost always authenticated by a shared
static password, and it produces four coupled problems rather than one:

- **No attribution.** Every human session appears as the same database role. If engineer traffic
  also arrives through a NAT or a mesh connector, the database sees one source address too — so
  the logs cannot identify a person even in principle.
- **A credential that must be distributed.** Copies accumulate: a password manager, a secret
  store, a project file someone emailed. Every rotation breaks every copy.
- **Privilege far beyond need.** The shared credential is usually the one that already exists,
  which is usually the master user. Read-only access ends up enforced by *pointing at a reader
  endpoint* — a topology control wearing an access control's clothes.
- **Rotation is blocked by the humans.** You cannot put the master on a short rotation while
  people are still authenticating with it.

Cloud providers already solve this: the database accepts a signed, short-lived token in place of
a password, and the signature is your IAM identity. The path exists.

**The reason it goes unused is friction.** In one measured audit of three environments over the
retention window, interactive access was ~100% shared-password and the per-engineer IAM path —
which existed, and was provisioned — had **zero sessions**. Not rejected. Just harder than the
thing next to it.

So the pattern is not "enable IAM auth". It is **make the IAM path easier than the password**,
and then delete the password so there is nothing to fall back to.

---

## The mechanism

Four moving parts, only one of which is unusual.

```
SSO login  ──▶  cached SSO token  ──▶  role credentials  ──▶  signed DB token  ──▶  connection
   (browser)      (~/.aws/sso)         (sso + ssooidc)         (rds presign)        (JDBC)
                                                                                        │
                                                              database role  ◀──────────┘
                                                       (member of the IAM-auth role)
```

1. **The engineer logs in to SSO once**, the way they already do for the CLI. Nothing new.
2. **The JDBC driver wrapper** — the cloud vendor ships one — resolves those credentials and
   mints a database token per connection. No custom code.
3. **The client is configured**, not programmed: point a driver definition at a folder of jars,
   set three connection properties, use the tier role as the username, leave the password blank.
4. **The database side** is a small set of tier roles — read-only, read-write, admin — each a
   member of the engine's IAM-authentication role, and an IAM policy granting connect on
   *that role name only*.

Grants are per-tier, not per-person, so onboarding is an SSO group change and offboarding is the
same change reversed. Nothing is provisioned in the database per engineer.

---

## The part that is not in any tutorial

**The vendor's JDBC wrapper is built for workloads, not for laptops, and its dependency graph
says so.**

Its published POM declares two SDK modules at runtime: the one that signs the token, and the one
that exchanges a role or a projected web identity for credentials. Between them those cover the
server-side story completely — which is the environment the wrapper was designed for.

On a workstation the credential source is an SSO session, and **that path lives in two modules
the wrapper never mentions**: the one that exchanges a cached SSO token for role credentials, and
the token provider that reads and refreshes the SSO cache. Neither appears anywhere in the
transitive closure. They are present only because a human named them.

The trap is precise, and it is why this costs an afternoon rather than five minutes:

> The profile-reading module *does* arrive transitively. So the SDK reads your config file
> happily, finds the profile, recognises it as an SSO profile — and then cannot resolve it,
> because the resolver is not on the classpath. You get a credentials error against a profile
> that works perfectly in the CLI.

The whole insight is four lines in a `pom.xml`. Knowing which four is the work.

See [`reference/pom.xml`](reference/pom.xml).

---

## How to use it

```bash
cd reference && mvn -q dependency:copy-dependencies -DoutputDirectory=lib
```

Then, once, in the client:

- **Driver definition** — new driver, class `software.amazon.jdbc.Driver`, URL template
  `jdbc:aws-wrapper:postgresql://{host}:{port}/{database}`, and add every jar in `lib/`.
- **Connection** — host, port, database, and username set to the **tier role**
  (`readonly`, `readwrite`, …), password blank.
- **Connection properties** — `wrapperPlugins=iam`, `awsProfile=<your-sso-profile>`,
  `sslmode=require`.

On the database, once per tier:

```sql
CREATE ROLE readonly LOGIN;
GRANT rds_iam TO readonly;          -- engine-specific: this is what enables token auth
-- plus whatever SELECT/USAGE the tier should actually have
```

And an IAM policy attached to the engineers' permission set, granting the connect action on
`dbuser:<cluster-resource-id>/readonly` — the *resource id*, not the cluster name, so the grant
survives a rename and does not follow a restored clone.

---

## What fails silently

| Failure | Why |
|---|---|
| **Credentials error on a profile that works in the CLI** | the two SSO resolution modules are missing — the whole point of this pattern |
| **Token valid ~15 minutes, connection lives longer** | the token authenticates *at connect time*; long-lived pooled connections survive, but a reconnect after expiry needs a fresh mint. The wrapper does this; a hand-rolled script usually does not |
| **Signature valid for one hostname only** | the token is signed for the endpoint you name. A CNAME, an alias or a tunnel that changes the apparent host produces an authentication failure that reads like a password problem |
| **Clock skew** | a signed request has a validity window. A laptop resumed from sleep with a drifted clock fails to authenticate and says nothing about time |
| **The tier role has no privileges** | `rds_iam` grants *authentication*, not authorisation. Membership lets you in; it grants no `SELECT` |

And the failure this pattern does **not** fix, which is worth saying plainly: it removes the
password from the humans, not from the applications. Those are separate migrations, and the
second one is harder.

---

## What this is not

It is not a bespoke authentication class. Everything above is a dependency set, a driver
registration and three connection properties. The engineering was in finding out *which*
dependency set — and the audit that proved the existing path had zero users, which is what made
the case for doing it at all.

See [`NOTES.md`](NOTES.md) for what it replaced and how the "before" was measured, and
[ADR-001](../../docs/decisions/001-curated-driver-bundle.md) for why a curated bundle beat a paid
client, a proxy and a bastion.
