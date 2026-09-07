"""Alert relay — N producers, one routing contract, one presentation layer.

A minimal, readable implementation of the pattern. Deliberately not the most featureful
version: the point is that the whole idea fits in one file you can read in a sitting.

    POST /relay/<route_key>

The route key travels in the URL path rather than the body, because third-party producers
will let you configure a URL and will not let you add an arbitrary field to their payload.

Configuration is two maps, supplied as environment variables so a routing change is a config
change rather than a deploy:

    ROUTES   {"k8s-prod": "#alerts-prod", "infra-noise": "#ops-noise"}
    SENDERS  {"k8s-": {"name": "cluster", "icon": ":ship:"},
              "infra-": {"name": "infra",  "icon": ":bricks:"}}

Identity is keyed on the route key's PREFIX so a family of routes shares one identity without
repeating it per route.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

ROUTES: dict[str, str] = json.loads(os.environ.get("ROUTES", "{}"))
SENDERS: dict[str, dict] = json.loads(os.environ.get("SENDERS", "{}"))
CHAT_TOKEN_ENV = "CHAT_TOKEN"          # the value is never logged, printed or echoed
CHAT_API = "https://slack.com/api/chat.postMessage"
HTTP_TIMEOUT_S = 10


class Unroutable(Exception):
    """The route key matched nothing. Deliberately fatal -- see reject_unmatched()."""


# --------------------------------------------------------------------------- routing

def destination_for(route_key: str) -> str:
    """Resolve a route key to a destination, or refuse.

    Refusing is a decision, not an omission. A misrouted alert delivered late to the wrong
    place is worse than one that fails loudly at configuration time -- so there is no
    default destination, no queue and no retry.

    The consequence is that ORDERING MATTERS: add the key to ROUTES and deploy BEFORE
    pointing a producer at it. Do it the other way round and every alert in the gap is lost,
    with no error on the producer's side, because from there the POST succeeded.
    """
    try:
        return ROUTES[route_key]
    except KeyError:
        raise Unroutable(
            f"unknown route key {route_key!r}; known keys: {sorted(ROUTES)}"
        ) from None


def identity_for(route_key: str) -> dict:
    """Presentation identity, keyed on the LONGEST matching prefix.

    Longest-match rather than first-match, so a specific prefix can override a general one
    without depending on dictionary order.
    """
    best = ""
    for prefix in SENDERS:
        if route_key.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return SENDERS.get(best, {"name": "alerts", "icon": ":warning:"})


# --------------------------------------------------------------------------- parsing

def normalise(route_key: str, payload: dict) -> list[dict]:
    """Turn a producer's payload into a list of {title, body, severity, url}.

    One parser per producer shape, selected by the same route key that chose the
    destination. Add a producer by adding a branch and a route -- the rest of the pipeline
    does not change.

    Only firing states are emitted. Resolution notices are dropped here rather than at the
    destination, because a channel of paired fire/resolve messages reads as duplicate alerts
    at a glance, and readers then learn to discount the channel.
    """
    if route_key.startswith("k8s-"):
        return [
            {
                "title": a.get("title") or a.get("name", "(untitled)"),
                "body": a.get("description", ""),
                "severity": a.get("severity", "info"),
                "url": a.get("source_url", ""),
            }
            for a in payload.get("alerts", [])
            if a.get("status", "firing") == "firing"
        ]

    # Generic webhook shape used by most hosted alerting platforms.
    return [
        {
            "title": a.get("labels", {}).get("alertname", "(untitled)"),
            "body": a.get("annotations", {}).get("summary", ""),
            "severity": a.get("labels", {}).get("severity", "info"),
            "url": a.get("generatorURL", ""),
        }
        for a in payload.get("alerts", [])
        if a.get("status") == "firing"
    ]


# --------------------------------------------------------------------------- delivery

def deliver(channel: str, identity: dict, alert: dict) -> None:
    """Post one alert, and verify the destination actually accepted it.

    THE POINT OF THIS FUNCTION: a chat API can return HTTP 200 and display nothing --
    wrong channel, revoked scope, archived conversation. Transport success is not delivery.
    So the LOGICAL status in the response body is what decides, and a failure raises.

    Without this check a relay runs clean for a day while delivering nothing, and the only
    symptom is a quiet channel, which is indistinguishable from a quiet week.
    """
    token = os.environ[CHAT_TOKEN_ENV]
    icon = {"critical": ":rotating_light:", "warning": ":warning:"}.get(
        alert["severity"], identity["icon"]
    )
    body = json.dumps(
        {
            "channel": channel,
            "username": identity["name"],
            "icon_emoji": icon,
            "text": f"*{alert['title']}*\n{alert['body']}"
            + (f"\n<{alert['url']}|open>" if alert["url"] else ""),
        }
    ).encode()

    req = urllib.request.Request(
        CHAT_API,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        result = json.loads(resp.read())

    if not result.get("ok"):
        # e.g. channel_not_found, not_in_channel, invalid_auth, is_archived
        raise RuntimeError(f"destination rejected the message: {result.get('error')}")


# --------------------------------------------------------------------------- entrypoint

def handle(route_key: str, payload: dict) -> tuple[int, str]:
    """Returns (status, message). Wire this to whatever HTTP front door you use."""
    try:
        channel = destination_for(route_key)
    except Unroutable as exc:
        # 4xx, not 5xx: the producer must not retry a request that can never succeed.
        log.error("unroutable: %s", exc)
        return 400, str(exc)

    identity = identity_for(route_key)
    alerts = normalise(route_key, payload)
    if not alerts:
        return 200, "no firing alerts in payload"

    for alert in alerts:
        deliver(channel, identity, alert)
    return 200, f"delivered {len(alerts)} to {channel}"
