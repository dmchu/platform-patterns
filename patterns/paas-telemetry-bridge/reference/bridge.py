#!/usr/bin/env python3
"""PaaS telemetry bridge -- one receiver, two delivery semantics, a probe independent of the data.

A managed frontend platform pushes signed telemetry at a URL you supply; a log backend accepts
only its own protocol. This sits between them and does the five jobs the pattern names:

    verify the signature over the RAW body     before a single byte is parsed
    normalise platform ids -> service name     projectId / projectName -> the name your traces use
    allowlist labels                           everything else is a field inside the line
    sample by class                            errors, fatals and crashes always; the rest by rate
    dedupe by id within a TTL                  drain records and webhook deliveries alike

Routes:
    POST /drain     the platform's log drain: a JSON array or NDJSON, signed with BRIDGE_DRAIN_SECRET
    POST /webhook   the platform's event webhook: one JSON object, signed with BRIDGE_WEBHOOK_SECRET
    GET  /metrics   Prometheus text exposition of THIS INSTANCE's counters
    GET  /healthz   process liveness only; it proves nothing about ingestion -- that is `probe`

The two ingest paths answer a backend failure differently, on purpose (ADR-003): the drain
acknowledges with a 200 whose body says so, and counts the loss, unless
BRIDGE_DRAIN_FAIL_ON_BACKEND_ERROR=true; the webhook answers 500, so that a platform which
redelivers on a non-2xx retries it, and the dedupe makes that retry safe.

Two entry points over one core, Bridge.handle(method, path, headers, raw_body, now), and one
client of it:
    python3 bridge.py serve --port 8080                      a threaded HTTP server
    handler(event, context)                                  an AWS Lambda adapter (Function URL / HTTP API v2)
    python3 bridge.py probe --url https://... --timeout 60   the heartbeat, on the other side of the front
        door: sign one synthetic record, POST it to the bridge, then ask the BACKEND for it.
        Exit 0 found, 2 not found, 1 the bridge rejected it or the probe could not run.

Configuration is environment only. There is no default secret and no flag to skip verification;
the process refuses to start without the secrets and a backend URL.

    BRIDGE_DRAIN_SECRET, BRIDGE_WEBHOOK_SECRET      required; the webhook secret is shown once at creation,
                                                    the drain secret can be read or replaced from the drain's Edit dialog
    BRIDGE_BACKEND                                  loki (default) | otlp
    BRIDGE_BACKEND_URL                              .../loki/api/v1/push  or  .../v1/logs
    BRIDGE_BACKEND_USER, BRIDGE_BACKEND_TOKEN       the write-only credential; basic auth with both, bearer with the token alone
    BRIDGE_BACKEND_TIMEOUT_SECONDS                  seconds per backend call (default 10); the probe reuses it
    BRIDGE_CONFIG                                   JSON file: service_map, label_allowlist, sample_rates (config.example.json)
    BRIDGE_SERVICE_MAP                              inline JSON or a path; overrides the file
    BRIDGE_LABEL_ALLOWLIST                          comma-separated; overrides the file
    BRIDGE_SAMPLE_RATE_STATIC, BRIDGE_SAMPLE_RATE_DEFAULT    0..1; override the file
    BRIDGE_DEDUPE_TTL_SECONDS, BRIDGE_DEDUPE_MAX_ENTRIES     the dedupe is bounded in time and in size
    BRIDGE_DRAIN_FAIL_ON_BACKEND_ERROR              true: the drain answers 500 instead of acknowledging a failed write
    BRIDGE_QUERY_URL, BRIDGE_QUERY_USER, BRIDGE_QUERY_TOKEN  probe only: a READ credential, never the bridge's

Standard library only. Counters and the dedupe are per instance (ADR-004); /metrics says so.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import http.client
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping

log = logging.getLogger("bridge")

SIGNATURE_HEADER = "x-vercel-signature"
PLATFORM = "vercel"
PROBE_SOURCE = "probe"                    # not a platform source, so the heartbeat has a stream of its own
PROBE_SERVICE = "bridge-probe"            # and an identity of its own, so it never shows up as an unmapped project
MAX_BODY_BYTES = 5 * 1024 * 1024 + 64 * 1024    # the platform batches up to 5 MB; a margin for framing
BODY_DISCARD_CAP = 4 * MAX_BODY_BYTES           # drain an oversize body up to this so the 413 reaches the sender
SOCKET_TIMEOUT_S = 30                           # a stalled body read raises instead of parking a thread
DEFAULT_ALLOWLIST = ("service_name", "service_identity", "source", "level", "environment", "platform")
SEVERITY = {"info": 9, "warning": 13, "error": 17, "fatal": 21}      # OTLP severityNumber
METRICS = {
    "bridge_records_total": ("counter", "Drain records by outcome: accepted, sampled_out, duplicate, "
                             "invalid, dropped (the write failed and was acknowledged)."),
    "bridge_batches_total": ("counter", "Drain batches by result: written, empty, backend_error, "
                             "rejected_size, rejected_signature, rejected_body."),
    "bridge_webhook_events_total": ("counter", "Webhook events by type, counted once per delivery id "
                                    "BEFORE the write: the series alerts threshold on."),
    "bridge_webhook_deliveries_total": ("counter", "Webhook deliveries by status: accepted, duplicate, "
                                        "retried, backend_error, rejected_size, rejected_signature, "
                                        "rejected_body."),
    "bridge_backend_write_failures_total": ("counter", "Backend writes that failed, by ingest path."),
    "bridge_backend_last_success_timestamp_seconds": ("gauge", "Unix time of this instance's last "
                                                      "successful backend write; 0 until there is one."),
}
PER_INSTANCE = ("Per instance: scraped through one address across several instances this is a "
                "sawtooth, not a counter (ADR-004).")
REJECTED = "rejected_size rejected_signature rejected_body"
SEEDS = {"bridge_records_total": ("outcome", "accepted sampled_out duplicate invalid dropped"),
         "bridge_batches_total": ("result", "written empty backend_error " + REJECTED),
         "bridge_webhook_deliveries_total": ("status", "accepted duplicate retried backend_error " + REJECTED),
         "bridge_backend_write_failures_total": ("path", "drain webhook")}

HttpFn = Callable[[str, dict, bytes | None, float], tuple[int, bytes]]


class ConfigError(Exception):
    """The process must not start. The message names variables, never values."""


class BackendError(Exception):
    """The backend did not accept the write. Carries a status or an error class: never the URL or
    the headers, which is where a credential could travel."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- configuration

@dataclass(frozen=True, repr=False)       # repr=False: a generated repr would print the secrets
class Config:
    drain_secret: str
    webhook_secret: str
    backend: str
    backend_url: str
    backend_user: str
    backend_token: str
    timeout: float
    service_map: dict[str, str]
    label_allowlist: tuple[str, ...]
    sample_rate_static: float
    sample_rate_default: float
    dedupe_ttl_s: float
    dedupe_max: int
    drain_fail_on_backend_error: bool
    query_url: str
    query_user: str
    query_token: str

    @classmethod
    def from_env(cls, env: Mapping[str, str], need_webhook: bool = True) -> Config:
        """File first (BRIDGE_CONFIG), environment on top. Secrets come only from the environment."""
        required = ["BRIDGE_DRAIN_SECRET", "BRIDGE_BACKEND_URL"] + (["BRIDGE_WEBHOOK_SECRET"] if need_webhook else [])
        missing = [k for k in required if not env.get(k)]
        if missing:
            raise ConfigError("refusing to start: " + ", ".join(missing) + " not set. There is no default "
                              "secret and no flag to skip verification.")
        backend = env.get("BRIDGE_BACKEND", "loki").lower()
        if backend not in ("loki", "otlp"):
            raise ConfigError(f"BRIDGE_BACKEND must be loki or otlp, not {backend!r}")
        file_cfg = _load_json(env["BRIDGE_CONFIG"]) if env.get("BRIDGE_CONFIG") else {}
        service_map = (_json_or_path(env["BRIDGE_SERVICE_MAP"]) if env.get("BRIDGE_SERVICE_MAP")
                       else file_cfg.get("service_map", {}))
        allow = ([s.strip() for s in env["BRIDGE_LABEL_ALLOWLIST"].split(",") if s.strip()]
                 if env.get("BRIDGE_LABEL_ALLOWLIST") else file_cfg.get("label_allowlist", list(DEFAULT_ALLOWLIST)))
        rates = file_cfg.get("sample_rates", {})
        return cls(
            drain_secret=env["BRIDGE_DRAIN_SECRET"], webhook_secret=env.get("BRIDGE_WEBHOOK_SECRET", ""),
            backend=backend, backend_url=env["BRIDGE_BACKEND_URL"],
            backend_user=env.get("BRIDGE_BACKEND_USER", ""), backend_token=env.get("BRIDGE_BACKEND_TOKEN", ""),
            timeout=float(env.get("BRIDGE_BACKEND_TIMEOUT_SECONDS", "10")),
            service_map={str(k): str(v) for k, v in service_map.items()}, label_allowlist=tuple(allow),
            sample_rate_static=float(env.get("BRIDGE_SAMPLE_RATE_STATIC", rates.get("static", 0.1))),
            sample_rate_default=float(env.get("BRIDGE_SAMPLE_RATE_DEFAULT", rates.get("default", 1.0))),
            dedupe_ttl_s=float(env.get("BRIDGE_DEDUPE_TTL_SECONDS", "3600")),
            dedupe_max=int(env.get("BRIDGE_DEDUPE_MAX_ENTRIES", "100000")),
            drain_fail_on_backend_error=env.get("BRIDGE_DRAIN_FAIL_ON_BACKEND_ERROR", "").lower() in ("1", "true", "yes"),
            query_url=env.get("BRIDGE_QUERY_URL", ""),
            query_user=env.get("BRIDGE_QUERY_USER") or env.get("BRIDGE_BACKEND_USER", ""),
            query_token=env.get("BRIDGE_QUERY_TOKEN") or env.get("BRIDGE_BACKEND_TOKEN", ""),
        )


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _json_or_path(value: str) -> dict:
    return json.loads(value) if value.lstrip().startswith("{") else _load_json(value)


# --------------------------------------------------------------------------- signature

def sign(secret: str, raw: bytes) -> str:
    """HMAC-SHA1 hex digest of the raw body, exactly as the platform computes it."""
    return hmac.new(secret.encode(), raw, hashlib.sha1).hexdigest()


def verify(secret: str, raw: bytes, header: str | None) -> bool:
    """Constant-time comparison over the RAW bytes, before any parsing. The bytes that were signed
    are the bytes that are checked; a re-serialised body would not match, and must not."""
    if not header:
        return False
    return hmac.compare_digest(sign(secret, raw).encode(), header.strip().lower().encode())


# --------------------------------------------------------------------------- dedupe

class Dedupe:
    """key -> (expiry, written), kept in insertion order, which is expiry order. Bounded by a TTL
    and by max_entries, because an unbounded dict in a long-lived instance is a slow leak; both
    bounds evict from the front only, so a full table costs O(1) per insert and never pins the
    lock for a sweep. Per instance: it does not hold across a cold start or a scale-out, which is
    ADR-003's accepted limitation and the reason the drain still defaults to acknowledging."""

    def __init__(self, ttl_s: float, max_entries: int):
        self.ttl_s, self.max_entries = ttl_s, max_entries
        self._entries: OrderedDict[str, tuple[float, bool]] = OrderedDict()
        self._lock = threading.Lock()

    def _get(self, key: str, now: float) -> bool | None:
        hit = self._entries.get(key)
        if hit is None:
            return None
        if hit[0] <= now:
            del self._entries[key]
            return None
        return hit[1]

    def _put(self, key: str, written: bool, now: float) -> None:
        if key in self._entries:
            self._entries.move_to_end(key)           # re-stamped: it is the newest again
        else:
            self._evict(now)
        self._entries[key] = (now + self.ttl_s, written)

    def _evict(self, now: float) -> None:
        """Pop the oldest while it is expired or while the table is full. Never a sweep."""
        while self._entries:
            expiry = next(iter(self._entries.values()))[0]
            if expiry > now and len(self._entries) < self.max_entries:
                return
            self._entries.popitem(last=False)

    def written(self, key: str, now: float) -> bool:
        with self._lock:
            return self._get(key, now) is True

    def claim(self, key: str, now: float) -> bool | None:
        """Atomically return the prior state -- None, False (claimed, not yet written), True
        (written) -- and claim the key if it was unknown. Claiming BEFORE the write is what keeps
        a retried delivery from counting twice."""
        with self._lock:
            prior = self._get(key, now)
            if prior is None:
                self._put(key, False, now)
            return prior

    def mark_written(self, keys, now: float) -> None:
        with self._lock:
            for key in keys:
                self._put(key, True, now)

    def __len__(self) -> int:
        return len(self._entries)


# --------------------------------------------------------------------------- counters

class Counters:
    """In-memory, lock-guarded, per instance. The expected series are seeded at zero so that a
    flat line is a visible flat line rather than an absent one."""

    def __init__(self):
        self._lock = threading.Lock()
        self._values: dict[tuple[str, tuple], float] = {}
        for name, (label, values) in SEEDS.items():
            for value in values.split():
                self.add(name, 0, **{label: value})
        self.set("bridge_backend_last_success_timestamp_seconds", 0)

    def add(self, name: str, by: float = 1, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._values[key] = self._values.get(key, 0) + by

    def set(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._values[(name, tuple(sorted(labels.items())))] = value

    def get(self, name: str, **labels: str) -> float:
        return self._values.get((name, tuple(sorted(labels.items()))), 0)

    def render(self) -> str:
        with self._lock:
            snapshot = sorted(self._values.items())
        lines = []
        for name, (kind, help_) in METRICS.items():
            lines += [f"# HELP {name} {help_} {PER_INSTANCE}", f"# TYPE {name} {kind}"]
            for (n, labels), value in snapshot:
                if n == name:
                    pairs = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
                    text = str(int(value)) if float(value).is_integer() else repr(float(value))
                    lines.append(f"{name}{{{pairs}}} {text}" if pairs else f"{name} {text}")
        return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# --------------------------------------------------------------------------- normalise

@dataclass
class Entry:
    ts_ns: int
    labels: dict[str, str]
    line: str
    level: str


def identity(record: dict, cfg: Config) -> tuple[str, str]:
    """(service name, how it was decided). projectId first, then projectName, against the map;
    the probe's own records are a class apart, so the heartbeat never looks like an unmapped
    project. Otherwise the platform's own name, flagged so every unmapped project is one query
    away: {service_identity="fallback"}."""
    if record.get("source") == PROBE_SOURCE:
        return PROBE_SERVICE, "probe"
    for key in (record.get("projectId"), record.get("projectName")):
        if key is not None and str(key) in cfg.service_map:
            return cfg.service_map[str(key)], "mapped"
    return str(record.get("projectName") or record.get("projectId") or "unknown"), "fallback"


def normalise(record: dict, cfg: Config, now: float) -> Entry:
    """Labels come ONLY from the allowlist; every other field rides inside the line, so a field
    the platform adds next month cannot become a series. Only scalars qualify: an object such as
    `proxy` never becomes a label even if someone lists it."""
    service, how = identity(record, cfg)
    synthetic = {"service_name": service, "service_identity": how, "platform": PLATFORM}
    labels = {}
    for key in cfg.label_allowlist:
        value = synthetic.get(key, record.get(key))
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            labels[key] = str(value)
    ts = record.get("timestamp")
    ts_ns = int(ts) * 1_000_000 if isinstance(ts, (int, float)) and not isinstance(ts, bool) else int(now * 1e9)
    line = json.dumps({**record, "service_name": service}, separators=(",", ":"), sort_keys=True)
    return Entry(ts_ns, labels, line, str(record.get("level") or ""))


def keep(record: dict, cfg: Config) -> bool:
    """Sampling by class. Errors, fatals, crashes and the probe's own records are never sampled;
    static assets go at one rate and everything else at another. The decision is a hash of the
    record id, so a redelivered record decides the same way both times."""
    status = record.get("statusCode")
    if record.get("level") in ("error", "fatal") or record.get("source") == PROBE_SOURCE:
        return True
    if isinstance(status, (int, float)) and not isinstance(status, bool) and (status >= 500 or status == -1):
        return True
    rate = cfg.sample_rate_static if record.get("source") == "static" else cfg.sample_rate_default
    if rate >= 1:
        return True
    digest = hashlib.sha256(str(record.get("id")).encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32 < rate


def event_record(event: dict) -> dict:
    """Shape a webhook delivery like a drain record, so one normaliser serves both paths."""
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    kind = str(event.get("type") or "unknown")
    return {"id": event.get("id"), "type": kind, "source": "webhook", "timestamp": event.get("createdAt"),
            "level": "error" if kind.endswith((".error", ".failed", "-failed")) else "info",
            "projectId": _dotted(payload, "project.id"), "projectName": _dotted(payload, "deployment.name"),
            "environment": payload.get("target"), "region": event.get("region"), "payload": payload}


def _dotted(obj, path: str):
    """The platform documents payload keys as `deployment.name`: accept the literal dotted key and
    the nested object it denotes."""
    if path in obj:
        return obj[path]
    for part in path.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


# --------------------------------------------------------------------------- backend

def loki_payload(entries: list[Entry]) -> dict:
    streams: dict[tuple, list] = {}
    for e in entries:
        streams.setdefault(tuple(sorted(e.labels.items())), []).append([str(e.ts_ns), e.line])
    return {"streams": [{"stream": dict(k), "values": v} for k, v in streams.items()]}


def otlp_payload(entries: list[Entry]) -> dict:
    by_service: dict[str, list] = {}
    for e in entries:
        attrs = [{"key": k, "value": {"stringValue": v}} for k, v in sorted(e.labels.items()) if k != "service_name"]
        by_service.setdefault(e.labels.get("service_name", "unknown"), []).append(
            {"timeUnixNano": str(e.ts_ns), "severityNumber": SEVERITY.get(e.level, 0), "severityText": e.level,
             "body": {"stringValue": e.line}, "attributes": attrs})
    return {"resourceLogs": [
        {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": svc}}]},
         "scopeLogs": [{"scope": {"name": "bridge"}, "logRecords": records}]}
        for svc, records in by_service.items()]}


def auth_headers(user: str, token: str) -> dict:
    if user and token:
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()}
    return {"Authorization": f"Bearer {token}"} if token else {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx from the backend is a failure, not a hop. The default handler would re-send the
    Authorization header to wherever Location points, turn the POST into a GET, and let a 200
    from a login page count as a successful write."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_call(url: str, headers: dict, data: bytes | None, timeout: float) -> tuple[int, bytes]:
    """The only function that touches the network; tests inject a replacement. POST with a body,
    GET without. Every failure becomes a BackendError that carries neither the URL nor the headers:
    a transport that answers garbage, closes mid-response or refuses the scheme is a backend
    failure like any other, and must be counted as one."""
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        raise BackendError(f"HTTP {exc.code}", exc.code) from None
    except urllib.error.URLError as exc:
        raise BackendError(f"URLError: {exc.reason}") from None
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise BackendError(type(exc).__name__) from None         # the type only: a message could carry the URL


# --------------------------------------------------------------------------- core

def _json(http_status: int, **body) -> tuple[int, dict, bytes]:
    return http_status, {"Content-Type": "application/json"}, json.dumps(body).encode()


def parse_batch(raw: bytes) -> list:
    """One pass over the body: a JSON array, one JSON object (however it is laid out), or NDJSON,
    one compact object per line. Anything else is a ValueError, which the caller turns into a 400."""
    text = raw.decode("utf-8")
    head = text.lstrip()[:1]
    if head == "[":
        return json.loads(text)
    if head == "{":
        try:
            return [json.loads(text)]
        except ValueError:
            return [json.loads(line) for line in text.splitlines() if line.strip()]
    raise ValueError("neither a JSON array nor NDJSON")


class Bridge:
    """The whole service behind one call: handle(method, path, headers, raw_body, now) ->
    (status, headers, body). The HTTP server, the Lambda adapter and the tests all go through
    it, and only the injected http function reaches the network."""

    def __init__(self, cfg: Config, http_fn: HttpFn = http_call):
        self.cfg, self.http = cfg, http_fn
        self.counters = Counters()
        self.dedupe = Dedupe(cfg.dedupe_ttl_s, cfg.dedupe_max)

    def handle(self, method: str, path: str, headers: Mapping[str, str], raw: bytes,
               now: float | None = None) -> tuple[int, dict, bytes]:
        now = time.time() if now is None else now
        lower = {k.lower(): v for k, v in headers.items()}
        route = {"/drain": ("POST", self.drain), "/webhook": ("POST", self.webhook),
                 "/metrics": ("GET", self.metrics), "/healthz": ("GET", self.healthz)}.get(path.split("?")[0])
        if route is None:
            return _json(404, code="not_found")
        if method.upper() != route[0]:
            return 405, {"Allow": route[0], "Content-Type": "application/json"}, b'{"code": "method_not_allowed"}'
        try:
            return route[1](lower, raw, now)
        except Exception:                       # a traceback must never reach the caller
            log.exception("unhandled error on %s", path)
            return _json(500, code="internal_error")

    def _gate(self, secret: str, lower: Mapping[str, str], raw: bytes) -> tuple[str, tuple[int, dict, bytes]] | None:
        """Size, then signature, before any parsing. None means pass; otherwise the rejection's
        name, for the counter, and the response."""
        try:
            declared = int(lower.get("content-length") or 0)
        except ValueError:
            declared = 0
        if max(declared, len(raw)) > MAX_BODY_BYTES:
            return "rejected_size", _json(413, code="payload_too_large")
        if not verify(secret, raw, lower.get(SIGNATURE_HEADER)):
            log.warning("signature check failed")           # never the header value, never the body
            return "rejected_signature", _json(403, code="invalid_signature")
        return None

    def drain(self, lower: Mapping[str, str], raw: bytes, now: float) -> tuple[int, dict, bytes]:
        gate = self._gate(self.cfg.drain_secret, lower, raw)
        if gate:
            self.counters.add("bridge_batches_total", result=gate[0])
            return gate[1]
        try:
            records = parse_batch(raw)
        except ValueError:
            self.counters.add("bridge_batches_total", result="rejected_body")
            return _json(400, code="invalid_body")
        tally = {"accepted": 0, "sampled_out": 0, "duplicate": 0, "invalid": 0}
        entries, ids = [], []
        for record in records:
            if not isinstance(record, dict) or record.get("id") in (None, ""):
                tally["invalid"] += 1        # the id is required by the platform; without it nothing can be deduplicated
            elif self.dedupe.written(str(record["id"]), now):
                tally["duplicate"] += 1
            elif not keep(record, self.cfg):
                tally["sampled_out"] += 1
            else:
                entries.append(normalise(record, self.cfg, now))
                ids.append(str(record["id"]))
        for outcome in ("sampled_out", "duplicate", "invalid"):
            self.counters.add("bridge_records_total", tally[outcome], outcome=outcome)
        if not entries:
            self.counters.add("bridge_batches_total", result="empty")
            return _json(200, **tally)
        try:
            self._write(entries, now)
        except BackendError as exc:
            self._failed("drain", exc)
            self.counters.add("bridge_batches_total", result="backend_error")
            if self.cfg.drain_fail_on_backend_error:
                return _json(500, code="backend_error")       # nothing was marked written, so a redelivery is not a duplicate
            self.counters.add("bridge_records_total", len(entries), outcome="dropped")
            # ADR-003: acknowledge, and count the loss. The platform asks for a 200; the body
            # says what happened, which is how the probe tells this apart from a write.
            return _json(200, status="acknowledged", dropped=len(entries), **tally)
        self.dedupe.mark_written(ids, now)
        tally["accepted"] = len(entries)
        self.counters.add("bridge_records_total", len(entries), outcome="accepted")
        self.counters.add("bridge_batches_total", result="written")
        return _json(200, **tally)

    def webhook(self, lower: Mapping[str, str], raw: bytes, now: float) -> tuple[int, dict, bytes]:
        gate = self._gate(self.cfg.webhook_secret, lower, raw)
        if gate:
            self.counters.add("bridge_webhook_deliveries_total", status=gate[0])
            return gate[1]
        try:
            event = json.loads(raw)
            if not isinstance(event, dict) or not event.get("id"):
                raise ValueError("no delivery id")
        except ValueError:
            self.counters.add("bridge_webhook_deliveries_total", status="rejected_body")
            return _json(400, code="invalid_body")
        key, kind = f"webhook:{event['id']}", str(event.get("type") or "unknown")
        prior = self.dedupe.claim(key, now)
        if prior is True:
            self.counters.add("bridge_webhook_deliveries_total", status="duplicate")
            return _json(200, status="duplicate")
        if prior is None:
            self.counters.add("bridge_webhook_events_total", type=kind)     # once per id, before the write
        try:
            self._write([normalise(event_record(event), self.cfg, now)], now)
        except BackendError as exc:
            self._failed("webhook", exc)
            self.counters.add("bridge_webhook_deliveries_total", status="backend_error")
            return _json(500, code="backend_error")            # if the platform redelivers, the retry is not a duplicate
        self.dedupe.mark_written([key], now)
        status = "accepted" if prior is None else "retried"
        self.counters.add("bridge_webhook_deliveries_total", status=status)
        return _json(200, status=status)

    def metrics(self, lower, raw, now) -> tuple[int, dict, bytes]:
        return 200, {"Content-Type": "text/plain; version=0.0.4; charset=utf-8"}, self.counters.render().encode()

    def healthz(self, lower, raw, now) -> tuple[int, dict, bytes]:
        return _json(200, status="alive", note="process liveness only: this proves nothing about ingestion. "
                                                "Run `bridge.py probe` for that.")

    def _write(self, entries: list[Entry], now: float) -> None:
        body = (loki_payload if self.cfg.backend == "loki" else otlp_payload)(entries)
        headers = {"Content-Type": "application/json", **auth_headers(self.cfg.backend_user, self.cfg.backend_token)}
        status, _ = self.http(self.cfg.backend_url, headers, json.dumps(body).encode(), self.cfg.timeout)
        if not 200 <= status < 300:                           # only a 2xx is a write
            raise BackendError(f"HTTP {status}", status)
        self.counters.set("bridge_backend_last_success_timestamp_seconds", now)

    def _failed(self, path: str, exc: BackendError) -> None:
        log.error("%s: backend write failed: %s", path, exc)
        self.counters.add("bridge_backend_write_failures_total", path=path)


# --------------------------------------------------------------------------- entry points

def serve(bridge: Bridge, bind: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = SOCKET_TIMEOUT_S

        def _dispatch(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0:
                    raise ValueError("negative length")
            except ValueError:
                self.close_connection = True
                return self._reply(400, {"Content-Type": "application/json"}, b'{"code": "invalid_content_length"}')
            if length > MAX_BODY_BYTES:
                self._discard(length)                        # refuse by declared size; the gate answers 413
                self.close_connection, raw = True, b""
            else:
                raw = self.rfile.read(length)
            self._reply(*bridge.handle(self.command, self.path, dict(self.headers.items()), raw))

        def _discard(self, length: int) -> None:
            """Read and drop an oversize body so the 413 reaches the sender instead of a reset;
            past the cap, the reset is the answer."""
            if length > BODY_DISCARD_CAP:
                return
            while length > 0:
                chunk = self.rfile.read(min(length, 65536))
                if not chunk:
                    return
                length -= len(chunk)

        def _reply(self, status: int, headers: dict, body: bytes) -> None:
            self.send_response(status)
            for k, v in {**headers, "Content-Length": str(len(body))}.items():
                self.send_header(k, v)
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _dispatch

        def log_message(self, fmt, *args):                   # request line and status: no header, no body
            log.info("%s %s", self.address_string(), fmt % args)

    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    return server


_BRIDGE: Bridge | None = None


def handler(event: dict, context=None) -> dict:
    """AWS Lambda adapter for a Function URL or an API Gateway HTTP API (payload v2): rawPath,
    headers, body and isBase64Encoded in; statusCode, headers and body out. The core is the one
    `serve` uses, so the tests of one are the tests of the other."""
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = Bridge(Config.from_env(os.environ))
    body = event.get("body") or ""
    try:
        raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    except (binascii.Error, ValueError):
        status, headers, out = _json(400, code="invalid_body")
    else:
        method = ((event.get("requestContext") or {}).get("http") or {}).get("method") or event.get("httpMethod") or "GET"
        status, headers, out = _BRIDGE.handle(method, event.get("rawPath") or event.get("path") or "/",
                                              event.get("headers") or {}, raw)
    return {"statusCode": status, "headers": headers, "body": out.decode(), "isBase64Encoded": False}


def marker_found(body: bytes, marker: str) -> bool:
    """Only a stored line counts: data.result[*].values[*][1] of a Loki query_range answer. The
    marker is also in the request URL, so a login page, a block page or a not-found page that
    echoes the request must never pass; a body that is not that shape is 'not found'."""
    try:
        results = json.loads(body)["data"]["result"]
    except (ValueError, KeyError, TypeError):
        return False
    for stream in results if isinstance(results, list) else []:
        values = stream.get("values") if isinstance(stream, dict) else None
        for value in values or []:
            if isinstance(value, list) and len(value) > 1 and marker in str(value[1]):
                return True
    return False


def probe(cfg: Config, url: str, timeout_s: float, query_url: str = "", http_fn: HttpFn = http_call,
          clock=time.time, sleep=time.sleep, out=print) -> int:
    """The heartbeat, independent of the telemetry. One synthetic record carrying a random marker
    goes through the real front door with a real signature, and then the BACKEND is asked for it.
    A stale write credential, a wrong tenant, a drain that acknowledges and drops: all end here as
    exit 2, while /healthz and the platform's delivery log both say fine."""
    marker, now = uuid.uuid4().hex, clock()
    record = {"id": f"probe-{marker}", "deploymentId": "dpl_probe", "source": PROBE_SOURCE, "host": PROBE_SERVICE,
              "timestamp": int(now * 1000), "projectId": PROBE_SERVICE, "projectName": PROBE_SERVICE,
              "level": "info", "message": f"bridge probe {marker}"}
    raw = json.dumps([record]).encode()
    try:
        status, reply = http_fn(url.rstrip("/") + "/drain", {"Content-Type": "application/json",
                                SIGNATURE_HEADER: sign(cfg.drain_secret, raw)}, raw, cfg.timeout)
    except BackendError as exc:
        if exc.status is not None and exc.status >= 500:
            out(f"probe: the bridge answered {exc}: its backend write failed and it is configured not to acknowledge")
            return 2
        out(f"probe: the bridge rejected the record or could not be reached: {exc}")
        return 1
    try:
        accepted = json.loads(reply).get("accepted")
    except (ValueError, AttributeError):
        out(f"probe: the answer at /drain ({status}) was not the bridge's")
        return 1
    if accepted != 1:
        out("probe: the bridge acknowledged without writing: its backend write failed")
        return 2
    if query_url:
        query = query_url.replace("{marker}", marker)
    elif cfg.backend == "loki":
        params = {"query": f'{{source="{PROBE_SOURCE}"}} |= "{marker}"', "limit": "1",
                  "start": str(int((now - 300) * 1e9))}
        query = cfg.backend_url.split("/loki/api/v1/push")[0] + "/loki/api/v1/query_range?" + urllib.parse.urlencode(params)
    else:
        out("probe: --query-url (or BRIDGE_QUERY_URL) is required for a non-Loki backend; put {marker} where the marker goes")
        return 1
    headers, deadline, last = auth_headers(cfg.query_user, cfg.query_token), now + timeout_s, "no response yet"
    while True:
        try:
            _, body = http_fn(query, headers, None, cfg.timeout)
            if marker_found(body, marker):
                out(f"probe: marker found in the backend after {clock() - now:.1f}s")
                return 0
            last = "the query answered without the marker in a stored line"
        except BackendError as exc:
            last = str(exc)
        if clock() >= deadline:
            out(f"probe: marker NOT found within {timeout_s:.0f}s ({last}); the bridge had answered {status}")
            return 2
        sleep(2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve", help="run the receiver as a threaded HTTP server")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--bind", default="127.0.0.1", help="0.0.0.0 inside a container")
    p = sub.add_parser("probe", help="push one signed synthetic record through the bridge and look for it in the backend")
    p.add_argument("--url", required=True, help="the bridge's base URL")
    p.add_argument("--timeout", type=float, default=60, help="seconds to wait for the backend (default 60)")
    p.add_argument("--query-url", default="", help="backend query URL containing {marker}, answering in the Loki "
                                                   "query_range shape; default: derived from BRIDGE_BACKEND_URL")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = Config.from_env(os.environ, need_webhook=args.command == "serve")
    except (ConfigError, OSError, ValueError) as exc:
        print(f"bridge: {exc}", file=sys.stderr)
        return 1
    if args.command == "probe":
        return probe(cfg, args.url, args.timeout, args.query_url or cfg.query_url)
    server = serve(Bridge(cfg), args.bind, args.port)
    log.info("listening on %s:%d, backend %s, %d mapped projects", args.bind, server.server_port,
             cfg.backend, len(cfg.service_map))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
