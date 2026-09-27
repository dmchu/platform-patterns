"""Alert relay — N producers, one routing contract, one presentation layer, one delivery state.

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

Delivery state is one record per (route, alert group): an episode is posted ONCE when it
fires, marked with a reaction when it resolves, and posted AGAIN if it fires after that. The
second half of that sentence is the part that was learned the expensive way -- see fire().
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

ROUTES: dict[str, str] = json.loads(os.environ.get("ROUTES", "{}"))
SENDERS: dict[str, dict] = json.loads(os.environ.get("SENDERS", "{}"))
CHAT_TOKEN_ENV = "CHAT_TOKEN"          # the value is never logged, printed or echoed
CHAT_API = "https://slack.com/api/"
HTTP_TIMEOUT_S = 10
RESOLVED_REACTION = "white_check_mark"


class Unroutable(Exception):
    """The route key matched nothing. Deliberately fatal -- see destination_for()."""


class DeliveryFailed(Exception):
    """The destination answered, and said no. Transport success is not delivery."""


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


# --------------------------------------------------------------------------- grouping

def group_key(route_key: str, payload: dict) -> str:
    """What "the same alert" means -- the unit that is posted once and resolved once.

    Prefer the producer's own group key. Without one, derive it from the alert name AND the
    labels, because the labels are what name the affected entity (the customer, the queue,
    the host).

    WHY THE ENTITY MUST BE IN THE KEY: the state below posts once per open group. Group only
    by alert name and the second customer hitting the same rule while the first is still open
    is never posted -- and if someone is always affected, the group never resolves. The
    producer's grouping decides this; the relay can only honour it.
    """
    key = payload.get("groupKey")
    if key:
        return f"{route_key}:{key}"
    parts = sorted(
        json.dumps(a.get("labels", {}), sort_keys=True) for a in payload.get("alerts", [])
    )
    return f"{route_key}:" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- state

@dataclass
class Record:
    ts: str          # the chat message id the episode was posted as
    channel: str
    status: str      # "open" | "resolved"


# In-memory, so per instance: swap for a table with a TTL for anything that scales out or
# restarts (the same caveat as the telemetry bridge's dedupe). The SEMANTICS are what this
# file is for, and they do not change with the store.
STATE: dict[str, Record] = {}


def is_open(record: Record | None, channel: str) -> bool:
    """A record suppresses a new post only while it is OPEN and in the SAME channel.

    Both conditions were learned separately. A record that ignores its own status hides
    every re-fire after a resolve. A record that ignores the channel keeps suppressing after
    a route moves, and the alert then never appears where people now look.
    """
    return record is not None and record.status == "open" and record.channel == channel


# --------------------------------------------------------------------------- parsing

def normalise(route_key: str, payload: dict) -> list[dict]:
    """Turn a producer's payload into a list of {title, body, severity, url}.

    One parser per producer shape, selected by the same route key that chose the
    destination. Add a producer by adding a branch and a route -- the rest of the pipeline
    does not change.
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
    ]


def render(identity: dict, alerts: list[dict], resolved: bool = False) -> dict:
    """One message per episode, however many alerts the producer batched into it."""
    lead = alerts[0]
    icon = {"critical": ":rotating_light:", "warning": ":warning:"}.get(
        lead["severity"], identity["icon"]
    )
    lines = [f"*{'Resolved: ' if resolved else ''}{a['title']}*\n{a['body']}"
             + (f"\n<{a['url']}|open>" if a["url"] else "") for a in alerts]
    return {
        "username": identity["name"],
        "icon_emoji": ":white_check_mark:" if resolved else icon,
        "text": "\n\n".join(lines),
    }


# --------------------------------------------------------------------------- chat API

def _post(method: str, body: dict) -> dict:
    """The only function that touches the network. Tests replace it."""
    token = os.environ[CHAT_TOKEN_ENV]
    req = urllib.request.Request(
        CHAT_API + method,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read())


def call(method: str, body: dict) -> dict:
    """Call the chat API and verify the destination actually accepted it.

    THE POINT OF THIS FUNCTION: a chat API can return HTTP 200 and display nothing --
    wrong channel, revoked scope, archived conversation. Transport success is not delivery.
    So the LOGICAL status in the response body is what decides, and a failure raises.

    Without this check a relay runs clean for a day while delivering nothing, and the only
    symptom is a quiet channel, which is indistinguishable from a quiet week.

    It raises rather than returning, so the failure reaches the function's own error metric.
    That metric is watched by an alarm on a DIFFERENT transport (the README explains why):
    the relay cannot be the thing that tells you the relay is broken.
    """
    result = _post(method, body)
    if not result.get("ok") and result.get("error") != "already_reacted":
        # e.g. channel_not_found, not_in_channel, invalid_auth, is_archived
        raise DeliveryFailed(f"{method}: destination rejected the call: {result.get('error')}")
    return result


# --------------------------------------------------------------------------- episodes

def fire(key: str, channel: str, identity: dict, alerts: list[dict]) -> tuple[int, str]:
    """Post an episode once, and post it AGAIN after it has resolved.

    The duplicate check is "is there an OPEN record for this group in this channel" -- not
    "is there a record". The weaker check looks identical in review and is wrong: after any
    resolve, every later firing of the same alert is skipped while the channel still shows
    the old message with its resolved mark. Measured in the reference estate: every re-fire
    dropped for the whole life of the record, one production alert shown resolved for a week
    while it was firing.
    """
    record = STATE.get(key)
    if is_open(record, channel):
        log.info("already posted, still open: %s", key)
        return 200, "already posted"

    result = call("chat.postMessage", {"channel": channel, **render(identity, alerts)})
    STATE[key] = Record(ts=result["ts"], channel=channel, status="open")
    return 200, f"posted to {channel}"


def resolve(key: str, channel: str, identity: dict, alerts: list[dict]) -> tuple[int, str]:
    """Mark the ORIGINAL message rather than posting a second one.

    A channel of paired fire/resolve messages reads as duplicate alerts at a glance, and
    readers learn to discount the channel. A reaction on the original keeps one message per
    episode and still records the outcome.

    If there is nothing to mark -- the relay was restarted, the record expired -- post a
    resolved message anyway. Losing the resolve is worse than one extra message.
    """
    record = STATE.get(key)
    if record is None or not record.ts:
        log.warning("no tracked message for %s; posting the resolve instead", key)
        call("chat.postMessage", {"channel": channel, **render(identity, alerts, resolved=True)})
        return 200, "resolved (untracked, posted)"

    if record.status == "resolved":
        return 200, "already resolved"

    call("reactions.add", {"channel": record.channel, "timestamp": record.ts,
                           "name": RESOLVED_REACTION})
    STATE[key] = Record(ts=record.ts, channel=record.channel, status="resolved")
    return 200, "resolved"


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
        return 200, "no alerts in payload"

    key = group_key(route_key, payload)
    if (payload.get("status") or "firing").lower() == "resolved":
        return resolve(key, channel, identity, alerts)
    return fire(key, channel, identity, alerts)
