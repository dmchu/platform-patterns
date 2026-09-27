# ADR-002 — A relay, not the producers' native chat integrations

**Status:** accepted · **Context:** four alert producers, one set of humans

## Decision

Receive every producer's generic webhook in one owned service and call the chat API from there.

## Why the stock integration lost

It posts **two messages per alert episode** — one on firing, one on resolution — and that is not
configurable. At a glance a channel of paired messages reads as duplicate alerts even when a
single thing happened, and the failure that follows is behavioural rather than technical:
readers learn to discount the channel. An alerting system nobody reads has failed regardless of
its uptime.

Secondary, but decisive once you have four producers: routing lives inside each vendor's UI, so
"where does this alert go?" is a four-system question and changing it is a four-system change.

## Why not the vendor's incident-management product

It does threading natively, which was the original motivation. Rejected because it did not
support the specific affordance actually wanted, and because adopting a whole incident product
to obtain one formatting behaviour is a large surface for a small win. The open-source edition
had also been moved to maintenance in favour of the hosted one, which makes it a poor thing to
build a dependency on.

## Transport: why not a plain function URL

A public function URL with no authorizer was specified, built, and smoke-tested first. Every
public request returned a platform-level authorisation failure **before reaching the handler**,
despite a correct resource policy. Replaced with an API gateway carrying a catch-all path route,
with bearer auth still terminated in the handler.

Recorded because the cheap option was genuinely tried rather than dismissed, and because the
failure was at a layer the documentation does not lead you to.

## Consequences

- One place decides routing, presentation and identity for every producer.
- Onboarding a producer is a URL on its side and one map entry on ours.
- The relay is now on the alerting path, so **its** failure is an alerting failure — which is why
  delivery is verified against the destination's logical status rather than the HTTP code.
- Route keys and handler code must ship in the right order, and the tooling must be able to see
  the handler's source. Both are covered in the pattern's README.
- Delivery state is part of the contract. A suppression rule that ignores resolution hides every
  re-fire, and a record that ignores the channel keeps suppressing after a route moves. Both are
  in the pattern's README with what they cost, and both have a test in the reference.

## Reopen if

A producer appears that cannot be pointed at an arbitrary URL. The path-based routing key is the
load-bearing assumption; without it the pattern needs a discriminator in the payload, which most
third-party producers will not give you.
