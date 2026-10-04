"""Tests for the reference bridge. Each test pins one sentence of the pattern's README or of
ADR-003: the signature is checked over the raw bytes, labels come only from the allowlist, errors
are never sampled, the drain acknowledges a failed write by default and the webhook does not, and
the dedupe runs before the counters it protects. No sockets, except the one end-to-end test."""

import base64
import dataclasses
import hashlib
import hmac
import http.client
import http.server
import json
import logging
import pathlib
import re
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

import bridge

DRAIN_SECRET = "drain-secret-for-tests"
WEBHOOK_SECRET = "webhook-secret-for-tests"
BACKEND_TOKEN = "write-only-token-for-tests"
PROJECT_ID = "gdufoJxB6b9b1fEqr1jUtFkyavUU"            # the platform docs' own example identifiers
DEPLOYMENT_ID = "dpl_233NRGRjVZX1caZrXWtz5g1TAksD"

ENV = {
    "BRIDGE_DRAIN_SECRET": DRAIN_SECRET, "BRIDGE_WEBHOOK_SECRET": WEBHOOK_SECRET,
    "BRIDGE_BACKEND_URL": "https://logs.example.com/loki/api/v1/push",
    "BRIDGE_BACKEND_USER": "123456", "BRIDGE_BACKEND_TOKEN": BACKEND_TOKEN,
    "BRIDGE_SERVICE_MAP": json.dumps({PROJECT_ID: "storefront-web", "docs-site": "docs-web"}),
    "BRIDGE_SAMPLE_RATE_STATIC": "0", "BRIDGE_DEDUPE_TTL_SECONDS": "600",
}


class FakeBackend:
    """Stands in for the network: records every write, fails on demand, answers queries."""

    def __init__(self):
        self.writes: list[dict] = []
        self.calls: list[tuple[str, dict, bytes | None]] = []
        self.fail = False

    def __call__(self, url, headers, data, timeout):
        self.calls.append((url, headers, data))
        if data is None:
            return 200, b'{"status":"success","data":{"result":[]}}'
        if self.fail:
            raise bridge.BackendError("HTTP 401")
        self.writes.append(json.loads(data))
        return 204, b""

    def lines(self):
        return [v[1] for w in self.writes for s in w["streams"] for v in s["values"]]

    def streams(self):
        return [s["stream"] for w in self.writes for s in w["streams"]]


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def cfg():
    return bridge.Config.from_env(ENV)


@pytest.fixture
def svc(cfg, backend):
    return bridge.Bridge(cfg, backend)


def sign(secret, raw):
    return hmac.new(secret.encode(), raw, hashlib.sha1).hexdigest()


def record(**over):
    base = {"id": "1573817250283254651097202070", "deploymentId": DEPLOYMENT_ID, "source": "lambda",
            "host": "my-app.vercel.app", "timestamp": 1573817250283, "projectId": PROJECT_ID,
            "projectName": "my-app", "level": "info", "message": "API request processed",
            "statusCode": 200, "path": "/api/users", "environment": "production"}
    base.update(over)
    return base


def event(eid="evt_01", kind="deployment.error", **over):
    ev = {"id": eid, "type": kind, "createdAt": 1700000000000, "region": "iad1",
          "payload": {"deployment": {"id": DEPLOYMENT_ID, "name": "my-app", "url": "my-app.vercel.app", "meta": {}},
                      "project": {"id": PROJECT_ID}, "target": "production", "plan": "pro", "regions": ["iad1"],
                      "links": {"deployment": "https://vercel.com/", "project": "https://vercel.com/"}}}
    ev.update(over)
    return ev


def post(svc, path, body, secret, sig=None, now=1700000000.0, **headers):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    h = {"content-length": str(len(raw)), **headers}
    if secret is not None:
        h[bridge.SIGNATURE_HEADER] = sig if sig is not None else sign(secret, raw)
    return svc.handle("POST", path, h, raw, now)


def drain(svc, records, **kw):
    return post(svc, "/drain", records, DRAIN_SECRET, **kw)


def webhook(svc, ev, **kw):
    return post(svc, "/webhook", ev, WEBHOOK_SECRET, **kw)


def body(resp):
    return json.loads(resp[2])


# --------------------------------------------------------------------------- signature

def test_a_known_hmac_sha1_vector_is_accepted(svc, backend):
    """A fixed vector: HMAC-SHA1, hex, of the docs' sample body under the test secret, pinned so a
    change to sign() cannot pass unnoticed."""
    raw = (b'[{"id":"1573817187330377061717300000","deploymentId":"dpl_233NRGRjVZX1caZrXWtz5g1TAksD",'
           b'"source":"lambda","host":"test.vercel.app","timestamp":1573817187330,'
           b'"projectId":"gdufoJxB6b9b1fEqr1jUtFkyavUU","level":"error","message":"boom"}]')
    digest = "62e7ca0ea0c4b30c061f41c5a30adf7c0d0eadf3"
    assert bridge.sign(DRAIN_SECRET, raw) == digest
    status, _, _ = svc.handle("POST", "/drain", {bridge.SIGNATURE_HEADER: digest}, raw, 0)
    assert status == 200 and len(backend.writes) == 1


def test_a_missing_signature_is_403_and_the_body_is_never_parsed(svc, backend):
    status, _, out = post(svc, "/drain", b"this is not even json", None)
    assert status == 403 and json.loads(out)["code"] == "invalid_signature"
    assert backend.writes == []
    assert svc.counters.get("bridge_batches_total", result="rejected_signature") == 1


def test_a_wrong_signature_is_403(svc):
    assert drain(svc, [record()], sig="00" * 20)[0] == 403


def test_the_signature_covers_the_raw_bytes_not_the_parsed_json(svc):
    pretty = json.dumps([record()], indent=2).encode()
    compact = json.dumps([record()]).encode()
    status, _, _ = svc.handle("POST", "/drain", {bridge.SIGNATURE_HEADER: sign(DRAIN_SECRET, pretty)}, compact, 0)
    assert status == 403


def test_the_comparison_is_constant_time(svc, monkeypatch):
    seen, real = [], hmac.compare_digest
    monkeypatch.setattr(bridge.hmac, "compare_digest", lambda a, b: seen.append((a, b)) or real(a, b))
    assert drain(svc, [record()])[0] == 200
    assert len(seen) == 1 and seen[0][0] == seen[0][1]


# --------------------------------------------------------------------------- body

def test_json_array_and_ndjson_are_both_accepted(svc, backend):
    assert drain(svc, [record(id="a1"), record(id="a2")])[0] == 200
    ndjson = "\n".join(json.dumps(record(id=i)) for i in ("n1", "n2")).encode()
    assert drain(svc, ndjson)[0] == 200
    assert len(backend.lines()) == 4


def test_an_unparseable_body_is_400(svc):
    assert drain(svc, b'[{"id": ')[0] == 400
    assert drain(svc, b"plain text")[0] == 400
    assert svc.counters.get("bridge_batches_total", result="rejected_body") == 2


def test_a_single_object_that_spans_lines_is_valid_json_not_broken_ndjson(svc, backend):
    pretty = json.dumps(record(id="p1"), indent=2).encode()
    assert body(drain(svc, pretty))["accepted"] == 1 and len(backend.lines()) == 1


def test_a_body_over_the_platform_maximum_is_413_even_when_signed(svc, backend):
    assert drain(svc, b"[" + b" " * bridge.MAX_BODY_BYTES + b"]")[0] == 413
    # The server refuses by declared length without reading, so the gate must honour the header too.
    headers = {"content-length": str(bridge.MAX_BODY_BYTES + 1), bridge.SIGNATURE_HEADER: sign(DRAIN_SECRET, b"")}
    assert svc.handle("POST", "/drain", headers, b"", 0)[0] == 413
    assert backend.writes == [] and svc.counters.get("bridge_batches_total", result="rejected_size") == 2


def test_an_unknown_path_is_404_and_the_wrong_method_is_405(svc):
    assert svc.handle("GET", "/nope", {}, b"", 0)[0] == 404
    status, headers, _ = svc.handle("GET", "/drain", {}, b"", 0)
    assert status == 405 and headers["Allow"] == "POST"
    assert svc.handle("POST", "/metrics", {}, b"", 0)[0] == 405


# --------------------------------------------------------------------------- normalise

def test_the_project_id_maps_to_the_service_name(svc, backend):
    drain(svc, [record()])
    assert backend.streams() == [{"service_name": "storefront-web", "service_identity": "mapped",
                                  "source": "lambda", "level": "info", "environment": "production",
                                  "platform": "vercel"}]


def test_the_project_name_maps_when_the_id_does_not(svc, backend):
    drain(svc, [record(projectId="prj_unknown", projectName="docs-site")])
    assert backend.streams()[0]["service_name"] == "docs-web"


def test_an_unmapped_project_falls_back_to_its_platform_name_and_says_so(svc, backend):
    drain(svc, [record(projectId="prj_unknown", projectName="admin-console")])
    stream = backend.streams()[0]
    assert (stream["service_name"], stream["service_identity"]) == ("admin-console", "fallback")


def test_a_field_outside_the_allowlist_is_a_line_field_not_a_label(svc, backend):
    drain(svc, [record(ja5Digest="a-field-the-platform-adds-next-month", proxy={"method": "GET"})])
    labels = backend.streams()[0]
    assert "ja5Digest" not in labels and "proxy" not in labels and "host" not in labels
    line = json.loads(backend.lines()[0])
    assert line["ja5Digest"] == "a-field-the-platform-adds-next-month" and line["proxy"]["method"] == "GET"
    assert line["service_name"] == "storefront-web"


def test_allowlisting_an_object_field_still_does_not_make_it_a_label(cfg, backend):
    wider = dataclasses.replace(cfg, label_allowlist=cfg.label_allowlist + ("proxy", "host"))
    drain(bridge.Bridge(wider, backend), [record(proxy={"method": "GET"})])
    assert "proxy" not in backend.streams()[0] and backend.streams()[0]["host"] == "my-app.vercel.app"


def test_a_millisecond_timestamp_becomes_a_nanosecond_string(svc, backend):
    drain(svc, [record(timestamp=1573817250283)])
    assert backend.writes[0]["streams"][0]["values"][0][0] == "1573817250283000000"


def test_the_otlp_shape_carries_service_name_as_a_resource_attribute(cfg, backend):
    otlp = dataclasses.replace(cfg, backend="otlp", backend_url="https://otlp.example.com/v1/logs")
    drain(bridge.Bridge(otlp, backend), [record(level="warning")])
    resource = backend.writes[0]["resourceLogs"][0]
    assert resource["resource"]["attributes"] == [{"key": "service.name", "value": {"stringValue": "storefront-web"}}]
    rec = resource["scopeLogs"][0]["logRecords"][0]
    assert (rec["timeUnixNano"], rec["severityNumber"], rec["severityText"]) == ("1573817250283000000", 13, "warning")


# --------------------------------------------------------------------------- sampling

def test_errors_and_fatals_are_never_sampled(cfg):
    zero = dataclasses.replace(cfg, sample_rate_default=0.0, sample_rate_static=0.0)
    assert bridge.keep(record(level="error"), zero) and bridge.keep(record(level="fatal", source="static"), zero)
    assert not bridge.keep(record(level="info"), zero)


def test_crashes_and_server_errors_are_never_sampled(cfg):
    zero = dataclasses.replace(cfg, sample_rate_default=0.0)
    assert bridge.keep(record(statusCode=-1), zero) and bridge.keep(record(statusCode=503), zero)
    assert not bridge.keep(record(statusCode=404), zero)


def test_static_assets_sample_at_their_own_rate(svc, backend):
    """The fixture sets the static rate to 0 and leaves the default at 1."""
    assert body(drain(svc, [record(id="s1", source="static"), record(id="l1")])) == {
        "accepted": 1, "sampled_out": 1, "duplicate": 0, "invalid": 0}
    assert svc.counters.get("bridge_records_total", outcome="sampled_out") == 1
    assert len(backend.lines()) == 1


def test_sampling_is_deterministic_by_record_id(cfg):
    half = dataclasses.replace(cfg, sample_rate_default=0.5)
    ids = [f"157381725028325465109720{i:04d}" for i in range(400)]
    first = [bridge.keep(record(id=i), half) for i in ids]
    assert first == [bridge.keep(record(id=i), half) for i in ids]      # a redelivery decides the same way
    assert 120 < sum(first) < 280                                        # and the rate is roughly honoured


def test_probe_records_are_never_sampled_out(cfg):
    zero = dataclasses.replace(cfg, sample_rate_default=0.0)
    assert bridge.keep({"id": "probe-x", "source": bridge.PROBE_SOURCE, "level": "info"}, zero)


# --------------------------------------------------------------------------- dedupe on the drain

def test_a_redelivered_drain_record_is_a_duplicate_not_a_second_line(svc, backend):
    drain(svc, [record(id="r1"), record(id="r2")])
    out = body(drain(svc, [record(id="r2"), record(id="r3")]))
    assert (out["duplicate"], out["accepted"]) == (1, 1)
    assert len(backend.lines()) == 3
    assert svc.counters.get("bridge_records_total", outcome="duplicate") == 1


def test_the_drain_dedupe_forgets_after_the_ttl(svc):
    drain(svc, [record(id="r1")], now=1000.0)
    assert body(drain(svc, [record(id="r1")], now=1000.0 + 599))["duplicate"] == 1
    assert body(drain(svc, [record(id="r1")], now=1000.0 + 601))["accepted"] == 1


def test_the_dedupe_is_bounded_in_memory(cfg, backend):
    svc = bridge.Bridge(dataclasses.replace(cfg, dedupe_max=3), backend)
    drain(svc, [record(id=f"r{i}") for i in range(10)])
    assert len(svc.dedupe) == 3
    assert body(drain(svc, [record(id="r9"), record(id="r0")])) == {
        "accepted": 1, "sampled_out": 0, "duplicate": 1, "invalid": 0}      # the newest are the ones kept


def test_eviction_from_a_full_dedupe_is_cheap_because_it_pops_from_the_front(cfg):
    """A full table used to be swept on every insert, holding the lock for the whole batch; the
    eviction now pops the oldest entry only, so a batch into a full table costs what an empty
    one does."""
    table = bridge.Dedupe(3600, 100_000)
    table.mark_written((f"k{i}" for i in range(100_000)), 1000.0)
    started = time.perf_counter()
    table.mark_written([f"n{i}" for i in range(2_000)], 1001.0)
    assert time.perf_counter() - started < 1.0
    assert len(table) == 100_000 and table.written("n1999", 1001.0) and not table.written("k0", 1001.0)


def test_a_record_without_an_id_is_invalid_because_it_cannot_be_deduplicated(svc, backend):
    orphan = record()
    del orphan["id"]
    assert body(drain(svc, [orphan, "not-an-object"]))["invalid"] == 2
    assert backend.writes == []


# --------------------------------------------------------------------------- delivery semantics (ADR-003)

def test_the_drain_acknowledges_a_failed_write_by_default_and_counts_the_loss(svc, backend):
    """The platform asks for a 200; the body, not the status, says that nothing was written."""
    backend.fail = True
    status, _, out = drain(svc, [record(id="r1"), record(id="r2")])
    assert status == 200 and json.loads(out) == {"status": "acknowledged", "dropped": 2, "accepted": 0,
                                                 "sampled_out": 0, "duplicate": 0, "invalid": 0}
    assert svc.counters.get("bridge_records_total", outcome="dropped") == 2
    assert svc.counters.get("bridge_backend_write_failures_total", path="drain") == 1
    assert svc.counters.get("bridge_batches_total", result="backend_error") == 1


def test_a_non_2xx_from_the_backend_is_a_failed_write_not_a_success(cfg):
    """A 3xx with a 200 behind it, or a 1xx, is not a write; only a 2xx marks records written."""
    def redirecting(url, headers, data, timeout):
        return 302, b""
    svc = bridge.Bridge(cfg, redirecting)
    status, _, out = drain(svc, [record(id="r1")])
    assert status == 200 and json.loads(out)["status"] == "acknowledged"
    assert svc.counters.get("bridge_backend_write_failures_total", path="drain") == 1
    assert not svc.dedupe.written("r1", 1700000000.0)


@pytest.mark.parametrize("raised", [http.client.BadStatusLine("HELLO NOT HTTP"), http.client.IncompleteRead(b""),
                                    ValueError("unknown url type"), ConnectionResetError()])
def test_a_transport_failure_of_any_class_is_an_acknowledged_failure_with_its_counters(cfg, monkeypatch, raised):
    """urllib lets http.client exceptions and ValueError escape past OSError; they must still be a
    backend failure, so the drain's documented answer and the metrics the README alerts on hold."""
    monkeypatch.setattr(bridge._OPENER, "open", lambda *a, **k: (_ for _ in ()).throw(raised))
    svc = bridge.Bridge(cfg)                                 # the real http_call, a broken transport
    status, _, out = drain(svc, [record(id="r1")])
    assert status == 200 and json.loads(out)["status"] == "acknowledged"
    assert svc.counters.get("bridge_backend_write_failures_total", path="drain") == 1
    assert svc.counters.get("bridge_records_total", outcome="dropped") == 1
    assert webhook(svc, event())[0] == 500
    assert svc.counters.get("bridge_backend_write_failures_total", path="webhook") == 1


def test_http_call_refuses_a_redirect_instead_of_following_it_with_the_credential():
    """A backend that answers 302 must not turn the push into a GET of the Location carrying the
    Authorization header, nor let the landing page's 200 count as a write."""
    seen = []

    class Backend(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(("POST", self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "/landing")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            seen.append(("GET", self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Backend)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(bridge.BackendError) as refused:
            bridge.http_call(f"http://127.0.0.1:{server.server_port}/loki/api/v1/push",
                             {"Authorization": "Basic not-for-the-landing-page"}, b"{}", 5)
        assert str(refused.value) == "HTTP 302" and refused.value.status == 302
        assert [s[0] for s in seen] == ["POST"]              # the credential went to the backend once, and nowhere else
    finally:
        server.shutdown()
        server.server_close()


def test_the_drain_returns_500_when_configured_and_the_retry_is_not_a_duplicate(cfg, backend):
    svc = bridge.Bridge(dataclasses.replace(cfg, drain_fail_on_backend_error=True), backend)
    backend.fail = True
    assert drain(svc, [record(id="r1")])[0] == 500
    backend.fail = False
    assert body(drain(svc, [record(id="r1")])) == {"accepted": 1, "sampled_out": 0, "duplicate": 0, "invalid": 0}


def test_a_duplicate_webhook_is_decided_before_the_counters_increment(svc, backend):
    assert body(webhook(svc, event()))["status"] == "accepted"
    assert body(webhook(svc, event()))["status"] == "duplicate"
    assert svc.counters.get("bridge_webhook_events_total", type="deployment.error") == 1
    assert svc.counters.get("bridge_webhook_deliveries_total", status="duplicate") == 1
    assert len(backend.lines()) == 1


def test_a_webhook_backend_failure_is_500_and_the_retry_writes_without_double_counting(svc, backend):
    """The coupling ADR-003 names: the 5xx is only safe because the dedupe makes the retry safe."""
    backend.fail = True
    assert webhook(svc, event())[0] == 500
    assert svc.counters.get("bridge_webhook_events_total", type="deployment.error") == 1
    backend.fail = False
    assert body(webhook(svc, event()))["status"] == "retried"
    assert svc.counters.get("bridge_webhook_events_total", type="deployment.error") == 1
    assert len(backend.lines()) == 1


def test_a_webhook_with_a_bad_signature_is_403_and_counted(svc, backend):
    """A wrong BRIDGE_WEBHOOK_SECRET must be visible in /metrics, not only in a WARNING line."""
    assert webhook(svc, event(), sig="deadbeef")[0] == 403
    assert post(svc, "/webhook", event(), None)[0] == 403
    assert post(svc, "/webhook", b"not json", WEBHOOK_SECRET)[0] == 400
    assert backend.writes == []
    assert svc.counters.get("bridge_webhook_deliveries_total", status="rejected_signature") == 2
    assert svc.counters.get("bridge_webhook_deliveries_total", status="rejected_body") == 1


def test_a_webhook_event_lands_as_a_record_of_its_own_source(svc, backend):
    webhook(svc, event())
    stream = backend.streams()[0]
    assert (stream["source"], stream["service_name"], stream["level"], stream["environment"]) == (
        "webhook", "storefront-web", "error", "production")
    assert json.loads(backend.lines()[0])["type"] == "deployment.error"


def test_a_webhook_payload_with_dotted_keys_is_read_too(svc, backend):
    webhook(svc, event(payload={"project.id": PROJECT_ID, "deployment.name": "my-app", "target": "staging"}))
    assert backend.streams()[0]["service_name"] == "storefront-web"
    assert backend.streams()[0]["environment"] == "staging"


# --------------------------------------------------------------------------- observability

METRIC_LINE = re.compile(r'^[a-z_]+(\{[a-z_]+="[^"]*"(,[a-z_]+="[^"]*")*\})? -?\d+(\.\d+)?$')


def test_the_metrics_exposition_parses_and_says_it_is_per_instance(svc):
    drain(svc, [record()])
    webhook(svc, event())
    status, headers, out = svc.handle("GET", "/metrics", {}, b"", 0)
    assert status == 200 and headers["Content-Type"].startswith("text/plain")
    text, typed = out.decode(), set()
    for line in text.strip().splitlines():
        if line.startswith("# HELP"):
            assert "Per instance" in line
        elif line.startswith("# TYPE"):
            typed.add(line.split()[2])
        else:
            assert METRIC_LINE.match(line), line
    assert typed == set(bridge.METRICS)
    assert 'bridge_records_total{outcome="accepted"} 1' in text
    assert 'bridge_webhook_events_total{type="deployment.error"} 1' in text
    assert "bridge_backend_last_success_timestamp_seconds 1700000000\n" in text
    # Seeded at zero, so the series a rotated secret shows up in exists before it is non-zero.
    assert 'bridge_batches_total{result="rejected_signature"} 0' in text
    assert 'bridge_webhook_deliveries_total{status="rejected_signature"} 0' in text


def test_healthz_says_it_proves_nothing_about_ingestion(svc):
    status, _, out = svc.handle("GET", "/healthz", {}, b"", 0)
    assert status == 200 and "proves nothing about ingestion" in out.decode()


def test_no_secret_reaches_a_log_line_or_a_response(svc, backend, caplog):
    caplog.set_level(logging.DEBUG)
    backend.fail = True
    responses = [drain(svc, [record()]), drain(svc, [record()], sig="bad"), webhook(svc, event()),
                 svc.handle("GET", "/metrics", {}, b"", 0), svc.handle("GET", "/healthz", {}, b"", 0),
                 svc.handle("GET", "/nope", {}, b"", 0)]
    blob = caplog.text + "".join(r[2].decode() for r in responses) + repr(svc.cfg) + str(svc.cfg)
    for secret in (DRAIN_SECRET, WEBHOOK_SECRET, BACKEND_TOKEN):
        assert secret not in blob
    assert "backend write failed" in caplog.text


# --------------------------------------------------------------------------- adapters

def test_the_lambda_adapter_decodes_base64_and_ignores_header_case(svc, monkeypatch):
    monkeypatch.setattr(bridge, "_BRIDGE", svc)
    raw = json.dumps([record()]).encode()
    ev = {"rawPath": "/drain", "requestContext": {"http": {"method": "POST"}}, "isBase64Encoded": True,
          "body": base64.b64encode(raw).decode(), "headers": {"X-Vercel-Signature": sign(DRAIN_SECRET, raw)}}
    out = bridge.handler(ev, None)
    assert out["statusCode"] == 200 and json.loads(out["body"])["accepted"] == 1
    probe = bridge.handler({"rawPath": "/healthz", "requestContext": {"http": {"method": "GET"}}}, None)
    assert probe["statusCode"] == 200 and probe["isBase64Encoded"] is False


def test_the_lambda_adapter_answers_400_to_a_body_that_is_not_base64(svc, monkeypatch):
    """A decode error must be a 400 from the bridge, not a runtime error the gateway turns into a 502."""
    monkeypatch.setattr(bridge, "_BRIDGE", svc)
    out = bridge.handler({"rawPath": "/drain", "isBase64Encoded": True, "body": "!!!notbase64", "headers": {}}, None)
    assert out["statusCode"] == 400 and json.loads(out["body"])["code"] == "invalid_body"


def test_end_to_end_over_a_real_socket(cfg, backend):
    server = bridge.serve(bridge.Bridge(cfg, backend), "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        raw = json.dumps([record(id="e1"), record(id="e2", level="error")]).encode()
        signed = urllib.request.Request(base + "/drain", data=raw, headers={bridge.SIGNATURE_HEADER: sign(DRAIN_SECRET, raw)})
        with urllib.request.urlopen(signed, timeout=5) as resp:
            assert resp.status == 200 and json.loads(resp.read())["accepted"] == 2
        with pytest.raises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(urllib.request.Request(base + "/drain", data=raw), timeout=5)
        assert rejected.value.code == 403
        with urllib.request.urlopen(base + "/metrics", timeout=5) as resp:
            assert 'bridge_records_total{outcome="accepted"} 2' in resp.read().decode()
        # An oversize body is drained so the 413 reaches the sender instead of a connection reset.
        big = b"[" + b" " * (bridge.MAX_BODY_BYTES + 1) + b"]"
        with pytest.raises(urllib.error.HTTPError) as too_big:
            urllib.request.urlopen(urllib.request.Request(base + "/drain", data=big,
                                   headers={bridge.SIGNATURE_HEADER: sign(DRAIN_SECRET, big)}), timeout=10)
        assert too_big.value.code == 413
        # A Content-Length the server cannot trust is a 400 and a closed connection, not a traceback or a parked thread.
        for bad in (b"abc", b"-1"):
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
                sock.sendall(b"POST /drain HTTP/1.1\r\nHost: x\r\nContent-Length: " + bad + b"\r\n\r\n")
                reply = b""
                while chunk := sock.recv(4096):              # the server closes after the reply, so read to EOF
                    reply += chunk
            assert reply.startswith(b"HTTP/1.1 400") and b"Connection: close" in reply and b"invalid_content_length" in reply
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- probe

def ticking():
    seconds = iter(range(10000))
    return lambda: float(next(seconds))


def test_the_probe_exits_0_when_the_marker_comes_back_from_the_backend(cfg, backend):
    """The fake transport is the bridge's front door AND the backend's query API: the POST is
    answered by a real Bridge, and the query returns whatever that Bridge wrote."""
    svc, urls = bridge.Bridge(cfg, backend), []

    def transport(url, headers, data, timeout):
        urls.append(url)
        if data is not None:
            status, _, out = svc.handle("POST", "/drain", headers, data, 0)
            if status >= 400:
                raise bridge.BackendError(f"HTTP {status}")
            return status, out
        return 200, json.dumps({"data": {"result": [{"values": [["0", ln]]} for ln in backend.lines()]}}).encode()

    assert bridge.probe(cfg, "https://bridge.example.com/", 10, http_fn=transport, clock=ticking(), sleep=lambda s: None) == 0
    assert urls[0] == "https://bridge.example.com/drain"
    assert urls[1].startswith("https://logs.example.com/loki/api/v1/query_range?query=%7Bsource%3D%22probe%22")
    stream = backend.streams()[0]
    assert (stream["source"], stream["service_name"], stream["service_identity"]) == ("probe", "bridge-probe", "probe")
    # The heartbeat is a class of its own, never an unmapped project: {service_identity="fallback"} stays clean.


def test_the_probe_exits_2_when_the_marker_never_appears(cfg):
    def transport(url, headers, data, timeout):
        return (200, b'{"accepted": 1}') if data is not None else (200, b'{"data":{"result":[]}}')
    assert bridge.probe(cfg, "https://bridge.example.com", 10, http_fn=transport, clock=ticking(), sleep=lambda s: None) == 2


def test_the_probe_exits_2_when_the_bridge_acknowledged_without_writing(cfg):
    """The stale-credential case: a 200 from the bridge, success in the platform's UI, nothing stored."""
    acknowledged = b'{"status": "acknowledged", "dropped": 1, "accepted": 0, "sampled_out": 0, "duplicate": 0, "invalid": 0}'
    assert bridge.probe(cfg, "https://bridge.example.com", 10, http_fn=lambda *a: (200, acknowledged), clock=ticking(),
                        sleep=lambda s: None) == 2


def test_the_probe_exits_2_when_the_bridge_is_configured_to_answer_500_for_the_same_failure(cfg):
    """BRIDGE_DRAIN_FAIL_ON_BACKEND_ERROR=true turns the acknowledgement into a 500; it is still the
    backend that failed, so the exit code is the same."""
    def failing_bridge(url, headers, data, timeout):
        raise bridge.BackendError("HTTP 500", 500)
    assert bridge.probe(cfg, "https://bridge.example.com", 10, http_fn=failing_bridge, clock=ticking(), sleep=lambda s: None) == 2


def test_the_probe_does_not_trust_a_page_that_echoes_the_marker_back(cfg):
    """The marker is in the query URL, so a login or block page that quotes the request contains
    it; only a stored line in a query_range answer counts."""
    def transport(url, headers, data, timeout):
        if data is not None:
            return 200, b'{"accepted": 1}'
        return 200, f"<html><body>Please sign in. You asked for {url}</body></html>".encode()
    assert bridge.probe(cfg, "https://bridge.example.com", 10, http_fn=transport, clock=ticking(), sleep=lambda s: None) == 2
    echo_in_json = lambda url, headers, data, timeout: (200, (b'{"accepted": 1}' if data is not None
                                                              else json.dumps({"error": f"no such path {url}"}).encode()))
    assert bridge.probe(cfg, "https://bridge.example.com", 10, http_fn=echo_in_json, clock=ticking(), sleep=lambda s: None) == 2


def test_the_probe_exits_1_when_something_other_than_the_bridge_answers_at_drain(cfg):
    html = lambda *a: (200, b"<html>welcome to the proxy</html>")
    assert bridge.probe(cfg, "https://bridge.example.com", 10, http_fn=html, clock=ticking(), sleep=lambda s: None) == 1


def test_the_probe_exits_1_when_the_bridge_rejects_the_record(cfg):
    def rejecting(url, headers, data, timeout):
        raise bridge.BackendError("HTTP 403")
    assert bridge.probe(cfg, "https://bridge.example.com", 10, http_fn=rejecting, clock=ticking(), sleep=lambda s: None) == 1


def test_the_probe_uses_a_custom_query_url_with_the_marker_substituted(cfg):
    urls = []

    def transport(url, headers, data, timeout):
        urls.append(url)
        if data is not None:
            return 200, b'{"accepted": 1}'
        marker = url.rsplit("=", 1)[1]
        return 200, json.dumps({"data": {"result": [{"values": [["0", f"bridge probe {marker}"]]}]}}).encode()

    code = bridge.probe(cfg, "https://bridge.example.com", 10, query_url="https://q.example.com/find?m={marker}",
                        http_fn=transport, clock=ticking(), sleep=lambda s: None)
    assert code == 0 and "{marker}" not in urls[1] and urls[1].startswith("https://q.example.com/find?m=")


# --------------------------------------------------------------------------- configuration

def test_it_refuses_to_start_without_secrets(monkeypatch, capsys):
    with pytest.raises(bridge.ConfigError) as exc:
        bridge.Config.from_env({"BRIDGE_BACKEND_URL": "https://logs.example.com/loki/api/v1/push"})
    assert "BRIDGE_DRAIN_SECRET" in str(exc.value) and "BRIDGE_WEBHOOK_SECRET" in str(exc.value)
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    assert bridge.main(["serve", "--port", "0"]) == 1
    assert "refusing to start" in capsys.readouterr().err


def test_the_config_file_is_read_and_the_environment_overrides_it():
    example = pathlib.Path(__file__).with_name("config.example.json")
    env = {k: v for k, v in ENV.items() if k not in ("BRIDGE_SERVICE_MAP", "BRIDGE_SAMPLE_RATE_STATIC")}
    env["BRIDGE_CONFIG"] = str(example)
    cfg, data = bridge.Config.from_env(env), json.loads(example.read_text())
    assert cfg.service_map == data["service_map"] and list(cfg.label_allowlist) == data["label_allowlist"]
    assert cfg.sample_rate_static == data["sample_rates"]["static"]
    assert bridge.Config.from_env({**env, "BRIDGE_SAMPLE_RATE_STATIC": "0.25"}).sample_rate_static == 0.25
    assert bridge.Config.from_env({**env, "BRIDGE_LABEL_ALLOWLIST": "service_name, level"}).label_allowlist == (
        "service_name", "level")
